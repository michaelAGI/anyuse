"""The global spend cap and ledger (execution/spend.py), offline with fake transports.

Pinned here: arming (only the CLI group, the MCP server's ``serve()`` and the intake app
factory arm; no bench and no other execution module does); an unarmed process reads no
cap and never touches ``<data>/spend/ledger.jsonl``, even with a cap of 0 on file (checked
in a fresh process); the cap refuses before a request is sent; ``unlimited`` behind the
typed-code gate, lowering without it; sums per feature and per UTC day; fail-closed on a
corrupt ledger or an unreadable cap; ``ANYUSE_RUN_BUDGET_USD=unlimited``; each draft
proposal or fill booked once in the ledger beside its own log; the read-only readout.

No network and no model: every answer comes from ``httpx.MockTransport``.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_spend_cap.py
"""
from __future__ import annotations

import glob
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from click.testing import CliRunner

from anyuse import settings
from anyuse.cli import cli
from anyuse.execution import draft_fill as DF
from anyuse.execution import draft_params as DP
from anyuse.execution import model_adapter as M
from anyuse.execution import spend

HERE = Path(__file__).resolve().parent.parent
CAP = spend.CAP_SETTING
NOON = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def answer(content='{"ok": true}', prompt=100, completion=50, cost=None, status=200):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion}
    if cost is not None:
        usage["cost"] = cost
    return status, {"choices": [{"message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}], "usage": usage}


class Fake:
    """An httpx transport that answers from a list and records what was sent."""

    def __init__(self, *replies, on_request=None):
        self.replies = list(replies) or [answer()]
        self.sent = []
        self.on_request = on_request
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.sent.append(json.loads(request.content))
        if self.on_request:
            self.on_request()
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(status, json=body)


def adapter(fake, *, price=1.0, max_tokens=1000, feature="", budget=1.0, retries=0):
    """A ModelAdapter on the fake: each request reserves max_tokens x 2 x price / 1M."""
    cfg = M.ModelConfig("local", "http://127.0.0.1:9/v1", "fake-model", "", max_tokens=max_tokens,
                        retries=retries, price_in=price, price_out=price)
    return M.ModelAdapter(cfg, transport=fake.transport, sleeper=lambda _s: None,
                          budget=M.RunBudget(budget), feature=feature)


def ask(ad):
    return ad.complete([{"role": "user", "content": "x"}])


class Isolated(unittest.TestCase):
    """A fresh data dir, no setting in the environment, an unarmed process."""

    EXTRA_ENV = ("ANYUSE_DATA_DIR", DP.DAILY_CAP_ENV, DF.DAILY_CAP_ENV, "CEREBRAS_API_KEY",
                 "ANYUSE_MODEL_ENV_FILE")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        names = set(settings.SETTINGS) | set(self.EXTRA_ENV)
        self.saved = {name: os.environ.get(name) for name in names}
        for name in names:
            os.environ.pop(name, None)
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name
        os.environ["ANYUSE_MODEL_ENV_FILE"] = os.path.join(self.tmp.name, "no-model.env")
        spend.disarm()

    def tearDown(self):
        spend.disarm()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for root, dirs, _files in os.walk(self.tmp.name):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        self.tmp.cleanup()

    @property
    def ledger(self) -> Path:
        return Path(self.tmp.name, "spend", "ledger.jsonl")

    def rows(self, path=None):
        p = Path(path) if path else self.ledger
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def write_rows(self, *rows):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger, "a") as fh:
            for row in rows:
                fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


# ---------------------------------------------------------------------------
# arming
# ---------------------------------------------------------------------------

class ArmingTests(Isolated):
    def test_only_the_two_entry_points_arm(self):
        self.assertFalse(spend.armed())
        result = CliRunner().invoke(cli, ["version"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(spend.armed(), "the anyuse CLI group arms its process")


        # serve() runs the stdio protocol, so its body is read instead of run
        from anyuse.mcpserver import server
        body = inspect.getsource(server.serve)
        self.assertRegex(body, r"\n    spend\.arm\(\)\n    protocol_stdout")
        self.assertIn("_spend.arm()", inspect.getsource(cli.callback))

    def test_no_bench_and_no_other_execution_module_arms(self):
        call = re.compile(r"(?:^|[^\w])arm\(\s*\)|spend\.arm\b", re.M)
        benches = sorted(glob.glob(str(HERE / "execution" / "web_runtime" / "*.py")))
        self.assertGreater(len(benches), 20)
        for path in benches:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(call.search(text), f"{path} calls arm()")
            self.assertNotRegex(text, r"execution\.spend|execution import spend",
                                f"{path} imports the spend ledger")
        for path in sorted(glob.glob(str(HERE / "execution" / "*.py"))):
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            hits = [m.group(0) for m in call.finditer(text)
                    if not text[max(0, m.start() - 3):m.end()].lstrip().startswith("def")]
            self.assertEqual(hits, [], f"{path} calls arm()")
        for entry in ("cli.py", "mcpserver/server.py"):
            self.assertRegex((HERE / entry).read_text(encoding="utf-8"), r"spend\.arm\(\)", entry)


# ---------------------------------------------------------------------------
# the unarmed process: no cap read, no ledger touched
# ---------------------------------------------------------------------------

PROBE = textwrap.dedent('''
    import json
    import httpx
    from anyuse import settings as S
    from anyuse.execution import model_adapter as M
    from anyuse.execution import spend

    assert not spend.armed()
    assert S.get("ANYUSE_SPEND_DAILY_USD") == 0.0, "the file holds a cap of 0"
    touched = []
    real_cap, real_read, real_book = spend.daily_cap_usd, spend.read_today, spend.book
    def cap(*a, **k):
        touched.append("cap"); return real_cap(*a, **k)
    def read(*a, **k):
        touched.append("ledger read"); return real_read(*a, **k)
    def book(*a, **k):
        touched.append("ledger write"); return real_book(*a, **k)
    spend.daily_cap_usd, spend.read_today, spend.book = cap, read, book
    sent = []
    def handler(request):
        sent.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    cfg = M.ModelConfig("local", "http://127.0.0.1:9/v1", "fake", "", max_tokens=1000,
                        price_in=1.0, price_out=1.0)
    ad = M.ModelAdapter(cfg, transport=httpx.MockTransport(handler), budget=M.RunBudget(1.0))
    oks = [ad.complete([{"role": "user", "content": "x"}]).ok for _ in range(3)]
    ledger = spend.ledger_path()
    out = {"oks": oks, "sent": len(sent), "touched": list(touched),
           "ledger": ledger.exists(), "spend_dir": ledger.parent.exists()}
    spend.arm()                     # the control: the same file refuses once armed
    r = ad.complete([{"role": "user", "content": "x"}])
    out.update(armed_ok=r.ok, armed_error=r.error, armed_sent=len(sent),
               ledger_after=ledger.exists())
    print("PROBE " + json.dumps(out))
''')


class UnarmedTests(Isolated):
    def test_a_fresh_unarmed_process_with_a_zero_cap_calls_and_never_creates_the_ledger(self):
        settings.set_value(CAP, "0")
        self.assertIn(f"{CAP} = 0.0", Path(self.tmp.name, "settings.toml").read_text())
        probe = Path(self.tmp.name, "unarmed_probe.py")
        probe.write_text(PROBE)
        env = {k: v for k, v in os.environ.items() if k not in settings.SETTINGS}
        proc = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        line = [x for x in proc.stdout.splitlines() if x.startswith("PROBE ")][-1]
        out = json.loads(line[len("PROBE "):])
        self.assertEqual(out["oks"], [True, True, True])
        self.assertEqual(out["sent"], 3)
        self.assertEqual(out["touched"], [], "an unarmed process reads no cap and no ledger")
        self.assertFalse(out["ledger"])
        self.assertFalse(out["spend_dir"])
        self.assertFalse(out["armed_ok"])
        self.assertIn(CAP, out["armed_error"])
        self.assertEqual(out["armed_sent"], 3, "the armed control was refused before sending")
        self.assertFalse(out["ledger_after"])
        self.assertFalse(self.ledger.parent.exists())

    def test_in_process_unarmed_calls_book_nothing_and_readout_reads_nothing(self):
        settings.set_value(CAP, "0")
        fake = Fake()
        ad = adapter(fake)
        for _ in range(2):
            self.assertTrue(ask(ad).ok)
        self.assertEqual(len(fake.sent), 2)
        self.assertFalse(self.ledger.parent.exists())
        self.assertEqual(spend.reserve(1.0), (None, ""))
        with self.assertRaises(RuntimeError):
            spend.read_today()
        self.write_rows("not json")             # would refuse an armed process
        info = spend.summary()
        self.assertFalse(info["armed"])
        self.assertIsNone(info["today"])
        self.assertTrue(ask(ad).ok)


# ---------------------------------------------------------------------------
# the cap
# ---------------------------------------------------------------------------

class CapTests(Isolated):
    def test_refused_before_the_request_is_sent(self):
        spend.arm()
        settings.set_value(CAP, "0.001")
        fake = Fake()
        ad = adapter(fake, feature="agent")      # at most 1000 x 2 x $1/M = $0.002
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertTrue(r.budget_exhausted)
        self.assertEqual(r.llm_calls, 0)
        self.assertIn(CAP, r.error)
        self.assertIn("no request was sent", r.error)
        self.assertEqual(fake.sent, [])
        self.assertEqual((ad.budget.spent_usd, ad.budget.reserved_usd), (0.0, 0.0))
        self.assertFalse(self.ledger.exists())

        os.environ[CAP] = "1"                    # the environment beats the file
        r = ask(ad)
        self.assertTrue(r.ok, r.error)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ("feature", "provider", "model", "prompt_tokens",
                                                   "completion_tokens", "cost_basis")},
                         {"feature": "agent", "provider": "local", "model": "fake-model",
                          "prompt_tokens": 100, "completion_tokens": 50,
                          "cost_basis": "tokens x list price"})
        self.assertAlmostEqual(rows[0]["cost_usd"], 150 / 1e6)
        self.assertTrue(rows[0]["at"].endswith("Z"))
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)

        # booked $0.00015 + at most $0.002 > $0.0021
        os.environ[CAP] = "0.0021"
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertRegex(r.error, r"today \(\d{4}-\d\d-\d\d UTC\) US\$0\.000[12] is booked")
        self.assertEqual(len(fake.sent), 1)

    def test_an_unreadable_cap_refuses(self):
        spend.arm()
        fake = Fake()
        ad = adapter(fake)
        for bad in ("lots", "-1", "inf", "nan"):
            os.environ[CAP] = bad
            r = ask(ad)
            self.assertFalse(r.ok, bad)
            self.assertIn(CAP, r.error)
        os.environ.pop(CAP)
        settings_file = Path(self.tmp.name, "settings.toml")
        settings_file.write_text(f'{CAP} = "lots"\n')   # resolve() would fall back to 5.0
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertIn("is not a non-negative amount", r.error)
        settings_file.write_text("this is [not toml\n")
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertEqual(fake.sent, [])
        settings_file.unlink()
        self.assertTrue(ask(ad).ok)

    def test_provider_cost_and_upper_bound_bases_and_retries_book_one_row_each(self):
        spend.arm()
        fake = Fake((429, {}), answer(cost=0.0042))
        ad = adapter(fake, retries=1, feature="agent_l2")
        r = ask(ad)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.llm_calls, 2)
        rows = self.rows()
        self.assertEqual([row["cost_basis"] for row in rows],
                         ["upper bound (no usage reported)", "provider"])
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.002)
        self.assertAlmostEqual(rows[1]["cost_usd"], 0.0042)
        self.assertAlmostEqual(ad.budget.spent_usd, 0.0062)

    def test_a_booking_that_fails_after_the_call_is_reported(self):
        spend.arm()

        def break_ledger():
            self.ledger.unlink()
            self.ledger.mkdir()
        fake = Fake(on_request=break_ledger)
        ad = adapter(fake)
        with self.assertLogs("anyuse.execution.spend", level="ERROR") as logs:
            r = ask(ad)
        self.assertTrue(r.ok)
        self.assertIn("could not be written", r.spend_booking_error)
        self.assertIn("spend_booking_error", r.to_dict())
        self.assertIn("could not be written", logs.output[0])
        r = ask(ad)                                    # the ledger cannot be read now
        self.assertFalse(r.ok)
        self.assertEqual(len(fake.sent), 1)
        self.assertNotIn("spend_booking_error", M.ModelResult().to_dict())


class UnlimitedTests(Isolated):
    def _set(self, value, gate):
        from anyuse.execution import human_gate
        with mock.patch.object(human_gate, "TtyGate", return_value=gate) as cls:
            result = CliRunner().invoke(cli, ["settings", "set", CAP, value])
        return result, cls

    def test_raising_or_lifting_needs_the_typed_code_lowering_does_not(self):
        refused = mock.Mock(return_value="refused: no person at the terminal")
        result, _ = self._set("unlimited", refused)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no person", result.output)
        self.assertFalse(Path(self.tmp.name, "settings.toml").exists())
        summary, _digest = refused.call_args.args
        self.assertEqual(summary["effects"]["value"], "unlimited")
        self.assertIn("no daily spend cap", summary["effects"]["warning"])

        allowed = mock.Mock(return_value=None)
        result, _ = self._set("unlimited", allowed)
        self.assertEqual(result.exit_code, 0, result.output)
        allowed.assert_called_once()
        self.assertEqual(settings.load_file()[CAP], "unlimited")
        self.assertEqual(CliRunner().invoke(cli, ["settings", "get", CAP]).output.strip(),
                         "unlimited")

        for value, gated in (("1", False), ("5", False), ("5.01", True), ("0", False),
                             ("12.5", True)):
            gate = mock.Mock(return_value=None)
            result, cls = self._set(value, gate)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(cls.called, gated, value)
            self.assertEqual(settings.load_file()[CAP], float(value))
        with self.assertRaises(settings.SettingError):
            settings.parse_for_storage(CAP, "inf")
        with self.assertRaises(settings.SettingError):
            settings.parse_for_storage(CAP, "-0.5")
        self.assertEqual(settings.SETTINGS[CAP].default, 5.0)

    def test_unlimited_lets_any_amount_through_and_still_books(self):
        spend.arm()
        settings.set_value(CAP, "unlimited")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write_rows({"at": today, "feature": "agent", "cost_usd": 1000.0})
        fake = Fake()
        r = ask(adapter(fake, feature="planner"))
        self.assertTrue(r.ok, r.error)
        self.assertEqual([row["feature"] for row in self.rows()], ["agent", "planner"])
        self.assertTrue(math.isinf(spend.daily_cap_usd()[0]))


# ---------------------------------------------------------------------------
# sums per feature and per UTC day
# ---------------------------------------------------------------------------

class SumTests(Isolated):
    def test_sums_per_feature_and_the_utc_day_rolls_over(self):
        spend.arm()
        yesterday = NOON - timedelta(days=1)
        self.write_rows(
            {"at": "2026-10-08T23:59:59Z", "feature": "agent", "cost_usd": 4.9},
            {"at": "2026-10-08T10:00:00Z", "feature": "draft_fill", "cost_usd": float("nan")},
            {"at": "2026-10-09T00:00:00Z", "feature": "draft_propose", "cost_usd": 0.01},
            {"at": "2026-10-09T08:00:00Z", "feature": "agent", "cost_usd": 0.02},
            {"at": "2026-10-09T11:00:00Z", "feature": "agent", "cost_usd": 0.02},
            {"at": "2026-10-09T11:30:00Z", "cost_usd": -3},
        )
        today = spend.read_today(NOON)
        self.assertEqual(today.day, "2026-10-09")
        self.assertAlmostEqual(today.total, 0.05)
        self.assertEqual({k: (round(v["usd"], 6), v["calls"]) for k, v in today.by_feature.items()},
                         {"agent": (0.04, 2), "draft_propose": (0.01, 1), "unlabelled": (0.0, 1)})
        # yesterday's NaN row did not refuse today; read as yesterday, it refuses
        self.assertIn("not a finite amount", spend.read_today(yesterday).problem)
        self.assertTrue(math.isinf(spend.read_today(yesterday).total))

        # $4.90 yesterday leaves today's whole $5 cap; a minute before midnight it does not
        ticket, why = spend.reserve(4.9, "agent", now=NOON)
        self.assertEqual(why, "")
        spend.book(ticket, provider="p", model="m", cost_usd=4.9, cost_basis="test", now=NOON)
        self.assertAlmostEqual(spend.read_today(NOON).total, 4.95)
        _t, why = spend.reserve(0.06, "agent", now=NOON)
        self.assertIn("would be passed", why)
        tomorrow = NOON + timedelta(days=1)
        ticket, why = spend.reserve(4.99, "agent", now=tomorrow)
        self.assertEqual(why, "")
        spend.book(ticket, provider="p", model="m", cost_usd=0.5, cost_basis="test", now=tomorrow)
        self.assertAlmostEqual(spend.read_today(tomorrow).total, 0.5)

    def test_requests_in_flight_count_until_booked(self):
        spend.arm()
        os.environ[CAP] = "0.003"
        first, why = spend.reserve(0.002, "agent")
        self.assertEqual(why, "")
        second, why = spend.reserve(0.002, "judge")
        self.assertIsNone(second)
        self.assertIn("in flight", why)
        spend.book(first, provider="p", model="m", cost_usd=0.0005, cost_basis="test")
        second, why = spend.reserve(0.002, "judge")
        self.assertEqual(why, "")
        spend.book(second, provider="p", model="m", cost_usd=float("nan"), cost_basis="?")
        rows = self.rows()
        self.assertEqual([r["cost_basis"] for r in rows],
                         ["test", "upper bound (no usage reported)"])
        self.assertAlmostEqual(rows[1]["cost_usd"], 0.002)

    def test_features_are_summed_from_adapter_requests(self):
        spend.arm()
        for feature, n in (("agent", 2), ("draft_fill", 1)):
            ad = adapter(Fake(), feature=feature)
            for _ in range(n):
                self.assertTrue(ask(ad).ok)
        unlabelled = M.ModelAdapter(adapter(Fake()).config, transport=Fake().transport)
        self.assertTrue(ask(unlabelled).ok)
        role = M.ModelAdapter.for_role("judge", provider="local", base_url="http://127.0.0.1:9/v1",
                                       model="fake", api_key_env="", transport=Fake().transport)
        self.assertEqual(role.feature, "judge")
        self.assertTrue(ask(role).ok)
        by = spend.read_today().by_feature
        self.assertEqual({k: v["calls"] for k, v in by.items()},
                         {"agent": 2, "draft_fill": 1, "unlabelled": 1, "judge": 1})


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------

class FailClosedTests(Isolated):
    def test_a_corrupt_or_unreadable_ledger_means_no_request(self):
        spend.arm()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fake = Fake()
        ad = adapter(fake)
        for content, why in (
                ("not json", "not JSON"),
                ("[1, 2]", "not a ledger row"),
                ('{"cost_usd": 0.1}', "not a ledger row"),
                (f'{{"at": "{today}", "cost_usd": NaN}}', "not a finite amount"),
                (f'{{"at": "{today}", "cost_usd": Infinity}}', "not a finite amount"),
                (f'{{"at": "{today}", "cost_usd": "viel"}}', "not a finite amount"),
                (f'{{"at": "{today}", "cost_usd": true}}', "not a finite amount")):
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps({"at": today, "cost_usd": 0.001}) + "\n" + content + "\n")
            r = ask(ad)
            self.assertFalse(r.ok, content)
            self.assertIn(why, r.error)
            self.assertIn(str(self.ledger), r.error)
            self.assertIn("line 2", r.error)
        self.assertEqual(fake.sent, [])

        self.ledger.unlink()
        self.ledger.mkdir()                                  # neither readable nor writable
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertIn("cannot be read", r.error)
        self.ledger.rmdir()

        os.chmod(self.ledger.parent, 0o500)                  # readable, nothing can be appended
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertIn("cannot be written", r.error)
        os.chmod(self.ledger.parent, 0o700)
        self.assertEqual(fake.sent, [])
        self.assertTrue(ask(ad).ok)
        self.assertEqual(len(self.rows()), 1)


# ---------------------------------------------------------------------------
# the per-run budget can be lifted
# ---------------------------------------------------------------------------

class RunBudgetTests(Isolated):
    def test_unlimited_run_budget(self):
        os.environ["ANYUSE_RUN_BUDGET_USD"] = "unlimited"
        budget = M.RunBudget.from_env()
        self.assertTrue(math.isinf(budget.limit_usd))
        self.assertTrue(math.isinf(budget.remaining_usd))
        reservation = budget.reserve(100_000, 100.0, 100.0)          # $20 maximum
        self.assertIsNotNone(reservation)
        self.assertTrue(math.isinf(budget.remaining_usd))
        budget.settle(reservation, None)
        self.assertAlmostEqual(budget.spent_usd, 20.0)
        doc = budget.to_dict()
        json.dumps(doc, allow_nan=False)
        self.assertEqual((doc["limit_usd"], doc["remaining_usd"], doc["unlimited"]),
                         (None, None, True))
        self.assertNotIn("unlimited", M.RunBudget(0.02).to_dict())
        self.assertTrue(math.isinf(M.RunBudget(math.inf).limit_usd))

        fake = Fake()
        cfg = adapter(fake, price=10.0, max_tokens=100_000).config   # $2 maximum
        ad = M.ModelAdapter(cfg, transport=fake.transport)            # budget from the setting
        self.assertTrue(math.isinf(ad.budget.limit_usd))
        self.assertTrue(ask(ad).ok)

        spend.arm()                                   # the daily cap still applies
        os.environ[CAP] = "0.5"
        r = ask(ad)
        self.assertFalse(r.ok)
        self.assertIn(CAP, r.error)
        self.assertEqual(len(fake.sent), 1)

        for bad in ("inf", "nan", "-0.1", "lots"):
            os.environ["ANYUSE_RUN_BUDGET_USD"] = bad
            with self.assertRaises(ValueError):
                M.RunBudget.from_env()
        with self.assertRaises(ValueError):
            M.RunBudget(float("nan"))

    def test_raising_the_run_budget_needs_the_typed_code(self):
        from anyuse.execution import human_gate
        name = "ANYUSE_RUN_BUDGET_USD"
        for value, gated in (("unlimited", True), ("2", True), ("1.0", False), ("0.01", False)):
            gate = mock.Mock(return_value=None)
            with mock.patch.object(human_gate, "TtyGate", return_value=gate) as cls:
                result = CliRunner().invoke(cli, ["settings", "set", name, value])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(cls.called, gated, value)
        self.assertEqual(settings.load_file()[name], 0.01)
        refused = mock.Mock(return_value="refused")
        with mock.patch.object(human_gate, "TtyGate", return_value=refused):
            result = CliRunner().invoke(cli, ["settings", "set", name, "unlimited"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(settings.load_file()[name], 0.01)
        self.assertIn("one run may spend without limit",
                      settings.security_warning(name, "unlimited"))


# ---------------------------------------------------------------------------
# draft proposals and fills: their own caps and logs, one ledger row each
# ---------------------------------------------------------------------------

class DraftBookingTests(Isolated):
    def setUp(self):
        super().setUp()
        os.environ["CEREBRAS_API_KEY"] = "fake-key-for-a-mock-transport"
        self.fake = Fake(answer('{"parameters": []}', prompt=400, completion=300))
        from anyuse.execution import api_step
        self.patch = mock.patch.object(api_step, "import_module_httpx_transport",
                                       return_value=self.fake.transport)
        self.patch.start()
        self.candidates = [DP.Candidate("c1", "typed", "Hallo", [1],
                                        {"role": "textfield", "title": "Text"})]

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_each_request_is_booked_once_in_the_ledger_and_once_in_its_own_log(self):
        spend.arm()
        out = DP.propose("write a note", self.candidates)
        self.assertEqual(out["problems"], [])
        text, why = DF.ModelGate().ask([{"role": "user", "content": "fill the gap"}], "gap fill")
        self.assertEqual((text, why), ('{"parameters": []}', ""))
        self.assertEqual(len(self.fake.sent), 2)

        ledger = self.rows()
        self.assertEqual([r["feature"] for r in ledger], ["draft_propose", "draft_fill"])
        propose_log = self.rows(DP.spend_log_path())
        fill_log = self.rows(DF.spend_log_path())
        self.assertEqual((len(propose_log), len(fill_log)), (1, 1))
        expected = (400 * 0.25 + 300 * 0.69) / 1e6
        for row in (ledger[0], ledger[1], propose_log[0], fill_log[0]):
            self.assertAlmostEqual(row["cost_usd"], expected)
        self.assertEqual(ledger[0]["model"], "gpt-oss-120b")
        # the draft modules never write the ledger themselves
        for module in (DP, DF):
            source = inspect.getsource(module)
            self.assertNotIn("ledger.jsonl", source.replace("``spend/ledger.jsonl``", ""))
            self.assertNotRegex(source, r"spend\.book|spend\.reserve")

    def test_a_request_needs_room_under_its_feature_cap_and_the_global_cap(self):
        spend.arm()
        os.environ[CAP] = "0"
        out = DP.propose("write a note", self.candidates)
        self.assertEqual(out["parameters"], [])
        self.assertIn("no model answer", out["problems"][0])
        self.assertIn(CAP, out["problems"][0])
        text, why = DF.ModelGate().ask([{"role": "user", "content": "fill"}], "gap fill")
        self.assertIsNone(text)
        self.assertIn(CAP, why)
        self.assertEqual(self.fake.sent, [])
        self.assertFalse(self.ledger.exists())
        for log in (DP.spend_log_path(), DF.spend_log_path()):
            self.assertEqual(Path(log).read_text(), "", "a refused request is booked nowhere")

        os.environ.pop(CAP)
        os.environ[DP.DAILY_CAP_ENV] = "0"
        out = DP.propose("write a note", self.candidates)
        self.assertIn("daily cap of US$0.0000 for parameter proposals", out["problems"][0])
        os.environ[DF.DAILY_CAP_ENV] = "0"
        text, why = DF.ModelGate().ask([{"role": "user", "content": "fill"}], "gap fill")
        self.assertIn(DF.DAILY_CAP_ENV, why)
        self.assertEqual(self.fake.sent, [])

    def test_unarmed_draft_requests_keep_their_own_logs_only(self):
        out = DP.propose("write a note", self.candidates)
        self.assertEqual(out["problems"], [])
        self.assertEqual(len(self.rows(DP.spend_log_path())), 1)
        self.assertFalse(self.ledger.exists())


# ---------------------------------------------------------------------------
# the readout
# ---------------------------------------------------------------------------

class ReadoutTests(Isolated):
    def test_settings_spend_on_a_fresh_data_dir_creates_nothing(self):
        result = CliRunner().invoke(cli, ["settings", "spend"])
        self.assertEqual(result.exit_code, 0, result.output)
        for pattern in (r"global, per UTC day\s+US\$5\.0000\s+\(ANYUSE_SPEND_DAILY_USD, default\)",
                        r"per run\s+US\$1\.0000\s+\(ANYUSE_RUN_BUDGET_USD, default\)",
                        r"parameter proposals, per day\s+US\$0\.2500",
                        r"gap fills, per day\s+unlimited", "no model request booked today",
                        r"total\s+US\$0\.0000\s+\(US\$5\.0000 left"):
            self.assertRegex(result.output, pattern)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_settings_spend_sums_per_feature(self):
        spend.arm()
        os.environ[DP.DAILY_CAP_ENV] = "0.5"
        os.environ["ANYUSE_RUN_BUDGET_USD"] = "unlimited"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.write_rows({"at": today, "feature": "agent", "cost_usd": 0.25},
                        {"at": today, "feature": "agent", "cost_usd": 0.25},
                        {"at": today, "feature": "draft_propose", "cost_usd": 0.001},
                        {"at": "2020-01-01T00:00:00Z", "feature": "agent", "cost_usd": 9})
        result = CliRunner().invoke(cli, ["settings", "spend"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertRegex(result.output, r"agent\s+US\$0\.5000\s+2 request")
        self.assertRegex(result.output, r"draft_propose\s+US\$0\.0010\s+1 request")
        self.assertRegex(result.output, r"total\s+US\$0\.5010\s+\(US\$4\.4990 left")
        self.assertRegex(result.output, r"per run\s+unlimited\s+\(ANYUSE_RUN_BUDGET_USD, env\)")
        self.assertIn("US$0.5000  (ANYUSE_DRAFT_PROPOSE_DAILY_USD, env)", result.output)

        result = CliRunner().invoke(cli, ["settings", "spend", "--json"])
        info = json.loads(result.output)
        self.assertTrue(info["armed"])
        self.assertEqual(info["today"]["by_feature"]["agent"], {"usd": 0.5, "calls": 2})
        self.assertEqual(info["caps"]["global_daily"]["usd"], 5.0)
        self.assertTrue(info["caps"]["per_run"]["unlimited"])
        self.assertTrue(info["caps"]["draft_fill_daily"]["unlimited"])
        self.assertAlmostEqual(info["remaining_usd"], 4.499)

        self.write_rows("garbage")
        result = CliRunner().invoke(cli, ["settings", "spend"])
        self.assertIn("not JSON (line 5)", result.output)

    def test_doctor_prints_one_spend_line(self):
        result = CliRunner().invoke(cli, ["doctor", "--json"])
        rows = {row["check"]: row for row in json.loads(result.output)["checks"]}
        row = rows["spend cap"]
        self.assertTrue(row["ok"], row)
        self.assertEqual(row["found"].split(" spent today")[0], "US$0.0000")
        self.assertIn("cap US$5.00 per UTC day (ANYUSE_SPEND_DAILY_USD, default)", row["found"])
        self.assertFalse(Path(self.tmp.name, "spend").exists())

        result = CliRunner().invoke(cli, ["doctor"])
        lines = [x for x in result.output.splitlines() if "spend cap:" in x]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[ok  ] spend cap: US$0.0000 spent today"))

        self.write_rows("garbage")
        result = CliRunner().invoke(cli, ["doctor", "--json"])
        row = {r["check"]: r for r in json.loads(result.output)["checks"]}["spend cap"]
        self.assertFalse(row["ok"])
        self.assertIn("not JSON", row["found"])

    def test_docs_name_the_setting_and_the_cost_basis(self):
        doc = (HERE / "docs" / "SETTINGS.md").read_text(encoding="utf-8")
        self.assertRegex(doc, r"\| `ANYUSE_SPEND_DAILY_USD` \* \| non-negative number or `unlimited` \| `5\.0`")
        self.assertRegex(doc, r"\| `ANYUSE_RUN_BUDGET_USD` \* \| non-negative number or `unlimited`")
        self.assertIn("## Spend caps", doc)
        for fact in ("0.25", "0.69", "$1.00", "$0.03", "150 Sonnet"):
            self.assertIn(fact, doc.split("## Spend caps", 1)[1])



class ClaudeCliPolicyTests(Isolated):
    """agent_run / `anyuse agent run --policy claude` calls `claude -p` outside the
    model adapter; it is held to the same daily cap and booked in the same ledger."""

    def test_a_claude_cli_policy_call_is_reserved_capped_and_booked(self):
        from anyuse.execution import harness_policies as HP
        ran = []

        def runner(argv, stdin, timeout):
            ran.append(argv)
            return 0, json.dumps({"result": "ok", "total_cost_usd": 0.0412,
                                  "usage": {"input_tokens": 900, "output_tokens": 40}}), ""
        spend.arm()
        os.environ[CAP] = "1"
        text, meta = HP.claude_cli_call(runner, model="sonnet", feature="agent")("sys", "user")
        self.assertEqual((text, len(ran)), ("ok", 1))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ("feature", "provider", "model", "prompt_tokens",
                                                   "completion_tokens", "cost_basis")},
                         {"feature": "agent", "provider": "claude-cli", "model": "sonnet",
                          "prompt_tokens": 900, "completion_tokens": 40, "cost_basis": "provider"})
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.0412)
        # the cap left no room for another call: refused before the CLI starts, nothing booked
        os.environ[CAP] = "0.2"
        text, meta = HP.claude_cli_call(runner, model="sonnet", feature="agent")("sys", "user")
        self.assertEqual(text, "")
        self.assertTrue(meta["budget_exhausted"])
        self.assertIn(CAP, meta["error"])
        self.assertEqual((len(ran), len(self.rows())), (1, 1))

    def test_a_cli_that_never_started_books_nothing_billed(self):
        from anyuse.execution import harness_policies as HP

        def missing(argv, stdin, timeout):
            raise FileNotFoundError("claude")
        spend.arm()
        os.environ[CAP] = "1"
        text, meta = HP.claude_cli_call(missing, feature="agent")("s", "u")
        self.assertEqual(text, "")
        self.assertEqual([r["cost_usd"] for r in self.rows()], [0.0])


class AuditBudgetRegressions(Isolated):
    def test_long_prompt_refused_before_provider_under_both_caps(self):
        os.environ[CAP] = "0.0002"
        spend.arm()
        fake = Fake(answer(prompt=10000, completion=100))
        ad = adapter(fake, price=1., max_tokens=100, budget=.0002)
        result = ad.complete([{"role": "user", "content": "word " * 10000}])
        self.assertTrue(result.budget_exhausted)
        self.assertEqual(fake.sent, [])
        self.assertEqual(ad.budget.spent_usd, 0.)

    def test_booking_keeps_reservation_until_append_is_visible(self):
        import threading
        os.environ[CAP] = "1"
        spend.arm()
        first, why = spend.reserve(1., "first")
        self.assertIsNotNone(first); self.assertFalse(why)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        real_path = spend._armed_ledger_path
        bookings, second = [], []
        def path():
            if threading.current_thread().name == "book-audit":
                entered.set()
                if not release.wait(5): raise RuntimeError("test barrier expired")
            return real_path()
        def book():
            bookings.append(spend.book(first, provider="fake", model="fake", cost_usd=1., cost_basis="fixture"))
        def reserve():
            second.append(spend.reserve(1., "second")); done.set()
        with mock.patch.object(spend, "_armed_ledger_path", path):
            writer = threading.Thread(target=book, name="book-audit")
            reader = threading.Thread(target=reserve)
            writer.start()
            try:
                self.assertTrue(entered.wait(2))
                reader.start()
                self.assertFalse(done.wait(.05))
            finally:
                release.set(); writer.join(5)
                if reader.ident is not None: reader.join(5)
        self.assertEqual(bookings, [""])
        self.assertIsNone(second[0][0])
        self.assertEqual(sum(row["cost_usd"] for row in self.rows()), 1.)


if __name__ == "__main__":
    unittest.main()
