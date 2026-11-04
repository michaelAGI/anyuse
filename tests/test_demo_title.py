"""Web tool version 11: a title link whose VISIBLE text is no title.

The cases (saved snapshots): www.aquaristikshop.com's tiles link the product four times - the name
("Juwel Eccoflow 600"), the rating, and a quick-view link whose visible text is "Schnellansicht" and whose aria-label
repeats the name; a version-10 tool compiled the quick-view link as the title link and read every title as
"Schnellansicht" ("query: no result names the input", twice); www.motoabbigliamento.it the same with
"Dettaglio"; www.raumfahrer.net's cards read a date badge "Sep. 20 2026" as two titles (the reader: wrong).

What must hold (fakes shaped on those snapshots):

1. ``no_title11``: a call to action, a date badge, one word are no title; a product name is;
2. a title-kind card (the quick-view link itself) takes the links to its URL next to it - its tile;
3. the runtime (``read_region_v6``, cards_version 11): a title link showing no title takes the card's other link to
   the result; cards_version 8 / 10 read exactly as before.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402

BASE = "https://www.aqua.test"
NAMES = ["Juwel Eccoflow 600", "Juwel Pumpenrad Eccoflow", "Juwel Bioflow Filter M"]


def test_no_title11():
    for t in ("Schnellansicht", "Dettaglio", "Sep. 20 2026", "20. September 2026", "2026-09-20", "Details", "quick view",
              ""):
        assert C.no_title11(t), t
    # a lone word that is no call to action is a title (www.e-hausaufgaben.de's "Biographie")
    for t in ("Juwel Eccoflow 600", "20. 9. 2026 – Starship soll Ende September 2026 zum 14. Testflug starten",
              "Pantaloni moto Dainese Trackpants Nero", "Biographie", "Hero", "Mai Tai Cocktail 2026"):
        assert not C.no_title11(t), t


def test_a_quick_view_title_link_takes_its_tile():
    """A TITLE-kind card (the card is the quick-view link itself; www.aquaristikshop.com): the links to
    the same URL next to it in the reader's order - its tile - name the result; the next tile's links never do."""
    els, cmap_map, rows = [], {}, []
    for k, n in enumerate(NAMES):
        u = f"{BASE}/aquaristik/{n.replace(' ', '-')}/{432150 + k}/"
        b = len(els)
        els += [{"i": b, "tag": "a", "href": u, "text": n}, {"i": b + 1, "tag": "a", "href": u, "text": "★ 5.0 (3)"},
                {"i": b + 2, "tag": "a", "href": u, "text": f"Schnellansicht: {n}"},
                {"i": b + 3, "tag": "button", "text": "In den Warenkorb"}]
        cmap_map[str(b + 2)] = [0, k, 1, 0, 1, "Schnellansicht"]
        rows.append([k, 0, b + 2, 1, "Schnellansicht", "main", 1, ""])
    spec = {"kind": "title", "chain": ["div", "div.card__content", "div.card__media", "a.card__quickview"], "rels": [""],
            "roles": [{"anchor": "a.card__quickview", "heading": False}]}
    region = {"cards": [spec], "cards_version": 11, "template": BASE + "/aquaristik/{v}/"}
    items = K.read_region_v6(els, {"map": cmap_map, "cards": rows}, region, "www.aqua.test")["items"]
    assert [i["title"] for i in items] == NAMES, items
    assert [i["title"] for i in K.read_region_v6(els, {"map": cmap_map, "cards": rows}, dict(region, cards_version=8),
                                                 "www.aqua.test")["items"]] == ["Schnellansicht"] * 3
    assert K.tile_texts11(els, els[2]) == ["★ 5.0 (3)", NAMES[0]]


def _region_read(version, vis_title):
    u = "https://www.r.test/20-9-2026-starship/"
    els = [{"i": 0, "tag": "a", "href": u, "text": vis_title},
           {"i": 1, "tag": "a", "href": u, "text": "20. 9. 2026 – Starship soll Ende September 2026 zum 14. Testflug starten"},
           {"i": 2, "tag": "a", "href": "https://www.r.test/miniupdate/", "text": "Miniupdate zu Starship Testflug 13"}]
    spec = {"kind": "card", "chain": ["div", "main", "div.row", "article.post"], "rels": ["a"], "roles": [{"anchor": "a"}]}
    cmap = {"map": {"0": [0, 0, 1, 0, 1, vis_title],
                    "1": [0, 0, 0, 1, 0, els[1]["text"]],
                    "2": [0, 1, 1, 0, 1, els[2]["text"]]},
            "cards": [[0, 0, 0, 2, "", "main", 1, ""], [1, 0, 2, 1, "", "main", 1, ""]]}
    region = {"cards": [spec], "cards_version": version, "template": "https://www.r.test/{v}/"}
    return K.read_region_v6(els, cmap, region, "www.r.test")["items"]


def test_runtime_title_fallback_is_version_11_only():
    it11 = _region_read(11, "Sep. 20 2026")
    assert [i["title"] for i in it11] == ["20. 9. 2026 – Starship soll Ende September 2026 zum 14. Testflug starten",
                                          "Miniupdate zu Starship Testflug 13"]
    for v in (8, 10):
        assert _region_read(v, "Sep. 20 2026")[0]["title"] == "Sep. 20 2026"
    # a title link that shows a title keeps it
    assert _region_read(11, "Starship Flug 14 am 28. September")[0]["title"] == "Starship Flug 14 am 28. September"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
