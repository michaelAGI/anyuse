"""``anyuse watch`` (docs/WATCH.md), offline: a fake replay stands in for the guarded browser, fakes for the
models (no browser, no network, no model; the store is a temporary directory).

What must hold:

1. an unchanged item gives an EMPTY diff (a baseline, then "unchanged": no changelog entry);
2. a price change and an availability change are diffed with the evidence of both sides (URL, sources, time);
3. a list with an added and a removed result is diffed as such (and a retitled / moved one);
4. a failed or incomplete replay is DRIFT ("re-demonstrate"), never a content change: the last good snapshot
   stays the baseline, and the next good replay is diffed against it;
5. a CONFLICT field (web-watch fix 1) is drift, never a change;
6. ``--dry-run`` writes nothing to the store (the guard's traffic ledger still books the contact, so the
   1 h minimum holds for dry runs too);
7. ``--explain`` calls the model only for a non-empty diff, logs the calls per run, and never uses the hosted
   Jev API / OpenRouter; OpenJev answers a typed choice, Cerebras a summary (both faked); OpenJev's choice ends
   with the FIXED option "nothing important changed", which answers a cosmetic reorder (web-valuecheck2);
8. the minimum interval of 1 h per item, the per-site day caps, a blocked site and a traffic violation's
   hard stop are enforced;
9. the agent hook is only printed (and its command always exits 0); the CLI keeps ``anyuse watch
   [audio|msg|none]`` (screen watching) working.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import watch as WA  # noqa: E402

T0 = dt.datetime(2026, 9, 30, 18, 0, tzinfo=dt.timezone.utc)
PRODUCT_TOOL = {"version": 6, "workflow": "product_lookup", "site": "www.shop.test", "start_url": "https://www.shop.test/",
                "search": {"mode": "url", "template": "https://www.shop.test/search?q={q}"},
                "fields": {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"},
                "region": {"regex": "^x$"}, "hosts": ["www.shop.test"], "model_calls": 0}
LIST_TOOL = dict(PRODUCT_TOOL, workflow="site_search", site="www.news.test", fields=None, top_k=5,
                 search={"mode": "url", "template": "https://www.news.test/?s={q}"}, hosts=["www.news.test"])


def product(price=159.0, availability="InStock", name="Line 6 POD Express Guitar", status="ok", **kw):
    url = "https://www.shop.test/p/line-6-pod-express-guitar/"
    rec = {"status": status, "output": {"name": name, "price": price, "currency": "EUR", "availability": availability,
                                        "url": url} if status == "ok" else None,
           "why": "identity and fields checked" if status == "ok" else kw.pop("why", "a failure"),
           "pages": [{"url": "https://www.shop.test/search?q=Line+6", "title": "Suche"},
                     {"url": url, "title": name, "h1": [name]}],
           "model_calls": 0, "requests": 9, "bytes": 400000, "picked": {"title": name, "url": url}}
    rec.update(kw)
    return rec


def listing(results, status="ok", **kw):
    rec = {"status": status, "output": {"results": [{"title": t, "url": u} for t, u in results]} if results is not None
           else None, "why": kw.pop("why", "query: ok"), "pages": [{"url": "https://www.news.test/?s=Garten", "title": "Suche"}],
           "model_calls": 0, "requests": 6, "bytes": 300000}
    rec.update(kw)
    return rec


NEWS = [(f"Artikel {n}", f"https://www.news.test/artikel-{n}/") for n in range(1, 7)]


class Replay:
    """The fake guarded replay: the next record of a script per item; counts the calls."""

    def __init__(self, script):
        self.script, self.calls = {k: list(v) for k, v in script.items()}, []

    def __call__(self, item, tool, caps):
        self.calls.append((item["id"], dict(caps)))
        key = "list" if item["workflow"] == "site_search" else "product"
        return self.script[key].pop(0)


class Store:
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="anyuse-watch-test-")
        self.st = WA.WatchStore(self.dir)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)

    def tool(self, tool, name="t.tool.json"):
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            json.dump(tool, fh)
        return p

    def files(self):
        out = {}
        for root, _d, fs in os.walk(self.dir):
            for f in fs:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.dir)] = fh.read()
        return out


def hours(h):
    return T0 + dt.timedelta(hours=h)


def test_an_unchanged_item_gives_an_empty_diff():
    with Store() as s:
        item = s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        rp = Replay({"product": [product(), product()]})
        r1 = WA.run_watch(s.st, rp, now=hours(0))
        assert [e["kind"] for e in r1["entries"]] == ["baseline"] and r1["model_calls"]["replay"] == 0, r1
        r2 = WA.run_watch(s.st, rp, now=hours(1))
        assert [e["kind"] for e in r2["entries"]] == ["unchanged"] and r2["entries"][0]["changes"] == [], r2
        assert [e["kind"] for e in s.st.changelog()] == ["baseline"]          # nothing to log for "unchanged"
        assert len(s.st.snapshots(item["id"])) == 2 and s.st.items()[item["id"]]["last_status"] == "ok"
        runs = open(os.path.join(s.dir, "runs.jsonl")).read().splitlines()
        assert len(runs) == 2 and json.loads(runs[1])["items"][0]["kind"] == "unchanged"


def test_a_price_change_with_the_evidence_of_both_sides():
    with Store() as s:
        s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        rp = Replay({"product": [product(159.0), product(149.0)]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(2))
        e = r["entries"][0]
        assert e["kind"] == "change" and e["changes"] == [
            {"kind": "price", "before": 159.0, "after": 149.0, "currency_before": "EUR", "currency_after": "EUR",
             "delta": -10.0, "pct": -6.3}], e
        assert e["before"]["snapshot"] == "2026-09-30T18:00:00Z" and e["after"]["snapshot"] == "2026-09-30T20:00:00Z"
        assert e["after"]["url"].endswith("/p/line-6-pod-express-guitar/") and e["after"]["sources"]["price"] == "jsonld"
        md = open(os.path.join(s.dir, "CHANGELOG.md")).read()
        assert "price 159.0 EUR -> 149.0 EUR (-10.00, -6.3 %)" in md and "- before: 2026-09-30T18:00:00Z" in md, md


def test_an_availability_change():
    with Store() as s:
        s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        rp = Replay({"product": [product(availability="InStock"), product(availability="https://schema.org/InStock"),
                                 product(availability="OutOfStock")]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(1))
        assert r["entries"][0]["kind"] == "unchanged", r            # the same class, spelled differently
        r = WA.run_watch(s.st, rp, now=hours(2))
        ch = r["entries"][0]["changes"]
        assert ch == [{"kind": "availability", "before": "https://schema.org/InStock", "after": "OutOfStock",
                       "before_class": "in_stock", "after_class": "out_of_stock"}], ch


def test_a_list_with_an_added_and_a_removed_result():
    with Store() as s:
        s.st.add(s.tool(LIST_TOOL), "Garten", "1h")
        before = NEWS[:5]
        after = [NEWS[0], NEWS[1], NEWS[5], NEWS[2], NEWS[3]]           # #5 removed, a new one at #3
        rp = Replay({"list": [listing(before), listing(after), listing([(t + " (neu)" if k == 0 else t, u)
                                                                       for k, (t, u) in enumerate(after)])]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(1))
        kinds = [(c["kind"], c.get("rank") or c.get("after_rank")) for c in r["entries"][0]["changes"]]
        assert ("removed", 5) in kinds and ("added", 3) in kinds, kinds
        assert ("moved", 4) in kinds and ("moved", 5) in kinds, kinds    # the two below the new one moved down
        r = WA.run_watch(s.st, rp, now=hours(2))
        assert [c["kind"] for c in r["entries"][0]["changes"]] == ["retitled"], r["entries"][0]


def test_a_failed_or_incomplete_replay_is_drift_never_a_change():
    with Store() as s:
        item = s.st.add(s.tool(LIST_TOOL), "Garten", "1h")
        rp = Replay({"list": [listing(NEWS[:5]),
                              listing(None, status="failed", why="membership: no item of the compiled region"),
                              listing(NEWS[:4] + [NEWS[5]], status="incomplete", missing=[{"why": "unseen card"}],
                                      why="incomplete: 1 result(s) among the top 5 could not be read"),
                              listing(NEWS[:5])]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(1))
        e = r["entries"][0]
        assert e["kind"] == "drift" and e["changes"] == [] and e["action"] == "re-demonstrate", e
        dg = e["drift"]["diagnosis"]
        assert dg["pages"][0]["url"] == "https://www.news.test/?s=Garten" and "wait" in dg, dg
        r = WA.run_watch(s.st, rp, now=hours(2))
        assert r["entries"][0]["kind"] == "drift" and r["entries"][0]["drift"]["status"] == "incomplete", r
        it = s.st.items()[item["id"]]
        assert it["drift_since"] == "2026-09-30T19:00:00Z" and it["last_good"].startswith("20260930T180000Z"), it
        # the next good replay is diffed against the LAST GOOD snapshot: unchanged, not "5 added"
        r = WA.run_watch(s.st, rp, now=hours(3))
        assert r["entries"][0]["kind"] == "unchanged", r
        assert s.st.items()[item["id"]]["drift_since"] is None
        md = open(os.path.join(s.dir, "CHANGELOG.md")).read()
        assert md.count("**DRIFT (re-demonstrate)**") == 2 and "**change**" not in md, md


def test_a_conflict_field_is_drift_never_a_change():
    with Store() as s:
        s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        conflict = product(status="failed", why="conflict: availability: jsonld 'LimitedAvailability' vs visible "
                                                "in_stock", conflicts={"availability": {"visible": "in_stock"}},
                           fields_seen={"availability": WA.CONFLICT, "price": 159.0})
        rp = Replay({"product": [product(availability="InStock"), conflict]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(1))
        e = r["entries"][0]
        assert e["kind"] == "drift" and e["changes"] == [] and r["items"][0]["status"] == "conflict", e
        assert e["drift"]["conflicts"] == {"availability": {"visible": "in_stock"}}, e
        # a conflict reported by an ACCEPTED record (any tool that returns one) is drift too
        snap = WA.snapshot_of(s.st.items()[e["item"]], PRODUCT_TOOL,
                              product(conflicts={"availability": {}}), hours(2))
        assert not snap["good"] and snap["status"] == "conflict" and snap["content"] is None


def test_a_dry_run_writes_nothing_to_the_store():
    with Store() as s:
        item = s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        rp = Replay({"product": [product(159.0), product(149.0), product(149.0)]})
        WA.run_watch(s.st, rp, now=hours(0))
        before = s.files()
        r = WA.run_watch(s.st, rp, now=hours(2), dry_run=True)
        assert r["dry_run"] and r["entries"][0]["kind"] == "change", r            # the diff is shown
        after = s.files()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        assert changed == [os.path.join("guard", "traffic.json")], changed       # the guard's ledger only
        assert len(s.st.snapshots(item["id"])) == 1 and len(s.st.changelog()) == 1
        # the ledger booked the dry run: the 1 h minimum holds for it
        r = WA.run_watch(s.st, rp, now=hours(2.5))
        assert r["items"][0]["skipped"].startswith("not due"), r
        assert len(rp.calls) == 2


def test_explain_calls_the_model_only_on_a_non_empty_diff():
    class FakeExplainer:
        backend, model = "fake", "fake-1"

        def __init__(self):
            self.calls = 0

        def explain(self, e):
            self.calls += 1
            assert e["kind"] == "change" and e["changes"]
            return {"backend": "fake", "model": "fake-1", "text": "the price fell", "calls": 1, "prompt_tokens": 50,
                    "completion_tokens": 8, "cost_usd": 0.00002}
    with Store() as s:
        s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        s.st.add(s.tool(LIST_TOOL, "l.tool.json"), "Garten", "1h")
        rp = Replay({"product": [product(159.0), product(159.0), product(149.0)],
                     "list": [listing(NEWS[:5]), listing(NEWS[:5]), listing(None, status="failed", why="x")]})
        x = FakeExplainer()
        r = WA.run_watch(s.st, rp, now=hours(0), explainer=x)          # baselines: no call
        assert x.calls == 0 and r["model_calls"]["explain"]["calls"] == 0, r["model_calls"]
        r = WA.run_watch(s.st, rp, now=hours(1), explainer=x)          # unchanged: no call
        assert x.calls == 0 and r["model_calls"] == {"replay": 0, "explain": {
            "backend": "fake", "model": "fake-1", "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost_usd": 0.0, "errors": []}}, r["model_calls"]
        r = WA.run_watch(s.st, rp, now=hours(2), explainer=x)          # a price change + a drift: one call
        assert x.calls == 1 and r["model_calls"]["explain"]["calls"] == 1, r["model_calls"]
        e = next(e for e in r["entries"] if e["kind"] == "change")
        assert e["explain"]["text"] == "the price fell"
        runs = [json.loads(ln) for ln in open(os.path.join(s.dir, "runs.jsonl"))]
        assert [x_["model_calls"]["explain"]["calls"] for x_ in runs] == [0, 0, 1], runs
        assert "explain (fake, fake-1): the price fell" in open(os.path.join(s.dir, "CHANGELOG.md")).read()


def test_the_explain_backends_never_the_jev_api_nor_openrouter():
    from anyuse.execution import model_adapter as M
    from anyuse.execution import openjev
    e = {"kind": "change", "workflow": "product_lookup", "site": "www.shop.test", "input": "x",
         "before": {"snapshot": "a"}, "after": {"snapshot": "b"},
         "changes": [{"kind": "price", "before": 2.0, "after": 1.0, "currency_before": "EUR", "currency_after": "EUR"},
                     {"kind": "availability", "before": "InStock", "after": "OutOfStock", "before_class": "in_stock",
                      "after_class": "out_of_stock"}]}
    prompt = WA.explain_prompt(e)
    assert "price 2.0 EUR -> 1.0 EUR" in prompt and "availability InStock" in prompt

    class FakeHttp:
        def __init__(self):
            self.sent = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.sent.append((url, json))

            class R:
                status_code = 200
                headers = {}
                content = b"{}"

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": "It sold out and got cheaper."},
                                         "finish_reason": "stop"}], "usage": {"prompt_tokens": 90, "completion_tokens": 9}}
            return R()
    cfg = M.ModelConfig(provider="cerebras", base_url=M.PROVIDER_PRESETS["cerebras"].base_url, model="gpt-oss-120b",
                        api_key_env="", price_in=0.25, price_out=0.69)
    ad = M.ModelAdapter(cfg, budget=M.RunBudget(0.10))
    http = FakeHttp()
    ad._client = http
    x = WA.CerebrasExplainer(adapter=ad)
    got = x.explain(e)
    assert got["text"] == "It sold out and got cheaper." and got["calls"] == 1 and got["prompt_tokens"] == 90, got
    assert http.sent[0][0] == "https://api.cerebras.ai/v1/chat/completions", http.sent
    bad = M.ModelAdapter(M.ModelConfig(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                                       model="m", api_key_env=""))
    for wrong in (bad,):
        try:
            WA.CerebrasExplainer(adapter=wrong)
            raise AssertionError("an OpenRouter adapter was accepted")
        except ValueError:
            pass
    # OpenJev: a typed choice among the diff's own lines; the hosted Jev API (OpenRouter) is refused
    answers = []

    def poster(url, body, headers, timeout):
        answers.append(url)
        return 200, {}, json.dumps({"answers": {"q": {"choice": "c1", "confidence": 0.8}},
                                    "usage": {"input_tokens": 120}}).encode()
    oj = WA.OpenJevExplainer(client=openjev.Client(url="http://127.0.0.1:8123", poster=poster, key=""))
    got = oj.explain(e)
    assert got["calls"] == 1 and got["text"].startswith("most important (0.80): availability InStock"), got
    assert answers == ["http://127.0.0.1:8123/v1/systemone"], answers
    one = dict(e, changes=e["changes"][:1])
    assert oj.explain(one)["calls"] == 0 and len(answers) == 1                 # one line: no call
    assert got["important"] is True


def test_explain_has_a_fixed_nothing_important_option():
    """web-valuecheck2: OpenJev's explain question always ends with the FIXED option "nothing important changed";
    a cosmetic reorder (only "moved" lines) is answered as unimportant, recorded on the entry and the changelog."""
    from anyuse.execution import openjev
    sent = []

    def poster(url, body, headers, timeout):
        crit = json.loads(body)["questions"]["q"]["criteria"]
        sent.append(list(crit.values()))
        lines = list(crit.values())[:-1]
        # the fake: a real change (price, availability, added, removed) is the most important line; a diff of
        # moved lines only is "nothing important" (the last option)
        k = next((f"c{i}" for i, v in enumerate(lines) if v.startswith(("price", "availability", "added", "removed"))),
                 f"c{len(crit) - 1}")
        return 200, {}, json.dumps({"answers": {"q": {"choice": k, "confidence": 0.9}}}).encode()
    oj = WA.OpenJevExplainer(client=openjev.Client(url="http://127.0.0.1:8123", poster=poster, key=""))
    with Store() as s:
        s.st.add(s.tool(LIST_TOOL), "Garten", "1h", now=hours(0))
        before = [(f"Artikel {n}", f"https://www.news.test/artikel-{n}/") for n in range(1, 6)]
        after = [before[0], before[2], before[1], before[3], before[4]]            # #2 <-> #3 swapped
        seq = [listing(before), listing(after)]
        rp = lambda it, tl, caps: seq.pop(0)  # noqa: E731
        WA.run_watch(s.st, rp, now=hours(0), explainer=oj)
        r = WA.run_watch(s.st, rp, now=hours(1), explainer=oj)
        e = r["entries"][0]
        assert e["kind"] == "change" and {c["kind"] for c in e["changes"]} == {"moved"} and len(e["changes"]) == 2
        assert sent[-1][-1] == WA.NOTHING_IMPORTANT and len(sent[-1]) == 3
        assert e["explain"]["important"] is False and e["explain"]["text"].startswith("nothing important (0.90)")
        assert r["model_calls"]["explain"]["calls"] == 1
        log = open(os.path.join(s.dir, "CHANGELOG.md")).read()
        assert "explain (openjev, " in log and "nothing important" in log
    # a real change among cosmetic lines is still picked as the most important line
    e = {"workflow": "product_lookup", "site": "www.shop.test", "input": "x", "changes": [
        {"kind": "field", "field": "url", "before": "https://www.shop.test/p/x", "after": "https://www.shop.test/p/x?ref=1"},
        {"kind": "price", "field": "price", "before": 159.0, "after": 129.0, "currency_before": "EUR",
         "currency_after": "EUR", "delta": -30.0, "pct": -18.9}]}
    got = oj.explain(e)
    assert got["important"] is True and "price" in got["text"] and sent[-1][-1] == WA.NOTHING_IMPORTANT
    for url in ("https://openrouter.ai/api/alpha", ""):
        try:
            WA.OpenJevExplainer(client=openjev.Client(url=url, poster=poster, key=""))
            raise AssertionError(f"{url!r} accepted")
        except ValueError:
            pass


def test_the_minimum_interval_the_day_caps_a_block_and_a_hard_stop():
    try:
        WA.parse_every("30m")
        raise AssertionError("30 minutes accepted")
    except ValueError as exc:
        assert "minimum interval of 1 h" in str(exc)
    assert WA.parse_every("1d") == 86400 and WA.parse_every("1h") == 3600 and WA.parse_every("2w") == 1209600
    with Store() as s:
        item = s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "6h")
        rp = Replay({"product": [product(), product(), product(requests=250), product()]})
        WA.run_watch(s.st, rp, now=hours(0))
        r = WA.run_watch(s.st, rp, now=hours(5))                        # every 6 h: not due
        assert r["items"][0]["skipped"].startswith("not due") and len(rp.calls) == 1, r
        WA.run_watch(s.st, rp, now=hours(6))
        assert rp.calls[-1][1] == {"requests": WA.RUN_REQUESTS, "bytes": WA.RUN_BYTES}, rp.calls
        # the caps may be lowered, never raised
        os.environ["ANYUSE_WATCH_RUN_REQUESTS"], os.environ["ANYUSE_WATCH_DAY_BYTES"] = "35", str(10 ** 12)
        try:
            assert WA.caps()["run_requests"] == 35 and WA.caps()["day_bytes"] == WA.DAY_BYTES, WA.caps()
        finally:
            del os.environ["ANYUSE_WATCH_RUN_REQUESTS"], os.environ["ANYUSE_WATCH_DAY_BYTES"]
        # a new UTC day: 9 requests, then a heavy run (250); the next run that day is refused (the day cap)
        WA.run_watch(s.st, rp, now=hours(12))
        r = WA.run_watch(s.st, rp, now=hours(18))
        assert "daily cap" in r["items"][0]["skipped"], r
        s.st.block("www.shop.test", "HTTP 403 challenge")
        r = WA.run_watch(s.st, rp, now=hours(40))
        assert r["items"][0]["skipped"].startswith("the site blocked us"), r
    with Store() as s:
        s.st.add(s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar", "1h")
        rp = Replay({"product": [product(violations=["a third-party request was sent"]), product()]})
        r = WA.run_watch(s.st, rp, now=hours(0))
        assert r["hard_stop"] == "a traffic violation" and os.path.exists(s.st.hard_stop_path), r
        r = WA.run_watch(s.st, rp, now=hours(2))
        assert r["hard_stop"] and r["items"] == [] and len(rp.calls) == 1, r


def test_the_hook_is_printed_never_installed_and_the_cli():
    from click.testing import CliRunner
    from anyuse import cli as CLI
    text = WA.install_hook_text()
    assert "printed only; anyuse installs nothing" in text and '"SessionStart"' in text and "UNVERIFIED" in text
    res = CliRunner().invoke(CLI.cli, ["watch", "install-hook"])
    assert res.exit_code != 0 and "installs no hook" in res.output, res.output
    res = CliRunner().invoke(CLI.cli, ["watch", "install-hook", "--print"])
    assert res.exit_code == 0 and res.output.strip() == text.strip(), res.output
    with Store() as s:
        res = CliRunner().invoke(CLI.cli, ["watch", "add", s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar",
                                           "--every", "30m", "--store", s.dir])
        assert res.exit_code != 0 and "minimum interval" in res.output, res.output
        res = CliRunner().invoke(CLI.cli, ["watch", "add", s.tool(PRODUCT_TOOL), "Line 6 POD Express Guitar",
                                           "--every", "1d", "--store", s.dir])
        assert res.exit_code == 0 and "watching www-shop-test--line-6-pod-express-guitar--" in res.output, res.output
        # the run command with the live replay replaced by the fake one (no browser)
        import anyuse.execution.watch_live as WL
        real = WL.make_replay
        WL.make_replay = lambda endpoint, cache_dir, log=None: Replay({"product": [product(), product(149.0)]})
        try:
            res = CliRunner().invoke(CLI.cli, ["watch", "run", "--store", s.dir])
            assert res.exit_code != 0 and "--browser" in res.output, res.output
            res = CliRunner().invoke(CLI.cli, ["watch", "run", "--store", s.dir, "--browser", "http://127.0.0.1:9"])
            assert res.exit_code == 0 and "**baseline**" in res.output and "replay 0" in res.output, res.output
        finally:
            WL.make_replay = real
        res = CliRunner().invoke(CLI.cli, ["watch", "log", "--store", s.dir])
        assert res.exit_code == 0 and "**baseline**" in res.output, res.output
        res = CliRunner().invoke(CLI.cli, ["watch", "list", "--store", s.dir])
        assert res.exit_code == 0 and " ok" in res.output, res.output
        # the hook command: nothing to say yet (a baseline is not news), exit 0
        res = CliRunner().invoke(CLI.cli, ["watch", "hook-run", "--store", s.dir])
        assert res.exit_code == 0 and res.output == "", res.output
        s.st.append_changelog({"at": WA.iso(WA.utcnow()), "item": "x", "site": "www.shop.test", "input": "y",
                               "workflow": "product_lookup", "tool_sha256": "0" * 64, "kind": "change",
                               "changes": [{"kind": "price", "before": 2.0, "after": 1.0, "currency_before": "EUR",
                                            "currency_after": "EUR"}]})
        res = CliRunner().invoke(CLI.cli, ["watch", "hook-run", "--store", s.dir])
        out = json.loads(res.output)
        assert res.exit_code == 0 and "1 change(s), 0 drift(s)" in out["hookSpecificOutput"]["additionalContext"], out
    # The old caption shortcuts are gone; bare watch still prints page-monitoring help.
    for mode in ("audio", "msg", "none"):
        res = CliRunner().invoke(CLI.cli, ["watch", mode])
        assert res.exit_code == 2 and "No such command" in res.output, res.output
    res = CliRunner().invoke(CLI.cli, ["watch"])
    assert res.exit_code == 0 and "Usage:" in res.output and "add" in res.output, res.output


def test_caption_only_commands_and_options_are_removed():
    from click.testing import CliRunner
    from anyuse import cli as CLI
    runner = CliRunner()
    res = runner.invoke(CLI.cli, ["watcher", "start", "--help"])
    assert res.exit_code == 0 and "--steps" in res.output and "--duration" in res.output
    for option in ("--interval", "--output", "--verbose", "--no-voice-input", "--push-to-talk"):
        assert option not in res.output, res.output
    res = runner.invoke(CLI.cli, ["watcher", "compose"])
    assert res.exit_code == 2 and "No such command" in res.output, res.output
    res = runner.invoke(CLI.cli, ["guide", "learn-from-session", "--help"])
    assert res.exit_code == 0 and "Structured steps are not learned from yet" in res.output, res.output


def test_watch_live_does_not_refuse_competitor_domains_and_uses_user_never_contact():
    from anyuse.execution import site_modes as SM
    from anyuse.execution import watch_live as WL
    from anyuse.execution import web_scope as WS
    from anyuse.execution.web_runtime import never_contact as NC
    WL._bench()
    import discover_bench as DBN
    import live_bench as LB
    bundled_before = open(NC._PATH, "rb").read()
    tmp = tempfile.mkdtemp(prefix="anyuse-nc-test-")
    old_data_dir = os.environ.get("ANYUSE_DATA_DIR")
    os.environ["ANYUSE_DATA_DIR"] = tmp
    orig_robots_load = DBN.Robots.load

    def fake_robots_load(self, host):
        import urllib.robotparser
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(["User-agent: *", "Allow: /"])
        self.parsers[host] = rp
        self.notes[host] = "parsed (200)"

    try:
        assert WS.contact_refusal("docs.anthropic.com", refuse_gov=True) == ""
        assert WS.contact_refusal("platform.openai.com", refuse_gov=True) == ""
        assert WS.contact_refusal("www.usa.gov", refuse_gov=True) != ""
        out_path = SM.add_user_never_contact("Blocked.Shop.Test", "HTTP 403 challenge")
        assert out_path == os.path.join(tmp, "never_contact.txt") and os.path.isfile(out_path)
        assert open(NC._PATH, "rb").read() == bundled_before
        assert "blocked.shop.test" in NC.never_contact()
        assert "blocked.shop.test" in DBN.never_contact()
        assert "blocked.shop.test" in LB._never()
        assert WS.contact_refusal("blocked.shop.test") == "on NEVER_CONTACT"
        assert WS.contact_refusal("BLOCKED.SHOP.TEST") == "on NEVER_CONTACT"

        class NoFetch:
            def get(self, *args, **kwargs):
                raise AssertionError("a never-contact host must not fetch robots.txt")

        # The user list wins in every robots mode, including with permissive cached rules.
        for refuse_competitors in (False, True):
            for override in ("", "ANYUSE_ROBOTS=ignore", "ANYUSE_ROBOTS=user_initiated (test)"):
                robots = DBN.Robots(NoFetch(), refuse_competitors=refuse_competitors)
                robots.override = override
                assert robots.load("blocked.shop.test") is None
                assert robots.parsers["blocked.shop.test"] is None
                assert robots.texts["blocked.shop.test"] is None
                assert robots.notes["blocked.shop.test"] == "refused (on NEVER_CONTACT): disallow all"
                assert not robots.allow("https://blocked.shop.test/robots.txt")
                fake_robots_load(robots, "blocked.shop.test")
                assert robots.parsers["blocked.shop.test"].can_fetch("*", "https://blocked.shop.test/page")
                assert not robots.allow("https://blocked.shop.test/robots.txt")
                assert not robots.allow("https://blocked.shop.test/page")

        # Exercise watch_live.make_replay on a competitor host (docs.anthropic.com):
        # 1) allowed through SiteCtx(refuse_competitors=False) and Robots(refuse_competitors=False),
        # 2) when inner returns status="blocked", writes to user never_contact.txt (not bundled),
        # 3) subsequent replay on the same host is refused before any request.
        DBN.Robots.load = fake_robots_load
        replay = WL.make_replay(
            "http://127.0.0.1:9",
            os.path.join(tmp, "cache"),
            inner=lambda ctx, endpoint, tool, value: {"status": "blocked", "why": "HTTP 403 challenge"},
        )
        comp_tool = dict(
            PRODUCT_TOOL,
            site="docs.anthropic.com",
            start_url="https://docs.anthropic.com/en/docs",
            search={"mode": "url", "template": "https://docs.anthropic.com/search?q={q}"},
            hosts=["docs.anthropic.com"],
        )
        comp_item = {"id": "comp-1", "site": "docs.anthropic.com", "input": "mcp", "workflow": "product_lookup"}
        r1 = replay(comp_item, comp_tool, {"requests": 20, "bytes": 1000000})
        assert r1["status"] == "blocked", r1
        assert "docs.anthropic.com" in NC.never_contact()
        assert open(NC._PATH, "rb").read() == bundled_before
        r2 = replay(comp_item, comp_tool, {"requests": 20, "bytes": 1000000})
        assert r2["status"] == "refused" and "NEVER_CONTACT" in r2["why"], r2
    finally:
        DBN.Robots.load = orig_robots_load
        if old_data_dir is None:
            os.environ.pop("ANYUSE_DATA_DIR", None)
        else:
            os.environ["ANYUSE_DATA_DIR"] = old_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
