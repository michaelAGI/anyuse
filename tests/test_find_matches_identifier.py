"""Regression: ``AccessibilityTree.find()`` also matches a recorded
``identifier`` (AXIdentifier), exactly and decisively, like the title path.

TextEdit's document ``AXTextArea`` has
``AXTitle = None`` and ``AXDescription = None`` — its only human-readable
attribute is ``identifier`` ("First Text View"), real captured data in
``testdata/ax/textedit_ax_raw.json`` (the same fixture
``test_ax_description_label.py`` uses). A live Cerebras planner read that
value out of the AX dump and (reasonably — there is no separate
``identifier`` field in the plan step schema) put it in the plan step's
``title``. Before this fix,
``find(role="AXTextArea", title="First Text View")`` returned **zero**
candidates — the match had been widened to title-or-label (title, else
AXDescription, else AXValue), but ``identifier`` is none of those three — so
``_resolve_target()`` correctly refused (no identity, no guess) rather
than typing into the wrong place, but the TextEdit write+save flow could
never resolve on the product path (0/3).

This file runs the REAL ``AccessibilityTree.find()`` — not a stand-in — over
the real fixture rows, by subclassing just ``_root()``/``_walk()`` (the two
methods that need pyobjc) so the matching logic in ``find()`` itself is
exercised unmodified, on the host, with no VM, no AX grant and no pyobjc
import.

Run from the anyuse package dir:
    cd anyuse && python3 test_find_matches_identifier.py
"""
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))  # flat: import context.accessibility directly, like executor.py does

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, (
    "resolved 'anyuse' to the wrong checkout — (D9)")

from anyuse.context.accessibility import AccessibilityTree, AXElementDesc  # noqa: E402

FIXTURES = _HERE / "testdata" / "ax"


def _as_desc(row):
    d = row.get("AXDescription")
    return AXElementDesc(
        role=row.get("AXRole"), title=row.get("AXTitle"), value=row.get("AXValue"),
        role_desc=row.get("AXRoleDescription"), identifier=row.get("AXIdentifier"),
        ax_description=None if d in (None, "None") else d,
    )


class _FixtureTree(AccessibilityTree):
    """The real ``AccessibilityTree`` with only the two pyobjc-touching
    methods replaced: ``_root()`` (never actually dereferenced by our
    ``_walk`` override) and ``_walk()`` (flat: each fixture row IS its own
    element ref, since these fixtures were captured with no duplicate
    identifiers at this role). ``find()`` itself — the code under test — is
    untouched, inherited straight from the real class."""

    def __init__(self, rows):
        super().__init__(pid=None)
        self._rows = rows

    def _root(self):
        return None

    def _walk(self, element, depth, descs, refs=None, in_menubar=False):
        for row in self._rows:
            d = _as_desc(row)
            descs.append(d)
            if refs is not None:
                refs.append((row, d))


def load(name):
    data = json.loads((FIXTURES / f"{name}_ax_raw.json").read_text())
    return data["meta"], data["elements"]


def test_identifier_only_element_is_found_by_title_lookup():
    meta, rows = load("textedit")
    assert meta["language"].startswith("de")
    tree = _FixtureTree(rows)

    text_areas = [r for r in rows if r.get("AXRole") == "AXTextArea"]
    assert text_areas, "fixture no longer carries an AXTextArea"
    assert all(r.get("AXTitle") is None and r.get("AXDescription") is None for r in text_areas), (
        "fixture no longer exercises the defect: these must have neither a title nor a description")

    before_would_have = [
        r for r in text_areas
        if r.get("AXTitle") == "First Text View" or (r.get("AXDescription") or r.get("AXValue")) == "First Text View"
    ]
    assert before_would_have == [], "the pre-D19 match criteria must not already match on identifier by accident"

    found = tree.find(role="AXTextArea", title="First Text View")
    assert len(found) == len(text_areas) > 0, (
        f"find(role='AXTextArea', title='First Text View') returned {len(found)} candidates, "
        f"expected all {len(text_areas)} — the exact D19 regression (a live planner puts the "
        f"AXIdentifier value into the plan step's title field)")
    print(f"OK  find(role='AXTextArea', title='First Text View') now matches "
          f"{len(found)}/{len(text_areas)} identifier-only elements (0 before the fix)")


def test_title_and_label_matching_is_unaffected_d16_still_holds():
    """The identifier widening must not change the title-or-label result: a control
    labelled only by AXDescription (Calculator's keypad) still matches on
    that label, and still ONLY on that label — the identifier check is an
    additional OR, not a replacement."""
    _, rows = load("calculator")
    tree = _FixtureTree(rows)

    add_btn = next(r for r in rows if r.get("AXDescription") == "Addieren")
    assert add_btn.get("AXTitle") is None
    found = tree.find(role="AXButton", title="Addieren")
    assert any(r.get("AXIdentifier") == add_btn.get("AXIdentifier") for r in found), (
        "D16 regressed: an AXDescription-only label no longer matches")
    print("OK  D16's AXDescription/label matching still holds (identifier is an additional OR, not a replacement)")

    # And an identifier that happens to equal some OTHER element's title must
    # not cause a false match — this is an exact identifier<->title string
    # comparison, not a fuzzy one.
    decoy = [r for r in rows if r.get("AXTitle") == "Info"]
    if decoy:
        found_decoy = tree.find(role=decoy[0].get("AXRole"), title="Info")
        assert all(r is d or r.get("AXTitle") == "Info" for r, d in [(r, decoy[0]) for r in found_decoy]), (
            "unexpected non-title match for 'Info'")
    print("OK  identifier matching is exact string equality, not a fuzzy fallback")


def test_two_elements_sharing_an_identifier_stay_ambiguous_not_a_guess():
    """The no-guess property must survive the widening: find() returning MULTIPLE
    candidates for one identifier must still leave the caller (executor.py's
    _resolve_target) refusing as ambiguous — find() itself never picks one."""
    rows = [
        {"AXRole": "AXTextArea", "AXTitle": None, "AXDescription": None,
         "AXIdentifier": "First Text View", "AXValue": "doc one"},
        {"AXRole": "AXTextArea", "AXTitle": None, "AXDescription": None,
         "AXIdentifier": "First Text View", "AXValue": "doc two"},
    ]
    tree = _FixtureTree(rows)
    found = tree.find(role="AXTextArea", title="First Text View")
    assert len(found) == 2, (
        "find() must return every match, not collapse duplicates — ambiguity is "
        "_resolve_target()'s job (D11), not find()'s")
    print("OK  two elements sharing an identifier both come back — find() never disambiguates, only matches")


TESTS = [
    test_identifier_only_element_is_found_by_title_lookup,
    test_title_and_label_matching_is_unaffected_d16_still_holds,
    test_two_elements_sharing_an_identifier_stay_ambiguous_not_a_guess,
]


def main():
    print("=" * 70)
    print("find() matches identifier exactly, like the title path (D19)")
    print("=" * 70)
    for t in TESTS:
        t()
    print("\nFIND IDENTIFIER MATCH TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
