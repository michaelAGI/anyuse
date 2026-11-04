"""An eligibility screen, run BEFORE randomisation.

usage: site_screen.py <candidates.tsv> <out.json>

Per candidate host, in file order, at most: ``robots.txt`` (one GET) and ONE GET
of the start URL (the home page), through the paced ``HttpFetcher`` with the
honest User-Agent; redirect hops are counted. Nothing else is requested. From
that single page it decides, offline:

* ``robots``: robots.txt unreadable, 401/403, or disallowing the start/listing URL;
* ``blocked``: the home page is a block/challenge/403/429 page (the host goes to
  the measurement harness);
* ``no_page``: not a 200 HTML page on the same host;
* ``no_search``: no search-like text input in the served HTML that is not hidden;
* ``listing``: the listing cannot be checked: the home page itself yields fewer
  than 5 qualifying values under ``corpus_live``'s rule AND the remembered listing URL is
  not linked from it;
* else ``eligible``, with ``listing_url`` = the home page when it yields >= 5
  values ("checked"), else the remembered, linked listing ("linked"), and the
  observed stratum: ``get_form`` (the input sits in a GET form whose action is
  on the same host) or ``other`` (JS-driven, POST, or no form).
"""
from __future__ import annotations

import html.parser
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = "research-probe/0.1 (read-only; person-paced)"
SEARCHY = re.compile(r"search|query|keyword|\bterm\b|suche|recherche|buscar|find", re.I)
SHORT_NAMES = {"q", "s", "k", "kw", "term", "terms"}
TEXTISH = {"", "text", "search"}


class _Inputs(html.parser.HTMLParser):
    """Every <input> with its enclosing form and a 'hidden' verdict (static HTML)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[Dict[str, Any]] = []
        self.form: Optional[Dict[str, str]] = None
        self.inputs: List[Dict[str, Any]] = []
        self.links: List[str] = []

    VOID = {"input", "br", "img", "meta", "link", "hr", "source", "area", "base", "col",
            "embed", "param", "track", "wbr"}

    @staticmethod
    def _hidden(a: Dict[str, str]) -> bool:
        style = (a.get("style") or "").replace(" ", "").lower()
        return ("hidden" in a or a.get("aria-hidden") == "true"
                or "display:none" in style or "visibility:hidden" in style)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        hidden = self._hidden(a) or any(f["hidden"] for f in self.stack)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "form":
            self.form = {"method": (a.get("method") or "get").lower(),
                         "action": a.get("action") or ""}
        if tag == "input":
            self.inputs.append({"type": (a.get("type") or "").lower(),
                                "name": a.get("name", ""), "id": a.get("id", ""),
                                "placeholder": a.get("placeholder", ""),
                                "aria": a.get("aria-label", ""), "role": a.get("role", ""),
                                "cls": a.get("class", ""),
                                "hidden": hidden or (a.get("type") or "").lower() == "hidden",
                                "form": dict(self.form) if self.form else None})
        if tag not in self.VOID:
            self.stack.append({"tag": tag, "hidden": self._hidden(a)})

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break


def search_inputs(dom: str) -> List[Dict[str, Any]]:
    p = _Inputs()
    try:
        p.feed(dom or "")
    except Exception:
        pass
    out = []
    for i in p.inputs:
        if i["hidden"] or i["type"] not in TEXTISH:
            continue
        blob = " ".join(str(i[k]) for k in ("type", "name", "id", "placeholder", "aria",
                                             "role", "cls"))
        if i["type"] == "search" or SEARCHY.search(blob) or \
                i["name"].lower() in SHORT_NAMES or i["id"].lower() in SHORT_NAMES:
            out.append(i)
    return out


def links(dom: str) -> List[str]:
    p = _Inputs()
    try:
        p.feed(dom or "")
    except Exception:
        pass
    return p.links


def _norm(u: str) -> str:
    s = urllib.parse.urlsplit(u)
    return f"{s.scheme}://{s.netloc.lower()}{s.path.rstrip('/') or ''}?{s.query}"


def classify(site: str, start: str, listing: str, dom: str, final_url: str,
             query_tokens) -> Dict[str, Any]:
    """The offline part of the screen (pure; tested)."""
    out: Dict[str, Any] = {}
    ins = search_inputs(dom)
    if not ins:
        return {"screen": "no_search"}
    toks = query_tokens(dom)
    if len(toks) >= 5:
        out.update(listing_url=start, listing_check="checked", home_values=len(toks))
    else:
        hrefs = {_norm(urllib.parse.urljoin(final_url or start, h)) for h in links(dom)}
        if listing != start and _norm(listing) in hrefs:
            out.update(listing_url=listing, listing_check="linked", home_values=len(toks))
        else:
            return {"screen": "listing", "home_values": len(toks)}
    stratum = "other"
    for i in ins:
        f = i.get("form")
        if f and f["method"] == "get":
            act = urllib.parse.urljoin(final_url or start, f["action"] or (final_url or start))
            if urllib.parse.urlsplit(act).netloc.lower() == site:
                stratum = "get_form"
                break
    out.update(screen="eligible", stratum=stratum, n_search_inputs=len(ins))
    return out


def text_of(dom: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(
        r"(?is)<(script|style)[^>]*>.*?</\1>", " ", dom or ""))).strip()


def main(argv=None) -> int:
    import corpus_live as CL
    import discover_bench as DBN
    import live_bench as V
    from anyuse.execution import discover as D
    args = argv or sys.argv[1:]
    src, dst = args[0], args[1]
    rows = [ln.rstrip("\n").split("\t") for ln in open(src) if ln.strip()]
    out = json.load(open(dst)) if os.path.exists(dst) else {"sites": {}, "order": []}
    pacer = V.HostPacer(1.0)
    fetch = D.HttpFetcher(user_agent=UA, allow_url=lambda u: robots.allow(u), pacer=pacer,
                          max_requests_per_host=V.SITE_CAP)
    robots = DBN.Robots(fetch)
    never = V._never()
    for site, start, listing in rows:
        if site in out["sites"]:
            continue
        rec: Dict[str, Any] = {"site": site, "start_url": start, "remembered_listing": listing,
                               "at": time.strftime("%H:%M:%S")}
        if site in never:
            rec["screen"] = "never_contact"
        else:
            robots.load(site)
            rec["robots"] = robots.notes.get(site)
            if str(rec["robots"]).startswith("robots.txt 40") or "blocked" in str(rec["robots"]):
                V.add_never_contact(site, f"site screen: {rec['robots']}")
                rec["screen"] = "robots"
            elif not robots.allow(start) or not robots.allow(listing):
                rec["screen"] = "robots"
            else:
                d = V.crawl_delay(robots, site)
                if d > 1.0:
                    pacer.intervals[site] = d
                    rec["crawl_delay"] = d
                r = fetch.get(start)
                rec.update(status=r.status, final_url=r.final_url, error=r.error[:120],
                           bytes=len(r.body or ""))
                blk = V.page_block(text_of(r.body), r.final_url or start,
                                   [{"resource_type": "Document", "url": r.final_url or start,
                                     "status": r.status}])
                if r.error.startswith("blocked") or blk or r.status in (401, 403, 429):
                    why = blk or r.error or f"HTTP {r.status}"
                    V.add_never_contact(site, f"site screen: {why}")
                    rec.update(screen="blocked", why=why[:160])
                elif r.status != 200 or D.host_of(r.final_url or start) != site or \
                        "html" not in (r.mime or ""):
                    rec["screen"] = "no_page"
                else:
                    rec.update(classify(site, start, listing, r.body, r.final_url or start,
                                        CL.qualifying_tokens))
                    if rec.get("listing_url") and not robots.allow(rec["listing_url"]):
                        rec["screen"] = "robots"
        rec["requests"] = int(pacer.count.get(site, 0))
        out["sites"][site] = rec
        out["order"].append(site)
        V.save_json(dst, out)
        print(f"{site:40s} {rec.get('screen'):14s} {rec.get('stratum', '')} "
              f"{rec.get('listing_check', '')} req={rec['requests']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
