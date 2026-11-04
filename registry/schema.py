"""The map-entry contract, in Python: validation, canonical hash, Ed25519.

Everything here reads ``registry/contract.json``, the SAME file the Cloudflare
Worker (``registry/worker/src/validate.js``) imports, so both sides refuse the
same entries for the same reasons.

Validation REFUSES, it never repairs: a map that does not match the contract
exactly is not sent (client) and not stored (server). Error details name the
field path and the rule, never the submitted value.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

CONTRACT_PATH = Path(__file__).resolve().with_name("contract.json")


class RegistryError(ValueError):
    """``code`` is one of contract["errors"]; ``detail`` never echoes input."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@lru_cache(maxsize=1)
def contract() -> Dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _full(pattern_name: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(contract()["patterns"][pattern_name], value) is not None


# ── canonical form and hash ────────────────────────────────────────────────
def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, ASCII only. Identical to the Worker's
    ``canonicalJSON`` for every entry that validates (all strings are
    printable ASCII and all numbers are small integers)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def content_hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "content_hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def map_id(hash_hex: str) -> str:
    return hash_hex[:32]


def with_hash(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in entry.items() if k != "content_hash"}
    out["content_hash"] = content_hash(out)
    return out


# ── the walk: keys and strings anywhere in the entry ───────────────────────
def _forbidden_keys() -> frozenset:
    c = contract()
    return frozenset(k.lower() for k in c["never_travels_snapshot"] + c["forbidden_keys_extra"])


def _walk(obj: Any, where: str, errors: List[str], depth: int = 0) -> None:
    if depth > 6:
        errors.append(f"{where}: nested too deep")
        return
    c = contract()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or not _full("printable_ascii", k):
                errors.append(f"{where}: non-ASCII key")
                continue
            if k.lower() in _forbidden_keys():
                errors.append(f"{where}.{k}: key never travels")
            _walk(v, f"{where}.{k}", errors, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{where}[{i}]", errors, depth + 1)
    elif isinstance(obj, str):
        if len(obj) > c["limits"]["max_string_chars"]:
            errors.append(f"{where}: string too long")
        if not _full("printable_ascii", obj):
            errors.append(f"{where}: not printable ASCII")
        low = obj.lower()
        for s in c["forbidden_substrings"]:
            if s in low:
                errors.append(f"{where}: contains a forbidden pattern")
                break
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, int):
        if not (0 <= obj <= 1_000_000):
            errors.append(f"{where}: integer out of range")
    else:
        errors.append(f"{where}: unsupported value type")


def _keys(obj: Any, where: str, required: set, optional: set, errors: List[str]) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{where}: must be an object")
        return False
    have = set(obj)
    for k in sorted(required - have):
        errors.append(f"{where}.{k}: missing")
    for k in sorted(have - required - optional):
        errors.append(f"{where}.{k}: unknown field")
    return not (required - have)


# ── templates ──────────────────────────────────────────────────────────────
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")


def parse_template(template: str, origin: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, str]], List[str]]:
    """Split a web template into path segments and query pairs, or say why not.

    Returns ``(segments, pairs, errors)``; ``segments`` is ``[(kind, text)]``
    with kind ``lit``/``ph``; ``pairs`` is ``[(key, kind, text)]``. The rules are
    the Worker's ``parseTemplate`` line for line: whole-segment and whole-value
    placeholders only, no empty inner segment, no dot segments, no fragment,
    fixed values from a small charset, no redirect or credential query key.
    """
    c = contract()
    errs: List[str] = []
    segs: List[Tuple[str, str]] = []
    pairs: List[Tuple[str, str, str]] = []
    if not isinstance(template, str) or len(template) > c["limits"]["max_template_chars"]:
        return segs, pairs, ["template missing or too long"]
    if not template.startswith(origin + "/"):
        return segs, pairs, ["template does not start with the entry origin"]
    rest = template[len(origin) + 1:]
    if "#" in rest:
        return segs, pairs, ["template has a fragment"]
    if "?" in rest:
        path, query = rest.split("?", 1)
        if query == "":
            errs.append("empty query")
    else:
        path, query = rest, None
    if path:
        parts = path.split("/")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if part == "":
                if not last:
                    errs.append("empty path segment")
                continue
            m = _PLACEHOLDER.fullmatch(part)
            if m:
                segs.append(("ph", m.group(1)))
            elif part in (".", "..") or not _full("path_literal", part):
                errs.append("path segment not allowed")
            else:
                segs.append(("lit", part))
    if query:
        seen = set()
        forbidden = {k.lower() for k in c["forbidden_query_keys"]}
        for raw in query.split("&"):
            if raw.count("=") != 1:
                errs.append("query pair malformed")
                continue
            k, v = raw.split("=")
            if not _full("query_key", k):
                errs.append("query key not allowed")
                continue
            if k.lower() in forbidden:
                errs.append("query key is a redirect or credential key")
            if k in seen:
                errs.append("duplicate query key")
            seen.add(k)
            m = _PLACEHOLDER.fullmatch(v)
            if m:
                pairs.append((k, "ph", m.group(1)))
            elif _full("fixed_value", v):
                pairs.append((k, "lit", v))
            else:
                errs.append("fixed query value not allowed")
    return segs, pairs, errs


# ── entry validation ───────────────────────────────────────────────────────
def _validate_proof(p: Any, where: str, errors: List[str]) -> None:
    c = contract()
    if not _keys(p, where, {"status", "date", "fresh_inputs", "proof_kind", "anyuse_version"},
                 set(), errors):
        return
    if p["status"] not in c["enums"]["proof_status"]:
        errors.append(f"{where}.status: not allowed")
    if not _full("date", p["date"]):
        errors.append(f"{where}.date: must be YYYY-MM-DD")
    fi = p["fresh_inputs"]
    if isinstance(fi, bool) or not isinstance(fi, int) or not 0 <= fi <= c["limits"]["max_fresh_inputs"]:
        errors.append(f"{where}.fresh_inputs: must be a small count")
    if p["proof_kind"] not in c["enums"]["proof_kind"]:
        errors.append(f"{where}.proof_kind: not allowed")
    if not _full("anyuse_version", p["anyuse_version"]):
        errors.append(f"{where}.anyuse_version: not a version")
    fi_ok = isinstance(fi, int) and not isinstance(fi, bool) and fi >= 1
    if p["status"] == "proved" and (p["proof_kind"] == "none" or not fi_ok):
        errors.append(f"{where}: a proved status needs a proof kind and >= 1 fresh input")


def _validate_params(params: Any, where: str, desktop: bool, errors: List[str]) -> Dict[str, dict]:
    c = contract()
    out: Dict[str, dict] = {}
    if not isinstance(params, list) or len(params) > c["limits"]["max_params"]:
        errors.append(f"{where}: must be a list of at most {c['limits']['max_params']}")
        return out
    for i, p in enumerate(params):
        w = f"{where}[{i}]"
        if not _keys(p, w, {"name", "type", "where"}, {"key"}, errors):
            continue
        if not _full("param_name", p["name"]):
            errors.append(f"{w}.name: not allowed")
        if p["type"] not in c["enums"]["param_type"]:
            errors.append(f"{w}.type: not allowed")
        allowed = c["enums"]["param_where_desktop" if desktop else "param_where_web"]
        if p["where"] not in allowed:
            errors.append(f"{w}.where: not allowed")
        if p["where"] == "query":
            if not _full("query_key", p.get("key")):
                errors.append(f"{w}.key: a query parameter needs its query key")
        elif "key" in p:
            errors.append(f"{w}.key: only a query parameter has a key")
        if p["name"] in out:
            errors.append(f"{w}.name: duplicate")
        out[p["name"]] = p
    return out


def _validate_web_cap(cap: dict, where: str, origin: str, errors: List[str]) -> None:
    c = contract()
    if cap["kind"] not in c["enums"]["web_kinds"]:
        errors.append(f"{where}.kind: not a web kind")
    if "operator" in cap or "request" not in cap:
        errors.append(f"{where}: a web capability has a request and no operator")
        return
    params = _validate_params(cap["params"], f"{where}.params", False, errors)
    req = cap["request"]
    if not _keys(req, f"{where}.request", {"method", "template"}, set(), errors):
        return
    if req["method"] not in c["enums"]["request_method"]:
        errors.append(f"{where}.request.method: not allowed")
    segs, pairs, terrs = parse_template(req["template"], origin)
    errors.extend(f"{where}.request.template: {e}" for e in terrs)
    if terrs:
        return
    used: Dict[str, int] = {}
    for kind, text in segs:
        if kind == "ph":
            used[text] = used.get(text, 0) + 1
            p = params.get(text)
            if p is not None and p["where"] != "path":
                errors.append(f"{where}.request.template: placeholder location differs from its param")
    for key, kind, text in pairs:
        if kind == "ph":
            used[text] = used.get(text, 0) + 1
            p = params.get(text)
            if p is not None and (p["where"] != "query" or p.get("key") != key):
                errors.append(f"{where}.request.template: placeholder key differs from its param")
    if set(used) != set(params) or any(n != 1 for n in used.values()):
        errors.append(f"{where}.request.template: every param exactly once, no undeclared placeholder")


def _validate_desktop_cap(cap: dict, where: str, errors: List[str]) -> None:
    c = contract()
    if cap["kind"] not in c["enums"]["desktop_kinds"]:
        errors.append(f"{where}.kind: not a desktop kind")
        return
    if "request" in cap or "operator" not in cap:
        errors.append(f"{where}: a desktop capability has an operator and no request")
        return
    params = _validate_params(cap["params"], f"{where}.params", True, errors)
    op = cap["operator"]
    w = f"{where}.operator"
    if cap["kind"] == "desktop_class":
        if not _keys(op, w, {"class"}, {"subtype"}, errors):
            return
        if op["class"] not in c["enums"]["desktop_class"]:
            errors.append(f"{w}.class: not allowed")
        if "subtype" in op and op["subtype"] not in c["enums"]["desktop_subtype"]:
            errors.append(f"{w}.subtype: not allowed")
        return
    if not _keys(op, w, {"idnames", "binds"}, set(), errors):
        return
    ids = op["idnames"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= c["limits"]["max_idnames"] \
            or not all(_full("idname", x) for x in ids):
        errors.append(f"{w}.idnames: 1..{c['limits']['max_idnames']} operator idnames")
        return
    binds = op["binds"]
    if not isinstance(binds, list) or len(binds) > c["limits"]["max_binds"]:
        errors.append(f"{w}.binds: must be a short list")
        return
    bound: Dict[str, int] = {}
    for i, b in enumerate(binds):
        bw = f"{w}.binds[{i}]"
        if not _keys(b, bw, {"param", "op", "kwarg"}, set(), errors):
            continue
        if b["param"] not in params:
            errors.append(f"{bw}.param: not a declared param")
        if isinstance(b["op"], bool) or not isinstance(b["op"], int) or not 0 <= b["op"] < len(ids):
            errors.append(f"{bw}.op: not an operator index")
        if not _full("kwarg", b["kwarg"]):
            errors.append(f"{bw}.kwarg: not allowed")
        bound[b["param"]] = bound.get(b["param"], 0) + 1
    if set(bound) != set(params) or any(n != 1 for n in bound.values()):
        errors.append(f"{w}.binds: every param bound exactly once")


def entry_errors(entry: Any, check_hash: bool = True) -> List[str]:
    """Every contract violation in ``entry`` (empty list: valid)."""
    try:
        return _entry_errors(entry, check_hash)
    except (TypeError, AttributeError, KeyError):  # e.g. a list where a name belongs
        return ["entry: malformed value types"]


def _entry_errors(entry: Any, check_hash: bool) -> List[str]:
    c = contract()
    errors: List[str] = []
    if not _keys(entry, "entry", {"schema", "target", "capabilities", "content_hash"}, set(),
                 errors):
        return errors
    _walk(entry, "entry", errors)
    if entry["schema"] != c["schema"]:
        errors.append("entry.schema: unknown schema version")
    t = entry["target"]
    origin = ""
    desktop = False
    if isinstance(t, dict) and t.get("kind") == "web":
        if _keys(t, "entry.target", {"kind", "origin"}, set(), errors):
            origin = t["origin"]
            if not _full("origin", origin):
                errors.append("entry.target.origin: must be https://<public dns name>")
    elif isinstance(t, dict) and t.get("kind") == "desktop":
        desktop = True
        if _keys(t, "entry.target", {"kind", "app", "app_id", "versions"}, set(), errors):
            if not _full("app", t["app"]):
                errors.append("entry.target.app: not allowed")
            if not _full("app_id", t["app_id"]):
                errors.append("entry.target.app_id: not a reverse-DNS id")
            v = t["versions"]
            if _keys(v, "entry.target.versions", {"min"}, {"max"}, errors):
                if not _full("version", v["min"]) or ("max" in v and not _full("version", v["max"])):
                    errors.append("entry.target.versions: digits and dots only")
    else:
        errors.append("entry.target.kind: must be web or desktop")
    caps = entry["capabilities"]
    if not isinstance(caps, list) or not 1 <= len(caps) <= c["limits"]["max_capabilities"]:
        errors.append(f"entry.capabilities: 1..{c['limits']['max_capabilities']} capabilities")
        caps = []
    names = set()
    for i, cap in enumerate(caps):
        w = f"entry.capabilities[{i}]"
        if not _keys(cap, w, {"name", "kind", "params", "proof"}, {"request", "operator"}, errors):
            continue
        if not _full("capability_name", cap["name"]):
            errors.append(f"{w}.name: not allowed")
        if cap["name"] in names:
            errors.append(f"{w}.name: duplicate")
        names.add(cap["name"])
        if cap["kind"] not in c["enums"]["capability_kind"]:
            errors.append(f"{w}.kind: not allowed")
            continue
        if desktop:
            _validate_desktop_cap(cap, w, errors)
        elif origin:
            _validate_web_cap(cap, w, origin, errors)
        _validate_proof(cap["proof"], f"{w}.proof", errors)
    if not _full("content_hash", entry["content_hash"]):
        errors.append("entry.content_hash: not a sha256 hex digest")
    elif check_hash and not errors and content_hash(entry) != entry["content_hash"]:
        errors.append("entry.content_hash: does not match the canonical content")
    return errors


def validate_entry(entry: Any) -> Dict[str, Any]:
    """Raise :class:`RegistryError` unless ``entry`` satisfies the contract."""
    errs = entry_errors(entry)
    if errs:
        code = "hash_mismatch" if errs == ["entry.content_hash: does not match the canonical content"] \
            else "schema"
        raise RegistryError(code, "; ".join(errs[:8]))
    return entry


def proof_report_errors(report: Any, capability_names: Optional[List[str]] = None) -> List[str]:
    try:
        return _proof_report_errors(report, capability_names)
    except (TypeError, AttributeError, KeyError):
        return ["proof: malformed value types"]


def _proof_report_errors(report: Any, capability_names: Optional[List[str]]) -> List[str]:
    errors: List[str] = []
    if not _keys(report, "proof", {"capability", "status", "date", "fresh_inputs", "proof_kind",
                                   "anyuse_version"}, set(), errors):
        return errors
    _walk(report, "proof", errors)
    if capability_names is not None and report["capability"] not in capability_names:
        errors.append("proof.capability: not a capability of this map")
    inner = {k: v for k, v in report.items() if k != "capability"}
    _validate_proof(inner, "proof", errors)
    if report["status"] == "unproved":
        errors.append("proof.status: a report is proved, discovered or refused")
    return errors


# ── Ed25519 signatures (v0) ────────────────────────────────────────────────
def string_to_sign(method: str, path_with_query: str, timestamp: str, nonce: str,
                   body: bytes) -> bytes:
    return "\n".join([method.upper(), path_with_query, str(timestamp), nonce,
                      hashlib.sha256(body or b"").hexdigest()]).encode("ascii")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_b64url(value: Any, pattern: str, size: int) -> Optional[bytes]:
    """Decode only the one canonical, unpadded base64url representation."""
    if not _full(pattern, value):
        return None
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == size and _b64url(raw) == value else None


def generate_keypair() -> Tuple[str, str]:
    """Return ``(private, public)`` as raw-key, unpadded base64url strings."""
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return _b64url(private_raw), _b64url(public_raw)


def public_key_for(private_key: str) -> str:
    raw = _decode_b64url(private_key, "private_key", 32)
    if raw is None:
        raise RegistryError("schema", "private key must be 32 raw bytes in base64url")
    public_raw = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return _b64url(public_raw)


def validate_public_key(public_key: str) -> str:
    raw = _decode_b64url(public_key, "public_key", 32)
    if raw is None:
        raise RegistryError("schema", "public key must be 32 raw bytes in base64url")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as e:
        raise RegistryError("schema", "public key is not Ed25519") from e
    return public_key


def sign(private_key: str, method: str, path_with_query: str, timestamp: str, nonce: str,
         body: bytes) -> str:
    raw = _decode_b64url(private_key, "private_key", 32)
    if raw is None:
        raise RegistryError("schema", "private key must be 32 raw bytes in base64url")
    signature = Ed25519PrivateKey.from_private_bytes(raw).sign(
        string_to_sign(method, path_with_query, timestamp, nonce, body))
    return _b64url(signature)


def verify_signature(public_key: str, signature: str, method: str, path_with_query: str,
                     timestamp: str, nonce: str, body: bytes) -> bool:
    public_raw = _decode_b64url(public_key, "public_key", 32)
    signature_raw = _decode_b64url(signature, "signature", 64)
    if public_raw is None or signature_raw is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(
            signature_raw,
            string_to_sign(method, path_with_query, timestamp, nonce, body),
        )
        return True
    except (InvalidSignature, ValueError):
        return False
