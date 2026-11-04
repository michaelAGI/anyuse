"""AnyUse side of the map registry: export (sanitise), push, pull, prove.

Nothing here sends anything unless a registry URL is configured explicitly
(``--registry`` or ``ANYUSE_REGISTRY_URL``). There is no default registry.

* **export** builds an entry from an ALLOW-LIST. It copies no field from a
  discovery record; each entry field is built here on purpose, and the result
  must pass the same contract validator the server runs. It refuses a target
  that is not recognised (curated public-origin list, curated vendor list), and
  it drops a capability whose template has a local proof input baked in.
* **pull** re-validates every entry (a client never trusts the server) and
  lands every capability as ``unproved``, whatever the contributor or the
  server's ``verified`` flag says.
* **prove** runs the unchanged local fresh-input proof
  (:func:`anyuse.execution.discover.prove`). Its input values come from the
  site's own pages through local discovery, or from the operator, and they
  never leave this machine. Only the summary can be reported.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.registry import schema as S

ENV_URL = "ANYUSE_REGISTRY_URL"
ENV_KEY_ID = "ANYUSE_REGISTRY_KEY_ID"
ENV_PUBLIC_ORIGINS = "ANYUSE_REGISTRY_PUBLIC_ORIGINS"
_HERE = Path(__file__).resolve().parent
DEFAULT_PUBLIC_ORIGINS = _HERE / "public_origins.json"
DESKTOP_TARGETS = _HERE / "desktop_targets.json"
PRIVATE_KEY_PARTS = ("registry", "ed25519.key")

KIND_OF = {"search": "search", "detail": "detail", "next": "page", "filter": "filter"}


class RegistryConfigError(RuntimeError):
    pass


def registry_private_key_path() -> Path:
    from anyuse.paths import anyuse_secret_path
    return anyuse_secret_path(*PRIVATE_KEY_PARTS)


def generate_private_key_file(path: Optional[Path] = None) -> Tuple[Path, str]:
    """Create the contributor private key once and return only its public key."""
    key_path = Path(path) if path is not None else registry_private_key_path()
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_key, public_key = S.generate_keypair()
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as e:
        raise RegistryConfigError(f"registry private key already exists: {key_path}") from e
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fd = -1
            fh.write(private_key + "\n")
    finally:
        if fd >= 0:
            os.close(fd)
    return key_path, public_key


def load_private_key(path: Optional[Path] = None) -> str:
    key_path = Path(path) if path is not None else registry_private_key_path()
    try:
        info = key_path.lstat()
    except OSError as e:
        raise RegistryConfigError(
            f"writes need a registry private key; run 'anyuse map keygen' ({key_path})") from e
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RegistryConfigError(f"registry private key is not a regular file: {key_path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RegistryConfigError(f"registry private key must have mode 0600: {key_path}")
    try:
        text = key_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as e:
        raise RegistryConfigError(f"registry private key is unreadable: {key_path}") from e
    private_key = text[:-1] if text.endswith("\n") else text
    if "\n" in private_key or not S._full("private_key", private_key):
        raise RegistryConfigError(f"registry private key has the wrong format: {key_path}")
    return private_key


def _version() -> str:
    try:
        from anyuse import __version__
        return str(__version__)
    except Exception:
        return "0.0.0"


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# ── curated identity lists ─────────────────────────────────────────────────
def public_origins() -> frozenset:
    """The operator's list. Unreadable -> empty -> nothing is exported."""
    path = os.environ.get(ENV_PUBLIC_ORIGINS, "").strip() or str(DEFAULT_PUBLIC_ORIGINS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return frozenset(o for o in data.get("origins", []) if S._full("origin", o))
    except (OSError, ValueError, AttributeError):
        return frozenset()


def desktop_app_ids() -> Dict[str, str]:
    try:
        data = json.loads(DESKTOP_TARGETS.read_text(encoding="utf-8"))
        return {k: v for k, v in data.get("app_ids", {}).items()
                if S._full("app", k) and S._full("app_id", v)}
    except (OSError, ValueError, AttributeError):
        return {}


# ── export: web ────────────────────────────────────────────────────────────
def _param_name(raw: str, fallback: str) -> str:
    name = re.sub(r"[^a-z0-9_]", "_", str(raw or "").lower()).strip("_")[:32]
    if not name or not name[0].isalpha():
        name = fallback
    return name


def _candidates(record: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """The candidates of one discovery kind in either record layout: a bench
    host record (``caps[kind]["best"]``) or ``HostDiscovery.to_dict()``."""
    caps = record.get("caps") or {}
    got = caps.get(kind)
    if isinstance(got, dict) and "best" in got:
        return [got["best"]] if got.get("best") else []
    if isinstance(got, list):
        proved = [c for c in got if c.get("status") == "proved"]
        return proved[:1] or got[:1]
    disc = (record.get("discovery") or {}).get("caps", {}).get(kind)
    if isinstance(disc, list):
        proved = [c for c in disc if c.get("status") == "proved"]
        return proved[:1] or disc[:1]
    return []


def _known_values(record: Dict[str, Any]) -> set:
    """Every proof input value this record mentions, anywhere. Used only to
    REFUSE templates that embed one; never exported."""
    seen: set = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("values",) and isinstance(v, list):
                    seen.update(str(x).lower() for x in v)
                elif k == "alt_values" and isinstance(v, list):
                    for vs in v:
                        if isinstance(vs, list):
                            seen.update(str(x).lower() for x in vs)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(record)
    return seen


def _is_sort(c: Dict[str, Any]) -> bool:
    try:
        from anyuse.execution.discover import Capability, is_sort
        return bool(is_sort(Capability(cap="filter", template=c.get("template", ""),
                                       param=c.get("param", ""),
                                       values=list(c.get("values") or []))))
    except Exception:
        return False


def _held_out_inputs(c: Dict[str, Any]) -> int:
    """How many validation inputs a discovery proof held out (proof_split).

    Only a proof that names its ``validation_values`` separately from its fit
    and selection values (discovery v2.1 and later) held anything out. An older
    record counted every value after the first as "fresh", although the
    ground truth had been derived across all of them; it is exported as
    ``discovered``, not ``proved``.
    """
    held = (c.get("proof") or {}).get("validation_values")
    return len(held) if isinstance(held, list) else 0


def _proof_summary(c: Dict[str, Any], date: str, version: str) -> Dict[str, Any]:
    n = _held_out_inputs(c)
    if c.get("status") == "proved" and n >= 1:
        kind = c.get("proof_kind") or "learn"
        return {"status": "proved", "date": date, "fresh_inputs": min(n, 50),
                "proof_kind": kind if kind in ("learn", "sort") else "learn",
                "anyuse_version": version}
    return {"status": "discovered", "date": date, "fresh_inputs": 0, "proof_kind": "none",
            "anyuse_version": version}


def export_web(record: Dict[str, Any], origin: Optional[str] = None, date: Optional[str] = None,
               version: Optional[str] = None, origins: Optional[frozenset] = None
               ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """``(entry | None, report)``. The report is LOCAL diagnostics only."""
    origin = (origin or record.get("origin") or (record.get("discovery") or {}).get("origin")
              or (f"https://{record['host']}" if record.get("host") else "")).rstrip("/")
    report: Dict[str, Any] = {"origin": origin, "exported": [], "dropped": []}
    allowed = public_origins() if origins is None else origins
    if origin not in allowed:
        report["refused"] = "origin is not on the curated public-origin list"
        return None, report
    date = date or _today()
    version = version or _version()
    if not S._full("date", date):
        report["refused"] = "date must be YYYY-MM-DD"
        return None, report
    known = _known_values(record)
    caps: List[Dict[str, Any]] = []
    for dkind in ("search", "detail", "next", "filter"):
        for c in _candidates(record, dkind):
            kind = KIND_OF[dkind]
            if dkind == "filter" and _is_sort(c):
                kind = "sort"
            why = _web_cap(c, kind, origin, known, date, version, caps)
            (report["dropped"] if why else report["exported"]).append(
                {"kind": kind, "reason": why} if why else {"kind": kind})
    if not caps:
        report["refused"] = "no capability survived sanitising"
        return None, report
    entry = S.with_hash({"schema": S.contract()["schema"],
                         "target": {"kind": "web", "origin": origin},
                         "capabilities": caps})
    errs = S.entry_errors(entry)
    if errs:  # the builder and the validator disagree: send nothing
        report["refused"] = "; ".join(errs[:5])
        return None, report
    return entry, report


def _web_cap(c: Dict[str, Any], kind: str, origin: str, known: set, date: str, version: str,
             caps: List[Dict[str, Any]]) -> str:
    status = c.get("status")
    if status not in ("proved", "discovered"):
        return f"status {status or 'missing'} does not travel"
    tmpl = str(c.get("template") or "")
    if tmpl.count("{v}") != 1:
        return "template has no single value slot"
    where = "path" if c.get("where") == "path" else "query"
    name = _param_name(c.get("param") if where == "query" else "item",
                       "item" if where == "path" else "q")
    param: Dict[str, Any] = {"name": name, "type": "integer" if kind == "page" else "string",
                             "where": where}
    if where == "query":
        param["key"] = str(c.get("param") or "")
    template = tmpl.replace("{v}", "{" + name + "}")
    segs, pairs, terrs = S.parse_template(template, origin)
    if terrs:
        return "template: " + terrs[0]
    for _k, pk, text in pairs:
        if pk == "lit" and text and text.lower() in known:
            return "template embeds a proof input value"
    for sk, text in segs:
        if sk == "lit" and text.lower() in known:
            return "template embeds a proof input value"
    cname = kind
    n = 2
    while any(x["name"] == cname for x in caps):
        cname, n = f"{kind}_{n}", n + 1
    cap = {"name": cname, "kind": kind, "params": [param],
           "request": {"method": "GET", "template": template},
           "proof": _proof_summary(c, date, version)}
    errs = S.entry_errors(S.with_hash({"schema": S.contract()["schema"],
                                       "target": {"kind": "web", "origin": origin},
                                       "capabilities": [cap]}))
    if errs:
        return "contract: " + errs[0].split(": ", 1)[-1]
    caps.append(cap)
    return ""


# ── export: desktop ────────────────────────────────────────────────────────
def export_desktop(observed_app: str, versions: Dict[str, str], capabilities: Sequence[Dict[str, Any]],
                   date: Optional[str] = None, version: Optional[str] = None
                   ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """The observed name only SELECTS a curated entry; nothing observed travels.

    ``capabilities``: ``{"name", "kind": "desktop_class", "class", "subtype"?,
    "params": [names], "status", "fresh_inputs", "proof_kind"}`` or
    ``{"name", "kind": "app_operator", "idnames": [...], "binds": [{"param",
    "op", "kwarg"}], ...}``. Recorded constants are not accepted at all.
    """
    from anyuse.guides.projection import recognised_vendor
    report: Dict[str, Any] = {"exported": [], "dropped": []}
    vendor = recognised_vendor(observed_app)
    if vendor is None:
        report["refused"] = "application is not on the curated vendor list"
        return None, report
    app = vendor["application"]
    app_id = desktop_app_ids().get(app)
    if not app_id:
        report["refused"] = "no curated application id for this application"
        return None, report
    date, version = date or _today(), version or _version()
    caps = []
    for c in capabilities:
        params = [{"name": p, "type": "string", "where": "input"} for p in c.get("params", [])]
        if c.get("kind") == "desktop_class":
            op = {"class": c.get("class")}
            if c.get("subtype"):
                op["subtype"] = c["subtype"]
        else:
            op = {"idnames": list(c.get("idnames") or []),
                  "binds": [{"param": b.get("param"), "op": b.get("op"), "kwarg": b.get("kwarg")}
                            for b in c.get("binds") or []]}
        proved = c.get("status") == "proved"
        cap = {"name": c.get("name"), "kind": c.get("kind"), "params": params, "operator": op,
               "proof": {"status": "proved" if proved else "discovered", "date": date,
                         "fresh_inputs": int(c.get("fresh_inputs") or 0) if proved else 0,
                         "proof_kind": "desktop" if proved else "none",
                         "anyuse_version": version}}
        errs = S.entry_errors(S.with_hash({"schema": S.contract()["schema"],
                                           "target": {"kind": "desktop", "app": app,
                                                      "app_id": app_id, "versions": versions},
                                           "capabilities": [cap]}))
        if errs:
            report["dropped"].append({"kind": str(c.get("kind")), "reason": errs[0]})
            continue
        caps.append(cap)
        report["exported"].append({"kind": cap["kind"]})
    if not caps:
        report["refused"] = "no capability survived sanitising"
        return None, report
    entry = S.with_hash({"schema": S.contract()["schema"],
                         "target": {"kind": "desktop", "app": app, "app_id": app_id,
                                    "versions": dict(versions)},
                         "capabilities": caps})
    errs = S.entry_errors(entry)
    if errs:
        report["refused"] = "; ".join(errs[:5])
        return None, report
    return entry, report


# ── HTTP client ────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def registry_url(explicit: Optional[str] = None) -> str:
    url = (explicit or os.environ.get(ENV_URL, "")).strip().rstrip("/")
    if not url:
        raise RegistryConfigError(
            "no registry configured: pass --registry URL or set ANYUSE_REGISTRY_URL "
            "(there is no default; nothing is sent without one)")
    p = urllib.parse.urlsplit(url)
    loop = p.hostname in ("127.0.0.1", "::1", "localhost")
    if p.scheme != "https" and not (p.scheme == "http" and loop):
        raise RegistryConfigError("registry URL must be https (http only on loopback)")
    if p.username or p.password or p.query or p.fragment:
        raise RegistryConfigError("registry URL must not carry credentials, query or fragment")
    return url


class RegistryClient:
    def __init__(self, url: str, key_id: Optional[str] = None,
                 private_key: Optional[str] = None, private_key_path: Optional[Path] = None,
                 timeout_s: float = 15.0, clock=time.time):
        self.url = registry_url(url)
        self.key_id = key_id if key_id is not None else os.environ.get(ENV_KEY_ID, "")
        self.private_key = private_key
        self.private_key_path = private_key_path
        self.timeout_s = timeout_s
        self.clock = clock
        self._opener = urllib.request.build_opener(_NoRedirect,
                                                   urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, body: Optional[bytes] = None,
              signed: bool = False) -> Tuple[int, Any]:
        headers = {"Accept": "application/json", "User-Agent": "anyuse-registry-client/0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if signed:
            if not self.key_id:
                raise RegistryConfigError("writes need ANYUSE_REGISTRY_KEY_ID")
            private_key = self.private_key if self.private_key is not None \
                else load_private_key(self.private_key_path)
            a = S.contract()["auth"]
            ts, nonce = str(int(self.clock())), secrets.token_hex(16)
            headers.update({a["header_alg"]: a["alg"], a["header_key"]: self.key_id,
                            a["header_timestamp"]: ts,
                            a["header_nonce"]: nonce,
                            a["header_signature"]: S.sign(private_key, method, path, ts, nonce,
                                                          body or b"")})
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout_s) as r:
                raw = r.read(4 * 1024 * 1024 + 1)
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(64 * 1024) if e.fp else b"", e.code
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("registry response too large")
        try:
            return status, json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, {"error": "unreadable response"}

    def push(self, entry: Dict[str, Any]) -> Tuple[int, Any]:
        S.validate_entry(entry)
        return self._call("POST", "/v0/maps", S.canonical_json(entry), signed=True)

    def get(self, mid: str) -> Tuple[int, Any]:
        if not S._full("map_id", mid):
            raise ValueError("not a map id")
        return self._call("GET", f"/v0/maps/{mid}")

    def by_target(self, target: str) -> Tuple[int, Any]:
        return self._call("GET", "/v0/maps?" + urllib.parse.urlencode({"target": target}))

    def search(self, q: str) -> Tuple[int, Any]:
        return self._call("GET", "/v0/search?" + urllib.parse.urlencode({"q": q}))

    def report_proof(self, mid: str, report: Dict[str, Any]) -> Tuple[int, Any]:
        errs = S.proof_report_errors(report)
        if errs:
            raise S.RegistryError("schema", "; ".join(errs[:5]))
        return self._call("POST", f"/v0/maps/{mid}/proofs", S.canonical_json(report), signed=True)


# ── local candidate store ──────────────────────────────────────────────────
class CandidateStore:
    """Pulled maps, one JSON file per map id, every capability ``unproved``
    until THIS machine proves it."""

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            from anyuse.paths import anyuse_data_path
            base_dir = anyuse_data_path("registry", "candidates")
        self.base = Path(base_dir)

    def _path(self, mid: str) -> Path:
        if not S._full("map_id", mid):
            raise ValueError("not a map id")
        return self.base / f"{mid}.json"

    def land(self, pulled: Dict[str, Any], source: str) -> Dict[str, Any]:
        """Validate a server response and store it as unproved candidates."""
        entry = pulled.get("entry") if isinstance(pulled, dict) else None
        S.validate_entry(entry)  # a client never trusts the server
        mid = S.map_id(entry["content_hash"])
        if isinstance(pulled, dict) and pulled.get("id") not in (None, mid):
            raise S.RegistryError("hash_mismatch", "server id does not match the content")
        status = S.contract()["trust"]["pulled_status"]
        meta = pulled.get("meta") if isinstance(pulled.get("meta"), dict) else {}
        rec = {"id": mid, "entry": entry, "source": source, "pulled_day": _today(),
               "registry_verified_capabilities": [
                   c for c in (meta.get("verified_capabilities") or []) if isinstance(c, str)][:32],
               "local": {c["name"]: {"status": status, "claimed": c["proof"]["status"],
                                     "reason": "pulled from a registry: a hint until proved here"}
                         for c in entry["capabilities"]}}
        self.base.mkdir(parents=True, exist_ok=True)
        old = self.load(mid)
        if old:  # a re-pull never downgrades a local proof
            for name, st in (old.get("local") or {}).items():
                if name in rec["local"] and st.get("status") != status:
                    rec["local"][name] = st
        tmp = self._path(mid).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path(mid))
        return rec

    def load(self, mid: str) -> Optional[Dict[str, Any]]:
        p = self._path(mid)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, rec: Dict[str, Any]) -> None:
        self._path(rec["id"]).write_text(json.dumps(rec, indent=2, sort_keys=True),
                                         encoding="utf-8")

    def list(self) -> List[Dict[str, Any]]:
        if not self.base.exists():
            return []
        out = []
        for p in sorted(self.base.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out


# ── local proof ────────────────────────────────────────────────────────────
def to_discover_capability(entry: Dict[str, Any], cap: Dict[str, Any], values: Sequence[str] = ()):
    """The registry shape back into a discovery candidate with a ``{v}`` slot."""
    from anyuse.execution.discover import Capability
    if entry["target"]["kind"] != "web" or "request" not in cap or len(cap["params"]) != 1:
        raise ValueError("only single-parameter web capabilities are provable in v0")
    p = cap["params"][0]
    tmpl = cap["request"]["template"].replace("{" + p["name"] + "}", "{v}")
    dcap = {"search": "search", "detail": "detail", "page": "next",
            "filter": "filter", "sort": "filter"}[cap["kind"]]
    return Capability(cap=dcap, template=tmpl, where=p["where"],
                      param=p.get("key") or "item", values=list(values), source="registry")


def values_from_local_discovery(origin: str, template_v: str, fetch, robots_txt: str = ""
                                ) -> Tuple[List[str], List[List[str]]]:
    """Proof inputs from the site's OWN pages, found by local discovery of the
    same template. Nothing is invented; no match means no values."""
    from anyuse.execution.discover import discover_host
    hd = discover_host(origin, fetch, robots_txt=robots_txt)
    for cands in hd.caps.values():
        for c in cands:
            if c.template == template_v and c.values:
                return list(c.values), [list(v) for v in c.alt_values]
    return [], []


def prove_candidate(store: CandidateStore, mid: str, cap_name: str, fetch,
                    values: Optional[Sequence[str]] = None, executor=None,
                    proof_preflight=None, transport=None) -> Dict[str, Any]:
    """Run the UNCHANGED local proof on one pulled capability; record the verdict
    locally. Returns the local status record (no values in it)."""
    from anyuse.execution import discover as D
    rec = store.load(mid)
    if rec is None:
        raise KeyError("no such candidate")
    entry = rec["entry"]
    S.validate_entry(entry)  # the stored copy is re-checked, not trusted
    cap = next((c for c in entry["capabilities"] if c["name"] == cap_name), None)
    if cap is None:
        raise KeyError("no such capability")
    origin = entry["target"]["origin"]
    dcap = to_discover_capability(entry, cap)
    alt: List[List[str]] = []
    if values:
        dcap.values = list(values)
        source = "operator"
    else:
        dcap.values, alt = values_from_local_discovery(origin, dcap.template, fetch)
        source = "local discovery"
    dcap.alt_values = alt
    local = rec["local"].setdefault(cap_name, {})
    if len(dcap.values) < D.N_VALUES:
        local.update(status="unproved", reason=f"no {D.N_VALUES} site values ({source}); pass "
                     "--values from the site's own pages", day=_today())
        store.save(rec)
        return dict(local)
    D.prove(dcap, origin, fetch, executor=executor, proof_preflight=proof_preflight,
            transport=transport)
    # fresh_inputs = the validation inputs held out from fitting and selection
    # (proof_split), not every value after the first
    held = len((dcap.proof or {}).get("validation_values") or [])
    proved = dcap.status == "proved" and held >= 1
    local.update(status="proved" if proved else "unproved",
                 reason=str(dcap.reason)[:200], day=_today(), values_from=source,
                 proof_kind=dcap.proof_kind or "learn",
                 fresh_inputs=held if proved else 0,
                 local_outcome=dcap.status,
                 scope=(f"{held} held-out validation input(s); fit on "
                        f"{len(dcap.values) - held} (1 compile, "
                        f"{max(0, len(dcap.values) - held - 1)} selection)")
                 if proved else "")
    store.save(rec)
    return dict(local)


def proof_report(local: Dict[str, Any], cap_name: str, version: Optional[str] = None) -> Dict[str, Any]:
    """The summary that may be reported: no values, no reason text, day only."""
    proved = local.get("status") == "proved"
    status = "proved" if proved else ("refused" if local.get("local_outcome") == "refused"
                                      else "discovered")
    return {"capability": cap_name, "status": status, "date": local.get("day") or _today(),
            "fresh_inputs": int(local.get("fresh_inputs") or 0) if proved else 0,
            "proof_kind": (local.get("proof_kind") or "learn") if proved else "none",
            "anyuse_version": version or _version()}
