"""Tests for the watcher service daemon.

Covers:
  - WATCHER_ENABLED gating: unset ⇒ run() refuses (fail-closed), status file
    records state=disabled
  - consent preflight: a definitively-denied TCC grant ⇒ refusal with
    instructions; unknown (None — headless env) ⇒ proceeds with warning
  - supervised start: transient start failures retry with backoff and
    eventually run; persistent failure exhausts attempts and refuses
  - the run loop: ticks, writes a CONTENT-FREE status file (counters only,
    never summaries/entities), and stops the watcher cleanly
  - the optional push producer: shareable + confirmed records push through the
    sender, local_only is skipped, the cursor advances (no re-push next tick),
    and a transport failure stops the batch WITHOUT advancing the cursor past
    the failed record (retried next tick, in order)
  - AbstractionStore.updated_since returns oldest-first (updated_at, record)
  - launchd plist content: label, module entrypoint, KeepAlive, WATCHER_ENABLED

Self-executing (pytest is not installed in this env). No real key stores, no
network, no TCC prompts — everything side-effecting is injected.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_watcher_service.py
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.watcher.models import AbstractionRecord
from anyuse.watcher.storage import AbstractionStore
from anyuse.watcher.service import (
    ENABLED_ENV,
    LAUNCHD_LABEL,
    WatcherService,
    consent_blocks_start,
    plist_content,
    watcher_enabled,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _env:
    """Set/unset WATCHER_ENABLED for a block, restoring the prior value."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.prior = os.environ.get(ENABLED_ENV)
        if self.value is None:
            os.environ.pop(ENABLED_ENV, None)
        else:
            os.environ[ENABLED_ENV] = self.value

    def __exit__(self, *a):
        if self.prior is None:
            os.environ.pop(ENABLED_ENV, None)
        else:
            os.environ[ENABLED_ENV] = self.prior


class _StubWatcher:
    def __init__(self):
        self._session = SimpleNamespace(id="sess123", step_events=[])
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


_CONSENT_OK = {"screen_recording": True, "accessibility": True}
_CONSENT_UNKNOWN = {"screen_recording": None, "accessibility": None}


def _service(td, **kw):
    td = pathlib.Path(td)
    defaults = dict(
        watcher_factory=_StubWatcher,
        status_path=td / "status.json",
        cursor_path=td / "cursor.json",
        tick_interval_s=0.01,
        restart_backoff_s=(0.01,),
        consent_checker=lambda: dict(_CONSENT_OK),
        sender=lambda record, base_url="": {"ok": True},
        abstraction_store=SimpleNamespace(updated_since=lambda *a, **k: []),
    )
    defaults.update(kw)
    return WatcherService(**defaults)


# ---------------------------------------------------------------------------

def test_disabled_gate():
    with _env(None), tempfile.TemporaryDirectory() as td:
        svc = _service(td)
        result = _run(svc.run(max_ticks=1))
        check("unset env refuses", result == {"ok": False, "reason": "disabled"})
        status = json.loads((pathlib.Path(td) / "status.json").read_text())
        check("status file records disabled", status["state"] == "disabled")
    with _env("0"):
        check("WATCHER_ENABLED=0 is disabled", not watcher_enabled())
    with _env("1"):
        check("WATCHER_ENABLED=1 is enabled", watcher_enabled())


def test_consent_gate():
    denied = {"screen_recording": False, "accessibility": True}
    check("step capture needs no screen recording", consent_blocks_start(denied) is None)
    denied_ax = {"screen_recording": True, "accessibility": False}
    check("denied accessibility blocks",
          "Accessibility" in (consent_blocks_start(denied_ax) or ""))
    check("unknown consent does not block",
          consent_blocks_start(_CONSENT_UNKNOWN) is None)

    with _env("1"), tempfile.TemporaryDirectory() as td:
        svc = _service(td, consent_checker=lambda: dict(denied_ax))
        result = _run(svc.run(max_ticks=1))
        check("service refuses on denied consent",
              result.get("reason") == "consent")
        status = json.loads((pathlib.Path(td) / "status.json").read_text())
        check("status file records consent_refused",
              status["state"] == "consent_refused")

        svc2 = _service(td, consent_checker=lambda: dict(_CONSENT_UNKNOWN))
        result2 = _run(svc2.run(max_ticks=1))
        check("service proceeds on unknown consent", result2.get("ok") is True)


def test_default_factory_records_steps():
    from unittest.mock import patch
    from anyuse.watcher.batch_watcher import BatchWatcher
    with patch.dict(os.environ):
        watcher = WatcherService._default_factory()
        assert isinstance(watcher, BatchWatcher) and watcher.config.steps_only
        assert watcher.config.output_level == "silent"
        assert os.environ["ANYUSE_WATCH_STRUCTURED_STEPS"] == "1"


def test_run_loop_and_status():
    with _env("1"), tempfile.TemporaryDirectory() as td:
        created = []

        def factory():
            w = _StubWatcher()
            created.append(w)
            return w

        svc = _service(td, watcher_factory=factory)
        result = _run(svc.run(max_ticks=2))
        check("run completes", result.get("ok") is True and result["ticks"] == 2)
        check("watcher started and stopped",
              created and created[0].started and created[0].stopped)

        status = json.loads((pathlib.Path(td) / "status.json").read_text())
        check("final status is stopped", status["state"] == "stopped")
        # Content-free: counters and identifiers only.
        forbidden = {"summary", "structure", "entities", "title", "ai_response"}
        check("status file is content-free",
              not (set(status.keys()) & forbidden), f"keys={list(status)}")


def test_start_backoff_and_exhaustion():
    with _env("1"), tempfile.TemporaryDirectory() as td:
        attempts = {"n": 0}

        def flaky_factory():
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("transient")
            return _StubWatcher()

        svc = _service(td, watcher_factory=flaky_factory)
        result = _run(svc.run(max_ticks=1))
        check("transient start failures recover",
              result.get("ok") is True and svc._start_attempts == 2,
              f"attempts={svc._start_attempts} result={result}")

        def always_fails():
            raise RuntimeError("permanent")

        svc2 = _service(td, watcher_factory=always_fails)
        svc2._max_start_attempts = 2
        result2 = _run(svc2.run(max_ticks=1))
        check("persistent start failure refuses",
              result2 == {"ok": False, "reason": "start_failed",
                          "detail": "permanent"})


# ---------------------------------------------------------------------------
# The optional push producer
# ---------------------------------------------------------------------------

def _mk_store(td) -> AbstractionStore:
    store = AbstractionStore(db_path=pathlib.Path(td) / "abs.db")
    store.upsert(AbstractionRecord(
        kind="task", title="shareable_task", summary="s1",
        apps=["Mail"], privacy_tier="shareable_summary", confidence=0.7))
    store.upsert(AbstractionRecord(
        kind="task", title="confirmed_task", summary="s2",
        apps=["Mail"], confidence=1.0))
    store.upsert(AbstractionRecord(
        kind="task", title="private_task", summary="s3",
        apps=["Mail"], confidence=0.5))  # local_only default
    return store


def _gate_sender(calls, fail_titles=()):
    """The injected sender's contract: skip local_only,
    transport-fail for the given titles."""

    def sender(record, base_url=""):
        calls.append(record.title)
        shareable = (record.privacy_tier == "shareable_summary"
                     or record.confidence >= 1.0)
        if not shareable:
            return {"ok": False, "skipped": True, "error": "record is local_only"}
        if record.title in fail_titles:
            return {"ok": False, "error": "connection refused"}
        return {"ok": True, "status": 200}

    return sender


def test_updated_since():
    with tempfile.TemporaryDirectory() as td:
        store = _mk_store(td)
        batch = store.updated_since("")
        check("updated_since sees all 3", len(batch) == 3)
        stamps = [u for u, _ in batch]
        check("oldest-first ordering", stamps == sorted(stamps))
        newer = store.updated_since(stamps[0])
        check("cursor excludes already-seen", len(newer) == 2)


def test_push_producer():
    with _env("1"), tempfile.TemporaryDirectory() as td:
        store = _mk_store(td)
        calls = []
        svc = _service(td, abstraction_store=store,
                       sender=_gate_sender(calls))
        pushed = svc._push_shareable_abstractions()
        check("pushes the 2 shareable/confirmed records", pushed == 2,
              f"pushed={pushed} calls={calls}")
        check("local_only was offered to the gate and skipped",
              "private_task" in calls)

        calls.clear()
        pushed2 = svc._push_shareable_abstractions()
        check("cursor prevents re-push", pushed2 == 0 and calls == [],
              f"calls={calls}")


def test_push_transport_failure_retries():
    with _env("1"), tempfile.TemporaryDirectory() as td:
        store = _mk_store(td)
        batch = store.updated_since("")
        titles_in_order = [r.title for _, r in batch]
        # Fail the SECOND shareable record in scan order.
        shareable_order = [t for t in titles_in_order if t != "private_task"]
        fail_title = shareable_order[1]

        calls = []
        svc = _service(td, abstraction_store=store,
                       sender=_gate_sender(calls, fail_titles=(fail_title,)))
        pushed = svc._push_shareable_abstractions()
        check("first record pushed before the failure",
              pushed >= 1, f"pushed={pushed}")
        check("push error recorded",
              "connection refused" in svc._last_push_error)

        # Hub back online: the failed record is retried.
        calls.clear()
        svc._sender = _gate_sender(calls)
        pushed2 = svc._push_shareable_abstractions()
        check("failed record retried next tick and succeeds",
              pushed2 >= 1 and fail_title in calls,
              f"pushed2={pushed2} calls={calls}")

        calls.clear()
        check("then nothing left to push",
              svc._push_shareable_abstractions() == 0 and calls == [])


def test_plist_content():
    text = plist_content(python_path="/opt/py", working_dir="/repo")
    for needle in (LAUNCHD_LABEL, "anyuse.watcher.service", "<key>KeepAlive</key><true/>",
                   ENABLED_ENV, "/opt/py", "/repo"):
        check(f"plist contains {needle!r}", needle in text)


if __name__ == "__main__":
    tests = [
        test_disabled_gate,
        test_consent_gate,
        test_default_factory_records_steps,
        test_run_loop_and_status,
        test_start_backoff_and_exhaustion,
        test_updated_since,
        test_push_producer,
        test_push_transport_failure_retries,
        test_plist_content,
    ]
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except Exception as e:  # noqa: BLE001 - report, keep going
            FAIL += 1
            print(f"  FAIL {t.__name__} raised: {type(e).__name__}: {e}")
    if FAIL:
        print(f"\n{PASS} passed, {FAIL} failed")
        sys.exit(1)
    print(f"\n{PASS} passed, 0 failed")
    print("ALL WATCHER SERVICE TESTS PASSED")
