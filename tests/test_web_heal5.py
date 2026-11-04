"""A SPARSE result list (1-2 items, not every item naming the value) is the query's
list when the page itself STATES its result count as the rows read and the site classifies the rows under the value
(an applied filter naming it), or the page names the value (its statement, a heading, the breadcrumb) and at least one
item names it as well. The statement and the headings are read from the visible text (``attribute_verify.count_statement``,
``sparse_evidence``), never from the card reader's map. Unknown stays a refusal.

Offline, on the fake shop of ``test_repair_ladder`` (no browser, no network, no model). What must hold:

1. coffee-perfect's shape ('Zu "Filterkaffee" wurden 2 Produkte gefunden'; one of the two items names the value): the
   list is accepted, the evidence booked (``sparse_evidence``), the status ``list`` / ``ok``;
2. a page that only echoes the query over items none of which names it stays refused (the fuzzy / "did you mean" case);
3. a count statement that does not match the rows is a CONFLICT: refused; no statement: refused (unknown is uncertain);
4. an APPLIED filter naming the value accepts the rows even when no item names it; a filter that is merely offered does not;
5. ``count_statement``: the statement forms of the 11 languages, never a cart, price, unit, rating or page count;
6. the switch ``SPARSE_EVIDENCE_RULE`` off restores the old refusal (the measurement's before reading);
7. lists of >= 3 items, one-item lists naming the value, and ``no_results`` pages are unchanged.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_web_heal5.py
"""
import asyncio
import html
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pytest  # noqa: E402

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from test_repair_ladder import BASE, Shop, eur, trace  # noqa: E402

Q = "Akkuschrauber"


class SparseShop(Shop):
    """The shop whose result page carries a RESULT-COUNT statement (``statement``: a format with ``{q}`` and ``{n}``),
    lists exactly ``items`` [(name, slug)], and may show a filter facet naming ``facet`` (``applied``: checked)."""

    def __init__(self, items, statement='Zu "{q}" wurden {n} Produkte gefunden', count=None, facet="", applied=False, **kw):
        super().__init__(**kw)
        self.items, self.statement, self.count, self.facet, self.applied = items, statement, count, facet, applied

    def results(self, q):
        n = self.count if self.count is not None else len(self.items)
        cards = "".join(f'<li class="product"><div class="info"><h3 class="name"><a class="title" href="/p/{s}">{html.escape(nm)}</a></h3>'
                        f'<span class="price">{eur(9.99)}</span></div></li>' for nm, s in self.items)
        facet = ""
        if self.facet:
            facet = (f'<div class="filter"><label><input type="checkbox" name="kaffeeart" value="x"{" checked" if self.applied else ""}> '
                     f'{html.escape(self.facet)}</label></div>')
        st = f"<p class=\"count\">{html.escape(self.statement.format(q=q, n=n))}</p>" if self.statement else ""
        body = f"<h1>Suche</h1>{st}{facet}<div class=\"listing\"><ul class=\"products\">{cards}</ul></div>"
        return (f"<html><head><title>Suche: {html.escape(q)}</title></head><body><header><nav><a href=\"/\">Start</a></nav>"
                f"<form action=\"/search\" method=\"get\" role=\"search\"><input type=\"search\" name=\"{self.qkey}\" value=\"{html.escape(q)}\">"
                f"</form></header><main>{body}</main><footer><a href=\"/impressum\">Impressum</a></footer></body></html>")


def compiled():
    g = C.compile_trace(trace(Shop(), Q, "site_search"), version=12)
    assert g["ok"], g.get("why")
    return g["tool"]


def run(site, tool, value):
    return asyncio.run(T.run_tool(D.DomPage(site), tool, value, D.no_settle, result_wait_s=0.0))


MIXED = [("Jacobs Bankett Filterkaffee Temperamentvoll", "jacobs-bankett"), ("Miko Forte ganze Kanne", "miko-forte")]
NONE_NAMED = [("Unrelated teaser", "u-1"), ("Another teaser", "u-2")]


def test_coffee_perfects_shape_is_accepted_with_the_evidence_booked():
    t = compiled()
    out = run(SparseShop(MIXED), t, "Filterkaffee")
    assert out["status"] == "ok", (out["status"], out["why"])
    assert [r["url"].rsplit("/", 1)[-1] for r in out["output"]["results"]] == ["jacobs-bankett", "miko-forte"]
    ev = out["checks"]["result_page"]["sparse_evidence"]
    assert ev["accepted"] and ev["count"] == "verified" and "statement" in ev["sources"], ev
    assert "the page states 2 result(s)" in out["why"] and "(sparse)" in out["why"]
    # the same two items without the statement: refused as before (unknown is uncertain)
    out = run(SparseShop(MIXED, statement=""), t, "Filterkaffee")
    assert out["status"] == "failed" and "does not belong" in out["why"], out["why"]
    assert out["checks"]["result_page"]["sparse_evidence"]["count"] == "uncertain"


def test_an_echo_over_unrelated_items_stays_refused_and_a_count_mismatch_is_a_conflict():
    t = compiled()
    out = run(SparseShop(NONE_NAMED), t, "Filterkaffee")              # the page says "2 Produkte" for the value, no item names it
    assert out["status"] == "failed" and "does not belong" in out["why"], out["why"]
    ev = out["checks"]["result_page"]["sparse_evidence"]
    assert not ev["accepted"] and ev["count"] == "verified" and "statement" in ev["sources"]
    out = run(SparseShop(MIXED, count=3), t, "Filterkaffee")           # the page says 3, the list has 2
    assert out["status"] == "failed"
    ev = out["checks"]["result_page"]["sparse_evidence"]
    assert ev["count"] == "conflict" and "states 3" in ev["attributes"][0]["why"]


def test_an_applied_filter_naming_the_value_accepts_an_offered_one_does_not():
    t = compiled()
    shop = SparseShop(NONE_NAMED, statement="{n} Produkte", facet="Filterkaffee", applied=True)
    rec, u = shop.route(f"{BASE}/search?q=Filterkaffee")
    read = dict(shop.read(rec, u))
    # the page reader marks the checked box (READ_JS ``checked``); the DOM reader of the fake may not: say it
    els = [dict(e) for e in read["elements"]]
    for e in els:
        if e.get("type") == "checkbox":
            e["checked"] = True
            e["text"] = "Filterkaffee"
    read["elements"] = els
    j = C.judge_result_page(t, read, "Filterkaffee")
    assert j["status"] == "list" and "an applied filter names the value" in j["why"], j.get("why")
    assert "filter" in j["sparse_evidence"]["sources"]
    # offered, not applied: the statement "2 Produkte" names no value and no item does - refused
    for e in els:
        e.pop("checked", None)
    j = C.judge_result_page(t, dict(read, elements=els), "Filterkaffee")
    assert j["status"] == "failed" and not j["sparse_evidence"]["accepted"]
    assert AV.applied_texts([{"type": "checkbox", "checked": True, "text": "A"}, {"type": "checkbox", "text": "B"},
                             {"tag": "select", "options": [{"text": "C", "selected": True}, {"text": "D"}]}]) == ["A", "C"]


def test_count_statements_of_the_languages_and_what_is_never_one():
    yes = {'Zu "Filterkaffee" wurden 2 Produkte gefunden': 2, "2 passende Artikel": 2, "Ihre Suche ergab 1 Treffer": 1,
           "Showing 1 to 2 of 2 results": 2, "2 results for 'kettle'": 2, "Produkte (2)": 2, "Ergebnisse: 1": 1,
           "2 résultats pour « cafetière »": 2, "2 risultati": 2, "2 resultados": 2, "2 resultaten": 2, "2 träffar": 2,
           "Zeige 1 bis 2 von 2": 2, "1 - 2 von 2 Artikeln": 2, "0 Produkte": 0, "2 wyniki": 2}
    for line, n in yes.items():
        st = AV.count_statement([line])
        assert st and st["count"] == n, (line, st)
    for line in ("2 Artikel im Warenkorb", "Preis ab 2 €", "Füllmenge 2 kg", "Seite 2 von 5", "5.0 (2) Bewertungen",
                 "2 Produkte pro Seite", "Lieferung in 2 Tagen", "Akkuschrauber 12V"):
        assert AV.count_statement([line]) is None, line
    # the first statement among the page's first lines wins; later lines are not read
    assert AV.count_statement(["Suche", "Zu \"x\" wurden 2 Produkte gefunden", "Produkte (9)"])["count"] == 2
    assert AV.count_statement(["x"] * 200 + ["2 Produkte"]) is None
    # the evidence verdict: count + names
    page = AV.Page("", 'Suche\nZu "Filterkaffee" wurden 2 Produkte gefunden\nJacobs\nMiko', "https://s.test/search?q=Filterkaffee")
    v = AV.sparse_evidence("Filterkaffee", 2, page)
    assert v.verdict == AV.ACCEPT and [d.status for d in v.decisions] == [AV.VERIFIED, AV.VERIFIED]
    v = AV.sparse_evidence("Espresso", 2, page, headings=["Suchergebnisse für Espresso"])
    assert [d.status for d in v.decisions] == [AV.VERIFIED, AV.VERIFIED] and v.decisions[1].readings[0].value == "heading"
    v = AV.sparse_evidence("Espresso", 1, page)
    assert v.decisions[0].status == AV.CONFLICT and v.decisions[1].status == AV.UNCERTAIN and v.verdict == AV.WITHHOLD
    assert AV.sparse_evidence("Espresso", 0, page).decisions[0].status == AV.UNCERTAIN


def test_the_switch_off_restores_the_old_refusal_and_other_lists_are_unchanged(monkeypatch):
    t = compiled()
    monkeypatch.setattr(C, "SPARSE_EVIDENCE_RULE", False)
    out = run(SparseShop(MIXED), t, "Filterkaffee")
    assert out["status"] == "failed" and "sparse_evidence" not in out["checks"]["result_page"]
    monkeypatch.setattr(C, "SPARSE_EVIDENCE_RULE", True)
    # one item naming the value: accepted as before, no evidence needed; a full list: as before; no results: as before
    out = run(SparseShop(MIXED[:1], statement=""), t, "Filterkaffee")
    assert out["status"] == "ok" and "sparse_evidence" not in out["checks"]["result_page"]
    assert run(Shop(), t, Q)["status"] == "ok" and len(run(Shop(), t, Q)["output"]["results"]) == 5
    assert run(Shop(), t, "Zxqvbn Plorf")["status"] == "no_results"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
