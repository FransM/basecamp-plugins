#!/usr/bin/env python3
"""Show the load of a CPU core on a DisplayPad key.

Uses psutil (already bundled with the BaseCamp Linux AppImage) to read
per-core CPU usage, and writes a formatted message to a dp_pipe_text pipe
every second. The number turns green/yellow/red depending on load.

Setup:
  1. In the DisplayPad action config, assign "Pipe Text" to a key and enter
     a pipe path, e.g. /tmp/dp_core0.pipe
  2. Run this script with the core number first, then that same pipe path:

       python3 core_load.py 0 /tmp/dp_core0.pipe

The dp_pipe_text plugin creates the pipe itself once the action is
assigned, so just make sure the plugin has had a couple of seconds to pick
up the assignment before starting the script.
"""

import sys
import time

import psutil


def _color_for_load(load):
    if load < 50:
        return "#22c55e"   # green
    if load < 80:
        return "#f5c542"   # yellow
    return "#dc2626"       # red


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
        color = _color_for_load(load)

        message = (
            f"#!color={color};size=16;align=center\n"
            f"CORE {core}\n"
            f"{load:.0f}%"
        )

        try:
            with open(pipe_path, "w") as f:
                f.write(message)
        except FileNotFoundError:
            print(f"Pipe not found yet: {pipe_path} "
                  f"(waiting for the plugin to create it)")
        except Exception as e:
            print(f"Write failed: {e}")


if __name__ == "__main__":
    main()
