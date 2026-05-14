## Snippets — text expander for DisplayPad

Bind any DisplayPad button to the **Snippet** action type and the plugin will
type out a multi-line text snippet whenever you press the key.

### Setup

1. Install the plugin via the Plugin Manager in BaseCamp.
2. Open the **Snippets** tab and click *+ Add snippet*.
3. Give it a label (anything, optional) and type the text you want.
4. Note the slot number shown at the left of the row (`#1`, `#2`, …).
5. Open the DisplayPad panel, pick a button, set its action type to
   **Snippet (by slot)** and put the slot number as the action value.

Press the key and the snippet is typed into whichever window is currently
focused, via `xdotool` (X11) or `ydotool` (Wayland).

### Placeholders

These tokens are expanded inside the snippet text just before typing:

| Token         | Becomes                                            |
|---------------|----------------------------------------------------|
| `{date}`      | `2026-05-14`                                       |
| `{time}`      | `14:23`                                            |
| `{datetime}`  | `2026-05-14 14:23`                                 |
| `{clipboard}` | current clipboard content (via wl-paste / xclip)   |
| `{cursor}`    | cursor lands here after typing (Left-arrow keys)   |

Example — a quick reply template:

```
Hi {clipboard},

quick update on {date}: {cursor}

Best,
Rami
```

After typing, the cursor stops right after "update on 2026-05-14:", so you
just keep typing the actual update.

### Test button

Each row has a *Test (1.5 s)* button. Click it, then switch to the window you
want to type into — after the countdown the snippet is typed there so you
can verify formatting without binding a DisplayPad key first.

### Notes

- Requires `xdotool` (X11) or `ydotool` (Wayland) — same dependency BaseCamp
  already needs for macro actions.
- Storage is local, in `~/.config/mountain-time-sync/plugins/snippets/config.json`.
- Use counters live next to each snippet so you can spot the heavy hitters.
