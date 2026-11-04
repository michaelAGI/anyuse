"""Fixed zero-input playback trials and retained evidence validation.

This deliberately separate backend leaves the older complete-AX/lifecycle
state policy unchanged.  The observer uses public AX and actual temporal
display pixels, never actor return values or target-native state getters.
Only one saved typed Objective-C method is dispatched; receiver getters are
counted separately from that one task invocation.  Restart is not inferred
from a changed PID: a campaign must independently admit a real restart witness.
"""
from __future__ import annotations

import copy
from pathlib import Path
import time
import uuid

from . import rea_ax_state as S
from . import rea_harness as H
from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_state_evidence as E
from . import rea_state_task as T
from . import rea_playback_state as O

POLICY = "anyuse.rea-playback-task/1"
INTENT = "anyuse.rea-playback-trial-intent/1"
CASE = "anyuse.rea-playback-trial-case/1"
JOURNAL = "anyuse.rea-playback-trial-journal/1"
REPORT = "anyuse.rea-playback-trial-measurement/1"
PHASES = (*T.PHASES, "replay")
EVIDENCE = {"before", "execution", "worker", "after", "journal"}


def _need(value, message):
    if not value: raise ValueError(message)


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "closed playback-trial schema required")


def validate_policy(policy):
    policy = copy.deepcopy(policy)
    _keys(policy, {"format", "id", "target", "kind", "params", "input_scope", "heldout_scope", "phase_minimums", "observer", "before", "after"})
    _need(policy["format"] == POLICY and policy["kind"] == "fixed_state_transition" and
          type(policy["params"]) is list and policy["params"] == [] and
          policy["input_scope"] == "fixed_no_actor_inputs" and policy["heldout_scope"] == "fresh_trials_with_declared_restart" and
          S._same(policy["phase_minimums"], T.MINIMUMS) and policy["before"] == "playing" and policy["after"] == "paused",
          "fixed zero-input playing-to-paused task policy required")
    T._identifier(policy["id"])
    policy["target"] = S.validate_target(policy["target"])
    policy["observer"] = O.validate_observer(policy["observer"])
    return policy


def _deployment(value, target):
    _keys(value, {"id", "kind", "identity"})
    T._identifier(value["id"])
    identity = S.validate_identity(value["identity"])
    _need(value["kind"] == "existing_process" and identity["bundle_id"] == target["bundle_id"],
          "actual existing process required; this trial never manufactures restart admission")
    return copy.deepcopy(value)


def validate_intent(policy, intent):
    policy, intent = validate_policy(policy), copy.deepcopy(intent)
    _keys(intent, {"format", "id", "phase", "policy_sha256", "inputs", "deployment", "context", "observed_monotonic"})
    T._identifier(intent["id"])
    _need(intent["format"] == INTENT and type(intent["phase"]) is str and intent["phase"] in PHASES and
          intent["policy_sha256"] == R.digest(policy), "prospective exact-policy playback intent required")
    T.validate_inputs(intent["inputs"])
    _deployment(intent["deployment"], policy["target"])
    context = O.validate_context(intent["context"])
    _need(context["window"]["pid"] == intent["deployment"]["identity"]["pid"], "current media/window owner differs from intent process")
    E._moment(intent["observed_monotonic"])
    return intent


def validate_case(policy, case):
    policy, case = validate_policy(policy), copy.deepcopy(case)
    _keys(case, {"format", "id", "phase", "inputs", "deployment", "context", "evidence"})
    T._identifier(case["id"])
    _need(case["format"] == CASE and type(case["phase"]) is str and case["phase"] in PHASES, "explicit playback trial case required")
    T.validate_inputs(case["inputs"])
    _deployment(case["deployment"], policy["target"])
    context = O.validate_context(case["context"])
    _need(context["window"]["pid"] == case["deployment"]["identity"]["pid"], "case window owner differs from process")
    _keys(case["evidence"], EVIDENCE)
    case["evidence"] = {key: T._pin(value) for key, value in case["evidence"].items()}
    _need(len({pin["file"] for pin in case["evidence"].values()}) == len(EVIDENCE), "trial evidence roles must use distinct files")
    _need(context["media"]["file"] not in {pin["file"] for pin in case["evidence"].values()}, "media cannot be execution evidence")
    return case


def _actor(actor):
    actor = B.validate_actor(actor)
    _need(actor["kind"] == "objc_method", "playback backend requires one saved typed objc_method")
    request = B.invocation_request(actor)
    _need(len(request["operations"]) == 1 and request["operations"][0]["op"] == "invoke", "one immutable task invocation required")
    return actor


def _pin(path):
    path = Path(path).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "bytes": path.lstat().st_size}


def _source_paths():
    # A fixed dependency set, never a serialized import/callback/producer path.
    return sorted({str(Path(module.__file__).resolve(strict=True)) for module in (O, S, B, E, H, R, T)} |
                  {str(Path(__file__).resolve(strict=True)), str(Path(B.__file__).with_name("rea_runtime_agent.js").resolve(strict=True))})


_LEGACY_SOURCE_PROFILE = {'execution/rea_ax_state.py': {'bytes': 43905, 'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'}, 'execution/rea_harness.py': {'bytes': 149743, 'sha256': 'd216baa33f2fba53e8ac537681b107366f57c5f65aa295638a8b2af71e6a7127'}, 'execution/rea_mapping.py': {'bytes': 34014, 'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'}, 'execution/rea_playback_state.py': {'bytes': 39272, 'sha256': '50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96'}, 'execution/rea_playback_trial.py': {'bytes': 23972, 'sha256': 'e5411b36f90f535f8b00b72d2d7ec54e9b357ccd32d22bcc00c56a2048860d80'}, 'execution/rea_runtime_agent.js': {'bytes': 231529, 'sha256': '2900eb5df04219d9de1c100567f4e9ad050dd48c22ddf0080484c74058a21382'}, 'execution/rea_runtime_bridge.py': {'bytes': 223039, 'sha256': '75c2bc7ba42172c7ca38f5729ecdfa4c6cd0b03f52015cc10c2bae064224e793'}, 'execution/rea_state_evidence.py': {'bytes': 35445, 'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'}, 'execution/rea_state_task.py': {'bytes': 13294, 'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}
_VALIDATOR_ROLE = "execution/rea_playback_trial.py"
_SOURCE_FAMILY = "playback"
_SOURCE_DOMAIN = "playback_source"
_SOURCE_MAXIMUM = B.MAX_AGENT_BYTES


# One reviewed forward migration; every producer role and installed core byte is fixed.
_REVIEWED_EXECUTION_RESTORATION_PAIR = {'installed_core': {'execution/rea_ax_state.py': {'bytes': 43905,
                                                  'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
                    'execution/rea_harness.py': {'bytes': 152112,
                                                 'sha256': '73e6f9eb8e861eb7b544ed613a186a92d2908ff13eebc183ac6bb74d6167d7ef'},
                    'execution/rea_mapping.py': {'bytes': 35219,
                                                 'sha256': '7c30e11b8b14f924c2f24030e537be1a53d6ca4981bcc0b2119eb9987820db0f'},
                    'execution/rea_playback_state.py': {'bytes': 39272,
                                                        'sha256': '50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96'},
                    'execution/rea_runtime_agent.js': {'bytes': 231711,
                                                       'sha256': 'b3170ddf0b1d7c3459fc37d85162909270ac4272339019abad4d32127949c81a'},
                    'execution/rea_runtime_bridge.py': {'bytes': 223984,
                                                        'sha256': '2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485'},
                    'execution/rea_state_evidence.py': {'bytes': 35445,
                                                        'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
                    'execution/rea_state_task.py': {'bytes': 13294,
                                                    'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}},
 'producer': {'execution/rea_ax_state.py': {'bytes': 43905,
                                            'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
              'execution/rea_harness.py': {'bytes': 149727,
                                           'sha256': 'e5815780190c8c9701e2f7117bae8c378e2df9ad79778f152f6625eaaac22f7f'},
              'execution/rea_mapping.py': {'bytes': 34014,
                                           'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'},
              'execution/rea_playback_state.py': {'bytes': 39272,
                                                  'sha256': '50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96'},
              'execution/rea_playback_trial.py': {'bytes': 28134,
                                                  'sha256': '62245b819294c5555ce8dc8a58c23516fea573346a2c49d3daa906c989beda6d'},
              'execution/rea_runtime_agent.js': {'bytes': 231711,
                                                 'sha256': 'b3170ddf0b1d7c3459fc37d85162909270ac4272339019abad4d32127949c81a'},
              'execution/rea_runtime_bridge.py': {'bytes': 223984,
                                                  'sha256': '2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485'},
              'execution/rea_state_evidence.py': {'bytes': 35445,
                                                  'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
              'execution/rea_state_task.py': {'bytes': 13294,
                                              'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}}


# Separately reviewed historical closure; never a live runtime adapter authorization.
_REVIEWED_V19_SOURCE_PROFILE = {'execution/rea_ax_state.py': {'bytes': 43905,
                               'sha256': '60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f'},
 'execution/rea_harness.py': {'bytes': 149743,
                              'sha256': 'd216baa33f2fba53e8ac537681b107366f57c5f65aa295638a8b2af71e6a7127'},
 'execution/rea_mapping.py': {'bytes': 34014,
                              'sha256': 'e126aa8d855d08dc0382162a74c0bad37fedff32d5e7bfd7d5e2d16e82115483'},
 'execution/rea_playback_state.py': {'bytes': 39272,
                                     'sha256': '50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96'},
 'execution/rea_playback_trial.py': {'bytes': 28134,
                                     'sha256': '62245b819294c5555ce8dc8a58c23516fea573346a2c49d3daa906c989beda6d'},
 'execution/rea_runtime_agent.js': {'bytes': 231529,
                                    'sha256': '2900eb5df04219d9de1c100567f4e9ad050dd48c22ddf0080484c74058a21382'},
 'execution/rea_runtime_bridge.py': {'bytes': 223039,
                                     'sha256': '75c2bc7ba42172c7ca38f5729ecdfa4c6cd0b03f52015cc10c2bae064224e793'},
 'execution/rea_state_evidence.py': {'bytes': 35445,
                                     'sha256': '1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce'},
 'execution/rea_state_task.py': {'bytes': 13294,
                                 'sha256': 'c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6'}}


# The visual backend adds only explicit R/H delegation branches. This separate
# whole-core variant preserves both original producer closures and their first
# reviewed installed core. No role can independently select old or new bytes.
_REVIEWED_VISUAL_VERIFICATION_CORE = {
    "execution/rea_ax_state.py": {"bytes": 43905, "sha256": "60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f"},
    "execution/rea_harness.py": {"bytes": 153262, "sha256": "c57fdae201e10d5375cb02c41d0607fe0facb3590313fac51a76f1d8b301fd6b"},
    "execution/rea_mapping.py": {"bytes": 35270, "sha256": "61a6dd1b9362f7cd98c56661dddbe9e3d637cc5a3bbd7b77f7adbd2cce282bb5"},
    "execution/rea_playback_state.py": {"bytes": 39272, "sha256": "50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96"},
    "execution/rea_runtime_agent.js": {"bytes": 231711, "sha256": "b3170ddf0b1d7c3459fc37d85162909270ac4272339019abad4d32127949c81a"},
    "execution/rea_runtime_bridge.py": {"bytes": 223984, "sha256": "2c0cad9803c1825651fe2742b31acd8a9f7c52afbe8d63d9193a001840099485"},
    "execution/rea_state_evidence.py": {"bytes": 35445, "sha256": "1c3279fc33ff1750b99dbb38a8672373726c3c7d7a61c06e958aab4449ff0bce"},
    "execution/rea_state_task.py": {"bytes": 13294, "sha256": "c0fe63d7bb7aee061230ac1668d1a4229af35d057beee6fcaf4ebcb08d0327a6"},
}


# Reviewed e314b3c..c8b433d: B/JS add declared-native resources and borrowed Qt
# receivers. Fixed objc_method/invoke playback enters neither branch and still
# requires no native resources, the original class grammar, module/actor/raw
# trial joins, exact call counts and cleanup. Preserve each earlier whole map.
_REVIEWED_GENERIC_WORKFLOW_CORE = {
    "execution/rea_ax_state.py": {"bytes": 43905, "sha256": "60a71de9180cbf4940ad1d128fe023ae4cb9e89329f74606d68046866586375f"},
    "execution/rea_harness.py": {"bytes": 153262, "sha256": "c57fdae201e10d5375cb02c41d0607fe0facb3590313fac51a76f1d8b301fd6b"},
    "execution/rea_mapping.py": {"bytes": 35270, "sha256": "61a6dd1b9362f7cd98c56661dddbe9e3d637cc5a3bbd7b77f7adbd2cce282bb5"},
    "execution/rea_playback_state.py": {"bytes": 39272, "sha256": "50728a4da284c9b9cb877232c724dc35a388adb943e1a1367deb21a4fcf5db96"},
    "execution/rea_runtime_agent.js": {"bytes": 232800, "sha256": "205a47b9ee7b617090bb06f53f06799071c2f190a417261a13775cd547d42883"},
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
        admission = "reviewed_v19_fixed_playback_exact_core"
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


def _sources(reader, pins=None, *, admission=None):
    current = {name: _pin(name) for name in _source_paths()}
    if pins is None: pins = current
    admitted = _admit_sources(reader, pins, current)
    if admission is not None: admission.update(admitted)
    return copy.deepcopy(pins)


def _historical_adapter(reader, adapter, sources):
    """Validate an exact admitted producer's retained asset, never for dispatch."""
    current = {name: _pin(name) for name in _source_paths()}
    admission = _admit_sources(reader, sources, current)
    _need(admission["admission"] in {"reviewed_v19_fixed_playback_exact_core", "reviewed_b2_execution_restoration_exact_core"},
          "historical adapter requires an exact reviewed playback source pair")
    source = next(pin for name, pin in sources.items() if Path(name).name == "rea_runtime_agent.js")
    reader.read(source, _SOURCE_DOMAIN, maximum=_SOURCE_MAXIMUM, retain=False)
    _need(type(adapter) is dict, "retained historical adapter declaration required")
    asset = {key: adapter.get(key) for key in ("file", "bytes", "sha256")}
    _need(type(asset["bytes"]) is int and 1 <= asset["bytes"] <= B.MAX_AGENT_BYTES,
          "bounded nonempty historical adapter required")
    reader.read(asset, "runtime_adapter", maximum=B.MAX_AGENT_BYTES, retain=False)
    manifest = adapter.get("manifest")
    _need(type(manifest) is dict and manifest.get("file") == asset["file"] + ".manifest.json" and
          type(manifest.get("bytes")) is int and 1 <= manifest["bytes"] <= B.MAX_REQUEST_BYTES,
          "exact bounded adjacent historical adapter manifest required")
    build = reader.json(manifest, "runtime_adapter", maximum=B.MAX_REQUEST_BYTES)
    _need(type(build) is dict and build.get("format") == "anyuse.rea-runtime-adapter/1" and
          build.get("source_sha256") == source["sha256"] and build.get("bundle_sha256") == asset["sha256"],
          "historical build must match the admitted source and exact retained compiled asset")
    installed = {**asset, "source_sha256": source["sha256"], "manifest": manifest, "build": build}
    _need(all(S._same(adapter.get(key), value) for key, value in installed.items()),
          "recorded historical adapter/source/full build differs")
    reader.recheck()
    return installed


def _native(reader, actor, runtime, worker_pin, identity, target, *, _historical_sources=None):
    """Join the fixed worker, config, cleanup journal and exact ObjC ABI route."""
    request, calls = B.invocation_request(actor), B.minimum_native_calls(actor)
    worker = reader.json(worker_pin, "trial")
    _need(runtime.get("format") == B.RUNTIME and runtime.get("status") == "executed" and
          runtime.get("evidence_only") is True and runtime.get("verified") is False and runtime.get("proof_created") is False and
          type(runtime.get("model_calls")) is int and runtime["model_calls"] == 0 and runtime.get("actuated") is True and
          runtime.get("native_calls") is True and runtime.get("resource_review_required") is False and runtime.get("error") is None,
          "current clean actual native invocation receipt required")
    _need(S._same(runtime.get("target"), target) and S._same(runtime.get("request"), request) and
          runtime.get("request_sha256") == R.digest(request) and runtime.get("bundle") == identity["bundle"] and
          S._same(runtime.get("result"), worker), "native request/target/raw worker differs from frozen actor")
    for value in (runtime.get("expected_identity"), runtime.get("identity"), runtime.get("after_identity"), worker.get("identity"), worker.get("after_identity")):
        _need(S._same(S.validate_identity(value), identity), "full native process identity changed within playback trial")
    _need(S._same(runtime.get("transport"), {"kind": "local"}) and S._same(worker.get("transport"), {"kind": "local"}), "explicit local runtime transport required")
    B._worker_result(worker, R.digest(request), request, {"kind": "local"})
    _need(H._runtime_cleanup_valid(runtime) and worker.get("format") == B.WORKER and worker.get("status") == "completed" and
          worker.get("attached") is True and worker.get("handles_expired") is True and worker.get("native_state_uncertain") is False and
          S._same(worker.get("cleanup"), {"script_unloaded": True, "detached": True}) and worker.get("cleanup_errors") == [] and
          worker.get("error") is None and worker.get("queue") == "main" and
          type(worker.get("native_calls")) is int and worker["native_calls"] == calls and
          type(worker.get("invocations")) is int and worker["invocations"] == 1 and
          type(worker.get("helper_load_attempts")) is int and worker["helper_load_attempts"] == 0 and
          type(worker.get("callable_resolutions")) is int and worker["callable_resolutions"] == 0 and
          worker.get("helper_residency") == [] and worker.get("preloaded_gadget_remains") is False,
          "actual getter/task counts, queue, cleanup or uncertainty differ")
    _need(S._same(worker.get("process"), {"pid": identity["pid"], "executable": identity["executable"], "architecture": "arm64"}), "native injected process metadata differs")
    for key in ("compiler_provenance", "native_resources"):
        _need(S._same(runtime.get(key), {}) and S._same(worker.get(key), {}), "ObjC playback action cannot acquire native helper/resources")
    _need(type(worker.get("operations")) is list and len(worker["operations"]) == 1 and
          H._objc_method_run(worker["operations"][0], actor, 0), "actual named method/signature/ABI differs from frozen action")
    row = worker["operations"][0]
    if (_historical_sources is not None and
            _same_source(_producer_profile(_historical_sources)[0], _REVIEWED_V19_SOURCE_PROFILE)):
        _need(B._name(actor["receiver"]["class"]) and B.CLASS.fullmatch(row["method"]["class"]) is not None,
              "reviewed V19 playback retains its original receiver and observed class grammar")
    _need(row.get("method", {}).get("origin") == "module_backed" and
          row["method"]["module"].startswith(identity["bundle"] + "/"), "task handler must be backed by the exact owned application bundle")
    if actor["return_type"] == "void": _need(row.get("value") is None, "void task returned an undeclared value")
    adapter = runtime.get("adapter")
    _need(type(adapter) is dict and S._same(adapter.get("transport"), {"kind": "local"}), "fixed local adapter identity required")
    installed = (B._adapter(adapter.get("file")) if _historical_sources is None else
                 _historical_adapter(reader, adapter, _historical_sources))
    _need(all(S._same(adapter.get(key), value) for key, value in installed.items()), "fixed adapter/source/build changed")
    reader.read({key: adapter[key] for key in ("file", "sha256", "bytes")}, "runtime_adapter", maximum=B.MAX_AGENT_BYTES, retain=False)
    reader.json(adapter["manifest"], "runtime_adapter", maximum=B.MAX_REQUEST_BYTES)
    native_pin = _pin(runtime["receipt"])
    _need(worker_pin["file"] == str(Path(native_pin["file"]).parent / "worker-output.json"), "raw worker must be this native transaction's exact output file")
    artifacts = E._artifact_files(reader, runtime, native_pin)
    _need(S._same(artifacts["worker-output.json"][0], worker_pin), "native inventory differs from raw worker pin")
    disposition = runtime.get("worker")
    _keys(disposition, {"returncode", "timed_out", "elapsed_s", "output", "sha256", "stderr"})
    _need(type(disposition["returncode"]) is int and disposition["returncode"] == 0 and disposition["timed_out"] is False and
          disposition["output"] == "worker-output.json" and disposition["stderr"] == "worker.stderr" and
          disposition["sha256"] == worker_pin["sha256"], "actual native worker disposition differs")
    E._moment(disposition["elapsed_s"])
    config = E._json(artifacts["worker-request.json"][1])
    _keys(config, {"format", "bundle", "target", "identity", "request", "adapter", "transport", "native_resources", "compiler_provenance", "backend", "deadline_ms", "trace"})
    _need(config["format"] == B.WORKER and config["bundle"] == identity["bundle"] and
          S._same(config["target"], target) and S._same(config["identity"], identity) and S._same(config["request"], request) and
          S._same(config["adapter"], adapter) and S._same(config["transport"], {"kind": "local"}) and config["backend"] in (None, "local") and
          config["native_resources"] == {} and config["compiler_provenance"] == {} and
          type(config["deadline_ms"]) is int and config["deadline_ms"] > 0 and config["trace"] == artifacts["trace.jsonl"][0]["file"], "actual worker configuration differs")
    journal = E._json(artifacts["worker-cleanup.json"][1])
    expected = {"format": B.CLEANUP_JOURNAL, "request_sha256": R.digest(request), "status": "completed", "attached": True,
                "frida_lifecycle": worker["frida_lifecycle"], "cleanup": worker["cleanup"], "cleanup_errors": [], "error": None,
                "native_state_uncertain": False, "native_calls": calls, "invocations": 1, "helper_load_attempts": 0, "completed_operations": 1}
    _need(S._same(journal, expected), "actual worker cleanup journal differs from completed call counts")
    return {"native_calls": calls, "getter_calls": calls - 1, "invocations": 1}


def make_intent(bundle, policy, id, phase, *, expected_identity=None):
    R.assert_guest()
    policy = validate_policy(policy)
    T._identifier(id)
    _need(type(phase) is str and phase in PHASES, "explicit playback phase required")
    app = Path(bundle).resolve(strict=True)
    _need(S._same(R.fingerprint(app), policy["target"]), "playback target differs from frozen policy")
    identity = B.process_identity(app, policy["target"]["bundle_id"])
    if expected_identity is not None:
        _need(S._same(identity, S.validate_identity(expected_identity)), "current process differs from supplied epoch; no successor adoption")
    context = O.current_context(policy["observer"], identity, deadline=time.monotonic() + 10)
    return validate_intent(policy, {"format": INTENT, "id": id, "phase": phase, "policy_sha256": R.digest(policy), "inputs": {},
        "deployment": {"id": "epoch-" + R.digest(identity)[:24], "kind": "existing_process", "identity": identity},
        "context": context, "observed_monotonic": time.monotonic()})


def validate_trial(policy, case, actor):
    R.assert_guest()
    policy, case, actor = validate_policy(policy), validate_case(policy, case), _actor(actor)
    reader, identity = E._Reader(), case["deployment"]["identity"]
    evidence = case["evidence"]
    journal = reader.json(evidence["journal"], "trial")
    _keys(journal, {"format", "status", "intent", "actor_sha256", "policy_sha256", "request_sha256", "sources", "started_monotonic", "before_finished_monotonic", "pre_dispatch", "pre_dispatch_monotonic", "dispatch_started_monotonic", "dispatch_finished_monotonic", "finished_monotonic", "timeout_seconds", "execution", "error"})
    intent = validate_intent(policy, journal["intent"])
    _need(journal["format"] == JOURNAL and journal["status"] == "completed" and journal["error"] is None and
          journal["actor_sha256"] == R.digest(actor) and journal["policy_sha256"] == R.digest(policy) and
          journal["request_sha256"] == R.digest(B.invocation_request(actor)), "completed immutable playback journal required")
    _need(all(S._same(case[key], intent[key]) for key in ("id", "phase", "inputs", "deployment", "context")), "case differs from prospective current-media intent")
    admission = {}
    _sources(reader, journal["sources"], admission=admission)
    reader.read(case["context"]["media"], "media", maximum=O.MAX_MEDIA_BYTES, retain=False)
    before_raw, after_raw = reader.json(evidence["before"], "trial"), reader.json(evidence["after"], "trial")
    before = O.validate_observation(before_raw, policy["observer"], policy["target"], identity, case["context"], "playing", reader)
    after = O.validate_observation(after_raw, policy["observer"], policy["target"], identity, case["context"], "paused", reader)
    checkpoint = O.validate_snapshot(journal["pre_dispatch"], policy["observer"], identity, case["context"])
    _need(S._same(checkpoint["system"], before["system"]) and
          checkpoint["control"]["title"] == policy["observer"]["control"]["playing_title"], "retained immediate pre-dispatch public precondition differs")
    _need(S._same(before["system"], after["system"]), "process/kernel/session/focus differs across playback action")
    moments = [E._moment(journal[k]) for k in ("started_monotonic", "before_finished_monotonic", "dispatch_started_monotonic", "dispatch_finished_monotonic", "finished_monotonic")]
    O._alpha_interval(checkpoint, moments[1], E._moment(journal["pre_dispatch_monotonic"]))
    _need(intent["observed_monotonic"] <= moments[0] <= before["started_monotonic"] < before["finished_monotonic"] == moments[1] <=
          moments[1] <= E._moment(journal["pre_dispatch_monotonic"]) <= moments[2] < moments[3] <= after["started_monotonic"] < after["finished_monotonic"] <= moments[4] and
          type(journal["timeout_seconds"]) is int and 5 <= journal["timeout_seconds"] <= 180 and
          moments[4] - moments[0] <= journal["timeout_seconds"], "prospective before/dispatch/after timing or deadline differs")
    _need(S._same(journal["execution"], evidence["execution"]), "journal execution pin differs")
    runtime = reader.json(evidence["execution"], "trial")
    _need(runtime.get("receipt") == evidence["execution"]["file"], "native runtime points outside this case")
    historical = journal["sources"] if admission["admission"] == "reviewed_v19_fixed_playback_exact_core" else None
    # B2's adapter matched the installed JS before the generic migration. Keep
    # that current-adapter route, and also reopen its exact retained B2 build
    # when the compiled source is historical. No adapter is executed here.
    adapter = runtime.get("adapter")
    if (admission["admission"] == "reviewed_b2_execution_restoration_exact_core" and type(adapter) is dict and
            adapter.get("source_sha256") == _REVIEWED_EXECUTION_RESTORATION_PAIR["producer"]["execution/rea_runtime_agent.js"]["sha256"]):
        historical = journal["sources"]
    counts = _native(reader, actor, runtime, evidence["worker"], identity, policy["target"], _historical_sources=historical)
    reader.recheck()
    return {"format": REPORT, "status": "trial_measurement_validated", "trial_measurement_validated": True,
            "actor_sha256": R.digest(actor), "request_sha256": R.digest(B.invocation_request(actor)), "policy_sha256": R.digest(policy),
            "case_sha256": R.digest(case), "case_id": case["id"], "phase": case["phase"],
            "target": policy["target"], "identity": identity, "kernel": before["kernel"],
            "started_monotonic": moments[0], "finished_monotonic": moments[4], **counts,
            "inputs": {}, "scope": "independent_public_control_and_temporal_video_region", "restart_claimed": False,
            "source_admission": admission,
            "files": {name: copy.deepcopy(row["pin"]) for name, row in reader.files.items()},
            "proof_created": False, "task_proof_created": False, "public_proof_created": False,
            "aggregation_validated": False, "input_generalization_claimed": False}


def run_trial(bundle, actor, policy, intent=None, out=None, *, expected_identity=None, setup_hook=None, timeout=35,
              _disposition=None):
    """Observe→dispatch once→observe. No serialized setup code or retry path."""
    R.assert_guest()
    R._replay_disposition(_disposition)
    policy, actor = validate_policy(policy), _actor(actor)
    _need(out is not None and type(timeout) is int and 5 <= timeout <= 180 and
          (setup_hook is None or callable(setup_hook)), "bounded trial output and optional trusted callable required")
    app, folder = Path(bundle).resolve(strict=True), Path(out).resolve()
    _need(not folder.is_relative_to(app), "trial output must be outside target app")
    _need(S._same(R.fingerprint(app), policy["target"]), "current app differs from immutable playback target")
    identity = B.process_identity(app, policy["target"]["bundle_id"])
    if expected_identity is not None: _need(S._same(identity, S.validate_identity(expected_identity)), "current process differs from expected identity")
    if intent is None: intent = make_intent(app, policy, "replay-" + uuid.uuid4().hex, "replay", expected_identity=identity)
    intent = validate_intent(policy, intent)
    _need(S._same(intent["deployment"]["identity"], identity), "intent process is stale; no replacement adoption")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    started, request, reader = time.monotonic(), B.invocation_request(actor), E._Reader()
    deadline = started + timeout
    sources = _sources(reader)
    reader.read(intent["context"]["media"], "media", maximum=O.MAX_MEDIA_BYTES, retain=False)
    journal = {"format": JOURNAL, "status": "running", "intent": intent, "actor_sha256": R.digest(actor), "policy_sha256": R.digest(policy),
               "request_sha256": R.digest(request), "sources": sources, "started_monotonic": started, "before_finished_monotonic": None,
               "pre_dispatch": None, "pre_dispatch_monotonic": None,
               "dispatch_started_monotonic": None, "dispatch_finished_monotonic": None, "finished_monotonic": None,
               "timeout_seconds": timeout, "execution": None, "error": None}
    journal_file = folder / "journal.json"
    R.write_json(journal_file, journal)
    runtime, case_file = None, None
    def current():
        reader.recheck()
        _need(time.monotonic() < deadline and S._same(B.process_identity(app, policy["target"]["bundle_id"]), identity) and
              S._same(R.fingerprint(app), policy["target"]), "target/media/producer identity changed or trial deadline expired")
    try:
        if setup_hook is not None: setup_hook(copy.deepcopy(intent))
        current()
        before = O.capture(policy["observer"], policy["target"], identity, intent["context"], "playing", folder / "before", deadline=deadline)
        retained_before = reader.json(_pin(folder / "before/observation.json"), "trial")
        _need(S._same(before, retained_before), "before producer return differs from retained observation")
        O.validate_observation(retained_before, policy["observer"], policy["target"], identity, intent["context"], "playing", reader)
        journal["before_finished_monotonic"] = before["finished_monotonic"]
        current()
        journal["pre_dispatch"] = O.check_current(policy["observer"], identity, intent["context"], deadline=deadline)
        journal["pre_dispatch_monotonic"] = time.monotonic()
        journal["dispatch_started_monotonic"] = time.monotonic()
        R.write_json(journal_file, journal)
        remaining = int(deadline - time.monotonic())
        _need(remaining >= 2, "no budget remains for native action and independent after observation")
        if _disposition is not None:
            _disposition.started()
        runtime = B.collect(str(app), str(folder / "runtime"), request, timeout=min(60, remaining - 1), backend="local", expected_identity=identity)
        journal["dispatch_finished_monotonic"] = time.monotonic()
        journal["execution"] = _pin(folder / "runtime/runtime.json")
        R.write_json(journal_file, journal)
        _need(runtime.get("status") == "executed" and runtime.get("resource_review_required") is False, "native dispatch failed or requires resource review; no retry")
        if _disposition is not None:
            retained = reader.json(journal["execution"], "trial")
            _need(S._same(retained, runtime), "native return differs from retained worker receipt")
            _native(reader, actor, retained, _pin(folder / "runtime/worker-output.json"), identity, policy["target"])
            _disposition.confirmed()
        current()
        after = O.capture(policy["observer"], policy["target"], identity, intent["context"], "paused", folder / "after", deadline=deadline)
        current()
        journal.update(status="completed", finished_monotonic=time.monotonic())
        R.write_json(journal_file, journal)
        case = validate_case(policy, {"format": CASE, "id": intent["id"], "phase": intent["phase"], "inputs": {},
            "deployment": intent["deployment"], "context": intent["context"], "evidence": {
                "before": _pin(folder / "before/observation.json"), "execution": journal["execution"],
                "worker": _pin(folder / "runtime/worker-output.json"), "after": _pin(folder / "after/observation.json"), "journal": _pin(journal_file)}})
        R.write_json(folder / "case.json", case)
        case_file = _pin(folder / "case.json")
        measured = validate_trial(policy, case, actor)
        R.write_json(folder / "measurement.json", measured)
        result = {"status": "trial_measurement_validated", "case": case, "case_file": case_file, "measurement": measured,
                  "report": measured, "actuated": True, "resource_review_required": False, "proof_created": False, "task_proof_created": False}
    except Exception as error:
        journal.update(status="failed", finished_monotonic=time.monotonic(), error={"type": type(error).__name__, "message": str(error)[:4096]})
        R.write_json(journal_file, journal)
        result = {"status": "failed", "case": None, "case_file": case_file, "measurement": None, "report": None,
                  "error": journal["error"], "actuated": False if runtime is None else runtime.get("actuated"),
                  "resource_review_required": False if runtime is None else runtime.get("resource_review_required"),
                  "native_calls": None if runtime is None else (runtime.get("result") or {}).get("native_calls"),
                  "invocations": None if runtime is None else (runtime.get("result") or {}).get("invocations"),
                  "proof_created": False, "task_proof_created": False}
    R.write_json(folder / "outcome.json", result)
    return result
