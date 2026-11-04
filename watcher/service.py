"""Watcher service daemon.

The production entrypoint the watcher never had: a supervised loop that owns a
BatchWatcher, gated OFF by default, with the consent preflight the privacy
model requires — plus an optional pusher of shareable abstractions to a sink the embedding
application injects (nothing is sent when no sink is configured).

What it does per tick (default 30s):
  - writes a CONTENT-FREE status file (counters/backend/uptime only — never
    summaries, entities, or screen text) that a host UI can render as the
    "what is the watcher doing" panel;
  - when a sink is injected, pushes shareable/confirmed AbstractionRecords to
    it, resuming from a persisted updated_at cursor (offline sink ⇒ cursor
    stays put, retried next tick).

Gates, all fail-closed:
  - ``WATCHER_ENABLED`` must be truthy ("1"/"true"/"yes") or the service
    refuses to run — the daemon existing must not mean the watcher runs.
  - TCC consent preflight: accessibility must be granted
    (accessibility is privacy-load-bearing — AXSecureTextField suppression is
    blind without it). Definitively-denied ⇒ refuse with instructions.
    Unknown (API unavailable, e.g. headless tests) ⇒ proceed with a warning.

Permanently-on is the LaunchAgent (``--install-launchd``): KeepAlive restarts
the process; in-process supervision only retries a failed start with backoff.

    python -m anyuse.watcher.service                 # run (needs WATCHER_ENABLED=1)
    python -m anyuse.watcher.service --status        # print last status file
    python -m anyuse.watcher.service --check-consent # print TCC consent state
    python -m anyuse.watcher.service --install-launchd / --uninstall-launchd
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from anyuse.paths import anyuse_data_path

logger = logging.getLogger(__name__)

# anyuse uses MIXED flat + package imports (the test suites bootstrap both
# roots). The daemon runs as ``python -m anyuse.watcher.service`` where flat
# imports (``from context.macos import …``) don't resolve — and every one of
# them sits in a try/except that silently degrades to a None-fallback, so the
# daemon loses AX trees / frontmost-app context WITHOUT any error. Making the
# package dir importable here restores the flat root for the whole process.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

ENABLED_ENV = "WATCHER_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")

STATUS_PATH = anyuse_data_path("watcher_status.json")
CURSOR_PATH = anyuse_data_path("watcher_push_cursor.json")
LAUNCHD_LABEL = "com.anyuse.watcher"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def watcher_enabled() -> bool:
    return (os.environ.get(ENABLED_ENV) or "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# TCC consent preflight (spec §11.1 first-run consent flow)
# ---------------------------------------------------------------------------

def check_capture_consent() -> Dict[str, Optional[bool]]:
    """Preflight the Accessibility grant that structured step capture needs.

    True/False when the OS API answers; None when the API is unavailable
    (off-macOS / headless test env) — callers treat None as "unknown".
    """
    accessibility: Optional[bool] = None
    for mod_name in ("ApplicationServices", "HIServices"):
        try:
            mod = __import__(mod_name, fromlist=["AXIsProcessTrusted"])
            accessibility = bool(mod.AXIsProcessTrusted())
            break
        except Exception:
            continue

    return {"accessibility": accessibility}


def consent_blocks_start(consent: Dict[str, Optional[bool]]) -> Optional[str]:
    """The refusal reason when a required grant is definitively denied.

    Accessibility supplies secure-field suppression and step identity.
    """
    if consent.get("accessibility") is False:
        return ("accessibility permission denied (secure-field suppression "
                "needs AX) — grant it in System Settings → Privacy & "
                "Security → Accessibility, then retry")
    return None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class WatcherService:
    """Supervised BatchWatcher owner + status writer + abstraction pusher.

    Everything side-effecting is injectable for tests: ``watcher_factory``
    (returns an object with async start()/stop() and the stats attrs),
    ``abstraction_store``, ``sender`` (``sender(record, base_url=...) -> {ok, skipped?, error?}``; none = no push),
    ``consent_checker``, ``status_path``/``cursor_path``.
    """

    def __init__(
        self,
        watcher_factory: Optional[Callable[[], Any]] = None,
        abstraction_store: Optional[Any] = None,
        sender: Optional[Callable[..., Dict[str, Any]]] = None,
        sink_url: Optional[str] = None,
        status_path: Optional[Path] = None,
        cursor_path: Optional[Path] = None,
        tick_interval_s: float = 30.0,
        restart_backoff_s: tuple = (1.0, 5.0, 30.0),
        max_start_attempts: int = 4,
        consent_checker: Optional[Callable[[], Dict[str, Optional[bool]]]] = None,
    ):
        self._watcher_factory = watcher_factory or self._default_factory
        self._abstraction_store = abstraction_store
        self._sender = sender
        self._sink_url = sink_url or os.environ.get("ANYUSE_ABSTRACTION_SINK_URL", "")
        self.status_path = Path(status_path) if status_path else STATUS_PATH
        self.cursor_path = Path(cursor_path) if cursor_path else CURSOR_PATH
        self.tick_interval_s = tick_interval_s
        self._restart_backoff_s = restart_backoff_s
        self._max_start_attempts = max_start_attempts
        self._consent_checker = consent_checker or check_capture_consent

        self._watcher: Optional[Any] = None
        self._running = False
        self._started_at: Optional[float] = None
        self._start_attempts = 0
        self._pushed_total = 0
        self._last_push_error = ""
        self._refusal_reason = ""

    # -- lazy defaults -----------------------------------------------------

    @staticmethod
    def _default_factory():
        from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig
        # The supervised service explicitly starts the same gated recorder as the CLI.
        os.environ["ANYUSE_WATCH_STRUCTURED_STEPS"] = "1"
        return BatchWatcher(BatchWatcherConfig(steps_only=True, output_level="silent"))

    def _get_store(self):
        if self._abstraction_store is None:
            try:
                from anyuse.watcher.storage import AbstractionStore
                self._abstraction_store = AbstractionStore()
            except Exception as e:
                logger.warning("watcher service: no abstraction store (%s)", e)
        return self._abstraction_store

    def _get_sender(self):
        return self._sender

    # -- lifecycle -----------------------------------------------------------

    async def run(self, max_ticks: Optional[int] = None) -> Dict[str, Any]:
        """The supervised loop. Returns a result dict when it exits —
        ``{ok: False, reason}`` on refusal, ``{ok: True, ...}`` after a clean
        stop. ``max_ticks`` bounds the loop for tests."""
        if not watcher_enabled():
            self._refusal_reason = (
                f"{ENABLED_ENV} is not set — the watcher daemon is "
                "opt-in (fail-closed)")
            self._write_status(state="disabled")
            return {"ok": False, "reason": "disabled"}

        consent = {}
        try:
            consent = self._consent_checker() or {}
        except Exception as e:
            logger.warning("consent preflight failed open-unknown: %s", e)
        block = consent_blocks_start(consent)
        if block:
            self._refusal_reason = block
            self._write_status(state="consent_refused", consent=consent)
            return {"ok": False, "reason": "consent", "detail": block}
        if None in consent.values():
            logger.warning("consent preflight unknown (%s) — proceeding; "
                           "capture will degrade, not leak", consent)

        # Start the watcher, with bounded backoff on start failure.
        while self._start_attempts < self._max_start_attempts:
            try:
                self._watcher = self._watcher_factory()
                await self._watcher.start()
                break
            except Exception as e:
                # The failed watcher may have started listeners (the step tap, input
                # capture) before it raised: release them before a retry builds another.
                failed, self._watcher = self._watcher, None
                await self._release_failed(failed)
                self._start_attempts += 1
                if self._start_attempts >= self._max_start_attempts:
                    self._refusal_reason = f"watcher start failed: {e}"
                    self._write_status(state="start_failed")
                    return {"ok": False, "reason": "start_failed",
                            "detail": str(e)}
                delay = self._restart_backoff_s[
                    min(self._start_attempts - 1,
                        len(self._restart_backoff_s) - 1)]
                logger.warning("watcher start failed (%s); retry in %.1fs", e, delay)
                await asyncio.sleep(delay)

        self._running = True
        self._started_at = time.time()
        self._write_status(state="running", consent=consent)

        ticks = 0
        try:
            while self._running:
                if max_ticks is not None and ticks >= max_ticks:
                    break
                await asyncio.sleep(self.tick_interval_s)
                ticks += 1
                self._push_shareable_abstractions()
                self._write_status(state="running", consent=consent)
        finally:
            await self.stop()

        return {"ok": True, "ticks": ticks, "pushed": self._pushed_total}

    @staticmethod
    async def _release_failed(watcher) -> None:
        if watcher is None:
            return
        try:
            abort = getattr(watcher, "abort_start", None)
            if abort is not None:
                abort()
            else:
                await watcher.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("releasing a failed watcher start failed: %s", e)

    async def stop(self):
        self._running = False
        w, self._watcher = self._watcher, None
        if w is not None:
            try:
                await w.stop()
            except Exception as e:
                logger.warning("watcher stop failed: %s", e)
        self._write_status(state="stopped")

    # -- the optional abstraction push ------------------------------------------

    def _load_cursor(self) -> str:
        try:
            return json.loads(self.cursor_path.read_text()).get("updated_at", "")
        except Exception:
            return ""

    def _save_cursor(self, updated_at: str):
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_path.write_text(json.dumps({"updated_at": updated_at}))
        except Exception as e:
            logger.warning("watcher service: cursor save failed: %s", e)

    def _push_shareable_abstractions(self, limit: int = 200) -> int:
        """Push shareable/confirmed records updated since the cursor through
        the injected sender's privacy gate. The cursor advances THROUGH successes
        and skips (local_only), and STOPS at the first transport failure so an
        offline sink is retried next tick, in order. No sender: nothing is pushed."""
        if self._get_sender() is None:
            return 0
        store = self._get_store()
        if store is None or not hasattr(store, "updated_since"):
            return 0
        try:
            batch = store.updated_since(self._load_cursor(), limit=limit)
        except Exception as e:
            logger.warning("watcher service: cursor scan failed: %s", e)
            return 0
        if not batch:
            return 0

        sender = self._get_sender()
        pushed = 0
        cursor = None
        self._last_push_error = ""  # reflects THIS tick; a failure below re-sets it
        for updated_at, record in batch:
            result: Dict[str, Any] = {}
            try:
                result = sender(record, base_url=self._sink_url) or {}
            except Exception as e:  # sender is non-raising by contract; belt
                result = {"ok": False, "error": str(e)}
            if result.get("ok"):
                pushed += 1
            elif not result.get("skipped"):
                # transport failure — stop here, keep cursor before this row
                self._last_push_error = str(result.get("error", ""))
                break
            cursor = updated_at
        if cursor:
            self._save_cursor(cursor)
        self._pushed_total += pushed
        return pushed

    # -- status surface (content-free by construction) -----------------------

    def status(self) -> Dict[str, Any]:
        w = self._watcher
        stats = {"structured_steps": len(getattr(getattr(w, "_session", None), "step_events", []))}
        session = getattr(w, "_session", None) if w else None
        return {
            "ts": datetime.now().isoformat(),
            "pid": os.getpid(),
            "enabled": watcher_enabled(),
            "backend": "none (structured steps)",
            "session_id": getattr(session, "id", "") if session else "",
            "uptime_s": round(time.time() - self._started_at, 1)
            if self._started_at else 0.0,
            "start_attempts": self._start_attempts,
            "abstractions_pushed": self._pushed_total,
            "last_push_error": self._last_push_error,
            "refusal_reason": self._refusal_reason,
            **stats,
        }

    def _write_status(self, state: str, consent: Optional[Dict] = None):
        payload = {"state": state, **self.status()}
        if consent is not None:
            payload["consent"] = consent
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            self.status_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning("watcher service: status write failed: %s", e)


# ---------------------------------------------------------------------------
# launchd (permanently-on lives HERE, not in the process)
# ---------------------------------------------------------------------------

def plist_content(python_path: Optional[str] = None,
                  working_dir: Optional[str] = None) -> str:
    python_path = python_path or sys.executable
    # service.py -> watcher -> anyuse -> repo root (the dir that makes
    # `import anyuse` resolve)
    working_dir = working_dir or str(Path(__file__).resolve().parents[2])
    log_dir = anyuse_data_path("logs")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>anyuse.watcher.service</string>
    </array>
    <key>WorkingDirectory</key><string>{working_dir}</string>
    <key>EnvironmentVariables</key>
    <dict><key>{ENABLED_ENV}</key><string>1</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log_dir}/watcher.out.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/watcher.err.log</string>
</dict>
</plist>
"""


def install_launchd(plist_path: Optional[Path] = None) -> Path:
    path = Path(plist_path) if plist_path else LAUNCHD_PLIST
    (anyuse_data_path("logs")).mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_content())
    print(f"wrote {path}\nload it with:  launchctl load -w {path}")
    return path


def uninstall_launchd(plist_path: Optional[Path] = None) -> bool:
    path = Path(plist_path) if plist_path else LAUNCHD_PLIST
    if path.exists():
        print(f"unload first if loaded:  launchctl unload {path}")
        path.unlink()
        print(f"removed {path}")
        return True
    print(f"not installed: {path}")
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_status_file():
    try:
        print(STATUS_PATH.read_text())
    except OSError:
        print(json.dumps({"state": "never_ran"}))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anyuse.watcher.service",
        description="Supervised watcher daemon (WATCHER_ENABLED-gated).")
    parser.add_argument("--status", action="store_true",
                        help="print the last status file and exit")
    parser.add_argument("--check-consent", action="store_true",
                        help="print TCC consent state and exit")
    parser.add_argument("--install-launchd", action="store_true")
    parser.add_argument("--uninstall-launchd", action="store_true")
    args = parser.parse_args(argv)

    if args.status:
        _print_status_file()
        return 0
    if args.check_consent:
        print(json.dumps(check_capture_consent(), indent=2))
        return 0
    if args.install_launchd:
        install_launchd()
        return 0
    if args.uninstall_launchd:
        uninstall_launchd()
        return 0

    logging.basicConfig(level=logging.INFO)
    service = WatcherService()

    loop = asyncio.new_event_loop()

    def _sig_stop(*_a):
        loop.create_task(service.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_stop)
        except (NotImplementedError, RuntimeError):
            pass

    result = loop.run_until_complete(service.run())
    if not result.get("ok"):
        print(f"watcher service refused: {result.get('reason')}"
              f" — {result.get('detail', service._refusal_reason)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
