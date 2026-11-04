"""Actual owned-reader outputs with inert APIs; VM guarded runner only."""
import copy
import json
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_ax_state as S
from anyuse.execution import rea_owned_ax_state as O
import test_rea_ax_state as X

TARGET, SELECTOR, Node = copy.deepcopy(X.TARGET), copy.deepcopy(X.SELECTOR), X.Node


def unavailable(*, local=False, code=-25212):
    result = {"status": "unavailable", "code": code, "pid": None,
              "error": {"type": "AXError", "message": "actual inert focus query unavailable"}}
    if local: result["selected_window_match"] = None
    return result


def focused(pid, *, local=False, match=True):
    result = {"status": "observed", "code": 0, "pid": pid, "error": None}
    if local: result["selected_window_match"] = match
    return result


class Backend(X.Backend):
    """New owner-mode API facts, never an edited legacy state/observation."""
    def __init__(self, *, pid=42, launched=1000.25, display_id=4):
        super().__init__(pid=pid, launched=launched, display_id=display_id)
        self.app.values["AXWindows"] = (0, [self.window])
        self.focus_rows = {"global": unavailable(), "local": focused(pid, local=True)}
        self.quartz_rows = [{"number": 17, "pid": pid, "layer": 8, "alpha": 1.0, "on_screen": True,
                            "position": [0.0, 0.0], "size": [1200.0, 800.0], "owner_name": "Fixture", "name": ""}]
        self.authorization_processes = []
        self.visibility_count, self.focus_count = 0, 0
        self.after_visibility, self.on_focus, self.on_sleep = None, None, None

    def context(self, expected, deadline, *, read_focus=True):
        self.calls.append("context")
        self.context_count += 1
        if self.context_count == 3 and self.after_context is not None: self.after_context(self)
        result = copy.deepcopy(self.current)
        if not read_focus: result["focus"]["focused_pid"] = None
        return result

    def focus(self, identity, window, deadline):
        self.calls.append("focus")
        self.focus_count += 1
        assert identity == self.identity and window is self.window
        if self.on_focus is not None: self.on_focus(self)
        return copy.deepcopy(self.focus_rows)

    def visible_windows(self, deadline):
        self.calls.append("visible_windows")
        self.visibility_count += 1
        if self.visibility_count == 2 and self.after_visibility is not None: self.after_visibility(self)
        return {"windows": copy.deepcopy(self.quartz_rows), "authorization_processes": copy.deepcopy(self.authorization_processes)}

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds
        if self.on_sleep is not None: self.on_sleep(self)


def observe(backend=None, *, selector=None, target=None, identity=None, deadline=None):
    backend = Backend() if backend is None else backend
    return O._capture(copy.deepcopy(TARGET if target is None else target),
        copy.deepcopy(backend.identity if identity is None else identity), copy.deepcopy(SELECTOR if selector is None else selector),
        deadline=backend.now + 10 if deadline is None else deadline, backend=backend, clock=backend.clock)


def contract(observation):
    return O.validate_contract({"format": O.CONTRACT, "target": copy.deepcopy(observation["context"]["target"]),
                               "selector": copy.deepcopy(observation["selector"]), "state": copy.deepcopy(observation["state"])})


def settle(backend=None, *, deadline=None, interval=.4, max_samples=16):
    backend = Backend() if backend is None else backend
    return O._settle(copy.deepcopy(TARGET), copy.deepcopy(backend.identity), copy.deepcopy(SELECTOR),
        deadline=backend.now + 10 if deadline is None else deadline, interval=interval, max_samples=max_samples,
        backend=backend, clock=backend.clock, sleep=backend.sleep)


def checked(backend=None, **kwargs):
    result = observe(backend, **kwargs)
    assert O.validate_observation(result) == result
    return result


def test_actual_full_tree_has_unknown_global_focus_without_invented_semantic_focus_or_input_permission():
    backend = Backend()
    result = checked(backend)
    assert result["complete"] and result["issues"] == [] and result["read_only"] is True and result["proof_created"] is False
    state, context = result["state"], result["context"]
    assert set(state) == {"format", "displays", "root", "node_count", "nodes"} and "focus" not in state
    assert state["node_count"] == 4 and [row["path"] for row in state["nodes"]] == [[], [0], [0, 0], [0, 1]]
    assert state["nodes"][2]["value"] is False and state["nodes"][3]["value"] == "Résumé 東京"
    assert context["completeness_scope"] == O.COMPLETENESS_SCOPE
    assert context["preflight"]["focus"]["global"]["status"] == "not_attempted"
    for phase in ("before", "after"):
        focus = context[phase]["focus"]
        assert focus["frontmost_pid"] == 42 and focus["focused_pid"] is None
        assert focus["global"] == unavailable() and focus["local"] == focused(42, local=True)
    assert context["visibility_before"]["association"] == "unique_owner_geometry"
    assert context["visibility_before"]["selected_window_number"] == 17
    assert O.matches(result, contract(result))
    assert set(backend.calls) <= {"context", "displays", "application", "timeout", "owner", "focus", "visible_windows", "AXWindows", *S.ATTRS.values()}
    json.dumps(result, allow_nan=False)


def test_both_unavailable_focus_queries_are_retained_as_unknown_and_not_a_focused_claim():
    backend = Backend(); backend.focus_rows["local"] = unavailable(local=True, code=-25205)
    result = checked(backend)
    assert result["complete"] and result["context"]["before"]["focus"]["local"] == unavailable(local=True, code=-25205)
    assert "focused_owner" not in json.dumps(result)
    legacy = X.Backend(); legacy.current["focus"]["focused_pid"] = None
    old = X.observe(legacy)
    assert old["complete"] is False and not S.matches(old, X.contract(X.observe()))
    with pytest.raises(ValueError): S.validate_contract(contract(result))


def test_actual_diagnostic_shaped_global_cannot_complete_and_successful_local_query_stay_independent():
    # Same availability/code distinction as retained guest diagnostic3369, using
    # a generic inert process/window rather than a copied app-specific receipt.
    backend = Backend()
    backend.focus_rows = {"global": unavailable(code=-25204), "local": focused(42, local=True)}
    result = checked(backend)
    assert result["complete"]
    focus = result["context"]["before"]["focus"]
    assert focus["global"]["code"] == -25204 and focus["global"]["status"] == "unavailable" and focus["focused_pid"] is None
    assert focus["local"]["code"] == 0 and focus["local"]["pid"] == 42 and focus["local"]["selected_window_match"] is True
    assert result["proof_created"] is False


@pytest.mark.parametrize("fault", ["global", "local_owner", "local_window", "frontmost", "unavailable_but_known_foreign_owner"])
def test_every_known_focus_contradiction_is_retained_but_never_admitted(fault):
    backend = Backend()
    if fault == "global": backend.focus_rows["global"] = focused(999)
    elif fault == "local_owner": backend.focus_rows["local"] = focused(999, local=True)
    elif fault == "local_window": backend.focus_rows["local"] = focused(42, local=True, match=False)
    elif fault == "frontmost": backend.current["focus"]["frontmost_pid"] = 999
    else:
        backend.focus_rows["local"] = unavailable(local=True, code=0)
        backend.focus_rows["local"]["pid"] = 999
    result = checked(backend)
    assert result["complete"] is False and result["issues"]
    assert not O.matches(result, contract(checked()))


def test_focus_availability_changes_remain_in_raw_context_without_becoming_whole_state_changes():
    backend = Backend()
    def change(b):
        if b.focus_count == 2: b.focus_rows = {"global": focused(42), "local": unavailable(local=True)}
    backend.on_focus = change
    result = checked(backend)
    assert result["complete"]
    assert result["context"]["before"]["focus"] != result["context"]["after"]["focus"]
    assert O.matches(result, contract(checked()))


def test_full_contract_is_reusable_with_fresh_owned_identity_but_not_semantic_edits():
    first = checked()
    other = Backend(pid=921, launched=2000.5, display_id=18); other.quartz_rows[0]["number"] = 222
    second = checked(other)
    assert second["context"] != first["context"] and second["state"] == first["state"]
    assert O.matches(second, contract(first))
    for mutation in (True, 0, "false"):
        changed = Backend(); changed.checkbox.values["AXValue"] = (0, mutation)
        assert not O.matches(checked(changed), contract(first))


@pytest.mark.parametrize("fault", ["duplicate_ax", "foreign_ax_root", "foreign_child", "missing_windows", "wrong_window_kind"])
def test_full_owned_ax_window_selection_never_uses_expected_child_values(fault):
    backend = Backend()
    if fault == "duplicate_ax": backend.app.values["AXWindows"] = (0, [backend.window, backend.window])
    elif fault == "foreign_ax_root": backend.window.owner = 999
    elif fault == "foreign_child": backend.checkbox.owner = 999
    elif fault == "missing_windows": backend.app.values["AXWindows"] = (-25212, None)
    else: backend.window.values["AXRole"] = (0, "AXGroup")
    result = checked(backend)
    assert result["complete"] is False and result["issues"]


@pytest.mark.parametrize("fault", ["absent", "duplicate", "wrong_owner", "hidden", "transparent", "moved", "covered", "authorization"])
def test_current_quartz_ownership_visibility_geometry_and_overlay_gates(fault):
    backend = Backend()
    row = backend.quartz_rows[0]
    if fault == "absent": backend.quartz_rows = []
    elif fault == "duplicate": backend.quartz_rows.append({**copy.deepcopy(row), "number": 18})
    elif fault == "wrong_owner": row["pid"] = 999
    elif fault == "hidden": row["on_screen"] = False
    elif fault == "transparent": row["alpha"] = 0.0
    elif fault == "moved": row["position"][0] += 1
    else:
        overlay = {**copy.deepcopy(row), "number": 18, "pid": 999, "size": [10.0, 10.0], "alpha": .1}
        backend.quartz_rows.insert(0, overlay)
        if fault == "authorization": backend.authorization_processes = [{"bundle_id": "com.apple.SecurityAgent", "pid": 999}]
    result = checked(backend)
    assert result["complete"] is False and result["context"]["visibility_before"]["windows"] == backend.quartz_rows
    assert not O.matches(result, contract(checked()))


def test_unrelated_visible_windows_behind_selected_owner_are_not_discarded_or_misread_as_covering():
    backend = Backend()
    backend.quartz_rows.append({**copy.deepcopy(backend.quartz_rows[0]), "number": 18, "pid": 999})
    result = checked(backend)
    assert result["complete"] and len(result["context"]["visibility_before"]["windows"]) == 2
    assert result["context"]["visibility_before"]["inventory_materialization_bounded"] is False


@pytest.mark.parametrize("fault", ["epoch", "uid", "session", "locked", "tcc", "console", "display", "window_replaced", "duplicate_after"])
def test_mid_read_identity_session_window_or_display_changes_fail_with_actual_prefix(fault):
    backend = Backend()
    def change(b):
        if fault == "epoch": b.current["kernel"]["started_at"] = "different epoch"
        elif fault == "uid": b.current["identity"]["uid"] += 1
        elif fault == "session": b.current["session"]["asid"] += 1
        elif fault == "locked": b.current["session"]["locked"] = True
        elif fault == "tcc": b.current["session"]["ax_trusted"] = False
        elif fault == "console": b.current["session"]["on_console"] = False
        elif fault == "display": b.screen_rows[0]["size"][0] += 1
        elif fault == "window_replaced":
            replacement = Node("AXWindow", position=(0.0, 0.0), size=(1200.0, 800.0)); replacement.values["AXSubrole"] = (0, "AXDialog")
            b.app.values["AXWindows"] = (0, [replacement])
        else: b.app.values["AXWindows"] = (0, [b.window, b.window])
    backend.after_context = change
    result = checked(backend)
    assert result["complete"] is False and result["state"]["node_count"] == 4


def test_selected_quartz_window_number_change_after_tree_is_not_hidden_by_equal_geometry():
    backend = Backend()
    backend.after_visibility = lambda b: b.quartz_rows[0].update(number=999)
    result = checked(backend)
    assert not result["complete"] and result["context"]["visibility_after"]["selected_window_number"] == 999


@pytest.mark.parametrize("fault", ["scalar", "type", "cycle", "duplicate", "children", "depth", "error", "timeout"])
def test_tree_read_failures_keep_the_actual_bounded_typed_prefix(fault):
    backend = Backend(); deadline = backend.now + 10
    if fault == "scalar": backend.checkbox.values["AXValue"] = (0, backend.checkbox)
    elif fault == "type": backend.checkbox.values["AXEnabled"] = (0, 1)
    elif fault == "cycle": backend.group.values["AXChildren"][1].append(backend.window)
    elif fault == "duplicate": backend.group.values["AXChildren"][1].append(backend.checkbox)
    elif fault == "children": backend.window.values["AXChildren"] = (0, [Node("AXGroup") for _ in range(65)])
    elif fault == "depth":
        child = Node("AXStaticText")
        for _ in range(9): child = Node("AXGroup", children=[child])
        backend.window.values["AXChildren"] = (0, [child])
    elif fault == "error": backend.checkbox.values["AXValue"] = (-25204, None)
    else: backend.step = .2; deadline = backend.now + .1
    result = checked(backend, deadline=deadline)
    assert result["complete"] is False and result["issues"]
    assert "object at" not in json.dumps(result) and "0x" not in json.dumps(result)


@pytest.mark.parametrize("fault", ["focus_field", "version", "proof", "state_field", "global_pid", "local_match", "geometry", "row_type", "scope", "window_number", "same_window"])
def test_pure_validator_rejects_forged_complete_actual_capture_fields(fault):
    result = checked()
    if fault == "focus_field": result["state"]["focus"] = {"focused_owner": True}
    elif fault == "version": result["format"] = S.OBSERVATION
    elif fault == "proof": result["proof_created"] = True
    elif fault == "state_field": result["state"]["nodes"][2]["value"] = 0
    elif fault == "global_pid": result["context"]["before"]["focus"]["focused_pid"] = 42
    elif fault == "local_match": result["context"]["before"]["focus"]["local"]["selected_window_match"] = False
    elif fault == "geometry": result["state"]["nodes"][0]["size"][0] += 1
    elif fault == "row_type": result["state"]["nodes"][2]["enabled"] = 1
    elif fault == "scope": result["context"]["completeness_scope"] = "all_application_state"
    elif fault == "same_window": result["context"]["window_after"]["same_window"] = False
    else: result["context"]["visibility_before"]["selected_window_number"] += 1
    if fault == "state_field":
        assert O.validate_observation(result) == result and not O.matches(result, contract(checked()))
    else:
        with pytest.raises(ValueError): O.validate_observation(result)


def test_settler_uses_actual_outputs_and_first_complete_equal_pair_with_no_oracle():
    backend = Backend()
    result = settle(backend)
    assert O.validate_settlement(result) == result and result["complete"] and result["stable_pair"] == [0, 1]
    assert len(result["samples"]) == 2 and backend.visibility_count == 4
    assert all(O.matches(row["observation"], contract(result["samples"][0]["observation"])) for row in result["samples"])
    for row in result["samples"]: assert row["observation"]["context"]["before"]["focus"]["focused_pid"] is None


def test_settler_retains_semantic_change_then_stops_at_first_later_equal_pair():
    backend = Backend(); backend.on_sleep = lambda b: b.checkbox.values.update(AXValue=(0, True))
    result = settle(backend)
    assert result["complete"] and result["stable_pair"] == [1, 2] and len(result["samples"]) == 3
    assert result["samples"][0]["observation"]["state"] != result["samples"][1]["observation"]["state"]


def test_settler_unknown_to_observed_focus_does_not_manufacture_a_focus_state():
    backend = Backend()
    backend.on_sleep = lambda b: b.focus_rows.update({"global": focused(42), "local": unavailable(local=True)})
    result = settle(backend)
    assert result["complete"] and result["stable_pair"] == [0, 1]
    assert result["samples"][0]["observation"]["context"]["before"]["focus"] != result["samples"][1]["observation"]["context"]["before"]["focus"]


def test_settler_rejects_selected_window_identity_change_even_if_full_tree_matches():
    backend = Backend(); backend.on_sleep = lambda b: b.quartz_rows[0].update(number=18)
    result = settle(backend)
    assert not result["complete"] and result["stable_pair"] is None and result["issues"] == [{"kind": "context_changed"}]
    assert len(result["samples"]) == 2 and O.validate_settlement(result) == result


def test_raw_incomplete_prefix_resets_pair_then_allows_two_fresh_complete_samples():
    class Once(Backend):
        def attribute(self, node, name):
            if node is self.checkbox and name == "AXValue" and not getattr(self, "failed_once", False):
                self.failed_once = True
                return -25204, None
            return super().attribute(node, name)
    result = settle(Once())
    assert result["complete"] and result["stable_pair"] == [1, 2]
    assert not result["samples"][0]["observation"]["complete"] and result["samples"][0]["observation"]["state"]["nodes"]
    assert O.validate_settlement(result) == result


@pytest.mark.parametrize("fault", ["pair", "skip_first", "cadence", "late", "format", "drop_prefix", "empty_issues"])
def test_settlement_consumer_rederives_pair_cadence_and_actual_failure_prefix(fault):
    result = settle()
    if fault == "pair": result["stable_pair"] = [1, 2]
    elif fault == "skip_first":
        third = copy.deepcopy(result["samples"][-1]); third["index"] = 2
        third["started_monotonic"] += .4; third["finished_monotonic"] += .4
        result["samples"].append(third); result["stable_pair"] = [1, 2]; result["finished_monotonic"] += .4
    elif fault == "cadence": result["samples"][1]["started_monotonic"] = result["samples"][0]["finished_monotonic"]
    elif fault == "late": result["finished_monotonic"] = result["deadline_monotonic"]
    elif fault == "format": result["format"] = S.SETTLEMENT
    elif fault == "drop_prefix": result["samples"][0]["observation"]["context"].pop("preflight")
    else: result.update(complete=False, stable_pair=None, issues=[])
    with pytest.raises(ValueError): O.validate_settlement(result)


def test_sample_limit_and_deadline_are_failure_evidence_not_usable_pairs():
    one = settle(max_samples=1)
    assert not one["complete"] and one["issues"] == [{"kind": "sample_limit"}] and O.validate_settlement(one) == one
    backend = Backend(); timed = settle(backend, deadline=backend.now + .1, interval=.4)
    assert not timed["complete"] and timed["issues"] == [{"kind": "deadline"}] and O.validate_settlement(timed) == timed


def test_fixed_mac_focus_api_preserves_error_codes_and_known_local_owner_without_any_action(monkeypatch):
    backend = object.__new__(O._Mac)
    system, application, selected = object(), object(), object()
    backend.A = SimpleNamespace(AXUIElementCreateSystemWide=lambda: system)
    backend.application = lambda pid: application
    backend.timeout = lambda element, seconds: None
    backend.attribute = lambda root, name: (-25212, None) if root is system else (0, selected)
    backend.owner = lambda element: 42
    backend.equal = lambda left, right: left is right
    monkeypatch.setattr(O.time, "monotonic", lambda: 100.0)
    result = backend.focus(Backend().identity, selected, 101.0)
    assert result["global"]["status"] == "unavailable" and result["global"]["code"] == -25212
    assert result["global"]["pid"] is None and result["local"] == focused(42, local=True)
    def unavailable_equality(left, right): raise RuntimeError("no equality result")
    backend.equal = unavailable_equality
    partial = backend.focus(Backend().identity, selected, 101.0)["local"]
    assert partial["status"] == "unavailable" and partial["code"] == 0 and partial["pid"] == 42 and partial["selected_window_match"] is None
    O._focus_probe(partial, local=True)


def test_fixed_mac_quartz_api_normalizes_actual_fields_then_actual_capture_and_pure_validator_agree(monkeypatch):
    mac = object.__new__(O._Mac)
    Q = SimpleNamespace(**{name: name for name in ("kCGWindowListOptionOnScreenOnly", "kCGNullWindowID", "kCGWindowBounds",
        "kCGWindowNumber", "kCGWindowOwnerPID", "kCGWindowLayer", "kCGWindowAlpha", "kCGWindowIsOnscreen", "kCGWindowOwnerName", "kCGWindowName")})
    source = [{Q.kCGWindowNumber: 17, Q.kCGWindowOwnerPID: 42, Q.kCGWindowLayer: 8, Q.kCGWindowAlpha: 1.0,
               Q.kCGWindowIsOnscreen: True, Q.kCGWindowOwnerName: "Fixture", Q.kCGWindowName: "",
               Q.kCGWindowBounds: {"X": 0.0, "Y": 0.0, "Width": 1200.0, "Height": 800.0}}]
    queries = []
    def inventory(options, null_window):
        assert options == Q.kCGWindowListOptionOnScreenOnly and null_window == Q.kCGNullWindowID
        queries.append("quartz")
        return source
    Q.CGWindowListCopyWindowInfo = inventory
    mac.Q = Q
    mac.F = SimpleNamespace(CFGetTypeID=lambda value: 1 if type(value) is dict else 99, CFDictionaryGetTypeID=lambda: 1)
    def apps(bundle): queries.append(bundle); return []
    mac.K = SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=apps))
    mac.native_scalar = lambda value: (_ for _ in ()).throw(AssertionError("unexpected native value"))
    monkeypatch.setattr(O.time, "monotonic", lambda: 100.0)
    normalized = mac.visible_windows(101.0)
    backend = Backend()
    assert normalized == {"windows": backend.quartz_rows, "authorization_processes": []}
    backend.visible_windows = lambda deadline: mac.visible_windows(deadline)
    observed = checked(backend)
    assert observed["complete"] and observed["context"]["visibility_after"]["windows"] == normalized["windows"]
    assert queries.count("quartz") == 3 and set(O.AUTHORIZATION_BUNDLE_IDS) <= set(queries)
    source[0][Q.kCGWindowBounds]["Width"] = float("nan")
    with pytest.raises(ValueError): mac.visible_windows(101.0)


@pytest.mark.parametrize("fault", ["too_many_windows", "duplicate_number", "bool_number", "unknown_auth_bundle", "negative_alpha", "integer_geometry"])
def test_closed_quartz_inventory_rejects_unbounded_or_inexact_metadata(fault):
    backend = Backend()
    inventory = backend.visible_windows(backend.now + 1)
    row = inventory["windows"][0]
    if fault == "too_many_windows": inventory["windows"] = [{**copy.deepcopy(row), "number": index + 1} for index in range(513)]
    elif fault == "duplicate_number": inventory["windows"].append(copy.deepcopy(row))
    elif fault == "bool_number": row["number"] = True
    elif fault == "unknown_auth_bundle": inventory["authorization_processes"] = [{"bundle_id": "caller_bundle", "pid": 999}]
    elif fault == "negative_alpha": row["alpha"] = -.1
    else: row["position"] = [0, 0]
    with pytest.raises(ValueError): O._inventory(inventory)


def test_public_apis_and_backend_constructor_check_vm_before_framework_or_live_reads(monkeypatch):
    calls = []
    def denied(): calls.append("guard"); raise ValueError("not in guest")
    monkeypatch.setattr(O.R, "assert_guest", denied)
    backend = Backend()
    for call in (lambda: O.capture(TARGET, backend.identity, SELECTOR, deadline=101.0),
                 lambda: O.settle(TARGET, backend.identity, SELECTOR, deadline=101.0), lambda: O._Mac()):
        with pytest.raises(ValueError, match="guest"): call()
    assert calls == ["guard", "guard", "guard"] and backend.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
