#!.venv/bin/python
"""
Interactive Roborock REPL for V1 vacuums (S7 = roborock.vacuum.a15).

    pip install -r requirements.txt   (python-roborock==7.14.1; the cache workaround depends on 7.x internals)
    python roborock_repl.py --email you@example.com

First run logs in via email code and stores the session in ~/.roborock-repl/.
Later runs reuse it (no new code request, no cloud rate limits).
"""
import argparse
import asyncio
import atexit
import dataclasses
import json
import pathlib
import pickle
import shlex
import sys
import threading

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
from roborock.exceptions import RoborockException, RoborockInvalidCode
from roborock.web_api import RoborockApiClient

import info_pretty

STATE_DIR = pathlib.Path.home() / ".roborock-repl"
SESSION_FILE = STATE_DIR / "session.json"
CACHE_FILE = STATE_DIR / "cache.pkl"
HISTORY_FILE = STATE_DIR / "history"
CONSUMABLE_NAMES = [c.value for c in ConsumableAttribute]
HISTORY_LENGTH = 1000
NO_RAW_COMMANDS = {"cls", "quit", "exit"}  # nothing to show plainly
ASKING_COMMANDS = {"clean", "zone", "goto", "spot", "resume", "pause", "stop", "dock", "routine"}  # accept -y

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


def ainput(prompt, remember=False):
    """input() in a daemon thread: Ctrl+C cancels the wait and exit never blocks on a pending prompt.
    Only lines read with remember=True stay in the persistent history."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result, error):
        if not future.done():
            future.set_exception(error) if error else future.set_result(result)

    def read():
        try:
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


def plain(obj, cls):
    """Copy a trait object into its plain data class (traits hold live RPC channels that can't be pickled)."""
    if obj is None or type(obj) is cls:
        return obj
    return cls(**{f.name: getattr(obj, f.name) for f in dataclasses.fields(cls) if f.init})


def serialize_cache(data):
    # python-roborock 7.x stores the NetworkInfo / DeviceFeatures traits themselves in the cache
    devices = {duid: dataclasses.replace(d, network_info=plain(d.network_info, NetworkInfo),
                                         device_features=plain(d.device_features, DeviceFeatures))
               for duid, d in data.device_info.items()}
    return pickle.dumps(dataclasses.replace(data, device_info=devices))


async def load_user_data(email, relogin):
    STATE_DIR.mkdir(exist_ok=True)
    if SESSION_FILE.exists() and not relogin:
        saved = json.loads(SESSION_FILE.read_text())
        if saved["email"] == email:
            info_pretty.render_logged_in(email, saved=True)
            return UserData.from_dict(saved["user_data"])

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
    SESSION_FILE.write_text(json.dumps({"email": email, "user_data": user_data.as_dict()}, default=vars))
    SESSION_FILE.chmod(0o600)
    info_pretty.render_logged_in(email, saved=False)
    return user_data


async def wait_connected(device, timeout=20):
    for _ in range(timeout * 2):
        if device.is_connected:
            return True
        await asyncio.sleep(0.5)
    return False


class Repl:
    def __init__(self, device, show_secrets):
        self.device = device
        self.p = device.v1_properties
        self.show_secrets = show_secrets
        self.selected = []  # segment ids
        self.passes = 1
        self._matches = []
        self.routines = None  # cloud data, cached for Tab completion (None = not loaded / failed)
        if readline is not None:
            readline.set_completer(self.complete)

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
        for name in traits:
            try:
                await getattr(self.p, name).refresh()
            except RoborockException as e:
                problems.append((name, e))
        try:
            await self.p.home.discover_home()
        except RoborockException as e:
            problems.append(("home", e))
            try:
                await self.p.rooms.refresh()
            except RoborockException as e2:
                problems.append(("rooms", e2))
        try:
            await self.load_routines()
        except RoborockException as e:
            problems.append(("routines", e))
        return problems

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
        result = await self.p.command.send(command, params)
        raw_line(f"{command} -> {result}")

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
            answer = (await ainput(f"{question}? [y/N] ")).strip().lower()
        except EOFError:
            answer = ""
        if answer in ("y", "yes", "j", "ja"):
            return True
        if raw:
            raw_line(f"Cancelled: {info_pretty.CANCELLED_DETAIL}")
        else:
            info_pretty.render_cancelled()
        return False

    async def send_pretty(self, command, params=None, label=None):
        info_pretty.render_sent(command, await self.p.command.send(command, params), label)

    async def perform(self, args, command, params=None, ask=None, label=None, hint=None, plan=None):
        """The one path for commands that act on the robot: plan → confirm → send.
        Raw mode skips the plan and names the exact command in its question; ask=None sends right away (find)."""
        if "raw" in args:
            if not ask or await self.confirm(args, ask, detail=f"{command.value} {params or ''}".rstrip()):
                await self.send(command, params)
            return
        if plan:
            await plan()
        if ask and not await self.confirm(args, ask):
            return
        await self.send_pretty(command, params, label)
        if hint:
            info_pretty.render_hint(hint)

    async def refresh_for_plan(self, with_map=False):
        await self.p.status.refresh()  # show the settings the robot will actually use
        if with_map:
            try:
                await self.p.map_content.refresh()  # for the mini map; the command is sent either way
            except RoborockException as e:
                info_pretty.render_warning(f"No map preview: {e}")

    async def cmd_clean(self, *args):
        if self.selected:
            command, params = RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": self.selected, "repeat": self.passes}]
        else:
            command, params = RoborockCommand.APP_START, None

        async def plan():
            await self.refresh_for_plan()
            info_pretty.render_clean_plan(self)
        await self.perform(args, command, params, ask="Start cleaning", plan=plan)

    async def cmd_zone(self, *args):
        coords = without(args, "raw", "-y")
        if len(coords) != 4:
            return self.usage("zone", args)
        zone = [*map(int, coords)]

        async def plan():
            await self.refresh_for_plan(with_map=True)
            info_pretty.render_zone_plan(self, zone)
        await self.perform(args, RoborockCommand.APP_ZONED_CLEAN, [[*zone, self.passes]], ask="Start zone cleaning",
                           plan=plan)

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
        await self.perform(args, RoborockCommand.APP_START, ask="Resume the robot", label="▶ Resume")

    async def cmd_stop(self, *args):
        await self.perform(args, RoborockCommand.APP_STOP, ask="Stop the robot", label="■ Stop",
                           hint="The robot stays where it is: 'dock' sends it home.")

    async def cmd_dock(self, *args):
        await self.perform(args, RoborockCommand.APP_CHARGE, ask="Dock the robot", label="⌂ Return to dock")

    async def cmd_spot(self, *args):
        await self.perform(args, RoborockCommand.APP_SPOT, ask="Start spot cleaning", label="✱ Spot clean",
                           hint="Cleans the area around the robot.")

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
        records = []
        for record_id in (self.p.clean_summary.records or [])[:count]:
            try:
                records.append((record_id, await self.p.clean_summary.get_clean_record(record_id)))
            except RoborockException:
                pass
        return records

    async def cmd_routines(self, *args):
        if "raw" not in args:
            try:
                await self.load_routines()
            except RoborockException:
                pass  # shown as "could not be loaded"
            return info_pretty.render_routines(self.routines)
        self.dump_routines(await self.load_routines())

    def dump_routines(self, routines):
        dump("Routines", [r.as_dict() for r in routines], info_pretty.gist(f"{len(routines)} routines on this account"))

    async def cmd_routine(self, *args):
        values = without(args, "raw", "-y")
        if not values:
            return await self.cmd_routines(*args)
        if "raw" in args:
            if await self.confirm(args, f"Start routine {int(values[0])}"):
                await self.p.routines.execute_routine(int(values[0]))
                raw_line("Routine started.")
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
        await self.p.routines.execute_routine(matches[0].id)
        info_pretty.render_done("Routine started via the Roborock cloud")

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
            try:
                parts = shlex.split(await ainput("roborock> ", remember=True))
            except EOFError:
                return print()
            except ValueError as e:  # e.g. unbalanced quotes
                info_pretty.render_error(e)
                continue
            if not parts:
                continue
            if parts[0] in ("quit", "exit"):
                return
            handler = getattr(self, f"cmd_{parts[0]}", None)
            if handler is None:
                info_pretty.render_unknown_command(parts[0], self.command_names())
                continue
            try:
                await handler(*parts[1:])
            except (RoborockException, ValueError) as e:  # ValueError covers bad numbers and bad JSON
                if "raw" in parts[1:]:
                    raw_line(f"Error: {e}")
                else:
                    info_pretty.render_error(e)


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
        ("quit", "leave"),
    ]),
]
# command -> (syntax as in HELP_SECTIONS, example, optional note): one wording for every usage message
USAGE = {
    "info": ("info [pretty|raw]", "info raw", None),
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
               "Tab completes commands and their arguments; ↑/↓ and Ctrl+R search the history, kept across sessions.")
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
    user_data = await load_user_data(args.email, args.relogin)
    cache = FileCache(CACHE_FILE, serialize_fn=serialize_cache)
    with info_pretty.console.status("Loading your devices ..."):
        manager = await create_device_manager(UserParams(username=args.email, user_data=user_data), cache=cache)
    try:
        vacuums = [d for d in await manager.get_devices() if d.v1_properties]
        if not vacuums:
            return info_pretty.render_problem("No V1 vacuum found on this account.")
        info_pretty.render_devices(vacuums)
        device = vacuums[0] if len(vacuums) == 1 else await choose_device(vacuums)

        with info_pretty.console.status(info_pretty.Text(f"Connecting to {device.name} ...")):
            connected = await wait_connected(device)
        if not connected:
            return info_pretty.render_problem(f"Could not connect to {device.name}: robot offline or unreachable.")
        info_pretty.render_connected(device)

        repl = Repl(device, args.show_secrets)
        await repl.cmd_info()
        await repl.run()
        goodbye = device.name  # only after a normal quit; Ctrl+C and errors print their own line
    finally:
        with info_pretty.console.status("Disconnecting ..."):
            try:
                await cache.flush()
            finally:
                await manager.close()
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
