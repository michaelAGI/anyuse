"""Inert AX observer tests; execute only through the VM guarded runner."""
import copy
import ctypes
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_ax_state as S


TARGET = {"bundle_id": "org.test.generic-observer", "version": "2", "build": "8", "sha256": "a" * 64, "files": 5, "bytes": 2048}
SELECTOR = {"format": S.SELECTOR, "role": "AXWindow", "subrole": "AXDialog", "title": "",
            "geometry": {"kind": "full_display"}}


class Node:
    def __init__(self, role, *, title="", value=None, children=(), position=(20.0, 30.0), size=(200.0, 30.0), owner=None):
        self.owner = owner
        self.values = {"AXRole": (0, role), "AXSubrole": (-25212, None), "AXTitle": (0, title),
            "AXDescription": (0, ""), "AXEnabled": (0, True), "AXValue": (-25212, None) if value is None else (0, value),
            "AXPosition": (0, position), "AXSize": (0, size), "AXChildren": (0, list(children))}

    def __repr__(self):
        raise AssertionError("an AX node representation/address must never be requested")


class Backend:
    def __init__(self, *, pid=42, launched=1000.25, display_id=4):
        self.now, self.step, self.calls, self.context_count = 100.0, 0.0, [], 0
        self.identity = {"bundle": "/fixture/Observer.app", "bundle_id": TARGET["bundle_id"],
            "executable": "/fixture/Observer.app/Contents/MacOS/observer", "pid": pid, "uid": 501, "launch_time": launched}
        self.current = {"identity": copy.deepcopy(self.identity),
            "kernel": {"pid": pid, "ppid": 1, "uid": 501, "started_at": "epoch:" + str(launched),
                       "executable": self.identity["executable"]},
            "session": {"asid": 23, "attribute_bits": 0x10, "uid": 501, "on_console": True,
                        "login_done": True, "locked": False, "ax_trusted": True},
            "focus": {"frontmost_pid": pid, "focused_pid": pid}}
        self.screen_rows = [{"id": display_id, "position": [0.0, 0.0], "size": [1200.0, 800.0]}]
        self.checkbox = Node("AXCheckBox", title="Show guides", value=False)
        self.label = Node("AXStaticText", value="Résumé 東京")
        self.group = Node("AXGroup", children=[self.checkbox, self.label])
        self.window = Node("AXWindow", children=[self.group], position=(0.0, 0.0), size=(1200.0, 800.0))
        self.window.values["AXSubrole"] = (0, "AXDialog")
        self.app = Node("AXApplication", children=[self.window])
        self.after_context = None

    def clock(self): return self.now

    def context(self, expected, deadline):
        self.calls.append("context")
        self.context_count += 1
        if self.context_count == 2 and self.after_context is not None: self.after_context(self)
        return copy.deepcopy(self.current)

    def displays(self, deadline):
        self.calls.append("displays")
        return copy.deepcopy(self.screen_rows)

    def application(self, pid):
        self.calls.append("application")
        assert pid == self.identity["pid"]
        return self.app

    def timeout(self, node, seconds):
        self.calls.append("timeout")
        assert 0 < seconds <= S.AX_SECONDS

    def owner(self, node):
        self.calls.append("owner")
        return self.identity["pid"] if node.owner is None else node.owner

    def attribute(self, node, name):
        self.calls.append(name)
        self.now += self.step
        return node.values.get(name, (-25205, None))

    def equal(self, left, right): return left is right
    def is_array(self, value): return type(value) in (list, tuple)
    def vector(self, value, key): return value
    def native_scalar(self, value): raise TypeError("unsupported inert native scalar")


def observe(backend=None, *, selector=None, target=None, identity=None, deadline=None):
    backend = Backend() if backend is None else backend
    return S._capture(copy.deepcopy(TARGET if target is None else target),
        copy.deepcopy(backend.identity if identity is None else identity), copy.deepcopy(SELECTOR if selector is None else selector),
        deadline=backend.now + 10 if deadline is None else deadline, backend=backend, clock=backend.clock)


def contract(observation):
    return {"format": S.CONTRACT, "target": copy.deepcopy(observation["context"]["target"]),
            "selector": copy.deepcopy(observation["selector"]), "state": copy.deepcopy(observation["state"])}


def test_complete_state_keeps_full_typed_tree_geometry_display_and_availability():
    backend = Backend()
    backend.label.values["AXDescription"] = (-25205, None)
    result = observe(backend)
    assert result["complete"] and result["issues"] == [] and result["read_only"] and not result["proof_created"]
    state = result["state"]
    assert state["node_count"] == 4 and [node["path"] for node in state["nodes"]] == [[], [0], [0, 0], [0, 1]]
    assert state["nodes"][0]["children"] == [1] and state["nodes"][1]["children"] == [2, 3]
    assert state["nodes"][2]["value"] is False and state["nodes"][3]["value"] == "Résumé 東京"
    assert state["nodes"][3]["availability"]["description"] == "unsupported"
    assert state["nodes"][1]["availability"]["value"] == "no_value"
    assert state["nodes"][0]["position"] == [0.0, 0.0] and state["nodes"][0]["size"] == [1200.0, 800.0]
    assert state["displays"] == [{"position": [0.0, 0.0], "size": [1200.0, 800.0]}]
    assert state["focus"] == {"frontmost_owner": True, "focused_owner": True}
    assert S.matches(result, contract(result))
    json.dumps(result, allow_nan=False)  # No ephemeral handles, objects or address-bearing reprs escaped.
    assert set(backend.calls) <= {"context", "displays", "application", "timeout", "owner", *S.ATTRS.values()}


def test_independent_full_contract_survives_restart_without_editing_old_receipt():
    before = observe(Backend(pid=42, launched=1000.25, display_id=4))
    saved = copy.deepcopy(before)
    frozen = contract(before)
    after = observe(Backend(pid=921, launched=2000.5, display_id=18))
    assert after["complete"] and S.matches(after, frozen)
    assert before["context"] != after["context"] and before["state"] == after["state"]
    assert before == saved and before["context"]["expected_identity"]["pid"] == 42
    assert after["context"]["expected_identity"]["pid"] == 921


@pytest.mark.parametrize("change", ["value_type", "role", "width", "display", "availability", "order", "extra_node"])
def test_whole_contract_refuses_every_semantic_difference_without_field_filters(change):
    first = observe()
    backend = Backend()
    if change == "value_type": backend.checkbox.values["AXValue"] = (0, 0)
    elif change == "role": backend.label.values["AXRole"] = (0, "AXCell")
    elif change == "width": backend.group.values["AXSize"] = (0, (201.0, 30.0))
    elif change == "display": backend.screen_rows.append({"id": 9, "position": [1200.0, 0.0], "size": [800.0, 600.0]})
    elif change == "availability": backend.label.values["AXDescription"] = (-25205, None)
    elif change == "order": backend.group.values["AXChildren"][1].reverse()
    else: backend.group.values["AXChildren"][1].append(Node("AXStaticText", value="additional"))
    after = observe(backend)
    assert after["complete"] and not S.matches(after, contract(first))


@pytest.mark.parametrize("change", ["pid", "uid", "launch", "kernel_start", "kernel_executable", "focus", "session", "display"])
def test_identity_or_context_change_after_tree_read_never_reports_complete(change):
    backend = Backend()
    def mutate(b):
        if change == "pid": b.current["identity"]["pid"] += 1
        elif change == "uid": b.current["identity"]["uid"] += 1
        elif change == "launch": b.current["identity"]["launch_time"] += 1
        elif change == "kernel_start": b.current["kernel"]["started_at"] = "different epoch"
        elif change == "kernel_executable": b.current["kernel"]["executable"] = "/fixture/other"
        elif change == "focus": b.current["focus"]["focused_pid"] += 1
        elif change == "session": b.current["session"]["asid"] += 1
        else: b.screen_rows[0]["size"][0] += 1
    backend.after_context = mutate
    result = observe(backend)
    assert result["state"]["node_count"] == 4 and not result["complete"] and result["issues"]
    assert not S.matches(result, contract(observe()))


@pytest.mark.parametrize("change", ["locked", "untrusted", "remote", "off_console", "wrong_focus", "foreign_element"])
def test_refuses_unowned_or_unauthorized_context_without_any_action(change):
    backend = Backend()
    if change == "locked": backend.current["session"]["locked"] = True
    elif change == "untrusted": backend.current["session"]["ax_trusted"] = False
    elif change == "remote": backend.current["session"]["attribute_bits"] |= 0x1000
    elif change == "off_console": backend.current["session"]["on_console"] = False
    elif change == "wrong_focus": backend.current["focus"]["frontmost_pid"] += 1
    else: backend.checkbox.owner = 777
    result = observe(backend)
    assert not result["complete"] and result["issues"]


def test_selector_ambiguity_is_not_resolved_by_expected_child_values_or_list_order():
    backend = Backend()
    another = Node("AXWindow", children=[Node("AXStaticText", value="different")], position=(0.0, 0.0), size=(1200.0, 800.0))
    another.values["AXSubrole"] = (0, "AXDialog")
    backend.app.values["AXChildren"] = (0, [another, backend.window])
    result = observe(backend)
    assert not result["complete"] and result["issues"] == [{"kind": "window_selector_ambiguous_or_absent", "matches": 2}]
    assert result["state"]["nodes"] == []
    backend.app.values["AXChildren"] = (0, [])
    assert observe(backend)["issues"] == [{"kind": "window_selector_ambiguous_or_absent", "matches": 0}]


def test_exact_selector_uses_explicit_geometry_and_preserves_all_displays():
    backend = Backend()
    backend.screen_rows = [{"id": 9, "position": [0.0, 0.0], "size": [1200.0, 800.0]},
                           {"id": 3, "position": [-900.0, 20.0], "size": [900.0, 700.0]}]
    selector = copy.deepcopy(SELECTOR)
    selector["geometry"] = {"kind": "exact", "position": [0.0, 0.0], "size": [1200.0, 800.0]}
    result = observe(backend, selector=selector)
    assert result["complete"] and result["state"]["displays"][0]["position"] == [-900.0, 20.0]
    assert len(result["state"]["displays"]) == 2 and S.matches(result, contract(result))
    selector["geometry"]["size"][0] += 1
    assert not observe(Backend(), selector=selector)["complete"]


@pytest.mark.parametrize("change", ["children", "children_type", "depth", "nodes", "cycle", "duplicate", "scalar", "geometry", "ax_error", "timeout"])
def test_bounds_unknown_scalars_and_read_errors_retain_explicit_incomplete_evidence(change):
    backend = Backend()
    deadline = backend.now + 10
    if change == "children": backend.window.values["AXChildren"] = (0, [Node("AXGroup") for _ in range(65)])
    elif change == "children_type": backend.label.values["AXChildren"] = (0, "")
    elif change == "depth":
        child = Node("AXStaticText")
        for _ in range(9): child = Node("AXGroup", children=[child])
        backend.window.values["AXChildren"] = (0, [child])
    elif change == "nodes":
        backend.window.values["AXChildren"] = (0, [Node("AXGroup", children=[Node("AXStaticText") for _ in range(64)]) for _ in range(4)])
    elif change == "cycle": backend.group.values["AXChildren"][1].append(backend.window)
    elif change == "duplicate": backend.group.values["AXChildren"][1].append(backend.checkbox)
    elif change == "scalar": backend.checkbox.values["AXValue"] = (0, backend.checkbox)
    elif change == "geometry": backend.checkbox.values["AXPosition"] = (0, (float("nan"), 0.0))
    elif change == "ax_error": backend.checkbox.values["AXValue"] = (-25204, None)
    else:
        # The first simulated blocking AX read takes0.2s against an explicitly
        # supplied0.1s phase budget; it necessarily crosses this deadline.
        backend.step = 0.2
        deadline = backend.now + 0.1
    result = observe(backend, deadline=deadline)
    assert not result["complete"] and result["issues"] and not result["proof_created"]
    if change == "timeout":
        assert result["issues"] == [{"kind": "phase_timeout"}]
        assert backend.clock() >= deadline
    serialized = json.dumps(result, allow_nan=False)
    assert "0x" not in serialized and "object at" not in serialized


def test_explicit_no_value_unsupported_and_present_null_are_distinct_states():
    results = []
    for value in [(-25212, None), (-25205, None), (0, None)]:
        backend = Backend()
        backend.label.values["AXValue"] = value
        results.append(observe(backend))
    assert all(row["complete"] for row in results)
    assert len({json.dumps(row["state"], sort_keys=True) for row in results}) == 3
    assert all(not S.matches(row, contract(results[0])) for row in results[1:])


class NativeInt(int): pass
class NativeFloat(float): pass
class NativeBool(int): pass


class Numbers:
    @staticmethod
    def CFGetTypeID(value): return 21 if isinstance(value, NativeBool) else 22 if isinstance(value, (NativeInt, NativeFloat)) else 99
    @staticmethod
    def CFBooleanGetTypeID(): return 21
    @staticmethod
    def CFNumberGetTypeID(): return 22


def native_number(value): return S._native_scalar(value, Numbers, (NativeInt, NativeFloat, NativeBool))


def test_cfnumber_subclasses_normalize_exactly_without_integer_boolean_confusion():
    assert type(S._scalar(NativeInt(0), native_number)) is int
    assert S._scalar(NativeInt(0), native_number) == 0
    assert type(S._scalar(NativeBool(0), native_number)) is bool
    assert S._scalar(NativeBool(0), native_number) is False
    assert type(S._scalar(NativeFloat(0.5), native_number)) is float
    for invalid in (NativeInt(2**63), NativeFloat(float("inf")), NativeBool(2), Node("AXValue")):
        with pytest.raises(ValueError): S._scalar(invalid, native_number)
    with pytest.raises(ValueError): S._scalar(2**63)
    with pytest.raises(ValueError): S._scalar(float("nan"))


@pytest.mark.parametrize("change", ["pid_bool", "launch_bool", "path", "missing_uid", "selector_extra", "integer_geometry", "deadline"])
def test_pure_argument_validation_refuses_bad_context_or_selectors_before_target_reads(change):
    backend = Backend()
    identity, selector, deadline = copy.deepcopy(backend.identity), copy.deepcopy(SELECTOR), backend.now + 10
    if change == "pid_bool": identity["pid"] = True
    elif change == "launch_bool": identity["launch_time"] = True
    elif change == "path": identity["executable"] = "/fixture/Observer.app/../other"
    elif change == "missing_uid": identity.pop("uid")
    elif change == "selector_extra": selector["index"] = 0
    elif change == "integer_geometry": selector["geometry"] = {"kind": "exact", "position": [0, 0], "size": [1200, 800]}
    else: deadline = backend.now + 11
    with pytest.raises(ValueError): observe(backend, identity=identity, selector=selector, deadline=deadline)
    assert backend.calls == []


@pytest.mark.parametrize("change", ["drop_attribute", "drop_availability", "unreachable", "cycle", "id_bool", "context_owner", "unknown_field"])
def test_retained_complete_claim_cannot_hide_partial_tree_or_context(change):
    observation = observe()
    expected = contract(observation)
    if change == "drop_attribute": observation["state"]["nodes"][2].pop("value")
    elif change == "drop_availability": observation["state"]["nodes"][2]["availability"].pop("value")
    elif change == "unreachable": observation["state"]["nodes"][1]["children"] = [2]
    elif change == "cycle": observation["state"]["nodes"][2]["children"] = [0]
    elif change == "id_bool": observation["state"]["nodes"][0]["id"] = False
    elif change == "context_owner": observation["context"]["after"]["kernel"]["uid"] += 1
    else: observation["state"]["nodes"][2]["ignored"] = "not allowed"
    with pytest.raises(ValueError): S.matches(observation, expected)


def test_public_vm_guard_precedes_framework_construction_and_all_target_io(monkeypatch):
    def deny(): raise PermissionError("VM only")
    monkeypatch.setattr(S.R, "assert_guest", deny)
    monkeypatch.setattr(S, "_Mac", lambda: pytest.fail("frameworks before VM guard"))
    with pytest.raises(PermissionError, match="VM only"):
        S.capture(TARGET, Backend().identity, SELECTOR, deadline=110)


def test_final_validation_cannot_report_success_after_deadline(monkeypatch):
    backend = Backend()
    original = S.validate_contract
    def delayed(value):
        result = original(value)
        backend.now += 11
        return result
    monkeypatch.setattr(S, "validate_contract", delayed)
    result = observe(backend)
    assert not result["complete"] and result["issues"] == [{"kind": "phase_timeout"}]


def test_window_must_still_match_selector_when_full_root_is_read():
    backend = Backend()
    attribute = backend.attribute
    title_reads = [0]
    def changing(node, name):
        if node is backend.window and name == "AXTitle":
            title_reads[0] += 1
            if title_reads[0] == 2: node.values[name] = (0, "different window title")
        return attribute(node, name)
    backend.attribute = changing
    result = observe(backend)
    assert not result["complete"] and result["state"]["nodes"][0]["title"] == "different window title"


@pytest.mark.parametrize("adopt_successor", [False, True])
def test_public_capture_pins_defensive_copies_before_framework_factory(monkeypatch, adopt_successor):
    backend = Backend()
    target, identity, selector = copy.deepcopy(TARGET), copy.deepcopy(backend.identity), copy.deepcopy(SELECTOR)
    saved = copy.deepcopy((target, identity, selector))
    monkeypatch.setattr(S.R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.time, "monotonic", backend.clock)
    # The real constructor imports frameworks. A constructor-side mutation of
    # caller-owned dictionaries must not change the already admitted pins.
    def factory():
        target["sha256"] = "b" * 64
        identity["pid"] = 777
        selector["title"] = "successor"
        if adopt_successor:
            backend.identity["pid"] = 777
            backend.current["identity"]["pid"] = 777
            backend.current["kernel"]["pid"] = 777
            backend.current["focus"] = {"frontmost_pid": 777, "focused_pid": 777}
        return backend
    monkeypatch.setattr(S, "_Mac", factory)
    frozen_capture = S._capture
    def capture_with_clock(*args, **kwargs):
        kwargs.setdefault("clock", backend.clock)
        return frozen_capture(*args, **kwargs)
    monkeypatch.setattr(S, "_capture", capture_with_clock)
    result = S.capture(target, identity, selector, deadline=backend.now + 10)
    assert result["complete"] is (not adopt_successor)
    assert result["context"]["target"] == saved[0]
    assert result["context"]["expected_identity"] == saved[1]
    assert result["selector"] == saved[2]
    assert identity["pid"] == 777 and result["context"]["expected_identity"]["pid"] == 42
    if not adopt_successor: assert S.matches(result, contract(observe(Backend())))


def test_present_null_children_refuses_while_known_empty_and_unavailable_are_explicit():
    empty = observe()
    assert empty["complete"] and empty["state"]["nodes"][2]["children"] == []
    assert empty["state"]["nodes"][2]["availability"]["children"] == "present"
    backend = Backend()
    backend.checkbox.values["AXChildren"] = (0, None)
    null = observe(backend)
    assert not null["complete"] and null["issues"] == [{"kind": "children_type"}]
    for code, status in ((-25205, "unsupported"), (-25212, "no_value")):
        backend = Backend()
        backend.checkbox.values["AXChildren"] = (code, None)
        unavailable = observe(backend)
        assert unavailable["complete"] and unavailable["state"]["nodes"][2]["children"] == []
        assert unavailable["state"]["nodes"][2]["availability"]["children"] == status
        assert not S.matches(unavailable, contract(empty))


@pytest.mark.parametrize("key,kind", [("position", 1), ("size", 2)])
def test_real_backend_normalizes_only_successful_typed_struct_pairs(key, kind):
    class Pair:
        def __len__(self): return 2
        def __getitem__(self, index): return (1.0, 2.0)[index]
        def __repr__(self): raise AssertionError("no struct repr")
    class Framework:
        kAXValueTypeCGPoint, kAXValueTypeCGSize = 1, 2
        def AXValueGetValue(self, value, supplied_kind, output):
            assert value is token and supplied_kind == kind and output is None
            return True, Pair()
    token = object()
    backend = object.__new__(S._Mac)
    backend.A = Framework()
    value = backend.vector(token, key)
    assert type(value) is list and S._vector(value, size=key == "size") == [1.0, 2.0]
    backend.A.AXValueGetValue = lambda *_: (False, Pair())
    with pytest.raises(ValueError): backend.vector(token, key)
    backend.A.AXValueGetValue = lambda *_: (True, [1.0])
    with pytest.raises(ValueError): backend.vector(token, key)
    backend.A.AXValueGetValue = lambda *_: (True, [float("nan"), 2.0])
    with pytest.raises(ValueError): S._vector(backend.vector(token, key), size=key == "size")


class PhaseClock:
    def __init__(self):
        self.now, self.sleeps = 100.0, []
    def clock(self): return self.now
    def sleep(self, duration):
        assert 0 < duration <= S.MAX_SECONDS
        self.sleeps.append(duration)
        self.now += duration


def scripted_settle(monkeypatch, observations, *, max_samples=16, seconds=10, capture_seconds=.05):
    """Real pure observation validators, with no framework or actor execution."""
    timer, calls = PhaseClock(), []
    def sample(target, identity, selector, *, deadline, backend, clock):
        index = len(calls)
        calls.append(copy.deepcopy((target, identity, selector)))
        timer.now += capture_seconds
        if index >= len(observations): raise AssertionError("sampler read beyond its supplied observations")
        result = observations[index]
        if isinstance(result, Exception): raise result
        return copy.deepcopy(result)
    monkeypatch.setattr(S, "_capture", sample)
    result = S._settle(TARGET, Backend().identity, SELECTOR, deadline=timer.now + seconds,
                       interval=.4, max_samples=max_samples, backend=object(), clock=timer.clock, sleep=timer.sleep)
    return result, timer, calls


def test_settle_stops_at_first_stable_wrong_pair_without_consulting_later_expected_state(monkeypatch):
    wrong = observe()
    backend = Backend()
    backend.checkbox.values["AXValue"] = (0, True)
    expected = observe(backend)
    result, timer, calls = scripted_settle(monkeypatch, [wrong, wrong, expected, expected])
    assert result["complete"] and result["stable_pair"] == [0, 1] and len(calls) == 2
    assert timer.sleeps == [.4] and len(result["samples"]) == 2
    assert not S.matches(result["samples"][-1]["observation"], contract(expected))
    assert S.validate_settlement(result) == result
    assert result["read_only"] and not result["proof_created"]


def test_settle_incomplete_sample_resets_the_consecutive_pair_and_is_retained(monkeypatch):
    complete = observe()
    backend = Backend()
    backend.checkbox.values["AXValue"] = (-25204, None)
    incomplete = observe(backend)
    assert not incomplete["complete"]
    result, _, calls = scripted_settle(monkeypatch, [complete, incomplete, complete, complete])
    assert result["complete"] and result["stable_pair"] == [2, 3] and len(calls) == 4
    assert result["samples"][1]["observation"] == incomplete
    assert S.validate_settlement(result) == result


@pytest.mark.parametrize("change", ["value_type", "role", "geometry", "order", "extra_node", "availability"])
def test_settle_full_semantic_differences_cannot_form_an_early_pair(monkeypatch, change):
    first = observe()
    backend = Backend()
    if change == "value_type": backend.checkbox.values["AXValue"] = (0, 0)
    elif change == "role": backend.label.values["AXRole"] = (0, "AXCell")
    elif change == "geometry": backend.group.values["AXSize"] = (0, (201.0, 30.0))
    elif change == "order": backend.group.values["AXChildren"][1].reverse()
    elif change == "extra_node": backend.group.values["AXChildren"][1].append(Node("AXStaticText", value="extra"))
    else: backend.label.values["AXDescription"] = (-25205, None)
    changed = observe(backend)
    assert changed["complete"]
    result, _, calls = scripted_settle(monkeypatch, [first, changed, changed])
    assert result["complete"] and result["stable_pair"] == [1, 2] and len(calls) == 3
    assert result["samples"][0]["observation"] == first
    assert result["samples"][2]["observation"] == changed


@pytest.mark.parametrize("change", ["process", "launch", "kernel", "session", "display_id", "window_index"])
def test_settle_same_semantic_state_cannot_hide_changed_current_phase_context(monkeypatch, change):
    first = observe()
    backend = Backend(pid=777 if change == "process" else 42,
                      launched=2000.5 if change == "launch" else 1000.25,
                      display_id=18 if change == "display_id" else 4)
    if change == "kernel": backend.current["kernel"]["started_at"] = "different kernel epoch"
    elif change == "session": backend.current["session"]["asid"] += 1
    elif change == "window_index": backend.app.values["AXChildren"][1].insert(0, Node("AXGroup"))
    changed = observe(backend)
    assert changed["complete"] and changed["state"] == first["state"]
    result, _, calls = scripted_settle(monkeypatch, [first, changed, first, first])
    assert not result["complete"] and result["issues"] == [{"kind": "context_changed"}]
    assert result["stable_pair"] is None and len(calls) == 2
    assert result["expected_identity"]["pid"] == 42 and result["samples"][1]["observation"] == changed
    assert S.validate_settlement(result) == result


def test_settle_context_from_incomplete_sample_also_stays_fixed(monkeypatch):
    backend = Backend()
    backend.checkbox.values["AXValue"] = (-25204, None)
    first = observe(backend)
    assert not first["complete"] and "before" in first["context"]
    changed_backend = Backend(display_id=18)
    changed = observe(changed_backend)
    result, _, calls = scripted_settle(monkeypatch, [first, changed, changed])
    assert not result["complete"] and result["issues"] == [{"kind": "context_changed"}] and len(calls) == 2


@pytest.mark.parametrize("maximum", [1, 3])
def test_settle_sample_limit_retains_every_incomplete_observation(monkeypatch, maximum):
    backend = Backend()
    backend.checkbox.values["AXValue"] = (-25204, None)
    incomplete = observe(backend)
    result, _, calls = scripted_settle(monkeypatch, [incomplete] * maximum, max_samples=maximum)
    assert not result["complete"] and result["issues"] == [{"kind": "sample_limit"}]
    assert len(calls) == len(result["samples"]) == maximum and result["stable_pair"] is None
    assert all(row["observation"] == incomplete for row in result["samples"])
    assert S.validate_settlement(result) == result


def test_settle_deadline_stops_before_another_read_and_retains_first_observation(monkeypatch):
    first = observe()
    result, timer, calls = scripted_settle(monkeypatch, [first, first], seconds=.2)
    assert not result["complete"] and result["issues"] == [{"kind": "deadline"}] and len(calls) == 1
    assert timer.clock() >= result["deadline_monotonic"] and result["samples"][0]["observation"] == first
    assert S.validate_settlement(result) == result


def test_settle_late_returned_complete_observation_cannot_authorize_a_pair(monkeypatch):
    first = observe()
    result, timer, calls = scripted_settle(monkeypatch, [first, first], seconds=.1, capture_seconds=.2)
    assert not result["complete"] and result["issues"] == [{"kind": "deadline"}] and len(calls) == 1
    assert result["samples"][0]["observation"]["complete"] and timer.clock() >= result["deadline_monotonic"]
    assert S.validate_settlement(result) == result


def test_settle_capture_exception_is_retained_without_fabricating_a_snapshot(monkeypatch):
    first = observe()
    result, _, calls = scripted_settle(monkeypatch, [first, RuntimeError("ordinary observer error"), first, first])
    assert not result["complete"] and result["issues"] == [{"kind": "capture_error"}] and len(calls) == 2
    assert result["samples"][0]["observation"] == first
    assert result["samples"][1]["observation"] is None and result["samples"][1]["error"] == {"type": "RuntimeError"}
    assert S.validate_settlement(result) == result


def test_settle_final_validation_cannot_carry_success_beyond_deadline(monkeypatch):
    observation, timer = observe(), PhaseClock()
    calls = []
    def sample(*args, **kwargs):
        calls.append(True)
        timer.now += .05
        return copy.deepcopy(observation)
    original = S.validate_settlement
    def delayed(value):
        validated = original(value)
        timer.now = value["deadline_monotonic"] + 1
        return validated
    monkeypatch.setattr(S, "_capture", sample)
    monkeypatch.setattr(S, "validate_settlement", delayed)
    result = S._settle(TARGET, Backend().identity, SELECTOR, deadline=110.0, interval=.4, max_samples=16,
                       backend=object(), clock=timer.clock, sleep=timer.sleep)
    assert len(calls) == 2 and not result["complete"] and result["stable_pair"] is None
    assert result["issues"] == [{"kind": "deadline"}] and result["finished_monotonic"] >= result["deadline_monotonic"]
    assert original(result) == result


@pytest.mark.parametrize("change", ["pair_type", "index", "cadence", "identity", "late", "missing_observation"])
def test_retained_settlement_validation_binds_pair_timing_identity_and_raw_samples(monkeypatch, change):
    first = observe()
    result, _, _ = scripted_settle(monkeypatch, [first, first])
    assert S.validate_settlement(result) == result
    if change == "pair_type": result["stable_pair"] = [False, True]
    elif change == "index": result["samples"][1]["index"] = True
    elif change == "cadence": result["samples"][1]["started_monotonic"] = result["samples"][0]["finished_monotonic"]
    elif change == "identity": result["expected_identity"]["launch_time"] += 1
    elif change == "late": result["finished_monotonic"] = result["deadline_monotonic"]
    else: result["samples"][0]["observation"] = None
    with pytest.raises(ValueError): S.validate_settlement(result)


def test_retained_settlement_cannot_skip_an_earlier_stable_wrong_pair(monkeypatch):
    wrong = observe()
    backend = Backend()
    backend.checkbox.values["AXValue"] = (0, True)
    expected = observe(backend)
    result, _, _ = scripted_settle(monkeypatch, [wrong, wrong])
    for index in (2, 3):
        begin = result["samples"][-1]["finished_monotonic"] + result["interval"]
        result["samples"].append({"index": index, "started_monotonic": begin, "finished_monotonic": begin + .05,
                                  "observation": copy.deepcopy(expected), "error": None})
    result["stable_pair"] = [2, 3]
    result["finished_monotonic"] = result["samples"][-1]["finished_monotonic"]
    with pytest.raises(ValueError, match="FIRST stopping event"):
        S.validate_settlement(result)


def test_public_settle_guard_and_validated_input_copies_precede_backend_construction(monkeypatch):
    def deny(): raise PermissionError("VM only")
    monkeypatch.setattr(S.R, "assert_guest", deny)
    monkeypatch.setattr(S, "_Mac", lambda: pytest.fail("backend before VM guard"))
    with pytest.raises(PermissionError, match="VM only"):
        S.settle(None, None, None, deadline=0)
    backend, timer = Backend(), PhaseClock()
    target, identity, selector = copy.deepcopy(TARGET), copy.deepcopy(backend.identity), copy.deepcopy(SELECTOR)
    saved = copy.deepcopy((target, identity, selector))
    monkeypatch.setattr(S.R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.time, "monotonic", timer.clock)
    monkeypatch.setattr(S.time, "sleep", timer.sleep)
    def factory():
        target["sha256"] = "b" * 64
        identity["pid"] = 777
        selector["title"] = "successor"
        return backend
    monkeypatch.setattr(S, "_Mac", factory)
    result = S.settle(target, identity, selector, deadline=timer.now + 10)
    assert result["complete"] and result["target"] == saved[0] and result["expected_identity"] == saved[1]
    assert result["selector"] == saved[2] and identity["pid"] == 777
    assert set(backend.calls) <= {"context", "displays", "application", "timeout", "owner", *S.ATTRS.values()}


@pytest.mark.parametrize("change", ["interval_zero", "interval_bool", "sample_bool", "sample_bound", "deadline"])
def test_settle_rejects_invalid_budgets_before_backend_construction(monkeypatch, change):
    monkeypatch.setattr(S.R, "assert_guest", lambda: None)
    monkeypatch.setattr(S.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(S, "_Mac", lambda: pytest.fail("invalid budget reached backend"))
    kwargs = {"deadline": 110.0, "interval": .4, "max_samples": 16}
    if change == "interval_zero": kwargs["interval"] = 0
    elif change == "interval_bool": kwargs["interval"] = True
    elif change == "sample_bool": kwargs["max_samples"] = True
    elif change == "sample_bound": kwargs["max_samples"] = 17
    else: kwargs["deadline"] = 111.0
    with pytest.raises(ValueError): S.settle(TARGET, Backend().identity, SELECTOR, **kwargs)


class DisplayFirstBackend(Backend):
    """Inert GUI client whose focus query needs the ordinary display read."""
    def __init__(self):
        super().__init__()
        self.connected = False

    def displays(self, deadline):
        rows = super().displays(deadline)
        self.connected = True
        return rows

    def context(self, expected, deadline):
        if not self.connected:
            raise RuntimeError("ordinary display connection not initialized")
        return super().context(expected, deadline)


class FocusUnavailableBackend(DisplayFirstBackend):
    def context(self, expected, deadline):
        super().context(expected, deadline)
        raise RuntimeError("focused application unavailable")


def test_required_display_read_precedes_focus_without_extra_warmup_or_tree_filter():
    backend = DisplayFirstBackend()
    result = observe(backend)
    assert result["complete"] and result["issues"] == []
    assert backend.calls[:2] == ["displays", "context"]
    assert backend.calls.count("displays") == backend.context_count == 2
    assert result["context"]["displays"] == backend.screen_rows
    assert S.matches(result, contract(observe()))


def test_failed_focus_retains_full_displays_without_context_tree_or_success():
    backend = FocusUnavailableBackend()
    result = observe(backend)
    assert not result["complete"] and not result["proof_created"]
    assert result["issues"] == [{"kind": "snapshot_incomplete", "error_type": "RuntimeError"}]
    assert backend.calls == ["displays", "context"]
    assert "before" not in result["context"] and "window_app_child_index" not in result["context"]
    assert result["context"]["displays"] == backend.screen_rows
    assert result["state"]["displays"] == [{"position": [0.0, 0.0], "size": [1200.0, 800.0]}]
    assert result["state"]["focus"] == {"frontmost_owner": False, "focused_owner": False}
    assert result["state"]["nodes"] == [] and result["state"]["root"] is None


@pytest.mark.parametrize("change", ["locked", "wrong_focus", "wrong_owner"])
def test_display_first_order_preserves_locked_focus_and_owner_refusal_before_tree(change):
    backend = DisplayFirstBackend()
    if change == "locked": backend.current["session"]["locked"] = True
    elif change == "wrong_focus": backend.current["focus"]["focused_pid"] += 1
    else: backend.current["kernel"]["uid"] += 1
    result = observe(backend)
    assert not result["complete"] and result["issues"] and not result["proof_created"]
    assert backend.calls == ["displays", "context"]  # No application/tree read or actor method.
    assert result["state"]["nodes"] == [] and "before" not in result["context"]
    assert result["context"]["displays"] == backend.screen_rows


def test_slow_required_display_read_retains_its_claim_but_never_queries_focus_after_deadline():
    class SlowDisplayBackend(DisplayFirstBackend):
        def displays(self, deadline):
            rows = super().displays(deadline)
            self.now += .2
            return rows
    backend = SlowDisplayBackend()
    deadline = backend.now + .1
    result = observe(backend, deadline=deadline)
    assert not result["complete"] and result["issues"] == [{"kind": "phase_timeout"}]
    assert backend.clock() >= deadline and backend.calls == ["displays"]
    assert result["context"]["displays"] == backend.screen_rows
    assert result["state"]["nodes"] == [] and "before" not in result["context"]


@pytest.mark.parametrize("failure", ["query_error", "invalid_displays"])
def test_unacquired_display_failure_never_reaches_focus_or_fabricates_a_display_claim(failure):
    class BadDisplayBackend(DisplayFirstBackend):
        def displays(self, deadline):
            self.calls.append("displays")
            if failure == "query_error": raise RuntimeError("display query failed")
            return []
    backend = BadDisplayBackend()
    result = observe(backend)
    assert not result["complete"] and result["issues"]
    assert backend.calls == ["displays"] and "displays" not in result["context"]
    assert result["state"]["displays"] == [] and result["state"]["nodes"] == []


def test_settle_binds_early_display_only_prefix_without_treating_it_as_a_complete_sample(monkeypatch):
    first = observe(FocusUnavailableBackend())
    complete = observe(DisplayFirstBackend())
    result, _, calls = scripted_settle(monkeypatch, [first, complete, complete])
    assert result["complete"] and result["stable_pair"] == [1, 2] and len(calls) == 3
    assert result["samples"][0]["observation"] == first
    assert S.validate_settlement(result) == result and not result["proof_created"]


@pytest.mark.parametrize("change", ["display_id", "position", "size"])
def test_settle_cannot_drop_display_drift_after_failed_focus_before_a_later_complete_sample(monkeypatch, change):
    first = observe(FocusUnavailableBackend())
    backend = DisplayFirstBackend()
    if change == "display_id": backend.screen_rows[0]["id"] += 1
    elif change == "position":
        backend.screen_rows[0]["position"][0] += 10
        backend.window.values["AXPosition"] = (0, tuple(backend.screen_rows[0]["position"]))
    else:
        backend.screen_rows[0]["size"][0] += 1
        backend.window.values["AXSize"] = (0, tuple(backend.screen_rows[0]["size"]))
    changed = observe(backend)
    assert changed["complete"]
    result, _, calls = scripted_settle(monkeypatch, [first, changed, changed])
    assert not result["complete"] and result["issues"] == [{"kind": "context_changed"}]
    assert result["stable_pair"] is None and len(calls) == 2
    assert result["samples"][0]["observation"] == first
    assert S.validate_settlement(result) == result


@pytest.mark.parametrize("change", ["state_geometry", "drop_context_displays", "window_before_context"])
def test_retained_early_display_claim_cannot_lose_its_full_projection_or_admit_a_window(monkeypatch, change):
    first = observe(FocusUnavailableBackend())
    result, _, _ = scripted_settle(monkeypatch, [first], max_samples=1)
    assert S.validate_settlement(result) == result
    # JSON retention has independent state/context geometry objects.
    changed = json.loads(json.dumps(result))
    observation = changed["samples"][0]["observation"]
    if change == "state_geometry": observation["state"]["displays"][0]["size"][0] += 1
    elif change == "drop_context_displays": observation["context"].pop("displays")
    else: observation["context"]["window_app_child_index"] = 0
    with pytest.raises(ValueError): S.validate_settlement(changed)


def test_historical_before_only_incomplete_prefix_stays_pending_without_inventing_displays(monkeypatch):
    complete = observe()
    legacy = copy.deepcopy(complete)
    legacy.update(complete=False, issues=[{"kind": "snapshot_incomplete", "error_type": "RuntimeError"}])
    legacy["context"] = {key: legacy["context"][key] for key in ("format", "target", "expected_identity", "before")}
    legacy["state"] = {"format": S.STATE, "displays": [], "focus": {"frontmost_owner": False, "focused_owner": False},
                       "root": None, "node_count": 0, "nodes": []}
    result, _, calls = scripted_settle(monkeypatch, [legacy, complete, complete])
    assert result["complete"] and result["stable_pair"] == [1, 2] and len(calls) == 3
    assert result["samples"][0]["observation"] == legacy and not legacy["complete"]
    assert S.validate_settlement(result) == result


def _inert_mac_context(tmp_path, monkeypatch, *, frontmost_pid=42, focus_available=False):
    """Exercise the actual context method with inert NS/ps/Security/AX APIs."""
    bundle = tmp_path.resolve() / "Context.app"
    executable = bundle / "Contents/MacOS/context"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"inert file, never executed")
    identity = {"bundle": str(bundle), "bundle_id": TARGET["bundle_id"], "executable": str(executable),
                "pid": 42, "uid": os.getuid(), "launch_time": 1000.25}
    calls, clock = [], {"now": 100.0}
    monkeypatch.setattr(S.time, "monotonic", lambda: clock["now"])
    app = SimpleNamespace(isTerminated=lambda: False,
        bundleURL=lambda: SimpleNamespace(path=lambda: str(bundle)),
        executableURL=lambda: SimpleNamespace(path=lambda: str(executable)),
        launchDate=lambda: SimpleNamespace(timeIntervalSince1970=lambda: identity["launch_time"]),
        bundleIdentifier=lambda: identity["bundle_id"], processIdentifier=lambda: identity["pid"])
    def application(pid):
        calls.append("NS-target")
        assert pid == identity["pid"]
        return app
    def frontmost():
        calls.append("NS-frontmost")
        return None if frontmost_pid is None else SimpleNamespace(processIdentifier=lambda: frontmost_pid)
    def query(argv, **kwargs):
        calls.append("kernel")
        assert argv == ["/bin/ps", "-o", "pid=,ppid=,uid=,lstart=,comm=", "-p", "42"]
        assert kwargs["capture_output"] is True and kwargs["check"] is False and 0 < kwargs["timeout"] <= 5
        assert kwargs["env"]["LC_ALL"] == "C" and kwargs["env"]["TZ"] == "UTC"
        raw = ("42 1 %d Sun Oct 18 10:00:00 2026 %s\n" % (identity["uid"], executable)).encode()
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
    monkeypatch.setattr(subprocess, "run", query)
    class SessionInfo:
        def __call__(self, session, asid, bits):
            calls.append("Security-session")
            assert session == 0xffffffff
            asid._obj.value, bits._obj.value = 23, 0x10
            return 0
    def security(path):
        assert path == "/System/Library/Frameworks/Security.framework/Security"
        return SimpleNamespace(SessionGetInfo=SessionInfo())
    monkeypatch.setattr(ctypes, "CDLL", security)
    def gui():
        calls.append("GUI-session")
        return {"uid": identity["uid"], "console": True, "kCGSessionLoginDoneKey": True,
                "CGSSessionScreenIsLocked": False}
    system, focused = object(), object()
    def system_wide():
        calls.append("AX-system")
        return system
    def attribute(element, name):
        calls.append(name)
        assert element is system and name == "AXFocusedApplication"
        return (0, focused) if focus_available else (-25204, None)
    def timeout(element, seconds):
        calls.append("AX-timeout")
        assert element is system or element is focused
        assert 0 < seconds <= S.AX_SECONDS
    def owner(element):
        calls.append("AX-focused-owner")
        assert element is focused
        return identity["pid"]
    backend = object.__new__(S._Mac)
    backend.K = SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=application),
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=frontmost)))
    backend.A = SimpleNamespace(AXUIElementCreateSystemWide=system_wide, AXIsProcessTrusted=lambda: True)
    backend.Q = SimpleNamespace(CGSessionCopyCurrentDictionary=gui, kCGSessionUserIDKey="uid", kCGSessionOnConsoleKey="console")
    backend.attribute, backend.timeout, backend.owner = attribute, timeout, owner
    return backend, identity, calls, clock


@pytest.mark.parametrize("frontmost_pid", [42, 91, None])
def test_real_mac_raw_preflight_skips_ax_focus_and_retains_actual_frontmost_and_unknown_focus(tmp_path, monkeypatch, frontmost_pid):
    backend, identity, calls, _ = _inert_mac_context(tmp_path, monkeypatch, frontmost_pid=frontmost_pid)
    result = backend.context(identity, 110.0, read_focus=False)
    assert result["identity"] == identity and result["kernel"]["pid"] == identity["pid"]
    assert result["kernel"]["uid"] == identity["uid"] and result["kernel"]["executable"] == identity["executable"]
    assert result["session"] == {"asid": 23, "attribute_bits": 0x10, "uid": identity["uid"], "on_console": True,
                                 "login_done": True, "locked": False, "ax_trusted": True}
    assert result["focus"] == {"frontmost_pid": frontmost_pid, "focused_pid": None}
    assert calls == ["NS-target", "kernel", "GUI-session", "Security-session", "NS-frontmost"]


@pytest.mark.parametrize("explicit", [False, True])
def test_real_mac_default_and_explicit_focus_stay_strict_when_ax_focus_is_unavailable(tmp_path, monkeypatch, explicit):
    backend, identity, calls, _ = _inert_mac_context(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="current AX focused application unavailable"):
        backend.context(identity, 110.0, **({"read_focus": True} if explicit else {}))
    assert calls == ["NS-target", "kernel", "GUI-session", "Security-session", "NS-frontmost",
                     "AX-system", "AX-timeout", "AXFocusedApplication"]


def test_real_mac_default_focus_still_reads_owner_and_produces_strict_context(tmp_path, monkeypatch):
    backend, identity, calls, _ = _inert_mac_context(tmp_path, monkeypatch, focus_available=True)
    result = backend.context(identity, 110.0)
    assert result["focus"] == {"frontmost_pid": identity["pid"], "focused_pid": identity["pid"]}
    assert S._check_context(result, identity) == result
    assert calls[-5:] == ["AX-system", "AX-timeout", "AXFocusedApplication", "AX-timeout", "AX-focused-owner"]


def test_raw_unknown_focus_is_rejected_by_strict_context_and_capture_without_tree_read(tmp_path, monkeypatch):
    backend, identity, calls, _ = _inert_mac_context(tmp_path, monkeypatch)
    raw = backend.context(identity, 110.0, read_focus=False)
    assert raw["focus"] == {"frontmost_pid": identity["pid"], "focused_pid": None}
    with pytest.raises(ValueError, match="frontmost and focused"):
        S._check_context(raw, identity)
    capture_backend = Backend()
    capture_backend.identity, capture_backend.current = copy.deepcopy(identity), copy.deepcopy(raw)
    result = observe(capture_backend)
    assert not result["complete"] and not result["proof_created"] and result["issues"]
    assert capture_backend.calls == ["displays", "context"]
    assert result["state"]["nodes"] == [] and "before" not in result["context"]
    assert "AXFocusedApplication" not in calls


@pytest.mark.parametrize("value", [0, 1, None, "false", [], {}])
def test_real_mac_preflight_option_requires_exact_bool_before_any_context_io(tmp_path, monkeypatch, value):
    backend, identity, calls, _ = _inert_mac_context(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="read_focus must be an exact boolean"):
        backend.context(identity, 110.0, read_focus=value)
    assert calls == []


def test_real_mac_no_focus_preflight_keeps_same_user_and_final_deadline_checks(tmp_path, monkeypatch):
    backend, identity, calls, clock = _inert_mac_context(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="same-user AX phase required"):
        backend.context({**identity, "uid": identity["uid"] + 1}, 110.0, read_focus=False)
    assert calls == []
    original = backend.K.NSWorkspace.sharedWorkspace
    def workspace():
        value = original()
        def frontmost():
            result = value.frontmostApplication()
            clock["now"] = 110.0
            return result
        return SimpleNamespace(frontmostApplication=frontmost)
    backend.K.NSWorkspace.sharedWorkspace = workspace
    with pytest.raises(ValueError, match="AX focus phase expired"):
        backend.context(identity, 110.0, read_focus=False)
    assert calls == ["NS-target", "kernel", "GUI-session", "Security-session", "NS-frontmost"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

