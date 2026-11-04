"""Read-only preflight for cited native Objective-C actor facts.

This does not discover receivers, execute getters, choose a method, or prove an
effect. Only registered current-session native observations support facts. A
missing fact permits a new model decision; damaged or stale evidence does not.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from . import rea_mapping as R
from . import rea_runtime_bridge as B

ID = "native_objc_v1"
FORMAT = "anyuse.rea-actor-evidence-preflight/1"
MAX_FILE_BYTES = 16 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2
MAX_FILES = 128
MAX_SECONDS = 15


class EvidenceIntegrityError(RuntimeError):
    """Evidence cannot safely support either admission or correction."""


class MissingActorEvidence(ValueError):
    """An otherwise intact observation set does not support the proposed actor."""

    def __init__(self, message, binding):
        super().__init__(message)
        self.binding = copy.deepcopy(binding)


def _need(ok, message):
    if not ok:
        raise EvidenceIntegrityError(message)


def _same(left, right):
    return R.digest(left) == R.digest(right)


def _deadline(value):
    now = time.monotonic()
    _need(value is None or type(value) in {int, float} and math.isfinite(value),
          "actor evidence needs a finite read deadline")
    return min(now + MAX_SECONDS, value) if value is not None else now + MAX_SECONDS


def _time(deadline):
    _need(time.monotonic() < deadline, "actor evidence read deadline exhausted")


def _read(file, expected, deadline):
    _time(deadline)
    file = Path(file)
    before = file.lstat()
    _need(file.is_absolute() and file.resolve(strict=True) == file and
          stat.S_ISREG(before.st_mode) and not file.is_symlink() and
          0 <= before.st_size <= MAX_FILE_BYTES, "actor evidence needs bounded canonical regular files")
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    # NONBLOCK also prevents a raced-in FIFO from blocking before fstat rejects
    # it; it does not change reads from the required regular descriptor.
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        _need(stat.S_ISREG(opened.st_mode) and 0 <= opened.st_size <= MAX_FILE_BYTES and
              identity(opened) == identity(before), "actor evidence changed before opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(fd)
        current = file.lstat()
        _need(file.resolve(strict=True) == file and not file.is_symlink() and
              identity(before) == identity(after) == identity(current) and len(raw) == before.st_size,
              "actor evidence changed during reading")
    finally:
        os.close(fd)
    pin = {"file": str(file), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    _need(expected is None or type(expected) is dict and set(expected) == set(pin) and _same(pin, expected),
          "actor evidence pin changed")
    _time(deadline)
    return raw, pin


def _json(raw):
    def invalid(value):
        raise EvidenceIntegrityError("nonfinite actor evidence JSON")
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate actor evidence JSON key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=invalid, object_pairs_hook=pairs)


def _current(state, deadline):
    """Existing metadata checks only; no runtime transaction or getter call."""
    _time(deadline)
    bundle = Path(state["bundle"])
    _need(R.fingerprint(bundle) == state["target"], "actor evidence target bytes changed")
    identity = B.process_identity(bundle, state["target"]["bundle_id"])
    _time(deadline)
    adapter = B.adapter_identity()
    _time(deadline)
    return {"identity": identity, "adapter": adapter}


def _pointer(value, pointer):
    if not (type(pointer) is str and 0 < len(pointer) <= 1024 and pointer.startswith("/") and
            re.search(r"~(?![01])", pointer) is None):
        return None
    parts = tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))
    try:
        for key in parts:
            if type(value) is list:
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    return None
                value = value[int(key)]
            elif type(value) is dict:
                value = value[key]
            else:
                return None
    except (KeyError, IndexError, ValueError):
        return None
    return parts


def _method(value):
    _need(type(value) is dict and all(type(value.get(key)) is str and 0 < len(value[key]) <= 1024
          for key in ("class", "selector", "signature", "dispatch")) and value["dispatch"] in {"+", "-"},
          "malformed native method observation")
    return value


def _object(value):
    if value is None:
        return None
    _need(type(value) is dict and type(value.get("class")) is str and B._class_name(value["class"]) and
          type(value.get("handle")) is str and re.fullmatch(r"h[0-9]+", value["handle"]) and
          value.get("expires") == "transaction", "malformed native object observation")
    return value["class"]


def _preflight(session, state, request, deadline):
    root = Path(session)
    _need(root.is_absolute() and root.resolve(strict=True) == root and root.is_dir(),
          "canonical current session required for actor evidence")
    sources = [Path(__file__).resolve(), Path(R.__file__).resolve(), Path(B.__file__).resolve(),
               Path(B.__file__).with_name("rea_runtime_agent.js").resolve()]
    pins = {}
    def read(file, expected=None):
        raw, pin = _read(file, expected, deadline)
        old = pins.get(pin["file"])
        _need(old is None or _same(old, pin), "actor evidence changed between reads")
        pins[pin["file"]] = pin
        _need(len(pins) <= MAX_FILES and sum(p["bytes"] for p in pins.values()) <= MAX_TOTAL_BYTES,
              "actor evidence closure exceeds its file/byte bound")
        return raw
    for file in sources:
        read(file)
    _need(_same(_json(read(root / "session.json")), state), "actor evidence session snapshot changed")
    current = _current(state, deadline)
    artifacts = state["artifacts"]
    steps = state["steps"]
    _need(type(artifacts) is dict and type(steps) is list and len(steps) <= 64 and
          all(type(step) is dict and step.get("step") == i and step.get("status") == "completed"
              for i, step in enumerate(steps)), "actor evidence requires bounded completed session history")
    def registered(name):
        _need(type(name) is str and name in artifacts, "actor evidence is not registered in this session")
        file = root / name
        entry = artifacts[name]
        _need(file.is_relative_to(root) and type(entry) is dict and entry.get("file") == str(file),
              "registered actor evidence path differs")
        return _json(read(file, entry))
    citations = request.get("citations")
    _need(type(citations) is list and 1 <= len(citations) <= 16, "bounded actor citations required")
    cited, invalid_pointers = {}, []
    for citation in citations:
        _need(type(citation) is dict and set(citation) == {"artifact", "pointer"},
              "malformed actor evidence citation")
        name = citation["artifact"]
        cited.setdefault(name, [])
        point = _pointer(registered(name), citation["pointer"])
        if point is None:
            invalid_pointers.append(name)
        else:
            cited.setdefault(name, []).append(point)
    methods, classes, uniques, edges = [], [], [], []
    for step in steps:
        _time(deadline)
        base = f"s{step['step']:04d}"
        runtime_name, worker_name = base + "/runtime/runtime.json", base + "/runtime/worker-output.json"
        # Summary citations and unrelated registered data cannot supply raw facts.
        if not any(name.startswith(base + "/") for name in cited):
            continue
        g_request = step["request"]
        if g_request.get("op") not in {"discover", "runtime"} or g_request.get("provider", "native") != "native":
            continue
        _need(_same(registered(base + "/request.json"), g_request) and
              _same(registered(base + "/feedback.json"), step), "actor evidence lost its registered G dispatch")
        runtime, worker = registered(runtime_name), registered(worker_name)
        worker_request = registered(base + "/runtime/worker-request.json")
        transaction = B.validate_request(g_request["request"])
        identity, adapter = current["identity"], current["adapter"]
        _need(runtime.get("format") == B.RUNTIME and runtime.get("bundle") == worker_request.get("bundle") == state["bundle"] and
              _same(runtime.get("target"), state["target"]) and _same(worker_request.get("target"), state["target"]) and
              all(_same(value, identity) for value in (runtime.get("identity"), runtime.get("after_identity"),
                  worker_request.get("identity"), worker.get("identity"), worker.get("after_identity"))) and
              all(_same(value, adapter) for value in (runtime.get("adapter"), worker_request.get("adapter"))) and
              _same(runtime.get("transport"), adapter["transport"]) and
              _same(worker_request.get("transport"), adapter["transport"]),
              "actor evidence target/process/adapter binding changed")
        for expected in (step.get("expected_runtime_identity"), runtime.get("expected_identity")):
            _need(expected is None or _same(expected, identity), "actor evidence expected process epoch differs")
        _need(worker_request.get("format") == B.WORKER and
              _same(runtime.get("request"), transaction) and _same(worker_request.get("request"), transaction) and
              runtime.get("request_sha256") == worker.get("request_sha256") == R.digest(transaction) and
              _same(runtime.get("result"), worker) and runtime.get("worker", {}).get("sha256") == artifacts[worker_name]["sha256"],
              "actor evidence raw request/result/hash binding differs")
        _need(worker.get("format") == B.WORKER, "actor evidence needs the current worker envelope")
        B._worker_result(worker, R.digest(transaction), transaction, adapter["transport"])
        _need(runtime.get("resource_review_required") is False and runtime.get("actuated") is False and
              runtime.get("evidence_only") is True and runtime.get("verified") is False and
              runtime.get("proof_created") is False and type(runtime.get("model_calls")) is int and
              runtime["model_calls"] == 0 and runtime.get("native_calls") is bool(worker["native_calls"]) and
              worker.get("attached") is True and worker.get("handles_expired") is True and
              worker.get("native_state_uncertain") is False and
              worker.get("cleanup") == {"script_unloaded": True, "detached": True} and
              worker.get("cleanup_errors") == [] and worker.get("invocations") == 0 and
              type(worker.get("invocations")) is int and worker.get("helper_load_attempts") == 0 and
              type(worker.get("helper_load_attempts")) is int and
              runtime.get("worker", {}).get("timed_out") is False and
              type(runtime.get("worker", {}).get("returncode")) is int,
              "actor evidence has uncertain native effects or cleanup")
        process = worker.get("process")
        _need(type(process) is dict and process.get("pid") == identity["pid"] and
              process.get("executable") == identity["executable"], "actor evidence worker process differs")
        operations, rows = transaction["operations"], worker["operations"]
        negative = (runtime.get("status") == worker.get("status") == "incomplete" and
                    type(runtime.get("error")) is dict and type(worker.get("error")) is dict and
                    all(op["op"] in {"classes", "methods", "ivars"} for op in operations) and
                    type(worker.get("native_calls")) is int and worker["native_calls"] == 0 and
                    runtime.get("native_calls") is False and
                    step.get("feedback", {}).get("status") == "negative_metadata_observation")
        if negative:
            continue  # Clean negative metadata is intact evidence, never a positive fact.
        _need(runtime.get("status") == "observed" and worker.get("status") == "completed" and
              runtime.get("error") is None and worker.get("error") is None and
              runtime.get("worker", {}).get("returncode") == 0 and len(rows) == len(operations),
              "actor evidence observation is incomplete or failed")
        def covered(location):
            refs = []
            for name, prefix in ((runtime_name, ("result",)), (worker_name, ())):
                actual = prefix + location
                for point in cited.get(name, []):
                    if actual[:len(point)] == point or point[:len(actual)] == actual:
                        refs.append({"artifact": name, "pointer": "/" + "/".join(actual)})
            return refs
        for i, (operation, row) in enumerate(zip(operations, rows)):
            loc = ("operations", str(i))
            if row["op"] in {"methods", "classes"}:
                values = row.get("rows")
                _need(type(values) is list and len(values) <= operation.get("limit", B.MAX_ROWS),
                      "actor metadata rows exceed their requested bound")
                for j, value in enumerate(values):
                    _time(deadline)
                    refs = covered(loc + ("rows", str(j)))
                    if row["op"] == "methods":
                        meta = _method(value)
                        _need(meta["class"] == operation["class"], "actor method query class differs")
                        if refs:
                            methods.append((meta, refs))
                    else:
                        _need(type(value) is dict and B._class_name(value.get("name")), "malformed native class row")
                        if refs:
                            classes.append((value["name"], refs))
            elif row["op"] == "instances":
                actual_class = _object(row.get("object"))
                _need(actual_class is not None, "successful instance observation lost its object")
                refs = covered(loc + ("object",))
                if refs:
                    uniques.append((operation, actual_class, refs))
            elif row["op"] == "read":
                fields, values = operation["fields"], row.get("fields")
                _need(type(values) is list and len(values) == len(fields), "actor read fields differ")
                for j, (field, value) in enumerate(zip(fields, values)):
                    _need(type(value) is dict and all(value.get(k) == field[k] for k in ("name", "kind", "type")),
                          "actor read field does not match its request")
                    if field["kind"] != "getter":
                        continue
                    meta = _method(value.get("metadata"))
                    _need(meta["selector"] == field["selector"] and
                          ("signature" not in field or meta["signature"] == field["signature"]) and
                          ("expected_class" not in operation or meta["class"] == operation["expected_class"]) and
                          meta.get("return_type") == field["type"] and meta.get("argument_types") == [],
                          "actor getter metadata does not match its typed read")
                    returned = _object(value.get("value")) if field["type"] == "object" else None
                    refs = covered(loc + ("fields", str(j)))
                    if refs:
                        methods.append((meta, refs))
                        if returned is not None:
                            edges.append((meta, returned, refs))
    binding = {"format": FORMAT, "session": str(root), "state_sha256": R.digest(state),
               "request_sha256": R.digest(request), "current": current, "pins": list(pins.values()),
               "support": [], "proof_created": False, "native_calls": 0}
    _revalidate(binding, root, state, request, deadline)
    def fact(ok, message):
        if not ok:
            raise MissingActorEvidence(message, binding)
    fact(not invalid_pointers, "actor citation pointer does not exist in its registered artifact")
    actor = B.validate_actor(request["actor"], allow_bindings=True)
    receiver = actor["receiver"]
    fact(receiver.get("kind") in {"class", "unique"}, "actor evidence preflight needs a reusable class or unique receiver recipe")
    current_class, dispatch = receiver["class"], "+" if receiver["kind"] == "class" else "-"
    if receiver["kind"] == "unique":
        matches = [(cls, refs) for op, cls, refs in uniques if op["class"] == current_class and
                   op.get("subclasses", False) == receiver.get("subclasses", False)]
        fact(matches and len({cls for cls, _ in matches}) == 1,
             "unique receiver needs a cited successful live-instance observation matching class and subclasses")
        current_class = matches[0][0]
        binding["support"].append({"role": "unique_receiver", "class": current_class, "evidence": matches[0][1]})
    else:
        matches = [refs for cls, refs in classes if cls == current_class] + [refs for meta, refs in methods if meta["class"] == current_class]
        fact(matches, "class receiver needs cited runtime class or method metadata")
        binding["support"].append({"role": "class_receiver", "class": current_class, "evidence": matches[0]})
    for i, getter in enumerate(receiver.get("path", [])):
        matches = [(cls, refs) for meta, cls, refs in edges if meta["class"] == current_class and meta["dispatch"] == dispatch and
                   meta["selector"] == getter["selector"] and meta["signature"] == getter["signature"]]
        fact(matches and len({cls for cls, _ in matches}) == 1,
             f"receiver getter {i} needs cited exact dispatch/signature and an observed non-nil returned class")
        binding["support"].append({"role": "getter", "index": i, "receiver_class": current_class,
                                  "returned_class": matches[0][0], "evidence": matches[0][1]})
        current_class, dispatch = matches[0][0], "-"
    matches = [(meta, refs) for meta, refs in methods if meta["class"] == current_class and meta["dispatch"] == dispatch and
               meta["selector"] == actor["selector"] and meta["signature"] == actor["signature"] and
               meta.get("origin") == "module_backed" and type(meta.get("module")) is str and
               Path(meta["module"]).is_absolute() and ".." not in Path(meta["module"]).parts and
               type(meta.get("implementation_offset")) is str and re.fullmatch(r"0x[0-9a-f]+", meta["implementation_offset"])]
    fact(matches, "goal method needs cited exact terminal class/dispatch/selector/signature and attributed module metadata")
    binding["support"].append({"role": "goal_method", "receiver_class": current_class, "evidence": matches[0][1]})
    return binding


def _revalidate(binding, session, state, request, deadline):
    _need(type(binding) is dict and binding.get("format") == FORMAT and binding.get("session") == str(session) and
          binding.get("state_sha256") == R.digest(state) and binding.get("request_sha256") == R.digest(request),
          "actor evidence admission binding changed")
    pins = binding.get("pins")
    _need(type(pins) is list and len(pins) <= MAX_FILES and sum(p["bytes"] for p in pins) <= MAX_TOTAL_BYTES,
          "actor evidence admission closure changed")
    for pin in pins:
        _read(pin["file"], pin, deadline)
    _need(_same(_current(state, deadline), binding["current"]), "actor evidence process or adapter changed before dispatch")
    # Metadata reads may take time. Keep the consumed closure stable across
    # that freshness check as well, rather than accepting a changed source.
    for pin in pins:
        _read(pin["file"], pin, deadline)


def preflight(session, state, request, *, deadline=None):
    """Return a pinned evidence binding, or None for other requests/actor families."""
    if request is None or request.get("op") != "propose" or request.get("actor", {}).get("kind") != "objc_method":
        return None
    try:
        return _preflight(session, state, request, _deadline(deadline))
    except (MissingActorEvidence, EvidenceIntegrityError):
        raise
    except Exception as error:
        raise EvidenceIntegrityError("actor evidence integrity check failed: " + str(error)[:1024]) from error


def revalidate(binding, session, state, request, *, deadline=None):
    """Reopen the consumed closure and current identity; never redo native work."""
    if binding is None:
        return
    try:
        _revalidate(binding, Path(session), state, request, _deadline(deadline))
    except EvidenceIntegrityError:
        raise
    except Exception as error:
        raise EvidenceIntegrityError("actor evidence revalidation failed: " + str(error)[:1024]) from error
