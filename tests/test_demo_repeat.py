"""Version 7: a value that repeats a result tile's TITLE with its SUBTITLE. Built from saved evidence of
teufel.de: the frozen value "Teufel MOVE 2 Schwarz Teufel MOVE 2 HiFi‑Klang per USB-C-Kabel" (a weak value: a tile
title repeated with its subtitle); the tool opened the right tile, whose product page names "Teufel MOVE 2"
(JSON-LD, the title "Teufel MOVE 2 | Teufel", no heading), and version 6's identity check failed at 55 % (6 of the
value's 11 content words, its repeated phrase counted twice). Offline (the fake shop of test_demo_compile.py; no
browser, no network, no model).

What must hold:

1. the repeat is found generically - the value repeats its own leading phrase of >= 2 words (>= 2 content words):
   the tile's title, the phrase, the subtitle and the deduped value; a value without such a repeat has none;
2. the identity is ALSO scored on the tile's title (the better share counts, recorded as ``scored_on``): the
   saved product page passes at 75 % ("Teufel MOVE 2 Schwarz": 3 of 4), a whole version-7 run answers it, a
   version-6 run keeps the measured 55 % failure;
3. the title must still be named: a page naming less of it (another model, the series only) fails; a value with
   no repeat is scored exactly as before; the landing check scores the same way.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

VALUE = "Teufel MOVE 2 Schwarz Teufel MOVE 2 HiFi‑Klang per USB-C-Kabel"
SLUG = "teufel-move-2-107003280"
#: the saved product page (the parts the checks read; the text abridged)
PAGE = {"url": "https://teufel.de/teufel-move-2-107003280", "title": "Teufel MOVE 2 | Teufel", "h1": [],
        "canonical": "https://teufel.de/teufel-move-2-107003280",
        "text": "KOPFHÖRER\nIN-EAR\nNEU\n4,2 (21)\nTeufel MOVE 2\n\nDirekt mit Kabel, ehrlich im Sound.\n29,99 €\n"
                "In den Warenkorb",
        "facts": [{"type": "Product", "name": "Teufel MOVE 2", "brand": "Teufel", "sku": "107003281", "color": "Schwarz",
                   "url": "https://teufel.de/teufel-move-2-107003280?v=107003281",
                   "offers": [{"price": 29.99, "currency": "EUR", "availability": "InStock"}], "price": 29.99,
                   "prices": [29.99], "currency": "EUR", "availability": "InStock"}],
        "meta": {"og:title": "Teufel MOVE 2 | Teufel", "og:type": "website"}, "microdata": {}}
FIELDS = {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"}


def test_the_repeat_is_found_generically():
    rep = C.repeated_title(VALUE)
    assert rep == {"title": "Teufel MOVE 2 Schwarz", "phrase": "Teufel MOVE 2",
                   "subtitle": "Teufel MOVE 2 HiFi‑Klang per USB-C-Kabel",
                   "deduped": "Teufel MOVE 2 Schwarz HiFi‑Klang per USB-C-Kabel"}, rep
    assert C.identity_values(VALUE) == ["Teufel MOVE 2 Schwarz"]
    # case and accents folded; the longest repeated phrase
    assert C.repeated_title("ACME Kettle Pro Steel Acme kettle pro 1,7 l")["phrase"] == "ACME Kettle Pro"
    for v in ("Canon PGI-550 PGBK", "MELON en MÉLANGE de 8 VARIÉTÉS", "the best of the best", "Set Set Box",
              "Vox VT20X Classic Blue", "Teufel MOVE 2 Teufel", ""):
        assert C.repeated_title(v) == {} and C.identity_values(v) == [], v


def test_prior_page_passes_on_the_tiles_title_and_version_6_is_unchanged():
    f = C.extract_fields(PAGE, FIELDS, VALUE)
    assert f["name"] == "Teufel MOVE 2" and f["price"] == 29.99 and f["availability"] == "InStock", f
    ok, why, checks = C.check_product(PAGE, f, VALUE, "teufel.de")
    assert not ok and why == "identity: the product page names 55% of the input's words", why   # as measured
    ok, why, checks = C.check_product(PAGE, f, VALUE, "teufel.de", C.identity_values(VALUE))
    assert ok, why
    assert checks["identity"] == {"share": 0.75, "name": "Teufel MOVE 2", "scored_on": "Teufel MOVE 2 Schwarz",
                                  "raw_share": 0.55}, checks["identity"]
    assert C.field_conflicts(PAGE, f, FIELDS, VALUE) == {}


def _spec_with_tile(orig):
    """The fake shop's pages, the result tile of the MOVE 2 showing its title AND its subtitle (the saved tile)."""
    def spec(url, recommend=True):
        s = orig(url, recommend)
        s["links"] = [(VALUE if h.endswith(SLUG) and area == "main" else t, h, area, ctx) for t, h, area, ctx in s["links"]]
        return s
    return spec


def test_a_whole_run_answers_it_at_version_7_only():
    t = TC.tool()
    old_cat, old_spec = list(TC.CATALOG), TC.spec_for
    TC.CATALOG[:] = old_cat + [(SLUG, "Teufel MOVE 2", 29.99, "InStock")]
    TC.spec_for = _spec_with_tile(old_spec)
    try:
        out = TC.run_tool(TC.FakeShopPage(), t, VALUE)
        assert out["status"] == "ok" and out["accepted"] and out["picked"]["url"].endswith(SLUG), out
        assert out["output"]["name"] == "Teufel MOVE 2" and out["output"]["price"] == 29.99, out["output"]
        assert out["checks"]["product"]["identity"]["scored_on"] == "Teufel MOVE 2 Schwarz", out["checks"]["product"]
        out6 = TC.run_tool(TC.FakeShopPage(), dict(t, version=6), VALUE)
        assert out6["status"] == "failed" and out6["why"] == "identity: the product page names 55% of the input's words", \
            out6
        assert out6["picked"]["url"].endswith(SLUG), out6
    finally:
        TC.CATALOG[:] = old_cat
        TC.spec_for = old_spec


def test_the_title_must_still_be_named():
    f = C.extract_fields(PAGE, FIELDS, VALUE)
    for name in ("Teufel MOVE", "Teufel MOVE 3 Weiß"):
        page = dict(PAGE, title=f"{name} | Teufel", facts=[dict(PAGE["facts"][0], name=name)])
        g = dict(f, name=name)
        ok, why, checks = C.check_product(page, g, VALUE, "teufel.de", C.identity_values(VALUE))
        assert not ok and why.startswith("identity:") and checks["identity"]["share"] < 0.75, (name, why, checks)
    # no repeat: exactly the version-6 scoring
    p2 = TC.read_of(TC.spec_for(f"{TC.BASE}/p/borealis-toaster-2"))
    f2 = C.extract_fields(p2, FIELDS, "Borealis Toaster 2 Slice")
    assert C.check_product(p2, f2, "Borealis Toaster 2 Slice", "www.shop.test", C.identity_values("Borealis Toaster 2 Slice")) \
        == C.check_product(p2, f2, "Borealis Toaster 2 Slice", "www.shop.test")
    # the landing check scores the product's own name the same way
    ok, why, checks = C.check_landing(PAGE, f, VALUE, "teufel.de", C.identity_values(VALUE))
    assert ok and checks["landing"]["share"] == 0.75, (why, checks)
    assert not C.check_landing(PAGE, f, VALUE, "teufel.de")[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
