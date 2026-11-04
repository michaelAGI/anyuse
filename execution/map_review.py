"""Gate R for the product: the independent review of a map-compiled plan.

The reviewer is ANOTHER model with ANOTHER prompt than the planner's - by default the user's Claude CLI
(``claude -p --model sonnet --tools "" --safe-mode``), run from an
empty directory with every tool off, or the Codex CLI (``codex exec --sandbox read-only``). It reads the task text and
the plan rendered in plain words (``map_compile.render_plan``) and answers ``{covers, missing}``; an unparseable or
missing answer does NOT cover (the gate fails closed). The setting ``ANYUSE_MAP_REVIEWER`` (``cli:model``, default
``claude:sonnet``; ``none`` disables the reviewer, and then no map-compiled workflow is ever ``ready``) chooses it.

Cost per plan (measured): one CLI call of about 5 s; about 1.5-2.5 k tokens in and under 150 out. Under the
user's CLI subscription no per-call price is booked; at Sonnet's API list price (US$3 / 15 per million tokens) a review
would cost about US$0.006-0.01. Every call is logged (prompt hash, raw answer).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Sequence

SETTING = "ANYUSE_MAP_REVIEWER"
DEFAULT_REVIEWER = "claude:sonnet"
CLIS = ("claude", "codex")
COST_NOTE = ("one CLI call per plan, ~5 s; ~1.5-2.5k tokens in, <150 out; no per-call price under the owner's CLI "
             "subscription; at API list price about US$0.006-0.01 per plan")


def parse_reviewer(spec: Optional[str]) -> Optional[Dict[str, str]]:
    """``cli:model`` -> {cli, model}; ``none`` / empty -> None (no reviewer: never ready)."""
    s = str(spec or "").strip()
    if not s or s.lower() in ("none", "off", "0", "false"):
        return None
    cli, _, model = s.partition(":")
    cli = cli.strip().lower()
    if cli not in CLIS:
        raise ValueError(f"{SETTING}: the reviewer must be one of {CLIS} (cli:model), not {spec!r}")
    model = model.strip() or ("sonnet" if cli == "claude" else "")
    if not model:
        raise ValueError(f"{SETTING}: {cli} requires an explicit model ({cli}:<model>), not {spec!r}")
    return {"cli": cli, "model": model}


class CliReviewer:
    """Gate R through a CLI from an empty directory, one call per distinct (task, rendering), under a call budget;
    every call logged to ``log_path`` (prompt hash, raw answer, verdict)."""

    def __init__(self, log_path: Optional[str] = None, budget: int = 26, cli: str = "claude", model: str = "sonnet"):
        self.log_path, self.budget, self.cli, self.model = log_path, budget, cli, model
        self.calls = 0
        self.cache: Dict[str, Dict[str, Any]] = {}
        #: verdicts of earlier runs by the 16-character key prefix the log keeps (``preload``)
        self.prefix_cache: Dict[str, Dict[str, Any]] = {}
        self.cwd = tempfile.mkdtemp(prefix="anyuse-review-cwd-")

    def describe(self) -> Dict[str, Any]:
        return {"cli": self.cli, "model": self.model, "budget": self.budget, "calls": self.calls, "cost": COST_NOTE}

    def __call__(self, text: str, rendering: str) -> Dict[str, Any]:
        from anyuse.execution import map_compile as M
        key = hashlib.sha256((text + "\n---\n" + rendering).encode()).hexdigest()
        if key in self.cache:
            return dict(self.cache[key], cached=True)
        if key[:16] in self.prefix_cache:
            # the same task and the same plan rendering were reviewed in an earlier run: that verdict stands
            # (no call is spent on a prompt already answered); logged with where it came from
            verdict = dict(self.prefix_cache[key[:16]], cached=True)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": key[:16], "cli": self.cli, "model": self.model, "verdict": verdict,
                                         "cached_from": verdict.get("cached_from"), "plan": rendering[:1500],
                                         "task": text[:300]}, ensure_ascii=False) + "\n")
            self.cache[key] = verdict
            return verdict
        if self.calls >= self.budget:
            return {"covers": False, "missing": "review budget exhausted (not reviewed)", "parsed": False, "skipped": True}
        msgs = M.review_messages(text, rendering)
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}   # nothing of ours goes to the CLI
        if self.cli == "claude":
            cmd = ["claude", "-p", "--model", self.model, "--safe-mode", "--tools", "", "--system-prompt", msgs[0]["content"],
                   "--no-session-persistence", "--output-format", "json"]
            prompt = msgs[1]["content"] + "\n\nAnswer with the JSON object only."
        else:
            cmd = ["codex", "exec", "-m", self.model, "--sandbox", "read-only", "-C", self.cwd, "--skip-git-repo-check",
                   "--ephemeral", "-c", 'model_reasoning_effort="low"', "--color", "never", "-"]
            prompt = msgs[0]["content"] + "\n\n" + msgs[1]["content"] + "\n\nAnswer with the JSON object only."
        self.calls += 1
        t0 = time.time()
        try:
            pr = subprocess.run(cmd, input=prompt.encode(), capture_output=True, cwd=self.cwd, env=env, timeout=600)
            out = pr.stdout.decode("utf-8", "replace")
            if self.cli == "claude":
                try:
                    out = json.loads(out).get("result") or out
                except ValueError:
                    pass
            rc = pr.returncode
        except Exception as exc:  # noqa: BLE001 - a reviewer that cannot run does not cover
            out, rc = f"error: {exc}", -1
        verdict = M.parse_review(out)
        verdict.update(call=self.calls, seconds=round(time.time() - t0, 1), rc=rc, cli=self.cli, model=self.model)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": key[:16], "cli": self.cli, "model": self.model,
                                     "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "verdict": verdict,
                                     "raw": out[:1500], "plan": rendering[:1500], "task": text[:300]}, ensure_ascii=False) + "\n")
        self.cache[key] = verdict
        return verdict


def _preload(self: "CliReviewer", paths: Sequence[str]) -> int:
    """Earlier ``reviews.jsonl`` logs: a PARSED verdict (covers / does not cover; never a skipped or unreadable
    one) is reused for the same (task, rendering), matched by the 16-character key prefix the log keeps."""
    n = 0
    for path in paths:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            v = d.get("verdict") or {}
            if d.get("key") and v.get("parsed", True) and not v.get("skipped") and "covers" in v:
                self.prefix_cache[str(d["key"])] = dict({k: v[k] for k in ("covers", "missing", "parsed")},
                                                        cached_from=os.path.relpath(path, os.path.expanduser("~")))
                n += 1
    return n


CliReviewer.preload = _preload  # type: ignore[attr-defined]


def reviewer_from_settings(explicit: Optional[str] = None, log_path: Optional[str] = None,
                           budget: int = 26) -> Optional[CliReviewer]:
    """The product's reviewer from ``ANYUSE_MAP_REVIEWER`` (or ``explicit``); None when disabled."""
    from anyuse.settings import get as get_setting
    spec = parse_reviewer(get_setting(SETTING, explicit) if explicit is None else explicit)
    if spec is None:
        return None
    if log_path is None:
        from anyuse.paths import anyuse_data_path
        log_path = str(anyuse_data_path("spend", "map_review.jsonl"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    return CliReviewer(log_path, budget=budget, cli=spec["cli"], model=spec["model"])
