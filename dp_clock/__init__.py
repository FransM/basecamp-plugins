"""DisplayPad Clock — live clock on a DisplayPad button.

Action value controls the format (comma-separated flags):
  (empty)     → HH:MM (24h, no seconds, no date)
  12h         → 12-hour with AM/PM
  sec         → show seconds  (HH:MM:SS)
  date        → show date below the time
  12h,sec     → combine flags

Original idea and first implementation by FransM.
"""
import hashlib
import os
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw

_timer_state = "off"
_elapsed_time = 0
_start_time = 0

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


def _render_clock(flags):
    """Render a 102x102 clock image based on flags set."""
    global _timer_state
    global _elapsed_time
    global _start_time

    now = datetime.now()
    show_sec = "sec" in flags
    show_12h = "12h" in flags
    show_date = "date" in flags

    img = Image.new("RGB", (102, 102), _BG)
    draw = ImageDraw.Draw(img)

    if _timer_state == "off":
        # Time string
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

        # Layout depends on what's shown
        if show_date and sec_str:
            # Time + seconds/ampm + date → pack tighter
            _centered(draw, 18, time_str, _FONT_TIME, _FG)
            _centered(draw, 48, sec_str, _FONT_SEC, _CYAN)
            date_str = now.strftime("%d %b %Y")
            _centered(draw, 72, date_str, _FONT_DATE, _GRAY)
        elif show_date:
            # Time + date
            _centered(draw, 22, time_str, _FONT_TIME, _FG)
            date_str = now.strftime("%d %b %Y")
            _centered(draw, 62, date_str, _FONT_DATE, _GRAY)
        elif sec_str:
            # Time + seconds/ampm
            _centered(draw, 28, time_str, _FONT_TIME, _FG)
            _centered(draw, 60, sec_str, _FONT_SEC, _CYAN)
        else:
            # Just time, centered
            _centered(draw, 34, time_str, _FONT_TIME, _FG)

    else:
        if _timer_state == "running":
            _elapsed_time = time.time() - _start_time
        minutes = int(_elapsed_time // 60)
        secs = int(_elapsed_time % 60)
        millis = int((_elapsed_time % 1) * 100)
        time_str = f"{minutes:02d}:{secs:02d}.{millis:02d}"

        _centered(draw, 50, time_str, _FONT_SEC, _FG)

    return img


def _stop_timer():
    global _timer_state

    time.sleep(5)
    if _timer_state == "stopping":
        _timer_state = "off"


class Plugin:
    panel_id = "dp_clock"
    panel_label = "DP Clock"
    cfg = {}


    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._hashes = {}

        ctx.register_translations({
            "en": {
                "clock_display": "Clock",
            },
            "de": {
                "clock_display": "Uhr",
            }
        })

        ctx.register_action_type("clock_display", ctx.T("clock_display"), self.on_press)

    def optionmenu_callback(choice):
        print("optionmenu dropdown clicked:", choice)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self._update()
            if _timer_state == "off":
                # Update every second if any clock shows seconds, else every 10s
                self._stop.wait(1)
            else:
                self._stop.wait(0.1)

    def on_press(self, action_value):
        global _timer_state
        global _elapsed_time
        global _start_time

        if _timer_state != "running":
            _timer_state = "running"
            _start_time = time.time()
            _elapsed_time = 0
        else:
            _timer_state = "stopping"
            _elapsed_time = time.time() - _start_time
            thread = threading.Thread(target=_stop_timer)
            thread.start()

    def _update(self):
        try:
            from shared.config import _load_displaypad_actions, CONFIG_DIR
        except ImportError:
            return

        actions = _load_displaypad_actions()
        dp = self.ctx.get_displaypad()

        for i, act in enumerate(actions):
            if act.get("type") != "clock_display":
                continue

            # Parse flags from action value
            val = act.get("action", "").strip().lower()
            flags = set(f.strip() for f in val.split(",") if f.strip())

            img = _render_clock(flags)

            # Only push if image changed
            raw = img.tobytes()
            h = hashlib.md5(raw).hexdigest()
            if self._hashes.get(i) == h:
                continue
            self._hashes[i] = h

            img_path = os.path.join(CONFIG_DIR, f"dp_clock_{i}.png")
            img.save(img_path)
            if dp:
                dp._images[str(i)] = img_path
                if hasattr(dp, "_page_images") and 0 in dp._page_images:
                    dp._page_images[0][str(i)] = img_path

            self.ctx.push_displaypad_image(i, img)
