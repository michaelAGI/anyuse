"""Web tool version 15, offline (fake pages; no browser, no network, no model): the site's own evidence before the
hand-written lists, for what is the same site, which link is a control, and which parameter is the search's.

What must hold:

1. ``compile_trace`` builds version 15 by default and still builds version 14 exactly; a version-15 card region
   keeps ``cards_version`` 15;
2. "the same site" is the registrable domain for a version-15 tool: the subdomains of a shop under a two-label
   public suffix are one site, the tenants of a shared-hosting suffix are not; a version-14 tool keeps its two-label
   rule;
3. a READ of result links takes a gated-action word in a link's text only when the page uses that text as a label
   (several targets): a title the page gives one target ("Kaufen & Verkaufen") is a title; a label on every card
   ("Jetzt kaufen") is still refused, and a link path to the cart always is; without a differential (one target) the
   old word rule stays;
4. a search template is the site's search when its parameter is the one the site's own result URL carried the typed
   input in, or when it came from the page's structure (a schema.org SearchAction, a GET search form), whatever the
   parameter is called; the e-mail / newsletter refusal stays; the old names still pass.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from test_demo_compile import BASE, product_trace, read_of, spec_for  # noqa: E402
import test_demo_compile as TD  # noqa: E402


def test_version_15_is_built_and_version_14_still_builds_14():
    t15 = C.compile_trace(product_trace())["tool"]
    t14 = C.compile_trace(product_trace(), version=14)["tool"]
    assert C.TOOL_VERSION >= 15 and t15["version"] == C.TOOL_VERSION and C.v15(t15) and C.v14(t15)
    assert t14["version"] == 14 and C.v14(t14) and not C.v15(t14)
    assert 15 in C.COMPILE_VERSIONS and 14 in C.COMPILE_VERSIONS
    if t15["region"].get("cards"):
        assert t15["region"]["cards_version"] == C.CARDS_VERSION_V15 == 15
        assert t14["region"]["cards_version"] == C.CARDS_VERSION_V12
    assert not C.v15({"version": "x"}) and not C.v15(None)


def test_the_same_site_is_the_registrable_domain_under_version_15():
    with W.site_evidence():
        assert W._same_site("m.shop.com.au", "www.shop.com.au")
        assert W._same_site("www.shop.co.za", "shop.co.za")
        assert W._same_site("www.shop.de", "shop.de")
        assert not W._same_site("alice.github.io", "bob.github.io")
        assert not W._same_site("shop.co.uk", "evil.co.uk")
        assert not W._same_site("shop.de", "other.de")
    # the old rule, unchanged outside the switch
    assert not W.site_evidence_first()
    assert not W._same_site("m.shop.com.au", "www.shop.com.au")
    assert W._same_site("alice.github.io", "bob.github.io")
    assert W._same_site("www.shop.de", "shop.de")


def test_a_version_15_tool_reads_a_subdomain_under_a_two_label_suffix_as_its_site():
    t = C.compile_trace(product_trace())["tool"]
    r = read_of(spec_for(f"{BASE}/search?q=Borealis+Toaster+2+Slice"))
    r["url"] = "https://m.shop.com.au/search?q=Borealis+Toaster+2+Slice"
    t15 = dict(t, site="www.shop.com.au")
    t14 = dict(t, site="www.shop.com.au", version=14)
    j14 = C.judge_result_page(t14, r, "Borealis Toaster 2 Slice")
    assert j14["status"] == "failed" and "off the site" in j14["why"], j14
    j15 = C.judge_result_page(t15, r, "Borealis Toaster 2 Slice")
    assert "off the site" not in j15["why"], j15
    # a tenant of a shared-hosting suffix is another site for a version-15 tool, one site for a version-14 one
    r["url"] = "https://bob.github.io/search?q=x"
    assert "off the site" in C.judge_result_page(dict(t, site="alice.github.io"), r, "x")["why"]
    assert "off the site" not in C.judge_result_page(dict(t, site="alice.github.io", version=14), r, "x")["why"]


def test_a_version_15_tools_canonical_link_on_a_sibling_subdomain_is_its_site():
    t15, t14 = C.compile_trace(product_trace())["tool"], C.compile_trace(product_trace(), version=14)["tool"]
    assert t15["fields"].get("evidence_rule") == "15" and "evidence_rule" not in t14["fields"]
    r = {"url": "", "title": "", "h1": ["Borealis Toaster"], "headings": [], "elements": [], "text": "", "jsonld": [],
         "meta": {}, "microdata": {}, "canonical": ""}
    sources = {"url": "canonical", "name": "h1"}
    for canon, want15 in (("https://m.shop.com.au/p/x", "https://m.shop.com.au/p/x"),
                          ("https://other.com.au/p/x", "https://www.shop.com.au/p/x")):
        r2 = dict(r, url="https://www.shop.com.au/p/x", canonical=canon)
        assert C.extract_fields(r2, dict(sources, evidence_rule="15"), "x")["url"] == want15, canon
        assert C.extract_fields(r2, sources, "x")["url"] == "https://www.shop.com.au/p/x", canon   # the old rule
        # a compile builds the same-product URL rule (version 9 on): a canonical link on another host names no
        # product of the page loaded, so the answer stays the URL loaded, the one the run's product check accepts
        same_product = dict(t15["fields"], url="canonical", name="h1")
        assert same_product["url_rule"] == "same_product"
        assert C.extract_fields(r2, same_product, "x")["url"] == "https://www.shop.com.au/p/x", canon


def _links(pairs):
    return [{"i": n, "tag": "a", "text": t, "href": h, "area": "main"} for n, (t, h) in enumerate(pairs)]


def test_a_title_the_page_gives_one_target_is_a_title_whatever_word_it_holds():
    els = _links([("Kaufen & Verkaufen", "https://s.test/a/1"), ("Haus Kaufen", "https://s.test/a/2"),
                  ("Jetzt kaufen", "https://s.test/a/1"), ("Jetzt kaufen", "https://s.test/a/2"),
                  ("Warenkorb", "https://s.test/cart")])
    labels = W.link_labels(els)
    assert labels == {"jetzt kaufen"}, labels
    with W.site_evidence():
        assert W.forbidden_link("Kaufen & Verkaufen", "https://s.test/a/1", labels) is None
        assert W.forbidden_link("Haus Kaufen", "https://s.test/a/2", labels) is None
        assert W.forbidden_link("Jetzt kaufen", "https://s.test/a/1", labels)       # a label: the word rule
        assert W.forbidden_link("Anything", "https://s.test/cart", labels)           # the cart path, always
        # no differential (no labels read): the old rule
        assert W.forbidden_link("Kaufen & Verkaufen", "https://s.test/a/1")
    # the old rule outside the switch, labels or not
    assert W.forbidden_link("Kaufen & Verkaufen", "https://s.test/a/1", labels)
    # a page with one target gives no differential
    assert W.link_labels(_links([("Jetzt kaufen", "https://s.test/a/1"), ("Kaufen", "https://s.test/a/1")])) is None


def test_region_links_keep_a_title_with_a_gated_word_for_a_version_15_read():
    region = {"regex": r"^https://s\.test/a/\d+$"}
    read = {"elements": _links([("Kaufen & Verkaufen", "https://s.test/a/1"), ("Jetzt kaufen", "https://s.test/a/1"),
                                ("Gartenhaus", "https://s.test/a/2"), ("Jetzt kaufen", "https://s.test/a/2")])}
    old = [e["text"] for e in C.region_links(read, region)]
    assert old == ["Gartenhaus"], old
    with W.site_evidence():
        new = [e["text"] for e in C.region_links(read, region)]
    assert new == ["Kaufen & Verkaufen", "Gartenhaus"], new


def test_the_parameter_that_carried_the_typed_input_is_the_sites_search():
    # a Turkish shop's search: neither the parameter nor the path is a listed search word
    tmpl = "https://www.dukkan.test/ara?kelime={q}"
    assert not C.search_template_ok(tmpl)
    with W.site_evidence():
        assert not C.search_template_ok(tmpl)                                    # no evidence: the names decide
        assert C.search_template_ok(tmpl, own=("kelime",))                       # the result URL carried it
        assert C.search_template_ok(tmpl, how="GET search form")                 # the page's own search form
        assert C.search_template_ok("https://www.dukkan.test/x?arama={q}", how="schema.org SearchAction")
        # the refusals stay whatever the evidence
        assert not C.search_template_ok("https://www.dukkan.test/x?email={q}", own=("email",))
        assert not C.search_template_ok("https://www.dukkan.test/newsletter?kelime={q}", how="GET search form")
        # the old names still pass
        assert C.search_template_ok("https://www.shop.test/search?q={q}")
    assert not C.search_template_ok(tmpl, own=("kelime",), how="GET search form")    # outside: the old rule


def test_a_version_15_compile_keeps_a_start_page_fallback_by_the_typed_inputs_parameter():
    tr = product_trace()
    start = tr["steps"][0]["page"]
    start["search_urls"] = ["https://www.shop.test/finden?q={q}", "https://www.shop.test/ara?kelime={q}"]
    # the result URL the site formed carried the typed input in ``kelime``
    tr["steps"][0]["outcome"]["url_after"] = BASE + "/ara?kelime=Acme+Kettle+2000+Steel"
    tr["steps"][1]["page"]["url"] = BASE + "/ara?kelime=Acme+Kettle+2000+Steel"
    t15 = C.compile_trace(tr)["tool"]
    t14 = C.compile_trace(tr, version=14)["tool"]
    assert "https://www.shop.test/ara?kelime={q}" in t15["search"]["fallback_templates"], t15["search"]
    assert "https://www.shop.test/ara?kelime={q}" not in t14["search"]["fallback_templates"], t14["search"]


# ---------------------------------------------------------------------------
# a product page on a sibling subdomain under a two-label suffix, at run time
# ---------------------------------------------------------------------------

WWW_AU, M_AU = "https://www.shop.com.au", "https://m.shop.com.au"


def _moved(obj):
    """The fake shop under a two-label suffix: its search on www., its product pages on m."""
    s = re.sub(r"https://www\.shop\.test/p/", M_AU + "/p/", json.dumps(obj))
    return json.loads(s.replace("https://www.shop.test", WWW_AU))


class SiblingPage(TD.FakeShopPage):
    """``test_demo_compile``'s shop under ``www.shop.com.au``, whose result links open ``m.shop.com.au`` (a click: a
    link to another host is no GET of the page's own)."""

    shown = ""

    async def goto(self, url):
        self.shown = url
        await super().goto(url.replace(WWW_AU, BASE).replace(M_AU, BASE))

    async def js(self, expr, read_only=False):
        from anyuse.execution import web_arch_run as RR
        m = re.search(r"\}\)\((\d+), (\"(?:[^\"\\\\]|\\\\.)*\")\)\s*$", str(expr))
        if m and "n.click()" in expr:
            e = next((x for x in _moved(await super().js(RR.READ_JS))["elements"] if x["i"] == int(m.group(1))), None)
            if e is None or not e.get("href"):
                return {"ok": False, "why": "gone"}
            await self.goto(e["href"])
            return {"ok": True}
        return _moved(await super().js(expr, read_only))

    async def type_search(self, value, want=None, **kw):
        return _moved(await super().type_search(value, want, **kw))

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.shown, "status": 200}]

    async def dom(self):
        return _moved(await super().dom())

    async def url(self):
        return self.shown


def test_a_version_15_tools_product_page_on_a_sibling_subdomain_is_on_its_site():
    tr = _moved(product_trace(value="Borealis Toaster 2 Slice", open_slug="borealis-toaster-2"))
    tr["site"] = "www.shop.com.au"
    t15, t14 = C.compile_trace(tr)["tool"], C.compile_trace(tr, version=14)["tool"]
    for value, slug in (("Borealis Toaster 2 Slice", "borealis-toaster-2"),
                        ("Acme Kettle 2000 Steel", "acme-kettle-2000")):
        out = TD.run_tool(SiblingPage(), t15, value)
        assert out["status"] == "ok" and out["output"]["url"] == f"{M_AU}/p/{slug}", (value, out["status"], out["why"])
        # a version-14 tool keeps its two-label rule
        out = TD.run_tool(SiblingPage(), t14, value)
        assert out["status"] == "failed" and "is not on the site" in out["why"], (value, out["why"])
    # the landing check and an opened result of a visit read the same rule
    page = _moved(read_of(spec_for(f"{BASE}/p/borealis-toaster-2")))
    fields = {"name": "Borealis Toaster 2 Slice", "price": 29.0, "url": f"{M_AU}/p/borealis-toaster-2"}
    assert C.check_landing(page, fields, "Borealis Toaster 2 Slice", "www.shop.com.au", v15=True)[0]
    assert "not on the site" in C.check_landing(page, fields, "Borealis Toaster 2 Slice", "www.shop.com.au")[1]
    from anyuse.execution import demo_breadth as DB
    assert DB.check_item(page, fields, "Borealis Toaster 2 Slice", "www.shop.com.au", v15=True)[0]
    assert not DB.check_item(page, fields, "Borealis Toaster 2 Slice", "www.shop.com.au")[0]
    assert not W.site_evidence_first()


def test_a_version_15_tools_run_follows_its_pagers_link_to_a_sibling_subdomain():
    from anyuse.execution import demo_breadth as DB
    from anyuse.execution import demo_tool as DT
    read = {"url": f"{WWW_AU}/search?q=toaster", "elements": [
        {"i": 0, "tag": "a", "text": "2", "href": f"{M_AU}/search?q=toaster&page=2", "area": "main"}]}
    assert DB.next_link(read, "www.shop.com.au", 1, [read["url"]]) is None              # the two-label rule
    with W.site_evidence():
        assert DB.next_link(read, "www.shop.com.au", 1, [read["url"]])["text"] == "2"
    # the run of a version-15 tool reads under the site's own evidence; an older tool's run keeps its rule
    seen = []

    async def fake_run(page, tool, value, settle, wait, params):
        seen.append(W.site_evidence_first())
        return {"status": "ok"}
    real, DT._run_tool = DT._run_tool, fake_run
    try:
        t15 = C.compile_trace(product_trace())["tool"]
        for tool in (t15, dict(t15, version=14)):
            TD.run_tool(SiblingPage(), tool, "toaster")
    finally:
        DT._run_tool = real
    assert seen == [True, False] and not W.site_evidence_first()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
