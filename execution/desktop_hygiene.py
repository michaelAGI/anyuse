"""Desktop test hygiene: every run starts from a DECLARED clean state, or not at all.

Repeatability study, desktop half:
Finder's "new folder" went 0/20 because the guest was not in the proof's
start state. A folder was selected and another process's authorization prompt
was on screen, left by an earlier job. From there the trigger moved the
selection into the new folder on 13 runs. The contract's preserved-state
check caught every one, but only AFTER acting: 20 runs acted on a state
nobody declared.

This module makes the start state a precondition that is checked BEFORE
anything is actuated, and refuses on a dirty state instead of acting on it or
trying to "tidy" it through the UI:

* :class:`CleanState` declares the state: the app (bundle id) in front, the
  known window, NO selection, no sheet or dialog in the app, no window of
  another process in front of it (a SecurityAgent prompt, an alert), and a
  disposable folder whose contents are exactly the declared entries.
* :func:`reset_folder` is the only preparation it performs, and it touches
  the filesystem only: inside a folder that carries the disposable marker
  file naming that same folder, it removes entries that are not declared and
  recreates declared ones that are missing. It never clicks, never deselects,
  never closes a window.
* :func:`check` compares an observation with the declaration and returns every
  problem; a field the observer could not read is a problem (fail closed).
* :func:`require_clean` resets, observes, checks. :func:`run_from_clean_state`
  runs the workflow only when the check returned no problem.

Pure apart from :func:`reset_folder`'s filesystem calls on the declared
disposable folder; the observer is injected (the guest driver reads AX and
the window list; host tests pass dicts).
"""
from __future__ import annotations

import os
import re as _re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: the marker a folder must carry before :func:`reset_folder` touches it
MARKER = ".anyuse-disposable"

#: processes whose windows are part of every macOS session and never a
#: foreign prompt (menu bar, Dock, desktop, notifications)
SESSION_OWNERS = frozenset({"Window Server", "Dock", "SystemUIServer", "Control Center",
                            "Kontrollzentrum", "Notification Center", "Mitteilungszentrale",
                            "WindowManager", "Spotlight", "TextInputMenuAgent",
                            "Wallpaper", "loginwindow"})

#: the owner of every authentication prompt (SecurityAgent): its windows are
#: never touched by any tidy step, only reported
AUTH_OWNERS = frozenset({"SecurityAgent"})

#: window-server layers at or above this are session chrome (menu bar, status
#: items, Dock, HUDs); 1000 is the layer of an authorization panel
CHROME_LAYER = 20
AUTH_LAYER = 1000


def leftover_windows(windows: Sequence[Dict[str, Any]], regular_pids: Sequence[int] = ()
                     ) -> List[Dict[str, Any]]:
    """The windows that would make
    EVERY app's per-row clean start dirty, from the on-screen window list
    ``[{"owner", "pid", "layer", "title"}]`` (front to back), each with a
    tidy verdict. :func:`check` (``no_foreign_windows``) refuses a row when
    another process's window is above the app's; a window ABOVE the normal
    layer stays above every app's windows whatever is activated, so it
    dirties every row of every candidate (a dictation panel, an
    input method's modal panel at layer 8 with no accessibility tree, left
    by an earlier probe, refused all 36 rows). The pre-job check
    lists such windows and tidies what may be tidied, by the ROLE of the
    owner, never by its name:

    * ``never``: an authentication agent's window: reported, never touched;
    * ``report``: a regular application's panel (an app with a Dock
      presence, ``regular_pids``): the measured apps are quit by the check
      job's existing step; anything else needs a person;
    * ``end``: a helper process's panel (no Dock presence: an input method,
      a palette, an alert agent): the helper is ended (the OS relaunches it
      on demand). Nothing is pressed or typed in it.

    Session chrome (:data:`SESSION_OWNERS`, chrome layers) and windows at
    the normal layer (ordered by activation: the measured app's windows come
    in front when it is activated) are not leftovers."""
    out: List[Dict[str, Any]] = []
    regular = {int(p) for p in regular_pids}
    for w in windows or []:
        owner = str((w or {}).get("owner") or "")
        try:
            layer = int((w or {}).get("layer") or 0)
            pid = int((w or {}).get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if owner in SESSION_OWNERS or layer <= 0:
            continue
        if layer >= CHROME_LAYER and layer != AUTH_LAYER and owner not in AUTH_OWNERS:
            continue
        tidy = "never" if owner in AUTH_OWNERS else "report" if pid in regular else "end"
        out.append({"owner": owner, "pid": pid, "layer": layer, "title": str((w or {}).get("title") or ""),
                    "tidy": tidy})
    return out


class DirtyStartState(RuntimeError):
    """Raised by :func:`ensure_clean`: the run was refused before acting."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class CleanState:
    bundle_id: str
    #: the front window's title (or document name) the run must start in;
    #: "" = any window of the app, but there must be exactly one in front
    window: str = ""
    #: the disposable folder the workflow creates in / reads from ("" = none)
    folder: str = ""
    #: its declared contents after a reset: name -> "dir" | "file"
    entries: Tuple[Tuple[str, str], ...] = ()
    #: the app may show no sheet or dialog at the start
    no_dialogs: bool = True
    #: nothing may be selected at the start (the trigger must not act on it)
    no_selection: bool = True
    #: no window of another process may be in front of the app
    no_foreign_windows: bool = True

    def listing(self) -> List[str]:
        return sorted(n for n, _k in self.entries)

    def to_dict(self) -> Dict[str, Any]:
        return {"bundle_id": self.bundle_id, "window": self.window, "folder": self.folder,
                "entries": [list(e) for e in self.entries], "no_dialogs": self.no_dialogs,
                "no_selection": self.no_selection, "no_foreign_windows": self.no_foreign_windows}


def check(declared: CleanState, observed: Optional[Dict[str, Any]]) -> List[str]:
    """Every way ``observed`` differs from the declared clean state.

    ``observed``: ``{"bundle_id", "front_window", "selection": [...],
    "dialogs": [...], "foreign_windows": [{"owner", "title"}],
    "folder_listing": [...]}``. A missing (``None``) field that the
    declaration needs is a problem of its own: an unreadable state is never
    assumed clean."""
    if not isinstance(observed, dict):
        return ["the start state could not be observed"]
    probs: List[str] = []

    def need(key: str) -> Any:
        if observed.get(key) is None:
            probs.append(f"could not read {key.replace('_', ' ')}: refusing rather than "
                         f"assuming it is clean")
        return observed.get(key)

    bid = need("bundle_id")
    if bid is not None and bid != declared.bundle_id:
        probs.append(f"{bid!r} is in front, not {declared.bundle_id!r}")
    win = need("front_window")
    if win is not None and declared.window and win != declared.window:
        probs.append(f"the front window is {win!r}, not the declared {declared.window!r}")
    if declared.no_selection:
        sel = need("selection")
        if sel:
            shown = ", ".join(str(s) for s in list(sel)[:5])
            probs.append(f"{len(sel)} item(s) selected ({shown}): a trigger could act on the "
                         f"selection instead of the declared target")
    if declared.no_dialogs:
        dlg = need("dialogs")
        if dlg:
            probs.append(f"the app shows {len(dlg)} sheet/dialog(s) at the start: {dlg[:3]}")
    if declared.no_foreign_windows:
        fw = need("foreign_windows")
        if fw:
            foreign = [w for w in fw if str((w or {}).get("owner") or "") not in SESSION_OWNERS
                       and str((w or {}).get("owner") or "") != declared.bundle_id]
            if foreign:
                shown = ", ".join(f"{w.get('owner')}: {w.get('title') or '(untitled)'}"
                                  for w in foreign[:3])
                probs.append(f"another process's window is in front ({shown})")
    if declared.folder:
        lst = need("folder_listing")
        if lst is not None:
            have = sorted(str(x) for x in lst if not str(x).startswith("."))
            want = declared.listing()
            extra = sorted(set(have) - set(want))
            missing = sorted(set(want) - set(have))
            if extra or missing:
                probs.append(f"the disposable folder is not in its declared state "
                             f"(unexpected: {extra[:5]}, missing: {missing[:5]})")
    return probs


def _marked(folder: str) -> bool:
    marker = os.path.join(folder, MARKER)
    try:
        with open(marker) as fh:
            return os.path.realpath(fh.read().strip()) == os.path.realpath(folder)
    except OSError:
        return False


def mark_disposable(folder: str) -> str:
    """Create ``folder`` and its marker (the marker names the folder, so a
    copied or moved marker does not authorise a reset elsewhere)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, MARKER)
    with open(path, "w") as fh:
        fh.write(os.path.realpath(folder))
    return path


def reset_folder(declared: CleanState) -> List[str]:
    """Bring the declared disposable folder to its declared contents.

    Refuses (raises :class:`DirtyStartState`) unless the folder carries the
    marker naming itself. Only direct children are touched; symlinks are
    removed as links, never followed. Returns what it did."""
    folder = declared.folder
    if not folder:
        return []
    if not os.path.isabs(folder) or os.path.realpath(folder) in ("/", os.path.expanduser("~")):
        raise DirtyStartState([f"refusing to reset {folder!r}: not a disposable folder path"])
    if not os.path.isdir(folder) or not _marked(folder):
        raise DirtyStartState([f"refusing to reset {folder!r}: it does not carry the "
                               f"{MARKER} marker naming itself"])
    want = dict(declared.entries)
    done: List[str] = []
    for name in sorted(os.listdir(folder)):
        if name == MARKER or name in want:
            continue
        path = os.path.join(folder, name)
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
        done.append(f"removed {name}")
    for name, kind in sorted(want.items()):
        path = os.path.join(folder, name)
        if os.path.lexists(path):
            ok = os.path.isdir(path) if kind == "dir" else os.path.isfile(path)
            if ok and not os.path.islink(path):
                continue
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)
        if kind == "dir":
            os.makedirs(path)
        else:
            open(path, "w").close()
        done.append(f"recreated {name}")
    return done


def require_clean(declared: CleanState, observe: Callable[[], Optional[Dict[str, Any]]],
                  reset: Optional[Callable[[CleanState], Any]] = reset_folder
                  ) -> Tuple[bool, List[str], Optional[Dict[str, Any]], List[str]]:
    """Reset the disposable folder (filesystem only), observe, check.
    Returns ``(clean, problems, observed, reset_actions)``."""
    actions: List[str] = []
    if reset is not None and declared.folder:
        try:
            actions = list(reset(declared) or [])
        except DirtyStartState as exc:
            return False, exc.problems, None, actions
    try:
        observed = observe()
    except Exception as exc:  # noqa: BLE001 — an unreadable state is never clean
        return False, [f"the start state could not be observed: {type(exc).__name__}: {exc}"], \
            None, actions
    problems = check(declared, observed)
    return not problems, problems, observed, actions


def ensure_clean(declared: CleanState, observe, reset=reset_folder) -> Dict[str, Any]:
    """:func:`require_clean`, raising :class:`DirtyStartState` on a dirty state."""
    clean, problems, observed, _actions = require_clean(declared, observe, reset)
    if not clean:
        raise DirtyStartState(problems)
    return observed or {}


def run_from_clean_state(declared: CleanState, observe, run: Callable[[], Any],
                         reset=reset_folder) -> Dict[str, Any]:
    """Run ``run()`` only from the declared clean state; otherwise refuse
    WITHOUT acting and say why."""
    clean, problems, observed, actions = require_clean(declared, observe, reset)
    if not clean:
        return {"ran": False, "refused": True, "why": problems, "observed": observed,
                "reset": actions}
    return {"ran": True, "refused": False, "result": run(), "observed": observed,
            "reset": actions}


# ---------------------------------------------------------------------------
# Harness fixes, all pure
# ---------------------------------------------------------------------------

#: ``<AXValue 0xbe9098e70> {value = x:0.000000 y:30.000000 type = kAXValueCGPointType}``
_AXVALUE_TYPE = _re.compile(r"type\s*=\s*kAXValue(\w+?)Type")
_AXVALUE_BODY = _re.compile(r"value\s*=\s*(.*?)\s*type\s*=", _re.S)
_AXVALUE_PAIR = _re.compile(r"([A-Za-z]+)\s*:\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_ADDRESS = _re.compile(r"0x[0-9a-fA-F]+")
_KINDS = {"CGPoint": "point", "CGSize": "size", "CGRect": "rect", "CFRange": "range"}


def ax_value_plain(text: Any) -> Any:
    """An AXValue as plain, comparable numbers.

    The text of a PyObjC AXValue carries the wrapper's MEMORY ADDRESS
    (``<AXValue 0xbe9098e70> {value = x:0.000000 y:30.000000 type =
    kAXValueCGPointType}``), so two reads of the same frame compared as
    strings are equal only when the allocator happens to reuse the address:
    a field_rw/property_update workflow then stops at "main
    window ... is not the declared window" although both frames are
    (0, 30, 1440 x 797). This returns ``(kind, n1, n2, ...)`` with ``kind``
    one of point/size/rect/range and the numbers in their printed order
    (x y / w h / x y w h / location length), rounded to 3 decimals. A text
    that is not an AXValue comes back with every ``0x...`` address removed,
    so no comparison ever sees an address; ``None`` stays ``None``."""
    if text is None:
        return None
    s = str(text)
    m_type, m_body = _AXVALUE_TYPE.search(s), _AXVALUE_BODY.search(s)
    if m_type and m_body:
        kind = _KINDS.get(m_type.group(1), m_type.group(1).lower())
        nums = [round(float(v), 3) for _k, v in _AXVALUE_PAIR.findall(m_body.group(1))]
        if nums:
            return tuple([kind] + nums)
    return _ADDRESS.sub("0x", s)


def own_surface_action(surface: Dict[str, Any], foreign: Sequence[Any] = ()) -> str:
    """What a workflow's own reset may do with a sheet or dialog its app
    shows (a sheet left by one workflow refused every row of that
    workflow and of the next two, 18 rows). ``surface``: ``{"key": ...,
    "auth": bool, "cancel_button": bool}``; ``foreign``: the keys of the
    surfaces that were already there when this workflow began.

    * ``never``: an authentication prompt: never touched, never answered;
    * ``leave``: a surface this workflow did not open (it was there at the
      workflow's first reset), or one without a Cancel button: it stays,
      and the per-row clean check refuses the row;
    * ``cancel``: a surface opened during this workflow (by its own step)
      that has a Cancel button: pressed Cancel (never a default button,
      never a typed key), as the probe's follow-up closes what it opened."""
    if (surface or {}).get("auth"):
        return "never"
    if (surface or {}).get("key") in set(foreign or ()):
        return "leave"
    return "cancel" if (surface or {}).get("cancel_button") else "leave"


def layout_toggles(rows: Sequence[Dict[str, Any]], not_a_toggle: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The recorded layout transitions a restore may use, from probe rows.
    Each: ``{"key", "trigger", "before", "after", "group"}``
    (container keys). ``group`` is the run of a marked menu group (radio
    items such as a view mode: pressing one sets ITS view whatever was
    shown, pressing it again does not undo it), else None (a self-inverse
    pane toggle). Left out: rows whose effects include ``not_a_toggle``,
    rows that changed the number of windows, rows probed under a
    precondition that needs a selection or selected text (the effect
    depends on it: Finder's 'Back' changed the folder), and non-group rows
    after which the window showed no container at all (a focus or window
    change such as 'Next Window', not a pane)."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for r in rows or []:
        b = (r.get("before") or {}).get("containers")
        a = (r.get("after") or {}).get("containers")
        if b is None or a is None or set(b) == set(a):
            continue
        eff = list(r.get("effects") or [])
        if any(e in not_a_toggle for e in eff):
            continue
        if len((r.get("before") or {}).get("windows") or []) != len((r.get("after") or {}).get("windows") or []):
            continue
        pc = str((r.get("precondition") or {}).get("key") or "")
        if "selected" in pc:
            continue
        key = (r.get("trigger") or {}).get("key") or r.get("key")
        if key in seen:
            continue
        group = r.get("run") if r.get("kind") == "run" and any(str(e).startswith("mark_") for e in eff) else None
        if group is None and not set(a):
            continue
        seen.add(key)
        out.append({"key": key, "trigger": r.get("trigger"), "before": sorted(b), "after": sorted(a),
                    "group": group})
    return out


def predict_layout(cur: Sequence[str], toggle: Dict[str, Any], group_views: Dict[Any, set],
                   norm: Callable[[str], str] = lambda k: k) -> Optional[frozenset]:
    """The layout a press of ``toggle`` gives from ``cur``, from its recorded
    transition; None when the recording says nothing about this state (then
    it is never pressed). A group member replaces the group's view; a
    self-inverse toggle applies forward when its removed keys are shown, in
    reverse when its added keys are shown."""
    c = {norm(k) for k in cur}
    b, a = {norm(k) for k in toggle["before"]}, {norm(k) for k in toggle["after"]}
    removed, added = b - a, a - b
    if toggle.get("group") is not None:
        return frozenset((c - group_views.get(toggle["group"], set())) | added)
    if removed <= c and not (added & c):
        return frozenset((c - removed) | added)
    if added <= c and not (removed & c):
        return frozenset((c - added) | removed)
    return None


def plan_layout(cur: Sequence[str], ref: Sequence[str], toggles: Sequence[Dict[str, Any]],
                norm: Callable[[str], str] = lambda k: k, max_presses: int = 3
                ) -> Tuple[List[Dict[str, Any]], List[frozenset]]:
    """The shortest press sequence (at most ``max_presses``) that the
    recorded transitions predict to bring ``cur`` to ``ref``; else the
    shortest one that ends closest to it, if that is strictly closer than
    ``cur``; else nothing. Returns (toggles to press, predicted layout after
    each). Replaces a greedy restore, which pressed an item that
    did not help AGAIN to undo it: a radio item pressed twice stays set
    (Finder ended in gallery view every second reset), and 'Back' or
    'Next Window' pressed twice moved the window further away."""
    groups: Dict[Any, set] = {}
    for t in toggles:
        if t.get("group") is not None:
            groups.setdefault(t["group"], set()).update(
                {norm(k) for k in t["before"]} ^ {norm(k) for k in t["after"]})
    goal = frozenset(norm(k) for k in ref)
    start = frozenset(norm(k) for k in cur)

    def dist(s):
        return len(s ^ goal)
    best = ([], [], dist(start))
    frontier = [(start, [], [])]
    seen = {start}
    for _depth in range(max_presses):
        nxt = []
        for state, seq, preds in frontier:
            for t in toggles:
                p = predict_layout(state, t, groups, norm)
                if p is None or p in seen:
                    continue
                seen.add(p)
                cand = (seq + [t], preds + [p])
                if p == goal:
                    return cand
                if dist(p) < best[2]:
                    best = (cand[0], cand[1], dist(p))
                nxt.append((p, cand[0], cand[1]))
        frontier = nxt
    return best[0], best[1]


def nested_entries(children: Dict[str, Sequence[str]], cap: int = 500) -> List[str]:
    """A folder model's sentinel for object_create: ``sub/entry``
    for every entry inside each subfolder of the scope folder
    (``children``: subfolder name -> its entries). A new object at the top
    of the scope (an empty folder, a file) leaves it unchanged; an object
    that went INTO a subfolder changes it. Finder's column view had a
    subfolder selected after a view switch and all six 'New Folder' rows put
    their folder into it: the observer read only the scope's own listing
    and reported "no object named ..." instead of a preserved-state
    violation. Capped (sorted) so a large tree cannot grow the observation."""
    out = sorted(f"{sub}/{k}" for sub, ks in (children or {}).items() for k in (ks or []))
    return out[:cap]
