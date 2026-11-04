"""A READ-ONLY POST SEARCH is allowed, search only (www.digitalkamera.de's results come from ``POST /api/Search``,
which the read-only browser refused). Pinned in the one guard
(``cdp_guard.LoadGuard.search_post``, ``RequestBudget(search_post=True)`` from the product's page factory and the
replay route):
* allowed: an XHR / fetch / document POST the site's own page sends right after typing into its search box (the
  session's typed value, the action window), to the site's own host, a granted search provider, or (under
  ANYUSE_THIRD_PARTY=search / any) the third-party host the page sends the typed value to, whose small form or JSON
  body carries the typed value - counted as DATA and logged on the run record (method, path, why allowed);
* refused (as every other POST): a body without the value, no typed value, outside the typing window, another host
  without a scope (or one on the ad/tracker blocklist),
  an account / login / cart / checkout / payment path, a state-changing path or body field, a telemetry path, a
  credential header, an upload; a budget without the knob refuses every POST as before.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_search_post.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as Dsc  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402

H = "www.kamera.test"
JSON = {"content-type": "application/json"}


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    for k in ("ANYUSE_THIRD_PARTY", "ANYUSE_ALLOW_READONLY_POST_SEARCH", "ANYUSE_ALLOW_SITE_OWN_HOSTS"):
        monkeypatch.delenv(k, raising=False)


def guard(scope=None, typed="Canon", acting=True, **kw):
    clock = [1000.0]
    pacer = Dsc.Pacer(0.0, clock=lambda: clock[0], sleeper=lambda s: None)
    kw.setdefault("search_post", True)
    b = G.RequestBudget(pacer, site_cap=20, web_scope=scope, load_per_navigation=True, browser_statics=True, **kw)
    g = G.LoadGuard(b)
    g.start(f"https://{H}/Suche?Q=Canon")
    if typed:
        g.typed.append(typed.lower())
    g.acting = 1 if acting else 0
    return b, g


def post(g, path, body, host=H, rtype="XHR", headers=None):
    return g.decide(f"https://{host}{path}", rtype, "POST", body if isinstance(body, str) else json.dumps(body),
                    headers if headers is not None else JSON)


def test_the_pages_own_search_post_is_allowed_counted_and_logged():
    b, g = guard()
    assert post(g, "/api/Search", {"query": "Canon", "page": 1}) == ""
    assert post(g, "/suche", "q=canon&sort=relevance", headers={"content-type": "application/x-www-form-urlencoded"},
                rtype="Document") == ""
    sent = []

    async def send(m, p):
        sent.append((m, p.get("requestId")))
    asyncio.run(g.handle({"requestId": "r1", "networkId": "n1", "frameId": "f1", "resourceType": "XHR",
                          "request": {"url": f"https://{H}/api/Search", "method": "POST",
                                      "postData": json.dumps({"query": "Canon"}), "headers": JSON}}, send))
    assert sent == [("Fetch.continueRequest", "r1")] and b.site_total(H) == 1           # counted as data
    assert b.search_posts == [{"method": "POST", "host": H, "path": "/api/Search", "type": "XHR",
                               "why": "a search POST right after typing, its body carries the typed value"}]
    assert b.traffic(H)["search_posts"][0]["path"] == "/api/Search"


@pytest.mark.parametrize("path,body,headers,why", [
    ("/api/Search", {"query": "Nikon"}, None, "does not carry the typed value"),
    ("/api/Search", "", None, "not a small search body"),
    ("/cart/add.js", {"q": "Canon"}, None, "account / login / cart"),
    ("/checkout/search", {"q": "Canon"}, None, "account / login / cart"),
    ("/payment/lookup", {"q": "Canon"}, None, "payment"),
    ("/newsletter/subscribe", {"email": "Canon"}, None, "state change"),
    ("/api/Search", {"query": "Canon", "addToCart": 1}, None, "body field names a state change"),
    ("/.well-known/shopify/monorail/unstable/produce_batch", {"q": "Canon"}, None, "telemetry"),
    # a store platform's analytics event echoed the typed value (it was sent; now refused)
    ("/.well-known/shopify/fec/produce", {"events": [{"query": "Canon"}]}, None, "telemetry or infrastructure"),
    ("/api/events", {"query": "Canon"}, None, "telemetry or infrastructure"),
    ("/api/v2/widgets", {"label": "Canon EOS"}, None, "named like a search"),
    ("/api/Search", {"query": "Canon"}, {"content-type": "application/json", "authorization": "Bearer x"}, "credential"),
    ("/api/Search", "--x\r\ncanon", {"content-type": "multipart/form-data; boundary=x"}, "multipart"),
])
def test_every_other_post_is_refused(path, body, headers, why):
    b, g = guard()
    got = post(g, path, body, headers=headers)
    assert got.startswith(("method POST", "account/login", "third-party")), got
    assert why in b.search_posts_refused[-1]["why"], b.search_posts_refused[-1]


def test_a_field_that_carries_exactly_the_typed_value_is_the_search_in_any_language():
    """Right after typing, a body field whose whole value is the typed value is the page's search, whatever the path
    and the field are called (Dutch, Polish, Turkish, Thai field names); the held-out proof decides whether its answer
    is a listing that changes with the input. The value inside a longer field still needs a search name."""
    for path, body in (("/api/zoeken", {"trefwoord": "Canon"}), ("/api/wyszukaj", {"fraza": "canon", "strona": 1}),
                       ("/arama/sonuc", "kelime=Canon&sayfa=1"), ("/api/v2/widgets", {"label": "Canon"}),
                       ("/ค้นหา", {"คำ": "Canon"}), ("/x", {"params": "text=Canon&hitsPerPage=20"})):
        b, g = guard()
        hdrs = JSON if isinstance(body, dict) else {"content-type": "application/x-www-form-urlencoded"}
        assert post(g, path, body, headers=hdrs) == "", (path, body, b.search_posts_refused)
    # the names rung still reads a value inside a longer field
    b, g = guard()
    assert post(g, "/api/Search", {"query": "Canon EOS"}) == ""
    # every refusal before it still holds: state words, telemetry, credentials, outside the typing window
    b, g = guard()
    assert post(g, "/api/zoeken", {"trefwoord": "Canon", "addToCart": 1}).startswith("method POST")
    b, g = guard(acting=False)
    assert post(g, "/api/zoeken", {"trefwoord": "Canon"}).startswith("method POST")


def test_no_typed_value_outside_the_window_another_host_or_no_knob_refuses():
    b, g = guard(typed="")
    assert post(g, "/api/Search", {"query": "Canon"}).startswith("method POST")
    assert "no search value was typed" in b.search_posts_refused[-1]["why"]
    b, g = guard(acting=False)
    assert post(g, "/api/Search", {"query": "Canon"}).startswith("method POST")
    assert "right after typing" in b.search_posts_refused[-1]["why"]
    b, g = guard()
    assert post(g, "/api/Search", {"query": "Canon"}, host="api.other.test").startswith(("method POST", "third-party"))
    assert "third-party host api.other.test" in b.search_posts_refused[-1]["why"]
    b, g = guard(search_post=False)
    assert post(g, "/api/Search", {"query": "Canon"}).startswith("method POST") and not b.search_posts_refused


def test_a_granted_providers_search_post_is_allowed_and_so_is_the_host_the_page_sends_the_value_to():
    sc = W.WebScope.from_settings(mode="search")
    sc._blocklist = frozenset({"tracker-x.test"})
    sc.observe(H, f"https://{H}/", "Document", '<script src="https://snapui.searchspring.io/cts0zk/bundle.js"></script>')
    b, g = guard(scope=sc)
    assert post(g, "/api/search/search.json", {"q": "canon"}, host="cts0zk.a.searchspring.io") == ""
    assert g.last_kind == "provider" and b.site_of("cts0zk.a.searchspring.io") == H
    # an uncatalogued host the page sends the typed value to right after typing: the site's own search provider
    assert post(g, "/api/Search", {"query": "Canon"}, host="www.notgranted.test") == ""
    assert g.last_kind == "provider" and "www.notgranted.test" in sc.site_report(H)["observed"]
    # an ad / tracker host never, the value or not
    assert post(g, "/api/Search", {"query": "Canon"}, host="api.tracker-x.test").startswith("third-party host")
    assert "blocklist" in b.search_posts_refused[-1]["why"]


def test_the_product_paths_turn_it_on():
    src = (_HERE / "execution" / "harness_product.py").read_text(encoding="utf-8")
    assert "search_post=True" in src
    assert "search_post=True" in (_HERE / "execution" / "watch_live.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_results_from_a_search_post_compile_to_a_typed_search():
    """www.digitalkamera.de: the agent's typed search loaded its results by a search POST; the tool compiled
    to the results URL, and a replay that loads that URL without typing gets no POST (refused by the rule). A search
    whose step recorded a search POST is replayed by typing."""
    import html as _html
    import types
    import urllib.parse as up
    from anyuse.execution import agent_harness as AH
    from anyuse.execution import dom_page as DP
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import harness_policies as P
    from anyuse.execution import harness_tools as HT
    from anyuse.execution import harness_types as T
    base = "https://www.kamera.test"
    items = [("p/canon-eos-r5", "Canon EOS R5 Gehäuse"), ("p/canon-eos-r6", "Canon EOS R6 Mark II"),
             ("p/canon-rf-50", "Canon RF 50mm F1.8 STM"), ("p/nikon-z6", "Nikon Z6 III")]

    class Shop(DP.DomSite):
        def __init__(self):
            super().__init__([])

        def page(self, q):
            rows = "".join(f'<li class="product"><h3><a href="/{s}">{_html.escape(t)}</a></h3><span>99 €</span></li>'
                           for s, t in items if q and q.lower() in t.lower())
            return (f'<html><body><header><form action="/Suche" method="get"><input type="search" name="Q" '
                    f'value="{_html.escape(q)}"></form></header><main><h1>Suche</h1><ul class="r">{rows}</ul>'
                    f'</main></body></html>')

        def route(self, url):
            sp = up.urlsplit(url)
            q = dict(up.parse_qsl(sp.query)).get("Q", "")
            if sp.path == "/Suche" and q:
                page.budget.search_posts.append({"method": "POST", "host": "www.kamera.test", "path": "/api/Search"})
            return DP.Recorded(url, self.page(q if sp.path == "/Suche" else "")), url
    page = DP.DomPage(Shop())
    page.budget = types.SimpleNamespace(search_posts=[], runaway={})
    tools = HT.BrowserTools(page, base + "/", "www.kamera.test")

    def rows(t, o, h):
        return T.Done({"results": [{"element": e.index} for e in o.elements if e.href and "/p/" in e.href]})
    tr = AH.Harness(HT.Toolbox(browser=tools), P.ScriptedPolicy([T.Search(0, "Canon"), rows]), AH.HarnessConfig(),
                    run_id="post").run("search for {q}", {"q": "Canon"})
    assert tr.stop == T.Stop.DONE and tr.steps[0].outcome.data.get("search_post")
    c = HC.compile_web(tr, "site_search")
    assert c["ok"], c.get("why")
    assert c["tool"]["search"]["mode"] == "typed", c["tool"]["search"]
