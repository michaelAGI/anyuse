"""Discovery v2 (execution/discover.py), offline: chrome-aware item clusters,
search values from item links, and the separate sort proof.

Served from dicts through injected clients — no socket, no browser, no model.
What must hold:

* a navigation bar of root-level section links (about/faq/docs...) never outranks
  the item cluster, and never supplies search or detail values;
* search values are words shared by several item names, with the names as the
  second value set;
* a sort parameter whose values reorder the SAME items is proved by the sort
  proof (proof_kind "sort"); a sort the site ignores, an alphabetical key that is
  not sorted, and a filter that reorders nothing are not;
* the learn-loop proof is unchanged: it still does not prove a pure reorder.
"""
from __future__ import annotations

import os
import unittest
import urllib.parse

from anyuse.execution import discover as D
from anyuse.execution.api_step import ApiResponse

HOST = "shop.example.test"
O = f"https://{HOST}"
ITEMS = ["python-requests", "python-flask", "libxml-dev", "libpng-dev", "rust-serde",
         "rust-tokio", "golang-cobra", "golang-viper"]
NAV = ["about", "faq", "docs", "news", "contact", "privacy", "help"]


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _rows(names):
    return "<table class='results'>" + "".join(
        f"<tr class='row'><td><a href='/pkg/{n}'>{n}</a></td><td>{n} package</td></tr>"
        for n in names) + "</table>"


def site(url, sort_mode="ok"):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    html = {"content-type": "text/html"}
    if p.path == "/":
        nav = "<nav>" + "".join(f"<a href='/{n}'>{n}</a>" for n in NAV) + "</nav>"
        foot = "<footer>" + "".join(f"<a href='/{n}.html'>{n}</a>" for n in NAV) + "</footer>"
        return 200, html, _page("Shop", nav + "<form action='/find'><input name='q'></form>"
                                "<main>" + _rows(ITEMS) + "</main>" + foot)
    if p.path == "/list":
        names = sorted(ITEMS)
        s = q.get("sort", "name")
        if sort_mode == "ok":
            if s == "-name":
                names = names[::-1]
            elif s == "date":
                names = names[3:] + names[:3]
            elif s == "-date":
                names = (names[3:] + names[:3])[::-1]
        elif sort_mode == "unsorted_name":
            names = names[2:] + names[:2]
            if s == "date":
                names = names[::-1]
            elif s == "-name":
                names = names[1:] + names[:1]
            elif s == "-date":
                names = names[4:] + names[:4]
        links = "".join(f"<a href='/list?sort={v}'>{v}</a>"
                        for v in ("name", "-name", "date", "-date"))
        return 200, html, _page("All packages", _rows(names) + links)
    if p.path.startswith("/pkg/"):
        n = p.path.split("/")[-1]
        return 200, html, _page(f"{n} | Shop", f"<h1>{n}</h1>")
    return 404, html, _page("Not found", "")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fetcher(mode="ok"):
    c = Clock()
    return D.HttpFetcher(user_agent="research-probe/0.1 (read-only; 1 req/s)",
                         pacer=D.Pacer(1.0, clock=c, sleeper=c.sleep),
                         client=lambda url, h: site(url, mode))


class Transport:
    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def send(self, method, url, headers, body, timeout_s):
        self.calls.append((method, url))
        st, h, text = site(url, self.mode)
        return ApiResponse(status=st, headers=h, text=text)


class ItemClusters(unittest.TestCase):

    def test_nav_and_footer_links_are_chrome(self):
        _s, _h, body = site(O + "/")
        scan = D.scan_page(body, O + "/")
        by = {urllib.parse.urlsplit(l.url).path: l for l in scan.links}
        self.assertTrue(by["/about"].chrome)
        self.assertTrue(by["/faq.html"].chrome)
        self.assertFalse(by["/pkg/rust-serde"].chrome)
        self.assertTrue(by["/pkg/rust-serde"].main)

    def test_item_cluster_outranks_navigation(self):
        _s, _h, body = site(O + "/")
        scan = D.scan_page(body, O + "/")
        det = D.detail_candidates(D.link_clusters(scan.links, HOST))
        self.assertEqual(det[0].template, O + "/pkg/{v}")
        self.assertFalse(any(D.is_nav_value(v) for d in det for v in d.values))

    def test_search_values_are_shared_item_words_then_names(self):
        f = fetcher()
        hd = D.discover_host(O, f)
        s = hd.caps["search"][0]
        self.assertEqual(s.values, ["python", "dev", "rust", "golang"])
        self.assertEqual(s.alt_values[0], ITEMS[:4])
        self.assertFalse(set(s.values) & set(NAV))

    def test_search_template_is_not_also_detail_or_filter(self):
        body = ("<form action='/'><input name='hoogle'></form><main>" + "".join(
            f"<a href='/?hoogle={n}'>{n}</a>" for n in ("map", "fold", "zip", "take", "drop",
                                                         "filter")) + "</main>")
        c = lambda url, h: (200, {"content-type": "text/html"}, _page("H", body))
        f = D.HttpFetcher(client=c, pacer=D.Pacer(0.0))
        hd = D.discover_host("https://hg.test", f)
        self.assertTrue(hd.caps["search"])
        self.assertFalse([d.template for d in hd.caps["detail"] + hd.caps["filter"]
                          if "hoogle" in d.template])

    def test_default_port_redirect_stays_on_the_host(self):
        seen = []

        def c(url, h):
            seen.append(url)
            if url == "https://pt.test/":
                return 301, {"location": "https://pt.test:443/nb"}, ""
            return 200, {"content-type": "text/html"}, _page("ok", "x")
        r = D.HttpFetcher(client=c, pacer=D.Pacer(0.0)).get("https://pt.test/")
        self.assertEqual(r.status, 200, r.error)
        self.assertEqual(seen, ["https://pt.test/", "https://pt.test/nb"])

    def test_section_prefixed_cluster_ranks_below_items(self):
        links = [D.Link(url=f"https://s.test/about/{n}", text=n) for n in
                 ("badges", "builds", "metadata", "redirections", "rustdoc-json", "download")]
        links += [D.Link(url=f"https://s.test/crate/{n}", text=n) for n in
                  ("serde", "tokio", "rand", "clap", "regex")]
        det = D.detail_candidates(D.link_clusters(links, "s.test"))
        self.assertEqual(det[0].template, "https://s.test/crate/{v}")

    def test_nav_words(self):
        for v in ("about", "faq.html", "zh-tw", "help/", "Categories"):
            self.assertTrue(D.is_nav_value(v), v)
        for v in ("python-requests", "jq", "accessibility-tools", "vim"):
            self.assertFalse(D.is_nav_value(v), v)


class SortProof(unittest.TestCase):

    def setUp(self):
        os.environ["ANYUSE_API_MODALITY"] = "on"

    def cap(self):
        return D.Capability(cap="filter", template=O + "/list?sort={v}", param="sort",
                            values=["name", "-name", "date", "-date"], source="links")

    def test_reorder_of_same_items_is_proved_by_the_sort_proof(self):
        f, tr = fetcher(), Transport()
        out = D.prove_sort(self.cap(), O, f, transport=D.PacedTransport(f, inner=tr))
        self.assertEqual(out.status, "proved", out.reason)
        self.assertEqual(out.proof_kind, "sort")
        self.assertTrue(tr.calls and all(m == "GET" for m, _u in tr.calls))
        self.assertTrue(any("reverse agreement 100%" in c for c in out.proof["checks"]))

    def test_dispatcher_tries_learn_loop_first_then_sort(self):
        f = fetcher()
        out = D.prove(self.cap(), O, f, transport=D.PacedTransport(f, inner=Transport()),
                      proof_preflight=lambda c, r: "")
        self.assertEqual(out.status, "proved")
        self.assertEqual(out.proof_kind, "sort")
        kinds = [a.get("kind", "learn") for a in out.proof["attempts"]]
        self.assertEqual(kinds[0], "learn")
        self.assertEqual(kinds[-1], "sort")

    def test_alphabetical_key_that_is_not_sorted_is_not_proved(self):
        f = fetcher("unsorted_name")
        out = D.prove_sort(self.cap(), O, f,
                           transport=D.PacedTransport(f, inner=Transport("unsorted_name")))
        self.assertEqual(out.status, "discovered")
        self.assertIn("alphabetical", out.reason)

    def test_a_sort_the_site_ignores_is_not_proved(self):
        f = fetcher("ignored")
        out = D.prove_sort(self.cap(), O, f,
                           transport=D.PacedTransport(f, inner=Transport("ignored")))
        self.assertEqual(out.status, "discovered")
        self.assertIn("different order", out.reason)

    def test_a_filter_that_is_no_sort_is_not_proved_by_the_sort_proof(self):
        # the sort proof is tried for every filter whose learn proof failed; one that reorders nothing keeps the
        # learn proof's verdict
        c = D.Capability(cap="filter", template=O + "/list?arch={v}", param="arch",
                         values=["x86", "arm", "riscv"])
        self.assertFalse(D.is_sort(c))
        self.assertTrue(D.is_sort(self.cap()))
        f = fetcher()
        out = D.prove(c, O, f, transport=D.PacedTransport(f, inner=Transport()),
                      proof_preflight=lambda c_, r: "")
        self.assertNotEqual(out.proof_kind, "sort")
        self.assertNotEqual(out.status, "proved")

    def test_extractor_is_fitted_without_the_validation_values(self):
        f, tr = fetcher(), Transport()
        seen = []
        orig = f.get
        f.get = lambda url, *a: (seen.append(url), orig(url, *a))[1]
        out = D.prove_sort(self.cap(), O, f, transport=D.PacedTransport(f, inner=tr))
        self.assertEqual(out.status, "proved", out.reason)
        self.assertEqual(out.proof["fit_values"], ["name", "-name"])
        self.assertEqual(out.proof["validation_values"], ["date", "-date"])
        # the compiled requests are for the validation values only
        self.assertEqual([u.split("=")[-1] for _m, u in tr.calls], ["date", "-date"])

    def test_every_sort_request_passes_the_gate(self):
        f, tr = fetcher(), Transport()
        out = D.prove_sort(self.cap(), O, f, transport=D.PacedTransport(f, inner=tr),
                           proof_preflight=lambda c, r: "proof permission refused " + c.template.url)
        self.assertEqual(out.status, "refused")
        self.assertEqual(tr.calls, [])

    def test_sort_fallback_is_gated_too(self):
        f, tr = fetcher(), Transport()
        out = D.prove(self.cap(), O, f, transport=D.PacedTransport(f, inner=tr),
                      proof_preflight=lambda c, r: "proof permission refused x")
        self.assertEqual(out.status, "refused")
        self.assertEqual(tr.calls, [])

    def test_no_sort_fallback_after_a_gate_refusal(self):
        orig = D.prove_capability

        def refused(c, *a, **k):
            c.status, c.found, c.reason = "refused", True, "proof permission refused x"
            return c
        D.prove_capability = refused
        try:
            f, tr = fetcher(), Transport()
            out = D.prove(self.cap(), O, f, transport=D.PacedTransport(f, inner=tr))
        finally:
            D.prove_capability = orig
        self.assertEqual(out.status, "refused")
        self.assertFalse(any(a.get("kind") == "sort" for a in out.proof["attempts"]))
        self.assertEqual(tr.calls, [])

    def test_too_few_sort_values_is_refused(self):
        c = self.cap()
        c.values = c.values[:3]
        out = D.prove_sort(c, O, fetcher(), transport=D.PacedTransport(fetcher(), inner=Transport()))
        self.assertEqual(out.status, "refused")
        self.assertIn("2 validation", out.reason)


class OneList(unittest.TestCase):

    def test_a_long_result_list_is_one_list_but_a_bare_tag_is_not(self):
        from anyuse.execution.html_extract import selector_is_one_list, selector_is_specific
        rows = "".join(f"<li class='r'><a href='/p/{i}'>item {i}</a></li>" for i in range(200))
        page = f"<nav><a href='/a'>a</a><a href='/b'>b</a></nav><ul class='res'>{rows}</ul>"
        self.assertFalse(selector_is_specific(page, "ul.res li.r", 60))
        self.assertTrue(selector_is_one_list(page, "ul.res li.r"))
        self.assertFalse(selector_is_one_list(page, "a"))
        self.assertFalse(selector_is_one_list(page, "li"))

    def test_row_numbers_do_not_break_the_same_item_set(self):
        a = "<table class='t'>" + "".join(
            f"<tr class='x'><td>{i}</td><td>{n}</td></tr>" for i, n in enumerate(["pa", "qb", "rc", "sd", "te"])) + "</table>"
        self.assertEqual(D._items_in(a, "table.t tr.x"), ["pa", "qb", "rc", "sd", "te"])


class SelectionIsNotValidation(unittest.TestCase):
    """compile_and_verify: runner-ups are chosen on selection inputs only."""

    def _run(self, selection):
        from anyuse.execution import api_verify as V
        from anyuse.execution.api_compile import CompileResult
        from anyuse.execution.api_step import RequestTemplate
        top = RequestTemplate(method="GET", url="https://h.test/a?q={q}", params=["q"])
        alt = RequestTemplate(method="GET", url="https://h.test/b?q={q}", params=["q"])
        calls = []

        def fake_compile(rec, **kw):
            r = CompileResult(param_name="q", param_value="v0")
            r.template, r.alternatives = top, [alt]
            return r

        def fake_verify(t, cases, executor=None, min_cases=2, **kw):
            calls.append((t.url.split("?")[0][-2:], [c["value"] for c in cases]))
            out = V.EquivalenceResult()
            out.passed = t is alt
            return out
        rec = lambda v: {"param": {"name": "q", "value": v}, "ui": {"items": ["a b c"]}}
        o1, o2 = V.compile_recording, V.verify_template
        V.compile_recording, V.verify_template = fake_compile, fake_verify
        try:
            compiled, proof = V.compile_and_verify(rec("v0"), [rec("v2"), rec("v3")],
                                                   selection_recordings=selection(rec))
        finally:
            V.compile_recording, V.verify_template = o1, o2
        return compiled, proof, calls

    def test_without_selection_input_no_runner_up_is_tried(self):
        compiled, proof, calls = self._run(lambda rec: [])
        self.assertFalse(proof.passed)
        self.assertEqual(calls, [("/a", ["v2", "v3"])])
        self.assertIn("not tried", compiled.reason)

    def test_runner_up_is_chosen_on_selection_then_validated_once(self):
        compiled, proof, calls = self._run(lambda rec: [rec("v1")])
        self.assertTrue(proof.passed)
        self.assertEqual(calls, [("/a", ["v1"]), ("/b", ["v1"]), ("/b", ["v2", "v3"])])
        self.assertTrue(compiled.template.url.endswith("/b?q={q}"))


if __name__ == "__main__":
    unittest.main()
