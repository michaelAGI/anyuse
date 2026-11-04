"""Page 2 of a proved listing: deriving the mechanism, emitting it, and holding
it to the same fresh-input proof as page 1.

Offline: synthetic responses and an injected transport. No desktop, no network.
"""
import json

from anyuse.execution.api_step import (
    ApiResponse,
    ApiStepExecutor,
    RequestPlan,
    RequestTemplate,
)
from anyuse.execution.api_verify import verify_template
from anyuse.execution.pagination import (
    Pagination,
    compile_next_page,
    derive_pagination,
    next_page_params,
    paginated_plan,
    paginated_template,
)

PAGE1 = ["Alpha Pkg", "Beta Pkg", "Gamma Pkg"]
PAGE2 = ["Delta Pkg", "Epsilon Pkg", "Zeta Pkg"]


def _tmpl(query=None, url="https://ex.test/api/search", **kw):
    return RequestTemplate(
        method="GET", url=url,
        query=query if query is not None else {"q": "{q}", "page": "1"},
        headers={"accept": "application/json"},
        extract={"items": "results[*].name"}, shape="list",
        expect={"status_in": [200], "content_type": "json",
                "path": "results", "min_items": 1},
        params=["q"], **kw)


def _numbered(names, page=1, total_pages=4, per_page=3):
    return {"page": page, "total_pages": total_pages, "per_page": per_page,
            "results": [{"name": n} for n in names]}


def _cursored(names, cursor="c2"):
    body = {"results": [{"name": n} for n in names]}
    if cursor:
        body["next_cursor"] = cursor
    return body


# ---------------------------------------------------------------------------
# deriving the mechanism — evidence, not guessing
# ---------------------------------------------------------------------------

def test_a_page_key_plus_response_evidence_is_a_numeric_pagination():
    pag, why = derive_pagination(_tmpl(), _numbered(PAGE1),
                                 "https://ex.test/api/search?q=pkg&page=1")
    assert pag is not None, why
    assert (pag.kind, pag.key, pag.unit, pag.first_value) == ("param", "page", "page", 1)
    assert pag.page_size == 3
    assert "total_pages" in pag.evidence


def test_a_page_key_WITHOUT_response_evidence_is_refused():
    """A query key called 'page' is a guess until the server says it paginates."""
    pag, why = derive_pagination(_tmpl(), {"results": [{"name": n} for n in PAGE1]},
                                 "https://ex.test/api/search?q=pkg&page=1")
    assert pag is None
    assert "would be a guess" in why, why


def test_an_offset_key_needs_a_stated_page_size():
    tmpl = _tmpl(query={"q": "{q}", "offset": "0"})
    # total present, per_page absent → the stride for page 2 is unknown
    pag, why = derive_pagination(tmpl, {"total": 40, "results": [{"name": n} for n in PAGE1]},
                                 "https://ex.test/api/search?q=pkg&offset=0")
    assert pag is None and "stride for page 2 is unknown" in why, why

    pag, why = derive_pagination(tmpl, {"total": 40, "limit": 20,
                                        "results": [{"name": n} for n in PAGE1]},
                                 "https://ex.test/api/search?q=pkg&offset=0")
    assert pag is not None, why
    assert (pag.unit, pag.key, pag.page_size) == ("offset", "offset", 20)


def test_a_cursor_in_the_response_beats_a_numeric_key():
    tmpl = _tmpl()                      # carries page=1 as well
    pag, why = derive_pagination(tmpl, _cursored(PAGE1),
                                 "https://ex.test/api/search?q=pkg&page=1")
    assert pag is not None, why
    assert pag.kind == "cursor" and pag.cursor_path == "next_cursor"
    assert pag.cursor_key == "next_cursor"


def test_a_next_page_url_tells_us_the_key_the_server_uses():
    body = {"results": [{"name": n} for n in PAGE1],
            "next": "https://ex.test/api/search?q=pkg&after=eyJpZCI6OTl9"}
    pag, why = derive_pagination(_tmpl(query={"q": "{q}"}), body,
                                 "https://ex.test/api/search?q=pkg")
    assert pag is not None, why
    assert pag.kind == "cursor" and pag.cursor_key == "after", pag.to_dict()


def test_a_cross_origin_next_url_is_refused():
    body = {"results": [{"name": n} for n in PAGE1],
            "next": "https://cdn.other.test/api/search?q=pkg&after=x"}
    pag, why = derive_pagination(_tmpl(query={"q": "{q}"}), body,
                                 "https://ex.test/api/search?q=pkg")
    assert pag is None
    assert "cross-origin hop" in why, why


def test_a_numeric_next_is_treated_as_a_page_index_not_a_cursor():
    body = {"page": 1, "next": 2, "total_pages": 5, "per_page": 3,
            "results": [{"name": n} for n in PAGE1]}
    pag, why = derive_pagination(_tmpl(), body, "https://ex.test/api/search?q=pkg&page=1")
    assert pag is not None and pag.kind == "param", (pag, why)


def test_an_html_selector_rung_is_refused_rather_than_guessed_at():
    tmpl = _tmpl()
    tmpl.extract_kind = "html"
    pag, why = derive_pagination(tmpl, _numbered(PAGE1))
    assert pag is None and "no structured response" in why


def test_a_response_with_nothing_to_go_on_says_so():
    pag, why = derive_pagination(_tmpl(query={"q": "{q}"}),
                                 {"results": [{"name": n} for n in PAGE1]})
    assert pag is None and "no continuation token" in why


# ---------------------------------------------------------------------------
# what page N means
# ---------------------------------------------------------------------------

def test_page_numbers_and_offsets_are_computed_from_the_recorded_first_page():
    by_page = Pagination(kind="param", key="page", unit="page", first_value=1, page_size=20)
    assert next_page_params(by_page, 2) == {"page": 2}
    assert next_page_params(by_page, 5) == {"page": 5}

    zero_based = Pagination(kind="param", key="p", unit="page", first_value=0, page_size=20)
    assert next_page_params(zero_based, 2) == {"p": 1}, "a 0-based API must stay 0-based"

    by_offset = Pagination(kind="param", key="offset", unit="offset",
                           first_value=0, page_size=20)
    assert next_page_params(by_offset, 2) == {"offset": 20}
    assert next_page_params(by_offset, 3) == {"offset": 40}

    assert next_page_params(Pagination(kind="cursor"), 2) == {}


# ---------------------------------------------------------------------------
# emitting page 2
# ---------------------------------------------------------------------------

def test_a_numeric_pagination_stays_ONE_request():
    out, pag, why = compile_next_page(_tmpl(), _numbered(PAGE1),
                                      "https://ex.test/api/search?q=pkg&page=1")
    assert why == "" and isinstance(out, RequestTemplate)
    assert out.query["page"] == "{page}"
    assert "page" in out.params and "q" in out.params
    assert out.problems() == []
    assert "never loops" in out.notes


def test_a_cursor_pagination_becomes_a_two_step_plan():
    out, pag, why = compile_next_page(_tmpl(query={"q": "{q}"}), _cursored(PAGE1))
    assert why == "" and isinstance(out, RequestPlan)
    assert [s.name for s in out.steps] == ["page 1", "page 2"]
    assert out.steps[0].bind == {"next_cursor": "next_cursor"}
    assert out.steps[1].template.query["next_cursor"] == "{next_cursor}"
    assert out.problems() == [], out.problems()
    assert "never a loop" in out.notes


def test_the_plan_refuses_a_step_that_reads_a_binding_nobody_produces():
    """The plan validator, not a runtime surprise."""
    tmpl = _tmpl(query={"q": "{q}"})
    bad = RequestPlan(steps=[
        __import__("anyuse.execution.api_step", fromlist=["PlanStep"]).PlanStep(
            name="page 2", template=RequestTemplate.from_dict(
                {**tmpl.to_dict(), "query": {"q": "{q}", "cur": "{never_bound}"},
                 "params": ["q", "never_bound"]}))],
        params=["q"])
    assert bad.problems(), "a plan needing an unbound parameter must be refused"


# ---------------------------------------------------------------------------
# execution and the same fresh-input proof
# ---------------------------------------------------------------------------

class PagedTransport:
    """Answers page 1 and page 2 differently, so a wrong page is visible."""

    def __init__(self, mode="page", status=200):
        self.mode, self.status = mode, status
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        if self.mode == "page":
            page = 2 if ("page=2" in url or "offset=3" in url) else 1
            names = PAGE2 if page == 2 else PAGE1
            payload = _numbered(names, page=page)
        else:
            payload = (_cursored(PAGE2, cursor=None) if "next_cursor=c2" in url
                       else _cursored(PAGE1))
        return ApiResponse(self.status, {"Content-Type": "application/json"},
                           json.dumps(payload))


def test_executing_page_2_returns_page_2():
    out, _pag, _ = compile_next_page(_tmpl(), _numbered(PAGE1),
                                     "https://ex.test/api/search?q=pkg&page=1")
    ex = ApiStepExecutor(transport=PagedTransport())
    first = ex.execute(out, {"q": "pkg", "page": 1})
    second = ex.execute(out, {"q": "pkg", "page": 2})
    assert first["extracted"]["items"] == PAGE1
    assert second["extracted"]["items"] == PAGE2, second["extracted"]


def test_executing_a_cursor_plan_returns_page_2():
    out, _pag, _ = compile_next_page(_tmpl(query={"q": "{q}"}), _cursored(PAGE1))
    tr = PagedTransport(mode="cursor")
    res = ApiStepExecutor(transport=tr).execute_plan(out, {"q": "pkg"})
    assert res["success"] is True, res.get("error")
    assert res["extracted"]["items"] == PAGE2, res["extracted"]
    assert len(tr.sent) == 2 and "next_cursor=c2" in tr.sent[1]


def test_page_2_is_proved_on_FRESH_input_like_everything_else():
    out, _pag, _ = compile_next_page(_tmpl(), _numbered(PAGE1),
                                     "https://ex.test/api/search?q=pkg&page=1")
    ex = ApiStepExecutor(transport=PagedTransport())
    cases = [{"params": {"q": "fresh one", "page": 2}, "value": "fresh one", "ui_items": PAGE2},
             {"params": {"q": "fresh two", "page": 2}, "value": "fresh two", "ui_items": PAGE2}]
    res = verify_template(out, cases, ex)
    assert res.passed, res.reason


def test_the_proof_catches_a_pagination_that_silently_returns_page_1():
    """The failure that matters: page 2 that is really page 1 again."""
    out, _pag, _ = compile_next_page(_tmpl(), _numbered(PAGE1),
                                     "https://ex.test/api/search?q=pkg&page=1")

    class AlwaysPage1(PagedTransport):
        def send(self, method, url, headers, body, timeout_s):
            self.sent.append(url)
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps(_numbered(PAGE1)))

    ex = ApiStepExecutor(transport=AlwaysPage1())
    cases = [{"params": {"q": "a", "page": 2}, "value": "a", "ui_items": PAGE2},
             {"params": {"q": "b", "page": 2}, "value": "b", "ui_items": PAGE2}]
    res = verify_template(out, cases, ex)
    assert res.passed is False
    assert "coverage" in res.reason, res.reason


def test_a_cursor_plan_fails_whole_when_page_2_fails():
    out, _pag, _ = compile_next_page(_tmpl(query={"q": "{q}"}), _cursored(PAGE1))

    class SecondCallFails:
        def __init__(self):
            self.n = 0

        def send(self, method, url, headers, body, timeout_s):
            self.n += 1
            if self.n == 1:
                return ApiResponse(200, {"Content-Type": "application/json"},
                                   json.dumps(_cursored(PAGE1)))
            return ApiResponse(503, {"Content-Type": "text/plain"}, "busy")

    res = ApiStepExecutor(transport=SecondCallFails()).execute_plan(out, {"q": "pkg"})
    assert res["success"] is False
    assert res["failed_step"] == 2, res
    assert res["plan_trace"][0]["success"] is True, "page 1 did succeed; the plan still fails"


def test_an_exhausted_cursor_is_an_ordinary_failure_not_a_silent_empty():
    """Asking for page 2 of a one-page listing: the bind has nothing to bind."""
    out, _pag, _ = compile_next_page(_tmpl(query={"q": "{q}"}), _cursored(PAGE1))

    class NoCursor:
        def send(self, method, url, headers, body, timeout_s):
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps(_cursored(PAGE1, cursor=None)))

    res = ApiStepExecutor(transport=NoCursor()).execute_plan(out, {"q": "pkg"})
    assert res["success"] is False, res


def test_the_pagination_round_trips_through_persistence():
    out, pag, _ = compile_next_page(_tmpl(), _numbered(PAGE1),
                                    "https://ex.test/api/search?q=pkg&page=1")
    again = RequestTemplate.from_dict(out.to_dict())
    assert again.query == out.query and again.params == out.params
    assert pag.to_dict()["kind"] == "param"

    plan, pag2, _ = compile_next_page(_tmpl(query={"q": "{q}"}), _cursored(PAGE1))
    plan_again = RequestPlan.from_dict(plan.to_dict())
    assert plan_again.steps[0].bind == {"next_cursor": "next_cursor"}
    assert plan_again.problems() == []


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
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
