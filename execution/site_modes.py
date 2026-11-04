"""Per-site execution modes for the web tier (docs/SITE_MODES.md).

The compiled-workflow web tier is conservative by default: it identifies itself
honestly (``anyuse-compiled-workflow/0.1``), respects ``robots.txt``, refuses
credential-bound requests and routes logged-in work through the real UI. Each
of those is a PER-SITE OPTION whose DEFAULT is exactly that behaviour. Four
options, each per registrable domain:

  ``identity``  ``honest`` (default) | ``browser``  — the replay sends the
                User-Agent CAPTURED from the recorded browser request, and only
                that header changes. A workflow with no captured User-Agent is
                refused under ``browser``; nothing is invented.
  ``robots``    ``respect`` (default) | ``ignore``  — do not consult robots.txt
                for this site's replays. Pacing stays on in every mode.
  ``login``     ``none`` (default) | ``replay``     — a credential-bound request
                may compile and replay, using a session imported from a browser
                profile a human picked (execution/session_import.py).
  ``route``     ``ui`` (default) | ``replay``       — logged-in work takes the
                real UI path, or the fast HTTP replay (requires login=replay).

Storage key: the registrable domain, but only when it is CERTAIN without a
public-suffix list (:func:`site_key`). There is no network, so no PSL can be
fetched; an entry whose registrable domain would be a guess is refused. Bare
TLDs, public suffixes, shared-hosting registries (the parent itself) and ``*``
are refused; there is no wildcard, no global default and no environment
variable.

ONE accessor reads modes on the PRODUCT execution path: :func:`effective_modes`
(and :func:`browser_identity_for`, which is a thin view of it). The guarded
benchmark browser (execution/cdp_guard.py) and the bench drivers never call it,
so a measured bench always runs the safe identity. NEVER_CONTACT hosts, and
government and public-body hosts (execution/gov_hosts.py) when
``ANYUSE_REFUSE_GOV_HOSTS`` is on, are refused at write time
(:func:`validate_host`) and ignored-and-reported at use time.
"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SITE_MODES_FILE = "site_modes.toml"

#: option -> (default, allowed values). The default row reproduces today.
OPTIONS: Dict[str, tuple] = {
    "identity": ("honest", ("honest", "browser")),
    "robots": ("respect", ("respect", "ignore")),
    "login": ("none", ("none", "replay")),
    "route": ("ui", ("ui", "replay")),
}
DEFAULTS: Dict[str, str] = {name: spec[0] for name, spec in OPTIONS.items()}


class SiteModeError(ValueError):
    """A host or mode value is invalid, or reading/writing the file failed."""


@dataclass
class SiteModes:
    """The modes for one registrable domain. Defaults reproduce today exactly."""
    host: str                                    # the registrable domain (site key)
    identity: str = "honest"
    robots: str = "respect"
    login: str = "none"
    route: str = "ui"
    #: login=replay: the REFERENCE to the imported session. Never values: only
    #: the browser, the profile directory name, the cookie host, the chosen
    #: cookie-database path and the cookie NAMES. ``None`` when login is none.
    session: Optional[Dict[str, Any]] = None

    def is_default(self) -> bool:
        return (self.identity == "honest" and self.robots == "respect"
                and self.login == "none" and self.route == "ui"
                and not self.session)

    def to_table(self) -> Dict[str, Any]:
        table: Dict[str, Any] = {k: getattr(self, k) for k in OPTIONS}
        if self.session:
            table["session_browser"] = str(self.session.get("browser") or "")
            table["session_profile"] = str(self.session.get("profile") or "")
            table["session_host"] = str(self.session.get("host") or "")
            table["session_db_path"] = str(self.session.get("db_path") or "")
            table["session_cookie_names"] = [str(n) for n in
                                             (self.session.get("cookie_names") or [])]
        return table

    @classmethod
    def from_table(cls, host: str, table: Mapping[str, Any]) -> "SiteModes":
        session = None
        if table.get("session_browser") or table.get("session_cookie_names"):
            session = {
                "browser": str(table.get("session_browser") or ""),
                "profile": str(table.get("session_profile") or ""),
                "host": str(table.get("session_host") or ""),
                "db_path": str(table.get("session_db_path") or ""),
                "cookie_names": [str(n) for n in (table.get("session_cookie_names") or [])],
            }
        return cls(
            host=host,
            identity=str(table.get("identity") or DEFAULTS["identity"]),
            robots=str(table.get("robots") or DEFAULTS["robots"]),
            login=str(table.get("login") or DEFAULTS["login"]),
            route=str(table.get("route") or DEFAULTS["route"]),
            session=session,
        )


# ---------------------------------------------------------------------------
# the certain-registrable-domain rule (no PSL, no network: refuse, never guess)
# ---------------------------------------------------------------------------

#: TLDs where the registrable domain is the second label, except for the public
#: second-level registries listed per TLD in :data:`TLD_SECOND_LEVELS`. Chosen
#: so every public second level is enumerable here; TLDs with many geographic or
#: functional second levels (``it``, ``no``, ``pl``, ``se``, ``jp``, ``br``,
#: ``us``, ``uk`` …) are deliberately NOT here, so an entry under them needs a
#: known multi-label suffix (``co.uk``, ``com.br``) or is refused.
FLAT_TLDS = frozenset({
    # generic
    "com", "net", "org", "info", "biz", "io", "ai", "app", "dev", "xyz", "tech",
    "online", "site", "store", "cloud", "page", "blog", "shop", "tv", "cc", "fm",
    "me", "co", "to", "eu", "ws",
    # country, flat or with a short, enumerable list of public second levels
    "de", "nl", "ch", "li", "at", "be", "dk", "fi", "cz", "es", "fr", "ca", "ie",
    "lu", "is", "ee", "lt", "lv", "si", "sk", "pt",
})
#: Public second-level registries under a FLAT TLD (an entry needs one more
#: label under these: ``x.co.at``, never ``co.at``).
TLD_SECOND_LEVELS: Dict[str, frozenset] = {
    "at": frozenset({"ac", "co", "gv", "or", "priv"}),
    "be": frozenset({"ac"}),
    "ca": frozenset({"ab", "bc", "mb", "nb", "nf", "nl", "ns", "nt", "nu", "on", "pe",
                     "qc", "sk", "yk", "gc"}),
    "co": frozenset({"arts", "com", "edu", "firm", "gov", "info", "int", "mil", "net",
                     "nom", "org", "rec", "web"}),
    "ee": frozenset({"aip", "com", "edu", "fie", "gov", "lib", "med", "org", "pri", "riik"}),
    "es": frozenset({"com", "edu", "gob", "nom", "org"}),
    "fi": frozenset({"aland"}),
    "fr": frozenset({"aeroport", "asso", "avocat", "avoues", "cci", "chambagri",
                     "chirurgiens-dentistes", "com", "experts-comptables",
                     "geometre-expert", "gouv", "greta", "huissier-justice", "medecin",
                     "nom", "notaires", "pharmacien", "port", "prd", "tm", "veterinaire"}),
    "ie": frozenset({"gov"}),
    "is": frozenset({"com", "edu", "gov", "int", "net", "org"}),
    "lt": frozenset({"gov"}),
    "lv": frozenset({"asn", "com", "conf", "edu", "gov", "id", "mil", "net", "org"}),
    "me": frozenset({"ac", "co", "edu", "gov", "its", "net", "org", "priv"}),
    "nl": frozenset({"bv"}),
    "pt": frozenset({"com", "edu", "gov", "int", "net", "nome", "org", "publ"}),
    "to": frozenset({"com", "edu", "gov", "mil", "net", "org"}),
    "ws": frozenset({"com", "edu", "gov", "net", "org"}),
    "ai": frozenset({"com", "net", "off", "org"}),
}
#: Shared-hosting / private registries: every label under them is a different
#: owner, so the SITE is the full tenant host and the registry itself is refused.
EXTRA_PRIVATE_REGISTRIES = frozenset({
    "duckdns.org", "dyndns.org", "no-ip.org", "ddns.net", "hopto.org", "zapto.org",
    "myftp.org", "freeddns.org", "dynu.net", "mooo.com", "afraid.org", "eu.org",
    "ngrok-free.app", "ngrok.app", "ngrok.dev", "glitch.me", "fly.dev", "deno.dev",
    "onrender.com", "railway.app", "up.railway.app", "replit.dev", "repl.co",
    # CentralNic and similar sub-registries under flat gTLDs
    "uk.com", "us.com", "eu.com", "gb.com", "de.com", "cn.com", "jpn.com", "kr.com",
    "no.com", "qc.com", "ru.com", "sa.com", "se.com", "za.com", "br.com", "uy.com",
    "ar.com", "hu.com", "gr.com", "mex.com", "co.com", "uk.net", "gb.net", "se.net",
    "hu.net", "in.net", "jp.net", "za.net", "us.org", "za.org", "ae.org",
})
_LABEL_OK = __import__("re").compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _private_registries() -> frozenset:
    from anyuse.execution.web_scope import PRIVATE_SUFFIXES
    return frozenset(PRIVATE_SUFFIXES) | EXTRA_PRIVATE_REGISTRIES


def _site_key(name: str) -> str:
    """The CERTAIN registrable domain of an already-encoded ``name``, or raise."""
    from anyuse.execution.web_scope import PUBLIC_SUFFIX2
    labels = name.split(".")
    if len(labels) < 2:
        raise SiteModeError(f"{name!r} is a bare TLD / single label — give a registrable "
                            "domain such as example.com")
    private = _private_registries()
    for n in (4, 3, 2):
        if len(labels) >= n and ".".join(labels[-n:]) in private:
            if len(labels) == n:
                raise SiteModeError(
                    f"{name!r} is a shared-hosting registry (every subdomain is a different "
                    f"owner); name the full subdomain host, e.g. you.{name}")
            return ".".join(labels[-(n + 1):])
    tld = labels[-1]
    two = ".".join(labels[-2:])
    second_level_suffix = (two in PUBLIC_SUFFIX2
                           or labels[-2] in TLD_SECOND_LEVELS.get(tld, frozenset()))
    if second_level_suffix:
        if len(labels) == 2:
            raise SiteModeError(f"{name!r} is a public suffix, not a site; give a "
                                f"registrable domain under it (e.g. example.{name})")
        return ".".join(labels[-3:])
    if tld in FLAT_TLDS:
        return two
    raise SiteModeError(
        f"cannot tell the registrable domain of {name!r} without a public-suffix list "
        f"(.{tld} has second-level registries this build does not enumerate), so it "
        f"is refused rather than guessed; see docs/SITE_MODES.md")


def _encode_host(raw: str) -> str:
    """IDNA-encode a host. UTS46 (non-transitional, so ``ß`` stays ``ß``) through
    the ``idna`` package (an httpx dependency); without it, refuse any host whose
    IDNA2003 and NFC forms disagree or that holds ß/ς/ZWJ/ZWNJ."""
    name = raw.strip().lower().rstrip(".")
    if all(ord(c) < 128 for c in name):
        return name
    try:
        import idna
    except ImportError:
        if any(c in name for c in ("ß", "ς", "‍", "‌")):
            raise SiteModeError(f"{raw!r} needs UTS46 IDNA, which is unavailable") from None
        nfc = unicodedata.normalize("NFC", name)
        try:
            enc = nfc.encode("idna").decode("ascii")
        except UnicodeError:
            raise SiteModeError(f"{raw!r} is not a valid host name") from None
        if enc.encode("ascii").decode("idna") != nfc:
            raise SiteModeError(f"{raw!r} encodes ambiguously; refused") from None
        return enc.lower()
    try:
        return idna.encode(name, uts46=True, transitional=False).decode("ascii").lower()
    except idna.IDNAError:
        raise SiteModeError(f"{raw!r} is not a valid host name") from None


def _host(host: str) -> str:
    return (host or "").split("/")[0].split(":", 1)[0].strip().lower().rstrip(".")


def _encoded(host: str) -> str:
    """The normalised, IDNA-encoded host name, or raise."""
    raw = (host or "").strip()
    if not raw or "*" in raw or any(c.isspace() for c in raw):
        raise SiteModeError("a site is one registrable domain (e.g. example.com); "
                            "wildcards and '*' are refused")
    name = _encode_host(_host(raw))
    labels = name.split(".")
    if not name or any(not _LABEL_OK.match(lab) for lab in labels):
        raise SiteModeError(f"{name!r} has characters that are not allowed in a host name "
                            "(letters, digits, hyphens and dots only)")
    return name


def site_key(host: str) -> str:
    """The certain registrable domain a mode for ``host`` is stored under, or
    ``""`` when it cannot be determined without guessing (never raises)."""
    try:
        return _site_key(_encoded(host))
    except SiteModeError:
        return ""


# ---------------------------------------------------------------------------
# the government / never-contact guard
# ---------------------------------------------------------------------------

def _never_contact_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_runtime", "NEVER_CONTACT.txt")


def _user_never_contact_path() -> str:
    from anyuse.paths import anyuse_data_path
    return str(anyuse_data_path("never_contact.txt"))


def _read_never_contact_file(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip().lower() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def _never_contact() -> set:
    """The bundled NEVER_CONTACT list plus user/watch additions under ANYUSE_DATA_DIR.

    On the product path a missing bench file is not a refusal; the bench side
    keeps its own fail-closed read (execution/web_runtime/never_contact.py)."""
    return _read_never_contact_file(_never_contact_path()) | _read_never_contact_file(_user_never_contact_path())


def add_user_never_contact(host: str, why: str = "") -> str:
    """Record ``host`` in ``ANYUSE_DATA_DIR/never_contact.txt`` without modifying bundled package data."""
    import fcntl
    import time
    h = _host(host)
    path = _user_never_contact_path()
    if not h:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            lines = []
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                lines = []
            existing = {ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")}
            if h in existing or h in _read_never_contact_file(_never_contact_path()):
                return path
            head = [ln for ln in lines if ln.startswith("#")]
            body = sorted(existing | {h})
            if why:
                head.append(f"# {time.strftime('%Y-%m-%d')}: {h}: {why[:90]}")
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(head + body) + "\n")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
    return path


def host_block_reason(host: str) -> str:
    """Why modes may not apply to ``host`` at all (the government rule when
    ``ANYUSE_REFUSE_GOV_HOSTS`` is on / never-contact), or ``""``. Checked
    against both the exact host and its site key."""
    from anyuse.execution.gov_hosts import government_refusal, rule_on
    name = _host(host)
    if not name:
        return ""
    key = site_key(name)
    on = rule_on()
    gov = government_refusal(name, on) or (government_refusal(key, on) if key and key != name else "")
    if gov:
        return f"government / public body ({gov})"
    nc = _never_contact()
    if name in nc or (key and key in nc):
        return "on the never-contact list"
    return ""


def validate_host(host: str) -> str:
    """Normalise ``host`` to the site key it is stored under, or raise.

    Refuses ``*``, wildcards and whitespace, a host that is not letters/digits/
    hyphens/dots after UTS46 IDNA encoding, a bare TLD, a public suffix, a
    shared-hosting registry itself, any host whose registrable domain is not
    certain without a public-suffix list, every NEVER_CONTACT host, and every
    government / public-body host when ``ANYUSE_REFUSE_GOV_HOSTS`` is on."""
    key = _site_key(_encoded(host))
    block = host_block_reason(key)
    if block:
        raise SiteModeError(f"refusing to set modes for {key}: {block}")
    return key


# ---------------------------------------------------------------------------
# storage — one TOML file, owner-only, atomic write (settings.py pattern)
# ---------------------------------------------------------------------------

def site_modes_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    if path is not None:
        return Path(path)
    from anyuse.paths import anyuse_data_path
    return anyuse_data_path(SITE_MODES_FILE)


def _read_trusted(path: Optional[os.PathLike[str] | str] = None) -> Tuple[Optional[bytes], str]:
    """``(bytes, "")`` for a trusted file, ``(None, "")`` when absent, else
    ``(None, reason)``. The parent directory must be owned by this user and not
    group/world-writable; the file is opened with ``O_NOFOLLOW`` and the OPEN
    descriptor is fstat'ed (regular file, this owner, not group/world-writable),
    so a symlink or a swap between check and read is refused."""
    target = site_modes_path(path)
    try:
        pst = os.stat(target.parent)
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"cannot stat {target.parent}: {exc}"
    if pst.st_uid != os.getuid():
        return None, f"{target.parent} is not owned by this user"
    if pst.st_mode & 0o022:
        return None, f"{target.parent} is group/world-writable"
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"cannot open {target} safely ({exc}); a symlink is refused"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, f"{target} is not a regular file"
        if st.st_uid != os.getuid():
            return None, f"{target} is not owned by this user"
        if st.st_mode & 0o022:
            return None, f"{target} is group/world-writable"
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), ""
    finally:
        os.close(fd)


def file_trust_reason(path: Optional[os.PathLike[str] | str] = None) -> str:
    """Why ``site_modes.toml`` may not be trusted, or ``""`` (absent or trusted)."""
    return _read_trusted(path)[1]


def load(path: Optional[os.PathLike[str] | str] = None, *,
         strict: bool = False) -> Dict[str, Dict[str, Any]]:
    """Every stored site table, keyed by site key. ``{}`` if no file.

    An untrusted file yields no modes. With ``strict`` an unreadable or
    untrusted file RAISES instead of returning ``{}``, so a writer never
    silently replaces a file it could not parse."""
    import tomllib
    target = site_modes_path(path)
    data, why = _read_trusted(path)
    if why:
        if strict:
            raise SiteModeError(f"refusing to use {target}: {why}")
        logger.warning("ignoring %s: %s", target, why)
        return {}
    if data is None:
        return {}
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        if strict:
            raise SiteModeError(f"refusing to use {target}: {exc}") from exc
        logger.warning("could not read %s: %s", target, exc)
        return {}
    sites = parsed.get("sites")
    return {str(k): dict(v) for k, v in sites.items() if isinstance(v, dict)} \
        if isinstance(sites, dict) else {}


def _toml_str(text: Any) -> str:
    """A TOML basic string with EVERY control character escaped."""
    out = []
    for ch in str(text):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    return _toml_str(value)


def _write(sites: Mapping[str, Mapping[str, Any]],
           path: Optional[os.PathLike[str] | str] = None) -> Path:
    target = site_modes_path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = ["# AnyUse per-site execution modes. Managed by `anyuse sites`.\n",
             "# Every entry is opt-in; a missing site runs today's safe defaults.\n"]
    for host in sorted(sites):
        table = sites[host]
        lines.append(f"\n[sites.{_toml_str(host)}]\n")
        for key in sorted(table):
            lines.append(f"{key} = {_toml_scalar(table[key])}\n")
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".tmp-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def stored_modes(host: str, path: Optional[os.PathLike[str] | str] = None) -> Optional[SiteModes]:
    """The RAW stored modes for ``host`` (by site key), or ``None``.

    No government / never-contact guard: this is the view ``anyuse sites show``
    prints. The product path uses :func:`effective_modes` instead."""
    key = site_key(host)
    if not key:
        return None
    table = load(path).get(key)
    return SiteModes.from_table(key, table) if table is not None else None


def set_modes(host: str, *, identity: Optional[str] = None, robots: Optional[str] = None,
              login: Optional[str] = None, route: Optional[str] = None,
              session: Optional[Dict[str, Any]] = None,
              path: Optional[os.PathLike[str] | str] = None) -> SiteModes:
    """Validate and persist the modes for ``host``. Unspecified options keep
    their current stored value (or the default). ``login=replay`` and
    ``route=replay`` are checked for consistency."""
    key = validate_host(host)
    sites = load(path, strict=True)      # refuse to clobber an unreadable/untrusted file
    current = SiteModes.from_table(key, sites.get(key) or {})
    for name, value in (("identity", identity), ("robots", robots),
                        ("login", login), ("route", route)):
        if value is None:
            continue
        allowed = OPTIONS[name][1]
        low = str(value).strip().lower()
        if low not in allowed:
            raise SiteModeError(f"{name} must be one of: {', '.join(allowed)}")
        setattr(current, name, low)
    if session is not None:
        current.session = session or None
    if current.route == "replay" and current.login != "replay":
        raise SiteModeError("route=replay requires login=replay (a fast HTTP replay of "
                            "logged-in work needs an imported session)")
    if current.login != "replay":
        current.session = None
    current.host = key
    sites[key] = current.to_table()
    _write(sites, path)
    return current


def reset_modes(host: str, path: Optional[os.PathLike[str] | str] = None) -> bool:
    """Remove ``host``'s entry. Returns whether it was present. A host whose key
    cannot be determined is matched verbatim, so a hand-edited entry can still be
    removed."""
    key = site_key(host) or _host(host)
    sites = load(path, strict=True)      # refuse to clobber an unreadable/untrusted file
    present = key in sites
    sites.pop(key, None)
    _write(sites, path)
    return present


# ---------------------------------------------------------------------------
# THE product-path accessor (the one place modes are read when replaying)
# ---------------------------------------------------------------------------

def effective_modes(host: str,
                    path: Optional[os.PathLike[str] | str] = None) -> Optional[SiteModes]:
    """The modes that apply to ``host`` on the PRODUCT path, or ``None``.

    This is the single accessor the product execution path consults. The
    guarded benchmark browser and the bench drivers never call it, so a measured
    bench is unaffected by any file this reads.

    Returns ``None`` when nothing non-default is stored, when the host has no
    certain site key, when a loaded key is not itself a valid site key (a
    hand-edited ``[sites."co.ug"]``), and when the host is a NEVER_CONTACT host
    or (with ``ANYUSE_REFUSE_GOV_HOSTS`` on) a government / public-body host — a
    stored mode is then ignored and the fact is logged."""
    key = site_key(host)
    if not key:
        return None
    modes = stored_modes(host, path)
    if modes is None or modes.is_default():
        return None
    try:
        if validate_host(key) != key:
            logger.warning("site modes key %r is not a site key: ignored", key)
            return None
    except SiteModeError as exc:
        logger.warning("site modes for %s are IGNORED: %s", key, exc)
        return None
    block = host_block_reason(host)
    if block:
        logger.warning("site modes for %s are IGNORED: %s", _host(host), block)
        return None
    return modes


def browser_identity_for(host: str,
                         path: Optional[os.PathLike[str] | str] = None) -> bool:
    """Whether ``host``'s site is set ``identity = browser`` — read through
    :func:`effective_modes`. The product recorders pass this as their
    User-Agent capture predicate; a bench never does, so it captures none."""
    modes = effective_modes(host, path)
    return bool(modes and modes.identity == "browser")
