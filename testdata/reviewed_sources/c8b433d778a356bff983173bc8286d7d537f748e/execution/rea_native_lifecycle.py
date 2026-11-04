"""Pure closed contracts for a same-RPC constructor-observed native root.

This module observes nothing and performs no file/native IO. A successful
receipt binds a private runtime decision; it is never a reusable capability or
automatic source/ABI/lifetime recovery. Incomplete receipts are retained by the
bridge as failure evidence, not accepted by validate_result as proof.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


FORMAT = "anyuse.rea-native-lifecycle-root/1"
CONTRACT = "anyuse.rea-native-lifecycle-observer-contract/1"
OBSERVATION = "anyuse.rea-native-lifecycle-observation/1"
MODE = "main_queue_observed_root"
MAX_EVENTS, MAX_GENERATIONS, MAX_DESTRUCTORS, MAX_RETURN_SITES = 64, 8, 8, 8
MAX_OBSERVE_MS, CLEANUP_RESERVE_MS = 45000, 3000
EVENT_KINDS = frozenset({"armed", "constructor_enter", "constructor_return", "destructor_enter",
    "destructor_leave", "observation_end", "selected", "acquire", "consume", "poison", "stop"})


def _path():
    # P imports this module. Resolve its shared pure schema helpers only after
    # import completion; neither module performs file/native IO.
    if __package__:
        from . import rea_native_path
    else:  # B's fixed-file --worker entry imports its pure siblings directly.
        import rea_native_path
    return rea_native_path


def validate_entry(value: Any) -> dict:
    p = _path()
    p.keys(value, {"module", "offset"})
    p.image(value["module"])
    offset = value["offset"]
    p.require(type(offset) is str and p.OFFSET.fullmatch(offset) is not None and
              int(offset, 16) <= p.MAX_DATA_OFFSET - 4 and int(offset, 16) % 4 == 0,
              "lifecycle entry needs a canonical aligned JavaScript-safe instruction offset")
    return copy.deepcopy(value)


def entries(spec: dict) -> list[dict]:
    """Actual hook entries first, then admitted constructor return PCs."""
    return [copy.deepcopy(spec["constructor"]["entry"]),
            *[copy.deepcopy(item["entry"]) for item in spec["destructors"]],
            *copy.deepcopy(spec["constructor"]["return_sites"])]


def validate_observer(value: Any) -> dict:
    p = _path()
    p.keys(value, {"format", "constructor", "destructors", "owner_thread", "selection", "observe_ms",
                   "event_limit", "generation_limit", "evidence"})
    p.require(value["format"] == FORMAT and value["owner_thread"] == "observed_constructor_on_macos_main" and
              value["selection"] == "sole_live_at_observation_end", "closed constructor-observed root policy required")
    constructor = value["constructor"]
    p.keys(constructor, {"entry", "return_sites", "this_argument", "this_adjustment", "completion"})
    p.require(type(constructor["this_argument"]) is int and constructor["this_argument"] == 0 and
              type(constructor["this_adjustment"]) is int and constructor["this_adjustment"] == 0 and
              constructor["completion"] == "complete_object_normal_return",
              "lifecycle constructor needs admitted normal complete-object return and primary arg0")
    validate_entry(constructor["entry"])
    sites = constructor["return_sites"]
    p.require(type(sites) is list and 1 <= len(sites) <= MAX_RETURN_SITES,
              "finite admitted constructor return sites required")
    site_keys = set()
    for site in sites:
        validate_entry(site)
        key = site["module"]["path"], site["offset"]
        p.require(key not in site_keys, "duplicate lifecycle constructor return site")
        site_keys.add(key)
    destructors = value["destructors"]
    p.require(type(destructors) is list and 1 <= len(destructors) <= MAX_DESTRUCTORS,
              "finite earliest-primary destructor coverage required")
    hook_keys = {(constructor["entry"]["module"]["path"], constructor["entry"]["offset"])}
    for destructor in destructors:
        p.keys(destructor, {"entry", "this_argument", "this_adjustment"})
        validate_entry(destructor["entry"])
        p.require(type(destructor["this_argument"]) is int and destructor["this_argument"] == 0 and
                  type(destructor["this_adjustment"]) is int and destructor["this_adjustment"] == 0,
                  "lifecycle destructor needs an independently admitted unadjusted primary arg0")
        key = destructor["entry"]["module"]["path"], destructor["entry"]["offset"]
        p.require(key not in hook_keys, "conflicting or duplicate lifecycle hook entries")
        hook_keys.add(key)
    p.require(site_keys.isdisjoint(hook_keys), "lifecycle return sites cannot overlap constructor/destructor hook roles")
    p.integer(value["observe_ms"], 100, MAX_OBSERVE_MS)
    p.integer(value["event_limit"], 8, MAX_EVENTS)
    p.integer(value["generation_limit"], 1, MAX_GENERATIONS)
    p.evidence(value["evidence"])
    # Check the whole pure file closure, without recursing through public APIs.
    claims = {}
    for entry in entries(value):
        image = entry["module"]
        prior = claims.setdefault(image["path"], image["sha256"])
        p.require(prior == image["sha256"], "conflicting lifecycle image identities")
    evidence = value["evidence"]
    for file, sha in [(evidence["claim"]["file"], evidence["claim"]["sha256"]),
                      *[(item["file"], item["sha256"]) for item in evidence["originals"]]]:
        prior = claims.setdefault(file, sha)
        p.require(prior == sha, "conflicting lifecycle original/evidence identities")
    return copy.deepcopy(value)


def observer_digest(spec: dict) -> str:
    value = validate_observer(spec)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def observer_contract(spec: dict, root_type: str, layout: dict) -> dict:
    p = _path()
    validate_observer(spec)
    p.name(root_type)
    p.validate_layout(layout)
    p.require(layout["name"] == root_type, "lifecycle observer contract differs from the declared primary type")
    return {"format": CONTRACT, "observer": {key: copy.deepcopy(value) for key, value in spec.items() if key != "evidence"},
            "pointee_type": root_type, "layout": p.layout_contract(layout),
            "construction_completion": "witnessed_complete_object_normal_return",
            "constructor_caller": "exact_admitted_return_site",
            "destruction_coverage": "earliest_primary_entry_independently_established",
            "owner_thread": "constructor_and_use_match_observed_macos_main",
            "invalidation_thread": "main_only_independently_established",
            "hook_policy": "checked_online_original_entries", "external_hook_removal": "independently_excluded",
            "selection": "sole_live_at_observation_end", "scope": "same_rpc_single_immediate_use",
            "pointer_policy": "borrowed_no_escape"}


def image_claims(spec: dict) -> list[dict]:
    validate_observer(spec)
    claims = {entry["module"]["path"]: copy.deepcopy(entry["module"]) for entry in entries(spec)}
    return [claims[path] for path in sorted(claims)]


def file_claims(spec: dict) -> dict[str, str]:
    claims = {image["path"]: image["sha256"] for image in image_claims(spec)}
    evidence = spec["evidence"]
    claims[evidence["claim"]["file"]] = evidence["claim"]["sha256"]
    claims.update({item["file"]: item["sha256"] for item in evidence["originals"]})
    return claims


def validate_scope(value: Any, spec: dict, *, for_call: bool) -> dict:
    p = _path()
    validate_observer(spec)
    p.require(type(for_call) is bool, "lifecycle use mode must be an exact Boolean")
    p.keys(value, {"observer_sha256", "generation", "constructor_enter_event", "constructor_return_event",
                   "selection_event", "acquire_event", "consume_event", "owner_thread_id", "acquire_epoch",
                   "checked_epoch", "use", "consumed", "expires"})
    p.require(value["observer_sha256"] == observer_digest(spec), "lifecycle use differs from the exact observer")
    p.integer(value["generation"], 1, spec["generation_limit"])
    order = [value[key] for key in ("constructor_enter_event", "constructor_return_event", "selection_event",
                                    "acquire_event", "consume_event")]
    for event in order:
        p.integer(event, 1, spec["event_limit"])
    p.require(all(left < right for left, right in zip(order, order[1:])),
              "lifecycle use lost ordered birth/selection/acquisition/consumption")
    p.integer(value["owner_thread_id"], 1, p.MAX_DATA_OFFSET)
    p.integer(value["acquire_epoch"], 0, 4096)
    p.integer(value["checked_epoch"], 0, 4096)
    p.require(value["acquire_epoch"] == value["checked_epoch"] == 0 and
              value["use"] == ("dispatch" if for_call else "read") and value["consumed"] is True and
              value["expires"] == "use", "lifecycle use lost its single-operation private epoch/expiry")
    return copy.deepcopy(value)


def validate_result(value: Any, spec: dict, *, scope: dict, for_call: bool,
                    native_calls: int, invocations: int) -> dict:
    """Accept only a complete same-RPC ledger, use row, counters and cleanup.

    The runtime is trusted fixed code. This replays its redacted state decisions
    and cross-binds evidence; it cannot discover hooks that were never admitted
    or turn a persisted descriptive generation into a live capability.
    """
    p = _path()
    validate_scope(scope, spec, for_call=for_call)
    p.keys(value, {"format", "observer_sha256", "status", "owner_thread_id", "selected_generation", "events",
                   "hooks_requested", "hook_attempts", "hooks_installed", "hooks_detached", "listeners_cleanup_flushed",
                   "callbacks_inflight", "lifecycle_frames_inflight", "poisoned", "tokens_expired"})
    p.require(value["format"] == OBSERVATION and value["status"] == "completed" and
              value["observer_sha256"] == observer_digest(spec), "complete bound lifecycle observer evidence required")
    owner, selected = scope["owner_thread_id"], scope["generation"]
    p.integer(value["owner_thread_id"], 1, p.MAX_DATA_OFFSET)
    p.integer(value["selected_generation"], 1, spec["generation_limit"])
    p.require(value["owner_thread_id"] == owner and value["selected_generation"] == selected,
              "lifecycle summary differs from its use owner/generation")
    hooks = 1 + len(spec["destructors"])
    for key in ("hooks_requested", "hook_attempts", "hooks_installed", "hooks_detached"):
        p.integer(value[key], hooks, hooks)
    for key in ("callbacks_inflight", "lifecycle_frames_inflight"):
        p.integer(value[key], 0, 0)
    p.require(value["listeners_cleanup_flushed"] is True and value["poisoned"] is False and
              value["tokens_expired"] is True, "lifecycle observer cleanup/poison/expiry is not complete")
    p.integer(native_calls, int(for_call), int(for_call))
    p.integer(invocations, int(for_call), int(for_call))
    events = value["events"]
    p.require(type(events) is list and 8 <= len(events) <= spec["event_limit"], "bounded complete lifecycle ledger required")
    states, frames, references = {}, [], {}
    armed = ended = chosen = acquired = consumed = stopped = False

    def sole_live():
        return [generation for generation, state in states.items() if state["phase"] == "live"]

    for seq, event in enumerate(events, 1):
        p.keys(event, {"seq", "kind", "generation", "entry", "return_site", "thread"})
        p.integer(event["seq"], seq, seq)
        kind, generation, entry, site, thread = (event[key] for key in ("kind", "generation", "entry", "return_site", "thread"))
        p.require(type(kind) is str and kind in EVENT_KINDS and not stopped,
                  "unknown or post-stop lifecycle event")
        if generation is not None:
            p.integer(generation, 1, spec["generation_limit"])
        if entry is not None:
            p.integer(entry, 0, max(0, len(spec["destructors"]) - 1))
        if site is not None:
            p.integer(site, 0, len(spec["constructor"]["return_sites"]) - 1)
        if thread is not None:
            p.integer(thread, 1, p.MAX_DATA_OFFSET)
        p.require(kind != "poison", "poisoned lifecycle ledger cannot prove a completed use")
        if kind == "armed":
            p.require(seq == 1 and not armed and generation is entry is site is None and thread == owner,
                      "lifecycle observer must arm once on its actual main thread")
            armed = True
            continue
        p.require(armed, "lifecycle event precedes complete observer arming")
        if kind in {"constructor_enter", "constructor_return", "destructor_enter", "destructor_leave"}:
            p.require(thread == owner and entry is not None, "lifecycle object event is not on its admitted owner thread")
            if acquired and not consumed:
                p.require(False, "lifecycle event reentered an acquired immediate-use scope")
            if kind == "constructor_enter":
                p.require(not ended and entry == 0 and not frames and (generation is None) is (site is None),
                          "post-window, nested or unbound constructor entry cannot admit a root")
                if generation is not None:
                    p.require(generation == len(states) + 1 and generation not in states,
                              "lifecycle generation is reused, skipped or stale")
                    states[generation] = {"phase": "constructing", "enter": seq, "return": None, "site": site}
                frames.append(("constructor", generation, entry, site))
            elif kind == "constructor_return":
                p.require(bool(frames) and frames[-1] == ("constructor", generation, entry, site),
                          "constructor completion lacks its exact matching entry")
                frames.pop()
                if generation is not None:
                    p.require(states[generation]["phase"] == "constructing", "invalidated construction cannot become live")
                    states[generation].update(phase="live", **{"return": seq})
                    if chosen and not consumed:
                        p.require(sole_live() == [selected], "another live generation made the frozen selection ambiguous")
            elif kind == "destructor_enter":
                p.require(site is None, "destructor evidence cannot invent a constructor return site")
                if generation is not None:
                    p.require(generation in states, "destructor refers to an unwitnessed generation")
                    p.require(not (chosen and not consumed and generation == selected),
                              "selected generation was destroyed before its use")
                    states[generation]["phase"] = "dead"
                frames.append(("destructor", generation, entry, None))
            else:
                p.require(site is None and bool(frames) and frames[-1] == ("destructor", generation, entry, None),
                          "destructor leave lacks its exact matching entry")
                frames.pop()
            continue
        p.require(entry is site is None, "lifecycle control event cannot carry a hook entry/return site")
        if kind == "observation_end":
            p.require(not ended and not frames and generation is thread is None,
                      "observation ended during an incomplete or nested lifecycle phase")
            ended = True
        elif kind == "selected":
            p.require(ended and not chosen and not frames and generation == selected and thread is None and
                      seq > 1 and events[seq - 2]["kind"] == "observation_end" and sole_live() == [selected],
                      "lifecycle selection is not the sole live generation at the fixed window end")
            chosen = True
            references["selection_event"] = seq
        elif kind == "acquire":
            p.require(chosen and not acquired and not frames and generation == selected and thread == owner and
                      sole_live() == [selected], "lifecycle acquisition lost its exact live primary slot")
            acquired = True
            references["acquire_event"] = seq
        elif kind == "consume":
            p.require(acquired and not consumed and not frames and generation == selected and thread == owner and
                      sole_live() == [selected] and events[seq - 2]["kind"] == "acquire",
                      "lifecycle use is stale, reentrant, ambiguous or reused")
            consumed = True
            references["consume_event"] = seq
        elif kind == "stop":
            p.require(consumed and not frames and generation is thread is None and seq == len(events),
                      "lifecycle stop did not expire a completed quiescent scope")
            stopped = True
        else:
            p.require(False, "unsupported lifecycle control event")
    p.require(stopped and not frames and selected in states, "lifecycle ledger did not finish its selected use")
    references.update(constructor_enter_event=states[selected]["enter"], constructor_return_event=states[selected]["return"])
    p.require(all(scope[key] == seq for key, seq in references.items()),
              "lifecycle use references another session's birth/selection/consumption events")
    return copy.deepcopy(value)
