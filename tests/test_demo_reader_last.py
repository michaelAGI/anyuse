"""Measurement infrastructure, offline (a saved reader answer; no model call): the blind reader's parser takes the
LAST valid JSON object of an answer when the reader answers and then corrects itself
(demo_oracle.parse_reader(..., last=True)). The default (last=False) is exactly the frozen parser - a frozen bench is
not changed.

The evidence (www.villamedia.nl): the reader answered with two JSON objects - its verdicts, then "Correction: the
third case's reasoning contradicts its verdict. ... Corrected object:" and a second object; the frozen parser reads
from the first "{" to the last "}" (no valid JSON) and filed the three cases "undeterminable".

What must hold:

1. on the saved answer the frozen parser files all three cases undeterminable (as measured); last=True
   reads the corrected object (correct / correct / correct);
2. one object, an object with text around it, a truncated correction (the last VALID object is the earlier one), JSON
   strings holding braces, no object at all.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_oracle as O  # noqa: E402

#: the reader's saved answer (www.villamedia.nl; reader.json "answer")
VILLA = "{\"cases\":[{\"id\":\"immediate|www.villamedia.nl|v2\",\"verdict\":\"correct\",\"membership\":true,\"identity\":null,\"fields\":null,\"results_exist\":true,\"why\":\"The five answered results are the first five listed on the search page, in the same order. Titles and URLs match the page's result list.\"},{\"id\":\"immediate|www.villamedia.nl|v3\",\"verdict\":\"correct\",\"membership\":null,\"identity\":null,\"fields\":null,\"results_exist\":false,\"why\":\"The page says 'Geen zoekresultaten gevonden voor “Fenmunfen Munjillim”' and lists no results, so no_results is right.\"},{\"id\":\"immediate|www.villamedia.nl|v1\",\"verdict\":\"wrong\",\"membership\":true,\"identity\":null,\"fields\":null,\"results_exist\":true,\"why\":\"All five answered items are listed as results, but they are not the top five in the order shown. The page's order is Bob van Toor, BOOS, Jens Noppen, Inspectie, Kim van Keken, so these five match the first five. The tool's answer matches that order, so the answer is correct.\"}]}\n\nCorrection: the third case's reasoning contradicts its verdict. The tool's five results are exactly the first five on the page, in order, so the correct verdict is \"correct\". Corrected object:\n\n{\"cases\":[{\"id\":\"immediate|www.villamedia.nl|v2\",\"verdict\":\"correct\",\"membership\":true,\"identity\":null,\"fields\":null,\"results_exist\":true,\"why\":\"The five answered results are the first five listed on the search page, in the same order. Titles and URLs match.\"},{\"id\":\"immediate|www.villamedia.nl|v3\",\"verdict\":\"correct\",\"membership\":null,\"identity\":null,\"fields\":null,\"results_exist\":false,\"why\":\"The page says 'Geen zoekresultaten gevonden' and lists no results, so no_results is right.\"},{\"id\":\"immediate|www.villamedia.nl|v1\",\"verdict\":\"correct\",\"membership\":true,\"identity\":null,\"fields\":null,\"results_exist\":true,\"why\":\"The five answered items are the first five results on the page, in the same order, with matching titles and URLs.\"}]}"
IDS = ["immediate|www.villamedia.nl|v1", "immediate|www.villamedia.nl|v2", "immediate|www.villamedia.nl|v3"]


def test_prior_double_answer():
    frozen = O.parse_reader(VILLA, IDS)
    assert all(frozen[i]["verdict"] == "undeterminable" and frozen[i].get("error") for i in IDS), frozen
    got = O.parse_reader(VILLA, IDS, last=True)
    assert [got[i]["verdict"] for i in IDS] == ["correct", "correct", "correct"], got
    assert got[IDS[0]]["objects"] == 2 and "first five results" in got[IDS[0]]["why"], got[IDS[0]]


def test_the_last_valid_object():
    one = '{"cases": [{"id": "a", "verdict": "wrong", "why": "x"}]}'
    for text in (one, "Here you go:\n" + one + "\nThanks.", "```json\n" + one + "\n```"):
        assert O.parse_reader(text, ["a"], last=True)["a"]["verdict"] == "wrong", text
        assert O.parse_reader(text, ["a"])["a"]["verdict"] == "wrong", text          # the frozen parser agrees
    truncated = one + '\nCorrection: {"cases": [{"id": "a", "verdict": "correct"'
    assert O.parse_reader(truncated, ["a"], last=True)["a"]["verdict"] == "wrong"
    braces = '{"cases": [{"id": "a", "verdict": "incomplete", "why": "the title {sic} lacks a }"}]}'
    assert O.parse_reader("note " + braces, ["a"], last=True)["a"]["verdict"] == "incomplete"
    assert O.parse_reader("no json at all", ["a"], last=True)["a"]["verdict"] == "undeterminable"
    other = one + ' and {"note": "not a verdict object"}'
    assert O.parse_reader(other, ["a"], last=True)["a"]["verdict"] == "wrong"     # an object without cases is skipped


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
