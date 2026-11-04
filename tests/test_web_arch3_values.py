"""ITEM-level proof values, offline (a fake start page).

The earlier values were navigation category names; the proof's contract asks that a
result item names the value, which item titles rarely do for a category word.

What must hold: the values are words of the site's own item titles (JSON-LD product
names, item clusters, content links), not site chrome, not generic words (colours,
sizes, marketing), not the site's own name; words shared by several titles come
first; the draw is seeded per site, before any arm, and never overlaps the search
value.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "web_runtime"))


HOME = """<html><head><title>Woolly</title>
<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [
 {"@type": "Product", "name": "Merino Crew Sock Grey"}, {"@type": "Product", "name": "Merino Hiking Sock"}]}</script>
</head><body><nav><a href="/collections/socks">Socks</a><a href="/account">My account</a></nav>
<main><ul>
<li><a href="/products/alpaca-scarf">Alpaca Scarf with Fringe</a></li>
<li><a href="/products/merino-beanie">Merino Beanie Hat</a></li>
<li><a href="/products/linen-apron">Linen Kitchen Apron</a></li>
<li><a href="/products/woolly-gift">Woolly Gift Card Voucher</a></li>
</ul></main></body></html>"""


def test_item_terms_are_words_of_item_titles():
    import web_arch3_values as W3
    titles = W3.item_titles(HOME, "https://www.woolly.example/")
    assert "Merino Hiking Sock" in titles and "Alpaca Scarf with Fringe" in titles
    assert not any(t in ("Socks", "My account") for t in titles)
    terms = W3.item_terms(HOME, "https://www.woolly.example/")
    assert terms[0] == "Merino", terms                        # shared by several titles first
    low = [t.lower() for t in terms]
    assert "sock" in low and "alpaca" in low and "scarf" in low
    for bad in ("grey", "woolly", "with", "gift", "card", "voucher", "account"):
        assert bad not in low, (bad, terms)
    v1 = W3.draw_proof_values("www.woolly.example", terms, "merino socks")
    assert v1 == W3.draw_proof_values("www.woolly.example", terms, "merino socks") and len(v1) == 4
    assert not any(v.lower() == "merino" for v in v1)



if __name__ == "__main__":
    import traceback
    try:
        test_item_terms_are_words_of_item_titles()
        print("OK   test_item_terms_are_words_of_item_titles\n\n1 passed, 0 failed")
    except Exception as e:                                # noqa: BLE001
        traceback.print_exc(limit=6)
        print("FAIL", repr(e)[:400])
        sys.exit(1)
