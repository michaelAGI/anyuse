"""Regression/unit tests for ``execution/blender_compile.py``.

``app_script`` is the first compile class whose recording source is an app's
own scripting API/operator log (Blender's ``window_manager.operators``) and
not the macOS accessibility tree every ``execution/desktop_compile.py`` class
uses. This file runs ``infer_class`` against four flows from REAL recordings
(``testdata/blender/*.json``, captured live from a running Blender 5.2.2 LTS
GUI session in the guest VM — not invented): one resolves to ``app_script``
(mesh add + move), three resolve to ``unresolved``, each for a distinct,
specific, empirically-observed reason (a bare ID-name property edit with no
operator entry at all for renaming an object or a material; a real,
filepath-parameterized, genuinely-executed operator that Blender simply never
adds to its own operator history for ``wm.save_as_mainfile``). Also tests
``generate_script`` produces the expected parameterised source, and the
generic ``prove()`` harness's own floor and its refusal to average away a
disagreement — independent of any one flow, same discipline
``test_desktop_compile_class_inference.py`` already established for the
AX-driven classes.

Run from the anyuse package dir:
    cd anyuse && python3 test_blender_compile_class_inference.py
"""
import json
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, (
    "resolved 'anyuse' to the wrong checkout — (D9)")

from anyuse.execution.blender_compile import (  # noqa: E402
    KNOWN_SHAPES, MIN_FRESH_CASES,
    generate_script, infer_class, prove, recording_from_json,
)

FIXTURES = _HERE / "testdata" / "blender"

#: flow file -> expected (op_class, subtype)
EXPECTED = {
    "flow1_mesh_add_move.json": ("app_script", "mesh_add_move"),
    "flow2_rename.json": ("unresolved", None),
    "flow3_material_add.json": ("unresolved", None),
    "flow4_save_as.json": ("unresolved", None),
}


def _load(name):
    with open(FIXTURES / name) as fh:
        return recording_from_json(json.load(fh))


def test_all_four_flows_classify_from_real_recordings():
    for name, (op_class, subtype) in EXPECTED.items():
        rec = _load(name)
        inf = infer_class(rec)
        assert inf.op_class == op_class, f"{name}: expected {op_class!r}, got {inf.op_class!r} ({inf.reason})"
        assert inf.subtype == subtype, f"{name}: expected subtype {subtype!r}, got {inf.subtype!r}"
    print(f"OK  all {len(EXPECTED)} real guest recordings classify as expected: "
          f"{ {k: v for k, v in EXPECTED.items()} }")


def test_mesh_add_move_evidence_names_the_real_translate_value():
    rec = _load("flow1_mesh_add_move.json")
    inf = infer_class(rec)
    assert inf.op_class == "app_script"
    assert any("TRANSFORM_OT_translate.value=" in e for e in inf.evidence)
    assert "[2.5, -1.0, 0.75]" in inf.evidence[0] or "2.5" in inf.evidence[0]
    print(f"OK  mesh_add_move evidence cites the real logged value: {inf.evidence}")


def test_rename_is_unresolved_because_the_operator_log_is_empty():
    rec = _load("flow2_rename.json")
    assert rec.ops == (), "fixture should reflect the real recording: F2 logged NO operator at all"
    inf = infer_class(rec)
    assert inf.op_class == "unresolved"
    assert "()" in inf.reason or "matches no known" in inf.reason
    print("OK  rename: unresolved because window_manager.operators stayed empty (real F2 behaviour)")


def test_material_add_is_unresolved_because_new_material_takes_no_name_kwarg():
    rec = _load("flow3_material_add.json")
    assert rec.ops[-1].idname == "MATERIAL_OT_new"
    assert rec.ops[-1].kwargs == {}, "MATERIAL_OT_new really is recorded with empty kwargs"
    inf = infer_class(rec)
    assert inf.op_class == "unresolved"
    print("OK  material_add: unresolved because MATERIAL_OT_new carries no name argument "
          "(real recorded kwargs are empty)")


def test_save_as_is_unresolved_because_the_save_operator_was_never_logged():
    rec = _load("flow4_save_as.json")
    assert not any(op.idname == "WM_OT_save_as_mainfile" for op in rec.ops), (
        "the real recording's operator log genuinely never gained a save_as_mainfile entry, "
        "even though the file was demonstrably written under the new name on disk"
    )
    inf = infer_class(rec)
    assert inf.op_class == "unresolved"
    print("OK  save_as: unresolved because wm.save_as_mainfile never appears in the operator "
          "log at all (a real, filepath-parameterized operator Blender simply does not register)")


def test_unresolved_beats_a_guess_for_an_unfamiliar_operator_sequence():
    from anyuse.execution.blender_compile import BlenderRecording, RecordedOperator
    rec = BlenderRecording(flow="made_up", ops=(
        RecordedOperator(idname="OBJECT_OT_made_up", kwargs={"value": 1.0}),
    ))
    inf = infer_class(rec)
    assert inf.op_class == "unresolved"
    print("OK  an operator sequence absent from KNOWN_SHAPES is unresolved, never a guess")


def test_generate_script_bakes_constants_and_parameterizes_the_translate_value():
    rec = _load("flow1_mesh_add_move.json")
    inf = infer_class(rec)
    src = generate_script(rec, inf)
    assert "bpy.ops.mesh.primitive_cube_add(size=2.0" in src
    assert "align='WORLD'" in src
    assert "bpy.ops.transform.translate(value=(p0, p1, p2)" in src
    assert "orient_type='GLOBAL'" in src
    assert "snap_elements={'INCREMENT'}" in src, "an ENUM_FLAG kwarg must render as a Python set, not a tuple"
    assert "capture.dump_scene_state()" in src
    compile(src, "<generated>", "exec")  # syntactically valid Python
    print("OK  generate_script produces syntactically valid Python: constants baked in "
          "verbatim, the recorded (x,y,z) replaced by argv-fed parameters")


def test_generate_script_refuses_an_unresolved_inference():
    rec = _load("flow2_rename.json")
    inf = infer_class(rec)
    try:
        generate_script(rec, inf)
        raise AssertionError("generate_script must refuse an unresolved inference")
    except ValueError as exc:
        assert "unresolved" in str(exc)
    print("OK  generate_script refuses to compile an unresolved recording "
          "(nothing to compile until a recording resolves to a class)")


#: base_empty.blend's two objects (testdata/blender/flow1_mesh_add_move.json
#: before_state), the declared starting scene of every proof row.
START = {"objects": [
    {"name": "Camera", "type": "CAMERA", "location": [7.358891, -6.925791, 4.958309]},
    {"name": "Light", "type": "LIGHT", "location": [4.076245, 1.005454, 5.903862]}]}
RECORDED = [(2.5, -1.0, 0.75)]
HELD_BACK = [(1.0, 2.0, -3.0), (-0.5, 4.25, 0.5)]


def _state(loc, name="Cube", typ="MESH"):
    return {"objects": START["objects"] + [
        {"name": name, "type": typ, "location": list(loc), "rotation_euler": [0, 0, 0],
         "scale": [1, 1, 1], "active_material": None, "material_slots": [], "modifiers": []}]}


def _prove(inputs=HELD_BACK, ui_fn=_state, script_fn=_state, recorded=RECORDED, start=START):
    return prove("mesh_add_move", "mesh_add_move", inputs=inputs, ui_fn=ui_fn, script_fn=script_fn,
                 recorded_inputs=recorded, start_state=start)


def test_prove_refuses_below_min_fresh_cases():
    report = _prove(inputs=[(1.0, 2.0, 3.0)])
    assert report.refusal_reason is not None and "distinct unseen input" in report.refusal_reason
    assert not report.proved
    print(f"OK  prove() refuses with only 1 distinct input (< MIN_FRESH_CASES={MIN_FRESH_CASES})")


def test_prove_passes_when_every_row_agrees():
    report = _prove()
    assert report.proved and report.refusal_reason is None, report.to_dict()
    assert len(report.rows) == 2
    print("OK  prove() proves mesh_add_move when both states agree and both meet the expectation")


def test_prove_surfaces_a_single_mismatch_never_hides_it():
    def script_state(loc):
        # models a hypothetical off-by-sign compile bug on ONE input only
        bad = loc == (1.0, 2.0, -3.0)
        return _state((-loc[0], loc[1], loc[2]) if bad else loc)

    report = _prove(script_fn=script_state)
    assert not report.proved, "a single disagreeing input must fail the whole proof"
    mismatches = [r for r in report.rows if not r.agree]
    assert len(mismatches) == 1 and mismatches[0].input == (1.0, 2.0, -3.0)
    agree = [r for r in report.rows if r.agree]
    assert len(agree) == 1 and agree[0].input == (-0.5, 4.25, 0.5)
    print("OK  prove() fails the whole report on one mismatching input but keeps both rows visible")


def test_prove_fails_on_a_shared_empty_observation():
    """Two callbacks that both return {} on two distinct inputs used to PASS
    (missing 'objects' defaulted to [])."""
    for empty in ({}, {"objects": None}, None, {"objects": []}):
        report = _prove(ui_fn=lambda i, e=empty: e, script_fn=lambda i, e=empty: e)
        assert not report.proved, f"shared observation {empty!r} must not prove"
        assert all(not r.agree for r in report.rows)
    print("OK  prove() fails when both paths return the same empty/invalid observation")


def test_prove_fails_on_a_shared_wrong_result():
    """Both paths agree with each other but not with the request: the cube
    never moved (or nothing new was created). Agreement alone is not enough."""
    report = _prove(ui_fn=lambda i: _state((0.0, 0.0, 0.0)), script_fn=lambda i: _state((0.0, 0.0, 0.0)))
    assert not report.proved and "requested" in report.rows[0].mismatch
    report = _prove(ui_fn=lambda i: START, script_fn=lambda i: START)
    assert not report.proved and "exactly one new object" in report.rows[0].mismatch
    report = _prove(ui_fn=lambda i: _state(i, typ="EMPTY"), script_fn=lambda i: _state(i, typ="EMPTY"))
    assert not report.proved and "not MESH" in report.rows[0].mismatch
    print("OK  prove() fails when both paths agree on a result that is not the requested one")


def test_prove_refuses_a_reused_recording_input():
    report = _prove(inputs=[RECORDED[0], HELD_BACK[0]])
    assert not report.proved and report.refusal_reason and "already used" in report.refusal_reason
    try:
        prove("mesh_add_move", "mesh_add_move", inputs=HELD_BACK, ui_fn=_state, script_fn=_state,
              start_state=START)
        raise AssertionError("recorded_inputs must be a required argument")
    except TypeError:
        pass
    print("OK  prove() refuses an input the recording used, and requires recorded_inputs")


def test_prove_requires_a_valid_start_state_and_a_known_shape():
    assert _prove(start={}).refusal_reason.startswith("start_state")
    r = prove("x", "not_a_shape", inputs=HELD_BACK, ui_fn=_state, script_fn=_state,
              recorded_inputs=RECORDED, start_state=START)
    assert not r.proved and "no expectation" in r.refusal_reason
    print("OK  prove() refuses without a valid starting state or a registered expectation")


def test_prior_proof_re_verified_under_the_hardened_prove():
    """The archived proof's 2/2 was computed before
    prove() validated observations or enforced unseen inputs. Its archived
    GUI and script dumps (testdata/blender/proof/) are re-checked here with
    the recording's own input excluded and base_empty.blend's scene as the
    declared start."""
    d = FIXTURES / "proof"
    ui = {(1.0, 2.0, -3.0): json.load(open(d / "gui_flow1_B.json"))["state"],
          (-0.5, 4.25, 0.5): json.load(open(d / "gui_flow1_C.json"))["state"]}
    sc = {(1.0, 2.0, -3.0): json.load(open(d / "script_flow1_B.json")),
          (-0.5, 4.25, 0.5): json.load(open(d / "script_flow1_C.json"))}
    start = _load("flow1_mesh_add_move.json").before_state
    report = prove("mesh_add_move", "mesh_add_move", inputs=list(ui), ui_fn=ui.__getitem__,
                   script_fn=sc.__getitem__, recorded_inputs=RECORDED, start_state=start)
    assert report.proved, report.to_dict()
    print("OK  archived GUI/script dumps still prove 2/2 under the hardened prove()")


TESTS = [
    test_all_four_flows_classify_from_real_recordings,
    test_mesh_add_move_evidence_names_the_real_translate_value,
    test_rename_is_unresolved_because_the_operator_log_is_empty,
    test_material_add_is_unresolved_because_new_material_takes_no_name_kwarg,
    test_save_as_is_unresolved_because_the_save_operator_was_never_logged,
    test_unresolved_beats_a_guess_for_an_unfamiliar_operator_sequence,
    test_generate_script_bakes_constants_and_parameterizes_the_translate_value,
    test_generate_script_refuses_an_unresolved_inference,
    test_prove_refuses_below_min_fresh_cases,
    test_prove_passes_when_every_row_agrees,
    test_prove_surfaces_a_single_mismatch_never_hides_it,
    test_prove_fails_on_a_shared_empty_observation,
    test_prove_fails_on_a_shared_wrong_result,
    test_prove_refuses_a_reused_recording_input,
    test_prove_requires_a_valid_start_state_and_a_known_shape,
    test_prior_proof_re_verified_under_the_hardened_prove,
]


def main():
    print("=" * 70)
    print("blender_compile.py class inference + script generator + proof (guest fixtures)")
    print("=" * 70)
    for t in TESTS:
        t()
    print("\nBLENDER COMPILE CLASS INFERENCE TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
