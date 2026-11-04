"""Offline A/B bench: HAR import of requests the browser sent with cookies.

The question: does a HAR exported with cookies (Firefox, Safari, Chrome's export
with sensitive data) compile and prove as often as the same HAR without them,
and is a request that only works with its cookies still never saved?

Two parts, each run once per tree:

* ``fixture``: the real CLI, end to end, persisting
  (``anyuse workflow compile --from-har H --expect ... --yes --json``), against
  a package-search site this script serves on 127.0.0.1 over HTTPS (the
  quickstart fixture's catalog, ``examples/quickstart/fixture_site.py``). Each
  case runs in its own process with HOME and every AnyUse data path in a fresh
  directory under ``--out``; afterwards every file written there is searched
  for the HAR's cookie and bearer values. The server counts the requests it
  received with a ``Cookie`` header (it sets one on every API answer, so a
  client that kept cookies would send it back).
* ``crates``: ``har_import.compile_from_har`` in process on the four crates.io
  attach sessions in ``testdata/attach_crates``, written out as HARs, with the
  proof's requests answered from the recorded API bodies (no socket; the
  guarded sitecustomize refuses one). The ground truth is the first six crate
  names of each recorded API answer.

Fixture endpoints, ``/<mode>/page?q=`` (HTML) and ``/<mode>/api?q=`` (JSON):

* ``public``: the catalog's results, cookies or not.
* ``g401``, ``g403``, ``g302``: without a ``sid`` cookie, 401, 403, or a
  redirect to ``/login``.
* ``gempty``: without ``sid``, 200 with no results.
* ``gpersonal``: without ``sid``, 200 with the public results; with it, the
  signed-in user's own list. The HAR and the typed cases hold the signed-in
  list, as a signed-in user would have seen it.

Classes (one HAR plus two typed cases each; triples of fixture words):

* ``a``: ``public`` exported with cookies: firefox, safari, chrome-sensitive,
  each with consent cookies (``consent``, ``_ga``) or with a session-looking
  one too (``sessionid``). 3 x 2 x 4 = 24.
* ``c``: ``public`` exported sanitized (no cookies). 4.
* ``b``: each gated mode exported with cookies (``consent``, ``_ga``, ``sid``),
  three styles, two triples. 5 x 3 x 2 = 30.
* ``b-clean``: each gated mode exported sanitized: the request still needed the
  cookie, the export dropped it. 5 x 2 = 10.

Every cookie-carrying HAR also has a tracking POST with an ``Authorization``
header, which must stay refused.

Usage::

    python tests/bench/har_cookies_bench.py run --old <tree> --new <tree> \
        [--python <interpreter>] [--out <dir>] [--only fixture|crates]

``--old`` is a checkout at the commit the branch forked from, ``--new`` the
branch checkout. The script copies itself into ``--out`` (outside both trees);
each run's PYTHONPATH is a pkgroot directory holding an ``anyuse`` symlink to
the tree. No ``ANYUSE_*`` variable is passed through.
"""
from __future__ import annotations

import argparse
import datetime
import html
import ipaddress
import json
import os
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORDS = ["amber", "birch", "cedar", "cloud", "delta", "ember", "falcon", "harbor",
         "maple", "river", "stone", "tulip"]
SUFFIXES = ["core", "cli", "kit", "lite", "sdk", "tools"]
CATALOG = [f"{w}-{s}" for w in WORDS
           for s in SUFFIXES[WORDS.index(w) % 6:] + SUFFIXES[:WORDS.index(w) % 6]]
TRIPLES = [("river", "stone", "cloud"), ("maple", "harbor", "ember"),
           ("amber", "falcon", "tulip"), ("birch", "cedar", "delta")]
GATED = ("g401", "g403", "g302", "gempty", "gpersonal")
STYLES = {"firefox": "Firefox", "safari": "WebKit Web Inspector",
          "chrome-sensitive": "WebInspector"}
CONSENT = ("consent", "_ga")
SESSION = ("consent", "_ga", "sessionid")
GATE = ("consent", "_ga", "sid")
SECRET_MARKS = ("CKV-", "ATK-")


def search(q):
    q = q.strip().lower()
    return [n for n in CATALOG if q and q in n][:10]


def personal(q):
    """The signed-in user's own list. No name contains a public one: the proof
    matches a label that contains a returned value (``api_compile.values_match``),
    so ``saved river-core`` would pass as ``river-core``."""
    return [f"{q}-draft-{i}" for i in range(1, 7)] if q.strip() else []


# ---------------------------------------------------------------------------
# the fixture site
# ---------------------------------------------------------------------------

class Counts:
    lock = threading.Lock()
    requests = 0
    with_cookie = 0


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, ctype, body, extra=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with Counts.lock:
            Counts.requests += 1
            if self.headers.get("Cookie"):
                Counts.with_cookie += 1
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
        if url.path == "/robots.txt":
            return self._send(200, "text/plain", "User-agent: *\nAllow: /\n")
        parts = url.path.strip("/").split("/")
        if len(parts) != 2 or parts[1] not in ("page", "api"):
            return self._send(404, "text/plain", "not found")
        mode, kind = parts
        signed_in = "sid=" in (self.headers.get("Cookie") or "")
        if mode == "public":
            names = search(q)
        elif signed_in:
            names = personal(q)
        elif mode == "g401":
            return self._send(401, "application/json", '{"error": "sign in"}')
        elif mode == "g403":
            return self._send(403, "application/json", '{"error": "forbidden"}')
        elif mode == "g302":
            return self._send(302, "text/html", "", [("Location", "/login")])
        elif mode == "gempty":
            names = []
        elif mode == "gpersonal":
            names = search(q)
        else:
            return self._send(404, "text/plain", "not found")
        if kind == "api":
            return self._send(200, "application/json",
                              json.dumps({"count": len(names),
                                          "results": [{"name": n} for n in names]}),
                              [("Set-Cookie", "srv_session=SRV-1; Path=/")])
        return self._send(200, "text/html; charset=utf-8", page_html(q, names))

    def log_message(self, *_a):
        pass


def page_html(q, names):
    rows = "".join(f'<li class="result">{html.escape(n)}</li>' for n in names)
    return (f"<!doctype html><html><head><title>Search results for {html.escape(q)}</title>"
            f"</head><body><ul id='results'>{rows}</ul></body></html>")


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_cert(certdir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    os.makedirs(certdir, exist_ok=True)
    cert, key = os.path.join(certdir, "cert.pem"), os.path.join(certdir, "key.pem")
    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
           .public_key(pk.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=1))
           .add_extension(x509.SubjectAlternativeName(
               [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .sign(pk, hashes.SHA256()))
    with open(key, "wb") as fh:
        fh.write(pk.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert, "wb") as fh:
        fh.write(crt.public_bytes(serialization.Encoding.PEM))
    return cert, key


def start_fixture(certdir):
    cert, key = make_cert(certdir)
    server = LoopbackServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, cert


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------

def har_entry(url, method="GET", status=200, mime="application/json", text="",
              cookies=None, extra_headers=None, resp_headers=None, post=None):
    headers = [{"name": "Accept", "value": "*/*"},
               {"name": "User-Agent", "value": "Mozilla/5.0 (bench)"}]
    if cookies:
        headers.append({"name": "Cookie",
                        "value": "; ".join(f"{k}={v}" for k, v in cookies)})
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    req = {"method": method, "url": url, "httpVersion": "HTTP/2", "headers": headers,
           "cookies": [{"name": k, "value": v} for k, v in (cookies or [])],
           "queryString": [], "headersSize": -1, "bodySize": -1}
    if post is not None:
        req["postData"] = {"mimeType": "application/json", "text": post}
    rh = [{"name": "Content-Type", "value": mime}] + list(resp_headers or [])
    return {"startedDateTime": "2026-10-22T10:00:00.000Z", "time": 12, "request": req,
            "response": {"status": status, "statusText": "OK", "httpVersion": "HTTP/2",
                         "headers": rh, "cookies": [],
                         "content": {"size": len(text), "mimeType": mime, "text": text},
                         "redirectURL": "", "headersSize": -1, "bodySize": len(text)},
            "cache": {}, "timings": {"send": 0, "wait": 10, "receive": 2}}


def write_har(path, creator, entries):
    with open(path, "w") as fh:
        json.dump({"log": {"version": "1.2", "creator": {"name": creator, "version": "1"},
                           "pages": [], "entries": entries}}, fh)


def fixture_cases():
    out = []
    for style in STYLES:
        for names in (CONSENT, SESSION):
            for triple in TRIPLES:
                out.append({"cls": "a", "mode": "public", "style": style,
                            "cookies": names, "triple": triple})
    for triple in TRIPLES:
        out.append({"cls": "c", "mode": "public", "style": "chrome-sanitized",
                    "cookies": (), "triple": triple})
    for mode in GATED:
        for style in STYLES:
            for triple in TRIPLES[:2]:
                out.append({"cls": "b", "mode": mode, "style": style,
                            "cookies": GATE, "triple": triple})
        for triple in TRIPLES[:2]:
            out.append({"cls": "b-clean", "mode": mode, "style": "chrome-sanitized",
                        "cookies": (), "triple": triple})
    for i, case in enumerate(out):
        case["id"] = f"f{i:03d}"
    return out


def expected_for(case, value):
    return search(value) if case["mode"] == "public" else personal(value)


def build_fixture_har(case, origin, corpus):
    value = case["triple"][0]
    cookies = [(n, f"CKV-{case['id']}-{n}") for n in case["cookies"]]
    shown = expected_for(case, value)
    q = urllib.parse.quote(value)
    entries = [
        har_entry(f"{origin}/{case['mode']}/page?q={q}", mime="text/html",
                  text=page_html(value, shown), cookies=cookies),
        har_entry(f"{origin}/{case['mode']}/api?q={q}",
                  text=json.dumps({"count": len(shown),
                                   "results": [{"name": n} for n in shown]}),
                  cookies=cookies,
                  resp_headers=[{"name": "Set-Cookie",
                                 "value": f"sid=CKV-{case['id']}-rotated; Path=/"}]
                  if cookies else []),
    ]
    if cookies:
        entries.append(har_entry(f"{origin}/collect", method="POST", text="{}",
                                 cookies=cookies, post='{"event": "search"}',
                                 extra_headers={"Authorization":
                                                f"Bearer ATK-{case['id']}"}))
    path = os.path.join(corpus, f"{case['id']}.har")
    write_har(path, STYLES.get(case["style"], "WebInspector"), entries)
    return path


# ---------------------------------------------------------------------------
# running one tree
# ---------------------------------------------------------------------------

CLI_BOOT = ("import sys; sys.argv[0] = 'anyuse'; "
            "from anyuse.cli import cli; cli()")


def base_env(pkgroot, home):
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "PYTHONPATH": pkgroot,
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
           "XDG_CONFIG_HOME": os.path.join(home, ".config"),
           "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
           "XDG_CACHE_HOME": os.path.join(home, ".cache")}
    data = os.path.join(home, ".anyuse")
    env.update({"ANYUSE_DATA_DIR": data, "ANYUSE_SECRETS_DIR": data,
                "ANYUSE_VAULT_KEYFILE": os.path.join(data, "entity_vault.keys"),
                "ANYUSE_API_HOSTS_FILE": os.path.join(data, "api_hosts.json"),
                "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(data, "api_blocked_hosts.json"),
                "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(data, "robots.json")})
    return env


def leaks_under(root):
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if any(mark in text for mark in SECRET_MARKS):
                hits.append(os.path.relpath(path, root))
    return hits


def parse_report(stdout):
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                return None
    return None


def run_fixture_arm(arm, py, pkgroot, out, origin, cert, cases, hars):
    rows = []
    Counts.requests = Counts.with_cookie = 0
    for case in cases:
        home = os.path.join(out, "runs", arm, case["id"])
        os.makedirs(home)
        env = base_env(pkgroot, home)
        env["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = origin
        env["SSL_CERT_FILE"] = cert
        v0, v1, v2 = case["triple"]
        args = [py, "-c", CLI_BOOT, "workflow", "compile", f"bench_{case['id']}", "--yes",
                "--json", "--from-har", hars[case["id"]], "--har-param", f"q={v0}",
                "--har-expect", "|".join(expected_for(case, v0)),
                "--expect", f"{v1}=" + "|".join(expected_for(case, v1)),
                "--expect", f"{v2}=" + "|".join(expected_for(case, v2))]
        t0 = time.monotonic()
        proc = subprocess.run(args, env=env, cwd=home, capture_output=True, text=True,
                              timeout=180)
        seconds = time.monotonic() - t0
        report = parse_report(proc.stdout) or {}
        har = report.get("har") or {}
        equivalence = report.get("equivalence") or {}
        extraction = report.get("extraction") or {}
        persisted = report.get("persisted")
        cookie_refs = False
        if persisted and os.path.exists(persisted):
            with open(persisted) as fh:
                cookie_refs = "cookie_refs" in fh.read()
        rows.append({
            "arm": arm, "part": "fixture", "id": case["id"], "cls": case["cls"],
            "mode": case["mode"], "style": case["style"], "cookies": list(case["cookies"]),
            "rc": proc.returncode,
            "refused_at_import": "HAR import refused" in proc.stdout,
            "refusal": proc.stdout.strip()[:200] if "HAR import refused" in proc.stdout else "",
            "compiled": bool(extraction.get("template")),
            "proof_passed": bool(equivalence.get("passed")),
            "persisted": bool(persisted),
            "needs_sign_in": bool(har.get("needs_sign_in")),
            "credential_bound": sum(int(s.get("credential_bound") or 0)
                                    for s in har.get("source") or [] if "entries" in s),
            "cookie_refs": cookie_refs,
            "leaks": leaks_under(home),
            "stdout_leak": any(m in proc.stdout + proc.stderr for m in SECRET_MARKS),
            "seconds": round(seconds, 2),
            "stderr_tail": proc.stderr.strip()[-300:] if proc.returncode not in (0, 1, 2) else "",
        })
    counts = {"requests": Counts.requests, "with_cookie": Counts.with_cookie}
    return rows, counts


CRATES_CHILD = r'''
import json, os, sys, urllib.parse
out_path, data_dir, corpus = sys.argv[1], sys.argv[2], sys.argv[3]
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
from anyuse.execution.har_import import HarCase, HarError, compile_from_har
sessions = []
for name in sorted(os.listdir(data_dir)):
    with open(os.path.join(data_dir, name)) as fh:
        sessions.append(json.load(fh))
def api(s):
    return next(n for n in s["network"] if "/api/v1/crates" in n["url"] and n.get("body"))
def q_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
bodies = {q_of(api(s)["url"]): api(s)["body"] for s in sessions}
labels = {q: [c["name"] for c in json.loads(b)["crates"]][:6] for q, b in bodies.items()}
sent = []
class Recorded:
    def send(self, method, url, headers, body, timeout_s):
        sent.append(sorted(k.lower() for k in (headers or {})))
        b = bodies.get(q_of(url))
        if b is None:
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        return ApiResponse(200, {"Content-Type": "application/json"}, b)
rows = []
for name in sorted(os.listdir(corpus)):
    style, idx = name[:-4].rsplit("_", 1)
    i = int(idx)
    qs = [q_of(api(sessions[(i + k) % len(sessions)])["url"]) for k in range(3)]
    cases = [HarCase(os.path.join(corpus, name), qs[0], labels[qs[0]]),
             HarCase("", qs[1], labels[qs[1]]), HarCase("", qs[2], labels[qs[2]])]
    row = {"part": "crates", "id": name[:-4], "cls": "crates-" + style}
    try:
        compiled, proof, report = compile_from_har(
            cases, param_name="q", executor=ApiStepExecutor(transport=Recorded()),
            preflight=lambda _c: "")
        tmpl = json.dumps(compiled.template.to_dict()) if compiled.template is not None else ""
        row.update(refused_at_import=False, compiled=compiled.template is not None,
                   proof_passed=bool(proof.passed), reason=proof.reason[:160],
                   leak=any(m in tmpl + json.dumps(report, default=str) for m in ("CKV-",)),
                   cookie_refs="cookie_refs" in tmpl)
    except HarError as exc:
        row.update(refused_at_import=True, compiled=False, proof_passed=False,
                   reason=str(exc)[:160], leak=False, cookie_refs=False)
    rows.append(row)
with open(out_path, "w") as fh:
    json.dump({"rows": rows, "cookie_headers_sent": sum("cookie" in h for h in sent),
               "requests_sent": len(sent)}, fh)
'''


def build_crates_corpus(tree, corpus):
    data_dir = os.path.join(tree, "testdata", "attach_crates")
    os.makedirs(corpus)
    names = sorted(os.listdir(data_dir))
    for i, name in enumerate(names):
        with open(os.path.join(data_dir, name)) as fh:
            session = json.load(fh)
        for style, jar in (("clean", ()),
                           ("consent", ("cookieconsent_status", "_ga")),
                           ("session", ("cookieconsent_status", "_ga", "cargo_session"))):
            cookies = [(n, f"CKV-crates{i}-{n}") for n in jar]
            entries = [har_entry(n["url"], status=n.get("status") or 200,
                                 mime=n.get("mime") or "application/json",
                                 text=n.get("body") or "", cookies=cookies)
                       for n in session["network"]]
            write_har(os.path.join(corpus, f"{style}_{i}.har"), "Firefox", entries)
    return data_dir


def run_crates_arm(arm, py, pkgroot, tree_for_guard, out, data_dir, corpus):
    home = os.path.join(out, "runs", arm, "crates")
    os.makedirs(home)
    env = base_env(pkgroot + os.pathsep + os.path.join(tree_for_guard, "tools",
                                                      "guarded_tests"), home)
    child = os.path.join(out, "crates_child.py")
    with open(child, "w") as fh:
        fh.write(CRATES_CHILD)
    result = os.path.join(home, "result.json")
    proc = subprocess.run([py, child, result, data_dir, corpus], env=env, cwd=home,
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"crates child failed for {arm}:\n{proc.stderr[-2000:]}")
    with open(result) as fh:
        got = json.load(fh)
    for row in got["rows"]:
        row["arm"] = arm
    return got["rows"], {"requests": got["requests_sent"],
                         "with_cookie": got["cookie_headers_sent"]}


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def summarize(rows):
    out = {}
    for row in rows:
        key = (row["arm"], row["part"], row["cls"])
        s = out.setdefault(key, {"n": 0, "refused_at_import": 0, "compiled": 0,
                                 "proof_passed": 0, "persisted": 0, "needs_sign_in": 0,
                                 "leaks": 0, "cookie_refs": 0, "seconds": []})
        s["n"] += 1
        for k in ("refused_at_import", "compiled", "proof_passed", "needs_sign_in",
                  "cookie_refs"):
            s[k] += int(bool(row.get(k)))
        s["persisted"] += int(bool(row.get("persisted", row.get("proof_passed"))))
        s["leaks"] += int(bool(row.get("leaks") or row.get("leak") or row.get("stdout_leak")))
        if "seconds" in row:
            s["seconds"].append(row["seconds"])
    for s in out.values():
        secs = s.pop("seconds")
        s["median_s"] = round(statistics.median(secs), 2) if secs else None
    return out


def print_table(summary, counts):
    cols = ("n", "refused_at_import", "compiled", "proof_passed", "persisted",
            "needs_sign_in", "leaks", "cookie_refs", "median_s")
    print(f"{'arm':4} {'part':8} {'class':16} " + " ".join(f"{c:>8.8}" for c in cols))
    for (arm, part, cls), s in sorted(summary.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0] != "old")):
        print(f"{arm:4} {part:8} {cls:16} " + " ".join(f"{str(s[c]):>8}" for c in cols))
    for (arm, part), c in sorted(counts.items()):
        print(f"{arm} {part}: {c['requests']} requests received/sent, "
              f"{c['with_cookie']} with a Cookie header")


def main(argv):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--old", required=True)
    run.add_argument("--new", required=True)
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--out", default="")
    run.add_argument("--only", choices=("fixture", "crates"), default="")
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="har-cookies-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"old": os.path.abspath(args.old), "new": os.path.abspath(args.new)}
    for tree in trees.values():
        if out.startswith(tree + os.sep):
            raise SystemExit("--out must be outside both trees")
    shutil.copy(os.path.abspath(__file__), os.path.join(out, "bench.py"))
    pkgroots = {}
    for arm, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{arm}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if not os.path.islink(link):
            os.symlink(tree, link)
        pkgroots[arm] = pkgroot
    rows, counts = [], {}
    if args.only in ("", "fixture"):
        server, cert = start_fixture(os.path.join(out, "cert"))
        origin = f"https://127.0.0.1:{server.server_port}"
        corpus = os.path.join(out, "corpus_fixture")
        os.makedirs(corpus)
        cases = fixture_cases()
        hars = {c["id"]: build_fixture_har(c, origin, corpus) for c in cases}
        for arm in ("old", "new"):
            got, c = run_fixture_arm(arm, args.python, pkgroots[arm], out, origin, cert,
                                     cases, hars)
            rows += got
            counts[(arm, "fixture")] = c
        server.shutdown()
    if args.only in ("", "crates"):
        corpus = os.path.join(out, "corpus_crates")
        data_dir = build_crates_corpus(trees["new"], corpus)
        for arm in ("old", "new"):
            got, c = run_crates_arm(arm, args.python, pkgroots[arm], trees["new"], out,
                                    data_dir, corpus)
            rows += got
            counts[(arm, "crates")] = c
    summary = summarize(rows)
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump({"rows": rows,
                   "summary": {"/".join(k): v for k, v in summary.items()},
                   "counts": {"/".join(k): v for k, v in counts.items()}}, fh, indent=1)
    print_table(summary, counts)
    print(f"results: {os.path.join(out, 'results.json')}")


if __name__ == "__main__":
    main(sys.argv[1:])
