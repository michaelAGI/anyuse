"""The repair ladder's CLOSED patch language. PURE: no page, no network, no model.

A repair of a compiled web tool (version >= 10) is a PATCH: a small JSON object of at most ``MAX_OPS`` operations, each
of one of five kinds. Tier 1 (the deterministic re-locate) and tier 2 (the small-model patch) both answer in this
language; ``validate`` checks an answer against it and ``apply`` applies a valid one to a COPY of the tool.

The five operations (no other key, no other value type is accepted):

* ``replace_anchor`` - re-find a step's target: ``anchor`` is one of
  ``cards`` (the result containers: 1-4 card specs, each a 4-descriptor chain, title paths and link roles),
  ``search_input`` (a typed search's box: its ``name`` / ``id`` / ``index``),
  ``refine_control`` (a refinement control found by its label: ``index`` of the control, its ``text`` / ``option_text``),
  ``pager`` (the page mechanism: a page / offset parameter, a path, or the next-page link);
* ``rebind_param`` - move the input's location in the search URL: a query parameter ``key`` (optionally on a new
  same-site ``path``) or a path segment;
* ``rename_url_param`` - rename one URL parameter of the search, a refinement or the pager (``from`` -> ``to``);
* ``insert_nav_step`` - ONE read-only navigation after the search: follow the same-site link with this text and path
  (a GET, never a form, never a control ``web_arch.forbidden_control`` refuses);
* ``skip_nav_step`` - remove such an inserted step.

THE HARD BOUNDARY. Nothing in the language names the tool's checks, the fields' rules, the workflow, the version, the
declared hosts (the origin policy), the start URL, the region's URL pattern, the counts and caps (``n``, ``k``,
``top_k``, ``max_pages``: the traffic budgets) or the oracle; nor a method, a form, a body, a login or another host.
Three independent layers hold it:

1. the schema: an unknown operation, anchor, scope or key, a wrong type, an over-long value is rejected;
2. the values: descriptors and parameter names match strict patterns; a path is a same-site absolute path that is no
   login / account / cart / checkout / admin / API path (``web_arch.forbidden_control`` and ``FORBIDDEN_PATH``); a
   template keeps the tool's own scheme and host; a link text is plain text;
3. the result: ``apply`` checks that the patched tool's PROTECTED VIEW (everything outside ``MUTABLE``) and its origin
   policy (``demo_compile.site_hosts_policy``) are byte-identical to the original's, and that every URL in it is on
   the tool's own hosts - so even a bug in an operation cannot reach a check, a budget or the origin policy.

``test_repair_patch.py`` feeds model answers that try each forbidden change; every one is rejected.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C

#: the operations of the language
OPS = ("replace_anchor", "rebind_param", "rename_url_param", "insert_nav_step", "skip_nav_step")
ANCHORS = ("cards", "search_input", "refine_control", "pager")
SCOPES = ("search", "refine", "pager")
PAGER_MODES = ("param", "path_append", "path_segment", "link")
MAX_OPS = 3
MAX_CARDS = 4
MAX_RELS = 6
MAX_NAV = 1
MAX_TEXT = 120
MAX_PATH = 200
MAX_WHY = 400

#: the tool keys a patch may change - everything else is the PROTECTED VIEW
MUTABLE = (("search", "template"), ("search", "fallback_templates"), ("search", "input"), ("region", "cards"),
           ("refine", "params"), ("refine", "controls"), ("pager",), ("params",), ("applied",), ("repair_nav",))

#: a card-chain descriptor (``demo_cards.desc``: ``tag#id.class.class``, an id / class never holds a digit) with an
#: optional position among same-descriptor siblings (``div@2``); an empty descriptor is the document root's. The card
#: reads compare descriptors as STRINGS (never as a CSS selector), so a class may hold any character but white space,
#: a digit, a quote, markup, a backslash and the separators ``.#@``
_NAME = r"[^\s\d.#@\"'<>\\\x00-\x1f\x7f\u0001]{1,60}"
_DESC = re.compile(r"^(?:[a-z][a-z0-9-]{0,30}(?:#" + _NAME + r")?(?:\." + _NAME + r"){0,2}(?:@\d{1,2})?)?$")
#: a URL parameter name
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-\[\]]{0,39}$")
#: a path-segment pattern of the pager (``seite-{page}``)
_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]{0,30}\{page\}[A-Za-z0-9_.\-]{0,30}$")
_SUFFIX = re.compile(r"^(?:[A-Za-z0-9_.\-]{1,30}/){0,1}\{page\}$")
#: a path no repair may navigate to or bind a search on (beyond ``web_arch.forbidden_control``'s control paths)
FORBIDDEN_PATH = re.compile(r"(?i)(?:^|[/_.\-])(?:log-?in|log-?out|sign-?in|sign-?up|signon|register|registrier\w*|"
                            r"anmeld\w*|abmeld\w*|account|konto|mein-?konto|my-?account|profile?|password|passwort|"
                            r"cart|basket|bag|warenkorb|panier|carrito|carrello|winkelwagen|kundvagn|koszyk|checkout|kasse|"
                            r"bestell\w*|order|orders|payment|pay|zahlung|admin|wp-admin|wp-login|api|graphql|ajax|"
                            r"wishlist|merkzettel|newsletter|subscribe|abonn\w*|contact|kontakt|upload|delete|remove)"
                            r"(?:$|[/_.\-])")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SCHEME = re.compile(r"(?i)\b(?:https?|javascript|data|file|ftp|mailto|tel|blob|chrome|about):")


class PatchError(ValueError):
    """A patch outside the language (``validate``) or one whose result leaves the boundary (``apply``)."""


# ---------------------------------------------------------------------------
# value checks
# ---------------------------------------------------------------------------

def _obj(x: Any, where: str, keys: Sequence[str], required: Sequence[str] = ()) -> Dict[str, Any]:
    if not isinstance(x, dict):
        raise PatchError(f"{where}: an object is required")
    extra = [k for k in x if k not in keys]
    if extra:
        raise PatchError(f"{where}: unknown key(s) {sorted(map(str, extra))[:4]}")
    miss = [k for k in required if k not in x]
    if miss:
        raise PatchError(f"{where}: missing key(s) {miss}")
    return x


def _int(x: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
        raise PatchError(f"{where}: an integer in [{lo}, {hi}] is required")
    return x


def _text(x: Any, where: str, limit: int = MAX_TEXT, empty: bool = True) -> str:
    if not isinstance(x, str) or len(x) > limit or _CONTROL_CHARS.search(x) or (not empty and not x.strip()):
        raise PatchError(f"{where}: plain text of <= {limit} characters is required")
    if _SCHEME.search(x) or "<" in x or ">" in x:
        raise PatchError(f"{where}: a URL scheme or markup in a text")
    return x


def _desc(x: Any, where: str) -> str:
    if not isinstance(x, str) or not _DESC.match(x):
        raise PatchError(f"{where}: not a descriptor (tag#id.class)")
    return x


def _key(x: Any, where: str) -> str:
    if not isinstance(x, str) or not _KEY.match(x):
        raise PatchError(f"{where}: not a URL parameter name")
    return x


def _path(x: Any, where: str) -> str:
    """A same-site ABSOLUTE path (no scheme, no host, no query) that is no login / account / cart / API path."""
    if not isinstance(x, str) or not x.startswith("/") or x.startswith("//") or len(x) > MAX_PATH:
        raise PatchError(f"{where}: an absolute path on the site is required")
    if any(c in x for c in "?#\\ \t") or ".." in x or _CONTROL_CHARS.search(x) or ":" in x or "@" in x:
        raise PatchError(f"{where}: a path with a query, a fragment, a scheme or a traversal")
    from anyuse.execution import web_arch as W
    if FORBIDDEN_PATH.search(x) or W.forbidden_control("", "https://x.invalid" + x):
        raise PatchError(f"{where}: a login / account / cart / checkout / admin / API path is never a repair")
    return x


def _rel(x: Any, where: str) -> str:
    if not isinstance(x, str) or len(x) > 1200:
        raise PatchError(f"{where}: a title path is required")
    if x:
        for d in x.split("\u0001"):
            _desc(d, where)
    return x


def _card(x: Any, where: str) -> Dict[str, Any]:
    _obj(x, where, ("kind", "chain", "rels", "roles"), ("kind", "chain", "rels"))
    if x["kind"] not in ("card", "title"):
        raise PatchError(f"{where}.kind: 'card' or 'title'")
    ch = x["chain"]
    if not isinstance(ch, list) or len(ch) != 4:
        raise PatchError(f"{where}.chain: 4 descriptors")
    for k, d in enumerate(ch):
        _desc(d, f"{where}.chain[{k}]")
    if not ch[3]:
        raise PatchError(f"{where}.chain: the card's own descriptor is empty")
    rels = x["rels"]
    if not isinstance(rels, list) or not 1 <= len(rels) <= MAX_RELS:
        raise PatchError(f"{where}.rels: 1-{MAX_RELS} title paths")
    for k, r in enumerate(rels):
        _rel(r, f"{where}.rels[{k}]")
    out = {"kind": x["kind"], "chain": list(ch), "rels": list(rels)}
    if "roles" in x:
        roles = x["roles"]
        if not isinstance(roles, list) or len(roles) > MAX_RELS:
            raise PatchError(f"{where}.roles: <= {MAX_RELS} roles")
        out["roles"] = []
        for k, r in enumerate(roles):
            _obj(r, f"{where}.roles[{k}]", ("anchor", "heading"), ("anchor", "heading"))
            _desc(r["anchor"], f"{where}.roles[{k}].anchor")
            if not isinstance(r["heading"], bool):
                raise PatchError(f"{where}.roles[{k}].heading: a boolean")
            out["roles"].append({"anchor": r["anchor"], "heading": r["heading"]})
    return out


def _pager(x: Any, where: str) -> Dict[str, Any]:
    _obj(x, where, ("mode", "key", "first", "stride", "suffix", "index", "pattern", "link"), ("mode",))
    m = x["mode"]
    if m not in PAGER_MODES:
        raise PatchError(f"{where}.mode: one of {PAGER_MODES}")
    out: Dict[str, Any] = {"mode": m}
    if m == "param":
        _obj(x, where, ("mode", "key", "first", "stride", "link"), ("key", "first", "stride"))
        out.update(key=_key(x["key"], f"{where}.key"), first=_int(x["first"], f"{where}.first", 0, 1),
                   stride=_int(x["stride"], f"{where}.stride", 1, 200))
    elif m == "path_append":
        _obj(x, where, ("mode", "suffix", "link"), ("suffix",))
        if not isinstance(x["suffix"], str) or not _SUFFIX.match(x["suffix"]) or \
                FORBIDDEN_PATH.search("/" + x["suffix"].replace("{page}", "2")):
            raise PatchError(f"{where}.suffix: 'page/{{page}}' or '{{page}}'")
        out["suffix"] = x["suffix"]
    elif m == "path_segment":
        _obj(x, where, ("mode", "index", "pattern", "link"), ("index", "pattern"))
        out["index"] = _int(x["index"], f"{where}.index", 0, 12)
        if not isinstance(x["pattern"], str) or not _SEGMENT.match(x["pattern"]):
            raise PatchError(f"{where}.pattern: a segment holding {{page}}")
        out["pattern"] = x["pattern"]
    else:
        _obj(x, where, ("mode", "link"), ("link",))
    if "link" in x:
        lk = _obj(x["link"], f"{where}.link", ("text", "rel"), ("text", "rel"))
        rel = _text(lk["rel"], f"{where}.link.rel", 20)
        if rel not in ("", "next"):
            raise PatchError(f"{where}.link.rel: '' or 'next'")
        out["link"] = {"text": _text(lk["text"], f"{where}.link.text", 40), "rel": rel}
    return out


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------

def validate(patch: Any, tool: Dict[str, Any]) -> Dict[str, Any]:
    """The patch, checked against the language and against the TOOL it is for (an operation on a step the tool does not
    have is rejected): a normalized copy, or ``PatchError``. Accepts the model's JSON text too."""
    if isinstance(patch, (str, bytes)):
        try:
            patch = json.loads(patch)
        except ValueError as exc:
            raise PatchError(f"not JSON ({str(exc)[:80]})") from None
    _obj(patch, "patch", ("ops", "why"), ("ops",))
    ops = patch["ops"]
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        raise PatchError(f"patch.ops: 1-{MAX_OPS} operations")
    why = patch.get("why", "")
    if not isinstance(why, str) or len(why) > MAX_WHY or _CONTROL_CHARS.sub("", why.replace("\n", " ")) != \
            why.replace("\n", " "):
        raise PatchError("patch.why: plain text")
    if not C.v10(tool):
        raise PatchError("the ladder repairs version-10 tools only")
    out = [_op(o, k, tool) for k, o in enumerate(ops)]
    ins = sum(1 for o in out if o["op"] == "insert_nav_step")
    skip = sum(1 for o in out if o["op"] == "skip_nav_step")
    if len(tool.get("repair_nav") or []) + ins - skip > MAX_NAV or skip > len(tool.get("repair_nav") or []):
        raise PatchError(f"patch: at most {MAX_NAV} inserted navigation step")
    if sum(1 for o in out if o["op"] == "replace_anchor" and o["anchor"] == "cards") > 1:
        raise PatchError("patch: the cards are replaced once")
    return {"ops": out, "why": why[:MAX_WHY]}


def _op(o: Any, k: int, tool: Dict[str, Any]) -> Dict[str, Any]:
    w = f"ops[{k}]"
    if not isinstance(o, dict) or o.get("op") not in OPS:
        raise PatchError(f"{w}.op: one of {OPS}")
    op = o["op"]
    if op == "replace_anchor":
        _obj(o, w, ("op", "anchor", "index", "value"), ("op", "anchor", "value"))
        a = o["anchor"]
        if a not in ANCHORS:
            raise PatchError(f"{w}.anchor: one of {ANCHORS}")
        if a == "cards":
            _obj(o, w, ("op", "anchor", "value"))
            if not (tool.get("region") or {}).get("cards"):
                raise PatchError(f"{w}: the tool has no result cards")
            v = o["value"]
            if not isinstance(v, list) or not 1 <= len(v) <= MAX_CARDS:
                raise PatchError(f"{w}.value: 1-{MAX_CARDS} card specs")
            return {"op": op, "anchor": a, "value": [_card(c, f"{w}.value[{j}]") for j, c in enumerate(v)]}
        if a == "search_input":
            _obj(o, w, ("op", "anchor", "value"))
            if (tool.get("search") or {}).get("mode") != "typed":
                raise PatchError(f"{w}: the tool's search is not typed")
            v = _obj(o["value"], f"{w}.value", ("name", "id", "index"))
            nv: Dict[str, Any] = {}
            if "name" in v:
                nv["name"] = _key(v["name"], f"{w}.value.name")
            if "id" in v:
                nv["id"] = _key(v["id"], f"{w}.value.id")
            if "index" in v:
                nv["index"] = _int(v["index"], f"{w}.value.index", 0, 20)
            if not nv:
                raise PatchError(f"{w}.value: name, id or index")
            return {"op": op, "anchor": a, "value": nv}
        if a == "refine_control":
            ctl = (tool.get("refine") or {}).get("controls") or []
            if (tool.get("refine") or {}).get("mode") != "controls" or not ctl:
                raise PatchError(f"{w}: the tool refines by no controls")
            i = _int(o.get("index"), f"{w}.index", 0, len(ctl) - 1)
            v = _obj(o["value"], f"{w}.value", ("text", "option_text"))
            nv = {kk: _text(v[kk], f"{w}.value.{kk}", MAX_TEXT, empty=False) for kk in ("text", "option_text") if kk in v}
            if not nv:
                raise PatchError(f"{w}.value: text or option_text")
            from anyuse.execution import web_arch as W
            if any(W.forbidden_control(t) for t in nv.values()):
                raise PatchError(f"{w}.value: a cart / login / checkout control is never a refinement")
            return {"op": op, "anchor": a, "index": i, "value": nv}
        _obj(o, w, ("op", "anchor", "value"))
        if tool.get("workflow") != "paged_search" or not isinstance(tool.get("pager"), dict):
            raise PatchError(f"{w}: the tool has no pager")
        return {"op": op, "anchor": a, "value": _pager(o["value"], f"{w}.value")}
    if op == "rebind_param":
        _obj(o, w, ("op", "param", "to"), ("op", "param", "to"))
        if o["param"] != "q":
            raise PatchError(f"{w}.param: only the input 'q' is bound in a search URL")
        if (tool.get("search") or {}).get("mode") != "url" and not (tool.get("search") or {}).get("fallback_templates"):
            raise PatchError(f"{w}: the tool has no search URL")
        to = _obj(o["to"], f"{w}.to", ("in", "key", "segment", "path"), ("in",))
        if to["in"] == "query":
            _obj(to, f"{w}.to", ("in", "key", "path"), ("in", "key"))
            nt = {"in": "query", "key": _key(to["key"], f"{w}.to.key")}
        elif to["in"] == "path":
            _obj(to, f"{w}.to", ("in", "segment", "path"), ("in", "segment"))
            nt = {"in": "path", "segment": _int(to["segment"], f"{w}.to.segment", 0, 8)}
        else:
            raise PatchError(f"{w}.to.in: 'query' or 'path'")
        if "path" in to:
            nt["path"] = _path(to["path"], f"{w}.to.path")
        return {"op": op, "param": "q", "to": nt}
    if op == "rename_url_param":
        _obj(o, w, ("op", "scope", "from", "to"), ("op", "scope", "from", "to"))
        sc = o["scope"]
        if sc not in SCOPES:
            raise PatchError(f"{w}.scope: one of {SCOPES}")
        f, t = _key(o["from"], f"{w}.from"), _key(o["to"], f"{w}.to")
        if f == t:
            raise PatchError(f"{w}: the same name")
        have = _scope_keys(tool, sc)
        if f not in have:
            raise PatchError(f"{w}.from: the tool's {sc} has no parameter {f!r}")
        if t in have:
            raise PatchError(f"{w}.to: the tool's {sc} already has {t!r}")
        return {"op": op, "scope": sc, "from": f, "to": t}
    if op == "insert_nav_step":
        _obj(o, w, ("op", "at", "link"), ("op", "at", "link"))
        if o["at"] != "after_search":
            raise PatchError(f"{w}.at: 'after_search'")
        if len(tool.get("repair_nav") or []) >= MAX_NAV:
            raise PatchError(f"{w}: the tool already has {MAX_NAV} inserted step")
        lk = _obj(o["link"], f"{w}.link", ("text", "path"), ("text", "path"))
        text = _text(lk["text"], f"{w}.link.text", MAX_TEXT, empty=False)
        path = _path(lk["path"], f"{w}.link.path")
        from anyuse.execution import web_arch as W
        if W.forbidden_control(text, "https://x.invalid" + path):
            raise PatchError(f"{w}.link: a control the engine never follows")
        return {"op": op, "at": "after_search", "link": {"text": text, "path": path}}
    _obj(o, w, ("op", "index"), ("op", "index"))
    nav = tool.get("repair_nav") or []
    if not nav:
        raise PatchError(f"{w}: the tool has no inserted navigation step")
    return {"op": op, "index": _int(o["index"], f"{w}.index", 0, len(nav) - 1)}


def _scope_keys(tool: Dict[str, Any], scope: str) -> List[str]:
    if scope == "search":
        s = tool.get("search") or {}
        ts = [s.get("template")] if s.get("mode") == "url" else []
        ts += list(s.get("fallback_templates") or [])
        keys: List[str] = []
        for t in ts:
            for k, _v in urllib.parse.parse_qsl(urllib.parse.urlsplit(str(t or "")).query, keep_blank_values=True):
                if k not in keys:
                    keys.append(k)
        return keys
    if scope == "refine":
        r = tool.get("refine") or {}
        return list((r.get("params") or {}).keys()) if r.get("mode") == "url" else []
    p = tool.get("pager") or {}
    return [p["key"]] if p.get("mode") == "param" and p.get("key") else []


# ---------------------------------------------------------------------------
# the protected view and the application
# ---------------------------------------------------------------------------

def protected_view(tool: Dict[str, Any]) -> Dict[str, Any]:
    """The tool without its MUTABLE keys (and with the bound parameters' kinds, defaults and sort keys kept, the names
    left out): everything a patch must leave byte-identical - the checks, the fields' rules, the workflow, the version,
    the declared hosts, the start URL, the region's URL pattern, the counts and caps."""
    t = copy.deepcopy(tool)
    for path in MUTABLE:
        d = t
        for k in path[:-1]:
            d = d.get(k) if isinstance(d, dict) else None
        if isinstance(d, dict):
            d.pop(path[-1], None)
    t["_params"] = [{k: v for k, v in p.items() if k != "name"} for p in tool.get("params") or [] if isinstance(p, dict)]
    t["_applied"] = [{k: v for k, v in a.items() if k != "param"} for a in tool.get("applied") or []
                     if isinstance(a, dict)]
    ref = tool.get("refine") or {}
    t["_refine_values"] = sorted(map(str, (ref.get("params") or {}).values())) if isinstance(ref.get("params"), dict) else []
    t["_controls"] = [{k: v for k, v in c.items() if k not in ("text", "option_text")} for c in ref.get("controls") or []
                      if isinstance(c, dict)]
    t["_search_hosts"] = sorted({_scheme_host(u) for u in _templates(tool)})
    return t


def protected_digest(tool: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(protected_view(tool), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _templates(tool: Dict[str, Any]) -> List[str]:
    s = tool.get("search") or {}
    return [str(u) for u in ([s.get("template")] + list(s.get("fallback_templates") or [])) if u]


def _scheme_host(u: str) -> str:
    p = urllib.parse.urlsplit(str(u or ""))
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def _rebind(template: str, to: Dict[str, Any]) -> str:
    """``template`` with ``{q}`` moved to ``to`` (a query parameter, optionally on a new path, or a path segment); the
    other query parameters kept."""
    p = urllib.parse.urlsplit(template)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if "{q}" not in v]
    if to.get("path"):
        path = to["path"]
    else:
        segs = [s for s in p.path.split("/") if s and "{q}" not in s]
        path = "/" + "/".join(segs) + ("/" if segs and p.path.endswith("/") else "")
    if to["in"] == "query":
        query = urllib.parse.urlencode(q + [(to["key"], "QQQ")], safe="[]").replace("QQQ", "{q}")
        return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, ""))
    s2 = [s for s in path.split("/") if s]
    s2.insert(min(int(to["segment"]), len(s2)), "{q}")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/" + "/".join(s2), urllib.parse.urlencode(q, safe="[]"), ""))


def _rename_q(url: str, f: str, t: str) -> str:
    p = urllib.parse.urlsplit(url)
    q = [(t if k == f else k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
    query = urllib.parse.urlencode(q, safe="{}[]").replace("%7Bq%7D", "{q}")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, query, p.fragment))


def apply(tool: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """The patched COPY of ``tool`` (the original is never changed). ``patch`` must be ``validate``'s answer for this
    tool; the result is checked against the boundary (``check_boundary``) before it is returned."""
    p = validate(patch, tool)
    t = copy.deepcopy(tool)
    for o in p["ops"]:
        op = o["op"]
        if op == "replace_anchor":
            a = o["anchor"]
            if a == "cards":
                t["region"]["cards"] = copy.deepcopy(o["value"])
            elif a == "search_input":
                t["search"]["input"] = dict(o["value"])
            elif a == "refine_control":
                t["refine"]["controls"][o["index"]].update(o["value"])
            else:
                t["pager"] = dict(o["value"])
        elif op == "rebind_param":
            s = t["search"]
            if s.get("mode") == "url":
                s["template"] = _rebind(s["template"], o["to"])
            s["fallback_templates"] = [_rebind(u, o["to"]) for u in s.get("fallback_templates") or []]
        elif op == "rename_url_param":
            f, to = o["from"], o["to"]
            if o["scope"] == "search":
                s = t["search"]
                if s.get("mode") == "url":
                    s["template"] = _rename_q(s["template"], f, to)
                s["fallback_templates"] = [_rename_q(u, f, to) for u in s.get("fallback_templates") or []]
            elif o["scope"] == "refine":
                r = t["refine"]
                r["params"] = {(to if k == f else k): v for k, v in r["params"].items()}
                for prm in t.get("params") or []:
                    if prm.get("name") == f:
                        prm["name"] = to
                for a in t.get("applied") or []:
                    if a.get("param") == f:
                        a["param"] = to
            else:
                t["pager"]["key"] = to
        elif op == "insert_nav_step":
            t.setdefault("repair_nav", []).append({"at": o["at"], "link": dict(o["link"])})
        else:
            del t["repair_nav"][o["index"]]
            if not t["repair_nav"]:
                t.pop("repair_nav")
    check_boundary(tool, t)
    return t


def check_boundary(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """The third layer: the protected view, the origin policy and the hosts of every URL are unchanged; the search
    templates still bind the input once and keep the tool's scheme and host. ``PatchError`` otherwise."""
    if protected_digest(before) != protected_digest(after):
        raise PatchError("boundary: the patch changes a protected key (checks, fields, hosts, workflow, budgets, ...)")
    if C.site_hosts_policy(before) != C.site_hosts_policy(after):
        raise PatchError("boundary: the patch changes the origin policy")
    hosts = {_scheme_host(u) for u in _templates(before)}
    for u in _templates(after):
        if _scheme_host(u) not in hosts or u.count("{q}") != 1:
            raise PatchError("boundary: a search template on another host, or not binding the input once")
        if FORBIDDEN_PATH.search(urllib.parse.urlsplit(u).path or "/"):
            raise PatchError("boundary: a search template on a login / account / cart / API path")
    nav = after.get("repair_nav") or []
    if not isinstance(nav, list) or len(nav) > MAX_NAV:
        raise PatchError(f"boundary: more than {MAX_NAV} inserted navigation step")
    for st in nav:
        _obj(st, "boundary: repair_nav", ("at", "link"), ("at", "link"))
        _obj(st["link"], "boundary: repair_nav.link", ("text", "path"), ("text", "path"))
        _text(st["link"]["text"], "boundary: repair_nav.text", MAX_TEXT, empty=False)
        _path(st["link"]["path"], "boundary: repair_nav")
