"""Discovery v3, offline: fake sites from dicts,
no socket, no browser, no model.

* detail pages keyed by a code in the URL whose title names the item, not the
  code, prove when a heading names the site's own link text; a heading that
  does not match still fails; the default landing check is unchanged;
* the typed-search probe's document URL and XHR URLs become search templates,
  used only when no form or OpenSearch was read;
* common search patterns are candidates only, robots-filtered, never proved
  without the proof;
* search values fall back to the site's frequent content words;
* the metric's signals (has_search, has_items) follow their fixed definitions.
"""
from __future__ import annotations

import os
import unittest
import urllib.parse

from anyuse.execution import discover as D
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

HOST = "codes.example.test"
O = f"https://{HOST}"
ITEMS = [("KAAA", "Alder Field"), ("KBBB", "Birch Harbour"), ("KCCC", "Cedar Point"),
         ("KDDD", "Dogwood Plains"), ("KEEE", "Elm Crossing"), ("KFFF", "Fir Valley")]


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def make_site(heading_matches=True, search_box=True):
    def site(url):
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        html = {"content-type": "text/html; charset=utf-8"}
        if p.path == "/robots.txt":
            return 200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /search/\n"
        if p.path == "/":
            links = "".join(f"<li><a href='/place/{c}'>{n}</a></li>" for c, n in ITEMS)
            box = ("<div role='search'><input type='text' placeholder='Find a place'></div>"
                   if search_box else "")
            return 200, html, _page("Places", f"{box}<main><h2>Harbour places</h2>"
                                               f"<ul>{links}</ul></main>")
        if p.path.startswith("/place/"):
            code = p.path.split("/")[-1]
            name = dict(ITEMS).get(code)
            if not name:
                return 404, html, _page("Not found", "no")
            head = name if heading_matches else "Place record"
            facts = "".join(f"<li class='fact'>{name} {k}</li>" for k in
                            ("elevation 12", "runway north", "operator city", "region coast"))
            return 200, html, _page(f"{name} | Places", f"<h1>{head}</h1><ul>{facts}</ul>")
        if p.path == "/find":
            term = q.get("term", "")
            rows = "".join(f"<li class='hit'><a href='/place/{c}'>{n} {term}</a></li>"
                           for c, n in ITEMS if term)
            return 200, html, _page("Find", f"<ul class='hits'>{rows}</ul>")
        return 404, html, _page("Not found", "nothing")
    return site


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fetcher(site, allow=lambda u: "/search/" not in urllib.parse.urlsplit(u).path):
    clock = Clock()
    calls = []

    def c(url, headers):
        calls.append(url)
        return site(url)
    f = D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)", allow_url=allow,
                      pacer=D.Pacer(1.0, clock=clock, sleeper=clock.sleep), client=c)
    f.calls = calls
    return f


class Transport:
    def __init__(self, site):
        self.site = site
        self.calls = []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        st, h, text = self.site(url)
        return ApiResponse(status=st, headers=h, text=text)


def prove(site, cap):
    os.environ["ANYUSE_API_MODALITY"] = "on"
    f = fetcher(site)
    ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=Transport(site)))
    return D.prove_capability(cap, O, f, executor=ex, proof_preflight=lambda c, r: "")


class DetailLabel(unittest.TestCase):

    def test_code_keyed_detail_with_heading_naming_the_link_text_is_proved(self):
        site = make_site()
        hd = D.discover_host(O, fetcher(site), patterns=False)
        det = hd.caps["detail"][0]
        self.assertEqual(det.template, O + "/place/{v}")
        self.assertEqual(det.labels["KAAA"], "Alder Field")
        out = prove(site, det)
        self.assertEqual(out.status, "proved", (out.reason, out.proof))
        self.assertIn("heading names", out.proof["confirm"])

    def test_heading_that_does_not_match_the_link_text_still_fails(self):
        site = make_site(heading_matches=False)
        det = D.discover_host(O, fetcher(site), patterns=False).caps["detail"][0]
        out = prove(site, det)
        self.assertNotEqual(out.status, "proved")
        self.assertIn("does not name", out.reason)

    def test_without_labels_the_v2_title_rule_is_unchanged(self):
        site = make_site()
        det = D.discover_host(O, fetcher(site), patterns=False).caps["detail"][0]
        det.labels = {}
        out = prove(site, det)
        self.assertNotEqual(out.status, "proved")

    def test_label_and_key_checks(self):
        self.assertTrue(D.label_matches("Alder Field", ["Alder Field"]))
        self.assertTrue(D.label_matches("Alder Field", ["Alder Field Airport (KAAA)"]))
        self.assertFalse(D.label_matches("Alder Field", ["Birch Harbour"]))
        self.assertFalse(D.label_matches("ab", ["ab"]))
        ok, _ = D.detail_label_ok("<h1>Alder Field</h1>", O + "/place/KBBB", "KAAA", "Alder Field")
        self.assertFalse(ok)                               # key not in the URL
        ok, _ = D.detail_label_ok("<h1>Alder Field</h1>", O + "/place/KAAA", "KAAA", "Alder Field")
        self.assertTrue(ok)

    def test_default_landing_check_is_the_learn_loop_one(self):
        from anyuse.execution import learn_loop as L
        import inspect
        src = inspect.getsource(L.usable_runs)
        self.assertIn("check = check or landing_check", src)


class SearchFinding(unittest.TestCase):

    def test_typed_probe_document_and_xhr_become_templates(self):
        site = make_site()
        seen = []

        def probe(url, value):
            seen.append((url, value))
            return {"document": f"{O}/find?term={urllib.parse.quote_plus(value)}&lang=en",
                    "xhr": [f"{O}/api/suggest?q={urllib.parse.quote_plus(value)}",
                            f"https://cdn.other.test/s?q={value}"]}
        hd = D.discover_host(O, fetcher(site), search_probe=probe)
        self.assertEqual(len(seen), 1)
        srcs = {c.source: c.template for c in hd.caps["search"]}
        self.assertEqual(srcs["typed"], O + "/find?term={v}&lang=en")
        self.assertEqual(srcs["xhr"], O + "/api/suggest?q={v}")
        self.assertNotIn("pattern", srcs)
        self.assertEqual(hd.caps["search"][0].source, "typed")
        self.assertEqual(len(hd.caps["search"][0].values), D.N_VALUES)

    def test_probe_is_not_used_when_a_form_exists(self):
        body = ("<form action='/s' method='get'><input type='search' name='q'></form>"
                + "".join(f"<a href='/place/{c}'>{n}</a>" for c, n in ITEMS))

        def site(url):
            if urllib.parse.urlsplit(url).path == "/":
                return 200, {"content-type": "text/html"}, _page("P", body)
            return 404, {"content-type": "text/html"}, _page("NF", "x")
        called = []
        D.discover_host(O, fetcher(site), search_probe=lambda u, v: called.append(u) or {})
        self.assertEqual(called, [])

    def test_patterns_are_candidates_only_and_robots_filtered(self):
        site = make_site(search_box=False)
        hd = D.discover_host(O, fetcher(site))
        pats = [c for c in hd.caps["search"] if c.source == "pattern"]
        self.assertTrue(pats)
        self.assertNotIn(O + "/search/?q={v}", [c.template for c in pats])   # robots
        out = prove(site, pats[0])                       # 404 on this site
        self.assertNotEqual(out.status, "proved")

    def test_content_word_fallback_values(self):
        sc = D.scan_page("<h1>River maps</h1><h2>River boats</h2><main>"
                         "<a href='/a'>Coastal maps archive</a><a href='/b'>Coastal boats</a>"
                         "<a href='/c'>Harbour charts</a><a href='/d'>Harbour river</a></main>",
                         O + "/")
        vals = D.content_word_values([sc])
        self.assertEqual(vals[0], "river")
        self.assertTrue(set(vals) <= {"river", "maps", "boats", "coastal", "harbour", "charts",
                                      "archive"})


class ValueRetry(unittest.TestCase):

    def test_a_value_that_lands_elsewhere_is_replaced_once(self):
        """A search that redirects its one exact match to the item page: that
        value is swapped for the next site value; the proof is unchanged."""
        base = make_site()

        def site(url):
            p = urllib.parse.urlsplit(url)
            q = dict(urllib.parse.parse_qsl(p.query))
            if p.path == "/find" and q.get("term") == "exact":
                return base(O + "/place/KAAA")[0], {"content-type": "text/html"}, \
                    base(O + "/place/KAAA")[2].replace("<title>", "<title>Exact ")
            return base(url)
        cap = D.Capability("search", template=O + "/find?term={v}", param="term",
                           values=["alder", "birch", "exact", "cedar"],
                           alt_values=[["dogwood", "elm", "fir", "cedar"]])
        os.environ["ANYUSE_API_MODALITY"] = "on"
        f = fetcher(site)
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=Transport(site)))
        D.prove(cap, O, f, executor=ex, proof_preflight=lambda c, r: "")
        vals = [a["values"] for a in cap.proof["attempts"]]
        self.assertIn(["alder", "birch", "cedar", "dogwood"], vals)
        self.assertEqual(cap.status, "proved", cap.reason)


class V31(unittest.TestCase):

    def test_protocol_relative_opensearch_template_is_resolved(self):
        xml = ('<OpenSearchDescription><Url type="text/html" '
               'template="//codes.example.test/browse?q={searchTerms}"/></OpenSearchDescription>')
        caps = D.parse_opensearch(xml, O + "/os.xml")
        self.assertEqual(caps[0].template, O + "/browse?q={v}")

    def test_search_names_drop_file_extensions(self):
        links = "".join(f"<a href='/docs/class_{n}.html'>class {n}</a>"
                        for n in ("node", "button", "label", "timer", "camera"))
        sc = D.scan_page(links, O + "/")
        cl = D.link_clusters(sc.links, HOST)
        det = D.detail_candidates(cl)
        sets = D.search_value_sets(det[0], cl)
        self.assertTrue(all(not v.endswith(".html") for vs in sets for v in vs), sets)

    def test_a_proof_that_raises_fails_the_candidate_not_the_host(self):
        cap = D.Capability("search", template=O + "/find?term={v}", param="term",
                           values=["alder", "birch", "cedar", "dogwood"])

        def boom(*a, **k):
            raise ValueError("deep html")
        orig = D.prove_capability
        D.prove_capability = boom
        try:
            D.prove(cap, O, fetcher(make_site()))
        finally:
            D.prove_capability = orig
        self.assertEqual(cap.status, "discovered")
        self.assertIn("proof raised ValueError", cap.reason)


class Signals(unittest.TestCase):

    def test_has_search_raw_and_rendered(self):
        s = D.scan_page("<form><input name='user'><input type='password' name='pw'></form>", O)
        self.assertFalse(D.has_site_search(s))
        s = D.scan_page("<div role='search'><input type='text'></div>", O)
        self.assertTrue(D.has_site_search(s))
        s = D.scan_page("<input type='text' placeholder='Search the catalogue'>", O)
        self.assertTrue(D.has_site_search(s))
        s = D.scan_page("<input type='text' name='email'>", O)
        self.assertFalse(D.has_site_search(s))
        s = D.scan_page("<link rel='search' type='application/opensearchdescription+xml' "
                        "href='/os.xml'>", O)
        self.assertTrue(D.has_site_search(s))
        s = D.scan_page("<input type='search' name='q'>", O)
        self.assertFalse(D.has_site_search(s, visible_inputs=[{"visible": False}]))
        self.assertTrue(D.has_site_search(s, visible_inputs=[{"visible": True}]))

    def test_has_items(self):
        items = "".join(f"<a href='/place/{c}'>{n}</a>" for c, n in ITEMS)
        self.assertTrue(D.has_item_links([D.scan_page(items, O + "/")], HOST))
        nav = "<nav>" + items + "</nav>"
        self.assertFalse(D.has_item_links([D.scan_page(nav, O + "/")], HOST))
        words = "".join(f"<a href='/{w}'>{w}</a>" for w in ("about", "faq", "help", "news",
                                                              "blog", "contact"))
        self.assertFalse(D.has_item_links([D.scan_page(words, O + "/")], HOST))

    def test_signals_on_the_host_walk(self):
        hd = D.discover_host(O, fetcher(make_site()), patterns=False)
        self.assertEqual(hd.signals, {"reachable": True, "has_search": True, "has_items": True,
                                      "rendered": False})

    def test_no_host_names_in_the_discovery_code(self):
        import re
        src = open(D.__file__).read()
        hosts = re.findall(r"[\"'](?:https?://)?(?:[a-z0-9-]+\.)+(?:org|com|net|io|dev|gov|edu|"
                           r"uk|de|fr|ch|nl|pl|br|ca)\b", src)
        self.assertEqual(hosts, [])


if __name__ == "__main__":
    unittest.main()
