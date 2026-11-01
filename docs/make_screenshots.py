"""Render the README screenshots (docs/*.svg) with rich, using sample data: no robot or login needed.

    .venv/bin/python docs/make_screenshots.py
"""
import pathlib
import sys
from types import SimpleNamespace as NS
DOCS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(DOCS.parent))
import info_pretty as ip, roborock_repl as r
from rich.console import Console
from rich.text import Text
from roborock.data.v1.v1_code_mappings import RoborockErrorCode as E, RoborockDockErrorCode as D, RoborockInCleaning as IC


def new_console():
    ip.console = Console(record=True, width=96, theme=ip.THEME, force_terminal=True, color_system="truecolor")
    return ip.console

def prompt(cmd):
    ip.console.print(Text.assemble(("roborock> ", ""), (cmd, "bold")))

# 1: help
c = new_console()
ip.render_banner()
prompt("help")
ip.render_help(r.HELP_SECTIONS, r.HELP_FOOTER)
c.save_svg(DOCS / "help.svg", title="roborock_repl.py")

# 2: status, clean, start check
status = NS(state_name="charging", battery=100, error_code=E.none, error_code_name="none", in_cleaning=IC.complete,
            dock_error_status=D.ok, current_cleaning_mode_name="vacuum", fan_speed_name="balanced",
            water_mode_name="off", mop_route_name="standard", square_meter_clean_area=42.5, clean_time=2760,
            current_map=0)
rooms = [NS(segment_id=16, name="Kitchen"), NS(segment_id=17, name="Living room"), NS(segment_id=18, name="Hallway")]
repl = NS(p=NS(status=status, maps=NS(map_info=[])), selected=[16, 18], passes=2,
          current_rooms=lambda: rooms, floors=lambda: {0: NS(name="Ground floor")})
c = new_console()
prompt("select Kitchen Hallway")
ip.render_select([("added", "Kitchen", rooms[0]), ("added", "Hallway", rooms[2])])
prompt("passes 2")
ip.render_passes(2)
prompt("status")
ip.render_status(repl)
prompt("clean")
ip.render_clean_plan(repl)
c.print("Start cleaning? [y/N] y", markup=False)
ip.render_sent("app_segment_clean", ["ok"])
status.state_name = "segment_cleaning"
ip.render_start_check("started", status, 4)
c.save_svg(DOCS / "clean.svg", title="roborock_repl.py")
