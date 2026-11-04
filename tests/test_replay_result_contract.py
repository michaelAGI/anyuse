"""The browser replay rung under the runtime result contract (execution/cdp_replay.py).

The compiled request refuses an empty page whose "no results" statement does not
name the query, and a small page whose items do not name it
(execution/result_contract.py). The browser replay below it serves the call
when the compiled request fails, so it must not be looser:

* an empty extraction succeeds only when the page states "no results" for THIS
  query, in the statement's own line, directly under a heading that names the
  query, or a few lines below a search summary that names it with no result
  row between; a sentence in a footer or a help box is not evidence, even when
  the rows the selector no longer matches name the query nearby;
* an error page is never an empty result;
* a template that carries the contract accepts a 1-2 item page only when each
  item's key names the query, and then without the count floor;
* a template fitted visible-only reads visible-only;
* a template stored before the contract keeps its floor (it gets only the
  empty rule).

Offline: saved pages, a fake driver, a fake agent. No browser, no network, no model.
"""
import gzip
import json
import os
import re
import sys
import urllib.parse
from types import SimpleNamespace

import pytest

from anyuse.execution import attach as A
from anyuse.execution import result_contract as RC
from anyuse.execution.api_compile import (compiled_record, load_compiled, persist_compiled,
                                          with_runtime_rungs)
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.cdp_replay import (PageResult, ReplayExecutor, ReplayTemplate,
                                         template_from_recording)
from anyuse.execution.html_extract import extract, parse
from anyuse.mcpserver.compiled_tools import run_compiled

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSYS2 = "div.card-body.overflow-auto table.table.table-hover.table-sm tbody tr"
COCKTAIL = "div.center div.row div.col-sm-3"
NAV = "https://h.test/s?q={q}"


def saved():
    with gzip.open(os.path.join(HERE, "testdata", "runtime_contract",
                                "saved_pages.json.gz"), "rt") as fh:
        return json.load(fh)["pages"]


def cocktails():
    with gzip.open(os.path.join(HERE, "testdata", "web_sessions",
                                "thecocktaildb_sparse.sessions.json.gz"), "rt") as fh:
        f = json.load(fh)
    return {v: A.result_snapshot(s, v)["dom"] for s, v in zip(f["sessions"], f["values"])}


P = saved()
C = cocktails()


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))


class Driver:
    """Serves one DOM for every URL. Its text is the DOM's visible text on one line,
    which is the hardest case for the line rule: the text alone has no lines."""

    def __init__(self, dom, text=None):
        self.dom, self.opened = dom, []
        self.text = parse(dom, visible_only=True).text() if text is None else text

    def open(self, url, settle_s, timeout_s):
        self.opened.append(url)
        return PageResult(url=url, dom=self.dom, text=self.text, status=200, elapsed_ms=10.0)


def tmpl(selector, floor, contract=True, visible_only=False, url=NAV):
    expect = {"selector": selector, "min_items": floor,
              "identity": {"final_url": "rendered_url"}}
    if contract:
        expect[RC.CONTRACT_KEY] = RC.replay_contract("q")
    return ReplayTemplate(url=url, params=["q"], extract={"items": selector}, expect=expect,
                          visible_only=visible_only)


def run(template, dom, value, text=None):
    return ReplayExecutor(driver=Driver(dom, text)).execute(template, {"q": value})


def renamed(dom, cls):
    """The page with the class ``cls`` renamed wherever it appears: the selector breaks,
    the rows and their text stay."""
    def sub(m):
        tokens = [t + "-v2" if t == cls else t for t in m.group(2).split()]
        return m.group(1) + " ".join(tokens) + m.group(3)
    out = re.sub(r'(class=")([^"]*)(")', sub, dom)
    return out


def with_footer(dom, sentence):
    i = dom.lower().rfind("</body>")
    block = f"<footer><p>{sentence}</p></footer>"
    return dom[:i] + block + dom[i:] if i >= 0 else dom + block


def results_page(body, q="zzqx"):
    return (f"<html><body><header><a>Home</a><form><input value='{q}'></form></header>"
            f"<main>{body}</main><footer><p>About us</p></footer></body></html>")


# ---------------------------------------------------------------------------
# empty pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("contract", [True, False])
def test_an_unbound_sentence_on_a_drifted_page_fails(contract):
    dom = with_footer(renamed(P["msys2_boost"]["html"], "table-hover"), "No results found.")
    assert extract(dom, MSYS2) == []
    # the unmatched row "mingw-w64-boost ..." sits within the contract's character window
    assert RC.empty_evidence_text(parse(dom, visible_only=True).text(), "boost")[0]
    out = run(tmpl(MSYS2, 3, contract=contract), dom, "boost")
    assert not out["success"] and out["outcome_kind"] == "incomplete"
    assert "not as an empty result" in out["error"]
    assert "that line does not name the query" in out["error"]


@pytest.mark.parametrize("contract", [True, False])
def test_a_statement_that_names_the_query_passes(contract):
    for body in ('<table class="r"><tbody></tbody></table><p>No results found for "zzqx".</p>',
                 '<h2>Search results for "zzqx"</h2><p>No results found.</p>'):
        out = run(tmpl("table.r tbody tr", 3, contract=contract), results_page(body), "zzqx")
        assert out["success"], (body, out.get("error"))
        assert out["extracted"]["items"] == [] and out["empty_state"]
        assert out["result_contract"]["kind"] == "empty"


def test_a_statement_about_another_query_or_under_another_heading_fails():
    for body in ('<p>No results found for "kafka".</p>',
                 '<h2>Results for "zzqx"</h2><p>Sort by name</p><p>Price</p><p>Date</p>'
                 '<p>No results found.</p>'):
        out = run(tmpl("table.r tbody tr", 1), results_page(body), "zzqx")
        assert not out["success"], body


@pytest.mark.parametrize("summary", ['Showing results for "zzqx"', "Search results for zzqx",
                                     "Results for zzqx", "You searched for zzqx"])
def test_a_statement_a_few_lines_below_a_search_summary_passes(summary):
    for between in ("", "<p>Sort by name</p>", "<p>Sort by name</p><div>Newest first</div>"):
        body = (f'<p class="summary">{summary}</p>{between}<table class="r"><tbody></tbody>'
                f'</table><p class="empty">No results found.</p>')
        for contract in (True, False):
            out = run(tmpl("table.r tbody tr", 3, contract=contract), results_page(body), "zzqx")
            assert out["success"], (body, out.get("error"))
            assert out["extracted"]["items"] == [] and out["empty_state"] == "No results found"
            assert "below" in out["result_contract"]["reason"]
    # a statement about "your search" is about the search the summary names
    body = f'<p>{summary}</p><p>No results found for your search.</p>'
    assert run(tmpl("table.r tbody tr", 3), results_page(body), "zzqx")["success"]


def test_a_search_summary_does_not_bind_a_statement_past_result_rows():
    hit = '<li class="hit-v2"><a href="/p/1">zzqx tool</a> A tool</li>'
    cases = [
        # the selector broke: a row with its link sits between the summary and a help box
        f'<p>Results for "zzqx"</p><ul class="hits">{hit}</ul>'
        '<div class="help"><p>No results found.</p></div>',
        # the same row without a link still names the query
        '<p>Results for "zzqx"</p><ul class="hits"><li class="hit-v2">zzqx tool</li></ul>'
        '<div class="help"><p>No results found.</p></div>',
        # the summary-like line is itself a result's link
        '<ul class="hits"><li class="hit-v2"><a href="/p/1">Results for zzqx benchmarks</a>'
        '</li></ul><div class="help"><p>No results found.</p></div>',
        # a card whose link wraps its lines, matched on its description, not its title
        '<p>Results for "zzqx"</p><div class="grid"><a class="card-v2" href="/p/1">'
        '<div>Fast parser</div><div>A tool</div></a></div>'
        '<div class="help"><p>No results found.</p></div>',
        # a paragraph that starts like a summary is not one (the query is still within
        # the character window of the statement)
        '<p>Results for "zzqx" are ranked by downloads, then by the date of the last release. '
        'Use the filters on the left to narrow them, or sort them with the menu above the '
        'list.</p><p>No results found.</p>',
        # the statement names its own subject, another query
        '<p>Results for "zzqx"</p><p>No results found for "kafka".</p>',
        # the summary names another query
        '<p>Results for "kafka"</p><p>No results found.</p>',
        # too far below the summary
        '<p>Results for "zzqx"</p><p>Sort by</p><p>Name</p><p>Date</p><p>No results found.</p>',
    ]
    for body in cases:
        assert extract(results_page(body), "ul.hits li.hit") == []
        out = run(tmpl("ul.hits li.hit", 1), results_page(body), "zzqx")
        assert not out["success"], body
        assert "not as an empty result" in out["error"]


@pytest.mark.parametrize("summary, statement", [
    ("Results for zzqx", "No results available for kafka."),
    ("Results for zzqx", "No results returned for kafka."),
    ("Results for zzqx", "No results match kafka."),
    ("Showing results for zzqx", "No items matched kafka"),
    ("Suchergebnisse für zzqx", "Keine Ergebnisse für kafka."),
    ("Suchergebnisse für zzqx", "Keine Treffer gefunden für kafka."),
    ("Ergebnisse für zzqx", "Keine Treffer zu kafka."),
    ("Résultats pour zzqx", "Aucun résultat pour kafka."),
    ("Résultats pour zzqx", "Aucun résultat ne correspond à kafka."),
    ("Resultados para zzqx", "Sin resultados para kafka."),
    ("Resultados para zzqx", "No se han encontrado resultados para kafka."),
    ("Risultati per zzqx", "Nessun risultato trovato per kafka."),
])
def test_a_statement_that_names_another_subject_is_not_bound_by_the_summary(summary, statement):
    out = run(tmpl("ul.hits li.hit", 1), results_page(f"<p>{summary}</p><p>{statement}</p>"),
              "zzqx")
    assert not out["success"], (summary, statement)


@pytest.mark.parametrize("summary, statement", [
    ("Suchergebnisse für zzqx", "Keine Ergebnisse gefunden."),
    ("Ergebnisse für zzqx", "Keine Treffer für Ihre Suche."),
    ("Résultats de la recherche pour zzqx", "Aucun résultat."),
    ("Résultats pour zzqx", "Aucun résultat ne correspond à votre recherche."),
    ("Resultados de búsqueda para zzqx", "No se han encontrado resultados."),
    ("Risultati della ricerca per zzqx", "Nessun risultato trovato."),
    ("Results for zzqx", "No results match your search."),
    ("Results for zzqx", "No results found for this query."),
    # the next sentence is not the statement's subject
    ("Results for zzqx", "No results found. Search for another name."),
    ("Results for zzqx", "No results found. For help, write to us."),
])
def test_a_statement_about_the_search_itself_is_bound_by_the_summary(summary, statement):
    out = run(tmpl("ul.hits li.hit", 1), results_page(f"<p>{summary}</p><p>{statement}</p>"),
              "zzqx")
    assert out["success"] and out["empty_state"], (summary, statement, out.get("error"))


def test_the_driver_text_binds_a_statement_only_right_below_a_summary():
    dom = results_page('<table class="r"><tbody></tbody></table>')
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx",
              text="Search\nResults for zzqx\nNo results found\nAbout us")
    assert out["success"] and out["empty_state"]
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx",
              text="Search\nResults for zzqx\nzzqx tool\nNo results found\nAbout us")
    assert not out["success"]
    # text lines carry no links, so a row that does not name the query is not seen
    # as a row: only the line right above binds
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx",
              text="Search\nResults for zzqx\nFast parser\nNo results found\nAbout us")
    assert not out["success"]


def test_a_search_summary_off_screen_does_not_bind():
    # the summary is in the DOM but a stylesheet hides it; the breadcrumb names the query
    dom = results_page('<nav>Home / zzqx</nav><p class="visually-gone">Results for zzqx</p>'
                       '<table class="r"><tbody></tbody></table><p>No results found.</p>')
    shown = "Home / zzqx\nNo results found.\nAbout us"
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx", text=shown)
    assert not out["success"]
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx",
              text="Home / zzqx\nResults for zzqx\nNo results found.\nAbout us")
    assert out["success"] and out["empty_state"] == "No results found"


@pytest.mark.parametrize("contract", [True, False])
def test_no_matching_noun_for_the_query_is_a_statement(contract):
    out = run(tmpl("ul.hits li.hit", 3, contract=contract),
              results_page('<ul class="hits"></ul><p>No matching crates for zzqx</p>'), "zzqx")
    assert out["success"], out.get("error")
    assert out["empty_state"] == "No matching crates"
    out = run(tmpl("ul.hits li.hit", 3, contract=contract),
              results_page('<ul class="hits"></ul><p>No matching crates for kafka</p>'), "zzqx")
    assert not out["success"]


def test_a_result_cards_heading_is_not_the_statements_heading():
    # the cards' class changed; each card is only a title naming the query, and a help
    # box follows the list: the heading above the sentence is a card's, not the section's
    cards = "".join(f'<article class="card-v2"><h3>zzqx guide {i}</h3></article>' for i in range(3))
    dom = results_page(f'<div class="grid">{cards}</div><div class="help"><p>No matches</p></div>')
    assert extract(dom, "div.grid article.card") == []
    out = run(tmpl("div.grid article.card", 1), dom, "zzqx")
    assert not out["success"], out
    # the same sentence under the section's own heading is the page's statement
    dom = results_page('<h2>Results for "zzqx"</h2><div class="grid"><p>No matches</p></div>')
    assert run(tmpl("div.grid article.card", 1), dom, "zzqx")["success"]


def test_a_heading_that_does_not_name_the_query_is_not_evidence():
    # a help box with its own heading after the drifted rows: the character window reaches
    # the rows, but the heading right above the sentence does not name the query
    rows = "".join(f'<li class="hit-v2">zzqx tool {i}</li>' for i in range(3))
    dom = results_page(f'<ul class="hits">{rows}</ul><div class="help"><h4>Search tips</h4>'
                       '<p>No results found. Check the spelling.</p></div>')
    assert RC.empty_evidence_text(parse(dom, visible_only=True).text(), "zzqx")[0]
    out = run(tmpl("ul.hits li.hit", 1), dom, "zzqx")
    assert not out["success"], out


def test_a_statement_the_browser_does_not_show_is_not_evidence():
    # an empty state hidden by a stylesheet class stays in the markup while the rows are
    # shown; the browser's text (innerText) leaves it out, so it is not the page's statement
    rows = "".join(f'<li class="hit-v2">zzqx tool {i}</li>' for i in range(3))
    dom = results_page(f'<ul class="hits">{rows}</ul><div class="help"><p>No matches</p></div>'
                       '<div class="empty hidden"><p>No results for "zzqx"</p></div>')
    text = "Home\nzzqx tool 0\nzzqx tool 1\nzzqx tool 2\nNo matches\nAbout us"
    out = run(tmpl("ul.hits li.hit", 1), dom, "zzqx", text=text)
    assert not out["success"], out
    # the same statement on screen is the page's statement
    shown = text.replace("About us", 'No results for "zzqx"\nAbout us')
    dom = results_page('<ul class="hits"></ul><div class="empty"><p>No results for "zzqx"</p></div>')
    out = run(tmpl("ul.hits li.hit", 1), dom, "zzqx", text=shown)
    assert out["success"] and out["empty_state"], out


def test_visible_lines_follow_the_blocks():
    root = parse("<div>A <b>b</b><p>C</p>D<h2>E <i>f</i></h2><ul><li>g</li>"
                 "<li>h <a>i</a></li></ul><p style='display:none'>hidden</p></div>",
                 visible_only=True)
    lines = RC.visible_lines(root)
    assert [t for t, _b, _h in lines] == ["A b", "C", "D", "E f", "g", "h i"]
    assert [h.tag if h is not None else None for _t, _b, h in lines] == \
        [None, None, None, "h2", None, None]


def test_an_error_banner_is_not_an_empty_result():
    body = '<div class="alert"><p>Something went wrong while searching for "zzqx": 0 results</p></div>'
    phrase, why = RC.empty_evidence_page(results_page(body), "zzqx")
    assert phrase == "" and "reports an error" in why
    for contract in (True, False):
        # the blocked-page check may stop it first; either way nothing is served
        out = run(tmpl("table.r tbody tr", 1, contract=contract), results_page(body), "zzqx")
        assert not out["success"]


def test_the_driver_text_is_used_when_the_markup_holds_no_statement():
    # a statement drawn outside the serialised DOM: the text's own lines decide
    dom = results_page('<table class="r"><tbody></tbody></table>')
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx", text="Search\nNo results for zzqx\nAbout us")
    assert out["success"] and out["empty_state"]
    out = run(tmpl("table.r tbody tr", 1), dom, "zzqx", text="Search zzqx\nNo results\nAbout us")
    assert not out["success"]


# ---------------------------------------------------------------------------
# small pages: the sparse rule, only for templates that carry the contract
# ---------------------------------------------------------------------------

def test_small_correct_pages_pass_at_floor_3_with_the_contract_and_fail_without():
    pages = [(MSYS2, P["msys2_boost"]["html"], "boost"),
             (COCKTAIL, C["Boyarsky"], "Boyarsky"), (COCKTAIL, C["Boston"], "Boston")]
    for sel, dom, value in pages:
        want = extract(dom, sel, limit=100)
        assert 1 <= len(want) <= 2
        out = run(tmpl(sel, 3), dom, value)
        assert out["success"], (value, out.get("error"))
        assert out["extracted"]["items"] == want
        assert out["result_contract"]["kind"] == "sparse"
        old = run(tmpl(sel, 3, contract=False), dom, value)
        assert not old["success"] and "postcondition failed" in old["error"], value


def test_another_querys_small_page_fails_even_at_floor_1():
    out = run(tmpl(MSYS2, 1), P["msys2_boost"]["html"], "ffmpeg")
    assert not out["success"] and out["error"].startswith("result contract refused")
    assert out["result_contract"]["kind"] == "refused"
    out = run(tmpl(COCKTAIL, 1), C["Boston"], "Boyarsky")
    assert not out["success"]
    # a template stored before the contract keeps the floor alone
    assert run(tmpl(MSYS2, 1, contract=False), P["msys2_boost"]["html"], "ffmpeg")["success"]


def test_a_contract_answer_parses_the_page_twice(monkeypatch):
    # once to extract, once for the selector count, which the contract check reuses
    from anyuse.execution import html_extract
    real, calls = html_extract.parse, []

    def counting(markup, *a, **kw):
        calls.append(kw.get("visible_only", a[0] if a else False))
        return real(markup, *a, **kw)
    monkeypatch.setattr(html_extract, "parse", counting)
    for visible in (False, True):
        calls.clear()
        out = run(tmpl(MSYS2, 3, visible_only=visible), P["msys2_openssl"]["html"], "openssl")
        assert out["success"] and out["result_contract"]["kind"] == "list"
        assert calls == [visible, visible], calls
        calls.clear()
        out = run(tmpl(MSYS2, 1, visible_only=visible), P["msys2_boost"]["html"], "boost")
        assert out["success"] and out["result_contract"]["kind"] == "sparse"
        assert calls == [visible, visible], calls


def test_a_list_below_the_floor_still_fails_with_the_contract():
    dom = P["msys2_openssl"]["html"]
    assert len(extract(dom, MSYS2)) == 3
    out = run(tmpl(MSYS2, 4), dom, "openssl")
    assert not out["success"] and "postcondition failed" in out["error"]
    out = run(tmpl(MSYS2, 3), dom, "openssl")
    assert out["success"] and out["result_contract"]["kind"] == "list"


# ---------------------------------------------------------------------------
# visible-only templates
# ---------------------------------------------------------------------------

ROWS = ["alpha parser", "beta server", "gamma client", "delta parser", "epsilon tool"]


def listing(show):
    hide = ' style="display: none"'
    lis = "".join(f'<li class="pkg"{"" if show in r else hide}><a>{r}</a></li>' for r in ROWS)
    return f'<html><body><input id="q"><ul class="list">{lis}</ul></body></html>'


def test_a_hide_filter_page_returns_only_the_visible_rows():
    sel = "ul.list li.pkg"
    for contract in (True, False):
        out = run(tmpl(sel, 1, contract=contract, visible_only=True), listing("parser"), "parser")
        assert out["success"], out.get("error")
        assert out["extracted"]["items"] == ["alpha parser", "delta parser"]
        assert out["selector_matched"] == 2
        # a tool that reads each row's name reads the visible rows' names
        t = tmpl(sel, 1, contract=contract, visible_only=True)
        t.read_labels = True
        out = run(t, listing("parser"), "parser")
        assert out["extracted"]["labels"] == ["alpha parser", "delta parser"]
    rec = {"param": {"name": "q", "value": "parser"}, "final_url": "https://h.test/s?q=parser",
           "ground_truth_selector": sel, "ground_truth_visible_only": True,
           "ui": {"items": ["alpha parser", "delta parser"]}}
    t = template_from_recording(rec, "q", readiness=False)
    out = run(t, listing("server"), "server")
    assert out["success"] and out["extracted"]["items"] == ["beta server"]


def test_visible_only_survives_the_stored_dict():
    t = tmpl("ul li", 1, visible_only=True)
    assert t.to_dict()["visible_only"] is True
    assert ReplayTemplate.from_dict(t.to_dict()).visible_only is True
    stored = t.to_dict()
    stored.pop("visible_only")
    assert ReplayTemplate.from_dict(stored).visible_only is False


def test_template_from_recording_attaches_the_contract_and_the_flag():
    rec = {"param": {"name": "q", "value": "alpha"}, "final_url": "https://h.test/s?q=alpha",
           "ground_truth_selector": "ul li", "ui": {"items": ["alpha 1", "alpha 2", "alpha 3"]}}
    t = template_from_recording(dict(rec), "q", readiness=False)
    c = RC.contract_of(t.expect)
    assert c == RC.replay_contract("q") and c["query_param"] == "q"
    assert c["region_coverage_min"] is None and "browser replay" in c["calibration"]
    assert t.visible_only is False and t.expect["min_items"] == 3
    t = template_from_recording({**rec, "ground_truth_visible_only": True}, "q", readiness=False)
    assert t.visible_only is True
    assert ReplayTemplate.from_dict(t.to_dict()).expect[RC.CONTRACT_KEY] == c


def test_the_replay_contract_is_the_compiled_contract_without_calibration():
    from anyuse.execution.api_step import RequestTemplate
    t = RequestTemplate(url="https://h.test/s", query={"q": "{q}"}, params=["q"],
                        extract={"items": "x"}, extract_kind="html", shape="list")
    compiled = RC.new_contract(t)
    replay = RC.replay_contract("q")
    assert compiled["calibration"] == "pending"
    assert {k: v for k, v in compiled.items() if k != "calibration"} == \
        {k: v for k, v in replay.items() if k != "calibration"}


def test_the_typed_template_dict_is_unchanged():
    from anyuse.execution.discover_replay import TypedReplayTemplate
    t = TypedReplayTemplate(url="https://h.test/", params=["q"], input={"name": "q"},
                            extract={"items": "ul li"}, visible_only=True,
                            expect={"selector": "ul li", "min_items": 1})
    d = t.to_dict()
    assert set(d) == {"url", "params", "path_params", "param_transforms", "extract", "settle_s",
                      "timeout_s", "expect", "readiness", "approved_origins", "origin", "notes",
                      "read_labels", "mode", "input", "visible_only"}
    assert d["mode"] == "type" and d["input"] == {"name": "q"} and d["visible_only"] is True


# ---------------------------------------------------------------------------
# the cascade: a broken browser read is not served
# ---------------------------------------------------------------------------

CRATES = os.path.join(HERE, "testdata", "attach_crates")


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


class Api:
    def __init__(self, captured, body=None):
        self.captured, self.body = captured, body

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        return ApiResponse(200, {"Content-Type": "application/json"},
                           self.body if self.body is not None else self.captured[q])


@pytest.fixture
def crates(tmp_path):
    sessions = [json.load(open(os.path.join(CRATES, n))) for n in sorted(os.listdir(CRATES))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]
    compiled, proof, _report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=Api(captured)), preflight=lambda _c: "")
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "crates_search", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("crates.io", path=hosts)
    return SimpleNamespace(storage=storage, hosts=hosts, record=record, captured=captured,
                           doms={r["param"]["value"]: r["dom"] for r in recs})


def _agent(calls):
    def run_agent(task, **kw):
        calls.append(task)
        return {"ok": True, "outcome": "done", "result": ["tokio", "tokio-util", "tokio-stream"],
                "model_calls": 2, "cost_usd": 0.02, "steps": 3}
    return run_agent


def _call(crates, dom, calls=None):
    return run_compiled("crates_search", {"q": "tokio"}, storage=crates.storage,
                        hosts_path=crates.hosts,
                        executor=ApiStepExecutor(transport=Api(crates.captured, body='{"crates": []}')),
                        browser_executor=ReplayExecutor(driver=Driver(dom)),
                        agent_runner=_agent(calls) if calls is not None else None)


def test_the_stored_browser_rung_carries_the_contract(crates):
    browser = crates.record["browser"]["template"]
    c = RC.contract_of(browser["expect"])
    # with the compiled request's query positions and why it has them
    assert {k: v for k, v in c.items() if k != "query_calibration"} == RC.replay_contract("q")
    assert "fitting list page" in c["query_calibration"]
    assert browser["visible_only"] is False


def test_a_drifted_page_with_a_footer_sentence_goes_to_the_agent_not_the_browser(crates, monkeypatch):
    sel = crates.record["browser"]["template"]["extract"]["items"]
    dom = with_footer(renamed(crates.doms["tokio"], "svelte-14b1qhn"), "No results found.")
    assert extract(dom, sel) == []
    # the browser replay checks an input known to have results: it finds none on this page either
    out = _call(crates, dom)
    browser = next(t for t in out["rungs_tried"] if t["rung"] == "browser_replay")
    assert out["ok"] is False and browser["ok"] is False
    assert "which had results, find" in browser["why"]
    # with no such searches, the statement rule refuses the footer sentence
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    # without an agent nothing is served, and the browser rung does not demote the request
    out = _call(crates, dom)
    assert out["ok"] is False
    browser = next(t for t in out["rungs_tried"] if t["rung"] == "browser_replay")
    # the reason survives the cascade's shortened summary
    assert browser["ok"] is False and "not next to the query 'tokio'" in browser["why"]
    assert load_compiled(crates.storage, "crates_search")["health"].get("preferred", True) is True
    calls = []
    out = _call(crates, dom, calls)
    assert out["ok"] and out["served_by"] == "agent", out
    assert len(calls) == 1 and out["result"] == {"items": ["tokio", "tokio-util", "tokio-stream"]}


def test_a_browser_answer_reports_the_contract_verdict(crates):
    out = _call(crates, crates.doms["tokio"], [])
    assert out["ok"] and out["served_by"] == "browser_replay", out
    assert out["result_contract"]["ok"] and out["result_contract"]["kind"] == "list"


# a Polish shop: its empty page is in a language the statement rule's lists do not cover
PL_NAMES = {q: [f"{q} świeże", f"sok {q}", f"dżem {q}", f"{q} suszone"]
            for q in ("jabłko", "gruszka", "wiśnia")}
PL_SEL = "ul.produkty li.produkt"


def _pl_page(q):
    names = PL_NAMES.get(q)
    if names:
        rows = "".join(f"<li class='produkt'><a href='/p/{i}'>{n}</a></li>" for i, n in enumerate(names))
        body = f"<p class='podsumowanie'>Wyniki dla „{q}”</p><ul class='produkty'>{rows}</ul>"
    else:
        body = f"<p class='pusto'>Brak wyników dla „{q}”.</p>"
    return (f"<html><body><header><a href='/'>Strona główna</a></header><main>{body}</main>"
            f"<footer><p>Obserwuj nas</p></footer></body></html>")


def _pl_query(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]


class PlSite:
    def send(self, method, url, headers, body, timeout_s):
        return ApiResponse(200, {"content-type": "text/html"}, _pl_page(_pl_query(url)))


class PlDriver:
    def __init__(self):
        self.opened, self.captured = [], []

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        q = _pl_query(url)
        self.opened.append(q)
        if capture_traffic:
            self.captured.append(q)
        dom = _pl_page(q)
        return PageResult(url=url, dom=dom, text=parse(dom, visible_only=True).text(),
                          status=200, elapsed_ms=5.0)


class Refused:
    def send(self, method, url, headers, body, timeout_s):
        raise ConnectionRefusedError("refused")


def test_the_browser_replay_answers_an_empty_page_no_list_covers(tmp_path):
    from anyuse.execution.api_verify import compile_and_verify

    def rec(q):
        url = f"https://sklep.test/szukaj?q={q}"
        return {"param": {"name": "q", "value": q}, "final_url": url,
                "ui": {"items": list(PL_NAMES[q]), "text": ""},
                "network": [{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                             "body": _pl_page(q)}]}
    compiled, proof = compile_and_verify(rec("jabłko"), [rec("gruszka"), rec("wiśnia")],
                                         executor=ApiStepExecutor(transport=PlSite()))
    assert proof.passed, proof.reason
    record = compiled_record(compiled.template, proof.to_dict(), workflow="sklep_szukaj",
                             source={"site": "sklep.test", "origin": "test"})
    record["browser"] = {"template": tmpl(PL_SEL, 1, url="https://sklep.test/szukaj?q={q}").to_dict()}
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "sklep_szukaj", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("sklep.test", path=hosts)
    driver = PlDriver()
    out = run_compiled("sklep_szukaj", {"q": "zzqx"}, storage=storage, hosts_path=hosts,
                       executor=ApiStepExecutor(transport=Refused()),
                       browser_executor=ReplayExecutor(driver=driver))
    assert out["ok"] is True and out["served_by"] == "browser_replay", out
    assert out["result"] == {"items": []} and out["empty_state"] == "Brak wyników dla „zzqx”."
    # only the call's own page is kept as recompile evidence
    assert driver.captured == ["zzqx"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
