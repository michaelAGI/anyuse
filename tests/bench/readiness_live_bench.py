"""Live A/B bench: the browser replay's readiness settle against the fixed timer, in a real Chrome.

The offline bench (``readiness_bench.py``) simulates the pages; this one serves
them from a fixture site and reads them in a real headless Chrome, both in
containers on a host-only container network that the person running the bench
names. Nothing here contacts a public site, publishes a port or creates,
picks or changes a network. Each case's timing (the delay of each request, the
gap before a second page, a ticking clock) is the offline bench's
:class:`readiness_bench.SimPage` for the same mode, word and seed, and the
rows are its ``full_list``, so both layers use one corpus.

Steps (``container`` is Apple's CLI; any runtime with a host-only network works)::

    # 1. the two container commands, printed for the network you name (this runs nothing)
    python tests/bench/readiness_live_bench.py containers --network NET \
        --bench-dir tests/bench --cert-dir CERTDIR
    # 2. run them; read both addresses (``container ls``); they change on restart
    # 3. a certificate for the fixture's address (the fixture waits for it)
    python tests/bench/readiness_live_bench.py cert --fixture-origin https://FIXTURE_IP:8443 \
        --cert-dir CERTDIR
    # 4. the bench
    python tests/bench/readiness_live_bench.py run --network NET \
        --cdp-endpoint http://CHROME_IP:9222 --fixture-origin https://FIXTURE_IP:8443 \
        --cert-dir CERTDIR --out OUTDIR
    python tests/bench/readiness_live_bench.py compare OUTDIR/rows.json

``run`` needs no installs beyond the AnyUse test environment (``websockets``)
and the ``cryptography`` package or an ``openssl`` binary for ``cert``. It
refuses an endpoint or origin that is not an IP literal in a private or
loopback range. Every AnyUse data path points into ``--out``.

Part 1, replays: modes ``xhr`` and ``lazy`` (``--modes``), 12 words x 17 seeds
= 204 cases per mode; per case both arms read the same plan, in a seeded
random order, through one ``ReplayExecutor`` (its per-host pacing holds).
Arms: ``timer`` (``template_from_recording(..., readiness=False)``) and
``ready`` (``readiness=True``), from recordings made by direct fetches (river
compiled from, stone and cloud held out). Metrics per arm and mode: early read
(the extracted list is not the full list), wrong success, failures, median and
p95 of the executor's ``elapsed_ms``, how the settle ended. Pass: 0 wrong
successes; early reads no higher than the timer; the ``xhr`` median at most
40% of the timer's; p95 at most the timer's p95 + 0.5 s.

Part 2, heal (``--heal-runs``, 10 per arm): compile ``fixture_search`` with
``anyuse workflow compile --recording`` (``ANYUSE_TIER2_READINESS`` on or off,
which decides the stored browser template), switch the fixture's API to the
changed shape, then call ``compiled_tools.run_compiled``: the browser replay
answers and the automatic recompile runs. Recorded: wall time, whether the
recompile swapped, how many pages were replayed. Pass: the same swap rate in
both arms and a lower median wall time with readiness.

Expected time: about 70 min for the timer arm of part 1 and 15 min for the
readiness arm; part 2 a few minutes.
"""
from __future__ import annotations

import argparse
import datetime
import html
import ipaddress
import json
import os
import random
import shlex
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import readiness_bench as B  # noqa: E402

WORKFLOW = "fixture_search"
LIVE_MODES = ("xhr", "lazy", "big")
BROWSER_NAME = "anyuse-browser-readiness"
FIXTURE_NAME = "anyuse-fixture-readiness"
BROWSER_IMAGE = "chromedp/headless-shell:latest"
FIXTURE_IMAGE = "python:3.11-slim"
CHROME_ARGS = ("--ignore-certificate-errors", "--disable-dev-shm-usage")
#: words the heal calls use: not a recorded input, so the recompile may compile from the call
HEAL_WORDS = [w for w in B.WORDS if w not in B.RECORDED]


# ---------------------------------------------------------------------------
# the fixture site (runs inside the fixture container; standard library only)
# ---------------------------------------------------------------------------

PAGE_SCRIPT = """(function () {
  var q = %(q)s, tick = %(tick)s;
  function names(j) {
    var list = j.results || j.packages || [];
    return list.map(function (r) { return r.name || r.title || ""; });
  }
  function render(list) {
    var ul = document.getElementById("results");
    list.forEach(function (n) {
      var li = document.createElement("li"); li.className = "result";
      var a = document.createElement("a"); a.href = "/pkg/" + encodeURIComponent(n);
      a.textContent = n; li.appendChild(a); ul.appendChild(li);
    });
  }
  function load(page) {
    fetch("/api/search?q=" + encodeURIComponent(q) + (page > 1 ? "&page=" + page : ""))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        render(names(j));
        var next = j.next || j.more;
        if (next) { setTimeout(function () { load(next.page); }, next.after_ms || 0); }
      });
  }
  if (tick) {
    var t0 = Date.now();
    setInterval(function () {
      var s = 43200 + Math.floor((Date.now() - t0) / 1000);
      var p = function (x) { return (x < 10 ? "0" : "") + x; };
      document.getElementById("clock").textContent =
        p(Math.floor(s / 3600)) + ":" + p(Math.floor(s / 60) %% 60) + ":" + p(s %% 60);
    }, 1000);
  }
  load(1);
})();"""


class FixtureState:
    """The plans (per query) and the API shape, set over ``/__bench/*`` by the bench."""
    lock = threading.Lock()
    plans: Dict[str, Dict[str, Any]] = {}
    shape = "original"


def fixture_plan(q: str) -> Dict[str, Any]:
    with FixtureState.lock:
        return dict(FixtureState.plans.get(q) or {"mode": "xhr"})


def fixture_page(q: str, mode: str, page: int) -> Tuple[List[str], bool]:
    """The rows of one API page, and whether a further page follows."""
    if q not in B.WORDS or mode not in LIVE_MODES:
        return [], False
    full = B.full_list(q, mode)
    if mode == "xhr":
        return (full if page == 1 else []), False
    first = 15 if mode == "big" else 6
    if page == 1:
        return full[:first], len(full) > first
    return (full[first:] if page == 2 else []), False


def _fixture_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status, ctype, body: str):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(url.query)

            def arg(name, default=""):
                return qs.get(name, [default])[0]
            q = arg("q")
            if url.path == "/robots.txt":
                return self._send(200, "text/plain", "User-agent: *\nAllow: /\n")
            if url.path == "/__bench/plan":
                plan = {"mode": arg("mode", "xhr")}
                for k in ("delay_ms", "gap_ms", "delay2_ms"):
                    plan[k] = max(0, int(float(arg(k, "0") or 0)))
                plan["tick"] = arg("tick") == "1"
                with FixtureState.lock:
                    FixtureState.plans[q] = plan
                return self._send(200, "application/json", json.dumps({"q": q, **plan}))
            if url.path == "/__bench/shape":
                with FixtureState.lock:
                    FixtureState.shape = "changed" if arg("value") == "changed" else "original"
                    shape = FixtureState.shape
                return self._send(200, "application/json", json.dumps({"shape": shape}))
            if url.path == "/__bench/truth":
                mode = arg("mode", "xhr")
                full = B.full_list(q, mode) if q in B.WORDS and mode in LIVE_MODES else []
                return self._send(200, "application/json", json.dumps({"q": q, "items": full}))
            if url.path == "/api/search":
                plan = fixture_plan(q)
                page = max(1, int(arg("page", "1") or 1))
                time.sleep((plan.get("delay_ms" if page == 1 else "delay2_ms") or 0) / 1000.0)
                rows, more = fixture_page(q, plan.get("mode", "xhr"), page)
                nxt = {"page": page + 1, "after_ms": int(plan.get("gap_ms") or 0)}
                with FixtureState.lock:
                    shape = FixtureState.shape
                if shape == "changed":
                    payload: Dict[str, Any] = {"total": len(rows),
                                               "packages": [{"title": n} for n in rows]}
                    if more:
                        payload["more"] = nxt
                else:
                    payload = {"count": len(rows),
                               "results": [{"name": n, "downloads": 1000 - 7 * i}
                                           for i, n in enumerate(rows)]}
                    if more:
                        payload["next"] = nxt
                return self._send(200, "application/json", json.dumps(payload))
            if url.path in ("/", "/search"):
                plan = fixture_plan(q)
                script = PAGE_SCRIPT % {"q": json.dumps(q), "tick": "true" if plan.get("tick") else "false"}
                clock = '<p id="clock">12:00:00</p>' if plan.get("tick") else ""
                page = (f"<!doctype html><html><head><title>Search results for {html.escape(q)}"
                        f" - Fixture Packages</title></head><body><nav><a href='/'>Fixture Packages"
                        f"</a></nav><form action='/search'><input name='q' value='{html.escape(q)}'>"
                        f"</form>{clock}<ul id='results'></ul><script>{script}</script></body></html>")
                return self._send(200, "text/html; charset=utf-8", page)
            return self._send(404, "text/plain", "not found")

        def log_message(self, *_args):
            pass
    return Handler


def serve_fixture(args) -> int:
    import socketserver
    from http.server import ThreadingHTTPServer

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            # http.server's server_bind also runs socket.getfqdn(host), a reverse lookup
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    cert = os.path.join(args.cert_dir, "cert.pem")
    key = os.path.join(args.cert_dir, "key.pem")
    deadline = time.monotonic() + args.wait_cert
    while not (os.path.exists(cert) and os.path.exists(key)):
        if time.monotonic() > deadline:
            print(f"fixture: no {cert} after {args.wait_cert} s; run the cert step", file=sys.stderr)
            return 2
        time.sleep(1.0)
    server = Server((args.host, args.port), _fixture_handler())
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"fixture: https on {args.host}:{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


# ---------------------------------------------------------------------------
# certificate and container commands (host side; nothing here starts anything)
# ---------------------------------------------------------------------------

def private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def checked_origin(value: str, scheme: str, what: str) -> Tuple[str, str, int]:
    """``(origin, host, port)`` of a private-address URL, or SystemExit."""
    sp = urllib.parse.urlsplit(value)
    if sp.scheme not in scheme.split("|") or not sp.hostname or not sp.port:
        raise SystemExit(f"{what} must look like {scheme.split('|')[0]}://IP:PORT")
    if not private_ip(sp.hostname):
        raise SystemExit(f"{what}: {sp.hostname!r} is not an IP literal in a private or loopback "
                         f"range; this bench talks to its own containers only")
    if sp.path not in ("", "/") or sp.query:
        raise SystemExit(f"{what} must be an origin, without a path or query")
    return f"{sp.scheme}://{sp.netloc}", sp.hostname, sp.port


def make_cert(cert_dir: str, ip: str) -> Tuple[str, str]:
    cert, key = os.path.join(cert_dir, "cert.pem"), os.path.join(cert_dir, "key.pem")
    os.makedirs(cert_dir, exist_ok=True)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "2", "-subj", f"/CN={ip}",
                        "-addext", f"subjectAltName=IP:{ip}"], check=True, capture_output=True)
        return cert, key
    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    now = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
           .public_key(pk.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=2))
           .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),
                          critical=False)
           .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
           .sign(pk, hashes.SHA256()))
    with open(key, "wb") as fh:
        fh.write(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert, "wb") as fh:
        fh.write(crt.public_bytes(serialization.Encoding.PEM))
    return cert, key


def container_commands(network: str, bench_dir: str, cert_dir: str, port: int) -> List[List[str]]:
    """The two ``container run`` commands: no published port, no --rm (the logs stay)."""
    return [
        ["container", "run", "-d", "--name", BROWSER_NAME, "--network", network,
         BROWSER_IMAGE, *CHROME_ARGS],
        ["container", "run", "-d", "--name", FIXTURE_NAME, "--network", network,
         "--mount", f"type=bind,source={os.path.abspath(bench_dir)},target=/bench,readonly",
         "--mount", f"type=bind,source={os.path.abspath(cert_dir)},target=/cert,readonly",
         FIXTURE_IMAGE, "python3", "/bench/readiness_live_bench.py", "fixture",
         "--host", "0.0.0.0", "--port", str(port), "--cert-dir", "/cert"],
    ]


# ---------------------------------------------------------------------------
# talking to the fixture from the host
# ---------------------------------------------------------------------------

class Fixture:
    def __init__(self, origin: str, cafile: str):
        self.origin = origin.rstrip("/")
        self.ctx = ssl.create_default_context(cafile=cafile)

    def get(self, path: str, timeout: float = 20.0) -> Dict[str, Any]:
        url = self.origin + path
        with urllib.request.urlopen(url, context=self.ctx, timeout=timeout) as resp:
            body = resp.read().decode()
            return {"url": url, "method": "GET", "status": int(resp.status),
                    "mime": resp.headers.get("Content-Type", "").split(";")[0],
                    "bytes": len(body), "body": body}

    def plan(self, word: str, mode: str, seed: Optional[int]) -> Dict[str, Any]:
        """Set ``word``'s plan from :class:`readiness_bench.SimPage` (``seed`` None: no delays)."""
        q = {"q": word, "mode": mode, "delay_ms": 0, "gap_ms": 0, "delay2_ms": 0, "tick": 0}
        if seed is not None:
            page = B.SimPage(word, mode, seed)
            b = page.batches
            q["delay_ms"] = round((b[0][1] - b[0][0]) * 1000)
            if len(b) > 1:
                q["gap_ms"] = round((b[1][0] - b[0][1] - B.RENDER_S) * 1000)
                q["delay2_ms"] = round((b[1][1] - b[1][0]) * 1000)
            q["tick"] = int(page.tick)
        json.loads(self.get("/__bench/plan?" + urllib.parse.urlencode(q))["body"])
        return q

    def shape(self, value: str) -> None:
        self.get("/__bench/shape?value=" + value)

    def truth(self, word: str, mode: str) -> List[str]:
        q = urllib.parse.urlencode({"q": word, "mode": mode})
        return json.loads(self.get("/__bench/truth?" + q)["body"])["items"]

    def record(self, word: str, mode: str, tag: str) -> Dict[str, Any]:
        """What a browser loads for ``word`` with no delays: the page, then each API page."""
        self.plan(word, mode, None)
        page = self.get(f"/search?q={urllib.parse.quote(word)}")
        network = [page]
        items: List[str] = []
        n = 1
        while True:
            api = self.get(f"/api/search?q={urllib.parse.quote(word)}" + (f"&page={n}" if n > 1 else ""))
            network.append(api)
            body = json.loads(api["body"])
            items += [r.get("name") or r.get("title") or ""
                      for r in body.get("results") or body.get("packages") or []]
            nxt = body.get("next") or body.get("more")
            if not nxt or n >= 4:
                break
            n = int(nxt.get("page") or n + 1)
        return {"_note": "Recorded from the readiness live bench fixture.",
                "workflow": WORKFLOW, "tag": tag,
                "site": urllib.parse.urlsplit(self.origin).netloc,
                "param": {"name": "q", "value": word}, "final_url": page["url"],
                "ground_truth_selector": B.SELECTOR,
                "ui": {"items": items, "text": "\n".join(items)}, "network": network}


# ---------------------------------------------------------------------------
# the replays (in the child process, with the bench environment)
# ---------------------------------------------------------------------------

def counting_driver(endpoint: str):
    from anyuse.execution.cdp_replay import CdpDriver

    class CountingDriver(CdpDriver):
        """A ``CdpDriver`` that notes each page it opens (elapsed and settle)."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.opened: List[Dict[str, Any]] = []

        def open(self, url, settle_s, timeout_s, readiness=None, url_policy=None,
                 content_selector="", capture_traffic=False):
            page = super().open(url, settle_s, timeout_s, readiness=readiness,
                                url_policy=url_policy, content_selector=content_selector,
                                capture_traffic=capture_traffic)
            self.opened.append({"url": url, "elapsed_ms": round(page.elapsed_ms, 1),
                                "how": (page.settle or {}).get("how", ""),
                                "readiness": bool(readiness)})
            return page
    return CountingDriver(endpoint)


def replay_row(executor, tdict: Dict[str, Any], fixture: Fixture, arm: str, mode: str,
               word: str, seed: int) -> Dict[str, Any]:
    from anyuse.execution.cdp_replay import ReplayTemplate
    plan = fixture.plan(word, mode, seed)
    t0 = time.monotonic()
    out = executor.execute(ReplayTemplate.from_dict(tdict), {"q": word})
    wall = (time.monotonic() - t0) * 1000.0
    full = B.full_list(word, mode)
    extracted = (out.get("extracted") or {}).get("items")
    items = [str(x) for x in extracted] if isinstance(extracted, list) else None
    ok = bool(out.get("success"))
    return {"arm": arm, "mode": mode, "word": word, "seed": seed, "plan": plan,
            "success": ok, "outcome_kind": out.get("outcome_kind") or "",
            "infrastructure": out.get("infrastructure") or "",
            "n_read": len(items) if items is not None else None, "n_full": len(full),
            "early": (items != full) if items is not None else None,
            "wrong_success": ok and items != full,
            "elapsed_ms": round(float(out.get("elapsed_ms") or 0.0), 1), "wall_ms": round(wall, 1),
            "how": (out.get("settle") or {}).get("how", ""),
            "error": str(out.get("error") or "")[:200]}


def child_replays(args) -> int:
    from anyuse.execution.cdp_replay import ReplayExecutor, template_from_recording
    fixture = Fixture(args.fixture_origin, os.path.join(args.cert_dir, "cert.pem"))
    fixture.shape("original")
    out_rows: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"templates": {}, "truth_mismatch": []}
    executor = ReplayExecutor(driver=counting_driver(args.cdp_endpoint))
    modes = [m for m in args.modes.split(",") if m]
    templates: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for mode in modes:
        for w in B.WORDS:
            if fixture.truth(w, mode) != B.full_list(w, mode):
                meta["truth_mismatch"].append([mode, w])
        recs = [fixture.record(w, mode, tag) for w, tag in zip(B.RECORDED, ("rec", "fresh1", "fresh2"))]
        templates[mode] = {
            "timer": template_from_recording(recs[0], "q", recordings=recs, readiness=False).to_dict(),
            "ready": template_from_recording(recs[0], "q", recordings=recs, readiness=True).to_dict()}
        meta["templates"][mode] = {a: {"url": t["url"], "min_items": t["expect"].get("min_items"),
                                       "readiness": t.get("readiness") or {}}
                                   for a, t in templates[mode].items()}
    if meta["truth_mismatch"]:
        raise SystemExit(f"the fixture serves other lists than readiness_bench: "
                         f"{meta['truth_mismatch'][:5]}")
    cases = [(m, w, s) for m in modes for w in B.WORDS for s in range(args.seeds)]
    rng = random.Random(args.order_seed)
    rng.shuffle(cases)
    t0 = time.monotonic()
    for i, (mode, word, seed) in enumerate(cases):
        arms = ["timer", "ready"]
        rng.shuffle(arms)
        for arm in arms:
            out_rows.append(replay_row(executor, templates[mode][arm], fixture, arm, mode, word, seed))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(cases)} cases, {time.monotonic() - t0:.0f} s", flush=True)
    with open(args.rows_out, "w") as fh:
        json.dump({"meta": meta, "rows": out_rows}, fh, indent=1, default=str)
    return 0


def child_heal(args) -> int:
    """One heal run: compile from fresh recordings, change the API shape, call the tool."""
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    fixture = Fixture(args.fixture_origin, os.path.join(args.cert_dir, "cert.pem"))
    fixture.shape("original")
    rec_dir = os.path.join(args.run_dir, "recordings")
    os.makedirs(rec_dir, exist_ok=True)
    for w, tag in zip(B.RECORDED, ("rec", "fresh1", "fresh2")):
        with open(os.path.join(rec_dir, f"{WORKFLOW}.{tag}.json"), "w") as fh:
            json.dump(fixture.record(w, "xhr", tag), fh, indent=1)
    compiled = subprocess.run([sys.executable, "-P", "-m", "anyuse", "workflow", "compile", WORKFLOW,
                               "--recording", rec_dir, "--yes"],
                              capture_output=True, text=True, timeout=600)
    row: Dict[str, Any] = {"arm": args.arm, "run": args.run_index, "word": args.word,
                           "compile_rc": compiled.returncode,
                           "compile_tail": (compiled.stdout + compiled.stderr)[-600:]}
    if compiled.returncode:
        with open(args.rows_out, "w") as fh:
            json.dump(row, fh, indent=1)
        return 0
    for w in B.WORDS:
        fixture.plan(w, "xhr", args.seed)
    fixture.shape("changed")
    driver = counting_driver(args.cdp_endpoint)
    t0 = time.monotonic()
    out = run_compiled(WORKFLOW, {"q": args.word}, browser_executor=ReplayExecutor(driver=driver))
    wall = (time.monotonic() - t0) * 1000.0
    fixture.shape("original")
    rec = out.get("recompiled") or {}
    items = (out.get("result") or {}).get("items")
    row.update({"ok": bool(out.get("ok")), "served_by": out.get("served_by") or "",
                "right": items == B.full_list(args.word, "xhr"),
                "swapped": bool(rec.get("swapped")), "recompile_reason": str(rec.get("reason") or "")[:200],
                "wall_ms": round(wall, 1), "pages": len(driver.opened), "opened": driver.opened,
                "error": str(out.get("error") or "")[:200]})
    with open(args.rows_out, "w") as fh:
        json.dump(row, fh, indent=1, default=str)
    return 0


# ---------------------------------------------------------------------------
# the parent: environment, preflight, children
# ---------------------------------------------------------------------------

def bench_env(args, data_dir: str, readiness: Optional[bool] = None) -> Dict[str, str]:
    tree = os.path.abspath(args.tree)
    pkgroot = os.path.join(os.path.abspath(args.out), "pkgroot")
    os.makedirs(pkgroot, exist_ok=True)
    link = os.path.join(pkgroot, "anyuse")
    if not os.path.islink(link):
        os.symlink(tree, link)
    home = os.path.join(data_dir, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ANYUSE_") and not k.endswith(("_API_KEY", "_TOKEN"))}
    env.update({
        "PYTHONPATH": os.pathsep.join([pkgroot, tree]), "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
        "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
        "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", "api_hosts.json"),
        "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", "blocked_hosts.json"),
        "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", "robots.json"),
        "ANYUSE_CDP_ENDPOINT": args.cdp_endpoint,
        "ANYUSE_LOCAL_FIXTURE_ORIGINS": args.fixture_origin,
        "SSL_CERT_FILE": os.path.join(os.path.abspath(args.cert_dir), "cert.pem"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
    if readiness is not None:
        env["ANYUSE_TIER2_READINESS"] = "on" if readiness else "off"
    return env


def preflight(args) -> Dict[str, Any]:
    cafile = os.path.join(args.cert_dir, "cert.pem")
    if not os.path.exists(cafile):
        raise SystemExit(f"no {cafile}: run the cert step for {args.fixture_origin} first")
    with urllib.request.urlopen(args.cdp_endpoint.rstrip("/") + "/json/version", timeout=10) as r:
        version = json.loads(r.read().decode())
    fixture = Fixture(args.fixture_origin, cafile)
    robots = fixture.get("/robots.txt")["body"]
    return {"browser": version.get("Browser") or version.get("User-Agent", ""),
            "fixture_robots": robots.strip().splitlines()[:2]}


def run(args) -> int:
    args.cdp_endpoint = checked_origin(args.cdp_endpoint, "http|https", "--cdp-endpoint")[0]
    args.fixture_origin = checked_origin(args.fixture_origin, "https", "--fixture-origin")[0]
    for m in args.modes.split(","):
        if m and m not in LIVE_MODES:
            raise SystemExit(f"--modes: {m!r} is not one of {LIVE_MODES}")
    out = os.path.abspath(args.out)
    tree = os.path.abspath(args.tree)
    if out.startswith(tree + os.sep):
        raise SystemExit("--out must be outside the tree")
    os.makedirs(out, exist_ok=True)
    pre = preflight(args)
    print(f"browser: {pre['browser']}; fixture: {pre['fixture_robots']}")
    commit = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    meta = {"network": args.network, "cdp_endpoint": args.cdp_endpoint,
            "fixture_origin": args.fixture_origin, "tree": tree, "commit": commit,
            "preflight": pre, "seeds": args.seeds, "modes": args.modes,
            "started": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    me = os.path.abspath(__file__)
    common = ["--cdp-endpoint", args.cdp_endpoint, "--fixture-origin", args.fixture_origin,
              "--cert-dir", os.path.abspath(args.cert_dir)]
    rows: List[Dict[str, Any]] = []
    if args.modes:
        path = os.path.join(out, "replays.json")
        proc = subprocess.run([sys.executable, me, "_replays", *common, "--modes", args.modes,
                               "--seeds", str(args.seeds), "--order-seed", str(args.order_seed),
                               "--rows-out", path],
                              env=bench_env(args, os.path.join(out, "replays")), cwd=out)
        if proc.returncode:
            return proc.returncode
        with open(path) as fh:
            data = json.load(fh)
        rows.extend(data["rows"])
        meta["templates"] = data["meta"]["templates"]
    heal: List[Dict[str, Any]] = []
    order = random.Random(args.order_seed + 1)
    for i in range(args.heal_runs):
        arms = ["timer", "ready"]
        order.shuffle(arms)
        for arm in arms:
            run_dir = os.path.join(out, "heal", f"{arm}-{i:02d}")
            os.makedirs(run_dir, exist_ok=True)
            path = os.path.join(run_dir, "row.json")
            word = HEAL_WORDS[i % len(HEAL_WORDS)]
            proc = subprocess.run([sys.executable, me, "_heal", *common, "--arm", arm,
                                   "--run-index", str(i), "--word", word, "--seed", str(i),
                                   "--run-dir", run_dir, "--rows-out", path],
                                  env=bench_env(args, run_dir, readiness=(arm == "ready")), cwd=run_dir)
            if proc.returncode:
                return proc.returncode
            with open(path) as fh:
                heal.append(json.load(fh))
            h = heal[-1]
            print(f"  heal {arm} {i + 1}/{args.heal_runs}: compile rc {h.get('compile_rc')}, "
                  f"ok {h.get('ok')}, swapped {h.get('swapped')}, {h.get('wall_ms')} ms, "
                  f"{h.get('pages')} page(s)", flush=True)
    meta["ended"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    rows_path = os.path.join(out, "rows.json")
    with open(rows_path, "w") as fh:
        json.dump({"meta": meta, "rows": rows, "heal": heal}, fh, indent=1, default=str)
    text = compare(rows_path)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    return 0


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

def compare(path: str) -> str:
    with open(path) as fh:
        data = json.load(fh)
    meta, rows, heal = data["meta"], data["rows"], data.get("heal") or []
    lines = [f"network {meta['network']}; browser {meta['cdp_endpoint']} "
             f"({meta['preflight'].get('browser')}); fixture {meta['fixture_origin']}; "
             f"tree {meta['commit'][:12]}; {meta['started']} -> {meta.get('ended')}"]
    for mode, t in (meta.get("templates") or {}).items():
        lines.append(f"  {mode}: " + "; ".join(
            f"{a}: floor {v['min_items']}, ready floor {(v['readiness'] or {}).get('min_items', '-')}, "
            f"counts {(v['readiness'] or {}).get('explaining_min', '-')}" for a, v in t.items()))
    checks: Dict[str, bool] = {}
    modes = sorted({r["mode"] for r in rows}, key=list(LIVE_MODES).index)
    stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for mode in modes:
        lines.append("")
        lines.append(f"{mode}")
        lines.append(f"  {'arm':<6} {'n':>4} {'early':>6} {'wrong-ok':>9} {'fail':>5} {'infra':>6} "
                     f"{'median ms':>10} {'p95 ms':>8}  settle")
        for arm in ("timer", "ready"):
            sel = [r for r in rows if r["mode"] == mode and r["arm"] == arm]
            usable = [r for r in sel if not r["infrastructure"]]
            times = [r["elapsed_ms"] for r in usable]
            how: Dict[str, int] = {}
            for r in sel:
                how[r["how"] or "-"] = how.get(r["how"] or "-", 0) + 1
            x = {"n": len(sel), "early": sum(bool(r["early"]) for r in usable),
                 "wrong": sum(r["wrong_success"] for r in usable),
                 "fail": sum(not r["success"] for r in usable),
                 "infra": len(sel) - len(usable),
                 "median": statistics.median(times) if times else 0.0, "p95": B.p95(times)}
            stats[(mode, arm)] = x
            lines.append(f"  {arm:<6} {x['n']:>4} {x['early']:>6} {x['wrong']:>9} {x['fail']:>5} "
                         f"{x['infra']:>6} {x['median']:>10.0f} {x['p95']:>8.0f}  "
                         + ", ".join(f"{k} {v}" for k, v in sorted(how.items())))
        r_, t_ = stats[(mode, "ready")], stats[(mode, "timer")]
        checks[f"{mode}: 0 wrong successes with readiness"] = r_["wrong"] == 0
        checks[f"{mode}: early reads no higher than the timer"] = r_["early"] <= t_["early"]
        checks[f"{mode}: p95 at most the timer's + 500 ms"] = r_["p95"] <= t_["p95"] + 500.0
        if mode == "xhr":
            checks["xhr: median at most 40% of the timer's"] = r_["median"] <= 0.4 * t_["median"]
    if heal:
        lines.append("")
        lines.append(f"heal  {'arm':<6} {'runs':>4} {'compiled':>8} {'ok':>4} {'right':>5} "
                     f"{'swapped':>7} {'median wall ms':>14} {'pages':>6}")
        hs: Dict[str, Dict[str, Any]] = {}
        for arm in ("timer", "ready"):
            sel = [h for h in heal if h["arm"] == arm]
            done = [h for h in sel if h.get("compile_rc") == 0]
            walls = [h["wall_ms"] for h in done if "wall_ms" in h]
            hs[arm] = {"runs": len(sel), "compiled": len(done),
                       "ok": sum(bool(h.get("ok")) for h in done),
                       "right": sum(bool(h.get("right")) for h in done),
                       "swapped": sum(bool(h.get("swapped")) for h in done),
                       "median": statistics.median(walls) if walls else 0.0,
                       "pages": statistics.median([h.get("pages", 0) for h in done]) if done else 0}
            x = hs[arm]
            lines.append(f"      {arm:<6} {x['runs']:>4} {x['compiled']:>8} {x['ok']:>4} {x['right']:>5} "
                         f"{x['swapped']:>7} {x['median']:>14.0f} {x['pages']:>6}")
        checks["heal: the same swap rate in both arms"] = (
            hs["timer"]["compiled"] > 0 and hs["ready"]["compiled"] > 0 and
            hs["timer"]["swapped"] * hs["ready"]["compiled"] == hs["ready"]["swapped"] * hs["timer"]["compiled"])
        checks["heal: lower median wall time with readiness"] = hs["ready"]["median"] < hs["timer"]["median"]
        for h in heal:
            if h.get("compile_rc"):
                lines.append(f"  compile failed ({h['arm']} run {h['run']}): {h.get('compile_tail', '')[-200:]!r}")
    lines.append("")
    for k, ok in checks.items():
        lines.append(f"{'PASS' if ok else 'FAIL'}  {k}")
    lines.append(f"pass criterion: {'holds' if checks and all(checks.values()) else 'does NOT hold'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("containers", help="print the two container commands (runs nothing)")
    c.add_argument("--network", required=True, help="the host-only container network to use")
    c.add_argument("--bench-dir", required=True, help="this directory, mounted read-only")
    c.add_argument("--cert-dir", required=True, help="where the cert step writes, mounted read-only")
    c.add_argument("--port", type=int, default=8443, help="the fixture's port inside its container")

    k = sub.add_parser("cert", help="write cert.pem / key.pem for the fixture's address")
    k.add_argument("--fixture-origin", required=True)
    k.add_argument("--cert-dir", required=True)

    f = sub.add_parser("fixture", help="the fixture site (inside the fixture container)")
    f.add_argument("--host", required=True, help="address to bind inside the container")
    f.add_argument("--port", type=int, required=True)
    f.add_argument("--cert-dir", required=True)
    f.add_argument("--wait-cert", type=float, default=600.0)

    r = sub.add_parser("run", help="the bench")
    r.add_argument("--network", required=True, help="recorded with the results")
    r.add_argument("--cdp-endpoint", required=True, help="http://CHROME_IP:9222")
    r.add_argument("--fixture-origin", required=True, help="https://FIXTURE_IP:PORT")
    r.add_argument("--cert-dir", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--tree", default=os.path.dirname(os.path.dirname(HERE)),
                   help="the AnyUse checkout to run (default: this script's)")
    r.add_argument("--modes", default="xhr,lazy", help="comma-separated; empty skips part 1")
    r.add_argument("--seeds", type=int, default=B.SEEDS)
    r.add_argument("--heal-runs", type=int, default=10)
    r.add_argument("--order-seed", type=int, default=7)

    for name in ("_replays", "_heal"):
        x = sub.add_parser(name)
        x.add_argument("--cdp-endpoint", required=True)
        x.add_argument("--fixture-origin", required=True)
        x.add_argument("--cert-dir", required=True)
        x.add_argument("--rows-out", required=True)
        if name == "_replays":
            x.add_argument("--modes", required=True)
            x.add_argument("--seeds", type=int, required=True)
            x.add_argument("--order-seed", type=int, required=True)
        else:
            for a in ("--arm", "--word", "--run-dir"):
                x.add_argument(a, required=True)
            x.add_argument("--run-index", type=int, required=True)
            x.add_argument("--seed", type=int, required=True)

    p = sub.add_parser("compare")
    p.add_argument("rows")
    args = ap.parse_args(argv)

    if args.cmd == "containers":
        for cmd in container_commands(args.network, args.bench_dir, args.cert_dir, args.port):
            print(shlex.join(cmd))
        print("# then read both addresses with `container ls`; they change on restart")
        return 0
    if args.cmd == "cert":
        _origin, host, _port = checked_origin(args.fixture_origin, "https", "--fixture-origin")
        cert, key = make_cert(args.cert_dir, host)
        print(f"wrote {cert} and {key} for {host}")
        return 0
    if args.cmd == "fixture":
        return serve_fixture(args)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "_replays":
        return child_replays(args)
    if args.cmd == "_heal":
        return child_heal(args)
    print(compare(args.rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
