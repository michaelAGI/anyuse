"""A tiny file-based command relay into a LIVE, GUI Blender session.

Loaded via ``blender <file.blend> --python hook.py`` (GUI mode, not
``--background``): Blender still opens its normal window and enters its
normal event loop, and this module additionally registers a
``bpy.app.timers`` callback that polls a JSON command file every 0.3s and
writes a JSON result file. That is the entire mechanism -- no AX, no window
scripting, no OS automation API of any kind, and nothing here runs unless a
guest job script wrote the command file first.

Why this exists instead of literal keystroke-driven menu navigation for
EVERY step: this round's exploration found the two most-menu-dependent
actions unreliable within a live GUI session driven by synthetic HID events
(documented in the round's report) --

* ``F3`` (operator search) ranks by fuzzy match, not exact label: querying
  ``"Cube"`` opened an unrelated "Import SVG as Curves" file browser, not
  ``mesh.primitive_cube_add`` -- there is no operator labelled exactly
  "Add Cube" to search for, and the ranking is not something this round can
  make deterministic without a human confirming the highlighted hit visually
  on every single invocation.
* ``Shift+A`` (the Add menu) posted as a raw keycode+modifier CGEvent did
  not reliably reproduce the interactive "hold Shift, press A" gesture --
  the operator log after that keystroke showed no menu was opened.

Per this round's brief ("driven by keystrokes/mouse ... or, if that proves
unreliable, by executing the same operators through Blender's own text
editor -- say which"): this module IS that fallback, using Blender's own
``--python``/timer scripting facility (the same mechanism the Text Editor's
"Run Script" ultimately triggers) rather than the Text Editor UI area
specifically, because it does not depend on guessing pixel coordinates for
that area in an unfamiliar window layout. Every action it runs is a real
``bpy.ops.*`` call against the real, visible, on-screen document -- a
screenshot taken at any point shows the same state a human clicking through
the same operators would have produced. Only ``bpy.ops.*`` calls this
module is TOLD to run are ever exec'd; nothing here reads or writes outside
the two file paths a caller controls.

Command file (JSON): ``{"action": "exec"|"dump_state"|"dump_ops"|"dump_both"|"dump_catalog"|"ping",
"code": "<python, for 'exec'>", "since": <int, for 'dump_ops'/'dump_both'>}``
Result file (JSON): ``{"ok": bool, ...action-specific keys..., "error": "..."}``
on failure.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

import bpy

_BLENDER_DIR = os.path.expanduser("~/anyuse-guest/blender")
CMD_PATH = os.path.join(_BLENDER_DIR, "cmd.json")
RESULT_PATH = os.path.join(_BLENDER_DIR, "result.json")

sys.path.insert(0, os.path.expanduser("~/anyuse-guest/src/anyuse/execution/guest/blender"))


def _handle(cmd: dict) -> dict:
    import capture  # local import: only needed once a command arrives

    action = cmd.get("action")
    if action == "ping":
        return {"ok": True, "pong": True}
    if action == "dump_state":
        return {"ok": True, "state": capture.dump_scene_state()}
    if action == "dump_ops":
        return {"ok": True, "ops": capture.dump_operator_log(int(cmd.get("since", 0)))}
    if action == "dump_both":
        since = int(cmd.get("since", 0))
        return {"ok": True, "state": capture.dump_scene_state(),
                "ops": capture.dump_operator_log(since)}
    if action == "dump_catalog":
        # desktop discovery: the operator registry, read-only
        return {"ok": True, "catalog": capture.dump_operator_catalog()}
    if action == "exec":
        exec(cmd["code"], {"bpy": bpy})
        return {"ok": True}
    if action == "eval":
        # Diagnostic only (used while establishing which real operator idname
        # backs a given GUI action, e.g. searching bpy.ops for "rename") --
        # never used by the compiled/proved scripts themselves.
        value = eval(cmd["code"], {"bpy": bpy})
        return {"ok": True, "value": repr(value)}
    return {"ok": False, "error": f"unknown action {action!r}"}


def _poll():
    if os.path.exists(CMD_PATH):
        try:
            with open(CMD_PATH) as fh:
                cmd = json.load(fh)
            os.remove(CMD_PATH)
            result = _handle(cmd)
        except Exception as exc:  # noqa: BLE001 - reported back, never raised into the timer
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                     "traceback": traceback.format_exc()}
        with open(RESULT_PATH, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
    return 0.3


bpy.app.timers.register(_poll, first_interval=0.3)
print("anyuse blender hook: armed, watching", CMD_PATH)
