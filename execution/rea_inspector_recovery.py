"""Explicit review and normal close of a failed owned inspector startup.

This module does not adopt processes, create configuration, establish renderer
readiness, execute app tasks, clear prior uncertainty, or retry a launch. Native
resource operations require the original lifecycle's persisted process identity.
Imports have no filesystem, native, or network effects.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from . import rea_mapping as R
from . import rea_inspector_bridge as I
from . import rea_inspector_lifecycle as L

FORMAT = "anyuse.rea-inspector-recovery/1"


def _source_identity():
    path = Path(__file__).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "lifecycle": L._source_identity()}


def _outside(folder, *receipts):
    for receipt in receipts:
        parent = Path(receipt["file"]).parent
        if L._inside(folder, parent) or L._inside(parent, folder):
            raise ValueError("recovery evidence must be outside all source receipt directories")


def _receipt(value, *, action, status, lifecycle=False):
    data, pin = L._private(value)
    source = L._source_identity() if lifecycle else _source_identity()
    if (data.get("format") != (L.FORMAT if lifecycle else FORMAT) or data.get("action") != action or
            data.get("status") != status or data.get("receipt") != pin["file"] or data.get("source") != source or
            data.get("evidence_identity") != L._directory(Path(pin["file"]).parent, mode=0o700)):
        raise ValueError("recovery needs the unchanged original private receipt and exact current sources")
    return data, pin


def _frozen_config(policy, transport):
    if not isinstance(transport, dict):
        raise ValueError("present configuration has no original frozen transport provenance")
    config, config_pin = I._config_for_target(policy["target"], policy["config"])
    if (L._private(policy["config"])[1] != config_pin or I._transport(config, config_pin) != transport or
            config["endpoint"] != "http://127.0.0.1:" + str(policy["port"]) or
            config["relative_url"] != policy["relative_url"]):
        raise ValueError("failed startup's frozen inspector configuration changed; no rebinding")
    return {"kind": "frozen", "transport": transport}


def _config_state(original, policy, policy_pin, profile_pin):
    previous = original["previous"]
    if previous is not None:
        if not isinstance(previous, dict) or not isinstance(previous.get("file"), str):
            raise ValueError("restart requires its exact pinned prior successful launch receipt")
        prior, prior_pin = _receipt(previous["file"], action="launch", status="ready", lifecycle=True)
        if (prior_pin != previous or prior.get("policy") != policy_pin or prior.get("target") != policy["target"] or
                prior.get("config_file") != policy["config"] or prior.get("profile_identity") != profile_pin or
                prior.get("resource_review_required") is not False or prior.get("isolation_verified") is not False):
            raise ValueError("restart configuration provenance differs from the original successful launch")
        state = _frozen_config(policy, prior.get("transport"))
        if original.get("transport") is not None and original["transport"] != state["transport"]:
            raise ValueError("failed restart and prior successful transport disagree")
        return {**state, "provenance": "pinned_previous_ready_launch", "previous": prior_pin}
    if original.get("transport") is not None:
        return {**_frozen_config(policy, original["transport"]), "provenance": "original_frozen_transport"}
    if Path(policy["config"]).exists() or Path(policy["config"]).is_symlink():
        raise ValueError("initial failed startup has present unpinned configuration; no adoption")
    return {"kind": "absent_initial", "file": policy["config"], "provenance": "original_initial_intent_and_current_absence",
            "claim_scope": "configuration is currently absent; historical creation or deletion is not established"}


def _original(value, out):
    original, original_pin = _receipt(value, action="launch", status="incomplete", lifecycle=True)
    if (type(original.get("launch_attempts")) is not int or original["launch_attempts"] != 1 or
            type(original.get("termination_requests")) is not int or original["termination_requests"] != 0 or
            original.get("resource_review_required") is not True or "previous" not in original or
            not isinstance(original.get("identity"), dict) or not isinstance(original.get("kernel_identity"), dict) or
            not isinstance(original.get("policy"), dict) or not isinstance(original["policy"].get("file"), str) or
            any(type(original.get(key)) is not int or original[key] != 0 for key in ("task_calls", "app_task_methods", "model_calls")) or
            original.get("proof_created") is not False or original.get("task_result_verified") is not False):
        raise ValueError("failed startup lacks a single untouched launch intent and persisted first owned process identity")
    data = L._policy(original["policy"]["file"], out)
    policy, policy_pin, bundle, profile, config, folder, executable = data
    profile_pin = L._directory(profile, mode=0o700)
    main, kernel = original["identity"], original["kernel_identity"]
    if (set(main) != {"bundle_id", "bundle", "executable", "pid", "launch_time", "uid"} or
            set(kernel) != {"pid", "ppid", "uid", "started_at", "executable"} or
            type(main["pid"]) is not int or main["pid"] <= 0 or kernel["pid"] != main["pid"] or
            type(kernel["pid"]) is not int or type(kernel["ppid"]) is not int or kernel["ppid"] < 0 or
            type(main["uid"]) is not int or main["uid"] != os.getuid() or type(kernel["uid"]) is not int or kernel["uid"] != main["uid"] or
            main["bundle_id"] != policy["target"]["bundle_id"] or main["bundle"] != str(bundle) or
            main["executable"] != str(executable) or kernel["executable"] != str(executable) or
            not L._text(kernel["started_at"], 256) or
            type(main["launch_time"]) not in {int, float} or not math.isfinite(main["launch_time"]) or main["launch_time"] <= 0 or
            type(original.get("launch_requested_at")) not in {int, float} or not math.isfinite(original["launch_requested_at"]) or
            original["launch_requested_at"] <= 0 or main["launch_time"] < original["launch_requested_at"]):
        raise ValueError("failed startup has malformed or incomplete first process/start identity pins")
    if (original["policy"] != policy_pin or original.get("target") != policy["target"] or
            original.get("profile_identity") != profile_pin or original.get("requested_profile") != {"file": str(profile)} or
            original.get("config_file") != str(config)):
        raise ValueError("failed launch policy, target, or owned profile identity changed")
    _outside(folder, original_pin)
    config_state = _config_state(original, policy, policy_pin, profile_pin)
    if "previous" in config_state:
        _outside(folder, config_state["previous"])
    return original, original_pin, data, config_state


def _current(original, policy, bundle, executable, deadline):
    main, kernel = L._exact_main(bundle, policy["target"], executable, deadline)
    if main != original["identity"] or kernel != original["kernel_identity"]:
        raise ValueError("first owned process identity changed or disappeared; no adoption or termination")
    return main, kernel


def _owned_kernel(bundle, main, kernel, deadline):
    rows = L._kernel_candidates(bundle, deadline)
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 64 or any(not isinstance(row, dict) for row in rows) or
            any(type(row.get("pid")) is not int for row in rows) or len({row["pid"] for row in rows}) != len(rows)):
        raise ValueError("failed app kernel inventory is missing, ambiguous, or exceeds its bound")
    processes = {}
    for row in rows:
        if type(row.get("pid")) is not int or row["pid"] <= 0:
            raise ValueError("failed app kernel inventory has malformed process IDs")
        current = I._process(row["pid"], deadline)
        if current != row:
            raise ValueError("failed app kernel process changed during observation")
        processes[row["pid"]] = current
        if row["pid"] == main["pid"]:
            if current != kernel:
                raise ValueError("failed app main kernel identity changed")
            continue
        visited = set()
        for _ in range(16):
            if (current["uid"] != main["uid"] or not L._inside(Path(current["executable"]), bundle) or
                    current["pid"] in visited):
                raise ValueError("kernel app candidate is not an exact same-user owned descendant")
            visited.add(current["pid"])
            if current["ppid"] == main["pid"]:
                break
            current = I._process(current["ppid"], deadline)
        else:
            raise ValueError("failed app descendant ancestry exceeded its bound")
    if main["pid"] not in processes:
        raise ValueError("persisted owned main is absent from kernel app inventory")
    return [processes[pid] for pid in sorted(processes)]


def _listener(policy, main, deadline):
    if L._port_absent(policy["port"], deadline):
        return {"status": "absent", "port": policy["port"]}
    listener = I._listener("http://127.0.0.1:" + str(policy["port"]), main, deadline)
    return {"status": "owned_loopback_listener", **listener}


def _unchanged(original, original_pin, data, config_state, source, *, review_pin=None):
    policy, policy_pin, bundle, profile, config, folder, executable = data
    if L._private(original_pin["file"])[1] != original_pin:
        raise ValueError("original failed receipt changed during recovery")
    if review_pin is not None and L._private(review_pin["file"])[1] != review_pin:
        raise ValueError("explicit resource review receipt changed")
    L._unchanged(policy, policy_pin, profile, original["profile_identity"])
    if _config_state(original, policy, policy_pin, original["profile_identity"]) != config_state:
        raise ValueError("failed startup's configuration provenance changed during recovery")
    if _source_identity() != source:
        raise ValueError("recovery, lifecycle, or inspector source changed")


def _base(action, folder, original, original_pin, policy, policy_pin, config_state):
    result = L._base(action, folder, policy, policy_pin)
    result.update(format=FORMAT, source=_source_identity(), original=original_pin,
                  original_status="incomplete", original_failure_preserved=True,
                  original_resource_review_required=True, original_uncertainty_cleared=False,
                  effect_rollback_claimed=False, isolation_verified=False,
                  metadata_only=action == "review_failed", config_state=config_state,
                  profile_identity=original["profile_identity"], resource_review_required=True,
                  ownership_revalidated=False, resources_closed=False, inspector_commands=0)
    return result


def _observe(original, data, config_state, deadline):
    policy, pin, bundle, profile, config, folder, executable = data
    main, kernel = _current(original, policy, bundle, executable, deadline)
    processes = _owned_kernel(bundle, main, kernel, deadline)
    listener = _listener(policy, main, deadline)
    _current(original, policy, bundle, executable, deadline)
    if _owned_kernel(bundle, main, kernel, deadline) != processes or _listener(policy, main, deadline) != listener:
        raise ValueError("owned failed startup resources changed during metadata review")
    return {"identity": main, "kernel_identity": kernel, "owned_kernel_processes": processes, "listener": listener,
            "profile_directory_revalidated": True, "observed_profile_use": None,
            "review_scope": "persisted lifecycle resource ownership; renderer readiness and profile use are not established"}


def review_failed(original, out, *, timeout=30):
    """Review exact failed-startup ownership without task or inspector calls."""
    R.assert_guest()
    deadline = L._timeout(timeout, 60)
    failed, original_pin, data, config_state = _original(original, out)
    policy, policy_pin, bundle, profile, config, folder, executable = data
    I._remaining(deadline)
    folder.mkdir(mode=0o700)
    result = _base("review_failed", folder, failed, original_pin, policy, policy_pin, config_state)
    L._write(folder, result)
    try:
        result.update(_observe(failed, data, config_state, deadline))
        _unchanged(failed, original_pin, data, config_state, result["source"])
        _current(failed, policy, bundle, executable, deadline)
        I._remaining(deadline)
        result.update(status="resource_reviewed", ownership_revalidated=True)
        return L._write(folder, result)
    except Exception as exc:
        return L._failure(folder, result, exc)


def close_failed(original, review, out, *, timeout=15):
    """After explicit review, request normal close of that same process once."""
    R.assert_guest()
    deadline = L._timeout(timeout, 60)
    failed, original_pin, data, config_state = _original(original, out)
    reviewed, review_pin = _receipt(review, action="review_failed", status="resource_reviewed")
    policy, policy_pin, bundle, profile, config, folder, executable = data
    if (reviewed.get("original") != original_pin or reviewed.get("policy") != policy_pin or
            reviewed.get("target") != policy["target"] or reviewed.get("config_state") != config_state or
            reviewed.get("profile_identity") != failed["profile_identity"] or
            reviewed.get("identity") != failed["identity"] or reviewed.get("kernel_identity") != failed["kernel_identity"] or
            reviewed.get("ownership_revalidated") is not True or reviewed.get("original_uncertainty_cleared") is not False or
            reviewed.get("resource_review_required") is not True or reviewed.get("resources_closed") is not False or
            reviewed.get("proof_created") is not False or reviewed.get("task_result_verified") is not False or
            any(type(reviewed.get(key)) is not int or reviewed[key] != 0 for key in
                ("launch_attempts", "termination_requests", "task_calls", "app_task_methods", "model_calls", "inspector_commands"))):
        raise ValueError("normal failed-startup close requires its explicit unchanged ownership review")
    _outside(folder, review_pin)
    I._remaining(deadline)
    folder.mkdir(mode=0o700)
    result = _base("close_failed", folder, failed, original_pin, policy, policy_pin, config_state)
    result["review"] = review_pin
    L._write(folder, result)
    try:
        result.update(_observe(failed, data, config_state, deadline))
        _unchanged(failed, original_pin, data, config_state, result["source"], review_pin=review_pin)
        main, kernel = _current(failed, policy, bundle, executable, deadline)
        _listener(policy, main, deadline)
        result["termination_intent"] = True
        L._write(folder, result)
        def before_request():
            result["termination_requests"] = 1
            L._write(folder, result)
        result["normal_termination_accepted"] = L._terminate(bundle, policy["target"], executable, main, kernel, deadline,
                                                             before_request=before_request)
        if not result["normal_termination_accepted"]:
            raise ValueError("failed startup refused normal termination; no escalation")
        while True:
            I._remaining(deadline)
            observation = {"old_kernel_pid_absent": L._kernel_absent(main["pid"], deadline),
                           "bundle_registrations_absent": not L._registrations(policy["target"]["bundle_id"]),
                           "kernel_app_candidates_absent": not L._kernel_candidates(bundle, deadline),
                           "listener_absent": L._port_absent(policy["port"], deadline)}
            result["closure_observation"] = observation
            if all(observation.values()):
                break
            L._wait_tick(deadline)
        _unchanged(failed, original_pin, data, config_state, result["source"], review_pin=review_pin)
        I._remaining(deadline)
        result.update(status="closed", ownership_revalidated=True, resources_closed=True, resource_review_required=False)
        return L._write(folder, result)
    except Exception as exc:
        return L._failure(folder, result, exc)
