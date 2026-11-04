"""Shared, explicitly model-judged visible-effect campaigns.

These receipts retain model judgments and their inputs. They do not turn a
visual model's judgment into deterministic verification. Existing deterministic
backends retain their own contracts and admission rules.
"""
from __future__ import annotations

import copy
from itertools import product
from pathlib import Path

from . import rea_mapping as R
from . import rea_harness as H
from . import rea_runtime_providers as P
from . import rea_state_campaign as SC
from . import rea_state_evidence as E
from . import rea_state_task as T

BACKEND = "guest_model_judged_state_v1"
PROFILE = "anyuse.rea-visual-effect-profile/1"
PLAN = "anyuse.rea-visual-effect-plan/1"
SCOPE = "model_judged_visible_effect_across_declared_trials_with_restart"
SCOPE_NO_RESTART = "model_judged_visible_transition_across_declared_trials"
TIER = "model_judged"
PHASES = ("learning", "fresh", "heldout")
DISJOINT_INPUTS = "crossphase_disjoint"
FINITE_INPUTS = "finite_enum_coverage"
DEFAULT_TRIAL_POLICY = SC.DEFAULT_TRIAL_POLICY
FAST_TRIAL_POLICY = SC.FAST_TRIAL_POLICY
validate_trial_policy = SC.validate_trial_policy
_CAMPAIGN_MAX_FILES = 4096
_CAMPAIGN_MAX_TOTAL_BYTES = 2 * 1024**3


def scope_for_trial_policy(raw=None):
    return SCOPE if validate_trial_policy(raw)["require_restart"] else SCOPE_NO_RESTART


class _CampaignReader(E._Reader):
    """Campaign-wide closure with unchanged per-file evidence admission.

    Each read delegates the canonical path, inode, exact pin and role checks to
    the original reader. This wrapper retains their global name/inode history
    while applying its own explicit campaign budget; legacy reader limits and
    the visual producer's smaller per-trial budget remain unchanged.
    """

    def read(self, pin, domain, *, maximum=T.MAX_EVIDENCE_BYTES, retain=True):
        E._keys(pin, {"file", "sha256", "bytes"})
        name, size = pin["file"], pin["bytes"]
        _need(type(name) is str and type(size) is int and 0 <= size <= maximum,
              "exact bounded canonical evidence file pin required")
        previous = self.files.get(name)
        if previous is None:
            _need(len(self.files) < _CAMPAIGN_MAX_FILES and
                  self.total + size <= _CAMPAIGN_MAX_TOTAL_BYTES,
                  "visual campaign file/aggregate byte budget exceeded")
        delegate = E._Reader()
        delegate.files = {} if previous is None else {name: previous}
        delegate.inodes = self.inodes
        result = delegate.read(pin, domain, maximum=maximum, retain=retain)
        self.files[name] = delegate.files[name]
        if previous is None:
            self.total += size
        return result


def _effect():
    from . import rea_visual_effect as V
    return V


def _need(value, message):
    if not value:
        raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed visual campaign schema required")


def _inputs(params, inputs):
    if params:
        H._inputs(params, inputs, check_files=False)
    else:
        _need(type(params) is list and params == [] and type(inputs) is dict and inputs == {},
              "unparameterized visual action has inputs={}")


def validate_plan(plan, params):
    _need(type(plan) is dict, "closed visual campaign schema required")
    _keys(plan, {"format", "learning", "verification", "heldout"} |
          ({"input_mode"} if "input_mode" in plan else set()) |
          ({"trial_policy"} if "trial_policy" in plan else set()))
    _need(plan["format"] == PLAN, "explicit visual-effect trial plan required")
    mode = plan.get("input_mode", DISJOINT_INPUTS)
    _need(type(mode) is str and mode in {DISJOINT_INPUTS, FINITE_INPUTS},
          "explicit supported trial input mode required")
    tp = validate_trial_policy(plan.get("trial_policy"))
    if params:
        H._params(params)
    else:
        _need(type(params) is list and params == [], "frozen typed parameter list required")
    ids, values = set(), set()
    role_mins = {"learning": tp["min_learning"], "verification": tp["min_verification"], "heldout": tp["min_heldout"]}
    for role in ("learning", "verification", "heldout"):
        rows = plan[role]
        min_n = role_mins[role]
        _need(type(rows) is list and min_n <= len(rows) <= 32, f"each phase needs {min_n}..32 actual trials")
        for row in rows:
            _keys(row, {"id", "inputs", "restart"})
            T._identifier(row["id"])
            _inputs(params, row["inputs"])
            _need(row["id"] not in ids and type(row["restart"]) is bool,
                  "distinct trial IDs and explicit restart declarations required")
            _need(not row["restart"] or role == "heldout", "declare restarts in held-out phase")
            digest = R.digest(row["inputs"])
            _need(mode == FINITE_INPUTS or not params or digest not in values,
                  "parameterized trial inputs must be disjoint")
            ids.add(row["id"]); values.add(digest)
    if tp["require_restart"]:
        _need(any(row["restart"] for row in plan["heldout"]), "held-outs require a declared process restart")
    out = copy.deepcopy(plan)
    if "trial_policy" in plan:
        out["trial_policy"] = tp
    return out


def _enum_domain(policy):
    """Return a bounded complete domain only for entirely enumerated inputs."""
    params, constraints = policy["params"], policy["input_constraints"]
    if not params:
        return None
    names, values, size = [], [], 1
    for param in params:
        name = param["name"]
        constraint = constraints.get(name)
        if constraint is None or constraint["kind"] != "enum":
            return None
        size *= len(constraint["values"])
        if size > 32:
            return None
        names.append(name); values.append(constraint["values"])
    return {R.digest(dict(zip(names, combination))) for combination in product(*values)}


def validate_coverage(plan, policy):
    """Check frozen argument domains before any trial/model dispatch."""
    policy = _effect().validate_policy(policy)
    plan = validate_plan(plan, policy["params"])
    domain = _enum_domain(policy)
    finite = plan.get("input_mode", DISJOINT_INPUTS) == FINITE_INPUTS
    if finite:
        _need(domain is not None,
              "finite coverage requires every parameter to have an enum and at most 32 combinations")
    for phase in ("learning", "verification", "heldout"):
        seen = set()
        for row in plan[phase]:
            inputs = _effect().validate_inputs(policy, row["inputs"])
            seen.add(R.digest(inputs))
        if finite:
            _need(seen == domain, "each trial phase must cover the complete frozen enum domain")
    return plan


def validate_profile_declaration(profile):
    _keys(profile, {"format", "id", "label", "effect", "params", "policy", "scope"})
    _need(profile["format"] == PROFILE and R.NAME.fullmatch(str(profile["id"])) and
          H._text(profile["label"]) and bool(profile["label"].strip()) and
          profile["effect"] == "visible_state_transition" and profile["scope"] in (SCOPE, SCOPE_NO_RESTART),
          "bounded visible-effect profile required")
    policy = _effect().validate_policy(profile["policy"])
    _need(profile["params"] == policy["params"], "profile must retain the policy's frozen parameters")
    return {**copy.deepcopy(profile), "policy": policy}


def validate_profile(profile):
    _keys(profile, {"format", "id", "label", "effect", "params", "policy", "scope", "evidence"})
    value = validate_profile_declaration({k: v for k, v in profile.items() if k != "evidence"})
    citations = profile["evidence"]
    _need(type(citations) is list and 1 <= len(citations) <= 16, "retained interface evidence required")
    for citation in citations:
        _keys(citation, {"file", "sha256", "pointer"})
        _need(type(citation["file"]) is str and Path(citation["file"]).is_absolute() and
              type(citation["sha256"]) is str and R.SHA.fullmatch(citation["sha256"]) and
              type(citation["pointer"]) is str and citation["pointer"].startswith("/") and
              len(citation["pointer"]) <= 1024, "exact retained interface citation required")
    return {**value, "evidence": copy.deepcopy(citations)}


def validate_actor(actor, params):
    # The lower producer and every campaign consumer share the same closed
    # recipe/binding contract, including parameterless actors.
    return _effect().validate_actor(actor, params)


def _input_mode(candidate):
    mode = candidate.get("input_mode", FINITE_INPUTS if _enum_domain(candidate["policy"]) is not None else DISJOINT_INPUTS)
    _need(type(mode) is str and mode in {DISJOINT_INPUTS, FINITE_INPUTS}, "supported candidate input mode required")
    _need(mode != FINITE_INPUTS or _enum_domain(candidate["policy"]) is not None,
          "finite candidate requires a bounded complete enum domain")
    return mode


def validate(candidate):
    _need(type(candidate) is dict, "closed visual campaign schema required")
    _keys(candidate, {"format", "backend", "id", "label", "target", "effect", "params", "actor", "policy",
                      "scope", "evidence", "learning_case_pins"} |
          ({"input_mode"} if "input_mode" in candidate else set()) |
          ({"trial_policy"} if "trial_policy" in candidate else set()))
    _need(candidate["format"] == R.CANDIDATE and candidate["backend"] == BACKEND,
          "unsupported model-judged candidate")
    profile = validate_profile({key: copy.deepcopy(candidate[key]) for key in
        ("id", "label", "effect", "params", "policy", "scope", "evidence")} | {"format": PROFILE})
    target = E.S.validate_target(candidate["target"])
    _need(target == profile["policy"]["target"], "visual effect target changed")
    actor = validate_actor(candidate["actor"], profile["params"])
    _input_mode({**candidate, "policy": profile["policy"]})
    tp = validate_trial_policy(candidate.get("trial_policy"))
    _need(candidate["scope"] == scope_for_trial_policy(tp), "candidate scope differs from trial_policy")
    pins = candidate["learning_case_pins"]
    min_learn = tp["min_learning"]
    _need(type(pins) is list and min_learn <= len(pins) <= 32,
          "three actual learning trials required" if min_learn == 3 else f"{min_learn}..32 actual learning trials required")
    out = {**copy.deepcopy(candidate), "target": target, "actor": actor,
           "policy": profile["policy"], "learning_case_pins": [_entry(pin) for pin in pins]}
    if "trial_policy" in candidate:
        out["trial_policy"] = tp
    return out


def bind(candidate, inputs):
    c = validate(candidate)
    _inputs(c["params"], inputs)
    return _effect().bind_actor(c["actor"], c["policy"], inputs)


def _entry(value):
    _keys(value, {"case", "restart"})
    from . import rea_state_campaign as C
    return {"case": T._pin(value["case"]), "restart":
            None if value["restart"] is None else C.validate_restart_entry(value["restart"])}


def _citations(c, reader):
    from . import rea_runtime_bridge as B
    resources = None
    for citation in c["evidence"]:
        old = reader.files.get(citation["file"])
        domain = "interface"
        if old is not None and old["domain"] != domain:
            # A declared actor's observed method receipt is both an interface
            # citation and a pinned invocation dependency. Admit only that
            # exact independently reopened resource, never task evidence.
            if resources is None:
                resources = P.for_actor(c["actor"]).resources(c["actor"])
            resource = resources.get(citation["file"])
            _need(old["domain"] == "measurement" and type(resource) is dict and
                  {key: resource.get(key) for key in ("file", "bytes", "sha256")} == old["pin"] and
                  resource.get("sha256") == citation["sha256"],
                  "interface citation overlaps unrelated task evidence or changed resource")
            domain = old["domain"]
        raw = reader.sha_file(citation["file"], citation["sha256"], domain, R.MAX_JSON_BYTES)
        B._json_pointer(raw, citation["pointer"])


def _restart(c, reader, entry, case, measured, seen):
    from . import rea_state_campaign as C
    C._restart(c, entry, case, measured, reader, seen)


def _measure(c, entries, phase, reader, seen, artifacts, inputs_seen):
    tp = validate_trial_policy(c.get("trial_policy"))
    phase_min = {"learning": tp["min_learning"], "fresh": tp["min_verification"], "heldout": tp["min_heldout"]}[phase]
    _need(type(entries) is list and phase_min <= len(entries) <= 32,
          "three actual trials per phase required" if phase_min == 3 else f"{phase_min}..32 actual trials per phase required")
    reports, phase_inputs = [], set()
    domain = _enum_domain(c["policy"]) if _input_mode(c) == FINITE_INPUTS else None
    for raw in entries:
        entry = _entry(raw)
        case = _effect().validate_case(c["policy"], reader.json(entry["case"], "case"))
        _need(case.get("phase") == phase and case.get("id") not in seen,
              "phase or distinct execution ID differs")
        _inputs(c["params"], case.get("inputs"))
        digest = R.digest(case["inputs"])
        _need(domain is not None or not c["params"] or digest not in inputs_seen,
              "parameterized proof inputs overlap")
        _need(entry["restart"] is None or phase == "heldout", "only held-outs admit a restart")
        for pin in case["evidence"].values():
            _need(pin["file"] not in artifacts, "one trial's artifacts cannot represent another execution")
            artifacts.add(pin["file"])
        measured = _effect().validate_trial(c["policy"], case, c["actor"])
        bound_actor = _effect().bind_actor(c["actor"], c["policy"], case["inputs"])
        _need(measured.get("status") == TIER and measured.get("verification_tier") == TIER and
              measured.get("trial_measurement_validated") is True and
              measured.get("proof_created") is False and measured.get("deterministic_proof_created") is False and
              measured.get("task_proof_created") is False and
              measured.get("actor_sha256") == R.digest(bound_actor) and
              measured.get("policy_sha256") == R.digest(c["policy"]) and
              measured.get("case_sha256") == R.digest(case) and measured.get("target") == c["target"] and
              measured.get("identity") == case["deployment"]["identity"] and
              measured.get("case_id") == case["id"] and measured.get("phase") == phase and
              measured.get("inputs") == case["inputs"] and
              type(measured.get("invocations")) is int and measured["invocations"] == 1 and
              type(measured.get("model_calls")) is int and measured["model_calls"] >= 2,
              "model judgment must bind this actor, policy, target, trial and actual calls")
        started, finished = E._moment(measured.get("started_monotonic")), E._moment(measured.get("finished_monotonic"))
        _need(started < finished and type(measured.get("files")) is dict and bool(measured["files"]),
              "complete ordered model-judged measurement and file closure required")
        for name, pin in measured["files"].items():
            _need(name == pin["file"], "trial closure file differs from its pin")
            old = reader.files.get(name)
            reader.read(pin, old["domain"] if old else "measurement", maximum=64 * 1024**2, retain=False)
        if seen:
            previous_id = next(reversed(seen))
            previous = seen[previous_id]
            _need(previous[1]["finished_monotonic"] < started,
                  "trials must be ordered actual executions")
            if entry["restart"] is not None:
                _need(entry["restart"]["predecessor"] == previous_id and
                      measured["identity"] != previous[1]["identity"],
                      "restart must bind the immediate measured predecessor and a new process epoch")
                _restart(c, reader, entry["restart"], case, measured, seen)
            else:
                _need(measured["identity"] == previous[1]["identity"] and
                      measured.get("kernel") == previous[1].get("kernel"),
                      "trial process/kernel changed without a witnessed restart")
        else:
            _need(entry["restart"] is None, "restart needs an earlier measured predecessor")
        seen[case["id"]] = (case, measured)
        inputs_seen.add(digest); phase_inputs.add(digest); reports.append(measured)
    if domain is not None:
        _need(phase_inputs == domain, "each measured phase must cover the complete frozen enum domain")
    return reports


def _campaign(c, phases, *, complete):
    _keys(phases, PHASES)
    _need(phases["learning"] == c["learning_case_pins"], "learning cases changed")
    tp = validate_trial_policy(c.get("trial_policy"))
    reader, seen, artifacts, inputs_seen = _CampaignReader(), {}, set(), set()
    reports = {phase: _measure(c, phases[phase], phase, reader, seen, artifacts, inputs_seen)
               for phase in PHASES if complete or phase != "heldout"}
    if complete:
        if tp["require_restart"]:
            _need(any(entry["restart"] is not None for entry in phases["heldout"]),
                  "publication requires a witnessed held-out restart")
        restart_proven = any(entry["restart"] is not None for entry in phases["heldout"])
    else:
        _need(phases["heldout"] == [], "publication requires a witnessed held-out restart")
        restart_proven = False
    _citations(c, reader); reader.recheck()
    from . import rea_native_receiver_contract as receiver_declaration
    from . import rea_native_receiver_v2 as receiver_v2
    basis = receiver_declaration.evidence_summary(c["actor"]) or receiver_v2.evidence_summary_v2(c["actor"])
    # Keep omitted-policy fresh receipts compatible; explicit policies report
    # witnessed restart coverage only after that evidence has been checked.
    scope = SCOPE if restart_proven or ("trial_policy" not in c and not complete) else SCOPE_NO_RESTART
    return {"scope": scope, "verification_tier": TIER,
            **({"native_evidence_basis": basis} if basis is not None else {}),
            **({"trial_policy": tp} if "trial_policy" in c else {}),
            "phase_counts": {phase: len(rows) for phase, rows in reports.items()},
            "restart_deployment_proven": restart_proven,
            "input_mode": _input_mode(c),
            "campaign_evidence_limits": {"max_files": _CAMPAIGN_MAX_FILES,
                                         "max_total_bytes": _CAMPAIGN_MAX_TOTAL_BYTES},
            "campaign_evidence_files": len(reader.files), "campaign_evidence_bytes": reader.total,
            "input_scope": "only the declared typed inputs and measured visible effects",
            "task_invocations": sum(row["invocations"] for rows in reports.values() for row in rows),
            "model_calls": sum(row["model_calls"] for rows in reports.values() for row in rows)}


def _folder(bundle, out):
    app, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    _need(not folder.is_relative_to(app), "campaign evidence must be outside the app")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    return app, folder


def learn(profile, demos, bundle, out, *, input_mode=None, trial_policy=None):
    R.assert_guest(); profile = validate_profile(profile)
    target = R.fingerprint(bundle)
    _need(target == profile["policy"]["target"], "visual policy target changed")
    tp = validate_trial_policy(trial_policy)
    min_learn = tp["min_learning"]
    _need(type(demos) is list and min_learn <= len(demos) <= 32,
          "three real learning cases required" if min_learn == 3 else f"{min_learn}..32 real learning cases required")
    for demo in demos:
        _keys(demo, {"actor", "case"})
    actor = validate_actor(demos[0]["actor"], profile["params"])
    _need(all(demo["actor"] == actor for demo in demos), "one frozen actor recipe required")
    c = validate({**{k: copy.deepcopy(v) for k, v in profile.items() if k != "format"},
        "format": R.CANDIDATE, "backend": BACKEND, "target": target, "actor": actor,
        "scope": scope_for_trial_policy(tp),
        "learning_case_pins": [{"case": demo["case"], "restart": None} for demo in demos],
        **({"input_mode": input_mode} if input_mode is not None else {}),
        **({"trial_policy": tp} if trial_policy is not None else {})})
    reader = _CampaignReader(); measured = _measure(c, c["learning_case_pins"], "learning", reader, {}, set(), set())
    _citations(c, reader); reader.recheck(); _, folder = _folder(bundle, out)
    R.write_json(folder / "candidate.json", c)
    R.write_json(folder / "learning.json", {"status": "candidate", "verification_tier": TIER,
        "candidate_sha256": R.digest(c), "recordings": len(demos), "proof_created": False,
        "model_calls": sum(row["model_calls"] for row in measured)})
    return c


def verify(candidate, cases, bundle, out, timeout=120):
    R.assert_guest(); c = validate(candidate)
    _need(R.fingerprint(bundle) == c["target"], "candidate target changed")
    phases = {"learning": c["learning_case_pins"], "fresh": [_entry(case) for case in cases], "heldout": []}
    measured = _campaign(c, phases, complete=False); _, folder = _folder(bundle, out)
    proof = {"format": R.PROOF, "backend": BACKEND, "candidate_sha256": R.digest(c), "target": c["target"],
             "status": "fresh_model_judged", "verification_tier": TIER, "cases": phases, "measurement": measured}
    R.write_json(folder / "proof.json", proof)
    return proof


def heldout(candidate, proof, cases, bundle, out):
    R.assert_guest(); c = validate(candidate)
    _need(R.fingerprint(bundle) == c["target"], "candidate target changed")
    _proof(c, proof, complete=False)
    phases = copy.deepcopy(proof["cases"]); phases["heldout"] = [_entry(case) for case in cases]
    measured = _campaign(c, phases, complete=True); _, folder = _folder(bundle, out)
    result = {"format": R.PROOF, "backend": BACKEND, "candidate_sha256": R.digest(c), "target": c["target"],
              "status": TIER, "verification_tier": TIER, "cases": phases, "measurement": measured}
    R.write_json(folder / "proof.json", result)
    return result


def _proof(c, proof, *, complete):
    _keys(proof, {"format", "backend", "candidate_sha256", "target", "status", "verification_tier", "cases", "measurement"})
    _need(proof["format"] == R.PROOF and proof["backend"] == BACKEND and proof["verification_tier"] == TIER and
          proof["candidate_sha256"] == R.digest(c) and proof["target"] == c["target"] and
          proof["status"] == (TIER if complete else "fresh_model_judged"), "matching model-judged campaign receipt required")
    _need(proof["measurement"] == _campaign(c, proof["cases"], complete=complete),
          "retained campaign summary differs from actual evidence")


def proof_status(candidate, proof, current):
    R.assert_guest()
    try:
        c = validate(candidate); _need(current == c["target"], "app changed; relearn and reevaluate")
        _proof(c, proof, complete=True)
        restart = "and witnessed restart" if proof["measurement"].get("restart_deployment_proven", True) else "without witnessed restart"
        return True, f"model-judged visible effect with retained judgments {restart}; not deterministic verification"
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        return False, str(error)


def replay(candidate, proof, inputs, bundle, out, timeout=120, *, expected_identity=None, _disposition=None):
    R.assert_guest(); R._replay_disposition(_disposition); c = validate(candidate); bind(c, inputs)
    ready, why = proof_status(c, proof, R.fingerprint(bundle)); _need(ready, why)
    intent = _effect().make_intent(bundle, c["policy"], "visual_replay", "replay", inputs,
        **({"expected_identity": expected_identity} if expected_identity is not None else {}))
    result = _effect().run_trial(bundle, c["actor"], c["policy"], intent, out, timeout=timeout,
        **({"expected_identity": expected_identity} if expected_identity is not None else {}),
        **({"_disposition": _disposition} if _disposition is not None else {}))
    if result.get("resource_review_required"):
        raise R.ResourceReviewRequired("visual action effects or cleanup require review")
    _need(result.get("status") == TIER and result.get("error") is None,
          "current visible effect did not pass its model judgment")
    recorded = E._Reader().json(T._pin(result["case_file"]), "case")
    _need(recorded == result["case"], "replay case differs from its retained artifact")
    measured = _effect().validate_trial(c["policy"], recorded, c["actor"])
    _need(measured == result["measurement"] and measured.get("verification_tier") == TIER and
          result["model_calls"] == measured["model_calls"],
          "current replay result differs from the retained model-judged measurement")
    return {"verdict": TIER, "verification_tier": TIER, "backend": BACKEND, "inputs": copy.deepcopy(inputs),
            "model_calls": result["model_calls"], "candidate_sha256": R.digest(c),
            "measurement": result["measurement"], "case": result["case"], "case_file": result["case_file"],
            "proof_created": False}
