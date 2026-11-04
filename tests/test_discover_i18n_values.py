"""Discovery's search values from a page in a non-Latin script, offline.

The values a discovered search capability is proved with come from the site's own
words: the words its item names share, its frequent content words, minus its
brand words. Those readers once matched ``[a-z]`` only, so a Japanese, Russian or
Arabic result page gave no content words and no shared words, and a site without a
strong item cluster had no values at all. Now:

* in a script written with spaces a word is a run of letters, with the same length
  floor in characters (Hebrew and Arabic, written without short vowels: one fewer);
* a script written without spaces between words (Chinese, Japanese, Korean, Thai)
  is never cut into guessed words: whole link labels count, and so do the prefixes
  two or more labels share ("東京" of "東京タワー" and "東京駅"), cut back to a kana
  boundary rather than inside a kana word;
* the title's runs are brand words, and a value inside one is dropped;
* ASCII text reads exactly as before.

Fake sites from strings: no socket, no browser, no model.
"""
from __future__ import annotations

import unittest
import urllib.parse

from anyuse.execution import discover as D

HOST = "fix.test"
O = f"https://{HOST}"


def result_page(title, heading, labels, nav=("Home",)):
    links = "".join(f"<li><a href='/item/{urllib.parse.quote(n)}'>{n}</a></li>" for n in labels)
    navs = "".join(f"<a href='/{i}'>{t}</a>" for i, t in enumerate(nav))
    return (f"<html><head><title>{title}</title></head><body><nav>{navs}</nav>"
            f"<main><h2>{heading}</h2><ul class='results'>{links}</ul></main></body></html>")


JA = result_page("旅行ガイド - 公式サイト", "東京の検索結果",
                 ["東京タワー", "東京タイムズ", "東京駅", "東京大学", "大阪城", "大阪駅", "京都御所", "京都駅"])
RU = result_page("Путеводитель — главная", "Результаты поиска: Москва",
                 ["Москва Сити", "Москва Река", "Москва Кремль", "Казань Кремль", "Казань Арена",
                  "Сочи Парк", "Сочи Арена", "Москва Арена"])
AR = result_page("دليل السفر", "نتائج البحث عن القاهرة",
                 ["فندق القاهرة", "مطار القاهرة", "جامعة القاهرة", "فندق دبي", "مطار دبي", "جامعة دبي",
                  "برج دبي"])


def words(html, n=8):
    sc = D.scan_page(html, O + "/")
    brand = D.brand_words(sc, HOST)
    return D.content_word_values([sc], n=n, bodies={sc.url: html}, exclude=brand)


def value_sets(html):
    sc = D.scan_page(html, O + "/")
    clusters = D.link_clusters(sc.links, HOST)
    detail = D.detail_candidates(clusters)
    return D.search_value_sets(detail[0], clusters, detail[1:3])


class ContentWords(unittest.TestCase):

    def test_a_japanese_result_page_gives_whole_labels_and_their_shared_prefixes(self):
        got = words(JA)
        self.assertEqual(got[:3], ["東京", "大阪", "京都"], got)
        self.assertIn("東京タワー", got)
        # never a guessed segment: no cut inside the kana word タワー / タイムズ, no single ideograph, no heading phrase
        self.assertNotIn("東京タ", got)
        self.assertTrue(all(len(w) >= 2 for w in got), got)
        self.assertNotIn("東京の検索結果", got)

    def test_a_russian_result_page_gives_its_words(self):
        got = words(RU)
        self.assertEqual(got[:4], ["москва", "арена", "кремль", "казань"], got)
        # the page's own search words and the title's are not values
        for w in ("результаты", "поиска", "путеводитель", "главная"):
            self.assertNotIn(w, got)

    def test_an_arabic_result_page_gives_its_words_with_the_abjad_length_floor(self):
        got = words(AR)
        self.assertEqual(got[:2], ["القاهرة", "دبي"], got)
        self.assertIn("فندق", got)
        for w in ("نتائج", "البحث", "دليل", "السفر"):
            self.assertNotIn(w, got)

    def test_ascii_text_reads_as_before(self):
        sc = D.scan_page("<h1>River maps</h1><h2>River boats</h2><main>"
                         "<a href='/a'>Coastal maps archive</a><a href='/b'>Coastal boats</a>"
                         "<a href='/c'>Harbour charts</a><a href='/d'>Harbour river</a></main>", O + "/")
        self.assertEqual(D.content_word_values([sc]), ["river", "maps", "boats", "coastal"])

    def test_a_sign_spelled_with_letters_still_cuts_the_word(self):
        # NFKC spells "™" as "TM" and "№" as "No"; a value is typed into the site's search box as a word
        sc = D.scan_page("<h1>PlayStation™ games</h1><main><a href='/a'>PlayStation™ controller</a>"
                         "<a href='/b'>Café games</a></main>", O + "/")
        self.assertEqual(D.content_word_values([sc])[:2], ["playstation", "games"])
        self.assertEqual(D.spaced_words("PlayStation™ №5 Café", 2), ["playstation", "café"])
        self.assertEqual(D.unspaced_runs("東京タワー™ ㈱大阪"), ["東京タワー", "大阪"])

    def test_words_are_letter_runs_and_unspaced_runs_stay_whole(self):
        self.assertEqual(D.spaced_words("mp3player x-ray abcd", 4), ["player", "abcd"])
        self.assertEqual(D.spaced_words("Müller-Straße Café", 4), ["müller", "straße", "café"])
        self.assertEqual(D.unspaced_runs("plain ascii"), [])
        self.assertEqual(D.unspaced_runs("iPhoneケース 東京タワー2号"), ["ケース", "東京タワー"])


class SharedWords(unittest.TestCase):

    def test_item_names_in_a_spaced_script_share_words(self):
        self.assertEqual(value_sets(RU)[0], ["москва", "арена", "кремль", "казань"])
        self.assertEqual(value_sets(AR)[0], ["دبي", "القاهرة", "فندق", "مطار"])

    def test_item_names_in_an_unspaced_script_share_prefixes(self):
        html = result_page("案内", "検索", ["東京タワー", "東京駅", "大阪城", "大阪駅", "京都御所", "京都駅",
                                         "札幌駅", "札幌ドーム", "名古屋城", "名古屋駅"])
        sets = value_sets(html)
        self.assertEqual(sets[0], ["東京", "大阪", "京都", "札幌"], sets)
        self.assertEqual(sets[1][:2], ["東京タワー", "東京駅"], sets)
        sets = value_sets(result_page("指南", "搜索", ["东京塔", "东京站", "大阪城", "大阪站", "京都塔", "京都站",
                                                   "札幌站", "札幌塔"]))
        self.assertEqual(sets[0], ["东京", "大阪", "京都", "札幌"], sets)

    def test_label_prefixes_stop_at_a_kana_boundary(self):
        self.assertEqual(D.label_prefixes(["東京タワー", "東京タイムズ"]), ["東京"])
        self.assertEqual(D.label_prefixes(["東京", "東京タワー"]), ["東京"])
        self.assertEqual(D.label_prefixes(["서울역", "서울숲"]), ["서울"])
        # one ideograph or two kana are too short for a value
        self.assertEqual(D.label_prefixes(["東京", "東北"]), [])
        self.assertEqual(D.label_prefixes(["あいう", "あいえ"]), [])


class BrandWords(unittest.TestCase):

    def test_the_title_in_an_unspaced_script_is_a_brand_run(self):
        brand = D.brand_words(D.scan_page(JA, O + "/"), HOST)
        self.assertIn("公式サイト", brand)
        self.assertTrue(D.in_brand("公式", brand) and D.in_brand("サイト", brand))
        self.assertFalse(D.in_brand("東京", brand))
        sets = D.arrange_value_sets([["公式", "東京", "大阪", "京都", "札幌"]], [], brand, weak=False)
        self.assertEqual(sets, [["東京", "大阪", "京都", "札幌"]])

    def test_a_long_title_phrase_is_still_a_brand_run(self):
        # a run longer than a label is still the page frame: a search for a part of it matches every page
        sc = D.scan_page("<html><head><title>日本最大級の旅行予約サイトでお得に国内ホテルを予約</title></head>"
                         "<body></body></html>", O + "/")
        brand = D.brand_words(sc, HOST)
        self.assertTrue(D.in_brand("ホテル", brand) and D.in_brand("予約", brand), brand)
        self.assertFalse(D.in_brand("東京", brand))

    def test_an_internationalised_host_name_is_read_in_its_script(self):
        self.assertIn("日本語", D.brand_words(None, "xn--wgv71a119e.jp"))
        self.assertIn("путеводитель", D.brand_words(D.scan_page(RU, O + "/"), HOST))


# ---------------------------------------------------------------------------
# end to end: a non-Latin site with a search form and no item cluster
# ---------------------------------------------------------------------------

HOME_RU = ("<html><head><title>Путеводитель</title></head><body>"
           "<form action='/find' method='get'><input type='text' name='q'><button>Найти</button></form>"
           "<main><h2>Москва и Казань</h2><p>Москва — столица. Казань — город на Волге. Сочи у моря. "
           "Волга течёт мимо Казани.</p><a href='/a'>Москва зимой</a><a href='/b/c'>Сочи летом</a>"
           "<a href='/d/e/f'>Волга осенью</a></main></body></html>")


def site(url):
    p = urllib.parse.urlsplit(url)
    html = {"content-type": "text/html; charset=utf-8"}
    if p.path == "/robots.txt":
        return 200, {"content-type": "text/plain"}, "User-agent: *\nAllow: /\n"
    if p.path == "/":
        return 200, html, HOME_RU
    if p.path == "/find":
        q = dict(urllib.parse.parse_qsl(p.query)).get("q", "")
        return 200, html, f"<html><body><h1>{q}</h1><ul><li>{q} 1</li><li>{q} 2</li></ul></body></html>"
    return 404, html, "<html><body>нет</body></html>"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fetcher():
    clock = Clock()
    return D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)", allow_url=lambda u: True,
                         pacer=D.Pacer(1.0, clock=clock, sleeper=clock.sleep),
                         client=lambda url, headers: site(url))


class EndToEnd(unittest.TestCase):

    def test_a_search_form_on_a_russian_site_gets_the_sites_own_words(self):
        hd = D.discover_host(O, fetcher())
        search = [c for c in hd.caps["search"] if c.source == "form"]
        self.assertTrue(search, hd.caps["search"])
        self.assertEqual(search[0].values, ["москва", "казань", "сочи", "волга"], search[0].values)


if __name__ == "__main__":
    unittest.main()
