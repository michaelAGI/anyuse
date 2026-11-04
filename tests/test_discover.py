"""Deterministic discovery (execution/discover.py), offline.

A fake package site is served from a dict through injected clients — no socket,
no browser, no model. What must hold:

* every reader finds its capability from markup alone: GET search form,
  OpenSearch, link-template cluster, page links, <select> and query-link filters,
  sitemap from robots.txt;
* proof values come from the site's own pages;
* the unchanged learn loop proves search and detail on the fake site (tier 1);
* the fetcher is GET-only, robots-gated, paced (>= 1 s per host, measured on a
  fake clock), stops a host on a block page and never re-hits it;
* a ranker may only reorder; a non-permutation is ignored.
"""
from __future__ import annotations

import os
import unittest
import urllib.parse

from anyuse.execution import discover as D
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

HOST = "pkgs.example.test"
O = f"https://{HOST}"
NAMES = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _items(names):
    return "<ul class='results'>" + "".join(
        f"<li class='row'><a href='/package/{n}'>{n}</a> <span>{n} tool v1</span></li>"
        for n in names) + "</ul>"


def site(url: str):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    html = {"content-type": "text/html; charset=utf-8"}
    if p.path == "/robots.txt":
        return 200, {"content-type": "text/plain"}, "User-agent: *\nDisallow: /private\n"
    if p.path == "/":
        links = "".join(f"<a href='/package/{n}'>{n}</a>" for n in NAMES)
        return 200, html, _page("Fake packages", (
            "<link rel='search' type='application/opensearchdescription+xml' href='/os.xml'>"
            "<form action='/search' method='get' role='search'>"
            "<input type='search' name='q'><input type='hidden' name='s' value='all'>"
            "<input type='submit' value='Go'></form>"
            "<form action='/login' method='post'><input name='user'>"
            "<input type='password' name='pw'></form>"
            f"<nav><a href='/packages'>Browse</a></nav>{links}"))
    if p.path == "/os.xml":
        return 200, {"content-type": "application/opensearchdescription+xml"}, (
            "<OpenSearchDescription><Url type='text/html' "
            "template='https://pkgs.example.test/search?q={searchTerms}&amp;s=all&amp;"
            "page={startPage?}'/></OpenSearchDescription>".replace("'", '"'))
    if p.path == "/packages":
        page = int(q.get("page", "1"))
        sort = q.get("sort", "name")
        names = [f"{n}{page}" for n in NAMES]
        if sort == "date":
            names = names[::-1]
        elif sort == "size":
            names = names[2:] + names[:2]
        elif sort == "downloads":
            names = names[4:] + names[:4]
        pag = "".join(f"<a href='/packages?page={i}'>{i}</a>" for i in range(1, 6) if i != page)
        sorts = "".join(f"<a href='/packages?sort={s}'>{s}</a>"
                        for s in ("name", "date", "size", "downloads"))
        return 200, html, _page(f"All packages page {page}", _items(names) + pag + sorts)
    if p.path == "/search":
        term = q.get("q", "")
        names = [f"{term}", f"{term}-dev", f"{term}-doc", f"lib{term}", f"{term}-extra"]
        if q.get("arch", "any") != "any":
            names = [f"{n}-{q['arch']}" for n in names]
        form = ("<form action='/search' method='get'><input name='q' value='" + term + "'>"
                "<select name='arch'><option value='any' selected>any</option>"
                "<option value='x86'>x86</option><option value='arm'>arm</option>"
                "<option value='riscv'>riscv</option></select></form>")
        return 200, html, _page("Search results", form + _items(names))
    if p.path.startswith("/package/"):
        n = p.path.split("/")[-1]
        facts = "".join(f"<li class='fact'>{n} {k}</li>" for k in
                        ("version 1.0", "license MIT", "maintainer team", "depends libc"))
        return 200, html, _page(f"{n} - Fake packages", f"<h1>{n}</h1><ul>{facts}</ul>")
    return 404, html, _page("Not found", "nothing")


class FakeTransport:
    def __init__(self):
        self.calls = []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        st, h, text = site(url)
        return ApiResponse(status=st, headers=h, text=text)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fetcher(client=None, allow=lambda u: "/private" not in u):
    clock = Clock()
    calls = []

    def c(url, headers):
        calls.append((url, headers["User-Agent"]))
        return (client or site)(url)
    f = D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)", allow_url=allow,
                      pacer=D.Pacer(1.0, clock=clock, sleeper=clock.sleep), client=c)
    f.calls, f.clock = calls, clock
    return f


class Readers(unittest.TestCase):

    def test_search_form_picks_get_form_and_skips_password_form(self):
        _st, _h, body = site(O + "/")
        caps = D.search_from_forms(D.scan_page(body, O + "/"))
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].template, O + "/search?q={v}&s=all")
        self.assertEqual(caps[0].render("a b"), O + "/search?q=a+b&s=all")

    def test_opensearch_template(self):
        _st, _h, xml = site(O + "/os.xml")
        caps = D.parse_opensearch(xml)
        self.assertEqual(caps[0].template, O + "/search?q={v}&s=all")
        self.assertEqual(caps[0].param, "q")

    def test_link_cluster_gives_detail_with_site_values(self):
        _st, _h, body = site(O + "/")
        scan = D.scan_page(body, O + "/")
        det = D.detail_candidates(D.link_clusters(scan.links, HOST))
        self.assertEqual(det[0].template, O + "/package/{v}")
        self.assertEqual(det[0].values, NAMES[:4])

    def test_pagination_and_filters_from_links_and_select(self):
        _st, _h, body = site(O + "/packages")
        scan = D.scan_page(body, O + "/packages")
        nxt = D.pagination_candidates(scan)
        self.assertEqual(nxt[0].template, O + "/packages?page={v}")
        self.assertEqual(nxt[0].values, ["2", "3", "4", "5"])
        flt = D.filter_candidates(scan)
        self.assertEqual(flt[0].param, "sort")
        self.assertEqual(sorted(flt[0].values), ["date", "downloads", "name", "size"])
        _st, _h, body = site(O + "/search?q=vim")
        sel = D.filter_candidates(D.scan_page(body, O + "/search?q=vim"))
        self.assertEqual(sel[0].template, O + "/search?q=vim&arch={v}")
        self.assertEqual(sel[0].values, ["x86", "arm", "riscv", "any"])

    def test_listing_links_skip_account_pages(self):
        body = ("<a href='/account/register.php'>New user</a><a href='/login'>Browse</a>"
                "<a href='/packages'>Browse</a>")
        self.assertEqual(D.listing_links(D.scan_page(body, O + "/"), None), [O + "/packages"])

    def test_sitemap_from_robots(self):
        self.assertEqual(D.sitemaps_from_robots("User-agent: *\nSitemap: https://h/s.xml\n"),
                         ["https://h/s.xml"])
        urls, kids = D.parse_sitemap("<urlset><url><loc>https://h/a</loc></url></urlset>")
        self.assertEqual((urls, kids), (["https://h/a"], []))
        urls, kids = D.parse_sitemap("<sitemapindex><sitemap><loc>https://h/1.xml</loc>"
                                     "</sitemap></sitemapindex>")
        self.assertEqual((urls, kids), ([], ["https://h/1.xml"]))


class Fetching(unittest.TestCase):

    def test_paced_robots_gated_neutral_ua(self):
        f = fetcher()
        f.get(O + "/")
        f.get(O + "/packages")
        r = f.get(O + "/private/x")
        self.assertIn("robots_refused", r.error)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(f.clock.sleeps, [1.0])
        self.assertTrue(all("anyuse" not in ua for _u, ua in f.calls))

    def test_block_page_stops_host_for_good(self):
        f = fetcher(client=lambda url: (429, {"retry-after": "60"}, "slow down"))
        r = f.get(O + "/")
        self.assertIn("blocked", r.error)
        r2 = f.get(O + "/packages")
        self.assertIn("not re-hit", r2.error)
        self.assertEqual(len(f.calls), 1)

    def test_offhost_redirect_is_not_followed(self):
        f = fetcher(client=lambda url: (302, {"location": "https://elsewhere.test/"}, ""))
        r = f.get(O + "/")
        self.assertIn("redirect off the host", r.error)
        self.assertEqual(len(f.calls), 1)


class Walk(unittest.TestCase):

    def test_discover_host_finds_all_four(self):
        f = fetcher()
        hd = D.discover_host(O, f, robots_txt="")
        self.assertEqual(hd.caps["search"][0].source, "opensearch")
        self.assertEqual(hd.caps["search"][0].values, NAMES[:4])
        self.assertEqual(hd.caps["detail"][0].template, O + "/package/{v}")
        self.assertTrue(hd.caps["next"])
        self.assertTrue(hd.caps["filter"])
        self.assertFalse(any(u.startswith(O + "/login") for u, _ua in f.calls))

    def test_ranker_only_reorders(self):
        f = fetcher()
        hd = D.discover_host(O, f, ranker=lambda kind, host, items: ([0] * len(items), 0.001))
        self.assertTrue(all(c.ranked_by == "rule" for c in hd.caps["search"]))
        self.assertTrue(any("not a permutation" in n for n in hd.notes))
        f = fetcher()
        rule = D.discover_host(O, f).caps["filter"]
        self.assertGreaterEqual(len(rule), 2)
        f = fetcher()
        hd = D.discover_host(O, f, ranker=lambda kind, host, items:
                             (list(range(len(items)))[::-1], 0.001))
        self.assertEqual([c.template for c in hd.caps["filter"]],
                         [c.template for c in rule][::-1])
        self.assertEqual(hd.caps["filter"][0].ranked_by, "model")
        self.assertEqual(len(hd.caps["detail"]), len(D.discover_host(O, fetcher()).caps["detail"]))


class Proof(unittest.TestCase):

    def setUp(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"

    def _prove(self, cap):
        f = fetcher()
        tr = FakeTransport()
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=tr))
        out = D.prove_capability(cap, O, f, executor=ex, proof_preflight=lambda c, r: "")
        return out, tr, f

    def test_four_capabilities_prove_tier1(self):
        f = fetcher()
        hd = D.discover_host(O, f)
        by = {"search": hd.caps["search"][0], "detail": hd.caps["detail"][0],
              "next": hd.caps["next"][0],
              "filter": next(c for c in hd.caps["filter"] if c.param == "arch")}
        for kind, cap in by.items():
            cap, tr, f2 = self._prove(cap)
            self.assertEqual(cap.status, "proved", (kind, cap.reason, cap.proof))
            self.assertTrue(tr.calls and all(m == "GET" for m, _u in tr.calls))
            self.assertTrue(all(b >= 1.0 for b in f2.clock.sleeps))

    def test_a_sort_that_only_reorders_is_not_proved(self):
        hd = D.discover_host(O, fetcher())
        cap = next(c for c in hd.caps["filter"] if c.param == "sort")
        out, _tr, _f = self._prove(cap)
        self.assertEqual(out.status, "discovered")
        self.assertIn("no repeated group", out.reason)

    def test_too_few_site_values_is_refused_not_invented(self):
        cap = D.Capability(cap="search", template=O + "/search?q={v}", values=["alpha"])
        out, tr, f = self._prove(cap)
        self.assertEqual(out.status, "refused")
        self.assertIn("none is invented", out.reason)
        self.assertEqual(f.calls, [])

    def test_gate_refusal_is_refused(self):
        f = fetcher()
        hd = D.discover_host(O, f)
        cap = hd.caps["detail"][0]
        f = fetcher()
        ex = ApiStepExecutor(transport=D.PacedTransport(f, inner=FakeTransport()))
        out = D.prove_capability(cap, O, f, executor=ex,
                                 proof_preflight=lambda c, r: "proof permission refused x")
        self.assertEqual(out.status, "refused")

    def test_paced_transport_refuses_writes(self):
        f = fetcher()
        with self.assertRaises(PermissionError):
            D.PacedTransport(f, inner=FakeTransport()).send("POST", O + "/", {}, "", 5)


if __name__ == "__main__":
    unittest.main()
