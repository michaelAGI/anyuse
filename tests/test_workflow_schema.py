"""Tagged workflow values and v1 migration (offline)."""
import json

from anyuse.execution.api_compile import (
    compiled_record, migrate_compiled_record, proof_matches_template,
)
from anyuse.execution.api_step import (
    ApiResponse, ApiStepExecutor, PlanStep, RequestPlan, RequestTemplate,
)


class ChainTransport:
    def __init__(self, produced="abc-1"):
        self.produced = produced
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if "/search" in url:
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"items": [{"id": self.produced}]}))
        return ApiResponse(200, {"Content-Type": "application/json"},
                           json.dumps({"item": {"id": self.produced}}))


def _plan(max_length=32):
    first = RequestTemplate(
        url="https://fixture.example/search", query={"q": "{q}"}, params=["q"],
        extract={"items": "items[*].id"},
        expect={"status_in": [200], "path": "items", "min_items": 1})
    second = RequestTemplate(
        url="https://fixture.example/detail/{item_id}", params=["item_id"],
        extract={"id": "item.id"}, shape="fields",
        expect={"status_in": [200], "path": "item", "non_empty": True})
    plan = RequestPlan(
        steps=[PlanStep("search", first, {"item_id": "items[*].id"}),
               PlanStep("detail", second)], params=["q"], shape="fields")
    for row in plan.parameter_schema:
        if row["kind"] == "derived":
            row["max_length"] = max_length
    plan.__post_init__()
    return plan


def test_const_is_immutable_and_a_change_invalidates_the_proof():
    template = RequestTemplate(
        url="https://fixture.example/search",
        query={"q": "{q}", "limit": "10"}, params=["q"])
    record = compiled_record(template, {"passed": True})
    assert proof_matches_template(record)
    try:
        template.render({"q": "alpha", "const_query_limit": "20"})
    except ValueError as exc:
        assert "immutable" in str(exc)
    else:
        raise AssertionError("a caller changed a proved const")
    record["template"]["query"]["limit"] = "20"
    assert proof_matches_template(record) is False


def test_input_is_typed_bounded_domain_checked_and_structurally_encoded():
    template = RequestTemplate(
        url="https://fixture.example/search", query={"q": "{q}"}, params=["q"],
        parameter_schema=[{"name": "q", "kind": "input", "type": "string",
                           "min_length": 2, "max_length": 8,
                           "enum": ["a&b", "alpha"]}])
    url, _headers, _body = template.render({"q": "a&b"})
    assert url.endswith("q=a%26b")
    for bad in (1, "x", "too-long-value", "outside"):
        try:
            template.render({"q": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"unconstrained input passed: {bad!r}")


def test_derived_comes_only_from_named_earlier_output_and_is_scope_checked():
    plan = _plan()
    derived = [row for row in plan.parameter_schema if row["kind"] == "derived"]
    assert derived == [{
        "name": "item_id", "kind": "derived", "type": "string",
        "max_length": 32,
        "source": {"step": "search", "output": "items", "path": "items[*].id"},
        "destinations": ["detail"],
        "destination_origins": ["https://fixture.example"],
    }]
    executor = ApiStepExecutor(transport=ChainTransport())
    forged = executor.execute(plan.steps[1].template, {"item_id": "forged"})
    assert forged["success"] is False and "earlier step" in forged["error"]
    result = executor.execute(plan, {"q": "alpha"})
    assert result["success"] is True
    assert executor.transport.sent[-1].endswith("/detail/abc-1")


def test_untrusted_derived_value_is_type_and_size_checked_before_destination():
    result = ApiStepExecutor(transport=ChainTransport("x" * 33)).execute(
        _plan(max_length=32), {"q": "alpha"})
    assert result["success"] is False
    assert result["outcome_kind"] == "incomplete"
    assert "untrusted response binding refused" in result["error"]


def test_v1_placeholders_and_cursor_bindings_migrate_to_input_and_derived():
    old = {
        "version": 1,
        "template": {
            "steps": [
                {"name": "page 1", "url": "https://fixture.example/api",
                 "query": {"q": "{q}"}, "params": ["q"],
                 "bind": {"next_cursor": "meta.next_cursor"}},
                {"name": "page 2", "url": "https://fixture.example/api",
                 "query": {"q": "{q}", "cursor": "{next_cursor}"},
                 "params": ["q", "next_cursor"]},
            ],
            "params": ["q"], "shape": "list",
        },
        "proof": {"passed": True},
    }
    migrated = migrate_compiled_record(old)
    assert migrated["version"] == 2
    schema = migrated["template"]["parameter_schema"]
    assert [(row["name"], row["kind"]) for row in schema] == [
        ("q", "input"), ("next_cursor", "derived")]
    assert schema[1]["source"]["path"] == "meta.next_cursor"
    assert proof_matches_template(migrated)


def test_only_public_v1_kinds_are_accepted():
    for removed in ("model", "remembered"):
        template = RequestTemplate(
            url="https://fixture.example/", params=["choice"],
            parameter_schema=[{"name": "choice", "kind": removed, "type": "string"}])
        assert any("unsupported kind" in problem for problem in template.problems())


if __name__ == "__main__":
    import inspect
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
