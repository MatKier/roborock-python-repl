# Roborock Python REPL

Interactive command line for controlling and inspecting Roborock V1 vacuums (tested on an S7, `roborock.vacuum.a15`) through the Roborock cloud account.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./roborock_repl.py --email you@example.com
```

The first run sends a login code to your email. The session is then stored in `~/.roborock-repl/` and reused, so later runs don't request a new code. Use `--relogin` to force a new code and `--show-secrets` to print the device's `local_key` unmasked.

## Usage

Type `help` at the `roborock>` prompt for the full list. Tab completion and history are available. The main commands are:

- **State:** `info`, `status`, `where`, `map`, `consumables`, `history`
- **Cleaning:** `rooms`, `select`, `clean`, `zone`, `goto`, `spot`, `pause`, `resume`, `stop`, `dock`
- **Settings:** `fan`, `water`, `route`, `mode`, `passes`, `volume`, `dnd`, `childlock`, `led`
- **Floors and routines:** `floors`, `floor`, `routines`, `routine`
- **Low level:** `raw`, `commands`

Commands that move the robot ask for confirmation first. Add `-y` to skip the prompt.

`map` saves a PNG of your floor plan. `*.png` is ignored by git so the image isn't committed by accident.

## Acknowledgement

This script is only a thin interface on top of [python-roborock](https://github.com/Python-roborock/python-roborock), and it would not work without that library. The library does all the real work: cloud login, device discovery, the MQTT/local protocol and map parsing. The cache workaround in `roborock_repl.py` depends on python-roborock 7.x internals, so keep the version pinned in `requirements.txt`.
