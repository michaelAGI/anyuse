"""Nested-layer slots (execution/api_slots.py): a parameter inside decoded layers.

Every fixture is synthetic and offline; transports are in-memory fakes. Five
request shapes, each found by discovery, filled and checked byte for byte:

* batchexecute-like: a form field holding a JSON array whose element is a
  JSON string (``f.req``);
* base64(JSON) state in a GET query value;
* GraphQL: ``variables`` as a JSON string, and an inline literal in the
  document (JSON-escaped);
* a percent-encoded nested URL (``next=/search?q=...``);
* a value that is part of a leaf (``"from:{q} lang:en"``).

Then what the POST rung's rules refuse over decoded layers (a hidden mutation,
a credential key or token), the depth cap, cross-recording agreement, the
fresh-input proof of a nested template, and old-format templates running
unchanged.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.parse

from anyuse.execution import api_slots as S
from anyuse.execution.api_compile import (
    Levers, compile_readonly_post, compile_recording, visible_items)
from anyuse.execution.api_step import (
    ApiResponse, ApiStepExecutor, RequestTemplate, readonly_post_operation)
from anyuse.execution.api_verify import compile_and_verify, verify_template
from anyuse.execution.workflow_schema import template_digest

J = {"Content-Type": "application/json"}


def compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def items_for(value):
    return [f"{value.title()} Item {i}" for i in range(1, 5)]


def response_for(value):
    return json.dumps({"results": [{"name": n} for n in items_for(value)]})


def recording(url, value, method="GET", post_data=None, headers=None, body=None):
    body = body if body is not None else response_for(value)
    net = {"url": url, "method": method, "status": 200, "mime": "application/json",
           "body": body, "bytes": len(body)}
    if post_data is not None:
        net["post_data"] = post_data
        net["headers"] = dict(headers or {"content-type": "application/x-www-form-urlencoded"})
    items = items_for(value)
    return {"param": {"name": "q", "value": value},
            "ui": {"items": items, "text": " ".join(items)},
            "final_url": "https://site.example/search", "network": [net]}


# --- batchexecute-like ---------------------------------------------------------

BE_URL = ("https://flights.example/_/FlightsUi/data/batchexecute"
          "?rpcids=Ab1Cd&source-path=%2Fsearch&hl=en&rt=c")


def be_body(value, extra=None):
    inner = [None, [value, 1, [3, "EUR"]], None]
    if extra is not None:
        inner.append(extra)
    outer = [[["Ab1Cd", compact(inner), None, "generic"]]]
    return "f.req=" + urllib.parse.quote(compact(outer), safe="") + "&"


def be_value(body):
    freq = urllib.parse.parse_qs(body)["f.req"][0]
    return json.loads(json.loads(freq)[0][0][1])[1][0]


# --- base64(JSON) state ----------------------------------------------------------

STATE_JSON = '{"filters": {"q": "granite", "sort": "rel"}, "page": 1, "note": "caf\\u00e9 \\/ x"}'


def state_url(state_json=STATE_JSON, extra="&lang=en"):
    b64 = base64.urlsafe_b64encode(state_json.encode()).decode().rstrip("=")
    return f"https://shop.example/api/search?state={b64}{extra}"


def decode_state(url):
    raw = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()


class StepGrammar(unittest.TestCase):

    def test_steps_parse_and_print_back(self):
        for raw in ("query:q", "query[2]:q", "form:f.req", "path:3", "body",
                    "json:/0/0/1", "json:", "b64", "pct", "query:a:b"):
            self.assertEqual(S.parse_step(raw).text(), raw)

    def test_position_rules(self):
        with self.assertRaises(S.SlotError):
            S.parse_at(["json:/a"])                      # must start at the request
        with self.assertRaises(S.SlotError):
            S.parse_at(["query:a", "body"])              # body only starts a path
        with self.assertRaises(S.SlotError):
            S.parse_at(["query:a"] + ["json:/x"] * 5)    # five layers: over the cap
        S.parse_at(["query:a"] + ["json:/x"] * 4)

    def test_template_braces(self):
        self.assertEqual(S.render_template("q {{ x(a: \"{q}\") }}", {"q": 'a"b'}, "json"),
                         'q { x(a: "a\\"b") }')
        with self.assertRaises(S.SlotError):
            S.parse_template("{ q")


class BatchExecute(unittest.TestCase):

    def setUp(self):
        self.rec = recording(BE_URL, "granite slab", "POST", be_body("granite slab"))

    def test_discovery_finds_the_value_two_json_layers_inside_the_form_field(self):
        slots, why = S.discover_slots(BE_URL, be_body("granite slab"), "granite slab", "q")
        self.assertEqual([s.at for s in slots],
                         [("form:f.req", "json:/0/0/1", "json:/1/0")], why)

    def test_the_post_rung_compiles_a_slotted_template(self):
        tmpl, why = compile_readonly_post(self.rec, visible_items(self.rec), "q",
                                          "granite slab", nested=True)
        self.assertIsNotNone(tmpl, why)
        self.assertTrue(tmpl.readonly_post)
        self.assertEqual(tmpl.slots, [{"param": "q",
                                       "at": ["form:f.req", "json:/0/0/1", "json:/1/0"]}])
        self.assertEqual(tmpl.body, be_body("granite slab"))     # captured verbatim
        self.assertEqual(tmpl.problems(), [])
        res = compile_recording(self.rec, levers=Levers(readonly_post=True))
        self.assertEqual(res.status, "readonly_post", res.reason)
        self.assertTrue(res.template.slots)

    def test_without_the_lever_the_nested_post_is_refused_as_before(self):
        tmpl, why = compile_readonly_post(self.rec, visible_items(self.rec), "q",
                                          "granite slab", nested=False)
        self.assertIsNone(tmpl)
        self.assertIn("re-bound", why)

    def test_fill_changes_only_the_value_bytes(self):
        tmpl, _ = compile_readonly_post(self.rec, visible_items(self.rec), "q",
                                        "granite slab", nested=True)
        url, _h, body = tmpl.render({"q": "granite slab"})
        self.assertEqual((url, body), (BE_URL, be_body("granite slab")))
        url, _h, body = tmpl.render({"q": "basalt tile"})
        self.assertEqual(url, BE_URL)
        self.assertEqual(body, be_body("granite slab").replace("granite%20slab",
                                                               "basalt%20tile"))
        self.assertEqual(body, be_body("basalt tile"))

    def test_a_hostile_value_stays_a_value_in_every_layer(self):
        tmpl, _ = compile_readonly_post(self.rec, visible_items(self.rec), "q",
                                        "granite slab", nested=True)
        hostile = 'x"]],null]&at=1&f.req=[["\\'
        _url, _h, body = tmpl.render({"q": hostile})
        self.assertEqual(be_value(body), hostile)
        self.assertEqual([k for k, _ in urllib.parse.parse_qsl(body)], ["f.req"])


class Base64State(unittest.TestCase):

    def test_discovery_and_byte_identity(self):
        url = state_url()
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query:state", "b64", "json:/filters/q")], why)
        # same length: no padding needed, so the recorded style suffices
        new_url, _ = S.fill_request(url, None, slots, {"q": "diorite"})
        # the decoded JSON keeps its spacing, \\u escape and \\/ escape
        self.assertEqual(decode_state(new_url), STATE_JSON.replace("granite", "diorite"))
        self.assertTrue(new_url.endswith("&lang=en"))
        self.assertEqual(S.fill_request(url, None, slots, {"q": "granite"})[0], url)

    def test_padding_no_recording_showed_is_refused_not_guessed(self):
        url = state_url()          # 81 bytes: the recording needed no padding, shows none
        slots, _why = S.discover_slots(url, None, "granite", "q")
        with self.assertRaises(S.SlotError):
            S.fill_request(url, None, slots, {"q": "marble"})        # 80 bytes needs it
        # another recording (79 bytes, unpadded) settles it: the step says nopad
        other = state_url(STATE_JSON.replace("granite", "slate"))
        ev = S.slot_evidence([recording(url, "granite"), recording(other, "slate")])
        annotated, refused = S.annotate_b64("GET", url, None, slots, ev)
        self.assertEqual((annotated[0].at[1], refused), ("b64:nopad", []))
        new_url, _ = S.fill_request(url, None, annotated, {"q": "marble"})
        self.assertNotIn("=", new_url.split("state=")[1].split("&")[0])
        self.assertEqual(decode_state(new_url), STATE_JSON.replace("granite", "marble"))

    def test_an_alphabet_no_recording_showed_is_refused(self):
        raw = base64.b64encode(compact({"q": "granite", "k": "ab"}).encode()).decode()
        self.assertFalse(any(c in raw for c in "+/-_="))
        url = "https://ex.test/s?st=" + raw
        slots, _why = S.discover_slots(url, None, "granite", "q")
        value = "~~~>"             # its base64 needs '+'
        self.assertIn("+", base64.b64encode(compact({"q": value, "k": "ab"}).encode()).decode())
        with self.assertRaises(S.SlotError):
            S.fill_request(url, None, slots, {"q": value})
        url_slot = [S.Slot("q", ("query:st", "b64:url,nopad", "json:/q"))]
        new, _ = S.fill_request(url, None, url_slot, {"q": value})
        enc = urllib.parse.parse_qs(urllib.parse.urlsplit(new).query)["st"][0]
        self.assertFalse(any(c in enc for c in "+/="))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)))["q"],
                         value)
        mixed = [S.b64_style("ab-c"), S.b64_style("ab+c")]
        with self.assertRaises(S.SlotError):
            S.b64_merge(mixed)

    def test_the_get_rung_compiles_it_by_default(self):
        rec = recording(state_url(), "granite")
        res = compile_recording(rec)
        self.assertEqual(res.status, "ok", res.reason)
        tmpl = res.template
        self.assertEqual(tmpl.method, "GET")
        self.assertEqual(tmpl.query, {})
        self.assertEqual(tmpl.slots[0]["at"], ["query:state", "b64", "json:/filters/q"])
        self.assertEqual(res.binding["where"], "nested")
        self.assertEqual(tmpl.problems(), [])
        off = compile_recording(rec, levers=Levers(nested_slots=False))
        self.assertIsNone(off.template)


class GraphQLInsideAString(unittest.TestCase):

    BODY = ('{"operationName":"Search","variables":"{\\"q\\":\\"granite\\",\\"first\\":10}",'
            '"query":"query Search($q: String!, $first: Int) { search(q: $q, first: $first) '
            '{ name } }"}')

    def test_variables_held_as_a_json_string(self):
        rec = recording("https://ex.test/graphql", "granite", "POST", self.BODY, J)
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite", nested=True)
        self.assertIsNotNone(tmpl, why)
        self.assertEqual(tmpl.slots[0]["at"], ["body", "json:/variables", "json:/q"])
        _u, _h, body = tmpl.render({"q": 'say "hi"'})
        self.assertEqual(json.loads(json.loads(body)["variables"])["q"], 'say "hi"')
        _u, _h, body = tmpl.render({"q": "basalt"})
        self.assertEqual(body, self.BODY.replace("granite", "basalt"))
        self.assertEqual(readonly_post_operation(body), "query:Search")

    def test_inline_literal_in_the_document_is_json_escaped(self):
        body = '{"query":"query { search(q: \\"granite\\") { name } }"}'
        slots, why = S.discover_slots("https://ex.test/graphql", body, "granite", "q")
        self.assertEqual(len(slots), 1, why)
        slot = slots[0]
        self.assertEqual((slot.at, slot.escape), (("body", "json:/query"), "json"))
        self.assertEqual(slot.template, 'query {{ search(q: "{q}") {{ name }} }}')
        _u, new = S.fill_request("https://ex.test/graphql", body, slots, {"q": 'a"b\\c'})
        self.assertEqual(json.loads(new)["query"], 'query { search(q: "a\\"b\\\\c") { name } }')

    def test_raw_text_in_graphql_source_is_not_a_slot(self):
        body = '{"query":"query { search(q: granite) { name } }"}'
        slots, _why = S.discover_slots("https://ex.test/graphql", body, "granite", "q")
        self.assertEqual(slots, [])

    def test_get_variables_in_the_query_string(self):
        url = ("https://ex.test/graphql?operationName=Search&variables="
               + urllib.parse.quote('{"q":"granite","first":10}', safe="")
               + "&extensions=%7B%22persistedQuery%22%3A%7B%22version%22%3A1%7D%7D")
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query:variables", "json:/q")], why)
        new, _ = S.fill_request(url, None, slots, {"q": "x&y=1"})
        self.assertEqual(new, url.replace("granite", "x%26y%3D1"))


class NestedUrl(unittest.TestCase):

    URL = "https://site.example/api/resolve?next=%2Fsearch%3Fq%3Dgranite%26page%3D2&src=nav"

    def test_structural_slot_through_the_nested_query(self):
        slots, why = S.discover_slots(self.URL, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query:next", "query:q")], why)
        new, _ = S.fill_request(self.URL, None, slots, {"q": "white marble"})
        self.assertEqual(new, self.URL.replace("granite", "white%2520marble"))
        nxt = urllib.parse.parse_qs(urllib.parse.urlsplit(new).query)["next"][0]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(nxt).query),
                         {"q": ["white marble"], "page": ["2"]})

    def test_value_in_the_nested_path_is_a_url_escaped_template(self):
        url = "https://site.example/api/go?next=%2Fuser%2Fgranite%2Fposts"
        slots, _why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([(s.at, s.template, s.escape) for s in slots],
                         [(("query:next",), "/user/{q}/posts", "url")])
        new, _ = S.fill_request(url, None, slots, {"q": "a/b c"})
        nxt = urllib.parse.parse_qs(urllib.parse.urlsplit(new).query)["next"][0]
        self.assertEqual(nxt, "/user/a%2Fb%20c/posts")


class SubstringTemplate(unittest.TestCase):

    URL = "https://social.example/api/search?q=from%3Agranite%20lang%3Aen&count=20"

    def test_part_of_a_leaf(self):
        rec = recording(self.URL, "granite")
        res = compile_recording(rec)
        self.assertIsNotNone(res.template, res.reason)
        self.assertEqual(res.template.slots,
                         [{"param": "q", "at": ["query:q"], "template": "from:{q} lang:en",
                           "escape": ""}])
        url, _h, _b = res.template.render({"q": "basalt"})
        self.assertEqual(url, self.URL.replace("granite", "basalt"))

    def test_the_value_must_stand_alone(self):
        url = "https://social.example/api/search?q=granitelike%20lang%3Aen"
        slots, _why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual(slots, [])


class ByteIdentity(unittest.TestCase):

    def test_untouched_bytes_of_every_touched_layer_survive(self):
        body = ('{ "a" : "x\\/y" ,  "variables" : "{\\"q\\": \\"granite\\", '
                '\\"x\\": \\"caf\\\\u00e9\\"}", "z": [1, 2.50, true], "a": "dup" }')
        slots, why = S.discover_slots("https://ex.test/g", body, "granite", "q")
        self.assertEqual([s.at for s in slots], [("body", "json:/variables", "json:/q")], why)
        _u, new = S.fill_request("https://ex.test/g", body, slots, {"q": "basalt"})
        self.assertEqual(new, body.replace("granite", "basalt"))
        # the same bytes a decode/re-encode would have destroyed
        self.assertIn('"x\\/y" ,  "variables" :', new)
        self.assertIn("2.50, true], \"a\": \"dup\" }", new)

    def test_query_string_style_is_kept(self):
        url = ("https://ex.test/s?a=x:y,z&variables=%7B%22q%22:%22granite%22%7D"
               "&b=%7e&c=hello+world#frag")
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query:variables", "json:/q")], why)
        new, _ = S.fill_request(url, None, slots, {"q": "big: rock"})
        # ':' was left raw in this value, so it stays raw; space follows %20
        self.assertEqual(new, url.replace("granite", "big:%20rock"))

    def test_plus_style_form_body_keeps_plus(self):
        outer = compact([[["Ab1Cd", compact([None, ["granite slab", 1]]), None, "generic"]]])
        body = "f.req=" + urllib.parse.quote_plus(outer, safe="") + "&at="
        slots, why = S.discover_slots(BE_URL, body, "granite slab", "q")
        self.assertEqual([s.at for s in slots], [("form:f.req", "json:/0/0/1", "json:/1/0")], why)
        _u, new = S.fill_request(BE_URL, body, slots, {"q": "basalt tile"})
        self.assertEqual(new, body.replace("granite+slab", "basalt+tile"))

    def test_duplicate_json_keys_resolve_to_the_last_as_json_loads_does(self):
        body = '{"q":"decoy","q":"granite","n":1}'
        slots, _why = S.discover_slots("https://ex.test/g", body, "granite", "q")
        self.assertEqual([s.at for s in slots], [("body", "json:/q")])
        _u, new = S.fill_request("https://ex.test/g", body, slots, {"q": "basalt"})
        self.assertEqual(new, '{"q":"decoy","q":"basalt","n":1}')
        self.assertEqual(json.loads(new)["q"], "basalt")

    def test_repeated_query_key_is_addressed_by_occurrence(self):
        url = ("https://ex.test/s?f=" + urllib.parse.quote('{"k":"x"}', safe="")
               + "&f=" + urllib.parse.quote('{"k":"granite"}', safe=""))
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query[1]:f", "json:/k")], why)
        new, _ = S.fill_request(url, None, slots, {"q": "basalt"})
        self.assertEqual(new, url.replace("granite", "basalt"))

    def test_a_path_segment_holding_base64_json(self):
        st = base64.urlsafe_b64encode(compact({"q": "granite", "n": 2}).encode()).decode()
        url = f"https://ex.test/api/state/{st}/list"
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("path:2", "b64", "json:/q")], why)
        # 21 bytes: the recording shows no padding either way; basalt (20) needs it
        with self.assertRaises(S.SlotError):
            S.fill_request(url, None, slots, {"q": "basalt"})
        nopad = [S.Slot("q", ("path:2", "b64:nopad", "json:/q"))]
        new, _ = S.fill_request(url, None, nopad, {"q": "basalt"})
        seg = new.split("/")[-2]
        self.assertNotIn("=", urllib.parse.unquote(seg))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))),
                         {"q": "basalt", "n": 2})
        self.assertTrue(new.startswith("https://ex.test/api/state/") and new.endswith("/list"))

    def test_a_twice_encoded_value_is_a_pct_layer(self):
        inner = urllib.parse.quote('{"q":"granite"}', safe="")
        url = "https://ex.test/s?x=" + urllib.parse.quote(inner, safe="")
        slots, why = S.discover_slots(url, None, "granite", "q")
        self.assertEqual([s.at for s in slots], [("query:x", "pct", "json:/q")], why)
        new, _ = S.fill_request(url, None, slots, {"q": "big rock"})
        self.assertEqual(new, url.replace("granite", "big%2520rock"))

    def test_number_leaf_keeps_its_type(self):
        url = "https://ex.test/s?v=" + urllib.parse.quote('{"year":2024,"q":"x"}', safe="")
        slots, _why = S.discover_slots(url, None, "2024", "year")
        self.assertEqual([(s.at, s.leaf) for s in slots], [(("query:v", "json:/year"), "number")])
        new, _ = S.fill_request(url, None, slots, {"year": "1999"})
        self.assertEqual(new, url.replace("2024", "1999"))
        with self.assertRaises(S.SlotError):
            S.fill_request(url, None, slots, {"year": "1999, \"x\": 1"})


class RulesOverDecodedLayers(unittest.TestCase):

    def _refused(self, post_data, headers=None):
        rec = recording("https://ex.test/graphql", "granite", "POST", post_data,
                        headers or J)
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite", nested=True)
        self.assertIsNone(tmpl, "compiled although it must be refused")
        return why

    def test_mutation_hidden_in_a_nested_string_refuses(self):
        body = compact({"query": "query S($q: String!) { search(q: $q) { name } }",
                        "variables": compact({"q": "granite",
                                              "next": "mutation Drop { dropAll }"})})
        self.assertIn("mutation", self._refused(body))

    def test_mutation_inside_a_form_field_json_refuses(self):
        self.assertIn("mutation", self._refused(
            be_body("granite", {"query": "mutation Drop { dropAll }"})))

    def test_mutation_inside_base64_refuses(self):
        b64 = base64.b64encode(compact({"query": "subscription T { ticks }"}).encode()).decode()
        body = compact({"variables": {"q": "granite"}, "state": b64})
        self.assertIn("subscription", self._refused(body))

    def test_credential_key_in_a_nested_layer_refuses(self):
        why = self._refused(be_body("granite", {"access_token": "abc"}))
        self.assertIn("access_token", why)
        why = self._refused(compact({"q": "granite",
                                     "ctx": "/x?session_id=1&q=2"}))
        self.assertIn("session_id", why)

    def test_credential_value_in_a_nested_layer_refuses(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
        why = self._refused(be_body("granite", {"viewer": jwt}))
        self.assertIn("JSON Web Token", why)

    def test_the_bound_request_is_judged_again_before_send(self):
        url = "https://ex.test/api?variables=" + urllib.parse.quote('{"q":"granite"}', safe="")
        rec = recording(url, "granite")
        tmpl = compile_recording(rec).template
        self.assertTrue(tmpl.slots)

        class Boom:
            def send(self, *a, **k):                       # pragma: no cover
                raise AssertionError("sent")

        out = ApiStepExecutor(transport=Boom()).execute(tmpl, {"q": "mutation X { drop }"})
        self.assertFalse(out["success"])
        self.assertTrue(out["blocked"])
        self.assertIn("mutation", out["error"])

    def test_a_forged_slotted_template_is_judged_from_its_text(self):
        forged = RequestTemplate(
            method="POST", url="https://ex.test/graphql", readonly_post=True,
            headers={"content-type": "application/json"}, params=["q"],
            body=compact({"variables": compact({"q": "x", "m": "mutation M { del }"})}),
            slots=[{"param": "q", "at": ["body", "json:/variables", "json:/q"]}])
        self.assertTrue(any("mutation" in p for p in forged.problems()))


class DepthCap(unittest.TestCase):

    @staticmethod
    def nested(levels):
        node = {"x": "granite"}
        for _ in range(levels - 1):
            node = {"x": compact(node)}
        return "https://ex.test/api?a=" + urllib.parse.quote(compact(node), safe="")

    def test_four_layers_bind_and_five_do_not(self):
        slots, why = S.discover_slots(self.nested(4), None, "granite", "q")
        self.assertEqual(len(slots[0].at), 5, why)
        self.assertEqual(S.deep_refusal(self.nested(4)), "")
        slots, why = S.discover_slots(self.nested(5), None, "granite", "q")
        self.assertEqual(slots, [])
        self.assertIn("4-layer cap", why)
        self.assertIn("deeper than 4", S.deep_refusal(self.nested(5)))
        res = compile_recording(recording(self.nested(5), "granite"))
        self.assertIsNone(res.template)

    def test_json_nested_too_deep_is_refused_not_skipped(self):
        deep_body = "[" * (S.MAX_JSON_NESTING + 5) + "]" * (S.MAX_JSON_NESTING + 5)
        self.assertIn("nests deeper", S.deep_refusal("https://ex.test/g", deep_body))
        very_deep = "[" * 5000 + "]" * 5000         # under the size cap
        url = "https://ex.test/api?a=" + urllib.parse.quote(very_deep, safe="")
        # CPython versions differ on whether decoding reaches our own depth
        # guard. Both paths must refuse the request for excessive nesting.
        self.assertRegex(S.deep_refusal(url), "too deeply|nests deeper")

    def test_an_oversized_layer_is_refused_not_skipped(self):
        big = compact({"x": "granite", "pad": "y" * (S.MAX_DECODE_CHARS + 10)})
        url = "https://ex.test/api?a=" + urllib.parse.quote(big, safe="")
        self.assertIn("cap", S.deep_refusal(url))


class Agreement(unittest.TestCase):

    def test_recordings_must_agree_on_where_the_value_sits(self):
        def rec(value, key):
            st = compact({"filters": {key: value}, "page": 1})
            return recording(state_url(st, extra=""), value)
        agree = S.slot_evidence([rec("granite", "q"), rec("marble", "q")])
        disagree = S.slot_evidence([rec("granite", "q"), rec("marble", "title")])
        slot = S.Slot("q", ("query:state", "b64", "json:/filters/q"))
        url = state_url(compact({"filters": {"q": "granite"}, "page": 1}), extra="")
        self.assertTrue(agree.corroborates("GET", url, slot)[0])
        ok, why = disagree.corroborates("GET", url, slot)
        self.assertFalse(ok)
        self.assertIn("elsewhere", why)


class FreshInputProof(unittest.TestCase):

    class StateServer:
        """Answers the search the nested state asks for, as the site would."""

        def __init__(self):
            self.sent = []

        def send(self, method, url, headers, body, timeout_s):
            self.sent.append((method, url, body))
            q = json.loads(decode_state(url))["filters"]["q"]
            return ApiResponse(200, J, response_for(q))

    def test_compile_and_verify_proves_a_nested_template(self):
        def rec(value):
            return recording(state_url(STATE_JSON.replace("granite", value)), value)
        server = self.StateServer()
        compiled, proof = compile_and_verify(
            rec("granite"), [rec("marble"), rec("basalt")],
            executor=ApiStepExecutor(transport=server), fitting_recordings=[rec("slate")])
        self.assertTrue(compiled.template.slots, compiled.reason)
        self.assertTrue(proof.passed, proof.reason)
        self.assertEqual(sorted(c.value for c in proof.cases), ["basalt", "marble"])
        self.assertEqual(decode_state(server.sent[0][1]), STATE_JSON.replace("granite", "marble"))

    def test_a_nested_post_is_proved_with_its_approval(self):
        from anyuse.execution.api_permission import acknowledge_host

        class BeServer:
            def send(self, method, url, headers, body, timeout_s):
                return ApiResponse(200, J, response_for(be_value(body)))

        rec = recording(BE_URL, "granite slab", "POST", be_body("granite slab"))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite slab",
                                          nested=True)
        self.assertIsNotNone(tmpl, why)
        cases = [{"value": v, "ui_items": items_for(v)} for v in ("basalt tile", "slate roof")]
        old = os.environ.get("ANYUSE_API_READONLY_POST")
        os.environ["ANYUSE_API_READONLY_POST"] = "on"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                hosts = os.path.join(tmp, "hosts.json")
                acknowledge_host("flights.example", path=hosts,
                                 readonly_post_endpoint=BE_URL.split("?")[0],
                                 readonly_post_operation=readonly_post_operation(tmpl.body))
                proof = verify_template(tmpl, cases, ApiStepExecutor(
                    transport=BeServer(), readonly_post_hosts_path=hosts))
        finally:
            if old is None:
                os.environ.pop("ANYUSE_API_READONLY_POST", None)
            else:
                os.environ["ANYUSE_API_READONLY_POST"] = old
        self.assertTrue(proof.passed, proof.reason)


class UninspectableIsRefused(unittest.TestCase):
    """Review item 1: what the walk cannot read refuses the request."""

    def test_each_probe_is_refused(self):
        url = "https://ex.test/g"
        probes = {
            "a pair value with an invalid UTF-8 escape":
                compact({"ctx": "x=1&query=%FFmutation%20M%20%7Bdel%7D"}),
            "a pair key with an invalid UTF-8 escape": compact({"ctx": "x=1&%FFk=v"}),
            "a form string with a '+' in a key": compact({"ctx": "first+name=a&access_token=abc"}),
            "a pair without '='": compact({"ctx": "flag&session_id=abc"}),
            "a nested URL's fragment": compact({"next": "/cb?x=1#access_token=abc123"}),
            "a JSON-shaped string that does not parse": compact({"x": '{"access_token":"a",}'}),
            "percent-encoded text that is not UTF-8": compact({"x": "%FF%FE"}),
        }
        for name, body in probes.items():
            self.assertTrue(S.deep_refusal(url, body), name)

    def test_the_shadowed_duplicate_key_is_inspected(self):
        body = ('{"query":"query S($q: String!) { search(q: $q) { name } }",'
                '"variables":{"q":"granite"},"v":"mutation M { del }","v":"ok"}')
        self.assertEqual(json.loads(body)["v"], "ok")         # what json.loads alone sees
        why = S.deep_refusal("https://ex.test/graphql", body)
        self.assertIn("mutation", why)
        rec = recording("https://ex.test/graphql", "granite", "POST", body, J)
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite")
        self.assertIsNone(tmpl)
        self.assertIn("mutation", why)
        # a shadowed duplicate is never a slot: the pointer reaches the last one
        slots, _why = S.discover_slots("https://ex.test/g", '{"q":"granite","q":"decoy"}',
                                       "granite", "q")
        self.assertEqual(slots, [])

    def test_unparsable_json_holding_equals_is_never_read_as_pairs(self):
        why = "a JSON-shaped string that does not parse"
        body = compact({"f": "{'query':'mutation { del(id: \"=1\") }'}"})
        self.assertIn(why, S.deep_refusal("https://ex.test/g", body))
        frag = urllib.parse.quote("{'a':'mutation { d(i: \"=1\") }'}", safe="")
        self.assertIn(why, S.deep_refusal("https://ex.test/g?x=1#" + frag))

    def test_a_body_with_a_trailing_newline_is_walked_and_an_unknown_body_refused(self):
        self.assertIn("access_token",
                      S.deep_refusal("https://ex.test/g", "q=granite&access_token=abc\n"))
        self.assertIn("neither JSON nor a form encoding",
                      S.deep_refusal("https://ex.test/g", "plain words\n"))

    def test_every_read_only_post_rechecks_its_nested_layers(self):
        plain = RequestTemplate(
            method="POST", url="https://ex.test/graphql", readonly_post=True,
            headers={"content-type": "application/json"}, params=["q"],
            body=compact({"query": "query S($q: String!) { s(q: $q) { n } }",
                          "variables": compact({"q": "x", "access_token": "t"}),
                          "q": "{q}"}))
        self.assertEqual(plain.slots, [])
        self.assertTrue(any("access_token" in p for p in plain.problems()), plain.problems())

    def test_ordinary_values_stay_readable(self):
        body = compact({"cursor": "YXJyYXljb25uZWN0aW9uOjk=", "q": "gene mutation (BRCA1)",
                        "note": "50% off", "math": "a = b"})
        self.assertEqual(S.deep_refusal("https://ex.test/g", body), "")


def html_page(value):
    cards = "".join(f'<div class="card"><h3 class="title">{n}</h3><p>about {n}</p></div>'
                    for n in items_for(value))
    nav = "".join(f'<a href="/n{i}">Nav {i}</a>' for i in range(5))
    return f'<html><body><nav>{nav}</nav><div class="results">{cards}</div></body></html>'


def state_param(value):
    raw = base64.urlsafe_b64encode(compact({"q": value, "page": 1}).encode()).decode()
    return "state=" + raw.rstrip("=")


class LeverFIsAFallbackForTheWholeLadder(unittest.TestCase):
    """Review item 2: a recording the other levers compile compiles exactly as
    with lever (f) off, whichever rung or candidate a nested slot would favour."""

    def assert_identical(self, rec, on, off):
        a, b = compile_recording(rec, levers=on), compile_recording(rec, levers=off)
        self.assertIsNotNone(b.template, b.reason)
        self.assertEqual(a.template.to_dict(), b.template.to_dict())
        self.assertEqual(a.template.slots, [])
        return a.template

    def test_json_rung_two_candidates(self):
        nested_url = f"https://api.example/a?{state_param('granite')}"
        plain_url = "https://api2.example/b?q=granite"
        bigger = json.dumps({"results": [{"name": n, "pad": "x" * 40}
                                         for n in items_for("granite")]})
        rec = recording(nested_url, "granite")
        rec["network"].append({"url": plain_url, "method": "GET", "status": 200,
                               "mime": "application/json", "body": bigger,
                               "bytes": len(bigger)})
        tmpl = self.assert_identical(rec, Levers(), Levers(nested_slots=False))
        self.assertTrue(tmpl.url.startswith("https://api2.example/b"))
        self.assertEqual(tmpl.query, {"q": "{q}"})

    def test_html_rung_two_documents(self):
        page = html_page("granite")
        rec = {"param": {"name": "q", "value": "granite"},
               "ui": {"items": items_for("granite"), "text": ""},
               "final_url": "https://site.example/t?q=granite",
               "network": [{"url": f"https://site.example/s?{state_param('granite')}",
                            "method": "GET", "status": 200, "mime": "text/html",
                            "body": page, "bytes": len(page)},
                           {"url": "https://site.example/t?q=granite", "method": "GET",
                            "status": 200, "mime": "text/html", "body": page,
                            "bytes": len(page)}]}
        tmpl = self.assert_identical(rec, Levers(), Levers(nested_slots=False))
        self.assertEqual(tmpl.extract_kind, "html")
        self.assertTrue(tmpl.url.startswith("https://site.example/t"))
        # and alone, the nested document compiles through lever (f)
        rec["network"] = rec["network"][:1]
        alone = compile_recording(rec)
        self.assertTrue(alone.template is not None and alone.template.slots, alone.reason)

    def test_post_rung_two_posts(self):
        nested = GraphQLInsideAString.BODY
        rec = recording("https://ex.test/graphql", "granite", "POST", nested, J)
        plain = compact({"query": "granite", "page": 1})
        rec["network"].append({"url": "https://ex.test/search", "method": "POST",
                               "status": 200, "mime": "application/json",
                               "post_data": plain, "headers": dict(J),
                               "body": response_for("granite"), "bytes": 10})
        tmpl = self.assert_identical(rec, Levers(readonly_post=True),
                                     Levers(readonly_post=True, nested_slots=False))
        self.assertEqual(tmpl.url, "https://ex.test/search")


class NoInjectionThroughATemplate(unittest.TestCase):
    """Review item 3."""

    def test_a_url_with_raw_spaces_is_still_a_url(self):
        url, body = "https://ex.test/g", compact({"next": "/search?q=granite slab&lang=en"})
        slots, why = S.discover_slots(url, body, "granite slab", "q")
        self.assertEqual([s.at for s in slots], [("body", "json:/next", "query:q")], why)
        _u, new = S.fill_request(url, body, slots, {"q": "x&lang=fr#"})
        nxt = json.loads(new)["next"]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(nxt).query),
                         {"q": ["x&lang=fr#"], "lang": ["en"]})
        self.assertEqual(S.fill_request(url, body, slots, {"q": "granite slab"})[1], body)

    def test_a_raw_hit_in_url_syntax_is_text_escaped(self):
        url, body = "https://ex.test/g", compact({"next": "/user/granite slab/posts"})
        slots, why = S.discover_slots(url, body, "granite slab", "q")
        self.assertEqual([(s.template, s.escape) for s in slots],
                         [("/user/{q}/posts", "text")], why)
        _u, new = S.fill_request(url, body, slots, {"q": "basalt tile"})
        self.assertEqual(new, body.replace("granite slab", "basalt tile"))
        for hostile in ("..", ".", "x?y=1", "x#y", "x&y", "100%", "a+b", "a/b"):
            with self.assertRaises(S.SlotError, msg=hostile):
                S.fill_request(url, body, slots, {"q": hostile})

    def test_a_dot_segment_hole_is_refused(self):
        url, body = "https://ex.test/g", compact({"next": "/users/foo/repos"})
        slots, why = S.discover_slots(url, body, "foo", "q")
        self.assertEqual([(s.template, s.escape) for s in slots],
                         [("/users/{q}/repos", "url")], why)
        for dots in ("..", "."):
            with self.assertRaises(S.SlotError, msg=dots):
                S.fill_request(url, body, slots, {"q": dots})
        _u, new = S.fill_request(url, body, slots, {"q": "bar"})
        self.assertEqual(json.loads(new)["next"], "/users/bar/repos")
        self.assertNotEqual(S.leaf_structure("/users/foo/repos"),
                            S.leaf_structure("/users/%2E%2E/repos"))

    def test_prose_with_a_question_mark_or_an_ampersand_is_not_url_syntax(self):
        url = "https://ex.test/g"
        body = compact({"q": "what is granite slab?"})
        slots, why = S.discover_slots(url, body, "granite slab", "q")
        self.assertEqual([(s.template, s.escape) for s in slots], [("what is {q}?", "")], why)
        _u, new = S.fill_request(url, body, slots, {"q": "C++ & more"})
        self.assertEqual(json.loads(new)["q"], "what is C++ & more?")
        body = compact({"q": "what is granite?"})
        slots, _why = S.discover_slots(url, body, "granite", "q")
        _u, new = S.fill_request(url, body, slots, {"q": "basalt slab"})
        self.assertEqual(json.loads(new)["q"], "what is basalt slab?")

    def test_the_read_back_compares_the_leaf_syntax(self):
        url, body = "https://ex.test/g", compact({"next": "/user/granite slab/posts"})
        raw = [S.Slot("q", ("body", "json:/next"), template="/user/{q}/posts", escape="")]
        with self.assertRaises(S.SlotError):
            S.fill_request(url, body, raw, {"q": "x?y=1#z"})       # adds a query
        problems = S.slot_problems(url, body, [raw[0].to_dict()], {"q"})
        self.assertTrue(any("raw into a URL/form-shaped" in p for p in problems), problems)


class PathBorneSlots(unittest.TestCase):
    """Review item 4."""

    @staticmethod
    def rec(value):
        st = base64.urlsafe_b64encode(compact({"q": value, "n": 2}).encode()).decode()
        return recording(f"https://ex.test/api/state/{st}/list", value)

    def test_agreement_needs_three_recordings_and_the_same_pointer(self):
        recs = [self.rec(v) for v in ("granite", "marble", "slate")]
        url = recs[0]["network"][0]["url"]
        good = S.Slot("q", ("path:2", "b64", "json:/q"))
        wrong = S.Slot("q", ("path:2", "b64", "json:/n"))
        three = S.slot_evidence(recs)
        self.assertEqual(three.corroborates("GET", url, good), (True, "3 recording(s) agree"))
        ok, why = three.corroborates("GET", url, wrong)
        self.assertFalse(ok)
        self.assertIn("elsewhere", why)
        ok, why = S.slot_evidence(recs[:2]).corroborates("GET", url, good)
        self.assertFalse(ok)
        self.assertIn("needs 3", why)
        kept, refused = S.corroborated("GET", url, [good], None)
        self.assertEqual(kept, [])
        self.assertIn("path segment", refused[0])

    def test_zero_voters_is_no_agreement(self):
        url = state_url()
        slot = S.Slot("q", ("query:state", "b64", "json:/filters/q"))
        elsewhere = S.slot_evidence([recording("https://other.example/x?q=a1b", "a1b"),
                                     recording("https://other.example/x?q=c2d", "c2d")])
        ok, why = elsewhere.corroborates("GET", url, slot)
        self.assertFalse(ok)
        self.assertIn("only 0 recording(s)", why)

    def test_a_segment_s_own_decodes_are_never_leaves(self):
        for url, value in (("https://ex.test/search/granite%2520slab", "granite slab"),
                           ("https://ex.test/s/k=granite", "granite")):
            slots, _why = S.discover_slots(url, None, value, "q")
            self.assertEqual(slots, [], url)
        self.assertIn("access_token", S.deep_refusal("https://ex.test/s/access_token=abc"))

    def test_the_compiler_binds_a_path_slot_only_on_three_recordings(self):
        one = compile_recording(self.rec("granite"))
        self.assertIsNone(one.template)
        self.assertIn("path segment", one.reason)
        recs = [self.rec(v) for v in ("granite", "marble", "slate")]
        two = compile_recording(recs[0], slot_evidence=S.slot_evidence(recs[:2]))
        self.assertIsNone(two.template)
        three = compile_recording(recs[0], slot_evidence=S.slot_evidence(recs))
        self.assertIsNotNone(three.template, three.reason)
        self.assertEqual(three.template.slots[0]["at"][0], "path:2")
        url, _h, _b = three.template.render({"q": "basalt"})
        seg = urllib.parse.unquote(url.split("/")[-2])
        self.assertEqual(json.loads(base64.urlsafe_b64decode(seg)), {"q": "basalt", "n": 2})


class MutationRuleIsScopedToGraphQL(unittest.TestCase):
    """Review item 7: the rule over decoded layers stays on with lever (f) off,
    and judges GraphQL, not words."""

    def test_a_search_for_the_word_mutation_compiles(self):
        value = "gene mutation (BRCA1)"
        body = compact({"query": value, "page": 1})
        rec = recording("https://ex.test/search", value, "POST", body, J)
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", value, nested=False)
        self.assertIsNotNone(tmpl, why)

    def test_documents_after_ignored_tokens_and_persisted_writes_refuse(self):
        url = "https://ex.test/g"
        for doc in (",mutation M { del }", "\ufeffmutation M { del }",
                    ", # note\n mutation M { del }"):
            self.assertIn("mutation or a subscription",
                          S.deep_refusal(url, compact({"doc": doc})), repr(doc))
        persisted = compact({"operationName": "CreateTweetMutation", "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": "a" * 64}}})
        self.assertIn("'CreateTweetMutation' is named as a mutation",
                      S.deep_refusal(url, persisted))
        self.assertIn("'useCometLikeMutation' is named as a mutation", S.deep_refusal(
            url, "fb_api_req_friendly_name=useCometLikeMutation&doc_id=7145285212184222"))
        self.assertIn("'operation' says mutation", S.deep_refusal(
            url, compact({"kind": "OperationDefinition", "operation": "mutation"})))

    def test_an_operation_type_field_and_a_token_header_still_refuse(self):
        body = compact({"operationName": "Add", "operationType": "mutation",
                        "variables": {"q": "granite"}})
        self.assertIn("operationType", S.deep_refusal("https://ex.test/g", body))
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
        rec = recording("https://ex.test/search", "granite", "POST",
                        compact({"query": "granite"}), {"content-type": "application/json",
                                                        "x-trace": jwt})
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite", nested=False)
        self.assertIsNone(tmpl)
        self.assertIn("JSON Web Token", why)


class GraphQLLiterals(unittest.TestCase):

    def test_quotes_in_a_comment_open_nothing(self):
        doc = 'query {\n  # a " stray quote\n  search(q: "granite") { name }\n}'
        body = compact({"query": doc})
        slots, why = S.discover_slots("https://ex.test/graphql", body, "granite", "q")
        self.assertEqual([s.escape for s in slots], ["json"], why)
        _u, new = S.fill_request("https://ex.test/graphql", body, slots, {"q": 'a"b'})
        self.assertIn('search(q: "a\\"b")', json.loads(new)["query"])

    def test_a_hit_in_a_comment_or_a_block_string_is_unsure(self):
        for doc in ('query {\n  # see "granite"\n  search(q: "granite") { name }\n}',
                    'query { search(q: """granite""") { name } }'):
            slots, _why = S.discover_slots("https://ex.test/graphql", compact({"query": doc}),
                                           "granite", "q")
            self.assertEqual(slots, [], doc)


class RoundTripOnEveryRung(unittest.TestCase):
    """Review item 7: a slotted template keeps its minor and its pinned digest
    through to_dict/from_dict, whichever rung built it."""

    def check(self, tmpl):
        from anyuse.execution.api_compile import compiled_record
        self.assertTrue(tmpl.slots)
        first = tmpl.to_dict()
        self.assertEqual(first.get("schema_minor"), S.SLOTS_SCHEMA_MINOR)
        back = RequestTemplate.from_dict(json.loads(json.dumps(first)))
        self.assertEqual(back.to_dict(), first)
        record = json.loads(json.dumps(compiled_record(tmpl, {"passed": True})))
        reloaded = RequestTemplate.from_dict(record["template"]).to_dict()
        self.assertEqual(template_digest(reloaded), record["proof"]["template_digest"])

    def test_get_rung(self):
        self.check(compile_recording(recording(state_url(), "granite")).template)

    def test_post_rung(self):
        rec = recording(BE_URL, "granite slab", "POST", be_body("granite slab"))
        tmpl, why = compile_readonly_post(rec, visible_items(rec), "q", "granite slab",
                                          nested=True)
        self.assertIsNotNone(tmpl, why)
        self.check(tmpl)
        self.check(compile_recording(rec, levers=Levers(readonly_post=True)).template)

    def test_document_rung(self):
        page = html_page("granite")
        rec = {"param": {"name": "q", "value": "granite"},
               "ui": {"items": items_for("granite"), "text": ""},
               "final_url": "https://site.example/s",
               "network": [{"url": f"https://site.example/s?{state_param('granite')}",
                            "method": "GET", "status": 200, "mime": "text/html",
                            "body": page, "bytes": len(page)}]}
        res = compile_recording(rec)
        self.assertEqual(res.template.extract_kind, "html", res.reason)
        self.check(res.template)


class TemplateVersions(unittest.TestCase):

    def test_a_newer_minor_or_version_is_refused_before_sending(self):
        from anyuse.execution.api_compile import compiled_record, proof_matches_template
        tmpl = compile_recording(recording(state_url(), "granite")).template
        record = compiled_record(tmpl, {"passed": True})
        self.assertTrue(proof_matches_template(record))
        from anyuse.execution.api_step import PAGE_SCHEMA_MINOR
        # the newest minor this runner reads: slots (1), page values (2)
        for field_name, value in (("schema_minor", max(S.SLOTS_SCHEMA_MINOR, PAGE_SCHEMA_MINOR) + 1),
                                  ("schema_version", 3), ("schema_minor", "1")):
            data = dict(tmpl.to_dict(), **{field_name: value})
            newer = RequestTemplate.from_dict(data)
            problems = newer.problems()
            self.assertTrue(problems and "format" in problems[0], (field_name, value))

            class Boom:
                def send(self, *a, **k):                   # pragma: no cover
                    raise AssertionError("sent")

            out = ApiStepExecutor(transport=Boom()).execute(newer, {"q": "diorite"})
            self.assertFalse(out["success"])
            # the proof digest pins the format fields and the slots
            changed = dict(record, template=data)
            self.assertFalse(proof_matches_template(changed))
        no_slots = dict(record, template={k: v for k, v in record["template"].items()
                                          if k != "slots"})
        self.assertFalse(proof_matches_template(no_slots))


class OldFormat(unittest.TestCase):

    OLD = {"schema_version": 2, "method": "GET", "url": "https://crates.example/api/v1/crates",
           "query": {"q": "{q}", "per_page": "10"}, "headers": {"accept": "application/json"},
           "body": None, "timeout_s": 10.0, "extract": {"items": "crates[*].name"},
           "extract_kind": "json", "embedded": {}, "shape": "list",
           "expect": {"status_in": [200], "content_type": "json", "path": "crates",
                      "min_items": 1},
           "params": ["q"], "origin": "deterministic", "notes": "", "readonly_post": False,
           "path_params": {}, "param_transforms": {}, "approved_origins": [],
           "parameter_schema": [
               {"name": "q", "kind": "input", "type": "string", "max_length": 2048},
               {"name": "const_query_per_page", "kind": "const", "type": "string",
                "value": "10", "max_length": 2, "destinations": ["query:per_page"]}]}

    def test_an_old_template_loads_serializes_and_runs_unchanged(self):
        tmpl = RequestTemplate.from_dict(self.OLD)
        self.assertEqual(tmpl.to_dict(), self.OLD)
        self.assertEqual(template_digest(tmpl.to_dict()), template_digest(self.OLD))
        self.assertNotIn("slots", tmpl.to_dict())

        class Recorder:
            sent = []

            def send(self, method, url, headers, body, timeout_s):
                self.sent.append(url)
                return ApiResponse(200, J, json.dumps({"crates": [{"name": "serde"}]}))

        tr = Recorder()
        out = ApiStepExecutor(transport=tr).execute(tmpl, {"q": "serde json"})
        self.assertTrue(out["success"], out.get("error"))
        self.assertEqual(tr.sent, ["https://crates.example/api/v1/crates?q=serde+json&per_page=10"])

    def test_a_slotted_template_round_trips_with_its_minor_version(self):
        tmpl = compile_recording(recording(state_url(), "granite")).template
        data = tmpl.to_dict()
        self.assertEqual((data["schema_version"], data["schema_minor"]), (2, S.SLOTS_SCHEMA_MINOR))
        back = RequestTemplate.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(back.to_dict(), data)
        self.assertEqual(back.render({"q": "diorite"}), tmpl.render({"q": "diorite"}))

    def test_plain_bindings_compile_exactly_as_without_the_lever(self):
        url = "https://crates.example/api/v1/crates?q=granite&per_page=10"
        on = compile_recording(recording(url, "granite"))
        off = compile_recording(recording(url, "granite"), levers=Levers(nested_slots=False))
        self.assertEqual(on.template.to_dict(), off.template.to_dict())
        self.assertEqual(on.template.slots, [])


if __name__ == "__main__":
    unittest.main()
