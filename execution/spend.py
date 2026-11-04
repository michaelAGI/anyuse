"""One ledger and one daily cap for every model call a product process makes.

``ModelAdapter.complete`` is the chokepoint. Before each request it asks
:func:`reserve` for room under the global daily cap; after the request it calls
:func:`book` with the amount the run budget settled (the provider's cost when
it reports one, else tokens x list price, else the reservation's maximum).

* **Ledger.** ``<data dir>/spend/ledger.jsonl``, one JSON row per request sent:
  UTC time (``at``), ``feature`` (the adapter role, or the label a caller gave
  the adapter), ``provider``, ``model``, ``prompt_tokens``,
  ``completion_tokens``, ``cost_usd`` and ``cost_basis``. Rows are appended
  under an exclusive ``flock`` and read under a shared one.
* **Cap.** ``ANYUSE_SPEND_DAILY_USD`` (settings registry): a non-negative
  number of US dollars per UTC day, or ``unlimited``; default 5.0. A request is
  refused before it is sent when today's booked sum, plus the maximum of the
  requests this process has in flight, plus this request's maximum would pass
  the cap. Other processes' in-flight requests are seen once they are booked.
* **Fail closed.** An unreadable ledger, a line that is not a ledger row, a
  today-row whose cost is not a finite number, an unparseable cap (environment
  or ``settings.toml``) or a ledger that cannot be appended to means no request.

**Arming.** All of the above applies only after :func:`arm` ran in this
process. The product entry points arm: the ``anyuse`` CLI group, the MCP
server's ``serve()``. An
unarmed process (benches, tests that do not arm) reads no cap and never opens,
reads or creates the ledger: :func:`reserve` returns ``(None, "")`` and
:func:`book` is never reached. Arming is code-level; there is no environment
switch for it.

Per-feature daily caps (``ANYUSE_DRAFT_PROPOSE_DAILY_USD``,
``ANYUSE_DRAFT_FILL_DAILY_USD``) keep their own logs and checks in
``draft_params`` / ``draft_fill``; their requests reach this ledger only through
the adapter, so each request is booked here once.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CAP_SETTING = "ANYUSE_SPEND_DAILY_USD"
DEFAULT_DAILY_USD = 5.0
UNLIMITED = "unlimited"
LEDGER_PARTS = ("spend", "ledger.jsonl")
#: the label of a request whose adapter was built without a role or a feature
UNLABELLED = "unlabelled"
_EPS = 1e-12

_armed = False
_lock = threading.Lock()
#: the maxima of requests this process reserved and has not booked yet
_inflight_usd = 0.0


def arm() -> None:
    """Make this process keep the ledger and obey the global cap (entry points only)."""
    global _armed
    _armed = True


def disarm() -> None:
    """Undo :func:`arm` (tests that arm a process and must hand it back unarmed)."""
    global _armed, _inflight_usd
    with _lock:
        _armed = False
        _inflight_usd = 0.0


def armed() -> bool:
    return _armed


def ledger_path() -> Path:
    """Where the ledger lives. Resolving the path touches nothing on disk."""
    from anyuse.paths import anyuse_data_path
    return anyuse_data_path(*LEDGER_PARTS)


def _armed_ledger_path() -> Path:
    if not _armed:
        raise RuntimeError("the spend ledger is used only by an armed process")
    return ledger_path()


# ---------------------------------------------------------------------------
# the cap
# ---------------------------------------------------------------------------

def cap_from_value(value: Any) -> float:
    """A registry value (number or ``unlimited``) as dollars; ``unlimited`` is ``inf``."""
    if isinstance(value, str) and value.strip().lower() == UNLIMITED:
        return math.inf
    return float(value)


def daily_cap_usd() -> Tuple[float, str, str]:
    """``(cap, source, problem)``. Any value that cannot be read is a cap of 0 with the reason."""
    from anyuse import settings as S
    try:
        resolved = S.resolve(CAP_SETTING)
    except S.SettingError as exc:
        return 0.0, "env", f"{exc} (a non-negative amount or 'unlimited'): no model call is made"
    if resolved.source not in ("env", "explicit"):
        # resolve() falls back to the default for a file value it cannot parse;
        # for the spend cap that fallback could raise the limit, so it refuses
        try:
            raw = S.load_file(strict=True)
        except S.SettingError as exc:
            return 0.0, "file", f"{exc}: no model call is made"
        if CAP_SETTING in raw and resolved.source != "file":
            return 0.0, "file", (f"{CAP_SETTING} = {str(raw[CAP_SETTING])[:20]!r} in "
                                 f"{S.settings_path()} is not a non-negative amount or "
                                 f"'unlimited': no model call is made")
    try:
        cap = cap_from_value(resolved.value)
    except (TypeError, ValueError):
        return 0.0, resolved.source, f"{CAP_SETTING} is not an amount: no model call is made"
    if math.isnan(cap) or cap < 0:
        return 0.0, resolved.source, f"{CAP_SETTING} is not a non-negative amount: no model call is made"
    return cap, resolved.source, ""


def format_usd(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "?"
    if math.isinf(value):
        return UNLIMITED
    return f"US${value:.{digits}f}"


# ---------------------------------------------------------------------------
# reading the ledger
# ---------------------------------------------------------------------------

@dataclass
class Today:
    """Today's (UTC) booked spend. ``total`` is ``inf`` and ``problem`` says why when the
    ledger cannot be read as amounts."""

    day: str
    total: float = 0.0
    by_feature: Dict[str, Dict[str, float]] = field(default_factory=dict)
    problem: str = ""
    exists: bool = False

    def to_dict(self) -> dict:
        return {"day": self.day,
                "total_usd": round(self.total, 6) if math.isfinite(self.total) else None,
                "by_feature": {k: {"usd": round(v["usd"], 6), "calls": int(v["calls"])}
                               for k, v in sorted(self.by_feature.items())},
                "problem": self.problem, "ledger_exists": self.exists}


def utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")


def read_today(now: Optional[datetime] = None, path: Optional[Path] = None) -> Today:
    """Sum today's rows per feature. Never creates the file or its directory."""
    p = Path(path) if path is not None else _armed_ledger_path()
    out = Today(day=utc_day(now))

    def corrupt(why: str) -> Today:
        out.total, out.by_feature, out.problem = math.inf, {}, (
            f"the spend ledger {p} {why}; no model call is made until it is fixed or moved away")
        return out

    try:
        fh = open(p, encoding="utf-8")
    except FileNotFoundError:
        return out
    except OSError as exc:
        return corrupt(f"cannot be read ({exc.strerror or type(exc).__name__})")
    out.exists = True
    try:
        with fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return corrupt(f"has a line that is not JSON (line {number})")
                if not isinstance(row, dict) or not isinstance(row.get("at"), str):
                    return corrupt(f"has a line that is not a ledger row (line {number})")
                if not row["at"].startswith(out.day):
                    continue
                cost = row.get("cost_usd")
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
                        or not math.isfinite(cost):
                    return corrupt(f"has a row of today whose cost is not a finite amount "
                                   f"(line {number})")
                feature = str(row.get("feature") or UNLABELLED)
                slot = out.by_feature.setdefault(feature, {"usd": 0.0, "calls": 0})
                slot["usd"] += max(float(cost), 0.0)
                slot["calls"] += 1
                out.total += max(float(cost), 0.0)
    except (OSError, UnicodeDecodeError) as exc:
        return corrupt(f"cannot be read ({getattr(exc, 'strerror', None) or type(exc).__name__})")
    return out


# ---------------------------------------------------------------------------
# reserve before the request, book after it
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    """Room reserved under the global cap for one request."""

    feature: str
    maximum_usd: float
    booked: bool = False


def _append_problem(p: Path) -> str:
    """Why a request could not be booked afterwards ("" when it can). Creates the file."""
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
    except OSError as exc:
        return (f"the spend ledger {p} cannot be written ({exc.strerror or type(exc).__name__}); "
                f"no model call is made")
    return ""


def reserve(maximum_usd: float, feature: str = "",
            now: Optional[datetime] = None) -> Tuple[Optional[Ticket], str]:
    """Room for one request of at most ``maximum_usd``: ``(ticket, "")``, or ``(None, why)``
    when it is refused. An unarmed process gets ``(None, "")`` and nothing is read."""
    global _inflight_usd
    if not _armed:
        return None, ""
    try:
        maximum = float(maximum_usd)
        if not math.isfinite(maximum) or maximum < 0:
            return None, "the request's maximum cost is not a finite amount: no model call is made"
        with _lock:
            cap, _source, problem = daily_cap_usd()
            if problem:
                return None, problem
            p = _armed_ledger_path()
            today = read_today(now, p)
            if today.problem:
                return None, today.problem
            if today.total + _inflight_usd + maximum > cap + _EPS:
                return None, (
                    f"the daily spend cap ({CAP_SETTING} = {format_usd(cap)}) would be passed: "
                    f"today ({today.day} UTC) {format_usd(today.total)} is booked"
                    + (f", {format_usd(_inflight_usd)} is in flight" if _inflight_usd > 0 else "")
                    + f" and this request costs at most {format_usd(maximum, 6)}; no request was "
                    f"sent. `anyuse settings spend` shows today's ledger; `anyuse settings set "
                    f"{CAP_SETTING} <amount|{UNLIMITED}>` raises or lifts the cap")
            problem = _append_problem(p)
            if problem:
                return None, problem
            _inflight_usd += maximum
            return Ticket(feature=str(feature or UNLABELLED), maximum_usd=maximum), ""
    except Exception as exc:  # noqa: BLE001 - any failure to check is a refusal, never a pass
        return None, f"the spend cap could not be checked ({type(exc).__name__}): no model call is made"


def book(ticket: Ticket, *, provider: str, model: str, prompt_tokens: int = 0,
         completion_tokens: int = 0, cost_usd: float, cost_basis: str,
         now: Optional[datetime] = None) -> str:
    """Append the request's row and release its reservation. Returns "" or the reason the row
    could not be written (also logged at ERROR)."""
    global _inflight_usd
    with _lock:
        if ticket.booked:
            return ""
        ticket.booked = True
        try:
            cost = float(cost_usd)
            if not math.isfinite(cost) or cost < 0:
                cost, cost_basis = ticket.maximum_usd, "upper bound (no usage reported)"
            row = {"at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "feature": ticket.feature, "provider": str(provider), "model": str(model),
                   "prompt_tokens": max(int(prompt_tokens or 0), 0),
                   "completion_tokens": max(int(completion_tokens or 0), 0),
                   "cost_usd": round(cost, 8), "cost_basis": str(cost_basis)}
            data = (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
            p = _armed_ledger_path()
            p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception as exc:  # noqa: BLE001 - reported to the caller and the log
            why = (f"the spend ledger row for a {ticket.feature} request "
                   f"({format_usd(float(cost_usd) if isinstance(cost_usd, (int, float)) else None, 6)}) "
                   f"could not be written ({type(exc).__name__}: {exc})")
            logger.error("%s", why)
            return why
        _inflight_usd = max(0.0, _inflight_usd - ticket.maximum_usd)
        return ""


# ---------------------------------------------------------------------------
# the readout (read-only)
# ---------------------------------------------------------------------------

def summary(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The effective caps and, in an armed process, today's ledger per feature. Creates no file."""
    from anyuse import settings as S
    cap, source, problem = daily_cap_usd()
    caps: Dict[str, Any] = {
        "global_daily": {"setting": CAP_SETTING, "usd": None if math.isinf(cap) else cap,
                         "unlimited": math.isinf(cap), "source": source, "problem": problem},
    }
    try:
        run = S.resolve("ANYUSE_RUN_BUDGET_USD")
        run_usd = cap_from_value(run.value)
        caps["per_run"] = {"setting": "ANYUSE_RUN_BUDGET_USD",
                           "usd": None if math.isinf(run_usd) else run_usd,
                           "unlimited": math.isinf(run_usd), "source": run.source, "problem": ""}
    except (S.SettingError, TypeError, ValueError) as exc:
        caps["per_run"] = {"setting": "ANYUSE_RUN_BUDGET_USD", "usd": None, "unlimited": False,
                           "source": "env", "problem": str(exc)}
    from anyuse.execution import draft_fill as DF
    from anyuse.execution import draft_params as DP
    for key, module in (("draft_propose_daily", DP), ("draft_fill_daily", DF)):
        env_name = module.DAILY_CAP_ENV
        entry: Dict[str, Any] = {"setting": env_name,
                                 "source": "env" if os.environ.get(env_name, "").strip() else "default"}
        fcap, fproblem = module.daily_cap_usd()
        log = module.spend_log_path()
        spent = DP.spent_today_usd(log, now)          # the feature's own log; never created here
        entry.update({"usd": None if math.isinf(fcap) else fcap, "unlimited": math.isinf(fcap),
                      "problem": fproblem, "log": log,
                      "spent_today_usd": round(spent, 6) if math.isfinite(spent) else None})
        caps[key] = entry
    out: Dict[str, Any] = {"armed": _armed, "ledger": str(ledger_path()), "caps": caps,
                           "today": None}
    if _armed:
        today = read_today(now)
        out["today"] = today.to_dict()
        if not today.problem and not problem:
            out["remaining_usd"] = None if math.isinf(cap) else round(max(0.0, cap - today.total), 6)
    return out


def doctor_line(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """``(ok, text)`` for ``anyuse doctor``: the global cap and today's spend."""
    cap, source, problem = daily_cap_usd()
    if problem:
        return False, problem
    cap_text = f"cap {format_usd(cap, 2)} per UTC day ({CAP_SETTING}, {source})"
    if not _armed:
        return True, cap_text + "; this process is not armed, the ledger was not read"
    today = read_today(now)
    if today.problem:
        return False, cap_text + "; " + today.problem
    return True, f"{format_usd(today.total)} spent today ({today.day} UTC), {cap_text}"
