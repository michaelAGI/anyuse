"""The web_arch2_live bench, offline: fakes only, no browser, no
network, no model.

Task design: the value pool is the site's own item and category names, never interface words
or files; the screen's search check (a GET form's result URL, a POST form's action, the usual result
paths of a script search) under the site's robots.txt; the site's tasks run search first, gates last.
Fresh start: every task runs RUNS = 3 times, each in a session class that opens a NEW
browser context. The proof path: the first successful search run feeds the proof right after the
search task's runs, before the other tasks spend the share; the compile value plus 3 pool values (the
compile value never twice). Also: the arm shares fit the approved 300; the deferred reader.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import web_arch2_live as L2  # noqa: E402

HOME = """<html><head><title>Woolly Shop</title></head><body>
<nav><a href="/">Home</a><a href="/collections/rain-jackets">Rain Jackets</a><a href="/collections/teapots">Teapots</a>
<a href="/sale">Sale</a><a href="/gift-cards">Gift Cards</a><a href="/account/login">Sign in</a>
<a href="/pages/contact">Contact us</a><a href="/cart">Cart (0)</a></nav>
<main><a href="/products/merino-crew-sock">Merino Crew Sock</a><a href="/img/merino.jpg">Merino Crew Sock photo</a>
<a href="/products/wool-beanie">Wool Beanie</a><a href="https://other.example/x">Partner Brand</a>
<a href="/blog/how-to-choose-a-jacket-for-every-season-and-every-weather">How to choose a jacket for every season</a>
<a href="/products/2024">2024</a><a href="/collections/down-jackets">Down Jackets</a></main>
<form action="/search" method="get"><input type="search" name="q" placeholder="Search"></form>
</body></html>"""


def test_addendum1_interface_words_of_more_languages_and_the_sites_own_name_are_not_values():
    html = ("<html><body>" + "".join(f'<a href="/x/{i}">{t}</a>' for i, t in enumerate(
        ["Mijn bestellingen", "Visa alla", "Fri frakt", "Privacybeleid", "Jetzt Member werden", "Retour aanmelden",
         "Bezorgbundel", "Wunschzettel erstellen", "#ichkochemit", "Woolly", "Kerstboomverlichting", "Datorer",
         "Allroundruten"])) + "</body></html>")
    pool = L2.value_pool(html, "https://www.woolly.example/")
    assert pool == ["Kerstboomverlichting", "Datorer", "Allroundruten"], pool


def test_fix3_the_value_pool_is_the_sites_own_names():
    pool = L2.value_pool(HOME, "https://shop.example/")
    assert pool[:5] == ["Rain Jackets", "Teapots", "Merino Crew Sock", "Wool Beanie", "Down Jackets"], pool
    for bad in ("Home", "Sale", "Gift Cards", "Sign in", "Contact us", "Cart (0)", "Partner Brand", "2024",
                "Merino Crew Sock photo"):
        assert bad not in pool, bad


class Robots:
    def __init__(self, deny=()):
        self.deny = tuple(deny)

    def allow(self, url):
        import urllib.parse
        p = urllib.parse.urlsplit(url)
        return not any((p.path or "/").startswith(d) for d in self.deny)


def test_fix3_the_search_check_under_robots():
    ok = L2.search_check("shop.example", HOME, "https://shop.example/", Robots(), "Teapots")
    assert ok["how"] == "get_form" and ok["ok"] and ok["url"] == "https://shop.example/search?q=Teapots", ok
    no = L2.search_check("shop.example", HOME, "https://shop.example/", Robots(deny=("/search",)), "Teapots")
    assert no["how"] == "get_form" and not no["ok"]
    post = HOME.replace('method="get"', 'method="post"')
    p = L2.search_check("shop.example", post, "https://shop.example/", Robots(), "Teapots")
    assert p["how"] == "post_form" and p["ok"] and p["url"] == "https://shop.example/search"
    script = HOME.replace('<form action="/search" method="get">', "<div>").replace("</form>", "</div>")
    s1 = L2.search_check("shop.example", script, "https://shop.example/", Robots(), "Teapots")
    s2 = L2.search_check("shop.example", script, "https://shop.example/", Robots(deny=("/search",)), "Teapots")
    assert s1["how"] == "script" and s1["ok"] and not s2["ok"], (s1, s2)


def test_the_screen_keeps_the_frames_stratum():
    import inspect
    src = inspect.getsource(L2.cmd_screen)
    i = src.index("VS.classify_safe(")
    assert 'rec["stratum"] = st' in src[i:i + 900], "classify's stratum must not replace the frame's"


def test_a_scope_hosts_robots_txt_is_fetched_once_not_recursively():
    # found in the measured arms: allow() -> robots.load(h) -> fetch robots.txt -> allow() -> load(h) ...
    import discover_bench as DBN
    from anyuse.execution import discover as D
    ctx = L2.SiteCtx("www.shop.example", "arch", 170, 16 * 1024 * 1024, "http://192.0.2.1:9222", None)
    gets = []

    def fake_send(url, headers):
        gets.append(url)
        return 200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"
    ctx.fetch._client = fake_send
    ctx.fetch.check_destination = False                                  # no DNS in a test
    ctx.fetch.pacer.sleeper = lambda s: None
    ctx.scope.host_site["static.shop.example"] = "www.shop.example"     # an own host the scope allowed
    assert ctx.allowed("https://static.shop.example/app.js")
    assert ctx.allowed("https://static.shop.example/other.js")
    assert gets == ["https://static.shop.example/robots.txt"], gets
    assert ctx.used() == 1


def test_fix3_tasks_run_search_first_gates_last():
    ts = [{"id": "S1-A3", "kind": "gate"}, {"id": "S1-B2", "kind": "answer_question"}, {"id": "S1-A1", "kind": "search"}]
    assert [t["kind"] for t in L2.ordered(ts)] == ["search", "answer_question", "gate"]


def test_fix7_proof_values_never_repeat_the_compile_value():
    assert L2.proof_values("teapots", ["Teapots", "Rain Jackets", "Wool Beanie", "Down Jackets"]) == \
        ["Rain Jackets", "Wool Beanie", "Down Jackets"]
    assert L2.proof_values("socks", ["A b", "C d", "E f", "G h"]) == ["A b", "C d", "E f"]


def test_the_shares_fit_the_approved_300():
    assert sum(L2.SHARES.values()) + 3 + L2.MARGIN <= L2.SITE_CAP
    assert L2.share_for("arch", 120 + 3) == 170                    # after the screen and the baseline
    assert L2.share_for("arch", 200) == 300 - 200 - L2.MARGIN       # never beyond what the 300 leaves
    assert L2.share_for("arch", 0) == 170 and L2.share_for("baseline", 0) == 120
    assert L2.STATIC_RESERVE_FRAC == 0.5 and L2.RUNS == 3


def test_the_reader_is_deferred_and_cached():
    col = {}
    rd = L2.cache_reader({}, col)
    v, why = rd("answer", {"utterance": "How much?", "kind": "answer_question", "target": "X"}, "5 EUR", "PAGE")
    assert v == "unavailable" and len(col) == 1
    k = next(iter(col))
    rd2 = L2.cache_reader({k: {"verdict": "correct", "why": "the page says 5 EUR"}})
    assert rd2("answer", {"utterance": "How much?", "kind": "answer_question", "target": "X"}, "5 EUR", "PAGE") == \
        ("correct", "the page says 5 EUR")


def test_fix6_every_session_opens_a_fresh_context():
    assert L2.session_class().fresh_context is True


# -- the run loop with fakes: 3 runs per task, the proof right after the search task -------------

class FakeBudget:
    def __init__(self):
        self.lanes, self.noise_skipped, self.static_skipped, self.refused_methods = {}, {}, {}, []
        self.third_party_refused, self.violations, self.sent, self.cache_hits = [], [], [], {}
        self.static_peak = 0
        self.site_bytes = {}

    def check(self):
        return None

    def site_of(self, h):
        return h


class FakeCtx:
    def __init__(self, site, arm, share, nbytes, endpoint, gate):
        self.site, self.share = site, share
        self.budget = FakeBudget()
        self.pacer = type("P", (), {"intervals": {}})()
        self.scope = type("S", (), {"site_report": lambda self, s: {}})()
        self.fetch = type("F", (), {"blocked": {}})()
        self.robots = type("R", (), {"parsers": {}, "notes": {site: "parsed"}, "load": lambda self, s: None,
                                     "allow": lambda self, u: True})()
        self.n = 0

    def used(self):
        return self.n

    def left(self):
        return self.share - self.n

    def nbytes(self):
        return 0


def test_fix6_fix7_three_runs_per_task_and_the_proof_after_the_search_task():
    from anyuse.execution.traffic_gate import SiteGate
    events = []
    calls = {"i": 0}

    class FakeArm:
        arm, root, reader, endpoint = "arch", tempfile.mkdtemp(), None, "http://192.0.2.1:9222"

        def log(self, m):
            pass

        def run_task(self, ctx, task, start, shots):
            calls["i"] += 1
            ctx.n += 5
            events.append(("run", task["id"]))
            ok = task["kind"] != "answer_question" or calls["i"] % 2 == 0
            ops = [{"op": "open", "url": start}] + ([{"op": "search", "value": "Teapots"}] if task["kind"] == "search"
                                                   else [])
            return {"task": task["id"], "stop": "done", "final": {"url": start + "search?q=teapots",
                                                                  "text": "3 results for Teapots\nBlue Teapot",
                                                                  "links": ["Blue Teapot"]},
                    "pages": [{"text_head": "home"}], "start_url": start, "program": {"ops": ops},
                    "answer": "12 EUR" if ok else "", "gate": {"kind": "cart"} if task["kind"] == "gate" else None}
    saved = (L2.SiteCtx, L2.prove, L2.compiled_repeat, L2.load_cache, L2.save_cache, L2.past_guard)
    try:
        L2.SiteCtx = FakeCtx
        L2.prove = lambda arm, ctx, rec, values, sdir: (events.append(("proof", rec["task"])) or
                                                        {"status": "proved", "task": rec["task"]})
        L2.compiled_repeat = lambda arm, ctx, rec, task: (events.append(("compiled", task["id"])) or
                                                          {"task": task["id"], "judge": {"verdict": "ok"},
                                                           "model_calls": 0})
        L2.load_cache = lambda b, s: 0
        L2.save_cache = lambda b, s: 0
        L2.past_guard = lambda arm: False
        os.makedirs(os.path.join(FakeArm.root, "runs"), exist_ok=True)
        state = {"entries": {}}
        tasks = [{"id": "S1-A3", "kind": "gate", "utterance": "Add the Blue Teapot to my cart"},
                 {"id": "S1-B2", "kind": "answer_question", "utterance": "How much is the Blue Teapot?",
                  "target": "Blue Teapot", "field": "other"},
                 {"id": "S1-A1", "kind": "search", "utterance": "Search for teapots", "value": "Teapots"}]
        out = tempfile.mkdtemp()
        res = L2.run_site(FakeArm(), "shop.example", tasks, ["Rain Jackets", "Wool Beanie", "Down Jackets"], out,
                          SiteGate(per_site=1, max_sites=1), state, lambda: None)
    finally:
        (L2.SiteCtx, L2.prove, L2.compiled_repeat, L2.load_cache, L2.save_cache, L2.past_guard) = saved
    assert res == ""
    runs = [t for k, t in events if k == "run"]
    assert runs == ["S1-A1"] * 3 + ["S1-B2"] * 3 + ["S1-A3"] * 3, runs
    assert events.index(("proof", "S1-A1")) == 3, events          # right after the search task's 3 runs
    e = state["entries"]["shop.example"]
    assert e["proof"]["status"] == "proved"
    assert [r["run"] for r in e["tasks"] if r["task"] == "S1-A1"] == [1, 2, 3]
    assert ("compiled", "S1-A1") in events and ("compiled", "S1-A3") in events
    saved_files = sorted(os.listdir(os.path.join(out, "shop_example")))
    assert "S1-A1_r3.json" in saved_files and "compiled_S1-A1.json" in saved_files


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
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
