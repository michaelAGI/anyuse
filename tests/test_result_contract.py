"""Runtime result contracts replace the proved item floor (execution/result_contract.py).

The repeatability study's main finding: the proof-time ``min_items`` refused
37 correct, small result pages. These tests pin the replacement on REAL saved
pages from that study (testdata/runtime_contract/saved_pages.json.gz: msys2
and cheese.com responses) and on synthetic error/empty pages:

* small correct pages pass (sparse: every item's key names the query);
* small WRONG pages fail (another query's answer, a key that only mentions it);
* empty pages pass only with a query-bound "no results" statement, never on
  an error page and never on a page that merely has no rows;
* a partial list (some rows renamed) fails the proved region coverage;
* a template without a contract keeps the old floor, unchanged.

Offline: saved markup and an injected transport. No desktop, no network.
"""
import gzip
import json
import os

from anyuse.execution import result_contract as RC
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.api_verify import EquivalenceResult, CaseResult
from anyuse.execution.html_extract import extract

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSYS2 = "div.card-body.overflow-auto table.table.table-hover.table-sm tbody tr"
CHEESE = ("div.center-block.col-md-9 div.catalog.internal.search-results "
          "div.product-list.product-list-category div.product-item")


def pages():
    with gzip.open(os.path.join(HERE, "testdata", "runtime_contract",
                                "saved_pages.json.gz"), "rt") as fh:
        return json.load(fh)["pages"]


P = pages()


def contract(**kw):
    c = RC.new_contract(RequestTemplate(url="https://h.test/s", query={"q": "{q}"},
                                        params=["q"], extract={"items": "x"},
                                        extract_kind="html", shape="list"))
    c.update(kw)
    return c


def verdict(name, selector, value=None, **kw):
    p = P[name]
    items = extract(p["html"], selector)
    return RC.check_html(contract(**kw), p["html"], selector, {"q": value or p["value"]}, items)


class Transport:
    def __init__(self, body, status=200, ct="text/html; charset=utf-8"):
        self.body, self.status, self.ct = body, status, ct
        self.urls = []

    def send(self, method, url, headers, body, timeout_s):
        self.urls.append(url)
        return ApiResponse(status=self.status, headers={"content-type": self.ct}, text=self.body)


def html_template(selector=MSYS2, floor=4, with_contract=True, **ckw):
    t = RequestTemplate(method="GET", url="https://packages.msys2.org/search",
                        query={"q": "{q}"}, extract={"items": selector}, extract_kind="html",
                        shape="list", params=["q"],
                        expect={"status_in": [200], "content_type": "html",
                                "selector": selector, "min_items": floor})
    if with_contract:
        assert RC.attach(t)
        t.expect[RC.CONTRACT_KEY].update(ckw)
    return t


def run(template, body, value, status=200, ct="text/html; charset=utf-8"):
    return ApiStepExecutor(transport=Transport(body, status, ct)).execute(template, {"q": value})


# ---------------------------------------------------------------------------
# small correct pages (the 37 refusals of the study)
# ---------------------------------------------------------------------------

def test_one_and_two_row_pages_that_name_the_query_pass():
    v = verdict("msys2_boost", MSYS2)
    assert v.ok and v.kind == "sparse" and v.items == 1, v.reason
    v = verdict("msys2_ffmpeg", MSYS2)
    assert v.ok and v.kind == "sparse" and v.items == 2, v.reason


def test_a_three_row_page_is_a_list_without_any_floor():
    v = verdict("msys2_openssl", MSYS2)
    assert v.ok and v.kind == "list" and v.items == 3
    v = verdict("cheese_gruyere", CHEESE)
    assert v.ok and v.kind == "list" and v.items == 16      # floor was 20


def test_the_executor_returns_the_small_correct_page_the_floor_refused():
    t = html_template(floor=4)
    out = run(t, P["msys2_boost"]["html"], "boost")
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == extract(P["msys2_boost"]["html"], MSYS2)
    assert out["result_contract"]["kind"] == "sparse"


def test_without_a_contract_the_old_floor_stands_unchanged():
    t = html_template(floor=4, with_contract=False)
    out = run(t, P["msys2_boost"]["html"], "boost")
    assert not out["success"]
    assert "expected >= 4" in out["error"] and "result_contract" not in out


# ---------------------------------------------------------------------------
# small WRONG pages
# ---------------------------------------------------------------------------

def test_another_querys_small_page_fails():
    # the saved boost page served for the query "ffmpeg": one row, wrong thing
    v = verdict("msys2_boost", MSYS2, value="ffmpeg")
    assert not v.ok and "does not name the query" in v.reason
    out = run(html_template(), P["msys2_boost"]["html"], "ffmpeg")
    assert not out["success"] and out["outcome_kind"] == "incomplete"
    assert out["error"].startswith("result contract refused")


def test_a_key_that_only_mentions_the_query_is_not_identity():
    assert RC.key_names_query("mingw-w64-boost", "boost")
    assert RC.key_names_query("mingw-w64-libjpeg-turbo", "libjpeg-turbo")
    assert RC.key_names_query("Le Gruyère AOP", "gruyère")
    assert not RC.key_names_query("boostnote", "boost")
    assert not RC.key_names_query("turbo-libjpeg", "libjpeg turbo")      # order matters
    assert not RC.key_names_query("", "boost") and not RC.key_names_query("x", "")


def test_the_key_is_the_records_title_not_its_description():
    page = ("<html><body><table class='r'><tbody>"
            "<tr><td><a>zlib-ng</a></td><td>a zlib replacement, compatible with boost</td></tr>"
            "</tbody></table></body></html>")
    v = RC.check_html(contract(), page, "table.r tbody tr", {"q": "boost"},
                      extract(page, "table.r tbody tr"))
    assert not v.ok, "the description mentions boost; the row's key does not"
    v = RC.check_html(contract(), page, "table.r tbody tr", {"q": "zlib-ng"},
                      extract(page, "table.r tbody tr"))
    assert v.ok and v.kind == "sparse"


# ---------------------------------------------------------------------------
# empty pages
# ---------------------------------------------------------------------------

def _empty_page(text, rows=""):
    return (f"<html><body><header><a>Home</a></header><main><h1>Search</h1>"
            f"<table class='r'><tbody>{rows}</tbody></table><p>{text}</p></main></body></html>")


def test_an_empty_page_with_a_query_bound_no_results_statement_passes():
    page = _empty_page('No packages found for "zzqx".')
    out = run(html_template("table.r tbody tr"), page, "zzqx")
    assert out["success"] and out["extracted"]["items"] == []
    assert out["empty_state"].lower().startswith("no packages found")
    assert out["result_contract"]["kind"] == "empty"


def test_no_matching_noun_for_the_query_is_a_statement():
    out = run(html_template("table.r tbody tr"), _empty_page("No matching crates for zzqx"), "zzqx")
    assert out["success"] and out["extracted"]["items"] == []
    assert out["empty_state"] == "No matching crates"
    assert RC.empty_evidence_text("No matching npm packages for zzqx.", "zzqx")[0] == \
        "No matching npm packages"
    # still query-bound: another query's statement is not evidence
    out = run(html_template("table.r tbody tr"), _empty_page("No matching crates for kafka"), "zzqx")
    assert not out["success"] and "not as an empty result" in out["error"]
    # and "no matching" alone is not a statement
    assert RC.empty_evidence_text("No matching zzqx", "zzqx")[0] == ""


def test_an_empty_page_without_evidence_is_a_failure():
    # the real msys2 page for "uuid": a results table with no rows and no statement
    v = verdict("msys2_uuid", MSYS2)
    assert not v.ok and v.kind == "refused" and v.items == 0
    out = run(html_template(), P["msys2_uuid"]["html"], "uuid")
    assert not out["success"] and "not as an empty result" in out["error"]


def test_a_no_results_statement_about_something_else_is_not_evidence():
    page = _empty_page("Your cart has no items." + " filler" * 60 + " Search: zzqx")
    phrase, why = RC.empty_evidence_html(page, "zzqx")
    assert phrase == "" and "not next to the query" in why
    assert not run(html_template("table.r tbody tr"), page, "zzqx")["success"]


def test_an_error_page_is_never_an_empty_result():
    for text in ('Internal Server Error. No results for "zzqx".',
                 'Something went wrong while searching for "zzqx": 0 results',
                 "Service temporarily unavailable"):
        page = _empty_page(text)
        phrase, why = RC.empty_evidence_html(page, "zzqx")
        assert phrase == "", text
        out = run(html_template("table.r tbody tr"), page, "zzqx")
        assert not out["success"], text


def test_error_statuses_and_detail_pages_still_fail():
    out = run(html_template(), P["msys2_boost"]["html"], "boost", status=500)
    assert not out["success"] and "non-2xx" in out["error"]
    # cheese.com's roquefort search lands on the cheese's own page: no list,
    # no "no results" statement
    v = verdict("cheese_roquefort", CHEESE)
    assert not v.ok and v.items == 0


def test_query_binding_needs_the_query_near_the_statement():
    txt = "Search results for zzqx. No results found."
    assert RC.empty_evidence_text(txt, "zzqx")[0]
    assert not RC.empty_evidence_text("No results found.", "zzqx")[0]
    assert not RC.empty_evidence_text("zzqx", "zzqx")[0]


# ---------------------------------------------------------------------------
# partial lists: the proved region coverage
# ---------------------------------------------------------------------------

def _rows(n_ok, n_renamed, q="zlib"):
    rows = "".join(f"<tr class='row'><td><a>{q}-{i}</a></td><td>d</td></tr>" for i in range(n_ok))
    rows += "".join(f"<tr class='hit'><td><a>{q}-x{i}</a></td><td>d</td></tr>"
                    for i in range(n_renamed))
    return f"<html><body><table class='r'><tbody>{rows}</tbody></table></body></html>"


def test_a_partial_list_fails_when_region_coverage_was_proved():
    page = _rows(7, 3)
    sel = "table.r tbody tr.row"
    items = extract(page, sel)
    v = RC.check_html(contract(region_coverage_min=0.8), page, sel, {"q": "zlib"}, items)
    assert not v.ok and "partial list" in v.reason and abs(v.region_coverage - 0.7) < 1e-9
    # not calibrated (the proof could not measure it): the list is accepted
    v = RC.check_html(contract(region_coverage_min=None), page, sel, {"q": "zlib"}, items)
    assert v.ok and v.kind == "list"
    full = _rows(10, 0)
    v = RC.check_html(contract(region_coverage_min=0.8), full, sel, {"q": "zlib"},
                      extract(full, sel))
    assert v.ok and v.region_coverage == 1.0


def test_a_long_list_cut_by_the_executor_is_not_a_partial_list():
    # replay finding: 160 rows, the
    # executor keeps the first 50; the region still holds only matched rows
    page = _rows(160, 0)
    sel = "table.r tbody tr.row"
    items = extract(page, sel)
    assert len(items) == 50
    v = RC.check_html(contract(region_coverage_min=0.8), page, sel, {"q": "zlib"}, items)
    assert v.ok and v.region_coverage == 1.0, v.reason
    page = _rows(100, 60)
    v = RC.check_html(contract(region_coverage_min=0.8), page, sel, {"q": "zlib"},
                      extract(page, sel))
    assert not v.ok and "partial list" in v.reason


def test_a_sparse_page_cut_from_a_bigger_region_fails():
    page = _rows(1, 9)
    sel = "table.r tbody tr.row"
    v = RC.check_html(contract(region_coverage_min=0.8), page, sel, {"q": "zlib"},
                      extract(page, sel))
    assert not v.ok and v.items == 1


def _cards(q, n, wrap=False):
    cards = "".join(f"<article class='card'><h3><a>{q} guide {i}</a></h3><p>on {q}</p></article>"
                    for i in range(n))
    if wrap:
        cards = f"<div class='grid'>{cards}</div>"
    return f"<html><body><main><div id='results'>{cards}</div></main></body></html>"


def test_merged_records_are_refused_even_when_small():
    """A region tool (``div#results > *``) whose page gained a wrapper: the ONE
    item is now every card at once. Its key names the query, so only the
    proved keys-per-item bound stops it."""
    sel = "div#results > *"
    good = _cards("zlib", 4)
    v = RC.check_html(contract(), good, sel, {"q": "zlib"}, extract(good, sel))
    assert v.ok and v.key_signature == "h3" and v.max_keys_per_item == 1
    merged = _cards("zlib", 4, wrap=True)
    items = extract(merged, sel)
    assert len(items) == 1
    calibrated = contract(key_signature="h3", max_keys_per_item=1)
    v = RC.check_html(calibrated, merged, sel, {"q": "zlib"}, items)
    assert not v.ok and "records were merged" in v.reason
    # uncalibrated, the same page would have passed as a sparse answer:
    # the bound is what closes that door
    assert RC.check_html(contract(), merged, sel, {"q": "zlib"}, items).ok


# ---------------------------------------------------------------------------
# JSON lists
# ---------------------------------------------------------------------------

def json_template():
    t = RequestTemplate(method="GET", url="https://api.h.test/search", query={"q": "{q}"},
                        extract={"items": "results[*].name"}, extract_kind="json",
                        shape="list", params=["q"],
                        expect={"status_in": [200], "content_type": "json", "path": "results",
                                "min_items": 5, "required_keys": ["name"]})
    assert RC.attach(t)
    return t


def test_json_sparse_empty_and_renamed_key():
    t = json_template()
    j = "application/json"
    ok = run(t, json.dumps({"results": [{"name": "zzqx-core"}]}), "zzqx", ct=j)
    assert ok["success"] and ok["result_contract"]["kind"] == "sparse"
    wrong = run(t, json.dumps({"results": [{"name": "other"}]}), "zzqx", ct=j)
    assert not wrong["success"]
    empty = run(t, json.dumps({"query": "zzqx", "total": 0, "results": []}), "zzqx", ct=j)
    assert empty["success"] and empty["extracted"]["items"] == [] and empty["empty_state"]
    silent = run(t, json.dumps({"results": []}), "zzqx", ct=j)
    assert not silent["success"], "an empty list with no count and no echo is not evidence"
    renamed = run(t, json.dumps({"results": [{"title": "zzqx-core"}]}), "zzqx", ct=j)
    assert not renamed["success"]
    moved = run(t, json.dumps({"data": {"results": [{"name": "zzqx-core"}]}}), "zzqx", ct=j)
    assert not moved["success"] and "expected a list" in moved["error"]


# ---------------------------------------------------------------------------
# the proof attaches and calibrates the contract
# ---------------------------------------------------------------------------

def test_finalize_calibrates_from_what_the_validation_measured():
    t = html_template()
    t.expect[RC.CONTRACT_KEY]["region_coverage_min"] = None
    proof = EquivalenceResult(passed=True, cases=[
        CaseResult(value="a", passed=True, contract={"runtime": {"region_coverage": 1.0,
                                                                 "record_depth": 0}}),
        CaseResult(value="b", passed=True, contract={"runtime": {"region_coverage": 0.9,
                                                                 "record_depth": 0}})])
    RC.finalize(t, proof)
    c = t.expect[RC.CONTRACT_KEY]
    assert c["region_coverage_min"] == 0.8 and c["record_depth"] == 0
    proof.cases[1].contract["runtime"]["region_coverage"] = 0.5
    RC.finalize(t, proof)
    assert c["region_coverage_min"] is None and "not enforced" in c["calibration"]


def _li_cards(q, n, ad_in=None):
    rows = "".join(f"<li class='card'><h3><a href='/p/{q}-{i}'>{q} guide {i}</a></h3>"
                   + (f"<div class='ad'><a href='https://ads.test/x'>Sponsored offer</a></div>" if i == ad_in else "")
                   + f"<p>on {q}</p></li>" for i in range(n))
    return f"<html><body><main><ul class='results'>{rows}</ul></main></body></html>"


def test_an_extra_link_in_a_record_is_collapsed_once_proved_one_per_record():
    sel = "li.card a"
    good = _li_cards("zlib", 4)
    v = RC.check_html(contract(), good, sel, {"q": "zlib"}, extract(good, sel))
    assert v.ok and v.items_per_record == 1 and v.to_dict()["max_items_per_record"] == 1, v.to_dict()
    ad = _li_cards("zlib", 4, ad_in=2)
    items = extract(ad, sel)
    assert len(items) == 5 and "Sponsored offer" in items
    proved = contract(one_item_per_record=True, key_signature="h3", max_keys_per_item=0)
    v = RC.check_html(proved, ad, sel, {"q": "zlib"}, items)
    assert v.ok and v.items == 4 and v.collapsed == 1 and v.items_per_record == 2, v.to_dict()
    assert [items[i] for i in v.keep] == [f"zlib guide {i}" for i in range(4)]
    # through the executor: one item per card
    t = html_template(sel, floor=1, one_item_per_record=True, key_signature="h3", max_keys_per_item=0)
    out = run(t, ad, "zlib")
    assert out["success"] and out["extracted"]["items"] == [f"zlib guide {i}" for i in range(4)], out
    assert out["result_contract"]["collapsed"] == 1
    # a record proved before the rule (no flag): the items are unchanged
    out = run(html_template(sel, floor=1, key_signature="h3", max_keys_per_item=0), ad, "zlib")
    assert out["success"] and len(out["extracted"]["items"]) == 5 and "collapsed" not in out["result_contract"]
    # a row whose key is its bare title link keeps that link, not the ad link in another cell
    rows = "".join(f"<tr class='row'><td><a href='/p/{i}'>zlib guide {i}</a></td><td>"
                   + ("<a href='https://ads.test/x'>Sponsored offer</a>" if i == 1 else "") + "info</td></tr>"
                   for i in range(4))
    table = f"<html><body><main><table><tbody>{rows}</tbody></table></main></body></html>"
    v = RC.check_html(contract(one_item_per_record=True, key_signature="a", max_keys_per_item=1), table,
                      "tr.row a", {"q": "zlib"}, extract(table, "tr.row a"))
    assert v.ok and v.collapsed == 1 and [extract(table, "tr.row a")[i] for i in v.keep] == [
        f"zlib guide {i}" for i in range(4)], v.to_dict()
    # merged records are still refused with the flag
    merged = _cards("zlib", 4, wrap=True)
    m = RC.check_html(contract(one_item_per_record=True, key_signature="h3", max_keys_per_item=1),
                      merged, "div#results > *", {"q": "zlib"}, extract(merged, "div#results > *"))
    assert not m.ok and "records were merged" in m.reason


def test_finalize_sets_one_item_per_record_only_when_measured_on_every_page():
    t = html_template()

    def proof(*per_record):
        return EquivalenceResult(passed=True, cases=[
            CaseResult(value=str(k), passed=True,
                       contract={"runtime": {"region_coverage": 1.0, "record_depth": 2,
                                             **({} if m is None else {"max_items_per_record": m})}})
            for k, m in enumerate(per_record)])
    RC.finalize(t, proof(1, 1))
    c = t.expect[RC.CONTRACT_KEY]
    assert c["one_item_per_record"] is True
    # a re-proof that measured two items in a record, or no record structure, drops the earlier flag
    RC.finalize(t, proof(1, 2))
    assert "one_item_per_record" not in c
    c["one_item_per_record"] = True
    RC.finalize(t, proof(1, None))
    assert "one_item_per_record" not in c
    j = json_template()
    RC.finalize(j, proof(1, 1))
    assert "one_item_per_record" not in j.expect[RC.CONTRACT_KEY]


def test_compile_and_verify_attaches_a_calibrated_contract():
    from anyuse.execution.api_verify import compile_and_verify

    def page(q, n):
        rows = "".join(f"<li class='row'><a class='t'>{q} lib {i}</a><span>about {q}</span></li>"
                       for i in range(n))
        return (f"<html><body><h1>Results for {q}</h1><ul class='results'>{rows}</ul>"
                f"</body></html>")

    def rec(q, n):
        body = page(q, n)
        return {"param": {"name": "q", "value": q}, "final_url": f"https://ex.test/s?q={q}",
                "ui": {"items": [f"{q} lib {i}" for i in range(n)], "text": ""},
                "network": [{"url": f"https://ex.test/s?q={q}", "method": "GET",
                             "status": 200, "mime": "text/html", "body": body}]}

    class T:
        def send(self, method, url, headers, body, timeout_s):
            q = url.split("q=")[1]
            return ApiResponse(status=200, headers={"content-type": "text/html"},
                               text=page(q, {"beta": 5, "gamma": 6}.get(q, 4)))

    compiled, proof = compile_and_verify(rec("alpha", 6), [rec("beta", 5), rec("gamma", 6)],
                                         executor=ApiStepExecutor(transport=T()))
    assert proof.passed, proof.reason
    c = compiled.template.expect.get(RC.CONTRACT_KEY)
    assert c and c["region_coverage_min"] == 0.8 and c["query_param"] == "q"
    # every validation page showed one matched item per result row
    assert c.get("one_item_per_record") is True, c
    # the floor stays on record, but a 1-result page for a new value now passes
    assert compiled.template.expect["min_items"] >= 5
    out = ApiStepExecutor(transport=Transport(page("delta", 1))).execute(
        compiled.template, {"q": "delta"})
    assert out["success"] and out["result_contract"]["kind"] == "sparse", out.get("error")


def test_a_statement_about_another_term_under_a_heading_naming_the_query_is_not_evidence():
    # the raw response's rule is the line rule: "kafka" is the statement's own subject
    page = "<html><body><h2>Results for zzqx</h2><p>No results found for kafka.</p></body></html>"
    v = RC.check_html(contract(), page, "ul li", {"q": "zzqx"}, [])
    assert not v.ok and v.kind == "refused", v.reason


# a Turkish shop: its empty page is in a language the statement rule's lists do not cover
TR_HELP = "<p class='yardim'>Seeing no results found for “{q}” is rare: write to us.</p>"
TR_NAMES = {q: [f"{q} reçeli", f"{q} kurusu", f"taze {q}", f"{q} suyu"]
            for q in ("elma", "armut", "kiraz", "incir")}


def _tr_page(q, help_line=False, broken=False):
    names = TR_NAMES.get(q)
    cls = "urun-v2" if broken else "urun"
    if names:
        rows = "".join(f"<li class='{cls}'><a href='/u/{i}'>{n}</a></li>" for i, n in enumerate(names))
        body = f"<p class='ozet'>“{q}” için sonuçlar</p><ul class='urunler'>{rows}</ul>"
    else:
        body = f"<p class='bos'>“{q}” için sonuç bulunamadı.</p>"
    foot = TR_HELP.format(q=q) if help_line else ""
    return (f"<html><body><header><a href='/'>Ana sayfa</a></header><main><h1>Arama</h1>{body}"
            f"</main><footer>{foot}<p>Bizi takip edin</p></footer></body></html>")


def _tr_compile(help_line=False):
    from anyuse.execution.api_verify import compile_and_verify

    def rec(q):
        url = f"https://pazar.test/ara?q={q}"
        return {"param": {"name": "q", "value": q}, "final_url": url,
                "ui": {"items": list(TR_NAMES[q]), "text": ""},
                "network": [{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                             "body": _tr_page(q, help_line)}]}

    class T:
        def send(self, method, url, headers, body, timeout_s):
            q = url.split("q=")[1]
            return ApiResponse(status=200, headers={"content-type": "text/html"},
                               text=_tr_page(q, help_line))
    compiled, proof = compile_and_verify(rec("elma"), [rec("armut"), rec("kiraz")],
                                         executor=ApiStepExecutor(transport=T()))
    assert proof.passed, proof.reason
    return compiled.template


def test_an_empty_page_in_a_language_no_list_covers_is_the_sites_own_empty_page():
    out = run(_tr_compile(), _tr_page("zzqx"), "zzqx")
    assert out["success"], out.get("error")
    assert out["result_contract"]["kind"] == "empty"
    assert out["empty_state"] == "“zzqx” için sonuç bulunamadı."


def test_a_statement_beside_rows_the_selector_no_longer_reads_is_not_evidence():
    # the rows name the input and link to their products: results, not an empty answer
    rows = "".join(f"<li class='row-v2'><a href='/p/{i}'>plum {i}</a></li>" for i in range(3))
    page = (f"<html><body><main><ul class='results'>{rows}</ul>"
            f"<p>No results found for plum.</p></main></body></html>")
    v = RC.check_html(contract(), page, "ul.results li.row", {"q": "plum"}, [])
    assert not v.ok and v.kind == "refused", v.reason
    assert "link somewhere" in v.reason


def test_a_help_line_every_page_shows_is_not_a_statement_on_a_broken_page():
    # the selector no longer reads the rows; the help line names the input on every page
    out = run(_tr_compile(help_line=True), _tr_page("incir", help_line=True, broken=True), "incir")
    assert not out["success"], out


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
