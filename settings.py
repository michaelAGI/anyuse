"""Typed, persistent behavior settings for AnyUse.

Resolution is deliberately central and late-bound::

    explicit argument > process environment > settings.toml > built-in default

The file contains behavior only.  Secrets, endpoints, and storage-location
variables remain environment/argument configuration and are never copied into
it.  Environment parsing preserves the historical behavior of each setting;
the CLI's persistent writer is stricter and rejects misspellings.
"""
from __future__ import annotations

import math
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


UNSET = object()
SETTINGS_FILE = "settings.toml"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


class SettingError(ValueError):
    """A setting name, value, or settings file is invalid."""


Parser = Callable[[Any, bool], Any]
Confirm = Callable[[Any], bool]


@dataclass(frozen=True)
class Setting:
    name: str
    value_type: str
    default: Any
    description: str
    allowed_values: tuple[Any, ...] = ()
    security_relevant: bool = False
    parser: Optional[Parser] = None
    confirm_when: Optional[Confirm] = None

    def parse(self, value: Any, *, strict: bool) -> Any:
        if self.parser is not None:
            return self.parser(value, strict)
        if self.value_type == "boolean":
            return _parse_bool(value, strict)
        if self.value_type == "integer":
            return _parse_int(value, strict)
        if self.value_type == "number":
            return _parse_float(value, strict)
        text = str(value).strip()
        if self.allowed_values:
            lowered = text.lower()
            allowed = {str(item).lower(): item for item in self.allowed_values}
            if lowered not in allowed:
                if strict:
                    raise SettingError(
                        f"{self.name} must be one of: "
                        + ", ".join(map(str, self.allowed_values)))
                return self.default
            return allowed[lowered]
        if strict and not text:
            raise SettingError(f"{self.name} must not be empty")
        return text or self.default

    def needs_confirmation(self, value: Any) -> bool:
        if not self.security_relevant:
            return False
        return self.confirm_when(value) if self.confirm_when else True


@dataclass(frozen=True)
class ResolvedSetting:
    setting: Setting
    value: Any
    source: str


def _parse_bool(value: Any, strict: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    if strict:
        raise SettingError("expected a boolean: on/off, true/false, yes/no, or 1/0")
    return False


def _positive_bool(default: bool) -> Parser:
    def parse(value: Any, strict: bool) -> bool:
        if strict:
            return _parse_bool(value, True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in _TRUTHY
    return parse


def _negative_bool(value: Any, strict: bool) -> bool:
    """Legacy kill-switch syntax: only an explicit false word disables it."""
    if strict:
        return _parse_bool(value, True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSEY


def _parse_int(value: Any, strict: bool) -> int:
    if isinstance(value, bool):
        raise SettingError("expected an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SettingError("expected an integer") from exc


def _parse_float(value: Any, strict: bool) -> float:
    if isinstance(value, bool):
        raise SettingError("expected a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingError("expected a number") from exc
    if not math.isfinite(result):
        raise SettingError("expected a finite number")
    return result


def _non_negative_float(value: Any, strict: bool) -> float:
    result = _parse_float(value, strict)
    if result < 0:
        raise SettingError("expected a non-negative number")
    return result


#: the word that lifts a dollar cap (``ANYUSE_RUN_BUDGET_USD``, ``ANYUSE_SPEND_DAILY_USD``);
#: readers turn it into ``math.inf``. ``inf`` itself stays refused, so only this word lifts a cap.
UNLIMITED = "unlimited"
DEFAULT_RUN_BUDGET_USD = 1.0
DEFAULT_SPEND_DAILY_USD = 5.0
#: the agent harness's per-run budget (``execution/agent_harness.DEFAULT_MAX_USD``)
DEFAULT_AGENT_MAX_USD = 1.0


def _dollar_cap(default: float) -> Parser:
    """A non-negative amount, or ``unlimited`` (kept as that word: TOML has no portable inf)."""
    def parse(value: Any, strict: bool) -> Any:
        if isinstance(value, str) and value.strip().lower() == UNLIMITED:
            return UNLIMITED
        if not strict and str(value).strip() == "":
            return default
        return _non_negative_float(value, strict)
    return parse


def _raises_cap(default: float) -> Confirm:
    """Confirmation is needed above the default and for ``unlimited``; lowering needs none."""
    def check(value: Any) -> bool:
        return value == UNLIMITED or float(value) > default
    return check


_run_budget = _dollar_cap(DEFAULT_RUN_BUDGET_USD)


def _non_negative_int(value: Any, strict: bool) -> int:
    try:
        result = _parse_int(value, strict)
    except SettingError:
        if strict:
            raise
        return 86_400
    if strict and result < 0:
        raise SettingError("expected a non-negative integer")
    return max(0, result)


def _retries(value: Any, strict: bool) -> int:
    if not strict and str(value).strip() == "":
        return 2
    result = _parse_int(value, strict)
    # Runtime model resolution owns the historical range error/message.  The
    # persistent writer is strict so an invalid value never reaches the file.
    if strict and not 0 <= result <= 5:
        raise SettingError("expected an integer from 0 through 5")
    return result


def _unit_interval(value: Any, strict: bool) -> float:
    try:
        result = _parse_float(value, strict)
    except SettingError:
        if strict:
            raise
        return 0.0
    if strict and not 0.0 <= result <= 1.0:
        raise SettingError("expected a number from 0 through 1")
    return max(0.0, min(1.0, result))


def _ttl_days(value: Any, strict: bool) -> float:
    try:
        result = _parse_float(value, strict)
    except SettingError:
        if strict:
            raise
        return 7.0
    if strict and result < 0:
        raise SettingError("expected a non-negative number")
    return result


def _candidate_ttl_hours(value: Any, strict: bool) -> float:
    try:
        result = _parse_float(value, strict)
    except SettingError:
        if strict:
            raise
        return 24.0
    if strict and result < 0:
        raise SettingError("expected a non-negative number")
    return result


def _compile_levers(value: Any, strict: bool) -> str:
    text = str(value).strip().lower()
    aliases = {"", "on", "all", "default", "off", "none"}
    allowed = {"repetition", "volatile", "post", "binding", "path", "slots", "nested"}
    if text in aliases:
        return text or "default"
    parts = {part.strip() for part in text.split(",") if part.strip()}
    unknown = parts - allowed
    if strict and unknown:
        raise SettingError("unknown compile lever(s): " + ", ".join(sorted(unknown)))
    return text


def _string(value: Any, strict: bool) -> str:
    text = str(value).strip()
    if strict and not text:
        raise SettingError("expected a non-empty string")
    return text


def _optional_string(value: Any, strict: bool) -> str:
    return str(value).strip()


#: the model adapter's presets (execution/model_adapter.PROVIDER_PRESETS; a test keeps the two equal)
MODEL_PROVIDERS = ("", "cerebras", "openrouter", "local", "gemini", "openai", "anthropic", "claude-cli", "codex-cli")


def _model_provider(value: Any, strict: bool) -> str:
    text = str(value).strip().lower()
    allowed = set(MODEL_PROVIDERS)
    if strict and text not in allowed:
        raise SettingError("expected one of: " + ", ".join(p for p in MODEL_PROVIDERS if p))
    # Invalid environment values historically reach resolve_model_config(),
    # which raises its own "unknown model provider" error.
    return text


def _optional_retries(value: Any, strict: bool) -> Any:
    if str(value).strip() == "":
        return ""
    return _retries(value, strict)


def _optional_non_negative_float(value: Any, strict: bool) -> Any:
    if str(value).strip() == "":
        return ""
    return _non_negative_float(value, strict)


def _webhook_url(value: Any, strict: bool) -> str:
    """https://, or http:// only to an exact loopback host. Anything else is
    refused by the CLI writer and read as "unset" (no notification) from the
    environment, so a typo can never send a card summary somewhere odd."""
    text = str(value).strip()
    if not text:
        return ""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        parts = None
    ok = bool(parts and parts.hostname and not parts.username and not parts.password
              and (parts.scheme == "https"
                   or (parts.scheme == "http"
                       and parts.hostname in ("127.0.0.1", "localhost", "::1"))))
    if ok:
        return text
    if strict:
        raise SettingError("expected an https:// URL (http:// only for a loopback host), "
                           "without credentials in it")
    return ""


def _imessage_to(value: Any, strict: bool) -> str:
    """One phone number (+, digits, spaces, dashes) or one e-mail address."""
    import re
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\+?[0-9][0-9 \-]{4,20}", text) or re.fullmatch(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text):
        return text
    if strict:
        raise SettingError("expected one phone number or one e-mail address")
    return ""


#: Run origins that may use ``human_solves_live``. ``bench`` is deliberately not
#: here: published numbers must never depend on a person solving a challenge,
#: so no value of the setting can admit it (execution/live_solve.py enforces it
#: again at run time).
LIVE_SOLVE_ORIGINS = ("user_task", "scheduled", "bulk")


def _live_solve_origins(value: Any, strict: bool) -> str:
    parts = [p.strip().lower() for p in str(value if value is not None else "").split(",")]
    parts = [p for p in parts if p]
    bad = [p for p in parts if p not in LIVE_SOLVE_ORIGINS]
    if bad:
        if strict:
            if "bench" in bad:
                raise SettingError("bench runs can never use live solving (published "
                                   "numbers must not depend on a person solving a challenge)")
            raise SettingError("expected a comma list over: " + ", ".join(LIVE_SOLVE_ORIGINS))
        parts = [p for p in parts if p in LIVE_SOLVE_ORIGINS]
    if not parts:
        if strict:
            raise SettingError("expected at least one of: " + ", ".join(LIVE_SOLVE_ORIGINS))
        return "user_task"
    return ",".join(o for o in LIVE_SOLVE_ORIGINS if o in parts)


def _live_solve_timeout(value: Any, strict: bool) -> int:
    try:
        result = _parse_int(value, strict)
    except SettingError:
        if strict:
            raise
        return 900
    if strict and not 30 <= result <= 3600:
        raise SettingError("expected seconds between 30 and 3600")
    return min(3600, max(30, result))


def _spec(name: str, value_type: str, default: Any, description: str, *,
          allowed: tuple[Any, ...] = (), security: bool = False,
          parser: Optional[Parser] = None,
          confirm_when: Optional[Confirm] = None) -> Setting:
    return Setting(name, value_type, default, description, allowed, security,
                   parser, confirm_when)


# This is the one behavior-setting registry. Path selection (ANYUSE_DATA_DIR,
# *_FILE), credentials, and browser endpoints intentionally do not belong here.
SETTINGS: dict[str, Setting] = {
    item.name: item for item in (
        # ``meta`` is a projection of ``default`` (four fixed tools over the same
        # run paths, docs/MCP_META.md), so only ``full`` widens the surface.
        _spec("ANYUSE_MCP_PROFILE", "string", "default",
              "MCP tool surface: the safe launch profile, its four-tool meta form, "
              "or the full compatibility surface.",
              allowed=("default", "meta", "full"), security=True,
              confirm_when=lambda value: value == "full"),
        _spec("ANYUSE_RUN_BUDGET_USD", "number", DEFAULT_RUN_BUDGET_USD,
              "Maximum model spend reserved for one run, in US dollars, or unlimited.",
              security=True, parser=_run_budget,
              confirm_when=_raises_cap(DEFAULT_RUN_BUDGET_USD)),
        _spec("ANYUSE_SPEND_DAILY_USD", "number", DEFAULT_SPEND_DAILY_USD,
              "Global cap on model spend per UTC day, in US dollars, or unlimited; every "
              "model request of the anyuse CLI or the MCP server counts.",
              security=True, parser=_dollar_cap(DEFAULT_SPEND_DAILY_USD),
              confirm_when=_raises_cap(DEFAULT_SPEND_DAILY_USD)),
        _spec("ANYUSE_ATTRIBUTE_VERIFY", "boolean", True,
              "Verifier independence: a web answer's critical attributes (availability, price, currency, name, "
              "URL; a list's titles) must agree with an independent second reading of the page, unknown phrases "
              "are uncertain, and only an uncertain attribute asks Cerebras gpt-oss-120b (abstention allowed); "
              "an attribute still uncertain withholds the answer, and a two-source disagreement no rule calls a "
              "known wrong pattern returns it as needs_review (never accepted; both sources' evidence). Each row "
              "visit_results opens is checked on its own item page (its identity against its card on the list "
              "page, its fields); a row that does not verify is withheld. ON by "
              "default; without a configured model the rules still run and a case that "
              "needs the model is withheld (verifier_model_unavailable).",
              parser=_positive_bool(True)),
        _spec("ANYUSE_WEB_FALLBACK", "boolean", True,
              "Saved web tools over MCP (docs/MCP_WEB_TOOLS.md): when a replay misses (failed, incomplete, a known "
              "wrong answer; never needs_review), the agent harness answers the same value on the same site with "
              "the configured model and a read-only browser, at most 8 model calls per run under the daily spend cap; only an "
              "answer the independent attribute verifier passes is returned (source: fallback), and it is booked "
              "so the repair ladder can make the next replay model-free again. ON by default; "
              "without a configured model the miss is returned with fallback no_model_configured.",
              parser=_positive_bool(True)),
        _spec("ANYUSE_AGENT_ESCALATION", "boolean", False,
              "Allow the learn agent to escalate beyond its base model after measured failure.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_AGENT_MAX_STEPS", "integer", 20,
              "Agent harness: the most steps (one policy call each) an open-ended agent run takes."),
        _spec("ANYUSE_AGENT_MAX_USD", "number", DEFAULT_AGENT_MAX_USD,
              "Agent harness: the most an open-ended agent run may spend on its policy's model calls, in US "
              "dollars (a CLI policy books the CLI's reported cost), or unlimited; 0 allows no model calls.",
              security=True, parser=_dollar_cap(DEFAULT_AGENT_MAX_USD),
              confirm_when=_raises_cap(DEFAULT_AGENT_MAX_USD)),
        _spec("ANYUSE_AGENT_UNASKED_IRREVERSIBLE", "string", "ask",
              "Agent harness, open tasks: a commit step (a login, a cart, a form sent, a "
              "payment, a deletion) the task did not cover: ask (the caller is asked once at that step: a "
              "confirm_required answer over MCP, a prompt in the CLI; no answer means stop) or stop (never asked: "
              "the run stops and reports it, for unattended use). hold is the earlier name of ask. A covered step "
              "runs without a question (initiation = approval); a deviation always stops. There is no allow.",
              allowed=("ask", "stop", "hold"), security=True,
              confirm_when=lambda value: False),
        _spec("ANYUSE_MCP_AGENT_COMMITS", "string", "human",
              "agent_run over MCP: who does a commit step (a login, a cart, a form sent, a payment, a "
              "deletion). human: the run stops and hands that step to you, even when the task asked for it, "
              "because over MCP the task and any confirmation come from an agent. agent: the calling agent's "
              "task approves it, and a step the task did not cover is asked of the calling agent.",
              allowed=("human", "agent"), security=True,
              confirm_when=lambda value: value == "agent"),
        _spec("ANYUSE_AGENT_CONTAINER_SANDBOX", "boolean", False,
              "Agent harness: let a runtime-written tool run in the Apple container sandbox "
              "(no host mount but the task workspace, network off). Off: the sandbox is planned, "
              "never started. A running tart VM refuses it until the vsock fix is confirmed.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_FILL_SHOW_VALUES", "boolean", False,
              "Agent bridge: the model also sees the values of the fields on screen "
              "(never a password field's: macOS does not expose it).",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_FILL_FREE_TEXT", "boolean", False,
              "Agent bridge: the model may type its own text; it is saved in the step and "
              "typed on every later run.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_FILL_SENSITIVE", "boolean", False,
              "Agent bridge: password, payment and terminal steps can be filled; a password is "
              "typed from the credential vault by alias, never shown to the model or saved.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        # the watcher's capture exclusion (watcher/batch_watcher.py): entries ADD to the built-in
        # password-manager and banking / payment / finance lists, never replace them
        _spec("ANYUSE_WATCH_DENYLIST_APPS", "string", "",
              "Watcher: more apps it never captures (bundle ids, or app names matched as whole words); "
              "comma-separated. Adds to the built-in password-manager and banking / payment / finance "
              "lists.", parser=_optional_string),
        _spec("ANYUSE_WATCH_DENYLIST_TITLES", "string", "",
              "Watcher: more window-title words that make a window never captured; comma-separated, "
              "matched as whole words. Adds to the built-in lists.", parser=_optional_string),
        _spec("ANYUSE_MAP_REVIEWER", "string", "claude:sonnet",
              "Compile from the map: the independent reviewer (gate R) that must say a plan covers the "
              "task before the workflow is saved ready - 'cli:model' with cli claude or codex; 'none' "
              "disables it, and then no map-compiled workflow is ever ready. Default: the measured one.",
              security=True, parser=_optional_string, confirm_when=lambda v: str(v or "") not in ("claude:sonnet",)),
        # the local runner's boundary for app switches (core/restriction.py from_settings):
        # lowering it (an empty value) asks for the confirmation code
        _spec("ANYUSE_APP_BLOCKLIST", "string", "",
              "Draft replay and fill: apps (bundle ids, or names) an app switch may never open; "
              "comma-separated.", security=True, parser=_optional_string,
              confirm_when=lambda v: not str(v or "").strip()),
        _spec("ANYUSE_APP_ALLOWLIST", "string", "",
              "Draft replay and fill: when set, the only apps (bundle ids, or names) an app switch "
              "may open; comma-separated.", security=True, parser=_optional_string,
              confirm_when=lambda v: not str(v or "").strip()),
        _spec("ANYUSE_MODEL_PROVIDER", "string", "",
              "Optional provider preset applied to every model role.",
              allowed=MODEL_PROVIDERS,
              parser=_model_provider),
        _spec("ANYUSE_MODEL_BASE_URL", "string", "",
              "Optional OpenAI-compatible base URL applied to every model role.",
              security=True, parser=_optional_string,
              confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_MODEL_NAME", "string", "",
              "Optional model name applied to every model role.",
              parser=_optional_string),
        _spec("ANYUSE_MODEL_API_KEY_ENV", "string", "",
              "Optional credential environment-variable name for every model role.",
              security=True, parser=_optional_string,
              confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_MODEL_RETRIES", "integer", 2,
              "Extra attempts after retryable model-provider failures (maximum 5).",
              parser=_retries),
        _spec("ANYUSE_MODEL_PRICE_IN", "number", "",
              "Optional input-token list price per million tokens for every model role.",
              parser=_optional_non_negative_float),
        _spec("ANYUSE_MODEL_PRICE_OUT", "number", "",
              "Optional output-token list price per million tokens for every model role.",
              parser=_optional_non_negative_float),
        _spec("ANYUSE_GEMINI_MODEL_FLASH", "string", "gemini-3.5-flash",
              "Gemini model used for general perception and control.", parser=_string),
        _spec("ANYUSE_GEMINI_MODEL_COMPUTER_USE", "string",
              "gemini-2.5-computer-use-preview-10-2025",
              "Gemini model used for last-resort visual computer use.", parser=_string),
        _spec("ANYUSE_ROBOTS", "string", "respect",
              "robots.txt: respect (default, RFC 9309: everything except the site's own pages in a "
              "signed-in task you started - an MCP tool call, a CLI run or replay that signs in with your "
              "credentials or uses your imported session); user_initiated (a lookup or "
              "compile the user or caller explicitly started - an MCP tool call, a CLI run or compile - may fetch "
              "paths robots.txt disallows; breadth exploration and unattended work still respect it); ignore. Every "
              "mode keeps AnyUse's user agent, pacing, budgets, the NEVER_CONTACT refusals (and the government "
              "refusals with ANYUSE_REFUSE_GOV_HOSTS), the "
              "third-party rules and never bypasses a CAPTCHA; robots.txt is still fetched and an override is "
              "recorded.",
              allowed=("respect", "user_initiated", "ignore"), security=True,
              confirm_when=lambda value: value != "respect"),
        _spec("ANYUSE_ROBOTS_POLICY", "string", "respect",
              "Older global switch for compiled HTTP workflows (respect | ignore); ignore means the same as "
              "ANYUSE_ROBOTS=ignore. Prefer ANYUSE_ROBOTS.",
              allowed=("respect", "ignore"), security=True,
              confirm_when=lambda value: value == "ignore"),
        _spec("ANYUSE_REFUSE_GOV_HOSTS", "boolean", False,
              "Refuse government, .int, EU-institution and military hosts (execution/gov_hosts.py) on every "
              "path before any request. Off by default; the NEVER_CONTACT list applies either way.",
              security=True, parser=_positive_bool(False), confirm_when=lambda value: False),
        _spec("ANYUSE_CONSENT_POLICY", "string", "reject_nonessential",
              "Cookie-consent walls: reject non-essential cookies, ask a person, "
              "or never interact.",
              allowed=("reject_nonessential", "ask", "never")),
        _spec("ANYUSE_CHALLENGE_POLICY", "string", "human_does_step",
              "What happens after a person clears a third-party anti-bot challenge: "
              "human_does_step (the person does the blocked step; the session is never reused) "
              "continue_in_own_session (only for own-site overrides and own-account logins), "
              "or human_solves_live (the person solves it live in their browser or a loopback "
              "view; AnyUse only relays their pointer and keys; see ANYUSE_LIVE_SOLVE_ORIGINS).",
              allowed=("human_does_step", "continue_in_own_session", "human_solves_live"),
              security=True,
              confirm_when=lambda value: value in ("continue_in_own_session",
                                                   "human_solves_live")),
        _spec("ANYUSE_LIVE_SOLVE_ORIGINS", "string", "user_task",
              "Run origins where human_solves_live applies: a comma list over user_task, "
              "scheduled, bulk. Widening beyond user_task means unattended runs wait for "
              "a person to solve a challenge. bench is never allowed.",
              security=True, parser=_live_solve_origins,
              confirm_when=lambda value: value != "user_task"),
        _spec("ANYUSE_LIVE_SOLVE_TIMEOUT_S", "integer", 900,
              "Seconds a run waits for a person to solve a challenge live (30 to 3600); "
              "the live view expires then and the run falls back to human_does_step.",
              parser=_live_solve_timeout),
        _spec("ANYUSE_ROBOTS_TTL_S", "integer", 86_400,
              "Seconds to retain robots.txt cache entries; zero disables disk caching.",
              parser=_non_negative_int),
        _spec("ANYUSE_DRIVE_ENABLED", "boolean", False,
              "Keystone permission for AnyUse to move the pointer or type.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_HIL_MODE", "string", "off",
              "Human approval holds for desktop actions: off, destructive, or all.",
              allowed=("off", "destructive", "all"), security=True,
              confirm_when=lambda value: value == "off"),
        _spec("ANYUSE_API_MODALITY", "boolean", True,
              "Enable deterministic HTTP replay as an execution modality.",
              parser=_negative_bool),
        _spec("ANYUSE_API_READONLY_POST", "boolean", False,
              "Permit separately approved read-only POST operations. A search provider's read POST "
              "that the site's own page sends (a page-keyed tool) runs without it once its host is "
              "acknowledged.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_PAGE_KEYS", "boolean", True,
              "Compiled HTTP tools may read a value the site's own page states by name (a search "
              "provider's public key, app id or index, a landing page's id) from that page at run "
              "time and send it as the page does; the value is never stored. Off: such searches stay "
              "on the browser replay.",
              security=True, parser=_positive_bool(True), confirm_when=lambda value: False),
        _spec("ANYUSE_DEMO_MODEL", "string", "sonnet",
              "Compile demonstrations: which model makes the "
              "demonstration of `agent run --compile` when no model provider is chosen. sonnet "
              "(default) = Sonnet through the Claude CLI (claude -p), at most US$0.40 per "
              "demonstration by the CLI's count, booked in the spend ledger inside "
              "ANYUSE_SPEND_DAILY_USD; cerebras = Cerebras as before; cascade = Cerebras first, "
              "then ONE Sonnet retry (same cap) of a demonstration that failed for an "
              "agent-quality reason, once per site. Proof replays stay at 0 model calls; the "
              "verifier and the MCP fallback are unchanged.",
              allowed=("sonnet", "cerebras", "cascade")),
        _spec("ANYUSE_DEMO_INSTRUCTIONS", "string", "v1",
              "The web demonstration's instructions: v1 (default) = the "
              "agent harness's prompt; v2 = the same plus five rules for site-search "
              "results (full-text matches, one row per result by its name, the result list only, no_results only "
              "on the page's statement, open the named search URL). Read once per demonstration policy; the MCP "
              "route's fallback agent and the adapters keep their own prompts.",
              allowed=("v1", "v2")),
        _spec("ANYUSE_THIRD_PARTY", "string", "search",
              "Guarded browser, third-party hosts (every guarded "
              "path): off = first-party only; search (default) = the site's own search "
              "provider: the third-party host the site's own page sends the typed value to in a "
              "data call, or a catalogued provider (Algolia, Doofinder, Klevu, Findologic, "
              "Searchspring, ...) once the page names it, its search endpoint "
              "only; any = any third-party host the site's own page uses for its content "
              "(APIs, widgets, data CDNs), except ads/trackers on the pinned snapshot. Always "
              "read-only as the page sends it, robots.txt of every host, "
              "NEVER_CONTACT hosts refused (and government hosts with ANYUSE_REFUSE_GOV_HOSTS), "
              "every host on the run record "
              "(docs/TRAFFIC_POLICY.md).",
              allowed=("off", "search", "any"), security=True,
              confirm_when=lambda value: value == "any"),
        _spec("ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS", "boolean", False,
              "SUPERSEDED by ANYUSE_THIRD_PARTY (no longer read): the site's own search "
              "provider is ANYUSE_THIRD_PARTY=search, the default.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_ALLOW_READONLY_POST_SEARCH", "boolean", False,
              "Guarded browser: allow a same-site POST only when it is the page's own "
              "search (its search form or a small body carrying the search value); "
              "state-changing paths and fields are refused.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_ALLOW_SITE_OWN_HOSTS", "boolean", True,
              "Guarded browser: allow the site's own subdomains/CDN hosts (same registrable "
              "domain - a shared-hosting suffix counts per tenant -, or a host declared per site "
              "with a reason), GET/HEAD, in the site's budget; robots.txt of every host. ON by "
              "default; off = the site's exact host only.",
              security=True, parser=_positive_bool(True), confirm_when=lambda value: False),
        _spec("ANYUSE_THIRD_PARTY_PAGE_CODE", "boolean", True,
              "Guarded browser: the page's own SCRIPTS and STYLESHEETS load from any other "
              "registrable domain the page requests - GET only, by the browser's resource type; never images, fonts, "
              "media, XHR / fetch, websockets or beacons; never a host on the pinned ad / tracker snapshot, a "
              "NEVER_CONTACT host (or a government host with ANYUSE_REFUSE_GOV_HOSTS), a robots.txt-disallowed "
              "URL or a request carrying the typed value; counted as browser statics (runaway guard, run cache). ON by default with ANYUSE_THIRD_PARTY "
              "search / any; ANYUSE_THIRD_PARTY=off keeps it off.",
              security=True, parser=_positive_bool(True), confirm_when=lambda value: False),
        _spec("ANYUSE_THIRD_PARTY_POLICY", "string", "block",
              "SUPERSEDED by ANYUSE_THIRD_PARTY (no longer read): third-party content hosts are "
              "ANYUSE_THIRD_PARTY=any (allowed except ads/trackers on the built-in "
              "EasyList/EasyPrivacy domain snapshot).",
              allowed=("block", "allow_with_blocklist", "allow_all"), security=True,
              confirm_when=lambda value: value != "block"),
        _spec("ANYUSE_LOCAL_FIXTURE_ORIGINS", "string", "",
              "Comma-separated exact HTTP loopback origins allowed only for offline fixtures.",
              security=True, parser=_optional_string,
              confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_COMPILE_LEVERS", "string", "default",
              "Compiler experiments: default/all, none/off, or a comma-separated lever list.",
              parser=_compile_levers),
        _spec("ANYUSE_TIER2_READINESS", "boolean", True,
              "Browser replay: read the page once the search's own data calls for the input "
              "have answered and the result rows stopped changing, with the timer as cap and "
              "fallback. One or two results that name the input, or a page that says it found "
              "nothing, are read then too. ON by default; off = fixed timer (10 s window).",
              parser=_positive_bool(True)),
        _spec("ANYUSE_AUTO_HEAL", "boolean", True,
              "Compiled web tools: when the compiled request fails on an answer from the site, "
              "or has not answered four calls in a row over 30 minutes while the site's own "
              "page answers, and no browser replay or agent answers, re-derive the broken step "
              "(else recompile) from plain HTTP reads of the server-rendered result page, "
              "re-prove it on held-out inputs and swap it in. No browser; a configured check "
              "model (ANYUSE_MODEL_CHECKS) is asked only when the page alone cannot say which "
              "list is the answer; a failed attempt is not repeated for 30 minutes, and a tool "
              "that fails again within 10 minutes of a heal is left alone for 30 minutes. ON "
              "by default; off = fail with the reason.",
              parser=_negative_bool),
        _spec("ANYUSE_KEEP_EVIDENCE", "boolean", True,
              "Compiled web tools: keep the recordings a tool was compiled from, and the pages "
              "of the last calls the agent rung answered, in the tool's folder as recompile "
              "evidence. ON by default; off = keep nothing new.",
              parser=_negative_bool),
        _spec("ANYUSE_EMPTY_PROBE", "boolean", True,
              "Compiled and saved web tools: learn the site's own empty page from two searches "
              "for made-up inputs when a tool is proved (a compiled tool proved before: at its "
              "first empty answer, with a search for an input known to have results), and send "
              "at most three more searches on an empty answer the page alone does not settle "
              "(two more, once, when the site has changed its empty page since). "
              "ON by default; off = no such requests, and empty answers are judged by the "
              "check model and the page's statement.",
              parser=_positive_bool(True)),
        _spec("ANYUSE_CHAIN_FIT", "boolean", True,
              "Search-then-open chains with no second recorded input to fit on: fit the chain "
              "on another result's name from the first input's own search, sent to the site "
              "over plain HTTP (or the browser replay when one is configured), at most 3 "
              "values; that input never proves. ON by default; off = record another input.",
              parser=_negative_bool),
        _spec("ANYUSE_JUDGE", "string", "none",
              "Optional conservative drift judge backend.",
              allowed=("none", "jev", "llm")),
        _spec("ANYUSE_TRIAGE_RANKER", "string", "none",
              "Optional ranker consulted only for stuck-run states the triage rules "
              "leave ambiguous; a rule always validates its answer.",
              allowed=("none", "jev", "llm", "laya")),
        _spec("ANYUSE_CONFIRMER", "string", "off",
              "Second look after a deterministic verdict: off, "
              "shadow (ask Laya in the offline Apple container and log, never act) or "
              "veto (act only for kinds that passed the enable rule; never promote).",
              allowed=("off", "shadow", "veto")),
        _spec("ANYUSE_CONFIRMER_LAYA_DIR", "string", "",
              "Directory with the Laya wheelhouse and safetensors weights, mounted "
              "read-only-by-convention into the offline container as /work.",
              parser=_optional_string),
        _spec("ANYUSE_CONFIRMER_CHECKPOINT", "string", "en",
              "Laya checkpoint the confirmer asks: en or typed.",
              allowed=("en", "typed")),
        _spec("ANYUSE_CONFIRMER_BACKEND", "string", "laya",
              "Which model the confirmer asks: laya (offline Apple container) or "
              "openjev (the server at ANYUSE_OPENJEV_URL).",
              allowed=("laya", "openjev")),
        _spec("ANYUSE_REA_PICKER", "string", "auto",
              "REA saved-action chooser: auto (configured Djev, else OpenJev), djev, "
              "openjev, laya (owned offline container), explicitly hosted jev or the configured "
              "reasoning model. Choice never executes.",
              allowed=("auto", "djev", "openjev", "laya", "jev", "reasoning")),
        _spec("ANYUSE_MAP_EFFORT", "string", "default",
              "Mapping and proof effort tier: default (always record HTTP/DOM and AX workflows on normal runs with "
              "0 Frida overhead), rea-fast (enable REA native/inspector discovery with 1/1/1 fast proof trials), "
              "or rea-strict (enable REA with 3/3/3 benchmark proof trials).",
              allowed=("default", "rea-fast", "rea-strict")),
        _spec("ANYUSE_REA_AUTO_THRESHOLD", "integer", 3,
              "Vision/computer-use fallback count on the same unmapped task before AnyUse flags it for REA "
              "mapping; 0 disables automatic frequency tracking.",
              parser=_non_negative_int),
        _spec("ANYUSE_REA_GUI_CAPTURE", "string", "auto",
              "GUI fallback capture: off disables capture; windows records redacted actuation windows; "
              "live requests differential capture with an explicitly attached trusted probe. auto requests live "
              "for rea-fast/rea-strict effort, windows for fallback escalation, otherwise off. "
              "No probe is attached automatically; unavailable live capture retains redacted windows.",
              allowed=("auto", "off", "windows", "live")),
        _spec("ANYUSE_REA_NM", "string", "",
              "Optional absolute path to an existing real nm/llvm-nm Mach-O tool for REA symbol evidence; "
              "installer shims, scripts and missing tools are refused before invocation.", parser=_optional_string),
        _spec("ANYUSE_DJEV_URL", "string", "",
              "Djev /v1/request base URL for the REA chooser. Empty = unavailable.",
              security=True, parser=_webhook_url, confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_REA_LAYA_DIR", "string", "",
              "Existing offline Laya wheelhouse and models for REA typed action choice.", parser=_string),
        _spec("ANYUSE_REA_LAYA_CHECKPOINT", "string", "typed",
              "Offline Laya checkpoint for REA choice; inputs that exceed its context are refused.",
              allowed=("typed", "en", "multilingual")),
        _spec("ANYUSE_REA_LAYA_CONTAINER", "string", "/opt/homebrew/bin/container",
              "Apple container executable used for the owned offline REA Laya worker.", parser=_string),
        _spec("ANYUSE_REA_LAYA_IMAGE", "string", "python:3.12-slim",
              "Already cached Python image for the offline REA Laya worker; no image is downloaded.", parser=_string),
        _spec("ANYUSE_OPENJEV_URL", "string", "",
              "OpenJev /v1/systemone base URL (https://, or http:// to loopback). Empty = "
              "unavailable: every OpenJev question is no opinion. Page text and, if "
              "enabled, screenshots are sent here; challenge pages never are.",
              security=True, parser=_webhook_url, confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_OPENJEV_MODEL", "string", "openjev-latest",
              "OpenJev model id (openjev-latest = DiffusionGemma; laya-1.0 also works).",
              parser=_string),
        _spec("ANYUSE_OPENJEV_TIMEOUT_S", "number", 3.0,
              "Seconds before an OpenJev question counts as no opinion.",
              parser=_non_negative_float),
        _spec("ANYUSE_OPENJEV_IMAGES", "boolean", False,
              "Send the page screenshot with an OpenJev question when the caller has one.",
              security=True, parser=_positive_bool(False), confirm_when=bool),
        _spec("ANYUSE_MODEL_CHECKS", "string", "auto",
              "Which small model answers a model check (one yes/no question per candidate where "
              "the site's own evidence leaves a check open): auto = OpenJev if ANYUSE_OPENJEV_URL "
              "is set, else Laya if ANYUSE_CONFIRMER_LAYA_DIR is a directory, else Jev or the judge "
              "model when ANYUSE_JUDGE names one, else none; none = never ask.",
              allowed=("auto", "none", "openjev", "laya", "jev", "llm")),
        _spec("ANYUSE_MODEL_CHECK_TIMEOUT_S", "number", 10.0,
              "Seconds one model check may take for all its questions; past that it has no "
              "opinion. 0 = the default.",
              parser=_non_negative_float),
        _spec("ANYUSE_ROUTINE_SHADOW_RANKER", "string", "none",
              "Routines: at a hand-off with candidates, log which one a ranker would pick "
              "(shadow only; receipts and cards never change): none or openjev.",
              allowed=("none", "openjev")),
        _spec("ANYUSE_NOTIFY_CHANNEL", "string", "none",
              "Tell the owner that a card waits when no MCP client can ask them: "
              "none, imessage (outbound only) or webhook.",
              allowed=("none", "imessage", "webhook")),
        _spec("ANYUSE_NOTIFY_IMESSAGE_TO", "string", "",
              "The one phone number or e-mail address that card notices go to via Messages.",
              security=True, parser=_imessage_to, confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_NOTIFY_WEBHOOK_URL", "string", "",
              "The https URL that receives a JSON card notice (http only for loopback).",
              security=True, parser=_webhook_url, confirm_when=lambda value: bool(value)),
        _spec("ANYUSE_DRIFT_GATE_ENABLED", "boolean", True,
              "Run the non-actuating AX drift check before replay steps.",
              security=True, parser=_positive_bool(True),
              confirm_when=lambda value: not value),
        _spec("ANYUSE_DETERMINISTIC_REPLAY", "boolean", True,
              "Try recorded deterministic replay before the model planner.",
              parser=_positive_bool(True)),
        # 0.05: measured with the resolver in place
        #: no wrong action added, 85 right cases refused
        # instead of 502 on a frozen corpus
        _spec("ANYUSE_DRIFT_MISSING_TOLERANCE", "number", 0.05,
              "Fraction of required AX anchors allowed to be missing (0 through 1).",
              parser=_unit_interval),
        _spec("ANYUSE_DRIFT_EXCLUDE_MENUBAR", "boolean", False,
              "Exclude menu-bar descendants from AX drift fingerprints.",
              parser=_positive_bool(False)),
        _spec("ANYUSE_ELEMENT_MATCHER", "string", "cascade",
              "AX element matcher: exact cascade or weighted scorer.",
              allowed=("cascade", "weighted")),
        _spec("ANYUSE_ATTACH_TTL_DAYS", "number", 7.0,
              "Days to retain attach-session records.", parser=_ttl_days),
        _spec("ANYUSE_LEARN_CANDIDATE_TTL_HOURS", "number", 24.0,
              "Hours to retain unconfirmed learn candidates.", parser=_candidate_ttl_hours),
    )
}

# Model role overrides are a regular family in the same registry. Empty means
# "inherit the corresponding global setting/preset", matching the adapter's
# historical environment semantics.
for _role in ("PLANNER", "VISUAL", "AGENT", "JUDGE", "AGENT_L2", "AGENT_L3"):
    for _suffix, _type, _description, _parser, _security in (
        ("PROVIDER", "string", "provider preset", _model_provider, False),
        ("BASE_URL", "string", "OpenAI-compatible base URL", _optional_string, True),
        ("NAME", "string", "model name", _optional_string, False),
        ("API_KEY_ENV", "string", "credential environment-variable name", _optional_string, True),
        ("RETRIES", "integer", "extra retry count", _optional_retries, False),
        ("PRICE_IN", "number", "input-token list price per million", _optional_non_negative_float, False),
        ("PRICE_OUT", "number", "output-token list price per million", _optional_non_negative_float, False),
    ):
        _name = f"ANYUSE_{_role}_MODEL_{_suffix}"
        SETTINGS[_name] = _spec(
            _name, _type, "",
            f"Optional {_role.lower()} role {_description}; empty inherits global/preset.",
            security=_security, parser=_parser,
            confirm_when=(lambda value: bool(value)) if _security else None,
        )


#: Shown by ``anyuse settings`` and in the terminal confirmation when the
#: effective value of a setting widens what AnyUse may do unattended.
def security_warning(name: str, value: Any) -> str:
    if name == "ANYUSE_SPEND_DAILY_USD" and value == UNLIMITED:
        return ("WARNING: no daily spend cap. Model requests are still booked in the spend "
                "ledger; the per-run budget (ANYUSE_RUN_BUDGET_USD) and the per-feature caps "
                "still apply.")
    if name == "ANYUSE_RUN_BUDGET_USD" and value == UNLIMITED:
        return ("WARNING: one run may spend without limit. The daily cap "
                "(ANYUSE_SPEND_DAILY_USD) still applies in the anyuse CLI and the MCP server.")
    if name == "ANYUSE_LIVE_SOLVE_ORIGINS" and value != "user_task":
        return ("WARNING: live solving is enabled for " + str(value) + ". Unattended "
                "(scheduled/bulk) runs will then stop on a challenge, notify you and wait "
                "for YOU to solve it; a solved session still serves only the one task that "
                "asked. bench runs are never allowed.")
    if name == "ANYUSE_CHALLENGE_POLICY" and value == "human_solves_live":
        return ("WARNING: human_solves_live lets a person solve a site's human check live "
                "(their own browser, or a loopback-only view AnyUse relays pointer and keys "
                "to). No model sees the challenge. Applies only to the origins in "
                "ANYUSE_LIVE_SOLVE_ORIGINS.")
    if name == "ANYUSE_THIRD_PARTY" and value == "any":
        return ("WARNING: ANYUSE_THIRD_PARTY=any lets the guarded browser send the page's own requests to ANY "
                "third-party host it uses for its content (APIs, widgets, data CDNs) - except hosts on the built-in "
                "ad/tracker snapshot, which may miss some. Requests stay read-only as the page sends them, "
                "robots.txt-checked, paced and inside the site's budget; NEVER_CONTACT hosts stay refused "
                "(and government hosts with ANYUSE_REFUSE_GOV_HOSTS). 'search' (the default) allows only the "
                "site's own search provider.")
    if name == "ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS" and value:
        return "NOTE: ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS is superseded by ANYUSE_THIRD_PARTY and no longer read."
    if name == "ANYUSE_THIRD_PARTY_POLICY" and value == "allow_all":
        return ("WARNING (superseded by ANYUSE_THIRD_PARTY, no longer read): allow_all lets pages load ANY third-party host (ads, trackers, "
                "analytics, social widgets included) in the guarded browser. Requests stay "
                "GET/HEAD, robots.txt-checked and inside the site's budget, but nothing is "
                "filtered. allow_with_blocklist is the narrower choice.")
    if name == "ANYUSE_THIRD_PARTY_POLICY" and value == "allow_with_blocklist":
        return ("WARNING: third-party hosts are allowed in the guarded browser except those on "
                "the built-in ad/tracker domain snapshot; hosts the snapshot misses are sent.")
    return ""


def normalize_name(name: str) -> str:
    normalized = str(name).strip().upper().replace("-", "_")
    if not normalized.startswith("ANYUSE_"):
        normalized = "ANYUSE_" + normalized
    if normalized not in SETTINGS:
        raise SettingError(f"unknown setting {name!r}")
    return normalized


def settings_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    if path is not None:
        return Path(path)
    from anyuse.paths import anyuse_data_path
    return anyuse_data_path(SETTINGS_FILE)


def load_file(path: Optional[os.PathLike[str] | str] = None, *,
              strict: bool = False) -> dict[str, Any]:
    target = settings_path(path)
    try:
        with target.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if strict:
            raise SettingError(f"cannot read {target}: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise SettingError(f"{target} must contain a top-level TOML table")
        return {}
    return data


def validate_file(path: Optional[os.PathLike[str] | str] = None) -> dict[str, Any]:
    values = load_file(path, strict=True)
    unknown = sorted(set(values) - set(SETTINGS))
    if unknown:
        raise SettingError("unknown setting(s) in file: " + ", ".join(unknown))
    parsed: dict[str, Any] = {}
    for name, value in values.items():
        try:
            parsed[name] = SETTINGS[name].parse(value, strict=True)
        except SettingError as exc:
            raise SettingError(f"{name}: {exc}") from exc
    return parsed


def resolve(name: str, explicit: Any = UNSET, *,
            env: Optional[Mapping[str, str]] = None,
            path: Optional[os.PathLike[str] | str] = None) -> ResolvedSetting:
    canonical = normalize_name(name)
    spec = SETTINGS[canonical]
    if explicit is not UNSET and explicit is not None:
        try:
            return ResolvedSetting(spec, spec.parse(explicit, strict=False), "explicit")
        except SettingError:
            return ResolvedSetting(spec, spec.default, "explicit")
    environ = os.environ if env is None else env
    if canonical in environ:
        try:
            value = spec.parse(environ[canonical], strict=False)
        except SettingError as exc:
            # Preserve settings whose old readers raised for bad values (budget
            # and retry count) while permissive parsers handle their own legacy
            # fallback rules.
            raise SettingError(f"{canonical}: {exc}") from exc
        return ResolvedSetting(spec, value, "env")
    raw_file = load_file(path)
    if canonical in raw_file:
        try:
            return ResolvedSetting(spec, spec.parse(raw_file[canonical], strict=True), "file")
        except SettingError:
            return ResolvedSetting(spec, spec.default, "default")
    return ResolvedSetting(spec, spec.default, "default")


def env_value_problem(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Why the environment value of a setting with a fixed set of values is not used, or "" when it is
    (or is unset): :func:`resolve` reads a value outside the set as the default, without an error."""
    canonical = normalize_name(name)
    spec = SETTINGS[canonical]
    environ = os.environ if env is None else env
    if canonical not in environ or spec.parser is not None or not spec.allowed_values:
        return ""
    raw = str(environ[canonical]).strip()
    if raw.lower() in {str(item).lower() for item in spec.allowed_values}:
        return ""
    return (f"{canonical}={raw!r} is not one of {', '.join(map(str, spec.allowed_values))}; "
            f"{spec.default!r} is used instead")


def get(name: str, explicit: Any = UNSET, *,
        env: Optional[Mapping[str, str]] = None,
        path: Optional[os.PathLike[str] | str] = None) -> Any:
    return resolve(name, explicit, env=env, path=path).value


def parse_for_storage(name: str, value: Any) -> tuple[str, Any]:
    canonical = normalize_name(name)
    spec = SETTINGS[canonical]
    try:
        return canonical, spec.parse(value, strict=True)
    except SettingError as exc:
        raise SettingError(f"{canonical}: {exc}") from exc


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SettingError("cannot write a non-finite number")
        return repr(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def write_file(values: Mapping[str, Any],
               path: Optional[os.PathLike[str] | str] = None) -> Path:
    target = settings_path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = ["# AnyUse behavior settings. Managed by `anyuse settings`.\n"]
    for name in sorted(values):
        canonical, parsed = parse_for_storage(name, values[name])
        lines.append(f"{canonical} = {_toml_value(parsed)}\n")
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def set_value(name: str, value: Any,
              path: Optional[os.PathLike[str] | str] = None) -> tuple[Path, Any]:
    canonical, parsed = parse_for_storage(name, value)
    values = validate_file(path)
    values[canonical] = parsed
    return write_file(values, path), parsed


def reset_value(name: str,
                path: Optional[os.PathLike[str] | str] = None) -> tuple[Path, bool]:
    canonical = normalize_name(name)
    values = validate_file(path)
    removed = canonical in values
    values.pop(canonical, None)
    return write_file(values, path), removed


def display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value == "":
        return "(unset)"
    return str(value)
