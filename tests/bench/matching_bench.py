"""Offline A/B bench: does a result name the query, in every script?

Three questions, each case with the answer a person would give:

* **positions**: in Chinese, Japanese and Thai, a compiled tool answers one result
  whose name holds the input on its own, at the start, at the end or inside a
  longer word (神戸, 神戸駅, 東神戸, 東神戸駅). On a site whose own result lists only
  ever start a word with their input, an answer at the end or inside is another
  thing (a decoy: refuse); on a site whose lists name their input inside longer
  words too, it is that site's correct answer (accept). A name without the input
  is always refused.
* **empty statements**: a "no results" line that names the input in quotes is
  about it (accept); one that holds it inside a longer word is about that word
  (refuse); one that writes it without quotes or spaces ("没有找到与南京相关的结果")
  is about it, but only a reading of the words can tell.
* **folding**: a site whose page writes its labels in capitals without accents,
  with the digits of another script, or with optional vowel marks, while its
  response writes them plainly, compiles and proves (accept). Labels whose letters
  really differ (Kazakh қ and к, a Thai tone mark, a kana voicing mark, Turkish ı
  and i) must not (refuse). Keys in spaced scripts keep the whole-word rule.

Arms: each tree with no check model, with a fake model that reads every case the
way the person would (``right``), and with a fake model that always reads the
input as a word of its own (``yes``). The fakes answer only the query-word
question; any other check question gets an unclear answer, so the statement rule
decides there. A row's ``got`` is whether the tree accepted.

Usage::

    python tests/bench/matching_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--out <dir>]
    python tests/bench/matching_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per tree, with a pkgroot holding an ``anyuse``
symlink to the tree and the tree's ``tools/guarded_tests`` on PYTHONPATH (its
sitecustomize refuses sockets). HOME and every AnyUse data path point into
``--out``, and no ``ANYUSE_*`` variable is passed through.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

HOST = "fix.test"
MODELS = ("off", "right", "yes")

# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------

#: per language: the compile input, two validation inputs, the input answered at run
#: time, filler letters of the same script that start and end words, and the
#: one-result answers at each position
POSITIONS = {
    "ja": {"fit": "京都", "validation": ("大阪", "札幌"), "run": "神戸",
           "after": ("駅", "城", "大学", "公園"), "before": ("東", "新"),
           "answers": {"word": "神戸", "start": "神戸駅", "end": "東神戸", "inside": "東神戸駅",
                       "absent": "大阪駅"}},
    "zh": {"fit": "北京", "validation": ("上海", "广州"), "run": "南京",
           "after": ("站", "路", "大学", "公园"), "before": ("江", "新"),
           "answers": {"word": "南京", "start": "南京路", "end": "江南京", "inside": "江南京站",
                       "absent": "上海站"}},
    "th": {"fit": "กรุงเทพ", "validation": ("ภูเก็ต", "พัทยา"), "run": "เชียงใหม่",
           "after": ("พลาซ่า", "สถานี", "ตลาด", "วัด"), "before": ("ตลาด", "เมือง"),
           "answers": {"word": "เชียงใหม่", "start": "เชียงใหม่พลาซ่า", "end": "ตลาดเชียงใหม่",
                       "inside": "ตลาดเชียงใหม่พลาซ่า", "absent": "ภูเก็ตพลาซ่า"}},
}

#: "no results" lines on an HTML page: (language, input, line, a person reads it as
#: about the input)
EMPTY_LINES = [
    ("zh", "南京", "没有找到与“南京”相关的结果", True),
    ("zh", "南京", "没有找到与江南京站相关的结果", False),
    ("zh", "南京", "没有找到与南京相关的结果", True),
    ("ja", "神戸", "「神戸」に一致する商品は見つかりませんでした", True),
    ("ja", "神戸", "東神戸駅に一致する商品は見つかりませんでした", False),
    ("ja", "京都", "東京都に一致する商品は見つかりませんでした", False),
]

#: folding: (case, the response's word, the page's word, they are the same word)
FOLDS = [
    ("el capitals without accents", "Αθήνα", "ΑΘΗΝΑ", True),
    ("el capitals, decomposed response", unicodedata.normalize("NFD", "Ηράκλειο"), "ΗΡΑΚΛΕΙΟ", True),
    ("de capitals without accents", "München", "MUNCHEN", True),
    ("vi accents and đ", "Đà Nẵng", "DA NANG", True),
    ("pl ł", "Wrocław", "Wroclaw", True),
    ("da ø", "Øresund", "Oresund", True),
    ("ar Arabic-Indic digits", "مكتبة 12", "مكتبة ١٢", True),
    ("th Thai digits", "ห้อง 25", "ห้อง ๒๕", True),
    ("ar harakat", "مكتبة", "مَكْتَبَة", True),
    ("ar hamza", "أحمد", "احمد", True),
    ("he niqqud", "שלום", "שָׁלוֹם", True),
    ("kk letters without a decomposition", "Қазақ", "Казак", False),
    ("th tone mark", "บ้าน", "บาน", False),
    ("ja voicing mark", "ガイド", "カイト", False),
    ("tr dotless i", "Kısa", "Kisa", False),
]
#: the other words of the labels a fold site lists (one per result)
FOLD_SUFFIXES = ("one", "two", "three", "four")
FOLD_INPUTS = ("alpha", "bravo", "charlie")

#: identity keys in scripts with spaces: (case, key, input, the key names the input)
KEYS = [
    ("ru whole word", "Москва Сити", "москва", True),
    ("ru inside a word", "Москвариум", "москва", False),
    ("el capitals", "ΑΘΗΝΑ ΚΕΝΤΡΟ", "Αθήνα", True),
    ("fr accent", "Le Gruyère AOP", "gruyere", True),
    ("en whole token", "mingw-w64-boost", "boost", True),
    ("en inside a token", "boostnote", "boost", False),
    ("kk distinct letters", "Қазақстан", "казакстан", False),
]


# ---------------------------------------------------------------------------
# the fake check model
# ---------------------------------------------------------------------------

class Fake:
    """Answers the query-word question (its state marks the input ``⟦ ⟧``): ``right``
    reads the input as a word exactly when the case says so, ``yes`` always. Any
    other question gets an unclear answer."""

    def __init__(self, mode: str):
        self.mode, self.truth, self.calls = mode, False, 0

    def __call__(self, instructions: str, state: str, obs: Any) -> float:
        self.calls += 1
        if "⟦" not in state:
            return 0.5
        line = state.splitlines()[-1]
        marked = state[state.index("⟦") + 1:state.index("⟧")]
        is_query = line == ">> " + marked
        named = True if self.mode == "yes" else self.truth
        return 0.95 if is_query == named else 0.05


def _call(fake: Optional[Fake], truth: bool, fn):
    """``fn()`` as a product call: inside the product scope, with ``fake`` (or no model)."""
    from anyuse.execution import model_check as MC
    if fake is not None:
        fake.truth = truth
    with MC.scope("product"), MC.using(fake):
        return fn()


# ---------------------------------------------------------------------------
# the sites
# ---------------------------------------------------------------------------

def _body(names: Sequence[str]) -> str:
    return json.dumps({"results": [{"name": n, "id": i} for i, n in enumerate(names)]},
                      ensure_ascii=False)


def _recording(value: str, ui: Sequence[str], api: Sequence[str]) -> Dict[str, Any]:
    q = urllib.parse.quote(value)
    return {"param": {"name": "q", "value": value}, "site": HOST,
            "final_url": f"https://{HOST}/search?q={q}",
            "ui": {"items": list(ui), "text": "\n".join(ui)},
            "network": [{"url": f"https://{HOST}/search?q={q}", "method": "GET", "status": 200,
                         "mime": "text/html", "body": "<html><div id=app></div></html>"},
                        {"url": f"https://{HOST}/api/search?q={q}", "method": "GET",
                         "status": 200, "mime": "application/json", "body": _body(api)}]}


class Site:
    def __init__(self, lists: Dict[str, List[str]]):
        self.lists = lists

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        return ApiResponse(200, {"Content-Type": "application/json"}, _body(self.lists.get(q, [])))


def _compile(lists_ui: Dict[str, List[str]], lists_api: Dict[str, List[str]], site: Site):
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    values = list(lists_ui)
    rec = _recording(values[0], lists_ui[values[0]], lists_api[values[0]])
    fresh = [_recording(v, lists_ui[v], lists_api[v]) for v in values[1:]]
    return compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=site),
                              preflight=lambda _c: "")


def _site_lists(spec: Dict[str, Any], kind: str) -> Dict[str, List[str]]:
    """The lists a site serves for the compile and validation inputs: ``prefix`` lists
    start every name with the input; ``substring`` lists also hold it at the end of
    and inside longer words."""
    out = {}
    for v in (spec["fit"], *spec["validation"]):
        a = spec["after"]
        b = spec["before"]
        if kind == "prefix":
            out[v] = [v + x for x in a]
        else:
            out[v] = [v + a[0], v + a[1], b[0] + v, b[1] + v + a[2]]
    return out


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def position_rows(models: Sequence[str]) -> List[Dict[str, Any]]:
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_step import ApiStepExecutor
    rows: List[Dict[str, Any]] = []
    for lang, spec in POSITIONS.items():
        for kind in ("prefix", "substring"):
            lists = _site_lists(spec, kind)
            compiled, proof = _compile(lists, lists, Site(lists))
            contract = RC.contract_of(compiled.template) if compiled.template is not None else None
            for pos, name in spec["answers"].items():
                truth = pos in ("word", "start") or (kind == "substring" and pos != "absent")
                site = Site({**lists, spec["run"]: [name]})
                for model in models:
                    fake = None if model == "off" else Fake(model)
                    if not proof.passed:
                        got, why = False, f"not proved: {proof.reason}"
                    else:
                        ex = ApiStepExecutor(transport=site)
                        ex.empty_context = EP.Context(probe=False, learn=False)
                        out = _call(fake, truth, lambda: ex.execute(compiled.template,
                                                                    {"q": spec["run"]}))
                        got = bool(out.get("success"))
                        why = str(out.get("error") or (out.get("result_contract") or {})
                                  .get("reason") or "")
                    rows.append({"group": f"positions/{kind}-site", "case": f"{lang} {pos}",
                                 "model": model, "truth": truth, "got": got,
                                 "model_calls": fake.calls if fake is not None else 0,
                                 "why": why[:160],
                                 "positions": (contract or {}).get("query_positions")})
    return rows


def empty_rows(models: Sequence[str]) -> List[Dict[str, Any]]:
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    rows = []
    for lang, value, line, truth in EMPTY_LINES:
        page = f"<html><body><main><h1>検索</h1><p>{line}</p></main></body></html>"
        for model in models:
            fake = None if model == "off" else Fake(model)
            v = _call(fake, truth, lambda: RC.check_html(
                RC.replay_contract("q"), page, "ul.r li", {"q": value}, [],
                empty=EP.Context(probe=False, learn=False)))
            rows.append({"group": "empty statements", "case": f"{lang} {line}", "model": model,
                         "truth": truth, "got": bool(v.ok),
                         "model_calls": fake.calls if fake is not None else 0,
                         "why": v.reason[:160]})
    return rows


def fold_rows(models: Sequence[str]) -> List[Dict[str, Any]]:
    rows = []
    for case, api_word, ui_word, truth in FOLDS:
        api = {v: [f"{api_word} {v} {s}" for s in FOLD_SUFFIXES] for v in FOLD_INPUTS}
        ui = {v: [f"{ui_word} {v} {s}" for s in FOLD_SUFFIXES] for v in FOLD_INPUTS}
        compiled, proof = _compile(ui, api, Site(api))
        got = compiled.template is not None and bool(proof.passed)
        why = proof.reason if compiled.template is not None else compiled.reason
        for model in models:
            rows.append({"group": "folding (proof)", "case": case, "model": model,
                         "truth": truth, "got": got, "model_calls": 0, "why": str(why)[:160]})
    return rows


def key_rows(models: Sequence[str]) -> List[Dict[str, Any]]:
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    rows = []
    for case, key, value, truth in KEYS:
        items = [{"name": key}]
        for model in models:
            fake = None if model == "off" else Fake(model)
            v = _call(fake, truth, lambda: RC.check_json(
                RC.replay_contract("q"), {"results": items}, "results", {"q": value}, items,
                empty=EP.Context(probe=False, learn=False)))
            rows.append({"group": "keys in spaced scripts", "case": case, "model": model,
                         "truth": truth, "got": bool(v.ok),
                         "model_calls": fake.calls if fake is not None else 0,
                         "why": v.reason[:160]})
    return rows


def run_arm(label: str, tree: str, out_path: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    t0 = time.monotonic()
    rows = position_rows(MODELS) + empty_rows(MODELS) + fold_rows(MODELS) + key_rows(MODELS)
    meta = {"label": label, "tree": tree, "anyuse": real, "seconds": round(time.monotonic() - t0, 2)}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _errors(rows: Sequence[Dict[str, Any]]) -> Tuple[int, int, int]:
    fa = sum(1 for r in rows if r["got"] and not r["truth"])
    fr = sum(1 for r in rows if r["truth"] and not r["got"])
    return fa, fr, len(rows)


def compare(paths: Sequence[str]) -> str:
    arms: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        arms[d["meta"]["label"]] = d
    labels = [k for k in ("OLD", "NEW") if k in arms] or list(arms)
    lines: List[str] = []
    groups = []
    for lab in labels:
        for r in arms[lab]["rows"]:
            if r["group"] not in groups:
                groups.append(r["group"])
    cols = [(lab, m) for lab in labels for m in MODELS]
    lines.append("false accepts / false rejects / cases, per arm (tree, check model)")
    lines.append(f"{'group':<26}" + "".join(f"{lab + ' ' + m:>14}" for lab, m in cols))
    for g in groups + ["all"]:
        cells = []
        for lab, m in cols:
            rows = [r for r in arms[lab]["rows"] if r["model"] == m and (g == "all" or r["group"] == g)]
            fa, fr, n = _errors(rows)
            cells.append(f"{fa}/{fr}/{n}")
        lines.append(f"{g:<26}" + "".join(f"{c:>14}" for c in cells))
    lines.append("")
    for lab in labels:
        for m in MODELS:
            wrong = [r for r in arms[lab]["rows"] if r["model"] == m and r["got"] != r["truth"]]
            calls = sum(r.get("model_calls", 0) for r in arms[lab]["rows"] if r["model"] == m)
            lines.append(f"{lab} model={m}: {len(wrong)} wrong, {calls} model question(s)")
            for r in wrong:
                kind = "false accept" if r["got"] else "false reject"
                lines.append(f"  {kind}: {r['group']}: {r['case']} ({r['why'][:110]})")
    if "OLD" in arms and "NEW" in arms:
        old = {(r["group"], r["case"], r["model"]): r for r in arms["OLD"]["rows"]}
        new_fa = [r for r in arms["NEW"]["rows"] if r["model"] == "off" and r["got"]
                  and not r["truth"] and not old.get((r["group"], r["case"], "off"), {}).get("got")]
        lines.append("")
        lines.append(f"new false accepts (NEW without a model, against OLD): {len(new_fa)}"
                     + "".join(f"\n  {r['group']}: {r['case']}" for r in new_fa))
        known = [("positions/prefix-site", "ja inside"), ("positions/prefix-site", "ja end"),
                 ("folding (proof)", "el capitals without accents"),
                 ("folding (proof)", "ar Arabic-Indic digits")]
        newr = {(r["group"], r["case"]): r for r in arms["NEW"]["rows"] if r["model"] == "off"}
        answered = [k for k in known if k in newr and newr[k]["got"] == newr[k]["truth"]]
        lines.append(f"known-issue cases answered (NEW without a model): {len(answered)}/{len(known)}"
                     + "".join(f"\n  {g}: {c}" for g, c in known if (g, c) not in answered))
        pos = sorted({(r["group"], r["case"].split()[0], json.dumps(r.get("positions")))
                      for r in arms["NEW"]["rows"] if r["group"].startswith("positions")})
        lines.append("NEW calibrated positions: " + "; ".join(
            f"{g.split('/')[1]} {lang}: {p}" for g, lang, p in pos))
        lines.append(f"seconds: " + ", ".join(f"{lab} {arms[lab]['meta']['seconds']}" for lab in labels))
        lines.append("PASS" if not new_fa and len(answered) == len(known) else "FAIL")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-matching-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "matching_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, tree, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{label}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{label}.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", f"robots_{label}.json"),
            "ANYUSE_MODEL_CHECKS": "none",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--label", label,
                               "--tree", tree, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
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
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
