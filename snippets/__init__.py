"""Snippets — type frequently-used text via DisplayPad buttons.

Each snippet lives in a numbered slot. To use one, bind a DisplayPad button
to the 'Snippet' action type and enter the slot number (1, 2, 3, …) as the
action value.

Placeholders inside the snippet text are expanded just before typing:
  {date}      → 2026-05-14
  {time}      → 14:23
  {datetime}  → 2026-05-14 14:23
  {clipboard} → current clipboard content (wl-paste / xclip)
  {cursor}    → cursor stops here after typing (sends Left-arrow keypresses)
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import customtkinter as ctk

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
    """Substitute placeholders. Returns (expanded_text, cursor_offset_from_end)."""
    now = datetime.now()
    text = text.replace("{date}", now.strftime("%Y-%m-%d"))
    text = text.replace("{time}", now.strftime("%H:%M"))
    text = text.replace("{datetime}", now.strftime("%Y-%m-%d %H:%M"))
    if "{clipboard}" in text:
        text = text.replace("{clipboard}", _get_clipboard())

    cursor_offset = 0
    if "{cursor}" in text:
        idx = text.index("{cursor}")
        text = text.replace("{cursor}", "", 1)
        cursor_offset = len(text) - idx
    return text, cursor_offset


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

        expanded, cursor_offset = _expand(text)
        try:
            from shared.macros import simulate_text, simulate_keypress
        except ImportError:
            return

        simulate_text(expanded)
        for _ in range(cursor_offset):
            simulate_keypress("left")

        snip["uses"] = int(snip.get("uses", 0)) + 1
        self._save()

        try:
            self.ctx.schedule(0, self._refresh_use_badges)
        except Exception:
            pass

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
