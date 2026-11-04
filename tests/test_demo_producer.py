"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): a PRODUCER / BRAND word may be absent from a result title - a run of 1-3 of the input's words at its start or
its end, fewer than half of its words, none of them a distinguishing word - when the title names EVERY other word of
the input (its distinguishing words too) and the opened product page confirms the absent words: its own name, its
heading or its JSON-LD brand names them (never the document title alone, which carries the shop's name). Otherwise the
usual failure.

The false failure (www.vinocentral.de): the input "Château de Béru Chablis Montserre Bio 2022"; the shop's tiles name
the wines without their producer ("Chablis Montserre Bio 2022", "Chablis Terroirs de Béru Bio 2022", ...): 4 of 6
words, "no result names the input (best share 0.67 < 0.75)". The product pages name the producer (a saved page:
"Château Le Puy Barthélemy 2019", "Weingut: Château Le Puy").

What must hold:

1. on vinocentral's list a version-9 tool picks "Chablis Montserre Bio 2022" and answers it when its page names
   "Château de Béru"; the same demonstration compiled as version 8 fails as measured;
2. a page that does not name the absent words is an identity failure, never an answer;
3. the absent run is the input's start or end only, never a distinguishing word (another vintage stays a failure).
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import Shop, compiled, product, run  # noqa: E402

V2 = "Château de Béru Chablis Montserre Bio 2022"
#: the saved list for ``V2`` (titles) and the products behind them
TILES = [("chateau-de-beru-chablis-montserre", "Chablis Montserre Bio 2022", "Château de Béru Chablis Montserre Bio 2022"),
         ("chateau-de-beru-chablis-terroirs-de-beru", "Chablis Terroirs de Béru Bio 2022",
          "Château de Béru Chablis Terroirs de Béru Bio 2022"),
         ("chateau-de-beru-chablis-clos-beru-monopole", "Chablis Clos Béru Monopole Bio 2019",
          "Château de Béru Chablis Clos Béru Monopole Bio 2019"),
         ("chateau-de-roquefort-gueule-de-loup", "Gueule de Loup Bio 2024", "Château de Roquefort Gueule de Loup Bio 2024")]


def vino_shop(page_names_producer=True, brand="Château de Béru"):
    cat = [product("chateau-le-puy-barthelemy", "Château Le Puy Barthélemy 2019", 149.0, brand="Château Le Puy")]
    for slug, tile, name in TILES:
        shown = name if page_names_producer else tile
        cat.append(product(slug, shown, 24.9, tile=tile, brand=brand if page_names_producer else ""))
    return Shop(cat)


def test_prior_producer_absent_from_the_tile_and_version_8_unchanged():
    shop = vino_shop()
    t9, t8 = compiled(shop, 0), compiled(shop, 0, version=8)
    r9, r8 = run(shop, t9, V2), run(shop, t8, V2)
    assert r9["status"] == "ok" and r9["output"]["name"] == V2, r9
    assert r9["picked"]["absent"] == ["chateau", "beru"], r9["picked"]
    assert r9["checks"]["identity9"]["absent"]["confirmed_by"] == "name", r9["checks"]["identity9"]
    assert r8["status"] == "failed" and "no result names the input" in r8["why"], r8   # the measured false failure


def test_a_page_that_does_not_confirm_the_absent_words_is_a_failure():
    shop = vino_shop(page_names_producer=False)
    t9 = compiled(shop, 0)
    r9 = run(shop, t9, V2)
    assert r9["status"] == "failed" and r9["why"].startswith("identity:") and not r9["accepted"], r9   # 67 % of the words
    # the document title alone (it carries the shop's name and may echo the search) never confirms
    page = {"url": "https://www.shop9.test/p/x", "title": "Château de Béru | Chablis Montserre Bio 2022", "h1": [],
            "facts": [], "text": "", "main_text": ""}
    ok, why, ev = C.check_pick9(page, {"name": "Chablis Montserre Bio 2022"}, V2, {"absent": ["chateau", "beru"]})
    assert not ok and ev["absent"]["confirmed_by"] == "" and "'chateau beru'" in why, (ok, why, ev)
    ok, why, ev = C.check_pick9(dict(page, facts=[{"type": "Product", "name": "Chablis Montserre Bio 2022",
                                                   "brand": "Château de Béru"}]),
                                {"name": "Chablis Montserre Bio 2022"}, V2, {"absent": ["chateau", "beru"]})
    assert ok and ev["absent"]["confirmed_by"] == "JSON-LD brand", (ok, why, ev)


def test_the_absent_run_is_the_start_or_end_and_never_a_distinguishing_word():
    runs = [r[0] for r in C.absent_runs(V2)]
    assert runs == [["chateau"], ["chateau", "beru"]], runs          # "2022" (a number) is never absent
    items = [{"title": t, "url": f"https://www.shop9.test/p/{s}", "ctx": ""} for s, t, _n in TILES]
    got = C.closest_choice9(items, "Château de Béru Chablis Montserre Bio 2021")
    assert got["el"] is None and not got.get("absent"), got            # another vintage: not the input's wine
    got = C.closest_choice9(items, "Chablis Château de Béru Montserre Bio 2022")
    assert got["el"] is None, got                                      # the producer in the middle: not a start / end run
    got = C.closest_choice9(items, V2)
    assert got["el"]["title"] == "Chablis Montserre Bio 2022" and got["absent"] == ["chateau", "beru"], got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
