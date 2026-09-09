#!/usr/bin/env python3
"""Show laptop battery percentage and status on a DisplayPad key.

Writes three lines to a dp_pipe_text pipe every few seconds:

  1. Title: "Battery"
  2. Battery percentage, colored:
     - red    if the battery is discharging AND below 15%
     - orange if below 30% (regardless of charging state)
     - green  otherwise
  3. Battery status: "Charging", "Full" or "Discharging"

Uses psutil (already bundled with the BaseCamp Linux AppImage) to read
battery info via psutil.sensors_battery().

Setup:
  In the DisplayPad action config, assign "Pipe Text" to a key with
  action value:

    /tmp/dp_battery.pipe;python3 battery_status.py

  The plugin creates the pipe and starts this script automatically,
  appending the pipe path as this script's argument. To run it manually
  instead:

    python3 battery_status.py /tmp/dp_battery.pipe
"""

import json
import sys
import time

import psutil

# Colors
COLOR_RED = "#dc2626"
COLOR_ORANGE = "#f5a623"
COLOR_GREEN = "#22c55e"
COLOR_GRAY = "#9ca3af"

UPDATE_INTERVAL_SECONDS = 5


def _percent_color(percent, is_discharging):
    """Pick a color for the percentage based on the battery level."""
    if is_discharging and percent < 15:
        return COLOR_RED
    if percent < 30:
        return COLOR_ORANGE
    return COLOR_GREEN


def _status_text(battery):
    """Translate psutil's battery flags into a short status label."""
    if battery.power_plugged:
        # power_plugged is True both while actively charging and once full
        if battery.percent >= 100:
            return "Full"
        return "Charging"
    return "Discharging"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <pipe-path>")
        sys.exit(1)

    pipe_path = sys.argv[1]

    while True:
        battery = psutil.sensors_battery()

        if battery is None:
            # No battery detected (desktop machine, VM, etc.)
            spec = {
                "align": "center",
                "lines": [
                    {"text": "Battery", "color": COLOR_GRAY, "size": 13, "bold": True},
                    {"text": "N/A", "color": COLOR_GRAY, "size": 20},
                    {"text": "No battery", "color": COLOR_GRAY, "size": 12},
                ],
            }
        else:
            percent = battery.percent
            status = _status_text(battery)
            color = _percent_color(percent, is_discharging=not battery.power_plugged)

            spec = {
                "align": "center",
                "lines": [
                    {"text": "Battery", "color": COLOR_GRAY, "size": 13, "bold": True},
                    {"text": f"{percent:.0f}%", "color": color, "size": 20, "bold": True},
                    {"text": status, "color": COLOR_GRAY, "size": 12},
                ],
            }

        try:
            with open(pipe_path, "w") as f:
                # A trailing newline makes back-to-back messages easier to
                # tell apart if they ever land close together on the wire.
                f.write(json.dumps(spec) + "\n")
        except FileNotFoundError:
            print(f"Pipe not found yet: {pipe_path} "
                  f"(waiting for the plugin to create it)")
        except Exception as e:
            print(f"Write failed: {e}")

        time.sleep(UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
