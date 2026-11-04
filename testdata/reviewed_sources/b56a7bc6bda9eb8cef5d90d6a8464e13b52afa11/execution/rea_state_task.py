"""Pure fixed zero-input state-task declarations, not an execution/proof gate.

Deployment/trial identities are separate from actor inputs. Distinct IDs and
file references establish bookkeeping only: their bytes, runtime, setup/ready
timing, settlements, restart evidence and independent provenance must still be
validated by a later shared consumer. This module performs no IO or actuation
and never turns declarations into successful trials, publication or input
generalization. Legacy content/lookup policies are not modified or accepted.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

from . import rea_ax_state as S
from . import rea_state_reader as SR

POLICY = "anyuse.rea-fixed-state-task/1"
POLICY2 = "anyuse.rea-fixed-state-task/2"
CASE = "anyuse.rea-state-deployment-case/1"
PLAN = "anyuse.rea-state-trial-plan/1"
BOOKKEEPING = "anyuse.rea-state-task-bookkeeping/1"
PHASES = ("learning", "fresh", "heldout")
MINIMUMS = {"learning": 3, "fresh": 3, "heldout": 3}
MAX_CASES, MAX_POLICY_BYTES, MAX_EVIDENCE_BYTES = 32, 8 * 1024**2, 64 * 1024**2
REQUIRED_EVIDENCE = {"before_settlement", "execution", "worker", "after_settlement", "run_context"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _require(value, message):
    if not value: raise ValueError(message)


def _keys(value, required):
    _require(type(value) is dict and set(value) == set(required), "closed fixed state-task schema required")


def _same(left, right):
    if type(left) is not type(right): return False
    if type(left) is dict:
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _identifier(value):
    _require(type(value) is str and _ID.fullmatch(value), "bounded literal deployment/trial identifier required")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    """Canonical declaration digest, not the hash of a differently encoded file."""
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _pin(value):
    _keys(value, {"file", "sha256", "bytes"})
    path = value["file"]
    _require(type(path) is str and 0 < len(path.encode("utf-8")) <= 4096 and
             all(32 <= ord(char) != 127 for char in path) and path.startswith("/") and not path.startswith("//") and
             str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts and PurePosixPath(path).name,
             "canonical lexical evidence file required; later VM reader must also reject aliases")
    _require(type(value["sha256"]) is str and _SHA.fullmatch(value["sha256"]) and
             type(value["bytes"]) is int and 0 < value["bytes"] <= MAX_EVIDENCE_BYTES,
             "actual bounded evidence file hash/size reference required")
    return copy.deepcopy(value)


def validate_inputs(inputs):
    _require(type(inputs) is dict and inputs == {}, "fixed state tasks have inputs={} only; case IDs are not actor arguments")
    return {}


def validate_policy(policy):
    policy = copy.deepcopy(policy)
    _require(type(policy) is dict and type(policy.get("format")) is str and policy["format"] in {POLICY, POLICY2},
             "unsupported fixed state-task policy format")
    fields = {"format", "id", "kind", "params", "reference_policy", "input_scope", "heldout_scope",
              "phase_minimums", "before", "after"}
    _keys(policy, fields | ({"observer"} if policy["format"] == POLICY2 else set()))
    if policy["format"] == POLICY2:
        _keys(policy["observer"], {"capability"})
        _require(policy["observer"]["capability"] == SR.ownedcap(), "explicit supported owner-bound state observer required")
    observer = SR.for_policy(policy)
    _identifier(policy["id"])
    _require(policy["kind"] == "fixed_state_transition" and
             type(policy["params"]) is list and policy["params"] == [] and
             policy["input_scope"] == "fixed_no_actor_inputs" and
             policy["heldout_scope"] == "fresh_trials_with_declared_restart" and
             policy["reference_policy"] == "independently_frozen_complete_states" and
             _same(policy["phase_minimums"], MINIMUMS), "unsupported fixed state-task policy")
    for name in ("before", "after"):
        selected = policy[name]
        _keys(selected, {"contract", "reference"})
        selected["contract"] = observer.validate_contract(selected["contract"])
        selected["reference"] = _pin(selected["reference"])
    before, after = policy["before"]["contract"], policy["after"]["contract"]
    _require(_same(before["target"], after["target"]) and _same(before["selector"], after["selector"]),
             "fixed transition needs the same exact target and owned window selector")
    _require(not _same(before["state"], after["state"]), "before and after FULL semantic states must differ")
    _require(policy["before"]["reference"]["file"] != policy["after"]["reference"]["file"] and
             policy["before"]["reference"]["sha256"] != policy["after"]["reference"]["sha256"],
             "distinct independently frozen complete state contract files required")
    _require(len(_json_bytes(policy)) <= MAX_POLICY_BYTES, "fixed state-task policy exceeds byte bound")
    return policy


def reference_material(policy, role, raw):
    """Pure binding of supplied reference bytes; never open or discover a file.

    This proves only that these bytes encode the declared complete contract.
    It does not authenticate independent authorship or validate any trial.
    """
    policy = validate_policy(policy)
    _require(type(role) is str and role in {"before", "after"} and type(raw) is bytes, "explicit state reference bytes required")
    selected, pin = policy[role]["contract"], policy[role]["reference"]
    _require(len(raw) == pin["bytes"] and hashlib.sha256(raw).hexdigest() == pin["sha256"], "state reference bytes differ from frozen file pin")
    def unique(pairs):
        value = {}
        for key, entry in pairs:
            _require(key not in value, "duplicate state reference JSON key")
            value[key] = entry
        return value
    def invalid(_): raise ValueError("nonfinite state reference JSON")
    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    _require(_same(SR.for_policy(policy).validate_contract(parsed), selected), "reference file does not contain the entire independently declared contract")
    return copy.deepcopy(parsed)


def validate_case(policy, case):
    policy, case = validate_policy(policy), copy.deepcopy(case)
    _keys(case, {"format", "id", "phase", "inputs", "deployment", "evidence"})
    _identifier(case["id"])
    _require(case["format"] == CASE and type(case["phase"]) is str and case["phase"] in PHASES, "explicit supported trial phase required")
    validate_inputs(case["inputs"])
    deployment = case["deployment"]
    _keys(deployment, {"id", "kind", "identity", "restart_from", "restart_evidence"})
    _identifier(deployment["id"])
    _require(type(deployment["kind"]) is str and deployment["kind"] in {"existing_process", "restart"}, "explicit supported deployment kind required")
    deployment["identity"] = S.validate_identity(deployment["identity"])
    _require(deployment["identity"]["bundle_id"] == policy["before"]["contract"]["target"]["bundle_id"], "deployment identity differs from the fixed target")
    if deployment["kind"] == "restart":
        _identifier(deployment["restart_from"])
        deployment["restart_evidence"] = _pin(deployment["restart_evidence"])
        _require(deployment["restart_from"] != case["id"], "restart cannot cite its own case")
    else:
        _require(deployment["restart_from"] is None and deployment["restart_evidence"] is None,
                 "existing-process repetition cannot claim restart evidence")
    _keys(case["evidence"], REQUIRED_EVIDENCE)
    case["evidence"] = {role: _pin(pin) for role, pin in case["evidence"].items()}
    return case


def _evidence(case):
    yield from case["evidence"].items()
    if case["deployment"]["restart_evidence"] is not None:
        yield "restart_evidence", case["deployment"]["restart_evidence"]


def validate_plan(policy, plan):
    """Check explicit phases and references, not whether any trial ran/passed."""
    policy, plan = validate_policy(policy), copy.deepcopy(plan)
    _keys(plan, {"format", "policy_sha256", "cases"})
    _require(plan["format"] == PLAN and plan["policy_sha256"] == digest(policy), "plan differs from the frozen fixed task policy")
    _keys(plan["cases"], PHASES)
    ids, deployments, epochs, seen, files = set(), {}, {}, {}, set()
    reserved = {policy[role]["reference"]["file"] for role in ("before", "after")}
    heldout_restarts = 0
    for phase in PHASES:
        cases = plan["cases"][phase]
        _require(type(cases) is list and MINIMUMS[phase] <= len(cases) <= MAX_CASES, "each declared phase needs3..32 trial cases")
        for index, case in enumerate(cases):
            case = validate_case(policy, case)
            _require(case["phase"] == phase and case["id"] not in ids, "case phase or distinct trial ID differs")
            deployment, identity = case["deployment"], case["deployment"]["identity"]
            key = digest(identity)
            _require(deployment["id"] not in deployments or _same(deployments[deployment["id"]], identity),
                     "one deployment ID cannot name multiple process epochs")
            _require(key not in epochs or epochs[key] == deployment["id"], "relabelled deployment IDs do not create independent processes")
            if deployment["kind"] == "restart":
                old_case = seen.get(deployment["restart_from"])
                _require(old_case is not None and key not in epochs, "restart needs an earlier declared predecessor and a new process epoch")
                old = old_case["deployment"]["identity"]
                _require(all(_same(identity[field], old[field]) for field in ("bundle", "bundle_id", "executable", "uid")) and
                         identity["launch_time"] > old["launch_time"], "restart must preserve the target owner and declare a later launch epoch")
                if phase == "heldout": heldout_restarts += 1
            for role, pin in _evidence(case):
                _require(pin["file"] not in reserved and pin["file"] not in files,
                         "retained execution/settlement/context files cannot be reused as another case or oracle")
                files.add(pin["file"])
            # Fixed states, worker bodies and even complete records may have
            # equal bytes. No hash or actor-input novelty is invented here.
            ids.add(case["id"])
            deployments[deployment["id"]], epochs[key], seen[case["id"]] = identity, deployment["id"], case
            cases[index] = case
    _require(heldout_restarts >= 1, "heldout deployment scope needs at least one explicitly declared restart")
    _require(len(_json_bytes(plan)) <= MAX_POLICY_BYTES, "state trial declaration plan exceeds byte bound")
    return plan


def file_claims(policy, plan):
    """Pure expected file closure for a later alias-safe VM evidence reader."""
    policy, plan = validate_policy(policy), validate_plan(policy, plan)
    pins = {policy[role]["reference"]["file"]: copy.deepcopy(policy[role]["reference"]) for role in ("before", "after")}
    for phase in PHASES:
        for case in plan["cases"][phase]:
            for _, pin in _evidence(case): pins[pin["file"]] = copy.deepcopy(pin)
    return pins


def bookkeeping(policy, plan):
    """Describe required evidence. No ID, count, format or pin is a passed run."""
    policy, plan = validate_policy(policy), validate_plan(policy, plan)
    cases = [case for phase in PHASES for case in plan["cases"][phase]]
    return {"format": BOOKKEEPING, "status": "declarations_only", "policy_sha256": digest(policy), "plan_sha256": digest(plan),
            "declared_cases": {phase: len(plan["cases"][phase]) for phase in PHASES},
            "declared_heldout_restarts": sum(case["deployment"]["kind"] == "restart" for case in plan["cases"]["heldout"]),
            "actor_inputs": {}, "novel_actor_inputs_claimed": False, "input_generalization_claimed": False,
            "scope": "fixed_transition_across_declared_trials_with_heldout_restart", "evidence_status": "pending",
            "trials_validated": 0, "restart_deployment_proven": False, "task_proof_created": False, "public_proof_created": False,
            "required_files": file_claims(policy, plan),
            "pending_bindings": ["independent_reference_provenance", "canonical_file_bytes_and_aliases", "complete_current_identities",
                                 "setup_ready_and_settlement_timing", "runtime_request_results_and_cleanup", "first_pair_complete_settlements",
                                 "exact_before_after_contract_matches", "declared_restart_evidence"],
            "case_ids": [case["id"] for case in cases]}
