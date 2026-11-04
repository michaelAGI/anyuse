"""Reasoning policy boundaries with real ModelAdapter and inert G sessions."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from anyuse.execution import model_adapter as M
from anyuse.execution import rea_agent_driver as D
from anyuse.execution import rea_agent as G
from anyuse.execution import rea_receiver_probe as RP
from test_rea_agent import session as legacy_session, proposal, fixture
from anyuse.execution import rea_runtime_bridge as B
from test_rea_runtime_bridge import _worker_fixture, runtime as native_metadata_bridge_runtime

_NATIVE_ADAPTER_IDENTITY = B.adapter_identity


@pytest.fixture
def session(legacy_session, tmp_path):
    old, profile, plan = legacy_session
    state = G.R.load_json(old / "session.json")
    root = tmp_path / "approved-driver-session"
    G.begin(state["bundle"], state["rea"], state["task"], state["params"], state["success_criteria"],
            plan, root, observer_profile={k: v for k, v in profile.items() if k != "evidence"})
    return root, profile, plan


@pytest.mark.parametrize("workflow,module_name", [
    ("guest_fixed_state_v1", "rea_state_campaign"),
    ("guest_model_judged_state_v1", "rea_visual_campaign"),
])
@pytest.mark.parametrize("require_restart", [False, True])
def test_agent_context_scope_matches_declared_restart_policy(session, workflow, module_name, require_restart):
    import importlib
    campaign = importlib.import_module("anyuse.execution." + module_name)
    state = G.R.load_json(session[0] / "session.json")
    state["workflow"] = workflow
    state["plan"]["trial_policy"] = {"preset": "fast", "require_restart": require_restart}
    result = G._context(state)
    assert result["capabilities"]["proof_scope"] == (
        campaign.SCOPE if require_restart else campaign.SCOPE_NO_RESTART)


def decision(request=None, reason="Measured evidence supports this bounded tool."):
    return {"action": "stop" if request is None else "step",
            "request_json": "" if request is None else json.dumps(request), "reason": reason}


def static_decision(request=None, reason="Measured evidence supports this bounded static tool."):
    if type(request) is dict and request.get("op") in D.STATIC_FIELDS:
        # Explicit fixture nulls represent the strict provider wire format.
        request = {**{key: None for key in D.STATIC_FIELDS[request["op"]]}, **request}
    return {"action": "stop" if request is None else "step", "request": request, "reason": reason}


def model(replies, *, hook=None, budget=None):
    seen = []
    replies = list(replies)
    def post(request):
        seen.append(json.loads(request.content))
        if hook:
            hook()
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "fixture failure"})
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    config = M.ModelConfig("local", "http://fixture.invalid/v1", "inert-reasoner", "",
                           timeout=.01, retries=0, price_in=1, price_out=1)
    return M.ModelAdapter(config, transport=httpx.MockTransport(post), budget=budget), seen


@pytest.fixture
def receiver_runtime(session, monkeypatch):
    """Real G registration/validation with inert native observations only."""
    import os
    root = session[0]
    state = G.R.load_json(root / "session.json")
    app = Path(state["bundle"])
    identity = {"bundle": str(app), "bundle_id": state["target"]["bundle_id"], "pid": 321,
                "uid": os.getuid(), "launch_time": 123.25,
                "executable": str(app / "Contents/MacOS/Fixture")}
    control = {"calls": [], "failure": None, "identity": identity}
    adapter = {"file": "inert-fixed-adapter", "sha256": "1" * 64}
    monkeypatch.setattr(B, "process_identity", lambda *args: copy.deepcopy(control["identity"]))
    monkeypatch.setattr(B, "adapter_identity", lambda: copy.deepcopy(adapter))
    def method(klass, selector):
        return {"class": klass, "selector": selector, "dispatch": "+" if selector == "sharedApplication" else "-",
                "signature": "@16@0:8", "module": "/fixture/AppKit", "origin": "module_backed",
                "implementation_offset": "0x20"}
    def collect(bundle, out, request, *, timeout=15, expected_identity=None):
        assert expected_identity == identity
        operations = request["operations"]
        control["calls"].append(copy.deepcopy(request))
        rows, count, failed = [], 0, False
        for i, operation in enumerate(operations):
            row = {"index": i, "op": operation["op"], "status": "ok"}
            if operation["op"] == "classes":
                if operation.get("owner") == "app" and operation.get("limit") == 32:
                    matched = control.get("inventory_matched", 1)
                    row.update(rows=[{"name": control.get("inventory_class_prefix", "") + operation["query"].title() + f"Owner{j}",
                                      "module": identity["executable"], "origin": "module_backed"}
                                     for j in range(min(matched, 32))], matched=matched, truncated=matched > 32)
                else:
                    row.update(rows=[{"name": "NSApplication", "module": "/fixture/AppKit", "origin": "module_backed"}],
                               matched=1, truncated=False)
            elif operation["op"] == "methods":
                item = method(operation["class"], operation["query"])
                if control["failure"] == "abi":
                    item["signature"] = "^{Opaque=}16@0:8"
                row.update(rows=[item], matched=1, truncated=False)
            elif operation["op"] == "read":
                field = operation["fields"][0]
                klass = operation["expected_class"]
                if control["failure"] == "class_change" and len(operations) == 2 and i == 1:
                    failed = True  # Fixed JS denies before this next getter.
                    break
                actual = {"sharedApplication": "ReceiptApplication", "mainWindow": "ReceiptWindow",
                          "keyWindow": "ReceiptWindow", "windowController": "ReceiptController"}[field["selector"]]
                actual = control.get("returned_classes", {}).get(field["selector"], actual)
                count += 1
                value = {"class": actual, "handle": f"h{i}", "alias": field["alias"], "expires": "transaction"}
                if control["failure"] == "class_change" and len(operations) == 2:
                    value["class"] = "DifferentApplication"
                terminal_nil = control.get("nil_depth") == len(operations) - 1 and i == len(operations) - 1
                if terminal_nil or control["failure"] == "nil_prefix" and len(operations) == 2 and i == 0:
                    value = None
                if control["failure"] == "unsupported":
                    value = {}
                if control["failure"] == "nil_prefix_drift" and len(operations) == 2 and i == 0:
                    value["class"] = "DifferentApplication"
                row["fields"] = [{"name": "receiver", "kind": "getter", "type": "object",
                                  "value": value, "metadata": {**method(klass, field["selector"]),
                                                              "return_type": "object", "argument_types": []}}]
                if terminal_nil and control["failure"] == "nil_missing_value":
                    row["fields"][0].pop("value")
                if terminal_nil and control["failure"] == "nil_bad_metadata":
                    row["fields"][0]["metadata"]["signature"] = "v16@0:8"
            rows.append(row)
        worker = {"format": B.WORKER, "request_sha256": G.R.digest(request), "status": "completed",
                  "operations": rows, "native_calls": count, "invocations": 0, "helper_load_attempts": 0,
                  "attached": True, "handles_expired": True, "native_state_uncertain": False,
                  "cleanup": {"script_unloaded": True, "detached": True}, "cleanup_errors": []}
        uncertain = control["failure"] == "cleanup" and count or control["failure"] == "nil_cleanup" and len(operations) == 2
        inventory = operations[0].get("op") == "classes" and operations[0].get("owner") == "app"
        if inventory and control["failure"] == "inventory_cleanup":
            uncertain = True
        if inventory and control["failure"] == "inventory_native":
            worker["native_calls"] = 1
        if uncertain:
            worker.update(native_state_uncertain=True, cleanup_errors=[{"phase": "detach", "error": "inert uncertainty"}])
        if failed:
            worker.update(status="incomplete", error={"type": "RuntimeError", "message": "receiver class differs before getter"})
        folder = Path(out); folder.mkdir(parents=True)
        G.R.write_json(folder / "worker-output.json", worker)
        G.R.write_json(folder / "worker-request.json", {"request": request, "target": state["target"],
                       "identity": expected_identity, "adapter": adapter})
        receipt = {"format": B.RUNTIME, "status": "incomplete" if failed else "observed", "request": request,
                   "request_sha256": G.R.digest(request), "bundle": str(app), "target": state["target"],
                   "identity": identity, "after_identity": identity, "expected_identity": expected_identity,
                   "adapter": adapter, "result": worker, "error": worker.get("error"), "actuated": False,
                   "resource_review_required": bool(uncertain),
                   "receipt": str(folder / "runtime.json")}
        G.R.write_json(folder / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    return root, control


@pytest.mark.parametrize("caller_seconds,absolute_remaining,step_seconds,expected_calls", [
    (90, None, 11, 7), (180, 19, 0, 0)])
def test_receiver_time_admission_never_shrinks_a_late_worker_to_startup_only(
        receiver_runtime, tmp_path, monkeypatch, caller_seconds, absolute_remaining, step_seconds, expected_calls):
    root, control = receiver_runtime
    clock, timeouts = [1000.0], []
    collect = B.collect
    def elapsed_collect(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        result = collect(*args, **kwargs)
        clock[0] += step_seconds
        return result
    monkeypatch.setattr(RP.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(B, "collect", elapsed_collect)
    result = RP.run(root, tmp_path / "insufficient-native-envelope", timeout_seconds=caller_seconds,
                    deadline_monotonic=None if absolute_remaining is None else clock[0] + absolute_remaining,
                    class_queries=["Read"])
    assert result["status"] == "stopped_failure" and result["retry"] is False
    assert "full native transaction and cleanup; no dispatch" in result["error"]["message"]
    assert result["g_step_dispatches"] == len(control["calls"]) == expected_calls
    assert timeouts == [15] * expected_calls
    assert result["native_calls"] == result["known_native_calls"] == (6 if expected_calls else 0)
    state = G.R.load_json(root / "session.json")
    assert state["status"] == "investigating" and all(row["status"] == "completed" for row in state["steps"])
    assert all(not any(op.get("owner") == "app" for op in request["operations"]) for request in control["calls"])
    assert result["model_calls"] == result["task_invocations"] == 0 and state["proof"] is None


def test_receiver_default_budget_fits_slow_complete_route_and_optional_inventory(receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    clock, timeouts = [1000.0], []
    collect = B.collect
    def elapsed_collect(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        result = collect(*args, **kwargs)
        clock[0] += 11
        return result
    monkeypatch.setattr(RP.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(B, "collect", elapsed_collect)
    result = RP.run(root, tmp_path / "slow-complete-receiver", class_queries=["Read"])
    assert result["status"] == "observed" and result["error"] is None
    assert 120 <= result["bounds"]["timeout_seconds"] <= 180
    assert result["g_step_dispatches"] == len(control["calls"]) == 8 and timeouts == [15] * 8
    assert RP.validate(result, root)["class_inventory"]["queries"] == ["Read"]
    assert result["model_calls"] == result["task_invocations"] == 0


def test_receiver_preserves_actual_timeout_and_resource_review_without_decoding_empty_worker(
        receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    collect, read = B.collect, RP._read
    state = G.R.load_json(root / "session.json")
    parsed_empty = []
    def fail_inventory(bundle, out, request, **kwargs):
        if request["operations"][0].get("owner") != "app":
            return collect(bundle, out, request, **kwargs)
        control["calls"].append(copy.deepcopy(request))
        folder = Path(out); folder.mkdir(parents=True)
        (folder / "worker-output.json").write_bytes(b"")
        receipt = {"format": B.RUNTIME, "status": "incomplete", "request": request,
                   "request_sha256": G.R.digest(request), "bundle": str(bundle), "target": state["target"],
                   "expected_identity": kwargs["expected_identity"], "identity": control["identity"],
                   "after_identity": None, "result": None, "native_calls": None, "actuated": None,
                   "worker": {"timed_out": True}, "resource_review_required": True,
                   "error": {"type": "TimeoutError", "message": "runtime worker timed out; detachment needs review"}}
        G.R.write_json(folder / "runtime.json", receipt)
        return receipt
    def forbid_empty_worker(pin):
        file = Path(pin["file"])
        if file.name == "worker-output.json" and file.stat().st_size == 0:
            parsed_empty.append(str(file))
            raise AssertionError("empty worker stdout must not be parsed as JSON")
        return read(pin)
    monkeypatch.setattr(B, "collect", fail_inventory)
    monkeypatch.setattr(RP, "_read", forbid_empty_worker)
    result = RP.run(root, tmp_path / "timed-out-inventory", class_queries=["Read"])
    assert result["status"] == "stopped_failure" and result["retry"] is False and not parsed_empty
    assert result["error"] == {"type": "TimeoutError", "message": "runtime worker timed out; detachment needs review"}
    assert result["resource_review_required"] is True
    assert result["native_calls"] is None and result["known_native_calls"] == 6
    assert result["g_step_dispatches"] == len(control["calls"]) == 8
    assert result["steps"][-1]["status"] == "failed" and result["steps"][-1]["native_calls"] is None
    state = G.R.load_json(root / "session.json")
    assert state["status"] == "resource_review_required" and state["steps"][-1]["status"] == "failed"
    assert (root / "s0007/runtime/worker-output.json").read_bytes() == b""
    with pytest.raises(G.R.ResourceReviewRequired, match="review failed provider resources"):
        G.step(root, {"op": "discover", "provider": "native", "request": {"operations": [
            {"op": "classes", "owner": "app", "query": "Read", "limit": 32}]}})
    assert len(control["calls"]) == 8 and state["proof"] is None and result["model_calls"] == 0


def test_receiver_scaffold_executes_measured_route_and_supplies_citable_model_view(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Receiver observed; task method still unknown."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "scaffold", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   max_context_chars=100000)
    assert result["status"] == "model_stopped" and len(seen) == 1
    receipt = result["discovery_routine"]
    assert receipt["status"] == "observed" and receipt["route"] == "main_window"
    assert "class_inventory_queries" not in receipt and "class_inventory" not in receipt
    assert receipt["bounds"]["steps"] == 7
    assert receipt["native_calls"] == receipt["known_native_calls"] == 6
    assert receipt["g_step_dispatches"] == result["tool_dispatches"] == len(control["calls"]) == 7
    assert len(result["decisions"]) == 1 and receipt["model_calls"] == receipt["task_invocations"] == 0
    assert [x["class"] for x in receipt["observations"]] == ["ReceiptApplication", "ReceiptWindow", "ReceiptController"]
    assert [x["native_calls"] for x in receipt["steps"]] == [0, 0, 1, 0, 2, 0, 3]
    view = json.loads(seen[0]["messages"][1]["content"])
    index = RP.validate(receipt, root)
    assert view["receiver_observations"] == index and "not_model_discoveries" in index["provenance"]
    assert '"expected_class"' not in json.dumps(view["decision_protocol"]["schema"])
    for observation in index["observations"]:
        assert "alias" not in json.dumps(observation["recipe"]) and "handle" not in json.dumps(observation)
        assert observation["observation_citation"]["artifact"] in view["artifacts"]
    state = G.R.load_json(root / "session.json")
    assert all(row["expected_runtime_identity"] == receipt["identity"] for row in state["steps"])
    assert state["proof"] is None and not state["recordings"] and state.get("hypothesis") is None
    assert all(op["op"] != "invoke" for request in control["calls"] for op in request["operations"])


def test_receiver_scaffold_preserves_measured_leading_dot_runtime_classes(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    window = "..NSKVONotifying_IINA.MainWindow"
    controller = ".NSKVONotifying_Receipt.Controller"
    control.update(returned_classes={"mainWindow": window, "windowController": controller},
                   inventory_class_prefix="..NSKVONotifying_")
    receipt = RP.run(root, tmp_path / "leading-dot-classes", class_queries=["read"])
    index = RP.validate(receipt, root)
    assert receipt["status"] == "observed" and receipt["error"] is None
    assert [row["class"] for row in index["observations"]] == ["ReceiptApplication", window, controller]
    assert receipt["native_calls"] == receipt["known_native_calls"] == 6
    assert receipt["g_step_dispatches"] == len(control["calls"]) == 8
    assert receipt["model_calls"] == receipt["task_invocations"] == 0
    methods = [op for request in control["calls"] for op in request["operations"] if op["op"] == "methods"]
    assert methods[-1]["class"] == window and methods[-1]["query"] == "windowController"
    final_read = control["calls"][-2]["operations"]
    assert final_read[-1]["expected_class"] == window
    for observation in index["observations"]:
        worker = G.R.load_json(observation["observation_citation"]["file"])
        assert worker["operations"][observation["hop"]]["fields"][0]["value"]["class"] == observation["class"]
    inventory = index["class_inventory"]["operations"][0]["rows"]
    assert inventory[0]["name"] == "..NSKVONotifying_ReadOwner0"
    state = G.R.load_json(root / "session.json")
    assert state["proof"] is None and state.get("hypothesis") is None and not state["recordings"]


@pytest.mark.parametrize("failure,expected_calls,expected_native", [("abi", 2, 0), ("unsupported", 3, 1), ("cleanup", 3, 1)])
def test_receiver_scaffold_stops_without_model_or_retry_and_retains_actual_counts(receiver_runtime, tmp_path, failure, expected_calls, expected_native):
    root, control = receiver_runtime
    control["failure"] = failure
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / failure, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    receipt = result["discovery_routine"]
    assert receipt["status"] == "stopped_failure" and receipt["native_calls"] == expected_native
    assert len(control["calls"]) == expected_calls and receipt["retry"] is False
    assert not receipt["observations"] and not result["decisions"]
    assert (tmp_path / failure / "receiver-probe/probe.json").is_file()


@pytest.mark.parametrize("depth,steps,native_calls", [(0, 3, 1), (1, 5, 3), (2, 7, 6)])
def test_receiver_scaffold_measured_nil_retains_partial_citations_and_continues_model(receiver_runtime, tmp_path, depth, steps, native_calls):
    root, control = receiver_runtime
    control["nil_depth"] = depth
    adapter, seen = model([{"action": "step", "request": {"op": "evidence", "artifact": "inventory.json"},
        "reason": "The declared route is unavailable; inspect independent registered inventory."},
        {"action": "stop", "request": None, "reason": "No task method established."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "nil-route", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["error"] is None
    assert len(seen) == result["model_calls"] == len(result["decisions"]) == 2
    receipt = result["discovery_routine"]
    assert receipt["status"] == "route_unavailable" and receipt["error"] is None
    assert receipt.get("final_review_error") is None and receipt["retry"] is False
    assert receipt["g_step_dispatches"] == len(control["calls"]) == steps
    assert result["tool_dispatches"] == steps + 1
    assert receipt["native_calls"] == receipt["known_native_calls"] == native_calls
    assert receipt["model_calls"] == receipt["task_invocations"] == 0
    index = RP.validate(receipt, root)
    unavailable = index["unavailable"]
    assert unavailable["reason"] == "measured_nil" and unavailable["hop"] == unavailable["prefix_length"] == depth
    assert not {"recipe", "class", "observation_citation"}.intersection(unavailable)
    assert len(index["observations"]) == depth
    null = unavailable["null_citation"]
    assert null["pointer"] == f"/operations/{depth}/fields/0/value"
    worker = G.R.load_json(null["file"])
    assert worker["operations"][depth]["fields"][0]["value"] is None
    assert worker["operations"][depth]["fields"][0]["metadata"]["return_type"] == "object"
    for payload in seen:
        view = json.loads(payload["messages"][1]["content"])
        assert view["receiver_observations"] == index
        assert unavailable["null_citation"]["artifact"] in view["artifacts"]
        assert sum(len(message["content"]) for message in payload["messages"]) <= 100000
    state = G.R.load_json(root / "session.json")
    assert state["proof"] is None and state.get("hypothesis") is None and not state["recordings"]
    assert all(operation["op"] != "invoke" for request in control["calls"] for operation in request["operations"])
    assert all(operation.get("query") != "keyWindow" for request in control["calls"] for operation in request["operations"])


@pytest.mark.parametrize("depth,route_steps,native_calls", [(None, 7, 6), (0, 3, 1), (1, 5, 3), (2, 7, 6)])
def test_optional_task_class_inventory_supplies_actual_rows_after_clean_route(receiver_runtime, tmp_path, depth, route_steps, native_calls):
    root, control = receiver_runtime
    control.update(nil_depth=depth, inventory_matched=40)
    adapter, seen = model([{"action": "step", "request": {"op": "evidence", "artifact": "inventory.json"},
        "reason": "Inspect independent evidence after the measured class search."},
        {"action": "stop", "request": None, "reason": "No measured task method yet."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    out = tmp_path / "class-inventory"
    result = D.run(root, out, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   discovery_class_queries=["READ", "result"], max_context_chars=150000)
    assert result["status"] == "model_stopped" and result["error"] is None and result["model_calls"] == len(seen) == 2
    receipt = result["discovery_routine"]
    assert receipt["g_step_dispatches"] == route_steps + 1 == len(control["calls"])
    assert result["tool_dispatches"] == route_steps + 2
    assert receipt["native_calls"] == receipt["known_native_calls"] == native_calls
    assert receipt["steps"][-1]["native_calls"] == receipt["task_invocations"] == receipt["model_calls"] == 0
    assert control["calls"][-1] == {"operations": [
        {"op": "classes", "owner": "app", "query": "READ", "limit": 32},
        {"op": "classes", "owner": "app", "query": "result", "limit": 32}]}
    index = RP.validate(receipt, root)
    inventory = index["class_inventory"]
    assert inventory["queries"] == ["READ", "result"] and inventory["max_indexed_rows"] == 128
    assert "not_model_decisions_or_receiver_or_task_proof" in inventory["provenance"]
    assert len(json.dumps(inventory, ensure_ascii=False, sort_keys=True).encode()) <= RP.CLASS_INDEX_BYTES
    for operation in inventory["operations"]:
        assert operation["matched"] == 40 and operation["truncated"] is True and operation["rows_in_artifact"] == 32
        assert operation["summary_omitted_rows"] == 0 and len(operation["rows"]) == 32
        raw = G.R.load_json(inventory["source"]["file"])
        for item in operation["rows"]:
            found = B._json_pointer(raw, item["pointer"])
            assert {key: item[key] for key in ("name", "module", "origin")} == found
            assert item["module"] == control["identity"]["executable"] and "recipe" not in item
    for payload in seen:
        view = json.loads(payload["messages"][1]["content"])
        assert view["receiver_observations"] == index
        assert any("never concatenate a namespace" in instruction for instruction in view["agent_instructions"])
        assert sum(len(message["content"]) for message in payload["messages"]) <= 150000
    redaction = G.R.load_json(out / "decision-000/redaction.json")
    assert redaction["receiver_observations"]["metadata_inventory"] == {
        "g_steps": 1, "native_calls": 0, "model_calls": 0,
        "source": "caller_selected_harness_metadata_scaffold_not_model_decisions"}
    memory = G.R.load_json(out / "memory.json")
    assert len(memory["turns"]) == 1 and memory["turns"][0]["op"] == "evidence"
    state = G.R.load_json(root / "session.json")
    assert state["proof"] is None and state.get("hypothesis") is None and not state["recordings"]


@pytest.mark.parametrize("queries", [[], ["Read"] * 2, ["Read", "READ"], ["Read"] * 5,
    ["not in the task"], [" Read"], ["Read\n"], [True], "Read", ["x" * 65]])
def test_task_class_queries_refuse_before_output_or_model(receiver_runtime, tmp_path, queries):
    root, control = receiver_runtime
    adapter, seen = model([decision()])
    out = tmp_path / "invalid-class-queries"
    with pytest.raises(ValueError, match="class inventory"):
        D.run(root, out, adapter=adapter, decision_protocol="full_native_v1",
              discovery_routine=RP.ID, discovery_class_queries=queries)
    assert not out.exists() and not control["calls"] and not seen


def test_task_query_accepts_casefold_literal_inside_task_word(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    state = G.R.load_json(root / "session.json")
    state["task"] = "Pause the playing video"
    G.R.write_json(root / "session.json", state)
    receipt = RP.run(root, tmp_path / "literal-substring", class_queries=["PLAY", "pause", "video"])
    assert receipt["status"] == "observed"
    assert RP.validate(receipt, root)["class_inventory"]["queries"] == ["PLAY", "pause", "video"]


@pytest.mark.parametrize("failure", ["inventory_cleanup", "inventory_native"])
def test_task_inventory_uncertain_cleanup_or_nonzero_native_count_stops_before_model(receiver_runtime, tmp_path, failure):
    root, control = receiver_runtime
    control.update(nil_depth=1, failure=failure)
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / failure, adapter=adapter, decision_protocol="full_native_v1",
                   discovery_routine=RP.ID, discovery_class_queries=["Read"], max_context_chars=100000)
    assert result["status"] == result["discovery_routine"]["status"] == "stopped_failure"
    assert result["model_calls"] == 0 and seen == [] and len(control["calls"]) == 6
    assert result["discovery_routine"]["g_step_dispatches"] == 6


@pytest.mark.parametrize("tamper", ["query", "module", "cleanup"])
def test_task_inventory_rederives_index_and_raw_cleanup_before_native_dispatch(receiver_runtime, tmp_path, tamper):
    root, control = receiver_runtime
    out = tmp_path / "class-tamper"
    def change():
        receipt = G.R.load_json(out / "receiver-probe/probe.json")
        pin = receipt["class_inventory"]["source"]
        if tamper == "query":
            file = out / "receiver-probe/receiver-index.json"
            value = G.R.load_json(file)
            value["class_inventory"]["queries"] = ["result"]
        else:
            file = Path(pin["file"])
            value = G.R.load_json(file)
            if tamper == "module":
                value["operations"][0]["rows"][0]["module"] = "/unrelated/module"
            else:
                value["cleanup"]["detached"] = False
        G.R.write_json(file, value)
    adapter, seen = model([{"action": "step", "request": {"op": "discover", "provider": "native",
        "request": {"operations": [{"op": "methods", "class": "ReadOwner0", "query": "Read", "own": False}]}},
        "reason": "Inspect a measured class."}], hook=change)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, out, adapter=adapter, decision_protocol="full_native_v1", discovery_routine=RP.ID,
                   discovery_class_queries=["Read"], max_context_chars=100000)
    assert result["status"] == "stopped_failure" and len(seen) == 1
    assert result["tool_dispatches"] == len(control["calls"]) == 8
    assert result.get("receiver_final_review_error") is not None


def test_task_inventory_rederives_forged_self_consistent_index_from_raw_rows(receiver_runtime, tmp_path):
    root, _ = receiver_runtime
    receipt = RP.run(root, tmp_path / "forged-inventory", class_queries=["Read"])
    receipt["class_inventory"]["operations"][0]["rows"][0]["name"] = "ReadInventedOwner"
    file = Path(receipt["index"]["file"])
    index = G.R.load_json(file)
    index["class_inventory"] = copy.deepcopy(receipt["class_inventory"])
    G.R.write_json(file, index)
    receipt["index"] = RP._pin(file)
    with pytest.raises(ValueError, match="actual raw class rows"):
        RP.validate(receipt, root)


def test_task_inventory_reserves_storage_again_before_extra_metadata_batch(receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    control["nil_depth"] = 1
    monkeypatch.setattr(RP.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=0 if len(control["calls"]) >= 5 else 2**40))
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / "inventory-storage", adapter=adapter, decision_protocol="full_native_v1",
                   discovery_routine=RP.ID, discovery_class_queries=["Read"], max_context_chars=100000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and seen == []
    assert len(control["calls"]) == result["discovery_routine"]["g_step_dispatches"] == 5
    assert "storage reserve" in result["discovery_routine"]["error"]["message"]


def test_task_inventory_cannot_displace_context_when_full_visible_budget_is_too_small(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / "inventory-context", adapter=adapter, decision_protocol="full_native_v1",
                   discovery_routine=RP.ID, discovery_class_queries=["Read"], max_context_chars=4096)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert len(control["calls"]) == result["discovery_routine"]["g_step_dispatches"] == 8
    assert "context" in result["error"]["message"]


def test_task_inventory_projects_whole_rows_with_explicit_byte_omissions(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    state = G.R.load_json(root / "session.json")
    queries = ["Read", "requested", "complete", "result"]
    rows = [{"index": i, "op": "classes", "status": "ok", "matched": 40, "truncated": True,
             "rows": [{"name": query.title() + f"Owner{j}", "module": state["bundle"] + "/" + "x" * 3000,
                       "origin": "module_backed"} for j in range(32)]} for i, query in enumerate(queries)]
    source = {"artifact": "raw.json", "file": "/inert/raw.json", "bytes": 0, "sha256": "0" * 64}
    value = RP._class_inventory(queries, state["task"], RP._frozen(state), "s0007", rows, source,
                                state["bundle"], control["identity"]["executable"])
    assert len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()) <= RP.CLASS_INDEX_BYTES
    indexed = sum(len(item["rows"]) for item in value["operations"])
    omitted = sum(item["summary_omitted_rows"] for item in value["operations"])
    assert 0 < indexed < 128 and indexed + omitted == 128
    assert all(item["matched"] == 40 and item["truncated"] is True and item["rows_in_artifact"] == 32
               for item in value["operations"])


@pytest.mark.parametrize("failure", ["nil_missing_value", "nil_bad_metadata", "nil_prefix_drift", "nil_prefix", "nil_cleanup"])
def test_receiver_scaffold_does_not_continue_on_malformed_or_uncertain_nil(receiver_runtime, tmp_path, failure):
    root, control = receiver_runtime
    control.update(nil_depth=1, failure=failure)
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / failure, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID)
    assert result["status"] == result["discovery_routine"]["status"] == "stopped_failure"
    assert result["model_calls"] == 0 and not seen and not result["decisions"]
    assert len(control["calls"]) == 5 and result["discovery_routine"]["native_calls"] == 3
    assert result["discovery_routine"]["task_invocations"] == 0


def test_receiver_scaffold_nil_rechecks_exact_raw_null_and_cleanup_before_later_use(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    control["nil_depth"] = 1
    result = RP.run(root, tmp_path / "partial-route")
    assert result["status"] == "route_unavailable"
    index = RP.validate(result, root)
    index["unavailable"]["null_citation"]["pointer"] = "/operations/0/fields/0/value"
    result["unavailable"] = copy.deepcopy(index["unavailable"])
    file = Path(result["index"]["file"])
    G.R.write_json(file, index)
    result["index"] = RP._pin(file)
    with pytest.raises(ValueError, match="nil citation"):
        RP.validate(result, root)


def test_receiver_scaffold_nil_final_validation_rejects_changed_raw_worker_even_after_model_stop(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    control["nil_depth"] = 1
    out = tmp_path / "nil-changed-after-model"
    def changed_worker():
        receipt = G.R.load_json(out / "receiver-probe/probe.json")
        Path(receipt["steps"][-1]["worker"]["file"]).write_text("{}")
    adapter, seen = model([{"action": "stop", "request": None, "reason": "The measured route is unavailable."}], hook=changed_worker)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, out, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   max_context_chars=100000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == len(seen) == 1
    assert result["receiver_final_review_error"] and "evidence changed" in result["error"]["message"]
    assert len(control["calls"]) == 5 and result["tool_dispatches"] == 5


@pytest.mark.parametrize("nil_depth", [None, 1])
def test_receiver_scaffold_rechecks_raw_sources_and_process_before_model_dispatch(receiver_runtime, tmp_path, nil_depth):
    root, control = receiver_runtime
    control["nil_depth"] = nil_depth
    result = RP.run(root, tmp_path / "probe", route="key_window")
    assert result["status"] == ("observed" if nil_depth is None else "route_unavailable") and result["route"] == "key_window"
    control["identity"] = {**control["identity"], "launch_time": 124.25}
    with pytest.raises(ValueError, match="process/adapter changed"):
        RP.validate(result, root)
    control["identity"] = copy.deepcopy(result["identity"])
    pin = result["steps"][-1]["worker"]
    Path(pin["file"]).write_text("{}")
    with pytest.raises(ValueError, match="evidence changed"):
        RP.validate(result, root)


@pytest.mark.parametrize("options", [{"discovery_route": "main_window"}, {"discovery_routine": "unknown"},
    {"discovery_class_queries": ["Read"]},
    {"discovery_routine": RP.ID}, {"discovery_routine": RP.ID, "decision_protocol": "full_native_v1", "until": "step"},
    {"discovery_routine": RP.ID, "decision_protocol": "full_native_v1", "allowed_tools": ["discover", "evidence"]}])
def test_receiver_scaffold_requires_explicit_compatible_scope_before_output(inert, tmp_path, options):
    adapter, seen = model([decision()])
    out = tmp_path / "refused-probe"
    with pytest.raises(ValueError):
        D.run(inert.root, out, adapter=adapter, **options)
    assert not out.exists() and not inert.calls and not seen


def test_receiver_scaffold_is_default_off_and_cli_requires_explicit_selection(inert, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_cli
    adapter, seen = model([decision()])
    result = D.run(inert.root, tmp_path / "ordinary-driver", adapter=adapter)
    assert result["status"] == "model_stopped" and "discovery_routine" not in result
    assert "receiver_observations" not in json.loads(seen[0]["messages"][1]["content"])
    calls = []
    monkeypatch.setattr(D, "run", lambda *a, **kw: calls.append((a, kw)) or {"status": "fixture"})
    answer = CliRunner().invoke(rea_cli.agent_commands, ["run", str(inert.root), "--out", str(tmp_path / "cli"),
        "--decision-protocol", "full_native_v1", "--discovery-routine", RP.ID, "--discovery-route", "key_window"])
    assert answer.exit_code == 0, answer.output
    assert calls[0][1]["discovery_routine"] == RP.ID and calls[0][1]["discovery_route"] == "key_window"
    assert "discovery_class_queries" not in calls[0][1]
    answer = CliRunner().invoke(rea_cli.agent_commands, ["run", str(inert.root), "--out", str(tmp_path / "cli-inventory"),
        "--decision-protocol", "full_native_v1", "--discovery-routine", RP.ID,
        "--discovery-class-query", "Read", "--discovery-class-query", "result"])
    assert answer.exit_code == 0, answer.output
    assert calls[1][1]["discovery_class_queries"] == ["Read", "result"]


def test_receiver_scaffold_reserves_storage_before_any_g_or_model_call(receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    monkeypatch.setattr(RP.shutil, "disk_usage", lambda *args: SimpleNamespace(free=RP.HEADROOM_BYTES))
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / "full-disk", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID)
    assert result["status"] == "stopped_failure" and not control["calls"] and not seen
    assert result["discovery_routine"]["g_step_dispatches"] == result["model_calls"] == 0
    assert "storage reserve" in result["discovery_routine"]["error"]["message"]


def test_receiver_scaffold_checks_full_session_device_even_when_output_device_has_room(receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    # Simulated separate devices: only the actual G evidence destination is full.
    monkeypatch.setattr(RP, "_storage_paths", lambda session_root, folder: [folder, session_root])
    checked = []
    def free(destination):
        checked.append(destination)
        return SimpleNamespace(free=RP.HEADROOM_BYTES if destination == root else 4 * RP.EVIDENCE_BYTES)
    monkeypatch.setattr(RP.shutil, "disk_usage", free)
    adapter, seen = model([decision()])
    result = D.run(root, tmp_path / "separate-output-disk", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID)
    assert root in checked and result["status"] == "stopped_failure" and not control["calls"] and not seen
    assert result["discovery_routine"]["g_step_dispatches"] == result["model_calls"] == 0
    assert str(root) in result["discovery_routine"]["error"]["message"]


def test_receiver_scaffold_retains_prefix_calls_when_class_changes_before_next_getter(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    control["failure"] = "class_change"
    result = RP.run(root, tmp_path / "changed-class")
    assert result["status"] == "stopped_failure" and len(control["calls"]) == 5
    assert result["native_calls"] == 2 and result["steps"][-1]["native_calls"] == 1
    assert len(result["observations"]) == 1 and result["observations"][0]["class"] == "ReceiptApplication"
    assert result["retry"] is False and result["model_calls"] == 0


def test_receiver_scaffold_restores_verified_model_memory_before_appending_separate_steps(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    first, _ = model([{"action": "step", "request": {"op": "evidence", "artifact": "inventory.json"}, "reason": "Read actual inventory."}])
    first.config = replace(first.config, structured_output_mode="json_object")
    first_result = D.run(root, tmp_path / "first-leg", adapter=first, decision_protocol="full_native_v1",
                         allowed_tools=["discover", "runtime", "evidence"], until="step", max_context_chars=100000)
    assert first_result["status"] == "boundary_reached" and not control["calls"]
    second, seen = model([{"action": "stop", "request": None, "reason": "Measured receiver needs a task method."}])
    second.config = replace(second.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "second-leg", adapter=second, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   resume_from=tmp_path / "first-leg", max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["investigation_memory"]["restored_turns"] == 1
    assert result["tool_dispatches"] == 7 and len(result["decisions"]) == 1
    memory = json.loads((tmp_path / "second-leg/memory.json").read_text())
    assert len(memory["turns"]) == 1 and memory["turns"][0]["reason_source"] == "model_authored_untrusted_not_fact_or_proof"
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["receiver_observations"]["provenance"].startswith("harness_scaffold")


@pytest.mark.parametrize("nil_depth,steps", [(None, 7), (1, 5)])
def test_receiver_scaffold_process_change_after_model_reply_stops_before_native_dispatch(receiver_runtime, tmp_path, nil_depth, steps):
    root, control = receiver_runtime
    control["nil_depth"] = nil_depth
    def restart():
        control["identity"] = {**control["identity"], "launch_time": 124.25}
    adapter, seen = model([{"action": "step", "request": {"op": "discover", "provider": "native",
        "request": {"operations": [{"op": "classes", "query": "Receipt", "owner": "app", "limit": 8}]}},
        "reason": "Inspect additional measured classes."}], hook=restart)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "restarted-after-model", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID, max_context_chars=100000)
    assert result["status"] == "stopped_failure" and len(seen) == result["model_calls"] == 1
    assert len(control["calls"]) == result["tool_dispatches"] == steps
    assert result["receiver_final_review_error"] and "process/adapter changed" in result["error"]["message"]


def test_receiver_scaffold_native_identity_handoff_continues_for_model_tools(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    request = {"op": "discover", "provider": "native", "request": {"operations": [
        {"op": "classes", "query": "NSApplication", "owner": "all", "limit": 8}]}}
    adapter, seen = model([{"action": "step", "request": request, "reason": "Reread native root metadata."},
                           {"action": "stop", "request": None, "reason": "Task method remains unknown."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "continued-identity", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID, max_context_chars=100000)
    assert result["status"] == "model_stopped" and len(control["calls"]) == result["tool_dispatches"] == 8
    state = G.R.load_json(root / "session.json")
    assert state["steps"][-1]["expected_runtime_identity"] == result["discovery_routine"]["identity"]
    assert len(seen) == len(result["decisions"]) == 2


@pytest.fixture(autouse=True)
def symbol_readiness(monkeypatch):
    """No static driver test depends on the host's developer-tool installation."""
    calls = []
    ready = {"status": "ready", "tool": {"file": "/fixture/real-toolchain/nm",
             "sha256": "1" * 64, "bytes": 123}, "why": "inert ready fixture",
             "error": None, "tool_invocations": 0}
    def inspect(nm=None):
        calls.append(nm)
        return copy.deepcopy(ready)
    monkeypatch.setattr(D.S, "tool_readiness", inspect, raising=False)
    return calls


@pytest.fixture
def inert(tmp_path, monkeypatch):
    app = tmp_path / "Owned.app"
    app.mkdir()
    root = tmp_path / "session"
    root.mkdir()
    state = {"bundle": str(app), "plan": {"learning": [{"inputs": {"text": "TRAINING_PRIVATE"}}],
             "verification": [{"inputs": {"text": "FRESH_ORACLE_PRIVATE"}}],
             "heldout": [{"inputs": {"text": "HELDOUT_ORACLE_PRIVATE"}}]},
             "status": "investigating", "history": [], "artifacts": {}}
    def save():
        (root / "session.json").write_text(json.dumps(state))
    save()
    def context(_):
        result = {"format": G.SESSION, "task": "Inspect the owned target operation", "target": {},
                "status": state["status"], "params": [], "history": copy.deepcopy(state["history"]),
                "artifacts": copy.deepcopy(state["artifacts"]), "plan": copy.deepcopy(state["plan"]),
                "hypothesis": {"actor": "OLD_RECIPE_PRIVATE"}, "source": "REPO_PRIVATE",
                "recorded_cases": [], "seconds_remaining": 100}
        if "observer_profile" in state:
            result.update(observer_profile=G._public_observer(state["observer_profile"]),
                          observer_profile_sha256=state["observer_profile_sha256"])
        return result
    calls, payloads = [], {}
    def step(_, request):
        calls.append(copy.deepcopy(request))
        if request["op"] == "propose":
            state["status"] = "hypothesis"
        row = {"step": len(state["history"]), "status": "completed", "request": request,
               "feedback": copy.deepcopy(payloads.get(request.get("relative", request["op"]), {"status": state["status"]}))}
        state["history"].append(row)
        name = f"s{row['step']:04d}/feedback.json"
        file = root / name
        file.parent.mkdir()
        file.write_text(json.dumps(row))
        state["artifacts"][name] = {"file": str(file), "sha256": D.hashlib.sha256(file.read_bytes()).hexdigest(),
                                     "bytes": file.stat().st_size}
        save()
        return {"feedback": copy.deepcopy(row), "context": context(root)}
    monkeypatch.setattr(G.R, "assert_guest", lambda: None)
    monkeypatch.setattr(G, "context", context)
    monkeypatch.setattr(G, "step", step)
    return SimpleNamespace(root=root, app=app, state=state, save=save, calls=calls, payloads=payloads)


def test_model_stop_is_honest_and_never_dispatches(inert, tmp_path):
    adapter, seen = model([decision(reason="No supported typed receiver is established.")])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "model_stopped" and not inert.calls
    assert result["model_calls"] == result["process_model_calls"] == 1
    assert len(seen) == 1 and result["budget_after"]["spent_usd"] > 0
    assert (tmp_path / "driver/decision-000/model-output.json").is_file()


def test_context_projection_withholds_plan_oracles_raw_history_source_and_old_recipe(inert, tmp_path):
    inert.state["history"] = [{"step": 0, "status": "completed", "request": {"op": "record", "secret": "HISTORY_PRIVATE"}}]
    inert.state["artifacts"] = {"s0000/record/case.json": {"private": "PROOF_PRIVATE"}}
    inert.save()
    adapter, seen = model([decision()])
    D.run(inert.root, tmp_path / "driver", adapter=adapter)
    sent = json.dumps(seen)
    for value in ["TRAINING_PRIVATE", "FRESH_ORACLE_PRIVATE", "HELDOUT_ORACLE_PRIVATE", "OLD_RECIPE_PRIVATE",
                  "REPO_PRIVATE", "HISTORY_PRIVATE", "PROOF_PRIVATE"]:
        assert value not in sent
    preimage = (tmp_path / "driver/decision-000/session-before.json").read_text()
    assert "HELDOUT_ORACLE_PRIVATE" in preimage
    manifest = json.loads((tmp_path / "driver/decision-000/redaction.json").read_text())
    assert manifest["context_truncation"] is False and manifest["omitted_artifacts"] == ["s0000/record/case.json"]


@pytest.mark.parametrize("reply", [
    {"action": "step", "request_json": '{"op":"shell","cmd":"open"}', "reason": "Attempt"},
    decision({"op": "observe", "shell": "open"}),
    decision({"op": "resource"}),
    {**decision(), "unexpected": True},
    {"action": "stop", "request_json": '{"op":"observe"}', "reason": "Stop"},
    '{"action":"stop","action":"step","request_json":"","reason":"x"}',
    "not JSON",
])
def test_malformed_unknown_or_extra_key_reply_stops_before_dispatch(inert, tmp_path, reply):
    adapter, seen = model([reply, decision({"op": "observe"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0
    assert len(seen) == 1 and not inert.calls
    assert (tmp_path / "driver/decision-000/failure.json").is_file()


def test_evidence_cannot_read_trial_or_external_oracle(inert, tmp_path):
    adapter, _ = model([decision({"op": "evidence", "artifact": "/private/oracle.json"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "stopped_failure" and not inert.calls


def test_real_G_proposal_gate_is_never_replaced_by_model_confidence(session, tmp_path):
    root, _, _ = session
    request = {"op": "propose", "rationale": "The model thinks it works", "citations": [],
               "actor": {"kind": "objc_method"}}
    adapter, seen = model([decision(request), decision({"op": "record", "case": 0})])
    result = D.run(root, tmp_path / "driver", adapter=adapter)
    actual = G.R.load_json(root / "session.json")
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 1 and len(seen) == 1
    assert actual["steps"][-1]["status"] == "failed" and actual["proof"] is None
    assert "candidate" not in actual and not actual["recordings"]


def test_actual_supported_hypothesis_is_default_boundary(session, tmp_path):
    root = session[0]
    request = proposal(session)
    adapter, seen = model([decision(request), decision({"op": "record", "case": 0})])
    result = D.run(root, tmp_path / "driver", adapter=adapter)
    state = G.R.load_json(root / "session.json")
    assert result["status"] == "boundary_reached" and state["status"] == "hypothesis", result.get("error")
    assert len(seen) == 1 and not state["recordings"] and state["proof"] is None


@pytest.mark.parametrize("op", ["record", "verify", "heldout"])
def test_each_trial_returns_needs_setup_before_another_trial(inert, tmp_path, op):
    adapter, seen = model([decision({"op": op, "case": 0}), decision({"op": op, "case": 1})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, until="published")
    assert result["status"] == "needs_setup" and len(inert.calls) == len(seen) == 1
    assert result["setup_performed"] is False


@pytest.mark.parametrize("status", list(D.FAILURES))
def test_failed_session_never_gets_another_model_or_tool_call(inert, tmp_path, status):
    inert.state["status"] = status
    inert.save()
    adapter, seen = model([decision({"op": "observe"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "session_requires_review" and result["model_calls"] == 0
    assert not seen and not inert.calls


def test_failed_history_is_preserved_and_not_retried(inert, tmp_path):
    inert.state["history"] = [{"step": 0, "status": "failed", "request": {"op": "discover"}}]
    inert.save()
    before = (inert.root / "session.json").read_bytes()
    adapter, seen = model([decision({"op": "discover", "request": {}})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "session_requires_review" and not seen and not inert.calls
    assert (inert.root / "session.json").read_bytes() == before


def test_model_failure_and_spend_refusal_do_not_retry_or_dispatch(inert, tmp_path):
    adapter, seen = model([503, decision({"op": "observe"})])
    result = D.run(inert.root, tmp_path / "failed", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 1 and len(seen) == 1
    empty, sent = model([decision()], budget=M.RunBudget(0))
    result = D.run(inert.root, tmp_path / "budget", adapter=empty)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not sent and not inert.calls


def test_interrupt_retains_exact_attempt_then_propagates(inert, tmp_path):
    adapter, _ = model([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        D.run(inert.root, tmp_path / "driver", adapter=adapter)
    result = json.loads((tmp_path / "driver/driver.json").read_text())
    assert result["status"] == "interrupted" and result["model_calls"] == 1 and not inert.calls
    assert (tmp_path / "driver/decision-000/failure.json").is_file()


def test_context_overflow_is_retained_refusal_without_truncation_or_model(inert, tmp_path):
    adapter, seen = model([decision()])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, max_context_chars=4096)
    assert result["status"] == "stopped_failure" and not seen and not inert.calls
    inp = json.loads((tmp_path / "driver/decision-000/model-input.json").read_text())
    assert sum(len(m["content"]) for m in inp["messages"]) > 4096


def test_changed_session_between_model_read_and_dispatch_refuses(inert, tmp_path):
    def change():
        inert.state["external_change"] = True
        inert.save()
    adapter, seen = model([decision({"op": "observe"})], hook=change)
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "stopped_failure" and len(seen) == 1 and not inert.calls
    assert "session changed" in result["error"]["message"]


def test_closed_choice_client_cannot_be_reported_as_reasoning_driver(inert, tmp_path):
    result = D.run(inert.root, tmp_path / "driver", adapter=SimpleNamespace(decide=lambda *_: {}))
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not inert.calls


def test_output_cannot_mutate_session_or_app(inert):
    for out in [inert.root / "driver", inert.app / "driver"]:
        with pytest.raises(ValueError):
            D.run(inert.root, out)
        assert not out.exists()


def test_handled_provider_unavailability_is_not_retried(inert, tmp_path, monkeypatch):
    def unavailable(_, request):
        inert.calls.append(request)
        return {"feedback": {"status": "completed", "feedback": {
                    "status": "unavailable", "error": {"type": "MissingProvider"}}},
                "context": G.context(inert.root)}
    monkeypatch.setattr(G, "step", unavailable)
    adapter, seen = model([decision({"op": "discover", "request": {}}), decision({"op": "observe"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "stopped_failure" and len(inert.calls) == len(seen) == 1


@pytest.mark.parametrize("status", ["recording", "candidate", "verified"])
def test_default_boundary_never_resumes_goals_after_existing_hypothesis(inert, tmp_path, status):
    inert.state["status"] = status
    inert.save()
    adapter, seen = model([decision({"op": "record", "case": 0})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "boundary_reached" and not seen and not inert.calls


def test_tool_interrupt_preserves_dispatched_attempt_without_retry(inert, tmp_path, monkeypatch):
    def interrupted(_, request):
        inert.calls.append(request)
        raise KeyboardInterrupt()
    monkeypatch.setattr(G, "step", interrupted)
    adapter, seen = model([decision({"op": "observe"}), decision({"op": "observe"})])
    with pytest.raises(KeyboardInterrupt):
        D.run(inert.root, tmp_path / "driver", adapter=adapter)
    result = json.loads((tmp_path / "driver/driver.json").read_text())
    assert result["status"] == "interrupted" and result["tool_dispatches"] == 1
    assert result["decisions"][0]["tool_dispatched"] and len(seen) == len(inert.calls) == 1


def test_decision_and_step_limits_stop_without_extra_calls(inert, tmp_path):
    adapter, seen = model([decision({"op": "observe"})] * 3)
    result = D.run(inert.root, tmp_path / "limited", adapter=adapter, max_decisions=2)
    assert result["status"] == "decision_budget_exhausted" and len(seen) == len(inert.calls) == 2
    adapter, seen = model([decision({"op": "observe"})] * 2)
    result = D.run(inert.root, tmp_path / "one-step", adapter=adapter, until="step")
    assert result["status"] == "boundary_reached" and len(seen) == 1


@pytest.mark.parametrize("tool_request", [
    {"op": "runtime", "request": {}},
    {"op": "propose", "rationale": "Try the actor", "citations": [], "actor": {"kind": "objc_method"}},
])
def test_static_admission_hides_and_refuses_runtime_or_propose_before_dispatch(inert, tmp_path, tool_request):
    admitted = ["analyze", "symbols", "evidence", "resource"]
    # Mutating the caller's original list during inference cannot widen this run.
    adapter, seen = model([decision(tool_request)], hook=lambda: admitted.append(tool_request["op"]))
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, allowed_tools=admitted)
    assert result["status"] == "stopped_failure" and not inert.calls and result["tool_dispatches"] == 0
    view = json.loads(seen[0]["messages"][1]["content"])
    assert set(view["tools"]) == {"analyze", "symbols", "evidence", "resource"}
    assert result["admitted_tools"] == ["analyze", "symbols", "evidence", "resource"]
    assert "outside caller's admitted tools" in result["error"]["message"]
    saved = json.loads((tmp_path / "driver/driver.json").read_text())
    redaction = json.loads((tmp_path / "driver/decision-000/redaction.json").read_text())
    assert saved["admitted_tools"] == redaction["admitted_tools"] == result["admitted_tools"]


def test_allowed_static_resource_uses_real_G_without_runtime_or_hypothesis(session, tmp_path):
    root = session[0]
    adapter, seen = model([decision({"op": "resource", "relative": "Contents/facts.plist"})])
    result = D.run(root, tmp_path / "driver", adapter=adapter, allowed_tools=("resource",), until="step")
    state = G.R.load_json(root / "session.json")
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == len(seen) == 1
    assert state["status"] == "investigating" and state.get("hypothesis") is None and state["proof"] is None
    assert state["steps"][-1]["request"] == {"op": "resource", "relative": "Contents/facts.plist"}
    retained = json.loads((tmp_path / "driver/decision-000/tool-result.json").read_text())
    assert retained["feedback"]["status"] == "completed"
    assert retained["feedback"]["feedback"]["status"] == "resource_collected"
    resource = next(name for name in retained["context"]["artifacts"] if name.endswith("resource.json"))
    assert G.R.load_json(root / resource)["content"]["handler"] == "TEST/READ"


def test_general_tool_object_uses_real_G_without_embedded_json(session, tmp_path):
    root = session[0]
    request = {"op": "resource", "relative": "Contents/facts.plist"}
    adapter, seen = model([{"action": "step", "request": request, "reason": "Read the actual admitted resource."}])
    result = D.run(root, tmp_path / "object-driver", adapter=adapter, allowed_tools=("resource",),
                   decision_protocol=D.TOOL_OBJECT, until="step")
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == 1
    assert G.R.load_json(root / "session.json")["steps"][-1]["request"] == request
    sent = json.loads(seen[0]["messages"][1]["content"])
    assert sent["decision_protocol"]["format"] == D.TOOL_OBJECT
    assert "example" not in sent["decision_protocol"]  # evidence was not admitted
    assert "request_json" not in sent["decision_protocol"]["schema"]["properties"]
    assert "response_format" not in seen[0]  # Generic nested data is not a strict provider schema.


def test_general_tool_object_preserves_explicit_json_object_model_transport(session, tmp_path):
    root = session[0]
    adapter, seen = model([{"action": "stop", "request": None, "reason": "No operation has been established."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "object-transport", adapter=adapter, allowed_tools=("resource",),
                   decision_protocol=D.TOOL_OBJECT)
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 0
    assert seen[0]["response_format"] == {"type": "json_object"}


def test_generic_nested_type_error_is_corrected_before_any_G_step_or_native_call(session, tmp_path):
    root = session[0]
    before = (root / "session.json").read_bytes()
    adapter, seen = model([
        {"action": "step", "request": {"op": "discover", "provider": "native",
         "request": {"operations": [{"op": "classes", "limit": True}]}}, "reason": "Malformed numeric bound."},
        {"action": "stop", "request": None, "reason": "The rejected request supplies no application evidence."},
    ])
    result = D.run(root, tmp_path / "generic-nested-correction", adapter=adapter, allowed_tools=("discover",),
                   decision_protocol=D.TOOL_OBJECT, max_decisions=2, max_decision_corrections=1)
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 0
    assert len(seen) == 2 and (root / "session.json").read_bytes() == before
    assert result["decisions"][0]["status"] == "decision_rejected"


def test_general_object_and_legacy_share_the_same_G_admission():
    tools = {"runtime": G.TOOLS["runtime"]}
    request = {"op": "runtime", "provider": "native", "request": {
        "operations": [{"op": "memory_ranges", "scope": {"kind": "all"}, "limit": 2, "as": "ranges"}]}}
    view = {"tools": tools, "artifacts": {}}
    legacy = D._request(decision(request), view)
    native = D._request({"action": "step", "request": request, "reason": "Inspect bounded current metadata."},
                        dict(view, decision_protocol={"format": D.TOOL_OBJECT}))
    assert legacy == native == request
    schema = D.tool_object_schema(tools)
    assert schema["properties"]["request"]["anyOf"][1]["properties"]["op"]["enum"] == ["runtime"]


@pytest.mark.parametrize("tool_request", ["{ op: 'resource' }", {"op": "resource", "relative": "x", "shell": "no"},
                                      {"op": "not_a_tool"}, {"op": "runtime", "provider": "native"}])
def test_general_object_bad_requests_preserve_session_without_dispatch(inert, tmp_path, tool_request):
    before = (inert.root / "session.json").read_bytes()
    adapter, _ = model([{"action": "step", "request": tool_request, "reason": "Attempt a bounded request."}])
    result = D.run(inert.root, tmp_path / "rejected-object", adapter=adapter,
                   decision_protocol=D.TOOL_OBJECT, allowed_tools=("resource",), max_decisions=1)
    assert result["status"] == "stopped_failure" and not inert.calls
    assert (inert.root / "session.json").read_bytes() == before
    assert result["model_calls"] == 1


def test_general_object_stop_and_pre_dispatch_correction_are_explicit(inert, tmp_path):
    adapter, seen = model([{"action": "step", "request": {"op": "resource", "relative": "x", "unknown": 1},
                            "reason": "First malformed request."},
                           {"action": "stop", "request": None, "reason": "Unsupported request; no tool ran."}])
    result = D.run(inert.root, tmp_path / "object-correction", adapter=adapter,
                   decision_protocol=D.TOOL_OBJECT, allowed_tools=("resource",), max_decisions=2,
                   max_decision_corrections=1)
    assert result["status"] == "model_stopped" and not inert.calls
    assert result["decision_corrections"]["used"] == 1 and len(seen) == 2


@pytest.mark.parametrize("enabled,declared,allowed", [(False, True, True), (True, False, True),
                                                      (True, True, False), (True, True, True)])
def test_v2_trace_tool_and_guide_require_both_mode_and_caller_admission(inert, tmp_path, enabled, declared, allowed):
    from anyuse.execution import rea_native_receiver_v2 as V2
    if enabled:
        inert.state["native_receiver_mode"] = "binary_runtime_hypothesis"
    if declared:
        inert.state["native_contract_mode"] = "declared"
    inert.save()
    trace = V2.discovery_guide(["correlate_native_trace_v2"])["correlate_native_trace_v2"]["example"]
    adapter, seen = model([static_decision(trace)])
    result = D.run(inert.root, tmp_path / "trace-admission", adapter=adapter,
                   allowed_tools=["correlate_native_trace_v2"] if allowed else ["evidence"],
                   decision_protocol=D.TOOL_OBJECT, max_decisions=1, until="step")
    view = json.loads(seen[0]["messages"][1]["content"])
    admitted = enabled and declared and allowed
    assert ("correlate_native_trace_v2" in view["tools"]) is admitted
    assert ("native_receiver_guide" in view) is admitted
    assert result["tool_dispatches"] == len(inert.calls) == int(admitted)
    assert result["status"] == ("boundary_reached" if admitted else "stopped_failure")
    static, _ = D.model_view(G.context(inert.root), inert.state, static_only=True)
    assert "correlate_native_trace_v2" not in static["tools"] and "native_receiver_guide" not in static
    with pytest.raises(ValueError, match="static-only scope"):
        D.model_view(G.context(inert.root), inert.state, static_only=True,
                     allowed_tools=["correlate_native_trace_v2"])


def test_v2_trace_bad_row_uses_existing_correction_without_dispatch(inert, tmp_path):
    from anyuse.execution import rea_native_receiver_v2 as V2
    inert.state.update(native_contract_mode="declared", native_receiver_mode="binary_runtime_hypothesis")
    inert.save()
    trace = V2.discovery_guide(["correlate_native_trace_v2"])["correlate_native_trace_v2"]["example"]
    bad = {**trace, "observed_getters": [7]}
    adapter, seen = model([static_decision(bad), static_decision(trace)])
    result = D.run(inert.root, tmp_path / "trace-correction", adapter=adapter,
                   allowed_tools=["correlate_native_trace_v2"], decision_protocol=D.TOOL_OBJECT,
                   max_decisions=2, max_decision_corrections=1, until="step")
    assert result["status"] == "boundary_reached", result.get("error")
    assert result["model_calls"] == len(seen) == 2
    assert result["decision_corrections"]["used"] == 1
    assert inert.calls == [trace] and result["tool_dispatches"] == 1


@pytest.mark.parametrize("admitted", ["resource", {"resource"}, ["not-a-tool"], ["resource", "resource"]])
def test_invalid_caller_admission_refuses_before_model_or_output(inert, tmp_path, admitted):
    adapter, seen = model([decision()])
    out = tmp_path / "driver"
    with pytest.raises(ValueError, match="allowed_tools"):
        D.run(inert.root, out, adapter=adapter, allowed_tools=admitted)
    assert not seen and not inert.calls and not out.exists()


def test_empty_admission_supports_honest_stop_only(inert, tmp_path):
    adapter, seen = model([decision(reason="No tools are admitted for this invocation.")])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, allowed_tools=[])
    assert result["status"] == "model_stopped" and result["admitted_tools"] == [] and not inert.calls
    assert json.loads(seen[0]["messages"][1]["content"])["tools"] == {}


@pytest.mark.parametrize("tool_request", [
    {"op": "analyze", "provider": "hopper"},
    {"op": "analyze", "provider": "ghidra"},
    {"op": "analyze", "javascript": "Contents/script.js"},
    {"op": "runtime", "request": {}},
    {"op": "observe"},
])
def test_static_preset_refuses_transport_overrides_or_live_tools_before_dispatch(inert, tmp_path, tool_request):
    adapter, seen = model([static_decision(tool_request)])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and not inert.calls and len(seen) == 1
    view = json.loads(seen[0]["messages"][1]["content"])
    assert set(view["tools"]) == set(D.STATIC_TOOLS)
    assert view["tools"]["analyze"]["optional"] == ["query", "function", "timeout"]
    assert result["static_only"] is True and result["tool_dispatches"] == 0


def test_static_preset_permits_native_analysis_with_default_ghidra_only(inert, tmp_path):
    adapter, seen = model([static_decision({"op": "analyze", "query": "pause"})])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True, until="step")
    assert result["status"] == "boundary_reached" and len(inert.calls) == 1
    assert inert.calls[0]["op"] == "analyze" and "provider" not in inert.calls[0]
    assert result["admitted_tools"] == list(D.STATIC_TOOLS)


def test_static_preset_cannot_be_widened_by_caller_tool_list(inert, tmp_path):
    adapter, seen = model([decision()])
    with pytest.raises(ValueError, match="static-only"):
        D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True, allowed_tools=["observe"])
    assert seen == [] and inert.calls == []


def test_static_phase_projects_evidence_objective_without_live_campaign_contract(inert, tmp_path, monkeypatch):
    original = G.context
    def context(root):
        return {**original(root), "capabilities": {"runtime": "LIVE_CAPABILITY_PRIVATE"},
                "workflow": "LIVE_WORKFLOW_PRIVATE", "checked_fresh_cases": ["FRESH_CASE_PRIVATE"],
                "checked_heldout_cases": ["HELDOUT_CASE_PRIVATE"], "proof_created": False}
    monkeypatch.setattr(G, "context", context)
    adapter, seen = model([static_decision({"op": "symbols", "query": "candidate"}),
                           static_decision(reason="Inspected symbol evidence; receiver and ABI remain unknown.")])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True)
    assert result["status"] == "model_stopped" and result["until"] == "static_evidence"
    assert result["requested_until"] == "hypothesis" and result["tool_dispatches"] == 1
    assert result["model_calls"] == len(seen) == 2
    for sent in seen:
        assert sent["messages"][0]["content"] == D.STATIC_SYSTEM
        view = json.loads(sent["messages"][1]["content"])
        assert view["agent_instructions"] == list(D.STATIC_INSTRUCTIONS)
        for key in ("capabilities", "workflow", "recorded_cases", "checked_fresh_cases",
                    "checked_heldout_cases", "proof_created", "case_counts"):
            assert key not in view
        assert view["driver_bounds"]["until"] == "static_evidence"
        assert "after_trial" not in view["driver_bounds"]
        assert all(spec["description"] for spec in view["tools"].values())
        assert "Live validation belongs to a later phase" in sent["messages"][0]["content"]
        assert all(value not in json.dumps(sent) for value in [
            "LIVE_CAPABILITY_PRIVATE", "LIVE_WORKFLOW_PRIVATE", "FRESH_CASE_PRIVATE", "HELDOUT_CASE_PRIVATE",
            "TRAINING_PRIVATE", "FRESH_ORACLE_PRIVATE", "HELDOUT_ORACLE_PRIVATE", "OLD_RECIPE_PRIVATE"])
    retained = json.loads((tmp_path / "static-driver/driver.json").read_text())
    assert retained["reason"] == "Inspected symbol evidence; receiver and ABI remain unknown."
    assert inert.state["status"] == "investigating" and "proof" not in inert.state


def test_static_phase_stop_remains_legal_without_any_tool_or_existing_hypothesis_goal(inert, tmp_path):
    inert.state["hypothesis"] = {"actor": "OLD_RECIPE_PRIVATE"}
    inert.save()
    adapter, seen = model([static_decision(reason="Nothing inspected; no static tool is admitted.")])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter,
                   static_only=True, allowed_tools=[])
    assert result["status"] == "model_stopped" and result["model_calls"] == 1 and not inert.calls
    assert result["reason"] == "Nothing inspected; no static tool is admitted."
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["tools"] == {} and "OLD_RECIPE_PRIVATE" not in json.dumps(seen)


def test_static_phase_refuses_mixed_history_and_preserves_authoritative_preimage(inert, tmp_path):
    inert.state["history"] = [
        {"step": 0, "status": "completed", "request": {"op": "runtime"}},
        {"step": 1, "status": "completed", "request": {"op": "symbols"}}]
    inert.save()
    before = (inert.root / "session.json").read_bytes()
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    view, _ = D.model_view(G.context(inert.root), inert.state, static_only=True)
    assert view["history"] == [{"step": 1, "status": "completed", "op": "symbols"}]
    assert (inert.root / "session.json").read_bytes() == before
    assert not (tmp_path / "static-driver/decision-000").exists()


def test_nonstatic_phase_keeps_original_system_and_campaign_fields(inert, tmp_path):
    adapter, seen = model([decision()])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    view = json.loads(seen[0]["messages"][1]["content"])
    assert seen[0]["messages"][0]["content"] == D.SYSTEM
    assert view["agent_instructions"] == G.INSTRUCTIONS
    assert view["case_counts"] == {"learning": 1, "verification": 1, "heldout": 1}
    assert view["driver_bounds"]["until"] == result["until"] == "hypothesis"
    assert "after_trial" in view["driver_bounds"]
    assert seen[0]["response_format"]["json_schema"]["schema"] == D.SCHEMA
    assert "request_json" in D.SCHEMA["properties"] and "request" not in D.SCHEMA["properties"]
    assert result["decision_protocol"] == "request_json_v1"


@pytest.mark.parametrize("admitted,sections", [
    (["discover"], {"discover"}), (["runtime"], {"runtime"}),
    (["propose"], {"propose"}), (["discover", "runtime", "propose"], {"discover", "runtime", "propose"}),
    (["resource"], set()), ([], set()),
])
def test_native_guide_model_projection_respects_admitted_tools_and_private_data(inert, admitted, sections):
    inert.state["observer_profile"] = {"format": G.H.PROFILE, "id": "public", "label": "Read content",
        "effect": "read_content", "params": [], "reference": {"fields": {"result": {"kind": "constant", "value": "ORACLE_PRIVATE"}}}}
    inert.state["observer_profile_sha256"] = G.R.digest(inert.state["observer_profile"])
    context = G.context(inert.root)
    context["observer_profile"] = G._public_observer(inert.state["observer_profile"])
    context["observer_profile_sha256"] = inert.state["observer_profile_sha256"]
    context["native_guide"] = {"secret": "UNTRUSTED_CONTEXT_GUIDE"}
    view, _ = D.model_view(context, inert.state, allowed_tools=admitted)
    guide = view.get("native_guide")
    if sections:
        assert guide == G.native_guide(admitted)
        assert set(guide) & {"discover", "runtime", "propose"} == sections
    else:
        assert guide is None
    sent = json.dumps(view)
    for secret in ["UNTRUSTED_CONTEXT_GUIDE", "OLD_RECIPE_PRIVATE", "TRAINING_PRIVATE",
                   "FRESH_ORACLE_PRIVATE", "HELDOUT_ORACLE_PRIVATE", "ORACLE_PRIVATE"]:
        assert secret not in sent
    static, _ = D.model_view(context, inert.state, static_only=True)
    assert "native_guide" not in static
    assert "ExampleRoot" not in json.dumps(static)


def test_full_native_guide_fits_default_prompt_bound_without_truncation(session, tmp_path):
    adapter, sent = model([decision(reason="Independent caller-approved observer declaration is required.")])
    result = D.run(session[0], tmp_path / "guide-driver", adapter=adapter)
    assert result["status"] == "model_stopped" and result["model_calls"] == 1
    messages = sent[0]["messages"]
    assert sum(len(row["content"]) for row in messages) <= 32000
    view = json.loads(messages[1]["content"])
    assert view["native_guide"] == G.native_guide()
    assert "caller-approved" in view["native_guide"]["propose"]["profile_rules"][0]


def test_static_native_object_dispatch_omits_only_optional_nulls_and_uses_valid_symbol_default(inert, tmp_path):
    wire = static_decision({"op": "symbols", "query": "candidate", "limit": None, "timeout": None})
    adapter, seen = model([wire])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True, until="step")
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == 1, result.get("error")
    assert inert.calls == [{"op": "symbols", "query": "candidate", "timeout": 30}]
    assert result["decision_protocol"] == "static_structured_request_v1"
    schema = seen[0]["response_format"]["json_schema"]["schema"]
    assert "request" in schema["properties"] and "request_json" not in schema["properties"]
    assert result["decisions"][0]["decision"]["request"]["limit"] is None
    effective = json.loads((tmp_path / "static-driver/decision-000/effective-request.json").read_text())
    assert "limit" not in effective and effective["query"] == wire["request"]["query"]
    redaction = json.loads((tmp_path / "static-driver/decision-000/redaction.json").read_text())
    assert redaction["decision_protocol"] == result["decision_protocol"]


def test_nonstatic_symbol_default_also_respects_actual_shared_tool_timeout_bound(inert, tmp_path):
    adapter, seen = model([decision({"op": "symbols", "query": "candidate"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, until="step")
    assert result["status"] == "boundary_reached" and inert.calls[0]["timeout"] == 30
    assert seen[0]["response_format"]["json_schema"]["schema"] == D.SCHEMA


def test_structured_static_resource_uses_real_G_and_no_runtime_or_hypothesis(session, tmp_path):
    root = session[0]
    adapter, seen = model([static_decision({"op": "resource", "relative": "Contents/facts.plist"})])
    result = D.run(root, tmp_path / "static-driver", adapter=adapter, static_only=True,
                   allowed_tools=("resource",), until="step")
    state = G.R.load_json(root / "session.json")
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == 1
    assert state["steps"][-1]["request"] == {"op": "resource", "relative": "Contents/facts.plist"}
    assert state["status"] == "investigating" and state["proof"] is None and state.get("hypothesis") is None


def test_static_schema_is_closed_to_admitted_subset_with_nullable_optional_scalars():
    schema = D.static_schema(("symbols", "evidence"))
    assert schema["additionalProperties"] is False
    branches = schema["properties"]["request"]["anyOf"]
    assert branches[0] == {"type": "null"}
    assert [b["properties"]["op"]["enum"] for b in branches[1:]] == [["symbols"], ["evidence"]]
    for branch in branches[1:]:
        assert branch["additionalProperties"] is False
        assert set(branch["required"]) == set(branch["properties"])
        assert not {"provider", "javascript", "runtime", "script"} & set(branch["properties"])
    from anyuse.execution import rea_symbols as S
    symbols, evidence = branches[1:]
    assert symbols["properties"]["limit"]["anyOf"][0]["maximum"] == S.MAX_ROWS
    assert symbols["properties"]["query"]["anyOf"][0]["maxLength"] == S.MAX_QUERY_BYTES
    assert symbols["properties"]["timeout"]["anyOf"][0]["maximum"] == 30
    assert evidence["properties"]["limit"]["anyOf"][0]["maximum"] == 16384
    assert evidence["properties"]["artifact"]["type"] == "string"
    empty = D.static_schema(())
    assert empty["properties"]["action"]["enum"] == ["stop"]
    assert empty["properties"]["request"] == {"type": "null"}


@pytest.mark.parametrize("tool_request", [
    {"op": "symbols", "query": 7},
    {"op": "symbols", "query": "é" * 129},
    {"op": "symbols", "limit": True},
    {"op": "symbols", "limit": 0},
    {"op": "symbols", "limit": 129},
    {"op": "symbols", "timeout": 31},
    {"op": "symbols", "query": "candidate\ncommand"},
    {"op": "analyze", "timeout": 1.5},
    {"op": "analyze", "query": "é" * 2050},
    {"op": "analyze", "provider": None},
    {"op": "analyze", "javascript": None},
    {"op": "resource", "relative": None},
    {"op": "resource", "relative": ["Contents/facts.plist"]},
    {"op": "evidence", "artifact": 3},
    {"op": "evidence", "artifact": "inventory.json", "offset": -1},
    {"op": "evidence", "artifact": "inventory.json", "limit": 16385},
])
def test_static_wrong_scalar_types_bounds_and_even_null_overrides_refuse_before_dispatch(inert, tmp_path, tool_request):
    adapter, seen = model([static_decision(tool_request)])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0
    assert len(seen) == 1 and not inert.calls


@pytest.mark.parametrize("reply", [
    static_decision("{'op': 'symbols', 'query': 'candidate'}"),
    static_decision('{"op":"symbols","query":"candidate","limit":null,"timeout":null}'),
    decision({"op": "symbols", "query": "candidate"}),
    {"action": "step", "request": None, "reason": "Try a null step"},
    {"action": "stop", "request": "", "reason": "Try the old stop string"},
    {"action": "stop", "request": {"op": "resource", "relative": "Contents/facts.plist"}, "reason": "Stop"},
    {**static_decision(), "unexpected": None},
    '{"action":"step","request":{"op":"symbols","op":"resource","query":null,"limit":null,"timeout":null},"reason":"x"}',
    '{"action":"step","request":{"op":"symbols","query":null,"limit":NaN,"timeout":null},"reason":"x"}',
])
def test_static_string_repr_legacy_protocol_duplicates_or_nonfinite_objects_are_never_repaired(inert, tmp_path, reply):
    adapter, seen = model([reply])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0
    assert len(seen) == 1 and not inert.calls


def test_static_empty_tools_permit_null_stop_and_refuse_even_known_static_operation(inert, tmp_path):
    adapter, seen = model([static_decision({"op": "symbols"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True, allowed_tools=[])
    assert result["status"] == "stopped_failure" and not inert.calls
    assert seen[0]["response_format"]["json_schema"]["schema"]["properties"]["action"]["enum"] == ["stop"]


def test_native_static_protocol_does_not_replace_existing_nonstatic_request_json(inert, tmp_path):
    adapter, seen = model([static_decision({"op": "symbols"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter)
    assert result["status"] == "stopped_failure" and not inert.calls
    assert seen[0]["response_format"]["json_schema"]["schema"] == D.SCHEMA


def test_static_ready_symbol_file_is_recorded_without_an_extra_model_or_tool_probe(inert, tmp_path, symbol_readiness):
    adapter, seen = model([static_decision({"op": "symbols", "query": "candidate"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True, until="step")
    assert result["status"] == "boundary_reached" and result["model_calls"] == len(seen) == 1
    assert len(inert.calls) == 1 and symbol_readiness == [None]
    assert result["requested_tools"] == result["admitted_tools"] == list(D.STATIC_TOOLS)
    assert result["tool_readiness"]["symbols"]["tool_invocations"] == 0
    assert result["omitted_tools"] == []
    assert any(p["file"] == str(Path(D.S.__file__).resolve()) for p in result["sources"])
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["tool_availability"]["tool_readiness"] == result["tool_readiness"]
    redaction = json.loads((tmp_path / "driver/decision-000/redaction.json").read_text())
    assert redaction["tool_availability"] == view["tool_availability"]


def test_missing_real_nm_hides_only_symbols_and_records_why_without_probes(inert, tmp_path, monkeypatch):
    import subprocess
    calls = []
    unavailable = {"status": "unavailable", "tool": None, "why": "real CLT nm not installed",
                   "error": "missing_file", "tool_invocations": 0}
    def inspect(nm=None):
        calls.append(nm)
        return copy.deepcopy(unavailable)
    def no_probe(*args, **kwargs):
        raise AssertionError("readiness must not spawn a tool, installer or health probe")
    monkeypatch.setattr(D.S, "tool_readiness", inspect)
    monkeypatch.setattr(subprocess, "run", no_probe)
    monkeypatch.setattr(subprocess, "Popen", no_probe)
    adapter, seen = model([static_decision(reason="Symbol tool unavailable; other static evidence remains admitted.")])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True)
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 0
    assert result["model_calls"] == len(seen) == 1 and calls == [None] and not inert.calls
    assert result["requested_tools"] == list(D.STATIC_TOOLS)
    assert result["admitted_tools"] == ["analyze", "evidence", "resource"]
    assert result["tool_readiness"]["symbols"] == unavailable
    assert result["omitted_tools"] == [{"op": "symbols", "status": "unavailable", "why": unavailable["why"]}]
    view = json.loads(seen[0]["messages"][1]["content"])
    assert set(view["tools"]) == {"analyze", "evidence", "resource"}
    branches = seen[0]["response_format"]["json_schema"]["schema"]["properties"]["request"]["anyOf"]
    assert [b["properties"]["op"]["enum"] for b in branches[1:]] == [["analyze"], ["evidence"], ["resource"]]


def test_unavailable_explicit_symbols_only_subset_becomes_stop_only_without_adding_tools(inert, tmp_path, monkeypatch):
    monkeypatch.setattr(D.S, "tool_readiness", lambda: {"status": "unavailable", "tool": None,
        "why": "operator-selected nm missing", "error": None, "tool_invocations": 0})
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True, allowed_tools=("symbols",))
    assert result["status"] == "model_stopped" and not inert.calls
    assert result["requested_tools"] == ["symbols"] and result["admitted_tools"] == []
    schema = seen[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["action"]["enum"] == ["stop"]
    assert schema["properties"]["request"] == {"type": "null"}


def test_model_cannot_dispatch_a_symbol_tool_removed_by_pure_file_readiness(inert, tmp_path, monkeypatch):
    monkeypatch.setattr(D.S, "tool_readiness", lambda: {"status": "unavailable", "tool": None,
        "why": "missing real nm", "error": None, "tool_invocations": 0})
    adapter, seen = model([static_decision({"op": "symbols", "query": "candidate"})])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0
    assert not inert.calls and result["model_calls"] == len(seen) == 1


def test_readiness_is_not_queried_for_nonstatic_or_unrequested_symbols_and_projection_stays_pure(inert, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure projection/unrequested/nonstatic tools must not inspect developer-tool files")
    monkeypatch.setattr(D.S, "tool_readiness", forbidden)
    context = G.context(inert.root)
    view, manifest = D.model_view(context, inert.state, static_only=True)
    assert set(view["tools"]) == set(D.STATIC_TOOLS) and "tool_availability" not in manifest
    adapter, seen = model([decision()])
    result = D.run(inert.root, tmp_path / "nonstatic-driver", adapter=adapter)
    assert result["status"] == "model_stopped" and result["tool_readiness"] == {}
    assert "tool_availability" not in json.loads(seen[0]["messages"][1]["content"])
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "static-driver", adapter=adapter, static_only=True, allowed_tools=("resource",))
    assert result["status"] == "model_stopped" and result["admitted_tools"] == ["resource"]
    assert result["tool_readiness"] == {} and result["omitted_tools"] == []


@pytest.mark.parametrize("static_only", [False, True])
def test_model_sees_exact_wire_schema_and_valid_inventory_example(inert, tmp_path, static_only):
    inert.state["artifacts"]["inventory.json"] = {"sha256": "1" * 64, "bytes": 123}
    inert.save()
    reply = static_decision() if static_only else decision()
    adapter, seen = model([reply])
    result = D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=static_only)
    assert result["status"] == "model_stopped" and len(seen) == 1 and not inert.calls
    view = json.loads(seen[0]["messages"][1]["content"])
    protocol = view["decision_protocol"]
    assert protocol["schema"] == seen[0]["response_format"]["json_schema"]["schema"]
    assert D._request(protocol["example"], view) == {
        "op": "evidence", "artifact": "inventory.json", "offset": 0, "limit": 4096}
    # This syntax illustration is never dispatched by the driver itself.
    assert result["tool_dispatches"] == 0


@pytest.mark.parametrize("with_inventory", [False, True])
def test_protocol_example_never_invents_artifact_or_read_capability(inert, tmp_path, with_inventory):
    if with_inventory:
        inert.state["artifacts"]["inventory.json"] = {"sha256": "1" * 64, "bytes": 123}
        inert.save()
    adapter, seen = model([static_decision()])
    allowed = ("resource",) if with_inventory else ("evidence",)
    D.run(inert.root, tmp_path / "driver", adapter=adapter, static_only=True, allowed_tools=allowed)
    protocol = json.loads(seen[0]["messages"][1]["content"])["decision_protocol"]
    assert "example" not in protocol and not inert.calls


def test_driver_requires_frozen_approval_for_propose_but_keeps_investigation(inert, tmp_path):
    adapter, seen = model([decision({"op": "propose", "rationale": "Guess", "citations": [], "actor": {}})])
    result = D.run(inert.root, tmp_path / "unapproved", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0 and not inert.calls
    view = json.loads(seen[0]["messages"][1]["content"])
    assert "propose" not in view["tools"] and "runtime" in view["tools"]
    assert view["observer_contract"]["status"] == result["observer_contract_status"] == "needs_observer_contract"
    assert result["omitted_tools"] == [{"tool": "propose", "why": "needs_observer_contract"}]


def test_driver_hash_only_proposal_uses_private_approval_and_static_never_sees_it(session, tmp_path):
    request = proposal(session)
    request.pop("profile")
    request["observer_profile_sha256"] = G.context(session[0])["observer_profile_sha256"]
    adapter, seen = model([decision(request)])
    result = D.run(session[0], tmp_path / "approved", adapter=adapter)
    assert result["status"] == "boundary_reached" and result["tool_dispatches"] == 1, result.get("error")
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["observer_profile_sha256"] == request["observer_profile_sha256"]
    assert view["observer_profile"]["proof_created"] is False
    assert "param" not in view["observer_profile"]["declaration"]["reference"]["fields"]["body"]
    assert "observer_profile_sha256" in view["tools"]["propose"]["optional"]
    state = G.R.load_json(session[0] / "session.json")
    static, manifest = D.model_view(G.context(session[0]), state, static_only=True)
    assert "observer_profile" not in static and "observer_profile_sha256" not in static and "observer_contract" not in static
    assert "observer_contract" not in manifest


@pytest.mark.parametrize("provider", sorted(M.CLI_PROVIDERS))
def test_driver_refuses_filesystem_capable_cli_provider_before_call(inert, tmp_path, provider):
    adapter, seen = model([decision({"op": "observe"})])
    adapter.config = replace(adapter.config, provider=provider)
    result = D.run(inert.root, tmp_path / "cli-refusal", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen and not inert.calls
    assert "tool-free HTTP" in result["error"]["message"]


@pytest.mark.parametrize("attribute,value", [("spend_booking_error", "ledger unavailable"), ("finish_reason", "length")])
def test_driver_retains_but_never_dispatches_unbooked_or_cutoff_answer(inert, tmp_path, monkeypatch, attribute, value):
    adapter, seen = model([decision({"op": "observe"})])
    complete = adapter.complete
    def changed(*args, **kwargs):
        answer = complete(*args, **kwargs)
        setattr(answer, attribute, value)
        return answer
    monkeypatch.setattr(adapter, "complete", changed)
    result = D.run(inert.root, tmp_path / "answer-refusal", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 1 and len(seen) == 1 and not inert.calls
    raw = json.loads((tmp_path / "answer-refusal/decision-000/model-output.json").read_text())
    assert raw[attribute] == value


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_driver_closes_only_its_own_adapter_even_after_failure(inert, tmp_path, monkeypatch, owned, failure):
    adapter, _ = model([503 if failure else decision()])
    closed = []
    monkeypatch.setattr(adapter, "close", lambda: closed.append(True))
    monkeypatch.setattr(M.ModelAdapter, "for_role", lambda *a, **k: adapter)
    result = D.run(inert.root, tmp_path / "owned-adapter", **({} if owned else {"adapter": adapter}))
    assert closed == ([True] if owned else [])
    assert result["adapter_cleanup"] == {"owned": owned, "attempted": owned, "confirmed": True if owned else None}


def test_prior_investigation_retains_A_reason_and_B_as_last_without_private_context(inert, tmp_path):
    inert.payloads.update(A={"facts": "A observed"}, B={"facts": "B observed"})
    adapter, seen = model([static_decision({"op": "resource", "relative": "A"}, reason="Model hypothesis A"),
                           static_decision({"op": "resource", "relative": "B"}, reason="Model hypothesis B"), static_decision()])
    result = D.run(inert.root, tmp_path / "window", adapter=adapter, static_only=True)
    assert result["status"] == "model_stopped" and result["model_calls"] == 3 and len(inert.calls) == 2
    third = json.loads(seen[2]["messages"][1]["content"])
    assert third["last_observation"] == {"facts": "B observed"}
    assert len(third["prior_investigations"]) == 1
    earlier = third["prior_investigations"][0]
    assert earlier["observation"] == {"facts": "A observed"} and earlier["model_reason"] == "Model hypothesis A"
    assert earlier["reason_source"] == "model_authored_untrusted_not_fact_or_proof"
    assert earlier["decision_index"] == earlier["g_step_index"] == 0
    assert earlier["evidence"]["artifact"] == "s0000/feedback.json" and earlier["evidence"]["pointer"] == "/feedback"
    assert earlier["evidence"]["sha256"] == inert.state["artifacts"]["s0000/feedback.json"]["sha256"]
    sent = json.dumps(seen)
    assert all(secret not in sent for secret in ["TRAINING_PRIVATE", "FRESH_ORACLE_PRIVATE", "HELDOUT_ORACLE_PRIVATE", "OLD_RECIPE_PRIVATE", "REPO_PRIVATE"])


def test_prior_window_evicts_after_four_and_never_imports_previous_driver_history(inert, tmp_path):
    replies = [static_decision({"op": "resource", "relative": str(i)}, reason=f"Reason {i}") for i in range(5)]
    adapter, seen = model(replies + [static_decision()])
    result = D.run(inert.root, tmp_path / "eviction", adapter=adapter, static_only=True)
    assert result["model_calls"] == 6 and len(inert.calls) == 5
    last = json.loads(seen[-1]["messages"][1]["content"])
    assert [p["decision_index"] for p in last["prior_investigations"]] == [1, 2, 3]
    assert last["last_observation"] == inert.payloads.get("4", {"status": "investigating"})
    new, second = model([static_decision()])
    result = D.run(inert.root, tmp_path / "new-invocation", adapter=new, static_only=True)
    assert result["model_calls"] == 1
    view = json.loads(second[0]["messages"][1]["content"])
    assert view["prior_investigations"] == [] and view["last_observation"] is None


def test_oversized_prior_payload_and_reason_are_omitted_whole_with_pinned_reread(inert, tmp_path):
    inert.payloads["A"] = {"large": "LARGE_PRIVATE_PAYLOAD" * 400}
    adapter, seen = model([static_decision({"op": "resource", "relative": "A"}, reason="OMITTED_MODEL_REASON"),
        static_decision({"op": "resource", "relative": "B"}), static_decision()])
    result = D.run(inert.root, tmp_path / "omission", adapter=adapter, static_only=True, max_context_chars=40000)
    assert result["model_calls"] == 3
    view = json.loads(seen[2]["messages"][1]["content"]);row=view["prior_investigations"][0]
    assert "observation" not in row and "model_reason" not in row
    assert row["payload_omitted"]["reread"]["artifact"] == "s0000/feedback.json"
    assert row["payload_omitted"]["reread"]["sha256"] == row["evidence"]["sha256"]
    assert "LARGE_PRIVATE_PAYLOAD" not in seen[2]["messages"][1]["content"] and "OMITTED_MODEL_REASON" not in seen[2]["messages"][1]["content"]
    manifest=json.loads((tmp_path / "omission/decision-002/redaction.json").read_text())["investigation_window"]
    assert manifest["prior_payload_chars"] == 0 and len(manifest["whole_payload_omissions"]) == 1 and not manifest["truncation_or_summary"]


def test_changed_registered_feedback_stops_before_next_dispatch(inert, tmp_path):
    adapter, seen = model([static_decision({"op": "resource", "relative": "A"}),
                           static_decision({"op": "resource", "relative": "B"})])
    complete = adapter.complete
    def tamper(*args, **kwargs):
        answer = complete(*args, **kwargs)
        if len(seen) == 2:
            (inert.root / "s0000/feedback.json").write_text("changed retained feedback")
        return answer
    adapter.complete = tamper
    result=D.run(inert.root, tmp_path / "drift", adapter=adapter, static_only=True)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 2 and len(inert.calls) == 1
    assert "feedback bytes changed" in result["error"]["message"]


def test_prior_evidence_reads_retain_exact_admitted_read_bounds(inert, tmp_path):
    inert.state["artifacts"]["inventory.json"]={"file": "inert inventory", "sha256": "1" * 64, "bytes": 1}
    inert.save()
    adapter, seen=model([static_decision({"op": "evidence", "artifact": "inventory.json", "offset": 10, "limit": 20}),
        static_decision({"op": "resource", "relative": "B"}), static_decision()])
    result=D.run(inert.root, tmp_path / "read-bounds", adapter=adapter, static_only=True)
    assert result["model_calls"] == 3
    view=json.loads(seen[2]["messages"][1]["content"])
    assert view["prior_investigations"][0]["evidence_read"] == {"artifact": "inventory.json", "offset": 10, "limit": 20}


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_malformed_nonfinite_response_retains_exact_raw_text_before_refusal(inert, tmp_path, constant):
    raw='{"action":"stop","request_json":"","reason":"Stop","invalid":'+constant+'}'
    adapter, seen=model([raw])
    result=D.run(inert.root, tmp_path / "nonfinite", adapter=adapter)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 1 and not inert.calls
    output=json.loads((tmp_path / "nonfinite/decision-000/model-output.json").read_text())
    assert output["text"] == raw and output["parsed"] is None and output["parsed_retained_in_raw_text"] is True


def test_newest_investigation_remains_visible_after_lifecycle_summary(inert, tmp_path):
    inert.state["observer_profile"]={"format": G.H.PROFILE, "id": "declared", "label": "Read",
        "effect": "read_content", "params": [], "reference": {"fields": {"content": {"kind": "file_text", "param": "reference"}}}}
    inert.state["observer_profile_sha256"]=G.R.digest(inert.state["observer_profile"]);inert.save()
    inert.payloads["A"]={"fact":"Retain across propose"}
    request={"op":"propose","rationale":"A hypothesis only","citations":[],"actor":{},
             "observer_profile_sha256":inert.state["observer_profile_sha256"]}
    adapter, seen=model([decision({"op":"resource","relative":"A"}, reason="Untrusted model reason"), decision(request), decision()])
    result=D.run(inert.root, tmp_path / "lifecycle", adapter=adapter, until="published")
    assert result["status"] == "model_stopped" and result["model_calls"] == 3 and len(inert.calls) == 2
    view=json.loads(seen[2]["messages"][1]["content"])
    assert view["last_observation"] == {"op":"propose","status":"hypothesis"}
    assert view["prior_investigations"][0]["observation"] == inert.payloads["A"]
    manifest=json.loads((tmp_path / "lifecycle/decision-002/redaction.json").read_text())
    assert manifest["investigation_window"]["latest_investigation_in_last_observation"] is False


@pytest.mark.parametrize("initial_failure", [False, True, "interrupt"])
def test_owned_cleanup_failure_does_not_erase_primary_failure_or_interrupt(inert, tmp_path, monkeypatch, initial_failure):
    adapter, _=model([KeyboardInterrupt() if initial_failure == "interrupt" else (503 if initial_failure else decision())])
    monkeypatch.setattr(M.ModelAdapter,"for_role",lambda *a, **k:adapter)
    def close():
        raise RuntimeError("cleanup failure")
    monkeypatch.setattr(adapter,"close",close)
    out=tmp_path / "cleanup-failure"
    if initial_failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):D.run(inert.root,out)
        result=json.loads((out / "driver.json").read_text())
        assert result["status"] == "interrupted" and result["error"]["type"] == "KeyboardInterrupt"
    else:
        result=D.run(inert.root,out)
        assert result["status"] == "stopped_failure"
        assert ("model failure" if initial_failure else "cleanup failed") in result["error"]["message"]
    assert result["adapter_cleanup"]["confirmed"] is False
    assert result["adapter_cleanup"]["error"] == "RuntimeError"


def test_prior_window_has_one_total_payload_budget_and_keeps_global_context_refusal(inert, tmp_path):
    for name in "ABCD":inert.payloads[name]={"fact":name*2400}
    replies=[static_decision({"op":"resource","relative":name}) for name in "ABCD"]+[static_decision()]
    adapter,seen=model(replies)
    result=D.run(inert.root,tmp_path / "aggregate",adapter=adapter,static_only=True,max_context_chars=40000)
    assert result["model_calls"] == 5
    view=json.loads(seen[-1]["messages"][1]["content"])
    rows=view["prior_investigations"]
    actual=sum(len(D._json({"model_reason":r["model_reason"],"observation":r["observation"]})) for r in rows if "observation" in r)
    assert actual <= 6000 and len(rows)==3 and sum("payload_omitted" in r for r in rows)==1
    # The existing whole-context limit still refuses rather than dropping memory or truncating a response.
    short,sent=model([static_decision()])
    result=D.run(inert.root,tmp_path / "global-bound",adapter=short,static_only=True,max_context_chars=4096)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not sent


@pytest.mark.parametrize("tampered_step", [None, 0, 1, 2])
def test_prior_window_keeps_newer_getter_beside_latest_method_with_same_budget(inert, tmp_path, tampered_step):
    def sized_observation(label, chars):
        value = {"label": label, "text": ""}
        value["text"] = "x" * (chars - len(D._json(value)))
        assert len(D._json(value)) == chars
        return value

    observations = {
        "metadata": sized_observation("OLDER_METADATA_BODY", 3225),
        "methods": sized_observation("OVERSIZED_METHOD_BODY", 7714),
        "getter": sized_observation("NEWER_GETTER_BODY", 3941),
        "latest-methods": sized_observation("LATEST_METHOD_BODY", 7714),
    }
    reasons = ["Older metadata reason", "Oversized method reason", "Newer getter reason", "Latest method reason"]
    inert.payloads.update(observations)
    requests = [{"op": "resource", "relative": name} for name in observations]
    replies = [static_decision(request, reason=reason) for request, reason in zip(requests, reasons)]
    replies.append(static_decision(None if tampered_step is None else
                                  {"op": "resource", "relative": "must-not-dispatch"}))
    calls = []
    def tamper_after_model_input():
        calls.append(True)
        if len(calls) == 5 and tampered_step is not None:
            (inert.root / f"s{tampered_step:04d}/feedback.json").write_text("changed registered evidence")

    adapter, seen = model(replies, hook=tamper_after_model_input)
    out = tmp_path / "newer-payload-priority"
    result = D.run(inert.root, out, adapter=adapter, static_only=True,
                   allowed_tools=("resource",), max_context_chars=40000)
    assert result["model_calls"] == len(seen) == 5 and inert.calls == requests
    view = json.loads(seen[-1]["messages"][1]["content"])
    assert view["last_observation"] == observations["latest-methods"]
    rows = view["prior_investigations"]
    assert [row["decision_index"] for row in rows] == [0, 1, 2]
    assert ["observation" in row for row in rows] == [False, False, True]
    assert rows[2]["observation"] == observations["getter"] and rows[2]["model_reason"] == reasons[2]
    for row in rows[:2]:
        assert "model_reason" not in row
        pin = inert.state["artifacts"][row["evidence"]["artifact"]]
        assert row["payload_omitted"]["reread"] == {
            "artifact": row["evidence"]["artifact"], "sha256": pin["sha256"],
            "bytes": pin["bytes"], "pointer": "/feedback"}
    assert all(label not in json.dumps(rows) for label in ("OLDER_METADATA_BODY", "OVERSIZED_METHOD_BODY"))
    window = json.loads((out / "decision-004/redaction.json").read_text())["investigation_window"]
    retained_size = len(D._json({"model_reason": reasons[2], "observation": observations["getter"]}))
    assert window["prior_payload_chars"] == retained_size <= window["maximum_prior_payload_chars"] == 6000
    assert window["prior_turns_exposed"] == 3 and not window["truncation_or_summary"]
    assert [row["decision_index"] for row in window["whole_payload_omissions"]] == [0, 1]
    if tampered_step is None:
        assert result["status"] == "model_stopped" and inert.state["status"] == "investigating"
    else:
        assert result["status"] == "stopped_failure" and "feedback bytes changed" in result["error"]["message"]


def _checkpoint(inert, tmp_path, *, full=False, count=1):
    replies = [(decision if full else static_decision)({"op": "resource", "relative": f"public-{i}"},
               reason=f"Retained model hypothesis {i}") for i in range(count)]
    adapter, seen = model(replies)
    out = tmp_path / "producer"
    result = D.run(inert.root, out, adapter=adapter, static_only=not full,
                   max_decisions=count, max_context_chars=60000)
    assert result["status"] == "decision_budget_exhausted" and result["tool_dispatches"] == count
    assert result["memory_checkpoint"]["file"] == str(out / "memory.json")
    return out, seen


def test_explicit_resume_restores_verified_evidence_without_repeating_tools(inert, tmp_path):
    inert.payloads["public-0"] = {"fact": "A real retained observation"}
    out, _ = _checkpoint(inert, tmp_path)
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["status"] == "model_stopped" and result["model_calls"] == 1
    assert len(inert.calls) == 1 and result["tool_dispatches"] == 0
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["last_observation"] is None
    assert view["prior_investigations"][0]["observation"] == inert.payloads["public-0"]
    assert view["prior_investigations"][0]["model_reason"] == "Retained model hypothesis 0"
    assert result["investigation_memory"]["restored_turns"] == 1
    assert all(secret not in json.dumps(seen) for secret in
               ("TRAINING_PRIVATE", "FRESH_ORACLE_PRIVATE", "HELDOUT_ORACLE_PRIVATE", "OLD_RECIPE_PRIVATE"))


def test_cross_run_step_identity_does_not_collide_with_new_decision_zero(inert, tmp_path):
    out, _ = _checkpoint(inert, tmp_path)
    inert.payloads["B"] = {"fact": "B"}
    adapter, seen = model([static_decision({"op": "resource", "relative": "B"}), static_decision()])
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["status"] == "model_stopped"
    view = json.loads(seen[1]["messages"][1]["content"])
    assert view["last_observation"] == {"fact": "B"}
    assert [turn["g_step_index"] for turn in view["prior_investigations"]] == [0]
    assert view["prior_investigations"][0]["decision_index"] == 0


def test_checkpoint_retains_more_history_than_bounded_prompt_window(inert, tmp_path):
    out, _ = _checkpoint(inert, tmp_path, count=7)
    saved = json.loads((out / "memory.json").read_text())
    assert len(saved["turns"]) == 7 and all("observation" not in turn for turn in saved["turns"])
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["investigation_memory"]["restored_turns"] == 7
    view = json.loads(seen[0]["messages"][1]["content"])
    assert [turn["g_step_index"] for turn in view["prior_investigations"]] == [4, 5, 6]


@pytest.mark.parametrize("change", ["plan", "task", "target", "history"])
def test_memory_refuses_changed_frozen_task_or_rewritten_history_before_model(inert, tmp_path, change):
    out, _ = _checkpoint(inert, tmp_path)
    if change == "history":
        inert.state["history"][0]["feedback"] = {"changed": True}
    else:
        inert.state[change] = {"changed": True}
    inert.save()
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert len(inert.calls) == 1


@pytest.mark.parametrize("role", ["model-input.json", "model-output.json", "effective-request.json", "feedback"])
def test_memory_reopens_each_original_producer_and_feedback_pin(inert, tmp_path, role):
    out, _ = _checkpoint(inert, tmp_path)
    file = inert.root / "s0000/feedback.json" if role == "feedback" else out / "decision-000" / role
    file.write_text("changed")
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert len(inert.calls) == 1


def test_memory_cannot_invent_model_reason_or_import_trial_entries(inert, tmp_path):
    out, _ = _checkpoint(inert, tmp_path)
    original = (out / "memory.json").read_text()
    for name, value in (("model_reason", "Fabricated hypothesis"), ("op", "heldout")):
        saved = json.loads(original)
        saved["turns"][0][name] = value
        (out / "memory.json").write_text(json.dumps(saved))
        adapter, seen = model([static_decision()])
        result = D.run(inert.root, tmp_path / ("refused-" + name), adapter=adapter, static_only=True, resume_from=out)
        assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen


def test_static_resume_filters_native_feedback_and_reason_before_model(inert, tmp_path):
    inert.payloads["runtime"] = {"native": "RUNTIME_PRIVATE_SENTINEL"}
    adapter, _ = model([decision({"op": "runtime", "request": {"operations": [{"op": "modules"}] }},
                                 reason="RUNTIME_REASON_PRIVATE"),
                        decision({"op": "resource", "relative": "public"}, reason="Derived from RUNTIME_PRIVATE_SENTINEL")])
    out = tmp_path / "producer"
    result = D.run(inert.root, out, adapter=adapter, max_decisions=2, max_context_chars=60000)
    assert result["tool_dispatches"] == 2
    reader, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "static-consumer", adapter=reader, static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert "RUNTIME_PRIVATE_SENTINEL" not in json.dumps(seen) and "RUNTIME_REASON_PRIVATE" not in json.dumps(seen)
    restored, receipt = D._load_memory(out, inert.root, inert.state, D.STATIC_TOOLS, True)
    assert restored == [] and receipt["scope_filtered_steps"] == [0, 1]


def test_memory_and_producer_drift_during_model_refuses_dispatch(inert, tmp_path):
    out, _ = _checkpoint(inert, tmp_path)
    def mutate():
        (out / "decision-000/model-output.json").write_text("changed")
    adapter, seen = model([static_decision({"op": "resource", "relative": "B"})], hook=mutate)
    result = D.run(inert.root, tmp_path / "consumer", adapter=adapter, static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 1 and len(seen) == 1
    assert result["tool_dispatches"] == 0 and len(inert.calls) == 1


def test_new_full_native_protocol_uses_native_objects_and_checkpoint_provenance(inert, tmp_path):
    replies = [{"action": "step", "request": {"op": "resource", "relative": "public"}, "reason": "Inspect metadata"},
               {"action": "stop", "request": None, "reason": "More evidence is required"}]
    adapter, seen = model(replies)
    out = tmp_path / "native-objects"
    result = D.run(inert.root, out, adapter=adapter, decision_protocol="full_native_v1", max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 1
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["decision_protocol"]["format"] == result["decision_protocol"] == "full_native_v1"
    assert "request_json" not in view["decision_protocol"]["schema"]["properties"]
    assert "record" not in view["tools"] and "invoke" not in view["tools"]
    assert any(pin["file"].endswith("rea_agent_protocol.py") for pin in result["sources"])
    reader, restored = model([replies[-1]])
    result = D.run(inert.root, tmp_path / "native-resume", adapter=reader, decision_protocol="full_native_v1",
                   resume_from=out, max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["investigation_memory"]["restored_turns"] == 1
    assert json.loads(restored[0]["messages"][1]["content"])["prior_investigations"][0]["op"] == "resource"


def test_full_native_protocol_never_repairs_nested_python_or_json_strings(inert, tmp_path):
    adapter, seen = model([{"action": "step", "request": "{'op':'resource','relative':'public'}", "reason": "Inspect"}])
    result = D.run(inert.root, tmp_path / "native-malformed", adapter=adapter,
                   decision_protocol="full_native_v1", max_context_chars=100000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 1 and not inert.calls


def test_memory_refuses_changed_policy_source_before_any_model(inert, tmp_path, monkeypatch):
    out, _ = _checkpoint(inert, tmp_path)
    original = D._pin
    def changed(file):
        value = original(file)
        if file == Path(D.P.__file__).resolve():
            value["sha256"] = "f" * 64
        return value
    monkeypatch.setattr(D, "_pin", changed)
    adapter, seen = model([static_decision()])
    result = D.run(inert.root, tmp_path / "changed-source", adapter=adapter,
                   static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert len(inert.calls) == 1


def test_evidence_read_lineage_cannot_carry_native_data_into_static_resume(inert, tmp_path):
    inert.payloads["runtime"] = {"native": "NATIVE_FACT_PRIVATE"}
    inert.payloads["evidence"] = {"native_copy": "NATIVE_FACT_PRIVATE"}
    replies = [decision({"op": "runtime", "request": {"operations": []}}, reason="NATIVE_REASON_PRIVATE"),
               decision({"op": "evidence", "artifact": "s0000/feedback.json"}, reason="NATIVE_READ_REASON_PRIVATE"),
               decision({"op": "evidence", "artifact": "s0001/feedback.json"}, reason="NATIVE_CHAIN_REASON_PRIVATE")]
    producer, _ = model(replies)
    out = tmp_path / "native-evidence-producer"
    result = D.run(inert.root, out, adapter=producer, max_decisions=3, max_context_chars=100000)
    assert result["status"] == "decision_budget_exhausted" and result["tool_dispatches"] == 3
    reader, sent = model([static_decision()])
    result = D.run(inert.root, tmp_path / "static-lineage", adapter=reader, static_only=True, resume_from=out)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not sent
    restored, receipt = D._load_memory(out, inert.root, inert.state, D.STATIC_TOOLS, True)
    assert restored == [] and receipt["scope_filtered_steps"] == [0, 1, 2]
    assert all(value not in json.dumps(sent) for value in ["NATIVE_FACT_PRIVATE", "NATIVE_REASON_PRIVATE",
                                                         "NATIVE_READ_REASON_PRIVATE", "NATIVE_CHAIN_REASON_PRIVATE"])
    view, _ = D.model_view(G.context(inert.root), inert.state, static_only=True)
    assert not view["artifacts"]
    with pytest.raises(ValueError, match="outside visible"):
        D._request(static_decision({"op": "evidence", "artifact": "s0002/feedback.json"}), view)


@pytest.mark.parametrize("static_only,tools", [(True, None), (False, ("symbols", "evidence", "resource"))])
def test_narrow_scope_refuses_native_derived_static_artifacts_before_model(inert, tmp_path, static_only, tools):
    producer, _ = model([decision({"op": "runtime", "request": {"operations": []}}),
                         decision({"op": "symbols", "query": "NATIVE_DERIVED_QUERY"})])
    result = D.run(inert.root, tmp_path / "mixed-producer", adapter=producer,
                   max_decisions=2, max_context_chars=100000)
    assert result["tool_dispatches"] == 2
    reader, sent = model([static_decision() if static_only else decision()])
    result = D.run(inert.root, tmp_path / "narrow-consumer", adapter=reader,
                   static_only=static_only, allowed_tools=tools)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not sent
    assert "prior session tools exceed" in result["error"]["message"]


def test_mid_invocation_policy_drift_cannot_relabel_completed_turns(inert, tmp_path, monkeypatch):
    original, changed, calls = D._pin, [False], []
    def pinned(file):
        value = original(file)
        if changed[0] and file == Path(D.P.__file__).resolve():
            value["sha256"] = "e" * 64
        return value
    def drift_on_second_call():
        calls.append(True)
        changed[0] = len(calls) == 2
    monkeypatch.setattr(D, "_pin", pinned)
    adapter, seen = model([static_decision({"op": "resource", "relative": "A"}),
                           static_decision({"op": "resource", "relative": "B"})], hook=drift_on_second_call)
    out = tmp_path / "mid-call-drift"
    result = D.run(inert.root, out, adapter=adapter, static_only=True, max_decisions=2)
    assert result["status"] == "stopped_failure" and result["model_calls"] == len(seen) == 2
    assert result["tool_dispatches"] == len(inert.calls) == 1
    assert "memory_checkpoint_error" in result and not (out / "memory.json").exists()


def test_static_checkpoint_can_promote_to_full_native_investigation(inert, tmp_path):
    out, _ = _checkpoint(inert, tmp_path)
    adapter, sent = model([{"action": "stop", "request": None, "reason": "Retain static evidence for native investigation."}])
    result = D.run(inert.root, tmp_path / "promoted", adapter=adapter, decision_protocol="full_native_v1",
                   resume_from=out, max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["investigation_memory"]["restored_turns"] == 1
    view = json.loads(sent[0]["messages"][1]["content"])
    assert view["prior_investigations"][0]["op"] == "resource"


def test_owned_adapter_receives_explicit_model_budget_and_retains_actual_bounds(inert, tmp_path, monkeypatch):
    from dataclasses import replace
    adapter, seen = model([decision()])
    captured = []
    def configured(*args, **kwargs):
        captured.append((args, kwargs))
        adapter.config = replace(adapter.config, timeout=kwargs["timeout"], max_tokens=kwargs["max_tokens"])
        return adapter
    monkeypatch.setattr(M.ModelAdapter, "for_role", configured)
    result = D.run(inert.root, tmp_path / "larger-budget", timeout_seconds=3600,
                   model_max_tokens=16384, model_timeout_seconds=300)
    assert result["status"] == "model_stopped" and len(seen) == 1
    assert captured[0][1] == {"retries": 0, "timeout": 300, "max_tokens": 16384, "feature": "rea_agent_driver"}
    assert seen[0]["max_tokens"] == 16384
    assert result["model_bounds"] == {"max_tokens": 16384, "timeout_seconds": 300, "retries": 0,
                                     "structured_output_mode": "json_schema"}
    assert result["adapter_cleanup"]["confirmed"] is True


def test_explicit_reasoning_effort_is_sent_and_retained_in_actual_model_bounds(inert, tmp_path):
    adapter, seen = model([decision()])
    adapter.config = replace(adapter.config, reasoning_effort="none")
    out = tmp_path / "explicit-reasoning-effort"
    result = D.run(inert.root, out, adapter=adapter)
    assert result["status"] == "model_stopped" and result["model_calls"] == len(seen) == 1
    assert result["tool_dispatches"] == 0 and inert.calls == []
    assert seen[0]["reasoning_effort"] == "none"
    assert result["model_bounds"] == {"max_tokens": 2000, "timeout_seconds": .01, "retries": 0,
                                     "structured_output_mode": "json_schema", "reasoning_effort": "none"}
    saved = json.loads((out / "driver.json").read_text())
    assert saved["model_bounds"] == result["model_bounds"]


@pytest.mark.parametrize("bounds", [{"model_max_tokens": True}, {"model_max_tokens": 32769},
                                  {"model_timeout_seconds": 301}, {"model_timeout_seconds": 0}])
def test_invalid_model_bounds_refuse_before_output_or_call(inert, tmp_path, bounds):
    out = tmp_path / "invalid-model-budget"
    with pytest.raises(ValueError, match="model token or timeout"):
        D.run(inert.root, out, **bounds)
    assert not out.exists() and not inert.calls


def test_clean_negative_native_metadata_is_retained_and_resumed_as_negative_evidence(session, tmp_path, monkeypatch):
    from test_rea_agent import clean_negative_metadata
    from anyuse.execution import rea_runtime_bridge as B
    root = session[0]
    calls = []
    def collect(bundle, out, request, **kwargs):
        calls.append(copy.deepcopy(request))
        state = G.R.load_json(root / "session.json")
        receipt = clean_negative_metadata(state, request)
        folder = Path(out); folder.mkdir(parents=True)
        receipt["receipt"] = str(folder / "runtime.json")
        G.R.write_json(folder / "runtime.json", receipt)
        G.R.write_json(folder / "worker-output.json", receipt["result"])
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    request = {"op": "discover", "provider": "native", "timeout": 15,
               "request": {"operations": [{"op": "methods", "class": "ExampleMissing",
                                             "query": None, "own": True, "limit": 8}]}}
    adapter, seen = model([{"action": "step", "request": request, "reason": "Inspect a candidate class."}])
    first = tmp_path / "negative-producer"
    result = D.run(root, first, adapter=adapter, decision_protocol="full_native_v1", until="step",
                   allowed_tools=["discover", "evidence"], max_context_chars=100000)
    assert result["status"] == "boundary_reached" and len(calls) == 1
    assert result["tool_dispatches"] == 1 and not G.R.load_json(root / "session.json").get("proof")
    reader, messages = model([{"action": "stop", "request": None, "reason": "Candidate absent; more evidence is needed."}])
    result = D.run(root, tmp_path / "negative-consumer", adapter=reader, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "evidence"], resume_from=first, max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["investigation_memory"]["restored_turns"] == 1
    view = json.loads(messages[0]["messages"][1]["content"])
    retained = json.dumps(view["prior_investigations"])
    assert "negative_metadata_observation" in retained and "ExampleMissing" in retained
    assert "exact Objective-C class is unavailable" in retained and len(calls) == 1


def test_native_discovery_facts_reach_the_next_decision_without_a_separate_read(session, tmp_path, monkeypatch):
    from anyuse.execution import rea_runtime_bridge as B
    root = session[0]
    calls = []
    def collect(bundle, out, request, **kwargs):
        calls.append(copy.deepcopy(request))
        folder = Path(out); folder.mkdir(parents=True)
        worker = {"status": "completed", "request_sha256": G.R.digest(request), "operations": [
            {"index": 0, "op": "classes", "status": "ok", "matched": 1, "truncated": False,
             "rows": [{"name": "ExampleObservedClass", "module": str(Path(bundle)/'Contents/MacOS/Fixture')}]}]}
        G.R.write_json(folder / "worker-output.json", worker)
        receipt = {"format": B.RUNTIME, "status": "observed", "error": None, "actuated": False,
                   "resource_review_required": False, "result": worker, "receipt": str(folder/'runtime.json')}
        G.R.write_json(folder / "runtime.json", receipt)
        return receipt
    monkeypatch.setattr(B, "collect", collect)
    request = {"op": "discover", "provider": "native", "timeout": 15,
               "request": {"operations": [{"op": "classes", "query": "Example", "owner": "app", "limit": 8}]}}
    adapter, messages = model([{"action": "step", "request": request, "reason": "Discover the app classes."},
                               {"action": "stop", "request": None, "reason": "Class observed; method evidence still needed."}])
    result = D.run(root, tmp_path/'direct-facts', adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "evidence"], max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["model_calls"] == 2 and len(calls) == 1
    observed = messages[1]["messages"][1]["content"]
    assert "ExampleObservedClass" in observed and "s0000/runtime/worker-output.json" in observed
    assert result["tool_dispatches"] == 1 and not G.R.load_json(root/'session.json').get('proof')




def _native_bad_request():
    return {"action": "step", "request": {"op": "resource"}, "reason": "Inspect current evidence"}


def _correctable_run(inert, tmp_path, adapter, **bounds):
    return D.run(inert.root, tmp_path / "correctable", adapter=adapter,
                 decision_protocol="full_native_v1", max_context_chars=100000,
                 max_decision_corrections=1, **bounds)


def test_bounded_rejection_feedback_requires_a_new_valid_decision_before_dispatch(inert, tmp_path):
    good = {"action": "step", "request": {"op": "resource", "relative": "public"}, "reason": "Read measured data"}
    stop = {"action": "stop", "request": None, "reason": "More native evidence is required"}
    adapter, seen = model([_native_bad_request(), good, stop])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=3)
    assert result["status"] == "model_stopped" and result["model_calls"] == 3
    assert inert.calls == [{"op": "resource", "relative": "public"}]
    assert result["tool_dispatches"] == 1 and result["decision_corrections"]["used"] == 1
    assert result["decisions"][0]["status"] == "decision_rejected"
    next_view = json.loads(seen[1]["messages"][1]["content"])
    rejected = next_view["last_observation"]
    assert rejected["rejected_decision"] == _native_bad_request()
    assert rejected["tool_dispatched"] is False and rejected["proof_created"] is False
    assert "missing=['relative']" in rejected["validation_error"]
    assert next_view["driver_bounds"]["decision_corrections_remaining"] == 0
    assert next_view["history"] == [] and next_view["prior_investigations"] == []
    output = json.loads((tmp_path / "correctable/decision-000/model-output.json").read_text())
    assert output["parsed"] == _native_bad_request()
    memory = json.loads((tmp_path / "correctable/memory.json").read_text())
    assert len(memory["turns"]) == 1 and memory["turns"][0]["op"] == "resource"


@pytest.mark.parametrize("until", ["step", "hypothesis", "published"])
def test_full_native_prompt_explains_only_its_configured_completion_boundary(inert, tmp_path, until):
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Current evidence does not establish a supported actor."}])
    result = D.run(inert.root, tmp_path / "phase-boundary", adapter=adapter, decision_protocol="full_native_v1",
                   until=until, max_context_chars=100000)
    assert result["status"] == "model_stopped" and not inert.calls and result["tool_dispatches"] == 0
    payload = json.loads(seen[0]["messages"][1]["content"])
    boundary = payload["decision_protocol"]["completion_boundary"]
    assert boundary["until"] == payload["driver_bounds"]["until"] == until
    assert "1200" in payload["decision_protocol"]["reason"] and "1200" in seen[0]["messages"][0]["content"]
    assert seen[0]["response_format"]["json_schema"]["schema"]["properties"]["reason"]["maxLength"] == 1200
    if until == "hypothesis":
        assert "unproved hypothesis" in boundary["deliverable"]
        assert "caller handles later learning, verification and heldout" in boundary["deliverable"]
        assert "do not need to run" in boundary["deliverable"] and "G still validates" in boundary["deliverable"]
    elif until == "step":
        assert "ends after one admitted tool call" in boundary["deliverable"]
        assert "caller handles later learning" not in boundary["deliverable"]
    else:
        assert "configured boundary is publication" in boundary["deliverable"]
        assert "every existing proof gate" in boundary["deliverable"] and "needs_setup" in boundary["deliverable"]


def test_overlong_stop_is_retained_unchanged_and_can_be_followed_by_bounded_stop(inert, tmp_path):
    long_stop = {"action": "stop", "request": None, "reason": "x" * 4993}
    concise_stop = {"action": "stop", "request": None, "reason": "Current method evidence remains incomplete."}
    adapter, seen = model([long_stop, concise_stop])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=2)
    assert result["status"] == "model_stopped" and result["model_calls"] == len(seen) == 2
    assert result["tool_dispatches"] == 0 and not inert.calls and result["decision_corrections"]["used"] == 1
    rejection = json.loads((tmp_path / "correctable/decision-000/decision-rejection.json").read_bytes())
    assert rejection["rejected_decision"] == long_stop
    assert "$.reason" in rejection["validation_error"] and "1..1200" in rejection["validation_error"]
    assert "received 4993" in rejection["validation_error"]
    corrected_view = json.loads(seen[1]["messages"][1]["content"])
    assert corrected_view["last_observation"] == rejection
    assert corrected_view["driver_bounds"]["decision_corrections_remaining"] == 0
    assert result["reason"] == concise_stop["reason"] and result["decisions"][-1]["status"] == "stopped"


def test_second_invalid_decision_exhausts_explicit_correction_bound_without_a_tool(inert, tmp_path):
    adapter, seen = model([_native_bad_request(), _native_bad_request()])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=8)
    assert result["status"] == "stopped_failure" and result["model_calls"] == len(seen) == 2
    assert result["decision_corrections"]["used"] == 1 and not inert.calls
    assert result["decisions"][0]["status"] == "decision_rejected"
    assert (tmp_path / "correctable/decision-000/decision-rejection.json").is_file()


def test_correction_uses_existing_total_decision_budget(inert, tmp_path):
    adapter, seen = model([_native_bad_request()])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=1)
    assert result["status"] == "decision_budget_exhausted" and result["model_calls"] == len(seen) == 1
    assert result["tool_dispatches"] == 0 and not inert.calls


@pytest.mark.parametrize("failure", [503, "not JSON", '{"action":"stop","action":"step"}'])
def test_correction_does_not_retry_transport_or_decoding_failure(inert, tmp_path, failure):
    adapter, seen = model([failure])
    result = _correctable_run(inert, tmp_path, adapter)
    assert result["status"] == "stopped_failure" and len(seen) == result["model_calls"] == 1
    assert result["decision_corrections"]["used"] == 0 and not inert.calls


@pytest.mark.parametrize("change", ["length", "parsed"])
def test_correction_never_relaxes_output_integrity_gates(inert, tmp_path, monkeypatch, change):
    adapter, seen = model([_native_bad_request()])
    complete = adapter.complete
    def changed(*args, **kwargs):
        answer = complete(*args, **kwargs)
        return replace(answer, **({"finish_reason": "length"} if change == "length" else {"parsed": {"different": True}}))
    monkeypatch.setattr(adapter, "complete", changed)
    result = _correctable_run(inert, tmp_path, adapter)
    assert result["status"] == "stopped_failure" and len(seen) == 1 and not inert.calls
    assert result["decision_corrections"]["used"] == 0


def test_changed_session_prevents_correction_or_dispatch(inert, tmp_path):
    def change():
        inert.state["changed_during_request"] = True
        inert.save()
    adapter, seen = model([_native_bad_request()], hook=change)
    result = _correctable_run(inert, tmp_path, adapter)
    assert result["status"] == "stopped_failure" and "session changed" in result["error"]["message"]
    assert len(seen) == 1 and not inert.calls and result["decision_corrections"]["used"] == 0


def test_changed_rejection_receipt_blocks_later_valid_tool(inert, tmp_path):
    def tamper():
        p = tmp_path / "correctable/decision-000/decision-rejection.json"
        if p.exists():
            p.write_text('{}\n')
    good = {"action": "step", "request": {"op": "resource", "relative": "public"}, "reason": "Inspect"}
    adapter, seen = model([_native_bad_request(), good], hook=tamper)
    result = _correctable_run(inert, tmp_path, adapter)
    assert result["status"] == "stopped_failure" and len(seen) == 2 and not inert.calls
    assert result["tool_dispatches"] == 0


def test_correction_feedback_obeys_existing_context_bound_without_truncation(inert, tmp_path):
    bad = _native_bad_request()
    bad["unknown"] = "x" * 90000
    adapter, seen = model([bad])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=2)
    assert result["status"] == "stopped_failure" and "context exceeds bound" in result["error"]["message"]
    assert len(seen) == 1 and not inert.calls
    rejected = json.loads((tmp_path / "correctable/decision-000/decision-rejection.json").read_text())
    assert rejected["rejected_decision"] == bad


@pytest.mark.parametrize("bound", [True, -1, 4, 1.0])
def test_correction_configuration_is_closed_and_bounded(inert, tmp_path, bound):
    adapter, seen = model([])
    with pytest.raises(ValueError):
        D.run(inert.root, tmp_path / "bad-corrections", adapter=adapter,
              decision_protocol="full_native_v1", max_decision_corrections=bound)
    assert not seen and not inert.calls


def test_correction_option_requires_explicit_full_native_protocol(inert, tmp_path):
    adapter, seen = model([])
    with pytest.raises(ValueError, match="full_native_v1"):
        D.run(inert.root, tmp_path / "wrong-protocol", adapter=adapter, max_decision_corrections=1)
    assert not seen


def test_decision_correction_never_retries_a_dispatched_failed_tool(inert, tmp_path):
    inert.payloads["public"] = {"status": "failed", "error": "native cleanup requires review"}
    good = {"action": "step", "request": {"op": "resource", "relative": "public"}, "reason": "Inspect"}
    adapter, seen = model([good])
    result = _correctable_run(inert, tmp_path, adapter)
    assert result["status"] == "stopped_failure" and "mapping tool failed" in result["error"]["message"]
    assert len(seen) == result["model_calls"] == result["tool_dispatches"] == 1
    assert result["decision_corrections"]["used"] == 0


def test_rejected_decision_keeps_latest_actual_observation_in_verified_prior_window(inert, tmp_path):
    inert.payloads["public"] = {"status": "observed", "measured_fact": "LATEST_ACTUAL_VALUE"}
    good = {"action": "step", "request": {"op": "resource", "relative": "public"}, "reason": "Inspect"}
    stop = {"action": "stop", "request": None, "reason": "Need more evidence"}
    adapter, seen = model([good, _native_bad_request(), stop])
    result = _correctable_run(inert, tmp_path, adapter, max_decisions=3)
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 1
    view = json.loads(seen[2]["messages"][1]["content"])
    assert view["last_observation"]["status"] == "decision_rejected"
    assert len(view["prior_investigations"]) == 1
    prior = view["prior_investigations"][0]
    assert prior["observation"]["measured_fact"] == "LATEST_ACTUAL_VALUE"
    assert prior["evidence"]["artifact"] == "s0000/feedback.json"
    manifest = json.loads((tmp_path / "correctable/decision-002/redaction.json").read_text())
    assert manifest["investigation_window"]["latest_investigation_in_last_observation"] is False
    assert manifest["investigation_window"]["prior_turns_exposed"] == 1


def _metadata_checkpoint(inert, tmp_path, *, requests=None, following=5, tools=None):
    requests = requests if requests is not None else [{"op": "discover", "provider": "native", "request": {
        "operations": [{"op": "methods", "class": "MeasuredClass", "query": "", "own": False, "limit": 128},
                       {"op": "ivars", "class": "MeasuredClass", "query": None},
                       {"op": "classes", "query": "Measured", "owner": "app"}]}}]
    requests = [*requests, *({"op": "resource", "relative": f"later-{i}"} for i in range(following))]
    tools = ["discover", "resource", "evidence"] if tools is None else tools
    producer, _ = model([decision(request, reason="Untrusted model reason must not enter query index")
                         for request in requests])
    out = tmp_path / "metadata-producer"
    result = D.run(inert.root, out, adapter=producer, allowed_tools=tools,
                   max_decisions=len(requests), max_context_chars=100000)
    assert result["status"] == "decision_budget_exhausted" and result["tool_dispatches"] == len(requests)
    return out


def _read_metadata_index(inert, tmp_path, *, resume_from=None, tools=None, name="metadata-consumer", max_context_chars=100000):
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Retain historical query scopes"}])
    result = D.run(inert.root, tmp_path / name, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "resource", "evidence"] if tools is None else tools,
                   resume_from=resume_from, max_context_chars=max_context_chars)
    assert result["status"] == "model_stopped"
    view = json.loads(seen[0]["messages"][1]["content"])
    return view["historical_metadata_query_index"], view, result


def test_metadata_query_index_keeps_exact_old_scopes_with_pinned_request_pointers(inert, tmp_path):
    inert.payloads["discover"] = {"observation": {"operations": [
        {"index": 0, "op": "methods", "status": "ok", "matched": 0, "truncated": False,
         "rows": [{"selector": "METHOD_ROWS_MUST_NOT_ENTER_INDEX", "address": "ADDRESS_PRIVATE"}]},
        {"index": 2, "op": "classes", "status": "ok", "matched": 9, "truncated": True}]},
        "proof": "PROOF_MUST_NOT_ENTER_INDEX"}
    out = _metadata_checkpoint(inert, tmp_path)
    index, view, result = _read_metadata_index(inert, tmp_path, resume_from=out)
    assert result["investigation_memory"]["restored_turns"] == 6
    assert all(turn["g_step_index"] > 0 for turn in view["prior_investigations"])
    entries = index["entries"]
    assert [entry["operation_index"] for entry in entries] == [0, 1, 2]
    assert entries[0]["request"] == {"op": "methods", "class": "MeasuredClass", "query": "", "own": False, "limit": 128}
    assert entries[1]["request"] == {"op": "ivars", "class": "MeasuredClass", "query": None}
    assert entries[0]["summary"] == {"status": "ok", "matched": 0, "truncated": False}
    assert entries[1]["summary"] == {} and "unknown" in index["unknowns"]
    assert entries[2]["summary"]["truncated"] is True
    assert "historical" in index["freshness"] and "freshness not checked" in index["freshness"]
    for entry in entries:
        assert entry["g_step_index"] == 0
        source = entry["evidence"]
        assert source["artifact"] == "s0000/feedback.json"
        assert source["sha256"] == inert.state["artifacts"][source["artifact"]]["sha256"]
        assert source["pointer"] == f"/request/request/operations/{entry['operation_index']}"
    assert all(value not in json.dumps(index) for value in
               ("METHOD_ROWS_MUST_NOT_ENTER_INDEX", "ADDRESS_PRIVATE", "PROOF_MUST_NOT_ENTER_INDEX", "Untrusted model reason"))
    saved = json.loads((tmp_path / "metadata-consumer/memory.json").read_text())
    assert saved["format"] == D.MEMORY_FORMAT
    assert all("historical_metadata_query_index" not in turn for turn in saved["turns"])


def test_metadata_query_index_keeps_old_inspector_absence_without_callable_or_freshness_claim(inert, tmp_path):
    request = {'op': 'discover', 'provider': 'inspector', 'request': {'operations': [
        {'op': 'method', 'recipe': {'root': {'kind': 'global', 'name': 'globalThis'}, 'steps': []},
         'method': 'absentLiteralMethod'}]}}
    inert.payloads['discover'] = {'observation': {'operations': [
        {'op': 'method', 'pointer': '/operations/0', 'method': 'absentLiteralMethod',
         'status': 'not_found', 'callable': False, 'function_source': 'DO_NOT_INDEX_A_FUNCTION_BODY'}]}}
    out = _metadata_checkpoint(inert, tmp_path, requests=[request], following=5)
    index, view, result = _read_metadata_index(inert, tmp_path, resume_from=out)
    assert result['investigation_memory']['restored_turns'] == 6
    assert all(turn['g_step_index'] > 0 for turn in view['prior_investigations'])
    assert len(index['entries']) == 1
    entry = index['entries'][0]
    assert entry['provider'] == 'inspector' and entry['g_step_index'] == 0
    assert entry['request'] == request['request']['operations'][0]
    assert entry['summary'] == {'status': 'not_found', 'callable': False}
    assert entry['evidence']['artifact'] == 's0000/feedback.json'
    assert entry['evidence']['sha256'] == inert.state['artifacts']['s0000/feedback.json']['sha256']
    assert entry['evidence']['summary_pointer'] == '/feedback/observation/operations/0'
    assert 'historical' in index['freshness'] and 'freshness not checked' in index['freshness']
    assert 'no duplicate suppression, caching or blocking' in index['use']
    assert 'DO_NOT_INDEX_A_FUNCTION_BODY' not in json.dumps(index)


def test_metadata_query_index_requires_explicit_resume_and_honors_source_scope(inert, tmp_path):
    out = _metadata_checkpoint(inert, tmp_path, tools=["discover", "resource", "evidence", "runtime"])
    no_resume, _, result = _read_metadata_index(inert, tmp_path, name="without-resume")
    assert no_resume["entries"] == [] and result["investigation_memory"]["requested"] is False
    narrowed, _, result = _read_metadata_index(inert, tmp_path, resume_from=out, name="narrowed")
    assert narrowed["entries"] == [] and result["investigation_memory"]["restored_turns"] == 0
    assert result["investigation_memory"]["scope_filtered_steps"] == list(range(6))
    admitted, _, _ = _read_metadata_index(inert, tmp_path, resume_from=out, name="admitted",
                                         tools=["discover", "resource", "evidence", "runtime"])
    assert len(admitted["entries"]) == 3


@pytest.mark.parametrize("summary", [None, {}, {"operations": []}, {"operations_omitted": 1},
    {"operations": [{"index": 0, "op": "methods", "status": "not_reached"}]}])
def test_metadata_query_index_never_infers_missing_or_partial_summary_counts(inert, tmp_path, summary):
    inert.payloads["discover"] = {"observation": summary}
    request = {"op": "discover", "provider": "native", "request": {
        "operations": [{"op": "methods", "class": "MeasuredClass"}]}}
    out = _metadata_checkpoint(inert, tmp_path, requests=[request], following=0)
    index, _, _ = _read_metadata_index(inert, tmp_path, resume_from=out)
    entry = index["entries"][0]
    assert "matched" not in entry["summary"] and "truncated" not in entry["summary"]
    assert entry["summary"] == ({"status": "not_reached"} if summary and summary.get("operations") else {})
    assert "unknown" in index["unknowns"] and "never imply zero" in index["unknowns"]


def test_metadata_query_index_excludes_mixed_instances_and_runtime_transactions(inert, tmp_path):
    out = _metadata_checkpoint(inert, tmp_path, following=0, tools=["discover", "runtime", "resource", "evidence"],
        requests=[{"op": "discover", "provider": "native", "request": {"operations": [
            {"op": "classes", "query": "PureMetadata"}]}},
            {"op": "discover", "provider": "native", "request": {"operations": [
                {"op": "classes", "query": "MIXED_MUST_NOT_ENTER_INDEX"},
                {"op": "instances", "class": "MeasuredClass", "alias": "PRIVATE_HANDLE"}]}},
            {"op": "runtime", "provider": "native", "request": {"operations": [
                {"op": "methods", "class": "RUNTIME_MUST_NOT_ENTER_INDEX"}]}}])
    index, _, _ = _read_metadata_index(inert, tmp_path, resume_from=out,
                                       tools=["discover", "runtime", "resource", "evidence"])
    assert len(index["entries"]) == 1 and index["entries"][0]["request"]["query"] == "PureMetadata"
    assert all(value not in json.dumps(index) for value in ("MIXED_MUST_NOT_ENTER_INDEX", "PRIVATE_HANDLE", "RUNTIME_MUST_NOT_ENTER_INDEX"))


@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("source", ["feedback", "producer"])
def test_old_indexed_source_change_blocks_dispatch_and_rejected_decision_continuation(inert, tmp_path, rejected, source):
    out = _metadata_checkpoint(inert, tmp_path)
    file = inert.root / "s0000/feedback.json" if source == "feedback" else out / "decision-000/model-input.json"
    def tamper():
        file.write_text('{}\n')
    reply = _native_bad_request() if rejected else {
        "action": "step", "request": {"op": "resource", "relative": "never-dispatched"}, "reason": "Inspect"}
    adapter, seen = model([reply], hook=tamper)
    calls_before = len(inert.calls)
    result = D.run(inert.root, tmp_path / "tampered-index", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "resource", "evidence"], resume_from=out,
                   max_context_chars=100000, max_decision_corrections=1)
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0
    assert len(seen) == 1 and len(inert.calls) == calls_before
    assert result["decision_corrections"]["used"] == 0
    assert "changed" in result["error"]["message"]


def test_metadata_query_index_bounds_whole_entries_without_truncation_or_deduplication(inert, tmp_path, monkeypatch):
    operation = {"op": "classes", "query": "Q" * 200, "owner": "app", "limit": 128}
    request = {"op": "discover", "provider": "native", "request": {"operations": [operation] * 16}}
    out = _metadata_checkpoint(inert, tmp_path, requests=[request] * 4, following=0)
    turns, _ = D._load_memory(out, inert.root, inert.state, ["discover", "resource", "evidence"], False)
    for budget in (1000, D.MAX_METADATA_QUERY_CHARS):
        index, sources = D._metadata_query_index(inert.root, inert.state, turns, ["discover"], budget)
        assert len(D._json(index)) <= budget
        assert index["omitted_entries"] == 64 - len(index["entries"])
        assert len(index["entries"]) <= D.MAX_METADATA_QUERY_ENTRIES
        assert all(entry["request"] == operation for entry in index["entries"])
        assert all(source["op"] == "discover" for source in sources)
    assert len(index["entries"]) > 1 and index["omitted_entries"] > 0
    # Check the independent operation bound even with ample character space.
    monkeypatch.setattr(D, "MAX_METADATA_QUERY_CHARS", 100000)
    index, _ = D._metadata_query_index(inert.root, inert.state, turns, ["discover"], 100000)
    assert len(index["entries"]) == 32 and index["omitted_entries"] == 32


def test_metadata_query_index_uses_remaining_context_without_displacing_existing_evidence(inert, tmp_path):
    out = _metadata_checkpoint(inert, tmp_path)
    original, view, _ = _read_metadata_index(inert, tmp_path, resume_from=out)
    wire = json.loads((tmp_path / "metadata-consumer/decision-000/model-input.json").read_text())
    base = {key: value for key, value in view.items() if key != "historical_metadata_query_index"}
    bound = len(wire["messages"][0]["content"]) + len(D._json(base)) + len(D._json("historical_metadata_query_index")) + 4 + 1000
    compact, compact_view, _ = _read_metadata_index(inert, tmp_path, resume_from=out,
                                                    name="compact-index", max_context_chars=bound)
    assert compact_view["prior_investigations"] == view["prior_investigations"]
    assert compact_view["artifacts"] == view["artifacts"]
    assert compact["omitted_entries"] > original["omitted_entries"]
    compact_wire = json.loads((tmp_path / "compact-index/decision-000/model-input.json").read_text())
    assert sum(len(message["content"]) for message in compact_wire["messages"]) <= bound


def test_old_indexed_feedback_is_revalidated_before_next_model(inert, tmp_path, monkeypatch):
    out = _metadata_checkpoint(inert, tmp_path)
    original_step = G.step
    def step_and_tamper(*args):
        observed = original_step(*args)
        (inert.root / "s0000/feedback.json").write_text('{}\n')
        return observed
    monkeypatch.setattr(G, "step", step_and_tamper)
    adapter, seen = model([{"action": "step", "request": {"op": "resource", "relative": "next"}, "reason": "Inspect"}])
    result = D.run(inert.root, tmp_path / "tampered-before-next-model", adapter=adapter,
                   decision_protocol="full_native_v1", allowed_tools=["discover", "resource", "evidence"],
                   resume_from=out, max_context_chars=100000)
    assert result["status"] == "stopped_failure" and "changed" in result["error"]["message"]
    assert len(seen) == result["tool_dispatches"] == 1


@pytest.mark.parametrize("static_only", [False, True])
def test_metadata_query_index_does_not_change_legacy_or_static_prompt(inert, tmp_path, static_only):
    adapter, seen = model([(static_decision if static_only else decision)()])
    result = D.run(inert.root, tmp_path / "unchanged-prompt", adapter=adapter, static_only=static_only)
    assert result["status"] == "model_stopped"
    assert "historical_metadata_query_index" not in json.loads(seen[0]["messages"][1]["content"])


@pytest.fixture
def native_metadata_bridge_session(native_metadata_bridge_runtime, tmp_path):
    """Real G session on the bridge's inert app, without harness adapter mocks."""
    app = native_metadata_bridge_runtime[0]
    rea = tmp_path / "metadata-rea-inert"
    rea.write_text("inert executable fixture; never executed")
    rea.chmod(0o700)
    params = [{"name": "query", "type": "string", "source": "lookup"}]
    plan = {
        role: [{"inputs": {"query": f"{role}-{index}"}} for index in range(3)]
        for role in G.ROLES
    }
    root = tmp_path / "metadata-bridge-session"
    G.begin(str(app), str(rea), "Inspect candidate metadata in the owned fixture",
            params, ["Retain observed metadata and explicit unavailable classes"],
            plan, str(root))
    # Only discover/evidence are admitted below; no proposal or task trial is
    # attempted, so an observer declaration is unnecessary for this session.
    return root


def _metadata_prefix_then_unknown_class_request():
    return {
        "op": "discover", "provider": "native", "timeout": 15,
        "request": {"operations": [
            {"op": "classes", "query": "Example", "owner": "app", "limit": 8},
            {"op": "methods", "class": "ExampleMissing", "query": None,
             "own": True, "limit": 8},
        ]},
    }


def _install_real_metadata_worker_runner(runtime, monkeypatch, *, cleanup_failure=None):
    """Replace only process spawning and the Frida API; keep bridge gates real."""
    app, identity, _, _, _ = runtime
    runs = []
    primary_error = {
        "type": "Error",
        "message": "exact Objective-C class is unavailable: ExampleMissing",
    }

    def bounded_run(argv, output, error, *, timeout, cwd):
        config_path = Path(argv[-1])
        config = G.R.load_json(config_path)
        assert config_path == Path(cwd) / "worker-request.json"
        assert config["identity"] == identity
        assert config["target"] == G.R.fingerprint(app)
        assert config["transport"] == {"kind": "local"}
        # full_native_v1 removes unused nullable wire fields before G dispatch.
        expected = _metadata_prefix_then_unknown_class_request()["request"]
        expected["operations"][1].pop("query")
        assert config["request"] == expected
        run = {"config": config, "output": Path(output)}
        runs.append(run)
        worker, activity, fixture_config, _ = _worker_fixture(
            runtime, monkeypatch, request=config["request"],
            worker_config_path=config_path, cleanup_failure=cleanup_failure,
            observed_changes={
                "status": "incomplete", "error": primary_error,
                "operations": [{
                    "index": 0, "op": "classes", "status": "ok",
                    "matched": 1, "truncated": False,
                    "rows": [{"name": "ExamplePresent",
                              "module": str(app / "Contents/MacOS/Canvas")}],
                }],
                "invocations": 0, "native_calls": 0, "helper_load_attempts": 0,
                "native_state_uncertain": False, "handles_expired": True,
            },
        )
        assert fixture_config["request"] == config["request"]
        run.update(worker=worker, activity=activity)
        G.R.write_json(output, worker)
        Path(error).write_text("")
        # Mirror the real bridge's __main__ exit disposition for incomplete
        # workers. _collect must admit a clean negative despite returncode=1.
        return {
            "returncode": 0 if worker["status"] == "completed" else 1,
            "timed_out": False, "elapsed_s": .001,
            "output": Path(output).name, "sha256": G.R._file_hash(Path(output)),
            "stderr": Path(error).name,
        }

    monkeypatch.setattr(G.R, "_bounded_run", bounded_run)
    return runs, primary_error


def _assert_real_metadata_worker_chain(root, runtime, runs, primary_error, *, manager_state):
    """Check actual persisted production envelopes, counters, and cleanup order."""
    assert len(runs) == 1
    run = runs[0]
    folder = root / "s0000/runtime"
    receipt = G.R.load_json(folder / "runtime.json")
    worker = G.R.load_json(folder / "worker-output.json")
    config = G.R.load_json(folder / "worker-request.json")
    assert worker == run["worker"] == receipt["result"]
    assert config == run["config"]
    assert worker["format"] == B.WORKER
    assert B._worker_result(worker, G.R.digest(config["request"]), config["request"],
                            config["transport"]) == worker
    assert receipt["format"] == B.RUNTIME and receipt["status"] == "incomplete"
    assert receipt["worker"]["returncode"] == 1
    assert receipt["worker"]["sha256"] == G.R._file_hash(folder / "worker-output.json")
    assert receipt["request"] == config["request"]
    assert receipt["request_sha256"] == worker["request_sha256"] == G.R.digest(config["request"])
    assert receipt["identity"] == receipt["after_identity"] == runtime[1]
    assert worker["identity"] == worker["after_identity"] == runtime[1]
    assert receipt["error"] == worker["error"] == primary_error
    assert receipt["actuated"] is False and receipt["native_calls"] is False
    assert receipt["evidence_only"] is True and receipt["verified"] is False
    assert receipt["proof_created"] is False
    for counter in ("invocations", "native_calls", "helper_load_attempts"):
        assert type(worker[counter]) is int and worker[counter] == 0
    assert worker["status"] == "incomplete" and worker["attached"] is True
    assert worker["handles_expired"] is True and worker["native_state_uncertain"] is False
    assert worker["cleanup"] == {"script_unloaded": True, "detached": True}
    assert len(worker["operations"]) == 1
    assert worker["operations"][0]["op"] == "classes"
    assert worker["operations"][0]["rows"][0]["name"] == "ExamplePresent"
    lifecycle = worker["frida_lifecycle"]
    assert lifecycle["format"] == B.FRIDA_LIFECYCLE
    assert lifecycle["manager_state"] == manager_state
    assert lifecycle["attachment_attempted"] is True
    assert lifecycle["helper_exit_confirmed"] is False
    activity = run["activity"]
    assert [call[0] for call in activity] == [
        "manager", "local", "attach", "create", "message_handler", "load", "run",
        "unload", "detach", "manager_close",
    ]
    assert activity[2] == ("attach", runtime[1]["pid"])
    assert activity[3][2] == {"name": "anyuse-fixed-rea-runtime", "runtime": "qjs"}
    assert activity[4] == ("message_handler", "message")
    assert activity[6][1]["request"] == config["request"]
    journal = G.R.load_json(folder / "worker-cleanup.json")
    assert journal["format"] == B.CLEANUP_JOURNAL
    assert journal["request_sha256"] == worker["request_sha256"]
    assert journal["completed_operations"] == 1
    assert journal["cleanup"] == worker["cleanup"]
    assert journal["frida_lifecycle"] == lifecycle
    assert journal["cleanup_errors"] == worker["cleanup_errors"]
    assert journal["error"] == primary_error
    assert not G.R.load_json(root / "session.json").get("proof")
    return receipt, worker


def test_real_bridge_partial_metadata_negative_continues_to_pinned_evidence(
        native_metadata_bridge_session, native_metadata_bridge_runtime, tmp_path, monkeypatch):
    root = native_metadata_bridge_session
    runs, primary_error = _install_real_metadata_worker_runner(
        native_metadata_bridge_runtime, monkeypatch)
    discover = _metadata_prefix_then_unknown_class_request()
    evidence = {"op": "evidence", "artifact": "s0000/runtime/worker-output.json",
                "offset": 0, "limit": 8192}
    adapter, messages = model([
        {"action": "step", "request": discover, "reason": "Inspect candidate classes and exact methods."},
        {"action": "step", "request": evidence, "reason": "Read the retained partial metadata and missing-class error."},
        {"action": "stop", "request": None, "reason": "One class was observed; the second exact class is unavailable."},
    ])
    result = D.run(root, tmp_path / "bridge-negative-driver", adapter=adapter,
                   decision_protocol="full_native_v1", allowed_tools=["discover", "evidence"],
                   max_context_chars=100000)
    assert result["status"] == "model_stopped"
    assert result["tool_dispatches"] == 2 and result["model_calls"] == len(messages) == 3
    receipt, worker = _assert_real_metadata_worker_chain(
        root, native_metadata_bridge_runtime, runs, primary_error, manager_state="closed")
    assert receipt["resource_review_required"] is False
    assert worker["cleanup_errors"] == [] and worker["frida_lifecycle"]["error"] is None
    state = G.R.load_json(root / "session.json")
    assert state["status"] == "investigating"
    assert len(state["steps"]) == 2
    assert all(step["status"] == "completed" for step in state["steps"])
    negative = state["steps"][0]["feedback"]
    assert negative["status"] == "negative_metadata_observation"
    assert negative["automatic_retry"] is False and negative["proof_created"] is False
    assert negative["negative_observation"] == {
        "status": "incomplete", "error": primary_error,
        "completed_operations": 1, "requested_operations": 2,
    }
    assert negative["observation"]["operations"][0]["names"] == ["ExamplePresent"]
    next_view = json.loads(messages[1]["messages"][1]["content"])
    assert next_view["last_observation"] == negative
    assert evidence["artifact"] in next_view["artifacts"]
    evidence_feedback = state["steps"][1]["feedback"]
    assert evidence_feedback["artifact"] == evidence["artifact"]
    assert evidence_feedback["sha256"] == receipt["worker"]["sha256"]
    assert json.loads(evidence_feedback["text"]) == worker


def test_real_bridge_metadata_negative_with_manager_cleanup_failure_is_terminal(
        native_metadata_bridge_session, native_metadata_bridge_runtime, tmp_path, monkeypatch):
    root = native_metadata_bridge_session
    runs, primary_error = _install_real_metadata_worker_runner(
        native_metadata_bridge_runtime, monkeypatch, cleanup_failure="manager")
    discover = _metadata_prefix_then_unknown_class_request()
    adapter, messages = model([
        {"action": "step", "request": discover, "reason": "Inspect candidate class metadata."},
        {"action": "step", "request": discover, "reason": "This follow-up must remain unconsumed."},
    ])
    result = D.run(root, tmp_path / "bridge-cleanup-failure-driver", adapter=adapter,
                   decision_protocol="full_native_v1", allowed_tools=["discover", "evidence"],
                   max_context_chars=100000)
    assert result["status"] == "stopped_failure"
    assert result["tool_dispatches"] == result["model_calls"] == len(messages) == 1
    receipt, worker = _assert_real_metadata_worker_chain(
        root, native_metadata_bridge_runtime, runs, primary_error, manager_state="failed")
    assert receipt["resource_review_required"] is True
    cleanup_error = {"type": "RuntimeError", "message": "manager close uncertain"}
    assert worker["frida_lifecycle"]["error"] == cleanup_error
    assert worker["cleanup_errors"] == [{"phase": "manager_close", "error": cleanup_error}]
    state = G.R.load_json(root / "session.json")
    assert state["status"] == result["session_status"] == "resource_review_required"
    assert len(state["steps"]) == 1 and state["steps"][0]["status"] == "failed"
    assert "runtime cleanup or effects need review; do not retry" in state["steps"][0]["feedback"]["error"]
    assert not (root / "s0001").exists()
    assert not (tmp_path / "bridge-cleanup-failure-driver/decision-001").exists()
    # A new driver invocation cannot reopen the failed session or consume even
    # a STOP model response. Only the existing review path can resolve it.
    blocked_adapter, blocked_messages = model([
        {"action": "stop", "request": None, "reason": "This response must remain unconsumed."},
    ])
    blocked = D.run(root, tmp_path / "bridge-cleanup-still-terminal", adapter=blocked_adapter,
                    decision_protocol="full_native_v1", allowed_tools=["discover", "evidence"],
                    max_context_chars=100000)
    assert blocked["status"] == "session_requires_review"
    assert blocked["tool_dispatches"] == blocked["model_calls"] == 0
    assert blocked_messages == [] and len(runs) == 1
    assert G.R.load_json(root / "session.json")["steps"] == state["steps"]




def test_json_object_optional_omissions_need_no_correction_and_memory_keeps_producer_mode(inert, tmp_path):
    request = {"op": "discover", "provider": "native", "request": {"operations": [{"op": "classes"}]}}
    reply = {"action": "step", "request": request, "reason": "Inspect metadata"}
    stop = {"action": "stop", "request": None, "reason": "Need more evidence"}
    adapter, sent = model([reply, stop])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    out = tmp_path / "json-object-optional"
    result = D.run(inert.root, out, adapter=adapter, decision_protocol="full_native_v1",
                   max_context_chars=100000, max_decision_corrections=1)
    assert result["status"] == "model_stopped" and result["decision_corrections"]["used"] == 0
    assert result["tool_dispatches"] == len(inert.calls) == 1
    dispatched = copy.deepcopy(inert.calls[0]); timeout = dispatched.pop("timeout")
    assert dispatched == request and type(timeout) is int and 1 <= timeout <= 60
    producer_view = json.loads(sent[0]["messages"][1]["content"])
    assert producer_view["decision_protocol"]["nullable_omissions"] is True
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert not any(x.startswith("Every schema field is required") for x in producer_view["agent_instructions"])
    saved = json.loads((out / "decision-000/model-output.json").read_text())
    assert saved["parsed"] == reply
    strict, strict_sent = model([reply])
    resumed = D.run(inert.root, tmp_path / "strict-after-compatible", adapter=strict,
                    decision_protocol="full_native_v1", resume_from=out, max_context_chars=100000)
    assert resumed["investigation_memory"]["restored_turns"] == 1
    assert resumed["status"] == "stopped_failure" and resumed["tool_dispatches"] == 0
    assert len(inert.calls) == 1
    current_view = json.loads(strict_sent[0]["messages"][1]["content"])
    assert "nullable_omissions" not in current_view["decision_protocol"]


def test_model_response_cannot_enable_nullable_omission_mode(inert, tmp_path):
    reply = {"action": "step", "request": {"op": "resource", "relative": "public"},
             "reason": "Inspect", "nullable_omissions": True}
    adapter, sent = model([reply])
    result = D.run(inert.root, tmp_path / "untrusted-mode", adapter=adapter,
                   decision_protocol="full_native_v1", max_context_chars=100000)
    assert result["status"] == "stopped_failure" and not inert.calls and len(sent) == 1


@pytest.mark.parametrize("with_history", [False, True])
def test_optional_metadata_index_omits_whole_envelope_when_base_prompt_fits(inert, tmp_path, with_history):
    producer = _metadata_checkpoint(inert, tmp_path) if with_history else None
    index, view, _ = _read_metadata_index(inert, tmp_path, resume_from=producer)
    wire = json.loads((tmp_path / "metadata-consumer/decision-000/model-input.json").read_text())
    base = {key: value for key, value in view.items() if key != "historical_metadata_query_index"}
    bound = len(wire["messages"][0]["content"]) + len(D._json(base)) + 8
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Retain historical query scopes"}])
    out = tmp_path / "near-context-bound"
    result = D.run(inert.root, out, adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "resource", "evidence"], resume_from=producer,
                   max_context_chars=bound)
    assert result["status"] == "model_stopped" and len(seen) == 1
    actual = json.loads(seen[0]["messages"][1]["content"])
    assert "historical_metadata_query_index" not in actual
    assert actual["prior_investigations"] == base["prior_investigations"]
    assert actual["artifacts"] == base["artifacts"]
    retained = json.loads((out / "redaction.json").read_text()) if (out / "redaction.json").exists() else json.loads((out / "decision-000/redaction.json").read_text())
    omitted = retained["historical_metadata_query_index"]
    assert omitted["status"] == "omitted_context_budget" and omitted["entries"] == omitted["json_chars"] == 0
    assert omitted["omitted_entries"] == len(index["entries"]) + index["omitted_entries"]
    assert sum(len(message["content"]) for message in seen[0]["messages"]) <= bound



def _appkit_run(inert, tmp_path, adapter, name='appkit', **kwargs):
    # These fixtures intentionally omit declared optional wire fields.
    adapter.config=replace(adapter.config,structured_output_mode='json_object')
    return D.run(inert.root,tmp_path/name,adapter=adapter,decision_protocol='full_native_v1',
                 allowed_tools=['discover','runtime','evidence'],discovery_playbook='objc_appkit_v1',
                 max_context_chars=100000,**kwargs)


def test_appkit_playbook_follows_actual_metadata_and_worker_evidence_without_automatic_calls(inert,tmp_path,monkeypatch):
    # Scripted replies verify actual view/evidence plumbing, not model ability.
    original=G.step
    worker_name='s0002/runtime/worker-output.json'
    worker_value={'operations':[{'op':'read','fields':[{'name':'document','value':
        {'class':'ReceiptCoordinator','expires':'transaction'},'metadata':{'selector':'currentDocument','signature':'@16@0:8'}}]}]}
    def step(root,request):
        if request['op']=='discover':
            operations=request['request']['operations']
            if operations[0]['op']=='classes':facts={'names':['LedgerApplication']}
            elif operations[0]['class']=='LedgerApplication':
                facts={'rows':[{'class':'LedgerApplication','selector':'currentDocument',
                                'dispatch':'-','signature':'@16@0:8'}]}
            else:
                facts={'rows':[{'class':'ReceiptCoordinator','selector':'commitRecord:','dispatch':'-',
                                'signature':'v24@0:8@16'}]}
            inert.payloads['discover']={'status':'completed','observation':facts,'evidence_only':True,'proof_created':False}
        elif request['op']=='runtime':
            inert.payloads['runtime']={'status':'completed','receipt':'s0002/runtime/runtime.json',
                                      'evidence_only':True,'proof_created':False}
        elif request['op']=='evidence':
            assert request['artifact']==worker_name
            file=inert.root/worker_name
            assert D._pin(file)==inert.state['artifacts'][worker_name]
            inert.payloads['evidence']={'status':'completed','artifact':worker_name,'text':file.read_text(),
                                       'evidence_only':True,'proof_created':False}
        value=original(root,request)
        if request['op']=='runtime':
            file=inert.root/worker_name;file.parent.mkdir();file.write_text(json.dumps(worker_value))
            inert.state['artifacts'][worker_name]=D._pin(file);inert.save()
            value['context']=G.context(root)
        return value
    monkeypatch.setattr(G,'step',step)
    requests=[{'op':'discover','provider':'native','request':{'operations':[
        {'op':'classes','query':'Ledger','owner':'app','limit':8}]}},
        {'op':'discover','provider':'native','request':{'operations':[
        {'op':'methods','class':'LedgerApplication','query':'currentDocument','own':False,'limit':8}]}},
        {'op':'runtime','provider':'native','request':{'operations':[{'op':'read',
        'receiver':{'class':'LedgerApplication','kind':'unique'},'fields':[{'name':'document','kind':'getter',
        'type':'object','selector':'currentDocument','signature':'@16@0:8','alias':'document'}]}]}},
        {'op':'evidence','artifact':worker_name},
        {'op':'discover','provider':'native','request':{'operations':[{'op':'methods','class':'ReceiptCoordinator',
        'query':'commit','own':False,'limit':32}]}}]
    adapter,seen=model([{'action':'step','request':request,'reason':'Inspect current fictional metadata'} for request in requests]+
                       [{'action':'stop','request':None,'reason':'Typed candidate still requires a measured receiver and proof'}])
    result=_appkit_run(inert,tmp_path,adapter)
    assert result['status']=='model_stopped' and result['tool_dispatches']==len(inert.calls)==5
    views=[json.loads(row['messages'][1]['content']) for row in seen]
    for view in views:
        assert view['native_guide']['discovery_playbook']==D.APPKIT_PLAYBOOK
        assert set(view['tools'])=={'discover','runtime','evidence'}
        assert all(secret not in json.dumps(view) for secret in ('OLD_RECIPE_PRIVATE','FRESH_ORACLE_PRIVATE','HELDOUT_ORACLE_PRIVATE'))
    assert views[1]['last_observation']['observation']['names']==['LedgerApplication']
    assert views[2]['last_observation']['observation']['rows'][0]['signature']=='@16@0:8'
    assert 'ReceiptCoordinator' not in json.dumps(views[3]['last_observation'])
    assert worker_name in views[3]['artifacts']
    assert 'ReceiptCoordinator' in views[4]['last_observation']['text']
    assert inert.calls[-1]['request']['operations'][0]['class']=='ReceiptCoordinator'
    assert inert.calls[-1]['request']['operations'][0]['own'] is False
    assert all(operation['op']!='invoke' for request in inert.calls if 'request' in request for operation in request['request']['operations'])
    assert result['discovery_playbook']=={'id':'objc_appkit_v1','sha256':G.R.digest(D.APPKIT_PLAYBOOK),
                                         'advice_only':True,'automatic_operations':0}
    manifest=G.R.load_json(tmp_path/'appkit/decision-000/redaction.json')
    assert manifest['discovery_playbook']==result['discovery_playbook']
    assert all('ReceiptCoordinator' not in json.dumps(row['native_guide']['discovery_playbook']) for row in views)


@pytest.mark.parametrize('mode',['legacy','static','full_native','inspector'])
def test_appkit_playbook_is_absent_from_default_and_other_provider_views(inert,tmp_path,mode):
    request={'op':'runtime','provider':'inspector','request':{'operations':[{'op':'read','recipe':
        {'root':{'kind':'global','name':'fixture'},'steps':[]}}]}}
    replies=([decision(request),decision()] if mode=='inspector' else
             [static_decision()] if mode=='static' else
             [{'action':'stop','request':None,'reason':'No platform playbook was selected'}] if mode=='full_native' else [decision()])
    adapter,seen=model(replies)
    options={'static_only':True} if mode=='static' else {'decision_protocol':'full_native_v1'} if mode=='full_native' else {}
    result=D.run(inert.root,tmp_path/mode,adapter=adapter,max_context_chars=100000,**options)
    assert result['status']=='model_stopped' and 'discovery_playbook' not in result
    for row in seen:
        view=json.loads(row['messages'][1]['content'])
        assert 'discovery_playbook' not in view.get('native_guide',{})
        if mode!='static':assert view['native_guide']==G.native_guide(view['tools'])
    if mode=='inspector':assert inert.calls[0]['provider']=='inspector'


@pytest.mark.parametrize('options',[{'discovery_playbook':'unknown'}, {'discovery_playbook':True},
    {'discovery_playbook':'objc_appkit_v1'},
    {'discovery_playbook':'objc_appkit_v1','static_only':True},
    {'discovery_playbook':'objc_appkit_v1','decision_protocol':'full_native_v1','allowed_tools':['discover','evidence']}])
def test_appkit_playbook_requires_explicit_compatible_scope_before_outputs(inert,tmp_path,options):
    adapter,seen=model([decision()]);out=tmp_path/'refused'
    with pytest.raises(ValueError):D.run(inert.root,out,adapter=adapter,**options)
    assert not out.exists() and not seen and not inert.calls


@pytest.mark.parametrize('failure',['nil','ambiguous','unsupported','cleanup'])
def test_appkit_advice_does_not_retry_failed_getters_or_clear_cleanup(inert,tmp_path,failure):
    inert.payloads['runtime']={'status':'resource_review_required' if failure=='cleanup' else 'failed',
        'error':{'message':failure},'evidence_only':True,'proof_created':False}
    request={'op':'runtime','provider':'native','request':{'operations':[{'op':'read',
        'receiver':{'class':'FictionalDocument','kind':'unique'},'fields':[{'name':'child','kind':'getter',
        'selector':'child','signature':'@16@0:8','type':'object','alias':'child'}]}]}}
    adapter,seen=model([{'action':'step','request':request,'reason':'Inspect bounded fictional receiver'},
                        {'action':'stop','request':None,'reason':'No retry'}])
    result=_appkit_run(inert,tmp_path,adapter)
    assert result['status']=='stopped_failure' and result['model_calls']==len(seen)==1
    assert result['tool_dispatches']==len(inert.calls)==1 and result['decisions'][0]['status']=='stopped_failure'


@pytest.mark.parametrize('tamper',[False,True])
def test_appkit_resume_reuses_pinned_evidence_and_rejects_changed_playbook_input(inert,tmp_path,tamper):
    request={'op':'discover','provider':'native','request':{'operations':[
        {'op':'classes','query':'Invoice','owner':'app','limit':8}]}}
    inert.payloads['discover']={'status':'completed','observation':{'names':['InvoiceDocument']},'proof_created':False}
    producer,_=model([{'action':'step','request':request,'reason':'Inspect actual fictional class metadata'}])
    first=_appkit_run(inert,tmp_path,producer,name='producer',max_decisions=1)
    assert first['status']=='decision_budget_exhausted' and len(inert.calls)==1
    if tamper:
        file=tmp_path/'producer/decision-000/model-input.json'
        value=G.R.load_json(file);view=json.loads(value['messages'][1]['content'])
        view['native_guide']['discovery_playbook']['steps'][0]='changed producer advice'
        value['messages'][1]['content']=json.dumps(view);file.write_text(json.dumps(value))
    reader,seen=model([{'action':'stop','request':None,'reason':'Retain historical evidence without new native actions'}])
    result=_appkit_run(inert,tmp_path,reader,name='consumer',resume_from=tmp_path/'producer')
    assert len(inert.calls)==1
    if tamper:
        assert result['status']=='stopped_failure' and not seen and result['model_calls']==0
    else:
        assert result['status']=='model_stopped' and result['investigation_memory']['restored_turns']==1
        view=json.loads(seen[0]['messages'][1]['content'])
        assert view['native_guide']['discovery_playbook']==D.APPKIT_PLAYBOOK
        assert view['last_observation'] is None and view['prior_investigations'][0]['op']=='discover'
        assert view['historical_metadata_query_index']['entries'][0]['request']['query']=='Invoice'
        assert result['tool_dispatches']==0


def test_appkit_playbook_source_drift_stops_before_dispatch(inert,tmp_path,monkeypatch):
    changed=[False];original=D._pin
    def pin(file):
        value=original(file)
        if changed[0] and Path(file)==Path(D.__file__).resolve():value['sha256']='f'*64
        return value
    monkeypatch.setattr(D,'_pin',pin)
    request={'op':'discover','provider':'native','request':{'operations':[{'op':'classes','query':'Invoice','owner':'app','limit':8}]}}
    adapter,seen=model([{'action':'step','request':request,'reason':'No operation after policy source drift'}],
                       hook=lambda:changed.__setitem__(0,True))
    result=_appkit_run(inert,tmp_path,adapter)
    assert result['status']=='stopped_failure' and len(seen)==1 and not inert.calls
    assert result['tool_dispatches']==0
    assert result['error']['message']=='memory policy or frozen task changed; no dispatch'


@pytest.mark.parametrize("playbook,protocol", [("objc_appkit_v1", "full_native_v1"), ("inspector_v1", "request_json_v1"), ("inspector_v1", "inspector_structured_v1")])
def test_discovery_cli_forwards_only_explicit_advisory_option(inert,tmp_path,monkeypatch,playbook,protocol):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    calls=[]
    def run(*args,**kwargs):calls.append((args,kwargs));return {'status':'model_stopped'}
    monkeypatch.setattr(D,'run',run)
    result=CliRunner().invoke(rea_commands,['agent','run',str(inert.root),'--out',str(tmp_path/'out'),
        '--decision-protocol',protocol,'--discovery-playbook',playbook])
    assert result.exit_code==0,result.output
    assert len(calls)==1 and calls[0][1]['discovery_playbook']==playbook
    assert calls[0][1]['decision_protocol']==protocol and not inert.calls



def test_appkit_advice_obeys_context_bound_without_silent_omission(inert,tmp_path):
    adapter,seen=model([{'action':'stop','request':None,'reason':'No context budget extension'}])
    result=D.run(inert.root,tmp_path/'bounded-appkit',adapter=adapter,decision_protocol='full_native_v1',
        allowed_tools=['discover','runtime','evidence'],discovery_playbook='objc_appkit_v1',max_context_chars=4096)
    assert result['status']=='stopped_failure' and result['model_calls']==0 and not seen and not inert.calls
    assert 'context exceeds bound' in result['error']['message']
    retained=G.R.load_json(tmp_path/'bounded-appkit/decision-000/model-input.json')
    assert json.loads(retained['messages'][1]['content'])['native_guide']['discovery_playbook']==D.APPKIT_PLAYBOOK


@pytest.mark.parametrize("controller,task,queries", [
    ("..SyntheticWriter.DocumentOwner", "Resize a document selection", ["resize", "document", "selection"]),
    ("SyntheticComposer.ScoreOwner", "Annotate a score measure", ["annotate", "score", "measure"]),
])
def test_frontier_separates_scaffold_class_search_from_unqueried_receiver_methods(
        receiver_runtime, tmp_path, controller, task, queries):
    root, control = receiver_runtime
    state = G.R.load_json(root / "session.json")
    state.update(task=task, max_steps=64)
    G.R.write_json(root / "session.json", state)
    control["returned_classes"] = {"windowController": controller}
    G.step(root, {"op": "evidence", "artifact": "inventory.json", "limit": 128})
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Task methods are still unqueried."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "frontier", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   discovery_class_queries=queries, max_decisions=24, timeout_seconds=900,
                   max_context_chars=100000)
    assert result["status"] == "model_stopped" and result["model_calls"] == len(seen) == 1
    view = json.loads(seen[0]["messages"][1]["content"])
    frontier = view["decision_protocol"]["investigation_frontier"]
    assert frontier["receiver"]["class"] == controller
    assert frontier["observer_contract_status"] == "caller_approved_independent_declaration"
    assert frontier["metadata"]["current_receiver_method_scope"] == "unqueried"
    assert frontier["metadata"]["class_name_query_scopes"] == 4
    assert frontier["metadata"]["method_query_scopes"] == 3
    assert all(scope.get("class") != controller for scope in frontier["metadata"]["scopes"])
    budgets = frontier["budgets"]
    assert (budgets["G_steps_used"], budgets["G_steps_maximum"], budgets["G_steps_remaining"]) == (9, 64, 55)
    assert budgets["scaffold_G_steps"] == 8 and budgets["driver_decisions_remaining"] == 24
    assert budgets["driver_seconds_remaining"] == view["driver_bounds"]["seconds_remaining"] > 800
    assert budgets["G_seconds_remaining"] == view["seconds_remaining"] > 0
    example = frontier["optional_metadata_example"]
    request = D.P.normalize_request(example, view)
    assert request["request"]["operations"] == [{"op": "methods", "class": controller, "query": "", "own": False, "limit": 32}]
    assert "STOP remains legal" in frontier["stop"]
    assert len(D._json(frontier)) <= 6000
    index = RP.validate(result["discovery_routine"], root)
    assert frontier["receiver"]["recipe"] == index["observations"][-1]["recipe"]
    assert frontier["receiver"]["recipe"]["kind"] == "class"
    for actual, measured in zip(frontier["receiver"]["hops"], index["observations"]):
        assert actual["metadata_citation"]["pointer"] == measured["metadata_citation"]["pointer"]
        assert actual["observation_citation"]["pointer"] == measured["observation_citation"]["pointer"]
    assert not any(key in D._json(frontier) for key in ('"actor"', '"handle"'))
    assert all(word not in D._json(frontier).lower() for word in ("pause", "play", "video"))
    assert result["discovery_routine"]["native_calls"] == 6
    assert len(control["calls"]) == 8 and not any(op["op"] == "invoke" for call in control["calls"] for op in call["operations"])


@pytest.mark.parametrize("summary,current,status", [
    ({"status": "ok", "matched": 0, "truncated": False}, True, "complete"),
    ({"status": "ok", "matched": 80, "truncated": True}, True, "truncated"),
    ({"status": "failed"}, True, "failed"),
    ({"status": "ok", "matched": 0}, True, "unknown"),
    (None, True, "unknown"),
    ({"status": "ok", "matched": 0, "truncated": False}, False, "unknown"),
])
def test_frontier_query_status_is_exact_scope_metadata_not_class_exhaustion(summary, current, status):
    operation = {"op": "methods", "class": ".Observed.Owner", "query": "literal", "own": True, "limit": 16}
    scope = D._metadata_scope(operation, summary, current_process=current)
    assert {key: scope[key] for key in ("provider", "class", "query", "own", "limit")} == {
        "provider": "native", "class": ".Observed.Owner", "query": "literal", "own": True, "limit": 16}
    assert scope["status"] == status and "exhausted" not in scope and "actor" not in scope


def test_frontier_recognizes_runtime_metadata_query_without_inventing_missing_summary(receiver_runtime, tmp_path):
    root, control = receiver_runtime
    operation = {"op": "methods", "class": "ReceiptController", "query": "entry", "own": False, "limit": 32}
    request = {"op": "runtime", "provider": "native", "request": {"operations": [operation]}}
    adapter, seen = model([{"action": "step", "request": request, "reason": "Inspect bounded receiver metadata."},
                           {"action": "stop", "request": None, "reason": "The query ran; its result summary remains unknown."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "runtime-frontier", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   max_context_chars=100000)
    assert result["status"] == "model_stopped" and len(seen) == 2
    first = json.loads(seen[0]["messages"][1]["content"])["decision_protocol"]["investigation_frontier"]
    final = json.loads(seen[1]["messages"][1]["content"])["decision_protocol"]["investigation_frontier"]
    assert first["metadata"]["current_receiver_method_scope"] == "unqueried"
    assert "optional_metadata_example" in first
    assert final["metadata"]["current_receiver_method_scope"] == "queried_metadata_only"
    assert "optional_metadata_example" not in final
    scope = final["metadata"]["scopes"][-1]
    assert {key: scope[key] for key in ("provider", "op", "class", "query", "own", "limit")} == {"provider": "native", **operation}
    assert scope["status"] == "unknown" and scope["process_binding"] == "current_receiver_epoch"
    assert "matched" not in scope and "truncated" not in scope
    assert len(control["calls"]) == 8 and control["calls"][-1] == request["request"]
    assert result["discovery_routine"]["native_calls"] == 6


def test_frontier_opted_out_wire_has_no_added_guidance_or_dispatch(inert, tmp_path):
    adapter, seen = model([{"action": "stop", "request": None, "reason": "No optional guidance requested."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(inert.root, tmp_path / "no-frontier", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], max_context_chars=100000)
    view = json.loads(seen[0]["messages"][1]["content"])
    assert "investigation_frontier" not in view["decision_protocol"]
    assert result["status"] == "model_stopped" and result["tool_dispatches"] == 0 and not inert.calls


def test_frontier_bounds_query_summary_and_does_not_guess_a_receiver(inert, tmp_path):
    operations = [{"op": "classes", "query": f"Scope{index}", "owner": "app", "limit": 8} for index in range(10)]
    request = {"op": "discover", "provider": "native", "request": {"operations": operations}}
    inert.payloads["discover"] = {"status": "completed", "observation": {"operations": [
        {"index": index, "op": "classes", "status": "ok", "matched": 0, "truncated": False} for index in range(10)]}}
    adapter, seen = model([{"action": "step", "request": request, "reason": "Collect bounded class metadata."},
                           {"action": "stop", "request": None, "reason": "No receiver was measured."}])
    result = _appkit_run(inert, tmp_path, adapter)
    view = json.loads(seen[-1]["messages"][1]["content"])
    frontier = view["decision_protocol"]["investigation_frontier"]
    assert result["status"] == "model_stopped" and len(inert.calls) == 1
    assert frontier["receiver"] is None and "optional_metadata_example" not in frontier
    assert frontier["metadata"]["class_name_query_scopes"] == 10
    assert len(frontier["metadata"]["scopes"]) == 8 and frontier["metadata"]["omitted_scopes"] == 2
    assert all(scope["status"] == "unknown" for scope in frontier["metadata"]["scopes"])
    assert frontier["metadata"]["current_receiver_method_scope"] == "unknown_receiver"
    assert frontier["budgets"]["G_steps_remaining"] is None  # This inert context supplies no G budget.


def test_frontier_stale_registered_metadata_stops_before_model(receiver_runtime, tmp_path, monkeypatch):
    root, control = receiver_runtime
    original = RP.validate
    def stale(result, session):
        index = original(result, session)
        if not (root / "tampered").exists():
            state = G.R.load_json(root / "session.json")
            feedback = Path(state["artifacts"]["s0000/feedback.json"]["file"])
            feedback.write_text("{}")
            (root / "tampered").write_text("inert fixture")
        return index
    monkeypatch.setattr(RP, "validate", stale)
    adapter, seen = model([{"action": "stop", "request": None, "reason": "No stale suggestion."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(root, tmp_path / "stale-frontier", adapter=adapter, decision_protocol="full_native_v1",
                   allowed_tools=["discover", "runtime", "evidence"], discovery_routine=RP.ID,
                   max_context_chars=100000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert len(control["calls"]) == 7


@pytest.fixture
def actor_evidence_session(native_metadata_bridge_runtime, session, monkeypatch, tmp_path):
    """Real bridge/worker receipts; only process spawning and Frida are inert."""
    runtime = native_metadata_bridge_runtime
    # The ordinary profile fixture installs an older harness adapter stub.
    # These tests exercise the production bridge and its real pinned adapter.
    monkeypatch.setattr(B, "adapter_identity", _NATIVE_ADAPTER_IDENTITY)
    app, identity, _, control, _ = runtime
    old, profile, plan = session
    old_state = G.R.load_json(old / "session.json")
    root = tmp_path / "actor-facts-session"
    G.begin(str(app), old_state["rea"], old_state["task"], old_state["params"],
            old_state["success_criteria"], plan, root,
            observer_profile={k: v for k, v in profile.items() if k != "evidence"})
    facts = {"root_class": "Document.WorkspaceHub", "leaf_class": "Document.RenderCoordinator",
             "getter": "currentDocument", "selector": "applyLayout", "runs": [],
             "module": str(app / "Contents/MacOS/Canvas"), "negative_class": None}

    def bounded_run(argv, output, error, *, timeout, cwd):
        config_path = Path(argv[-1])
        config = G.R.load_json(config_path)
        rows, count = [], 0
        negative = any(op.get("class") == facts["negative_class"]
                       for op in config["request"]["operations"]) if facts["negative_class"] else False
        for index, op in enumerate(config["request"]["operations"]):
            if negative:
                break
            row = {"index": index, "op": op["op"], "status": "ok"}
            if op["op"] == "methods":
                getter = op.get("query") == facts["getter"]
                row.update(matched=1, truncated=False, rows=[{
                    "class": op["class"], "selector": op["query"],
                    "dispatch": "+" if op["class"] == facts["root_class"] else "-",
                    "signature": "@16@0:8" if getter else facts.get("signature", "v16@0:8"),
                    "module": facts["module"], "origin": "module_backed",
                    "implementation_offset": "0x20"}])
                repeat = facts.get("metadata_repeat", 1)
                if repeat > 1:
                    row["rows"] = [{**row["rows"][0], "selector": op["query"] + str(i)} for i in range(repeat)]
                    row["matched"] = repeat
            elif op["op"] == "instances":
                row["object"] = {"class": op["class"], "handle": "h0", "alias": op["alias"],
                                 "expires": "transaction"}
            elif op["op"] == "read":
                field = op["fields"][0]
                count += 1
                row["fields"] = [{"name": field["name"], "kind": "getter", "type": "object",
                    "value": {"class": facts["leaf_class"], "handle": "h0", "alias": field["alias"],
                              "expires": "transaction"},
                    "metadata": {"class": facts["root_class"], "selector": field["selector"],
                        "dispatch": "+", "signature": "@16@0:8", "module": facts["module"],
                        "origin": "module_backed", "implementation_offset": "0x20",
                        "return_type": "object", "argument_types": []}}]
            else:
                raise AssertionError("fixture permits metadata and one declared getter only")
            rows.append(row)
        observed = {"status": "incomplete" if negative else "completed", "operations": rows,
                    "native_calls": count, "invocations": 0, "helper_load_attempts": 0,
                    "error": {"type": "Error", "message": "exact Objective-C class is unavailable: " + facts["negative_class"]} if negative else None,
                    "native_state_uncertain": False, "handles_expired": True}
        local_runtime = (*runtime[:4], Path(cwd) / "inert-worker")
        worker, activity, _, _ = _worker_fixture(local_runtime, monkeypatch,
            request=config["request"], worker_config_path=config_path, observed_changes=observed)
        facts["runs"].append({"worker": worker, "activity": activity})
        G.R.write_json(output, worker)
        Path(error).write_text("")
        return {"returncode": 1 if negative else 0, "timed_out": False, "elapsed_s": .001,
                "output": Path(output).name, "sha256": G.R._file_hash(Path(output)),
                "stderr": Path(error).name}

    monkeypatch.setattr(G.R, "_bounded_run", bounded_run)

    def prepare(*, unique=False):
        G.step(root, {"op": "discover", "provider": "native", "request": {"operations": [
            {"op": "methods", "class": facts["root_class"], "query": facts["getter"]},
            {"op": "methods", "class": facts["leaf_class"], "query": facts["selector"]},
            {"op": "methods", "class": facts["root_class"], "query": facts["selector"]}]}})
        G.step(root, {"op": "runtime", "provider": "native", "request": {"operations": [
            {"op": "read", "receiver": {"class": facts["root_class"], "kind": "class"},
             "fields": [{"name": "document", "kind": "getter", "type": "object",
                         "selector": facts["getter"], "signature": "@16@0:8", "alias": "document"}]}]}})
        if unique:
            G.step(root, {"op": "discover", "provider": "native", "request": {"operations": [
                {"op": "instances", "class": facts["leaf_class"], "alias": "document"}]}})
        state = G.R.load_json(root / "session.json")
        assert all(f"s{index:04d}/runtime/worker-output.json" in state["artifacts"]
                   for index in range(3 if unique else 2)), state["steps"]
        assert all(step["feedback"].get("error") is None for step in state["steps"])

    def proposal_request(kind="path", *, unique_citation=True):
        def citation(step, pointer):
            return {"artifact": f"s{step:04d}/runtime/worker-output.json", "pointer": pointer}
        if kind == "class":
            receiver = {"kind": "class", "class": facts["root_class"]}
            citations = [citation(0, "/operations/2/rows/0")]
        elif kind == "unique":
            receiver = {"kind": "unique", "class": facts["leaf_class"]}
            citations = [citation(0, "/operations/1/rows/0")]
            if unique_citation:
                citations.append(citation(2, "/operations/0/object"))
        else:
            receiver = {"kind": "class", "class": facts["root_class"], "path": [
                {"selector": facts["getter"], "signature": "@16@0:8"}]}
            citations = [citation(0, "/operations/0/rows/0"), citation(0, "/operations/1/rows/0"),
                         citation(1, "/operations/0/fields/0/value/class")]
        state = G.R.load_json(root / "session.json")
        return {"op": "propose", "rationale": "A measured candidate; effect remains unproved.",
                "actor": {"kind": "objc_method", "receiver": receiver, "selector": facts["selector"],
                          "signature": facts.get("signature", "v16@0:8"),
                          "arguments": copy.deepcopy(facts.get("arguments", [])), "return_type": "void"},
                "citations": citations, "observer_profile_sha256": state["observer_profile_sha256"]}
    return SimpleNamespace(root=root, facts=facts, prepare=prepare, request=proposal_request,
                           identity=identity, runtime=runtime, control=control)


@pytest.mark.parametrize("kind", ["class", "path", "unique"])
@pytest.mark.parametrize("namespace", ["Document", ".Alternate.Vector"])
def test_actor_evidence_accepts_measured_recipes_without_task_or_app_rules(actor_evidence_session, kind, namespace):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.facts.update(root_class=namespace + ".Hub", leaf_class=namespace + ".Coordinator",
                      module="/System/Library/Frameworks/SyntheticKit.framework/SyntheticKit")
    case.prepare(unique=kind == "unique")
    state = G.R.load_json(case.root / "session.json")
    request = case.request(kind)
    before = (case.root / "session.json").read_bytes()
    count = len(case.facts["runs"])
    binding = E.preflight(case.root, state, request)
    E.revalidate(binding, case.root, state, request)
    assert (case.root / "session.json").read_bytes() == before
    assert len(case.facts["runs"]) == count
    assert not state.get("hypothesis") and not state.get("proof")
    assert sum(row["worker"]["invocations"] for row in case.facts["runs"]) == 0


def test_actor_evidence_returns_uncited_unique_fact_without_mutating_hypothesis(actor_evidence_session):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare(unique=True)
    state = G.R.load_json(case.root / "session.json")
    before = (case.root / "session.json").read_bytes()
    with pytest.raises(E.MissingActorEvidence) as caught:
        E.preflight(case.root, state, case.request("unique", unique_citation=False))
    assert "unique" in str(caught.value).lower()
    E.revalidate(caught.value.binding, case.root, state, case.request("unique", unique_citation=False))
    assert (case.root / "session.json").read_bytes() == before


def test_actor_evidence_preserves_declared_typed_parameter_bindings(actor_evidence_session):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.facts.update(selector="setLayout:", signature="v24@0:8@16",
                      arguments=[{"type": "utf8", "value": {"input": "query"}}])
    case.prepare()
    state = G.R.load_json(case.root / "session.json")
    request = case.request()
    binding = E.preflight(case.root, state, request)
    E.revalidate(binding, case.root, state, request)
    assert request["actor"]["arguments"] == [{"type": "utf8", "value": {"input": "query"}}]
    assert len(case.facts["runs"]) == 2 and not state.get("proof")


def test_actor_evidence_requires_observed_getter_return_not_only_getter_metadata(actor_evidence_session):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare()
    request = case.request()
    request["citations"] = request["citations"][:2]
    state = G.R.load_json(case.root / "session.json")
    with pytest.raises(E.MissingActorEvidence):
        E.preflight(case.root, state, request)
    assert state["status"] == "investigating" and not state.get("hypothesis")


@pytest.mark.parametrize("cite_negative", [False, True])
def test_actor_evidence_preserves_clean_negative_metadata_without_positive_facts(actor_evidence_session, cite_negative):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare()
    case.facts["negative_class"] = "Unobserved.MetadataCandidate"
    result = G.step(case.root, {"op": "discover", "provider": "native", "request": {"operations": [
        {"op": "methods", "class": case.facts["negative_class"], "query": "apply"}]}})
    assert result["feedback"]["feedback"]["status"] == "negative_metadata_observation"
    state = G.R.load_json(case.root / "session.json")
    request = case.request("unique", unique_citation=False) if cite_negative else case.request()
    if cite_negative:
        request["citations"].append({"artifact": "s0002/runtime/worker-output.json", "pointer": "/error"})
        with pytest.raises(E.MissingActorEvidence):
            E.preflight(case.root, state, request)
    else:
        binding = E.preflight(case.root, state, request)
        E.revalidate(binding, case.root, state, request)
    assert len(case.facts["runs"]) == 3
    assert case.facts["runs"][-1]["worker"]["native_calls"] == 0
    assert state["status"] == "investigating" and not state.get("proof")


@pytest.mark.parametrize("tamper", ["worker_bytes", "epoch", "adapter"])
def test_actor_evidence_revalidation_stops_on_changed_evidence_or_runtime(actor_evidence_session, monkeypatch, tamper):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare()
    state = G.R.load_json(case.root / "session.json")
    request = case.request()
    binding = E.preflight(case.root, state, request)
    if tamper == "worker_bytes":
        file = case.root / "s0001/runtime/worker-output.json"
        file.write_bytes(file.read_bytes() + b" ")
    elif tamper == "epoch":
        monkeypatch.setattr(B, "process_identity", lambda *args: {**case.identity, "launch_time": 999.0})
    else:
        case.runtime[2].write_text("changed compiled adapter")
    with pytest.raises(E.EvidenceIntegrityError):
        E.revalidate(binding, case.root, state, request)
    assert G.R.load_json(case.root / "session.json")["status"] == "investigating"


def test_actor_evidence_refuses_duplicate_json_even_with_refreshed_registration_pins(actor_evidence_session):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare()
    state = G.R.load_json(case.root / "session.json")
    worker_file = case.root / "s0000/runtime/worker-output.json"
    original = worker_file.read_bytes()
    worker_file.write_bytes(b'{"status":"completed",' + original.lstrip()[1:])
    worker_pin = D._pin(worker_file)
    runtime_file = case.root / "s0000/runtime/runtime.json"
    receipt = G.R.load_json(runtime_file)
    receipt["worker"]["sha256"] = worker_pin["sha256"]
    for row in receipt.get("artifacts", []):
        if row.get("file") == "worker-output.json":
            row.update(bytes=worker_pin["bytes"], sha256=worker_pin["sha256"])
    G.R.write_json(runtime_file, receipt)
    feedback_file = case.root / "s0000/feedback.json"
    source = state["steps"][0]["feedback"].get("observation", {}).get("source")
    if source is not None:
        source["sha256"] = worker_pin["sha256"]
    G.R.write_json(feedback_file, state["steps"][0])
    for file in (worker_file, runtime_file, feedback_file):
        state["artifacts"][file.relative_to(case.root).as_posix()] = D._pin(file)
    G.R.write_json(case.root / "session.json", state)
    with pytest.raises(E.EvidenceIntegrityError, match="duplicate"):
        E.preflight(case.root, state, case.request())
    assert state["status"] == "investigating" and len(case.facts["runs"]) == 2


@pytest.mark.parametrize("replacement", ["symlink", "oversized"])
def test_actor_evidence_refuses_noncanonical_or_oversized_registered_input(actor_evidence_session, replacement, tmp_path):
    from anyuse.execution import rea_actor_evidence as E
    case = actor_evidence_session
    case.prepare()
    state = G.R.load_json(case.root / "session.json")
    worker_file = case.root / "s0000/runtime/worker-output.json"
    if replacement == "symlink":
        target = tmp_path / "other-observation.json"
        target.write_bytes(worker_file.read_bytes())
        worker_file.unlink()
        worker_file.symlink_to(target)
    else:
        worker_file.write_bytes(b" " * (E.MAX_FILE_BYTES + 1))
    with pytest.raises(E.EvidenceIntegrityError):
        E.preflight(case.root, state, case.request())
    assert state["status"] == "investigating" and len(case.facts["runs"]) == 2


def test_driver_corrects_missing_receiver_fact_with_real_metadata_before_proposal(actor_evidence_session, tmp_path):
    case = actor_evidence_session
    case.prepare()
    missing = case.request("unique", unique_citation=False)
    query = {"op": "discover", "provider": "native", "request": {"operations": [
        {"op": "instances", "class": case.facts["leaf_class"], "alias": "document"}]}}
    accepted = case.request("unique")
    replies = [{"action": "step", "request": request, "reason": "Use measured evidence; effect remains unproved."}
               for request in (missing, query, accepted)]
    adapter, seen = model(replies)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(case.root, tmp_path / "correct-actor-facts", adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence", "propose"],
        proposal_evidence="native_objc_v1", max_decision_corrections=1, max_context_chars=150000)
    assert result["status"] == "boundary_reached" and result["model_calls"] == len(seen) == 3
    assert result["decision_corrections"]["used"] == 1
    assert [row["status"] for row in result["decisions"]] == ["decision_rejected", "tool_completed", "tool_completed"]
    assert result["decisions"][0]["tool_dispatched"] is False
    state = G.R.load_json(case.root / "session.json")
    assert state["status"] == "hypothesis" and state["hypothesis"]["actor"] == accepted["actor"]
    assert [step["request"]["op"] for step in state["steps"]] == ["discover", "runtime", "discover", "propose"]
    assert all(row["worker"]["invocations"] == 0 for row in case.facts["runs"])
    assert not state.get("proof") and not state.get("recordings")


def test_frontier_method_rows_cite_raw_registered_artifact_not_feedback(actor_evidence_session, tmp_path):
    case = actor_evidence_session
    case.prepare()
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Metadata still needs independent task proof."}])
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(case.root, tmp_path / "raw-method-citations", adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence"],
        discovery_playbook="objc_appkit_v1", max_context_chars=150000)
    assert result["status"] == "model_stopped" and len(seen) == 1
    frontier = json.loads(seen[0]["messages"][1]["content"])["decision_protocol"]["investigation_frontier"]
    rows = frontier["metadata"]["method_rows"]
    assert len(rows) == 3 and frontier["metadata"]["omitted_method_rows"] == 0
    state = G.R.load_json(case.root / "session.json")
    for row in rows:
        citation = row["citation"]
        assert citation["artifact"] == "s0000/runtime/worker-output.json"
        assert citation["sha256"] == state["artifacts"][citation["artifact"]]["sha256"]
        raw = G.R.load_json(case.root / citation["artifact"])
        for part in citation["pointer"].strip("/").split("/"):
            raw = raw[int(part)] if isinstance(raw, list) else raw[part]
        assert all(row[key] == raw[key] for key in ("class", "selector", "signature", "dispatch", "origin"))
        assert "arguments" not in row and "actor" not in row
    assert len(D._json(frontier)) <= 6000 and len(case.facts["runs"]) == 2


def test_frontier_bounds_whole_method_rows_with_explicit_omissions(actor_evidence_session, tmp_path):
    case = actor_evidence_session
    case.facts["metadata_repeat"] = 20
    case.prepare()
    adapter, seen = model([{"action": "stop", "request": None, "reason": "Omitted metadata remains unknown."}])
    result = D.run(case.root, tmp_path / "bounded-method-citations", adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence"],
        discovery_playbook="objc_appkit_v1", max_context_chars=150000)
    assert result["status"] == "model_stopped" and len(seen) == 1
    frontier = json.loads(seen[0]["messages"][1]["content"])["decision_protocol"]["investigation_frontier"]
    assert len(D._json(frontier)) <= 6000
    rows = frontier["metadata"]["method_rows"]
    assert 0 < len(rows) <= 8 and frontier["metadata"]["omitted_method_rows"] > 0
    assert all(set(row["citation"]) == {"artifact", "sha256", "pointer"} for row in rows)
    assert not G.R.load_json(case.root / "session.json").get("hypothesis")


@pytest.mark.parametrize("rejected", [False, True])
def test_frontier_raw_metadata_change_stops_dispatch_and_correction(actor_evidence_session, tmp_path, rejected):
    case = actor_evidence_session
    case.prepare()
    before = (case.root / "session.json").read_bytes()
    def changed_raw():
        file = case.root / "s0000/runtime/worker-output.json"
        file.write_bytes(file.read_bytes() + b" ")
    reply = {"action": "invalid_action" if rejected else "stop", "request": None,
             "reason": "Only unchanged registered evidence may support another decision."}
    adapter, seen = model([reply], hook=changed_raw)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(case.root, tmp_path / ("reject-drift" if rejected else "stop-drift"), adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence"],
        discovery_playbook="objc_appkit_v1", max_decision_corrections=1, max_context_chars=150000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == len(seen) == 1
    assert result["decision_corrections"]["used"] == 0
    assert result["tool_dispatches"] == 0 and len(case.facts["runs"]) == 2
    assert (case.root / "session.json").read_bytes() == before


def test_frontier_refuses_projection_that_disagrees_with_registered_raw_row(actor_evidence_session, tmp_path):
    case = actor_evidence_session
    case.prepare()
    state = G.R.load_json(case.root / "session.json")
    step = state["steps"][0]
    step["feedback"]["observation"]["operations"][0]["rows"][0]["selector"] = "inventedGetter"
    file = case.root / "s0000/feedback.json"
    G.R.write_json(file, step)
    state["artifacts"]["s0000/feedback.json"] = D._pin(file)
    G.R.write_json(case.root / "session.json", state)
    adapter, seen = model([{"action": "stop", "request": None, "reason": "No invented projection."}])
    result = D.run(case.root, tmp_path / "projection-drift", adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence"],
        discovery_playbook="objc_appkit_v1", max_context_chars=150000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == 0 and not seen
    assert result["tool_dispatches"] == 0 and len(case.facts["runs"]) == 2


def test_driver_actor_evidence_epoch_change_is_terminal_not_a_correction(actor_evidence_session, monkeypatch, tmp_path):
    case = actor_evidence_session
    case.prepare()
    before = (case.root / "session.json").read_bytes()
    reply = {"action": "step", "request": case.request("unique", unique_citation=False),
             "reason": "Candidate remains unproved."}
    def changed_epoch():
        monkeypatch.setattr(B, "process_identity", lambda *args: {**case.identity, "launch_time": 999.0})
    adapter, seen = model([reply], hook=changed_epoch)
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(case.root, tmp_path / "changed-actor-epoch", adapter=adapter,
        decision_protocol="full_native_v1", allowed_tools=["discover", "runtime", "evidence", "propose"],
        proposal_evidence="native_objc_v1", max_decision_corrections=2, max_context_chars=150000)
    assert result["status"] == "stopped_failure" and result["model_calls"] == len(seen) == 1
    assert result["decision_corrections"]["used"] == 0
    assert (case.root / "session.json").read_bytes() == before
    assert len(case.facts["runs"]) == 2


def test_actor_evidence_is_default_off_without_touching_default_wire(inert, tmp_path, monkeypatch):
    from anyuse.execution import rea_actor_evidence as E
    def forbidden(*args, **kwargs):
        raise AssertionError("Unselected evidence preflight must not run")
    monkeypatch.setattr(E, "preflight", forbidden)
    adapter, seen = model([{"action": "stop", "request": None, "reason": "No operation selected."}])
    result = D.run(inert.root, tmp_path / "ordinary-native", adapter=adapter,
                   decision_protocol="full_native_v1", max_context_chars=100000)
    assert result["status"] == "model_stopped" and len(seen) == 1
    assert not inert.calls
    assert not any(Path(row["file"]).name == "rea_actor_evidence.py" for row in result["sources"])


@pytest.mark.parametrize("options", [
    {"proposal_evidence": "unknown"},
    {"proposal_evidence": "native_objc_v1"},
    {"proposal_evidence": "native_objc_v1", "decision_protocol": "full_native_v1", "allowed_tools": ["evidence"]},
])
def test_actor_evidence_scope_refuses_before_output_or_model(inert, tmp_path, options):
    adapter, seen = model([decision()])
    out = tmp_path / "invalid-actor-preflight"
    with pytest.raises(ValueError):
        D.run(inert.root, out, adapter=adapter, **options)
    assert not out.exists() and not seen and not inert.calls


def test_actor_evidence_cli_forwards_only_explicit_option(inert, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution.rea_cli import rea_commands
    calls = []
    monkeypatch.setattr(D, "run", lambda *args, **kwargs: calls.append(kwargs) or {"status": "model_stopped"})
    result = CliRunner().invoke(rea_commands, ["agent", "run", str(inert.root), "--out", str(tmp_path / "out"),
        "--decision-protocol", "full_native_v1", "--proposal-evidence", "native_objc_v1"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0]["proposal_evidence"] == "native_objc_v1"
    assert not inert.calls


@pytest.mark.parametrize("with_playbook", [False, True])
def test_structured_inspector_driver_uses_objects_and_leaves_default_native_route_unchanged(inert, tmp_path, with_playbook):
    answer = {"action": "stop", "request": None, "reason": "No current method observed yet."}
    adapter, seen = model([answer])
    result = D.run(inert.root, tmp_path / "structured-inspector-stop", adapter=adapter,
        decision_protocol="inspector_structured_v1", **({"discovery_playbook": "inspector_v1"} if with_playbook else {}))
    assert result["status"] == "model_stopped" and result["model_calls"] == 1, result.get("error")
    view = json.loads(seen[0]["messages"][1]["content"])
    assert view["decision_protocol"]["format"] == "inspector_structured_v1"
    assert view["decision_protocol"]["request_format"] == "native JSON object"
    assert "request" in view["decision_protocol"]["schema"]["properties"]
    assert "request_json" not in view["decision_protocol"]["schema"]["properties"]
    assert "native_guide" not in view and view["caller_scope"]["runtime_provider"] == "inspector"
    assert {"discover", "declare_inspector"} <= set(view["tools"]) <= set(D.P.INSPECTOR_SUPPORTED_TOOLS)
    assert any(p["file"] == str(Path(D.P.__file__).resolve()) for p in result["sources"])
    assert not inert.calls


def test_structured_inspector_read_then_scope_index_keeps_exact_paging_request(inert, tmp_path):
    request = {"op": "discover", "provider": "inspector", "timeout": None,
               "request": {"operations": [{"op": "describe", "recipe": {"root": {"kind": "global", "name": "globalThis"}, "steps": []},
                    "max_properties": 8, "prototype_depth": 0, "offset": 16, "property_query": "value"}]}}
    # Nullable fields are intentionally omitted only in the declared compatibility mode.
    adapter, seen = model([{"action": "step", "request": request, "reason": "Inspect a bounded declared page."},
                          {"action": "stop", "request": None, "reason": "Read exact raw method evidence next."}])
    from dataclasses import replace
    adapter.config = replace(adapter.config, structured_output_mode="json_object")
    result = D.run(inert.root, tmp_path / "structured-inspector-index", adapter=adapter,
        decision_protocol="inspector_structured_v1", max_decisions=2, max_context_chars=64000)
    assert result["status"] == "model_stopped" and result["model_calls"] == 2, result.get("error")
    assert len(inert.calls) == 1 and inert.calls[0]["provider"] == "inspector"
    assert type(inert.calls[0]["timeout"]) is int and 1 <= inert.calls[0]["timeout"] <= 60
    view = json.loads(seen[1]["messages"][1]["content"])
    entries = view["historical_metadata_query_index"]["entries"]
    assert len(entries) == 1 and entries[0]["provider"] == "inspector"
    assert entries[0]["request"] == request["request"]["operations"][0]
    assert "historical" in view["historical_metadata_query_index"]["freshness"]


@pytest.mark.parametrize("tool_request", ["{op:'discover'}", {"op": "discover", "provider": "native", "request": {"operations": []}, "timeout": None}])
def test_structured_inspector_rejects_encoded_or_native_request_before_dispatch(inert, tmp_path, tool_request):
    adapter, _ = model([{"action": "step", "request": tool_request, "reason": "Rejected syntax is not app evidence."}])
    result = D.run(inert.root, tmp_path / "structured-inspector-invalid", adapter=adapter,
        decision_protocol="inspector_structured_v1")
    assert result["status"] == "stopped_failure" and result["tool_dispatches"] == 0 and not inert.calls


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def _inspector_run(inert, tmp_path, adapter, name='inspector-playbook', **options):
    return D.run(inert.root, tmp_path / name, adapter=adapter,
        allowed_tools=['discover', 'runtime', 'evidence', 'declare_inspector', 'propose'],
        decision_protocol='request_json_v1', discovery_playbook='inspector_v1', **options)


@pytest.mark.parametrize('protocol', ['request_json_v1', 'inspector_structured_v1'])
def test_inspector_view_distinguishes_app_resources_from_registered_session_evidence(inert, tmp_path, protocol):
    tools = ['resource', 'evidence', 'discover']
    original = {op: copy.deepcopy(G.TOOLS[op]) for op in tools}
    answer = decision() if protocol == 'request_json_v1' else {
        'action': 'stop', 'request': None, 'reason': 'Tool descriptions distinguish artifact and resource paths.'}
    adapter, seen = model([answer])
    result = D.run(inert.root, tmp_path / ('tool-meaning-' + protocol), adapter=adapter,
        allowed_tools=tools, decision_protocol=protocol,
        **({'discovery_playbook': 'inspector_v1'} if protocol == 'request_json_v1' else {}))
    assert result['status'] == 'model_stopped' and result['tool_dispatches'] == 0 and not inert.calls
    view = json.loads(seen[0]['messages'][1]['content'])
    for op in ('resource', 'evidence'):
        assert view['tools'][op]['description'] == D.STATIC_TOOL_DESCRIPTIONS[op]
    first = view['discovery_playbook']['steps'][0]
    assert 'resource.relative resolves only inside the installed app bundle' in first
    assert "{op:'evidence',artifact:'inventory.json'}" in first and 'never resource' in first
    assert 'actual names from artifacts' in first
    assert view['caller_scope']['runtime_provider'] == 'inspector'
    assert {op: G.TOOLS[op] for op in tools} == original
    default, default_seen = model([decision()])
    ordinary = D.run(inert.root, tmp_path / ('default-tool-meaning-' + protocol), adapter=default,
        allowed_tools=tools)
    assert ordinary['status'] == 'model_stopped' and ordinary['tool_dispatches'] == 0
    default_view = json.loads(default_seen[0]['messages'][1]['content'])
    assert default_view['tools'] == original
    assert 'discovery_playbook' not in default_view and 'caller_scope' not in default_view
    assert default_view['native_guide'] == G.native_guide(tools)


@pytest.mark.parametrize('spelling', ['inspector_v1', 'insp_v1'])
def test_inspector_playbook_projects_only_selected_provider_without_native_hints(inert, tmp_path, monkeypatch, spelling):
    original = G.context
    def context(root):
        value = original(root)
        value['capabilities'] = {'actors': ['objc_method', 'inspector_method'],
            'runtime_providers': [{'provider': 'native', 'discovery_operations': ['classes']},
                                  {'provider': 'inspector', 'discovery_operations': ['describe', 'method']}],
            'native_abi_routes': ['default'], 'compiler_adapter_families': ['native-family'],
            'runtime_field_sources': ['objc_ivar'], 'automatic_native_abi_recovery': False}
        return value
    monkeypatch.setattr(G, 'context', context)
    adapter, seen = model([decision(reason='No method has been observed; no task action.')])
    result = D.run(inert.root, tmp_path / spelling, adapter=adapter,
        allowed_tools=['discover', 'evidence', 'declare_inspector'],
        decision_protocol='request_json_v1', discovery_playbook=spelling)
    assert result['status'] == 'model_stopped' and result['tool_dispatches'] == 0 and not inert.calls
    assert len(seen) == result['model_calls'] == 1
    view = json.loads(seen[0]['messages'][1]['content'])
    assert 'native_guide' not in view
    assert view['discovery_playbook'] == D.INSPECTOR_PLAYBOOK
    assert view['caller_scope']['runtime_provider'] == 'inspector'
    assert view['caller_scope']['provider_fallback'] is False
    assert view['capabilities']['actors'] == ['inspector_method']
    assert [row['provider'] for row in view['capabilities']['runtime_providers']] == ['inspector']
    assert 'native_abi_routes' not in view['capabilities'] and 'runtime_field_sources' not in view['capabilities']
    assert all(example['provider'] == 'inspector' for example in view['tools']['discover']['examples'])
    assert all(name not in json.dumps(view['agent_instructions']) for name in ['NSApplication', 'windowController', 'MarkText', 'draw.io'])
    # The selected playbook is supplied once; the short instruction points to it.
    assert 'discovery_playbook.steps' in ' '.join(view['agent_instructions'])
    instructions = json.dumps([view['agent_instructions'], view['discovery_playbook']['steps']])
    assert 'next_offset' in instructions and 'property_query' in instructions and 'not discovery progress' in instructions
    assert 'model-declared' in instructions and 'untrusted data' in instructions
    assert len(seen[0]['messages'][0]['content']) + len(seen[0]['messages'][1]['content']) <= 32000
    assert result['discovery_playbook'] == {'id': spelling, 'sha256': G.R.digest(D.INSPECTOR_PLAYBOOK),
        'advice_only': False, 'automatic_operations': 0, 'runtime_provider': 'inspector'}
    manifest = G.R.load_json(tmp_path / spelling / 'decision-000/redaction.json')
    assert manifest['discovery_playbook'] == result['discovery_playbook']
    assert not any(secret in json.dumps(view) for secret in ['OLD_RECIPE_PRIVATE', 'FRESH_ORACLE_PRIVATE', 'HELDOUT_ORACLE_PRIVATE'])


@pytest.mark.parametrize('tool_request', [
    {'op': 'discover', 'provider': 'native', 'request': {'operations': [{'op': 'classes', 'query': 'InventedOwner'}]}},
    {'op': 'discover', 'request': {'operations': [{'op': 'classes', 'query': 'InventedOwner'}]}},
    {'op': 'runtime', 'provider': 'native', 'request': {'operations': []}},
    {'op': 'propose', 'rationale': 'No invented native mapping', 'citations': [], 'actor': {'kind': 'objc_method'}},
    {'op': 'declare_inspector', 'citations': [], 'actor': {'kind': 'objc_method'}},
])
def test_inspector_playbook_refuses_other_provider_or_actor_before_dispatch(inert, tmp_path, tool_request):
    # Retain a caller-approved observer so proposal admission itself is visible.
    inert.state['observer_profile'] = {'format': G.H.PROFILE, 'id': 'public', 'label': 'Read content',
        'effect': 'read_content', 'params': [], 'reference': {'fields': {'result': {'kind': 'constant', 'value': 'ORACLE_PRIVATE'}}}}
    inert.state['observer_profile_sha256'] = G.R.digest(inert.state['observer_profile']); inert.save()
    before = (inert.root / 'session.json').read_bytes()
    adapter, seen = model([decision(tool_request)])
    result = _inspector_run(inert, tmp_path, adapter)
    assert result['status'] == 'stopped_failure' and result['tool_dispatches'] == 0
    assert len(seen) == result['model_calls'] == 1 and not inert.calls
    assert 'inspector discovery scope requires' in result['error']['message']
    assert (inert.root / 'session.json').read_bytes() == before
    assert not (tmp_path / 'inspector-playbook/decision-000/effective-request.json').exists()


def test_inspector_playbook_dispatches_explicit_inspector_query_and_retains_actual_paging(inert, tmp_path):
    request = {'op': 'discover', 'provider': 'inspector', 'request': {'operations': [{'op': 'describe',
        'recipe': {'root': {'kind': 'global', 'name': 'globalThis'}, 'steps': []},
        'max_properties': 8, 'offset': 8, 'property_query': 'taskword'}]}}
    observed = {'source': {'artifact': 'current-inspector.json', 'sha256': 'a' * 64},
        'operations': [{'op': 'describe', 'offset': 8, 'next_offset': 16, 'truncated': True,
                        'properties': [{'name': 'ObservedProperty', 'pointer': '/operations/0/properties/0'}]}],
        'whole_graph_coverage': False}
    inert.payloads['discover'] = {'status': 'observed', 'provider': 'inspector', 'observation': observed,
        'evidence_only': True, 'proof_created': False}
    adapter, seen = model([decision(request), decision(reason='Retained page is partial; next page remains unknown.')])
    result = _inspector_run(inert, tmp_path, adapter)
    assert result['status'] == 'model_stopped' and result['tool_dispatches'] == len(inert.calls) == 1
    assert inert.calls[0]['provider'] == 'inspector' and inert.calls[0]['request'] == request['request']
    views = [json.loads(row['messages'][1]['content']) for row in seen]
    assert views[1]['last_observation']['observation'] == observed
    assert 'native_guide' not in views[1] and 'discovery_playbook' in views[1]
    assert views[1]['discovery_playbook']['scope'] == D.INSPECTOR_PLAYBOOK['scope']


@pytest.mark.parametrize('options', [
    {},
    {'allowed_tools': ['discover']},
    {'allowed_tools': ['discover', 'evidence', 'record']},
    {'allowed_tools': ['discover', 'evidence'], 'static_only': True},
    {'allowed_tools': ['discover', 'evidence'], 'decision_protocol': 'full_native_v1'},
    {'allowed_tools': ['discover', 'evidence'], 'discovery_routine': RP.ID},
])
def test_inspector_playbook_requires_compatible_explicit_scope_before_outputs(inert, tmp_path, options):
    adapter, seen = model([decision()]); out = tmp_path / 'invalid-inspector-scope'
    with pytest.raises(ValueError):
        D.run(inert.root, out, adapter=adapter, discovery_playbook='inspector_v1', **options)
    assert not out.exists() and not seen and not inert.calls


def test_inspector_playbook_does_not_change_default_native_provider_admission(inert, tmp_path):
    request = {'op': 'discover', 'provider': 'native', 'request': {'operations': [{'op': 'classes', 'query': 'Observed'}]}}
    adapter, seen = model([decision(request), decision()])
    result = D.run(inert.root, tmp_path / 'unchanged-default', adapter=adapter, allowed_tools=['discover', 'evidence'])
    assert result['status'] == 'model_stopped' and len(inert.calls) == result['tool_dispatches'] == 1
    assert inert.calls[0]['provider'] == 'native'
    assert 'discovery_playbook' not in result
    for message in seen:
        view = json.loads(message['messages'][1]['content'])
        assert view['native_guide'] == G.native_guide(view['tools'])
        assert 'discovery_playbook' not in view and 'caller_scope' not in view
