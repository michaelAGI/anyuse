"""Closed private phased-permit data. Imports perform no IO or runtime work.

The inherited channel owns authority; hashes and this pure validator do not.
Actual execution/identity/lifetime/observation checks remain with B and E.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import struct

PROTOCOL = "anyuse.rea-phased-permit/1"
BINDING = "anyuse.rea-phased-permit-binding/1"
FRAME = "anyuse.rea-phased-permit-frame/1"
PARENT = "anyuse.rea-phased-permit-parent/1"
WORKER = "anyuse.rea-phased-permit-worker/1"
AGENT = "anyuse.rea-phased-permit-agent/1"
CHECKPOINT = "anyuse.rea-phased-permit-checkpoint/1"
SETUP = "anyuse.rea-phased-setup-return/1"
CAPABILITIES = frozenset({"ax_focused_readonly/1", "ax_owned_visible_readonly/1"})
MAX_FRAME, MAX_FRAMES, MAX_EVENTS, MAX_ERRORS = 8192, 16, 32, 8
MAX_SAFE = 2**53 - 1
SHA = re.compile(r"[0-9a-f]{64}\Z")
P_EVENTS = frozenset({"prepared", "worker_started", "ready_received", "setup_complete", "before_complete",
                      "need_received", "decision_intent", "decision_sent", "closed", "error"})
W_EVENTS = frozenset({"started", "ready_sent", "need_sent", "decision_received", "post_started",
                      "post_returned", "parent_eof", "abort_posted", "stopped", "error"})
J_EVENTS = frozenset({"prepared", "waiting", "permit_accepted", "permit_consumed", "native_entry",
                      "native_returned", "denied", "expired", "late_abort", "closed", "error"})
_BINDING_FIELDS = {"format", "run_nonce", "request_sha256", "identity_sha256", "target_sha256", "adapter_sha256",
    "adapter_source_sha256", "adapter_manifest_sha256", "native_resources_sha256", "compiler_provenance_sha256",
    "observer_sha256", "observer_capability", "pid", "executable", "deadline_ms"}


def need(value, message):
    if not value: raise ValueError(message)


def keys(value, fields):
    need(type(value) is dict and set(value) == set(fields), "closed phased permit schema required")


def integer(value, minimum=0, maximum=MAX_SAFE):
    need(type(value) is int and minimum <= value <= maximum, "bounded exact phased permit integer required")
    return value


def text(value, maximum=2048, *, empty=False):
    need(type(value) is str and (empty or bool(value)) and len(value) <= maximum and "\0" not in value,
         "bounded phased permit text required")
    return value


def moment(value):
    need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300800000,
         "finite local phased permit time required")
    return value


def same(left, right):
    if type(left) is not type(right): return False
    if type(left) is dict: return set(left) == set(right) and all(same(left[key], right[key]) for key in left)
    if type(left) is list: return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def canonical(value):
    try: return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error: raise ValueError("plain finite permit JSON required") from error


def digest(value):
    try: return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
    except UnicodeError as error: raise ValueError("literal UTF-8 permit JSON required") from error


def sha(value):
    need(type(value) is str and SHA.fullmatch(value), "exact phased permit digest/nonce required")
    return value


def path(value):
    text(value, 4096)
    need(value.startswith("/") and not value.startswith("//") and str(PurePosixPath(value)) == value and
         ".." not in PurePosixPath(value).parts, "canonical absolute permit evidence path required")
    return value


def pin(value):
    keys(value, {"file", "sha256", "bytes"})
    path(value["file"]); sha(value["sha256"]); integer(value["bytes"], 1, 129 * 1024**2)
    return copy.deepcopy(value)


def error(value):
    keys(value, {"type", "message"}); text(value["type"], 128); text(value["message"], empty=True)


def selection(value):
    keys(value, {"generation", "owner_thread_id"})
    integer(value["generation"], 1, 4096); integer(value["owner_thread_id"], 1)
    return copy.deepcopy(value)


def validate_binding(value):
    keys(value, _BINDING_FIELDS)
    need(value["format"] == BINDING, "current phased permit binding required")
    for name in _BINDING_FIELDS - {"format", "observer_capability", "pid", "executable", "deadline_ms"}: sha(value[name])
    need(type(value["observer_capability"]) is str and value["observer_capability"] in CAPABILITIES,
         "fixed phased observer capability required")
    integer(value["pid"], 1, 2**31 - 1); path(value["executable"]); integer(value["deadline_ms"], 1)
    return copy.deepcopy(value)


def observer_descriptor(config, *, observer_capability, observer_selector, observer_spec):
    return {"capability": observer_capability, "target": copy.deepcopy(config["target"]),
            "selector": copy.deepcopy(observer_selector), "spec": copy.deepcopy(observer_spec)}


def make_binding(config, *, run_nonce, observer_capability, observer_selector, observer_spec):
    adapter, identity = config["adapter"], config["identity"]
    need(type(adapter) is dict and type(adapter.get("build")) is dict and
         adapter["build"].get("interlock_protocol") == PROTOCOL,
         "compiled fixed adapter lacks the required pre-actuation protocol")
    value = {"format": BINDING, "run_nonce": run_nonce, "request_sha256": digest(config["request"]),
        "identity_sha256": digest(identity), "target_sha256": digest(config["target"]),
        "adapter_sha256": adapter["sha256"], "adapter_source_sha256": adapter["source_sha256"],
        "adapter_manifest_sha256": adapter["manifest"]["sha256"],
        "native_resources_sha256": digest(config["native_resources"]),
        "compiler_provenance_sha256": digest(config["compiler_provenance"]),
        "observer_sha256": digest(observer_descriptor(config, observer_capability=observer_capability,
            observer_selector=observer_selector, observer_spec=observer_spec)),
        "observer_capability": observer_capability, "pid": identity["pid"], "executable": identity["executable"],
        "deadline_ms": config["deadline_ms"]}
    return validate_binding(value)


def validate_config(value):
    keys(value, {"format", "binding", "observer_selector", "observer_spec", "control_fd", "parent_pid"})
    need(value["format"] == PROTOCOL and type(value["observer_selector"]) is dict and
         type(value["observer_spec"]) is dict, "fixed private phased permit configuration required")
    validate_binding(value["binding"]); integer(value["control_fd"], 3, 2**31 - 1)
    integer(value["parent_pid"], 1, 2**31 - 1)
    return copy.deepcopy(value)


def validate_setup(value):
    """Trusted producer's explicit successful-return marker, not authentication."""
    keys(value, {"format", "status", "result"})
    need(value["format"] == SETUP and value["status"] == "returned", "explicit successful trusted setup return required")
    try: raw = canonical(value).encode("utf-8")
    except UnicodeError as failure: raise ValueError("literal UTF-8 setup return required") from failure
    need(len(raw) <= 64 * 1024, "trusted setup return exceeds its phase bound")
    return copy.deepcopy(value)


def validate_frame(value):
    need(type(value) is dict and type(value.get("kind")) is str and value["kind"] in {"ready", "need", "decision"},
         "literal private phased permit frame kind required")
    base = {"format", "kind", "binding", "worker_pid", "worker_nonce"}
    extra = ({"selection"} if value["kind"] == "need" else
             {"selection", "decision", "ordinal", "setup", "before", "checkpoint", "reason"} if value["kind"] == "decision" else set())
    keys(value, base | extra)
    need(value["format"] == FRAME, "current private phased permit frame required")
    validate_binding(value["binding"]); integer(value["worker_pid"], 1, 2**31 - 1); sha(value["worker_nonce"])
    if value["kind"] == "need": selection(value["selection"])
    if value["kind"] == "decision":
        integer(value["ordinal"], 1, 1)
        need(type(value["decision"]) is str and value["decision"] in {"allow", "deny"}, "literal permit decision required")
        if value["selection"] is not None: selection(value["selection"])
        for name in ("setup", "before", "checkpoint"):
            if value[name] is not None: pin(value[name])
        if value["decision"] == "allow":
            need(all(value[name] is not None for name in ("selection", "setup", "before", "checkpoint")) and
                 value["reason"] is None, "allow needs exact completed phase and checkpoint pins")
        else: text(value["reason"], 256)
    try: raw = canonical(value).encode("utf-8")
    except UnicodeError as failure: raise ValueError("literal UTF-8 permit frame required") from failure
    need(1 <= len(raw) <= MAX_FRAME, "phased permit frame exceeds its byte bound")
    return copy.deepcopy(value)


def encode_frame(value):
    raw = canonical(validate_frame(value)).encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def decode_frames(raw):
    need(type(raw) is bytes and len(raw) <= (MAX_FRAME + 4) * MAX_FRAMES, "bounded raw permit channel buffer required")
    def pairs(items):
        result = {}
        for name, value in items:
            need(name not in result, "duplicate permit JSON key")
            result[name] = value
        return result
    def constant(_value): raise ValueError("nonfinite permit JSON")
    frames, cursor = [], 0
    while len(raw) - cursor >= 4:
        count = struct.unpack(">I", raw[cursor:cursor + 4])[0]
        need(1 <= count <= MAX_FRAME, "permit channel frame length exceeds its bound")
        if len(raw) - cursor - 4 < count: break
        frame = json.loads(raw[cursor + 4:cursor + 4 + count].decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        frames.append(validate_frame(frame)); cursor += 4 + count
        need(len(frames) <= MAX_FRAMES, "permit channel frame count exceeds its bound")
    return frames, raw[cursor:]


def events(value, allowed):
    need(type(value) is list and len(value) <= MAX_EVENTS, "bounded permit event ledger required")
    previous, seen = None, set()
    for index, row in enumerate(value, 1):
        keys(row, {"seq", "kind", "at"}); integer(row["seq"], index, index)
        need(type(row["kind"]) is str and row["kind"] in allowed, "unknown permit journal event")
        at = moment(row["at"])
        need(previous is None or previous <= at, "permit journal local time order changed")
        need(row["kind"] not in seen or row["kind"] in {"error", "late_abort"}, "repeated one-use permit journal event")
        previous = at; seen.add(row["kind"])


def _own_decision(journal):
    decision = journal["decision"]
    if decision is not None:
        validate_frame(decision)
        need(decision["kind"] == "decision" and same(decision["binding"], journal["binding"]) and
             decision["worker_pid"] == journal["worker_pid"] and decision["worker_nonce"] == journal["worker_nonce"] and
             (same(decision["selection"], journal["selection"]) or
              decision["decision"] == "deny" and decision["selection"] is None),
             "journal adopted another transaction decision")


def validate_parent(value):
    keys(value, {"format", "binding", "worker_pid", "worker_nonce", "selection", "decision", "sent", "events", "error"})
    need(value["format"] == PARENT and type(value["sent"]) is bool, "current actual parent permit journal required")
    validate_binding(value["binding"])
    if value["worker_pid"] is not None: integer(value["worker_pid"], 1, 2**31 - 1)
    if value["worker_nonce"] is not None: sha(value["worker_nonce"])
    if value["selection"] is not None: selection(value["selection"])
    if value["error"] is not None: error(value["error"])
    need(not value["sent"] or value["decision"] is not None, "parent send has no actual decision")
    _own_decision(value); events(value["events"], P_EVENTS)
    return copy.deepcopy(value)


def validate_worker(value):
    keys(value, {"format", "binding", "worker_pid", "worker_nonce", "selection", "decision", "post_started",
                  "post_returned", "parent_eof", "thread_started", "thread_stopped", "events", "errors"})
    need(value["format"] == WORKER, "current actual worker permit journal required")
    validate_binding(value["binding"]); integer(value["worker_pid"], 1, 2**31 - 1); sha(value["worker_nonce"])
    if value["selection"] is not None: selection(value["selection"])
    need(all(type(value[name]) is bool for name in ("post_started", "post_returned", "parent_eof", "thread_started", "thread_stopped")),
         "actual typed permit worker dispositions required")
    need(not value["post_returned"] or value["post_started"] and value["decision"] is not None,
         "worker post cannot invent an unissued parent decision")
    need(not value["thread_stopped"] or value["thread_started"], "worker control thread cannot stop before starting")
    need(type(value["errors"]) is list and len(value["errors"]) <= MAX_ERRORS, "bounded worker permit errors required")
    for row in value["errors"]: error(row)
    _own_decision(value); events(value["events"], W_EVENTS)
    return copy.deepcopy(value)


def validate_agent(value):
    keys(value, {"format", "binding_sha256", "worker_nonce", "selection", "decision_sha256", "state",
                  "permit_consumed", "native_entry", "closed", "events", "error"})
    need(value["format"] == AGENT, "current actual fixed-agent permit journal required")
    sha(value["binding_sha256"]); sha(value["worker_nonce"])
    if value["selection"] is not None: selection(value["selection"])
    if value["decision_sha256"] is not None: sha(value["decision_sha256"])
    need(type(value["state"]) is str and value["state"] in {"prepared", "waiting", "allowed", "consumed", "denied", "expired", "failed"} and
         all(type(value[name]) is bool for name in ("permit_consumed", "native_entry", "closed")),
         "actual fixed-agent permit disposition required")
    need(not value["native_entry"] or value["permit_consumed"], "native entry cannot precede positive permit consumption")
    need(not value["permit_consumed"] or value["selection"] is not None and value["decision_sha256"] is not None,
         "consumed permit lost its actual decision/selection")
    if value["error"] is not None: error(value["error"])
    events(value["events"], J_EVENTS)
    return copy.deepcopy(value)


def validate_checkpoint(value):
    keys(value, {"format", "binding", "worker_pid", "worker_nonce", "selection", "setup", "before",
        "setup_finished_monotonic", "before_finished_monotonic", "checked_monotonic", "deadline_monotonic", "current"})
    need(value["format"] == CHECKPOINT, "actual immutable pre-actuation checkpoint required")
    binding = validate_binding(value["binding"])
    integer(value["worker_pid"], 1, 2**31 - 1); sha(value["worker_nonce"]); selection(value["selection"])
    pin(value["setup"]); pin(value["before"])
    times = [moment(value[name]) for name in ("setup_finished_monotonic", "before_finished_monotonic", "checked_monotonic", "deadline_monotonic")]
    need(times[0] <= times[1] <= times[2] < times[3], "pre-actuation checkpoint does not follow completed phases within deadline")
    current = value["current"]
    keys(current, {"identity", "target", "adapter", "native_resources", "compiler_provenance"})
    identity, adapter = current["identity"], current["adapter"]
    need(type(identity) is dict and type(adapter) is dict and type(adapter.get("manifest")) is dict and
         type(adapter.get("build")) is dict and adapter["build"].get("interlock_protocol") == PROTOCOL,
         "actual current identity and capable compiled adapter required")
    need(identity.get("pid") == binding["pid"] and identity.get("executable") == binding["executable"] and
         digest(identity) == binding["identity_sha256"] and digest(current["target"]) == binding["target_sha256"] and
         adapter.get("sha256") == binding["adapter_sha256"] and adapter.get("source_sha256") == binding["adapter_source_sha256"] and
         adapter["manifest"].get("sha256") == binding["adapter_manifest_sha256"] and
         digest(current["native_resources"]) == binding["native_resources_sha256"] and
         digest(current["compiler_provenance"]) == binding["compiler_provenance_sha256"],
         "current checkpoint differs from frozen process/source/resources")
    return copy.deepcopy(value)


def _ordered(journal, kinds):
    positions = {row["kind"]: index for index, row in enumerate(journal["events"])}
    need(all(kind in positions for kind in kinds) and
         all(positions[left] < positions[right] for left, right in zip(kinds, kinds[1:])),
         "completed permit journal lost its actual causal order")


def validate_completed(parent, worker, agent, checkpoint, *, binding, setup_pin, before_pin, checkpoint_pin,
                       native_calls, invocations):
    """Pure evidence validation; callers reopen/pin all actual canonical files.

    This never authorizes a callback, acquires an object or proves a task.
    """
    binding = validate_binding(binding)
    parent, worker, agent, checkpoint = (validate_parent(parent), validate_worker(worker),
                                         validate_agent(agent), validate_checkpoint(checkpoint))
    for value in (parent, worker, checkpoint): need(same(value["binding"], binding), "permit evidence changed its frozen binding")
    for value in (worker, checkpoint):
        need(value["worker_pid"] == parent["worker_pid"] and value["worker_nonce"] == parent["worker_nonce"] and
             same(value["selection"], parent["selection"]), "permit evidence adopted another worker/generation")
    decision = parent["decision"]
    need(decision is not None and decision["decision"] == "allow" and parent["sent"] is True and parent["error"] is None,
         "completed native call needs the actual positive parent commit")
    need(same(decision["setup"], pin(setup_pin)) and same(decision["before"], pin(before_pin)) and
         same(decision["checkpoint"], pin(checkpoint_pin)) and same(checkpoint["setup"], setup_pin) and
         same(checkpoint["before"], before_pin), "permit does not bind the exact retained successful phases/checkpoint")
    need(same(worker["decision"], decision) and worker["thread_started"] is True and worker["thread_stopped"] is True and
         worker["post_started"] is True and worker["post_returned"] is True and worker["parent_eof"] is False and
         worker["errors"] == [], "completed permit needs actual clean owned forwarding and control-thread exit")
    need(agent["binding_sha256"] == digest(binding) and agent["worker_nonce"] == parent["worker_nonce"] and
         same(agent["selection"], parent["selection"]) and agent["decision_sha256"] == digest(decision) and
         agent["state"] == "consumed" and agent["permit_consumed"] is True and agent["native_entry"] is True and
         agent["closed"] is True and agent["error"] is None,
         "completed native call lacks one actual fixed-agent permit consumption/entry")
    integer(native_calls, 1, 1); integer(invocations, 1, 1)
    _ordered(parent, ["prepared", "worker_started", "ready_received", "setup_complete", "before_complete", "decision_intent", "decision_sent", "closed"])
    _ordered(parent, ["ready_received", "need_received", "decision_intent"])
    times = {row["kind"]: row["at"] for row in parent["events"]}
    need(checkpoint["setup_finished_monotonic"] <= times["setup_complete"] <= checkpoint["checked_monotonic"] and
         checkpoint["before_finished_monotonic"] <= times["before_complete"] <= checkpoint["checked_monotonic"] and
         checkpoint["checked_monotonic"] <= times["decision_intent"] <= times["decision_sent"] < checkpoint["deadline_monotonic"],
         "parent permit events do not follow its actual completed phases/checkpoint before deadline")
    _ordered(worker, ["started", "ready_sent", "need_sent", "decision_received", "post_started", "post_returned", "stopped"])
    _ordered(agent, ["prepared", "waiting", "permit_accepted", "permit_consumed", "native_entry", "native_returned", "closed"])
    need(not any(row["kind"] in {"error"} for row in parent["events"]) and
         not any(row["kind"] in {"error", "parent_eof", "abort_posted"} for row in worker["events"]) and
         not any(row["kind"] in {"error", "denied", "expired", "late_abort"} for row in agent["events"]),
         "a failed/denied/aborted permit cannot become completed evidence")
    return {"format": PROTOCOL, "observer_capability": binding["observer_capability"],
            "parent_committed": True, "agent_consumed": True, "native_calls": 1, "invocations": 1,
            "evidence_only": True, "proof_created": False}
