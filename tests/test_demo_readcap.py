"""Web tool version 11: the results past the reader's cap of controls.

The case (www.compass24.de): the result page's navigation showed 769 links, so
``READ_JS`` (at most 700 visible controls, in page order) stopped before the result tile - "membership: no item of
the compiled region on the result page" for an existing product, while the tile was in the page.

What must hold (fakes shaped on that page):

1. ``READ_JS_V11`` is ``READ_JS`` with the cap counted on the controls outside the header, navigation and footer;
2. ``read_page11`` reads once more only when the reader stopped at its cap, and keeps the longer list;
3. a version-11 tool reads the tile behind a 760-link navigation; a version-10 tool fails as it did (its read is
   unchanged).
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

SITE = "www.boat.test"
BASE = f"https://{SITE}"
VALUE = "Seajet 2300 Antifouling Biozidfrei"


def elements(n_nav=760):
    els = [{"i": k, "tag": "a", "href": f"{BASE}/kategorie/{k}", "text": f"Kategorie {k}", "area": "nav"}
           for k in range(n_nav)]
    els.append({"i": n_nav, "tag": "a", "href": f"{BASE}/seajet-2300-antifouling-biozidfrei/922100",
                "text": "Seajet 2300 Antifouling, Biozidfrei", "area": "main"})
    return els


class Page:
    """The result page: ``READ_JS`` stops at 700 visible controls, ``READ_JS_V11`` counts outside the navigation."""

    def __init__(self, n_nav=760):
        self.all = elements(n_nav)
        self.reads = []

    async def js(self, expr, read_only=False):
        if expr == RR.READ_JS:
            self.reads.append("v")
            return {"url": BASE + "/search?search=x", "elements": self.all[:700], "text": "Ergebnisse"}
        if expr == RR.READ_JS_V11:
            self.reads.append("v11")
            return {"url": BASE + "/search?search=x", "elements": self.all, "text": "Ergebnisse"}
        return None

    async def search_inputs(self):
        return []

    async def settle(self, s):
        await asyncio.sleep(0)


def test_the_v11_read():
    assert RR.READ_JS_V11.startswith("/*anyuse:read11*/") and "body < 700 && els.length < 3000" in RR.READ_JS_V11
    assert "els.length < 700" in RR.READ_JS and "let body" not in RR.READ_JS
    assert RR.READ_JS_V11.replace("/*anyuse:read11*/", "").replace(
        "  let body = 0;\n  for (let k = 0; k < all.length && body < 700 && els.length < 3000; k++) {",
        "  for (let k = 0; k < all.length && els.length < 700; k++) {").replace(
        " if (['header', 'nav', 'footer'].indexOf(e.area) < 0) body++;", "") == RR.READ_JS


def test_read_page11_reads_past_the_cap_only_when_capped():
    p = Page()
    r = asyncio.run(RR.read_page11(p))
    assert p.reads == ["v", "v11"] and len(r["elements"]) == 761 and r["elements_read"].startswith("v11")
    q = Page(n_nav=40)
    r = asyncio.run(RR.read_page11(q))
    assert q.reads == ["v"] and len(r["elements"]) == 41 and "elements_read" not in r


def test_a_version_11_tool_reads_the_tile_behind_the_navigation():
    """``read_full`` / ``wait_results`` of a version-11 tool read past the cap; a version-10 tool's reads are as before
    (the result wait ends at its ceiling with no item)."""
    assert len(asyncio.run(T.read_full(Page(), cap11=True))["elements"]) == 761
    assert len(asyncio.run(T.read_full(Page()))["elements"]) == 700
    region = {"kind": "cluster", "template": BASE + "/{v}", "where": "path", "width": 2,
              "regex": r"^https?://(?:www\.)?boat\.test/[^/?#]+(?:/[^/?#]+){0,1}(?:\?[^#]*)?(?:#.*)?$"}
    for version, settled in ((10, False), (11, True)):
        tool = {"version": version, "workflow": "product_lookup", "region": region}
        got = asyncio.run(T.wait_results(Page(), tool, VALUE, 0.3))
        assert (got is not None) == settled, (version, got)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
