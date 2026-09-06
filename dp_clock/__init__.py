"""DisplayPad Clock — live clock on a DisplayPad button.

Action value controls the format (comma-separated flags):
  (empty)     → HH:MM (24h, no seconds, no date)
  12h         → 12-hour with AM/PM
  sec         → show seconds  (HH:MM:SS)
  date        → show date below the time
  12h,sec     → combine flags

Press the assigned key to start a stopwatch on the button. Press again to
stop — the elapsed time stays visible for 5 seconds, then the clock returns.

Original idea and first implementation by FransM.
"""
import hashlib
import os
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw

try:
    from PIL import ImageFont
    _FONT_TIME = None
    _FONT_SEC = None
    _FONT_AMPM = None
    _FONT_DATE = None
    # Try common font paths across distros
    for fpath in (
        "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(fpath):
            _FONT_TIME = ImageFont.truetype(fpath, 28)
            _FONT_SEC = ImageFont.truetype(fpath, 14)
            _FONT_AMPM = ImageFont.truetype(fpath, 11)
            _FONT_DATE = ImageFont.truetype(fpath, 11)
            break
    if _FONT_TIME is None:
        _FONT_TIME = ImageFont.load_default()
        _FONT_SEC = _FONT_AMPM = _FONT_DATE = _FONT_TIME
except Exception:
    from PIL import ImageFont
    _FONT_TIME = ImageFont.load_default()
    _FONT_SEC = _FONT_AMPM = _FONT_DATE = _FONT_TIME

_BG = (16, 16, 36)
_FG = (220, 220, 240)
_CYAN = (14, 165, 233)
_GRAY = (90, 90, 136)


def _centered(draw, y, text, font, fill):
    tw = draw.textlength(text, font=font)
    draw.text(((102 - tw) / 2, y), text, fill=fill, font=font)


def _render_clock(flags, timer_state="off", elapsed=0.0):
    """Render a 102x102 clock image, or the stopwatch if timer is active."""
    now = datetime.now()
    show_sec = "sec" in flags
    show_12h = "12h" in flags
    show_date = "date" in flags

    img = Image.new("RGB", (102, 102), _BG)
    draw = ImageDraw.Draw(img)

    if timer_state == "off":
        if show_12h:
            hour = now.hour % 12 or 12
            ampm = "AM" if now.hour < 12 else "PM"
            if show_sec:
                time_str = f"{hour}:{now.minute:02d}"
                sec_str = f":{now.second:02d} {ampm}"
            else:
                time_str = f"{hour}:{now.minute:02d}"
                sec_str = ampm
        else:
            if show_sec:
                time_str = f"{now.hour:02d}:{now.minute:02d}"
                sec_str = f":{now.second:02d}"
            else:
                time_str = f"{now.hour:02d}:{now.minute:02d}"
                sec_str = None

        if show_date and sec_str:
            _centered(draw, 18, time_str, _FONT_TIME, _FG)
            _centered(draw, 48, sec_str, _FONT_SEC, _CYAN)
            date_str = now.strftime("%d %b %Y")
            _centered(draw, 72, date_str, _FONT_DATE, _GRAY)
        elif show_date:
            _centered(draw, 22, time_str, _FONT_TIME, _FG)
            date_str = now.strftime("%d %b %Y")
            _centered(draw, 62, date_str, _FONT_DATE, _GRAY)
        elif sec_str:
            _centered(draw, 28, time_str, _FONT_TIME, _FG)
            _centered(draw, 60, sec_str, _FONT_SEC, _CYAN)
        else:
            _centered(draw, 34, time_str, _FONT_TIME, _FG)

    else:
        minutes = int(elapsed // 60)
        secs = int(elapsed % 60)
        millis = int((elapsed % 1) * 100)
        time_str = f"{minutes:02d}:{secs:02d}.{millis:02d}"
        _centered(draw, 50, time_str, _FONT_SEC, _FG)

    return img



def _current_page(ctx):
    """The page that is on the pad right now, 0 if it cannot be asked.

    The frame files are named after it: two pages can put the same widget on
    the same physical key with different settings, and a name keyed on the key
    index alone meant both wrote to one file, so whichever rendered last
    decided what the other page showed (#88).
    """
    try:
        return int(ctx.get_displaypad_current_page())
    except Exception:
        return 0


def _save_frame(img, path):
    """Write the frame beside its destination and move it into place.

    The upload worker reads these files while we write them, and a PIL save
    straight onto the destination is not atomic: a reader that catches it half
    written gets "cannot identify image file" (#89).
    """
    import os as _os
    tmp = "%s.%d.tmp" % (path, _os.getpid())
    # The format has to be named: PIL takes it from the file extension, and
    # the extension here is .tmp.
    img.save(tmp, "PNG")
    _os.replace(tmp, path)

class Plugin:
    panel_id = "dp_clock"
    panel_label = "DP Clock"

    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._hashes = {}
        self._timer_state = "off"   # "off" | "running" | "stopping"
        self._elapsed = 0.0
        self._start_time = 0.0

        ctx.register_translations({
            "en": {"clock_display": "Clock"},
            "de": {"clock_display": "Uhr"},
        })

        ctx.register_action_type("clock_display", ctx.T("clock_display"), self.on_press)

    def start(self):
        """Begin the clock.

        Each thread carries its own stop, so a stopped one can never be
        revived alongside its successor: clearing one shared event let a
        predecessor that had not reached its next check carry on beside the
        new thread. A key edit brings the page's services in line straight
        away now, so a stop closely followed by a start is ordinary rather
        than rare.
        """
        self._stop.set()                # any predecessor ends here
        stop = threading.Event()
        self._stop = stop
        threading.Thread(target=self._loop, args=(stop,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self, stop):
        while not stop.is_set():
            try:
                if self._timer_state == "running":
                    self._elapsed = time.time() - self._start_time
                self._update()
            except Exception as e:
                # A transient error (DisplayPad busy, disk write, render) must
                # never kill this daemon thread — that was issue #8, where the
                # clock "suddenly stopped" and only a disable/enable (which
                # restarts the thread) brought it back. Log and keep ticking.
                try:
                    print(f"[dp_clock] update error (continuing): {e}", flush=True)
                except Exception:
                    pass
            # While the stopwatch runs we redraw fast; otherwise once per second.
            stop.wait(0.1 if self._timer_state != "off" else 1)

    def on_press(self, action_value):
        if self._timer_state != "running":
            self._timer_state = "running"
            self._start_time = time.time()
            self._elapsed = 0.0
        else:
            self._timer_state = "stopping"
            self._elapsed = time.time() - self._start_time
            threading.Thread(target=self._revert_after_delay, daemon=True).start()

    def _revert_after_delay(self):
        # Hold the final time on screen, then drop back to the clock.
        if self._stop.wait(5):
            return
        if self._timer_state == "stopping":
            self._timer_state = "off"

    def _current_actions(self):
        """The 12 button actions of the page that is actually on the pad.

        Not shared.config._load_displaypad_actions(): its page argument
        defaults to 0, so a clock assigned on a sub-page was never found while
        the one on Main kept being painted, on whatever key sat at that index
        on the visible page (issues #82 and #70)."""
        try:
            return self.ctx.get_displaypad_actions()
        except Exception:
            pass
        try:
            from shared.config import _load_displaypad_actions
            return _load_displaypad_actions()
        except Exception:
            return []

    def _update(self):
        try:
            from shared.config import CONFIG_DIR
        except ImportError:
            return

        actions = self._current_actions()
        dp = self.ctx.get_displaypad()

        for i, act in enumerate(actions):
            if act.get("type") != "clock_display":
                continue

            val = act.get("action", "").strip().lower()
            flags = set(f.strip() for f in val.split(",") if f.strip())

            img = _render_clock(flags, self._timer_state, self._elapsed)

            raw = img.tobytes()
            h = hashlib.md5(raw).hexdigest()
            if self._hashes.get(i) == h:
                continue
            self._hashes[i] = h

            page = _current_page(self.ctx)
            img_path = os.path.join(CONFIG_DIR, f"dp_clock_p{page}_k{i}.png")
            _save_frame(img, img_path)
            if dp:
                dp._images[str(i)] = img_path
                if hasattr(dp, "_page_images"):
                    dp._page_images.setdefault(page, {})[str(i)] = img_path

            self.ctx.push_displaypad_image(i, img)
