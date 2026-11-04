"""One trusted fixed-state trial; no campaign, publication or JSON callbacks.

The authorized caller supplies setup code and freezes its producer provenance.
That local pin is not callable authentication. Ordinary setup remains bounded
and separately authorized; no JSON flag can grant that authority. The default
path retains its historical armed-worker behavior. Explicit private ACK mode
requires a retained one-use permission immediately before target entry; actual
outcomes still come only from actual worker reports.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import time

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_native_path as P
from . import rea_ax_state as S
from . import rea_state_reader as SR
from . import rea_state_task as T
from . import rea_state_evidence as E

INTENT = "anyuse.rea-fixed-state-trial-intent/1"
INTENT2 = "anyuse.rea-fixed-state-trial-intent/2"
RESTART_ADMISSION = "anyuse.rea-state-restart-admission/1"
ATTEMPT = "anyuse.rea-fixed-state-trial-attempt/1"
REPORT = "anyuse.rea-fixed-state-trial-outcome/1"
REPORT2 = "anyuse.rea-fixed-state-trial-outcome/2"
REPORT3 = "anyuse.rea-fixed-state-trial-outcome/3"
REPORT4 = "anyuse.rea-fixed-state-trial-outcome/4"
EVIDENCE_FILES = {"before_settlement": "before-settlement.json", "execution": "phased.json",
                  "worker": "runtime/worker-output.json", "after_settlement": "after-settlement.json",
                  "run_context": "run-context.json"}


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed fixed-state trial schema required")


def validate_intent(intent):
    """Pure prospective declaration; never manufacture a post-run T.CASE."""
    intent = copy.deepcopy(intent)
    _keys(intent, {"format", "id", "phase", "actor_sha256", "policy_sha256", "setup", "deployment"})
    _need(intent["format"] in (INTENT, INTENT2) and type(intent["phase"]) is str and intent["phase"] in T.PHASES,
          "explicit supported fixed-state trial intent required")
    T._identifier(intent["id"])
    for key in ("actor_sha256", "policy_sha256"):
        _need(type(intent[key]) is str and R.SHA.fullmatch(intent[key]), "exact frozen actor/policy digest required")
    _keys(intent["setup"], {"id", "producer"})
    T._identifier(intent["setup"]["id"])
    intent["setup"]["producer"] = T._pin(intent["setup"]["producer"])
    restarted = intent["format"] == INTENT2
    _keys(intent["deployment"], {"id", "kind"} |
          ({"predecessor_case", "restart_intent", "restart_receipt"} if restarted else set()))
    T._identifier(intent["deployment"]["id"])
    if restarted:
        _need(intent["deployment"]["kind"] == "restart", "intent2 admits only an explicit prospective restart")
        for name in ("predecessor_case", "restart_intent", "restart_receipt"):
            intent["deployment"][name] = T._pin(intent["deployment"][name])
        names = [intent["deployment"][key]["file"] for key in ("predecessor_case", "restart_intent", "restart_receipt")]
        _need(len(set(names)) == len(names), "restart declaration roles must name distinct files")
    else:
        _need(intent["deployment"]["kind"] == "existing_process", "this trial runner does not admit restart declarations")
    return intent


def _adopt_files(reader, files, domain):
    """Retain actual validator closures, sharing only already identical pins.

    Each validator independently enforces its semantic role separation. The
    common references/resources/source pins may legitimately be shared; the
    combined reader still rejects changed pins, hardlinks and symlink aliases.
    """
    _need(type(files) is dict and bool(files), "actual validated file closure required")
    for name, pin in files.items():
        _need(type(pin) is dict and pin.get("file") == name, "validated file closure key differs from its pin")
        previous = reader.files.get(name)
        reader.read(pin, previous["domain"] if previous is not None else domain,
                    maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)


def _restart_admission(reader, actor, policy, intent, identity, target):
    """Independently validate a real retained witness BEFORE native dispatch.

    This is fixed local code, never a model/plugin/import path from intent.
    Producer/source pins describe provenance and grant no callback authority.
    No old process is looked up and no stop/launch is performed here.
    """
    from . import rea_process_restart as RR
    deployment = intent["deployment"]
    _need(intent["format"] == INTENT2 and deployment["kind"] == "restart", "prospective restart intent required")
    predecessor = T.validate_case(policy, reader.json(deployment["predecessor_case"], "restart_predecessor_case"))
    _need(predecessor["id"] != intent["id"] and predecessor["deployment"]["id"] != deployment["id"],
          "restart requires distinct predecessor case and deployment IDs")
    # An earlier restart predecessor would need recursive historical admission.
    # This first explicit boundary intentionally admits one witnessed restart.
    _need(predecessor["deployment"]["kind"] == "existing_process", "one measured existing-process predecessor required")
    previous = E.validate_trial(copy.deepcopy(policy), copy.deepcopy(predecessor), copy.deepcopy(actor))
    _need(type(previous) is dict and previous.get("trial_measurement_validated") is True and
          previous.get("actor_sha256") == R.digest(actor) and previous.get("policy_sha256") == T.digest(policy) and
          previous.get("case_sha256") == T.digest(predecessor), "predecessor is not this actor/policy's actual measured trial")
    _adopt_files(reader, previous.get("files"), "restart_predecessor")
    declaration = reader.json(deployment["restart_intent"], "restart_intent")
    witness = RR.validate_restart(copy.deepcopy(declaration), copy.deepcopy(deployment["restart_receipt"]))
    _need(type(witness) is dict and witness.get("format") == RR.REPORT and
          witness.get("restart_measurement_validated") is True and
          P.same_data(witness.get("receipt"), deployment["restart_receipt"]) and
          witness.get("intent_sha256") == R.digest(declaration), "independent retained restart measurement required")
    _adopt_files(reader, witness.get("files"), "restart_witness")
    _need(P.same_data(witness["target"], target) and
          P.same_data(witness["old_identity"], predecessor["deployment"]["identity"]) and
          P.same_data(witness["new_identity"], identity) and not P.same_data(identity, witness["old_identity"]),
          "restart old/new epochs do not bind predecessor and exact current target")
    context_pin = predecessor["evidence"]["run_context"]
    context = reader.json(context_pin, reader.files[context_pin["file"]]["domain"])
    settlement_pin = predecessor["evidence"]["after_settlement"]
    settled = reader.json(settlement_pin, reader.files[settlement_pin["file"]]["domain"])
    # E already proved that every retained complete snapshot has the same epoch.
    old_kernel = settled["samples"][settled["stable_pair"][0]]["observation"]["context"]["before"]["kernel"]
    _need(P.same_data(old_kernel, witness["old_kernel"]), "restart old kernel differs from the measured predecessor")
    receipt_pin = deployment["restart_receipt"]
    raw = reader.json(receipt_pin, reader.files[receipt_pin["file"]]["domain"])
    baseline_pin = raw["baseline"]
    baseline = reader.json(baseline_pin, reader.files[baseline_pin["file"]]["domain"])
    previous_end = E._moment(context["phases"]["after"]["finished_monotonic"])
    restart_start, restart_end = E._moment(baseline["started_monotonic"]), E._moment(raw["finished_monotonic"])
    checked = E._moment(time.monotonic())
    _need(previous_end < restart_start <= restart_end < checked,
          "restart must follow the measured predecessor and finish before admission/dispatch")
    reader.recheck()
    return {"format": RESTART_ADMISSION, "status": "independently_validated_before_dispatch",
        "intent_sha256": R.digest(intent), "predecessor_case": copy.deepcopy(deployment["predecessor_case"]),
        "predecessor_case_id": predecessor["id"], "predecessor_measurement": copy.deepcopy(previous),
        "restart_intent": copy.deepcopy(deployment["restart_intent"]),
        "restart_receipt": copy.deepcopy(receipt_pin), "restart_measurement": copy.deepcopy(witness),
        "old_identity": copy.deepcopy(witness["old_identity"]), "new_identity": copy.deepcopy(identity),
        "predecessor_finished_monotonic": previous_end, "restart_started_monotonic": restart_start,
        "restart_finished_monotonic": restart_end, "checked_monotonic": checked,
        "proof_created": False, "task_proof_created": False, "public_proof_created": False,
        "aggregation_validated": False, "input_generalization_claimed": False}


def _restart_trial_binding(reader, evidence, admission, identity):
    """Join the actual new complete trial back to its admitted restart epoch."""
    context = reader.json(evidence["run_context"], "trial")
    before = reader.json(evidence["before_settlement"], "trial")
    current = before["samples"][before["stable_pair"][0]]["observation"]["context"]["before"]
    _need(P.same_data(current["identity"], identity) and
          P.same_data(current["kernel"], admission["restart_measurement"]["new_kernel"]) and
          admission["checked_monotonic"] <= E._moment(context["worker_anchor_monotonic"]),
          "actual new trial kernel/identity/time differs from its pre-dispatch restart admission")


def _pin(path):
    path = Path(path)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.lstat().st_size}


def _save(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    _need(len(raw) <= R.MAX_JSON_BYTES, "fixed-state trial journal exceeds byte bound")
    pending = path.with_name("." + path.name + ".pending")
    with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def _error(error):
    return {"type": type(error).__name__, "message": str(error)[:4096]}


def _resources(reader, actor, request):
    """Use B's complete walker, then strict selected JSON before dispatch."""
    resources = B.request_resources(request)
    claims = B.native_file_claims(actor)
    _need(type(resources) is dict and set(resources) == set(claims), "complete original resource closure required")
    for name, sha in claims.items():
        row = resources[name]
        _need(type(row) is dict and row.get("file") == name and row.get("sha256") == sha,
              "original file pin differs from the frozen actor")
        reader.read({key: row[key] for key in ("file", "sha256", "bytes")}, "resource",
                    maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
    def selected(claim):
        row = resources[claim["file"]]
        body = reader.json({key: row[key] for key in ("file", "sha256", "bytes")},
                           "resource", maximum=B.MAX_RESULT_BYTES)
        return B._json_pointer(body, claim["pointer"])
    if actor["kind"] == "native_function":
        _need(P.same_data(selected(actor["signature_evidence"]), B._contract(actor)), "typed original actor ABI differs")
    # Qt's public dispatcher ABI, exact selected method and primary QObject
    # base are admitted as the path's own consumer contract below. There is
    # no synthetic native_function declaration or user-supplied argv here.
    for recipe in B.request_native_path_recipes(request):
        for evidence in P.evidence_plan(recipe):
            _need(P.same_data(selected(evidence["claim"]), evidence["contract"]), "typed original path contract differs")
            for original in evidence["originals"]:
                if original["role"] == "independent_review":
                    row = resources[original["file"]]
                    reader.json({key: row[key] for key in ("file", "sha256", "bytes")},
                                "resource", maximum=B.MAX_RESULT_BYTES)
    return resources


def _adapter_files(reader, adapter):
    reader.read({key: adapter[key] for key in ("file", "sha256", "bytes")}, "resource",
                maximum=B.MAX_AGENT_BYTES, retain=False)
    reader.json(adapter["manifest"], "resource", maximum=B.MAX_REQUEST_BYTES)
    source = Path(B.__file__).with_name("rea_runtime_agent.js").resolve(strict=True)
    reader.read({"file": str(source), "sha256": adapter["source_sha256"], "bytes": source.lstat().st_size},
                "resource", maximum=B.MAX_AGENT_BYTES, retain=False)


def _current(bundle, target, identity, request, resources, adapter, reader):
    reader.recheck()
    _need(P.same_data(R.fingerprint(bundle), target), "fixed-state target changed")
    _need(P.same_data(B.process_identity(bundle, target["bundle_id"]), identity),
          "current process differs from the frozen full identity; no successor adoption")
    _need(P.same_data(B.request_resources(request), resources) and
          P.same_data(B.adapter_identity(backend="local"), adapter), "fixed-state runtime/original resources changed")


def _disposition(folder, wrapper, expected_request_sha256, *, observer_capability=None,
                 require_pre_actuation_ack=False):
    """Preserve request-bound producer reports, never an E success verdict.

    A rejected worker can still report actual calls. Raw counts additionally
    require this run's independent naturally reaped worker context. Neither
    source supplies a clean resource or execution proof; conflicting sources
    remain unknown and retain both available file pins and reported values.
    """
    capability = SR.strictcap() if observer_capability is None else observer_capability
    SR.for_capability(capability)  # Closed capability selection before any file IO.
    _need(type(require_pre_actuation_ack) is bool, "an exact private acknowledgement option is required")
    result = {"receipt": None, "runtime_status": None, "worker_status": None,
              "native_calls": None, "invocations": None, "resource_review_required": None,
              "owned_worker": None, "phase_context": None, "phase_errors": None,
              "raw_worker": None, "raw_worker_provenance": None, "resource_review_provenance": None,
              "worker_reports": {"runtime_result": None, "raw_worker": None},
              "worker_conflict": False, "owned_context_confirmed": False,
              "reported_counts_provenance": None, "read_error": None, "read_errors": []}
    reader, embedded, raw, raw_exists = E._Reader(), None, None, False
    def reported(worker):
        _need(type(expected_request_sha256) is str and R.SHA.fullmatch(expected_request_sha256) and
              type(worker) is dict and worker.get("format") == B.WORKER and
              worker.get("request_sha256") == expected_request_sha256 and
              type(worker.get("status")) is str and worker["status"] in {"completed", "incomplete", "unavailable"},
              "reported worker differs from the frozen request/current format")
        for key in ("native_calls", "invocations"):
            _need(key in worker and (worker[key] is None or type(worker[key]) is int and worker[key] >= 0),
                  "reported worker counts must be exact nonnegative integers or unknown")
        return {key: copy.deepcopy(worker[key]) for key in ("status", "native_calls", "invocations")}
    try:
        path = folder / "runtime/runtime.json"
        pin = _pin(path)
        runtime = reader.json(pin)
        result["receipt"] = pin
        embedded = runtime.get("result") if type(runtime) is dict else None
        _need(type(runtime) is dict and runtime.get("format") == B.RUNTIME and
              runtime.get("request_sha256") == expected_request_sha256,
              "retained native receipt differs from the frozen request/current format")
        result["runtime_status"] = runtime.get("status")
        review = runtime.get("resource_review_required")
        result["resource_review_required"] = review if type(review) is bool else None
        if embedded is not None:
            result["worker_reports"]["runtime_result"] = reported(embedded)
    except Exception as error:
        result["read_errors"].append(_error(error))
    try:
        if (folder / "run-context.json").exists():
            pin = _pin(folder / "run-context.json")
            context = reader.json(pin)
            result["phase_context"] = pin
        else:
            context = wrapper.get("run_context") if type(wrapper) is dict else None
        if type(context) is dict:
            context_format = (B.PHASE_CONTEXT3 if require_pre_actuation_ack else
                              B.PHASE_CONTEXT if capability == SR.strictcap() else B.PHASE_CONTEXT2)
            _need(context.get("format") == context_format and
                  (not (require_pre_actuation_ack or capability != SR.strictcap()) or
                   context.get("observer_capability") == capability) and
                  context.get("request_sha256") == expected_request_sha256,
                  "owned worker context differs from the frozen request/current format")
            if type(context.get("owned_worker")) is dict:
                result["owned_worker"] = copy.deepcopy(context["owned_worker"])
            if type(context.get("errors")) is list:
                result["phase_errors"] = copy.deepcopy(context["errors"])
            owned = result["owned_worker"]
            result["owned_context_confirmed"] = (result["phase_context"] is not None and type(owned) is dict and
                type(owned.get("pid")) is int and 1 <= owned["pid"] < 2**31 and
                owned.get("reaped") is True and type(owned.get("returncode")) is int and owned["returncode"] == 0 and
                owned.get("term_attempted") is False and owned.get("kill_attempted") is False and
                P.same_data(owned.get("errors"), []))
    except Exception as error:
        result["read_errors"].append(_error(error))
    try:
        path = folder / "runtime/worker-output.json"
        info = path.lstat()
        raw_exists = True
        _need(path.resolve(strict=True) == path and path.is_file() and info.st_nlink == 1 and
              0 <= info.st_size <= B.MAX_RESULT_BYTES, "bounded canonical unaliased raw worker file required")
        pin = _pin(path)
        raw_bytes = reader.read(pin, "trial", maximum=B.MAX_RESULT_BYTES)
        result["raw_worker"] = pin
        raw = E._json(raw_bytes)
        result["worker_reports"]["raw_worker"] = reported(raw)
        result["raw_worker_provenance"] = "request_bound_raw_report_not_validated_execution"
        if raw.get("native_state_uncertain") is True:
            result["resource_review_required"] = True
            result["resource_review_provenance"] = "raw_worker_reports_native_state_uncertain"
    except Exception as error:
        result["read_errors"].append(_error(error))
    if embedded is not None and raw_exists and not P.same_data(embedded, raw):
        result["worker_conflict"] = True
        result["read_errors"].append(_error(ValueError(
            "runtime result conflicts with raw worker output; reported counts remain unknown")))
    reports = result["worker_reports"]
    selected = reports["runtime_result"] if reports["runtime_result"] is not None else reports["raw_worker"]
    if not result["worker_conflict"] and selected is not None:
        result["worker_status"] = selected["status"]
        if result["owned_context_confirmed"]:
            for key in ("native_calls", "invocations"): result[key] = selected[key]
            result["reported_counts_provenance"] = (
                "runtime_result_and_raw_worker_reports" if all(value is not None for value in reports.values()) else
                "runtime_result_report" if reports["runtime_result"] is not None else "request_bound_raw_worker_report")
    try:
        reader.recheck()
    except Exception as error:
        result["native_calls"] = result["invocations"] = None
        result["reported_counts_provenance"] = None
        result["read_errors"].append(_error(error))
    result["read_error"] = result["read_errors"][0] if result["read_errors"] else None
    return result


def run_trial(bundle, actor, policy, intent, out, *, expected_identity, setup_hook, limits, timeout=35,
              require_pre_actuation_ack=False):
    """VM-only single trusted run. No H/A/campaign or public-proof promotion.

    setup_hook(context) is authorized, bounded host code, never deserialized
    from intent. It preserves its ordinary-action issuance/result and raises on
    refusal/uncertainty. Its producer pin is provenance, not authentication.
    """
    R.assert_guest()  # Before paths, backend acquisition, callback or writes.
    _need(type(require_pre_actuation_ack) is bool, "an exact trusted private acknowledgement option is required")
    actor, policy, intent, expected_identity, limits = copy.deepcopy((actor, policy, intent, expected_identity, limits))
    _need(callable(setup_hook), "a fixed trusted host setup callback is required")
    actor, policy, intent = B.validate_actor(actor), T.validate_policy(policy), validate_intent(intent)
    restarted = intent["format"] == INTENT2
    _need(not restarted or require_pre_actuation_ack is True,
          "prospective restart trials require the trusted private acknowledgement option")
    capability, observer = SR.capability(policy), SR.for_policy(policy)
    owned_observer = capability == SR.ownedcap()
    identity, bounds = B._expected_identity(expected_identity), B._phase_limits(limits)
    _need(identity is not None, "one measured full current identity is required")
    identity = dict(identity)
    _need(type(timeout) is int and 1 <= timeout <= 60, "trial timeout must be1..60 seconds")
    _need(intent["actor_sha256"] == R.digest(actor) and intent["policy_sha256"] == T.digest(policy),
          "trial intent differs from the frozen actor/policy")
    _need(actor["kind"] in {"native_function", "qt_moc_method"}, "trial requires one actual typed native or Qt action")
    request = B.invocation_request(actor)
    paths = B.request_native_path_recipes(request)
    _need(len(request["operations"]) == 1 and len(paths) == 1 and P.is_lifecycle_root(paths[0]),
          "trial requires one immediate lifecycle-root native action")
    B._require_native_path_backend(request)
    B._lifecycle_budget(request, timeout * 1000 - 100)
    _need(bounds["margin_seconds"] < paths[0]["root"]["lifecycle"]["observe_ms"] / 1000,
          "phase margin leaves no conservative pre-terminal window")
    app = Path(bundle).resolve(strict=True)
    target = R.fingerprint(app)
    _need(P.same_data(target, policy["before"]["contract"]["target"]) and
          identity["bundle"] == str(app) and identity["bundle_id"] == target["bundle_id"],
          "policy/current identity differs from the actual target")
    reader = E._Reader()
    for role in ("before", "after"):
        T.reference_material(policy, role, reader.read(policy[role]["reference"], "reference"))
    reader.read(intent["setup"]["producer"], "setup_provenance", maximum=R.MAX_JSON_BYTES).decode("utf-8")
    resources = _resources(reader, actor, request)
    adapter = B.adapter_identity(backend="local")
    _adapter_files(reader, adapter)
    _current(app, target, identity, request, resources, adapter, reader)
    restart_admission = None
    if restarted:
        source = Path(__file__).resolve(strict=True)
        reader.read(_pin(source), "restart_admission_source", maximum=R.MAX_JSON_BYTES, retain=False)
        restart_admission = _restart_admission(reader, actor, policy, intent, identity, target)
    name = str(out)
    root = Path(name)
    _need(root.is_absolute() and str(root) == name and ".." not in root.parts and
          root.parent.resolve(strict=True) == root.parent and not root.is_relative_to(app),
          "trial output must be a fresh canonical directory outside the app")
    inputs = {"actor": actor, "policy": policy, "intent": intent, "expected_identity": identity,
              "request": request, "limits": bounds, "timeout": timeout, "target": target,
              "adapter": adapter, "native_resources": resources,
              "files": {name: copy.deepcopy(row["pin"]) for name, row in reader.files.items()}}
    if owned_observer: inputs["observer_capability"] = capability
    if require_pre_actuation_ack:
        inputs.update(observer_capability=capability, require_pre_actuation_ack=True)
    if restarted: inputs["restart_admission"] = copy.deepcopy(restart_admission)
    # Check journal serialization before creating output or attempting B.
    _need(len(json.dumps(inputs, ensure_ascii=False, allow_nan=False).encode()) <= R.MAX_JSON_BYTES // 2,
          "frozen trial inputs exceed journal byte bound")
    root.mkdir(mode=0o700, exist_ok=False)
    _save(root / "inputs.json", inputs)
    _save(root / "intent.json", intent)
    journals = {name: _pin(root / name) for name in ("inputs.json", "intent.json")}
    if restarted:
        _save(root / "restart-admission.json", restart_admission)
        journals["restart-admission.json"] = _pin(root / "restart-admission.json")
    attempt = {"format": ATTEMPT, "status": "running", "intent_sha256": R.digest(intent),
               "inputs": journals["inputs.json"], "collection_attempted": False,
               "errors": [], "proof_created": False}
    _save(root / "attempt.json", attempt)
    selector, phases, used = copy.deepcopy(policy["before"]["contract"]["selector"]), root / "phases", []
    def hook(phase, context):
        _need(P.same_data(context["target"], target) and P.same_data(context["expected_identity"], identity) and
              context["request_sha256"] == R.digest(request) and context["phase"] == phase,
              "phase context differs from the frozen trial")
        _need(context.get("observer_capability") == capability if owned_observer or require_pre_actuation_ack else
              "observer_capability" not in context,
              "phase observer differs from the frozen policy capability")
        _need(phase in ("setup", "before", "after") and phase not in used, "trial phase may be used only once")
        used.append(phase)
        if phase == "setup": return setup_hook(copy.deepcopy(context))
        returned = observer.settle(copy.deepcopy(target), copy.deepcopy(identity), copy.deepcopy(selector),
                                  deadline=context["deadline_monotonic"], interval=.4, max_samples=16)
        # B first persists the WHOLE raw return, then validates the fixed reader + completeTrue.
        # Raising here would discard an honest failure settlement's samples.
        return returned
    wrapper = measurement = case = interrupted = None
    case_pin = measurement_pin = None
    outputs = validated_files = None
    review_required = False
    try:
        _current(app, target, identity, request, resources, adapter, reader)
        if restarted:
            # Independently rerun BOTH historical validators and reopen all
            # source/witness files immediately before the single dispatch.
            rechecked = _restart_admission(reader, actor, policy, intent, identity, target)
            _need(P.same_data({key: value for key, value in rechecked.items() if key != "checked_monotonic"},
                              {key: value for key, value in restart_admission.items() if key != "checked_monotonic"}) and
                  rechecked["checked_monotonic"] >= restart_admission["checked_monotonic"],
                  "restart admission changed after its prospective freeze")
            reader.recheck()
        attempt["collection_attempted"] = True
        _save(root / "attempt.json", attempt)
        observer_options = {"observer_capability": capability} if owned_observer else {}
        if require_pre_actuation_ack:
            observer_options.update(observer_capability=capability, require_pre_actuation_ack=True,
                                    observer_selector=copy.deepcopy(selector))
        wrapper = B.collect_phased(app, phases, request, expected_identity=copy.deepcopy(identity), phase_hook=hook,
                                   limits=copy.deepcopy(bounds), timeout=timeout, backend="local", **observer_options)
        evidence = {}
        outputs = E._Reader()
        for role, relative in EVIDENCE_FILES.items():
            pin = _pin(phases / relative)
            outputs.read(pin, "trial", maximum=B.MAX_RESULT_BYTES if role == "worker" else T.MAX_EVIDENCE_BYTES,
                         retain=False)
            evidence[role] = pin
        deployment = ({"id": intent["deployment"]["id"], "kind": "restart", "identity": copy.deepcopy(identity),
                       "restart_from": restart_admission["predecessor_case_id"],
                       "restart_evidence": copy.deepcopy(intent["deployment"]["restart_receipt"])} if restarted else
                      {**intent["deployment"], "identity": copy.deepcopy(identity), "restart_from": None, "restart_evidence": None})
        case = T.validate_case(policy, {"format": T.CASE, "id": intent["id"], "phase": intent["phase"], "inputs": {},
                                      "deployment": deployment, "evidence": evidence})
        _save(root / "case.json", case)
        case_pin = _pin(root / "case.json")
        candidate = E.validate_trial(copy.deepcopy(policy), copy.deepcopy(case), copy.deepcopy(actor))
        _need(type(candidate) is dict and candidate.get("format") ==
              (E.REPORT3 if require_pre_actuation_ack else E.REPORT2 if owned_observer else E.REPORT) and
              (candidate.get("observer_capability") == capability and
               candidate.get("state_observation_scope") == "selected_owned_visible_ax_window_tree" if owned_observer else
               (candidate.get("observer_capability") == capability if require_pre_actuation_ack else
                "observer_capability" not in candidate) and "state_observation_scope" not in candidate) and
              (candidate.get("pre_actuation_ack_required") is True and
               candidate.get("pre_actuation_ack_validated") is True and
               type(candidate.get("pre_actuation_ack")) is dict if require_pre_actuation_ack else
               not any(key in candidate for key in ("pre_actuation_ack_required", "pre_actuation_ack_validated",
                                                    "pre_actuation_ack"))) and
              candidate.get("trial_measurement_validated") is True and
              candidate.get("actor_sha256") == R.digest(actor) and candidate.get("policy_sha256") == T.digest(policy) and
              candidate.get("case_sha256") == T.digest(case), "E did not return this trial's validated measurement")
        if restarted: _restart_trial_binding(outputs, evidence, restart_admission, identity)
        validated_files = E._Reader()
        _need(type(candidate.get("files")) is dict, "E did not return the validated actual file closure")
        for name, pin in candidate["files"].items():
            _need(pin.get("file") == name, "E file closure key differs from its actual file pin")
            validated_files.read(pin, "validated", maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
        _save(root / "measurement.json", candidate)
        measurement, measurement_pin = copy.deepcopy(candidate), _pin(root / "measurement.json")
    except BaseException as error:
        attempt["errors"].append(_error(error))
        review_required = isinstance(error, R.ResourceReviewRequired)
        if not isinstance(error, Exception): interrupted = error
    finally:
        try:
            _current(app, target, identity, request, resources, adapter, reader)
            for name, pin in journals.items():
                saved = E._Reader().json(pin)
                expected = inputs if name == "inputs.json" else intent if name == "intent.json" else restart_admission
                _need(P.same_data(saved, expected), "frozen trial journal changed")
            if outputs is not None: outputs.recheck()
            if validated_files is not None: validated_files.recheck()
            for pin in (case_pin, measurement_pin):
                if pin is not None: E._Reader().read(pin, "trial", maximum=R.MAX_JSON_BYTES, retain=False)
        except Exception as error:
            review_required = True
            attempt["errors"].append(_error(error))
        disposition_options = {"observer_capability": capability} if owned_observer else {}
        if require_pre_actuation_ack:
            disposition_options.update(observer_capability=capability, require_pre_actuation_ack=True)
        disposition = _disposition(phases, wrapper, R.digest(request), **disposition_options) if attempt["collection_attempted"] else None
        uncertain = (attempt["collection_attempted"] and (disposition["read_error"] is not None or
                     disposition["resource_review_required"] is not False or
                     type(disposition["owned_worker"]) is not dict or disposition["owned_worker"].get("reaped") is not True or
                     not P.same_data(disposition["owned_worker"].get("errors"), []) or
                     type(disposition["owned_worker"].get("returncode")) is not int or
                     disposition["owned_worker"]["returncode"] != 0 or
                     disposition["owned_worker"].get("term_attempted") is not False or
                     disposition["owned_worker"].get("kill_attempted") is not False))
        uncertain = uncertain or review_required
        measured = measurement is not None and not attempt["errors"] and not uncertain
        attempt["status"] = "measured" if measured else "resource_review_required" if uncertain else "failed"
        attempt["native_disposition"] = disposition
        attempt["trial_measurement_validated"] = measured
        _save(root / "attempt.json", attempt)
    report = {"format": REPORT4 if restarted else REPORT3 if require_pre_actuation_ack else REPORT2 if owned_observer else REPORT,
              "status": attempt["status"], "evidence_only": True,
              "intent_sha256": R.digest(intent), "actor_sha256": R.digest(actor), "policy_sha256": T.digest(policy),
              "attempt": _pin(root / "attempt.json"), "case": case_pin, "measurement": measurement_pin,
              "trial_measurement_validated": measured, "native_disposition": disposition,
              "errors": copy.deepcopy(attempt["errors"]), "proof_created": False, "task_proof_created": False,
              "public_proof_created": False, "card_created": False, "aggregation_validated": False,
              "restart_deployment_proven": False, "input_generalization_claimed": False}
    if restarted:
        report["restart_admission"] = copy.deepcopy(journals["restart-admission.json"])
    if owned_observer:
        report.update(observer_capability=capability, state_observation_scope="selected_owned_visible_ax_window_tree")
    if require_pre_actuation_ack:
        report.update(observer_capability=capability, pre_actuation_ack_required=True,
                      pre_actuation_ack_validated=measured,
                      pre_actuation_ack=copy.deepcopy(measurement["pre_actuation_ack"]) if measured else None)
    _save(root / "outcome.json", report)
    if interrupted is not None: raise interrupted
    return report
