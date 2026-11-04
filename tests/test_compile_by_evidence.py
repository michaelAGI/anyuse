"""The web compile reads evidence before its word lists (``execution/harness_compile.py``).

* which tool a run compiles to: the answer's own shape, then the configured check model's answer about the task
  (``model_check``, in the compile scope opened around that one question), then the attribute words as whole words;
* proof values: a word on every page of the demonstration (a line the site repeats on each page) is the site's
  furniture, in any language; the furniture lexicon only where fewer than two pages can be compared.

Offline: model checks faked with ``model_check.using``; no network.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402


def trace(task, result=None):
    return T.Trace(kind="web", task=task, stop="done", result=result if result is not None else {"name": "X"})


def test_the_attribute_words_are_whole_words():
    assert HC.workflow_of(trace("Find hotels in Stockholm near {q}")) == "site_search"
    assert HC.workflow_of(trace("Show the pricing page of {q}")) == "site_search"
    assert HC.workflow_of(trace("Wie viel kostet {q}? Preis bitte")) == "product_lookup"
    assert HC.workflow_of(trace("Is {q} available in black?")) == "product_lookup"
    assert HC.workflow_of(trace("Is {q} in stock")) == "product_lookup"
    # the old substring rule's own cases: a German compound ending in or starting with "Preis", Italian "costa"
    assert HC.workflow_of(trace("Was ist der Kaufpreis von {q}?")) == "product_lookup"
    assert HC.workflow_of(trace("Preisvergleich für {q}")) == "product_lookup"
    assert HC.workflow_of(trace("Quanto costa il {q}?")) == "product_lookup"


def test_the_check_model_answers_before_the_words():
    asked = []

    def yes(instructions, state, obs):
        asked.append(state)
        return 0.95

    def no(instructions, state, obs):
        asked.append(state)
        return 0.05
    turkish = trace("Bu ürünün fiyatı nedir: {q}")          # Turkish: "what is this product's price"
    assert HC.workflow_of(turkish) == "site_search"          # no model: no word of the lexicon
    with MC.using(yes):
        assert HC.workflow_of(turkish) == "product_lookup"
    with MC.using(no):
        assert HC.workflow_of(trace("Preis von {q}")) == "site_search"
    assert len(asked) == 2 and "fiyatı" in asked[0]
    assert MC.current_scope() == ""                          # the compile scope was opened for the question alone
    # the answer's own shape decides first: no question is asked
    asked.clear()
    with MC.using(yes):
        assert HC.workflow_of(trace("Bu ürünün fiyatı", {"results": [{"title": "a", "url": "u"}]})) == "site_search"
        assert HC.workflow_of(trace("x", {"name": "A", "price": "9,99 €"})) == "product_lookup"
    assert asked == []



def test_a_runs_question_about_its_task_is_asked_in_the_scope_it_holds_never_the_compile_scope(monkeypatch):
    from anyuse.execution import agent_harness as AH
    scopes, real = [], MC.check

    def check(question, *a, **kw):
        # the scope the question is asked in (the asker itself runs on the check's own thread)
        scopes.append(MC.current_scope())
        return real(question, *a, **kw)
    monkeypatch.setattr(MC, "check", check)

    def yes(instructions, state, obs):
        return 0.95
    rowless = {"search": "x", "url": "https://shop.test/s?q=x"}
    task = "Bu ürünün fiyatı nedir: {q}"                     # no word of the lexicon
    with MC.using(yes):
        # outside a scope (a replay of a given mandate, a run no model drives): nothing is asked, the lexicon decides
        assert AH.rowless_answer(rowless, task)
        assert scopes == []
        with MC.scope("agent"):
            assert not AH.rowless_answer(rowless, task)      # the model: one item's attribute, so no list answer
        # at compile time the compile scope is opened around the question
        assert HC.workflow_of(trace(task)) == "product_lookup"
    assert scopes == ["agent", "compile"] and MC.current_scope() == ""

def demo(start_text, result_text, titles):
    results = [{"title": t, "url": f"https://fiets.test/p/{i}"} for i, t in enumerate(titles)]
    els = [{"tag": "a", "text": t, "href": r["url"], "area": "main"} for t, r in zip(titles, results)]
    start = {"url": "https://fiets.test/", "text": start_text, "elements": []}
    fin = {"url": "https://fiets.test/zoeken?q=stadsfiets", "text": result_text, "elements": els}
    return {"workflow": "site_search", "value": "fiets", "steps": [{"page": start}, {"page": fin}],
            "final_page": fin, "result": {"results": results}}


TITLES = ["Gazelle Uitverkoop Stadsfiets 28 inch", "Batavus Stadsfiets Dames", "Cortina Transportfiets",
          "Sparta Elektrische fiets"]


def test_a_line_on_every_page_is_the_sites_furniture():
    # a Dutch shop's banner "Uitverkoop" (sale) stands on every page, and inside one title: never a proof value
    d = demo("Welkom\nUitverkoop\nWinkelwagen", "Uitverkoop\nWinkelwagen\nResultaten\n" + "\n".join(TITLES), TITLES)
    assert {"uitverkoop", "winkelwagen"} <= HC.site_furniture(d)
    values = [v["value"].casefold() for v in HC.site_vocabulary(d, "zadel")]
    assert "gazelle" in values and "uitverkoop" not in values, values


def test_the_furniture_lexicon_where_pages_cannot_be_compared():
    titles = ["Bosch Akkuschrauber Warenkorb", "Makita Akkuschrauber Warenkorb", "Metabo Akkuschrauber", "Einhell Set"]
    d = demo("", "Akkuschrauber\n" + "\n".join(titles), titles)
    d["steps"] = d["steps"][1:]
    assert HC.site_furniture(d) is None
    values = [v["value"].casefold() for v in HC.site_vocabulary(d, "Bohrer")]
    assert "warenkorb" not in values and "akkuschrauber" in values, values


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
