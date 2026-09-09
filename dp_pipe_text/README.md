# DisplayPad Pipe Text

BaseCamp Linux plugin: renders text from a named pipe (FIFO) onto a
DisplayPad key. Multiline, with formatting: color, font size, bold,
alignment, background color.

This is an **action** plugin (like `dp_clock`): you assign it to a key from
the DisplayPad action dropdown, rather than pointing it at a directory of
pipes.

## Installation

```
mkdir -p ~/.config/mountain-time-sync/plugins/dp_pipe_text
cp plugin.json __init__.py ~/.config/mountain-time-sync/plugins/dp_pipe_text/
```

Restart BaseCamp Linux. Requires `Pillow` (bundled with the AppImage) and
BaseCamp 2.1.7 or newer: the plugin asks the app which DisplayPad page is on
screen and what is assigned to its keys, an API older versions do not have.

## Usage

The action field takes one of two forms:

```
<pipe-path>
<pipe-path>;<shell command>
```

1. Open the DisplayPad action config and pick **"Pipe Text"** as the type
   for a key.
2. **Just a pipe path** -- the plugin only creates the FIFO and reads from
   it; you write to it yourself:

   ```
   /tmp/dp_status.pipe
   ```

   ```bash
   echo "Build OK" > /tmp/dp_status.pipe
   ```

3. **A pipe path followed by `;` and a command** -- the plugin creates the
   FIFO *and* launches the command for you, automatically appending the
   pipe path as an extra argument:

   ```
   /tmp/dp_core0.pipe;python3 core_load.py 0
   ```

   is equivalent to manually creating `/tmp/dp_core0.pipe` and running:

   ```bash
   python3 core_load.py 0 /tmp/dp_core0.pipe
   ```

   The command is started in its own process group, so it (and anything it
   spawns) is cleanly terminated if you change the action field, remove the
   action, or close the app.

Each key with a "Pipe Text" action gets its own independent reader thread
(and, if a command is given, its own producer process) for the path you
assigned to it, so multiple keys can each show something different at the
same time.

Pressing the key clears it back to a blank background.

An empty message (`echo "" > pipe` or `echo '{}' > pipe`) also clears the
key.

## Message format

### Option 1: plain text (simplest)

Each line becomes a line on the key, centered, light gray, size 14 by
default. An optional first line starting with `#!` sets the defaults for
the whole message:

```
#!color=#22c55e;size=16;align=left;bg=#101828;valign=top
Build: OK
12 tests passed
```

Directive keys: `color`, `size`, `bold` (0/1), `align`
(`left`/`center`/`right`), `valign` (`top`/`middle`/`bottom`), `bg`.

### Option 2: JSON (full per-line control)

Either a bare list of strings, or an object:

```json
{
  "bg": "#101828",
  "align": "center",
  "valign": "middle",
  "color": "#e0e0e0",
  "size": 14,
  "lines": [
    {"text": "ERROR", "color": "#dc2626", "size": 20, "bold": true},
    "disk 92% full",
    {"text": "check logs", "align": "right", "size": 10}
  ]
}
```

Top-level `color`/`size`/`align`/`bold` are the defaults; each entry in
`lines` can be a plain string (uses the defaults) or an object overriding
any of them individually. Long lines are word-wrapped automatically to fit
the 102x102 key.

### Back-to-back messages

If a producer writes faster than a message can be rendered, or two writes
land close together, a JSON message can arrive as several concatenated
JSON values (e.g. `{...}{...}`) instead of one. The parser handles this by
decoding as many complete JSON values as it finds and using only the
*last* one -- older ones are simply superseded, never causing a parse
error. Ending each message with a trailing newline (as `core_load.py` does)
isn't required, but makes messages easier to tell apart if they ever do
land back-to-back. Reading the pipe and rendering are also decoupled
internally, so a slow render never delays picking up the next message --
see "How it works internally" below.

## Practical example: CPU temperature every 10s

```bash
#!/bin/bash
PIPE=/tmp/dp_status.pipe
while true; do
  temp=$(sensors | awk '/Package id 0/ {print $4}')
  printf '#!color=#f5c542;size=18;align=center\nCPU\n%s\n' "$temp" > "$PIPE"
  sleep 10
done
```

Assign "Pipe Text" to a key with action value `/tmp/dp_status.pipe`, run
the script, and the key updates every 10 seconds.

## Practical example: first CPU core (core 0) load

`examples/core_load.py` shows the load **and temperature** of a given CPU
core, updating every second, with each number turning green/yellow/red
depending on how high it is. It takes the core number as its first
argument, and the pipe path (a second argument the plugin appends
automatically) as its second:

```
/tmp/dp_core0.pipe;python3 examples/core_load.py 0
```

Just assign that as the action value for "Pipe Text" on a key -- the
plugin creates the pipe and starts the script for you, running the
equivalent of:

```bash
python3 examples/core_load.py 0 /tmp/dp_core0.pipe
```

It uses `psutil` (already bundled with the BaseCamp Linux AppImage) to read
per-core usage and, where available, per-core temperature (via
`psutil.sensors_temperatures()`, using whatever the kernel's `coretemp` /
`k10temp` driver reports for that core, falling back to an overall CPU
temperature, or showing "N/A" if no sensor is available -- which is normal
inside VMs and containers). It writes a JSON message to the pipe every
second so the core label, load, and temperature can each have their own
color, e.g. rendering something like:

```
CORE 0
42%
58°C
```

## Practical example: battery status

`examples/battery_status.py` shows the laptop's battery percentage and
charging status, updating every 5 seconds, with the percentage turning
green/orange/red depending on the level and whether it's discharging. It
takes only the pipe path (the argument the plugin appends automatically):

```
/tmp/dp_battery.pipe;python3 examples/battery_status.py
```

Just assign that as the action value for "Pipe Text" on a key -- the
plugin creates the pipe and starts the script for you, running the
equivalent of:

```bash
python3 examples/battery_status.py /tmp/dp_battery.pipe
```

It uses `psutil` (already bundled with the BaseCamp Linux AppImage) to read
battery info via `psutil.sensors_battery()`. It writes a JSON message to
the pipe every 5 seconds with a title line, a colored percentage line, and
a status line, e.g. rendering something like:

```
Battery
72%
Discharging
```

The percentage is red if the battery is discharging and below 15%, orange
if below 30% (regardless of charging state), and green otherwise. The
status line shows "Charging", "Full" or "Discharging". On a machine with
no battery (desktop, VM, container), it shows "N/A" / "No battery" instead.

## How it works internally

Like `dp_clock`, this plugin registers an action type (`pipe_text`) and runs
a background scan loop (every ~2s) over the DisplayPad action config. For
every key assigned the "Pipe Text" type, it starts a dedicated reader thread
bound to the pipe path in that key's action field; if you change the path,
the old reader is stopped and a new one started automatically. If the
action is removed from a key, its reader is stopped.

Each reader opens its pipe non-blocking and uses `select()` so it can be
shut down cleanly; a message is whatever gets written between one pipe
open and close (matching typical `echo ... > pipe` usage). The reader
thread only ever grabs the raw bytes and hands them off -- it never parses
or renders -- so it can reopen the pipe for the next writer immediately.

A separate per-key render worker thread does the actual parsing and
rendering. It always works on the most recently received message; if a
newer one arrives while it's still busy with an older one, the older one
is discarded rather than queued, so the key never falls behind under load
and always ends up showing the latest state. Rendered frames are also
deduplicated by hash so unchanged content is not re-uploaded, and (like
`dp_clock`) the last frame is cached to disk and registered as the key's
static image so it survives a page switch/reload.

## Limitations / notes

- Key assignments are re-scanned every ~2 seconds, so a newly assigned key
  or a changed pipe path takes a moment to take effect.
- Writing to the pipe (`echo ... > pipe`) blocks briefly until the plugin
  has the pipe open for reading -- this happens almost immediately, but if
  you want to guarantee your script never blocks, write with
  `printf ... > pipe &`.
- Only one pipe path per key is supported at a time; assigning "Pipe Text"
  to the same key with a different path replaces the previous reader.
- When using the `;<command>` form, the command runs with whatever working
  directory the app itself was launched from -- use an absolute path to
  your script (or `cd /your/script/dir && command`) if you're not sure
  what that will be.
