"""A/B bench: does a compiled tool heal without a browser when its site changes?

The site is the offline quickstart's fixture (examples/quickstart/fixture_site.py),
served over HTTPS on 127.0.0.1 from a thread that counts every request per path
(robots.txt apart) and switches its shape in place. Per case: record three inputs
with examples/quickstart/record_fixture.py, compile with ``python -m anyuse workflow
compile ... --yes``, optionally warm the tool, switch the shape, then make 10 calls with
inputs nobody recorded through ``mcpserver.compiled_tools.run_compiled`` (what the MCP
tool calls), with no browser and no model configured. Gold is ``fixture_site.search``.

Usage::

    python tests/bench/heal_bench.py run --old <tree> --new <tree> [--python <py>] [--out <dir>]
    python tests/bench/heal_bench.py fresh --old <tree> --new <tree> [--python <py>] [--out <dir>]

``fresh`` runs the offline cases further down (lists that change between reads, a request
that answers 503 forever while the site's page answers, a heal followed by a failure, kept
evidence): every site is a fake transport inside the arm's process, and no socket is
opened. Each arm writes ``FRESH_OLD.json`` / ``FRESH_NEW.json``; ``fresh-compare`` prints the
table and the pass criteria (no wrong swap in any arm; the check model asked only in the
heal scope and only where a fake model is configured).

``--old`` is a checkout at the commit the feature branch forked from (read only:
bytecode writes are off), ``--new`` the feature checkout. Both arms load the fixture
and the recorder from ``--new``, so the site is identical. Each arm runs as a
subprocess whose PYTHONPATH is a pkgroot directory (in ``--out``) holding an
``anyuse`` symlink to its tree; every AnyUse path points into ``--out``. The arms write
``OLD.json`` and ``NEW.json``; ``compare`` prints the table and the pass criteria.

Cases (compiled under ``original`` unless named, then switched):

* ``a``: JSON tool, warmed (``play maple`` without fall-back, then a call for
  ``harbor``: the anchor becomes ``harbor``), switched to ``changed``.
* ``a2``: the same, cold (the anchor is the compile-time one).
* ``b``: recorded ``--page-only``, so the tool reads the page's HTML; switched to
  ``html_renamed``.
* ``c``: JSON tool, switched to ``api_gone`` (the API answers 410, the page still
  lists the results): the full recompile from the pages.
* ``n1``: compiled under ``js_only`` (the server sends an empty list, the page's script
  fills it), switched to ``js_only_changed``: refused by the compile-time gate.
* ``n2``: switched to ``changed_lying_page`` (the API moved, the page lists other
  packages): refused by the page check (none of the page's entries is in the API's
  own answer).
* ``n3``: switched to ``gone`` (the data is removed): nothing to heal from.
"""
from __future__ import annotations

import argparse
import importlib.util
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
from typing import Any, Dict, List, Optional

DRIFTED = ["amber", "birch", "cedar", "delta", "ember", "falcon", "tulip", "core", "sdk", "tools"]
NAME = "fixture_search"
CASES = [
    # (id, compile shape, drifted shape, page-only recording, warm, expect a heal)
    ("a", "original", "changed", False, True, True),
    ("a2", "original", "changed", False, False, True),
    ("b", "original", "html_renamed", True, False, True),
    ("c", "original", "api_gone", False, False, True),
    ("n1", "js_only", "js_only_changed", False, False, False),
    ("n2", "original", "changed_lying_page", False, False, False),
    ("n3", "original", "gone", False, False, False),
]
MODEL_ENV = ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
             "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY",
             "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

class Site:
    """The fixture in a thread on a free loopback port, counting requests per path."""

    def __init__(self, fx, certdir: str, shape: str):
        cert, key = fx.make_cert(certdir)
        self.lock = threading.Lock()
        self.counts: Dict[str, int] = {}
        site = self

        class Handler(fx.Handler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with site.lock:
                    site.counts[path] = site.counts.get(path, 0) + 1
                return super().do_GET()
        Handler.shape = shape
        self.handler = Handler
        self.server = fx.LoopbackServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def take(self) -> Dict[str, int]:
        with self.lock:
            out, self.counts = dict(self.counts), {}
        return out

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _env_for(case_dir: str, origin: str, cert: str) -> Dict[str, str]:
    data = os.path.join(case_dir, "data")
    os.makedirs(data, exist_ok=True)
    return {"ANYUSE_DATA_DIR": data, "ANYUSE_SECRETS_DIR": os.path.join(case_dir, "secrets"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(data, "api_hosts.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(data, "api_blocked_hosts.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(data, "robots_cache.json"),
            "ANYUSE_LOCAL_FIXTURE_ORIGINS": origin, "SSL_CERT_FILE": cert}


def _summary(out: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("attempted", "swapped", "rung", "localised", "relearned", "requests",
            "model_calls", "browser", "status", "reason")
    return {k: v for k, v in (out or {}).items() if k in keep}


def run_case(case, fx, rec_mod, fixture_dir: str, out_dir: str, label: str) -> Dict[str, Any]:
    cid, compile_shape, drift_shape, page_only, warm, _expect = case
    from anyuse.execution import api_permission as perm
    from anyuse.execution.api_compile import load_compiled
    from anyuse.mcpserver.compiled_tools import run_compiled
    case_dir = os.path.join(out_dir, f"{label}_{cid}")
    shutil.rmtree(case_dir, ignore_errors=True)
    os.makedirs(case_dir)
    certdir = os.path.join(out_dir, "cert")
    site = Site(fx, certdir, compile_shape)
    origin = f"https://127.0.0.1:{site.port}"
    env = _env_for(case_dir, origin, os.path.join(certdir, "cert.pem"))
    os.environ.update(env)
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    row: Dict[str, Any] = {"case": cid, "arm": label, "compile_shape": compile_shape,
                           "drift_shape": drift_shape, "page_only": page_only, "warm": warm}
    try:
        recdir = os.path.join(case_dir, "recordings")
        rec_mod.main([*(["--page-only"] if page_only else []), origin, recdir, NAME,
                      "river", "stone", "cloud"])
        site.take()
        proc = subprocess.run([sys.executable, "-B", "-m", "anyuse", "workflow", "compile", NAME,
                               "--recording", recdir, "--yes"], cwd=case_dir,
                              env={**os.environ}, capture_output=True, text=True, timeout=300)
        row["compile_rc"] = proc.returncode
        row["compile_requests"] = site.take()
        if proc.returncode != 0:
            row["compile_output"] = (proc.stdout + proc.stderr)[-2000:]
            return row
        record = load_compiled(_storage(), _wid(NAME)) or {}
        row["compiled"] = {"kind": (record.get("template") or {}).get("extract_kind"),
                           "items": ((record.get("template") or {}).get("extract") or {}).get("items"),
                           "page": record.get("page")}
        if warm:
            w1 = run_compiled(NAME, {"q": "maple"}, cascade=False)
            w2 = run_compiled(NAME, {"q": "harbor"})
            row["warm_ok"] = [bool(w1.get("ok")), bool(w2.get("ok"))]
        site.take()
        normal: List[float] = []
        for _ in range(3):              # what a call costs while nothing is wrong
            t0 = time.time()
            run_compiled(NAME, {"q": "maple"}, cascade=False, record_outcome=False)
            normal.append(time.time() - t0)
        site.take()
        row["normal_call_s"] = round(statistics.median(normal), 4)
        site.handler.shape = drift_shape
        calls = []
        for q in DRIFTED:
            t0 = time.time()
            out = run_compiled(NAME, {"q": q})
            seconds = time.time() - t0
            counts = site.take()
            items = (out.get("result") or {}).get("items") if out.get("ok") else None
            calls.append({
                "q": q, "ok": bool(out.get("ok")), "served_by": out.get("served_by"),
                "items": items, "gold": fx.search(q), "exact": items == fx.search(q),
                "model_calls": int(out.get("model_calls") or 0),
                "healed": _summary(out.get("healed")) if out.get("healed") else None,
                "heal": _summary(out.get("heal")) if out.get("heal") else None,
                "requests": {"api": counts.get("/api/search", 0), "page": counts.get("/search", 0),
                             "robots": counts.get("/robots.txt", 0),
                             "other": sum(v for k, v in counts.items()
                                          if k not in ("/api/search", "/search", "/robots.txt"))},
                "seconds": round(seconds, 4), "error": str(out.get("error") or "")[:200]})
        row["calls"] = calls
        stored = load_compiled(_storage(), _wid(NAME)) or {}
        row["final"] = {"kind": (stored.get("template") or {}).get("extract_kind"),
                        "items": ((stored.get("template") or {}).get("extract") or {}).get("items"),
                        "status": (stored.get("proof") or {}).get("status"),
                        "split_held_out": ((stored.get("proof") or {}).get("split") or {}).get("held_out"),
                        "healed": stored.get("healed")}
        return row
    finally:
        site.stop()


def _storage():
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def _wid(name: str) -> str:
    try:
        wf = _storage().get(name)
        if wf is not None:
            return wf.id
    except Exception:  # noqa: BLE001
        pass
    return name


def run_arm(label: str, fixture_dir: str, out_file: str) -> None:
    out_dir = os.path.dirname(out_file)
    fx = _load(os.path.join(fixture_dir, "fixture_site.py"), "heal_bench_fixture_site")
    rec_mod = _load(os.path.join(fixture_dir, "record_fixture.py"), "heal_bench_record_fixture")
    import anyuse
    rows = {"arm": label, "tree": os.path.dirname(os.path.realpath(anyuse.__file__)), "cases": []}
    for case in CASES:
        rows["cases"].append(run_case(case, fx, rec_mod, fixture_dir, out_dir, label))
        print(f"{label} {case[0]}: done", flush=True)
    with open(out_file, "w") as fh:
        json.dump(rows, fh, indent=1, default=str)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def _metrics(case: Dict[str, Any]) -> Dict[str, Any]:
    calls = case.get("calls") or []
    healing = [c for c in calls if c.get("healed")]
    swaps = [c for c in healing if (c["healed"] or {}).get("swapped")]
    attempts = [c for c in calls if (c.get("healed") or c.get("heal") or {}).get("attempted")]
    final = case.get("final") or {}
    first_attempt = attempts[0] if attempts else None
    total = lambda c: sum((c or {}).get("requests", {}).get(k, 0) for k in ("api", "page", "other"))
    wrong = bool(swaps) and any(c["ok"] and not c["exact"] for c in calls)
    return {
        "compile_rc": case.get("compile_rc"),
        "ok": sum(c["ok"] for c in calls), "exact": sum(c["exact"] for c in calls),
        "explicit_failures": sum((not c["ok"]) and not c.get("items") for c in calls),
        "heals": len(swaps),
        "healed_and_proved": int(bool(swaps) and final.get("status") == "proved"
                                 and bool(final.get("split_held_out"))),
        "rung": (swaps[0]["healed"].get("rung") if swaps else ""),
        "attempts": len(attempts),
        "wrong_heals": int(wrong),
        "model_calls": sum(c["model_calls"] for c in calls),
        "heal_call_requests": total(first_attempt) if first_attempt else 0,
        "heal_call_page_requests": (first_attempt or {}).get("requests", {}).get("page", 0),
        "heal_call_robots": (first_attempt or {}).get("requests", {}).get("robots", 0),
        "heal_call_s": (first_attempt or {}).get("seconds"),
        "normal_call_s": case.get("normal_call_s"),
        "later_requests": sum(total(c) for c in calls[1:]),
        "browser": any(((c.get("healed") or {}).get("browser")) for c in calls),
    }


def compare(files: List[str]) -> str:
    arms = [json.load(open(f)) for f in files]
    lines = []
    head = (f"{'arm':4} {'case':4} {'ok':>3} {'gold':>4} {'fail':>4} {'heal':>4} {'prov':>4} "
            f"{'rung':11} {'att':>3} {'wrong':>5} {'llm':>3} {'req':>3} {'page':>4} "
            f"{'heal s':>7} {'norm s':>7} {'later':>5}")
    lines.append(head)
    verdicts = []
    for arm in arms:
        for case in arm["cases"]:
            m = _metrics(case)
            lines.append(f"{arm['arm']:4} {case['case']:4} {m['ok']:>3} {m['exact']:>4} "
                         f"{m['explicit_failures']:>4} {m['heals']:>4} {m['healed_and_proved']:>4} "
                         f"{m['rung'] or '-':11} {m['attempts']:>3} {m['wrong_heals']:>5} "
                         f"{m['model_calls']:>3} {m['heal_call_requests']:>3} "
                         f"{m['heal_call_page_requests']:>4} "
                         f"{(m['heal_call_s'] if m['heal_call_s'] is not None else 0):>7.3f} "
                         f"{(m['normal_call_s'] or 0):>7.4f} {m['later_requests']:>5}")
            verdicts.append((arm["arm"], case, m))
    lines.append("")
    lines.append("columns: ok / gold = of 10 drifted calls, ok and exactly the fixture's list; fail = "
                 "explicit failures (ok false, no items); heal = swaps; prov = the swap's stored "
                 "proof is proved and held out; att = heal attempts; req / page = requests in the "
                 "first attempt's call (robots.txt apart); later = requests in calls 2-10")
    lines.append("")
    for arm_name, case, m in verdicts:
        expect_heal = next(c[5] for c in CASES if c[0] == case["case"])
        if arm_name == "OLD":
            ok = m["ok"] == 0 and m["attempts"] == 0
            lines.append(f"{'PASS' if ok else 'FAIL'} OLD {case['case']}: 0/10 ok and 0 heal attempts "
                         f"(got {m['ok']}/10, {m['attempts']})")
            continue
        if expect_heal:
            ok = (m["heals"] == 1 and m["healed_and_proved"] == 1 and m["ok"] == 10
                  and m["exact"] == 10 and m["model_calls"] == 0 and not m["browser"]
                  and m["heal_call_requests"] <= 15)
            lines.append(f"{'PASS' if ok else 'FAIL'} NEW {case['case']}: one proved heal ({m['rung']}), "
                         f"10/10 exact, 0 model calls, no browser, <= 15 requests in the healing call "
                         f"(got {m['heals']} heal(s), {m['exact']}/10, {m['model_calls']} model calls, "
                         f"{m['heal_call_requests']} requests)")
        else:
            limit_ok = m["heal_call_requests"] <= 6 and (case["case"] != "n1"
                                                          or m["heal_call_page_requests"] == 0)
            ok = (m["heals"] == 0 and m["explicit_failures"] == 10 and m["attempts"] <= 1
                  and limit_ok)
            lines.append(f"{'PASS' if ok else 'FAIL'} NEW {case['case']}: 0 heals, 10/10 explicit "
                         f"failures, at most one attempt, <= 6 requests in it"
                         + (" and 0 page requests" if case["case"] == "n1" else "")
                         + f" (got {m['heals']}, {m['explicit_failures']}/10, {m['attempts']} "
                           f"attempt(s), {m['heal_call_requests']} requests, "
                           f"{m['heal_call_page_requests']} page)")
    wrong = sum(m["wrong_heals"] for a, _c, m in verdicts if a == "NEW")
    lines.append(f"{'PASS' if wrong == 0 else 'FAIL'} NEW: 0 wrong heals overall (got {wrong})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-heal-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    fixture_dir = os.path.join(trees["NEW"], "examples", "quickstart")
    me = os.path.join(out, "heal_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(home, exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ANYUSE_") and k not in MODEL_ENV}
        env.update({"PYTHONPATH": pkgroot, "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
                    "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                    "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, "-B", me, "arm", "--label", label,
                               "--fixture", fixture_dir, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-call rows: {', '.join(results)}")
    return 0


# ---------------------------------------------------------------------------
# fresh-truth cases: offline fake sites, in-process, OLD vs NEW
# ---------------------------------------------------------------------------
#
# ``fresh --old <tree> --new <tree> [--out <dir>]`` runs each arm in a subprocess whose
# PYTHONPATH is a pkgroot directory (in ``--out``) with an ``anyuse`` symlink to its tree.
# Every site is a fake transport in the arm's own process: no socket is opened. The cases:
#
# * lists that change between reads, which the old 80 % rule refused: ``moved`` (the JSON
#   list moved and the prices in the names changed), ``gone`` (the API answers 410, newest
#   entries first and new prices on the page), ``statement`` (as ``gone``, and a made-up
#   search fills the list region with a popular list under "No results for X"), ``model``
#   (as ``gone``, and a made-up search fills the region with no statement; a fake check
#   model is configured); ``nomodel`` (the same without a model) and ``featured`` (the
#   page lists another word's entries for every input; a fake model says yes to every
#   line) must not heal;
# * ``down503``: the API answers 503 forever while the page answers (calls 10 minutes
#   apart); ``down_all``: every path answers 503 for 5 hours, which must never take the
#   request out;
# * ``heal_then_fail``: a heal puts a demoted request back in service, then the site
#   removes the data: full heals counted; ``swap_then_fail``: a heal swaps, the new request
#   fails at once, a failing agent rung: agent runs counted;
# * ``kept_har``: a HAR-compiled tool, one changed answer, the agent answers: a recompile
#   from the kept recordings; ``kept_agent``: a tool only the agent answers after its API
#   moved: a recompile from the agent's kept pages.

FRESH_WORDS = ["amber", "birch", "cedar", "cloud", "delta", "ember", "falcon", "harbor",
               "maple", "river", "stone", "tulip"]
FRESH_SUFFIXES = ["core", "cli", "kit", "lite", "sdk", "tools"]
FRESH_CALLS = ["amber", "birch", "cedar", "delta", "ember", "falcon", "harbor", "maple"]
FRESH_HOST = "feed.test"
FRESH_NAME = "feed_search"
FRESH_CASES = ["moved", "gone", "statement", "model", "nomodel", "featured", "down503",
               "down_all", "heal_then_fail", "swap_then_fail", "kept_har", "kept_agent"]
#: the cases that must heal or recompile in the NEW arm
FRESH_HEALS = {"moved", "gone", "statement", "model", "down503", "kept_har", "kept_agent"}
#: the cases where a swap may happen (``swap_then_fail`` starts with one)
FRESH_MAY_SWAP = FRESH_HEALS | {"swap_then_fail"}


class Feed:
    """A server-rendered result page and a JSON API (as tests/test_heal_fresh_truth.py)."""

    def __init__(self):
        self.epoch, self.churn, self.api, self.page, self.down_all = 0, False, "results", "list", False
        self.log: List[Any] = []

    def names(self, q: str) -> List[str]:
        q = q.strip().lower()
        if q not in FRESH_WORDS:
            return []
        out = [f"{q}-{s} {10 + i + self.epoch} EUR" for i, s in enumerate(FRESH_SUFFIXES)]
        if self.churn:
            out = [f"{q}-new{e}{k} {4 + e} EUR" for e in range(self.epoch, 0, -1) for k in "ab"] + out
        return out[:10]

    def _other(self, q: str) -> str:
        other = FRESH_WORDS[(sum(map(ord, q.lower())) + 5) % len(FRESH_WORDS)]
        return other if other != q.lower() else FRESH_WORDS[(FRESH_WORDS.index(other) + 1) % 12]

    def shown(self, q: str):
        if self.page == "featured":
            return "", [f"{n.split(' ')[0]}-pro" for n in self.names(self._other(q))]
        if self.page in ("popular_on_empty", "filler") and not self.names(q):
            popular = [n.split(" ")[0] + "-top" for n in self.names("maple")[:4]]
            return (f"No results for {q}." if self.page == "popular_on_empty" else ""), popular
        if self.page == "notice":
            return "", []
        return "", self.names(q)

    def page_html(self, q: str) -> str:
        import html as _h
        said, entries = self.shown(q)
        rows = "".join(f'<li class="result"><a href="/pkg/{_h.escape(n)}">{_h.escape(n)}</a></li>'
                       for n in entries)
        notice = f"<p class='notice'>{_h.escape(said)}</p>" if said else ""
        if self.page == "notice":
            notice = "<p class='notice'>Search has moved to our app.</p>"
        return (f"<!doctype html><html><head><title>Search results for {_h.escape(q)} - Feed"
                f"</title></head><body><nav><a href='/'>Feed Packages</a></nav>"
                f"<form action='/search'><input name='q' value='{_h.escape(q)}'></form>"
                f"<h2>Latest prices</h2>{notice}<ul id='results'>{rows}</ul>"
                f"<footer>Prices change every day.</footer></body></html>")

    def api_body(self, q: str):
        names = self.names(q)
        if self.api == "gone":
            return 410, {"error": "this API was retired"}
        if self.api == "error503":
            return 503, {"error": "busy"}
        if self.api == "packages":
            return 200, {"total": len(names), "packages": [{"title": n} for n in names]}
        return 200, {"count": len(names), "results": [{"name": n} for n in names]}

    def send(self, method, url, headers, body, timeout_s):
        import urllib.parse
        from anyuse.execution.api_step import ApiResponse
        sp = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
        self.log.append((sp.path, q))
        if self.down_all:
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")
        if sp.path == "/api/search":
            status, payload = self.api_body(q)
            return ApiResponse(status, {"Content-Type": "application/json"}, json.dumps(payload))
        if sp.path in ("/", "/search"):
            return ApiResponse(200, {"Content-Type": "text/html; charset=utf-8"}, self.page_html(q))
        return ApiResponse(404, {"Content-Type": "text/plain"}, "not found")

    def take(self) -> Dict[str, int]:
        out = {"api": sum(1 for p, _q in self.log if p == "/api/search"),
               "page": sum(1 for p, _q in self.log if p in ("/", "/search"))}
        self.log = []
        return out


class _Store:
    def __init__(self, base: str):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id: str) -> str:
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name: str):
        return None


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now


def _feed_recording(feed: Feed, value: str, tag: str) -> Dict[str, Any]:
    page_url = f"https://{FRESH_HOST}/search?q={value}"
    _status, api = feed.api_body(value)
    items = feed.shown(value)[1]
    return {"workflow": FRESH_NAME, "tag": tag, "site": FRESH_HOST,
            "param": {"name": "q", "value": value}, "final_url": page_url,
            "ui": {"items": items, "text": "\n".join(items)},
            "network": [{"url": page_url, "method": "GET", "status": 200, "mime": "text/html",
                         "body": feed.page_html(value)},
                        {"url": f"https://{FRESH_HOST}/api/search?q={value}", "method": "GET",
                         "status": 200, "mime": "application/json", "body": json.dumps(api)}]}


class _Ctx:
    """One case's isolated paths, the fake check model and the record of where it was asked."""

    def __init__(self, case_dir: str):
        from anyuse.execution import api_permission as perm
        from anyuse.execution import model_check as MC
        self.dir = case_dir
        data = os.path.join(case_dir, "data")
        os.makedirs(data, exist_ok=True)
        os.environ.update({"ANYUSE_DATA_DIR": data, "ANYUSE_SECRETS_DIR": data,
                           "ANYUSE_API_HOSTS_FILE": os.path.join(data, "api_hosts.json"),
                           "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(data, "blocked.json"),
                           "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(data, "robots.json"),
                           "ANYUSE_MODEL_CHECKS": "none"})
        perm._default_fetch = lambda url, *a, **k: "User-agent: *\nAllow: /\n"
        perm._ROBOTS_CACHE.clear()
        perm._ROBOTS_NOTES.clear()
        self.hosts = os.environ["ANYUSE_API_HOSTS_FILE"]
        for host in (FRESH_HOST, "ex.test", "crates.io"):
            perm.acknowledge_host(host, path=self.hosts)
        self.storage = _Store(os.path.join(case_dir, "wf"))
        self.asked: List[str] = []
        #: scope -> questions the fake model got from checks made in it
        self.asked_in: Dict[str, int] = {}
        real = getattr(MC, "_bench_real_check", None) or MC.check
        MC._bench_real_check = real
        ctx = self

        def check(*args, **kwargs):
            scope, before = MC.current_scope() or "none", len(ctx.asked)
            try:
                return real(*args, **kwargs)
            finally:
                if len(ctx.asked) > before:
                    ctx.asked_in[scope] = ctx.asked_in.get(scope, 0) + len(ctx.asked) - before
        MC.check = check

    def asker(self, answer):
        def ask(instructions, state, guard_obs):
            self.asked.append(state.rsplit(">> ", 1)[-1])
            return answer(state.rsplit(">> ", 1)[-1])
        return ask


def _call(ctx: _Ctx, feed: Feed, q: str, **kw) -> Dict[str, Any]:
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    return run_compiled(FRESH_NAME, {"q": q}, storage=ctx.storage, hosts_path=ctx.hosts,
                        executor=ApiStepExecutor(transport=feed), **kw)


def _feed_tool(ctx: _Ctx, feed: Feed) -> None:
    from anyuse.execution.api_compile import compiled_record, persist_compiled, with_runtime_rungs
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    recs = [_feed_recording(feed, v, t) for v, t in (("river", "rec"), ("stone", "fresh1"),
                                                     ("cloud", "fresh2"))]
    compiled, proof = compile_and_verify(recs[0], recs[1:], ApiStepExecutor(transport=feed))
    if not proof.passed:
        raise RuntimeError(f"the bench tool did not prove: {proof.reason}")
    record = compiled_record(compiled.template, proof.to_dict(), workflow=FRESH_NAME,
                             source={"site": FRESH_HOST})
    with_runtime_rungs(record, recs[0])
    persist_compiled(ctx.storage, FRESH_NAME, record)
    feed.log = []


def _row(out: Dict[str, Any], gold: List[str], feed: Optional[Feed] = None) -> Dict[str, Any]:
    items = (out.get("result") or {}).get("items") if out.get("ok") else None
    summary = out.get("healed") or out.get("heal") or {}
    return {"ok": bool(out.get("ok")), "served_by": out.get("served_by"),
            "exact": items == gold, "items": items,
            "swapped": bool((out.get("healed") or {}).get("swapped")
                            or (out.get("recompiled") or {}).get("swapped")),
            "heal_attempted": bool(summary.get("attempted")),
            "restored": bool(out.get("restored")),
            "rung": str((out.get("healed") or {}).get("rung") or ""),
            "truth": str(((out.get("healed") or {}).get("truth") or {}).get("rung") or ""),
            "model_calls": int(out.get("model_calls") or 0),
            "requests": feed.take() if feed is not None else {},
            "error": str(out.get("error") or "")[:160]}


def _churn_case(ctx: _Ctx, case: str) -> Dict[str, Any]:
    from anyuse.execution import model_check as MC
    feed = Feed()
    _feed_tool(ctx, feed)
    feed.epoch = 1
    if case == "moved":
        feed.api = "packages"
    else:
        feed.api, feed.churn = "gone", True
        feed.page = {"gone": "list", "statement": "popular_on_empty", "model": "filler",
                     "nomodel": "filler", "featured": "featured"}[case]
    answer = None
    if case == "model":
        answer = lambda line: 0.95 if " EUR" in line else 0.05            # noqa: E731
    elif case == "featured":
        answer = lambda line: 0.95                                       # noqa: E731
    calls = []
    with MC.using(ctx.asker(answer) if answer else None):
        for q in FRESH_CALLS:
            calls.append(_row(_call(ctx, feed, q), feed.names(q), feed))
    return {"calls": calls, "fake_model": answer is not None}


def _down_case(ctx: _Ctx, case: str) -> Dict[str, Any]:
    from anyuse.execution import api_recompile as R
    feed = Feed()
    _feed_tool(ctx, feed)
    clock = _Clock(1_800_000_000.0)
    real_time = R.time
    R.time = clock
    calls, checks = [], []
    try:
        if case == "down503":
            feed.api = "error503"
            for q in FRESH_CALLS[:6]:
                calls.append(_row(_call(ctx, feed, q), feed.names(q), feed))
                clock.now += 600
        else:
            feed.down_all = True
            for i in range(30):
                row = _row(_call(ctx, feed, "amber"), feed.names("amber"), feed)
                if row["requests"].get("page"):
                    checks.append(clock.now)
                calls.append(row)
                clock.now += 600
    finally:
        R.time = real_time
    from anyuse.execution.api_compile import load_compiled
    health = (load_compiled(ctx.storage, FRESH_NAME) or {}).get("health") or {}
    return {"calls": calls, "site_checks": len(checks),
            "check_spacing_ok": all(b - a >= 1800 for a, b in zip(checks, checks[1:])),
            "preferred": bool(health.get("preferred", True))}


def _heal_then_fail(ctx: _Ctx, case: str) -> Dict[str, Any]:
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_recompile import _at
    feed = Feed()
    _feed_tool(ctx, feed)
    agent_runs: List[str] = []

    def agent(task, **kw):
        agent_runs.append(task)
        return {"ok": False, "outcome": "error", "reason": "the page did not load"}
    calls = []
    if case == "heal_then_fail":
        assert _call(ctx, feed, "harbor")["ok"]
        record = load_compiled(ctx.storage, FRESH_NAME)
        now = time.time()
        record["health"] = {**record["health"], "preferred": False,
                            "demoted_reason": "3 consecutive failures (>= 3); last error: postcondition failed",
                            "last_error": "postcondition failed", "demoted_at": _at(now),
                            "probe_after": _at(now + 600)}
        persist_compiled(ctx.storage, FRESH_NAME, record)
        feed.take()
        calls.append(_row(_call(ctx, feed, "amber"), feed.names("amber"), feed))
        feed.api, feed.page = "gone", "notice"
        for q in FRESH_CALLS[1:5]:
            calls.append(_row(_call(ctx, feed, q), feed.names(q), feed))
    else:
        feed.api = "packages"
        state = {"swapped": False}

        def hook_send(method, url, headers, body, timeout_s, _real=feed.send):
            if "/api/search" in url and (load_compiled(ctx.storage, FRESH_NAME) or {}).get("healed"):
                feed.api = "gone"
                state["swapped"] = True
            return _real(method, url, headers, body, timeout_s)
        feed.send = hook_send
        calls.append(_row(_call(ctx, feed, "amber", agent_runner=agent), feed.names("amber"), feed))
    return {"calls": calls, "full_heals": sum(1 for c in calls if c["heal_attempted"]
                                              or c["swapped"] or c["restored"]),
            "agent_runs": len(agent_runs)}


def _kept_har(ctx: _Ctx, case: str) -> Dict[str, Any]:
    import urllib.parse
    from click.testing import CliRunner
    from anyuse.cli import cli
    from anyuse.execution import har_import as HI
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    sets = {"alpha": ["Alpha Pkg", "Beta Pkg", "Gamma Pkg", "Delta Pkg"],
            "beta": ["Beta One", "Beta Two", "Beta Three"],
            "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}

    def body(q):
        return json.dumps({"results": [{"name": n} for n in sets[q]]})

    class Api:
        flaky = 0

        def send(self, method, url, headers, data, timeout_s):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
            if self.flaky > 0:
                self.flaky -= 1
                return ApiResponse(200, {"Content-Type": "application/json"}, '{"changed": true}')
            return ApiResponse(200, {"Content-Type": "application/json"}, body(q))
    api = Api()
    real = HI.compile_from_har
    HI.compile_from_har = lambda cases, **kw: real(cases, executor=ApiStepExecutor(transport=api), **kw)
    har = {"log": {"version": "1.2", "creator": {"name": "DevTools", "version": "1"}, "entries": [{
        "startedDateTime": "2026-09-21T00:00:00+00:00", "time": 1,
        "request": {"method": "GET", "url": "https://ex.test/api?q=alpha", "cookies": [],
                    "headers": [{"name": "Accept", "value": "application/json"}], "queryString": []},
        "response": {"status": 200, "statusText": "OK",
                     "headers": [{"name": "content-type", "value": "application/json"}],
                     "content": {"size": len(body("alpha")), "mimeType": "application/json",
                                 "text": body("alpha")}, "redirectURL": ""},
        "cache": {}, "timings": {}}]}}
    path = os.path.join(ctx.dir, "alpha.har")
    with open(path, "w") as fh:
        json.dump(har, fh)
    try:
        res = CliRunner().invoke(cli, ["workflow", "compile", "ex_search", "--yes", "--from-har", path,
                                       "--har-param", "q=alpha", "--har-expect", "|".join(sets["alpha"]),
                                       "--expect", "beta=" + "|".join(sets["beta"]),
                                       "--expect", "gamma=" + "|".join(sets["gamma"])])
    finally:
        HI.compile_from_har = real
    if "Persisted" not in res.output:
        return {"calls": [], "error": res.output[-400:]}
    api.flaky = 1
    out = run_compiled("ex_search", {"q": "alpha"}, hosts_path=ctx.hosts,
                       executor=ApiStepExecutor(transport=api),
                       agent_runner=lambda task, **kw: {"ok": True, "result": sets["alpha"]})
    row = _row(out, sets["alpha"])
    row["recompile_from"] = str((out.get("recompiled") or {}).get("from") or "")
    row["recompile_reason"] = str((out.get("recompiled") or {}).get("reason") or "")[:200]
    after = run_compiled("ex_search", {"q": "beta"}, hosts_path=ctx.hosts,
                         executor=ApiStepExecutor(transport=api),
                         agent_runner=lambda task, **kw: {"ok": True, "result": sets["beta"]})
    return {"calls": [row, _row(after, sets["beta"])]}


def _kept_agent(ctx: _Ctx, case: str, testdata: str) -> Dict[str, Any]:
    import urllib.parse
    from anyuse.execution import attach as A
    from anyuse.execution.api_compile import compiled_record, persist_compiled, with_runtime_rungs
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    folder = os.path.join(testdata, "attach_crates")
    sessions = [json.load(open(os.path.join(folder, n))) for n in sorted(os.listdir(folder))]
    captured: Dict[str, str] = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]

    def moved(text: str) -> str:
        return json.dumps({"data": {"packages": [{"title": c["name"], "summary": c.get("description")}
                                                 for c in json.loads(text)["crates"]]}})

    class Crates:
        def __init__(self, changed: bool):
            self.changed = changed

        def send(self, method, url, headers, data, timeout_s):
            sp = urllib.parse.urlsplit(url)
            q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
            if sp.path.endswith("/v2/crates"):
                return ApiResponse(200, {"Content-Type": "application/json"}, moved(captured[q]))
            text = '{"results": []}' if self.changed else captured[q]
            return ApiResponse(200, {"Content-Type": "application/json"}, text)
    compiled, proof, _report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=Crates(False)), preflight=lambda c: "")
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    persist_compiled(ctx.storage, "crates_search", record)
    site = Crates(True)

    def agent(task, **kw):
        q = kw["values"]["q"]
        names = [c["name"] for c in json.loads(captured[q])["crates"]][:10]
        dom = "<html><body><ul>" + "".join(f"<li>{n}</li>" for n in names) + "</ul></body></html>"
        return {"ok": True, "outcome": "done", "result": names, "model_calls": 2,
                "evidence": {"final_url": f"https://crates.io/search?q={q}", "dom": dom,
                             "text": "\n".join(names),
                             "network": [{"url": f"https://crates.io/api/v2/crates?q={q}&per_page=10",
                                          "method": "GET", "status": 200, "mime": "application/json",
                                          "body": moved(captured[q]), "resource_type": "Fetch"}]}}
    calls = []
    for q in list(captured):
        out = run_compiled("crates_search", {"q": q}, storage=ctx.storage, hosts_path=ctx.hosts,
                           executor=ApiStepExecutor(transport=site), agent_runner=agent)
        gold = [c["name"] for c in json.loads(captured[q])["crates"]][:10]
        row = _row(out, gold)
        row["recompile_from"] = str((out.get("recompiled") or {}).get("from") or "")
        calls.append(row)
    q = list(captured)[0]
    out = run_compiled("crates_search", {"q": q}, storage=ctx.storage, hosts_path=ctx.hosts,
                       executor=ApiStepExecutor(transport=site), agent_runner=agent)
    gold = [c["name"] for c in json.loads(captured[q])["crates"]][:10]
    calls.append(_row(out, gold))
    return {"calls": calls}


def fresh_arm(label: str, out_file: str, testdata: str) -> None:
    import anyuse
    out_dir = os.path.dirname(out_file)
    rows = {"arm": label, "tree": os.path.dirname(os.path.realpath(anyuse.__file__)), "cases": {}}
    for case in FRESH_CASES:
        case_dir = os.path.join(out_dir, f"fresh_{label}_{case}")
        shutil.rmtree(case_dir, ignore_errors=True)
        os.makedirs(case_dir)
        ctx = _Ctx(case_dir)
        t0 = time.time()
        try:
            if case in ("moved", "gone", "statement", "model", "nomodel", "featured"):
                got = _churn_case(ctx, case)
            elif case in ("down503", "down_all"):
                got = _down_case(ctx, case)
            elif case in ("heal_then_fail", "swap_then_fail"):
                got = _heal_then_fail(ctx, case)
            elif case == "kept_har":
                got = _kept_har(ctx, case)
            else:
                got = _kept_agent(ctx, case, testdata)
        except Exception as exc:  # noqa: BLE001 - a case that crashes is reported, not hidden
            got = {"calls": [], "error": f"{type(exc).__name__}: {exc}"[:400]}
        got.update({"asked": len(ctx.asked), "asked_in": dict(ctx.asked_in),
                    "asked_outside_heal": sum(n for sc, n in ctx.asked_in.items() if sc != "heal"),
                    "seconds": round(time.time() - t0, 3)})
        rows["cases"][case] = got
        print(f"{label} {case}: done", flush=True)
    with open(out_file, "w") as fh:
        json.dump(rows, fh, indent=1, default=str)


def _fresh_metrics(case: str, got: Dict[str, Any]) -> Dict[str, Any]:
    calls = got.get("calls") or []
    swaps = [i for i, c in enumerate(calls) if c.get("swapped")]
    # an answer that is not the site's list now, and a swap where none may happen
    wrong = sum(1 for c in calls if c["ok"] and not c["exact"]) \
        + (0 if case in FRESH_MAY_SWAP else len(swaps))
    return {"calls": len(calls), "ok": sum(c["ok"] for c in calls),
            "exact": sum(c["ok"] and c["exact"] for c in calls), "swaps": len(swaps),
            "wrong": wrong, "model_calls": sum(c.get("model_calls") or 0 for c in calls),
            "asked": got.get("asked", 0), "outside_heal": got.get("asked_outside_heal", 0),
            "rung": next((c["truth"] or c["rung"] for c in calls if c.get("swapped")), ""),
            "full_heals": got.get("full_heals"), "agent_runs": got.get("agent_runs"),
            "site_checks": got.get("site_checks"), "spacing_ok": got.get("check_spacing_ok"),
            "preferred": got.get("preferred"), "error": got.get("error", ""),
            "from": next((c.get("recompile_from") for c in calls if c.get("recompile_from")), "")}


def fresh_compare(files: List[str]) -> str:
    arms = {a["arm"]: a for a in (json.load(open(f)) for f in files)}
    lines = [f"{'arm':4} {'case':15} {'calls':>5} {'ok':>3} {'gold':>4} {'swap':>4} {'wrong':>5} "
             f"{'llm':>3} {'asked':>5} {'rung / from':28} {'heals':>5} {'agent':>5} {'checks':>6}"]
    m: Dict[str, Dict[str, Any]] = {}
    for label in ("OLD", "NEW"):
        for case in FRESH_CASES:
            got = (arms.get(label) or {}).get("cases", {}).get(case) or {}
            x = _fresh_metrics(case, got)
            m[f"{label}:{case}"] = x
            lines.append(f"{label:4} {case:15} {x['calls']:>5} {x['ok']:>3} {x['exact']:>4} "
                         f"{x['swaps']:>4} {x['wrong']:>5} {x['model_calls']:>3} {x['asked']:>5} "
                         f"{(x['rung'] or x['from'] or '-')[:28]:28} "
                         f"{'-' if x['full_heals'] is None else x['full_heals']:>5} "
                         f"{'-' if x['agent_runs'] is None else x['agent_runs']:>5} "
                         f"{'-' if x['site_checks'] is None else x['site_checks']:>6}"
                         + (f"  ERROR {x['error'][:120]}" if x["error"] else ""))
    lines.append("")
    lines.append("columns: ok / gold = calls answered / answered with exactly the site's list now; "
                 "swap = heals or recompiles that swapped a request in; wrong = answers that were "
                 "not the site's list after a swap, and swaps where none may happen; llm = model "
                 "calls the results report; asked = questions the fake check model got; heals = "
                 "heal attempts and restores; agent = agent-rung runs; checks = site-check page reads")
    lines.append("")

    def verdict(ok: bool, text: str) -> None:
        lines.append(f"{'PASS' if ok else 'FAIL'} {text}")
    wrong = sum(x["wrong"] for x in m.values())
    verdict(wrong == 0, f"no wrong swap in any arm (got {wrong})")
    outside = sum(x["outside_heal"] for x in m.values())
    verdict(outside == 0, f"the check model is asked only inside the heal scope (got {outside} outside)")
    unasked = [k for k, x in m.items() if x["asked"] and k.split(":")[1] not in ("model", "featured")]
    verdict(not unasked, f"the check model is asked only where a fake model is configured "
                         f"(asked in {unasked or 'none'} besides model/featured)")
    for case in ("moved", "gone", "statement", "model"):
        o, n = m[f"OLD:{case}"], m[f"NEW:{case}"]
        verdict(o["exact"] == 0 and n["exact"] == n["calls"] == len(FRESH_CALLS) and n["swaps"] == 1,
                f"{case}: OLD {o['exact']}/{o['calls']} (the 80 % rule refused), NEW "
                f"{n['exact']}/{n['calls']} exact after one swap ({n['rung']})")
    for case in ("model", "featured"):
        # a heal that swapped and one that refused both report the questions it asked
        n = m[f"NEW:{case}"]
        verdict(n["asked"] > 0 and n["model_calls"] == n["asked"],
                f"{case}: the calls report the check model's {n['asked']} question(s) "
                f"({n['model_calls']})")
    for case in ("nomodel", "featured"):
        o, n = m[f"OLD:{case}"], m[f"NEW:{case}"]
        verdict(o["swaps"] == n["swaps"] == 0 and n["ok"] == 0,
                f"{case}: no heal in either arm (OLD {o['swaps']}, NEW {n['swaps']}), NEW answers "
                f"nothing ({n['ok']} ok)")
    o, n = m["OLD:down503"], m["NEW:down503"]
    verdict(o["ok"] == 0 and n["swaps"] == 1 and n["exact"] == 3,
            f"down503: OLD {o['ok']}/6 ok; NEW taken out as gone at the 4th call and healed "
            f"({n['rung']}), {n['exact']}/6 exact")
    o, n = m["OLD:down_all"], m["NEW:down_all"]
    verdict(o["swaps"] == n["swaps"] == 0 and n["preferred"] and n["spacing_ok"],
            f"down_all: never taken out (NEW preferred={n['preferred']}), {n['site_checks']} site "
            f"check(s) at least 30 minutes apart")
    o, n = m["OLD:heal_then_fail"], m["NEW:heal_then_fail"]
    verdict(n["full_heals"] < o["full_heals"],
            f"heal_then_fail: full heals OLD {o['full_heals']}, NEW {n['full_heals']}")
    o, n = m["OLD:swap_then_fail"], m["NEW:swap_then_fail"]
    verdict(n["agent_runs"] == 1 and o["agent_runs"] > n["agent_runs"],
            f"swap_then_fail: agent runs OLD {o['agent_runs']}, NEW {n['agent_runs']}")
    for case in ("kept_har", "kept_agent"):
        o, n = m[f"OLD:{case}"], m[f"NEW:{case}"]
        verdict(o["swaps"] == 0 and n["swaps"] == 1 and bool(n["from"]),
                f"{case}: OLD {o['swaps']} recompiles, NEW {n['swaps']} from {n['from'] or '-'}")
    return "\n".join(lines)


def fresh_run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-heal-fresh-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "heal_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    testdata = os.path.join(trees["NEW"], "testdata")
    home = os.path.join(out, "home")
    os.makedirs(home, exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ANYUSE_") and k not in MODEL_ENV}
        env.update({"PYTHONPATH": pkgroot, "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
                    "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                    "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
                    "ANYUSE_MODEL_CHECKS": "none"})
        path = os.path.join(out, f"FRESH_{label}.json")
        proc = subprocess.run([args.python or sys.executable, "-B", me, "fresh-arm", "--label", label,
                               "--out-file", path, "--testdata", testdata], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = fresh_compare(results)
    with open(os.path.join(out, "fresh_summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-call rows: {', '.join(results)}")
    return 0 if "FAIL" not in text else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--fixture", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    f = sub.add_parser("fresh")
    f.add_argument("--old", required=True)
    f.add_argument("--new", required=True)
    f.add_argument("--python", default="")
    f.add_argument("--out", default="")
    fa = sub.add_parser("fresh-arm")
    fa.add_argument("--label", required=True, choices=["OLD", "NEW"])
    fa.add_argument("--out-file", required=True)
    fa.add_argument("--testdata", required=True)
    fc = sub.add_parser("fresh-compare")
    fc.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "fresh":
        return fresh_run(args)
    if args.cmd == "fresh-arm":
        fresh_arm(args.label, os.path.abspath(args.out_file), os.path.abspath(args.testdata))
        return 0
    if args.cmd == "fresh-compare":
        print(fresh_compare(args.files))
        return 0
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.fixture), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
