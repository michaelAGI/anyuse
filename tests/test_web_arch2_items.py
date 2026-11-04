"""The NAMED item, not the first result; a section goal is a navigation; a
search the user asks for is made. Offline (the fake shop of test_web_arch.py,
fake planner and picker).

The failure: "first result" was opened instead of the item the task named
(highlandwoodworking), and a section goal was done as search + open an item
("Take me to Sharpening Tools" opened a product; fatquartershop's collection).

What must hold:

1. a position ("first result", "top result") is taken only when the USER asks
   for one; the planner's position words are dropped and the item the task
   names is mapped by its title (the target's own words, else the value just
   searched), then the picker; never the first result by default;
2. a goal that asks to GO TO a section (take me to / go to / a collection,
   category, section, department) is a navigation: search(V) + open_item(V)
   and a lone open_item become navigate_in_site; section words are not needed
   in the link text; questions and search goals are left alone;
3. when the user asks for a search, the search box is used even when the
   item is linked on the page shown (the search is the task).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, a, chooser_of, run  # noqa: E402


def test_positions_are_the_users_only():
    assert W.user_ordinal("Search for merino and open the first result") == 0
    assert W.user_ordinal("Öffne das zweite Ergebnis") == 1
    assert W.user_ordinal("Open the Merino Hiking Sock") is None
    assert W.user_ordinal("Show me the Top Coat") is None
    assert W.ordinal_in("Lakeland Dual 8.4L Air Fryer first result")
    assert W.ordinal_in("first result") and W.ordinal_in("top result") and W.ordinal_in("first")
    assert not W.ordinal_in("Top Coat Gloss Finish") and not W.ordinal_in("Merino Hiking Sock")
    assert W.item_words("Lakeland Dual 8.4L Air Fryer first result") == "lakeland dual 8 4l air fryer"
    assert W.item_words("the first result") == "the"
    assert W.content_tokens(W.item_words("first result")) == []


def _open(goal, plan, chooser=None):
    page = FakePage()
    rec = run(R.WebArch(FakePlanner(plan), chooser=chooser, shadow_images=False).run(
        page, {"id": "T", "utterance": goal, "kind": "open_item"}, BASE + "/"))
    return rec, page


def test_the_named_item_not_the_first_result():
    rec, page = _open("Open the Merino Hiking Sock",
                      [S("search", value="merino"), S("open_item", "Merino Hiking Sock first result")])
    assert page.gotos[-1].endswith("/products/merino-hiking-sock"), page.gotos
    step = next(s for s in rec["steps"] if s["step"]["effect"] == "open_item")
    assert step["position_dropped"]["words"] == "merino hiking sock"


def test_a_bare_position_maps_the_value_just_searched():
    rec, page = _open("Show me the Merino Hiking Sock",
                      [S("search", value="Merino Hiking Sock"), S("open_item", "first result")])
    assert page.gotos[-1].endswith("/products/merino-hiking-sock"), page.gotos


def test_no_named_match_goes_to_the_picker_never_the_first_result():
    ch = chooser_of([("none", .9), ("none", .9), ("none", .9), ("none", .9)])
    rec, page = _open("Show me the Merino Trail Sock",
                      [S("search", value="merino"), S("open_item", "top result")], chooser=ch)
    assert not any(g.endswith("/products/merino-crew-sock") for g in page.gotos), page.gotos
    assert rec["picks"] and ch.calls


def test_the_users_own_position_is_honoured():
    rec, page = _open("Search for merino and open the first result",
                      [S("search", value="merino"), S("open_item", "first result")])
    assert page.gotos[-1].endswith("/products/merino-crew-sock"), page.gotos


def test_a_section_goal_is_a_navigation():
    steps, ch = W.check_plan("Take me to Sharpening Tools",
                             [S("search", value="Sharpening Tools"), S("open_item", "Sharpening Tools")])
    assert [(s["effect"], s["target"]) for s in steps] == [("navigate_in_site", "Sharpening Tools")], steps
    steps, ch = W.check_plan("Show me the Baked with Love collection",
                             [S("search", value="Baked with Love"), S("open_item", "Baked with Love collection")])
    assert [s["effect"] for s in steps] == ["navigate_in_site"] and ch
    steps, _ = W.check_plan("Show me the Baked with Love collection", [S("open_item", "Baked with Love collection")])
    assert steps[0]["effect"] == "navigate_in_site"
    steps, _ = W.check_plan("Go to the socks section", [S("search", value="socks"), S("open_item", "first result")])
    assert [(s["effect"], s["target"]) for s in steps] == [("navigate_in_site", "socks")]
    # questions, searches and item goals are left alone
    for goal in ("Which category is the Wool Scarf in?", "Search the site for sharpening tools",
                 "Open the Merino Hiking Sock"):
        steps, _ = W.check_plan(goal, [S("search", value="x y"), S("open_item", "x y")])
        assert steps[0]["effect"] == "search", goal
    assert W.nav_words("Baked with Love collection") == "baked with love"
    assert W.nav_words("Collections") == "Collections"


def test_a_section_link_is_found_without_the_section_word():
    page = FakePage()
    rec = run(R.WebArch(FakePlanner([S("navigate_in_site", "Socks category")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Take me to the Socks category", "kind": "navigate_in_site"}, BASE + "/"))
    assert page.gotos[-1].endswith("/collections/socks"), page.gotos


def test_a_search_the_user_asks_for_is_made_even_when_the_item_is_linked():
    page = FakePage()
    run(R.WebArch(FakePlanner([S("search", value="Wool Beanie"), S("open_item", "Wool Beanie")]),
                  shadow_images=False).run(page, {"id": "T", "utterance": "Search for Wool Beanie"}, BASE + "/"))
    assert page.typed == ["Wool Beanie"]
    page2 = FakePage()
    run(R.WebArch(FakePlanner([S("search", value="Wool Beanie"), S("open_item", "Wool Beanie")]),
                  shadow_images=False).run(page2, {"id": "T", "utterance": "Open the Wool Beanie"}, BASE + "/"))
    assert page2.typed == [] and page2.gotos[-1].endswith("/products/wool-beanie")


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
