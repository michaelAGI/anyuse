"""Version 7: among the results naming every word of the input, the CLOSEST one - or "ambiguous" - never simply the
first. Built from saved evidence of www.druckerzubehoer.de: the site's six result titles for "Canon PGI-550 PGBK", in
its order; version 6 took the FIRST naming every word, a third-party "alternative Patrone", while the original was
listed 5th (the measured wrong output). The prices of the fake pages are invented. Offline (the fake shop of
test_demo_compile.py; no browser, no network, no model).

What must hold:

1. the six saved titles: a version-7 tool takes the original ("Druckerpatrone Canon PGI-550 PGBK schwarz 15 ml |
   300 Seiten (6496B001)") and a whole run answers its product page; a version-6 tool still takes the first (the
   measured output - its code path is unchanged);
2. the criteria, in order: a substitute marker ("alternative", "kompatibel", "fur", "for", "passend",
   "replacement", ...) the input carries none of; the input as a contiguous phrase; fewer extra words (a price is
   not a word of the title); the input's first word (the brand) first; a marker in the input disables the rule;
3. a tie of the closest -> INCOMPLETE with the tied results listed (the judge and a whole run: not accepted, no
   output), never a guess; the same URL twice is one result, not a tie;
4. one result naming every word, or none (the unique-best-share rule), selects exactly as version 6 does.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

VALUE = "Canon PGI-550 PGBK"
#: the saved result list for VALUE (the saved titles, the site's order; the slugs end as the saved URLs do)
DZ = [("canon-pgi-550-pgbk-alternative-patrone-dz104934",
       "Canon PGI-550 PGBK - alternative Patrone 'schwarz' 20,4 ml | 620 Seiten - Digital Revolution", 5.95, "InStock"),
      ("canon-pgi-550-cli-551-alternatives-xl-patronen-multipack-dz105950",
       "Canon PGI-550 / CLI-551 - alternatives XL Patronen Multipack KCMY+GY 1x 20,4 ml | 620 Seiten (PGBK), 3x 12,4 ml "
       "(CMY) | 3.560 Seiten, 1x 3.350 Seiten (GY) - Dig", 21.95, "InStock"),
      ("digital-revolution-reinigungspatrone-canon-pgi-550pgbk-pgbkxl-schwarz-pgi550pgbk-drr",
       "Digital Revolution Reinigungspatrone für Canon PGI-550PGBK PGBKXL schwarz", 7.95, "InStock"),
      ("druckerpatrone-canon-pgi-550-pgbkxl-schwarz-22-ml-500-seiten-6431b001-ijc338",
       "Druckerpatrone Canon PGI-550 PGBKXL schwarz 22 ml | 500 Seiten (6431B001)", 24.90, "InStock"),
      ("druckerpatrone-canon-pgi-550-pgbk-schwarz-15-ml-300-seiten-6496b001-ijc337",
       "Druckerpatrone Canon PGI-550 PGBK schwarz 15 ml | 300 Seiten (6496B001)", 17.90, "InStock"),
      ("druckerpatronen-doppelpack-canon-pgi-550-pgbkxl-schwarz-2x-22-ml-6431b010-dz109258",
       "Druckerpatronen Doppelpack Canon 'PGI-550 PGBKXL' schwarz 2x 22 ml (6431B010)", 46.90, "InStock")]
ORIGINAL = DZ[4][0]


def items(*titles, ctx=""):
    return [{"title": t, "url": f"{TC.BASE}/p/{n}", "ctx": ctx} for n, t in enumerate(titles)]


class Catalog:
    """The fake shop's catalogue extended for one test (restored after)."""

    def __init__(self, extra):
        self.extra = extra

    def __enter__(self):
        self.old = list(TC.CATALOG)
        TC.CATALOG[:] = self.old + list(self.extra)

    def __exit__(self, *a):
        TC.CATALOG[:] = self.old


def test_prior_six_titles_the_original_and_version_6_unchanged():
    t = TC.tool()
    assert t["version"] == C.TOOL_VERSION >= 7 and C.v7(t) and C.v6(t)
    assert not C.v7(dict(t, version=6)) and not C.v7(dict(t, version=5)) and not C.v7({})
    saved = [{"title": d[1], "url": f"https://www.druckerzubehoer.de/p/{d[0]}", "ctx": ""} for d in DZ]
    el, why, tied = C.closest_match(saved, VALUE)
    assert el["url"].endswith(ORIGINAL) and not tied, (el, why)
    assert "the input as a phrase" in why and "no substitute marker" in why, why
    el6, why6 = C.best_match(saved, VALUE)
    assert el6["url"].endswith(DZ[0][0]) and "the first" in why6, (el6, why6)       # the measured wrong output
    with Catalog(DZ):
        r = TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Canon+PGI-550+PGBK"))
        j = C.judge_result_page(t, r, VALUE)
        assert j["status"] == "pick" and j["pick"]["url"].endswith(ORIGINAL), j
        j6 = C.judge_result_page(dict(t, version=6), r, VALUE)
        assert j6["status"] == "pick" and j6["pick"]["url"].endswith(DZ[0][0]), j6
        out = TC.run_tool(TC.FakeShopPage(), t, VALUE)
        assert out["status"] == "ok" and out["accepted"] and out["picked"]["url"].endswith(ORIGINAL), out
        assert out["output"]["name"] == DZ[4][1] and out["output"]["price"] == 17.90, out["output"]
        out6 = TC.run_tool(TC.FakeShopPage(), dict(t, version=6), VALUE)
        assert out6["status"] == "ok" and out6["output"]["name"] == DZ[0][1], out6


def test_the_criteria_in_their_order():
    # (1) a substitute marker the input does not carry loses, even with fewer extra words
    el, _w, _t = C.closest_match(items("Acme Kettle 2000 kompatibel", "Acme Kettle 2000 Steel Original 1.7 l"),
                                 "Acme Kettle 2000")
    assert el["title"] == "Acme Kettle 2000 Steel Original 1.7 l", el
    for m in ("Ersatzdeckel for", "passend für", "compatible with", "replacement for", "alternative zu"):
        el, _w, _t = C.closest_match(items(f"{m} Acme Kettle 2000", "Acme Kettle 2000 Steel Edition"), "Acme Kettle 2000")
        assert el["title"] == "Acme Kettle 2000 Steel Edition", (m, el)
    # ... unless the input carries one itself: then the closest phrase wins
    el, _w, _t = C.closest_match(items("Deckel für Acme Kettle 2000 Glas Edition", "Deckel für Acme Kettle 2000"),
                                 "Deckel für Acme Kettle 2000")
    assert el["title"] == "Deckel für Acme Kettle 2000", el
    # (2) the input as a contiguous phrase beats the same words apart
    el, _w, _t = C.closest_match(items("Acme Steel Kettle 2000", "Acme Kettle 2000 Steel"), "Acme Kettle 2000")
    assert el["title"] == "Acme Kettle 2000 Steel", el
    # (3) fewer extra words; a price is not a word of the title
    el, _w, _t = C.closest_match(items("Acme Kettle 2000 Steel Black Edition", "Acme Kettle 2000 Steel 49,99 €"),
                                 "Acme Kettle 2000")
    assert el["title"] == "Acme Kettle 2000 Steel 49,99 €", el
    assert C.closeness("Acme Kettle 2000 Steel 1.299,00 €", "Acme Kettle 2000") == (0, 0, 1, 0)
    # (4) the brand first
    el, _w, _t = C.closest_match(items("Steel Acme Kettle 2000", "Acme Kettle 2000 Steel"), "Acme Kettle 2000")
    assert el["title"] == "Acme Kettle 2000 Steel", el
    # a compound / plural of an input word is not an extra word, but not the phrase either
    assert C.closeness("Druckerpatrone Canon PGI-550 PGBKXL schwarz", VALUE) == (0, 1, 2, 1)


def test_a_tie_is_ambiguous_never_a_guess():
    el, why, tied = C.closest_match(items("Acme Kettle 2000 Steel", "Acme Kettle 2000 Black"), "Acme Kettle 2000")
    assert el is None and len(tied) == 2 and why.startswith("ambiguous: 2 of the 2"), (el, why)
    # the same URL twice is ONE result (a card repeated), not a tie
    two = items("Acme Kettle 2000 Steel", "Acme Kettle 2000 Black")
    two[1]["url"] = two[0]["url"]
    el, why, tied = C.closest_match(two, "Acme Kettle 2000")
    assert el is two[0] and not tied, why
    extra = [("acme-kettle-2000-black", "Acme Kettle 2000 Black", 49.99, "InStock"),
             ("acme-kettle-2000-white", "Acme Kettle 2000 White", 49.99, "InStock")]
    t = TC.tool()
    with Catalog(extra):
        r = TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Acme+Kettle+2000"))
        j = C.judge_result_page(t, r, "Acme Kettle 2000")
        assert j["status"] == "incomplete" and j["why"].startswith("incomplete: ambiguous: 3 of the 3"), j
        assert sorted(a["url"].rsplit("/", 1)[1] for a in j["ambiguous"]) == \
            ["acme-kettle-2000", "acme-kettle-2000-black", "acme-kettle-2000-white"], j["ambiguous"]
        out = TC.run_tool(TC.FakeShopPage(), t, "Acme Kettle 2000")
        assert out["status"] == "incomplete" and not out["accepted"] and out["output"] is None, out
        assert len(out["ambiguous"]) == 3 and "picked" not in out, out
        # version 6 took the first (unchanged)
        out6 = TC.run_tool(TC.FakeShopPage(), dict(t, version=6), "Acme Kettle 2000")
        assert out6["status"] == "ok" and out6["picked"]["url"].endswith("/p/acme-kettle-2000"), out6


def test_one_or_no_full_match_selects_as_version_6():
    one = items("Acme Kettle 2000 Steel", "Acme Kettle 3000 Glass")
    assert C.closest_match(one, "Acme Kettle 2000 Steel")[0] is C.best_match(one, "Acme Kettle 2000 Steel")[0] is one[0]
    part = items("Acme Kettle Steel", "Borealis Toaster")
    el, why, tied = C.closest_match(part, "Acme Kettle 2000 Steel")
    assert el is part[0] and not tied and (el, why) == C.best_match(part, "Acme Kettle 2000 Steel"), why
    none = items("Borealis Toaster", "Cumulus Blender")
    assert C.closest_match(none, "Acme Kettle")[0] is None and C.closest_match(none, "Acme Kettle")[2] == []
    # by card text when no title names every word (the same order as version 6)
    cards = items("Kettle 2000 Steel", "Kettle 2000")
    cards[0]["ctx"], cards[1]["ctx"] = "Acme Kettle 2000 Steel 59,99 €", "Acme Kettle 2000 49,99 €"
    el, why, _t = C.closest_match(cards, "Acme Kettle 2000")
    assert el is cards[1] and "card" in why and C.best_match(cards, "Acme Kettle 2000")[0] is cards[0], why
    assert C.closest_match([], "of the")[1] == "the input has no content word"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
