"""Step-level drift repair (execution/step_repair.py).

A synthetic, deterministic site with switchable drifts: every request is
answered in-process by an injected transport. Each test checks that the
failing step is localised, that ONLY that step is re-learned from ONE capture
of the anchor's value, that the re-learned tool is re-proved on held-out
inputs with the shared split before it is trusted, and that wrong data stays
impossible (a candidate that fits only the anchor page is not trusted).

Offline: no desktop, no network, no model.
"""
import json
import os
import tempfile
import urllib.parse

from anyuse.execution import proof_split as PS
from anyuse.execution import result_contract as RC
from anyuse.execution import step_repair as SR
from anyuse.execution.api_compile import compiled_record, load_compiled, persist_compiled
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate

VALUES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]


def names(q, n=6):
    return [f"{q}-lib-{i}" for i in range(n)]


def rows_html(q, drift):
    items = names(q)
    if drift == "renamed":            # a class rename on rows and name links
        body = "".join(f"<tr class='entry'><td><a class='title'>{n}</a></td>"
                       f"<td class='d'>v1</td></tr>" for n in items)
        region = f"<table class='results'><tbody>{body}</tbody></table>"
    elif drift == "moved":            # the region moved and its markup changed
        body = "".join(f"<div class='hit'><h4>{n}</h4><p>v1</p></div>" for n in items)
        region = f"<p>intro</p><section class='hits'>{body}</section>"
    else:
        body = "".join(f"<tr class='row'><td><a class='name'>{n}</a></td>"
                       f"<td class='d'>v1</td></tr>" for n in items)
        region = f"<table class='results'><tbody>{body}</tbody></table>"
    sidebar = ""
    if drift == "trap":
        # page furniture that shows the ANCHOR's items on every page (stale
        # "recent searches"); the real list moved as in "moved"
        body = "".join(f"<div class='hit'><h4>{n}</h4><p>v1</p></div>" for n in items)
        region = f"<section class='hits'>{body}</section>"
        sidebar = ("<aside><ul class='recent'>" +
                   "".join(f"<li class='r'>{n}</li>" for n in names("alpha")) +
                   "</ul></aside>")
    return (f"<html><body><header><a>Home</a><a>About</a></header><main>"
            f"<h1>Results for {q}</h1>{region}</main>{sidebar}"
            f"<footer><a>Terms</a></footer></body></html>")


def cards_html(q, drift):
    cards = "".join(f"<article class='card'><h3><a>{n}</a></h3><p>on {n}</p></article>"
                    for n in names(q))
    if drift == "wrapped":
        cards = f"<div class='grid'>{cards}</div>"
    return f"<html><body><main><h1>{q}</h1><div id='results'>{cards}</div></main></body></html>"


def json_doc(q, drift):
    rows = [{"name": n, "version": "1.0"} for n in names(q)]
    if drift == "renamed_key":
        rows = [{"pkgname": r["name"], "version": "1.0"} for r in rows]
    doc = {"query": q, "total": len(rows), "results": rows}
    if drift == "wrapper":
        doc = {"data": doc}
    return doc


class Site:
    """``https://ex.test``: /search?q= (HTML list), /cards?q= (region list),
    /api?q= (JSON), with a switchable drift."""

    def __init__(self, drift=None):
        self.drift = drift
        self.log = []

    def send(self, method, url, headers, body, timeout_s):
        self.log.append(url)
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        html = {"content-type": "text/html; charset=utf-8"}
        if self.drift == "param_renamed" and p.path == "/search":
            v = q.get("query")
            if not v:
                return ApiResponse(200, html, "<html><body><h1>Popular</h1><p>Browse all "
                                              "libraries</p></body></html>")
            return ApiResponse(200, html, rows_html(v, None))
        if self.drift == "endpoint_moved" and p.path == "/search":
            return ApiResponse(404, html, "<html><body>Not found</body></html>")
        if p.path in ("/search", "/find") and q.get("q"):
            return ApiResponse(200, html, rows_html(q["q"], self.drift))
        if p.path == "/cards" and q.get("q"):
            return ApiResponse(200, html, cards_html(q["q"], self.drift))
        if p.path == "/api" and q.get("q"):
            return ApiResponse(200, {"content-type": "application/json"},
                               json.dumps(json_doc(q["q"], self.drift)))
        if p.path == "/ui" and q.get("q"):        # the page a person sees for the JSON tool
            return ApiResponse(200, html, rows_html(q["q"], None))
        return ApiResponse(404, html, "<html><body>Not found</body></html>")


def html_tool(selector="table.results tbody tr.row a.name", path="/search"):
    t = RequestTemplate(method="GET", url=f"https://ex.test{path}", query={"q": "{q}"},
                        extract={"items": selector}, extract_kind="html", shape="list",
                        params=["q"], expect={"status_in": [200], "content_type": "html",
                                              "selector": selector, "min_items": 6})
    RC.attach(t)
    return t


def json_tool():
    t = RequestTemplate(method="GET", url="https://ex.test/api", query={"q": "{q}"},
                        extract={"items": "results[*].name"}, extract_kind="json",
                        shape="list", params=["q"],
                        expect={"status_in": [200], "content_type": "json", "path": "results",
                                "min_items": 6, "required_keys": ["name"]})
    RC.attach(t)
    return t


def record(template, anchor_value="alpha", site_for_anchor=None):
    rec = compiled_record(template, {"passed": True, "status": PS.PROVED,
                                     "cases": [], "case_count": 2}, workflow="w")
    site = site_for_anchor or Site()
    out = ApiStepExecutor(transport=site).execute(template, {"q": anchor_value})
    assert out["success"], out.get("error")
    assert SR.note_success(rec, anchor_value, out)
    return rec


def executor(site):
    return ApiStepExecutor(transport=site)


def run_on(rec_or_t, site, value):
    t = rec_or_t
    if isinstance(rec_or_t, dict):
        from anyuse.execution.api_step import load_command
        t = load_command(rec_or_t["template"])
    return executor(site).execute(t, {"q": value})


def repaired_items_exact(new_rec, site, value, want):
    out = run_on(new_rec, site, value)
    return out["success"] and out["extracted"]["items"] == want


# ---------------------------------------------------------------------------

def test_the_drift_breaks_the_old_tool_explicitly():
    rec = record(html_tool())
    for drift in ("renamed", "moved"):
        out = run_on(rec, Site(drift), "delta")
        assert not out["success"] and "result contract refused" in out["error"], drift


def test_a_renamed_class_relearns_only_the_extraction():
    rec = record(html_tool())
    site = Site("renamed")
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon", "zeta"])
    assert new is not None, out.reason
    assert out.localised == "extraction_region"
    assert out.relearned == ["extraction"] and "request" in out.reused
    assert out.status == PS.PROVED and out.proof["split"]["held_out"]
    assert out.proof["split"]["validation"] == 3 and out.proof["split"]["selection"] == 1
    assert new["template"]["url"] == rec["template"]["url"]          # request reused
    assert new["repair"]["captures"] == 1 and new["repair"]["model_calls"] == 0
    for v in ("delta", "epsilon", "zeta"):                            # exact, same granularity
        assert repaired_items_exact(new, site, v, names(v)), v


def test_a_moved_region_with_new_markup_is_relearned_at_the_same_granularity():
    rec = record(html_tool())
    site = Site("moved")
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon"])
    assert new is not None, out.reason
    assert out.localised == "extraction_region"
    # names, not "name + about name" rows
    assert repaired_items_exact(new, site, "delta", names("delta"))


def test_merged_records_are_localised_as_segmentation():
    rec = record(html_tool("div#results > *", path="/cards"))
    site = Site("wrapped")
    before = run_on(rec, site, "delta")
    assert not before["success"] or len(before["extracted"]["items"]) != 6
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon"])
    assert out.localised == "record_segmentation", out.evidence
    assert new is not None, out.reason
    got = run_on(new, site, "delta")
    assert got["success"] and len(got["extracted"]["items"]) == 6


def ui_page(site):
    def page(v):
        r = site.send("GET", f"https://ex.test/ui?q={v}", {}, None, 5)
        return f"https://ex.test/ui?q={v}", r.text
    return page


def test_a_renamed_json_key_and_an_added_wrapper_relearn_the_path():
    for drift, path in (("renamed_key", "results[*].pkgname"),
                        ("wrapper", "data.results[*].name")):
        rec = record(json_tool())
        site = Site(drift)
        assert not run_on(rec, site, "delta")["success"], drift
        new, out = SR.repair(rec, executor=executor(site), selection="beta",
                             held_out=["delta", "epsilon"], ui_page=ui_page(site))
        assert new is not None, (drift, out.reason)
        assert out.localised == "extraction_region" and out.relearned == ["extraction"]
        assert new["template"]["extract"]["items"] == path, drift
        assert repaired_items_exact(new, site, "delta", names("delta")), drift


def test_a_renamed_parameter_is_rebound_from_one_navigation():
    rec = record(html_tool())
    site = Site("param_renamed")

    def navigate(v):
        url = f"https://ex.test/search?query={v}"
        return {"final_url": url, "dom": rows_html(v, None), "network": []}
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon"], navigate=navigate)
    assert out.localised == "parameter_binding", out.evidence
    assert new is not None, out.reason
    assert out.relearned == ["parameter_binding"] and "extraction" in out.reused
    assert new["template"]["query"] == {"query": "{q}"}
    assert repaired_items_exact(new, site, "delta", names("delta"))


def test_a_moved_endpoint_is_relearned_from_one_navigation_capture():
    rec = record(html_tool())
    site = Site("endpoint_moved")

    def navigate(v):
        url = f"https://ex.test/find?q={v}"
        body = rows_html(v, None)
        return {"final_url": url, "dom": body,
                "network": [{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                             "body": body, "resource_type": "Document"}]}
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon"], navigate=navigate)
    assert out.localised == "request_shape", out.evidence
    assert new is not None, out.reason
    assert new["template"]["url"] == "https://ex.test/find"
    assert repaired_items_exact(new, site, "delta", names("delta"))


def test_without_localisation_or_capture_only_the_full_pipeline_runs():
    calls = []
    rec = record(html_tool())
    rec.pop("drift_anchor")
    new, out = SR.repair(rec, executor=executor(Site("renamed")), selection="beta",
                         held_out=["delta", "epsilon"], full_pipeline=calls.append)
    assert new is None and out.fallback == "full_pipeline" and "no drift anchor" in calls[0]
    calls.clear()
    rec = record(html_tool())
    new, out = SR.repair(rec, executor=executor(Site("param_renamed")), selection="beta",
                         held_out=["delta", "epsilon"], full_pipeline=calls.append)
    assert new is None and out.localised == "parameter_binding" and calls


def test_a_working_tool_is_not_repaired():
    rec = record(html_tool())
    new, out = SR.repair(rec, executor=executor(Site()), selection="beta",
                         held_out=["delta", "epsilon"])
    assert new is None and out.localised == "none" and not out.fallback


def test_a_candidate_that_only_fits_the_anchor_page_is_never_trusted():
    """The drifted page carries a stale 'recent searches' box that shows the
    anchor's items verbatim. On the anchor page alone both it and the real
    list reproduce the anchor; only the held-out re-proof tells them apart."""
    rec = record(html_tool())
    site = Site("trap")
    new, out = SR.repair(rec, executor=executor(site), selection="beta",
                         held_out=["delta", "epsilon"])
    if new is not None:
        for v in ("delta", "epsilon", "zeta"):
            assert repaired_items_exact(new, site, v, names(v)), \
                "a repaired tool returned the stale box: wrong data"
    else:
        assert "re-proof failed" in out.reason or "no re-derived" in out.reason


def test_selection_and_validation_reject_an_anchor_only_fit():
    """Forced: the stale box IS offered as a candidate. It reproduces the
    anchor exactly on the anchor page, so only the shared split stops it:
    the selection page rejects it, and alone it fails the re-proof."""
    site = Site("trap")
    base = html_tool()
    box = SR.with_extraction(base, "aside ul.recent li.r", "test: the stale box")
    real = SR.with_extraction(base, "main section.hits div.hit h4", "test: the list")
    cap = SR.capture(base, "alpha", executor(site))
    assert SR.exact_cover(names("alpha"), SR._old_items(box, cap))[0] == 1.0
    vals = ["alpha", "beta", "delta", "epsilon"]
    chosen, proof = SR.reprove(box, vals, executor(site), choose=[box, real])
    assert proof.passed and chosen.extract["items"] == real.extract["items"]
    _chosen, proof = SR.reprove(box, vals, executor(site))
    assert not proof.passed, "an anchor-only fit must never be proved"


def test_record_containment_reads_fields_inside_records_in_order():
    """A record-level ground truth (whole rows) against a field-level tool
    (cells): each value must sit inside a record, in order; values past the
    last compared record are outside the window, not misses."""
    from anyuse.execution.api_verify import compare, record_containment
    rows = [f"lib{i} by someone a long description of library number {i} 1 {i} 0 {i}00"
            for i in range(12)]
    cells = []
    for i in range(12):
        cells += [f"a long description of library number {i}", f"1 {i} 0", f"{i}00"]
    cov, prec, _ = record_containment(rows[:10], cells)
    assert cov == 1.0 and prec == 1.0
    cr = compare(rows, cells, value="lib", records=True)
    assert cr.passed and cr.contract["kind"] == "record_containment"
    assert not compare(rows, cells, value="lib").passed          # off unless asked for
    shuffled = cells[3:] + cells[:3]
    assert not compare(rows, list(reversed(cells)), value="lib", records=True).passed
    chrome = ["Home", "About us and our team", "Contact the maintainers"] * 10
    assert not compare(rows, chrome, value="lib", records=True).passed
    assert shuffled


def test_the_new_status_never_exceeds_the_old_one():
    rec = record(html_tool())
    rec["proof"]["status"] = PS.CHECKED
    new, out = SR.repair(rec, executor=executor(Site("renamed")), selection="beta",
                         held_out=["delta", "epsilon"])
    assert new is not None and new["proof"]["status"] == PS.CHECKED


def test_too_few_held_out_inputs_cannot_prove():
    rec = record(html_tool())
    new, out = SR.repair(rec, executor=executor(Site("renamed")), selection="beta",
                         held_out=["delta"])
    assert new is None and "re-proof failed" in out.reason


def test_the_anchor_is_kept_from_list_answers_only():
    rec = {}
    assert not SR.note_success(rec, "q", {"extracted": {"items": ["a", "b"]},
                                          "result_contract": {"kind": "sparse"}})
    assert not SR.note_success(rec, "q", {"extracted": {"items": ["a", "b"]}})
    assert SR.note_success(rec, "q", {"extracted": {"items": ["a", "b", "c"]},
                                      "result_contract": {"kind": "list"}})
    assert rec["drift_anchor"]["items"] == ["a", "b", "c"]


def test_compile_time_anchor_is_stored_on_the_record_not_in_the_proof():
    from anyuse.execution.api_verify import EquivalenceResult, CaseResult
    proof = EquivalenceResult(passed=True, cases=[CaseResult(value="x", passed=True)],
                              anchor={"value": "x", "items": ["a", "b", "c"], "count": 3})
    rec = compiled_record(html_tool(), proof.to_dict(), workflow="w")
    assert rec["drift_anchor"]["value"] == "x" and "drift_anchor" not in rec["proof"]


class _Storage:
    def __init__(self, root):
        self.root = root

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.root, workflow_id)


def test_the_recompile_manager_tries_the_step_first():
    from anyuse.execution.api_recompile import RecompileManager
    with tempfile.TemporaryDirectory() as d:
        storage = _Storage(d)
        rec = record(html_tool())
        rec["health"] = {"consecutive_failures": 3, "total_runs": 3, "total_failures": 3,
                         "preferred": True}
        persist_compiled(storage, "w", rec)
        loader_calls = []
        mgr = RecompileManager(storage, lambda wid: loader_calls.append(wid),
                               executor=executor(Site("renamed")))
        out = mgr.maybe_repair("w", held_out=["delta", "epsilon"], selection="beta")
        assert out.persisted and "step-level repair" in out.reason, out.reason
        assert not loader_calls, "the full recompile must not run when the step repair proves"
        back = load_compiled(storage, "w")
        assert back["repair"]["localised"] == "extraction_region"
        assert back["proof"]["template_digest"]


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:400])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
