"""``app_script`` compile: Blender, the first app-native-script class.

The five ``execution/desktop_compile.py`` classes are all AX-driven --
``canonical_role``/``AXIdentifier``/a menu item's command character. Blender
draws its own UI (Cocoa/Metal, custom widgets), so the accessibility tree
inside it is nearly empty: there is no AX vocabulary to record here at all.
What Blender exposes INSTEAD is its own scripting API (``bpy``) plus an
operator log of everything that already ran through it
(``bpy.context.window_manager.operators``, see ``execution/guest/blender/capture.py``
for how a recording is produced from a live session). ``app_script`` is that
new class: compile a recording straight to a parameterised call against the
app's OWN API, never to an OS-level primitive (``os.makedirs``, `find`, ...)
the way every ``execution/desktop_compile.py`` class does.

Same non-negotiable property as every class before it (the resolver's
"0 candidates stays 0 candidates", ``desktop_compile.infer_class``'s
"a positive, exclusive shape or unresolved, never a guess"): a recording is
``app_script`` only if its operator sequence matches ONE of a small, fixed,
pre-declared table of shapes (``KNOWN_SHAPES`` below) AND the value that
varies across inputs is an EXPLICITLY-SET keyword argument on one of those
operators (``op.kwargs``, built from Blender's own ``is_property_set`` --
see ``capture.dump_operator_log``, never a property left at its default).
Anything else -- an unfamiliar operator sequence, or a familiar one whose
would-be parameter never actually got a value logged against it -- is
``unresolved``. Of four measured flows, three are exactly that: Blender's
generic ID-rename widget (an object's name, a material's name) and
``wm.save_as_mainfile`` all either never appear in the operator log or appear
with no parameters at all, even though the underlying action plainly
succeeded. No model is involved in any of this, exactly
as every class before it: the table below is data, not a proposal a model
made about this specific recording.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Same floor as the web half and the desktop half: a recording that only
#: reproduces itself has proved nothing.
MIN_FRESH_CASES = 2


# ---------------------------------------------------------------------------
# 1. The recording model -- built from Blender's own operator log, never AX.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordedOperator:
    """One entry from ``bpy.context.window_manager.operators``, reduced to
    what the class rules use: the operator's idname and the keyword
    arguments Blender itself considers explicitly set
    (``capture.dump_operator_log``'s ``is_property_set`` filter) -- never a
    property left at its default."""
    idname: str
    label: str = ""
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BlenderRecording:
    """A recording of one Blender flow: the operator log plus the scene
    state read before and after (``capture.dump_scene_state``). ``flow`` is
    for reporting only -- ``infer_class`` below consults ``ops`` alone,
    never the flow's name."""
    flow: str
    ops: Tuple[RecordedOperator, ...] = ()
    before_state: Dict[str, Any] = field(default_factory=dict)
    after_state: Dict[str, Any] = field(default_factory=dict)


def recording_from_json(entry: Dict[str, Any]) -> BlenderRecording:
    """Build a :class:`BlenderRecording` from the exact
    ``{"flow", "ops": [{"idname","label","kwargs"}], "before_state", "after_state"}``
    shape ``execution/guest/blender/capture.py``'s ``dump_operator_log``/
    ``dump_scene_state`` write (``testdata/blender/*.json`` -- real captured
    recordings, not invented)."""
    ops = tuple(RecordedOperator(idname=o["idname"], label=o.get("label", ""),
                                 kwargs=dict(o.get("kwargs") or {}))
               for o in entry.get("ops") or [])
    return BlenderRecording(flow=entry.get("flow", ""), ops=ops,
                            before_state=dict(entry.get("before_state") or {}),
                            after_state=dict(entry.get("after_state") or {}))


# ---------------------------------------------------------------------------
# 2. Class inference -- deterministic, over the operator idname SEQUENCE and
#    which of their kwargs were explicitly set. A fixed table, exactly like
#    desktop_compile.CLASS_OPERATIONS; never a per-recording guess.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    op_index: int      # which recorded operator (0-based) carries the parameter
    kwarg: str          # its keyword argument name
    count: int          # how many scalar values (3 for an (x,y,z) vector, 1 for a string)


@dataclass(frozen=True)
class ShapeSpec:
    subtype: str
    params: Tuple[ParamSpec, ...]
    template: str        # name of the script template in generate_script()


#: The fixed, pre-declared table: a recording's operator-idname sequence is
#: looked up here VERBATIM. No entry -> unresolved, regardless of how plausible
#: the recording looks otherwise.
KNOWN_SHAPES: Dict[Tuple[str, ...], ShapeSpec] = {
    ("MESH_OT_primitive_cube_add", "TRANSFORM_OT_translate"): ShapeSpec(
        subtype="mesh_add_move",
        params=(ParamSpec(op_index=1, kwarg="value", count=3),),
        template="mesh_add_move",
    ),
}


@dataclass(frozen=True)
class ClassInference:
    op_class: str                        # "app_script" | "unresolved"
    subtype: Optional[str] = None
    reason: str = ""
    evidence: Tuple[str, ...] = ()


def _unresolved(reason: str, **kw) -> ClassInference:
    return ClassInference("unresolved", reason=reason, **kw)


def infer_class(rec: BlenderRecording) -> ClassInference:
    """Infer the operation class from ``rec.ops`` alone -- the operator
    idname sequence and which of their arguments were explicitly set. Never
    a guess: an operator sequence absent from ``KNOWN_SHAPES``, or present
    but missing its parameter's kwarg, is ``unresolved`` -- the same
    "positive, exclusive shape or nothing" property
    ``desktop_compile.infer_class`` already established for the AX-driven
    classes, applied here to an operator-idname sequence instead of a
    role/bundle-id combination."""
    idnames = tuple(op.idname for op in rec.ops)
    shape = KNOWN_SHAPES.get(idnames)
    if shape is None:
        return _unresolved(
            f"operator sequence {idnames!r} matches no known app_script shape "
            f"(KNOWN_SHAPES has {sorted(KNOWN_SHAPES.keys())!r})")
    missing = [p for p in shape.params if p.kwarg not in rec.ops[p.op_index].kwargs]
    if missing:
        m = missing[0]
        return _unresolved(
            f"{rec.ops[m.op_index].idname} did not explicitly set {m.kwarg!r} -- "
            f"the operator log does not carry the value this flow needs")
    evidence = tuple(f"{rec.ops[p.op_index].idname}.{p.kwarg}="
                     f"{rec.ops[p.op_index].kwargs[p.kwarg]!r}" for p in shape.params)
    return ClassInference("app_script", subtype=shape.subtype,
                          reason=f"operator sequence {idnames!r} matches KNOWN_SHAPES "
                                 f"and every parameter kwarg is explicitly set",
                          evidence=evidence)


# ---------------------------------------------------------------------------
# 3. Script generation -- a parameterised bpy script from the recording.
#    Values named by the shape's ParamSpec become `input` (argv); every
#    other explicitly-set kwarg is baked in as a `const`.
# ---------------------------------------------------------------------------

#: Operator keyword arguments that are Blender ENUM_FLAG properties, which
#: Python's operator binding requires as a ``set`` (a ``tuple``/``list`` is
#: refused with "expected a set, not a tuple" -- found empirically running
#: the very first generated script against a real Blender). JSON has no set
#: type, so ``capture.py`` necessarily wrote these as JSON arrays; this fixed,
#: small table is what tells the generator which recorded arrays need to come
#: back as a Python ``set`` literal rather than the ``tuple`` every other
#: recorded array (a location/rotation/scale/translate vector) needs to stay.
_ENUM_FLAG_KWARGS = {"snap_elements"}


def _py_repr(value: Any, as_set: bool = False) -> str:
    """A literal Python source repr for a captured (already-JSON) value."""
    if isinstance(value, list):
        if as_set:
            return "{" + ", ".join(_py_repr(v) for v in value) + "}"
        return "(" + ", ".join(_py_repr(v) for v in value) + ("," if len(value) == 1 else "") + ")"
    return repr(value)


def _kwargs_src(op: RecordedOperator, params: Sequence[ParamSpec], op_index: int,
                var_names: Sequence[str]) -> str:
    parts = []
    param_kwarg = {p.kwarg for p in params if p.op_index == op_index}
    for k, v in op.kwargs.items():
        if k in param_kwarg:
            parts.append(f"{k}=({', '.join(var_names)})" if len(var_names) > 1
                        else f"{k}={var_names[0]}")
        else:
            parts.append(f"{k}={_py_repr(v, as_set=k in _ENUM_FLAG_KWARGS)}")
    return ", ".join(parts)


def generate_script(rec: BlenderRecording, inference: ClassInference) -> str:
    """A self-contained ``blender --background <base.blend> --python <this> --
    <param values...> <out_state.json>`` script: replays ``rec.ops`` with the
    shape's parameter slot(s) fed from argv, everything else baked in as the
    exact constant the recording captured, then writes
    ``capture.dump_scene_state()`` to the path argv names.

    Never invoked for an ``unresolved`` inference -- exactly like
    ``desktop_compile.operation_for`` returning ``None``, there is nothing to
    compile until a recording resolves to a class.
    """
    if inference.op_class != "app_script":
        raise ValueError("generate_script requires an app_script inference, "
                         f"got {inference.op_class!r} ({inference.reason})")
    shape = KNOWN_SHAPES[tuple(op.idname for op in rec.ops)]
    n_values = sum(p.count for p in shape.params)
    var_names = [f"p{i}" for i in range(n_values)]

    lines = [
        "import bpy, sys, os, json",
        "sys.path.insert(0, os.path.expanduser("
        "'~/anyuse-guest/src/anyuse/execution/guest/blender'))",
        "import capture",
        "",
        "_argv = sys.argv[sys.argv.index('--') + 1:]",
        f"_vals = [float(_argv[_i]) for _i in range({n_values})]",
        f"{', '.join(var_names)} = _vals" if n_values > 1 else f"{var_names[0]} = _vals[0]",
        f"_out_path = _argv[{n_values}]",
        "",
    ]

    offset = 0
    for i, op in enumerate(rec.ops):
        my_vars = []
        for p in shape.params:
            if p.op_index == i:
                my_vars = var_names[offset:offset + p.count]
                offset += p.count
        src = _kwargs_src(op, shape.params, i, my_vars) if my_vars else \
            ", ".join(f"{k}={_py_repr(v)}" for k, v in op.kwargs.items())
        module, func = op.idname.split("_OT_")
        lines.append(f"bpy.ops.{module.lower()}.{func}({src})")

    lines += [
        "",
        "with open(_out_path, 'w') as _fh:",
        "    json.dump(capture.dump_scene_state(), _fh, indent=2, sort_keys=True)",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. The proof -- >= 2 distinct unseen inputs, every one must agree, a
#    mismatch fails and stays in the report. Same discipline as
#    desktop_compile.prove, generalized to comparing two STATE dumps rather
#    than two scalar/string values.
# ---------------------------------------------------------------------------

@dataclass
class ProofRow:
    input: Any
    ui_state: Any
    script_state: Any
    agree: bool
    mismatch: Optional[str] = None

    def to_dict(self):
        return {"input": self.input, "agree": self.agree, "mismatch": self.mismatch,
                "ui_state": self.ui_state, "script_state": self.script_state}


@dataclass
class ProofReport:
    flow: str
    subtype: str
    rows: List[ProofRow] = field(default_factory=list)
    proved: bool = False
    refusal_reason: Optional[str] = None

    def to_dict(self):
        return {"flow": self.flow, "subtype": self.subtype, "proved": self.proved,
                "refusal_reason": self.refusal_reason,
                "rows": [r.to_dict() for r in self.rows]}


_STATE_FIELDS = ("name", "type", "location", "rotation_euler", "scale",
                "active_material", "material_slots", "modifiers")

#: Tolerance for comparing two float vectors from two Blender sessions.
_TOL = 1e-4


def validate_state(state: Any) -> Optional[str]:
    """``None`` if ``state`` is a well-formed ``capture.dump_scene_state()``
    observation, else the reason it is not. A missing ``objects`` key, a
    non-list, an object without a name/type or without a 3-vector location
    is invalid. An invalid observation never counts as agreement, so two
    callbacks that both fail the same way (both return ``{}``) cannot pass."""
    if not isinstance(state, dict):
        return f"observation is {type(state).__name__}, not a dict"
    objs = state.get("objects")
    if not isinstance(objs, list):
        return "observation has no 'objects' list"
    for o in objs:
        if not isinstance(o, dict) or not isinstance(o.get("name"), str) or not o.get("name"):
            return f"object without a name: {o!r}"
        if not isinstance(o.get("type"), str) or not o.get("type"):
            return f"object {o.get('name')!r} has no type"
        loc = o.get("location")
        if not (isinstance(loc, list) and len(loc) == 3 and all(isinstance(v, (int, float)) for v in loc)):
            return f"object {o.get('name')!r} has no 3-vector location"
    return None


def _objects(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {o["name"]: o for o in state.get("objects", [])}


def _expect_mesh_add_move(start: Dict[str, Any], inp: Any, state: Dict[str, Any]) -> Optional[str]:
    """mesh_add_move on ``inp`` = (x, y, z), from ``start``: every starting
    object is still there, exactly ONE new object exists, it is a MESH, and
    its location is the requested translation (the cube is added at the
    origin, then moved by ``inp``)."""
    before, after = _objects(start), _objects(state)
    gone = sorted(set(before) - set(after))
    if gone:
        return f"starting objects missing afterwards: {gone}"
    new = sorted(set(after) - set(before))
    if len(new) != 1:
        return f"expected exactly one new object, found {new}"
    obj = after[new[0]]
    if obj.get("type") != "MESH":
        return f"new object {new[0]!r} is {obj.get('type')!r}, not MESH"
    want = [float(v) for v in inp]
    loc = obj.get("location")
    if len(want) != 3 or any(abs(a - b) > _TOL for a, b in zip(loc, want)):
        return f"new object {new[0]!r} at {loc!r}, requested {want!r}"
    return None


#: Per-shape expectation: what a correct run on ``inp`` from ``start`` must
#: produce. A shape without an entry cannot be proved at all.
EXPECTATIONS: Dict[str, Any] = {"mesh_add_move": _expect_mesh_add_move}


def _diff_object_states(ui_state: Dict[str, Any], script_state: Dict[str, Any]) -> Optional[str]:
    """Compare the two dumps on exactly these fields:
    object names/types/transforms/material names/(here) modifiers.
    Floats compare with a small tolerance (two independent Blender sessions,
    not float-identical bit patterns, produced these). Callers validate
    both dumps first (:func:`validate_state`)."""
    a, b = _objects(ui_state), _objects(script_state)
    if set(a) != set(b):
        return f"object name sets differ: ui={sorted(a)} script={sorted(b)}"
    for name in a:
        oa, ob = a[name], b[name]
        for field_name in _STATE_FIELDS:
            va, vb = oa.get(field_name), ob.get(field_name)
            if isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    return f"{name}.{field_name}: ui={va!r} script={vb!r}"
                for x, y in zip(va, vb):
                    numeric = isinstance(x, (int, float)) and isinstance(y, (int, float))
                    if (numeric and abs(x - y) > _TOL) or (not numeric and x != y):
                        return f"{name}.{field_name}: ui={va!r} script={vb!r}"
            elif va != vb:
                return f"{name}.{field_name}: ui={va!r} script={vb!r}"
    return None


def prove(flow: str, subtype: str, inputs: Sequence[Any],
         ui_fn: "Callable[[Any], Dict[str, Any]]",
         script_fn: "Callable[[Any], Dict[str, Any]]",
         *, recorded_inputs: Sequence[Any], start_state: Dict[str, Any]) -> ProofReport:
    """``ui_fn(input)`` drives the live GUI with ``input`` and returns its
    resulting ``dump_scene_state()``; ``script_fn(input)`` runs the compiled
    script headless with the same input and returns the same shape. Both are
    caller-supplied (guest-bound in a job script; fakes in a host unit test),
    the same injection convention every proof routine in this codebase uses.

    ``recorded_inputs`` (required, may be empty only when nothing was
    recorded) are the inputs the recording itself used, plus anything
    replayed before; an overlap with ``inputs`` refuses the proof, so
    "unseen" is enforced here rather than trusted. ``start_state`` is the
    declared starting scene both paths begin from (``base_empty.blend``'s
    dump); it must be a valid observation.

    A row agrees only if both observations are valid, both meet the shape's
    expectation for that input (``EXPECTATIONS``: the expected new object
    with the requested transform, starting objects intact), and they match
    each other field by field. A single disagreeing row fails the whole
    report and is kept, never dropped."""
    def refuse(reason):
        return ProofReport(flow, subtype, refusal_reason=reason)

    distinct = len({repr(i) for i in inputs})
    if distinct < MIN_FRESH_CASES:
        return refuse(f"only {distinct} distinct unseen input(s), need >= {MIN_FRESH_CASES}")
    reused = sorted({repr(i) for i in inputs} & {repr(i) for i in recorded_inputs})
    if reused:
        return refuse(f"input(s) already used by the recording or an earlier replay: {reused}")
    bad_start = validate_state(start_state)
    if bad_start:
        return refuse(f"start_state is not a valid observation: {bad_start}")
    expect = EXPECTATIONS.get(subtype)
    if expect is None:
        return refuse(f"no expectation registered for shape {subtype!r}; cannot prove it")
    rows = []
    for inp in inputs:
        ui_state = ui_fn(inp)
        script_state = script_fn(inp)
        mismatch = (
            (lambda r: r and f"ui observation invalid: {r}")(validate_state(ui_state))
            or (lambda r: r and f"script observation invalid: {r}")(validate_state(script_state))
            or (lambda r: r and f"ui: {r}")(expect(start_state, inp, ui_state))
            or (lambda r: r and f"script: {r}")(expect(start_state, inp, script_state))
            or _diff_object_states(ui_state, script_state))
        rows.append(ProofRow(inp, ui_state, script_state, not mismatch, mismatch or None))
    return ProofReport(flow, subtype, rows=rows, proved=all(r.agree for r in rows))


# ---------------------------------------------------------------------------
# 5. Guest-side glue: run the generated script headless via `blender
#    --background`. Never imported/called on the host guard (subprocess to a
#    real Blender binary) -- only from guest job scripts.
# ---------------------------------------------------------------------------

def run_headless(blender_bin: str, base_blend: str, script_path: str,
                 args: Sequence[str], timeout: float = 60.0) -> Tuple[float, str]:
    """Run ``script_path`` headless against ``base_blend`` with ``args`` after
    ``--``; returns (elapsed_ms, combined stdout+stderr) for the report's
    latency column. Raises on a non-zero exit so a broken script is never
    silently "proved" by an empty/stale output file."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [blender_bin, "--background", base_blend, "--python", script_path, "--", *args],
        capture_output=True, text=True, timeout=timeout)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(f"blender --background exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
    return elapsed_ms, proc.stdout + proc.stderr
