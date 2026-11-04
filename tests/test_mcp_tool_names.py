"""MCP tool names in the format Anthropic's and OpenAI's tool APIs accept (mcpserver/tool_names.py).

Offline: one server per profile over temp storage with every kind of tool (compiled web requests, a saved browser
web tool, compiled desktop records, skill/routine manifests, the agent tool), fake transports and runners only.

What must hold:

1. every name ``tools/list`` returns, in every profile, matches ``^[a-zA-Z0-9_-]{1,64}$`` and is unique;
2. ``agent.run`` is ``agent_run``; ``<host>.<workflow>`` is ``<host>__<workflow>`` with the host's dots as
   underscores; ``desktop.<workflow>`` is ``desktop__<workflow>``; ``routine.<name>`` is ``routine__<name>``;
3. a name over 64 characters is cut and ends in a hash of the whole name; two that share their first 64 (or 80)
   characters stay apart; tools that would share a name (also across hosts that differ by a port) each get a suffix
   from their own key, so each listed name runs its own workflow and an added tool never takes a name over: the
   shared name, and an older name both had, call neither and name the tools that do;
4. a call by the older dotted name reaches the same tool with the same result (the SDK's input check included), but
   only the new names are listed; every text that names a tool to call names the new one.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest  # noqa: E402

from anyuse.execution import web_tool_call as WT  # noqa: E402
from anyuse.execution.api_compile import compiled_record, persist_compiled  # noqa: E402
from anyuse.execution.api_permission import acknowledge_host  # noqa: E402
from anyuse.execution.api_step import ApiStepExecutor  # noqa: E402
from anyuse.execution.skill_gen import RESUME_TOOL_NAME, skill_md  # noqa: E402
from anyuse.mcpserver import agent_tools as AT  # noqa: E402
from anyuse.mcpserver import compiled_tools as CT  # noqa: E402
from anyuse.mcpserver import desktop_compiled_tools as DT  # noqa: E402
from anyuse.mcpserver import tool_names as TN  # noqa: E402
from anyuse.mcpserver.server import create_server, server_instructions  # noqa: E402

import test_launch_mcp_profile as launch  # noqa: E402
import test_routine_example as rex  # noqa: E402

PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
LONG = "crates_detail_with_a_workflow_name_far_longer_than_any_tool_api_accepts"
WEB_TOOL = {"version": 13, "workflow": "product_lookup", "site": "shop.example", "start_url": "https://shop.example/",
            "search": {"mode": "url", "template": "https://shop.example/search?q={q}",
                       "start_url": "https://shop.example/"},
            "output": ["name", "price", "currency", "availability", "url"],
            "fields": {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"},
            "hosts": ["shop.example"]}


def agent_manifest():
    m = copy.deepcopy(json.load(open(rex.MANIFEST_FILE)))
    m["name"] = "crates_agent_pick"
    pick = next(s for s in m["steps"] if s["id"] == "pick")
    pick["mode"] = "agent"
    pick["action"] = {"kind": "agent_choice", "choose_from": "$in.candidates", "into": "crate"}
    pick["autonomy"] = {"level": "model_proposes_human_confirms"}
    return m


def save_web_tool(base, wid, name):
    d = os.path.join(base, f"workflow_{wid}")
    os.makedirs(d, exist_ok=True)
    rec = {"format": WT.FORMAT, "workflow": name, "workflow_id": wid, "kind": "web", "site": "shop.example",
           "value": "Bosch GSR 12V", "tool": WEB_TOOL, "tool_sha256": WT.tool_sha(WEB_TOOL),
           "proof": {"passed": True, "independent": True, "model_calls": 0,
                     "cases": [{"value": "Makita DF333", "passed": True}, {"value": "Metabo", "passed": True}]},
           "status": "ready", "status_reasons": [], "compiled_at": "2026-10-12T00:00:00+00:00"}
    with open(os.path.join(d, WT.TOOL_FILE), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


class FakeRoute:
    """The saved web tools' call path: records which saved tool each call reached."""

    def __init__(self, storage):
        self.storage = storage
        self.calls = []

    def call(self, entry, arguments):
        self.calls.append((entry["name"], dict(arguments)))
        return {"ok": True, "status": "ok", "result": {"name": "x"}, "source": "replay", "verification": "plain"}


def fake_agent(task, **kw):
    return {"ok": True, "outcome": "done", "reason": "finished", "steps": 1, "decisions": 1, "model_calls": 0,
            "cost_usd": 0.0, "result": {"task": task}, "compiled": None, "run_id": "r1", "policy": kw.get("policy")}


class Fixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="anyuse-names-")
        self.env = mock.patch.dict(os.environ, {"ANYUSE_DATA_DIR": os.path.join(self.dir, "data")})
        self.env.start()
        self.storage, self.hosts = rex.setup(self.dir)
        for name in (LONG + "_a", LONG + "_b", "dup.name", "dup_name"):
            persist_compiled(self.storage, name, compiled_record(rex.detail_template(), rex.PROOF, workflow=name))
        save_web_tool(self.storage.base_dir, "wt1", "lookup")
        self.route = FakeRoute(self.storage)
        self.desktop = launch.Storage(os.path.join(self.dir, "desktop"))
        launch.record(self.desktop)
        self.skills = os.path.join(self.dir, "skills")
        os.makedirs(self.skills)
        shutil.copy(rex.MANIFEST_FILE, self.skills)
        with open(os.path.join(self.skills, "agent.manifest.json"), "w") as fh:
            json.dump(agent_manifest(), fh)
        self.executor = launch.CheckedExecutorFake()
        self.transport = rex.FakeTransport()

    def server(self, profile):
        return create_server(profile=profile, compiled_storage=self.storage, compiled_hosts_path=self.hosts,
                             desktop_storage=self.desktop, desktop_executor=self.executor, skills_dir=self.skills,
                             compiled_executor=ApiStepExecutor(transport=self.transport), web_route=self.route,
                             agent_runner=fake_agent)

    def close(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def listed(server):
    return asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest())).root.tools


def called(server, name, arguments=None):
    req = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments or {}))
    return asyncio.run(server.request_handlers[CallToolRequest](req)).root


def payload(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


class NameRuleTests(unittest.TestCase):
    def test_generated_names(self):
        self.assertEqual(AT.TOOL_NAME, "agent_run")
        self.assertEqual(CT.typed_tool_name("crates.io", "search_crates"), "crates_io__search_crates")
        self.assertEqual(CT.typed_tool_name("127.0.0.1:8443", "fixture_search"), "127_0_0_1__fixture_search")
        self.assertEqual(CT.legacy_typed_tool_name("crates.io", "search_crates"), "crates.io.search_crates")
        self.assertEqual(DT._name({"workflow": "read setting"}), "desktop__read_setting")
        self.assertEqual(DT._legacy_name({"workflow": "read setting"}), "desktop.read_setting")

    def test_a_long_name_is_cut_with_a_hash_of_the_whole_name(self):
        a, b = CT.typed_tool_name("crates.io", LONG + "_a"), CT.typed_tool_name("crates.io", LONG + "_b")
        for name in (a, b):
            self.assertRegex(name, PATTERN)
            self.assertEqual(len(name), TN.MAX_LEN)
            self.assertRegex(name, r"_[0-9a-f]{8}$")
        self.assertNotEqual(a, b)
        self.assertEqual(a[:55], b[:55])
        self.assertEqual(a, CT.typed_tool_name("crates.io", LONG + "_a"))      # deterministic
        self.assertEqual(TN.fit("x" * 64), "x" * 64)                          # 64 fits as it is

    def test_a_collision_gets_a_stable_suffix_and_aliases_resolve(self):
        def table(order):
            reg = TN.Registry()
            for kind, source in order:
                reg.add(kind, source, aliases=(f"{source}.old",))
            return reg
        one = table([("web_api", "same_name"), ("desktop", "same_name")])
        two = table([("web_api", "same_name"), ("desktop", "same_name")])
        self.assertEqual(one.names(), two.names())
        self.assertEqual(one.names()[0], "same_name")
        self.assertRegex(one.names()[1], r"^same_name_[0-9a-f]{6}$")
        self.assertEqual(one.resolve(one.names()[1]).kind, "desktop")
        self.assertEqual(one.resolve("same_name").kind, "web_api")             # a listed name wins over an alias
        self.assertIsNone(one.resolve("same_name.old"))                       # an older name both had calls neither
        self.assertEqual(one.sharing("same_name.old"), one.names())
        self.assertIsNone(one.resolve("nothing.like.this"))
        reg = TN.Registry()
        reg.add("web_api", "a", aliases=("a.old",))
        reg.add("desktop", "b", aliases=("b.old",))
        self.assertEqual((reg.resolve("a.old").name, reg.resolve("b.old").name), ("a", "b"))
        # a cut name that collides is suffixed within 64 characters
        reg = TN.Registry()
        first, second = reg.add("a", "y" * 80), reg.add("b", "y" * 80)
        self.assertNotEqual(first.name, second.name)
        self.assertTrue(all(PATTERN.match(e.name) for e in (first, second)))
        self.assertEqual(reg.resolve("y" * 80), first)


class ProfileListingTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_every_profile_lists_only_valid_unique_names(self):
        for profile in ("default", "full", "meta"):
            names = [t.name for t in listed(self.fx.server(profile))]
            self.assertTrue(names, profile)
            bad = [n for n in names if not PATTERN.match(n)]
            self.assertEqual(bad, [], profile)
            self.assertEqual(len(names), len(set(names)), profile)

    def test_default_names(self):
        names = {t.name for t in listed(self.fx.server("default"))}
        expected = {"agent_run", "crates_io__crates_search", "crates_io__crates_detail", "shop_example__lookup",
                    "desktop__read_setting", "routine__crates_lookup", RESUME_TOOL_NAME,
                    CT.typed_tool_name("crates.io", LONG + "_a"), CT.typed_tool_name("crates.io", LONG + "_b")}
        self.assertTrue(expected <= names, expected - names)
        self.assertFalse([n for n in names if "." in n])
        # "dup.name" and "dup_name" both become crates_io__dup_name: each gets a suffix from its own workflow
        dups = sorted(n for n in names if n.startswith("crates_io__dup_name"))
        self.assertEqual(dups, sorted(TN.suffixed("crates_io__dup_name", f"crates.io\0{w}")
                                      for w in ("dup.name", "dup_name")))

    def test_full_lists_the_agent_step_routine_and_meta_keeps_its_four_tools(self):
        full = {t.name for t in listed(self.fx.server("full"))}
        self.assertIn("routine__crates_agent_pick", full)
        self.assertNotIn("agent_run", full)
        meta = [t.name for t in listed(self.fx.server("meta"))]
        self.assertEqual(meta, ["list_sites", "list_operations", "call_operation", "login"])

    def test_the_listing_is_stable(self):
        server = self.fx.server("default")
        self.assertEqual([t.name for t in listed(server)], [t.name for t in listed(self.fx.server("default"))])


class AliasCallTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def both(self, new, old, arguments, profile="default"):
        server = self.fx.server(profile)
        return payload(called(server, new, arguments)), payload(called(server, old, arguments))

    def test_agent_run_by_its_old_name(self):
        args = {"task": "search for {q}", "site": "www.shop.test", "values": {"q": "x"}}
        new, old = self.both("agent_run", "agent.run", args)
        self.assertTrue(new["ok"] and old["ok"])
        self.assertEqual(new, old)
        # the meta profile has no agent.run: its agent is call_operation on site agent
        refused = json.loads(called(self.fx.server("meta"), "agent.run", args).content[0].text)
        self.assertFalse(refused["success"])

    def test_a_typed_web_request_by_its_old_name(self):
        new, old = self.both("crates_io__crates_detail", "crates.io.crates_detail", {"crate": "serde_json"})
        self.assertTrue(new["ok"], new)
        self.assertEqual((new["ok"], new["result"]), (old["ok"], old["result"]))
        self.assertEqual(len(self.fx.transport.sent), 2)
        self.assertEqual(self.fx.transport.sent[0], self.fx.transport.sent[1])

    def test_a_cut_and_a_suffixed_name_call_their_own_record(self):
        server = self.fx.server("default")
        cut = CT.typed_tool_name("crates.io", LONG + "_a")
        for name in (cut, CT.legacy_typed_tool_name("crates.io", LONG + "_a")):
            out = payload(called(server, name, {"crate": "serde_json"}))
            self.assertTrue(out["ok"], (name, out))
        names = sorted(n for n in (t.name for t in listed(server)) if n.startswith("crates_io__dup_name"))
        reached = {payload(called(server, name, {"crate": "serde_json"}))["workflow"] for name in names}
        self.assertEqual(reached, {"dup.name", "dup_name"})
        for legacy in ("crates.io.dup.name", "crates.io.dup_name"):
            out = payload(called(server, legacy, {"crate": "serde_json"}))
            self.assertEqual((out["ok"], out["workflow"]), (True, legacy[len("crates.io."):]))

    def test_a_saved_web_tool_by_its_old_name(self):
        new, old = self.both("shop_example__lookup", "shop.example.lookup", {"value": "Bosch GSR 12V"})
        self.assertEqual(new, old)
        self.assertEqual(self.fx.route.calls, [("lookup", {"value": "Bosch GSR 12V"})] * 2)

    def test_a_desktop_tool_by_its_old_name(self):
        new, old = self.both("desktop__read_setting", "desktop.read_setting", {"label": "Version"})
        self.assertTrue(new["ok"] and old["ok"], (new, old))
        self.assertEqual(len(self.fx.executor.calls), 2)
        self.assertEqual(self.fx.executor.calls[0][0].command, self.fx.executor.calls[1][0].command)
        # the module's own entry point takes either name
        out = asyncio.run(DT.run_typed_tool("desktop.read_setting", {"label": "Version"},
                                            storage=self.fx.desktop, executor=self.fx.executor))
        self.assertTrue(out["ok"], out)

    def test_a_routine_by_its_old_name(self):
        new, old = self.both("routine__crates_lookup", "routine.crates_lookup", {"q": "serde"})
        self.assertEqual((new["status"], old["status"]), ("handed_off", "handed_off"), (new, old))
        self.assertEqual(new["manifest_digest"], old["manifest_digest"])
        self.assertEqual(len(self.fx.transport.sent), 2)

    def test_an_old_name_gets_the_same_input_check(self):
        server = self.fx.server("default")
        for name in ("agent_run", "agent.run"):
            res = called(server, name, {"task": "x", "unknown": 1})
            self.assertTrue(res.isError, name)
            self.assertIn("Input validation error", res.content[0].text, name)
        for name in ("desktop__read_setting", "desktop.read_setting"):
            res = called(server, name, {})
            self.assertTrue(res.isError, name)
            self.assertIn("Input validation error", res.content[0].text, name)
        self.assertEqual(self.fx.executor.calls, [])

    def test_a_name_never_listed_stays_unavailable(self):
        out = json.loads(called(self.fx.server("default"), "crates.io.nothing", {}).content[0].text)
        self.assertFalse(out["success"])
        self.assertIn("not available", out["error"])


class SharedNameTests(unittest.TestCase):
    """Tools whose generated names meet: a port or a character a tool name cannot hold, or the same first 80
    characters. Each listed name runs its own workflow, and a tool added later never takes a name over."""

    def setUp(self):
        self.fx = Fixture()
        self.args = {"crate": "serde_json"}

    def tearDown(self):
        self.fx.close()

    def compile(self, name, url="https://crates.io/api/v1/crates/{crate}"):
        persist_compiled(self.fx.storage, name, compiled_record(rex.detail_template(url), rex.PROOF, workflow=name))

    def reached(self, server, name):
        before = len(self.fx.transport.sent)
        out = payload(called(server, name, self.args))
        return out.get("workflow"), self.fx.transport.sent[before:]

    def names(self, server, prefix):
        return sorted(n for n in (t.name for t in listed(server)) if n.startswith(prefix))

    def test_hosts_that_differ_by_a_port_keep_their_own_workflows(self):
        self.compile("port.lookup", "https://crates.io:8443/api/v1/crates/{crate}")
        self.compile("port_lookup")
        acknowledge_host("crates.io:8443", path=self.fx.hosts)
        server = self.fx.server("default")
        names = self.names(server, "crates_io__port_lookup")
        self.assertEqual(len(names), 2, names)
        expect = {"port.lookup": ["https://crates.io:8443/api/v1/crates/serde_json"],
                  "port_lookup": ["https://crates.io/api/v1/crates/serde_json"]}
        self.assertEqual(dict(self.reached(server, n) for n in names), expect)
        for old, workflow in (("crates.io.port.lookup", "port.lookup"), ("crates.io.port_lookup", "port_lookup")):
            self.assertEqual(self.reached(server, old), (workflow, expect[workflow]), old)
        # the module's own names are unique too, so its entry point finds one tool per name
        module = [t["name"] for t in CT.executable_typed_tools(self.fx.storage, self.fx.hosts)]
        self.assertEqual(len(module), len(set(module)))

    def test_a_shared_name_stops_calling_instead_of_moving(self):
        self.compile("late_name")
        server = self.fx.server("default")
        self.assertEqual(self.reached(server, "crates_io__late_name")[0], "late_name")
        own = TN.suffixed("crates_io__late_name", "crates.io\0late_name")
        self.assertEqual(self.reached(server, own)[0], "late_name")      # its suffixed name already calls it
        self.compile("late.name")
        server = self.fx.server("default")
        names = self.names(server, "crates_io__late_name")
        self.assertEqual(len(names), 2, names)
        self.assertNotIn("crates_io__late_name", names)
        self.assertIn(own, names)
        self.assertEqual({self.reached(server, n)[0] for n in names}, {"late_name", "late.name"})
        before = len(self.fx.transport.sent)
        refused = json.loads(called(server, "crates_io__late_name", self.args).content[0].text)
        self.assertFalse(refused["success"])
        for name in names:
            self.assertIn(name, refused["error"])                       # the call names the tools that share it
        self.assertEqual(len(self.fx.transport.sent), before)
        # the other one removed: the plain name is back, the suffixed one still calls the same workflow
        shutil.rmtree(self.fx.storage.get_workflow_dir("late.name"))
        server = self.fx.server("default")
        self.assertEqual(self.names(server, "crates_io__late_name"), ["crates_io__late_name"])
        self.assertEqual(self.reached(server, "crates_io__late_name")[0], "late_name")
        self.assertEqual(self.reached(server, own)[0], "late_name")

    def test_a_desktop_name_stops_calling_instead_of_moving(self):
        server = self.fx.server("default")
        payload(called(server, "desktop__read_setting", {"label": "Version"}))
        self.assertEqual(self.fx.executor.calls[-1][2].id, "read_setting")
        launch.record(self.fx.desktop, name="read setting")
        server = self.fx.server("default")
        names = self.names(server, "desktop__read_setting")
        self.assertEqual(len(names), 2, names)
        self.assertNotIn("desktop__read_setting", names)
        self.assertTrue(all(re.match(r"^desktop__read_setting_[0-9a-f]{6}$", n) for n in names), names)
        reached = set()
        for name in names:
            payload(called(server, name, {"label": "Version"}))
            reached.add(self.fx.executor.calls[-1][2].id)
        self.assertEqual(reached, {"read_setting", "read setting"})
        calls = len(self.fx.executor.calls)
        refused = json.loads(called(server, "desktop__read_setting", {"label": "Version"}).content[0].text)
        self.assertFalse(refused["success"])
        self.assertEqual(len(self.fx.executor.calls), calls)
        # both had the older name desktop.read_setting: it calls neither now, and names the two tools
        refused = json.loads(called(server, "desktop.read_setting", {"label": "Version"}).content[0].text)
        self.assertFalse(refused["success"])
        self.assertEqual(sorted(refused["tools"]), names)
        self.assertEqual(len(self.fx.executor.calls), calls)
        self.assertIsNone(asyncio.run(DT.run_typed_tool("desktop.read_setting", {"label": "Version"},
                                                        storage=self.fx.desktop, executor=self.fx.executor)))

    def test_names_do_not_depend_on_which_tool_was_first(self):
        items = [("crates_io__x", "k1"), ("crates_io__x", "k2"), ("crates_io__y", "k3")]
        self.assertEqual(TN.unique(items), list(reversed(TN.unique(list(reversed(items))))))
        self.assertEqual(TN.unique(items)[2], "crates_io__y")
        self.assertEqual(TN.unique(items)[0], TN.suffixed("crates_io__x", "k1"))
        # a name another kind keeps (``taken``) gets the same treatment
        self.assertEqual(TN.unique([("crates_io__y", "k3")], taken={"crates_io__y"}),
                         [TN.suffixed("crates_io__y", "k3")])
        # names stay unique even when a suffix meets another tool's plain name, or one key appears twice
        clash = TN.suffixed("a", "k1")
        out = TN.unique([("a", "k1"), ("a", "k2"), (clash, "k3"), ("b", "k4"), ("b", "k4")])
        self.assertEqual(len(out), len(set(out)), out)

    def test_two_long_names_that_share_their_first_80_characters(self):
        a, b = (CT.typed_tool_name("crates.io", "w" * 80 + tail) for tail in ("_alpha", "_beta"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(DT._name({"workflow": "w" * 80 + "_alpha"}), DT._name({"workflow": "w" * 80 + "_beta"}))
        self.assertTrue(all(PATTERN.match(n) for n in (a, b)))

    def test_a_saved_web_tool_keeps_its_old_suffixed_name(self):
        # a compiled request and a saved web tool both named lookup on shop.example: the request had
        # shop.example.lookup, the web tool shop.example.lookup.<sha12>; both old names still call their tool
        self.compile("lookup", "https://shop.example/api/v1/crates/{crate}")
        acknowledge_host("shop.example", path=self.fx.hosts)
        server = self.fx.server("default")
        old = f"shop.example.lookup.{WT.tool_sha(WEB_TOOL)[:12]}"
        payload(called(server, old, {"value": "Bosch GSR 12V"}))
        self.assertEqual(self.fx.route.calls, [("lookup", {"value": "Bosch GSR 12V"})])
        self.assertEqual(self.reached(server, "shop.example.lookup"),
                         ("lookup", ["https://shop.example/api/v1/crates/serde_json"]))
        self.assertEqual(len(self.fx.route.calls), 1)


class TextTests(unittest.TestCase):
    def test_texts_name_the_new_tools(self):
        self.assertIn("call agent_run again", AT.TOOL_DESCRIPTION)
        self.assertNotIn("agent.run", AT.TOOL_DESCRIPTION)
        text = server_instructions("default")
        self.assertIn("agent_run", text)
        self.assertIn("<host>__<workflow>", text)
        self.assertNotIn("agent.run", text)
        md = skill_md(json.load(open(rex.MANIFEST_FILE)))
        self.assertIn("`routine__crates_lookup`", md)
        self.assertNotIn("routine.crates_lookup", md)

    def test_skills_doc_names_the_dispatched_tools(self):
        text = open(os.path.join(HERE, "docs", "SKILLS.md"), encoding="utf-8").read()
        integration = text.split("## Integration", 1)[1].split("\n## ", 1)[0]
        self.assertIn("skill__<name>", integration)
        self.assertNotIn("dispatches `skill.*`", integration)

    def test_a_resume_hint_names_agent_run(self):
        from anyuse.execution import commit_policy as CP

        def asks(task, asker=None, **kw):
            q = CP.Question(kind=CP.CONFIRM, why="a step the task did not ask for", run_id="r1")
            asker(q)
            return fake_agent(task, **kw)
        out = AT.handle({"task": "x"}, run=asks, ttl_s=1, wait_s=5)
        self.assertEqual(out["outcome"], "confirm_required", out)
        self.assertTrue(out["resume"]["how"].startswith("call agent_run with"), out["resume"])
        self.assertNotIn("agent.run", json.dumps(out))
        AT.handle({"resume": out["resume"]["token"], "confirm": False}, run=asks, ttl_s=1, wait_s=5)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
