"""Saved browser-replayed web tools over MCP, and the verified model fallback on a miss (docs/MCP_WEB_TOOLS.md;
``execution/web_tool_call.py``). Fakes and recorded pages only: no browser, no network, no model.

What must hold:

1. the default profile lists one typed tool per READY saved web tool (``<host>__<workflow>``, one input ``value``,
   a typed output schema, ``_meta.anyuse.kind == "web_tool"``; a browser-only host is marked); a draft, a failed
   proof or a tool whose digest does not match its file is not listed; the meta profile serves the same tool as a
   ``call_operation`` row (kind ``web_tool``) through the SAME route call (same arguments);
2. the call: ``ok`` (``verification`` verified when the attribute verifier accepted the run, plain otherwise),
   ``needs_review`` (``review_view`` with both sources), ``withheld`` / ``failed`` / ``refused`` with a typed
   ``reason_code``; no container browser endpoint (or one that is not a throwaway browser) is the typed refusal
   ``no_browser`` and nothing is sent; a browser that does not answer is ``infrastructure`` before any request; a
   government host is refused; a block is booked (the site is not contacted again) and a traffic violation stops the
   route; meta maps each to its class;
3. the default replay is the CLI's own (``watch_live.make_replay``, no seam set) and, with the fallback off, no verdict
   store and no ladder (as ``anyuse watch run``);
4. the fallback (``ANYUSE_WEB_FALLBACK``, ON by default): only a miss (failed, incomplete, known wrong) reaches it -
   never needs_review, a block or a refusal; only a VERIFIED answer is ``ok`` with ``source: "fallback"``; a fallback
   needs_review / unverified answer is not ok; a query the tool found ambiguous is not asked of it; a verified one is
   booked (verdict store ``correct`` / ``web_fallback``,
   the ladder's last known good answer, the miss ledger) and the next call of that input replays through the ladder
   (tiers 0-1) with the fallback checker;
5. ``run_fallback`` on recorded pages: the read-only lookup (no fill, login or typing), the independent verifier on the
   page the agent ended on, the identity rule against the input (a list: a title, or the page's heading / result
   count, names the input), the agent's cut references resolved against the page, and the per-run model-call cap;
6. the cascade end to end on a drifted version-10 shop (recorded pages): the replay misses, the verified fallback
   answers and is booked, the next call repairs the tool through the ladder at 0 model calls (source ``ladder``), and
   the call after that is a plain tier-0 replay of the repaired revision;
7. silent known wrongs 0: on EVERY replay (fallback on or off, ladder or not) an answer the verdict store holds as wrong
   is the typed miss ``known_wrong`` - withheld with the fallback off, the fallback's case with it on; a fallback answer
   the store holds as wrong is never ok; ``anyuse watch run`` shows it as a KNOWN WRONG drift, never a content change.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest  # noqa: E402

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import web_tool_call as WT  # noqa: E402
from anyuse.mcpserver.server import create_server  # noqa: E402

EP = "http://192.168.70.9:9222"
NAME = "Akkuschrauber Bosch GSR 12V-15"
PURL = "https://shop.example/p/bosch-gsr-12v-15"


def product_tool(version=13):
    return {"version": version, "workflow": "product_lookup", "site": "shop.example",
            "start_url": "https://shop.example/",
            "search": {"mode": "url", "template": "https://shop.example/search?q={q}",
                       "start_url": "https://shop.example/"},
            "output": ["name", "price", "currency", "availability", "url"],
            "fields": {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"},
            "hosts": ["shop.example"]}


def record(tool, name="lookup", status="ready", passed=True, sha=None):
    return {"format": WT.FORMAT, "workflow": name, "workflow_id": "w1", "kind": "web", "site": tool["site"],
            "value": "Bosch GSR 12V", "tool": tool, "tool_sha256": sha or WT.tool_sha(tool),
            "proof": {"passed": passed, "independent": True, "model_calls": 0,
                      "cases": [{"value": "Makita DF333", "passed": passed}, {"value": "Metabo PowerMaxx",
                                                                              "passed": passed}]},
            "status": status, "status_reasons": [], "compiled_at": "2026-10-12T00:00:00+00:00"}


class FakeStorage:
    def __init__(self, base):
        self.base_dir = Path(base)

    def get_workflow_dir(self, wid):
        return self.base_dir / f"workflow_{wid}"

    def get(self, name):
        return None


def save(base, wid, rec):
    d = os.path.join(base, f"workflow_{wid}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, WT.TOOL_FILE), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


def ok_record(verified=False, value=NAME):
    rec = {"value": value, "status": "ok", "accepted": True, "why": "identity and fields checked",
           "output": {"name": NAME, "price": 89.9, "currency": "EUR", "availability": "Auf Lager", "url": PURL},
           "checks": {}, "pages": [{"url": "https://shop.example/search?q=x", "title": "Suche"},
                                   {"url": PURL, "title": NAME}], "model_calls": 0, "requests": 4, "bytes": 1000}
    if verified:
        rec["checks"]["attribute_verify"] = {"verdict": "accept", "model_calls": 0}
    return rec


def review_record():
    return {"value": NAME, "status": "needs_review", "accepted": False, "output": None, "requests": 3,
            "why": "attribute verify: needs review (availability): the page's two sources disagree",
            "unverified_output": {"name": NAME, "availability": "https://schema.org/BackOrder"},
            "needs_review": {"answer": {"name": NAME, "availability": "https://schema.org/BackOrder"},
                             "disputed": [{"attribute": "availability", "answered": "backorder",
                                           "sources": [{"source": "structured", "reads": "backorder"},
                                                       {"source": "visible", "reads": "in_stock"}]}],
                             "verified": ["name", "url"], "decide": AV.REVIEW_DECIDE}}


class Fixture:
    def __init__(self, tool=None, fallback=False):
        self.dir = tempfile.mkdtemp(prefix="anyuse-wt-")
        self.base = os.path.join(self.dir, "workflows")
        os.makedirs(self.base)
        self.tool = tool or product_tool()
        save(self.base, "w1", record(self.tool))
        self.storage = FakeStorage(self.base)
        self.route = WT.WebToolRoute(storage=self.storage, root=os.path.join(self.dir, "web_tools"), endpoint=EP,
                                     fallback=fallback)
        self.replays, self.fallbacks = [], []
        self.next_replay = ok_record()
        self.next_fallback = None
        self.route.check_endpoint = lambda ep: None
        self.route.probe = lambda ep: (True, "fake browser")
        self.route.pace = lambda host: 0.0
        # the fake fallback below stands for a configured model (without a key the route
        # never tries the fallback - test_no_model_key_types_the_miss_and_contacts_nothing)
        self.route.model_ready = lambda: True

        def replay(entry, value, caps, ladder=None):
            self.replays.append({"name": entry["name"], "value": value, "caps": dict(caps), "ladder": ladder})
            return json.loads(json.dumps(self.next_replay))

        def fallback(entry, value, caps):
            self.fallbacks.append({"name": entry["name"], "value": value, "caps": dict(caps)})
            return json.loads(json.dumps(self.next_fallback))
        self.route.replay, self.route.fallback = replay, fallback

    def server(self, profile="default"):
        return create_server(profile=profile, compiled_storage=self.storage,
                             compiled_hosts_path=os.path.join(self.dir, "hosts.json"),
                             skills_dir=os.path.join(self.dir, "skills"), web_route=self.route)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def listed(server):
    return asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest())).root


def call(server, name, arguments=None):
    req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    res = asyncio.run(server.request_handlers[CallToolRequest](req)).root
    return json.loads(res.content[0].text)


TYPED = "shop_example__lookup"


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_default_profile_lists_the_typed_web_tool(self):
        tools = {t.name: t for t in listed(self.fx.server()).tools}
        self.assertIn(TYPED, tools)
        t = tools[TYPED]
        self.assertEqual(t.inputSchema["required"], ["value"])
        self.assertEqual(set(t.inputSchema["properties"]), {"value"})
        self.assertIn("ok", t.outputSchema["properties"])
        meta = t.meta["anyuse"]
        self.assertEqual((meta["kind"], meta["workflow"], meta["tool_version"], meta["browser_only"]),
                         ("web_tool", "product_lookup", 13, False))
        self.assertNotIn(NAME, t.description)              # no page text, no recorded value in the instruction

    def test_browser_only_host_is_marked(self):
        with mock.patch("anyuse.execution.blocked.is_browser_only", return_value={"reason": "403"}):
            t = {t.name: t for t in listed(self.fx.server()).tools}[TYPED]
        self.assertTrue(t.meta["anyuse"]["browser_only"])

    def test_draft_failed_proof_and_changed_tool_are_not_listed(self):
        tool2 = dict(product_tool(), site="b.example", search={"mode": "url", "template": "https://b.example/s?q={q}"})
        save(self.fx.base, "w2", record(tool2, name="draft", status="draft"))
        save(self.fx.base, "w3", record(tool2, name="unproved", passed=False))
        save(self.fx.base, "w4", record(tool2, name="changed", sha="0" * 64))
        names = [s["name"] for s in WT.tool_specs(self.fx.storage)]
        self.assertEqual(names, [TYPED])

    def test_meta_lists_the_tool_as_an_operation(self):
        server = self.fx.server("meta")
        with mock.patch.dict(os.environ, {WT.ENDPOINT_ENV: EP}):
            sites = call(server, "list_sites")
            ops = call(server, "list_operations", {"site": "shop.example"})
        self.assertIn({"site": "shop.example", "kinds": ["web_tool"], "operations": 1, "ready": 1}, sites["sites"])
        row = ops["operations"][0]
        self.assertEqual((row["operation"], row["kind"], row["ready"], row["risk_class"]),
                         ("lookup", "web_tool", True, "read_only"))
        self.assertEqual(row["parameters"][0]["name"], "value")
        self.assertEqual(row["parameters"][0]["example"], "Makita DF333")
        with mock.patch.dict(os.environ, {WT.ENDPOINT_ENV: ""}):
            row = call(server, "list_operations", {"site": "shop.example"})["operations"][0]
        self.assertFalse(row["ready"])
        self.assertIn(WT.ENDPOINT_ENV, row["why_not"])


class CallTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_ok_plain_and_verified_and_meta_parity(self):
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertTrue(out["ok"])
        self.assertEqual((out["status"], out["verification"], out["source"]), ("ok", "plain", "replay"))
        self.assertEqual(out["result"]["url"], PURL)
        self.fx.next_replay = ok_record(verified=True)
        meta = call(self.fx.server("meta"), "call_operation",
                    {"site": "shop.example", "operation": "lookup", "args": {"value": NAME}})
        self.assertTrue(meta["ok"])
        self.assertEqual((meta["kind"], meta["verification"], meta["served_by"]), ("web_tool", "verified",
                                                                                   "browser_replay"))
        self.assertEqual(self.fx.replays[0]["value"], self.fx.replays[1]["value"])
        self.assertEqual(self.fx.replays[0]["name"], self.fx.replays[1]["name"])
        self.assertEqual(self.fx.replays[0]["caps"], self.fx.replays[1]["caps"])

    def test_needs_review_carries_both_sources(self):
        self.fx.next_replay = review_record()
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], "needs_review")
        srcs = out["review_view"]["disputed"][0]["sources"]
        self.assertEqual([s["source"] for s in srcs], ["structured", "visible"])
        meta = call(self.fx.server("meta"), "call_operation",
                    {"site": "shop.example", "operation": "lookup", "args": {"value": NAME}})
        self.assertEqual(meta["class"], "needs_review")
        self.assertEqual(meta["review_view"]["status"], "needs_review")
        self.assertTrue(meta["next"].startswith("hand to the user"))

    def test_withheld_and_failed_carry_typed_reasons(self):
        self.fx.next_replay = dict(ok_record(), status="incomplete", accepted=False, output=None,
                                   why="attribute verify: availability withheld")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["ok"], out["status"], out["reason_code"]), (False, "withheld", "unverified"))
        self.assertEqual(out["fallback"], {"tried": False, "why": "ANYUSE_WEB_FALLBACK is off"})
        self.assertNotIn("result", out)
        self.fx.next_replay = dict(ok_record(), status="failed", accepted=False, output=None,
                                   why="identity: the product page names 60% of the input's words")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["status"], out["reason_code"]), ("failed", "check_failed"))
        meta = call(self.fx.server("meta"), "call_operation",
                    {"site": "shop.example", "operation": "lookup", "args": {"value": NAME}})
        self.assertEqual(meta["class"], "failed")
        self.assertEqual(self.fx.fallbacks, [])

    def test_no_browser_is_a_typed_refusal_and_sends_nothing(self):
        self.fx.route._endpoint = ""
        with mock.patch.dict(os.environ, {WT.ENDPOINT_ENV: ""}):
            out = call(self.fx.server(), TYPED, {"value": NAME})
            meta = call(self.fx.server("meta"), "call_operation",
                        {"site": "shop.example", "operation": "lookup", "args": {"value": NAME}})
        self.assertEqual((out["ok"], out["status"], out["reason_code"]), (False, "refused", "no_browser"))
        self.assertEqual(meta["class"], "no_browser")
        self.fx.route._endpoint = "http://127.0.0.1:9222"

        def refuse(ep):
            raise RuntimeError("a loopback endpoint is most likely the user's own Chrome")
        self.fx.route.check_endpoint = refuse
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual(out["reason_code"], "no_browser")
        self.assertEqual(self.fx.replays, [])

    def test_browser_down_gov_block_and_hard_stop(self):
        self.fx.route.probe = lambda ep: (False, "TimeoutError")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["status"], out["reason_code"], out["requests"]), ("failed", "infrastructure", 0))
        self.assertEqual(self.fx.replays, [])
        self.fx.route.probe = lambda ep: (True, "ok")
        self.fx.next_replay = dict(ok_record(), status="blocked", accepted=False, output=None, why="HTTP 403")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual(out["reason_code"], "blocked")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["status"], out["reason_code"]), ("refused", "site_blocked"))
        self.assertEqual(len(self.fx.replays), 1)
        fx2 = Fixture()
        try:
            fx2.next_replay = dict(ok_record(), violations=["a third-party request"])
            call(fx2.server(), TYPED, {"value": NAME})
            out = call(fx2.server(), TYPED, {"value": NAME})
            self.assertEqual(out["reason_code"], "hard_stop")
            self.assertEqual(len(fx2.replays), 1)
        finally:
            fx2.close()
        gov = dict(product_tool(), site="www.bund.de", search={"mode": "url", "template": "https://www.bund.de/s?q={q}"})
        fx3 = Fixture(tool=gov)
        old = os.environ.get("ANYUSE_REFUSE_GOV_HOSTS")
        os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = "on"         # the government rule refuses before any request
        try:
            out = call(fx3.server(), "www.bund.de.lookup", {"value": NAME})
            self.assertEqual((out["status"], out["reason_code"]), ("refused", "refused"))
            self.assertEqual(fx3.replays, [])
        finally:
            if old is None:
                os.environ.pop("ANYUSE_REFUSE_GOV_HOSTS", None)
            else:
                os.environ["ANYUSE_REFUSE_GOV_HOSTS"] = old
            fx3.close()

    def test_traffic_is_booked_in_the_route_ledger(self):
        call(self.fx.server(), TYPED, {"value": NAME})
        day = self.fx.route.clock().strftime("%Y-%m-%d")
        booked = self.fx.route.traffic().traffic()["sites"]["shop.example"][day]
        self.assertEqual((booked["requests"], booked["contacts"]), (4, 1))

    def test_the_default_replay_is_the_cli_replay_without_seams(self):
        got = {}

        def fake_make_replay(endpoint, cache_dir, log=None, *, contact=None, inner=None):
            got.update(endpoint=endpoint, contact=contact, inner=inner)
            return lambda item, tool, caps: dict(ok_record(), item=item["id"])
        route = WT.WebToolRoute(storage=self.fx.storage, root=self.fx.route.root, endpoint=EP, fallback=False)
        entry = WT.saved_tools(self.fx.storage)[0]
        with mock.patch("anyuse.execution.watch_live.make_replay", fake_make_replay):
            rec = route._live_replay(entry, NAME, {"requests": 10, "bytes": 1000}, route.ladder_for(entry["tool"],
                                                                                                    NAME))
        self.assertEqual((got["endpoint"], got["contact"], got["inner"]), (EP, None, None))
        self.assertEqual(rec["status"], "ok")


class FallbackRouteTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(fallback=True)

    def tearDown(self):
        self.fx.close()

    def verified(self):
        return {"value": NAME, "status": "ok", "accepted": True, "source": "fallback",
                "output": {"name": NAME, "price": 89.9, "currency": "EUR", "availability": "Auf Lager", "url": PURL},
                "picked": {"url": PURL, "title": NAME}, "why": "fallback answer verified", "model_calls": 5,
                "cost_usd": 0.0031, "requests": 3, "checks": {"attribute_verify": {"verdict": "accept"}},
                "pages": [{"url": PURL, "title": NAME}]}

    def test_on_by_default_and_off_still_turns_it_off(self):
        # the fallback is ON by default (about US$0.50 per 1,000 runs); off / 0 turn it off
        from anyuse import settings as S
        self.assertIs(S.SETTINGS["ANYUSE_WEB_FALLBACK"].default, True)
        route = WT.WebToolRoute(storage=self.fx.storage, root=self.fx.route.root, endpoint=EP)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANYUSE_WEB_FALLBACK", None)
            self.assertTrue(WT.fallback_enabled() and route.fallback_on())
        for off in ("off", "0"):
            with mock.patch.dict(os.environ, {"ANYUSE_WEB_FALLBACK": off}):
                self.assertFalse(route.fallback_on())

    def test_no_model_key_types_the_miss_and_contacts_nothing(self):
        # on by default, but without a model key the fallback is never tried - a typed reason, no crash,
        # no fallback contact, never ok
        self.fx.next_replay = dict(ok_record(), status="failed", accepted=False, output=None,
                                   why="membership: no item of the compiled region")
        self.fx.route.model_ready = WT.WebToolRoute.model_ready.__get__(self.fx.route)    # the product's own check
        with mock.patch.dict(os.environ, {"CEREBRAS_API_KEY": "", "ANYUSE_MODEL_ENV_FILE": os.devnull}):
            name = WT.tool_specs(self.fx.storage)[0]["name"]
            out = call(self.fx.server(), name, {"value": NAME})
        self.assertFalse(out["ok"])
        self.assertEqual(out["fallback"]["reason_code"], WT.NO_MODEL)
        self.assertFalse(out["fallback"]["tried"])
        self.assertEqual(self.fx.fallbacks, [])

    def test_a_miss_is_answered_only_by_a_verified_fallback_and_booked(self):
        self.fx.next_replay = dict(ok_record(), status="failed", accepted=False, output=None,
                                   why="membership: no item of the compiled region")
        self.fx.next_fallback = self.verified()
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertTrue(out["ok"])
        self.assertEqual((out["source"], out["verification"], out["model_calls"]), ("fallback", "verified", 5))
        self.assertEqual(out["replay"]["reason_code"], "check_failed")
        caps0, caps1 = self.fx.replays[0]["caps"], self.fx.fallbacks[0]["caps"]
        self.assertEqual((caps1["requests"], caps1["bytes"]), (caps0["requests"] - 4, caps0["bytes"] - 1000))
        self.assertEqual(out["fallback"]["booked"], {"verdict_store": True, "ladder_lkg": True, "ledger": True})
        rows = self.fx.route.verdicts().rows()
        self.assertEqual([(r["verdict"], r["source"], r["ref"]) for r in rows], [("correct", "checker", "web_fallback")])
        self.assertEqual(self.fx.route.booked(self.fx.tool, NAME)["url"], PURL)
        self.assertEqual(self.fx.replays[0]["ladder"], None)
        # the next call of that input replays through the ladder (tiers 0-1) with the fallback checker
        self.fx.next_replay = ok_record()
        call(self.fx.server(), TYPED, {"value": NAME})
        lad = self.fx.replays[1]["ladder"]
        self.assertTrue(lad["config"].enabled)
        self.assertFalse(lad["config"].tier2 or lad["config"].tier3)
        self.assertIsNotNone(lad["store"].lkg(NAME))
        agree = lad["checker"](self.fx.tool, NAME, ok_record())
        self.assertEqual(agree["verdict"], "agrees")
        other = dict(ok_record(), output=dict(ok_record()["output"], url="https://shop.example/p/other"))
        self.assertEqual(lad["checker"](self.fx.tool, NAME, other)["verdict"], "undeterminable")

    def test_never_for_needs_review_a_block_or_a_refusal(self):
        for rec in (review_record(), dict(ok_record(), status="blocked", accepted=False, output=None, why="403"),
                    dict(ok_record(), status="refused", accepted=False, output=None, why="robots.txt")):
            fx = Fixture(fallback=True)
            try:
                fx.next_replay = rec
                out = call(fx.server(), TYPED, {"value": NAME})
                self.assertFalse(out["ok"])
                self.assertEqual(fx.fallbacks, [])
            finally:
                fx.close()

    def test_an_ambiguous_query_is_not_asked_of_the_fallback(self):
        self.fx.next_replay = dict(ok_record(), status="incomplete", accepted=False, output=None,
                                   why="incomplete: ambiguous: 12 of the 20 results naming every word of the input "
                                       "match it equally closely")
        self.fx.next_fallback = self.verified()
        out = call(self.fx.server(), TYPED, {"value": "CALVIN KLEIN"})
        self.assertEqual((out["ok"], out["status"]), (False, "withheld"))
        self.assertFalse(out["fallback"]["tried"])
        self.assertIn("ambiguous", out["fallback"]["why"])
        self.assertEqual(self.fx.fallbacks, [])

    def test_an_unverified_or_disputed_fallback_answer_is_not_ok(self):
        self.fx.next_replay = dict(ok_record(), status="incomplete", accepted=False, output=None,
                                   why="attribute verify: price withheld")
        self.fx.next_fallback = dict(self.verified(), status="incomplete", accepted=False, output=None,
                                     unverified_output=self.verified()["output"], why="attribute verify: withheld")
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["ok"], out["status"]), (False, "withheld"))
        self.assertEqual(out["fallback"]["status"], "incomplete")
        self.assertNotIn("result", out)
        self.assertEqual(self.fx.route.verdicts().rows(), [])
        self.fx.next_fallback = dict(review_record(), model_calls=4, cost_usd=0.002)
        out = call(self.fx.server(), TYPED, {"value": NAME})
        self.assertEqual((out["ok"], out["status"], out["source"]), (False, "needs_review", "fallback"))
        self.assertEqual(out["review_view"]["status"], "needs_review")

    def test_a_list_tool_below_version_10_books_no_ladder_answer(self):
        fx = Fixture(tool=product_tool(version=9), fallback=True)
        try:
            fx.next_replay = dict(ok_record(), status="failed", accepted=False, output=None, why="identity")
            fx.next_fallback = self.verified()
            out = call(fx.server(), TYPED, {"value": NAME})
            self.assertEqual(out["fallback"]["booked"]["ladder_lkg"], False)
            self.assertIsNone(fx.route.ladder_for(fx.tool, NAME))
        finally:
            fx.close()


def book_wrong(route, tool, value, output, status="ok"):
    from anyuse.execution import demo_compile as C
    route.verdicts().add(tool=C.tool_digest(tool), site=tool["site"], value=value, status=status, output=output,
                         verdict="wrong", source="audit", ref="test", why="another product")


class KnownWrongTests(unittest.TestCase):
    def test_off_a_known_wrong_replay_is_withheld(self):
        fx = Fixture(fallback=False)
        try:
            book_wrong(fx.route, fx.tool, NAME, ok_record()["output"])
            out = call(fx.server(), TYPED, {"value": NAME})
            self.assertEqual((out["ok"], out["status"], out["reason_code"]), (False, "withheld", "known_wrong"))
            self.assertEqual((out["known_wrong"]["verdict"], out["known_wrong"]["source"]), ("wrong", "audit"))
            self.assertNotIn("result", out)
            meta = call(fx.server("meta"), "call_operation",
                        {"site": "shop.example", "operation": "lookup", "args": {"value": NAME}})
            self.assertEqual((meta["ok"], meta["class"]), (False, "withheld"))
            # another answer for the same value is not affected
            fx.next_replay = dict(ok_record(), output=dict(ok_record()["output"], price=79.9))
            self.assertTrue(call(fx.server(), TYPED, {"value": NAME})["ok"])
        finally:
            fx.close()

    def test_on_a_known_wrong_replay_goes_to_the_fallback_and_a_known_wrong_fallback_is_not_ok(self):
        fx = Fixture(fallback=True)
        try:
            book_wrong(fx.route, fx.tool, NAME, ok_record()["output"])
            good = dict(FallbackRouteTests.verified(None), output=dict(ok_record()["output"], price=79.9))
            fx.next_fallback = good
            out = call(fx.server(), TYPED, {"value": NAME})
            self.assertEqual((out["ok"], out["source"], out["replay"]["reason_code"]), (True, "fallback", "known_wrong"))
            self.assertEqual(len(fx.fallbacks), 1)
            fx.next_fallback = dict(good, output=ok_record()["output"])      # the fallback finds the wrong one too
            out = call(fx.server(), TYPED, {"value": NAME})
            self.assertEqual((out["ok"], out["status"]), (False, "withheld"))
            self.assertEqual(out["fallback"]["status"], "known_wrong")
        finally:
            fx.close()

    def test_the_ladder_path_is_checked_too(self):
        fx = Fixture(fallback=True)
        try:
            book_wrong(fx.route, fx.tool, NAME, ok_record()["output"])
            fx.route.ladder_for = lambda tool, value: {"config": None}         # any ladder run: the check follows it
            fx.next_fallback = dict(review_record())
            out = call(fx.server(), TYPED, {"value": NAME})
            self.assertFalse(out["ok"])
            self.assertEqual(out["replay"]["reason_code"], "known_wrong")
            self.assertIsNotNone(fx.replays[0]["ladder"])
        finally:
            fx.close()

    def test_watch_run_marks_a_known_wrong_answer(self):
        from anyuse.execution import watch as WA
        from anyuse.execution.verdict_store import VerdictStore
        d = tempfile.mkdtemp(prefix="anyuse-wt-watch-")
        try:
            tool = product_tool(version=7)
            tool["workflow"] = "product_lookup"
            tp = os.path.join(d, "t.tool.json")
            with open(tp, "w") as fh:
                json.dump(tool, fh)
            st = WA.WatchStore(os.path.join(d, "watch"))
            st.add(tp, NAME, "1d")
            vs = VerdictStore(os.path.join(d, "verdicts.jsonl"))
            from anyuse.execution import demo_compile as C
            vs.add(tool=C.tool_digest(tool), site=tool["site"], value=NAME, status="ok",
                   output=ok_record()["output"], verdict="wrong", source="audit", ref="test", why="another product")
            rep = WA.run_watch(st, lambda item, t, caps: dict(ok_record(), requests=2, bytes=10), verdicts=vs)
            e = rep["entries"][0]
            self.assertEqual((e["kind"], e["drift"]["status"]), ("drift", "known_wrong"))
            self.assertIn("KNOWN WRONG", WA.render_entry(e))
            self.assertIsNone(st.items()[e["item"]]["last_good"])           # never a baseline
            self.assertIn("KNOWN WRONG", WA.hook_text(st))
            # without the store the CLI path is unchanged
            st2 = WA.WatchStore(os.path.join(d, "watch2"))
            st2.add(tp, NAME, "1d")
            rep = WA.run_watch(st2, lambda item, t, caps: dict(ok_record(), requests=2, bytes=10))
            self.assertEqual(rep["entries"][0]["kind"], "baseline")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# run_fallback on recorded pages
# ---------------------------------------------------------------------------

def search_dom(links):
    items = "".join(f'<li class="product"><a href="{u}">{t}</a> <span>89,90 €</span></li>' for u, t in links)
    return (f"<html><head><title>Suche | Shop</title></head><body><header><nav><a href='/'>Start</a></nav></header>"
            f"<main><h1>Suchergebnisse</h1><ul>{items}</ul></main></body></html>")


def product_dom(name=NAME, price="89.90", availability="InStock", url=PURL):
    ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": name,
                     "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                                "availability": f"https://schema.org/{availability}"}})
    return (f'<html><head><title>{name} | Shop</title><script type="application/ld+json">{ld}</script>'
            f'<link rel="canonical" href="{url}"></head><body><main><h1>{name}</h1>'
            f'<p class="price">89,90 €</p><p>Auf Lager</p><button>In den Warenkorb</button>'
            f'<p>Beschreibung: ein Akkuschrauber.</p></main></body></html>')


def site(value=NAME, product=None):
    from anyuse.execution import demo_compile as C
    s = D.DomSite()
    s.add(D.Recorded(C.fill(product_tool()["search"]["template"], value),
                     search_dom([(PURL, NAME), ("https://shop.example/p/other", "Bosch GSR 18V Koffer")])))
    s.add(D.Recorded(PURL, product or product_dom()))
    return s


def click_to(href_part):
    def item(task, obs, history):
        for e in obs.elements:
            if href_part in (e.href or ""):
                return {"action": "click", "element": e.index}
        return {"action": "fail", "reason": "no such link"}
    return item


def done(**result):
    return {"action": "done", "result": result}


class RunFallbackTests(unittest.TestCase):
    def run_it(self, script, s=None, model=None, cap=WT.MAX_CALLS, policy=None):
        page = D.DomPage(s or site())
        return WT.run_fallback(page, product_tool(), NAME, policy=policy or P.ScriptedPolicy(script), model=model,
                               cap=cap)

    def test_a_verified_answer_is_ok(self):
        out = self.run_it([click_to("/p/bosch"), done(name=NAME, price="89,90 €", availability="Auf Lager",
                                                      url=PURL)])
        self.assertEqual(out["status"], "ok", out.get("why"))
        self.assertTrue(out["accepted"])
        self.assertEqual(out["output"]["price"], 89.9)
        self.assertEqual(out["output"]["currency"], "EUR")
        self.assertEqual(out["checks"]["attribute_verify"]["verdict"], "accept")

    def test_another_product_fails_the_identity_rule(self):
        out = self.run_it([click_to("/p/bosch"), done(name="Bosch Koffer", price="89,90 €",
                                                      availability="Auf Lager", url=PURL)])
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("identity", out["why"])

    def test_an_answer_the_page_does_not_support_is_withheld(self):
        out = self.run_it([click_to("/p/bosch"), done(name=NAME, price="79,90 €", availability="Auf Lager",
                                                      url=PURL)])
        self.assertNotEqual(out["status"], "ok")
        self.assertFalse(out["accepted"])

    def test_a_missing_field_and_a_no_results_answer_are_not_accepted(self):
        out = self.run_it([click_to("/p/bosch"), done(name=NAME, url=PURL)])
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("lacks", out["why"])
        out = self.run_it([done(no_results=True)])
        self.assertEqual(out["status"], "failed")

    def test_the_lookup_is_read_only(self):
        tb = WT._lookup_toolbox(__import__("anyuse.execution.harness_tools", fromlist=["x"]).BrowserTools(
            D.DomPage(site()), "https://shop.example/", "shop.example"))
        avail = tb.available()
        for k in ("fill", "login", "type", "http_get", "write_file"):
            self.assertNotIn(k, avail)
        from anyuse.execution import harness_types as T
        why, _hard = tb.gate(T.action_from_dict({"action": "login"}), T.Observation(kind="web"))
        self.assertIn("read-only lookup", why)

    def test_the_model_call_cap_holds_for_agent_and_verifier_together(self):
        from anyuse.execution.model_adapter import note_external_model_call

        class Counting:
            def __init__(self, script):
                self.inner = P.ScriptedPolicy(script)

            def decide(self, *a):
                note_external_model_call("test", "fake", feature="web_fallback")
                return self.inner.decide(*a)
        asked = []

        def model(prompt, schema):
            asked.append(1)
            note_external_model_call("test", "fake", feature="attribute_verify")
            return {"choice": "unclear", "calls": 1, "usd": 0.0}
        # an endless agent stops at MAX_CALLS - VERIFY_RESERVE decisions
        out = self.run_it([{"action": "scroll", "direction": "down"}] * 20, policy=Counting(
            [{"action": "scroll", "direction": "down"}] * 20), model=model)
        self.assertLessEqual(out["model_calls"], WT.MAX_CALLS - WT.VERIFY_RESERVE)
        # a disputed page: the verifier asks only within what is left of the cap
        prod = product_dom().replace("<p>Auf Lager</p>", "<p>Lieferbar in Kürze, Bestellung möglich</p>")
        out = self.run_it(None, s=site(product=prod), model=model, cap=3, policy=Counting(
            [click_to("/p/bosch"), done(name=NAME, price="89,90 €", availability="Auf Lager", url=PURL)]))
        self.assertLessEqual(out["model_calls"], 3)
        self.assertNotEqual(out["status"], "ok")


class ResolveTests(unittest.TestCase):
    def test_cut_references_are_resolved_against_the_page(self):
        long_url = "https://news.example/cronaca/lotta-al-bullismo-dalla-provincia-di-trento-fino-a-7-mila-euro"
        title = "Lotta al bullismo, dalla Provincia di Trento fino a 7 mila euro per ogni scuola della provincia"
        dom = (f"<html><body><main><ul><li><a href='{long_url}'>{title}</a></li>"
               f"<li><a href='https://news.example/other'>Altro</a></li></ul></main></body></html>")
        out = {"results": [{"title": title[:80], "url": long_url[:70]}]}
        got = WT.resolve_on_page("site_search", out, dom, "https://news.example/search?q=bullismo")
        self.assertEqual(got["results"][0], {"title": title, "url": long_url})
        # a URL the page does not hold stays as answered (the verifier refuses it)
        out = {"results": [{"title": "X", "url": "https://news.example/invented-article-that-is-not-there"}]}
        self.assertEqual(WT.resolve_on_page("site_search", out, dom, "https://news.example/s")["results"][0]["url"],
                         "https://news.example/invented-article-that-is-not-there")
        # a product's cut URL is the loaded page; another page stays as answered
        got = WT.resolve_on_page("product_lookup", {"name": NAME, "url": PURL[:25]}, "", PURL)
        self.assertEqual(got["url"], PURL)
        got = WT.resolve_on_page("product_lookup", {"name": NAME, "url": "https://shop.example/p/x"}, "", PURL)
        self.assertEqual(got["url"], "https://shop.example/p/x")

    def test_a_list_identity_by_the_page_heading(self):
        out = {"results": [{"title": "Wileyfox Spark im Test", "url": "https://n.example/a"}]}
        tool = {"workflow": "site_search"}
        self.assertIn("names the input", WT.identity_problem("site_search", "testen", out, tool, None))
        page = AV.Page("<html><body><h1>Suchergebnisse für testen</h1></body></html>", "Suchergebnisse für testen",
                       "https://n.example/?s=testen")
        self.assertEqual(WT.identity_problem("site_search", "testen", out, tool, page), "")


class CascadeTests(unittest.TestCase):
    """The whole cascade on the ladder tests' drifted shop (recorded pages; fakes for the guard only)."""

    def test_miss_fallback_booking_ladder_repair_then_zero_calls(self):
        import test_repair_ladder as TL
        from anyuse.execution import demo_tool as T
        v = "Bosch GSR 12V Akkuschrauber"
        tool = TL.compiled(v)
        self.assertEqual(tool["version"], 10)
        shop = TL.Shop(cls=TL.RENAMED)
        fx = Fixture(tool=dict(tool), fallback=True)
        # the cascade's mechanics with the replay's own checks and the fallback checker (as written): the in-run
        # verifier is pinned off - on by default, it withholds the repaired price without a model key
        os.environ[AV.SETTING], _old_av = "off", os.environ.get(AV.SETTING)
        try:
            def replay(entry, value, caps, ladder=None):
                fx.replays.append({"value": value, "ladder": ladder})
                if ladder is None:
                    return asyncio.run(T.run_tool(D.DomPage(shop), entry["tool"], value, D.no_settle,
                                                  result_wait_s=0.0))
                return asyncio.run(WT.ladder_run(lambda: D.DomPage(shop), entry["tool"], value, D.no_settle, ladder,
                                                 0.0))
            purl = TL.BASE + "/p/bosch-gsr-12v"

            def fallback(entry, value, caps):
                pol = P.ScriptedPolicy([{"action": "open", "url": TL.BASE + "/search?q=Bosch+GSR+12V"},
                                        click_to("/p/bosch-gsr-12v"),
                                        done(name=v, price="89,90 €", availability="Auf Lager", url=purl)])
                return WT.run_fallback(D.DomPage(shop), entry["tool"], value, policy=pol, model=None)
            fx.route.replay, fx.route.fallback = replay, fallback
            name = WT.tool_specs(fx.storage)[0]["name"]
            first = call(fx.server(), name, {"value": v})
            self.assertTrue(first["ok"], first)
            self.assertEqual((first["source"], first["replay"]["status"]), ("fallback", "failed"))
            self.assertTrue(first["fallback"]["booked"]["ladder_lkg"])
            second = call(fx.server(), name, {"value": v})
            self.assertTrue(second["ok"], second)
            self.assertEqual((second["source"], second["model_calls"]), ("ladder", 0))
            self.assertEqual((second["ladder"]["outcome"], second["ladder"]["tier"]), ("repaired", 1))
            self.assertNotIn("fallback", second)
            third = call(fx.server(), name, {"value": v})
            self.assertEqual((third["ok"], third["source"], third["ladder"]["tier"]), (True, "replay", 0))
            self.assertEqual(third["result"]["url"], purl)
        finally:
            fx.close()
            if _old_av is None:
                os.environ.pop(AV.SETTING, None)
            else:
                os.environ[AV.SETTING] = _old_av


if __name__ == "__main__":
    unittest.main()
