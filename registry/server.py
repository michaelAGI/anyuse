"""Reference registry server: HTTP/SQLite storage with Ed25519 verification.

The executable specification of the contract in ``registry/contract.json``; the
Cloudflare Worker in ``registry/worker/`` implements the same routes and
checks (not executed on this machine).

Local use only. It binds 127.0.0.1 by default, has no TLS and is meant for
tests and for trying the client against something real:

    python -m anyuse.registry.server --db /tmp/reg.sqlite --port 8787
    python -m anyuse.registry.server --db /tmp/reg.sqlite --add-contributor alice \
        --public-key BASE64URL_RAW_PUBLIC_KEY

``--add-contributor`` stores an Ed25519 public key. The server never receives
or stores contributor private key material.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from anyuse.registry import schema as S

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contributors (
  key_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, created INTEGER NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS maps (
  id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE, target_key TEXT NOT NULL,
  body TEXT NOT NULL, contributor TEXT NOT NULL, created INTEGER NOT NULL,
  installs INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS maps_target ON maps(target_key);
CREATE TABLE IF NOT EXISTS caps (
  map_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  PRIMARY KEY (map_id, name));
CREATE TABLE IF NOT EXISTS proofs (
  map_id TEXT NOT NULL, capability TEXT NOT NULL, contributor TEXT NOT NULL,
  status TEXT NOT NULL, date TEXT NOT NULL, fresh_inputs INTEGER NOT NULL,
  proof_kind TEXT NOT NULL, anyuse_version TEXT NOT NULL, created INTEGER NOT NULL,
  PRIMARY KEY (map_id, capability, contributor));
CREATE TABLE IF NOT EXISTS writes (contributor TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS writes_by ON writes(contributor, ts);
CREATE TABLE IF NOT EXISTS nonces (
  contributor TEXT NOT NULL, nonce TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY (contributor, nonce));
"""


def target_key(target: Dict[str, Any]) -> str:
    if target.get("kind") == "web":
        return target["origin"]
    return "app:" + target["app"]


class Registry:
    """The storage and rules, independent of HTTP (so both are testable)."""

    def __init__(self, db_path: str = ":memory:", clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA_SQL)
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(contributors)")]
        if columns != ["key_id", "public_key", "created", "disabled"]:
            self.db.close()
            raise RuntimeError("legacy contributor schema: recreate or migrate this v0 database")
        self.lock = threading.Lock()

    # -- contributors -----------------------------------------------------
    def add_contributor(self, key_id: str, public_key: str) -> None:
        if not S._full("key_id", key_id):
            raise S.RegistryError("schema", "key id not allowed")
        S.validate_public_key(public_key)
        with self.lock:
            self.db.execute(
                "INSERT INTO contributors(key_id, public_key, created) VALUES (?,?,?)",
                (key_id, public_key, int(self.clock())),
            )

    def authenticate(self, headers, method: str, path_with_query: str, body: bytes) -> str:
        a = S.contract()["auth"]
        key = headers.get(a["header_key"]) or ""
        ts = headers.get(a["header_timestamp"]) or ""
        nonce = headers.get(a["header_nonce"]) or ""
        sig = headers.get(a["header_signature"]) or ""
        alg = headers.get(a["header_alg"]) or ""
        if not (alg == a["alg"] and S._full("key_id", key) and
                S._full("nonce", nonce) and ts.isdigit()):
            raise S.RegistryError("unauthorized", "missing or malformed auth headers")
        now = int(self.clock())
        if abs(now - int(ts)) > a["max_skew_s"]:
            raise S.RegistryError("unauthorized", "timestamp outside the window")
        with self.lock:
            row = self.db.execute("SELECT public_key, disabled FROM contributors WHERE key_id=?",
                                  (key,)).fetchone()
        if row is None or row[1]:
            raise S.RegistryError("unauthorized", "unknown key")
        if not S.verify_signature(row[0], sig, method, path_with_query, ts, nonce, body):
            raise S.RegistryError("unauthorized", "bad signature")
        with self.lock:
            self.db.execute("DELETE FROM nonces WHERE ts < ?", (now - 2 * a["max_skew_s"],))
            try:
                self.db.execute("INSERT INTO nonces(contributor, nonce, ts) VALUES (?,?,?)",
                                (key, nonce, now))
            except sqlite3.IntegrityError:
                raise S.RegistryError("replay", "nonce already used")
        return key

    def _rate(self, key: str) -> None:
        now = int(self.clock())
        cap = S.contract()["rate_limits"]["writes_per_key_per_hour"]
        with self.lock:
            self.db.execute("DELETE FROM writes WHERE ts < ?", (now - 3600,))
            n = self.db.execute("SELECT COUNT(*) FROM writes WHERE contributor=? AND ts >= ?",
                                (key, now - 3600)).fetchone()[0]
            if n >= cap:
                raise S.RegistryError("rate_limited", "write limit for this key")
            self.db.execute("INSERT INTO writes(contributor, ts) VALUES (?,?)", (key, now))

    # -- maps ---------------------------------------------------------------
    def submit(self, key: str, entry: Any) -> Tuple[Dict[str, Any], bool]:
        self._rate(key)
        S.validate_entry(entry)
        h = entry["content_hash"]
        mid = S.map_id(h)
        with self.lock:
            if self.db.execute("SELECT 1 FROM maps WHERE id=?", (mid,)).fetchone():
                return {"id": mid, "content_hash": h}, False
            self.db.execute(
                "INSERT INTO maps(id, content_hash, target_key, body, contributor, created) "
                "VALUES (?,?,?,?,?,?)",
                (mid, h, target_key(entry["target"]), S.canonical_json(entry).decode("ascii"),
                 key, int(self.clock())))
            for cap in entry["capabilities"]:
                self.db.execute("INSERT INTO caps(map_id, name, kind) VALUES (?,?,?)",
                                (mid, cap["name"], cap["kind"]))
        return {"id": mid, "content_hash": h}, True

    def _meta(self, mid: str) -> Dict[str, Any]:
        t = S.contract()["trust"]
        rows = self.db.execute(
            "SELECT capability, contributor, status, date, fresh_inputs, proof_kind, "
            "anyuse_version FROM proofs WHERE map_id=? ORDER BY capability, contributor",
            (mid,)).fetchall()
        reports = [dict(zip(("capability", "contributor", "status", "date", "fresh_inputs",
                             "proof_kind", "anyuse_version"), r)) for r in rows]
        by: Dict[str, set] = {}
        for r in reports:
            if r["status"] == t["verified_counts_status"]:
                by.setdefault(r["capability"], set()).add(r["contributor"])
        verified = sorted(c for c, who in by.items()
                          if len(who) >= t["verified_min_distinct_contributors"])
        m = self.db.execute("SELECT contributor, created, installs FROM maps WHERE id=?",
                            (mid,)).fetchone()
        return {"contributor": m[0], "created_day": time.strftime("%Y-%m-%d", time.gmtime(m[1])),
                "installs": m[2], "proof_reports": reports, "verified_capabilities": verified,
                "verified": bool(verified),
                "trust_note": "metadata only: clients land every capability as unproved"}

    def get(self, mid: str, count_install: bool = True) -> Dict[str, Any]:
        if not S._full("map_id", mid):
            raise S.RegistryError("not_found", "")
        with self.lock:
            row = self.db.execute("SELECT body FROM maps WHERE id=?", (mid,)).fetchone()
            if row is None:
                raise S.RegistryError("not_found", "")
            if count_install:
                self.db.execute("UPDATE maps SET installs = installs + 1 WHERE id=?", (mid,))
            return {"id": mid, "entry": json.loads(row[0]), "meta": self._meta(mid)}

    def _summary(self, mid: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT target_key FROM maps WHERE id=?", (mid,)).fetchone()
        caps = self.db.execute("SELECT name, kind FROM caps WHERE map_id=? ORDER BY name",
                               (mid,)).fetchall()
        meta = self._meta(mid)
        return {"id": mid, "target": row[0], "capabilities": [{"name": n, "kind": k} for n, k in caps],
                "verified": meta["verified"], "verified_capabilities": meta["verified_capabilities"],
                "installs": meta["installs"], "reports": len(meta["proof_reports"])}

    def _ordered(self, ids: List[str]) -> List[Dict[str, Any]]:
        out = [self._summary(i) for i in ids]
        out.sort(key=lambda s: (not s["verified"], -s["reports"], -s["installs"], s["id"]))
        return out[:S.contract()["limits"]["max_list_results"]]

    def by_target(self, target: str) -> List[Dict[str, Any]]:
        if not (S._full("origin", target) or
                (target.startswith("app:") and S._full("app", target[4:]))):
            raise S.RegistryError("bad_query", "target must be https://host or app:<name>")
        with self.lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM maps WHERE target_key=? ORDER BY created DESC LIMIT 500",
                (target,))]
            return self._ordered(ids)

    def search(self, q: str) -> List[Dict[str, Any]]:
        lim = S.contract()["limits"]["max_search_query_chars"]
        if not q or len(q) > lim or not S._full("printable_ascii", q):
            raise S.RegistryError("bad_query", "q must be 1..64 printable ASCII characters")
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT DISTINCT m.id FROM maps m LEFT JOIN caps c ON c.map_id = m.id "
                "WHERE lower(m.target_key) LIKE ? ESCAPE '\\' OR lower(c.name) LIKE ? ESCAPE '\\' "
                "OR lower(c.kind) LIKE ? ESCAPE '\\' ORDER BY m.created DESC LIMIT 500",
                (like, like, like))]
            return self._ordered(ids)

    def report_proof(self, key: str, mid: str, report: Any) -> Dict[str, Any]:
        self._rate(key)
        with self.lock:
            row = self.db.execute("SELECT body FROM maps WHERE id=?", (mid,)).fetchone() \
                if S._full("map_id", mid) else None
        if row is None:
            raise S.RegistryError("not_found", "")
        names = [c["name"] for c in json.loads(row[0])["capabilities"]]
        errs = S.proof_report_errors(report, names)
        if errs:
            raise S.RegistryError("schema", "; ".join(errs[:8]))
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO proofs(map_id, capability, contributor, status, date, "
                "fresh_inputs, proof_kind, anyuse_version, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, report["capability"], key, report["status"], report["date"],
                 report["fresh_inputs"], report["proof_kind"], report["anyuse_version"],
                 int(self.clock())))
            return self._meta(mid)


# ── HTTP ───────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    registry: Registry = None  # set by make_server
    server_version = "anyuse-registry/0"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet: never log request content
        pass

    def _send(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, e: S.RegistryError) -> None:
        status = S.contract()["errors"].get(e.code, 400)
        self._send(status, {"error": e.code, "detail": e.detail[:600]})

    def _body(self) -> bytes:
        cap = S.contract()["limits"]["max_body_bytes"]
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise S.RegistryError("bad_json", "bad content length")
        if n < 0 or n > cap:
            raise S.RegistryError("too_large", f"body over {cap} bytes")
        return self.rfile.read(n)

    def _route(self, method: str):
        parts = urllib.parse.urlsplit(self.path)
        path, qs = parts.path, urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        reg = self.registry
        if path == "/v0/maps" and method == "GET":
            t = qs.get("target", [""])
            if len(t) != 1:
                raise S.RegistryError("bad_query", "exactly one target")
            return 200, {"maps": reg.by_target(t[0])}
        if path == "/v0/search" and method == "GET":
            q = qs.get("q", [""])
            if len(q) != 1:
                raise S.RegistryError("bad_query", "exactly one q")
            return 200, {"maps": reg.search(q[0])}
        segs = path.split("/")
        if path == "/v0/maps" and method == "POST":
            body = self._body()
            key = reg.authenticate(self.headers, method, self.path, body)
            entry = _json(body)
            out, created = reg.submit(key, entry)
            return (201 if created else 200), out
        if len(segs) == 4 and segs[:3] == ["", "v0", "maps"] and method == "GET":
            return 200, reg.get(segs[3])
        if len(segs) == 5 and segs[:3] == ["", "v0", "maps"] and segs[4] == "proofs" \
                and method == "POST":
            body = self._body()
            key = reg.authenticate(self.headers, method, self.path, body)
            return 201, reg.report_proof(key, segs[3], _json(body))
        known = path in ("/v0/maps", "/v0/search") or (
            len(segs) in (4, 5) and segs[:3] == ["", "v0", "maps"])
        raise S.RegistryError("method_not_allowed" if known else "not_found", "")

    def _handle(self, method: str) -> None:
        try:
            status, obj = self._route(method)
            self._send(status, obj)
        except S.RegistryError as e:
            self._error(e)
        except Exception:  # never leak a traceback or the request
            self._send(500, {"error": "internal"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise S.RegistryError("bad_json", "body is not JSON")


def make_server(registry: Registry, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"registry": registry})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AnyUse map registry — local reference server")
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--add-contributor", metavar="KEY_ID")
    ap.add_argument("--public-key", metavar="BASE64URL_RAW_KEY")
    a = ap.parse_args(argv)
    reg = Registry(a.db)
    if a.add_contributor:
        if not a.public_key:
            ap.error("--add-contributor requires --public-key")
        reg.add_contributor(a.add_contributor, a.public_key)
        print(f"added contributor {a.add_contributor}")
        return 0
    if a.public_key:
        ap.error("--public-key requires --add-contributor")
    if a.host not in ("127.0.0.1", "::1", "localhost"):
        print("refusing to bind a non-loopback address: this is a local reference server")
        return 2
    srv = make_server(reg, a.host, a.port)
    print(f"anyuse registry (local) on http://{a.host}:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
