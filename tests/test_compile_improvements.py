"""Milestone-4 improvements, each pinned by the behaviour it claims.

Three things measured before and after, and kept only because they helped:

1. **Response-shape tolerance.** A compiled rung must survive an endpoint that
   ADDS a field and must fail when one it actually compares is REMOVED. This is
   the difference between "brittle" and "strict", and the two are easy to
   confuse. Documented and pinned rather than assumed.
2. **A persistent robots cache.** Compiling n workflows on one host used to
   fetch robots.txt n times; now it fetches once per host per day. Fewer
   requests to somebody else's server is the point.
3. **Actionable failure messages.** Every compile failure a user can act on
   must say what to do, not just that it failed.

Offline: no network, no desktop, no keychain.
"""
import json
import os
import tempfile

from anyuse.execution.api_compile import compile_recording, visible_items
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
from anyuse.execution.api_verify import verify_template

ITEMS = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"]


def _rec(body, url="https://ex.test/api/search?q=alpha"):
    return {"param": {"name": "q", "value": "alpha"},
            "ui": {"items": ITEMS, "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200,
                         "mime": "application/json", "bytes": len(body), "body": body}]}


def _list_body(names, extra=None, key="name"):
    items = [{key: n, "version": "1.0"} for n in names]
    if extra:
        for it in items:
            it.update(extra)
    payload = {"results": items}
    if extra:
        payload["meta"] = {"generated_at": "2026-09-21T00:00:00Z"}
    return json.dumps(payload)


class Transport:
    def __init__(self, body):
        self.body = body

    def send(self, method, url, headers, body, timeout_s):
        return ApiResponse(200, {"Content-Type": "application/json"}, self.body)


def _compiled():
    res = compile_recording(_rec(_list_body(ITEMS)))
    assert res.template is not None, res.reason
    return res.template


def _cases(items=ITEMS):
    return [{"value": "fresh one", "ui_items": items},
            {"value": "fresh two", "ui_items": items}]


# ---------------------------------------------------------------------------
# 1. response-shape tolerance
# ---------------------------------------------------------------------------

def test_an_added_field_does_not_break_a_proof():
    """An endpoint that starts returning `downloads` and `license` alongside the
    fields we read is doing the ordinary thing. Breaking on that would make
    every compiled rung a liability."""
    tmpl = _compiled()
    widened = _list_body(ITEMS, extra={"downloads": 91234, "license": "MIT",
                                       "keywords": ["a", "b"]})
    res = verify_template(tmpl, _cases(), ApiStepExecutor(transport=Transport(widened)))
    assert res.passed, res.reason


def test_a_new_top_level_key_does_not_break_a_proof():
    tmpl = _compiled()
    payload = json.loads(_list_body(ITEMS))
    payload["pagination"] = {"page": 1, "total": 99}
    payload["warnings"] = []
    res = verify_template(tmpl, _cases(),
                          ApiStepExecutor(transport=Transport(json.dumps(payload))))
    assert res.passed, res.reason


def test_a_REMOVED_compared_field_DOES_break_the_proof():
    """The other half, and the one that matters: if the field the extraction
    reads disappears, the rung must fail loudly and fall back — not return an
    empty list that a caller mistakes for 'no results'."""
    tmpl = _compiled()
    renamed = _list_body(ITEMS, key="title")        # `name` is gone
    res = verify_template(tmpl, _cases(),
                          ApiStepExecutor(transport=Transport(renamed)))
    assert res.passed is False
    assert "postcondition failed" in res.cases[0].reason or res.cases[0].coverage < 0.8


def test_a_shorter_list_that_no_longer_covers_the_ui_fails():
    tmpl = _compiled()
    truncated = _list_body(ITEMS[:1])
    res = verify_template(tmpl, _cases(), ApiStepExecutor(transport=Transport(truncated)))
    assert res.passed is False, res.reason


def test_a_reordered_list_fails_because_order_is_part_of_the_claim():
    tmpl = _compiled()
    flipped = _list_body(list(reversed(ITEMS)))
    res = verify_template(tmpl, _cases(), ApiStepExecutor(transport=Transport(flipped)))
    assert res.passed is False
    assert "order" in res.reason, res.reason


def test_the_two_halves_together_are_the_property_we_claim():
    """Stated once, as one assertion, because it is the actual design rule:
    tolerant to addition, strict about what it compares."""
    tmpl = _compiled()
    add = ApiStepExecutor(transport=Transport(_list_body(ITEMS, extra={"x": 1})))
    remove = ApiStepExecutor(transport=Transport(_list_body(ITEMS, key="title")))
    assert verify_template(tmpl, _cases(), add).passed is True
    assert verify_template(tmpl, _cases(), remove).passed is False


# ---------------------------------------------------------------------------
# 2. the persistent robots cache
# ---------------------------------------------------------------------------

def test_robots_is_fetched_once_per_host_across_processes():
    from anyuse.execution import api_permission as ap

    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("ANYUSE_ROBOTS_CACHE_FILE")
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(tmp, "robots.json")
        ap._ROBOTS_CACHE.clear()
        try:
            fetches = []

            def fetch(url):
                fetches.append(url)
                return "User-agent: *\nDisallow: /private\n"

            assert ap.robots_allows("https://ex.test/a", fetch)[0] is True
            ap._ROBOTS_CACHE.clear()                     # a NEW process
            assert ap.robots_allows("https://ex.test/private", fetch)[0] is False
            ap._ROBOTS_CACHE.clear()
            assert ap.robots_allows("https://ex.test/b", fetch)[0] is True
            assert len(fetches) == 1, fetches            # BEFORE: 3, AFTER: 1
        finally:
            ap._ROBOTS_CACHE.clear()
            if old is None:
                os.environ.pop("ANYUSE_ROBOTS_CACHE_FILE", None)
            else:
                os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = old


def test_a_missing_robots_file_is_cached_too():
    """A 404 costs the site a request just like a hit does."""
    from anyuse.execution import api_permission as ap

    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("ANYUSE_ROBOTS_CACHE_FILE")
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(tmp, "robots.json")
        ap._ROBOTS_CACHE.clear()
        try:
            fetches = []

            def fetch(url):
                fetches.append(url)
                return None                              # no robots.txt

            allowed, note = ap.robots_allows("https://none.test/x", fetch)
            assert allowed is None and "no robots.txt" in note
            ap._ROBOTS_CACHE.clear()
            allowed, note = ap.robots_allows("https://none.test/y", fetch)
            assert allowed is None
            assert len(fetches) == 1, fetches
        finally:
            ap._ROBOTS_CACHE.clear()
            if old is None:
                os.environ.pop("ANYUSE_ROBOTS_CACHE_FILE", None)
            else:
                os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = old


def test_the_cache_can_be_switched_off():
    from anyuse.execution import api_permission as ap

    with tempfile.TemporaryDirectory() as tmp:
        saved = {k: os.environ.get(k) for k in
                 ("ANYUSE_ROBOTS_CACHE_FILE", "ANYUSE_ROBOTS_TTL_S")}
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = os.path.join(tmp, "robots.json")
        os.environ["ANYUSE_ROBOTS_TTL_S"] = "0"
        ap._ROBOTS_CACHE.clear()
        try:
            fetches = []

            def fetch(url):
                fetches.append(url)
                return "User-agent: *\nAllow: /\n"

            ap.robots_allows("https://ex.test/a", fetch)
            ap._ROBOTS_CACHE.clear()
            ap.robots_allows("https://ex.test/b", fetch)
            assert len(fetches) == 2, "TTL 0 must disable the on-disk cache"
            assert not os.path.exists(os.environ["ANYUSE_ROBOTS_CACHE_FILE"])
        finally:
            ap._ROBOTS_CACHE.clear()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_a_stale_entry_is_refetched():
    from anyuse.execution import api_permission as ap

    with tempfile.TemporaryDirectory() as tmp:
        saved = {k: os.environ.get(k) for k in
                 ("ANYUSE_ROBOTS_CACHE_FILE", "ANYUSE_ROBOTS_TTL_S")}
        path = os.path.join(tmp, "robots.json")
        os.environ["ANYUSE_ROBOTS_CACHE_FILE"] = path
        os.environ["ANYUSE_ROBOTS_TTL_S"] = "3600"
        ap._ROBOTS_CACHE.clear()
        try:
            with open(path, "w") as fh:
                json.dump({"ex.test": {"at": 0, "body": "User-agent: *\nDisallow: /\n"}}, fh)
            fetches = []

            def fetch(url):
                fetches.append(url)
                return "User-agent: *\nAllow: /\n"

            allowed, _ = ap.robots_allows("https://ex.test/a", fetch)
            assert allowed is True, "a 1970 entry must not be trusted"
            assert len(fetches) == 1
        finally:
            ap._ROBOTS_CACHE.clear()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ---------------------------------------------------------------------------
# 3. failure messages a user can act on
# ---------------------------------------------------------------------------

def test_every_compile_failure_says_something_actionable():
    cases = [
        ("empty screen", {"param": {"name": "q", "value": "x"},
                          "ui": {"items": [], "text": ""}, "network": []},
         ["no result list"]),
        ("unusable recording",
         {"param": {"name": "q", "value": "x"}, "ui": {"items": [], "text": ""},
          "network": [], "recorder_warning": "the result selector never matched"},
         ["unusable recording"]),
        ("nothing explains the screen",
         _rec(json.dumps({"unrelated": {"config": "dark"}})),
         ["no request explains the screen", "coverage"]),
    ]
    for label, rec, needles in cases:
        res = compile_recording(rec)
        assert res.template is None, label
        assert res.reason, f"{label}: a failure with no reason is not actionable"
        assert any(n in res.reason for n in needles), (label, res.reason)


def test_a_parameter_that_is_not_in_the_url_says_exactly_that():
    body = json.dumps({"results": [{"name": n} for n in ITEMS]})
    rec = {"param": {"name": "q", "value": "alpha"},
           "ui": {"items": ITEMS, "text": ""},
           "network": [{"url": "https://ex.test/api/all", "method": "GET", "status": 200,
                        "mime": "application/json", "bytes": len(body), "body": body}]}
    res = compile_recording(rec)
    assert res.template is None
    assert "not carried by" in res.reason, res.reason


def test_a_vanished_embedded_payload_names_the_payload():
    from anyuse.execution.api_compile import compile_embedded_json

    page = ('<html><body><ul><li>Alpha Pkg</li></ul>'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"r": [{"n": n} for n in ITEMS]}) + '</script></body></html>')
    rec = {"param": {"name": "q", "value": "alpha"}, "ui": {"items": ITEMS, "text": ""},
           "network": [{"url": "https://ex.test/s?q=alpha", "method": "GET", "status": 200,
                        "mime": "text/html", "bytes": len(page), "body": page}]}
    tmpl, why = compile_embedded_json(rec, visible_items(rec), "q", "alpha")
    assert tmpl is not None, why

    class Gone:
        def send(self, *a, **k):
            return ApiResponse(200, {"Content-Type": "text/html"}, "<html></html>")

    res = ApiStepExecutor(transport=Gone()).execute(tmpl, {"q": "x"})
    assert res["success"] is False
    assert "__NEXT_DATA__" in res["error"], res["error"]


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
