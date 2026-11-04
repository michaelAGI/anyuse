"""A control's label is often ONLY in AXDescription.

The fixtures in ``testdata/ax/`` are real AX trees, captured inside the guest VM
from macOS 26.6.2 (25G83), locale ``de_DE``, with the raw attribute
values exactly as ``AXUIElementCopyAttributeValue`` returned them. They are the
evidence, and they make this test reproducible on the host, under
the desktop guard, with no VM and no accessibility grant.

What the guest showed
---------------------
Every one of macOS Calculator's keypad buttons has ``AXTitle = None``. Its only
human label is ``AXDescription`` — and on a German system that reads
``'Addieren'``, ``'Ist gleich'``, ``'Alles löschen'``. ``_describe()`` read
AXRole, AXTitle, AXValue, AXRoleDescription and AXIdentifier, and never
AXDescription, so:

  * ``AXElementDesc.title`` was None for all 25 buttons;
  * ``selector_from_attrs`` built ``name = (title or value) = ""``;
  * ``is_durable()`` was therefore False for any such control in an app that
    also assigns no AXIdentifier — the abstain signal fired on a control that is
    perfectly well labelled;
  * and ``find_selector`` matched ``selector.name`` against ``title or value``
    only, so a stored AXDescription-derived name could never match. With nothing
    else to go on it fell through to "same canonical role", which returns the
    FIRST button in the tree. On a keypad that is a wrong-target press.

``AXElementDesc.description`` compounded it by returning AXRoleDescription — the
KIND of control ("Taste" for every button) — under a name that callers use as a
label.

Run from the anyuse package dir:
    cd anyuse && python3 test_ax_description_label.py
"""

import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.context.accessibility import (  # noqa: E402
    AXElementDesc, Selector, selector_from_attrs,
)

FIXTURES = _HERE / "testdata" / "ax"


def load(name):
    data = json.loads((FIXTURES / f"{name}_ax_raw.json").read_text())
    return data["meta"], data["elements"]


def as_desc(row):
    """The fixture row as _describe() now builds it."""
    d = row.get("AXDescription")
    return AXElementDesc(
        role=row.get("AXRole"), title=row.get("AXTitle"), value=row.get("AXValue"),
        role_desc=row.get("AXRoleDescription"), identifier=row.get("AXIdentifier"),
        ax_description=None if d in (None, "None") else d,
    )


def main():
    print("=" * 70)
    print("AXDescription is a label, not decoration (defect D5)")
    print("=" * 70)

    meta, rows = load("calculator")
    print(f"fixture: {meta['app']} on macOS {meta['macos']} ({meta['build']}), "
          f"locale {meta['language']}, {meta['n']} elements")
    assert meta["language"].startswith("de"), (
        "this fixture is supposed to be the GERMAN capture — the point is that a "
        "label match cannot rely on English strings")

    descs = [as_desc(r) for r in rows]
    buttons = [d for d in descs if d.role == "AXButton"]
    assert buttons, "no AXButton in the Calculator fixture"

    only_desc = [d for d in descs if not d.title and d.ax_description]
    print(f"OK  {len(only_desc)} of {len(descs)} elements are labelled ONLY by "
          f"AXDescription")
    assert only_desc, "fixture no longer exercises the defect"

    # 1. The label is found where it actually lives. The assertion is on the
    #    IMPROVEMENT, not on a round percentage: three of Calculator's buttons
    #    genuinely carry no label of any kind (no title, no description, no
    #    identifier), and a test that demanded 100 % would be asserting something
    #    macOS does not provide.
    before = [d for d in buttons if (d.title or d.value)]      # the old rule
    labelled = [d for d in buttons if d.label()]               # title|desc|value
    print(f"OK  AXButtons with a label: {len(before)}/{len(buttons)} before the fix, "
          f"{len(labelled)}/{len(buttons)} after")
    assert len(labelled) > len(before), (
        f"AXDescription is not being consulted: {len(labelled)} labelled buttons "
        f"is no better than the old title-or-value rule's {len(before)}")
    unlabelled = [d for d in buttons if not d.label()]
    print(f"    {len(unlabelled)} button(s) carry no label at all in any attribute "
          f"— those are the ones the abstain signal is genuinely for")

    # 2. A German AXDescription-only control reaches the selector's name.
    german = [d for d in buttons
              if not d.title and (d.ax_description or "") == "Addieren"]
    assert german, "expected the 'Addieren' (add) button in the German fixture"
    sel = german[0].to_selector(path="window/button[15]")
    print(f"OK  selector for the add key: {sel.describe()}  name={sel.name!r}")
    assert sel.name == "Addieren", f"selector name is {sel.name!r}, expected 'Addieren'"

    # 3. is_durable() must not abstain on a well-labelled control. Checked with
    #    the identifier stripped, which is the case the abstain signal is for.
    no_id = selector_from_attrs(role="AXButton", title=None, value=None,
                                identifier=None, path="window/button[15]",
                                description="Addieren")
    print(f"OK  with no AXIdentifier: name={no_id.name!r} durable={no_id.is_durable()}")
    assert no_id.is_durable(), (
        "a button whose label is 'Addieren' is not a fragile selector; abstaining "
        "here is what pushed these controls onto the coordinate rung")

    # 4. .description must be the element's own description, not its role kind.
    d = german[0]
    print(f"OK  .description={d.description!r} (role_desc is {d.role_desc!r})")
    assert d.description == "Addieren", (
        f".description returned {d.description!r}; callers use it as a LABEL and "
        f"role_desc is the same string for every button in the app")

    # 5. Distinct keys must produce distinct selector names — the property that
    #    actually prevents a wrong-target press.
    keypad = [d for d in buttons if d.ax_description and not d.title]
    names = [d.to_selector().name for d in keypad]
    print(f"OK  {len(set(names))} distinct names across {len(names)} keypad buttons")
    assert len(set(names)) == len(names), (
        f"keypad buttons share selector names: "
        f"{[n for n in names if names.count(n) > 1]!r}. Identical names are how a "
        f"replay presses the wrong key.")

    # 6. text() carries the description, so the drift gate and the scorer see it.
    assert "Addieren" in d.text(), f"text() lost the description: {d.text()!r}"
    print(f"OK  text() = {d.text()!r}")

    # 7. The same holds for the TextEdit capture.
    meta2, rows2 = load("textedit")
    d2 = [as_desc(r) for r in rows2]
    only2 = [x for x in d2 if not x.title and x.ax_description]
    print(f"OK  textedit fixture ({meta2['n']} elements): {len(only2)} labelled "
          f"only by AXDescription")
    assert only2

    print("\nAX DESCRIPTION LABEL TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
