"""List ORDER and COMPLETENESS in the attribute verifier (``attribute_verify.verify_order`` / ``result_items``). Pages built inline, no network, no model.

The failing cases: www.velo101.com answered results 2, 7, 8, 18 and
19 of its page (every result a heading link), www.woopets.fr skipped the 4th result (list-item links); every
answered title was on its own card, so both were verified, and the reader called both wrong. What must hold:

1. the page's result list is read independently of the card reader: the region holding the answered links and its
   items (the region's children of the answered items' tag holding a link with an answered link's tag path; a row of
   several answered results split into its own items), in document order - a category or author link inside a card
   makes no result of its own;
2. a result skipped before or between the answered ones, or two answered results in the other order, is a CONFLICT -
   the answer is withheld (the velo101 and woopets shapes);
3. the first N in order is verified (a top-5 of 10, the whole list); a placement link (rel sponsored / nofollow) and a
   link outside the tool's scope are not results;
4. links in the page's header, navigation, footer or a sidebar are chrome, never results (an answered result that is
   also a menu entry is placed by its own result link); no DOM is UNCERTAIN - withheld, never ok, and no model call is
   made for it;
5. one answered result: verified when the page lists one, a conflict when the page's list starts with another;
6. through ``check`` (site_search) and ``verify_paged`` (each page's rows against that page).
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402

BASE = "https://news.example"
TITLES = ["Championnats de France J-1 les engagés", "Interview de Laurent Servaes", "Hugo Hofstetter couronné",
          "Le Dauphiné va changer", "Un concentré de talent", "Mourey contre les autres", "Interview de Laurent Bezault",
          "Interview Alexandre Blanc", "Mercantour Classic le parcours", "Tour des Alpes Maritimes"]


def url(k):
    return f"{BASE}/article/{k}-story/"


def heading_list(extra_before="", sidebar=False, sponsored_at=None, offsite_at=None):
    cards = []
    for k, t in enumerate(TITLES):
        if sponsored_at == k:
            cards.append(f'<div class="card"><div class="in"><h2><a rel="sponsored" href="{BASE}/promo/">Promo partner'
                         f'</a></h2></div></div>')
        if offsite_at == k:
            cards.append('<div class="card"><div class="in"><h2><a href="https://ads.example/x">Elsewhere</a></h2>'
                         '</div></div>')
        cards.append(f'<div class="card"><div class="in"><h2><a href="{url(k)}">{t}</a></h2><p>Teaser {k}</p></div></div>')
    side = (f'<aside><h3><a href="{BASE}/most-read/">Most read story</a></h3></aside>' if sidebar else "")
    return (f'<html><head><title>Search</title></head><body><header><nav><ul><li><a href="/">Home</a></li>'
            f'<li><a href="/route/">Route</a></li></ul></nav></header><main><h1>Résultats</h1>{extra_before}'
            f'<div class="results">{"".join(cards)}</div>{side}</main><footer><a href="/legal/">Legal</a></footer>'
            f'</body></html>')


def li_list():
    items = "".join(f'<li><a href="{url(k)}">{t}</a></li>' for k, t in enumerate(TITLES))
    return (f'<html><body><header><nav><ul><li><a href="/">Home</a></li></ul></nav></header><main>'
            f'<div class="r"><ul class="list">{items}</ul></div></main></body></html>')


def page(dom, u=BASE + "/?s=x"):
    return AV.Page(dom, "", u)


def answer(ks):
    return {"results": [{"title": TITLES[k], "url": url(k)} for k in ks]}


def order(ans, dom, scope=("news.example",)):
    v = AV.verify_order(ans, page(dom), list(scope) if scope is not None else None)
    return v.decisions[0] if v.decisions else None


class OrderTests(unittest.TestCase):
    def test_velo101_shape_skipped_first_result_is_a_conflict(self):
        d = order(answer([1, 6, 7]), heading_list())
        self.assertEqual(d.status, AV.CONFLICT)
        self.assertIn("skipped", d.why)
        self.assertIn("Championnats", d.why)

    def test_woopets_shape_skipped_fourth_result_is_a_conflict(self):
        d = order(answer([0, 1, 2, 4, 5]), li_list())
        self.assertEqual(d.status, AV.CONFLICT)
        self.assertIn("result 4", d.why)

    def test_first_n_in_order_is_verified(self):
        for dom in (heading_list(), li_list()):
            self.assertEqual(order(answer([0, 1, 2, 3, 4]), dom).status, AV.VERIFIED)
            self.assertEqual(order(answer(list(range(10))), dom).status, AV.VERIFIED)

    def test_reordered_is_a_conflict(self):
        d = order(answer([1, 0, 2]), heading_list())
        self.assertEqual(d.status, AV.CONFLICT)
        self.assertIn("reordered", d.why)

    def test_placement_and_off_scope_links_are_not_results(self):
        self.assertEqual(order(answer([0, 1, 2]), heading_list(sponsored_at=1)).status, AV.VERIFIED)
        self.assertEqual(order(answer([0, 1, 2]), heading_list(offsite_at=2)).status, AV.VERIFIED)
        # without a declared scope the off-site heading link is a result like any other
        self.assertEqual(order(answer([0, 1, 2]), heading_list(offsite_at=2), scope=None).status, AV.CONFLICT)

    def test_page_chrome_makes_no_result_and_no_dom_is_uncertain(self):
        # the answered results sit in two blocks of the body; the header, the nav between them and the footer are chrome
        split = (f'<html><body><header><nav><a href="/">Home</a></nav></header><section><h2><a href="{url(0)}">'
                 f'{TITLES[0]}</a></h2></section><nav><h2><a href="/a/">A menu heading</a></h2></nav><section><h2>'
                 f'<a href="{url(1)}">{TITLES[1]}</a></h2></section><footer><a href="/l/">Legal</a></footer></body>'
                 f'</html>')
        self.assertEqual(order(answer([0, 1]), split).status, AV.VERIFIED)
        # a category landing page that is ALSO in the menu: its result link, not its menu link, places it
        menu = heading_list().replace('<li><a href="/route/">Route</a></li>', f'<li><a href="{url(2)}">Menu entry</a></li>')
        self.assertEqual(order(answer([0, 1, 2]), menu).status, AV.VERIFIED)
        self.assertEqual(AV.verify_order(answer([0]), AV.Page("", "x", BASE)).decisions[0].status, AV.UNCERTAIN)
        # an answer that took a sidebar item next to the list: never verified
        ans = {"results": [{"title": TITLES[0], "url": url(0)}, {"title": "Most read story",
                                                                "url": BASE + "/most-read/"}]}
        self.assertNotEqual(order(ans, heading_list(sidebar=True)).status, AV.VERIFIED)

    def test_one_answered_result(self):
        single = (f'<html><body><main><div class="r"><h2><a href="{url(0)}">{TITLES[0]}</a></h2></div></main>'
                  f'</body></html>')
        self.assertEqual(order(answer([0]), single).status, AV.VERIFIED)
        self.assertEqual(order(answer([3]), heading_list()).status, AV.CONFLICT)
        self.assertEqual(order(answer([0]), heading_list()).status, AV.VERIFIED)

    def test_through_check_withheld_without_a_model_call(self):
        asked = []

        def model(prompt, schema):
            asked.append(prompt)
            return {"choice": "unclear", "calls": 1}
        dom = heading_list()
        v = AV.check("site_search", answer([1, 6, 7]), page(dom), AV.Resolver(model), scope=["news.example"])
        self.assertNotEqual(v.verdict, AV.ACCEPT)
        od = next(d for d in v.decisions if d.attr == "order")
        self.assertIn(od.status, (AV.CONFLICT, AV.WITHHELD))
        self.assertEqual(asked, [])
        v = AV.check("site_search", answer([0, 1, 2]), page(dom), AV.Resolver(model), scope=["news.example"])
        self.assertEqual(v.verdict, AV.ACCEPT)

    def test_paged_rows_are_checked_on_their_own_page(self):
        p1, p2 = page(heading_list()), page(li_list(), BASE + "/?s=x&page=2")
        rows = [dict(r, page=1) for r in answer([0, 1, 2])["results"]] + \
               [dict(r, page=2) for r in answer([0, 1])["results"]]
        v = AV.verify_paged({"results": rows}, {1: p1, 2: p2}, None, ["news.example"])
        self.assertTrue(all(d.status == AV.VERIFIED for d in v.decisions if d.attr == "order"))
        rows[3:] = [dict(r, page=2) for r in answer([0, 2])["results"]]
        v = AV.verify_paged({"results": rows}, {1: p1, 2: p2}, None, ["news.example"])
        self.assertTrue(any(d.attr == "order" and d.status == AV.CONFLICT for d in v.decisions))


if __name__ == "__main__":
    unittest.main()
