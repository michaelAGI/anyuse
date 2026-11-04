"""Discovery v4, offline: fake sites and a fake
CDP socket; no network, no browser, no model.

* the detail proof: fields fitted on the compile + selection pages only, each
  validation key fetched once by the compiled request and required to name the
  linking list's text; constant or non-matching fields fail; a JS-only item page
  is proved as tier 2 through the (fake) browser;
* a typed search with no URL and no XHR is proved as tier 2 by the unchanged
  learn-loop replay check, with 0 model calls, and becomes a "replay" tool;
* request economy: an identical URL is fetched once per run (memo), and the
  guard answers a repeated same-host GET from the run cache without sending,
  pacing or counting it, while a document URL carrying the session's input is
  always sent, and the audit stays clean.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
import urllib.parse

from anyuse.execution import cdp_guard as G
from anyuse.execution import discover as D
from anyuse.execution import discover_replay as R
from anyuse.execution.agent_browser import AgentRun
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

HOST = "items.example.test"
O = f"https://{HOST}"
ITEMS = [("aa11", "Alder Field"), ("bb22", "Birch Harbour"), ("cc33", "Cedar Point"),
         ("dd44", "Dogwood Plains"), ("ee55", "Elm Crossing"), ("ff66", "Fir Valley")]


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def item_site(heading="name", js_only=False):
    """Item pages with one heading and a facts table, no repeated group that changes."""
    def site(url):
        p = urllib.parse.urlsplit(url)
        html = {"content-type": "text/html; charset=utf-8"}
        if p.path == "/":
            links = "".join(f"<li><a href='/item/{c}'>{n}</a></li>" for c, n in ITEMS)
            return 200, html, _page("Items", f"<main><ul>{links}</ul></main>")
        if p.path.startswith("/item/"):
            code = p.path.split("/")[-1]
            name = dict(ITEMS).get(code)
            if not name:
                return 404, html, _page("Not found", "no")
            if js_only:
                return 200, html, _page("Loading", "<div id='app'></div>")
            h1 = {"name": name, "constant": "Item record", "wrong": "Something else " + code}[heading]
            return 200, html, _page("Item | Catalogue",
                                    f"<h1>{h1}</h1><table><tr><th>Code</th><td>{code}</td></tr>"
                                    "<tr><th>Status</th><td>active</td></tr></table>")
        return 404, html, _page("Not found", "nothing")
    return site


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s

    async def asleep(self, s):
        self.t += s


def fetcher(site, memo=True):
    clock = Clock()
    calls = []

    def c(url, headers):
        calls.append(url)
        return site(url)
    f = D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)",
                      allow_url=lambda u: True,
                      pacer=D.Pacer(1.0, clock=clock, sleeper=clock.sleep), client=c, memo=memo)
    f.calls = calls
    return f


class Transport:
    def __init__(self, site):
        self.site, self.calls = site, []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        st, h, text = self.site(url)
        return ApiResponse(status=st, headers=h, text=text)


def detail_cap():
    return D.Capability(cap="detail", template=O + "/item/{v}", where="path", param="item",
                        values=[c for c, _n in ITEMS[:4]], source="links",
                        labels={c: n for c, n in ITEMS})


class DetailProof(unittest.TestCase):

    def test_item_pages_prove_on_the_heading_that_names_the_linked_item(self):
        site = item_site()
        f = fetcher(site)
        tr = Transport(site)
        cap = D.prove_detail_fields(detail_cap(), O, f, transport=D.PacedTransport(f, inner=tr))
        self.assertEqual(cap.status, "proved", cap.reason)
        self.assertEqual(cap.proof["tier"], 1)
        self.assertEqual(cap.proof["field"], "h1")
        # fit on 2 keys by plain GET, validation keys ONLY through the compiled request
        self.assertEqual([u.rsplit("/", 1)[-1] for u in f.calls], ["aa11", "bb22"])
        self.assertEqual([u.rsplit("/", 1)[-1] for _m, u in tr.calls], ["cc33", "dd44"])
        self.assertIn("pair:code", cap.proof["keyed_pairs"])

    def test_a_constant_heading_is_page_chrome_and_fails(self):
        site = item_site("constant")
        f = fetcher(site)
        cap = D.prove_detail_fields(detail_cap(), O, f,
                                    transport=D.PacedTransport(f, inner=Transport(site)))
        self.assertNotEqual(cap.status, "proved")

    def test_a_heading_that_does_not_name_the_linked_item_fails(self):
        site = item_site("wrong")
        f = fetcher(site)
        cap = D.prove_detail_fields(detail_cap(), O, f,
                                    transport=D.PacedTransport(f, inner=Transport(site)))
        self.assertNotEqual(cap.status, "proved")

    def test_validation_mismatch_fails(self):
        base = item_site()

        def site(url):
            if url.endswith("/item/dd44"):
                return 200, {"content-type": "text/html"}, _page("x", "<h1>Alder Field</h1>")
            return base(url)
        f = fetcher(site)
        cap = D.prove_detail_fields(detail_cap(), O, f,
                                    transport=D.PacedTransport(f, inner=Transport(site)))
        self.assertNotEqual(cap.status, "proved")
        self.assertIn("does not name", cap.reason)

    def test_without_link_texts_nothing_is_matched(self):
        site = item_site()
        f = fetcher(site)
        cap = detail_cap()
        cap.labels = {}
        out = D.prove_detail_fields(cap, O, f)
        self.assertNotEqual(out.status, "proved")
        self.assertEqual(f.calls, [])

    def test_js_only_item_pages_are_tier_2_through_the_browser(self):
        site = item_site(js_only=True)
        rendered = item_site()
        f = fetcher(site)
        opened = []

        def browser_open(url, value):
            opened.append(url)
            st, _h, body = rendered(url)
            return AgentRun(value=value, start_url=url, final_url=url, dom=body)
        cap = D.prove_detail_fields(detail_cap(), O, f, browser_open=browser_open,
                                    transport=D.PacedTransport(f, inner=Transport(site)))
        self.assertEqual(cap.status, "proved", cap.reason)
        self.assertEqual(cap.proof["tier"], 2)
        self.assertEqual(len(opened), 4)
        self.assertEqual(cap.proof["replay_tool"]["tier"], "replay")

    def test_prove_uses_the_detail_proof_first_only_when_asked(self):
        site = item_site()
        os.environ["ANYUSE_API_MODALITY"] = "on"
        f = fetcher(site)
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=Transport(site)))
        cap = D.prove(detail_cap(), O, f, executor=ex, proof_preflight=lambda c, r: "",
                      transport=D.PacedTransport(f, inner=Transport(site)), detail_fields=True)
        self.assertEqual(cap.status, "proved")
        self.assertEqual(cap.proof_kind, "detail_fields")
        self.assertEqual(cap.proof["attempts"][0]["kind"], "detail_fields")

    def test_page_fields(self):
        f = D.page_fields("<html><head><title>T</title><meta property='og:title' content='OG'>"
                          "</head><body><h1>Head <b>one</b></h1><div itemprop='name'>N</div>"
                          "<dl><dt>Version</dt><dd>1.2</dd></dl><script>var h1</script></body>")
        self.assertEqual(f["title"], "T")
        self.assertEqual(f["og:title"], "OG")
        self.assertEqual(f["h1"], "Head one")
        self.assertEqual(f["itemprop:name"], "N")
        self.assertEqual(f["pair:version"], "1.2")


# -- typed search, replayed ---------------------------------------------------

WORDS = ["alder", "birch", "cedar", "dogwood"]


def typed_results(value):
    rows = "".join(f"<li class='hit'>{n} {value}</li>" for _c, n in ITEMS)
    return _page("Search", f"<form><input type='search' name='q'></form>"
                           f"<ul class='hits'>{rows}</ul>")


class TypedReplay(unittest.TestCase):

    def test_typed_search_is_a_tier_2_replay_tool_with_zero_model_calls(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"
        typed = []

        def typed_open(url, value, spec):
            typed.append((url, value, spec.get("name")))
            return AgentRun(value=value, start_url=url, final_url=url, dom=typed_results(value),
                            text=f"results for {value}", steps=[{"action": "type"}],
                            typed=[value])
        replay = R.GuardedReplay(lambda u, v: AgentRun(error="not used"), typed_open)
        f = fetcher(lambda u: (404, {}, ""))
        cap = D.Capability(cap="search", template=O + "/", where="type", param="q",
                           values=list(WORDS), source="typed-replay",
                           input_spec={"index": 0, "name": "q", "id": ""})
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=Transport(lambda u: (404, {}, ""))))
        D.prove_capability(cap, O, f, executor=ex, proof_preflight=lambda c, r: "",
                           replay=replay)
        self.assertEqual(cap.status, "proved", cap.reason)
        self.assertEqual(cap.proof["tier"], 2)
        tool = cap.proof["replay_tool"]
        self.assertEqual(tool["mode"], "type")
        self.assertEqual(tool["model_calls"], 0)
        # 4 recordings (1 compile, 1 selection, 2 validation) + 2 fresh replays
        self.assertEqual(len(typed), 6)
        self.assertEqual([v for _u, v, _n in typed[-2:]], WORDS[2:])
        self.assertEqual(replay.model_calls, 0)
        recs = R.tool_records(HOST, {"search": {"best": cap.to_dict()}})
        self.assertEqual(recs[0]["tier"], "replay")

    def test_typed_search_without_a_browser_is_refused(self):
        f = fetcher(lambda u: (404, {}, ""))
        cap = D.Capability(cap="search", template=O + "/", where="type", values=list(WORDS))
        D.prove_capability(cap, O, f, replay=None)
        self.assertEqual(cap.status, "refused")

    def test_navigate_replay_checks_identity_and_empty_state(self):
        def opener(url, value):
            if "none" in url:
                return AgentRun(value=value, start_url=url, final_url=url,
                                dom="<ul></ul>", text=f"No results found for {value}")
            if "vague" in url:
                return AgentRun(value=value, start_url=url, final_url=url,
                                dom="<ul></ul>", text="No results found")
            if "away" in url:
                return AgentRun(value=value, start_url=url, final_url=O + "/elsewhere",
                                dom="<ul class='hits'><li class='hit'>x</li></ul>")
            return AgentRun(value=value, start_url=url, final_url=url, dom=typed_results(value))
        rp = R.GuardedReplay(opener)
        from anyuse.execution.cdp_replay import ReplayTemplate
        t = ReplayTemplate(url=O + "/s?q={q}", params=["q"], extract={"items": "li.hit"})
        self.assertTrue(rp.execute(t, {"q": "alder"})["success"])
        self.assertTrue(rp.execute(t, {"q": "none"}).get("empty_state"))
        # a "no results" sentence that does not name the query is not an empty result
        self.assertFalse(rp.execute(t, {"q": "vague"})["success"])
        self.assertFalse(rp.execute(t, {"q": "away"})["success"])

    def test_an_empty_replay_reads_the_sites_own_empty_page_first(self):
        """The template pins the site's empty page (a Czech one, which no phrase list
        reads): a page that is that page for the query is an empty answer, with no model
        call; the same page beside rows the selector no longer reads is not."""
        from anyuse.execution import empty_page as EP
        from anyuse.execution import result_contract as RC
        from anyuse.execution.cdp_replay import ReplayTemplate

        def empty(q):
            return (f"<html><body><main><ul class='hits'></ul><p>Pro „{q}“ jsme nic "
                    f"nenašli.</p></main><footer>Kontakt</footer></body></html>")

        def full(q):
            rows = "".join(f"<li class='hit'><a href='/i/{i}'>{q} {i}</a></li>" for i in range(4))
            return (f"<html><body><main><ul class='hits'>{rows}</ul></main>"
                    f"<footer>Kontakt</footer></body></html>")
        sig = EP.learn("html", [EP.Page("html", q, markup=full(q), items=["x"])
                                for q in ("alder", "birch")],
                       [EP.Page("html", q, markup=empty(q)) for q in ("zorquavex", "limtrax")])
        self.assertEqual(sig["kind"], "html", sig.get("why"))
        contract = RC.replay_contract("q")
        contract["empty"] = {**contract["empty"], "page": sig}
        t = ReplayTemplate(url=O + "/s?q={q}", params=["q"], extract={"items": "li.hit"},
                           expect={RC.CONTRACT_KEY: contract})
        broken = lambda q: empty(q).replace(  # noqa: E731
            "<ul class='hits'></ul>",
            "".join(f"<div class='row'><a href='/i/{i}'>{q} {i}</a></div>" for i in range(3)))
        pages = {"zzqx": empty("zzqx"), "yyqv": broken("yyqv")}

        def opener(url, value):
            return AgentRun(value=value, start_url=url, final_url=url, dom=pages[value])
        rp = R.GuardedReplay(opener)
        out = rp.execute(t, {"q": "zzqx"})
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(out["empty_by"], EP.BY_SITE)
        self.assertIn("zzqx", out["empty_state"])
        self.assertEqual(out["model_calls"], 0)
        out = rp.execute(t, {"q": "yyqv"})
        self.assertFalse(out["success"], out)

    def test_an_empty_replay_refuses_a_statement_beside_unread_result_rows(self):
        """A listed-language statement on a page whose rows name the query and link
        somewhere (the selector broke): the rows are results, not an empty answer."""
        from anyuse.execution.cdp_replay import ReplayTemplate
        rows = "".join(f"<div class='card'><a href='/i/{i}'>kafka {i}</a></div>" for i in range(3))

        def opener(url, value):
            return AgentRun(value=value, start_url=url, final_url=url,
                            dom=f"<html><body><main>{rows}<p>No results found for kafka.</p>"
                                f"</main></body></html>")
        t = ReplayTemplate(url=O + "/s?q={q}", params=["q"], extract={"items": "li.hit"})
        out = R.GuardedReplay(opener).execute(t, {"q": "kafka"})
        self.assertFalse(out["success"], out)
        self.assertIn("no longer reads", out["error"])


class ProofStatus(unittest.TestCase):

    def test_a_pass_without_held_out_selection_is_checked_not_proved(self):
        from anyuse.execution import learn_loop as L
        from anyuse.execution import proof_split as PS
        site = item_site()
        f = fetcher(site)
        orig = L.continue_learning

        def fake(cand, others, **kw):
            res = L.LearnResult(outcome="tier1", reason="passed")
            res.proof_status = PS.CHECKED
            return res
        L.continue_learning = fake
        try:
            cap = detail_cap()
            D.prove_capability(cap, O, f, proof_preflight=lambda c, r: "")
        finally:
            L.continue_learning = orig
        self.assertTrue(cap.confirmed, cap.reason)
        self.assertEqual(cap.status, "checked")


# -- request economy ----------------------------------------------------------

class Memo(unittest.TestCase):

    def test_identical_url_is_fetched_once_per_run(self):
        site = item_site()
        f = fetcher(site)
        a = f.get(O + "/item/aa11")
        b = f.get(O + "/item/aa11")
        self.assertEqual(a.body, b.body)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(f.memo_hits[HOST], 1)
        self.assertEqual(f.pacer.count[HOST], 1)

    def test_a_validation_value_page_is_never_answered_from_the_memo(self):
        f = fetcher(item_site())
        f.get(O + "/item/aa11")
        f.protect(["AA11"])
        f.get(O + "/item/aa11")
        f.get(O + "/item/aa11")
        self.assertEqual(len(f.calls), 3)
        self.assertEqual(f.value_fetches, [(O + "/item/aa11", "network")] * 2)
        self.assertEqual(f.memo_hits.get(HOST, 0), 0)

    def test_memo_is_off_by_default(self):
        f = fetcher(item_site(), memo=False)
        f.get(O + "/")
        f.get(O + "/")
        self.assertEqual(len(f.calls), 2)


class FakeWS:
    def __init__(self, script=None):
        self.inbox = list(script or [])
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("method") not in ("Fetch.continueRequest", "Fetch.failRequest",
                                     "Fetch.fulfillRequest"):
            res = {}
            if msg.get("method") == "Network.getResponseBody":
                res = {"body": "console.log(1)", "base64Encoded": False}
            self.inbox.append({"id": msg["id"], "result": res})

    async def recv(self):
        if not self.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.inbox.pop(0))

    async def close(self):
        return None

    def answered(self, method):
        return [m["params"]["requestId"] for m in self.sent if m.get("method") == method]


def paused(i, url, rtype="Script"):
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": f"f{i}", "networkId": f"n{i}", "resourceType": rtype,
                       "request": {"url": url, "method": "GET"}}}


STATIC_OK = {"Content-Type": "text/javascript", "Content-Encoding": "gzip",
             "Cache-Control": "public, max-age=3600", "ETag": '"v1"'}


def response(i, url, rtype="Script", headers=None):
    return {"method": "Network.responseReceived",
            "params": {"requestId": f"n{i}", "type": rtype,
                       "response": {"url": url, "status": 200,
                                    "headers": dict(STATIC_OK if headers is None
                                                    else headers)}}}


class RunCache(unittest.TestCase):
    """Guard hardening: the run cache holds STATIC assets only, under HTTP rules;
    documents and XHR/fetch data are always fetched from the site."""

    def _session(self, budget, script, fresh=()):
        s = G.GuardedSession("http://192.0.2.1:9222", user_agent="research-probe/0.1",
                             allow_url=lambda u: True, budget=budget, fresh_values=fresh)
        s._ws = FakeWS(script)
        return s

    def _budget(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep)
        budget.cache_enabled = True
        return clock, pacer, budget

    def test_a_repeated_script_is_answered_from_the_cache_documents_never(self):
        _clock, pacer, budget = self._budget()
        js = f"https://{HOST}/app.js"
        s1 = self._session(budget, [paused(0, O + "/", "Document"), paused(1, js),
                                    response(0, O + "/", "Document"), response(1, js)])
        asyncio.run(s1.goto(O + "/"))
        n = asyncio.run(s1.harvest_cache())
        self.assertEqual(n, 1)                          # the script only
        self.assertEqual(budget.cached_urls(), [js])
        ent = next(iter(budget.cache.values()))
        hdrs = {h["name"].lower() for h in ent["headers"]}
        self.assertNotIn("content-encoding", hdrs)
        self.assertEqual(ent["etag"], '"v1"')
        s1.guard.close(s1._events)
        self.assertEqual(pacer.count[HOST], 2)
        # second load: the shell document goes to the site again, the script is
        # answered, the value URL goes to the site
        val = O + "/search?q=alder"
        s2 = self._session(budget, [paused(0, O + "/", "Document"), paused(1, js),
                                    paused(2, val, "Document"),
                                    response(0, O + "/", "Document"), response(1, js),
                                    response(2, val, "Document")], fresh=("alder",))
        asyncio.run(s2.goto(O + "/"))
        self.assertEqual(s2._ws.answered("Fetch.fulfillRequest"), ["f1"])
        self.assertEqual(s2._ws.answered("Fetch.continueRequest"), ["f0", "f2"])
        self.assertEqual(pacer.count[HOST], 4)
        self.assertEqual(budget.cache_hits[HOST], 1)
        s2.guard.close(s2._events)
        self.assertEqual(budget.violations, [])
        budget.check()
        asyncio.run(s2.harvest_cache())
        self.assertNotIn(val, budget.cached_urls())

    def test_the_cache_keys_on_the_headers_the_guard_saw_paused(self):
        """Observed: 0 cache hits in every live run. Entries were keyed
        on ``Network.requestWillBeSent``'s headers, lookups on the paused
        request's; Chrome shows different header sets at the two stages, so no
        stored entry ever matched. Both sides now use the paused headers."""
        _clock, pacer, budget = self._budget()
        css = f"https://{HOST}/app.js"
        seen = {"Accept": "*/*", "Accept-Language": "en-US",
                "Referer": O + "/"}

        def paused_h(i):
            ev = paused(i, css)
            ev["params"]["request"]["headers"] = dict(seen)
            return ev

        will = {"method": "Network.requestWillBeSent",
                "params": {"requestId": "n1", "request": {"url": css, "method": "GET",
                                                          "headers": {"Referer": O + "/"}}}}
        s1 = self._session(budget, [paused(0, O + "/", "Document"), paused_h(1), will,
                                    response(1, css)])
        asyncio.run(s1.goto(O + "/"))
        self.assertEqual(asyncio.run(s1.harvest_cache()), 1)
        s1.guard.close(s1._events)
        s2 = self._session(budget, [paused(0, O + "/", "Document"), paused_h(1)])
        asyncio.run(s2.goto(O + "/"))
        self.assertEqual(s2._ws.answered("Fetch.fulfillRequest"), ["f1"])
        s2.guard.close(s2._events)

    def test_a_value_that_is_part_of_the_host_does_not_make_every_url_bearing(self):
        _clock, _pacer, budget = self._budget()
        budget.protect(["Extension"])
        self.assertFalse(budget.carries_value(
            "https://extensions.gnome.org/static/js/app.js", "",
            {"Referer": "https://extensions.gnome.org/"}))
        self.assertTrue(budget.carries_value(
            "https://extensions.gnome.org/extension-query/?search=Extension"))
        self.assertTrue(budget.carries_value(
            "https://extensions.gnome.org/app.js", "",
            {"Referer": "https://extensions.gnome.org/#q=extension"}))

    def test_validation_data_is_always_fetched_fresh_and_instrumented(self):
        """Validation XHR/fetch answers could be cached.
        Now data is never cached, and every request carrying a validation input
        is recorded with the path it took."""
        _clock, pacer, budget = self._budget()
        budget.protect(["tokio", "rand"])                # the run's validation inputs
        api = O + "/api/search?q=tokio"
        post_api = O + "/api/graphql"
        # a fit load already fetched the very same data URL (it would have been
        # cached by v4)
        s1 = self._session(budget, [paused(0, O + "/", "Document"), paused(1, api, "XHR"),
                                    response(0, O + "/", "Document"),
                                    response(1, api, "XHR", headers={
                                        "Content-Type": "application/json",
                                        "Cache-Control": "max-age=86400"})])
        asyncio.run(s1.goto(O + "/"))
        self.assertEqual(asyncio.run(s1.harvest_cache()), 0)
        s1.guard.close(s1._events)
        # the validation load: the same URL, a data request whose value is only in
        # a header, and one whose URL does not carry it at all
        p_hdr = paused(3, post_api, "Fetch")
        p_hdr["params"]["request"]["headers"] = {"X-Query": "tokio"}
        s2 = self._session(budget, [paused(0, O + "/", "Document"), paused(1, api, "XHR"),
                                    paused(2, O + "/api/results", "Fetch"), p_hdr],
                           fresh=("tokio",))
        asyncio.run(s2.goto(O + "/"))
        self.assertEqual(s2._ws.answered("Fetch.fulfillRequest"), [])
        self.assertEqual(s2._ws.answered("Fetch.continueRequest"), ["f0", "f1", "f2", "f3"])
        ev = budget.validation_evidence()
        self.assertEqual(ev["cache"], 0)
        self.assertEqual(ev["network"], 3)             # api (twice) + the header one
        self.assertEqual([r["path"] for r in budget.value_requests],
                         ["network", "network", "network"])
        self.assertEqual(budget.data_paths.get("cache", 0), 0)
        s2.guard.close(s2._events)
        budget.check()

    def test_a_validation_request_served_without_the_site_is_a_violation(self):
        _clock, _pacer, budget = self._budget()
        budget.value_requests.append({"url": O + "/api?q=x", "type": "XHR",
                                      "method": "GET", "path": "cache"})
        with self.assertRaises(G.TrafficViolation):
            budget.check()

    def test_http_rules_decide_what_is_stored(self):
        _clock, _pacer, budget = self._budget()
        js = O + "/a.js"
        base = {"Content-Type": "text/javascript"}
        cases = [({"Cache-Control": "no-store, max-age=600"}, "no-store"),
                 ({"Cache-Control": "no-cache"}, "no-cache (needs revalidation)"),
                 ({"Pragma": "no-cache"}, "no-cache (needs revalidation)"),
                 ({"Cache-Control": "max-age=600", "Vary": "*"}, "vary *"),
                 ({"Cache-Control": "max-age=0"}, "stale on arrival"),
                 ({"Cache-Control": "max-age=60", "Age": "120"}, "stale on arrival"),
                 ({}, "no freshness information"),
                 ({"Cache-Control": "max-age=600", "Set-Cookie": "a=b"}, "sets a cookie")]
        for extra, why in cases:
            budget.cache_refused.clear()
            self.assertFalse(budget.remember(js, 200, dict(base, **extra), "x"), extra)
            self.assertEqual(list(budget.cache_refused), [why], extra)
        self.assertFalse(budget.remember(js, 200, {"Cache-Control": "max-age=600"}, "x",
                                         rtype="XHR"))
        self.assertFalse(budget.remember(js, 200, {"Cache-Control": "max-age=600"}, "x",
                                         rtype="Document"))
        self.assertFalse(budget.remember(js, 200, {"Cache-Control": "max-age=600"}, "x",
                                         method="POST"))
        self.assertFalse(budget.remember(js, 404, {"Cache-Control": "max-age=600"}, "x"))
        self.assertFalse(budget.remember(js, 200, {"Cache-Control": "max-age=600"}, "x",
                                         request_headers={"Authorization": "Bearer z"}))
        budget.protect(["alder"])
        self.assertFalse(budget.remember(O + "/i18n/alder.js", 200,
                                         {"Cache-Control": "max-age=600"}, "x"))
        # heuristic freshness from Last-Modified and an explicit Expires
        self.assertTrue(budget.remember(O + "/b.js", 200, {
            "Date": "Wed, 23 Sep 2026 10:00:00 GMT",
            "Last-Modified": "Mon, 21 Sep 2026 10:00:00 GMT"}, "x"))
        self.assertTrue(budget.remember(O + "/c.js", 200, {
            "Date": "Wed, 23 Sep 2026 10:00:00 GMT",
            "Expires": "Wed, 23 Sep 2026 11:00:00 GMT"}, "x"))

    def test_the_key_is_method_url_and_request_headers_and_vary_is_respected(self):
        _clock, _pacer, budget = self._budget()
        css = O + "/s.css"
        budget.remember(css, 200, {"Cache-Control": "max-age=600", "Vary": "Accept-Encoding, X-Theme"},
                        "a", rtype="Stylesheet", request_headers={"Accept": "text/css",
                                                                  "X-Theme": "dark"})
        self.assertIsNotNone(budget.lookup("GET", css, {"Accept": "text/css", "X-Theme": "dark"}))
        self.assertIsNone(budget.lookup("GET", css, {"Accept": "text/css", "X-Theme": "light"}))
        self.assertIsNone(budget.lookup("GET", css, {"Accept": "*/*", "X-Theme": "dark"}))
        self.assertIsNone(budget.lookup("HEAD", css, {"Accept": "text/css", "X-Theme": "dark"}))
        self.assertIsNone(budget.lookup("GET", css + "?v=2", {"Accept": "text/css",
                                                              "X-Theme": "dark"}))

    def test_a_stale_entry_is_dropped_and_the_request_goes_to_the_site(self):
        clock, pacer, budget = self._budget()
        js = O + "/app.js"
        self.assertTrue(budget.remember(js, 200, {"Cache-Control": "max-age=10"}, "x"))
        s = self._session(budget, [paused(0, O + "/", "Document"), paused(1, js)])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), ["f1"])
        clock.t += 60
        s = self._session(budget, [paused(0, O + "/", "Document"), paused(1, js)])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), [])
        self.assertEqual(budget.cached_urls(), [])

    def test_the_discovery_handoff_is_one_shot_and_never_value_bearing(self):
        _clock, pacer, budget = self._budget()
        self.assertTrue(budget.handoff(O + "/", 200, {"content-type": "text/html"}, "<html/>"))
        budget.protect(["alder"])
        self.assertFalse(budget.handoff(O + "/search?q=alder", 200, {}, "<html/>"))
        s = self._session(budget, [paused(0, O + "/", "Document")])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), ["f0"])
        s = self._session(budget, [paused(0, O + "/", "Document")])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])

    def test_cache_off_changes_nothing(self):
        clock = Clock()
        pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
        budget = G.RequestBudget(pacer, asleep=clock.asleep)
        self.assertFalse(budget.remember(O + "/a.js", 200, {"Cache-Control": "max-age=60"}, "x"))
        self.assertFalse(budget.handoff(O + "/", 200, {}, "x"))
        s = self._session(budget, [paused(0, O + "/", "Document"), response(0, O + "/", "Document")])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(asyncio.run(s.harvest_cache()), 0)
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), [])

    def test_robots_still_applies_to_cached_urls(self):
        _clock, _pacer, budget = self._budget()
        budget.remember(O + "/private.js", 200, {"Cache-Control": "max-age=60"}, "x")
        s = G.GuardedSession("http://192.0.2.1:9222", user_agent="research-probe/0.1",
                             allow_url=lambda u: "private" not in u, budget=budget)
        s._ws = FakeWS([paused(0, O + "/", "Document"), paused(1, O + "/private.js")])
        asyncio.run(s.goto(O + "/"))
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])


if __name__ == "__main__":
    unittest.main()
