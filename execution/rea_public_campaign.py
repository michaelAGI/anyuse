"""Trusted neutral ordinary-UI setup and restart producers for the live trial.

No target-private method, debugger, preference edit, force quit or PID adoption.
Call through PublicPlayback from authorized caller code. No callbacks are loaded from product JSON.
"""
from pathlib import Path
from types import MappingProxyType
import json
import os
import time

_OPTIONAL_READ_EVENTS = None


def need(value, message):
    if not value:
        raise ValueError(message)


def api():
    from anyuse.execution import rea_mapping as R, rea_runtime_bridge as B, rea_ax_state as S
    from anyuse.execution import foreground_interlock as F
    from anyuse.context.accessibility import AccessibilityTree
    R.assert_guest()
    import AppKit as K
    import ApplicationServices as A
    import Foundation as N
    return R, B, S, F, AccessibilityTree, K, A, N


def identity(value):
    R, B, S, F, AT, K, A, N = api()
    frozen = S.validate_identity(value)
    current = B.process_identity(Path(frozen["bundle"]), frozen["bundle_id"])
    need(S._same(current, frozen), "owned full process identity changed; no successor adoption")
    apps = list(K.NSRunningApplication.runningApplicationsWithBundleIdentifier_(frozen["bundle_id"]))
    need(len(apps) == 1 and int(apps[0].processIdentifier()) == frozen["pid"], "one exact owned running application required")
    return current, apps[0]


def gate(expected, where, deadline=None):
    need(deadline is None or time.monotonic() < deadline, "ordinary public action deadline expired")
    R, B, S, F, AT, K, A, N = api()
    need(os.environ.get("ANYUSE_DRIVE_ENABLED") in {"on", "1"}, "explicit drive authorization required")
    current, owned = identity(expected)
    F.require_clear(where)
    need(AT(pid=current["pid"]).actuation_refusal(where) is None, "public actuation gate refused")
    need(deadline is None or time.monotonic() < deadline, "ordinary public action deadline expired")
    return current, owned


def attr(node, name, *, optional=False):
    A = api()[6]
    A.AXUIElementSetMessagingTimeout(node, 1.0)
    code, value = A.AXUIElementCopyAttributeValue(node, name, None)
    if optional:
        # Unavailable optional metadata cannot identify the requested document.
        # Re-enumeration is read-only and bounded by prepare_case's deadline.
        if _OPTIONAL_READ_EVENTS is not None:
            need(len(_OPTIONAL_READ_EVENTS) < 2048, "bounded optional AX read log exhausted")
            _OPTIONAL_READ_EVENTS.append({"attribute": name, "code": int(code),
                "value_is_none": value is None, "monotonic": time.monotonic()})
        if code != 0 or value is None:
            return None
    need(code == 0 and value is not None, "public AX attribute unavailable: " + name)
    return value


def pump(deadline, predicate):
    N = api()[7]
    while not predicate():
        need(time.monotonic() < deadline, "ordinary setup completion deadline expired; no retry")
        N.NSRunLoop.currentRunLoop().runUntilDate_(N.NSDate.dateWithTimeIntervalSinceNow_(min(.05, deadline - time.monotonic())))


def _open(expected, urls, deadline, *, new_instance=False, ignore_saved_state=False, environment=None):
    """Actual normal completion app is retained; no open-and-scan provenance."""
    need(time.monotonic() < deadline, "ordinary open deadline expired")
    R, B, S, F, AT, K, A, N = api()
    if new_instance:
        need(not list(K.NSRunningApplication.runningApplicationsWithBundleIdentifier_(expected["bundle_id"])),
             "old app must be absent before ordinary launch")
        need(os.environ.get("ANYUSE_DRIVE_ENABLED") in {"on", "1"}, "explicit drive authorization required")
        F.require_clear("campaign.ordinary-owned-launch")
    else:
        gate(expected, "campaign.ordinary-owned-open", deadline)
    cfg = K.NSWorkspaceOpenConfiguration.configuration()
    cfg.setCreatesNewApplicationInstance_(bool(new_instance))
    cfg.setActivates_(True)
    cfg.setAddsToRecentItems_(False)
    cfg.setPromptsUserIfNeeded_(False)
    if ignore_saved_state:
        cfg.setArguments_(["-ApplePersistenceIgnoreState", "YES"])
    if environment:
        cfg.setEnvironment_(environment)
        need(dict(cfg.environment()) == environment, "ordinary launch environment differs")
    done = []
    def completion(app, error):
        done.append((app, error))
    workspace = K.NSWorkspace.sharedWorkspace()
    need(time.monotonic() < deadline, "ordinary open deadline expired")
    if urls:
        used_api = "NSWorkspace.openURLs_withApplicationAtURL_configuration_completionHandler_"
        workspace.openURLs_withApplicationAtURL_configuration_completionHandler_(
            [N.NSURL.fileURLWithPath_(str(file)) for file in urls],
            N.NSURL.fileURLWithPath_(expected["bundle"]), cfg, completion)
    else:
        used_api = "NSWorkspace.openApplicationAtURL_configuration_completionHandler_"
        workspace.openApplicationAtURL_configuration_completionHandler_(
            N.NSURL.fileURLWithPath_(expected["bundle"]), cfg, completion)
    pump(deadline, lambda: bool(done))
    need(time.monotonic() < deadline, "ordinary open completion deadline expired")
    need(len(done) == 1, "ordinary launch completion repeated unexpectedly")
    app, error = done[0]
    need(error is None and app is not None and not app.isTerminated(), "ordinary open/launch completion failed: " + str(error))
    need(app.bundleURL() is not None and str(Path(str(app.bundleURL().path())).resolve(strict=True)) == expected["bundle"] and
         str(app.bundleIdentifier()) == expected["bundle_id"] and app.executableURL() is not None and
         str(Path(str(app.executableURL().path())).resolve(strict=True)) == expected["executable"],
         "completion application differs from exact owned bundle/executable")
    pid = int(app.processIdentifier())
    need(pid > 0 and (new_instance or pid == expected["pid"]), "ordinary open completion targeted another process")
    return {"application_pid": pid, "result": {"api": used_api,
        "completion_count": 1, "error": None, "new_instance": bool(new_instance), "url_paths": [str(file) for file in urls],
        "bundle": expected["bundle"], "bundle_id": expected["bundle_id"], "executable": expected["executable"],
        "completion_launch_time": float(app.launchDate().timeIntervalSince1970()),
        "configuration_environment": environment or {}, "HOME_overridden": False}}


def prepare_case(context):
    """Public setup only. request={media:absolute path,observer:measured declaration}.

    Uses the current exact process identity resolved by public NS/kernel APIs.
    G independently freezes its own intent AFTER this function returns.
    """
    global _OPTIONAL_READ_EVENTS
    need(time.monotonic() < context["deadline_monotonic"], "public setup deadline expired")
    R, B, S, F, AT, K, A, N = api()
    need(set(context["request"]) == {"media", "observer"}, "explicit media and measured public observer required")
    from anyuse.execution import rea_playback_state as O
    observer = O.validate_observer(context["request"]["observer"])
    bundle = Path(context["bundle"]).resolve(strict=True)
    need(R.fingerprint(bundle) == context["target"], "ordinary setup target bytes changed")
    current = B.process_identity(bundle, context["target"]["bundle_id"])
    need(S._same(current, S.validate_identity(context["expected_identity"])), "setup process epoch differs; no successor adoption")
    media = Path(context["request"]["media"])
    need(media.is_absolute() and media.resolve(strict=True) == media and media.is_file() and not media.is_symlink(), "canonical authorized exact public media file required")
    deadline = min(time.monotonic() + 35, context["deadline_monotonic"])
    need(time.monotonic() < deadline, "public setup deadline expired")
    folder = Path(context["out"])
    folder.mkdir(mode=0o700)
    journal = {"format": "anyuse.trusted-public-playback-setup/1", "started_monotonic": time.monotonic(), "status": "running",
               "identity": current, "media": str(media), "opened": None, "play_attempts": 0,
               "activation_attempts": 0, "raise_attempts": 0, "main_attempts": 0, "native_goal_calls": 0, "error": None}
    journal["optional_ax_reads"] = []
    _OPTIONAL_READ_EVENTS = journal["optional_ax_reads"]
    def retain():
        (folder / "setup.json").write_text(json.dumps(journal, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
    retain()
    try:
        root = A.AXUIElementCreateApplication(current["pid"])
        def exact_windows():
            identity(current)
            windows = attr(root, "AXWindows")
            need(len(windows) <= 128, "bounded owned AX window inventory required")
            return [window for window in windows if str(attr(window, "AXDocument", optional=True) or "") == media.as_uri()]
        matches = exact_windows()
        need(len(matches) <= 1, "ambiguous same-media public windows; no ordinal selection")
        journal["opened"] = not bool(matches)
        retain()
        if not matches:
            journal["open_completion"] = _open(current, [media], deadline)
            retain()
            pump(deadline, lambda: bool(exact_windows()))
            matches = exact_windows()
        need(len(matches) == 1, "one exact authorized media window required")
        window = matches[0]
        _, owned = gate(current, "campaign.public-owned-activate", deadline)
        journal["activation_attempts"] += 1; retain()
        need(time.monotonic() < deadline, "ordinary activation deadline expired")
        need(owned.activateWithOptions_(K.NSApplicationActivateIgnoringOtherApps), "ordinary owned activation refused")
        gate(current, "campaign.public-exact-raise", deadline)
        journal["raise_attempts"] += 1; retain()
        need(time.monotonic() < deadline, "ordinary raise deadline expired")
        need(int(A.AXUIElementPerformAction(window, "AXRaise")) == 0, "ordinary exact-window raise refused")
        code, settable = A.AXUIElementIsAttributeSettable(window, "AXMain", None)
        need(code == 0 and settable, "ordinary exact AXMain cannot be set")
        gate(current, "campaign.public-exact-main", deadline)
        journal["main_attempts"] += 1; retain()
        need(time.monotonic() < deadline, "ordinary main-window deadline expired")
        need(int(A.AXUIElementSetAttributeValue(window, "AXMain", True)) == 0, "ordinary exact-window main assignment refused")
        pump(deadline, lambda: int(K.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()) == current["pid"] and
             str(attr(attr(root, "AXFocusedWindow"), "AXDocument")) == media.as_uri() and bool(attr(window, "AXMain")))
        control = root
        for step in observer["control"]["path"]:
            control = attr(control, step["attribute"])
            if step["index"] is not None:
                need(len(control) <= 128 and step["index"] < len(control), "measured public AX control path changed")
                control = control[step["index"]]
        expected = observer["control"]
        need(str(attr(control, "AXRole")) == expected["role"] and
             attr(control, "AXIdentifier", optional=True) == expected["identifier"] and bool(attr(control, "AXEnabled")),
             "measured enabled public playback control identity changed")
        title = str(attr(control, "AXTitle"))
        journal["control_title_before"] = title
        need(title in {expected["playing_title"], expected["paused_title"]}, "public control neither playing nor paused")
        if title == expected["paused_title"]:
            gate(current, "campaign.public-play-setup", deadline)
            need(str(attr(attr(root, "AXFocusedWindow"), "AXDocument")) == media.as_uri(), "current exact media drifted before public Play")
            journal["play_attempts"] = 1; retain()
            need(time.monotonic() < deadline, "ordinary public Play deadline expired")
            need(int(A.AXUIElementPerformAction(control, "AXPress")) == 0, "ordinary public Play refused")
            pump(deadline, lambda: str(attr(control, "AXTitle")) == expected["playing_title"])
        # AX's playing label can precede visible video. Only read-only,
        # fully guarded black/still samples may wait inside this same deadline.
        # The later trial still captures its own fresh complete before window.
        playback_context = O.current_context(observer, current, deadline=deadline)
        need(playback_context["media"]["file"] == str(media), "public readiness media changed")
        from anyuse.execution import rea_playback_readiness as V
        journal["pixel_readiness"] = V.wait_for_playing(observer, context["target"], current, playback_context,
            str(folder / "pixel-readiness"), deadline=deadline)
        journal.update(status="ready", finished_monotonic=time.monotonic(), control_title_after=str(attr(control, "AXTitle")),
            windows=[{"title": str(attr(w, "AXTitle", optional=True) or ""), "document": str(attr(w, "AXDocument", optional=True) or "")} for w in attr(root, "AXWindows")])
        need(time.monotonic() < deadline, "public setup completion deadline expired")
        retain()
        return {"status": "ready", "journal": str(folder / "setup.json"), "identity": current, "media": str(media), "native_goal_calls": 0}
    except BaseException as error:
        journal.update(status="failed", finished_monotonic=time.monotonic(), error={"type": type(error).__name__, "message": str(error)[:4096]})
        retain()
        raise
    finally:
        _OPTIONAL_READ_EVENTS = None


def stop_owned(context):
    """One ordinary exact owned terminate request. RR witnesses actual absence."""
    current, app = gate(context["old_identity"], "campaign.ordinary-owned-quit", context["deadline_monotonic"])
    need(time.monotonic() < context["deadline_monotonic"], "ordinary stop deadline expired")
    accepted = bool(app.terminate())
    need(accepted, "ordinary owned quit refused; no force quit or retry")
    return {"api": "NSRunningApplication.terminate", "attempts": 1, "accepted": accepted, "identity": current, "native_goal_calls": 0}


def launch_owned(context, *, environment=None):
    """One ordinary empty launch, completion-PID bound; file setup follows RR."""
    return _open(context["old_identity"], [], context["deadline_monotonic"], new_instance=True, ignore_saved_state=True, environment=environment)


class PublicPlayback:
    """Fixed ordinary public operations; all app facts come from frozen data.

    The caller authorizes the exact owned app/identity and media. No task-native
    action, force quit, preference edit or process-wide environment mutation is
    part of this backend. RR independently witnesses each ordinary restart.
    """
    def __init__(self, launch_environment=None):
        environment = dict(launch_environment or {})
        need(set(environment) <= {"CFFIXED_USER_HOME", "TMPDIR", "XDG_CACHE_HOME"},
             "only explicit process-private launch storage overrides are supported")
        for name, value in environment.items():
            need(type(value) is str and value.startswith("/") and ".." not in Path(value).parts and
                 Path(value).is_dir() and Path(value).resolve(strict=True) == Path(value) and
                 os.access(value, os.W_OK), "canonical writable existing launch storage required")
        self.launch_environment = MappingProxyType(environment)

    @property
    def producers(self):
        from . import rea_mapping as R
        file = Path(__file__).resolve(strict=True)
        pin = {"file": str(file), "sha256": R._file_hash(file), "bytes": file.stat().st_size}
        return {role: dict(pin) for role in ("setup", "stop", "launch")}

    def setup(self, context):
        return prepare_case(context)

    def stop(self, context):
        return stop_owned(context)

    def launch(self, context):
        return launch_owned(context, environment=dict(self.launch_environment))
