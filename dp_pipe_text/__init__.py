"""DisplayPad Pipe Text -- render text from a named pipe onto a DisplayPad key.

This is an *action* plugin, following the same pattern as dp_clock: register
an action type, then let the user assign it to any key (K1-K12) via the
DisplayPad action config. The action value the user types in is not a
sound file or a command by itself -- it is a pipe path, optionally followed
by a producer command to run automatically.

Action field syntax
-------------------
    <pipe-path>
    <pipe-path>;<shell command>

1. Just a pipe path (no ";"): the plugin only creates the FIFO and reads
   from it. You are responsible for writing to it yourself:

       /tmp/dp_status.pipe

   echo "Build OK" > /tmp/dp_status.pipe

2. A pipe path followed by ";" and a shell command: the plugin creates the
   FIFO *and* launches the command for you, appending the pipe path as an
   extra argument. This is meant for the common case where the pipe only
   exists to feed one particular producer script:

       /tmp/dp_core0.pipe;python3 core_load.py 0

   is equivalent to manually creating /tmp/dp_core0.pipe and running:

       python3 core_load.py 0 /tmp/dp_core0.pipe

   The command runs in its own process group so it can be cleanly
   terminated (including any children it spawns) when the pipe path
   changes, the action is removed, or the app shuts down.

Setup
-----
1. In the DisplayPad action config, pick "Pipe Text" as the type for a key.
2. Enter a pipe path (optionally with ";<command>") in the action field.
3. The plugin creates the FIFO automatically if it doesn't exist yet, and
   continuously reads whatever is written to it, rendering the result onto
   that key. Multiple keys can each have their own "Pipe Text" action with
   their own pipe path.

If you later change the action field on a key, or remove the "Pipe Text"
action from it, the plugin stops the old reader (and the old command, if
one was launched) and deletes the old FIFO from disk (only if it's still a
FIFO) so stale pipes don't accumulate.

Pressing the assigned key clears it back to a blank background.

Message format
--------------
Two ways to write a message to the pipe:

1. Plain text (simplest). Each line becomes a rendered line, using default
   styling (centered, light gray, size 14). Optionally prefix with a
   directive line starting with "#!" to set defaults for the whole message:

       #!color=#22c55e;size=16;align=left;bg=#101828;valign=top
       Build: OK
       12 tests passed

   Directive keys: color, size, bold (0/1), align (left/center/right),
   valign (top/middle/bottom), bg (background color).

2. JSON, for per-line control. Either a bare list of strings, or an object:

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

   Top-level color/size/align/bold are defaults; each entry in "lines" can
   be a plain string (uses the defaults) or an object overriding any of
   them individually. Long lines are word-wrapped automatically to fit the
   102x102 key.

Sending an empty message (echo "" > pipe, or "{}") clears the key back to
a blank background.

Framing and back-to-back messages
----------------------------------
A message is whatever gets written to the pipe between one writer opening
it and closing it again (the usual `echo ... > pipe` / `with open(...) as
f: f.write(...)` pattern). If a producer writes messages faster than they
can be rendered, or two writes end up landing back-to-back without a gap,
a JSON message may arrive as several concatenated JSON values (e.g.
"{...}{...}") rather than a single one. The parser handles this by
decoding as many complete JSON values as it can find and using only the
*last* one -- older, superseded values are discarded rather than causing a
parse error. Producers are encouraged (but not required) to end each
message with a trailing newline, which makes back-to-back messages easier
to tell apart on the wire and in logs.

Reading the pipe and rendering the image are also decoupled internally:
a fast reader thread just grabs whatever was written and hands it off to a
per-key render worker, which always renders the most recently received
message and drops any older one still waiting -- so a slow render never
holds the pipe closed longer than necessary, and the key always ends up
showing the latest state rather than falling behind.

Original idea and rendering approach inspired by dp_clock.
"""

import hashlib
import json
import os
import select
import shlex
import signal
import stat
import subprocess
import threading
import time

from PIL import Image, ImageDraw, ImageColor

try:
    from PIL import ImageFont
    _FONT_REGULAR_PATH = None
    _FONT_BOLD_PATH = None
    for fpath in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if os.path.exists(fpath):
            _FONT_REGULAR_PATH = fpath
            break
    for fpath in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(fpath):
            _FONT_BOLD_PATH = fpath
            break
except Exception:
    ImageFont = None
    _FONT_REGULAR_PATH = None
    _FONT_BOLD_PATH = None

_SIZE = 102
_MARGIN = 4
_DEFAULT_BG = (16, 16, 36)
_DEFAULT_FG = (224, 224, 240)
_DEFAULT_SIZE = 14
_DEFAULT_ALIGN = "center"
_DEFAULT_VALIGN = "middle"

_font_cache = {}


def _get_font(size, bold=False):
    size = max(6, min(48, int(size)))
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = (_FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH) or _FONT_REGULAR_PATH
    font = None
    if ImageFont is not None and path:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            font = None
    if font is None and ImageFont is not None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _parse_color(value, fallback):
    if value is None:
        return fallback
    try:
        return ImageColor.getrgb(str(value))
    except Exception:
        return fallback


def _normalize_align(value, fallback):
    v = str(value).strip().lower() if value else fallback
    return v if v in ("left", "center", "right") else fallback


def _normalize_valign(value, fallback):
    v = str(value).strip().lower() if value else fallback
    return v if v in ("top", "middle", "bottom") else fallback


def _normalize_spec(raw_spec):
    """Turn a loosely-typed dict (from JSON or a directive line) into a
    canonical render spec: {bg, valign, lines:[{text,color,size,bold,align}]}"""
    bg = _parse_color(raw_spec.get("bg"), _DEFAULT_BG)
    valign = _normalize_valign(raw_spec.get("valign"), _DEFAULT_VALIGN)

    default_color = _parse_color(raw_spec.get("color"), _DEFAULT_FG)
    default_size = raw_spec.get("size", _DEFAULT_SIZE)
    try:
        default_size = int(default_size)
    except Exception:
        default_size = _DEFAULT_SIZE
    default_bold = str(raw_spec.get("bold", False)).strip().lower() in ("1", "true", "yes")
    default_align = _normalize_align(raw_spec.get("align"), _DEFAULT_ALIGN)

    lines_in = raw_spec.get("lines", [])
    lines_out = []
    for entry in lines_in:
        if isinstance(entry, dict):
            text = str(entry.get("text", ""))
            color = _parse_color(entry.get("color"), default_color)
            size = entry.get("size", default_size)
            try:
                size = int(size)
            except Exception:
                size = default_size
            bold = str(entry.get("bold", default_bold)).strip().lower() in ("1", "true", "yes")
            align = _normalize_align(entry.get("align"), default_align)
        else:
            text = str(entry)
            color, size, bold, align = default_color, default_size, default_bold, default_align
        lines_out.append({"text": text, "color": color, "size": size, "bold": bold, "align": align})

    return {"bg": bg, "valign": valign, "lines": lines_out}


_json_decoder = json.JSONDecoder()


def _extract_last_json(text):
    """Return the *last* complete JSON value found in text.

    Handles the case where a buffer contains more than one JSON value
    back-to-back with no separator (e.g. "{...}{...}") or separated by
    whitespace/newlines (e.g. "{...}\\n{...}\\n") -- which can happen if a
    producer writes faster than the pipe is drained. Earlier values are
    superseded by later ones and simply discarded. Any trailing content
    that isn't a complete JSON value is ignored. Returns None if no
    complete JSON value could be found at all.
    """
    idx = 0
    n = len(text)
    last_value = None
    found_any = False
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n or text[idx] not in "{[":
            break
        try:
            value, end = _json_decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        last_value = value
        found_any = True
        idx = end
    return last_value if found_any else None


def _parse_message(raw_text):
    """Parse whatever a client wrote to the pipe into a canonical spec."""
    text = raw_text.rstrip("\n")
    stripped = text.strip()

    if not stripped:
        return _normalize_spec({"lines": []})

    if stripped[0] in "{[":
        data = _extract_last_json(text)
        if isinstance(data, list):
            return _normalize_spec({"lines": data})
        if isinstance(data, dict):
            return _normalize_spec(data)
        # fall through to plain-text handling if no JSON value parsed at all

    lines = text.split("\n")
    directive = {}
    if lines and lines[0].startswith("#!"):
        for part in lines[0][2:].split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                directive[k.strip()] = v.strip()
        lines = lines[1:]

    spec = dict(directive)
    spec["lines"] = lines
    return _normalize_spec(spec)


def _parse_action(raw):
    """Split a key's action field into (pipe_path, command).

    Syntax: "<pipe-path>" or "<pipe-path>;<shell command>". Returns
    (None, None) if raw is empty/blank.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if ";" in raw:
        path_part, cmd_part = raw.split(";", 1)
        path = os.path.expanduser(path_part.strip())
        command = cmd_part.strip() or None
    else:
        path = os.path.expanduser(raw)
        command = None
    return (path or None), command


def _wrap_line(draw, text, font, max_width):
    """Word-wrap text to fit max_width, hard-breaking words that are too long."""
    if not text:
        return [""]
    words = text.split(" ")
    out, cur = [], ""
    for word in words:
        candidate = (cur + " " + word).strip() if cur else word
        if draw.textlength(candidate, font=font) <= max_width or not cur:
            cur = candidate
        else:
            out.append(cur)
            cur = word
        while draw.textlength(cur, font=font) > max_width and len(cur) > 1:
            # hard-break an overly long single word
            lo, hi = 1, len(cur)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if draw.textlength(cur[:mid], font=font) <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            out.append(cur[:lo])
            cur = cur[lo:]
    if cur:
        out.append(cur)
    return out or [""]


def _render_spec(spec):
    img = Image.new("RGB", (_SIZE, _SIZE), spec["bg"])
    draw = ImageDraw.Draw(img)
    max_width = _SIZE - 2 * _MARGIN

    rendered = []  # (text, color, font, align, line_height)
    for line in spec["lines"]:
        font = _get_font(line["size"], line["bold"])
        for sub in _wrap_line(draw, line["text"], font, max_width):
            ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (line["size"], 2)
            rendered.append((sub, line["color"], font, line["align"], ascent + descent + 2))

    total_height = sum(r[4] for r in rendered) if rendered else 0

    if spec["valign"] == "top":
        y = _MARGIN
    elif spec["valign"] == "bottom":
        y = _SIZE - _MARGIN - total_height
    else:
        y = (_SIZE - total_height) / 2
    y = max(_MARGIN, y)

    for text, color, font, align, line_height in rendered:
        tw = draw.textlength(text, font=font) if text else 0
        if align == "left":
            x = _MARGIN
        elif align == "right":
            x = _SIZE - _MARGIN - tw
        else:
            x = (_SIZE - tw) / 2
        if text:
            draw.text((x, y), text, fill=color, font=font)
        y += line_height

    return img


class Plugin:
    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._readers = {}   # key_index -> {"path": str, "raw": str, "stop": Event,
                              #               "thread": Thread, "work_event": Event,
                              #               "worker_thread": Thread}
        self._procs = {}     # key_index -> Popen (producer command, if any)
        self._hashes = {}    # key_index -> last pushed image hash
        self._pending = {}        # key_index -> latest not-yet-rendered raw message
        self._pending_lock = threading.Lock()

        ctx.register_translations({
            "en": {"pipe_text": "Pipe Text"},
            "de": {"pipe_text": "Pipe Text"},
        })
        ctx.register_action_type("pipe_text", ctx.T("pipe_text"), self.on_press)

    def start(self):
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            for info in self._readers.values():
                info["stop"].set()
            for key_index in list(self._procs):
                self._stop_command(key_index)

    # ------------------------------------------------------------ scanner --
    # Watches the DisplayPad action config for keys assigned the "pipe_text"
    # type, and keeps one reader thread per key in sync with the pipe path
    # the user typed into that key's action field.

    def _scan_loop(self):
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                # A transient error here must never kill this daemon thread --
                # log and keep scanning, same lesson as dp_clock issue #8.
                try:
                    print(f"[dp_pipe_text] scan error (continuing): {e}", flush=True)
                except Exception:
                    pass
            self._stop.wait(2)

    def _scan_once(self):
        try:
            from shared.config import _load_displaypad_actions
        except ImportError:
            return
        actions = _load_displaypad_actions()

        assigned = {}
        for i, act in enumerate(actions):
            if act.get("type") != "pipe_text":
                continue
            raw = act.get("action", "").strip()
            if not raw:
                continue
            path, command = _parse_action(raw)
            if not path:
                continue
            assigned[i] = (path, command, raw)

        with self._lock:
            for key_index, (path, command, raw) in assigned.items():
                current = self._readers.get(key_index)
                if current is not None and current["raw"] == raw:
                    continue
                if current is not None:
                    # The action field changed (pipe path and/or command) --
                    # stop the old reader/command and remove the old FIFO
                    # from disk so stale pipes don't pile up.
                    current["stop"].set()
                    self._remove_pipe_file(current["path"])
                    self._stop_command(key_index)
                    with self._pending_lock:
                        self._pending.pop(key_index, None)
                self._start_reader(key_index, path, command, raw)

            gone = [k for k in self._readers if k not in assigned]
            for k in gone:
                # The action was unassigned/removed from this key -- stop
                # the reader/command and clean up the pipe it was using.
                self._readers[k]["stop"].set()
                self._remove_pipe_file(self._readers[k]["path"])
                self._stop_command(k)
                with self._pending_lock:
                    self._pending.pop(k, None)
                del self._readers[k]

    def _remove_pipe_file(self, path):
        """Best-effort removal of a FIFO we previously created/used."""
        try:
            if path and os.path.exists(path) and stat.S_ISFIFO(os.stat(path).st_mode):
                os.remove(path)
                print(f"[dp_pipe_text] removed old pipe {path}", flush=True)
        except Exception as e:
            print(f"[dp_pipe_text] could not remove old pipe {path}: {e}", flush=True)

    def _start_reader(self, key_index, path, command, raw):
        try:
            dirpath = os.path.dirname(path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            if not os.path.exists(path):
                os.mkfifo(path)
            elif not stat.S_ISFIFO(os.stat(path).st_mode):
                print(f"[dp_pipe_text] {path} exists and is not a FIFO, skipping", flush=True)
                return
        except Exception as e:
            print(f"[dp_pipe_text] could not prepare pipe {path}: {e}", flush=True)
            return

        stop_event = threading.Event()
        work_event = threading.Event()
        reader_thread = threading.Thread(
            target=self._reader_loop, args=(path, key_index, stop_event), daemon=True
        )
        worker_thread = threading.Thread(
            target=self._render_worker, args=(key_index, stop_event, work_event), daemon=True
        )
        self._readers[key_index] = {
            "path": path, "raw": raw, "stop": stop_event,
            "thread": reader_thread, "work_event": work_event, "worker_thread": worker_thread,
        }
        reader_thread.start()
        worker_thread.start()

        if command:
            self._start_command(key_index, path, command)

    def _start_command(self, key_index, path, command):
        """Launch the producer command, appending the pipe path as an
        extra argument, e.g. "python3 core_load.py 0" + path becomes
        "python3 core_load.py 0 /tmp/dp_core0.pipe"."""
        full_cmd = f"{command} {shlex.quote(path)}"
        try:
            proc = subprocess.Popen(
                full_cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # own process group, for clean teardown
            )
            self._procs[key_index] = proc
            print(f"[dp_pipe_text] started command for key {key_index + 1}: {full_cmd}", flush=True)
        except Exception as e:
            print(f"[dp_pipe_text] failed to start command '{full_cmd}': {e}", flush=True)

    def _stop_command(self, key_index):
        proc = self._procs.pop(key_index, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            print(f"[dp_pipe_text] error stopping command for key {key_index + 1}: {e}", flush=True)

    # ------------------------------------------------------------- reader --

    def _reader_loop(self, path, key_index, stop_event):
        while not stop_event.is_set() and not self._stop.is_set():
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                return
            except OSError as e:
                print(f"[dp_pipe_text] open failed for {path}: {e}", flush=True)
                stop_event.wait(1)
                continue

            buf = b""
            got_data = False
            try:
                while not stop_event.is_set() and not self._stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 0.5)
                    if not ready:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        # EOF: writer closed. Reopen for the next writer.
                        break
                    buf += chunk
                    got_data = True
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

            # Hand the raw message off to this key's render worker and go
            # straight back to the top of the loop to reopen the pipe --
            # parsing/rendering never blocks the read side, so the pipe is
            # available for the next writer again as fast as possible.
            if got_data:
                self._handle_message(key_index, buf.decode("utf-8", errors="replace"))

    def _handle_message(self, key_index, raw_text):
        """Stash the latest raw message and wake the render worker for this
        key. Cheap and non-blocking -- any message not yet picked up by the
        worker is simply overwritten, so only the freshest state survives."""
        info = self._readers.get(key_index)
        if info is None:
            return
        with self._pending_lock:
            self._pending[key_index] = raw_text
        info["work_event"].set()

    def _render_worker(self, key_index, stop_event, work_event):
        """Runs alongside the reader thread for this key. Renders and
        pushes whatever is the most recently received message, never
        falling behind on a backlog of stale ones."""
        while not stop_event.is_set() and not self._stop.is_set():
            if not work_event.wait(timeout=0.5):
                continue
            with self._pending_lock:
                raw_text = self._pending.pop(key_index, None)
                work_event.clear()
            if raw_text is None:
                continue
            try:
                spec = _parse_message(raw_text)
                img = _render_spec(spec)
                self._push_image(key_index, img)
            except Exception as e:
                print(f"[dp_pipe_text] render error for key {key_index + 1}: {e}", flush=True)

    # ----------------------------------------------------------- actions --

    def on_press(self, action_value):
        """Pressing the assigned key clears it back to a blank background."""
        path, _command = _parse_action(action_value)
        with self._lock:
            for key_index, info in self._readers.items():
                if info["path"] == path:
                    blank = Image.new("RGB", (_SIZE, _SIZE), _DEFAULT_BG)
                    self._push_image(key_index, blank)
                    break

    # -------------------------------------------------------------- push --

    def _push_image(self, key_index, img):
        raw = img.tobytes()
        h = hashlib.md5(raw).hexdigest()
        if self._hashes.get(key_index) == h:
            return
        self._hashes[key_index] = h
        self.ctx.push_displaypad_image(key_index, img)

        # Persist the rendered frame to disk and register it as the key's
        # static image, so it survives a page switch/reload -- same trick
        # dp_clock uses for its stopwatch frames.
        try:
            from shared.config import CONFIG_DIR
            img_path = os.path.join(CONFIG_DIR, f"dp_pipe_text_{key_index}.png")
            img.save(img_path)
            dp = self.ctx.get_displaypad()
            if dp:
                dp._images[str(key_index)] = img_path
                if hasattr(dp, "_page_images") and 0 in dp._page_images:
                    dp._page_images[0][str(key_index)] = img_path
        except Exception:
            pass
