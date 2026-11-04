"""Inert closed-protocol fixtures; execution belongs to the guest guarded suite."""
import copy
import json
import struct

import pytest

from anyuse.execution import rea_runtime_permit as RP


def _config():
    return {"request": {"operations": [{"op": "native_invoke"}]},
        "identity": {"bundle_id": "org.fixture.App", "bundle": "/fixture/App.app", "pid": 42,
                     "launch_time": 1000.25, "uid": 501, "executable": "/fixture/App.app/Contents/MacOS/App"},
        "target": {"bundle_id": "org.fixture.App", "sha256": "a" * 64},
        "adapter": {"sha256": "b" * 64, "source_sha256": "c" * 64,
                    "manifest": {"sha256": "d" * 64}, "build": {"interlock_protocol": RP.PROTOCOL}},
        "native_resources": {"/fixture/App.app/Contents/MacOS/App": {"sha256": "a" * 64}},
        "compiler_provenance": {}, "deadline_ms": 100000}


def _completed():
    config = _config()
    binding = RP.make_binding(config, run_nonce="e" * 64, observer_capability="ax_focused_readonly/1",
        observer_selector={"literal_inert_selector": True}, observer_spec={"literal_inert_spec": True})
    selected = {"generation": 1, "owner_thread_id": 7}
    def pin(name): return {"file": "/fixture/" + name + ".json", "sha256": "f" * 64, "bytes": 20}
    setup, before, checkpoint_pin = pin("setup"), pin("before"), pin("checkpoint")
    decision = {"format": RP.FRAME, "kind": "decision", "binding": binding, "worker_pid": 51,
        "worker_nonce": "0" * 64, "selection": selected, "decision": "allow", "ordinal": 1,
        "setup": setup, "before": before, "checkpoint": checkpoint_pin, "reason": None}
    def events(names, start):
        return [{"seq": i + 1, "kind": kind, "at": start + i} for i, kind in enumerate(names)]
    parent = {"format": RP.PARENT, "binding": binding, "worker_pid": 51, "worker_nonce": "0" * 64,
        "selection": selected, "decision": decision, "sent": True, "error": None,
        "events": events(["prepared", "worker_started", "ready_received", "setup_complete", "before_complete",
                          "need_received", "decision_intent", "decision_sent", "closed"], 1)}
    worker = {"format": RP.WORKER, "binding": binding, "worker_pid": 51, "worker_nonce": "0" * 64,
        "selection": selected, "decision": decision, "post_started": True, "post_returned": True,
        "parent_eof": False, "thread_started": True, "thread_stopped": True, "errors": [],
        "events": events(["started", "ready_sent", "need_sent", "decision_received", "post_started", "post_returned", "stopped"], 100)}
    agent = {"format": RP.AGENT, "binding_sha256": RP.digest(binding), "worker_nonce": "0" * 64,
        "selection": selected, "decision_sha256": RP.digest(decision), "state": "consumed", "permit_consumed": True,
        "native_entry": True, "closed": True, "error": None,
        "events": events(["prepared", "waiting", "permit_accepted", "permit_consumed", "native_entry", "native_returned", "closed"], 10000)}
    checkpoint = {"format": RP.CHECKPOINT, "binding": binding, "worker_pid": 51, "worker_nonce": "0" * 64,
        "selection": selected, "setup": setup, "before": before, "setup_finished_monotonic": 4,
        "before_finished_monotonic": 5, "checked_monotonic": 6, "deadline_monotonic": 9,
        "current": {key: copy.deepcopy(config[key]) for key in
                    ("identity", "target", "adapter", "native_resources", "compiler_provenance")}}
    return parent, worker, agent, checkpoint, {"binding": binding, "setup_pin": setup, "before_pin": before,
        "checkpoint_pin": checkpoint_pin, "native_calls": 1, "invocations": 1}


def test_completed_protocol_requires_all_actual_roles_and_confers_only_measurement():
    args = _completed()
    summary = RP.validate_completed(*args[:4], **args[4])
    assert summary["evidence_only"] is True and summary["proof_created"] is False
    assert summary["agent_consumed"] is True and summary["native_calls"] == summary["invocations"] == 1


@pytest.mark.parametrize("role,change", [
    (0, "not_sent"), (0, "deny"), (0, "early_before"), (0, "early_commit"), (0, "late_send"), (0, "need_before_ready"),
    (1, "not_stopped"), (1, "eof"), (1, "not_posted"), (1, "error"),
    (2, "not_consumed"), (2, "no_entry"), (2, "no_return"), (2, "replayed"),
    (3, "new_process"), (3, "new_resource"), (3, "late_check"),
])
def test_rehashed_or_partial_roles_cannot_promote_a_permit_to_completed(role, change):
    args = copy.deepcopy(_completed())
    row = args[role]
    if change == "not_sent": row["sent"] = False
    elif change == "deny":
        row["decision"].update(decision="deny", reason="inert failed setup")
        args[1]["decision"] = copy.deepcopy(row["decision"])
        args[2]["decision_sha256"] = RP.digest(row["decision"])
    elif change == "early_before": row["events"][3]["at"], row["events"][4]["at"] = 3, 4
    elif change == "early_commit":
        args[3]["checked_monotonic"] = 7.5
    elif change == "late_send": row["events"][7]["at"], row["events"][8]["at"] = 9, 10
    elif change == "need_before_ready":
        need = row["events"].pop(5)
        row["events"].insert(2, need)
        for index, event in enumerate(row["events"], 1): event.update(seq=index, at=index)
    elif change == "not_stopped": row["thread_stopped"] = False
    elif change == "eof": row["parent_eof"] = True
    elif change == "not_posted": row["post_returned"] = False
    elif change == "error": row["errors"] = [{"type": "OSError", "message": "retained forwarding failure"}]
    elif change == "not_consumed": row["permit_consumed"] = False
    elif change == "no_entry": row["native_entry"] = False
    elif change == "no_return":
        row["events"] = [event for event in row["events"] if event["kind"] != "native_returned"]
        for index, event in enumerate(row["events"], 1): event["seq"] = index
    elif change == "replayed": row["worker_nonce"] = "1" * 64
    elif change == "new_process": row["current"]["identity"]["launch_time"] += 1
    elif change == "new_resource": row["current"]["native_resources"] = {}
    else: row["checked_monotonic"] = row["deadline_monotonic"]
    with pytest.raises(ValueError): RP.validate_completed(*args[:4], **args[4])


def test_frame_fragmentation_is_bounded_and_has_no_partial_approval():
    parent, *_ = _completed()
    frame = RP.encode_frame(parent["decision"])
    assert RP.decode_frames(frame[:-1]) == ([], frame[:-1])
    assert RP.decode_frames(frame) == ([parent["decision"]], b"")
    assert RP.decode_frames(frame + frame[:3]) == ([parent["decision"]], frame[:3])


@pytest.mark.parametrize("raw", [b"\0\0\0\0", struct.pack(">I", RP.MAX_FRAME + 1),
    struct.pack(">I", 13) + b'{"x":1,"x":2}', struct.pack(">I", 10) + b'{"x": NaN}',
    struct.pack(">I", 1) + b"\xff"])
def test_frame_decoder_refuses_oversize_duplicate_nonfinite_and_bad_utf8(raw):
    with pytest.raises((ValueError, UnicodeError)): RP.decode_frames(raw)


def test_early_deny_without_selection_is_honest_and_never_completed():
    parent, worker, agent, checkpoint, keywords = copy.deepcopy(_completed())
    decision = parent["decision"]
    decision.update(decision="deny", selection=None, setup=None, before=None, checkpoint=None, reason="setup failed")
    worker["decision"] = copy.deepcopy(decision)
    assert RP.validate_parent(parent) == parent
    assert RP.validate_worker(worker) == worker
    with pytest.raises(ValueError): RP.validate_completed(parent, worker, agent, checkpoint, **keywords)


@pytest.mark.parametrize("value", [{}, {"returned": True}, {"format": RP.SETUP, "status": "failed", "result": {}},
    {"format": RP.SETUP, "status": "returned", "result": {}, "verified": True}])
def test_setup_success_requires_explicit_closed_trusted_return(value):
    with pytest.raises(ValueError): RP.validate_setup(value)
    accepted = {"format": RP.SETUP, "status": "returned", "result": {"actual_journal": ["returned"]}}
    assert RP.validate_setup(accepted) == accepted


def test_binding_requires_compiled_capability_and_all_frozen_current_hashes():
    config = _config()
    keywords = {"run_nonce": "e" * 64, "observer_capability": "ax_focused_readonly/1",
                "observer_selector": {}, "observer_spec": {}}
    before = RP.make_binding(config, **keywords)
    config["native_resources"] = {}
    assert RP.make_binding(config, **keywords)["native_resources_sha256"] != before["native_resources_sha256"]
    config["adapter"]["build"].pop("interlock_protocol")
    with pytest.raises(ValueError, match="compiled"): RP.make_binding(config, **keywords)


@pytest.mark.parametrize("field,value", [("native_calls", True), ("invocations", 1.0), ("native_calls", 0), ("invocations", 2)])
def test_one_use_receipt_never_coerces_counter_types_or_counts(field, value):
    args = copy.deepcopy(_completed())
    args[4][field] = value
    with pytest.raises(ValueError): RP.validate_completed(*args[:4], **args[4])


@pytest.mark.parametrize("fault", ["extra", "nonce", "pid_bool", "ordinal_bool", "zero_pin", "relative_pin"])
def test_closed_decision_refuses_executable_extras_and_coerced_identity_fields(fault):
    frame = copy.deepcopy(_completed()[0]["decision"])
    if fault == "extra": frame["callback"] = "arbitrary.code"
    elif fault == "nonce": frame["worker_nonce"] = "reuse"
    elif fault == "pid_bool": frame["worker_pid"] = True
    elif fault == "ordinal_bool": frame["ordinal"] = True
    elif fault == "zero_pin": frame["checkpoint"]["bytes"] = 0
    else: frame["setup"]["file"] = "relative.json"
    with pytest.raises(ValueError): RP.validate_frame(frame)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
