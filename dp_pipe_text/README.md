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

Restart BaseCamp Linux. Requires `Pillow` (bundled with the AppImage).

## Usage

1. Open the DisplayPad action config and pick **"Pipe Text"** as the type
   for a key.
2. In the action field, enter a pipe path, e.g.:

   ```
   /tmp/dp_status.pipe
   ```

3. The plugin creates the FIFO automatically if it doesn't exist yet, and
   continuously reads whatever is written to it, rendering the result onto
   that key.

   ```bash
   echo "Build OK" > /tmp/dp_status.pipe
   ```

Each key with a "Pipe Text" action gets its own independent reader thread
for the path you assigned to it, so multiple keys can each show something
different at the same time.

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

## How it works internally

Like `dp_clock`, this plugin registers an action type (`pipe_text`) and runs
a background scan loop (every ~2s) over the DisplayPad action config. For
every key assigned the "Pipe Text" type, it starts a dedicated reader thread
bound to the pipe path in that key's action field; if you change the path,
the old reader is stopped and a new one started automatically. If the
action is removed from a key, its reader is stopped.

Each reader opens its pipe non-blocking and uses `select()` so it can be
shut down cleanly; a full message is whatever gets written between one pipe
open and close (matching typical `echo ... > pipe` usage). Rendered frames
are deduplicated by hash so unchanged content is not re-uploaded, and (like
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
