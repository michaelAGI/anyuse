"""One provider-agnostic boundary for OpenAI-compatible chat completions.

Providers are configuration, not implementations.  Every HTTP provider
receives the same ``POST <base_url>/chat/completions`` request and returns the
same :class:`ModelResult`.  This module deliberately depends on httpx only; no
provider SDK is imported or required. The ``claude-cli`` and ``codex-cli``
presets instead run the CLI the user is logged into (no key) and return the
same :class:`ModelResult`.

Credentials are resolved at *call time*.  ``api_key_env`` names an environment
variable; if it is absent, ``ANYUSE_MODEL_ENV_FILE`` (or ``model.env`` in the
AnyUse secrets directory) is read as ``NAME=value`` lines.  The value is never
put in a subprocess environment, command line, log message, exception, or
result object.

Spend: every request first reserves its input envelope plus maximum output tokens
in the adapter's :class:`RunBudget` and, in a process a product entry point
armed, under the global daily cap of ``execution/spend.py``. After the request
the run budget is settled with what it cost (the provider's figure, else tokens
x list price, else the reservation's maximum) and the same amount is booked as
one row of the spend ledger, labelled with the adapter's ``feature``.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from anyuse.execution import spend
from anyuse.settings import SETTINGS, UNLIMITED, get as get_setting, resolve as resolve_setting


USER_AGENT = "anyuse-model-adapter/0.4.0 (OpenAI-compatible; python-httpx)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: retry policy for throttling and provider-side failures (HTTP 408/429/5xx):
#: the first wait, the doubling cap (also the largest ``Retry-After`` honoured)
#: and the most extra attempts a configuration may ask for. HTTP 402, other
#: 4xx and transport errors are never retried.
RETRY_BASE_DELAY_S = 1.0
RETRY_MAX_DELAY_S = 8.0
MAX_RETRIES = 5
RUN_BUDGET_ENV = "ANYUSE_RUN_BUDGET_USD"
#: one Sonnet-class decision costs about US$0.03, so a smaller default stops a multi-step agent or
#: computer-use run after its first decision; the daily cap (ANYUSE_SPEND_DAILY_USD) still applies
DEFAULT_RUN_BUDGET_USD = 1.0

# ---------------------------------------------------------------------------
# The ONE model-call counter of the process. Every request this adapter sends is counted
# here, once, at the moment it is sent. A model reached by another path (the
# Claude CLI the desktop escalation and the agent harness run, a legacy SDK
# client) reports to the same counter through ``note_external_model_call`` at
# ITS one chokepoint. A run reports its model calls as the delta of this
# counter, never as a sum of per-rung self-reports.
# ---------------------------------------------------------------------------
_PROCESS_LOCK = threading.Lock()
_PROCESS_CALLS = 0
_EXTERNAL_CALLS: List[Dict[str, Any]] = []


def process_call_count() -> int:
    """Model calls made by this process so far (adapter requests + external calls)."""
    with _PROCESS_LOCK:
        return _PROCESS_CALLS


def _count_process_call() -> int:
    global _PROCESS_CALLS
    with _PROCESS_LOCK:
        _PROCESS_CALLS += 1
        return _PROCESS_CALLS


def note_external_model_call(provider: str, model: str, feature: str = "",
                             cost_usd: Optional[float] = None) -> int:
    """Count a model call made outside this adapter (a CLI, a legacy SDK client)
    at its single chokepoint. Returns the process count after it."""
    with _PROCESS_LOCK:
        if len(_EXTERNAL_CALLS) < 1000:
            _EXTERNAL_CALLS.append({"provider": str(provider), "model": str(model),
                                    "feature": str(feature), "cost_usd": cost_usd})
    return _count_process_call()


def calls_since(mark: int) -> int:
    """The model calls made since ``mark`` (a value of :func:`process_call_count`)."""
    return max(0, process_call_count() - int(mark))

# Published per-million-token prices used by the configured role defaults.
# Explicit ModelConfig instances (notably local test servers) default to zero.
_ROLE_PRICES = {
    "agent": (0.25, 0.69),
    "judge": (0.25, 0.69),
    "agent_l2": (2.0, 10.0),
    "agent_l3": (0.75, 3.75),
    "planner": (0.30, 2.50),
    "visual": (0.30, 2.50),
}


@dataclass
class BudgetReservation:
    maximum_usd: float
    settled: bool = False


class RunBudget:
    """Thread-safe reserve-before-send dollar budget shared by one run.

    ``limit_usd`` may be ``math.inf`` (``ANYUSE_RUN_BUDGET_USD=unlimited``): every
    reservation then fits, ``remaining_usd`` is ``inf`` and :meth:`to_dict` reports
    ``None`` with ``unlimited: true`` (JSON has no infinity)."""

    def __init__(self, limit_usd: float = DEFAULT_RUN_BUDGET_USD):
        limit = spend.cap_from_value(limit_usd)
        if math.isnan(limit) or limit < 0:
            raise ValueError("run budget must be a non-negative amount or unlimited")
        self.limit_usd = limit
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self.refused_requests = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "RunBudget":
        try:
            value = get_setting(RUN_BUDGET_ENV)
        except ValueError as exc:
            raise ValueError(f"{RUN_BUDGET_ENV} must be a non-negative dollar amount "
                             f"or {UNLIMITED}") from exc
        return cls(value)

    def reserve(self, max_tokens: int, price_in: float,
                price_out: float, *, input_tokens: Optional[int] = None) -> Optional[BudgetReservation]:
        tokens = int(max_tokens)
        prices = (float(price_in), float(price_out))
        if tokens < 0 or any(not math.isfinite(price) or price < 0 for price in prices):
            raise ValueError("budget reservation inputs must be finite and non-negative")
        inputs = tokens if input_tokens is None else int(input_tokens)
        if inputs < 0:
            raise ValueError("input token bound must be non-negative")
        maximum = (inputs * prices[0] + tokens * prices[1]) / 1_000_000
        with self._lock:
            if self.spent_usd + self.reserved_usd + maximum > self.limit_usd + 1e-12:
                self.refused_requests += 1
                return None
            reservation = BudgetReservation(maximum)
            self.reserved_usd += maximum
            return reservation

    def settle(self, reservation: BudgetReservation, actual_usd: Optional[float]) -> None:
        with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            self.reserved_usd = max(0.0, self.reserved_usd - reservation.maximum_usd)
            # No trustworthy usage (transport/HTTP/parser failure) keeps the
            # conservative maximum: the provider may still have billed it.
            actual = float(actual_usd) if actual_usd is not None else None
            if actual is None or not math.isfinite(actual) or actual < 0:
                actual = reservation.maximum_usd
            self.spent_usd += actual

    @property
    def remaining_usd(self) -> float:
        with self._lock:
            return max(0.0, self.limit_usd - self.spent_usd - self.reserved_usd)

    def to_dict(self) -> dict:
        with self._lock:
            out = {"limit_usd": self.limit_usd, "spent_usd": self.spent_usd,
                   "reserved_usd": self.reserved_usd,
                   "remaining_usd": max(0.0, self.limit_usd - self.spent_usd
                                        - self.reserved_usd),
                   "refused_requests": self.refused_requests}
        if math.isinf(self.limit_usd):
            out.update(limit_usd=None, remaining_usd=None, unlimited=True)
        return out


@dataclass(frozen=True)
class ProviderPreset:
    base_url: str
    model: str
    api_key_env: str


PROVIDER_PRESETS: Dict[str, ProviderPreset] = {
    "cerebras": ProviderPreset(
        "https://api.cerebras.ai/v1", "gpt-oss-120b", "CEREBRAS_API_KEY"
    ),
    "openrouter": ProviderPreset(
        "https://openrouter.ai/api/v1", "openai/gpt-oss-120b", "OPENROUTER_API_KEY"
    ),
    "local": ProviderPreset(
        "http://127.0.0.1:8000/v1", "local-model", ""
    ),
    "gemini": ProviderPreset(
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-flash", "GEMINI_API_KEY",
    ),
    # OpenAI has no default here: name the model your key can use (ANYUSE_MODEL_NAME)
    "openai": ProviderPreset("https://api.openai.com/v1", "", "OPENAI_API_KEY"),
    # Anthropic's OpenAI-compatible endpoint (it ignores response_format; answers are still checked as JSON)
    "anthropic": ProviderPreset("https://api.anthropic.com/v1/", "claude-sonnet-5-5", "ANTHROPIC_API_KEY"),
    # a CLI the user is already logged into; no key, no base URL. Codex has no
    # default here: it runs with --ignore-user-config, which skips the user's
    # default model, so name one with ANYUSE_MODEL_NAME (doctor says so).
    "claude-cli": ProviderPreset("", "sonnet", ""),
    "codex-cli": ProviderPreset("", "", ""),
}
CLI_PROVIDERS = frozenset({"claude-cli", "codex-cli"})
#: one CLI call may take longer than an HTTP request (the CLI starts, then thinks)
CLI_TIMEOUT_S = 300.0

# Preserve the pre-adapter role defaults until the operator selects one global
# provider.  Setting ANYUSE_MODEL_PROVIDER makes all four roles use that preset;
# role-specific environment variables can then split them again.
_ROLE_DEFAULTS = {
    "planner": ("gemini", "gemini-3.5-flash"),
    "visual": ("gemini", "gemini-2.5-computer-use-preview-10-2025"),
    "agent": ("cerebras", "gpt-oss-120b"),
    "judge": ("cerebras", "gpt-oss-120b"),
    # the agent tier's escalation levels; used only when
    # ANYUSE_AGENT_ESCALATION=on. L1 is the plain ``agent`` role above.
    "agent_l2": ("openrouter", "anthropic/claude-sonnet-5"),
    "agent_l3": ("openrouter", "google/gemini-3.8-flash"),
}


@dataclass(frozen=True)
class ModelConfig:
    """Everything needed for one OpenAI-compatible chat-completions role."""

    provider: str
    base_url: str
    model: str
    api_key_env: str
    timeout: float = 90.0
    max_tokens: int = 2000
    user_agent: str = USER_AGENT
    #: extra attempts after a 408/429/5xx before the call is reported as
    #: ``provider_unreachable`` (``ANYUSE_MODEL_RETRIES``; 0 = single shot)
    retries: int = 2
    price_in: float = 0.0
    price_out: float = 0.0
    # Explicit compatibility mode for servers whose schema grammar compiler
    # rejects an otherwise supported schema. Callers still validate decisions.
    structured_output_mode: str = "json_schema"
    # Optional provider-defined HTTP reasoning control. None preserves the
    # endpoint's default and emits no field; "none" is an explicit wire value.
    reasoning_effort: Optional[str] = None

    def __post_init__(self) -> None:
        effort = self.reasoning_effort
        if effort is None:
            return
        if (not isinstance(effort, str) or not 1 <= len(effort) <= 64
                or effort.strip() != effort or not effort.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in effort)):
            raise ValueError("model reasoning effort must be a nonempty string of at most 64 characters, "
                             "without surrounding whitespace or control characters")
        if self.provider in CLI_PROVIDERS:
            raise ValueError("explicit reasoning effort applies to HTTP providers, not CLI providers")

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: what the provider says the call cost, when it says (OpenRouter's
    #: ``usage.cost``); 0.0 means "not reported", not "free"
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class ModelResult:
    """Typed outcome of one adapter invocation, successful or otherwise."""

    ok: bool = False
    text: str = ""
    parsed: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""
    provider_unreachable: bool = False
    error: str = ""
    status_code: Optional[int] = None
    #: requests actually sent for this answer, the adapter's own retries included
    llm_calls: int = 0
    #: HTTP 200 with no answer text (e.g. a reasoning model spent the whole
    #: ``max_tokens`` budget on hidden reasoning). Not an outage by itself;
    #: callers decide (the agent tier treats it like ``provider_unreachable``).
    empty_answer: bool = False
    #: the provider's ``finish_reason`` for the first choice, when it gave one
    finish_reason: str = ""
    budget_exhausted: bool = False
    #: why a sent request's spend-ledger row could not be written (armed processes);
    #: the answer itself is unaffected
    spend_booking_error: str = ""

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "text": self.text,
            "parsed": self.parsed,
            "usage": self.usage.to_dict(),
            "latency_ms": round(self.latency_ms, 1),
            "provider": self.provider,
            "model": self.model,
            "provider_unreachable": self.provider_unreachable,
            "error": self.error,
            "status_code": self.status_code,
            "llm_calls": self.llm_calls,
            "empty_answer": self.empty_answer,
            "finish_reason": self.finish_reason,
            "budget_exhausted": self.budget_exhausted,
        }
        if self.spend_booking_error:
            out["spend_booking_error"] = self.spend_booking_error
        return out


def _env(role: str, suffix: str) -> str:
    """Role override, then global value, while env always beats the file."""
    role_name = f"ANYUSE_{role.upper()}_MODEL_{suffix}"
    global_name = f"ANYUSE_MODEL_{suffix}"

    # Keep the adapter's established role-env > global-env hierarchy. An empty
    # role environment value remains a real override.
    if role_name in os.environ and role_name in SETTINGS:
        return str(get_setting(role_name)).strip()
    if role_name in os.environ:
        return os.environ[role_name].strip()
    if global_name in os.environ and global_name in SETTINGS:
        return str(get_setting(global_name)).strip()
    if global_name in os.environ:
        return os.environ[global_name].strip()

    # With no environment override, a role-specific file value precedes the
    # global file value. Empty means inherit, as documented by the registry.
    if role_name in SETTINGS:
        role_value = resolve_setting(role_name, env={})
        if role_value.source == "file" and str(role_value.value).strip():
            return str(role_value.value).strip()
    if global_name in SETTINGS:
        return str(resolve_setting(global_name, env={}).value).strip()
    return os.environ.get(global_name, "").strip()


def resolve_model_config(role: str, **overrides: Any) -> ModelConfig:
    """Resolve one role with explicit > role env > global env > preset precedence."""
    role = role.strip().lower()
    default_provider, default_model = _ROLE_DEFAULTS.get(role, ("cerebras", "gpt-oss-120b"))
    provider = str(overrides.get("provider") or _env(role, "PROVIDER") or default_provider).lower()
    if provider not in PROVIDER_PRESETS:
        raise ValueError(
            f"unknown model provider {provider!r}; choose "
            + ", ".join(sorted(PROVIDER_PRESETS))
        )
    preset = PROVIDER_PRESETS[provider]
    provider_was_selected = bool(overrides.get("provider") or _env(role, "PROVIDER"))
    model_default = preset.model if provider_was_selected else default_model
    price_in, price_out = _ROLE_PRICES.get(role, (0.25, 0.69))
    values = {
        "provider": provider,
        "base_url": str(overrides.get("base_url") or _env(role, "BASE_URL") or preset.base_url),
        "model": str(overrides.get("model") or _env(role, "NAME") or model_default),
        "api_key_env": str(
            overrides["api_key_env"] if "api_key_env" in overrides
            else (_env(role, "API_KEY_ENV") or preset.api_key_env)
        ),
        "timeout": float(overrides.get("timeout", 90.0)),
        "max_tokens": int(overrides.get("max_tokens", 2000)),
        "user_agent": str(overrides.get("user_agent") or USER_AGENT),
        "retries": int(overrides["retries"] if "retries" in overrides
                       else (_env(role, "RETRIES") or 2)),
        "price_in": float(overrides.get("price_in", _env(role, "PRICE_IN") or price_in)),
        "price_out": float(overrides.get("price_out", _env(role, "PRICE_OUT") or price_out)),
        "structured_output_mode": str(overrides.get("structured_output_mode") or
                                      _env(role, "STRUCTURED_OUTPUT_MODE") or "json_schema"),
        "reasoning_effort": overrides.get("reasoning_effort"),
    }
    cli = provider in CLI_PROVIDERS
    if values["structured_output_mode"] not in {"json_schema", "json_object"}:
        raise ValueError("structured output mode must be json_schema or json_object")
    if cli and values["structured_output_mode"] != "json_schema":
        raise ValueError("structured output compatibility mode applies to HTTP providers")
    if not cli and not values["base_url"].startswith(("http://", "https://")):
        raise ValueError("model base_url must be an http(s) URL")
    if not values["model"] and provider != "codex-cli":
        raise ValueError(f"the {provider} preset has no default model: set ANYUSE_MODEL_NAME"
                         if provider == "openai" else "model name is empty")
    if not 0 < values["timeout"] <= 300:
        raise ValueError("model timeout must be in (0, 300] seconds")
    if not 0 < values["max_tokens"] <= 100_000:
        raise ValueError("model max_tokens must be in (0, 100000]")
    if not values["user_agent"].isascii():
        raise ValueError("model User-Agent must be ASCII")
    if not 0 <= values["retries"] <= MAX_RETRIES:
        raise ValueError(f"model retries must be in [0, {MAX_RETRIES}]")
    if any(not math.isfinite(value) or value < 0
           for value in (values["price_in"], values["price_out"])):
        raise ValueError("model list prices must be finite and non-negative")
    return ModelConfig(**values)


#: what a command that needs a model says when none is configured: `anyuse "task"`, `workflow record`,
#: `interactive`, `agent run` and the MCP agent tool
MODEL_SETUP_HINT = """No model is configured for agent tasks. Either use a CLI you are logged into:
  export ANYUSE_MODEL_PROVIDER=claude-cli        # or codex-cli
  (to keep it: anyuse settings set ANYUSE_MODEL_PROVIDER claude-cli)
or a key you have: ANYUSE_MODEL_PROVIDER=openai (with ANYUSE_MODEL_NAME), anthropic, gemini or openrouter
(each reads OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY),
or point AnyUse at any OpenAI-compatible endpoint:
  export ANYUSE_MODEL_PROVIDER=local ANYUSE_MODEL_BASE_URL=http://127.0.0.1:8000/v1 ANYUSE_MODEL_NAME=<model>
See docs/MODEL_CONFIGURATION.md. Compiled replays (anyuse workflow play) need no model."""


def provider_configured(role: str) -> bool:
    """Did the user choose a provider or a base URL for ``role`` (its own or the global one), in the
    environment or in settings.toml? Read the way :func:`resolve_model_config` reads them."""
    return bool(_env(role, "PROVIDER") or _env(role, "BASE_URL"))


def role_problem(role: str) -> str:
    """Why ``role`` cannot call a model now, or "" when it can: nothing configured (:data:`MODEL_SETUP_HINT`),
    an unusable configuration, a CLI preset whose command is not on PATH, or an unset credential.
    Reads the credential in-process and says only whether it is set."""
    import shutil
    try:
        cfg = resolve_model_config(role)
    except ValueError as exc:
        return f"the {role} model configuration cannot be used: {exc} (docs/MODEL_CONFIGURATION.md)"
    if cfg.provider in CLI_PROVIDERS:
        binary = "claude" if cfg.provider == "claude-cli" else "codex"
        if shutil.which(binary):
            return ""
        return (f"the model provider is {cfg.provider}, but the `{binary}` command is not on PATH: "
                f"install it and log in, or choose another provider (docs/MODEL_CONFIGURATION.md)")
    if credential_status(cfg) != "unset":
        return ""
    if not provider_configured(role):
        return MODEL_SETUP_HINT
    return (f"the {role} model ({cfg.provider}) reads its key from {cfg.api_key_env}, which is not set in the "
            f"environment or in {model_env_file_path()} (docs/MODEL_CONFIGURATION.md)")


def model_env_file_path() -> str:
    explicit = os.environ.get("ANYUSE_MODEL_ENV_FILE", "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    from anyuse.paths import anyuse_secret_path
    return str(anyuse_secret_path("model.env"))


def _key_from_file(name: str, path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if not line.startswith(name + "="):
                    continue
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def read_model_api_key(name: str) -> str:
    """Read a named credential in-process at call time; never expose its value."""
    if not name:
        return ""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = _key_from_file(name, model_env_file_path()).strip()
    if value:
        return value
    raise RuntimeError(
        f"model credential {name} is unset (environment and configured model env file)"
    )


def credential_status(config: ModelConfig) -> str:
    """Return ``set``/``unset``/``not required`` without returning the secret."""
    if not config.api_key_env:
        return "not required"
    if os.environ.get(config.api_key_env, "").strip():
        return "set"
    return "set" if _key_from_file(config.api_key_env, model_env_file_path()).strip() else "unset"


def image_data_url(data: bytes, mime_type: str = "image/png") -> str:
    import base64
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


class ModelAdapter:
    """Synchronous/async OpenAI-compatible chat-completions adapter."""

    def __init__(self, config: ModelConfig, transport=None, sleeper=None,
                 budget: Optional[RunBudget] = None, feature: str = "", cli_runner=None):
        if config.structured_output_mode not in {"json_schema", "json_object"}:
            raise ValueError("structured output mode must be json_schema or json_object")
        if config.provider in CLI_PROVIDERS and config.structured_output_mode != "json_schema":
            raise ValueError("structured output compatibility mode applies to HTTP providers")
        self.config = config
        #: ``runner(argv, stdin_text, cwd, timeout) -> (rc, stdout, stderr)`` for the CLI
        #: providers; raises OSError when the process could not start (tests inject one)
        self._cli_runner = cli_runner or _run_cli
        #: the spend ledger's label for this adapter's requests (``for_role``: the role)
        self.feature = str(feature or spend.UNLABELLED)
        self._transport = transport
        self._client = None
        self._calls = 0
        self._lock = threading.Lock()
        #: how a retry waits (injected by tests; ``time.sleep`` otherwise)
        self._sleep = sleeper or time.sleep
        self.budget = budget or RunBudget.from_env()
        self.price_in = float(config.price_in)
        self.price_out = float(config.price_out)

    @classmethod
    def for_role(cls, role: str, transport=None, sleeper=None,
                 budget: Optional[RunBudget] = None, feature: str = "", cli_runner=None,
                 **overrides: Any) -> "ModelAdapter":
        return cls(resolve_model_config(role, **overrides), transport=transport,
                   sleeper=sleeper, budget=budget,
                   feature=feature or role.strip().lower(), cli_runner=cli_runner)

    def set_budget(self, budget: RunBudget, *, price_in: Optional[float] = None,
                   price_out: Optional[float] = None) -> None:
        """Attach a shared run budget before the run sends its first request."""
        self.budget = budget
        if price_in is not None:
            self.price_in = float(price_in)
        if price_out is not None:
            self.price_out = float(price_out)

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._calls

    def _note_call(self) -> None:
        with self._lock:
            self._calls += 1
        _count_process_call()

    def _get_client(self):
        if self._client is None:
            import httpx
            transport = self._transport
            if transport is None:
                # This is the same IPv4-forced transport used by the compiled
                # request rung. No provider SDK or ambient proxy client exists.
                from anyuse.execution.api_step import import_module_httpx_transport
                transport = import_module_httpx_transport()
            self._client = httpx.Client(
                timeout=self.config.timeout,
                follow_redirects=False,
                transport=transport,
                headers={"User-Agent": self.config.user_agent},
            )
        return self._client

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        json_schema: Optional[Mapping[str, Any]] = None,
        schema_name: str = "response",
        images: Sequence[str] = (),
    ) -> ModelResult:
        """Make one request. Missing credentials refuse before a call is counted."""
        booking_errors: List[str] = []
        result = self._complete(messages, temperature, max_tokens, json_schema, schema_name,
                                images, booking_errors)
        if booking_errors:
            result.spend_booking_error = "; ".join(booking_errors)
        return result

    def _cost(self, usage: TokenUsage) -> Tuple[Optional[float], str]:
        """What a request cost: the provider's figure, else tokens x list price, else unknown."""
        if usage.cost_usd:
            return usage.cost_usd, "provider"
        if usage.prompt_tokens or usage.completion_tokens:
            return ((usage.prompt_tokens * self.price_in
                     + usage.completion_tokens * self.price_out) / 1_000_000,
                    "tokens x list price")
        return None, ""

    def _settle(self, reservation: BudgetReservation, ticket: Optional["spend.Ticket"],
                booking_errors: List[str], actual: Optional[float] = None, basis: str = "",
                usage: Optional[TokenUsage] = None) -> None:
        """Settle the run budget and book the same amount as one spend-ledger row.

        No trustworthy cost (transport/HTTP/parser failure, no usage) keeps the
        reservation's maximum: the provider may still have billed it."""
        if actual is None or not math.isfinite(actual) or actual < 0:
            actual, basis = reservation.maximum_usd, "upper bound (no usage reported)"
        self.budget.settle(reservation, actual)
        if ticket is None:
            return
        usage = usage or TokenUsage()
        why = spend.book(ticket, provider=self.config.provider, model=self.config.model,
                         prompt_tokens=usage.prompt_tokens,
                         completion_tokens=usage.completion_tokens,
                         cost_usd=actual, cost_basis=basis)
        if why:
            booking_errors.append(why)

    @staticmethod
    def _input_token_bound(messages, response_format=None, *, minimum=0):
        """Conservative UTF-8 envelope, including schemas and image allowances.

        Count bytes rather than a language-dependent characters/token ratio.
        Images include their encoded payload plus an allowance for small images
        whose vision token count can exceed their compressed byte length.
        """
        raw = json.dumps({"messages": messages, "response_format": response_format},
                         ensure_ascii=False).encode("utf-8")
        images = sum(1 for message in messages for part in
                     (message.get("content") if isinstance(message.get("content"), list) else [])
                     if isinstance(part, dict) and part.get("type") == "image_url")
        return max(int(minimum), len(raw) + 128 * (len(messages) + 1) + images * 8192)

    def _complete(self, messages: Sequence[Mapping[str, Any]], temperature: float,
                  max_tokens: Optional[int], json_schema: Optional[Mapping[str, Any]],
                  schema_name: str, images: Sequence[str],
                  booking_errors: List[str]) -> ModelResult:
        cfg = self.config
        if cfg.provider in CLI_PROVIDERS:
            return self._complete_cli(messages, max_tokens, json_schema, images, booking_errors)
        try:
            key = read_model_api_key(cfg.api_key_env)
        except RuntimeError as exc:
            return ModelResult(
                provider=cfg.provider, model=cfg.model, error=str(exc), llm_calls=0
            )

        payload_messages = [dict(message) for message in messages]
        if images:
            self._attach_images(payload_messages, images)
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": payload_messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens or cfg.max_tokens),
        }
        if cfg.reasoning_effort is not None:
            payload["reasoning_effort"] = cfg.reasoning_effort
        if cfg.provider == "openrouter":
            # ask for the provider-side cost in ``usage.cost`` (a number, no
            # identifiers); budgets are then kept in dollars, not estimates
            payload["usage"] = {"include": True}
        if json_schema is not None:
            if cfg.structured_output_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            else:
                safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", schema_name)[:64] or "response"
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": safe_name, "strict": True, "schema": dict(json_schema)},
                }

        headers = {"Content-Type": "application/json"}
        if cfg.api_key_env:
            headers["Authorization"] = f"Bearer {key}"
        attempts = 0
        delay = RETRY_BASE_DELAY_S
        started = time.perf_counter()
        reservation: Optional[BudgetReservation] = None
        ticket: Optional[spend.Ticket] = None
        try:
            while True:
                reservation = self.budget.reserve(
                    payload["max_tokens"], self.price_in, self.price_out,
                    input_tokens=self._input_token_bound(payload_messages, payload.get("response_format"),
                                                         minimum=payload["max_tokens"]))
                if reservation is None:
                    return ModelResult(
                        provider=cfg.provider, model=cfg.model,
                        error=(f"run budget exhausted before provider request "
                               f"({self.budget.remaining_usd:.6f} USD remaining)"),
                        llm_calls=attempts, budget_exhausted=True)
                # the global daily cap (armed processes only; unarmed: no ticket, no read)
                ticket, refusal = spend.reserve(reservation.maximum_usd, self.feature)
                if refusal:
                    self.budget.settle(reservation, 0.0)
                    reservation = None
                    return ModelResult(
                        provider=cfg.provider, model=cfg.model, error=refusal,
                        llm_calls=attempts, budget_exhausted=True)
                attempts += 1
                self._note_call()
                started = time.perf_counter()
                response = self._get_client().post(
                    cfg.endpoint, json=payload, headers=headers, timeout=cfg.timeout
                )
                latency = (time.perf_counter() - started) * 1000.0
                status = int(response.status_code)
                if status == 200:
                    break
                self._settle(reservation, ticket, booking_errors)
                reservation, ticket = None, None
                if (status in (408, 429) or status >= 500) and attempts <= cfg.retries:
                    # throttled, or the provider's side failed: wait and ask
                    # again, honouring a numeric Retry-After up to the cap
                    wait = _retry_after(response.headers)
                    if wait is None:
                        wait, delay = delay, min(delay * 2, RETRY_MAX_DELAY_S)
                    self._sleep(wait)
                    continue
                return ModelResult(
                    provider=cfg.provider,
                    model=cfg.model,
                    latency_ms=latency,
                    status_code=status,
                    provider_unreachable=status in (402, 408, 429) or status >= 500,
                    error=f"provider returned HTTP {status}"
                          + (f" ({attempts} attempts)" if attempts > 1 else ""),
                    llm_calls=attempts,
                )
            content = response.content
            if len(content) > MAX_RESPONSE_BYTES:
                self._settle(reservation, ticket, booking_errors)
                reservation, ticket = None, None
                return ModelResult(
                    provider=cfg.provider, model=cfg.model, latency_ms=latency,
                    error="provider response exceeded local byte budget", status_code=status,
                    llm_calls=attempts,
                )
            data = response.json()
            text = _response_text(data)
            usage = _usage(data.get("usage") or {})
            actual, basis = self._cost(usage)
            self._settle(reservation, ticket, booking_errors, actual, basis, usage)
            reservation, ticket = None, None
            finish = _finish_reason(data)
            if not text.strip():
                # say WHY without echoing any content: the finish reason and
                # whether the provider sent a (hidden) reasoning field
                hint = f"finish_reason={finish or '?'}"
                if _has_reasoning(data):
                    hint += ", reasoning only"
                return ModelResult(
                    provider=cfg.provider, model=cfg.model, usage=usage,
                    latency_ms=latency, error=f"provider returned an empty answer ({hint})",
                    status_code=status, llm_calls=attempts, empty_answer=True,
                    finish_reason=finish,
                )
            parsed = None
            if json_schema is not None:
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    return ModelResult(
                        provider=cfg.provider, model=cfg.model, text=text, usage=usage,
                        latency_ms=latency, error="structured answer was not valid JSON",
                        status_code=status, llm_calls=attempts,
                    )
            return ModelResult(
                ok=True, text=text, parsed=parsed, usage=usage, latency_ms=latency,
                provider=cfg.provider, model=cfg.model, status_code=status, llm_calls=attempts,
                finish_reason=finish,
            )
        except Exception as exc:
            if reservation is not None:
                self._settle(reservation, ticket, booking_errors)
            latency = (time.perf_counter() - started) * 1000.0
            return ModelResult(
                provider=cfg.provider,
                model=cfg.model,
                latency_ms=latency,
                provider_unreachable=_is_unreachable(exc),
                error=f"provider request failed ({type(exc).__name__})",
                llm_calls=attempts,
            )

    def _complete_cli(self, messages: Sequence[Mapping[str, Any]], max_tokens: Optional[int],
                      json_schema: Optional[Mapping[str, Any]], images: Sequence[str],
                      booking_errors: List[str]) -> ModelResult:
        """One call through ``claude -p`` or ``codex exec``: one started process = one attempt.

        The prompt goes on stdin, never the command line; the process starts in an empty
        temporary directory; the CLI's own login is used, no credential is passed."""
        import tempfile
        from pathlib import Path

        cfg = self.config
        claude = cfg.provider == "claude-cli"
        system, prompt, image_parts = _cli_prompt(messages, images)
        if image_parts and not claude:
            return ModelResult(provider=cfg.provider, model=cfg.model, llm_calls=0,
                               error="codex-cli takes text only; this request carries images")
        binary = "claude" if claude else "codex"
        reservation = self.budget.reserve(int(max_tokens or cfg.max_tokens), self.price_in, self.price_out,
            input_tokens=self._input_token_bound(messages, json_schema, minimum=int(max_tokens or cfg.max_tokens)))
        if reservation is None:
            return ModelResult(provider=cfg.provider, model=cfg.model, budget_exhausted=True,
                               error=(f"run budget exhausted before provider request "
                                      f"({self.budget.remaining_usd:.6f} USD remaining)"))
        ticket, refusal = spend.reserve(reservation.maximum_usd, self.feature)
        if refusal:
            self.budget.settle(reservation, 0.0)
            return ModelResult(provider=cfg.provider, model=cfg.model, error=refusal,
                               budget_exhausted=True)
        attempts = 0
        started = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="anyuse-cli-") as empty, \
                    tempfile.TemporaryDirectory(prefix="anyuse-cli-out-") as outdir:
                if claude:
                    argv, stdin = _claude_request(cfg.model, system, prompt, json_schema, image_parts)
                    last = None
                else:
                    last = Path(outdir) / "answer.txt"
                    schema_file = None
                    if json_schema is not None:
                        schema_file = Path(outdir) / "schema.json"
                        schema_file.write_text(json.dumps(dict(json_schema)))
                    argv, stdin = _codex_request(cfg.model, system, prompt, last, schema_file)
                try:
                    rc, out, err = self._cli_runner(argv, stdin, empty, max(cfg.timeout, CLI_TIMEOUT_S))
                except OSError as exc:  # not started: no attempt, nothing billed
                    self._settle(reservation, ticket, booking_errors, 0.0, "process not started")
                    reservation = None
                    why = ("not found on PATH" if isinstance(exc, FileNotFoundError)
                           else f"could not start ({type(exc).__name__})")
                    return ModelResult(provider=cfg.provider, model=cfg.model, llm_calls=0,
                                       provider_unreachable=True, error=f"{binary} CLI {why}")
                except Exception:
                    attempts = 1
                    self._note_call()
                    raise
                attempts = 1
                self._note_call()
                latency = (time.perf_counter() - started) * 1000.0
                if claude:
                    text, usage, failed = _claude_answer(out, streamed=bool(image_parts))
                else:
                    failed = False
                    usage = TokenUsage()
                    text = last.read_text(errors="replace") if last.is_file() else ""
            actual, basis = self._cost(usage)
            self._settle(reservation, ticket, booking_errors, actual, basis, usage)
            reservation = None
            if rc != 0 or failed:
                return ModelResult(provider=cfg.provider, model=cfg.model, usage=usage,
                                   latency_ms=latency, provider_unreachable=True, llm_calls=attempts,
                                   error=f"{binary} exited {rc}: {(err or '').strip()[-160:]}")
            if not text.strip():
                return ModelResult(provider=cfg.provider, model=cfg.model, usage=usage,
                                   latency_ms=latency, error="provider returned an empty answer",
                                   llm_calls=attempts, empty_answer=True)
            parsed = None
            if json_schema is not None:
                text = _strip_fence(text)
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    return ModelResult(provider=cfg.provider, model=cfg.model, text=text, usage=usage,
                                       latency_ms=latency, error="structured answer was not valid JSON",
                                       llm_calls=attempts)
            return ModelResult(ok=True, text=text, parsed=parsed, usage=usage, latency_ms=latency,
                               provider=cfg.provider, model=cfg.model, llm_calls=attempts)
        except Exception as exc:
            if reservation is not None:
                self._settle(reservation, ticket, booking_errors)
            return ModelResult(provider=cfg.provider, model=cfg.model,
                               latency_ms=(time.perf_counter() - started) * 1000.0,
                               provider_unreachable=True, llm_calls=attempts,
                               error=f"{binary} CLI call failed ({type(exc).__name__})")

    async def acomplete(self, *args: Any, **kwargs: Any) -> ModelResult:
        import asyncio
        return await asyncio.to_thread(self.complete, *args, **kwargs)

    @staticmethod
    def _attach_images(messages: List[dict], images: Sequence[str]) -> None:
        image_parts = [
            {"type": "image_url", "image_url": {"url": value}}
            for value in images
        ]
        target = next(
            (message for message in reversed(messages) if message.get("role") == "user"),
            None,
        )
        if target is None:
            messages.append({"role": "user", "content": image_parts})
            return
        content = target.get("content", "")
        if isinstance(content, str):
            target["content"] = ([{"type": "text", "text": content}] if content else []) + image_parts
        elif isinstance(content, list):
            target["content"] = list(content) + image_parts
        else:
            target["content"] = image_parts

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _response_text(data: Mapping[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, Mapping)
        )
    return str(content or "")


def _finish_reason(data: Mapping[str, Any]) -> str:
    try:
        return str(data["choices"][0].get("finish_reason") or "")[:32]
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _has_reasoning(data: Mapping[str, Any]) -> bool:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    return isinstance(msg, Mapping) and any(
        msg.get(k) for k in ("reasoning", "reasoning_content", "reasoning_details"))


def _usage(data: Mapping[str, Any]) -> TokenUsage:
    prompt = int(data.get("prompt_tokens", data.get("input_tokens", 0)) or 0)
    completion = int(data.get("completion_tokens", data.get("output_tokens", 0)) or 0)
    total = int(data.get("total_tokens", 0) or 0) or prompt + completion
    try:
        cost = float(data.get("cost", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return TokenUsage(prompt, completion, total, cost)


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    """Seconds from a numeric ``Retry-After`` header, capped; ``None`` when the
    header is absent or in its HTTP-date form (then our own backoff is used)."""
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), RETRY_MAX_DELAY_S))
    except (TypeError, ValueError):
        return None


def _run_cli(argv: List[str], stdin: str, cwd: str, timeout: float) -> Tuple[int, str, str]:
    import subprocess
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _cli_prompt(messages: Sequence[Mapping[str, Any]], images: Sequence[str]
                ) -> Tuple[str, str, List[Tuple[str, str]]]:
    """(system text, transcript, [(media type, base64)]) from chat messages and data-URL images."""
    system: List[str] = []
    turns: List[str] = []
    urls: List[str] = list(images)
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "image_url":
                    urls.append(str((part.get("image_url") or {}).get("url") or ""))
                else:
                    texts.append(str(part.get("text") or ""))
            text = "\n".join(texts)
        else:
            text = str(content or "")
        role = message.get("role")
        if role == "system":
            system.append(text)
        elif role == "user":
            turns.append(text)
        else:
            turns.append(f"[{role}]\n{text}")
    parts = []
    for url in urls:
        head, sep, data = url.partition(";base64,")
        if url.startswith("data:") and sep:
            parts.append((head[5:] or "image/png", data))
    return "\n\n".join(system), "\n\n".join(turns), parts


def _claude_request(model: str, system: str, prompt: str, json_schema: Optional[Mapping[str, Any]],
                    images: List[Tuple[str, str]]) -> Tuple[List[str], str]:
    """``claude -p`` with no tools and none of the person's customisations (the policy argv of
    ``harness_policies.claude_cli_argv``), plus structured output and streamed image input."""
    from anyuse.execution.harness_policies import claude_cli_argv
    argv = claude_cli_argv(model or "sonnet", system)
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(dict(json_schema))]
    if not images:
        return argv, prompt
    argv[argv.index("--output-format") + 1] = "stream-json"
    argv += ["--input-format", "stream-json", "--verbose"]
    content: List[Dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": d}}
                                     for mt, d in images]
    content.append({"type": "text", "text": prompt})
    return argv, json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def _claude_answer(out: str, *, streamed: bool) -> Tuple[str, TokenUsage, bool]:
    """(answer text, usage, failed) from ``claude -p`` JSON or the last stream-json result line."""
    result: Mapping[str, Any] = {}
    if streamed:
        for line in reversed((out or "").splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "result":
                result = item
                break
    else:
        try:
            item = json.loads(out or "")
            result = item if isinstance(item, dict) else {}
        except ValueError:
            result = {}
    usage = result.get("usage") or {}
    prompt = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    completion = int(usage.get("output_tokens") or 0)
    try:
        cost = float(result.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    structured = result.get("structured_output")
    text = json.dumps(structured) if structured is not None else str(result.get("result") or "")
    failed = not result or bool(result.get("is_error"))
    return text, TokenUsage(prompt, completion, prompt + completion, cost), failed


def _codex_request(model: str, system: str, prompt: str, last, schema_file) -> Tuple[List[str], str]:
    """``codex exec`` read-only, without the user's config.toml (its hooks and MCP servers),
    no saved session; the answer is read from ``--output-last-message``."""
    argv = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral",
            "--ignore-user-config"]
    if model:
        argv += ["-m", model]
    if schema_file is not None:
        argv += ["--output-schema", str(schema_file)]
    argv += ["--output-last-message", str(last), "-"]
    text = (system + "\n\n" if system else "") + prompt
    if schema_file is not None:
        text += "\n\nAnswer with one JSON object that matches the given output schema, and nothing else."
    return argv, text


def _strip_fence(text: str) -> str:
    """A JSON answer wrapped in a Markdown code fence, unwrapped."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3]
        if stripped.startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()


def _is_unreachable(exc: Exception) -> bool:
    try:
        import httpx
        return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError))
    except Exception:
        return type(exc).__name__ in {
            "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"
        }
