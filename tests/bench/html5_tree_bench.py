"""Offline A/B bench: HTML5 implied end tags in the html_extract tree builder.

Three arms run the same corpus, each in its own process that imports ``anyuse``
from its own checkout:

* ``old``: a checkout without implied end tags (``--old-root``);
* ``raw``: this checkout (``--new-root``, default: the checkout holding this
  file), every html_extract call with ``implied_end_tags=True``: how a raw
  server response body is read (the HTML rung, step repair's own captures);
* ``dom``: this checkout with the defaults: how a DOM snapshot is read (attach,
  the browser replay, the learn loop). It must reproduce the old arm exactly.
  ``compile_html`` is the raw-body rung and always reads with the rules, so
  the dom arm does not run it.

Corpus, read from ``--data-root`` (default: the new checkout):

1. testdata/runtime_contract/saved_pages.json.gz: raw msys2 and cheese.com
   responses. Gold selector: the msys2 table row or the cheese.com product item.
2. In the list-detection fixtures (``LIST_FIXTURES``): opendata_swiss_facets.json.gz,
   two result pages. Gold: ``derive_list_items`` with the query values.
3. The ``*.sessions.json.gz`` files there, turned into recordings. Gold:
   ``derive_ground_truth``.
4. bbcgoodfood_listing.html.gz and poemhunter_listing.html.gz from there.
   Gold: the top repeated group (its row selector and row texts).
5. testdata/attach_crates and testdata/attach_detail sessions: snapshots
   (gold: ``derive_ground_truth``) and document bodies (gold: top repeated group).
6. testdata/consent/*.html: tree identity only.
7. A synthetic depth set: 50 to 300 rows with no end tags, and inline svg
   icons with as many self-closing paths.
8. A synthetic DOM set: snapshots of trees a script built and the HTML parser
   never would (a p holding a div or a list, an li inside an li).

Every page gets a variant without the end tags the HTML standard lets a page
omit, as an HTML minifier writes it: every ``</li> </dt> </dd> </option>
</optgroup> </td> </th> </tr> </thead> </tbody> </tfoot>``, and ``</p>`` where
the next tag starts a block that closes it (the standard's list, table
included) or ends a parent other than a, audio, del, ins, map, noscript or
video. Script, style, textarea, title and comments are left alone. A second
variant (``var_doc``) also drops ``</head>``, ``</body>`` and ``</html>`` where
a tag or the end of the page follows.

Gold is computed on the original pages with the old arm. Measured, old vs new:

a. item recall and precision of the gold selector, ``derive_selector`` and
   ``derive_selector_repeated`` on the variants (gold items as the values), and
   whether the page text (the root's ``Node.text``) is still the original's;
b. ``derive_list_items`` / ``derive_ground_truth`` per page family on the
   variants: selector found, same as gold, item recall;
c. ``compile_html`` on a one-document recording of the variant: a template, and
   its selector reaches recall >= 0.95. The template is then run through the
   executor (``ApiStepExecutor`` with an in-process transport serving the
   page): the call must read the items the compile read, and every template
   the raw arm compiles must carry ``html_parser: "html5"``;
d. pages the parser refuses (HtmlLimitError);
e. saved tools: a template compiled by the old arm (on the original and on the
   variant), with the result contract calibrated on that page, is run through
   the executor by each arm. It carries no ``html_parser``, so the call reads
   the tree it was compiled on: its items and outcome must not change. The raw
   arm also runs each one marked ``html5``, which counts what the marker
   prevents.

On the originals: a dump of the whole tree, old vs raw, and the raw builder's
count of implied closes. A page where no rule fires must give the old tree; the
bench fails if one does not. Pages where a rule fires are listed with their
ground truth, old vs new.

Usage::

    python tests/bench/html5_tree_bench.py --old-root /path/to/old/checkout \\
        [--json results.json]

No network. Not part of the test suite and not shipped in the wheel.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(os.path.dirname(HERE))
LIST_FIXTURES = os.path.join("testdata", "web_sessions")

MSYS2 = "div.card-body.overflow-auto table.table.table-hover.table-sm tbody tr"
CHEESE = ("div.center-block.col-md-9 div.catalog.internal.search-results "
          "div.product-list.product-list-category div.product-item")
#: item recall a page must reach to count as recovered
RECOVERED = 0.95
#: share of variant pages that must be recovered by the new arm
PASS_SHARE = 0.90
DEPTH_NS = (50, 127, 129, 300)
GOLD_LIMIT = 50
SIDES = ("orig", "var", "var_doc")

# ---------------------------------------------------------------------------
# the variant: optional end tags removed
# ---------------------------------------------------------------------------

_PSEUDO_TAG = re.compile(r"</?::[A-Za-z-]+\s*/?>")
_PROTECT = re.compile(r"<!--.*?-->|<(script|style|textarea|title)\b.*?</\1\s*>", re.S | re.I)
_END_TAG = re.compile(r"</(li|dt|dd|option|optgroup|td|th|tr|thead|tbody|tfoot|p)\s*>", re.I)
_NEXT_TAG = re.compile(r"\s*<(/?)([A-Za-z][A-Za-z0-9-]*)")
#: a p's end tag may be left out before these (WHATWG, "optional tags")
_P_BEFORE = {"address", "article", "aside", "blockquote", "details", "dialog", "div",
             "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
             "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav",
             "ol", "p", "pre", "search", "section", "table", "ul"}
#: ... and at the end of any parent except these
_P_PARENT_KEEP = {"a", "audio", "del", "ins", "map", "noscript", "video", "p"}


_DOC_END_TAG = re.compile(r"</(head|body|html)\s*>", re.I)
_NEXT_MARKUP = re.compile(r"\s*(<[A-Za-z/]|\Z)")


def strip_optional_end_tags(markup: str, document: bool = False):
    """``(variant, {tag: removed count})``. ``document`` also drops ``</head>``,
    ``</body>`` and ``</html>`` where a tag or the end of the page follows (not
    a comment, script or style)."""
    counts: collections.Counter = collections.Counter()

    def drop_doc(m, last):
        nxt = _NEXT_MARKUP.match(m.string, m.end())
        if not nxt or (not nxt.group(1) and not last):
            return m.group(0)
        counts[m.group(1).lower()] += 1
        return ""

    def chunk(text, last):
        text = _END_TAG.sub(drop, text)
        if document:
            text = _DOC_END_TAG.sub(lambda m: drop_doc(m, last), text)
        return text

    def drop(m):
        tag = m.group(1).lower()
        if tag == "p":
            nxt = _NEXT_TAG.match(m.string, m.end())
            if not nxt:
                return m.group(0)
            closing, name = nxt.group(1) == "/", nxt.group(2).lower()
            if closing and name in _P_PARENT_KEEP:
                return m.group(0)
            if not closing and name not in _P_BEFORE:
                return m.group(0)
            if not closing and name == "table":
                counts["p before table"] += 1
        counts[tag] += 1
        return ""

    text = _PSEUDO_TAG.sub("", markup or "")
    out, pos = [], 0
    for m in _PROTECT.finditer(text):
        out.append(chunk(text[pos:m.start()], False))
        out.append(m.group(0))
        pos = m.end()
    out.append(chunk(text[pos:], True))
    return "".join(out), dict(counts)


def depth_set():
    """``[(id, markup, container selector, row tag, items)]``: rows with no end tags."""
    out = []
    for n in DEPTH_NS:
        out.append((f"li/{n}", "<ul class=rows>" + "".join(
            f'<li><a href="/r{i}">Row {i} name</a> detail {i}' for i in range(n)) + "</ul>",
            "ul.rows", "li", [f"Row {i} name" for i in range(n)]))
        out.append((f"option/{n}", "<select name=s>" + "".join(
            f"<option value={i}>Choice {i} label" for i in range(n)) + "</select>",
            "select", "option", [f"Choice {i} label" for i in range(n)]))
        out.append((f"p/{n}", "<div class=text>" + "".join(
            f"<p>Paragraph {i} words <b>here</b>" for i in range(n)) + "</div>",
            "div.text", "p", [f"Paragraph {i} words here" for i in range(n)]))
        out.append((f"tr/{n}", "<table class=grid>" + "".join(
            f"<tr><td>Cell {i} a<td>Cell {i} b" for i in range(n)) + "</table>",
            "table.grid", "tr", [f"Cell {i} a Cell {i} b" for i in range(n)]))
        out.append((f"svg/{n}", "<div class=icon><svg viewBox='0 0 9 9'>" + "".join(
            f"<path d='M{i} 0h1'/>" for i in range(n)) + "</svg></div>",
            "div.icon svg", "path", [""] * n))
    return out


def dom_set():
    """``[(id, markup, selector, items)]``: serialised DOM snapshots of trees a
    script built. Each holds an element the HTML parser would have closed."""
    titles = [f"Card {i} title" for i in range(5)]
    links = [f"Link {i} text" for i in range(5)]
    inner = [f"Inner {i} row" for i in range(5)]
    return [
        ("p>div", "<section class=list>" + "".join(
            f'<p class="card"><div class="title">{t}</div><span>by x</span></p>'
            for t in titles) + "</section>", "p.card div.title", titles),
        ("p>ul", '<p class="intro">See also<ul class="links">' + "".join(
            f"<li>{t}</li>" for t in links) + "</ul></p>", "p.intro ul.links li", links),
        ("li>li", '<ul class="tree"><li class="outer">group' + "".join(
            f'<li class="inner">{t}</li>' for t in inner) + "</li></ul>",
         "li.outer li.inner", inner),
    ]


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------

def _gz_json(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def corpus(data: str, A):
    """``(pages, families)``. A page: id, markup, value, gold kind (``selector``,
    ``group``, ``family`` or ``identity``) and, for ``selector``, the gold
    selector. A family: id, kind (``list`` or ``recs``), member page ids and
    what to rebuild it from."""
    pages, families, seen = [], [], set()

    def add(pid, markup, kind, value="", sel="", family=""):
        digest = hashlib.sha256((markup or "").encode("utf-8", "ignore")).hexdigest()
        if kind == "identity" and digest in seen:
            return
        seen.add(digest)
        pages.append({"id": pid, "markup": markup or "", "kind": kind, "value": value,
                      "gold_sel": sel, "family": family})

    td = os.path.join(data, "testdata")
    saved = _gz_json(os.path.join(td, "runtime_contract", "saved_pages.json.gz"))["pages"]
    fam = collections.defaultdict(list)
    for name in sorted(saved):
        p = saved[name]
        site = "msys2" if name.startswith("msys2") else "cheese"
        add(f"saved/{name}", p["html"], "selector", p["value"],
            MSYS2 if site == "msys2" else CHEESE, family=f"saved/{site}")
        fam[site].append(f"saved/{name}")
    for site, ids in sorted(fam.items()):
        families.append({"id": f"saved/{site}", "kind": "list", "pages": ids})

    lists = os.path.join(data, LIST_FIXTURES)
    od = _gz_json(os.path.join(lists, "opendata_swiss_facets.json.gz"))
    ids = []
    for i, (markup, value) in enumerate(zip(od["pages"], od["values"])):
        add(f"opendata/{i}", markup, "family", value, family="opendata")
        ids.append(f"opendata/{i}")
    families.append({"id": "opendata", "kind": "list", "pages": ids})
    if od.get("before"):
        add("opendata/before", od["before"], "identity")

    def sessions_family(fid, sessions, values, names):
        recs = [A.session_to_recording(s, n, v, f"attach{i}")
                for i, (s, v, n) in enumerate(zip(sessions, values, names))]
        ids = []
        for i, r in enumerate(recs):
            add(f"{fid}/{i}", r.get("dom") or "", "family", values[i], family=fid)
            ids.append(f"{fid}/{i}")
        families.append({"id": fid, "kind": "recs", "pages": ids,
                         "sessions": sessions, "values": values, "names": names})
        for i, s in enumerate(sessions):
            for j, snap in enumerate(s.get("snapshots") or []):
                if snap.get("dom"):
                    add(f"{fid}/{i}/snap{j}", snap["dom"], "identity")
            for j, net in enumerate(s.get("network") or []):
                if net.get("body") and "html" in str(net.get("mime") or "").lower():
                    add(f"{fid}/{i}/doc{j}", net["body"], "group", values[i])

    for path in sorted(glob.glob(os.path.join(lists, "*.sessions.json.gz"))):
        f = _gz_json(path)
        name = os.path.basename(path).split(".")[0]
        sessions_family(f"web_sessions/{name}", f["sessions"], list(f["values"]),
                        ["q"] * len(f["sessions"]))
    for kind in ("attach_crates", "attach_detail"):
        sessions = []
        for path in sorted(glob.glob(os.path.join(td, kind, "session_*.json"))):
            with open(path, encoding="utf-8") as fh:
                sessions.append(json.load(fh))
        sessions_family(kind, sessions, [s["input"]["value"] for s in sessions],
                        [s["input"]["name"] for s in sessions])

    for name in ("bbcgoodfood_listing", "poemhunter_listing"):
        with gzip.open(os.path.join(lists, name + ".html.gz"), "rt",
                       encoding="utf-8") as fh:
            add(f"web_sessions/{name}", fh.read(), "group", "bench")
    for path in sorted(glob.glob(os.path.join(td, "consent", "*.html"))):
        with open(path, encoding="utf-8") as fh:
            add("consent/" + os.path.basename(path), fh.read(), "identity")
    return pages, families


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

def run_arm(ns):
    sys.dont_write_bytecode = True
    sys.path.insert(0, ns.pkgroot)
    from anyuse.execution import attach as A
    from anyuse.execution import html_extract as X
    from anyuse.execution import result_contract as RC
    from anyuse.execution.api_compile import compile_html, normalise, values_match
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate
    from anyuse.execution.learn_loop import derive_ground_truth

    # the raw arm reads every page as a raw server response; the old and dom
    # arms call with the defaults
    opt = {"implied_end_tags": True} if ns.arm == "raw" else {}
    started = time.time()
    gold_doc = None
    if ns.gold:
        with open(ns.gold, encoding="utf-8") as fh:
            gold_doc = json.load(fh)

    def match(a, b):
        return values_match(normalise(a), normalise(b))

    def rp(gold, got):
        if not gold:
            return None
        got = list(got or [])
        rec = sum(1 for g in gold if any(match(g, x) for x in got)) / len(gold)
        prec = (sum(1 for x in got if any(match(g, x) for g in gold)) / len(got)) if got else 0.0
        return {"recall": round(rec, 4), "precision": round(prec, 4), "n": len(got)}

    def guarded(fn, *a, **kw):
        try:
            return fn(*a, **kw), ""
        except Exception as exc:                    # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"[:200]

    def build(markup):
        """``(root, implied closes, error)`` exactly as parse() builds it."""
        if len(markup.encode("utf-8", "ignore")) > X.MAX_HTML_BYTES:
            return None, {}, "HtmlLimitError: bytes"
        b = X._TreeBuilder(**opt)
        try:
            b.feed(X._PSEUDO_TAG.sub("", markup))
            b.close()
        except X.HtmlLimitError as exc:
            return None, dict(getattr(b, "implied", {}) or {}), f"HtmlLimitError: {exc}"
        except Exception:                           # noqa: BLE001
            pass
        return b.root, dict(getattr(b, "implied", {}) or {}), ""

    def tree_hash(root):
        h = hashlib.sha256()
        for n in root.walk():
            h.update(json.dumps([n.tag, list(n.classes), n.node_id, list(n._text),
                                 list(n._text_at), len(n.children)]).encode("utf-8"))
        return h.hexdigest()

    def top_group(markup):
        root = X.parse(markup, **opt)
        groups = X.repeated_groups(root)
        if not groups:
            return {"sel": "", "items": []}
        rows = groups[0]
        return {"sel": X._row_selector(rows),
                "items": [t for t in (r.text() for r in rows) if t][:GOLD_LIMIT]}

    class Serve:
        """The transport: every request is answered with one page."""

        def __init__(self, body):
            self.body = body

        def send(self, method, url, headers, body, timeout_s):
            return ApiResponse(200, {"Content-Type": "text/html; charset=utf-8"}, self.body)

    def call(tmpl, markup, value):
        """A compiled tool run as at call time, on ``markup``."""
        res, err = guarded(ApiStepExecutor(transport=Serve(markup)).execute, tmpl,
                           {"q": value})
        res = res or {}
        items = (res.get("extracted") or {}).get("items")
        return {"success": bool(res.get("success")),
                "items": list(items) if isinstance(items, list) else [],
                "error": str(res.get("error") or err or "")[:200]}

    pages, families = corpus(ns.data_root, A)
    out = {"arm": ns.arm, "html_extract": os.path.realpath(X.__file__),
           "option": dict(opt), "pages": {}, "families": {}, "depth": {}, "dom_set": {}}
    # var: the optional end tags listed above; var_doc: also </head>, </body>, </html>
    variants = {"orig": {p["id"]: p["markup"] for p in pages}, "var": {}, "var_doc": {}}
    for p in pages:
        rec = {"family": p["family"], "kind": p["kind"], "value": p["value"],
               "bytes": len(p["markup"]), "orig": {}}
        for side, document in (("var", False), ("var_doc", True)):
            var, stripped = strip_optional_end_tags(p["markup"], document=document)
            variants[side][p["id"]] = var
            rec[side] = {"stripped": stripped}
        for side in SIDES:
            root, implied, err = build(variants[side][p["id"]])
            text = root.text() if root is not None else ""
            rec[side].update({"implied": implied, "error": err,
                              "hash": tree_hash(root) if root is not None else "",
                              "text_len": len(text),
                              "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        out["pages"][p["id"]] = rec

    # ground truth with this arm's reading, on the originals and on the variants
    for fam in families:
        fres = {}
        for side in ("orig", "var"):
            markups = [variants[side][pid] for pid in fam["pages"]]
            if fam["kind"] == "list":
                values = [out["pages"][pid]["value"] for pid in fam["pages"]]
                got, err = guarded(X.derive_list_items, markups, values=values, **opt)
                sel, _items = got if got else ("", [])
                per = []
                for m in markups:
                    items, err2 = guarded(X.extract, m, sel, limit=GOLD_LIMIT, **opt) if sel \
                        else ([], "")
                    per.append({"sel": sel, "items": items or [], "error": err or err2})
            else:
                recs = [A.session_to_recording(s, n, v, f"attach{i}") for i, (s, v, n) in
                        enumerate(zip(fam["sessions"], fam["values"], fam["names"]))]
                if side == "var":
                    for r in recs:
                        for key in ("dom", "before_dom"):
                            if r.get(key):
                                r[key] = strip_optional_end_tags(r[key])[0]
                sel, err = guarded(derive_ground_truth, recs, **opt)
                sel = sel or ""
                per = [{"sel": sel, "items": list((r.get("ui") or {}).get("items") or []),
                        "visible_only": bool(r.get("ground_truth_visible_only")),
                        "error": err} for r in recs]
            fres[side] = {"sel": per[0]["sel"] if per else "",
                          "error": per[0]["error"] if per else ""}
            for pid, g in zip(fam["pages"], per):
                # the family's answer; a saved page keeps its fixed selector as gold
                out["pages"][pid][side]["fam_gt"] = g
                out["pages"][pid][side]["gt"] = g
        out["families"][fam["id"]] = fres
    for p in pages:
        for side in ("orig", "var"):
            markup = variants[side][p["id"]]
            if p["kind"] == "selector":
                items, err = guarded(X.extract, markup, p["gold_sel"], limit=GOLD_LIMIT, **opt)
                out["pages"][p["id"]][side]["gt"] = {"sel": p["gold_sel"], "items": items or [],
                                                     "error": err}
            elif p["kind"] == "group":
                got, err = guarded(top_group, markup)
                out["pages"][p["id"]][side]["gt"] = dict(got or {"sel": "", "items": []},
                                                         error=err)

    def gold_of(pid):
        src = gold_doc["pages"][pid] if gold_doc else out["pages"][pid]
        return src["orig"].get("gt") or {}

    # a, c and the old arm's templates for e
    for p in pages:
        gold = gold_of(p["id"])
        items = gold.get("items") or []
        if not items:
            continue
        value = p["value"] or "bench"
        rec = out["pages"][p["id"]]
        for side in SIDES:
            markup = variants[side][p["id"]]
            m = {}
            got, err = guarded(X.extract, markup, gold["sel"], limit=GOLD_LIMIT,
                               visible_only=bool(gold.get("visible_only")), **opt)
            m["gold_sel"] = dict(rp(items, got or []), error=err)
            for name, fn in (("ds", X.derive_selector), ("dsr", X.derive_selector_repeated)):
                got, err = guarded(fn, markup, items, **opt)
                sel, cov, vals = got if got else ("", 0.0, [])
                m[name] = dict(rp(items, vals), sel=sel, error=err)
            if ns.arm == "dom":
                rec[side]["metrics"] = m
                continue
            url = "https://bench.test/search?q=" + urllib.parse.quote_plus(value)
            recording = {"param": {"name": "q", "value": value},
                         "ui": {"items": items, "text": ""},
                         "network": [{"url": url, "method": "GET", "status": 200,
                                      "mime": "text/html; charset=utf-8",
                                      "bytes": len(markup), "body": markup}]}
            got, err = guarded(compile_html, recording, items, "q", value)
            tmpl, why = got if got else (None, err)
            c = {"ok": False, "sel": "", "why": why}
            if tmpl is not None:
                sel = tmpl.extract.get("items", "")
                ext, err = guarded(X.extract, markup, sel, **opt)
                score = rp(items, ext or [])
                c = {"ok": score["recall"] >= RECOVERED, "sel": sel, "recall": score["recall"],
                     "why": err, "html_parser": tmpl.to_dict().get("html_parser", "")}
                # the same template at call time reads what the compile read
                now = call(tmpl, markup, value)
                c["call_same"] = now["success"] and now["items"] == (ext or [])
                c["call_error"] = now["error"]
                if not gold_doc and side != "var_doc":
                    # the saved tool: this template with its contract calibrated
                    # on this page, as a proof on it would store it
                    contract = RC.new_contract(tmpl)
                    v = RC.check_html(contract, markup, sel, {"q": value}, ext or [], **opt)
                    if v.key_signature and isinstance(v.max_keys_per_item, int):
                        contract["key_signature"] = v.key_signature
                        contract["max_keys_per_item"] = v.max_keys_per_item
                    if isinstance(v.record_depth, int):
                        contract["record_depth"] = v.record_depth
                    if isinstance(v.region_coverage, (int, float)) and \
                            v.region_coverage >= RC.REGION_COVERAGE:
                        contract["region_coverage_min"] = RC.REGION_COVERAGE
                    saved = tmpl.to_dict()
                    saved["expect"] = dict(saved["expect"], **{RC.CONTRACT_KEY: contract})
                    c["saved"] = {"template": saved, "items": ext or []}
            m["compile"] = c
            rec[side]["metrics"] = m

    # e: the old arm's saved tools, run through the executor by this arm (the
    # old arm runs its own: the baseline)
    for p in pages:
        old = (gold_doc or out)["pages"][p["id"]]
        for side in ("orig", "var"):
            saved = (((old[side].get("metrics") or {}).get("compile") or {}).get("saved"))
            if not saved:
                continue
            markup = variants[side][p["id"]]
            value = p["value"] or "bench"
            tmpl = RequestTemplate.from_dict(saved["template"])
            now = call(tmpl, markup, value)
            res = {"selector": tmpl.extract.get("items", ""),
                   "html_parser": saved["template"].get("html_parser", ""),
                   "items_saved": len(saved["items"]), "items_now": len(now["items"]),
                   "items_changed": now["items"] != saved["items"],
                   "success": now["success"], "error": now["error"]}
            if ns.arm == "raw":
                # the same tool marked html5: what the marker keeps it from
                forced = RequestTemplate.from_dict(dict(saved["template"],
                                                        html_parser=X.HTML5_PARSER))
                f = call(forced, markup, value)
                res["as_html5"] = {"items_changed": f["items"] != saved["items"],
                                   "success": f["success"], "error": f["error"]}
            out["pages"][p["id"]][side]["saved_rerun"] = res

    # d (synthetic): the depth set
    for did, markup, container, row_tag, items in depth_set():
        root, implied, err = build(markup)
        rows = 0
        if root is not None:
            boxes = X.select(root, container)
            rows = sum(1 for c in boxes[0].children if c.tag == row_tag) if boxes else 0
        out["depth"][did] = {"ok": not err, "error": err, "rows": rows, "want": len(items)}
    # the DOM set: what each arm reads from a script-built tree
    for did, markup, selector, items in dom_set():
        got, err = guarded(X.extract, markup, selector, **opt)
        out["dom_set"][did] = {"items": got or [], "want": items, "error": err}
    out["seconds"] = round(time.time() - started, 1)
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _r(m, key="recall"):
    if not m or m.get(key) is None:
        return None
    return m[key]


def _fmt(x):
    return "  -  " if x is None else f"{x:5.2f}"


def report(old, raw, dom):
    lines, checks = [], []
    pages = sorted(old["pages"])
    kinds = collections.Counter(old["pages"][p]["kind"] for p in pages)
    lines.append(f"corpus: {len(pages)} original pages ({dict(sorted(kinds.items()))}), "
                 f"{len(pages)} variants, {len(old['families'])} page families, "
                 f"{len(old['depth'])} synthetic depth cases, {len(old['dom_set'])} DOM cases")
    differ = [p for p in pages if sum(old["pages"][p]["var"]["stripped"].values())]
    lines.append(f"variants that differ from their original: {len(differ)}")
    removed = collections.Counter()
    for p in pages:
        removed.update(old["pages"][p]["var"]["stripped"])
    lines.append("end tags removed: " + ", ".join(f"{k} {v}" for k, v in sorted(removed.items())))
    closes = collections.Counter()
    doc_removed = collections.Counter()
    for p in pages:
        closes.update(raw["pages"][p]["var"]["implied"])
        doc_removed.update({k: v for k, v in old["pages"][p]["var_doc"]["stripped"].items()
                            if k in ("head", "body", "html")})
    lines.append("implied closes on the variants (raw arm): "
                 + (", ".join(f"{k} {v}" for k, v in sorted(closes.items())) or "none"))
    lines.append("var_doc variants also drop: "
                 + (", ".join(f"</{k}> {v}" for k, v in sorted(doc_removed.items())) or "none"))

    # a and c: old vs raw
    gold_pages = [p for p in pages if (old["pages"][p]["var"].get("metrics"))]
    lines.append("")
    lines.append(f"a/c. variant pages with gold items: {len(gold_pages)} (old vs raw)")
    lines.append(f"{'page':44s} {'strip':>5s} | gold-sel old raw | derive_sel old raw "
                 f"| repeated old raw | compile old raw")
    summary = {}
    for p in gold_pages:
        o, n = old["pages"][p]["var"]["metrics"], raw["pages"][p]["var"]["metrics"]
        lines.append(
            f"{p[:44]:44s} {sum(old['pages'][p]['var']['stripped'].values()):5d} | "
            f"{_fmt(_r(o['gold_sel']))} {_fmt(_r(n['gold_sel']))} | "
            f"{_fmt(_r(o['ds']))} {_fmt(_r(n['ds']))} | "
            f"{_fmt(_r(o['dsr']))} {_fmt(_r(n['dsr']))} | "
            f"{'yes' if o['compile']['ok'] else 'no':>5s} "
            f"{'yes' if n['compile']['ok'] else 'no':>3s}")

    def score(m, name):
        if name == "best":
            return max(_r(m["ds"]) or 0, _r(m["dsr"]) or 0)
        if name == "compile":
            return 1.0 if m["compile"]["ok"] else 0.0
        return _r(m[name]) or 0.0

    # a method is held to a variant page only where the old arm recovers the
    # original page with that method
    eligible = {name: [p for p in gold_pages
                       if score(old["pages"][p]["orig"]["metrics"], name) >= RECOVERED]
                for name in ("gold_sel", "ds", "dsr", "best", "compile")}
    for name in ("gold_sel", "ds", "dsr", "best", "compile"):
        row = {"eligible": len(eligible[name]), "all": len(gold_pages)}
        pool = eligible[name]
        for arm, doc in (("old", old), ("raw", raw)):
            for side in SIDES:
                vals = [score(doc["pages"][p][side]["metrics"], name) for p in pool]
                hit = sum(1 for v in vals if v >= RECOVERED)
                row[f"{arm}_{side}"] = {"pages": len(vals), "recovered": hit,
                                        "share": round(hit / len(vals), 4) if vals else None,
                                        "mean_recall": round(sum(vals) / len(vals), 4)
                                        if vals else None}
                if name in ("gold_sel", "ds", "dsr"):
                    precs = [_r(doc["pages"][p][side]["metrics"][name], "precision") or 0.0
                             for p in pool]
                    row[f"{arm}_{side}"]["mean_precision"] = \
                        round(sum(precs) / len(precs), 4) if precs else None
        summary[name] = row
    lines.append("")
    lines.append(f"recovered = item recall >= {RECOVERED}; each metric counts the pages where "
                 f"the old arm recovers the original with it (best = the better of "
                 f"derive_selector and derive_selector_repeated, as compile_html picks)")
    lines.append(f"{'metric':10s} recovered/eligible (mean recall/precision)")
    for name, row in summary.items():
        cells = []
        for key in ("old_orig", "raw_orig", "old_var", "raw_var", "old_var_doc", "raw_var_doc"):
            c = row[key]
            prec = f"/{c['mean_precision']:.2f}" if c.get("mean_precision") is not None else ""
            cells.append(f"{key} {c['recovered']}/{c['pages']} ({c['mean_recall']:.2f}{prec})"
                         if c["pages"] else f"{key} -")
        lines.append(f"{name:8s} [eligible {row['eligible']:2d} of {row['all']}] "
                     + " | ".join(cells[:2]))
        lines.append(f"{'':8s} {'':19s} " + " | ".join(cells[2:4]))
        lines.append(f"{'':8s} {'':19s} " + " | ".join(cells[4:]))
    best_raw = summary["best"]["raw_var"]
    ok = bool(best_raw["pages"]) and best_raw["share"] >= PASS_SHARE
    checks.append((f"raw arm recovers >= {RECOVERED} of gold items on >= {PASS_SHARE:.0%} of "
                   f"eligible variant pages (best of the two derivations)",
                   ok, f"{best_raw['recovered']}/{best_raw['pages']} "
                   f"(old: {summary['best']['old_var']['recovered']}/"
                   f"{summary['best']['old_var']['pages']})"))
    for name in ("ds", "dsr", "compile"):
        c = summary[name]["raw_var"]
        checks.append((f"same criterion for {name} alone", bool(c["pages"]) and
                       c["share"] >= PASS_SHARE,
                       f"{c['recovered']}/{c['pages']} (old: "
                       f"{summary[name]['old_var']['recovered']}/"
                       f"{summary[name]['old_var']['pages']})"))

    # c, at call time: the raw arm's own templates
    own = [(p, s, raw["pages"][p][s]["metrics"]["compile"]) for p in gold_pages for s in SIDES
           if raw["pages"][p][s]["metrics"]["compile"].get("sel")]
    marked = sum(1 for _p, _s, c in own if c.get("html_parser") == "html5")
    same = sum(1 for _p, _s, c in own if c.get("call_same"))
    lines.append("")
    lines.append(f"c at call time: the raw arm compiled {len(own)} templates, {marked} carry "
                 f"html_parser 'html5', {same} read at call time what the compile read")
    for p, s, c in own:
        if not c.get("call_same"):
            lines.append(f"   {p} [{s}]: {c['sel']!r}: {c.get('call_error') or 'items differ'}")
    checks.append(("every template the raw arm compiles carries html_parser 'html5' and "
                   "reads at call time what the compile read",
                   bool(own) and marked == len(own) and same == len(own),
                   f"{marked}/{len(own)} marked, {same}/{len(own)} same"))

    # b
    lines.append("")
    lines.append("b. page families (derive_list_items, or derive_ground_truth for sessions)")
    lines.append(f"{'family':34s} {'gold selector':52s} | var old: same / recall | "
                 f"var raw: same / recall | orig raw same")
    fam_rows = {}
    for fid in sorted(old["families"]):
        gsel = old["families"][fid]["orig"]["sel"]
        ids = [p for p in pages if old["pages"][p]["family"] == fid]
        row = {"gold": gsel}
        cells = []
        for arm, doc in (("old", old), ("raw", raw)):
            fsel = doc["families"][fid]["var"]["sel"]
            recs = []
            for p in ids:
                gi = old["pages"][p]["orig"]["fam_gt"]["items"]
                vi = doc["pages"][p]["var"]["fam_gt"]["items"]
                if gi:
                    recs.append(_r(_rp_plain(gi, vi)) or 0.0)
            mean = round(sum(recs) / len(recs), 4) if recs else None
            row[arm] = {"sel": fsel, "same": fsel == gsel, "mean_recall": mean}
            cells.append(f"{'yes' if fsel == gsel else 'NO ':>4s} / {_fmt(mean)}")
        row["raw_orig_same"] = raw["families"][fid]["orig"]["sel"] == gsel
        fam_rows[fid] = row
        lines.append(f"{fid[:34]:34s} {(gsel or '(none)')[:52]:52s} | {cells[0]:>17s} | "
                     f"{cells[1]:>17s} | {'yes' if row['raw_orig_same'] else 'NO'}")

    # identity and fired rules on the originals
    lines.append("")
    fired = [p for p in pages if raw["pages"][p]["orig"]["implied"]]
    silent_diff = [p for p in pages if not raw["pages"][p]["orig"]["implied"]
                   and raw["pages"][p]["orig"]["hash"] != old["pages"][p]["orig"]["hash"]]
    checks.append(("every original where no rule fires gives the old tree (raw arm)",
                   not silent_diff,
                   f"{len(pages) - len(fired)} silent originals, {len(silent_diff)} differ"))
    lines.append(f"originals where a rule fires (raw arm): {len(fired)} of {len(pages)}")
    fired_rows = {}
    for p in fired:
        o, n = old["pages"][p]["orig"], raw["pages"][p]["orig"]
        og, ng = o.get("gt") or {}, n.get("gt") or {}
        same_sel = og.get("sel") == ng.get("sel")
        same_items = og.get("items") == ng.get("items")
        fired_rows[p] = {"implied": n["implied"], "tree_same": o["hash"] == n["hash"],
                         "gt_sel_old": og.get("sel"), "gt_sel_raw": ng.get("sel"),
                         "gt_items_same": same_items,
                         "gt_items_old": (og.get("items") or [])[:5],
                         "gt_items_raw": (ng.get("items") or [])[:5]}
        if not og:
            lines.append(f"  {p}: closes {n['implied']}; tree identity only")
            continue
        sel_text = "same" if same_sel else f"{og.get('sel')!r} -> {ng.get('sel')!r}"
        lines.append(f"  {p}: closes {n['implied']}; ground truth selector {sel_text}, "
                     f"items {'same' if same_items else 'CHANGED'} "
                     f"({len(og.get('items') or [])} -> {len(ng.get('items') or [])})")
    gt_changed = [p for p, r in fired_rows.items()
                  if r["gt_sel_old"] is not None and
                  (r["gt_sel_old"] != r["gt_sel_raw"] or not r["gt_items_same"])]
    checks.append(("no fired original changes its ground truth (selector and items)",
                   not gt_changed, ", ".join(gt_changed) or "none"))

    # the DOM-snapshot arm against the old arm: every reading must be the same
    dom_tree = [f"{p}/{s}" for p in pages for s in SIDES
                if dom["pages"][p][s]["hash"] != old["pages"][p][s]["hash"]
                or dom["pages"][p][s]["error"] != old["pages"][p][s]["error"]]
    dom_metric = [f"{p}/{s}" for p in pages for s in SIDES
                  if json.dumps(_reads(dom["pages"][p][s].get("metrics")), sort_keys=True)
                  != json.dumps(_reads(old["pages"][p][s].get("metrics")), sort_keys=True)]
    dom_gt = [f"{p}/{s}" for p in pages for s in ("orig", "var")
              if json.dumps(dom["pages"][p][s].get("gt"), sort_keys=True)
              != json.dumps(old["pages"][p][s].get("gt"), sort_keys=True)]
    dom_fam = [f for f in old["families"]
               if json.dumps(dom["families"][f], sort_keys=True)
               != json.dumps(old["families"][f], sort_keys=True)]
    dom_depth = [d for d in old["depth"] if dom["depth"][d] != old["depth"][d]]
    dom_rows = {"trees": dom_tree, "metrics": dom_metric, "ground_truth": dom_gt,
                "families": dom_fam, "depth": dom_depth}
    lines.append("")
    lines.append("dom arm (the DOM-snapshot reading) against the old arm: "
                 + ", ".join(f"{k} {len(v)} changed" for k, v in dom_rows.items()))
    for k, v in dom_rows.items():
        if v:
            lines.append(f"   {k}: {v[:10]}")
    checks.append(("the dom arm reproduces the old arm (trees on every side, a/b metrics, "
                   "ground truth, families, depth set)",
                   not any(dom_rows.values()),
                   f"{sum(len(v) for v in dom_rows.values())} differences"))

    # the page text (Node.text of the root): implied closes keep document order,
    # so a variant reads the original's text unless the parse loses some of it
    lines.append("")
    text_lost = {}
    for arm, doc in (("old", old), ("raw", raw)):
        for side in ("var", "var_doc"):
            text_lost[f"{arm}_{side}"] = [p for p in pages if doc["pages"][p][side]["text_hash"]
                                          != doc["pages"][p]["orig"]["text_hash"]]
    lines.append("page text differs from the original's: " + ", ".join(
        f"{k} {len(v)}" for k, v in text_lost.items()))
    for k in ("raw_var", "raw_var_doc"):
        for p in text_lost[k]:
            lines.append(f"   {k} {p}: {raw['pages'][p]['orig']['text_len']} -> "
                         f"{raw['pages'][p][k.split('_', 1)[1]]['text_len']} chars")
    checks.append(("the raw arm reads every variant's page text as on the original",
                   not text_lost["raw_var"] and not text_lost["raw_var_doc"],
                   f"old arm differs on {len(text_lost['old_var'])} var and "
                   f"{len(text_lost['old_var_doc'])} var_doc pages"))

    # d
    lines.append("")
    lim = {arm: [f"{p}/{s}" for p in pages for s in SIDES
                 if doc["pages"][p][s]["error"]] for arm, doc in (("old", old), ("raw", raw))}
    lines.append(f"d. parser refusals (HtmlLimitError) on the corpus: old {len(lim['old'])} "
                 f"{lim['old']}, raw {len(lim['raw'])} {lim['raw']}")
    lines.append(f"{'depth case':12s} | old ok rows | raw ok rows | dom ok rows")
    for did in old["depth"]:
        cells = [f"{'yes' if d['ok'] else 'no':>6s} {d['rows']:4d}"
                 for d in (old["depth"][did], raw["depth"][did], dom["depth"][did])]
        lines.append(f"{did:12s} | " + " | ".join(cells) + f"  (want {old['depth'][did]['want']})")
    depth_ok = all(d["ok"] and d["rows"] == d["want"] for d in raw["depth"].values())
    old_fail = collections.defaultdict(list)
    for k, d in old["depth"].items():
        if not d["ok"]:
            old_fail[k.split("/")[0]].append(int(k.split("/")[1]))
    checks.append(("depth set: the raw arm parses every case with every row a sibling",
                   depth_ok, ""))
    checks.append(("depth set: old fails for N >= 129",
                   all(not d["ok"] for k, d in old["depth"].items() if int(k.split("/")[1]) >= 129),
                   "old fails at " + ", ".join(f"{fam} N in {ns}"
                                               for fam, ns in sorted(old_fail.items()))))

    # the DOM set
    lines.append("")
    lines.append("DOM set (script-built trees, serialised): items read old / raw / dom")
    dom_set_ok = True
    for did in old["dom_set"]:
        got = [len(a["dom_set"][did]["items"]) for a in (old, raw, dom)]
        want = len(old["dom_set"][did]["want"])
        dom_set_ok = dom_set_ok and dom["dom_set"][did]["items"] == old["dom_set"][did]["want"]
        lines.append(f"   {did:8s} {got[0]} / {got[1]} / {got[2]}  (want {want})")
    checks.append(("DOM set: the dom arm reads every item of every script-built tree",
                   dom_set_ok, ""))

    # e
    lines.append("")
    risk = {}
    for side in ("orig", "var"):
        base = [p for p in pages if old["pages"][p][side].get("saved_rerun")]
        row = {"tools": len(base)}
        for arm, doc in (("raw", raw), ("dom", dom)):
            runs = [(p, old["pages"][p][side]["saved_rerun"], doc["pages"][p][side]["saved_rerun"])
                    for p in base]
            row[arm] = {
                "items_changed": [p for p, _o, r in runs if r["items_changed"]],
                "outcome_changed": [p for p, o, r in runs if r["success"] != o["success"]],
                "failed": sum(1 for _p, _o, r in runs if not r["success"]),
            }
        forced = [(p, raw["pages"][p][side]["saved_rerun"]["as_html5"]) for p in base]
        row["raw_as_html5"] = {
            "items_changed": [p for p, f in forced if f["items_changed"]],
            "outcome_changed": [p for p, f in forced
                                if f["success"] != old["pages"][p][side]["saved_rerun"]["success"]],
            "detail": {p: f["error"] for p, f in forced if f["items_changed"]}}
        row["failed_old"] = sum(1 for p in base if not old["pages"][p][side]["saved_rerun"]["success"])
        risk[side] = row
        lines.append(f"e. saved tools compiled by the old arm on the {side} pages, run through "
                     f"the executor: {row['tools']} tools ({row['failed_old']} fail in the old "
                     f"arm); raw arm: items changed {len(row['raw']['items_changed'])}, outcome "
                     f"changed {len(row['raw']['outcome_changed'])}; dom arm: items changed "
                     f"{len(row['dom']['items_changed'])}, outcome changed "
                     f"{len(row['dom']['outcome_changed'])}")
        lines.append(f"   the same tools marked html5: items changed "
                     f"{len(row['raw_as_html5']['items_changed'])} "
                     f"{row['raw_as_html5']['items_changed']}, outcome changed "
                     f"{len(row['raw_as_html5']['outcome_changed'])}")
        for p, why in row["raw_as_html5"]["detail"].items():
            lines.append(f"     {p}: {why or 'items differ'}")
    unchanged = all(not risk[s][a][k] for s in risk for a in ("raw", "dom")
                    for k in ("items_changed", "outcome_changed"))
    checks.append(("saved tools without the marker read the tree they were compiled on "
                   "(no item or outcome changes in the raw or dom arm)", unchanged, ""))

    lines.append("")
    lines.append("checks:")
    for name, passed, detail in checks:
        lines.append(f"  [{'PASS' if passed else 'FAIL'}] {name}"
                     + (f": {detail}" if detail else ""))
    lines.append(f"arm seconds: old {old['seconds']}, raw {raw['seconds']}, dom {dom['seconds']}")
    result = {"summary": summary, "families": fam_rows, "fired": fired_rows,
              "text_differs": text_lost, "silent_diff": silent_diff, "dom_vs_old": dom_rows,
              "refusals": lim, "depth": {"old": old["depth"], "raw": raw["depth"],
                                         "dom": dom["depth"]},
              "dom_set": {a: d["dom_set"] for a, d in (("old", old), ("raw", raw), ("dom", dom))},
              "saved_tool_risk": risk,
              "checks": [{"check": c, "pass": p, "detail": d} for c, p, d in checks],
              "arms": {a: {"html_extract": d["html_extract"], "option": d.get("option"),
                           "seconds": d["seconds"]}
                       for a, d in (("old", old), ("raw", raw), ("dom", dom))}}
    return "\n".join(lines), result


def _rp_plain(gold, got):
    """Exact-text recall, for the family comparison (no matcher in the parent)."""
    norm = lambda s: " ".join(str(s).split()).lower()         # noqa: E731
    want = [norm(g) for g in gold]
    have = {norm(x) for x in got or []}
    return {"recall": sum(1 for g in want if g in have) / len(want) if want else None}


def _reads(m):
    """The a metrics only: the dom arm does not compile."""
    if not m:
        return m
    return {k: v for k, v in m.items() if k != "compile"}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def _arm(arm, root, tmp, data, gold=None):
    pk = os.path.join(tmp, "pk-" + arm)
    os.mkdir(pk)
    link = os.path.join(pk, "anyuse")
    os.symlink(os.path.realpath(root), link)
    out = os.path.join(tmp, arm + ".json")
    cmd = [sys.executable, "-I", "-B", os.path.abspath(__file__), "--arm", arm,
           "--pkgroot", pk, "--data-root", data, "--out", out]
    if gold:
        cmd += ["--gold", gold]
    return subprocess.Popen(cmd), out, link


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old-root", help="checkout of the code without implied end tags")
    ap.add_argument("--new-root", default=CHECKOUT)
    ap.add_argument("--data-root", default=None, help="checkout whose testdata/ is read")
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--arm", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pkgroot", default="", help=argparse.SUPPRESS)
    ap.add_argument("--gold", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    if ns.arm:
        run_arm(ns)
        return 0
    if not ns.old_root:
        ap.error("--old-root is required: a checkout of the code before the change")
    data = ns.data_root or ns.new_root
    tmp = tempfile.mkdtemp(prefix="html5-tree-bench-")
    links = []
    try:
        proc, old_out, link = _arm("old", ns.old_root, tmp, data)
        links.append(link)
        if proc.wait() != 0:
            print("the old arm failed")
            return 2
        procs = []
        for arm in ("raw", "dom"):
            proc, out, link = _arm(arm, ns.new_root, tmp, data, gold=old_out)
            links.append(link)
            procs.append((proc, out))
        if [p.wait() for p, _o in procs] != [0, 0]:
            print("a new arm failed")
            return 2
        docs = []
        for path in (old_out, procs[0][1], procs[1][1]):
            with open(path, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        text, result = report(*docs)
        print(text)
        if ns.json:
            with open(ns.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=1)
        return 0 if all(c["pass"] for c in result["checks"]) else 1
    finally:
        for link in links:
            os.unlink(link)
            os.rmdir(os.path.dirname(link))
        for name in os.listdir(tmp):
            os.unlink(os.path.join(tmp, name))
        os.rmdir(tmp)


if __name__ == "__main__":
    sys.exit(main())
