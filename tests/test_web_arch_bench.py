"""The web-architecture bench, offline: the frame
filter, the cohort, the arm shares of a site's 300 requests, the tier-1 URL
template, the spend ledger and its caps, the model clients with fake
transports. No browser, no network, no model."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import web_arch_live as L  # noqa: E402
import web_arch_models as M  # noqa: E402


def test_the_frame_drops_prior_never_contact_government_competitor_and_duplicate_hosts():
    rows = [("www.alpha-shop.com", "shop", "yarn shop"), ("shop.beta.com", "shop", "x"),
            ("www.gamma.org", "other", "recipes"), ("www.delta.gov", "other", "gov"),
            ("www.nc-host.com", "shop", "x"), ("www.browser-use.com", "other", "x"),
            ("m.alpha-shop.com", "shop", "dup"), ("www.bad host", "shop", "x"), ("www.eps.com", "shop", "skip"),
            ("www.s1.com", "shop", "a"), ("www.s2.com", "shop", "b"), ("www.s3.com", "shop", "c"),
            ("www.o1.com", "other", "d"), ("www.o2.com", "other", "e")]
    prior = {"www.beta.com"}                              # registrable beta.com is prior
    frame, dropped = L.build_frame(rows, prior, {"www.nc-host.com"},
                                   gov=lambda h: "gov" if h.endswith(".gov") else "")
    sites = [e["site"] for e in frame]
    assert "shop.beta.com" in dropped["prior"] and "www.delta.gov" in dropped["government"]
    assert "www.nc-host.com" in dropped["never_contact"] and "www.browser-use.com" in dropped["competitor"]
    assert "m.alpha-shop.com" in dropped["duplicate"] and "www.bad host" in dropped["malformed"]
    assert "www.eps.com" in dropped["malformed"]
    assert sorted(sites) == sorted(["www.alpha-shop.com", "www.gamma.org", "www.s1.com", "www.s2.com",
                                    "www.s3.com", "www.o1.com", "www.o2.com"])
    # 3 shops : 2 others, deterministic
    assert [e["stratum"] for e in frame][:5] == ["shop", "shop", "shop", "other", "other"]
    assert frame == L.build_frame(rows, prior, {"www.nc-host.com"},
                                  gov=lambda h: "gov" if h.endswith(".gov") else "")[0]


def test_the_cohort_takes_main_sites_per_stratum_in_screen_order_and_alternates_halves():
    order = [f"s{i}.com" for i in range(20)] + [f"o{i}.org" for i in range(14)]
    sites = {s: {"screen": "eligible", "stratum": "shop"} for s in order[:20]}
    sites.update({s: {"screen": "eligible", "stratum": "other"} for s in order[20:]})
    sites["s3.com"]["screen"] = "blocked"
    c = L.cohort_from({"order": order, "sites": sites})
    assert len(c["main"]["shop"]) == L.MAIN["shop"] and "s3.com" not in c["main"]["shop"]
    assert c["reserves"]["shop"][0] == "s15.com" and c["reserves"]["other"][0] == f"o{L.MAIN['other']}.org"
    assert len(c["order"]) == L.MAIN["shop"] + L.MAIN["other"]
    assert {c["half"][s] for s in c["order"]} == {"A", "B"}
    assert abs(sum(1 for s in c["order"] if c["half"][s] == "A") - len(c["order"]) / 2) <= 1


def test_arm_shares_split_the_sites_300_and_never_exceed_what_is_left():
    assert sum(L.SHARES.values()) + 10 <= L.SITE_CAP
    assert L.share_for("baseline", 2) == 90 and L.share_for("arch", 190) == 106
    assert L.share_for("arch", 290) == 6 and L.share_for("sonnet_agent", 299) == 0
    assert sum(L.BYTES.values()) <= 30 * 1024 * 1024


def test_the_tier1_template_replaces_the_value_in_the_query_or_the_path():
    assert L.tier1_template("https://x.com/search?q=wool+beanie&sort=rel", "wool beanie") == \
        "https://x.com/search?q={q}&sort=rel"
    assert L.tier1_template("https://x.com/search/wool-beanie", "wool beanie") == "https://x.com/search/{q}"
    assert L.tier1_template("https://x.com/results", "wool") == ""


def test_the_refusal_covers_government_and_competitor_domains():
    old = os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
    try:
        # government hosts with ANYUSE_REFUSE_GOV_HOSTS on (off by default: the bench never chooses them as sites)
        assert not L.refusal("www.example.gov")
        os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = "on"
        assert L.refusal("www.bundesregierung.de") and L.refusal("www.example.gov")
    finally:
        os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
        if old is not None:
            os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = old
    assert L.refusal("app.browser-use.com") == "competitor domain"


def test_the_ledger_refuses_a_call_that_could_cross_a_cap_and_books_actual_spend():
    with tempfile.TemporaryDirectory() as d:
        led = M.Ledger(os.path.join(d, "l.json"), cap=0.05, sonnet_cap=0.03)
        assert led("sonnet_agent")
        led("sonnet_agent", actual=0.02)
        assert not led("sonnet_agent")                 # 0.02 + worst 0.03 > 0.03
        assert led("planner")
        led("reader", actual=0.04)
        assert not led("reader")                       # 0.06 + 0.01 > 0.05
        d2 = led.read()
        assert d2["by_kind"] == {"sonnet_agent": 0.02, "reader": 0.04} and d2["calls"]["reader"] == 1


def fake_poster(reply, status=200, seen=None):
    def post(host, path, payload, key, timeout=90.0):
        if seen is not None:
            seen.append((host, path, payload))
        assert host in M.ALLOWED_HOSTS and key == "k"
        return status, json.dumps(reply).encode(), {}
    return post


def test_the_sonnet_brain_books_cost_parses_one_action_and_stops_at_its_cap():
    from anyuse.execution.agent_browser import BudgetExhausted
    with tempfile.TemporaryDirectory() as d:
        led = M.Ledger(os.path.join(d, "l.json"), cap=3.0, sonnet_cap=0.05)
        seen = []
        reply = {"choices": [{"message": {"content": '{"action": "click", "index": 3}'}}],
                 "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "cost": 0.021}}
        b = M.SonnetBrain("k", led, poster=fake_poster(reply, seen=seen))
        act, usage, cached = b([{"role": "user", "content": "x"}])
        assert act == {"action": "click", "index": 3} and usage["cost_usd"] == 0.021 and not cached
        assert seen[0][2]["model"] == M.SONNET_MODEL and seen[0][2]["reasoning"] == {"enabled": False}
        try:
            b([{"role": "user", "content": "y"}])      # 0.021 + worst 0.03 > 0.05
            raise AssertionError("no cap")
        except BudgetExhausted:
            pass


def test_the_planner_client_sends_the_strict_schema_and_prices_cerebras_tokens():
    from anyuse.execution import web_arch as W
    with tempfile.TemporaryDirectory() as d:
        led = M.Ledger(os.path.join(d, "l.json"))
        seen = []
        reply = {"choices": [{"message": {"content": '{"steps": []}'}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 2000, "completion_tokens": 400}}
        spec = W.load_planner_spec()
        p = M.make_planner("k", spec, led, poster=fake_poster(reply, seen=seen))
        text, meta = p([{"role": "user", "content": "g"}])
        assert text == '{"steps": []}' and abs(meta["cost"] - (2000 * 0.25 + 400 * 0.69) / 1e6) < 1e-12
        rf = seen[0][2]["response_format"]
        assert rf["json_schema"]["strict"] and rf["json_schema"]["schema"] == spec["plan_schema"]
        assert seen[0][0] == "api.cerebras.ai" and led.read()["calls"]["planner"] == 1


def test_openjev_is_reached_only_through_loopback():
    old = os.environ.get("ANYUSE_OPENJEV_URL")
    try:
        os.environ["ANYUSE_OPENJEV_URL"] = "https://pod.example.com"
        try:
            M.make_openjev_chooser()
            raise AssertionError("a non-loopback OpenJev URL was accepted")
        except RuntimeError:
            pass
        os.environ["ANYUSE_OPENJEV_URL"] = "http://127.0.0.1:18080"
        assert callable(M.make_openjev_chooser()) and callable(M.make_openjev_confirmer())
    finally:
        if old is None:
            os.environ.pop("ANYUSE_OPENJEV_URL", None)
        else:
            os.environ["ANYUSE_OPENJEV_URL"] = old


def test_shop_signals_and_snippets_read_the_fetched_page_only():
    body = ("<html><head><title>Woolly</title><script type='application/ld+json'>"
            + json.dumps({"@type": "Product", "name": "Wool Beanie", "offers": {"price": "25", "priceCurrency": "EUR"}})
            + "</script></head><body><h1>Woolly Shop</h1><a href='https://w.com/p/wool-beanie'>Wool Beanie</a>"
              "<a href='https://w.com/cart'>Cart</a><p>€25.00 €18.00 €9.90</p></body></html>")
    sig = L.shop_signals(body)
    assert sig["jsonld_products"] == 1 and sig["shop_like"]
    sn = L.snippets(body, "https://w.com/")
    assert sn["title"] == "Woolly" and "Wool Beanie" in sn["links"] and "Cart" not in sn["links"]


def test_the_sonnet_agent_sees_a_wider_page_and_clicks_in_the_same_wider_list():
    from anyuse.execution import agent_browser as AB
    obs, pick = L.wide_js()
    assert ".slice(0, 60);" in obs and ".slice(0, 60);" in pick and ".slice(0, 40);" in AB.OBSERVE_JS
    assert "slice(0, 3000)" in obs and "slice(0, 2500)" in AB.OBSERVE_JS


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
