"""anyuse context.accessibility smoke.

The diff / to_summary / clearly_matches logic is pure and tested deterministically
(no permission needed). The live AX calls (snapshot/find/perform_action/set_value)
are exercised for graceful degradation — they must never crash even with no
Accessibility TCC (they return empty/False).

    cd anyuse && /usr/local/bin/python3.13 test_context_accessibility.py
"""

import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)


def main():
    print("=" * 60)
    print("anyuse context.accessibility — AX tree")
    print("=" * 60)

    from anyuse.context.accessibility import (
        AccessibilityTree, AXSnapshot, AXDiff, AXElementDesc,
    )
    print("OK  context.accessibility imports (AccessibilityTree/AXSnapshot/AXDiff)")

    tree = AccessibilityTree()

    # ── pure diff logic (no permission) ──
    before = AXSnapshot([
        AXElementDesc(role="AXButton", title="Submit"),
        AXElementDesc(role="AXTextField", title="Email", value="abc"),
    ])
    after = AXSnapshot([
        AXElementDesc(role="AXButton", title="Submit"),
        AXElementDesc(role="AXTextField", title="Email", value="me@x.com"),
        AXElementDesc(role="AXStaticText", title=None, value="Saved successfully!"),
    ])
    d = tree.diff(before, after)
    assert any("Saved successfully" in e.text() for e in d.added), d.to_summary()
    assert any(b.value == "abc" and a.value == "me@x.com" for b, a in d.changed), d.to_summary()
    print(f"OK  diff detects added + value-change: {d.to_summary()}")

    assert d.clearly_matches("saved successfully") is True
    assert d.clearly_matches("me@x.com") is True          # via the changed field
    assert d.clearly_matches("error occurred") is False
    assert d.clearly_matches("") is False
    print("OK  clearly_matches: matches added/changed evidence, rejects absent text")

    same = tree.diff(before, before)
    assert same.is_empty() and same.to_summary() == "no AX changes"
    print("OK  identical snapshots → empty diff ('no AX changes')")

    # ── reordered same-key elements must NOT report false changes ──
    # Two elements share the same key (role, title, role_desc) but differ in
    # value; reordering them across snapshots must not be mis-paired positionally
    # into spurious changed/added/removed. (regression: was zip()-paired by order)
    reordered_before = AXSnapshot([
        AXElementDesc(role="AXTextField", title="Row", value="alpha"),
        AXElementDesc(role="AXTextField", title="Row", value="beta"),
    ])
    reordered_after = AXSnapshot([
        AXElementDesc(role="AXTextField", title="Row", value="beta"),
        AXElementDesc(role="AXTextField", title="Row", value="alpha"),
    ])
    rd = tree.diff(reordered_before, reordered_after)
    assert rd.is_empty(), f"reordered same-key snapshot reported false changes: {rd.to_summary()}"
    assert rd.clearly_matches("alpha") is False
    print("OK  reordered same-key elements → empty diff (no positional mis-pairing)")

    # ── live AX calls degrade gracefully (no crash without TCC) ──
    snap = tree.snapshot()
    assert isinstance(snap, AXSnapshot)
    found = tree.find(role="AXButton")
    assert isinstance(found, list)
    assert tree.perform_action("not-an-element", "AXPress") is False
    assert tree.set_value("not-an-element", "x") is False
    print(f"OK  live AX calls degrade gracefully (snapshot={len(snap.elements)} elems, "
          f"find→{len(found)}, bad perform/set→False)")

    # ── find(title=...) must also match the composite label (AXDescription),
    # not AXTitle alone — the same blind spot the recorded-selector matcher
    # covers, here in this bare no-recorded-identity lookup too (used when a
    # live planner names a target with nothing recorded to check it against).
    # `_walk` is monkeypatched so this stays a pure, offline unit test: no
    # pyobjc, no live AX tree, no TCC dependency. ──
    fake_elements = [
        AXElementDesc(role="AXButton", title=None, ax_description="Sieben"),
        AXElementDesc(role="AXButton", title="Save", ax_description=None),
        AXElementDesc(role="AXButton", title=None, ax_description="Ambiguous"),
        AXElementDesc(role="AXButton", title=None, ax_description="Ambiguous"),
    ]

    def _fake_walk(_element, _depth, descs, refs=None, in_menubar=False):
        descs.extend(fake_elements)
        if refs is not None:
            refs.extend((object(), d) for d in fake_elements)

    # find() calls self._root() before self._walk(); under the guard (no
    # pyobjc) _root() itself raises, so it needs stubbing too — otherwise
    # find()'s own try/except returns [] before the fake _walk ever runs.
    tree._root = lambda: None
    tree._walk = _fake_walk
    by_description = tree.find(role="AXButton", title="Sieben")
    assert len(by_description) == 1, by_description
    by_title = tree.find(role="AXButton", title="Save")
    assert len(by_title) == 1, by_title
    ambiguous = tree.find(role="AXButton", title="Ambiguous")
    assert len(ambiguous) == 2, "an ambiguous label must still return every candidate, not guess one"
    missing = tree.find(role="AXButton", title="Nobody Home")
    assert missing == []
    print("OK  find(title=...) matches AXDescription too, without weakening ambiguity")

    print("\nCONTEXT.ACCESSIBILITY TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
