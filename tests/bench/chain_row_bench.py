"""Offline A/B bench: which result row a compiled search -> open chain binds.

Two arms run the same corpus, each in its own process that imports ``anyuse``
from its own checkout:

* ``old``: a checkout that always binds the first result row (``--old-root``);
* ``new``: this checkout (``--new-root``, default: the checkout holding this
  file), which binds the row whose name is the input when a recording showed
  the page opening a later row of that name.

Corpus: a synthetic package registry served by an in-process transport (the
compiler only reads https recordings, so a loopback HTTP server cannot be
compiled from). ``/api/search?q=`` returns four popularity-ranked
``{"id", "name"}`` rows; ``/api/package/<id>`` returns one package. Ids are
opaque (``pk3f…``), so the second request's value always differs from the
typed word. For each word the row named by the word is the first row for 60%
of the words and row 1 to 3 for the other 40%; two of the later-row words name
it with different case (a normalised match). Five more words have no row of
that name.

Each layout is one compile: a compile recording, one fitting recording and two
validation recordings, run through ``compile_and_verify`` (recordings carry
the intermediate binding the proof checks):

* ``later_in_fit``: the compile recording opened a later row of that name;
* ``all_first``: every recording opened the first row (it was the named one);
* ``later_in_validation``: only a validation recording opened a later row;
* ``later_unnamed``: the compile recording opened a later row that does NOT
  carry the typed name (the person clicked another result).

A proved tool is then called for 20 unseen words that have a row of that name
and 5 that have none. Counted per call: the named row opened (correct), another
row opened (wrong, served as a success), refused with ``not_found``, other
failure. Each arm also runs its own ``tests/test_api_chain.py``.

Usage::

    python tests/bench/chain_row_bench.py --old-root /path/to/old/checkout \\
        [--json results.json]

No network. Not part of the test suite and not shipped in the wheel.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORIGIN = "https://registry.test"
SEED = 7
ROWS = 4


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------

SYLLABLES = ["ar", "bel", "cor", "dax", "el", "fen", "gro", "hal", "ix", "jun",
             "kel", "lum", "mor", "nax", "ol", "pyr", "qua", "ros", "sel", "tor",
             "ul", "vex", "wyn", "yar", "zel"]


def _ident(name):
    return "pk" + hashlib.sha256(name.encode()).hexdigest()[:8]


def corpus():
    """``{word: [row names, most popular first]}``, each word's role, the words.

    Recorded words: four whose named row is the first and one whose named row
    is a later one. Unseen words: 12 first, 8 later (two of them named with
    different case), and 5 with no row of that name.
    """
    rng = random.Random(SEED)
    words = []
    seen = set()
    while len(words) < 30:
        word = rng.choice(SYLLABLES) + rng.choice(SYLLABLES)
        if word not in seen:
            seen.add(word)
            words.append(word)
    recorded, unseen, unnamed = words[:5], words[5:25], words[25:]
    later = {recorded[4], *unseen[12:]}
    cased = set(unseen[12:14])
    results = {}
    roles = {}
    for word in recorded + unseen:
        others = [f"{word}-{s}" for s in ("core", "derive", "macros", "cli", "utils")]
        rng.shuffle(others)
        rows = others[:ROWS - 1]
        position = rng.randint(1, ROWS - 1) if word in later else 0
        rows.insert(position, word.capitalize() if word in cased else word)
        results[word] = rows
        roles[word] = {"named_row": position, "normalised": word in cased}
    for word in unnamed:
        rows = [f"{word}-{s}" for s in ("core", "derive", "macros", "cli")]
        rng.shuffle(rows)
        results[word] = rows
        roles[word] = {"named_row": -1, "normalised": False}
    return results, roles, recorded, unseen, unnamed


def search_body(results, q):
    return {"crates": [{"id": _ident(n), "name": n} for n in results[q]]}


def detail_body(name):
    ident = _ident(name)
    return {"item": {"id": ident, "title": f"{name} package", "summary": f"what {name} does",
                     "version": f"1.{len(name)}.{int(ident[2:4], 16) % 9}"}}


class Registry:
    """The in-process transport: search and package endpoints."""

    def __init__(self, results):
        self.results = results
        self.by_id = {_ident(n): n for rows in results.values() for n in rows}
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        self.sent.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/api/search":
            q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps(search_body(self.results, q)))
        ident = parts.path.rsplit("/", 1)[-1]
        if ident not in self.by_id:
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        return ApiResponse(200, {"Content-Type": "application/json"},
                           json.dumps(detail_body(self.by_id[ident])))


def recording(results, q, row):
    """Searching ``q`` and opening result ``row``."""
    from anyuse.execution.api_compile import CHAIN_PARAM
    opened = results[q][row]
    item = detail_body(opened)["item"]
    return {
        "param": {"name": "q", "value": q},
        "ui": {"items": [item["title"], item["summary"], item["version"]], "text": "",
               "fields": {"field_1": item["title"], "field_2": item["summary"],
                          "field_3": item["version"]}},
        "bindings": {CHAIN_PARAM: _ident(opened)},
        "network": [
            {"url": f"{ORIGIN}/api/search?q={urllib.parse.quote(q)}", "method": "GET",
             "status": 200, "mime": "application/json", "bytes": 100,
             "body": json.dumps(search_body(results, q))},
            {"url": f"{ORIGIN}/api/package/{_ident(opened)}", "method": "GET",
             "status": 200, "mime": "application/json", "bytes": 80,
             "body": json.dumps(detail_body(opened))},
        ],
    }


def layouts(roles, recorded):
    """Recorded words and opened rows per layout: (compile, fitting, validation)."""
    first = [w for w in recorded if roles[w]["named_row"] == 0]
    later = [w for w in recorded if roles[w]["named_row"] > 0]
    pick = lambda w: (w, roles[w]["named_row"])
    other_row = lambda w: (w, 1 if roles[w]["named_row"] != 1 else 2)
    out = {
        "later_in_fit": (pick(later[0]), pick(first[0]), [pick(first[1]), pick(first[2])]),
        "all_first": (pick(first[0]), pick(first[1]), [pick(first[2]), pick(first[3])]),
        "later_in_validation": (pick(first[0]), pick(first[1]),
                                [pick(first[2]), pick(later[0])]),
        "later_unnamed": (other_row(first[0]), pick(first[1]),
                          [pick(first[2]), pick(first[3])]),
    }
    return out


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

def _chain_tests(root):
    """Run ``tests/test_api_chain.py`` of this arm's checkout: (passed, failed)."""
    path = os.path.join(root, "tests", "test_api_chain.py")
    scope = {"__name__": "bench_chain_tests", "__file__": path}
    with open(path, encoding="utf-8") as fh:
        exec(compile(fh.read(), path, "exec"), scope)
    passed = failed = 0
    names = []
    for name, fn in list(scope.items()):
        if name.startswith("test_") and callable(fn) and not inspect.signature(fn).parameters:
            try:
                fn()
                passed += 1
            except Exception:
                failed += 1
                names.append(name)
    return passed, failed, names


def run_arm(ns):
    sys.dont_write_bytecode = True
    sys.path.insert(0, ns.pkgroot)
    from anyuse.execution.api_compile import CHAIN_PARAM
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify

    results, roles, recorded, unseen_named, unseen_unnamed = corpus()
    plans = layouts(roles, recorded)
    out = {"arm": ns.arm, "layouts": {}, "unseen": {"named": len(unseen_named),
                                                    "unnamed": len(unseen_unnamed)}}
    for label, (base, fit, fresh) in plans.items():
        rec = lambda pair: recording(results, *pair)
        compiled, proof = compile_and_verify(
            rec(base), [rec(p) for p in fresh],
            executor=ApiStepExecutor(transport=Registry(results)),
            fitting_recordings=[rec(fit)])
        row = {"recorded": {"compile": list(base), "fitting": list(fit),
                            "validation": [list(p) for p in fresh]},
               "status": compiled.status, "proved": bool(proof.passed),
               "reason": (proof.reason or compiled.reason)[:240],
               "rule": None, "calls": {}}
        tmpl = compiled.template
        if tmpl is not None and getattr(tmpl, "steps", None):
            row["rule"] = (getattr(tmpl.steps[0], "pick", {}) or {}).get(CHAIN_PARAM) or "first"
            row["proof_rules"] = [c.contract.get("row_rules") for c in proof.cases]
        if proof.passed and tmpl is not None:
            counts = {"correct": 0, "wrong": 0, "not_found": 0, "failed": 0}
            rules = {}
            unnamed_counts = {"refused": 0, "served": 0, "failed": 0}
            wrong = []
            for word in unseen_named + unseen_unnamed:
                registry = Registry(results)
                res = ApiStepExecutor(transport=registry).execute(tmpl, {"q": word})
                bound = ((res.get("plan_trace") or [{}])[0].get("bound") or {}).get(CHAIN_PARAM)
                opened = registry.by_id.get(bound) if bound else None
                how = ((res.get("plan_trace") or [{}])[0].get("picked") or {}).get(CHAIN_PARAM)
                rule_used = how["rule"] if how else ("first entry" if bound else "none")
                rules[rule_used] = rules.get(rule_used, 0) + 1
                if word in unseen_named:
                    want = results[word][roles[word]["named_row"]]
                    if res.get("success") and opened == want:
                        counts["correct"] += 1
                    elif res.get("success"):
                        counts["wrong"] += 1
                        wrong.append([word, opened])
                    elif res.get("reason_code") == "not_found":
                        counts["not_found"] += 1
                    else:
                        counts["failed"] += 1
                else:
                    if res.get("reason_code") == "not_found":
                        unnamed_counts["refused"] += 1
                    elif res.get("success"):
                        unnamed_counts["served"] += 1
                    else:
                        unnamed_counts["failed"] += 1
            row["calls"] = {"named": counts, "unnamed": unnamed_counts,
                            "rules": rules, "wrong_examples": wrong[:3]}
        out["layouts"][label] = row
    passed, failed, names = _chain_tests(os.path.realpath(os.path.join(ns.pkgroot, "anyuse")))
    out["test_api_chain"] = {"passed": passed, "failed": failed, "failed_names": names}
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def report(old, new):
    lines = []
    checks = []
    n_named = new["unseen"]["named"]
    n_unnamed = new["unseen"]["unnamed"]
    lines.append(f"unseen words: {n_named} with a row of that name, {n_unnamed} without")
    lines.append(f"{'layout':22} {'arm':4} {'proved':6} {'rule':28} "
                 f"{'correct':>7} {'wrong':>5} {'refused':>7} | {'no-name refused':>15} {'served':>6}")
    for label in new["layouts"]:
        for arm in (old, new):
            row = arm["layouts"][label]
            rule = row["rule"]
            rule_text = ("-" if rule is None else rule if isinstance(rule, str) else
                         f"name={rule['name_path']}" + (" else first" if rule["else_first"] else ""))
            calls = row["calls"] or {}
            named = calls.get("named") or {}
            unnamed = calls.get("unnamed") or {}
            lines.append(f"{label:22} {arm['arm']:4} {str(row['proved']):6} {rule_text:28} "
                         f"{named.get('correct', '-'):>7} {named.get('wrong', '-'):>5} "
                         f"{named.get('not_found', '-'):>7} | {unnamed.get('refused', '-'):>15} "
                         f"{unnamed.get('served', '-'):>6}")
        lines.append(f"{'':22} rules: old {(old['layouts'][label]['calls'] or {}).get('rules')}"
                     f"; new {(new['layouts'][label]['calls'] or {}).get('rules')}")
        lines.append(f"{'':22} reasons: old: {old['layouts'][label]['reason'][:110]}")
        lines.append(f"{'':22}          new: {new['layouts'][label]['reason'][:110]}")

    def calls(arm, label):
        return arm["layouts"][label]["calls"] or {}

    fit = calls(new, "later_in_fit")
    checks.append({"name": "later_in_fit: new opens the named row for every unseen word with one",
                   "pass": (fit.get("named") or {}).get("correct") == n_named})
    checks.append({"name": "later_in_fit: new refuses every unseen word without one",
                   "pass": (fit.get("unnamed") or {}).get("refused") == n_unnamed})
    first_old, first_new = calls(old, "all_first"), calls(new, "all_first")
    checks.append({"name": "all_first: new answers exactly as old (no evidence, no change)",
                   "pass": first_old == first_new and bool(first_new)})
    checks.append({"name": "later_unnamed: new refuses to compile",
                   "pass": not new["layouts"]["later_unnamed"]["proved"]
                   and new["layouts"]["later_unnamed"]["status"] == "failed"})
    checks.append({"name": "test_api_chain.py passes in both arms",
                   "pass": old["test_api_chain"]["failed"] == 0
                   and new["test_api_chain"]["failed"] == 0})
    lines.append("")
    lines.append(f"test_api_chain.py: old {old['test_api_chain']['passed']} passed / "
                 f"{old['test_api_chain']['failed']} failed; new "
                 f"{new['test_api_chain']['passed']} passed / {new['test_api_chain']['failed']} failed")
    for check in checks:
        lines.append(("PASS " if check["pass"] else "FAIL ") + check["name"])
    return "\n".join(lines), {"old": old, "new": new, "checks": checks}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def _arm(arm, root, tmp):
    pk = os.path.join(tmp, "pk-" + arm)
    os.mkdir(pk)
    os.symlink(os.path.realpath(root), os.path.join(pk, "anyuse"))
    out = os.path.join(tmp, arm + ".json")
    # the arm's test_api_chain.py run reads and writes only under this home
    home = os.path.join(tmp, "home-" + arm)
    data = os.path.join(home, ".anyuse")
    os.makedirs(data)
    env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
               XDG_DATA_HOME=os.path.join(home, ".local", "share"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"), ANYUSE_DATA_DIR=data,
               ANYUSE_SECRETS_DIR=data, ANYUSE_VAULT_KEYFILE=os.path.join(data, "entity_vault.keys"),
               ANYUSE_API_HOSTS_FILE=os.path.join(data, "api_hosts.json"),
               ANYUSE_BLOCKED_HOSTS_FILE=os.path.join(data, "api_blocked_hosts.json"),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    cmd = [sys.executable, "-I", "-B", os.path.abspath(__file__), "--arm", arm,
           "--pkgroot", pk, "--out", out]
    return subprocess.Popen(cmd, env=env), out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old-root", help="checkout of the code that always binds the first row")
    ap.add_argument("--new-root", default=CHECKOUT)
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--arm", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pkgroot", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    if ns.arm:
        run_arm(ns)
        return 0
    if not ns.old_root:
        ap.error("--old-root is required: a checkout of the code before the change")
    tmp = tempfile.mkdtemp(prefix="chain-row-bench-")
    try:
        procs = [_arm(arm, root, tmp)
                 for arm, root in (("old", ns.old_root), ("new", ns.new_root))]
        if [p.wait() for p, _o in procs] != [0, 0]:
            print("an arm failed")
            return 2
        docs = []
        for _p, path in procs:
            with open(path, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        text, result = report(*docs)
        print(text)
        if ns.json:
            with open(ns.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=1)
        return 0 if all(c["pass"] for c in result["checks"]) else 1
    finally:
        for arm in ("old", "new"):
            link = os.path.join(tmp, "pk-" + arm, "anyuse")
            if os.path.islink(link):
                os.unlink(link)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
