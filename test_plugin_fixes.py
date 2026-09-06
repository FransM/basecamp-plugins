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

print()
if failures:
    print("%d check(s) failed:" % len(failures))
    for failure in failures:
        print("  - %s" % failure)
    sys.exit(1)
print("all checks passed")
