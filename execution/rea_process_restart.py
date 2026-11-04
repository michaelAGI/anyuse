"""One ordinary app-process restart witness, inside the VM only.

Callbacks are trusted authorized code, never JSON programs. Their file pins
are provenance, not authentication. Launch completion must identify its actual
application PID; a successful `open` exit code cannot identify the new app.
Only independent NS/kernel observations establish the retained process epochs.
No attachment, forced app termination, automatic app cleanup or VM restart.
Callback/NS deadlines are cooperative; owned ps cleanup has separate bounds.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_ax_state as S
from . import rea_state_evidence as E

INTENT = "anyuse.rea-process-restart-intent/1"
BASELINE = "anyuse.rea-process-restart-baseline/1"
RECEIPT = "anyuse.rea-process-restart-receipt/1"
OBSERVATION = "anyuse.rea-process-restart-observation/1"
QUERY = "anyuse.rea-process-restart-kernel-query/1"
CALLBACK = "anyuse.rea-process-restart-callback/1"
CLEANUP = "anyuse.rea-process-restart-owned-cleanup/1"
REPORT = "anyuse.rea-process-restart-measurement/1"
STAGES = ("before", "absence", "prelaunch", "new", "final")
MAX_SAMPLES, INTERVAL = 24, .2
PS = Path("/bin/ps")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, names):
    _need(type(value) is dict and set(value) == set(names), "closed restart evidence schema required")


def _same(a, b): return S._same(a, b)


def _error(error): return {"type": type(error).__name__, "message": str(error)[:2048]}


def _moment(value):
    _need(type(value) in (int, float) and math.isfinite(value) and value >= 0, "finite restart time required")
    return value


def _pin(path):
    path = Path(path).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.lstat().st_size}


def _save(path, body):
    raw = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    _need(len(raw) <= R.MAX_JSON_BYTES, "bounded restart journal required")
    pending = path.with_name("." + path.name + ".pending")
    with os.fdopen(os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    return _pin(path)


def validate_intent(intent):
    intent = copy.deepcopy(intent)
    _keys(intent, {"format", "id", "target", "old_identity", "stop", "launch"})
    E.T._identifier(intent["id"])
    _need(intent["format"] == INTENT, "explicit app-process restart intent required")
    intent["target"] = S.validate_target(intent["target"])
    intent["old_identity"] = S.validate_identity(intent["old_identity"])
    _need(intent["old_identity"]["bundle_id"] == intent["target"]["bundle_id"], "restart target and old identity differ")
    for phase in ("stop", "launch"):
        _keys(intent[phase], {"id", "producer"})
        E.T._identifier(intent[phase]["id"])
        intent[phase]["producer"] = E.T._pin(intent[phase]["producer"])
    return intent


def _sources():
    # The fixed producer/validator/readers actually used by this boundary,
    # plus the executed read-only OS tool. This is not whole-OS attestation.
    modules = (R, B, S, E, E.P, E.T, B.NL, B.Q)
    paths = {Path(__file__).resolve(strict=True), PS.resolve(strict=True)}
    paths.update(Path(module.__file__).resolve(strict=True) for module in modules)
    return {str(path): _pin(path) for path in sorted(paths)}


_LEGACY_SOURCE_PROFILE = {'execution/rea_ax_state.py': {'bytes': 43905, 'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'}, 'execution/rea_mapping.py': {'bytes': 34014, 'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'}, 'execution/rea_native_lifecycle.py': {'bytes': 17451, 'sha256': '8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac'}, 'execution/rea_native_path.py': {'bytes': 38018, 'sha256': 'e3f549d92c76cfe7e7470c0318e57d47cab103eea259f5220c54c9c5f18de3a9'}, 'execution/rea_process_restart.py': {'bytes': 45230, 'sha256': 'bb57a8a0a8731559c9bfa238e70ad7522edd6c0ca85c926f97564c812718cf1f'}, 'execution/rea_qt_moc.py': {'bytes': 34652, 'sha256': 'f16e7461dee7fc2c684258013aa642cb32d4416bb452d79e7574d5f75d85ddbd'}, 'execution/rea_runtime_bridge.py': {'bytes': 223039, 'sha256': '75c2bc7ba42172c7ca38f5729ecdfa4c6cd0b03f52015cc10c2bae064224e793'}, 'execution/rea_state_evidence.py': {'bytes': 35445, 'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'}, 'execution/rea_state_task.py': {'bytes': 13294, 'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}
_VALIDATOR_ROLE = "execution/rea_process_restart.py"
_SOURCE_FAMILY = "restart"
_SOURCE_DOMAIN = "source"
_SOURCE_MAXIMUM = 64 * 1024**2


# One reviewed forward migration; every producer role and installed core byte is fixed.
_REVIEWED_EXECUTION_RESTORATION_PAIR = {'installed_core': {'execution/rea_ax_state.py': {'bytes': 43905,
                                                  'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
                    'execution/rea_mapping.py': {'bytes': 35219,
                                                 'sha256': '7c30e11b8b14f924c2f24030e537be1a53d6ca4981bcc0b2119eb9987820db0f'},
                    'execution/rea_native_lifecycle.py': {'bytes': 17451,
                                                          'sha256': '8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac'},
                    'execution/rea_native_path.py': {'bytes': 38018,
                                                     'sha256': 'e3f549d92c76cfe7e7470c0318e57d47cab103eea259f5220c54c9c5f18de3a9'},
                    'execution/rea_qt_moc.py': {'bytes': 34652,
                                                'sha256': 'f16e7461dee7fc2c684258013aa642cb32d4416bb452d79e7574d5f75d85ddbd'},
                    'execution/rea_runtime_bridge.py': {'bytes': 223984,
                                                        'sha256': '2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485'},
                    'execution/rea_state_evidence.py': {'bytes': 35445,
                                                        'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
                    'execution/rea_state_task.py': {'bytes': 13294,
                                                    'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}},
 'producer': {'execution/rea_ax_state.py': {'bytes': 43905,
                                            'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
              'execution/rea_mapping.py': {'bytes': 34014,
                                           'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'},
              'execution/rea_native_lifecycle.py': {'bytes': 17451,
                                                    'sha256': '8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac'},
              'execution/rea_native_path.py': {'bytes': 38018,
                                               'sha256': 'e3f549d92c76cfe7e7470c0318e57d47cab103eea259f5220c54c9c5f18de3a9'},
              'execution/rea_process_restart.py': {'bytes': 49447,
                                                   'sha256': 'c9c52c46f157b1496aafb940e0cf52235bd85f5e7d4b5c4f6881e98643a9a6d5'},
              'execution/rea_qt_moc.py': {'bytes': 34652,
                                          'sha256': 'f16e7461dee7fc2c684258013aa642cb32d4416bb452d79e7574d5f75d85ddbd'},
              'execution/rea_runtime_bridge.py': {'bytes': 223984,
                                                  'sha256': '2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485'},
              'execution/rea_state_evidence.py': {'bytes': 35445,
                                                  'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
              'execution/rea_state_task.py': {'bytes': 13294,
                                              'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}}


# Separately reviewed historical closure; never a live runtime adapter authorization.
_REVIEWED_V19_SOURCE_PROFILE = {'execution/rea_ax_state.py': {'bytes': 43905,
                               'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
 'execution/rea_mapping.py': {'bytes': 34014,
                              'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'},
 'execution/rea_native_lifecycle.py': {'bytes': 17451,
                                       'sha256': '8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac'},
 'execution/rea_native_path.py': {'bytes': 38018,
                                  'sha256': 'e3f549d92c76cfe7e7470c0318e57d47cab103eea259f5220c54c9c5f18de3a9'},
 'execution/rea_process_restart.py': {'bytes': 49447,
                                      'sha256': 'c9c52c46f157b1496aafb940e0cf52235bd85f5e7d4b5c4f6881e98643a9a6d5'},
 'execution/rea_qt_moc.py': {'bytes': 34652,
                             'sha256': 'f16e7461dee7fc2c684258013aa642cb32d4416bb452d79e7574d5f75d85ddbd'},
 'execution/rea_runtime_bridge.py': {'bytes': 223039,
                                     'sha256': '75c2bc7ba42172c7ca38f5729ecdfa4c6cd0b03f52015cc10c2bae064224e793'},
 'execution/rea_state_evidence.py': {'bytes': 35445,
                                     'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
 'execution/rea_state_task.py': {'bytes': 13294,
                                 'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}


# R's visual backend registration leaves restart/native semantics unchanged.
# Match this independently reviewed complete core, never per-role alternatives.
_REVIEWED_VISUAL_VERIFICATION_CORE = {
    "execution/rea_ax_state.py": {"bytes": 43905, "sha256": "60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f"},
    "execution/rea_mapping.py": {"bytes": 35270, "sha256": "61a6dd1b9362f7cd98c56661dddbe9e3d637cc5a3bbd7b77f7adbd2cce282bb5"},
    "execution/rea_native_lifecycle.py": {"bytes": 17451, "sha256": "8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac"},
    "execution/rea_native_path.py": {"bytes": 38018, "sha256": "e3f549d92c76cfe7e7470c0318e57d47cab103eea259f5220c54c9c5f18de3a9"},
    "execution/rea_qt_moc.py": {"bytes": 34652, "sha256": "f16e7461dee7fc2c684258013aa642cb32d4416bb452d79e7574d5f75d85ddbd"},
    "execution/rea_runtime_bridge.py": {"bytes": 223984, "sha256": "2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485"},
    "execution/rea_state_evidence.py": {"bytes": 35445, "sha256": "1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce"},
    "execution/rea_state_task.py": {"bytes": 13294, "sha256": "c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6"},
}


# Reviewed e314b3c..c8b433d: B/NP/Q add declared-native resources and borrowed
# Qt receivers. This boundary uses neither: process identity, retained epochs,
# callback checks and owned-query cleanup are unchanged. Keep this complete
# installed core separate from both earlier maps; no per-role substitutions.
_REVIEWED_GENERIC_WORKFLOW_CORE = {
    "execution/rea_ax_state.py": {"bytes": 43905, "sha256": "60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f"},
    "execution/rea_mapping.py": {"bytes": 35270, "sha256": "61a6dd1b9362f7cd98c56661dddbe9e3d637cc5a3bbd7b77f7adbd2cce282bb5"},
    "execution/rea_native_lifecycle.py": {"bytes": 17451, "sha256": "8ba0ad5f75babe735761d3e9eab523843b88bd81407b34af4a9a9beb720d52ac"},
    "execution/rea_native_path.py": {"bytes": 38049, "sha256": "36cd5d96758637ef80507ca92df8fe12b80c94ca26fbfbad42d8e6838f48b6a4"},
    "execution/rea_qt_moc.py": {"bytes": 35587, "sha256": "049a70829699033b676aa68d2949d60269798130f453237cd9e77fdca26a0f38"},
    "execution/rea_runtime_bridge.py": {"bytes": 225263, "sha256": "08d7a1e366351fc9f2b8cfaaae572bc4bb3b5213b79adfd42293ca1dddbec068"},
    "execution/rea_state_evidence.py": {"bytes": 35445, "sha256": "1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce"},
    "execution/rea_state_task.py": {"bytes": 13294, "sha256": "c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6"},
}


def _same_source(left, right): return S._same(left, right)


def _reviewed_core_variant(trusted):
    core = {role: pin for role, pin in trusted.items() if role != _VALIDATOR_ROLE}
    if _same_source(core, _REVIEWED_EXECUTION_RESTORATION_PAIR["installed_core"]):
        return "execution_restoration"
    if _same_source(core, _REVIEWED_VISUAL_VERIFICATION_CORE):
        return "visual_verification"
    if _same_source(core, _REVIEWED_GENERIC_WORKFLOW_CORE):
        return "generic_workflow"
    return None


def _producer_profile(pins):
    """Normalize only the fixed Python/JS roles, never import supplied paths."""
    _need(type(pins) is dict and len(pins) == len(_LEGACY_SOURCE_PROFILE),
          "exact fixed producer source roles required")
    profile, roots = {}, set()
    for name, raw in pins.items():
        pin = E.T._pin(raw)
        _need(name == pin["file"], "producer source key differs from file pin")
        path = Path(name)
        role = "execution/" + path.name
        _need(path.parent.name == "execution" and role in _LEGACY_SOURCE_PROFILE and role not in profile,
              "unknown or duplicate producer source role")
        roots.add(str(path.parent.parent))
        profile[role] = {key: pin[key] for key in ("bytes", "sha256")}
    _need(set(profile) == set(_LEGACY_SOURCE_PROFILE) and len(roots) == 1,
          "producer sources need every fixed role under one package root")
    return profile, next(iter(roots))


def _admit_sources(reader, pins, current, *, tool=None):
    """Exact current bytes or fixed reviewed producer/core pairs; no old code runs."""
    _need(type(pins) is dict and type(current) is dict, "closed producer source maps required")
    original, installed = copy.deepcopy(pins), copy.deepcopy(current)
    if tool is not None:
        _need(tool == "/bin/ps" and tool in original and tool in installed and
              _same_source(original[tool], installed[tool]),
              "restart OS tool must match current canonical /bin/ps exactly")
        original.pop(tool); installed.pop(tool)
    profile, source_root = _producer_profile(original)
    trusted, _ = _producer_profile(installed)
    core_variant = _reviewed_core_variant(trusted)
    reviewed_profile = None
    if _same_source(profile, trusted):
        admission = "current_exact_roles"
    elif (_same_source(profile, _REVIEWED_EXECUTION_RESTORATION_PAIR["producer"]) and core_variant is not None):
        admission = "reviewed_b2_execution_restoration_exact_core"
        reviewed_profile = _SOURCE_FAMILY + "/reviewed-b2-execution-restoration"
    elif (_same_source(profile, _REVIEWED_V19_SOURCE_PROFILE) and core_variant is not None):
        admission = "reviewed_v19_restart_exact_core"
        reviewed_profile = _SOURCE_FAMILY + "/reviewed-v19-history"
    else:
        _need(_same_source(profile, _LEGACY_SOURCE_PROFILE) and all(
            _same_source(trusted[role], expected) for role, expected in _LEGACY_SOURCE_PROFILE.items()
            if role != _VALIDATOR_ROLE),
            "producer source profile is not current or reviewed compatible legacy")
        admission = "reviewed_v1_exact_core"
        reviewed_profile = _SOURCE_FAMILY + "/reviewed-producer-v1"
    if reviewed_profile is not None and core_variant == "visual_verification":
        reviewed_profile += "/visual-verification-core"
    if reviewed_profile is not None and core_variant == "generic_workflow":
        reviewed_profile += "/generic-workflow-core"
    # The current validator and retained producer are separate identities. Both
    # are bounded read-only resources rechecked by the enclosing validation.
    for collection in (current, pins):
        for pin in collection.values():
            reader.read(pin, _SOURCE_DOMAIN, maximum=_SOURCE_MAXIMUM, retain=False)
    return {"format": "anyuse.rea-source-closure-admission/1", "family": _SOURCE_FAMILY,
            "admission": admission, "producer_root": source_root,
            "producer_profile_sha256": R.digest(profile),
            "producer_sources": copy.deepcopy(pins), "validator_sources": copy.deepcopy(current),
            "legacy_profile": reviewed_profile,
            "old_sources_imported": False}


def _resources(reader, intent, sources):
    admission = _admit_sources(reader, sources, _sources(), tool=str(PS))
    for phase in ("stop", "launch"): reader.read(intent[phase]["producer"], "producer", retain=False)
    return admission


def _argv(pid): return [str(PS), "-o", "pid=,ppid=,uid=,lstart=,comm=", "-p", str(pid)]


def _kernel_start(text):
    """Decode the fixed C/UTC five-word lstart without changing host locale.

    The text represents exactly one whole-second interval. No empirically
    unmeasured tolerance or timezone offset is accepted.
    """
    parts = text.split() if type(text) is str else []
    _need(len(parts) == 5 and parts[0] in _WEEKDAYS and parts[1] in _MONTHS and
          parts[2].isascii() and parts[2].isdecimal() and 1 <= len(parts[2]) <= 2 and
          len(parts[4]) == 4 and parts[4].isascii() and parts[4].isdecimal(),
          "exact C/UTC five-word kernel start required")
    clock = parts[3].split(":")
    _need(len(clock) == 3 and all(len(value) == 2 and value.isascii() and value.isdecimal() for value in clock),
          "exact C/UTC kernel start clock required")
    try:
        value = datetime(int(parts[4]), _MONTHS.index(parts[1]) + 1, int(parts[2]),
            *(int(field) for field in clock), tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError("valid C/UTC kernel start date required") from error
    _need(_WEEKDAYS[value.weekday()] == parts[0] and value.timestamp() > 0,
          "kernel start weekday or epoch differs")
    return value.timestamp()


def _bind_epoch(kernel, launched):
    start = _kernel_start(kernel["started_at"])
    _need(start <= launched < start + 1, "NS launch timestamp is outside the kernel start's exact one-second interval")


def _ps_query(pid, folder, deadline):
    """Retain raw fixed ps output and the owned query's actual disposition."""
    folder.mkdir(mode=0o700)
    started = time.monotonic()
    command = _argv(pid)
    owned = {"pid": None, "returncode": None, "reaped": False, "term_attempted": False,
             "kill_attempted": False, "errors": []}
    row = {"format": QUERY, "command": command, "started_monotonic": started,
           "finished_monotonic": None, "timeout_seconds": min(2, deadline - started),
           "status": "running", "owned": owned, "stdout": None, "stderr": None, "error": None}
    _save(folder / "receipt.json", row)
    child = interrupted = receipt_pin = None
    try:
        _need(row["timeout_seconds"] > 0, "kernel observation deadline expired")
        with (folder / "stdout").open("xb") as stdout, (folder / "stderr").open("xb") as stderr:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, env=dict(os.environ, LC_ALL="C", LANG="C", TZ="UTC"))
            owned["pid"] = child.pid
            _save(folder / "receipt.json", row)
            child.wait(timeout=row["timeout_seconds"])
            owned.update(returncode=child.returncode, reaped=True)
        _need(child.returncode in (0, 1), "kernel observation failed")
        row["status"] = "returned"
    except BaseException as error:
        row.update(status="failed", error=_error(error))
        if not isinstance(error, Exception): interrupted = error
    finally:
        if child is not None and not owned["reaped"]:
            try:
                if child.poll() is None:
                    owned["term_attempted"] = True
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        owned["kill_attempted"] = True
                        try: os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        child.wait(timeout=1)
                else: child.wait(timeout=1)
                owned.update(returncode=child.returncode, reaped=True)
            except BaseException as error:
                owned["errors"].append(_error(error))
                if not isinstance(error, Exception) and interrupted is None: interrupted = error
        for name in ("stdout", "stderr"):
            try:
                if (folder / name).exists(): row[name] = _pin(folder / name)
            except BaseException as error:
                owned["errors"].append(_error(error))
                if not isinstance(error, Exception) and interrupted is None: interrupted = error
        row["finished_monotonic"] = time.monotonic()
        try: receipt_pin = _save(folder / "receipt.json", row)
        except BaseException as error:
            owned["errors"].append(_error(error))
            row.update(status="failed", error=row["error"] or _error(error))
            if not isinstance(error, Exception) and interrupted is None: interrupted = error
            # Preserve the actual completed cleanup in an independent failure
            # artifact. It cannot pass the successful canonical query path.
            try: receipt_pin = _save(folder / "preservation-failure.json", row)
            except BaseException as error:
                owned["errors"].append(_error(error))
                if not isinstance(error, Exception) and interrupted is None: interrupted = error
    if interrupted is not None: raise interrupted
    _need(receipt_pin is not None, "actual kernel-query disposition could not be preserved")
    return row, receipt_pin


def _kernel(query, reader, pid, *, allow_exiting=False):
    """Pure interpretation of the retained original ps bytes/disposition."""
    _keys(query, {"format", "command", "started_monotonic", "finished_monotonic", "timeout_seconds",
                  "status", "owned", "stdout", "stderr", "error"})
    owned = query["owned"]
    _keys(owned, {"pid", "returncode", "reaped", "term_attempted", "kill_attempted", "errors"})
    _need(query["format"] == QUERY and _same(query["command"], _argv(pid)) and query["status"] == "returned" and
          query["error"] is None and type(owned["pid"]) is int and owned["pid"] > 1 and
          type(owned["returncode"]) is int and owned["returncode"] in (0, 1) and owned["reaped"] is True and
          owned["term_attempted"] is False and owned["kill_attempted"] is False and _same(owned["errors"], []),
          "actual naturally reaped read-only kernel query required")
    start, end = _moment(query["started_monotonic"]), _moment(query["finished_monotonic"])
    _need(start <= end and type(query["timeout_seconds"]) in (int, float) and
          0 < query["timeout_seconds"] <= 2 and end - start <= query["timeout_seconds"], "kernel query timing differs")
    stdout = reader.read(query["stdout"], "trial", maximum=8192)
    stderr = reader.read(query["stderr"], "trial", maximum=1024)
    _need(stderr == b"", "kernel observation emitted a diagnostic")
    if owned["returncode"] == 1:
        _need(stdout.strip() == b"", "kernel absence cannot carry a process row")
        return None
    lines = stdout.decode("utf-8").splitlines()
    fields = lines[0].split(None, 8) if len(lines) == 1 else []
    _need(len(fields) == 9 and all(field.isdecimal() for field in fields[:3]) and int(fields[0]) == pid,
          "complete exact one-PID kernel record required")
    started_at = " ".join(fields[3:8])
    _kernel_start(started_at)
    kernel = {"pid": pid, "ppid": int(fields[1]), "uid": int(fields[2]), "started_at": started_at}
    executable = fields[8]
    if allow_exiting and executable.startswith("(") and executable.endswith(")") and len(executable) > 2 and \
            all(char not in "/()" and 32 <= ord(char) != 127 for char in executable[1:-1]):
        # macOS ps may retain only parenthesized comm while an app exits.
        # Preserve it literally; it is presence evidence, never an executable
        # path or a complete process identity. The old epoch is joined below.
        return {**kernel, "exiting_comm": executable}
    _need(executable.startswith("/") and str(Path(executable)) == executable and ".." not in Path(executable).parts,
          "canonical observed kernel executable required")
    return {**kernel, "executable": executable}


class _Mac:
    """Same read-only NS/ps protocol as S.context and inspector lifecycle.

    Unlike S.context, a restart observer must retain absence and partial startup
    metadata. No AX, focus, attachment or private object access is performed.
    """
    def __init__(self):
        import AppKit
        import Foundation
        self.K, self.F = AppKit, Foundation

    def registrations(self, bundle_id):
        apps = list(self.K.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
        _need(len(apps) <= 64, "bounded NS application inventory required")
        rows = []
        for app in apps:
            bundle, executable, launched = app.bundleURL(), app.executableURL(), app.launchDate()
            rows.append({"pid": int(app.processIdentifier()), "terminated": bool(app.isTerminated()),
                "bundle_id": str(app.bundleIdentifier()) if app.bundleIdentifier() is not None else None,
                "bundle": str(Path(str(bundle.path())).resolve(strict=True)) if bundle is not None else None,
                "executable": str(Path(str(executable.path())).resolve(strict=True)) if executable is not None else None,
                "launch_time": float(launched.timeIntervalSince1970()) if launched is not None else None})
        return rows

    def query(self, pid, folder, deadline): return _ps_query(pid, folder, deadline)

    def tick(self, deadline):
        _need(time.monotonic() < deadline, "restart observation deadline expired")
        self.F.NSRunLoop.currentRunLoop().runUntilDate_(self.F.NSDate.dateWithTimeIntervalSinceNow_(min(INTERVAL, deadline - time.monotonic())))


def _ns_identity(rows, old):
    _need(type(rows) is list and len(rows) <= 1, "ambiguous NS app registration; no successor selection")
    if not rows: return None
    row = rows[0]
    _keys(row, {"pid", "terminated", "bundle_id", "bundle", "executable", "launch_time"})
    _need(type(row["pid"]) is int and 1 <= row["pid"] < 2**31 and type(row["terminated"]) is bool,
          "typed original NS registration required")
    _need(row["launch_time"] is None or type(row["launch_time"]) in (int, float) and
          0 < row["launch_time"] < 253402300800 and math.isfinite(row["launch_time"]),
          "typed complete or unavailable NS launch timestamp required")
    for key in ("bundle_id", "bundle", "executable"):
        _need(row[key] is None or row[key] == old[key], "NS registration belongs to another target")
    if row["terminated"] or any(row[key] is None for key in ("bundle_id", "bundle", "executable", "launch_time")):
        return None
    return S.validate_identity({key: row[key] for key in ("bundle_id", "bundle", "executable", "pid", "launch_time")} | {"uid": old["uid"]})


def _identity(rows, kernel, old):
    identity = _ns_identity(rows, old)
    if identity is None or kernel is None: return None
    if "exiting_comm" in kernel: return None
    _need(kernel["pid"] == identity["pid"] and kernel["uid"] == old["uid"] and kernel["executable"] == old["executable"],
          "independent NS/kernel target ownership differs")
    _bind_epoch(kernel, identity["launch_time"])
    return identity


def _observe(backend, stage, index, pid, old, folder, deadline):
    path = folder / (stage + "-%02d.json" % index)
    row = {"format": OBSERVATION, "stage": stage, "index": index, "queried_pid": pid,
           "started_monotonic": time.monotonic(), "finished_monotonic": None,
           "started_wall": time.time(), "finished_wall": None, "registrations": None,
           "query": None, "identity": None, "kernel": None, "status": "running", "error": None}
    _save(path, row)
    interrupted = None
    try:
        _need(row["started_monotonic"] < deadline, "restart observation expired")
        row["registrations"] = backend.registrations(old["bundle_id"])
        query, row["query"] = backend.query(pid, folder / (stage + "-%02d-ps" % index), deadline)
        reader = E._Reader()
        row["kernel"] = _kernel(query, reader, pid, allow_exiting=stage in {"absence", "prelaunch"})
        row["identity"] = _identity(row["registrations"], row["kernel"], old)
        registration = _ns_identity(row["registrations"], old)
        _need(registration is None or registration["launch_time"] <= time.time(), "observed NS launch timestamp lies in the future")
        row["status"] = "observed"
    except BaseException as error:
        row.update(status="failed", error=_error(error))
        if not isinstance(error, Exception): interrupted = error
    finally:
        # A query interruption may occur after the query saved its journal but
        # before it returned a pin. Retain that real owned cleanup disposition.
        try:
            query_folder = folder / (stage + "-%02d-ps" % index)
            query_path = query_folder / "preservation-failure.json"
            if not query_path.exists(): query_path = query_folder / "receipt.json"
            if row["query"] is None and query_path.exists(): row["query"] = _pin(query_path)
        except BaseException as error:
            row.update(status="failed", error=row["error"] or _error(error))
            if not isinstance(error, Exception) and interrupted is None: interrupted = error
        row.update(finished_monotonic=time.monotonic(), finished_wall=time.time())
        try: _save(path, row)
        except BaseException:
            if interrupted is not None: raise interrupted
            raise
    if interrupted is not None: raise interrupted
    return row, _pin(path)


def _old_or_absent(row, old, kernel):
    observed = row["kernel"]
    if observed is not None:
        if "exiting_comm" in observed:
            _need(set(observed) == {"pid", "ppid", "uid", "started_at", "exiting_comm"} and
                  all(_same(observed[key], kernel[key]) for key in ("pid", "ppid", "uid", "started_at")) and
                  observed["exiting_comm"] == "(" + Path(old["executable"]).name + ")",
                  "exiting kernel comm does not preserve the exact old process owner and epoch")
        else: _need(_same(observed, kernel), "old kernel PID was replaced before witnessed absence")
    for registration in row["registrations"]:
        _need(all(_same(registration[key], old[key]) for key in ("pid", "bundle_id", "bundle", "executable", "launch_time")),
              "an app appeared before the owned launch; no successor adoption")
    return row["kernel"] is None and row["registrations"] == []


def _launch_return(value):
    _need(type(value) is dict and set(value) == {"application_pid", "result"},
          "normal launch completion did not identify its application PID; provenance uncertain")
    _need(type(value["application_pid"]) is int and 1 <= value["application_pid"] < 2**31,
          "normal launch completion did not identify its application PID; provenance uncertain")
    return value["application_pid"]


def _new(row, old, pid, launch):
    _need(all(item["pid"] == pid for item in row["registrations"]), "NS successor differs from the normal launch completion PID")
    kernel, identity = row["kernel"], row["identity"]
    if kernel is not None:
        _need(kernel["uid"] == old["uid"] and kernel["executable"] == old["executable"], "launch PID belongs to another kernel process")
        start = _kernel_start(kernel["started_at"])
        _need(start <= launch["finished_wall"] and launch["entry_check_wall"] < start + 1,
              "kernel start interval does not overlap the actual launch callback")
    registration = _ns_identity(row["registrations"], old)
    if registration is not None:
        _need(registration["pid"] == pid and registration["launch_time"] > old["launch_time"] and
              launch["entry_check_wall"] <= registration["launch_time"] <= launch["finished_wall"] and
              registration["launch_time"] <= row["finished_wall"],
              "new app lacks its strictly later actual callback-bound NS launch epoch")
    return identity


def _hook(phase, callback, context, folder, deadline):
    path = folder / (phase + ".json")
    callback_context = copy.deepcopy(context)
    row = {"format": CALLBACK, "phase": phase, "attempts": None, "issuance": "unknown", "status": "prepared",
           "started_monotonic": time.monotonic(), "started_wall": time.time(), "deadline_monotonic": deadline,
           "entry_check_monotonic": None, "entry_check_wall": None,
           "finished_monotonic": None, "finished_wall": None, "return": None, "error": None}
    # A durable prepared record does not claim either issuance or zero calls.
    # A hard interruption before the final journal leaves that fact unknown.
    _save(path, row)
    interrupted = None
    attempted = False
    try:
        row["entry_check_wall"] = time.time()
        row["entry_check_monotonic"] = time.monotonic()
        _need(row["entry_check_monotonic"] < deadline, "ordinary callback deadline expired before entry; not issued")
        row.update(attempts=1, issuance="attempted", status="attempted")
        attempted = True
        value = callback(callback_context)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        _need(len(raw) <= 32 * 1024 and time.monotonic() < deadline, "callback return exceeded its cooperative bound")
        row["return"] = _save(folder / (phase + "-return.json"), value)
        row["status"] = "returned"
    except BaseException as error:
        row.update(status="failed" if attempted else "not_issued", error=_error(error))
        if not attempted: row.update(attempts=0, issuance="not_issued")
        if not isinstance(error, Exception): interrupted = error
    finally:
        row.update(finished_monotonic=time.monotonic(), finished_wall=time.time())
        try: _save(path, row)
        except BaseException:
            if interrupted is not None: raise interrupted
            raise
    if interrupted is not None: raise interrupted
    _need(row["status"] == "returned", "ordinary " + phase + " callback failed; outcome retained without retry")
    return value, row, _pin(path)


def run_restart(bundle, expected_identity, intent, out, *, stop_hook, launch_hook, timeout=30):
    """One trusted stop and one launch; callbacks own their ordinary API code.

    Callbacks must be authorized, bounded, retain their ordinary API result,
    and raise on refusal/uncertainty. launch_hook returns application_pid from
    its normal completion application, not an app found by scanning afterward.
    Callback child cleanup is their trusted producer's responsibility. This
    boundary owns only its short ps queries and never cleans up the app.
    """
    R.assert_guest()
    intent, old = validate_intent(intent), S.validate_identity(copy.deepcopy(expected_identity))
    _need(callable(stop_hook) and callable(launch_hook), "trusted authorized stop and launch callbacks required")
    _need(type(timeout) is int and 1 <= timeout <= 60 and old["uid"] == os.getuid() == os.geteuid() and
          _same(old, intent["old_identity"]), "exact same-user frozen restart identity and bounded timeout required")
    app, root = Path(bundle).resolve(strict=True), Path(str(out))
    _need(str(app) == old["bundle"] and _same(R.fingerprint(app), intent["target"]), "unchanged exact target required")
    _need(_same(B.process_identity(app, old["bundle_id"]), old), "old B identity changed; no successor adoption")
    reader, sources = E._Reader(), _sources()
    _resources(reader, intent, sources)
    _need(root.is_absolute() and str(root) == str(out) and ".." not in root.parts and
          root.parent.resolve(strict=True) == root.parent and not root.is_relative_to(app), "fresh canonical evidence folder outside the app required")
    root.mkdir(mode=0o700)
    intent_pin = _save(root / "intent.json", intent)
    started, wall = time.monotonic(), time.time()
    deadline = started + timeout
    baseline_pin = _save(root / "baseline.json", {"format": BASELINE, "intent_sha256": R.digest(intent),
        "sources": sources, "timeout_seconds": timeout, "started_monotonic": started, "started_wall": wall,
        "deadline_monotonic": deadline, "max_samples": MAX_SAMPLES, "interval_seconds": INTERVAL})
    result = {"format": RECEIPT, "status": "running", "intent": intent_pin, "baseline": baseline_pin,
        "callbacks": {"stop": None, "launch": None}, "observations": {stage: [] for stage in STAGES},
        "owned_cleanup": None, "new_identity": None, "target_after": None, "source_unchanged": False,
        "errors": [], "preservation_errors": [], "finished_monotonic": None, "finished_wall": None, "proof_created": False,
        "task_proof_created": False, "public_proof_created": False, "card_created": False}
    receipt = root / "receipt.json"
    _save(receipt, result)
    query_pins, interrupted, receipt_pin, preservation_pin = [], None, None, None
    def preservation_error(phase, error):
        nonlocal interrupted
        result["status"] = "failed"
        result["preservation_errors"].append({"phase": phase, "error": _error(error)})
        if not isinstance(error, Exception) and interrupted is None: interrupted = error
    def retain_final_receipt():
        nonlocal receipt_pin, preservation_pin
        receipt_pin = None
        try: receipt_pin = _save(receipt, result)
        except BaseException as error:
            preservation_error("final_receipt", error)
            # A previous pin may now be stale. Only the independent failure
            # artifact, if available, describes this finalization attempt.
            try: preservation_pin = _save(root / "finalization-failure.json", result)
            except BaseException as error: preservation_error("finalization_journal", error)
    def failed():
        return {"receipt": receipt_pin, "measurement": None, "status": "failed", "preservation": preservation_pin,
                "preservation_errors": copy.deepcopy(result["preservation_errors"]), "proof_created": False}
    def observe(stage, pid):
        index = len(result["observations"][stage])
        row, pin, previous_errors = None, None, len(result["preservation_errors"])
        try:
            row, pin = _observe(backend, stage, index, pid, old, root, deadline)
        finally:
            # Assignment may be interrupted after the physical journals were
            # saved. Audit these exact attempted paths independently.
            try:
                path = root / (stage + "-%02d.json" % index)
                if pin is None and path.exists(): pin = _pin(path)
                if pin is not None: result["observations"][stage].append(pin)
            except BaseException as error: preservation_error(stage + ":observation", error)
            try:
                query_pin = row["query"] if row is not None else None
                folder = root / (stage + "-%02d-ps" % index)
                path = folder / "preservation-failure.json"
                if not path.exists(): path = folder / "receipt.json"
                if query_pin is None and path.exists(): query_pin = _pin(path)
                if query_pin is not None: query_pins.append(query_pin)
            except BaseException as error: preservation_error(stage + ":query", error)
            try: _save(receipt, result)
            except BaseException as error: preservation_error(stage + ":receipt", error)
        _need(len(result["preservation_errors"]) == previous_errors, "observation preservation failed; no further action")
        _need(row["status"] == "observed", "independent restart observation failed")
        return row
    def unchanged():
        reader.recheck()
        _resources(reader, intent, sources)
        _need(_same(R.fingerprint(app), intent["target"]), "app bundle changed during restart")
    try:
        backend = _Mac()
        before = observe("before", old["pid"])
        _need(_same(before["identity"], old), "independent old NS/kernel identity differs")
        old_kernel = before["kernel"]
        unchanged()
        context = {"target": intent["target"], "old_identity": old, "old_kernel": old_kernel,
                   "deadline_monotonic": deadline}
        _, _, result["callbacks"]["stop"] = _hook("stop", stop_hook, context, root, deadline)
        _save(receipt, result)
        for index in range(MAX_SAMPLES):
            if _old_or_absent(observe("absence", old["pid"]), old, old_kernel): break
            _need(index + 1 < MAX_SAMPLES, "old app did not become independently absent within observation budget")
            backend.tick(deadline)
        unchanged()
        _need(_old_or_absent(observe("prelaunch", old["pid"]), old, old_kernel), "target reappeared before the owned launch")
        returned, launch, result["callbacks"]["launch"] = _hook("launch", launch_hook, context, root, deadline)
        _save(receipt, result)
        pid, first_kernel, first_ns, previous = _launch_return(returned), None, None, None
        for index in range(MAX_SAMPLES):
            row = observe("new", pid)
            candidate = _new(row, old, pid, launch)
            registration = _ns_identity(row["registrations"], old)
            if registration is not None:
                _need(first_ns is None or _same(first_ns, registration), "launch PID changed its first complete NS epoch; no adoption")
                first_ns = registration
            elif first_ns is not None: raise ValueError("launch PID lost its complete NS registration before confirmation")
            if row["kernel"] is not None:
                _need(first_kernel is None or _same(first_kernel, row["kernel"]), "launch PID changed kernel epochs; no adoption")
                first_kernel = row["kernel"]
            elif first_kernel is not None: raise ValueError("launch PID disappeared before confirmation")
            if candidate is not None and previous is not None and _same(previous["identity"], candidate) and _same(previous["kernel"], row["kernel"]):
                result["new_identity"] = candidate
                break
            previous = row if candidate is not None else None
            _need(index + 1 < MAX_SAMPLES, "launch completion PID lacked two consistent complete observations")
            backend.tick(deadline)
        unchanged()
        final = observe("final", pid)
        _need(_same(final["identity"], result["new_identity"]) and _same(final["kernel"], first_kernel), "new app changed after confirmation")
        _need(time.monotonic() < deadline, "restart measurement deadline expired")
        result["status"] = "restart_observed"
    except BaseException as error:
        result["status"] = "failed"
        result["errors"].append(_error(error))
        if not isinstance(error, Exception): interrupted = error
    finally:
        # Retain attempted callback journals even when an exception interrupted
        # assignment of their final return pin. No zero-attempt default here.
        for phase in ("stop", "launch"):
            try:
                path = root / (phase + ".json")
                expected_pin = result["callbacks"][phase]
                if expected_pin is not None:
                    _need(path.exists(), "captured callback journal is missing")
                    _need(_same(_pin(path), expected_pin), "captured callback journal changed after completion")
                elif path.exists(): result["callbacks"][phase] = _pin(path)
            except BaseException as error: preservation_error(phase + ":callback", error)
        cleanup = {"format": CLEANUP, "queries": query_pins, "app_processes_owned": False,
                   "app_cleanup_attempts": 0, "callback_cleanup": "trusted_producer_responsibility"}
        try: result["owned_cleanup"] = _save(root / "owned-cleanup.json", cleanup)
        except BaseException as error: preservation_error("owned_cleanup", error)
        try:
            unchanged()
            result["source_unchanged"] = True
            result["target_after"] = R.fingerprint(app)
        except BaseException as error:
            preservation_error("resources", error)
        try: result.update(finished_monotonic=time.monotonic(), finished_wall=time.time())
        except BaseException as error: preservation_error("finished_time", error)
        retain_final_receipt()
    if interrupted is not None: raise interrupted
    if result["status"] != "restart_observed": return failed()
    try:
        measurement = validate_restart(intent, receipt_pin)
    except BaseException as error:
        result["status"] = "failed"
        result["errors"].append(_error(error))
        retain_final_receipt()
        if not isinstance(error, Exception): raise
        if interrupted is not None: raise interrupted
        return failed()
    return {"receipt": receipt_pin, "measurement": _save(root / "measurement.json", measurement),
            "status": "restart_measurement_validated", "proof_created": False}


def _load_observation(reader, pin, root, stage, index, pid, old, deadline):
    _need(pin["file"] == str(root / (stage + "-%02d.json" % index)), "observation path/stage/index differs")
    row = reader.json(pin)
    _keys(row, {"format", "stage", "index", "queried_pid", "started_monotonic", "finished_monotonic", "started_wall",
                "finished_wall", "registrations", "query", "identity", "kernel", "status", "error"})
    _need(row["format"] == OBSERVATION and row["stage"] == stage and type(row["index"]) is int and row["index"] == index and
          type(row["queried_pid"]) is int and row["queried_pid"] == pid and row["status"] == "observed" and row["error"] is None,
          "complete actual restart observation required")
    begin, end = _moment(row["started_monotonic"]), _moment(row["finished_monotonic"])
    _need(begin <= end < deadline and _moment(row["started_wall"]) <= _moment(row["finished_wall"]), "observation timing differs")
    query_pin = row["query"]
    query_root = root / (stage + "-%02d-ps" % index)
    _need(query_pin["file"] == str(query_root / "receipt.json"), "kernel observation query path differs")
    query = reader.json(query_pin)
    for name in ("stdout", "stderr"):
        _need(query[name]["file"] == str(query_root / name), "actual raw kernel file path differs")
    kernel = _kernel(query, reader, pid, allow_exiting=stage in {"absence", "prelaunch"})
    registration = _ns_identity(row["registrations"], old)
    _need(begin <= query["started_monotonic"] <= query["finished_monotonic"] <= end and
          (registration is None or registration["launch_time"] <= row["finished_wall"]) and
          _same(row["kernel"], kernel) and _same(row["identity"], _identity(row["registrations"], kernel, old)),
          "NS/kernel observation interpretation or actual query interval differs")
    return row


def _validate_restart(intent, receipt_pin):
    """Historical VM-only restart measurement. Never query an old live PID.

    Raw NS snapshots and callback completion provenance remain trusted local
    producer evidence, not signatures. This does not prove causality against
    outside interference, task success, actor reuse, trial aggregation or cards.
    """
    intent, receipt_pin = validate_intent(intent), copy.deepcopy(receipt_pin)
    reader = E._Reader()
    result = reader.json(receipt_pin)
    root = Path(receipt_pin["file"]).parent
    _need(receipt_pin["file"] == str(root / "receipt.json"), "actual restart receipt path required")
    _keys(result, {"format", "status", "intent", "baseline", "callbacks", "observations", "owned_cleanup", "new_identity",
                   "target_after", "source_unchanged", "errors", "preservation_errors", "finished_monotonic", "finished_wall", "proof_created",
                   "task_proof_created", "public_proof_created", "card_created"})
    _need(result["format"] == RECEIPT and result["status"] == "restart_observed" and result["source_unchanged"] is True and
          _same(result["errors"], []) and _same(result["preservation_errors"], []) and _same(result["target_after"], intent["target"]) and
          all(result[key] is False for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created")),
          "complete clean ordinary restart evidence required")
    _need(result["intent"]["file"] == str(root / "intent.json") and result["baseline"]["file"] == str(root / "baseline.json") and
          _same(reader.json(result["intent"]), intent), "retained restart intent changed")
    baseline = reader.json(result["baseline"])
    _keys(baseline, {"format", "intent_sha256", "sources", "timeout_seconds", "started_monotonic", "started_wall",
                     "deadline_monotonic", "max_samples", "interval_seconds"})
    _need(baseline["format"] == BASELINE and baseline["intent_sha256"] == R.digest(intent) and
          type(baseline["timeout_seconds"]) is int and 1 <= baseline["timeout_seconds"] <= 60 and
          type(baseline["max_samples"]) is int and baseline["max_samples"] == MAX_SAMPLES and
          type(baseline["interval_seconds"]) is float and baseline["interval_seconds"] == INTERVAL,
          "fixed restart source/budget baseline differs")
    admission = _resources(reader, intent, baseline["sources"])
    old, deadline = intent["old_identity"], _moment(baseline["deadline_monotonic"])
    _need(deadline == _moment(baseline["started_monotonic"]) + baseline["timeout_seconds"] and
          _moment(baseline["started_wall"]) <= _moment(result["finished_wall"]) and
          old["launch_time"] <= baseline["started_wall"] and
          _moment(result["finished_monotonic"]) < deadline, "restart measurement outside actual budget")
    _need(_same(R.fingerprint(old["bundle"]), intent["target"]), "current unchanged app resource fingerprint differs")
    _keys(result["callbacks"], {"stop", "launch"})
    callbacks, returned = {}, {}
    for phase in ("stop", "launch"):
        pin = result["callbacks"][phase]
        _need(pin["file"] == str(root / (phase + ".json")), "actual callback journal path differs")
        row = reader.json(pin)
        _keys(row, {"format", "phase", "attempts", "issuance", "status", "started_monotonic", "started_wall", "deadline_monotonic",
                    "entry_check_monotonic", "entry_check_wall",
                    "finished_monotonic", "finished_wall", "return", "error"})
        _need(row["format"] == CALLBACK and row["phase"] == phase and type(row["attempts"]) is int and row["attempts"] == 1 and
              row["issuance"] == "attempted" and row["status"] == "returned" and row["error"] is None and row["deadline_monotonic"] == deadline and
              _moment(row["started_monotonic"]) <= _moment(row["entry_check_monotonic"]) <= _moment(row["finished_monotonic"]) < deadline and
              _moment(row["started_wall"]) <= _moment(row["entry_check_wall"]) <= _moment(row["finished_wall"]) and
              row["return"]["file"] == str(root / (phase + "-return.json")), "one complete bounded ordinary callback required")
        returned[phase] = reader.json(row["return"])
        _need(len(json.dumps(returned[phase], ensure_ascii=False, allow_nan=False).encode()) <= 32 * 1024,
              "callback logical JSON return exceeds actual producer bound")
        callbacks[phase] = row
    pid = _launch_return(returned["launch"])
    _keys(result["observations"], STAGES)
    history, previous, wall = {}, baseline["started_monotonic"], baseline["started_wall"]
    queries = []
    for stage in STAGES:
        pins = result["observations"][stage]
        _need(type(pins) is list and (len(pins) == 1 if stage in {"before", "prelaunch", "final"} else 1 <= len(pins) <= MAX_SAMPLES),
              "bounded complete ordered restart history required")
        rows = []
        for index, pin in enumerate(pins):
            row = _load_observation(reader, pin, root, stage, index, old["pid"] if stage in {"before", "absence", "prelaunch"} else pid, old, deadline)
            _need(previous < row["started_monotonic"] and wall < row["started_wall"], "restart observation order changed")
            previous, wall = row["finished_monotonic"], row["finished_wall"]
            queries.append(row["query"])
            rows.append(row)
        history[stage] = rows
    before, prelaunch, final = history["before"][0], history["prelaunch"][0], history["final"][0]
    stop, launch = callbacks["stop"], callbacks["launch"]
    _need(before["finished_monotonic"] < stop["started_monotonic"] <= stop["finished_monotonic"] < history["absence"][0]["started_monotonic"] and
          history["absence"][-1]["finished_monotonic"] < prelaunch["started_monotonic"] <= prelaunch["finished_monotonic"] < launch["started_monotonic"] and
          launch["finished_monotonic"] < history["new"][0]["started_monotonic"] and
          final["finished_monotonic"] < result["finished_monotonic"], "actual stop/absence/owned-launch/new-epoch order differs")
    _need(before["finished_wall"] < stop["started_wall"] <= stop["finished_wall"] < history["absence"][0]["started_wall"] and
          history["absence"][-1]["finished_wall"] < prelaunch["started_wall"] <= prelaunch["finished_wall"] < launch["started_wall"] and
          launch["finished_wall"] < history["new"][0]["started_wall"] and final["finished_wall"] < result["finished_wall"],
          "actual restart wall-time order differs")
    _need(_same(before["identity"], old) and before["kernel"] is not None, "actual full old identity differs")
    absent = [_old_or_absent(row, old, before["kernel"]) for row in history["absence"]]
    _need(absent[-1] is True and not any(absent[:-1]) and _old_or_absent(prelaunch, old, before["kernel"]),
          "FIRST actual complete NS and old kernel absence required before launch")
    first_kernel, first_ns, previous, stable = None, None, None, []
    for index, row in enumerate(history["new"]):
        candidate = _new(row, old, pid, launch)
        registration = _ns_identity(row["registrations"], old)
        if registration is not None:
            _need(first_ns is None or _same(first_ns, registration), "launch PID changed its first complete NS epoch")
            first_ns = registration
        else: _need(first_ns is None, "launch PID lost its complete NS registration")
        if row["kernel"] is not None:
            _need(first_kernel is None or _same(first_kernel, row["kernel"]), "launch PID kernel epoch changed")
            first_kernel = row["kernel"]
        else: _need(first_kernel is None, "launch PID disappeared before confirmation")
        if candidate is not None and previous is not None and _same(previous["identity"], candidate) and _same(previous["kernel"], row["kernel"]): stable.append(index)
        previous = row if candidate is not None else None
    _need(stable == [len(history["new"]) - 1] and len(history["new"]) >= 2 and
          _same(result["new_identity"], history["new"][-1]["identity"]) and
          _same(final["identity"], result["new_identity"]) and _same(final["kernel"], first_kernel),
          "FIRST complete consistent new epoch pair and final current identity required")
    _need(result["owned_cleanup"]["file"] == str(root / "owned-cleanup.json"), "actual owned-query cleanup journal required")
    cleanup = reader.json(result["owned_cleanup"])
    _need(_same(cleanup, {"format": CLEANUP, "queries": queries, "app_processes_owned": False,
        "app_cleanup_attempts": 0, "callback_cleanup": "trusted_producer_responsibility"}), "owned cleanup scope differs from actual read-only query ledger")
    reader.recheck()
    _need(_same(R.fingerprint(old["bundle"]), intent["target"]), "app resource changed during historical validation")
    return {"format": REPORT, "status": "restart_measurement_validated", "restart_measurement_validated": True,
        "intent_sha256": R.digest(intent), "receipt": copy.deepcopy(receipt_pin), "target": copy.deepcopy(intent["target"]),
        "old_identity": copy.deepcopy(old), "new_identity": copy.deepcopy(result["new_identity"]),
        "old_kernel": copy.deepcopy(before["kernel"]), "new_kernel": copy.deepcopy(final["kernel"]),
        "launch_completion_pid": pid, "stop_attempts": 1, "launch_attempts": 1,
        "callback_deadlines": "cooperative", "provenance": "trusted_local_producer_not_authentication",
        "trial_aggregation_validated": False, "task_proof_created": False, "public_proof_created": False,
        "proof_created": False, "card_created": False, "input_generalization_claimed": False,
        "source_admission": admission,
        "files": {name: copy.deepcopy(row["pin"]) for name, row in reader.files.items()}}


def validate_restart(intent, receipt_pin):
    """VM-only retained measurement validation; no old-process lookup."""
    R.assert_guest()  # Before malformed declarations or any retained file IO.
    try:
        return _validate_restart(copy.deepcopy(intent), copy.deepcopy(receipt_pin))
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError("malformed retained process restart evidence") from error
