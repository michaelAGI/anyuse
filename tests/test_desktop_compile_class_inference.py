"""Regression/unit tests for ``execution/desktop_compile.py``.

The question is "why isn't desktop compile generic?" — this file is
the offline proof that the class-inference rules are genuinely a function of
the recorded action vocabulary (role, bundle id, keystroke identity), not of
which script or which flow name produced the recording: every one of the ten
guest flows plus the HIL flow is
run through ``infer_class`` here from a REAL recording — extracted from the
guest corpus (``testdata/ax/desktop_recorded_steps.json``,
real ``AXIdentifier``s, real menu-item command characters, real German
labels), not a synthetic stand-in — except flow 7 (Calculator), whose guest
JSON recorded only a step COUNT (``execution/guest/flows.py``'s
``flow7_calculator`` returns ``"selectors": len(rec.steps)``, not the steps
themselves); its fixture entry is reconstructed with that same function's own
recording rule (every AXButton whose AXIdentifier is one of ``CALC_IDS``'s
values) over the real captured identifiers in
``testdata/ax/calculator_ax_raw.json`` — see the extraction script's ``meta``
block in the fixture file for exactly how.

Also tests the generic ``prove()`` harness's own floors and its refusal to
average away a disagreement, independent of any one class.

Run from the anyuse package dir:
    cd anyuse && python3 test_desktop_compile_class_inference.py
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

from anyuse.execution.desktop_compile import (  # noqa: E402
    ClassInference, DesktopRecording, RecordedStep, CLASS_OPERATIONS,
    MIN_FRESH_CASES, MIN_RUNS_PER_INPUT,
    direct_arithmetic, direct_directory_create, direct_directory_rename,
    direct_file_search, direct_file_write, direct_read_value,
    infer_class, operation_for, prove, read_directory_exists,
    read_directory_renamed, read_file_write, recording_from_flow_json,
)

FIXTURES = _HERE / "testdata" / "ax"

#: flow key -> (expected op_class, expected subtype)
EXPECTED = {
    "1 textedit_write_save": ("file_write", None),
    "2 textedit_find": ("unresolved", None),
    "3 finder_create_folder": ("directory_op", "create"),
    "4 finder_search": ("file_search", None),
    "5 notes_create": ("unresolved", None),
    "6 notes_search": ("unresolved", None),
    "7 calculator_arithmetic": ("arithmetic", None),
    "8 settings_read": ("read_value", None),
    "9 safari_list": ("unresolved", None),
    "10 safari_detail": ("unresolved", None),
    "H hil_delete": ("unresolved", None),
}


def _load_flows():
    data = json.loads((FIXTURES / "desktop_recorded_steps.json").read_text())
    return data["flows"]


def test_all_ten_flows_plus_hil_classify_from_real_recordings():
    flows = _load_flows()
    assert set(flows) == set(EXPECTED), f"fixture/expectation mismatch: {set(flows) ^ set(EXPECTED)}"
    for key, (want_class, want_subtype) in EXPECTED.items():
        entry = dict(flows[key])
        entry["flow"] = key
        rec = recording_from_flow_json(entry)
        inf = infer_class(rec)
        print(f"  {key:26s} bundle_id={rec.bundle_id:32s} steps={len(rec.steps)} "
              f"-> {inf.op_class}/{inf.subtype}  ({inf.reason})")
        assert inf.op_class == want_class, f"{key}: got {inf.op_class!r}, want {want_class!r} ({inf.reason})"
        assert inf.subtype == want_subtype, f"{key}: subtype {inf.subtype!r}, want {want_subtype!r}"
        if inf.op_class != "unresolved":
            assert inf.reason, f"{key}: a resolved class must say why"
            assert operation_for(inf) is not None, f"{key}: {inf.op_class}/{inf.subtype} has no registered operation"
        else:
            assert operation_for(inf) is None
    print("OK  all 10 flows + HIL classify correctly from real recorded_steps, never a guess")


def test_calculator_reconstruction_used_real_identifiers_not_a_shortcut():
    flows = _load_flows()
    calc = flows["7 calculator_arithmetic"]["recorded_steps"]
    # every step's identifier must be a real value from CALC_IDS, e.g. "Seven"/"Add"/"Equals" —
    # if the extraction script had cut a corner this would be empty or synthetic.
    idents = {s["identity"]["identifier"] for s in calc.values()}
    assert {"Seven", "Add", "Equals", "AllClear"} <= idents, idents
    print(f"OK  Calculator fixture carries {len(calc)} real AXButton identifiers: {sorted(idents)}")


def test_unresolved_beats_a_guess_for_an_unknown_app():
    rec = DesktopRecording(flow="x", app="SomeApp", bundle_id="com.example.unknown",
                           steps=(RecordedStep(name="a", role="AXButton"),))
    inf = infer_class(rec)
    assert inf.op_class == "unresolved", inf
    assert "no class rule" in inf.reason
    print("OK  an app with no rule is unresolved, not guessed into the nearest class")


def test_textedit_with_no_save_step_is_unresolved_not_file_write():
    """A text-entry recording with nothing proving a write (no Save step) —
    e.g. flow 2's find-in-document — must not be classified file_write just
    because an AXTextArea was touched."""
    rec = DesktopRecording(flow="x", app="TextEdit", bundle_id="com.apple.TextEdit",
                           steps=(RecordedStep(name="area", role="AXTextArea"),
                                  RecordedStep(name="edit_menu", role="AXMenuBarItem"),
                                  RecordedStep(name="find_item", role="AXMenuItem", cmd="f")))
    inf = infer_class(rec)
    assert inf.op_class == "unresolved", inf
    assert "Save" in inf.reason
    print("OK  AXTextArea + a non-Save menu item stays unresolved (find, not a write)")


def test_directory_op_rename_subtype_is_structurally_distinguished():
    """No 'rename' flow exists among the ten guest flows — this
    fixture is CONSTRUCTED (documented as such, not a guest capture) to
    exercise the rule's rename branch: a Finder text-field step reached
    through a menu item that is NOT New Folder (any cmd other than 'n'/'1')
    must resolve to directory_op/rename, structurally, from the same rule
    that resolves flow 3 to directory_op/create."""
    rec = DesktopRecording(flow="synthetic_rename", app="Finder", bundle_id="com.apple.finder",
                           steps=(RecordedStep(name="file_menu", role="AXMenuBarItem"),
                                  RecordedStep(name="rename_item", role="AXMenuItem",
                                              title="Umbenennen", cmd="", mods=""),
                                  RecordedStep(name="name_field", role="AXTextField",
                                              identifier="ShrinkToFit Text Field")))
    inf = infer_class(rec)
    assert inf.op_class == "directory_op" and inf.subtype == "rename", inf
    op = operation_for(inf)
    assert op is not None and op.direct is direct_directory_rename
    print("OK  a text-field step reached via a non-New-Folder menu item resolves to directory_op/rename")

    # And the real flow-3 (create) recording must NOT take this branch.
    flows = _load_flows()
    entry = dict(flows["3 finder_create_folder"]); entry["flow"] = "3 finder_create_folder"
    create_rec = recording_from_flow_json(entry)
    create_inf = infer_class(create_rec)
    assert create_inf.subtype == "create", create_inf
    print("OK  the real finder_create_folder recording still resolves to directory_op/create, not rename")


def test_directory_op_rename_via_bare_keystroke_is_also_recognized():
    """Finder's OTHER standard rename trigger — select an item, press Return
    — has no menu item and no button at all, just the same inline text field
    create uses. This must ALSO resolve to directory_op/rename (not
    unresolved, and not confused with file_search, which always has a
    button)."""
    rec = DesktopRecording(flow="synthetic_rename_keystroke", app="Finder", bundle_id="com.apple.finder",
                           steps=(RecordedStep(name="name_field", role="AXTextField",
                                              identifier="ShrinkToFit Text Field"),))
    inf = infer_class(rec)
    assert inf.op_class == "directory_op" and inf.subtype == "rename", inf
    print("OK  a bare text-field-only Finder recording (select + Return) also resolves to directory_op/rename")


# ---------------------------------------------------------------------------
# The direct operations themselves, on a real (temp) filesystem — no VM
# needed, no subprocess, no coordinate ever touched.
# ---------------------------------------------------------------------------

def test_file_write_direct_and_read_agree():
    with tempfile.TemporaryDirectory() as td:
        path = f"{td}/doc.txt"
        direct_file_write(path, "anyuse desktop validation zeta 0")
        assert read_file_write(path) == "anyuse desktop validation zeta 0"
        assert read_file_write(f"{td}/missing.txt") is None
    print("OK  file_write direct op + postcondition reader agree on a real temp file")


def test_directory_create_and_rename_direct_ops():
    with tempfile.TemporaryDirectory() as td:
        target = f"{td}/tamarind"
        direct_directory_create(target)
        assert read_directory_exists(target)
        try:
            direct_directory_create(target)
            raise AssertionError("a second makedirs on the same path should have raised")
        except FileExistsError:
            pass
        dst = f"{td}/tamarind_renamed"
        direct_directory_rename(target, dst)
        assert read_directory_renamed(target, dst)
        assert not os_path_isdir(target)
    print("OK  directory_op create + rename direct ops behave and their postconditions agree")


def os_path_isdir(p):
    import os
    return os.path.isdir(p)


def test_arithmetic_direct_matches_python():
    assert direct_arithmetic(7, "*", 6) == 42
    assert direct_arithmetic(144, "/", 12) == 12
    assert direct_arithmetic(99, "-", 57) == 42
    print("OK  arithmetic direct op matches plain python arithmetic")


def test_read_value_uses_injected_backend_never_a_real_subprocess_in_this_test():
    calls = []

    def fake_backend(label):
        calls.append(label)
        return {"Version": "26.6.2", "Speicher": "12 GB"}[label]

    assert direct_read_value("Version", backend=fake_backend) == "26.6.2"
    assert direct_read_value("Speicher", backend=fake_backend) == "12 GB"
    assert calls == ["Version", "Speicher"]
    print("OK  read_value dispatches through an injected backend (no subprocess spawned here)")


def test_file_search_direct_uses_injected_backend_and_surfaces_the_berry_case():
    """The case: 'berry' inside 'blueberry'. The
    class's direct op is a literal substring match, so an injected backend
    modelling that DOES return blueberry_pie.txt for pattern 'berry' — this
    test is about the direct op's own contract, not about Finder's UI (that
    disagreement is measured in the guest, not reproduced here)."""
    seed = ["apple_pie.txt", "banana_split.txt", "blueberry_pie.txt", "fig_roll.txt"]

    def substring_backend(folder, pattern):
        return [n for n in seed if pattern.lower() in n.lower()]

    assert direct_file_search("/scratch/finder", "pie", backend=substring_backend) == \
        ["apple_pie.txt", "blueberry_pie.txt"]
    assert direct_file_search("/scratch/finder", "berry", backend=substring_backend) == \
        ["blueberry_pie.txt"]
    print("OK  file_search's literal-substring contract matches 'berry' inside 'blueberry' "
          "(the mismatch with Finder's own search field is a UI-vs-direct finding, not a bug here)")


# ---------------------------------------------------------------------------
# The generic proof harness — floors and mismatch surfacing, independent of
# any one class.
# ---------------------------------------------------------------------------

FW_START = {"text": "seed\n", "sentinel": "the other document\n"}


def _fw_expect(start, inp, run, obs):
    want = f"line-{inp}-{run}"
    return None if obs["text"] == want else f"text {obs['text']!r} != {want!r}"


def _fw(text, sentinel="the other document\n"):
    return {"text": text, "sentinel": sentinel}


FW_KW = dict(recorded_inputs=["alpha"], start_state=FW_START, expect=_fw_expect)


def test_prove_refuses_below_min_fresh_cases():
    op = CLASS_OPERATIONS["file_write"]
    report = prove(op, inputs=["zeta"], runs_per_input=3,
                   ui_fn=lambda i, r: _fw(f"line-{i}-{r}"), direct_fn=lambda i, r: _fw(f"line-{i}-{r}"),
                   **FW_KW)
    assert report.refusal_reason is not None and "distinct unseen input" in report.refusal_reason
    assert report.rows == []
    assert not report.proved
    print(f"OK  prove() refuses with only 1 distinct input (< MIN_FRESH_CASES={MIN_FRESH_CASES})")


def test_prove_refuses_below_min_runs_per_input():
    op = CLASS_OPERATIONS["file_write"]
    report = prove(op, inputs=["zeta", "eta"], runs_per_input=2,
                   ui_fn=lambda i, r: _fw(f"line-{i}-{r}"), direct_fn=lambda i, r: _fw(f"line-{i}-{r}"),
                   **FW_KW)
    assert report.refusal_reason is not None and "runs_per_input" in report.refusal_reason
    print(f"OK  prove() refuses runs_per_input=2 (< MIN_RUNS_PER_INPUT={MIN_RUNS_PER_INPUT})")


def test_prove_passes_when_every_row_agrees():
    op = CLASS_OPERATIONS["file_write"]
    report = prove(op, inputs=["zeta", "eta"], runs_per_input=3,
                   ui_fn=lambda i, r: _fw(f"line-{i}-{r}"), direct_fn=lambda i, r: _fw(f"line-{i}-{r}"),
                   **FW_KW)
    assert report.proved and report.refusal_reason is None
    assert len(report.rows) == 2 * 3
    assert report.to_dict()["mismatches"] == []
    print("OK  prove() proves when UI and direct agree on every one of 2x3 rows")


def test_prove_requires_recorded_inputs_and_excludes_them():
    op = CLASS_OPERATIONS["file_write"]
    ok = lambda i, r: _fw(f"line-{i}-{r}")  # noqa: E731
    try:
        prove(op, ["zeta", "eta"], 3, ok, ok, start_state=FW_START, expect=_fw_expect)
        raise AssertionError("recorded_inputs must be a required argument")
    except TypeError:
        pass
    rep = prove(op, ["zeta", "alpha"], 3, ok, ok, **FW_KW)
    assert rep.rows == [] and "already used" in rep.refusal_reason
    print("OK  prove(): recorded_inputs is required, and a recorded input refuses the proof")


def test_prove_requires_a_valid_declared_start_state_and_expectations():
    op = CLASS_OPERATIONS["file_write"]
    ok = lambda i, r: _fw(f"line-{i}-{r}")  # noqa: E731
    for start, why in ((None, "no declared start state"), ({}, "empty observation"),
                       ({"text": "seed"}, "sentinel")):
        rep = prove(op, ["zeta", "eta"], 3, ok, ok, recorded_inputs=[], start_state=start,
                    expect=_fw_expect)
        assert rep.rows == [] and why in rep.refusal_reason, rep.refusal_reason
    rep = prove(op, ["zeta", "eta"], 3, ok, ok, recorded_inputs=[], start_state=FW_START)
    assert rep.rows == [] and "no expected state delta" in rep.refusal_reason
    rep = prove(CLASS_OPERATIONS["read_value"], ["Version", "Chip"], 3,
                lambda i, r: {"value": "x"}, lambda i, r: {"value": "x"},
                recorded_inputs=[], start_state={"value": "-"})
    assert rep.rows == [] and "no expected state delta" in rep.refusal_reason
    print("OK  prove() refuses without a valid declared start state or an expected delta")


def test_prove_rejects_injected_shared_failures():
    """Both paths fail the SAME way. Plain equality would call every one of
    these 'agree'; the hardened prover must fail every row."""
    op = CLASS_OPERATIONS["file_write"]
    shared = {
        "both None": lambda i, r: None,
        "both the same error": lambda i, r: {"error": "no window"},
        "both empty": lambda i, r: {},
        "both unchanged (start state)": lambda i, r: dict(FW_START),
        "both raise": lambda i, r: (_ for _ in ()).throw(RuntimeError("AX timeout")),
        "both wrote the same wrong text": lambda i, r: _fw("line-something-else"),
        "both clobbered the sentinel": lambda i, r: _fw(f"line-{i}-{r}", sentinel="overwritten"),
    }
    for name, fn in shared.items():
        rep = prove(op, ["zeta", "eta"], 3, fn, fn, **FW_KW)
        assert not rep.proved and rep.rows and not any(r.agree for r in rep.rows), name
        assert all(r.mismatch for r in rep.rows), name
    print(f"OK  prove() rejects all {len(shared)} injected shared failures on every row "
          "(None, error, empty, unchanged, exception, same wrong value, same preserved-state violation)")


def test_prove_one_sided_failure_and_reset_check():
    op = CLASS_OPERATIONS["file_write"]
    good = lambda i, r: _fw(f"line-{i}-{r}")  # noqa: E731
    rep = prove(op, ["zeta", "eta"], 3, lambda i, r: None, good, **FW_KW)
    assert not rep.proved and all("ui observation invalid" in r.mismatch for r in rep.rows)
    starts = {("eta", 2): {"text": "leftover from run 1", "sentinel": "the other document\n"}}
    rep = prove(op, ["zeta", "eta"], 3, good, good,
                observe_start=lambda i, r: starts.get((i, r), dict(FW_START)), **FW_KW)
    bad = [r for r in rep.rows if not r.agree]
    assert not rep.proved and len(bad) == 1 and "declared start state" in bad[0].mismatch
    print("OK  prove(): a one-sided invalid observation fails its row; a run whose reset missed "
          "the declared start state fails even though both paths agree")


def test_prove_surfaces_a_single_mismatch_never_hides_it():
    """The 'berry' case: one input among several disagrees. The WHOLE report
    must fail, and those rows must still be present in the output. Under
    the hardened prover an empty search result is a VALID observation (it
    carries a completion signal), so the failure is the equality, exactly
    as in the guest — not a validation
    artefact."""
    op = CLASS_OPERATIONS["file_search"]
    scope = ["apple_pie.txt", "blueberry_pie.txt", "fig_roll.txt"]

    def obs(names):
        return {"complete": True, "names": names, "scope": list(scope)}

    def ui_fn(pattern, run):
        # models Finder's own (Spotlight-backed) search: no mid-word match
        return obs({"pie": ["apple_pie.txt", "blueberry_pie.txt"], "berry": []}[pattern])

    def direct_fn(pattern, run):
        # models the literal-substring direct op: DOES match mid-word
        return obs({"pie": ["apple_pie.txt", "blueberry_pie.txt"], "berry": ["blueberry_pie.txt"]}[pattern])

    report = prove(op, inputs=["pie", "berry"], runs_per_input=3, ui_fn=ui_fn, direct_fn=direct_fn,
                   equal_fn=lambda a, b: set(a["names"]) == set(b["names"]),
                   recorded_inputs=["tart"], start_state=obs([]))
    assert not report.proved, "a single disagreeing input must fail the whole class proof"
    mism = report.to_dict()["mismatches"]
    assert len(mism) == 3, f"expected all 3 'berry' runs to be kept as mismatches, got {len(mism)}"
    assert all(m["input"] == "berry" and m["mismatch"] == "ui and direct observations differ"
               for m in mism)
    agree_rows = [r for r in report.rows if r.agree]
    assert len(agree_rows) == 3 and all(r.input == "pie" for r in agree_rows)
    # and an unfinished search is not an empty result
    rep = prove(op, ["pie", "berry"], 3, lambda p, r: {"complete": False, "names": [], "scope": scope},
                lambda p, r: {"complete": False, "names": [], "scope": scope},
                recorded_inputs=[], start_state=obs([]))
    assert not rep.proved and all("completion" in r.mismatch for r in rep.rows)
    print(f"OK  prove() fails the class overall on 'berry' but keeps all {len(mism)} mismatching rows "
          f"AND the 3 agreeing 'pie' rows visible; an unfinished search never passes as empty")


def test_directory_create_expectation_and_arithmetic_checks():
    from anyuse.execution.desktop_compile import expect_new_entry
    op = CLASS_OPERATIONS[("directory_op", "create")]
    expect, preserved = expect_new_entry(lambda n, r: f"{n}_{r}")
    start = {"entries": ["keep_me"]}
    good = lambda n, r: {"entries": ["keep_me", f"{n}_{r}"]}  # noqa: E731
    kw = dict(recorded_inputs=["papaya_rec"], start_state=start, expect=expect, preserved=preserved)
    assert prove(op, ["yuzu", "damson"], 3, good, good, **kw).proved
    # the old prover compared two booleans; True == True with nothing created proves nothing
    rep = prove(op, ["yuzu", "damson"], 3, lambda n, r: {"entries": ["keep_me"]},
                lambda n, r: {"entries": ["keep_me"]}, **kw)
    assert not rep.proved and all("not in the listing" in r.mismatch for r in rep.rows)
    rep = prove(op, ["yuzu", "damson"], 3, lambda n, r: {"entries": [f"{n}_{r}"]},
                lambda n, r: {"entries": [f"{n}_{r}"]}, **kw)
    assert not rep.proved and all("missing afterwards" in r.mismatch for r in rep.rows)
    ar = CLASS_OPERATIONS["arithmetic"]
    rep = prove(ar, [(318, "-", 45), (23, "*", 17)], 3, lambda i, r: {"display": [0.0]},
                lambda i, r: {"display": [0.0]}, recorded_inputs=[], start_state={"display": [0.0]})
    assert not rep.proved and all("does not show" in r.mismatch for r in rep.rows)
    print("OK  directory_op/create needs exactly the one new entry with the start intact; "
          "arithmetic's shared wrong display fails")


TESTS = [
    test_all_ten_flows_plus_hil_classify_from_real_recordings,
    test_calculator_reconstruction_used_real_identifiers_not_a_shortcut,
    test_unresolved_beats_a_guess_for_an_unknown_app,
    test_textedit_with_no_save_step_is_unresolved_not_file_write,
    test_directory_op_rename_subtype_is_structurally_distinguished,
    test_directory_op_rename_via_bare_keystroke_is_also_recognized,
    test_file_write_direct_and_read_agree,
    test_directory_create_and_rename_direct_ops,
    test_arithmetic_direct_matches_python,
    test_read_value_uses_injected_backend_never_a_real_subprocess_in_this_test,
    test_file_search_direct_uses_injected_backend_and_surfaces_the_berry_case,
    test_prove_refuses_below_min_fresh_cases,
    test_prove_refuses_below_min_runs_per_input,
    test_prove_passes_when_every_row_agrees,
    test_prove_requires_recorded_inputs_and_excludes_them,
    test_prove_requires_a_valid_declared_start_state_and_expectations,
    test_prove_rejects_injected_shared_failures,
    test_prove_one_sided_failure_and_reset_check,
    test_prove_surfaces_a_single_mismatch_never_hides_it,
    test_directory_create_expectation_and_arithmetic_checks,
]


def main():
    print("=" * 70)
    print("desktop_compile.py class inference + generic proof harness (offline fixtures)")
    print("=" * 70)
    for t in TESTS:
        t()
    print("\nDESKTOP COMPILE CLASS INFERENCE TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
