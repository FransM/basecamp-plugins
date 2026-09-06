#!/usr/bin/env python3
"""
Checks how the widget plugins write their frames to disk.

    python3 test_frame_writing.py            # from this directory

Two things are pinned here, both reported against the DisplayPad:

  #88  a frame file named after the key index alone is shared by every page,
       so the same widget on the same key of two pages overwrote itself and
       one of the pages showed the other's picture
  #89  a frame written straight onto its destination can be read half
       written by the upload worker, which reports "cannot identify image
       file"

It needs the main application on the import path, because the plugins ask it
for CONFIG_DIR, and Pillow. No hardware and no display: the plugin is driven
with a stand-in context.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("BASECAMP_APP", os.path.expanduser("~/mountain-time-sync"))
if not os.path.isdir(APP):
    print("main application not found at %s, set BASECAMP_APP" % APP)
    sys.exit(0)
sys.path.insert(0, APP)

_TMP_HOME = tempfile.mkdtemp(prefix="basecamp-plugin-test-")
os.environ["HOME"] = _TMP_HOME

try:
    from PIL import Image
except ImportError:
    print("Pillow not installed, skipping")
    sys.exit(0)

from shared.config import CONFIG_DIR   # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + "%-52s %s" % (name, detail))
    if not ok:
        failures.append("%s: %s" % (name, detail))


def load(plugin):
    """Import one plugin package by path, without installing it."""
    spec = importlib.util.spec_from_file_location(
        "plugin_" + plugin, os.path.join(HERE, plugin, "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakePad:
    def __init__(self):
        self._images = {}
        self._page_images = {0: {}}


class FakeCtx:
    """The parts of the plugin context these plugins actually use."""

    def __init__(self, pad, actions, page):
        self._pad = pad
        self._actions = actions
        self._page = page
        self.pushed = []

    def get_displaypad(self):
        return self._pad

    def get_displaypad_current_page(self):
        return self._page

    def get_displaypad_actions(self, page=None):
        return self._actions

    def push_displaypad_image(self, key, img):
        self.pushed.append(key)

    def register_translations(self, _t):
        pass

    def register_action_type(self, *_a, **_kw):
        pass

    def T(self, key, **_kw):
        return key

    def load_plugin_config(self, *_a, **_kw):
        return {}

    def save_plugin_config(self, *_a, **_kw):
        pass

    def __getattr__(self, name):
        # Anything else a plugin reaches for while it is being constructed.
        return lambda *a, **kw: None


# plugin, action type, action value, file name prefix
WIDGETS = (
    ("dp_clock", "clock_display", "", "dp_clock"),
    ("system_monitor", "mon_cpu", "", "dp_mon"),
    ("hue_control", "hue_toggle", "light:1", "dp_hue"),
    ("snippets", "snippet", "1", "dp_snippet"),
)


def render_once(plugin, plug):
    """Ask a plugin to paint its keys, whatever it calls that."""
    for name in ("_update", "_update_displaypad", "_draw_keys"):
        method = getattr(plug, name, None)
        if method is not None:
            return method()
    raise AttributeError("%s has no way to render a key" % plugin)

# ── The same widget on the same key of two pages ─────────────────────────────
for plugin, action_type, action_value, prefix in WIDGETS:
    module = load(plugin)
    written = {}
    for page in (0, 2):
        pad = FakePad()
        actions = [{"type": "none", "action": ""} for _ in range(12)]
        actions[3] = {"type": action_type, "action": action_value}
        ctx = FakeCtx(pad, actions, page)
        plug = module.Plugin(ctx)
        if plugin == "hue_control":
            # It renders nothing for a light it has never heard of, and it
            # only hears about lights from a bridge.
            plug._lights = {"1": {"name": "Desk", "state": {"on": True}}}
        if plugin == "snippets":
            # Same for a slot that holds nothing.
            plug._snippets = [{"label": "Greeting", "text": "kind regards,\nFrans"}]
        try:
            render_once(plugin, plug)
        except Exception as exc:
            check("%s renders a frame" % plugin, False, repr(exc))
            break
        files = sorted(f for f in os.listdir(CONFIG_DIR)
                       if f.startswith(prefix) and f.endswith(".png"))
        written[page] = files
        check("%s puts the page in the file name (page %d)" % (plugin, page),
              any("_p%d_k3" % page in f for f in files), files[-3:])
        check("%s registers the frame under that page" % plugin,
              page in pad._page_images and "3" in pad._page_images[page],
              sorted(pad._page_images))
    if 0 in written and 2 in written:
        check("%s does not overwrite the other page's file" % plugin,
              len(set(written[0]) | set(written[2])) >= 2,
              sorted(set(written[0]) | set(written[2]))[:4])

# ── Nothing may go back to a page-less name ──────────────────────────────────
# dp_video and dp_pipe_text render from a source this test cannot stand in for,
# so their names are checked in the source instead of by running them.
import re   # noqa: E402

for plugin in ("dp_clock", "system_monitor", "hue_control", "dp_video",
               "dp_pipe_text", "snippets"):
    text = open(os.path.join(HERE, plugin, "__init__.py"), encoding="utf-8").read()
    names = re.findall(r'f"(dp_[a-z_]*?_(?:p\{page\}_k)?\{[a-z_]+\}\.png)"', text)
    pageless = [n for n in names if "_p{page}_k" not in n]
    check("%s names every frame file after its page" % plugin,
          names and not pageless, pageless or names)
    check("%s writes its frames through _save_frame" % plugin,
          "_save_frame(img" in text and "img.save(img_path)" not in text)

# ── The write itself ─────────────────────────────────────────────────────────
# A reader must never catch a half written file. Drive _save_frame while a
# second thread reads the destination as fast as it can.
module = load("dp_clock")
target = os.path.join(CONFIG_DIR, "atomic_check.png")
stop = threading.Event()
torn = []


def reader():
    while not stop.is_set():
        try:
            with Image.open(target) as im:
                im.load()
        except FileNotFoundError:
            pass
        except Exception as exc:
            torn.append(repr(exc))
            return


watcher = threading.Thread(target=reader, daemon=True)
watcher.start()
big = Image.new("RGB", (102, 102))
for x in range(102):
    for y in range(102):
        big.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256))
for _ in range(80):
    module._save_frame(big, target)
stop.set()
watcher.join(timeout=5)
check("a reader never catches a half written frame", not torn, torn[:1])

leftovers = [f for f in os.listdir(CONFIG_DIR) if f.endswith(".tmp")]
check("and no temporary file is left behind", not leftovers, leftovers[:3])

# ── The page helper ──────────────────────────────────────────────────────────
check("the page comes from the context",
      module._current_page(FakeCtx(FakePad(), [], 7)) == 7)


class NoPage:
    def get_displaypad_current_page(self):
        raise RuntimeError("older host application")


check("and falls back to Main when it cannot be asked",
      module._current_page(NoPage()) == 0)

shutil.rmtree(_TMP_HOME, ignore_errors=True)

print()
if failures:
    print("%d check(s) failed:" % len(failures))
    for failure in failures:
        print("  - %s" % failure)
    sys.exit(1)
print("all checks passed")
