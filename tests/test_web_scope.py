"""execution/web_scope.py, offline: every allow and deny path of the four
web-scope settings (docs/TRAFFIC_POLICY.md "Web scope"). No network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from anyuse.execution import web_scope as W

SITE = "www.example.org"
CSE_PAGE = ('<html><head><script async src="https://cse.google.com/cse.js?cx=0123456789abc:defgh">'
            '</script></head><body><div class="gcse-search"></div></body></html>')
ALGOLIA_JS = ('window.cfg={algolia:{appId:"ABCDEF1234",apiKey:"0123456789abcdef0123456789abcdef",'
              'indexName:"docs"}};')
YEXT_PAGE = '<script src="https://assets.sitescdn.net/answers/v1.15/answers.min.js"></script>'
TINY_BLOCK = frozenset({"google-analytics.com", "doubleclick.net", "adnxs.com"})


def scope(**kw):
    kw.setdefault("blocklist_domains", TINY_BLOCK)
    return W.WebScope(**kw)


class Detection(unittest.TestCase):
    def test_google_cse_from_script_url(self):
        found = W.detect_providers(CSE_PAGE)
        self.assertEqual([s.name for s, _h, _e in found], ["google_cse"])
        hosts = found[0][1]
        self.assertIn("cse.google.com", hosts)
        self.assertIn("www.googleapis.com", hosts)

    def test_algolia_needs_the_app_id_and_the_word(self):
        found = W.detect_providers(ALGOLIA_JS)
        self.assertEqual([s.name for s, _h, _e in found], ["algolia"])
        self.assertIn("abcdef1234-dsn.algolia.net", found[0][1])
        self.assertIn("abcdef1234-2.algolianet.com", found[0][1])
        # an id-shaped value without any mention of algolia is not a detection
        self.assertEqual(W.detect_providers('cfg={appId:"ABCDEF1234"}'), [])

    def test_evidence_is_redacted(self):
        found = W.detect_providers(
            'x="https://abcdef1234-dsn.algolia.net/1/indexes/*/queries?'
            'x-algolia-api-key=SECRET1234567890&x-algolia-application-id=ABCDEF1234"')
        self.assertTrue(found)
        self.assertNotIn("SECRET1234567890", found[0][2])

    def test_other_providers(self):
        for src, name in (
                (YEXT_PAGE, "yext"),
                ('fetch("https://my-deploy.ent.us-east-1.aws.cloud.es.io/api/as/v1/engines/x/search")',
                 "elastic_app_search"),
                ('<script src="//s.swiftypecdn.com/install/v2/st.js"></script>', "swiftype"),
                ('nodes:[{host:"abc123xyz-1.a1.typesense.net"}]', "typesense_cloud"),
                ('<script src="https://static.cloud.coveo.com/searchui/v2.10/js/CoveoJsSearch.js">',
                 "coveo"),
                ('<script src="https://ui.customsearch.ai/api/ux/render?customConfig=1"></script>',
                 "bing_custom_search")):
            self.assertIn(name, [s.name for s, _h, _e in W.detect_providers(src)], src)

    def test_nothing_is_guessed(self):
        # a page that merely links to google.com, or talks about search, grants nothing
        self.assertEqual(W.detect_providers('<a href="https://www.google.com/search?q=x">'
                                            'Search with Google</a>'), [])
        self.assertEqual(W.detect_providers(""), [])


class Providers(unittest.TestCase):
    def granted(self, src=CSE_PAGE, **kw):
        s = scope(providers=True, **kw)
        self.assertEqual(s.observe(SITE, f"https://{SITE}/", "Document", src)[:1],
                         [W.detect_providers(src)[0][0].name])
        return s

    def test_off_by_default(self):
        s = scope()
        self.assertFalse(s.enabled)
        s.observe(SITE, f"https://{SITE}/", "Document", CSE_PAGE)
        v = s.classify(SITE, "https://cse.google.com/cse.js?cx=1", "GET", "Script")
        self.assertFalse(v.ok)
        self.assertTrue(v.why.startswith("third-party host"))

    def test_grant_allows_provider_search_get_for_that_site_only(self):
        s = self.granted()
        v = s.classify(SITE, "https://cse.google.com/cse/element/v1?q=x&cx=1", "GET", "Script")
        self.assertEqual(v.kind, W.KIND_PROVIDER)
        self.assertEqual(s.site_for("cse.google.com"), SITE)
        other = s.classify("other.example.net", "https://cse.google.com/cse.js?cx=1", "GET",
                           "Script")
        self.assertFalse(other.ok, "a grant is for the site whose page named the provider")

    def test_paths_outside_the_search_paths_are_refused(self):
        s = self.granted()
        self.assertFalse(s.classify(SITE, "https://www.google.com/search?q=x", "GET",
                                    "XHR").ok)
        self.assertFalse(s.classify(SITE, "https://www.google.com/recaptcha/api.js", "GET",
                                    "Script").ok)
        self.assertTrue(s.classify(SITE, "https://www.google.com/cse/static/element/a/x.js",
                                   "GET", "Script").ok)

    def test_provider_is_never_a_navigation(self):
        s = self.granted()
        self.assertFalse(s.classify(SITE, "https://cse.google.com/cse?cx=1&q=x", "GET",
                                    "Document").ok)

    def test_documented_post_read_and_its_preflight(self):
        s = self.granted(ALGOLIA_JS)
        url = "https://abcdef1234-dsn.algolia.net/1/indexes/*/queries?x-algolia-api-key=k"
        body = json.dumps({"requests": [{"indexName": "docs", "params": "query=rust"}]})
        self.assertEqual(s.classify(SITE, url, "POST", "XHR", body).kind, W.KIND_PROVIDER)
        pre = s.classify(SITE, url, "OPTIONS", "Preflight",
                         headers={"Access-Control-Request-Method": "POST"})
        self.assertEqual(pre.kind, W.KIND_PROVIDER)
        # a write to the same provider (index settings, a DELETE) is not a read
        self.assertFalse(s.classify(SITE, "https://abcdef1234-dsn.algolia.net/1/indexes/docs/"
                                          "settings", "PUT", "XHR", "{}").ok)
        self.assertFalse(s.classify(SITE, "https://abcdef1234-dsn.algolia.net/1/indexes/docs",
                                    "DELETE", "XHR").ok)
        self.assertFalse(s.classify(SITE, url, "OPTIONS", "Preflight",
                                    headers={"Access-Control-Request-Method": "DELETE"}).ok)

    def test_provider_analytics_hosts_stay_blocked(self):
        s = self.granted(ALGOLIA_JS)
        self.assertFalse(s.classify(SITE, "https://insights.algolia.io/1/events", "POST",
                                    "XHR", "{}").ok)

    def test_a_post_read_with_a_state_field_is_refused(self):
        s = self.granted(ALGOLIA_JS)
        url = "https://abcdef1234-dsn.algolia.net/1/indexes/*/queries"
        self.assertFalse(s.classify(SITE, url, "POST", "XHR",
                                    json.dumps({"deleteObject": 1})).ok)

    def test_grants_are_only_from_the_sites_own_sources(self):
        # the caller only shows the scope same-site sources; observe() on a
        # different site grants for THAT site, never for SITE
        s = scope(providers=True)
        s.observe("other.example.net", "https://other.example.net/", "Document", CSE_PAGE)
        self.assertFalse(s.classify(SITE, "https://cse.google.com/cse.js?cx=1", "GET",
                                    "Script").ok)


class PostSearch(unittest.TestCase):
    FORM = ('<form method="post" action="/search/results"><input type="text" name="keyword" '
            'placeholder="Search"><input type="hidden" name="type" value="all">'
            '<button type="submit" name="go">Go</button></form>'
            '<form method="post" action="/login"><input name="user"><input type="password" '
            'name="pw"></form>')

    def test_off_refuses_every_post(self):
        s = scope()
        v = s.classify(SITE, f"https://{SITE}/search", "POST", "Document", "q=x",
                       same_site=True)
        self.assertFalse(v.ok)
        self.assertIn("method POST", v.why)

    def test_the_pages_own_post_search_form(self):
        s = scope(readonly_post=True)
        s.observe(SITE, f"https://{SITE}/", "Document", self.FORM)
        self.assertEqual(len(s.forms[SITE]), 1, "the login form is not a search form")
        ok = s.classify(SITE, f"https://{SITE}/search/results", "POST", "Document",
                        "keyword=granite&type=all&go=", same_site=True)
        self.assertEqual(ok.kind, W.KIND_POST)
        extra = s.classify(SITE, f"https://{SITE}/search/results", "POST", "Document",
                           "keyword=granite&type=all&admin=1", same_site=True)
        self.assertFalse(extra.ok, "a field the page's form does not send")

    def test_xhr_post_carrying_the_search_value(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["Granite", "basalt"])
        ok = s.classify(SITE, f"https://{SITE}/api/find", "POST", "XHR",
                        json.dumps({"query": "granite", "page": 1}), same_site=True)
        self.assertEqual(ok.kind, W.KIND_POST)
        no_value = s.classify(SITE, f"https://{SITE}/api/find", "POST", "XHR",
                              json.dumps({"query": "something else"}), same_site=True)
        self.assertFalse(no_value.ok)
        # the value in a non-search field of a non-search path is not the search
        self.assertFalse(s.classify(SITE, f"https://{SITE}/api/items", "POST", "XHR",
                                    json.dumps({"name": "granite"}), same_site=True).ok)

    def test_the_typed_value_right_after_typing_is_the_search_whatever_its_name(self):
        # Polish, Turkish, Vietnamese field names: the run typed "kawa" into the site's search box and the page sent
        # it as a whole field value right after; the held-out proof decides whether the answer is a listing
        s = scope(readonly_post=True)
        s.note_values(SITE, ["kawa"])
        for path, body in (("/api/wyszukaj", json.dumps({"fraza": "kawa", "strona": 1})),
                           ("/arama", "kelime=kawa"), ("/tim-kiem", json.dumps({"tu_khoa": "Kawa"}))):
            v = s.classify(SITE, f"https://{SITE}{path}", "POST", "XHR", body, same_site=True, acting=True)
            self.assertEqual(v.kind, W.KIND_POST, (path, v.why))
            self.assertIn("right after typing", v.detail)
            # not right after typing, the names decide, and these name no search
            self.assertFalse(s.classify(SITE, f"https://{SITE}{path}", "POST", "XHR", body, same_site=True).ok)
        # a value the run did not type into the search box (a run input) needs a search name, as before
        s2 = scope(readonly_post=True)
        self.assertFalse(s2.classify(SITE, f"https://{SITE}/api/items", "POST", "XHR", json.dumps({"name": "kawa"}),
                                     same_site=True, values=("kawa",), acting=True).ok)

    def test_search_words_are_whole_words_or_word_starts(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["granite"])
        # "research" holds "search", "besuchen" holds "suche": neither path names a search
        for path in ("/research/topics", "/besuchen"):
            self.assertFalse(s.classify(SITE, f"https://{SITE}{path}", "POST", "XHR", "topic=granite",
                                        same_site=True).ok, path)
        for path in ("/searchResults", "/api/site-suche", "/zoekresultaten"):
            self.assertTrue(s.classify(SITE, f"https://{SITE}{path}", "POST", "XHR", "topic=granite",
                                       same_site=True).ok, path)
        # an HTML <search> element or an ARIA searchbox makes a POST form a search form, in any language
        page = ('<search><form method="post" action="/szukaj"><input name="fraza"></form></search>'
                '<form method="post" action="/x"><input role="searchbox" name="aranan"></form>'
                '<form method="post" action="/newsletter"><input name="research_area"></form>')
        s.observe(SITE, f"https://{SITE}/", "Document", page)
        self.assertEqual(sorted(f.path for f in s.forms[SITE]), ["/szukaj", "/x"])

    def test_state_changing_paths_and_fields_are_refused(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["granite"])
        for path, body in (("/cart/search", "q=granite"),
                           ("/account/search", "q=granite"),
                           ("/search", "q=granite&subscribe=1"),
                           ("/search", "q=granite&commentText=hi"),
                           ("/api/vote", "q=granite"),
                           ("/search", json.dumps({"q": "granite", "deleteId": 3})),
                           ("/userLogin", "q=granite"),
                           ("/search/addToCart", "q=granite"),
                           ("/write/search", "q=granite")):
            v = s.classify(SITE, f"https://{SITE}{path}", "POST", "XHR", body, same_site=True)
            self.assertFalse(v.ok, (path, body))
            self.assertIn("state change", v.why)

    def test_graphql_mutations_are_refused(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["granite"])
        url = f"https://{SITE}/graphql"
        ok = json.dumps({"query": "query Search($q: String) { search(q: $q) { id } }",
                         "variables": {"q": "granite"}})
        self.assertEqual(s.classify(SITE, url, "POST", "XHR", ok, same_site=True).kind,
                         W.KIND_POST)
        for body in (json.dumps({"query": "mutation Save($q: String) { save(q: $q) }",
                                 "variables": {"q": "granite"}}),
                     json.dumps({"query": "query A { a } mutation B { b }",
                                 "variables": {"q": "granite"}}),
                     json.dumps({"operationName": "AddToFavorites",
                                 "variables": {"q": "granite"},
                                 "extensions": {"persistedQuery": {"sha256Hash": "ab"}}})):
            v = s.classify(SITE, url, "POST", "XHR", body, same_site=True)
            self.assertFalse(v.ok, body)

    def test_ordinary_words_survive(self):
        for word in ("address", "keywords", "designing", "bloginfo", "category"):
            self.assertEqual(W.state_word(word), "", word)

    def test_uploads_big_bodies_and_credentials_are_refused(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["granite"])
        url = f"https://{SITE}/search"
        self.assertFalse(s.classify(SITE, url, "POST", "XHR", "q=granite",
                                    {"Content-Type": "multipart/form-data; b=1"},
                                    same_site=True).ok)
        self.assertFalse(s.classify(SITE, url, "POST", "XHR", "q=granite&x=" + "a" * 9000,
                                    same_site=True).ok)
        self.assertFalse(s.classify(SITE, url, "POST", "XHR", "q=granite",
                                    {"Authorization": "Bearer abc"}, same_site=True).ok)
        self.assertFalse(s.classify(SITE, url, "POST", "XHR", "", same_site=True).ok)

    def test_other_methods_stay_refused(self):
        s = scope(readonly_post=True)
        for m in ("PUT", "DELETE", "PATCH"):
            self.assertFalse(s.classify(SITE, f"https://{SITE}/search", m, "XHR", "q=x",
                                        same_site=True).ok)

    def test_post_never_widens_a_third_party(self):
        s = scope(readonly_post=True)
        s.note_values(SITE, ["granite"])
        v = s.classify(SITE, "https://search.other.net/search", "POST", "XHR", "q=granite")
        self.assertFalse(v.ok)


class OwnHosts(unittest.TestCase):
    def test_registrable(self):
        self.assertEqual(W.registrable("static.www.example.co.uk"), "example.co.uk")
        self.assertEqual(W.registrable("cdn.pixabay.com"), "pixabay.com")
        self.assertEqual(W.registrable("foo.github.io"), "foo.github.io")
        self.assertEqual(W.registrable("d3njjcbhbojbot.cloudfront.net"),
                         "d3njjcbhbojbot.cloudfront.net")

    def test_same_registrable_domain(self):
        s = scope(own_hosts=True)
        v = s.classify("www.curseforge.com", "https://static-beta.curseforge.com/app.js", "GET",
                       "Script")
        self.assertEqual(v.kind, W.KIND_OWN)
        self.assertEqual(s.site_for("static-beta.curseforge.com"), "www.curseforge.com")

    def test_off_and_non_get(self):
        self.assertFalse(scope().classify("www.curseforge.com",
                                          "https://static-beta.curseforge.com/a.js", "GET",
                                          "Script").ok)
        s = scope(own_hosts=True)
        self.assertFalse(s.classify("www.curseforge.com", "https://api.curseforge.com/x",
                                    "POST", "XHR", "{}").ok)

    def test_shared_hosting_siblings_and_lookalikes_are_not_own(self):
        s = scope(own_hosts=True)
        self.assertFalse(s.classify("alice.github.io", "https://bob.github.io/x.js", "GET",
                                    "Script").ok)
        self.assertFalse(s.classify("www.example.org", "https://example.org.evil.net/x.js",
                                    "GET", "Script").ok)
        self.assertFalse(s.classify("www.example.org", "https://www.example.com/x.js", "GET",
                                    "Script").ok)

    def test_a_country_suffix_with_a_generic_second_level_is_public(self):
        # com.pk, org.ng, net.pl, co.ve are suffixes nobody owns: each shop under them is its own site
        s = scope(own_hosts=True)
        for site, sibling, own in (("www.shop-a.com.pk", "other.com.pk", "cdn.shop-a.com.pk"),
                                   ("tienda.co.ve", "otra.co.ve", "img.tienda.co.ve"),
                                   ("www.sklep.net.pl", "inny.net.pl", "static.sklep.net.pl"),
                                   ("www.duka.org.ng", "another.org.ng", "media.duka.org.ng")):
            self.assertEqual(W.registrable(site), site.split(".", 1)[1] if site.startswith("www.") else site)
            self.assertFalse(s.classify(site, f"https://{sibling}/x.js", "GET", "Script").ok, (site, sibling))
            self.assertEqual(s.classify(site, f"https://{own}/x.js", "GET", "Script").kind, W.KIND_OWN, own)
        # the suffixes the built-in lists already held read as before
        self.assertEqual(W.registrable("static.www.example.co.uk"), "example.co.uk")
        self.assertEqual(W.registrable("a.b.example.com.br"), "example.com.br")
        self.assertEqual(W.registrable("www.example.de"), "example.de")
        self.assertEqual(W.suffix_of("x.example.com.pk"), ("com.pk", "generic"))

    def test_an_unlisted_country_suffix_needs_the_sites_own_evidence(self):
        # .pl and .it have public second levels this build does not list (waw.pl): two hosts under one guessed
        # registrable domain are the site's own only when the site's own document, script or redirect names the host
        s = scope(own_hosts=True)
        self.assertTrue(W.suffix_uncertain("a.waw.pl") and W.suffix_uncertain("www.negozio.it"))
        self.assertFalse(W.suffix_uncertain("www.example.de") or W.suffix_uncertain("www.example.com"))
        self.assertFalse(s.classify("a.waw.pl", "https://b.waw.pl/x.js", "GET", "Script").ok)
        self.assertFalse(s.classify("www.negozio.it", "https://cdn.negozio.it/a.js", "GET", "Script").ok)
        page = ('<link rel="stylesheet" href="https://cdn.negozio.it/s.css">'
                '<script>var api = "https:\\/\\/api.negozio.it\\/v1";</script><a href="https://b.waw.pl/">x</a>')
        s.observe("www.negozio.it", "https://www.negozio.it/", "Document", page)
        for h in ("cdn.negozio.it", "api.negozio.it"):
            v = s.classify("www.negozio.it", f"https://{h}/a.js", "GET", "Script")
            self.assertEqual(v.kind, W.KIND_OWN, h)
            self.assertIn("named by the site's own document", v.detail)
        self.assertFalse(s.classify("www.negozio.it", "https://img.negozio.it/a.png", "GET", "Image").ok)
        # a binding never crosses to another registrable domain
        self.assertNotIn("b.waw.pl", s.bound.get("www.negozio.it", {}))
        s.bind("a.waw.pl", "b.waw.pl", "a redirect of the site's navigation")
        self.assertEqual(s.classify("a.waw.pl", "https://b.waw.pl/x.js", "GET", "Script").kind, W.KIND_OWN)

    def test_a_known_suffix_reads_no_host_names_from_the_sites_sources(self):
        # under a suffix the build can tell, the registrable domain decides alone: the site's documents and scripts
        # are not searched for host names, and its own hosts stay its own
        s = scope(own_hosts=True)
        s.observe("www.example.de", "https://www.example.de/", "Document",
                  '<img src="https://cdn.example.de/a.png"><script>var u = "https://api.example.de/v1";</script>')
        self.assertEqual(s.bound.get("www.example.de", {}), {})
        self.assertEqual(s.classify("www.example.de", "https://img.example.de/a.png", "GET", "Image").kind,
                         W.KIND_OWN)

    def test_a_pinned_public_suffix_list_decides_when_present(self):
        psl = ("// ===BEGIN ICANN DOMAINS===\nit\nroma.it\n*.ck\n!www.ck\ncom.pk\n"
               "// ===BEGIN PRIVATE DOMAINS===\nwebflow.io\n")
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "public_suffix_list.dat")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(psl)
            with open(path + ".sha256", "w", encoding="ascii") as fh:
                fh.write(hashlib.sha256(psl.encode()).hexdigest() + "  public_suffix_list.dat\n")
            with mock.patch.object(W, "PSL_FILE", path), mock.patch.object(W, "PSL_SHA256_FILE", path + ".sha256"), \
                    mock.patch.object(W, "_PSL", None), mock.patch.object(W, "_PSL_STATUS", ""):
                self.assertEqual(W.psl_status(), "loaded")
                self.assertEqual(W.registrable("www.comune.roma.it"), "comune.roma.it")
                self.assertEqual(W.registrable("a.b.foo.ck"), "b.foo.ck")
                self.assertEqual(W.registrable("www.ck"), "www.ck")
                self.assertEqual(W.registrable("one.webflow.io"), "one.webflow.io")
                self.assertEqual(W.registrable("cdn.shop.it"), "shop.it")
                self.assertFalse(W.suffix_uncertain("www.shop.it"))
                s = scope(own_hosts=True)
                self.assertFalse(s.classify("one.webflow.io", "https://two.webflow.io/a.js", "GET", "Script").ok)
                self.assertTrue(s.classify("www.shop.it", "https://cdn.shop.it/a.js", "GET", "Script").ok)
            with open(path + ".sha256", "w", encoding="ascii") as fh:
                fh.write("0" * 64)
            with mock.patch.object(W, "PSL_FILE", path), mock.patch.object(W, "PSL_SHA256_FILE", path + ".sha256"), \
                    mock.patch.object(W, "_PSL", None), mock.patch.object(W, "_PSL_STATUS", ""):
                self.assertIn("does not match", W.psl_status())
                self.assertEqual(W.registrable("www.comune.roma.it"), "roma.it")     # the built-in rules decide
        self.assertEqual(W.psl_status(), "absent")

    def test_tracking_labels_and_listed_own_subdomains_stay_blocked(self):
        s = scope(own_hosts=True, blocklist_domains={"ads.example.org"})
        for h in ("smetrics.example.org", "analytics.example.org", "ads.example.org"):
            self.assertFalse(s.classify("www.example.org", f"https://{h}/b", "GET", "XHR").ok, h)

    def test_declared_host_with_reason(self):
        s = scope(own_hosts=True, declared={"www.coursera.org": [
            {"host": "d3njjcbhbojbot.cloudfront.net",
             "reason": "the site's own script bundle (session capture)"}]})
        v = s.classify("www.coursera.org", "https://d3njjcbhbojbot.cloudfront.net/app.js",
                       "GET", "Script")
        self.assertEqual(v.kind, W.KIND_OWN)
        self.assertIn("declared", v.detail)
        self.assertFalse(s.classify("www.coursera.org",
                                    "https://other.cloudfront.net/app.js", "GET",
                                    "Script").ok)

    def test_declarations_are_checked(self):
        for rows in ([{"host": "*.cloudfront.net", "reason": "a long enough reason"}],
                     [{"host": "cdn.example.net", "reason": ""}],
                     [{"host": "https://cdn.example.net/x", "reason": "a long enough reason"}],
                     ["cdn.example.net"]):
            with self.assertRaises(ValueError, msg=str(rows)):
                W.WebScope(own_hosts=True, declared={"www.example.org": rows})
        with self.assertRaises(ValueError):
            W.WebScope(own_hosts=True, declared={"www.example.org": [
                {"host": "www.google-analytics.com", "reason": "declared as own, wrongly"}]})

    def test_load_declared_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "site_hosts.json")
            with open(p, "w") as fh:
                json.dump({"sites": {"www.coursera.org": [
                    {"host": "d3njjcbhbojbot.cloudfront.net", "reason": "own script bundle"}]}},
                    fh)
            self.assertIn("www.coursera.org", W.load_declared(p))


class ThirdParty(unittest.TestCase):
    def test_block_is_todays_rule(self):
        v = scope().classify(SITE, "https://cdn.jsdelivr.net/npm/x.js", "GET", "Script")
        self.assertFalse(v.ok)
        self.assertTrue(v.why.startswith("third-party host"))

    def test_allow_with_blocklist(self):
        s = scope(third_party="allow_with_blocklist")
        self.assertEqual(s.classify(SITE, "https://cdn.jsdelivr.net/npm/x.js", "GET",
                                    "Script").kind, W.KIND_THIRD)
        for u in ("https://www.google-analytics.com/g/collect", "https://ad.doubleclick.net/x",
                  "https://ib.adnxs.com/ut"):
            v = s.classify(SITE, u, "GET", "Script")
            self.assertFalse(v.ok, u)
            self.assertIn("blocklist", v.why)

    def test_allow_all_still_get_head_only_and_no_challenges(self):
        s = scope(third_party="allow_all")
        self.assertTrue(s.classify(SITE, "https://www.google-analytics.com/g/collect", "GET",
                                   "XHR").ok)
        self.assertFalse(s.classify(SITE, "https://api.other.net/x", "POST", "XHR", "a=1").ok)
        self.assertFalse(s.classify(SITE, "https://challenges.cloudflare.com/turnstile/v0/a.js",
                                    "GET", "Script").ok)
        self.assertFalse(s.classify(SITE, "https://www.google.com/recaptcha/api.js", "GET",
                                    "Script").ok)

    def test_pinned_snapshot_loads_and_matches_its_hash(self):
        bl = W.blocklist()
        self.assertGreater(len(bl), 50000)
        for d in ("google-analytics.com", "googletagmanager.com", "adnxs.com"):
            self.assertTrue(W.blocklisted("www." + d), d)
        self.assertEqual(W.blocklisted("cdn.jsdelivr.net"), "")

    def test_a_tampered_snapshot_is_refused(self):
        with mock.patch.object(W, "BLOCKLIST_SHA256", "0" * 64), \
                mock.patch.object(W, "_BLOCKLIST", None):
            with self.assertRaises(RuntimeError):
                W.blocklist()


class Redaction(unittest.TestCase):
    def test_keys_are_redacted(self):
        t = ("https://x-dsn.algolia.net/1/indexes/*/queries?x-algolia-api-key=SECRET"
             "&x-algolia-application-id=APPID&keyword=granite")
        out = W.redact_keys(t)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("APPID", out)
        self.assertIn("keyword=granite", out)
        self.assertNotIn("tok", W.redact_keys("https://cse.google.com/cse/element/v1?"
                                              "cse_tok=tok&q=x").split("cse_tok=")[1][:3])
        self.assertEqual(W.redact_obj({"a": ["api_key=zzz"]}), {"a": ["api_key=<redacted>"]})
        self.assertIn("<redacted>", W.redact_keys("Authorization: Bearer search-abc123"))


class Settings(unittest.TestCase):
    NAMES = ("ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS", "ANYUSE_ALLOW_READONLY_POST_SEARCH",
             "ANYUSE_ALLOW_SITE_OWN_HOSTS")

    def test_registry_defaults_off_and_security_relevant(self):
        from anyuse import settings as S
        for n in self.NAMES[:2]:
            spec = S.SETTINGS[n]
            self.assertIs(spec.default, False)
            self.assertTrue(spec.security_relevant)
            self.assertTrue(spec.needs_confirmation(True))
            self.assertFalse(spec.needs_confirmation(False))
        # the site's own hosts (same registrable domain) are ON by default - setting it on is
        # no widening (no confirmation), off narrows; still a security setting
        own = S.SETTINGS["ANYUSE_ALLOW_SITE_OWN_HOSTS"]
        self.assertIs(own.default, True)
        self.assertTrue(own.security_relevant)
        self.assertFalse(own.needs_confirmation(True) or own.needs_confirmation(False))
        tp = S.SETTINGS["ANYUSE_THIRD_PARTY_POLICY"]
        self.assertEqual(tp.default, "block")
        self.assertTrue(tp.security_relevant)
        self.assertFalse(tp.needs_confirmation("block"))
        self.assertTrue(tp.needs_confirmation("allow_with_blocklist"))
        self.assertTrue(tp.needs_confirmation("allow_all"))
        with self.assertRaises(S.SettingError):
            tp.parse("allow_some", strict=True)
        self.assertIn("WARNING", S.security_warning("ANYUSE_THIRD_PARTY_POLICY", "allow_all"))
        self.assertEqual(S.security_warning("ANYUSE_THIRD_PARTY_POLICY", "block"), "")

    def test_from_settings(self):
        env = {k: "" for k in self.NAMES}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"ANYUSE_DATA_DIR": d, "ANYUSE_THIRD_PARTY_POLICY": "block"}):
            for k in self.NAMES:
                os.environ.pop(k, None)
            # (the four settings as they were read are the historical benches' reader; the product's
            # scope is ANYUSE_THIRD_PARTY - test_third_party_setting.py)
            self.assertIsNone(W.WebScope.from_legacy_settings())
            os.environ["ANYUSE_ALLOW_SITE_SEARCH_PROVIDERS"] = "on"
            os.environ["ANYUSE_THIRD_PARTY_POLICY"] = "allow_with_blocklist"
            s = W.WebScope.from_legacy_settings()
            self.assertTrue(s.providers and not s.readonly_post and not s.own_hosts)
            self.assertEqual(s.third_party, "allow_with_blocklist")
        del env


if __name__ == "__main__":
    unittest.main()
