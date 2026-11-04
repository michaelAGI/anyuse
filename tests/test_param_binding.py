"""Binding a parameter that is only PART of a URL path — and the readiness settle.

Milestone 6, work items 2 and 3. Everything here is pure logic over strings and
dicts: this file opens no socket, starts no browser and imports no websocket
client. The readiness loop is exercised through injected ``js`` / ``pump``
coroutines, which is the same boundary the container driver uses.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest

from anyuse.execution.api_step import RequestTemplate
from anyuse.execution.cdp_replay import (
    CdpDriver, ReplayTemplate, readiness_from_recording, template_from_recording)
from anyuse.execution.param_binding import (
    MIN_PART, BindingSite, PathBindError, agreed_transform, apply_transform,
    bind_path_value, binding_evidence, corroborated_sites, sites_for,
    templated_url)


def rec(value, url, items=("Alpha", "Beta", "Gamma"), network=None):
    return {"param": {"name": "pkg", "value": value}, "final_url": url,
            "ui": {"items": list(items), "text": " ".join(items)},
            "network": list(network or [])}


# ---------------------------------------------------------------------------
# bind_path_value — the safety invariant
# ---------------------------------------------------------------------------

class BindPathValue(unittest.TestCase):

    def test_a_single_segment_parameter_still_encodes_a_slash(self):
        # This is the milestone-1..5 behaviour and it must not change: a value
        # containing "/" stays ONE segment.
        self.assertEqual(bind_path_value("a/b?c", 1), "a%2Fb%3Fc")
        self.assertEqual(bind_path_value("rust async", 1), "rust%20async")

    def test_a_two_segment_parameter_contributes_exactly_one_slash(self):
        self.assertEqual(bind_path_value("guzzlehttp/guzzle", 2),
                         "guzzlehttp/guzzle")
        self.assertEqual(bind_path_value("a b/c d", 2), "a%20b/c%20d")

    def test_a_value_of_the_wrong_shape_is_refused_not_reshaped(self):
        with self.assertRaises(PathBindError):
            bind_path_value("onlyone", 2)
        with self.assertRaises(PathBindError):
            bind_path_value("a/b/c", 2)

    def test_empty_and_relative_segments_are_refused(self):
        for bad in ("a//b", "../x", "a/.."):
            with self.assertRaises(PathBindError):
                bind_path_value(bad, 2)
        for bad in (".", ".."):
            with self.assertRaises(PathBindError):
                bind_path_value(bad, 1)

    def test_a_multi_segment_value_cannot_smuggle_a_query_or_fragment(self):
        out = bind_path_value("a?x=1/b#frag", 2)
        self.assertNotIn("?", out)
        self.assertNotIn("#", out)
        self.assertEqual(out.count("/"), 1)


class Transforms(unittest.TestCase):

    def test_the_four_transforms(self):
        self.assertEqual(apply_transform("identity", "A Christmas Carol"),
                         "A Christmas Carol")
        self.assertEqual(apply_transform("lower", "A Christmas Carol"),
                         "a christmas carol")
        self.assertEqual(apply_transform("hyphen", "A Christmas Carol"),
                         "A-Christmas-Carol")
        self.assertEqual(apply_transform("slug", "A Christmas  Carol"),
                         "a-christmas-carol")

    def test_an_unknown_transform_raises_rather_than_falling_back(self):
        with self.assertRaises(KeyError):
            apply_transform("titlecase", "x")


# ---------------------------------------------------------------------------
# where a value could sit
# ---------------------------------------------------------------------------

class Sites(unittest.TestCase):

    def test_a_run_of_two_path_segments(self):
        sites = sites_for("https://packagist.org/packages/guzzlehttp/guzzle",
                          "guzzlehttp/guzzle")
        best = sites[0]
        self.assertEqual((best.where, best.span, best.transform),
                         ("path_run", 2, "identity"))

    def test_a_whole_query_value_is_preferred_over_a_path_fragment(self):
        sites = sites_for("https://x.test/search-serde?q=serde", "serde")
        self.assertEqual(sites[0].where, "query")

    def test_a_delimited_fragment_inside_one_segment(self):
        sites = sites_for("https://x.test/book/1234-a-christmas-carol",
                          "a-christmas-carol")
        part = [s for s in sites if s.where == "path_part"]
        self.assertTrue(part)
        self.assertEqual(part[0].prefix, "1234-")
        self.assertEqual(part[0].suffix, "")

    def test_an_undelimited_fragment_is_never_offered(self):
        # "guzzle" inside "guzzlehttp" is not a parameter, it is a coincidence.
        sites = sites_for("https://x.test/packages/guzzlehttp", "guzzle")
        self.assertEqual([s for s in sites if s.where == "path_part"], [])

    def test_a_fragment_shorter_than_the_floor_is_never_offered(self):
        short = "ab"
        self.assertLess(len(short), MIN_PART)
        sites = sites_for(f"https://x.test/p/{short}-x", short)
        self.assertEqual([s for s in sites if s.where == "path_part"], [])

    def test_templated_url_round_trips_a_path_run(self):
        site = BindingSite("path_run", "2:3", span=2)
        self.assertEqual(
            templated_url("https://packagist.org/packages/guzzlehttp/guzzle",
                          site, "pkg"),
            "https://packagist.org/packages/{pkg}")

    def test_templated_url_returns_none_when_the_site_does_not_describe_the_url(self):
        site = BindingSite("path_run", "5:6", span=2)
        self.assertIsNone(templated_url("https://x.test/a/b", site, "pkg"))


# ---------------------------------------------------------------------------
# evidence across recordings
# ---------------------------------------------------------------------------

class Evidence(unittest.TestCase):

    def three(self):
        return [rec("guzzlehttp/guzzle", "https://packagist.org/packages/guzzlehttp/guzzle"),
                rec("monolog/monolog", "https://packagist.org/packages/monolog/monolog"),
                rec("symfony/console", "https://packagist.org/packages/symfony/console")]

    def test_a_site_all_three_recordings_support_is_corroborated(self):
        ev = binding_evidence(self.three(), placeholder="pkg")
        sites, why = corroborated_sites(
            "https://packagist.org/packages/guzzlehttp/guzzle",
            "guzzlehttp/guzzle", ev, "pkg")
        self.assertEqual(why, "")
        self.assertEqual(sites[0].span, 2)

    def test_one_recording_is_never_enough(self):
        ev = binding_evidence(self.three()[:1], placeholder="pkg")
        sites, why = corroborated_sites(
            "https://packagist.org/packages/guzzlehttp/guzzle",
            "guzzlehttp/guzzle", ev, "pkg")
        self.assertEqual(sites, [])
        self.assertIn("requires three distinct", why)

    def test_two_distinct_recordings_are_not_enough_for_path_binding(self):
        ev = binding_evidence(self.three()[:2], placeholder="pkg")
        sites, why = corroborated_sites(
            "https://packagist.org/packages/guzzlehttp/guzzle",
            "guzzlehttp/guzzle", ev, "pkg")
        self.assertEqual(sites, [])
        self.assertIn("three", why)

    def test_a_site_only_one_recording_supports_is_dropped(self):
        recs = self.three()
        # the third recording lives under a DIFFERENT constant remainder, so
        # the site describes two recordings and not the third
        recs[2] = rec("symfony/console", "https://packagist.org/p2/symfony/console")
        ev = binding_evidence(recs, placeholder="pkg")
        sites, why = corroborated_sites(
            "https://packagist.org/packages/guzzlehttp/guzzle",
            "guzzlehttp/guzzle", ev, "pkg")
        self.assertEqual(sites, [])
        self.assertIn("none is supported by all 3 recordings", why)

    def test_recordings_that_disagree_about_the_transform_are_refused(self):
        recs = [rec("Alpha Beta", "https://x.test/b/alpha-beta"),
                rec("Gamma Delta", "https://x.test/b/Gamma-Delta")]
        transform, why = agreed_transform(recs)
        self.assertEqual(transform, "")
        self.assertIn("disagree", why)

    def test_a_transform_every_recording_agrees_on_is_used(self):
        recs = [rec("A Christmas Carol", "https://se.test/ebooks/a-christmas-carol"),
                rec("Great Expectations", "https://se.test/ebooks/great-expectations"),
                rec("David Copperfield", "https://se.test/ebooks/david-copperfield")]
        transform, why = agreed_transform(recs)
        self.assertEqual((transform, why), ("slug", ""))
        ev = binding_evidence(recs, placeholder="pkg")
        sites, _ = corroborated_sites(
            "https://se.test/ebooks/a-christmas-carol", "A Christmas Carol", ev, "pkg")
        self.assertEqual(sites[0].transform, "slug")

    def test_every_recording_using_the_same_value_proves_nothing(self):
        same = [rec("x/y", "https://h.test/p/x/y") for _ in range(3)]
        ev = binding_evidence(same, placeholder="pkg")
        self.assertEqual(ev.supported, set())


# ---------------------------------------------------------------------------
# the templates
# ---------------------------------------------------------------------------

class Rendering(unittest.TestCase):

    def test_request_template_renders_a_two_segment_parameter(self):
        t = RequestTemplate(url="https://packagist.org/packages/{pkg}",
                            params=["pkg"], path_params={"pkg": 2},
                            extract={"items": "a[*]"})
        url, _h, _b = t.render({"pkg": "monolog/monolog"})
        self.assertEqual(url, "https://packagist.org/packages/monolog/monolog")

    def test_request_template_refuses_a_value_of_the_wrong_shape(self):
        t = RequestTemplate(url="https://packagist.org/packages/{pkg}",
                            params=["pkg"], path_params={"pkg": 2},
                            extract={"items": "a[*]"})
        with self.assertRaises(PathBindError):
            t.render({"pkg": "monolog"})

    def test_a_template_with_no_path_params_renders_exactly_as_before(self):
        t = RequestTemplate(url="https://x.test/p/{q}", params=["q"])
        url, _h, _b = t.render({"q": "a/b"})
        self.assertEqual(url, "https://x.test/p/a%2Fb")

    def test_path_params_naming_a_placeholder_that_is_not_there_is_a_problem(self):
        t = RequestTemplate(url="https://x.test/p/{q}", params=["q", "other"],
                            path_params={"other": 2}, extract={"items": "a"})
        self.assertTrue(any("not a placeholder" in p for p in t.problems()))

    def test_an_unknown_transform_is_a_problem(self):
        t = RequestTemplate(url="https://x.test/p/{q}", params=["q"],
                            param_transforms={"q": "titlecase"},
                            extract={"items": "a"})
        self.assertTrue(any("unknown parameter transform" in p for p in t.problems()))

    def test_replay_template_renders_and_refuses_the_same_way(self):
        t = ReplayTemplate(url="https://packagist.org/packages/{pkg}",
                           params=["pkg"], path_params={"pkg": 2},
                           extract={"items": "li.row"})
        self.assertEqual(t.render({"pkg": "symfony/console"}),
                         "https://packagist.org/packages/symfony/console")
        with self.assertRaises(PathBindError):
            t.render({"pkg": "symfony"})

    def test_replay_template_applies_an_agreed_transform(self):
        t = ReplayTemplate(url="https://se.test/ebooks/{b}", params=["b"],
                           param_transforms={"b": "slug"}, extract={"items": "p"})
        self.assertEqual(t.render({"b": "A Christmas Carol"}),
                         "https://se.test/ebooks/a-christmas-carol")


class TemplateFromRecording(unittest.TestCase):

    def three(self):
        return [rec("guzzlehttp/guzzle", "https://packagist.org/packages/guzzlehttp/guzzle"),
                rec("monolog/monolog", "https://packagist.org/packages/monolog/monolog"),
                rec("symfony/console", "https://packagist.org/packages/symfony/console")]

    def test_without_the_other_recordings_it_still_refuses(self):
        # This is milestone 5's behaviour, and it is correct for one recording.
        t = template_from_recording(self.three()[0], "pkg", selector="li.row")
        self.assertEqual(t.url, "")
        self.assertIn("cannot be re-bound", t.notes)

    def test_with_the_other_recordings_it_binds_the_path_run(self):
        recs = self.three()
        t = template_from_recording(recs[0], "pkg", selector="li.row",
                                    recordings=recs)
        self.assertEqual(t.url, "https://packagist.org/packages/{pkg}")
        self.assertEqual(t.path_params, {"pkg": 2})
        self.assertEqual(t.problems(), [])
        self.assertEqual(t.render({"pkg": "monolog/monolog"}),
                         "https://packagist.org/packages/monolog/monolog")

    def test_a_whole_segment_workflow_is_unchanged_by_the_new_path(self):
        recs = [rec("serde", "https://crates.io/crates/serde"),
                rec("regex", "https://crates.io/crates/regex")]
        a = template_from_recording(recs[0], "pkg", selector="li.row")
        b = template_from_recording(recs[0], "pkg", selector="li.row",
                                    recordings=recs)
        self.assertEqual(a.url, b.url)
        self.assertEqual(b.path_params, {})


# ---------------------------------------------------------------------------
# readiness (work item 3)
# ---------------------------------------------------------------------------

class Readiness(unittest.TestCase):

    def recording(self):
        return rec("guzzlehttp/guzzle",
                   "https://packagist.org/packages/guzzlehttp/guzzle",
                   items=("Alpha", "Beta"),
                   network=[{"url": "https://packagist.org/packages/guzzlehttp/guzzle",
                             "status": 200, "mime": "text/html",
                             "body": "<html>Alpha Beta</html>"},
                            {"url": "https://cdn.test/app.js", "status": 200,
                             "mime": "application/javascript", "body": "var x=1"},
                            {"url": "https://packagist.org/api/list?x=1", "status": 200,
                             "mime": "application/json", "body": '{"a":"Alpha"}'}])

    def test_the_document_and_the_explaining_xhr_are_the_patterns(self):
        r = readiness_from_recording(self.recording(), "li.row")
        hosts = {h for h, _p in r["explaining"]}
        self.assertIn("packagist.org", hosts)
        self.assertNotIn("cdn.test", hosts)

    def test_the_pattern_stops_before_the_segment_carrying_the_value(self):
        r = readiness_from_recording(self.recording(), "li.row")
        self.assertIn(["packagist.org", "/packages"], r["explaining"])

    def test_a_visible_only_recording_counts_only_the_shown_rows(self):
        from anyuse.execution.cdp_replay import ready_read_passes
        shown = rec("x", "https://h.test/s?q=x", items=("alpha x", "beta x"))
        hidden = {**shown, "ground_truth_visible_only": True}
        self.assertFalse(readiness_from_recording(shown, "li.row").get("visible_only"))
        ready = readiness_from_recording(hidden, "li.row")
        self.assertTrue(ready["visible_only"])
        self.assertEqual(ready["min_items"], 2)
        # the rows are there but still hidden: the filter has not shown them yet
        dom = ("<html><ul><li class='row' style='display:none'>alpha x</li>"
               "<li class='row' hidden>beta x</li><li class='row'>gamma x</li></ul></html>")
        ok, why = ready_read_passes(dom, ready)
        self.assertFalse(ok, why)
        self.assertIn("1 item(s)", why)
        self.assertTrue(ready_read_passes(dom, readiness_from_recording(shown, "li.row"))[0])
        # the browser rung built from the recording waits the same way
        tmpl = template_from_recording(dict(hidden, ground_truth_selector="li.row"), "q",
                                       readiness=True)
        self.assertTrue(tmpl.visible_only and tmpl.readiness["visible_only"])

    def test_the_wait_loop_counts_only_shown_rows_for_a_visible_only_template(self):
        seen = []
        for visible in (False, True):
            self.drive([{"_finish": True, "n": 3, "len": 500, "ready": "complete"}] * 3,
                       [["packagist.org", "/packages"]], extra={"visible_only": visible},
                       exprs=seen)
        plain, shown = seen[0], seen[-1]
        self.assertNotIn("hidden", plain)
        # the marks html_extract.hidden_by_markup reads, on the row and every ancestor
        from anyuse.execution.html_extract import _HIDDEN_STYLE
        self.assertIn("hasAttribute('hidden')", shown)
        self.assertIn("'aria-hidden'", shown)
        self.assertIn(json.dumps(_HIDDEN_STYLE.pattern), shown)
        self.assertIn("parentElement", shown)
        # not the rendered layout: the recorded count and the ready read both include
        # rows a stylesheet hides, so a wait that left them out could stay below the
        # recorded count and run on to the timer
        self.assertNotIn("getClientRects", shown)
        self.assertNotIn("getComputedStyle", shown)

    def test_matching_is_host_plus_path_prefix(self):
        pats = [["packagist.org", "/packages"]]
        self.assertTrue(CdpDriver._matches(
            "https://packagist.org/packages/monolog/monolog", pats))
        self.assertFalse(CdpDriver._matches("https://other.test/packages/x", pats))
        self.assertFalse(CdpDriver._matches("https://packagist.org/about", pats))

    def test_the_recorded_request_count_is_kept_per_pattern(self):
        recording = self.recording()
        recording["network"].append({"url": "https://packagist.org/api/list?x=2", "status": 200,
                                     "mime": "application/json", "body": '{"a":"Gamma"}'})
        r = readiness_from_recording(recording, "li.row")
        self.assertEqual(r["explaining"], [["packagist.org", "/packages"],
                                           ["packagist.org", "/api/list"]])
        self.assertEqual(r["explaining_min"], [1, 2])
        for x in range(3, 7):
            recording["network"].append({"url": f"https://packagist.org/api/list?x={x}",
                                         "status": 200, "mime": "application/json",
                                         "body": "{}"})
        recording["network"].append({"url": "https://packagist.org/api/list?x=9",
                                     "status": 404, "mime": "application/json", "body": "{}"})
        self.assertEqual(readiness_from_recording(recording, "li.row")["explaining_min"],
                         [1, 3])

    # -- the wait loop, with injected coroutines ----------------------------

    def drive(self, states, patterns, stable_ms=0, ceiling=2.0, keep_growing=False,
              extra=None, exprs=None):
        """Run ``_await_ready`` over a scripted sequence of page states."""
        driver = CdpDriver("http://127.0.0.1:0")
        events = [{"method": "Network.requestWillBeSent",
                   "params": {"requestId": "1",
                              "request": {"url": "https://packagist.org/packages/x"}}}]
        seq = list(states)
        tail = {"len": states[-1].get("len", 0) if states else 0}

        async def js(_expr):
            if exprs is not None:
                exprs.append(_expr)
            if seq:
                return seq.pop(0)
            if keep_growing:                      # a page that never settles
                # grow PROPORTIONALLY: the stability rule is a relative one, so
                # a fixed increment would eventually fall inside its tolerance
                tail["len"] = int(tail["len"] * 1.2) + 5000
                return {"n": 3, "len": tail["len"], "ready": "complete"}
            return {"n": 0, "len": 10, "ready": "loading"}

        async def pump(seconds):
            if seq and seq[0].get("_finish"):
                events.append({"method": "Network.loadingFinished",
                               "params": {"requestId": "1"}})
            await asyncio.sleep(0)

        readiness = {"selector": "li.row", "explaining": patterns,
                     "stable_ms": stable_ms, "poll_ms": 50, "min_items": 1, **(extra or {})}
        return asyncio.run(driver._await_ready(js, pump, events, readiness, ceiling))

    def test_it_returns_as_soon_as_the_evidence_is_all_there(self):
        out = self.drive(
            [{"_finish": True, "n": 0, "len": 100, "ready": "loading"},
             {"n": 3, "len": 500, "ready": "complete"},
             {"n": 3, "len": 500, "ready": "complete"}],
            [["packagist.org", "/packages"]])
        self.assertEqual(out["how"], "ready")
        self.assertTrue(out["net"] and out["items"] and out["stable"])

    def test_it_stops_at_the_ceiling_when_the_selector_never_matches(self):
        out = self.drive([{"n": 0, "len": 100, "ready": "complete"}] * 40,
                         [["packagist.org", "/packages"]], ceiling=0.4)
        self.assertEqual(out["how"], "ceiling")
        self.assertFalse(out["items"])
        self.assertIn("the fixed timer would have read it", out["note"])

    def test_a_growing_dom_is_not_stable(self):
        out = self.drive(
            [{"_finish": True, "n": 3, "len": 100, "ready": "complete"}],
            [["packagist.org", "/packages"]], stable_ms=300, ceiling=0.4,
            keep_growing=True)
        self.assertEqual(out["how"], "ceiling")
        self.assertFalse(out["stable"])
        self.assertTrue(out["net"] and out["items"])

    def test_an_invalid_selector_never_satisfies_the_item_condition(self):
        out = self.drive([{"n": -1, "len": 100, "ready": "complete"}] * 40,
                         [["packagist.org", "/packages"]], ceiling=0.3)
        self.assertEqual(out["how"], "ceiling")
        self.assertFalse(out["items"])

    # -- request counts, on a simulated clock ---------------------------------

    def drive_requests(self, explaining_min, second_at=1.0, ceiling=3.0, stable_ms=0):
        """``_await_ready`` while the page loads its document, one /api/list request,
        and a second one at ``second_at`` s (never when ``None``)."""
        now = [0.0]
        events = []
        schedule = [(0.0, "Network.requestWillBeSent", "doc",
                     "https://packagist.org/packages/x"),
                    (0.1, "Network.loadingFinished", "doc", None),
                    (0.1, "Network.requestWillBeSent", "a1", "https://packagist.org/api/list?p=1"),
                    (0.3, "Network.loadingFinished", "a1", None)]
        if second_at is not None:
            schedule += [(second_at, "Network.requestWillBeSent", "a2",
                          "https://packagist.org/api/list?p=2"),
                         (second_at + 0.4, "Network.loadingFinished", "a2", None)]

        async def pump(seconds):
            now[0] += seconds
            while schedule and schedule[0][0] <= now[0] + 1e-9:
                _t, method, rid, url = schedule.pop(0)
                params = {"requestId": rid}
                if url:
                    params["request"] = {"url": url}
                events.append({"method": method, "params": params})

        async def js(_expr):
            return {"n": 3, "len": 500, "ready": "complete"}

        readiness = {"selector": "li.row", "stable_ms": stable_ms, "poll_ms": 50, "min_items": 1,
                     "explaining": [["packagist.org", "/packages"],
                                    ["packagist.org", "/api/list"]]}
        if explaining_min is not None:
            readiness["explaining_min"] = explaining_min
        return asyncio.run(CdpDriver("http://127.0.0.1:0")._await_ready(
            js, pump, events, readiness, ceiling, clock=lambda: now[0]))

    def test_it_waits_for_every_recorded_request_of_a_pattern(self):
        out = self.drive_requests([1, 2])
        self.assertEqual(out["how"], "ready")
        self.assertGreaterEqual(out["waited_ms"], 1400)

    def test_the_page_must_settle_after_the_last_request_finishes(self):
        # the DOM never changes here, so it was "stable" long before the second
        # request finished; the page's script may not have rendered it yet
        out = self.drive_requests([1, 2], stable_ms=400)
        self.assertEqual(out["how"], "ready")
        self.assertGreaterEqual(out["waited_ms"], 1800)

    def test_a_missing_second_request_holds_the_read_until_the_ceiling(self):
        out = self.drive_requests([1, 2], second_at=None)
        self.assertEqual(out["how"], "ceiling")
        self.assertFalse(out["net"])
        self.assertEqual(out["waited_ms"], 3000)

    def test_without_request_counts_one_seen_and_none_in_flight_is_enough(self):
        out = self.drive_requests(None)
        self.assertEqual(out["how"], "ready")
        self.assertLess(out["waited_ms"], 1000)

    def test_a_request_in_flight_holds_the_read_once_the_count_is_met(self):
        # one request was recorded; a second one of the same pattern goes out
        # before the first finishes and is still loading when the count is met
        out = self.drive_requests([1, 1], second_at=0.2)
        self.assertEqual(out["how"], "ready")
        self.assertGreaterEqual(out["waited_ms"], 600)

    def test_the_documents_own_pattern_counts_one_request(self):
        # the value sits in the query, so the document's pattern is the whole host
        # and also matches the page's script and stylesheet
        recording = rec("rust", "https://hn.example/?query=rust", items=("Alpha", "Beta"),
                        network=[{"url": "https://hn.example/?query=rust", "status": 200,
                                  "mime": "text/html", "body": "<html>Alpha Beta</html>"},
                                 {"url": "https://hn.example/main.js", "status": 200,
                                  "mime": "application/javascript", "body": ""},
                                 {"url": "https://hn.example/main.css", "status": 200,
                                  "mime": "text/css", "body": ""}])
        r = readiness_from_recording(recording, "li.row")
        self.assertEqual(r["explaining"], [["hn.example", "/"]])
        self.assertEqual(r["explaining_min"], [1])

    # -- value calls: the search's own data calls, counted at compile time ----

    def _typed(self, network, final="https://h.test/search?q=river"):
        return rec("river", final, items=("Alpha", "Beta", "Gamma"), network=network)

    @staticmethod
    def _get(url, body="", mime="application/json"):
        return {"url": url, "status": 200, "mime": mime, "body": body}

    def test_prefix_calls_typed_on_the_way_are_not_counted(self):
        names = '{"items": ["Alpha", "Beta", "Gamma"]}'
        network = [self._get("https://h.test/search?q=river", "<html></html>", "text/html")]
        network += [self._get(f"https://h.test/api/list?q={p}", names)
                    for p in ("ri", "riv", "rive", "river")]
        r = readiness_from_recording(self._typed(network), "li.row")
        self.assertEqual(r["rule"], "value_calls")
        self.assertEqual(r["explaining"], [["h.test", "/search"], ["h.test", "/api/list"]])
        self.assertEqual(r["value_calls"], [0, 1])
        # the request count the earlier rule waits for is kept
        self.assertEqual(r["explaining_min"], [1, 3])

    def test_calls_before_the_result_document_are_not_counted(self):
        names = '{"items": ["Alpha", "Beta", "Gamma"]}'
        network = [self._get("https://h.test/", "<html>home</html>", "text/html"),
                   self._get("https://h.test/api/suggest?q=river", names),
                   self._get("https://h.test/search?q=river", "<html></html>", "text/html"),
                   self._get("https://h.test/api/list?q=river&limit=50", names)]
        r = readiness_from_recording(self._typed(network), "li.row")
        self.assertEqual(r["explaining"], [["h.test", "/search"], ["h.test", "/api/suggest"],
                                           ["h.test", "/api/list"]])
        self.assertEqual(r["value_calls"], [0, 0, 1])

    def test_a_re_query_with_the_same_rows_is_not_a_second_batch(self):
        doc = self._get("https://h.test/search?q=river", "<html></html>", "text/html")
        same = [doc, self._get("https://h.test/api/list?q=river&_=1", '{"a": ["Alpha", "Beta"]}'),
                self._get("https://h.test/api/list?q=river&_=2", '{"a": ["Alpha", "Beta"]}')]
        self.assertEqual(readiness_from_recording(self._typed(same), "li.row")["value_calls"], [0, 1])
        paged = same + [self._get("https://h.test/api/list?q=river&page=2", '{"a": ["Gamma"]}')]
        self.assertEqual(readiness_from_recording(self._typed(paged), "li.row")["value_calls"],
                         [0, 2])
        # a call whose body was not kept counts, as the request count does
        unkept = same + [{"url": "https://h.test/api/list?q=river&page=3", "status": 200}]
        self.assertEqual(readiness_from_recording(self._typed(unkept), "li.row")["value_calls"],
                         [0, 2])
        # the replay counts distinct calls, so the same call made twice is one batch
        twice = same + [self._get("https://h.test/api/list?q=river&_=2", '{"a": ["Gamma"]}')]
        self.assertEqual(readiness_from_recording(self._typed(twice), "li.row")["value_calls"],
                         [0, 1])
        # and no more than the most a ready read waits for
        many = [doc] + [self._get(f"https://h.test/api/list?q=river&page={i}", f'{{"a": ["{n}"]}}')
                        for i, n in enumerate(("Alpha", "Beta", "Gamma", "Delta"), 1)]
        recording = rec("river", "https://h.test/search?q=river",
                        items=("Alpha", "Beta", "Gamma", "Delta"), network=many)
        from anyuse.execution.cdp_replay import MAX_EXPLAINING_COUNT
        self.assertEqual(readiness_from_recording(recording, "li.row")["value_calls"],
                         [0, MAX_EXPLAINING_COUNT])

    def test_a_data_call_to_the_result_url_is_not_taken_for_the_document(self):
        names = '{"items": ["Alpha", "Beta", "Gamma"]}'
        network = [self._get("https://h.test/search?q=river", "<html></html>", "text/html"),
                   self._get("https://h.test/api/list?q=river", names),
                   self._get("https://h.test/search?q=river", names)]
        r = readiness_from_recording(self._typed(network), "li.row")
        self.assertEqual(r["explaining"], [["h.test", "/search"], ["h.test", "/api/list"]])
        # the JSON call to the result URL is a batch of the document's own pattern
        self.assertEqual(r["value_calls"], [1, 1])

    def test_a_list_fetched_from_the_documents_own_path_is_a_batch(self):
        names = '{"items": ["Alpha", "Beta", "Gamma"]}'
        shell = self._get("https://h.test/search?q=river", "<html><ul></ul></html>", "text/html")
        own = self._get("https://h.test/search?q=river&format=json", names)
        r = readiness_from_recording(self._typed([shell, own]), "li.row")
        self.assertEqual(r["explaining"], [["h.test", "/search"]])
        self.assertEqual(r["value_calls"], [1])
        # a document that already holds the rows needs no call of its own path
        page = self._get("https://h.test/search?q=river",
                         "<html><ul><li>Alpha</li><li>Beta</li><li>Gamma</li></ul></html>",
                         "text/html")
        self.assertEqual(readiness_from_recording(self._typed([page, own]), "li.row")
                         ["value_calls"], [0])
        # nor is the document a batch of its own path when its body was not kept
        bodiless = {"url": "https://h.test/search?q=river", "status": 200, "mime": "text/html"}
        self.assertEqual(readiness_from_recording(self._typed([bodiless]), "li.row")
                         ["value_calls"], [0])

    def test_the_document_is_not_a_batch_of_its_own_path(self):
        doc = [(0.0, "Network.requestWillBeSent", "doc", "https://h.test/search?q=kafka",
                "Document"),
               (0.1, "Network.loadingFinished", "doc", None)]
        late = self._list(1.5, 1.9, "j1", "https://h.test/search?q=kafka&format=json")
        def probe(t):
            n = 6 if t >= 1.9 else 0
            return {"n": n, "rlen": 10 * n, "len": 500, "ready": "complete"}
        out = self.drive_calls(doc + late, [1], explaining=[["h.test", "/search"]], probe=probe)
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 1900 + 400)
        self.assertEqual(out["items"], 6)

    def test_a_pattern_whose_calls_never_carry_the_value_keeps_the_recorded_count(self):
        network = [self._get("https://h.test/search?q=river", "<html></html>", "text/html"),
                   self._get("https://h.test/api/results/abc123", '{"a": ["Alpha", "Beta"]}')]
        r = readiness_from_recording(self._typed(network), "li.row")
        self.assertEqual(r["value_calls"], [0, None])
        self.assertEqual(r["explaining_min"], [1, 1])

    def test_the_value_in_a_post_body_or_another_encoding_is_carried(self):
        network = [self._get("https://h.test/search?q=Red%20River", "<html></html>", "text/html"),
                   {**self._get("https://h.test/graphql", '{"data": ["Alpha"]}'),
                    "post_data": '{"query": "red river"}'},
                   self._get("https://h.test/api/s/red-river", '{"a": ["Beta"]}')]
        recording = rec("Red River", "https://h.test/search?q=Red%20River",
                        items=("Alpha", "Beta"), network=network)
        self.assertEqual(readiness_from_recording(recording, "li.row")["value_calls"], [0, 1, 1])

    def drive_calls(self, schedule, value_calls, ceiling=3.0, stable_ms=400, probe=None,
                    explaining=None, explaining_min=None, rule=True, value="kafka"):
        """``_await_value_calls`` on a simulated clock. ``schedule``: ``(at, method, rid,
        url)``; ``probe(now)``: the page's answer to the readiness probe."""
        now = [0.0]
        events = []
        schedule = sorted(schedule, key=lambda x: x[0])

        async def pump(seconds):
            now[0] += seconds
            while schedule and schedule[0][0] <= now[0] + 1e-9:
                _t, method, rid, url, *kind = schedule.pop(0)
                params = {"requestId": rid}
                if kind:
                    params["type"] = kind[0]
                if url:
                    params["request"] = {"url": url}
                events.append({"method": method, "params": params})

        async def js(_expr):
            return (probe(now[0]) if probe else
                    {"n": 3, "rlen": 30, "len": 500, "ready": "complete"})

        readiness = {"selector": "li.row", "stable_ms": stable_ms, "poll_ms": 50,
                     "min_items": 6, "value": value,
                     "explaining": explaining or [["h.test", "/"], ["h.test", "/api/list"]],
                     "value_calls": value_calls}
        if explaining_min is not None:
            readiness["explaining_min"] = explaining_min
        if rule:
            readiness["rule"] = "value_calls"
            return asyncio.run(CdpDriver("http://127.0.0.1:0")._await_value_calls(
                js, pump, events, readiness, ceiling, clock=lambda: now[0]))
        return asyncio.run(CdpDriver("http://127.0.0.1:0")._await_ready(
            js, pump, events, readiness, ceiling, clock=lambda: now[0]))

    DOC = [(0.0, "Network.requestWillBeSent", "doc", "https://h.test/?q=kafka"),
           (0.1, "Network.loadingFinished", "doc", None)]

    def _list(self, sent, done, rid="a1", url="https://h.test/api/list?q=kafka"):
        out = [(sent, "Network.requestWillBeSent", rid, url)]
        if done is not None:
            out.append((done, "Network.loadingFinished", rid, None))
        return out

    def test_an_open_request_without_the_value_does_not_hold_the_read(self):
        poll = [(0.1, "Network.requestWillBeSent", "poll", "https://h.test/poll?id=1")]
        out = self.drive_calls(self.DOC + self._list(0.1, 0.3) + poll, [0, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertEqual(out["rule"], "value_calls")
        self.assertLess(out["waited_ms"], 1000)
        self.assertEqual(out["open_value_calls"], 0)
        # the earlier rule: the site-root pattern matches the poll, which never answers
        old = self.drive_calls(self.DOC + self._list(0.1, 0.3) + poll, [0, 1], rule=False,
                               explaining_min=[1, 1])
        self.assertEqual(old["how"], "ceiling", old)

    def test_a_value_call_in_flight_holds_the_read(self):
        out = self.drive_calls(self.DOC + self._list(0.1, 2.0), [0, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 2000 + 400)
        # also one of the document's own pattern, which is never counted
        out = self.drive_calls(self.DOC + self._list(0.1, 0.3)
                               + self._list(0.2, 1.5, "a2", "https://h.test/facets?q=kafka"),
                               [0, 1])
        self.assertGreaterEqual(out["waited_ms"], 1500 + 400)

    def test_a_seen_pattern_waits_for_its_recorded_batches(self):
        two = self.DOC + self._list(0.1, 0.3) + self._list(
            1.0, 1.4, "a2", "https://h.test/api/list?q=kafka&page=2")
        out = self.drive_calls(two, [0, 2])
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 1400 + 400)
        out = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 2])
        self.assertEqual(out["how"], "ceiling", out)
        self.assertFalse(out["calls"])
        self.assertIn("the fixed timer would have read it", out["note"])

    def test_a_pattern_the_replay_never_calls_is_not_waited_for(self):
        out = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 1, 1],
                               explaining=[["h.test", "/"], ["h.test", "/api/suggest"],
                                           ["h.test", "/api/list"]])
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 1000)
        # nor when the results come from a call that never carried the input
        results = [(0.1, "Network.requestWillBeSent", "r1", "https://h.test/api/results/xyz"),
                   (0.6, "Network.loadingFinished", "r1", None)]
        out = self.drive_calls(self.DOC + results, [0, 1, None], explaining_min=[1, 3, 1],
                               explaining=[["h.test", "/search"], ["h.test", "/api/suggest"],
                                           ["h.test", "/api/results"]])
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 2000)

    def test_nothing_answered_yet_is_not_ready(self):
        out = self.drive_calls(self.DOC, [0, 1])
        self.assertEqual(out["how"], "ceiling", out)
        self.assertFalse(out["answered"])
        # the list came with the document: nothing to wait for but the rows
        out = self.drive_calls(self.DOC, [0], explaining=[["h.test", "/"]])
        self.assertEqual(out["how"], "ready", out)
        # but not before the document is complete
        out = self.drive_calls(self.DOC, [0], explaining=[["h.test", "/"]], probe=lambda t: {
            "n": 3, "rlen": 30, "len": 500, "ready": "interactive"})
        self.assertEqual(out["how"], "ceiling", out)

    def test_a_pattern_without_value_calls_keeps_the_earlier_condition(self):
        results = [(0.1, "Network.requestWillBeSent", "r1", "https://h.test/api/results/abc"),
                   (0.6, "Network.loadingFinished", "r1", None)]
        out = self.drive_calls(self.DOC + results, [0, None],
                               explaining=[["h.test", "/search"], ["h.test", "/api/results"]],
                               explaining_min=[1, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 600 + 400)
        out = self.drive_calls(self.DOC, [0, None],
                               explaining=[["h.test", "/search"], ["h.test", "/api/results"]],
                               explaining_min=[1, 1])
        self.assertEqual(out["how"], "ceiling", out)

    def test_a_value_the_page_sends_in_another_form_keeps_the_earlier_condition(self):
        # the page folds the input ("Red River" -> red_river): no call carries it in a form
        # the rule knows, so the list pattern is held to the earlier rule, not to the ceiling
        folded = self._list(0.1, 0.3, url="https://h.test/api/list?q=red_river")
        out = self.drive_calls(self.DOC + folded, [0, 1], explaining_min=[1, 1],
                               value="Red River")
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 1000)
        # as many calls as the recording made, none of them still open
        two = folded + self._list(0.2, 1.5, "a2", "https://h.test/api/list?q=red_river&page=2")
        out = self.drive_calls(self.DOC + two, [0, 1], explaining_min=[1, 2], value="Red River")
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 1500 + 400)
        out = self.drive_calls(self.DOC + folded, [0, 1], explaining_min=[1, 2],
                               value="Red River")
        self.assertEqual(out["how"], "ceiling", out)

    def test_a_call_without_the_value_does_not_stand_in_for_the_search_call(self):
        three = [["h.test", "/"], ["h.test", "/api/suggest"], ["h.test", "/api/list"]]
        prefetch = self._list(0.1, 0.2, "s1", "https://h.test/api/suggest?q=")
        # the suggestion list is fetched empty on load; the search call goes out later
        out = self.drive_calls(self.DOC + prefetch + self._list(0.8, 1.0), [0, 1, 1],
                               explaining=three, explaining_min=[1, 1, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertGreaterEqual(out["waited_ms"], 1000 + 400)
        # once a call carries the input, a pattern called without it is not waited for
        out = self.drive_calls(self.DOC + prefetch + self._list(0.1, 0.3), [0, 1, 1],
                               explaining=three, explaining_min=[1, 3, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 1000)

    def test_an_input_in_the_sites_own_name_does_not_hold_the_read(self):
        doc = [(0.0, "Network.requestWillBeSent", "doc", "https://crates.test/?q=crates"),
               (0.1, "Network.loadingFinished", "doc", None)]
        poll = [(0.1, "Network.requestWillBeSent", "poll", "https://crates.test/poll?id=1")]
        out = self.drive_calls(
            doc + self._list(0.1, 0.3, url="https://crates.test/api/list?q=crates") + poll,
            [0, 1], value="crates",
            explaining=[["crates.test", "/"], ["crates.test", "/api/list"]])
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 1000)

    def test_an_open_request_outside_the_patterns_does_not_hold_the_read(self):
        # another host's beacon carries the page's URL, and so the input, and never answers
        beacon = [(0.1, "Network.requestWillBeSent", "b1",
                   "https://stats.other.test/collect?dl=https%3A%2F%2Fh.test%2F%3Fq%3Dkafka")]
        out = self.drive_calls(self.DOC + self._list(0.1, 0.3) + beacon, [0, 1])
        self.assertEqual(out["how"], "ready", out)
        self.assertLess(out["waited_ms"], 1000)
        self.assertEqual(out["open_value_calls"], 0)

    def test_a_failed_value_call_is_not_an_answer(self):
        failed = [(0.1, "Network.requestWillBeSent", "a1", "https://h.test/api/list?q=kafka"),
                  (0.3, "Network.loadingFailed", "a1", None)]
        out = self.drive_calls(self.DOC + failed, [0, 1])
        self.assertEqual(out["how"], "ceiling", out)
        self.assertFalse(out["answered"])

    def test_without_the_input_the_replay_keeps_the_earlier_rule(self):
        # a readiness with no input to match calls against waits by the earlier rule
        from anyuse.execution.browser_supervisor import DOM_EXPR
        events = [{"method": "Network.requestWillBeSent",
                   "params": {"requestId": "d", "request": {"url": "https://h.test/?q=kafka"}}},
                  {"method": "Network.loadingFinished", "params": {"requestId": "d"}}]

        async def js(expr):
            return "<html></html>" if expr == DOM_EXPR else {
                "n": 6, "rlen": 30, "len": 500, "ready": "complete"}

        async def pump(_s):
            return None
        for value, rule in (("kafka", "value_calls"), ("", None)):
            readiness = {"selector": "li.row", "rule": "value_calls", "value": value,
                         "explaining": [["h.test", "/"]], "explaining_min": [1],
                         "value_calls": [0], "min_items": 6, "poll_ms": 50, "stable_ms": 0}
            _dom, settle = asyncio.run(CdpDriver("http://127.0.0.1:0")._settle_and_read(
                js, pump, events, readiness, 0.5,
                clock=iter([i * 0.05 for i in range(200)]).__next__))
            self.assertEqual(settle["how"], "ready", settle)
            self.assertEqual(settle.get("rule"), rule, settle)

    def test_the_rows_must_stand_still(self):
        growing = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 1], probe=lambda t: {
            "n": 3, "rlen": 30 + int(t * 400), "len": 500, "ready": "complete"})
        self.assertEqual(growing["how"], "ceiling", growing)
        self.assertFalse(growing["stable"])
        # a clock elsewhere on the page changes the document, not the rows
        ticking = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 1], probe=lambda t: {
            "n": 3, "rlen": 30, "len": 500 + int(t * 400), "ready": "complete"})
        self.assertEqual(ticking["how"], "ready", ticking)
        # with no row, the document is what must stand still
        empty = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 1], probe=lambda t: {
            "n": 0, "rlen": 0, "len": 500 + int(t * 400), "ready": "complete"})
        self.assertEqual(empty["how"], "ceiling", empty)
        # the recorded count (6) is not a condition of the wait
        short = self.drive_calls(self.DOC + self._list(0.1, 0.3), [0, 1], probe=lambda t: {
            "n": 2 if t >= 0.3 else 0, "rlen": 20, "len": 500, "ready": "complete"})
        self.assertEqual(short["how"], "ready", short)
        self.assertEqual(short["items"], 2)

    def test_the_probe_reads_the_rows_text_length(self):
        seen = []
        driver = CdpDriver("http://127.0.0.1:0")

        async def js(expr):
            seen.append(expr)
            return {"n": 1, "len": 10, "ready": "complete"}

        async def pump(_s):
            return None
        out = asyncio.run(driver._await_value_calls(
            js, pump, [], {"selector": "li.row", "value": "kafka", "rule": "value_calls",
                           "explaining": [], "value_calls": [], "poll_ms": 50, "stable_ms": 0},
            0.3, clock=iter([i * 0.05 for i in range(100)]).__next__))
        self.assertIn('querySelectorAll("li.row")', seen[0])
        self.assertIn("readyState", seen[0])
        self.assertIn("textContent", seen[0])
        # a page that does not report the rows' text length (rlen) counts it as constant
        self.assertEqual(out["how"], "ready", out)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
