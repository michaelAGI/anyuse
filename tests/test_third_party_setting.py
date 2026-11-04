"""ANYUSE_THIRD_PARTY: ONE setting for third-party hosts, applied in ONE place
(``web_scope.third_party_flags`` / ``WebScope.from_settings``) that every guarded path builds its scope from - the
agent's page factory and robots gate (``harness_product``), the compile's proof replays (the run's budget), the MCP
route / fallback (the replay's site context). Pinned, per mode:
* off: first-party only - no provider, no third-party host;
* search (DEFAULT): the site's own search provider - the host the site's page sends the typed value to
  (tests/test_observed_provider.py), and a catalogued provider once the site's own page loads it, its search endpoint
  and widget script only; any other third-party host refused (``provider_search`` stays the typed stop);
* any: the hosts the page uses for its content (GET/HEAD), never a document of another site, never an ad/tracker host;
* every mode: NEVER_CONTACT hosts (and government hosts with ANYUSE_REFUSE_GOV_HOSTS) refused before any request;
  robots.txt of every allowed host
  fetched once and obeyed; every third-party host on the run record; the superseded settings are not read;
* under search, a store platform's own asset CDN that the site's page names is FIRST
  PARTY for static assets only (logged as platform_cdn), an unknown CDN stays third party; a catalogued provider has
  its own share of 10 requests per page load, counted in the run total, outside the site's per-load cap and static share.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_third_party_setting.py
"""
import os
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
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402

H = "www.toolnut.test"
SPRING = '<html><script src="https://snapui.searchspring.io/cts0zk/bundle.js" id="searchspring-context"></script></html>'
TINY_BLOCK = frozenset({"google-analytics.com", "doubleclick.net"})


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    for k in ("ANYUSE_THIRD_PARTY", "ANYUSE_THIRD_PARTY_POLICY", "ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS",
              "ANYUSE_ALLOW_READONLY_POST_SEARCH", "ANYUSE_ALLOW_SITE_OWN_HOSTS", "ANYUSE_THIRD_PARTY_PAGE_CODE",
              "ANYUSE_REFUSE_GOV_HOSTS"):
        monkeypatch.delenv(k, raising=False)


def scope_for(mode):
    sc = W.WebScope.from_settings(mode=mode)
    if sc is not None:
        sc._blocklist = TINY_BLOCK
    return sc


def guard(sc, **kw):
    clock = lambda: 1000.0  # noqa: E731
    pacer = D.Pacer(1.0, clock=clock, sleeper=lambda s: None)
    b = G.RequestBudget(pacer, web_scope=sc, **kw)
    g = G.LoadGuard(b)
    g.start(f"https://{H}/")
    return g


# ------------------------------------------------------------------------------------------------- the setting
def test_the_setting_defaults_to_search_and_supersedes_the_old_two(monkeypatch):
    from anyuse import settings as S
    spec = S.SETTINGS["ANYUSE_THIRD_PARTY"]
    assert spec.default == "search" and spec.security_relevant
    assert spec.needs_confirmation("any") and not spec.needs_confirmation("off") and not spec.needs_confirmation("search")
    with pytest.raises(S.SettingError):
        spec.parse("some", strict=True)
    assert "WARNING" in S.security_warning("ANYUSE_THIRD_PARTY", "any")
    assert W.third_party_mode() == "search"
    with pytest.raises(ValueError):
        W.third_party_mode("everything")
    # the superseded settings are not read: allow_all / providers off change nothing
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_POLICY", "allow_all")
    monkeypatch.setenv("ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS", "off")
    sc = W.WebScope.from_settings()
    assert sc.mode == "search" and sc.providers and sc.third_party == "block"
    monkeypatch.setenv("ANYUSE_THIRD_PARTY", "off")
    # the site's own hosts (same registrable domain) are first party by default - "off" is
    # no third-party host, the site's own subdomains still load; with them off too, the exact-host guard (None)
    sc = W.WebScope.from_settings()
    assert sc.own_hosts and not sc.providers and sc.third_party == "block" and not sc.platform_cdn
    monkeypatch.setenv("ANYUSE_ALLOW_SITE_OWN_HOSTS", "off")
    assert W.WebScope.from_settings() is None                 # first-party exact host only: today's guard
    monkeypatch.delenv("ANYUSE_ALLOW_SITE_OWN_HOSTS")
    monkeypatch.setenv("ANYUSE_THIRD_PARTY", "any")
    sc = W.WebScope.from_settings()
    assert sc.providers and sc.third_party == "allow_with_blocklist" and sc.mode == "any"


# ------------------------------------------------------------------------------------------------- per mode
def test_off_is_first_party_only():
    g = guard(scope_for("off"))
    assert g.decide(f"https://{H}/search?q=drill", "Document") == ""
    assert g.decide("https://snapui.searchspring.io/cts0zk/bundle.js", "Script").startswith("third-party host")
    assert g.decide("https://api.widget.test/data.json", "XHR").startswith("third-party host")


def test_search_allows_the_sites_own_provider_after_its_page_names_it(monkeypatch):
    # this test is another rule's: the page-code rule (on by default) is pinned off here
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    sc = scope_for("search")
    g = guard(sc)
    bundle = "https://snapui.searchspring.io/cts0zk/bundle.js"
    api = "https://cts0zk.a.searchspring.io/api/search/search.json?siteId=cts0zk&q=drill"
    assert g.decide(bundle, "Script").startswith("third-party host")      # not before the site's page names it
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    assert g.decide(bundle, "Script") == ""
    assert g.decide(api, "XHR") == ""
    assert g.decide("https://cts0zk.a.searchspring.io/api/personalization/recs", "XHR").startswith("third-party")
    assert g.decide("https://beacon.searchspring.io/beacon", "XHR").startswith("third-party host")   # analytics
    assert g.decide(api, "XHR", method="DELETE").startswith(("method", "third-party"))
    assert g.decide(api, "Document").startswith("third-party host")                   # never a page navigation
    assert g.decide("https://api.widget.test/data.json", "XHR").startswith("third-party host")  # not a provider
    rep = sc.site_report(H)
    assert rep["mode"] == "search" and "searchspring" in rep["grants"]
    assert any("beacon.searchspring.io" in r[0] for r in rep["refusals"])


def test_any_allows_the_pages_content_hosts_but_no_document_tracker_or_write():
    g = guard(scope_for("any"))
    assert g.decide("https://api.widget.test/data.json", "XHR") == ""
    assert g.decide("https://cdn.datahost.test/lib.js", "Script") == ""
    assert g.decide("https://www.google-analytics.com/collect", "XHR").startswith("third-party host")
    assert g.decide("https://api.widget.test/data.json", "XHR", method="POST").startswith(("method", "third-party"))
    assert g.decide("https://other-site.test/page", "Document").startswith("third-party host")


@pytest.mark.parametrize("mode", ["search", "any"])
def test_every_mode_refuses_never_contact_hosts_and_government_hosts_with_the_rule(mode, monkeypatch):
    from anyuse.execution import site_modes
    monkeypatch.setattr(site_modes, "_never_contact", lambda: {"never.example.test"})
    # off by default: a government host is judged like any host; NEVER_CONTACT is refused either way
    off = guard(scope_for(mode))
    assert "never contacted" not in off.decide("https://data.agency.gov/x.json", "XHR")
    assert "never contacted" in off.decide("https://never.example.test/x.json", "XHR")
    assert W.contact_refusal("api.data.europa.eu") == ""
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    sc = scope_for(mode)
    sc.observe(H, f"https://{H}/", "Document",
               '<script src="https://cdn.doofinder.com/livelayer/1/js/loader.min.js"></script>')
    g = guard(sc)
    for url in ("https://data.agency.gov/x.json", "https://never.example.test/x.json"):
        why = g.decide(url, "XHR")
        assert "never contacted" in why or why.startswith("third-party host"), (url, why)
    assert "never contacted" in g.decide("https://data.agency.gov/x.json", "XHR")
    assert W.contact_refusal("api.data.europa.eu")
    assert W.contact_refusal("never.example.test") == "on NEVER_CONTACT"
    assert W.contact_refusal("docs.anthropic.com") == "" and W.contact_refusal("cdn.browser-use.com") == ""


def test_provider_search_names_only_the_origin_policys_refusal():
    import types
    from anyuse.execution import harness_tools as HT
    api = "https://cts0zk.a.searchspring.io/api/suggest/query?siteId=cts0zk&query=drill"
    page = types.SimpleNamespace(guard=types.SimpleNamespace(failed=[(api, "page-load cap of 30 reached")]))
    assert HT.search_refusal(page, {"failed": 0}, "drill", f"https://{H}/") is None
    page.guard.failed.append((api, "third-party host cts0zk.a.searchspring.io"))
    assert HT.search_refusal(page, {"failed": 0}, "drill", f"https://{H}/")[0] == "provider_search"


# ------------------------------------------------------------------------------------------------- robots, record
def test_robots_txt_of_every_allowed_host_is_checked_once(monkeypatch):
    from anyuse.execution import api_permission as AP
    fetched = []

    def allows(url, fetcher=None, on_hop=None):     # on_hop: each redirect hop counted
        fetched.append(url)
        return (not url.startswith("https://cts0zk.a.searchspring.io/api/search/blocked"), "test")
    monkeypatch.setattr(AP, "robots_allows", allows)
    monkeypatch.setattr(AP, "robots_allows_cached", lambda url: (None, "unknown"))
    monkeypatch.setattr(AP, "robots_known_absent", lambda url: False)
    sc = scope_for("search")
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    gate = HP.robots_gate(scope=sc)
    g = guard(sc)
    api = "https://cts0zk.a.searchspring.io/api/search/search.json?q=drill"
    assert g.decide(api, "XHR") == ""                                   # the scope allows the host for the site
    assert gate(api) is True and len(fetched) == 1
    gate(api)
    assert len(fetched) == 1                                            # once per host and run
    assert gate("https://unrelated.test/x") is False                    # not the scope's: fail closed, no fetch
    assert len(fetched) == 1


def test_every_third_party_host_is_on_the_run_record():
    sc = scope_for("search")
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    g = guard(sc)
    g.decide("https://cts0zk.a.searchspring.io/api/search/search.json?q=drill", "XHR")
    sc.record(H, "provider", "cts0zk.a.searchspring.io")
    g.decide("https://tracker.example.test/p", "XHR")

    class Factory:
        budget = g.budget
    rec = HP.third_party_record(Factory, H)
    assert rec["mode"] == "search" and rec["hosts"]["provider"] == {"cts0zk.a.searchspring.io": 1}
    assert any(r[0] == "tracker.example.test" for r in rec["refusals"])
    assert HP.third_party_record(None, H)["mode"] == "search"


def test_the_products_page_factory_takes_its_scope_from_the_setting(monkeypatch):
    from anyuse.execution import agent_browser as AB
    monkeypatch.setenv("ANYUSE_LEARN_ENDPOINT", "http://192.168.64.9:9222")
    monkeypatch.setattr(AB, "assert_container_endpoint", lambda ep, opener=None: {"Browser": "fake"})
    f = HP.guarded_page_factory(start_url=f"https://{H}/", site_cap=50)
    assert f.budget.web_scope is not None and f.budget.web_scope.mode == "search"
    monkeypatch.setenv("ANYUSE_THIRD_PARTY", "off")
    off = HP.guarded_page_factory(start_url=f"https://{H}/", site_cap=50).budget.web_scope
    assert off is not None and off.own_hosts and not off.providers
    monkeypatch.setenv("ANYUSE_ALLOW_SITE_OWN_HOSTS", "off")
    assert HP.guarded_page_factory(start_url=f"https://{H}/", site_cap=50).budget.web_scope is None


# -------------------------------------------------------------------------------------------------------------------
# a store platform's own asset CDN; the provider's own share per page load
SHOP_PAGE = ('<html><link rel="stylesheet" href="//www.toolnut.test/cdn/shop/t/1/assets/base.css">'
             '<script src="https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js"></script>'
             '<link rel="preload" href="https://fonts.shopifycdn.com/inter/inter_n4.woff2" as="font"></html>')


def test_a_platforms_asset_cdn_is_first_party_for_static_assets_only(monkeypatch):
    # this test is another rule's: the page-code rule (on by default) is pinned off here
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    sc = scope_for("search")
    g = guard(sc, realistic_browser=True, static_bytes_cap=10_000_000)     # (a budget that loads CSS, fonts, images)
    js = "https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.js"
    assert g.decide(js, "Script").startswith("third-party host")             # not before the site's page names it
    sc.observe(H, f"https://{H}/", "Document", SHOP_PAGE)
    for url, rtype in ((js, "Script"), ("https://cdn.shopify.com/s/files/1/0001/t/1/assets/a.css", "Stylesheet"),
                       ("https://fonts.shopifycdn.com/inter/inter_n4.woff2", "Font"),
                       ("https://cdn.shopify.com/s/files/1/0001/products/p.jpg", "Image")):
        assert g.decide(url, rtype) == "", (url, rtype)
        assert g.last_kind == "platform_cdn"
    assert g.decide("https://cdn.shopify.com/s/files/x.json", "XHR").startswith("third-party host")   # data: never
    assert g.decide("https://cdn.shopify.com/", "Document").startswith("third-party host")             # a page: never
    assert g.decide(js, "Script", method="POST").startswith(("method", "third-party"))
    # a host of the catalogue the page did not name, and an unknown CDN, stay third party (fail closed)
    assert g.decide("https://extensions.shopifycdn.com/x.js", "Script").startswith("third-party host")
    assert g.decide("https://cdn.unknowncdn.test/x.js", "Script").startswith("third-party host")
    sc.record(H, "platform_cdn", "cdn.shopify.com")
    rep = sc.site_report(H)
    assert rep["platform_cdn"] == {"shopify": ["cdn.shopify.com", "fonts.shopifycdn.com"]}
    assert rep["hosts"]["platform_cdn"] == {"cdn.shopify.com": 1}
    # off: first-party only - the platform CDN is not granted (the site's own hosts are)
    off = scope_for("off")
    assert off is not None and not off.platform_cdn and not off.providers and off.own_hosts
    assert [c.name for c, _h in W.detect_platform_cdns(SHOP_PAGE)] == ["shopify"]
    assert W.detect_platform_cdns('<script src="https://cdn11.bigcommerce.com/s-abc123/x.js"></script>')[0][1] == \
        ["cdn11.bigcommerce.com"]


def test_a_host_the_sites_document_loads_its_assets_from_is_first_party_for_static_assets(monkeypatch):
    # a store platform the catalogue does not hold: the site's own document loads its theme, fonts and images from it
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    sc = scope_for("search")
    g = guard(sc, realistic_browser=True, static_bytes_cap=10_000_000)
    page = ('<html><head><link rel="stylesheet" href="https://static.winkelplatform.test/thema/basis.css">'
            '<style>@font-face{src:url("https://lettertype.cdnplatforma.test/f/a.woff2")}</style></head><body>'
            '<img srcset="https://beelden.winkelplatform.test/p/1.webp 1x, https://beelden.winkelplatform.test/p/2.webp 2x">'
            '<script src="https://static.winkelplatform.test/thema/app.js"></script>'
            '<a href="https://sociaal.netwerk.test/winkel">volg ons</a> https://tekst.genoemd.test/x</body></html>')
    css = "https://static.winkelplatform.test/thema/basis.css"
    assert g.decide(css, "Stylesheet").startswith("third-party host")          # not before the site's page names it
    sc.observe(H, f"https://{H}/", "Document", page)
    for url, rtype in ((css, "Stylesheet"), ("https://static.winkelplatform.test/thema/app.js", "Script"),
                       ("https://lettertype.cdnplatforma.test/f/a.woff2", "Font"),
                       ("https://beelden.winkelplatform.test/p/2.webp", "Image")):
        assert g.decide(url, rtype) == "", (url, rtype)
        assert g.last_kind == "platform_cdn" and "asset host" in sc.classify(H, url, "GET", rtype).detail
    # data, a page, a POST, a host only linked to or written in the text: never
    assert g.decide("https://static.winkelplatform.test/api/producten.json", "XHR").startswith("third-party host")
    assert g.decide("https://static.winkelplatform.test/", "Document").startswith("third-party host")
    assert g.decide(css, "Stylesheet", method="POST").startswith(("method", "third-party"))
    for url in ("https://sociaal.netwerk.test/w.png", "https://tekst.genoemd.test/x.png"):
        assert g.decide(url, "Image").startswith("third-party host"), url
    assert set(sc.site_report(H)["asset_hosts"]) == {"static.winkelplatform.test", "lettertype.cdnplatforma.test",
                                                     "beelden.winkelplatform.test"}
    # a host that answers the page with data serves data, not assets: its grant ends
    sc.note_response(H, "beelden.winkelplatform.test", "application/json; charset=utf-8", 200)
    assert g.decide("https://beelden.winkelplatform.test/p/3.webp", "Image").startswith("third-party host")
    for mime, status in (("text/css", 200), ("application/octet-stream", 200), ("text/html", 404)):
        sc.note_response(H, "static.winkelplatform.test", mime, status)
    assert g.decide(css, "Stylesheet") == ""
    # the ad / tracker snapshot and the typed value still refuse
    sc2 = scope_for("search")
    sc2._blocklist = frozenset({"cdnplatforma.test"})
    sc2.observe(H, f"https://{H}/", "Document", page)
    assert not sc2.classify(H, "https://lettertype.cdnplatforma.test/f/a.woff2", "GET", "Font").ok
    assert not sc2.classify(H, "https://beelden.winkelplatform.test/p/kaffee.webp", "GET", "Image",
                            values=("kaffee",)).ok


def test_the_guarded_session_tells_the_scope_what_an_asset_host_answered():
    import asyncio
    sc = scope_for("search")
    sc.observe(H, f"https://{H}/", "Document", '<img src="https://beelden.winkelplatform.test/p/1.webp">')
    s = G.GuardedSession("http://192.0.2.1:9222", budget=G.RequestBudget(D.Pacer(1.0), web_scope=sc))
    s.guard.start(f"https://{H}/")
    msg = {"method": "Network.responseReceived",
           "params": {"requestId": "r1", "type": "XHR", "response": {
               "url": "https://beelden.winkelplatform.test/api/p.json", "mimeType": "application/json", "status": 200}}}
    assert sc.asset_host(H, "beelden.winkelplatform.test")
    asyncio.run(s._maybe_gate(msg))
    assert not sc.asset_host(H, "beelden.winkelplatform.test")


def paused(i, url, rtype, method="GET", frame="f1"):
    return {"requestId": f"r{i}", "networkId": f"n{i}", "frameId": frame, "resourceType": rtype,
            "request": {"url": url, "method": method, "headers": {}}}


def test_the_search_provider_has_its_own_share_per_page_load():
    import asyncio
    sc = scope_for("search")
    sc.observe(H, f"https://{H}/", "Document", SPRING)
    clock = [1000.0]

    async def asleep(s):
        clock[0] += s
    pacer = D.Pacer(0.0, clock=lambda: clock[0], sleeper=lambda s: None)
    b = G.RequestBudget(pacer, per_load_cap=3, site_cap=40, web_scope=sc, load_per_navigation=True,
                        asleep=asleep, static_reserve=30)
    g = G.LoadGuard(b)
    g.start(f"https://{H}/")
    sent = []

    async def send(m, p):
        sent.append((m, p.get("requestId")))

    async def go(reqs):
        for r in reqs:
            await g.handle(r, send)
    api = "https://cts0zk.a.searchspring.io/api/search/search.json?q=drill&p={}"
    reqs = [paused(0, f"https://{H}/collections/shop?q=drill", "Document")]
    reqs += [paused(i, f"https://{H}/s{i}.js", "Script") for i in range(1, 4)]          # the site's load cap (3) used
    reqs += [paused(10 + i, api.format(i), "XHR") for i in range(11)]                     # the provider's own share
    reqs += [paused(30, f"https://{H}/api/more", "XHR")]                                 # the site's cap still holds
    asyncio.run(go(reqs))
    cont = [rid for m, rid in sent if m == "Fetch.continueRequest"]
    assert cont == ["r0", "r1", "r2"] + [f"r{10 + i}" for i in range(10)], cont   # 10 provider requests, not the 11th
    assert any("provider share of 10" in w for _u, w in g.failed)
    assert any("page-load cap of 3" in w for _u, w in g.failed)
    assert b.site_total(H) == 13 and b.provider_sent[H] == 10 and b.own_total(H) == 3   # in the run total, not the share
    # a new page load: a fresh provider share
    asyncio.run(go([paused(40, f"https://{H}/collections/shop?q=saw", "Document"),
                    paused(41, api.format(99), "XHR")]))
    assert ("Fetch.continueRequest", "r41") in sent


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ----------------------------------------------------------------------------------------------------------------
# ANYUSE_ALLOW_SITE_OWN_HOSTS ON by default - the site's own subdomains and CDN hosts on the SAME registrable
# domain (a shared-hosting suffix counts per tenant), GET/HEAD, in the site's budget; robots, gov / NEVER_CONTACT
# refusals, ANYUSE_THIRD_PARTY and pacing unchanged
def test_the_sites_own_hosts_are_allowed_by_default_and_nothing_else_is(monkeypatch):
    # this test is another rule's: the page-code rule (on by default) is pinned off here
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    from anyuse import settings as S
    assert S.SETTINGS["ANYUSE_ALLOW_SITE_OWN_HOSTS"].default is True
    assert not S.SETTINGS["ANYUSE_ALLOW_SITE_OWN_HOSTS"].needs_confirmation(True)
    sc = scope_for("search")
    assert sc.own_hosts
    g = guard(sc)
    assert g.decide("https://cdn-assets.toolnut.test/app.js", "Script") == ""
    assert g.decide("https://static.toolnut.test/api/search?q=x", "XHR") == ""
    # another registrable domain: refused as before
    assert g.decide("https://cdn.toolnut-assets.test/app.js", "Script").startswith("third-party host")
    assert g.decide("https://toolnut.test.evil.test/app.js", "Script").startswith("third-party host")
    # GET/HEAD only; a tracking label of the site's own domain stays refused
    assert g.decide("https://api.toolnut.test/cart", "XHR", method="POST").startswith("method")
    assert g.decide("https://matomo.toolnut.test/matomo.js", "Script").startswith("third-party host")
    # a shared-hosting suffix counts per tenant: a sibling tenant is never the site's own
    assert sc.is_own_host("shop-a.github.io", "cdn.shop-a.github.io")
    assert not sc.is_own_host("shop-a.github.io", "shop-b.github.io")
    assert not sc.is_own_host("shop-a.github.io", "github.io")
    # a government host never with ANYUSE_REFUSE_GOV_HOSTS on, whatever the domain
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    assert scope_for("search").classify("www.amt.example.gov", "https://cdn.amt.example.gov/x.js", "GET",
                                        "Script").why


def test_off_restores_the_exact_host_guard(monkeypatch):
    # this test is another rule's: the page-code rule (on by default) is pinned off here
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    monkeypatch.setenv("ANYUSE_ALLOW_SITE_OWN_HOSTS", "off")
    sc = scope_for("search")
    assert not sc.own_hosts
    assert guard(sc).decide("https://cdn-assets.toolnut.test/app.js", "Script").startswith("third-party host")


# ----------------------------------------------------------------------------------------------------------------
# the page's own SCRIPTS and STYLESHEETS from any other registrable domain - GET only, by
# the browser's resource type; never images / fonts / media / XHR / fetch / websockets / beacons, never a listed ad or
# tracker host, never gov / NEVER_CONTACT, never robots-disallowed, never a request carrying the typed value
def test_page_code_from_another_domain_loads_and_nothing_else_of_that_host(monkeypatch):
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    from anyuse import settings as S
    assert S.SETTINGS["ANYUSE_THIRD_PARTY_PAGE_CODE"].default is True
    sc = scope_for("search")
    assert sc.page_code
    g = guard(sc, browser_statics=True, realistic_browser=True, static_bytes_cap=10_000_000)
    for url, rtype in (("https://asset1-327a.kxcdn.test/js/app.js", "Script"),
                       ("https://asset1-327a.kxcdn.test/css/site.css", "Stylesheet")):
        assert g.decide(url, rtype) == "", (url, rtype)
        assert g.last_kind == W.KIND_PAGE_CODE and g.last_static          # counted as a browser static
    for url, rtype in (("https://asset1-327a.kxcdn.test/img/a.png", "Image"),
                       ("https://asset1-327a.kxcdn.test/f/a.woff2", "Font"),
                       ("https://asset1-327a.kxcdn.test/api/list.json", "XHR"),
                       ("https://asset1-327a.kxcdn.test/api/list", "Fetch"),
                       ("https://asset1-327a.kxcdn.test/v.mp4", "Media"),
                       ("https://asset1-327a.kxcdn.test/ws", "WebSocket"),
                       ("https://asset1-327a.kxcdn.test/b", "Ping")):
        assert g.decide(url, rtype).startswith(("third-party host", "resource type")), (url, rtype)
    assert g.decide("https://asset1-327a.kxcdn.test/js/app.js", "Script", method="POST").startswith(("method", "third"))
    # a listed tracker's script, a government host (the rule on): refused; any other domain's script loads like the one
    # above
    assert "blocklist" in g.decide("https://www.google-analytics.com/analytics.js", "Script")
    assert g.decide("https://cdn.amt.example.gov/x.js", "Script").startswith("third-party host")
    assert g.decide("https://cdn.browser-use.com/x.js", "Script") == ""


def test_a_script_carrying_the_typed_value_is_the_sites_provider_and_robots_still_decides():
    sc = scope_for("search")
    jsonp = "https://suggest.libcdn.test/jsonp.js?q=Akkuschrauber&cb=f"
    # the site's page sends the typed value there (JSONP): the host is the site's search provider, not page code
    v = sc.classify(H, jsonp, "GET", "Script", values=("Akkuschrauber",))
    assert v.ok and v.kind == W.KIND_PROVIDER and v.detail.startswith("observed"), v
    # inside a page address another script reports, the value never makes a provider: page code refuses it
    v = sc.classify(H, "https://cdn.libcdn.test/x.js?u=https%3A%2F%2Fwww.toolnut.test%2Fs%3Fq%3DAkkuschrauber",
                    "GET", "Script", values=("Akkuschrauber",))
    assert not v.ok and "typed value" in v.why                     # never a leak of the value to an unchosen host
    assert not scope_for("off").classify(H, jsonp, "GET", "Script", values=("Akkuschrauber",)).ok
    # a short value inside a longer word is not the value: the page's code loads
    v = sc.classify(H, "https://cdn.libcdn.test/js/tvshow.js", "GET", "Script", values=("tv",))
    assert v.ok and v.kind == W.KIND_PAGE_CODE, v
    clock = lambda: 1000.0  # noqa: E731
    b = G.RequestBudget(D.Pacer(1.0, clock=clock, sleeper=lambda s: None), web_scope=sc, realistic_browser=True,
                        static_bytes_cap=10_000_000)
    g = G.LoadGuard(b, allow_url=lambda u: "/private/" not in u)      # robots.txt of the CDN host disallows /private/
    g.start(f"https://{H}/")
    assert g.decide("https://cdn.libcdn.test/public/a.js", "Script") == ""
    assert g.decide("https://cdn.libcdn.test/private/a.js", "Script").startswith("robots_refused")


def test_page_code_off_restores_todays_rule_and_third_party_off_keeps_it_off(monkeypatch):
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    sc = scope_for("search")
    assert not sc.page_code
    assert guard(sc, realistic_browser=True, static_bytes_cap=10_000_000).decide("https://cdn.libcdn.test/a.js", "Script").startswith("third-party")
    monkeypatch.delenv("ANYUSE_THIRD_PARTY_PAGE_CODE")
    off = scope_for("off")                                           # first party only: no page code from elsewhere
    assert off is not None and not off.page_code and off.own_hosts
    assert guard(off, realistic_browser=True, static_bytes_cap=10_000_000).decide("https://cdn.libcdn.test/a.js", "Script").startswith("third-party")


def test_the_products_page_factory_takes_page_code_from_the_setting(monkeypatch):
    from anyuse.execution import agent_browser as AB
    monkeypatch.setenv("ANYUSE_LEARN_ENDPOINT", "http://192.168.64.9:9222")
    monkeypatch.setattr(AB, "assert_container_endpoint", lambda ep, opener=None: {"Browser": "fake"})
    f = HP.guarded_page_factory(start_url=f"https://{H}/", site_cap=50)
    assert f.budget.web_scope is not None and f.budget.web_scope.page_code
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    assert not HP.guarded_page_factory(start_url=f"https://{H}/", site_cap=50).budget.web_scope.page_code


def test_a_pinned_supplement_tracker_script_is_refused_under_the_page_code_rule():
    # known trackers and ads stay blocked - the snapshot's misses are in a pinned supplement
    sup = W.tracker_supplement()
    assert "facebook.net" in sup and "cloudflareinsights.com" in sup and all(sup.values())     # each with its reason
    sc = W.WebScope.from_settings(mode="search")                    # the real pinned snapshot + supplement
    assert sc.page_code
    for url in ("https://connect.facebook.net/en_US/fbevents.js", "https://static.cloudflareinsights.com/beacon.min.js"):
        v = sc.classify(H, url, "GET", "Script")
        assert not v.ok and "blocklist" in v.why, (url, v.why)
    # a widget / consent manager is no tracker by purpose: its script is page code, its hits stay refused
    assert sc.classify(H, "https://widget.trustpilot.com/bootstrap/v5/tp.widget.bootstrap.min.js", "GET", "Script").ok
    for rtype in ("XHR", "Ping", "Image"):
        assert not sc.classify(H, "https://widget.trustpilot.com/stats", "GET", rtype).ok
    assert "tracker_supplement_sha256" in sc.knobs()


def test_boost_commerce_is_a_site_search_provider_once_the_sites_page_names_it():
    # www.divertimenti.co.uk: the Shopify store's search results come from Boost Commerce's search API
    sc = scope_for("search")
    url = "https://services.mybcapps.com/bc-sf-filter/search?shop=divertimenti-uk.myshopify.com&q=knife&page=1"
    assert not sc.classify(H, url, "GET", "XHR").ok                         # not before the site's own page names it
    page = '<script src="https://cdn.boostcommerce.io/widget-integration/1.89.0/bc-widget-integration.js"></script>'
    assert "boost_commerce" in sc.observe(H, f"https://{H}/", "Document", page)
    assert sc.classify(H, url, "GET", "XHR").ok
    assert not sc.classify(H, "https://services.mybcapps.com/bc-sf-filter/subscribe-b2s?shop=x", "GET", "XHR").ok
    assert not sc.classify(H, "https://services.mybcapps.com/discovery/recommend?shop=x", "GET", "XHR").ok
    assert not sc.classify(H, url, "POST", "XHR", post="{}").ok                                       # reads by GET

