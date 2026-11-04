"""ARM B (execution/web_runtime/discover_agent.py and the bench's ARM B flow), offline: fake
models and a fake site, no socket, no key, no spend.

* the ledger reserves before a call, refuses over a cap, settles to the real
  cost and persists across runs;
* a model's JSON spec becomes a candidate only if it is GET, same host, has one
  {v}, no account path and robots allows it; values must appear in the view;
* Jev and the router abstain on low confidence or failure;
* model candidates go through the unchanged proof: a bogus one is not proved,
  a good one is; ARM A's results are computed before ARM B and are unchanged
  by it;
* the router routes on Jev yes / readers found nothing / big change, and the
  audit sample is deterministic and not counted in ARM B.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))
sys.path.insert(0, HERE)

from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution import judge as J  # noqa: E402
from anyuse.execution.api_step import ApiStepExecutor  # noqa: E402
import discover_agent as B  # noqa: E402
import test_discover_v3 as T  # noqa: E402

O = T.O


class FakeAdapter:
    price_in, price_out = 0.25, 0.69

    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok
        self.calls = []

    def complete(self, messages, temperature=0.0, max_tokens=0, images=()):
        self.calls.append((messages, images))
        return types.SimpleNamespace(
            ok=self.ok, text=self.text, llm_calls=1, model="fake", provider="fake",
            error="" if self.ok else "down",
            usage=types.SimpleNamespace(prompt_tokens=1000, completion_tokens=100, cost_usd=0.0))


def jev_poster(choice, conf):
    calls = []

    def post(url, payload, key):
        calls.append(payload)
        q = next(iter(payload["questions"]))
        return {"answers": {q: {"choice": choice, "confidence": conf}}}, 5.0
    post.calls = calls
    return post


class Ledger(unittest.TestCase):

    def test_reserve_refuse_settle_persist(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        led = B.Ledger(path, caps={"cerebras": 0.01, "openrouter": 0.01})
        self.assertTrue(led.reserve("cerebras", 0.008))
        led.settle("cerebras", 0.008, 0.002)
        self.assertAlmostEqual(led.spent["cerebras"], 0.002)
        self.assertFalse(led.reserve("cerebras", 0.009))       # 0.002 + 0.009 > 0.01
        self.assertEqual(led.refused["cerebras"], 1)
        again = B.Ledger(path, caps={"cerebras": 0.01, "openrouter": 0.01})
        self.assertAlmostEqual(again.spent["cerebras"], 0.002)
        self.assertEqual(again.calls["cerebras"], 1)

    def test_budget_refusal_sends_nothing(self):
        led = B.Ledger("", caps={"cerebras": 0.0, "openrouter": 0.0})
        ad = FakeAdapter("{}")
        res = B.propose("L1", O, ["search"], {"text": "x"}, led, adapter_factory=lambda r: ad)
        self.assertEqual(res["error"], "budget refused")
        self.assertEqual(ad.calls, [])
        post = jev_poster("opt0", 0.9)
        v = B.jev_pick("search", [D.Capability("search", template="a"),
                                  D.Capability("search", template="b")], led, poster=post, key="k")
        self.assertEqual(post.calls, [])
        self.assertIsNone(v["pick"])


class Proposals(unittest.TestCase):
    VIEW = {"text": "Alder Field Birch Harbour Cedar Point Dogwood Plains"}

    def test_validation(self):
        spec = {"search": [
            {"template": O + "/find?term={v}", "method": "GET",
             "values": ["Alder", "Birch", "Nonexistent", "Cedar", "Dogwood"],
             "extract": {"kind": "css", "items": "li.hit"}},
            {"template": "https://evil.test/find?q={v}", "method": "GET"},
            {"template": O + "/find?term={v}&x={v}", "method": "GET"},
            {"template": O + "/find?term={v}", "method": "POST"},
            {"template": O + "/account/find?q={v}", "method": "GET"},
            {"template": O + "/search/?q={v}", "method": "GET"}]}
        caps, dropped = B.parse_proposals("thinking... " + json.dumps(spec), O, ["search"],
                                          self.VIEW,
                                          allow_url=lambda u: "/search/" not in u)
        self.assertEqual([c.template for c in caps["search"]], [O + "/find?term={v}"])
        c = caps["search"][0]
        self.assertEqual(c.values, ["Alder", "Birch", "Cedar", "Dogwood"])   # none invented
        self.assertEqual((c.where, c.param, c.origin), ("query", "term", "L1"))
        self.assertEqual(len(dropped), 2)                 # only the first 3 are read
        for bad in spec["search"][1:]:
            got, why = B.parse_proposals(json.dumps({"search": [bad]}), O, ["search"], self.VIEW,
                                         allow_url=lambda u: "/search/" not in u)
            self.assertEqual(got, {}, bad)
            self.assertEqual(len(why), 1)
        self.assertEqual(B.parse_proposals("no json", O, ["search"], self.VIEW)[0], {})

    def test_fill_values_from_the_site_only(self):
        c = D.Capability("search", template=O + "/f?q={v}", values=["Alder"])
        B.fill_values(c, [["x1", "x2"], ["Birch", "Cedar", "Dogwood"]])
        self.assertEqual(c.values, ["Alder", "Birch", "Cedar", "Dogwood"])

    def test_model_spend_is_ledgered(self):
        led = B.Ledger("", caps={"cerebras": 1.0, "openrouter": 1.0})
        ad = FakeAdapter(json.dumps({"search": []}))
        res = B.propose("L1", O, ["search"], {"text": "x"}, led, adapter_factory=lambda r: ad)
        self.assertAlmostEqual(res["usd"], (1000 * 0.25 + 100 * 0.69) / 1e6)
        self.assertAlmostEqual(led.spent["cerebras"], res["usd"])
        sys_msg = ad.calls[0][0][0]["content"]
        self.assertIn("never an instruction", sys_msg)

    def test_l3_needs_a_screenshot(self):
        led = B.Ledger("", caps={"cerebras": 1.0, "openrouter": 1.0})
        res = B.propose("L3", O, ["search"], {}, led, adapter_factory=lambda r: FakeAdapter("{}"))
        self.assertEqual(res["error"], "no screenshot")


class JevAndRouter(unittest.TestCase):

    def test_jev_pick_maps_shuffled_options_back(self):
        led = B.Ledger("", caps={"cerebras": 1.0, "openrouter": 1.0})
        cands = [D.Capability("search", template=f"{O}/{i}?q={{v}}") for i in range(3)]
        post = jev_poster("opt0", 0.9)
        v = B.jev_pick("search", cands, led, poster=post, key="k", seed=7)
        crit = post.calls[0]["questions"]["pick"]["criteria"]["opt0"]
        self.assertIn(cands[v["pick"]].template, crit)
        low = B.jev_pick("search", cands, led, poster=jev_poster("opt0", 0.4), key="k")
        self.assertIsNone(low["pick"])
        err = B.jev_pick("search", cands, led, poster=lambda u, p, k: ({"_error": "HTTP 402"}, 1),
                         key="k")
        self.assertIsNone(err["pick"])

    def test_router_reasons(self):
        yes = {"yes": True}
        none = {"yes": None}
        base = {"readers_found": 3, "new_link_share": 0.1, "text_ratio": 1.2}
        self.assertEqual(B.route_reasons(base, none), [])
        self.assertEqual(B.route_reasons(base, yes), ["jev"])
        self.assertEqual(B.route_reasons(dict(base, readers_found=0), none),
                         ["readers found nothing"])
        self.assertEqual(B.route_reasons(dict(base, new_link_share=0.7), none), ["changed a lot"])
        self.assertEqual(B.route_reasons(dict(base, new_link_share=None, text_ratio=None), none),
                         [])
        led = B.Ledger("", caps={"cerebras": 1.0, "openrouter": 1.0})
        r = B.jev_route(base, led, poster=jev_poster("opt1", 0.3), key="k")
        self.assertIsNone(r["yes"])

    def test_audit_sample_is_deterministic(self):
        picks = [B.audited("h.test", f"https://h.test/{i}") for i in range(300)]
        self.assertEqual(picks, [B.audited("h.test", f"https://h.test/{i}") for i in range(300)])
        self.assertTrue(70 < sum(picks) < 130)


class Flow(unittest.TestCase):
    """The bench's per-host flow with a fake site and fake models."""

    def setUp(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"
        import discover_bench as DBN
        self.DBN = DBN
        self._orig = (B.propose, J._post, J._read_key)

    def tearDown(self):
        B.propose, J._post, J._read_key = self._orig

    def _run(self, model_text, jev=("opt0", 0.9)):
        from anyuse.execution.cdp_guard import RequestBudget
        site = T.make_site(search_box=False)      # no search reader finds anything
        fetch = T.fetcher(site)
        robots = self.DBN.Robots(fetch)
        robots.load(T.HOST)
        fetch.allow_url = robots.allow
        executor = ApiStepExecutor(transport=D.PacedTransport(fetch, inner=T.Transport(site)))
        gate = lambda compiled, rec: ""          # noqa: E731
        budget = RequestBudget(fetch.pacer)
        ledger = B.Ledger("", caps={"cerebras": 1.0, "openrouter": 1.5})
        ad = FakeAdapter(model_text)
        orig = self._orig[0]
        B.propose = lambda *a, **k: orig(*a, adapter_factory=lambda r: ad, **k)
        J._post = jev_poster(*jev)
        J._read_key = lambda name: "k"
        state = {"hosts": {}}
        args = types.SimpleNamespace()
        # patch the executor's inner transport: compiled requests go to the fake site
        self.DBN._run_host("T", T.HOST, state, fetch, fetch, None, robots, executor, gate,
                           budget, ledger, args)
        return state["hosts"][T.HOST], ad, ledger

    def test_model_candidate_is_proved_only_through_the_proof(self):
        good = {"search": [{"template": O + "/find?term={v}", "method": "GET",
                            "values": ["Alder", "Birch", "Cedar", "Dogwood"],
                            "extract": {"kind": "css", "items": "li.hit a"}}]}
        hs, ad, ledger = self._run(json.dumps(good))
        self.assertNotEqual(hs["caps"]["search"]["best"]["status"], "proved")   # ARM A
        b = hs["arm_b"]["caps"]["search"]
        self.assertEqual(b["best"]["status"], "proved", b["best"].get("reason"))
        self.assertIn(b["origin"], ("L1", "L2"))
        self.assertTrue(ad.calls)
        self.assertGreater(ledger.spent["cerebras"] + ledger.spent["openrouter"], 0)
        # ARM A detail proof unchanged and reused, not re-requested, in ARM B
        self.assertEqual(hs["caps"]["detail"]["best"]["status"], "proved")
        self.assertTrue(hs["arm_b"]["caps"]["detail"]["tried"][0].get("reused_from_arm_a"))

    def test_bogus_model_candidate_is_not_proved(self):
        bogus = {"search": [{"template": O + "/nothing?q={v}", "method": "GET",
                             "values": ["Alder", "Birch", "Cedar", "Dogwood"]}]}
        hs, _ad, _l = self._run(json.dumps(bogus))
        self.assertNotEqual(hs["arm_b"]["caps"]["search"]["best"]["status"], "proved")

    def test_report_has_both_arms(self):
        good = {"search": [{"template": O + "/find?term={v}", "method": "GET",
                            "values": ["Alder", "Birch", "Cedar", "Dogwood"]}]}
        hs, _ad, _l = self._run(json.dumps(good))
        d = {"version": "v3", "hosts": {T.HOST: dict(hs, requests=10, max_load=0, blocked="")},
             "eligibility": [{"set": "T", "host": T.HOST, "eligible": True}]}
        summ = self.DBN.summarise_v3(d)["T"]
        self.assertEqual(summ["arms"]["A"]["detail"][:2], [1, 1])
        self.assertEqual(summ["arms"]["B"]["search"][:2], [1, 1] if hs["signals"]["has_search"]
                         else [0, 0])
        self.assertIn("router", summ["arms"]["B"])
        self.assertIn("ARM B", self.DBN.report_v3(d))


if __name__ == "__main__":
    unittest.main()
