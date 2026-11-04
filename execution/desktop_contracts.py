"""Reusable desktop operation classes with explicit contracts, mapped from
discovery by STRUCTURE, never by bundle id.

``execution/desktop_compile.py``'s classes (arithmetic, file_write,
directory_op, file_search, read_value) are each tied to one app by bundle id
(``infer_class`` branches on ``com.apple.calculator``, ``com.apple.TextEdit``,
...). Here each class carries its own contract instead, so that any app
whose discovered structure fits a class gets that class, and the proof
decides whether the fit was real.

Six classes. Each :class:`Contract` states:

* **identity**      what identifies the target: app (bundle id of the
                    process actually observed), window/document, element
                    (the recorded selector plus the trigger's shortcut);
* **required start** the state a run must start from (a reset run is checked
                    against it, ``desktop_compile.prove(observe_start=...)``);
* **expected delta** what a correct run on input ``x`` changes;
* **preserved**     what it must leave alone (a sentinel the run never
                    targets: another document, other options, other objects);
* **completion**    the observable signal that the operation finished (a
                    file on disk, a mark in a menu read from a fresh tree, a
                    listing read after the app was restarted).

| class | trigger (structure) | parameter | observation |
|---|---|---|---|
| ``field_rw`` | an identified text field/area, not a search field | text | the field's value from a fresh AX tree |
| ``toggle_select`` | a menu radio group: >= 2 sibling items, exactly one marked | option key | which option is marked, read with the menu open, again after a restart |
| ``scoped_find`` | ⌘F (no extra modifier) + a container (table/outline/list/browser) | pattern | result names + the scope's listing |
| ``doc_save`` | ⌘S (no extra modifier) + a text area | text | the document's bytes on disk + a sentinel document |
| ``object_create`` | a New-family shortcut (``n`` with any modifiers) + a container | name | the container's primary labels after a restart |
| ``property_update`` | an identified slider/incrementor/pop-up button | value | the control's value from a fresh tree |

These are the platform's own conventions (⌘S saves, ⌘F finds, ⌘N-family
creates, a checkmark marks the chosen option), not rules about one app. A
structural fit can be wrong — Finder's ⌘N opens a window, not an object —
and that is the proof's job to catch, not the mapper's to guess around.

Where no direct backend exists, the class is executed as parameterised AX
record/replay: record the trigger's selector once (on an input that is then
excluded from the proof), replay it on unseen inputs through
``StateGraphExecutor.execute_checked_step`` (its identity refusal), and
prove the observed effect against the contract's own prediction
(:func:`Contract.predict`) instead of a second implementation. Everything in
this module is pure: no pyobjc, no subprocess. The guest driver is
``execution/guest/desktop_gen.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple  # noqa: F401

from . import blender_compile as bc
from . import desktop_compile as dc

#: Roles that hold a collection of objects (a scope to search, a container
#: to create in).
CONTAINER_ROLES = frozenset({"AXTable", "AXOutline", "AXList", "AXBrowser"})
TEXT_FIELD_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
PROPERTY_ROLES = frozenset({"AXSlider", "AXIncrementor", "AXPopUpButton"})

#: toggle_select needs one option to record on and >= MIN_FRESH_CASES others
#: to prove on.
MIN_OPTIONS = dc.MIN_FRESH_CASES + 1


# ---------------------------------------------------------------------------
# 1. Observation schema, shared by every class
# ---------------------------------------------------------------------------
#
#   {"identity": {"bundle_id": str, "window": str|None, "document": str|None},
#    "complete": True,        # the class's completion signal was observed
#    "target":   <class-specific>,
#    "sentinel": <class-specific, what must be preserved>}

def validate_common(obs: Any) -> Optional[str]:
    bad = dc.default_validate(obs)
    if bad:
        return bad
    if not isinstance(obs, dict):
        return f"observation is {type(obs).__name__}, not a dict"
    ident = obs.get("identity")
    if not isinstance(ident, dict) or not ident.get("bundle_id"):
        return "no observed app identity"
    if obs.get("complete") is not True:
        return "completion signal not observed"
    if "target" not in obs or "sentinel" not in obs:
        return "no 'target'/'sentinel' read"
    return None


def _identity_ok(start: Dict[str, Any], obs: Dict[str, Any]) -> Optional[str]:
    """The observation was taken on the declared app (and document, when the
    start state names one)."""
    s, o = start.get("identity") or {}, obs.get("identity") or {}
    if s.get("bundle_id") != o.get("bundle_id"):
        return f"observed app {o.get('bundle_id')!r}, declared {s.get('bundle_id')!r}"
    if s.get("document") and s.get("document") != o.get("document"):
        return f"observed document {o.get('document')!r}, declared {s.get('document')!r}"
    return None


def side_effect_allowed(item: str, allowed: Sequence[str]) -> bool:
    """Does a declared side effect cover this sentinel item? A sentinel item
    is ``identity`` or ``parent>identity`` (an item inside a submenu). A
    declaration is an item identity, or ``parent>*``: every item of that
    submenu, including items the menu shows only in some states."""
    base = item.split(">", 1)[-1]
    for a in allowed:
        if a.endswith(">*"):
            if item.startswith(a[:-1]):
                return True
        elif a == item or a == base:
            return True
    return False


def _sentinel_ok(start, obs, allowed: Sequence[str] = ()) -> Optional[str]:
    """The sentinel is unchanged, except for DECLARED coupled settings:
    when the sentinel is a list of setting identities
    (the other marked menu items), a change confined to ``allowed`` is the
    contract's declared side effect; any other change is still a violation."""
    a, b = start.get("sentinel"), obs.get("sentinel")
    if a == b:
        return None
    if allowed and isinstance(a, list) and isinstance(b, list):
        diff = set(a) ^ set(b)
        undeclared = sorted(x for x in diff if not side_effect_allowed(x, allowed))
        if not undeclared:
            return None
        return f"undeclared side effect on {undeclared} (sentinel {a!r} -> {b!r})"
    return f"sentinel changed: {a!r} -> {b!r}"


def _predict_with(target_fn):
    def predict(start, inp, run):
        out = {"identity": dict(start.get("identity") or {}), "complete": True,
               "target": target_fn(start, inp, run), "sentinel": start.get("sentinel")}
        if "model" in start:
            out["model"] = start.get("model")
        return out
    return predict


# ---------------------------------------------------------------------------
# 2. Contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Contract:
    name: str
    effect: str                       # "read" | "write" | "create"
    identity: str
    required_start: str
    expected_delta: str
    preserved: str
    completion: str
    parameter: str
    validate_target: Callable[[Any], Optional[str]]
    expect_target: Callable[[Any, Any, int, Any], Optional[str]]
    preserve_target: Callable[[Any, Any, int, Any], Optional[str]]
    predict: Callable[[Any, Any, int], Any]
    #: how two observations are compared (default: target + sentinel equal)
    equal: Optional[Callable[[Any, Any], bool]] = None
    #: does an observed run start meet the declared one (default: equal)
    start_ok: Optional[Callable[[Any, Any], bool]] = None

    # -- the three callables desktop_compile.prove takes --------------------
    def validate(self, obs: Any) -> Optional[str]:
        return validate_common(obs) or self.validate_target(obs["target"])

    def expect(self, start, inp, run, obs) -> Optional[str]:
        return _identity_ok(start, obs) or self.expect_target(start, inp, run, obs)

    def preserve(self, start, inp, run, obs, allowed: Sequence[str] = ()) -> Optional[str]:
        return _sentinel_ok(start, obs, allowed) or self.preserve_target(start, inp, run, obs)

    def equal_fn(self, a, b) -> bool:
        if self.equal is not None:
            return self.equal(a, b)
        return a.get("target") == b.get("target") and a.get("sentinel") == b.get("sentinel")

    def equal_with(self, allowed: Sequence[str] = ()):
        """Equality in which declared coupled settings may differ in the
        sentinel (the prediction cannot know where a coupled setting lands;
        the preserved check has already confined the change to them)."""
        if not allowed:
            return self.equal_fn

        def eq(a, b):
            sa, sb = a.get("sentinel"), b.get("sentinel")
            if isinstance(sa, list) and isinstance(sb, list):
                a2 = dict(a, sentinel=sorted(x for x in sa if not side_effect_allowed(x, allowed)))
                b2 = dict(b, sentinel=sorted(x for x in sb if not side_effect_allowed(x, allowed)))
                return self.equal_fn(a2, b2)
            return self.equal_fn(a, b)
        return eq

    def start_meets_with(self, allowed: Sequence[str] = ()):
        if not allowed:
            return self.start_meets

        def ok(declared, observed):
            return self.start_meets(dict(declared, sentinel=None), dict(observed, sentinel=None)) and \
                _sentinel_ok(declared, observed, allowed) is None
        return ok

    def start_meets(self, declared, observed) -> bool:
        if self.start_ok is not None:
            return self.start_ok(declared, observed)
        return all(declared.get(k) == observed.get(k) for k in ("identity", "target", "sentinel"))

    def operation(self, candidate: str) -> dc.DesktopOperation:
        return dc.DesktopOperation(self.name, None, candidate, self.completion, None,
                                   "read_only" if self.effect == "read"
                                   else "side_effecting (stays behind the HIL gate)")

    def to_dict(self):
        return {k: getattr(self, k) for k in ("name", "effect", "identity", "required_start",
                                              "expected_delta", "preserved", "completion", "parameter")}


def _str_target(t):
    return None if isinstance(t, str) else "target is not a string"


def _nothing(*_a):
    return None


# field_rw ------------------------------------------------------------------

def _fr_expect(start, inp, run, obs):
    return None if obs["target"] == str(inp) else f"field reads {obs['target']!r}, wrote {str(inp)!r}"


FIELD_RW = Contract(
    name="field_rw", effect="write",
    identity="app + window + the field's recorded selector (identifier)",
    required_start="the field shows the declared start value",
    expected_delta="the field's value equals the input",
    preserved="every other identified field in the window keeps its value (sentinel)",
    completion="the value read from a fresh AX tree after the write",
    parameter="text",
    validate_target=_str_target, expect_target=_fr_expect, preserve_target=_nothing,
    predict=_predict_with(lambda s, i, r: str(i)))


# toggle_select ---------------------------------------------------------------

def _ts_validate(t):
    if not isinstance(t, dict) or not isinstance(t.get("options"), list) \
            or not isinstance(t.get("selected"), list):
        return "target needs 'options' and 'selected' lists"
    if len(t["options"]) < 2:
        return "fewer than two options observed"
    return None


def _ts_expect(start, inp, run, obs):
    t = obs["target"]
    if inp not in t["options"]:
        return f"option {inp!r} not among the observed options"
    if t["selected"] != [inp]:
        return f"selected {t['selected']!r}, want exactly [{inp!r}]"
    return None


def _ts_preserve(start, inp, run, obs):
    if sorted(obs["target"]["options"]) != sorted(start["target"]["options"]):
        return "the option group changed"
    return None


TOGGLE_SELECT = Contract(
    name="toggle_select", effect="write",
    identity="app + the option's menu item (identifier and shortcut) inside its group",
    required_start="the declared start option is the one marked",
    expected_delta="exactly the requested option is marked",
    preserved="the group's options; every other menu mark (sentinel)",
    completion="the marks read with the menu open, from a fresh tree, after an app restart",
    parameter="option key",
    validate_target=_ts_validate, expect_target=_ts_expect, preserve_target=_ts_preserve,
    predict=_predict_with(lambda s, i, r: {"options": list(s["target"]["options"]), "selected": [i]}))


# flag_toggle -----------------------------------------------------------------
#
# A run of checkmarked menu items whose marks flip one at a
# time (a checkbox group) is not an option group. Its contract: pressing
# option ``x`` flips x's mark and no other mark in the group.

def _ft_validate(t):
    if not isinstance(t, dict) or not isinstance(t.get("options"), list) \
            or not isinstance(t.get("marked"), list):
        return "target needs 'options' and 'marked' lists"
    if len(t["options"]) < 2:
        return "fewer than two flags observed"
    return None


def _ft_want(start, inp):
    return sorted(set(start["target"]["marked"]) ^ {inp})


def _ft_expect(start, inp, run, obs):
    t = obs["target"]
    if inp not in t["options"]:
        return f"flag {inp!r} not among the observed flags"
    want = _ft_want(start, inp)
    return None if sorted(t["marked"]) == want else f"marked {sorted(t['marked'])!r}, want {want!r}"


FLAG_TOGGLE = Contract(
    name="flag_toggle", effect="write",
    identity="app + the flag's menu item (identifier and shortcut) inside its group",
    required_start="the declared flags are the marked ones",
    expected_delta="exactly the requested flag flips; the group's other flags keep their marks",
    preserved="the group's flags; every other menu mark except declared coupled settings (sentinel)",
    completion="the marks read with the menu open, from a fresh tree",
    parameter="flag key",
    validate_target=_ft_validate, expect_target=_ft_expect, preserve_target=_ts_preserve,
    predict=_predict_with(lambda s, i, r: {"options": list(s["target"]["options"]),
                                           "marked": _ft_want(s, i)}))


# scoped_find -----------------------------------------------------------------

def _sf_validate(t):
    if not isinstance(t, dict) or not isinstance(t.get("names"), list) \
            or not isinstance(t.get("scope"), list):
        return "target needs 'names' and 'scope' lists"
    return None


def rows_outside(t) -> Optional[str]:
    """A result view whose ROW count exceeds its distinct names
    lists same-named items from outside the scope (a name-only comparison
    would not see them)."""
    rows = t.get("rows")
    if isinstance(rows, int) and rows != len(set(t.get("names") or [])):
        return (f"results outside the scope: {rows} result rows for {len(set(t['names']))} "
                f"distinct names (same-named items elsewhere)")
    return None


def _sf_expect(start, inp, run, obs):
    t = obs["target"]
    outside = sorted(set(t["names"]) - set(t["scope"]))
    if outside:
        return f"results outside the scope: {outside}"
    if rows_outside(t):
        return rows_outside(t)
    wrong = sorted(n for n in t["names"] if str(inp).lower() not in n.lower())
    return f"results not containing {inp!r}: {wrong}" if wrong else None


def _sf_preserve(start, inp, run, obs):
    if sorted(obs["target"]["scope"]) != sorted(start["target"]["scope"]):
        return "the searched scope changed during a read-only search"
    return None


SCOPED_FIND = Contract(
    name="scoped_find", effect="read",
    identity="app + the window showing the scope (its document/folder)",
    required_start="the scope is shown, no search active",
    expected_delta="none (a read); the results are the scope's names containing the pattern",
    preserved="the scope's listing (sentinel: its names)",
    completion="the result view replaced the plain listing (a finished search)",
    parameter="pattern",
    validate_target=_sf_validate, expect_target=_sf_expect, preserve_target=_sf_preserve,
    # the contract's matching semantics: literal, case-insensitive substring.
    # An app whose own search tokenises (Finder: Spotlight) disagrees on a
    # mid-word pattern, and the proof reports that row as a failure.
    predict=_predict_with(lambda s, i, r: {
        "names": sorted(n for n in s["target"]["scope"] if str(i).lower() in n.lower()),
        "scope": list(s["target"]["scope"])}),
    equal=lambda a, b: sorted(a["target"]["names"]) == sorted(b["target"]["names"])
    and a.get("sentinel") == b.get("sentinel"))


# doc_save ----------------------------------------------------------------------

def _ds_validate(t):
    return None if isinstance(t, dict) and isinstance(t.get("text"), str) else "no document text read"


def _ds_expect(start, inp, run, obs):
    got = obs["target"]["text"].rstrip("\n")
    if got == str(inp):
        return None
    return f"document holds {got!r}, want {str(inp)!r}"


DOC_SAVE = Contract(
    name="doc_save", effect="write",
    identity="app + the window whose AXDocument is the target file + the text area's selector",
    required_start="the target document is open, holding the declared seed text",
    expected_delta="the file on disk holds exactly the input text",
    preserved="a second open document (opened later, so in front) is untouched on disk (sentinel)",
    completion="the bytes on disk after the Save item fired",
    parameter="text",
    validate_target=_ds_validate, expect_target=_ds_expect, preserve_target=_nothing,
    predict=_predict_with(lambda s, i, r: {"text": str(i)}),
    equal=lambda a, b: a["target"]["text"].rstrip("\n") == b["target"]["text"].rstrip("\n")
    and a.get("sentinel") == b.get("sentinel"))


# object_create -----------------------------------------------------------------

def _oc_validate(t):
    if not isinstance(t, dict) or not isinstance(t.get("names"), list):
        return "target needs a 'names' list"
    return None


def object_name(inp: Any, run: int) -> str:
    """object_create's input is a base name; run ``r`` names its object
    ``<base>-<r>``, so a reset run never collides with an earlier run's
    object and never has to delete one through the UI."""
    return f"{inp}-{run}"


def _sentinel_moved(start, obs) -> Optional[str]:
    """How the sentinel changed, briefly (list sentinels: the entries that
    appeared and went); None when it did not."""
    a, b = start.get("sentinel"), obs.get("sentinel")
    if a == b:
        return None
    if isinstance(a, list) and isinstance(b, list):
        return f"sentinel changed: new {sorted(set(b) - set(a))[:10]}, gone {sorted(set(a) - set(b))[:10]}"
    return f"sentinel changed: {a!r} -> {b!r}"


def _nfc_names(names) -> set:
    """Object names in Unicode NFC: Finder lists a folder it created decomposed (task GG05: 'Rückfragen' as
    'Ru\u0308ckfragen'); every name comparison of the object checks compares composed."""
    import unicodedata
    return {unicodedata.normalize("NFC", n) if isinstance(n, str) else n for n in names or []}


def _oc_expect(start, inp, run, obs):
    import unicodedata
    want = unicodedata.normalize("NFC", object_name(inp, run))
    before, after = _nfc_names(start["target"]["names"]), _nfc_names(obs["target"]["names"])
    if want in before:
        return f"{want!r} already existed at the start of the run"
    if want not in after:
        # Finder can put all six folders INTO a selected subfolder; the
        # folder model's sentinel (entries inside the subfolders) shows where
        # an object went that the scope's own listing does not contain: a
        # wrong-target row, not merely a miss
        moved = _sentinel_moved(start, obs)
        return f"no object named {want!r} afterwards" + (f"; {moved}" if moved else "")
    extra = sorted(after - before - {want})
    if extra:
        return f"unexpected additional objects: {extra}"
    # where the observer counts items (not distinct names), exactly
    # one more item (two objects with the same name would collapse in a set)
    c0, c1 = start["target"].get("count"), obs["target"].get("count")
    if isinstance(c0, int) and isinstance(c1, int) and c1 != c0 + 1:
        return f"unexpected additional objects: {c1 - c0} new items, want exactly 1"
    return None


def _oc_start_ok(declared, observed):
    """A reset run of object_create starts from the declared objects plus
    whatever earlier runs created (their names are run-suffixed)."""
    return (declared.get("identity") == observed.get("identity")
            and declared.get("sentinel") == observed.get("sentinel")
            and _nfc_names(declared["target"]["names"]) <= _nfc_names(observed["target"]["names"]))


# ---------------------------------------------------------------------------
# Inputs for field_rw / property_update, derived from the control
# ---------------------------------------------------------------------------

#: slider positions (fractions of [min, max]) in the order they are used: the
#: first three that differ from the current value are the recording and the
#: two unseen inputs
SLIDER_FRACTIONS = (0.25, 0.5, 0.75, 0.375, 0.625)


def slider_inputs(lo: Any, hi: Any, cur: Any) -> Tuple[Optional[Any], List[Any], str]:
    """(recording value, unseen values, why) for a slider with range
    [lo, hi] currently at ``cur``: the values at SLIDER_FRACTIONS of the
    range, whole numbers when the range and the current value are whole.
    A value equal to the current one is skipped (it would not change)."""
    try:
        lo, hi, cur = float(lo), float(hi), float(cur)
    except (TypeError, ValueError):
        return None, [], "slider range not readable"
    if not hi > lo:
        return None, [], f"empty slider range [{lo}, {hi}]"
    whole = all(abs(v - round(v)) < 1e-9 for v in (lo, hi, cur))
    vals = []
    for f in SLIDER_FRACTIONS:
        v = lo + (hi - lo) * f
        v = float(int(v + 0.5)) if whole else round(v, 4)
        if abs(v - cur) > 1e-9 and v not in vals and len(vals) < 1 + dc.MIN_FRESH_CASES:
            vals.append(v)
    if len(vals) < 1 + dc.MIN_FRESH_CASES:
        return None, [], f"only {len(vals)} distinct slider values"
    return vals[0], vals[1:1 + dc.MIN_FRESH_CASES], "slider range fractions"


def option_inputs(options: Sequence[str], current: Optional[str]) -> Tuple[Optional[str], List[str], str]:
    """(recording option, unseen options, why) for a pop-up button: the
    option after the current one, then the next two (cyclic), as for
    toggle_select."""
    opts = [o for o in options if o]
    if len(opts) != len(set(opts)):
        return None, [], "pop-up options are not distinct"
    if current not in opts:
        return None, [], f"current value {current!r} is not among the options"
    if len(opts) < 1 + dc.MIN_FRESH_CASES + 1:
        return None, [], f"{len(opts)} options: one to record on leaves fewer than {dc.MIN_FRESH_CASES} unseen"
    i = opts.index(current)
    seq = [opts[(i + k) % len(opts)] for k in range(1, len(opts))]
    return seq[0], seq[1:1 + dc.MIN_FRESH_CASES], "pop-up options after the current one"


def model_preserved(start_model: Any, obs_model: Any) -> Optional[str]:
    """Preserved state read through the app's model, not the viewport.
    ``{"items": [...]}`` (a backend listing, e.g. the
    folder on disk): every starting item is still there. ``{"scopes":
    {label: count}}`` (a navigation container's rows with object-count
    badges, read from all rows): every starting scope still exists and no
    scope holds fewer objects than at the start. None = nothing to compare
    (then the caller falls back to the view)."""
    if not isinstance(start_model, dict) or not isinstance(obs_model, dict):
        return "no model reading" if isinstance(start_model, dict) else None
    probs = []
    if isinstance(start_model.get("items"), list):
        if not isinstance(obs_model.get("items"), list):
            return "model listing not read afterwards"
        gone = sorted(set(start_model["items"]) - set(obs_model["items"]))
        if gone:
            probs.append(f"starting objects missing from the model: {gone}")
    if isinstance(start_model.get("scopes"), dict):
        after = obs_model.get("scopes")
        if not isinstance(after, dict):
            return "model scopes not read afterwards"
        lost = sorted(k for k in start_model["scopes"] if k not in after)
        fewer = sorted(k for k, n in start_model["scopes"].items() if k in after and after[k] < n)
        if lost:
            probs.append(f"scopes missing afterwards: {lost}")
        if fewer:
            probs.append(f"scopes lost objects: {[(k, start_model['scopes'][k], after[k]) for k in fewer]}")
    return "; ".join(probs) or None


def _oc_preserve(start, inp, run, obs):
    if isinstance(start.get("model"), dict):
        return model_preserved(start.get("model"), obs.get("model"))
    gone = sorted(_nfc_names(start["target"]["names"]) - _nfc_names(obs["target"]["names"]))
    return f"starting objects missing afterwards: {gone}" if gone else None


OBJECT_CREATE = Contract(
    name="object_create", effect="create",
    identity="app + the container shown in the front window + the trigger's menu item",
    required_start="the container shows the declared objects",
    expected_delta="exactly one new object, named by the input",
    preserved="every starting object is still there (sentinel: a seeded object)",
    completion="the container's listing read after the app (or its window) was restarted",
    parameter="base name (run r creates '<base>-<r>')",
    validate_target=_oc_validate, expect_target=_oc_expect, preserve_target=_oc_preserve,
    predict=_predict_with(lambda s, i, r: dict({"names": sorted(set(s["target"]["names"]) | {object_name(i, r)})},
                                               **({"count": s["target"]["count"] + 1}
                                                  if isinstance(s["target"].get("count"), int) else {}))),
    # the model is not part of the equality: the prediction cannot know the
    # counts, and model_preserved already judged it on each path
    equal=lambda a, b: sorted(set(a["target"]["names"])) == sorted(set(b["target"]["names"]))
    and a.get("sentinel") == b.get("sentinel"),
    start_ok=_oc_start_ok)


# property_update ---------------------------------------------------------------

def _pu_validate(t):
    return None if isinstance(t, dict) and isinstance(t.get("value"), (int, float, str)) \
        else "no property value read"


def _pu_expect(start, inp, run, obs):
    got = obs["target"]["value"]
    try:
        ok = abs(float(got) - float(inp)) < 1e-6
    except (TypeError, ValueError):
        ok = str(got) == str(inp)
    return None if ok else f"property reads {got!r}, set {inp!r}"


PROPERTY_UPDATE = Contract(
    name="property_update", effect="write",
    identity="app + window + the control's recorded selector (identifier)",
    required_start="the control shows the declared start value",
    expected_delta="the property equals the input",
    preserved="every other identified control keeps its value (sentinel)",
    completion="the value read from a fresh tree after the window was reopened",
    parameter="value",
    validate_target=_pu_validate, expect_target=_pu_expect, preserve_target=_nothing,
    predict=_predict_with(lambda s, i, r: {"value": i}),
    equal=lambda a, b: _pu_expect(None, a["target"]["value"], 0, b) is None
    and a.get("sentinel") == b.get("sentinel"))


def scoped_find_contract(semantics: str, match: Optional[Callable[[Sequence[str], str], List[str]]] = None
                         ) -> Contract:
    """scoped_find with its completeness semantics stated.
    ``semantics`` names the matching rule the app was measured to use
    (``desktop_effects.classify_semantics``); ``match`` is the pure rule
    when one exists. The expected delta is: results inside the scope, each
    satisfying the rule (checkable only with a pure rule). The direct side of
    the proof must be a backend with the SAME rule; equality is exact."""
    def expect(start, inp, run, obs):
        t = obs["target"]
        outside = sorted(set(t["names"]) - set(t["scope"]))
        if outside:
            return f"results outside the scope: {outside}"
        if rows_outside(t):
            return rows_outside(t)
        if match is not None:
            want = match(t["scope"], str(inp))
            wrong = sorted(set(t["names"]) - set(want))
            if wrong:
                return f"results that do not satisfy the {semantics} rule for {inp!r}: {wrong}"
        return None

    predict = None
    if match is not None:
        predict = _predict_with(lambda s, i, r: {"names": sorted(match(s["target"]["scope"], str(i))),
                                                 "scope": list(s["target"]["scope"])})
    return Contract(
        name="scoped_find", effect="read",
        identity=SCOPED_FIND.identity, required_start=SCOPED_FIND.required_start,
        expected_delta=f"none (a read); the results are the scope's items matching the {semantics} rule",
        preserved=SCOPED_FIND.preserved, completion=SCOPED_FIND.completion,
        parameter=f"pattern ({semantics} semantics)",
        validate_target=_sf_validate, expect_target=expect, preserve_target=_sf_preserve,
        predict=predict or (lambda s, i, r: {"error": f"no pure backend for {semantics}"}),
        equal=SCOPED_FIND.equal)


CONTRACTS: Dict[str, Contract] = {c.name: c for c in (
    FIELD_RW, TOGGLE_SELECT, SCOPED_FIND, DOC_SAVE, OBJECT_CREATE, PROPERTY_UPDATE, FLAG_TOGGLE)}


def prove_contract(contract: Contract, candidate: str, inputs: Sequence[Any], runs: int,
                   ui_fn: Callable[[Any, int], Any], *, recorded_inputs: Sequence[Any],
                   start_state: Any, direct_fn: Optional[Callable[[Any, int], Any]] = None,
                   observe_start: Optional[Callable[[Any, int], Any]] = None,
                   side_effects: Sequence[str] = (),
                   clean_check: Optional[Callable[[Any, int], Sequence[str]]] = None
                   ) -> dc.ProofReport:
    """``desktop_compile.prove`` with the contract's schema, delta and
    preserved-state checks. ``direct_fn`` is the class's direct backend where
    one exists; otherwise the contract's own prediction from the declared
    start state and the input (the AX record/replay case).

    ``side_effects``: the contract's DECLARED coupled settings,
    identities that may change in the sentinel (a view mode that re-marks
    the sort order). They are part of the contract, listed in the report,
    and an undeclared change is still a preserved-state violation.

    ``clean_check`` (``execution/desktop_hygiene.py``): the declared clean
    start state, checked before every row; a dirty state refuses the row
    without acting."""
    seen: Dict[str, Any] = {}
    allowed = tuple(side_effects or ())
    start_meets = contract.start_meets_with(allowed)

    def observed_start(inp, run):
        seen["start"] = None
        obs = observe_start(inp, run)
        if contract.validate(obs) is None and start_meets(start_state, obs):
            seen["start"] = obs
        return obs

    def predicted(inp, run):
        return contract.predict(seen.get("start") or start_state, inp, run)

    def preserved(start, inp, run, obs):
        return contract.preserve(start, inp, run, obs, allowed)

    return dc.prove(contract.operation(candidate), inputs, runs, ui_fn, direct_fn or predicted,
                    equal_fn=contract.equal_with(allowed), recorded_inputs=recorded_inputs,
                    start_state=start_state, expect=contract.expect,
                    preserved=preserved, validate=contract.validate,
                    observe_start=observed_start if observe_start else None,
                    start_equal=start_meets, clean_check=clean_check)


# ---------------------------------------------------------------------------
# 3. Mapping discovered candidates to contracts, by structure
# ---------------------------------------------------------------------------

@dataclass
class ContractCandidate:
    id: str
    bundle_id: str
    contract: str
    triggers: List[str]                  # discovery keys (identifier / shortcut / idname)
    recipe: Dict[str, Any] = field(default_factory=dict)
    provable: bool = True
    note: str = ""

    def to_dict(self):
        return {"id": self.id, "bundle_id": self.bundle_id, "contract": self.contract,
                "triggers": list(self.triggers), "recipe": self.recipe,
                "provable": self.provable, "note": self.note}


def _mods(item: Dict[str, Any]) -> str:
    m = item.get("cmd_mods")
    return "0" if m in (None, "") else str(m).strip()


def _leaf_menu(dump: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The menu items discovery keeps (desktop_discover.menu_candidates'
    rules: no Apple menu, no Services subtree, no submenu parents, no
    separators), each with its discovery key."""
    from . import desktop_discover as dd
    cands = dd.menu_candidates(dump.get("bundle_id") or "", dump.get("menu_items") or [])
    by_path = {tuple(c.extra["index_path"]): c for c in cands}
    out = []
    for it in dump.get("menu_items") or []:
        c = by_path.get(tuple(it.get("index_path") or []))
        if c is not None:
            out.append({**it, "key": c.key, "key_kind": c.key_kind})
    return out


def _trigger_recipe(it: Dict[str, Any]) -> Dict[str, Any]:
    return {"key": it["key"], "identifier": it.get("identifier") or "",
            "cmd_char": (it.get("cmd_char") or "").lower(), "cmd_mods": _mods(it),
            "index_path": list(it.get("index_path") or [])}


def _controls(dump):
    return [c for c in dump.get("window_controls") or []]


def _has_container(dump) -> Optional[Dict[str, Any]]:
    return next((c for c in _controls(dump) if c.get("role") in CONTAINER_ROLES), None)


def map_contracts(dump: Dict[str, Any]) -> Tuple[List[ContractCandidate], Dict[str, Any]]:
    """Every contract candidate one app's discovery dump supports, plus the
    counts the mapping rate is computed from. Structure only: shortcuts,
    roles, subroles, identifiers and menu marks. The bundle id is carried
    along for reporting and never consulted."""
    bid = dump.get("bundle_id") or ""
    menu = _leaf_menu(dump)
    # AXEnabled is STATE, not structure (TextEdit's Save is disabled until the
    # document changes; Finder's "New Folder with Selection" until something
    # is selected), so it never decides a mapping.
    enabled = menu
    ctrls = _controls(dump)
    container = _has_container(dump)
    out: List[ContractCandidate] = []

    def add(contract, triggers, recipe, provable=True, note=""):
        cid = f"{bid}:{contract}:{'+'.join(triggers)}"
        out.append(ContractCandidate(cid, bid, contract, list(triggers), recipe, provable, note))

    # doc_save: ⌘S + a text area
    area = next((c for c in ctrls if c.get("role") == "AXTextArea"), None)
    for m in enabled:
        if (m.get("cmd_char") or "").lower() == "s" and _mods(m) == "0" and area is not None:
            add("doc_save", [m["key"]], {"trigger": _trigger_recipe(m),
                                         "entry": {"role": "AXTextArea",
                                                   "identifier": area.get("identifier") or ""}})
    # object_create: New-family shortcut + a container
    for m in enabled:
        if (m.get("cmd_char") or "").lower() == "n" and container is not None:
            add("object_create", [m["key"]], {"trigger": _trigger_recipe(m),
                                              "container_role": container.get("role"),
                                              "entry": "focused text entry after the trigger",
                                              "commit": "return"})
    # scoped_find: ⌘F + a container
    for m in enabled:
        if (m.get("cmd_char") or "").lower() == "f" and _mods(m) == "0" and container is not None:
            add("scoped_find", [m["key"]], {"trigger": _trigger_recipe(m),
                                            "container_role": container.get("role"),
                                            "entry": "focused search field after the trigger",
                                            "commit": "return"})
    # toggle_select: radio groups among sibling menu items
    have_marks = any("mark_char" in m for m in dump.get("menu_items") or [])
    groups: Dict[Tuple[int, ...], List[Dict[str, Any]]] = {}
    if have_marks:
        runs: List[List[Dict[str, Any]]] = []
        raw = sorted(dump.get("menu_items") or [], key=lambda m: m.get("index_path") or [])
        keyed = {tuple(m["index_path"]): m for m in menu}
        cur: List[Dict[str, Any]] = []
        prev = None
        for it in raw:
            p = tuple(it.get("index_path") or [])
            k = keyed.get(p)
            contiguous = prev is not None and p[:-1] == prev[:-1] and p[-1] == prev[-1] + 1
            if k is None or not contiguous:
                if cur:
                    runs.append(cur)
                cur = [k] if k is not None else []
            else:
                cur.append(k)
            prev = p
        if cur:
            runs.append(cur)
        for r in runs:
            marked = [m for m in r if (m.get("mark_char") or "").strip()]
            if len(r) >= 2 and len(marked) == 1:
                groups[tuple(r[0]["index_path"])] = r
    for start, r in sorted(groups.items()):
        opts = [_trigger_recipe(m) for m in r]
        add("toggle_select", [m["key"] for m in r],
            {"options": opts, "parent_index_path": list(start[:-1])},
            provable=len(r) >= MIN_OPTIONS,
            note="" if len(r) >= MIN_OPTIONS else
            f"{len(r)} options: one to record on leaves fewer than {dc.MIN_FRESH_CASES} unseen")
    # field_rw: identified, enabled text entry that is not a search field
    seen = set()
    for c in ctrls:
        if c.get("role") in TEXT_FIELD_ROLES and c.get("identifier") \
                and c.get("subrole") != "AXSearchField" and c["identifier"] not in seen:
            seen.add(c["identifier"])
            add("field_rw", [f"id:{c['identifier']}"], {"role": c["role"], "identifier": c["identifier"]})
    # property_update: identified slider / incrementor / pop-up button
    for c in ctrls:
        if c.get("role") in PROPERTY_ROLES and c.get("identifier"):
            add("property_update", [f"id:{c['identifier']}"], {"role": c["role"],
                                                                "identifier": c["identifier"]})
    # object_create through an app's own API: a blender_compile shape whose
    # first operator adds an object (the shape table is data; see bc.KNOWN_SHAPES)
    if dump.get("bpy_catalog") is not None:
        from . import desktop_discover as dd
        cands = dd.bpy_candidates(bid, dump["bpy_catalog"])
        for hit in dd.rule_app_script(bid, cands):
            seq = [c.extra.get("idname") for c in hit.triggers]
            if seq and "_add" in (seq[0] or ""):
                add("object_create", [c.key for c in hit.triggers],
                    {"backend": "bpy", "shape": hit.subtype, "sequence": seq})

    counts = mapping_counts(dump, out, have_marks)
    return out, counts


def mapping_counts(dump, cands: Sequence[ContractCandidate], have_marks: bool) -> Dict[str, Any]:
    from . import desktop_discover as dd
    disc = dd.discover_app(dump)
    all_keys = {c.key for c in disc.candidates}
    used = set()
    for c in cands:
        used.update(k for k in c.triggers if k in all_keys)
    title_keyed = [k for c in cands for k in c.triggers if k.startswith("pos:")]
    return {"discovered": len(disc.candidates), "source_candidates_mapped": len(used),
            "mapping_rate": round(len(used) / len(disc.candidates), 4) if disc.candidates else 0.0,
            "contract_candidates": len(cands),
            "provable_candidates": sum(1 for c in cands if c.provable),
            "by_contract": {n: sum(1 for c in cands if c.contract == n) for n in CONTRACTS},
            "menu_marks_in_dump": have_marks,
            "triggers_keyed_by_position": len(title_keyed)}
