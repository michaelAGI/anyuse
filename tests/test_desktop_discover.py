"""Offline tests for ``execution/desktop_discover.py``.

Pure: no pyobjc, no subprocess, no bpy. The app dumps below are CONSTRUCTED
(small, in the exact shape ``execution/guest/discover_desktop.py dump`` writes),
except the Calculator button identifiers, which come from the real
capture ``testdata/ax/calculator_ax_raw.json``, and the German titles, which
are the guest's real ones. The real measured dumps are covered separately in
``test_desktop_discover_real.py`` once they exist.

Run from the anyuse package dir:
    python3 test_desktop_discover.py
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

from anyuse.execution import desktop_compile as dc  # noqa: E402
from anyuse.execution import desktop_discover as dd  # noqa: E402


def mi(idx, titles, ident="", cmd="", mods=0, vk=None, enabled=True, sub=False):
    return {"index_path": idx, "title_path": titles, "title": titles[-1], "identifier": ident,
            "cmd_char": cmd, "cmd_mods": mods, "cmd_virtual_key": vk, "enabled": enabled,
            "has_submenu": sub}


APPLE = [mi([0, 0], ["", "Über diesen Mac"], "_aboutThisMacRequested:")]
SERVICES = [mi([1, 2], ["TextEdit", "Dienste"], "", sub=True),
            mi([1, 2, 0], ["TextEdit", "Dienste", "Dienste-Einstellungen"], "_configureServicesMenu:")]


def textedit_dump(bundle="com.apple.TextEdit", area=True):
    menu = APPLE + SERVICES + [
        mi([1, 0], ["TextEdit", "TextEdit beenden"], "terminate:", "q"),
        mi([2, 0], ["Ablage", "Neu"], "newDocument:", "n"),
        mi([2, 1], ["Ablage", "Sichern …"], "saveDocument:", "s"),
        mi([2, 2], ["Ablage", "Duplizieren"], "duplicateDocument:", "s", mods=1),
        mi([2, 3], ["Ablage", ""], ""),                                  # separator
        mi([3, 0], ["Format", "Schrift"], "", sub=True),
        mi([3, 0, 0], ["Format", "Schrift", "Fett"], "menuAction:", "b"),
        mi([3, 0, 1], ["Format", "Schrift", "Kursiv"], "menuAction:", "i"),
        mi([3, 1], ["Format", "Ohne Kürzel"], ""),
    ]
    controls = [{"role": "AXWindow", "identifier": ""}, {"role": "AXScrollArea", "identifier": ""}]
    if area:
        controls.append({"role": "AXTextArea", "identifier": "First Text View", "enabled": True})
    return {"bundle_id": bundle, "app": "TextEdit", "menu_items": menu, "window_controls": controls}


def test_menu_exclusions_and_language_independent_keys():
    d = dd.discover_app(textedit_dump())
    menu = {c.key: c for c in d.candidates if c.source == "menu"}
    labels = " ".join(c.label for c in menu.values())
    assert "Über diesen Mac" not in labels, "the Apple menu is not the app's"
    assert "Dienste" not in labels, "Services items belong to other apps"
    assert "Schrift" not in [c.label.split(" > ")[-1] for c in menu.values()], "submenu parents are not candidates"
    assert "id:saveDocument:" in menu and menu["id:saveDocument:"].key_kind == "identifier"
    # the generic SwiftUI selector is shared: position appended to both holders
    shared = [k for k in menu if k.startswith("id:menuAction:@")]
    assert len(shared) == 2 and all(menu[k].key_kind == "identifier+position" for k in shared)
    assert "pos:3/1" in menu and menu["pos:3/1"].key_kind == "position"
    assert d.counts()["menu_language_independent"] == f"{len(menu) - 1}/{len(menu)}"
    print(f"OK  menu: {len(menu)} leaf items, Apple/Services/submenu parents/separators left out, "
          f"keys from identifiers/shortcuts, position only as a last resort")


def test_shortcut_key_when_no_identifier():
    assert dd.menu_key(mi([2, 1], ["Ablage", "Sichern"], "", "S", 0)) == ("key:0:s", "shortcut")
    assert dd.menu_key(mi([2, 1], ["File", "Save"], "", "s", 0)) == ("key:0:s", "shortcut")
    # function-key glyph (private-use range) -> virtual key
    assert dd.menu_key(mi([4, 0], ["X", "Y"], "", "", 0, vk=122)) == ("key:0:vk122", "shortcut")
    print("OK  Sichern and Save get the same key; a function-key glyph falls back to the virtual key")


def test_file_write_maps_through_the_existing_rule():
    d = dd.discover_app(textedit_dump())
    assert [k.op_class for k in d.classes] == ["file_write"], [k.to_dict() for k in d.classes]
    k = d.classes[0]
    assert set(k.triggers) == {"id:First Text View", "id:saveDocument:"}
    assert k.status == "mapped" and "TextEdit" in k.inferred_reason
    mapped = [c for c in d.candidates if c.status == "mapped"]
    assert {c.key for c in mapped} == set(k.triggers)
    un = [c for c in d.candidates if c.status == "unresolved"]
    assert un and all(c.reason for c in un)
    print(f"OK  TextEdit: file_write mapped via desktop_compile.infer_class; "
          f"{len(un)} other candidates unresolved, each with a reason")


def test_no_text_area_no_file_write():
    d = dd.discover_app(textedit_dump(area=False))
    assert d.classes == []
    print("OK  ⌘S without a text area in the front window maps to nothing")


def test_class_rules_stay_bound_to_their_bundle():
    d = dd.discover_app(textedit_dump(bundle="com.apple.Notes"))
    assert d.classes == [], "file_write is TextEdit-only in desktop_compile; discovery must not widen it"
    print("OK  the same ⌘S + text area in Notes maps to nothing (infer_class says unresolved)")


def finder_dump():
    return {"bundle_id": "com.apple.finder", "app": "Finder", "menu_items": [
        mi([1, 0], ["Finder", "Einstellungen …"], "", ","),
        mi([2, 0], ["Ablage", "Neues Finder-Fenster"], "", "n", 0),
        mi([2, 1], ["Ablage", "Neuer Ordner"], "", "n", 1),
        mi([2, 2], ["Ablage", "In den Papierkorb legen"], "", "\x08", 0),
    ], "window_controls": [{"role": "AXButton", "identifier": "", "title": "Zurück"}]}


def test_directory_create_from_new_folder_shortcut_with_implied_field():
    d = dd.discover_app(finder_dump())
    assert [(k.op_class, k.subtype) for k in d.classes] == [("directory_op", "create")]
    k = d.classes[0]
    assert k.triggers == ["key:1:n"] and k.implied and "AXTextField" in k.implied[0]
    new_window = next(c for c in d.candidates if c.key == "key:0:n")
    assert new_window.status == "unresolved", "⌘N (new window) is not New Folder (⇧⌘N)"
    print("OK  Finder: ⇧⌘N maps to directory_op/create, the rename field marked implied; ⌘N stays unresolved")


def calculator_controls():
    raw = json.load(open(_HERE / "testdata" / "ax" / "calculator_ax_raw.json"))["elements"]
    return [{"role": e.get("AXRole"), "identifier": e.get("AXIdentifier") or "",
             "title": e.get("AXTitle") or "", "description": e.get("AXDescription") or ""}
            for e in raw if e.get("AXRole") != "AXMenuItem"]


def test_arithmetic_from_real_calculator_identifiers():
    d = dd.discover_app({"bundle_id": "com.apple.calculator", "app": "Rechner",
                         "menu_items": [], "window_controls": calculator_controls()})
    assert [k.op_class for k in d.classes] == ["arithmetic"], [k.to_dict() for k in d.classes]
    assert len(d.classes[0].triggers) == len(dd.ARITHMETIC_IDS)
    print(f"OK  Calculator (real AX identifiers): arithmetic mapped on "
          f"{len(dd.ARITHMETIC_IDS)} identified buttons")


def test_arithmetic_needs_the_whole_operand_vocabulary():
    ctl = [c for c in calculator_controls() if c["identifier"] != "Equals"]
    d = dd.discover_app({"bundle_id": "com.apple.calculator", "app": "Rechner",
                         "menu_items": [], "window_controls": ctl})
    assert d.classes == []
    print("OK  a Calculator without an Equals button is not the arithmetic class")


NOTES_SDEF = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd">
<dictionary xmlns:xi="http://www.w3.org/2001/XInclude">
  <xi:include href="file://localhost/System/Library/ScriptingDefinitions/CocoaStandard.sdef" xpointer="xpointer(/dictionary/suite)"/>
  <suite name="Notes Suite" code="note">
    <command name="show" code="noteshow">
      <direct-parameter><type type="note"/><type type="folder"/></direct-parameter>
      <parameter name="separately" code="sepa" type="boolean" optional="yes"/>
      <result><type type="note"/></result>
    </command>
    <command name="secret" code="notesecr" hidden="yes"/>
  </suite>
</dictionary>"""

STANDARD = """<?xml version="1.0" encoding="UTF-8"?>
<dictionary><suite name="Standard Suite" code="????">
  <command name="close" code="coreclos"><direct-parameter type="specifier"/>
    <parameter name="saving" code="savo" type="save options" optional="yes"/></command>
</suite></dictionary>"""


def test_sdef_typed_commands_with_include():
    href = "file://localhost/System/Library/ScriptingDefinitions/CocoaStandard.sdef"
    cmds = dd.parse_sdef(NOTES_SDEF, {href: STANDARD})
    names = {c["name"]: c for c in cmds}
    assert set(names) == {"close", "show", "secret"}
    assert names["show"]["code"] == "noteshow" and names["show"]["direct_parameter"]["type"] == "note | folder"
    assert names["show"]["params"] == [{"name": "separately", "code": "sepa", "type": "boolean", "optional": True}]
    cands = dd.sdef_candidates("com.apple.Notes", cmds)
    assert {c.key for c in cands} == {"aevt:coreclos", "aevt:noteshow"}, "hidden commands are left out"
    missing = dd.parse_sdef(NOTES_SDEF, {})
    assert missing[-1]["_missing_includes"] == [href]
    d = dd.discover_app({"bundle_id": "com.apple.Notes", "app": "Notes", "menu_items": [],
                         "window_controls": [], "sdef": {"xml": NOTES_SDEF, "includes": {}}})
    assert any("not collected" in n for n in d.notes)
    assert all(c.status == "unresolved" and "Apple events" in c.reason for c in d.candidates)
    print("OK  sdef: typed commands keyed by event code, xi:include resolved from the dump, "
          "a missing include reported; OSA commands stay unresolved with the reason")


def test_legacy_script_suite():
    suite = {"Name": "TextEdit", "AppleEventCode": "txtd", "Commands": {
        "Print": {"AppleEventClassCode": "aevt", "AppleEventCode": "pdoc", "Type": "NSString",
                  "Arguments": {"ShowPanel": {"AppleEventCode": "pdlg", "Type": "NSNumber<Bool>",
                                              "Optional": "YES"}}}}}
    term = {"Commands": {"Print": {"Name": "print", "Arguments": {"ShowPanel": {"Name": "print dialog"}}}}}
    cmds = dd.parse_script_suite(suite, term)
    assert cmds[0]["name"] == "print" and cmds[0]["code"] == "aevtpdoc"
    assert cmds[0]["params"][0] == {"name": "print dialog", "code": "pdlg", "type": "NSNumber<Bool>",
                                    "optional": True}
    print("OK  legacy .scriptSuite + .scriptTerminology parse to the same typed shape")


def bpy_op(idname, props):
    return {"idname": idname, "idname_py": dd.idname_py(idname), "label": idname, "props": props}


VEC3 = {"identifier": "value", "type": "FLOAT", "array_length": 3, "subtype": "TRANSLATION"}


def test_app_script_shape_from_the_operator_catalog():
    cat = [bpy_op("MESH_OT_primitive_cube_add", [{"identifier": "size", "type": "FLOAT"}]),
           bpy_op("TRANSFORM_OT_translate", [VEC3]),
           bpy_op("OBJECT_OT_delete", [{"identifier": "confirm", "type": "BOOLEAN"}])]
    d = dd.discover_app({"bundle_id": "org.blenderfoundation.blender", "app": "Blender",
                         "menu_items": [], "window_controls": [], "bpy_catalog": cat})
    assert [(k.op_class, k.subtype) for k in d.classes] == [("app_script", "mesh_add_move")]
    k = d.classes[0]
    assert k.triggers == ["mesh.primitive_cube_add", "transform.translate"]
    assert [p.name for p in k.params] == ["value[0]", "value[1]", "value[2]"]
    delete = next(c for c in d.candidates if c.key == "object.delete")
    assert delete.status == "unresolved" and delete.params[0].type == "boolean"
    rec = dd.synthetic_blender_recording(k)
    from anyuse.execution import blender_compile as bc
    src = bc.generate_script(rec, bc.infer_class(rec))
    assert "bpy.ops.mesh.primitive_cube_add()" in src
    assert "bpy.ops.transform.translate(value=(p0, p1, p2))" in src
    compile(src, "<generated>", "exec")
    print("OK  Blender: KNOWN_SHAPES mesh_add_move found in the catalog with a typed 3-vector "
          "parameter; generated script has no recorded constants; other operators unresolved")


def test_app_script_refuses_a_wrongly_typed_parameter():
    for bad in ({**VEC3, "array_length": 0}, {**VEC3, "type": "STRING"}):
        cat = [bpy_op("MESH_OT_primitive_cube_add", []), bpy_op("TRANSFORM_OT_translate", [bad])]
        d = dd.discover_app({"bundle_id": "org.blenderfoundation.blender", "app": "Blender",
                             "menu_items": [], "window_controls": [], "bpy_catalog": cat})
        assert d.classes == []
    cat = [bpy_op("TRANSFORM_OT_translate", [VEC3])]
    d = dd.discover_app({"bundle_id": "org.blenderfoundation.blender", "app": "Blender",
                         "menu_items": [], "window_controls": [], "bpy_catalog": cat})
    assert d.classes == []
    print("OK  app_script needs every operator of the shape and a numeric 3-vector 'value'")


def _proof(inputs, agree=True, runs=3):
    op = dc.CLASS_OPERATIONS["arithmetic"]
    fn = (lambda i, r: {"display": [dc.direct_arithmetic(*i)]}) if agree else \
        (lambda i, r: {"display": [0.0]})
    return dc.prove(op, inputs, runs, fn, lambda i, r: {"display": [dc.direct_arithmetic(*i)]},
                    recorded_inputs=[], start_state={"display": [0.0]}).to_dict()


def test_attach_proof_rules():
    fresh = [(318, "-", 45), (23, "*", 17)]
    seen = [(7, "*", 6)]

    def k():
        return dd.ClassCandidate(id="x", bundle_id="com.apple.calculator", op_class="arithmetic",
                                 subtype=None, triggers=[])
    assert dd.attach_proof(k(), _proof(fresh), fresh, seen).status == "proved"
    r = dd.attach_proof(k(), _proof([fresh[0], seen[0]]), [fresh[0], seen[0]], seen)
    assert r.status == "mapped" and "already seen" in r.proof_reason
    r = dd.attach_proof(k(), _proof(fresh, agree=False), fresh, seen)
    assert r.status == "mapped" and "disagree" in r.proof_reason
    r = dd.attach_proof(k(), _proof(fresh, runs=1), fresh, seen)
    assert r.status == "mapped" and "refused" in r.proof_reason
    r = dd.attach_proof(k(), _proof([fresh[0]]), [fresh[0]], seen)
    assert r.status == "mapped"
    # a report that claims proved but carries no rows is not a proof
    r = dd.attach_proof(k(), {"proved": True, "rows": []}, fresh, seen)
    assert r.status == "mapped" and "no rows" in r.proof_reason
    print("OK  attach_proof: proved only with >= 2 unseen inputs and every row agreeing; "
          "seen inputs, a disagreement, a refusal or an empty report keep it mapped")


TESTS = [
    test_menu_exclusions_and_language_independent_keys,
    test_shortcut_key_when_no_identifier,
    test_file_write_maps_through_the_existing_rule,
    test_no_text_area_no_file_write,
    test_class_rules_stay_bound_to_their_bundle,
    test_directory_create_from_new_folder_shortcut_with_implied_field,
    test_arithmetic_from_real_calculator_identifiers,
    test_arithmetic_needs_the_whole_operand_vocabulary,
    test_sdef_typed_commands_with_include,
    test_legacy_script_suite,
    test_app_script_shape_from_the_operator_catalog,
    test_app_script_refuses_a_wrongly_typed_parameter,
    test_attach_proof_rules,
]


def main():
    for t in TESTS:
        t()
    print("\nDESKTOP DISCOVER TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
