#!/usr/bin/env python3
"""Show the load and temperature of a CPU core on a DisplayPad key.

Uses psutil (already bundled with the BaseCamp Linux AppImage) to read
per-core CPU usage and temperature, and writes a formatted message to a
dp_pipe_text pipe every second. The load and temperature numbers each turn
green/yellow/red depending on how high they are.

Setup:
  1. In the DisplayPad action config, assign "Pipe Text" to a key with
     action value:

       /tmp/dp_core0.pipe;python3 core_load.py 0

     The plugin creates the pipe and starts this script automatically,
     appending the pipe path as this script's second argument. To run it
     manually instead:

       python3 core_load.py 0 /tmp/dp_core0.pipe

Temperature notes:
  Per-core temperature depends on what psutil.sensors_temperatures() can
  read from the kernel (the "coretemp" driver on most Intel machines,
  "k10temp"/"zenpower" on AMD). If a sensor labeled "Core <N>" is found for
  the requested core, that's used; otherwise the script falls back to a
  package/overall CPU temperature, or shows "N/A" if no sensor is
  available at all (this is normal inside VMs and containers).
"""

import json
import sys
import time

import psutil


def _color_for(value, warn, crit):
    if value < warn:
        return "#22c55e"   # green
    if value < crit:
        return "#f5c542"   # yellow
    return "#dc2626"       # red


def _get_core_temp(core):
    """Best-effort per-core temperature in Celsius, or None if unavailable."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, NotImplementedError):
        return None
    if not temps:
        return None

    target_label = f"Core {core}"
    for entries in temps.values():
        for entry in entries:
            if entry.label == target_label:
                return entry.current

    # Fall back to a package/overall reading shared by all cores.
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if key in temps and temps[key]:
            return temps[key][0].current

    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <core-number> <pipe-path>")
        sys.exit(1)

    try:
        core = int(sys.argv[1])
    except ValueError:
        print(f"Core number must be an integer, got: {sys.argv[1]!r}")
        sys.exit(1)

    pipe_path = sys.argv[2]

    core_count = psutil.cpu_count(logical=True) or 1
    if not (0 <= core < core_count):
        print(f"Core {core} does not exist (this machine has {core_count} logical cores, "
              f"valid range is 0-{core_count - 1})")
        sys.exit(1)

    # The first call only establishes a baseline; discard it.
    psutil.cpu_percent(percpu=True)

    while True:
        time.sleep(1)

        per_core = psutil.cpu_percent(interval=None, percpu=True)
        load = per_core[core]
        load_color = _color_for(load, warn=50, crit=80)

        temp = _get_core_temp(core)
        if temp is None:
            temp_text = "N/A"
            temp_color = "#707090"
        else:
            temp_text = f"{temp:.0f}\u00b0C"
            temp_color = _color_for(temp, warn=60, crit=80)

        spec = {
            "align": "center",
            "lines": [
                {"text": f"CORE {core}", "color": "#9ca3af", "size": 13, "bold": True},
                {"text": f"{load:.0f}%", "color": load_color, "size": 20},
                {"text": temp_text, "color": temp_color, "size": 14},
            ],
        }

        try:
            with open(pipe_path, "w") as f:
                f.write(json.dumps(spec))
        except FileNotFoundError:
            print(f"Pipe not found yet: {pipe_path} "
                  f"(waiting for the plugin to create it)")
        except Exception as e:
            print(f"Write failed: {e}")


if __name__ == "__main__":
    main()
