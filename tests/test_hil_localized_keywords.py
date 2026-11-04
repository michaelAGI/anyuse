"""The HIL gate must read a destructive verb in the language the desktop speaks.

Guest VM, macOS 26.6.2 de_DE: Finder's "Move to Trash" is the
menu item "In den Papierkorb legen" (⌘⌫). None of the English keywords in
``_DESTRUCTIVE_KEYWORDS`` occurs in it, so ``classify_step`` returned
SIDE_EFFECTING and, under ``ANYUSE_HIL_MODE=destructive``, the gate said
``actuate=True``. The default keyword set now includes the German verbs; the
English-only set is kept and pinned here so the before/after stays measurable.
Run: python3 test_hil_localized_keywords.py
"""
import asyncio
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution import hil_gate as hg  # noqa: E402

# The step as the recorder's coordinate vocabulary names it ("click"), which the
# classifier recognises, and as the AX executor names it ("press"), which it
# does NOT: an unrecognised action fails closed to DESTRUCTIVE, so every AX
# step is held under mode "destructive" — over-holding, i.e. mode "destructive"
# behaves like mode "all" for AX replay. Both are pinned below.
TRASH = {"action": "click", "args": {"label": "In den Papierkorb legen", "role": "AXMenuItem",
                                     "identifier": "_NS:213", "cmd": "⌘⌫"}}
TRASH_AX = {"action": "press", "args": dict(TRASH["args"])}


def test_english_only_keywords_let_the_german_delete_through():
    assert hg.classify_step(TRASH, keywords=hg._DESTRUCTIVE_KEYWORDS) == hg.StepClass.SIDE_EFFECTING
    gate = hg.HILGate(mode="destructive", approver=None)
    d = asyncio.run(gate.require_approval(TRASH, drive_enabled=True, keywords=hg._DESTRUCTIVE_KEYWORDS))
    assert d.actuate is True            # the gate that was measured in the guest


def test_default_keywords_hold_the_german_delete_and_record_a_request():
    assert hg.classify_step(TRASH) == hg.StepClass.DESTRUCTIVE
    requests = []

    async def approver(ctx):
        requests.append(ctx)
        return False

    d = asyncio.run(hg.HILGate(mode="destructive", approver=approver).require_approval(TRASH, drive_enabled=True))
    assert d.actuate is False and d.step_class == hg.StepClass.DESTRUCTIVE
    assert len(requests) == 1 and requests[0]["args"]["label"] == "In den Papierkorb legen"
    try:
        asyncio.run(hg.HILGate(mode="destructive").require_approval(TRASH, drive_enabled=True))
    except hg.ApprovalRequired as exc:
        assert exc.step_class == hg.StepClass.DESTRUCTIVE
    else:
        raise AssertionError("no approver: must fail closed")


def test_ordinary_german_labels_stay_side_effecting():
    for label in ("Sichern", "Neuer Ordner", "Öffnen …", "Suchen", "Fortfahren", "Alles auswählen"):
        assert hg.classify_step({"action": "click", "args": {"label": label}}) == hg.StepClass.SIDE_EFFECTING, label
    for label in ("Löschen", "Papierkorb entleeren", "Alle entfernen", "Jetzt senden", "Deinstallieren"):
        assert hg.classify_step({"action": "click", "args": {"label": label}}) == hg.StepClass.DESTRUCTIVE, label


def test_ax_press_is_unrecognised_and_therefore_held_regardless_of_language():
    assert hg.classify_step(TRASH_AX, keywords=hg._DESTRUCTIVE_KEYWORDS) == hg.StepClass.DESTRUCTIVE
    assert hg.classify_step({"action": "press", "args": {"label": "Sichern"}}) == hg.StepClass.DESTRUCTIVE
    assert hg.classify_step({"action": "click", "args": {"label": "Sichern"}}) == hg.StepClass.SIDE_EFFECTING


def test_gate_off_never_classifies():
    d = asyncio.run(hg.HILGate(mode="destructive").require_approval(TRASH, drive_enabled=False))
    assert d.actuate is False and d.step_class == ""


if __name__ == "__main__":
    test_english_only_keywords_let_the_german_delete_through()
    test_default_keywords_hold_the_german_delete_and_record_a_request()
    test_ordinary_german_labels_stay_side_effecting()
    test_ax_press_is_unrecognised_and_therefore_held_regardless_of_language()
    test_gate_off_never_classifies()
    print("ok")
