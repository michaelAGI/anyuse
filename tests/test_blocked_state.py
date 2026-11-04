"""Blocked-state detection and the hand-over to a human.

Fixtures are minimal redacted copies of PUBLIC challenge pages already
encountered (pypi.org, openalex.org) plus hand-written minimal ones for the
other vendors — no new challenges were provoked to write these tests.

Nothing here, and nothing in the module under test, attempts to bypass anything.
"""
import json
import os
import shutil
import tempfile
import time

from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.blocked import (
    CAPTCHA_GENERIC,
    CAPTCHA_HCAPTCHA,
    CAPTCHA_RECAPTCHA,
    CAPTCHA_TURNSTILE,
    CHALLENGE_INTERSTITIAL,
    COOKIE_WALL,
    ERROR_PAGE,
    FORBIDDEN,
    LOGIN_WALL,
    RATE_LIMITED,
    BlockedState,
    blocked_hil_action,
    blocked_until,
    clear_blocked,
    detect_blocked,
    detect_blocked_ui,
    mark_blocked,
    unknown_block_context,
)
from anyuse.execution.hil_gate import StepClass, classify_step

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "blocked")

HANDWRITTEN = {
    CAPTCHA_RECAPTCHA: '<html><body><div class="g-recaptcha" data-sitekey="x"></div>'
                       '<script src="https://www.google.com/recaptcha/api.js"></script></body></html>',
    CAPTCHA_HCAPTCHA: '<html><body><div class="h-captcha" data-sitekey="x"></div>'
                      '<script src="https://hcaptcha.com/1/api.js"></script></body></html>',
    CAPTCHA_TURNSTILE: '<html><body><div class="cf-turnstile"></div>'
                       '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
                       '</script></body></html>',
    LOGIN_WALL: "<html><body><h1>Members only</h1><p>Please sign in to continue.</p></body></html>",
    COOKIE_WALL: "<html><body><h2>We use cookies</h2><button>Accept all cookies</button></body></html>",
    ERROR_PAGE: "<html><body><h1>Service unavailable</h1></body></html>",
}


def _fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# vendor signatures
# ---------------------------------------------------------------------------

def test_each_vendor_challenge_is_named_exactly():
    for expected, body in HANDWRITTEN.items():
        state = detect_blocked(200, {"content-type": "text/html"}, body,
                               "https://example.test/x")
        assert state.blocked is True, expected
        assert state.reason == expected, f"{expected} detected as {state.reason}"
        assert state.evidence


def test_a_real_pypi_captcha_page_is_detected():
    fx = _fixture("pypi_org.json")
    state = detect_blocked(fx["status"], fx["headers"],
                           fx["body"] + fx.get("ui_text", ""),
                           "https://pypi.org/search/?q=httpx")
    assert state.blocked is True
    assert state.reason in (CAPTCHA_GENERIC, CAPTCHA_RECAPTCHA, CHALLENGE_INTERSTITIAL)
    assert state.host == "pypi.org"


def test_a_real_cloudflare_challenge_page_is_detected():
    fx = _fixture("openalex_org.json")
    state = detect_blocked(fx["status"], fx["headers"],
                           fx["body"] + fx.get("ui_text", ""), "https://openalex.org/")
    assert state.blocked is True
    assert state.reason in (CAPTCHA_TURNSTILE, CHALLENGE_INTERSTITIAL)


def test_a_challenge_header_alone_is_enough():
    state = detect_blocked(403, {"cf-mitigated": "challenge"}, "<html></html>",
                           "https://x.test/")
    assert state.blocked and state.reason == CHALLENGE_INTERSTITIAL


# ---------------------------------------------------------------------------
# status-based signals
# ---------------------------------------------------------------------------

def test_429_is_a_rate_limit_and_honours_retry_after():
    state = detect_blocked(429, {"Retry-After": "120"}, "slow down",
                           "https://x.test/api")
    assert state.blocked and state.reason == RATE_LIMITED
    assert state.retry_after_s == 120 and state.ttl_s == 120
    assert "120s" in state.message


def test_a_bare_403_is_forbidden_not_a_captcha():
    state = detect_blocked(403, {}, "<html><body>Forbidden</body></html>",
                           "https://x.test/api")
    assert state.blocked and state.reason == FORBIDDEN


def test_a_plain_5xx_is_an_ordinary_failure_not_a_blocked_state():
    # The ladder already falls back on a 5xx; calling it "blocked" would
    # suppress the host for hours because a server hiccuped once.
    assert detect_blocked(503, {}, "<html>oops</html>", "https://x.test/").blocked is False


def test_a_200_that_renders_an_error_page_is_an_error_page():
    state = detect_blocked(200, {}, "<html><body><h1>Service unavailable</h1></body></html>",
                           "https://x.test/")
    assert state.blocked and state.reason == ERROR_PAGE
    assert "the site's problem" in state.message


def test_only_refusals_put_a_host_on_do_not_auto_retry():
    from anyuse.execution.blocked import STICKY_REASONS
    assert ERROR_PAGE not in STICKY_REASONS and LOGIN_WALL not in STICKY_REASONS
    assert RATE_LIMITED in STICKY_REASONS and CAPTCHA_TURNSTILE in STICKY_REASONS


def test_ordinary_content_is_not_blocked():
    body = "<html><body><ul><li>alpha</li><li>beta</li><li>gamma</li></ul></body></html>"
    assert detect_blocked(200, {"content-type": "text/html"}, body,
                          "https://x.test/s?q=a").blocked is False
    assert detect_blocked(200, {}, '{"crates":[{"id":"serde"}]}',
                          "https://x.test/api").blocked is False


def test_a_consent_banner_on_a_full_page_is_not_a_wall():
    page = ("<html><body><p>We use cookies.</p>" + "<li>result row</li>" * 2000
            + "</body></html>")
    assert detect_blocked(200, {}, page, "https://x.test/").blocked is False


# ---------------------------------------------------------------------------
# a served page that MENTIONS a captcha is not a captcha page
# ---------------------------------------------------------------------------
#
# Found by measurement, not by reading: packagist.org's ordinary package page is
# 83 KB of real content and loads reCAPTCHA for a form elsewhere on the site.
# The detector called it `captcha_recaptcha`, put the host on do-not-auto-retry
# for six hours and the workflow was filed as "the site said no". hackage's
# 505 KB package page did the same via the bare word "captcha". Two measured
# workflows sat in tier 4 partly because of this.

def _served_page(marker):
    return ("<html><head><title>monolog/monolog - Packagist</title></head><body>"
            + f"<script src='https://www.google.com/recaptcha/api.js'></script>{marker}"
            + "<li>a real result row</li>" * 3000 + "</body></html>")


def test_a_big_served_page_that_mentions_recaptcha_is_not_blocked():
    state = detect_blocked(200, {"content-type": "text/html"},
                           _served_page("grecaptcha.execute()"),
                           "https://packagist.org/packages/monolog/monolog")
    assert state.blocked is False
    assert any("furniture" in e for e in state.evidence), state.evidence


def test_a_big_served_page_that_merely_says_captcha_is_not_blocked():
    page = "<html><body>" + "<p>this package solves a captcha</p>" * 3000 + "</body></html>"
    state = detect_blocked(200, {}, page, "https://hackage.haskell.org/package/x")
    assert state.blocked is False
    assert any("the word is not the situation" in e for e in state.evidence)


def test_a_short_captcha_page_is_still_blocked():
    page = ("<html><head><title>Just a moment...</title></head><body>"
            "<div class='g-recaptcha'></div></body></html>")
    state = detect_blocked(200, {}, page, "https://x.test/s")
    assert state.blocked is True
    assert state.reason == CAPTCHA_RECAPTCHA


def test_a_non_2xx_with_a_captcha_marker_is_still_blocked():
    page = "<html><body>" + "<p>filler</p>" * 5000 + "<div class='h-captcha'></div></body></html>"
    state = detect_blocked(403, {}, page, "https://x.test/s")
    assert state.blocked is True


def test_a_big_page_whose_TITLE_is_the_challenge_is_still_blocked():
    page = ("<html><head><title>Attention Required! | Cloudflare</title></head><body>"
            + "<p>filler</p>" * 5000 + "<div class='cf-turnstile'></div></body></html>")
    state = detect_blocked(200, {}, page, "https://x.test/s")
    assert state.blocked is True
    assert state.reason == CAPTCHA_TURNSTILE


def test_a_challenge_url_marker_beats_the_size_guard():
    page = "<html><body>" + "<p>filler</p>" * 5000 + "<p>grecaptcha</p></body></html>"
    state = detect_blocked(200, {}, page,
                           "https://x.test/s?__cf_chl_tk=abc")
    assert state.blocked is True


# ---------------------------------------------------------------------------
# the UI path
# ---------------------------------------------------------------------------

def test_the_ui_path_detects_a_challenge_from_the_rendered_text():
    state = detect_blocked_ui("Performing security verification\nRay ID: abc",
                              url="https://x.test/")
    assert state.blocked and state.reason == CHALLENGE_INTERSTITIAL


def test_the_ui_path_detects_a_challenge_from_the_request_log_alone():
    state = detect_blocked_ui("Just a moment", url="https://x.test/", requests=[
        {"url": "https://x.test/cdn-cgi/challenge-platform/h/g/fo/1", "status": 200}])
    assert state.blocked and state.reason == CAPTCHA_TURNSTILE


# ---------------------------------------------------------------------------
# messages never suggest a bypass
# ---------------------------------------------------------------------------

def test_every_message_hands_over_to_the_human_and_offers_no_workaround():
    banned = ("bypass", "solve the captcha", "work around", "circumvent", "retry until")
    for reason in (CAPTCHA_RECAPTCHA, CAPTCHA_HCAPTCHA, CAPTCHA_TURNSTILE,
                   CHALLENGE_INTERSTITIAL, RATE_LIMITED, LOGIN_WALL, COOKIE_WALL,
                   FORBIDDEN, ERROR_PAGE):
        msg = BlockedState(blocked=True, reason=reason, host="x.test").message
        assert msg
        low = msg.lower()
        assert not any(b in low for b in banned), (reason, msg)
    assert BlockedState().message == ""


# ---------------------------------------------------------------------------
# the HIL pause
# ---------------------------------------------------------------------------

def test_a_blocked_state_is_held_by_the_existing_hil_gate():
    state = detect_blocked(200, {}, HANDWRITTEN[CAPTCHA_RECAPTCHA], "https://x.test/")
    action = blocked_hil_action(state)
    assert classify_step(action) == StepClass.DESTRUCTIVE, "must be held, not waved through"
    assert action["args"]["host"] == "x.test"


# ---------------------------------------------------------------------------
# do-not-auto-retry registry
# ---------------------------------------------------------------------------

def test_a_blocked_host_is_remembered_with_a_ttl_and_expires():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "blocked.json")
        state = BlockedState(blocked=True, reason=RATE_LIMITED, host="x.test", ttl_s=900)
        entry = mark_blocked(state, path)
        assert entry["reason"] == RATE_LIMITED
        assert blocked_until("x.test", path) is not None
        assert blocked_until("other.test", path) is None
        assert blocked_until("x.test", path, now=time.time() + 100000) is None
        assert clear_blocked("x.test", path) is True
        assert blocked_until("x.test", path) is None
        assert clear_blocked("x.test", path) is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# executor integration
# ---------------------------------------------------------------------------

class Transport:
    def __init__(self, status, body, headers=None):
        self.status, self.body = status, body
        self.headers = headers or {"Content-Type": "text/html"}
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        return ApiResponse(self.status, self.headers, self.body)


def _tmpl():
    return RequestTemplate(url="https://blocked.test/api", query={"q": "{q}"},
                           params=["q"], extract={"items": "a[*].b"},
                           expect={"status_in": [200]})


def test_the_executor_reports_a_challenge_as_blocked_not_as_a_shape_change():
    tmp = tempfile.mkdtemp()
    try:
        os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(tmp, "b.json")
        tr = Transport(200, HANDWRITTEN[CAPTCHA_HCAPTCHA])
        res = ApiStepExecutor(transport=tr).execute(_tmpl(), {"q": "x"})
        assert res["success"] is False
        assert res["requires_human"] is True
        assert res["blocked_state"]["reason"] == CAPTCHA_HCAPTCHA
        assert res["blocked_state"]["action_required"] == "human"
        assert "postcondition" not in (res.get("error") or "")
        # and the host is now on do-not-auto-retry
        assert blocked_until("blocked.test", os.environ["ANYUSE_BLOCKED_HOSTS_FILE"])
    finally:
        os.environ.pop("ANYUSE_BLOCKED_HOSTS_FILE", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_host_on_do_not_auto_retry_is_refused_before_a_request_is_sent():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "b.json")
        os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = path
        mark_blocked(BlockedState(blocked=True, reason=RATE_LIMITED,
                                  host="blocked.test", ttl_s=900), path)
        tr = Transport(200, "{}")
        res = ApiStepExecutor(transport=tr).execute(_tmpl(), {"q": "x"})
        assert res["success"] is False and res["requires_human"] is True
        assert tr.sent == [], "a blocked host must not be knocked on again"
    finally:
        os.environ.pop("ANYUSE_BLOCKED_HOSTS_FILE", None)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# the typed-judge hook (documented, not wired)
# ---------------------------------------------------------------------------

def test_the_unknown_block_hook_describes_shape_only_and_is_not_wired():
    ctx = unknown_block_context(
        200, {"Server": "x", "Retry-After": "5", "Set-Cookie": "secret=abc"},
        '<html><iframe src="https://vendor.test/c"></iframe><form></form>'
        '<script src="https://vendor.test/a.js"></script>body text</html>',
        "https://x.test/p")
    assert ctx["iframe_count"] == 1 and ctx["form_count"] == 1
    assert ctx["script_hosts"] == ["vendor.test"]
    assert "set-cookie" not in ctx["headers_seen"], "no credential-bearing headers"
    assert "abc" not in json.dumps(ctx), "no header VALUES may leak"
    # It must be defined and never CALLED: no caller anywhere in the package.
    import glob as _glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    callers = []
    for path in _glob.glob(os.path.join(root, "*", "*.py")):
        if path.endswith("blocked.py") or os.sep + "tests" + os.sep in path:
            continue
        if "unknown_block_context(" in open(path).read():
            callers.append(os.path.basename(path))
    assert callers == [], f"the hook must stay unwired, called from {callers}"


def test_a_429_from_a_third_party_endpoint_does_not_block_a_served_page():
    # the page rendered (a title, 200 KB of results); its
    # CSP reports went to report-uri.com, which answered 429. That is not the
    # site refusing us.
    text = "DailyMed - Search Results for aspirin\n" + "aspirin tablet 81 mg\n" * 200
    reqs = [{"url": "https://dailymed.example/dailymed/search.cfm?query=aspirin", "status": 200},
            {"url": "https://nlmoccs.report-uri.example/r/d/csp/reportOnly", "status": 429},
            {"url": "https://nlmoccs.report-uri.example/r/d/csp/reportOnly", "status": 429}]
    st = detect_blocked_ui(text, "", "https://dailymed.example/dailymed/search.cfm?query=aspirin",
                           reqs, items=[])
    assert not st.blocked, st.evidence


def test_a_429_from_the_pages_own_host_still_blocks():
    text = "Search\n"
    reqs = [{"url": "https://dailymed.example/dailymed/search.cfm?query=aspirin", "status": 200},
            {"url": "https://dailymed.example/dailymed/xhr?query=aspirin", "status": 429}]
    st = detect_blocked_ui(text, "", "https://dailymed.example/dailymed/search.cfm?query=aspirin",
                           reqs, items=[])
    assert st.blocked and st.reason == RATE_LIMITED, (st.reason, st.evidence)


def test_a_consent_banner_over_a_served_results_page_is_not_a_wall():
    # "Your title search for housing ... has returned more
    # than 200 results" in 12 KB of visible text, plus a cookie-preferences
    # banner, was filed as a cookie wall.
    text = ("We use cookies. Change your cookie preferences\n"
            "Search Results\nYour title search for housing has returned more than 200 "
            "results.\n" + "Housing Act 2004 (c. 34)\n" * 300)
    assert len(text) > 4_000
    st = detect_blocked_ui(text, "", "https://legislation.example/all?title=housing", [],
                           items=[])
    assert not st.blocked, st.evidence
    assert "furniture" in " ".join(st.evidence)


def test_a_document_that_is_nothing_but_the_banner_is_still_a_wall():
    text = ("This site uses cookies. Accept all cookies or manage your cookie "
            "preferences to continue.")
    st = detect_blocked_ui(text, "", "https://snap.example/search?q=x", [], items=[])
    assert st.blocked and st.reason == COOKIE_WALL, (st.reason, st.evidence)


def test_a_beacon_on_the_pages_own_host_is_not_evidence_either():
    # The block decision must look only at the
    # site's own document/API answers, never at a report or beacon request,
    # whichever host it went to.
    text = "Results for aspirin\n" + "aspirin tablet\n" * 100
    reqs = [{"url": "https://site.example/search?q=aspirin", "status": 200,
             "resource_type": "Document"},
            {"url": "https://site.example/csp-report", "status": 429,
             "resource_type": "CSPViolationReport"},
            {"url": "https://site.example/api/collect", "status": 429}]
    st = detect_blocked_ui(text, "", "https://site.example/search?q=aspirin", reqs, items=[])
    assert not st.blocked, st.evidence
    from anyuse.execution.blocked import is_side_request
    assert is_side_request("https://x.example/r/d/csp/reportOnly")
    assert is_side_request("https://x.example/anything", "Ping")
    assert not is_side_request("https://x.example/api/search?q=a", "XHR")


def test_challenge_served_instead_reads_the_situation_not_the_word():
    from anyuse.execution.blocked import challenge_served_instead
    # served result list that mentions recaptcha, with vendor/CDN scripts
    assert challenge_served_instead(
        "vibe_recaptcha\timplementation of google's recaptcha service",
        "https://x.test/search?q=vibe",
        ["https://www.google.com/recaptcha/api.js",
         "https://x.test/cdn-cgi/challenge-platform/scripts/jsd/main.js"],
        ["vibe_recaptcha", "vibe-core"]) == ""
    # real interstitials
    assert challenge_served_instead("Performing security verification", "https://x.test/",
                                    ["https://x.test/cdn-cgi/challenge-platform/h/g"], [])
    assert challenge_served_instead("Verify you are human", "https://x.test/", [], [])
    assert challenge_served_instead("", "https://x.test/?__cf_chl_tk=abc", [], ["a"])


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
