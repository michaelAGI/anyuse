"""Offline tests for ``execution/osa_compile.py`` (class ``osa_command``).

Pure: no pyobjc, no subprocess, no osascript, no Apple event. The dictionaries
are the REAL ones the guest read (``testdata/discovery/``); the
Cocoa standard suite TextEdit inherits implicitly is the guest's own
``CocoaStandard.sdef``, taken from the Notes dump's collected ``xi:include``.
Every runner and permission check is a fake.

Run from the anyuse package dir:
    python3 test_osa_compile.py
"""
import gzip
import json
import os
import pathlib
import sys
import tempfile

# The runner below is a fake: no osascript is spawned. The foreground interlock
# (execution/foreground_interlock.py) would otherwise refuse on a desktop where
# someone is logged in; its test flag is honoured only under the guarded runner.
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, (
    "resolved 'anyuse' to the wrong checkout — (D9)")

from anyuse.execution import desktop_compile as dc  # noqa: E402
from anyuse.execution import desktop_discover as dd  # noqa: E402
from anyuse.execution import hil_gate  # noqa: E402
from anyuse.execution import osa_compile as oc  # noqa: E402

DATA = _HERE / "testdata" / "discovery"
COCOA_HREF = "file://localhost/System/Library/ScriptingDefinitions/CocoaStandard.sdef"


def dump(bid):
    with gzip.open(DATA / f"dump_{bid}.json.gz", "rt") as fh:
        return json.load(fh)


def cocoa_standard():
    return dump("com.apple.Notes")["sdef"]["includes"][COCOA_HREF]


def dictionary(bid):
    return oc.dictionary_from_dump(dump(bid), cocoa_standard())


# --- dictionary model ------------------------------------------------------

def test_finder_dictionary_typed():
    d = dictionary("com.apple.finder")
    assert len([c for c in d.commands if not c.get("hidden")]) == 24
    folder = d.class_by_code("cfol")
    assert folder is not None and folder.name == "folder"
    props = d.resolved_properties("folder")
    assert props["pnam"].writable and props["pnam"].type == "text"        # inherited from item
    assert not props["ctnr"].writable                                        # container is r
    assert "folder" in d.application().elements
    print("  ok  Finder: 24 commands, folder inherits a writable name from item")


def test_notes_dictionary_merges_include_and_extension():
    d = dictionary("com.apple.Notes")
    codes = {c["code"] for c in d.commands}
    assert {"noteshow", "noteopen", "corecrel", "coredelo", "coresave"} <= codes
    app = d.application()
    assert {"note", "folder", "account", "document", "window"} <= set(app.elements)
    note = d.classes["note"]
    assert note.properties["body"].writable and not note.properties["ID  "].writable
    print("  ok  Notes: CocoaStandard include + application extension merged")


def test_textedit_implicit_cocoa_suite():
    d = dictionary("com.apple.TextEdit")
    assert d.command("coresave") and d.command("corecrel") and d.command("aevtodoc")
    doc = d.class_by_code("docu")
    text = d.resolved_properties(doc.name)["ctxt"]
    assert text.writable and text.type == "text" and text.name == "text"
    assert any("implicit Cocoa standard suite" in n for n in d.notes)
    bare = oc.dictionary_from_dump(dump("com.apple.TextEdit"), None)
    assert bare.command("coresave") is None and any("not collected" in n for n in bare.notes)
    print("  ok  TextEdit: legacy suite + implicit CocoaStandard, text property typed")


def test_unscriptable_apps_are_empty():
    for bid in ("com.apple.calculator", "org.blenderfoundation.blender"):
        d = dictionary(bid)
        r = oc.analyse_app(d)
        assert r["counts"]["commands"] == 0 and r["counts"]["osa_commands_mapped"] == 0, bid
    print("  ok  Calculator and Blender: no dictionary, nothing mapped")


# --- effects and qualification --------------------------------------------

def test_effect_table_qualification():
    ops = {op.key: op for op in oc.typed_operations(dictionary("com.apple.finder"))}
    for never in ("aevt:fndrempt", "aevt:fndrfera", "aevt:fndrshut", "aevt:aevtquit", "aevt:fndrrest"):
        assert ops[never].effect == "never" and not ops[never].qualifies, never
    assert ops["aevt:coredelo"].effect == "destructive" and not ops["aevt:coredelo"].qualifies
    assert ops["aevt:aevtodoc"].effect == "ui" and not ops["aevt:aevtodoc"].qualifies
    assert ops["aevt:corecrel"].qualifies and ops["aevt:corecnte"].qualifies
    assert all(not op.qualifies for op in ops.values() if op.effect in ("never", "ui", "destructive",
                                                                          "undeclared"))
    assert ops["get:capp.vers"].qualifies and "set:capp.vers" not in ops    # version is r
    print("  ok  effect table: never/ui/destructive/undeclared never qualify")


def test_create_needs_delete_to_qualify():
    d = oc.parse_sdef_dictionary("x.y", """<dictionary><suite name="s" code="ssss">
      <command name="make" code="corecrel"><parameter name="new" code="kocl" type="type"/></command>
      <command name="frob" code="xxxxfrob"/>
    </suite></dictionary>""")
    ops = {op.key: op for op in oc.typed_operations(d)}
    assert not ops["aevt:corecrel"].qualifies and "reversible" in ops["aevt:corecrel"].reason
    assert ops["aevt:xxxxfrob"].effect == "undeclared" and not ops["aevt:xxxxfrob"].qualifies
    print("  ok  create without delete and undeclared codes do not qualify")


# --- shapes and mapping ----------------------------------------------------

def test_directory_create_maps_on_finder_only():
    c = oc.map_class(dictionary("com.apple.finder"), "directory_op", "create")
    assert isinstance(c, oc.OsaCommand), c
    assert c.shape == "make_element" and c.effect == "create" and c.step_class == "side_effecting"
    assert "«class kocl»:«class cfol»" in c.script and "«class insh»:theContainer" in c.script
    assert 'tell application id "com.apple.finder"' in c.script
    assert [a.name for a in c.args] == ["folder", "name"] and c.args[0].under_scratch
    assert oc.argv_only(c.script)
    for other in ("com.apple.Notes", "com.apple.TextEdit"):
        u = oc.map_class(dictionary(other), "directory_op", "create")
        assert isinstance(u, oc.Unmapped), other
    print("  ok  directory_op/create -> make_element cfol on Finder; unmapped elsewhere")


def test_file_write_maps_on_textedit():
    c = oc.map_class(dictionary("com.apple.TextEdit"), "file_write")
    assert isinstance(c, oc.OsaCommand), c
    assert c.shape == "document_write" and c.effect == "write"
    assert "set «class ctxt» of theDoc to (item 2 of argv)" in c.script
    assert "«event coresave» theDoc" in c.script and "«constant ****no  »" in c.script
    assert oc.argv_only(c.script)
    assert isinstance(oc.map_class(dictionary("com.apple.finder"), "file_write"), oc.Unmapped)
    print("  ok  file_write -> document_write on TextEdit (implicit Cocoa suite)")


def test_mapping_from_a_real_recording():
    flows = json.loads((_HERE / "testdata" / "ax" / "desktop_recorded_steps.json").read_text())["flows"]
    got = {}
    for key, e in flows.items():
        rec = dc.recording_from_flow_json({**e, "flow": key})
        if rec.bundle_id not in ("com.apple.finder", "com.apple.TextEdit"):
            continue
        r = oc.map_recording(dictionary(rec.bundle_id), rec)
        got[(rec.flow, rec.bundle_id)] = r.shape if isinstance(r, oc.OsaCommand) else None
    assert "make_element" in got.values() and "document_write" in got.values(), got
    print(f"  ok  real recordings -> {sorted(set(v for v in got.values() if v))}")


def test_notes_note_is_discovered_without_an_ax_class():
    d = dictionary("com.apple.Notes")
    found = oc.discovered_make_elements(d)
    assert [c.id for c in found] == ["com.apple.Notes:osa_command/make_element/note"], [c.id for c in found]
    c = found[0]
    assert "«class pnam»:(item 1 of argv), «class body»:(item 2 of argv)" in c.script
    assert "return «class ID  » of theResult" in c.script and c.source == "discovered"
    assert oc.discovered_make_elements(dictionary("com.apple.finder")) == []
    print("  ok  Notes: make_element/note discovered (leaf, writable name + body, delete present)")


def test_no_interpolation_even_for_hostile_input():
    c = oc.map_class(dictionary("com.apple.finder"), "directory_op", "create")
    hostile = 'x" & (do shell script "id") & "'
    argv = c.argv(["/tmp/scratch", hostile])
    assert argv == [oc.OSASCRIPT, "-", "/tmp/scratch", hostile]
    assert hostile not in c.script
    for bad, fn in (("cf»l", oc._code), ('a" & x', oc._code), ('com.x"; do', oc._bundle)):
        try:
            fn(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} would have entered a script")
    print("  ok  inputs only as argv items; codes validated before they enter a script")


def test_app_property_shapes():
    ok, bad = oc.app_property_commands(dictionary("com.apple.Notes"))
    ids = {c.id for c in ok}
    assert "com.apple.Notes:osa_command/get_property/vers" in ids
    assert not any(c.id.endswith("set_property/vers") for c in ok)
    for c in ok:
        assert oc.argv_only(c.script), c.id
    fin, _ = oc.app_property_commands(dictionary("com.apple.finder"))
    setters = [c for c in fin if c.shape == "set_property"]
    assert setters and all("» of it to " in c.script for c in setters)
    print(f"  ok  application properties: {len(ok)} compiled, {len(bad)} not passable/writable")


# --- execution guard -------------------------------------------------------

class Runner:
    def __init__(self, rc=0, out=""):
        self.calls, self.rc, self.out = [], rc, out

    def __call__(self, argv, script):
        self.calls.append((argv, script))
        return self.rc, self.out, ""


class Gate:
    def __init__(self, actuate):
        self.actuate, self.seen = actuate, []

    async def require_approval(self, action, *, drive_enabled):
        self.seen.append(action)
        return hil_gate.ApprovalDecision(self.actuate, "fake", "side_effecting")


def test_run_osa_refuses_without_permission_and_never_runs():
    c = oc.map_class(dictionary("com.apple.finder"), "directory_op", "create")
    r = Runner()
    res = oc.run_osa(c, ["/s/x", "n"], permission=lambda b: -1744, runner=r, gate=Gate(True),
                     drive_enabled=True, scratch_root="/s")
    assert not res.ok and res.refused.startswith("automation_not_granted: -1744") and r.calls == []
    print("  ok  no Automation grant: refused, nothing sent")


def test_run_osa_write_needs_drive_scratch_and_gate():
    c = oc.map_class(dictionary("com.apple.finder"), "directory_op", "create")
    with tempfile.TemporaryDirectory() as root:
        r = Runner(out="/x\n")
        kw = dict(permission=lambda b: 0, runner=r, scratch_root=root)
        assert "drive gate off" in oc.run_osa(c, [root, "a"], gate=Gate(True), **kw).refused
        assert "disposable root" in oc.run_osa(c, ["/etc", "a"], gate=Gate(True), drive_enabled=True,
                                               **kw).refused
        assert "no human gate" in oc.run_osa(c, [root, "a"], drive_enabled=True, **kw).refused
        assert "human gate" in oc.run_osa(c, [root, "a"], gate=Gate(False), drive_enabled=True,
                                          **kw).refused
        real = hil_gate.HILGate(mode="all")                    # no approver: fail closed
        assert "ApprovalRequired" in oc.run_osa(c, [root, "a"], gate=real, drive_enabled=True, **kw).refused
        assert r.calls == []
        approved = hil_gate.HILGate(mode="all", approver=oc.disposable_approver(root, [c.id]))
        res = oc.run_osa(c, [root, "a"], gate=approved, drive_enabled=True, **kw)
        assert res.ok and r.calls[0][0] == [oc.OSASCRIPT, "-", root, "a"] and r.calls[0][1] == c.script
        other = hil_gate.HILGate(mode="all", approver=oc.disposable_approver(root, ["something:else"]))
        assert "denied" in oc.run_osa(c, [root, "b"], gate=other, drive_enabled=True, **kw).refused
    print("  ok  write: drive gate, disposable root, HILGate (fail-closed) all enforced")


def test_run_osa_read_skips_gate_but_not_permission():
    c = oc.compile_app_property(dictionary("com.apple.Notes"), "vers", write=False)
    r = Runner(out="4.13\n")
    assert oc.run_osa(c, [], permission=lambda b: 0, runner=r).ok
    assert not oc.run_osa(c, [], permission=lambda b: -1743, runner=Runner()).ok
    print("  ok  read: no gate, permission still required")


# --- proof -----------------------------------------------------------------

def _cmd():
    return oc.map_class(dictionary("com.apple.finder"), "directory_op", "create")


def _expect(start, inp, obs):
    return None if obs == sorted(set(start) | {inp}) else f"expected {inp} added to {start}, got {obs}"


def test_prove_refusals():
    c, start = _cmd(), ["keep"]
    ok = lambda i: sorted(["keep", i])  # noqa: E731
    kw = dict(recorded_inputs=["papaya_rec"], start_state=start, expect=_expect)
    assert "automation_not_granted" in oc.prove(c, ["a", "b"], ok, ok, automation_status=-1744,
                                                **kw).refusal_reason
    assert "distinct" in oc.prove(c, ["a", "a"], ok, ok, automation_status=0, **kw).refusal_reason
    assert "already used" in oc.prove(c, ["a", "papaya_rec"], ok, ok, automation_status=0,
                                      **kw).refusal_reason
    bad = oc.prove(c, ["a", "b"], ok, ok, automation_status=0, recorded_inputs=[], start_state=[],
                   expect=_expect)
    assert "start_state" in bad.refusal_reason
    print("  ok  refused: no grant, <2 inputs, recorded input, invalid start state")


def test_prove_rejects_shared_failure_and_no_effect():
    c, start = _cmd(), ["keep"]
    kw = dict(recorded_inputs=[], start_state=start, expect=_expect, automation_status=0)
    both_fail = oc.prove(c, ["a", "b"], lambda i: None, lambda i: None, **kw)
    assert not both_fail.proved and "invalid" in both_fail.rows[0].mismatch
    both_err = oc.prove(c, ["a", "b"], lambda i: {"error": "x"}, lambda i: {"error": "x"}, **kw)
    assert not both_err.proved
    no_effect = oc.prove(c, ["a", "b"], lambda i: ["keep"], lambda i: ["keep"], **kw)
    assert not no_effect.proved and "expected" in no_effect.rows[0].mismatch
    one_off = oc.prove(c, ["a", "b"], lambda i: sorted(["keep", i]),
                       lambda i: sorted(["keep", i]) if i == "a" else ["keep", "zzz"], **kw)
    assert not one_off.proved and one_off.rows[0].agree and not one_off.rows[1].agree
    good = oc.prove(c, ["a", "b"], lambda i: sorted(["keep", i]), lambda i: sorted(["keep", i]), **kw)
    assert good.proved and len(good.rows) == 2
    print("  ok  shared failure / no effect / one bad row all fail; clean rows prove")


def test_attach_proof_accepts_the_report():
    good = oc.prove(_cmd(), ["a", "b"], lambda i: sorted(["keep", i]), lambda i: sorted(["keep", i]),
                    recorded_inputs=[], start_state=["keep"], expect=_expect, automation_status=0)
    k = dd.ClassCandidate(id="x", bundle_id="com.apple.finder", op_class="osa_command",
                          subtype="make_element", triggers=["aevt:corecrel"])
    dd.attach_proof(k, good.to_dict(), ["a", "b"], ["papaya_rec"])
    assert k.status == "proved", k.proof_reason
    refused = oc.prove(_cmd(), ["a", "b"], None, None, recorded_inputs=[], start_state=["keep"],
                       expect=_expect, automation_status=-1744)
    k2 = dd.ClassCandidate(id="y", bundle_id="com.apple.finder", op_class="osa_command",
                           subtype="make_element", triggers=["aevt:corecrel"])
    dd.attach_proof(k2, refused.to_dict(), ["a", "b"], [])
    assert k2.status == "mapped" and "automation_not_granted" in k2.proof_reason
    print("  ok  desktop_discover.attach_proof reads osa proof reports")


def test_analysis_counts_on_real_dictionaries():
    table = {}
    for bid, classes in (("com.apple.finder", [("directory_op", "create")]),
                         ("com.apple.TextEdit", [("file_write", None)]),
                         ("com.apple.Notes", []),
                         ("com.apple.calculator", [("arithmetic", None)]),
                         ("org.blenderfoundation.blender", [("app_script", "mesh_add_move")])):
        r = oc.analyse_app(dictionary(bid), classes)
        table[bid] = r["counts"]
        print(f"      {bid}: {json.dumps(r['counts'], sort_keys=True)}")
    assert table["com.apple.finder"]["commands"] == 24 and table["com.apple.Notes"]["commands"] == 13
    assert table["com.apple.TextEdit"]["commands"] == 11
    assert [table[b]["osa_commands_mapped"] for b in ("com.apple.finder", "com.apple.TextEdit",
                                                      "com.apple.Notes")] == [1, 1, 1]
    assert table["com.apple.calculator"]["osa_commands_mapped"] == 0
    print("  ok  analysis on the five real dictionaries")


def test_host_recompute_equals_guest():
    guest = json.loads((_HERE / "testdata" / "osa" / "guest_counts.json").read_text())["apps"]
    for bid, g in guest.items():
        r = oc.analyse_app(dictionary(bid), {"com.apple.finder": [("directory_op", "create")],
                                             "com.apple.TextEdit": [("file_write", None)],
                                             "com.apple.calculator": [("arithmetic", None)],
                                             "org.blenderfoundation.blender": [("app_script", "mesh_add_move")],
                                             "com.apple.Notes": []}[bid])
        assert r["counts"] == g["counts"], (bid, r["counts"], g["counts"])
        assert {m["id"]: m["script"] for m in r["mapped"]} == g["mapped_scripts"], bid
    print("  ok  host recompute == guest (counts and the compiled scripts)")


TESTS = [
    test_finder_dictionary_typed,
    test_notes_dictionary_merges_include_and_extension,
    test_textedit_implicit_cocoa_suite,
    test_unscriptable_apps_are_empty,
    test_effect_table_qualification,
    test_create_needs_delete_to_qualify,
    test_directory_create_maps_on_finder_only,
    test_file_write_maps_on_textedit,
    test_mapping_from_a_real_recording,
    test_notes_note_is_discovered_without_an_ax_class,
    test_no_interpolation_even_for_hostile_input,
    test_app_property_shapes,
    test_run_osa_refuses_without_permission_and_never_runs,
    test_run_osa_write_needs_drive_scratch_and_gate,
    test_run_osa_read_skips_gate_but_not_permission,
    test_prove_refusals,
    test_prove_rejects_shared_failure_and_no_effect,
    test_attach_proof_accepts_the_report,
    test_analysis_counts_on_real_dictionaries,
    test_host_recompute_equals_guest,
]


def main():
    for t in TESTS:
        t()
    print("\nOSA COMPILE TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
