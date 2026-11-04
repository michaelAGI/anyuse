"""Foreground interlock: no input event goes into a session a person is looking at.

``ANYUSE_DRIVE_ENABLED`` says AnyUse MAY drive. This module says WHERE it must
not. Every real input primitive calls :func:`refusal` (or
:func:`require_clear`) immediately before it acts:

* AX actions and value writes   ``context/accessibility.py``
* CGEvent key posts             ``context/keyboard.py``
* pyautogui mouse/keyboard      ``execution/actions.py`` (``_Backend``)
* AppleScript / JavaScript      ``execution/executor.py``, ``execution/intervention.py``,
                                ``execution/osa_compile.py``
* app launch / activation       ``execution/ax_actuator.py``,
                                ``execution/agent_orchestrator.py``,
                                ``memory/window_manager.py`` (activate, move/resize)
* credential typing (agent API) ``core/agent.py`` ``Agent.use_credential``

A refusal is hard: the primitive returns its normal failure shape, nothing is
sent, and one ERROR line ``FOREGROUND INTERLOCK refused <where>: <reason>`` is
logged on the ``anyuse.interlock`` logger.

Rules
-----
"Own session" is the ``IOConsoleUsers`` entry whose ``kCGSSessionAuditIDKey``
equals this process's audit session ID (``getaudit_addr``) — not its uid: one
uid can run processes in a GUI session, an ssh session and launchd at once.
``CGSSessionScreenIsLocked`` is present only while a session is locked.

Host (not a VM, or VM-ness could not be read — unknown counts as host):

1. refuse when the console state cannot be read, including an
   ``IOConsoleUsers`` entry that does not say whether it is on the console;
2. refuse while a screen-sharing client holds an ESTABLISHED TCP connection to
   port 5900 (Screen Sharing / VNC) or 3283 (ARD), or when ``netstat`` cannot
   be read — a remote viewer is a person looking, whichever session it shows;
3. refuse when the own session is on the console — the screen in front of the
   person (locked or not);
4. refuse when any entry is on the console: a logged-in user, a nameless
   entry, or ``loginwindow`` (keystrokes could land in its password field).
   Events posted at the HID event tap (``CGEventPost(kCGHIDEventTap, ...)``,
   which ``keyboard.py`` and pyautogui both use) enter the window server as
   hardware input; that they stay in a background session is not established;
5. refuse when the own audit session cannot be read or has no GUI login
   session (an ssh or daemon process posting at the HID tap reaches whatever
   is on the console);
6. refuse when no entry is on the console either: then the login window is
   showing, the same hazard as rule 4.

So no host state allows input today. Allowing a background session needs a measurement that its input
cannot reach the console, and a rule change here.

VM guest (``kern.hv_vmm_present`` = 1 or ``hw.model`` VirtualMac*):

1. refuse while a screen-sharing client holds an ESTABLISHED TCP connection to
   the guest's VNC port (5900, macOS Screen Sharing — ``tart run --vnc``) or ARD
   port (3283);
2. refuse when that cannot be read (``netstat`` failed, or its output has no
   ``Proto`` header line);
3. refuse when the console sessions or the own audit session cannot be read,
   or this process has no GUI login session (an ssh process);
4. refuse when the own session is not the console session (another login on the
   console, fast user switching, the login window): HID-level input lands in the
   console session, not in a background one;
5. refuse while the own session's screen is locked (nothing is typed into a
   lock screen);
6. refuse while a system prompt is on screen: a window owned by a permission,
   authentication or alert process (:data:`PROMPT_OWNERS`) or at the
   authorization-panel layer. Clicks and keystrokes would land in it, and only
   the person may answer it. A window list that cannot be read refuses too.

What the guest cannot see, and is therefore not checked here: the host-side
Tart window (``VZVirtualMachineView``, present unless ``--no-graphics``) and
Virtualization.framework's own VNC server (``tart run --vnc-experimental``).
Both live in the host's ``tart`` process; the guest's virtual display looks the
same whether or not anyone watches it. Whether a viewer is *focused* on the
viewing machine is not visible from the guest either, so an attached viewer is
refused regardless of focus. :func:`host_tart_viewer` reads the tart
controller's arguments on the host so a harness can refuse to submit guest jobs
while a window or a host-side VNC server exists.

Test flag
---------
``ANYUSE_INTERLOCK_TEST_BYPASS=1`` lets unit tests that drive FAKE AX / Quartz
objects pass the interlock. It is honoured only when the guarded test runner's
desktop import guard is loaded in the process (``tools/guarded_tests/
sitecustomize.py``: Quartz, ApplicationServices, AppKit and pyautogui cannot be
imported there, so no real input event can be sent). Anywhere else the flag is
ignored with an ERROR line and the real check runs. There is no other bypass.

Read-only CLI: ``python -m anyuse.execution.foreground_interlock`` prints the
current verdict as JSON. It runs ``sysctl``, ``ioreg`` and ``netstat`` and
reads its own audit session; it sends nothing.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger("anyuse.interlock")

TEST_BYPASS_ENV = "ANYUSE_INTERLOCK_TEST_BYPASS"
VIEWER_PORTS = (5900, 3283)          # Screen Sharing / VNC, Apple Remote Desktop
#: window owners of macOS permission, authentication and alert prompts (TCC consent,
#: authorization, Touch ID / password sheets, the Accessibility warning); matched as
#: case-insensitive substrings of ``kCGWindowOwnerName``, as the guest job agent does
PROMPT_OWNERS = ("SecurityAgent", "UserNotificationCenter", "coreautha", "tccd",
                 "universalAccessAuthWarn", "authorizationhost")
#: the window-server layer of an authorization panel
AUTH_LAYER = 1000
_PROBE_TTL_S = 0.25                  # one probe serves the bursts inside enter_text


@dataclass(frozen=True)
class ConsoleSession:
    """One entry of IOKit's ``IOConsoleUsers`` (the data CGSession reads)."""
    uid: Optional[int]
    user: str
    on_console: bool
    login_done: bool
    locked: Optional[bool] = None    # CGSSessionScreenIsLocked; absent (None) while unlocked
    audit_id: Optional[int] = None   # kCGSSessionAuditIDKey: the session's audit session ID


@dataclass(frozen=True)
class ViewerState:
    """Whether a remote viewer is attached to THIS machine's screen.
    ``attached`` None = could not be determined."""
    attached: Optional[bool]
    evidence: str = ""


@dataclass(frozen=True)
class SessionState:
    """Everything the rules need. ``None`` anywhere means "could not be read"."""
    is_vm: Optional[bool]
    own_uid: int
    sessions: Optional[List[ConsoleSession]]
    viewer: Optional[ViewerState] = None
    own_asid: Optional[int] = None   # this process's audit session ID (getaudit_addr)
    #: on-screen system prompt windows ``({"owner", "layer", "pid"}, ...)``; () = none, None = unreadable
    prompts: Optional[tuple] = ()


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    where: str
    reason: str
    environment: str                 # "host" | "vm-guest"
    bypassed: bool = False
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ForegroundInterlockRefused(RuntimeError):
    """Raised by :func:`require_clear`; carries the :class:`Verdict`."""

    def __init__(self, verdict: Verdict):
        super().__init__(f"foreground interlock refused {verdict.where}: {verdict.reason}")
        self.verdict = verdict


# ---------------------------------------------------------------------------
# The rules (pure)
# ---------------------------------------------------------------------------

def _own_session(state: SessionState):
    """(session, None) for the IOConsoleUsers entry in this process's audit
    session, else (None, why). A uid can own several processes in different
    audit sessions (GUI, ssh, launchd); only the audit session says which login
    session posted input lands in."""
    if state.sessions is None:
        return None, "console session state unreadable"
    if state.own_asid is None:
        return None, "this process's audit session cannot be read"
    own = [s for s in state.sessions if s.audit_id == state.own_asid]
    if not own:
        return None, (f"this process (audit session {state.own_asid}) has no GUI login session; "
                      "posted input would reach the console session")
    return own[0], None


def _viewer_refusal(v: Optional[ViewerState], prefix: str) -> Optional[str]:
    if v is None or v.attached is None:
        return (f"{prefix}: cannot tell whether a screen-sharing viewer is attached"
                + (f" ({v.evidence})" if v and v.evidence else ""))
    if v.attached:
        return f"{prefix}: a screen-sharing viewer is attached ({v.evidence})"
    return None


def _prompt_refusal(prompts: Optional[tuple], prefix: str) -> Optional[str]:
    if prompts is None:
        return (f"{prefix}: cannot tell whether a system permission prompt is on screen "
                "(the window list is unreadable)")
    if prompts:
        owners = ", ".join(sorted({str(p.get("owner") or "?") for p in prompts}))
        return (f"{prefix}: a system permission prompt is on screen ({owners}); a person must "
                "answer it before AnyUse sends any input")
    return None


def evaluate(state: SessionState, where: str) -> Verdict:
    """Apply the rules in the module docstring to ``state``. Pure."""
    detail = {"own_uid": state.own_uid, "own_asid": state.own_asid,
              "prompts": list(state.prompts) if state.prompts is not None else None,
              "viewer": asdict(state.viewer) if state.viewer else None,
              "sessions": [asdict(s) for s in state.sessions] if state.sessions is not None else None}
    if state.is_vm is True:
        why = _viewer_refusal(state.viewer, "vm guest")
        if why:
            return Verdict(False, where, why, "vm-guest", detail=detail)
        own, missing = _own_session(state)
        if own is None:
            return Verdict(False, where, f"vm guest: {missing}", "vm-guest", detail=detail)
        if not own.on_console:
            # HID-level input lands in the console session: in a background session (fast user
            # switching, another login on the console) it would reach someone else's screen
            return Verdict(False, where, f"vm guest: this process's session (audit session "
                           f"{own.audit_id}, {own.user!r}) is not the console session; posted "
                           "input would reach the session on the console", "vm-guest", detail=detail)
        if own.locked:
            return Verdict(False, where, "vm guest: the screen is locked (nothing is typed into a "
                           "lock screen)", "vm-guest", detail=detail)
        why = _prompt_refusal(state.prompts, "vm guest")
        if why:
            return Verdict(False, where, why, "vm-guest", detail=detail)
        return Verdict(True, where, "vm guest: own session on the console, unlocked, no screen-sharing viewer "
                       "attached (host-side tart window not visible from the guest)",
                       "vm-guest", detail=detail)

    env_note = "" if state.is_vm is False else " (VM-ness unknown: host rules)"
    if state.sessions is None:
        return Verdict(False, where, "host: console session state unreadable" + env_note,
                       "host", detail=detail)
    why = _viewer_refusal(state.viewer, "host")
    if why:
        return Verdict(False, where, why + env_note, "host", detail=detail)
    own, missing = _own_session(state)
    if own is not None and own.on_console:
        locked = " (screen locked)" if own.locked else ""
        return Verdict(False, where, f"host: the target session (audit session {own.audit_id}, "
                       f"{own.user!r}) is the console session — a person is looking at it{locked}"
                       + env_note, "host", detail=detail)
    at_console = [s for s in state.sessions if s.on_console]
    if at_console:
        s = at_console[0]
        if s.user == "loginwindow":
            return Verdict(False, where, "host: the login window is on the console; keystrokes "
                           "could land in its password field" + env_note, "host", detail=detail)
        return Verdict(False, where, f"host: {s.user!r} (uid {s.uid}) is logged in at the console; "
                       "HID-level input is not proven to stay in a background session" + env_note,
                       "host", detail=detail)
    if own is None:
        return Verdict(False, where, f"host: {missing}" + env_note, "host", detail=detail)
    return Verdict(False, where, "host: no session is on the console, so the login window is "
                   "showing; keystrokes could land in its password field" + env_note,
                   "host", detail=detail)


# ---------------------------------------------------------------------------
# Probes (read-only)
# ---------------------------------------------------------------------------

def _run(argv: List[str], timeout: float = 5.0) -> Optional[bytes]:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return p.stdout if p.returncode == 0 else None


_IS_VM_CACHE: List[Optional[bool]] = []


def probe_is_vm() -> Optional[bool]:
    """True in a Virtualization.framework guest; None when sysctl is unreadable.
    Cached for the process: it cannot change while the process runs."""
    if _IS_VM_CACHE:
        return _IS_VM_CACHE[0]
    model = _run(["/usr/sbin/sysctl", "-n", "hw.model"])
    hv = _run(["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"])
    if model is None and hv is None:
        result: Optional[bool] = None
    else:
        result = ("virtualmac" in (model or b"").decode(errors="replace").lower()
                  or (hv or b"").strip() == b"1")
    _IS_VM_CACHE.append(result)
    return result


def parse_console_users(plist_bytes: bytes) -> Optional[List[ConsoleSession]]:
    """``ioreg -n Root -d1 -a`` output → sessions; None when it does not parse.
    A missing ``IOConsoleUsers`` key (nobody logged in) is an empty list. An
    entry that is not a dict or lacks ``kCGSSessionOnConsoleKey`` makes the
    whole state unreadable: reading it as "not on the console" would allow."""
    try:
        root = plistlib.loads(plist_bytes)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(root, list):
        root = root[0] if root and isinstance(root[0], dict) else {}
    if not isinstance(root, dict):
        return None
    out = []
    for d in root.get("IOConsoleUsers") or []:
        if not isinstance(d, dict) or "kCGSSessionOnConsoleKey" not in d:
            return None
        uid = d.get("kCGSSessionUserIDKey")
        locked = d.get("CGSSessionScreenIsLocked")
        asid = d.get("kCGSSessionAuditIDKey")
        out.append(ConsoleSession(
            uid=int(uid) if isinstance(uid, int) else None,
            user=str(d.get("kCGSSessionUserNameKey") or ""),
            on_console=bool(d.get("kCGSSessionOnConsoleKey")),
            login_done=bool(d.get("kCGSessionLoginDoneKey")),
            locked=bool(locked) if locked is not None else None,
            audit_id=int(asid) if isinstance(asid, int) else None,
        ))
    return out


def probe_console_sessions() -> Optional[List[ConsoleSession]]:
    raw = _run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"])
    return None if raw is None else parse_console_users(raw)


def parse_viewer_connections(netstat_text: str, ports=VIEWER_PORTS) -> ViewerState:
    """``netstat -an -p tcp`` → ESTABLISHED connections whose LOCAL port is a
    viewer port (someone connected to this machine's screen). Output without
    the ``Proto`` header line is not netstat's table: unknown, not "no viewer"."""
    hits = []
    header = False
    for line in netstat_text.splitlines():
        parts = line.split()
        if parts[:1] == ["Proto"]:
            header = True
        if len(parts) < 6 or not parts[0].startswith("tcp") or parts[5] != "ESTABLISHED":
            continue
        local, foreign = parts[3], parts[4]
        port = local.rsplit(".", 1)[-1]
        if port.isdigit() and int(port) in ports:
            hits.append(f"{local}<-{foreign}")
    if hits:
        return ViewerState(True, "ESTABLISHED " + ", ".join(hits[:4]))
    if not header:
        return ViewerState(None, "netstat output not recognised (no Proto header)")
    return ViewerState(False, "no ESTABLISHED connection on ports "
                       + "/".join(str(p) for p in ports))


def probe_remote_viewer() -> ViewerState:
    raw = _run(["/usr/sbin/netstat", "-an", "-p", "tcp"])
    if raw is None:
        return ViewerState(None, "netstat failed")
    return parse_viewer_connections(raw.decode(errors="replace"))


def parse_prompt_windows(rows) -> tuple:
    """``CGWindowListCopyWindowInfo`` rows → the system prompt windows among them (pure)."""
    out = []
    for w in rows or []:
        owner = str((w or {}).get("kCGWindowOwnerName") or "")
        try:
            layer = int((w or {}).get("kCGWindowLayer") or 0)
            pid = int((w or {}).get("kCGWindowOwnerPID") or 0)
        except (TypeError, ValueError):
            continue
        if layer == AUTH_LAYER or any(o.lower() in owner.lower() for o in PROMPT_OWNERS):
            out.append({"owner": owner, "layer": layer, "pid": pid})
    return tuple(out)


def probe_prompt_windows() -> Optional[tuple]:
    """The on-screen system prompt windows (read-only window-server query; owner names and
    layers need no Screen Recording grant); None when the window list cannot be read."""
    try:
        import Quartz  # pyobjc-framework-Quartz
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        rows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
    except Exception:  # noqa: BLE001 - unreadable: the rule refuses
        return None
    if rows is None:
        return None
    return parse_prompt_windows(rows)


def probe_own_audit_session() -> Optional[int]:
    """This process's audit session ID (``getaudit_addr(2)``, struct
    ``auditinfo_addr`` from <bsm/audit.h>); None when it cannot be read. It is
    the value IOConsoleUsers carries as ``kCGSSessionAuditIDKey``."""
    try:
        import ctypes

        class _Mask(ctypes.Structure):
            _fields_ = [("am_success", ctypes.c_uint32), ("am_failure", ctypes.c_uint32)]

        class _TidAddr(ctypes.Structure):
            _fields_ = [("at_port", ctypes.c_int32), ("at_type", ctypes.c_uint32),
                        ("at_addr", ctypes.c_uint32 * 4)]

        class _AuditinfoAddr(ctypes.Structure):
            _fields_ = [("ai_auid", ctypes.c_uint32), ("ai_mask", _Mask), ("ai_termid", _TidAddr),
                        ("ai_asid", ctypes.c_int32), ("ai_flags", ctypes.c_uint64)]

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        info = _AuditinfoAddr()
        if libc.getaudit_addr(ctypes.byref(info), ctypes.sizeof(info)) != 0:
            return None
        return int(info.ai_asid) if info.ai_asid > 0 else None
    except Exception:  # noqa: BLE001
        return None


_PROBE_CACHE: List[tuple] = []


def probe_state() -> SessionState:
    """The live :class:`SessionState` (read-only; cached for 0.25 s)."""
    now = time.monotonic()
    if _PROBE_CACHE and now - _PROBE_CACHE[0][0] < _PROBE_TTL_S:
        return _PROBE_CACHE[0][1]
    is_vm = probe_is_vm()
    state = SessionState(
        is_vm=is_vm,
        own_uid=os.getuid(),
        sessions=probe_console_sessions(),
        viewer=probe_remote_viewer(),
        own_asid=probe_own_audit_session(),
        prompts=probe_prompt_windows(),
    )
    _PROBE_CACHE[:] = [(now, state)]
    return state


# ---------------------------------------------------------------------------
# The entry points the actuators call
# ---------------------------------------------------------------------------

def _desktop_guard_active() -> bool:
    """The guarded test runner's import guard is loaded in this process."""
    sc = sys.modules.get("sitecustomize")
    if getattr(sc, "MARK", None) != "DESKTOP GUARD":
        return False
    return any(type(f).__name__ == "_Guard" for f in sys.meta_path)


_BYPASS_WARNED: List[bool] = []


def check(where: str, *, probe: Optional[Callable[[], SessionState]] = None) -> Verdict:
    """The verdict for sending input now. Logs one ERROR line on refusal."""
    if os.environ.get(TEST_BYPASS_ENV, "").strip() == "1":
        if _desktop_guard_active():
            if not _BYPASS_WARNED:
                _BYPASS_WARNED.append(True)
                logger.warning("foreground interlock bypassed by %s=1 (test flag; desktop import "
                               "guard active, no real input possible)", TEST_BYPASS_ENV)
            return Verdict(True, where, f"test bypass ({TEST_BYPASS_ENV}=1 under the desktop "
                           "import guard)", "test", bypassed=True)
        logger.error("FOREGROUND INTERLOCK: %s=1 ignored outside the guarded test runner; "
                     "checking for real", TEST_BYPASS_ENV)
    try:
        state = (probe or probe_state)()
        verdict = evaluate(state, where)
    except Exception as exc:  # noqa: BLE001  a broken probe never lets input through
        verdict = Verdict(False, where, f"interlock probe failed: {type(exc).__name__}: {exc}", "host")
    if not verdict.allowed:
        logger.error("FOREGROUND INTERLOCK refused %s: %s", where, verdict.reason)
    return verdict


def refusal(where: str) -> Optional[str]:
    """None when input may be sent now, else the refusal message for the
    caller's own error field."""
    v = check(where)
    return None if v.allowed else f"refused by foreground interlock: {v.reason}"


def require_clear(where: str) -> Verdict:
    """:func:`check`, raising :class:`ForegroundInterlockRefused` on refusal."""
    v = check(where)
    if not v.allowed:
        raise ForegroundInterlockRefused(v)
    return v


# ---------------------------------------------------------------------------
# Host side: is anyone watching a Tart guest?
# ---------------------------------------------------------------------------

def parse_tart_viewer(ps_text: str, vm_name: str) -> ViewerState:
    """From ``ps -axo args`` on the HOST: does ``vm_name``'s tart controller
    offer a view the guest cannot see? ``attached`` None = the VM is not running.

    * no ``--no-graphics`` / ``--vnc`` / ``--vnc-experimental``: a window on the
      host — attached;
    * ``--vnc-experimental``: Virtualization.framework's VNC server, in the host
      process — attached (whether a client is connected is not read);
    * ``--vnc``: the guest's own Screen Sharing, no window — not attached here;
      the guest-side check sees a connected client;
    * ``--no-graphics``: no view at all.
    """
    for line in ps_text.splitlines():
        toks = line.split()
        if "run" not in toks or vm_name not in toks:
            continue
        if not any(os.path.basename(t) == "tart" for t in toks[: toks.index("run")]):
            continue
        if "--vnc-experimental" in toks:
            return ViewerState(True, f"tart run {vm_name}: --vnc-experimental (host-side VNC server, "
                               "not visible from the guest)")
        if "--vnc" in toks:
            return ViewerState(False, f"tart run {vm_name}: --vnc (guest Screen Sharing, checked "
                               "inside the guest)")
        if "--no-graphics" in toks:
            return ViewerState(False, f"tart run {vm_name}: --no-graphics")
        return ViewerState(True, f"tart run {vm_name}: graphics window (no --no-graphics)")
    return ViewerState(None, f"no tart controller for {vm_name!r}")


def host_tart_viewer(vm_name: str) -> ViewerState:
    raw = _run(["/bin/ps", "-axo", "args"])
    if raw is None:
        return ViewerState(None, "ps failed")
    return parse_tart_viewer(raw.decode(errors="replace"), vm_name)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Print the foreground interlock verdict (read-only).")
    ap.add_argument("--tart-vm", help="also report the host-side viewer state of this tart VM")
    args = ap.parse_args(argv)
    v = check("cli status")
    out = {"verdict": v.to_dict()}
    if args.tart_vm:
        out["tart_viewer"] = asdict(host_tart_viewer(args.tart_vm))
    print(json.dumps(out, indent=1, default=str))
    return 0 if v.allowed else 3


if __name__ == "__main__":
    sys.exit(main())
