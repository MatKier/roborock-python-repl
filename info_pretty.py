"""All terminal output of roborock_repl.py, rendered with rich.

Sections: styles and constants, formatting helpers, panel builders (return renderables),
map preview, output (render_* print views, one-line results, help, startup, errors),
and the raw dumps (JSON blocks with guarded plain-words summaries).
Text from the robot or the user is always passed as Text, never parsed as rich markup."""
import dataclasses
import difflib
import datetime
import io
import json
import math
import re

from PIL import Image, ImageChops, ImageDraw

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.color import Color
from rich.highlighter import RegexHighlighter
from rich.json import JSON
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

from roborock import const
from vacuum_map_parser_base.map_data import Point
from roborock.exceptions import (CommandVacuumError, RoborockConnectionException, RoborockDeviceBusy,
                                 RoborockException, RoborockTimeout, UnknownMethodError, VacuumError)

# ---------- styles, console and constants ----------

class RawHighlighter(RegexHighlighter):
    """Colors the one-line raw outputs without changing a single character of them."""
    base_style = "raw."
    highlights = [
        r"(?P<key>\b[A-Za-z_]+)(?P<eq>=)",
        r"^(?P<key2>[a-z]+ [a-z]+)(?==)",  # two-word keys only start a line: "child lock=", "selected rooms="
        r"(?P<number>(?<![\w.])-?\d+(\.\d+)?%?(?![\w.]))",
        r"(?P<const>\b(None|True|False)\b)",
        r"(?P<ok>\bok\b|\bon\b(?=$|\s))",
        r"(?P<arrow>->)",
        r"(?P<mark>\[x\])",
        r"(?P<get>\bget_\w+)", r"(?P<app>\bapp_\w+|RoborockCommand\.APP_\w+)", r"(?P<set>\bset_\w+)",
        r"(?P<error>^Error:)",
    ]

class HelpHighlighter(RegexHighlighter):
    """`help raw` / `help examples raw`: command column, placeholders and headings."""
    base_style = "help."
    highlights = [
        r"(?m)^(?P<command>\S.*?)(?=\s{2,}\S)",  # command column of the command list
        r"(?m)^ {5}(?P<command2>\S.*?)(?=\s{2,}|$)",  # commands in the examples
        r"(?m)^(?P<heading>\d+\. .*)$",
        r"(?P<placeholder><[^>\n]*>|\[[^\]\n]*\])",
    ]

THEME = Theme({
    "raw.key": "grey62", "raw.key2": "grey62", "raw.eq": "grey50", "raw.number": "bold cyan", "raw.const": "italic magenta",
    "raw.ok": "bold green", "raw.arrow": "grey50", "raw.mark": "bold green",
    "raw.get": "green", "raw.app": "yellow", "raw.set": "magenta", "raw.error": "bold red",
    "help.command": "bold cyan", "help.command2": "bold green", "help.heading": "bold cyan",
    "help.placeholder": "italic grey62",
})

console = Console(theme=THEME)

# (field, label, lifetime, unit): "h" parts report seconds worked, "×" parts report usage counts
CONSUMABLES = [
    ("main_brush_work_time", "Main brush", const.MAIN_BRUSH_REPLACE_TIME, "h"),
    ("side_brush_work_time", "Side brush", const.SIDE_BRUSH_REPLACE_TIME, "h"),
    ("filter_work_time", "Filter", const.FILTER_REPLACE_TIME, "h"),
    ("sensor_dirty_time", "Sensors (clean)", const.SENSOR_DIRTY_REPLACE_TIME, "h"),
    ("moproller_work_time", "Mop roller", const.MOP_ROLLER_REPLACE_TIME, "h"),
    ("strainer_work_times", "Strainer", const.STRAINER_REPLACE_TIME, "×"),
    ("cleaning_brush_work_times", "Dock cleaning brush", const.CLEANING_BRUSH_REPLACE_TIME, "×"),
    ("dust_collection_work_times", "Dust bag", const.DUST_COLLECTION_REPLACE_TIME, "×"),
]

CONSUMABLE_INFO = {field: (label, lifetime, unit) for field, label, lifetime, unit in CONSUMABLES}

SWITCHES = [("child_lock", "Child lock"), ("led_status", "LED"), ("flow_led_status", "Flow LED"),
            ("valley_electricity_timer", "Off-peak charging"), ("mop_dryer", "Mop dryer")]

MODES = [("dust_collection_mode", "Dust collection"), ("wash_towel_mode", "Mop wash"),
         ("smart_wash_params", "Smart wash")]

TASK_KINDS = {"global_clean_not_complete": "whole-floor clean", "zone_clean_not_complete": "zone clean",
              "segment_clean_not_complete": "room clean"}

CLEAN_TYPES = {"all_zone": "whole floor", "draw_zone": "zone", "select_zone": "rooms", "quick_build": "mapping",
               "video_patrol": "patrol", "pet_patrol": "patrol"}

COMMAND_KINDS = [("get_", "green", "reads"), ("app_", "yellow", "acts on the robot"),
                 ("set_", "magenta", "changes a setting")]

STATE_STYLES = {"error": ("bold red", "✖"), "charging": ("bold blue", "⚡"), "working": ("bold green", "▶"),
                "idle": ("bold yellow", "■")}

MARKS = {"ok": ("✔ ", "bold green"), "no": ("✘ ", "bold yellow"), "error": ("✘ ", "bold red"),
         "warn": ("⚠ ", "bold yellow")}

MAP_MARKS = {"dock": ("◆", "bold yellow"), "robot": ("●", "bold red"), "target": ("✚", "bold white on magenta"),
             "zone": ("■", "bold white")}  # (character, style) for markers and legend

CANCELLED_DETAIL = "nothing sent to the robot."  # shared by the pretty and the raw cancel line

# ---------- formatting helpers (values -> str / Text) ----------

def txt(value, style=""):
    """Plain Text (never parsed as rich markup, so names with [brackets] are safe)."""
    return Text("n/a" if value is None else str(value), style=style)

def human(name):
    return str(getattr(name, "name", name) or "n/a").replace("_", " ")

def bar(pct, color, width=10):
    filled = round(max(0, min(100, pct)) / 100 * width)
    return Text.assemble(("█" * filled, color), ("░" * (width - filled), "grey35"))

def level_color(pct, good, warn):
    return "green" if pct >= good else "yellow" if pct >= warn else "red"

def area(square_meters, fmt=""):
    """Area for display; the robot sometimes reports none (None -> 0)."""
    return f"{square_meters or 0:{fmt}} m²"

def duration(seconds):
    if seconds is None:
        return "n/a"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"

def on_off(value):
    return Text.assemble(("● on", "green") if value else ("○ off", "grey50"))

def online_text(online):
    return Text.assemble(("● online", "green") if online else ("● offline", "red"))

def connection_via(device):
    return "local" if device.is_local_connected else "cloud (MQTT)"

def xy_mm(x, y):
    return f"x {x:.0f}  y {y:.0f} mm"

def distance_m(x1, y1, x2, y2):
    return math.hypot(x1 - x2, y1 - y2) / 1000

def dnd_window(dnd):
    return f"{dnd.start_time:%H:%M}–{dnd.end_time:%H:%M}" if dnd.start_time and dnd.end_time else ""

def started(record):
    return datetime.datetime.fromtimestamp(record.begin).strftime("%a %d.%m. %H:%M") if record.begin else "n/a"

def kv_table():
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    return table

def panel(body, title, expand=True, color="blue"):
    return Panel(body, title=Text(title, style="bold"), title_align="left", box=box.ROUNDED,
                 border_style=color, expand=expand)

def safe(title, build, expand=True):
    """Build one section; a broken section shows n/a instead of hiding the others."""
    try:
        return build()
    except Exception as e:  # noqa: BLE001
        return panel(txt(f"n/a ({e})", "red"), title, expand, color="red")

def state_kind(name):
    """error / charging / working / idle, checked in that order (charging_problem is an error)."""
    n = name or ""
    if any(w in n for w in ("error", "problem", "offline", "locked")):
        return "error"
    if any(w in n for w in ("charging", "docking", "returning")):
        return "charging"
    if any(w in n for w in ("clean", "mop", "wash", "spot", "zoned", "segment", "mapping", "going_to")):
        return "working"
    return "idle"

def state_style(name):
    return STATE_STYLES[state_kind(name)]

def command_kind(name):
    """(color, label) of a command by prefix: get_ reads, app_ acts, set_ changes a setting."""
    return next(((color, label) for prefix, color, label in COMMAND_KINDS if name.startswith(prefix)), ("", "other"))

def command_text(cmd, color):
    """Command syntax: names in the section color, <placeholders> and [optionals] dim, | separators grey."""
    out = Text()
    for part in re.split(r"(<[^>]*>|\[[^\]]*\]|\|)", cmd):
        if part == "|":
            out.append(part, style="grey50")
        elif part.startswith(("<", "[")):
            out.append(part, style="italic grey62")
        elif part:
            out.append(part, style=f"bold {color}")
    return out

def settings_line(s):
    """'vacuum · suction balanced · water off · standard route'"""
    return " · ".join((human(s.current_cleaning_mode_name), f"suction {human(s.fan_speed_name)}",
                       f"water {human(s.water_mode_name)}", f"{human(s.mop_route_name)} route"))

def selected_labels(repl):
    """['Bedroom (16)', ...] for the selected segments."""
    names = {r.segment_id: r.name or "?" for r in repl.current_rooms()}
    return [f"{names.get(seg, '?')} ({seg})" for seg in repl.selected]

def floor_name(repl, flag):
    if info := repl.floors().get(flag):
        return info.name
    return next((m.name for m in (repl.p.maps.map_info or []) if m.map_flag == flag), None)

def task_summary(s):
    """(text, running): ('room clean running', True) / ('room clean paused, not finished', False), or None."""
    task = s.in_cleaning
    if task is None or getattr(task, "value", task) == 0:
        return None
    kind = TASK_KINDS.get(getattr(task, "name", ""), human(task))
    if state_kind(s.state_name) == "working":
        return f"{kind} running", True
    return f"{kind} paused, not finished", False

def task_row(s, hint=True):
    """Unfinished cleaning task (in_cleaning), or None. The robot keeps it after a pause even when its
    state later drops to idle / charger disconnected, which is how the app still offers 'resume'."""
    if not (task := task_summary(s)):
        return None
    text, running = task
    if running:
        return Text(f"▶ {text}", style="bold green")
    return Text.assemble((f"‖ {text}", "bold yellow"), ("\nresume continues · dock ends it" if hint else "", "grey50"))

def finish_text(record):
    # enum names carry variant suffixes (manual_interrupt_10); drop them for display
    reason = human(re.sub(r"_\d+$", "", record.finish_reason.name)) if record.finish_reason is not None else ""
    if record.complete == 1:
        return Text(f"✔ {reason or 'complete'}", style="green")
    return Text(f"✘ {reason or 'incomplete'}", style="yellow")

def consumable_wear(c, field):
    """(used, left, remaining bar) as display values, or None when the robot reports no data for this part."""
    if field not in CONSUMABLE_INFO:
        return None
    _, lifetime, unit = CONSUMABLE_INFO[field]
    used = getattr(c, field, None)
    if used is None or (unit == "×" and used == 0):  # count-based 0 = unused or not fitted
        return None
    left = lifetime - used
    pct = left / lifetime * 100
    color = level_color(pct, 30, 10)
    fmt = (lambda v: f"{v / 3600:.0f} h") if unit == "h" else (lambda v: f"{v}×")
    return (fmt(used), Text(fmt(left), style=color) if left > 0 else Text("due!", style="bold red"),
            bar(pct, color, 20) + Text(f" {max(pct, 0):3.0f}%"))

def wear_table(*lead):
    """Table with the given leading (header, column options) columns plus Used / Left / Remaining."""
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for header, options in lead:
        t.add_column(header, **options)
    t.add_column("Used", justify="right")
    t.add_column("Left", justify="right")
    t.add_column("Remaining")
    return t

def history_totals(cs, suffix=""):
    """'347h 07m cleaned  ·  11,220 m²  ·  1401 runs' (+ dust collections when the dock empties the bin)."""
    totals = Text.assemble(
        (duration(cs.clean_time), "bold"), " cleaned  ·  ",
        (area(cs.square_meter_clean_area, ",.0f"), "bold"), "  ·  ", (f"{cs.clean_count or 0} runs", "bold"), suffix)
    if cs.dust_collection_count:
        totals.append(f"  ·  {cs.dust_collection_count} dust collections")
    return totals

def dnd_value(dnd):
    window = dnd_window(dnd)
    return on_off(dnd.is_on) + Text(f"  {window}" if window else "", style="grey50")

def volume_value(sound_volume):
    volume = sound_volume.volume or 0
    return bar(volume, "cyan") + Text(f" {volume}%")

def option_rows(p):
    s = p.status
    return {
        "fan": (s.fan_speed_mapping.values(), s.fan_speed_name),
        "water": (s.water_mode_mapping.values(), s.water_mode_name),
        "route": (s.mop_route_mapping.values(), s.mop_route_name),
        "mode": ([m.value for m in s.cleaning_mode_options], s.current_cleaning_mode_name),
    }

def option_line(options, current):
    line = Text()
    for i, option in enumerate(options):
        if i:
            line.append(" · ", style="grey50")
        line.append(option, style="bold black on green" if option == current else "")
    return line

# ---------- panel builders (return a renderable, print nothing) ----------

def header_panel(repl, local_key):
    d, prod = repl.device.device_info, repl.device.product
    t = kv_table()
    t.add_row("Model", txt(f"{prod.name} ({prod.model})"))
    t.add_row("Firmware", txt(d.fv))
    t.add_row("Serial", txt(d.sn))
    t.add_row("Time zone", txt(d.time_zone_id))
    t.add_row("Connection", online_text(d.online) + Text(f"  via {connection_via(repl.device)}"))
    if row := task_row(repl.p.status, hint=False):  # one line here; the Status panel below adds the hint
        t.add_row("Task", row)
    t.add_row("Local key", txt(local_key, "grey50"))
    return Panel(t, title=Text(f" {d.name} ", style="bold black on cyan"), title_align="left",
                 box=box.HEAVY, border_style="cyan")

def status_panel(p, extra=()):
    s = p.status
    style, icon = state_style(s.state_name)
    battery = s.battery or 0
    error = getattr(s.error_code, "value", s.error_code)
    t = kv_table()
    t.add_row("State", Text(f"{icon} {human(s.state_name)}", style=style))
    t.add_row("Battery", bar(battery, level_color(battery, 50, 20)) + Text(f" {battery}%"))
    t.add_row("Error", Text("none", style="green") if error in (0, None)
              else Text(f"{human(s.error_code_name)} ({error})", style="bold red"))
    if row := task_row(s):
        t.add_row("Task", row)
    if s.dock_error_status:  # latched until resolved, even after the cause is fixed
        t.add_row("Dock error", Text(f"{human(s.dock_error_status)} ({int(s.dock_error_status)})", style="bold red"))
    t.add_row("Mode", txt(human(s.current_cleaning_mode_name)))
    t.add_row("Suction", txt(human(s.fan_speed_name)))
    t.add_row("Water", txt(human(s.water_mode_name)))
    t.add_row("Mop route", txt(human(s.mop_route_name)))
    t.add_row("Current/last run", txt(f"{area(s.square_meter_clean_area)} in {duration(s.clean_time)}"))
    for row in extra:
        t.add_row(*row)
    return panel(t, "Status", expand=False, color="green")

def settings_panel(p):
    t = kv_table()
    t.add_row("Do not disturb", dnd_value(p.dnd))
    t.add_row("Volume", volume_value(p.sound_volume))
    for name, label in SWITCHES:
        if (trait := getattr(p, name)) is not None:
            t.add_row(label, on_off(trait.is_on))
    for name, label in MODES:
        if (trait := getattr(p, name)) is not None:
            mode = getattr(trait, "mode", None)
            value = human(mode) if mode is not None else ", ".join(f"{k}={v}" for k, v in trait.as_dict().items())
            t.add_row(label, txt(value or "n/a"))
    return panel(t, "Settings", expand=False, color="magenta")

def consumables_panel(p):
    t = wear_table(("Part", {"style": "bold"}))
    for field, label, _, _ in CONSUMABLES:
        if wear := consumable_wear(p.consumables, field):
            t.add_row(label, *wear)
    return panel(t, "Consumables", color="yellow")

def history_panel(p, records):
    totals = history_totals(p.clean_summary)
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    t.add_column("Started")
    t.add_column("Duration", justify="right")
    t.add_column("Area", justify="right")
    t.add_column("Result")
    for r in records:
        t.add_row(started(r), duration(r.duration), area(r.square_meter_area), finish_text(r))
    body = Group(totals, t) if records else totals
    return panel(body, "Cleaning history", color="blue")

def activity_strip(records, max_days=28):
    """One block per day: height = cleaning minutes that day (only the runs fetched)."""
    minutes = {}
    for r in records:
        if r.begin:
            day = datetime.date.fromtimestamp(r.begin)
            minutes[day] = minutes.get(day, 0) + (r.duration or 0) / 60
    if not minutes:
        return Text()
    today = datetime.date.today()
    start = max(min(minutes), today - datetime.timedelta(days=max_days - 1))
    peak = max((m for day, m in minutes.items() if day >= start), default=0) or 1  # only days actually drawn
    bars, letters = Text("  "), Text("  ")
    day = start
    while day <= today:
        m = minutes.get(day, 0)
        level = "▁▂▃▄▅▆▇█"[min(7, max(0, math.ceil(m / peak * 8) - 1))]
        bars.append(f"{level} " if m else "· ", style="green" if m else "grey35")
        letters.append(day.strftime("%a")[0] + " ", style="bold" if day == today else "grey50")
        day += datetime.timedelta(days=1)
    caption = Text(f"  {start:%d.%m.} – today  ·  tallest bar = {peak:.0f} min", style="grey50 italic")
    return Group(bars, letters, caption)

def history_timeline(records):
    today = datetime.date.today()
    longest = max((r.duration or 0 for r in records), default=0) or 1
    t = Table.grid(padding=(0, 1))
    for justify in ("left", "left", "left", "right", "right", "left", "left"):
        t.add_column(justify=justify, no_wrap=True)
    day = None
    for i, r in enumerate(records):  # newest first
        begin = datetime.datetime.fromtimestamp(r.begin) if r.begin else None
        end = datetime.datetime.fromtimestamp(r.end) if r.end else None
        if begin and begin.date() != day:
            day = begin.date()
            label = "today" if day == today else "yesterday" if day == today - datetime.timedelta(days=1) \
                else day.strftime("%a %d.%m.")
            t.add_row(Text("◉", style="bold cyan"), Text(label, style="bold cyan"), "", "", "", "", "")
        color = "green" if r.complete == 1 else "yellow"
        rail = "└─" if i == len(records) - 1 else "├─"
        span = f"{begin:%H:%M}–{end:%H:%M}" if begin and end else f"{begin:%H:%M}" if begin else "n/a"
        width = max(1, round((r.duration or 0) / longest * 12))
        how = " · ".join(x for x in (
            CLEAN_TYPES.get(getattr(r.clean_type, "name", ""), human(r.clean_type) if r.clean_type else ""),
            human(r.start_type) if r.start_type else "") if x)
        t.add_row(Text.assemble((rail, "grey50"), ("●", color)), span,
                  Text.assemble(("█" * width, color), (" " * (12 - width), "")),
                  duration(r.duration), area(r.square_meter_area), Text(how, style="grey62"), finish_text(r))
    return t

def floors_panel(repl):
    p = repl.p
    current = p.status.current_map
    floors = repl.floors()
    note = None
    if floors:
        items = [(flag, info.name, info.rooms) for flag, info in floors.items()]
    else:  # home discovery failed: only the active floor's rooms are known
        names = {m.map_flag: m.name for m in (p.maps.map_info or [])} or {current: None}
        items = [(flag, name, repl.current_rooms() if flag == current else None) for flag, name in names.items()]
        if len(items) > 1:
            note = "Other floors' rooms unknown: dock the robot and restart."
    tree = Tree("floors", hide_root=True, guide_style="grey50")
    for flag, name, rooms in items:
        active = flag == current
        branch = tree.add(Text.assemble(("★ " if active else "  ", "yellow"),
                                        (name or "(unnamed)", "bold" if active else ""),
                                        (f"  floor {flag}", "grey50")))
        if rooms is None:
            branch.add(Text("rooms unknown", style="grey50 italic"))
        for r in rooms or []:
            picked = active and r.segment_id in repl.selected
            branch.add(Text.assemble((r.name or "?", "bold green" if picked else ""),
                                     (f"  seg {r.segment_id}", "grey50"), ("  ✔ selected" if picked else "", "green")))
    body = Group(tree, Text(note, style="grey50 italic")) if note else tree
    return panel(body, "Floors & rooms", expand=False, color="green")

def rooms_panel(repl):
    current = repl.p.status.current_map
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    t.add_column("#", justify="right", style="grey50")
    t.add_column("Room")
    t.add_column("Segment", justify="right", style="grey50")
    t.add_column("")
    for i, r in enumerate(repl.current_rooms(), 1):
        picked = r.segment_id in repl.selected
        t.add_row(str(i), Text(r.name or "?", style="bold green" if picked else ""), str(r.segment_id),
                  Text("✔ selected", style="green") if picked else "")
    hint = Text(f"passes {repl.passes}  ·  select <name|#|segment>  ·  clear", style="grey50") if repl.selected \
        else Text("nothing selected: 'clean' = whole floor", style="yellow")
    return panel(Group(t, hint), f"Rooms on {floor_name(repl, current) or 'active floor'}", expand=False,
                 color="green")

def network_panel(p):
    n = p.network_info
    t = kv_table()
    t.add_row("Wi-Fi", txt(n.ssid))
    t.add_row("IP", txt(n.ip))
    t.add_row("MAC", txt(n.mac))
    if n.rssi:
        level = 4 if n.rssi >= -55 else 3 if n.rssi >= -67 else 2 if n.rssi >= -75 else 1
        signal = Text.assemble(("▂▄▆█"[:level], level_color(level * 25, 75, 50)), ("▂▄▆█"[level:], "grey35"))
        t.add_row("Signal", signal + Text(f" {n.rssi} dBm"))
    else:
        t.add_row("Signal", txt("n/a", "grey50"))
    return panel(t, "Network", expand=False, color="cyan")

def routines_panel(routines):
    if routines is None:
        body = txt("could not be loaded", "grey50")
    elif not routines:
        body = txt("none", "grey50")
    else:
        body = Group(*(Text.assemble((f"{r.id:>4}  ", "grey50"), r.name or "?") for r in routines))
    return panel(body, "Routines  (routine <id>)", expand=False, color="magenta")

def options_panel(p):
    t = kv_table()
    for label, (options, current) in option_rows(p).items():
        t.add_row(label, option_line(options, current))
    return panel(t, "Settable options  (fan|water|route|mode <value>, current highlighted)", color="grey50")

def footer_line(repl):
    feats = repl.p.device_features
    supported = sum(1 for f in dataclasses.fields(feats) if getattr(feats, f.name) is True)
    schema = len(repl.device.product.schema or [])
    return Text(f"{supported} supported features, {schema} product data points: see 'info raw'",
                style="grey50 italic")

def plan_table(repl):
    t = kv_table()
    t.add_row("Floor", txt(floor_name(repl, repl.p.status.current_map) or "active floor"))
    return t

def where_table(repl, map_data):
    p = repl.p
    t = kv_table()
    t.add_row("Floor", txt(floor_name(repl, p.status.current_map) or map_data.map_name))
    room = map_data.vacuum_room_name or next(
        (r.name for r in repl.current_rooms() if r.segment_id == map_data.vacuum_room), None)
    t.add_row("Room", txt(room or "between rooms / unknown", "bold" if room else "grey50"))
    robot, dock = map_data.vacuum_position, map_data.charger
    t.add_row("Position", txt(xy_mm(robot.x, robot.y) if robot else None))
    t.add_row("Dock", txt(xy_mm(dock.x, dock.y) if dock else None))
    if robot and dock:
        meters = distance_m(robot.x, robot.y, dock.x, dock.y)
        t.add_row("Distance", Text(f"{meters:.1f} m from the dock", style="green" if meters < 0.3 else ""))
    return t

def settings_summary(p):
    return Text(settings_line(p.status), style="grey62")

# ---------- map preview ----------

def image_point(map_data, point):
    """Robot coordinates (mm) -> pixel position in the rendered map image."""
    dims = map_data.image.dimensions
    return point.to_img(dims).rotated(dims)

def zone_pixels(map_data, zone):
    """Zone (x1, y1, x2, y2 in mm) -> (left, top, right, bottom) in image pixels."""
    a, b = image_point(map_data, Point(zone[0], zone[1])), image_point(map_data, Point(zone[2], zone[3]))
    return min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)

def map_preview(map_content, max_cols, max_rows, zone=None, target=None):
    """Map image as half-block characters (2 pixels per cell), cropped to the mapped area,
    with ● robot and ◆ dock drawn on top (the image's own icons vanish when scaled down).
    An optional zone (mm) is drawn as a translucent white rectangle, an optional target (mm) as ✚."""
    image = Image.open(io.BytesIO(map_content.image_content)).convert("RGB")
    background = image.getpixel((0, 0))
    left, top, right, bottom = ImageChops.difference(image, Image.new("RGB", image.size, background)).getbbox() \
        or (0, 0, *image.size)
    map_data = map_content.map_data
    has_image = map_data.image is not None
    zone_px = zone_pixels(map_data, zone) if zone and has_image else None
    target_pt = Point(*target) if target else None
    boxes = [zone_px] if zone_px else []  # keep zone and target visible even outside the mapped area
    if target_pt and has_image:
        pos = image_point(map_data, target_pt)
        boxes.append((pos.x, pos.y, pos.x, pos.y))
    for box_left, box_top, box_right, box_bottom in boxes:
        left, top = min(left, int(box_left)), min(top, int(box_top))
        right, bottom = max(right, int(box_right) + 1), max(bottom, int(box_bottom) + 1)
    pad = 8
    left, top = max(left - pad, 0), max(top - pad, 0)
    right, bottom = min(right + pad, image.width), min(bottom + pad, image.height)
    image = image.crop((left, top, right, bottom))
    scale = min(max_cols / image.width, max_rows * 2 / image.height, 1)
    if zone_px:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(
            (zone_px[0] - left, zone_px[1] - top, zone_px[2] - left, zone_px[3] - top),
            fill=(255, 255, 255, 110), outline=(255, 255, 255, 255), width=max(2, round(1.5 / scale)))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    cols, rows2 = max(1, round(image.width * scale)), max(2, round(image.height * scale / 2) * 2)
    small = image.resize((cols, rows2), Image.Resampling.BOX)
    px = small.load()

    markers = {}  # drawn in this order, so the robot wins over the dock and the target over both
    for point, mark in ((map_data.charger, "dock"), (map_data.vacuum_position, "robot"), (target_pt, "target")):
        if point is not None and has_image:
            pos = image_point(map_data, point)
            col = int((pos.x - left) * cols / image.width)
            row = int((pos.y - top) * rows2 / image.height) // 2
            if 0 <= col < cols and 0 <= row < rows2 // 2:
                markers[(col, row)] = MAP_MARKS[mark]

    out = Text()
    for row in range(rows2 // 2):
        for col in range(cols):
            upper, lower = px[col, row * 2], px[col, row * 2 + 1]
            if (col, row) in markers:
                char, style = markers[(col, row)]
                mid = Color.from_rgb(*((a + b) // 2 for a, b in zip(upper, lower)))
                base = Style.parse(style)  # markers without their own background blend into the map
                out.append(char, style=base if base.bgcolor else base + Style(bgcolor=mid))
            else:
                out.append("▀", style=Style(color=Color.from_rgb(*upper), bgcolor=Color.from_rgb(*lower)))
        out.append("\n")
    shown = ["robot", "dock"] + ["zone"] * bool(zone_px) + ["target"] * bool(target_pt)
    out.append_text(Text("   ").join(Text.assemble(MAP_MARKS[mark], f" {mark}") for mark in shown))
    return out

def preview_rows(mc, max_rows=16, max_cols=None, **marks):
    """Mini map rows for a panel (blank line + preview), guarded; empty without image or map data."""
    if not mc.image_content or mc.map_data is None:
        return []
    cols = max_cols or min(console.width - 4, 60)
    return [Text(), safe("Map", lambda: map_preview(mc, cols, max_rows, **marks))]

# ---------- output: views (render_* print) ----------

def render(repl, local_key, records):
    """The `info` overview; records = the newest CleanRecords, loaded by the REPL."""
    p = repl.p
    routines = repl.routines  # loaded by refresh_all, None if that failed
    console.print(
        safe("Device", lambda: header_panel(repl, local_key)),
        Columns([safe("Status", lambda: status_panel(p), False),
                 safe("Settings", lambda: settings_panel(p), False)]),
        safe("Consumables", lambda: consumables_panel(p)),
        safe("Cleaning history", lambda: history_panel(p, records)),
        Columns([safe("Floors & rooms", lambda: floors_panel(repl), False),
                 safe("Network", lambda: network_panel(p), False),
                 safe("Routines", lambda: routines_panel(routines), False)]),
        safe("Settable options", lambda: options_panel(p)),
        safe("Footer", lambda: footer_line(repl)),
    )

def render_status(repl):
    def build():  # inside safe(): a broken room list must not hide the status
        selected = Text(", ".join(selected_labels(repl)), style="bold") \
            if repl.selected else Text("none: 'clean' = whole floor", style="yellow")
        return status_panel(repl.p, [("", ""), ("Selected rooms", selected), ("Passes", txt(repl.passes))])
    console.print(safe("Status", build, False))

def render_rooms(repl):
    console.print(safe("Rooms", lambda: rooms_panel(repl), False))

def render_floors(repl):
    console.print(safe("Floors & rooms", lambda: floors_panel(repl), False))

def render_options(p):
    console.print(safe("Settable options", lambda: options_panel(p)))

def render_option(p, label):
    options, current = option_rows(p)[label]
    kv_line(label, option_line(options, current))

def render_routines(routines):
    console.print(safe("Routines", lambda: routines_panel(routines), False))

def render_consumables(p):
    console.print(safe("Consumables", lambda: consumables_panel(p)))

def render_history(repl, records):
    """records: the newest CleanRecords, loaded by the REPL."""
    totals = history_totals(repl.p.clean_summary, " in total")
    if not records:
        return console.print(panel(totals, "Cleaning history", color="blue"))
    def build():  # inside safe(): one odd record must not hide the totals
        done = sum(1 for r in records if r.complete == 1)
        shown = Text(f"Last {len(records)} runs: {duration(sum(r.duration or 0 for r in records))}, "
                     f"{area(sum(r.square_meter_area or 0 for r in records), '.1f')}, {done}/{len(records)} complete",
                     style="grey62")
        body = Group(totals, shown, Text(), activity_strip(records), Text(), history_timeline(records))
        return panel(body, "Cleaning history", expand=False, color="blue")
    console.print(safe("Cleaning history", build))

def render_where(repl):
    mc = repl.p.map_content
    if mc.map_data is None:
        return console.print(txt("No map data available.", "yellow"))
    body = [where_table(repl, mc.map_data), Text("coordinates work with goto x y / zone", style="grey50 italic")]
    console.print(panel(Group(*body, *preview_rows(mc, max_rows=18)), "Where is the robot", expand=False, color="cyan"))

def render_map(mc, path):
    """path is None when the robot returned no image (then nothing was written)."""
    if path is None:
        return console.print(panel(txt("The robot returned no map image: nothing written.", "yellow"), "Map",
                                   expand=False, color="yellow"))
    info = Text.assemble("Saved ", (str(path), "bold"), (f"  ({path.stat().st_size / 1024:.0f} KB)", "grey50"))
    rows = preview_rows(mc, max_rows=max(console.height - 8, 20), max_cols=console.width - 4)
    console.print(panel(Group(info, *rows), "Map", expand=False, color="cyan"))

def render_clean_plan(repl):
    """Shown right before the clean command is sent."""
    t = plan_table(repl)
    if repl.selected:
        t.add_row("Rooms", Text(", ".join(selected_labels(repl)), style="bold"))
        t.add_row("Passes", txt(repl.passes))
        title = f"▶ Cleaning {len(repl.selected)} room{'s' if len(repl.selected) > 1 else ''}"
    else:
        t.add_row("Rooms", Text("none selected → whole floor", style="bold yellow"))
        title = "▶ Cleaning the whole floor"
    t.add_row("Settings", settings_summary(repl.p))
    console.print(panel(t, title, expand=False, color="green"))

def render_zone_plan(repl, zone):
    """Shown right before the zone command is sent; includes a mini map with the zone when available."""
    x1, y1, x2, y2 = zone
    width_m, height_m = abs(x2 - x1) / 1000, abs(y2 - y1) / 1000
    t = plan_table(repl)
    t.add_row("Zone", txt(f"x {min(x1, x2)}–{max(x1, x2)}  y {min(y1, y2)}–{max(y1, y2)} mm"))
    t.add_row("Size", Text(f"{width_m:.1f} × {height_m:.1f} m = {width_m * height_m:.1f} m²", style="bold"))
    t.add_row("Passes", txt(repl.passes))
    t.add_row("Settings", settings_summary(repl.p))
    console.print(panel(Group(t, *preview_rows(repl.p.map_content, zone=zone)), "▶ Zone cleaning",
                        expand=False, color="green"))

def render_goto_plan(repl, target):
    """Shown right before the goto command is sent: target, distances and a mini map."""
    x, y = target
    t = plan_table(repl)
    t.add_row("Target", txt(xy_mm(x, y)))
    mc = repl.p.map_content
    map_data = mc.map_data
    for label, point in (("From robot", map_data and map_data.vacuum_position), ("From dock", map_data and map_data.charger)):
        if point:
            t.add_row(label, txt(f"{distance_m(x, y, point.x, point.y):.1f} m"))
    console.print(panel(Group(t, *preview_rows(mc, target=target)), "▶ Go to", expand=False, color="green"))

# ---------- output: one-line results of commands ----------

def say(mark, *parts):
    """One status line: ✔ ok / ✘ no (refused, nothing done) / ✘ error / ⚠ warn, then the text parts."""
    console.print(Text.assemble(MARKS[mark], *parts))

def kv_line(label, *value):
    console.print(Text.assemble((f"{label}  ", "bold"), *value))

def render_dnd(p):
    kv_line("Do not disturb", dnd_value(p.dnd))

def render_switch(label, trait):
    kv_line(label, on_off(trait.is_on))

def render_volume(p):
    kv_line("Volume", volume_value(p.sound_volume))

def render_passes(passes):
    kv_line("Passes", ("●" * passes, "bold green"), ("○" * (3 - passes), "grey35"), f" {passes}")

def render_cleared(previous):
    if previous:
        say("ok", "Selection cleared", (f"  (was: {', '.join(previous)})", "grey50"))
    else:
        say("ok", "Nothing was selected")
    render_hint("'clean' now cleans the whole floor; 'select' picks rooms again.")

def render_select(notes):
    """One line per typed room: added / already selected / ambiguous / unknown."""
    for outcome, token, detail in notes:
        if outcome == "added":
            line = Text.assemble(MARKS["ok"], "Added ", (detail.name or "?", "bold"), (f" ({detail.segment_id})", "grey50"))
        elif outcome == "already":
            line = Text.assemble(("· ", "grey50"), (detail.name or "?", "bold"), (f" ({detail.segment_id})", "grey50"),
                                 (" was already selected", "grey50"))
        elif outcome == "ambiguous":
            line = Text.assemble(MARKS["no"], f"'{token}' matches ", (", ".join(detail), "bold"),
                                 (": type more of the name or use the number", "yellow"))
        else:
            line = Text.assemble(MARKS["no"], f"No room '{token}' on this floor", ("  (see the list below)", "grey50"))
        console.print(line)

def render_reset_options(c, names, problem=None):
    """Resettable parts with their current wear; problem = why the given name didn't match."""
    t = wear_table(("reset <name>", {"style": "bold cyan", "no_wrap": True}), ("Part", {"style": "bold"}))
    for name in names:
        label = CONSUMABLE_INFO.get(name, (human(name),))[0]
        t.add_row(name, label, *(consumable_wear(c, name) or ("", Text("no data", style="grey50"), "")))
    body = [t, Text("A unique part of the name is enough, e.g. 'reset main'. Do this after replacing the part.",
                    style="grey50 italic")]
    if problem:
        body.insert(0, Text(problem, style="yellow"))
    console.print(panel(Group(*body), "Reset a consumable", expand=False, color="yellow"))

def render_reset(c, name):
    """Shown right before a wear counter is reset."""
    label = CONSUMABLE_INFO.get(name, (human(name),))[0]
    wear = consumable_wear(c, name)
    console.print(Text.assemble(("↺ ", "bold yellow"), "Resetting ", (label, "bold"), (f" ({name})", "grey50"),
                                f": {wear[0]} used → 0" if wear else ""))

def render_sent(command, result, label=None):
    """One line after an action command: did the robot accept it? Optionally prefixed by an action label."""
    command = getattr(command, "value", command)  # app_start, not RoborockCommand.APP_START
    out = Text.assemble((f"{label}   ", "bold")) if label else Text()
    if result in ("ok", ["ok"]):
        out.append_text(Text.assemble(MARKS["ok"], "Robot accepted ", (str(command), "bold")))
    else:
        out.append_text(Text.assemble(("? ", "bold yellow"), "Robot answered ", (str(command), "bold"),
                                      " with ", (repr(result), "yellow")))
    console.print(out)

def render_start_check(outcome, s, waited):
    """After a start command the robot accepted: did it really start? outcome from Repl.check_started."""
    state = Text(human(s.state_name), style=state_style(s.state_name)[0])
    error = s.dock_error_status or s.error_code  # dock errors first, like 'resolve'
    error_text = Text.assemble((human(error), "bold red"), (f" ({int(error)})", "grey50")) if error else None
    if outcome == "started":
        say("ok", "Robot is running: ", state, (f"  ({waited} s)", "grey50"))
    elif outcome == "error":
        say("error", ("Robot did not start: ", "red"), error_text or state)
        render_hint("Fix the cause, then 'resolve' clears the error and you can retry.")
    elif outcome == "timeout":
        say("warn", (f"No start seen after {waited} s: ", "yellow"), "robot is ", state,
            *((", error ", error_text) if error_text else ()))
        render_hint("It may still start: 'status' shows the current state.")
    else:
        say("warn", ("Stopped waiting", "yellow"), ": robot is ", state, (" (the robot itself was not affected)", "grey50"))

def render_resolve(status):
    """Say which error gets resolved (dock errors first, like the library). Returns False if there is none."""
    if status.dock_error_status:
        kind, error = "dock error", status.dock_error_status
    elif status.error_code:
        kind, error = "robot error", status.error_code
    else:
        say("ok", "No error to resolve.")
        return False
    console.print(Text.assemble(("⚑ ", "bold yellow"), f"Resolving {kind} ",
                                (human(error), "bold yellow"), (f" ({int(error)})", "grey50")))
    return True

def render_floor_switch(name, flag, already=False):
    label = Text.assemble((name or "(unnamed)", "bold"), (f"  floor {flag}", "grey50"))
    if already:
        console.print(Text.assemble(("★ ", "yellow"), "Already on ", label, ": nothing sent."))
    else:
        console.print(Text.assemble(("⇄ ", "bold cyan"), "Switching to ", label, ("  (clears the room selection)", "grey50")))

def render_routine_start(routine):
    console.print(Text.assemble(("▶ ", "bold green"), "Routine ", (routine.name or "?", "bold"),
                                (f"  (id {routine.id})", "grey50")))

def render_raw_request(command, params, known):
    color, label = command_kind(command)
    line = Text.assemble(("→ ", "bold"), (command, f"bold {color}".strip()), (f"  {label}", "grey50"))
    if params is not None:
        line.append_text(Text.assemble(("  params ", "grey50"), json.dumps(params, ensure_ascii=False)))
    console.print(line)
    if not known:
        render_hint("Not in the known command list ('commands'): sent anyway.")

def render_raw_result(command, result):
    if result in ("ok", ["ok"]):
        return render_sent(command, result)
    console.print(panel(JSON.from_data(result, default=str), f"{command} → answer", expand=False, color="cyan"))

# ---------- output: help ----------

def render_help(sections, footer_text):
    t = Table.grid(padding=(0, 2))
    t.add_column(no_wrap=True)
    t.add_column()
    for i, (title, color, rows) in enumerate(sections):
        if i:
            t.add_row("", "")
        t.add_row(Text(f"▌{title}", style=f"bold {color}"), "")
        for cmd, desc in rows:
            t.add_row(Text.assemble(" ", command_text(cmd, color)), Text(desc))
    console.print(panel(Group(t, Text(), Text(footer_text, style="grey50 italic")), "Commands", color="cyan"))

def render_examples(text):
    """Style the plain example text: headings, command lines, comments, notes and warnings."""
    out = Text()
    for line in text.strip("\n").splitlines():
        if re.match(r"\d+\. ", line):
            out.append(line + "\n", style="bold cyan")
        elif m := re.match(r"( {5})(\S.*?)(\s{2,}.*)?$", line):  # "     cmd   comment"
            out.append(m[1])
            out.append(m[2], style="bold green")
            out.append((m[3] or "") + "\n", style="grey62")
        elif line.startswith(" " * 10):  # wrapped comment
            out.append(line + "\n", style="grey62")
        elif "Careful" in line:
            out.append(line + "\n", style="yellow")
        else:
            out.append(line + "\n", style="italic")
    console.print(panel(out, "Examples", color="cyan"))

def render_commands(names, query):
    items = [Text(n, style=command_kind(n)[0]) for n in names]
    legend = Text("  ·  ").join([*(Text(f"{prefix}* {label}", style=color) for prefix, color, label in COMMAND_KINDS),
                                 Text("other")])
    body = Group(Columns(items, padding=(0, 2), column_first=True), Text(), legend) if items \
        else Text("no command matches", style="grey50")
    title = f"{len(names)} commands" + (f" matching '{query}'" if query else "") + "  (send with raw <cmd> [json])"
    console.print(panel(body, title, color="red"))

# ---------- output: startup and shutdown ----------

def render_banner():
    console.rule(Text("Roborock REPL", style="bold cyan"), style="cyan")

def render_logged_in(email, saved, encrypted=False):
    how = "saved session, no new code needed" if saved else "session saved, next start needs no code"
    if encrypted:
        how += "; passphrase protected"
    say("ok", "Logged in as ", (email, "bold"), (f"  ({how})", "grey50"))

def render_code_sent(email):
    body = Text.assemble("A login code was sent to ", (email, "bold"), ".\n",
                         ("Enter it below (check spam if it doesn't arrive). Codes expire after a few minutes.",
                          "grey62"))
    console.print(panel(body, "Login", expand=False, color="cyan"))

def render_devices(vacuums):
    if len(vacuums) == 1:
        d = vacuums[0]
        return say("ok", "Found ", (d.name, "bold"), (f"  ({d.product.name}, {d.product.model})", "grey50"))
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for column in ("#", "Name", "Model", "Online"):
        t.add_column(column)
    for i, d in enumerate(vacuums, 1):
        t.add_row(str(i), Text(d.name, style="bold"), txt(f"{d.product.name} ({d.product.model})"),
                  online_text(d.device_info.online))
    console.print(panel(t, f"{len(vacuums)} robots on this account", expand=False, color="cyan"))

def render_connected(device):
    """Connection status line (startup and cls): reflects the connection at this moment."""
    if not device.is_connected:
        return say("error", "Not connected to ", (device.name, "bold"),
                   ("  right now: the library keeps reconnecting in the background", "grey50"))
    via = "local network" if device.is_local_connected else "Roborock cloud (MQTT)"
    say("ok", "Connected to ", (device.name, "bold"), (f"  via {via}", "grey50"))

def render_refresh_problems(problems):
    for name, e in problems:
        if name == "home":
            say("warn", ("Floor list unavailable", "yellow"), (f": {e}", "grey50"))
            render_hint("Only the active floor's rooms are known. Dock the robot and restart to load all floors.")
        else:
            say("warn", (f"Could not read {human(name)}", "yellow"), (f": {e}", "grey50"))

def render_goodbye(device_name):
    say("ok", "Disconnected from ", (device_name, "bold"), ("  (cache saved)", "grey50"), "  Bye!")

def render_interrupted():
    console.print()  # the terminal's ^C leaves the cursor on the prompt line
    console.print(Text.assemble(("■ ", "bold yellow"), ("Interrupted", "bold yellow"), ": shut down cleanly",
                                ("  (connection closed)", "grey50")))

# ---------- output: errors, warnings and hints ----------

def describe_error(error):
    """(message, hint) in plain words for the errors a user typically runs into."""
    text = str(error)
    if isinstance(error, json.JSONDecodeError):
        return f"Invalid JSON parameters: {error.msg} (column {error.colno})", "Example: raw set_custom_mode [102]"
    if isinstance(error, ValueError):
        if "quotation" in text:
            return "Unbalanced quotes in that line", "Close the quote, e.g. select \"Living room\""
        if m := re.match(r"invalid literal for int\(\) with base 10: (.*)", text):
            return f"Expected a whole number, got {m[1]}", None
        return f"Invalid input: {text}", None
    if isinstance(error, RoborockTimeout) or (type(error) is RoborockException and "timed out" in text.lower()):
        return "The robot didn't answer in time", "Is it online and in Wi-Fi range? Try again in a moment."
    if isinstance(error, RoborockDeviceBusy):
        return f"The robot is busy: {text}", "Wait until it is idle or docked, then try again."
    if isinstance(error, UnknownMethodError):
        return "The robot doesn't know this command", "'commands' lists the known ones."
    if isinstance(error, (VacuumError, CommandVacuumError)):
        return f"The robot rejected the command: {text}", None
    if isinstance(error, RoborockConnectionException):
        return f"Connection problem: {text}", "Check the network, then try again."
    return text or type(error).__name__, None

def render_error(error):
    message, hint = describe_error(error)
    say("error", (message, "red"), (f"  ({type(error).__name__})", "grey50"))
    if hint:
        render_hint(hint)

def render_usage(syntax, example, note=None):
    say("no", ("Usage: ", "yellow"), command_text(syntax, "cyan"))
    console.print(Text.assemble(("  e.g. ", "grey50"), (example, "cyan"), (f"  ·  {note}" if note else "", "grey50")))

def render_connection_lost(reason):
    say("warn", ("Connection lost", "yellow"), f" ({reason}): reconnecting ...")

def render_reconnect_failed(error):
    say("error", (str(error), "red"))
    render_hint("Check the network and the robot's Wi-Fi. 'reconnect' tries again.")

def render_still_offline(name):
    say("error", ("Still no connection to ", "red"), (name, "bold"),
        ("  (just tried to reconnect)", "grey50"))
    render_hint("'reconnect' tries again right away.")

def render_delivery(delivery):
    """After a dropped connection: actions are never sent twice, so say what is known about this one
    (delivery: unsent / unknown / accepted, see Repl.delivery)."""
    if delivery == "unsent":
        say("warn", ("Nothing was sent to the robot", "yellow"), ": run the command again.")
    elif delivery == "accepted":
        say("warn", ("The robot accepted the command", "yellow"), "; only the follow-up read was cut off.")
        render_hint("'status' shows what it is doing now.")
    else:
        say("warn", ("The command may or may not have reached the robot", "yellow"), ": it was not sent again.")
        render_hint("Check 'status' and repeat the command if needed.")

def render_command_cancelled():
    say("no", ("Cancelled", "yellow"), ": stopped waiting, the robot may already have received the command.")

def render_warning(message):
    say("warn", (message, "yellow"))

def render_unknown_command(word, known):
    line = Text.assemble(MARKS["no"], "Unknown command ", (f"'{word}'", "bold"))
    if close := difflib.get_close_matches(word.lower(), known, n=3, cutoff=0.6):
        line.append_text(Text.assemble(": did you mean ", Text(" / ").join(Text(c, style="bold cyan") for c in close), "?"))
    console.print(line)
    render_hint("'help' lists all commands.")

def render_cancelled():
    say("no", ("Cancelled", "yellow"), f": {CANCELLED_DETAIL}")

def render_done(message):
    say("ok", message)

def render_problem(message):
    """Something refused or not found (nothing was done): ✘ plus the message."""
    say("no", (message, "yellow"))

def render_hint(hint):
    console.print(Text(f"  {hint}", style="grey50 italic"))

def render_plain(line, help_text=False):
    """Raw one-liners: exactly the old text (also when piped), just colored in a terminal."""
    highlighter = HelpHighlighter() if help_text else RawHighlighter()
    console.print(highlighter(Text(line)), soft_wrap=True, markup=False, emoji=False)

# ---------- raw dumps: JSON blocks with plain-words summaries (guarded: never break a dump) ----------

def json_block(title, data, summary=None):
    """Highlighted JSON of the unconverted data under a heading; stays plain, copyable JSON when piped."""
    heading = Text.assemble(("▌", "cyan"), (title, "bold cyan"))
    if summary:
        heading.append_text(Text("  ") + summary)
    console.print()
    console.print(heading, soft_wrap=True)  # never break a heading into the JSON that follows
    # soft_wrap: rich must never fold a long JSON line, or piped output stops being valid JSON
    console.print(JSON.from_data(data.as_dict() if hasattr(data, "as_dict") else data, default=str), soft_wrap=True)

def gist(text):
    return Text(text, style="grey50")

def guarded(build):
    """Summary helpers must never break a raw dump: on any surprise just leave the heading bare."""
    def wrapper(*args):
        try:
            return build(*args)
        except Exception:  # noqa: BLE001
            return None
    return wrapper

@guarded
def summary_device(device, show_secrets):
    d, prod = device.device_info, device.product
    return gist(f"{d.name} · {prod.name} · firmware {d.fv} · {'online' if d.online else 'offline'}"
                f" · local_key {'shown' if show_secrets else 'masked'}")

@guarded
def summary_features(feats):
    flags = [getattr(feats, f.name) for f in dataclasses.fields(feats) if isinstance(getattr(feats, f.name), bool)]
    return gist(f"{sum(flags)} of {len(flags)} feature flags are true")

@guarded
def summary_status(s):
    error = getattr(s.error_code, "value", s.error_code)
    task = f" · task: {summary[0]}" if (summary := task_summary(s)) else ""
    return gist(f"{human(s.state_name)}{task} · battery {s.battery}% · error {human(s.error_code_name) if error else 'none'}"
                f" · {human(s.fan_speed_name)} / water {human(s.water_mode_name)} / {human(s.mop_route_name)} route"
                f" · area in mm², time in s")

@guarded
def summary_network(n):
    return gist(f"{n.ssid} · {n.ip}")

@guarded
def summary_consumables(c):
    parts = [f"{label} {max(lifetime - getattr(c, field), 0) / lifetime * 100:.0f}% left"  # never below 0%
             for field, label, lifetime, unit in CONSUMABLES if unit == "h" and getattr(c, field, None)]
    return gist(" · ".join(parts) + " · work times in s")

@guarded
def summary_clean(cs):
    return gist(f"{duration(cs.clean_time)} · {area(cs.square_meter_clean_area, ',.0f')} · {cs.clean_count or 0} runs"
                f" · {len(cs.records or [])} record ids, newest first (times in s, areas in mm²)")

@guarded
def summary_dnd(dnd):
    window = dnd_window(dnd)
    return gist(f"{'on' if dnd.is_on else 'off'}{f' · {window}' if window else ''}")

@guarded
def summary_trait(trait):
    if hasattr(trait, "is_on"):
        return gist("on" if trait.is_on else "off")
    mode = getattr(trait, "mode", None)
    return gist(human(mode)) if mode is not None else None

@guarded
def summary_maps(maps, current):
    floors = ", ".join(f"{m.name or '(unnamed)'} ({m.map_flag}{', active' if m.map_flag == current else ''})"
                       for m in maps.map_info or [])
    return gist(f"{len(maps.map_info or [])} floors: {floors}")

@guarded
def summary_options(p):
    return gist(f"current: {settings_line(p.status)}")

@guarded
def summary_record(r):
    return Text.assemble((f"{started(r)} · {duration(r.duration)} · {area(r.square_meter_area)} · ", "grey50"), finish_text(r))

def render_history_raw(summary, records):  # records: [(record_id, CleanRecord)]
    """`history raw`: the robot's data as is (all fields, raw units), annotated in plain words."""
    json_block("Clean summary", summary, summary_clean(summary))
    for record_id, r in records:
        json_block(f"Record {record_id}", r, summary_record(r))
