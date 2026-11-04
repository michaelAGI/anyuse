"""The repeated record each node of a page sits in (``result_contract.record_units``), read from the page's own
structure: a card's title link and the ad, variant or shop link inside the same card share one record; the cards of
one grid row do not. Pure: parsed inline pages, no network, no model.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

from anyuse.execution import html_extract as H  # noqa: E402
from anyuse.execution import result_contract as RC  # noqa: E402
from test_verifier_units import AD, fish, guide_page  # noqa: E402


def links(markup, hrefs):
    root = H.parse(markup)
    return [next(n for n in root.walk() if n.tag == "a" and n.href == h) for h in hrefs]


def cards(variants_in=1):
    return "".join(
        f'<li class="card"><h3><a href="/p/{k}">Title {k}</a></h3>'
        + (f'<div class="variants"><a href="/p/{k}?c=red">Red</a><a href="/p/{k}?c=blue">Blue</a></div>'
           if k == variants_in else "")
        + "<p>about it</p></li>" for k in range(3))


LIST = f'<main><ul class="results">{cards()}</ul></main>'


def test_an_ad_link_inside_a_card_shares_the_cards_record():
    u = RC.record_units(links(guide_page()._dom, [fish(0), AD] + [fish(k) for k in range(1, 6)]))
    assert u.ids == [0, 0, 1, 2, 3, 4, 5] and u.how == "separates", u.ids
    assert u.primary == [True, False, True, True, True, True, True]
    assert u.most_per_record() == 2


def test_a_variant_link_shares_its_cards_record():
    u = RC.record_units(links(LIST, ["/p/0", "/p/1", "/p/1?c=red", "/p/2"]))
    assert u.ids == [0, 1, 1, 2] and u.primary == [True, True, False, True], (u.ids, u.primary)


def test_the_cards_of_a_grid_row_are_records_of_their_own():
    grid = "".join('<div class="row">' + "".join(
        f'<div class="card"><h3><a href="/g/{r}{c}">Grid {r}{c}</a></h3><p>x y</p></div>' for c in range(3))
        + "</div>" for r in range(2))
    u = RC.record_units(links(f'<main><div class="grid">{grid}</div></main>',
                              [f"/g/{r}{c}" for r in range(2) for c in range(3)]))
    assert u.ids == [0, 1, 2, 3, 4, 5] and all(u.primary), u.ids


def test_no_repeated_record_is_no_opinion():
    page = ('<main><div class="featured"><h2><a href="/f">Featured</a></h2></div>'
            '<ul class="list"><li><a href="/a">Alpha</a></li></ul></main>')
    u = RC.record_units(links(page, ["/f", "/a"]))
    assert u.ids == [None, None] and u.how == "" and u.most_per_record() is None


def test_classless_cards_are_told_apart_by_position():
    page = "<main><ul>" + "".join(f'<li><div><a href="/t/{k}">T{k} title</a></div><div><a href="/v/{k}">'
                                  f'V{k} variant</a></div></li>' for k in range(3)) + "</ul></main>"
    u = RC.record_units(links(page, ["/t/0", "/v/0", "/t/1", "/t/2"]))
    assert u.ids == [0, 0, 1, 2] and u.primary == [True, False, True, True], (u.ids, u.primary)


def test_one_cards_title_and_variant_are_one_record():
    u = RC.record_units(links(LIST, ["/p/1", "/p/1?c=red"]))
    assert u.ids == [0, 0] and u.how == "siblings" and u.primary == [True, False], (u.ids, u.how)


def test_a_calibrated_depth_places_each_node_at_that_level():
    u = RC.record_units(links(LIST, ["/p/0", "/p/1", "/p/1?c=red", "/p/2"]), depth=2)
    assert u.ids == [0, 1, 1, 2] and u.how == "depth" and [r.tag for r in u.records] == ["li"] * 4


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
