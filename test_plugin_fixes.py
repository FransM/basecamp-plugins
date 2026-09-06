#!/usr/bin/env python3
"""
Checks the two things @FransM reported in issues #15 and #16.

    python3 test_plugin_fixes.py             # from this directory

#15  A snippet's {cursor} walked back with Left presses only, and most
     editors do not wrap a Left at the start of a line round to the end of
     the one above, so in vi the cursor stopped at the start of the last
     line instead of where it was asked to.

#16  The Hue bridge says no with HTTP 200 and a list of error objects. Every
     caller here only checked whether it had a dict, so a key the bridge had
     forgotten looked exactly like a bridge that was switched off, and both
     came out as a bare "Not connected" with nothing to do about it.

No bridge, no pad, no editor: both are pure functions here.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("BASECAMP_APP", os.path.expanduser("~/mountain-time-sync"))
if not os.path.isdir(APP):
    print("main application not found at %s, set BASECAMP_APP" % APP)
    sys.exit(0)
sys.path.insert(0, APP)
os.environ["HOME"] = tempfile.mkdtemp(prefix="basecamp-plugin-fixes-")

failures = []


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + "%-54s %s" % (name, detail))
    if not ok:
        failures.append("%s: %s" % (name, detail))


def load(plugin):
    spec = importlib.util.spec_from_file_location(
        "plugin_" + plugin, os.path.join(HERE, plugin, "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── #15: walking back to {cursor} ────────────────────────────────────────────
sn = load("snippets")


def keys_for(text):
    """Where the cursor ends up, described as the keys that get it there."""
    return sn._expand(text)[1]


check("no marker, no keys back", keys_for("plain text") == [])

check("a marker on the one line still walks left",
      keys_for("Hello {cursor}world") == ["left"] * len("world"),
      keys_for("Hello {cursor}world"))

check("a marker at the very end needs nothing",
      keys_for("Hello{cursor}") == [], keys_for("Hello{cursor}"))

# FransM's own snippet: the marker on the first line, three lines under it.
got = keys_for("{cursor}\n\nRegards,\nFrans")
check("three lines below, the route climbs instead of walking left",
      got == ["home", "up", "up", "up", "home"], got)
check("and not a single Left, which is what failed in vi",
      "left" not in got, got)

# His second example, where the target is inside a line rather than at its
# start: two lines up, then along to the column.
got = keys_for("some text {cursor}here\nhi\nbyebye")
check("a column inside the line above is reached with Right",
      got == ["home", "up", "up", "home"] + ["right"] * len("some text "), got)

check("one line below is still a climb, not 200 lefts",
      keys_for("a{cursor}b\n" + "x" * 200)
      == ["home", "up", "home", "right"],
      keys_for("a{cursor}b\n" + "x" * 200))

# ── #16: what the bridge said ────────────────────────────────────────────────
hue = load("hue_control")

check("a bridge that answered properly did not refuse",
      hue._hue_error({"1": {"name": "Desk"}}) is None)

check("a forgotten key comes back as its own reason",
      hue._hue_error([{"error": {"type": 1, "description": "unauthorized user"}}])
      == "unauthorized user",
      hue._hue_error([{"error": {"type": 1, "description": "unauthorized user"}}]))

check("no answer at all is not a refusal", hue._hue_error(None) is None)

check("a refusal with no description still says something",
      isinstance(hue._hue_error([{"error": {"type": 3}}]), str),
      hue._hue_error([{"error": {"type": 3}}]))

check("a list that holds no error is not a refusal",
      hue._hue_error([{"success": {"/lights/1/state/on": True}}]) is None)

check("a refusal is described in words, not as a dict dump",
      "{" not in hue._hue_error([{"error": {"type": 3}}]),
      hue._hue_error([{"error": {"type": 3}}]))


# The fetch has to come out through the one door that refreshes the screen,
# and it has to keep the groups on the way. Both were mine to get wrong: the
# reason is gathered in the branch that used to return on the spot, and the
# group assignment sat behind that return where nothing reached it.
class FetchCtx:
    def __init__(self):
        self.scheduled = 0

    def schedule(self, _ms, callback):
        self.scheduled += 1

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def fetch_with(answers):
    """Run _fetch against a bridge that answers like this."""
    import threading as _t
    plug = hue.Plugin.__new__(hue.Plugin)
    plug.ctx = FetchCtx()
    plug._lock = _t.RLock()
    plug._bridge_ip, plug._api_key = "10.0.0.2", "key"
    plug._lights, plug._groups, plug._scenes = {}, {}, {}
    plug._connected, plug._last_error = False, ""
    real = hue._hue
    hue._hue = lambda method, ip, key, path, data=None: answers.get(path)
    try:
        plug._fetch()
    finally:
        hue._hue = real
    return plug


good = fetch_with({
    "lights": {"1": {"name": "Desk"}},
    "groups": {"1": {"name": "Woonkamer", "type": "Room"},
               "2": {"name": "Entertainmentruimte1", "type": "Entertainment"}},
})
check("a good fetch keeps the groups", list(good._groups) == ["1"], good._groups)
check("and leaves the entertainment group out",
      "2" not in good._groups, good._groups)
check("and refreshes the screen once", good.ctx.scheduled == 1,
      good.ctx.scheduled)

refused = fetch_with({
    "lights": [{"error": {"type": 1, "description": "unauthorized user"}}],
    "groups": None,
})
check("a refused fetch is not connected", refused._connected is False)
check("and carries the bridge's own words",
      refused._last_error == "unauthorized user", refused._last_error)
check("and still refreshes the screen, or nobody sees the reason",
      refused.ctx.scheduled == 1, refused.ctx.scheduled)

# The window is rebuilt when the connection or the reason changes, and not
# otherwise: the poll behind it runs every three seconds, and rebuilding on
# every tick would destroy and recreate the Try again button under the
# pointer.
class Win:
    _group_rows = {}
    _light_rows = {}
    rebuilds = 0

    def _build_all(self):
        self.rebuilds += 1
        self._built_connected = self.p._connected
        self._built_error = getattr(self.p, "_last_error", "")

    refresh = hue.HueWindow.refresh


class Plug:
    _connected = False
    _last_error = "unauthorized user"
    _groups = {}
    _lights = {}


win = Win()
win.p = Plug()
win._build_all()
before = win.rebuilds
for _ in range(4):
    win.refresh()
check("a steady disconnected window is not rebuilt on every poll",
      win.rebuilds == before, win.rebuilds - before)

win.p._last_error = "no answer from 10.0.0.2"
win.refresh()
check("a new reason does rebuild it", win.rebuilds == before + 1,
      win.rebuilds - before)

win.p._connected = True
win.refresh()
check("and so does the bridge coming back", win.rebuilds == before + 2,
      win.rebuilds - before)

silent = fetch_with({"lights": None, "groups": None})
check("a bridge that says nothing says so by address",
      "10.0.0.2" in silent._last_error, silent._last_error)

print()
if failures:
    print("%d check(s) failed:" % len(failures))
    for failure in failures:
        print("  - %s" % failure)
    sys.exit(1)
print("all checks passed")
