"""A/B bench: provider-backed searches, typed searches that do nothing, and searches
that redirect to a landing page, on offline fake sites (no socket, no browser, no model).

Usage::

    python tests/bench/provider_bench.py run --old <tree> --new <tree> [--python <py>] [--out <dir>]
    python tests/bench/provider_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the feature branch forked from (read only), ``--new``
the feature checkout. Each arm runs as a subprocess whose PYTHONPATH is a pkgroot directory
(in ``--out``) holding an ``anyuse`` symlink to its tree; every AnyUse path points into
``--out``. The arms write ``OLD.json`` and ``NEW.json``; ``compare`` prints the table and the
pass criteria.

Cases:

* ``provider_get``: an Algolia-style provider. The shop's page states the search key and the
  app id, and both change on every page read; the provider answers only the pair the page
  stated last. Recorded by the learn agent's recorder (``AgentRun.as_recording``), compiled
  and proved (``api_verify.compile_and_verify``), then called with six inputs nobody recorded.
* ``provider_post``: a Typesense-style ``multi_search`` POST whose key the page states and
  sends in a header; the provider's host is acknowledged, ``ANYUSE_API_READONLY_POST`` is off.
* ``dead_form_button``, ``dead_lone_control``: Enter does nothing; the input's form has a
  submit button / the input has one icon control and no form. A guarded session on a fake
  page whose submit script is evaluated by the rule the evaluated script states (the old
  script's word list, the new script's structure).
* ``dead_get_form``, ``dead_search_action``: Enter does nothing; the input sits in a GET
  form without a button / the page has a schema.org SearchAction. A demonstration step on
  recorded pages (``dom_page``) and the compile of its trace.
* ``landing``: the search URL redirects to a collection page whose data call sends the
  collection id and a filter key the landing page states. Recorded as HARs.
* ``home``: the site sends every query to its home page.
* ``spa_home``, ``http_upgrade`` (no regression): a script-routed search whose URL redirects
  to ``/`` and whose page then asks its API for the value; a search reached through an
  http-to-https redirect whose results are server-rendered. Both compiled before.

Pass: compiled HTTP tools where OLD had none (provider_get, provider_post, landing), answering
the calls; 0 provider keys in any stored record, trace or result in NEW; the dead searches
reach the results in NEW; the home-page site refused as ``search_to_home`` in NEW; the
no-regression cases compile in both arms.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from typing import Any, Dict, List

MODEL_ENV = ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
             "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY",
             "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")
CASES = ["provider_get", "provider_post", "dead_form_button", "dead_lone_control",
         "dead_get_form", "dead_search_action", "landing", "home", "spa_home", "http_upgrade"]
HTTP_CASES = ("provider_get", "provider_post", "landing")
DEAD_CASES = ("dead_form_button", "dead_lone_control", "dead_get_form", "dead_search_action")
KEEP_CASES = ("spa_home", "http_upgrade")

CATALOGUE = {
    "red": ["red fox plush", "red panda mug", "red kite poster"],
    "blue": ["blue whale lamp", "blue jay print", "blue tang towel"],
    "green": ["green frog clock", "green tea tin", "green moss rug"],
    "amber": ["amber bead necklace", "amber ale glass", "amber lamp shade"],
    "slate": ["slate coaster set", "slate cheese board", "slate wall clock"],
    "coral": ["coral reef puzzle", "coral linen cushion", "coral desk lamp"],
    "olive": ["olive wood spoon", "olive oil cruet", "olive grove print"],
    "ivory": ["ivory candle set", "ivory knit throw", "ivory tea cups"],
    "teal": ["teal velvet chair", "teal glass vase", "teal yoga mat"],
    "plum": ["plum wool scarf", "plum enamel pot", "plum silk tie"],
}
RECORDED = ["red", "blue", "green", "amber"]          # compile, fit, two validation inputs
CALLS = ["slate", "coral", "olive", "ivory", "teal", "plum"]
SHOP = "https://shop.test"


# ---------------------------------------------------------------------------
# the fake sites
# ---------------------------------------------------------------------------

def _resp(status, body, ctype="application/json"):
    from anyuse.execution.api_step import ApiResponse
    return ApiResponse(status, {"Content-Type": ctype}, body)


class ProviderShop:
    """The shop's page states the provider's key and app id; every page read issues a new pair,
    and the provider answers only the pair the page stated last."""

    def __init__(self, post=False):
        self.post, self.n, self.issued = post, 0, []
        self.key, self.app = self._rotate()

    def _rotate(self):
        self.n += 1
        pair = (f"srch-key-{self.n:06d}-zz", f"APP{self.n:07d}")
        self.issued.append(pair[0])
        return pair

    def page(self):
        cfg = json.dumps({"appId": self.app, "searchKey": self.key, "index": "prod_items"})
        return (f"<html><head><script>window.searchConfig={cfg};</script></head><body>"
                f"<input type=search name=q></body></html>")

    def read_page(self):
        self.key, self.app = self._rotate()
        return self.page()

    def answer(self, term):
        return json.dumps({"hits": [{"name": n} for n in CATALOGUE.get(term, [])], "q": term})

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.netloc == "shop.test":
            return _resp(200, self.read_page(), "text/html")
        hdrs = {str(k).lower(): v for k, v in (headers or {}).items()}
        if self.post:
            key, app = hdrs.get("x-typesense-api-key"), q.get("app")
            term = (json.loads(body or "{}").get("searches") or [{}])[0].get("q", "")
        else:
            key, app = q.get("x-algolia-api-key"), q.get("x-algolia-application-id")
            term = q.get("query", "")
        if key != self.key or app != self.app:
            return _resp(403, '{"message":"invalid key"}')
        return _resp(200, self.answer(term))

    def learn_traffic(self, term):
        """What the learn agent's browser saw for ``term``: the page, then the provider call."""
        page = self.read_page()
        if self.post:
            provider = {"url": f"https://ts.prov.test/multi_search?app={self.app}", "method": "POST",
                        "headers": {"content-type": "application/json",
                                    "x-typesense-api-key": self.key},
                        "post_data": json.dumps({"searches": [{"collection": "products", "q": term,
                                                               "query_by": "name"}]})}
        else:
            provider = {"url": (f"https://dsn.search.test/1/indexes/prod_items?query={term}"
                                f"&x-algolia-application-id={self.app}&x-algolia-api-key={self.key}"),
                        "method": "GET", "headers": {}}
        return [{"url": f"{SHOP}/", "method": "GET", "status": 200, "mime": "text/html",
                 "resource_type": "Document", "body": page, "headers": {}, "bytes": len(page)},
                {**provider, "status": 200, "mime": "application/json", "resource_type": "XHR",
                 "body": self.answer(term), "bytes": 100}]


def _learn_recording(site, term):
    from anyuse.execution.agent_browser import AgentRun
    run = AgentRun(value=term, task="search the shop", start_url=f"{SHOP}/")
    run.final_url = f"{SHOP}/"
    run.network = site.learn_traffic(term)
    run.dom = run.network[0]["body"]
    rec = run.as_recording("shop_search", "q", f"agent{RECORDED.index(term)}")
    rec["ui"] = {"items": list(CATALOGUE[term]), "text": "\n".join(CATALOGUE[term])}
    return rec


def _count_keys(blobs: List[Any], keys: List[str]) -> int:
    text = json.dumps(blobs, default=str)
    return sum(text.count(k) for k in keys)


def case_provider(post: bool) -> Dict[str, Any]:
    from anyuse.execution.api_compile import compiled_record
    from anyuse.execution.api_permission import acknowledge_host
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    site = ProviderShop(post=post)
    hosts = os.path.join(os.environ["HOME"], "api_hosts.json")
    for host in ("shop.test", "dsn.search.test", "ts.prov.test"):
        acknowledge_host(host, note="bench", path=hosts)
    recs = [_learn_recording(site, t) for t in RECORDED]
    executor = ApiStepExecutor(transport=site, readonly_post_hosts_path=hosts)
    compiled, proof = compile_and_verify(recs[0], recs[2:], executor=executor,
                                         fitting_recordings=[recs[1]])
    out: Dict[str, Any] = {"status": compiled.status, "reason": (compiled.reason or "")[:300],
                           "proof": bool(proof.passed), "proof_reason": (proof.reason or "")[:300],
                           "tool": compiled.template is not None and bool(proof.passed)}
    stored: List[Any] = [recs]
    results = []
    if compiled.template is not None:
        stored.append(compiled_record(compiled.template, proof.to_dict()))
        for term in CALLS:
            res = executor.execute(compiled.template, {"q": term})
            got = (res.get("extracted") or {}).get("items")
            results.append({"term": term, "ok": bool(res.get("success")),
                            "exact": got == CATALOGUE[term], "error": str(res.get("error") or "")[:160]})
            stored.append(res)
    out["calls"] = results
    out["keys_stored"] = _count_keys(stored, site.issued)
    out["kind"] = "plan" if compiled.template is not None and getattr(compiled.template, "steps", None) \
        else ("request" if compiled.template is not None else "")
    return out


# --- typed searches whose Enter does nothing ---------------------------------

class FakePage:
    """A guarded session's websocket on a page whose Enter does nothing. The submit script is
    answered by the rule the evaluated script states: the old script clicks a control whose
    label says search, the new one the form's button or the one control next to the input."""

    def __init__(self, form_button: bool, controls: List[str]):
        self.url, self.form_button, self.controls = f"{SHOP}/", form_button, controls
        self.inbox: List[dict] = []
        self.session = None

    def navigate(self, url):
        self.url = url
        self.session.budget.sent.append(("shop.test", 0.0, url, "Document"))

    def evaluate(self, e: str):
        import re
        results = f"{SHOP}/search?q=red+fox"
        if "__anyuseSettle" in e:
            return {"quiet_ms": 900, "mutations": 1, "ready": "complete"}
        if "querySelectorAll('input').forEach" in e:
            return [{"index": 0, "visible": True, "name": "q", "id": "", "type": "search"}]
        if "el.focus(); el.value = ''" in e:
            return True
        if "String(el.value" in e:
            return "red fox"
        if "__anyuseSubmitSearch" in e:
            if self.form_button:
                self.navigate(results)
                return {"clicked": True, "how": "form submit control"}
            if "search button next to the input" in e:          # the script before version 14
                words = re.compile(r"search|suche|recherche|buscar|zoek|cerca|find|go", re.I)
                if any(words.search(c) for c in self.controls):
                    self.navigate(results)
                    return {"clicked": True, "how": "search button next to the input"}
                return {"clicked": False, "how": "no submit control"}
            if len(self.controls) == 1:
                self.navigate(results)
                return {"clicked": True, "how": "the one control next to the input",
                        "label": self.controls[0]}
            return {"clicked": False, "how": "no submit control"}
        if "__anyuseFormGetUrl" in e:
            return None
        if "location.href" in e:
            return self.url
        return None


class _WS:
    def __init__(self, page):
        self.page = page

    async def send(self, raw):
        msg = json.loads(raw)
        val = None
        if msg.get("method") == "Runtime.evaluate":
            val = self.page.evaluate(msg["params"]["expression"])
        if msg.get("method") in ("Fetch.continueRequest", "Fetch.failRequest"):
            return
        self.page.inbox.append({"id": msg["id"], "result": {"result": {"value": val}}})

    async def recv(self):
        if not self.page.inbox:
            raise asyncio.TimeoutError()
        return json.dumps(self.page.inbox.pop(0))


def case_dead_session(form_button: bool, controls: List[str]) -> Dict[str, Any]:
    from anyuse.execution import cdp_guard as G
    from anyuse.execution import discover as D
    page = FakePage(form_button, controls)
    s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(1.0)))
    page.session = s
    s._ws = _WS(page)

    async def goto(url):
        page.navigate(url)
    s.goto = goto
    out = asyncio.run(s.type_search("red fox"))
    reached = page.url.startswith(f"{SHOP}/search?q=")
    return {"reached": reached, "url": page.url, "submit": out.get("submit"),
            "form_url": out.get("form_url", "")}


BASE = "https://dead.test"
ITEMS = ["Red fox plush", "Red fox mug", "Red fox poster"]
GET_FORM = ('<html><body><header><form action="/find" method="get"><input type="search" name="term">'
            '</form></header><main><h1>Welcome</h1><a href="/about">About us</a></main></body></html>')
SEARCH_ACTION = ('<html><head><script type="application/ld+json">' + json.dumps(
    {"@context": "https://schema.org", "@type": "WebSite", "url": BASE,
     "potentialAction": {"@type": "SearchAction", "target": BASE + "/find?term={search_term_string}",
                         "query-input": "required name=search_term_string"}}) +
    '</script></head><body><div><input type="search" name="q" placeholder="Search"></div>'
    '<main><h1>Welcome</h1></main></body></html>')


def case_dead_dom(start_html: str) -> Dict[str, Any]:
    import html as H
    from anyuse.execution import demo_compile as C
    from anyuse.execution import demo_policy as DPOL
    from anyuse.execution import dom_page as D

    class Dead(D.DomSite):
        def search(self, page, url, inp, value):
            return None

        def route(self, url):
            sp = urllib.parse.urlsplit(url)
            q = dict(urllib.parse.parse_qsl(sp.query))
            if sp.path in ("", "/"):
                return D.Recorded(url, start_html), url
            term = q.get("term") or q.get("q") or ""
            if sp.path == "/find" and term:
                rows = "".join(f'<li class="card"><a href="/p/{i}">{H.escape(t)}</a></li>'
                               for i, t in enumerate(ITEMS) if term.split()[0].lower() in t.lower())
                return D.Recorded(url, f"<html><head><title>Find {H.escape(term)}</title></head><body>"
                                       f"<main><h1>Results</h1><ul class=results>{rows}</ul></main>"
                                       f"</body></html>"), url
            if sp.path.startswith("/p/"):
                return D.Recorded(url, "<html><body><h1>Item</h1></body></html>"), url
            return D.Recorded(url, D.NOT_FOUND, {}, 404), url

    site = Dead([])
    page = D.DomPage(site)
    DPOL.SEARCH_WAIT_S = 0.0

    def read(url):
        rec, u = site.route(url)
        r = dict(site.read(rec, u))
        r.setdefault("search_urls", [])
        return r

    async def go():
        await page.goto(BASE + "/")
        return await DPOL.execute(page, {"action": "search", "element": 0, "text": "red fox"},
                                  {0: {"src": "input", "input": {"index": 0}}}, "dead.test", D.no_settle)
    outcome = asyncio.run(go())
    after = outcome.get("url_after") or ""
    reached = "/find?" in after
    tool = None
    if reached:
        p0, p1 = read(BASE + "/"), read(after)
        links = [e for e in p1["elements"] if e["tag"] == "a" and "/p/" in (e.get("href") or "")]
        res = {"results": [{"title": e["text"], "url": e["href"]} for e in links[:5]]}
        trace = {"version": 1, "workflow": "site_search", "site": "dead.test", "start_url": BASE + "/",
                 "value": "red fox", "stop": "done", "forbidden": [], "result": res, "final_page": p1,
                 "steps": [{"k": 0, "page": p0, "action": {"action": "search", "element": 0, "text": "red fox"},
                            "outcome": outcome},
                           {"k": 1, "page": p1, "action": {"action": "done", "result": res}, "outcome": {}}]}
        got = C.compile_trace(trace)
        tool = (got.get("tool") or {}).get("search") if got.get("ok") else {"why": got.get("why")}
    return {"reached": reached, "how": outcome.get("how"), "url": after, "tool_search": tool}


# --- a search that redirects to a landing page ---------------------------------

LANDING = {"red": "col_8812", "blue": "col_9001", "green": "col_7777", "amber": "col_5555",
           "slate": "col_4242", "coral": "col_3131", "olive": "col_2020", "ivory": "col_1919",
           "teal": "col_1818", "plum": "col_1717"}
FILTER_KEY = "flt-live-424242aa"


def _landing_html(term):
    return (f"<html><head><script>var page={json.dumps({'collectionId': LANDING[term], 'filterKey': FILTER_KEY})};"
            f"</script></head><body><h1>{term}</h1><div id=grid></div></body></html>")


class LandingShop:
    def __init__(self, home=False):
        self.home = home

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.netloc == "shop.test" and sp.path == "/search":
            return _resp(200, _landing_html(q.get("q", "red")) if not self.home else "<html>home</html>",
                         "text/html")
        if sp.netloc == "api.shop.test" and sp.path == "/filter":
            if q.get("key") != FILTER_KEY:
                return _resp(403, "{}")
            term = next((t for t, c in LANDING.items() if c == q.get("collection")), "")
            return _resp(200, json.dumps({"products": [{"title": t} for t in CATALOGUE.get(term, [])]}))
        return _resp(404, "")


def _har(term, home=False):
    def entry(url, status, mime, text, redirect=""):
        return {"request": {"method": "GET", "url": url, "headers": [], "cookies": [], "queryString": []},
                "response": {"status": status, "headers": [{"name": "content-type", "value": mime}],
                             "content": {"size": len(text), "mimeType": mime, "text": text},
                             "redirectURL": redirect}, "_resourceType": "document"}
    search = f"{SHOP}/search?q={term}"
    if home:
        featured = json.dumps({"products": [{"title": t} for t in ["Gift card", "Summer sale", "New in"]]})
        entries = [entry(search, 302, "text/html", "", "/"),
                   entry(f"{SHOP}/", 200, "text/html", "<html><body>home</body></html>"),
                   dict(entry("https://api.shop.test/featured", 200, "application/json", featured),
                        _resourceType="xhr")]
    else:
        landing = f"{SHOP}/collections/{term}"
        data = json.dumps({"products": [{"title": t} for t in CATALOGUE[term]]})
        entries = [entry(search, 302, "text/html", "", f"/collections/{term}"),
                   entry(landing, 200, "text/html", _landing_html(term)),
                   dict(entry(f"https://api.shop.test/filter?collection={LANDING[term]}&key={FILTER_KEY}",
                              200, "application/json", data), _resourceType="xhr")]
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "bench"}, "entries": entries}})


def case_landing(home=False) -> Dict[str, Any]:
    from anyuse.execution.api_compile import compiled_record
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    from anyuse.execution.har_import import har_to_recording, parse_har
    items = (lambda t: ["Gift card", "Summer sale", "New in"]) if home else (lambda t: CATALOGUE[t])
    recs = [har_to_recording(parse_har(_har(t, home)), "q", t, items(t)) for t in RECORDED]
    executor = ApiStepExecutor(transport=LandingShop(home))
    compiled, proof = compile_and_verify(recs[0], recs[2:], executor=executor, fitting_recordings=[recs[1]])
    out = {"status": compiled.status, "reason": (compiled.reason or "")[:300], "proof": bool(proof.passed),
           "proof_reason": (proof.reason or "")[:300],
           "tool": compiled.template is not None and bool(proof.passed)}
    stored: List[Any] = [recs]
    results = []
    if compiled.template is not None and not home:
        stored.append(compiled_record(compiled.template, proof.to_dict()))
        for term in CALLS:
            res = executor.execute(compiled.template, {"q": term})
            got = (res.get("extracted") or {}).get("items")
            results.append({"term": term, "ok": bool(res.get("success")), "exact": got == CATALOGUE[term],
                            "error": str(res.get("error") or "")[:160]})
            stored.append(res)
    out["calls"] = results
    out["keys_stored"] = _count_keys(stored, [FILTER_KEY])
    return out


class KeepShop:
    """``spa.test``: its API answers the search; ``up.test``: its search page lists the results."""

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        term = dict(urllib.parse.parse_qsl(sp.query)).get("q", "")
        if sp.netloc == "spa.test" and sp.path == "/api/search":
            return _resp(200, json.dumps({"results": [{"title": t} for t in CATALOGUE.get(term, [])]}))
        if sp.netloc == "up.test" and sp.path == "/search":
            return _resp(200, _rendered(term), "text/html")
        return _resp(404, "")


def _rendered(term):
    rows = "".join(f'<li class="card"><a href="/p/{i}">{t}</a></li>' for i, t in enumerate(CATALOGUE.get(term, [])))
    return f"<html><body><h1>Results</h1><ul class=results>{rows}</ul></body></html>"


def _keep_har(case, term):
    def entry(url, status, mime, text, redirect="", rtype="document"):
        return {"request": {"method": "GET", "url": url, "headers": [], "cookies": [], "queryString": []},
                "response": {"status": status, "headers": [{"name": "content-type", "value": mime}],
                             "content": {"size": len(text), "mimeType": mime, "text": text},
                             "redirectURL": redirect}, "_resourceType": rtype}
    if case == "spa_home":
        data = json.dumps({"results": [{"title": t} for t in CATALOGUE[term]]})
        entries = [entry(f"https://spa.test/search?q={term}", 302, "text/html", "", f"/#/search?q={term}"),
                   entry("https://spa.test/", 200, "text/html", "<html><body><div id=app></div></body></html>"),
                   entry(f"https://spa.test/api/search?q={term}", 200, "application/json", data, rtype="xhr")]
    else:
        entries = [entry(f"http://up.test/search?q={term}", 301, "text/html", "",
                         f"https://up.test/search?q={term}"),
                   entry(f"https://up.test/search?q={term}", 200, "text/html", _rendered(term))]
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "bench"}, "entries": entries}})


def case_keep(case: str) -> Dict[str, Any]:
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    from anyuse.execution.har_import import har_to_recording, parse_har
    recs = [har_to_recording(parse_har(_keep_har(case, t)), "q", t, CATALOGUE[t]) for t in RECORDED]
    executor = ApiStepExecutor(transport=KeepShop())
    compiled, proof = compile_and_verify(recs[0], recs[2:], executor=executor, fitting_recordings=[recs[1]])
    results = []
    if compiled.template is not None:
        for term in CALLS:
            res = executor.execute(compiled.template, {"q": term})
            got = (res.get("extracted") or {}).get("items")
            results.append({"term": term, "ok": bool(res.get("success")), "exact": got == CATALOGUE[term]})
    return {"status": compiled.status, "reason": (compiled.reason or "")[:300], "proof": bool(proof.passed),
            "tool": compiled.template is not None and bool(proof.passed), "calls": results}


def run_arm(label: str, out_file: str) -> None:
    runners = {
        "provider_get": lambda: case_provider(False),
        "provider_post": lambda: case_provider(True),
        "dead_form_button": lambda: case_dead_session(True, ["→"]),
        "dead_lone_control": lambda: case_dead_session(False, ["→"]),
        "dead_get_form": lambda: case_dead_dom(GET_FORM),
        "dead_search_action": lambda: case_dead_dom(SEARCH_ACTION),
        "landing": lambda: case_landing(False),
        "home": lambda: case_landing(True),
        "spa_home": lambda: case_keep("spa_home"),
        "http_upgrade": lambda: case_keep("http_upgrade"),
    }
    cases: Dict[str, Any] = {}
    for name in CASES:
        try:
            cases[name] = runners[name]()
        except Exception as exc:                       # noqa: BLE001 - recorded per case
            cases[name] = {"error": f"{type(exc).__name__}: {exc}"[:300],
                           "trace": traceback.format_exc()[-1200:]}
    with open(out_file, "w") as fh:
        json.dump({"arm": label, "cases": cases}, fh, indent=1, default=str)


# ---------------------------------------------------------------------------
# compare and run
# ---------------------------------------------------------------------------

def compare(files: List[str]) -> str:
    arms = {a["arm"]: a["cases"] for a in (json.load(open(f)) for f in files)}
    old, new = arms.get("OLD") or {}, arms.get("NEW") or {}
    lines = [f"{'arm':4} {'case':19} {'tool':>4} {'proof':>5} {'calls':>5} {'exact':>5} {'keys':>4} "
             f"{'reached':>7}  status / how"]
    for label, cases in (("OLD", old), ("NEW", new)):
        for name in CASES:
            c = cases.get(name) or {}
            calls = c.get("calls") or []
            how = c.get("error") or c.get("status") or c.get("how") or ""
            if name == "home":
                how = (c.get("reason") or how)[:70]
            lines.append(f"{label:4} {name:19} {str(bool(c.get('tool')))[0]:>4} "
                         f"{str(bool(c.get('proof')))[0]:>5} {len(calls):>5} "
                         f"{sum(1 for x in calls if x.get('exact')):>5} "
                         f"{c.get('keys_stored', '-')!s:>4} {str(c.get('reached', '-'))[:7]:>7}  {how}")
    lines.append("")
    lines.append("columns: tool = a compiled tool whose proof passed; calls / exact = calls with inputs "
                 "nobody recorded, answered with exactly the site's list; keys = provider key values found "
                 "in the stored recordings, records, proofs, traces and results; reached = the typed search "
                 "reached the site's results")
    lines.append("")

    def verdict(ok: bool, text: str) -> None:
        lines.append(f"{'PASS' if ok else 'FAIL'} {text}")
    for name in HTTP_CASES:
        o, n = old.get(name) or {}, new.get(name) or {}
        exact = sum(1 for x in n.get("calls") or [] if x.get("exact"))
        verdict(not o.get("tool") and bool(n.get("tool")) and exact == len(CALLS),
                f"{name}: OLD tool={bool(o.get('tool'))}, NEW tool={bool(n.get('tool'))} "
                f"({n.get('status')}), NEW {exact}/{len(CALLS)} exact")
    keys = sum(int((new.get(x) or {}).get("keys_stored") or 0) for x in HTTP_CASES)
    old_keys = sum(int((old.get(x) or {}).get("keys_stored") or 0) for x in HTTP_CASES)
    verdict(keys == 0, f"0 provider keys stored in NEW (got {keys}; OLD {old_keys})")
    for name in DEAD_CASES:
        o, n = old.get(name) or {}, new.get(name) or {}
        want_old = name == "dead_form_button"
        verdict(bool(n.get("reached")) and bool(o.get("reached")) == want_old,
                f"{name}: OLD reached={bool(o.get('reached'))}, NEW reached={bool(n.get('reached'))}"
                + (f" ({n.get('how')})" if n.get("how") else ""))
    for name in ("dead_get_form", "dead_search_action"):
        n = new.get(name) or {}
        ts = n.get("tool_search") or {}
        verdict(ts.get("mode") == "url" and "{q}" in str(ts.get("template") or ""),
                f"{name}: NEW compiles a browser tool on the search URL ({ts.get('template') or ts.get('why')})")
    n = new.get("home") or {}
    verdict(not n.get("tool") and str(n.get("reason") or "").startswith("search_to_home"),
            f"home: NEW refused as search_to_home ({str(n.get('reason'))[:60]}); OLD tool="
            f"{bool((old.get('home') or {}).get('tool'))}")
    for name in KEEP_CASES:
        o, n = old.get(name) or {}, new.get(name) or {}
        exact = sum(1 for x in n.get("calls") or [] if x.get("exact"))
        verdict(bool(o.get("tool")) and bool(n.get("tool")) and exact == len(CALLS),
                f"{name}: no regression, OLD tool={bool(o.get('tool'))}, NEW tool={bool(n.get('tool'))} "
                f"({n.get('status') or n.get('reason')}), NEW {exact}/{len(CALLS)} exact")
    return "\n".join(lines)


def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-provider-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "provider_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    results = []
    for label, tree in trees.items():
        home = os.path.join(out, f"home_{label}")
        os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_") and k not in MODEL_ENV}
        env.update({"PYTHONPATH": pkgroot, "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
                    "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                    "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                    "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
                    "ANYUSE_API_HOSTS_FILE": os.path.join(home, "api_hosts.json"),
                    "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, "blocked.json"),
                    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
                    "ANYUSE_MODEL_CHECKS": "none", "ANYUSE_ATTRIBUTE_VERIFY": "off"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, "-B", me, "arm", "--label", label,
                               "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0 if "FAIL" not in text else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
