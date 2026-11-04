"""Tests for the phishing guard (watcher/phishing_guard.py).

Covers:
  - URL heuristics: userinfo trick, raw-IP host, punycode, suspicious TLD,
    brand-in-foreign-host (paypal.com.evil.icu), lookalike domain (paypa1.com)
  - legitimate URLs stay clean (paypal.com, accounts.google.com, subdomains)
  - structure: credential form + title-brand/domain mismatch; link text vs
    target host mismatch
  - subthreshold suspicion stays advisory without model assistance
  - alert cooldown per host; alert payload shape (kind=phishing_alert)
  - BatchWatcher integration: toggle off ⇒ no guard constructed; injected
    guard sees the recorded surface context; suspicious verdict fires alert exactly once
  - guard never raises on garbage input
  - scheduled advisory task shutdown: failure, child cancellation and caller
    cancellation all release the step tap and persist the encrypted session

Self-executing (pytest is not installed in this env).

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_phishing_guard.py
"""

import asyncio
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.watcher.phishing_guard import (
    PhishingGuard, PhishingVerdict, _levenshtein_leq1, _registrable_domain,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _guard():
    g = PhishingGuard(notify=False)
    # Silence the network side for unit tests; capture payloads instead.
    g._sent = []

    def _fake_post(v):
        g._sent.append(v)
        return {"ok": True, "status": 200}

    return g


def test_helpers():
    assert _registrable_domain("www.paypal.com") == "paypal.com"
    assert _registrable_domain("a.b.evil.co.uk") == "evil.co.uk"
    assert _levenshtein_leq1("paypa1", "paypal")
    assert _levenshtein_leq1("paypall", "paypal")
    assert not _levenshtein_leq1("paypa11", "paypal")
    print("OK  test_helpers")


def test_url_heuristics_flag_attacks():
    g = _guard()
    cases = {
        "https://paypal.com@evil.io/login": "userinfo_in_url",
        "http://192.168.4.20/verify": "raw_ip_host",
        "https://xn--pypal-4ve.com/signin": "punycode_host",
        "https://paypal.com.secure-verify.icu/login": "brand_in_foreign_host:paypal",
        "https://login-paypal.evil.io/": "brand_in_foreign_host:paypal",
        "https://paypa1.com/signin": "lookalike_domain:paypal",
        "http://sparkasse-kunden.top/login": "brand_in_foreign_host:sparkasse",
    }
    for url, expected_reason in cases.items():
        v = g.check(url=url)
        assert any(r.startswith(expected_reason.split(":")[0]) for r in v.reasons), \
            f"{url}: expected {expected_reason}, got {v.reasons}"
        assert v.score > 0, url
    # The strongest ones cross the threshold on their own.
    assert g.check(url="https://paypal.com@evil.io/login").suspicious
    assert g.check(url="https://paypal.com.secure-verify.icu/login").suspicious
    print("OK  test_url_heuristics_flag_attacks")


def test_legit_urls_stay_clean():
    g = _guard()
    for url in (
        "https://www.paypal.com/signin",
        "https://accounts.google.com/v3/signin",
        "https://github.com/login",
        "https://www.amazon.de/ap/signin",
        "https://appleid.apple.com/",
        "https://en.wikipedia.org/wiki/Phishing",
        "",
        "not a url at all",
    ):
        v = g.check(url=url)
        assert not v.suspicious, f"{url} misflagged: {v.reasons}"
    print("OK  test_legit_urls_stay_clean")


def test_structure_title_brand_mismatch():
    g = _guard()
    ax = [{"role": "AXSecureTextField", "title": "Password"}]
    # Benign URL alone + brandy title + credential form ⇒ strong signal.
    v = g.check(url="https://kundenservice-check.icu/de",
                window_title="PayPal — verify your account",
                ax_elements=ax)
    assert any(r.startswith("title_brand_domain_mismatch") for r in v.reasons), v.reasons
    assert v.suspicious, (v.score, v.reasons)

    # Same title on the REAL domain ⇒ clean.
    v2 = g.check(url="https://www.paypal.com/signin",
                 window_title="PayPal — log in", ax_elements=ax)
    assert not v2.suspicious, v2.reasons
    print("OK  test_structure_title_brand_mismatch")


def test_link_text_target_mismatch():
    g = _guard()
    ax = [{"role": "AXLink", "title": "https://www.dhl.de/track",
           "url": "https://dhl-paket-service.icu/t/x1"}]
    v = g.check(url="", window_title="Ihre Sendung — Mail", ax_elements=ax)
    assert any(r.startswith("link_text_target_mismatch") for r in v.reasons), v.reasons
    assert v.suspicious
    print("OK  test_link_text_target_mismatch")


def test_subthreshold_suspicion():
    g = _guard()
    # Suspicious TLD (0.25) + credential form (0.2) stays below the threshold.
    ax = [{"role": "AXSecureTextField", "title": "Passwort"}]
    v = g.check(url="https://secure-check.icu/login", window_title="Anmelden",
                ax_elements=ax)
    assert not v.suspicious
    assert abs(v.score - 0.45) < 1e-9, (v.score, v.reasons)
    assert v.reasons == ["suspicious_tld:icu", "credential_form_on_suspicious_url"]
    print("OK  test_subthreshold_suspicion")


def test_alert_cooldown_and_payload():
    import json
    import urllib.request

    g = PhishingGuard(notify=False, alert_url="http://127.0.0.1:9/alerts")
    posted = []

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def getcode(self): return 200

    def fake_urlopen(req, timeout=0):
        posted.append(json.loads(req.data.decode()))
        return _Resp()

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        v = g.check(url="https://paypal.com.evil.icu/login")
        assert v.suspicious
        r1 = g.alert(v)
        assert r1["ok"] is True
        r2 = g.alert(v)  # same host, inside cooldown
        assert r2.get("skipped") is True
        assert len(posted) == 1
        payload = posted[0]
        assert payload["kind"] == "phishing_alert"
        assert payload["source"] == "anyuse.watcher.phishing_guard"
        assert payload["entities"][0]["host"] == "paypal.com.evil.icu"
        assert g.alerts == 1
    finally:
        urllib.request.urlopen = real
    print("OK  test_alert_cooldown_and_payload")


def test_watcher_integration_toggle_and_alert():
    from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig

    class _Ctx:
        def get_frontmost_app(self):
            return {"name": "Safari",
                    "window_title": "PayPal — verify your account"}
        def get_browser_tabs(self):
            return [{"url": "https://paypal.com.secure-verify.icu/login",
                     "title": "PayPal"}]

    cfg_off = BatchWatcherConfig(output_level="silent")
    assert cfg_off.phishing_guard is False
    w_off = BatchWatcher(config=cfg_off)
    assert w_off._phishing_guard is None

    # Toggle ON via injection: the recorder runs the check and fires ONE alert.
    g = PhishingGuard(notify=False)
    fired = []
    g.alert = lambda v: fired.append(v) or {"ok": True}

    w = BatchWatcher(config=BatchWatcherConfig(output_level="silent"),
                     phishing_guard=g)
    w._ax_available = False
    w.macos_context = _Ctx()
    w._compute_exclusion = lambda: None  # isolate the advisory check from finance exclusions
    from anyuse.watcher.models import WatchSession
    w._session = WatchSession.create_new()

    _run(w._run_phishing_check())
    assert len(fired) == 1, "suspicious surface must alert exactly once"
    assert fired[0].host == "paypal.com.secure-verify.icu"
    assert g.checks == 1
    print("OK  test_watcher_integration_toggle_and_alert")


def test_guard_never_raises_on_garbage():
    g = _guard()
    for bad in (None, 123, "::::", "http://", {"x": 1}):
        try:
            v = g.check(url=bad if isinstance(bad, str) else "",
                        window_title=str(bad),
                        ax_elements=[bad] if not isinstance(bad, list) else bad)
            assert isinstance(v, PhishingVerdict)
        except Exception as e:
            raise AssertionError(f"guard raised on {bad!r}: {e}")
    print("OK  test_guard_never_raises_on_garbage")


def test_watcher_shutdown_after_advisory_failure_or_cancellation():
    import os
    from datetime import datetime
    from unittest.mock import patch
    from cryptography.fernet import Fernet
    from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig
    from anyuse.watcher.models import TagEvent, TagType
    from anyuse.watcher.raw_store import MAGIC, RawArtifactCrypto
    from anyuse.watcher.storage import SessionStorage

    async def scenario(mode, base):
        crypto = RawArtifactCrypto(key=Fernet.generate_key())
        entered, cancelling = asyncio.Event(), asyncio.Event()

        class Capture:
            stopped = 0

            def start(self):
                return None

            def stop(self):
                self.stopped += 1
                # The tap can flush its last event on stop: it must be saved too.
                watcher._on_structured_event(TagEvent(
                    id="flushed", timestamp=datetime.now(),
                    tag_type=TagType.STEP, target="last step", data={"action": "click"}))

        capture = Capture()
        watcher = BatchWatcher(config=BatchWatcherConfig(output_level="silent"),
                               phishing_guard=PhishingGuard(notify=False),
                               raw_crypto=crypto, step_capture_factory=lambda _: capture)
        watcher.macos_context, watcher._ax_available = None, False

        async def check():
            entered.set()
            if mode == "failed":
                raise RuntimeError("advisory task crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                if mode == "cancel_stop":
                    # Hold shutdown until its caller is cancelled as well.
                    await asyncio.Event().wait()
                raise

        watcher._run_phishing_check = check
        await watcher.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        task = watcher._phishing_task
        if mode == "failed":
            assert task.done() and isinstance(task.exception(), RuntimeError)
        completed = []
        watcher.on_session_complete = completed.append
        stop = asyncio.create_task(watcher.stop())
        if mode == "cancel_stop":
            await asyncio.wait_for(cancelling.wait(), timeout=1)
            stop.cancel()
            try:
                await asyncio.wait_for(stop, timeout=1)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("stop() must propagate its caller's cancellation")
        else:
            assert await asyncio.wait_for(stop, timeout=1) is watcher._session
            assert completed == [watcher._session]
        assert capture.stopped == 1 and watcher._step_capture is None
        assert task.done() and watcher._phishing_task is None and not watcher._running
        if mode != "failed":
            assert task.cancelled()
        stored = SessionStorage(base_dir=base, crypto=crypto).load(watcher._session.id)
        assert stored is not None and stored.status == "stopped" and stored.ended_at
        assert len(stored.step_events) == 1 and stored.step_events[0].target == "last step"
        assert (base / stored.id / "session.json").read_bytes().startswith(MAGIC)

    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "ANYUSE_WATCH_STRUCTURED_STEPS": "1"}):
        for mode in ("failed", "cancel_advisory", "cancel_stop"):
            base = pathlib.Path(td) / mode
            with patch.object(SessionStorage, "BASE_DIR", base):
                asyncio.run(scenario(mode, base))
    print("OK  test_watcher_shutdown_after_advisory_failure_or_cancellation")


def main():
    print("=" * 64)
    print("phishing guard — tests")
    print("=" * 64)
    test_helpers()
    test_url_heuristics_flag_attacks()
    test_legit_urls_stay_clean()
    test_structure_title_brand_mismatch()
    test_link_text_target_mismatch()
    test_subthreshold_suspicion()
    test_alert_cooldown_and_payload()
    test_watcher_integration_toggle_and_alert()
    test_guard_never_raises_on_garbage()
    test_watcher_shutdown_after_advisory_failure_or_cancellation()
    print("\nALL PHISHING GUARD TESTS PASSED")


if __name__ == "__main__":
    main()
