#!.venv/bin/python
"""
Interactive Roborock REPL for V1 vacuums (S7 = roborock.vacuum.a15).

    pip install -r requirements.txt   (python-roborock==7.14.1; the cache workaround depends on 7.x internals)
    python roborock_repl.py --email you@example.com

First run logs in via email code and stores the session in ~/.roborock-repl/, optionally encrypted
(together with the device cache) with a passphrase. Later runs reuse it (no new code request, no cloud rate limits).
"""
import argparse
import asyncio
import atexit
import contextlib
import dataclasses
import functools
import getpass
import json
import pathlib
import pickle
import shlex
import signal
import sys
import threading
import time

try:
    import readline  # line editing, tab completion and history where available
except ImportError:
    readline = None

from roborock import RoborockCommand
from roborock.data import NetworkInfo, UserData
from roborock.device_features import DeviceFeatures
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.devices.file_cache import FileCache
from roborock.devices.traits.v1.consumeable import ConsumableAttribute
from roborock.exceptions import RoborockConnectionException, RoborockException, RoborockInvalidCode, RoborockTimeout
from roborock.web_api import RoborockApiClient

import info_pretty
import secret_store

STATE_DIR = pathlib.Path.home() / ".roborock-repl"
SESSION_FILE = STATE_DIR / "session.json"
CACHE_FILE = STATE_DIR / "cache.pkl"
HISTORY_FILE = STATE_DIR / "history"
CONSUMABLE_NAMES = [c.value for c in ConsumableAttribute]
HISTORY_LENGTH = 1000
NO_RAW_COMMANDS = {"cls", "quit", "exit"}  # nothing to show plainly
START_TIMEOUT, START_POLL = 30, 2  # seconds: how long / how often to check that a started action really runs
ASKING_COMMANDS = {"clean", "zone", "goto", "spot", "resume", "pause", "stop", "dock", "routine"}  # accept -y
# Connection upkeep (seconds). The library's own recovery after a sleep can take minutes (MQTT backoff up to 6 h),
# so the REPL checks the line before a command and builds fresh connections when it is dead.
SLEEP_GAP = 60  # wall clock ran this much further than the monotonic clock: the computer was asleep
IDLE_CHECK = 5 * 60  # no robot answer for this long: check the line before the next command
PROBE_TIMEOUT = 4  # the quick status read that checks the line
RECONNECT_TRY_FOR = 30  # keep trying this long (Wi-Fi often comes back a few seconds after a wake-up)
RECONNECT_FLOOR = 30  # at least this long between automatic reconnects (each is a new cloud login)
CLOSE_TIMEOUT = 5  # closing connections that died in a sleep must not hang
# commands that never talk to the robot: no connection check before them
LOCAL_COMMANDS = {"help", "cls", "clear", "passes", "select", "rooms", "floors", "commands", "reconnect"}
# safe to send again automatically after a dropped connection: they only read
READ_ONLY_COMMANDS = {"info", "status", "consumables", "history", "where", "map", "options", "routines"}
SHOW_OR_SET_COMMANDS = {"fan", "water", "route", "mode", "volume", "dnd", "childlock", "led", "floor", "reset",
                        "routine"}  # read-only without a value
DELIVERY_RAW = {  # raw-mode line after a dropped connection, by Repl.delivery
    "unsent": "Nothing was sent to the robot: run the command again.",
    "unknown": "The command may or may not have reached the robot: check 'status'.",
    "accepted": "Accepted by the robot; only the follow-up read was cut off.",
}
CONNECTION_ERROR_TEXT = ("timed out", "not connected", "failed to publish", "no available connection",
                         "failed to connect")

OPTIONAL_TRAITS = [
    "child_lock", "led_status", "flow_led_status", "valley_electricity_timer",
    "dust_collection_mode", "wash_towel_mode", "smart_wash_params", "mop_dryer",
]


def raw_line(*parts):
    """print() replacement for the one-line raw outputs: same text, colored in a terminal."""
    info_pretty.render_plain(" ".join(str(p) for p in parts))


def dump(title, obj, summary=None):
    """Raw data as highlighted JSON under a heading (plain JSON when piped); summary = plain-words gist."""
    info_pretty.json_block(title, obj, summary)


def mask(secret, show):
    if not secret or show:
        return secret
    return "*" * (len(secret) - 4) + secret[-4:]


def without(args, *drop):
    """Arguments minus the flags (default: raw)."""
    drop = drop or ("raw",)
    return [a for a in args if a not in drop]


def pick(items, token, name):
    """(unique match or None, candidates): an exact name (any case) wins over partial matches,
    so "Flur" still picks "Flur" when there is also "Flur oben"."""
    wanted = token.lower()
    hits = [i for i in items if (name(i) or "").lower() == wanted] \
        or [i for i in items if wanted in (name(i) or "").lower()]
    return (hits[0] if len(hits) == 1 else None), hits


def shlex_words(line):
    """Words of a partly typed line; tolerates an unfinished quote."""
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def protect_terminal():
    """readline turns off echo and line mode while a prompt waits. Ctrl+C can end the script while the
    input thread is still inside readline, so restore the terminal's original settings on every exit."""
    global restore_terminal
    if not sys.stdin.isatty():
        return
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (ImportError, OSError):  # no termios (Windows) or no real terminal
        return

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except OSError:
            pass

    restore_terminal = restore
    atexit.register(restore)


def restore_terminal():  # replaced by protect_terminal() when there is a terminal to restore
    pass


def setup_readline():
    """Persistent history (~/.roborock-repl/history) and Tab completion; a no-op without readline."""
    if readline is None:
        return
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:  # first run: no history yet
        pass
    readline.set_history_length(HISTORY_LENGTH)
    readline.set_completer_delims(" \t\n")  # complete whole words like main_brush_work_time
    libedit = "libedit" in (readline.__doc__ or "")  # macOS ships libedit instead of GNU readline
    readline.parse_and_bind("bind ^I rl_complete" if libedit else "tab: complete")
    atexit.register(save_history)  # runs on quit, Ctrl+C and errors alike


def save_history():
    try:
        STATE_DIR.mkdir(exist_ok=True)
        readline.write_history_file(HISTORY_FILE)
        HISTORY_FILE.chmod(0o600)
    except OSError:
        pass


def tidy_history(line, remember):
    """Drop answers to prompts (login code, y/N, device number) and consecutive duplicates."""
    if readline is None or not line.strip():
        return
    n = readline.get_current_history_length()
    if n and readline.get_history_item(n) == line and (
            not remember or (n > 1 and readline.get_history_item(n - 1) == line)):
        readline.remove_history_item(n - 1)


def masked_input(prompt, mark="•"):
    """getpass that shows a dot per typed character. Backspace and Ctrl+U edit, arrow keys are ignored.
    Echo and line mode go off, signals stay on, so Ctrl+C still ends the script (protect_terminal
    restores the terminal). Plain getpass without a terminal or termios."""
    try:
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (ImportError, OSError, ValueError):
        return getpass.getpass(prompt)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ECHO | termios.ICANON)
    raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
    out, typed = sys.stdout, []
    out.write(prompt)
    out.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, raw)
    try:
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r", ""):  # "" = end of input
                break
            if ch == "\x04" and not typed:  # Ctrl+D on an empty line, like input()
                raise EOFError
            if ch in ("\x7f", "\b"):
                if typed:
                    typed.pop()
                    out.write("\b \b")
            elif ch == "\x15":  # Ctrl+U: clear the line
                out.write("\b \b" * len(typed))
                typed.clear()
            elif ch == "\x1b":  # escape sequence (arrow keys etc.): skip it
                if sys.stdin.read(1) == "[":
                    while (c := sys.stdin.read(1)) and not (c.isalpha() or c == "~"):  # e.g. ESC [ A, ESC [ 3 ~
                        pass
            elif ch >= " ":
                typed.append(ch)
                out.write(mark)
            out.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.write("\n")
        out.flush()
    return "".join(typed)


def ainput(prompt, remember=False, secret=False):
    """input() in a daemon thread: Ctrl+C cancels the wait and exit never blocks on a pending prompt.
    Only lines read with remember=True stay in the persistent history; secret=True hides the typing."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result, error):
        if not future.done():
            future.set_exception(error) if error else future.set_result(result)

    def read():
        try:
            if secret:
                result, error = masked_input(prompt), None
            else:
                result, error = input(prompt), None
                tidy_history(result, remember)
        except Exception as e:  # EOFError on Ctrl+D / closed stdin
            result, error = None, e
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:  # loop already closed (shutting down)
            pass

    threading.Thread(target=read, daemon=True).start()
    return future


def errors_of(status):
    """(dock error, robot error) as plain ints, 0 = none."""
    return tuple(int(getattr(e, "value", e) or 0) for e in (status.dock_error_status, status.error_code))


def plain(obj, cls):
    """Copy a trait object into its plain data class (traits hold live RPC channels that can't be pickled)."""
    if obj is None or type(obj) is cls:
        return obj
    return cls(**{f.name: getattr(obj, f.name) for f in dataclasses.fields(cls) if f.init})


def serialize_cache(data, key=None):
    # python-roborock 7.x stores the NetworkInfo / DeviceFeatures traits themselves in the cache
    devices = {duid: dataclasses.replace(d, network_info=plain(d.network_info, NetworkInfo),
                                         device_features=plain(d.device_features, DeviceFeatures))
               for duid, d in data.device_info.items()}
    pickled = pickle.dumps(dataclasses.replace(data, device_info=devices))
    magic = secret_store.CACHE_MAGIC  # also authenticated (AAD): the header can't be swapped unnoticed
    return magic + secret_store.encrypt(key, pickled, aad=magic) if key else pickled


def deserialize_cache(raw, key=None):
    """An unreadable encrypted cache (no or another passphrase, e.g. after --relogin) is dropped:
    returning None makes FileCache start empty, and it is rebuilt from the cloud.
    A plain cache is read as is and written encrypted on the next save when a key is set."""
    if raw.startswith(secret_store.CACHE_MAGIC):
        try:
            if key is None:
                raise secret_store.WrongKey("no passphrase")
            raw = secret_store.decrypt(key, raw[len(secret_store.CACHE_MAGIC):], aad=secret_store.CACHE_MAGIC)
        except secret_store.WrongKey:
            info_pretty.render_hint("The saved device cache belongs to another login: it is rebuilt from the cloud.")
            return None
    return pickle.loads(raw)


async def ask_new_passphrase():
    """Passphrase for a newly saved login, or None (Enter) to store it unencrypted."""
    while True:
        first = await ainput(f"Passphrase to protect the saved login "
                             f"({secret_store.MIN_PASSPHRASE}+ characters, Enter to skip): ", secret=True)
        if not first:
            return None
        if len(first) < secret_store.MIN_PASSPHRASE:
            info_pretty.render_problem(f"At least {secret_store.MIN_PASSPHRASE} characters, please.")
            continue
        if await ainput("Repeat: ", secret=True) == first:
            return first
        info_pretty.render_problem("The two entries differ. Try again.")


async def unlock(saved):
    """Key for an encrypted session (3 tries). Raises RoborockException when they all fail."""
    for attempt in range(1, 4):
        passphrase = await ainput("Passphrase: ", secret=True)
        with info_pretty.console.status("Unlocking ..."):
            key = secret_store.derive_key(passphrase, saved["kdf"])
            try:
                return UserData.from_dict(json.loads(
                    secret_store.decrypt(key, secret_store.unb64(saved["user_data_enc"])))), key
            except secret_store.WrongKey:
                pass
        if attempt < 3:
            info_pretty.render_problem("Wrong passphrase.")
    raise RoborockException("Wrong passphrase 3 times. Forgotten it? --relogin logs in again and sets a new one.")


def save_session(email, user_data, passphrase):
    """Writes session.json; returns the cache key (None without passphrase)."""
    session, key = {"email": email}, None
    plain_json = json.dumps(user_data.as_dict(), default=vars)
    if passphrase:
        kdf = secret_store.new_kdf()
        with info_pretty.console.status("Encrypting ..."):
            key = secret_store.derive_key(passphrase, kdf)
        session |= {"kdf": kdf, "user_data_enc": secret_store.b64(secret_store.encrypt(key, plain_json.encode()))}
    else:
        session["user_data"] = json.loads(plain_json)
    SESSION_FILE.write_text(json.dumps(session))
    SESSION_FILE.chmod(0o600)
    return key


async def load_user_data(email, relogin):
    """(user data, cache key or None). The passphrase is only chosen when a new login is saved."""
    STATE_DIR.mkdir(exist_ok=True)
    if SESSION_FILE.exists() and not relogin:
        saved = json.loads(SESSION_FILE.read_text())
        if saved["email"] == email:
            if "user_data_enc" in saved:
                user_data, key = await unlock(saved)
            else:
                user_data, key = UserData.from_dict(saved["user_data"]), None
            info_pretty.render_logged_in(email, saved=True, encrypted=key is not None)
            return user_data, key

    api = RoborockApiClient(username=email)
    with info_pretty.console.status("Requesting a login code ..."):
        await api.request_code()
    info_pretty.render_code_sent(email)
    for attempt in range(1, 4):
        code = (await ainput("Code: ")).strip()
        try:
            with info_pretty.console.status("Checking the code ..."):
                user_data = await api.code_login(code)
            break
        except RoborockInvalidCode:
            if attempt == 3:
                raise
            info_pretty.render_problem("That code was not accepted. Check the newest email and try again.")
    key = save_session(email, user_data, await ask_new_passphrase())
    info_pretty.render_logged_in(email, saved=False, encrypted=key is not None)
    return user_data, key


async def wait_connected(device, timeout=20):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not device.is_connected:
        if loop.time() >= end:
            return False
        await asyncio.sleep(0.5)
    return True


def is_connection_error(error):
    """The line to the robot is gone (as opposed to the robot answering with an error).
    python-roborock 7.x raises a plain RoborockException for timeouts, hence the text check."""
    return isinstance(error, (RoborockTimeout, RoborockConnectionException)) or (
        type(error) is RoborockException and any(t in str(error).lower() for t in CONNECTION_ERROR_TEXT))


def retry_safe(command, args):
    """May this command be sent again automatically after a dropped connection? Only if it just reads."""
    return command in READ_ONLY_COMMANDS or (command in SHOW_OR_SET_COMMANDS and not without(args, "raw", "-y"))


class Connection:
    """The device manager and the chosen vacuum. reconnect() replaces both with fresh connections:
    a new MQTT session and local socket, none of the library's backoff left over from before a sleep."""

    def __init__(self, user_params, cache):
        self.user_params, self.cache = user_params, cache
        self.manager = self.device = None
        self.last_reconnect = None  # loop time of the last reconnect attempt

    async def vacuums(self):
        self.manager = await create_device_manager(self.user_params, cache=self.cache)
        return [d for d in await self.manager.get_devices() if d.v1_properties]

    async def reconnect(self):
        """Fresh connections to the same robot, retried for RECONNECT_TRY_FOR seconds. self.device is the new
        device even when this fails (the library keeps trying in the background); raises when it stays unreachable."""
        loop = asyncio.get_running_loop()
        self.last_reconnect = loop.time()
        deadline = loop.time() + RECONNECT_TRY_FOR
        duid, name = self.device.duid, self.device.name
        while True:
            await self.close_manager()
            try:
                vacuums = await self.vacuums()
            except RoborockException as e:  # e.g. no network yet for the home data
                problem = e
            else:
                device = next((d for d in vacuums if d.duid == duid), None)
                if device is None:
                    raise RoborockException(f"{name} is no longer on this account.")
                self.device = device
                if await wait_connected(device, max(5, deadline - loop.time())):
                    return device
                problem = "robot offline or unreachable"
            if loop.time() >= deadline:
                raise RoborockConnectionException(f"Could not reach {name}: {problem}")
            await asyncio.sleep(3)

    async def close_manager(self):
        if self.manager is not None:
            manager, self.manager = self.manager, None
            with contextlib.suppress(Exception):  # a timeout, or whatever a connection that died in a sleep raises
                await asyncio.wait_for(manager.close(), CLOSE_TIMEOUT)

    async def close(self):
        try:
            await self.cache.flush()
            if CACHE_FILE.exists():
                CACHE_FILE.chmod(0o600)  # FileCache creates it with the default umask
        finally:
            await self.close_manager()


class Repl:
    def __init__(self, conn, show_secrets):
        self.conn = conn
        self.adopt(conn.device)
        self.show_secrets = show_secrets
        self.last_ok = (time.time(), time.monotonic())  # last robot answer: wall clock and monotonic clock
        self.delivery = "unknown"  # of the running command: unsent / unknown (on its way) / accepted by the robot
        self.running = None  # the command task Ctrl+C cancels
        self.sigint_default = None  # Ctrl+C handler outside commands (asyncio.run's: ends the script)
        self.selected = []  # segment ids
        self.passes = 1
        self._matches = []
        self.routines = None  # cloud data, cached for Tab completion (None = not loaded / failed)
        if readline is not None:
            readline.set_completer(self.complete)

    def adopt(self, device):
        """Use this device object (a reconnect brings a new one); selection, passes and routines stay."""
        self.device = device
        self.p = device.v1_properties

    # ---------- connection upkeep ----------

    def mark_ok(self):
        self.last_ok = (time.time(), time.monotonic())

    async def ensure_connection(self):
        """Before a command that talks to the robot: after a sleep, a long pause or a drop, check the line and
        reconnect when it's dead. False = the robot stays unreachable and the command is skipped."""
        wall, mono = time.time(), time.monotonic()
        slept = (wall - self.last_ok[0]) - (mono - self.last_ok[1]) > SLEEP_GAP  # monotonic stops during sleep
        idle = mono - self.last_ok[1] > IDLE_CHECK
        if self.device.is_connected:
            if not slept and not idle:
                return True
            try:
                with info_pretty.console.status("Checking the connection ..."):
                    await asyncio.wait_for(self.p.status.refresh(), PROBE_TIMEOUT)
                self.mark_ok()
                return True
            except (RoborockException, TimeoutError):
                pass
        return await self.reconnect("the computer was asleep" if slept else "the connection dropped")

    async def reconnect(self, reason=None):
        """Fresh connections to the robot. reason=None: asked for by 'reconnect', so no wait between attempts."""
        loop = asyncio.get_running_loop()
        name = self.device.name
        if reason and self.conn.last_reconnect is not None \
                and loop.time() - self.conn.last_reconnect < RECONNECT_FLOOR:
            info_pretty.render_still_offline(name)  # don't hammer the cloud login
            return False
        if reason:
            info_pretty.render_connection_lost(reason)
        try:
            with info_pretty.console.status(info_pretty.Text(f"Reconnecting to {name} ...")):
                await self.conn.reconnect()
        except RoborockException as e:
            info_pretty.render_reconnect_failed(e)
            return False
        finally:
            self.adopt(self.conn.device)
        with info_pretty.console.status("Reading the status ..."):
            await self.reload_basics()
        info_pretty.render_connected(self.device)
        self.mark_ok()
        return True

    async def reload_basics(self):
        """A new device object starts empty: status (current floor) and rooms, for 'rooms' and Tab completion.
        The floor list comes from the cache; nothing here is worth failing the reconnect for."""
        with contextlib.suppress(RoborockException):
            await self.p.status.refresh()
        try:
            await self.p.home.discover_home()
        except RoborockException:
            with contextlib.suppress(RoborockException):
                await self.p.rooms.refresh()

    async def cmd_reconnect(self, *_):
        await self.reconnect()

    @contextlib.contextmanager
    def ctrl_c_cancels(self, task):
        """While a command runs, Ctrl+C cancels just that command instead of ending the script."""
        self.running = task
        self.arm_ctrl_c()
        try:
            yield
        finally:
            self.disarm_ctrl_c()
            self.running = None

    @contextlib.contextmanager
    def ctrl_c_ends(self):
        """Around a prompt inside a command: Ctrl+C ends the script, as at the main prompt.
        Cancelling would leave the prompt's reader thread waiting, and it would swallow the next line."""
        self.disarm_ctrl_c()
        try:
            yield
        finally:
            self.arm_ctrl_c()

    def arm_ctrl_c(self):
        if self.running is None:
            return
        self.sigint_default = signal.getsignal(signal.SIGINT)
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self.running.cancel)
        except (NotImplementedError, RuntimeError):  # no signal handlers in this loop (Windows): Ctrl+C ends the script
            self.sigint_default = None

    def disarm_ctrl_c(self):
        if self.sigint_default is None:
            return
        asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, self.sigint_default)  # remove_signal_handler alone would drop asyncio.run's
        self.sigint_default = None

    async def execute(self, command, handler, args):
        """One command: connection check, the command, and after a dropped connection a reconnect
        plus one more try, but only for commands that just read. Anything else is never sent twice."""
        if command in LOCAL_COMMANDS:  # no robot involved: also leaves the sleep detection alone
            return await handler(*args)
        if not await self.ensure_connection():
            return
        self.delivery = "unknown"
        try:
            await handler(*args)
            self.mark_ok()
        except RoborockException as e:
            if not is_connection_error(e):
                raise
            self.render_failure(e, args)
            again, delivery = retry_safe(command, args), self.delivery
            if await self.reconnect("the connection dropped") and again:
                info_pretty.render_hint("Trying once more ...")
                await handler(*args)
                self.mark_ok()
            elif not again:
                if "raw" in args:
                    raw_line(DELIVERY_RAW[delivery])
                else:
                    info_pretty.render_delivery(delivery)

    async def dispatch(self, parts):
        """Run one command line (already split): Ctrl+C cancels it, errors are shown, the REPL goes on."""
        handler = getattr(self, f"cmd_{parts[0]}", None)
        if handler is None:
            return info_pretty.render_unknown_command(parts[0], self.command_names())
        task = asyncio.ensure_future(self.execute(parts[0], handler, parts[1:]))
        try:
            with self.ctrl_c_cancels(task):
                await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the REPL itself is being cancelled
            if "raw" in parts[1:]:
                raw_line("Cancelled: the robot may already have received the command.")
            else:
                info_pretty.render_command_cancelled()
        except (RoborockException, ValueError) as e:  # ValueError covers bad numbers and bad JSON
            self.render_failure(e, parts[1:])

    def render_failure(self, error, args):
        if "raw" in args:
            raw_line(f"Error: {error}")
        else:
            info_pretty.render_error(error)

    # ---------- tab completion (runs in the input thread: only data already in memory, no robot calls) ----------

    def command_names(self):
        return sorted({name[4:] for name in dir(self) if name.startswith("cmd_")} | {"quit", "exit"})

    def argument_options(self, command):
        s = self.p.status
        quote = lambda name: f'"{name}"' if " " in name else name  # shlex keeps quoted names together
        options = {
            "select": lambda: [quote(r.name) for r in self.current_rooms() if r.name],
            "routine": lambda: [quote(r.name) for r in self.routines or [] if r.name]
                               + [str(r.id) for r in self.routines or []],
            "floor": lambda: [quote(i.name) for i in self.floors().values() if i.name],  # what 'floor' accepts
            "fan": lambda: list(s.fan_speed_mapping.values()),
            "water": lambda: list(s.water_mode_mapping.values()),
            "route": lambda: list(s.mop_route_mapping.values()),
            "mode": lambda: [m.value for m in s.cleaning_mode_options],
            "dnd": lambda: ["on", "off"], "childlock": lambda: ["on", "off"], "led": lambda: ["on", "off"],
            "info": lambda: ["pretty"], "help": lambda: ["examples"],
            "reset": lambda: CONSUMABLE_NAMES,
            "raw": lambda: [c.value for c in RoborockCommand],
            "commands": lambda: ["app", "get", "set"],
        }
        try:
            found = options.get(command, list)()
        except Exception:  # noqa: BLE001  (missing data must never break typing)
            found = []
        return found + ([] if command in NO_RAW_COMMANDS else ["raw"]) + (["-y"] if command in ASKING_COMMANDS else [])

    def completions(self, line):
        """Candidates for the word being typed at the end of `line`."""
        words = line.split()
        current = "" if not line or line[-1].isspace() else words[-1]
        pool = self.command_names() if len(words) == 0 or (len(words) == 1 and current) \
            else self.argument_options(words[0])
        if words and words[0] == "select":  # don't offer rooms already typed on this line
            typed = {w.strip('"').lower() for w in shlex_words(line)[1:]}
            pool = [o for o in pool if o.strip('"').lower() not in typed]
        if line.count('"') % 2:  # open quote: readline only replaces the text after the last space
            quoted = line[line.rindex('"') + 1:]
            last = line.split(" ")[-1]
            names = [o.strip('"') for o in pool if o.strip('"').lower().startswith(quoted.lower())]
            start = len(quoted) - len(last)  # where `last` begins inside the name (negative: it holds the quote)
            return sorted({(n[start:] if start >= 0 else last[:-start] + n) + '"' for n in names})
        prefix = current.lstrip('"').lower()
        return sorted({o for o in pool if o.strip('"').lower().startswith(prefix)})

    def complete(self, text, state):
        """readline completer: called with state 0, 1, 2, ... until it returns None."""
        if state == 0:
            try:
                self._matches = self.completions(readline.get_line_buffer()[: readline.get_endidx()])
            except Exception:  # noqa: BLE001
                self._matches = []
        return self._matches[state] if state < len(self._matches) else None

    # ---------- info ----------

    async def refresh_all(self):
        """Re-read every trait. Returns the problems as (source, error) so each view can show them its way."""
        problems = []
        traits = ["status", "consumables", "clean_summary", "dnd", "sound_volume", "maps", "network_info"]
        traits += [t for t in OPTIONAL_TRAITS if getattr(self.p, t) is not None]
        streak = []  # connection errors in a row

        def note(source, error):
            self.give_up_if_dead(error, streak)
            if is_connection_error(error):
                streak.append(error)
            else:
                streak.clear()
            problems.append((source, error))

        for name in traits:
            try:
                await getattr(self.p, name).refresh()
                streak.clear()
            except RoborockException as e:
                note(name, e)
        try:
            await self.p.home.discover_home()
        except RoborockException as e:
            note("home", e)
            try:
                await self.p.rooms.refresh()
            except RoborockException as e2:
                note("rooms", e2)
        try:
            await self.load_routines()
        except RoborockException as e:
            note("routines", e)
        return problems

    def give_up_if_dead(self, error, streak):
        """In a series of reads: stop at a connection error when the line looks dead (not connected, or the second
        in a row; streak = the ones just before), so an outage costs one or two timeouts, not one per read.
        A single slow read stays a listed problem."""
        if is_connection_error(error) and (streak or not self.device.is_connected):
            raise error

    async def load_routines(self):
        """Fetch the routines from the Roborock cloud and keep them for completion and the overview."""
        self.routines = None
        self.routines = await self.p.routines.get_routines()
        return self.routines

    def dump_device(self):
        d, prod = self.device.device_info, self.device.product
        info = {
            "name": d.name, "model": prod.model, "product": prod.name, "duid": d.duid,
            "firmware": d.fv, "serial": d.sn, "online": d.online, "time_zone": d.time_zone_id,
            "local_key": mask(d.local_key, self.show_secrets),
            "connection": info_pretty.connection_via(self.device),
        }
        dump("Device", info, info_pretty.summary_device(self.device, self.show_secrets))
        dump("Product schema (data points)", [s.as_dict() for s in prod.schema or []],
             info_pretty.gist(f"{len(prod.schema or [])} data points the robot reports (id, code, rw mode, type)"))

    def dump_options(self):
        options = {label: list(values) for label, (values, _) in info_pretty.option_rows(self.p).items()}
        dump("Settable options", {**options, "consumables": CONSUMABLE_NAMES}, info_pretty.summary_options(self.p))

    async def cmd_info(self, *args):
        mode = args[0] if args else "pretty"
        if mode not in ("pretty", "raw"):
            return self.usage("info", args)
        with info_pretty.console.status("Reading everything from the robot ..."):
            problems = await self.refresh_all()
            records = [] if mode == "raw" else [r for _, r in await self.fetch_records(3)]
        info_pretty.render_refresh_problems(problems)
        if mode == "raw":
            await self.dump_info_raw()
        else:
            info_pretty.render(self, mask(self.device.device_info.local_key, self.show_secrets), records)

    async def dump_info_raw(self):
        self.dump_device()
        dump("Device features", self.p.device_features, info_pretty.summary_features(self.p.device_features))
        dump("Status", self.p.status, info_pretty.summary_status(self.p.status))
        dump("Network", self.p.network_info, info_pretty.summary_network(self.p.network_info))
        dump("Consumables", self.p.consumables, info_pretty.summary_consumables(self.p.consumables))
        dump("Clean summary", self.p.clean_summary, info_pretty.summary_clean(self.p.clean_summary))
        dump("Do not disturb", self.p.dnd, info_pretty.summary_dnd(self.p.dnd))
        dump("Volume", self.p.sound_volume, info_pretty.gist(f"{self.p.sound_volume.volume}%"))
        for name in OPTIONAL_TRAITS:
            if (trait := getattr(self.p, name)) is not None:
                dump(name, trait, info_pretty.summary_trait(trait))
        dump("Maps", self.p.maps, info_pretty.summary_maps(self.p.maps, self.p.status.current_map))
        await self.cmd_floors("raw")
        self.dump_options()
        self.dump_routines(self.routines if self.routines is not None else await self.load_routines())

    async def cmd_status(self, *args):
        s = self.p.status
        await s.refresh()
        if "raw" not in args:
            return info_pretty.render_status(self)
        task = getattr(s.in_cleaning, "name", s.in_cleaning)  # e.g. segment_clean_not_complete, complete
        raw_line(f"state={s.state_name} task={task} battery={s.battery}% fan={s.fan_speed_name} water={s.water_mode_name} "
              f"route={s.mop_route_name} mode={s.current_cleaning_mode_name} error={s.error_code} "
              f"area={s.clean_area} time={s.clean_time}s map={s.current_map} repeat={s.repeat}")
        raw_line(f"selected rooms={self.selected or 'none (= full clean)'} passes={self.passes}")

    # ---------- floors & rooms ----------

    def floors(self):
        return self.p.home.home_map_info or {}

    def current_rooms(self):
        info = self.floors().get(self.p.status.current_map)
        return info.rooms if info else self.p.rooms.rooms or []

    async def cmd_floors(self, *args):
        if "raw" not in args:
            return info_pretty.render_floors(self)
        current = self.p.status.current_map
        for flag, info in self.floors().items():
            mark = "*" if flag == current else " "
            raw_line(f"{mark} floor {flag}: {info.name or '(unnamed)'}")
            for r in info.rooms:
                raw_line(f"      segment {r.segment_id:>3}  {r.name}")

    async def cmd_floor(self, *args):
        raw = "raw" in args
        values = without(args)
        if not values:
            return await self.cmd_floors(*args)
        target = values[0]
        for flag, info in self.floors().items():
            if target == str(flag) or target.lower() == (info.name or "").lower():
                if not raw and flag == self.p.status.current_map:
                    info_pretty.render_floor_switch(info.name, flag, already=True)
                    return info_pretty.render_rooms(self)
                if not raw:
                    info_pretty.render_floor_switch(info.name, flag)
                await self.p.maps.set_current_map(flag)
                await self.p.home.refresh()
                await self.p.status.refresh()  # current_map drives 'rooms' and the floor marker
                self.selected = []
                if raw:
                    return raw_line(f"Switched to floor {flag} ({info.name}). Selection cleared.")
                info_pretty.render_done(f"Now on {info.name or flag}, room selection cleared")
                return info_pretty.render_rooms(self)
        if raw:
            return raw_line("Unknown floor. Use 'floors'.")
        info_pretty.render_problem(f"Unknown floor '{target}'.")
        info_pretty.render_floors(self)

    async def cmd_rooms(self, *args):
        if "raw" not in args:
            return info_pretty.render_rooms(self)
        for i, r in enumerate(self.current_rooms(), 1):
            mark = "x" if r.segment_id in self.selected else " "
            raw_line(f"[{mark}] {i:>2}. {r.name}  (segment {r.segment_id})")

    def resolve_room(self, token):
        """(room or None, candidates): segment id, then list number, then name."""
        rooms = self.current_rooms()
        if token.isdigit():
            n = int(token)
            by_seg = [r for r in rooms if r.segment_id == n]
            if by_seg:
                return by_seg[0], by_seg
            if 1 <= n <= len(rooms):
                return rooms[n - 1], [rooms[n - 1]]
        return pick(rooms, token, lambda r: r.name)

    async def cmd_select(self, *args):
        raw = "raw" in args
        notes = []  # (outcome, token, detail) per token for the pretty summary
        for token in without(args):
            room, candidates = self.resolve_room(token)
            if room is None:
                if raw:
                    raw_line(f"No unique room for '{token}'")
                    continue
                notes.append(("ambiguous", token, [r.name for r in candidates]) if candidates
                             else ("unknown", token, None))
            elif room.segment_id in self.selected:
                notes.append(("already", token, room))
            else:
                self.selected.append(room.segment_id)
                notes.append(("added", token, room))
        if raw:
            return await self.cmd_rooms("raw")
        info_pretty.render_select(notes)
        await self.cmd_rooms()

    async def cmd_clear(self, *args):
        previous = info_pretty.selected_labels(self)
        self.selected = []
        if "raw" in args:
            return raw_line("Selection cleared.")
        info_pretty.render_cleared(previous)

    async def cmd_passes(self, *args):
        values = without(args)
        valid = bool(values) and values[0] in ("1", "2", "3")
        if valid:
            self.passes = int(values[0])
        if "raw" in args:
            return raw_line(f"passes={self.passes}")
        if values and not valid:
            self.usage("passes", args)
        info_pretty.render_passes(self.passes)

    # ---------- cleaning settings ----------

    async def show_options(self, label, options, raw, unknown=None):
        """Options of one setting; unknown = the rejected value (then nothing was sent)."""
        if raw:
            prefix = f"Unknown {label} '{unknown}': nothing sent. Options" if unknown else label
            return raw_line(f"{prefix}: {', '.join(options)}")
        if unknown:
            info_pretty.render_problem(f"Unknown {label} '{unknown}': nothing sent. Options:")
        info_pretty.render_option(self.p, label)

    async def set_from_mapping(self, mapping, command, args, label):
        raw, values = "raw" in args, without(args)
        if not values:
            await self.p.status.refresh()
            return await self.show_options(label, list(mapping.values()), raw)
        codes = {name.lower(): code for code, name in mapping.items()}
        if (code := codes.get(values[0].lower())) is None:
            return await self.show_options(label, list(mapping.values()), raw, unknown=values[0])
        await self.p.command.send(command, [code])
        self.delivery = "accepted"
        await self.cmd_status(*(["raw"] if raw else []))

    async def cmd_options(self, *args):
        await self.p.status.refresh()
        if "raw" in args:
            self.dump_options()
        else:
            info_pretty.render_options(self.p)

    async def cmd_fan(self, *args):
        await self.set_from_mapping(self.p.status.fan_speed_mapping, RoborockCommand.SET_CUSTOM_MODE, args, "fan")

    async def cmd_water(self, *args):
        await self.set_from_mapping(self.p.status.water_mode_mapping,
                                    RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, args, "water")

    async def cmd_route(self, *args):
        await self.set_from_mapping(self.p.status.mop_route_mapping, RoborockCommand.SET_MOP_MODE, args, "route")

    async def cmd_mode(self, *args):
        raw, values = "raw" in args, without(args)
        await self.p.status.refresh()
        options = [m.value for m in self.p.status.cleaning_mode_options]
        if not values:
            return await self.show_options("mode", options, raw)
        mode = next((o for o in options if o.lower() == values[0].lower()), None)  # exact names only
        if mode is None:
            return await self.show_options("mode", options, raw, unknown=values[0])
        await self.p.status.set_cleaning_mode(mode)
        await self.cmd_status(*(["raw"] if raw else []))

    # ---------- actions ----------

    async def send(self, command, params=None):
        self.delivery = "unknown"
        result = await self.p.command.send(command, params)
        self.delivery = "accepted"
        raw_line(f"{command} -> {result}")
        return result

    def usage(self, name, args=()):
        """Same usage message everywhere: syntax as in 'help', an example and a short note."""
        syntax, example, note = USAGE[name]
        if "raw" in args:
            raw_line(f"Usage: {syntax}")
            return raw_line(f"e.g. {example}" + (f"  ·  {note}" if note else ""))
        info_pretty.render_usage(syntax, example, note)

    async def confirm(self, args, question, detail=None):
        """Ask before the robot starts moving; only an explicit yes (or -y in args) proceeds.
        Raw mode asks the same question and adds the exact command in brackets (detail)."""
        raw = "raw" in args
        if "-y" in args:
            return True
        if raw and detail:
            question = f"{question} ({detail})"
        try:
            with self.ctrl_c_ends():
                answer = (await ainput(f"{question}? [y/N] ")).strip().lower()
        except EOFError:
            answer = ""
        if answer in ("y", "yes", "j", "ja"):
            if await self.ensure_connection():  # the prompt may have waited through a sleep
                return True
            if raw:
                raw_line("Nothing was sent to the robot.")
            else:
                info_pretty.render_hint("Nothing was sent to the robot.")
            return False
        if raw:
            raw_line(f"Cancelled: {info_pretty.CANCELLED_DETAIL}")
        else:
            info_pretty.render_cancelled()
        return False

    async def send_pretty(self, command, params=None, label=None):
        self.delivery = "unknown"
        result = await self.p.command.send(command, params)
        self.delivery = "accepted"
        info_pretty.render_sent(command, result, label)
        return result

    async def perform(self, args, command, params=None, ask=None, label=None, hint=None, plan=None, starts=False):
        """The one path for commands that act on the robot: plan → confirm → send (→ check it started).
        Raw mode skips the plan and names the exact command in its question; ask=None sends right away (find).
        starts=True: the command should set the robot working, so watch the status afterwards (check_started)."""
        self.delivery = "unsent"  # until send() / send_pretty()
        raw = "raw" in args
        if raw:
            if ask and not await self.confirm(args, ask, detail=f"{command.value} {params or ''}".rstrip()):
                return
            if starts:
                await self.p.status.refresh()  # errors before the start, so only new ones count as failure
            before = errors_of(self.p.status)
            result = await self.send(command, params)
        else:
            if plan:
                await plan()  # every plan refreshes the status
            if ask and not await self.confirm(args, ask):
                return
            before = errors_of(self.p.status)
            result = await self.send_pretty(command, params, label)
            if hint:
                info_pretty.render_hint(hint)
        if starts and result in ("ok", ["ok"]):
            await self.check_started(raw, before)

    async def check_started(self, raw, before):
        """Accepted is not started: a missing dust bin, for example, only shows up in the status afterwards.
        Polls the status until the robot works, reports a (new) error, or START_TIMEOUT passes.
        Ctrl+C (which cancels the command, see ctrl_c_cancels) only stops the waiting, not the robot."""
        status, loop = self.p.status, asyncio.get_running_loop()
        started = loop.time()

        async def poll():
            while loop.time() - started < START_TIMEOUT:
                await asyncio.sleep(START_POLL)
                await status.refresh()
                kind = info_pretty.state_kind(status.state_name)
                if kind == "working":
                    return "started"
                if kind == "error" or any(now and now != was for now, was in zip(errors_of(status), before)):
                    return "error"
            return "error" if any(errors_of(status)) else "timeout"  # an old error kept it from starting

        try:
            with contextlib.nullcontext() if raw else \
                    info_pretty.console.status("Checking that the robot starts ... (Ctrl+C stops waiting)"):
                outcome = await poll()
        except asyncio.CancelledError:
            if self.running is None or self.running is not asyncio.current_task():
                raise  # not our Ctrl+C (e.g. shutting down)
            self.running.uncancel()  # the command itself goes on: report what was seen so far
            outcome = "cancelled"
        waited = round(loop.time() - started)
        if raw:
            dock, error = errors_of(status)
            return raw_line(f"start check: {outcome} after {waited}s state={status.state_name} "
                            f"error={status.error_code} dock_error={dock}")
        info_pretty.render_start_check(outcome, status, waited)

    async def refresh_for_plan(self, with_map=False):
        await self.p.status.refresh()  # show the settings the robot will actually use
        if with_map:
            try:
                await self.p.map_content.refresh()  # for the mini map; the command is sent either way
            except RoborockException as e:  # MQTT only: may fail while the local line works
                info_pretty.render_warning(f"No map preview: {e}")

    async def cmd_clean(self, *args):
        if self.selected:
            command, params = RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": self.selected, "repeat": self.passes}]
        else:
            command, params = RoborockCommand.APP_START, None

        async def plan():
            await self.refresh_for_plan()
            info_pretty.render_clean_plan(self)
        await self.perform(args, command, params, ask="Start cleaning", plan=plan, starts=True)

    async def cmd_zone(self, *args):
        coords = without(args, "raw", "-y")
        if len(coords) != 4:
            return self.usage("zone", args)
        zone = [*map(int, coords)]

        async def plan():
            await self.refresh_for_plan(with_map=True)
            info_pretty.render_zone_plan(self, zone)
        await self.perform(args, RoborockCommand.APP_ZONED_CLEAN, [[*zone, self.passes]], ask="Start zone cleaning",
                           plan=plan, starts=True)

    async def cmd_goto(self, *args):
        coords = without(args, "raw", "-y")
        if len(coords) != 2:
            return self.usage("goto", args)
        target = [*map(int, coords)]

        async def plan():
            await self.refresh_for_plan(with_map=True)
            info_pretty.render_goto_plan(self, target)
        await self.perform(args, RoborockCommand.APP_GOTO_TARGET, target, ask="Start driving to the target", plan=plan)

    async def cmd_where(self, *args):
        await self.p.map_content.refresh()
        if "raw" not in args:
            return info_pretty.render_where(self)
        data = self.p.map_content.map_data
        raw_line("position:", getattr(data, "vacuum_position", None), " charger:", getattr(data, "charger", None))

    async def cmd_map(self, *args):
        files = without(args)
        if len(files) > 1 or (files and pathlib.Path(files[0]).suffix.lower() != ".png"):
            return self.usage("map", args)  # e.g. 'map help' must not write a PNG named help
        path = pathlib.Path(files[0] if files else "map.png").resolve()
        await self.p.map_content.refresh()
        image = self.p.map_content.image_content
        if image:
            path.write_bytes(image)
        if "raw" in args:
            raw_line(f"Map written to {path}" if image else "No map image from the robot: nothing written")
        else:
            info_pretty.render_map(self.p.map_content, path if image else None)

    async def cmd_pause(self, *args):
        await self.perform(args, RoborockCommand.APP_PAUSE, ask="Pause the robot", label="‖ Pause")

    async def cmd_resume(self, *args):
        await self.perform(args, RoborockCommand.APP_START, ask="Resume the robot", label="▶ Resume", starts=True)

    async def cmd_stop(self, *args):
        await self.perform(args, RoborockCommand.APP_STOP, ask="Stop the robot", label="■ Stop",
                           hint="The robot stays where it is: 'dock' sends it home.")

    async def cmd_dock(self, *args):
        await self.perform(args, RoborockCommand.APP_CHARGE, ask="Dock the robot", label="⌂ Return to dock")

    async def cmd_spot(self, *args):
        await self.perform(args, RoborockCommand.APP_SPOT, ask="Start spot cleaning", label="✱ Spot clean",
                           hint="Cleans the area around the robot.", starts=True)

    async def cmd_find(self, *args):  # the only action that never asks: it just makes the robot speak
        await self.perform(args, RoborockCommand.FIND_ME, label="♪ Find me", hint="The robot announces itself.")

    async def cmd_resolve(self, *args):
        status = self.p.status
        await status.refresh()  # resolve_error() decides from the cached error codes
        if "raw" in args:
            await status.resolve_error()
            return await self.cmd_status("raw")
        if not info_pretty.render_resolve(status):  # nothing to resolve: resolve_error() would send nothing either
            return
        await status.resolve_error()
        await self.cmd_status()

    # ---------- device settings ----------

    async def cmd_volume(self, *args):
        values = without(args)
        if values:
            await self.p.sound_volume.set_volume(int(values[0]))
        await self.p.sound_volume.refresh()
        if "raw" in args:
            raw_line(f"volume={self.p.sound_volume.volume}")
        else:
            info_pretty.render_volume(self.p)

    async def toggle(self, trait, args, label, show, usage):
        if trait is None:
            return info_pretty.render_problem(f"This robot has no {label} setting.")
        values = without(args)
        if values and values[0] not in ("on", "off"):  # never treat a typo as "off"
            return self.usage(usage, args)
        if values:
            await (trait.enable() if values[0] == "on" else trait.disable())
        await trait.refresh()
        if "raw" in args:
            raw_line(f"{label}={'on' if trait.is_on else 'off'}")
        else:
            show()

    async def cmd_dnd(self, *args):
        await self.toggle(self.p.dnd, args, "dnd", lambda: info_pretty.render_dnd(self.p), "dnd")

    async def cmd_childlock(self, *args):
        await self.toggle(self.p.child_lock, args, "child lock",
                          lambda: info_pretty.render_switch("Child lock", self.p.child_lock), "childlock")

    async def cmd_led(self, *args):
        await self.toggle(self.p.led_status, args, "led", lambda: info_pretty.render_switch("LED", self.p.led_status),
                          "led")

    async def cmd_consumables(self, *args):
        await self.p.consumables.refresh()
        if "raw" in args:
            dump("Consumables", self.p.consumables, info_pretty.summary_consumables(self.p.consumables))
        else:
            info_pretty.render_consumables(self.p)

    async def cmd_reset(self, *args):
        names = CONSUMABLE_NAMES
        values = without(args)
        if "raw" in args:
            if not values:
                self.usage("reset", args)
                return raw_line("Options:", ", ".join(names))
            await self.p.consumables.reset_consumable(ConsumableAttribute.from_str(values[0]))
            return await self.cmd_consumables("raw")
        await self.p.consumables.refresh()
        if not values:
            return info_pretty.render_reset_options(self.p.consumables, names)
        matches = pick(names, values[0], str)[1]
        if len(matches) != 1:
            problem = f"'{values[0]}' matches {', '.join(matches)}: be more specific." if matches \
                else f"Unknown consumable '{values[0]}'."
            return info_pretty.render_reset_options(self.p.consumables, names, problem)
        info_pretty.render_reset(self.p.consumables, matches[0])
        await self.p.consumables.reset_consumable(ConsumableAttribute.from_str(matches[0]))
        await self.cmd_consumables()

    async def cmd_history(self, *args):
        raw = "raw" in args
        count = next((int(a) for a in args if a.isdigit()), 3 if raw else 10)
        await self.p.clean_summary.refresh()
        if not raw:
            return info_pretty.render_history(self, [r for _, r in await self.fetch_records(count)])
        info_pretty.render_history_raw(self.p.clean_summary, await self.fetch_records(count))

    async def fetch_records(self, count):
        """[(record_id, CleanRecord)] of the newest runs; a record the robot can't deliver is skipped."""
        records, streak = [], []
        for record_id in (self.p.clean_summary.records or [])[:count]:
            try:
                records.append((record_id, await self.p.clean_summary.get_clean_record(record_id)))
                streak.clear()
            except RoborockException as e:  # a record the robot can't deliver is skipped
                self.give_up_if_dead(e, streak)
                if is_connection_error(e):
                    streak.append(e)
        return records

    async def cmd_routines(self, *args):
        if "raw" not in args:
            try:
                await self.load_routines()
            except RoborockException as e:
                if is_connection_error(e):
                    raise
                # otherwise shown as "could not be loaded"
            return info_pretty.render_routines(self.routines)
        self.dump_routines(await self.load_routines())

    def dump_routines(self, routines):
        dump("Routines", [r.as_dict() for r in routines], info_pretty.gist(f"{len(routines)} routines on this account"))

    async def cmd_routine(self, *args):
        self.delivery = "unsent"
        values = without(args, "raw", "-y")
        if not values:
            return await self.cmd_routines(*args)
        if "raw" in args:
            if await self.confirm(args, f"Start routine {int(values[0])}"):
                await self.p.status.refresh()
                before = errors_of(self.p.status)
                self.delivery = "unknown"
                await self.p.routines.execute_routine(int(values[0]))
                self.delivery = "accepted"
                raw_line("Routine started.")
                await self.check_started(True, before)
            return
        routines = await self.load_routines()
        wanted = values[0].lower()
        matches = [r for r in routines if str(r.id) == wanted] or pick(routines, wanted, lambda r: r.name)[1]
        if len(matches) != 1:
            info_pretty.render_problem(f"'{values[0]}' matches several routines: use the id."
                                       if matches else f"No routine '{values[0]}'.")
            return await self.cmd_routines()
        info_pretty.render_routine_start(matches[0])
        if not await self.confirm(args, f"Start routine {matches[0].name or matches[0].id}"):
            return
        await self.p.status.refresh()
        before = errors_of(self.p.status)
        self.delivery = "unknown"
        await self.p.routines.execute_routine(matches[0].id)
        self.delivery = "accepted"
        info_pretty.render_done("Routine started via the Roborock cloud")
        await self.check_started(False, before)

    # ---------- raw ----------

    async def cmd_raw(self, *args):
        one_line = len(args) > 1 and args[-1] == "raw"  # trailing raw = old one-line output
        args = args[:-1] if one_line else args
        if not args:
            return self.usage("raw")
        params = json.loads(" ".join(args[1:])) if len(args) > 1 else None
        if one_line:
            return await self.send(args[0], params)
        info_pretty.render_raw_request(args[0], params, known=args[0] in {c.value for c in RoborockCommand})
        info_pretty.render_raw_result(args[0], await self.p.command.send(args[0], params))

    async def cmd_commands(self, *args):
        filters = [a.lower() for a in without(args)]
        names = sorted(c.value for c in RoborockCommand if not filters or filters[0] in c.value)
        if "raw" in args:
            raw_line(", ".join(names))
        else:
            info_pretty.render_commands(names, filters[0] if filters else None)

    async def cmd_cls(self, *_):
        """Clear the screen and the terminal's scrollback, then show the startup banner and connection line;
        does nothing when output isn't a terminal."""
        if sys.stdout.isatty():
            sys.stdout.write("\033[H\033[2J\033[3J")  # home + clear screen + clear scrollback
            sys.stdout.flush()
            info_pretty.render_banner()
            info_pretty.render_connected(self.device)

    async def cmd_help(self, *args):
        examples = "examples" in args
        if "raw" in args:
            info_pretty.render_plain(HELP_EXAMPLES if examples else HELP, help_text=True)
        elif examples:
            info_pretty.render_examples(HELP_EXAMPLES)
        else:
            info_pretty.render_help(HELP_SECTIONS, HELP_FOOTER)

    async def run(self):
        await self.cmd_help()
        while True:
            prompt = "roborock> " if self.device.is_connected else "roborock (offline)> "
            try:
                parts = shlex.split(await ainput(prompt, remember=True))
            except EOFError:
                return print()
            except ValueError as e:  # e.g. unbalanced quotes
                info_pretty.render_error(e)
                continue
            if not parts:
                continue
            if parts[0] in ("quit", "exit"):
                return
            await self.dispatch(parts)


# (title, color, [(command, description)]): rendered by `help`, plain text by `help raw`
HELP_SECTIONS = [
    ("Overview", "cyan", [
        ("info [pretty|raw]", "re-read everything: overview (default) | all of it as raw JSON"),
        ("status", "state, battery, cleaning settings, room selection"),
        ("consumables", "brush / filter / sensor wear"),
        ("history [n]", "totals, daily activity and a timeline of the last n runs (default 10, max 20)"),
        ("where", "room, position and dock distance with a mini map"),
        ("map [file.png]", "save the map image (default map.png) and preview it"),
    ]),
    ("Floors & rooms", "green", [
        ("floors | floor <id|name>", "floor tree with rooms | switch active floor"),
        ("rooms", "rooms on the active floor (selected highlighted)"),
        ("select <room...>", 'add rooms by (partial) name, list number or segment id ("select Kitchen 2")'),
        ("clear", "clear room selection"),
        ("passes <1-3>", "passes for room/zone cleaning"),
    ]),
    ("Cleaning settings", "magenta", [
        ("options", "all settable options, current value highlighted"),
        ("fan|water|route [value]", "show options or set suction / water flow / mop route"),
        ("mode [value]", "show or set high-level cleaning mode (if supported)"),
    ]),
    ("Actions: these move the robot", "yellow", [
        ("clean [-y]", "clean selected rooms (or whole floor if none selected): shows the plan, asks first"),
        ("zone x1 y1 x2 y2 [-y]", "zone clean (mm, see where) with current passes: mini map, asks first"),
        ("goto x y [-y]", "drive to a point (mm, see where): mini map, asks first"),
        ("spot [-y]", "spot clean around the robot, asks first"),
        ("pause | resume | stop [-y]", "pause / continue / stop in place (all ask first; -y skips that)"),
        ("dock [-y]", "end the clean and return to base, asks first"),
        ("routines | routine <id|name> [-y]", "list | start a routine (asks first)"),
    ]),
    ("Device", "blue", [
        ("volume [0-100]", "show or set the voice volume"),
        ("dnd | childlock | led [on|off]", "show or switch do not disturb / child lock / LED"),
        ("find", "the robot announces itself"),
        ("resolve", "clear the current robot or dock error (says which first)"),
        ("reset [consumable]", "list resettable parts | reset a wear counter after replacing the part"),
    ]),
    ("Expert", "red", [
        ("raw <cmd> [json]", "send any command (no safety checks), shows the answer as JSON"),
        ("commands [filter]", "list all known commands, optionally filtered"),
    ]),
    ("General", "grey50", [
        ("help [examples]", "this list | step-by-step examples"),
        ("cls", "clear screen and scrollback, then show banner and connection ('clear' clears the room selection)"),
        ("reconnect", "fresh connections to the robot (done automatically after a sleep or a drop)"),
        ("quit", "leave"),
    ]),
]
# command -> (syntax as in HELP_SECTIONS, example, optional note): one wording for every usage message
USAGE = {
    "info": ("info [pretty|raw]", "info raw", None),
    "map": ("map [file.png]", "map flat.png", "saves map.png when no file is given"),
    "passes": ("passes <1-3>", "passes 2", "passes for room and zone cleaning"),
    "zone": ("zone x1 y1 x2 y2 [-y]", "zone 24000 24000 26000 26000", "coordinates in mm, see 'where'"),
    "goto": ("goto x y [-y]", "goto 25000 26000", "coordinates in mm, see 'where'"),
    "dnd": ("dnd [on|off]", "dnd on", None),
    "childlock": ("childlock [on|off]", "childlock on", None),
    "led": ("led [on|off]", "led on", None),
    "reset": ("reset [consumable]", "reset main_brush_work_time", "'reset' alone lists them"),
    "raw": ("raw <cmd> [json]", "raw set_custom_mode [102]", "'commands' lists them"),
}
HELP_FOOTER = ("Display commands accept raw for the plain output, e.g. 'status raw', 'help raw'.\n"
               "Tab completes commands and their arguments; ↑/↓ and Ctrl+R search the history, kept across sessions.\n"
               "Ctrl+C cancels a running command; at the prompt it quits.")
HELP_WIDTH = max(len(cmd) for _, _, rows in HELP_SECTIONS for cmd, _ in rows) + 2  # >= 2 spaces before descriptions
HELP = "\n" + "\n".join(f"{cmd:<{HELP_WIDTH}}{desc}" for _, _, rows in HELP_SECTIONS for cmd, desc in rows) \
    + f"\n\n{HELP_FOOTER}\n"

HELP_EXAMPLES = """
1. Pick the floor (switching clears the room selection)
     floors                    list floors and their rooms (★ = active)
     floor Upstairs            switch by name or floor id
   The floor list needs home discovery, which the robot refuses while it is
   busy or off the dock ("device action locked"); then only the active floor's
   rooms are known and 'floor' answers "Unknown floor". Dock it and restart.

2. Clean one or more rooms n times
     select Bedroom            by (partial) name, list number or segment id;
                               several at once: select Bedroom Office
     passes 2                  1-3; any other value is ignored
     rooms                     check the selection (✔)
     status                    check the passes
     clean                     shows the plan and asks "Start cleaning? [y/N]"
   Careful: with nothing selected, 'clean' cleans the whole floor;
   'clear' empties the selection.

3. Pause and continue
     pause                     asks "Pause the robot? [y/N]" (pause -y skips that)
     resume

4. Stop and return to base
     dock                      ends the clean and drives home (asks first; dock -y skips that)
     stop                      stops in place only; follow with 'dock' (asks first; stop -y skips that)
"""


async def choose_device(vacuums):
    while True:
        answer = (await ainput("Device number: ")).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(vacuums):
            return vacuums[int(answer) - 1]
        info_pretty.render_problem(f"Enter a number from 1 to {len(vacuums)}.")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--relogin", action="store_true", help="request a new email code")
    parser.add_argument("--show-secrets", action="store_true", help="print the local_key unmasked")
    args = parser.parse_args()

    protect_terminal()
    setup_readline()
    info_pretty.render_banner()
    user_data, key = await load_user_data(args.email, args.relogin)
    cache = FileCache(CACHE_FILE, serialize_fn=functools.partial(serialize_cache, key=key),
                      deserialize_fn=functools.partial(deserialize_cache, key=key))
    conn = Connection(UserParams(username=args.email, user_data=user_data), cache)
    try:
        with info_pretty.console.status("Loading your devices ..."):
            vacuums = await conn.vacuums()
        if not vacuums:
            return info_pretty.render_problem("No V1 vacuum found on this account.")
        info_pretty.render_devices(vacuums)
        device = conn.device = vacuums[0] if len(vacuums) == 1 else await choose_device(vacuums)

        with info_pretty.console.status(info_pretty.Text(f"Connecting to {device.name} ...")):
            connected = await wait_connected(device)
        if not connected:
            return info_pretty.render_problem(f"Could not connect to {device.name}: robot offline or unreachable.")
        info_pretty.render_connected(device)

        repl = Repl(conn, args.show_secrets)
        await repl.dispatch(["info"])
        await repl.run()
        goodbye = conn.device.name  # only after a normal quit; Ctrl+C and errors print their own line
    finally:
        with info_pretty.console.status("Disconnecting ..."):
            await conn.close()
    info_pretty.render_goodbye(goodbye)


if __name__ == "__main__":
    # Ctrl+C cancels main(); its finally block still flushes the cache and disconnects
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        restore_terminal()  # before printing: the prompt may have left echo off
        info_pretty.render_interrupted()
        sys.exit(130)
    except RoborockException as e:  # login / network problems: one clear line instead of a traceback
        info_pretty.render_error(e)
        sys.exit(1)
