"""A site that does not answer never takes a compiled request out of service, and a request
out of service comes back by itself (execution/api_recompile.py, through
``mcpserver/compiled_tools.run_compiled``). A check of it that the site blocks is answered like
any blocked call, and one refused before it was sent is not counted.

Offline: a fake transport, a temp workflow directory, a temp host-ack file and a fake clock
for the health record. Nothing here opens a socket, starts a browser or calls a model.
"""
import datetime
import email.utils
import json
import os
import sys
import urllib.parse

import pytest

from anyuse.execution import api_recompile as R
from anyuse.execution import blocked as B
from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, PlanStep, RequestPlan, RequestTemplate
from anyuse.mcpserver import compiled_tools as CT
from anyuse.mcpserver import meta_tools as MT
from anyuse.mcpserver.compiled_tools import run_compiled

NAME = "crates_search"
START = 1_800_000_000.0


def _body(*names):
    return json.dumps({"crates": [{"id": n} for n in names]})


SERDE = _body("serde", "serde_json", "serde_yaml")
OTHER = _body("rand", "rand_core", "rand_chacha")
CHANGED = '{"packages": []}'


class Site:
    """The site's API: ``answer(q) -> (status, body, headers)``; every request is logged."""

    def __init__(self, status=200, body=SERDE, headers=None):
        self.status, self.body, self.headers = status, body, dict(headers or {})
        self.by_q = {}
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.sent.append(q)
        status, text, extra = self.by_q.get(q, (self.status, self.body, self.headers))
        return ApiResponse(status, {"Content-Type": "application/json", **extra}, text)


class Storage:
    def __init__(self, base):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now

    def iso(self, offset=0.0):
        return R._at(self.now + offset)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    clock = Clock()
    # the health record's clock: outage waits, probe windows and their timestamps
    monkeypatch.setattr(R, "time", clock)
    storage = Storage(str(tmp_path / "wf"))
    template = RequestTemplate(
        method="GET", url="https://crates.io/api/v1/crates", query={"q": "{q}"},
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates",
                "min_items": 1, "required_keys": ["id"]},
        params=["q"])
    record = compiled_record(template, {"passed": True, "passed_count": 2, "case_count": 2,
                                        "reason": "passed on 2 fresh values"}, workflow=NAME)
    persist_compiled(storage, NAME, record)
    hosts = str(tmp_path / "hosts.json")
    acknowledge_host("crates.io", path=hosts)

    class Env:
        pass
    e = Env()
    e.storage, e.hosts, e.clock, e.tmp = storage, hosts, clock, tmp_path
    return e


def _call(env, site, q="serde", **kw):
    return run_compiled(NAME, {"q": q}, storage=env.storage, hosts_path=env.hosts,
                        executor=ApiStepExecutor(transport=site), **kw)


def _stored(env):
    return load_compiled(env.storage, NAME)


def _health(env):
    return R.ApiHealth.from_dict(_stored(env).get("health"))


def _demote(env, last_error, offset=0.0, **fields):
    """Take the stored request out of service as of ``offset`` seconds from now."""
    record = _stored(env)
    record["health"] = {**(record.get("health") or {}), "preferred": False,
                        "demoted_reason": (f"3 consecutive failures (>= 3); last error: {last_error}; "
                                           f"{CT._DEMOTED_NEXT}"),
                        "consecutive_failures": 3, "total_runs": 3, "total_failures": 3,
                        "last_error": last_error, "last_outcome_at": env.clock.iso(offset), **fields}
    persist_compiled(env.storage, NAME, record)


# ---------------------------------------------------------------------------
# a site that does not answer
# ---------------------------------------------------------------------------

def test_server_errors_and_timeouts_never_take_the_request_out_of_service(env):
    down = Site(status=503, body="busy")
    for _ in range(6):
        out = _call(env, down)
        assert not out["ok"] and out["transient"] and "again later" in out["fallback"], out
        assert "record the search again" not in out["fallback"]
        env.clock.now += 3600                       # each wait is over before the next call
    health = _health(env)
    assert health.preferred is True and health.consecutive_failures == 0, health
    assert health.consecutive_outages == 6 and health.total_outages == 6 and health.total_runs == 0
    timed_out = Site()
    timed_out.send = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out"))
    _call(env, timed_out)
    assert _health(env).preferred is True and _health(env).consecutive_outages == 7
    env.clock.now += 3600
    up = Site()
    out = _call(env, up)
    assert out["ok"] and out["result"]["items"][0] == "serde"
    assert _health(env).consecutive_outages == 0 and _health(env).retry_after == ""


def test_the_request_waits_after_the_second_call_the_site_did_not_answer(env):
    down = Site(status=503, body="busy")
    out = _call(env, down)
    assert out["transient"] and "retry_after" not in out, out     # the first one is a blip: no wait
    _call(env, down)
    health = _health(env)
    assert health.consecutive_outages == 2 and health.retry_after == env.clock.iso(30), health
    out = _call(env, down)
    assert len(down.sent) == 2, down.sent                        # held: nothing sent
    assert out["transient"] and out["retry_after"] == env.clock.iso(30), out
    assert env.clock.iso(30) in out["fallback"] and "again later" in out["fallback"]
    assert out["rungs_tried"][0]["skipped"] is True
    assert _health(env).consecutive_outages == 2                # a held call is not counted
    env.clock.now += 31
    _call(env, down)
    assert len(down.sent) == 3 and _health(env).retry_after == env.clock.iso(60)


def test_a_retry_after_on_a_server_error_is_respected(env):
    down = Site(status=503, body="busy", headers={"Retry-After": "120"})
    out = _call(env, down)
    assert out["retry_after"] == env.clock.iso(120), out
    env.clock.now += 119
    _call(env, down)
    assert len(down.sent) == 1
    env.clock.now += 2
    _call(env, down)
    assert len(down.sent) == 2


def test_the_structured_outcome_decides_whether_the_site_answered():
    did = R.site_did_not_answer
    # a status from the site decides, whatever the message quotes from its page
    quoted = ("result contract refused: the page reports an error ('Service temporarily "
              "unavailable'), not an empty result")
    assert not did(200, quoted)
    assert not did(404, "non-2xx status 404 (connection: close)")
    assert did(503, "")
    assert did(None, "", {"status": 502})
    # a block or a held host is an answer, or nothing was sent
    assert not did(None, "host is on do-not-auto-retry: temporarily blocked", {"blocked": True})
    # the executor's own messages: a transport failure, a name that did not resolve
    assert did(None, "transport error: timed out")
    assert did(None, "transport error: pre-send policy refused request: destination address "
                     "resolution failed closed: gaierror")
    assert not did(None, "transport error: pre-send policy refused request: private address")
    # the executor marks a pre-send refusal as a block: a name that did not resolve still counts
    assert did(None, "", {"blocked": True, "outcome_kind": "terminal_refusal",
                          "error": "pre-send policy refused request: destination address "
                                   "resolution failed closed: gaierror"})
    assert not did(None, "", {"blocked": True, "error": "pre-send policy refused request: "
                                                        "origin is not in the allowlist"})
    assert did(None, "non-2xx status 504")
    # an apostrophe inside a word opens no quote
    assert did(None, "the browser couldn't load the page: connection reset by 'shop.test'")
    # the words are the last rung, and never inside a quote
    assert did(None, "Page.navigate timed out; browser answers, the page does not")
    assert not did(None, quoted)
    assert not did(None, 'the page says "connection to the warehouse lost" beside 0 rows')


def test_a_call_reads_the_executors_flags_before_the_words_of_its_message():
    # a held host, a robots refusal or a request held for a person: the site answered, or nothing was sent,
    # whatever the message's words say
    assert not CT._transient({"error": "host is on do-not-auto-retry: temporarily blocked", "blocked": True})
    assert not CT._transient({"error": "robots.txt refuses the path (connection kept open)",
                              "robots_refused": True})
    assert not CT._transient({"error": "the site timed out the session: a person must sign in",
                              "requires_human": True})
    assert CT._transient({"error": "transport error: timed out"})
    assert CT._transient({"status": 503, "error": "", "blocked": False})


def test_a_retry_after_given_as_a_date_is_read():
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=300)
    seconds = B.retry_after_seconds({"Retry-After": email.utils.format_datetime(when, usegmt=True)})
    assert seconds is not None and 290 <= seconds <= 300, seconds
    assert B.retry_after_seconds({"retry-after": "45"}) == 45
    assert B.retry_after_seconds({"Retry-After": "soon"}) is None


def test_a_rate_limit_does_not_count_towards_taking_the_request_out_of_service(env):
    changed = Site(body=CHANGED)
    _call(env, changed)
    _call(env, Site(status=429, body="slow down"), post_card=False)
    _call(env, Site(), post_card=False)                 # refused before sending: the host is held
    B.clear_blocked("crates.io")
    _call(env, changed)
    health = _health(env)
    assert health.preferred is True and health.consecutive_failures == 2, health


# ---------------------------------------------------------------------------
# a request out of service comes back
# ---------------------------------------------------------------------------

def test_a_request_taken_out_by_server_errors_is_probed_and_restored(env):
    _demote(env, "non-2xx status 503")
    site = Site()
    out = _call(env, site)
    assert not out["ok"] and site.sent == [] and "demoted" in out["error"], out
    assert out["out_of_service"] == {"cause": "outage", "next_check_after": env.clock.iso(600)}
    assert out["fallback"].startswith(f"the site did not answer; AnyUse tries again after "
                                      f"{env.clock.iso(600)}"), out["fallback"]
    assert "record the search again" not in out["fallback"] + out["error"]
    assert "heal" not in out                          # no heal while the site did not answer
    env.clock.now += 601
    out = _call(env, site)
    assert out["ok"] and out["served_by"] == "api" and site.sent == ["serde"], out
    assert out["result"]["items"] == ["serde", "serde_json", "serde_yaml"]
    assert out["restored"]["by"] == "probe" and out["restored"]["at"] == env.clock.iso()
    assert "back in service" in out["next"]
    stored = _stored(env)
    assert stored["health"]["preferred"] is True and stored["health"]["demoted_reason"] == ""
    assert stored["restored"]["was"].startswith("3 consecutive failures")
    assert stored["restored"]["at"] == env.clock.iso() and "passed" in stored["restored"]["why"]
    out = _call(env, site, "serde")
    assert out["ok"] and "restored" not in out


def test_a_probe_that_fails_waits_twice_as_long_for_the_next(env):
    _demote(env, "postcondition failed: path 'crates' not found")
    site = Site(body=CHANGED)
    out = _call(env, site)
    assert out["out_of_service"]["cause"] == "answers"
    assert "record the search again" in out["fallback"] and env.clock.iso(600) in out["fallback"]
    env.clock.now += 601
    out = _call(env, site)
    assert site.sent == ["serde"] and not out["ok"], out
    assert out["probe"]["restored"] is False and out["probe"]["next_check_after"] == env.clock.iso(1200)
    health = _health(env)
    assert health.probe_failures == 1 and health.last_probe_outage is False
    env.clock.now += 1199
    _call(env, site)
    assert site.sent == ["serde"]
    env.clock.now += 2
    _call(env, site)
    assert site.sent == ["serde", "serde"] and _health(env).probe_after == env.clock.iso(2400)
    # the back-off stops at a day
    assert R._doubling(600.0, 12, 86400.0) == 86400.0


def test_a_probe_the_site_does_not_answer_says_so_and_waits(env):
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    down = Site(status=503, body="busy")
    out = _call(env, down)
    assert down.sent == ["serde"] and out["out_of_service"]["cause"] == "outage", out
    assert "the site did not answer" in out["fallback"] and "heal" not in out


def test_a_probe_answered_with_a_retry_after_waits_at_least_that_long(env):
    _demote(env, "non-2xx status 503", offset=-601)
    down = Site(status=503, body="busy", headers={"Retry-After": "7200"})
    out = _call(env, down)
    assert down.sent == ["serde"] and out["probe"]["next_check_after"] == env.clock.iso(7200), out
    env.clock.now += 1201                       # the doubled window is over, the site's wait is not
    _call(env, down)
    assert down.sent == ["serde"]
    env.clock.now += 6000
    _call(env, down)
    assert down.sent == ["serde", "serde"]


def test_a_probe_whose_request_was_replaced_while_it_ran_says_so(env, monkeypatch):
    _demote(env, "non-2xx status 503", offset=-601)
    monkeypatch.setattr(R, "restore", lambda *a, **k: None)
    out = _call(env, Site())
    assert not out["ok"] and "replaced while this call ran" in out["probe"]["why"], out
    assert "passed the tool's checks" not in out["probe"]["why"]


def test_a_probe_answer_that_passes_the_contract_but_not_the_last_good_answer_is_not_restored(env):
    assert _call(env, Site())["ok"]                                  # the anchor is serde's answer
    assert _stored(env)["drift_anchor"]["value"] == "serde"
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    lying = Site(body=OTHER)
    out = _call(env, lying, "tokio")
    assert lying.sent == ["tokio", "serde"] and not out["ok"], out
    assert out["probe"]["restored"] is False and "repeats 0%" in out["probe"]["why"], out["probe"]
    assert "result" not in out and _health(env).preferred is False
    # the same site answering the last good answer again restores it
    env.clock.now += 1201
    good = Site()
    good.by_q["tokio"] = (200, _body("tokio", "tokio-util", "tokio-stream"), {})
    out = _call(env, good, "tokio")
    assert out["ok"] and good.sent == ["tokio", "serde"] and out["restored"]["by"] == "probe", out
    assert out["result"]["items"] == ["tokio", "tokio-util", "tokio-stream"]


def test_parallel_calls_send_one_probe(env):
    _demote(env, "non-2xx status 503", offset=-601)
    first = _stored(env)
    claimed, _ = R.claim_probe(env.storage, NAME, first)
    again, health = R.claim_probe(env.storage, NAME, first)
    assert claimed is True and again is False and health.probe_after == env.clock.iso(600)


def test_a_record_with_no_time_gets_its_first_probe_scheduled(env):
    record = _stored(env)
    record["health"] = {"preferred": False, "demoted_reason": "endpoint changed"}
    persist_compiled(env.storage, NAME, record)
    site = Site()
    out = _call(env, site)
    assert site.sent == [] and out["out_of_service"]["next_check_after"] == env.clock.iso(600), out
    env.clock.now += 601
    assert _call(env, site)["ok"] and site.sent == ["serde"]


def test_a_request_demoted_on_wrong_answers_gets_its_probe_window(env):
    changed = Site(body=CHANGED)
    for _ in range(3):
        _call(env, changed)
    health = _health(env)
    assert health.preferred is False and health.demoted_at == env.clock.iso()
    assert health.probe_after == env.clock.iso(600)
    assert "record the search again" in health.demoted_reason


# ---------------------------------------------------------------------------
# a probe the site blocks
# ---------------------------------------------------------------------------

CAPTCHA = '<html><body><div class="g-recaptcha" data-sitekey="k"></div></body></html>'
SIGN_IN = "<html><body><p>Please sign in to continue.</p></body></html>"


@pytest.mark.parametrize("status, body, headers, reason", [
    (403, CAPTCHA, {}, B.CAPTCHA_RECAPTCHA),
    (200, SIGN_IN, {}, B.LOGIN_WALL),
    (429, "slow down", {"Retry-After": "7200"}, B.RATE_LIMITED),
])
def test_a_probe_the_site_blocks_returns_the_block_and_its_inbox_card(env, status, body, headers, reason):
    from anyuse.execution import human_inbox
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    site = Site(status=status, body=body, headers=headers)
    out = _call(env, site)
    assert site.sent == ["serde"] and not out["ok"], out
    # the same outcome as a call to a request in service that the site blocked
    assert out["blocked"]["reason"] == reason and out["action_required"] == "human", out
    assert out["status"] == "needs_human" and out["needs_human"]["card"], out
    assert "demoted" not in out["error"] and "out_of_service" not in out
    assert MT._web_failure(out, "crates.io", NAME)["class"] != "demoted"
    cards = human_inbox.Inbox(hosts_path=env.hosts).cards()
    assert [c["host"] for c in cards] == ["crates.io"], cards
    # the request stays out of service and the probe counts: the site answered it
    assert out["probe"]["sent"] is True and out["probe"]["restored"] is False
    assert _health(env).preferred is False and _health(env).probe_failures == 1


def test_a_probe_answered_with_a_rate_limit_is_reported_as_a_rate_limit(env):
    _demote(env, "non-2xx status 503", offset=-601)
    out = _call(env, Site(status=429, body="slow down", headers={"Retry-After": "7200"}))
    assert out["blocked"]["reason"] == B.RATE_LIMITED and "rate limit" in out["fallback"], out
    assert "did not answer" not in out["fallback"]
    assert out["probe"]["next_check_after"] == env.clock.iso(7200)
    health = _health(env)
    assert health.last_probe_rate_limited and R.demotion_cause(health) == "rate_limit"
    # the calls until the next check say the site asked AnyUse to wait
    site = Site()
    out = _call(env, site)
    assert site.sent == [] and out["out_of_service"]["cause"] == "rate_limit", out
    assert out["fallback"].startswith(f"the site asked AnyUse to wait (rate limit); AnyUse tries again "
                                      f"after {env.clock.iso(7200)}"), out["fallback"]
    assert "record the search again" not in out["fallback"] + out["error"] and "heal" not in out
    meta = MT._web_failure(out, "crates.io", NAME)
    assert meta["next"].startswith("the site asked AnyUse to wait (rate limit)"), meta
    row = CT.list_compiled(env.storage, env.hosts)[0]
    assert row["health"]["out_of_service_cause"] == "rate_limit"


def test_a_probe_refused_by_a_held_host_sends_nothing_and_is_not_counted(env):
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    B.mark_blocked(B.BlockedState(blocked=True, reason=B.CAPTCHA_GENERIC, host="crates.io", ttl_s=3600))
    site = Site()
    out = _call(env, site)
    assert site.sent == [] and out["blocked"]["reason"] == B.CAPTCHA_GENERIC, out
    assert out["action_required"] == "human" and out["needs_human"]["card"], out
    assert out["probe"]["sent"] is False
    health = _health(env)
    assert health.probe_failures == 0 and health.last_probe == "", health
    # claiming the probe moved the next check one window ahead, as for any probe
    assert out["probe"]["next_check_after"] == env.clock.iso(600) == health.probe_after
    B.clear_blocked("crates.io")
    env.clock.now += 601
    out = _call(env, site)
    assert out["ok"] and site.sent == ["serde"] and out["restored"]["by"] == "probe", out


@pytest.mark.parametrize("chain", [False, True])
def test_a_probe_the_pre_send_policy_refuses_in_the_transport_is_not_counted(env, chain):
    # the transport checks the destination before the first hop and times that check, so the
    # refusal comes back with a time on the wire although nothing was sent
    if chain:
        plan = RequestPlan(steps=[PlanStep("search", RequestTemplate.from_dict(_stored(env)["template"]))],
                           params=["q"], shape="list")
        persist_compiled(env.storage, NAME, compiled_record(
            plan, {"passed": True, "passed_count": 2, "case_count": 2}, workflow=NAME))
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    refused = Site()
    refused.send = lambda *a, **k: (_ for _ in ()).throw(PermissionError(
        "pre-send policy refused request: nonpublic destination '10.0.0.7' is refused by default"))
    out = _call(env, refused)
    assert not out["ok"] and "pre-send policy refused request" in out["probe"]["why"], out
    assert out["probe"]["sent"] is False, out["probe"]
    health = _health(env)
    assert health.probe_failures == 0 and health.last_probe == "", health
    # a name that did not resolve is the site not answering: that probe counts
    unresolved = Site()
    unresolved.send = lambda *a, **k: (_ for _ in ()).throw(PermissionError(
        "pre-send policy refused request: destination address resolution failed closed: no address"))
    env.clock.now += 601
    out = _call(env, unresolved)
    assert out["probe"]["sent"] is True and _health(env).probe_failures == 1, out["probe"]


def test_a_probe_robots_txt_refuses_is_not_counted(env, monkeypatch):
    from anyuse.execution import api_permission
    monkeypatch.setattr(api_permission, "replay_refusal",
                        lambda url, hosts_path=None, policy=None: "robots.txt disallows /api/v1/crates")
    _demote(env, "postcondition failed: path 'crates' not found", offset=-601)
    site = Site()
    out = _call(env, site)
    assert site.sent == [] and out["probe"]["sent"] is False, out
    assert _health(env).probe_failures == 0 and _health(env).last_probe == ""


# ---------------------------------------------------------------------------
# what the listing, the meta tools and the CLI say
# ---------------------------------------------------------------------------

def test_the_listing_offers_a_request_out_of_service_when_its_probe_is_due(env):
    _demote(env, "non-2xx status 503")
    row = CT.list_compiled(env.storage, env.hosts)[0]
    assert row["available_via"] is None and not row["runnable_without_human"]
    assert row["health"]["next_check_after"] == env.clock.iso(600)
    assert row["health"]["out_of_service_cause"] == "outage"
    op = MT._web_operation(NAME, _stored(env), env.hosts).row
    assert op["ready"] is False and env.clock.iso(600) in op["why_not"]
    env.clock.now += 601
    row = CT.list_compiled(env.storage, env.hosts)[0]
    assert row["available_via"] == "api" and row["runnable_without_human"], row
    tools = CT.executable_typed_tools(env.storage, env.hosts)
    assert [t["workflow"] for t in tools] == [NAME]
    assert "the next call checks whether the compiled request answers again" in tools[0]["description"]


def test_the_meta_tool_and_the_cli_say_what_happened(env):
    from anyuse import cli as C
    _demote(env, "non-2xx status 503")
    out = _call(env, Site())
    meta = MT._web_failure(out, "crates.io", NAME)
    assert meta["class"] == "demoted" and meta["next"].startswith("the site did not answer; AnyUse "
                                                                 "tries again after"), meta
    assert "record the search again" not in meta["next"] and "compile" not in meta["next"]
    assert C._play_next_lines(NAME, out, False) == [f"Next: {out['fallback']}."]
    _demote(env, "postcondition failed: path 'crates' not found")
    out = _call(env, Site(body=CHANGED))
    meta = MT._web_failure(out, "crates.io", NAME)
    assert "record the search again" in meta["next"] and env.clock.iso(600) in meta["reason"], meta
    down = Site(status=503, body="busy", headers={"Retry-After": "90"})
    record = _stored(env)
    record["health"] = {"preferred": True}
    persist_compiled(env.storage, NAME, record)
    out = _call(env, down)
    meta = MT._web_failure(out, "crates.io", NAME)
    assert meta["class"] == "unavailable" and env.clock.iso(90) in meta["next"], meta
    assert env.clock.iso(90) in C._play_next_lines(NAME, out, False)[0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
