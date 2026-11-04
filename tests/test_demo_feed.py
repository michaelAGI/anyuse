"""Version 7: a FEED is never a tool's result page. Built from saved evidence of filamentworld.de: the demonstrator
searched twice with the site's box (an HTML result page listing the item), then typed the search's RSS feed URL
(``...&post_type=product&feed=rss2``) and opened the item from the feed's XML; version 6 compiled the FEED URL as the
tool's search, and all three held-out requests replayed the XML and read no item (not even the empty one's statement).
Offline (the fake shop of test_demo_compile.py; no browser, no network, no model).

What must hold:

1. a demonstration whose last search is a feed compiles to its OWN last HTML search (the URL the trace shows), the
   result region read on that HTML page, which must list the opened item; the report says so;
2. with no HTML search in the trace, or an item the HTML page does not list, an explicit compile failure, never a
   tool that reads XML;
3. a version-7 tool that meets a feed at run time (a redirect, a hand-made tool) fails explicitly ("the result page
   is a feed"); a version-6 tool reads the same page as before;
4. the feed tests themselves: ``feed=`` / ``rss=`` / ``atom=``, a feed format parameter, ``.xml`` / ``.rss`` /
   ``/feed/`` paths, an RSS / Atom content type, a page whose text is the XML; ordinary search URLs and pages are
   not feeds; a start page's feed URL is never a search fallback.
"""
import copy
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

VALUE = "Acme Kettle 2000 Steel"
FEED_XML = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"\n\txmlns:atom="http://www.w3.org/2005/Atom"\n\t>'
            '\n\n<channel>\n\t<title>Du hast nach {v} gesucht - Shop Test</title>\n\t<item><link>{base}/p/acme-kettle-2000'
            '</link></item></channel></rss>')


def feed_url_of(value):
    return f"{TC.BASE}/search?q={urllib.parse.quote_plus(value)}&feed=rss2"


def feed_read(url, value=VALUE):
    text = FEED_XML.format(v=value, base=TC.BASE)
    return {"url": url, "title": "", "h1": [], "headings": [], "elements": [], "text": text, "main_text": text,
            "jsonld": [], "facts": [], "canonical": "", "og_type": "", "md_products": 0, "breadcrumb": [], "inputs": [],
            "meta": {}, "microdata": {}, "link_sigs": {},
            "dom": '<html><head><meta name="color-scheme" content="light dark"></head><body><pre>' +
                   text.replace("<", "&lt;").replace(">", "&gt;") + "</pre></body></html>"}


def feed_trace(html_search=True, item_on_html=True):
    """The filamentworld.de shape: a typed HTML search, then the search's feed URL, the item opened from it."""
    tr = TC.product_trace(value=VALUE)
    s0, s1, s2 = tr["steps"]
    furl = feed_url_of(VALUE)
    html_page = s1["page"]
    if not item_on_html:
        html_page["elements"] = [e for e in html_page["elements"] if "/p/acme-kettle-2000" not in str(e.get("href"))]
    steps = [s0,
             {"k": 1, "page": html_page, "action": {"action": "goto", "url": furl},
              "outcome": {"ok": True, "how": "goto", "href": furl, "url_after": furl}},
             {"k": 2, "page": feed_read(furl), "action": {"action": "goto", "url": f"{TC.BASE}/p/acme-kettle-2000"},
              "outcome": {"ok": True, "how": "goto", "href": f"{TC.BASE}/p/acme-kettle-2000",
                          "url_after": f"{TC.BASE}/p/acme-kettle-2000"}},
             dict(s2, k=3)]
    if not html_search:
        # the demonstrator went to the feed directly from the start page (no HTML search at all)
        steps = [dict(steps[1], k=0, page=s0["page"])] + steps[2:]
    tr["steps"] = steps
    return tr


class FeedShop(TC.FakeShopPage):
    """The fake shop whose search answers with its RSS feed when the URL asks for one (``feed=``)."""

    async def js(self, expr, read_only=False):
        if "feed=" not in self.cur:
            return await TC.FakeShopPage.js(self, expr, read_only)
        from anyuse.execution import demo_cards as K
        if expr == RR.READ_JS:
            return feed_read(self.cur)
        got = K.recorded_answer(expr, feed_read(self.cur))
        return got if got is not None else {}

    async def dom(self):
        return feed_read(self.cur)["dom"] if "feed=" in self.cur else await TC.FakeShopPage.dom(self)


def test_a_feed_search_compiles_to_the_demonstrations_own_html_search():
    tr = feed_trace()
    assert C._search_steps(tr) == [0, 1] and C.search_feed(tr, 1) and not C.search_feed(tr, 0)
    got = C.compile_trace(tr)
    assert got["ok"], got
    t = got["tool"]
    assert t["version"] == C.TOOL_VERSION >= 7 and t["search"]["mode"] == "url", t["search"]
    assert t["search"]["template"] == f"{TC.BASE}/search?q={{q}}" and not C.feed_url(t["search"]["template"]), t["search"]
    fd = got["report"]["feed"]
    assert fd["search_step"] == 1 and fd["html_search_step"] == 0 and fd["item_on_html_page"], fd
    assert "feed=rss2" in fd["why"] and got["report"]["search_step"] == 0, got["report"]
    # the region was read on the HTML page: it reads the demonstration's results back, and a held-out value runs
    assert got["report"]["structure"]["kept"] in ("cards", "title links"), got["report"]["structure"]
    page = FeedShop()
    out = TC.run_tool(page, t, "Borealis Toaster 2 Slice")
    assert out["status"] == "ok" and out["output"]["name"] == "Borealis Toaster 2 Slice", out
    assert out["search_via"] == "url" and not any("feed=" in g for g in page.gotos), page.gotos
    # the same demonstration without its feed step compiles to the same search
    plain = C.compile_trace(TC.product_trace(value=VALUE))["tool"]
    assert plain["search"] == t["search"] and plain["fields"] == t["fields"], (plain["search"], t["search"])


def test_no_html_search_or_an_item_off_the_html_page_is_an_explicit_compile_failure():
    got = C.compile_trace(feed_trace(html_search=False))
    assert not got["ok"] and "is a feed" in got["why"] and "no HTML search" in got["why"], got
    assert got["report"]["feed"]["html_search_step"] is None, got["report"]
    got = C.compile_trace(feed_trace(item_on_html=False))
    assert not got["ok"] and "not a link of its HTML result page" in got["why"], got


def test_a_feed_at_run_time_fails_explicitly_and_version_6_reads_as_before():
    t = C.compile_trace(feed_trace())["tool"]
    # a tool that meets a feed (a hand-made template, a redirect): an explicit failure
    bad = copy.deepcopy(t)
    bad["search"]["template"] = f"{TC.BASE}/search?q={{q}}&feed=rss2"
    out = TC.run_tool(FeedShop(), bad, "Zqurvant Oxlembiq")
    assert out["status"] == "failed" and not out["accepted"], out
    assert out["why"].startswith("search_page: the result page is a feed") and "feed=rss2" in out["why"], out["why"]
    j = C.judge_result_page(bad, feed_read(feed_url_of(VALUE)), VALUE)
    assert j["status"] == "failed" and "is a feed" in j["why"], j
    # version 6: the same page is read as before - no item, no statement, no feed check
    out6 = TC.run_tool(FeedShop(), dict(bad, version=6), "Zqurvant Oxlembiq")
    assert out6["status"] == "failed" and "feed" not in out6["why"], out6
    j6 = C.judge_result_page(dict(bad, version=6), feed_read(feed_url_of(VALUE)), VALUE)
    assert j6["status"] == "failed" and "feed" not in j6["why"], j6


def test_what_a_feed_is():
    for u in ("https://filamentworld.de/?s=x&post_type=product&feed=rss2&switch_shop=b2c", "https://a.test/search?q=x&rss=1",
              "https://a.test/search?q=x&format=atom", "https://a.test/search/x/feed/", "https://a.test/search/x/feed/rss2/",
              "https://a.test/feeds/search.xml?q=x", "https://a.test/search.rss?q=x", "https://a.test/s?q=x&output=RSS"):
        assert C.feed_url(u), u
    for u in ("https://filamentworld.de/?s=x&post_type=product&dgwt_wcas=1&switch_shop=b2c", "https://a.test/search?q=feed",
              "https://a.test/feedback?q=x", "https://a.test/search?type=product&q=x", "https://a.test/rss-guide/search?q=x",
              "https://a.test/catalogsearch/result/?q=x", "", None):
        assert not C.feed_url(u), u
    assert C.feed_page({"url": "https://a.test/s?q=x", "text": FEED_XML})
    assert C.feed_page({"url": "https://a.test/s?q=x", "text": '<feed xmlns="http://www.w3.org/2005/Atom">'})
    assert C.feed_page({"url": "https://a.test/s?q=x", "content_type": "application/rss+xml; charset=UTF-8", "text": ""})
    assert C.feed_page({"url": "https://a.test/s", "text": "This XML file does not appear to have any style information "
                                                          "associated with it."})
    assert not C.feed_page({"url": "https://a.test/s?q=x", "text": "3 results for x\n<b>Acme</b>"})
    assert not C.feed_page(TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Acme")))
    # a start page's feed search URL is never a fallback of the compiled search
    tr = TC.product_trace()
    tr["steps"][0]["page"]["search_urls"] = [f"{TC.BASE}/search?q={{q}}&feed=rss2", f"{TC.BASE}/search?q={{q}}"]
    t = C.compile_trace(tr)["tool"]
    assert t["search"]["fallback_templates"] == [f"{TC.BASE}/search?q={{q}}"], t["search"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
