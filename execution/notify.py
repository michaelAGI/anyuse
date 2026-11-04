"""Tell the user a card is waiting, when no MCP client can ask them.

The inbox (docs/HUMAN_INBOX.md) asks through MCP elicitation first. When no
client with elicitation is attached — a scheduled run, a client without the
capability — the card waits silently until someone runs ``anyuse inbox``. This
module sends ONE short notice through the channel the user configured:

* ``none`` (default): nothing is sent.
* ``imessage``: **outbound only.** An AppleScript for Messages.app is built as
  a constant STRING and handed to an injected runner. The recipient and the
  text travel as AppleScript arguments (``on run argv``), never interpolated
  into the script, so no card text can become AppleScript. The default runner
  pipes the script to ``/usr/bin/osascript -`` — tests only ever use a fake.
  Reading replies would need Full Disk Access to ``chat.db``; that is NOT
  built (a later opt-in, docs/NOTIFY.md).
* ``webhook``: POST a small JSON body to the user's URL (https, or http to a
  loopback host), no redirects followed, no proxies.

What a notice contains: the card id, what kind of help is needed, and how to
answer (the MCP client, or ``anyuse inbox do <id>``). Never a host, an
instruction, a value, an error text or a secret. A card is notified at most
once (de-duplicated by id, persisted), and notices are rate-limited.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

CHANNEL_ENV = "ANYUSE_NOTIFY_CHANNEL"
IMESSAGE_TO_ENV = "ANYUSE_NOTIFY_IMESSAGE_TO"
WEBHOOK_URL_ENV = "ANYUSE_NOTIFY_WEBHOOK_URL"
CHANNELS = ("none", "imessage", "webhook")

#: at least this many seconds between two notices
MIN_INTERVAL_S = 60.0
#: and at most this many in any rolling hour
MAX_PER_HOUR = 10
#: sent card ids are remembered this long (de-duplication)
DEDUP_TTL_S = 14 * 86400

OSASCRIPT = "/usr/bin/osascript"

#: The whole script. It is a constant: recipient and text arrive as argv.
IMESSAGE_SCRIPT = """on run argv
  set theRecipient to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    set theService to 1st account whose service type = iMessage
    set theBuddy to participant theRecipient of theService
    send theText to theBuddy
  end tell
end run
"""

_NEED = {
    "approval": "your approval of a step",
    "value": "a value only you know",
    "login_or_2fa": "you to sign in",
    "challenge": "you to deal with a site's human check",
    "do_step": "you to do one step yourself",
    "consent": "your cookie choice",
    "reconcile": "you to say whether a step happened",
}
_ID = re.compile(r"^[0-9a-f]{12}$")


def message_for(card: Mapping[str, Any], more_waiting: int = 0) -> str:
    """Short and value-free: id, kind of help, how to answer."""
    cid = str(card.get("id") or card.get("card") or "")
    if not _ID.match(cid):
        raise ValueError("malformed card id")
    need = _NEED.get(str(card.get("kind") or ""), "your help")
    if card.get("how") == "solve_live":
        # a run is waiting for the person to solve a check live; the private
        # view link is never in a notice (anyuse inbox live shows it on a TTY)
        text = (f"AnyUse: card {cid}: a run is waiting for you to solve a human check "
                f"yourself. Run: anyuse inbox live {cid}")
    else:
        text = (f"AnyUse: card {cid} needs {need}. Answer in your MCP client, "
                f"or run: anyuse inbox do {cid}")
    if more_waiting > 0:
        text += f" ({more_waiting} more waiting: anyuse inbox)"
    return text


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

def default_osascript_runner(argv: List[str], stdin: str) -> int:
    """The real runner: ``/usr/bin/osascript - <recipient> <text>`` with the
    script on stdin. Never called by the tests (they inject a fake)."""
    import subprocess
    proc = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                          timeout=20, check=False)
    return proc.returncode


def imessage_command(recipient: str, text: str) -> Dict[str, Any]:
    """``{"argv": [...], "stdin": script}``. The script is constant; the
    recipient and text are separate argv items after ``-``."""
    return {"argv": [OSASCRIPT, "-", recipient, text], "stdin": IMESSAGE_SCRIPT}


def default_webhook_poster(url: str, body: Dict[str, Any], timeout: float = 8.0) -> int:
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):          # never follow
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "anyuse-notify/1"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _setting(name: str, explicit: Any = None) -> Any:
    from anyuse import settings as _settings
    try:
        return _settings.get(name, explicit) if explicit is not None else _settings.get(name)
    except Exception:
        return _settings.SETTINGS[name].default


def _valid_url(url: str) -> str:
    from anyuse.settings import _webhook_url
    return _webhook_url(url, False)


def _valid_recipient(to: str) -> str:
    from anyuse.settings import _imessage_to
    return _imessage_to(to, False)


# ---------------------------------------------------------------------------
# the notifier
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, channel: Optional[str] = None, *, imessage_to: Optional[str] = None,
                 webhook_url: Optional[str] = None,
                 runner: Optional[Callable[[List[str], str], int]] = None,
                 poster: Optional[Callable[[str, Dict[str, Any]], int]] = None,
                 state_path: Optional[str] = None, clock: Callable[[], float] = time.time,
                 min_interval_s: float = MIN_INTERVAL_S, max_per_hour: int = MAX_PER_HOUR):
        ch = str(channel if channel is not None else _setting(CHANNEL_ENV) or "none")
        self.channel = ch.strip().lower() if ch.strip().lower() in CHANNELS else "none"
        self.imessage_to = _valid_recipient(
            imessage_to if imessage_to is not None else str(_setting(IMESSAGE_TO_ENV) or ""))
        self.webhook_url = _valid_url(
            webhook_url if webhook_url is not None else str(_setting(WEBHOOK_URL_ENV) or ""))
        self.runner = runner or default_osascript_runner
        self.poster = poster or default_webhook_poster
        self.state_path = state_path
        self.clock = clock
        self.min_interval_s, self.max_per_hour = float(min_interval_s), int(max_per_hour)

    # -- persisted de-dup / rate-limit state ---------------------------------

    def _path(self) -> str:
        if self.state_path:
            return self.state_path
        from anyuse.paths import anyuse_data_path
        return str(anyuse_data_path("notify_state.json"))

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self._path(), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {"sent": dict(data.get("sent") or {}),
                        "times": [float(t) for t in data.get("times") or []],
                        "suppressed": list(data.get("suppressed") or [])}
        except (OSError, ValueError, TypeError):
            pass
        return {"sent": {}, "times": [], "suppressed": []}

    def _save(self, state: Mapping[str, Any]) -> None:
        path = self._path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-notify-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- the one entry point ---------------------------------------------------

    def configured(self) -> bool:
        return ((self.channel == "imessage" and bool(self.imessage_to))
                or (self.channel == "webhook" and bool(self.webhook_url)))

    def notify_card(self, card: Mapping[str, Any], *,
                    elicitation_attached: bool = False) -> Dict[str, Any]:
        """``{"sent": bool, "channel": ..., "reason": ...}``. Never raises."""
        out = {"sent": False, "channel": self.channel}
        if elicitation_attached:
            return {**out, "reason": "an MCP client with elicitation asks the person"}
        if self.channel == "none":
            return {**out, "reason": "no notify channel configured"}
        if not self.configured():
            return {**out, "reason": f"{self.channel} has no valid target configured"}
        cid = str(card.get("id") or card.get("card") or "")
        if not _ID.match(cid):
            return {**out, "reason": "malformed card id"}
        now = self.clock()
        state = self._load()
        state["sent"] = {k: v for k, v in state["sent"].items()
                         if now - float(v) < DEDUP_TTL_S}
        state["times"] = [t for t in state["times"] if now - t < 3600.0]
        if cid in state["sent"]:
            return {**out, "reason": "already notified for this card"}
        if (state["times"] and now - max(state["times"]) < self.min_interval_s) \
                or len(state["times"]) >= self.max_per_hour:
            if cid not in state["suppressed"]:
                state["suppressed"] = (state["suppressed"] + [cid])[-50:]
            self._save(state)
            return {**out, "reason": "rate-limited; the next notice will mention it"}
        more = len([c for c in state["suppressed"] if c != cid])
        try:
            text = message_for(card, more_waiting=more)
            ok = self._send(cid, str(card.get("kind") or ""), text)
        except Exception as exc:          # a notice is never worth failing a run
            logger.warning("notify: %s channel failed: %s", self.channel, type(exc).__name__)
            return {**out, "reason": f"send failed ({type(exc).__name__})"}
        if not ok:
            return {**out, "reason": "send failed"}
        state["sent"][cid] = now
        state["times"].append(now)
        state["suppressed"] = []
        self._save(state)
        return {**out, "sent": True, "reason": "sent"}

    def _send(self, card_id: str, kind: str, text: str) -> bool:
        if self.channel == "imessage":
            cmd = imessage_command(self.imessage_to, text)
            return self.runner(cmd["argv"], cmd["stdin"]) == 0
        if self.channel == "webhook":
            verb = "live" if "anyuse inbox live" in text else "do"
            body = {"schema": "anyuse.notice/1", "card": card_id, "kind": kind,
                    "text": text, "answer_with": f"anyuse inbox {verb} {card_id}"}
            status = self.poster(self.webhook_url, body)
            return 200 <= int(status) < 300
        return False


def notify_card(card: Mapping[str, Any], *, elicitation_attached: bool = False,
                notifier: Optional[Notifier] = None) -> Dict[str, Any]:
    """Convenience for the surfaces. Never raises."""
    try:
        return (notifier or Notifier()).notify_card(
            card, elicitation_attached=elicitation_attached)
    except Exception as exc:
        return {"sent": False, "channel": "", "reason": f"notify error ({type(exc).__name__})"}
