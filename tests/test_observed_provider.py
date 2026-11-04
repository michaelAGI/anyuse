"""ANYUSE_THIRD_PARTY=search (the default): the third-party host the site's own page sends the typed value to is the
site's search provider for the run (``web_scope.WebScope.observed_call``); the catalogue is a hint. Pinned here:
* a data call (XHR, fetch, JSONP script, event stream) that carries the typed value as a parameter value, a body
  field or a path segment makes its host the provider; after that, GET/HEAD under the same path area, the CORS
  preflight and a read-only search POST right after typing are the provider's (kind ``provider``, its own share of 10
  requests per page load, robots.txt of the host read once);
* nothing is granted for a request without the value, a value inside another URL, an image or a font, a document, a
  method beyond GET/HEAD/POST, a host on the ad/tracker blocklist, a telemetry or state-changing path, mode ``off``,
  or a scope built from explicit flags (the benches' protocols);
* a CORS preflight right after typing never clears a telemetry, infrastructure or state-changing path;
* under ``any``, what the provider rule refuses is still judged by the third-party policy;
* a value-carrying call to a catalogued provider outside the catalogue's paths is the provider's too;
* the typed stop ``provider_search`` names the scope's reason.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh tests/test_observed_provider.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import types

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402

H = "www.toolnut.test"
TINY_BLOCK = frozenset({"google-analytics.com", "doubleclick.net", "stats-y.test"})
API = "https://api.findit.test"
SPRING = '<html><script src="https://snapui.searchspring.io/cts0zk/bundle.js" id="searchspring-context"></script></html>'
JSON = {"content-type": "application/json"}


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    for k in ("ANYUSE_THIRD_PARTY", "ANYUSE_THIRD_PARTY_POLICY", "ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS",
              "ANYUSE_ALLOW_READONLY_POST_SEARCH", "ANYUSE_ALLOW_SITE_OWN_HOSTS", "ANYUSE_THIRD_PARTY_PAGE_CODE",
              "ANYUSE_REFUSE_GOV_HOSTS"):
        monkeypatch.delenv(k, raising=False)


def scope_for(mode="search"):
    sc = W.WebScope.from_settings(mode=mode)
    if sc is not None:
        sc._blocklist = TINY_BLOCK
    return sc


def guard(sc, typed="drill", acting=True, allow_url=None, **kw):
    pacer = D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None)
    kw.setdefault("search_post", True)
    b = G.RequestBudget(pacer, web_scope=sc, **kw)
    g = G.LoadGuard(b, allow_url=allow_url)
    g.start(f"https://{H}/")
    if typed:
        g.typed.append(typed)
        if sc is not None:
            sc.note_values(H, [typed])
    g.acting = 1 if acting else 0
    return b, g


def test_the_host_the_sites_page_sends_the_typed_value_to_is_its_provider():
    sc = scope_for()
    b, g = guard(sc)
    for url, rtype in ((f"{API}/v2/query?q=drill&lang=de", "XHR"),
                       (f"{API}/v2/facets?lang=de", "XHR"),                # the same area, no value: a follow-up
                       (f"{API}/v2/query?q=drill&cb=f", "Script"),         # JSONP
                       (f"{API}/v2/suggest/drill", "Fetch")):              # the value as a path segment
        assert g.decide(url, rtype) == "", (url, rtype)
        assert g.last_kind == W.KIND_PROVIDER
    assert g.decide(f"{API}/admin/x", "XHR").startswith("third-party host")              # outside the area
    assert g.decide(f"{API}/v2/query?q=drill", "Document").startswith("third-party host")  # never a page
    assert g.decide(f"{API}/v2/query?q=drill", "XHR", method="DELETE").startswith(("method", "third-party"))
    rep = sc.site_report(H)["observed"]["api.findit.test"]
    assert rep["paths"] == ["/v2/", "/v2/suggest/"] and rep["type"] == "XHR" and "q=drill" in rep["evidence"]
    assert sc.observed_for(H, "api.findit.test", "/v2/facets") is not None
    assert g._granted_provider("api.findit.test")                        # its CORS preflight is the provider's too
    assert HT.provider_call(f"{API}/v2/query?q=x", sc, H) and not HT.provider_call(f"{API}/admin/x", sc, H)
    assert sc.knobs()["observed_providers"] is True


def test_no_value_a_nested_url_or_an_image_grants_nothing():
    sc = scope_for()
    b, g = guard(sc)
    for url, rtype in (("https://px.stats-x.test/c?dl=https%3A%2F%2Fwww.toolnut.test%2Fsearch%3Fq%3Ddrill", "XHR"),
                       ("https://i.img.test/drill.jpg?q=drill", "Image"),
                       ("https://f.fonts.test/drill.woff2?q=drill", "Font"),
                       ("https://api.fresh.test/v1/list?page=2", "XHR")):
        assert g.decide(url, rtype).startswith("third-party host"), (url, rtype)
    assert sc.site_report(H)["observed"] == {}
    # a one-letter value matches too much: never evidence
    sc2 = scope_for()
    _b, g2 = guard(sc2, typed="x")
    assert g2.decide(f"{API}/v2/query?q=x", "XHR").startswith("third-party host")
    assert W.carried_value(f"{API}/v2/query?q=drill", values=["drill"]) == "drill"
    assert W.carried_value(f"{API}/v2/query?q=drill%2A", values=["Drill"]) == "drill"        # "drill*"
    assert W.carried_value(f"{API}/x?u=https%3A%2F%2Fa.test%2F%3Fq%3Ddrill", values=["drill"]) == ""
    assert W.carried_value(f"{API}/x", json.dumps({"params": "query=drill&hitsPerPage=20"}), ["drill"],
                           "application/json") == "drill"


def test_blocklist_telemetry_state_paths_and_mode_off_stay_refused():
    sc = scope_for()
    b, g = guard(sc)
    assert "blocklist" in g.decide("https://region1.stats-y.test/g/q?q=drill", "XHR")
    assert "telemetry" in g.decide(f"{API}/collect?q=drill", "XHR")
    assert "state change" in g.decide(f"{API}/wishlist/add?q=drill", "XHR")
    assert sc.site_report(H)["observed"] == {}
    off = scope_for("off")
    _b, g_off = guard(off)
    assert g_off.decide(f"{API}/v2/query?q=drill", "XHR").startswith("third-party host")
    # the benches' explicit protocols and the historical settings never observe
    bench = W.WebScope(providers=True)
    assert not bench.observed_providers
    assert not bench.classify(H, f"{API}/v2/query?q=drill", "GET", "XHR", values=("drill",)).ok
    legacy = W.WebScope.from_legacy_settings()
    assert legacy is None or not legacy.observed_providers


def test_a_value_carrying_call_outside_a_catalogued_providers_paths_is_the_providers():
    sc = scope_for()
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    b, g = guard(sc)
    host = "https://cts0zk.a.searchspring.io"
    assert g.decide(f"{host}/api/personalization/recs", "XHR").startswith("third-party host")   # no value: the list
    assert g.decide(f"{host}/api/v2/autocomplete?q=drill", "XHR") == "" and g.last_kind == W.KIND_PROVIDER
    assert "cts0zk.a.searchspring.io" in sc.site_report(H)["observed"]


def test_a_read_only_search_post_to_an_observed_host_after_typing():
    sc = scope_for()
    b, g = guard(sc)
    url = f"{API}/v1/search"
    assert g.decide(url, "Preflight", "OPTIONS", headers={"Access-Control-Request-Method": "POST"}) == ""
    assert g.decide(url, "XHR", "POST", json.dumps({"query": "drill", "page": 1}), JSON) == ""
    assert g.last_kind == W.KIND_PROVIDER and "api.findit.test" in sc.site_report(H)["observed"]
    assert "provider" in g.last_search_post                               # logged as a search POST when sent
    # a multi-query body: the search fields are a query string inside a body field
    multi = "https://q1.searchcloud.test/1/indexes/*/queries"
    assert g.decide(multi, "XHR", "POST", json.dumps({"requests": [{"indexName": "p",
                                                                     "params": "query=drill&hitsPerPage=20"}]}),
                    JSON) == ""
    for body, why in (({"query": "saw"}, "does not carry the typed value"),
                      ({"query": "drill", "addToCart": 1}, "state change"),
                      ({"requests": [{"params": "query=drill&addToCart=1"}]}, "state change")):
        assert g.decide(url, "XHR", "POST", json.dumps(body), JSON).startswith(("method", "third-party")), body
        assert why in b.search_posts_refused[-1]["why"], b.search_posts_refused[-1]
    sc2 = scope_for()
    b2, g2 = guard(sc2, acting=False)                                     # not right after typing
    assert g2.decide(f"{API}/v1/search", "XHR", "POST", json.dumps({"query": "drill"}), JSON).startswith(
        ("method", "third-party"))
    assert "right after typing" in b2.search_posts_refused[-1]["why"]
    assert g2.decide(f"{API}/v1/search", "Preflight", "OPTIONS",
                     headers={"Access-Control-Request-Method": "POST"}).startswith("third-party host")
    assert g2.decide(f"{API}/v1/search", "Preflight", "OPTIONS",
                     headers={"Access-Control-Request-Method": "PUT"}).startswith(("method", "third-party"))


def test_any_mode_judges_what_the_provider_rule_refuses_and_no_preflight_clears_a_refused_path():
    # ANYUSE_THIRD_PARTY=any: a content API the page sends the value to loads, whatever its path is named
    _b, g = guard(scope_for("any"))
    assert g.decide(f"{API}/events/search?q=drill", "XHR") == "" and g.last_kind == W.KIND_THIRD
    assert "blocklist" in g.decide("https://region1.stats-y.test/g/q?q=drill", "XHR")
    # right after typing, a CORS preflight never clears a path whose request would be refused
    _b, g = guard(scope_for())
    for path in ("/collect", "/v1/events", "/cart/add"):
        why = g.decide(f"https://t.unlisted.test{path}", "Preflight", "OPTIONS",
                       headers={"Access-Control-Request-Method": "POST"})
        assert why.startswith("third-party host t.unlisted.test:"), (path, why)
    assert g.decide("https://s.unlisted.test/v1/search", "Preflight", "OPTIONS",
                    headers={"Access-Control-Request-Method": "POST"}) == ""


def paused(i, url, rtype, method="GET"):
    return {"requestId": f"r{i}", "networkId": f"n{i}", "frameId": "f1", "resourceType": rtype,
            "request": {"url": url, "method": method, "headers": {}}}


def test_robots_of_the_observed_host_is_read_once_and_the_share_holds(monkeypatch):
    from anyuse.execution import api_permission as AP
    fetched = []

    def allows(url, fetcher=None, on_hop=None):
        fetched.append(url)
        return ("/v2/private" not in url, "test")
    monkeypatch.setattr(AP, "robots_allows", allows)
    monkeypatch.setattr(AP, "robots_allows_cached", lambda url: (None, "unknown"))
    monkeypatch.setattr(AP, "robots_known_absent", lambda url: False)
    sc = scope_for()
    gate = HP.robots_gate(scope=sc)
    b, g = guard(sc, allow_url=gate)
    assert g.decide(f"{API}/v2/query?q=drill", "XHR") == "" and len(fetched) == 1
    assert g.decide(f"{API}/v2/private/x?q=drill", "XHR").startswith("robots_refused")
    assert len(fetched) == 1                                              # once per host and run
    # the provider's own share of 10 per page load
    pacer = D.Pacer(0.0, clock=lambda: 1000.0, sleeper=lambda s: None)
    sc2 = scope_for()
    b2 = G.RequestBudget(pacer, per_load_cap=30, site_cap=60, web_scope=sc2, load_per_navigation=True)
    g2 = G.LoadGuard(b2)
    g2.start(f"https://{H}/")
    g2.typed.append("drill")
    sent = []

    async def send(m, p):
        sent.append((m, p.get("requestId")))

    async def go(reqs):
        for r in reqs:
            await g2.handle(r, send)
    asyncio.run(go([paused(0, f"https://{H}/search?q=drill", "Document")] +
                   [paused(10 + i, f"{API}/v2/query?q=drill&p={i}", "XHR") for i in range(11)]))
    cont = [rid for m, rid in sent if m == "Fetch.continueRequest"]
    assert cont == ["r0"] + [f"r{10 + i}" for i in range(10)], cont
    assert any("provider share of 10" in w for _u, w in g2.failed)
    assert b2.provider_sent[H] == 10


def test_provider_search_stop_names_the_scopes_reason():
    url = "https://x.test/api/search?q=drill"
    page = types.SimpleNamespace(guard=types.SimpleNamespace(
        failed=[(url, "third-party host x.test (on the ad/tracker blocklist: x.test)")], failed_types={url: "XHR"}))
    st, why = HT.search_refusal(page, {"failed": 0}, "drill", f"https://{H}/")
    assert st == "provider_search" and "blocklist" in why and "does not grant" in why
    page.guard.failed = [(url, "third-party host x.test")]
    st, why = HT.search_refusal(page, {"failed": 0}, "drill", f"https://{H}/")
    assert st == "provider_search" and "does not grant" in why and "sends the typed value to" in why


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
