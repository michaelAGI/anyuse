"""The container sandbox RUNNER, wired and NOT started.

``harness_toolwrite.ContainerSandbox.plan`` is the exact argv; this module is the one place that may hand
it to the operating system, and it does so only when BOTH hold, checked before every call:

1. ``ANYUSE_AGENT_CONTAINER_SANDBOX`` is on (a security-relevant setting: enabling it takes the typed
   code; the default is off);
2. no tart VM is running on this host (``ps -axo args`` shows no ``tart run <vm>``), until the
   vsock fix is confirmed (``VSOCK_FIX_CONFIRMED``; a constant in the code, not a setting).

Anything else answers rc 125 with a typed refusal and starts nothing. The argv must be a container
sandbox plan (``container run`` with ``--network none`` and a non-root ``--user``); any other argv is refused as well. ``subprocess``
is imported only inside the one function that starts a container, after the guard passed; tests inject
``run`` and ``ps_reader`` and never reach it.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

SETTING = "ANYUSE_AGENT_CONTAINER_SANDBOX"
#: True once the VM's network no longer breaks when a container starts (the vsock
#: fix); until then a running tart VM refuses the runner even when the setting is on
VSOCK_FIX_CONFIRMED = True  # the harness drives the VM over vsock (tart exec); a container start no longer cuts it off
REFUSED_RC = 125

RunFn = Callable[[List[str], str, float], Tuple[int, str, str]]


def tart_running(ps_text: str) -> Optional[str]:
    """The VM name of a ``tart run <vm>`` process in ``ps -axo args`` output, else None."""
    for line in (ps_text or "").splitlines():
        toks = line.split()
        if "run" not in toks:
            continue
        i = toks.index("run")
        if not any(os.path.basename(t) == "tart" for t in toks[:i]):
            continue
        rest = [t for t in toks[i + 1:] if not t.startswith("--")]
        return rest[0] if rest else "(unnamed)"
    return None


def enabled() -> bool:
    from anyuse.settings import get as get_setting
    return bool(get_setting(SETTING))


def refusal(*, enabled_now: Optional[bool] = None, ps_text: Optional[str] = None,
            vsock_confirmed: Optional[bool] = None) -> str:
    """Why the runner may not start a container now, or ``""``."""
    on = enabled() if enabled_now is None else bool(enabled_now)
    if not on:
        return f"{SETTING} is off (enabling it takes the typed code); the container sandbox stays unstarted"
    vm = tart_running(ps_text or "")
    confirmed = VSOCK_FIX_CONFIRMED if vsock_confirmed is None else bool(vsock_confirmed)
    if vm and not confirmed:
        return (f"a tart VM is running ({vm}); a container start breaks the VM's network until the vsock fix "
                "is confirmed by main")
    return ""


def non_root_user(argv: List[str]) -> bool:
    """The plan names an unprivileged user (``--user`` / ``-u`` with a uid or name that is not root)."""
    for flag in ("--user", "-u"):
        if flag in argv:
            u = str(argv[argv.index(flag) + 1]) if argv.index(flag) + 1 < len(argv) else ""
            uid = u.split(":")[0].strip()
            return bool(uid) and uid not in ("0", "root")
    return False


def is_sandbox_plan(argv: List[str]) -> bool:
    try:
        return list(argv[:2]) == ["container", "run"] and argv[argv.index("--network") + 1] == "none" \
            and "--rm" in argv and not any(a.startswith("--privileged") for a in argv) and non_root_user(argv)
    except (ValueError, IndexError, TypeError):
        return False


def _ps() -> str:
    """``ps -axo args`` on the host (only reached when the setting is on)."""
    import subprocess  # noqa: S404 - the one guarded place
    try:
        return subprocess.run(["/bin/ps", "-axo", "args"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


#: where Apple's ``container`` CLI is installed (Homebrew on Apple silicon, the pkg installer, Intel Homebrew)
CONTAINER_LOCATIONS = ("/opt/homebrew/bin/container", "/usr/local/bin/container")
#: the runner's whole environment: no key, no HOME, nothing of AnyUse's; the CLI reaches its service over XPC
RUN_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def container_binary(exists: Callable[[str], bool] = os.path.isfile) -> str:
    """The absolute path of the ``container`` CLI, or ``""`` (the runner's minimal PATH holds no Homebrew
    directory, so a PATH lookup never finds ``container``)."""
    return next((p for p in CONTAINER_LOCATIONS if exists(p)), "")


def _start(argv: List[str], stdin_text: str, timeout: float) -> Tuple[int, str, str]:
    """Start the container (only after the guard passed)."""
    import subprocess  # noqa: S404 - the one guarded place
    binary = container_binary()
    if not binary:
        return REFUSED_RC, "", f"the container CLI is not installed ({', '.join(CONTAINER_LOCATIONS)})"
    try:
        r = subprocess.run([binary] + list(argv[1:]), input=stdin_text, capture_output=True, text=True,
                           timeout=timeout, env={"PATH": RUN_PATH})
    except subprocess.TimeoutExpired:
        return REFUSED_RC, "", f"timeout after {timeout:.0f} s"
    except OSError as exc:
        return REFUSED_RC, "", f"{type(exc).__name__}: {exc}"
    return r.returncode, r.stdout, r.stderr


def container_runner(*, enabled_now: Optional[bool] = None, ps_reader: Optional[Callable[[], str]] = None,
                     run: Optional[RunFn] = None, vsock_confirmed: Optional[bool] = None) -> RunFn:
    """A ``runner(argv, stdin_text, timeout) -> (rc, out, err)`` for ``ContainerSandbox``; the guard is
    checked on EVERY call. ``ps_reader`` / ``run`` are injected by tests (no ``ps``, no container)."""
    def runner(argv: List[str], stdin_text: str, timeout: float) -> Tuple[int, str, str]:
        on = enabled() if enabled_now is None else bool(enabled_now)
        ps_text = ""
        if on:
            ps_text = (ps_reader or _ps)()
        why = refusal(enabled_now=on, ps_text=ps_text, vsock_confirmed=vsock_confirmed)
        if why:
            return REFUSED_RC, "", "refused: " + why
        if not is_sandbox_plan(list(argv)):
            return REFUSED_RC, "", "refused: not a container sandbox plan (container run --rm --network none --user <non-root> ...)"
        return (run or _start)(list(argv), stdin_text, float(timeout))
    return runner


def status(*, ps_text: Optional[str] = None) -> dict:
    """What ``anyuse agent`` reports about the sandbox (read-only)."""
    on = enabled()
    ps = ps_text if ps_text is not None else (_ps() if on else "")
    vm = tart_running(ps)
    # the verdict reads the SAME process list as the VM line (with an empty one, a running VM was
    # reported while "would start" ignored it whenever the vsock fix was unconfirmed)
    return {"setting": SETTING, "enabled": on, "tart_vm": vm, "vsock_fix_confirmed": VSOCK_FIX_CONFIRMED,
            "would_start": not refusal(enabled_now=on, ps_text=ps)}
