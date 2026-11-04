"""Asking the human: elicitation first, inbox fallback, challenge policy (offline).

Fake runners, fake block pages, a fake terminal, fake elicitation clients and a
real in-memory MCP client/server pair (mcp.shared.memory). Receipts, cards,
the hosts/accounts files and the vault key file live in temp directories.
No network, no desktop, no model.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner
from cryptography.fernet import Fernet

from anyuse.cli import cli
from anyuse.execution import human_gate, human_inbox as hi
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.routine import Engine, ReceiptStore, outcome
from anyuse.execution.skill_gen import RESUME_TOOL_NAME
from anyuse.execution.skill_manifest import validate_manifest
from anyuse.mcpserver import skill_tools
from anyuse.watcher import entity_vault as ev
from test_skill_manifest import base, proved_step

SECRET = "hunter2-S3cret-Value"
KEY = Fernet.generate_key()
TURNSTILE = {"blocked": True, "reason": "captcha_turnstile", "host": "a.example",
             "status": 403, "evidence": ["matched 'cf-turnstile'", "HTTP 403"]}
LOGIN = {"blocked": True, "reason": "login_wall", "host": "a.example", "status": 200,
         "evidence": ["matched 'please sign in'"]}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class FakeRunner:
    """Records every dispatch (step, bound inputs, ctx). ``block`` maps a step
    id to how many times it answers with a block page first; ``crash`` to how
    many times it raises after dispatch (an uncertain effect)."""

    def __init__(self, shares_session=True, block=None, blocked=TURNSTILE, crash=None):
        self.supports_attached_session = shares_session
        self.block, self.crash = dict(block or {}), dict(crash or {})
        self.blocked = blocked
        self.calls = []
        self.fresh_problem = None

    def fresh_check(self, step, bound):
        return self.fresh_problem

    def execute(self, step, bound, ctx):
        sid = step["id"]
        self.calls.append((sid, dict(bound), dict(ctx)))
        if self.crash.get(sid, 0) > 0:
            self.crash[sid] -= 1
            raise RuntimeError("connection reset after send")
        if self.block.get(sid, 0) > 0:
            self.block[sid] -= 1
            return outcome(False, error="blocked", blocked=self.blocked)
        if sid == "fetch":
            return outcome(True, {"title": f"T:{bound['q']}"})
        if sid == "detail":
            return outcome(True, {"body": f"B:{bound['title']}"})
        if sid == "use":
            return outcome(True, {"result": f"ok:{len(bound['pin'])}"})
        if sid == "send":
            return outcome(True, {"sent": True}, effect_class="side_effecting")
        return outcome(True, {})


def chal_manifest():
    """fetch (host a) -> detail (host a). A block on fetch is the scenario."""
    m = base(name="chal")
    m["steps"] = [
        proved_step("fetch", inputs={"q": {"type": "string"}},
                    outputs={"title": {"type": "string"}},
                    bindings={"q": {"kind": "input", "name": "q"}}),
        proved_step("detail", inputs={"title": {"type": "string"}},
                    outputs={"body": {"type": "string"}},
                    bindings={"title": {"kind": "derived", "step": "fetch",
                                        "output": "title"}}),
    ]
    m["outputs"] = {"body": {"type": "string", "from": {"step": "detail", "output": "body"}}}
    return m


def val_manifest():
    """ask (value card) -> use (host b)."""
    m = base(name="val")
    m["steps"] = [
        {"id": "ask", "mode": "human",
         "action": {"kind": "human", "card": "value",
                    "instruction": "Enter the one-time PIN from your letter."},
         "evidence": {"status": "none"}, "inputs": {}, "bindings": {},
         "outputs": {"pin": {"type": "string", "minLength": 4, "maxLength": 64}},
         "effects": {"class": "read_only", "reversible": True},
         "autonomy": {"level": "rule"}},
        proved_step("use", app="b", inputs={"pin": {"type": "string"}},
                    outputs={"result": {"type": "string"}},
                    bindings={"pin": {"kind": "derived", "step": "ask", "output": "pin"}}),
    ]
    m["outputs"] = {"result": {"type": "string", "from": {"step": "use", "output": "result"}}}
    return m


def send_manifest():
    """send (host b): side-effecting, needs approval, not idempotent."""
    m = base(name="send")
    m["steps"] = [proved_step(
        "send", app="b", inputs={"q": {"type": "string"}},
        outputs={"sent": {"type": "boolean"}},
        bindings={"q": {"kind": "input", "name": "q"}},
        effects={"class": "side_effecting", "reversible": False},
        approval={"required": True},
        policy={"timeout_s": 5, "retries": 0, "idempotent": False})]
    m["outputs"] = {"sent": {"type": "boolean", "from": {"step": "send", "output": "sent"}}}
    return m


class Tty:
    """A person at /dev/tty: answers prompts from ``lines`` in order and types
    back whatever code the gate printed."""

    def __init__(self, lines=(), code=None):
        self.lines, self.code = list(lines), code
        self.written, self.opened = [], 0

    def __call__(self):
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.written)

    def readline(self):
        last = self.written[-1] if self.written else ""
        if last.endswith("> ") and "type this code:" in self.text:
            if self.code is not None:
                return self.code + "\n"
            return re.findall(r"type this code: (\w+)", self.text)[-1] + "\n"
        return self.lines.pop(0) + "\n"


class Elicitor:
    """A client that shows elicitation forms: scripted answers, then decline."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def ask(self, message, schema):
        self.asked.append((message, json.loads(json.dumps(schema))))
        return dict(self.answers.pop(0)) if self.answers else {"action": "decline"}


def as_json(text):
    """The CLI's JSON result (a degraded-custody warning may precede it)."""
    return json.loads(text[text.index("{"):])


def accept(**content):
    return {"action": "accept", "content": content}


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.runs = os.path.join(self.tmp, "runs")
        self.hosts = os.path.join(self.tmp, "api_hosts.json")
        self.accounts = os.path.join(self.tmp, "accounts.json")
        self.inbox = hi.Inbox(os.path.join(self.tmp, "inbox"), hosts_path=self.hosts,
                              accounts_path=self.accounts, key_provider=lambda: KEY)
        self.engines = {}

    def engine(self, manifest, runner):
        eng = Engine(manifest, {"compiled_tool": runner}, ReceiptStore(self.runs),
                     inbox=self.inbox, human_channel="cli")
        self.engines[eng.digest] = eng
        return eng

    def engine_for(self, run_id):
        meta = ReceiptStore(self.runs).meta(run_id)
        return self.engines[meta["manifest_digest"]], None

    def gate(self, tty, env=None, ttys=True):
        return human_gate.TtyGate(env=env or {}, stdio_are_ttys=lambda: ttys,
                                  open_tty=tty, nonce_factory=lambda: "n0",
                                  hostname="h", optin_file=os.path.join(self.tmp, "optin"))

    def do(self, card_id, lines=(), secret=None, tty=None):
        tty = tty or Tty(lines)
        res = hi.do_card(self.inbox, card_id, engine_for=self.engine_for,
                         gate=self.gate(tty), read_secret=(lambda _p: secret))
        return res, tty

    def elicit(self, card_id, *answers):
        asker = Elicitor(*answers)
        res = hi.elicit_card(self.inbox, card_id, engine_for=self.engine_for, elicitor=asker)
        return res, asker

    def pending(self):
        return {c["id"]: c for c in self.inbox.cards()}

    def own_account(self, host):
        with open(self.accounts, "w") as fh:
            json.dump({"version": 1, "accounts": [
                {"name": "Mine", "service": host, "url": f"https://{host}/login",
                 "credential_type": "website", "created_by": "user"},
                {"name": "Bot", "service": "bot.example", "created_by": "agent"}]}, fh)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def all_files_text(root):
    out = []
    for d, _, names in os.walk(root):
        for n in names:
            with open(os.path.join(d, n), "rb") as fh:
                out.append((os.path.join(d, n), fh.read().decode("utf-8", "replace")))
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self._policy = mock.patch.dict(os.environ, {hi.POLICY_ENV: ""})
        self._policy.start()

    def tearDown(self):
        self._policy.stop()
        self.env.close()


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

class PolicyUnitTests(unittest.TestCase):
    def test_policy_fails_closed_to_human_does_step(self):
        for raw in ("", "continue", "CONTINUE_IN_OWN", "solve", "stealth"):
            with mock.patch.dict(os.environ, {hi.POLICY_ENV: raw}):
                self.assertEqual(hi.challenge_policy(), hi.HUMAN_DOES_STEP)
        with mock.patch.dict(os.environ, {hi.POLICY_ENV: " Continue_In_Own_Session "}):
            self.assertEqual(hi.challenge_policy(), hi.CONTINUE_IN_OWN_SESSION)

    def test_block_reasons_map_to_card_kinds(self):
        self.assertEqual(hi.kind_for_block("captcha_turnstile"), hi.KIND_CHALLENGE)
        self.assertEqual(hi.kind_for_block("challenge_interstitial"), hi.KIND_CHALLENGE)
        self.assertEqual(hi.kind_for_block("forbidden"), hi.KIND_CHALLENGE)
        self.assertEqual(hi.kind_for_block("login_wall"), hi.KIND_LOGIN)
        for r in ("rate_limited", "cookie_wall", "error_page", "unknown_block"):
            self.assertEqual(hi.kind_for_block(r), hi.KIND_DO_STEP)

    def test_manifest_card_kinds_are_validated(self):
        self.assertEqual(validate_manifest(val_manifest()), [])
        self.assertEqual(validate_manifest(send_manifest()), [])
        bad = val_manifest()
        bad["steps"][0]["action"]["card"] = "solve_captcha"
        self.assertTrue(any("action.card" in e for e in validate_manifest(bad)))
        bad = val_manifest()
        bad["steps"][0]["outputs"] = {"pin": {"type": "array", "items": {"type": "string"}}}
        self.assertTrue(any("scalar outputs" in e for e in validate_manifest(bad)))

    def test_own_accounts_are_only_the_users_own(self):
        env = Env()
        try:
            env.own_account("a.example")
            self.assertTrue(hi.own_account("a.example", env.accounts))
            self.assertTrue(hi.own_account("www.a.example", env.accounts))
            self.assertIsNone(hi.own_account("bot.example", env.accounts))
            self.assertIsNone(hi.own_account("c.example", env.accounts))
        finally:
            env.close()


# ---------------------------------------------------------------------------
# primary channel: elicitation
# ---------------------------------------------------------------------------

class ElicitationTests(_Base):
    def test_approval_is_a_boolean_bound_to_the_digest_and_needs_no_code(self):
        e = self.env
        runner = FakeRunner()
        eng = e.engine(send_manifest(), runner)
        res = eng.run({"q": "x"})
        card = e.inbox.get(res["card"]["id"])
        self.assertEqual(card["kind"], "approval")
        # "no" leaves the card pending and sends nothing
        out, asker = e.elicit(card["id"], accept(approve=False))
        self.assertEqual(out["asked"], "declined")
        self.assertEqual(runner.calls, [])
        msg, schema = asker.asked[0]
        self.assertEqual(schema["properties"]["approve"]["type"], "boolean")
        self.assertIn(card["approval_digest"][:16], msg)
        self.assertIn("side_effecting", msg)
        out, _ = e.elicit(card["id"], accept(approve=True))
        self.assertEqual(out["status"], "completed", out)
        self.assertEqual([c[0] for c in runner.calls], ["send"])
        chain = ReceiptStore(e.runs).load(res["run_id"])
        appr = [r for r in chain if r["type"] == "approval"]
        self.assertEqual(appr[0]["channel"], "mcp-elicitation")
        self.assertEqual(e.inbox.get(card["id"])["answered_via"], "mcp-elicitation")

    def test_value_is_a_sensitive_string_and_is_never_echoed_logged_or_stored_plain(self):
        e = self.env
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        old = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            runner = FakeRunner()
            runner.fresh_problem = "hold"       # stop right after the value is kept
            eng = e.engine(val_manifest(), runner)
            res = eng.run({"q": "x"})
            out, asker = e.elicit(res["card"]["id"], accept(pin=SECRET))
            waiting = all_files_text(e.tmp)
            runner.fresh_problem = None
            done = eng.resume(res["run_id"])
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        _, schema = asker.asked[0]
        field = schema["properties"]["pin"]
        self.assertEqual(field["type"], "string")
        self.assertTrue(field["writeOnly"] and field["x-anyuse-sensitive"])
        self.assertEqual(out["status"], "refused")            # held by the fresh check
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(runner.calls[0][1], {"pin": SECRET})  # the runner got it
        for path, text in waiting + all_files_text(e.tmp):
            self.assertNotIn(SECRET, text, path)                # encrypted while waiting
        values = [p for p, _ in waiting if os.sep + "values" + os.sep in p]
        self.assertEqual(len(values), 1)
        self.assertNotIn(SECRET, json.dumps([out, done, asker.asked[0][0]]))
        self.assertNotIn(SECRET, buf.getvalue())
        self.assertFalse([p for p, _ in all_files_text(e.tmp)
                          if os.sep + "values" + os.sep in p])  # gone once done

    def test_do_step_asks_done_plus_the_result_and_resumes(self):
        e = self.env
        m = base()          # search -> pick (plain human step) -> detail
        runner = FakeRunner()
        runner.execute = lambda step, bound, ctx: (runner.calls.append(step["id"]) or outcome(
            True, {"items": ["a1", "a2"]} if step["id"] == "search" else {"title": "t"}))
        eng = e.engine(m, runner)
        res = eng.run({"q": "x"})
        out, asker = e.elicit(res["card"]["id"], accept(done=False))
        self.assertEqual(out["asked"], "declined")
        self.assertEqual(set(asker.asked[0][1]["required"]), {"done", "id"})
        out, _ = e.elicit(res["card"]["id"], accept(done=True, id="zz"))
        self.assertEqual(out["status"], "refused")               # postcondition
        self.assertIn(res["card"]["id"], e.pending())
        out, _ = e.elicit(res["card"]["id"], accept(done=True, id="a2"))
        self.assertEqual(out["status"], "completed", out)
        self.assertEqual(runner.calls, ["search", "detail"])

    def test_decline_or_cancel_leaves_the_card_for_the_cli_fallback(self):
        e = self.env
        runner = FakeRunner(block={"fetch": 1})
        res = e.engine(chal_manifest(), runner).run({"q": "x"})
        for answer, word in (({"action": "decline"}, "declined"),
                             ({"action": "cancel"}, "cancelled")):
            out, _ = e.elicit(res["card"]["id"], answer)
            self.assertEqual(out["asked"], word)
        self.assertIn(res["card"]["id"], e.pending())
        done, _ = e.do(res["card"]["id"], lines=["T"])
        self.assertEqual(done["handoff"]["reason"], "human_only_host")

    def test_reconcile_is_a_card(self):
        e = self.env
        runner = FakeRunner(crash={"send": 1})
        eng = e.engine(send_manifest(), runner)
        res = eng.run({"q": "x"})
        out, _ = e.elicit(res["card"]["id"], accept(approve=True))
        self.assertEqual(out["status"], "needs_reconcile", out)
        card = e.inbox.get(out["card"]["id"])
        self.assertEqual(card["kind"], "reconcile")
        again = eng.resume(res["run_id"])                 # no duplicate card
        self.assertEqual(again["card"]["id"], card["id"])
        out, asker = e.elicit(card["id"], accept(outcome="not_done"))
        self.assertEqual(asker.asked[0][1]["properties"]["outcome"]["enum"],
                         ["completed", "not_done"])
        self.assertEqual(out["status"], "completed", out)
        self.assertEqual([c[0] for c in runner.calls], ["send", "send"])


# ---------------------------------------------------------------------------
# challenge policy, stacking, resume
# ---------------------------------------------------------------------------

class InboxEngineTests(_Base):
    def test_three_cards_from_two_runs_stack_and_each_run_resumes_on_its_own(self):
        e = self.env
        vrun, crun = FakeRunner(), FakeRunner(block={"fetch": 1})
        veng, ceng = e.engine(val_manifest(), vrun), e.engine(chal_manifest(), crun)
        ra, rb = veng.run({"q": "x"}), ceng.run({"q": "x"})
        self.assertEqual((ra["card"]["kind"], rb["card"]["kind"]), ("value", "challenge"))
        self.assertEqual(rb["handoff"]["reason"], "blocked")
        c1, c2 = ra["card"]["id"], rb["card"]["id"]
        self.assertEqual(set(e.pending()), {c1, c2})
        row = next(c for c in hi.list_pending(e.inbox)["cards"] if c["id"] == c2)
        self.assertEqual(row["host"], "a.example")
        self.assertIn("will not solve", row["needed"])
        self.assertIn("detail", row["continues_after"])
        # the challenge first (out of order), over elicitation
        res, _ = e.elicit(c2, accept(done=True, title="Title seen by me"))
        self.assertEqual(res["handoff"]["reason"], "human_only_host")
        c3 = res["card"]["id"]
        self.assertEqual(set(e.pending()), {c1, c3})          # stacked
        self.assertEqual(len(e.inbox.cards(None)), 3)
        res, _ = e.do(c1, secret=SECRET)                     # run A via the CLI
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(set(e.pending()), {c3})
        res, _ = e.elicit(c3, accept(done=True, body="Body seen by me"))
        self.assertEqual(res["outputs"], {"body": "Body seen by me"})
        self.assertEqual(e.pending(), {})
        self.assertEqual([c[0] for c in crun.calls], ["fetch"])
        self.assertEqual([c[0] for c in vrun.calls], ["use"])
        again, _ = e.do(c3, lines=["x"])
        self.assertEqual(again["status"], "refused")

    def test_challenge_under_default_policy_never_reuses_the_session(self):
        e = self.env
        acknowledge_host("a.example", path=e.hosts, ignore_robots=True,
                         robots_reason="my own site")      # own site, default policy
        runner = FakeRunner(shares_session=True, block={"fetch": 1})
        eng = e.engine(chal_manifest(), runner)
        res = eng.run({"q": "x"})
        card = e.inbox.get(res["card"]["id"])
        self.assertEqual(card["how"], "human_does_step")
        again = eng.resume(res["run_id"])
        self.assertEqual(again["card"]["id"], card["id"])
        self.assertEqual(len(runner.calls), 1)
        done, asker = e.elicit(card["id"], accept(done=True, title="T"))
        self.assertIn("does not solve or bypass", asker.asked[0][0])
        e.elicit(done["card"]["id"], accept(done=True, body="B"))
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(all("session" not in ctx for _, _, ctx in runner.calls))
        chain = ReceiptStore(e.runs).load(res["run_id"])
        self.assertEqual({r["host"] for r in chain if r["type"] == "host_policy"},
                         {"a.example"})

    def test_own_site_override_continues_in_the_attached_session(self):
        e = self.env
        acknowledge_host("a.example", path=e.hosts, ignore_robots=True,
                         robots_reason="my own site")
        with mock.patch.dict(os.environ, {hi.POLICY_ENV: hi.CONTINUE_IN_OWN_SESSION}):
            runner = FakeRunner(block={"fetch": 1})
            eng = e.engine(chal_manifest(), runner)
            res = eng.run({"q": "x"})
            card = e.inbox.get(res["card"]["id"])
            self.assertEqual(card["how"], "continue_session")
            runner.fresh_problem = "the compiled proof changed"
            stopped, asker = e.elicit(card["id"], accept(done=True))
            self.assertEqual(list(asker.asked[0][1]["properties"]), ["done"])
            self.assertEqual(stopped["status"], "refused")        # fresh check on resume
            self.assertEqual(len(runner.calls), 1)
            runner.fresh_problem = None
            done = eng.resume(res["run_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual([c[0] for c in runner.calls], ["fetch", "fetch", "detail"])
        self.assertEqual(runner.calls[1][2].get("session"), "attached")

    def test_continue_policy_without_own_site_or_session_runner_falls_back(self):
        e = self.env
        with mock.patch.dict(os.environ, {hi.POLICY_ENV: hi.CONTINUE_IN_OWN_SESSION}):
            res = e.engine(chal_manifest(), FakeRunner(block={"fetch": 1})).run({"q": "x"})
            card = e.inbox.get(res["card"]["id"])
            self.assertEqual(card["how"], "human_does_step")
            self.assertIn("not on your own-sites list", card["policy"]["why"])
            acknowledge_host("a.example", path=e.hosts, ignore_robots=True,
                             robots_reason="my own site")
            res = e.engine(chal_manifest(), FakeRunner(shares_session=False,
                                                       block={"fetch": 1})).run({"q": "y"})
            self.assertEqual(e.inbox.get(res["card"]["id"])["how"], "human_does_step")

    def test_policy_is_rechecked_when_the_card_is_answered(self):
        e = self.env
        acknowledge_host("a.example", path=e.hosts, ignore_robots=True,
                         robots_reason="my own site")
        with mock.patch.dict(os.environ, {hi.POLICY_ENV: hi.CONTINUE_IN_OWN_SESSION}):
            runner = FakeRunner(block={"fetch": 1})
            res = e.engine(chal_manifest(), runner).run({"q": "x"})
        done, asker = e.elicit(res["card"]["id"], accept(done=True, title="T"))
        self.assertIn("title", asker.asked[0][1]["properties"])
        self.assertEqual(done["handoff"]["reason"], "human_only_host")
        self.assertEqual(len(runner.calls), 1)

    def test_login_on_own_account_continues_by_default_other_logins_do_not(self):
        e = self.env
        runner = FakeRunner(block={"fetch": 1}, blocked=LOGIN)
        res = e.engine(chal_manifest(), runner).run({"q": "x"})
        card = e.inbox.get(res["card"]["id"])
        self.assertEqual((card["kind"], card["how"]), ("login_or_2fa", "human_does_step"))
        self.assertIn("not in your own-accounts list", card["policy"]["why"])
        e.own_account("a.example")
        runner = FakeRunner(block={"fetch": 1}, blocked=LOGIN)
        res = e.engine(chal_manifest(), runner).run({"q": "y"})
        card = e.inbox.get(res["card"]["id"])
        self.assertEqual(card["how"], "continue_session")
        self.assertIn("YOUR OWN account", card["needed"])
        done, _ = e.elicit(card["id"], accept(done=True))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(runner.calls[1][2]["session"], "attached")

    def test_value_card_never_completes_in_band_and_cli_value_is_hidden(self):
        e = self.env
        runner = FakeRunner()
        eng = e.engine(val_manifest(), runner)
        res = eng.run({"q": "x"})
        forged = eng.resume(res["run_id"], human={"step": "ask", "outputs": {"pin": SECRET}})
        self.assertEqual(forged["status"], "handed_off")
        self.assertIn("anyuse inbox do", forged["error"])
        done, tty = e.do(res["card"]["id"], secret=SECRET)
        self.assertEqual(done["status"], "completed", done)
        self.assertNotIn(SECRET, tty.text + json.dumps(done) + json.dumps(forged))
        for path, text in all_files_text(e.tmp):
            self.assertNotIn(SECRET, text, path)
        for c in e.inbox.cards(None):
            self.assertEqual(stat.S_IMODE(os.stat(e.inbox._card_path(c["id"])).st_mode),
                             0o600)
        self.assertEqual(stat.S_IMODE(os.stat(e.inbox.base_dir).st_mode), 0o700)

    def test_vault_key_custody_is_used_and_fails_closed_when_it_does_not_persist(self):
        tmp = tempfile.mkdtemp()
        saved = (ev._KEYFILE, ev.KEYRING_AVAILABLE)
        try:
            ev._KEYFILE = __import__("pathlib").Path(tmp) / "keys"
            ev.KEYRING_AVAILABLE = False
            k1 = hi.vault_value_key()
            self.assertEqual(k1, hi.vault_value_key())
            self.assertIn(hi.VALUE_KEY_NAME, json.loads(ev._KEYFILE.read_text()))
            self.assertEqual(stat.S_IMODE(os.stat(ev._KEYFILE).st_mode), 0o600)
            ev.KEYRING_AVAILABLE = True           # the null keyring drops writes
            with mock.patch.object(ev, "keyring", create=True) as kr:
                kr.get_password.return_value = None
                ev._KEYFILE = __import__("pathlib").Path(tmp) / "other"
                with self.assertRaises(RuntimeError):
                    hi.vault_value_key()
        finally:
            ev._KEYFILE, ev.KEYRING_AVAILABLE = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_blocked_page_with_no_person_is_never_a_dead_end_or_a_retry(self):
        e = self.env
        runner = FakeRunner(block={"fetch": 5})
        eng = e.engine(chal_manifest(), runner)
        res = eng.run({"q": "x"})
        for _ in range(3):
            eng.resume(res["run_id"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(e.inbox.cards()), 1)


# ---------------------------------------------------------------------------
# surfaces: MCP (skill_tools + the real server over an in-memory client), CLI
# ---------------------------------------------------------------------------

class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.skills = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills)
        for m in (val_manifest(), chal_manifest(), send_manifest()):
            with open(os.path.join(self.skills, f"{m['name']}.manifest.json"), "w") as fh:
                json.dump(m, fh)
        self.runner = FakeRunner(block={"fetch": 1})
        self.saved_vault = (ev._KEYFILE, ev.KEYRING_AVAILABLE)
        ev._KEYFILE = __import__("pathlib").Path(self.tmp) / "vault.keys"
        ev.KEYRING_AVAILABLE = False
        self._patches = [
            mock.patch.object(skill_tools, "CompiledToolRunner",
                              lambda *a, **k: self.runner),
            mock.patch.dict(os.environ, {hi.POLICY_ENV: "",
                                         "ANYUSE_DATA_DIR": os.path.join(self.tmp, "data")}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        ev._KEYFILE, ev.KEYRING_AVAILABLE = self.saved_vault
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mcp(self, name, args, elicitor=None):
        return skill_tools.handle(name, args, directory=self.skills, launch_profile=True,
                                  elicitor=elicitor)

    def cli(self, *args, tty=None, ttys=True, env_extra=None, secret=None):
        env = {k: v for k, v in os.environ.items() if not human_gate.agent_markers({k: v})}
        env.update(env_extra or {})
        tty = tty or Tty()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(human_gate, "_stdio_are_ttys", lambda: ttys), \
                mock.patch.object(human_gate, "_open_dev_tty", tty), \
                mock.patch.object(hi, "_read_secret_tty", lambda _p: secret):
            return CliRunner().invoke(cli, ["inbox", "--skills-dir", self.skills, *args],
                                      obj={})

    def card_bytes(self):
        d = os.path.join(self.tmp, "data", "inbox", "cards")
        out = {}
        for n in sorted(os.listdir(d)):
            with open(os.path.join(d, n), "rb") as fh:
                out[n] = fh.read()
        return out

    def test_with_elicitation_the_run_asks_and_continues_in_one_call(self):
        asker = Elicitor(accept(pin=SECRET))
        res = self.mcp("routine__val", {"q": "x"}, elicitor=asker)
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(len(asker.asked), 1)
        self.assertNotIn(SECRET, json.dumps(res))
        self.assertEqual(self.mcp(hi.MCP_LIST_TOOL_NAME, {})["pending"], 0)
        # the card is kept for audit
        cards = hi.Inbox().cards(None)
        self.assertEqual([(c["kind"], c["status"], c["answered_via"]) for c in cards],
                         [("value", "done", "mcp-elicitation")])

    def test_without_elicitation_the_card_waits_and_no_tool_call_can_answer_it(self):
        names = [t["name"] for t in skill_tools.tool_definitions(self.skills,
                                                                 launch_profile=True)]
        self.assertEqual([n for n in names if "inbox" in n], [hi.MCP_LIST_TOOL_NAME])
        ra = self.mcp("routine__val", {"q": "x"})
        rb = self.mcp("routine__chal", {"q": "x"})
        self.assertEqual(ra["needs_human"]["asked"], "no_elicitation")
        self.assertEqual(rb["needs_human"]["kind"], "challenge")
        before = self.card_bytes()
        listing = self.mcp(hi.MCP_LIST_TOOL_NAME, {})
        self.assertEqual(listing["pending"], 2)
        self.assertEqual(self.card_bytes(), before)
        for guess in ("anyuse_inbox_do", "anyuse_inbox_complete", "anyuse_inbox_answer"):
            self.assertIsNone(self.mcp(guess, {"id": ra["card"]["id"]}))
        for run, human in ((ra, {"step": "ask", "outputs": {"pin": SECRET}}),
                           (rb, {"step": "fetch", "outputs": {"title": "T"}})):
            forged = self.mcp(RESUME_TOOL_NAME, {"run_id": run["run_id"], "human": human})
            self.assertEqual(forged["status"], "refused")
            self.assertIn("elicitation", forged["error"])
        again = self.mcp(RESUME_TOOL_NAME, {"run_id": rb["run_id"]})
        self.assertEqual(again["handoff"]["reason"], "blocked")
        self.assertEqual([c[0] for c in self.runner.calls], ["fetch"])
        self.assertEqual(self.mcp(hi.MCP_LIST_TOOL_NAME, {})["pending"], 2)

    def test_cli_fallback_keeps_the_terminal_gate(self):
        ra = self.mcp("routine__val", {"q": "x"})
        rb = self.mcp("routine__chal", {"q": "x"}, elicitor=Elicitor({"action": "decline"}))
        self.assertEqual(rb["needs_human"]["asked"], "declined")
        out = self.cli()
        self.assertIn("2 card(s) need you", out.output)
        for kw in ({"ttys": False}, {"env_extra": {"CLAUDECODE": "1"}}):
            tty = Tty()
            refused = self.cli("do", ra["card"]["id"], tty=tty, secret=SECRET, **kw)
            self.assertEqual(refused.exit_code, 1, refused.output)
            self.assertEqual(tty.opened, 0)
        wrong = self.cli("do", ra["card"]["id"], tty=Tty(code="ZZZZZZ"), secret=SECRET)
        self.assertIn("did not match", wrong.output)
        ok = self.cli("do", ra["card"]["id"], secret=SECRET)
        self.assertEqual(ok.exit_code, 0, (ok.output, ok.exception))
        self.assertEqual(as_json(ok.output)["status"], "completed", ok.output)
        self.assertNotIn(SECRET, ok.output + wrong.output)
        self.assertEqual(as_json(self.cli("skip", rb["card"]["id"]).output)["status"],
                         "skipped")
        res = as_json(self.cli("do", rb["card"]["id"], tty=Tty(["T"])).output)
        self.assertEqual(res["handoff"]["reason"], "human_only_host")
        cancelled = self.cli("cancel", res["card"]["id"])
        self.assertEqual(as_json(cancelled.output)["status"], "cancelled")
        self.assertEqual(self.mcp(RESUME_TOOL_NAME, {"run_id": rb["run_id"]})["status"],
                         "cancelled")
        self.assertIn("nothing needs you", self.cli().output)

    def test_real_mcp_client_with_and_without_elicitation(self):
        from mcp.shared.memory import create_connected_server_and_client_session
        from mcp.types import ElicitResult
        from anyuse.mcpserver.server import create_server

        asked = []

        async def on_elicit(ctx, params):
            asked.append((params.message, params.requestedSchema))
            return ElicitResult(action="accept", content={"approve": True})

        async def call(elicit):
            server = create_server(profile="default", skills_dir=self.skills)
            kw = {"elicitation_callback": on_elicit} if elicit else {}
            async with create_connected_server_and_client_session(server, **kw) as client:
                res = await client.call_tool("routine__send", {"q": "x"})
                text = res.content[0].text
                try:
                    return json.loads(text)
                except ValueError:
                    raise AssertionError(text) from None

        without = asyncio.run(call(False))
        self.assertEqual(without["status"], "handed_off", without)
        self.assertEqual(without["needs_human"]["asked"], "no_elicitation")
        self.assertEqual(asked, [])
        self.assertEqual(self.runner.calls, [])
        with_ = asyncio.run(call(True))
        self.assertEqual(with_["status"], "completed", with_)
        self.assertEqual(len(asked), 1)
        self.assertIn("approve", asked[0][1]["properties"])
        self.assertEqual([c[0] for c in self.runner.calls], ["send"])
        # the first run's card still waits for the person
        self.assertEqual(self.mcp(hi.MCP_LIST_TOOL_NAME, {})["pending"], 1)


class CompiledRungBlockTests(unittest.TestCase):
    """The production runner and a direct compiled call over a fake transport
    that serves a Cloudflare Turnstile page."""

    def setUp(self):
        import test_routine_example as rex
        from anyuse.execution.api_step import ApiResponse
        self.rex = rex
        self.tmp = tempfile.mkdtemp()
        self.storage, self.hosts = rex.setup(self.tmp)

        class Challenge(rex.FakeTransport):
            def send(self, method, url, headers, body, timeout_s):
                self.sent.append(url)
                return ApiResponse(403, {"Content-Type": "text/html"},
                                   "<html><title>Just a moment...</title>"
                                   "<div class='cf-turnstile'></div></html>")
        self.transport = Challenge()
        self._env = mock.patch.dict(os.environ, {
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(self.tmp, "blocked.json"),
            "ANYUSE_DATA_DIR": os.path.join(self.tmp, "data")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_routine_step_block_becomes_a_challenge_card_and_is_not_resent(self):
        from anyuse.execution.api_step import ApiStepExecutor
        from anyuse.execution.routine import CompiledToolRunner
        acknowledge_host("crates.io", path=self.hosts, ignore_robots=True,
                         robots_reason="my own site")
        runner = CompiledToolRunner(self.storage, self.hosts,
                                    ApiStepExecutor(transport=self.transport))
        inbox = hi.Inbox(os.path.join(self.tmp, "inbox"), hosts_path=self.hosts)
        with mock.patch.dict(os.environ, {hi.POLICY_ENV: hi.CONTINUE_IN_OWN_SESSION}):
            eng = Engine(self.rex.build_manifest(self.storage, self.hosts),
                         {"compiled_tool": runner},
                         ReceiptStore(os.path.join(self.tmp, "r")), inbox=inbox)
            res = eng.run({"q": "serde"})
            card = inbox.get(res["card"]["id"])
            self.assertEqual((card["kind"], card["host"]), ("challenge", "crates.io"))
            self.assertEqual(card["how"], "human_does_step")    # HTTP rung shares no session
            eng.resume(res["run_id"])
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(hi.Inbox().cards(), [])                # no standalone duplicate

    def test_a_direct_compiled_call_returns_needs_human_and_posts_a_card(self):
        from anyuse.execution.api_step import ApiStepExecutor
        from anyuse.mcpserver import compiled_tools
        out = compiled_tools.run_compiled(
            "crates_search", {"q": "serde"}, storage=self.storage,
            executor=ApiStepExecutor(transport=self.transport), hosts_path=self.hosts)
        self.assertEqual(out["status"], "needs_human")
        need = out["needs_human"]
        self.assertEqual((need["kind"], need["host"]), ("challenge", "crates.io"))
        self.assertIn("will not get around it", need["needed"])
        cards = hi.Inbox().cards()
        self.assertEqual([c["id"] for c in cards], [need["card"]])
        asker = Elicitor(accept(done=True, result="3 crates named serde*"))
        res = hi.elicit_card(hi.Inbox(), need["card"], engine_for=None, elicitor=asker)
        self.assertEqual(res["status"], "done_by_human")
        self.assertEqual(res["human_result"], "3 crates named serde*")
        self.assertEqual(hi.Inbox().cards(), [])
        self.assertEqual(len(self.transport.sent), 1)


if __name__ == "__main__":
    unittest.main()
