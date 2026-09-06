"""Philips Hue -- control Hue lights from DisplayPad buttons."""
import json
import threading
import urllib.request
import customtkinter as ctk
from PIL import Image, ImageDraw

try:
    from shared.ui_helpers import BG, BG2, BG3, FG, FG2, BLUE, GRN, RED, YLW, BORDER
except ImportError:
    BG, BG2, BG3 = "#0e0e1a", "#16162a", "#222244"
    FG, FG2 = "#e0e0e0", "#707090"
    BLUE, GRN, RED, YLW = "#0ea5e9", "#22c55e", "#dc2626", "#f5c542"
    BORDER = "#2a2a4a"

_AMBER = "#f59e0b"
_PURPLE = "#a855f7"


# ── Hue Bridge HTTP ─────────────────────────────────────────────────────────

def _hue(method, bridge_ip, api_key, path, data=None):
    """Single HTTP helper for all Hue Bridge calls.

    Returns whatever the bridge answered, or None if it did not answer at
    all. A V1 bridge says no with HTTP 200 and a list of error objects, so a
    refusal is a perfectly good answer and reaches the caller as one; use
    _hue_error() to read it (#16).
    """
    try:
        url = f"http://{bridge_ip}/api/{api_key}/{path}" if api_key else f"http://{bridge_ip}/api"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _hue_error(result):
    """What the bridge refused with, as text, or None if it did not refuse.

    The V1 API answers `[{"error": {"type": 1, "description": "unauthorized
    user"}}]` for a key it does not know, with a 200 beside it. Every caller
    here only checked whether it had a dict, so a stored key that the bridge
    had forgotten was indistinguishable from a bridge that was switched off,
    and both came out as a bare "Not connected" (#16).
    """
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and "error" in item:
                err = item["error"]
                if isinstance(err, dict):
                    return (err.get("description")
                            or "bridge error %s" % err.get("type", "?"))
                return str(err)
    return None


def _xy_to_rgb(x, y, bri=254):
    z = 1.0 - x - y if y > 0 else 0
    Y = bri / 254.0
    X = (Y / y) * x if y > 0 else 0
    Z = (Y / y) * z if y > 0 else 0
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    r = 12.92 * r if r <= 0.0031308 else (1.055 * (r ** (1 / 2.4)) - 0.055)
    g = 12.92 * g if g <= 0.0031308 else (1.055 * (g ** (1 / 2.4)) - 0.055)
    b = 12.92 * b if b <= 0.0031308 else (1.055 * (b ** (1 / 2.4)) - 0.055)
    return tuple(max(0, min(255, int(c * 255))) for c in (r, g, b))


def _ct_to_rgb(ct):
    kelvin = max(1000, min(10000, 1000000 // ct)) if ct else 4000
    t = kelvin / 100.0
    if t <= 66:
        r = 255
        g = max(0, min(255, int(99.47 * pow(t, 0.3901) - 161.12))) if t > 1 else 0
        b = 0 if t <= 19 else max(0, min(255, int(138.52 * pow(t - 10, 0.50) - 305.04)))
    else:
        r = max(0, min(255, int(329.70 * pow(t - 60, -0.1332))))
        g = max(0, min(255, int(288.12 * pow(t - 60, -0.0755))))
        b = 255
    return (r, g, b)


def _light_rgb(state):
    if not state.get("on", False):
        return (40, 40, 40)
    bri = state.get("bri", 254)
    cm = state.get("colormode", "ct")
    if cm == "xy" and "xy" in state:
        return _xy_to_rgb(state["xy"][0], state["xy"][1], bri)
    if cm == "ct" and "ct" in state:
        rgb = _ct_to_rgb(state["ct"])
        f = bri / 254.0
        return (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))
    f = bri / 254.0
    return (int(255 * f), int(200 * f), int(120 * f))


# ── Plugin ───────────────────────────────────────────────────────────────────


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
    panel_id = "hue_control"
    panel_label = "Philips Hue"

    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._lock = threading.Lock()

        cfg = ctx.load_plugin_config("hue_control")
        self._bridge_ip = cfg.get("bridge_ip", "")
        self._api_key = cfg.get("api_key", "")

        self._lights = {}
        self._groups = {}
        self._scenes = {}
        self._connected = False
        self._last_error = ""   # why not, for the status line (#16)
        self._win = None  # HueWindow instance

        ctx.register_translations({
            "en": {
                "hue_title":       "Philips Hue",
                "hue_open":        "Open Hue Control",
                "hue_bridge_ip":   "Bridge IP",
                "hue_pair":        "Pair",
                "hue_pairing":     "Press bridge button...",
                "hue_paired":      "Paired!",
                "hue_pair_fail":   "Pairing failed",
                "hue_connected":   "Connected",
                "hue_disconnected": "Not connected",
                "hue_no_bridge":   "Enter Bridge IP and pair first",
                "hue_lights":      "Lights",
                "hue_groups":      "Groups",
                "hue_scenes":      "Scenes",
                "hue_on":          "ON",
                "hue_off":         "OFF",
                "hue_all_on":      "All On",
                "hue_all_off":     "All Off",
                "hue_retry":       "Try again",
                "hue_toggle":      "Hue: Toggle Light",
                "hue_scene":       "Hue: Activate Scene",
                "hue_bri":         "Hue: Brightness",
            },
            "de": {
                "hue_title":       "Philips Hue",
                "hue_open":        "Hue Steuerung öffnen",
                "hue_bridge_ip":   "Bridge IP",
                "hue_pair":        "Koppeln",
                "hue_pairing":     "Bridge-Taste drücken...",
                "hue_paired":      "Gekoppelt!",
                "hue_pair_fail":   "Kopplung fehlgeschlagen",
                "hue_connected":   "Verbunden",
                "hue_disconnected": "Nicht verbunden",
                "hue_no_bridge":   "Bridge-IP eingeben und koppeln",
                "hue_lights":      "Lampen",
                "hue_groups":      "Gruppen",
                "hue_scenes":      "Szenen",
                "hue_on":          "AN",
                "hue_off":         "AUS",
                "hue_all_on":      "Alles an",
                "hue_all_off":     "Alles aus",
                "hue_retry":       "Erneut versuchen",
                "hue_toggle":      "Hue: Licht umschalten",
                "hue_scene":       "Hue: Szene aktivieren",
                "hue_bri":         "Hue: Helligkeit",
            }
        })

        ctx.register_action_type("hue_toggle", ctx.T("hue_toggle"), self._action_toggle)
        ctx.register_action_type("hue_scene", ctx.T("hue_scene"), self._action_scene)
        ctx.register_action_type("hue_bri", ctx.T("hue_bri"), self._action_bri)

    # ── Panel (minimal — just a launcher) ────────────────────────────────────

    def create_panel(self, parent):
        frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(hdr, text=self.ctx.T("hue_title"),
                     font=("Helvetica", 14, "bold"), text_color=FG).pack(side="left")
        self._panel_status = ctk.CTkLabel(hdr, text="", font=("Helvetica", 10), text_color=FG2)
        self._panel_status.pack(side="right")

        # Bridge IP row
        cfg_frame = ctk.CTkFrame(frame, fg_color=BG2, corner_radius=6)
        cfg_frame.pack(fill="x", padx=16, pady=(8, 4))
        row = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(row, text=self.ctx.T("hue_bridge_ip"),
                     font=("Helvetica", 10), text_color=FG2).pack(side="left")
        self._ip_entry = ctk.CTkEntry(row, width=160, height=28, fg_color=BG3,
                                       border_color=BORDER, text_color=FG, font=("Helvetica", 11))
        self._ip_entry.pack(side="left", padx=(8, 8))
        if self._bridge_ip:
            self._ip_entry.insert(0, self._bridge_ip)
        self._pair_btn = ctk.CTkButton(
            row, text=self.ctx.T("hue_pair"), font=("Helvetica", 10, "bold"),
            fg_color=BLUE, hover_color="#0284c7", text_color=FG,
            height=28, width=80, corner_radius=4, command=self._start_pairing)
        self._pair_btn.pack(side="left")
        self._pair_lbl = ctk.CTkLabel(row, text="", font=("Helvetica", 10), text_color=FG2)
        self._pair_lbl.pack(side="left", padx=(10, 0))

        # Open button
        ctk.CTkButton(
            frame, text=self.ctx.T("hue_open"),
            font=("Helvetica", 12, "bold"),
            fg_color=BLUE, hover_color="#0284c7", text_color=FG,
            height=36, width=200, corner_radius=6,
            command=self._open_window
        ).pack(padx=16, pady=(20, 8))

        self._update_panel_status()
        return frame

    def _update_panel_status(self):
        if not hasattr(self, "_panel_status"):
            return
        if self._connected:
            n = len(self._lights)
            self._panel_status.configure(
                text=f"\u2022 {self.ctx.T('hue_connected')}  ({n} lights)", text_color=GRN)
        else:
            why = getattr(self, "_last_error", "")
            text = f"\u2022 {self.ctx.T('hue_disconnected')}"
            if why:
                text += f": {why}"
            self._panel_status.configure(text=text, text_color=RED)

    # ── Hue Control Window ───────────────────────────────────────────────────

    def _open_window(self):
        if self._win and self._win.winfo_exists():
            self._win.focus()
            return
        self._win = HueWindow(self)

    # ── Pairing ──────────────────────────────────────────────────────────────

    def _start_pairing(self):
        ip = self._ip_entry.get().strip()
        if not ip:
            return
        self._bridge_ip = ip
        self._pair_lbl.configure(text=self.ctx.T("hue_pairing"), text_color=YLW)
        self._pair_btn.configure(state="disabled")
        threading.Thread(target=self._pair_loop, args=(ip,), daemon=True).start()

    def _pair_loop(self, ip):
        for _ in range(30):
            if self._stop.is_set():
                return
            result = _hue("POST", ip, None, "", {"devicetype": "basecamp_linux#hue"})
            if result and isinstance(result, list) and len(result) > 0:
                if "success" in result[0]:
                    self._api_key = result[0]["success"]["username"]
                    self.ctx.save_plugin_config("hue_control", {
                        "bridge_ip": self._bridge_ip, "api_key": self._api_key})
                    self.ctx.schedule(0, lambda: self._on_paired(True))
                    return
            self._stop.wait(1)
        self.ctx.schedule(0, lambda: self._on_paired(False))

    def _on_paired(self, ok):
        self._pair_btn.configure(state="normal")
        self._pair_lbl.configure(
            text=self.ctx.T("hue_paired") if ok else self.ctx.T("hue_pair_fail"),
            text_color=GRN if ok else RED)

    # ── State fetching ───────────────────────────────────────────────────────

    def _fetch(self, include_scenes=False):
        """Fetch state from bridge. Called from background thread."""
        if not self._bridge_ip or not self._api_key:
            return
        lights = _hue("GET", self._bridge_ip, self._api_key, "lights")
        groups = _hue("GET", self._bridge_ip, self._api_key, "groups")
        with self._lock:
            if isinstance(lights, dict):
                self._lights = lights
                self._connected = True
                self._last_error = ""
                if isinstance(groups, dict):
                    # Entertainment groups are set up by the sync software and
                    # do not appear in the Hue app, so a second group holding
                    # the same lights showed up here with no way to tell what
                    # it was or where it came from (#16).
                    self._groups = {
                        k: v for k, v in groups.items()
                        if isinstance(v, dict)
                        and v.get("type") != "Entertainment"}
            else:
                self._connected = False
                # Why, so the screen can say more than "not connected": a key
                # the bridge has forgotten and a bridge that is switched off
                # used to look exactly the same from here (#16).
                self._last_error = (_hue_error(lights)
                                    or f"no answer from {self._bridge_ip}")
        if not self._connected:
            # Out through the same door as a good fetch. The status line and
            # the window are only ever refreshed from there, so returning
            # straight from the branch above is what kept the reason off the
            # screen it was gathered for.
            self.ctx.schedule(0, self._on_fetched)
            return
        if include_scenes:
            scenes = _hue("GET", self._bridge_ip, self._api_key, "scenes")
            if isinstance(scenes, dict):
                with self._lock:
                    self._scenes = {
                        k: v for k, v in scenes.items()
                        if v.get("type") in ("GroupScene", "LightScene")
                        and not v.get("recycle", False)}
        self.ctx.schedule(0, self._on_fetched)

    def _on_fetched(self):
        self._update_panel_status()
        if self._win and self._win.winfo_exists():
            self._win.refresh()
        self._update_displaypad()

    # ── Action handlers ──────────────────────────────────────────────────────

    def _action_toggle(self, value):
        if not self._bridge_ip or not self._api_key:
            return
        parts = value.split(":", 1)
        if len(parts) != 2:
            return
        kind, oid = parts
        if kind == "light":
            on = self._lights.get(oid, {}).get("state", {}).get("on", False)
            _hue("PUT", self._bridge_ip, self._api_key, f"lights/{oid}/state", {"on": not on})
        elif kind == "group":
            on = self._groups.get(oid, {}).get("state", {}).get("any_on", False)
            _hue("PUT", self._bridge_ip, self._api_key, f"groups/{oid}/action", {"on": not on})
        self._fetch()

    def _action_scene(self, value):
        if not self._bridge_ip or not self._api_key:
            return
        parts = value.split(":", 1)
        if len(parts) != 2:
            return
        gid, sid = parts
        _hue("PUT", self._bridge_ip, self._api_key, f"groups/{gid}/action", {"scene": sid})
        self._fetch()

    def _action_bri(self, value):
        """Set brightness. value: 'light:ID:PERCENT' or 'group:ID:PERCENT'."""
        if not self._bridge_ip or not self._api_key:
            return
        parts = value.split(":")
        if len(parts) != 3:
            return
        kind, oid, pct_s = parts
        try:
            pct = int(pct_s)
        except ValueError:
            return
        bri = max(1, min(254, int(pct * 254 / 100)))
        data = {"bri": bri, "on": True} if pct > 0 else {"on": False}
        if kind == "light":
            _hue("PUT", self._bridge_ip, self._api_key, f"lights/{oid}/state", data)
        elif kind == "group":
            _hue("PUT", self._bridge_ip, self._api_key, f"groups/{oid}/action", data)
        self._fetch()

    # ── DisplayPad rendering ─────────────────────────────────────────────────

    def _current_actions(self):
        """The 12 button actions of the page that is actually on the pad.

        This plugin was left out of the #82 round. Asking without a page
        answers with Main, so a Hue key on a sub-page was never found while
        Main's key kept being painted, onto whatever sat at that index on the
        page actually being looked at.
        """
        try:
            return self.ctx.get_displaypad_actions()
        except Exception:
            pass
        try:
            from shared.config import _load_displaypad_actions
            return _load_displaypad_actions()
        except Exception:
            return []

    def _update_displaypad(self):
        """Render Hue button images, save to disk, and register with DisplayPad.
        Only pushes when the image content actually changed (avoids USB conflicts).
        """
        import os, hashlib
        try:
            from shared.config import CONFIG_DIR
        except ImportError:
            return
        actions = self._current_actions()
        dp = self.ctx.get_displaypad()
        for i, act in enumerate(actions):
            atype, aval = act.get("type", ""), act.get("action", "")
            img = None
            if atype == "hue_toggle" and aval:
                img = self._render_btn(aval)
            elif atype == "hue_scene" and aval:
                img = self._render_scene_btn(aval)
            elif atype == "hue_bri" and aval:
                img = self._render_bri_btn(aval)

            if not img:
                continue

            # Check if image actually changed (avoid redundant USB uploads)
            raw = img.tobytes()
            h = hashlib.md5(raw).hexdigest()
            if not hasattr(self, "_dp_hashes"):
                self._dp_hashes = {}
            if self._dp_hashes.get(i) == h:
                continue
            self._dp_hashes[i] = h

            # Save rendered image to disk so DisplayPad's normal upload uses it
            page = _current_page(self.ctx)
            img_path = os.path.join(CONFIG_DIR, f"dp_hue_p{page}_k{i}.png")
            _save_frame(img, img_path)

            # Register in DisplayPad's _images dict so full uploads include it
            if dp:
                dp._images[str(i)] = img_path
                if hasattr(dp, "_page_images"):
                    dp._page_images.setdefault(page, {})[str(i)] = img_path

            # Push to device
            self.ctx.push_displaypad_image(i, img)

    def _render_btn(self, value):
        parts = value.split(":", 1)
        if len(parts) != 2:
            return None
        kind, oid = parts
        if kind == "light":
            info = self._lights.get(oid, {})
            name, state = info.get("name", f"L{oid}"), info.get("state", {})
            is_on, rgb = state.get("on", False), _light_rgb(state)
        elif kind == "group":
            info = self._groups.get(oid, {})
            name = info.get("name", f"G{oid}")
            is_on = info.get("state", {}).get("any_on", False)
            rgb = _light_rgb(info.get("action", {})) if is_on else (40, 40, 40)
        else:
            return None
        img = Image.new("RGB", (102, 102), (16, 16, 36))
        draw = ImageDraw.Draw(img)
        draw.ellipse([31, 18, 71, 58], fill=rgb if is_on else (40, 40, 40),
                      outline=(80, 80, 80) if not is_on else None)
        try:
            from PIL import ImageFont
            fsm = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Bold.ttf", 11)
            fxs = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Regular.ttf", 9)
        except Exception:
            from PIL import ImageFont
            fsm = fxs = ImageFont.load_default()
        st = "ON" if is_on else "OFF"
        tw = draw.textlength(st, font=fsm)
        draw.text(((102 - tw) / 2, 64), st, fill=(34, 197, 94) if is_on else (90, 90, 136), font=fsm)
        if len(name) > 12:
            name = name[:11] + "\u2026"
        tw = draw.textlength(name, font=fxs)
        draw.text(((102 - tw) / 2, 82), name, fill=(200, 200, 220), font=fxs)
        return img

    def _render_scene_btn(self, value):
        parts = value.split(":", 1)
        if len(parts) != 2:
            return None
        gid, sid = parts
        name = self._scenes.get(sid, {}).get("name", "Scene")
        img = Image.new("RGB", (102, 102), (16, 16, 36))
        draw = ImageDraw.Draw(img)
        try:
            from PIL import ImageFont
            fsm = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Bold.ttf", 11)
            fxs = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Regular.ttf", 9)
        except Exception:
            from PIL import ImageFont
            fsm = fxs = ImageFont.load_default()
        if len(name) > 12:
            name = name[:11] + "\u2026"
        tw = draw.textlength(name, font=fsm)
        draw.text(((102 - tw) / 2, 40), name, fill=(200, 200, 220), font=fsm)
        gname = self._groups.get(gid, {}).get("name", "")
        if gname:
            if len(gname) > 14:
                gname = gname[:13] + "\u2026"
            tw = draw.textlength(gname, font=fxs)
            draw.text(((102 - tw) / 2, 60), gname, fill=(90, 90, 136), font=fxs)
        return img

    def _render_bri_btn(self, value):
        """Render a 102x102 brightness button. value: 'kind:ID:PERCENT'."""
        parts = value.split(":")
        if len(parts) != 3:
            return None
        kind, oid, pct_s = parts
        try:
            pct = int(pct_s)
        except ValueError:
            return None
        if kind == "light":
            name = self._lights.get(oid, {}).get("name", f"Light {oid}")
        elif kind == "group":
            name = self._groups.get(oid, {}).get("name", f"Group {oid}")
        else:
            return None
        img = Image.new("RGB", (102, 102), (16, 16, 36))
        draw = ImageDraw.Draw(img)
        # Brightness bar
        bar_w = int(80 * pct / 100)
        bar_color = (245, 158, 11)  # amber
        draw.rounded_rectangle([11, 28, 91, 48], radius=4, fill=(30, 30, 50))
        if bar_w > 0:
            draw.rounded_rectangle([11, 28, 11 + bar_w, 48], radius=4, fill=bar_color)
        try:
            from PIL import ImageFont
            fsm = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Bold.ttf", 14)
            fxs = ImageFont.truetype("/usr/share/fonts/google-noto/NotoSans-Regular.ttf", 9)
        except Exception:
            from PIL import ImageFont
            fsm = fxs = ImageFont.load_default()
        # Percentage text
        pct_text = f"{pct}%"
        tw = draw.textlength(pct_text, font=fsm)
        draw.text(((102 - tw) / 2, 54), pct_text, fill=bar_color, font=fsm)
        # Name
        if len(name) > 12:
            name = name[:11] + "\u2026"
        tw = draw.textlength(name, font=fxs)
        draw.text(((102 - tw) / 2, 78), name, fill=(200, 200, 220), font=fxs)
        return img

    # ── Service ──────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        if self._bridge_ip and self._api_key:
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        self._fetch(include_scenes=True)
        while not self._stop.is_set():
            self._stop.wait(3)
            if not self._stop.is_set():
                self._fetch()


# ── Separate Hue Control Window ──────────────────────────────────────────────

class HueWindow(ctk.CTkToplevel):
    """Standalone window for Hue light control. No flicker, direct widget updates."""

    def __init__(self, plugin):
        super().__init__()
        self.p = plugin
        self.title("Philips Hue")
        self.geometry("420x600")
        self.configure(fg_color=BG)
        self.resizable(True, True)
        self.minsize(350, 300)

        self._group_rows = {}   # gid -> widgets
        self._light_rows = {}   # lid -> widgets
        self._bri_lock = set()  # keys currently being adjusted

        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="Philips Hue", font=("Helvetica", 14, "bold"),
                     text_color=FG).pack(side="left")
        self._status = ctk.CTkLabel(hdr, text="", font=("Helvetica", 10), text_color=FG2)
        self._status.pack(side="right")

        # Scrollable area
        self._scroll = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        self._scroll.pack(fill="both", expand=True, padx=8, pady=4)

        # Cap scroll speed (prevent jumping top/bottom)
        _c = self._scroll._parent_canvas
        _orig_yview = _c.yview
        def _capped_yview(*args):
            if args and args[0] == "scroll":
                n    = max(-2, min(2, int(args[1])))
                what = args[2] if len(args) > 2 else "units"
                return _orig_yview("scroll", n, what)
            return _orig_yview(*args)
        _c.yview = _capped_yview

        # All On / All Off. Only off was offered, which is half a switch (#16).
        row_all = ctk.CTkFrame(self, fg_color="transparent")
        row_all.pack(padx=12, pady=(4, 10), anchor="w")
        ctk.CTkButton(
            row_all, text=plugin.ctx.T("hue_all_on"),
            font=("Helvetica", 11, "bold"),
            fg_color=GRN, hover_color="#15803d", text_color=FG,
            height=32, width=120, corner_radius=4,
            command=lambda: self._all(True)
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            row_all, text=plugin.ctx.T("hue_all_off"),
            font=("Helvetica", 11, "bold"),
            fg_color=RED, hover_color="#b91c1c", text_color=FG,
            height=32, width=120, corner_radius=4,
            command=lambda: self._all(False)
        ).pack(side="left")

        self._build_all()

    def _build_all(self):
        """Full build of all rows."""
        for w in self._scroll.winfo_children():
            w.destroy()
        self._group_rows.clear()
        self._light_rows.clear()

        p = self.p
        self._built_connected = p._connected
        if not p._connected:
            why = getattr(p, "_last_error", "")
            ctk.CTkLabel(self._scroll,
                         text=why or p.ctx.T("hue_no_bridge"),
                         font=("Helvetica", 11), text_color=FG2).pack(pady=(30, 6))
            # A window that only says "not connected" leaves nothing to do
            # but close it and guess (#16). The bridge's own reason is above,
            # and this tries again without a trip back to the settings.
            ctk.CTkButton(self._scroll, text=p.ctx.T("hue_retry"),
                          font=("Helvetica", 11), height=30, width=140,
                          corner_radius=4, command=self._retry).pack(pady=(0, 20))
            self._status.configure(text=p.ctx.T("hue_disconnected"), text_color=RED)
            return

        n = len(p._lights)
        self._status.configure(text=f"\u2022 {n} lights", text_color=GRN)

        # Groups
        if p._groups:
            ctk.CTkLabel(self._scroll, text=p.ctx.T("hue_groups"),
                         font=("Helvetica", 11, "bold"), text_color=FG2
                         ).pack(fill="x", pady=(8, 4), anchor="w")
            for gid in sorted(p._groups, key=lambda g: p._groups[g].get("name", "")):
                self._build_group(gid)

        # Lights
        if p._lights:
            ctk.CTkLabel(self._scroll, text=p.ctx.T("hue_lights"),
                         font=("Helvetica", 11, "bold"), text_color=FG2
                         ).pack(fill="x", pady=(12, 4), anchor="w")
            for lid in sorted(p._lights, key=lambda l: p._lights[l].get("name", "")):
                self._build_light(lid)

        # Scenes
        if p._scenes:
            ctk.CTkLabel(self._scroll, text=p.ctx.T("hue_scenes"),
                         font=("Helvetica", 11, "bold"), text_color=FG2
                         ).pack(fill="x", pady=(12, 4), anchor="w")
            for sid in sorted(p._scenes, key=lambda s: p._scenes[s].get("name", "")):
                self._build_scene(sid)

    def _build_group(self, gid):
        p = self.p
        ginfo = p._groups[gid]
        name = ginfo.get("name", f"Group {gid}")
        action = ginfo.get("action", {})
        is_on = ginfo.get("state", {}).get("any_on", False)

        row = ctk.CTkFrame(self._scroll, fg_color=BG3, corner_radius=4)
        row.pack(fill="x", pady=2)

        rgb = _light_rgb(action) if is_on else (40, 40, 40)
        dot = ctk.CTkLabel(row, text="\u2B24", font=("Helvetica", 14),
                           text_color=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}", width=24)
        dot.pack(side="left", padx=(8, 4), pady=4)

        ctk.CTkLabel(row, text=name, font=("Helvetica", 11), text_color=FG, anchor="w"
                     ).pack(side="left", fill="x", expand=True, padx=4, pady=4)

        bri = action.get("bri", 254) if is_on else 0
        slider = ctk.CTkSlider(row, from_=0, to=254, width=100, height=16,
                               fg_color=BG2, progress_color=_AMBER,
                               button_color=FG, button_hover_color=FG2)
        slider.set(bri)
        slider.pack(side="right", padx=(4, 8), pady=4)

        sc = GRN if is_on else FG2
        btn = ctk.CTkButton(row, text="ON" if is_on else "OFF",
                            font=("Helvetica", 9, "bold"), fg_color=sc,
                            hover_color="#16a34a" if is_on else BORDER,
                            text_color=FG, height=22, width=44, corner_radius=4)
        btn.pack(side="right", padx=4, pady=4)

        # Bind commands after creation (avoids closure issues)
        btn.configure(command=lambda: self._toggle_group(gid))
        slider.configure(command=lambda v, g=gid: self._slider_group(g, int(v)))

        self._group_rows[gid] = {"dot": dot, "btn": btn, "slider": slider, "on": is_on}

    def _build_light(self, lid):
        p = self.p
        linfo = p._lights[lid]
        name = linfo.get("name", f"Light {lid}")
        state = linfo.get("state", {})
        is_on = state.get("on", False)

        row = ctk.CTkFrame(self._scroll, fg_color=BG3, corner_radius=4)
        row.pack(fill="x", pady=2)

        rgb = _light_rgb(state)
        dot = ctk.CTkLabel(row, text="\u2B24", font=("Helvetica", 12),
                           text_color=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}", width=20)
        dot.pack(side="left", padx=(8, 4), pady=4)

        ctk.CTkLabel(row, text=name, font=("Helvetica", 10), text_color=FG, anchor="w"
                     ).pack(side="left", fill="x", expand=True, padx=4, pady=4)

        bri = state.get("bri", 254) if is_on else 0
        slider = ctk.CTkSlider(row, from_=0, to=254, width=80, height=14,
                               fg_color=BG2, progress_color=_AMBER,
                               button_color=FG, button_hover_color=FG2)
        slider.set(bri)
        slider.pack(side="right", padx=(4, 8), pady=4)

        sc = GRN if is_on else FG2
        btn = ctk.CTkButton(row, text="ON" if is_on else "OFF",
                            font=("Helvetica", 9, "bold"), fg_color=sc,
                            hover_color="#16a34a" if is_on else BORDER,
                            text_color=FG, height=22, width=44, corner_radius=4)
        btn.pack(side="right", padx=4, pady=4)

        btn.configure(command=lambda: self._toggle_light(lid))
        slider.configure(command=lambda v, l=lid: self._slider_light(l, int(v)))

        self._light_rows[lid] = {"dot": dot, "btn": btn, "slider": slider, "on": is_on}

    def _build_scene(self, sid):
        p = self.p
        sinfo = p._scenes[sid]
        name = sinfo.get("name", f"Scene {sid}")
        group = sinfo.get("group", "")
        gname = p._groups.get(group, {}).get("name", "")

        row = ctk.CTkFrame(self._scroll, fg_color=BG3, corner_radius=4)
        row.pack(fill="x", pady=2)

        ctk.CTkLabel(row, text="\u2728", font=("Helvetica", 10), width=20
                     ).pack(side="left", padx=(8, 4), pady=4)
        lbl = f"{name}  ({gname})" if gname else name
        ctk.CTkLabel(row, text=lbl, font=("Helvetica", 10), text_color=FG, anchor="w"
                     ).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ctk.CTkButton(row, text="\u25B6", font=("Helvetica", 9, "bold"),
                      fg_color=_PURPLE, hover_color="#7c3aed", text_color=FG,
                      height=22, width=36, corner_radius=4,
                      command=lambda g=group, s=sid: self._scene(g, s)
                      ).pack(side="right", padx=(4, 8), pady=4)

    # ── Refresh from poll (in-place updates) ─────────────────────────────────

    def refresh(self):
        """Called from plugin._on_fetched on GUI thread."""
        p = self.p

        # Structure changed? Full rebuild. So does a change of connection, and
        # so does staying disconnected: the reason and the retry button are
        # built by _build_all, and while that view is up both row sets are
        # empty and match a bridge that has answered nothing, so comparing
        # them alone never noticed and Try again changed nothing (#16).
        if (not p._connected
                or getattr(self, "_built_connected", None) != p._connected
                or set(self._group_rows) != set(p._groups)
                or set(self._light_rows) != set(p._lights)):
            self._build_all()
            return

        # Update groups in-place
        for gid, w in self._group_rows.items():
            ginfo = p._groups.get(gid, {})
            action = ginfo.get("action", {})
            is_on = ginfo.get("state", {}).get("any_on", False)
            rgb = _light_rgb(action) if is_on else (40, 40, 40)
            w["dot"].configure(text_color=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")

            if w["on"] != is_on:
                sc = GRN if is_on else FG2
                w["btn"].configure(text="ON" if is_on else "OFF", fg_color=sc,
                                   hover_color="#16a34a" if is_on else BORDER)
                w["on"] = is_on

            if f"group:{gid}" not in self._bri_lock:
                bri = action.get("bri", 254) if is_on else 0
                w["slider"].set(bri)

        # Update lights in-place
        for lid, w in self._light_rows.items():
            state = p._lights.get(lid, {}).get("state", {})
            is_on = state.get("on", False)
            rgb = _light_rgb(state)
            w["dot"].configure(text_color=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")

            if w["on"] != is_on:
                sc = GRN if is_on else FG2
                w["btn"].configure(text="ON" if is_on else "OFF", fg_color=sc,
                                   hover_color="#16a34a" if is_on else BORDER)
                w["on"] = is_on

            if f"light:{lid}" not in self._bri_lock:
                bri = state.get("bri", 254) if is_on else 0
                w["slider"].set(bri)

    # ── Controls ─────────────────────────────────────────────────────────────

    def _toggle_light(self, lid):
        p = self.p
        w = self._light_rows.get(lid)
        if not w:
            return
        new_on = not w["on"]
        # Optimistic UI
        sc = GRN if new_on else FG2
        w["btn"].configure(text="ON" if new_on else "OFF", fg_color=sc,
                           hover_color="#16a34a" if new_on else BORDER)
        w["on"] = new_on
        def _do():
            _hue("PUT", p._bridge_ip, p._api_key, f"lights/{lid}/state", {"on": new_on})
        threading.Thread(target=_do, daemon=True).start()

    def _toggle_group(self, gid):
        p = self.p
        w = self._group_rows.get(gid)
        if not w:
            return
        new_on = not w["on"]
        sc = GRN if new_on else FG2
        w["btn"].configure(text="ON" if new_on else "OFF", fg_color=sc,
                           hover_color="#16a34a" if new_on else BORDER)
        w["on"] = new_on
        def _do():
            _hue("PUT", p._bridge_ip, p._api_key, f"groups/{gid}/action", {"on": new_on})
        threading.Thread(target=_do, daemon=True).start()

    def _slider_light(self, lid, bri):
        """Directly send brightness — no debounce, Hue bridge handles it."""
        key = f"light:{lid}"
        self._bri_lock.add(key)
        p = self.p
        data = {"bri": bri, "on": True} if bri > 0 else {"on": False}
        threading.Thread(target=lambda: _hue("PUT", p._bridge_ip, p._api_key,
                                              f"lights/{lid}/state", data),
                         daemon=True).start()
        # Unlock after 3s so poll can update again
        self.after(3000, lambda: self._bri_lock.discard(key))

    def _slider_group(self, gid, bri):
        key = f"group:{gid}"
        self._bri_lock.add(key)
        p = self.p
        data = {"bri": bri, "on": True} if bri > 0 else {"on": False}
        threading.Thread(target=lambda: _hue("PUT", p._bridge_ip, p._api_key,
                                              f"groups/{gid}/action", data),
                         daemon=True).start()
        self.after(3000, lambda: self._bri_lock.discard(key))

    def _scene(self, gid, sid):
        p = self.p
        threading.Thread(target=lambda: _hue("PUT", p._bridge_ip, p._api_key,
                                              f"groups/{gid}/action", {"scene": sid}),
                         daemon=True).start()

    def _retry(self):
        """Ask the bridge again, from the window that said it did not answer."""
        p = self.p
        threading.Thread(target=lambda: p._fetch(include_scenes=True),
                         daemon=True).start()

    def _all(self, on):
        """Every light at once. Shown straight away, then confirmed.

        The screen used to be set and left that way whatever the bridge did
        with the request, so a command that never arrived left every row
        reading OFF.
        """
        label, colour = ("ON", GRN) if on else ("OFF", FG2)
        hover = "#16a34a" if on else BORDER
        for w in list(self._group_rows.values()) + list(self._light_rows.values()):
            w["btn"].configure(text=label, fg_color=colour, hover_color=hover)
            w["dot"].configure(text_color=colour if on else "#282828")
            w["on"] = on
        p = self.p

        def send():
            _hue("PUT", p._bridge_ip, p._api_key, "groups/0/action", {"on": on})
            p._fetch()      # whatever really happened is what the rows show

        threading.Thread(target=send, daemon=True).start()
