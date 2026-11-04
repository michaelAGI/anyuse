"""The compile levers: repetition selectors, evidence-based volatile
parameters, and the read-only POST rung.

Every fixture here is synthetic and offline. Nothing in this file opens a
socket, touches the desktop or reads the user's data; it runs under the guarded
runner like the rest of the suite.

Read this file as the specification of what the read-only POST rung REFUSES —
that is most of it, and it is the part that matters.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

from anyuse.execution.api_compile import (
    Levers, compile_html, compile_readonly_post, compile_recording,
    readonly_post_candidates, visible_items, volatile_evidence, urlify)
from anyuse.execution.api_step import (
    RequestTemplate, api_hil_action, readonly_post_operation, readonly_post_shape,
    signed_param_reason)
from anyuse.execution.api_permission import acknowledge_host
from anyuse.execution.blocked import detect_blocked_ui
from anyuse.execution.html_extract import (
    derive_list_items, derive_selector, derive_selector_repeated,
    repeated_groups, parse)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

# The shape that collapsed the value-first derivation to a bare ``a`` on sr.ht
# and nuget.org, reproduced faithfully: the on-screen labels come from MORE THAN
# ONE kind of element inside a row (an owner link, a project link, a tag), so the
# chains of the matched nodes share nothing but ``a`` — and the page, like every
# real page, carries far more than sixty anchors, so that bare ``a`` is then
# correctly rejected as unspecific and nothing compiles at all.
_CHROME = "".join(f'<a href="/nav{i}">Nav {i}</a>' for i in range(70))


def _rows_page(rows, sidebar):
    body = "".join(
        f'<div class="event"><h4><a href="/{u}">~{u}</a>/'
        f'<a href="/{u}/{p}">{p}</a></h4><p>{d}</p>'
        f'<div class="tags"><a class="tag" href="/t?q=%23{t}">#{t}</a></div></div>'
        for u, p, d, t in rows)
    # A second list in a sidebar, using a DIFFERENT heading tag. This is what
    # breaks the value-first derivation: the labels the recorder saw live under
    # both `h4 a` and `h5 a`, the shared suffix of those chains is just `a`, and
    # a bare `a` on a page with this many anchors is rejected.
    aside = "".join(f'<div class="event-list"><h5><a href="/s/{s}">{s}</a></h5></div>'
                    for s in sidebar)
    return (f"<html><body><nav>{_CHROME}</nav>"
            f'<div class="event-list">{body}</div>'
            f"<aside>{aside}</aside></body></html>")


CLASSLESS_ROWS = _rows_page([
    ("ann", "alpha", "first thing", "rust"),
    ("bob", "beta", "second thing", "go"),
    ("cid", "gamma", "third thing", "c"),
    ("dee", "delta", "fourth thing", "zig"),
], ["mmre", "small-projects"])

#: The same page for a different query — used for ground-truth derivation.
CLASSLESS_ROWS_OTHER = _rows_page([
    ("eve", "epsilon", "fifth", "rust"),
    ("fay", "zeta", "sixth", "go"),
    ("gus", "eta", "seventh", "c"),
    ("hal", "theta", "eighth", "zig"),
], ["mmre", "small-projects"])

ITEMS = ["~ann", "alpha", "#rust", "~bob", "beta", "#go",
         "~cid", "gamma", "#c", "~dee", "delta", "#zig", "mmre"]


def html_recording(body=CLASSLESS_ROWS, url="https://ex.test/search?q=pkg"):
    return {
        "param": {"name": "q", "value": "pkg"},
        "ui": {"items": list(ITEMS), "text": " ".join(ITEMS)},
        "final_url": url,
        "network": [{"url": url, "method": "GET", "status": 200,
                     "mime": "text/html", "body": body, "bytes": len(body)}],
    }


GQL_BODY = json.dumps({"operationName": "Search",
                       "query": "query Search($q: String!) { search(q: $q) { name } }",
                       "variables": {"q": "pkg"}})

POST_ITEMS = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg", "Delta Pkg"]

GQL_RESPONSE = json.dumps({"data": {"search": [{"name": n} for n in POST_ITEMS]}})


def post_recording(post_data=GQL_BODY, url="https://ex.test/graphql",
                   headers=None, body=GQL_RESPONSE, occurrences=1):
    net = [{"url": url, "method": "POST", "status": 200,
            "mime": "application/json", "post_data": post_data,
            "headers": dict(headers or {"content-type": "application/json"}),
            "body": body, "bytes": len(body)}
           for _ in range(occurrences)]
    return {
        "param": {"name": "q", "value": "pkg"},
        "ui": {"items": list(POST_ITEMS), "text": " ".join(POST_ITEMS)},
        "final_url": "https://ex.test/search",
        "network": net,
    }


# ---------------------------------------------------------------------------
# lever (a) — repetition
# ---------------------------------------------------------------------------

class RepetitionSelector(unittest.TestCase):

    def test_value_first_derivation_finds_nothing_usable_here(self):
        """The milestone-4 behaviour, kept as a regression guard.

        Not a criticism of the old derivation. Walking up from the matched
        values, the only chain every match agrees on is a bare ``a`` — which on
        a page with this many anchors is not a result list — and the runner-up
        is the tag column, which explains a third of the screen. Neither clears
        ``MIN_COVERAGE``, so milestone 4 compiled nothing. The lever exists to
        find a better chain, not to lower the bar.
        """
        _sel, cov, _vals = derive_selector(CLASSLESS_ROWS, ITEMS)
        self.assertLess(cov, 0.5)

    def test_repetition_finds_the_row_and_the_field(self):
        from anyuse.execution.html_extract import selector_is_specific
        sel, cov, vals = derive_selector_repeated(CLASSLESS_ROWS, ITEMS)
        self.assertGreaterEqual(cov, 0.9, sel)
        self.assertIn("div.event", sel)
        self.assertTrue(selector_is_specific(CLASSLESS_ROWS, sel, 60))
        self.assertEqual(vals, [i for i in ITEMS if i != "mmre"])

    def test_repetition_ignores_the_navigation(self):
        """The nav repeats too. It must not win: its items answer no query."""
        sel, _cov, vals = derive_selector_repeated(CLASSLESS_ROWS, ITEMS)
        self.assertFalse([v for v in vals if v.startswith("Nav ")])
        self.assertNotIn("nav", sel)

    def test_never_emits_a_selector_that_explains_nothing(self):
        sel, cov, _ = derive_selector_repeated(CLASSLESS_ROWS, ["nothing here at all"])
        self.assertEqual((sel, cov), ("", 0.0))

    def test_groups_need_at_least_three_members(self):
        two = "<html><body><ul><li>one</li><li>two</li></ul></body></html>"
        self.assertEqual(repeated_groups(parse(two)), [])

    def test_lever_off_reproduces_milestone_4(self):
        rec = html_recording()
        off, _ = compile_html(rec, ITEMS, "q", "pkg", levers=Levers.none())
        self.assertIsNone(off)
        on, _ = compile_html(rec, ITEMS, "q", "pkg", levers=Levers())
        self.assertIsNotNone(on)
        self.assertIn("repetition derivation", on.notes)

    def test_compiled_selector_actually_extracts(self):
        from anyuse.execution.html_extract import extract
        tmpl, why = compile_html(html_recording(), ITEMS, "q", "pkg")
        self.assertIsNotNone(tmpl, why)
        self.assertEqual(extract(CLASSLESS_ROWS, tmpl.extract["items"]),
                         [i for i in ITEMS if i != "mmre"])


# ---------------------------------------------------------------------------
# lever (f) — ground truth without a hand-written selector
# ---------------------------------------------------------------------------

class GroundTruthDerivation(unittest.TestCase):

    def test_picks_the_group_that_changed_between_queries(self):
        sel, items = derive_list_items([CLASSLESS_ROWS, CLASSLESS_ROWS_OTHER])
        self.assertIn("div.event", sel)
        self.assertEqual(len(items), 4)
        self.assertIn("alpha", items[0])

    def test_a_page_that_ignored_the_query_has_no_ground_truth(self):
        sel, items = derive_list_items([CLASSLESS_ROWS, CLASSLESS_ROWS])
        self.assertEqual((sel, items), ("", []))

    def test_one_markup_is_not_enough(self):
        self.assertEqual(derive_list_items([CLASSLESS_ROWS]), ("", []))

    def test_a_container_of_lists_yields_the_inner_result_list(self):
        # www.tvmaze.com: a results page of a few section blocks
        # (shows / people / networks), each holding a list. The blocks change as
        # much as the rows inside them and carry more text; the rows are the
        # result list, the blocks are not.
        def page(q):
            def sec(kind, n):
                rows = "".join(f'<div class="row"><a href="/{kind}/{i}">{q} {kind} {i}</a>'
                               f'<p>about {q} number {i} in {kind}</p></div>' for i in range(n))
                return f'<section class="block"><h2>{kind}</h2>{rows}</section>'
            return ("<html><body><main>" + sec("shows", 10) + sec("people", 6)
                    + sec("networks", 4) + "</main></body></html>")
        sel, items = derive_list_items([page("alpha"), page("beta")])
        self.assertEqual(len(items), 10)
        self.assertTrue(all("shows" in it for it in items), items)
        self.assertIn("div.row", sel)

    def test_text_is_read_in_document_order_and_pseudo_tags_are_dropped(self):
        # rdrr.io: a snapshot row "<h3>R/a.R:</h3>GITHUB pkg: title" with
        # a "<::after></::after>" placeholder read "< ::after> pkg: title" (the
        # heading lost, own text before children) and matched nothing in the
        # server HTML "<h3>R/a.R:</h3><p>pkg: title</p>".
        from anyuse.execution.html_extract import extract
        snap = ('<div class="list"><a class="item"><::after></::after><div class="d">'
                '<h3><b>R/a.R:</b></h3><p></p>pkg: title<p></p><code>x</code></div></a></div>')
        served = ('<div class="list"><a class="item"><div class="d"><h3><b>R/a.R:</b></h3>'
                  '<p>pkg: title</p><code>x</code></div></a></div>')
        self.assertEqual(extract(snap, "div.list a.item"), ["R/a.R: pkg: title x"])
        self.assertEqual(extract(served, "div.list a.item"), ["R/a.R: pkg: title x"])

    def test_a_flat_list_is_unchanged_by_the_descent(self):
        sel, items = derive_list_items([CLASSLESS_ROWS, CLASSLESS_ROWS_OTHER])
        self.assertIn("div.event", sel)


# ---------------------------------------------------------------------------
# lever (b) — evidence-based volatile parameters
# ---------------------------------------------------------------------------

class EvidenceVolatile(unittest.TestCase):

    def _recs(self, first_extra, second_extra):
        return [
            {"param": {"name": "q", "value": "alpha"},
             "network": [{"url": f"https://ex.test/api?q=alpha&{first_extra}",
                          "method": "GET", "status": 200}]},
            {"param": {"name": "q", "value": "beta"},
             "network": [{"url": f"https://ex.test/api?q=beta&{second_extra}",
                          "method": "GET", "status": 200}]},
        ]

    def test_a_key_that_varies_is_dropped_whatever_it_is_called(self):
        ev = volatile_evidence(self._recs("nonce2=aaa", "nonce2=bbb"))
        self.assertTrue(ev.decide("https://ex.test/api", "nonce2", "aaa"))

    def test_a_stable_key_is_kept_even_when_the_name_list_hates_it(self):
        """``ts`` is on the hardcoded volatile list. Evidence overrules it.

        This is the direction that matters: stripping a parameter the API really
        takes turns the template into an answer to a different question, and the
        fresh-input proof may not catch it if the default happens to match.
        """
        ev = volatile_evidence(self._recs("ts=week", "ts=week"))
        self.assertFalse(ev.decide("https://ex.test/api", "ts", "week"))

    def test_an_unseen_key_falls_back_to_the_name_list(self):
        ev = volatile_evidence(self._recs("q2=x", "q2=x"))
        self.assertTrue(ev.decide("https://other.test/api", "utm_source", "nl"))

    def test_the_parameter_itself_is_never_called_volatile(self):
        ev = volatile_evidence(self._recs("page=1", "page=1"))
        self.assertNotIn(("ex.test", "/api", "q"), ev.varying)
        self.assertNotIn(("ex.test", "/api", "q"), ev.stable)

    def test_urlify_honours_the_evidence(self):
        ev = volatile_evidence(self._recs("ts=week", "ts=week"))
        _url, query, dropped, _used, _why = urlify(
            "https://ex.test/api?q=alpha&ts=week", "q", "alpha", evidence=ev)
        self.assertEqual(query.get("ts"), "week")
        self.assertEqual(dropped, [])
        # and without the evidence the name list still strips it
        _url, query, dropped, _used, _why = urlify(
            "https://ex.test/api?q=alpha&ts=week", "q", "alpha")
        self.assertEqual(dropped, ["ts"])


# ---------------------------------------------------------------------------
# lever (c) — the read-only POST rung, and everything it refuses
# ---------------------------------------------------------------------------

class ReadOnlyPostShape(unittest.TestCase):

    def test_query_is_a_read(self):
        kind, op, why = readonly_post_shape(GQL_BODY)
        self.assertEqual((kind, op, why), ("graphql", "query", ""))

    def test_anonymous_query_is_a_read(self):
        body = json.dumps({"query": "{ search(q: \"x\") { name } }"})
        kind, op, why = readonly_post_shape(body)
        self.assertEqual((kind, op, why), ("graphql", "anonymous", ""))

    def test_mutation_is_refused(self):
        body = json.dumps({"query": "mutation Del($id: ID!) { delete(id: $id) }"})
        _kind, _op, why = readonly_post_shape(body)
        self.assertTrue(why)

    def test_subscription_is_refused(self):
        body = json.dumps({"query": "subscription S { ticks }"})
        _kind, _op, why = readonly_post_shape(body)
        self.assertTrue(why)

    def test_a_query_hiding_a_mutation_is_refused(self):
        """One HTTP request may carry several operations."""
        body = json.dumps({"query": "query A { x }\nmutation B { del }"})
        _kind, _op, why = readonly_post_shape(body)
        self.assertTrue(why)

    def test_unparseable_body_is_refused(self):
        _kind, _op, why = readonly_post_shape("\x00\x01binary")
        self.assertTrue(why)

    def test_empty_body_is_refused(self):
        _kind, _op, why = readonly_post_shape("")
        self.assertTrue(why)


class ReadOnlyPostCompile(unittest.TestCase):

    def test_a_graphql_query_compiles(self):
        rec = post_recording()
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNotNone(tmpl, why)
        self.assertEqual(tmpl.method, "POST")
        self.assertTrue(tmpl.readonly_post)
        self.assertEqual(tmpl.problems(), [])
        self.assertIn("{q}", tmpl.body)

    def test_default_compiler_does_not_offer_the_post_rung(self):
        res = compile_recording(post_recording())
        self.assertIsNone(res.template)
        self.assertNotEqual(res.status, "readonly_post")

    def test_the_bound_body_round_trips(self):
        rec = post_recording()
        tmpl, _ = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        _url, _h, body = tmpl.render({"q": "other"})
        self.assertEqual(json.loads(body)["variables"]["q"], "other")

    def test_a_mutation_is_refused_with_a_reason(self):
        rec = post_recording(post_data=json.dumps(
            {"query": "mutation M($q: String!) { create(q: $q) { name } }",
             "variables": {"q": "pkg"}}))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("mutation", why)

    def test_an_api_key_in_the_query_string_is_refused(self):
        """The rfc-editor.org case, which is the reason this rung exists."""
        rec = post_recording(url="https://ex.test/multi_search?x-typesense-api-key=abc")
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("api-key", why)

    def test_a_cookie_header_is_refused(self):
        rec = post_recording(headers={"content-type": "application/json",
                                      "Cookie": "session=abc"})
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("Cookie", why)

    def test_a_csrf_token_in_the_body_is_refused(self):
        rec = post_recording(post_data=json.dumps(
            {"query": "query S { search { name } }", "csrf_token": "abc",
             "variables": {"q": "pkg"}}))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("csrf", why.lower())

    def test_a_post_issued_twice_is_refused(self):
        rec = post_recording(occurrences=2)
        self.assertEqual(readonly_post_candidates(rec), [])
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("single-occurrence", why)

    def test_a_body_that_does_not_carry_the_value_is_refused(self):
        rec = post_recording(post_data=json.dumps(
            {"query": "query S { search { name } }", "variables": {"page": 1}}))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("re-bound", why)

    def test_an_oversized_body_is_refused(self):
        rec = post_recording(post_data=json.dumps(
            {"query": "query S { search { name } }",
             "variables": {"q": "pkg"}, "pad": "x" * 9000}))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNone(tmpl)
        self.assertIn("cap", why)

    def test_a_form_search_post_compiles(self):
        rec = post_recording(post_data="query=pkg&page=1",
                             url="https://ex.test/search",
                             body=json.dumps({"hits": [{"title": i} for i in POST_ITEMS]}))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        self.assertIsNotNone(tmpl, why)
        self.assertEqual(tmpl.headers["content-type"],
                         "application/x-www-form-urlencoded")

    def test_lever_off_means_no_post_rung_at_all(self):
        rec = post_recording()
        res = compile_recording(rec, levers=Levers.none())
        self.assertIsNone(res.template)
        res = compile_recording(rec, levers=Levers(readonly_post=True))
        self.assertIsNotNone(res.template)
        self.assertEqual(res.status, "readonly_post")


class ReadOnlyPostPolicy(unittest.TestCase):

    def _proved(self):
        rec = post_recording()
        tmpl, _ = compile_readonly_post(rec, visible_items(rec), "q", "pkg")
        return tmpl

    def test_the_boolean_alone_does_not_make_a_post_a_read(self):
        """A hand-edited compiled_api.json must not be able to smuggle one."""
        forged = RequestTemplate(method="POST", url="https://ex.test/graphql",
                                 body=json.dumps({"query": "mutation M { del }"}),
                                 readonly_post=True, params=[])
        self.assertTrue(forged.problems())
        self.assertEqual(api_hil_action(forged)["action"], "api_post")

    def test_a_post_without_the_flag_is_still_refused(self):
        plain = RequestTemplate(method="POST", url="https://ex.test/graphql",
                                body=GQL_BODY, params=["q"])
        self.assertTrue(any("not a read method" in p for p in plain.problems()))

    def test_a_proved_read_only_post_reaches_the_read_verb(self):
        action = api_hil_action(self._proved(), {"q": "pkg"})
        self.assertEqual(action["action"], "read")
        self.assertEqual(action["args"]["method"], "POST")

    def test_the_gate_still_promotes_a_destructive_keyword(self):
        from anyuse.execution.hil_gate import classify_step
        tmpl = self._proved()
        tmpl.url = "https://ex.test/graphql/delete"
        action = api_hil_action(tmpl, {"q": "pkg"})
        self.assertNotEqual(str(classify_step(action)), "read_only")

    def test_post_binding_encodes_values_without_changing_json_or_form_structure(self):
        json_t = RequestTemplate(
            method="POST", url="https://ex.test/graphql",
            headers={"content-type": "application/json"},
            body='{"query":"query Search { search(q: \\"{q}\\") { id } }",'
                 '"variables":{"q":"{q}"}}',
            params=["q"], readonly_post=True)
        attack = '\"},\"mutation\":\"deleteEverything'
        _url, _headers, body = json_t.render({"q": attack})
        decoded = json.loads(body)
        self.assertEqual(decoded["variables"]["q"], attack)
        self.assertEqual(set(decoded), {"query", "variables"})

        form_t = RequestTemplate(
            method="POST", url="https://ex.test/search",
            headers={"content-type": "application/x-www-form-urlencoded"},
            body="query={q}&page=1", params=["q"], readonly_post=True)
        _url, _headers, body = form_t.render({"q": "x&admin=true"})
        self.assertEqual(body, "query=x%26admin%3Dtrue&page=1")

    def test_bound_graphql_is_reclassified_immediately_before_send(self):
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

        class RecordingTransport:
            def __init__(self):
                self.sent = []
            def send(self, *args):
                self.sent.append(args)
                return ApiResponse(200, {"Content-Type": "application/json"}, "{}")

        tmpl = RequestTemplate(
            method="POST", url="https://ex.test/graphql",
            headers={"content-type": "application/json"}, body='{"query":"{q}"}',
            params=["q"], readonly_post=True)
        tr = RecordingTransport()
        old = os.environ.get("ANYUSE_API_READONLY_POST")
        os.environ["ANYUSE_API_READONLY_POST"] = "on"
        try:
            out = ApiStepExecutor(transport=tr).execute(
                tmpl, {"q": "mutation Delete { deleteEverything }"})
        finally:
            if old is None:
                os.environ.pop("ANYUSE_API_READONLY_POST", None)
            else:
                os.environ["ANYUSE_API_READONLY_POST"] = old
        self.assertFalse(out["success"])
        self.assertTrue(out["blocked"])
        self.assertEqual(tr.sent, [])

    def test_the_kill_switch_refuses_before_sending(self):
        import os
        from anyuse.execution.api_step import ApiStepExecutor

        class Boom:
            def send(self, *a, **k):                       # pragma: no cover
                raise AssertionError("a request was sent with the lever off")

        old = os.environ.get("ANYUSE_API_READONLY_POST")
        os.environ["ANYUSE_API_READONLY_POST"] = "off"
        try:
            out = ApiStepExecutor(transport=Boom()).execute(self._proved(),
                                                            {"q": "pkg"})
        finally:
            if old is None:
                os.environ.pop("ANYUSE_API_READONLY_POST", None)
            else:
                os.environ["ANYUSE_API_READONLY_POST"] = old
        self.assertFalse(out["success"])
        self.assertTrue(out["requires_approval"])

    def test_post_needs_exact_endpoint_and_operation_approval(self):
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor

        class RecordingTransport:
            def __init__(self):
                self.sent = []
            def send(self, *args):
                self.sent.append(args)
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   GQL_RESPONSE)

        self.assertEqual(readonly_post_operation(GQL_BODY), "query:Search")
        old = os.environ.get("ANYUSE_API_READONLY_POST")
        os.environ["ANYUSE_API_READONLY_POST"] = "on"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hosts = os.path.join(tmp, "hosts.json")
                tr = RecordingTransport()
                denied = ApiStepExecutor(
                    transport=tr, readonly_post_hosts_path=hosts).execute(
                        self._proved(), {"q": "pkg"})
                self.assertFalse(denied["success"])
                self.assertEqual(tr.sent, [])
                acknowledge_host(
                    "ex.test", path=hosts,
                    readonly_post_endpoint="https://ex.test/graphql",
                    readonly_post_operation="query:Other")
                denied = ApiStepExecutor(
                    transport=tr, readonly_post_hosts_path=hosts).execute(
                        self._proved(), {"q": "pkg"})
                self.assertFalse(denied["success"])
                acknowledge_host(
                    "ex.test", path=hosts,
                    readonly_post_endpoint="https://ex.test/graphql",
                    readonly_post_operation="query:Search")
                allowed = ApiStepExecutor(
                    transport=tr, readonly_post_hosts_path=hosts).execute(
                        self._proved(), {"q": "pkg"})
                self.assertTrue(allowed["success"], allowed.get("error"))
                self.assertEqual(len(tr.sent), 1)
        finally:
            if old is None:
                os.environ.pop("ANYUSE_API_READONLY_POST", None)
            else:
                os.environ["ANYUSE_API_READONLY_POST"] = old

    def test_signed_param_names_that_must_not_false_positive(self):
        for benign in ("keywords", "keyword", "monkey", "sorting", "q", "page",
                       "category", "tokenizer_mode"):
            self.assertEqual(signed_param_reason(benign), "", benign)

    def test_signed_param_names_that_must_be_caught(self):
        for bad in ("x-typesense-api-key", "apiKey", "X-CSRF-Token",
                    "access_token", "user_password", "client_secret"):
            self.assertTrue(signed_param_reason(bad), bad)


# ---------------------------------------------------------------------------
# lever (e) — challenge interstitials the milestone-4 detector missed
# ---------------------------------------------------------------------------

class ChallengeDetection(unittest.TestCase):

    def test_goaway_challenge_in_the_final_url(self):
        state = detect_blocked_ui(
            "Projects", "", "https://sr.ht/projects?__goaway_challenge=meta-refresh")
        self.assertTrue(state.blocked)
        self.assertEqual(state.reason, "challenge_interstitial")

    def test_a_cookie_banner_over_a_working_list_is_not_a_block(self):
        state = detect_blocked_ui("We use cookies. Manage your cookie settings.",
                                  "", "https://ex.test/s?q=x",
                                  items=["Alpha Pkg", "Beta Pkg"])
        self.assertFalse(state.blocked)

    def test_a_cookie_wall_with_nothing_behind_it_is_a_block(self):
        state = detect_blocked_ui("We use cookies. Manage your cookie settings.",
                                  "", "https://ex.test/s?q=x", items=[])
        self.assertTrue(state.blocked)

    def test_a_captcha_is_never_downgraded_by_a_rendered_list(self):
        state = detect_blocked_ui("Please complete the captcha", "",
                                  "https://ex.test/s?q=x", items=["Alpha Pkg"])
        self.assertTrue(state.blocked)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
