"""Offline A/B bench: compile and prove a search whose labels are not ASCII.

The question: when a site's result labels are written in Japanese, Russian,
Greek, Arabic or another non-ASCII script, does the compiler find the request,
does the fresh-input proof pass, and does the compiled tool answer a value
nobody recorded? And does every existing recording compile and prove exactly
as before?

Each corpus is the quickstart fixture (``examples/quickstart/fixture_site.py``)
with its WORDS and SUFFIXES swapped for one script: a search for a word lists
six names made of the word and a suffix. The recordings have the quickstart's
layout (one to compile from, two held out), and the "site" is an in-memory
transport that serves the same lists to the proof and the replay. Nothing
opens a socket, starts a browser or calls a model.

Forms (how the page and the response write the same strings):

* ``same``: both in NFC;
* ``nfd_api``: the response in NFD, the page in NFC;
* ``nfd_ui``: the page in NFD, the response in NFC;
* ``fullwidth_ui``: the page shows ASCII letters and digits in full-width forms;
* ``bidi_ui``: the page wraps each right-to-left label in direction marks.

A form is run only on the corpora where it changes a string.

Shapes:

* ``json``: the quickstart page, whose script loads ``/api/search?q=``;
* ``html``: the results are in the served page only (no JSON request);
* ``cards``: as ``html``, each result a card with its name in a heading and a
  rating line of digits under it (the result contract reads the heading as
  the card's identity);
* ``sparse``: as ``json``, but the held-out pages show two results each, so
  the proof's sparse contract (every result names the query) decides.

Per case: ``compiled`` (a template came out), ``proved`` (the fresh-input
proof passed), ``replay`` (the proved template, run on a fifth word nobody
recorded, returns its six names), ``sparse_replay`` (run on a sixth word whose
search has two results, accepted by the result contract and correct), and two
decoys that must FAIL: ``decoy_proof`` (a response that ignores the query must
not prove) and ``decoy_sparse`` (a two-item answer for another word must be
refused at run time).

The ``testdata`` section compiles and proves every existing recording family
(``testdata/compile``, ``testdata/attach_crates``, ``testdata/attach_detail``)
and runs the result contract on ``testdata/runtime_contract`` pages; its
canonical output (timings removed) must be byte-identical between the trees.

Usage::

    python tests/bench/unicode_compile_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--out <dir>]
    python tests/bench/unicode_compile_bench.py compare OLD.json NEW.json

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per tree. Each run's PYTHONPATH is a pkgroot
directory holding an ``anyuse`` symlink to the tree, then the tree, then its
``tools/guarded_tests`` (whose sitecustomize refuses sockets). HOME and every
AnyUse data path point into ``--out``, and no ``ANYUSE_*`` variable is passed
through.
"""
from __future__ import annotations

import argparse
import copy
import glob
import gzip
import hashlib
import html as _html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

HOST = "fix.test"

#: script -> (separator between word and suffix, six words, six suffixes)
CORPORA: Dict[str, Tuple[str, List[str], List[str]]] = {
    "en": ("-", ["river", "stone", "cloud", "maple", "harbor", "tulip"],
           ["core", "cli", "kit", "lite", "sdk", "tools"]),
    "de": (" ", ["München", "Köln", "Düsseldorf", "Nürnberg", "Görlitz", "Lübeck"],
           ["Bahnhof", "Brücke", "Straße", "Schloss", "Gärten", "Museum"]),
    "vi": (" ", ["Hà Nội", "Đà Nẵng", "Huế", "Hội An", "Cần Thơ", "Nha Trang"],
           ["Chợ", "Phố Cổ", "Bến Xe", "Sân Bay", "Bảo Tàng", "Công Viên"]),
    "ru": (" ", ["Москва", "Казань", "Самара", "Пермь", "Томск", "Псков"],
           ["Центр", "Вокзал", "Кремль", "Набережная", "Музей", "Парк"]),
    "el": (" ", ["Αθήνα", "Πάτρα", "Ηράκλειο", "Λάρισα", "Βόλος", "Χανιά"],
           ["Κέντρο", "Λιμάνι", "Μουσείο", "Πλατεία", "Σταθμός", "Πάρκο"]),
    "ja": ("", ["東京", "大阪", "京都", "札幌", "横浜", "神戸"],
           ["タワー", "駅", "大学", "ドーム", "市役所", "ガイド"]),
    "ja-latin": ("", ["iphone", "galaxy", "pixel", "xperia", "aquos", "arrows"],
                 ["ケース", "フィルム", "充電器", "カバー", "ストラップ", "スタンド"]),
    "zh": ("", ["北京", "上海", "广州", "深圳", "成都", "杭州"],
           ["大学", "机场", "火车站", "博物馆", "公园", "图书馆"]),
    "ko": ("", ["서울", "부산", "대구", "인천", "광주", "대전"],
           ["역", "대학교", "공항", "시청", "타워", "시장"]),
    "ar": (" ", ["القاهرة", "دبي", "الرياض", "عمان", "بيروت", "تونس"],
           ["مكتبة", "جامعة", "متحف", "مطار", "حديقة", "سوق"]),
    "he": (" ", ["ירושלים", "חיפה", "אילת", "נתניה", "עכו", "צפת"],
           ["מוזיאון", "ספרייה", "תחנה", "פארק", "שוק", "נמל"]),
    "th": ("", ["กรุงเทพ", "เชียงใหม่", "ภูเก็ต", "พัทยา", "ขอนแก่น", "อยุธยา"],
           ["ตลาด", "วัด", "สนามบิน", "โรงแรม", "พิพิธภัณฑ์", "สวน"]),
    "hi": (" ", ["दिल्ली", "मुंबई", "कोलकाता", "चेन्नई", "जयपुर", "पुणे"],
           ["संग्रहालय", "बाज़ार", "स्टेशन", "विश्वविद्यालय", "क़िला", "पार्क"]),
}
RTL = {"ar", "he"}
FORMS = ["same", "nfd_api", "nfd_ui", "fullwidth_ui", "bidi_ui"]
SHAPES = ["json", "html", "cards", "sparse"]
#: shapes whose results are only in the served page
PAGE_ONLY = ("html", "cards")
#: word roles, by index into a corpus's words
COMPILE, FRESH1, FRESH2, REPLAY, SPARSE, DECOY = range(6)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in s)


def bidi(s: str) -> str:
    return "‏" + s + "‏"


def ui_form(form: str, s: str) -> str:
    if form == "nfd_ui":
        return unicodedata.normalize("NFD", s)
    if form == "fullwidth_ui":
        return fullwidth(s)
    if form == "bidi_ui":
        return bidi(s)
    return nfc(s)


def api_form(form: str, s: str) -> str:
    return unicodedata.normalize("NFD", s) if form == "nfd_api" else nfc(s)


def names(corpus: str, word: str, n: int = 6) -> List[str]:
    sep, words, suffixes = CORPORA[corpus]
    k = words.index(word) % len(suffixes)
    return [nfc(f"{word}{sep}{s}") for s in suffixes[k:] + suffixes[:k]][:n]


def applies(corpus: str, form: str) -> bool:
    """Does ``form`` change any string of ``corpus``?"""
    if form == "bidi_ui":
        return corpus in RTL
    _sep, words, _suffixes = CORPORA[corpus]
    every = [n for w in words for n in names(corpus, w)] + words
    if form == "same":
        return True
    fn = ui_form if form.endswith("_ui") else api_form
    return any(fn(form, s) != nfc(s) for s in every)


# ---------------------------------------------------------------------------
# the fixture site and its recordings
# ---------------------------------------------------------------------------

def api_body(rows: List[str]) -> str:
    return json.dumps({"count": len(rows), "results": [{"name": n, "downloads": 1000 - 7 * i}
                                                      for i, n in enumerate(rows)]},
                      ensure_ascii=False)


def page_body(q: str, rows: List[str], script: bool, cards: bool = False) -> str:
    if cards:
        lis = "".join(f'<li class="result"><h3><a href="/pkg/{i}">{_html.escape(n)}</a></h3>'
                      f'<p class="meta">★ 4.{i} · {120 + i}</p></li>' for i, n in enumerate(rows))
    else:
        lis = "".join(f'<li class="result"><a href="/pkg/{i}">{_html.escape(n)}</a></li>'
                      for i, n in enumerate(rows))
    js = (f"<script>fetch('/api/search?q='+encodeURIComponent({json.dumps(q)}))</script>"
          if script else "")
    return (f"<!doctype html><html><head><title>Search results for {_html.escape(q)}"
            f" - Fixture</title></head><body><nav><a href='/'>Fixture</a></nav>"
            f"<form action='/search'><input name='q' value='{_html.escape(q)}'></form>"
            f"<ul id='results'>{lis}</ul>{js}</body></html>")


def page_url(q: str) -> str:
    return f"https://{HOST}/search?q={urllib.parse.quote(q)}"


def api_url(q: str) -> str:
    return f"https://{HOST}/api/search?q={urllib.parse.quote(q)}"


class Site:
    """The fixture site for one corpus and form; ``rows`` overrides what a query lists."""

    def __init__(self, corpus: str, form: str, shape: str, rows: Optional[Dict[str, List[str]]] = None):
        self.corpus, self.form, self.shape = corpus, form, shape
        self.rows = rows or {}
        self.calls: List[str] = []

    def listing(self, q: str) -> List[str]:
        q = nfc(q)
        if q in self.rows:
            return self.rows[q]
        _sep, words, _s = CORPORA[self.corpus]
        for w in words:
            if nfc(w).casefold() == q.casefold():
                return names(self.corpus, w)
        return []

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        self.calls.append(url)
        sp = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(sp.query).get("q", [""])[0]
        rows = [api_form(self.form, n) for n in self.listing(q)]
        if sp.path == "/api/search" and self.shape not in PAGE_ONLY:
            return ApiResponse(200, {"Content-Type": "application/json"}, api_body(rows))
        if sp.path == "/search":
            return ApiResponse(200, {"Content-Type": "text/html; charset=utf-8"},
                               page_body(q, rows, script=self.shape not in PAGE_ONLY,
                                         cards=self.shape == "cards"))
        return ApiResponse(404, {"Content-Type": "text/plain"}, "not found")


def recording(site: Site, value: str, tag: str, rows: Optional[List[str]] = None) -> dict:
    """What ``examples/quickstart/record_fixture.py`` writes for one search."""
    shown = rows if rows is not None else site.listing(value)
    served = [api_form(site.form, n) for n in shown]
    page = {"url": page_url(value), "method": "GET", "status": 200, "mime": "text/html",
            "body": page_body(value, served, script=site.shape not in PAGE_ONLY,
                              cards=site.shape == "cards")}
    page["bytes"] = len(page["body"])
    network = [page]
    if site.shape not in PAGE_ONLY:
        api = {"url": api_url(value), "method": "GET", "status": 200, "mime": "application/json",
               "body": api_body(served)}
        api["bytes"] = len(api["body"])
        network.append(api)
    items = [ui_form(site.form, n) for n in shown]
    return {"workflow": "fixture_search", "tag": tag, "site": HOST,
            "param": {"name": "q", "value": value}, "final_url": page_url(value),
            "ui": {"items": items, "text": "\n".join(items)}, "network": network}


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------

def _same(got: Any, want: List[str]) -> bool:
    if not isinstance(got, list) or len(got) != len(want):
        return False
    return all(nfc(str(g)).casefold() == nfc(w).casefold() for g, w in zip(got, want))


def run_case(corpus: str, form: str, shape: str) -> Dict[str, Any]:
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    _sep, words, _suffixes = CORPORA[corpus]
    site = Site(corpus, form, shape)
    row: Dict[str, Any] = {"corpus": corpus, "form": form, "shape": shape}
    rec = recording(site, words[COMPILE], "rec")
    if shape == "sparse":
        fresh_rows = {words[i]: names(corpus, words[i], 2) for i in (FRESH1, FRESH2)}
        site.rows.update({nfc(k): v for k, v in fresh_rows.items()})
        fresh = [recording(site, w, f"fresh{j + 1}", fresh_rows[w])
                 for j, w in enumerate((words[FRESH1], words[FRESH2]))]
    else:
        fresh = [recording(site, words[i], f"fresh{j + 1}") for j, i in enumerate((FRESH1, FRESH2))]
    try:
        compiled, proof = compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=site),
                                             preflight=lambda _c: "")
    except Exception as exc:                                # noqa: BLE001
        row.update(compiled=False, proved=False, error=f"{type(exc).__name__}: {exc}"[:200])
        return row
    tmpl = compiled.template
    row["compiled"] = tmpl is not None
    row["proved"] = bool(proof.passed)
    row["reason"] = (proof.reason or compiled.reason or "")[:200]
    row["extract_kind"] = getattr(tmpl, "extract_kind", "") if tmpl is not None else ""
    row["replay"] = row["sparse_replay"] = False
    row["decoy_sparse_refused"] = None
    if tmpl is not None and proof.passed:
        ex = ApiStepExecutor(transport=site)
        out = ex.execute(tmpl, {"q": words[REPLAY]})
        row["replay"] = bool(out.get("success")) and _same(
            (out.get("extracted") or {}).get("items"), names(corpus, words[REPLAY]))
        two = names(corpus, words[SPARSE], 2)
        site.rows[nfc(words[SPARSE])] = two
        out = ex.execute(tmpl, {"q": words[SPARSE]})
        row["sparse_replay"] = bool(out.get("success")) and _same(
            (out.get("extracted") or {}).get("items"), two)
        if not row["sparse_replay"]:
            row["sparse_replay_error"] = str(out.get("error") or "")[:200]
        # another word's two results served for this word: the run must be refused
        site.rows[nfc(words[DECOY])] = names(corpus, words[COMPILE], 2)
        out = ex.execute(tmpl, {"q": words[DECOY]})
        row["decoy_sparse_refused"] = not out.get("success")
    # a response that ignores the query: the proof must fail
    deaf = Site(corpus, form, shape, rows={nfc(w): names(corpus, words[COMPILE]) for w in words})
    try:
        _c2, p2 = compile_and_verify(recording(deaf, words[COMPILE], "rec"),
                                     [recording(site, words[i], f"fresh{j + 1}",
                                                names(corpus, words[i]))
                                      for j, i in enumerate((FRESH1, FRESH2))],
                                     executor=ApiStepExecutor(transport=deaf),
                                     preflight=lambda _c: "")
        row["decoy_proof_failed"] = not p2.passed
    except Exception as exc:                                # noqa: BLE001
        row["decoy_proof_failed"] = True
        row["decoy_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return row


# ---------------------------------------------------------------------------
# existing recordings: the output must not change
# ---------------------------------------------------------------------------

def _timing(key: str) -> bool:
    """Timings and timestamps, which differ between any two runs."""
    return key.endswith(("_ms", "_s", "_at")) or "_ms_" in key or key == "at"


def _canon(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _canon(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))
                if not _timing(str(k))}
    if isinstance(x, (list, tuple)):
        return [_canon(v) for v in x]
    if isinstance(x, set):
        return sorted(_canon(v) for v in x)
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if hasattr(x, "to_dict"):
        return _canon(x.to_dict())
    return repr(x)


#: keys a new contract adds (query positions and why)
_NEW_CONTRACT_KEYS = ("query_positions", "query_calibration")


def _comparable(x: Any) -> Any:
    """``x`` without the contract keys this change adds and with the normaliser's
    version of a learned empty page masked."""
    if isinstance(x, dict):
        return {k: ("*" if k == "norm" else _comparable(v)) for k, v in x.items()
                if k not in _NEW_CONTRACT_KEYS}
    if isinstance(x, list):
        return [_comparable(v) for v in x]
    return x


def _first_difference(a: Any, b: Any, path: str = "") -> str:
    """Where two canonical outputs first differ, and both values there."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                return _first_difference(a.get(k), b.get(k), f"{path}.{k}")
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_difference(x, y, f"{path}[{i}]")
    shown = lambda v: json.dumps(v, ensure_ascii=False)[:160]   # noqa: E731
    return f"{path or '.'}: OLD {shown(a)} NEW {shown(b)}"


def _compiled_view(compiled, proof) -> Dict[str, Any]:
    tmpl = getattr(compiled, "template", None)
    return {"status": getattr(compiled, "status", ""), "reason": getattr(compiled, "reason", ""),
            "template": tmpl.to_dict() if tmpl is not None else None,
            "alternatives": [t.to_dict() for t in getattr(compiled, "alternatives", []) or []],
            "proof": proof.to_dict() if proof is not None else None}


def testdata_outputs(tree: str) -> Dict[str, Any]:
    from anyuse.execution import attach as A
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_compile import compile_recording, normalise
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
    from anyuse.execution.api_verify import compile_and_verify
    from anyuse.execution.html_extract import extract
    td = os.path.join(tree, "testdata")
    out: Dict[str, Any] = {}

    def load(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    comp = os.path.join(td, "compile")
    for p in sorted(glob.glob(os.path.join(comp, "*.json"))):
        res = compile_recording(load(p))
        out[f"compile/{os.path.basename(p)}"] = _compiled_view(res, None)

    for fam in ("crates_search", "crates_detail"):
        rec = load(os.path.join(comp, f"{fam}.rec.json"))
        fresh = [load(os.path.join(comp, f"{fam}.fresh{i}.json")) for i in (1, 2)]
        bodies = {}
        for r in [rec, *fresh]:
            for n in r.get("network") or []:
                if n.get("body"):
                    sp = urllib.parse.urlsplit(n["url"])
                    bodies[(sp.netloc, sp.path, urllib.parse.unquote(sp.query))] = n

        class Recorded:
            def send(self, method, url, headers, body, timeout_s):
                sp = urllib.parse.urlsplit(url)
                hit = bodies.get((sp.netloc, sp.path, urllib.parse.unquote(sp.query)))
                if hit is None:
                    for (h, path, _q), n in bodies.items():
                        if h == sp.netloc and path == sp.path:
                            vals = {v for _k, v in urllib.parse.parse_qsl(sp.query)}
                            if vals & {v for _k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(n["url"]).query)}:
                                hit = n
                                break
                if hit is None:
                    return ApiResponse(404, {"Content-Type": "text/plain"}, "not recorded")
                return ApiResponse(200, {"Content-Type": hit.get("mime") or "application/json"},
                                   hit["body"])
        c, p = compile_and_verify(rec, fresh, executor=ApiStepExecutor(transport=Recorded()),
                                  preflight=lambda _c: "")
        out[f"prove/{fam}"] = _compiled_view(c, p)

    crates_dir = os.path.join(td, "attach_crates")
    sessions = [load(p) for p in sorted(glob.glob(os.path.join(crates_dir, "*.json")))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]

    class Captured:
        def send(self, method, url, headers, body, timeout_s):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
            return ApiResponse(200, {"Content-Type": "application/json"}, captured.get(q, "{}"))
    c, p, report, _recs = A.compile_sessions(copy.deepcopy(sessions),
                                             executor=ApiStepExecutor(transport=Captured()),
                                             preflight=lambda _c: "")
    out["attach/crates"] = {**_compiled_view(c, p), "report": report}

    detail = [load(p) for p in sorted(glob.glob(os.path.join(td, "attach_detail", "session_*.json")))]
    pages = {}
    for s in detail:
        for r in s["network"]:
            if r.get("resource_type") == "Document" and r.get("body"):
                pages[r["url"]] = r["body"]

    class Docs:
        def send(self, method, url, headers, body, timeout_s):
            if url not in pages:
                return ApiResponse(404, {"Content-Type": "text/plain"}, "not recorded")
            return ApiResponse(200, {"Content-Type": "text/html"}, pages[url])
    c, p, report, _recs = A.compile_sessions(copy.deepcopy(detail), preflight=lambda _c: "",
                                             transport=Docs())
    out["attach/detail"] = {**_compiled_view(c, p), "report": report}

    with gzip.open(os.path.join(td, "runtime_contract", "saved_pages.json.gz"), "rt") as fh:
        saved = json.load(fh)["pages"]
    selectors = {"msys2": "div.card-body.overflow-auto table.table.table-hover.table-sm tbody tr",
                 "cheese": ("div.center-block.col-md-9 div.catalog.internal.search-results "
                            "div.product-list.product-list-category div.product-item")}
    contract = RC.new_contract(RequestTemplate(url="https://h.test/s", query={"q": "{q}"},
                                               params=["q"], extract={"items": "x"},
                                               extract_kind="html", shape="list"))
    for name, page in sorted(saved.items()):
        sel = selectors[name.split("_")[0]]
        items = extract(page["html"], sel)
        v = RC.check_html(contract, page["html"], sel, {"q": page["value"]}, items)
        keys_ok = [RC.key_names_query(i, page["value"]) for i in items]
        out[f"contract/{name}"] = {"items": items, "verdict": v.to_dict(), "keys": keys_ok,
                                   "empty": list(RC.empty_evidence_page(page["html"], page["value"]))}
    return _canon(out)


def _strings(x: Any, out: List[str]) -> None:
    if isinstance(x, dict):
        for k, v in x.items():
            out.append(str(k))
            _strings(v, out)
    elif isinstance(x, list):
        for v in x:
            _strings(v, out)
    elif isinstance(x, str):
        out.append(x)
        if x.lstrip()[:1] in ("{", "["):
            try:
                _strings(json.loads(x), out)
            except ValueError:
                pass
        elif "<" in x:
            out.extend(t for t in _html.unescape(x).replace(">", "<").split("<") if t.strip())


def testdata_strings(tree: str) -> Dict[str, List[Any]]:
    """``[is ASCII, normalise(string)]`` for every string in the recordings and saved
    pages, keyed by the string's digest."""
    from anyuse.execution.api_compile import normalise
    td = os.path.join(tree, "testdata")
    found: List[str] = []
    for p in sorted(glob.glob(os.path.join(td, "compile", "*.json"))
                    + glob.glob(os.path.join(td, "attach_crates", "*.json"))
                    + glob.glob(os.path.join(td, "attach_detail", "*.json"))):
        with open(p, encoding="utf-8") as fh:
            _strings(json.load(fh), found)
    with gzip.open(os.path.join(td, "runtime_contract", "saved_pages.json.gz"), "rt") as fh:
        _strings(json.load(fh), found)
    return {hashlib.sha256(t.encode()).hexdigest()[:20]: [t.isascii(), normalise(t)]
            for t in set(found)}


def normalise_cost(tree: str, rounds: int = 20) -> Dict[str, float]:
    """Seconds per round of ``normalise`` over the testdata strings, and over the
    corpora's labels."""
    from anyuse.execution.api_compile import normalise
    td = os.path.join(tree, "testdata")
    found: List[str] = []
    for p in sorted(glob.glob(os.path.join(td, "compile", "*.json"))
                    + glob.glob(os.path.join(td, "attach_crates", "*.json"))):
        with open(p, encoding="utf-8") as fh:
            _strings(json.load(fh), found)
    labels = [n for c, (_sep, words, _s) in CORPORA.items() for w in words for n in names(c, w)]
    out = {}
    for key, pool in (("testdata", found), ("labels", labels * 20)):
        t0 = time.perf_counter()
        for _ in range(rounds):
            for t in pool:
                normalise(t)
        out[key + "_s_per_round"] = round((time.perf_counter() - t0) / rounds, 5)
    out["testdata_strings"], out["label_strings"] = len(found), len(labels) * 20
    return out


# ---------------------------------------------------------------------------
# one tree
# ---------------------------------------------------------------------------

def run_arm(label: str, tree: str, out_path: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    rows: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    for corpus in CORPORA:
        for form in FORMS:
            if not applies(corpus, form):
                continue
            for shape in SHAPES:
                rows.append(run_case(corpus, form, shape))
    cases_s = time.monotonic() - t0
    t1 = time.monotonic()
    testdata = testdata_outputs(tree)
    testdata_s = time.monotonic() - t1
    strings = testdata_strings(tree)
    cost = normalise_cost(tree)
    blob = json.dumps(testdata, sort_keys=True, ensure_ascii=False)
    meta = {"label": label, "tree": tree, "anyuse": real, "cases_s": round(cases_s, 2),
            "testdata_s": round(testdata_s, 2), "normalise_cost": cost,
            "testdata_sha256": hashlib.sha256(blob.encode()).hexdigest()}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "rows": rows, "testdata": testdata, "strings": strings}, fh,
                  ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

def _rate(rows: List[Dict[str, Any]], key: str) -> str:
    n = sum(1 for r in rows if r.get(key))
    return f"{n}/{len(rows)}"


def compare(paths: List[str]) -> str:
    arms = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        arms[d["meta"]["label"]] = d
    lines: List[str] = []
    labels = [k for k in ("OLD", "NEW") if k in arms] or list(arms)
    keys = ["compiled", "proved", "replay", "sparse_replay"]
    lines.append(f"{'corpus':<9} {'form':<13} " + "  ".join(
        f"{lab + ' ' + k:>20}" for k in keys for lab in labels))
    groups: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = {}
    for lab in labels:
        for r in arms[lab]["rows"]:
            groups.setdefault((r["corpus"], r["form"]), {}).setdefault(lab, []).append(r)
    for (corpus, form), by in groups.items():
        lines.append(f"{corpus:<9} {form:<13} " + "  ".join(
            f"{_rate(by.get(lab, []), k):>20}" for k in keys for lab in labels))
    lines.append("")
    for lab in labels:
        rows = arms[lab]["rows"]
        latin = [r for r in rows if r["corpus"] == "en" and r["form"] == "same"]
        other = [r for r in rows if not (r["corpus"] == "en" and r["form"] == "same")]
        decoys = [r for r in rows if r.get("decoy_sparse_refused") is not None]
        lines.append(f"{lab}: ASCII control (en/same) proved {_rate(latin, 'proved')}; "
                     f"every other case compiled {_rate(other, 'compiled')}, proved "
                     f"{_rate(other, 'proved')}, replay {_rate(other, 'replay')}, sparse replay "
                     f"{_rate(other, 'sparse_replay')}")
        lines.append(f"{lab}: decoy proof failed {_rate(rows, 'decoy_proof_failed')}; "
                     f"decoy sparse answer refused {_rate(decoys, 'decoy_sparse_refused')} "
                     f"(of the proved cases); cases {arms[lab]['meta']['cases_s']} s, testdata "
                     f"{arms[lab]['meta']['testdata_s']} s")
        nc = arms[lab]["meta"].get("normalise_cost") or {}
        lines.append(f"{lab}: normalise {nc.get('testdata_s_per_round')} s per pass over "
                     f"{nc.get('testdata_strings')} testdata strings, "
                     f"{nc.get('labels_s_per_round')} s over {nc.get('label_strings')} corpus labels")
    if "OLD" in arms and "NEW" in arms:
        a, b = arms["OLD"]["testdata"], arms["NEW"]["testdata"]
        same = json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True,
                                                                               ensure_ascii=False)
        lines.append(f"testdata outputs byte-identical: {'yes' if same else 'NO'} "
                     f"({len(a)} entries; sha256 {arms['OLD']['meta']['testdata_sha256'][:12]} vs "
                     f"{arms['NEW']['meta']['testdata_sha256'][:12]})")
        if not same:
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    lines.append(f"  differs: {k}")
            # a contract's query positions and the normaliser's version are new or bumped
            # by design; anything else that differs is listed with what changed
            ca, cb = _comparable(a), _comparable(b)
            rest = [k for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)]
            lines.append(f"testdata outputs identical apart from the contract's query positions and "
                         f"the normaliser version: {'yes' if not rest else 'NO'}")
            for k in rest:
                lines.append(f"  differs: {k}: {_first_difference(ca.get(k), cb.get(k))}")
        sa, sb = arms["OLD"].get("strings") or {}, arms["NEW"].get("strings") or {}
        changed = [k for k in sa if sb.get(k) != sa[k]]
        ascii_changed = [k for k in changed if sa[k][0]]
        lines.append(f"normalise over the {len(sa)} distinct testdata strings "
                     f"({sum(1 for v in sa.values() if not v[0])} with a non-ASCII character): "
                     f"{len(changed)} change, {len(ascii_changed)} of them ASCII")
        old_rows = {(r["corpus"], r["form"], r["shape"]): r for r in arms["OLD"]["rows"]}
        lost = [k for k, r in ((k, r) for k, r in ((
            (r["corpus"], r["form"], r["shape"]), r) for r in arms["NEW"]["rows"]))
                if old_rows.get(k, {}).get("proved") and not r.get("proved")]
        lines.append(f"cases OLD proved and NEW did not: {len(lost)}" + (
            "".join(f"\n  {k}" for k in lost)))
        fails = [r for r in arms["NEW"]["rows"] if not (r.get("proved") and r.get("replay")
                                                        and r.get("sparse_replay"))]
        for r in fails:
            lines.append(f"  NEW not fully passing: {r['corpus']}/{r['form']}/{r['shape']}: "
                         f"{r.get('reason') or r.get('sparse_replay_error') or r.get('error', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-unicode-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "unicode_compile_bench.py")
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
