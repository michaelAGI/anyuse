"""Explicit VM-only setup of an owned Chromium renderer inspector.

This is an operator lifecycle, never discovery, replay fallback, or task proof.
Only fixed LaunchServices arguments are supported. Failure leaves the app for
explicit resource review; it never closes, retries, or switches transports.
Importing this module performs no native, network, or filesystem operation.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import time
from urllib.parse import unquote, urlsplit

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_inspector_bridge as I

POLICY = "anyuse.rea-inspector-lifecycle-policy/1"
FORMAT = "anyuse.rea-inspector-lifecycle/1"
MAX_METADATA = 2 * 1024**2
_POLICY_KEYS = {"format", "bundle", "target", "launch_profile", "port", "relative_url", "profile", "config"}
_TARGET_KEYS = {"bundle_id", "version", "build", "sha256", "files", "bytes"}


class _NotReady(ValueError):
    """Only absence of startup metadata may be polled within one launch."""


def _text(value, maximum=4096):
    return (isinstance(value, str) and 0 < len(value) <= maximum and
            all(32 <= ord(char) != 127 for char in value) and I._text(value, maximum))


def _path(value):
    if not isinstance(value, (str, Path)) or not _text(str(value)):
        raise ValueError("lifecycle paths need bounded absolute canonical text without control characters")
    path = Path(value)
    if not path.is_absolute() or str(path) != str(value) or path.resolve() != path:
        raise ValueError("lifecycle paths must be absolute canonical paths without symlink aliases")
    return path


def _inside(path, parent):
    return path == parent or path.is_relative_to(parent)


def _directory(path, *, mode=None):
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or (mode is not None and stat.S_IMODE(value.st_mode) != mode):
        raise ValueError("lifecycle directory must be an owned real directory with the declared mode")
    if value.st_mode & 0o022:
        raise ValueError("lifecycle directories must not be writable by other users")
    return {"file": str(path), "device": value.st_dev, "inode": value.st_ino, "uid": value.st_uid,
            "mode": format(stat.S_IMODE(value.st_mode), "04o")}


def _private(value):
    path = _path(value)
    if path.lstat().st_nlink != 1:
        raise ValueError("lifecycle input must be a singly linked owned private regular file")
    pin, raw = B._private_file(str(path), MAX_METADATA, "lifecycle input needs owned mode 0600")
    try:
        def unique(pairs):
            output = {}
            for key, item in pairs:
                if key in output:
                    raise ValueError("duplicate lifecycle JSON key")
                output[key] = item
            return output
        data = json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("lifecycle input must be bounded UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("lifecycle input must be a JSON object")
    return data, pin


def _source_identity():
    path = Path(__file__).resolve(strict=True)
    return {"file": str(path), "sha256": R._file_hash(path), "inspector": I.adapter_identity()}


def _target(value):
    if (not isinstance(value, dict) or set(value) != _TARGET_KEYS or
            not _text(value["bundle_id"], 512) or
            any(not I._text(value[key], 512, empty=True) for key in ("version", "build")) or
            not isinstance(value["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None or
            any(type(value[key]) is not int or value[key] <= 0 for key in ("files", "bytes")) or
            value["files"] > 100000 or value["bytes"] > R.MAX_BUNDLE_BYTES):
        raise ValueError("policy needs an exact complete target fingerprint")


def _policy(policy_file, out):
    policy, pin = _private(policy_file)
    if (set(policy) != _POLICY_KEYS or policy.get("format") != POLICY or
            policy.get("launch_profile") != "chromium_renderer_cdp" or
            type(policy.get("port")) is not int or not 1024 <= policy["port"] <= 65535):
        raise ValueError("unsupported closed inspector lifecycle policy")
    _target(policy["target"])
    bundle, profile, config, folder = (_path(policy["bundle"]), _path(policy["profile"]),
                                       _path(policy["config"]), _path(out))
    relative = policy["relative_url"]
    if (not _text(relative, 2048) or "\\" in relative or "%" in relative or "?" in relative or "#" in relative or
            relative != PurePosixPath(relative).as_posix() or PurePosixPath(relative).is_absolute() or
            any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("renderer must be an exact app-relative path")
    # app.asar paths are virtual filesystem paths; do not demand an OS HTML file.
    if bundle.suffix != ".app" or not bundle.is_dir() or not _inside((bundle / relative).resolve(), bundle):
        raise ValueError("renderer path must stay inside its canonical app bundle")
    paths = [bundle, profile, config, folder, Path(pin["file"])]
    if any(_inside(a, b) or _inside(b, a) for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ValueError("bundle, profile, config, policy and evidence paths must not overlap")
    for path in (profile, config, folder):
        _directory(path.parent)
    if folder.exists():
        raise ValueError("lifecycle evidence directory must be fresh")
    if R.fingerprint(bundle) != policy["target"]:
        raise ValueError("app bundle differs from exact policy fingerprint")
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    name = info.get("CFBundleExecutable")
    if not _text(name, 256) or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("target has no exact executable name")
    executable = _path(bundle / "Contents/MacOS" / name)
    if not executable.is_file():
        raise ValueError("target main executable is not a regular file")
    return policy, pin, bundle, profile, config, folder, executable


def _timeout(value, maximum):
    if type(value) not in {int, float} or not math.isfinite(value) or not 1 <= value <= maximum:
        raise ValueError("lifecycle timeout outside its fixed bound")
    return time.monotonic() + value


def _run(argv, deadline, *, env=None):
    result = subprocess.run(argv, capture_output=True, timeout=min(5, I._remaining(deadline)), env=env, check=False)
    if len(result.stdout) > MAX_METADATA or len(result.stderr) > 16384:
        raise ValueError("lifecycle command exceeded its metadata budget")
    return result


def _registrations(bundle_id):
    import AppKit
    rows = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if len(rows) > 64:
        raise ValueError("bundle registration budget exceeded")
    return [{"pid": int(app.processIdentifier()), "terminated": bool(app.isTerminated())} for app in rows]


def _kernel_candidates(bundle, deadline):
    result = _run(["/bin/ps", "-axo", "pid=,ppid=,uid=,lstart=,comm="], deadline, env=I._ps_environment())
    if result.returncode:
        raise ValueError("kernel app inventory unavailable")
    lines = result.stdout.decode("utf-8").splitlines()
    if len(lines) > 32768:
        raise ValueError("kernel process inventory budget exceeded")
    rows = []
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) == 9 and parts[8].startswith(str(bundle) + "/"):
            if not all(part.isdecimal() for part in parts[:3]):
                raise ValueError("target kernel process metadata malformed")
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "uid": int(parts[2]),
                         "started_at": " ".join(parts[3:8]), "executable": parts[8]})
    return rows


def _port_absent(port, deadline):
    result = _run(["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fp"], deadline)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def _absent(policy, bundle, deadline):
    if _registrations(policy["target"]["bundle_id"]):
        raise ValueError("an app with this bundle ID is already registered; no launch")
    if _kernel_candidates(bundle, deadline):
        raise ValueError("kernel app processes already exist; no launch")
    if not _port_absent(policy["port"], deadline):
        raise ValueError("inspector listener is occupied or absence is unconfirmed; no launch")


def _exact_main(bundle, target, executable, deadline):
    main = B.process_identity(bundle, target["bundle_id"])
    kernel = I._process(main["pid"], deadline)
    if main["executable"] != str(executable) or kernel["executable"] != str(executable) or kernel["uid"] != main["uid"]:
        raise ValueError("fresh NS and kernel app identities disagree")
    return main, kernel


def _environment():
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("NODE_", "ELECTRON_", "DYLD_")) and
            key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"}}


def _open(policy, bundle, profile, deadline):
    args = ["/usr/bin/open", "-n", str(bundle), "--args", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=" + str(policy["port"]), "--user-data-dir=" + str(profile),
            "--disable-background-networking", "--disable-component-update", "--no-proxy-server",
            "--host-resolver-rules=MAP * ~NOTFOUND"]
    result = _run(args, deadline, env=_environment())
    if result.returncode:
        raise ValueError("the single LaunchServices request failed")


def _config_data(policy, suffix):
    return {"format": I.TRANSPORT, "endpoint": "http://127.0.0.1:" + str(policy["port"]),
            "relative_url": policy["relative_url"], "target": policy["target"], "document_suffix": suffix}


def _initial_page(policy, bundle, deadline):
    endpoint = "http://127.0.0.1:" + str(policy["port"])
    rows = I._http_json(endpoint, "/json/list", deadline)
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("renderer inventory is malformed or exceeds its bound")
    wanted = bundle / policy["relative_url"]
    matches = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "page" or not I._text(row.get("url"), 16384):
            continue
        parsed = urlsplit(row["url"])
        if parsed.scheme == "file" and parsed.netloc == "" and unquote(parsed.path) == str(wanted):
            suffix = I._document_suffix({"query": parsed.query, "fragment": parsed.fragment})
            matches.append((row["url"], suffix))
    if not matches:
        raise _NotReady("exact app renderer has not appeared")
    if len(matches) != 1:
        raise ValueError("exact app renderer is ambiguous")
    return matches[0]


def _owned_processes(browser, bundle, main, renderers, deadline):
    rows = browser.call("SystemInfo.getProcessInfo").get("processInfo")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("bounded browser process inventory unavailable")
    browsers = [row for row in rows if isinstance(row, dict) and row.get("type") == "browser"]
    if len(browsers) != 1 or browsers[0].get("id") != main["pid"]:
        raise ValueError("browser main PID changed between metadata snapshots")
    processes = {main["pid"]: I._process(main["pid"], deadline)}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
            raise ValueError("browser process inventory has malformed process IDs")
        pid = row["id"]
        if pid in seen:
            raise ValueError("browser process inventory contains duplicate PIDs")
        seen.add(pid)
        if pid == main["pid"]:
            continue
        process = I._process(pid, deadline)
        chain, visited = process, set()
        for _ in range(16):
            if chain["uid"] != main["uid"] or not _inside(Path(chain["executable"]), bundle) or chain["pid"] in visited:
                raise ValueError("browser child is not an exact owned app descendant")
            visited.add(chain["pid"])
            if chain["ppid"] == main["pid"]:
                break
            chain = I._process(chain["ppid"], deadline)
        else:
            raise ValueError("browser process ancestry exceeded its bound")
        processes[pid] = process
    if any(row["pid"] not in processes for row in renderers):
        raise ValueError("renderer inventory changed between bounded snapshots")
    return processes


def _lsof_rows(raw):
    """Parse lsof -F0 without treating a newline inside a filename as a field."""
    if len(raw) > MAX_METADATA:
        raise ValueError("profile open-file metadata exceeded its bound")
    pid = uid = None
    current, output = None, []
    for field in raw.split(b"\0"):
        field = field.lstrip(b"\n")  # lsof record separators precede the next field tag.
        if not field:
            continue
        tag, value = chr(field[0]), field[1:]
        if tag in {"p", "f"} and current is not None:
            output.append(current)
            current = None
        if tag == "p":
            pid, uid = int(value), None
        elif tag == "u":
            uid = int(value)
        elif tag == "f":
            current = {"pid": pid, "uid": uid, "fd": value.decode("ascii")}
        elif current is not None and tag in {"t", "i", "D", "n"}:
            if tag in current:
                raise ValueError("ambiguous lsof file metadata")
            current[tag] = value.decode("utf-8")
        if len(output) > 32768:
            raise ValueError("profile open-file record budget exceeded")
    if current is not None:
        output.append(current)
    return output


def _profile_use(profile, processes, deadline):
    result = _run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(str(pid) for pid in sorted(processes)),
                   "-F0pufitDn"], deadline)
    if result.returncode or result.stderr:
        raise ValueError("positive owned profile open-file observation unavailable")
    artifacts = []
    for row in _lsof_rows(result.stdout):
        if row.get("t") != "REG" or row.get("pid") not in processes or row.get("uid") != os.getuid():
            continue
        name = row.get("n")
        if not _text(name) or not Path(name).is_absolute() or not _inside(Path(name), profile):
            continue
        path = _path(name)
        value = path.lstat()
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_nlink != 1 or
                int(row.get("i", "-1")) != value.st_ino or int(row.get("D", "-1"), 16) != value.st_dev):
            raise ValueError("observed profile artifact inode/device/ownership differs")
        if I._process(row["pid"], deadline) != processes[row["pid"]]:
            raise ValueError("profile-opening process identity changed")
        artifacts.append({"file": str(path), "pid": row["pid"], "device": value.st_dev,
                          "inode": value.st_ino, "uid": value.st_uid, "bytes": value.st_size})
        if len(artifacts) == 8:
            break
    if not artifacts:
        raise ValueError("no exact owned app process was observed opening a regular owned profile artifact")
    return {"status": "observed_use", "artifacts": artifacts, "contents_read": False,
            "claim_scope": "owned app process opened these exact regular profile artifacts"}


def _snapshot(policy, bundle, main, kernel, config, profile, deadline):
    endpoint = config["endpoint"]
    listener = I._listener(endpoint, main, deadline)
    browser = page = None
    cleanup = {"browser_socket_closed": False, "page_socket_closed": False}
    failure = None
    try:
        browser_url, selected = I._select_target(bundle, config, deadline)
        browser = I._CDP(I._socket(browser_url, deadline), deadline)
        renderers = I._renderer_processes(browser, bundle, main, deadline)
        processes = _owned_processes(browser, bundle, main, renderers, deadline)
        page = I._CDP(I._socket(selected["webSocketDebuggerUrl"], deadline), deadline)
        frame = page.call("Page.getFrameTree").get("frameTree", {}).get("frame")
        if not isinstance(frame, dict) or not I._text(frame.get("id"), 128) or I._effective_frame_url(frame) != selected["url"]:
            raise ValueError("selected renderer frame differs from the exact app URL")
        page.call("Runtime.enable")
        if page.call("Page.getFrameTree").get("frameTree", {}).get("frame") != frame:
            raise ValueError("renderer frame changed during metadata binding")
        contexts = [event.get("params", {}).get("context", {}) for event in page.events
                    if event.get("method") == "Runtime.executionContextCreated"]
        contexts = [context for context in contexts if isinstance(context, dict) and
                    context.get("auxData", {}).get("isDefault") is True and
                    context.get("auxData", {}).get("frameId") == frame["id"]]
        if (len(contexts) != 1 or type(contexts[0].get("id")) is not int or contexts[0]["id"] <= 0 or
                not I._text(contexts[0].get("uniqueId"), 256)):
            raise ValueError("exact renderer default context is absent or ambiguous")
        context = contexts[0]
        page.bound_context = {"unique_id": context["uniqueId"], "frame_id": frame["id"]}
        observed = _profile_use(profile, processes, deadline)
        if B.process_identity(bundle, policy["target"]["bundle_id"]) != main or I._process(main["pid"], deadline) != kernel:
            raise ValueError("owned app identity changed during metadata observation")
        if I._listener(endpoint, main, deadline) != listener or I._select_target(bundle, config, deadline)[1] != selected:
            raise ValueError("owned listener or exact renderer changed during metadata observation")
        if (page.call("Page.getFrameTree").get("frameTree", {}).get("frame") != frame or page.invalid_context or
                I._renderer_processes(browser, bundle, main, deadline) != renderers or
                _owned_processes(browser, bundle, main, renderers, deadline) != processes):
            raise ValueError("renderer frame/context/process set changed during metadata observation")
        result = {"identity": main, "kernel_identity": kernel, "listener": listener, "renderers": renderers,
                  "selected_url": selected["url"], "execution_context": {"unique_id": context["uniqueId"],
                      "numeric_id": context["id"], "frame_id": frame["id"], "frame_url": selected["url"]},
                  "observed_profile_use": observed, "cleanup": cleanup}
    except Exception as exc:
        failure = exc
    finally:
        for name, connection in (("page_socket_closed", page), ("browser_socket_closed", browser)):
            if connection is None:
                cleanup[name] = True
            else:
                try:
                    connection.close()
                    cleanup[name] = connection.closed is True
                except Exception:
                    cleanup[name] = False
    if failure is not None or not all(cleanup.values()):
        exc = ValueError("metadata socket closure was not acknowledged") if failure is None else failure
        exc.lifecycle_cleanup = cleanup
        raise exc
    return result


def _write(folder, value):
    if _path(folder) != folder or _directory(folder, mode=0o700) != value["evidence_identity"]:
        raise ValueError("lifecycle evidence directory identity changed")
    R.write_json(folder / "receipt.json", value)
    return value


def _base(action, folder, policy, pin):
    return {"format": FORMAT, "action": action, "status": "intent", "receipt": str(folder / "receipt.json"),
            "policy": pin, "target": policy["target"], "source": _source_identity(),
            "evidence_identity": _directory(folder, mode=0o700),
            "requested_profile": {"file": policy["profile"]}, "observed_profile_use": None,
            "isolation_verified": False, "launch_attempts": 0, "termination_requests": 0,
            "metadata_only": action == "inspect", "inspector_commands_metadata_only": True,
            "app_task_methods": 0, "model_calls": 0, "task_calls": 0,
            "task_result_verified": False, "proof_created": False, "automatic_fallback": False,
            "security_settings_changed": False, "vm_restarted": False, "resource_review_required": False}


def _failure(folder, result, exc):
    result.update(status="incomplete", error={"type": type(exc).__name__, "message": str(exc)[:1024]},
                  resource_review_required=True)
    if hasattr(exc, "lifecycle_cleanup"):
        result["cleanup"] = exc.lifecycle_cleanup
    return _write(folder, result)


def _unchanged(policy, pin, profile, profile_pin, *, config_pin=None):
    if _private(pin["file"])[1] != pin or R.fingerprint(policy["bundle"]) != policy["target"]:
        raise ValueError("policy or target changed during lifecycle operation")
    if _directory(profile, mode=0o700) != profile_pin:
        raise ValueError("owned profile directory identity changed")
    if config_pin is not None and _private(policy["config"])[1] != config_pin:
        raise ValueError("frozen inspector config changed")


def _saved(value, out):
    receipt, receipt_pin = _private(value)
    if (receipt.get("format") != FORMAT or receipt.get("action") != "launch" or receipt.get("status") != "ready" or
            receipt.get("receipt") != str(_path(value)) or receipt.get("source") != _source_identity() or
            receipt.get("isolation_verified") is not False or receipt.get("resource_review_required") is not False):
        raise ValueError("operation needs an unchanged successful lifecycle launch receipt")
    data = _policy(receipt.get("policy", {}).get("file"), out)
    policy, pin, bundle, profile, config, folder, executable = data
    if receipt.get("policy") != pin or receipt.get("target") != policy["target"] or receipt.get("config_file") != str(config):
        raise ValueError("launch policy or target no longer matches its frozen receipt")
    if _inside(folder, Path(receipt_pin["file"]).parent) or _inside(Path(receipt_pin["file"]).parent, folder):
        raise ValueError("new evidence must be outside the prior evidence directory")
    config_data, config_pin = I._config_for_target(policy["target"], str(config))
    if (receipt.get("transport") != I._transport(config_data, config_pin) or
            config_data["endpoint"] != "http://127.0.0.1:" + str(policy["port"]) or
            config_data["relative_url"] != policy["relative_url"] or
            receipt.get("profile_identity") != _directory(profile, mode=0o700)):
        raise ValueError("saved config, profile, or exact transport changed; no rebinding")
    return receipt, receipt_pin, data, config_data, config_pin


def launch(policy_file, out, *, previous=None, timeout=60):
    """One declared LaunchServices request; restart preserves config and profile."""
    R.assert_guest()
    deadline = _timeout(timeout, 120)
    data = _policy(policy_file, out)
    policy, pin, bundle, profile, config, folder, executable = data
    old = config_data = config_pin = None
    if previous is not None:
        old, old_pin, old_data, config_data, config_pin = _saved(previous, out)
        if old_data[1] != pin:
            raise ValueError("restart policy differs from the original launch")
        profile_pin = old["profile_identity"]
    else:
        if profile.exists() or config.exists():
            raise ValueError("initial profile and config must both be absent")
        profile_pin = None
    _absent(policy, bundle, deadline)
    folder.mkdir(mode=0o700)
    result = _base("launch", folder, policy, pin)
    result["previous"] = old_pin if old is not None else None
    result["config_file"] = str(config)
    _write(folder, result)
    try:
        if profile_pin is None:
            profile.mkdir(mode=0o700)
            profile_pin = _directory(profile, mode=0o700)
        result["profile_identity"] = profile_pin
        _absent(policy, bundle, deadline)
        _unchanged(policy, pin, profile, profile_pin, config_pin=config_pin)
        result["launch_requested_at"] = time.time()
        result["launch_attempts"] = 1
        _write(folder, result)  # Durable intent precedes the sole external launch.
        _open(policy, bundle, profile, deadline)
        stable_url, stable_since, first_identity = None, None, None
        while True:
            I._remaining(deadline)
            try:
                registrations = _registrations(policy["target"]["bundle_id"])
                if not registrations and first_identity is None:
                    raise _NotReady("app registration has not appeared")
                if len(registrations) != 1:
                    raise ValueError("app registration disappeared or became ambiguous after launch")
                main, kernel = _exact_main(bundle, policy["target"], executable, deadline)
                if main["launch_time"] < result["launch_requested_at"] or old is not None and main == old["identity"]:
                    raise ValueError("observed app was not freshly started by this launch")
                if first_identity is None:
                    first_identity = (main, kernel)
                    result.update(identity=main, kernel_identity=kernel)
                    _write(folder, result)
                elif (main, kernel) != first_identity:
                    raise ValueError("first owned app process was replaced; no new-process adoption")
                if _port_absent(policy["port"], deadline):
                    raise _NotReady("owned inspector listener has not appeared")
                I._listener("http://127.0.0.1:" + str(policy["port"]), main, deadline)
                try:
                    url, suffix = _initial_page(policy, bundle, deadline)
                except OSError as exc:
                    raise _NotReady("owned inspector HTTP startup is incomplete") from exc
                candidate = _config_data(policy, suffix) if old is None else config_data
                if old is not None and suffix != I._document_suffix(config_data.get("document_suffix", {"query": "", "fragment": ""})):
                    raise ValueError("restart renderer suffix differs from the frozen transport; no rebinding")
                if url != stable_url:
                    stable_url, stable_since = url, time.monotonic()
                if time.monotonic() - stable_since < 1:
                    time.sleep(min(0.1, I._remaining(deadline)))
                    continue
                break
            except _NotReady as exc:
                if time.monotonic() + 0.15 >= deadline:
                    raise ValueError("bounded launch observation incomplete: " + str(exc)) from exc
                time.sleep(min(0.1, I._remaining(deadline)))
        # Once startup metadata is stable, any binding/profile/cleanup failure
        # ends this operation. Do not repeat a failed complete observation.
        snapshot = _snapshot(policy, bundle, main, kernel, candidate, profile, deadline)
        _unchanged(policy, pin, profile, profile_pin, config_pin=config_pin)
        I._remaining(deadline)
        if old is None:
            raw = (json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()
            descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            config_data, config_pin = I._config_for_target(policy["target"], str(config))
        result.update(snapshot)
        result.update(status="ready", transport=I._transport(config_data, config_pin), target_unchanged=True)
        _unchanged(policy, pin, profile, profile_pin, config_pin=config_pin)
        if result["source"] != _source_identity():
            raise ValueError("lifecycle or inspector source changed during launch")
        I._remaining(deadline)
        return _write(folder, result)
    except Exception as exc:
        return _failure(folder, result, exc)


def inspect(launch_receipt, out, *, timeout=30):
    """Metadata-only observation of the same owned live app and pinned config."""
    R.assert_guest()
    deadline = _timeout(timeout, 60)
    saved, saved_pin, data, config_data, config_pin = _saved(launch_receipt, out)
    policy, pin, bundle, profile, config, folder, executable = data
    folder.mkdir(mode=0o700)
    result = _base("inspect", folder, policy, pin)
    result["launch_receipt"] = saved_pin
    _write(folder, result)
    try:
        main, kernel = _exact_main(bundle, policy["target"], executable, deadline)
        if main != saved["identity"] or kernel != saved["kernel_identity"]:
            raise ValueError("launch app identity changed; no adoption")
        result.update(_snapshot(policy, bundle, main, kernel, config_data, profile, deadline))
        _unchanged(policy, pin, profile, saved["profile_identity"], config_pin=config_pin)
        if result["source"] != _source_identity():
            raise ValueError("lifecycle or inspector source changed during inspection")
        I._remaining(deadline)
        result.update(status="inspected", transport=saved["transport"], target_unchanged=True)
        return _write(folder, result)
    except Exception as exc:
        return _failure(folder, result, exc)


def _terminate(bundle, target, executable, main, kernel, deadline, *, before_request):
    if _exact_main(bundle, target, executable, deadline) != (main, kernel):
        raise ValueError("owned app identity changed immediately before normal termination")
    import AppKit
    rows = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(target["bundle_id"])
    if len(rows) != 1:
        raise ValueError("owned registration changed before normal termination")
    app = rows[0]
    url, exe, launched = app.bundleURL(), app.executableURL(), app.launchDate()
    if (app.isTerminated() or int(app.processIdentifier()) != main["pid"] or not url or not exe or not launched or
            str(url.path()) != str(bundle) or str(exe.path()) != str(executable) or
            float(launched.timeIntervalSince1970()) != main["launch_time"]):
        raise ValueError("fresh NS registration differs from the complete owned launch identity")
    before_request()
    return bool(app.terminate())


def _kernel_absent(pid, deadline):
    result = _run(["/bin/ps", "-p", str(pid), "-o", "pid="], deadline)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def _wait_tick(deadline):
    import Foundation
    Foundation.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(min(0.1, I._remaining(deadline))))


def close(launch_receipt, out, *, timeout=15):
    """Request normal close once; no forced termination, signal, or VM restart."""
    R.assert_guest()
    deadline = _timeout(timeout, 60)
    saved, saved_pin, data, config_data, config_pin = _saved(launch_receipt, out)
    policy, pin, bundle, profile, config, folder, executable = data
    folder.mkdir(mode=0o700)
    result = _base("close", folder, policy, pin)
    result["launch_receipt"] = saved_pin
    _write(folder, result)
    try:
        main, kernel = _exact_main(bundle, policy["target"], executable, deadline)
        if main != saved["identity"] or kernel != saved["kernel_identity"]:
            raise ValueError("owned app identity changed; no termination requested")
        I._listener(config_data["endpoint"], main, deadline)
        _unchanged(policy, pin, profile, saved["profile_identity"], config_pin=config_pin)
        if result["source"] != _source_identity():
            raise ValueError("lifecycle or inspector source changed during close")
        result.update(identity=main, kernel_identity=kernel, termination_intent=True)
        _write(folder, result)
        def before_request():
            result["termination_requests"] = 1
            _write(folder, result)
        result["normal_termination_accepted"] = _terminate(bundle, policy["target"], executable, main, kernel, deadline,
                                                          before_request=before_request)
        if not result["normal_termination_accepted"]:
            raise ValueError("app refused normal termination; explicit resource review required")
        while True:
            I._remaining(deadline)
            absent = _kernel_absent(main["pid"], deadline)
            registrations = _registrations(policy["target"]["bundle_id"])
            candidates = _kernel_candidates(bundle, deadline)
            port_gone = _port_absent(policy["port"], deadline)
            result["closure_observation"] = {"old_kernel_pid_absent": absent, "bundle_registrations_absent": not registrations,
                                              "kernel_app_candidates_absent": not candidates, "listener_absent": port_gone}
            if absent and not registrations and not candidates and port_gone:
                break
            _wait_tick(deadline)
        _unchanged(policy, pin, profile, saved["profile_identity"], config_pin=config_pin)
        if result["source"] != _source_identity():
            raise ValueError("lifecycle or inspector source changed during close")
        I._remaining(deadline)
        result.update(status="closed", target_unchanged=True)
        return _write(folder, result)
    except Exception as exc:
        return _failure(folder, result, exc)
