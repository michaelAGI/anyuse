"""Rendered screenshots at the bench's decision points (data for a later
vision-model evaluation).

At a decision point (a landing/listing page, a result page, an item page, the
final page of an agent attempt, a tier-2 replay page, and a page the block
detector reads as a login wall, a cookie wall or an error page) the open
container page is captured with CDP ``Page.captureScreenshot`` (JPEG q70, the
1280x900 viewport, never beyond it) and saved next to the page's HTML and the
rule's decision as JSON.

Never captured: a page the block detector reads as a challenge, CAPTCHA,
403/429/forbidden, rate limit or unknown block (checked BEFORE the capture, so
the image is never taken), a page under a live-solve quarantine, and anything
while no benchmark directory is set. A screenshot is a local CDP read of the
container page: no request, nothing against a site's traffic budget.

HTML and decision text pass through ``web_scope.redact_keys`` (no provider key
is stored outside the tool record).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

#: set by the bench per site: {"dir": <raw>/shots, "sid": <site id>}
CURRENT: Dict[str, str] = {}
#: kind -> saved; "skipped_challenge" / "skipped_error" counts
COUNTS: Dict[str, int] = {}
KINDS = ("landing", "result", "item", "attempt_final", "replay", "login", "consent", "error")
_SEQ = {"n": 0}
#: detector reasons that are NEVER captured
NEVER = ("captcha", "challenge", "rate_limited", "forbidden", "unknown_block", "HTTP 403",
         "HTTP 429")


def _bump(k: str) -> None:
    COUNTS[k] = COUNTS.get(k, 0) + 1


def classify(text: str, url: str, network: List[dict]) -> str:
    """'' (an ordinary page), 'never' (challenge-like: not captured), or the
    detector's kind for a login wall / cookie wall / error page."""
    from anyuse.execution.blocked import (COOKIE_WALL, ERROR_PAGE, LOGIN_WALL,
                                          detect_blocked_ui)
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    for r in network or []:
        if r.get("resource_type") == "Document" and \
                urllib.parse.urlsplit(r.get("url") or "").netloc.lower() == host and \
                int(r.get("status") or 0) in (403, 429):
            return "never"
    try:
        state = detect_blocked_ui(text or "", "", url or "", network or [])
    except Exception:                      # noqa: BLE001 - unsure = never captured
        return "never"
    if not state.blocked:
        return ""
    if state.reason == LOGIN_WALL:
        return "login"
    if state.reason == COOKIE_WALL:
        return "consent"
    if state.reason == ERROR_PAGE:
        return "error"
    return "never"


def item_like(url: str, value: str) -> bool:
    """The item-landing reading: the value is in the URL PATH, not the query."""
    if not value:
        return False
    path = urllib.parse.unquote(urllib.parse.urlsplit(url or "").path).lower()
    v = value.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", v).strip("-")
    return bool(v and (v in path or (slug and slug in path)))


async def shoot(page, kind: str, decision: Dict[str, Any], value: str = "") -> str:
    """Capture the open page if allowed; returns the saved path stem or ''."""
    if not CURRENT.get("dir"):
        return ""
    try:
        text, url = await page.text(), await page.url()
        net = await page.network(want_bodies=False)
    except Exception:                      # noqa: BLE001 - a shot never breaks a run
        return ""
    cls = classify(text or "", url or "", net)
    if cls == "never":
        _bump("skipped_challenge_or_block")
        return ""
    if cls:
        kind = cls
    elif kind == "result" and item_like(url, value):
        kind = "item"
    try:
        from anyuse.execution.live_solve import QUARANTINE
        if QUARANTINE.active(getattr(page, "_target", "")):
            _bump("skipped_challenge_or_block")
            return ""
    except Exception:                      # noqa: BLE001
        pass
    try:
        res = await page.call("Page.captureScreenshot",
                              {"format": "jpeg", "quality": 70, "captureBeyondViewport": False,
                               "clip": {"x": 0, "y": 0, "width": 1280, "height": 900,
                                        "scale": 1}})
        data = base64.b64decode(res.get("data") or "")
        dom = await page.dom()
    except Exception as exc:               # noqa: BLE001
        _bump("capture_failed")
        return f"failed: {type(exc).__name__}"
    if not data:
        _bump("capture_failed")
        return ""
    from anyuse.execution.web_scope import redact_keys, redact_obj
    d = os.path.join(CURRENT["dir"], CURRENT.get("sid", "site"))
    os.makedirs(d, exist_ok=True)
    _SEQ["n"] += 1
    stem = os.path.join(d, f"{_SEQ['n']:04d}_{kind}")
    with open(stem + ".jpg", "wb") as fh:
        fh.write(data)
    with open(stem + ".html", "w", encoding="utf-8") as fh:
        fh.write(redact_keys(dom or ""))
    with open(stem + ".json", "w", encoding="utf-8") as fh:
        json.dump(redact_obj({"kind": kind, "url": url, "decision": decision,
                              "detector": cls or "ordinary page",
                              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "format": "jpeg q70, 1280x900 viewport"}), fh, indent=1,
                  default=str)
    _bump(kind)
    return stem
