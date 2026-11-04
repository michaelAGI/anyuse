"""Explicit, persistent REA registrations shared by agents and MCP.

Registration does not grant a proof or restoration policy. Every listing reopens
the pinned catalog and its evidence; execution uses the ordinary REA gate. This
adapter is deliberately VM-only, including when a CLI enabled ``--allow-host``.
An absent registry is inert and does not create any files.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from anyuse.paths import anyuse_data_path
from . import rea_actions as A
from . import rea_mapping as R

FORMAT = "anyuse.rea-registrations/1"
NAME = re.compile(r"[a-zA-Z0-9_-]{1,48}\Z")
MAX_BYTES = 16 * 1024 * 1024
MAX_PLAN_BYTES = 256 * 1024
MAX_OBSERVATION_BYTES = 256 * 1024


def plan_steps(steps):
    """Closed, bounded literal data. No result references or live-handle binding."""
    if type(steps) is not list or not 1 <= len(steps) <= 32:
        raise ValueError("REA plan requires 1–32 steps")
    def literal(value, depth=0):
        if depth > 16:
            raise ValueError("REA plan inputs exceed nesting budget")
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str or key.startswith("$"):
                    raise ValueError("REA plan cannot bind runtime handles or result references")
                literal(item, depth + 1)
        elif type(value) is list:
            for item in value:
                literal(item, depth + 1)
        elif type(value) is str:
            if re.search(r"\$(?:v|ref|handle)\d+\b", value):
                raise ValueError("REA plan cannot bind runtime handles or result references")
        elif value is not None and type(value) not in {int, float, bool}:
            raise ValueError("REA plan inputs must be pure JSON")
    for step in steps:
        if (type(step) is not dict or set(step) - {"name", "inputs", "expected_catalog_sha256"} or
                not {"name", "inputs"} <= set(step) or type(step["name"]) is not str or
                type(step["inputs"]) is not dict):
            raise ValueError("REA plan steps require only a registered name and typed inputs")
        if "expected_catalog_sha256" in step and not R.SHA.fullmatch(str(step["expected_catalog_sha256"])):
            raise ValueError("REA plan catalog pin must be SHA-256")
        literal(step["inputs"])
    if len(json.dumps(steps, ensure_ascii=False, allow_nan=False).encode()) > MAX_PLAN_BYTES:
        raise ValueError("REA plan exceeds byte budget")
    return copy.deepcopy(steps)


def _observation_kind(candidate):
    # Registered runtime/visual actors do not imply an admitted read interface.
    # Only frozen declarative AX selectors and the independent DOM SVG reader
    # are admitted, never actor getters, source or a newly chosen provider.
    from . import rea_harness as H
    return {H.BACKEND: "learned_ax", H.SVG_BACKEND: "learned_svg"}.get(candidate.get("backend"))


def _state_summary(result):
    """Reuse the replay's independent observation, without attaching again."""
    primary = result.get("operation", {}).get("result") if "completion_status" in result else result
    if not isinstance(primary, dict) or primary.get("verdict") != "verified" or "seen" not in primary:
        return None
    summary = {"observer": primary.get("observer"), "values": primary["seen"],
               "phase": "post_action_before_restoration" if "completion_status" in result else "post_action",
               "evidence_only": True, "proof_created": False}
    if len(json.dumps(summary, ensure_ascii=False, allow_nan=False).encode()) > MAX_OBSERVATION_BYTES:
        return {"omitted": "complete observation exceeds summary budget", "evidence_only": True,
                "proof_created": False}
    return copy.deepcopy(summary)


def _guest():
    if R.host_allowed():
        raise PermissionError("registered REA actions refuse inherited --allow-host; run inside the VM")
    R.assert_guest()


def _pin(path):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file() or
            path.resolve(strict=True) != path or path.stat().st_size > MAX_BYTES):
        raise ValueError("bounded canonical regular REA evidence file required")
    return {"file": str(path), "sha256": R._file_hash(path)}


def _schema(candidate):
    props = {}
    for param in candidate["params"]:
        # REA's string/path values remain strings. In particular decimal text
        # must not be coerced to a JSON number by a tool client.
        kind = {"string": "string", "path": "string", "integer": "integer", "boolean": "boolean"}.get(param["type"])
        if kind is None:
            raise ValueError("unsupported registered REA parameter type")
        props[param["name"]] = {"type": kind, "description": param.get("source", param["type"])}
        domain = candidate.get("policy", {}).get("input_constraints", {}).get(param["name"])
        if domain is not None:
            props[param["name"]]["description"] += "; measured domain: " + json.dumps(domain, sort_keys=True)
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}


def accepted(result):
    """Do not promote a model judgment or incomplete cleanup to verified."""
    if not isinstance(result, dict) or result.get("resource_review_required") or result.get("state_uncertain"):
        return False
    if "completion_status" in result:
        return (result["completion_status"] == "completed" and
                result.get("restoration", {}).get("status") == "restored" and
                accepted(result.get("operation", {}).get("result")))
    return (result.get("verdict") == "verified" or
            result.get("verdict") == "model_judged" and result.get("verification_tier") == "model_judged")


def _identity_bound(candidate):
    """Registrations always supply a process epoch; admit only replay routes that consume it."""
    from . import rea_harness as H
    if not H.identity_bound(candidate):
        raise ValueError("registered REA action requires a replay backend with process-identity binding")


class CatalogStore:
    def __init__(self, path=None, *, output_dir=None, timeout=300, restoration_provider=None):
        self.path = Path(path) if path is not None else anyuse_data_path("rea", "registrations.json")
        self.path = self.path.expanduser().absolute()
        self.output_dir = Path(output_dir) if output_dir is not None else self.path.parent / "executions"
        self.output_dir = self.output_dir.expanduser().absolute()
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 300:
            raise ValueError("REA execution timeout must be 1–300 seconds")
        self.timeout = timeout
        # Supplied by trusted application code, never by model tool arguments.
        # Called freshly for each execution; historical restoration is insufficient.
        self.restoration_provider = restoration_provider

    def _load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {"format": FORMAT, "registrations": {}}
        _pin(self.path)
        value = R.load_json(self.path)
        if (type(value) is not dict or set(value) != {"format", "registrations"} or
                value["format"] != FORMAT or type(value["registrations"]) is not dict or
                len(value["registrations"]) > 16):
            raise ValueError("invalid REA registration store")
        for name, item in value["registrations"].items():
            if (not NAME.fullmatch(name) or type(item) is not dict or
                    set(item) != {"catalog", "pins", "require_restoration"} or
                    not isinstance(item["catalog"], str) or not item["catalog"] or
                    type(item["require_restoration"]) is not bool or type(item["pins"]) is not list or
                    not 1 <= len(item["pins"]) <= 25):
                raise ValueError("invalid REA registration")
            for pin in item["pins"]:
                if (type(pin) is not dict or set(pin) != {"file", "sha256"} or
                        not isinstance(pin["file"], str) or not isinstance(pin["sha256"], str) or
                        not R.SHA.fullmatch(pin["sha256"])):
                    raise ValueError("invalid REA evidence pin")
        return value

    def _checked(self, item):
        _guest()  # before app/evidence IO, regardless of CLI process globals
        cat_path = Path(item["catalog"])
        cat_pin = _pin(cat_path)
        if cat_pin not in item["pins"]:
            raise ValueError("registered catalog changed")
        for pin in item["pins"]:
            if _pin(pin["file"]) != pin:
                raise ValueError("registered REA evidence changed")
        cat = R.load_json(cat_path)
        checked = A._actions(cat)
        for _, candidate, _, _ in checked:
            _identity_bound(candidate)
        expected = {_pin(cat_path)["file"]}
        expected.update(str(Path(a["entry"][key])) for a, _, _, _ in checked
                        for key in ("candidate", "proof", "card"))
        if expected != {p["file"] for p in item["pins"]}:
            raise ValueError("registered evidence closure changed")
        if cat.get("restoration") is not None and not item["require_restoration"]:
            raise ValueError("registered action lost required fresh restoration")
        if any(_pin(pin["file"]) != pin for pin in item["pins"]):
            raise ValueError("registered REA evidence changed during validation")
        return cat, checked

    def _lock(self):
        import fcntl
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.resolve(strict=True) != self.path.parent:
            raise ValueError("canonical REA registration directory required")
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        if lock.is_symlink():
            raise ValueError("REA registry lock cannot be a symlink")
        stream = os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+b")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        return stream

    def _outside_apps(self, checked):
        for action, _, _, _ in checked:
            bundle = Path(action["entry"]["bundle"]).resolve(strict=True)
            if any(path.resolve(strict=False).is_relative_to(bundle) for path in (self.path, self.output_dir)):
                raise ValueError("REA registration and execution records must remain outside app bundles")
        if self.output_dir.resolve(strict=False) != self.output_dir:
            raise ValueError("canonical REA execution directory required")

    def register(self, name, catalog, *, require_restoration=False):
        _guest()
        if not isinstance(name, str) or not NAME.fullmatch(name) or type(require_restoration) is not bool:
            raise ValueError("bounded registration name and explicit restoration policy required")
        path = Path(catalog).resolve(strict=True)
        cat = R.load_json(path)
        checked = A._actions(cat)
        self._outside_apps(checked)
        if any(not NAME.fullmatch(action["id"]) for action, _, _, _ in checked):
            raise ValueError("registered action id cannot be represented as a bounded tool name")
        paths = {path}
        paths.update(Path(a["entry"][key]) for a, _, _, _ in checked for key in ("candidate", "proof", "card"))
        item = {"catalog": str(path), "pins": [_pin(p) for p in sorted(paths)],
                "require_restoration": require_restoration or "restoration" in cat}
        self._checked(item)
        with self._lock():
            value = self._load()
            if name in value["registrations"] or len(value["registrations"]) >= 16:
                raise ValueError("REA registration exists or store is full; remove it explicitly first")
            value["registrations"][name] = item
            R.write_json(self.path, value)
        return self.list_tools()

    def unregister(self, name):
        with self._lock():
            value = self._load()
            if name not in value["registrations"]:
                raise ValueError("unknown REA registration")
            del value["registrations"][name]
            R.write_json(self.path, value)

    def list_tools(self):
        specs = []
        for name, item in sorted(self._load()["registrations"].items()):
            cat, checked = self._checked(item)
            view = A.prepare_selection(cat, "Describe the registered actions within their measured scope.")
            cards = {card["id"]: card for card in A._model_cards(view)}
            for action, candidate, _, card in checked:
                ident = action["id"]
                if not NAME.fullmatch(ident):
                    raise ValueError("registered action id cannot be represented as a tool name")
                tier = card["contract"].get("verification_tier", "deterministic")
                specs.append({"name": f"rea.{name}.{ident}", "registration": name, "action": ident,
                              "description": card["summary"] + "\n" + card["details"] +
                                  f"\nVerification: {tier}. Uses the ordinary REA replay gate; failures are terminal.",
                              "input_schema": _schema(candidate), "contract": cards[ident]["contract"],
                              "verification_tier": tier, "require_restoration": item["require_restoration"],
                              "observation_kind": _observation_kind(candidate),
                              "catalog_sha256": R.digest(cat)})
        if len(json.dumps(specs, ensure_ascii=False)) > 128000:
            raise ValueError("registered REA descriptions exceed context budget")
        return specs

    def execute(self, name, inputs, *, expected_catalog_sha256=None, expected_identity=None):
        """An explicitly selected registered tool; no second chooser or UI fallback."""
        started = False
        out = None
        try:
            from .actions import drive_enabled
            if not drive_enabled():
                raise PermissionError("registered REA execution requires ANYUSE_DRIVE_ENABLED")
            _guest()
            with self._lock():
                specs = self.list_tools()
                matches = [s for s in specs if s["name"] == name]
                if len(matches) != 1:
                    raise ValueError("unknown or ambiguous registered REA action")
                spec = matches[0]
                if expected_catalog_sha256 is not None and spec["catalog_sha256"] != expected_catalog_sha256:
                    raise ValueError("registered REA catalog differs from the frozen selection")
                item = self._load()["registrations"][spec["registration"]]
                cat, checked = self._checked(item)
                self._outside_apps(checked)
                action, candidate, _, _ = next(a for a in checked if a[0]["id"] == spec["action"])
                if not isinstance(inputs, dict):
                    raise ValueError("registered REA inputs must be an object")
                R.bind(candidate, inputs)
                if item["require_restoration"] and self.restoration_provider is None:
                    raise ValueError("registered action requires a fresh execution-restoration declaration")
                from . import foreground_interlock as F
                # The policy resolver belongs before observation/planning. A
                # prompt appearing during a plan only refuses the next primitive.
                F.require_clear("rea.catalog.execute")
                from . import rea_runtime_bridge as B
                identity = B.process_identity(Path(action["entry"]["bundle"]), candidate["target"]["bundle_id"])
                if expected_identity is not None and identity != dict(B._expected_identity(expected_identity)):
                    raise ValueError("registered REA process differs from the frozen plan epoch")
                restoration = (self.restoration_provider(copy.deepcopy(spec), copy.deepcopy(inputs), copy.deepcopy(identity))
                               if self.restoration_provider is not None else None)
                if item["require_restoration"] and restoration is None:
                    raise ValueError("fresh restoration provider returned no declaration")
                selection = {"format": A.SELECTION, "status": "selected", "catalog_sha256": R.digest(cat),
                             "action": action["id"], **{key: action[key] for key in ("candidate_sha256", "card_sha256")}}
                self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                out = Path(tempfile.mkdtemp(prefix="rea-", dir=self.output_dir)) / "replay"
                started = True
                result = A.execute(cat, selection, inputs, str(out), timeout=self.timeout,
                                   restoration=restoration, expected_identity=identity)
                primary = result.get("operation", {}).get("result") if "completion_status" in result else result
                primary = primary if isinstance(primary, dict) else {}
                matches_tier = (primary.get("verdict") == "model_judged" and
                                primary.get("verification_tier") == "model_judged"
                                if spec["verification_tier"] == "model_judged" else primary.get("verdict") == "verified")
                ok = accepted(result) and matches_tier
                summary = _state_summary(result) if ok else None
                return {"ok": ok, "status": "completed" if ok else "uncertain",
                        "name": name, "catalog_sha256": spec["catalog_sha256"], "identity": identity,
                        "verification_tier": spec["verification_tier"], "result": result,
                        **({"state_summary": summary} if summary is not None else {}),
                        "model_calls": primary.get("model_calls"), "output_dir": str(out),
                        "automatic_retry": False, "terminal_on_failure": True, "served_by": "rea"}
        except Exception as exc:
            retained = None
            if started:
                for filename in ("execution.json", "result.json", "replay.json"):
                    try:
                        path = out / filename
                        _pin(path)
                        retained = R.load_json(path)
                        break
                    except (ValueError, OSError):
                        pass
            return {"ok": False, "status": "uncertain" if started else "refused", "reason": str(exc),
                    "model_calls": None if started else 0, "output_dir": str(out) if out else None,
                    **({"result": retained} if retained is not None else {}),
                    "automatic_retry": False, "terminal_on_failure": True, "served_by": "rea"}

    def execute_plan(self, steps, *, execute_step=None):
        """Fail-fast sequence, not a transaction: completed effects are not rolled back.

        Preflight all inputs before the first effect. Each primitive reopens its
        pins through execute, retains its own receipt and fresh restoration, and
        refuses process successors. execute_step is trusted application code for
        per-step harness/BOUNDARY gates, never a model-provided callback.
        """
        receipt = {"ok": False, "status": "refused", "completed_steps": 0,
                   "failed_step_index": None, "executions": [], "model_calls": 0,
                   "automatic_retry": False, "terminal_on_failure": True, "served_by": "rea",
                   "verification_tier": "deterministic", "atomic": False}
        folder, dispatch_started = None, False
        try:
            from .actions import drive_enabled
            if not drive_enabled():
                raise PermissionError("registered REA plan requires ANYUSE_DRIVE_ENABLED")
            _guest()
            steps = plan_steps(steps)
            receipt["planned_steps"] = len(steps)
            with self._lock():
                specs = {s["name"]: s for s in self.list_tools()}
                prepared, epochs = [], {}
                from . import rea_runtime_bridge as B
                for index, step in enumerate(steps):
                    receipt["failed_step_index"] = index
                    spec = specs.get(step["name"])
                    if spec is None or spec["verification_tier"] != "deterministic":
                        raise ValueError("REA plans require registered deterministic actions")
                    if step.get("expected_catalog_sha256", spec["catalog_sha256"]) != spec["catalog_sha256"]:
                        raise ValueError("REA plan catalog differs from the frozen selection")
                    item = self._load()["registrations"][spec["registration"]]
                    _, checked = self._checked(item)
                    self._outside_apps(checked)
                    action, candidate, _, _ = next(a for a in checked if a[0]["id"] == spec["action"])
                    R.bind(candidate, step["inputs"])
                    if item["require_restoration"] and self.restoration_provider is None:
                        raise ValueError("REA plan requires fresh per-step restoration")
                    bundle = action["entry"]["bundle"]
                    identity = B.process_identity(Path(bundle), candidate["target"]["bundle_id"])
                    if bundle in epochs and identity != epochs[bundle]:
                        raise ValueError("REA process changed during plan preflight")
                    epochs[bundle] = identity
                    prepared.append((step, spec["catalog_sha256"], identity))
                self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                folder = Path(tempfile.mkdtemp(prefix="rea-plan-", dir=self.output_dir))
            receipt.update(status="running", failed_step_index=None, output_dir=str(folder))
            R.write_json(folder / "plan.json", receipt)
            dispatch = self.execute if execute_step is None else execute_step
            for index, (step, pin, identity) in enumerate(prepared):
                receipt["failed_step_index"] = index
                R.write_json(folder / "plan.json", receipt)
                dispatch_started = True
                try:
                    execution = dispatch(step["name"], copy.deepcopy(step["inputs"]),
                                         expected_catalog_sha256=pin, expected_identity=copy.deepcopy(identity))
                except Exception as exc:
                    execution = {"ok": False, "status": "uncertain", "reason": str(exc), "model_calls": None,
                                 "automatic_retry": False, "terminal_on_failure": True}
                if not isinstance(execution, dict):
                    execution = {"ok": False, "status": "uncertain", "reason": "REA step returned no execution receipt",
                                 "model_calls": None, "automatic_retry": False, "terminal_on_failure": True}
                receipt["executions"].append(execution)
                calls = execution.get("model_calls")
                receipt["model_calls"] = (receipt["model_calls"] + calls
                    if receipt["model_calls"] is not None and type(calls) is int and calls >= 0 else None)
                if execution.get("ok") is not True or execution.get("status") != "completed":
                    receipt.update(status=("refused" if receipt["completed_steps"] == 0 and
                                           execution.get("status") == "refused" else "uncertain"),
                                   reason=execution.get("reason") or "REA plan step did not complete")
                    R.write_json(folder / "plan.json", receipt)
                    return receipt
                receipt["completed_steps"] += 1
                receipt["failed_step_index"] = None
                if "state_summary" in execution:
                    receipt["state_summary"] = execution["state_summary"]
                R.write_json(folder / "plan.json", receipt)
            receipt.update(ok=True, status="completed", failed_step_index=None)
        except Exception as exc:
            receipt.update(ok=False, status="uncertain" if dispatch_started else "refused", reason=str(exc))
        if folder is not None:
            try:
                R.write_json(folder / "plan.json", receipt)
            except OSError as exc:
                receipt.update(ok=False, status="uncertain" if dispatch_started else "refused",
                               reason="REA plan receipt retention failed: " + str(exc))
        return receipt

    def observe(self, name, inputs, *, expected_catalog_sha256=None):
        """Read only the registered, proof-pinned observer; never execute the actor."""
        started, out = False, None
        try:
            _guest()
            from . import foreground_interlock as F
            from . import rea_runtime_bridge as B
            from . import rea_harness as H
            F.require_clear("rea.catalog.observe")
            with self._lock():
                matches = [s for s in self.list_tools() if s["name"] == name]
                if len(matches) != 1 or matches[0].get("observation_kind") not in {"learned_ax", "learned_svg"}:
                    raise ValueError("registered action has no supported read-only observer")
                spec = matches[0]
                if expected_catalog_sha256 is not None and expected_catalog_sha256 != spec["catalog_sha256"]:
                    raise ValueError("registered REA observer catalog changed")
                item = self._load()["registrations"][spec["registration"]]
                _, checked = self._checked(item)
                self._outside_apps(checked)
                action, candidate, proof, _ = next(a for a in checked if a[0]["id"] == spec["action"])
                R.bind(candidate, inputs)
                svg = spec["observation_kind"] == "learned_svg"
                profile_keys = ("target", "observer", "reference") if svg else ("target", "selectors", "reference")
                declaration = {k: candidate[k] for k in profile_keys}
                if len(json.dumps(declaration, ensure_ascii=False).encode()) > MAX_OBSERVATION_BYTES:
                    raise ValueError("pinned observer profile exceeds byte budget")
                # Both independent file inputs remain mandatory for SVG. A
                # complete mismatch is valid current-state evidence; its oracle
                # comparison never becomes an action-success claim here.
                reference = H.expected(candidate, inputs) if svg else None
                bundle = Path(action["entry"]["bundle"])
                identity = B.process_identity(bundle, candidate["target"]["bundle_id"])
                resources = H.resources(candidate)
                self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                out = Path(tempfile.mkdtemp(prefix="rea-observe-", dir=self.output_dir))
                started = True
                runtime = H.Runtime(bundle, candidate, expected_identity=identity)
                detail = {}
                if svg:
                    observation = H._svg_read(candidate, inputs, runtime, out / "svg", min(60, self.timeout),
                                              reference, resources)
                    if any(observation.get(key) != identity for key in ("identity", "after_identity", "expected_identity")):
                        raise ValueError("REA SVG observation differs from its expected process epoch")
                    field = next(iter(candidate["reference"]["fields"]))
                    values = {field: observation["values"]}
                    detail = {"oracle_matches": observation["matches"], "schema_sha256": observation["schema_sha256"],
                              "observer_receipt": str(out / "svg" / "observation.json"),
                              "visibility_proved": False, "geometry_proved": False, "connectivity_proved": False,
                              "persistence_proved": False}
                else:
                    values = H.observe(candidate, runtime.read(), inputs)
                if len(json.dumps(values, ensure_ascii=False, allow_nan=False).encode()) > MAX_OBSERVATION_BYTES:
                    raise ValueError("complete REA observation exceeds byte budget")
                F.require_clear("rea.catalog.observe.after")
                self._checked(item)
                if B.process_identity(bundle, candidate["target"]["bundle_id"]) != identity or H.resources(candidate) != resources:
                    raise ValueError("REA observer process/resources changed during capture")
                result = {"ok": True, "status": "observed", "name": name, "observation_kind": spec["observation_kind"],
                          "values": values, "identity": identity, **detail,
                          "catalog_sha256": spec["catalog_sha256"], "observer_sha256": R.digest(declaration),
                          "observed_monotonic": time.monotonic(), "output_dir": str(out), "actuated": False,
                          "evidence_only": True, "proof_created": False, "model_calls": 0}
                R.write_json(out / "observation.json", result)
                return result
        except Exception as exc:
            result = {"ok": False, "status": "uncertain" if started else "refused", "reason": str(exc),
                      "output_dir": str(out) if out else None, "actuated": False, "model_calls": 0,
                      "evidence_only": True, "proof_created": False,
                      "automatic_retry": False, "terminal_on_failure": True}
            if out is not None:
                try:
                    R.write_json(out / "observation.json", result)
                except OSError as retention_error:
                    result["retention_error"] = str(retention_error)
            return result
