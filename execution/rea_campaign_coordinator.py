"""Caller-private deterministic lifecycle for an approved fixed-state campaign.

The reasoning driver stops at the hypothesis. This coordinator then uses fixed
public setup and the existing G/PB/RR proof gates without more model decisions.
TrustedHooks are authorized Python code supplied directly by a caller; no JSON
callback, import, shell, source program or task-native setup is interpreted.
Completed journal boundaries can resume. An attempted/failed/uncertain action
never resets or retries, including when another output folder is requested.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from . import rea_agent as G
from . import rea_mapping as R
from . import rea_ax_state as S
from . import rea_runtime_bridge as B
from . import rea_playback_trial as P
from . import rea_process_restart as RR
from . import rea_state_campaign as C
from . import rea_public_campaign as U
from . import rea_playback_readiness as V
from . import rea_actions as A
from . import rea_document_restoration as D

CONFIG = "anyuse.rea-campaign-coordinator/1"
JOURNAL = "anyuse.rea-campaign-coordinator-journal/1"
PREPARED = "anyuse.rea-prepared-fixed-state-case/1"
_MIB = 1024 * 1024
DEFAULT_STORAGE = {"reserve_bytes": 512 * _MIB, "action_headroom_bytes": 128 * _MIB,
                   "max_observed_decline_bytes": 4096 * _MIB}
ROLES = (("learning", "record", "recordings", "learning"),
         ("verification", "verify", "fresh_recordings", "fresh"),
         ("heldout", "heldout", "heldout_recordings", "heldout"))


def _need(ok, why):
    if not ok:
        raise ValueError(why)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed coordinator data schema required")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _resource(path, maximum, *, retain=False):
    file = Path(path)
    before = file.lstat()
    _need(file.is_absolute() and file.resolve(strict=True) == file and stat.S_ISREG(before.st_mode) and not file.is_symlink(),
          "canonical regular coordinator resource required")
    _need(type(maximum) is int and 0 < before.st_size <= maximum, "coordinator resource exceeds its byte bound")
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    digest, total, chunks = hashlib.sha256(), 0, []
    with os.fdopen(os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        _need(identity(os.fstat(stream.fileno())) == identity(before), "coordinator resource changed before read")
        while True:
            chunk = stream.read(min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            total += len(chunk)
            _need(total <= maximum, "coordinator resource grew beyond its byte bound")
            digest.update(chunk)
            if retain:
                chunks.append(chunk)
        _need(identity(os.fstat(stream.fileno())) == identity(before) and total == before.st_size,
              "coordinator resource changed during read")
    _need(identity(file.lstat()) == identity(before) and file.resolve(strict=True) == file,
          "coordinator resource path changed during read")
    return {"file": str(file), "bytes": total, "sha256": digest.hexdigest()}, b"".join(chunks) if retain else None


def _pin(path, maximum=R.MAX_JSON_BYTES):
    return _resource(path, maximum)[0]


def _sources():
    # Fixed policy closure, never caller-selected producer/import paths.
    modules = {"coordinator": __file__, "agent": G.__file__, "mapping": R.__file__,
               "ax_state": S.__file__, "runtime_bridge": B.__file__, "playback_trial": P.__file__,
               "process_restart": RR.__file__, "state_campaign": C.__file__, "public_campaign": U.__file__,
               "public_readiness": V.__file__,
               "playback_state": P.O.__file__, "state_evidence": P.E.__file__, "harness": P.H.__file__,
               "state_task": P.T.__file__, "actions": A.__file__, "document_restoration": D.__file__}
    return {role: _pin(Path(file).resolve(strict=True)) for role, file in modules.items()}


def _verified(pin, maximum, *, retain=False):
    _keys(pin, {"file", "bytes", "sha256"})
    _need(type(pin["file"]) is str and type(pin["bytes"]) is int and 0 < pin["bytes"] <= maximum and
          type(pin["sha256"]) is str and R.SHA.fullmatch(pin["sha256"]), "bounded exact resource pin required")
    actual, raw = _resource(pin["file"], maximum, retain=retain)
    _need(actual == pin, "coordinator resource bytes changed")
    return raw


def _verify(pin, maximum=R.MAX_JSON_BYTES):
    _verified(pin, maximum)


def _read(pin, maximum=R.MAX_JSON_BYTES):
    return _verified(pin, maximum, retain=True)


def _save(path, value, *, replace=False):
    raw = _json(value) + b"\n"
    _need(len(raw) <= R.MAX_JSON_BYTES, "bounded coordinator journal required")
    pending = path.with_name("." + path.name + ".pending") if replace else path
    with pending.open("xb") as stream:
        os.chmod(pending, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(pending, path)
    return _pin(path)


def _snapshot(root):
    file = root / "session.json"
    pin, raw = _resource(file, R.MAX_JSON_BYTES, retain=True)
    state = G._load(root)
    _need(_pin(file) == pin, "G session changed during caller snapshot")
    return state, _hash(raw)


def _healthy(state):
    _need(state.get("workflow") == C.BACKEND and state.get("observer_profile") is not None and
          state.get("hypothesis") is not None, "an approved fixed-state hypothesis is required before coordination")
    _need(state["status"] not in {"resource_review_required", "evaluation_failed", "hypothesis_failed", "needs_capability"} and
          not any(row.get("status") in {"running", "failed"} for row in state["steps"]),
          "failed or uncertain G effects require review; no coordinator retry")
    _need(state["hypothesis"]["profile"]["policy"] == state["observer_profile"]["policy"],
          "hypothesis observer differs from the caller-approved contract")


def validate_configuration(value, state, session):
    value = copy.deepcopy(value)
    expected = {"format", "session", "target", "plan_sha256", "observer_profile_sha256", "owned", "expected_identity",
                "cases", "launch_environment", "limits", "publication"}
    _need(type(value) is dict and expected <= set(value) <= expected | {"storage", "restoration"},
          "closed coordinator data schema required")
    value.setdefault("storage", copy.deepcopy(DEFAULT_STORAGE))
    storage = value["storage"]
    _keys(storage, DEFAULT_STORAGE)
    _need(all(type(storage[name]) is int and _MIB <= storage[name] <= 1024 ** 4 for name in storage) and
          storage["action_headroom_bytes"] <= storage["max_observed_decline_bytes"],
          "finite explicit storage reserve, action headroom and observed-decline bounds required")
    _need(value["format"] == CONFIG and value["session"] == str(session) and value["owned"] is True and
          value["target"] == state["target"] and value["plan_sha256"] == state["plan_sha256"] and
          value["observer_profile_sha256"] == state["observer_profile_sha256"],
          "configuration must approve the exact owned session, plan, observer and target")
    identity = S.validate_identity(value["expected_identity"])
    _need(identity["bundle"] == state["bundle"] and identity["bundle_id"] == state["target"]["bundle_id"] and
          identity["uid"] == os.getuid() == os.geteuid(), "same-user exact owned process required")
    rows = [row for role, *_ in ROLES for row in state["plan"][role]]
    _need(type(value["cases"]) is list and len(value["cases"]) == len(rows), "one private media binding per frozen case required")
    media, paths = set(), set()
    for binding, declared in zip(value["cases"], rows):
        _keys(binding, {"id", "media"})
        _need(binding["id"] == declared["id"], "private media bindings must retain frozen case order")
        _verify(binding["media"], P.O.MAX_MEDIA_BYTES)
        _need(binding["media"]["sha256"] not in media and binding["media"]["file"] not in paths,
              "distinct case media bytes and paths required")
        media.add(binding["media"]["sha256"]); paths.add(binding["media"]["file"])
    limits = value["limits"]
    _keys(limits, {"seconds", "setup_seconds", "restart_seconds", "trial_seconds", "max_actions"})
    for name, lo, hi in (("seconds", 30, 7200), ("setup_seconds", 1, 35), ("restart_seconds", 1, 60),
                         ("trial_seconds", 5, 60), ("max_actions", 1, 256)):
        _need(type(limits[name]) is int and lo <= limits[name] <= hi, "finite explicit coordinator action/time bounds required")
    if "restoration" in value:
        recovery = value["restoration"]
        _keys(recovery, {"checkpoint", "seconds"})
        _need(type(recovery["seconds"]) is int and 1 <= recovery["seconds"] <= 60 and
              recovery["seconds"] < limits["seconds"], "explicit restoration time reserve inside campaign deadline required")
        checkpoint = D.inspect_checkpoint(recovery["checkpoint"])
        _need(checkpoint["context"]["identity"] == identity and checkpoint["context"]["target"] == state["target"],
              "restoration checkpoint must bind the original campaign process and target")
    _keys(value["publication"], {"summary", "details"})
    _need(all(G._text(value["publication"][name], 4096) for name in ("summary", "details")), "bounded publication text required")
    _need(len(_json(value)) <= 256 * 1024, "bounded private coordinator configuration required")
    # Validate fixed backend storage even when an explicit trusted backend is supplied.
    _need(type(value["launch_environment"]) is dict, "closed launch environment mapping required")
    U.PublicPlayback(value["launch_environment"])
    return value


def _storage_bindings(session, folder, cfg):
    # Watch actual framework output and caller-declared private launch storage.
    # No arbitrary probe path can redirect admission to an unrelated volume.
    paths = {str(session), str(folder), *cfg["launch_environment"].values()}
    if "restoration" in cfg:
        workspace = D._workspace(D.inspect_checkpoint(cfg["restoration"]["checkpoint"])["workspace"])
        paths.add(workspace["root"]["path"])
    directories, filesystems = [], {}
    for text in sorted(paths):
        path = Path(text)
        info = path.lstat()
        _need(path.is_absolute() and path.resolve(strict=True) == path and stat.S_ISDIR(info.st_mode) and
              not path.is_symlink(), "canonical existing coordinator storage directory required")
        row = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
        directories.append(row)
        filesystems.setdefault(str(info.st_dev), {"path": str(path), "device": info.st_dev})
    return {"directories": directories, "filesystems": list(filesystems.values())}


def _available_bytes(path):
    value = os.statvfs(path)
    return value.f_bavail * value.f_frsize


def _storage_sample(binding, session, folder, cfg):
    _need(_storage_bindings(session, folder, cfg) == binding, "coordinator storage directory/device binding changed")
    rows = []
    for filesystem in binding["filesystems"]:
        available = _available_bytes(filesystem["path"])
        _need(type(available) is int and available >= 0, "actual available storage measurement required")
        rows.append({**filesystem, "available_bytes": available})
    _need(_storage_bindings(session, folder, cfg) == binding,
          "coordinator storage directory/device changed during measurement")
    return {"observed_monotonic": time.monotonic(), "filesystems": rows}


def _storage_admit(sample, policy, declines):
    for row in sample["filesystems"]:
        _need(row["available_bytes"] >= policy["reserve_bytes"] + policy["action_headroom_bytes"],
              "storage admission refused: reserve plus per-action headroom unavailable")
    _need(sum(declines.values()) + policy["action_headroom_bytes"] * len(sample["filesystems"]) <=
          policy["max_observed_decline_bytes"],
          "storage admission refused: aggregate observed-decline budget cannot admit another action")


def _storage_account(before, after, policy, declines, *, action=True):
    previous = {str(row["device"]): row["available_bytes"] for row in before["filesystems"]}
    for row in after["filesystems"]:
        device = str(row["device"])
        row["observed_decline_bytes"] = max(0, previous[device] - row["available_bytes"])
        declines[device] += row["observed_decline_bytes"]
    # Retain all samples/counters before reporting any overrun. Other processes'
    # writes are deliberately not attributed to this coordinator.
    for row in after["filesystems"]:
        _need(row["available_bytes"] >= policy["reserve_bytes"], "storage reserve crossed during action; no continuation")
        _need(not action or row["observed_decline_bytes"] <= policy["action_headroom_bytes"],
              "observed storage decline exceeded per-action headroom; no continuation")
    _need(sum(declines.values()) <= policy["max_observed_decline_bytes"],
          "aggregate observed storage decline exceeded campaign bound; no continuation")


@dataclass(frozen=True)
class TrustedHooks:
    setup: object
    stop: object
    launch: object
    producers: dict

    def __post_init__(self):
        _need(all(callable(fn) for fn in (self.setup, self.stop, self.launch)), "trusted setup/stop/launch Python callbacks required")
        _keys(self.producers, {"setup", "stop", "launch"})
        object.__setattr__(self, "producers", copy.deepcopy(self.producers))
        for pin in self.producers.values():
            _read(pin)


@dataclass(frozen=True)
class PreparedCase:
    receipt: dict

    def __post_init__(self):
        object.__setattr__(self, "receipt", copy.deepcopy(self.receipt))


class _Boundary(Exception):
    pass


def _setup_chronology(setup, intent, started, finished):
    times = (started, setup.get("started_monotonic"), setup.get("finished_monotonic"),
             intent.get("observed_monotonic"), finished, time.monotonic())
    _need(all(type(value) in {float, int} and math.isfinite(value) and value >= 0 for value in times) and
          all(left <= right for left, right in zip(times, times[1:])),
          "actual setup, intent and prepare-action chronology differs")


def validate_prepared(state, session, request, handoff):
    """Caller-private G boundary; validate once before G records/dispatches it."""
    _need(isinstance(handoff, PreparedCase), "prepared_case is a caller Python handoff, never model JSON")
    if state.get("workflow") == "guest_model_judged_state_v1":
        return _validate_visual_prepared(state, session, request, handoff)
    value = json.loads(_read(handoff.receipt))
    _keys(value, {"format", "status", "configuration", "coordinator", "session", "session_sha256", "plan_sha256",
                  "hypothesis_sha256", "policy_sha256", "op", "role", "case", "case_id", "media", "identity", "intent",
                  "setup", "restart", "producers", "sources", "expires_wall"})
    _need(value["format"] == PREPARED and value["status"] == "prepared" and value["session"] == str(session) and
          value["session_sha256"] == _pin(session / "session.json")["sha256"] and
          value["plan_sha256"] == state["plan_sha256"] and value["hypothesis_sha256"] == R.digest(state["hypothesis"]) and
          value["policy_sha256"] == R.digest(state["hypothesis"]["profile"]["policy"]), "prepared case is stale or belongs to another hypothesis/session")
    _need(type(value["expires_wall"]) in {float, int} and math.isfinite(value["expires_wall"]) and time.time() < value["expires_wall"],
          "prepared case deadline expired")
    configuration = json.loads(_read(value["configuration"]))
    journal = R.load_json(Path(value["coordinator"]) / "coordinator.json")
    _need(configuration["session"] == str(session) and state.get("coordinator", {}).get("out") == value["coordinator"] and
          state["coordinator"]["configuration_sha256"] == R.digest(configuration) and
          journal["configuration"] == value["configuration"] and journal["sources"] == value["sources"] and
          journal["producers"] == value["producers"] and journal["status"] == "running" and
          journal["session_sha256"] == value["session_sha256"] and
          journal["steps"][-1]["status"] == "started" and journal["steps"][-1]["kind"] == request["op"] and
          journal["steps"][-1]["session_before_sha256"] == value["session_sha256"],
          "prepared case coordinator binding differs")
    matching = [row for row in ROLES if row[1] == request["op"]]
    _need(len(matching) == 1 and "restart" not in request and type(request.get("case")) is int,
          "caller preparation must bind one fixed-state trial and own its restart handoff")
    role, op, storage, phase = matching[0]
    index = request["case"]
    _need(value["op"] == op and value["role"] == role and value["case"] == index and
          0 <= index < len(state["plan"][role]) and value["case_id"] == state["plan"][role][index]["id"] and
          index == len(state.get(storage, {})), "prepared case op/index/order differs from frozen plan")
    _need(not any(row.get("prepared_case") == handoff.receipt for row in state["steps"]), "prepared case was already consumed")
    for pin in [*value["sources"].values(), *value["producers"].values()]:
        _read(pin)
    _need(value["sources"] == _sources(),
          "prepared caller implementation changed")
    media = next(row["media"] for row in configuration["cases"] if row["id"] == value["case_id"])
    _need(value["media"] == media, "prepared media differs from frozen private binding")
    _verify(media, P.O.MAX_MEDIA_BYTES)
    identity = S.validate_identity(value["identity"])
    _need(S._same(B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]), identity),
          "prepared process epoch changed; no successor adoption")
    intent = P.validate_intent(state["hypothesis"]["profile"]["policy"], value["intent"])
    _need(intent["id"] == value["case_id"] and intent["phase"] == phase and intent["deployment"]["identity"] == identity and
          intent["context"]["media"] == media, "prepared actual playback intent differs")
    setup = json.loads(_read(value["setup"]))
    _need(setup["status"] == "ready" and setup["identity"] == identity and setup["media"] == media["file"] and
          setup["native_goal_calls"] == 0, "actual public setup journal differs or reports a task-native call")
    _need(len(journal["steps"]) >= 2, "actual completed prepare action required")
    preparation = journal["steps"][-2]
    _need(preparation["kind"] == "prepare" and preparation["status"] == "completed" and
          preparation["session_before_sha256"] == value["session_sha256"] == preparation["session_after_sha256"],
          "prepared case must follow its actual unchanged prepare action")
    prepared_result = json.loads(_read(preparation["result"]))
    _keys(prepared_result, {"prepared"})
    _need(prepared_result["prepared"] == handoff.receipt, "prepare action retained another handoff")
    _setup_chronology(setup, intent, preparation["started_monotonic"], preparation["finished_monotonic"])
    restart = value["restart"]
    _need(state["plan"][role][index]["restart"] == (restart is not None), "prepared restart differs from frozen declaration")
    if restart is not None:
        restart = C.validate_restart_entry(restart)
        witness = RR.validate_restart(json.loads(_read(restart["intent"])), restart["receipt"])
        predecessors = [(declared["id"], state[store][str(i)]) for r, _, store, _ in ROLES
                        for i, declared in enumerate(state["plan"][r]) if str(i) in state.get(store, {})]
        _need(predecessors and restart["predecessor"] == predecessors[-1][0], "restart predecessor is not the latest actual completed trial")
        previous = predecessors[-1][1]
        previous_pin = previous.get("case", previous)
        previous_case = json.loads(_read(previous_pin))
        measured = C._playback().validate_trial(state["hypothesis"]["profile"]["policy"], previous_case,
                                               state["hypothesis"]["actor"])
        receipt = json.loads(_read(restart["receipt"]))
        baseline = json.loads(_read(receipt["baseline"]))
        _need(witness["restart_measurement_validated"] is True and witness["target"] == state["target"] and
              witness["new_identity"] == identity and witness["old_identity"] == measured["identity"] and
              witness["old_kernel"] == measured["kernel"] and
              measured["finished_monotonic"] < baseline["started_monotonic"] <= receipt["finished_monotonic"] < intent["observed_monotonic"],
              "prepared restart does not bind this actual predecessor/process/time")
    return value


def run(session, configuration, out, *, backend=None, resume=False, actions_per_run=256, restoration_backend=None):
    """Finish the frozen nine-case lifecycle after the reasoning hypothesis.

    Public preparation/restart are deterministic caller actions, never model
    tools. Defaults use fixed PublicPlayback; TrustedHooks require explicit
    authorized Python callbacks and real producer pins. No automatic retries.
    """
    R.assert_guest()
    _need(type(actions_per_run) is int and 1 <= actions_per_run <= 256, "bounded coordinator invocation required")
    session, folder = Path(session).resolve(strict=True), Path(out).resolve()
    _need(not folder.is_relative_to(session) and not session.is_relative_to(folder), "separate coordinator output required")
    state, session_hash = _snapshot(session)
    _healthy(state)
    cfg = validate_configuration(configuration, state, session)
    recovery_cfg = cfg.get("restoration")
    _need(recovery_cfg is not None or restoration_backend is None, "restoration adapter requires an explicit frozen contract")
    recovery_backend = D._backend(restoration_backend) if recovery_cfg is not None else None
    if recovery_cfg is not None:
        _need(not resume and actions_per_run >= cfg["limits"]["max_actions"],
              "required document restoration disallows partial-run/resume boundaries")
        _need(recovery_backend.producers == D.inspect_checkpoint(recovery_cfg["checkpoint"])["producers"],
              "restoration adapter differs from captured checkpoint")
    _need(not folder.is_relative_to(Path(state["bundle"])), "coordinator output must be outside the app")
    backend = U.PublicPlayback(cfg["launch_environment"]) if backend is None else backend
    _need(isinstance(backend, (U.PublicPlayback, TrustedHooks)), "fixed backend or explicit typed trusted hooks required")
    producers = copy.deepcopy(backend.producers)
    _keys(producers, {"setup", "stop", "launch"})
    sources = _sources()
    if not resume:
        _need(not folder.exists() and "coordinator" not in state, "new coordinator output/session binding required; no retry with another folder")
        folder.mkdir(mode=0o700, parents=True)
        config_pin = _save(folder / "configuration.json", cfg)
        journal = {"format": JOURNAL, "status": "initializing", "configuration": config_pin, "sources": sources,
                   "producers": producers, "identity": cfg["expected_identity"], "steps": [], "error": None,
                   "deadline_wall": min(time.time() + cfg["limits"]["seconds"], state["deadline"]),
                   "session_sha256": session_hash, "model_calls": 0, "automatic_retry": False, "published": None,
                   "storage": {"policy": copy.deepcopy(cfg["storage"]), "binding": _storage_bindings(session, folder, cfg),
                               "last_sample": None}}
        if recovery_cfg is not None:
            journal["primary_outcome"] = {"status": "pending", "error": None}
            journal["restoration"] = {"status": "pending", "checkpoint": recovery_cfg["checkpoint"],
                                      "producers": copy.deepcopy(recovery_backend.producers), "result": None}
        journal["storage"]["observed_decline_bytes"] = {str(row["device"]): 0 for row in journal["storage"]["binding"]["filesystems"]}
        _save(folder / "coordinator.json", journal)
        with (session / "session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current, current_hash = _snapshot(session)
            _need(current_hash == session_hash and "coordinator" not in current, "G session changed before coordinator binding")
            current["coordinator"] = {"out": str(folder), "configuration_sha256": R.digest(cfg)}
            R.write_json(session / "session.json", current)
        state, session_hash = _snapshot(session)
        journal.update(status="ready", session_sha256=session_hash)
        _save(folder / "coordinator.json", journal, replace=True)
    else:
        _need(folder.is_dir(), "existing completed-boundary journal required for resume")
        journal = R.load_json(folder / "coordinator.json")
        _need(journal["format"] == JOURNAL and journal["status"] in {"ready", "completed"} and
              all(row["status"] == "completed" for row in journal["steps"]), "partial/failed/uncertain coordinator cannot resume or retry")
        _need(json.loads(_read(journal["configuration"])) == cfg and journal["sources"] == sources and
              journal["producers"] == producers and journal["session_sha256"] == session_hash and
              state.get("coordinator") == {"out": str(folder), "configuration_sha256": R.digest(cfg)},
              "coordinator session/configuration/producer drift; no resume")
        expected_storage = journal.get("storage", {})
        _need(expected_storage.get("policy") == cfg["storage"] and
              expected_storage.get("binding") == _storage_bindings(session, folder, cfg),
              "coordinator storage policy/directory binding drift; no resume")
        devices = {str(row["device"]) for row in expected_storage["binding"]["filesystems"]}
        declines = expected_storage.get("observed_decline_bytes", {})
        _need(type(declines) is dict and set(declines) == devices and
              all(type(value) is int and 0 <= value <= cfg["storage"]["max_observed_decline_bytes"] for value in declines.values()),
              "invalid retained storage accounting; no resume")
        rebuilt = {device: 0 for device in devices}
        previous = None
        for row in journal["steps"]:
            retained = row.get("storage", {})
            _need(set(retained) == {"before", "after", "error"} and retained["error"] is None and
                  retained["before"] is not None and retained["after"] is not None,
                  "incomplete retained storage measurements; no resume")
            for sample in (retained["before"], retained["after"]):
                _need(type(sample) is dict and set(sample) == {"observed_monotonic", "filesystems"} and
                      type(sample["observed_monotonic"]) in {int, float} and math.isfinite(sample["observed_monotonic"]) and
                      sample["observed_monotonic"] >= 0 and type(sample["filesystems"]) is list and
                      [{"path": part["path"], "device": part["device"]} for part in sample["filesystems"]] == expected_storage["binding"]["filesystems"] and
                      all(type(part["available_bytes"]) is int and part["available_bytes"] >= 0 for part in sample["filesystems"]),
                      "invalid retained storage measurements; no resume")
                if previous is not None:
                    _need(previous["observed_monotonic"] <= sample["observed_monotonic"], "storage measurement chronology changed; no resume")
                previous = sample
            if row is not journal["steps"][0]:
                _storage_account(last, copy.deepcopy(retained["before"]), cfg["storage"], rebuilt, action=False)
            _storage_admit(retained["before"], cfg["storage"], rebuilt)
            _storage_account(retained["before"], copy.deepcopy(retained["after"]), cfg["storage"], rebuilt)
            last = retained["after"]
        _need(rebuilt == declines and expected_storage.get("last_sample") == previous,
              "retained storage accounting drift; no resume")
    with (folder / "coordinator.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _need(R.load_json(folder / "coordinator.json") == journal and _snapshot(session)[1] == journal["session_sha256"],
              "coordinator journal/session changed before ownership; no stale journal overwrite")
        invocation_start = len(journal["steps"])
        def retain():
            _save(folder / "coordinator.json", journal, replace=True)
        def work_timeout(limit, minimum=1):
            reserve = recovery_cfg["seconds"] if recovery_cfg is not None and journal["restoration"]["status"] != "restored" else 0
            remaining = int(journal["deadline_wall"] - reserve - time.time())
            _need(remaining >= minimum, "insufficient work budget before reserved document cleanup; no dispatch")
            return min(limit, remaining)
        def current():
            fresh, raw_hash = _snapshot(session)
            _healthy(fresh)
            reserve = recovery_cfg["seconds"] if recovery_cfg is not None and journal["restoration"]["status"] != "restored" else 0
            _need(raw_hash == journal["session_sha256"] and time.time() < journal["deadline_wall"] - reserve,
                  "G session drift or campaign work deadline expired (cleanup reserve retained)")
            _need(json.loads(_read(journal["configuration"])) == cfg, "private coordinator configuration changed")
            _need(sources == _sources(), "coordinator policy implementation changed")
            for pin in [*sources.values(), *producers.values()]:
                _read(pin)
            _need(S._same(B.process_identity(Path(fresh["bundle"]), fresh["target"]["bundle_id"]), journal["identity"]),
                  "owned process epoch drift; no adoption")
            return fresh
        def restore_once():
            if recovery_cfg is None or journal["restoration"]["status"] != "pending":
                return
            recovery = journal["restoration"]
            recovery["status"] = "started"
            retain()  # An interrupted attempt is never silently repeated.
            try:
                fresh, _ = _snapshot(session)
                _need(json.loads(_read(journal["configuration"])) == cfg and sources == _sources(),
                      "source/configuration drift prevents recovery actions")
                def uncertain(value):
                    if type(value) is dict:
                        if (value.get("resource_review_required") is True or value.get("status") == "resource_review_required" or
                            "native_state_uncertain" in value and value["native_state_uncertain"] is not False or
                            any(key in value and value[key] is None for key in ("native_calls", "invocations", "helper_load_attempts")) or
                            "reaped" in value and value["reaped"] is not True or
                            value.get("manager_state") in {"pending", "failed"}):
                            return True
                        return any(uncertain(v) for v in value.values())
                    return type(value) is list and any(uncertain(v) for v in value)
                _need(not any(row.get("status") == "running" for row in fresh["steps"]),
                      "interrupted G action has no final disposition; recovery actions refused")
                _need(not uncertain(fresh) and not uncertain(journal["steps"]),
                      "native resource cleanup is unknown; document recovery actions refused")
                # G can retain a failed native result only in its hashed artifact
                # registry. Inspect those actual JSON receipts too, not merely
                # the truncated error projected into the session history.
                for artifact in fresh["artifacts"].values():
                    path = Path(artifact["file"])
                    if path.suffix == ".json":
                        _need(path.is_relative_to(session), "registered recovery evidence escaped session")
                        _need(not uncertain(json.loads(_read(artifact))), "retained native worker evidence is uncertain; recovery refused")
                native_ops = {"record", "verify", "heldout"}
                failed_native = [row for row in fresh["steps"]
                                 if row.get("status") == "failed" and row["request"]["op"] in native_ops]
                if any(row["kind"] in native_ops and row["status"] != "completed" for row in journal["steps"]):
                    _need(fresh["steps"] and fresh["steps"][-1]["request"]["op"] in native_ops,
                          "native-capable coordinator failure lacks final G disposition")
                    failed_native.append(fresh["steps"][-1])
                for row in failed_native:
                    # An observer failure may follow a clean completed native
                    # action. Reuse its existing full retained validator; never
                    # infer cleanup from missing/error-projected feedback.
                    base = f"s{row['step']:04d}/record/runtime/"
                    runtime_pin = fresh["artifacts"].get(base + "runtime.json")
                    worker_pin = fresh["artifacts"].get(base + "worker-output.json")
                    _need(runtime_pin is not None and worker_pin is not None,
                          "failed native-capable action lacks positive final cleanup evidence")
                    reader = P.E._Reader()
                    runtime = reader.json(runtime_pin, "trial")
                    P._native(reader, fresh["hypothesis"]["actor"], runtime, worker_pin,
                              journal["identity"], fresh["target"])
                    reader.recheck()
                _need(S._same(B.process_identity(Path(fresh["bundle"]), fresh["target"]["bundle_id"]), journal["identity"]),
                      "process changed before recovery; no adoption")
                remaining = int(journal["deadline_wall"] - time.time())
                _need(remaining >= 1, "original campaign authorization expired before recovery")
                restarts = []
                for row in journal["steps"]:
                    if row["kind"] == "restart" and row["status"] == "completed":
                        entry = json.loads(_read(row["result"]))["restart"]
                        restarts.append({key: entry[key] for key in ("intent", "receipt")})
                current_pin = D.current_document(recovery_cfg["checkpoint"])
                accounting = journal["storage"]
                before_storage = _storage_sample(accounting["binding"], session, folder, cfg)
                recovery["storage"] = {"before": before_storage, "after": None, "error": None}
                if accounting["last_sample"] is not None:
                    _storage_account(accounting["last_sample"], before_storage, cfg["storage"],
                                     accounting["observed_decline_bytes"], action=False)
                _storage_admit(before_storage, cfg["storage"], accounting["observed_decline_bytes"])
                accounting["last_sample"] = copy.deepcopy(before_storage)
                result = D.restore(recovery_cfg["checkpoint"], current_pin, folder / "document-restoration",
                    backend=recovery_backend, identity=journal["identity"], restarts=restarts,
                    timeout=min(recovery_cfg["seconds"], remaining))
                recovery.update(status=result["status"], result=result["receipt"])
                if result["status"] == "restored":
                    D.verify_restored(recovery_cfg["checkpoint"], result["receipt"])
            except BaseException as error:
                recovery.update(status="unknown", error={"type": type(error).__name__, "message": str(error)[:2048]})
                # A core receipt may exist even when control flow was interrupted.
                receipt_file = folder / "document-restoration" / "result.json"
                if receipt_file.is_file():
                    recovery["result"] = D.pin(receipt_file)
                if not isinstance(error, Exception):
                    try: retain()
                    except BaseException as retention_error: raise error from retention_error
                    raise
            finally:
                if recovery.get("storage", {}).get("before") is not None:
                    try:
                        after_storage = _storage_sample(journal["storage"]["binding"], session, folder, cfg)
                        recovery["storage"]["after"] = after_storage
                        _storage_account(recovery["storage"]["before"], after_storage, cfg["storage"],
                                         journal["storage"]["observed_decline_bytes"])
                        journal["storage"]["last_sample"] = copy.deepcopy(after_storage)
                    except BaseException as storage_error:
                        recovery["storage"]["error"] = {"type": type(storage_error).__name__, "message": str(storage_error)[:2048]}
                        recovery["status"] = "unknown"
            retain()
        def action(kind, callback):
            before = current()
            if len(journal["steps"]) - invocation_start >= actions_per_run:
                raise _Boundary()
            _need(len(journal["steps"]) < cfg["limits"]["max_actions"], "coordinator action budget exhausted")
            work = folder / f"a{len(journal['steps']):03d}-{kind}"
            row = {"kind": kind, "status": "not_dispatched", "session_before_sha256": journal["session_sha256"], "out": str(work),
                   "started_monotonic": time.monotonic(), "result": None,
                   "storage": {"before": None, "after": None, "error": None}}
            journal["steps"].append(row); journal["status"] = "running"
            accounting = journal["storage"]
            failure = None
            try:
                row["storage"]["before"] = _storage_sample(accounting["binding"], session, folder, cfg)
                if accounting["last_sample"] is not None:
                    _storage_account(accounting["last_sample"], row["storage"]["before"], cfg["storage"],
                                     accounting["observed_decline_bytes"], action=False)
                accounting["last_sample"] = copy.deepcopy(row["storage"]["before"])
                _storage_admit(row["storage"]["before"], cfg["storage"], accounting["observed_decline_bytes"])
            except Exception as error:
                row["storage"]["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
                row["error"] = copy.deepcopy(row["storage"]["error"])
                row["finished_monotonic"] = time.monotonic()
                journal.update(status="failed_no_retry", error=copy.deepcopy(row["error"]))
                try:
                    retain()
                except BaseException as retention_error:
                    raise error from retention_error
                raise
            work.mkdir(mode=0o700)
            row["status"] = "started"
            retain()
            try:
                value = callback(before, work)
                after, after_hash = _snapshot(session)
                _healthy(after)
                row.update(result=_save(work / "result.json", value), session_after_sha256=after_hash)
            except BaseException as error:
                failure = error
            try:
                row["storage"]["after"] = _storage_sample(accounting["binding"], session, folder, cfg)
                _storage_account(row["storage"]["before"], row["storage"]["after"], cfg["storage"],
                                 accounting["observed_decline_bytes"])
                accounting["last_sample"] = copy.deepcopy(row["storage"]["after"])
            except BaseException as error:
                row["storage"]["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
                if failure is None:
                    failure = error
            row["finished_monotonic"] = time.monotonic()
            if failure is not None:
                row.update(status="failed", error={"type": type(failure).__name__, "message": str(failure)[:2048]})
                journal.update(status="failed_no_retry", error=copy.deepcopy(row["error"]))
                try:
                    retain()
                except BaseException as retention_error:
                    raise failure from retention_error
                raise failure
            row["status"] = "completed"
            journal.update(status="ready", session_sha256=after_hash)
            retain()
            return value
        def g_step(request, prepared=None):
            def call(before, work):
                observed = G.step(str(session), request, prepared_case=prepared)
                _save(work / "g-result.json", observed)
                _need(observed["feedback"]["status"] == "completed", "G trial/lifecycle gate failed; no retry")
                return observed
            return action(request["op"], call)
        def prepare(role, op, storage, phase, index):
            declared = state["plan"][role][index]
            restart = None
            previous_action = journal["steps"][-1] if journal["steps"] else None
            previous_result = json.loads(_read(previous_action["result"])) if previous_action is not None else None
            if previous_action is not None and previous_action["kind"] == "prepare":
                previous_prepared = json.loads(_read(previous_result["prepared"]))
                _need(previous_prepared["case_id"] == declared["id"] and previous_prepared["op"] == op and
                      previous_prepared["session_sha256"] == journal["session_sha256"], "prepared boundary drift; no second setup")
                return PreparedCase(previous_result["prepared"])
            if previous_action is not None and previous_action["kind"] == "restart":
                _need(previous_result["case_id"] == declared["id"], "restart boundary belongs to another case; no second restart")
                restart = previous_result["restart"]
            if declared["restart"] and restart is None:
                def restart_once(before, work):
                    nonlocal restart
                    ordered = [row["id"] for r, _, st, _ in ROLES for i, row in enumerate(before["plan"][r])
                               if str(i) in before.get(st, {})]
                    _need(ordered, "restart requires an actual completed predecessor")
                    old = copy.deepcopy(journal["identity"])
                    intent = {"format": RR.INTENT, "id": "restart-" + declared["id"], "target": before["target"], "old_identity": old,
                        "stop": {"id": "ordinary-stop", "producer": producers["stop"]},
                        "launch": {"id": "ordinary-launch", "producer": producers["launch"]}}
                    result = RR.run_restart(before["bundle"], old, intent, str(work / "restart"), stop_hook=backend.stop,
                        launch_hook=backend.launch, timeout=(work_timeout(cfg["limits"]["restart_seconds"]) if recovery_cfg is not None else
                            min(cfg["limits"]["restart_seconds"], max(1, int(journal["deadline_wall"] - time.time())))))
                    _need(result["status"] == "restart_measurement_validated" and result["measurement"] is not None,
                          "ordinary restart failed or cleanup is unknown; no preparation/trial follows")
                    receipt = json.loads(_read(result["receipt"]))
                    witness = RR.validate_restart(intent, result["receipt"])
                    _need(witness["old_identity"] == old and witness["target"] == before["target"], "restart witness predecessor differs")
                    restart = {"intent": receipt["intent"], "receipt": result["receipt"], "predecessor": ordered[-1]}
                    journal["identity"] = copy.deepcopy(witness["new_identity"])
                    return {"restart": restart, "measurement": result["measurement"], "identity": journal["identity"], "case_id": declared["id"]}
                action("restart", restart_once)
            media = next(row["media"] for row in cfg["cases"] if row["id"] == declared["id"])
            def public_once(before, work):
                _verify(media, P.O.MAX_MEDIA_BYTES)
                public = backend.setup({"bundle": before["bundle"], "target": before["target"], "expected_identity": journal["identity"],
                    "out": str(work / "public"), "deadline_monotonic": time.monotonic() + (work_timeout(cfg["limits"]["setup_seconds"]) if recovery_cfg is not None else
                                                                  min(cfg["limits"]["setup_seconds"], journal["deadline_wall"] - time.time())),
                    "request": {"media": media["file"], "observer": before["hypothesis"]["profile"]["policy"]["observer"]}})
                _need(public["status"] == "ready" and public["identity"] == journal["identity"] and public["media"] == media["file"] and
                      public["native_goal_calls"] == 0, "public preparation failed or targeted another process/media")
                setup_pin = _pin(public["journal"])
                _need(Path(setup_pin["file"]).is_relative_to(work / "public"), "public setup receipt must be retained inside this owned action")
                unchanged, raw_hash = _snapshot(session)
                _need(raw_hash == journal["session_sha256"], "G session changed during public setup")
                intent = P.make_intent(before["bundle"], before["hypothesis"]["profile"]["policy"], declared["id"], phase,
                                       expected_identity=journal["identity"])
                _need(intent["context"]["media"] == media, "actual setup selected another media file")
                setup = json.loads(_read(setup_pin))
                _need(setup["status"] == "ready" and setup["identity"] == journal["identity"] and
                      setup["media"] == media["file"] and setup["native_goal_calls"] == 0,
                      "retained public setup journal differs")
                _setup_chronology(setup, intent, journal["steps"][-1]["started_monotonic"], time.monotonic())
                value = {"format": PREPARED, "status": "prepared", "configuration": journal["configuration"], "coordinator": str(folder),
                    "session": str(session), "session_sha256": raw_hash, "plan_sha256": before["plan_sha256"],
                    "hypothesis_sha256": R.digest(before["hypothesis"]), "policy_sha256": R.digest(before["hypothesis"]["profile"]["policy"]),
                    "op": op, "role": role, "case": index, "case_id": declared["id"], "media": media, "identity": journal["identity"],
                    "intent": intent, "setup": setup_pin, "restart": restart, "producers": producers, "sources": sources,
                    "expires_wall": min(journal["deadline_wall"], time.time() + cfg["limits"]["trial_seconds"])}
                prepared_pin = _save(work / "prepared.json", value)
                return {"prepared": prepared_pin}
            result = action("prepare", public_once)
            return PreparedCase(result["prepared"])
        try:
            for role, op, storage, phase in ROLES:
                state = current()
                for index in range(len(state["plan"][role])):
                    state = current()
                    if str(index) in state.get(storage, {}):
                        continue
                    prepared = prepare(role, op, storage, phase, index)
                    timeout = work_timeout(cfg["limits"]["trial_seconds"], minimum=5) if recovery_cfg is not None else cfg["limits"]["trial_seconds"]
                    g_step({"op": op, "case": index, "timeout": timeout}, prepared)
                state = current()
                if role == "learning" and state["status"] == "recording":
                    g_step({"op": "learn"})
            state = current()
            if recovery_cfg is not None:
                journal["primary_outcome"] = {"status": "proof_completed", "error": None}
                restore_once()
                _need(journal["restoration"]["status"] == "restored", "required document restoration incomplete; publication refused")
            if state["status"] != "published":
                g_step({"op": "publish", **cfg["publication"]})
            state, raw_hash = _snapshot(session)
            _need(state["status"] == "published" and state.get("heldout_passed"), "complete nine-case proof gate did not publish")
            journal.update(status="completed", published=state["published"], session_sha256=raw_hash)
            if recovery_cfg is not None:
                journal["primary_outcome"] = {"status": "completed", "error": None}
            retain()
        except _Boundary:
            journal.update(status="ready", boundary="completed_actions_only; explicit resume continues without repeating setup/restart")
            retain()
        except BaseException as error:
            primary = error
            if recovery_cfg is None:
                if journal["status"] not in {"failed_no_retry"}:
                    journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:2048]})
                    retain()
                if not isinstance(error, Exception): raise
                return copy.deepcopy(journal)
            if recovery_cfg is not None and journal["primary_outcome"]["status"] != "proof_completed":
                journal["primary_outcome"] = {"status": "interrupted" if not isinstance(error, Exception) else "failed",
                                              "error": {"type": type(error).__name__, "message": str(error)[:2048]}}
            if journal["status"] not in {"failed_no_retry"}:
                journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:2048]})
            try:
                retain()
                restore_once()
            except BaseException as cleanup_error:
                if recovery_cfg is not None:
                    journal["restoration"].setdefault("finalization_error", {"type": type(cleanup_error).__name__, "message": str(cleanup_error)[:2048]})
                try: retain()
                except BaseException as retention_error: raise primary from retention_error
                if not isinstance(primary, Exception): raise primary from cleanup_error
            if not isinstance(primary, Exception):
                raise primary
        return copy.deepcopy(journal)


VISUAL_CONFIG = "anyuse.rea-visual-campaign-coordinator/1"
VISUAL_JOURNAL = "anyuse.rea-visual-campaign-journal/1"
VISUAL_CHECKPOINT = "anyuse.rea-public-setup-checkpoint/1"
VISUAL_PREPARED = "anyuse.rea-prepared-visual-case/1"


def _visual_modules():
    from . import rea_visual_campaign as campaign, rea_visual_effect as effect
    return campaign, effect


def _visual_sources():
    from . import model_adapter as models, rea_workflow_restoration as recovery
    campaign, effect = _visual_modules()
    return {**_sources(), "visual_campaign": _pin(Path(campaign.__file__).resolve()),
            "visual_effect": _pin(Path(effect.__file__).resolve()), "model_adapter": _pin(Path(models.__file__).resolve()),
            "workflow_restoration": _pin(Path(recovery.__file__).resolve())}


def _visual_healthy(state):
    _need(state.get("workflow") == "guest_model_judged_state_v1" and state.get("observer_profile") is not None and
          state.get("hypothesis") is not None and state["status"] in
          {"hypothesis", "recording", "candidate", "model_judged", "published"} and
          all(row.get("status") == "completed" for row in state["steps"]),
          "a healthy visual hypothesis is required; failed or uncertain actions never resume")
    _need(state["hypothesis"]["profile"]["policy"] == state["observer_profile"]["policy"],
          "visual hypothesis differs from the caller-frozen observer")


def _visual_configuration(value, state, session):
    value = copy.deepcopy(value)
    required = {"format", "session", "target", "plan_sha256", "observer_profile_sha256", "owned",
                "expected_identity", "resources", "limits", "publication", "replays", "selection_task"}
    _need(type(value) is dict and required <= set(value) <= required | {"storage", "restoration"},
          "closed generic visual coordinator configuration required")
    _need(value["format"] == VISUAL_CONFIG and value["owned"] is True and value["session"] == str(session) and
          value["target"] == state["target"] and value["plan_sha256"] == state["plan_sha256"] and
          value["observer_profile_sha256"] == state["observer_profile_sha256"],
          "visual configuration must bind the exact owned session, target, plan and observer")
    identity = S.validate_identity(value["expected_identity"])
    _need(identity["bundle"] == state["bundle"] and identity["bundle_id"] == state["target"]["bundle_id"] and
          identity["uid"] == os.getuid() == os.geteuid(), "same-user owned visual process required")
    _need(type(value["resources"]) is list and len(value["resources"]) <= 128,
          "bounded pinned caller resources required")
    names = set()
    for resource in value["resources"]:
        _verify(resource, 2 * 1024**3)
        _need(resource["file"] not in names and not Path(resource["file"]).is_relative_to(Path(state["bundle"])),
              "distinct task resources outside the application required")
        names.add(resource["file"])
    _keys(value["limits"], {"seconds", "checkpoint_seconds", "trial_seconds", "max_actions"})
    for name, lo, hi in (("seconds", 30, 7200), ("checkpoint_seconds", 1, 1800),
                         ("trial_seconds", 5, 300), ("max_actions", 1, 256)):
        _need(type(value["limits"][name]) is int and lo <= value["limits"][name] <= hi,
              "finite explicit visual coordinator bounds required")
    campaign, effect = _visual_modules()
    policy = state["hypothesis"]["profile"]["policy"]
    campaign.validate_coverage(state["plan"], policy)
    minimum = 2 * policy["model"]["timeout"] + 2 * effect.V.CAPTURE_TIMEOUT + effect.AFTER_VALIDATION_SECONDS + effect.MIN_DISPATCH_SECONDS
    _need(value["limits"]["trial_seconds"] >= minimum,
          "visual trial budget must cover both judges, captures, dispatch and validation")
    if "restoration" in value:
        from . import rea_workflow_restoration as recovery
        value["restoration"] = recovery.validate_configuration(value["restoration"], state, identity,
                                                                value["limits"]["seconds"])
    _keys(value["publication"], {"summary", "details"})
    _need(G._text(value["publication"]["summary"], 240) and G._text(value["publication"]["details"], 1200) and
          G._text(value["selection_task"], 3000), "bounded publication and selection descriptions required")
    _need(type(value["replays"]) is list and len(value["replays"]) <= 32, "bounded frozen replay cases required")
    ids = {row["id"] for role, *_ in ROLES for row in state["plan"][role]}
    for replay in value["replays"]:
        _keys(replay, {"id", "inputs"}); effect.T._identifier(replay["id"])
        _need(replay["id"] not in ids, "distinct replay case IDs required")
        ids.add(replay["id"]); effect.validate_inputs(policy, replay["inputs"])
    value.setdefault("storage", copy.deepcopy(DEFAULT_STORAGE))
    _keys(value["storage"], DEFAULT_STORAGE)
    _need(all(type(value["storage"][name]) is int and _MIB <= value["storage"][name] <= 1024**4
              for name in DEFAULT_STORAGE) and value["storage"]["action_headroom_bytes"] <= value["storage"]["max_observed_decline_bytes"],
          "finite explicit visual storage bounds required")
    _need(len(_json(value)) <= 256 * 1024, "bounded visual coordinator configuration required")
    return value


def _visual_latest(state):
    _, effect = _visual_modules()
    rows = [(declared["id"], state[store][str(i)]) for role, _, store, _ in ROLES
            for i, declared in enumerate(state["plan"][role]) if str(i) in state.get(store, {})]
    _need(bool(rows), "restart requires an actual measured predecessor")
    identifier, entry = rows[-1]
    case_pin = entry.get("case", entry)
    case = json.loads(_read(case_pin))
    measured = effect.validate_trial(state["hypothesis"]["profile"]["policy"], case, state["hypothesis"]["actor"])
    _need(measured["case_id"] == identifier, "latest actual trial differs from its frozen case")
    return identifier, measured


def _visual_restart(state, entry, old, new, requested, completed):
    entry = C.validate_restart_entry(entry)
    predecessor, measured = _visual_latest(state)
    intent = json.loads(_read(entry["intent"])); receipt = json.loads(_read(entry["receipt"]))
    witness = RR.validate_restart(intent, entry["receipt"])
    baseline = json.loads(_read(receipt["baseline"]))
    _need(entry["predecessor"] == predecessor and witness.get("restart_measurement_validated") is True and
          witness["target"] == state["target"] and witness["old_identity"] == old == measured["identity"] and
          witness["new_identity"] == new and new != old and witness["old_kernel"] == measured["kernel"] and
          measured["finished_monotonic"] < baseline["started_monotonic"] and
          requested <= baseline["started_monotonic"] <= receipt["finished_monotonic"] <= completed,
          "restart must bind the immediate measured predecessor, actual epochs and this request's chronology")
    # A validated restart includes naturally empty ps stderr/absence stdout.
    # Reopen that exact witness closure with the evidence reader; ordinary
    # caller resources and producer pins retain their nonempty admission.
    witness_reader = RR.E._Reader()
    for pin in witness["files"].values():
        witness_reader.read(pin, "restart", maximum=B.MAX_NATIVE_MODULE_BYTES, retain=False)
    witness_reader.recheck()
    return entry


def _visual_next(state, journal, cfg):
    for role, op, store, phase in ROLES:
        index = len(state.get(store, {}))
        if index < len(state["plan"][role]):
            row = state["plan"][role][index]
            return {"kind": op, "role": role, "case": index, "case_id": row["id"], "phase": phase,
                    "inputs": copy.deepcopy(row["inputs"]), "restart": row["restart"]}
        if role == "learning" and state["status"] == "recording":
            return {"kind": "learn"}
    if state["status"] != "published":
        return {"kind": "publish"}
    if journal["selection"] is None:
        return {"kind": "select"}
    if journal["replay_index"] < len(cfg["replays"]):
        return {"kind": "replay", **copy.deepcopy(cfg["replays"][journal["replay_index"]])}
    return None


def _validate_visual_prepared(state, session, request, handoff):
    value = json.loads(_read(handoff.receipt))
    _keys(value, {"format", "session", "session_sha256", "coordinator", "configuration", "sources", "challenge",
                  "checkpoint", "identity", "intent", "restart", "expires_wall"})
    _need(value["format"] == VISUAL_PREPARED and value["session"] == str(session) and
          value["session_sha256"] == _pin(session / "session.json")["sha256"] and
          time.time() < value["expires_wall"], "stale prepared visual session or deadline")
    cfg = json.loads(_read(value["configuration"])); folder = Path(value["coordinator"])
    journal = R.load_json(folder / "coordinator.json")
    _need(state.get("coordinator") == {"out": str(folder), "configuration_sha256": R.digest(cfg)} and
          cfg["format"] == VISUAL_CONFIG and journal["format"] == VISUAL_JOURNAL and journal["status"] == "running" and
          journal["configuration"] == value["configuration"] and journal["sources"] == value["sources"] == _visual_sources() and
          journal["session_sha256"] == value["session_sha256"], "visual prepared coordinator binding differs")
    row = journal["steps"][-1]; challenge = value["challenge"]; planned = challenge["action"]
    _need(row["status"] == "started" and row["kind"] == request["op"] == planned["kind"] and
          row.get("prepared") == handoff.receipt and row["checkpoint"] == value["checkpoint"] and
          row["challenge"] == challenge and "restart" not in request and request["case"] == planned["case"] and
          not any(step.get("prepared_case") == handoff.receipt for step in state["steps"]),
          "prepared visual handoff is forged, reordered or already consumed")
    checkpoint = json.loads(_read(value["checkpoint"]))
    _need(checkpoint["request_sha256"] == R.digest(challenge) and checkpoint["kind"] == "setup" and
          checkpoint["identity"] == value["identity"] == journal["identity"], "prepared setup belongs to another request or epoch")
    for pin in [*value["sources"].values(), *cfg["resources"], *checkpoint["evidence"]]:
        _verify(pin, 2 * 1024**3)
    _need(B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]) == value["identity"],
          "prepared visual process epoch changed")
    _, effect = _visual_modules()
    intent = effect.validate_intent(state["hypothesis"]["profile"]["policy"], value["intent"])
    declared = state["plan"][planned["role"]][planned["case"]]
    _need(declared["id"] == planned["case_id"] == intent["id"] and intent["phase"] == planned["phase"] and
          intent["inputs"] == declared["inputs"] == planned["inputs"] and
          intent["deployment"]["identity"] == value["identity"] and
          challenge["requested_monotonic"] <= checkpoint["started_monotonic"] <= checkpoint["finished_monotonic"] <= intent["observed_monotonic"] and
          declared["restart"] == (value["restart"] is not None), "prepared visual case, inputs, restart or chronology differs")
    if value["restart"] is not None:
        predecessor = _visual_latest(state)[1]
        _visual_restart(state, value["restart"], predecessor["identity"], value["identity"],
                        predecessor["finished_monotonic"], checkpoint["started_monotonic"])
    return {**value, "intent": intent}


def run_visual(session, configuration, out, *, checkpoint=None, resume=False, actions_per_run=256,
               restoration_backend=None):
    """Coordinate a visual proof and reuse through data-only caller checkpoints.

    This function never performs public setup or a restart. It yields one stable
    request for external computer use and consumes its pinned observation once.
    Caller readiness is an assertion; V's actual before/after judgments remain
    authoritative and retain their model_judged tier. No callbacks are loaded.
    """
    from . import model_adapter as models, rea_workflow_restoration as recovery
    R.assert_guest()
    _need(type(actions_per_run) is int and 1 <= actions_per_run <= 256, "bounded visual invocation required")
    session, folder = Path(session).resolve(strict=True), Path(out).resolve()
    _need(not folder.is_relative_to(session) and not session.is_relative_to(folder) and
          not folder.is_relative_to(Path(G._load(session)["bundle"])), "separate visual coordinator output required")
    state, state_hash = _snapshot(session); _visual_healthy(state)
    cfg = _visual_configuration(configuration, state, session)
    recovery_cfg = cfg.get("restoration")
    _need(recovery_cfg is not None or restoration_backend is None, "restoration adapter requires a frozen contract")
    sources = _visual_sources()
    storage_cfg = {"launch_environment": {str(i): str(Path(pin["file"]).parent) for i, pin in enumerate(cfg["resources"])}}
    if recovery_cfg is not None:
        storage_cfg["restoration"] = recovery_cfg
    if not resume:
        _need(not folder.exists() and "coordinator" not in state and checkpoint is None and state["status"] == "hypothesis" and
              not any(state.get(store) for _, _, store, _ in ROLES), "new visual session/output required; no adoption or retry")
        _need(B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]) == cfg["expected_identity"],
              "initial visual process epoch differs")
        folder.mkdir(mode=0o700, parents=True)
        journal = {"format": VISUAL_JOURNAL, "status": "ready", "configuration": _save(folder / "configuration.json", cfg),
            "sources": sources, "session_sha256": state_hash, "identity": cfg["expected_identity"], "steps": [],
            "deadline_wall": min(state["deadline"], time.time() + cfg["limits"]["seconds"]), "pending": None,
            "restart": None, "selection": None, "published": None, "replay_index": 0, "model_calls": 0,
            "coordinator_model_calls": 0, "verification_tier": "model_judged", "automatic_retry": False,
            "setup_assertions_are_proof": False, "error": None, "primary_outcome": "pending",
            "storage": {"binding": _storage_bindings(session, folder, storage_cfg), "last_sample": None,
                        "observed_decline_bytes": {}}}
        if recovery_cfg is not None:
            journal["restoration"] = recovery.initialize(recovery_cfg, backend=restoration_backend)
        journal["storage"]["observed_decline_bytes"] = {str(row["device"]): 0 for row in journal["storage"]["binding"]["filesystems"]}
        with (session / "session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fresh, fresh_hash = _snapshot(session)
            _need(fresh_hash == state_hash and "coordinator" not in fresh, "visual session changed before binding")
            fresh["coordinator"] = {"out": str(folder), "configuration_sha256": R.digest(cfg)}
            R.write_json(session / "session.json", fresh)
        journal["session_sha256"] = _snapshot(session)[1]
        _save(folder / "coordinator.json", journal)
    else:
        journal = R.load_json(folder / "coordinator.json")
    with (folder / "coordinator.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _need(journal == R.load_json(folder / "coordinator.json") and journal["format"] == VISUAL_JOURNAL and
              journal["status"] in {"ready", "needs_setup", "needs_restart", "completed"} and
              all(row["status"] == "completed" for row in journal["steps"]),
              "interrupted, failed or concurrent visual action cannot resume")
        def retain():
            _save(folder / "coordinator.json", journal, replace=True)
        def current(*, identity=True, healthy=True):
            fresh, digest = _snapshot(session)
            if healthy:
                _visual_healthy(fresh)
            _need(digest == journal["session_sha256"] and sources == journal["sources"] == _visual_sources() and
                  json.loads(_read(journal["configuration"])) == cfg and
                  fresh.get("coordinator") == {"out": str(folder), "configuration_sha256": R.digest(cfg)},
                  "visual session, configuration or source drift")
            for pin in [*sources.values(), *cfg["resources"]]:
                _verify(pin, 2 * 1024**3)
            for row in journal["steps"]:
                if row.get("checkpoint"):
                    previous = json.loads(_read(row["checkpoint"]))
                    for pin in previous["evidence"]:
                        _verify(pin, 2 * 1024**3)
            if identity:
                _need(B.process_identity(Path(fresh["bundle"]), fresh["target"]["bundle_id"]) == journal["identity"],
                      "visual process epoch changed without an admitted restart")
            _need(time.time() < journal["deadline_wall"], "original visual campaign deadline expired")
            return fresh
        def storage(*, after_action=False, observation=None):
            account = journal["storage"]
            sample = _storage_sample(account["binding"], session, folder, storage_cfg)
            if observation is not None:
                observation["after"] = sample
            previous = account["last_sample"]
            account["last_sample"] = sample
            if previous is not None:
                _storage_account(previous, sample, cfg["storage"], account["observed_decline_bytes"], action=after_action)
            if not after_action:
                _storage_admit(sample, cfg["storage"], account["observed_decline_bytes"])
        def action(kind, callback, checkpoint_pin=None, challenge=None):
            _need(len(journal["steps"]) < cfg["limits"]["max_actions"], "visual action budget exhausted")
            _need(recovery.remaining_seconds(journal, recovery_cfg, replay=kind == "replay") >= 1,
                  "original visual budget has reached its required cleanup reserve")
            storage(); work = folder / f"a{len(journal['steps']):03d}-{kind}"; work.mkdir(mode=0o700)
            row = {"kind": kind, "status": "started", "session_before_sha256": journal["session_sha256"],
                   "checkpoint": checkpoint_pin, "challenge": challenge, "out": str(work), "result": None,
                   "started_monotonic": time.monotonic(),
                   "storage": {"before": copy.deepcopy(journal["storage"]["last_sample"]), "after": None, "error": None}}
            journal["steps"].append(row); journal.update(status="running", pending=None); retain()
            mark = models.process_call_count()
            failure = None
            try:
                value = callback(work, row)
                actual_calls = models.process_call_count() - mark
                _need(row.get("validated_model_calls", 0) == actual_calls,
                      "actual model calls differ from the retained successful measurement")
                fresh, digest = _snapshot(session); _visual_healthy(fresh)
                row.update(result=_save(work / "result.json", value), session_after_sha256=digest)
                journal["session_sha256"] = digest
            except BaseException as error:
                failure = error
                # G may retain a clean failed trial. Recovery must inspect that
                # actual disposition rather than the prior successful journal.
                journal["session_sha256"] = _snapshot(session)[1]
            finally:
                try:
                    actual_calls = models.process_call_count() - mark
                    _need(type(actual_calls) is int and actual_calls >= 0, "actual coordinator observer-call counter drifted")
                    row["model_calls"] = actual_calls; journal["model_calls"] += actual_calls
                except BaseException as error:
                    failure = failure or error
                try:
                    storage(after_action=True, observation=row["storage"])
                except BaseException as error:
                    row["storage"]["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
                    failure = failure or error
                row["finished_monotonic"] = time.monotonic()
            if failure is not None:
                raise failure
            row["status"] = "completed"; journal["status"] = "ready"; retain()
            return value
        def restore():
            def before():
                storage()
                return copy.deepcopy(journal["storage"]["last_sample"])
            def after():
                storage(after_action=True)
                return copy.deepcopy(journal["storage"]["last_sample"])
            return recovery.restore_once(session, folder, cfg, journal, retain=retain,
                verify_context=lambda: current(healthy=False), backend=restoration_backend,
                storage_before=before, storage_after=after)
        try:
            pending = journal["pending"]
            state = current(identity=not (pending and pending["kind"] == "restart")); storage()
            if journal["status"] == "completed":
                _need(checkpoint is None, "completed campaign cannot consume another checkpoint")
                return copy.deepcopy(journal)
            if checkpoint is None and pending is not None:
                _need(time.time() < pending["expires_wall"], "original visual checkpoint request expired; no replacement request")
                retain()
                return copy.deepcopy(journal)
            if checkpoint is not None:
                _need(pending is not None and time.time() < pending["expires_wall"], "no live visual checkpoint request")
                checkpoint_pin = _pin(Path(checkpoint).resolve(strict=True)); observed = json.loads(_read(checkpoint_pin))
                required = {"format", "request_sha256", "kind", "status", "identity", "evidence", "started_monotonic", "finished_monotonic"}
                _keys(observed, required | ({"restart"} if pending["kind"] == "restart" else set()))
                _need(observed["format"] == VISUAL_CHECKPOINT and observed["status"] == "ready" and
                      observed["kind"] == pending["kind"] and observed["request_sha256"] == R.digest(pending) and
                      not any(row.get("checkpoint") == checkpoint_pin for row in journal["steps"]),
                      "visual checkpoint is stale, repeated or belongs to another request")
                moments = [pending["requested_monotonic"], observed["started_monotonic"], observed["finished_monotonic"], time.monotonic()]
                _need(all(type(v) in {int, float} and math.isfinite(v) for v in moments) and
                      all(a <= b for a, b in zip(moments, moments[1:])), "visual checkpoint chronology differs")
                _need(type(observed["evidence"]) is list and 1 <= len(observed["evidence"]) <= 16,
                      "bounded pinned public observation evidence required")
                for pin in observed["evidence"]:
                    _verify(pin, 64 * 1024**2)
                new_identity = S.validate_identity(observed["identity"])
                _need(B.process_identity(Path(state["bundle"]), state["target"]["bundle_id"]) == new_identity,
                      "checkpoint identity differs from the actual current process")
                if pending["kind"] == "restart":
                    restart = _visual_restart(state, observed["restart"], journal["identity"], new_identity,
                                              pending["requested_monotonic"], observed["finished_monotonic"])
                    def accept_restart(work, row):
                        journal.update(identity=new_identity, restart=restart)
                        return {"restart": restart, "identity": new_identity, "setup_performed_by_coordinator": False}
                    action("restart", accept_restart, checkpoint_pin, pending)
                else:
                    _need(new_identity == journal["identity"], "setup cannot adopt a successor process")
                    planned = pending["action"]
                    _need(planned == _visual_next(state, journal, cfg), "checkpoint no longer names the next frozen case")
                    remaining = recovery.remaining_seconds(journal, recovery_cfg, replay=planned["kind"] == "replay")
                    timeout = min(cfg["limits"]["trial_seconds"], remaining)
                    _need(timeout >= cfg["limits"]["trial_seconds"], "insufficient original budget for a complete visual trial")
                    def trial(work, row):
                        fresh = current(); _, effect = _visual_modules()
                        if planned["kind"] == "replay":
                            cat = json.loads(_read(journal["published"])); selection = json.loads(_read(journal["selection"]))
                            options = ({"restoration": recovery.replay_declaration(recovery_cfg, journal["identity"]),
                                        "restoration_backend": restoration_backend} if recovery_cfg is not None else {})
                            result = A.execute(cat, selection, planned["inputs"], str(work / "replay"),
                                               timeout=timeout, expected_identity=journal["identity"], **options)
                            if recovery_cfg is not None:
                                row["restored_execution"] = copy.deepcopy(result)
                                _need(result.get("completion_status") == "completed" and
                                      result["restoration"]["status"] == "restored",
                                      "fresh replay restoration incomplete; no further replay")
                                result = result["operation"]["result"]
                            _need(result.get("verdict") == result.get("verification_tier") == "model_judged" and
                                  result.get("backend") == "guest_model_judged_state_v1" and
                                  result["case"]["deployment"]["identity"] == journal["identity"],
                                  "replay must retain a passing actual model judgment in the admitted epoch")
                            row["validated_model_calls"] = result["model_calls"]
                            journal["replay_index"] += 1
                            return result
                        intent = effect.make_intent(fresh["bundle"], fresh["hypothesis"]["profile"]["policy"],
                            planned["case_id"], planned["phase"], planned["inputs"], expected_identity=journal["identity"])
                        prepared = {"format": VISUAL_PREPARED, "session": str(session), "session_sha256": journal["session_sha256"],
                            "coordinator": str(folder), "configuration": journal["configuration"], "sources": sources,
                            "challenge": pending, "checkpoint": checkpoint_pin, "identity": journal["identity"], "intent": intent,
                            "restart": journal["restart"], "expires_wall": min(journal["deadline_wall"], pending["expires_wall"])}
                        row["prepared"] = _save(work / "prepared.json", prepared); retain()
                        result = G.step(str(session), {"op": planned["kind"], "case": planned["case"], "timeout": timeout},
                                        prepared_case=PreparedCase(row["prepared"]), expected_runtime_identity=journal["identity"])
                        _save(work / "g-result.json", result)
                        _need(result["feedback"]["status"] == "completed", "G visual gate failed; no retry")
                        measured = _visual_latest(G._load(session))[1]
                        row["validated_model_calls"] = measured["model_calls"]; journal["restart"] = None
                        return result
                    action(planned["kind"], trial, checkpoint_pin, pending)
            count = 1 if checkpoint is not None else 0
            while count < actions_per_run:
                state = current(); planned = _visual_next(state, journal, cfg)
                if planned is None:
                    journal.update(status="completed", primary_outcome="completed"); retain(); return copy.deepcopy(journal)
                if planned["kind"] == "publish":
                    journal["primary_outcome"] = "proof_completed"; retain()
                if planned["kind"] == "publish" and recovery_cfg is not None:
                    restore()
                    _need(journal["restoration"]["status"] == "restored",
                          "required document restoration incomplete; publication refused")
                if planned["kind"] in {"record", "verify", "heldout", "replay"}:
                    kind = "restart" if planned.get("restart") and journal["restart"] is None else "setup"
                    if journal["pending"] is None:
                        journal["pending"] = {"kind": kind, "action": planned, "identity": journal["identity"],
                            "configuration_sha256": R.digest(cfg), "session_sha256": journal["session_sha256"],
                            "sequence": len(journal["steps"]), "requested_monotonic": time.monotonic(),
                            "expires_wall": min(journal["deadline_wall"] -
                                (recovery_cfg["seconds"] if recovery_cfg is not None and
                                 journal["restoration"]["status"] == "pending" else 0),
                                time.time() + cfg["limits"]["checkpoint_seconds"])}
                    _need(journal["pending"]["action"] == planned and time.time() < journal["pending"]["expires_wall"],
                          "original visual checkpoint request expired or drifted; no replacement request")
                    journal["status"] = "needs_" + kind; retain(); return copy.deepcopy(journal)
                def automatic(work, row):
                    if planned["kind"] == "select":
                        cat = json.loads(_read(journal["published"])); view = A.prepare_selection(cat, cfg["selection_task"])
                        _need(len(view["actions"]) == 1, "exact newly published action required")
                        chosen = view["actions"][0]
                        selection = {"format": A.SELECTION, "catalog_sha256": view["catalog_sha256"],
                            "task_sha256": R.digest(cfg["selection_task"]), "choice_view_sha256": R.digest(view),
                            "status": "selected", "action": chosen["id"], "candidate_sha256": chosen["candidate_sha256"],
                            "card_sha256": chosen["card_sha256"], "actuated": False, "model_calls": 0,
                            "selection_source": "explicit_caller_single_published_action"}
                        journal["selection"] = _save(work / "selection.json", selection)
                        _save(work / "choice-view.json", view); return selection
                    request = {"op": planned["kind"]}
                    if planned["kind"] == "publish": request.update(cfg["publication"])
                    result = G.step(str(session), request)
                    _save(work / "g-result.json", result)
                    _need(result["feedback"]["status"] == "completed", "G visual promotion failed; no retry")
                    if planned["kind"] == "publish": journal["published"] = _pin(G._load(session)["published"])
                    return result
                action(planned["kind"], automatic); count += 1
            journal["status"] = "ready"; retain()
        except BaseException as error:
            journal.update(status="failed_no_retry", error={"type": type(error).__name__, "message": str(error)[:2048]})
            if journal["primary_outcome"] == "pending":
                journal["primary_outcome"] = "failed"
            if journal["steps"] and journal["steps"][-1]["status"] == "started":
                journal["steps"][-1]["error"] = copy.deepcopy(journal["error"])
            retain()
            try:
                restore()
            except BaseException as cleanup_error:
                if recovery_cfg is not None:
                    journal["restoration"].setdefault("finalization_error", {
                        "type": type(cleanup_error).__name__, "message": str(cleanup_error)[:2048]})
                    retain()
            if not isinstance(error, Exception): raise
        return copy.deepcopy(journal)


def publication_restoration(state):
    """Return required verified recovery evidence before G creates a catalog.

    Ordinary sessions/catalogs keep their existing shape. The coordinator's
    optional contract cannot be bypassed by calling G.publish directly.
    """
    binding = state.get("coordinator")
    if binding is None:
        return None
    journal = R.load_json(Path(binding["out"]) / "coordinator.json")
    cfg = json.loads(_read(journal["configuration"]))
    _need(R.digest(cfg) == binding["configuration_sha256"], "coordinator publication configuration drift")
    if "restoration" not in cfg:
        return None
    recovery = journal.get("restoration", {})
    _need(journal["status"] == "running" and recovery.get("status") == "restored" and
          recovery.get("checkpoint") == cfg["restoration"]["checkpoint"],
          "required restoration must finish before catalog publication")
    D.verify_restored(recovery["checkpoint"], recovery["result"])
    return {"checkpoint": recovery["checkpoint"], "receipt": recovery["result"]}
