"""VM guarded tests with inert app/NS/kernel/callback/process fixtures.

These execute the real producer, raw-byte parser and historical file validator.
They never start/stop an app or prove a real restart. No generic fixture names
are imported from other test modules.
"""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_process_restart as X


#: Exact bytes of the reviewed historical sources, vendored so the suite runs
#: without the development history (``testdata/reviewed_sources/<commit>/<role>``).
_REVIEWED_SOURCES = Path(__file__).resolve().parent.parent / "testdata" / "reviewed_sources"


def _reviewed_bytes(head, role):
    return (_REVIEWED_SOURCES / head / role).read_bytes()


def _ns(identity, terminated=False):
    return {key: copy.deepcopy(identity[key]) for key in ("pid", "bundle_id", "bundle", "executable", "launch_time")} | {"terminated": terminated}


def _kernel(identity):
    value = datetime.fromtimestamp(identity["launch_time"], timezone.utc)
    # Produce fixed C words independently of the process-wide test locale.
    weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[value.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[value.month - 1]
    started = "%s %s %d %02d:%02d:%02d %04d" % (weekday, month, value.day, value.hour, value.minute, value.second, value.year)
    return {"pid": identity["pid"], "ppid": 1, "uid": identity["uid"],
            "started_at": started, "executable": identity["executable"]}


def _stdout(kernel):
    if kernel is None: return b""
    return ("%d %d %d %s %s\n" % (kernel["pid"], kernel["ppid"], kernel["uid"], kernel["started_at"], kernel["executable"])).encode()


def _fake_query(pid, folder, deadline, clock, kernel):
    folder.mkdir(mode=0o700)
    start = clock.now
    (folder / "stdout").write_bytes(_stdout(kernel))
    (folder / "stderr").write_bytes(b"")
    clock.now += .01
    row = {"format": X.QUERY, "command": X._argv(pid), "started_monotonic": start,
        "finished_monotonic": clock.now, "timeout_seconds": min(2, deadline - start), "status": "returned",
        "owned": {"pid": 8000 + int(clock.now * 100), "returncode": 0 if kernel is not None else 1,
                  "reaped": True, "term_attempted": False, "kill_attempted": False, "errors": []},
        "stdout": X._pin(folder / "stdout"), "stderr": X._pin(folder / "stderr"), "error": None}
    return row, X._save(folder / "receipt.json", row)


@pytest.fixture
def restart_data(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = tmp_path / "Witness.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    executable = app / "Contents/MacOS/Witness"
    executable.write_bytes(b"inert executable, never run")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.test.restart",
        "CFBundleExecutable": "Witness", "CFBundleVersion": "1"}))
    class Clock:
        now = 100.0
        def monotonic(self):
            self.now += .00001
            return self.now
        def time(self): return 1792317600 + self.now
    clock = Clock()
    monkeypatch.setattr(X.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(X.time, "time", clock.time)
    old = {"bundle": str(app), "bundle_id": "org.test.restart", "executable": str(executable),
           "pid": 111, "launch_time": clock.time() - 10, "uid": os.getuid()}
    old_kernel = _kernel(old)
    class Backend:
        phase, mode, ticks, new_queries = "old", "normal", 0, 0
        new_identity, new_kernel = None, None
        def registrations(self, bundle_id):
            assert bundle_id == old["bundle_id"]
            if self.phase == "old": return [_ns(old)]
            if self.phase == "stopping": return [_ns(old, terminated=True)]
            if self.phase == "absent": return []
            if self.mode == "ns_disappears_before_kernel" and self.new_queries: return []
            if self.mode == "foreign_registration": return [_ns({**self.new_identity, "pid": self.new_identity["pid"] + 1})]
            return [_ns(self.new_identity)]
        def query(self, pid, folder, deadline):
            if self.phase in {"old", "stopping"}: kernel = old_kernel if pid == old["pid"] else None
            elif self.phase == "absent": kernel = None
            else:
                self.new_queries += 1
                missing = self.mode in {"late_kernel", "ns_epoch_before_kernel", "ns_disappears_before_kernel"} and self.new_queries == 1
                kernel = self.new_kernel if pid == self.new_identity["pid"] and not missing else None
            return _fake_query(pid, folder, deadline, clock, kernel)
        def tick(self, deadline):
            assert clock.now < deadline
            clock.now += X.INTERVAL
            self.ticks += 1
            if self.phase == "stopping" and self.mode == "auto_successor":
                self.new_identity = {**copy.deepcopy(old), "pid": 223, "launch_time": clock.time()}
                self.new_kernel = _kernel(self.new_identity)
                self.phase = "new"
            elif self.phase == "stopping" and self.mode != "never_absent": self.phase = "absent"
            elif self.phase == "new" and self.mode == "epoch_replacement": self.new_identity["launch_time"] += 1
            elif self.phase == "new" and self.mode == "ns_epoch_before_kernel":
                self.new_identity["launch_time"] += .000001
                self.new_kernel = _kernel(self.new_identity)
    backend = Backend()
    monkeypatch.setattr(X, "_Mac", lambda: backend)
    monkeypatch.setattr(B, "process_identity", lambda bundle, bundle_id: copy.deepcopy(old))
    source = tmp_path / "trusted-normal-hook-producer.txt"
    source.write_text("Inert separately frozen ordinary API callback provenance, not authentication.\n")
    producer = X._pin(source)
    intent = {"format": X.INTENT, "id": "one-inert-restart", "target": R.fingerprint(app), "old_identity": old,
        "stop": {"id": "one-normal-stop", "producer": producer},
        "launch": {"id": "one-normal-launch", "producer": copy.deepcopy(producer)}}
    calls = []
    def stop(context):
        calls.append(("stop", copy.deepcopy(context)))
        assert context["old_identity"] == old and context["old_kernel"] == old_kernel
        backend.phase = "stopping"
        if backend.mode == "stop_uncertain": raise ValueError("ordinary stop outcome uncertain after issuance")
        return {"normal_api_result": True}
    def launch(context):
        calls.append(("launch", copy.deepcopy(context)))
        assert backend.phase == "absent"
        backend.new_identity = {**copy.deepcopy(old), "pid": 222, "launch_time": clock.time()}
        backend.new_kernel = _kernel(backend.new_identity)
        backend.phase = "new"
        if backend.mode == "launch_uncertain": raise ValueError("normal launch completion lost after issuance")
        if backend.mode == "open_only": return {"result": {"returncode": 0}}
        return {"application_pid": backend.new_identity["pid"] + (1 if backend.mode == "wrong_completion" else 0),
                "result": {"normal_completion_application": True}}
    return {"app": app, "old": old, "old_kernel": old_kernel, "intent": intent, "out": tmp_path / "restart",
            "clock": clock, "backend": backend, "calls": calls, "stop": stop, "launch": launch, "source": source}


def _run(data):
    return X.run_restart(data["app"], data["old"], data["intent"], data["out"],
        stop_hook=data["stop"], launch_hook=data["launch"])


def _completed(data):
    outcome = _run(data)
    assert outcome["status"] == "restart_measurement_validated"
    return {"data": data, "outcome": outcome, "receipt": R.load_json(outcome["receipt"]["file"])}


def _repin(completed):
    """Rehash physically retained inert bytes so semantic errors reach gates."""
    receipt, root = completed["receipt"], completed["data"]["out"]
    queries = []
    for stage in X.STAGES:
        for index, pin in enumerate(receipt["observations"][stage]):
            path = Path(pin["file"])
            row = R.load_json(path)
            query_path = Path(row["query"]["file"])
            query = R.load_json(query_path)
            for name in ("stdout", "stderr"): query[name] = X._pin(query_path.parent / name)
            row["query"] = X._save(query_path, query)
            receipt["observations"][stage][index] = X._save(path, row)
            queries.append(row["query"])
    for phase in ("stop", "launch"):
        path = Path(receipt["callbacks"][phase]["file"])
        row = R.load_json(path)
        row["return"] = X._pin(row["return"]["file"])
        receipt["callbacks"][phase] = X._save(path, row)
    cleanup_path = Path(receipt["owned_cleanup"]["file"])
    cleanup = R.load_json(cleanup_path)
    cleanup["queries"] = queries
    receipt["owned_cleanup"] = X._save(cleanup_path, cleanup)
    completed["outcome"]["receipt"] = X._save(root / "receipt.json", receipt)


def _validate(completed):
    return X.validate_restart(completed["data"]["intent"], completed["outcome"]["receipt"])


def _copied_restart_sources(tmp_path, monkeypatch, *, legacy=False):
    sources = {}
    for name, pin in X._sources().items():
        if name == "/bin/ps":
            sources[name] = copy.deepcopy(pin)
            continue
        file = tmp_path / "relocated-package/execution" / Path(name).name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(Path(name).read_bytes())
        if legacy and file.name == "rea_process_restart.py":
            file.write_bytes(b"Inert reviewed legacy restart producer; never imported or executed.\n")
        sources[str(file)] = X._pin(file)
    if legacy:
        profile = {"execution/" + Path(name).name: {key: pin[key] for key in ("bytes", "sha256")}
                   for name, pin in sources.items() if name != "/bin/ps"}
        monkeypatch.setattr(X, "_LEGACY_SOURCE_PROFILE", profile)
    return sources


def test_reviewed_restart_profile_is_immutable_and_current_closure_has_advanced():
    assert len(X._LEGACY_SOURCE_PROFILE) == 9
    assert X._LEGACY_SOURCE_PROFILE[X._VALIDATOR_ROLE] == {
        "bytes": 45230, "sha256": "bb57a8a0a8731559c9bfa238e70ad7522edd6c0ca85c926f97564c812718cf1f"}
    assert R.digest(X._LEGACY_SOURCE_PROFILE) == "4623050e16901e11d96dac511a4768a8f25b684cd4fd1af200028936ae1c83b5"
    current, _ = X._producer_profile({name: pin for name, pin in X._sources().items() if name != "/bin/ps"})
    assert set(current) == set(X._LEGACY_SOURCE_PROFILE)
    # expected_class support changes the native bridge. Historical pins remain
    # fixed, and no longer describe the current non-validator core.
    assert current["execution/rea_runtime_bridge.py"] != X._LEGACY_SOURCE_PROFILE["execution/rea_runtime_bridge.py"]


def test_actual_historical_restart_profile_is_refused_after_core_advance_before_evidence_reads(tmp_path):
    # Declared old pins exercise the real compatibility branch, not an old
    # execution. No legacy file bytes are reconstructed or imported here.
    historical = {str(tmp_path / "historical-package" / role): {
        "file": str(tmp_path / "historical-package" / role), **pin}
        for role, pin in X._LEGACY_SOURCE_PROFILE.items()}
    current = X._sources()
    historical["/bin/ps"] = copy.deepcopy(current["/bin/ps"])
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("incompatible historical profile reached evidence reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        X._admit_sources(NoReads(), historical, current, tool="/bin/ps")


def test_current_restart_producers_are_admitted_against_current_physical_source_closure():
    current = X._sources()
    reader = X.E._Reader()
    admission = X._admit_sources(reader, current, current, tool="/bin/ps")
    assert admission["admission"] == "current_exact_roles" and admission["legacy_profile"] is None
    assert admission["producer_sources"] == admission["validator_sources"] == current
    assert admission["old_sources_imported"] is False
    reader.recheck()


def _b2_restart_sources(tmp_path, *, version="b2"):
    sources = {"/bin/ps": copy.deepcopy(X._sources()["/bin/ps"])}
    profile = X._REVIEWED_EXECUTION_RESTORATION_PAIR["producer"] if version == "b2" else X._REVIEWED_V19_SOURCE_PROFILE
    head = "b2b6600a28216618e0a961008443170857934995" if version == "b2" else "b53f41905d5b9d3c81b71d897c2e1f91185fad2e"
    for role, expected in profile.items():
        raw = _reviewed_bytes(head, role)
        file = tmp_path / ("reviewed-" + version + "-package") / role
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
        pin = X._pin(file)
        assert {key: pin[key] for key in ("bytes", "sha256")} == expected
        sources[str(file)] = pin
    return sources


def _reviewed_restart_installed_sources(tmp_path, variant):
    """Retain exact old/current source bytes without executing old producers."""
    sources = {}
    profile, head = {
        "execution_restoration": (X._REVIEWED_EXECUTION_RESTORATION_PAIR["installed_core"], "b56a7bc6bda9eb8cef5d90d6a8464e13b52afa11"),
        "visual_verification": (X._REVIEWED_VISUAL_VERIFICATION_CORE, "e314b3c0627b5c094f7139e86a4cf104b6e0e9c9"),
        "generic_workflow": (X._REVIEWED_GENERIC_WORKFLOW_CORE, "c8b433d778a356bff983173bc8286d7d537f748e"),
    }[variant]
    for name, pin in X._sources().items():
        if name == "/bin/ps":
            sources[name] = copy.deepcopy(pin)
            continue
        role = "execution/" + Path(name).name
        if role != X._VALIDATOR_ROLE:
            raw = _reviewed_bytes(head, role)
        else:
            raw = Path(name).read_bytes()
        file = tmp_path / ("installed-" + variant) / role
        file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
        sources[str(file)] = X._pin(file)
        if role != X._VALIDATOR_ROLE:
            assert {key: sources[str(file)][key] for key in ("bytes", "sha256")} == profile[role]
    return sources


def test_reviewed_restart_core_variants_preserve_original_pair_and_distinct_full_maps():
    assert R.digest(X._REVIEWED_EXECUTION_RESTORATION_PAIR) == "aa55e0892e3048eed0cebb47a44f2ad3f68b47ff67a143925be5e69865bcbf48"
    assert R.digest(X._REVIEWED_VISUAL_VERIFICATION_CORE) == "83a01f4129138649af0b48053a40e78a888c443168c3f791c1bd1eed5c923107"
    old = X._REVIEWED_EXECUTION_RESTORATION_PAIR["installed_core"]
    new = X._REVIEWED_VISUAL_VERIFICATION_CORE
    assert set(old) == set(new) == set(X._LEGACY_SOURCE_PROFILE) - {X._VALIDATOR_ROLE}
    assert {role for role in old if old[role] != new[role]} == {"execution/rea_mapping.py"}
    generic = X._REVIEWED_GENERIC_WORKFLOW_CORE
    assert R.digest(generic) == "91051a78718354dde6c2bb4922983d8b4a7c54bebd28a584f0f7e82ff20d5747"
    assert set(generic) == set(new)
    assert {role for role in new if new[role] != generic[role]} == {
        "execution/rea_runtime_bridge.py", "execution/rea_native_path.py", "execution/rea_qt_moc.py"}


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("variant", ["execution_restoration", "visual_verification", "generic_workflow"])
def test_reviewed_restart_producers_admit_separate_exact_complete_installed_cores(tmp_path, version, variant):
    sources = _b2_restart_sources(tmp_path, version=version)
    current = _reviewed_restart_installed_sources(tmp_path, variant)
    reader = X.E._Reader()
    admission = X._admit_sources(reader, sources, current, tool="/bin/ps")
    expected = "reviewed_b2_execution_restoration_exact_core" if version == "b2" else "reviewed_v19_restart_exact_core"
    assert admission["admission"] == expected and admission["old_sources_imported"] is False
    assert admission["legacy_profile"].endswith("/visual-verification-core") is (variant == "visual_verification")
    assert admission["legacy_profile"].endswith("/generic-workflow-core") is (variant == "generic_workflow")
    assert admission["producer_sources"] == sources and admission["validator_sources"] == current
    assert set(reader.files) == set(sources) | set(current)
    reader.recheck()


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("fault", ["modified_mapping", "producer_mapping_in_new_core", "producer_bridge_in_old_core"])
def test_reviewed_restart_producers_refuse_modified_or_mixed_complete_core_before_reads(tmp_path, version, fault):
    sources = _b2_restart_sources(tmp_path, version=version)
    variant = "execution_restoration" if fault == "producer_bridge_in_old_core" else "visual_verification"
    current = _reviewed_restart_installed_sources(tmp_path, variant)
    name = "rea_runtime_bridge.py" if fault == "producer_bridge_in_old_core" else "rea_mapping.py"
    installed = next(key for key in current if Path(key).name == name)
    if fault == "modified_mapping":
        Path(installed).write_bytes(Path(installed).read_bytes() + b"\n# unreviewed core byte\n")
        current[installed] = X._pin(installed)
    else:
        # A V19 bridge or either original producer's R is not an installed-core
        # role alternative, even though its bytes belong to reviewed history.
        producer = _b2_restart_sources(tmp_path, version="v19")
        old = next(key for key in producer if Path(key).name == name)
        Path(installed).write_bytes(Path(old).read_bytes()); current[installed] = X._pin(installed)
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed complete core reached source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        X._admit_sources(NoReads(), sources, current, tool="/bin/ps")


@pytest.mark.parametrize("version", ["b2", "v19"])
@pytest.mark.parametrize("role", ["rea_runtime_bridge.py", "rea_native_path.py", "rea_qt_moc.py"])
@pytest.mark.parametrize("fault", ["modified", "visual_role"])
def test_generic_restart_core_refuses_unreviewed_bytes_and_mixed_roles_before_reads(tmp_path, version, role, fault):
    sources = _b2_restart_sources(tmp_path, version=version)
    current = _reviewed_restart_installed_sources(tmp_path, "generic_workflow")
    installed = next(key for key in current if Path(key).name == role)
    if fault == "modified":
        raw = Path(installed).read_bytes() + b"\n# unreviewed generic core\n"
    else:
        visual = _reviewed_restart_installed_sources(tmp_path, "visual_verification")
        raw = Path(next(key for key in visual if Path(key).name == role)).read_bytes()
    Path(installed).write_bytes(raw); current[installed] = X._pin(installed)
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed or mixed generic core reached source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        X._admit_sources(NoReads(), sources, current, tool="/bin/ps")


@pytest.mark.parametrize("version", ["b2", "v19"])
def test_historical_restart_producers_refuse_advanced_current_core_before_reads(tmp_path, version):
    sources, current = _b2_restart_sources(tmp_path, version=version), X._sources()
    profile, _ = X._producer_profile({name: pin for name, pin in current.items() if name != "/bin/ps"})
    assert X._reviewed_core_variant(profile) is None
    class NoReads:
        def read(self, *args, **kwargs):
            pytest.fail("unreviewed current core reached historical source reads")
    with pytest.raises(ValueError, match="not current or reviewed compatible legacy"):
        X._admit_sources(NoReads(), sources, current, tool="/bin/ps")


def test_actual_b2_restart_producers_admit_with_exact_tool_and_reviewed_core(tmp_path):
    assert R.digest(X._REVIEWED_EXECUTION_RESTORATION_PAIR["producer"]) == "c1094368f39fbbcebbf1865219920ee14b07a5277a3767e6585cb8da31f60a77"
    sources = _b2_restart_sources(tmp_path)
    current = _reviewed_restart_installed_sources(tmp_path, "generic_workflow")
    reader = X.E._Reader()
    admission = X._admit_sources(reader, sources, current, tool="/bin/ps")
    assert admission["admission"] == "reviewed_b2_execution_restoration_exact_core"
    assert admission["producer_sources"] == sources and admission["validator_sources"] == current
    assert set(reader.files) == set(sources) | set(current) and admission["old_sources_imported"] is False
    reader.recheck()


@pytest.mark.parametrize("fault", ["producer_pin", "producer_bytes", "mixed_profile", "installed_core", "tool"])
def test_actual_b2_restart_pair_refuses_tampered_mixed_or_foreign_tool(tmp_path, fault):
    sources = _b2_restart_sources(tmp_path)
    current = _reviewed_restart_installed_sources(tmp_path, "generic_workflow")
    reader = X.E._Reader()
    assert X._admit_sources(reader, sources, current, tool="/bin/ps")["admission"] == "reviewed_b2_execution_restoration_exact_core"
    reader.recheck()
    old = next(name for name in sources if Path(name).name == "rea_mapping.py")
    if fault == "producer_pin": sources[old]["sha256"] = "0" * 64
    elif fault == "producer_bytes": Path(old).write_bytes(Path(old).read_bytes() + b"changed")
    elif fault == "mixed_profile":
        Path(old).write_bytes(Path(R.__file__).read_bytes()); sources[old] = X._pin(old)
    elif fault == "tool": sources["/bin/ps"]["sha256"] = "0" * 64
    else:
        installed = next(name for name in current if Path(name).name == "rea_mapping.py")
        current[installed]["sha256"] = "0" * 64
    with pytest.raises(ValueError): X._admit_sources(X.E._Reader(), sources, current, tool="/bin/ps")


def test_b2_restart_migration_keeps_full_historical_epoch_checks(restart_data, tmp_path, monkeypatch):
    completed = _completed(restart_data)
    # Exercise historical validation against retained reviewed source bytes;
    # today's advanced runtime is not a reviewed historical installed core.
    installed = _reviewed_restart_installed_sources(tmp_path, "generic_workflow")
    monkeypatch.setattr(X, "_sources", lambda: copy.deepcopy(installed))
    receipt = completed["receipt"]
    path = Path(receipt["baseline"]["file"])
    baseline = R.load_json(path); baseline["sources"] = _b2_restart_sources(tmp_path)
    receipt["baseline"] = X._save(path, baseline)
    completed["outcome"]["receipt"] = X._save(restart_data["out"] / "receipt.json", receipt)
    report = _validate(completed)
    assert report["source_admission"]["admission"] == "reviewed_b2_execution_restoration_exact_core"
    callback_path = Path(receipt["callbacks"]["launch"]["file"])
    callback = R.load_json(callback_path); returned_path = Path(callback["return"]["file"])
    returned = R.load_json(returned_path); returned["application_pid"] += 1
    X._save(returned_path, returned); _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


def test_actual_v19_restart_profile_has_separate_exact_admission_and_epoch_checks(restart_data, tmp_path, monkeypatch):
    assert R.digest(X._REVIEWED_V19_SOURCE_PROFILE) == "43547a94796576743ecee94b1a522aebc257260935941f2f67bd613df2bbb98d"
    completed = _completed(restart_data)
    installed = _reviewed_restart_installed_sources(tmp_path, "generic_workflow")
    monkeypatch.setattr(X, "_sources", lambda: copy.deepcopy(installed))
    receipt = completed["receipt"]; path = Path(receipt["baseline"]["file"])
    baseline = R.load_json(path); baseline["sources"] = _b2_restart_sources(tmp_path, version="v19")
    receipt["baseline"] = X._save(path, baseline)
    completed["outcome"]["receipt"] = X._save(restart_data["out"] / "receipt.json", receipt)
    report = _validate(completed)
    assert report["source_admission"]["admission"] == "reviewed_v19_restart_exact_core"
    receipt["new_identity"]["launch_time"] += 1
    completed["outcome"]["receipt"] = X._save(restart_data["out"] / "receipt.json", receipt)
    with pytest.raises(ValueError): _validate(completed)


@pytest.mark.parametrize("legacy", [False, True])
def test_restart_relocated_source_profile_preserves_fixed_system_tool(restart_data, tmp_path, monkeypatch, legacy):
    sources = _copied_restart_sources(tmp_path, monkeypatch, legacy=legacy)
    before = copy.deepcopy(sources)
    reader = X.E._Reader()
    admission = X._resources(reader, restart_data["intent"], sources)
    assert sources == before and admission["producer_sources"] == before
    assert admission["admission"] == ("reviewed_v1_exact_core" if legacy else "current_exact_roles")
    assert admission["validator_sources"]["/bin/ps"] == X._sources()["/bin/ps"]
    assert not admission["old_sources_imported"]
    reader.recheck()


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "mixed_root", "key_file", "changed", "unknown_profile", "symlink", "ps_hash", "ps_path"])
def test_restart_source_closure_rejects_incomplete_alias_or_foreign_tool(restart_data, tmp_path, monkeypatch, fault):
    sources = _copied_restart_sources(tmp_path, monkeypatch)
    name = next(key for key in sources if key != "/bin/ps")
    file = Path(name)
    if fault == "missing": sources.pop(name)
    elif fault == "extra":
        extra = file.with_name("unexpected.py"); extra.write_bytes(b"extra")
        sources[str(extra)] = X._pin(extra)
    elif fault == "duplicate":
        removed = next(key for key in sources if key not in {name, "/bin/ps"}); sources.pop(removed)
        other = tmp_path / "duplicate/execution" / file.name
        other.parent.mkdir(parents=True); other.write_bytes(file.read_bytes())
        sources[str(other)] = X._pin(other)
    elif fault == "mixed_root":
        other = tmp_path / "other/execution" / file.name
        other.parent.mkdir(parents=True); other.write_bytes(file.read_bytes())
        sources.pop(name); sources[str(other)] = X._pin(other)
    elif fault == "key_file": sources[name]["file"] = name + ".different"
    elif fault in {"changed", "unknown_profile"}:
        file.write_bytes(file.read_bytes() + b"changed")
        if fault == "unknown_profile": sources[name] = X._pin(file)
    elif fault == "symlink":
        original = file.with_name("outside-source.fixture"); file.rename(original); file.symlink_to(original)
    elif fault == "ps_hash": sources["/bin/ps"]["sha256"] = "0" * 64
    elif fault == "ps_path":
        sources["/tmp/ps"] = sources.pop("/bin/ps"); sources["/tmp/ps"]["file"] = "/tmp/ps"
    with pytest.raises((ValueError, OSError)):
        X._resources(X.E._Reader(), restart_data["intent"], sources)


def test_reviewed_restart_profile_does_not_authorize_changed_current_core(restart_data, tmp_path, monkeypatch):
    sources = _copied_restart_sources(tmp_path, monkeypatch, legacy=True)
    current = X._sources()
    name = next(name for name in current if Path(name).name == "rea_mapping.py")
    current[name]["sha256"] = "0" * 64
    monkeypatch.setattr(X, "_sources", lambda: current)
    with pytest.raises(ValueError, match="compatible legacy"):
        X._resources(X.E._Reader(), restart_data["intent"], sources)


def test_legacy_restart_profile_keeps_full_historical_epoch_and_cleanup_gates(restart_data, tmp_path, monkeypatch):
    completed = _completed(restart_data)
    receipt = completed["receipt"]
    path = Path(receipt["baseline"]["file"])
    baseline = R.load_json(path)
    baseline["sources"] = _copied_restart_sources(tmp_path, monkeypatch, legacy=True)
    receipt["baseline"] = X._save(path, baseline)
    completed["outcome"]["receipt"] = X._save(restart_data["out"] / "receipt.json", receipt)
    report = _validate(completed)
    assert report["source_admission"]["admission"] == "reviewed_v1_exact_core"
    assert report["old_identity"] == restart_data["old"]
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]
    # A recognized producer cannot bless an invented launch completion PID.
    callback_path = Path(receipt["callbacks"]["launch"]["file"])
    callback = R.load_json(callback_path)
    returned_path = Path(callback["return"]["file"])
    returned = R.load_json(returned_path); returned["application_pid"] += 1
    X._save(returned_path, returned)
    _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


def _exiting_stop(data, monkeypatch, *, registered=True, never_absent=False, fault=None):
    """Keep raw ps comm while independent NS registration may lag or vanish."""
    backend = data["backend"]
    query, registrations, tick = backend.query, backend.registrations, backend.tick
    def observed_registrations(bundle_id):
        if backend.phase != "stopping": return registrations(bundle_id)
        if not registered: return []
        old = copy.deepcopy(data["old"])
        if fault == "ns_epoch": old["launch_time"] += .25
        return [_ns(old)]
    def observed_query(pid, folder, deadline):
        if backend.phase != "stopping": return query(pid, folder, deadline)
        kernel = copy.deepcopy(data["old_kernel"])
        kernel["executable"] = "(" + Path(data["old"]["executable"]).name + ")"
        if fault in {"pid", "ppid", "uid"}: kernel[fault] += 1
        elif fault == "start": kernel["started_at"] = _kernel({**data["old"], "launch_time": data["old"]["launch_time"] + 1})["started_at"]
        elif fault == "comm": kernel["executable"] = "(DifferentApp)"
        elif fault == "relative": kernel["executable"] = Path(data["old"]["executable"]).name
        return _fake_query(pid, folder, deadline, data["clock"], kernel)
    def observed_tick(deadline):
        was_stopping = backend.phase == "stopping"
        tick(deadline)
        if was_stopping and never_absent: backend.phase = "stopping"
    monkeypatch.setattr(backend, "registrations", observed_registrations)
    monkeypatch.setattr(backend, "query", observed_query)
    monkeypatch.setattr(backend, "tick", observed_tick)


@pytest.mark.parametrize("registered", [True, False])
def test_exiting_raw_comm_stays_present_until_complete_ns_and_kernel_absence(restart_data, monkeypatch, registered):
    _exiting_stop(restart_data, monkeypatch, registered=registered)
    completed = _completed(restart_data)
    pins = completed["receipt"]["observations"]["absence"]
    first, absent = (R.load_json(pin["file"]) for pin in pins)
    assert first["kernel"] == {key: restart_data["old_kernel"][key] for key in ("pid", "ppid", "uid", "started_at")} | {"exiting_comm": "(Witness)"}
    assert first["identity"] is None and bool(first["registrations"]) is registered
    assert X._old_or_absent(first, restart_data["old"], restart_data["old_kernel"]) is False
    assert absent["kernel"] is None and absent["registrations"] == []
    query = R.load_json(first["query"]["file"])
    assert query["command"] == X._argv(restart_data["old"]["pid"])
    assert query["owned"]["returncode"] == 0 and Path(query["stdout"]["file"]).read_bytes().endswith(b"(Witness)\n")
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]
    def forbidden(*args, **kwargs): raise AssertionError("historical exit handling queried the live process")
    monkeypatch.setattr(X, "_Mac", forbidden)
    monkeypatch.setattr(X, "_ps_query", forbidden)
    assert _validate(completed)["restart_measurement_validated"] is True


def test_exiting_kernel_row_without_ns_never_counts_as_absence_or_issues_launch(restart_data, monkeypatch):
    _exiting_stop(restart_data, monkeypatch, registered=False, never_absent=True)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    assert [name for name, _ in restart_data["calls"]] == ["stop"]
    receipt = R.load_json(outcome["receipt"]["file"])
    rows = [R.load_json(pin["file"]) for pin in receipt["observations"]["absence"]]
    assert len(rows) == X.MAX_SAMPLES and all(row["kernel"]["exiting_comm"] == "(Witness)" and row["identity"] is None for row in rows)
    assert receipt["callbacks"]["launch"] is None and receipt["observations"]["new"] == []


def test_exiting_comm_reappearing_at_prelaunch_still_blocks_the_launch(restart_data, monkeypatch):
    backend, query = restart_data["backend"], restart_data["backend"].query
    def observed_query(pid, folder, deadline):
        if Path(folder).name == "prelaunch-00-ps":
            kernel = {**restart_data["old_kernel"], "executable": "(Witness)"}
            return _fake_query(pid, folder, deadline, restart_data["clock"], kernel)
        return query(pid, folder, deadline)
    monkeypatch.setattr(backend, "query", observed_query)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and [name for name, _ in restart_data["calls"]] == ["stop"]
    receipt = R.load_json(outcome["receipt"]["file"])
    row = R.load_json(receipt["observations"]["prelaunch"][0]["file"])
    assert row["kernel"]["exiting_comm"] == "(Witness)" and row["identity"] is None and row["registrations"] == []
    assert "reappeared before" in receipt["errors"][-1]["message"]


@pytest.mark.parametrize("fault", ["pid", "ppid", "uid", "start", "comm", "relative", "ns_epoch"])
def test_exiting_comm_requires_the_exact_old_owner_epoch_and_registration(restart_data, monkeypatch, fault):
    _exiting_stop(restart_data, monkeypatch, fault=fault)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    assert [name for name, _ in restart_data["calls"]] == ["stop"]
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["callbacks"]["launch"] is None and receipt["new_identity"] is None


@pytest.mark.parametrize("stage", ["before", "new", "final"])
def test_rehashed_exiting_comm_cannot_replace_any_canonical_process_identity(restart_data, stage):
    completed = _completed(restart_data)
    path = Path(completed["receipt"]["observations"][stage][0]["file"])
    row = R.load_json(path)
    kernel = row["kernel"]
    (Path(row["query"]["file"]).parent / "stdout").write_bytes(_stdout({**kernel, "executable": "(Witness)"}))
    row["kernel"] = {key: kernel[key] for key in ("pid", "ppid", "uid", "started_at")} | {"exiting_comm": "(Witness)"}
    row["identity"] = None
    X._save(path, row); _repin(completed)
    with pytest.raises(ValueError, match="canonical observed kernel executable"): _validate(completed)


@pytest.mark.parametrize("forgery", ["canonical_identity", "absence", "changed_owner", "changed_epoch"])
def test_rehashed_exiting_observation_cannot_be_normalized_forged_or_reassigned(restart_data, monkeypatch, forgery):
    _exiting_stop(restart_data, monkeypatch)
    completed = _completed(restart_data)
    path = Path(completed["receipt"]["observations"]["absence"][0]["file"])
    row = R.load_json(path)
    if forgery == "canonical_identity": row.update(kernel=copy.deepcopy(restart_data["old_kernel"]), identity=copy.deepcopy(restart_data["old"]))
    elif forgery == "absence": row.update(kernel=None, identity=None, registrations=[])
    else:
        if forgery == "changed_owner": row["kernel"]["uid"] += 1
        else: row["kernel"]["started_at"] = _kernel({**restart_data["old"], "launch_time": restart_data["old"]["launch_time"] + 1})["started_at"]
        raw = {key: row["kernel"][key] for key in ("pid", "ppid", "uid", "started_at")} | {"executable": row["kernel"]["exiting_comm"]}
        (Path(row["query"]["file"]).parent / "stdout").write_bytes(_stdout(raw))
    X._save(path, row); _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


def test_actual_inert_chain_binds_owned_launch_completion_and_raw_absence_without_task_credit(restart_data, monkeypatch):
    completed = _completed(restart_data)
    result = R.load_json(completed["outcome"]["measurement"]["file"])
    assert result["restart_measurement_validated"] is True
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]
    assert result["old_identity"] == restart_data["old"] and result["new_identity"] == restart_data["backend"].new_identity
    assert result["launch_completion_pid"] == result["new_identity"]["pid"] == 222
    assert result["new_identity"]["launch_time"] > result["old_identity"]["launch_time"]
    assert result["stop_attempts"] == result["launch_attempts"] == 1
    assert len(completed["receipt"]["observations"]["absence"]) == 2
    assert len(completed["receipt"]["observations"]["new"]) == 2
    for key in ("proof_created", "task_proof_created", "public_proof_created", "card_created",
                "trial_aggregation_validated", "input_generalization_claimed"):
        assert result[key] is False
    def forbidden(*args, **kwargs): raise AssertionError("historical restart validation performed a live lookup/action")
    monkeypatch.setattr(B, "process_identity", forbidden)
    monkeypatch.setattr(X, "_Mac", forbidden)
    monkeypatch.setattr(X, "_ps_query", forbidden)
    assert _validate(completed) == result


@pytest.mark.parametrize("mode", ["stop_uncertain", "never_absent", "auto_successor", "launch_uncertain", "open_only",
                                   "wrong_completion", "foreign_registration", "epoch_replacement",
                                   "ns_epoch_before_kernel", "ns_disappears_before_kernel"])
def test_partial_uncertainty_and_successor_changes_never_retry_adopt_or_clean_up_app(restart_data, mode):
    restart_data["backend"].mode = mode
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["errors"] and receipt["proof_created"] is False
    stop = R.load_json(receipt["callbacks"]["stop"]["file"])
    assert stop["attempts"] == 1
    expected = ["stop"] if mode in {"stop_uncertain", "never_absent", "auto_successor"} else ["stop", "launch"]
    assert [name for name, _ in restart_data["calls"]] == expected
    if mode == "open_only": assert "provenance uncertain" in receipt["errors"][-1]["message"]
    if mode in {"ns_epoch_before_kernel", "ns_disappears_before_kernel"}:
        assert "complete NS" in receipt["errors"][-1]["message"]
    cleanup = R.load_json(receipt["owned_cleanup"]["file"])
    assert cleanup["app_processes_owned"] is False and cleanup["app_cleanup_attempts"] == 0
    for pin in cleanup["queries"]:
        query = R.load_json(pin["file"])
        assert query["owned"]["reaped"] is True and not query["owned"]["term_attempted"]


@pytest.mark.parametrize("fault", ["source", "target", "old_identity", "callback_name", "restart_flag"])
def test_changed_or_unauthorized_declarations_refuse_before_callbacks(restart_data, fault):
    data = restart_data
    if fault == "source": data["source"].write_text("changed hook producer")
    elif fault == "target": (data["app"] / "Contents/changed").write_text("changed app")
    elif fault == "old_identity": data["old"] = {**data["old"], "launch_time": data["old"]["launch_time"] + 1}
    elif fault == "callback_name": data["stop"] = "code.from.JSON"
    else: data["intent"]["force_kill"] = True
    with pytest.raises(ValueError): _run(data)
    assert data["calls"] == [] and not data["out"].exists()


@pytest.mark.parametrize("fault", ["stop_twice", "callback_order", "not_issued", "entry_after_deadline", "entry_wall_after_return",
                                   "unreaped_query", "query_signal", "raw_pid", "false_pid", "first_epoch", "cleanup_app_owned"])
def test_rehashed_semantically_corrupt_journals_cannot_become_restart_measurements(restart_data, fault):
    completed = _completed(restart_data)
    receipt = completed["receipt"]
    if fault in {"stop_twice", "callback_order", "not_issued", "entry_after_deadline", "entry_wall_after_return"}:
        path = Path(receipt["callbacks"]["stop"]["file"])
        row = R.load_json(path)
        if fault == "stop_twice": row["attempts"] = 2
        elif fault == "not_issued": row.update(attempts=0, issuance="not_issued")
        elif fault == "entry_after_deadline": row["entry_check_monotonic"] = row["deadline_monotonic"]
        elif fault == "entry_wall_after_return": row["entry_check_wall"] = row["finished_wall"] + .1
        else:
            later = R.load_json(receipt["callbacks"]["launch"]["file"])["finished_monotonic"]
            row.update(started_monotonic=later, finished_monotonic=later + .001)
        X._save(path, row)
    elif fault == "cleanup_app_owned":
        path = Path(receipt["owned_cleanup"]["file"])
        row = R.load_json(path)
        row["app_processes_owned"] = True
        X._save(path, row)
    else:
        path = Path(receipt["observations"]["new"][0]["file"])
        row = R.load_json(path)
        query_path = Path(row["query"]["file"])
        query = R.load_json(query_path)
        if fault == "unreaped_query": query["owned"]["reaped"] = False
        elif fault == "query_signal": query["owned"]["term_attempted"] = True
        elif fault == "raw_pid": (query_path.parent / "stdout").write_bytes(_stdout({**row["kernel"], "pid": 333}))
        elif fault == "false_pid": row["queried_pid"] = False
        else:
            row["identity"]["launch_time"] += 1
            row["registrations"][0]["launch_time"] += 1
        X._save(query_path, query)
        X._save(path, row)
    _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


@pytest.mark.parametrize("only", ["ns", "kernel"])
def test_absence_needs_both_independent_readers_not_one_caller_bool(restart_data, only):
    completed = _completed(restart_data)
    path = Path(completed["receipt"]["observations"]["absence"][-1]["file"])
    row = R.load_json(path)
    if only == "ns":
        row["kernel"] = copy.deepcopy(restart_data["old_kernel"])
        query_path = Path(row["query"]["file"])
        query = R.load_json(query_path)
        query["owned"]["returncode"] = 0
        (query_path.parent / "stdout").write_bytes(_stdout(row["kernel"]))
        X._save(query_path, query)
    else: row["registrations"] = [_ns(restart_data["old"], terminated=True)]
    X._save(path, row)
    _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "producer_overlap"])
def test_historical_reader_refuses_aliases_and_role_overlap(restart_data, alias):
    completed = _completed(restart_data)
    pin = completed["receipt"]["observations"]["before"][0]
    path = Path(pin["file"])
    if alias == "symlink":
        saved = path.with_name("saved-before.json")
        path.rename(saved)
        path.symlink_to(saved)
    elif alias == "hardlink": os.link(path, path.with_name("linked-before.json"))
    else:
        completed["receipt"]["observations"]["before"][0] = copy.deepcopy(restart_data["intent"]["stop"]["producer"])
        completed["outcome"]["receipt"] = X._save(restart_data["out"] / "receipt.json", completed["receipt"])
    with pytest.raises(ValueError): _validate(completed)


@pytest.mark.parametrize("outcome", ["normal", "timeout", "cleanup_uncertain", "interrupt"])
def test_only_owned_ps_query_cleanup_is_bounded_and_actual_disposition_is_retained(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    events = []
    class Child:
        pid, returncode = 9182, None
        def __init__(self, argv, **kwargs):
            assert argv == X._argv(111) and kwargs["start_new_session"] is True
            assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["env"]["LC_ALL"] == "C" and kwargs["env"]["TZ"] == "UTC"
        def poll(self): return self.returncode
        def wait(self, *, timeout):
            events.append(("wait", timeout))
            if len(events) == 1:
                if outcome == "interrupt": raise KeyboardInterrupt("inert interrupted ps wait")
                if outcome != "normal": raise subprocess.TimeoutExpired("ps", timeout)
            if outcome == "cleanup_uncertain": raise subprocess.TimeoutExpired("ps-cleanup", timeout)
            self.returncode = 0
            return 0
    monkeypatch.setattr(X.subprocess, "Popen", Child)
    def killpg(pid, signum):
        assert pid == Child.pid
        events.append(("signal", signum))
    monkeypatch.setattr(X.os, "killpg", killpg)
    folder = tmp_path / "owned-ps"
    deadline = X.time.monotonic() + 10
    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt): X._ps_query(111, folder, deadline)
        query = R.load_json(folder / "receipt.json")
    else: query, _ = X._ps_query(111, folder, deadline)
    assert query["owned"]["pid"] == Child.pid and (folder / "stdout").is_file() and (folder / "stderr").is_file()
    if outcome == "normal":
        assert query["status"] == "returned" and query["owned"]["reaped"] is True and not query["owned"]["term_attempted"]
    elif outcome == "cleanup_uncertain":
        assert query["status"] == "failed" and query["owned"]["reaped"] is False and query["owned"]["errors"]
        assert query["owned"]["term_attempted"] and query["owned"]["kill_attempted"]
    else:
        assert query["status"] == "failed" and query["owned"]["reaped"] is True and query["owned"]["term_attempted"]


def test_vm_guard_precedes_every_invalid_input_callback_and_file_operation(monkeypatch):
    def deny(): raise PermissionError("VM only")
    def forbidden(*args, **kwargs): raise AssertionError("host restart boundary reached IO or callbacks")
    monkeypatch.setattr(R, "assert_guest", deny)
    monkeypatch.setattr(Path, "resolve", forbidden)
    with pytest.raises(PermissionError, match="VM only"):
        X.run_restart("/unused", {}, {}, "/unused", stop_hook=forbidden, launch_hook=forbidden)
    with pytest.raises(PermissionError, match="VM only"): X.validate_restart({}, {})


@pytest.mark.parametrize("fault", ["old_kernel_start", "future_kernel_start", "wrong_weekday", "invalid_date",
                                   "malformed_clock", "extra_word", "coherent_before_callback", "coherent_after_callback"])
def test_rehashed_actual_raw_epoch_must_match_ns_and_the_real_launch_callback_window(restart_data, fault):
    completed = _completed(restart_data)
    receipt = completed["receipt"]
    launch = R.load_json(receipt["callbacks"]["launch"]["file"])
    changed = copy.deepcopy(receipt["new_identity"])
    if fault == "coherent_before_callback": changed["launch_time"] = launch["started_wall"] - .25
    elif fault == "coherent_after_callback": changed["launch_time"] = launch["finished_wall"] + 5
    receipt["new_identity"] = changed
    for stage in ("new", "final"):
        for pin in receipt["observations"][stage]:
            path = Path(pin["file"])
            row = R.load_json(path)
            row["identity"] = copy.deepcopy(changed)
            row["registrations"] = [_ns(changed)]
            row["kernel"] = _kernel(changed)
            if fault == "old_kernel_start": row["kernel"]["started_at"] = restart_data["old_kernel"]["started_at"]
            elif fault == "future_kernel_start":
                row["kernel"] = _kernel({**changed, "launch_time": launch["finished_wall"] + 5})
            elif fault == "wrong_weekday": row["kernel"]["started_at"] = "Mon" + row["kernel"]["started_at"][3:]
            elif fault == "invalid_date": row["kernel"]["started_at"] = "Sun Feb 30 10:00:00 2026"
            elif fault == "malformed_clock": row["kernel"]["started_at"] = "Sun Oct 18 10:xx:00 2026"
            elif fault == "extra_word": row["kernel"]["started_at"] += " extra"
            query_path = Path(row["query"]["file"])
            (query_path.parent / "stdout").write_bytes(_stdout(row["kernel"]))
            X._save(path, row)
    _repin(completed)
    with pytest.raises(ValueError): _validate(completed)


def test_kernel_interval_is_exactly_half_open_and_independent_of_the_caller_locale(restart_data):
    kernel = restart_data["old_kernel"]
    start = X._kernel_start(kernel["started_at"])
    X._bind_epoch(kernel, start)
    X._bind_epoch(kernel, start + .999)
    for outside in (start - .001, start + 1):
        with pytest.raises(ValueError, match="one-second interval"): X._bind_epoch(kernel, outside)
    with pytest.raises(ValueError, match="C/UTC"): X._kernel_start("So Okt 18 10:01:30 2026")


def test_partial_kernel_startup_keeps_the_first_independent_ns_epoch_and_requires_a_later_complete_pair(restart_data):
    restart_data["backend"].mode = "late_kernel"
    completed = _completed(restart_data)
    rows = [R.load_json(pin["file"]) for pin in completed["receipt"]["observations"]["new"]]
    assert len(rows) == 3 and rows[0]["kernel"] is None and rows[0]["identity"] is None
    assert rows[0]["registrations"] == rows[1]["registrations"] == rows[2]["registrations"]
    assert rows[1]["identity"] == rows[2]["identity"] == completed["receipt"]["new_identity"]
    assert _validate(completed)["restart_measurement_validated"] is True


def test_rehashed_first_complete_ns_epoch_cannot_change_while_its_kernel_record_is_absent(restart_data):
    restart_data["backend"].mode = "late_kernel"
    completed = _completed(restart_data)
    path = Path(completed["receipt"]["observations"]["new"][0]["file"])
    row = R.load_json(path)
    assert row["kernel"] is None and row["identity"] is None and row["registrations"]
    row["registrations"][0]["launch_time"] += .000001
    X._save(path, row)
    _repin(completed)
    with pytest.raises(ValueError, match="first complete NS epoch"): _validate(completed)


@pytest.mark.parametrize("phase", ["stop", "launch"])
def test_callback_final_pin_failure_does_not_skip_other_cleanup_or_resource_audits(restart_data, monkeypatch, phase):
    original, counts = X._pin, {"stop": 0, "launch": 0}
    def pin(path):
        if Path(path).name in {"stop.json", "launch.json"}:
            name = Path(path).stem
            counts[name] += 1
            if name == phase and counts[name] == 4: raise OSError("inert final callback audit failure")
        return original(path)
    monkeypatch.setattr(X, "_pin", pin)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert counts == {"stop": 4, "launch": 4}
    assert [row["phase"] for row in receipt["preservation_errors"]] == [phase + ":callback"]
    assert receipt["source_unchanged"] is True and receipt["target_after"] == restart_data["intent"]["target"]
    cleanup = R.load_json(receipt["owned_cleanup"]["file"])
    assert cleanup["queries"] and all(R.load_json(pin["file"])["owned"]["reaped"] for pin in cleanup["queries"])
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]


def test_cleanup_journal_failure_retains_callbacks_and_still_audits_resources(restart_data, monkeypatch):
    original = X._save
    def save(path, body):
        if Path(path).name == "owned-cleanup.json": raise OSError("inert cleanup journal failure")
        return original(path, body)
    monkeypatch.setattr(X, "_save", save)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["owned_cleanup"] is None
    assert [row["phase"] for row in receipt["preservation_errors"]] == ["owned_cleanup"]
    assert receipt["source_unchanged"] is True and receipt["target_after"] == restart_data["intent"]["target"]
    assert all(R.load_json(pin["file"])["attempts"] == 1 for pin in receipt["callbacks"].values())
    for pins in receipt["observations"].values():
        for pin in pins:
            observation = R.load_json(pin["file"])
            assert R.load_json(observation["query"]["file"])["owned"]["reaped"] is True


def test_final_source_change_is_retained_after_both_callback_and_cleanup_audits(restart_data, monkeypatch):
    backend = restart_data["backend"]
    original = backend.registrations
    def registrations(bundle_id):
        result = original(bundle_id)
        if backend.phase == "new" and backend.new_queries >= 2:
            restart_data["source"].write_text("source changed after the actual new epoch pair")
        return result
    monkeypatch.setattr(backend, "registrations", registrations)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["source_unchanged"] is False and receipt["target_after"] is None
    assert [row["phase"] for row in receipt["preservation_errors"]] == ["resources"]
    assert all(receipt["callbacks"].values()) and R.load_json(receipt["owned_cleanup"]["file"])["queries"]


@pytest.mark.parametrize("fallback_fails", [False, True])
def test_final_receipt_failure_never_returns_a_stale_pin_and_retains_independent_failure_when_possible(restart_data, monkeypatch, fallback_fails):
    original = X._save
    def save(path, body):
        if Path(path).name == "receipt.json" and body.get("finished_monotonic") is not None and body.get("format") == X.RECEIPT:
            raise OSError("inert final receipt failure")
        if fallback_fails and Path(path).name == "finalization-failure.json": raise OSError("inert finalization journal failure")
        return original(path, body)
    monkeypatch.setattr(X, "_save", save)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["receipt"] is None and outcome["measurement"] is None
    phases = [row["phase"] for row in outcome["preservation_errors"]]
    assert phases == (["final_receipt", "finalization_journal"] if fallback_fails else ["final_receipt"])
    if fallback_fails: assert outcome["preservation"] is None
    else:
        retained = R.load_json(outcome["preservation"]["file"])
        assert retained["status"] == "failed" and retained["preservation_errors"] == outcome["preservation_errors"]
    assert (restart_data["out"] / "owned-cleanup.json").is_file()
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]
    with pytest.raises(ValueError): X.validate_restart(restart_data["intent"], X._pin(restart_data["out"] / "receipt.json"))


def test_failed_final_observation_write_recovers_actual_query_for_owned_cleanup(restart_data, monkeypatch):
    original, failures = X._save, []
    def save(path, body):
        if Path(path).name == "new-00.json" and body["status"] == "observed" and not failures:
            failures.append(True)
            raise OSError("inert observation final write failure")
        return original(path, body)
    monkeypatch.setattr(X, "_save", save)
    outcome = _run(restart_data)
    assert failures == [True] and outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["errors"][-1]["type"] == "OSError"
    assert len(receipt["observations"]["new"]) == 1
    query_pin = X._pin(restart_data["out"] / "new-00-ps/receipt.json")
    assert query_pin in R.load_json(receipt["owned_cleanup"]["file"])["queries"]
    assert R.load_json(query_pin["file"])["owned"]["reaped"] is True


def test_actual_query_interruption_propagates_only_after_attempt_and_cleanup_journals_are_retained(restart_data, monkeypatch):
    backend, interrupted = restart_data["backend"], []
    original = backend.query
    def query(pid, folder, deadline):
        result = original(pid, folder, deadline)
        if backend.phase == "new" and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("inert interruption after the owned query was reaped")
        return result
    monkeypatch.setattr(backend, "query", query)
    with pytest.raises(KeyboardInterrupt): _run(restart_data)
    receipt = R.load_json(restart_data["out"] / "receipt.json")
    assert receipt["status"] == "failed" and receipt["errors"][-1]["type"] == "KeyboardInterrupt"
    assert len(receipt["observations"]["new"]) == 1
    query_pin = X._pin(restart_data["out"] / "new-00-ps/receipt.json")
    assert query_pin in R.load_json(receipt["owned_cleanup"]["file"])["queries"]
    assert all(R.load_json(pin["file"])["attempts"] == 1 for pin in receipt["callbacks"].values())


def test_owned_query_final_write_failure_retains_actual_reap_in_an_independent_failure_artifact(tmp_path, monkeypatch):
    class Child:
        pid, returncode = 9182, None
        def __init__(self, argv, **kwargs): pass
        def wait(self, *, timeout):
            self.returncode = 0
            return 0
    monkeypatch.setattr(X.subprocess, "Popen", Child)
    original = X._save
    def save(path, body):
        if Path(path).name == "receipt.json" and body["finished_monotonic"] is not None:
            raise OSError("inert query final write failure")
        return original(path, body)
    monkeypatch.setattr(X, "_save", save)
    row, pin = X._ps_query(111, tmp_path / "ps-write-failure", X.time.monotonic() + 10)
    assert row == R.load_json(pin["file"]) and Path(pin["file"]).name == "preservation-failure.json"
    assert row["status"] == "failed" and row["owned"]["reaped"] is True and row["owned"]["returncode"] == 0
    assert row["owned"]["errors"] and row["owned"]["term_attempted"] is False and row["owned"]["kill_attempted"] is False


@pytest.mark.parametrize("phase,where", [("stop", "journal"), ("launch", "journal"), ("launch", "prelaunch")])
def test_deadline_crossing_during_prepared_journal_or_prelaunch_never_issues_a_late_callback(restart_data, monkeypatch, phase, where):
    original_save, prepared_rows = X._save, []
    def save(path, body):
        result = original_save(path, body)
        if Path(path).name == phase + ".json" and body["status"] == "prepared":
            prepared_rows.append(copy.deepcopy(body))
            if where == "journal": restart_data["clock"].now = body["deadline_monotonic"] + .001
        return result
    monkeypatch.setattr(X, "_save", save)
    backend, original_query = restart_data["backend"], restart_data["backend"].query
    def query(pid, folder, deadline):
        result = original_query(pid, folder, deadline)
        if where == "prelaunch" and Path(folder).name == "prelaunch-00-ps": restart_data["clock"].now = deadline + .001
        return result
    monkeypatch.setattr(backend, "query", query)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    assert [name for name, _ in restart_data["calls"]] == ([] if phase == "stop" else ["stop"])
    assert len(prepared_rows) == 1 and prepared_rows[0]["attempts"] is None and prepared_rows[0]["issuance"] == "unknown"
    receipt = R.load_json(outcome["receipt"]["file"])
    callback = R.load_json(receipt["callbacks"][phase]["file"])
    assert callback["status"] == callback["issuance"] == "not_issued" and callback["attempts"] == 0
    assert callback["entry_check_monotonic"] >= callback["deadline_monotonic"]
    assert callback["return"] is None and "before entry" in callback["error"]["message"]
    assert not receipt["observations"]["new"] and receipt["new_identity"] is None
    assert R.load_json(receipt["owned_cleanup"]["file"])["app_cleanup_attempts"] == 0


@pytest.mark.parametrize("phase", ["stop", "launch"])
@pytest.mark.parametrize("fault", ["changed", "deleted"])
def test_completed_callback_journal_is_frozen_and_cannot_be_replaced_or_silently_removed_at_final_audit(restart_data, monkeypatch, phase, fault):
    original, captured = X._hook, {}
    def hook(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == phase:
            captured[phase] = copy.deepcopy(result[2])
            path = Path(result[2]["file"])
            if fault == "changed": path.write_bytes(path.read_bytes() + b"\n")
            else: path.unlink()
        return result
    monkeypatch.setattr(X, "_hook", hook)
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    assert receipt["callbacks"][phase] == captured[phase]
    assert [row["phase"] for row in receipt["preservation_errors"]] == [phase + ":callback"]
    assert ("changed" if fault == "changed" else "missing") in receipt["preservation_errors"][0]["error"]["message"]
    assert receipt["source_unchanged"] is True and receipt["owned_cleanup"] is not None
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]


def test_launch_result_cannot_adopt_an_epoch_from_before_entry_even_if_it_appeared_during_journaling(restart_data, monkeypatch):
    original_save, journal_wall = X._save, []
    def save(path, body):
        result = original_save(path, body)
        if Path(path).name == "launch.json" and body["status"] == "prepared":
            journal_wall.append(body["started_wall"])
            restart_data["clock"].now += .25
        return result
    monkeypatch.setattr(X, "_save", save)
    original_launch = restart_data["launch"]
    def launch(context):
        result = original_launch(context)
        restart_data["backend"].new_identity["launch_time"] = journal_wall[0] + .1
        restart_data["backend"].new_kernel = _kernel(restart_data["backend"].new_identity)
        return result
    restart_data["launch"] = launch
    outcome = _run(restart_data)
    assert outcome["status"] == "failed" and outcome["measurement"] is None
    receipt = R.load_json(outcome["receipt"]["file"])
    callback = R.load_json(receipt["callbacks"]["launch"]["file"])
    assert callback["issuance"] == "attempted" and callback["attempts"] == 1
    assert callback["started_wall"] < restart_data["backend"].new_identity["launch_time"] < callback["entry_check_wall"]
    assert "callback-bound" in receipt["errors"][-1]["message"]
    assert [name for name, _ in restart_data["calls"]] == ["stop", "launch"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
