"""Caller-selected AppKit receiver observations over the existing G tools.

This scaffold executes getters. It supplies no task method, actor, proposal or
effect proof. Every transaction expires its aliases; only measured recipes and
registered evidence citations survive. Importing it performs no inspection.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from . import rea_agent as G
from . import rea_runtime_bridge as B
from . import rea_runtime_providers as V

ID = "objc_appkit_receiver_v1"
FORMAT = "anyuse.rea-receiver-probe/1"
INDEX = "anyuse.rea-receiver-observations/1"
ROUTES = {"main_window": "mainWindow", "key_window": "keyWindow"}
MAX_STEPS = 7
MAX_NATIVE_CALLS = 6  # Prefixes are executed again: 1 + 2 + 3, not three.
# Eight transactions may each consume their full worker allowance, in addition
# to app/source hashing and evidence registration between transactions. Keep a
# bounded total that can accommodate that work without shrinking a late worker
# to a startup-only timeout.
MAX_SECONDS = 180
TRANSACTION_SECONDS = 15
EVIDENCE_BYTES = 128 * 1024**2
HEADROOM_BYTES = 128 * 1024**2
CLEANUP_SECONDS = 5
CLASS_QUERY_LIMIT = 32
CLASS_INDEX_BYTES = 64 * 1024
CLASS_INVENTORY = "anyuse.rea-task-class-inventory/1"


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _pin(file):
    raw = file.read_bytes()
    return {"file": str(file), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _read(pin):
    file = Path(pin["file"])
    _need(not file.is_symlink() and file.resolve(strict=True) == file and
          file.is_file() and 0 < file.stat().st_size <= G.R.MAX_JSON_BYTES,
          "receiver evidence must be a nonempty bounded canonical JSON file")
    _need(set(pin) <= {"file", "bytes", "sha256", "artifact"} and
          _pin(file) == {key: pin.get(key) for key in ("file", "bytes", "sha256")}, "receiver evidence changed")
    return G.R.load_json(file)


def _write(file, value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    _need(len(raw) <= 256 * 1024, "receiver index exceeds its bound")
    file.write_bytes(raw)


def _session(root):
    raw = (root / "session.json").read_bytes()
    _need(len(raw) <= G.R.MAX_JSON_BYTES, "session exceeds its bound")
    context = G.context(str(root))
    _need((root / "session.json").read_bytes() == raw, "session changed while reading receiver context")
    return raw, json.loads(raw), context


def _frozen(state):
    keys = ("format", "bundle", "target", "task", "params", "success_criteria", "plan",
            "plan_sha256", "observer_profile_sha256", "max_steps", "deadline")
    return G.R.digest({key: state.get(key) for key in keys})


def _storage_paths(root, folder):
    """Both evidence destinations need admission; avoid charging one disk twice."""
    devices = {}
    for path in (root, folder):
        devices.setdefault(path.stat().st_dev, path)
    return list(devices.values())


def _class_queries(value, task):
    """Caller search terms are task substrings, never seeded app class names."""
    if value is None:
        return None
    _need(type(value) is list and 1 <= len(value) <= 4 and type(task) is str,
          "class inventory requires one to four frozen task-literal queries")
    _need(all(type(term) is str and 1 <= len(term) <= 64 and term == term.strip() and
              all(character.isprintable() for character in term) and
              term.casefold() in task.casefold() for term in value) and
          len({term.casefold() for term in value}) == len(value),
          "class inventory queries must be distinct bounded literal substrings of the frozen task")
    return copy.deepcopy(value)


def _class_inventory(queries, task, binding, directory, rows, source, bundle, executable):
    """Derive a compact metadata index; omissions never mean absent classes."""
    _need(type(rows) is list and len(rows) == len(queries), "class inventory operation count differs")
    value = {"format": CLASS_INVENTORY, "queries": queries, "task_sha256": G.R.digest(task),
             "session_binding": binding, "g_step_directory": directory, "source": source,
             "provenance": "harness_metadata_scaffold_not_model_decisions_or_receiver_or_task_proof",
             "max_rows_per_query": CLASS_QUERY_LIMIT, "max_indexed_rows": 128,
             "maximum_json_bytes": CLASS_INDEX_BYTES, "operations": [],
             "scope": "Historical exact class names and module metadata only. Each query is a task-literal search hypothesis. Truncated or omitted rows remain unknown. Class names do not establish a live receiver, method ABI, ownership of a task method or an effect. Use exact observed names before methods/ivars and read the registered raw rows for complete returned metadata."}
    for position, (query, row) in enumerate(zip(queries, rows)):
        actual = row.get("rows")
        matched, truncated = row.get("matched"), row.get("truncated")
        _need(row.get("index") == position and row.get("op") == "classes" and row.get("status") == "ok" and
              type(actual) is list and type(matched) is int and matched >= 0 and
              len(actual) == min(matched, CLASS_QUERY_LIMIT) and type(truncated) is bool and
              truncated is (matched > len(actual)), "class inventory rows/count/truncation differ")
        for item in actual:
            _need(type(item) is dict and B._class_name(item.get("name")) and
                  query.casefold() in item["name"].casefold() and item.get("origin") == "module_backed" and
                  B._text(item.get("module"), 4096) and
                  (item["module"] == executable or item["module"].startswith(bundle + "/")),
                  "class inventory lacks attributable task-query metadata")
        _need(len({item["name"] for item in actual}) == len(actual), "class inventory names are duplicated")
        value["operations"].append({"query": query, "matched": matched, "truncated": truncated,
            "rows_in_artifact": len(actual), "summary_omitted_rows": len(actual), "rows": []})
    # Keep whole rows within a separate index-byte budget; raw registered rows
    # remain available and every projection omission is explicit.
    for position, row in enumerate(rows):
        entry = value["operations"][position]
        for offset, item in enumerate(row["rows"]):
            entry["rows"].append({**{key: item[key] for key in ("name", "module", "origin")},
                                  "pointer": f"/operations/{position}/rows/{offset}"})
            entry["summary_omitted_rows"] -= 1
            if len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()) > CLASS_INDEX_BYTES:
                entry["rows"].pop()
                entry["summary_omitted_rows"] += 1
                break
    _need(len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()) <= CLASS_INDEX_BYTES,
          "class inventory index exceeds its byte bound")
    return value


def _returned(rows, measured, previous):
    """Separate an explicit final null from missing or malformed observations."""
    returned = []
    nil = False
    for i, row in enumerate(rows):
        fields = row.get("fields")
        _need(type(fields) is list and len(fields) == 1, "receiver getter fields are incomplete")
        field = fields[0]
        _need(field.get("name") == "receiver" and field.get("kind") == "getter" and
              field.get("type") == "object" and "value" in field,
              "receiver getter value or typed field is missing")
        metadata = field.get("metadata")
        _need(type(metadata) is dict and all(metadata.get(key) == item for key, item in measured[i].items()) and
              metadata.get("return_type") == "object" and metadata.get("argument_types") == [],
              "getter runtime contract differs from its measured metadata")
        value = field["value"]
        if value is None:
            _need(i == len(measured) - 1 and i == len(rows) - 1,
                  "receiver prefix became nil; no route continuation")
            nil = True
        else:
            _need(type(value) is dict and B._class_name(value.get("class")) and value.get("expires") == "transaction",
                  "receiver getter returned an unsupported object")
            returned.append(value["class"])
    _need((returned if nil else returned[:-1]) == previous,
          "receiver prefix class changed; no route continuation")
    return returned, nil


def _edge(depth, measured, klass, method_pin, observation_pin, offset, class_pin):
    return {"hop": depth, "class": klass,
            "recipe": {"class": "NSApplication", "kind": "class", "path": [
                {"selector": item["selector"], "signature": item["signature"]} for item in measured]},
            "getter": measured[-1],
            "metadata_citation": {**method_pin, "pointer": f"/operations/0/rows/{offset}"},
            "observation_citation": {**observation_pin, "pointer": f"/operations/{depth}/fields/0/value/class"},
            "root_citation": {**class_pin, "pointer": "/operations/0/rows"},
            "source": "harness_scaffold_measured_getter_not_task_or_effect_proof"}


def _nil(depth, measured, method_pin, observation_pin, offset, class_pin):
    # No available receiver/class/recipe is inferred for a measured null.
    return {"reason": "measured_nil", "hop": depth, "prefix_length": depth,
            "getter": measured[-1],
            "metadata_citation": {**method_pin, "pointer": f"/operations/0/rows/{offset}"},
            "null_citation": {**observation_pin, "pointer": f"/operations/{depth}/fields/0/value"},
            "root_citation": {**class_pin, "pointer": "/operations/0/rows"},
            "source": "harness_scaffold_measured_nil_not_available_receiver_or_task_proof"}


def run(session, out, *, route="main_window", timeout_seconds=MAX_SECONDS,
        deadline_monotonic=None, class_queries=None):
    """Observe one route once; failure retains evidence and never retries.

    Bounds are cooperative between existing bounded G calls, including their
    existing worker cleanup. The caller deadline is never extended. Storage is
    reserved before calls and measured afterward; it is not an OS disk quota.
    """
    G.R.assert_guest()
    _need(type(route) is str and route in ROUTES, "select one supported receiver route")
    _need(type(timeout_seconds) is int and 1 <= timeout_seconds <= MAX_SECONDS,
          f"receiver timeout must be 1–{MAX_SECONDS} seconds")
    _need(deadline_monotonic is None or type(deadline_monotonic) in {int, float} and
          math.isfinite(deadline_monotonic), "invalid caller receiver deadline")
    root, folder = Path(session).resolve(strict=True), Path(out).resolve()
    _need(not folder.exists() and not folder.is_relative_to(root) and not root.is_relative_to(folder),
          "receiver probe requires new output separate from the session")
    _, initial, _ = _session(root)
    queries = _class_queries(class_queries, initial["task"])
    max_steps = MAX_STEPS + int(queries is not None)
    app = Path(initial["bundle"]).resolve(strict=True)
    _need(not folder.is_relative_to(app), "receiver evidence must be outside the app")
    _need(initial.get("status") == "investigating" and initial.get("hypothesis") is None and
          not initial.get("proof") and not initial.get("recordings") and
          all(row.get("status") == "completed" and not row.get("feedback", {}).get("error") and
              row.get("feedback", {}).get("status") not in {"incomplete", "unavailable", "failed", "refused", "uncertain"}
              for row in initial.get("steps", [])),
          "receiver scaffold requires an unfailed investigation before proposal or trials")
    _need(initial["max_steps"] - len(initial["steps"]) >= max_steps,
          "receiver scaffold needs seven remaining G steps" if queries is None else
          "receiver scaffold and class inventory need eight remaining G steps")
    folder.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    deadline = min(started + timeout_seconds,
                   float("inf") if deadline_monotonic is None else deadline_monotonic)
    sources = [_pin(Path(module.__file__).resolve()) for module in (G, B, V)]
    sources += [_pin(Path(__file__).resolve()), _pin(Path(B.__file__).with_name("rea_runtime_agent.js").resolve())]
    result = {"format": FORMAT, "routine": ID, "route": route, "source": "caller_selected_harness_scaffold",
              "session": str(root), "target": initial["target"], "status": "starting", "error": None,
              "model_calls": 0, "g_step_dispatches": 0, "native_calls": 0, "known_native_calls": 0,
              "task_invocations": 0, "proof_created": False, "retry": False,
              "bounds": {"steps": max_steps, "native_calls": MAX_NATIVE_CALLS,
                         "timeout_seconds": timeout_seconds, "evidence_bytes": EVIDENCE_BYTES,
                         "free_headroom_bytes": HEADROOM_BYTES, "cleanup_seconds": CLEANUP_SECONDS},
              "sources": sources, "identity": None, "adapter": None, "steps": [],
              "observations": [], "started_monotonic": started, "finished_monotonic": None}
    if queries is not None:
        result["class_inventory_queries"] = queries
    binding = _frozen(initial)
    last_raw = (root / "session.json").read_bytes()
    pins = []

    def evidence_size():
        paths = [folder, *[root / row["directory"] for row in result["steps"]]]
        total = 0
        for directory in paths:
            for file in directory.rglob("*"):
                _need(not file.is_symlink(), "receiver evidence contains a symlink")
                if file.is_file():
                    total += file.stat().st_size
        return total

    def retain():
        index = {"format": INDEX, "routine": ID, "route": route,
                 "provenance": "harness_scaffold_observations_not_model_discoveries_or_task_proof",
                 "session": str(root), "target": initial["target"], "identity": result["identity"],
                 "status": result["status"], "observations": result["observations"],
                 "scope": "Historical process-bound getter observations. Class equality does not establish object/window identity. Recipes re-resolve current objects; independent task-context/effect gates and revalidation remain required. No durable handles or task-method selection."}
        if "unavailable" in result:
            index["unavailable"] = copy.deepcopy(result["unavailable"])
        if "class_inventory" in result:
            index["class_inventory"] = copy.deepcopy(result["class_inventory"])
        _write(folder / "receiver-index.json", index)
        result["index"] = _pin(folder / "receiver-index.json")
        _write(folder / "probe.json", result)

    def check():
        nonlocal last_raw
        _need(time.monotonic() + CLEANUP_SECONDS < deadline, "receiver time reserve exhausted")
        _need(time.time() + CLEANUP_SECONDS < initial["deadline"], "G session time reserve exhausted")
        _need(all(_pin(Path(p["file"])) == p for p in sources), "receiver policy source changed")
        for pin in pins:
            _read(pin)
        raw, state, _ = _session(root)
        _need(raw == last_raw and _frozen(state) == binding, "receiver session changed outside this routine")
        _need(state["status"] == "investigating" and
              all(row.get("status") == "completed" for row in state["steps"]),
              "receiver session requires review")
        if result["identity"] is not None:
            _need(B.process_identity(app, initial["target"]["bundle_id"]) == result["identity"],
                  "receiver process changed; no successor adoption")
            _need(B.adapter_identity() == result["adapter"], "receiver adapter changed")
        size = evidence_size()
        _need(size <= EVIDENCE_BYTES, "receiver evidence budget exhausted")
        for destination in _storage_paths(root, folder):
            _need(shutil.disk_usage(destination).free >= HEADROOM_BYTES + EVIDENCE_BYTES - size,
                  "receiver storage reserve unavailable on evidence destination: " + str(destination))

    def dispatch(op, operations, calls):
        nonlocal last_raw
        check()
        _need(result["g_step_dispatches"] < max_steps and
              result["known_native_calls"] + calls <= MAX_NATIVE_CALLS,
              "receiver step/native-call bound exhausted")
        remaining = min(deadline - time.monotonic(), initial["deadline"] - time.time())
        _need(remaining >= TRANSACTION_SECONDS + CLEANUP_SECONDS,
              "receiver time reserve cannot fit a full native transaction and cleanup; no dispatch")
        timeout = TRANSACTION_SECONDS
        request = {"op": op, "provider": "native", "timeout": timeout,
                   "request": {"operations": operations}}
        # G's private identity handoff reaches B.collect before worker creation.
        entry = {"directory": f"s{len(json.loads(last_raw)['steps']):04d}",
                 "request": request, "status": "running", "expected_native_calls": calls,
                 "native_calls": None, "provenance": "harness_scaffold"}
        result["steps"].append(entry)
        result["g_step_dispatches"] += 1
        result["native_calls"] = None  # Unknown until a retained worker proves its actual count.
        retain()
        observed = G.step(str(root), request, expected_runtime_identity=result["identity"])
        last_raw = (root / "session.json").read_bytes()
        _, state, _ = _session(root)
        base = root / entry["directory"]
        def registered(file):
            name = file.relative_to(root).as_posix()
            pin = state["artifacts"].get(name)
            _need(type(pin) is dict and pin.get("file") == str(file), "receiver artifact is not G registered")
            value = _read(pin)
            pins.append(copy.deepcopy(pin))
            return value, {"artifact": name, **copy.deepcopy(pin)}
        receipt, receipt_pin = registered(base / "runtime/runtime.json")
        entry["receipt"] = receipt_pin
        # A timed-out worker may retain an empty stdout file. Its parent
        # receipt is the actual failure evidence; do not JSON-decode stdout and
        # replace that failure with a secondary parser error. Counts from prior
        # completed workers remain known, while this transaction stays unknown.
        runtime_error = receipt.get("error")
        if not isinstance(receipt.get("result"), dict) and isinstance(runtime_error, dict):
            entry.update(status="failed", runtime_error=copy.deepcopy(runtime_error))
            result["resource_review_required"] = receipt.get("resource_review_required") is True
            if runtime_error.get("type") == "TimeoutError":
                raise TimeoutError(str(runtime_error.get("message", "runtime worker timed out")))
            raise ValueError("receiver runtime failed: " + str(runtime_error.get("type", "RuntimeError")) +
                             ": " + str(runtime_error.get("message", "native observation unavailable")))
        worker, worker_pin = registered(base / "runtime/worker-output.json")
        entry["worker"] = worker_pin
        count = worker.get("native_calls")
        if type(count) is int and count >= 0:
            entry["native_calls"] = count
            result["known_native_calls"] += count
            result["native_calls"] = result["known_native_calls"]
        _need(receipt.get("format") == B.RUNTIME and receipt.get("request") == request["request"] and
              receipt.get("request_sha256") == G.R.digest(request["request"]) == worker.get("request_sha256") and
              receipt.get("bundle") == str(app) and receipt.get("target") == initial["target"] and
              receipt.get("expected_identity") == receipt.get("identity") == receipt.get("after_identity") == result["identity"] and
              receipt.get("adapter") == result["adapter"] and receipt.get("result") == worker,
              "receiver runtime observation lost request/process/adapter binding")
        _need(receipt.get("resource_review_required") is False and receipt.get("error") is None and
              receipt.get("status") == "observed" and worker.get("format") == B.WORKER and
              worker.get("status") == "completed" and worker.get("error") is None and worker.get("attached") is True and
              worker.get("handles_expired") is True and worker.get("native_state_uncertain") is False and
              worker.get("cleanup") == {"script_unloaded": True, "detached": True} and
              worker.get("cleanup_errors") == [] and worker.get("invocations") == 0 and
              worker.get("helper_load_attempts") == 0 and count == calls and
              observed["feedback"].get("status") == "completed",
              "receiver getter failed or cleanup/native disposition requires review; no retry")
        rows = worker.get("operations")
        _need(type(rows) is list and len(rows) == len(operations) and
              all(row.get("index") == i and row.get("op") == operation["op"] and row.get("status") == "ok"
                  for i, (row, operation) in enumerate(zip(rows, operations))),
              "receiver operation observations are incomplete")
        entry["status"] = "completed"
        check()
        retain()
        return rows, worker_pin

    try:
        check()
        result["identity"] = dict(B.process_identity(app, initial["target"]["bundle_id"]))
        B._expected_identity(result["identity"])
        result["adapter"] = B.adapter_identity()
        retain()
        classes, class_pin = dispatch("discover", [{"op": "classes", "query": "NSApplication", "owner": "all", "limit": 128}], 0)
        matches = [row for row in classes[0].get("rows", []) if row.get("name") == "NSApplication"]
        _need(classes[0].get("truncated") is False and len(matches) == 1 and
              matches[0].get("origin") == "module_backed" and type(matches[0].get("module")) is str,
              "AppKit root search did not establish one attributable NSApplication class")
        route_selectors = ["sharedApplication", ROUTES[route], "windowController"]
        measured = []
        classes_seen = []
        for depth, selector in enumerate(route_selectors):
            klass = "NSApplication" if depth == 0 else classes_seen[-1]
            methods, method_pin = dispatch("discover", [{"op": "methods", "class": klass,
                "query": selector, "own": False, "limit": 128}], 0)
            dispatch_kind = "+" if depth == 0 else "-"
            found = [(i, row) for i, row in enumerate(methods[0].get("rows", []))
                     if row.get("selector") == selector and row.get("dispatch") == dispatch_kind]
            _need(methods[0].get("truncated") is False and len(found) == 1, "one exact measured getter is required")
            offset, method = found[0]
            _need(method.get("class") == klass and method.get("origin") == "module_backed" and
                  type(method.get("module")) is str and type(method.get("implementation_offset")) is str,
                  "getter lacks measured class/module attribution")
            B._signature(selector, method.get("signature"), [], "object")
            measured.append(copy.deepcopy(method))
            operations = []
            for i, item in enumerate(measured):
                receiver = {"class": "NSApplication", "kind": "class"} if i == 0 else {"alias": f"receiver{i-1}"}
                operations.append({"op": "read", "receiver": receiver, "expected_class": item["class"],
                    "fields": [{"name": "receiver", "kind": "getter",
                    "type": "object", "selector": item["selector"], "signature": item["signature"], "alias": f"receiver{i}"}]})
            rows, observation_pin = dispatch("runtime", operations, depth + 1)
            returned, nil = _returned(rows, measured, classes_seen)
            if nil:
                result.update(status="route_unavailable", unavailable=_nil(
                    depth, measured, method_pin, observation_pin, offset, class_pin))
                break
            classes_seen = returned
            result["observations"].append(_edge(
                depth, measured, returned[-1], method_pin, observation_pin, offset, class_pin))
            retain()
        else:
            result["status"] = "observed"
        if queries is not None:
            rows, source = dispatch("discover", [{"op": "classes", "owner": "app", "query": term,
                "limit": CLASS_QUERY_LIMIT} for term in queries], 0)
            result["class_inventory"] = _class_inventory(queries, initial["task"], binding,
                result["steps"][-1]["directory"], rows, source, str(app), result["identity"]["executable"])
    except BaseException as error:
        result.update(status="interrupted" if not isinstance(error, Exception) else "stopped_failure",
                      error={"type": type(error).__name__, "message": str(error)[:4096]})
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            _need(all(_pin(Path(p["file"])) == p for p in sources), "receiver policy source changed")
            for pin in pins:
                _read(pin)
            result["evidence_bytes"] = evidence_size()
            _need(result["evidence_bytes"] <= EVIDENCE_BYTES, "receiver evidence budget exhausted")
            if result["status"] in {"observed", "route_unavailable"}:
                check()
        except Exception as error:
            result["final_review_error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
            result["status"] = "stopped_failure"
        result["finished_monotonic"] = time.monotonic()
        retain()
    return result


def validate(result, session):
    """Reopen the compact index and its raw G registrations before later use."""
    root = Path(session).resolve(strict=True)
    _need(result.get("format") == FORMAT and result.get("status") in {"observed", "route_unavailable"} and
          result.get("session") == str(root), "receiver scaffold is not a completed observation")
    _, state, _ = _session(root)
    _need(state["target"] == result["target"] and
          B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]) == result["identity"] and
          B.adapter_identity() == result["adapter"], "receiver scaffold process/adapter changed")
    for pin in result["sources"]:
        _need(_pin(Path(pin["file"])) == pin, "receiver scaffold source changed")
    index = _read(result["index"])
    _need(index.get("format") == INDEX and index.get("status") == result["status"] and
          index.get("observations") == result["observations"] and index.get("identity") == result["identity"] and
          index.get("target") == result["target"] and index.get("session") == str(root),
          "receiver scaffold index differs from the actual result")
    for step in result["steps"]:
        for key in ("receipt", "worker"):
            pin = step[key]
            _need(state["artifacts"].get(pin["artifact"]) == {k: v for k, v in pin.items() if k != "artifact"},
                  "receiver scaffold raw source is no longer G registered")
            _read(pin)
    if result["status"] == "route_unavailable":
        _validate_nil(result, state, root, index)
    if "class_inventory_queries" in result or "class_inventory" in result or "class_inventory" in index:
        _validate_class_inventory(result, state, root, index)
    return index


def _validate_nil(result, state, root, index):
    """Re-derive a clean unavailable route from actual registered native rows."""
    unavailable = result.get("unavailable")
    _need(type(unavailable) is dict and unavailable.get("reason") == "measured_nil" and
          type(unavailable.get("hop")) is int and unavailable["hop"] in range(3) and
          index.get("unavailable") == unavailable and result.get("routine") == ID and
          result.get("route") in ROUTES and index.get("routine") == ID and
          index.get("route") == result["route"] and
          index.get("provenance") == "harness_scaffold_observations_not_model_discoveries_or_task_proof",
          "receiver unavailable result is not an explicit measured nil")
    depth = unavailable["hop"]
    inventory_steps = int("class_inventory_queries" in result)
    calls = (depth + 1) * (depth + 2) // 2
    bounds = result.get("bounds", {})
    timeout = bounds.get("timeout_seconds")
    started, finished = result.get("started_monotonic"), result.get("finished_monotonic")
    _need(type(timeout) is int and 1 <= timeout <= MAX_SECONDS and bounds == {
          "steps": MAX_STEPS + inventory_steps, "native_calls": MAX_NATIVE_CALLS, "timeout_seconds": timeout,
          "evidence_bytes": EVIDENCE_BYTES, "free_headroom_bytes": HEADROOM_BYTES, "cleanup_seconds": CLEANUP_SECONDS} and
          all(type(value) in {int, float} and math.isfinite(value) for value in (started, finished)) and
          0 <= finished - started <= timeout,
          "receiver nil time/storage bounds differ")
    _need(result.get("g_step_dispatches") == len(result["steps"]) == 3 + 2 * depth + inventory_steps and
          result.get("native_calls") == result.get("known_native_calls") == calls and
          result.get("model_calls") == result.get("task_invocations") == 0 and
          result.get("retry") is False and result.get("proof_created") is False and
          result.get("error") is None and result.get("final_review_error") is None and
          type(result.get("evidence_bytes")) is int and 0 <= result["evidence_bytes"] <= EVIDENCE_BYTES and
          result.get("source") == "caller_selected_harness_scaffold",
          "receiver nil disposition/counts/bounds are not clean")
    expected_sources = [_pin(Path(module.__file__).resolve()) for module in (G, B, V)]
    expected_sources += [_pin(Path(__file__).resolve()), _pin(Path(B.__file__).with_name("rea_runtime_agent.js").resolve())]
    _need(result["sources"] == expected_sources, "receiver nil source closure differs")

    def registered(file):
        name = file.relative_to(root).as_posix()
        pin = state["artifacts"].get(name)
        _need(type(pin) is dict and pin.get("file") == str(file), "receiver nil raw evidence is not G registered")
        return _read(pin), {"artifact": name, **copy.deepcopy(pin)}

    raw = []
    ordinals = []
    for position, step in enumerate(result["steps"][:3 + 2 * depth]):
        expected_calls = 0 if position == 0 or position % 2 else position // 2
        directory = step.get("directory", "")
        _need(type(directory) is str and len(directory) == 5 and directory.startswith("s") and
              directory[1:].isdigit(), "receiver nil step directory differs")
        ordinal = int(directory[1:])
        ordinals.append(ordinal)
        base = root / directory
        g = state["steps"][ordinal]
        request, _ = registered(base / "request.json")
        feedback, _ = registered(base / "feedback.json")
        receipt, receipt_pin = registered(base / "runtime/runtime.json")
        worker, worker_pin = registered(base / "runtime/worker-output.json")
        worker_request, _ = registered(base / "runtime/worker-request.json")
        _need(step.get("status") == "completed" and step.get("provenance") == "harness_scaffold" and
              step.get("expected_native_calls") == step.get("native_calls") == expected_calls and
              step.get("request") == request == g.get("request") and feedback == g and
              g.get("step") == ordinal and g.get("status") == "completed" and
              g.get("expected_runtime_identity") == result["identity"] and
              request.get("provider") == "native" and type(request.get("timeout")) is int and
              1 <= request["timeout"] <= 15 and step.get("receipt") == receipt_pin and step.get("worker") == worker_pin,
              "receiver nil step/identity/count/provenance differs")
        operations = request["request"]["operations"]
        _need(receipt.get("format") == B.RUNTIME and receipt.get("request") == request["request"] == worker_request.get("request") and
              receipt.get("request_sha256") == worker.get("request_sha256") == G.R.digest(request["request"]) and
              receipt.get("bundle") == state["bundle"] and receipt.get("target") == worker_request.get("target") == state["target"] and
              receipt.get("expected_identity") == receipt.get("identity") == receipt.get("after_identity") ==
              worker_request.get("identity") == result["identity"] and
              receipt.get("adapter") == worker_request.get("adapter") == result["adapter"] and receipt.get("result") == worker and
              receipt.get("status") == "observed" and receipt.get("error") is None and receipt.get("resource_review_required") is False and
              worker.get("format") == B.WORKER and worker.get("status") == "completed" and worker.get("error") is None and
              worker.get("attached") is True and worker.get("handles_expired") is True and worker.get("native_state_uncertain") is False and
              worker.get("cleanup") == {"script_unloaded": True, "detached": True} and worker.get("cleanup_errors") == [] and
              type(worker.get("native_calls")) is int and worker["native_calls"] == expected_calls and
              worker.get("invocations") == worker.get("helper_load_attempts") == 0,
              "receiver nil raw worker/request/process/cleanup differs")
        rows = worker.get("operations")
        _need(type(rows) is list and len(rows) == len(operations) and all(
              row.get("index") == i and row.get("op") == op["op"] and row.get("status") == "ok"
              for i, (row, op) in enumerate(zip(rows, operations))), "receiver nil raw operations differ")
        raw.append((request, rows, worker_pin))
    _need(ordinals == list(range(ordinals[0], ordinals[0] + len(ordinals))), "receiver nil G steps are not consecutive")
    _need(raw[0][0]["op"] == "discover" and raw[0][0]["request"]["operations"] == [
          {"op": "classes", "query": "NSApplication", "owner": "all", "limit": 128}], "receiver nil root query differs")
    classes = raw[0][1][0]
    matches = [row for row in classes.get("rows", []) if row.get("name") == "NSApplication"]
    _need(classes.get("truncated") is False and len(matches) == 1 and matches[0].get("origin") == "module_backed" and
          type(matches[0].get("module")) is str, "receiver nil root lacks measured metadata")
    measured, previous, observations = [], [], []
    for hop, selector in enumerate(["sharedApplication", ROUTES[result["route"]], "windowController"][:depth + 1]):
        meta_request, meta_rows, meta_pin = raw[1 + 2 * hop]
        read_request, read_rows, read_pin = raw[2 + 2 * hop]
        klass = "NSApplication" if hop == 0 else previous[-1]
        _need(meta_request["op"] == "discover" and meta_request["request"]["operations"] == [
              {"op": "methods", "class": klass, "query": selector, "own": False, "limit": 128}],
              "receiver nil method query differs")
        methods = meta_rows[0]
        choices = [(i, row) for i, row in enumerate(methods.get("rows", []))
                   if row.get("selector") == selector and row.get("dispatch") == ("+" if hop == 0 else "-")]
        _need(methods.get("truncated") is False and len(choices) == 1, "receiver nil method metadata is ambiguous")
        offset, method = choices[0]
        _need(method.get("class") == klass and method.get("origin") == "module_backed" and
              type(method.get("module")) is str and type(method.get("implementation_offset")) is str,
              "receiver nil getter lacks attribution")
        B._signature(selector, method.get("signature"), [], "object")
        measured.append(copy.deepcopy(method))
        operations = [{"op": "read", "receiver": {"class": "NSApplication", "kind": "class"} if i == 0 else {"alias": f"receiver{i-1}"},
                       "expected_class": item["class"], "fields": [{"name": "receiver", "kind": "getter", "type": "object",
                       "selector": item["selector"], "signature": item["signature"], "alias": f"receiver{i}"}]}
                      for i, item in enumerate(measured)]
        _need(read_request["op"] == "runtime" and read_request["request"]["operations"] == operations,
              "receiver nil prefix request differs")
        returned, nil = _returned(read_rows, measured, previous)
        _need(nil is (hop == depth), "receiver nil is not the declared final hop")
        if nil:
            _need(unavailable == _nil(hop, measured, meta_pin, read_pin, offset, raw[0][2]),
                  "receiver nil citation or measured terminal getter differs")
        else:
            observations.append(_edge(hop, measured, returned[-1], meta_pin, read_pin, offset, raw[0][2]))
            previous = returned
    _need(result["observations"] == observations, "receiver nil successful prefix differs from raw observations")


def _validate_class_inventory(result, state, root, index):
    """Re-derive optional metadata from the actual zero-call G transaction."""
    queries = _class_queries(result.get("class_inventory_queries"), state["task"])
    _need(queries is not None and type(result.get("class_inventory")) is dict and
          index.get("class_inventory") == result["class_inventory"] and
          result.get("routine") == index.get("routine") == ID and
          result.get("route") == index.get("route") and result.get("route") in ROUTES and
          index.get("provenance") == "harness_scaffold_observations_not_model_discoveries_or_task_proof",
          "class inventory selection/index differs")
    route_steps = MAX_STEPS if result["status"] == "observed" else 3 + 2 * result["unavailable"]["hop"]
    calls = MAX_NATIVE_CALLS if result["status"] == "observed" else (
        result["unavailable"]["hop"] + 1) * (result["unavailable"]["hop"] + 2) // 2
    timeout = result.get("bounds", {}).get("timeout_seconds")
    started, finished = result.get("started_monotonic"), result.get("finished_monotonic")
    _need(type(timeout) is int and 1 <= timeout <= MAX_SECONDS and result.get("bounds") == {
          "steps": MAX_STEPS + 1, "native_calls": MAX_NATIVE_CALLS, "timeout_seconds": timeout,
          "evidence_bytes": EVIDENCE_BYTES, "free_headroom_bytes": HEADROOM_BYTES, "cleanup_seconds": CLEANUP_SECONDS} and
          all(type(value) in {int, float} and math.isfinite(value) for value in (started, finished)) and
          0 <= finished - started <= timeout and result.get("g_step_dispatches") == len(result["steps"]) == route_steps + 1 and
          result.get("native_calls") == result.get("known_native_calls") == calls and
          result.get("model_calls") == result.get("task_invocations") == 0 and
          result.get("retry") is False and result.get("proof_created") is False and result.get("error") is None and
          result.get("final_review_error") is None and type(result.get("evidence_bytes")) is int and
          0 <= result["evidence_bytes"] <= EVIDENCE_BYTES and result.get("source") == "caller_selected_harness_scaffold",
          "class inventory time/storage/counter disposition differs")
    expected_sources = [_pin(Path(module.__file__).resolve()) for module in (G, B, V)]
    expected_sources += [_pin(Path(__file__).resolve()), _pin(Path(B.__file__).with_name("rea_runtime_agent.js").resolve())]
    _need(result["sources"] == expected_sources, "class inventory source closure differs")
    step = result["steps"][-1]
    directory = step.get("directory", "")
    _need(type(directory) is str and len(directory) == 5 and directory.startswith("s") and directory[1:].isdigit(),
          "class inventory G step directory differs")
    ordinal = int(directory[1:])
    _need(0 <= ordinal < len(state["steps"]) and ordinal > 0 and
          result["steps"][-2]["directory"] == f"s{ordinal - 1:04d}",
          "class inventory must immediately follow the measured receiver route")
    base = root / directory
    def registered(file):
        name = file.relative_to(root).as_posix()
        pin = state["artifacts"].get(name)
        _need(type(pin) is dict and pin.get("file") == str(file), "class inventory source is not G registered")
        return _read(pin), {"artifact": name, **copy.deepcopy(pin)}
    request, _ = registered(base / "request.json")
    feedback, _ = registered(base / "feedback.json")
    receipt, receipt_pin = registered(base / "runtime/runtime.json")
    worker, worker_pin = registered(base / "runtime/worker-output.json")
    worker_request, _ = registered(base / "runtime/worker-request.json")
    g = state["steps"][ordinal]
    operations = [{"op": "classes", "owner": "app", "query": term, "limit": CLASS_QUERY_LIMIT} for term in queries]
    _need(step.get("status") == "completed" and step.get("provenance") == "harness_scaffold" and
          step.get("expected_native_calls") == step.get("native_calls") == 0 and
          step.get("request") == request == g.get("request") and feedback == g and
          g.get("step") == ordinal and g.get("status") == "completed" and
          g.get("expected_runtime_identity") == result["identity"] and
          request.get("op") == "discover" and request.get("provider") == "native" and
          request.get("request") == {"operations": operations} and type(request.get("timeout")) is int and
          1 <= request["timeout"] <= 15 and step.get("receipt") == receipt_pin and step.get("worker") == worker_pin,
          "class inventory request/identity/accounting differs")
    _need(receipt.get("format") == B.RUNTIME and receipt.get("request") == worker_request.get("request") == request["request"] and
          receipt.get("request_sha256") == worker.get("request_sha256") == G.R.digest(request["request"]) and
          receipt.get("bundle") == state["bundle"] and receipt.get("target") == worker_request.get("target") == state["target"] and
          receipt.get("expected_identity") == receipt.get("identity") == receipt.get("after_identity") ==
          worker_request.get("identity") == result["identity"] and
          receipt.get("adapter") == worker_request.get("adapter") == result["adapter"] and receipt.get("result") == worker and
          receipt.get("status") == "observed" and receipt.get("error") is None and receipt.get("resource_review_required") is False and
          worker.get("format") == B.WORKER and worker.get("status") == "completed" and worker.get("error") is None and
          worker.get("attached") is True and worker.get("handles_expired") is True and worker.get("native_state_uncertain") is False and
          worker.get("cleanup") == {"script_unloaded": True, "detached": True} and worker.get("cleanup_errors") == [] and
          type(worker.get("native_calls")) is int and worker["native_calls"] == 0 and
          worker.get("invocations") == worker.get("helper_load_attempts") == 0,
          "class inventory worker/request/process/cleanup differs")
    expected = _class_inventory(queries, state["task"], _frozen(state), directory, worker.get("operations"),
                                worker_pin, state["bundle"], result["identity"]["executable"])
    _need(result["class_inventory"] == expected, "class inventory index differs from its actual raw class rows")
