"""Secrets by reference for open tasks.

A task names a secret as ``${secret:NAME}``; the reference is all a model, a log, a trace, a recording, an MCP
payload or a compiled tool ever holds. The EXECUTOR resolves it at the moment it types the value into a field:

* the macOS Keychain (production): ``/usr/bin/security find-generic-password -s anyuse -a NAME -w`` through an
  injected runner (this module never spawns unless the product's runner is used; the output is never logged);
* a test-only JSON file (``ANYUSE_TEST_SECRETS_FILE``), honoured ONLY when the file lies under ``$HOME`` and ``$HOME``
  is not the account's real home directory (a scratch HOME of a test or a bench), so it can never shadow the
  Keychain of a real installation.

``Redactor`` replaces every resolved secret (and every one-time code a caller answered) with its reference in any
string or nested structure, in its raw, URL-encoded, form-encoded, base64 and JSON-escaped forms; the harness passes
observations, outcomes, reports and compiled tools through it. A value shorter than ``MIN_SECRET_LEN`` cannot be
redacted reliably and is refused (``SecretUnavailable``).
"""
from __future__ import annotations

import base64
import json
import os
import pwd
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: ``${secret:NAME}``; NAME is letters, digits, ``_ . -`` (1-64)
SECRET_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]{1,64})\}")
#: the placeholder a policy writes into a one-time-code field; the executor asks the caller for the code
OTP_REF = "${otp}"
KEYCHAIN_SERVICE = "anyuse"
SECURITY_BIN = "/usr/bin/security"
TEST_FILE_ENV = "ANYUSE_TEST_SECRETS_FILE"
MIN_SECRET_LEN = 4

#: ``runner(argv) -> (rc, stdout, stderr)``
Runner = Callable[[List[str]], Tuple[int, str, str]]


class SecretUnavailable(Exception):
    """A reference that could not be resolved. The message names the reference and why - never a value."""

    def __init__(self, name: str, why: str):
        super().__init__(f"${{secret:{name}}}: {why}")
        self.name, self.why = name, why


def ref(name: str) -> str:
    return "${secret:" + str(name) + "}"


def refs_in(text: Any) -> List[str]:
    """The secret names referenced in ``text`` (in order, no duplicates)."""
    out: List[str] = []
    for m in SECRET_RE.finditer(str(text or "")):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def is_ref(text: Any) -> bool:
    """Is the whole value one secret reference?"""
    return bool(SECRET_RE.fullmatch(str(text or "").strip()))


def has_secret_ref(values: Optional[Dict[str, Any]]) -> bool:
    return any(refs_in(v) for v in (values or {}).values())


def real_home() -> str:
    """The account's real home directory (the password database), whatever ``$HOME`` says."""
    try:
        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001 - unknown: treat every HOME as real (the test file is then refused)
        return ""


def test_file_path() -> Tuple[str, str]:
    """``(path, refusal)`` of the test-only secrets file: honoured only under a scratch ``$HOME``."""
    raw = os.environ.get(TEST_FILE_ENV, "").strip()
    if not raw:
        return "", "no test secrets file is set"
    home = os.path.realpath(os.path.expanduser(os.environ.get("HOME", "") or "~"))
    path = os.path.realpath(os.path.expanduser(raw))
    real = real_home()
    if not real or home == real:
        return "", "the test secrets file is honoured only under a scratch HOME (HOME is the real home)"
    if not (path == home or path.startswith(home + os.sep)):
        return "", "the test secrets file must lie under the scratch HOME"
    return path, ""


def keychain_argv(name: str, service: str = KEYCHAIN_SERVICE, keychain: str = "") -> List[str]:
    argv = [SECURITY_BIN, "find-generic-password", "-s", service, "-a", name, "-w"]
    if keychain:
        argv.append(keychain)
    return argv


def product_runner(argv: List[str]) -> Tuple[int, str, str]:
    """The real Keychain read (the product's only spawn here): a fixed argv, the output kept in memory."""
    import subprocess  # noqa: S404 - the one product place for the Keychain read
    if not argv or argv[0] != SECURITY_BIN or argv[1:2] != ["find-generic-password"]:
        return 2, "", "refused: not a Keychain read"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__
    return r.returncode, r.stdout, ""          # stderr is dropped: it may echo the query, never needed


class SecretStore:
    """Resolves references for ONE run and remembers what it resolved, for the redactor. ``runner``: the Keychain
    read (``product_runner`` in a product run; a test injects a fake or none). The test file wins only where
    :func:`test_file_path` allows it."""

    def __init__(self, runner: Optional[Runner] = None, *, service: str = KEYCHAIN_SERVICE, keychain: str = "",
                 use_test_file: bool = True):
        self.runner = runner
        self.service, self.keychain = service, keychain
        self.use_test_file = use_test_file
        self._resolved: Dict[str, str] = {}
        self.redactor = Redactor()
        self.lookups: List[Dict[str, str]] = []      # name + backend + ok (never a value)

    def _from_test_file(self, name: str) -> Optional[str]:
        if not self.use_test_file:
            return None
        path, why = test_file_path()
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        v = data.get(name) if isinstance(data, dict) else None
        return v if isinstance(v, str) else None

    def resolve(self, name: str) -> str:
        if name in self._resolved:
            return self._resolved[name]
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", str(name or "")):
            raise SecretUnavailable(str(name)[:64], "not a valid secret name")
        value = self._from_test_file(name)
        backend = "test_file"
        if value is None:
            backend = "keychain"
            if self.runner is None:
                self.lookups.append({"name": name, "backend": backend, "ok": "no"})
                raise SecretUnavailable(name, "no Keychain reader in this process")
            rc, out, _err = self.runner(keychain_argv(name, self.service, self.keychain))
            if rc != 0:
                self.lookups.append({"name": name, "backend": backend, "ok": "no"})
                raise SecretUnavailable(name, "not found in the Keychain" if rc == 44 else f"Keychain read failed (rc {rc})")
            value = (out or "").rstrip("\n")
        if len(value) < MIN_SECRET_LEN:
            self.lookups.append({"name": name, "backend": backend, "ok": "no"})
            raise SecretUnavailable(name, f"shorter than {MIN_SECRET_LEN} characters: it could not be kept out of "
                                          "logs reliably")
        self._resolved[name] = value
        self.redactor.add(value, ref(name))
        self.lookups.append({"name": name, "backend": backend, "ok": "yes"})
        return value

    def resolve_text(self, text: str) -> str:
        """``text`` with every reference resolved (for typing only; never stored)."""
        return SECRET_RE.sub(lambda m: self.resolve(m.group(1)), str(text or ""))

    def note_otp(self, code: str) -> None:
        """A one-time code the caller answered: typed once, redacted everywhere as ``${otp}``."""
        if code:
            self.redactor.add(str(code), OTP_REF, min_len=4)


class Redactor:
    """Replaces known secret values by their references in strings and nested structures."""

    def __init__(self):
        self._pairs: List[Tuple[str, str]] = []

    def add(self, value: str, reference: str, min_len: int = MIN_SECRET_LEN) -> None:
        v = str(value or "")
        if len(v) < min_len:
            return
        forms = {v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), urllib.parse.quote(v),
                 json.dumps(v)[1:-1], base64.b64encode(v.encode()).decode().rstrip("="),
                 base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")}
        for f in forms:
            if f and len(f) >= min_len and (f, reference) not in self._pairs:
                self._pairs.append((f, reference))
        # longest first, so a value inside another form is not half-replaced
        self._pairs.sort(key=lambda p: -len(p[0]))

    @property
    def active(self) -> bool:
        return bool(self._pairs)

    def text(self, s: Any) -> Any:
        if not isinstance(s, str) or not self._pairs:
            return s
        for v, r in self._pairs:
            if v in s:
                s = s.replace(v, r)
        return s

    def obj(self, o: Any) -> Any:
        """A redacted copy of ``o`` (dicts, lists, tuples, strings; other values unchanged)."""
        if not self._pairs:
            return o
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            t = [self.obj(x) for x in o]
            return t if isinstance(o, list) else tuple(t)
        return o

    def leaks(self, s: str) -> int:
        """How many known values occur in ``s`` (the bench's leak check uses its own grep; this is a self-check)."""
        return sum(s.count(v) for v, _r in self._pairs) if isinstance(s, str) else 0


def count_occurrences(blob: str, values: Sequence[str]) -> int:
    """The leak check's grep: every value in its raw, URL-, form- and base64-encoded and JSON-escaped forms."""
    n = 0
    for v in values:
        if not v:
            continue
        forms = {v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), json.dumps(v)[1:-1],
                 base64.b64encode(v.encode()).decode().rstrip("="),
                 base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")}
        n += sum(blob.count(f) for f in forms if f)
    return n
