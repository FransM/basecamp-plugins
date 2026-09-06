"""Snippets — type frequently-used text via DisplayPad buttons.

Each snippet lives in a numbered slot. To use one, bind a DisplayPad button
to the 'Snippet' action type and enter the slot number (1, 2, 3, …) as the
action value.

Placeholders inside the snippet text are expanded just before typing:
  {date}      → 2026-05-14
  {time}      → 14:23
  {datetime}  → 2026-05-14 14:23
  {clipboard} → current clipboard content (wl-paste / xclip)
  {cursor}    → cursor stops here after typing (walks back with arrow keys)
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

_TILE = 102     # a DisplayPad key

try:
    from shared.ui_helpers import BG, BG2, BG3, FG, FG2, BLUE, GRN, RED, YLW, BORDER
except ImportError:
    BG, BG2, BG3 = "#0e0e1a", "#16162a", "#222244"
    FG, FG2 = "#e0e0e0", "#707090"
    BLUE, GRN, RED, YLW = "#0ea5e9", "#22c55e", "#dc2626", "#f5c542"
    BORDER = "#2a2a4a"


def _get_clipboard():
    """Read the clipboard via wl-paste (Wayland) or xclip (X11). Empty on failure."""
    for cmd in (["wl-paste", "--no-newline"],
                ["xclip", "-selection", "clipboard", "-o"]):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=1)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", errors="replace")
        except Exception:
            continue
    return ""


def _expand(text):
    """Substitute placeholders. Returns (expanded_text, keys_back_to_cursor).

    The second value is the keys to press after the text has been typed, to
    put the cursor where {cursor} was.
    """
    now = datetime.now()
    text = text.replace("{date}", now.strftime("%Y-%m-%d"))
    text = text.replace("{time}", now.strftime("%H:%M"))
    text = text.replace("{datetime}", now.strftime("%Y-%m-%d %H:%M"))
    if "{clipboard}" in text:
        text = text.replace("{clipboard}", _get_clipboard())

    if "{cursor}" not in text:
        return text, []
    idx = text.index("{cursor}")
    text = text.replace("{cursor}", "", 1)
    return text, _keys_back_to(text, idx)


def _keys_back_to(text, idx):
    """The keys that walk the cursor from the end of `text` back to `idx`.

    Left presses alone are what this used to send, and that only works while
    the way back stays on one line: most editors do not wrap a Left at the
    start of a line round to the end of the one above, so in vi a snippet
    whose {cursor} sat above the last line left the cursor sitting at the
    start of the last line instead (#15).

    With a line to climb, the route is Home to leave the column behind, Up
    once per line, Home again because Up keeps a column of its own, and then
    Right to the target column. On one line it stays with Left, which needs
    no Home and so cannot be thrown off by an editor that wraps long lines
    visually.
    """
    tail = text[idx:]
    lines_up = tail.count("\n")
    if lines_up == 0:
        return ["left"] * len(tail)
    line_start = text.rfind("\n", 0, idx) + 1
    column = idx - line_start
    return ["home"] + ["up"] * lines_up + ["home"] + ["right"] * column


_FONTS = {}


def _font(size):
    """A face at this size, loaded once. Kept because the drawing below asks
    for one per line and a truetype load is a file read every time."""
    if size not in _FONTS:
        _FONTS[size] = ImageFont.load_default()
        for path in ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/TTF/DejaVuSans.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
            if os.path.exists(path):
                try:
                    _FONTS[size] = ImageFont.truetype(path, size)
                except Exception:
                    pass
                break
    return _FONTS[size]


def _render_snippet(slot, label, preview):
    """The key's picture: its slot number, its name, and the text beneath.

    A snippet key had no picture at all, on the pad or in the editor, so
    twelve of them were twelve blank keys (#15).
    """
    img = Image.new("RGB", (_TILE, _TILE), (18, 18, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, _TILE - 1, 13], fill=(14, 165, 233))
    draw.text((4, 1), "#%s" % slot, fill=(10, 10, 20), font=_font(11))

    # A snippet with a name is shown by it, with the text underneath. One
    # without falls back to its own first line, which is all there is to say.
    first_line = next((l for l in preview.splitlines() if l.strip()), "")
    title = label.strip() or first_line.strip()

    y = 20
    for line in _wrap(draw, title, _font(13), 3 if label.strip() else 5):
        draw.text((5, y), line, fill=(230, 230, 240), font=_font(13))
        y += 15

    if label.strip():
        y += 4
        for line in _wrap(draw, " ".join(preview.split()), _font(10), 3):
            if y > _TILE - 12:
                break
            draw.text((5, y), line, fill=(120, 120, 150), font=_font(10))
            y += 12
    return img


def _wrap(draw, text, font, max_lines):
    """Break text into lines that fit the key. What did not fit ends in a dot."""
    lines, line, dropped = [], "", False
    for word in text.split():
        candidate = ("%s %s" % (line, word)).strip()
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] > _TILE - 10 and line:
            lines.append(line)
            line = word
            if len(lines) == max_lines:
                # This word and everything after it has nowhere to go.
                dropped = True
                break
        else:
            line = candidate
    if line and len(lines) < max_lines:
        lines.append(line)
    elif line:
        dropped = True
    if dropped and lines:
        lines[-1] = lines[-1][:-1] + "\u2026" if len(lines[-1]) > 1 else "\u2026"
    return lines


def _save_frame(img, path):
    """Write the frame beside its destination and move it into place.

    The upload worker reads these files while we write them, and a PIL save
    straight onto the destination is not atomic (#89).
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    img.save(tmp, "PNG")            # named, the extension here is .tmp
    os.replace(tmp, path)


def _current_page(ctx):
    """The page that is on the pad right now, 0 if it cannot be asked."""
    try:
        return int(ctx.get_displaypad_current_page())
    except Exception:
        return 0


class Plugin:
    panel_id = "snippets"
    panel_label = "Snippets"

    def __init__(self, ctx):
        self.ctx = ctx
        self._snippets = []           # [{"label": "...", "text": "...", "uses": 0}, ...]
        self._rows = []               # widget refs per visible row
        self._frame = None
        self._scroll = None
        self._save_lock = threading.Lock()
        self._stop = threading.Event()
        self._drawn = {}              # key index -> what is on it already
        self._load()

        ctx.register_translations({
            "en": {
                "snip_title":   "Snippets",
                "snip_hint":    ("Bind a DisplayPad button to the Snippet action type, then enter "
                                 "the slot number (1, 2, 3, ...) as the action value. Placeholders "
                                 "in the text are expanded at type-time: {date} {time} {datetime} "
                                 "{clipboard} {cursor}."),
                "snip_action":  "Snippet (by slot)",
                "snip_add":     "+ Add snippet",
                "snip_label":   "Label (optional)",
                "snip_text":    "Snippet text",
                "snip_uses":    "uses",
                "snip_test":    "Test (1.5s)",
                "snip_delete":  "Delete",
                "snip_empty":   "No snippets yet — click \"+ Add snippet\" to start.",
                "snip_testing": "Switch to target window… typing in {sec}s",
            },
            "de": {
                "snip_title":   "Snippets",
                "snip_hint":    ("DisplayPad-Taste auf die Snippet-Aktion legen und die Slot-Nummer "
                                 "(1, 2, 3, ...) als Aktionswert eintragen. Platzhalter im Text werden "
                                 "beim Tippen ersetzt: {date} {time} {datetime} {clipboard} {cursor}."),
                "snip_action":  "Snippet (per Slot)",
                "snip_add":     "+ Snippet hinzufügen",
                "snip_label":   "Bezeichnung (optional)",
                "snip_text":    "Snippet-Text",
                "snip_uses":    "Nutzungen",
                "snip_test":    "Test (1,5 s)",
                "snip_delete":  "Löschen",
                "snip_empty":   "Noch keine Snippets — klick \"+ Snippet hinzufügen\".",
                "snip_testing": "Wechsle ins Zielfenster… tippt in {sec}s",
            },
        })

        ctx.register_action_type("snippet", ctx.T("snip_action"), self._on_press)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        data = self.ctx.load_plugin_config("snippets")
        self._snippets = data.get("snippets", []) if isinstance(data, dict) else []

    def _save(self):
        with self._save_lock:
            self.ctx.save_plugin_config("snippets", {"snippets": self._snippets})

    # ── Action handler ────────────────────────────────────────────────────────

    def _on_press(self, action_value):
        """Type the snippet at the given slot (1-based)."""
        try:
            idx = int(str(action_value).strip()) - 1
        except (ValueError, TypeError):
            return
        if idx < 0 or idx >= len(self._snippets):
            return

        snip = self._snippets[idx]
        text = snip.get("text", "")
        if not text:
            return

        expanded, keys_back = _expand(text)
        try:
            from shared.macros import simulate_text, simulate_keypress
        except ImportError:
            return

        simulate_text(expanded)
        for key in keys_back:
            simulate_keypress(key)

        snip["uses"] = int(snip.get("uses", 0)) + 1
        self._save()

        try:
            self.ctx.schedule(0, self._refresh_use_badges)
        except Exception:
            pass

    # ── The picture on the key ────────────────────────────────────────────────

    def start(self):
        """Paint the keys this plugin is on, and keep them painted.

        A snippet key had no picture at all: the pad showed whatever the key
        held before and the editor showed the plugin placeholder, so a page of
        snippets was a page of keys you had to remember (#15). Started and
        stopped with the page, the way the other widget plugins are.

        Each thread carries its own stop, so a stopped one can never be
        revived: clearing one shared event would let a predecessor that had
        not reached its next check yet carry on beside the new thread, and
        two of them painting the same keys is two of everything. Editing a
        key now brings the services in line straight away, so a stop followed
        closely by a start is an ordinary thing rather than a rarity.
        """
        self._stop.set()                # any predecessor ends here
        stop = threading.Event()
        self._stop = stop
        self._drawn.clear()
        threading.Thread(target=self._draw_loop, args=(stop,),
                         daemon=True).start()

    def stop(self):
        self._stop.set()

    def _draw_loop(self, stop):
        while not stop.is_set():
            try:
                self._draw_keys()
            except Exception as e:
                # A transient failure must never end this thread, or the keys
                # stay as they are until the plugin is disabled and enabled.
                print(f"[snippets] draw error (continuing): {e}", flush=True)
            stop.wait(2)

    def _current_actions(self):
        """The 12 actions of the page on the pad, not of the main page (#82)."""
        try:
            return self.ctx.get_displaypad_actions()
        except Exception:
            return []

    def _draw_keys(self):
        try:
            from shared.config import CONFIG_DIR
        except ImportError:
            return
        dp = self.ctx.get_displaypad()
        page = _current_page(self.ctx)
        for i, act in enumerate(self._current_actions()):
            if act.get("type") != "snippet":
                continue
            try:
                slot = int(str(act.get("action", "")).strip())
            except (TypeError, ValueError):
                continue
            snip = self._snippets[slot - 1] if 1 <= slot <= len(self._snippets) else None
            if snip is None:
                continue
            label = str(snip.get("label", ""))
            text = str(snip.get("text", ""))
            # Nothing to send while nothing about the key has changed: this
            # loop runs every two seconds and the text rarely does.
            state = (slot, label, text, page)
            if self._drawn.get(i) == state:
                continue
            self._drawn[i] = state
            img = _render_snippet(slot, label, text)
            path = os.path.join(CONFIG_DIR, f"dp_snippet_p{page}_k{i}.png")
            _save_frame(img, path)
            if dp is not None:
                dp._images[str(i)] = path
                if hasattr(dp, "_page_images"):
                    dp._page_images.setdefault(page, {})[str(i)] = path
            self.ctx.push_displaypad_image(i, img)

    # ── Panel UI ──────────────────────────────────────────────────────────────

    def create_panel(self, parent):
        self._frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)

        # Header
        hdr = ctk.CTkFrame(self._frame, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(hdr, text=self.ctx.T("snip_title"),
                     font=("Helvetica", 14, "bold"),
                     text_color=BLUE).pack(side="left")
        ctk.CTkButton(hdr, text=self.ctx.T("snip_add"),
                      font=("Helvetica", 11, "bold"),
                      fg_color=BLUE, hover_color="#0884be",
                      height=28, width=150,
                      command=self._add_snippet).pack(side="right")

        # Hint
        ctk.CTkLabel(self._frame, text=self.ctx.T("snip_hint"),
                     font=("Helvetica", 10), text_color=FG2,
                     wraplength=620, justify="left").pack(
                     fill="x", padx=16, pady=(0, 6))

        # Status line (used by Test button countdown)
        self._status_lbl = ctk.CTkLabel(self._frame, text="",
                                         font=("Helvetica", 10),
                                         text_color=YLW)
        self._status_lbl.pack(fill="x", padx=16)

        # Scrollable rows
        self._scroll = ctk.CTkScrollableFrame(self._frame, fg_color=BG)
        self._scroll.pack(fill="both", expand=True, padx=16, pady=(4, 14))

        self._rebuild_rows()
        return self._frame

    def _rebuild_rows(self):
        for row in self._rows:
            try:
                row["frame"].destroy()
            except Exception:
                pass
        self._rows = []

        if not self._snippets:
            empty = ctk.CTkLabel(self._scroll,
                                  text=self.ctx.T("snip_empty"),
                                  font=("Helvetica", 11),
                                  text_color=FG2)
            empty.pack(pady=30)
            self._rows.append({"frame": empty})
            return

        for i, snip in enumerate(self._snippets):
            self._build_row(i, snip)

    def _build_row(self, i, snip):
        row = ctk.CTkFrame(self._scroll, fg_color=BG2, corner_radius=8)
        row.pack(fill="x", pady=4)

        # Top: slot # | label | uses badge
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))

        ctk.CTkLabel(top, text=f"#{i+1}",
                     font=("Helvetica", 12, "bold"),
                     text_color=BLUE, width=30).pack(side="left")

        label_var = ctk.StringVar(value=snip.get("label", ""))
        label_entry = ctk.CTkEntry(top, textvariable=label_var,
                                    placeholder_text=self.ctx.T("snip_label"),
                                    fg_color=BG3, border_color=BORDER, height=26,
                                    font=("Helvetica", 11))
        label_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        label_entry.bind("<FocusOut>",
                         lambda e, idx=i, v=label_var: self._on_label_change(idx, v.get()))

        uses_lbl = ctk.CTkLabel(top,
                                 text=f"{snip.get('uses', 0)} {self.ctx.T('snip_uses')}",
                                 font=("Helvetica", 9),
                                 text_color=FG2, width=80, anchor="e")
        uses_lbl.pack(side="left", padx=(0, 6))

        # Multi-line text area
        text_widget = ctk.CTkTextbox(row, height=80, fg_color=BG3,
                                      border_color=BORDER, border_width=1,
                                      font=("DejaVu Sans Mono", 11),
                                      text_color=FG, wrap="word")
        text_widget.pack(fill="x", padx=10, pady=(2, 4))
        text_widget.insert("1.0", snip.get("text", ""))
        text_widget.bind("<FocusOut>",
                         lambda e, idx=i, w=text_widget: self._on_text_change(
                             idx, w.get("1.0", "end-1c")))

        # Bottom: Test (left) + Delete (right)
        bot = ctk.CTkFrame(row, fg_color="transparent")
        bot.pack(fill="x", padx=10, pady=(0, 8))

        test_btn = ctk.CTkButton(bot, text=self.ctx.T("snip_test"),
                                  font=("Helvetica", 10, "bold"),
                                  fg_color=BG3, hover_color="#3a3a5a",
                                  height=24, width=90,
                                  command=lambda idx=i: self._test_snippet(idx))
        test_btn.pack(side="left")

        ctk.CTkButton(bot, text=self.ctx.T("snip_delete"),
                       font=("Helvetica", 10, "bold"),
                       fg_color=BG3, hover_color=RED, text_color=RED,
                       height=24, width=80,
                       command=lambda idx=i: self._delete_snippet(idx)).pack(side="right")

        self._rows.append({
            "frame": row, "label_var": label_var,
            "text_widget": text_widget, "uses_lbl": uses_lbl,
            "test_btn": test_btn,
        })

    # ── Edit handlers ─────────────────────────────────────────────────────────

    def _on_label_change(self, idx, value):
        if 0 <= idx < len(self._snippets):
            if self._snippets[idx].get("label", "") != value.strip():
                self._snippets[idx]["label"] = value.strip()
                self._save()

    def _on_text_change(self, idx, value):
        if 0 <= idx < len(self._snippets):
            if self._snippets[idx].get("text", "") != value:
                self._snippets[idx]["text"] = value
                self._save()

    def _add_snippet(self):
        # Flush any pending edits first
        self._commit_visible_edits()
        self._snippets.append({"label": "", "text": "", "uses": 0})
        self._save()
        self._rebuild_rows()

    def _delete_snippet(self, idx):
        if 0 <= idx < len(self._snippets):
            self._commit_visible_edits()
            self._snippets.pop(idx)
            self._save()
            self._rebuild_rows()

    def _commit_visible_edits(self):
        """Pull current widget contents into the data model. FocusOut might not
        have fired if the user hits Add/Delete while still in an entry."""
        for i, row in enumerate(self._rows):
            if i >= len(self._snippets):
                continue
            if "label_var" in row:
                self._snippets[i]["label"] = row["label_var"].get().strip()
            if "text_widget" in row:
                try:
                    self._snippets[i]["text"] = row["text_widget"].get("1.0", "end-1c")
                except Exception:
                    pass

    # ── Test typing ───────────────────────────────────────────────────────────

    def _test_snippet(self, idx):
        if not (0 <= idx < len(self._snippets)):
            return
        # Make sure any unsaved text edits are part of the test
        self._commit_visible_edits()

        def _countdown():
            for sec in (2, 1):
                try:
                    self.ctx.schedule(0, lambda s=sec: self._status_lbl.configure(
                        text=self.ctx.T("snip_testing", sec=s)))
                except Exception:
                    pass
                time.sleep(0.75)
            try:
                self.ctx.schedule(0, lambda: self._status_lbl.configure(text=""))
            except Exception:
                pass
            self._on_press(str(idx + 1))

        threading.Thread(target=_countdown, daemon=True).start()

    def _refresh_use_badges(self):
        for i, row in enumerate(self._rows):
            if i >= len(self._snippets):
                continue
            if "uses_lbl" in row:
                try:
                    row["uses_lbl"].configure(
                        text=f"{self._snippets[i].get('uses', 0)} {self.ctx.T('snip_uses')}")
                except Exception:
                    pass
