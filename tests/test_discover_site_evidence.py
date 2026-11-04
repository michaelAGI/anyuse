"""Discovery (execution/discover.py) reads the site's own evidence before its word lists, offline.

What must hold:

* paging: a query key whose values on the page's own links read as page numbers, page offsets or a ``rel=next``
  link is paging in any language (``strona``, ``sayfa``, ``od``); a per-page choice, a year filter and item ids are
  not; a key named like paging still is (the last rung);
* filters, listings, detail keys and link texts match whole words: ``location`` is no ``cat`` filter, "Newsletter"
  is no "new" listing, ``tv`` is not the key of ``/tvshow``;
* languages: an ISO 639-1 code is a language switch, read against its cluster ("vi" among editor packages is an
  item); other two-letter values ("tv", "ai") are items;
* a search box by the page's structure (HTML ``<search>``, ``role=searchbox``), then by the check model inside an
  open scope only, then by names;
* links every page read shows are navigation, and words every page read shows are no search values, in any
  language;
* any filter may be a sort: the sort proof runs for a filter named in Dutch whose values reorder the same items,
  and a filter that reorders nothing keeps the learn proof's reason;
* the optional ranker uses the configured judge on any provider.
"""
from __future__ import annotations

import os
import sys
import unittest
import urllib.parse

from anyuse.execution import discover as D
from anyuse.execution import model_check as MC
from anyuse.execution.api_step import ApiResponse

O = "https://winkel.test"


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def scan(body, url=O + "/lijst"):
    return D.scan_page(_page("Lijst", body), url)


class Paging(unittest.TestCase):

    def test_page_numbers_offsets_and_next_in_any_language(self):
        for key, vals, texts, want in (("strona", [2, 3, 4, 5], ["2", "3", "4", "5"], ["2", "3", "4", "5"]),
                                       ("sayfa", [1, 2, 4, 9], ["1", "2", "4", "9"], ["2", "4", "9"]),
                                       ("od", [20, 40, 60], ["2", "3", "4"], ["20", "40", "60"])):
            body = "".join(f"<a href='/lijst?q=thee&{key}={v}'>{t}</a>" for v, t in zip(vals, texts))
            got = D.pagination_candidates(scan(body, O + "/lijst?q=thee"))
            self.assertEqual([(c.param, c.values) for c in got], [(key, want)], key)
        nxt = scan("<link rel='next' href='/lijst?q=thee&trang=2'>", O + "/lijst?q=thee")
        self.assertEqual([c.param for c in D.pagination_candidates(nxt)], ["trang"])

    def test_choices_years_and_ids_are_not_paging_but_a_paging_name_still_is(self):
        for key, vals in (("ile", [12, 24, 48]), ("rok", [2019, 2020, 2021, 2022]), ("id", [101, 102, 103])):
            body = "".join(f"<a href='/lijst?{key}={v}'>{v}</a>" for v in vals)
            self.assertEqual(D.pagination_candidates(scan(body)), [], key)
        body = "<a href='/lijst?page=2'>volgende</a>"
        self.assertEqual([c.param for c in D.pagination_candidates(scan(body))], ["page"])
        self.assertEqual(D.paging_run([(0, "1", ""), (24, "2", ""), (48, "3", "")]), "offset")


class WholeWords(unittest.TestCase):

    def test_filter_listing_and_key_words(self):
        for name in ("sortBy", "category_id", "orderby", "lang"):
            self.assertTrue(D.filter_named(name), name)
        for name in ("location", "baby", "stage", "zip"):
            self.assertFalse(D.filter_named(name), name)
        for text in ("New arrivals", "Browse all", "Categories", "list"):
            self.assertTrue(D.listing_named(text), text)
        for text in ("Newsletter", "Listen now", "Indexing service"):
            self.assertFalse(D.listing_named(text), text)
        self.assertTrue(D.key_in_url("https://s.test/p/12345-blue-shirt", "12345"))
        self.assertTrue(D.key_in_url("https://s.test/show?id=tv", "tv"))
        self.assertTrue(D.key_in_url("https://s.test/pkg/serde/", "serde"))
        self.assertFalse(D.key_in_url("https://s.test/tvshow/1", "tv"))
        self.assertFalse(D.key_in_url("https://s.test/p/123456", "2345"))

    def test_a_select_named_location_scores_as_any_field(self):
        opts = "".join(f"<option value='{v}'>{v}</option>" for v in ("a", "b", "c", "d", "e"))
        sc = scan(f"<form method='get' action='/lijst'><select name='location'>{opts}</select></form>")
        self.assertEqual([c.score for c in D.filter_candidates(sc)], [2.0])

    def test_languages_by_the_standard_and_by_the_cluster(self):
        for v in ("en", "pt-br", "zh_hant", "de/", "faq"):
            self.assertTrue(D.is_nav_value(v), v)
        for v in ("tv", "ai", "go", "jq", "gh", "js", "vim"):
            self.assertFalse(D.is_nav_value(v), v)
        pkgs = D.link_clusters([D.Link(url=f"https://s.test/pkg/{n}", text=n)
                                for n in ("vim", "vi", "nano", "emacs", "joe", "mg")], "s.test")[0]
        self.assertFalse(pkgs.is_nav("vi"))
        langs = D.link_clusters([D.Link(url=f"https://s.test/{n}/", text=n)
                                 for n in ("en", "de", "fr", "vi", "pl", "nl")], "s.test")[0]
        self.assertTrue(langs.is_nav("vi") and langs.nav_frac() == 1.0)


class SearchInput(unittest.TestCase):

    def test_structure_then_an_open_scope_then_names(self):
        page = ("<search><input name='fraza'></search><input role='searchbox' name='aranan'>"
                "<input name='tu_khoa'><input name='research_area'><input name='q'>")
        inputs = {i["name"]: i for i in scan(page).inputs}
        for name in ("fraza", "aranan", "q"):
            self.assertTrue(D.is_search_input(inputs[name]), name)
        for name in ("tu_khoa", "research_area"):
            self.assertFalse(D.is_search_input(inputs[name]), name)
        asked = []

        def model(instructions, state, obs):
            asked.append(state)
            return 0.95 if "tu_khoa" in state else 0.05
        with MC.using(model):
            self.assertFalse(D.is_search_input(inputs["tu_khoa"]))       # no scope open: nothing is asked
            self.assertEqual(asked, [])
            with MC.scope("compile"):
                self.assertTrue(D.is_search_input(inputs["tu_khoa"]))
                self.assertFalse(D.is_search_input(inputs["research_area"]))
                self.assertTrue(D.is_search_input(inputs["fraza"]))      # structure: not asked
        self.assertEqual(len(asked), 2)

    def test_get_search_forms_rank_by_structure_and_whole_words(self):
        page = ("<form action='/research/list' method='get' class='research-filter'>"
                "<input name='topic' placeholder='Research topic'></form>"
                "<search><form action='/zoeken' method='get'><input name='zoekterm'></form></search>"
                "<form action='/szukaj' method='get'><input name='miasto'><input name='fraza' role='searchbox'></form>")
        caps = sorted(D.search_from_forms(scan(page, O + "/")), key=lambda c: -c.score)
        by_param = {c.param: c.score for c in caps}
        self.assertEqual(set(by_param), {"topic", "zoekterm", "fraza"})       # role=searchbox picks the field
        self.assertLess(by_param["topic"], by_param["zoekterm"])               # "research" names no search
        self.assertLess(by_param["topic"], by_param["fraza"])
        self.assertEqual(by_param["topic"], 0.0)
        old = ("<form action='/search' method='get' class='search-form'>"
               "<input name='q' placeholder='Search the shop'></form>"
               "<form action='/index.php?route=product/search' method='get'><input name='filter_name'></form>")
        got = {c.param: c.score for c in D.search_from_forms(scan(old, O + "/"))}
        self.assertEqual(got, {"q": 8.0, "filter_name": 2.0})


class FrameAndChrome(unittest.TestCase):

    BODY = ("<div class='kop'><a href='/over'>Over ons</a><a href='/hulp'>Hulp en service</a>"
            "<a href='/winkelwagen'>Winkelwagen</a></div><p>Voor deze winkel geldt: naar onze voorwaarden, "
            "maar niet voor alles.</p><div class='lijst'>{rows}</div>")

    def _scans(self, n):
        pages = [("thee", ["groene thee", "zwarte thee", "kruidenthee", "rooibos thee"]),
                 ("koffie", ["koffiebonen arabica", "koffiebonen robusta", "filterkoffie", "espresso bonen"]),
                 ("melk", ["volle melk", "halfvolle melk", "karnemelk", "melkpoeder"])][:n]
        scans, bodies = [], {}
        for word, rows in pages:
            body = self.BODY.format(rows="".join(f"<a href='/p/{r.replace(' ', '-')}'>{r}</a>" for r in rows))
            url = f"{O}/{word}"
            scans.append(D.scan_page(_page(word, body), url))
            bodies[url] = _page(word, body)
        return scans, bodies

    def test_links_and_words_on_every_page_read_are_the_sites_frame(self):
        scans, bodies = self._scans(3)
        self.assertEqual(D.mark_site_chrome(scans), 9)
        self.assertTrue(all(ln.chrome for sc in scans for ln in sc.links if "/p/" not in ln.url))
        self.assertFalse(any(ln.chrome for sc in scans for ln in sc.links if "/p/" in ln.url))
        frame = D.frame_words(scans, bodies)
        self.assertTrue({"voor", "deze", "naar", "onze", "maar"} <= frame)
        words = D.content_word_values(scans, n=8, bodies=bodies)
        self.assertFalse(set(words) & {"voor", "deze", "naar", "onze", "maar", "winkel"}, words)
        self.assertTrue({"thee", "melk"} & set(words), words)

    def test_two_pages_are_not_enough_to_call_anything_frame(self):
        scans, bodies = self._scans(2)
        self.assertEqual(D.mark_site_chrome(scans), 0)
        self.assertEqual(D.frame_words(scans, bodies), set())


ITEMS = ["appel", "banaan", "citroen", "dadel", "druif", "kiwi", "mango", "peer"]


def shop(url, reorder=True):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    html = {"content-type": "text/html"}
    if p.path == "/lijst":
        names = list(ITEMS)
        v = q.get("volgorde", "naam")
        if reorder:
            k = {"naam": 0, "prijs": 3, "nieuw": 5, "populair": 2}.get(v, 0)
            names = names[k:] + names[:k]
            if v == "prijs":
                names = names[::-1]
        rows = "".join(f"<li class='product'><a href='/p/{n}'>{n}</a> <span>{n} fruit</span></li>" for n in names)
        links = "".join(f"<a href='/lijst?volgorde={x}'>{x}</a>" for x in ("naam", "prijs", "nieuw", "populair"))
        return 200, html, _page("Fruit", f"<ul class='lijst'>{rows}</ul>{links}")
    return 404, html, _page("Niet gevonden", "")


class Transport:
    def __init__(self, reorder=True):
        self.reorder, self.calls = reorder, []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        st, h, text = shop(url, self.reorder)
        return ApiResponse(status=st, headers=h, text=text)


class AnySortName(unittest.TestCase):

    def setUp(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"

    def cap(self):
        return D.Capability(cap="filter", template=O + "/lijst?volgorde={v}", param="volgorde",
                            values=["naam", "prijs", "nieuw", "populair"], source="links")

    def _fetch(self, reorder=True):
        return D.HttpFetcher(user_agent="research-probe/0.1", pacer=D.Pacer(0.0),
                             client=lambda url, h: shop(url, reorder))

    def test_a_filter_named_in_dutch_that_reorders_the_same_items_is_a_sort(self):
        self.assertFalse(D.is_sort(self.cap()))
        f = self._fetch()
        out = D.prove(self.cap(), O, f, transport=D.PacedTransport(f, inner=Transport()),
                      proof_preflight=lambda c, r: "")
        self.assertEqual((out.status, out.proof_kind), ("proved", "sort"), out.reason)

    def test_a_filter_that_reorders_nothing_keeps_the_learn_proofs_reason(self):
        f = self._fetch(reorder=False)
        out = D.prove(self.cap(), O, f, transport=D.PacedTransport(f, inner=Transport(False)),
                      proof_preflight=lambda c, r: "")
        self.assertNotEqual(out.status, "proved")
        self.assertEqual(out.proof_kind, "learn")
        self.assertEqual([a.get("kind", "learn") for a in out.proof["attempts"]][-1], "sort")


class Ranker(unittest.TestCase):

    def test_the_ranker_runs_on_any_configured_provider(self):
        from anyuse.execution.model_adapter import ModelResult, TokenUsage

        class Adapter:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, **kw):
                self.calls += 1
                return ModelResult(ok=True, text='{"order": [1, 0]}', usage=TokenUsage(100, 10, 110))
        a = Adapter()
        rank = D.model_ranker(adapter=a)
        order, usd = rank("search", "winkel.test", [{"template": "a"}, {"template": "b"}])
        self.assertEqual((order, a.calls), ([1, 0], 1))
        src = open(D.__file__, encoding="utf-8").read()
        self.assertNotIn('provider != "cerebras"', src)


if __name__ == "__main__":
    sys.exit(unittest.main())
