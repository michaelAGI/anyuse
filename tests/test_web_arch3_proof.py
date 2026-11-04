"""The proof finds script-rendered result lists, and its values are ITEM-level
terms. Offline (fake CDP socket, fake fetcher, fake replays).

Measured: no proof passed on 22 sites: "no result list derivable" (scripts never
entered the static cache - Set-Cookie / no lifetime - and once the static share
was spent later runs and the proof rendered without them), 74 runs started on a
page the byte ceiling had already killed, and the proof values were category
names that result items rarely name.

What must hold:

1. the SITE static cache keeps a script or stylesheet that sets a cookie, says
   no-cache or has no lifetime (its Set-Cookie never stored), and still refuses
   no-store, Vary: *, a document, a non-GET and anything carrying an input; off
   by default;
2. statics stop at the byte headroom (skipped, not counted, host not stopped;
   a cached copy is still served); a document still goes;
3. the runs cannot spend the proof's reserve (requests and bytes) until the
   proof ran; a run is never started below the byte floor;
4. the proof: tier 1 = the HTTP GET of the result URL template (compile +
   fitting derive the list, two held-out values validate it); when HTTP cannot
   reproduce the list, tier 2 = the browser replay with a full render on the
   same values (the recordings carry the page before the search and the text);
(the item-level values: test_web_arch3_values.py)
"""
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))

from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from test_static_share import H, budget, paused, run_script  # noqa: E402


def test_the_site_cache_keeps_statics_that_give_no_lifetime():
    _p, b = budget()
    b.cache_enabled = True
    url = f"https://{H}/app.js"
    hdr = {"set-cookie": "sid=1", "content-type": "application/javascript"}
    assert not b.remember(url, 200, hdr, "x=1", rtype="Script")           # off: refused (a cookie)
    b.site_cache = True
    assert b.remember(url, 200, hdr, "x=1", rtype="Script")
    ent = b.lookup("GET", url)
    assert ent and not any(h["name"].lower() == "set-cookie" for h in ent["headers"])
    assert b.remember(f"https://{H}/b.js", 200, {}, "y", rtype="Script")              # no lifetime
    assert b.remember(f"https://{H}/c.css", 200, {"cache-control": "no-cache"}, "z", rtype="Stylesheet")
    assert not b.remember(f"https://{H}/d.js", 200, {"cache-control": "no-store"}, "w", rtype="Script")
    assert not b.remember(f"https://{H}/e.js", 200, {"vary": "*"}, "w", rtype="Script")
    assert not b.remember(f"https://{H}/", 200, {}, "<html>", rtype="Document")
    assert not b.remember(f"https://{H}/f.js", 200, {}, "w", rtype="Script", method="POST")
    b.protect(["merino"])
    assert not b.remember(f"https://{H}/s.js?q=merino", 200, {}, "w", rtype="Script")
    assert b.site_cache_overrides["sets a cookie"] == 1 and b.site_cache_overrides["no freshness information"] == 1
    assert G.SITE_CACHE_TTL_S >= 3600


class ByteHeadroom(unittest.TestCase):
    def test_statics_stop_at_the_headroom_documents_go(self):
        pacer, b = budget(static_bytes_cap=1000)
        b.static_bytes_headroom = 0.85
        b.site_bytes[H] = 900
        s = run_script(b, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/a.js", "Script")])
        self.assertEqual(s._ws.answered("Fetch.continueRequest"), ["f0"])
        self.assertEqual(s._ws.answered("Fetch.failRequest"), ["f1"])
        self.assertNotIn(H, b.stopped)
        self.assertTrue(any("byte headroom" in w for _u, w in s.guard.failed))

    def test_a_cached_static_is_still_served_over_the_headroom(self):
        pacer, b = budget(static_bytes_cap=1000)
        b.static_bytes_headroom = 0.85
        b.cache_enabled = b.site_cache = True
        self.assertTrue(b.remember(f"https://{H}/a.js", 200, {}, "x", rtype="Script"))
        b.site_bytes[H] = 900
        s = run_script(b, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/a.js", "Script")])
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), ["f1"])


    def test_a_cache_answer_books_no_bytes(self):
        pacer, b = budget(static_bytes_cap=10 ** 7)
        b.cache_enabled = b.site_cache = True
        self.assertTrue(b.remember(f"https://{H}/a.js", 200, {}, "x" * 5000, rtype="Script"))

        def net(i, url, n):
            return [{"method": "Network.requestWillBeSent", "params": {"requestId": f"n{i}", "request": {"url": url}}},
                    {"method": "Network.loadingFinished", "params": {"requestId": f"n{i}", "encodedDataLength": n}}]
        s = run_script(b, [paused(0, f"https://{H}/", "Document"), paused(1, f"https://{H}/a.js", "Script")]
                       + net(0, f"https://{H}/", 700) + net(1, f"https://{H}/a.js", 5000))
        self.assertEqual(s._ws.answered("Fetch.fulfillRequest"), ["f1"])
        self.assertEqual(b.site_bytes.get(H), 700)


def test_the_runs_leave_the_proof_reserve():
    import web_arch3_live as W3

    class Ctx:
        def left(self):
            return 60

        def bytes_left(self):
            return 5 * 1024 * 1024
    req, nb = W3.run_left(Ctx(), "arch", True)
    assert req == 60 - W3.PROOF_RESERVE["arch"] and nb == 5 * 1024 * 1024 - W3.PROOF_BYTES["arch"]
    assert W3.run_left(Ctx(), "arch", False) == (60, 5 * 1024 * 1024)
    assert W3.MIN_RUN_BYTES >= 1024 * 1024


class FakeGet:
    def __init__(self, status, body):
        self.status, self.body, self.error = status, body, ""


def results_page(q, n=4, rendered=True):
    items = "".join(f'<li class="hit"><a href="/p/{q}-{i}">{q.title()} Thing {i}</a></li>' for i in range(n))
    return (f"<html><body><h1>Results for {q}</h1><ul class='results'>{items if rendered else ''}</ul>"
            f"<footer><a href='/about'>About</a></footer></body></html>")


class FakeCtx:
    site = "www.shop.example"

    def __init__(self, rendered=True):
        self.rendered = rendered
        self.urls = []
        self.n = 100

        class Budget:
            def add_bytes(_s, host, n):
                pass
        self.budget = Budget()
        ctx = self

        class Fetch:
            def get(_s, url):
                ctx.urls.append(url)
                ctx.n -= 1
                q = url.split("q=")[1]
                return FakeGet(200, results_page(q, rendered=ctx.rendered))
        self.fetch = Fetch()

    def allowed(self, u):
        return True

    def left(self):
        return self.n


def test_tier1_proves_over_http_and_fails_cleanly_when_the_list_is_script_rendered():
    import tempfile
    import web_arch3_live as W3
    d = tempfile.mkdtemp()
    ctx = FakeCtx()
    t1 = W3.tier1(ctx, "https://www.shop.example/search?q={q}", "merino", "alpaca", ["scarf", "apron"], d, "T")
    assert t1["status"] == "proved", t1
    assert [c["ok"] for c in t1["cases"]] == [True, True] and len(ctx.urls) == 4
    ctx2 = FakeCtx(rendered=False)
    t1 = W3.tier1(ctx2, "https://www.shop.example/search?q={q}", "merino", "alpaca", ["scarf", "apron"], d, "T")
    assert t1["status"] == "no_list" and len(ctx2.urls) == 2          # never spends the validation values
    assert W3.tier1(ctx2, "", "a", "b", ["c", "d"], d, "T")["status"] == "no_template"


def test_prove_goes_to_the_full_render_when_http_cannot_reproduce_the_list():
    import tempfile
    import web_arch3_live as W3
    from anyuse.execution import web_arch_run as RR
    calls = []

    async def fake_replay(page, ops, settle=None, by_rule=False, result_wait_s=0.0):
        q = next(o["value"] for o in ops if o.get("op") == "search")
        calls.append((q, result_wait_s))
        dom = results_page(q)
        return {"doms": {"after_search": dom, "open": "<html><body><main></main></body></html>"},
                "search_url": f"https://www.shop.example/search?q={q}", "stop": "done",
                "final": {"text": f"Results for {q} " + " ".join(f"{q} thing {i}" for i in range(4)), "url": ""},
                "pages": [], "steps": []}

    class FakeSession:
        def __init__(self, *a, **k):
            self.fresh_check = {"fresh": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    old = (RR.replay, W3.session_class, W3.settle)
    try:
        RR.replay = fake_replay
        W3.session_class = lambda: FakeSession
        W3.settle = lambda: None

        class Arm:
            arm, endpoint = "arch", "http://127.0.0.1:1"
        prog = {"ops": [{"op": "open", "url": "https://www.shop.example/"},
                        {"op": "search", "value": "merino", "input": {}, "url_after": "https://www.shop.example/#"}]}
        rec = {"task": "T", "run": 1, "program": prog, "doms": {}}
        ctx = FakeCtx(rendered=False)
        res = W3.prove(Arm(), ctx, rec, ["alpaca", "scarf", "apron", "spare"], tempfile.mkdtemp())
        assert res["tier1"].startswith("failed") and res["status"] == "proved" and res["tier"] == "tier2", res
        assert all(w == W3.PROOF_RENDER_WAIT_S for _q, w in calls) and [q for q, _w in calls][0] == "merino"
    finally:
        RR.replay, W3.session_class, W3.settle = old


def test_the_result_url_template_comes_from_the_run_and_makes_a_url_tool():
    import web_arch3_live as W3
    prog = {"start_url": "https://toom.example/",
            "ops": [{"op": "open", "url": "https://toom.example/"}, {"op": "search", "value": "Hochbeet", "input": {}}]}
    rec = {"pages": [{"url": "https://toom.example/"}], "final": {"url": "https://toom.example/s/hochbeet"}}
    t, how = W3.search_url_of(prog, rec)
    assert t == "https://toom.example/s/{q}" and how == "the result URL", (t, how)
    assert W3.search_url_of(prog, {"final": {"url": "https://elsewhere.example/s/hochbeet"}})[0] == ""
    tool = W3.url_tool(t)
    assert tool["ops"] == [{"op": "search", "via": "url", "template": t, "value": "{q}"}]
    from anyuse.execution import web_arch as W
    assert W.bind(tool["ops"], "Zement")[0]["value"] == "Zement"
    via = {"ops": [{"op": "search", "via": "url", "template": "https://toom.example/?q={q}", "value": "x"}]}
    assert W3.search_url_of(via)[0] == "https://toom.example/?q={q}"


def test_compounds_name_the_value_and_http_bytes_are_the_wire_bytes():
    import gzip
    import web_arch3_live as W3
    from anyuse.execution import web_arch as W
    items = ["toom Zementmörtel 25 kg", "Putzmörtel 25 kg", "Reparaturmörtel 10 kg"]
    assert W.list_contract(items, "Mörtel")[0] and not W.list_contract(items, "Kalk")[0]
    assert W.names_all_compound("Estrichbeton 25 kg", "Beton") and not W.names_all_compound("Rabatt", "Bat")
    body = "x" * 10000

    class G:
        def __init__(self, h):
            self.headers, self.body = h, body
    assert W3.wire_bytes(G({"Content-Length": "123"})) == 123
    assert W3.wire_bytes(G({"Content-Encoding": "gzip"})) == len(gzip.compress(body.encode(), 6)) < 10000
    assert W3.wire_bytes(G({})) == 10000


if __name__ == "__main__":
    import inspect
    import traceback
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn()
            p += 1
            print("OK  ", n)
        except Exception as e:                            # noqa: BLE001
            f += 1
            print("FAIL", n, "::", repr(e)[:400])
            traceback.print_exc(limit=6)
    r = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(ByteHeadroom))
    f += len(r.failures) + len(r.errors)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
