"""robots.txt follows RFC 9309 (docs/TRAFFIC_POLICY.md). Pinned, in the ONE robots module
(``execution/api_permission.py``: ``fetch_robots`` / ``robots_status_rule``) that every call site uses:
* any 4xx except 429 (401 and 403 included) is "unavailable": no rules, the site is allowed, the status is the
  recorded reason (``robots_unavailable_4xx``), and it is cached like a missing file;
* 429 backs off and retries (bounded); still 429: refused for this run (``robots_rate_limited_429``), never cached;
* a 5xx or a network failure is "unreachable": a complete disallow (``robots_unreachable``), never cached;
* the shared fetch follows redirects as RFC 9309 2.3.1.2 says: at most 5 consecutive hops
  to ANY host, each hop counted in the asked site's budget; a hop to a government / NEVER_CONTACT host,
  a loop or a 6th hop is unreachable (complete disallow, typed), the chain in the recorded reason;
* the start-of-run refusal, the page gate and the cached replay gate all give the same answer.
No network: the fetcher and the HTTP transport are injected.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_robots_rfc9309.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

import httpx  # noqa: E402
import pytest  # noqa: E402

_REAL_CLIENT = httpx.Client

from anyuse.execution import api_permission as AP  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots_cache.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path))
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    waits = []
    monkeypatch.setattr(AP, "_sleep", lambda s: waits.append(s))
    yield waits
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()


def status(code, retry_after=None):
    def f(url):
        raise AP.RobotsHTTPStatus(url, code, retry_after)
    return f


def test_the_status_rule():
    assert [AP.robots_status_rule(s) for s in (200, 204, 404, 401, 403, 410, 451, 429, 500, 503, 520, None)] == \
        ["rules", "rules", "absent", "none", "none", "none", "none", "rate_limited", "unreachable", "unreachable",
         "unreachable", "unreachable"]


@pytest.mark.parametrize("code", [401, 403, 410, 451])
def test_a_4xx_robots_is_no_rules_and_the_site_is_allowed(env, code):
    url = f"https://www.s{code}.test/"
    assert HP.robots_start_refusal(url, HP.robots_gate(), fetcher=status(code)) == ""
    ok, why = AP.robots_allows(url + "x")
    assert ok is None and why.startswith("robots_unavailable_4xx") and f"HTTP {code}" in why
    assert AP.robots_known_absent(url) is True and HP.robots_gate()(url + "any/path") is True
    # read back from disk in a new process: the same answer, the same reason
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    ok, why = AP.robots_allows(url, fetcher=lambda u: pytest.fail("cached: no second fetch"))
    assert ok is None and f"HTTP {code}" in why


def test_429_backs_off_retries_and_then_refuses_for_the_run(env):
    calls = []

    def always(url):
        calls.append(url)
        raise AP.RobotsHTTPStatus(url, 429, retry_after=60)
    why = HP.robots_start_refusal("https://www.busy.test/", HP.robots_gate(), fetcher=always)
    assert "robots_rate_limited_429" in why and "nothing is opened" in why
    assert len(calls) == 1 + AP.ROBOTS_429_RETRIES and env == [AP.ROBOTS_429_MAX_WAIT_S] * AP.ROBOTS_429_RETRIES
    assert HP.robots_gate()("https://www.busy.test/") is False
    assert AP.robots_known_absent("https://www.busy.test/") is False            # never cached on disk
    # a 429 that clears: the file's rules
    seq = [AP.RobotsHTTPStatus("u", 429), "User-agent: *\nDisallow: /private\n"]

    def clears(url):
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    assert HP.robots_start_refusal("https://www.later.test/", HP.robots_gate(), fetcher=clears) == ""
    assert HP.robots_gate()("https://www.later.test/private/x") is False


@pytest.mark.parametrize("fetcher, word", [(status(500), "HTTP 500"), (status(503), "HTTP 503"), (status(520), "HTTP 520"),
                                           (lambda u: (_ for _ in ()).throw(OSError("connection reset")), "reset"),
                                           (lambda u: (_ for _ in ()).throw(TimeoutError("timed out")), "timed out")])
def test_5xx_and_network_failures_are_a_complete_disallow(env, fetcher, word):
    url = "https://www.down.test/"
    why = HP.robots_start_refusal(url, HP.robots_gate(), fetcher=fetcher)
    assert "robots_unreachable" in why and word in why and "nothing is opened" in why
    assert HP.robots_gate()(url) is False and AP.robots_allows_cached(url)[0] is False
    assert AP.robots_known_absent(url) is False                                  # never cached on disk


def test_a_policy_refusal_stays_fail_closed(env):
    def cross(url):
        raise PermissionError("robots pre-send policy refused request: a redirect to another site (other.test)")
    why = HP.robots_start_refusal("https://www.moved.test/", HP.robots_gate(), fetcher=cross)
    assert "unknown" in why and "fail closed" in why and "another site" in why
    assert HP.robots_gate()("https://www.moved.test/") is False


def mock_client(monkeypatch, handler):
    from anyuse.execution import request_policy as RP
    monkeypatch.setattr(RP, "destination_refusal", lambda *a, **k: "")

    def client(*a, **kw):
        kw.pop("transport", None)
        return _REAL_CLIENT(*a, transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", client)


def test_the_shared_fetch_follows_five_redirects_and_raises_the_status(env, monkeypatch):
    def handler(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(301, headers={"location": "/r1"})
        n = int(p[2:])
        if n < 5:
            return httpx.Response(302, headers={"location": f"/r{n + 1}"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /no\n")
    mock_client(monkeypatch, handler)
    assert AP._default_fetch("https://www.hops.test/robots.txt").startswith("User-agent")

    def six(req):
        p = req.url.path
        n = 0 if p == "/robots.txt" else int(p[2:])
        return httpx.Response(302, headers={"location": f"/r{n + 1}"})
    mock_client(monkeypatch, six)
    # a 6th redirect is unreachable (typed), not a bare RuntimeError
    with pytest.raises(AP.RobotsUnreachable) as six_hops:
        AP._default_fetch("https://www.loop.test/robots.txt")
    assert six_hops.value.reason == "too_many_redirects" and len(six_hops.value.chain) == 7
    mock_client(monkeypatch, lambda req: httpx.Response(403))
    with pytest.raises(AP.RobotsHTTPStatus) as e:
        AP._default_fetch("https://www.waf.test/robots.txt")
    assert e.value.status == 403
    assert AP.fetch_robots("https://www.waf.test/robots.txt")[0] == "none"
    mock_client(monkeypatch, lambda req: httpx.Response(429, headers={"retry-after": "1"}))
    assert AP.fetch_robots("https://www.busy2.test/robots.txt")[0] == "disallow" and env[-1] == 1.0
    mock_client(monkeypatch, lambda req: httpx.Response(404))
    assert AP.fetch_robots("https://www.none.test/robots.txt") == ("rules", None, "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------------------------------------------------
# RFC 9309 section 2.2 rule matching: wildcards, the longest match, allow on a tie, agent groups
# ---------------------------------------------------------------------------------------------------------------------
DRUPAL = """User-agent: *
Allow: /core/*.css$
Allow: /core/*.css?
Allow: /core/*.js$
Allow: /core/*.js?
Disallow: /core/
Disallow: /search?
Disallow: /*/print
Disallow:

User-agent: anyuse-compiled-workflow
User-agent: OtherBot
Disallow: /private/
Allow: /private/open$
"""


def _rules(text):
    from anyuse.execution import api_permission as P
    r = P.RobotsRules()
    r.parse(text.splitlines())
    return r, P.ROBOTS_AGENT


def test_wildcards_and_the_longest_match_decide():
    r, _a = _rules(DRUPAL.split("\n\nUser-agent: anyuse")[0])
    ok = lambda u: r.can_fetch("SomeBot", u)  # noqa: E731
    assert ok("https://x.test/core/assets/vendor/jquery/jquery.min.js?v=4.0.0")   # Allow /core/*.js? beats /core/
    assert ok("https://x.test/core/misc/drupal.js") and ok("https://x.test/core/themes/a.css")
    assert not ok("https://x.test/core/misc/drupal.js.map")                     # /core/*.js$ does not match
    assert not ok("https://x.test/core/install.php")
    assert not ok("https://x.test/news/123/print")                               # a wildcard Disallow applies
    assert not ok("https://x.test/search?q=x") and ok("https://x.test/suche?q=x")
    assert ok("https://x.test/robots.txt")


def test_the_crawlers_own_group_replaces_the_star_group_and_a_tie_allows():
    r, agent = _rules(DRUPAL)
    assert not r.can_fetch(agent, "https://x.test/private/a")
    assert r.can_fetch(agent, "https://x.test/private/open")
    assert r.can_fetch(agent, "https://x.test/core/install.php")          # its own group: /core/ is not disallowed
    tie, _a = _rules("User-agent: *\nDisallow: /a\nAllow: /a\n")
    assert tie.can_fetch("x", "https://x.test/a/b")
    empty, _a = _rules("User-agent: *\nDisallow:\n")
    assert empty.can_fetch("x", "https://x.test/anything")


def test_a_group_applies_by_product_token_never_by_substring():
    """RFC 9309 section 2.2.1: ``User-agent: flow`` names another crawler, although ``flow`` sits inside our token;
    ``AnyUse`` names the product family (a leading hyphen-delimited part), compared case-insensitively."""
    other, agent = _rules("User-agent: flow\nDisallow: /\n\nUser-agent: *\nDisallow: /private/\n")
    assert other.can_fetch(agent, "https://x.test/page")                  # the '*' group applies, not 'flow'
    assert not other.can_fetch(agent, "https://x.test/private/a")
    for value in ("work", "use", "any", "compiled-workflow", "workflow"):
        r, _a = _rules(f"User-agent: {value}\nDisallow: /\n")
        assert r.can_fetch(agent, "https://x.test/page"), value
    family, _a = _rules("User-agent: AnyUse\nDisallow: /shop/\n\nUser-agent: *\nDisallow: /\n")
    assert family.can_fetch(agent, "https://x.test/page")                  # its own group replaces '*'
    assert not family.can_fetch(agent, "https://x.test/shop/a")
    versioned, _a = _rules("User-agent: AnyUse-Compiled-Workflow/0.1\nDisallow: /a\n")
    assert not versioned.can_fetch(agent, "https://x.test/a") and versioned.can_fetch(agent, "https://x.test/b")
    from anyuse.execution import api_permission as P
    assert P.RobotsRules.product_tokens("anyuse-compiled-workflow/0.1 (replay)") == (
        "anyuse-compiled-workflow", "anyuse-compiled", "anyuse")
    assert P.RobotsRules.product_tokens("") == ()


# -------------------------------------------------------------------------------- redirects (RFC 9309 section 2.3.1.2)
PUBLIC = staticmethod(lambda host: ["93.184.216.34"])


def web(routes):
    """A fake web: url -> (status, location or text); every GET recorded in ``sent``."""
    sent = []

    def get(url):
        sent.append(url)
        status, body = routes[url]
        if 300 <= status < 400:
            return status, {"location": body}, ""
        return status, {}, body
    return get, sent


def fetch(url, get):
    return AP._default_fetch(url, get=get, resolver=lambda h: ["93.184.216.34"])


def test_a_same_domain_redirect_hop_is_followed_and_the_file_obeyed_for_the_asked_host(env):
    get, sent = web({"https://static.shop.test/robots.txt": (301, "https://www.shop.test/robots.txt"),
                     "https://www.shop.test/robots.txt": (200, "User-agent: *\nDisallow: /private/\n")})
    ok, why = AP.robots_allows("https://static.shop.test/private/a.css", fetcher=lambda u: fetch(u, get))
    assert ok is False and "redirected to https://www.shop.test/robots.txt" in why
    assert AP.robots_allows("https://static.shop.test/theme.css", fetcher=lambda u: fetch(u, get))[0] is True
    assert sent == ["https://static.shop.test/robots.txt", "https://www.shop.test/robots.txt"]
    assert AP.robots_status("www.shop.test")[0] == "rules"          # the landing host's own /robots.txt, kept for it


def test_a_cross_domain_redirect_hop_is_followed(env):
    get, sent = web({"https://app1-dsn.search-test.net/robots.txt": (302, "https://www.search-test.com/robots.txt"),
                     "https://www.search-test.com/robots.txt": (200, "User-agent: *\nAllow: /\n")})
    ok, why = AP.robots_allows("https://app1-dsn.search-test.net/1/indexes/x/query", fetcher=lambda u: fetch(u, get))
    assert ok is True and "redirected to https://www.search-test.com/robots.txt" in why
    assert sent[-1] == "https://www.search-test.com/robots.txt"


@pytest.mark.parametrize("target,word,gov", [("https://www.example.gov/robots.txt", "government host", "on"),
                                             ("https://europa.eu/robots.txt", "government host", "on"),
                                             ("https://never.example.test/robots.txt", "NEVER_CONTACT", "")])
def test_a_hop_to_a_never_contacted_host_is_unreachable_and_never_sent(env, monkeypatch, target, word, gov):
    from anyuse.execution import site_modes
    monkeypatch.setattr(site_modes, "_never_contact", lambda: {"never.example.test"})
    if gov:
        monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", gov)            # the government rule on
    else:
        monkeypatch.delenv("ANYUSE_REFUSE_GOV_HOSTS", raising=False)  # NEVER_CONTACT: refused by default
    get, sent = web({"https://cdn.shop.test/robots.txt": (301, target), target: (200, "User-agent: *\nAllow: /\n")})
    kind, text, note = AP.fetch_robots("https://cdn.shop.test/robots.txt", fetcher=lambda u: fetch(u, get))
    assert kind == "disallow" and text is None and target not in sent
    assert "redirect_to_refused_host" in note and word in note and f"-> {target}" in note
    assert "complete disallow" in note


def test_more_than_five_hops_is_unreachable(env):
    routes = {"https://www.far.test/robots.txt": (301, "https://h1.far.test/robots.txt")}
    for i in range(1, 7):
        routes[f"https://h{i}.far.test/robots.txt"] = (301, f"https://h{i + 1}.far.test/robots.txt")
    get, sent = web(routes)
    kind, _t, note = AP.fetch_robots("https://www.far.test/robots.txt", fetcher=lambda u: fetch(u, get))
    assert kind == "disallow" and "too_many_redirects" in note and len(sent) == 6        # the 6th hop is never sent
    five = dict(routes, **{"https://h5.far.test/robots.txt": (200, "User-agent: *\nAllow: /\n")})
    get5, sent5 = web(five)
    assert AP.fetch_robots("https://www.far.test/robots.txt", fetcher=lambda u: fetch(u, get5))[0] == "rules"
    assert len(sent5) == 6                                                                # 5 hops are followed


def test_a_redirect_loop_is_unreachable(env):
    get, sent = web({"https://www.loop2.test/robots.txt": (301, "https://cdn.loop2.test/robots.txt"),
                     "https://cdn.loop2.test/robots.txt": (302, "https://www.loop2.test/robots.txt")})
    ok, why = AP.robots_allows("https://www.loop2.test/a", fetcher=lambda u: fetch(u, get))
    assert ok is False and "redirect_loop" in why and len(sent) == 2
    assert "redirects: https://www.loop2.test/robots.txt -> https://cdn.loop2.test/robots.txt -> " \
           "https://www.loop2.test/robots.txt" in why


def test_the_reason_a_robots_file_was_unreadable_is_recorded(env):
    # the status code, the redirect chain, a network error: kept for the host (robots_status) and on the gate's record
    get, _sent = web({"https://app2-dsn.search-test.net/robots.txt": (301, "https://www.search-test.com/robots.txt"),
                      "https://www.search-test.com/robots.txt": (503, "")})
    AP.robots_allows("https://app2-dsn.search-test.net/q", fetcher=lambda u: fetch(u, get))
    st, note = AP.robots_status("app2-dsn.search-test.net")
    assert st == "unreadable" and "answered HTTP 503" in note and "-> https://www.search-test.com/robots.txt" in note

    def down(url):
        raise httpx.ConnectError("connection refused")
    AP.robots_allows("https://down2.search-test.net/q", fetcher=down)
    st, note = AP.robots_status("down2.search-test.net")
    assert st == "unreadable" and "connection refused" in note


def test_each_redirect_hop_counts_in_the_asked_sites_budget(env, monkeypatch):
    counted = []
    gate = HP.robots_gate()
    gate.on_fetch = lambda url, for_host="": counted.append((url, for_host)) or ""
    get, sent = web({"https://www.hopsite.test/robots.txt": (301, "https://www.hopsite-cdn.net/robots.txt"),
                     "https://www.hopsite-cdn.net/robots.txt": (200, "User-agent: *\nAllow: /\n")})
    monkeypatch.setattr(HP, "ROBOTS_FETCHER", lambda u: fetch(u, get))
    assert HP.robots_start_refusal("https://www.hopsite.test/", gate) == ""
    assert counted == [("https://www.hopsite-cdn.net/robots.txt", "www.hopsite.test")]
    # at the site's ceiling the hop is not made: unreachable, complete disallow
    AP._ROBOTS_CACHE.clear()
    gate.on_fetch = lambda url, for_host="": "site ceiling of 50 requests reached"
    get2, sent2 = web({"https://www.hopsite2.test/robots.txt": (301, "https://www.hopsite-cdn.net/robots.txt")})
    monkeypatch.setattr(HP, "ROBOTS_FETCHER", lambda u: fetch(u, get2))
    why = HP.robots_start_refusal("https://www.hopsite2.test/", gate)
    assert "redirect_not_sent" in why and "site ceiling" in why and sent2 == ["https://www.hopsite2.test/robots.txt"]



# -------------------------------------------------------------- the MCP route's robots (watch_live -> discover_bench.Robots)
class _Fetcher:
    """A fake ``HttpFetcher``: one request per ``get(follow=False)``; records (url, count_as)."""

    def __init__(self, routes):
        self.routes, self.sent = routes, []

    def get(self, url, max_hops=5, *, follow=True, count_as=""):
        from anyuse.execution.discover import Fetched
        self.sent.append((url, count_as, follow))
        status, body = self.routes[url]
        if 300 <= status < 400:
            return Fetched(url=url, final_url=url, status=status, headers={"Location": body})
        return Fetched(url=url, final_url=url, status=status, body=body)


def test_the_replay_routes_robots_match_by_rfc_9309_and_follow_redirects(env, monkeypatch):
    sys.path.insert(0, str(_HERE / "execution" / "web_runtime"))
    import discover_bench as DBN
    from anyuse.execution import request_policy as RP
    monkeypatch.setattr(RP, "destination_refusal", lambda *a, **k: "")
    # www.bright.nl's file: a blank line after "User-agent: *" - Python's parser dropped the group (everything allowed)
    f = _Fetcher({"https://www.shop-nl.test/robots.txt": (200, "User-agent: *\n\nDisallow: /zoeken\nDisallow: */aggregate\n"),
                  "https://static.shop-nl.test/robots.txt": (301, "https://www.shop-nl.test/robots.txt")})
    rb = DBN.Robots(f)
    rb.load("www.shop-nl.test")
    assert rb.allow("https://www.shop-nl.test/zoeken?q=Samsung") is False                   # the site's own rule
    assert rb.allow("https://www.shop-nl.test/p/123") is True
    assert rb.allow("https://www.shop-nl.test/x/aggregate") is False                        # a wildcard rule
    rb.load("static.shop-nl.test")                                                          # a redirected file
    assert "redirected to https://www.shop-nl.test/robots.txt" in rb.notes["static.shop-nl.test"]
    assert ("https://www.shop-nl.test/robots.txt", "static.shop-nl.test", False) in f.sent  # counted for the asked host
    assert rb.allow("https://static.shop-nl.test/zoeken") is False
    # a redirect loop: unreachable, disallow all, the chain named
    g = _Fetcher({"https://loop.shop-nl.test/robots.txt": (302, "https://www.loop-cdn.test/robots.txt"),
                  "https://www.loop-cdn.test/robots.txt": (302, "https://loop.shop-nl.test/robots.txt")})
    rb2 = DBN.Robots(g)
    assert rb2.load("loop.shop-nl.test") is None and "redirect_loop" in rb2.notes["loop.shop-nl.test"]
    assert rb2.allow("https://loop.shop-nl.test/") is False


def test_a_robots_redirect_hop_is_not_gated_as_a_page(env, monkeypatch):
    # www.heimwerker.de: its Algolia hosts redirect robots.txt to https://algolia.net/1/404; the MCP route's
    # fetcher asked its own robots gate about that hop as if it were a page and refused it - the provider stayed refused
    sys.path.insert(0, str(_HERE / "execution" / "web_runtime"))
    import discover_bench as DBN
    from anyuse.execution import discover as D
    from anyuse.execution import request_policy as RP
    monkeypatch.setattr(RP, "destination_refusal", lambda *a, **k: "")
    routes = {"https://app-dsn.search-x.net/robots.txt": (301, {"location": "https://search-x.net/1/404"}, ""),
              "https://search-x.net/1/404": (404, {}, "not found")}
    box = {}
    pacer = D.Pacer(0.0)
    pacer.sleeper = lambda s: None
    f = D.HttpFetcher(user_agent="t", allow_url=lambda u: box["rb"].allow(u), pacer=pacer,
                      client=lambda url, headers: routes[url])
    box["rb"] = rb = DBN.Robots(f)
    rb.load("app-dsn.search-x.net")
    assert rb.notes["app-dsn.search-x.net"].startswith("no robots.txt (404): allow")
    assert rb.allow("https://app-dsn.search-x.net/1/indexes/live/query")
    assert rb.allow("https://search-x.net/somewhere") is False          # only the hop itself, never a page there


def test_the_routes_record_reads_its_own_robots_notes():
    # the route's traffic record used to say "absent" (the product module's cache) for hosts the route could not read
    import types
    from anyuse.execution import watch_live as WL
    ctx = types.SimpleNamespace(robots=types.SimpleNamespace(
        notes={"a.test": "robots_unreachable: ... (RFC 9309: unreachable, complete disallow): disallow all",
               "b.test": "parsed"}, override="", overrides={}))
    tr = WL._route_traffic(ctx, {"robots_refused": {"a.test": {"requests": 2, "robots": "absent", "why": "x"},
                                                   "b.test": {"requests": 1, "robots": "rules", "why": ""},
                                                   "c.test": {"requests": 1, "robots": "absent", "why": "y"}}})
    rr = tr["robots_refused"]
    assert rr["a.test"]["robots"] == "unreadable" and "unreachable" in rr["a.test"]["why"]
    assert rr["b.test"]["robots"] == "rules" and rr["c.test"]["robots"] == "unknown"
    assert "robots_override" not in tr


# ---------------------------------------------------------------- the route uses the product's ONE robots cache
def _route_robots(routes):
    sys.path.insert(0, str(_HERE / "execution" / "web_runtime"))
    import discover_bench as DBN
    f = _Fetcher(routes)
    return DBN.Robots(f), f


def test_a_second_call_reads_robots_from_the_one_cache_within_its_ttl(env, monkeypatch):
    from anyuse.execution import request_policy as RP
    monkeypatch.setattr(RP, "destination_refusal", lambda *a, **k: "")
    routes = {"https://app-dsn.search-r.net/robots.txt": (301, "https://search-r.net/1/404"),
              "https://search-r.net/1/404": (404, "")}
    rb, f = _route_robots(routes)
    rb.load("app-dsn.search-r.net")
    assert len(f.sent) == 2 and rb.notes["app-dsn.search-r.net"].startswith("no robots.txt (404): allow (redirected")
    rb2, f2 = _route_robots(routes)                                   # the next MCP call: a new site context
    AP._ROBOTS_CACHE.clear()
    rb2.load("app-dsn.search-r.net")
    assert f2.sent == [] and rb2.cached["app-dsn.search-r.net"]      # 0 robots requests - the hop included
    assert "redirected to https://search-r.net/1/404" in rb2.notes["app-dsn.search-r.net"]
    assert rb2.allow("https://app-dsn.search-r.net/1/indexes/x/query")
    # after the TTL (24 hours at most, RFC 9309 2.4) the file is read again
    now = AP.time.time()
    monkeypatch.setattr(AP.time, "time", lambda: now + AP.ROBOTS_CACHE_MAX_S + 1)
    rb3, f3 = _route_robots(routes)
    rb3.load("app-dsn.search-r.net")
    assert len(f3.sent) == 2 and not rb3.cached["app-dsn.search-r.net"]


def test_an_unreachable_verdict_is_cached_and_read_again_after_an_hour(env, monkeypatch):
    from anyuse.execution import request_policy as RP
    monkeypatch.setattr(RP, "destination_refusal", lambda *a, **k: "")
    routes = {"https://down.search-r.net/robots.txt": (503, "")}
    rb, f = _route_robots(routes)
    assert rb.load("down.search-r.net") is None and len(f.sent) == 1
    rb2, f2 = _route_robots(routes)
    assert rb2.load("down.search-r.net") is None and f2.sent == []    # complete disallow, from the cache
    assert "answered HTTP 503" in rb2.notes["down.search-r.net"] and rb2.allow("https://down.search-r.net/") is False
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    ok, why = AP.robots_allows("https://down.search-r.net/x", fetcher=lambda u: (_ for _ in ()).throw(AssertionError(u)))
    assert ok is False and "503" in why                               # the product's gate: the same verdict, no request
    now = AP.time.time()
    monkeypatch.setattr(AP.time, "time", lambda: now + AP.ROBOTS_UNREACHABLE_TTL_S + 1)
    rb3, f3 = _route_robots(routes)
    rb3.load("down.search-r.net")
    assert len(f3.sent) == 1                                          # read again after the hour


def test_a_network_failure_is_not_cached_on_disk(env, monkeypatch):
    # a failure to reach the site at all (DNS, a refused connection, no network - possibly this machine's own) stays this
    # process's complete disallow; the site's own answer (5xx) is what the cache keeps
    def down(url):
        raise httpx.ConnectError("no route to host")
    ok, why = AP.robots_allows("https://www.nonet.test/x", fetcher=down)
    assert ok is False and "no route to host" in why
    assert AP._cache_load("www.nonet.test") is None
    AP._ROBOTS_CACHE.clear()
    AP._ROBOTS_NOTES.clear()
    assert AP.robots_allows("https://www.nonet.test/x", fetcher=lambda u: "User-agent: *\nAllow: /\n")[0] is True

