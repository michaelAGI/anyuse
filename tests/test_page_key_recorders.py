"""The recorders keep a provider key the site's own page stated as a locator and
never as a value: a browser replay's traffic (cdp_replay.traffic_from_events and
read_bodies) and the learn agent's recording (AgentRun.as_recording). Both keep a
redirect's hops. Entirely offline.
"""
import asyncio
import json

from anyuse.execution import page_literals as PL
from anyuse.execution.cdp_replay import read_bodies, traffic_from_events

PAGE = ('<html><script>window.cfg={"appId":"APP12345","searchKey":"k3y-public-123"}'
        '</script><body>shop</body></html>')


def _req(rid, url, rtype, mime, headers=None, redirect=False):
    out = [{"method": "Network.requestWillBeSent", "params": {
        "requestId": rid, "type": rtype, **({"redirectResponse": {"status": 302}} if redirect else {}),
        "request": {"url": url, "method": "GET", "headers": headers or {}}}}]
    return out


def _done(rid, rtype, mime):
    return [{"method": "Network.responseReceived", "params": {
                "requestId": rid, "type": rtype, "response": {"status": 200, "mimeType": mime}}},
            {"method": "Network.loadingFinished", "params": {"requestId": rid}}]


def _events(provider_url, extra=None):
    ev = _req("1", "https://shop.test/", "Document", "text/html") + _done("1", "Document", "text/html")
    ev += _req("2", provider_url, "XHR", "application/json")
    if extra:
        ev.append({"method": "Network.requestWillBeSentExtraInfo",
                   "params": {"requestId": "2", "headers": extra}})
    ev += _done("2", "XHR", "application/json")
    return ev


BODIES = {"1": PAGE, "2": '{"hits":[{"name":"red fox"}],"echo":"k3y-public-123"}'}


async def _call(method, params, timeout):
    return {"body": BODIES[params["requestId"]]}


def test_replay_traffic_keeps_a_page_key_and_never_the_value():
    kept = traffic_from_events(_events("https://search.prov.test/q?query=red"
                                       "&x-algolia-api-key=k3y-public-123"))
    assert [e["_rid"] for e in kept] == ["1", "2"]
    assert "k3y-public-123" not in kept[1]["url"]
    bodies = asyncio.run(read_bodies(kept, _call, 5.0))
    assert len(bodies) == 2 and "_key_fields" not in bodies[1]
    assert [k["at"] for k in bodies[1]["page_keys"]] == ["query:x-algolia-api-key"]
    assert "k3y-public-123" not in json.dumps(bodies)


def test_replay_traffic_drops_a_key_no_page_stated():
    BODIES["1"] = "<html>no key</html>"
    try:
        kept = traffic_from_events(_events("https://search.prov.test/q?query=red"
                                           "&x-algolia-api-key=k3y-public-123"))
        bodies = asyncio.run(read_bodies(kept, _call, 5.0))
    finally:
        BODIES["1"] = PAGE
    assert [b["url"] for b in bodies] == ["https://shop.test/"]


def test_replay_traffic_with_a_session_cookie_is_never_keyed():
    kept = traffic_from_events(_events("https://search.prov.test/q?query=red"
                                       "&x-algolia-api-key=k3y-public-123",
                                       extra={"cookie": "sessionid=abc"}))
    assert [e["_rid"] for e in kept] == ["1"]


def test_replay_traffic_sent_with_a_header_its_request_did_not_show_is_never_keyed():
    kept = traffic_from_events(_events("https://search.prov.test/q?query=red"
                                       "&x-algolia-api-key=k3y-public-123",
                                       extra={"authorization": "Bearer s3ss10n"}))
    assert [e["_rid"] for e in kept] == ["1"]


def test_replay_traffic_keeps_redirect_hops():
    ev = _req("1", "https://shop.test/search?q=nike", "Document", "text/html")
    ev += _req("1", "https://shop.test/collections/nike", "Document", "text/html", redirect=True)
    ev += _done("1", "Document", "text/html")
    kept = traffic_from_events(ev)
    assert kept[0]["url"] == "https://shop.test/collections/nike"
    assert kept[0]["redirected_from"] == ["https://shop.test/search?q=nike"]


def test_an_attach_recording_reads_the_app_id_from_the_page_that_stated_the_key():
    # attach numbers its requests from 1, and a recording keeps only the requests it may
    # compile: the key's source is found by its URL, not by its place in the list
    from anyuse.execution import attach as A
    from anyuse.execution.api_compile import compile_recording
    items = ["red fox plush", "red panda mug", "red kite poster"]
    rec = A.Recorder(pin=None)

    def load(rid, url, rtype, mime, body):
        rec.on_event({"method": "Network.requestWillBeSent", "params": {
            "requestId": rid, "type": rtype, "frameId": "F",
            "request": {"url": url, "method": "GET", "headers": {}}}})
        rec.on_event({"method": "Network.responseReceived", "params": {
            "requestId": rid, "type": rtype, "response": {"status": 200, "mimeType": mime}}})
        if ("body", rid) in rec.on_event({"method": "Network.loadingFinished",
                                          "params": {"requestId": rid, "encodedDataLength": 10}}):
            rec.on_body(rid, {"body": body})
    rec.on_event({"method": "Page.frameNavigated",
                  "params": {"frame": {"id": "F", "url": "https://shop.test/"}}})
    load("t1", "https://shop.test/api/me?session_token=zzzzzzzz", "XHR", "application/json", "{}")
    load("d1", "https://shop.test/", "Document", "text/html", PAGE)
    load("x1", "https://search.prov.test/1/indexes/items?query=red&x-algolia-application-id=APP12345"
               "&x-algolia-api-key=k3y-public-123", "XHR", "application/json",
         json.dumps({"hits": [{"name": n} for n in items]}))
    session = rec.to_session("s")
    recording = {"param": {"name": "q", "value": "red"}, "final_url": "https://shop.test/",
                 "ui": {"items": items, "text": ""},
                 "network": [dict(r) for r in session["network"] if not r.get("credential_bound")]}
    res = compile_recording(recording)
    assert res.status == "page_keyed", res.reason
    binds = res.template.steps[0].page_bind
    assert any(b["names"][0][0] == "appId" for b in binds.values()), binds


def test_the_learn_recording_holds_no_key():
    from anyuse.execution.agent_browser import AgentRun
    run = AgentRun(value="red", task="t", start_url="https://shop.test/")
    run.final_url = "https://shop.test/"
    run.dom = PAGE
    run.text = "red fox k3y-public-123"
    run.network = [
        {"url": "https://shop.test/", "method": "GET", "status": 200, "mime": "text/html",
         "resource_type": "Document", "body": PAGE, "headers": {}},
        {"url": "https://search.prov.test/q?query=red&x-algolia-api-key=k3y-public-123",
         "method": "GET", "status": 200, "mime": "application/json", "resource_type": "XHR",
         "body": BODIES["2"], "headers": {}},
    ]
    run.network.append({"url": "https://shop.test/api/s?q=red&_token=s3ss10n-zzz", "method": "GET",
                        "status": 200, "mime": "application/json", "resource_type": "XHR",
                        "body": '{"items":[]}', "headers": {}})
    rec = run.as_recording("w", "q", "agent0")
    assert "k3y-public-123" not in json.dumps(rec)
    # a credential no page stated stays as the agent's browser sent it, as before
    assert rec["network"][2]["url"].endswith("_token=s3ss10n-zzz") and rec["network"][2]["body"]
    assert not rec["network"][2].get("credential_bound")
    assert rec["network"][1]["page_keys"][0]["locator"]["names"] == [["searchKey", '":"']]
    assert PL.SHOWN in rec["dom"] and PL.SHOWN in rec["ui"]["text"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
