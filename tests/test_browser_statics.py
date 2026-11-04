"""Static assets load like a normal browser visit. Pinned, in the ONE guard (``cdp_guard.LoadGuard`` with
``RequestBudget(browser_statics=True)``, which the product's page factory turns on):
* a script, stylesheet, font or image (GET) of the site itself, its own host, its platform asset CDN or its granted
  search provider is SENT - counted apart (``static_sent``), logged (``budget.sent``, the scope's record), run-cached -
  and never counted in the run's ceiling, the per-load cap, the static share or the provider's share;
* the run's ceiling and the per-load cap count documents, data calls (XHR / fetch, a provider's search call) and every
  robots.txt GET only; a static whose QUERY carries the searched value is a data call (a JSONP search endpoint);
* a third-party content host's static (ANYUSE_THIRD_PARTY=any) keeps the old rules (counted, CSS / fonts / images
  blocked); gov / NEVER_CONTACT hosts, robots.txt, the account-path rule and the 3 s document gap hold;
* the runaway guard: only a REAL LOOP - the same static URL asked for more than
  ``STATIC_RUNAWAY_REPEAT`` times, or more than ``STATIC_RUNAWAY_DISTINCT`` distinct statics, in one page load - stops
  the site for the run, the typed stop ``static_runaway`` (agent run and proof replay); a heavy page loads fully;
* browser statics go through their own lane (at most ``BROWSER_STATIC_IN_FLIGHT`` in flight), never the 1 req/s lane
  of documents and data; their bytes are counted apart (a run's byte ceiling counts documents and data);
* an XHR / fetch GET of a static FILE (by extension, or by its response's content
  type) is a browser static; a sub-frame analytics / pixel document (Shopify's web-pixel sandboxes) is refused like a
  tracker; the replay route (``watch_live``: anyuse watch, the MCP web-tool route) uses the same rule;
* without the knob the guard is byte-for-byte the guard before it.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_browser_statics.py
"""
import asyncio
import html
import pathlib
import sys
import tempfile
import types
import urllib.parse

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as AH  # noqa: E402
from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as Dsc  # noqa: E402
from anyuse.execution import dom_page as DP  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import web_scope as W  # noqa: E402

H = "www.shop.test"
SHOP = ('<html><script src="https://snapui.searchspring.io/cts0zk/bundle.js"></script>'
        '<link rel="stylesheet" href="https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.css"></html>')


@pytest.fixture(autouse=True)
def scratch_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    for k in ("ANYUSE_THIRD_PARTY", "ANYUSE_THIRD_PARTY_POLICY", "ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS",
              "ANYUSE_ALLOW_READONLY_POST_SEARCH", "ANYUSE_ALLOW_SITE_OWN_HOSTS"):
        monkeypatch.delenv(k, raising=False)


def budget(scope=None, **kw):
    clock = [1000.0]

    async def asleep(s):
        clock[0] += s
    pacer = Dsc.Pacer(1.0, clock=lambda: clock[0], sleeper=lambda s: None)
    kw.setdefault("site_cap", 10)
    kw.setdefault("per_load_cap", 5)
    kw.setdefault("per_host_cap", kw["site_cap"])
    kw.setdefault("load_per_navigation", True)
    kw.setdefault("browser_statics", True)
    b = G.RequestBudget(pacer, asleep=asleep, web_scope=scope, **kw)
    g = G.LoadGuard(b)
    g.start(f"https://{H}/")
    return b, g


def paused(i, url, rtype, method="GET", frame="f1"):
    return {"requestId": f"r{i}", "networkId": f"n{i}", "frameId": frame, "resourceType": rtype,
            "request": {"url": url, "method": method, "headers": {}}}


def drive(g, reqs, finish=True):
    """Answer the paused requests; with ``finish`` each static's loadingFinished frees its lane slot at once."""
    sent = []

    async def send(m, p):
        sent.append((m, p.get("requestId")))

    async def go():
        for r in reqs:
            await g.handle(r, send)
            if finish and r["networkId"] in g.budget.static_open:
                await g.release(r["networkId"])
    asyncio.run(go())
    return sent


def continued(sent):
    return [rid for m, rid in sent if m == "Fetch.continueRequest"]


# ------------------------------------------------------------------------------------------------- the decision
def test_the_sites_statics_are_sent_and_counted_apart_from_the_ceiling():
    b, g = budget()
    reqs = [paused(0, f"https://{H}/", "Document")]
    reqs += [paused(i, f"https://{H}/a/s{i}.{ext}", rt) for i, (ext, rt) in enumerate(
        [("js", "Script"), ("css", "Stylesheet"), ("woff2", "Font"), ("png", "Image")] * 10, start=1)]
    reqs += [paused(90, f"https://{H}/api/list?page=1", "XHR")]
    sent = drive(g, reqs)
    assert continued(sent) == [f"r{i}" for i in range(41)] + ["r90"]                   # all 40 statics sent
    assert b.site_total(H) == 2                                                        # the document and the data call
    assert b.static_sent[H] == 40 and b.static_sent_kinds[H] == {"site": 40}
    assert b.static_load_max[H] == 40 and g.load_n == 2                               # the per-load cap counts 2
    assert sum(1 for _h, _t, _u, ty in b.sent if ty in G.STATIC_TYPES) == 40          # every static logged
    assert not b.static_skipped and not b.stopped


def test_the_ceilings_count_documents_and_data_only():
    b, g = budget(site_cap=4, per_load_cap=3)
    reqs = [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/s{i}.js", "Script")
                                                        for i in range(1, 20)]
    reqs += [paused(30 + i, f"https://{H}/api/x{i}", "XHR") for i in range(4)]
    sent = drive(g, reqs)
    assert [r for r in continued(sent) if r in {"r30", "r31", "r32", "r33"}] == ["r30", "r31"]   # per-load cap 3
    assert any("page-load cap of 3" in w for _u, w in g.failed)
    assert b.static_sent[H] == 19 and b.site_total(H) == 3


def test_no_static_share_for_browser_statics_but_it_stays_for_counted_statics(monkeypatch):
    # the counted statics of a content host under ANYUSE_THIRD_PARTY=any (this test's subject): the page-code rule (on
    # by default: such scripts / stylesheets are browser statics then) is pinned off here
    monkeypatch.setenv("ANYUSE_THIRD_PARTY_PAGE_CODE", "off")
    sc = W.WebScope.from_settings(mode="any")
    sc._blocklist = frozenset({"ads.test"})
    b, g = budget(sc, site_cap=10, per_load_cap=30, static_reserve=8)                  # static share: 2
    reqs = [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/x1", "XHR"),
            paused(2, f"https://{H}/x2", "XHR")]
    reqs += [paused(10 + i, f"https://{H}/s{i}.js", "Script") for i in range(5)]       # the site's: past the share
    reqs += [paused(20, "https://cdn.content.test/lib.js", "Script"),                  # a content host's: counted
             paused(21, "https://cdn.content.test/look.css", "Stylesheet")]            # its CSS: blocked as before
    sent = drive(g, reqs)
    assert [f"r1{i}" for i in range(5)] == [r for r in continued(sent) if r.startswith("r1") and len(r) == 3]
    assert "r20" not in continued(sent) and "r21" not in continued(sent)
    assert any("static share of 2" in w for u, w in g.failed if "lib.js" in u)
    assert any("resource type Stylesheet is blocked" in w for u, w in g.failed if "look.css" in u)


def test_platform_cdn_and_provider_statics_are_browser_statics_the_providers_search_is_data():
    sc = W.WebScope.from_settings(mode="search")
    sc.observe(H, f"https://{H}/", "Document", SHOP)
    b, g = budget(sc, site_cap=40, per_load_cap=3)
    api = "https://cts0zk.a.searchspring.io/api/search/search.json?siteId=cts0zk&q=drill&p={}"
    reqs = [paused(0, f"https://{H}/collections/shop?q=drill", "Document")]
    reqs += [paused(i, f"https://cdn.shopify.com/s/files/1/0001/t/1/assets/a{i}.js", "Script") for i in range(1, 30)]
    reqs += [paused(40, "https://cdn.shopify.com/s/files/1/0001/t/1/assets/theme.css", "Stylesheet")]
    reqs += [paused(50 + i, f"https://snapui.searchspring.io/cts0zk/chunk{i}.js", "Script") for i in range(15)]
    reqs += [paused(70 + i, api.format(i), "XHR") for i in range(12)]
    sent = drive(g, reqs)
    c = continued(sent)
    assert all(f"r{i}" in c for i in range(1, 30)) and "r40" in c                      # the store's theme: all of it
    assert all(f"r{50 + i}" in c for i in range(15))                                   # the provider's scripts: all
    assert [r for r in c if r in {f"r{70 + i}" for i in range(12)}] == [f"r{70 + i}" for i in range(10)]   # share 10
    assert b.static_sent_kinds[H] == {"platform_cdn": 30, "provider": 15}
    assert b.site_total(H) == 11 and b.provider_sent[H] == 10                          # doc + 10 provider data calls
    rep = sc.site_report(H)
    assert rep["hosts"]["platform_cdn"]["cdn.shopify.com"] == 30 and rep["usage"]["provider"] == 25


def test_a_static_whose_query_carries_the_searched_value_is_a_data_call():
    b, g = budget(site_cap=10)
    g.typed.append("drill")
    sent = drive(g, [paused(0, f"https://{H}/", "Document"),
                     paused(1, f"https://{H}/search.js?q=drill&callback=cb", "Script"),        # JSONP search: data
                     paused(2, f"https://{H}/img/drill-press.jpg", "Image"),                   # named after it: asset
                     paused(3, f"https://{H}/img/x.jpg", "Image")])
    assert continued(sent) == ["r0", "r1", "r2", "r3"]
    assert b.site_total(H) == 2 and b.static_sent[H] == 2
    s = G.GuardedSession("http://192.0.2.1:9222", budget=b, allow_url=lambda u: True)
    s.note_typed("Canon")
    assert "canon" in s.guard.typed


def test_the_rules_that_hold_for_every_request_hold_for_statics(monkeypatch):
    b, g = budget()
    g.allow_url = lambda u: "/private/" not in u
    sent = drive(g, [paused(0, f"https://{H}/", "Document"),
                     paused(1, f"https://{H}/private/a.js", "Script"),                        # robots.txt
                     paused(2, f"https://{H}/account/app.js", "Script"),                      # the account path
                     paused(3, f"https://{H}/a.mp4", "Media"),                                # media stays blocked
                     paused(4, "https://www.googletagmanager.com/gtm.js", "Script"),          # a tracker: third party
                     paused(5, f"https://{H}/a.js", "Script", method="POST")])                # a write
    assert continued(sent) == ["r0"]
    whys = [w for _u, w in g.failed]
    assert whys[0].startswith("robots_refused") and whys[1] == "account/login/cart path"
    # the run's record names the host the robots gate refused, with what its robots.txt said (here: never read)
    assert b.traffic(H)["robots_refused"] == {H: {"requests": 1, "robots": "unknown", "why": ""}}
    assert whys[2].startswith("resource type Media") and whys[3].startswith("third-party host")
    assert whys[4].startswith("method POST")
    # a never-contacted host (a government host with ANYUSE_REFUSE_GOV_HOSTS on) is refused before anything, in any
    # mode
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    sc = W.WebScope.from_settings(mode="any")
    b2, g2 = budget(sc)
    assert "never contacted" in g2.decide("https://data.example.gov/x.js", "Script")


def test_documents_keep_their_gap_and_statics_never_wait_on_the_document_lane():
    b, g = budget(nav_min_interval_s=3.0, per_load_cap=30, site_cap=30)
    drive(g, [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/s{i}.js", "Script")
                                                         for i in range(1, 30)])
    assert b.pacer.count[H] == 1                                   # the statics never booked the 1 req/s lane
    t0 = b.clock()
    drive(g, [paused(40, f"https://{H}/search?q=x", "Document", frame="f1")])
    assert b.clock() - t0 >= 2.0                                   # the document gap (3 s after the first) still holds


def test_the_static_lane_keeps_at_most_four_in_flight():
    b, g = budget(per_load_cap=30)
    sent = drive(g, [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/s{i}.js", "Script")
                                                                for i in range(1, 9)], finish=False)
    assert continued(sent) == ["r0", "r1", "r2", "r3", "r4"] and len(g.static_queue) == 4
    assert b.lane_limit() == G.BROWSER_STATIC_IN_FLIGHT == 4
    out = []

    async def send(m, p):
        out.append((m, p.get("requestId")))
    g._send = send
    asyncio.run(g.release("n1"))
    assert out == [("Fetch.continueRequest", "r5")]


# ----------------------------------------------------------------------------------------------------- static files by fetch
def test_a_fetch_of_a_static_file_is_a_browser_static_a_data_fetch_is_not():
    b, g = budget(site_cap=10)
    g.typed.append("drill")
    sent = drive(g, [paused(0, f"https://{H}/", "Document"),
                     paused(1, f"https://{H}/cdn/shopifycloud/checkout-web/assets/c1/app-legacy.Bllj9bY5.js", "Fetch"),
                     paused(2, f"https://{H}/assets/look.css", "XHR"),
                     paused(3, f"https://{H}/cart.js", "XHR"),
                     paused(4, f"https://{H}/products/x.json", "Fetch"),
                     paused(5, f"https://{H}/search.js?q=drill", "Fetch")])
    assert continued(sent) == ["r0", "r1", "r2", "r4", "r5"]                # /cart.js: the cart path rule, as before
    # the document, x.json and the JSONP search count; the two script / stylesheet FILES are browser statics
    assert b.site_total(H) == 3 and b.static_sent[H] == 2
    # a FILE whose response is data after all (Shopify's /cart.js answers JSON) moves to the data count
    assert g.reclassify("n2", "application/json") is True and b.site_total(H) == 4 and b.static_sent[H] == 1
    assert g.reclassify("n1", "text/javascript") is False and b.static_sent[H] == 1
    # at the ceiling a fetch of a file is not sent (it may be data)
    b2, g2 = budget(site_cap=1)
    sent2 = drive(g2, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/a/app.js", "Fetch"),
                       paused(2, f"https://{H}/a/app2.js", "Script")])
    assert continued(sent2) == ["r0", "r2"] and any("may be data" in w for _u, w in g2.failed)


def test_a_counted_fetch_whose_response_is_a_static_file_moves_to_the_statics():
    b, g = budget(site_cap=10)
    drive(g, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/api/chunk?id=7", "Fetch"),
              paused(2, f"https://{H}/api/list", "XHR")])
    assert b.site_total(H) == 3 and g.load_n == 3
    assert g.reclassify("n1", "application/javascript; charset=utf-8") is True
    assert g.reclassify("n2", "application/json") is False
    assert b.site_total(H) == 2 and g.load_n == 2 and b.static_sent[H] == 1 and len(b.reclassified) == 1
    assert "n1" in g.static_ids and g.reclassify("n1", "text/css") is False       # once only


def test_a_sub_frame_telemetry_document_is_refused_like_a_tracker():
    b, g = budget(site_cap=20, per_load_cap=20)
    pixel = f"https://{H}/web-pixels@7ec887c8w14f9f074p854094e1m1370c602/custom/web-pixel-192905510@1/sandbox/legacy/"
    sent = drive(g, [paused(0, f"https://{H}/", "Document", frame="main"),
                     paused(1, pixel, "Document", frame="px1"),
                     paused(2, f"https://{H}/analytics/frame.html", "Document", frame="px2"),
                     paused(3, f"https://{H}/embed/size-chart", "Document", frame="sub"),   # the site's own content
                     paused(4, f"https://{H}/collections/shop?q=drill", "Document", frame="main")])
    assert continued(sent) == ["r0", "r3", "r4"]
    assert sum(1 for _u, w in g.failed if w.startswith("telemetry frame")) == 2
    assert b.site_total(H) == 3
    # without the knob (the benches) a sub-frame document is decided as before
    b2, g2 = budget(browser_statics=False, site_cap=20, per_load_cap=20)
    assert continued(drive(g2, [paused(0, f"https://{H}/", "Document", frame="main"),
                                paused(1, pixel, "Document", frame="px1")])) == ["r0", "r1"]


def test_static_bytes_are_counted_apart_from_the_byte_ceiling():
    b, g = budget(site_cap=10)
    s = G.GuardedSession("http://192.0.2.1:9222", budget=b, allow_url=lambda u: True)
    s.guard = g
    drive(g, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/big.png", "Image")], finish=False)

    async def go():
        for rid in ("n0", "n1"):
            await s._maybe_gate({"method": "Network.requestWillBeSent",
                                 "params": {"requestId": rid, "request": {"url": f"https://{H}/x"}}})
        await s._maybe_gate({"method": "Network.loadingFinished", "params": {"requestId": "n0", "encodedDataLength": 100}})
        await s._maybe_gate({"method": "Network.loadingFinished",
                             "params": {"requestId": "n1", "encodedDataLength": 900000}})
    asyncio.run(go())
    assert b.site_bytes[H] == 100 and b.static_bytes[H] == 900000
    assert b.traffic(H)["static_bytes"] == 900000 and b.traffic(H)["bytes"] == 100


def test_the_replay_route_uses_the_same_rule():
    src = (_HERE / "execution" / "watch_live.py").read_text(encoding="utf-8")
    assert "browser_statics=True" in src and "traffic=ctx.budget.traffic(site)" in src
    sys.path.insert(0, str(_HERE / "execution" / "web_runtime"))
    import web_arch3_live as L3
    gate = types.SimpleNamespace(holds=lambda s: True)
    ctx = L3.SiteCtx(H, "watch", 40, 4_000_000, "http://192.0.2.1:9222", gate, browser_statics=True)
    assert ctx.budget.browser_statics and ctx.budget.skip_types == frozenset() and ctx.budget.site_cap == 40
    bench = L3.SiteCtx(H, "watch", 40, 4_000_000, "http://192.0.2.1:9222", gate)
    assert not bench.budget.browser_statics and "Image" in bench.budget.skip_types     # the benches: unchanged


def test_the_lane_serves_render_critical_statics_before_prefetches():
    # www.toolnut.com: ~120 checkout-script prefetches (fetches of .js files) filled the lane; the brand page's
    # own provider scripts waited behind them. Like a browser: scripts / CSS / fonts first, images next, prefetches last
    b, g = budget(per_load_cap=30, site_cap=20)
    first = [paused(0, f"https://{H}/", "Document")] + \
        [paused(i, f"https://{H}/cdn/shopifycloud/web/assets/c{i}.js", "Fetch") for i in range(1, 9)]
    sent = drive(g, first, finish=False)
    assert continued(sent) == ["r0", "r1", "r2", "r3", "r4"] and len(g.static_queue) == 4
    sent2 = drive(g, [paused(20, f"https://{H}/img/hero.jpg", "Image"),
                      paused(21, f"https://{H}/provider/bundle.js", "Script")], finish=False)
    assert sent2 == [] and len(g.static_queue) == 6
    out = []

    async def send(m, p):
        out.append(p.get("requestId"))
    g._send = send
    for nid in ("n1", "n2", "n3"):
        asyncio.run(g.release(nid))
    assert out == ["r21", "r20", "r5"]            # the script, then the image, then the queued prefetches in order


# ------------------------------------------------------------------------------------------------- the runaway guard
def test_a_legit_heavy_page_loads_fully():
    # www.lampen24.nl: 230 distinct RequireJS modules + 60 images
    b, g = budget(per_load_cap=30)
    reqs = [paused(0, f"https://{H}/", "Document")] + \
        [paused(i, f"https://{H}/static/requirejs/m{i}.min.js", "Script") for i in range(1, 231)] + \
        [paused(500 + i, f"https://{H}/media/p{i}.jpg", "Image") for i in range(70)]
    sent = drive(g, reqs)
    assert len(continued(sent)) == 301 and not b.runaway and b.static_load_max[H] == 300


def test_the_runaway_guard_catches_the_same_url_asked_again_and_again():
    b, g = budget(per_load_cap=30)
    n = G.STATIC_RUNAWAY_REPEAT
    assert n == 5 and G.STATIC_RUNAWAY_DISTINCT == 1000
    reqs = [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/loop.js?v=1", "Script")
                                                       for i in range(1, n + 2)]
    sent = drive(g, reqs)
    assert len(continued(sent)) == 1 + n and f"r{n + 1}" not in continued(sent)
    assert b.runaway[H].startswith(G.STATIC_RUNAWAY) and "a loop" in b.runaway[H] and b.stopped[H] == b.runaway[H]
    assert drive(g, [paused(900, f"https://{H}/next", "Document")]) == [("Fetch.failRequest", "r900")]
    assert b.traffic(H)["runaway"]


def test_the_runaway_guard_catches_more_than_a_thousand_distinct_statics():
    b, g = budget(per_load_cap=30)
    reqs = [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/i{i}.png", "Image")
                                                       for i in range(1, G.STATIC_RUNAWAY_DISTINCT + 2)]
    sent = drive(g, reqs)
    assert len(continued(sent)) == 1 + G.STATIC_RUNAWAY_DISTINCT and "distinct" in b.runaway[H]


def test_a_new_page_load_starts_a_new_count_and_polled_file_fetches_are_no_loop():
    b, g = budget(per_load_cap=30, site_cap=40)
    n = G.STATIC_RUNAWAY_REPEAT
    first = [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/a.png", "Image") for i in range(1, n + 1)]
    second = [paused(1000, f"https://{H}/search?q=x", "Document")] + \
        [paused(1000 + i, f"https://{H}/a.png", "Image") for i in range(1, n + 1)]
    polled = [paused(2000 + i, f"https://{H}/cart.js", "XHR") for i in range(n + 3)]
    sent = drive(g, first + second + polled)
    assert len(continued(sent)) == 2 * (n + 1) + 0 and not b.runaway       # /cart.js: the cart path rule (no grant)
    b2, g2 = budget(per_load_cap=30, site_cap=40)
    sent2 = drive(g2, [paused(0, f"https://{H}/", "Document")] +
                  [paused(10 + i, f"https://{H}/api/state.js", "Fetch") for i in range(n + 3)])
    assert not b2.runaway and len(continued(sent2)) == 1 + n + 3            # a polled "file": never a loop


def test_the_runaway_is_the_agents_typed_stop():
    class Shop(DP.DomSite):
        def __init__(self):
            super().__init__([])

        def route(self, url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("q", "")
            if q:
                page.budget.runaway[H] = "static runaway guard: 300 static assets in one page load of " + H
            return DP.Recorded(url, '<html><body><form action="/search"><input type="search" name="q" value="'
                               f'{html.escape(q)}"></form><main><a href="/p1">Kettle</a></main></body></html>'), url
    page = DP.DomPage(Shop())
    page.budget = types.SimpleNamespace(runaway={})
    tools = HT.BrowserTools(page, f"https://{H}/", H)
    pol = P.ScriptedPolicy([T.Search(0, "Kettle"), T.ReadText()])
    tr = AH.Harness(HT.Toolbox(browser=tools), pol, AH.HarnessConfig(), run_id="runaway").run(
        "search for {q}", {"q": "Kettle"})
    assert tr.stop == T.Stop.STATIC_RUNAWAY and "static runaway" in tr.stop_reason, (tr.stop, tr.stop_reason)
    assert len(tr.steps) == 1                       # stopped at the action itself (lampen24's search), typed
    assert tr.steps[0].outcome.status == "static_runaway"
    assert T.Stop.STATIC_RUNAWAY in T.STOPS
    assert HT.runaway_stop(types.SimpleNamespace(budget=types.SimpleNamespace(runaway={}))) == ""


# ------------------------------------------------------------------------------------------------- the product
def _factory(**kw):
    import anyuse.execution.agent_browser as AB
    saved = AB.assert_container_endpoint, AB.learn_endpoint
    AB.assert_container_endpoint, AB.learn_endpoint = (lambda ep: None), (lambda ep="": "http://192.168.70.9:9222")
    try:
        return HP.guarded_page_factory(start_url=f"https://{H}/", **kw)
    finally:
        AB.assert_container_endpoint, AB.learn_endpoint = saved


def test_the_products_page_factory_loads_browser_statics_and_counts_robots():
    f = _factory(site_cap=HP.COMPILE_SITE_CAP)
    assert f.budget.browser_statics and f.budget.lane_limit() == 4 and f.budget.site_cap == 50
    assert f.budget.static_bytes_cap == 0 and not f.budget.realistic_browser
    gate = HP.robots_gate(siblings=["shop.test"])
    assert gate.on_fetch is None
    f2 = _factory(robots=gate)
    assert gate.on_fetch == f2.budget.count_robots
    f2.budget.count_robots("https://cdn.shopify.com/robots.txt")
    f2.budget.count_robots("https://shop.test/robots.txt")             # the www sibling: the site's own
    assert f2.budget.site_total(H) == 1 + (1 if f2.budget.site_of("cdn.shopify.com") == H else 0)
    rec = HP.traffic_record(f2, H)
    assert rec["browser_statics"] and rec["ceiling"] == HP.RUN_SITE_CAP and len(rec["robots_counted"]) == 2
    # at the site's ceiling a robots.txt GET is not made (the gate refuses the host: fail closed)
    f3 = _factory(site_cap=2)
    f3.budget.pacer.count[H] = 2
    assert "site ceiling of 2" in f3.budget.count_robots("https://shop.test/robots.txt")
    assert f3.budget.site_total(H) == 2 and not f3.budget.robots_counted
    fetches = []
    gate2 = HP.robots_gate(siblings=["shop.test"])
    gate2.on_fetch = f3.budget.count_robots
    saved = HP.ROBOTS_FETCHER
    HP.ROBOTS_FETCHER = lambda u: fetches.append(u) or ""
    try:
        assert gate2("https://shop.test/p/1") is False and fetches == []
    finally:
        HP.ROBOTS_FETCHER = saved


def test_without_the_knob_the_guard_is_unchanged():
    b, g = budget(browser_statics=False, site_cap=10, per_load_cap=30, static_reserve=6)
    sent = drive(g, [paused(0, f"https://{H}/", "Document")] + [paused(i, f"https://{H}/s{i}.js", "Script")
                                                                for i in range(1, 8)]
                 + [paused(20, f"https://{H}/a.css", "Stylesheet")])
    assert continued(sent) == ["r0", "r1", "r2", "r3"]                  # the static share of 4: as before
    assert b.site_total(H) == 4 and not b.static_sent and b.lane_limit() == 0
    assert any("resource type Stylesheet is blocked" in w for _u, w in g.failed)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
