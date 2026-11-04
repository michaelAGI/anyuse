"""One executable manifest for an AnyUse skill or routine.

A *skill* is a short, typed sequence of steps an agent may call as ONE tool; a
*routine* is the same structure whose steps may themselves be pinned skills.
Both are the single source of truth for three projections:

* ``SKILL.md`` (``execution/skill_gen.py``) — what a human or an agent reads;
* the MCP tool definition (``execution/skill_gen.py``) — typed input/output
  schemas an agent calls;
* the runtime (``execution/routine.py``) — what actually executes, with
  receipts, hand-off and resume.

Because all three are generated from the same dict and carry the same
``manifest_digest``, the prose an agent reads cannot drift from what runs.

The schema extends the dataflow contract: every step input is bound by exactly one ``const | input | derived``
binding, and ``derived`` may only name an EARLIER step's declared output.

Design rules enforced by :func:`validate_manifest` (each is a test):

* **Mode and evidence are separate.** ``mode`` is how a step executes
  (``proved | replay | agent | human``); ``evidence.status`` is what is known
  about it (``proved | candidate | recorded | unresolved | none``). Only a step
  with ``evidence.status == "proved"`` AND an immutable proof reference
  (``action.proof_id`` + ``action.template_digest``) may run in mode
  ``proved``. A recording never confers ``proved``: see
  :func:`manifest_from_recording`.
* **Bindings are explicit.** No implicit name matching between steps.
* **Cross-app data transfer is an explicit edge.** A value that originated in
  app A may reach a step acting on app B only through a declared
  ``data_transfers`` edge naming the fields (confused-deputy guard).
* **Effects are declared.** A step that is not read-only, or not reversible,
  and that a machine would execute, must require approval. Model autonomy
  (``model_decides_within_limits``) is only valid on read-only, reversible
  agent-choice steps and must carry a cap.

Pure data and pure functions: no I/O, no network, no model.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

MANIFEST_SCHEMA = "anyuse.manifest/1"
MANIFEST_KINDS = frozenset({"skill", "routine"})

MODES = ("proved", "replay", "agent", "human")
EVIDENCE = ("proved", "candidate", "recorded", "unresolved", "none")
ACTION_KINDS = frozenset({"compiled_tool", "replay", "skill", "agent_choice", "human"})
#: action kinds whose proof can be pinned by an immutable reference
PROVABLE_ACTIONS = frozenset({"compiled_tool", "skill"})
MODE_ACTIONS = {
    "proved": frozenset({"compiled_tool", "skill"}),
    "replay": frozenset({"replay"}),
    "agent": frozenset({"agent_choice"}),
    "human": frozenset({"human"}),
}
#: ``action.card`` on a human step: what the person's inbox card asks for
HUMAN_CARDS = frozenset({"do_step", "value", "login_or_2fa", "consent"})
EFFECT_CLASSES = ("read_only", "side_effecting", "destructive")  # = hil_gate.StepClass
AUTONOMY_LEVELS = ("rule", "model_proposes_human_confirms", "model_decides_within_limits")
BINDING_KINDS = frozenset({"const", "input", "derived"})
PREDICATE_OPS = frozenset({"exists", "non_empty", "eq", "ne", "in", "len_min",
                           "len_max", "matches", "type"})
VALUE_TYPES = frozenset({"string", "integer", "number", "boolean", "scalar",
                         "array", "object"})

DEFAULT_MAX_LENGTH = 4096
DEFAULT_MAX_ITEMS = 1000
MAX_TIMEOUT_S = 600
MAX_RETRIES = 5
MAX_STEPS = 32

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_HEX = re.compile(r"^[0-9a-f]{8,64}$")


# ---------------------------------------------------------------------------
# typed values
# ---------------------------------------------------------------------------

def _is_type(value: Any, name: str) -> bool:
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "scalar":
        return isinstance(value, (str, int, float, bool))
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return False


def type_spec_problems(spec: Any, where: str) -> List[str]:
    """Static problems with a type spec (not with a value)."""
    if not isinstance(spec, Mapping):
        return [f"{where}: type spec must be an object"]
    t = spec.get("type")
    if t not in VALUE_TYPES:
        return [f"{where}: unknown type {t!r}"]
    out: List[str] = []
    if t == "array":
        if "items" not in spec:
            out.append(f"{where}: array needs an items type")
        else:
            out.extend(type_spec_problems(spec["items"], where + "[]"))
    if t == "object":
        for name, sub in dict(spec.get("properties") or {}).items():
            out.extend(type_spec_problems(sub, f"{where}.{name}"))
    return out


def check_value(spec: Mapping[str, Any], value: Any, where: str = "value") -> List[str]:
    """Runtime check of ``value`` against a type spec. Returns problems."""
    t = spec.get("type")
    if not _is_type(value, t):
        return [f"{where}: expected {t}, got {type(value).__name__}"]
    out: List[str] = []
    if "enum" in spec and value not in list(spec["enum"]):
        out.append(f"{where}: not one of the allowed values")
    if isinstance(value, str):
        if len(value) > int(spec.get("maxLength", DEFAULT_MAX_LENGTH)):
            out.append(f"{where}: longer than {spec.get('maxLength', DEFAULT_MAX_LENGTH)}")
        if len(value) < int(spec.get("minLength", 0)):
            out.append(f"{where}: shorter than {spec.get('minLength')}")
    if t in ("integer", "number"):
        if spec.get("minimum") is not None and value < spec["minimum"]:
            out.append(f"{where}: below minimum")
        if spec.get("maximum") is not None and value > spec["maximum"]:
            out.append(f"{where}: above maximum")
    if t == "array":
        if len(value) > int(spec.get("maxItems", DEFAULT_MAX_ITEMS)):
            out.append(f"{where}: more than {spec.get('maxItems', DEFAULT_MAX_ITEMS)} items")
        if len(value) < int(spec.get("minItems", 0)):
            out.append(f"{where}: fewer than {spec.get('minItems')} items")
        for i, item in enumerate(value):
            out.extend(check_value(spec["items"], item, f"{where}[{i}]"))
            if len(out) > 10:
                break
    if t == "object":
        props = dict(spec.get("properties") or {})
        for name in spec.get("required") or list(props):
            if name not in value:
                out.append(f"{where}: missing field {name!r}")
        for name, sub in props.items():
            if name in value:
                out.extend(check_value(sub, value[name], f"{where}.{name}"))
        if spec.get("additionalProperties") is False:
            extra = [k for k in value if k not in props]
            if extra:
                out.append(f"{where}: undeclared field(s) {extra}")
    return out


def types_compatible(producer: Mapping[str, Any], consumer: Mapping[str, Any]) -> bool:
    """Structural compatibility: the consumer can accept the producer's type.

    Bounds are not compared statically; every value is re-checked at runtime.
    """
    pt, ct = producer.get("type"), consumer.get("type")
    if ct == "scalar" and pt in ("string", "integer", "number", "boolean", "scalar"):
        return True
    if ct == "number" and pt == "integer":
        return True
    if pt != ct:
        return False
    if pt == "array":
        return types_compatible(producer.get("items") or {}, consumer.get("items") or {})
    if pt == "object":
        pp = dict(producer.get("properties") or {})
        for name, sub in dict(consumer.get("properties") or {}).items():
            if name not in pp or not types_compatible(pp[name], sub):
                return False
    return True


def to_json_schema(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a type spec to JSON Schema (never an empty ``{}``)."""
    t = spec["type"]
    if t == "scalar":
        out: Dict[str, Any] = {"type": ["string", "number", "boolean"]}
    else:
        out = {"type": t}
    if t == "string":
        out["maxLength"] = int(spec.get("maxLength", DEFAULT_MAX_LENGTH))
        if spec.get("minLength"):
            out["minLength"] = int(spec["minLength"])
    for key in ("enum", "minimum", "maximum", "minItems"):
        if key in spec:
            out[key] = copy.deepcopy(spec[key])
    if t == "array":
        out["items"] = to_json_schema(spec["items"])
        out["maxItems"] = int(spec.get("maxItems", DEFAULT_MAX_ITEMS))
    if t == "object":
        props = dict(spec.get("properties") or {})
        out["properties"] = {k: to_json_schema(v) for k, v in props.items()}
        out["required"] = list(spec.get("required") or list(props))
        out["additionalProperties"] = bool(spec.get("additionalProperties", False))
    return out


def object_schema(fields: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    return {"type": "object",
            "properties": {k: to_json_schema(v) for k, v in fields.items()},
            "required": list(fields), "additionalProperties": False}


# ---------------------------------------------------------------------------
# machine-evaluated predicates (pre/postconditions)
# ---------------------------------------------------------------------------

_MISSING = object()


def resolve_ref(ref: str, scope: Mapping[str, Any]) -> Any:
    """Resolve ``$run.x``, ``$in.x``, ``$out.x`` or ``$steps.<id>.<out>[.k|.N]``."""
    if not isinstance(ref, str) or not ref.startswith("$"):
        return _MISSING
    parts = ref[1:].split(".")
    cur: Any = scope
    for part in parts:
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _operand(value: Any, scope: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping) and set(value) == {"ref"}:
        return resolve_ref(value["ref"], scope)
    return value


def predicate_problems(pred: Any, where: str) -> List[str]:
    if not isinstance(pred, Mapping):
        return [f"{where}: predicate must be an object"]
    op = pred.get("op")
    if op not in PREDICATE_OPS:
        return [f"{where}: unknown predicate op {op!r}"]
    if not isinstance(pred.get("ref"), str) or not pred["ref"].startswith("$"):
        return [f"{where}: predicate needs a $ref"]
    if op == "matches":
        pattern = pred.get("value")
        if not isinstance(pattern, str) or len(pattern) > 200:
            return [f"{where}: matches needs a short pattern"]
        try:
            re.compile(pattern)
        except re.error:
            return [f"{where}: invalid pattern"]
    if op == "type" and pred.get("value") not in VALUE_TYPES:
        return [f"{where}: type predicate needs a known type"]
    return []


def evaluate(pred: Mapping[str, Any], scope: Mapping[str, Any]) -> Tuple[bool, str]:
    """Evaluate one predicate. Missing refs are FALSE, never an exception."""
    op = pred["op"]
    ref = pred["ref"]
    actual = resolve_ref(ref, scope)
    label = f"{op}({ref})"
    if op == "exists":
        return actual is not _MISSING, label
    if actual is _MISSING:
        return False, label + ": missing"
    expected = _operand(pred.get("value"), scope)
    try:
        if op == "non_empty":
            ok = actual not in (None, "", [], {})
        elif op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "in":
            pool = _operand(pred.get("in"), scope)
            ok = isinstance(pool, list) and actual in pool
        elif op == "len_min":
            ok = len(actual) >= int(expected)
        elif op == "len_max":
            ok = len(actual) <= int(expected)
        elif op == "matches":
            ok = isinstance(actual, str) and re.fullmatch(str(expected), actual) is not None
        elif op == "type":
            ok = _is_type(actual, str(expected))
        else:
            ok = False
    except (TypeError, ValueError):
        ok = False
    return bool(ok), label


# ---------------------------------------------------------------------------
# manifest validation
# ---------------------------------------------------------------------------

def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Immutable version reference: sha256 of the canonical manifest."""
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def step_index(manifest: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(s.get("id")): s for s in manifest.get("steps") or []}


def _origins(manifest: Mapping[str, Any]) -> Dict[str, Set[str]]:
    """Which app(s) each step's outputs originate from.

    A machine step's outputs originate in its own app. A human or agent step
    does not launder provenance: its outputs carry the union of the origins of
    what it was shown (a human picking from app A's list still hands on A data).
    """
    origins: Dict[str, Set[str]] = {}
    for step in manifest.get("steps") or []:
        sid = str(step.get("id"))
        if step.get("app"):
            origins[sid] = {str(step["app"])}
            continue
        acc: Set[str] = set()
        for b in dict(step.get("bindings") or {}).values():
            if isinstance(b, Mapping) and b.get("kind") == "derived":
                acc |= origins.get(str(b.get("step")), set())
        origins[sid] = acc
    return origins


def transfer_allowed(manifest: Mapping[str, Any], from_app: str, to_app: str,
                     field: str) -> bool:
    if from_app == to_app:
        return True
    for edge in manifest.get("data_transfers") or []:
        if (edge.get("from_app") == from_app and edge.get("to_app") == to_app
                and field in (edge.get("fields") or [])):
            return True
    return False


def cross_app_problems(manifest: Mapping[str, Any], step: Mapping[str, Any]) -> List[str]:
    """Derived bindings into ``step`` that cross an app boundary undeclared."""
    target = step.get("app")
    if not target:
        return []
    origins = _origins(manifest)
    out: List[str] = []
    for name, b in dict(step.get("bindings") or {}).items():
        if not isinstance(b, Mapping) or b.get("kind") != "derived":
            continue
        for app in sorted(origins.get(str(b.get("step")), set())):
            if not transfer_allowed(manifest, app, str(target), str(b.get("output"))):
                out.append(f"step {step.get('id')!r} input {name!r}: data from app "
                           f"{app!r} may not flow to app {target!r} without a "
                           f"declared data_transfers edge for field {b.get('output')!r}")
    return out


def validate_manifest(manifest: Any) -> List[str]:
    """Every problem with a manifest. Empty list means executable."""
    if not isinstance(manifest, Mapping):
        return ["manifest must be an object"]
    errs: List[str] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errs.append(f"schema must be {MANIFEST_SCHEMA!r}")
    if manifest.get("kind") not in MANIFEST_KINDS:
        errs.append("kind must be 'skill' or 'routine'")
    if not _SAFE_NAME.match(str(manifest.get("name") or "")):
        errs.append("name must match [a-z][a-z0-9_]{0,63}")
    if not _VERSION.match(str(manifest.get("version") or "")):
        errs.append("version must be MAJOR.MINOR.PATCH")
    run_inputs = dict(manifest.get("inputs") or {})
    for name, spec in run_inputs.items():
        if not _SAFE_NAME.match(str(name)):
            errs.append(f"input name {name!r} is not safe")
        errs.extend(type_spec_problems(spec, f"inputs.{name}"))
    apps = dict(manifest.get("apps") or {})
    for edge in manifest.get("data_transfers") or []:
        if (edge.get("from_app") not in apps or edge.get("to_app") not in apps
                or not edge.get("fields") or not edge.get("purpose")):
            errs.append("data_transfers edge needs declared from_app/to_app, "
                        "fields and a purpose")

    steps = list(manifest.get("steps") or [])
    if not steps:
        errs.append("a manifest needs at least one step")
    if len(steps) > MAX_STEPS:
        errs.append(f"at most {MAX_STEPS} steps")
    seen: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        errs.extend(_step_problems(manifest, step, seen, run_inputs, apps))
        if isinstance(step, Mapping) and step.get("id"):
            seen[str(step["id"])] = dict(step)

    for name, spec in dict(manifest.get("outputs") or {}).items():
        errs.extend(type_spec_problems(spec, f"outputs.{name}"))
        src = (spec or {}).get("from") or {}
        producer = seen.get(str(src.get("step")))
        if producer is None or src.get("output") not in (producer.get("outputs") or {}):
            errs.append(f"outputs.{name}: 'from' must name a step output")
        elif not types_compatible(producer["outputs"][src["output"]], spec):
            errs.append(f"outputs.{name}: type does not match its producer")
    return errs


def _step_problems(manifest, step, seen, run_inputs, apps) -> List[str]:
    if not isinstance(step, Mapping):
        return ["each step must be an object"]
    sid = str(step.get("id") or "")
    w = f"step {sid!r}"
    errs: List[str] = []
    if not _SAFE_NAME.match(sid):
        errs.append(f"{w}: id must match [a-z][a-z0-9_]{{0,63}}")
    if sid in seen:
        errs.append(f"{w}: duplicate id")
    mode = step.get("mode")
    action = dict(step.get("action") or {})
    kind = action.get("kind")
    evidence = dict(step.get("evidence") or {})
    status = evidence.get("status")
    if mode not in MODES:
        errs.append(f"{w}: mode must be one of {MODES}")
    if status not in EVIDENCE:
        errs.append(f"{w}: evidence.status must be one of {EVIDENCE}")
    if kind not in ACTION_KINDS:
        errs.append(f"{w}: unknown action kind {kind!r}")
    elif mode in MODE_ACTIONS and kind not in MODE_ACTIONS[mode]:
        errs.append(f"{w}: mode {mode!r} cannot execute action kind {kind!r}")

    # mode and evidence are separate; "proved" must be earned and pinned
    if status == "proved":
        if kind not in PROVABLE_ACTIONS:
            errs.append(f"{w}: only a compiled tool or a pinned skill can carry "
                        f"proved evidence")
        if not evidence.get("proof_id"):
            errs.append(f"{w}: proved evidence needs a proof_id")
        if evidence.get("origin") == "recording":
            errs.append(f"{w}: a recording never confers proved status")
    if mode == "proved":
        if status != "proved":
            errs.append(f"{w}: mode 'proved' requires evidence.status 'proved'")
        if kind == "compiled_tool":
            if not (_HEX.match(str(action.get("proof_id") or ""))
                    and _HEX.match(str(action.get("template_digest") or ""))):
                errs.append(f"{w}: a proved compiled tool needs immutable proof_id "
                            f"and template_digest references")
            if evidence.get("proof_id") != action.get("proof_id"):
                errs.append(f"{w}: evidence.proof_id must equal action.proof_id")
        if kind == "skill" and not _HEX.match(str(action.get("digest") or "")):
            errs.append(f"{w}: a pinned skill needs its manifest digest")
    if kind in ("compiled_tool", "replay", "skill") and not action.get("ref"):
        errs.append(f"{w}: action.ref is required")
    if kind == "human" and not str(action.get("instruction") or "").strip():
        errs.append(f"{w}: a human step needs an instruction")
    card = action.get("card")
    if card is not None:
        # human inbox card kind (docs/HUMAN_INBOX.md); default is do_step
        if kind != "human" or card not in HUMAN_CARDS:
            errs.append(f"{w}: action.card must be one of {sorted(HUMAN_CARDS)} "
                        f"on a human step")
        elif card == "value":
            outs = dict(step.get("outputs") or {})
            if not outs or any((spec or {}).get("type") not in
                               ("string", "integer", "number", "scalar")
                               for spec in outs.values()):
                errs.append(f"{w}: a value card needs scalar outputs")
    if kind == "skill" and manifest.get("kind") != "routine":
        errs.append(f"{w}: only a routine may compose skills")

    # apps / tenancy
    app = step.get("app")
    if mode in ("proved", "replay") and not app:
        errs.append(f"{w}: a machine step must name its app")
    if app and app not in apps:
        errs.append(f"{w}: app {app!r} is not declared in apps")

    # typed io + bindings (const | input | derived, derived from EARLIER steps)
    inputs = dict(step.get("inputs") or {})
    outputs = dict(step.get("outputs") or {})
    for name, spec in inputs.items():
        errs.extend(type_spec_problems(spec, f"{w} inputs.{name}"))
    for name, spec in outputs.items():
        errs.extend(type_spec_problems(spec, f"{w} outputs.{name}"))
    bindings = dict(step.get("bindings") or {})
    for extra in sorted(set(bindings) - set(inputs)):
        errs.append(f"{w}: binding for undeclared input {extra!r}")
    for name, spec in inputs.items():
        b = bindings.get(name)
        if not isinstance(b, Mapping) or b.get("kind") not in BINDING_KINDS:
            errs.append(f"{w}: input {name!r} needs exactly one const|input|derived binding")
            continue
        if b["kind"] == "const":
            if "value" not in b or check_value(spec, b["value"], name):
                errs.append(f"{w}: const binding for {name!r} is missing or ill-typed")
        elif b["kind"] == "input":
            src = run_inputs.get(str(b.get("name")))
            if src is None:
                errs.append(f"{w}: input binding {name!r} names an undeclared run input")
            elif not types_compatible(src, spec):
                errs.append(f"{w}: input binding {name!r} type mismatch")
        else:
            producer = seen.get(str(b.get("step")))
            if producer is None:
                errs.append(f"{w}: derived binding {name!r} must name an EARLIER step")
            elif b.get("output") not in (producer.get("outputs") or {}):
                errs.append(f"{w}: derived binding {name!r} names an undeclared output")
            elif not types_compatible(producer["outputs"][b["output"]], spec):
                errs.append(f"{w}: derived binding {name!r} type mismatch")
    for dep in step.get("after") or []:
        if dep not in seen:
            errs.append(f"{w}: 'after' must name an earlier step")
    errs.extend(cross_app_problems(manifest, {**step, "id": sid}))

    for i, pred in enumerate(step.get("preconditions") or []):
        errs.extend(predicate_problems(pred, f"{w} precondition[{i}]"))
    for i, pred in enumerate(step.get("postconditions") or []):
        errs.extend(predicate_problems(pred, f"{w} postcondition[{i}]"))

    # effects, approval, policy
    effects = dict(step.get("effects") or {})
    eclass = effects.get("class")
    reversible = effects.get("reversible")
    if eclass not in EFFECT_CLASSES:
        errs.append(f"{w}: effects.class must be one of {EFFECT_CLASSES}")
    if not isinstance(reversible, bool):
        errs.append(f"{w}: effects.reversible must be true or false")
    approval = dict(step.get("approval") or {})
    if mode in ("proved", "replay", "agent") and (eclass != "read_only" or reversible is False):
        if not approval.get("required"):
            errs.append(f"{w}: a machine-executed step with effects "
                        f"{eclass!r}/reversible={reversible} requires approval")
    policy = dict(step.get("policy") or {})
    timeout = policy.get("timeout_s", 30)
    retries = policy.get("retries", 0)
    if not isinstance(timeout, (int, float)) or not (0 < timeout <= MAX_TIMEOUT_S):
        errs.append(f"{w}: policy.timeout_s must be in (0, {MAX_TIMEOUT_S}]")
    if not isinstance(retries, int) or not (0 <= retries <= MAX_RETRIES):
        errs.append(f"{w}: policy.retries must be 0..{MAX_RETRIES}")
    elif retries and not (policy.get("idempotent") is True or eclass == "read_only"):
        errs.append(f"{w}: retries require an idempotent or read-only step")

    # autonomy: models only on agent-choice steps
    autonomy = dict(step.get("autonomy") or {"level": "rule"})
    level = autonomy.get("level")
    if level not in AUTONOMY_LEVELS:
        errs.append(f"{w}: autonomy.level must be one of {AUTONOMY_LEVELS}")
    if level != "rule" and mode != "agent":
        errs.append(f"{w}: a model may only act on an agent-choice step")
    if mode == "agent":
        if not str(action.get("choose_from") or "").startswith("$in."):
            errs.append(f"{w}: agent_choice.choose_from must be a $in. reference")
        if action.get("into") not in outputs:
            errs.append(f"{w}: agent_choice.into must name a declared output")
    if level == "model_decides_within_limits":
        limits = dict(autonomy.get("limits") or {})
        if eclass != "read_only" or reversible is not True:
            errs.append(f"{w}: model_decides_within_limits is reversible/read-only only")
        if not (isinstance(limits.get("cap_usd"), (int, float)) and limits["cap_usd"] >= 0):
            errs.append(f"{w}: model_decides_within_limits needs limits.cap_usd")
        if not (isinstance(limits.get("max_decisions"), int) and limits["max_decisions"] > 0):
            errs.append(f"{w}: model_decides_within_limits needs limits.max_decisions")
    return errs


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("manifest file must hold a JSON object")
    return data


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def _compiled_output_types(row: Mapping[str, Any], extract_names: Iterable[str]) -> Dict[str, Any]:
    if row.get("result_shape", "list") == "list":
        return {"items": {"type": "array", "items": {"type": "scalar"}}}
    return {name: {"type": "scalar"} for name in extract_names}


def step_from_compiled(step_id: str, row: Mapping[str, Any], app: str,
                       extract_names: Iterable[str] = (),
                       bindings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A step from one ``compiled_tools.describe_compiled`` row.

    Mode ``proved`` only when the compiled record itself says the proof passed,
    is current for the template, and the rung is runnable without a human.
    Anything else becomes an ``agent``-free ``human`` hand-off with
    ``candidate`` evidence — never a silently weaker automatic path.
    """
    proof = dict(row.get("proof") or {})
    params = [p for p in row.get("parameter_schema") or []]
    inputs = {}
    for p in params:
        spec: Dict[str, Any] = {"type": p.get("type", "string")}
        if spec["type"] == "string":
            spec["maxLength"] = int(p.get("max_length", DEFAULT_MAX_LENGTH))
        inputs[str(p["name"])] = spec
    if not inputs:
        inputs = {str(n): {"type": "string", "maxLength": 2048}
                  for n in row.get("parameters") or []}
    # A "checked (selection not held out)" record (docs/ATTACH.md) is not
    # proved; records without a status keep their old meaning.
    proved = bool(proof.get("passed") and proof.get("current")
                  and row.get("runnable_without_human")
                  and str(proof.get("status") or "proved") == "proved")
    step: Dict[str, Any] = {
        "id": step_id,
        "app": app,
        "inputs": inputs,
        "outputs": _compiled_output_types(row, extract_names),
        "bindings": dict(bindings or {n: {"kind": "input", "name": n} for n in inputs}),
        "effects": {"class": str(row.get("risk_class") or "side_effecting"),
                    "reversible": row.get("risk_class") == "read_only"},
        "policy": {"timeout_s": 30, "retries": 0, "idempotent": row.get("risk_class") == "read_only"},
        "approval": {"required": row.get("risk_class") != "read_only"},
        "autonomy": {"level": "rule"},
    }
    if proved:
        step.update({
            "mode": "proved",
            "action": {"kind": "compiled_tool", "ref": str(row["workflow"]),
                       "proof_id": proof["id"], "template_digest": proof["template_digest"]},
            "evidence": {"status": "proved", "proof_id": proof["id"],
                         "scope": str(proof.get("what_was_checked") or "")[:300],
                         "origin": "compiled_proof"},
        })
    else:
        step.update({
            "mode": "human",
            "action": {"kind": "human", "instruction":
                       "This step has no current proof; perform it yourself and "
                       "enter the declared outputs."},
            "evidence": {"status": "candidate", "origin": "compiled_unproved"},
            "approval": {"required": False},
        })
    return step


def manifest_from_recording(name: str, recorded_steps: Iterable[Mapping[str, Any]],
                            version: str = "0.1.0") -> Dict[str, Any]:
    """A draft manifest from a recording. It NEVER marks anything proved.

    Each recorded step becomes a ``replay`` step with ``recorded`` evidence when
    the recording resolved a target, or an explicit ``human`` hand-off with
    ``unresolved`` evidence when it did not. Replay steps are conservatively
    classified side-effecting and so require approval until someone proves or
    reclassifies them.
    """
    steps: List[Dict[str, Any]] = []
    for i, rec in enumerate(recorded_steps):
        sid = f"step_{i + 1}"
        resolved = bool(rec.get("resolved"))
        if resolved:
            steps.append({
                "id": sid, "mode": "replay", "app": str(rec.get("app") or "app"),
                "action": {"kind": "replay", "ref": str(rec.get("ref") or sid)},
                "evidence": {"status": "recorded", "origin": "recording"},
                "inputs": {}, "outputs": {}, "bindings": {},
                "effects": {"class": "side_effecting", "reversible": False},
                "approval": {"required": True},
                "policy": {"timeout_s": 30, "retries": 0, "idempotent": False},
                "autonomy": {"level": "rule"},
            })
        else:
            steps.append({
                "id": sid, "mode": "human",
                "action": {"kind": "human", "instruction":
                           "The recording could not resolve this step; do it yourself."},
                "evidence": {"status": "unresolved", "origin": "recording"},
                "inputs": {}, "outputs": {}, "bindings": {},
                "effects": {"class": "side_effecting", "reversible": False},
                "approval": {"required": False},
                "autonomy": {"level": "rule"},
            })
    apps = {s["app"]: {"origin": "", "tenant": ""} for s in steps if s.get("app")}
    return {"schema": MANIFEST_SCHEMA, "kind": "skill", "name": name,
            "version": version, "summary": "Draft from a recording; nothing proved.",
            "inputs": {}, "outputs": {}, "apps": apps, "data_transfers": [],
            "steps": steps}
