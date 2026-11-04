"""An eligibility screen for the ``query_values`` rule, run BEFORE randomisation.

usage: value_screen.py <candidates.tsv> <out.json>

The screen of ``execution/web_runtime/site_screen.py`` (robots.txt + ONE GET of the home page,
paced, honest User-Agent, redirect hops counted, nothing else) with one change:
the listing check uses the ``query_values`` rule (``query_values.listing_tokens``) and
needs ``N_ELIGIBLE`` = 4 tokens (``site_screen``: ``corpus_live``'s rule, 5 tokens). Outcomes,
strata and NEVER_CONTACT handling are ``site_screen``'s.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import site_screen as S  # noqa: E402

UA = S.UA


def classify(site: str, start: str, listing: str, dom: str, final_url: str,
             query_tokens, min_tokens: int = 4) -> Dict[str, Any]:
    """``site_screen``'s classify with the token threshold as a parameter (pure; tested)."""
    out: Dict[str, Any] = {}
    ins = S.search_inputs(dom)
    if not ins:
        return {"screen": "no_search"}
    toks = query_tokens(dom)
    if len(toks) >= min_tokens:
        out.update(listing_url=start, listing_check="checked", home_values=len(toks))
    else:
        hrefs = {S._norm(urllib.parse.urljoin(final_url or start, h)) for h in S.links(dom)}
        if listing != start and S._norm(listing) in hrefs:
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


def classify_safe(site, start, listing, dom, final_url, query_tokens, min_tokens=4):
    """A home page beyond the HTML parser's limits (node count or nesting
    depth) raised out of the screen (www.mushroomexpert.com, nesting > 128).
    Such a page cannot be read by the compiler either: ``no_page``."""
    from anyuse.execution.html_extract import HtmlLimitError
    try:
        return classify(site, start, listing, dom, final_url, query_tokens,
                        min_tokens=min_tokens)
    except HtmlLimitError as exc:
        return {"screen": "no_page", "why": f"html limit: {exc}"[:160]}


def main(argv=None) -> int:
    import discover_bench as DBN
    import live_bench as V
    import query_values as VV
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
    tokens = lambda dom: VV.listing_tokens(dom)          # noqa: E731
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
                V.add_never_contact(site, f"value screen: {rec['robots']}")
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
                blk = V.page_block(S.text_of(r.body), r.final_url or start,
                                   [{"resource_type": "Document", "url": r.final_url or start,
                                     "status": r.status}])
                if r.error.startswith("blocked") or blk or r.status in (401, 403, 429):
                    why = blk or r.error or f"HTTP {r.status}"
                    V.add_never_contact(site, f"value screen: {why}")
                    rec.update(screen="blocked", why=why[:160])
                elif r.status != 200 or D.host_of(r.final_url or start) != site or \
                        "html" not in (r.mime or ""):
                    rec["screen"] = "no_page"
                else:
                    rec.update(classify_safe(site, start, listing, r.body,
                                             r.final_url or start, tokens,
                                             min_tokens=VV.N_ELIGIBLE))
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
