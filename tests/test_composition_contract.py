"""Composition contracts through an in-memory HTTP transport; no sockets."""
from __future__ import annotations

import inspect
import json
import os
import urllib.parse
import httpx

from anyuse.execution.api_step import ApiStepExecutor, HttpxTransport, PlanStep, RequestPlan, RequestTemplate
from anyuse.execution.api_verify import compare_named_fields, verify_template
from anyuse.execution.navigation import (
    CompiledNavigationTemplate, navigate_to,
)


class Fixture:
    def respond(self, request):
        assert request.method == "GET" and str(request.url).startswith(self.origin + "/")
        parsed = urllib.parse.urlsplit(str(request.url))
        if parsed.path == "/search":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            payload = {"items": [{"id": f"{q}-1"}, {"id": f"{q}-2"}]}
        else:
            ident = parsed.path.rsplit("/", 1)[-1]
            q = ident.rsplit("-", 1)[0]
            payload = {"item": {"id": ident, "title": f"Title {q}",
                                "summary": f"Summary {q}"}}
        return httpx.Response(200, json=payload)

    def __enter__(self):
        self.origin = "http://127.0.0.1:8001"
        self.transport = HttpxTransport()
        self.transport._client = httpx.Client(transport=httpx.MockTransport(self.respond), follow_redirects=False)
        self.previous = os.environ.get("ANYUSE_LOCAL_FIXTURE_ORIGINS")
        os.environ["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = self.origin
        return self

    def __exit__(self, *_exc):
        self.transport.close()
        if self.previous is None:
            os.environ.pop("ANYUSE_LOCAL_FIXTURE_ORIGINS", None)
        else:
            os.environ["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = self.previous


def plan(origin):
    return RequestPlan(steps=[
        PlanStep("search", RequestTemplate(
            url=origin + "/search", query={"q": "{q}"}, params=["q"],
            extract={"items": "items[*].id"},
            expect={"status_in": [200], "path": "items", "min_items": 1}),
            {"id": "items[*].id"}),
        PlanStep("detail", RequestTemplate(
            url=origin + "/detail/{id}", params=["id"], shape="fields",
            extract={"id": "item.id", "title": "item.title",
                     "summary": "item.summary"},
            expect={"status_in": [200], "path": "item", "non_empty": True})),
    ], params=["q"], shape="fields")


def fresh_case(value):
    return {"value": value,
            "ui_fields": {"id": f"{value}-1", "title": f"Title {value}",
                          "summary": f"Summary {value}"},
            "bindings": {"id": f"{value}-1"}}


def test_chain_runs_and_proves_end_to_end_per_input_on_exact_loopback_fixture():
    with Fixture() as fixture:
        compiled = plan(fixture.origin)
        result = verify_template(compiled, [fresh_case("alpha"), fresh_case("beta")],
                                 ApiStepExecutor(transport=fixture.transport))
        assert result.passed, result.reason
        assert all(case.contract["kind"] == "named_fields" for case in result.cases)
        assert all(case.contract["intermediate_bindings"] == ["id"]
                   for case in result.cases)
        assert "intermediate bindings checked" in result.reason


def test_http_fixture_exception_is_exact_and_loopback_only():
    previous = os.environ.get("ANYUSE_LOCAL_FIXTURE_ORIGINS")
    try:
        os.environ["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = "http://127.0.0.1:8001"
        exact = RequestTemplate(url="http://127.0.0.1:8001/search")
        wrong_port = RequestTemplate(url="http://127.0.0.1:8002/search")
        private_lan = RequestTemplate(url="http://10.0.0.7:8001/search")
        assert not any("scheme" in problem for problem in exact.problems())
        assert any("scheme" in problem for problem in wrong_port.problems())
        os.environ["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = "http://10.0.0.7:8001"
        assert any("scheme" in problem for problem in private_lan.problems())
    finally:
        if previous is None:
            os.environ.pop("ANYUSE_LOCAL_FIXTURE_ORIGINS", None)
        else:
            os.environ["ANYUSE_LOCAL_FIXTURE_ORIGINS"] = previous


def test_swapped_field_meanings_cannot_pass_even_when_flattened_values_match():
    expected = {"id": "one", "title": "Alpha", "summary": "Beta"}
    actual = {"id": "one", "title": "Beta", "summary": "Alpha"}
    result = compare_named_fields(expected, actual)
    assert result.passed is False
    assert set(result.missing) == {"title", "summary"}


def test_wrong_intermediate_binding_fails_the_whole_per_input_contract():
    with Fixture() as fixture:
        bad = fresh_case("alpha")
        bad["bindings"] = {"id": "beta-1"}
        result = verify_template(plan(fixture.origin), [bad, fresh_case("beta")],
                                 ApiStepExecutor(transport=fixture.transport))
        assert result.passed is False
        assert "intermediate binding mismatch" in result.reason


def test_dataflow_contract_names_inputs_outputs_order_bindings_and_final_output():
    compiled = plan("https://fixture.example")
    contract = compiled.dataflow_contract()
    assert contract["inputs"] == ["q"]
    assert contract["steps"][0]["outputs"] == {"items": "items[*].id"}
    assert contract["steps"][0]["ordering"] == "preserved"
    assert contract["steps"][0]["bindings"][0]["source_output"] == "items"
    assert contract["steps"][0]["bindings"][0]["destinations"] == ["detail"]
    assert contract["ordering"] == "not_applicable"
    assert set(contract["final_output"]) == {"id", "title", "summary"}
    assert compiled.expression() == "detail(id=search(q).items[0])"


class Container:
    def __init__(self, owned=True):
        self.owned_container = owned
        self.urls = []

    def navigate(self, url):
        self.urls.append(url)
        return True


def test_navigate_to_accepts_only_a_pinned_template_and_owned_container():
    request = RequestTemplate(
        url="https://fixture.example/search", query={"q": "{q}"}, params=["q"])
    template = CompiledNavigationTemplate("search", request, "proof-1").pin()
    container = Container()
    result = navigate_to(template, {"q": "a&b"}, container)
    assert result["success"] is True
    assert container.urls == ["https://fixture.example/search?q=a%26b"]
    assert "url" not in inspect.signature(navigate_to).parameters
    denied = navigate_to(template, {"q": "x"}, Container(owned=False))
    assert denied["outcome_kind"] == "terminal_refusal"


def test_navigate_to_rejects_a_template_changed_after_proof():
    request = RequestTemplate(
        url="https://fixture.example/search", query={"q": "{q}"}, params=["q"])
    template = CompiledNavigationTemplate("search", request, "proof-1").pin()
    template.request.url = "https://other.example/{q}"
    result = navigate_to(template, {"q": "x"}, Container())
    assert result["success"] is False and "not pinned" in result["error"]


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    passed = failed = 0
    for name, fn in [(n, f) for n, f in vars(mod).items()
                     if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); passed += 1; print("OK  ", name)
        except Exception as exc:
            failed += 1; print("FAIL", name, "::", repr(exc)[:300])
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
