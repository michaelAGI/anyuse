"""Arch12, offline with fakes: "trag ... ein" fills the labelled line.

Arch11 on SF08: "Trag bei der Waschmaschine in wartung.txt die Modellnummer ein" — the right note, the right number
(WM-4410), but the plan APPENDED it as a new last line instead of completing the empty line "Waschmaschine Modell:":
a real wrong action with residual damage.

Pinned here: a fill-in request (trag ein / ergänze / fill in / complete) whose document has exactly one EMPTY
labelled line the user's words name best turns the planned append / insert into an edit AT that label (the same
line, after the colon); the exact permitted diff then checks that only that line changed. Not a fill-in (an
explicit new line, no empty label, two labels as good, the value already there): unchanged. No desktop, no AX, no
subprocess, no network.
"""
from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e12 as Z

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9
import test_desktop_e2e11 as T11

EMAP = W.EffectMap4()
S, plan4, FakePlanner4 = T4.S, T4.plan4, T4.FakePlanner4
NOTES, ROOT = T4.NOTES, T4.ROOT

SF08 = "Trag bei der Waschmaschine in wartung.txt die Modellnummer ein, die steht in meinen Notizen."
WARTUNG = "Wartungsliste\nWaschmaschine Modell:\nTrockner Modell: TR-2205"


def step(value, mode="append", find="", target=""):
    return {"effect": "doc_save", "app": "TextEdit", "target": target, "value": value, "mode": mode, "find": find}


def test_the_empty_labelled_line_the_users_words_name_is_filled_in():
    f = Z.fill_in12(SF08, WARTUNG, step("WM-4410"))
    assert f == {"line": 1, "label": "Waschmaschine Modell", "find": "Waschmaschine Modell:",
                 "value": "Waschmaschine Modell: WM-4410"}
    assert W.compose(WARTUNG, "edit", f["value"], f["find"])[0] == \
        "Wartungsliste\nWaschmaschine Modell: WM-4410\nTrockner Modell: TR-2205"
    # the plan wrote the labelled line itself: the same edit
    assert Z.fill_in12(SF08, WARTUNG, step("Waschmaschine Modell: WM-4410"))["value"] == "Waschmaschine Modell: WM-4410"
    assert Z.fill_in12("Fill in the dryer's model number.", "Washer model:\nDryer model:", step("TR-1"))["label"] == \
        "Dryer model"


def test_no_fill_in_without_a_fill_request_an_empty_label_or_a_clear_best_label():
    assert Z.fill_in12("Häng die Modellnummer als neue Zeile an.", WARTUNG, step("WM-4410")) is None
    assert not Z.asks_fill_in12("Ergänz ganz unten „Aufbau ab 8 Uhr.“.")        # GB09: a new last line
    assert Z.fill_in12(SF08, "Wartungsliste\nTrockner Modell: TR-2205", step("WM-4410")) is None
    assert Z.fill_in12(SF08, "Waschmaschine Modell:\nWaschmaschine Modell alt:", step("WM-4410")) is not None
    assert Z.fill_in12("Trag die Nummer ein.", "Modell A:\nModell B:", step("X-1")) is None       # two as good
    assert Z.fill_in12(SF08, WARTUNG + "\nWM-4410", step("WM-4410")) is None                     # already there
    assert Z.fill_in12(SF08, WARTUNG, step("WM-4410", mode="edit", find="x")) is None            # an edit stays
    assert Z.fill_in12(SF08, WARTUNG, step("Trockner Modell: TR-9")) is None                      # another label


def _sf08_guest():
    g = T9.FakeGuest9({"H/wartung.txt": WARTUNG}, shown="H", te_front="H/wartung.txt",
                      notes={"Haushalt_Info": [("Waschmaschine", "Waschmaschine\nModell WM-4410, gekauft 2019"),
                                               ("Trockner", "Trockner\nModell TR-2205")]})
    return g


PLAN_SF08 = (plan4(S("launch_app", "Notes"), S("open_item", "Notes", "Waschmaschine"),
                   S("read_content", "Notes", "note", "note_text")),
             plan4(S("extract", "none", "die Modellnummer", "model", find="WM-4410"), S("launch_app", "TextEdit"),
                   S("doc_save", "TextEdit", "", "{{model}}", mode="append")))


def test_sf08_like_the_model_number_is_written_after_the_label_and_nowhere_else():
    g = _sf08_guest()
    rec = X.Rec("SF08", "arch", 1)
    Z.Arch12(g, EMAP, planner=FakePlanner4(*PLAN_SF08), config11=T11.cfg(), jchooser=T11.ConfOracle("x"),
             spend=None, inbox=None, sleep=lambda s: None).run(
        X.Task("SF08", SF08, "cross-app", {"app": NOTES}, [], {}), rec)
    assert g.fs[f"{ROOT}/H/wartung.txt"] == "Wartungsliste\nWaschmaschine Modell: WM-4410\nTrockner Modell: TR-2205", \
        (rec.stopped, [s.get("why") for s in rec.steps])
    save = next(s for s in rec.steps if s["step"]["effect"] == "doc_save")
    assert save["fill_in12"]["planned"]["mode"] == "append" and save["exact_diff9"] == []
    assert rec.fixes12 and rec.fixes12[0]["fix"] == "fill_in" and rec.errors9 == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
