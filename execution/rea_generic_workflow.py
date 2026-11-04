"""One persistent front door for model discovery, public setup and catalog reuse.

An ordinary caller agent supplies public setup through a journalled handoff.
It needs no application-specific Python producer. Discovery sees no held-out
values or proof results; the existing campaign still owns every proof gate.
"""
from __future__ import annotations

import copy
import fcntl
import json
from pathlib import Path
import time

from . import rea_agent as G
from . import rea_agent_driver as D
from . import rea_campaign_coordinator as C
from . import rea_runtime_bridge as B
from . import rea_runtime_providers as P
from . import rea_visual_campaign as VC
from . import rea_visual_effect as V
from . import rea_mapping as R

FORMAT = "anyuse.rea-generic-workflow/1"
JOURNAL = "anyuse.rea-generic-workflow-journal/1"
PUBLIC = "anyuse.rea-generic-public-handoff/1"
DISCOVERY_TOOLS = ("analyze", "symbols", "resource", "evidence", "static_evidence", "discover", "runtime",
                   "visual_windows", "visual", "observe", "declare_inspector", "declare_native", "declare_native_receiver", "declare_native_receiver_v2", "correlate_native_trace_v2", "build_native_path", "inspect_compiler_toolchain",
                   "prepare_compiler_adapter", "propose")
DEFAULT_LIMITS = {"seconds": 7200, "max_steps": 64, "discovery_decisions": 32,
                  "discovery_seconds": 1800, "context_chars": 96000,
                  "checkpoint_seconds": 900, "trial_seconds": 240, "max_actions": 128}


def ui_trace_context(path, *, bundle_id=None):
    """Load redacted historical windows/differential hypotheses, never proof.

    This helper never attaches a collector. Plain windows need independent
    native observations; saved differential candidates remain bounded hints
    and cannot substitute for actor admission or measured task verification.
    """
    from .rea_ui_trace import load_candidate_trace
    return load_candidate_trace(path, bundle_id=bundle_id)


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _pin(path):
    file = Path(path)
    _need(file.resolve(strict=True) == file and not file.is_symlink() and file.is_file(),
          "canonical regular workflow resource required")
    return {"file": str(file), "bytes": file.stat().st_size, "sha256": R._file_hash(file)}


def _save(path, value, *, replace=False):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    _need(len(raw.encode()) <= 2 * 1024**2, "workflow journal byte budget exceeded")
    if replace:
        R.write_json(path, value)
    else:
        with Path(path).open("x") as stream:
            stream.write(raw)
    return _pin(Path(path))


def _sources():
    from . import rea_static_artifacts as artifacts, rea_process_restart as restart, rea_public_campaign as public, rea_native_contract as declaration, rea_qt_sources as qt_sources, rea_ghidra_artifacts as ghidra
    from . import rea_workflow_restoration as recovery, rea_native_path_builder as native_path_builder, rea_native_receiver_contract as receiver_declaration, rea_native_receiver_v2 as receiver_v2
    return {name: _pin(Path(module.__file__).resolve()) for name, module in
            (("workflow", __import__(__name__, fromlist=["_"])), ("agent", G), ("driver", D),
             ("coordinator", C), ("runtime", B), ("providers", P), ("static_artifacts", artifacts),
             ("visual_campaign", VC), ("visual_effect", V), ("restart", restart), ("public", public),
             ("native_declaration", declaration), ("qt_sources", qt_sources), ("ghidra_artifacts", ghidra),
             ("workflow_restoration", recovery), ("native_path_builder", native_path_builder),
             ("native_receiver_declaration", receiver_declaration), ("native_receiver_v2", receiver_v2))}


def _limits(raw):
    _need(type(raw) is dict and set(raw) <= set(DEFAULT_LIMITS), "unknown workflow limit")
    limits = {**DEFAULT_LIMITS, **raw}
    bounds = {"seconds": (30, 7200), "max_steps": (8, 64), "discovery_decisions": (1, 64),
              "discovery_seconds": (1, 7200), "context_chars": (4096, 250000),
              "checkpoint_seconds": (1, 1800), "trial_seconds": (5, 300), "max_actions": (1, 256)}
    for name, (lo, hi) in bounds.items():
        _need(type(limits[name]) is int and lo <= limits[name] <= hi, "bounded workflow limit required: " + name)
    _need(limits["discovery_seconds"] <= limits["seconds"], "discovery must fit the original workflow deadline")
    return limits


def _creation_spec(spec):
    """Freeze creation-time defaults; saved specs never consult current settings."""
    from .. import settings
    _need(type(spec) is dict, "closed generic workflow task specification required")
    frozen = copy.deepcopy(spec)
    if frozen.get("effort") is None:
        effort = settings.get("ANYUSE_MAP_EFFORT")
        if effort in {"rea-fast", "rea-strict"}:
            frozen["effort"] = effort
    return frozen


def _resolve_spec_modes(spec):
    """Resolve only the supplied spec, including when reloading a frozen journal."""
    effort = spec.get("effort")
    _need(effort in {None, "default", "fast", "strict", "rea-fast", "rea-strict"},
          "effort must be default, fast, strict, rea-fast, or rea-strict")
    rea_default = effort in {"rea-fast", "rea-strict"}
    contract_mode = spec.get("native_contract_mode", "declared" if rea_default else "attributable")
    receiver_mode = spec.get("native_receiver_mode", "binary_runtime_hypothesis" if rea_default else None)
    raw_tp = spec.get("trial_policy")
    if raw_tp is not None:
        if type(raw_tp) is dict and "preset" not in raw_tp and effort in {"fast", "rea-fast"}:
            tp = VC.validate_trial_policy({"preset": "fast", **raw_tp})
        else:
            tp = VC.validate_trial_policy(raw_tp)
    elif effort is not None:
        tp = VC.validate_trial_policy(effort)
    else:
        tp = None
    return contract_mode, receiver_mode, tp


def build_spec(task, before, after, *, spec_id=None, observer_model=None, effort=None,
               trial_policy=None, window_title=None, minimum_confidence=None,
               owned=True, restart_allowed=True):
    """Build and validate a closed generic workflow task specification without IO."""
    import re
    _need(G._text(task) and G._text(before) and G._text(after),
          "freeze the task, visible starting state and desired result before discovery")
    if spec_id is None:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", task.strip().lower()).strip("_-")[:60].rstrip("_-")
        spec_id = slug if slug and R.NAME.fullmatch(slug) else "workflow_task"
    _need(isinstance(spec_id, str) and bool(R.NAME.fullmatch(spec_id)), "valid workflow identifier required")
    model = copy.deepcopy(observer_model) if observer_model is not None else {
        "provider": "local",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "llava",
        "api_key_env": "",
        "timeout": 30,
        "max_tokens": 512,
        "retries": 0,
    }
    if isinstance(model, dict):
        model.setdefault("timeout", 30)
        model.setdefault("max_tokens", 512)
        model.setdefault("retries", 0)
    spec = {
        "format": FORMAT,
        "id": spec_id,
        "task": task,
        "before": before,
        "after": after,
        "observer_model": model,
        "owned": owned,
        "restart_allowed": restart_allowed,
    }
    if window_title is not None:
        spec["window_title"] = window_title
    if minimum_confidence is not None:
        spec["minimum_confidence"] = minimum_confidence
    if effort is not None:
        spec["effort"] = effort
    if trial_policy is not None:
        spec["trial_policy"] = trial_policy
    frozen = _creation_spec(spec)
    _task_contract({
        "bundle_id": "com.example.App",
        "version": "1.0",
        "build": "1",
        "files": 1,
        "bytes": 1,
        "sha256": "a" * 64,
    }, frozen)
    return frozen


def task_contract(target, spec):
    """Freeze a task-authored visual oracle before any candidate exists."""
    return _task_contract(target, _creation_spec(spec))


def _task_contract(target, spec):
    required = {"format", "id", "task", "before", "after", "observer_model", "owned", "restart_allowed"}
    optional = {"params", "plan", "replays", "resources", "window_title", "window_layer",
                "minimum_confidence", "input_constraints", "limits", "publication", "selection_task", "native_contract_mode",
                "restoration", "native_path_review", "native_receiver_mode", "effort", "trial_policy"}
    _need(type(spec) is dict and required <= set(spec) <= required | optional and spec["format"] == FORMAT,
           "closed generic workflow task specification required")
    _need(spec["owned"] is True and spec["restart_allowed"] is True,
          "declare ownership and normal restart authorization for the task-owned test context")
    contract_mode, receiver_mode, tp = _resolve_spec_modes(spec)
    _need(contract_mode in {"attributable", "declared"},
          "select attributable or explicitly declared native types")
    _need(receiver_mode in {None, "binary_runtime_hypothesis"} and
          (receiver_mode is None or contract_mode == "declared"),
          "binary receiver hypotheses require an explicit declared-native workflow")
    _need(G._text(spec["task"], 3000) and G._text(spec["before"]) and G._text(spec["after"], 1200) and
          G._text(spec.get("selection_task", spec["task"]), 3000),
          "freeze bounded task/selection (3000), starting state (4096) and success criterion (1200) before discovery")
    params = copy.deepcopy(spec.get("params", []))
    _need(not params or "replays" in spec, "parameterized workflows need explicit independent replay inputs")
    observer = {"window_title": copy.deepcopy(spec.get("window_title"))}
    if "window_layer" in spec:
        observer["window_layer"] = spec["window_layer"]
    policy = V.validate_policy({"format": V.POLICY, "id": spec["id"], "target": target,
        "kind": "visible_state_transition", "params": params, "observer": observer,
        "before": spec["before"], "after": spec["after"],
        "minimum_confidence": spec.get("minimum_confidence", .9), "model": spec["observer_model"],
        "input_constraints": copy.deepcopy(spec.get("input_constraints", {}))})
    plan = copy.deepcopy(spec.get("plan"))
    if plan is None:
        _need(not params, "parameterized workflows need explicit independent trial inputs")
        effective_tp = tp or VC.DEFAULT_TRIAL_POLICY
        plan = {"format": VC.PLAN}
        if tp is not None:
            plan["trial_policy"] = tp
        role_counts = {"learning": effective_tp["min_learning"],
                       "verification": effective_tp["min_verification"],
                       "heldout": effective_tp["min_heldout"]}
        for role in ("learning", "verification", "heldout"):
            plan[role] = [{"id": role + "_" + str(i), "inputs": {},
                           "restart": bool(effective_tp["require_restart"]) and role == "heldout" and i == 0}
                          for i in range(role_counts[role])]
    elif tp is not None and type(plan) is dict:
        if "trial_policy" not in plan:
            plan["trial_policy"] = tp
        else:
            _need(VC.validate_trial_policy(plan["trial_policy"]) == tp,
                  "explicit plan trial_policy conflicts with workflow effort or trial_policy")
    VC.validate_coverage(plan, policy)
    profile = VC.validate_profile_declaration({"format": VC.PROFILE, "id": spec["id"], "label": spec["task"],
        "effect": "visible_state_transition", "params": params, "policy": policy,
        "scope": VC.scope_for_trial_policy(plan.get("trial_policy"))})
    limits = _limits(spec.get("limits", {}))
    _need(G._campaign_steps(plan) + 1 <= limits["max_steps"],
          "trial plan exceeds the session step budget including proposal, learning and publication")
    minimum = 2 * policy["model"]["timeout"] + 2 * V.V.CAPTURE_TIMEOUT + V.AFTER_VALIDATION_SECONDS + V.MIN_DISPATCH_SECONDS
    _need(limits["trial_seconds"] >= minimum, "trial bound cannot cover both independent visual judgments")
    return profile, plan, limits


def begin(bundle, rea, spec, out):
    R.assert_guest()
    root, app = Path(out).resolve(), Path(bundle).resolve(strict=True)
    _need(not root.exists() and not root.is_relative_to(app), "new workflow folder outside the app required")
    target = R.fingerprint(app)
    spec = _creation_spec(spec)
    profile, plan, limits = _task_contract(target, spec)
    contract_mode, receiver_mode, _ = _resolve_spec_modes(spec)
    identity = B.process_identity(app, target["bundle_id"])
    resources = copy.deepcopy(spec.get("resources", []))
    _need(type(resources) is list and len(resources) <= 128, "bounded task resource list required")
    for pin in resources:
        _need(type(pin) is dict and _pin(Path(pin.get("file", ""))) == pin and
              not Path(pin["file"]).is_relative_to(app), "exact task resources outside the app required")
    _need(len({p["file"] for p in resources}) == len(resources), "distinct task resources required")
    root.mkdir(mode=0o700, parents=True)
    spec_pin = _save(root / "spec.json", spec)
    G.begin(app, rea, spec["task"], profile["params"], [spec["after"]], plan, root / "session",
            max_steps=limits["max_steps"], budget_seconds=limits["seconds"], observer_profile=profile,
            native_contract_mode=contract_mode,
            native_path_review=spec.get("native_path_review"),
            **({"native_receiver_mode": receiver_mode} if receiver_mode is not None else {}))
    journal = {"format": JOURNAL, "status": "discovering", "spec": spec_pin, "sources": _sources(),
        "session": str(root / "session"), "target": target, "identity": identity, "limits": limits,
        "observer_profile_sha256": R.digest(profile), "events": [], "discovery_memory": None,
        "campaign_configuration": None, "campaign_started": False, "pending": None, "public_handoff": None,
        "deadline_wall": G._load(root / "session")["deadline"], "error": None, "automatic_retry": False,
        "verification_tier": "model_judged", "catalog": None}
    # Reject malformed publication/replay/recovery declarations before discovery
    # consumes any model calls, using the caller-frozen profile for pure checks.
    state = G._load(root / "session")
    validation_state = {**state, "hypothesis": {"profile": profile}}
    cfg = C._visual_configuration(_configuration(root, journal, state), validation_state, root / "session")
    journal["campaign_configuration"] = _save(root / "campaign-configuration.json", cfg)
    _save(root / "workflow.json", journal)
    return context(root)


def _load(root):
    journal = R.load_json(root / "workflow.json")
    _need(journal.get("format") == JOURNAL and journal["sources"] == _sources(), "workflow source closure changed")
    _need(_pin(Path(journal["spec"]["file"])) == journal["spec"], "frozen workflow specification changed")
    _need(journal["session"] == str(root / "session") and R.fingerprint(G._load(root / "session")["bundle"]) == journal["target"],
          "workflow session or target changed")
    spec = R.load_json(journal["spec"]["file"])
    contract_mode, receiver_mode, _ = _resolve_spec_modes(spec)
    state = G._load(root / "session")
    _need(state.get("native_contract_mode", "attributable") == contract_mode and
          state.get("native_receiver_mode") == receiver_mode and
          state.get("native_path_review") == spec.get("native_path_review"), "caller-frozen native review mode or policy changed")
    return journal


def _view(root, journal):
    result = {"format": JOURNAL, "root": str(root), "status": journal["status"], "pending": copy.deepcopy(journal["pending"]),
              "catalog": copy.deepcopy(journal["catalog"]), "error": copy.deepcopy(journal["error"]),
              "verification_tier": "model_judged", "automatic_retry": False,
              "session": journal["session"], "events": len(journal["events"])}
    result["public_handoff"] = copy.deepcopy(journal["public_handoff"])
    if journal["pending"] and journal["pending"]["kind"] == "setup":
        spec = R.load_json(journal["spec"]["file"])
        result["public_setup"] = {
            "task": spec["before"], "identity": journal["pending"]["identity"],
            "inputs": copy.deepcopy(journal["pending"]["action"]["inputs"]),
            "resources": copy.deepcopy(spec.get("resources", [])),
            "instructions": ("A public-start claim already exists. Resume that handoff and call public-finish with its actual observations; do not claim or repeat setup automatically. "
                if journal["public_handoff"] is not None else
                "Call public-start before ordinary computer use, prepare this visible starting state in the exact owned app using public UI, retain observations, then call public-finish. ") +
                "Do not invoke the native candidate. Setup readiness is not task proof; the trial performs independent before/after checks.",
        }
    return result


def context(root):
    R.assert_guest()
    root = Path(root).resolve(strict=True)
    return _view(root, _load(root))


def _configuration(root, journal, state):
    spec = R.load_json(journal["spec"]["file"])
    publication = spec.get("publication", {"summary": spec["task"][:240], "details": spec["after"][:1200]})
    limits = journal["limits"]
    cfg = {"format": C.VISUAL_CONFIG, "session": str(root / "session"), "target": journal["target"],
            "plan_sha256": state["plan_sha256"], "observer_profile_sha256": state["observer_profile_sha256"],
            "owned": True, "expected_identity": journal["identity"], "resources": spec.get("resources", []),
            "limits": {k: limits[k] for k in ("seconds", "checkpoint_seconds", "trial_seconds", "max_actions")},
            "publication": publication, "replays": spec.get("replays", [] if state["params"] else
                [{"id": "replay_0", "inputs": {}}, {"id": "replay_1", "inputs": {}}]),
            "selection_task": spec.get("selection_task", spec["task"])}
    if "restoration" in spec:
        cfg["restoration"] = copy.deepcopy(spec["restoration"])
    return cfg


def advance(root, *, adapter=None, continue_discovery=False):
    """Run existing discovery/proof machinery until the next ordinary-agent handoff."""
    R.assert_guest()
    root = Path(root).resolve(strict=True)
    with (root / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = _load(root)
        _need(type(continue_discovery) is bool, "continue_discovery must be explicit boolean data")
        if continue_discovery:
            _need(journal["status"] in {"discovery_incomplete", "discovery_stopped"} and
                  G._load(root / "session")["status"] == "investigating",
                  "only healthy bounded discovery can continue; failed tools and uncertain executions cannot retry")
            journal.update(status="discovering", error=None)
        _need(journal["status"] in {"discovering", "campaign_ready", "needs_setup", "needs_restart", "completed"},
              "failed, interrupted or incomplete discovery cannot silently resume")
        if journal["status"] == "completed" or journal["pending"] is not None:
            return _view(root, journal)
        _need(time.time() < journal["deadline_wall"], "original workflow deadline expired")
        work = root / ("event-" + str(len(journal["events"])).zfill(4))
        work.mkdir(mode=0o700)
        event = {"status": "started", "kind": journal["status"], "out": str(work), "started_monotonic": time.monotonic()}
        journal["events"].append(event); journal["status"] = "running"
        _save(root / "workflow.json", journal, replace=True)
        try:
            state = G._load(root / "session")
            if event["kind"] == "discovering":
                _need(B.process_identity(Path(state["bundle"]), journal["target"]["bundle_id"]) == journal["identity"],
                      "discovery process epoch changed")
                limits = journal["limits"]
                seconds = min(limits["discovery_seconds"], int(journal["deadline_wall"] - time.time()))
                decisions = (1 if state["status"] == "hypothesis" else min(limits["discovery_decisions"],
                    state["max_steps"] - len(state["steps"]) - G._campaign_steps(state["plan"])))
                _need(decisions > 0, "discovery exhausted its step allowance reserved before the campaign")
                result = D.run(journal["session"], str(work / "discovery"), adapter=adapter,
                    max_decisions=decisions, timeout_seconds=seconds,
                    max_context_chars=limits["context_chars"], until="hypothesis", allowed_tools=DISCOVERY_TOOLS,
                    decision_protocol=D.TOOL_OBJECT, max_decision_corrections=2,
                    resume_from=journal["discovery_memory"])
                event["result"] = _save(work / "result.json", result)
                if result.get("memory_checkpoint") is not None:
                    journal["discovery_memory"] = str(work / "discovery")
                state = G._load(root / "session")
                if result["status"] != "boundary_reached" or state["status"] != "hypothesis":
                    journal["status"] = {"decision_budget_exhausted": "discovery_incomplete", "model_stopped": "discovery_stopped"}.get(result["status"], "failed_no_retry")
                    journal["error"] = result.get("error") or {"type": result["status"], "message": result.get("reason") or "Discovery did not produce an admitted hypothesis; see the retained model/tool evidence."}
                    event["status"] = "completed"; event["finished_monotonic"] = time.monotonic()
                    _save(root / "workflow.json", journal, replace=True)
                    return _view(root, journal)
            _need(state["status"] in {"hypothesis", "recording", "candidate", "model_judged", "published"},
                  "campaign requires a healthy admitted hypothesis")
            cfg_pin = journal["campaign_configuration"]
            _need(_pin(Path(cfg_pin["file"])) == cfg_pin, "campaign configuration changed")
            result = C.run_visual(journal["session"], R.load_json(cfg_pin["file"]), str(root / "campaign"),
                                  resume=journal["campaign_started"])
            journal["campaign_started"] = True
            event["campaign_result"] = _save(work / "campaign-result.json", result)
            _need(result["status"] in {"needs_setup", "needs_restart", "completed", "ready"},
                  "campaign stopped; no retry: " + str(result.get("error")))
            journal.update(status=result["status"] if result["status"] != "ready" else "campaign_ready",
                           pending=copy.deepcopy(result["pending"]), identity=result["identity"], catalog=result["published"])
            event["status"] = "completed"
        except BaseException as error:
            journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:4096]})
            event["status"] = "failed"
            if not isinstance(error, Exception):
                raise
        finally:
            event["finished_monotonic"] = time.monotonic()
            _save(root / "workflow.json", journal, replace=True)
        return _view(root, journal)


def public_start(root):
    """Claim exactly one current setup request before ordinary computer use."""
    R.assert_guest()
    root = Path(root).resolve(strict=True)
    with (root / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = _load(root); pending = journal["pending"]
        _need(journal["status"] in {"needs_setup", "needs_restart"} and pending is not None and
              journal["public_handoff"] is None and time.time() < pending["expires_wall"],
              "one unconsumed live public request required")
        handoff = {"format": PUBLIC, "request_sha256": R.digest(pending), "kind": pending["kind"],
                   "started_monotonic": time.monotonic(), "identity": pending["identity"], "status": "started"}
        journal["public_handoff"] = _save(root / ("public-" + str(pending["sequence"]) + "-start.json"), handoff)
        _save(root / "workflow.json", journal, replace=True)
        return {**_view(root, journal), "public_handoff": copy.deepcopy(journal["public_handoff"])}


def public_finish(root, evidence, *, restart=None):
    """Retain an ordinary-agent assertion; the campaign independently checks it."""
    R.assert_guest()
    root = Path(root).resolve(strict=True)
    with (root / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = _load(root); pending, handoff_pin = journal["pending"], journal["public_handoff"]
        _need(journal["status"] in {"needs_setup", "needs_restart"} and pending is not None and handoff_pin is not None,
              "public-start is required before public-finish")
        _need(_pin(Path(handoff_pin["file"])) == handoff_pin, "public start observation changed")
        handoff = R.load_json(handoff_pin["file"])
        _need(handoff["request_sha256"] == R.digest(pending) and time.time() < pending["expires_wall"],
              "public handoff is stale or belongs to another request")
        _need(type(evidence) is list and 1 <= len(evidence) <= 15, "1..15 actual public observation pins required")
        for pin in evidence:
            _need(type(pin) is dict and _pin(Path(pin.get("file", ""))) == pin and pin["bytes"] <= 64 * 1024**2,
                  "public observation bytes changed or exceed the bound")
        _need((pending["kind"] == "restart") == (restart is not None), "restart evidence belongs only to a restart request")
        identity = B.process_identity(
            Path(G._load(root / "session")["bundle"]), journal["target"]["bundle_id"])
        if pending["kind"] == "setup":
            _need(identity == pending["identity"], "ordinary setup cannot adopt a successor process")
        checkpoint = {"format": C.VISUAL_CHECKPOINT, "request_sha256": R.digest(pending), "kind": pending["kind"],
            "status": "ready", "identity": identity, "evidence": [handoff_pin, *copy.deepcopy(evidence)],
            "started_monotonic": handoff["started_monotonic"], "finished_monotonic": time.monotonic()}
        if restart is not None:
            checkpoint["restart"] = copy.deepcopy(restart)
        checkpoint_pin = _save(root / ("public-" + str(pending["sequence"]) + "-finish.json"), checkpoint)
        journal["status"] = "running"; _save(root / "workflow.json", journal, replace=True)
        try:
            cfg_pin = journal["campaign_configuration"]
            _need(_pin(Path(cfg_pin["file"])) == cfg_pin, "campaign configuration changed")
            result = C.run_visual(journal["session"], R.load_json(cfg_pin["file"]), str(root / "campaign"),
                                  resume=True, checkpoint=checkpoint_pin["file"])
            _need(result["status"] in {"needs_setup", "needs_restart", "ready", "completed"},
                  "campaign failed after public handoff; no retry: " + str(result.get("error")))
            journal.update(status=result["status"] if result["status"] != "ready" else "campaign_ready",
                           pending=copy.deepcopy(result["pending"]), identity=result["identity"], catalog=result["published"],
                           public_handoff=None)
        except BaseException as error:
            journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:4096]})
            if not isinstance(error, Exception):
                raise
        finally:
            _save(root / "workflow.json", journal, replace=True)
        return _view(root, journal)


def public_restart(root, *, timeout=30):
    """One normal owned-app restart through the existing independent witness.

    The frozen task authorizes restarts. A refusal, save dialog, failed launch
    or uncertain cleanup stops this workflow; there is no force quit or retry.
    """
    from . import rea_process_restart as RR, rea_public_campaign as U
    R.assert_guest()
    _need(type(timeout) is int and 1 <= timeout <= 60, "restart timeout must be 1..60 seconds")
    root = Path(root).resolve(strict=True)
    _need(context(root)["status"] == "needs_restart", "one pending restart required")
    # Public-start records the beginning before any stop/launch operation.
    public_start(root)
    with (root / "workflow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = _load(root); pending = journal["pending"]
        _need(journal["status"] == "needs_restart" and pending["kind"] == "restart", "one pending restart required")
        _need(R.load_json(journal["spec"]["file"])["restart_allowed"] is True, "normal restart was not authorized")
        journal["status"] = "public_running"; _save(root / "workflow.json", journal, replace=True)
        try:
            state = G._load(root / "session")
            predecessor, _ = C._visual_latest(state)
            backend = U.PublicPlayback()
            intent = {"format": RR.INTENT, "id": "restart-" + pending["action"]["case_id"],
                      "target": journal["target"], "old_identity": pending["identity"],
                      "stop": {"id": "ordinary-stop", "producer": backend.producers["stop"]},
                      "launch": {"id": "ordinary-launch", "producer": backend.producers["launch"]}}
            work = root / ("restart-" + str(pending["sequence"]))
            seconds = min(timeout, int(pending["expires_wall"] - time.time()), int(journal["deadline_wall"] - time.time()))
            _need(seconds >= 1, "restart request expired before dispatch")
            result = RR.run_restart(state["bundle"], pending["identity"], intent, str(work),
                                    stop_hook=backend.stop, launch_hook=backend.launch, timeout=seconds)
            _need(result["status"] == "restart_measurement_validated", "ordinary restart failed; no retry")
            receipt = R.load_json(result["receipt"]["file"])
            entry = {"intent": receipt["intent"], "receipt": result["receipt"], "predecessor": predecessor}
            journal["status"] = "needs_restart"
        except BaseException as error:
            journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:4096]})
            if not isinstance(error, Exception):
                raise
        finally:
            _save(root / "workflow.json", journal, replace=True)
        if journal["status"] == "failed_no_retry":
            return _view(root, journal)
    return public_finish(root, [result["receipt"], result["measurement"]], restart=entry)
