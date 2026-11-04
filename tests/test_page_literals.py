"""Values a site's own page states by name (execution/page_literals.py).

The scanner, the locator and its read, the keyed source index, the request
fields that carry a credential-like name, and the sanitised learn traffic:
a provider key the site's page stated is kept as a locator, never as a value.
Entirely offline.
"""
import json

from anyuse.execution import page_literals as PL

PAGE = ('<html><head><script>window.cfg={"appId":"ABCDEF1234","searchKey":"k3y-public-123",'
        '"index":"prod_items"}</script></head><body><h1>Shop</h1></body></html>')


def test_locate_names_the_key_by_its_name_and_punctuation():
    loc = PL.locate(PAGE, "k3y-public-123", secret=True)
    assert loc is not None
    assert loc.names == (("searchKey", '":"'),) and loc.right == '"'
    d = loc.to_dict()
    assert "k3y-public-123" not in json.dumps(d) and d["else"] is None and d["secret"]
    assert len(loc.ident()) == 12 and loc.ident() == PL.Locator.from_dict(d).ident()


def test_read_follows_a_rotated_key_and_refuses_two_or_none():
    loc = PL.locate(PAGE, "k3y-public-123", secret=True)
    rotated = PAGE.replace("k3y-public-123", "k3y-public-999")
    assert PL.read(rotated, loc) == ("k3y-public-999", "")
    two = rotated.replace("</script>", '</script><script>x={"searchKey":"other-key-777"}</script>')
    value, why = PL.read(two, loc)
    assert value is None and "2 different values" in why
    value, why = PL.read("<html><body>no config</body></html>", loc)
    assert value is None and "no longer states searchKey" in why


def test_a_secret_shorter_than_six_characters_is_never_read():
    loc = PL.locate(PAGE, "k3y-public-123", secret=True)
    short = PL.locate('{"searchKey":"k3y456"}', "k3y456", secret=True)
    assert loc is not None and short is not None and short.min_len == 3
    value, why = PL.read('{"searchKey":"k3y"}', short)
    assert value is None and "no longer states searchKey" in why


def test_a_non_secret_literal_falls_back_to_its_recorded_value():
    loc = PL.locate(PAGE, "prod_items")
    assert loc is not None and loc.else_value == "prod_items"
    assert PL.read("<html></html>", loc) == ("prod_items", "")
    assert PL.read(PAGE.replace("prod_items", "prod_v2_items"), loc) == ("prod_v2_items", "")


def test_a_positional_argument_has_no_name_and_no_locator():
    assert PL.locate('<script>init("A1B2C3", "k3y-public-123")</script>', "k3y-public-123",
                     secret=True) is None


def test_attribute_and_url_statements_are_found():
    loc = PL.locate('<div data-search-key="k3y-attr-555"></div>', "k3y-attr-555", secret=True)
    assert loc is not None and loc.names == (("data-search-key", '="'),)
    loc = PL.locate('<a href="/s?apiKey=k3y-url-444&x=1">s</a>', "k3y-url-444", secret=True)
    assert loc is not None and loc.names == (("apiKey", "="),) and loc.right == "&"


def test_a_name_stated_with_two_values_is_not_a_locator():
    text = '{"key1":"aaaaaa1","key1":"bbbbbb2"}'
    assert PL.locate(text, "aaaaaa1", secret=True) is None


def test_source_index_finds_only_an_earlier_source():
    index = PL.SourceIndex(key=b"0" * 16)
    index.add(0, "https://shop.test/", PAGE)
    fp = index.fingerprint("k3y-public-123")
    hit = index.find(fp, before=3)
    assert hit is not None and hit["source_url"] == "https://shop.test/"
    assert hit["locator"]["names"] == [["searchKey", '":"']]
    assert index.find(fp, before=0) is None
    assert index.find(index.fingerprint("another-key-1"), before=3) is None


def test_source_index_keeps_the_earliest_source_whatever_order_bodies_arrive_in():
    # attach indexes a body when it arrives: a later request's body can come first
    index = PL.SourceIndex(key=b"0" * 16)
    index.add(7, "https://shop.test/api/config", '{"searchKey":"k3y-public-123"}')
    index.add(1, "https://shop.test/", PAGE)
    hit = index.find(index.fingerprint("k3y-public-123"), before=3)
    assert hit is not None and hit["source_index"] == 1 and hit["source_url"] == "https://shop.test/"


def test_credential_fields_query_header_and_body_never_a_cookie():
    fields, never = PL.credential_fields(
        "https://p.test/q?query=x&x-algolia-api-key=K123456",
        {"X-Algolia-API-Key": "K123456", "accept": "*/*"},
        '{"params":{"apiKey":"K123456","q":"x"}}')
    assert never == ""
    assert fields == {"query:x-algolia-api-key": "K123456", "header:x-algolia-api-key": "K123456",
                      "body:params.apiKey": "K123456"}
    fields, never = PL.credential_fields("https://p.test/q", {"Cookie": "sid=1"})
    assert fields == {} and "Cookie" in never
    fields, never = PL.credential_fields("https://p.test/q?x-api-key=K123456",
                                         {"Authorization": "K123456"})
    assert fields == {} and "Authorization" in never


def test_an_authorization_header_the_page_stated_is_never_a_page_key():
    traffic = _learn_traffic()
    traffic[1]["headers"] = {"Authorization": "k3y-public-123"}
    out = PL.mark_network(traffic, "shop.test")
    assert "page_keys" not in out[1] and out[1]["credential_bound"]
    learn = PL.mark_network(traffic, "shop.test", refuse_unstated=False)
    assert "page_keys" not in learn[1]


def _learn_traffic(key="k3y-public-123", source_host="shop.test"):
    return [
        {"url": f"https://{source_host}/", "method": "GET", "status": 200, "mime": "text/html",
         "resource_type": "Document", "body": PAGE.replace("k3y-public-123", key)},
        {"url": f"https://abcdef1234-dsn.search.test/1/indexes/prod_items?query=red&x-api-key={key}",
         "method": "GET", "status": 200, "mime": "application/json", "resource_type": "XHR",
         "headers": {}, "body": json.dumps({"hits": [{"name": "red fox"}], "echo": key})},
        {"url": "https://shop.test/api/cart?session_token=s3ss10n-zzz", "method": "GET",
         "status": 200, "mime": "application/json", "resource_type": "XHR",
         "body": '{"cart":[]}'},
    ]


def test_mark_network_keeps_a_locator_and_drops_every_copy_of_the_key():
    secrets = set()
    out = PL.mark_network(_learn_traffic(), "shop.test", secrets_out=secrets)
    blob = json.dumps(out)
    assert "k3y-public-123" not in blob and secrets == {"k3y-public-123"}
    provider = out[1]
    assert "x-api-key=%3Credacted%3E" in provider["url"]
    assert provider["credential_bound"] == ""
    keys = PL.page_keys_of(provider)
    assert list(keys) == ["query:x-api-key"]
    assert keys["query:x-api-key"]["source_url"] == "https://shop.test/"
    assert keys["query:x-api-key"]["locator"]["names"] == [["searchKey", '":"']]
    assert PL.SHOWN in out[0]["body"] and PL.SHOWN in provider["body"]
    # a session token no page of the site stated: a fact, its body dropped
    assert out[2]["credential_bound"] and out[2]["body"] is None
    assert "s3ss10n-zzz" not in blob


def test_a_value_only_another_site_stated_is_no_provenance():
    out = PL.mark_network(_learn_traffic(source_host="cdn.other.test"), "shop.test")
    assert out[1]["credential_bound"] and out[1]["body"] is None and "page_keys" not in out[1]
    assert "k3y-public-123" not in out[1]["url"]


def test_scrub_replaces_plain_and_encoded_forms():
    got = PL.scrub({"url": "https://x.test/?k=a%2Fb-value", "e": ["a/b-value"], "n": 3},
                   ["a/b-value"])
    assert got == {"url": "https://x.test/?k=<page value>", "e": ["<page value>"], "n": 3}


def test_locator_problems():
    assert PL.locator_problems({"kind": "page_literal", "names": [["appId", '":"']],
                                "right": '"', "min": 5, "max": 20}) == []
    assert PL.locator_problems({"kind": "json"})
    assert PL.locator_problems({"kind": "page_literal", "names": [["a b", "~"]], "min": 1, "max": 2})


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
