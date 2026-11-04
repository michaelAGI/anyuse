"""Structured desktop step recording, session persistence and capture guards.

The historical BatchWatcher name is retained for callers. Recording uses only
listen-only Quartz events and Accessibility identity; no model or screen frames.
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from anyuse.watcher.models import TagEvent, TagType, WatchSession
from anyuse.watcher import raw_store
from anyuse.watcher.storage import SessionStorage

logger = logging.getLogger("anyuse.watcher.batch")


@dataclass
class BatchWatcherConfig:
    """Step recording and capture-side exclusion settings."""
    # Compatibility field: all recordings now contain structured steps only.
    steps_only: bool = True
    phishing_guard: bool = False
    output_level: str = "msg"

    denylist_apps: tuple = (
        "1Password", "Bitwarden", "Keychain Access", "KeePassXC",
        "LastPass", "Dashlane", "Enpass", "NordPass",
        "Schlüsselbundverwaltung", "Passwords", "Passwörter",
        # macOS permission prompts: a person answers them, they are never workflow steps
        "UserNotificationCenter", "SecurityAgent", "coreautha", "universalAccessAuthWarn",
    )

    denylist_bundle_ids: tuple = (
        "com.apple.UserNotificationCenter", "com.apple.SecurityAgent", "com.apple.authorizationhost",
        "com.apple.keychainaccess", "com.apple.Passwords", "com.1password.1password",
        "com.agilebits.onepassword7", "com.bitwarden.desktop", "org.keepassxc.keepassxc",
    )

    denylist_title_patterns: tuple = (
        "password", "passkey", "recovery code", "private browsing", "incognito",
        "passwort", "passwörter", "kennwort", "wiederherstellungscode", "privater modus",
        "inkognito",
    )

    denylist_finance_apps: tuple = (
        "Wallet", "Sparkasse", "S-Banking", "VR Banking", "VR-Banking", "comdirect", "DKB", "N26", "ING",
        "Commerzbank", "Deutsche Bank", "Postbank", "PayPal", "Klarna", "MoneyMoney", "StarMoney",
        "Outbank", "Finanzguru", "Banking",
    )

    denylist_finance_bundle_ids: tuple = (
        "com.apple.Passbook", "com.apple.wallet", "com.moneymoney-app.retail",
        "com.starfinanz.mac.starmoney", "com.paypal.PPClient",
    )

    denylist_finance_title_words: tuple = (
        "online-banking", "onlinebanking", "online banking", "überweisung", "ueberweisung",
        "kontoauszug", "kontostand", "tan", "pushtan", "phototan", "chiptan", "smstan", "mtan",
        "paypal", "klarna", "sparkasse", "volksbank", "raiffeisenbank", "comdirect", "commerzbank",
        "postbank", "deutsche bank", "dkb", "n26",
    )


class SecureFieldScan:
    """The capture exclusion's "is a secure field visible?" answer, kept off the input path.

    Keyed by the focused window: ``key()`` is a few AX attribute reads (pid, window title and
    frame, the focused element's role) - ``scan(pid)`` a bounded walk of that window
    (``context/accessibility.focused_window_has_secure_field``) on a background thread;
    ``current()`` never waits for it. An answer counts only for the window it was taken
    in: after a switch to another app or window, the answer is unknown until the new
    window was scanned. Within one window a fresh answer (``ttl``) is returned as is, a
    stale one is used for ``grace`` seconds while it refreshes. Unknown - no answer for this
    window, a key that cannot be read, a failed scan, an answer older than ``grace`` - is
    "yes": the capture withholds rather than guess."""

    def __init__(self, scan: Callable[[Optional[int]], bool], key: Callable[[], Optional[tuple]],
                 ttl: float = 2.0, grace: float = 6.0, clock: Callable[[], float] = time.monotonic,
                 key_for: Optional[Callable[[int], Optional[tuple]]] = None):
        self._scan, self._key, self._ttl, self._grace, self._clock = scan, key, ttl, grace, clock
        #: the key of ``pid``'s focused window read afresh (uncached): after a scan it must still be
        #: the key the scan was for, else the answer is discarded (the window changed meanwhile)
        self._key_for = key_for
        self.mismatches = 0
        self._lock = threading.Lock()
        self._for: Optional[tuple] = None
        self._result: Optional[bool] = None
        self._failed = False             # the answer held is a failed scan's (or an unreadable key's)
        self._at: Optional[float] = None
        self._running = False
        self.failures = 0

    def _read_key(self) -> Optional[tuple]:
        try:
            return self._key()
        except Exception:  # noqa: BLE001
            return None

    def refresh(self, key: Optional[tuple] = None) -> bool:
        k = key if key is not None else self._read_key()
        failed = k is None
        if k is None:
            result = True
        else:
            try:
                result = bool(self._scan(k[0]))
            except Exception:  # noqa: BLE001 - a scan that cannot answer withholds
                result, failed = True, True
                self.failures += 1
            # the scan walked the app's focused window as of now (its sheets included): filed only when
            # that is still the window the key names - pid, title, frame. A focus change inside the window
            # (a menu-bar click) keeps the answer; the cache key keeps the focus, so the next lookup with
            # the new focus scans again. Another window meanwhile: discarded (unknown withholds)
            if self._key_for is not None and k[0] is not None and k != ("no-ax",):
                try:
                    live = self._key_for(k[0])
                except Exception:  # noqa: BLE001
                    live = None
                if live is None or tuple(live[:3]) != tuple(k[:3]):
                    self.mismatches += 1
                    with self._lock:
                        self._running = False
                    return True
        with self._lock:
            self._for, self._result, self._at, self._running = k, result, self._clock(), False
            self._failed = failed
        return result

    def wait(self, key: Optional[tuple], timeout: float,
             sleep: Callable[[float], None] = time.sleep) -> Tuple[Optional[bool], bool]:
        """``(answer, failed)`` for ``key`` once a fresh answer exists, kicking a scan OF that key
        when none runs; ``(None, False)`` when none came within ``timeout``. It waits: never call it
        on the input path (the structured-step worker only)."""
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            with self._lock:
                if (key is not None and self._for == key and self._at is not None
                        and self._clock() - self._at < self._ttl):
                    return bool(self._result), self._failed
                self._kick(key)
            if self._clock() >= deadline:
                return None, False
            sleep(0.05)

    def _kick(self, key: Optional[tuple] = None) -> None:
        if not self._running:
            self._running = True
            threading.Thread(target=self.refresh, args=(key,), name="anyuse-secure-scan", daemon=True).start()

    def known(self, key: Optional[tuple]) -> bool:
        """A fresh answer exists for this window."""
        with self._lock:
            return (key is not None and self._for == key and self._at is not None
                    and self._clock() - self._at < self._ttl)

    def current(self, key: Optional[tuple] = None) -> bool:
        k = key if key is not None else self._read_key()
        now = self._clock()
        with self._lock:
            if k is None or self._for != k or self._at is None:
                self._kick()             # unknown window, or another one: scan it, withhold now
                return True
            result, age = bool(self._result), now - self._at
            if age < self._ttl:
                return result
            self._kick()
        return result if age < self._grace else True


class BatchWatcher:
    """Own the structured step tap, privacy guards and encrypted session."""

    _EXCLUSION_TTL_S = 2.0
    _TYPING_SUPPRESSION_FLAG: Optional[Path] = None
    _TYPING_SUPPRESSION_MAX_AGE_S = 30.0
    _WINDOW_KEY_TTL_S = 0.15
    STEP_SCAN_WAIT_S = 0.6

    def __init__(self, config: Optional[BatchWatcherConfig] = None,
                 entity_vault: Optional[Any] = None,
                 phishing_guard: Optional[Any] = None,
                 raw_crypto: Optional[Any] = None,
                 step_capture_factory: Optional[Callable[["BatchWatcher"], Any]] = None):
        self.config = config or BatchWatcherConfig()
        self._raw_crypto = raw_crypto
        self._entity_vault = entity_vault
        self._entity_vault_failed = False
        self._phishing_guard = phishing_guard
        self._phishing_task = None
        self._step_capture_factory = step_capture_factory
        self._step_capture = None
        self._phishing_flag: Optional[Dict[str, Any]] = None
        self._running = False
        self._session: Optional[WatchSession] = None
        self._secure_scan: Optional[SecureFieldScan] = None
        self._last_excluded_phish_check = 0.0
        self._exclusion_cached: Optional[str] = None
        self._exclusion_checked_at = 0.0
        self._exclusion_key: Optional[tuple] = None
        self._wkey: Optional[tuple] = None
        self._wkey_at = 0.0
        self._wfront: Optional[Dict[str, Any]] = None
        self.on_session_complete: Optional[Callable[[WatchSession], None]] = None
        try:
            from anyuse.context.macos import MacOSContext
            self.macos_context = MacOSContext()
        except ImportError:
            self.macos_context = None
        try:
            from anyuse.context.accessibility import AX_AVAILABLE
            self._ax_available = AX_AVAILABLE
        except ImportError:
            self._ax_available = False

    async def _start_impl(self):
        self._running = True
        # start() runs inside the caller's loop (including on Python 3.14).
        self._loop = asyncio.get_running_loop()
        self._session = WatchSession.create_new()
        session_dir = SessionStorage.BASE_DIR / self._session.id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._session.storage_path = str(session_dir)
        try:
            if self.macos_context:
                active = self.macos_context.get_frontmost_app() or {}
                name = (active.get("name") or "").strip()
                if name:
                    self._session.apps_used.append(name)
        except Exception:
            pass
        if self.config.phishing_guard and self._phishing_guard is None:
            from anyuse.watcher.phishing_guard import PhishingGuard
            self._phishing_guard = PhishingGuard()
        if self._ax_available and self.macos_context:
            if self._secure_scan is None:
                self._secure_scan = SecureFieldScan(self._scan_secure_field_now, self._window_key_now,
                                                    key_for=self._key_for_pid)
            self._secure_scan.refresh()
        if not self._start_step_capture():
            raise RuntimeError("structured step recording could not start (see the line above)")
        if self._phishing_guard is not None:
            self._phishing_task = asyncio.create_task(self._phishing_loop())
        if self.config.output_level != "silent":
            print("Step recorder started (AX identity; typed values in the local vault)")

    async def stop(self) -> Optional[WatchSession]:
        self._running = False
        task, self._phishing_task = self._phishing_task, None
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # Child cancellation is normal shutdown; cancellation of
                    # stop() itself must still reach its caller after cleanup.
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    logger.warning("Phishing advisory task failed: %s", exc)
        finally:
            try:
                self._stop_step_capture()
            finally:
                if self._session:
                    self._session.ended_at = datetime.now()
                    self._session.status = "stopped"
                    self._save_session_data()
        if self.config.output_level != "silent":
            count = sum(e.tag_type == TagType.STEP for e in self._session.step_events) if self._session else 0
            print(f"Step recorder stopped. Structured steps: {count}")
        if self.on_session_complete and self._session:
            self.on_session_complete(self._session)
        return self._session

    async def _phishing_loop(self):
        while self._running:
            await self._run_phishing_check()
            await asyncio.sleep(5.0)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def start(self):
        """Start step recording. A start that fails part-way releases what it had
        started (the step tap and advisory task) before
        the error goes up - nothing is left listening for a watcher nobody owns."""
        if self._running:
            return
        try:
            await self._start_impl()
        except BaseException:
            self.abort_start()
            raise

    def abort_start(self) -> None:
        """Release a failed start; idempotent and saves nothing."""
        self._running = False
        if self._phishing_task is not None:
            self._phishing_task.cancel()
            self._phishing_task = None
        self._stop_step_capture()

    def _start_step_capture(self) -> bool:
        """Start the structured step capture when ANYUSE_WATCH_STRUCTURED_STEPS is on and the
        process is Accessibility-trusted (fail-closed: without AX a secure field cannot be
        recognised). Returns whether it runs."""
        from anyuse.watcher import step_capture as SC
        if not SC.enabled() or not self._session:
            return False
        try:
            if self._step_capture_factory is not None:
                capture = self._step_capture_factory(self)
            else:
                why = SC.capture_refusal()
                if why:
                    if self.config.output_level != "silent":
                        print(f"  ⚠️ Structured steps off: {why}")
                    return False
                recorder = SC.StructuredStepRecorder(
                    SC.MacAXProbe(), vault=self._get_entity_vault(), guard=self._step_guard,
                    sink=self._on_structured_event, state_sink=self._on_structured_state,
                    guard_cause=self._step_guard_cause_now)
                capture = SC.StepCapture(recorder, suppressed=self._typing_suppressed)
            self._step_capture = capture
            why = capture.start()
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
        if why:
            self._stop_step_capture()
            if self.config.output_level != "silent":
                print(f"  ⚠️ Structured steps off: {why}")
            return False
        self._step_capture = capture
        if self.config.output_level != "silent":
            print("  🧩 Structured steps on (AX identity per action; typed values in the vault)")
        return True

    def _stop_step_capture(self) -> None:
        capture, self._step_capture = self._step_capture, None
        if capture is None:
            return
        try:
            capture.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("structured step capture stop failed: %s", e)

    def _on_structured_event(self, event: TagEvent) -> None:
        if self._session is not None:
            self._session.step_events.append(event)
            app = str(event.data.get("app") or "")
            if app and app not in self._session.apps_used:
                self._session.apps_used.append(app)

    def _on_structured_state(self, state_id: str, state: dict) -> None:
        if self._session is not None:
            self._session.ax_states.setdefault(state_id, state)

    def _withhold_flagged(self, app: str) -> int:
        """Steps the structured capture recorded in ``app`` during the phishing hold before
        the flag: rewritten to their withheld form, fingerprints only they used dropped, and
        every vault marker they and those fingerprints held (typed values, anchors, labels,
        row labels, list names, window titles) forgotten when nothing kept names it."""
        if not self._session or not app:
            return 0
        from anyuse.watcher import step_capture as SC
        since = datetime.now().timestamp() - SC.PHISHING_HOLD_S
        forgotten: set = set()
        n = SC.withhold_events(self._session.step_events, lambda e: e.data.get("app") == app
                               and e.timestamp.timestamp() >= since,
                               "phishing_suspected", vault=self._get_entity_vault(),
                               states=self._session.ax_states, forgotten=forgotten,
                               drop_state=lambda sid, st: st.get("app") == app
                               and float(st.get("captured_at") or 0) >= since)
        rec = getattr(self._step_capture, "recorder", None)
        if forgotten and rec is not None and hasattr(rec, "forget_cached"):
            rec.forget_cached(forgotten)
        return n

    def _denylist_reason(self, app: str, bundle_id: str, title: Optional[str]) -> Optional[str]:
        """``denylist_app`` / ``denylist_title`` for a surface on the capture denylists (the
        password-manager lists as substrings, the banking / payment / finance lists and the
        settings' additions as whole words, bundle ids exactly), else None."""
        import re
        from anyuse import settings as S
        app_l, title_l = (app or "").lower(), (title or "").lower()

        def extra(name: str) -> List[str]:
            # read at most every 5 s (the guard runs per input event)
            cache = self.__dict__.setdefault("_denylist_settings", {})
            hit = cache.get(name)
            if hit is not None and time.time() - hit[0] < 5.0:
                return hit[1]
            try:
                got = [x.strip() for x in str(S.get(name) or "").split(",") if x.strip()]
            except Exception:  # noqa: BLE001 - an unreadable setting adds nothing
                got = []
            cache[name] = (time.time(), got)
            return got
        added = extra("ANYUSE_WATCH_DENYLIST_APPS")
        bundles = set(self.config.denylist_bundle_ids) | set(self.config.denylist_finance_bundle_ids) | {
            x for x in added if "." in x and " " not in x}
        if bundle_id and bundle_id in bundles:
            return "denylist_app"
        if any(d.lower() in app_l for d in self.config.denylist_apps):
            return "denylist_app"

        def word(w: str, text: str) -> bool:
            return bool(w) and re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", text) is not None
        if any(word(d, app_l) for d in list(self.config.denylist_finance_apps) + added):
            return "denylist_app"
        if any(p.lower() in title_l for p in self.config.denylist_title_patterns):
            return "denylist_title"
        if any(word(t, title_l) for t in list(self.config.denylist_finance_title_words)
               + extra("ANYUSE_WATCH_DENYLIST_TITLES")):
            return "denylist_title"
        return None

    def _step_guard_cause_now(self) -> str:
        return getattr(self, "_step_guard_cause", "")

    def _step_secure_answer(self, front: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """``(reason, sub-cause)`` of the secure-field half for the step's front: the window key of
        the RECORDER's front pid (its read is ticked and cross-checked; nothing is re-read here),
        its scan kicked and waited for up to ``STEP_SCAN_WAIT_S`` on the worker. ``secure_field``
        (fail closed) when the key cannot be read (``key_unreadable``), no answer came
        (``not_scanned_yet``), the scan failed (``scan_failed``) or found one (``found``). A front
        the recorder verified to have no window at all (``windowless``: a click in the menu bar of an
        app whose window list answered empty) has none to show a secure field."""
        if front.get("windowless") is True:
            return None, ""
        info = self._window_info(front.get("pid"))
        if info is None:
            return "secure_field", "key_unreadable"
        key = (info["pid"], info["title"], info["frame"], info["focus"])
        if self._secure_scan is None:
            self._secure_scan = SecureFieldScan(self._scan_secure_field_now, self._window_key_now,
                                                key_for=self._key_for_pid)
        result, failed = self._secure_scan.wait(key, self.STEP_SCAN_WAIT_S)
        if result is None:
            return "secure_field", "not_scanned_yet"
        if failed:
            return "secure_field", "scan_failed"
        if result:
            return "secure_field", "found"
        return None, ""

    def _step_guard(self, front: Dict[str, Any]) -> Optional[str]:
        """The structured capture's per-surface decision for the step's front (content-free reason
        or None): credential typing, the denylist (the step's app and title), a visible secure field
        in the step's window (``_step_secure_answer``: on the worker a window not scanned yet is
        waited for), a surface the phishing guard flagged, a call or meeting surface. Called on
        the structured-step worker only; the front is the recorder's
        (``step_capture.front_application``); a front with no pid is left to the recorder, which
        withholds it. The sub-cause of a ``secure_field`` is ``_step_guard_cause`` (no content)."""
        self._step_guard_cause = ""
        why = self._exclusion_reason()
        if why and (why not in ("secure_field", "surface_unknown") or self._exclusion_checked_at > 0):
            # a definitive answer about the surface now (credential typing, a denylisted front, a
            # secure field found): it holds for the step as well
            self._step_guard_cause = "found" if why == "secure_field" else ""
            return why
        # otherwise (no answer, or only "unknown": AX did not name the window, not scanned yet) the
        # step's own front decides
        if self._typing_suppressed():
            return "credential_typing"
        app = str(front.get("app") or "")
        title = str(front.get("window_title") or "")
        why = self._denylist_reason(app, str(front.get("bundle_id") or ""), title)
        if why:
            return why
        # a front with no pid is left to the recorder, which withholds it (surface_unknown)
        if self._ax_available and self.macos_context and front.get("pid") is not None:
            why, self._step_guard_cause = self._step_secure_answer(front)
            if why:
                return why
        flag = self._phishing_flag
        if flag and flag.get("app") == app and time.time() - flag.get("at", 0) < 600:
            return "phishing_suspected"
        try:
            from anyuse.watcher.step_capture import is_meeting_surface
            if is_meeting_surface(app, title):
                return "meeting_surface"
        except Exception:  # noqa: BLE001 - an unknown meeting state withholds
            return "meeting_surface"
        return None

    def _typing_flag_path(self) -> Path:
        if self._TYPING_SUPPRESSION_FLAG is not None:
            return Path(self._TYPING_SUPPRESSION_FLAG)
        from anyuse.paths import typing_suppression_flag
        return typing_suppression_flag()

    def _typing_suppressed(self) -> bool:
        """True while CredentialVault.use_credential is typing a secret at the
        cursor (it touches the flag file; stale flags expire so a crash mid-type
        cannot suppress capture forever)."""
        try:
            st = self._typing_flag_path().stat()
        except OSError:
            return False
        return (time.time() - st.st_mtime) < self._TYPING_SUPPRESSION_MAX_AGE_S

    def _secure_field_on_screen(self) -> bool:
        """True when the focused window contains an AXSecureTextField (role or subrole) —
        a password/passkey prompt is visible; nothing may be captured. The answer comes from
        ``SecureFieldScan`` (a bounded walk on a background thread, never on the input
        thread); a scan that fails, or no answer yet, is True."""
        if not self._ax_available or not self.macos_context:
            return False
        if self._secure_scan is None:
            self._secure_scan = SecureFieldScan(self._scan_secure_field_now, self._window_key_now,
                                                key_for=self._key_for_pid)
        return self._secure_scan.current(self._window_key_now())

    def _scan_secure_field_now(self, pid: Optional[int]) -> bool:
        from anyuse.context.accessibility import focused_window_has_secure_field
        return focused_window_has_secure_field(pid)

    def _window_key_now(self) -> Optional[tuple]:
        """(pid, window title, window frame, focused element's role/subrole) of the focused
        window, through AX only, read at most every 0.15 s; the front it was read for is
        ``_wfront`` (the exclusion's denylist judges the same app). The pid is the front read's
        (``step_capture.front_application``) only when AX answered it: this runs on the input path,
        where no run-loop tick is run, and an untickled NSWorkspace lags one switch (both of its
        reads then name the previous app). None: AX could not answer - the window is unknown and
        the exclusion withholds. Without AX at all, a constant (the secure-field half of the
        exclusion is off then)."""
        if not self._ax_available or not self.macos_context:
            return ("no-ax",)
        now = time.monotonic()
        if now - self._wkey_at < self._WINDOW_KEY_TTL_S:
            return self._wkey
        front = self._ax_front_now()
        info = None
        if front is not None:
            info = self._window_info(front["pid"])
        self._wkey = ((info["pid"], info["title"], info["frame"], info["focus"])
                      if info is not None else None)
        self._wfront = front if info is not None else None
        self._wkey_at = now
        return self._wkey

    @staticmethod
    def _ax_front_now() -> Optional[Dict[str, Any]]:
        """The front app as AX names it ({pid, app, bundle_id}), or None (no NSWorkspace guess)."""
        try:
            from anyuse.watcher.step_capture import front_application
            f = front_application(tick_s=0.0)
        except Exception:  # noqa: BLE001
            return None
        if f.get("front_source") != "ax" or f.get("pid") is None:
            return None
        return {"pid": f["pid"], "app": str(f.get("app") or ""), "bundle_id": str(f.get("bundle_id") or "")}

    def _key_for_pid(self, pid: int) -> Optional[tuple]:
        """``pid``'s focused-window key read afresh (no cache): the scan's check that its window is
        still the one it was asked for."""
        info = self._window_info(pid)
        return (info["pid"], info["title"], info["frame"], info["focus"]) if info is not None else None

    def _window_unreadable(self) -> bool:
        """Only to name a withhold whose window key AX could not read: the context reports its front
        window's title as unknown (None) - "surface_unknown" then, else the secure-field half's
        "secure_field". Both withhold; nothing here is taken as the front app."""
        try:
            active = self.macos_context.get_frontmost_app() or {}
        except Exception:  # noqa: BLE001
            return False
        return "window_title" in active and active.get("window_title") is None

    def _name_for_pid(self, front: Dict[str, Any]) -> Dict[str, Any]:
        """The AX pid's app name and bundle id when the front read could not look them up: the
        context's (NSWorkspace) front only when it names THAT pid (a lagging read naming another
        app is never taken)."""
        try:
            try:
                active = self.macos_context.get_frontmost_app(with_window=False) or {}
            except TypeError:
                active = self.macos_context.get_frontmost_app() or {}
        except Exception:  # noqa: BLE001
            active = {}
        if active.get("pid") is not None and int(active["pid"]) == int(front["pid"]):
            return dict(front, app=str(active.get("name") or ""), bundle_id=str(active.get("bundle_id") or ""))
        return front

    @staticmethod
    def _window_info(pid: Optional[int]) -> Optional[Dict[str, Any]]:
        if pid is None:
            return None
        try:
            from anyuse.context.accessibility import focused_window_info
            return focused_window_info(pid)
        except Exception:  # noqa: BLE001
            return None

    def _compute_exclusion(self) -> Optional[str]:
        """Uncached check. Returns a reason string when the current surface
        must not be captured, else None. Reasons carry NO content — they are
        safe to count/log."""
        if self._typing_suppressed():
            return "credential_typing"
        if self._ax_available and self.macos_context:
            # the denylist judges the app AX names in front, the window key's own (``_wfront``): the
            # NSWorkspace front ``get_frontmost_app`` reads lags one switch, so a switch to a denylisted
            # app would be judged as the app before it. AX not answering: withheld, no guess
            key = self._window_key_now()
            front = self._wfront
            if key is not None and front is not None:
                if not (front.get("bundle_id") or front.get("app")):
                    front = self._name_for_pid(front)
                if not (front.get("bundle_id") or front.get("app")):
                    return "surface_unknown"     # the app in front cannot be named: no guess
                why = self._denylist_reason(front["app"], front["bundle_id"], str(key[1] or ""))
                if why:
                    return why
            elif self._window_unreadable():
                return "surface_unknown"         # AX could not read the window: no guess
            # otherwise no key either way: the secure-field half below withholds (an unknown window)
        elif self.macos_context:
            try:
                active = self.macos_context.get_frontmost_app() or {}
                title = active.get("window_title")
                why = self._denylist_reason(str(active.get("name") or ""), str(active.get("bundle_id") or ""),
                                            title)
                if why:
                    return why
                if "window_title" in active and title is None and self._ax_available:
                    return "surface_unknown"     # AX could not read the window: no guess
            except Exception:
                pass
        if self._secure_field_on_screen():
            return "secure_field"
        return None

    def _exclusion_reason(self) -> Optional[str]:
        """Cached exclusion check — cheap enough for input events. The cache holds for
        ~2 s within ONE focused window: a switch to another app or window (the window key)
        recomputes at once, and an answer that only means "not scanned yet" is not kept."""
        now = time.time()
        key = self._window_key_now()
        if (key is not None and key == self._exclusion_key
                and now - self._exclusion_checked_at < self._EXCLUSION_TTL_S):
            return self._exclusion_cached
        reason = self._compute_exclusion()
        definitive = key is not None and (
            reason not in ("secure_field", "surface_unknown") or not self._ax_available
            or (self._secure_scan is not None and self._secure_scan.known(key)))
        self._exclusion_cached, self._exclusion_key = reason, key
        self._exclusion_checked_at = now if definitive else 0.0
        return reason

    async def _run_phishing_check(self) -> None:
        """Run the phishing guard against the current surface (best-effort)."""
        guard = self._phishing_guard
        if not guard:
            return
        # Password managers and credential typing remain completely excluded.
        # A login page's secure field may be inspected for deterministic phishing
        # signals, without screenshots, model calls or retaining AX text.
        reason = self._compute_exclusion()
        if reason not in (None, "secure_field"):
            return
        now = time.monotonic()
        if now - self._last_excluded_phish_check < 5.0:
            return
        self._last_excluded_phish_check = now
        try:
            url, title, app_name = "", "", ""
            if self.macos_context:
                active = self.macos_context.get_frontmost_app() or {}
                title = active.get("window_title") or ""
                app_name = active.get("name") or ""
                if app_name in ("Safari", "Google Chrome", "Firefox", "Arc",
                                "Brave Browser", "Microsoft Edge"):
                    try:
                        tabs = self.macos_context.get_browser_tabs() or []
                        if tabs:
                            url = tabs[0].get("url") or ""
                    except Exception:
                        pass
            ax = self._capture_ax_snapshot_data() or {}
            verdict = guard.check(url=url, window_title=title,
                                  ax_elements=ax.get("elements", []))

            # the structured step capture keeps no value on a flagged surface - from now on
            # (the guard), and for what it recorded there before the flag (rewritten here)
            front_app = app_name
            if verdict.suspicious:
                self._phishing_flag = {"app": front_app, "at": time.time()}
                self._withhold_flagged(front_app)
            elif self._phishing_flag and self._phishing_flag.get("app") == front_app:
                self._phishing_flag = None

            if verdict.suspicious:
                if self.config.output_level != "silent":
                    print(f"  🎣 Phishing suspicion: {verdict.host} "
                          f"({', '.join(verdict.reasons[:3])})")
                guard.alert(verdict)
        except Exception as e:  # noqa: BLE001 — advisory, never crash the loop
            if self.config.output_level == "verbose":
                print(f"  Phishing check failed: {e}")

    def _get_entity_vault(self):
        """Lazily obtain the EntityVault (best-effort, never raises).

        Returns None when unavailable; the structured recorder withholds values
        it cannot place in the vault."""
        if self._entity_vault is not None:
            return self._entity_vault
        if self._entity_vault_failed:
            return None
        try:
            from anyuse.watcher.entity_vault import get_default_vault
            self._entity_vault = get_default_vault()
        except Exception:
            self._entity_vault = None
        if self._entity_vault is None:
            self._entity_vault_failed = True
        return self._entity_vault

    def _capture_ax_snapshot_data(self) -> Optional[dict]:
        """Capture AX tree of focused window only. Much cleaner than full tree."""
        if not self._ax_available or not self.macos_context:
            return None
        try:
            from anyuse.context.accessibility import AccessibilityTree

            pid = self.macos_context.get_frontmost_pid()
            if pid is None:
                return None

            from anyuse.context.accessibility import ACTIONABLE_ROLES

            tree = AccessibilityTree(pid)
            snapshot = tree.snapshot(focused_window_only=True)

            # Only include elements that have meaningful info
            # (skip empty AXGroup containers with no title/description). The fields are
            # AXElementDesc's (it has no ``enabled`` / ``is_actionable()`` / ``focused``, and the
            # snapshot has no ``pid``).
            elements = []
            for n in snapshot.root_elements:
                label = n.ax_description
                # Skip noise: empty groups, separators, empty menu items
                if n.role in ("AXGroup", "AXSplitGroup", "AXScrollArea") and not n.title and not label:
                    continue
                if n.role == "AXMenuItem" and not n.title:
                    continue

                el = {
                    "role": n.role,
                    "title": n.title or "",
                    "actionable": (n.role or "") in ACTIONABLE_ROLES,
                }
                # Only include non-empty optional fields. Secure-field values
                # are never captured; the AX reader also removes secure values.
                if n.value and n.role != "AXSecureTextField":
                    el["value"] = str(n.value)[:100]
                if n.identifier:
                    el["identifier"] = n.identifier
                if label:
                    el["description"] = label
                if n.position:
                    el["position"] = list(n.position)
                if n.size:
                    el["size"] = list(n.size)

                elements.append(el)

            return {
                "app": snapshot.app_name,
                "pid": pid,
                "element_count": len(elements),
                "actionable_count": sum(1 for e in elements if e.get("actionable")),
                "elements": elements[:200],  # cap at 200
            }
        except Exception:
            return None

    def _save_session_data(self):
        """Save session to disk."""
        if not self._session:
            return
        
        try:
            if self._session.storage_path:
                storage_dir = Path(self._session.storage_path)
            else:
                storage_dir = SessionStorage.BASE_DIR / self._session.id
            storage_dir.mkdir(parents=True, exist_ok=True)

            session_file = storage_dir / "session.json"
            raw_store.write_json_encrypted(
                session_file, self._session.to_dict(), self._raw_crypto)

            if self.config.output_level != "silent":
                print(f"  💾 Session saved: {storage_dir}")
                
        except Exception as e:
            print(f"  ⚠️ Failed to save session: {e}")
