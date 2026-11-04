"""Terminal gate for out-of-band routine approvals (``anyuse routine approve|reconcile``).

An MCP client may itself be a model, and an agent with a shell running as the
same user can run the CLI. This gate raises the bar for the second case as far
as a command-line tool can. It is NOT authentication (docs/ROUTINES.md,
docs/THREAT_MODEL.md); the stronger path (a macOS LocalAuthentication / Touch
ID prompt) belongs to a native app.

1. **A terminal on both ends.** stdin AND stdout must be TTYs. The answer is
   read from ``/dev/tty``, never from stdin, a pipe, the environment or an
   argument.
2. **Typed-back code.** The effect summary is written to ``/dev/tty``, and the
   person types back a short code derived from the confirmation digest and a
   fresh random nonce. The code is never an argument. ``--confirm <digest>``
   still binds the request/effect; on its own it is no longer sufficient.
3. **Agent markers.** If well-known agent environment markers are present
   (``CLAUDECODE``, ``CLAUDE_CODE_*``, ``CODEX_*`` ...), the gate refuses
   unless a per-machine opt-in file exists. That file can only be written by
   ``anyuse routine allow-agent-terminals`` from a TTY session with no agent
   marker, after the same typed-back code. It is owner-only (0600) and names
   this host.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

AGENT_ENV_EXACT = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME",
    "GEMINI_CLI", "CURSOR_AGENT", "AIDER_MODEL", "OPENHANDS",
})
AGENT_ENV_PREFIXES = ("CLAUDE_CODE_", "CODEX_")
OPTIN_FILE = "routine_agent_terminal_optin.json"
OPTIN_SCHEMA = "anyuse.human_gate_optin/1"
CODE_LEN = 6


def agent_markers(env: Mapping[str, str]) -> List[str]:
    """Names of agent-marker variables present (values are never read)."""
    return sorted(k for k in env if k in AGENT_ENV_EXACT
                  or any(k.startswith(p) for p in AGENT_ENV_PREFIXES))


def short_code(digest: str, nonce: str) -> str:
    """Six characters derived from the digest and a per-invocation nonce."""
    raw = hashlib.sha256(f"{nonce}:{digest}".encode("utf-8")).hexdigest()
    return raw[:CODE_LEN].upper()


def optin_path() -> str:
    # The secret dir is pinned to the home directory by design (paths.py);
    # it does not follow ANYUSE_DATA_DIR.
    from anyuse.paths import anyuse_secret_dir
    return str(anyuse_secret_dir() / OPTIN_FILE)


def _stdio_are_ttys() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _new_nonce() -> str:
    return secrets.token_hex(8)


def _open_dev_tty():
    return open("/dev/tty", "r+", encoding="utf-8")


class TtyGate:
    """``gate(summary, digest) -> None`` when a person confirmed, else a reason."""

    def __init__(self, *, env: Optional[Mapping[str, str]] = None,
                 stdio_are_ttys: Optional[Callable[[], bool]] = None,
                 open_tty: Optional[Callable[[], Any]] = None,
                 nonce_factory: Optional[Callable[[], str]] = None,
                 hostname: Optional[str] = None, optin_file: Optional[str] = None):
        # Defaults are resolved at construction time (module attributes), so
        # offline tests can substitute a fake terminal.
        self.env = dict(os.environ if env is None else env)
        self.stdio_are_ttys = stdio_are_ttys or _stdio_are_ttys
        self.open_tty = open_tty or _open_dev_tty
        self.nonce_factory = nonce_factory or _new_nonce
        self.hostname = hostname or socket.gethostname()
        self.optin_file = optin_file or optin_path()

    # -- checks ---------------------------------------------------------------

    def optin_valid(self) -> bool:
        try:
            st = os.stat(self.optin_file)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                return False
            with open(self.optin_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        return (isinstance(data, dict) and data.get("schema") == OPTIN_SCHEMA
                and data.get("host") == self.hostname and data.get("written_by") == "tty")

    def environment_refusal(self, *, allow_optin: bool = True) -> Optional[str]:
        if not self.stdio_are_ttys():
            return ("refused: stdin and stdout must both be a terminal; this command "
                    "cannot be driven through a pipe or by another program's stdin")
        markers = agent_markers(self.env)
        if markers and not (allow_optin and self.optin_valid()):
            return ("refused: agent environment marker(s) present "
                    f"({', '.join(markers)}); run this from your own terminal, or opt "
                    "this machine in with `anyuse routine allow-agent-terminals` "
                    "from a terminal without agent markers")
        return None

    # -- the prompt -------------------------------------------------------------

    def _ask(self, lines: Iterable[str], digest: str, verb: str) -> Optional[str]:
        code = short_code(digest, self.nonce_factory())
        try:
            with self.open_tty() as tty:
                for line in lines:
                    tty.write(line + "\n")
                tty.write(f"To {verb}, type this code: {code}\n> ")
                tty.flush()
                answer = (tty.readline() or "").strip().upper()
        except OSError as exc:
            return f"refused: cannot open the controlling terminal ({exc})"
        if not hmac.compare_digest(answer, code):
            return "refused: the typed code did not match; nothing was recorded"
        return None

    def __call__(self, summary: Mapping[str, Any], digest: str) -> Optional[str]:
        why = self.environment_refusal()
        if why:
            return why
        lines = ["", "AnyUse routine: a person must confirm this.", ""]
        for key in ("purpose", "manifest", "version", "run_id", "step", "effects",
                    "action", "bound_inputs", "outcome", "outputs", "inputs_digest"):
            if key in summary:
                text = json.dumps(summary[key], sort_keys=True, default=str)
                lines.append(f"  {key:14} {text[:500]}")
        lines += [f"  {'digest':14} {digest}", ""]
        return self._ask(lines, digest, str(summary.get("purpose") or "confirm"))

    def write_optin(self) -> Dict[str, Any]:
        """Per-machine opt-in: only from a TTY session with no agent marker."""
        why = self.environment_refusal(allow_optin=False)
        if why:
            return {"status": "refused", "error": why}
        digest = hashlib.sha256(f"optin:{self.hostname}:{self.optin_file}".encode()).hexdigest()
        why = self._ask(["", "Allow `anyuse routine approve|reconcile` to run in terminals",
                         f"that carry agent markers, on this machine ({self.hostname}).",
                         "Only do this if you understand that an agent in such a terminal",
                         "could then show you a prompt you might answer.", ""],
                        digest, "allow")
        if why:
            return {"status": "refused", "error": why}
        os.makedirs(os.path.dirname(self.optin_file), mode=0o700, exist_ok=True)
        tmp = self.optin_file + f".tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"schema": OPTIN_SCHEMA, "host": self.hostname, "written_by": "tty",
                       "created": time.time()}, fh)
        os.replace(tmp, self.optin_file)
        return {"status": "allowed", "file": self.optin_file}
