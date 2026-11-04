"""Desktop compile, generalized — infer the OPERATION CLASS, never a per-app script.

The measurement harness proved four HARDCODED candidates
against four hardcoded UI flows. The question was: why isn't
this generic? This module answers it: given a recording — the SAME
recorded-selector vocabulary ``execution/guest/flows.py``'s ``Recorder`` and
``memory/recorder.py`` already produce (canonical_role, AXIdentifier, a menu
item's command character; never a screen coordinate) — infer WHICH KIND of
operation the recording performed, from the recorded action vocabulary alone,
and hand back a direct implementation plus a postcondition reader for that
class. No model is involved in the decision, exactly as
the design notes established for the web half ("no model
proposes a script — each candidate is a fixed function of the recorded
flow's kind"): here the fixed function is fixed to a CLASS of recordings, not
to one recording.

Five classes, each a deterministic rule over roles + app (by bundle id) +
recorded keystroke identity (cmd/mods) — never over which script or which
flow name recorded it:

  arithmetic    Calculator: every recorded step is an AXButton press.
  file_write    An AXTextArea text-entry step + a Save (⌘S) AXMenuItem.
  directory_op  Finder, subtype "create": a New Folder (⇧⌘N) AXMenuItem +
                a text-field step (the rename-on-create field).
                Finder, subtype "rename": a text-field step reached either
                through some OTHER AXMenuItem (not ⇧⌘N) or through NO menu
                and NO button at all (select + Return, Finder's other
                standard rename trigger) — an in-place rename either way.
  file_search   Finder: an AXButton + an AXTextField, no AXMenuItem at all
                (the search toggle/field pair, never reached through a menu).
  read_value    A read-only app (no AXTextArea/AXTextField step recorded)
                with at least one AXButton/navigation step.

Anything else is ``"unresolved"`` — never a guess (the same "0-found stays
0-found, never a first-of-role fallback" property element resolution
already has, applied here to class resolution).

Each class = a direct operation + a postcondition reader + the SAME
fresh-input proof discipline the web half's ``api_verify.verify_template``
uses: >= 2 unseen inputs (``MIN_FRESH_CASES``, the exact floor
the design notes document), each run >= 3 times
(``MIN_RUNS_PER_INPUT``, stricter than the measurement harness's
proof-of-concept default of 2), and EVERY row must agree for the class to be
"proved" on that recording. "Agree" means what it means in
``osa_compile.prove`` and ``blender_compile.prove``: the recording's own
inputs are excluded, the start state is declared and valid, both
observations are valid, both show the expected state delta and the
preserved state — only then are they compared (§4 below). A single disagreeing row (Finder search's
"berry" inside "blueberry" — Spotlight-backed search does not match a
mid-word substring; a literal ``find`` does) fails the whole proof and is
reported per-row, not averaged away or silently dropped: see
the design notes for why ``file_search``'s canonical direct
operation is deliberately NOT claimed equivalent to Finder's own search
field in general.

Every direct operation and every "read the real world" backend is
injectable (``backend=``), the same convention the rest of this codebase
uses (``sec=``, ``transport=``, ``runner=``, ``crypto=``...) so the class
rules and the proof harness are fully unit-testable on the host, offline,
against real captured recordings (``testdata/ax/desktop_recorded_steps.json``
— extracted from a guest corpus, not invented) with no VM, no AX
grant and no subprocess ever spawned.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: Same floor the design notes document for the web half
#: (api_verify.MIN_FRESH_CASES): a template/class that reproduces only the
#: recording it came from has proved nothing.
MIN_FRESH_CASES = 2

#: Runs per input. The measurement harness's proof-of-concept used
#: 2 by default; this floor is n >= 3 runs.
MIN_RUNS_PER_INPUT = 3

_BUNDLE_IDS = {
    "TextEdit": "com.apple.TextEdit",
    "Finder": "com.apple.finder",
    "Calculator": "com.apple.calculator",
    "SystemSettings": "com.apple.systempreferences",
    "Notes": "com.apple.Notes",
    "Safari": "com.apple.Safari",
}


# ---------------------------------------------------------------------------
# 1. The recording model — built from the product's own recorded-selector
#    vocabulary, never from screen coordinates.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordedStep:
    """One recorded step, reduced to the facts the class rules use: role,
    identifier, title, and a menu item's command character/modifiers — the
    SAME ``item_identity()`` shape ``execution/guest/flows.py`` already records,
    independent of which matcher later resolves it."""
    name: str
    role: str = ""
    identifier: str = ""
    title: str = ""
    cmd: str = ""
    mods: str = ""
    path: str = ""


@dataclass(frozen=True)
class DesktopRecording:
    """A recording of one desktop UI flow. ``flow``/``app`` are for
    reporting only — the class rules below consult ``bundle_id`` and
    ``steps`` alone, never the flow's name or which script recorded it."""
    flow: str
    app: str
    bundle_id: str
    steps: Tuple[RecordedStep, ...] = ()


def recording_from_flow_json(entry: Dict[str, Any]) -> DesktopRecording:
    """Build a :class:`DesktopRecording` from one flow's recording JSON, the
    exact ``{"flow", "app", "recorded_steps": {name: {selector, identity}}}``
    shape ``execution/guest/flows.py``/``flows_more.py`` write (or
    ``recorded_step``, singular, for the HIL flow)."""
    app = entry.get("app", "") or ""
    bundle_id = entry.get("bundle_id") or _BUNDLE_IDS.get(app, "")
    raw = dict(entry.get("recorded_steps") or {})
    single = entry.get("recorded_step")
    if single is not None:
        raw = {**raw, "_single": single}
    steps = []
    for name, s in raw.items():
        identity = (s or {}).get("identity") or {}
        selector = (s or {}).get("selector") or {}
        steps.append(RecordedStep(
            name=name,
            role=identity.get("role") or selector.get("raw_role") or "",
            identifier=identity.get("identifier") or selector.get("identifier") or "",
            title=identity.get("title") or selector.get("name") or "",
            cmd=identity.get("cmd") or "",
            mods=identity.get("mods") or "",
            path=selector.get("path") or "",
        ))
    return DesktopRecording(flow=entry.get("flow", ""), app=app, bundle_id=bundle_id,
                            steps=tuple(steps))


# ---------------------------------------------------------------------------
# 2. Class inference — deterministic, over roles/bundle-id/cmd chars only.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassInference:
    op_class: str                       # "arithmetic"|"file_write"|"directory_op"|"file_search"|"read_value"|"unresolved"
    subtype: Optional[str] = None       # "create"|"rename" for directory_op
    reason: str = ""
    evidence: Tuple[str, ...] = ()

    def key(self):
        return (self.op_class, self.subtype) if self.subtype else self.op_class


def _unresolved(reason: str, **kw) -> ClassInference:
    return ClassInference("unresolved", reason=reason, **kw)


def infer_class(rec: DesktopRecording) -> ClassInference:
    """Infer the operation class from ``rec.bundle_id`` and ``rec.steps``
    alone. Never a guess: an app whose recorded vocabulary does not match one
    of the five fixed shapes below is ``"unresolved"``, exactly like element resolution's
    "0 candidates stays 0 candidates" and its identifier widening — a wider
    net can only turn "no class" into a legitimate match, never a wrong one,
    because every branch requires a POSITIVE, exclusive shape (a keystroke
    identity, a role combination), not "whatever is left over"."""
    steps = rec.steps
    roles = [s.role for s in steps]

    if rec.bundle_id == "com.apple.calculator":
        if steps and all(r == "AXButton" for r in roles):
            return ClassInference("arithmetic",
                                  reason="Calculator: every recorded step is an AXButton press",
                                  evidence=(f"{len(steps)} AXButton step(s)",))
        return _unresolved("Calculator app, but not every recorded step is an AXButton press")

    if rec.bundle_id == "com.apple.TextEdit":
        has_text_area = "AXTextArea" in roles
        save_step = next((s for s in steps if s.role == "AXMenuItem" and s.cmd.lower() == "s"), None)
        if has_text_area and save_step is not None:
            return ClassInference("file_write",
                                  reason="TextEdit: an AXTextArea text-entry step + a Save (cmd char 's') AXMenuItem",
                                  evidence=("AXTextArea present", f"save cmd={save_step.cmd!r} mods={save_step.mods!r}"))
        if has_text_area:
            return _unresolved("TextEdit: AXTextArea recorded but no Save (cmd 's') menu item — "
                               "nothing to prove a write against (e.g. find-in-document, not a write)")
        return _unresolved("TextEdit app, but no AXTextArea step recorded")

    if rec.bundle_id == "com.apple.finder":
        new_folder = next((s for s in steps
                           if s.role == "AXMenuItem" and s.cmd.lower() == "n" and s.mods == "1"), None)
        other_menu_item = next((s for s in steps if s.role == "AXMenuItem" and s is not new_folder), None)
        text_field = next((s for s in steps if s.role == "AXTextField"), None)
        button = next((s for s in steps if s.role == "AXButton"), None)
        has_any_menu_item = any(s.role == "AXMenuItem" for s in steps)

        if new_folder is not None and text_field is not None:
            return ClassInference("directory_op", subtype="create",
                                  reason="Finder: a New Folder (cmd 'n' mods '1') AXMenuItem + a text-field step",
                                  evidence=(f"new_folder cmd={new_folder.cmd!r} mods={new_folder.mods!r}",))
        if not has_any_menu_item and button is not None and text_field is not None:
            return ClassInference("file_search",
                                  reason="Finder: an AXButton + an AXTextField, no AXMenuItem at all "
                                         "(the search toggle/field pair)",
                                  evidence=("no AXMenuItem step", "AXButton + AXTextField present"))
        if new_folder is None and other_menu_item is not None and text_field is not None:
            return ClassInference("directory_op", subtype="rename",
                                  reason="Finder: a text-field step + an AXMenuItem that is NOT New Folder "
                                         "— an in-place rename reached through a menu",
                                  evidence=(f"menu item cmd={other_menu_item.cmd!r} (not 'n'/'1')",))
        if not has_any_menu_item and button is None and text_field is not None:
            return ClassInference("directory_op", subtype="rename",
                                  reason="Finder: only a text-field step, no AXButton and no AXMenuItem at all "
                                         "— an in-place rename reached by selecting an item and pressing Return "
                                         "(Finder's other standard rename trigger, distinct from the search "
                                         "toggle/field pair, which always has an AXButton)",
                                  evidence=("no AXButton step", "no AXMenuItem step", "one AXTextField step"))
        return _unresolved("Finder app, but the recorded vocabulary matched none of "
                           "directory_op/file_search's fixed shapes")

    if rec.bundle_id == "com.apple.systempreferences":
        has_text_entry = any(r in ("AXTextArea", "AXTextField") for r in roles)
        has_nav = any(r in ("AXButton", "AXStaticText") for r in roles)
        if steps and not has_text_entry and has_nav:
            return ClassInference("read_value",
                                  reason="System Settings: navigation/button steps only, no text entry — a read",
                                  evidence=("no AXTextArea/AXTextField step recorded",))
        return _unresolved("System Settings app, but a text-entry step was recorded (not a plain read)")

    return _unresolved(f"no class rule for bundle_id={rec.bundle_id!r}")


# ---------------------------------------------------------------------------
# 3. Direct operations + postcondition readers, one pair per class. Every
#    backend is injectable so these are host-unit-testable with no
#    subprocess, no VM, no AX grant.
# ---------------------------------------------------------------------------

def direct_arithmetic(a: float, op: str, b: float) -> float:
    return {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]


def direct_file_write(path: str, text: str) -> None:
    with open(path, "w") as fh:
        fh.write(text)


def read_file_write(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return fh.read()


def direct_directory_create(path: str) -> None:
    os.makedirs(path, exist_ok=False)


def direct_directory_rename(src: str, dst: str) -> None:
    os.rename(src, dst)


def read_directory_exists(path: str) -> bool:
    return os.path.isdir(path)


def read_directory_renamed(src: str, dst: str) -> bool:
    """True iff the rename happened cleanly: the destination now exists and
    the source no longer does (a copy that left the source behind is not a
    rename)."""
    return os.path.isdir(dst) and not os.path.exists(src)


def _default_find_backend(folder: str, pattern: str) -> List[str]:
    out = subprocess.run(["/usr/bin/find", folder, "-iname", f"*{pattern}*"],
                         capture_output=True, text=True, timeout=15).stdout
    return [os.path.basename(p.rstrip("/")) for p in out.splitlines() if p.strip()]


def direct_file_search(folder: str, pattern: str,
                       backend: Optional[Callable[[str, str], List[str]]] = None) -> List[str]:
    """The class's canonical direct operation: a literal, case-insensitive
    substring match over file NAMES (``find <folder> -iname "*pattern*"`` by
    default). Deliberately NOT Finder's own search semantics — Finder's
    search field is Spotlight-backed and does not match a substring with no
    word delimiter (the design notes's "berry" inside "blueberry" finding). This function does not hide
    that: it is a literal substring match, full stop; the proof step is what
    surfaces where that disagrees with the UI."""
    backend = backend or _default_find_backend
    return sorted(set(backend(folder, pattern)))


def _default_sysctl_backend(label: str) -> str:
    """The exact independent accessor ``execution/guest/flows_more.py``'s
    ``independent_value()`` uses, duplicated here (not imported) so this
    module has zero import-time dependency on the guest modules and stays
    importable with no VM present."""
    def sysctl(k):
        return subprocess.run(["/usr/sbin/sysctl", "-n", k], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    if label == "Version":
        return subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    if label == "Speicher":
        return f"{int(sysctl('hw.memsize')) // (1024 ** 3)} GB"
    if label == "Seriennummer":
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOPlatformExpertDevice", "-d", "2"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r'"IOPlatformSerialNumber" = "([^"]+)"', out)
        return m.group(1) if m else ""
    if label == "Chip":
        return sysctl("machdep.cpu.brand_string")
    return ""


def direct_read_value(label: str, backend: Optional[Callable[[str], str]] = None) -> str:
    backend = backend or _default_sysctl_backend
    return backend(label)


@dataclass(frozen=True)
class DesktopOperation:
    op_class: str
    subtype: Optional[str]
    candidate: str          # human description of the direct call
    verifier: str            # human description of the equality check
    direct: Callable[..., Any]
    hil_class: str            # how the HIL gate would classify this if wired


CLASS_OPERATIONS: Dict[Any, DesktopOperation] = {
    "arithmetic": DesktopOperation(
        "arithmetic", None, "python arithmetic ({+,-,*,/})", "calculator's displayed value",
        direct_arithmetic, "read_only"),
    "file_write": DesktopOperation(
        "file_write", None, "open(path, 'w').write(text)", "bytes on disk",
        direct_file_write, "side_effecting (a write: stays behind the HIL gate)"),
    ("directory_op", "create"): DesktopOperation(
        "directory_op", "create", "os.makedirs(path)", "os.path.isdir(path)",
        direct_directory_create, "side_effecting (a write: stays behind the HIL gate)"),
    ("directory_op", "rename"): DesktopOperation(
        "directory_op", "rename", "os.rename(src, dst)",
        "os.path.isdir(dst) and not os.path.exists(src)",
        direct_directory_rename, "side_effecting (a write: stays behind the HIL gate)"),
    "file_search": DesktopOperation(
        "file_search", None, "find <folder> -iname '*pattern*' (literal substring)",
        "set equality with the UI's reported names — proved only where this agrees; "
        " for the documented mid-word-substring mismatch",
        direct_file_search, "read_only"),
    "read_value": DesktopOperation(
        "read_value", None, "sw_vers / sysctl / ioreg", "string equality with the UI's shown value",
        direct_read_value, "read_only"),
}


def operation_for(inference: ClassInference) -> Optional[DesktopOperation]:
    if inference.op_class == "unresolved":
        return None
    return CLASS_OPERATIONS.get(inference.key()) or CLASS_OPERATIONS.get(inference.op_class)


# ---------------------------------------------------------------------------
# 4. The proof — generic over every class, never per-app. Same rules as
#    api_verify.verify_template: >= 2 distinct unseen inputs, each run
#    >= 3 times, EVERY row must agree — and the discipline osa_compile.prove
#    and blender_compile.prove already had: a declared valid start state, the
#    recording's own inputs excluded, every observation validated, the
#    expected state delta and the preserved state checked on BOTH paths, so a
#    shared failure (both paths empty, both erroring, both unchanged) is a
#    failure, never agreement.
# ---------------------------------------------------------------------------

def default_validate(obs: Any) -> Optional[str]:
    """Why ``obs`` is not a valid observation, or ``None``. ``None``, an
    ``{"error": ...}`` dict and an empty collection are invalid: an
    observation that cannot be told apart from "nothing was read" never
    counts. A class whose legitimate result can be empty (a search with no
    hits) wraps it in a dict with a completion signal instead."""
    if obs is None:
        return "empty observation"
    if isinstance(obs, dict) and obs.get("error"):
        return f"error: {obs['error']}"
    if isinstance(obs, (str, list, dict, tuple, set)) and len(obs) == 0:
        return "empty observation"
    return None


def _no_preserved_state(start: Any, inp: Any, run: int, obs: Any) -> Optional[str]:
    """Declared, not defaulted: this class reads or computes and has no state
    outside its own observation to keep intact."""
    return None


# --- per-class observation schemas / expected deltas / preserved state ------
#
# Observation shapes (both paths return the same shape):
#   arithmetic    {"display": [number, ...]}      numbers the calculator shows / [a op b]
#   file_write    {"text": str, "sentinel": str}  the target file's text, an untouched file's text
#   directory_op  {"entries": [name, ...]}        the container's listing afterwards
#   file_search   {"complete": True, "names": [...], "scope": [...]}   results + the scope's listing
#   read_value    {"value": str}

def _num_list(obs, key):
    v = obs.get(key) if isinstance(obs, dict) else None
    if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) for x in v):
        return None
    return v


def _validate_arithmetic(obs):
    bad = default_validate(obs)
    if bad:
        return bad
    return None if _num_list(obs, "display") is not None else "no numeric 'display' list"


def _expect_arithmetic(start, inp, run, obs):
    want = direct_arithmetic(*inp)
    return None if any(abs(x - want) < 1e-6 for x in obs["display"]) else \
        f"display {obs['display']!r} does not show {want!r}"


def _validate_file_write(obs):
    bad = default_validate(obs)
    if bad:
        return bad
    if not isinstance(obs, dict) or not isinstance(obs.get("text"), str):
        return "no 'text' read from the target file"
    if "sentinel" not in obs:
        return "no 'sentinel' read (the untouched file)"
    return None


def _preserve_sentinel(start, inp, run, obs):
    if obs.get("sentinel") != start.get("sentinel"):
        return f"sentinel changed: {start.get('sentinel')!r} -> {obs.get('sentinel')!r}"
    return None


def _validate_entries(obs):
    bad = default_validate(obs)
    if bad:
        return bad
    e = obs.get("entries") if isinstance(obs, dict) else None
    if not isinstance(e, list) or not all(isinstance(x, str) for x in e):
        return "no 'entries' listing"
    return None


def _validate_file_search(obs):
    bad = default_validate(obs)
    if bad:
        return bad
    if not isinstance(obs, dict) or obs.get("complete") is not True:
        return "no completion signal (a search that never finished is not an empty result)"
    if not isinstance(obs.get("names"), list) or not isinstance(obs.get("scope"), list):
        return "no 'names'/'scope' lists"
    return None


def _expect_file_search(start, inp, run, obs):
    """Every reported name lies in the scope and contains the pattern
    (case-insensitive). Completeness is NOT an expectation here — it is what
    the two paths' equality tests, which is exactly where Finder's
    Spotlight-backed search and a literal substring match part ("berry"
    inside "blueberry")."""
    scope = set(obs["scope"])
    outside = sorted(n for n in obs["names"] if n not in scope)
    if outside:
        return f"results outside the scope: {outside}"
    wrong = sorted(n for n in obs["names"] if str(inp).lower() not in n.lower())
    if wrong:
        return f"results not containing {inp!r}: {wrong}"
    return None


def _preserve_scope(start, inp, run, obs):
    if sorted(obs["scope"]) != sorted(start.get("scope") or []):
        return "the searched scope changed during a read-only search"
    return None


def _validate_read_value(obs):
    bad = default_validate(obs)
    if bad:
        return bad
    v = obs.get("value") if isinstance(obs, dict) else None
    return None if isinstance(v, str) and v.strip() else "no non-empty 'value'"


#: Validators / expectations / preserved-state checks registered per class
#: key. A class with no expectation cannot be proved at all (refused), the
#: same rule blender_compile.EXPECTATIONS applies. Classes whose expectation
#: depends on how the caller built the input (the text a flow wrote, the name
#: a folder got) are passed ``expect=`` by the caller instead.
CLASS_CHECKS: Dict[Any, Dict[str, Callable[..., Optional[str]]]] = {
    "arithmetic": {"validate": _validate_arithmetic, "expect": _expect_arithmetic,
                   "preserved": _no_preserved_state},
    "file_write": {"validate": _validate_file_write, "preserved": _preserve_sentinel},
    ("directory_op", "create"): {"validate": _validate_entries},
    ("directory_op", "rename"): {"validate": _validate_entries},
    "file_search": {"validate": _validate_file_search, "expect": _expect_file_search,
                    "preserved": _preserve_scope},
    "read_value": {"validate": _validate_read_value, "preserved": _no_preserved_state},
}


def expect_new_entry(name_for: Callable[[Any, int], str]):
    """directory_op/create's expected delta, for a caller that names each
    run's folder ``name_for(input, run)``: that entry is new, and it is the
    ONLY new entry; every starting entry is still there (preserved)."""
    def expect(start, inp, run, obs):
        before, after = set(start.get("entries") or []), set(obs["entries"])
        want = name_for(inp, run)
        new = sorted(after - before)
        if want not in after:
            return f"{want!r} not in the listing afterwards"
        if want in before:
            return f"{want!r} already existed in the declared start state"
        extra = [n for n in new if n != want]
        return f"unexpected additional entries: {extra}" if extra else None

    def preserved(start, inp, run, obs):
        gone = sorted(set(start.get("entries") or []) - set(obs["entries"]))
        return f"starting entries missing afterwards: {gone}" if gone else None
    return expect, preserved


@dataclass
class ProofRow:
    input: Any
    run: int
    ui_value: Any
    ui_ms: float
    direct_value: Any
    direct_ms: float
    agree: bool
    mismatch: Optional[str] = None
    start_observed: Any = None

    def to_dict(self):
        d = {"input": self.input, "run": self.run, "ui_value": self.ui_value, "ui_ms": self.ui_ms,
             "direct_value": self.direct_value, "direct_ms": self.direct_ms, "agree": self.agree,
             "mismatch": self.mismatch}
        if self.start_observed is not None:
            d["start_observed"] = self.start_observed
        return d


@dataclass
class ProofReport:
    op_class: str
    subtype: Optional[str]
    candidate: str
    verifier: str
    rows: List[ProofRow] = field(default_factory=list)
    proved: bool = False
    refusal_reason: Optional[str] = None

    def to_dict(self):
        return {"op_class": self.op_class, "subtype": self.subtype, "candidate": self.candidate,
                "verifier": self.verifier, "proved": self.proved, "refusal_reason": self.refusal_reason,
                "rows": [r.to_dict() for r in self.rows],
                "mismatches": [r.to_dict() for r in self.rows if not r.agree]}


def _timed(fn, *a, **kw):
    """(result, ms). An exception is an observation too — an invalid one —
    so a path that crashes fails its row instead of aborting the proof or,
    worse, being skipped."""
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        out = {"error": f"{type(exc).__name__}: {exc}"}
    return out, (time.perf_counter() - t0) * 1000.0


def _op_key(op: Any) -> Any:
    return (op.op_class, op.subtype) if getattr(op, "subtype", None) else op.op_class


def prove(op: DesktopOperation, inputs: Sequence[Any], runs_per_input: int,
         ui_fn: Callable[[Any, int], Any], direct_fn: Callable[[Any, int], Any],
         equal_fn: Optional[Callable[[Any, Any], bool]] = None, *,
         recorded_inputs: Sequence[Any],
         start_state: Any,
         expect: Optional[Callable[[Any, Any, int, Any], Optional[str]]] = None,
         preserved: Optional[Callable[[Any, Any, int, Any], Optional[str]]] = None,
         validate: Optional[Callable[[Any], Optional[str]]] = None,
         observe_start: Optional[Callable[[Any, int], Any]] = None,
         start_equal: Optional[Callable[[Any, Any], bool]] = None,
         clean_check: Optional[Callable[[Any, int], Sequence[str]]] = None) -> ProofReport:
    """Run ``ui_fn``/``direct_fn`` over every (input, run) pair and compare.

    ``ui_fn(input, run_idx)`` and ``direct_fn(input, run_idx)`` are supplied
    by the caller — in the guest, bound to a real UI flow and a real OS call
    (or, for a class with no direct backend, the contract's prediction of
    the observation from the start state and the input); on the host, fakes.
    This function contains no app-specific knowledge.

    Refuses (no row runs) when: fewer than ``MIN_FRESH_CASES`` distinct
    inputs; fewer than ``MIN_RUNS_PER_INPUT`` runs; any input was recorded or
    replayed before (``recorded_inputs`` is REQUIRED — "unseen" is checked
    here, not promised by the caller); the declared ``start_state`` is not a
    valid observation; the class has no expected delta (``expect``) or no
    preserved-state check (``preserved``; a class with nothing to preserve
    declares that explicitly). Class defaults come from ``CLASS_CHECKS``.

    A row agrees only if: the run's observed start (``observe_start``, when
    given — a reset run must reach the declared start state) is valid and
    meets ``start_state`` (``start_equal(declared, observed)``, default
    equality) — that observed start is then the row's baseline for
    ``expect``/``preserved``, so each run's delta is judged against the
    state it really started from; BOTH observations are valid; BOTH meet
    ``expect(start_state, input, run, obs)`` and
    ``preserved(start_state, input, run, obs)`` (``None`` = met); and
    ``equal_fn(ui, direct)``. So a shared failure — both paths returning
    nothing, the same error, or the unchanged start state — can never agree.
    A single disagreeing row fails the whole report and is kept, with its
    reason, in ``rows``/``mismatches`` — never dropped, never averaged away
    (Finder's "berry" row stays a failure).

    ``clean_check(input, run_idx)`` (desktop hygiene,
    ``execution/desktop_hygiene.py``): called BEFORE each row; a non-empty
    list of problems refuses that row WITHOUT calling ``ui_fn`` or
    ``direct_fn`` (nothing is actuated on a dirty state). With it, a reset
    that did not reach the declared start state (``observe_start``) also
    refuses the row before acting, instead of acting and failing afterwards."""
    def refuse(reason):
        return ProofReport(op.op_class, op.subtype, op.candidate, op.verifier, refusal_reason=reason)

    distinct = len({repr(i) for i in inputs})
    if distinct < MIN_FRESH_CASES:
        return refuse(f"only {distinct} distinct unseen input(s), need >= {MIN_FRESH_CASES}")
    if runs_per_input < MIN_RUNS_PER_INPUT:
        return refuse(f"runs_per_input={runs_per_input} < {MIN_RUNS_PER_INPUT}")
    reused = sorted({repr(i) for i in inputs} & {repr(i) for i in recorded_inputs})
    if reused:
        return refuse(f"input(s) already used by the recording or an earlier replay: {reused}")
    checks = CLASS_CHECKS.get(_op_key(op)) or CLASS_CHECKS.get(op.op_class) or {}
    validate = validate or checks.get("validate") or default_validate
    expect = expect or checks.get("expect")
    preserved = preserved or checks.get("preserved")
    bad = validate(start_state) if start_state is not None else "no declared start state"
    if bad:
        return refuse(f"start_state is not a valid observation: {bad}")
    if expect is None:
        return refuse(f"no expected state delta declared for {_op_key(op)!r}; cannot prove it")
    if preserved is None:
        return refuse(f"no preserved-state check declared for {_op_key(op)!r}; cannot prove it")
    equal_fn = equal_fn or (lambda a, b: a == b)
    start_equal = start_equal or (lambda a, b: a == b)

    def check(label, obs, inp, run, base):
        r = validate(obs)
        if r:
            return f"{label} observation invalid: {r}"
        try:
            r = expect(base, inp, run, obs) or preserved(base, inp, run, obs)
        except Exception as exc:  # noqa: BLE001  (a malformed observation fails its row)
            r = f"check raised {type(exc).__name__}: {exc}"
        return f"{label}: {r}" if r else None

    rows: List[ProofRow] = []
    for inp in inputs:
        for i in range(runs_per_input):
            started = None
            mismatch = None
            if observe_start is not None:
                started, _ms = _timed(observe_start, inp, i)
                r = validate(started)
                if r:
                    mismatch = f"reset did not reach a valid start state: {r}"
                elif not start_equal(start_state, started):  # noqa: SIM114
                    mismatch = "reset did not reach the declared start state"
            base = started if (observe_start is not None and mismatch is None) else start_state
            if clean_check is not None:
                try:
                    dirty = list(clean_check(inp, i) or [])
                except Exception as exc:  # noqa: BLE001 — unreadable is never clean
                    dirty = [f"clean-state check raised {type(exc).__name__}: {exc}"]
                if mismatch is not None:
                    dirty.append(mismatch)
                if dirty:
                    rows.append(ProofRow(inp, i, None, 0.0, None, 0.0, False,
                                         "refused before acting: dirty start state: "
                                         + "; ".join(dirty), started))
                    continue
            ui_val, ui_ms = _timed(ui_fn, inp, i)
            direct_val, direct_ms = _timed(direct_fn, inp, i)
            if mismatch is None:
                mismatch = (check("ui", ui_val, inp, i, base)
                            or check("direct", direct_val, inp, i, base))
            if mismatch is None:
                try:
                    same = bool(equal_fn(ui_val, direct_val))
                except Exception as exc:  # noqa: BLE001
                    same, mismatch = False, f"equality check raised {type(exc).__name__}: {exc}"
                if not same and mismatch is None:
                    mismatch = "ui and direct observations differ"
            rows.append(ProofRow(inp, i, ui_val, round(ui_ms, 3), direct_val, round(direct_ms, 4),
                                 mismatch is None, mismatch, started))
    return ProofReport(op.op_class, op.subtype, op.candidate, op.verifier, rows=rows,
                       proved=bool(rows) and all(r.agree for r in rows))
