"""Read one retained fixed-state trial; no execution, campaign or public proof.

The authorized session supplies independently frozen full references and one
immutable actor. Local receipts are measurements, not signatures. Historical
validation never looks up, adopts, attaches to or requires the old live process.
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

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_native_path as P
from . import rea_ax_state as S
from . import rea_state_reader as SR
from . import rea_runtime_permit as RP
from . import rea_state_task as T
from . import rea_harness as H

REPORT = "anyuse.rea-fixed-state-trial-measurement/1"
REPORT2 = "anyuse.rea-fixed-state-trial-measurement/2"
REPORT3 = "anyuse.rea-fixed-state-trial-measurement/3"
MAX_FILES, MAX_TOTAL_BYTES = 256, 2 * 1024**3
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed retained state-trial schema required")


def _same(left, right):
    return P.same_data(left, right)


def _moment(value):
    _need(type(value) in (int, float) and math.isfinite(value) and value >= 0, "finite monotonic trial time required")
    return value


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _need(key not in result, "duplicate retained evidence JSON key")
            result[key] = value
        return result
    def invalid(_): raise ValueError("nonfinite retained evidence JSON")
    def real(token):
        value = float(token)
        _need(math.isfinite(value), "nonfinite retained evidence JSON numeric literal")
        return value
    try: return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid, parse_float=real)
    except (UnicodeError, RecursionError) as error: raise ValueError("bounded UTF8 evidence JSON required") from error


class _Reader:
    """Canonical pinned files, role separation and bounded streaming hashes."""

    def __init__(self):
        self.files, self.inodes, self.total = {}, {}, 0

    def read(self, pin, domain, *, maximum=T.MAX_EVIDENCE_BYTES, retain=True):
        _keys(pin, {"file", "sha256", "bytes"})
        name, sha, size = pin["file"], pin["sha256"], pin["bytes"]
        _need(type(name) is str and 0 < len(name.encode()) <= 4096 and
              all(32 <= ord(char) != 127 for char in name) and name.startswith("/") and not name.startswith("//") and
              str(Path(name)) == name and ".." not in Path(name).parts and
              type(sha) is str and SHA.fullmatch(sha) and type(size) is int and 0 <= size <= maximum,
              "exact bounded canonical evidence file pin required")
        path = Path(name)
        _need(path.resolve(strict=True) == path, "evidence path has a symlink or noncanonical component")
        before = path.lstat()
        fields = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_uid, row.st_gid, row.st_nlink,
                              row.st_size, row.st_mtime_ns, row.st_ctime_ns)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and not before.st_mode & 0o022 and before.st_size == size,
              "evidence must be an unaliased regular file with its exact size")
        inode, previous = (before.st_dev, before.st_ino), self.files.get(name)
        _need(inode not in self.inodes or self.inodes[inode] == name, "two evidence paths alias the same inode")
        _need(previous is None or previous["domain"] == domain and _same(previous["pin"], pin),
              "reference/trial/resource roles overlap or one file has conflicting pins")
        if previous is None:
            _need(len(self.files) < MAX_FILES and self.total + size <= MAX_TOTAL_BYTES, "trial file/aggregate byte budget exceeded")
        digest, chunks = hashlib.sha256(), []
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            _need(fields(os.fstat(stream.fileno())) == fields(before), "evidence inode changed while opening")
            consumed = 0
            while True:
                chunk = stream.read(min(1024**2, size - consumed + 1))
                if not chunk: break
                consumed += len(chunk)
                _need(consumed <= size, "evidence grew while hashing")
                digest.update(chunk)
                if retain: chunks.append(chunk)
            _need(consumed == size and fields(os.fstat(stream.fileno())) == fields(before), "evidence changed while reading")
        _need(fields(path.lstat()) == fields(before) and digest.hexdigest() == sha, "evidence bytes differ from exact retained pin")
        if previous is None: self.total += size
        self.inodes[inode] = name
        self.files[name] = {"pin": copy.deepcopy(pin), "domain": domain, "maximum": maximum}
        return b"".join(chunks) if retain else None

    def json(self, pin, domain="trial", *, maximum=T.MAX_EVIDENCE_BYTES):
        return _json(self.read(pin, domain, maximum=maximum))

    def sha_file(self, name, sha, domain, maximum):
        # Some current producer references bind a hash without repeating size.
        # Their actual size is bounded and then retained in this reader's closure.
        pin = {"file": name, "sha256": sha, "bytes": Path(name).lstat().st_size}
        return self.json(pin, domain, maximum=maximum)

    def recheck(self):
        for row in list(self.files.values()):
            self.read(row["pin"], row["domain"], maximum=row["maximum"], retain=False)


def _artifact_files(reader, runtime, native_pin, *, require_pre_actuation_ack=False):
    folder, result = Path(native_pin["file"]).parent, {}
    rows = runtime.get("artifacts")
    expected = {"worker-request.json", "worker-output.json", "worker.stderr", "trace.jsonl", "worker-cleanup.json"}
    if require_pre_actuation_ack: expected.add("worker-permit.json")
    _need(type(rows) is list and len(rows) == len(expected), "exact current native artifact inventory required")
    for row in rows:
        _keys(row, {"file", "bytes", "sha256", "oversized"})
        name = row["file"]
        _need(type(name) is str and name in expected and name not in result and row["oversized"] is False,
              "native artifact inventory is incomplete or ambiguous")
        pin = {"file": str(folder / name), "sha256": row["sha256"], "bytes": row["bytes"]}
        result[name] = (pin, reader.read(pin, "trial", maximum=R.MAX_JSON_BYTES))
    return result


def _native(reader, actor, request, runtime, worker, context, identity, target, native_pin, worker_pin, *,
            require_pre_actuation_ack=False):
    _need(runtime.get("format") == B.RUNTIME and runtime.get("status") == "executed" and
          runtime.get("evidence_only") is True and runtime.get("verified") is False and runtime.get("proof_created") is False and
          type(runtime.get("model_calls")) is int and runtime["model_calls"] == 0 and runtime.get("actuated") is True and
          runtime.get("native_calls") is True and runtime.get("resource_review_required") is False and runtime.get("error") is None,
          "trial requires a current clean actual native execution receipt")
    _need(_same(runtime.get("target"), target) and _same(runtime.get("request"), request) and
          runtime.get("request_sha256") == R.digest(request) and runtime.get("bundle") == identity["bundle"] and
          runtime.get("receipt") == native_pin["file"], "native receipt differs from immutable actor/target/request")
    for container, keys in ((runtime, ("expected_identity", "identity", "after_identity")),
                            (worker, ("identity", "after_identity"))):
        for key in keys:
            _need(_same(S.validate_identity(container.get(key)), identity), "historical full process identity differs within trial")
    _need(_same(runtime.get("result"), worker), "raw worker output differs from embedded native result")
    _need(_same(runtime.get("transport"), {"kind": "local"}) and
          _same(worker.get("transport"), {"kind": "local"}), "initial state trial consumer requires explicit local transport")
    _need(worker.get("format") == B.WORKER and worker.get("status") == "completed" and
          worker.get("attached") is True and worker.get("handles_expired") is True,
          "trial requires a completed attached current worker with expired handles")
    _need(type(worker.get("pre_actuation_ack")) is dict if require_pre_actuation_ack else
          "pre_actuation_ack" not in worker, "worker acknowledgement differs from the selected phase protocol")
    artifacts = None
    if require_pre_actuation_ack:
        # The primitive rejects unrequested permit summaries. Bind its true
        # branch to the actual pinned worker config; the independent full
        # declaration/binding/current/selection gates remain below.
        artifacts = _artifact_files(reader, runtime, native_pin, require_pre_actuation_ack=True)
        permit_config = _json(artifacts["worker-request.json"][1])
        declaration = RP.validate_config(permit_config["pre_actuation_ack"])
        B._worker_result(worker, R.digest(request), request, {"kind": "local"},
                         expected_permit=declaration["binding"])
    else:
        B._worker_result(worker, R.digest(request), request, {"kind": "local"})
    _need(H._runtime_cleanup_valid(runtime) and _same(worker.get("cleanup"), {"script_unloaded": True, "detached": True}) and
          worker.get("native_state_uncertain") is False and worker.get("error") is None and worker.get("queue") == "main" and
          type(worker.get("native_calls")) is int and worker["native_calls"] == 1 and
          type(worker.get("invocations")) is int and worker["invocations"] == 1 and
          type(worker.get("helper_load_attempts")) is int and worker["helper_load_attempts"] == 0 and
          type(worker.get("callable_resolutions")) is int and worker["callable_resolutions"] == 0 and
          worker.get("helper_residency") == [] and worker.get("preloaded_gadget_remains") is False,
          "native action counters/queue/cleanup or uncertainty differ")
    _need(_same(worker.get("process"), {"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"}),
          "injected native process metadata differs from historical full pin")
    _need(_same(runtime.get("compiler_provenance"), {}) and _same(worker.get("compiler_provenance"), {}),
          "one lifecycle action cannot acquire a compiler/helper provenance path")
    resources, claims = runtime.get("native_resources"), B.native_file_claims(actor)
    _need(type(resources) is dict and set(resources) == set(claims) and
          _same(resources, worker.get("native_resources")) and _same(resources, context["native_resources"]),
          "native transitive resource closure differs")
    for name, sha in claims.items():
        row = resources[name]
        _need(type(row) is dict and row.get("file") == name and row.get("sha256") == sha,
              "native original file pin differs from actor")
        reader.read({key: row[key] for key in ("file", "sha256", "bytes")}, "resource",
                    maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
    def selected(claim):
        pin = resources[claim["file"]]
        body = reader.json({key: pin[key] for key in ("file", "sha256", "bytes")}, "resource", maximum=B.MAX_RESULT_BYTES)
        return B._json_pointer(body, claim["pointer"])
    if actor["kind"] == "native_function":
        _need(_same(selected(actor["signature_evidence"]), B._contract(actor)), "selected original ABI is not exact typed JSON")
    # A Qt operation's exact callback ABI/selection/primary-base admission is
    # its independently reviewed path consumer, not an inferred C++ body ABI.
    for recipe in B.request_native_path_recipes(request):
        for evidence in P.evidence_plan(recipe):
            _need(_same(selected(evidence["claim"]), evidence["contract"]), "selected original path contract is not exact typed JSON")
            for original in evidence["originals"]:
                if original["role"] == "independent_review":
                    reader.json({key: resources[original["file"]][key] for key in ("file", "sha256", "bytes")},
                                "resource", maximum=B.MAX_RESULT_BYTES)
    _need(_same(B.request_resources(request), resources), "native original bytes or selected ABI/layout/lifetime/observer contracts changed")
    row = worker["operations"][0]
    if actor["kind"] == "qt_moc_method":
        # Reuse the one authoritative modern Qt callback/vector/observer gate.
        # This cannot replace the independent complete external state check.
        B.validate_qt_moc_result(request, worker)
        _need(H._native_section_pin_run(row["function"], resources),
              "Qt callback original executable section differs")
    else:
        _need(H._native_function_run(row, actor, 0) and H._native_section_pin_run(row["function"], resources) and
              H._native_pointer_reads_run(row, actor, resources) and H._native_swift_reads_run(row, actor, resources) and
              H._native_callable_run(row, actor, resources) and H._native_compiler_completion_run(row, actor),
              "native function/ABI/original executable section or auxiliary receipt differs")
    _need(all(_same(row.get(key), []) for key in ("out_buffers", "pointer_reads", "swift_field_reads", "callable_entries")) and
          row.get("compiler_completion") is None, "terminal lifecycle action has an undeclared output/auxiliary path")
    if actor["kind"] == "qt_moc_method" or actor["return_type"] == "void":
        _need(row.get("value") is None, "void native action returned an undeclared value")
    else: B._value({"type": actor["return_type"], "value": row.get("value")})
    adapter = runtime.get("adapter")
    _need(type(adapter) is dict and _same(adapter.get("transport"), {"kind": "local"}) and
          _same(adapter, context["adapter"]), "fixed adapter identity differs within trial")
    for pin, maximum in (({key: adapter[key] for key in ("file", "sha256", "bytes")}, B.MAX_AGENT_BYTES),
                         (adapter["manifest"], B.MAX_REQUEST_BYTES)):
        reader.read(pin, "resource", maximum=maximum, retain=False)
    reader.json(adapter["manifest"], "resource", maximum=B.MAX_REQUEST_BYTES)
    current_adapter = B._adapter(adapter["file"])
    _need(all(_same(adapter.get(key), value) for key, value in current_adapter.items()), "fixed compiled adapter/source/build manifest changed")
    if require_pre_actuation_ack:
        _need(type(adapter.get("build")) is dict and
              adapter["build"].get("interlock_protocol") == RP.PROTOCOL,
              "actual compiled adapter does not declare the required private acknowledgement protocol")
    source = Path(B.__file__).with_name("rea_runtime_agent.js").resolve(strict=True)
    reader.read({"file": str(source),
                 "sha256": adapter["source_sha256"], "bytes": source.stat().st_size},
                "resource", maximum=B.MAX_AGENT_BYTES, retain=False)
    disposition = runtime.get("worker")
    _keys(disposition, {"returncode", "timed_out", "elapsed_s", "output", "sha256", "stderr"})
    _need(type(disposition["returncode"]) is int and disposition["returncode"] == 0 and disposition["timed_out"] is False and
          disposition["output"] == "worker-output.json" and disposition["stderr"] == "worker.stderr" and
          disposition["sha256"] == worker_pin["sha256"] and
          worker_pin["file"] == str(Path(native_pin["file"]).parent / "worker-output.json"), "native worker file/disposition differs")
    _moment(disposition["elapsed_s"])
    if artifacts is None:
        artifacts = _artifact_files(reader, runtime, native_pin)
    _need(_same(artifacts["worker-output.json"][0], worker_pin), "raw worker file pin differs from native artifact inventory")
    config = _json(artifacts["worker-request.json"][1])
    config_fields = {"format", "bundle", "target", "identity", "request", "adapter", "transport", "native_resources",
                     "compiler_provenance", "backend", "deadline_ms", "trace"}
    _keys(config, config_fields | ({"pre_actuation_ack"} if require_pre_actuation_ack else set()))
    _need(config["format"] == B.WORKER and config["bundle"] == identity["bundle"] and
          _same(config["identity"], identity) and _same(config["target"], target) and _same(config["request"], request) and
          _same(config["adapter"], adapter) and _same(config["transport"], {"kind": "local"}) and config["backend"] in (None, "local") and
          _same(config["native_resources"], resources) and _same(config["compiler_provenance"], {}) and
          type(config["deadline_ms"]) is int and config["deadline_ms"] > 0 and
          config["trace"] == artifacts["trace.jsonl"][0]["file"], "actual worker configuration differs from recorded native route")
    journal = _json(artifacts["worker-cleanup.json"][1])
    expected = {"format": B.CLEANUP_JOURNAL, "request_sha256": R.digest(request), "status": "completed", "attached": True,
        "frida_lifecycle": worker["frida_lifecycle"], "cleanup": worker["cleanup"], "cleanup_errors": [], "error": None,
        "native_state_uncertain": False, "native_calls": 1, "invocations": 1, "helper_load_attempts": 0, "completed_operations": 1}
    _need(_same(journal, expected), "actual native cleanup journal differs from completed worker")
    return artifacts


def _acknowledgement(reader, wrapper, context, artifacts, worker, policy, request, identity, target, native_pin, capability):
    """Reopen independent P/W/J/checkpoint evidence; never infer calls from a permit."""
    native_folder = Path(native_pin["file"]).parent
    parent_pin = wrapper["pre_actuation_ack"]
    _need(_same(parent_pin, context["pre_actuation_ack"]) and
          parent_pin["file"] == str(native_folder.parent / "pre-actuation-parent.json"),
          "phase wrapper/context do not name the same separate final parent journal")
    parent = RP.validate_parent(reader.json(parent_pin))
    actual_worker_pin, actual_worker_raw = artifacts["worker-permit.json"]
    _need(actual_worker_pin["file"] == str(native_folder / "worker-permit.json"),
          "worker permission journal escaped its native artifact closure")
    control_worker = RP.validate_worker(_json(actual_worker_raw))
    agent = RP.validate_agent(worker["pre_actuation_ack"])
    config = _json(artifacts["worker-request.json"][1])
    declaration = RP.validate_config(config["pre_actuation_ack"])
    spec = B.request_native_path_recipes(request)[0]["root"]["lifecycle"]
    selector = policy["before"]["contract"]["selector"]
    _need(_same(declaration["observer_selector"], selector) and _same(declaration["observer_spec"], spec),
          "permission descriptor differs from the frozen policy selector or actual lifecycle specification")
    binding = RP.make_binding(config, run_nonce=parent["binding"]["run_nonce"], observer_capability=capability,
                              observer_selector=selector, observer_spec=spec)
    _need(_same(declaration["binding"], binding), "actual worker permission binding differs from its immutable inputs")
    decision = parent["decision"]
    _need(type(decision) is dict and decision.get("decision") == "allow",
          "completed acknowledgement requires the actual parent allow decision")
    checkpoint_pin = decision["checkpoint"]
    _need(type(checkpoint_pin) is dict and
          checkpoint_pin.get("file") == str(native_folder.parent / "pre-actuation-checkpoint.json"),
          "immutable permission checkpoint must be separate from mutable phase context")
    checkpoint = RP.validate_checkpoint(reader.json(checkpoint_pin))
    _need(parent["worker_pid"] == context["owned_worker"]["pid"] and
          _same(checkpoint["current"], {"identity": identity, "target": target, "adapter": config["adapter"],
                                       "native_resources": config["native_resources"],
                                       "compiler_provenance": config["compiler_provenance"]}) and
          checkpoint["setup_finished_monotonic"] == context["phases"]["setup"]["finished_monotonic"] and
          checkpoint["before_finished_monotonic"] == context["phases"]["before"]["finished_monotonic"] and
          checkpoint["deadline_monotonic"] == context["permit_deadline_monotonic"],
          "independent permission checkpoint lost the actual owner/resources or phase boundaries")
    observed = worker["lifecycle_observer"]
    scopes = worker["operations"][0]["native_paths"]
    _need(type(scopes) is list and len(scopes) == 1 and
          _same(parent["selection"], {"generation": observed["selected_generation"],
                                      "owner_thread_id": observed["owner_thread_id"]}) and
          _same(parent["selection"], {"generation": scopes[0]["lifecycle_scope"]["generation"],
                                      "owner_thread_id": scopes[0]["lifecycle_scope"]["owner_thread_id"]}),
          "permission selection differs from the actual admitted native receiver generation/thread")
    return RP.validate_completed(parent, control_worker, agent, checkpoint, binding=binding,
        setup_pin=context["phases"]["setup"]["return"], before_pin=context["phases"]["before"]["return"],
        checkpoint_pin=checkpoint_pin, native_calls=worker["native_calls"], invocations=worker["invocations"])


def _timing(reader, context, identity, target, request, case, wrapper, artifacts, *, observer, capability,
            require_pre_actuation_ack=False):
    owned_observer = capability == SR.ownedcap()
    context_fields = {"format", "evidence_only", "proof_created", "limits", "expected_identity", "request_sha256", "target", "adapter",
                      "native_resources", "worker_anchor_monotonic", "earliest_terminal_monotonic", "pre_terminal_deadline_monotonic",
                      "ready", "phases", "errors", "owned_worker", "native_receipt"}
    _keys(context, context_fields | ({"observer_capability"} if owned_observer or require_pre_actuation_ack else set()) |
          ({"pre_actuation_ack", "runtime_timeout_seconds", "permit_deadline_monotonic"}
           if require_pre_actuation_ack else set()))
    _need(context["format"] == (B.PHASE_CONTEXT3 if require_pre_actuation_ack else
                               B.PHASE_CONTEXT2 if owned_observer else B.PHASE_CONTEXT) and
          (not (owned_observer or require_pre_actuation_ack) or context["observer_capability"] == capability) and
          context["evidence_only"] is True and context["proof_created"] is False and
          context["errors"] == [] and _same(context["expected_identity"], identity) and _same(context["target"], target) and
          context["request_sha256"] == R.digest(request), "historical phase context does not bind this trial")
    limits = B._phase_limits(context["limits"])
    spec = B.request_native_path_recipes(request)[0]["root"]["lifecycle"]
    anchor, earliest, bound = (_moment(context[key]) for key in
                              ("worker_anchor_monotonic", "earliest_terminal_monotonic", "pre_terminal_deadline_monotonic"))
    if require_pre_actuation_ack:
        _need(type(context["runtime_timeout_seconds"]) is int and 1 <= context["runtime_timeout_seconds"] <= 60 and
              _moment(context["permit_deadline_monotonic"]) ==
              anchor + context["runtime_timeout_seconds"] - 3,
              "permission deadline differs from the actual worker timeout and fixed cleanup reserve")
    _need(earliest == anchor + spec["observe_ms"] / 1000 and bound == earliest - limits["margin_seconds"] and anchor < bound,
          "baseline timing differs from the conservative pre-Popen observation-delay bound")
    expected_ready = {"event": "lifecycle-observer-ready", "observe_ms": spec["observe_ms"], "hooks": 1 + len(spec["destructors"])}
    _need(_same(context["ready"], expected_ready), "phase readiness differs from admitted observer")
    raw = artifacts["trace.jsonl"][1]
    _need(raw.endswith(b"\n") and len(raw) <= 128 * 8193, "complete bounded final trace required")
    lines = raw.splitlines()
    _need(len(lines) <= 128 and all(len(line) <= 8192 for line in lines), "native trace row/count budget exceeded")
    events = [_json(line) for line in lines]
    _need(all(type(row) is dict for row in events), "native trace needs object events")
    ready = [index for index, row in enumerate(events) if row.get("event") == "lifecycle-observer-ready"]
    _need(len(ready) == 1 and _same(events[ready[0]], expected_ready), "actual final trace lost exact unique readiness")
    terminal = [[index for index, row in enumerate(events) if row.get("event") == name]
                for name in ("native-invoke-start", "native-invoke-returned", "operation-returned")]
    _need(all(len(indices) == 1 for indices in terminal) and ready[0] < terminal[0][0] < terminal[1][0] < terminal[2][0] and
          not any(row.get("event") in {"native-getter-start", "native-getter-returned", "agent-error"} for row in events),
          "actual native terminal trace is incomplete, repeated or out of order")
    owned = context["owned_worker"]
    _keys(owned, {"pid", "returncode", "reaped", "term_attempted", "kill_attempted", "errors"})
    _need(type(owned["pid"]) is int and 1 <= owned["pid"] <= 2**31 - 1 and owned["pid"] != identity["pid"] and
          type(owned["returncode"]) is int and owned["returncode"] == 0 and owned["reaped"] is True and
          owned["term_attempted"] is False and owned["kill_attempted"] is False and owned["errors"] == [],
          "owned native worker was not naturally reaped with clean disposition")
    _keys(context["phases"], {"setup", "before", "after"})
    rows, settlements = context["phases"], {}
    previous = anchor
    for phase in ("setup", "before", "after"):
        row = rows[phase]
        _keys(row, {"status", "started_monotonic", "deadline_monotonic", "returned_monotonic", "finished_monotonic", "return", "error",
                    "pre_terminal_gate_checked"})
        begin, deadline, returned, end = (_moment(row[key]) for key in
                                         ("started_monotonic", "deadline_monotonic", "returned_monotonic", "finished_monotonic"))
        _need(row["status"] == "returned" and row["error"] is None and previous <= begin <= returned <= end < deadline and
              deadline <= begin + limits[phase + "_seconds"] and
              row["pre_terminal_gate_checked"] is (phase != "after"), "phase order/deadline or completion gate differs")
        if phase != "after": _need(end < bound and deadline <= bound, "setup or entire baseline was not strictly pre-terminal")
        else:
            _need(begin >= earliest and begin >= anchor + wrapper["runtime"]["worker"]["elapsed_s"] - .001,
                  "after observation precedes the admitted terminal or rounded worker completion")
        previous = end
        pin = row["return"]
        if phase == "setup":
            setup = reader.json(pin)  # Opaque actual setup data, never an effect verdict.
            # The producer caps logical JSON BEFORE its pretty file write.
            _need(len(json.dumps(setup, ensure_ascii=False, allow_nan=False).encode()) <= 64 * 1024,
                  "actual setup return exceeds the producer logical JSON byte cap")
            _need(Path(pin["file"]).name == "setup-return.json", "actual setup return file required")
            if require_pre_actuation_ack: RP.validate_setup(setup)
            continue
        _need(_same(pin, case["evidence"][phase + "_settlement"]), "phase return does not name the case's actual settlement")
        settlement = observer.validate_settlement(reader.json(pin))
        _need(settlement["complete"] is True and _same(settlement["target"], target) and
              _same(settlement["expected_identity"], identity) and
              begin <= settlement["started_monotonic"] <= settlement["finished_monotonic"] <= returned and
              settlement["deadline_monotonic"] <= deadline, "phase settlement is incomplete, stale or outside its actual callback")
        settlements[phase] = settlement
    _need(wrapper["phases_complete"] is True, "phase wrapper did not complete all three bounded host phases")
    return settlements


def _validate_trial(policy, case, actor):
    """Validate ONE retained local lifecycle action and whole external state pair.

    This is a historical measurement gate. It does not require the old process
    to remain running, aggregate campaigns, validate restart or publish a proof.
    """
    policy, case, actor = T.validate_policy(policy), T.validate_case(policy, case), B.validate_actor(actor)
    capability, observer = SR.capability(policy), SR.for_policy(policy)
    owned_observer = capability == SR.ownedcap()
    _need(actor.get("kind") in {"native_function", "qt_moc_method"}, "state trial needs one actual typed native or Qt action")
    request = B.invocation_request(actor)
    paths = B.request_native_path_recipes(request)
    _need(len(request["operations"]) == 1 and len(paths) == 1 and P.is_lifecycle_root(paths[0]),
          "state trial needs one immediate constructor-observed native action")
    identity, target = case["deployment"]["identity"], policy["before"]["contract"]["target"]
    names = [case["evidence"][role]["file"] for role in T.REQUIRED_EVIDENCE]
    names += [policy[role]["reference"]["file"] for role in ("before", "after")]
    _need(len(names) == len(set(names)), "case artifacts and independent references cannot overlap")
    reader = _Reader()
    for role in ("before", "after"):
        T.reference_material(policy, role, reader.read(policy[role]["reference"], "reference"))
    wrapper = reader.json(case["evidence"]["execution"])
    require_pre_actuation_ack = wrapper.get("format") == B.PHASED3 if type(wrapper) is dict else False
    wrapper_fields = {"format", "evidence_only", "proof_created", "phases_complete", "runtime", "run_context"}
    _keys(wrapper, wrapper_fields | ({"observer_capability"} if owned_observer or require_pre_actuation_ack else set()) |
          ({"pre_actuation_ack"} if require_pre_actuation_ack else set()))
    _need(wrapper["format"] == (B.PHASED3 if require_pre_actuation_ack else B.PHASED2 if owned_observer else B.PHASED) and
          (not (owned_observer or require_pre_actuation_ack) or wrapper["observer_capability"] == capability) and
          wrapper["evidence_only"] is True and wrapper["proof_created"] is False,
          "actual current phased measurement envelope required")
    context = reader.json(case["evidence"]["run_context"])
    _need(_same(context, wrapper["run_context"]), "raw phase context differs from embedded wrapper")
    native_reference = context["native_receipt"]
    _keys(native_reference, {"file", "sha256", "status", "resource_review_required", "native_calls", "invocations"})
    runtime = reader.sha_file(native_reference["file"], native_reference["sha256"], "trial", T.MAX_EVIDENCE_BYTES)
    native_pin = reader.files[native_reference["file"]]["pin"]
    _need(_same(runtime, wrapper["runtime"]) and native_reference["status"] == runtime.get("status") and
          native_reference["resource_review_required"] is False and type(native_reference["native_calls"]) is int and
          native_reference["native_calls"] == 1 and type(native_reference["invocations"]) is int and native_reference["invocations"] == 1,
          "raw native receipt differs from its actual phased binding")
    worker = reader.json(case["evidence"]["worker"], maximum=B.MAX_RESULT_BYTES)
    artifacts = _native(reader, actor, request, runtime, worker, context, identity, target, native_pin, case["evidence"]["worker"],
                        require_pre_actuation_ack=require_pre_actuation_ack)
    settlements = _timing(reader, context, identity, target, request, case, wrapper, artifacts,
                          observer=observer, capability=capability, require_pre_actuation_ack=require_pre_actuation_ack)
    acknowledgement = (_acknowledgement(reader, wrapper, context, artifacts, worker, policy, request, identity,
                                        target, native_pin, capability) if require_pre_actuation_ack else None)
    owner = None
    for role in ("before", "after"):
        settlement = settlements[role]
        _need(_same(settlement["selector"], policy[role]["contract"]["selector"]), "settlement changed the independently frozen window selector")
        for sample in settlement["samples"]:
            observation = sample["observation"]
            for key in ("before", "after"):
                current = observation["context"].get(key)
                if current is not None:
                    epoch = {name: current[name] for name in ("identity", "kernel", "session")}
                    _need(owner is None or _same(owner, epoch), "historical process/kernel/GUI owner changed within trial")
                    owner = copy.deepcopy(epoch)
        for index in settlement["stable_pair"]:
            _need(observer.matches(settlement["samples"][index]["observation"], policy[role]["contract"]),
                  "FIRST complete stable whole state differs from its independent contract")
    reader.recheck()
    report = {"format": REPORT3 if require_pre_actuation_ack else REPORT2 if owned_observer else REPORT,
        "status": "trial_measurement_validated", "trial_measurement_validated": True,
        "actor_sha256": R.digest(actor), "request_sha256": R.digest(request), "policy_sha256": T.digest(policy),
        "case_sha256": T.digest(case), "case_id": case["id"], "phase": case["phase"], "identity": copy.deepcopy(identity),
        "state_sha256": {role: R.digest(settlements[role]["samples"][-1]["observation"]["state"]) for role in ("before", "after")},
        "native_calls": 1, "invocations": 1, "proof_created": False, "task_proof_created": False,
        "public_proof_created": False, "card_created": False, "aggregation_validated": False,
        "restart_deployment_proven": False, "input_generalization_claimed": False,
        "after_reaped_order_basis": "trusted_fixed_producer_source_order_no_independent_exit_timestamp",
        "files": {name: copy.deepcopy(row["pin"]) for name, row in reader.files.items()}}
    if owned_observer:
        report.update(observer_capability=capability, state_observation_scope="selected_owned_visible_ax_window_tree")
    if require_pre_actuation_ack:
        report.update(observer_capability=capability, pre_actuation_ack_required=True,
                      pre_actuation_ack_validated=True, pre_actuation_ack=acknowledgement)
    return report


def validate_trial(policy, case, actor):
    """VM-only historical measurement validation, with no old-process lookup."""
    R.assert_guest()  # Before files, backend acquisition or callbacks.
    try:
        policy, case, actor = copy.deepcopy((policy, case, actor))
        return _validate_trial(policy, case, actor)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        raise ValueError("malformed retained fixed-state trial evidence") from error
