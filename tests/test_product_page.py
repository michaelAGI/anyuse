"""A category page is not a product page, offline (the fake shop of
test_web_arch.py; fake planner).

The failure: the arch answered from a category or series page instead of the
product page (fonq "Barkruk" collection, naturkompaniet's Kånken series): 6 runs.

What must hold:

1. ``product_detail``: one schema.org Product (JSON-LD), og:type product or one
   Product microdata item = a product page; an ItemList / several Products /
   several microdata items / many prices with many item links = a list; else
   undecided (never a guess);
2. before reading for a question about one item, and right after opening a
   "product", the engine checks the page; on a list it continues to the item
   (its words by the map, then the picker; the first item only when the user
   asks for ANY one item of a section), once per run, compiled as a goto;
3. a product page, an undecided page and a gated goal are left alone.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, a, product_ld, run  # noqa: E402

ITEMLIST = json.dumps({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
    {"@type": "ListItem", "position": 1, "url": BASE + "/products/merino-crew-sock"},
    {"@type": "ListItem", "position": 2, "url": BASE + "/products/merino-hiking-sock"}]})


def test_product_detail_verdicts():
    one = {"facts": W.product_facts([product_ld("Merino Crew Sock", "18.00")]), "jsonld": []}
    assert W.product_detail(one) == (True, "one schema.org Product")
    three = {"facts": W.product_facts([product_ld(n, "9.00") for n in ("A Sock", "B Sock", "C Sock")]), "jsonld": []}
    assert W.product_detail(three)[0] is False
    assert W.product_detail({"facts": [], "jsonld": [ITEMLIST]})[0] is False
    assert W.product_detail({"facts": [], "jsonld": [], "og_type": "product"})[0] is True
    assert W.product_detail({"facts": [], "jsonld": [], "md_products": 1})[0] is True
    assert W.product_detail({"facts": [], "jsonld": [], "md_products": 12})[0] is False
    listing = {"facts": [], "jsonld": [], "main_text": " ".join(f"Item {k} €{k}9.00" for k in range(8)),
               "elements": [a(k, f"Wool Thing {k}", f"/products/w{k}") for k in range(8)]}
    assert W.product_detail(listing)[0] is False
    single = {"facts": [], "jsonld": [], "main_text": "Wool Beanie\n€25.00\nArt.-Nr. 12345", "h1": ["Wool Beanie"],
              "elements": [{"i": 1, "tag": "button", "text": "Add to cart", "area": "main"}]}
    assert W.product_detail(single, "Wool Beanie")[0] is True
    assert W.product_detail({"facts": [], "jsonld": [], "main_text": "About us"})[0] is None


def test_any_item_goals():
    assert W.any_item_goal("Open één product uit Barkruk en vertel me het materiaal volgens die productpagina.")
    assert W.any_item_goal("Öffne einen Artikel aus Teleskopruten und sag mir die Marke von seiner Produktseite.")
    assert W.any_item_goal("Open a product from the Socks collection")
    assert W.any_item_goal("Otwórz jeden przepis na placki i powiedz mi, ile jajek trzeba")
    assert not W.any_item_goal("Open the Merino Hiking Sock")
    assert not W.any_item_goal("What does the Merino Crew Sock cost?")


class ListPage(FakePage):
    """The fake shop whose collection pages publish an ItemList (a category page)."""

    async def js(self, expr, read_only=False):
        r = await super().js(expr, read_only)
        if expr == R.READ_JS and isinstance(r, dict) and "/collections/" in self.cur:
            r["jsonld"] = [ITEMLIST]
            r["elements"] = list(r["elements"]) + [a(61, "Merino Hiking Sock", "/products/merino-hiking-sock")]
        return r


def test_a_question_read_on_a_category_page_continues_to_the_item():
    page = ListPage()
    plan = [S("navigate_in_site", "Socks"), S("read_content", "page")]
    rec = run(R.WebArch(FakePlanner(plan), shadow_images=False).run(
        page, {"id": "T", "utterance": "What does the Merino Crew Sock cost?", "kind": "answer_question"},
        BASE + "/"))
    assert page.gotos[-1] == BASE + "/products/merino-crew-sock", page.gotos
    hop = next(s for s in rec["steps"] if s.get("item_hop"))
    assert hop["product_page"]["verdict"] is False and hop["item_hop"]["ok"]
    assert any(o.get("op") == "goto" and o.get("href", "").endswith("merino-crew-sock") for o in rec["program"]["ops"])
    assert any("not an item page" in c for c in rec["changes"])


def test_an_opened_category_continues_to_the_first_item_only_when_the_user_asks_for_any_one():
    page = ListPage()
    plan = [S("navigate_in_site", "Socks"), S("read_content", "page")]
    run(R.WebArch(FakePlanner(plan), shadow_images=False).run(
        page, {"id": "T", "utterance": "Open a product from Socks and tell me its material",
               "kind": "answer_question"}, BASE + "/"))
    assert page.gotos[-1] == BASE + "/products/merino-crew-sock", page.gotos


def test_a_product_page_and_a_gated_goal_are_left_alone():
    page = ListPage()
    plan = [S("search", value="Merino Crew Sock"), S("open_item", "Merino Crew Sock"), S("read_content", "page")]
    rec = run(R.WebArch(FakePlanner(plan), shadow_images=False).run(
        page, {"id": "T", "utterance": "What does the Merino Crew Sock cost?", "kind": "answer_question"},
        BASE + "/"))
    assert page.gotos[-1] == BASE + "/products/merino-crew-sock"
    assert not any(s.get("item_hop") for s in rec["steps"])
    page = ListPage()
    run(R.WebArch(FakePlanner([S("navigate_in_site", "Socks")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Add the Merino Crew Sock to my cart", "kind": "gate"}, BASE + "/"))
    assert not any("/products/" in u for u in page.gotos)


def test_a_position_stays_when_the_user_asks_for_any_one_item():
    page = ListPage()
    plan = [S("search", value="merino"), S("open_item", "first result"), S("read_content", "page")]
    rec = run(R.WebArch(FakePlanner(plan), shadow_images=False).run(
        page, {"id": "T", "utterance": "Open a product from the merino search and tell me its price",
               "kind": "answer_question"}, BASE + "/"))
    step = next(s for s in rec["steps"] if s["step"]["effect"] == "open_item")
    assert "position_dropped" not in step and step["result"]["ok"], step


if __name__ == "__main__":
    import inspect
    import traceback
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn()
            p += 1
            print("OK  ", n)
        except Exception as e:                            # noqa: BLE001
            f += 1
            print("FAIL", n, "::", repr(e)[:400])
            traceback.print_exc(limit=6)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
