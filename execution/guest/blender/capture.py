"""Blender-side recording source: the operator log, not AX.

Blender draws its own UI (Cocoa/Metal, custom widgets) so the macOS
accessibility tree is nearly empty inside it -- there is no ``AXButton``/
``AXTextField`` vocabulary to record here the way ``execution/guest/flows.py``
records TextEdit/Finder/Calculator. What Blender exposes instead is its own
operator log: every ``bpy.ops.<name>(...)`` call the running session executed
-- through the mouse, the keyboard, a menu, or a script -- is retained on
``bpy.context.window_manager.operators`` for the "Repeat Last"/"Adjust Last
Operation" machinery, and that list IS this app's recorded-selector
vocabulary, the exact analogue of the design notes Sec 11's
``canonical_role``/``AXIdentifier`` for the AX-driven apps.

This module has exactly two jobs, both run INSIDE Blender's own bundled
Python (``blender --background --python`` or a live GUI session) via
``import bpy``:

* :func:`dump_operator_log` -- every operator run since the session (or
  since a caller-supplied ``since`` index) started, reduced to
  ``{"idname", "label", "kwargs"}`` where ``kwargs`` holds only the
  properties Blender itself considers EXPLICITLY SET (``is_property_set``)
  -- the same subset the Info editor's ``bpy.ops.category.name(kwarg=val)``
  report line would show, never a property merely left at its default.
* :func:`dump_scene_state` -- a deterministic snapshot of document state:
  every object's type/transform/active-material/modifier stack, plus scene
  unit settings and the on-disk path. This is what the proof step (run on
  the host, no bpy needed, against JSON this module wrote) diffs the GUI
  path against the compiled script's path.

Nothing here decides whether a class is proved, generates a script, or
talks to the guest's job runner -- that is ``execution/blender_compile.py``,
importable with no ``bpy`` present, host-testable against the JSON this
module produces.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional


def _normalize(value: Any) -> Any:
    """Reduce a Blender/mathutils value to something ``json.dumps`` accepts,
    with no information loss for the scalar/vector/enum shapes this class of
    operator actually uses (translate deltas, string names, bools, enums)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    # mathutils.Vector/Euler/Color and bpy_prop_array all support len()+index
    # without importing mathutils (this module must stay importable with no
    # bpy, so it duplicates zero mathutils-specific behaviour).
    if hasattr(value, "__len__") and not isinstance(value, dict):
        try:
            return [_normalize(v) for v in value]
        except TypeError:
            pass
    return str(value)  # enum items and anything else: their label is enough


def dump_operator_log(since: int = 0) -> List[Dict[str, Any]]:
    """Every operator recorded from index ``since`` onward, in order.

    ``since`` lets a caller snapshot ``len(bpy.context.window_manager.operators)``
    before a flow and pass it back here after, so two flows recorded in the
    same Blender session never bleed into each other's log -- the same
    "know exactly which steps belong to this flow" discipline
    ``execution/guest/flows.py``'s ``Recorder`` already applies to AX steps.
    """
    import bpy  # noqa: F401  (only importable inside Blender's own Python)

    out: List[Dict[str, Any]] = []
    ops = bpy.context.window_manager.operators
    for op in list(ops)[since:]:
        props = op.properties
        kwargs: Dict[str, Any] = {}
        try:
            rna_props = props.bl_rna.properties
        except Exception:  # pragma: no cover - defensive, not expected live
            rna_props = []
        for p in rna_props:
            ident = p.identifier
            if ident == "rna_type":
                continue
            try:
                if props.is_property_set(ident):
                    kwargs[ident] = _normalize(getattr(props, ident))
            except Exception:  # pragma: no cover - some props refuse reads
                continue
        out.append({"idname": op.bl_idname, "label": op.bl_label, "kwargs": kwargs})
    return out


def dump_scene_state() -> Dict[str, Any]:
    """A deterministic dump of document state: object list (sorted by name,
    never insertion order, so two independent runs compare byte-for-byte),
    each object's type/transform/active-material/modifier stack, the scene's
    unit settings, and the on-disk path (``""`` for an unsaved file)."""
    import bpy

    scn = bpy.context.scene
    objects = []
    for obj in scn.objects:
        objects.append({
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 6) for v in obj.location],
            "rotation_euler": [round(v, 6) for v in obj.rotation_euler],
            "scale": [round(v, 6) for v in obj.scale],
            "active_material": obj.active_material.name if obj.active_material else None,
            "material_slots": [s.name if s.material is None else s.material.name
                               for s in obj.material_slots],
            "modifiers": [m.type for m in obj.modifiers],
        })
    objects.sort(key=lambda d: d["name"])
    active = bpy.context.view_layer.objects.active
    return {
        "objects": objects,
        "active_object": active.name if active else None,
        "unit_system": scn.unit_settings.system,
        "unit_scale_length": round(scn.unit_settings.scale_length, 6),
        "filepath": bpy.data.filepath,
        "materials": sorted(m.name for m in bpy.data.materials),
    }


def dump_operator_catalog(max_enum_items: int = 64) -> List[Dict[str, Any]]:
    """Every registered operator, by ``bpy`` introspection, for desktop
    discovery (``execution/desktop_discover.py``): idname, Python idname,
    label, description, and each RNA property as a typed parameter (type,
    subtype, array length, enum items, default, soft range). Reads the
    registry only; runs no operator and touches no scene."""
    import bpy

    out: List[Dict[str, Any]] = []
    for mod_name in sorted(dir(bpy.ops)):
        if mod_name.startswith("_"):
            continue
        mod = getattr(bpy.ops, mod_name)
        for fn_name in sorted(dir(mod)):
            if fn_name.startswith("_"):
                continue
            try:
                opfn = getattr(mod, fn_name)
                rna = opfn.get_rna_type()
            except Exception as exc:  # noqa: BLE001 - a broken add-on op is recorded, not fatal
                out.append({"idname_py": f"{mod_name}.{fn_name}", "error": f"{type(exc).__name__}: {exc}"})
                continue
            props = []
            for p in rna.properties:
                if p.identifier == "rna_type":
                    continue
                d: Dict[str, Any] = {"identifier": p.identifier, "type": p.type,
                                     "subtype": getattr(p, "subtype", ""),
                                     "array_length": int(getattr(p, "array_length", 0) or 0),
                                     "hidden": bool(getattr(p, "is_hidden", False)),
                                     "skip_save": bool(getattr(p, "is_skip_save", False))}
                if p.type == "ENUM":
                    # read each item WHILE iterating: a dynamic enum's items are
                    # temporary, and touching them after list() segfaulted a
                    # headless Blender 5.2.2
                    d["enum_items"] = [e.identifier for e in p.enum_items][:max_enum_items]
                    d["enum_flag"] = bool(getattr(p, "is_enum_flag", False))
                try:
                    if d["array_length"]:
                        d["default"] = _normalize(p.default_array)
                    elif p.type in ("BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"):
                        d["default"] = _normalize(p.default if p.type != "ENUM" or not d.get("enum_flag")
                                                  else sorted(p.default_flag))
                except Exception:  # noqa: BLE001
                    pass
                if p.type in ("INT", "FLOAT"):
                    d["soft_min"], d["soft_max"] = _normalize(p.soft_min), _normalize(p.soft_max)
                props.append(d)
            out.append({"idname": opfn.idname(), "idname_py": opfn.idname_py(),
                        "label": rna.name, "description": rna.description, "props": props})
    return out


def _cli() -> None:
    """``blender --background [file.blend] --python capture.py -- <cmd> [args]``

    ``state <out.json>``           dump_scene_state() -> out.json
    ``ops <since> <out.json>``     dump_operator_log(since) -> out.json
    ``both <since> <out.json>``    {"state":..., "ops":...} -> out.json
    ``catalog <out.json>``         dump_operator_catalog() -> out.json
    """
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    cmd = argv[0]
    if cmd == "state":
        with open(argv[1], "w") as fh:
            json.dump(dump_scene_state(), fh, indent=2, sort_keys=True)
    elif cmd == "ops":
        since = int(argv[1])
        with open(argv[2], "w") as fh:
            json.dump(dump_operator_log(since), fh, indent=2, sort_keys=True)
    elif cmd == "both":
        since = int(argv[1])
        with open(argv[2], "w") as fh:
            json.dump({"state": dump_scene_state(), "ops": dump_operator_log(since)},
                     fh, indent=2, sort_keys=True)
    elif cmd == "catalog":
        with open(argv[1], "w") as fh:
            json.dump(dump_operator_catalog(), fh, indent=1, sort_keys=True)
    else:
        raise SystemExit(f"unknown capture.py command: {cmd!r}")


if __name__ == "__main__":
    _cli()
