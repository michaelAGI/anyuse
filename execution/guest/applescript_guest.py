#!/usr/bin/env python3
"""The AppleScript route's guest side. INSIDE THE GUEST VM ONLY (imported by the job agent's ``scope_read``,
``script_step`` and ``script_intent`` ops; the design notes).

The ONLY guest module of the route that starts ``/usr/bin/osascript``, and only for the route's scripts:

* :class:`RouteRunner` runs a script only when its text is exactly one of the route's constant reads (the grant's,
  the scope reads) or a script THIS PROCESS compiled from an app's own dictionary (``script_planner.REGISTRY``:
  raw codes, inputs only as argv items), and its argv is ``osascript -s s -`` followed by plain text items.
  Anything else raises :class:`NotARouteScript` before a process starts: no script text from a message, no shell,
  no ``run script``, no ``do shell script``.
* the permission is read with ``askUserIfNeeded=false`` (``osa_desktop.automation_status``): nothing here can raise
  the Automation prompt (only ``grant_automation_job.py`` does);
* the dictionaries are read from the apps' own bundles (``script_dictionary.read_bundle``: files only, never the
  ``sdef`` tool), per app version, once (:class:`GuestDictionaries`).

The job agent (``desktop_e2e_job.py``) keeps its rule: it starts no such process itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anyuse.execution import applescript_route as AR   # noqa: E402  (pure)
from anyuse.execution import osa_compile as oc         # noqa: E402  (pure)
from anyuse.execution import script_dictionary as SD   # noqa: E402  (file reads only)
from anyuse.execution import script_planner as SP      # noqa: E402  (pure)


def constant_scripts() -> Dict[str, Tuple[str, str]]:
    """The route's CONSTANT reads (the grant's one read per app, the scope reads) -> (bundle, kind)."""
    idx: Dict[str, Tuple[str, str]] = {}
    for b, sc in AR.SCOPE_SCRIPTS.items():
        idx[sc.script] = (b, "scope")
    for b, sc in AR.GRANT_EVENTS.items():
        idx[sc.script] = (b, "grant")
    return idx


class NotARouteScript(Exception):
    """A script or argv the route never builds: refused before any process starts."""


class RouteRunner:
    """``runner(argv, script) -> (rc, stdout, stderr)`` for ``applescript_route``, restricted to the route's constant
    reads and the scripts this process compiled (exact text), ``osascript -s s -`` with plain text argv items.
    ``spawn`` is the process starter (``subprocess.run``; tests pass a fake); ``timeout`` the seconds one script may
    take (a container read runs inside a recording or a replay: :data:`SCOPE_READ_TIMEOUT_S`)."""

    def __init__(self, dictionaries: Any = None, spawn: Callable[..., Any] = subprocess.run, timeout: float = 60):
        self.constants = constant_scripts()
        self.spawn = spawn
        self.timeout = timeout
        self.ran: List[Tuple[str, str]] = []

    def key(self, script: str) -> Optional[Tuple[str, str]]:
        if script in self.constants:
            return self.constants[script]
        sc = SP.REGISTRY.get(script)
        if sc is None or not oc.argv_only(script):
            return None
        return sc.bundle_id, sc.id.split("script_rung/", 1)[-1]

    def __call__(self, argv: Sequence[Any], script: str) -> Tuple[int, str, str]:
        key = self.key(script)
        if key is None:
            raise NotARouteScript("the script is not one of the route's scripts")
        head = list(AR.OSASCRIPT_ARGV)
        if list(argv[:len(head)]) != head or not all(isinstance(a, str) for a in argv[len(head):]):
            raise NotARouteScript("argv is not osascript -s s - followed by text items")
        p = self.spawn(list(argv), input=script, capture_output=True, text=True, timeout=self.timeout)
        self.ran.append(key)
        return p.returncode, p.stdout, p.stderr


ConstantRunner = RouteRunner        # the earlier name


def permission(bundle: str) -> int:
    """``AEDeterminePermissionToAutomateTarget(..., askUserIfNeeded=false)``: never asks."""
    from osa_desktop import automation_status
    return automation_status(bundle)


def _app_path(bundle: str) -> Optional[str]:
    import AppKit
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
    return str(url.path()) if url is not None else None


class GuestDictionaries(dict):
    """bundle id -> its dictionary, read on first use from the app's own bundle (``script_dictionary``, cached per
    app version; ``ANYUSE_SCRIPT_DICT_CACHE`` names a directory for the JSON cache). An app that is not installed
    or ships no dictionary is None. ``locate`` finds a bundle's path (``NSWorkspace``; tests pass a fake)."""

    def __init__(self, locate: Callable[[str], Optional[str]] = _app_path, cache: Optional[SD.DictionaryCache] = None):
        super().__init__()
        self.locate = locate
        self.cache = cache or SD.DictionaryCache(os.environ.get("ANYUSE_SCRIPT_DICT_CACHE") or None)
        self[AR.DICTIONARY] = None

    def get(self, bundle: str, default: Any = None) -> Any:     # type: ignore[override]
        if bundle not in self:
            path = self.locate(bundle) if bundle else None
            d = self.cache.get(path) if path else None
            self[bundle] = None if d is None or d.empty else d
        return super().get(bundle, default)


_DICTS: Optional[GuestDictionaries] = None


def dictionaries() -> GuestDictionaries:
    global _DICTS
    if _DICTS is None:
        _DICTS = GuestDictionaries()
    return _DICTS


def runner(timeout: float = 60) -> RouteRunner:
    return RouteRunner(timeout=timeout)


#: seconds one container read's script may take: it runs inside a recording (the watcher's worker) or right before a
#: replay acts; a Finder that does not answer in time is no answer (the AX reading decides), never a stalled step
SCOPE_READ_TIMEOUT_S = 10
#: the same read inside the watcher's recording jobs (``watch_steps_e2e.py``): it runs on the recorder's worker, and at
#: stop the watcher drains its queue for only ``watcher/step_capture.STOP_DRAIN_S`` (5 s), so a Finder that does not
#: answer must not hold the worker past it (later steps would go to ``dropped_at_stop``)
RECORD_SCOPE_READ_TIMEOUT_S = 2


def scope_reader(frame_of: Optional[Callable[[Dict[str, Any]], Any]] = None) -> AR.ScriptScopeReader:
    """The Apple-Events source for ``container_scope`` (``frame_of``: the window's AX frame)."""
    return AR.ScriptScopeReader(permission, runner(SCOPE_READ_TIMEOUT_S), frame_of=frame_of)


def ax_frame(query: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """The queried window's AX frame ``(x, y, w, h)`` (its ``AXPosition`` / ``AXSize`` through ``AXValueGetValue``,
    ``accessibility._ax_pair``), or None when unreadable: then a Finder window is matched by name alone, and two
    same-named windows give no answer (AX decides)."""
    from anyuse.context.accessibility import _ax_pair
    tree, window = query.get("tree"), query.get("window")
    attr = getattr(tree, "attr", None)
    if window is None or not callable(attr):
        return None
    pos, size = _ax_pair(attr(window, "AXPosition"), "point"), _ax_pair(attr(window, "AXSize"), "size")
    if pos is None or size is None:
        return None
    return pos[0], pos[1], size[0], size[1]


def plug_scope_reader(frame_of: Optional[Callable[[Dict[str, Any]], Any]] = None,
                      timeout: float = SCOPE_READ_TIMEOUT_S) -> AR.ScriptScopeReader:
    """THE guest wiring: the Apple-Events container read plugged into ``container_scope`` for
    this whole process (``applescript_route.plug_scope_reader``) with the real permission check, read WITHOUT asking
    (:func:`permission`: nothing here raises the prompt), and the route's constant-script runner. Called by every
    guest process that records or replays with the product: the job agent (``desktop_e2e_job.py serve``), the watcher's
    recording and replay jobs (``watch_steps_e2e.py``), the drive harness's guest commands (``interlock_drive_e2e.py``).
    An app not granted answers ``automation_not_granted`` and its read stays AX, as before. ``frame_of`` defaults to
    :func:`ax_frame`; ``timeout`` the script's limit (:data:`SCOPE_READ_TIMEOUT_S`; a recording job passes
    :data:`RECORD_SCOPE_READ_TIMEOUT_S`)."""
    return AR.plug_scope_reader(permission, runner(timeout), frame_of=frame_of or ax_frame)


def wire_process(where: str, out: Any = None, timeout: float = SCOPE_READ_TIMEOUT_S) -> Dict[str, Any]:
    """:func:`plug_scope_reader` for a guest job's process, NEVER raising: ``{"plugged", "why"}``, also printed as one
    line on stderr (``out``; no path, no title). Not plugged (an import or an AppKit failure): every read stays AX, the
    behaviour before the wiring, and the line says so."""
    try:
        plug_scope_reader(timeout=timeout)
        got = {"plugged": True, "why": ""}
    except Exception as exc:  # noqa: BLE001 - no Apple-Events source: the AX reading, as before
        got = {"plugged": False, "why": type(exc).__name__}
    print(f"[{where}] container read: Apple Events first {'plugged in' if got['plugged'] else 'NOT plugged in'}"
          + (f" ({got['why']})" if got["why"] else ""), file=out or sys.stderr, flush=True)
    return got


def rung_caller(scratch_root: str, approved_ids: Sequence[str], drive_enabled: bool) -> AR.OsaCaller:
    """The script rung's caller: ``osa_compile.run_osa``'s guards, the human gate with the standing approval
    (``approved_ids``, every path argument under ``scratch_root``)."""
    from anyuse.execution import hil_gate
    gate = hil_gate.HILGate(mode="all", approver=oc.disposable_approver(scratch_root, approved_ids))
    return AR.OsaCaller(permission, runner(), gate=gate, drive_enabled=drive_enabled, scratch_root=scratch_root)
