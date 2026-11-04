"""What identifies a link's target (execution/url_identity.py): the page's own links decide
first, and the tracking lists are the last rung. A parameter the page tells its links apart
by stays part of their identity whatever a list names; a parameter that varies per item
while the path tells the targets apart is noise; with no page evidence, the click ids and
the vendor tags are dropped as before.

Offline: link lists and one synthetic page. No browser, network or model.
"""
import sys

import pytest

from anyuse.execution import discover as D
from anyuse.execution import url_identity as UI


def test_a_listed_name_the_page_tells_its_links_apart_by_is_identity():
    links = [f"https://tv.test/show?ito={i}" for i in (101, 102, 103)]
    noise = UI.page_noise(links)
    assert noise == set() and noise.identity == {"ito"}
    assert len({UI.identity(u, noise) for u in links}) == 3
    # a session tag that is the only thing telling the targets apart is identity too
    sid = [f"https://shop.test/p?_sid={s}" for s in ("a1", "b2")]
    assert len({UI.identity(u, UI.page_noise(sid)) for u in sid}) == 2
    # without the page's evidence the list is the last rung
    assert len({UI.identity(u) for u in links}) == 1


def test_per_item_noise_and_tracking_still_never_identify_a_target():
    shop = [f"https://s.test/products/{s}?_pos={i}&_sid=ab&gclid=x{i}&utm_source=nl"
            for i, s in enumerate("abc", 1)]
    noise = UI.page_noise(shop)
    assert {"_pos"} <= noise and not noise.identity & {"_pos", "_sid"}
    assert [UI.identity(u, noise) for u in shop] == [f"https://s.test/products/{s}" for s in "abc"]
    # one product listed twice, at two positions: the position is still not what tells targets apart
    twice = shop + ["https://s.test/products/a?_pos=4&_sid=ab"]
    assert "_pos" not in UI.page_noise(twice).identity
    assert UI.identity(twice[0], UI.page_noise(twice)) == UI.identity(twice[-1], UI.page_noise(twice))
    # the same on a short list: two products, one of them listed twice
    short = ["https://s.test/products/a?_pos=1&_sid=ab", "https://s.test/products/b?_pos=2&_sid=ab",
             "https://s.test/products/a?_pos=3&_sid=ab"]
    noise = UI.page_noise(short)
    assert "_pos" not in noise.identity and len({UI.identity(u, noise) for u in short}) == 2


def test_a_page_whose_results_differ_only_in_a_listed_name_forms_a_result_region():
    rows = "".join(f"<li><a href='/show?ito={i}'>Episode {i}</a></li>" for i in range(1, 6))
    scan = D.scan_page(f"<html><body><ul>{rows}</ul></body></html>", "https://tv.test/search?q=x")
    clusters = D.link_clusters(scan.links, "tv.test", min_members=2)
    assert any(sorted(c.values) == [str(i) for i in range(1, 6)] for c in clusters), \
        [(c.template, c.values) for c in clusters]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
