"""A search the site redirects to a landing page (a brand or collection page)
whose data call sends a value the landing page states. Compiled to a plan: the
search URL (its redirect followed), the landing page's value read by its
locator, the data call. A site that sends every query to its home page is
refused as search_to_home. Entirely offline: the transport is injected.
"""
import json
import urllib.parse

from anyuse.execution import page_literals as PL
from anyuse.execution.api_compile import (LANDING_UNCORROBORATED, SEARCH_TO_HOME,
                                          compile_recording)
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestPlan
from anyuse.execution.api_verify import compile_and_verify, compile_and_verify_with_fit

SHOP = "https://shop.test"
API = "https://api.shop.test"
BRANDS = {
    "nike": ("col_8812", ["Nike Air Zoom", "Nike Pegasus 41", "Nike Vomero 18"]),
    "adidas": ("col_9001", ["Adidas Ultraboost", "Adidas Samba OG", "Adidas Gazelle"]),
    "puma": ("col_7777", ["Puma Suede Classic", "Puma Velocity Nitro", "Puma Palermo"]),
    "asics": ("col_5555", ["Asics Gel Kayano", "Asics Novablast 5", "Asics Superblast"]),
}
HOME_ITEMS = ["Gift card", "Summer sale shoes", "New arrivals jackets"]


def landing_html(brand, key=""):
    col, _items = BRANDS[brand]
    cfg = {"collectionId": col, "handle": f"{brand}-shoes"}
    if key:
        cfg["filterKey"] = key
    return (f"<html><head><script>var page={json.dumps(cfg)};</script></head>"
            f"<body><h1>{brand.title()} shoes</h1><div id=grid></div></body></html>")


def rendered_html(brand):
    _col, items = BRANDS[brand]
    rows = "".join(f'<li class="card"><a href="/p/{i}">{name}</a></li>'
                   for i, name in enumerate(items))
    return f"<html><body><h1>{brand}</h1><ul class=results>{rows}</ul></body></html>"


class Shop:
    """``/search?q=B`` lands on B's collection page (the fake follows the redirect)."""

    def __init__(self, home=False, rendered=False, key=""):
        self.home, self.rendered, self.key = home, rendered, key
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        html = lambda t: ApiResponse(200, {"Content-Type": "text/html"}, t)
        if sp.netloc == "shop.test" and sp.path == "/search":
            brand = q.get("q", "")
            if self.home or brand not in BRANDS:
                return html("<html><body><ul>" + "".join(f"<li>{x}</li>" for x in HOME_ITEMS)
                            + "</ul></body></html>")
            return html(rendered_html(brand) if self.rendered else landing_html(brand, self.key))
        if sp.netloc == "api.shop.test" and sp.path == "/filter":
            if self.key and q.get("key") != self.key:
                return ApiResponse(403, {"Content-Type": "application/json"}, "{}")
            col = q.get("collection")
            items = next((items for c, items in BRANDS.values() if c == col), [])
            return ApiResponse(200, {"Content-Type": "application/json"},
                               json.dumps({"products": [{"title": t} for t in items]}))
        return ApiResponse(404, {}, "")


def recording(brand, home=False, rendered=False, key="", literal=None):
    col, items = BRANDS[brand]
    if literal:
        col = literal
    search = f"{SHOP}/search?q={brand}"
    if home:
        net = [{"url": f"{SHOP}/", "redirected_from": [search], "method": "GET", "status": 200,
                "mime": "text/html", "resource_type": "Document",
                "body": "<html><body>" + "".join(f"<p>{x}</p>" for x in HOME_ITEMS) + "</body></html>"},
               {"url": f"{API}/featured", "method": "GET", "status": 200,
                "mime": "application/json", "resource_type": "XHR",
                "body": json.dumps({"products": [{"title": t} for t in HOME_ITEMS]})}]
        return {"param": {"name": "q", "value": brand}, "final_url": f"{SHOP}/",
                "ui": {"items": list(HOME_ITEMS), "text": ""}, "network": net}
    landing = {"url": f"{SHOP}/collections/{brand}-shoes", "redirected_from": [search],
               "method": "GET", "status": 200, "mime": "text/html", "resource_type": "Document",
               "body": rendered_html(brand) if rendered else
               landing_html(brand, key).replace(BRANDS[brand][0], col)}
    net = [landing]
    if not rendered:
        net.append({"url": f"{API}/filter?collection={col}" + (f"&key={key}" if key else ""),
                    "method": "GET", "status": 200, "mime": "application/json",
                    "resource_type": "XHR",
                    "body": json.dumps({"products": [{"title": t} for t in items]})})
    if key:
        net = PL.mark_network(net, "shop.test")
    return {"param": {"name": "q", "value": brand}, "final_url": landing["url"],
            "ui": {"items": list(items), "text": ""}, "network": net}


def prove(shop, **kw):
    return compile_and_verify(recording("nike", **kw), [recording("puma", **kw), recording("asics", **kw)],
                              executor=ApiStepExecutor(transport=shop),
                              fitting_recordings=[recording("adidas", **kw)])


def test_a_redirect_to_a_collection_page_compiles_and_proves():
    shop = Shop()
    compiled, proof = prove(shop)
    assert compiled.status == "landing", compiled.reason
    assert proof.passed, proof.reason
    plan = compiled.template
    assert isinstance(plan, RequestPlan) and [s.name for s in plan.steps] == ["search", "data"]
    search = plan.steps[0].template
    assert search.extract_kind == "page_text" and search.url == f"{SHOP}/search"
    assert search.query == {"q": "{q}"}
    assert plan.steps[0].page_bind["landing_1"]["names"] == [["collectionId", '": "']]
    assert plan.steps[0].page_bind["landing_1"]["else"] is None
    assert plan.steps[1].template.query == {"collection": "{landing_1}"}
    out = ApiStepExecutor(transport=shop).execute(plan, {"q": "adidas"})
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == BRANDS["adidas"][1]
    assert out["plan_trace"][0]["bound"] == {"landing_1": "col_9001"}


def test_a_landing_url_that_names_the_value_still_replays_the_search_url():
    def named(brand):
        rec = recording(brand)
        rec["network"][0]["url"] = f"{SHOP}/c/{brand}"
        return rec
    compiled, proof = compile_and_verify(named("nike"), [named("puma"), named("asics")],
                                         executor=ApiStepExecutor(transport=Shop()),
                                         fitting_recordings=[named("adidas")])
    assert compiled.status == "landing", compiled.reason
    assert proof.passed, proof.reason
    assert compiled.template.steps[0].template.url == f"{SHOP}/search"


def test_the_landing_value_needs_a_second_input():
    res = compile_recording(recording("nike"))
    assert res.template is None and res.reason == LANDING_UNCORROBORATED
    assert res.chain_needs_fitting
    # the proof's input list gives one to fit it on, which then leaves the proof
    compiled, proof, fit = compile_and_verify_with_fit(
        recording("nike"), [recording("puma"), recording("asics"), recording("adidas")],
        [recording("adidas")], executor=ApiStepExecutor(transport=Shop()), generate=False)
    assert fit is not None and compiled.status == "landing", compiled.reason
    assert proof.passed, proof.reason


def test_a_landing_value_equal_for_every_input_is_not_corroborated():
    res = compile_recording(recording("nike", literal="col_same"),
                            chain_recordings=[recording("adidas", literal="col_same")])
    assert res.template is None and res.reason == LANDING_UNCORROBORATED, res.reason


def test_two_inputs_that_share_a_landing_page_are_not_search_to_home():
    # "nike air" lands on nike's collection page too; another brand lands on its own
    nike_air = recording("nike")
    nike_air["param"]["value"] = "nike air"
    nike_air["network"][0]["redirected_from"] = [f"{SHOP}/search?q=nike+air"]
    res = compile_recording(recording("nike"), chain_recordings=[nike_air, recording("adidas")])
    assert res.status == "landing", res.reason
    # every other input on the same page: the landing does not depend on the search
    same = recording("adidas")
    same["network"][0]["url"] = f"{SHOP}/collections/nike-shoes"
    res = compile_recording(recording("nike"), chain_recordings=[nike_air, same])
    assert res.template is None and res.reason == SEARCH_TO_HOME, res.reason


def test_a_site_that_sends_every_query_home_is_refused():
    res = compile_recording(recording("nike", home=True))
    assert res.template is None and res.reason.startswith("search_to_home"), res.reason
    assert res.reason == SEARCH_TO_HOME
    compiled, proof = compile_and_verify(recording("nike", home=True),
                                         [recording("puma", home=True), recording("asics", home=True)],
                                         executor=ApiStepExecutor(transport=Shop(home=True)),
                                         fitting_recordings=[recording("adidas", home=True)])
    assert compiled.template is None and compiled.reason == SEARCH_TO_HOME
    assert not proof.passed


def test_a_page_that_lands_home_and_then_searches_is_not_search_to_home():
    # a script-routed search: /search?q=B redirects to / (the route sits in the
    # fragment the browser never sends), and the page then asks its API for B
    def spa(brand):
        _col, items = BRANDS[brand]
        net = [{"url": f"{SHOP}/", "redirected_from": [f"{SHOP}/search?q={brand}"],
                "method": "GET", "status": 200, "mime": "text/html", "resource_type": "Document",
                "body": "<html><body><div id=app></div></body></html>"},
               {"url": f"{SHOP}/api/search?q={brand}", "method": "GET", "status": 200,
                "mime": "application/json", "resource_type": "XHR",
                "body": json.dumps({"results": [{"title": t} for t in items]})}]
        return {"param": {"name": "q", "value": brand}, "final_url": f"{SHOP}/",
                "ui": {"items": list(items), "text": ""}, "network": net}
    res = compile_recording(spa("nike"))
    assert res.template is not None, res.reason
    assert res.template.url == f"{SHOP}/api/search" and res.template.query == {"q": "{q}"}


def test_an_http_to_https_redirect_compiles_on_the_page_it_landed_on():
    rec = recording("nike", rendered=True)
    rec["network"][0]["url"] = f"{SHOP}/search?q=nike"
    rec["network"][0]["redirected_from"] = ["http://shop.test/search?q=nike"]
    res = compile_recording(rec)
    assert res.status == "html", res.reason
    assert res.template.url == f"{SHOP}/search" and res.template.query == {"q": "{q}"}


def test_a_server_rendered_landing_compiles_on_the_search_url():
    compiled, proof = prove(Shop(rendered=True), rendered=True)
    assert compiled.status == "html", compiled.reason
    assert proof.passed, proof.reason
    assert compiled.template.url == f"{SHOP}/search" and compiled.template.query == {"q": "{q}"}


def test_a_server_rendered_landing_that_names_the_value_compiles_on_the_search_url():
    def named(brand):
        rec = recording(brand, rendered=True)
        rec["network"][0]["url"] = f"{SHOP}/c/{brand}"
        return rec
    compiled, proof = compile_and_verify(named("nike"), [named("puma"), named("asics")],
                                         executor=ApiStepExecutor(transport=Shop(rendered=True)),
                                         fitting_recordings=[named("adidas")])
    assert compiled.status == "html", compiled.reason
    assert proof.passed, proof.reason
    assert compiled.template.url == f"{SHOP}/search" and compiled.template.query == {"q": "{q}"}


def test_a_landing_page_that_states_the_data_calls_key():
    shop = Shop(key="flt-key-424242")
    compiled, proof = prove(shop, key="flt-key-424242")
    assert compiled.status == "landing", compiled.reason
    assert proof.passed, proof.reason
    plan = compiled.template
    assert set(plan.steps[0].page_bind) == {"landing_1", "page_key_1"}
    assert "flt-key-424242" not in json.dumps(plan.to_dict())
    out = ApiStepExecutor(transport=shop).execute(plan, {"q": "puma"})
    assert out["success"], out.get("error")
    assert "flt-key-424242" not in json.dumps(out)
    assert out["plan_trace"][0]["bound"]["page_key_1"] == PL.SHOWN


class HomeKeyShop(Shop):
    """The data call's key is stated on the home page, not on the landing page."""

    def send(self, method, url, headers, body, timeout_s):
        sp = urllib.parse.urlsplit(url)
        if sp.netloc == "shop.test" and sp.path == "/":
            self.sent.append(url)
            return ApiResponse(200, {"Content-Type": "text/html"},
                               f'<html><script>var cfg={{"filterKey":"{self.key}"}};</script></html>')
        if sp.netloc == "shop.test" and sp.path == "/search":
            self.sent.append(url)
            return ApiResponse(200, {"Content-Type": "text/html"}, landing_html(dict(
                urllib.parse.parse_qsl(sp.query)).get("q", "")))
        return super().send(method, url, headers, body, timeout_s)


def home_key_recording(brand, key):
    col, items = BRANDS[brand]
    net = [{"url": f"{SHOP}/", "method": "GET", "status": 200, "mime": "text/html",
            "resource_type": "Document",
            "body": f'<html><script>var cfg={{"filterKey":"{key}"}};</script></html>'},
           {"url": f"{SHOP}/collections/{brand}-shoes", "redirected_from": [f"{SHOP}/search?q={brand}"],
            "method": "GET", "status": 200, "mime": "text/html", "resource_type": "Document",
            "body": landing_html(brand)},
           {"url": f"{API}/filter?collection={col}&key={key}", "method": "GET", "status": 200,
            "mime": "application/json", "resource_type": "XHR",
            "body": json.dumps({"products": [{"title": t} for t in items]})}]
    return {"param": {"name": "q", "value": brand}, "final_url": net[1]["url"],
            "ui": {"items": list(items), "text": ""}, "network": PL.mark_network(net, "shop.test")}


def test_a_data_call_key_stated_on_another_page_adds_a_page_step():
    key = "flt-key-424242"
    shop = HomeKeyShop(key=key)
    compiled, proof = compile_and_verify(
        home_key_recording("nike", key), [home_key_recording("puma", key), home_key_recording("asics", key)],
        executor=ApiStepExecutor(transport=shop), fitting_recordings=[home_key_recording("adidas", key)])
    assert compiled.status == "landing", compiled.reason
    assert proof.passed, proof.reason
    plan = compiled.template
    assert [s.name for s in plan.steps] == ["page", "search", "data"]
    assert set(plan.steps[0].page_bind) == {"page_key_1"}
    assert set(plan.steps[1].page_bind) == {"landing_1"}
    assert key not in json.dumps(plan.to_dict())
    out = ApiStepExecutor(transport=shop).execute(plan, {"q": "puma"})
    assert out["success"], out.get("error")
    assert out["extracted"]["items"] == BRANDS["puma"][1]
    assert key not in json.dumps(out)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
