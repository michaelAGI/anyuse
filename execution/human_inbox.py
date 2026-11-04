"""Asking the human: MCP elicitation first, the durable inbox as the fallback.

When a run needs a person (approval, value, login_or_2fa, challenge, do_step,
reconcile), the engine posts a **card** (``Engine(inbox=...)``) for audit and
then:

1. **Elicitation (primary).** If the connected MCP client advertises the
   elicitation capability, AnyUse sends ONE form with a clear message and a
   typed schema. The client shows it to the user like Claude Code shows its own
   permission prompts. An ``accept`` answer is applied with the same checks as
   every other human input (pending step, inputs digest, preconditions, the
   approval/reconcile digest) and the run resumes with the fresh checks. No
   typed-back code: the client's own UI is the trusted surface.
2. **Inbox (fallback).** No client attached (scheduled or CLI runs), a client
   without elicitation, or the user declined/cancelled: the card stays pending.
   ``anyuse inbox`` lists it, and ``anyuse inbox do <id>`` completes it behind
   the terminal gate (TTYs, agent-marker refusal, ``/dev/tty``, typed-back
   code).

**The line: AnyUse never solves a CAPTCHA, never automates a
challenge, never evades bot detection, and has no stealth mode.** After a
person dealt with a block:

* third-party challenge: ``ANYUSE_CHALLENGE_POLICY`` (new setting, env var
  until the settings registry merge). ``human_does_step`` (default, and any
  unrecognised value): the person does the step and hands back its result;
  the session they cleared is not reused and the host is human-only for the
  run. ``continue_in_own_session``: continue in the attached session, only for
  hosts on the own-sites list (per-host override with a mandatory reason).
  ``human_solves_live``: the PERSON solves it live (their attached browser, or
  a loopback live view relaying only their pointer and keys; no model sees
  it), then this task continues; only for run origins listed in
  ``ANYUSE_LIVE_SOLVE_ORIGINS``, never ``bench`` (``execution/live_solve.py``);
* ``login_or_2fa`` on a host in the user's own-accounts list
  (``<data>/accounts.json``, entries the user created): continues in the same
  attached session by default. Any other login: the person does the step.

Either continuation also needs a runner that can share the person's attached
browser session; the compiled HTTP rung cannot, so its steps fall back.

A value typed for a ``value`` card is Fernet-encrypted with a key held in the
vault's key custody (``watcher/entity_vault.load_or_create_named_key``,
Keychain or the 0600 key file) while its run waits, and deleted when the run
ends. It never appears in a receipt, a card, a log or a tool result.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from anyuse.execution import blocked as _blocked
from anyuse.execution.routine import (
    HUMAN_CHANNEL_CLI, HUMAN_CHANNEL_ELICIT, HUMAN_CHANNEL_INBOX, inputs_digest,
    is_secret_ref,
)
from anyuse.execution.skill_manifest import canonical

KIND_APPROVAL = "approval"
KIND_VALUE = "value"
KIND_LOGIN = "login_or_2fa"
KIND_CHALLENGE = "challenge"
KIND_DO_STEP = "do_step"
KIND_CONSENT = "consent"
KIND_RECONCILE = "reconcile"
KINDS = (KIND_APPROVAL, KIND_VALUE, KIND_LOGIN, KIND_CHALLENGE, KIND_DO_STEP,
         KIND_CONSENT, KIND_RECONCILE)

#: Registered in settings.py (``anyuse settings``); env var > settings file > default.
POLICY_ENV = "ANYUSE_CHALLENGE_POLICY"
HUMAN_DOES_STEP = "human_does_step"
CONTINUE_IN_OWN_SESSION = "continue_in_own_session"
#: The person solves the challenge live (their attached browser, or the
#: loopback live view for a headless run); AnyUse only relays their pointer and
#: keys and never decides anything on the page (execution/live_solve.py).
HUMAN_SOLVES_LIVE = "human_solves_live"
POLICIES = (HUMAN_DOES_STEP, CONTINUE_IN_OWN_SESSION, HUMAN_SOLVES_LIVE)

#: Who started a run. Stored in the run's metadata (hash-covered). A run
#: without an origin is none of these and never gets ``human_solves_live``.
ORIGIN_USER_TASK = "user_task"
ORIGIN_SCHEDULED = "scheduled"
ORIGIN_BENCH = "bench"
ORIGIN_BULK = "bulk"
ORIGINS = (ORIGIN_USER_TASK, ORIGIN_SCHEDULED, ORIGIN_BENCH, ORIGIN_BULK)
LIVE_ORIGINS_ENV = "ANYUSE_LIVE_SOLVE_ORIGINS"
LIVE_TIMEOUT_ENV = "ANYUSE_LIVE_SOLVE_TIMEOUT_S"


class LiveSolveRefused(RuntimeError):
    """``human_solves_live`` asked for where it can never apply (a bench run)."""

#: Block reasons that are a third party's anti-bot check.
CHALLENGE_REASONS = frozenset({
    _blocked.CAPTCHA_RECAPTCHA, _blocked.CAPTCHA_HCAPTCHA, _blocked.CAPTCHA_TURNSTILE,
    _blocked.CAPTCHA_GENERIC, _blocked.CHALLENGE_INTERSTITIAL, _blocked.FORBIDDEN,
})

HOW_APPROVE = "approve"
HOW_HUMAN_STEP = "human_step"
HOW_AGENT_CHOICE = "agent_choice"
HOW_HUMAN_DOES_STEP = "human_does_step"
HOW_CONTINUE = "continue_session"
HOW_RECONCILE = "reconcile"
HOW_STANDALONE = "standalone"        # a direct compiled-tool call; no run to resume
HOW_SOLVE_LIVE = "solve_live"        # the person solves a challenge live (live_solve.py)
LIVE_URL_REF = "live_view_url"

VALUE_KEY_NAME = "k_inbox"
OPEN = ("pending", "skipped")
MCP_LIST_TOOL_NAME = "anyuse_inbox_list"
MCP_LIST_TOOL = {
    "name": MCP_LIST_TOOL_NAME,
    "description": ("List AnyUse inbox cards: things only a person can do (approve, "
                    "enter a value, sign in, pass a site's human check, do a step, say "
                    "whether an uncertain write happened). Read-only. AnyUse asks the "
                    "person itself (elicitation, or `anyuse inbox do <id>` in their own "
                    "terminal); no tool call can answer a card."),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
HOW_TO_COMPLETE = ("AnyUse asks the person through the MCP client's elicitation prompt "
                   "when it can; otherwise the person runs `anyuse inbox do <id>` in their "
                   "own terminal. No tool call answers a card.")


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

def _configured_policy(explicit: Optional[str]) -> str:
    if explicit:
        raw = explicit.strip().lower()
        return raw if raw in POLICIES else HUMAN_DOES_STEP
    try:
        from anyuse import settings as _settings
        raw = str(_settings.get(POLICY_ENV) or "").strip().lower()
    except Exception:                      # registry unavailable or invalid: safe default
        raw = (os.environ.get(POLICY_ENV) or "").strip().lower()
    return raw if raw in POLICIES else HUMAN_DOES_STEP


def live_solve_origins(explicit: Optional[str] = None) -> Tuple[str, ...]:
    """The origins ``human_solves_live`` applies to; never ``bench``."""
    try:
        from anyuse import settings as _settings
        raw = str(_settings.get(LIVE_ORIGINS_ENV, explicit) or "")
    except Exception:
        raw = ORIGIN_USER_TASK
    out = tuple(o for o in (p.strip().lower() for p in raw.split(","))
                if o in (ORIGIN_USER_TASK, ORIGIN_SCHEDULED, ORIGIN_BULK))
    return out or (ORIGIN_USER_TASK,)


def live_solve_timeout_s(explicit: Any = None) -> int:
    try:
        from anyuse import settings as _settings
        return int(_settings.get(LIVE_TIMEOUT_ENV, explicit))
    except Exception:
        return 900


def refuse_bench_live_solve(origin: Optional[str], explicit: Optional[str] = None) -> None:
    """Benchmarks refuse ``human_solves_live`` outright, whatever the origins setting."""
    if origin == ORIGIN_BENCH and _configured_policy(explicit) == HUMAN_SOLVES_LIVE:
        raise LiveSolveRefused(
            f"{POLICY_ENV}={HUMAN_SOLVES_LIVE} is refused for bench runs: published numbers "
            f"must not depend on a person solving a challenge; unset it for benchmarks")


def challenge_policy(explicit: Optional[str] = None, *, origin: Optional[str] = None,
                     origins: Optional[str] = None) -> str:
    """The effective policy for a run of ``origin``.

    ``human_does_step`` unless another value is set exactly. ``human_solves_live``
    applies only to a run whose origin is listed in ``ANYUSE_LIVE_SOLVE_ORIGINS``
    (default ``user_task``); every other run (no origin, bench, an origin not
    listed) falls back to ``human_does_step``. Use :func:`refuse_bench_live_solve`
    where a bench run must refuse outright.
    """
    raw = _configured_policy(explicit)
    if raw == HUMAN_SOLVES_LIVE:
        if origin == ORIGIN_BENCH or origin not in live_solve_origins(origins):
            return HUMAN_DOES_STEP
    return raw


def kind_for_block(reason: str) -> str:
    if reason == _blocked.LOGIN_WALL:
        return KIND_LOGIN
    if reason in CHALLENGE_REASONS:
        return KIND_CHALLENGE
    return KIND_DO_STEP     # rate limit, consent wall, error page, unknown


def accounts_file() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("accounts.json"))


def own_account(host: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The user's own account for ``host`` from the account registry file
    (``credentials/accounts.py`` format), or ``None``. Only entries the user
    created count; agent-created accounts are not "your own account"."""
    host = (host or "").lower()
    if not host:
        return None
    try:
        with open(path or accounts_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    for acc in (data.get("accounts") or []) if isinstance(data, dict) else []:
        if not isinstance(acc, dict) or str(acc.get("created_by") or "user") != "user":
            continue
        hosts = {str(acc.get("service") or "").lower(),
                 (urllib.parse.urlsplit(str(acc.get("url") or "")).netloc or "").lower()}
        hosts |= {h[4:] for h in hosts if h.startswith("www.")}
        hosts.discard("")
        if host in hosts or (host.startswith("www.") and host[4:] in hosts):
            return {"service": acc.get("service"), "name": acc.get("name")}
    return None


def continuation(kind: str, host: str, *, policy: Optional[str] = None,
                 hosts_path: Optional[str] = None, accounts_path: Optional[str] = None,
                 runner_shares_session: bool = False) -> Tuple[bool, str]:
    """May the run continue in the person's session after they finish?"""
    if kind not in (KIND_CHALLENGE, KIND_LOGIN):
        return False, "only a challenge or a login is ever continued in a session"
    if not runner_shares_session:
        return False, ("this step runs through a client that cannot share your browser "
                       "session, so you do this step yourself")
    if kind == KIND_LOGIN:
        acc = own_account(host, accounts_path)
        if acc:
            return True, (f"{host} is in your own-accounts list "
                          f"({str(acc.get('service') or '')[:60]}); the run continues in "
                          f"the session you sign in to")
        return False, (f"{host or 'this host'} is not in your own-accounts list, so you "
                       f"do this step yourself")
    if challenge_policy(policy) != CONTINUE_IN_OWN_SESSION:
        return False, (f"{POLICY_ENV}={HUMAN_DOES_STEP}: you do this step yourself and "
                       f"hand back the result; AnyUse does not reuse your session")
    from anyuse.execution.api_permission import host_override
    override = host_override(host, hosts_path) if host else None
    if override:
        return True, (f"{host} is on your own-sites list "
                      f"(reason: {str(override.get('reason') or '')[:120]})")
    return False, (f"{host or 'this host'} is not on your own-sites list (per-host "
                   f"override with a reason), so you do this step yourself")


def inbox_dir() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("inbox"))


def _plain(text: Any, limit: int = 300) -> str:
    from anyuse.execution.skill_gen import plain
    return plain(text, limit)


def _sha(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def vault_value_key() -> bytes:
    """The inbox's Fernet key, under the vault's key custody. Fails closed if
    the custody chain does not actually keep it (a keyring that drops writes
    would otherwise make a waiting value undecryptable)."""
    from anyuse.watcher import entity_vault as ev
    key = ev.load_or_create_named_key(VALUE_KEY_NAME)
    if ev.get_named_secret(VALUE_KEY_NAME) != key.decode():
        raise RuntimeError("the vault key store did not keep the inbox key")
    return key


# ---------------------------------------------------------------------------
# the inbox
# ---------------------------------------------------------------------------

class Inbox:
    """Durable cards + the encrypted values of ``value`` cards."""

    def __init__(self, base_dir: Optional[str] = None, *, hosts_path: Optional[str] = None,
                 accounts_path: Optional[str] = None, policy: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 key_provider: Optional[Callable[[], bytes]] = None):
        self.base_dir = str(base_dir or inbox_dir())
        self.hosts_path, self.accounts_path = hosts_path, accounts_path
        self.policy, self.clock = policy, clock
        self.key_provider = key_provider or vault_value_key

    # -- files ---------------------------------------------------------------

    def _dir(self, *parts: str) -> str:
        path = os.path.join(self.base_dir, *parts)
        os.makedirs(path, mode=0o700, exist_ok=True)
        for d in (self.base_dir, path):
            os.chmod(d, 0o700)
        return path

    @staticmethod
    def _write(path: str, data: Mapping[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _card_path(self, card_id: str) -> str:
        if not (isinstance(card_id, str) and len(card_id) == 12
                and all(c in "0123456789abcdef" for c in card_id)):
            raise KeyError("malformed card id")
        return os.path.join(self._dir("cards"), f"{card_id}.json")

    def get(self, card_id: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._card_path(card_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (KeyError, OSError, ValueError):
            return None

    def _all(self) -> List[Dict[str, Any]]:
        out = []
        cards = self._dir("cards")
        for name in sorted(os.listdir(cards)):
            if name.endswith(".json") and not name.startswith("."):
                card = self.get(name[:-5])
                if card:
                    out.append(card)
        return sorted(out, key=lambda c: (c.get("created", 0), c["id"]))

    def set_status(self, card_id: str, status: str, note: str = "",
                   channel: str = "") -> Optional[Dict]:
        card = self.get(card_id)
        if card is None:
            return None
        card.update(status=status, status_at=self.clock())
        if note:
            card["note"] = _plain(note)
        if channel:
            card["answered_via"] = channel
        self._write(self._card_path(card_id), card)
        return card

    # -- effective status (computed, never written while listing) --------------

    def effective_status(self, card: Mapping[str, Any]) -> str:
        status = str(card.get("status"))
        if status not in OPEN or card.get("how") in (HOW_STANDALONE, HOW_SOLVE_LIVE):
            return status
        from anyuse.execution.routine import ReceiptError, ReceiptStore
        try:
            chain = ReceiptStore(card["runs_dir"]).load(card["run_id"])
        except (ReceiptError, OSError, KeyError, ValueError):
            return "stale"
        after = [r for r in chain if int(r.get("seq", -1)) > int(card.get("seq", -1))]
        if any(r.get("type") == "cancelled" for r in after):
            return "cancelled"
        if any(r.get("type") in ("step", "reconcile") and r.get("step") == card["step"]
               and r.get("status") in ("completed", "not_done") for r in after):
            return "superseded"
        return status

    def cards(self, statuses=OPEN) -> List[Dict[str, Any]]:
        out = []
        for card in self._all():
            eff = self.effective_status(card)
            if statuses is None or eff in statuses:
                out.append({**card, "status": eff})
        return out

    # -- posting ---------------------------------------------------------------

    def post(self, engine, result: Mapping[str, Any], step: Mapping[str, Any]
             ) -> Optional[Dict[str, Any]]:
        """A card for a stopped run, or the open card already posted for it."""
        handoff = result.get("handoff") or {}
        reason = handoff.get("reason")
        nxt = handoff.get("next_step") or {}
        run_id = str(result["run_id"])
        host = engine.step_host(step)
        blk = dict(nxt.get("blocked") or {})
        if reason == "approval":
            kind, how = KIND_APPROVAL, HOW_APPROVE
        elif reason == "reconcile":
            kind, how = KIND_RECONCILE, HOW_RECONCILE
        elif reason in ("human_step", "confirm_model_proposal"):
            if step["mode"] == "agent":
                kind, how = KIND_DO_STEP, HOW_AGENT_CHOICE
            else:
                kind = str(step["action"].get("card") or KIND_DO_STEP)
                how = HOW_HUMAN_STEP
        elif reason == "blocked":
            host = str(blk.get("host") or host).lower()
            kind, how = kind_for_block(str(blk.get("reason") or "")), HOW_HUMAN_DOES_STEP
        elif reason == "human_only_host":
            kind, how = KIND_DO_STEP, HOW_HUMAN_DOES_STEP
        else:
            return None
        idig = str(nxt.get("inputs_digest") or "")
        for card in self.cards():
            if (card["run_id"], card["step"], card["kind"]) == (run_id, step["id"], kind) \
                    and (kind == KIND_RECONCILE or card["inputs_digest"] == idig):
                return card
        runner = engine.runners.get(step["action"].get("kind"))
        cont, why = continuation(kind, host, policy=self.policy, hosts_path=self.hosts_path,
                                 accounts_path=self.accounts_path,
                                 runner_shares_session=bool(
                                     getattr(runner, "supports_attached_session", False)))
        if how == HOW_HUMAN_DOES_STEP and cont:
            how = HOW_CONTINUE
        card_id = _sha([run_id, step["id"], kind, handoff.get("head")])[:12]
        outs = dict(step.get("outputs") or {})
        card = {
            "schema": "anyuse.inbox_card/2", "id": card_id, "kind": kind, "how": how,
            "status": "pending", "created": self.clock(),
            "run_id": run_id, "runs_dir": engine.store.base_dir,
            "seq": len(engine.store.load(run_id)) - 1,
            "manifest": engine.manifest["name"], "version": engine.manifest["version"],
            "manifest_digest": engine.digest, "step": step["id"], "mode": step["mode"],
            "host": host, "inputs_digest": idig,
            "instruction": _plain(step["action"].get("instruction") or ""),
            "effects": dict(step.get("effects") or {}),
            "expected_outputs": {k: str((v or {}).get("type")) for k, v in outs.items()},
            "secret_outputs": sorted(outs) if kind == KIND_VALUE else [],
            "policy": {"name": challenge_policy(self.policy), "continue_in_session": cont,
                       "why": _plain(why)},
            "error": _plain(result.get("error") or ""),
        }
        if nxt.get("candidates"):
            card["candidates"] = [c for c in nxt["candidates"]
                                  if isinstance(c, (str, int, float, bool))][:50]
        if blk:
            card["blocked"] = {"reason": blk.get("reason"), "status": blk.get("status")}
        for key in ("approval_digest", "reconcile_digest"):
            if nxt.get(key):
                card[key] = nxt[key]
        card["needed"] = self._needed(card)
        card["continues_after"] = self._continues(engine, step)
        self._write(self._card_path(card_id), card)
        return card

    def post_standalone(self, blocked: Mapping[str, Any], workflow: str
                        ) -> Dict[str, Any]:
        """A card for a direct compiled-tool call that hit a block page."""
        host = str(blocked.get("host") or "").lower()
        requested_kind = str(blocked.get("card_kind") or "")
        kind = (requested_kind if requested_kind in KINDS else
                kind_for_block(str(blocked.get("reason") or "")))
        for card in self.cards():
            same_card = ((card["step"], card["host"], card["kind"])
                         == (workflow, host, kind))
            if self.effective_status(card) in OPEN \
                    and card.get("how") == HOW_STANDALONE and same_card:
                return card
        card_id = _sha(["standalone", workflow, host, kind, self.clock()])[:12]
        rate_limited = str(blocked.get("reason") or "") == _blocked.RATE_LIMITED
        what = ("a human check" if kind == KIND_CHALLENGE else
                "a rate limit (it asked AnyUse to wait)" if rate_limited else "a sign-in or block page")
        card = {
            "schema": "anyuse.inbox_card/2", "id": card_id, "kind": kind,
            "how": HOW_STANDALONE, "status": "pending", "created": self.clock(),
            "run_id": "", "manifest": workflow, "version": "", "step": workflow,
            "mode": "proved", "host": host, "inputs_digest": "", "instruction": "",
            "expected_outputs": {}, "secret_outputs": [],
            "blocked": {"reason": blocked.get("reason"), "status": blocked.get("status")},
            "policy": {"name": ("ask" if kind == KIND_CONSENT else
                                  challenge_policy(self.policy)), "continue_in_session": False,
                       "why": "a direct tool call has no run to continue"},
            "needed": ((f"Accept or reject cookies on {host or 'the site'}? AnyUse can "
                        f"remember the choice, but will never click accept on your behalf")
                       if kind == KIND_CONSENT else
                       (f"{host or 'the site'} served {what} for {workflow}; AnyUse waits and "
                        f"will not retry before then. Run the tool again later")
                       if rate_limited else
                       (f"{host or 'the site'} served {what} for {workflow}; AnyUse will not "
                        f"get around it. Do it yourself in your own browser if you want "
                        f"the result")),
            "continues_after": "nothing automatic; run the tool again later if you like",
        }
        self._write(self._card_path(card_id), card)
        return card

    def post_live(self, req, view_url: Optional[str]) -> Dict[str, Any]:
        """A card for a live solve (``human_solves_live``). The private view URL
        of a headless run is kept Fernet-encrypted with the card (never in the
        card, a log or a tool result); ``anyuse inbox live <id>`` reveals it on
        the person's terminal only."""
        host = str(getattr(req, "host", "") or "").lower()
        task = str(getattr(req, "task_id", "") or "")
        step = str(getattr(req, "step", "") or "")
        card_id = _sha(["live", task, host, step, self.clock(), os.urandom(8).hex()])[:12]
        where = ("run `anyuse inbox live " + card_id + "` in your own terminal to open the "
                 "private live view of that page" if view_url else
                 "in your attached browser window")
        card = {
            "schema": "anyuse.inbox_card/2", "id": card_id, "kind": KIND_CHALLENGE,
            "how": HOW_SOLVE_LIVE, "status": "pending", "created": self.clock(),
            "run_id": str(getattr(req, "run_id", "") or ""),
            "task": task, "origin": getattr(req, "origin", None),
            "manifest": str(getattr(req, "manifest", "") or ""), "version": "",
            "step": step, "mode": "live", "host": host, "inputs_digest": "",
            "instruction": "", "expected_outputs": {}, "secret_outputs": [],
            "policy": {"name": HUMAN_SOLVES_LIVE, "continue_in_session": True,
                       "why": "the person solves it live; the session serves this task only"},
            "needed": (f"{host or 'the site'} asks for a human check; solve it yourself "
                       f"({where}). AnyUse never solves it and no model sees it"),
            "continues_after": "this task continues as soon as the page is back",
            "live_view": bool(view_url),
        }
        self._write(self._card_path(card_id), card)
        if view_url:
            self.keep(card_id, LIVE_URL_REF, view_url)
        return card

    def finish_live(self, card_id: str, outcome: str) -> None:
        """Close a live card and forget its view URL."""
        try:
            os.remove(self._values_path(card_id))
        except (OSError, KeyError):
            pass
        card = self.get(card_id)
        if card is not None and card.get("status") in OPEN:
            self.set_status(card_id, "done" if outcome == "cleared" else "closed",
                            note=f"live solve: {outcome}")

    @staticmethod
    def _needed(card: Mapping[str, Any]) -> str:
        host = card["host"] or "the site"
        kind, instr, sid = card["kind"], card["instruction"], card["step"]
        if kind == KIND_APPROVAL:
            eff = card.get("effects") or {}
            return (f"approve step {sid} ({eff.get('class')}, "
                    f"{'reversible' if eff.get('reversible') else 'not reversible'})")
        if kind == KIND_RECONCILE:
            return (f"say whether step {sid} happened: its outcome is uncertain and AnyUse "
                    f"will not repeat it to find out")
        if kind == KIND_CONSENT:
            return (f"Accept or reject cookies on {host}? AnyUse can remember the choice, "
                    f"but will never click accept on your behalf")
        if kind == KIND_VALUE:
            return (f"enter {', '.join(card['secret_outputs'])} (only you know it; kept "
                    f"encrypted, never shown or logged)" + (f": {instr}" if instr else ""))
        if card["how"] == HOW_CONTINUE:
            if kind == KIND_LOGIN:
                return (f"sign in to YOUR OWN account on {host} in the attached browser; "
                        f"the run then continues in that session")
            return (f"{host} asks for a human check; complete it yourself in the attached "
                    f"browser (your own site); the run then continues in that session")
        if kind == KIND_LOGIN:
            return (f"{host} needs a sign-in: sign in to your own account in your browser, "
                    f"do step {sid} yourself and hand back its result")
        if kind == KIND_CHALLENGE:
            return (f"{host} asks for a human check that AnyUse will not solve: do step "
                    f"{sid} yourself in your browser and hand back its result; "
                    f"AnyUse will not reuse that session")
        if card["how"] == HOW_HUMAN_DOES_STEP:
            why = ("is human-only in this run" if not card.get("blocked") else
                   f"refused automation ({(card.get('blocked') or {}).get('reason')})")
            return f"{host} {why}: do step {sid} yourself and hand back its result"
        return instr or f"do step {sid} yourself"

    @staticmethod
    def _continues(engine, step: Mapping[str, Any]) -> str:
        ids = [s["id"] for s in engine.steps]
        rest = engine.steps[ids.index(step["id"]) + 1:]
        if not rest:
            return "the run completes"
        parts = []
        for s in rest[:6]:
            host = engine.step_host(s)
            parts.append(f"{s['id']} ({s['mode']}{', ' + host if host else ''})")
        return "then: " + ", ".join(parts) + (" ..." if len(rest) > 6 else "")

    # -- encrypted values of value cards ----------------------------------------

    def _values_path(self, run_id: str) -> str:
        if not (run_id and all(c in "0123456789abcdef" for c in run_id)):
            raise KeyError("malformed run id")
        return os.path.join(self._dir("values"), f"{run_id}.json")

    def _fernet(self):
        from cryptography.fernet import Fernet
        return Fernet(self.key_provider())

    def keep(self, run_id: str, ref: str, value: Any) -> str:
        token = self._fernet().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")
        path = self._values_path(run_id)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data[ref] = token
        data["_cipher"] = "fernet, key " + VALUE_KEY_NAME + " in the vault key custody"
        self._write(path, data)
        return ref

    def reveal(self, run_id: str, ref: str) -> Any:
        try:
            with open(self._values_path(run_id), encoding="utf-8") as fh:
                token = json.load(fh)[ref]
            return json.loads(self._fernet().decrypt(token.encode("ascii")))
        except Exception:
            raise KeyError(ref) from None

    def run_finished(self, run_id: str) -> None:
        """Forget the run's values and close its open cards."""
        try:
            os.remove(self._values_path(run_id))
        except (OSError, KeyError):
            pass
        for card in self._all():
            if card["run_id"] == run_id and card["status"] in OPEN:
                self.set_status(card["id"], "closed")


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def card_summary(card: Mapping[str, Any]) -> Dict[str, Any]:
    return {"id": card["id"], "kind": card["kind"], "status": card["status"],
            "host": card.get("host") or "", "needed": _plain(card.get("needed")),
            "continues_after": _plain(card.get("continues_after")),
            "run_id": card["run_id"], "manifest": card.get("manifest"),
            "step": card["step"], "created": card.get("created"),
            "policy": (card.get("policy") or {}).get("name")}


def list_pending(inbox: Inbox, *, include_all: bool = False) -> Dict[str, Any]:
    """Read-only; the MCP tool and ``anyuse inbox`` both use this."""
    cards = inbox.cards(None if include_all else OPEN)
    return {"status": "ok", "pending": sum(1 for c in cards if c["status"] == "pending"),
            "cards": [card_summary(c) for c in cards], "how_to_complete": HOW_TO_COMPLETE}


def needs_human(card: Mapping[str, Any]) -> Dict[str, Any]:
    """The structured ``needs_human`` block a tool result carries."""
    return {"card": card["id"], "kind": card["kind"], "host": card.get("host") or "",
            "needed": card["needed"], "continues_after": card["continues_after"],
            "how_to_complete": HOW_TO_COMPLETE}


# ---------------------------------------------------------------------------
# the one completion core (elicitation and CLI both end here)
# ---------------------------------------------------------------------------

def _refused(card_id: str, error: str, **extra) -> Dict[str, Any]:
    return {"status": "refused", "card": card_id, "error": error, **extra}


def _coerce(type_name: str, raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    if type_name in ("string", "scalar"):
        return raw
    if type_name == "integer":
        return int(raw)
    if type_name == "number":
        return float(raw)
    if type_name == "boolean":
        return raw.strip().lower() in ("true", "yes", "1")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _current(engine, card) -> Tuple[Optional[Mapping[str, Any]], Dict[str, Any], str]:
    """The card's step if the run is still waiting on exactly it."""
    state, step, bound, why = engine.pending(card["run_id"])
    if why or step is None or step["id"] != card["step"] or \
            inputs_digest(bound) != card["inputs_digest"]:
        return None, {}, why or "the run is no longer waiting on this card"
    return step, bound, ""


def effective_how(inbox: Inbox, card: Mapping[str, Any], engine, step) -> str:
    """Re-read the policy now: a narrower setting since posting wins."""
    how = card["how"]
    if how == HOW_CONTINUE:
        runner = engine.runners.get(step["action"].get("kind"))
        ok, _ = continuation(card["kind"], card.get("host") or "", policy=inbox.policy,
                             hosts_path=inbox.hosts_path, accounts_path=inbox.accounts_path,
                             runner_shares_session=bool(
                                 getattr(runner, "supports_attached_session", False)))
        if not ok:
            return HOW_HUMAN_DOES_STEP
    return how


def _prepare(inbox, card, engine) -> Tuple[Optional[str], Dict[str, Any], str]:
    """(how, bound inputs, refusal) for a run card, re-checked now."""
    how = card["how"]
    if how in (HOW_APPROVE, HOW_RECONCILE):
        return how, {}, ""                  # the engine re-checks the digest itself
    step, bound, why = _current(engine, card)
    if step is None:
        inbox.set_status(card["id"], "superseded", why)
        return None, {}, why
    return effective_how(inbox, card, engine, step), bound, ""


def apply_answer(inbox: Inbox, card: Mapping[str, Any], engine, answer: Mapping[str, Any],
                 *, channel: str, how: str) -> Dict[str, Any]:
    """Record a person's answer for ``card`` and resume that run.

    ``answer``: ``{}`` (approve / continue in session) | ``{"outcome",
    "outputs"}`` (reconcile) | ``{"outputs", "secrets"}``. Every engine check
    applies unchanged, then the run resumes with its fresh checks.
    """
    card_id, run_id, sid = card["id"], card["run_id"], card["step"]
    if card.get("kind") == KIND_CONSENT:
        from anyuse.execution.consent import remember_decision
        remember_decision(card.get("host") or "", str(answer.get("choice") or ""))
    if how == HOW_APPROVE:
        res = engine.human_approve(run_id, sid, card.get("approval_digest", ""),
                                   channel=channel)
        if res.get("status") != "approved":
            return {**res, "card": card_id}
    elif how == HOW_RECONCILE:
        res = engine.human_reconcile(run_id, sid, str(answer.get("outcome") or ""),
                                     card.get("reconcile_digest", ""),
                                     answer.get("outputs") or None, channel=channel)
        if res.get("status") != "reconciled":
            return {**res, "card": card_id}
    elif how == HOW_AGENT_CHOICE:
        res = engine.resume(run_id, human={"step": sid, "outputs": answer.get("outputs")})
        nxt = ((res.get("handoff") or {}).get("next_step") or {}).get("id")
        if res.get("status") == "handed_off" and nxt == sid:
            return {**res, "for_card": card_id}
        inbox.set_status(card_id, "done", channel=channel)
        return {**res, "completed_card": card_id}
    else:
        secrets = dict(answer.get("secrets") or {})
        rec = engine.complete_card(run_id, sid, how=how, card_id=card_id,
                                   inputs_digest_=card["inputs_digest"],
                                   outputs=answer.get("outputs") or {},
                                   secret_outputs=secrets, host=card.get("host") or "",
                                   channel=channel)
        secrets.clear()
        if rec.get("status") != "recorded":
            return {**rec, "card": card_id}
    inbox.set_status(card_id, "done", channel=channel)
    return {**engine.resume(run_id), "completed_card": card_id}


# ---------------------------------------------------------------------------
# primary channel: MCP elicitation
# ---------------------------------------------------------------------------

_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number",
               "boolean": "boolean"}


def _field(name: str, type_name: str, description: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": _JSON_TYPES.get(type_name, "string"), "title": name}
    if description:
        out["description"] = description
    return out


def elicitation_for(card: Mapping[str, Any], how: str) -> Tuple[str, Dict[str, Any]]:
    """One message + one flat typed schema (what MCP form elicitation allows)."""
    host = card.get("host") or ""
    lines = [f"AnyUse needs you ({card['kind'].replace('_', ' ')}"
             f"{', ' + host if host else ''}).", card["needed"] + "."]
    if card.get("continues_after"):
        lines.append(f"After this: {card['continues_after']}.")
    if card["kind"] in (KIND_CHALLENGE, KIND_LOGIN):
        lines.append("AnyUse does not solve or bypass this check. Do it yourself, in your "
                     "own browser, only on your own account.")
    props: Dict[str, Any] = {}
    required: List[str] = []
    outs = dict(card.get("expected_outputs") or {})
    if how == HOW_APPROVE:
        lines.append(f"Run {card['run_id']} of {card['manifest']} {card['version']}; "
                     f"approval digest {str(card.get('approval_digest', ''))[:16]}.")
        props["approve"] = {"type": "boolean", "title": "Approve",
                            "description": f"Approve step {card['step']} as described"}
        required.append("approve")
    elif how == HOW_RECONCILE:
        props["outcome"] = {"type": "string", "title": "What happened",
                            "enum": ["completed", "not_done"],
                            "enumNames": ["It happened", "It did not happen"]}
        required.append("outcome")
        for name, t in outs.items():
            props[name] = _field(name, t, "Only if it happened: what you observed")
    elif card["kind"] == KIND_VALUE:
        for name, t in outs.items():
            props[name] = {**_field(name, t, card.get("instruction") or ""),
                           "writeOnly": True, "x-anyuse-sensitive": True}
            required.append(name)
        lines.append("Sensitive: AnyUse keeps it encrypted and never shows or logs it.")
    elif how == HOW_AGENT_CHOICE:
        props["choice"] = {"type": "string", "title": "Choice",
                           "enum": [str(c) for c in card.get("candidates") or []]}
        required.append("choice")
    elif how == HOW_CONTINUE:
        props["done"] = {"type": "boolean", "title": "Done",
                         "description": "I have finished it in the attached browser"}
        required.append("done")
    elif card["kind"] == KIND_CONSENT:
        props["choice"] = {"type": "string", "title": "Cookie choice",
                           "enum": ["reject", "accept"],
                           "enumNames": ["Reject non-essential cookies",
                                         "Accept (I will do this myself)"]}
        required.append("choice")
        lines.append("AnyUse may automate only the reject choice; it never clicks accept.")
    elif how == HOW_STANDALONE:
        props["done"] = {"type": "boolean", "title": "Done",
                         "description": "I did it myself in my own browser"}
        props["result"] = {"type": "string", "title": "Result (optional)",
                           "description": "What you saw, if the agent should use it"}
        required.append("done")
    else:
        props["done"] = {"type": "boolean", "title": "Done",
                         "description": "I did this step myself"}
        required.append("done")
        for name, t in outs.items():
            props[name] = _field(name, t, "The result you saw")
            required.append(name)
    return "\n".join(lines), {"type": "object", "properties": props, "required": required}


def answer_from_elicitation(card: Mapping[str, Any], how: str,
                            content: Mapping[str, Any]) -> Tuple[Optional[Dict], str]:
    """(answer, "") or (None, why). ``approve: false`` / ``done: false`` is a no."""
    content = dict(content or {})
    outs = dict(card.get("expected_outputs") or {})
    try:
        if how == HOW_APPROVE:
            return ({}, "") if content.get("approve") is True else (None, "not approved")
        if how == HOW_RECONCILE:
            outcome_ = content.get("outcome")
            if outcome_ not in ("completed", "not_done"):
                return None, "no outcome"
            vals = {k: _coerce(t, content[k]) for k, t in outs.items()
                    if k in content and content[k] not in (None, "")}
            return {"outcome": outcome_,
                    "outputs": vals if outcome_ == "completed" else {}}, ""
        if card["kind"] == KIND_VALUE:
            secrets = {k: _coerce(t, content.get(k)) for k, t in outs.items()}
            if any(v in (None, "") for v in secrets.values()):
                return None, "no value entered"
            return {"secrets": secrets}, ""
        if how == HOW_AGENT_CHOICE:
            match = [c for c in card.get("candidates") or []
                     if str(c) == content.get("choice")]
            if not match or not outs:
                return None, "no choice"
            return {"outputs": {next(iter(outs)): match[0]}}, ""
        if card["kind"] == KIND_CONSENT:
            choice = str(content.get("choice") or "").strip().lower()
            return ({"choice": choice}, "") if choice in ("accept", "reject") else (
                None, "no cookie choice")
        if content.get("done") is not True:
            return None, "not done"
        if how in (HOW_CONTINUE, HOW_STANDALONE):
            return {"result": str(content.get("result") or "")}, ""
        return {"outputs": {k: _coerce(t, content.get(k)) for k, t in outs.items()}}, ""
    except (TypeError, ValueError):
        return None, "an answer did not have the expected type"


class McpElicitor:
    """Asks through the connected MCP client from a worker thread.

    Built by the server only when the client advertises elicitation. ``ask``
    returns ``{"action": accept|decline|cancel, "content": {...}}``; a timeout
    or a transport error is a ``cancel`` (the card stays pending). Nothing is
    logged, so a value never reaches a log.
    """

    def __init__(self, session, loop, related_request_id=None, timeout_s: float = 900.0):
        self.session, self.loop = session, loop
        self.related_request_id, self.timeout_s = related_request_id, timeout_s

    def ask(self, message: str, schema: Mapping[str, Any]) -> Dict[str, Any]:
        coro = self.session.elicit_form(message, dict(schema),
                                        related_request_id=self.related_request_id)
        try:
            res = asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout_s)
        except Exception as exc:     # noqa: BLE001 — never a verdict, never logged
            return {"action": "cancel", "content": None, "error": type(exc).__name__}
        return {"action": str(res.action), "content": dict(res.content or {})}


def elicit_card(inbox: Inbox, card_id: str, *, engine_for, elicitor) -> Dict[str, Any]:
    """Ask the person about one card through the MCP client, then apply it.

    Returns the resumed run (or, for a standalone card, what the person
    handed back). ``{"asked": "declined"|"cancelled"|..., ...}`` means the card
    stays pending for ``anyuse inbox do``.
    """
    card = inbox.get(card_id)
    if card is None or inbox.effective_status(card) not in OPEN:
        return {"asked": "not_open", "card": card_id}
    how, engine = card["how"], None
    if how == HOW_SOLVE_LIVE:
        return {"asked": "not_asked", "card": card_id,
                "error": "a live solve is asked by the run that waits on it"}
    if how != HOW_STANDALONE:
        engine, refusal = engine_for(card["run_id"])
        if refusal:
            return {"asked": "not_asked", "card": card_id,
                    "error": str(refusal.get("error") or "")}
        how, _, why = _prepare(inbox, card, engine)
        if how is None:
            return {"asked": "not_open", "card": card_id, "error": why}
    message, schema = elicitation_for(card, how)
    reply = elicitor.ask(message, schema)
    action = reply.get("action")
    if action != "accept":
        return {"asked": "declined" if action == "decline" else "cancelled",
                "card": card_id}
    answer, why = answer_from_elicitation(card, how, reply.get("content") or {})
    reply.clear()
    if answer is None:
        return {"asked": "declined", "card": card_id, "error": why}
    if how == HOW_STANDALONE:
        if card["kind"] == KIND_CONSENT:
            from anyuse.execution.consent import remember_decision
            remember_decision(card.get("host") or "", str(answer.get("choice") or ""))
        inbox.set_status(card_id, "done", channel=HUMAN_CHANNEL_ELICIT)
        return {"status": "done_by_human", "completed_card": card_id,
                "human_result": answer.get("result") or answer.get("choice") or ""}
    return apply_answer(inbox, card, engine, answer, channel=HUMAN_CHANNEL_ELICIT, how=how)


# ---------------------------------------------------------------------------
# fallback channel: the CLI inbox behind the terminal gate
# ---------------------------------------------------------------------------

def _read_secret_tty(prompt: str) -> str:
    """Read one line from /dev/tty with echo off. Refuses if echo cannot be
    turned off; the value is never written anywhere by this function."""
    import termios
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    with open(fd, "r+", encoding="utf-8", closefd=True) as tty:
        old = termios.tcgetattr(fd)
        new = list(old)
        new[3] &= ~termios.ECHO
        tty.write(prompt)
        tty.flush()
        termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        try:
            line = tty.readline()
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            tty.write("\n")
            tty.flush()
    return line.rstrip("\r\n")


def do_card(inbox: Inbox, card_id: str, *, engine_for, gate,
            read_secret: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """A person completes a card at their terminal, then that run resumes."""
    card = inbox.get(card_id)
    if card is None:
        return _refused(card_id, "no such card")
    status = inbox.effective_status(card)
    if status not in OPEN:
        return _refused(card_id, f"the card is {status}")
    if card.get("how") == HOW_SOLVE_LIVE:
        return _refused(card_id, "a live solve card: run `anyuse inbox live "
                                 f"{card_id}` to open the private view")
    why = gate.environment_refusal()
    if why:
        return _refused(card_id, why)
    how, engine, bound = card["how"], None, {}
    if how != HOW_STANDALONE:
        engine, refusal = engine_for(card["run_id"])
        if refusal:
            return _refused(card_id, str(refusal.get("error") or "run unavailable"))
        if engine.digest != card["manifest_digest"]:
            return _refused(card_id, "the run's manifest version changed")
        if how == HOW_APPROVE:          # the gate shows the effect and asks the code
            res = engine.human_approve(card["run_id"], card["step"],
                                       card.get("approval_digest", ""),
                                       channel=HUMAN_CHANNEL_CLI, gate=gate)
            if res.get("status") != "approved":
                return {**res, "card": card_id}
            inbox.set_status(card_id, "done", channel=HUMAN_CHANNEL_CLI)
            return {**engine.resume(card["run_id"]), "completed_card": card_id}
        how, bound, why = _prepare(inbox, card, engine)
        if how is None:
            return _refused(card_id, why)
    outs = dict(card.get("expected_outputs") or {})
    secret_names = list(card.get("secret_outputs") or [])
    answer: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    try:
        with gate.open_tty() as tty:
            lines = ["", f"AnyUse inbox card {card_id} ({card['kind']})",
                     f"  run       {card['run_id'] or '-'} ({card['manifest']} "
                     f"{card['version']})", f"  step      {card['step']}",
                     f"  host      {card.get('host') or '-'}",
                     f"  needed    {card['needed']}",
                     f"  after     {card['continues_after']}"]
            if card.get("instruction"):
                lines.append(f"  step says {card['instruction']}")
            shown = {k: ("(hidden)" if is_secret_ref(v) else v) for k, v in bound.items()}
            if shown:
                lines.append("  inputs    " + json.dumps(shown, sort_keys=True,
                                                         default=str)[:500])
            if card["kind"] in (KIND_CHALLENGE, KIND_LOGIN):
                lines += ["", "  AnyUse does not solve or bypass this check. Do it yourself,",
                          "  in your own browser, only on your own account."]
            for line in lines + [""]:
                tty.write(line + "\n")
            if how == HOW_RECONCILE:
                tty.write("  did it happen? (completed / not_done): ")
                tty.flush()
                answer["outcome"] = (tty.readline() or "").strip()
            if card["kind"] == KIND_CONSENT:
                tty.write("  choice (accept / reject): ")
                tty.flush()
                answer["choice"] = (tty.readline() or "").strip().lower()
                if answer["choice"] not in ("accept", "reject"):
                    return _refused(card_id, "the choice must be accept or reject")
            if how in (HOW_HUMAN_STEP, HOW_AGENT_CHOICE, HOW_HUMAN_DOES_STEP) or (
                    how == HOW_RECONCILE and answer.get("outcome") == "completed"):
                for name, t in outs.items():
                    if name in secret_names:
                        continue
                    tty.write(f"  {name} ({t}): ")
                    tty.flush()
                    outputs[name] = _coerce(t, (tty.readline() or "").rstrip("\r\n"))
            tty.flush()
    except OSError as exc:
        return _refused(card_id, f"cannot open the controlling terminal ({exc})")
    except ValueError:
        return _refused(card_id, "a result did not have the expected type")
    secrets: Dict[str, Any] = {}
    reader = read_secret or _read_secret_tty
    for name in secret_names:
        try:
            raw = reader(f"  {name} (typing is not shown): ")
        except (OSError, ImportError) as exc:
            return _refused(card_id, f"cannot read a hidden value from the terminal "
                                     f"({type(exc).__name__}); nothing was recorded")
        if not raw:
            return _refused(card_id, "no value entered; nothing was recorded")
        try:
            secrets[name] = _coerce(outs[name], raw)
        except ValueError:
            return _refused(card_id, "the value did not have the expected type")
    digest = _sha({"purpose": "inbox", "card": card_id, "run_id": card["run_id"],
                   "step": card["step"], "kind": card["kind"], "how": how,
                   "inputs_digest": card["inputs_digest"], "outputs": outputs,
                   "answer": answer, "secret_outputs": sorted(secrets)})
    confirm = [f"  completing {card_id}: {how.replace('_', ' ')}"]
    if answer:
        confirm.append("  answer    " + json.dumps(answer, sort_keys=True))
    if outputs:
        confirm.append("  result    " + json.dumps(outputs, sort_keys=True, default=str)[:500])
    if secrets:
        confirm.append(f"  hidden    {', '.join(sorted(secrets))} (entered, not shown)")
    if how == HOW_CONTINUE:
        confirm.append("  the run continues in your attached browser session")
    why = gate._ask(confirm + [""], digest, "complete this card")
    if why:
        return _refused(card_id, why)
    if how == HOW_STANDALONE:
        if card["kind"] == KIND_CONSENT:
            from anyuse.execution.consent import remember_decision
            remember_decision(card.get("host") or "", str(answer.get("choice") or ""))
        inbox.set_status(card_id, "done", channel=HUMAN_CHANNEL_CLI)
        return {"status": "done_by_human", "completed_card": card_id,
                **({"human_result": answer["choice"]} if answer.get("choice") else {})}
    channel = HUMAN_CHANNEL_CLI if how == HOW_RECONCILE else HUMAN_CHANNEL_INBOX
    return apply_answer(inbox, card, engine, {**answer, "outputs": outputs,
                                              "secrets": secrets},
                        channel=channel, how=how)


def skip_card(inbox: Inbox, card_id: str, *, gate) -> Dict[str, Any]:
    """Put a card aside (the run keeps waiting; ``do`` still works on it)."""
    card = inbox.get(card_id)
    if card is None:
        return _refused(card_id, "no such card")
    why = gate.environment_refusal()
    if why:
        return _refused(card_id, why)
    if inbox.effective_status(card) != "pending":
        return _refused(card_id, f"the card is {inbox.effective_status(card)}")
    inbox.set_status(card_id, "skipped")
    return {"status": "skipped", "card": card_id, "run_id": card["run_id"]}


def cancel_card(inbox: Inbox, card_id: str, *, engine_for, gate) -> Dict[str, Any]:
    """Cancel the card's run for good (typed-back code)."""
    card = inbox.get(card_id)
    if card is None:
        return _refused(card_id, "no such card")
    if inbox.effective_status(card) not in OPEN:
        return _refused(card_id, f"the card is {inbox.effective_status(card)}")
    why = gate.environment_refusal()
    if why:
        return _refused(card_id, why)
    digest = _sha({"purpose": "inbox-cancel", "card": card_id, "run_id": card["run_id"]})
    why = gate._ask(["", f"Cancel {card['run_id'] or card['step']} ({card['manifest']}) "
                         f"for good.", f"  waiting on {card['step']}: {card['needed']}", ""],
                    digest, "cancel this")
    if why:
        return _refused(card_id, why)
    if card["how"] == HOW_STANDALONE:
        inbox.set_status(card_id, "cancelled")
        return {"status": "cancelled", "card": card_id}
    engine, refusal = engine_for(card["run_id"])
    if refusal:
        return _refused(card_id, str(refusal.get("error") or "run unavailable"))
    res = engine.cancel(card["run_id"], card_id=card_id)
    if res.get("status") == "cancelled":
        inbox.set_status(card_id, "cancelled")
    return {**res, "card": card_id}


def reveal_live(inbox: Inbox, card_id: str, *, gate) -> Dict[str, Any]:
    """Show a waiting live solve's private view URL on the person's terminal.

    Behind the terminal gate (TTYs, agent-marker refusal, typed-back code); the
    URL is written to ``/dev/tty`` only and never returned, so it cannot reach
    stdout, a log, an agent or a model."""
    card = inbox.get(card_id)
    if card is None or card.get("how") != HOW_SOLVE_LIVE:
        return _refused(card_id, "no such live solve card")
    if card.get("status") != "pending":
        return _refused(card_id, f"the card is {card.get('status')}")
    if not card.get("live_view"):
        return _refused(card_id, "this check is solved in your attached browser window; "
                                 "there is no view to open")
    why = gate.environment_refusal()
    if why:
        return _refused(card_id, why)
    digest = _sha({"purpose": "inbox-live", "card": card_id, "task": card.get("task")})
    why = gate._ask(["", f"Live solve {card_id}: {card['needed']}.",
                     "The link opens a private view of that ONE page on this computer; it",
                     "works once and closes when the check clears. Only you solve it.", ""],
                    digest, "show the link")
    if why:
        return _refused(card_id, why)
    try:
        url = inbox.reveal(card_id, LIVE_URL_REF)
    except KeyError:
        return _refused(card_id, "the view is gone (the check cleared or it expired)")
    try:
        with gate.open_tty() as tty:
            tty.write(f"\n  open: {url}\n\n")
            tty.flush()
    except OSError as exc:
        return _refused(card_id, f"cannot open the controlling terminal ({exc})")
    return {"status": "shown", "card": card_id, "shown_on": "your terminal"}
