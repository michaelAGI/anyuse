"""Same-site redirects and typed navigation failures (execution/site_canon.py; final-web-fresh defect 2), offline.
Pinned here: a redirect is followed only to the same site (the www sibling, or http -> https; never another domain,
another subdomain, https -> http or another port); robots.txt that redirects to the sibling is kept for BOTH hosts
(the page that lives there is gated, never refused for want of a fetch) and the hop policy refuses any other host;
the product's page factory names the sibling in its origin policy; a start page that lands on the sibling is followed
and recorded and the trace's site becomes the page's own (the compiled tool's site); a start page that does not load
is a typed stop (dns, tls, a redirect off the site, ...) and an empty start page too, never an error page handed to
the policy; a search whose form sends POST is the typed stop post_only_search. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_site_canon.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import api_permission as AP  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import site_canon as SC  # noqa: E402

import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE           # https://www.ladder-shop.test
APEX = "https://ladder-shop.test/"


def test_only_a_same_site_redirect_is_followed():
    ok = [("https://www.a.de/x", "https://a.de/x"), ("https://a.de/", "https://www.a.de/"),
          ("http://www.a.de/", "https://www.a.de/"), ("http://a.de/robots.txt", "https://www.a.de/robots.txt"),
          ("https://www.a.de/", "/robots.txt")]
    no = [("https://www.a.de/", "https://b.de/"), ("https://www.a.de/", "https://shop.a.de/"),
          ("https://a.de/", "http://a.de/"), ("https://a.de/", "https://a.de:8443/"),
          ("https://www.a.de/", "https://www.a.de.evil.test/"), ("https://www.a.de/", "javascript:alert(1)")]
    assert all(SC.is_same_site_redirect(a, b) for a, b in ok)
    assert not any(SC.is_same_site_redirect(a, b) for a, b in no)
    assert SC.sibling_of("www.a.de") == "a.de" and SC.sibling_of("a.de") == "www.a.de" and SC.sibling_of("10.0.0.1") == ""
    assert SC.sibling_origins("https://www.a.de/p") == ["https://www.a.de", "https://a.de"]


def test_robots_redirected_to_the_sibling_is_kept_for_both_hosts(monkeypatch=None):
    AP._ROBOTS_CACHE.clear()
    url = "https://www.redir-robots.test/search?q=x"

    def fetch(robots_url):
        AP.ROBOTS_REDIRECTS[robots_url] = "https://redir-robots.test/robots.txt"
        return "User-agent: *\nDisallow: /konto\n"
    ok, why = AP.robots_allows(url, fetcher=fetch)
    assert ok is True and "redirected to https://redir-robots.test/robots.txt" in why
    assert AP.robots_allows_cached("https://redir-robots.test/search?q=y")[0] is True
    assert AP.robots_allows_cached("https://redir-robots.test/konto")[0] is False


class FakeResp:
    def __init__(self, status, location="", text=""):
        self.status_code, self.headers, self.text = status, {"location": location} if location else {}, text

    def raise_for_status(self):
        return None


def fake_client(chain):
    class Client:
        def __init__(self, *a, **k):
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            chain["sent"].append(url)
            return chain["map"][url]
    return Client


def test_the_robots_fetch_follows_same_site_and_cross_site_hops():
    # RFC 9309 2.3.1.2: a robots.txt redirect is followed to ANY host
    import httpx
    from anyuse.execution import request_policy as RP
    real_client, real_ref = httpx.Client, RP.destination_refusal
    chain = {"sent": [], "map": {"https://www.hop.test/robots.txt": FakeResp(301, "https://hop.test/robots.txt"),
                                 "https://hop.test/robots.txt": FakeResp(200, text="User-agent: *\nAllow: /\n")}}
    httpx.Client = fake_client(chain)
    RP.destination_refusal = lambda url, allowed, resolve=False: "" if RP.origin(url) in allowed else "not allowed"
    try:
        assert "Allow" in AP._default_fetch("https://www.hop.test/robots.txt")
        assert AP.ROBOTS_REDIRECTS.pop("https://www.hop.test/robots.txt") == "https://hop.test/robots.txt"
        chain["map"]["https://www.off.test/robots.txt"] = FakeResp(302, "https://elsewhere.test/robots.txt")
        chain["map"]["https://elsewhere.test/robots.txt"] = FakeResp(200, text="User-agent: *\nDisallow: /x\n")
        assert "Disallow" in AP._default_fetch("https://www.off.test/robots.txt")
        assert "https://elsewhere.test/robots.txt" in chain["sent"]
        assert AP.ROBOTS_REDIRECTS.pop("https://www.off.test/robots.txt") == "https://elsewhere.test/robots.txt"
    finally:
        httpx.Client, RP.destination_refusal = real_client, real_ref


def test_the_product_page_factory_names_the_sibling():
    from anyuse.execution import harness_product as HP
    real = (HP.__dict__.get("guarded_page_factory"),)
    import anyuse.execution.agent_browser as AB
    saved = AB.assert_container_endpoint, AB.learn_endpoint
    AB.assert_container_endpoint, AB.learn_endpoint = (lambda ep: None), (lambda ep="": "http://192.168.70.9:9222")
    try:
        f = HP.guarded_page_factory(start_url="https://www.sib.test/")
        pol = f.budget.origin_policy
        assert pol.allows("www.sib.test", "sib.test") and not pol.allows("www.sib.test", "other.test")
    finally:
        AB.assert_container_endpoint, AB.learn_endpoint = saved
    assert real[0] is HP.guarded_page_factory


class ApexShop(SHOP.Shop):
    """The shop lives at www; its apex answers with a redirect to www (the page lands there)."""

    def route(self, url):
        if url.startswith(APEX):
            url = BASE + "/" + url[len(APEX):]
        return super().route(url)


def test_a_start_page_on_the_sibling_is_followed_recorded_and_becomes_the_site():
    site = ApexShop()
    tools = HT.BrowserTools(D.DomPage(site), APEX, "ladder-shop.test")
    pol = P.ScriptedPolicy([lambda t, o, h: T.Search(0, "Bosch"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if "/p/" in e.href][:3]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="sib").run("search for {q}",
                                                                                        {"q": "Bosch"})
    assert tr.ok and tr.start_redirect == {"from": APEX, "to": BASE + "/"}
    assert tr.site == SITE and tr.start_url == BASE + "/" and tr.to_dict()["start_redirect"]["to"] == BASE + "/"
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    assert c["tool"]["site"] == SITE


class ErrPage:
    """A live session's surface, offline: every navigation ends on Chrome's error page; the session's records say
    why (``load_errors``, the guard's ``failed``, the budget's ``refused_methods``)."""

    def __init__(self, errors=(), failed=(), methods=(), after_search=None):
        self.load_errors = list(errors)
        self.guard = type("G", (), {"failed": list(failed)})()
        self.budget = type("B", (), {"refused_methods": list(methods)})()
        self.url_now = "chrome-error://chromewebdata/"
        self.after_search = after_search

    async def goto(self, url):
        self.url_now = "chrome-error://chromewebdata/" if self.url_now.startswith("chrome-error") else self.url_now

    async def settle(self, s=0.0):
        return None


def start_failed(page):
    import types
    tools = HT.BrowserTools(page, "https://www.down.test/", "www.down.test")

    async def obs(_p):
        return {"url": page.url_now, "text": ""}, {"elements": [], "text": ""}, {}
    import anyuse.execution.demo_policy as DPOL
    real = DPOL.observe
    DPOL.observe = obs
    try:
        tools.page.network = types.MethodType(lambda self, want_bodies=False: _empty(), tools.page)
        return H.Harness(HT.Toolbox(browser=tools), P.ScriptedPolicy([T.Fail("x")]), H.HarnessConfig(),
                         run_id="down").run("search for {q}", {"q": "x"})
    finally:
        DPOL.observe = real


async def _empty():
    return []


def test_a_start_page_that_does_not_load_is_a_typed_stop():
    tr = start_failed(ErrPage(errors=[{"url": "https://www.down.test/", "type": "Document",
                                       "errorText": "net::ERR_NAME_NOT_RESOLVED"}]))
    assert tr.stop == T.Stop.START_FAILED and "start failed (dns)" in tr.stop_reason and tr.decisions == 0
    tr = start_failed(ErrPage(errors=[{"type": "Document", "errorText": "net::ERR_CERT_DATE_INVALID"}]))
    assert "start failed (tls)" in tr.stop_reason
    tr = start_failed(ErrPage(failed=[("https://elsewhere.test/", "third-party host elsewhere.test")]))
    assert "start failed (cross_site_redirect)" in tr.stop_reason
    tr = start_failed(ErrPage())
    assert "start failed (unknown)" in tr.stop_reason and "No site URL" not in tr.stop_reason


def test_an_empty_start_page_is_a_typed_stop():
    import types
    page = ErrPage()
    page.url_now = "https://www.down.test/"
    tr = start_failed(page)
    assert tr.stop == T.Stop.START_FAILED and "empty_page" in tr.stop_reason
    del types


def test_the_classifier_reads_every_record():
    assert SC.classify_failure(ErrPage(methods=[("POST", "https://a.de/suche")]))[0] == "post_only"
    assert SC.classify_failure(ErrPage(failed=[("https://a.de/x", "host cap of 17 reached")]))[0] == "budget"
    assert SC.classify_failure(ErrPage(errors=[{"errorText": "net::ERR_CONNECTION_REFUSED"}]))[0] == "unreachable"
    assert SC.classify_failure(ErrPage(errors=[{"errorText": "net::ERR_TOO_MANY_REDIRECTS"}]))[0] == "redirect_loop"
    p = ErrPage(errors=[{"errorText": "net::ERR_NAME_NOT_RESOLVED"}])
    m = SC.marks(p)
    assert SC.classify_failure(p, since=m)[0] == "unknown"           # only what came after the mark counts
    assert set(SC.CLASSES) >= {"dns", "tls", "cross_site_redirect", "post_only", "empty_page"}


def test_a_post_only_search_is_a_typed_stop():
    site = SHOP.Shop()
    page = D.DomPage(site)
    page.budget = type("B", (), {"refused_methods": []})()
    tools = HT.BrowserTools(page, BASE + "/", SITE)
    import anyuse.execution.demo_policy as DPOL
    real = DPOL.execute

    async def post_refused(pg, d, mapping, site_, settle, value=""):
        pg.budget.refused_methods.append(("POST", BASE + "/"))
        return {"ok": True, "how": "typed", "url_after": "chrome-error://chromewebdata/"}
    DPOL.execute = post_refused
    try:
        tr = H.Harness(HT.Toolbox(browser=tools), P.ScriptedPolicy([T.Search(0, "Bosch"), T.Fail("x")]),
                       H.HarnessConfig(), run_id="post").run("search for {q}", {"q": "Bosch"})
    finally:
        DPOL.execute = real
    assert tr.stop == T.Stop.POST_ONLY_SEARCH and "post_only_search" in tr.stop_reason


def test_an_off_site_search_is_a_typed_stop():
    site = SHOP.Shop()
    page = D.DomPage(site)
    page.guard = type("G", (), {"failed": []})()
    tools = HT.BrowserTools(page, BASE + "/", SITE)
    import anyuse.execution.demo_policy as DPOL
    real = DPOL.execute

    async def offsite(pg, d, mapping, site_, settle, value=""):
        pg.guard.failed.append(("https://search.hosted.test/x", "third-party host search.hosted.test"))
        return {"ok": True, "how": "typed", "url_after": "chrome-error://chromewebdata/"}
    DPOL.execute = offsite
    try:
        tr = H.Harness(HT.Toolbox(browser=tools), P.ScriptedPolicy([T.Search(0, "Bosch"), T.Fail("x")]),
                       H.HarnessConfig(), run_id="off").run("search for {q}", {"q": "Bosch"})
    finally:
        DPOL.execute = real
    assert tr.stop == T.Stop.OFF_SITE_SEARCH and "search.hosted.test" in tr.stop_reason


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
