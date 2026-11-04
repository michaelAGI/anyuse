"""Offline (a saved reader answer; no browser, no network, no model): MEASUREMENT INFRASTRUCTURE - when the blind
reader's answer for a case is TRUNCATED or has NO FINAL VERDICT, the reader is asked ONCE more (the same case) before
a verdict is recorded (``demo_oracle.ask_reader``; the bench's ``READER_ONCE_MORE``, off by default: one call per
site and wave exactly).

The evidence (www.ruhrbarone.de): the reader (Sonnet) answered one object; case v2's verdict is "wrong" while its own
reasoning ends "... this matches the answer, so on the evidence the answer is correct." (the recorded reason, cut at
300 characters by the parser, read as broken off); the saved render supports the answer, yet it was counted wrong.

What must hold:

1. on the saved answer only case v2 is to be asked again ("no final verdict": the verdict and the reasoning's last
   sentence disagree); with a second answer for v2 alone, v2's recorded verdict is the second one (the first kept as
   evidence), v1 and v3 keep the first answer's;
2. a reasoning that breaks off, a missing case and an unreadable verdict are asked again; a consistent answer is
   not; the reader cap refuses the second call -> the first verdicts stand; never a third call;
3. the bench's default is unchanged (one call).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_oracle as O  # noqa: E402

#: the reader's whole answer (www.ruhrbarone.de; reader.json "answer")
ANSWER = "{\"cases\": [{\"id\": \"immediate|www.ruhrbarone.de|v3\", \"verdict\": \"correct\", \"membership\": null, \"identity\": null, \"fields\": null, \"results_exist\": false, \"why\": \"Search page states 'No results found'; the listed posts are sidebar/recommendation blocks, not results. no_results is correct.\"}, {\"id\": \"immediate|www.ruhrbarone.de|v1\", \"verdict\": \"correct\", \"membership\": true, \"identity\": null, \"fields\": null, \"results_exist\": true, \"why\": \"All five answered results are the first five items in the result list, in the same order, with matching titles and URLs.\"}, {\"id\": \"immediate|www.ruhrbarone.de|v2\", \"verdict\": \"wrong\", \"membership\": false, \"identity\": null, \"fields\": null, \"results_exist\": true, \"why\": \"The answer's first, second, fourth and fifth items are listed as results in the same order. The third answer item, 'Die Linke Berlin: Mit den Grünen regieren, gegen die Grünen agitieren', is listed third on the page and matches. However, the page's actual results include the answered items in order 1, 2, 3, 4, 5, with the Bundeswehr article listed fifth; this matches the answer, so on the evidence the answer is correct.\"}]}"
IDS = ["immediate|www.ruhrbarone.de|v1", "immediate|www.ruhrbarone.de|v2", "immediate|www.ruhrbarone.de|v3"]
CASES = [{"id": i, "task": "site search", "query": q, "tool_answer": {"results": []}, "search_results_page": None}
         for i, q in zip(IDS, ("Ruhrgebiet", "Berlin", "zorquavex"))]


class Script:
    """A scripted reader CLI: its answers in order; the prompts it got."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0), {"seconds": 1.0}


def case_ids(prompt):
    return [c["id"] for c in json.loads(prompt.split("CASES:\n", 1)[1])]


def test_prior_ruhrbarone_answer_is_asked_once_more_for_v2_only():
    retry = O.reader_retry_cases(ANSWER, IDS)
    assert list(retry) == [IDS[1]] and "concludes 'correct'" in retry[IDS[1]], retry
    assert O.concluded_verdict(json.loads(ANSWER)["cases"][2]["why"]) == "correct"
    # a sub-criterion or a phrase is no conclusion (naturalia.fr: "... so result-list membership cannot be
    # determined." under a "wrong" verdict - consistent, not asked again)
    for why, want in (("The price matches, but the page says 'En stock'. The supplied search evidence is a product "
                       "page, so result-list membership cannot be determined.", ""),
                      ("All five items are listed in the correct order.", ""),
                      ("The third item is missing, so the answer is not correct.", "wrong"),
                      ("Hence the verdict: incomplete.", "incomplete"), ("All five are correct.", "correct"),
                      ("The tool skipped one result, but nothing it reported is wrong.", "")):
        assert O.concluded_verdict(why) == want, (why, O.concluded_verdict(why))
    second = json.dumps({"cases": [{"id": IDS[1], "verdict": "correct", "membership": True, "identity": None,
                                    "fields": None, "results_exist": True,
                                    "why": "The five answered results are the page's first five, in order."}]})
    cli = Script(ANSWER, second)
    got = O.ask_reader(cli, CASES, last=True)
    assert got["calls"] == 2 and case_ids(cli.prompts[1]) == [IDS[1]], cli.prompts
    v = got["verdicts"]
    assert v[IDS[1]]["verdict"] == "correct" and v[IDS[1]]["asked_twice"] and v[IDS[1]]["first_verdict"] == "wrong", v
    assert v[IDS[0]]["verdict"] == "correct" and v[IDS[2]]["verdict"] == "correct" and "asked_twice" not in v[IDS[0]]
    assert got["again"]["asked"] and got["again"]["text"] == second


def test_truncated_missing_unreadable_and_the_cap():
    def ans(cases):
        return json.dumps({"cases": cases})
    ok = {"id": IDS[0], "verdict": "correct", "why": "All five answered results are listed in order."}
    cut = {"id": IDS[1], "verdict": "wrong", "why": "The third answered item is not among the results and the page"}
    odd = {"id": IDS[2], "verdict": "maybe", "why": "Hard to say."}
    r = O.reader_retry_cases(ans([ok, cut, odd]), IDS)
    assert set(r) == {IDS[1], IDS[2]} and r[IDS[1]].startswith("truncated") and "unreadable" in r[IDS[2]], r
    r = O.reader_retry_cases(ans([ok]), IDS)
    assert set(r) == {IDS[1], IDS[2]} and all(x.startswith("no final verdict") for x in r.values()), r
    assert O.reader_retry_cases(ans([ok, dict(ok, id=IDS[1]), dict(ok, id=IDS[2])]), IDS) == {}
    # a cut-off ANSWER (the CLI's output ends inside the object): every case is asked again
    assert set(O.reader_retry_cases(ans([ok, cut])[:-30], IDS)) == set(IDS)
    # the cap refuses the second call: the first verdicts stand
    cli = Script(ans([ok, cut, odd]))
    got = O.ask_reader(cli, CASES, budget=lambda: False)
    assert got["calls"] == 1 and len(cli.prompts) == 1 and got["again"]["asked"] is False, got
    assert got["verdicts"][IDS[1]]["verdict"] == "wrong", got["verdicts"]
    # never a third call: a second answer that is again cut off is recorded as it is
    cli = Script(ans([ok, cut, odd]), ans([cut, odd]))
    got = O.ask_reader(cli, CASES)
    assert got["calls"] == 2 and len(cli.prompts) == 2, got
    assert got["verdicts"][IDS[1]]["verdict"] == "wrong" and got["verdicts"][IDS[2]]["verdict"] == "undeterminable"
    assert got["verdicts"][IDS[1]]["asked_twice"] and got["verdicts"][IDS[0]]["verdict"] == "correct"


def test_the_benchs_default_is_one_call():
    from anyuse.execution.web_runtime import web_arch4_live as A4
    assert A4.READER_ONCE_MORE is False and A4.READER_PARSE_LAST is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
