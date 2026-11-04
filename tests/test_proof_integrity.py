"""Proof integrity: every compile path splits fit/selection from validation
through ``execution/proof_split.py``.

Offline: fake executors and injected transports only. No socket, no browser,
no model call, no desktop.

For each path the two properties under test are:

* too few inputs -> the best status is ``checked (selection not held out)``,
  never ``proved``;
* selection (and fitting) never sees a validation input — asserted by
  instrumenting the executor (which values each phase actually requested) and
  the ground-truth derivation (which pages it was fitted on).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from anyuse.execution import proof_split as PS
from anyuse.execution import api_verify as V
from anyuse.execution.api_compile import CompileResult
from anyuse.execution.api_step import RequestTemplate

CHECKED = "checked (selection not held out)"


def rec(value, items=("alpha one", "alpha two", "alpha three"), **extra):
    out = {"param": {"name": "q", "value": value},
           "ui": {"items": [f"{value} {i}" for i in items]}}
    out.update(extra)
    return out


class SpyExecutor:
    """Answers every template with the case's own UI items; logs each call.

    ``fail_urls``: templates that return nothing (a failing winner)."""

    def __init__(self, fail_urls=()):
        self.calls = []                     # (url, value) in call order
        self.fail_urls = set(fail_urls)

    def execute(self, template, params):
        value = str(params.get("q", ""))
        self.calls.append((template.url, value))
        if template.url in self.fail_urls:
            return {"success": True, "extracted": {"items": ["nothing relevant"]}}
        items = [f"{value} {i}" for i in ("alpha one", "alpha two", "alpha three")]
        return {"success": True, "extracted": {"items": items}, "elapsed_ms": 1.0}


TOP = "https://h.test/a?q={q}"
ALT = "https://h.test/b?q={q}"


def fake_compile_with_runner_up(rec_, **kw):
    r = CompileResult(param_name="q", param_value=rec_["param"]["value"])
    r.template = RequestTemplate(method="GET", url=TOP, params=["q"], shape="list",
                                 extract={"items": "x[*]"})
    r.alternatives = [RequestTemplate(method="GET", url=ALT, params=["q"], shape="list",
                                      extract={"items": "y[*]"})]
    return r


class _PatchedCompile:
    def __enter__(self):
        self.orig = V.compile_recording
        V.compile_recording = fake_compile_with_runner_up
        return self

    def __exit__(self, *a):
        V.compile_recording = self.orig


# ---------------------------------------------------------------------------
# the primitive
# ---------------------------------------------------------------------------

class Primitive(unittest.TestCase):

    def test_four_inputs_hold_two_out(self):
        s = PS.assign(["c", "s", "v1", "v2"])
        self.assertTrue(s.held_out)
        self.assertEqual((s.compile, s.fitting, s.selection, s.validation),
                         ("c", [], ["s"], ["v1", "v2"]))

    def test_three_inputs_cannot_hold_out(self):
        s = PS.assign(["c", "v1", "v2"])
        self.assertFalse(s.held_out)
        self.assertEqual(s.selection, [])
        self.assertEqual(s.validation, ["v1", "v2"])
        self.assertEqual(s.status(True), CHECKED)
        self.assertEqual(s.status(False), "not proved")

    def test_assignment_is_deterministic_and_order_preserving(self):
        items = [f"x{i}" for i in range(7)]
        a, b = PS.assign(items), PS.assign(list(items))
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(a.validation, items[2:])

    def test_finalize_only_downgrades(self):
        s = PS.declared(rec("c"), [rec("v1"), rec("v2")])
        s.note_selection(["v1"])
        self.assertFalse(s.finalize())
        self.assertIn("also used", s.why)

    def test_derived_ground_truth_without_provenance_is_not_held_out(self):
        s = PS.declared(rec("c"), [rec("v1", ground_truth_selector="ul li"),
                                   rec("v2", ground_truth_selector="ul li")])
        self.assertFalse(s.finalize())
        ok = PS.declared(rec("c"), [
            rec("v1", ground_truth_selector="ul li", ground_truth_fitted_on=["c", "s"]),
            rec("v2", ground_truth_selector="ul li", ground_truth_fitted_on=["c", "s"])])
        self.assertTrue(ok.finalize())

    def test_scope_names_counts_and_status(self):
        text = PS.scope_text({"status": "proved", "case_count": 2,
                              "split": {"compile": 1, "fitting": 0, "selection": 1,
                                        "validation": 2, "held_out": True}})
        self.assertIn("2 held-out input(s)", text)
        self.assertIn("selection on 1", text)
        self.assertIn("status proved", text)
        weak = PS.scope_text({"status": CHECKED, "case_count": 2,
                              "split": {"held_out": False}})
        self.assertIn("not held out", weak)

    def test_weaker(self):
        self.assertEqual(PS.weaker(PS.PROVED, CHECKED), CHECKED)
        self.assertEqual(PS.weaker(CHECKED, PS.PROVED), CHECKED)
        self.assertEqual(PS.weaker(PS.PROVED, PS.PROVED), PS.PROVED)


# ---------------------------------------------------------------------------
# recording compile (compile_and_verify, roles by file name)
# ---------------------------------------------------------------------------

class RecordingPath(unittest.TestCase):

    def test_selection_never_sees_validation_inputs(self):
        ex = SpyExecutor(fail_urls={TOP})
        with _PatchedCompile():
            compiled, proof = V.compile_and_verify(
                rec("c0"), [rec("v1"), rec("v2")], executor=ex,
                selection_recordings=[rec("s1")])
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(compiled.template.url, ALT)
        validation = {"v1", "v2"}
        first_val = min(i for i, (_u, v) in enumerate(ex.calls) if v in validation)
        before = ex.calls[:first_val]
        self.assertTrue(before, "selection ran")
        self.assertFalse({v for _u, v in before} & validation)
        self.assertEqual({v for _u, v in before}, {"s1"})
        # validation ran once, on the chosen template only, after selection
        self.assertEqual(ex.calls[first_val:], [(ALT, "v1"), (ALT, "v2")])
        self.assertEqual(proof.effective_status, "proved")
        d = proof.to_dict()
        self.assertEqual(d["split"]["selection"], 1)
        self.assertEqual(d["split"]["validation"], 2)
        self.assertTrue(d["selection_held_out"])
        self.assertIn("2 held-out input(s)", d["scope"])

    def test_no_selection_input_means_no_runner_up_and_still_held_out(self):
        ex = SpyExecutor()
        with _PatchedCompile():
            compiled, proof = V.compile_and_verify(rec("c0"), [rec("v1"), rec("v2")],
                                                   executor=ex)
        self.assertEqual({u for u, _v in ex.calls}, {TOP})
        self.assertEqual(proof.effective_status, "proved")

    def test_too_few_validation_inputs_is_not_proved(self):
        with _PatchedCompile():
            _c, proof = V.compile_and_verify(rec("c0"), [rec("v1")], executor=SpyExecutor())
        self.assertFalse(proof.passed)
        self.assertEqual(proof.to_dict()["status"], "not proved")

    def test_validation_with_a_derived_ground_truth_of_unknown_origin_is_checked(self):
        """Bench recorders derived the selector across rec + fresh1 (dry look)."""
        with _PatchedCompile():
            _c, proof = V.compile_and_verify(
                rec("c0", ground_truth_selector="ul li"),
                [rec("v1", ground_truth_selector="ul li"),
                 rec("v2", ground_truth_selector="ul li")], executor=SpyExecutor())
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(proof.effective_status, CHECKED)
        self.assertIn("not held out", proof.scope)

    def test_a_validation_value_repeating_a_selection_value_is_dropped(self):
        with _PatchedCompile():
            _c, proof = V.compile_and_verify(
                rec("c0"), [rec("s1"), rec("v1"), rec("v2")], executor=SpyExecutor(),
                selection_recordings=[rec("s1")])
        self.assertEqual([c.value for c in proof.cases], ["v1", "v2"])
        self.assertEqual(proof.effective_status, "proved")


    def test_path_binding_is_corroborated_by_selection_never_by_validation(self):
        """1 compile + 1 fitting + 1 selection + 2 validation (the re-proof
        layout): a value spanning two path segments needs three corroborating
        recordings; the selection input completes them, validation never does."""
        seen = {}

        def spy(rec_, **kw):
            seen["binding"] = kw.get("binding")
            return fake_compile_with_runner_up(rec_, **kw)

        def page(value):
            return rec(value, final_url=f"https://h.test/port/{value}")
        orig = V.compile_recording
        V.compile_recording = spy
        try:
            _c, proof = V.compile_and_verify(
                page("www/nginx"), [page("sec/sudo"), page("ftp/curl")],
                executor=SpyExecutor(), fitting_recordings=[page("net/rsync")],
                selection_recordings=[page("vcs/git")])
        finally:
            V.compile_recording = orig
        ev = seen["binding"]
        self.assertEqual(ev.n_recordings, 3)
        self.assertTrue(ev.supported, ev.notes)
        self.assertTrue(proof.split["held_out"], proof.split)
        self.assertEqual(proof.effective_status, "proved")


# ---------------------------------------------------------------------------
# HAR compile
# ---------------------------------------------------------------------------

class HarPath(unittest.TestCase):

    def test_har_proof_carries_status_and_scope_and_fits_on_the_first_file_only(self):
        from test_har_import import api_body, entry, har, write
        from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
        from anyuse.execution.har_import import HarCase, compile_from_har
        import urllib.parse
        sets = {"alpha": ["Alpha Pkg", "Beta Pkg", "Gamma Pkg", "Delta Pkg"],
                "beta": ["Beta One", "Beta Two", "Beta Three", "Beta Four"],
                "gamma": ["Gamma One", "Gamma Two", "Gamma Three"]}
        seen = []

        class T:
            def send(self, method, url, headers, body, timeout_s):
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
                seen.append(q)
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   api_body(sets[q]))
        with tempfile.TemporaryDirectory() as tmp:
            cases = [HarCase(write(tmp, f"{v}.har",
                                   har([entry(url=f"https://ex.test/api?q={v}",
                                              text=api_body(sets[v]))])), v, sets[v])
                     for v in sets]
            _c, proof, _r = compile_from_har(cases, param_name="q",
                                             executor=ApiStepExecutor(transport=T()),
                                             preflight=lambda _c: "")
        self.assertTrue(proof.passed, proof.reason)
        from anyuse.execution import empty_page as EP
        # only the final check sends, then the made-up searches that learn the empty page
        self.assertEqual(seen[:2], ["beta", "gamma"])
        self.assertEqual(len(seen), 2 + EP.PROBES)
        self.assertFalse(set(seen[2:]) & set(sets))
        d = proof.to_dict()
        self.assertEqual(d["status"], "proved")
        self.assertEqual((d["split"]["fitting"], d["split"]["selection"],
                          d["split"]["validation"]), (0, 0, 2))


# ---------------------------------------------------------------------------
# learn loop (confirm-once) and discovery's use of it
# ---------------------------------------------------------------------------

class LearnPath(unittest.TestCase):

    def _learn(self, others):
        import test_learn_loop as T
        from anyuse.execution import learn_loop
        fitted = []
        real = learn_loop.derive_ground_truth

        def spy(recordings):
            fitted.append([r["param"]["value"] for r in recordings])
            return real(recordings)
        learn_loop.derive_ground_truth = spy
        try:
            res, _a, _n = T.do_learn(T.SERDE, others)
        finally:
            learn_loop.derive_ground_truth = real
        return res, fitted

    def test_three_values_earn_at_most_checked(self):
        import test_learn_loop as T
        res, fitted = self._learn([T.REGEX, T.TOKIO])
        self.assertEqual(res.outcome, "tier1", res.reason)
        self.assertEqual(res.proof_status, CHECKED)
        self.assertEqual(res.proof["status"], CHECKED)
        self.assertEqual(fitted, [["serde", "regex", "tokio"]])

    def test_four_values_hold_validation_out_of_fitting_and_selection(self):
        import test_learn_loop as T
        rand = T.nav_for("rand", ["rand v0", "rand_core v0", "rand_chacha v0"])
        res, fitted = self._learn([T.REGEX, T.TOKIO, rand])
        self.assertEqual(res.outcome, "tier1", res.reason)
        self.assertEqual(fitted, [["serde", "regex"]])
        self.assertEqual([c["value"] for c in res.proof["cases"]], ["tokio", "rand"])
        self.assertEqual(res.proof_status, "proved")
        self.assertEqual(res.split["selection"], 1)


# ---------------------------------------------------------------------------
# attach recorder
# ---------------------------------------------------------------------------

class AttachPath(unittest.TestCase):

    def _run(self, rows):
        from test_attach import RecordedTransport, record
        from anyuse.execution import attach as A
        from anyuse.execution.api_step import ApiStepExecutor
        sessions = [record(v, r)[0].to_session(f"20260923T12000{i}-0000000{i}")
                    for i, (v, r) in enumerate(rows.items())]
        recs = [A.session_to_recording(s, "q", v, f"t{i}")
                for i, (s, v) in enumerate(zip(sessions, rows))]
        inner = ApiStepExecutor(transport=RecordedTransport(recs))
        calls = []

        class Spy:
            def execute(self, template, params):
                calls.append(str(params.get("q")))
                return inner.execute(template, params)
        return A.compile_sessions(sessions, executor=Spy(),
                                  preflight=lambda c: ""), calls

    def test_three_sessions_are_checked(self):
        from test_attach import ROWS
        (_c, proof, report, _r), _calls = self._run(ROWS)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(report["status"], CHECKED)
        self.assertEqual(proof.to_dict()["status"], CHECKED)
        self.assertIn("not held out", report["scope"])

    def test_four_sessions_prove_and_validation_is_requested_only_in_the_final_check(self):
        from test_attach import ROWS4
        (_c, proof, report, recs), calls = self._run(ROWS4)
        self.assertEqual(report["status"], "proved", proof.reason)
        self.assertEqual(calls[-2:], ["tokio", "rand"])
        self.assertNotIn("tokio", calls[:-2])
        self.assertNotIn("rand", calls[:-2])
        self.assertEqual(recs[2]["ground_truth_fitted_on"], ["serde", "regex"])


# ---------------------------------------------------------------------------
# recompile / self-heal
# ---------------------------------------------------------------------------

class RecompilePath(unittest.TestCase):

    def _store(self, tmp, proof):
        from anyuse.execution.api_compile import compiled_record, persist_compiled

        class Storage:
            def get_workflow_dir(self, wid):
                return os.path.join(tmp, wid)
        storage = Storage()
        t = RequestTemplate(method="GET", url=TOP, params=["q"], shape="list",
                            extract={"items": "x[*]"})
        record = compiled_record(t, proof, workflow="wf")
        record["health"] = {"preferred": True, "consecutive_failures": 99,
                            "total_runs": 100}
        persist_compiled(storage, "wf", record)
        return storage

    def _recompile(self, prior_proof):
        from anyuse.execution.api_recompile import RecompileManager, RecompilePolicy
        from anyuse.execution.api_compile import load_compiled, proof_matches_template
        with tempfile.TemporaryDirectory() as tmp, _PatchedCompile():
            storage = self._store(tmp, prior_proof)
            loader = lambda _wid: (rec("c0"), [], [rec("v1"), rec("v2")])  # noqa: E731
            r = RecompileManager(storage, loader, executor=SpyExecutor(),
                           policy=RecompilePolicy(), proof_preflight=lambda c: "")
            out = r.maybe_recompile("wf")
            record = load_compiled(storage, "wf")
            return out, record, proof_matches_template(record)

    def test_a_recompile_never_upgrades_a_checked_record(self):
        out, record, current = self._recompile({"passed": True, "status": CHECKED})
        self.assertTrue(out.persisted, out.reason)
        self.assertEqual(record["proof"]["status"], CHECKED)
        self.assertFalse(record["proof"]["split"]["held_out"])
        self.assertIn("not held out", record["proof"]["scope"])
        self.assertTrue(current, "new evidence is pinned to the new template")

    def test_a_recompile_of_a_proved_record_stays_proved_on_held_out_input(self):
        out, record, _cur = self._recompile({"passed": True, "status": "proved"})
        self.assertTrue(out.persisted, out.reason)
        self.assertEqual(record["proof"]["status"], "proved")


# ---------------------------------------------------------------------------
# MCP surfaces and the registry
# ---------------------------------------------------------------------------

class Surfaces(unittest.TestCase):

    def test_legacy_learned_record_without_split_is_checked(self):
        from anyuse.mcpserver.compiled_tools import proof_status_of
        self.assertEqual(proof_status_of({"proof": {"passed": True},
                                          "source": {"origin": "agent"}}), CHECKED)
        self.assertEqual(proof_status_of({"proof": {"passed": True},
                                          "source": {"origin": "recording"}}), "proved")
        self.assertEqual(proof_status_of({"proof": {"passed": True, "status": "proved",
                                                    "split": {"held_out": True}},
                                          "source": {"origin": "agent"}}), "proved")

    def test_describe_and_explain_carry_scope_and_split(self):
        from anyuse.mcpserver.compiled_tools import describe_compiled, proof_scope
        from anyuse.execution.api_compile import compiled_record
        t = RequestTemplate(method="GET", url="https://h.test/a?q={q}", params=["q"],
                            shape="list", extract={"items": "x[*]"})
        proof = {"passed": True, "case_count": 2, "status": CHECKED,
                 "split": {"compile": 1, "fitting": 2, "selection": 0,
                           "validation": 2, "held_out": False}}
        record = compiled_record(t, proof, workflow="wf")
        row = describe_compiled("wf", record)
        self.assertEqual(row["proof"]["status"], CHECKED)
        self.assertEqual(row["proof"]["split"]["validation"], 2)
        self.assertIn("not held out", row["proof"]["scope"])
        self.assertIn("not held out", proof_scope(t, proof, record))

    def test_registry_counts_only_held_out_inputs(self):
        from anyuse.registry import client as C
        legacy = {"status": "proved", "values": ["a", "b", "c"], "proof": {}}
        self.assertEqual(C._proof_summary(legacy, "2026-09-24", "0")["status"],
                         "discovered")
        split = {"status": "proved", "values": ["a", "b", "c", "d"],
                 "proof": {"validation_values": ["c", "d"]}}
        got = C._proof_summary(split, "2026-09-24", "0")
        self.assertEqual((got["status"], got["fresh_inputs"]), ("proved", 2))


if __name__ == "__main__":
    unittest.main()
