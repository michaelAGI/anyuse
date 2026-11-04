"""The empty-answer cascade end to end: proof, compiled request, browser replay, run_compiled.

An empty extraction is an answer only when the site itself says so
(execution/result_contract.py ``empty_verdict``): its own empty page, learned
from searches for made-up inputs (execution/empty_page.py); the configured check
model on a product call (execution/model_check.py); a statement in the listed
languages. The fake sites here write their empty pages in Turkish and
Vietnamese, which no list covers.

Offline: fake transports, a fake driver, a fake model (``model_check.using``).
"""
import copy
import json
import sys
import time
import urllib.parse

import pytest

from anyuse.execution import empty_page as EP
from anyuse.execution import model_check as MC
from anyuse.execution import result_contract as RC
from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.api_verify import compile_and_verify
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, ReplayTemplate
from anyuse.execution.html_extract import parse
from anyuse.execution.workflow_schema import template_digest
from anyuse.mcpserver.compiled_tools import run_compiled

HOST = "pazar.test"
SEARCH = f"https://{HOST}/ara"
CATALOG = {q: [f"{q} reçeli", f"{q} kurusu", f"taze {q}", f"{q} suyu"]
           for q in ("elma", "armut", "kiraz", "incir")}


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "ANYUSE_EMPTY_PROBE", "ANYUSE_AUTO_HEAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_API_HOSTS_FILE", str(tmp_path / "hosts.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))


def shell(q, body, help_line=""):
    return ("<html><body><header><nav><a href='/'>Ana sayfa</a></nav>"
            f"<form><input name='q' value='{q}'></form></header>"
            f"<main><h1>Arama</h1>{body}</main>"
            f"<footer>{help_line}<p>Bizi takip edin</p></footer></body></html>")


def results_html(q, names, help_line=""):
    rows = "".join(f"<li class='urun'><a href='/u/{i}'>{n}</a></li>" for i, n in enumerate(names))
    return shell(q, f"<p class='ozet'>“{q}” için sonuçlar</p><ul class='urunler'>{rows}</ul>",
                 help_line)


def empty_html(q, help_line=""):
    return shell(q, f"<p class='bos'>“{q}” için sonuç bulunamadı.</p>", help_line)


class Site:
    """A Turkish shop: a result list for the catalogue, its empty page for anything else."""

    def __init__(self, page_for=None):
        self.log = []
        self.page_for = page_for

    def page(self, q):
        if self.page_for is not None:
            got = self.page_for(q)
            if got is not None:
                return got
        names = CATALOG.get(q)
        return results_html(q, names) if names else empty_html(q)

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.log.append(q)
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"}, self.page(q))


def rec(q, html=None):
    body = html or results_html(q, CATALOG[q])
    return {"param": {"name": "q", "value": q}, "final_url": f"{SEARCH}?q={q}",
            "ui": {"items": list(CATALOG[q]), "text": ""},
            "network": [{"url": f"{SEARCH}?q={q}", "method": "GET", "status": 200,
                         "mime": "text/html", "body": body}]}


def compile_site(site, seen=None):
    def preflight(compiled):
        if seen is not None:
            seen["requests"] = compiled.proof_requests
        return ""
    compiled, proof = compile_and_verify(rec("elma"), [rec("armut"), rec("kiraz")],
                                         executor=ApiStepExecutor(transport=site),
                                         preflight=preflight)
    assert proof.passed, proof.reason
    return compiled, proof


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return f"{self.base_dir}/workflow_{workflow_id}"

    def get(self, name):
        return None


def stored(tmp_path, compiled, proof, browser=None):
    record = compiled_record(compiled.template, proof.to_dict(), workflow="pazar_ara",
                             source={"site": HOST, "origin": "test"})
    if browser is not None:
        record["browser"] = {"template": browser.to_dict()}
    storage = Storage(str(tmp_path))
    persist_compiled(storage, "pazar_ara", record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host(HOST, path=hosts)
    return storage, hosts


def call(storage, hosts, site, q, **kw):
    return run_compiled("pazar_ara", {"q": q}, storage=storage, hosts_path=hosts,
                        executor=ApiStepExecutor(transport=site), **kw)


# ---------------------------------------------------------------------------
# the proof learns the site's empty page
# ---------------------------------------------------------------------------

def test_a_proof_learns_the_sites_empty_page_with_two_made_up_searches():
    site, seen = Site(), {}
    compiled, _proof = compile_site(site, seen)
    sig = RC.contract_of(compiled.template)["empty"]["page"]
    assert sig["kind"] == "html" and sig["learned"] == "proof"
    assert sig["marks"] == ["{q} icin sonuc bulunamadı"] and sig["echo"] is True
    # the two validation inputs, then the two made-up ones
    assert site.log[:2] == ["armut", "kiraz"] and site.log[2:] == sig["probes"]
    assert len(sig["probes"]) == EP.PROBES
    # the question about the new host counts them
    assert seen["requests"] == (2 + EP.PROBES, 2 + EP.PROBES)


def test_the_pinned_empty_page_answers_an_empty_search_with_one_request():
    site = Site()
    compiled, _proof = compile_site(site)
    site.log.clear()
    out = ApiStepExecutor(transport=site).execute(compiled.template, {"q": "zzqx"})
    assert out["success"], out.get("error")
    assert out["result_contract"]["empty_by"] == "site_empty_page"
    assert out["empty_state"] == "“zzqx” için sonuç bulunamadı."
    assert site.log == ["zzqx"]
    # the statement rule alone does not read Turkish: the same page is a failure without it
    bare = copy.deepcopy(compiled.template)
    assert RC.drop_empty_page(bare)
    out = ApiStepExecutor(transport=site).execute(bare, {"q": "zzqx"})
    assert not out["success"] and "no query-bound completion evidence" in out["error"]


def test_with_probes_off_a_proof_sends_no_made_up_search(monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    site, seen = Site(), {}
    compiled, _proof = compile_site(site, seen)
    assert "page" not in RC.contract_of(compiled.template)["empty"]
    assert site.log == ["armut", "kiraz"] and seen["requests"] == (2, 2)


def test_a_site_that_lists_results_for_anything_learns_no_empty_page():
    site = Site(page_for=lambda q: results_html(q, CATALOG["elma"]) if q not in CATALOG else None)
    compiled, _proof = compile_site(site)
    sig = RC.contract_of(compiled.template)["empty"]["page"]
    assert sig["kind"] == "none" and "made-up input" in sig["why"]


# ---------------------------------------------------------------------------
# the help-line gap: a line on every page is not a statement
# ---------------------------------------------------------------------------

HELP = "<p class='yardim'>Seeing no results found for “{q}” is rare: write to us.</p>"


def help_site():
    def page(q):
        names = CATALOG.get(q)
        line = HELP.format(q=q)
        return results_html(q, names, line) if names else empty_html(q, line)
    return Site(page_for=page)


def test_a_help_line_the_result_pages_show_too_is_not_a_statement():
    site = help_site()
    compiled, _proof = compile_site(site)
    sig = RC.contract_of(compiled.template)["empty"]["page"]
    # the search shows its summary and the help line naming the input, and nothing else
    blank = shell("incir", "<p class='ozet'>“incir” için sonuçlar</p>", HELP.format(q="incir"))
    site.page_for = lambda q: blank
    out = ApiStepExecutor(transport=site).execute(compiled.template, {"q": "incir"})
    assert not out["success"] and "also stands on the site's result pages" in out["error"]
    assert EP.veto(sig, RC._page_statement(blank, "incir")[2], "incir")
    # without the learned page the statement rule takes the help line
    bare = copy.deepcopy(compiled.template)
    RC.drop_empty_page(bare)
    assert ApiStepExecutor(transport=site).execute(bare, {"q": "incir"})["success"]
    # rows the selector no longer reads beside it: they read as results whatever the rule
    broken = results_html("incir", CATALOG["incir"], HELP.format(q="incir")).replace(
        "class='urun'", "class='urun-v2'")
    site.page_for = lambda q: broken
    out = ApiStepExecutor(transport=site).execute(bare, {"q": "incir"})
    assert not out["success"] and "link somewhere" in out["error"]


def test_a_page_that_is_not_the_empty_page_a_made_up_search_still_shows_states_nothing(tmp_path):
    site = help_site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    blank = shell("incir", "<p class='ozet'>“incir” için sonuçlar</p>"
                           "<p>No results found for “incir”.</p>", HELP.format(q="incir"))
    site.page_for = lambda q: blank if q == "incir" else None
    site.log.clear()
    out = call(storage, hosts, site, "incir")
    assert out["ok"] is False and "not the site's own empty page" in out["error"], out
    sig = RC.contract_of(compiled.template)["empty"]["page"]
    assert site.log[:2] == ["incir", sig["probes"][0]]


def test_an_empty_page_with_a_did_you_mean_is_still_the_sites_empty_page(tmp_path):
    site = Site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    # an input near a real one: the empty page adds a link to the search for that one
    dym = "<p class='oneri'>Bunu mu demek istediniz: <a href='/ara?q=elma'>elma</a>?</p>"
    site.page_for = lambda q: empty_html(q).replace("</main>", dym + "</main>") \
        if q == "elmaa" else None
    out = call(storage, hosts, site, "elmaa")
    assert out["ok"] is True and out["result_contract"]["empty_by"] == "site_empty_page", out


def test_the_check_model_refuses_a_help_line_when_no_empty_page_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    site = help_site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    # the search is down: no rows and no empty page, only the help line
    site.page_for = lambda q: shell(q, "", HELP.format(q=q))
    asked = []

    def no(instructions, state, obs):
        asked.append(state)
        return 0.05
    with MC.using(no):
        out = call(storage, hosts, site, "incir", cascade=True)
    assert out["ok"] is False and asked, out
    assert "read none of the page's lines" in out["error"]
    # the same page with no model: the statement rule accepts the help line
    assert call(storage, hosts, site, "incir")["ok"] is True


# ---------------------------------------------------------------------------
# the check model on a product call
# ---------------------------------------------------------------------------

VI = "<p class='thongbao'>Không tìm thấy sản phẩm nào cho “{q}”.</p>"


def test_the_check_model_reads_a_statement_no_list_covers_on_a_product_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    site = Site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    site.page_for = lambda q: None if q in CATALOG else shell(q, VI.format(q=q))
    with MC.using(lambda i, s, o: 0.95 if "Không tìm thấy" in s.splitlines()[-1] else 0.05):
        out = call(storage, hosts, site, "zzqx")
    assert out["ok"] is True, out
    assert out["result_contract"]["empty_by"] == "model_check"
    assert out["model_calls"] >= 1 and out["llm_calls"] == out["model_calls"]
    # a watch run (a pinned copy, no fall-back) asks no model and refuses the page
    record = load_compiled(storage, "pazar_ara")

    def boom(*_a):
        raise AssertionError("a watch run asked the check model")
    with MC.using(boom):
        out = run_compiled("pazar_ara", {"q": "zzqx"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=site), record=record,
                           cascade=False, record_outcome=False, post_card=False)
    assert out["ok"] is False and out["model_calls"] == 0


def _asker(answer):
    """A fake check model: ``answer(kind, line) -> P(yes)``; records ``(kind, line)``."""
    asked = []

    def ask(instructions, state, obs):
        kind = "error" if "failed to answer" in instructions else "empty"
        line = state.rsplit(">> ", 1)[-1]
        asked.append((kind, line))
        return answer(kind, line)
    return ask, asked


def _swahili(body, footer=""):
    return (f"<html><body><main>{body}<p>Hakuna matokeo ya “zzqx”.</p></main>"
            f"<footer>{footer}</footer></body></html>")


def _check_page(page, fake, sig=None):
    contract = RC.replay_contract("q")
    if sig is not None:
        contract["empty"] = {**contract["empty"], "page": sig}
    with MC.scope("product"), MC.using(fake):
        return RC.check_html(contract, page, "ul.r li", {"q": "zzqx"}, [],
                             empty=EP.Context(probe=False, learn=False))


def test_the_check_model_judges_an_error_line_before_the_empty_question():
    # a yes to the error line: the page reports an error, and nothing else is asked
    fake, asked = _asker(lambda kind, line: 0.95)
    page = _swahili("<div class='alert'><p>Something went wrong. Please try again later.</p></div>")
    v = _check_page(page, fake)
    assert not v.ok and "reports an error" in v.reason, v.reason
    assert asked == [("error", "Something went wrong. Please try again later.")], asked
    # a no to it (the model reads the line as no error): the statement is judged next
    fake, asked = _asker(lambda kind, line: 0.05 if kind == "error" else 0.95)
    v = _check_page(page, fake)
    assert v.ok and v.empty_by == "model_check", v.reason
    assert [k for k, _l in asked] == ["error", "empty"], asked
    # no answer about the error line: the phrase list refuses the page
    fake, asked = _asker(lambda kind, line: 0.5)
    v = _check_page(page, fake)
    assert not v.ok and "reports an error" in v.reason and \
        [k for k, _l in asked] == ["error"], (v.reason, asked)
    # cleared by the model, but no answer about the statement: the phrase list still vetoes
    # the statement rule
    fake, asked = _asker(lambda kind, line: 0.05 if kind == "error" else 0.5)
    english = ("<html><body><main><p>Something went wrong.</p><p>No results found for "
               "“zzqx”.</p></main></body></html>")
    v = _check_page(english, fake)
    assert not v.ok and "reports an error" in v.reason, v.reason
    # the same statement without the error: the model reads it, and no error question is asked
    fake, asked = _asker(lambda kind, line: 0.95)
    v = _check_page(_swahili(""), fake)
    assert v.ok and v.empty_by == "model_check" and [k for k, _l in asked] == ["empty"], asked


def test_no_configured_model_leaves_no_backend_on_the_evidence(monkeypatch):
    # a product call without a configured model: the error line is read by the phrase list
    # alone, and the evidence names no model backend
    monkeypatch.setenv("ANYUSE_MODEL_CHECKS", "none")
    page = EP.Page("html", "zzqx", markup=_swahili("<p>Something went wrong.</p>"))
    with MC.scope("product"):
        ev = RC.empty_verdict(RC.replay_contract("q"), page,
                              ctx=EP.Context(probe=False, learn=False))
    assert ev.state == EP.UNKNOWN and "reports an error" in ev.why, ev.why
    assert ev.backend == "" and ev.model_calls == 0 and ev.detail() == {}, ev.detail()


def test_no_error_question_on_a_page_with_rows_the_selector_no_longer_reads():
    # rows that name the input and link somewhere refuse the page whatever the model says
    # about its error line, so the check model is not asked about it
    fake, asked = _asker(lambda kind, line: 0.05)
    rows = "".join(f"<li class='hit'><a href='/p/{i}'>zzqx model {i}</a></li>" for i in range(3))
    page = _swahili(f"<p>Something went wrong.</p><ul class='new'>{rows}</ul>")
    v = _check_page(page, fake)
    assert not v.ok and asked == [], (v.reason, asked)


def _sig_with_footer(footer):
    def result_page(q):
        rows = "".join(f"<li class='r'><a href='/p/{i}'>{q} {i}</a></li>" for i in range(4))
        return (f"<html><body><main><ul class='r'>{rows}</ul></main>"
                f"<footer>{footer}</footer></body></html>")

    def made_up(q):
        return (f"<html><body><main><p>Hakuna kitu kwa “{q}”.</p></main>"
                f"<footer>{footer}</footer></body></html>")
    results = [EP.Page("html", q, markup=result_page(q), items=["x"]) for q in ("ndizi", "embe")]
    probes = [EP.Page("html", q, markup=made_up(q)) for q in ("vorquazib", "limtrax")]
    sig = EP.learn("html", results, probes)
    assert sig["kind"] == "html" and sig["shared"], sig
    return sig


def test_an_error_phrase_in_the_sites_chrome_does_not_stop_the_model():
    footer = "<p>Order desk: something went wrong with an order, we fix it.</p>"
    sig = _sig_with_footer(footer)
    page = _swahili("", footer=footer)
    fake, asked = _asker(lambda kind, line: 0.95)
    v = _check_page(page, fake, sig=sig)
    # the footer stands on the site's result pages too: chrome, not this page's answer
    assert v.ok and v.empty_by == "model_check", v.reason
    assert asked and all(k == "empty" for k, _l in asked), asked
    # without the learned page the same line is read, and no model clears it here
    v = _check_page(page, _asker(lambda kind, line: 0.5)[0])
    assert not v.ok and "reports an error" in v.reason, v.reason


def test_try_again_later_is_help_text_and_a_question_is_not_an_error():
    for help_line in ("Please try again later.", "Something went wrong? Contact us.",
                      "If something went wrong, write to us."):
        page = (f"<html><body><main><ul class='r'></ul><p>No results found for “zzqx”.</p>"
                f"<p>{help_line}</p></main></body></html>")
        v = RC.check_html(RC.replay_contract("q"), page, "ul.r li", {"q": "zzqx"}, [])
        assert v.ok and v.kind == "empty" and v.empty_by == "page_statement", (help_line, v.reason)
    # a stated error still refuses the page, beside the same statement
    for err in ("Something went wrong.", "Service temporarily unavailable",
                "Too many requests. Please try again later."):
        page = (f"<html><body><main><p>{err}</p><p>No results found for “zzqx”.</p>"
                f"</main></body></html>")
        v = RC.check_html(RC.replay_contract("q"), page, "ul.r li", {"q": "zzqx"}, [])
        assert not v.ok and "reports an error" in v.reason, (err, v.reason)


# ---------------------------------------------------------------------------
# a JSON API
# ---------------------------------------------------------------------------

def api_body(q, n_found=None, items=None):
    names = CATALOG.get(q, []) if items is None else items
    n = len(names) if n_found is None else n_found
    return json.dumps({"request": {"term": q}, "stats": {"n_found": n, "page": 1},
                       "facets": {"marka": ["yerel"] if names else []},
                       "results": [{"name": x} for x in names]})


class Api:
    def __init__(self, body=api_body):
        self.body, self.log = body, []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.log.append(q)
        return ApiResponse(200, {"content-type": "application/json"}, self.body(q))


def jrec(q):
    url = f"https://{HOST}/api/ara?q={q}"
    return {"param": {"name": "q", "value": q}, "final_url": f"{SEARCH}?q={q}",
            "ui": {"items": list(CATALOG[q]), "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200, "mime": "application/json",
                         "body": api_body(q), "resource_type": "XHR"}]}


def test_a_json_apis_empty_answer_is_learned_from_its_own_fields():
    api = Api()
    compiled, proof = compile_and_verify(jrec("elma"), [jrec("armut"), jrec("kiraz")],
                                         executor=ApiStepExecutor(transport=api))
    assert proof.passed, proof.reason
    sig = RC.contract_of(compiled.template)["empty"]["page"]
    assert sig["kind"] == "json" and sig["count_path"] == ["stats", "n_found"]
    assert sig["echo_path"] == ["request", "term"]
    out = ApiStepExecutor(transport=api).execute(compiled.template, {"q": "zzqx"})
    assert out["success"] and out["result_contract"]["empty_by"] == "site_empty_page", out
    # a count the list path no longer reads
    lying = Api(body=lambda q: api_body(q, n_found=3, items=[]))
    out = ApiStepExecutor(transport=lying).execute(compiled.template, {"q": "zzqx"})
    assert not out["success"] and "stats.n_found=3" in out["error"]
    # a 200 answer that reports an error beside the zero count and the echo
    failing = Api(body=lambda q: json.dumps({**json.loads(api_body(q)), "error": {"code": 504}}))
    out = ApiStepExecutor(transport=failing).execute(compiled.template, {"q": "zzqx"})
    assert not out["success"] and "field(s) the site's empty answer lacks" in out["error"], out


# ---------------------------------------------------------------------------
# a tool proved before: it learns at its first empty answer
# ---------------------------------------------------------------------------

def test_a_tool_proved_before_learns_its_empty_page_at_its_first_empty_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    site = Site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "on")
    record = load_compiled(storage, "pazar_ara")
    anchor = record["drift_anchor"]["value"]
    site.log.clear()
    out = call(storage, hosts, site, "zzqx")
    assert out["ok"] is True and out["result_contract"]["empty_by"] == "site_empty_page", out
    # the call, an input known to have results, two made-up inputs
    assert site.log[:2] == ["zzqx", anchor] and len(site.log) == 2 + EP.PROBES
    saved = load_compiled(storage, "pazar_ara")
    sig = saved["empty_page"]["request"]
    assert sig["for"] == template_digest(saved["template"]) and sig["learned"] == "first empty answer"
    # the template is unchanged, so its proof still holds; the next empty answer costs one request
    assert saved["proof"]["template_digest"] == template_digest(saved["template"])
    site.log.clear()
    out = call(storage, hosts, site, "qqqq")
    assert out["ok"] is True and site.log == ["qqqq"], (out, site.log)


def test_an_empty_page_the_site_changed_since_its_proof_is_learned_again(tmp_path):
    site = Site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    pinned = RC.contract_of(compiled.template)["empty"]["page"]
    # the site rewords its empty page; the result pages and the request stay as they were
    site.page_for = lambda q: None if q in CATALOG else shell(
        q, f"<div class='sonuc-yok'><p>Aradığınız “{q}” için ürün yok.</p></div>")
    site.log.clear()
    out = call(storage, hosts, site, "zzqx")
    assert out["ok"] is True and out["result_contract"]["empty_by"] == "site_empty_page", out
    # the call, the pinned page's made-up search, an input known to have results, two new ones
    assert site.log[:2] == ["zzqx", pinned["probes"][0]] and len(site.log) == 3 + EP.PROBES
    saved = load_compiled(storage, "pazar_ara")
    sig = saved["empty_page"]["request"]
    assert sig["for"] == template_digest(saved["template"])
    assert sig["marks"] == ["aradıgınız {q} icin urun yok"]
    # the pinned page and the proof are unchanged; the next empty answer reads the new page
    assert RC.contract_of(saved["template"]["expect"])["empty"]["page"] == pinned
    assert saved["proof"]["template_digest"] == template_digest(saved["template"])
    site.log.clear()
    out = call(storage, hosts, site, "qqqq")
    assert out["ok"] is True and site.log == ["qqqq"], (out, site.log)


def test_a_changed_empty_page_that_no_longer_names_the_input_is_not_learned(tmp_path):
    site = Site()
    compiled, proof = compile_site(site)
    storage, hosts = stored(tmp_path, compiled, proof)
    pinned = RC.contract_of(compiled.template)["empty"]["page"]
    # a search that finds nothing now gets an error page that does not name the input; the
    # result pages still work
    site.page_for = lambda q: None if q in CATALOG else shell(
        q, "<div class='hata'><p>Bir hata oluştu. Lütfen daha sonra tekrar deneyin.</p></div>")
    out = call(storage, hosts, site, "zzqx")
    assert out["ok"] is False and "no query-bound completion evidence" in out["error"], out
    saved = load_compiled(storage, "pazar_ara")
    # no "none" took the pinned page's place: a marker says when to learn again
    marker = saved["empty_page"]["request"]
    assert marker["kind"] == "none" and marker["retry_after"] > time.time(), marker
    assert RC.contract_of(saved["template"]["expect"])["empty"]["page"] == pinned
    # once the site serves its empty page again, the pinned page answers
    site.page_for = None
    site.log.clear()
    out = call(storage, hosts, site, "qqqq")
    assert out["ok"] is True and out["result_contract"]["empty_by"] == "site_empty_page", out
    assert site.log == ["qqqq"]


def test_an_empty_page_another_call_stored_survives_this_calls_save(tmp_path):
    from anyuse.execution.api_recompile import save_call_outcome
    site = Site()
    compiled, proof = compile_site(site)
    storage, _hosts = stored(tmp_path, compiled, proof)
    record = load_compiled(storage, "pazar_ara")              # this call's snapshot
    other = load_compiled(storage, "pazar_ara")
    other["empty_page"] = {"browser": {"kind": "none", "why": "a parallel call"}}
    persist_compiled(storage, "pazar_ara", other)             # another call learned meanwhile
    record["empty_page"] = {"request": {"kind": "none", "why": "this call"}}
    assert save_call_outcome(storage, "pazar_ara", record)
    assert set(load_compiled(storage, "pazar_ara")["empty_page"]) == {"request", "browser"}


class Driver:
    """Serves the site's page for the URL's input; records what it opened and captured."""

    def __init__(self, site):
        self.site, self.opened, self.captured = site, [], []

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.opened.append(q)
        if capture_traffic:
            self.captured.append(q)
        dom = self.site.page(q)
        return PageResult(url=url, dom=dom, text=parse(dom, visible_only=True).text(),
                          status=200, elapsed_ms=5.0)


class Down:
    """The compiled request's transport: the site does not answer it."""

    def send(self, method, url, headers, body, timeout_s):
        raise ConnectionRefusedError("refused")


def test_the_browser_replay_of_a_tool_proved_before_learns_its_empty_page(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    site = Site()
    compiled, proof = compile_site(site)
    sel = compiled.template.extract["items"]
    browser = ReplayTemplate(url=f"{SEARCH}?q={{q}}", params=["q"], extract={"items": sel},
                             expect={"selector": sel, "min_items": 1,
                                     RC.CONTRACT_KEY: RC.replay_contract("q")})
    storage, hosts = stored(tmp_path, compiled, proof, browser=browser)
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "on")
    driver = Driver(site)
    out = run_compiled("pazar_ara", {"q": "zzqx"}, storage=storage, hosts_path=hosts,
                       executor=ApiStepExecutor(transport=Down()),
                       browser_executor=ReplayExecutor(driver=driver))
    assert out["ok"] is True and out["served_by"] == "browser_replay", out
    assert out["result_contract"]["empty_by"] == "site_empty_page"
    # the call's page, then an input known to have results and two made-up ones
    assert driver.opened[0] == "zzqx" and len(driver.opened) == 1 + 1 + EP.PROBES
    # only the call's own page is captured as recompile evidence
    assert driver.captured == ["zzqx"]
    assert out["model_calls"] == 0
    saved = load_compiled(storage, "pazar_ara")
    assert saved["empty_page"]["browser"]["for"] == template_digest(saved["browser"]["template"])


# ---------------------------------------------------------------------------
# a step repair keeps the empty page of an unchanged request
# ---------------------------------------------------------------------------

def test_the_check_models_calls_are_counted_whichever_rung_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_EMPTY_PROBE", "off")
    monkeypatch.setenv("ANYUSE_AUTO_HEAL", "off")
    site = help_site()
    compiled, proof = compile_site(site)
    sel = compiled.template.extract["items"]
    browser = ReplayTemplate(url=f"{SEARCH}?q={{q}}", params=["q"], extract={"items": sel},
                             expect={"selector": sel, "min_items": 1,
                                     RC.CONTRACT_KEY: RC.replay_contract("q")})
    storage, hosts = stored(tmp_path, compiled, proof, browser=browser)
    # the search is down: no rows and no empty page, only the help line naming the input
    down = Site(page_for=lambda q: shell(q, "", HELP.format(q=q)))
    asked = []

    def no(instructions, state, obs):
        asked.append(state)
        return 0.05
    # the compiled request asks and is refused; the browser replay answers with the rows
    with MC.using(no):
        out = run_compiled("pazar_ara", {"q": "incir"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=down),
                           browser_executor=ReplayExecutor(driver=Driver(help_site())))
    assert out["ok"] is True and out["served_by"] == "browser_replay", out
    assert asked and out["model_calls"] == out["llm_calls"] == len(asked)
    # the compiled request is now out of service: the browser replay asks, is refused, and no
    # agent answers
    asked.clear()
    with MC.using(no):
        out = run_compiled("pazar_ara", {"q": "incir"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=down),
                           browser_executor=ReplayExecutor(driver=Driver(down)))
    browser_tried = next(t for t in out["rungs_tried"] if t["rung"] == "browser_replay")
    assert out["ok"] is False and "out_of_service" in out, out
    assert out["model_calls"] == browser_tried["model_calls"] == len(asked) > 0
    # a tool in service: both rungs ask and are refused
    (tmp_path / "fresh").mkdir()
    storage, hosts = stored(tmp_path / "fresh", compiled, proof, browser=browser)
    asked.clear()
    with MC.using(no):
        out = run_compiled("pazar_ara", {"q": "incir"}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=down),
                           browser_executor=ReplayExecutor(driver=Driver(down)))
    browser_tried = next(t for t in out["rungs_tried"] if t["rung"] == "browser_replay")
    assert out["ok"] is False and "out_of_service" not in out, out
    assert out["model_calls"] == len(asked) > browser_tried["model_calls"] > 0


def test_a_repair_keeps_the_empty_page_of_an_unchanged_request_and_relearns_a_changed_one():
    from anyuse.execution import step_repair as SR
    site = Site()
    compiled, _proof = compile_site(site)
    old = compiled.template
    pinned = RC.contract_of(old)["empty"]["page"]

    def ui_page(v):
        return f"{SEARCH}?q={v}", site.page(v)
    values = ["elma", "armut", "kiraz", "incir"]
    site.log.clear()
    same = copy.deepcopy(old)
    chosen, proof = SR.reprove(same, values, ApiStepExecutor(transport=site), ui_page=ui_page,
                               previous=old)
    assert proof.passed, proof.reason
    assert RC.contract_of(chosen)["empty"]["page"] == pinned
    assert not set(site.log) - set(values)                 # no made-up search
    moved = copy.deepcopy(old)
    moved.url = f"https://{HOST}/arama"
    RC.contract_of(moved)["empty"]["page"]["marks"] = ["stale"]

    class Moved(Site):
        def send(self, method, url, headers, body, timeout_s):
            assert urllib.parse.urlsplit(url).path == "/arama"
            return super().send(method, url, headers, body, timeout_s)
    site = Moved()

    def ui_moved(v):
        return f"https://{HOST}/arama?q={v}", site.page(v)
    chosen, proof = SR.reprove(moved, values, ApiStepExecutor(transport=site), ui_page=ui_moved,
                               previous=old)
    assert proof.passed, proof.reason
    relearned = RC.contract_of(chosen)["empty"]["page"]
    assert relearned["marks"] == pinned["marks"] and site.log[-EP.PROBES:] == relearned["probes"]


# ---------------------------------------------------------------------------
# a demonstration tool (an MCP web tool) learns its empty page at its proof
# ---------------------------------------------------------------------------

def test_a_demonstration_tool_learns_its_empty_page_and_answers_from_it():
    import html as _html

    import test_harness_compile as THC
    from anyuse.execution import harness_compile as HC

    class PolishShop(THC.SHOP.Shop):
        def results(self, q):
            page = super().results(q).replace("Suchergebnisse für", "Wyniki dla")
            return page.replace("<p>Keine Ergebnisse gefunden.</p>",
                                f"<p>Brak pasujących produktów dla „{_html.escape(q)}”.</p>")
    tr, site = THC.web_run(site=PolishShop())
    rep = HC.compile_and_save_web(tr, THC.storage(), site, ["Makita", "Ryobi"])
    assert rep["status"] == "ready", rep["reasons"]
    sig = rep["tool"]["empty_page"]
    assert sig["kind"] == "text" and sig["echo"] is True
    assert sig["marks"] == ["brak pasujacych produktow dla {q}"]
    out = HC.replay_web(rep["tool"], site, "Zxqvbn Plorf")
    assert out["status"] == "no_results", out.get("why")
    assert "Brak pasujących produktów" in out["checks"]["result_page"]["empty_statement"]
    # the same tool without its learned page: the statement rule does not read Polish
    bare = {k: v for k, v in rep["tool"].items() if k != "empty_page"}
    assert HC.replay_web(bare, site, "Zxqvbn Plorf")["status"] != "no_results"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
