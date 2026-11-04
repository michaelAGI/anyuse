"""Offline A/B bench: can a search -> open -> read chain be proved from
recordings made the way people make them?

Two arms run the same corpus, each in its own process that imports ``anyuse``
from its own checkout:

* ``old``: the checkout before the change (``--old-root``). It traces each
  validation recording's intermediate binding through its own traffic and fits
  a chain on a recorded input only: ``NAME.fit1.json`` in a recording folder,
  the second of four HARs, attach sessions or learn values;
* ``new``: this checkout (``--new-root``, default: the checkout holding this
  file), which with no recorded input free to fit on generates one from the
  first input's own search (``execution/chain_fit.py``), and rebuilds a chain
  from the browser replay's traffic.

The site is a package registry served by a loopback HTTP server
(``127.0.0.1``, an ephemeral port): ``/api/search?q=`` returns four rows
``{"id", "name"}`` for a word, and the rows whose names start with ``q`` for
anything else (a search for a result's name lists it); ``/api/package/<id>``
one package's fields (``downloads`` is a JSON number);
``/api/package/<id>/releases`` its releases; ``/package/<id>`` the HTML page a
browser shows; ``/find?q=`` a search page that shows the found result's
releases. The same site is served with string ids (``pk3f…``, under ``/s``)
and with numeric ids (``40213``, under ``/n``).

Recordings are made by fetching the site the way a person's browser does (the
home page, the search request, sometimes a detail fetched ahead for the first
row, the opened result's request and page) and writing that traffic:

* ``har``: a HAR 1.2 file as a browser's DevTools exports it, read back with
  ``har_import.load_har`` and ``har_import.har_to_recording`` (the visible
  labels as ``--har-expect`` gives them; an item page's values are named
  during the proof from the recorded responses, ``execution/field_names.py``);
* ``attach``: an attach session (``network`` + page ``snapshots``), read back
  with ``attach.session_to_recording``, the result list derived from the pages
  as ``attach.compile_sessions`` derives it;
* ``fields``: the HAR recording with the detail page's fields named
  (``ui.fields``), the recording-file format ``tests/test_api_chain.py`` uses.

The compiler reads only https traffic, so the recorder writes the loopback
answers under ``https://registry.test`` and every replay of that origin is sent
to the loopback server.

Each scenario compiles one recording with one fitting recording and proves on
two validation recordings (the ``workflow compile --recording DIR`` layout:
``NAME.rec.json``, ``NAME.fit1.json``, ``NAME.fresh*.json``), with the
recordings as recorded (no ``bindings``) and with ``bindings`` added by hand
as the tests write them. Layouts:

* ``first``: every recording opened the first result, which is named what was typed;
* ``named``: the compile recording and a validation recording opened a later
  result named what was typed;
* ``other_row``: a validation recording opened the second result (not named
  what was typed) after the page fetched the first result's detail ahead;
* ``typed``: the validation inputs are typed in (``--expect``), no traffic.

A proved chain is persisted (``compiled_record`` -> ``persist_compiled``), read
back (``load_compiled``) and called for held-out words: 10 whose first result
is named the word, 5 whose named result is a later one, 3 with no result of
that name. Counted per call: the named result's page returned (correct),
another result's page returned (wrong), refused, failed.

The sources that declare no fitting recording are run end to end, on the
``first`` and ``named`` layouts and the releases page, and their proved chains
are persisted and called for the same held-out words:

* ``har_import.compile_from_har`` with four HARs (the second fits the chain,
  two prove it) and with three (old refuses and says to give a fourth; new fits
  the chain on a generated value and the two later HARs prove it);
* ``attach.compile_sessions`` with four sessions (the selection session fits
  the chain) and with three (old refuses; new reaches ``checked``);
* ``learn_loop.continue_learning`` with four values and with three (old
  refuses; new reaches ``checked``), the agent's runs being the recorded
  traffic and pages.

The browser-replay recompile: a chain compiled from four ``/find`` attach
sessions is persisted with its runtime rungs, its releases endpoint moves to
``/api/v2/...`` and it is called (``run_compiled``) for a word that is no proof
input, the browser replay's driver serving each page with the traffic it made.
``fitted``: the browser rung keeps its fitted inputs, one of which is captured
and fits the chain; ``generated``: the rung is fitted on the call's word only,
so new replays a generated value in the browser to fit it. A swapped chain is
called for the held-out words on the moved site.

Usage::

    python tests/bench/chain_proof_bench.py --old-root /path/to/old/checkout \\
        [--json results.json]

No network beyond 127.0.0.1. Not part of the test suite and not shipped.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORIGIN = "https://registry.test"
SEED = 11
ROWS = 4
KINDS = ("s", "n")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


# ---------------------------------------------------------------------------
# the site
# ---------------------------------------------------------------------------

SYLLABLES = ["ar", "bel", "cor", "dax", "el", "fen", "gro", "hal", "ix", "jun",
             "kel", "lum", "mor", "nax", "ol", "pyr", "qua", "ros", "sel", "tor",
             "ul", "vex", "wyn", "yar", "zel"]


def corpus():
    """``{word: [row names, most popular first]}``, each word's named row
    (-1: none), and the words by role."""
    rng = random.Random(SEED)
    words, seen = [], set()
    while len(words) < 34:
        word = rng.choice(SYLLABLES) + rng.choice(SYLLABLES) + rng.choice(SYLLABLES)
        if word not in seen:
            seen.add(word)
            words.append(word)
    roles = {"rec_first": words[:8], "rec_later": words[8:12],
             "held_first": words[12:22], "held_later": words[22:27],
             "held_unnamed": words[27:30]}
    later = set(roles["rec_later"]) | set(roles["held_later"])
    results, named = {}, {}
    for word in words[:30]:
        others = [f"{word}-{s}" for s in ("core", "derive", "macros", "cli", "utils")]
        rng.shuffle(others)
        if word in roles["held_unnamed"]:
            results[word], named[word] = others[:ROWS], -1
            continue
        rows = others[:ROWS - 1]
        position = rng.randint(1, ROWS - 1) if word in later else 0
        rows.insert(position, word)
        results[word], named[word] = rows, position
    return results, named, roles


def _hash(name):
    return hashlib.sha256(name.encode()).hexdigest()


def ident(kind, name):
    h = _hash(name)
    return 10000 + int(h[:6], 16) % 89999 if kind == "n" else "pk" + h[:8]


def search_doc(kind, results, q):
    """The rows the site lists for ``q``: a word's own rows, else every row whose name
    starts with ``q`` (a search for a result's name lists that result)."""
    rows = results.get(q)
    if rows is None:
        rows = sorted({n for names in results.values() for n in names if q and n.startswith(q)})
    return {"results": [{"id": ident(kind, n), "name": n} for n in rows]}


def find_opens(rows, q):
    """The row the ``/find`` page shows: the one named ``q``, else the first."""
    return next((r for r in rows if r["name"] == q), rows[0] if rows else None)


def package_doc(kind, name):
    h = int(_hash(name)[:8], 16)
    return {"package": {"id": ident(kind, name), "title": f"{name} package",
                        "summary": f"what {name} does for you",
                        "version": f"{len(name)}.{h % 9}.{h % 7}",
                        "downloads": 1000 + h % 90000}}


def releases_doc(name):
    h = int(_hash(name)[:8], 16)
    return {"releases": [{"title": f"{name} {len(name)}.{(h + k) % 9}.{k}", "yanked": False}
                         for k in (3, 2, 1)]}


def shown(final, name):
    """What the opened page shows: the release titles, or the package fields."""
    if final == "releases":
        return [r["title"] for r in releases_doc(name)["releases"]]
    item = package_doc("s", name)["package"]
    return [item["title"], item["summary"], item["version"], str(item["downloads"])]


def page_html(final, name):
    if final == "releases":
        rows = "".join(f"<li class=\"release\">{html.escape(t)}</li>" for t in shown(final, name))
        body = f"<h1>{html.escape(name)}</h1><ul class=\"releases\">{rows}</ul>"
    else:
        cells = "".join(f"<dd>{html.escape(t)}</dd>" for t in shown(final, name))
        body = f"<h1>{html.escape(name)}</h1><dl>{cells}</dl>"
    return (f"<html><head><title>{html.escape(name)}</title></head><body><nav>Registry"
            f"</nav><main>{body}</main><footer>About</footer></body></html>")


HOME = ("<html><head><title>Registry</title></head><body><nav>Registry</nav><main>"
        "<form><input name=\"q\" placeholder=\"Search packages\"></form></main>"
        "<footer>About</footer></body></html>")


def make_handler(results):
    by_id = {(kind, str(ident(kind, n))): n
             for rows in results.values() for n in rows for kind in KINDS}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, ctype, text):
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            seg = [s for s in parts.path.split("/") if s]
            if not seg or seg[0] not in KINDS:
                return self.reply(404, "text/plain", "not found")
            kind, rest = seg[0], seg[1:]
            if not rest:
                return self.reply(200, "text/html; charset=utf-8", HOME)
            if rest == ["api", "search"]:
                q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
                return self.reply(200, "application/json",
                                  json.dumps(search_doc(kind, results, q)))
            if rest == ["find"]:
                # a search page that shows the found result's releases (its script
                # fetches the search, then the releases)
                q = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
                opened = find_opens(search_doc(kind, results, q)["results"], q)
                if opened is None:
                    return self.reply(404, "text/plain", "no results")
                return self.reply(200, "text/html; charset=utf-8",
                                  page_html("releases", opened["name"]))
            if rest[:2] == ["api", "package"] and len(rest) in (3, 4):
                name = by_id.get((kind, rest[2]))
                if name is None:
                    return self.reply(404, "application/json", "{}")
                if len(rest) == 4 and rest[3] == "releases":
                    return self.reply(200, "application/json", json.dumps(releases_doc(name)))
                if len(rest) == 3:
                    return self.reply(200, "application/json",
                                      json.dumps(package_doc(kind, name)))
            if rest[0] == "package" and len(rest) == 2:
                name = by_id.get((kind, rest[1]))
                final = urllib.parse.parse_qs(parts.query).get("view", ["releases"])[0]
                if name is not None:
                    return self.reply(200, "text/html; charset=utf-8", page_html(final, name))
            return self.reply(404, "text/plain", "not found")

    return Handler


def serve(results):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(results))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _local(port, url):
    parts = urllib.parse.urlsplit(url)
    return f"http://127.0.0.1:{port}{parts.path}" + (f"?{parts.query}" if parts.query else "")


def fetch(port, url):
    """GET ``url`` (an ``ORIGIN`` url) from the loopback server: (status, type, text)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_local(port, url), timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode()


# ---------------------------------------------------------------------------
# recording: what a browser fetched and showed
# ---------------------------------------------------------------------------

def browse(port, kind, word, opened_row, final, prefetch=False):
    """Visit the home page, search ``word``, open result ``opened_row``.

    Returns the requests in order (url, resource type, status, type, text)
    and the pages the browser showed."""
    base = f"{ORIGIN}/{kind}"
    traffic, pages = [], []

    def get(url, rtype):
        status, ctype, text = fetch(port, url)
        traffic.append({"url": url, "type": rtype, "status": status, "ctype": ctype,
                        "text": text})
        return text

    pages.append({"url": f"{base}/", "dom": get(f"{base}/", "document"), "text": "Registry"})
    rows = json.loads(get(f"{base}/api/search?q={urllib.parse.quote(word)}", "xhr"))["results"]
    if prefetch:
        get(f"{base}/api/package/{rows[0]['id']}", "xhr")
    opened = rows[opened_row]
    if final == "releases":
        get(f"{base}/api/package/{opened['id']}/releases", "xhr")
    else:
        get(f"{base}/api/package/{opened['id']}", "xhr")
    url = f"{base}/package/{opened['id']}?view={final}"
    dom = get(url, "document")
    pages.append({"url": url, "dom": dom,
                  "text": "\n".join([opened["name"], *shown(final, opened["name"])])})
    return traffic, pages, opened


def browse_find(port, kind, word, releases_at="/api/package"):
    """Open ``/find?q=word``: the page, the search it makes and the found result's
    releases (requested under ``releases_at``, fetched from the site's endpoint)."""
    base = f"{ORIGIN}/{kind}"
    traffic = []

    def get(url, rtype, label=""):
        status, ctype, text = fetch(port, url)
        traffic.append({"url": label or url, "type": rtype, "status": status, "ctype": ctype,
                        "text": text})
        return text

    url = f"{base}/find?q={urllib.parse.quote(word)}"
    dom = get(url, "document")
    rows = json.loads(get(f"{base}/api/search?q={urllib.parse.quote(word)}", "xhr"))["results"]
    opened = find_opens(rows, word)
    if opened is not None:
        get(f"{base}/api/package/{opened['id']}/releases", "xhr",
            label=f"{base}{releases_at}/{opened['id']}/releases")
    name = opened["name"] if opened else word
    pages = [{"url": f"{base}/", "dom": HOME, "text": "Registry"},
             {"url": url, "dom": dom, "text": "\n".join([name, *shown("releases", name)])}]
    return traffic, pages, opened


def write_har(path, traffic):
    """The traffic as a HAR 1.2 file, shaped like a DevTools export."""
    entries = []
    for i, item in enumerate(traffic):
        parts = urllib.parse.urlsplit(item["url"])
        entries.append({
            "startedDateTime": f"2026-10-01T10:00:{i:02d}.000Z", "time": 12,
            "request": {"method": "GET", "url": item["url"], "httpVersion": "HTTP/2",
                        "headers": [{"name": "User-Agent", "value": UA},
                                    {"name": "Accept", "value": "application/json, text/html"},
                                    {"name": "Cookie", "value": "consent=yes; theme=dark"}],
                        "queryString": [{"name": k, "value": v} for k, v in
                                        urllib.parse.parse_qsl(parts.query)],
                        "cookies": [{"name": "consent", "value": "yes"}],
                        "headersSize": -1, "bodySize": 0},
            "response": {"status": item["status"], "statusText": "OK", "httpVersion": "HTTP/2",
                         "headers": [{"name": "Content-Type", "value": item["ctype"]},
                                     {"name": "Set-Cookie", "value": "seen=1"}],
                         "cookies": [],
                         "content": {"size": len(item["text"]), "mimeType": item["ctype"],
                                     "text": item["text"]},
                         "redirectURL": "", "headersSize": -1, "bodySize": len(item["text"])},
            "cache": {}, "timings": {"send": 1, "wait": 10, "receive": 1},
            "_resourceType": item["type"],
        })
    doc = {"log": {"version": "1.2", "creator": {"name": "WebInspector", "version": "537.36"},
                   "pages": [], "entries": entries}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def attach_session(session_id, word, traffic, pages):
    """The traffic and pages as an attach session file holds them."""
    network = [{"url": t["url"], "method": "GET", "status": t["status"],
                "mime": t["ctype"].split(";")[0], "body": t["text"], "post_data": None,
                "bytes": len(t["text"]),
                "resource_type": "Document" if t["type"] == "document" else "XHR",
                "headers": {"content-type": t["ctype"]}, "credential_bound": "",
                "body_dropped": ""} for t in traffic]
    return {"kind": "attach_session", "version": 1, "id": session_id, "task": "package releases",
            "input": {"name": "q", "value": word}, "started_at": "2026-10-01T10:00:00+0000",
            "snapshots": [{"url": p["url"], "title": "", "dom": p["dom"], "text": p["text"],
                           "password_field": False} for p in pages],
            "network": network, "navigations": [p["url"] for p in pages]}


def layout_runs(results, named, roles):
    """Per layout: (word, opened row, prefetch) for compile, fitting, validation."""
    first, later = roles["rec_first"], roles["rec_later"]
    pick = lambda w: (w, named[w], False)
    other = lambda w: (w, 1, True)
    return {
        "first": [pick(first[0]), pick(first[1]), pick(first[2]), pick(first[3])],
        "named": [pick(later[0]), pick(first[1]), pick(first[4]), pick(later[1])],
        "other_row": [pick(first[0]), pick(first[1]), pick(first[2]), other(first[5])],
        "typed": [pick(first[0]), pick(first[1]), pick(first[6]), pick(first[7])],
    }


def record_all(port, tmp, results, named, roles):
    """Record every run of every layout as a HAR and as an attach session."""
    index = {}
    for kind in KINDS:
        for final in ("releases", "detail"):
            for layout, runs in layout_runs(results, named, roles).items():
                for pos, (word, row, prefetch) in enumerate(runs):
                    key = f"{kind}-{final}-{layout}-{pos}"
                    traffic, pages, opened = browse(port, kind, word, row, final, prefetch)
                    har = os.path.join(tmp, key + ".har")
                    write_har(har, traffic)
                    sess = os.path.join(tmp, key + ".session.json")
                    with open(sess, "w", encoding="utf-8") as fh:
                        json.dump(attach_session(key, word, traffic, pages), fh)
                    index[key] = {"word": word, "row": row, "opened": opened["name"],
                                  "opened_id": opened["id"], "har": har, "session": sess}
        # the /find page's sessions (a result page whose URL carries the input), for the
        # browser-replay recompile, and the word a call is made for
        first = roles["rec_first"]
        for pos, word in [*enumerate(first[:4]), ("call", first[4])]:
            key = f"{kind}-find-{pos}"
            traffic, pages, opened = browse_find(port, kind, word)
            sess = os.path.join(tmp, key + ".session.json")
            with open(sess, "w", encoding="utf-8") as fh:
                json.dump(attach_session(key, word, traffic, pages), fh)
            index[key] = {"word": word, "opened": opened["name"], "session": sess}
    with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return index


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

class Loopback:
    """The proof's and the tool's transport: ``ORIGIN`` is the loopback server."""

    def __init__(self, port):
        self.port = port
        self.sent = 0

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        parts = urllib.parse.urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != ORIGIN or method != "GET":
            return ApiResponse(599, {"Content-Type": "text/plain"}, "not the fixture")
        self.sent += 1
        status, ctype, text = fetch(self.port, url)
        return ApiResponse(status, {"Content-Type": ctype}, text)


class Moved(Loopback):
    """The site after its releases endpoint moved from ``/api/package/<id>/releases``
    (now 404) to ``/api/v2/package/<id>/releases``."""

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        path = urllib.parse.urlsplit(url).path
        if "/api/package/" in path and path.endswith("/releases"):
            self.sent += 1
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        return super().send(method, url.replace("/api/v2/package/", "/api/package/"),
                            headers, body, timeout_s)


class FindBrowser:
    """The browser replay's driver for the moved site: it opens ``/find`` pages and,
    asked to capture, returns the requests the page made (to the moved endpoint)."""

    def __init__(self, port, kind):
        self.port, self.kind, self.opened = port, kind, []

    def open(self, url, settle_s, timeout_s, capture_traffic=False):
        from anyuse.execution.cdp_replay import PageResult
        self.opened.append((url, bool(capture_traffic)))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        traffic, pages, _opened = browse_find(self.port, self.kind, q,
                                              releases_at="/api/v2/package")
        network = [{"url": t["url"], "method": "GET", "status": t["status"],
                    "mime": t["ctype"].split(";")[0], "body": t["text"], "bytes": len(t["text"]),
                    "resource_type": "Document" if t["type"] == "document" else "XHR",
                    "headers": {}} for t in traffic] if capture_traffic else []
        return PageResult(url=url, dom=pages[-1]["dom"], text=pages[-1]["text"],
                          status=traffic[0]["status"], elapsed_ms=300.0, network=network)


class _Storage:
    def __init__(self, base):
        self.base = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def har_recording(entry, final):
    from anyuse.execution.har_import import har_to_recording, load_har
    return har_to_recording(load_har(entry["har"]), "q", entry["word"],
                            shown(final, entry["opened"]))


def attach_recordings(entries):
    """Attach recordings of one layout: ground truth fitted on the first two."""
    from anyuse.execution.attach import session_to_recording
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    recs = []
    for i, entry in enumerate(entries):
        with open(entry["session"], encoding="utf-8") as fh:
            recs.append(session_to_recording(json.load(fh), "q", entry["word"], f"attach{i}"))
    selector = derive_ground_truth(recs[:2])
    vis = any(r.get("ground_truth_visible_only") for r in recs[:2])
    for r in recs[2:]:
        r["ui"]["items"] = extract(r["dom"], selector, limit=30, visible_only=vis) if selector else []
        r["ui"]["labels"] = (extract(r["dom"], selector, limit=30, visible_only=vis, labels=True)
                             if selector else [])
    return recs


def with_fields(rec, final, entry):
    if final == "detail":
        values = shown(final, entry["opened"])
        rec["ui"]["fields"] = {f"field_{i + 1}": v for i, v in enumerate(values)}
    return rec


def held_out(port, plan, kind, final, results, named, roles, site=None):
    from anyuse.execution.api_compile import CHAIN_PARAM
    from anyuse.execution.api_step import ApiStepExecutor
    by_id = {str(ident(kind, n)): n for rows in results.values() for n in rows}
    out = {}
    for group in ("held_first", "held_later", "held_unnamed"):
        counts = {"correct": 0, "wrong": 0, "refused": 0, "failed": 0}
        for word in roles[group]:
            res = ApiStepExecutor(transport=(site or Loopback)(port)).execute(plan, {"q": word})
            bound = ((res.get("plan_trace") or [{}])[0].get("bound") or {}).get(CHAIN_PARAM)
            opened = by_id.get(str(bound)) if bound is not None else None
            if res.get("success"):
                got = res.get("extracted") or {}
                values = got.get("items") if final == "releases" else list(got.values())
                right = named[word] >= 0 and opened == results[word][named[word]]
                # the returned values are the opened result's own page
                page_ok = (opened is not None and bool(values)
                           and all(str(v) in shown(final, opened) for v in values))
                counts["correct" if right and page_ok else "wrong"] += 1
            elif res.get("reason_code") == "not_found":
                counts["refused"] += 1
            else:
                counts["failed"] += 1
        out[group] = counts
    return out


def run_scenario(ns, index, source, kind, final, layout, bindings, corpus_parts):
    from anyuse.execution.api_compile import (CHAIN_PARAM, compiled_record, load_compiled,
                                              load_command, persist_compiled,
                                              proof_matches_template)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    from anyuse.execution.har_import import typed_recording
    results, named, roles = corpus_parts
    entries = [index[f"{kind}-{final}-{layout}-{pos}"] for pos in range(4)]
    row = {"source": source, "ids": kind, "final": final, "layout": layout,
           "bindings": bindings, "status": "", "proved": False, "persisted": False,
           "traced": False, "reason": "", "crashed": "", "held_out": None}
    try:
        if source == "attach":
            recs = attach_recordings(entries)
        else:
            recs = [har_recording(e, final) for e in entries]
            if source == "fields":
                recs = [with_fields(r, final, e) for r, e in zip(recs, entries)]
        if layout == "typed":
            recs[2:] = [typed_recording("q", e["word"], shown(final, e["opened"]))
                        for e in entries[2:]]
            if source == "fields":
                recs[2:] = [with_fields(r, final, e) for r, e in zip(recs[2:], entries[2:])]
        if bindings == "given":
            for r, e in zip(recs, entries):
                r["bindings"] = {CHAIN_PARAM: e["opened_id"]}
        transport = Loopback(ns.port)
        compiled, proof = compile_and_verify(recs[0], recs[2:],
                                             executor=ApiStepExecutor(transport=transport),
                                             fitting_recordings=[recs[1]],
                                             preflight=lambda _c: "")
        row["status"] = compiled.status
        row["proved"] = bool(proof.passed)
        row["reason"] = (proof.reason or compiled.reason)[:300]
        row["proof_requests"] = transport.sent
        row["traced"] = any(c.contract.get("bindings_derived") for c in proof.cases)
        steps = getattr(compiled.template, "steps", None) or []
        if steps:
            spec = next((r for r in compiled.template.parameter_schema
                         if r.get("name") == CHAIN_PARAM), {})
            row["derived_type"] = spec.get("type")
        if proof.passed and steps:
            storage = _Storage(os.path.join(ns.work, "store"))
            wf = f"{source}-{kind}-{final}-{layout}-{bindings}"
            persist_compiled(storage, wf, compiled_record(compiled.template, proof.to_dict(),
                                                          workflow=wf))
            record = load_compiled(storage, wf)
            row["persisted"] = bool(record) and proof_matches_template(record)
            if row["persisted"]:
                plan = load_command(record["template"])
                row["held_out"] = held_out(ns.port, plan, kind, final, results, named, roles)
    except Exception as exc:                              # noqa: BLE001
        row["crashed"] = f"{type(exc).__name__}: {exc}"[:240]
        row["trace"] = traceback.format_exc()[-600:]
    return row


class _Agent:
    """The learn loop's agent: each value's run is its recorded traffic and pages."""

    def __init__(self, runs):
        self.runs = runs

    def run(self, task, start_url, value):
        return self.runs[value]


def agent_run(entry):
    """An attach session's traffic and result page as the agent's run of its word."""
    from anyuse.execution.agent_browser import AgentRun
    with open(entry["session"], encoding="utf-8") as fh:
        session = json.load(fh)
    page = session["snapshots"][-1]
    return AgentRun(value=entry["word"], task="package releases",
                    start_url=session["snapshots"][0]["url"], final_url=page["url"],
                    dom=page["dom"], text=page["text"], network=session["network"],
                    steps=[{"action": "type", "index": 0, "text": entry["word"]},
                           {"action": "submit", "index": 0}, {"action": "done"}],
                    typed=[entry["word"]])


def learn_entry(ns, entries):
    """``continue_learning`` on the agent's runs of ``entries``; the first is confirmed."""
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.learn_loop import Candidate, continue_learning, page_title
    runs = {e["word"]: agent_run(e) for e in entries}
    first = runs[entries[0]["word"]]
    cand = Candidate(workflow="learned", task=first.task, start_url=first.start_url,
                     param="q", cls="list", value=first.value, final_url=first.final_url,
                     title=page_title(first.dom),
                     recording=first.as_recording("learned", "q", "agent0", "list"))
    kept = {}

    def persist(compiled, proof, _recordings, _res):
        kept["compiled"], kept["proof"] = compiled, proof
        return "learned"
    transport = Loopback(ns.port)
    res = continue_learning(cand, [e["word"] for e in entries[1:]], agent=_Agent(runs),
                            executor=ApiStepExecutor(transport=transport),
                            persist=persist, confirmed_by="bench",
                            proof_preflight=lambda _c: "")
    compiled, proof = kept.get("compiled"), kept.get("proof")
    if compiled is None:
        return None, None, {"reason": res.reason, "status": (res.compiled or {}).get("status"),
                            "split": res.split}, transport
    return compiled, proof, {}, transport


def run_entry(ns, index, source, kind, layout, n, corpus_parts):
    """One source end to end on ``n`` recordings of ``layout`` (releases page)."""
    from anyuse.execution.api_compile import (compiled_record, load_compiled, load_command,
                                              persist_compiled, proof_matches_template)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.attach import compile_sessions
    from anyuse.execution.har_import import HarCase, compile_from_har
    results, named, roles = corpus_parts
    entries = [index[f"{kind}-releases-{layout}-{pos}"] for pos in range(n)]
    row = {"source": source, "ids": kind, "layout": layout, "n": n, "status": "",
           "proved": False, "proof_status": "", "split": {}, "traced": False,
           "second": entries[1]["word"], "fitted_on": "", "generated": False,
           "fit_is_input": False, "persisted": False, "reason": "", "crashed": "",
           "held_out": None}
    try:
        transport = Loopback(ns.port)
        executor = ApiStepExecutor(transport=transport)
        if source == "har":
            cases = [HarCase(path=e["har"], param_value=e["word"],
                             expected=shown("releases", e["opened"])) for e in entries]
            compiled, proof, report = compile_from_har(cases, executor=executor,
                                                       preflight=lambda _c: "")
            fitted = report.get("fitting") or []
            row["fitted_on"] = next((e["word"] for e in entries if e["har"] in fitted), "")
        elif source == "attach":
            sessions = []
            for e in entries:
                with open(e["session"], encoding="utf-8") as fh:
                    sessions.append(json.load(fh))
            compiled, proof, report, _recs = compile_sessions(sessions, executor=executor,
                                                              preflight=lambda _c: "")
            sid = report.get("chain_fitted_on") or ""
            row["fitted_on"] = next((e["word"] for e in entries if sid and
                                     os.path.basename(e["session"]) == sid + ".session.json"),
                                    "")
        else:
            compiled, proof, failed, transport = learn_entry(ns, entries)
            if compiled is None:
                row.update(status=failed.get("status") or "", split=failed.get("split") or {},
                           reason=str(failed.get("reason") or "")[:600])
                return row
        generated = getattr(proof, "chain_fit", None) or {}
        if generated.get("value"):
            # a fitting input generated from the first input's search (chain_fit)
            row["fitted_on"], row["generated"] = generated["value"], True
            row["fit_is_input"] = generated["value"] in (
                [e["word"] for e in entries] + [c.value for c in proof.cases])
        row["status"] = compiled.status
        row["proved"] = bool(proof.passed)
        row["proof_status"] = str(getattr(proof, "effective_status", "") or "")
        row["split"] = dict(proof.split or {})
        # what to record comes after the refusal, so the whole reason is kept
        row["reason"] = (proof.reason or compiled.reason)[:600]
        row["proof_requests"] = transport.sent
        row["traced"] = any(c.contract.get("bindings_derived") for c in proof.cases)
        if proof.passed and getattr(compiled.template, "steps", None):
            storage = _Storage(os.path.join(ns.work, "store"))
            wf = f"entry-{source}-{kind}-{layout}-{n}"
            persist_compiled(storage, wf, compiled_record(compiled.template, proof.to_dict(),
                                                          workflow=wf))
            record = load_compiled(storage, wf)
            row["persisted"] = bool(record) and proof_matches_template(record)
            if row["persisted"]:
                plan = load_command(record["template"])
                row["held_out"] = held_out(ns.port, plan, kind, "releases", results, named,
                                           roles)
    except Exception as exc:                              # noqa: BLE001
        row["crashed"] = f"{type(exc).__name__}: {exc}"[:240]
        row["trace"] = traceback.format_exc()[-600:]
    return row


ENTRIES = [(source, kind, layout, n)
           for kind in KINDS
           for source in ("har", "attach", "learn")
           for layout, n in (("first", 4), ("named", 4), ("first", 3), ("named", 3))]


def run_recompile(ns, index, kind, variant, corpus_parts):
    """A chain compiled from four ``/find`` attach sessions, its releases endpoint moved,
    called for a word that is no proof input: does the call's automatic recompile rebuild
    it from the browser replay's traffic? ``variant``: ``fitted`` keeps the browser
    rung's fitted inputs (one is captured and fits the chain); ``generated`` leaves the
    rung fitted on the call's word only, so the chain's fitting input is generated."""
    from anyuse.execution import api_permission as perm
    from anyuse.execution.api_compile import (compiled_record, load_command, load_compiled,
                                              persist_compiled, with_runtime_rungs)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.attach import compile_sessions
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    results, named, roles = corpus_parts
    entries = [index[f"{kind}-find-{pos}"] for pos in range(4)]
    call = index[f"{kind}-find-call"]["word"]
    row = {"ids": kind, "variant": variant, "compiled": "", "swapped": False, "status": "",
           "split": {}, "chain_fit": {}, "captured": [], "reason": "", "crashed": "",
           "held_out": None, "held_out_before": None}
    try:
        # robots.txt of the fixture origin is answered here, not fetched
        perm._default_fetch = lambda url, *a, **kw: "User-agent: *\nAllow: /\n"
        sessions = []
        for e in entries:
            with open(e["session"], encoding="utf-8") as fh:
                sessions.append(json.load(fh))
        compiled, proof, _report, recs = compile_sessions(
            sessions, executor=ApiStepExecutor(transport=Loopback(ns.port)),
            preflight=lambda _c: "")
        row["compiled"] = str(proof.effective_status)
        if not proof.passed or not getattr(compiled.template, "steps", None):
            row["reason"] = (proof.reason or compiled.reason)[:300]
            return row
        wf = f"find-{kind}-{variant}"
        # the compiled chain on the site before its endpoint moved
        row["held_out_before"] = held_out(ns.port, compiled.template, kind, "releases",
                                          results, named, roles)
        record = compiled_record(compiled.template, proof.to_dict(), workflow=wf,
                                 source={"site": "registry.test", "origin": "attach"})
        with_runtime_rungs(record, recs[0], origin="attach")
        if variant == "generated":
            record["browser"]["fitted_on"] = [call]
        storage = _Storage(os.path.join(ns.work, "store-recompile"))
        persist_compiled(storage, wf, record)
        hosts = os.path.join(ns.work, "hosts.json")
        perm.acknowledge_host("registry.test", path=hosts)
        browser = FindBrowser(ns.port, kind)
        out = run_compiled(wf, {"q": call}, storage=storage, hosts_path=hosts,
                           executor=ApiStepExecutor(transport=Moved(ns.port)),
                           browser_executor=ReplayExecutor(driver=browser),
                           agent_runner=lambda task, **kw: {"ok": False, "outcome": "failed"})
        rec = out.get("recompiled") or {}
        row["swapped"] = bool(rec.get("swapped"))
        row["reason"] = str(rec.get("reason") or out.get("error") or "")[:400]
        row["captured"] = sorted({urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                                  .get("q", [""])[0] for u, cap in browser.opened if cap})
        stored = load_compiled(storage, wf) or {}
        if row["swapped"]:
            row["status"] = str((stored.get("proof") or {}).get("status") or "")
            row["split"] = dict((stored.get("proof") or {}).get("split") or {})
            row["chain_fit"] = dict(stored.get("chain_fit") or {})
            row["cases"] = sorted(c.get("value") for c in stored["proof"].get("cases") or [])
            row["held"] = sorted(e["word"] for e in entries[2:])
            row["held_out"] = held_out(ns.port, load_command(stored["template"]), kind,
                                       "releases", results, named, roles, site=Moved)
    except Exception as exc:                              # noqa: BLE001
        row["crashed"] = f"{type(exc).__name__}: {exc}"[:240]
        row["trace"] = traceback.format_exc()[-600:]
    return row


SCENARIOS = [(source, kind, final, layout)
             for kind in KINDS
             for source, final in (("har", "releases"), ("attach", "releases"),
                                   ("har", "detail"), ("fields", "detail"))
             for layout in ("first", "named", "other_row", "typed")
             if not (source == "attach" and layout == "typed")]


def run_arm(ns):
    sys.dont_write_bytecode = True
    sys.path.insert(0, ns.pkgroot)
    with open(os.path.join(ns.work, "index.json"), encoding="utf-8") as fh:
        index = json.load(fh)
    parts = corpus()
    started = time.monotonic()
    rows = [run_scenario(ns, index, source, kind, final, layout, bindings, parts)
            for source, kind, final, layout in SCENARIOS
            for bindings in ("as_recorded", "given")]
    entries = [run_entry(ns, index, source, kind, layout, n, parts)
               for source, kind, layout, n in ENTRIES]
    recompiles = [run_recompile(ns, index, kind, variant, parts)
                  for kind in KINDS for variant in ("fitted", "generated")]
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump({"arm": ns.arm, "scenarios": rows, "entries": entries,
                   "recompiles": recompiles,
                   "seconds": round(time.monotonic() - started, 1)}, fh, indent=1)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _key(row):
    return (row["source"], row["ids"], row["final"], row["layout"], row["bindings"])


def _sum(rows, group, what):
    return sum((r["held_out"] or {}).get(group, {}).get(what, 0) for r in rows)


def report(old, new):
    lines, checks = [], []
    olds = {_key(r): r for r in old["scenarios"]}
    lines.append(f"{'source':7} {'ids':3} {'final':8} {'layout':9} {'bindings':11} "
                 f"{'old':22} {'new':22} new held-out (first/later/unnamed: ok,wrong,refused)")
    for row in new["scenarios"]:
        o = olds[_key(row)]

        def cell(r):
            if r["crashed"]:
                return "CRASH " + r["crashed"].split(":")[0]
            state = ("persisted" if r["persisted"] else "proved" if r["proved"]
                     else "refused" if r["status"] in ("", "failed") else "not proved")
            return f"{state}{' (traced)' if r['traced'] else ''}"
        held = "-"
        if row["held_out"]:
            held = " / ".join(",".join(str(row["held_out"][g][k])
                                       for k in ("correct", "wrong", "refused"))
                              for g in ("held_first", "held_later", "held_unnamed"))
        lines.append(f"{row['source']:7} {row['ids']:3} {row['final']:8} {row['layout']:9} "
                     f"{row['bindings']:11} {cell(o):22} {cell(row):22} {held}")
    lines.append("")
    lines.append("reasons (new, not proved):")
    seen = set()
    for row in new["scenarios"]:
        if not row["proved"] and not row["crashed"]:
            text = row["reason"].split("; '")[0][:200]
            tag = (row["source"], row["final"], row["layout"], text)
            if tag not in seen:
                seen.add(tag)
                lines.append(f"  {row['source']}/{row['final']}/{row['layout']}: {text}")
    lines.append("reasons (old, as recorded, first of each kind):")
    seen = set()
    for row in old["scenarios"]:
        if row["bindings"] == "as_recorded" and not row["proved"]:
            text = (row["crashed"] or row["reason"]).split("; '")[0][:160]
            if text not in seen:
                seen.add(text)
                lines.append(f"  {row['source']}/{row['ids']}/{row['final']}/{row['layout']}: {text}")

    def rows(arm, **want):
        return [r for r in arm["scenarios"] if all(r[k] in v for k, v in want.items())]

    valid = dict(layout={"first", "named"}, source={"har", "attach", "fields"})
    good_shapes = lambda r: not (r["source"] == "har" and r["final"] == "detail")
    new_valid = [r for r in rows(new, bindings={"as_recorded"}, **valid) if good_shapes(r)]
    old_valid = [r for r in rows(old, bindings={"as_recorded"}, **valid) if good_shapes(r)]
    lines.append("")
    lines.append(f"chains proved and persisted, as recorded, layouts first+named "
                 f"(har/attach lists, named-field details; {len(new_valid)} scenarios): "
                 f"old {sum(r['persisted'] for r in old_valid)}, "
                 f"new {sum(r['persisted'] for r in new_valid)}")
    for arm in (old, new):
        given = [r for r in rows(arm, bindings={"given"}, **valid) if good_shapes(r)]
        lines.append(f"  with bindings given by hand: {arm['arm']} "
                     f"{sum(r['persisted'] for r in given)} of {len(given)}"
                     + (f" (crashed: {sum(1 for r in given if r['crashed'])})"
                        if any(r["crashed"] for r in given) else ""))
    for arm in (old, new):
        served = [r for r in arm["scenarios"] if r["held_out"]]
        lines.append(f"  held-out calls on {arm['arm']}'s persisted chains: "
                     f"first-named {_sum(served, 'held_first', 'correct')} correct / "
                     f"{_sum(served, 'held_first', 'wrong')} wrong; later-named "
                     f"{_sum(served, 'held_later', 'correct')} correct / "
                     f"{_sum(served, 'held_later', 'wrong')} wrong; no named result "
                     f"{_sum(served, 'held_unnamed', 'refused')} refused / "
                     f"{_sum(served, 'held_unnamed', 'wrong')} served")

    def held_text(r):
        if not r["held_out"]:
            return "-"
        return " / ".join(",".join(str(r["held_out"][g][k]) for k in ("correct", "wrong", "refused"))
                          for g in ("held_first", "held_later", "held_unnamed"))

    def entry_cell(r):
        if r["crashed"]:
            return "CRASH " + r["crashed"].split(":")[0]
        state = ("persisted" if r["persisted"] else "proved" if r["proved"]
                 else "refused" if r["status"] in ("", "failed") else "not proved")
        return f"{state}{' ' + r['proof_status'] if r['proved'] else ''}"

    def ekey(r):
        return (r["source"], r["ids"], r["layout"], r["n"])
    old_entries = {ekey(r): r for r in old["entries"]}
    lines.append("")
    lines.append("sources that declare no fitting recording (releases page):")
    lines.append(f"{'source':7} {'ids':3} {'layout':6} {'n':2} {'old':20} {'new':20} "
                 f"{'new split (fit/sel/val)':26} new held-out (first/later/unnamed: "
                 f"ok,wrong,refused)")
    for r in new["entries"]:
        o = old_entries[ekey(r)]
        sp = r["split"] or {}
        split = (f"{sp.get('fitting', '-')}/{sp.get('selection', '-')}/"
                 f"{sp.get('validation', '-')}"
                 + (f" on {r['fitted_on']}" if r["fitted_on"] else ""))
        lines.append(f"{r['source']:7} {r['ids']:3} {r['layout']:6} {r['n']:<2} "
                     f"{entry_cell(o):20} {entry_cell(r):20} {split:26} {held_text(r)}")
    lines.append("reasons (old, first of each source and count):")
    seen = set()
    for r in old["entries"]:
        if not r["proved"] and (r["source"], r["n"]) not in seen:
            seen.add((r["source"], r["n"]))
            lines.append(f"  {r['source']}/{r['n']}: {(r['crashed'] or r['reason'])[:220]}")
    lines.append("reasons (new, not proved; the end of the reason):")
    seen = set()
    for r in new["entries"]:
        if not r["proved"] and (r["source"], r["n"]) not in seen:
            seen.add((r["source"], r["n"]))
            lines.append(f"  {r['source']}/{r['n']}: ...{(r['crashed'] or r['reason'])[-240:]}")

    checks.append({"name": "new proves and persists every first/named chain recorded without bindings",
                   "pass": all(r["persisted"] and r["traced"] for r in new_valid)})
    changed = [_key(r) for r in new["scenarios"]
               if (r["proved"], r["persisted"], r["held_out"])
               != (olds[_key(r)]["proved"], olds[_key(r)]["persisted"],
                   olds[_key(r)]["held_out"])]
    checks.append({"name": "new decides every recording-folder scenario as old does",
                   "pass": not changed})
    refused = rows(new, bindings={"as_recorded"}, layout={"other_row", "typed"})
    checks.append({"name": "new refuses every other_row and typed chain recorded without bindings",
                   "pass": all(not r["proved"] for r in refused) and bool(refused)})
    same = []
    for r in rows(new, bindings={"as_recorded"}, layout={"first", "named", "other_row"}):
        twin = next(g for g in new["scenarios"]
                    if _key(g)[:4] == _key(r)[:4] and g["bindings"] == "given")
        same.append(r["proved"] == twin["proved"] and r["held_out"] == twin["held_out"])
    checks.append({"name": "new: traced bindings decide every recorded layout as hand-given ones do",
                   "pass": all(same)})
    named_rows = [r for r in new["scenarios"] if r["layout"] == "named" and r["held_out"]]
    checks.append({"name": "new: chains proved on the named layout serve no wrong page",
                   "pass": bool(named_rows) and _sum(named_rows, "held_first", "wrong")
                   + _sum(named_rows, "held_later", "wrong")
                   + _sum(named_rows, "held_unnamed", "wrong") == 0})
    numeric = [r for r in new_valid if r["ids"] == "n"]
    checks.append({"name": "new: numeric ids are typed integer and run",
                   "pass": bool(numeric) and all(r.get("derived_type") == "integer"
                                                 and r["held_out"] for r in numeric)})

    four = [r for r in new["entries"] if r["n"] == 4]
    checks.append({"name": "new proves and persists every four-input source chain (har, attach, "
                           "learn), status proved, fitted on one input, two held out",
                   "pass": bool(four) and all(
                       r["persisted"] and r["traced"] and r["proof_status"] == "proved"
                       and (r["split"].get("fitting"), r["split"].get("validation")) == (1, 2)
                       for r in four)})
    checks.append({"name": "new fits each har and attach chain on the second input",
                   "pass": all(r["fitted_on"] == r["second"] for r in four
                               if r["source"] in ("har", "attach"))})
    checks.append({"name": "new decides every four-input source chain as old does",
                   "pass": bool(four) and all(
                       (r["proved"], r["persisted"], r["held_out"], r["fitted_on"])
                       == (old_entries[ekey(r)]["proved"], old_entries[ekey(r)]["persisted"],
                           old_entries[ekey(r)]["held_out"], old_entries[ekey(r)]["fitted_on"])
                       for r in four)})
    three = [r for r in new["entries"] if r["n"] == 3]
    checks.append({"name": "three inputs: old refuses every one and says what to record",
                   "pass": bool(three) and all(
                       not old_entries[ekey(r)]["proved"]
                       and "needs 4 " in old_entries[ekey(r)]["reason"] for r in three)})
    want = {"har": "proved", "attach": "checked (selection not held out)",
            "learn": "checked (selection not held out)"}
    checks.append({"name": "three inputs: new proves the har chains (proved) and the attach and "
                           "learn chains (checked), fitted on one generated value from the first "
                           "input's search that is no recorded input and no proof case, two "
                           "recorded inputs proving it",
                   "pass": bool(three) and all(
                       r["persisted"] and r["traced"] and r["proof_status"] == want[r["source"]]
                       and r["generated"] and not r["fit_is_input"]
                       and (r["split"].get("fitting"), r["split"].get("fitting_generated"),
                            r["split"].get("validation")) == (1, 1, 2)
                       for r in three)})
    new_entries = {ekey(r): r for r in new["entries"]}
    checks.append({"name": "three inputs: new's chains serve held-out calls as the four-input "
                           "chains of their layout do (no wrong page on the named layout)",
                   "pass": bool(three) and all(
                       r["held_out"] and r["held_out"] == new_entries[ekey(r)[:3] + (4,)]["held_out"]
                       for r in three)
                   and not any(_sum([r], g, "wrong") for r in three if r["layout"] == "named"
                               for g in ("held_first", "held_later", "held_unnamed"))})
    twins = []
    for r in four:
        if r["source"] == "har":
            twin = next(g for g in new["scenarios"]
                        if _key(g) == ("har", r["ids"], "releases", r["layout"], "as_recorded"))
            twins.append(r["held_out"] == twin["held_out"])
    checks.append({"name": "new: a four-HAR chain serves held-out calls as the recording-folder "
                           "layout does",
                   "pass": bool(twins) and all(twins)})
    named_entries = [r for r in four if r["layout"] == "named" and r["held_out"]]
    checks.append({"name": "new: source chains proved on the named layout serve no wrong page",
                   "pass": bool(named_entries) and _sum(named_entries, "held_first", "wrong")
                   + _sum(named_entries, "held_later", "wrong")
                   + _sum(named_entries, "held_unnamed", "wrong") == 0})
    old_rc = {(r["ids"], r["variant"]): r for r in old.get("recompiles") or []}
    lines.append("")
    lines.append("browser-replay recompile of a /find chain whose releases endpoint moved:")
    lines.append(f"{'ids':3} {'variant':9} {'old':12} {'new':12} {'new status':10} "
                 f"{'new fit':30} new held-out (first/later/unnamed: ok,wrong,refused)")
    for r in new.get("recompiles") or []:
        o = old_rc.get((r["ids"], r["variant"])) or {}

        def rc_cell(x):
            if x.get("crashed"):
                return "CRASH"
            return "swapped" if x.get("swapped") else "not swapped"
        fit = r.get("chain_fit") or {}
        fit_text = (f"{fit.get('value')} ({fit.get('via')})" if fit
                    else f"captured {','.join(r.get('captured') or [])}"[:30])
        lines.append(f"{r['ids']:3} {r['variant']:9} {rc_cell(o):12} {rc_cell(r):12} "
                     f"{r.get('status') or '-':10} {fit_text:30} {held_text(r)}")
    lines.append("reasons (old, recompile):")
    for r in old.get("recompiles") or []:
        lines.append(f"  {r['ids']}/{r['variant']}: {(r['crashed'] or r['reason'])[:240]}")
    recompiles = new.get("recompiles") or []
    checks.append({"name": "browser-replay recompile: every chain compiled from four /find "
                           "sessions in both arms",
                   "pass": bool(recompiles) and all(
                       x.get("compiled") == "proved"
                       for x in [*recompiles, *(old.get("recompiles") or [])])})
    checks.append({"name": "browser-replay recompile: old rebuilds no chain",
                   "pass": bool(old_rc) and not any(x["swapped"] for x in old_rc.values())})
    checks.append({"name": "browser-replay recompile: new rebuilds every chain, status proved on "
                           "the held-out proof inputs; fitted on a captured fitted input, or on "
                           "a generated value replayed in the browser",
                   "pass": bool(recompiles) and all(
                       x["swapped"] and x["status"] == "proved" and x["cases"] == x["held"]
                       and ((x["variant"] == "fitted" and not x["chain_fit"])
                            or (x["variant"] == "generated"
                                and x["chain_fit"].get("via") == "browser"
                                and x["chain_fit"].get("value") in x["captured"]))
                       for x in recompiles)})
    checks.append({"name": "browser-replay recompile: new's rebuilt chains serve held-out calls "
                           "on the moved site as the chain they replace did before the move",
                   "pass": bool(recompiles) and all(
                       x["held_out"] and x["held_out"] == x["held_out_before"]
                       for x in recompiles)})
    checks.append({"name": "new crashes nowhere",
                   "pass": not any(r["crashed"] for r in [*new["scenarios"], *new["entries"],
                                                          *recompiles])})
    lines.append("")
    lines.append(f"arm time: old {old['seconds']}s, new {new['seconds']}s")
    for check in checks:
        lines.append(("PASS " if check["pass"] else "FAIL ") + check["name"])
    return "\n".join(lines), {"old": old, "new": new, "checks": checks}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def _arm(arm, root, tmp, port):
    pk = os.path.join(tmp, "pk-" + arm)
    os.mkdir(pk)
    os.symlink(os.path.realpath(root), os.path.join(pk, "anyuse"))
    out = os.path.join(tmp, arm + ".json")
    work = os.path.join(tmp, "work-" + arm)
    os.makedirs(work)
    shutil.copy(os.path.join(tmp, "index.json"), os.path.join(work, "index.json"))
    home = os.path.join(tmp, "home-" + arm)
    data = os.path.join(home, ".anyuse")
    os.makedirs(data)
    env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
               XDG_DATA_HOME=os.path.join(home, ".local", "share"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"), ANYUSE_DATA_DIR=data,
               ANYUSE_SECRETS_DIR=data, ANYUSE_VAULT_KEYFILE=os.path.join(data, "entity_vault.keys"),
               ANYUSE_API_HOSTS_FILE=os.path.join(data, "api_hosts.json"),
               ANYUSE_BLOCKED_HOSTS_FILE=os.path.join(data, "api_blocked_hosts.json"),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
               NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", ANYUSE_MODEL_CHECKS="none")
    # no browser and no model from the caller's environment: the arms use fakes
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_OPENJEV_URL",
                 "ANYUSE_JUDGE", "ANYUSE_CONFIRMER", "ANYUSE_CONFIRMER_LAYA_DIR",
                 "ANYUSE_CHAIN_FIT"):
        env.pop(name, None)
    cmd = [sys.executable, "-I", "-B", os.path.abspath(__file__), "--arm", arm,
           "--pkgroot", pk, "--out", out, "--work", work, "--port", str(port)]
    return subprocess.Popen(cmd, env=env), out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old-root", help="checkout of the code before the change")
    ap.add_argument("--new-root", default=CHECKOUT)
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--arm", default="", help=argparse.SUPPRESS)
    ap.add_argument("--pkgroot", default="", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--work", default="", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    if ns.arm:
        run_arm(ns)
        return 0
    if not ns.old_root:
        ap.error("--old-root is required: a checkout of the code before the change")
    tmp = tempfile.mkdtemp(prefix="chain-proof-bench-")
    results, named, roles = corpus()
    server = serve(results)
    try:
        record_all(server.server_address[1], tmp, results, named, roles)
        procs = [_arm(arm, root, tmp, server.server_address[1])
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
        server.shutdown()
        for arm in ("old", "new"):
            link = os.path.join(tmp, "pk-" + arm, "anyuse")
            if os.path.islink(link):
                os.unlink(link)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
