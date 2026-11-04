"""Measured fixed-state campaigns for the ordinary REA action catalog.

Trial IDs identify executions, never pretend to be different actor inputs.
By default publication requires three learning, three fresh and three held-out
trials, including an independently witnessed process restart. Explicit trial
policies retain their actual counts and restart coverage. Every promotion
reopens the retained measurements; descriptions and counters cannot create a proof.
"""
from __future__ import annotations

import copy
from pathlib import Path

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_ax_state as S
from . import rea_state_task as T
from . import rea_state_evidence as E

BACKEND = "guest_fixed_state_v1"
PROFILE = "anyuse.rea-fixed-state-profile/1"
PLAN = "anyuse.rea-fixed-state-campaign-plan/1"
SCOPE = "fixed_transition_across_declared_trials_with_heldout_restart"
SCOPE_NO_RESTART = "fixed_transition_across_declared_trials"
PHASES = ("learning", "fresh", "heldout")
DEFAULT_TRIAL_POLICY = {"min_learning": 3, "min_verification": 3, "min_heldout": 3, "require_restart": True}
FAST_TRIAL_POLICY = {"min_learning": 1, "min_verification": 1, "min_heldout": 1, "require_restart": True}


def _need(value, message):
    if not value:
        raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed fixed-state campaign schema required")


def _playback():
    from . import rea_playback_trial as P
    return P


def validate_trial_policy(raw=None):
    """Validate optional proof trial counts (learn, fresh/verification, heldout, restart)."""
    if raw is None:
        return copy.deepcopy(DEFAULT_TRIAL_POLICY)
    if type(raw) is str:
        lowered = raw.strip().lower()
        if lowered in {"default", "strict", "rea-strict"}:
            return copy.deepcopy(DEFAULT_TRIAL_POLICY)
        if lowered in {"fast", "rea-fast"}:
            return copy.deepcopy(FAST_TRIAL_POLICY)
        raise ValueError("trial_policy preset must be 'default', 'strict', or 'fast'")
    _need(type(raw) is dict, "trial_policy must be a dict or preset string")
    aliases = {"learning": "min_learning", "learn": "min_learning",
               "verification": "min_verification", "fresh": "min_verification",
               "heldout": "min_heldout", "restart": "require_restart"}
    allowed = set(DEFAULT_TRIAL_POLICY) | set(aliases) | {"preset"}
    _need(set(raw) <= allowed, "unknown trial_policy field")
    preset = raw.get("preset", "strict")
    _need(type(preset) is str and preset.lower() in {"default", "strict", "rea-strict", "fast", "rea-fast"},
          "unsupported trial_policy preset")
    base = FAST_TRIAL_POLICY if preset.lower() in {"fast", "rea-fast"} else DEFAULT_TRIAL_POLICY
    policy = copy.deepcopy(base)
    seen_canonical = {}
    for k, v in raw.items():
        if k == "preset":
            continue
        canon = aliases.get(k, k)
        if canon in seen_canonical and (type(seen_canonical[canon]) is not type(v) or
                                       seen_canonical[canon] != v):
            raise ValueError(f"conflicting trial_policy values for {canon}")
        seen_canonical[canon] = v
        policy[canon] = v
    for count_key in ("min_learning", "min_verification", "min_heldout"):
        val = policy[count_key]
        _need(type(val) is int and 1 <= val <= 32, f"{count_key} must be an integer in 1..32")
    _need(type(policy["require_restart"]) is bool, "require_restart must be a boolean")
    return policy


def scope_for_trial_policy(raw=None):
    return SCOPE if validate_trial_policy(raw)["require_restart"] else SCOPE_NO_RESTART


def validate_plan(plan):
    """Freeze prospective cases without inventing future artifact hashes."""
    plan = copy.deepcopy(plan)
    _need(type(plan) is dict, "closed fixed-state campaign schema required")
    _keys(plan, {"format", "learning", "verification", "heldout"} |
          ({"trial_policy"} if "trial_policy" in plan else set()))
    _need(plan["format"] == PLAN, "explicit fixed-state campaign plan required")
    tp = validate_trial_policy(plan.get("trial_policy"))
    if "trial_policy" in plan:
        plan["trial_policy"] = tp
    seen = set()
    role_mins = {"learning": tp["min_learning"], "verification": tp["min_verification"], "heldout": tp["min_heldout"]}
    for role in ("learning", "verification", "heldout"):
        rows = plan[role]
        min_n = role_mins[role]
        _need(type(rows) is list and min_n <= len(rows) <= 32, f"each state phase needs {min_n}..32 real trials")
        for row in rows:
            _keys(row, {"id", "inputs", "restart"})
            T._identifier(row["id"])
            T.validate_inputs(row["inputs"])
            _need(row["id"] not in seen and type(row["restart"]) is bool,
                  "distinct trial IDs and explicit restart declarations required")
            _need(not row["restart"] or role == "heldout", "declare restarts in held-out phase")
            seen.add(row["id"])
    if tp["require_restart"]:
        _need(any(row["restart"] for row in plan["heldout"]), "held-outs need a prospectively declared restart")
    return plan


def validate_profile_declaration(profile):
    """Validate an actor- and evidence-free observer declaration, without IO."""
    _keys(profile, {"format", "id", "label", "effect", "params", "policy", "scope"})
    _need(profile["format"] == PROFILE and R.NAME.fullmatch(str(profile["id"])) and
          type(profile["label"]) is str and 0 < len(profile["label"]) <= 4096 and "\0" not in profile["label"],
          "bounded fixed-state profile required")
    _need(profile["effect"] == "fixed_state_transition" and profile["params"] == [] and
          type(profile["params"]) is list and profile["scope"] in (SCOPE, SCOPE_NO_RESTART),
          "fixed state profiles have no actor parameters or input-generalization claim")
    policy = _playback().validate_policy(profile["policy"])
    return {**copy.deepcopy(profile), "policy": policy}


def validate_profile(profile):
    _keys(profile, {"format", "id", "label", "effect", "params", "policy", "scope", "evidence"})
    declaration = validate_profile_declaration({k: v for k, v in profile.items() if k != "evidence"})
    citations = profile["evidence"]
    _need(type(citations) is list and 1 <= len(citations) <= 16, "retained interface evidence required")
    for citation in citations:
        _keys(citation, {"file", "sha256", "pointer"})
        _need(type(citation["file"]) is str and Path(citation["file"]).is_absolute() and
              type(citation["sha256"]) is str and R.SHA.fullmatch(citation["sha256"]) and
              type(citation["pointer"]) is str and citation["pointer"].startswith("/") and
              len(citation["pointer"]) <= 1024, "exact retained interface citation required")
    return {**declaration, "evidence": copy.deepcopy(citations)}


def _entry(entry):
    _keys(entry, {"case", "restart"})
    result = {"case": T._pin(entry["case"]), "restart": None}
    if entry["restart"] is not None:
        result["restart"] = validate_restart_entry(entry["restart"])
    return result


def validate_restart_entry(restart):
    _keys(restart, {"intent", "receipt", "predecessor"})
    T._identifier(restart["predecessor"])
    return {"intent": T._pin(restart["intent"]), "receipt": T._pin(restart["receipt"]),
            "predecessor": restart["predecessor"]}


def validate(candidate):
    _need(type(candidate) is dict, "closed fixed-state campaign schema required")
    _keys(candidate, {"format", "backend", "id", "label", "target", "effect", "params", "actor", "policy",
                      "scope", "evidence", "learning_case_pins"} |
          ({"trial_policy"} if "trial_policy" in candidate else set()))
    _need(candidate["format"] == R.CANDIDATE and candidate["backend"] == BACKEND,
          "unsupported fixed-state candidate")
    profile = validate_profile({key: copy.deepcopy(candidate[key]) for key in
                               ("id", "label", "effect", "params", "policy", "scope", "evidence")} | {"format": PROFILE})
    target = S.validate_target(candidate["target"])
    _need(target == profile["policy"]["target"], "state policy differs from the candidate target")
    actor = B.validate_actor(candidate["actor"])
    _need(actor["kind"] == "objc_method", "this state backend supports one measured Objective-C action")
    tp = validate_trial_policy(candidate.get("trial_policy"))
    _need(candidate["scope"] == scope_for_trial_policy(tp), "candidate scope differs from trial_policy")
    pins = candidate["learning_case_pins"]
    min_learn = tp["min_learning"]
    _need(type(pins) is list and min_learn <= len(pins) <= 32,
          "three measured learning trials required" if min_learn == 3 else f"{min_learn}..32 measured learning trials required")
    out = {**copy.deepcopy(candidate), "target": target, "actor": actor,
           "policy": profile["policy"], "learning_case_pins": [_entry(pin) for pin in pins]}
    if "trial_policy" in candidate:
        out["trial_policy"] = tp
    return out


def bind(candidate, inputs):
    c = validate(candidate)
    T.validate_inputs(inputs)
    return copy.deepcopy(c["actor"])


def _measure(c, entries, phase, reader, seen, artifacts):
    tp = validate_trial_policy(c.get("trial_policy"))
    phase_min = {"learning": tp["min_learning"], "fresh": tp["min_verification"], "heldout": tp["min_heldout"]}[phase]
    _need(type(entries) is list and phase_min <= len(entries) <= 32,
          f"each measured state phase needs {phase_min}..32 trials")
    reports = []
    for raw in entries:
        entry = _entry(raw)
        case = _playback().validate_case(c["policy"], reader.json(entry["case"], "case"))
        _need(case["phase"] == phase and case["id"] not in seen, "state phase or distinct execution ID differs")
        _need(entry["restart"] is None or phase == "heldout", "only held-outs admit restart evidence")
        for pin in case["evidence"].values():
            _need(pin["file"] not in artifacts, "trial artifacts cannot be reused as another execution")
            artifacts.add(pin["file"])
        measurement = _playback().validate_trial(c["policy"], case, c["actor"])
        _need(measurement.get("trial_measurement_validated") is True and
              measurement.get("actor_sha256") == R.digest(c["actor"]) and
              measurement.get("policy_sha256") == R.digest(c["policy"]) and
              measurement.get("case_sha256") == R.digest(case) and measurement.get("target") == c["target"],
              "actual trial measurement does not bind this actor, policy and target")
        files = measurement.get("files")
        _need(type(files) is dict and bool(files), "complete measured trial file closure required")
        for name, pin in files.items():
            _need(name == pin.get("file"), "trial closure file differs from pin")
            old = reader.files.get(name)
            reader.read(pin, old["domain"] if old else "measurement", maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
        if seen:
            previous_id = next(reversed(seen))
            previous = seen[previous_id][1]
            _need(E._moment(previous["finished_monotonic"]) < E._moment(measurement["started_monotonic"]),
                  "state phases must be distinct ordered actual trials")
            if entry["restart"] is not None:
                _need(entry["restart"]["predecessor"] == previous_id and
                      measurement["identity"] != previous["identity"],
                      "restart must bind the immediate measured predecessor and a new process epoch")
            else:
                _need(measurement["identity"] == previous["identity"] and
                      measurement.get("kernel") == previous.get("kernel"),
                      "trial process/kernel changed without a witnessed restart")
        seen[case["id"]] = (case, measurement)
        reports.append(measurement)
        if entry["restart"] is not None:
            _restart(c, entry["restart"], case, measurement, reader, seen)
    return reports


def _restart(c, restart, case, measured, reader, seen):
    """Validate the independent absent-old/actual-new process witness."""
    from . import rea_process_restart as RR
    previous = seen.get(restart["predecessor"])
    _need(previous is not None and previous[0]["id"] != case["id"], "restart needs an earlier measured predecessor")
    declaration = reader.json(restart["intent"], "restart_intent")
    witness = RR.validate_restart(declaration, restart["receipt"])
    _need(witness.get("restart_measurement_validated") is True and witness.get("target") == c["target"] and
          witness.get("old_identity") == previous[1]["identity"] and
          witness.get("new_identity") == measured["identity"] and
          witness.get("old_kernel") == previous[1]["kernel"] and witness.get("new_kernel") == measured["kernel"],
          "restart witness does not bind actual predecessor/new trial process epochs")
    for name, pin in witness["files"].items():
        old = reader.files.get(name)
        reader.read(pin, old["domain"] if old else "restart", maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
    receipt = reader.json(restart["receipt"], reader.files[restart["receipt"]["file"]]["domain"])
    baseline_pin = receipt["baseline"]
    baseline = reader.json(baseline_pin, reader.files[baseline_pin["file"]]["domain"])
    _need(E._moment(previous[1]["finished_monotonic"]) < E._moment(baseline["started_monotonic"]) <=
          E._moment(receipt["finished_monotonic"]) < E._moment(measured["started_monotonic"]),
          "restart must occur between the measured predecessor and held-out trial")


def _campaign(c, phases, *, complete):
    _keys(phases, PHASES)
    _need(phases["learning"] == c["learning_case_pins"], "proof learning cases differ from learned candidate")
    tp = validate_trial_policy(c.get("trial_policy"))
    reader, seen, artifacts = E._Reader(), {}, set()
    reports = {}
    for phase in PHASES if complete else PHASES[:2]:
        reports[phase] = _measure(c, phases[phase], phase, reader, seen, artifacts)
    if complete:
        if tp["require_restart"]:
            _need(any(entry["restart"] is not None for entry in phases["heldout"]),
                  "publication needs an independently witnessed held-out restart")
        restart_proven = any(entry["restart"] is not None for entry in phases["heldout"])
    else:
        _need(phases["heldout"] == [], "fresh verification cannot contain unchecked held-outs")
        restart_proven = False
    _citations(c, reader)
    reader.recheck()
    # Legacy fresh receipts retain their schema/values. Explicit-policy receipts
    # describe only witnessed deployment coverage, including before held-outs.
    scope = SCOPE if restart_proven or ("trial_policy" not in c and not complete) else SCOPE_NO_RESTART
    return {"scope": scope, "phase_counts": {phase: len(rows) for phase, rows in reports.items()},
            **({"trial_policy": tp} if "trial_policy" in c else {}),
            "restart_deployment_proven": restart_proven, "actor_inputs": {}, "input_generalization_claimed": False,
            "native_calls": sum(row["native_calls"] for rows in reports.values() for row in rows),
            "task_invocations": sum(row["invocations"] for rows in reports.values() for row in rows),
            "model_calls": 0}


def _citations(c, reader):
    for citation in c["evidence"]:
        raw = reader.sha_file(citation["file"], citation["sha256"], "interface", R.MAX_JSON_BYTES)
        B._json_pointer(raw, citation["pointer"])


def _folder(bundle, out):
    app, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    _need(not folder.is_relative_to(app), "campaign evidence must be outside the app")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    return app, folder


def learn(profile, demos, bundle, out, *, trial_policy=None):
    R.assert_guest()
    profile = validate_profile(profile)
    target = R.fingerprint(bundle)
    _need(target == profile["policy"]["target"], "state policy target changed")
    tp = validate_trial_policy(trial_policy)
    min_learn = tp["min_learning"]
    _need(type(demos) is list and min_learn <= len(demos) <= 32,
          "three actual learning cases required" if min_learn == 3 else f"{min_learn}..32 actual learning cases required")
    # The actor is supplied once in each retained recording, then checked equal.
    for demo in demos:
        _keys(demo, {"actor", "case"})
    actor = B.validate_actor(demos[0]["actor"])
    _need(all(demo["actor"] == actor for demo in demos), "learning must retain one frozen typed actor")
    c = validate({**{k: copy.deepcopy(v) for k, v in profile.items() if k != "format"},
                  "format": R.CANDIDATE, "backend": BACKEND, "target": target, "actor": actor,
                  "scope": scope_for_trial_policy(tp),
                  "learning_case_pins": [{"case": demo["case"], "restart": None} for demo in demos],
                  **({"trial_policy": tp} if trial_policy is not None else {})})
    reader = E._Reader()
    _measure(c, c["learning_case_pins"], "learning", reader, {}, set())
    _citations(c, reader)
    reader.recheck()
    _, folder = _folder(bundle, out)
    R.write_json(folder / "candidate.json", c)
    R.write_json(folder / "learning.json", {"status": "candidate", "candidate_sha256": R.digest(c),
                                           "recordings": len(demos), "proof_created": False, "model_calls": 0})
    return c


def verify(candidate, cases, bundle, out, timeout=30):
    R.assert_guest()
    c = validate(candidate)
    _need(R.fingerprint(bundle) == c["target"], "state candidate target changed")
    phases = {"learning": c["learning_case_pins"], "fresh": [_entry(case) for case in cases], "heldout": []}
    measured = _campaign(c, phases, complete=False)
    _, folder = _folder(bundle, out)
    proof = {"format": R.PROOF, "backend": BACKEND, "candidate_sha256": R.digest(c), "target": c["target"],
             "status": "fresh_verified", "cases": phases, "measurement": measured}
    R.write_json(folder / "proof.json", proof)
    return proof


def heldout(candidate, proof, cases, bundle, out):
    R.assert_guest()
    c = validate(candidate)
    _need(R.fingerprint(bundle) == c["target"], "state candidate target changed")
    _proof(c, proof, complete=False)
    phases = copy.deepcopy(proof["cases"])
    phases["heldout"] = [_entry(case) for case in cases]
    measured = _campaign(c, phases, complete=True)
    _, folder = _folder(bundle, out)
    result = {"format": R.PROOF, "backend": BACKEND, "candidate_sha256": R.digest(c), "target": c["target"],
              "status": "verified", "cases": phases, "measurement": measured}
    R.write_json(folder / "proof.json", result)
    return result


def _proof(c, proof, *, complete):
    _keys(proof, {"format", "backend", "candidate_sha256", "target", "status", "cases", "measurement"})
    _need(proof["format"] == R.PROOF and proof["backend"] == BACKEND and
          proof["candidate_sha256"] == R.digest(c) and proof["target"] == c["target"] and
          proof["status"] == ("verified" if complete else "fresh_verified"), "matching complete state campaign receipt required")
    measured = _campaign(c, proof["cases"], complete=complete)
    _need(proof["measurement"] == measured, "state campaign summary differs from actual measurements")


def proof_status(candidate, proof, current):
    R.assert_guest()
    try:
        c = validate(candidate)
        _need(current == c["target"], "app fingerprint changed; relearn and reverify")
        _proof(c, proof, complete=True)
        restart = "and witnessed restart" if proof["measurement"].get("restart_deployment_proven", True) else "without witnessed restart"
        return True, f"measured fixed transition, independent playback checks {restart}; local evidence"
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        return False, str(error)


def replay(candidate, proof, inputs, bundle, out, timeout=30, *, expected_identity=None, _disposition=None):
    R.assert_guest()
    R._replay_disposition(_disposition)
    c = validate(candidate)
    bind(c, inputs)
    ready, why = proof_status(c, proof, R.fingerprint(bundle))
    _need(ready, why)
    # No serialized callback, launch, play/reset, or model call. An invalid
    # playing/current-window precondition stops before the native actor.
    result = _playback().run_trial(bundle, c["actor"], c["policy"], None, out,
                                   timeout=timeout,
                                   **({"expected_identity": expected_identity} if expected_identity is not None else {}),
                                   **({"_disposition": _disposition} if _disposition is not None else {}))
    if result.get("resource_review_required"):
        raise R.ResourceReviewRequired("playback native effects or cleanup need review before another action")
    if result.get("status") != "trial_measurement_validated":
        detail = (result.get("error") or {}).get("message", "independent playback observer did not pass")
        raise ValueError("current state replay failed: " + str(detail)[:4096])
    return {"verdict": "verified", "backend": BACKEND, "inputs": {}, "model_calls": 0,
            "candidate_sha256": R.digest(c), "measurement": result["measurement"],
            "case": result["case"], "case_file": result["case_file"], "proof_created": False}
