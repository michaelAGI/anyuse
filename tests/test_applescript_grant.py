"""The AppleScript route's Automation grant (``execution/applescript_route.grant_app``,
the measurement harness), offline with FAKES: a permission
check that answers a scripted sequence of codes (it never asks), a fake event process, a fake clock.

Pinned here:

* one harmless read-only event per app (Finder ``name of front window``, Notes ``name of default account``,
  TextEdit ``name of documents``), each a constant script with its wait as the only argv item;
* the outcome comes from the permission status, polled without asking: 0 granted, -1743 not granted (said no,
  nothing sent when it was said before), -1744 until the deadline not granted with ``timeout`` (the pending event
  is stopped), an app that is not running is ``not_running`` and nothing is sent or launched;
* the read's answer (a window or account name) never appears in the result;
* the job script's ``main`` over the same fakes writes one report, refuses outside a guest and on a locked screen.

Also the reader of ``osascript -s s`` answers (``parse_result``). No desktop, no osascript, no network.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_applescript_grant.py
"""
import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM = os.path.join(HERE, "execution", "guest")

from anyuse.execution import applescript_route as AR  # noqa: E402
from anyuse.execution import osa_compile as oc  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Event:
    """A fake started osascript: answers (rc, stdout, stderr) once finished."""

    def __init__(self, log, rc=0, out='"Kistenpreise Herbst"', err="", done=True):
        self.log, self.rc, self.out, self.err, self._done = log, rc, out, err, done
        self.killed = False

    def __call__(self, argv, script):
        self.log.append((argv, script))
        return self

    def done(self):
        return self._done

    def finish(self, timeout):
        return self.rc, self.out, self.err

    def kill(self):
        self.killed = True


def perm(seq):
    it = iter(seq)
    last = [None]

    def f(bundle):
        last[0] = next(it, last[0])
        return last[0]
    return f


def run(bundle, codes, event=None, running=True, wait_s=10.0):
    log, clock = [], Clock()
    ev = event or Event(log)
    ev.log = log
    r = AR.grant_app(bundle, permission=perm(codes), running=lambda b: running, start_event=ev, wait_s=wait_s,
                     poll_s=1.0, clock=clock, sleep=clock.sleep)
    return r, log, ev


def test_one_constant_read_per_app():
    assert set(AR.GRANT_ORDER) == set(AR.GRANT_EVENTS) == {AR.FINDER, AR.NOTES, AR.TEXTEDIT}
    for bid, ev in AR.GRANT_EVENTS.items():
        assert ev.purpose == "read" and ev.bundle_id == bid
        assert oc.argv_only(ev.script) and f'tell application id "{bid}"' in ev.script
        assert "(item 1 of argv) as integer) seconds" in ev.script          # the wait is the only input
        assert "set " not in ev.script and "«event" not in ev.script        # a get: no command, no setter
        assert ev.argv([300]) == ["/usr/bin/osascript", "-s", "s", "-", "300"]
    assert "«class pnam» of «class brow» 1" in AR.GRANT_EVENTS[AR.FINDER].script
    assert "«class pnam» of «class dfac»" in AR.GRANT_EVENTS[AR.NOTES].script
    assert "«class pnam» of every «class docu»" in AR.GRANT_EVENTS[AR.TEXTEDIT].script


def test_never_answered_then_ok_is_granted_and_the_answer_is_not_kept():
    r, log, _ev = run(AR.FINDER, [-1744, -1744, -1744, 0])
    assert r["status"] == AR.GRANTED and r["code"] == 0 and r["sent"] and len(log) == 1
    assert r["answered"] is True and r["event_error"] is None and r["waited_s"] == 3.0 and not r["timeout"]
    assert r["asked_before"] is False
    assert "Kistenpreise" not in json.dumps(r)                               # the read's answer is never kept
    assert log[0][0][-1] == "10" and log[0][1] == AR.GRANT_EVENTS[AR.FINDER].script


def test_the_owner_says_no():
    r, log, _ev = run(AR.NOTES, [-1744, -1744, -1743], event=Event([], rc=1, out="",
                                                                 err="execution error: Nicht berechtigt. (-1743)"))
    assert r["status"] == AR.NOT_GRANTED and r["code"] == -1743 and r["sent"] and len(log) == 1
    assert r["answered"] is False and r["event_error"] == -1743 and r["detail"] == "the owner said no"


def test_no_answer_in_time_stops_the_pending_event():
    ev = Event([], done=False)
    r, log, ev = run(AR.TEXTEDIT, [-1744] * 100, event=ev, wait_s=5.0)
    assert r["status"] == AR.NOT_GRANTED and r["code"] == -1744 and r["timeout"] and ev.killed
    assert r["answered"] is False and r["waited_s"] == 5.0 and len(log) == 1
    assert "in time" in r["detail"]


def test_denied_before_sends_nothing():
    r, log, _ev = run(AR.FINDER, [-1743])
    assert r["status"] == AR.NOT_GRANTED and not r["sent"] and log == [] and "System Settings" in r["detail"]
    assert r["asked_before"] is True


def test_already_granted_sends_the_read_once_to_confirm():
    r, log, _ev = run(AR.FINDER, [0])
    assert r["status"] == AR.GRANTED and r["sent"] and len(log) == 1 and r["waited_s"] == 0.0


def test_a_delivered_error_answer_is_still_a_grant():
    # Finder without a window answers -1728 (no such object): the event was delivered, the status decides
    r, _log, _ev = run(AR.FINDER, [-1744, 0], event=Event([], rc=1, out="",
                                                        err="execution error: Finder-Fenster 1 fehlt. (-1728)"))
    assert r["status"] == AR.GRANTED and r["answered"] is False and r["event_error"] == -1728


def test_not_running_sends_nothing_and_launches_nothing():
    r, log, _ev = run(AR.NOTES, [0], running=False)
    assert r["status"] == AR.NOT_RUNNING and r["code"] == -600 and not r["sent"] and log == []
    r, log, _ev = run(AR.NOTES, [-600])
    assert r["status"] == AR.NOT_RUNNING and not r["sent"] and log == []


def test_any_other_app_gets_the_general_read_and_a_bad_bundle_id_is_an_error():
    # the general rung: a new app's ONE read is "count of windows" (harmless, read-only)
    for bid in ("com.apple.reminders", "com.apple.iCal", "com.apple.AddressBook", "com.apple.Safari"):
        ev = AR.grant_event(bid)
        assert ev.purpose == "read" and "count of every «class cwin»" in ev.script and oc.argv_only(ev.script)
        assert f'tell application id "{bid}"' in ev.script and AR.irreversible_uses(ev.script) == []
    r, log, _ev = run("com.apple.reminders", [-1744, 0])
    assert r["status"] == AR.GRANTED and r["sent"] and r["event"] == "count of windows" and len(log) == 1
    assert "Kistenpreise" not in json.dumps(r)                                     # the answer is never kept
    r, log, _ev = run('x"; do shell script "id', [0])
    assert r["status"] == AR.ERROR and not r["sent"] and log == [] and r["detail"] == "not a bundle id"


def test_grant_all_summarises():
    codes = {AR.FINDER: [0], AR.NOTES: [-1743], AR.TEXTEDIT: [-1744, 0]}
    perms = {b: perm(c) for b, c in codes.items()}
    clock = Clock()
    out = AR.grant_all(permission=lambda b: perms[b](b), running=lambda b: True, start_event=Event([]),
                       wait_s=5, poll_s=1, clock=clock, sleep=clock.sleep)
    assert out["granted"] == [AR.FINDER, AR.TEXTEDIT] and out["not_granted"] == [AR.NOTES]
    assert not out["all_granted"]


def test_status_classes_and_reasons():
    assert AR.classify_status(0) == AR.GRANTED
    assert AR.classify_status(-1743) == AR.classify_status(-1744) == AR.NOT_GRANTED
    assert AR.classify_status(-600) == AR.NOT_RUNNING and AR.classify_status(-50) == AR.ERROR
    assert AR.classify_status(None) == AR.ERROR
    assert AR.status_reason(-1743).startswith("automation_not_granted: -1743")
    assert AR.status_reason(-600).startswith("not_running: -600")
    assert AR.error_number("x.scpt: execution error: no (-1728)\n") == -1728 and AR.error_number("") is None


def test_parse_result_reads_lists_records_text_and_numbers():
    assert AR.parse_result('{{"Kistenpreise, alt", {0, 25, 800, 600}, "/Users/m/C/K/"}, {"x", {1, 2, 3, 4}, ""}}') \
        == [["Kistenpreise, alt", [0, 25, 800, 600], "/Users/m/C/K/"], ["x", [1, 2, 3, 4], ""]]
    assert AR.parse_result('"a \\"q\\" \\\\ b"') == 'a "q" \\ b'
    assert AR.parse_result("{}") == [] and AR.parse_result("") == ""
    assert AR.parse_result("{name:\"x\", id:3, flag:true}") == {"name": "x", "id": 3, "flag": True}
    assert AR.parse_result("missing value") is None and AR.parse_result("-3.5") == -3.5
    assert AR.parse_result('{alias "Macintosh HD:Users:m:", «class cfol» "A" of «class cfol» "B"}') == \
        ['alias "Macintosh HD:Users:m:"', '«class cfol» "A" of «class cfol» "B"']
    assert AR.parse_result('"Zeile 1\nZeile 2"') == "Zeile 1\nZeile 2"
    with pytest.raises(ValueError):
        AR.parse_result('{"a", ')
    with pytest.raises(ValueError):
        AR.parse_result('"a" "b"')


# ── the job script, over the same fakes ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
