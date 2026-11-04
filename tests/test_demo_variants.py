"""Version 7 ("first + list variants"): a tie for the closest match
among results whose NORMALIZED titles are identical is answered with the FIRST, the others listed with it as
``variants_same_title`` (count; each one's title, the price the result list shows, URL) - never opened, no extra
request. A tie among DIFFERENT titles stays INCOMPLETE (ambiguous). Built from saved evidence
(www.aponeo.de, both values: one title listed 5 and 3 times, pack sizes; www.omoda.nl: "Nieuw" / "-30%" /
"-50%" badges before one title). Offline (the fake shop of test_demo_compile.py;
no browser, no network, no model).

What must hold:

1. aponeo-like pack sizes: a version-7 run answers the first, lists all three with their shown prices and URLs,
   and opens nothing but the first (the page's requests are the search and the answer only); version 6 answers the
   first with no list (unchanged);
2. the normalization: badges, discount and price tags around one title are one title (omoda's saved titles);
3. a tie among different titles (or one title and another) stays INCOMPLETE with the tied results listed;
4. the saved lists: aponeo's two and omoda's are answered with the first again (the version-6 pick), the others
   listed; a unique closest match carries no list.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_demo_compile as TC  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

CEROLA = "Cerola Vitamin C Taler Grandel"
PACKS = [("03106667-cerola-vitamin-c-taler-grand", CEROLA, 38.99, "InStock"),
         ("08032398-cerola-vitamin-c-taler-grandel", CEROLA, 21.49, "InStock"),
         ("03106472-cerola-vitamin-c-taler-grand", CEROLA, 12.99, "InStock"),
         ("02752891-cerola-c-plus-zink-taler-gra", "Cerola C plus Zink Taler Grandel", 9.99, "InStock")]
#: the saved www.omoda.nl titles (the first 12 of the list, the site's order)
OMODA = ["Nieuw Mexx Lage sneakers"] * 6 + ["-30% Mexx Lage sneakers", "-50% Mexx Lage sneakers",
                                           "-30% Mexx Lage sneakers", "Laatste maten -50% Mexx Lage sneakers",
                                           "-50% Mexx Lage sneakers", "Laatste maten -50% Mexx Lage sneakers"]


class Catalog:
    def __init__(self, extra):
        self.extra = extra

    def __enter__(self):
        self.old = list(TC.CATALOG)
        TC.CATALOG[:] = self.old + list(self.extra)

    def __exit__(self, *a):
        TC.CATALOG[:] = self.old


def items(titles, ctx=None):
    return [{"title": t, "url": f"{TC.BASE}/p/{n}", "ctx": (ctx or {}).get(n, "")} for n, t in enumerate(titles)]


def test_pack_sizes_answer_the_first_and_list_the_others_unopened():
    t = TC.tool()
    with Catalog(PACKS):
        page = TC.FakeShopPage()
        out = TC.run_tool(page, t, CEROLA)
        assert out["status"] == "ok" and out["accepted"] and out["picked"]["url"].endswith(PACKS[0][0]), out
        assert out["output"]["name"] == CEROLA and out["output"]["price"] == 38.99, out["output"]
        v = out["output"]["variants_same_title"]
        assert v == out["variants_same_title"] and v["count"] == 3 and v["title"] == "cerola vitamin c taler grandel", v
        assert [x["url"].rsplit("/", 1)[1] for x in v["variants"]] == [p[0] for p in PACKS[:3]], v
        assert [x["price"] for x in v["variants"]] == ["38,99 €", "21,49 €", "12,99 €"], v
        assert [x["price_value"] for x in v["variants"]] == [38.99, 21.49, 12.99], v
        assert [x["answered"] for x in v["variants"]] == [True, False, False] and all(x["title"] == CEROLA for x in v["variants"])
        # nothing but the search and the answer was requested: no variant page opened
        assert page.gotos == [f"{TC.BASE}/search?q=Cerola+Vitamin+C+Taler+Grandel", f"{TC.BASE}/p/{PACKS[0][0]}"], page.gotos
        assert "the first of 3 results with the same title" in out["checks"]["result_page"]["why"], out["checks"]
        # version 6: the first, no list (unchanged)
        out6 = TC.run_tool(TC.FakeShopPage(), dict(t, version=6), CEROLA)
        assert out6["status"] == "ok" and out6["picked"]["url"].endswith(PACKS[0][0]), out6
        assert "variants_same_title" not in out6 and "variants_same_title" not in out6["output"], out6


def test_badges_and_price_tags_around_one_title_are_one_title():
    keys = {C.title_key(x) for x in OMODA[:9] if not x.startswith("Laatste")}
    assert keys == {"mexx lage sneakers"}, keys
    assert C.title_key("Acme Kettle 2000 Steel 49,99 €") == C.title_key("NEW Acme Kettle 2000 Steel") == "acme kettle 2000 steel"
    assert C.title_key("Acme Kettle 2000 Steel") != C.title_key("Acme Kettle 2000 Black")
    assert C.shown_price({"title": "Acme Kettle", "ctx": "Acme Kettle UVP 59,99 € 49,99 €"}) == ("59,99 €", 59.99)
    assert C.shown_price({"title": "Acme Kettle 2000", "ctx": ""}) == ("", None)


def test_a_tie_of_different_titles_stays_ambiguous():
    t = TC.tool()
    extra = [("acme-taler-orange", "Acme Taler Orange", 4.99, "InStock"),
             ("acme-taler-zitrone", "Acme Taler Zitrone", 4.99, "InStock")]
    with Catalog(extra):
        r = TC.read_of(TC.spec_for(f"{TC.BASE}/search?q=Acme+Taler"))
        j = C.judge_result_page(t, r, "Acme Taler")
        assert j["status"] == "incomplete" and j["why"].startswith("incomplete: ambiguous: 2 of the 2"), j
        assert "variants_same_title" not in j, j
        out = TC.run_tool(TC.FakeShopPage(), t, "Acme Taler")
        assert out["status"] == "incomplete" and not out["accepted"] and out["output"] is None, out
    # one title twice and another once, all equally close: different titles -> ambiguous
    got = C.closest_choice(items(["Acme Taler Orange", "Acme Taler Orange", "Acme Taler Zitrone"]), "Acme Taler")
    assert got["el"] is None and len(got["tied"]) == 3 and got["variants"] is None, got
    # the same title twice, a closer one present: the closer one, no list
    got = C.closest_choice(items(["Acme Taler Orange Bio", "Acme Taler Orange Bio", "Acme Taler"]), "Acme Taler")
    assert got["el"]["title"] == "Acme Taler" and got["variants"] is None and not got["tied"], got


def test_the_saved_lists_are_answered_again():
    aponeo = items([CEROLA] * 5 + ["Cerola C plus Zink Taler Grandel"] * 3)
    got = C.closest_choice(aponeo, CEROLA)
    assert got["el"] is aponeo[0] and got["variants"]["count"] == 5 and not got["tied"], got
    assert got["el"] is C.best_match(aponeo, CEROLA)[0]                     # the audited correct pick
    b = items(["Vitamin B-Komplex ratiopharm Kapseln"] * 3 + ["Nicapur Vitamin B-Komplex aktiviert Kapseln"])
    got = C.closest_choice(b, "Vitamin B-Komplex ratiopharm Kapseln")
    assert got["el"] is b[0] and got["variants"]["count"] == 3, got
    om = items(OMODA)
    got = C.closest_choice(om, "Mexx Lage sneakers")
    assert got["el"] is om[0] and got["el"] is C.best_match(om, "Mexx Lage sneakers")[0], got
    assert got["variants"]["count"] == 10 and got["variants"]["title"] == "mexx lage sneakers", got["variants"]
    assert not any("Laatste" in x["title"] for x in got["variants"]["variants"])    # not tied: 3 extra words
    # a unique closest match carries no list
    got = C.closest_choice(items(["Acme Kettle 2000 Steel", "Acme Kettle 2000 Steel Black Edition"]), "Acme Kettle 2000")
    assert got["el"]["title"] == "Acme Kettle 2000 Steel" and got["variants"] is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
