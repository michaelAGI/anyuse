"""Regression: the PRODUCT executor's ``_resolve_target()`` must refuse a
wrong target, not guess one.

The VM test harness (`execution/guest/flows.py`'s
``Replayer``) resolves a recorded step with the configured matcher, then
independently checks the result against the recorded identity and REFUSES a
mismatch before anything is pressed (``same_identity()``). The product's own
``execution/executor.py`` ``StateGraphExecutor._execute_ax()`` →
``_resolve_target()`` did not: on a miss (or a matcher abstention) it fell
back to "the first element of this role/title", unscoped and unchecked — a
wrong-target press. Every "0 wrong-target actions" number measured
that way was therefore a property of the harness, not of
the shipped executor.

This file proves the fix with the SAME guest-captured Calculator AX fixture
(`testdata/ax/calculator_ax_raw.json`, real ``AXDescription``-only labels,
real ``AXIdentifier``s) ``test_ax_description_label.py`` uses, so the identity check runs
against real captured AX data rather than a synthetic stand-in. Every test
here FAILS on the pre-fix ``_resolve_target()`` (a plain
``els[0] if els else None`` fallback) and PASSES after.

Run from the anyuse package dir:
    cd anyuse && python3 test_execution_resolve_target_identity.py
"""

import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))  # flat: import config

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, (
    "resolved 'anyuse' to the wrong checkout — (D9)")

from anyuse.execution.executor import StateGraphExecutor  # noqa: E402
from anyuse.execution.planner import PlannedStep  # noqa: E402
from anyuse.context.accessibility import AXElementDesc  # noqa: E402

FIXTURES = _HERE / "testdata" / "ax"


def load(name):
    data = json.loads((FIXTURES / f"{name}_ax_raw.json").read_text())
    return data["meta"], data["elements"]


def _as_desc(row):
    d = row.get("AXDescription")
    return AXElementDesc(
        role=row.get("AXRole"), title=row.get("AXTitle"), value=row.get("AXValue"),
        identifier=row.get("AXIdentifier"),
        ax_description=None if d in (None, "None") else d,
    )


class _FixtureAxTree:
    """Stands in for ``context.accessibility.AccessibilityTree`` over a fixed
    list of raw AX rows (dicts double as "element refs" — a row IS its own
    ref here). ``resolve_mode`` selects which resolution ``find_selector``
    returns, so a single fixture drives every scenario:

      "wrong"   — always the FIRST AXButton in the tree, whatever was asked
                  for. This is the exact bug: the cascade/weighted
                  matcher abstained or missed, and the old code fell back to
                  "the first element of this role".
      "correct" — a real lookup by AXIdentifier, i.e. a resolution that
                  really is the recorded element.
      "none"    — nothing (the matcher abstained below its margin / found no
                  candidate at all).
    """

    def __init__(self, rows, resolve_mode="correct"):
        self.rows = rows
        self.resolve_mode = resolve_mode
        self.find_calls = []
        self.pressed = []

    def to_selector(self, row):
        return _as_desc(row).to_selector()

    def find_selector(self, selector):
        buttons = [r for r in self.rows if r.get("AXRole") == "AXButton"]
        if self.resolve_mode == "none":
            return None
        if self.resolve_mode == "wrong":
            return buttons[0] if buttons else None
        for r in buttons:
            if (r.get("AXIdentifier") or "") == selector.identifier:
                return r
        return None

    def find(self, role=None, title=None):
        self.find_calls.append((role, title))
        return [r for r in self.rows
                if (role is None or r.get("AXRole") == role)
                and (title is None or r.get("AXTitle") == title)]

    def perform_action(self, el, action):
        self.pressed.append((el.get("AXIdentifier"), action))
        return True


def _press_step(recorded_selector=None, role="AXButton", title=None):
    cmd = {"role": role, "action": "press"}
    if title is not None:
        cmd["title"] = title
    if recorded_selector is not None:
        cmd["selector"] = recorded_selector
    return PlannedStep(intent="press", source="workflow", target_element="",
                       modality="ax", command=cmd)


def _divide_row(rows):
    return next(r for r in rows if r.get("AXIdentifier") == "Divide")


def test_refuses_wrong_target_instead_of_guessing_first_of_role():
    _, rows = load("calculator")
    tree = _FixtureAxTree(rows, resolve_mode="wrong")
    recorded = tree.to_selector(_divide_row(rows)).to_dict()
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_press_step(recorded_selector=recorded))

    assert res["success"] is False, res
    assert res.get("outcome") == "refused", res
    assert res.get("refused") is True, res
    assert tree.pressed == [], f"must never actuate a wrong target: {tree.pressed}"
    assert "does not match the recorded identity" in (res.get("error") or ""), res
    print("OK  wrong-target resolution -> refused, never guessed")


def test_accepts_a_confirmed_identity_match():
    _, rows = load("calculator")
    tree = _FixtureAxTree(rows, resolve_mode="correct")
    recorded = tree.to_selector(_divide_row(rows)).to_dict()
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_press_step(recorded_selector=recorded))

    assert res["success"] is True, res
    assert res.get("outcome") is None or res.get("outcome") != "refused", res
    assert tree.pressed == [("Divide", "AXPress")], tree.pressed
    print("OK  a resolution whose identity matches the recording is accepted")


def test_refuses_when_matcher_has_no_candidate_never_falls_back_to_find():
    _, rows = load("calculator")
    tree = _FixtureAxTree(rows, resolve_mode="none")
    recorded = tree.to_selector(_divide_row(rows)).to_dict()
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_press_step(recorded_selector=recorded))

    assert res["success"] is False, res
    assert res.get("outcome") == "refused", res
    assert tree.find_calls == [], (
        "a recorded identity must never fall through to a plain role/title lookup")
    assert tree.pressed == []
    print("OK  no candidate within the matcher's threshold -> refused, no role/title fallback")


def test_legacy_step_without_selector_refuses_when_ambiguous():
    rows = [
        {"AXRole": "AXButton", "AXTitle": "Info", "AXIdentifier": None, "AXDescription": None},
        {"AXRole": "AXButton", "AXTitle": "Info", "AXIdentifier": None, "AXDescription": None},
    ]
    tree = _FixtureAxTree(rows)
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_press_step(title="Info"))

    assert res["success"] is False, res
    assert res.get("outcome") == "uncertain", res
    # every unresolved target is a typed refusal that stops the
    # ladder (outcome_kind "refused"), never a retryable refused=False.
    assert res.get("refused") is True and res.get("outcome_kind") == "refused", res
    assert res.get("reason_code") == "ambiguous", res
    assert tree.pressed == []
    print("OK  no recorded identity + >1 candidate -> typed refusal, not 'the first one'")


def test_legacy_step_without_selector_accepts_when_unambiguous():
    rows = [{"AXRole": "AXButton", "AXTitle": "Info", "AXIdentifier": None, "AXDescription": None}]
    tree = _FixtureAxTree(rows)
    ex = StateGraphExecutor(ax_tree=tree)

    res = ex._execute_ax(_press_step(title="Info"))

    assert res["success"] is True, res
    assert tree.pressed == [(None, "AXPress")], tree.pressed
    print("OK  exactly one candidate with no recorded identity is still accepted (unchanged)")


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in sorted(vars(mod).items()):
        if not (n.startswith("test_") and callable(fn)):
            continue
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1
        except Exception as e:
            f += 1
            print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
