"""A/B bench: does a compiled tool answer again after its site was down?

The site is the offline quickstart's fixture (examples/quickstart/fixture_site.py),
served over HTTPS on 127.0.0.1 from a thread that counts every request per path
(robots.txt apart, which is always answered). The bench switches what the site does
before each call: ``up`` (the fixture's ``original`` shape), ``down`` (HTTP 503 on every
path), ``down_ra`` (503 with ``Retry-After: 1200``) and ``drift`` (``changed_lying_page``:
the API moved and the page lists other packages, so nothing can heal it).

Per scenario: record three inputs with examples/quickstart/record_fixture.py, compile with
``python -m anyuse workflow compile ... --yes``, warm the tool (``maple`` without fall-back,
then ``harbor``: its answer becomes the last good answer), then make one call per step of
the scenario's timeline through ``mcpserver.compiled_tools.run_compiled`` (what the MCP
tool calls), with no browser and no model configured. Calls are five minutes apart on a
simulated clock: the arm process replaces ``time.time`` before AnyUse is imported, so the
health record, the heal pacing and the waits all see it. Gold is ``fixture_site.search``.

Scenarios:

* ``outage``: 8 calls down (40 minutes), then 24 calls up (2 hours).
* ``outage_ra``: the same, the 503s carrying ``Retry-After: 1200``.
* ``flapping``: 40 calls on a fixed up/down pattern with runs of up to four calls down.
* ``drift``: 30 calls on the drifted site; nothing should be answered and nothing restored.
* ``old_record``: the record as the old code left it after a 503 outage (out of service,
  last error ``non-2xx status 503``), then 24 calls with the site up.
* ``answers_record``: the record out of service after answers that failed its checks (a
  short-lived bad answer), then 24 calls with the site up: the heal finds nothing to
  repair and, in NEW, puts the request back in service.

Usage::

    python tests/bench/outage_bench.py run --old <tree> --new <tree> [--python <py>] [--out <dir>]

``--old`` is a checkout at the commit the branch forked from (read only: bytecode writes
are off), ``--new`` the branch. Both arms load the fixture and the recorder from
``--new``. Each arm runs as a subprocess whose PYTHONPATH is a pkgroot directory (in
``--out``) holding an ``anyuse`` symlink to its tree; every AnyUse path points into
``--out``. The arms write ``OLD.json`` and ``NEW.json``; ``compare`` prints the table.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

NAME = "fixture_search"
#: simulated seconds between two calls
GAP_S = 300.0
WORDS = ["amber", "birch", "cedar", "delta", "ember", "falcon", "tulip", "core", "sdk", "tools"]
FLAP = "UUDDUDDDUUDUDDDDUUUDUDDUUUDDDUDUUDDDDUUU"
SCENARIOS = {
    "outage": ["down"] * 8 + ["up"] * 24,
    "outage_ra": ["down_ra"] * 8 + ["up"] * 24,
    "flapping": ["up" if c == "U" else "down" for c in FLAP],
    "drift": ["drift"] * 30,
    "old_record": ["up"] * 24,
    "answers_record": ["up"] * 24,
}
MODEL_ENV = ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
             "ANYUSE_AGENT_MODEL_PROVIDER", "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY",
             "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")
#: what the old code wrote on a tool after three 503s in a row
OLD_OUTAGE_REASON = ("3 consecutive failures (>= 3); last error: non-2xx status 503; AnyUse recompiles "
                     "it automatically when a lower rung answers; if that cannot prove a new request, "
                     "record the search again and recompile it")
#: a tool taken out after three answers that failed its checks
ANSWERS_ERROR = "postcondition failed: path 'results' is NoneType, expected a list"

_OFFSET = [0.0]


def _install_clock() -> None:
    """``time.time`` runs ``_OFFSET`` seconds ahead; called before AnyUse is imported, so
    defaults bound at import time (a heal's clock) take it too."""
    real = time.time

    def simulated() -> float:
        return real() + _OFFSET[0]
    time.time = simulated


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Site:
    """The fixture in a thread on a free loopback port; ``state`` switches what it serves."""

    def __init__(self, fx, certdir: str):
        cert, key = fx.make_cert(certdir)
        self.lock = threading.Lock()
        self.counts: Dict[str, int] = {}
        self.state = "up"
        site = self

        class Handler(fx.Handler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with site.lock:
                    site.counts[path] = site.counts.get(path, 0) + 1
                    state = site.state
                if path == "/robots.txt" or state == "up":
                    return self._send(*fx.respond(self.path, "original"))
                if state == "drift":
                    return self._send(*fx.respond(self.path, "changed_lying_page"))
                data = b"busy"
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                if state == "down_ra":
                    self.send_header("Retry-After", "1200")
                self.end_headers()
                self.wfile.write(data)
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


def _iso(seconds: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def run_case(cid: str, fx, rec_mod, out_dir: str, label: str) -> Dict[str, Any]:
    from anyuse.execution import api_permission as perm
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.mcpserver.compiled_tools import run_compiled
    case_dir = os.path.join(out_dir, f"{label}_{cid}")
    shutil.rmtree(case_dir, ignore_errors=True)
    os.makedirs(case_dir)
    certdir = os.path.join(out_dir, "cert")
    site = Site(fx, certdir)
    origin = f"https://127.0.0.1:{site.port}"
    os.environ.update(_env_for(case_dir, origin, os.path.join(certdir, "cert.pem")))
    perm._ROBOTS_CACHE.clear()
    perm._ROBOTS_NOTES.clear()
    _OFFSET[0] = 0.0
    row: Dict[str, Any] = {"case": cid, "arm": label, "timeline": SCENARIOS[cid]}
    try:
        recdir = os.path.join(case_dir, "recordings")
        rec_mod.main([origin, recdir, NAME, "river", "stone", "cloud"])
        site.take()
        proc = subprocess.run([sys.executable, "-B", "-m", "anyuse", "workflow", "compile", NAME,
                               "--recording", recdir, "--yes"], cwd=case_dir,
                              env={**os.environ}, capture_output=True, text=True, timeout=300)
        row["compile_rc"] = proc.returncode
        if proc.returncode != 0:
            row["compile_output"] = (proc.stdout + proc.stderr)[-2000:]
            return row
        w1 = run_compiled(NAME, {"q": "maple"}, cascade=False)
        w2 = run_compiled(NAME, {"q": "harbor"})
        row["warm_ok"] = [bool(w1.get("ok")), bool(w2.get("ok"))]
        if cid in ("old_record", "answers_record"):
            record = load_compiled(_storage(), _wid(NAME)) or {}
            error = "non-2xx status 503" if cid == "old_record" else ANSWERS_ERROR
            record["health"] = {**(record.get("health") or {}), "preferred": False,
                                "demoted_reason": OLD_OUTAGE_REASON.replace("non-2xx status 503", error),
                                "consecutive_failures": 3, "total_failures": 3, "last_error": error,
                                "last_outcome_at": _iso(time.time())}
            persist_compiled(_storage(), _wid(NAME), record)
        site.take()
        calls = []
        for i, state in enumerate(SCENARIOS[cid]):
            _OFFSET[0] += GAP_S
            site.state = state
            q = WORDS[i % len(WORDS)]
            t0 = time.monotonic()
            out = run_compiled(NAME, {"q": q})
            seconds = time.monotonic() - t0
            counts = site.take()
            items = (out.get("result") or {}).get("items") if out.get("ok") else None
            calls.append({
                "i": i, "minute": round(_OFFSET[0] / 60.0), "state": state, "q": q,
                "ok": bool(out.get("ok")), "served_by": out.get("served_by"),
                "exact": items == fx.search(q), "items": items,
                "restored": bool(out.get("restored")), "healed": bool(out.get("healed")),
                "transient": bool(out.get("transient")),
                "next": str(out.get("fallback") or out.get("next") or "")[:240],
                "requests": {"api": counts.get("/api/search", 0), "page": counts.get("/search", 0),
                             "robots": counts.get("/robots.txt", 0),
                             "other": sum(v for k, v in counts.items()
                                          if k not in ("/api/search", "/search", "/robots.txt"))},
                "seconds": round(seconds, 4), "error": str(out.get("error") or "")[:200]})
        row["calls"] = calls
        stored = load_compiled(_storage(), _wid(NAME)) or {}
        row["final"] = {"health": stored.get("health"), "restored": stored.get("restored"),
                        "healed": bool(stored.get("healed")),
                        "items": ((stored.get("template") or {}).get("extract") or {}).get("items")}
        return row
    finally:
        site.stop()


def run_arm(label: str, fixture_dir: str, out_file: str) -> None:
    out_dir = os.path.dirname(out_file)
    fx = _load(os.path.join(fixture_dir, "fixture_site.py"), "outage_bench_fixture_site")
    rec_mod = _load(os.path.join(fixture_dir, "record_fixture.py"), "outage_bench_record_fixture")
    import anyuse
    rows = {"arm": label, "tree": os.path.dirname(os.path.realpath(anyuse.__file__)),
            "gap_s": GAP_S, "cases": []}
    for cid in SCENARIOS:
        rows["cases"].append(run_case(cid, fx, rec_mod, out_dir, label))
        print(f"{label} {cid}: done", flush=True)
    with open(out_file, "w") as fh:
        json.dump(rows, fh, indent=1, default=str)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

def _total(call: Dict[str, Any]) -> int:
    return sum(call["requests"][k] for k in ("api", "page", "other"))


def _metrics(case: Dict[str, Any]) -> Dict[str, Any]:
    calls = case.get("calls") or []
    first_bad = next((c["i"] for c in calls if c["state"] != "up"), None)
    last_bad = max((c["i"] for c in calls if c["state"] != "up"), default=None)
    after = [c for c in calls if c["state"] == "up" and (last_bad is None or c["i"] > last_bad)]
    up = [c for c in calls if c["state"] == "up"]
    answered_after = [c for c in after if c["ok"] and c["exact"]]
    first = answered_after[0] if answered_after else None
    start = calls[last_bad + 1]["minute"] if last_bad is not None and last_bad + 1 < len(calls) else (
        calls[0]["minute"] if calls else 0)
    bad = [c for c in calls if c["state"] != "up"]
    return {
        "compile_rc": case.get("compile_rc"),
        "calls": len(calls),
        "up": len(up), "answered_up": sum(c["ok"] and c["exact"] for c in up),
        "after": len(after), "answered_after": len(answered_after),
        "first_after_min": (first["minute"] - start) if first else None,
        "wrong": sum(c["ok"] and not c["exact"] for c in calls),
        "req_bad": sum(_total(c) for c in bad), "req_up": sum(_total(c) for c in up),
        "req_page": sum(c["requests"]["page"] for c in calls),
        "restores": sum(c["restored"] for c in calls), "heals": sum(c["healed"] for c in calls),
        "final_preferred": bool(((case.get("final") or {}).get("health") or {}).get("preferred", True)),
        "first_bad": first_bad,
    }


def compare(files: List[str]) -> str:
    arms = [json.load(open(f)) for f in files]
    lines = [f"{'arm':4} {'case':14} {'up':>3} {'ans':>4} {'after':>5} {'ans':>4} {'1st min':>7} "
             f"{'wrong':>5} {'req bad':>7} {'req up':>6} {'page':>4} {'rest':>4} {'heal':>4} {'final':>6}"]
    metrics: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for arm in arms:
        for case in arm["cases"]:
            m = _metrics(case)
            metrics.setdefault(case["case"], {})[arm["arm"]] = m
            first = "-" if m["first_after_min"] is None else str(m["first_after_min"])
            lines.append(f"{arm['arm']:4} {case['case']:14} {m['up']:>3} {m['answered_up']:>4} "
                         f"{m['after']:>5} {m['answered_after']:>4} {first:>7} {m['wrong']:>5} "
                         f"{m['req_bad']:>7} {m['req_up']:>6} {m['req_page']:>4} {m['restores']:>4} "
                         f"{m['heals']:>4} {'in' if m['final_preferred'] else 'out':>6}")
    lines.append("")
    lines.append(f"columns: up / ans = calls made while the site was up, and of those answered with exactly the "
                 f"fixture's list; after / ans = the same for the calls after the last bad one; 1st min = "
                 f"simulated minutes from the first call after recovery to the first exact answer; wrong = "
                 f"answers that are not the fixture's list; req bad / req up = requests (robots.txt apart) in "
                 f"calls while the site was down or drifted / up; page = result-page reads; rest = calls that "
                 f"put the request back in service; heal = calls a heal swapped; final = in or out of service "
                 f"at the end. Calls are {int(GAP_S / 60)} simulated minutes apart.")
    lines.append("")
    for cid, by_arm in metrics.items():
        new, old = by_arm.get("NEW"), by_arm.get("OLD")
        if not new:
            continue
        if cid in ("outage", "outage_ra", "old_record", "answers_record"):
            ok = (new["wrong"] == 0 and new["final_preferred"] and new["answered_after"] >= new["after"] - 3)
            lines.append(f"{'PASS' if ok else 'FAIL'} NEW {cid}: back in service, at most 3 calls after recovery "
                         f"unanswered, 0 wrong (got {new['answered_after']}/{new['after']}, "
                         f"{new['wrong']} wrong, final {'in' if new['final_preferred'] else 'out'})")
        elif cid == "flapping":
            ok = new["wrong"] == 0 and new["final_preferred"] and new["answered_up"] > (old or {}).get(
                "answered_up", 0)
            lines.append(f"{'PASS' if ok else 'FAIL'} NEW {cid}: more up-calls answered than OLD, 0 wrong, still in "
                         f"service (got {new['answered_up']}/{new['up']} vs OLD "
                         f"{(old or {}).get('answered_up')}/{(old or {}).get('up')})")
        else:
            ok = new["wrong"] == 0 and not new["final_preferred"] and new["restores"] == 0
            lines.append(f"{'PASS' if ok else 'FAIL'} NEW {cid}: never put back in service, 0 wrong (got "
                         f"{new['restores']} restores, {new['wrong']} wrong, final "
                         f"{'in' if new['final_preferred'] else 'out'}; requests {new['req_bad']} vs OLD "
                         f"{(old or {}).get('req_bad')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-outage-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    fixture_dir = os.path.join(trees["NEW"], "examples", "quickstart")
    me = os.path.join(out, "outage_bench.py")
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
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        _install_clock()
        run_arm(args.label, os.path.abspath(args.fixture), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
