"""A search provider whose public key the site's own page states: compiled to a
two-step HTTP plan that reads the key (and the app id and index) from the page at
run time and never stores it. Entirely offline: the transport is injected and
the recordings go through the learn recorder's sanitiser (page_literals).
"""
import json
import os
import tempfile
import urllib.parse

from anyuse.execution import page_literals as PL
from anyuse.execution.api_compile import compile_recording, compiled_record
from anyuse.execution.api_step import (ApiResponse, ApiStepExecutor, RequestPlan,
                                       bound_request_problems, load_command)
from anyuse.execution.api_verify import compile_and_verify
from anyuse.execution.workflow_schema import template_digest

SHOP = "https://shop.test"
PROVIDER = "https://abcdef1234-dsn.search.test"
CATALOGUE = {
    "red": ["red fox plush", "red panda mug", "red kite poster"],
    "blue": ["blue whale lamp", "blue jay print", "blue tang towel"],
    "green": ["green frog clock", "green tea tin", "green moss rug"],
    "amber": ["amber bead necklace", "amber ale glass", "amber lamp shade"],
    "slate": ["slate coaster set", "slate cheese board", "slate wall clock"],
}


def page_html(key, name="searchKey", app="ABCDEF1234", index="prod_items"):
    cfg = json.dumps({"appId": app, name: key, "index": index})
    return (f"<html><head><script>window.searchConfig={cfg};</script></head>"
            f"<body><form><input name=q type=search></form></body></html>")


class PageKeyShop:
    """The shop's page states the current key; the provider answers only that key."""

    def __init__(self, key="k3y-public-123", post=False):
        self.key, self.name, self.post = key, "searchKey", post
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append((method, url, dict(headers), body))
        sp = urllib.parse.urlsplit(url)
        if f"{sp.scheme}://{sp.netloc}" == SHOP:
            return ApiResponse(200, {"Content-Type": "text/html"}, page_html(self.key, self.name))
        q = dict(urllib.parse.parse_qsl(sp.query))
        if self.post:
            data = json.loads(body or "{}")
            term = data.get("params", {}).get("query", "")
        else:
            term = q.get("query", "")
        key = q.get("x-algolia-api-key") or headers.get("x-algolia-api-key")
        if key != self.key or q.get("x-algolia-application-id") != "ABCDEF1234" \
                or "/prod_items" not in sp.path:
            return ApiResponse(403, {"Content-Type": "application/json"}, '{"message":"bad key"}')
        hits = [{"name": n, "objectID": f"o{i}"} for i, n in enumerate(CATALOGUE.get(term, []))]
        return ApiResponse(200, {"Content-Type": "application/json"},
                           json.dumps({"hits": hits, "query": term}))


def recording(q, key="k3y-public-123", post=False):
    """A learn agent's raw traffic for searching ``q``, sanitised as as_recording does."""
    hits = [{"name": n, "objectID": f"o{i}"} for i, n in enumerate(CATALOGUE[q])]
    if post:
        provider = {"url": f"{PROVIDER}/1/indexes/prod_items/query?x-algolia-application-id=ABCDEF1234"
                           f"&x-algolia-api-key={key}",
                    "method": "POST", "post_data": json.dumps({"params": {"query": q}}),
                    "headers": {"content-type": "application/json"}}
    else:
        provider = {"url": f"{PROVIDER}/1/indexes/prod_items?query={q}"
                           f"&x-algolia-application-id=ABCDEF1234&x-algolia-api-key={key}",
                    "method": "GET", "headers": {}}
    raw = [
        {"url": f"{SHOP}/", "method": "GET", "status": 200, "mime": "text/html",
         "resource_type": "Document", "body": page_html(key)},
        {**provider, "status": 200, "mime": "application/json", "resource_type": "XHR",
         "body": json.dumps({"hits": hits, "query": q})},
    ]
    return {"param": {"name": "q", "value": q},
            "ui": {"items": list(CATALOGUE[q]), "text": "\n".join(CATALOGUE[q])},
            "final_url": f"{SHOP}/", "site": "shop.test",
            "network": PL.mark_network(raw, "shop.test")}


def prove(shop, post=False):
    return compile_and_verify(recording("red", post=post),
                              [recording("green", post=post), recording("amber", post=post)],
                              executor=ApiStepExecutor(transport=shop),
                              fitting_recordings=[recording("blue", post=post)])


class RotatingShop(PageKeyShop):
    """Every page read states a new key and a new app id; the provider answers the last pair."""

    def __init__(self):
        super().__init__()
        self.n = 0
        self.app = "APP0000000"

    def rotate(self):
        self.n += 1
        self.key, self.app = f"k3y-rot-{self.n:04d}", f"APP{self.n:07d}"

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        if f"{sp.scheme}://{sp.netloc}" == SHOP:
            self.rotate()
            return ApiResponse(200, {"Content-Type": "text/html"}, page_html(self.key, app=self.app))
        q = dict(urllib.parse.parse_qsl(sp.query))
        if q.get("x-algolia-api-key") != self.key or q.get("x-algolia-application-id") != self.app:
            return ApiResponse(403, {"Content-Type": "application/json"}, "{}")
        hits = [{"name": n} for n in CATALOGUE.get(q.get("query", ""), [])]
        return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps({"hits": hits}))


def rotating_recording(shop, q):
    shop.rotate()
    hits = [{"name": n} for n in CATALOGUE[q]]
    raw = [{"url": f"{SHOP}/", "method": "GET", "status": 200, "mime": "text/html",
            "resource_type": "Document", "body": page_html(shop.key, app=shop.app)},
           {"url": f"{PROVIDER}/1/indexes/prod_items?query={q}&x-algolia-application-id={shop.app}"
                   f"&x-algolia-api-key={shop.key}", "method": "GET", "status": 200,
            "mime": "application/json", "resource_type": "XHR", "headers": {},
            "body": json.dumps({"hits": hits})}]
    return {"param": {"name": "q", "value": q}, "ui": {"items": list(CATALOGUE[q]), "text": ""},
            "final_url": f"{SHOP}/", "network": PL.mark_network(raw, "shop.test")}


def test_an_app_id_that_changes_between_reads_is_read_from_the_page_not_dropped():
    shop = RotatingShop()
    recs = [rotating_recording(shop, q) for q in ("red", "blue", "green", "amber")]
    compiled, proof = compile_and_verify(recs[0], recs[2:], executor=ApiStepExecutor(transport=shop),
                                         fitting_recordings=[recs[1]])
    assert compiled.status == "page_keyed", compiled.reason
    assert proof.passed, proof.reason
    search = compiled.template.steps[1].template
    assert search.query["x-algolia-application-id"].startswith("{page_lit_"), search.query
    out = ApiStepExecutor(transport=shop).execute(compiled.template, {"q": "slate"})
    assert out["success"] and out["extracted"]["items"] == CATALOGUE["slate"], out.get("error")


def test_the_recording_keeps_a_locator_and_never_the_key():
    rec = recording("red")
    assert "k3y-public-123" not in json.dumps(rec)
    keys = rec["network"][1]["page_keys"]
    assert [k["at"] for k in keys] == ["query:x-algolia-api-key"]


def test_a_page_keyed_provider_compiles_proves_and_runs():
    shop = PageKeyShop()
    compiled, proof = prove(shop)
    assert compiled.status == "page_keyed", compiled.reason
    assert proof.passed, proof.reason
    plan = compiled.template
    assert isinstance(plan, RequestPlan) and [s.name for s in plan.steps] == ["page", "search"]
    assert plan.steps[0].template.extract_kind == "page_text"
    assert plan.steps[0].template.url == f"{SHOP}/"
    assert set(plan.steps[0].page_bind) == {"page_key_1", "page_lit_1", "page_lit_2"}
    assert "page values read from the site's page" in proof.reason
    record = compiled_record(plan, proof.to_dict())
    blob = json.dumps(record)
    assert "k3y-public-123" not in blob and "<page value>" not in blob
    # the provider's app id and index fall back to the recorded values; the key never does
    binds = plan.steps[0].page_bind
    assert binds["page_key_1"]["else"] is None and binds["page_key_1"]["secret"]
    assert {binds["page_lit_1"]["else"], binds["page_lit_2"]["else"]} == {"ABCDEF1234", "prod_items"}

    out = ApiStepExecutor(transport=shop).execute(plan, {"q": "slate"})
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == CATALOGUE["slate"]
    assert "k3y-public-123" not in json.dumps(out)
    assert out["plan_trace"][0]["bound"]["page_key_1"] == PL.SHOWN
    assert PL.SHOWN in out["url"] and PL.SHOWN in out["plan_trace"][1]["url"]
    # the provider got the key, the page got no cookie
    sent = [u for m, u, h, b in shop.sent if "search.test" in u][-1]
    assert "x-algolia-api-key=k3y-public-123" in sent
    assert all("cookie" not in {k.lower() for k in h} for _m, _u, h, _b in shop.sent)


def test_a_rotated_key_needs_no_recompile():
    shop = PageKeyShop()
    compiled, proof = prove(shop)
    assert proof.passed, proof.reason
    stored = compiled_record(compiled.template, proof.to_dict())
    before = template_digest(stored["template"])
    shop.key = "k3y-rotated-777"
    plan = load_command(stored["template"])
    out = ApiStepExecutor(transport=shop).execute(plan, {"q": "blue"})
    assert out["success"], out.get("error")
    assert "k3y-rotated-777" not in json.dumps(out)
    assert template_digest(plan.to_dict()) == before


def test_a_key_the_page_no_longer_names_stops_the_plan():
    shop = PageKeyShop()
    compiled, _proof = prove(shop)
    shop.name = "publicKey"
    out = ApiStepExecutor(transport=shop).execute(compiled.template, {"q": "blue"})
    assert not out["success"] and "no longer states searchKey" in out["error"]
    assert out["failed_step"] == 1


def test_with_page_keys_off_the_refusal_is_the_old_one():
    os.environ["ANYUSE_PAGE_KEYS"] = "off"
    try:
        res = compile_recording(recording("red"))
    finally:
        del os.environ["ANYUSE_PAGE_KEYS"]
    assert res.template is None and "contains 'api-key'" in res.reason, res.reason


def test_with_page_keys_off_a_compiled_tool_sends_nothing():
    shop = PageKeyShop()
    compiled, _proof = prove(shop)
    shop.sent.clear()
    os.environ["ANYUSE_PAGE_KEYS"] = "off"
    try:
        out = ApiStepExecutor(transport=shop).execute(compiled.template, {"q": "red"})
    finally:
        del os.environ["ANYUSE_PAGE_KEYS"]
    assert not out["success"] and "ANYUSE_PAGE_KEYS is off" in out["error"], out
    assert shop.sent == []
    out = ApiStepExecutor(transport=shop).execute(compiled.template, {"q": "red"})
    assert out["success"], out.get("error")


def test_bound_request_refuses_a_credential_header_that_is_no_page_value():
    shop = PageKeyShop()
    compiled, _proof = prove(shop)
    step = compiled.template.steps[1].template
    vals = {"page_key_1": "k3y-public-123", "page_lit_1": "ABCDEF1234", "page_lit_2": "prod_items"}
    from anyuse.execution.workflow_schema import DataflowValues
    flow = DataflowValues({"q": "red"})
    for name, value in vals.items():
        row = next(r for r in compiled.template.parameter_schema if r["name"] == name)
        flow.add_derived(name, value, row["source"])
    flow.destination_step = "search"
    url, headers, body = step.render(flow)
    assert bound_request_problems(step, url, headers, body, page_values=vals) == []
    # another credential header next to the page key is still refused
    problems = bound_request_problems(step, url, {**headers, "x-api-key": "zzzzzzzzzzzz"}, body,
                                      page_values=vals)
    assert any("x-api-key" in p for p in problems), problems
    # the exemption is exact: the page key's own field with another value is refused
    from anyuse.execution.api_slots import deep_refusal
    exempt = {"x-algolia-api-key": {"k3y-public-123"}}
    assert deep_refusal(url, None, exempt=exempt) == ""
    assert "api-key" in deep_refusal(url.replace("k3y-public-123", "k3y-other-000"), None,
                                     exempt=exempt)
    assert "api-key" in deep_refusal(url, None)


def test_a_page_value_is_never_sent_in_an_authorization_header():
    from anyuse.execution.api_step import RequestTemplate
    compiled, _proof = prove(PageKeyShop())
    data = compiled.template.steps[1].template.to_dict()
    query = {k: v for k, v in data["query"].items() if v != "{page_key_1}"}
    as_header = RequestTemplate.from_dict({**data, "query": query,
                                           "headers": {**data["headers"], "x-algolia-api-key": "{page_key_1}"}})
    assert as_header.problems() == [], as_header.problems()
    as_auth = RequestTemplate.from_dict({**data, "query": query,
                                         "headers": {**data["headers"], "Authorization": "{page_key_1}"}})
    assert any("authorization" in p.lower() for p in as_auth.problems()), as_auth.problems()


def test_a_provider_read_post_compiles_and_runs_once_its_host_is_acknowledged():
    from anyuse.execution.api_permission import acknowledge_host
    shop = PageKeyShop(post=True)
    with tempfile.TemporaryDirectory() as tmp:
        hosts = os.path.join(tmp, "hosts.json")
        executor = ApiStepExecutor(transport=shop, readonly_post_hosts_path=hosts)
        compiled, proof = compile_and_verify(
            recording("red", post=True), [recording("green", post=True), recording("amber", post=True)],
            executor=executor, fitting_recordings=[recording("blue", post=True)])
        assert compiled.status == "page_keyed", compiled.reason
        search = compiled.template.steps[1].template
        assert search.method == "POST" and search.readonly_post and search.provider_read == "shop.test"
        assert not proof.passed and "acknowledge-host abcdef1234-dsn.search.test" in proof.reason
        acknowledge_host("abcdef1234-dsn.search.test", note="test", path=hosts)
        out = executor.execute(compiled.template, {"q": "slate"})
        assert out["success"], out.get("error")
        assert out["extracted"]["items"] == CATALOGUE["slate"]
        assert "k3y-public-123" not in json.dumps(out)


def test_an_acknowledged_provider_read_is_checked_again_on_every_send():
    import copy
    from anyuse.execution.api_permission import acknowledge_host
    from anyuse.execution.api_step import RequestTemplate
    shop = PageKeyShop(post=True)
    with tempfile.TemporaryDirectory() as tmp:
        hosts = os.path.join(tmp, "hosts.json")
        executor = ApiStepExecutor(transport=shop, readonly_post_hosts_path=hosts)
        compiled, _proof = compile_and_verify(
            recording("red", post=True), [recording("green", post=True), recording("amber", post=True)],
            executor=executor, fitting_recordings=[recording("blue", post=True)])
        acknowledge_host("abcdef1234-dsn.search.test", note="test", path=hosts)
        stored = compiled.template.to_dict()
        # a path that names telemetry, and a body field that names a state change
        telemetry = copy.deepcopy(stored)
        telemetry["steps"][1]["url"] = telemetry["steps"][1]["url"].replace("/query", "/events")
        state = copy.deepcopy(stored)
        state["steps"][1]["body"] = state["steps"][1]["body"].replace('"params"', '"delete":"1","params"')
        for changed, why in ((telemetry, "telemetry"), (state, "state change")):
            sent = len(shop.sent)
            out = executor.execute(load_command(changed), {"q": "slate"})
            assert not out["success"] and why in out["error"], out.get("error")
            assert not any("search.test" in u for _m, u, _h, _b in shop.sent[sent:])
        # a provider read that sends no value the page stated needs the setting
        search = compiled.template.steps[1].template
        plain = RequestTemplate.from_dict({**search.to_dict(), "parameter_schema": [],
                                           "params": ["q"],
                                           "url": search.url.replace("{page_lit_2}", "prod_items"),
                                           "query": {"x-algolia-application-id": "ABCDEF1234"}})
        out = executor.execute(plain, {"q": "slate"})
        assert not out["success"] and "ANYUSE_API_READONLY_POST" in out["error"], out.get("error")


def test_a_page_keyed_post_to_the_site_itself_needs_the_lever():
    from anyuse.execution.api_compile import Levers, compile_page_keyed_post
    rec = recording("red", post=True)
    rec["network"][1]["url"] = rec["network"][1]["url"].replace(PROVIDER, SHOP)
    items = ["red fox plush", "red panda mug", "red kite poster"]
    plan, why = compile_page_keyed_post(rec, items, "q", "red")
    assert plan is None and "the site itself" in why and "'post' compile lever" in why, why
    plan, why = compile_page_keyed_post(rec, items, "q", "red", levers=Levers(readonly_post=True))
    assert plan is not None and plan.steps[1].template.provider_read == "", why


def test_a_post_that_is_not_the_providers_read_needs_the_lever():
    from anyuse.execution.api_compile import Levers, compile_page_keyed_post
    rec = recording("red", post=True)
    rec["network"][1]["url"] = rec["network"][1]["url"].replace("/query?", "/events?")
    plan, why = compile_page_keyed_post(rec, ["red fox plush", "red panda mug", "red kite poster"],
                                        "q", "red")
    assert plan is None and "'post' compile lever" in why, why
    plan, why = compile_page_keyed_post(rec, ["red fox plush", "red panda mug", "red kite poster"],
                                        "q", "red", levers=Levers(readonly_post=True))
    assert plan is not None, why
    assert plan.steps[1].template.provider_read == ""


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
