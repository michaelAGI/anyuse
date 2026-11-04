"""Map registry v0, offline.

The real registry HTTP handler runs through an in-memory urllib transport.
Client serialization, signatures, server routing and redirect handling all run
without binding a listener. Local proofs use test_discover's in-process site.

What must hold:

* the contract refuses everything that must not travel (shared vectors, the
  NEVER_TRAVELS pin, other hosts, redirect/credential keys, values, headers);
* export is an allow-list: tenant origins and unrecognised apps do not travel,
  templates with a proof input baked in are dropped, no value is ever exported;
* a pulled map lands UNPROVED, even when the registry says "verified", and
  becomes proved only after the unchanged local proof;
* "verified" needs >= 2 distinct contributors; one key reporting twice is one;
* Ed25519 auth, replay, skew, rate limit and size caps are enforced by the server;
* nothing is sent without an explicitly configured registry URL.
"""
from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.request
import urllib.response
from email.message import Message
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from anyuse.registry import client as C
from anyuse.registry import schema as S
from anyuse.registry import server as SV

HERE = Path(__file__).resolve().parent.parent
VECTOR_DOC = json.loads((HERE / "registry" / "contract_vectors.json").read_text())
VECTORS = VECTOR_DOC["vectors"]
SIGNATURE_VECTORS = VECTOR_DOC["signature_vectors"]
TEST_PRIVATE_KEY = SIGNATURE_VECTORS["test_only_private_key_base64url"]
TEST_PUBLIC_KEY = SIGNATURE_VECTORS["public_key_base64url"]
O = "https://pkgs.example.org"


def _vec(name):
    return copy.deepcopy(next(v["entry"] for v in VECTORS if v["name"] == name))


class Clock:
    def __init__(self, t=1_790_000_000):
        self.t = t

    def __call__(self):
        return self.t


class InMemoryHTTP(urllib.request.HTTPHandler):
    """Execute the actual server handler against serialized client requests."""

    def __init__(self, handler_class, origin="http://127.0.0.1:8001"):
        super().__init__()
        self.handler_class, self.origin, self.requests = handler_class, origin, []

    def http_open(self, request):
        assert request.full_url.startswith(self.origin + "/"), request.full_url
        self.requests.append(request.full_url)
        handler = self.handler_class.__new__(self.handler_class)
        handler.path, handler.command = request.selector, request.get_method()
        handler.headers = Message()
        for key, value in request.header_items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(request.data or b"")
        handler.wfile = io.BytesIO()
        response_headers, response_status = Message(), []
        handler.send_response = lambda code: response_status.append(code)
        handler.send_header = lambda key, value: response_headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        getattr(handler, "do_" + handler.command)()
        response = urllib.response.addinfourl(io.BytesIO(handler.wfile.getvalue()), response_headers,
                                              request.full_url, response_status[0])
        response.msg = "inert registry response"
        return response


class Loopback:
    """Reference server semantics without a listener or background thread."""

    def __init__(self, clock=None):
        self.reg = SV.Registry(":memory:", clock=clock or Clock())

    def __enter__(self):
        self.url = "http://127.0.0.1:8001"
        self.transport = InMemoryHTTP(type("BoundHandler", (SV.Handler,), {"registry": self.reg}), self.url)
        return self

    def opener(self):
        return urllib.request.build_opener(C._NoRedirect, urllib.request.ProxyHandler({}), self.transport)

    def client(self, key_id="", private_key=None):
        client = C.RegistryClient(self.url, key_id=key_id, private_key=private_key,
                                  clock=self.reg.clock)
        client._opener = self.opener()
        return client

    def __exit__(self, *exc):
        self.reg.db.close()


# ── contract ────────────────────────────────────────────────────────────────
class Contract(unittest.TestCase):

    def test_shared_vectors(self):
        self.assertGreaterEqual(len(VECTORS), 30)
        for v in VECTORS:
            errs = S.entry_errors(v["entry"])
            code = "ok" if not errs else (
                "hash_mismatch" if errs == ["entry.content_hash: does not match the canonical content"]
                else "schema")
            self.assertEqual(code, v["expect"], (v["name"], errs))

    def test_never_travels_is_pinned_to_the_projection(self):
        from anyuse.guides.projection import NEVER_TRAVELS
        snap = set(S.contract()["never_travels_snapshot"])
        self.assertTrue(set(NEVER_TRAVELS) <= snap, set(NEVER_TRAVELS) - snap)
        for key in NEVER_TRAVELS:
            e = _vec("ok_web")
            e["capabilities"][0]["proof"][key] = "x"
            self.assertTrue(any("never travels" in x for x in S.entry_errors(e)), key)

    def test_hash_is_canonical_and_id_is_its_prefix(self):
        e = _vec("ok_web")
        shuffled = json.loads(json.dumps(e, sort_keys=False))
        self.assertEqual(S.content_hash(shuffled), e["content_hash"])
        self.assertEqual(S.map_id(e["content_hash"]), e["content_hash"][:32])
        self.assertEqual(S.canonical_json({"b": 1, "a": [1, "x"]}), b'{"a":[1,"x"],"b":1}')

    def test_signature_binds_method_path_time_nonce_body(self):
        private, public = S.generate_keypair()
        sig = S.sign(private, "POST", "/v0/maps", "1", "a" * 32, b"{}")
        self.assertTrue(S.verify_signature(public, sig, "POST", "/v0/maps", "1", "a" * 32, b"{}"))
        for args in (("GET", "/v0/maps", "1", "a" * 32, b"{}"),
                     ("POST", "/v0/maps/x", "1", "a" * 32, b"{}"),
                     ("POST", "/v0/maps", "2", "a" * 32, b"{}"),
                     ("POST", "/v0/maps", "1", "b" * 32, b"{}"),
                     ("POST", "/v0/maps", "1", "a" * 32, b"{ }")):
            self.assertFalse(S.verify_signature(public, sig, *args), args)
        other_public = S.generate_keypair()[1]
        self.assertFalse(S.verify_signature(other_public, sig, "POST", "/v0/maps", "1",
                                            "a" * 32, b"{}"))

    def test_python_generated_signature_vectors(self):
        private = SIGNATURE_VECTORS["test_only_private_key_base64url"]
        public = SIGNATURE_VECTORS["public_key_base64url"]
        self.assertEqual(S.public_key_for(private), public)
        for vector in SIGNATURE_VECTORS["vectors"]:
            args = (vector["method"], vector["path_with_query"], vector["timestamp"],
                    vector["nonce"], vector["body_utf8"].encode("utf-8"))
            self.assertEqual(S.sign(private, *args), vector["signature_base64url"], vector["name"])
            self.assertTrue(S.verify_signature(public, vector["signature_base64url"], *args),
                            vector["name"])

    def test_worker_reads_the_same_contract(self):
        w = HERE / "registry" / "worker"
        src = (w / "src" / "index.js").read_text() + (w / "src" / "validate.js").read_text()
        self.assertIn("../../contract.json", src)
        for route in ("/v0/maps", "/v0/search", "/proofs"):
            self.assertIn(route, src)
        self.assertIn("contract_vectors.json", (w / "vectors.mjs").read_text())
        schema_sql = (w / "schema.sql").read_text()
        for table in ("contributors", "maps", "caps", "proofs", "writes", "nonces"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema_sql)
        self.assertIn("public_key TEXT NOT NULL", schema_sql)
        self.assertNotIn("token TEXT", schema_sql)
        self.assertIn('crypto.subtle.importKey("raw", pub, { name: "Ed25519" }', src)
        self.assertIn('crypto.subtle.verify({ name: "Ed25519" }', src)


# ── export ──────────────────────────────────────────────────────────────────
def _record(origin=O, **over):
    rec = {"host": origin.split("//")[1], "origin": origin, "caps": {
        "search": [{"cap": "search", "template": origin + "/search?q={v}&s=all", "where": "query",
                    "param": "q", "values": ["alpha", "bravo", "charlie"],
                    "alt_values": [["delta", "echo", "golf"]], "status": "proved",
                    "proof_kind": "learn", "evidence": "form on page",
                    "proof": {"preview": ["alpha tool"], "first_url": origin + "/search?q=alpha",
                              # held out by proof_split: 1 compile + 1 selection + 2
                              "validation_values": ["charlie", "delta"]}}],
        "detail": [{"cap": "detail", "template": origin + "/package/{v}", "where": "path",
                    "param": "item", "values": ["alpha", "bravo", "charlie"],
                    "status": "discovered"}],
        "next": [{"cap": "next", "template": origin + "/list?sort=alpha&page={v}",
                  "where": "query", "param": "page", "values": ["2", "3", "4"],
                  "status": "proved"}],
        "filter": [{"cap": "filter", "template": "", "status": "refused"}]}}
    rec.update(over)
    return rec


class Export(unittest.TestCase):

    def test_export_is_an_allow_list_and_carries_no_values(self):
        entry, rep = C.export_web(_record(), date="2026-11-03", version="0.4.0",
                                  origins=frozenset({O}))
        self.assertIsNotNone(entry, rep)
        self.assertEqual(S.entry_errors(entry), [])
        text = json.dumps(entry)
        for leak in ("alpha", "bravo", "delta", "preview", "evidence", "first_url", "values"):
            self.assertNotIn(leak, text)
        names = {c["name"]: c for c in entry["capabilities"]}
        self.assertEqual(set(names), {"search", "detail"})
        self.assertEqual(names["search"]["request"]["template"], O + "/search?q={q}&s=all")
        self.assertEqual(names["search"]["proof"]["fresh_inputs"], 2)
        self.assertEqual(names["detail"]["proof"]["status"], "discovered")
        dropped = {d["kind"]: d["reason"] for d in rep["dropped"]}
        self.assertEqual(dropped["page"], "template embeds a proof input value")
        self.assertIn("does not travel", dropped["filter"])

    def test_tenant_origin_is_not_exported(self):
        tenant = "https://mueller-gmbh.shop-vendor.example"
        entry, rep = C.export_web(_record(tenant), origins=frozenset({O}))
        self.assertIsNone(entry)
        self.assertIn("curated public-origin list", rep["refused"])

    def test_unreadable_origin_list_exports_nothing(self):
        os.environ[C.ENV_PUBLIC_ORIGINS] = "/nonexistent/origins.json"
        try:
            self.assertEqual(C.public_origins(), frozenset())
            self.assertIsNone(C.export_web(_record())[0])
        finally:
            del os.environ[C.ENV_PUBLIC_ORIGINS]

    def test_off_host_template_in_a_record_is_dropped(self):
        rec = _record()
        rec["caps"]["detail"][0]["template"] = "https://evil.example.com/package/{v}"
        entry, rep = C.export_web(rec, date="2026-11-03", origins=frozenset({O}))
        self.assertNotIn("detail", {c["name"] for c in entry["capabilities"]})
        self.assertIn("template", {d["kind"]: d["reason"] for d in rep["dropped"]}["detail"])

    def test_desktop_identity_is_recognised_not_derived(self):
        caps = [{"name": "sum", "kind": "desktop_class", "class": "arithmetic", "params": ["a"],
                 "status": "proved", "fresh_inputs": 2}]
        entry, rep = C.export_desktop("  microsoft excel ", {"min": "16.0", "max": "16.99"}, caps,
                                      date="2026-11-03", version="0.4.0")
        self.assertIsNotNone(entry, rep)
        self.assertEqual(entry["target"]["app"], "Microsoft Excel")
        self.assertEqual(entry["target"]["app_id"], "com.microsoft.Excel")
        self.assertNotIn("microsoft excel ", json.dumps(entry))
        for observed in ("Electron (Angebot Mueller GmbH)", "Microsoft Excel - Kunde Mueller",
                         "Blender"):
            e, r = C.export_desktop(observed, {"min": "1.0"}, caps)
            self.assertIsNone(e, observed)
            self.assertIn("curated vendor list", r["refused"])

    def test_desktop_recorded_constants_are_not_accepted(self):
        caps = [{"name": "sum", "kind": "desktop_class", "class": "arithmetic", "params": [],
                 "status": "proved", "fresh_inputs": 2, "constants": {"a": "4711"}}]
        entry, _ = C.export_desktop("Microsoft Excel", {"min": "16.0"}, caps)
        self.assertNotIn("4711", json.dumps(entry))

    def test_seed_file_is_valid_and_value_free(self):
        seed = json.loads((HERE / "registry" / "seed" / "seed.json").read_text())
        self.assertGreaterEqual(len(seed["entries"]), 10)
        for e in seed["entries"]:
            self.assertEqual(S.entry_errors(e), [], e["target"])
            self.assertIn(e["target"]["origin"], C.public_origins())
        text = json.dumps(seed["entries"])
        for leak in ('"values"', "preview", "evidence", "jammy", "first_url"):
            self.assertNotIn(leak, text)


# ── registry rules (no HTTP) ────────────────────────────────────────────────
class Rules(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.reg = SV.Registry(":memory:", clock=self.clock)
        self.alice_private, alice_public = S.generate_keypair()
        self.bob_private, bob_public = S.generate_keypair()
        self.reg.add_contributor("alice", alice_public)
        self.reg.add_contributor("bob", bob_public)
        self.entry = _vec("ok_web")
        self.mid = self.reg.submit("alice", self.entry)[0]["id"]

    def _report(self, who, status="proved", cap="search"):
        return self.reg.report_proof(who, self.mid, {
            "capability": cap, "status": status, "date": "2026-11-03", "fresh_inputs": 2,
            "proof_kind": "learn" if status == "proved" else "none", "anyuse_version": "0.4.0"}
            if status == "proved" else {
            "capability": cap, "status": status, "date": "2026-11-03", "fresh_inputs": 0,
            "proof_kind": "none", "anyuse_version": "0.4.0"})

    def test_verified_needs_two_distinct_contributors(self):
        self.assertFalse(self._report("alice")["verified"])
        self.assertFalse(self._report("alice")["verified"])  # same key again: still one
        m = self._report("bob", "discovered")
        self.assertFalse(m["verified"])
        m = self._report("bob")
        self.assertEqual(m["verified_capabilities"], ["search"])
        self.assertTrue(m["verified"])
        self.assertIn("metadata only", m["trust_note"])

    def test_submit_is_idempotent_by_content(self):
        out, created = self.reg.submit("bob", self.entry)
        self.assertFalse(created)
        self.assertEqual(out["id"], self.mid)

    def test_rate_limit_per_key(self):
        cap = S.contract()["rate_limits"]["writes_per_key_per_hour"]
        with self.assertRaises(S.RegistryError) as cm:
            for _ in range(cap + 1):
                self.reg.submit("bob", self.entry)
        self.assertEqual(cm.exception.code, "rate_limited")
        self.clock.t += 3601
        self.reg.submit("bob", self.entry)

    def test_invalid_entries_also_count_against_the_limit(self):
        bad = _vec("other_host")
        cap = S.contract()["rate_limits"]["writes_per_key_per_hour"]
        codes = set()
        for _ in range(cap + 1):
            try:
                self.reg.submit("bob", bad)
            except S.RegistryError as e:
                codes.add(e.code)
        self.assertEqual(codes, {"schema", "rate_limited"})

    def test_proof_report_rules(self):
        for bad in ({"capability": "nope", "status": "proved", "date": "2026-11-03",
                     "fresh_inputs": 2, "proof_kind": "learn", "anyuse_version": "0.4.0"},
                    {"capability": "search", "status": "unproved", "date": "2026-11-03",
                     "fresh_inputs": 0, "proof_kind": "none", "anyuse_version": "0.4.0"},
                    {"capability": "search", "status": "proved", "date": "2026-11-03",
                     "fresh_inputs": 2, "proof_kind": "learn", "anyuse_version": "0.4.0",
                     "values": ["alpha"]}):
            with self.assertRaises(S.RegistryError):
                self.reg.report_proof("bob", self.mid, bad)
        with self.assertRaises(S.RegistryError) as cm:
            self.reg.report_proof("bob", "0" * 32, {})
        self.assertEqual(cm.exception.code, "not_found")

    def test_search_matches_target_and_capability_only(self):
        self.assertEqual([m["id"] for m in self.reg.search("example.org")], [self.mid])
        self.assertEqual([m["id"] for m in self.reg.search("detail")], [self.mid])
        self.assertEqual(self.reg.search("0.4.0"), [])
        self.assertEqual(self.reg.search("%"), [])
        with self.assertRaises(S.RegistryError):
            self.reg.search("x" * 65)


# ── HTTP on loopback ────────────────────────────────────────────────────────
class Http(unittest.TestCase):

    def test_push_get_list_search_over_loopback(self):
        with Loopback() as lb:
            private, public = S.generate_keypair()
            lb.reg.add_contributor("alice", public)
            c = lb.client("alice", private)
            e = _vec("ok_web")
            st, out = c.push(e)
            self.assertEqual(st, 201, out)
            mid = out["id"]
            self.assertEqual(c.push(e)[0], 200)
            st, got = c.get(mid)
            self.assertEqual(st, 200)
            self.assertEqual(got["entry"], e)
            self.assertEqual(got["meta"]["installs"], 1)
            st, lst = c.by_target(O)
            self.assertEqual([m["id"] for m in lst["maps"]], [mid])
            self.assertEqual(c.by_target("http://pkgs.example.org")[0], 400)
            st, found = c.search("search")
            self.assertEqual([m["id"] for m in found["maps"]], [mid])
            self.assertEqual(c.get("f" * 32)[0], 404)

    def test_server_refuses_what_the_contract_refuses(self):
        with Loopback() as lb:
            private, public = S.generate_keypair()
            lb.reg.add_contributor("alice", public)
            c = lb.client("alice", private)
            for name in ("other_host", "values_travel", "headers_travel", "redirect_key"):
                st, out = c._call("POST", "/v0/maps", S.canonical_json(_vec(name)), signed=True)
                self.assertEqual((st, out["error"]), (422, "schema"), name)
                self.assertNotIn("evil", json.dumps(out))  # never echoes content
            st, out = c._call("POST", "/v0/maps", S.canonical_json(_vec("hash_mismatch")),
                              signed=True)
            self.assertEqual((st, out["error"]), (422, "hash_mismatch"))
            st, out = c._call("POST", "/v0/maps", b"not json", signed=True)
            self.assertEqual(st, 400)
            big = b"{" + b" " * (S.contract()["limits"]["max_body_bytes"] + 1) + b"}"
            self.assertEqual(c._call("POST", "/v0/maps", big, signed=True)[0], 413)

    def test_ed25519_auth_wrong_key_tampered_body_replay_hmac_and_skew(self):
        clock = Clock()
        with Loopback(clock) as lb:
            private, public = S.generate_keypair()
            wrong_private, _ = S.generate_keypair()
            lb.reg.add_contributor("alice", public)
            body = S.canonical_json(_vec("ok_web"))
            anon = lb.client()
            self.assertEqual(anon._call("POST", "/v0/maps", body)[0], 401)
            self.assertEqual(lb.client("alice", wrong_private)._call(
                "POST", "/v0/maps", body, signed=True)[0], 401)
            self.assertEqual(lb.client("mallory", private)._call(
                "POST", "/v0/maps", body, signed=True)[0], 401)
            import urllib.request
            a = S.contract()["auth"]
            ts, nonce = str(clock.t), "c" * 32
            hdr = {a["header_alg"]: a["alg"], a["header_key"]: "alice",
                   a["header_timestamp"]: ts, a["header_nonce"]: nonce,
                   a["header_signature"]: S.sign(private, "POST", "/v0/maps", ts, nonce, body),
                   "Content-Type": "application/json"}
            opener = lb.opener()

            def send(sent_body=body, sent_headers=hdr):
                req = urllib.request.Request(lb.url + "/v0/maps", data=sent_body, method="POST",
                                             headers=sent_headers)
                try:
                    return opener.open(req, timeout=5).status
                except urllib.error.HTTPError as e:
                    return e.code
            self.assertEqual(send(body + b" "), 401)
            self.assertEqual(send(), 201)
            self.assertEqual(send(), 401)  # same nonce: replay
            hmac_headers = dict(hdr)
            hmac_headers[a["header_alg"]] = "hmac-sha256"
            hmac_headers[a["header_nonce"]] = "d" * 32
            hmac_headers[a["header_signature"]] = "0" * 64
            self.assertEqual(send(body, hmac_headers), 401)
            skewed = lb.client("alice", private)
            skewed.clock = lambda: clock.t - 301
            self.assertEqual(skewed._call("POST", "/v0/maps", body, signed=True)[0], 401)

    def test_contributor_records_hold_only_strict_public_keys(self):
        with Loopback() as lb:
            lb.reg.add_contributor("alice", TEST_PUBLIC_KEY)
            columns = [row[1] for row in lb.reg.db.execute("PRAGMA table_info(contributors)")]
            self.assertEqual(columns, ["key_id", "public_key", "created", "disabled"])
            stored = lb.reg.db.execute(
                "SELECT public_key FROM contributors WHERE key_id='alice'").fetchone()[0]
            self.assertEqual(stored, TEST_PUBLIC_KEY)
            self.assertNotEqual(stored, TEST_PRIVATE_KEY)
            for invalid in ("+" + "x" * 42, TEST_PUBLIC_KEY + "=", "0" * 64):
                with self.assertRaises(S.RegistryError):
                    lb.reg.add_contributor("badkey", invalid)

    def test_unknown_routes_and_methods(self):
        with Loopback() as lb:
            c = lb.client()
            self.assertEqual(c._call("GET", "/v1/maps")[0], 404)
            self.assertEqual(c._call("DELETE", "/v0/maps")[0], 405)
            self.assertEqual(c._call("GET", "/v0/search?q=a&q=b")[0], 400)


class _Redirector(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "https://evil.example.com/v0/maps")
        self.end_headers()


class ClientConfig(unittest.TestCase):

    def test_no_registry_url_means_nothing_is_sent(self):
        prev = os.environ.pop(C.ENV_URL, None)
        try:
            with self.assertRaises(C.RegistryConfigError):
                C.registry_url(None)
        finally:
            if prev is not None:
                os.environ[C.ENV_URL] = prev
        for bad in ("http://registry.example.org", "https://u:p@registry.example.org",
                    "https://registry.example.org/?k=1", "ftp://registry.example.org"):
            with self.assertRaises(C.RegistryConfigError, msg=bad):
                C.registry_url(bad)
        self.assertEqual(C.registry_url("http://127.0.0.1:9/"), "http://127.0.0.1:9")

    def test_writes_need_explicit_credentials(self):
        c = C.RegistryClient("http://127.0.0.1:9", key_id="", private_key=None)
        with self.assertRaises(C.RegistryConfigError):
            c.push(_vec("ok_web"))

    def test_client_refuses_invalid_entries_before_sending(self):
        c = C.RegistryClient("http://127.0.0.1:9", key_id="alice",
                             private_key=TEST_PRIVATE_KEY)
        with self.assertRaises(S.RegistryError):
            c.push(_vec("values_travel"))

    def test_client_does_not_follow_redirects(self):
        transport = InMemoryHTTP(_Redirector)
        client = C.RegistryClient(transport.origin)
        client._opener = urllib.request.build_opener(C._NoRedirect, urllib.request.ProxyHandler({}), transport)
        st, _ = client.search("x")
        self.assertEqual(st, 302)
        self.assertEqual(len(transport.requests), 1)


# ── end to end: discover -> export -> push -> pull -> prove -> report ────────
class EndToEnd(unittest.TestCase):

    def setUp(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"
        import test_discover as TD
        self.TD = TD
        self.tmp = tempfile.TemporaryDirectory()
        self.store = C.CandidateStore(Path(self.tmp.name) / "candidates")

    def tearDown(self):
        self.tmp.cleanup()

    def _prove(self, mid, name, values=None):
        from anyuse.execution import discover as D
        from anyuse.execution.api_step import ApiStepExecutor
        TD = self.TD
        f = TD.fetcher()
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=TD.FakeTransport()))
        return C.prove_candidate(self.store, mid, name, f, values=values, executor=ex,
                                 proof_preflight=lambda c, r: "",
                                 transport=D.PacedTransport(f, inner=TD.FakeTransport()))

    def test_pulled_map_is_a_hint_until_proved_here(self):
        from anyuse.execution import discover as D
        TD = self.TD
        hd = D.discover_host(TD.O, TD.fetcher())
        entry, rep = C.export_web(hd.to_dict(), date="2026-11-03", version="0.4.0",
                                  origins=frozenset({TD.O}))
        self.assertIsNotNone(entry, rep)
        names = {c["name"] for c in entry["capabilities"]}
        self.assertIn("search", names)
        with Loopback() as lb:
            private_a, public_a = S.generate_keypair()
            private_b, public_b = S.generate_keypair()
            lb.reg.add_contributor("alice", public_a)
            lb.reg.add_contributor("bob", public_b)
            a, b = lb.client("alice", private_a), lb.client("bob", private_b)
            st, out = a.push(entry)
            self.assertEqual(st, 201, out)
            mid = out["id"]
            for who in (a, b):  # two independent reports -> "verified" on the server
                st, _ = who.report_proof(mid, {"capability": "search", "status": "proved",
                                               "date": "2026-11-03", "fresh_inputs": 2,
                                               "proof_kind": "learn",
                                               "anyuse_version": "0.4.0"})
                self.assertEqual(st, 201)
            st, pulled = b.get(mid)
            self.assertTrue(pulled["meta"]["verified"])
            rec = self.store.land(pulled, source=lb.url)
            # verified on the server, claimed proved by the contributor: still unproved here
            self.assertEqual({v["status"] for v in rec["local"].values()}, {"unproved"})
            self.assertEqual(rec["registry_verified_capabilities"], ["search"])

            local = self._prove(mid, "search")
            self.assertEqual(local["status"], "proved", local)
            self.assertEqual(local["values_from"], "local discovery")
            self.assertEqual(self.store.load(mid)["local"]["search"]["status"], "proved")
            report = C.proof_report(local, "search", version="0.4.0")
            self.assertEqual(set(report), {"capability", "status", "date", "fresh_inputs",
                                           "proof_kind", "anyuse_version"})
            st, meta = b.report_proof(mid, report)
            self.assertEqual(st, 201)
            # a re-pull never downgrades the local proof
            self.store.land(b.get(mid)[1], source=lb.url)
            self.assertEqual(self.store.load(mid)["local"]["search"]["status"], "proved")

    def test_tampered_pull_is_refused(self):
        e = _vec("ok_web")
        e["capabilities"][0]["request"]["template"] = "https://evil.example.com/s?q={q}"
        with self.assertRaises(S.RegistryError):
            self.store.land({"id": e["content_hash"][:32], "entry": e, "meta": {}}, "x")
        ok = _vec("ok_web")
        with self.assertRaises(S.RegistryError):
            self.store.land({"id": "0" * 32, "entry": ok, "meta": {}}, "x")
        self.assertEqual(self.store.list(), [])

    def test_no_site_values_means_not_proved(self):
        e = _vec("ok_web")
        e["capabilities"][0]["request"]["template"] = O + "/nowhere?q={q}"
        e = S.with_hash(e)
        rec = self.store.land({"entry": e, "meta": {"verified_capabilities": ["search"]}}, "x")
        local = self._prove(rec["id"], "search")  # template is not on the fake site
        self.assertEqual(local["status"], "unproved")
        self.assertIn("site values", local["reason"])


# ── CLI ─────────────────────────────────────────────────────────────────────
class Cli(unittest.TestCase):

    def test_map_commands_refuse_without_a_registry(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        prev = os.environ.pop(C.ENV_URL, None)
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "e.json"
                p.write_text(json.dumps(_vec("ok_web")))
                r = CliRunner().invoke(cli, ["map", "push", str(p), "--yes"], obj={})
                self.assertNotEqual(r.exit_code, 0)
                self.assertIn("no registry configured", r.output)
                r = CliRunner().invoke(cli, ["map", "pull", "--target", O], obj={})
                self.assertIn("no registry configured", r.output)
        finally:
            if prev is not None:
                os.environ[C.ENV_URL] = prev

    def test_map_keygen_creates_a_0600_private_key_and_never_prints_it(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        with tempfile.TemporaryDirectory() as d:
            previous = os.environ.get("ANYUSE_SECRETS_DIR")
            os.environ["ANYUSE_SECRETS_DIR"] = d
            try:
                result = CliRunner().invoke(cli, ["map", "keygen"], obj={})
                self.assertEqual(result.exit_code, 0, result.output)
                path = C.registry_private_key_path()
                private_key = path.read_text(encoding="ascii").strip()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(S.public_key_for(private_key),
                                 result.output.split("Public key: ", 1)[1].strip())
                self.assertNotIn(private_key, result.output)
                again = CliRunner().invoke(cli, ["map", "keygen"], obj={})
                self.assertNotEqual(again.exit_code, 0)
                self.assertIn("already exists", again.output)
            finally:
                if previous is None:
                    os.environ.pop("ANYUSE_SECRETS_DIR", None)
                else:
                    os.environ["ANYUSE_SECRETS_DIR"] = previous

    def test_map_export_writes_a_valid_entry(self):
        from click.testing import CliRunner
        from anyuse.cli import cli
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "bench.json"
            src.write_text(json.dumps({"hosts": {"code.dlang.org": _record("https://code.dlang.org")}}))
            out = Path(d) / "entry.json"
            r = CliRunner().invoke(cli, ["map", "export", str(src), "--host", "code.dlang.org",
                                         "--date", "2026-11-03", "-o", str(out)], obj={})
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertEqual(S.entry_errors(json.loads(out.read_text())), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
