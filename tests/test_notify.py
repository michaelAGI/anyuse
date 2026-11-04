"""Owner notifications for waiting cards. Offline: the iMessage runner is a
fake (osascript is NEVER executed), the webhook uses an in-memory HTTP handler."""
import io
import json
import os
import tempfile
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch

from anyuse import settings
from anyuse.execution import notify as N

CARD = {"id": "0123456789ab", "kind": "value", "host": "bank.example",
        "needed": "type your account PIN 4711", "instruction": "secret instr"}


class FakeRunner:
    def __init__(self, rc=0):
        self.rc, self.calls = rc, []

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        return self.rc


class FakePoster:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, url, body):
        self.calls.append((url, body))
        return self.status


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _state():
    return os.path.join(tempfile.mkdtemp(), "notify_state.json")


def _im(runner, clock=None, **kw):
    return N.Notifier("imessage", imessage_to="+49 170 1234567", runner=runner,
                      state_path=_state(), clock=clock or Clock(), **kw)


def test_default_is_none_and_sends_nothing():
    for name in ("ANYUSE_NOTIFY_CHANNEL", "ANYUSE_NOTIFY_IMESSAGE_TO",
                 "ANYUSE_NOTIFY_WEBHOOK_URL"):
        os.environ.pop(name, None)
    runner, poster = FakeRunner(), FakePoster()
    n = N.Notifier(runner=runner, poster=poster, state_path=_state())
    out = n.notify_card(CARD)
    assert n.channel == "none" and not out["sent"]
    assert runner.calls == [] and poster.calls == []


def test_elicitation_attached_means_no_notice():
    runner = FakeRunner()
    out = _im(runner).notify_card(CARD, elicitation_attached=True)
    assert not out["sent"] and runner.calls == []


def test_message_is_short_and_value_free():
    text = N.message_for(CARD)
    assert "0123456789ab" in text and "anyuse inbox do 0123456789ab" in text
    assert "a value only you know" in text
    for leak in ("bank.example", "4711", "PIN", "secret instr"):
        assert leak not in text
    assert len(text) < 200


def test_imessage_script_is_constant_and_args_are_argv():
    runner = FakeRunner()
    out = _im(runner).notify_card(CARD)
    assert out["sent"] and len(runner.calls) == 1
    argv, stdin = runner.calls[0]
    assert argv[:2] == ["/usr/bin/osascript", "-"]
    assert argv[2] == "+49 170 1234567" and argv[3].startswith("AnyUse: card 0123456789ab")
    assert stdin == N.IMESSAGE_SCRIPT and "on run argv" in stdin
    assert "0123456789ab" not in stdin and "+49" not in stdin


def test_hostile_recipient_is_refused_before_anything_runs():
    runner = FakeRunner()
    for bad in ('x" & do shell script "rm -rf ~" & "', "a@b.c; rm", "-e", ""):
        n = N.Notifier("imessage", imessage_to=bad, runner=runner, state_path=_state())
        assert not n.notify_card(CARD)["sent"]
    assert runner.calls == []


def test_card_text_cannot_reach_the_script():
    runner = FakeRunner()
    evil = {"id": "abcdefabcdef", "kind": 'value" & do shell script "x'}
    _im(runner).notify_card(evil)
    argv, stdin = runner.calls[0]
    assert stdin == N.IMESSAGE_SCRIPT
    assert "do shell script" not in argv[3]           # unknown kind -> generic phrase
    assert not _im(FakeRunner()).notify_card({"id": "../../etc", "kind": "value"})["sent"]


def test_dedup_per_card_and_rate_limit():
    runner, clock = FakeRunner(), Clock()
    n = _im(runner, clock)
    assert n.notify_card(CARD)["sent"]
    clock.t += 3600
    assert "already" in n.notify_card(CARD)["reason"]           # never twice
    other = {"id": "fedcba987654", "kind": "approval"}
    third = {"id": "111111111111", "kind": "challenge"}
    assert n.notify_card(other)["sent"]
    clock.t += 5                                                  # too soon
    out = n.notify_card(third)
    assert not out["sent"] and "rate-limited" in out["reason"]
    clock.t += 120
    fourth = {"id": "222222222222", "kind": "do_step"}
    assert n.notify_card(fourth)["sent"]
    assert "(1 more waiting" in runner.calls[-1][0][3]
    assert len(runner.calls) == 3


def test_hourly_cap():
    runner, clock = FakeRunner(), Clock()
    n = _im(runner, clock, min_interval_s=0, max_per_hour=2)
    ids = ["%012x" % i for i in range(1, 5)]
    sent = [n.notify_card({"id": i, "kind": "value"})["sent"] for i in ids]
    assert sent == [True, True, False, False]
    clock.t += 3601
    assert n.notify_card({"id": "%012x" % 9, "kind": "value"})["sent"]


def test_state_file_is_private():
    path = _state()
    N.Notifier("imessage", imessage_to="me@example.org", runner=FakeRunner(),
               state_path=path).notify_card(CARD)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    data = json.load(open(path))
    assert list(data["sent"]) == ["0123456789ab"]


def test_runner_failure_is_not_marked_sent():
    runner = FakeRunner(rc=1)
    n = _im(runner)
    assert not n.notify_card(CARD)["sent"]
    n.runner = FakeRunner(rc=0)
    assert n.notify_card(CARD)["sent"]                 # retried next time


def test_webhook_url_validation():
    assert N._valid_url("https://hooks.example.org/x") == "https://hooks.example.org/x"
    for bad in ("http://hooks.example.org/x", "ftp://a", "https://u:p@a.org/",
                "javascript:alert(1)", "file:///etc/passwd"):
        assert N._valid_url(bad) == "", bad
    assert N._valid_url("http://127.0.0.1:9/x")


def test_webhook_fake_poster_body():
    poster = FakePoster()
    n = N.Notifier("webhook", webhook_url="https://hooks.example.org/a", poster=poster,
                   state_path=_state())
    assert n.notify_card(CARD)["sent"]
    url, body = poster.calls[0]
    assert body["card"] == "0123456789ab" and body["kind"] == "value"
    assert "4711" not in json.dumps(body) and "bank.example" not in json.dumps(body)
    assert not N.Notifier("webhook", webhook_url="https://h.example.org/a",
                          poster=FakePoster(500), state_path=_state()).notify_card(CARD)["sent"]


def test_webhook_serialization_through_in_memory_http_handler():
    got = []
    origin = "http://127.0.0.1:8001"
    class InMemoryHTTP(urllib.request.HTTPHandler):
        def http_open(self, request):
            assert request.full_url == origin + "/hook" and request.get_method() == "POST"
            assert request.get_header("Content-type") == "application/json"
            assert request.get_header("User-agent") == "anyuse-notify/1"
            got.append(json.loads(request.data))
            response = urllib.response.addinfourl(io.BytesIO(b""), Message(), request.full_url, 204)
            response.msg = "No Content"
            return response
    build = urllib.request.build_opener
    with patch.object(urllib.request, "build_opener", lambda *handlers: build(*handlers, InMemoryHTTP())):
        n = N.Notifier("webhook", webhook_url=origin + "/hook", state_path=_state())
        out = n.notify_card(CARD)
    assert out["sent"], out
    assert got and got[0]["card"] == "0123456789ab"


def test_settings_registered_and_security_relevant():
    for name in ("ANYUSE_NOTIFY_CHANNEL", "ANYUSE_NOTIFY_IMESSAGE_TO",
                 "ANYUSE_NOTIFY_WEBHOOK_URL", "ANYUSE_TRIAGE_RANKER"):
        assert name in settings.SETTINGS
    assert settings.SETTINGS["ANYUSE_NOTIFY_IMESSAGE_TO"].security_relevant
    assert settings.SETTINGS["ANYUSE_NOTIFY_WEBHOOK_URL"].security_relevant
    assert settings.SETTINGS["ANYUSE_NOTIFY_CHANNEL"].default == "none"
    assert settings.SETTINGS["ANYUSE_TRIAGE_RANKER"].default == "none"
    try:
        settings.parse_for_storage("ANYUSE_NOTIFY_WEBHOOK_URL", "http://evil.example/x")
    except settings.SettingError:
        pass
    else:
        raise AssertionError("plain http to a public host must be refused")


def test_skill_tools_notifies_only_without_elicitation():
    from anyuse.mcpserver import skill_tools
    tmp = tempfile.mkdtemp()
    saved = {k: os.environ.get(k) for k in ("ANYUSE_DATA_DIR", "ANYUSE_NOTIFY_CHANNEL",
                                            "ANYUSE_NOTIFY_WEBHOOK_URL")}
    poster = FakePoster()
    orig = N.default_webhook_poster
    os.environ.update(ANYUSE_DATA_DIR=tmp, ANYUSE_NOTIFY_CHANNEL="webhook",
                      ANYUSE_NOTIFY_WEBHOOK_URL="https://hooks.example.org/a")
    N.default_webhook_poster = poster
    try:
        res = {"status": "handed_off", "card": {"id": "0123456789ab", "kind": "approval",
                                                  "needed": "approve step s1"}}
        out = skill_tools.ask_human(None, res, None)
        assert out["needs_human"]["asked"] == "no_elicitation"
        assert out["needs_human"]["notified"] == "webhook"
        assert len(poster.calls) == 1
        # the same card again: de-duplicated
        skill_tools.ask_human(None, res, None)
        assert len(poster.calls) == 1
    finally:
        N.default_webhook_poster = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
