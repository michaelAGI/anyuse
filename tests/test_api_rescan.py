"""Rescan (execution/api_rescan.py): a rotated persisted-query id read back from
the site's own bundles, without a browser.

Synthetic bundle texts shaped like the three real carriers: X's ``queryId``
next to ``operationName`` in an exported object (and in the request path),
Meta's ``doc_id`` exported from a ``__d(...)`` module (in a form body), and
Apollo's ``sha256Hash`` (in a JSON body). One test per rule, the tie case,
token swapping in old- and new-format templates, bundle URL listing and the
heal loop guard. Nothing here touches a network.
"""
from __future__ import annotations

import unittest
import urllib.parse

from anyuse.execution import api_rescan as R
from anyuse.execution.api_step import RequestTemplate

OLD_QID = "AbCdEfGhIjKlMnOpQrStUv"            # 22, base64url family
NEW_QID = "Yka-W8dz7RaEuQNkroPkYw"
OTHER_QID = "Zz9_q8dz7RaEuQNkroPkYw"


def x_module(qid, name="UserByScreenName", extra=""):
    return (f'e.exports={{queryId:"{qid}",operationName:"{name}",operationType:"query",'
            f'metadata:{{featureSwitches:["responsive_web_graphql"]{extra}}}}}')


class Rules(unittest.TestCase):

    def test_finds_the_rotated_token_with_evidence(self):
        bundle = "var a=1;" + x_module(NEW_QID) + ";var b=2;"
        res = R.rescan(OLD_QID, "UserByScreenName", {"main.js": bundle})
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.token, NEW_QID)
        ev = res.evidence
        self.assertEqual((ev.file, ev.offset), ("main.js", bundle.index(NEW_QID)))
        self.assertEqual(ev.distance, len('",operationName:"'))
        self.assertTrue(ev.in_code and ev.anchor_in_code and ev.same_group)

    def test_shape_charset_and_length(self):
        # nearer, but 21 characters; nearer still, but with a '+' (base64 family)
        short, plus = "Shrt-W8dz7RaEuQNkroPk", "Plus+W8dz7RaEuQNkroPkY"
        self.assertEqual((len(short), len(plus)), (21, 22))
        bundle = ('{queryId:"' + NEW_QID + '",id:"' + short + '",'
                  'docId:"' + plus + '",operationName:"UserByScreenName"}')
        self.assertEqual(R.rescan(OLD_QID, "UserByScreenName", {"m.js": bundle}).token, NEW_QID)
        self.assertEqual(R.token_shape("7145285212184222").family, "digits")
        self.assertEqual(R.token_shape("a" * 64).family, "hex")
        self.assertEqual(R.token_shape(OLD_QID).family, "base64url")   # successor may hold - _
        self.assertIsNone(R.token_shape("short"))

    def test_a_token_cut_out_of_a_longer_run_is_no_candidate(self):
        bundle = '{x:"' + NEW_QID + 'Tail",operationName:"UserByScreenName"}'
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": bundle})
        self.assertFalse(res.ok)

    def test_window(self):
        far = '{queryId:"' + NEW_QID + '",' + "pad:1," * 60 + 'operationName:"UserByScreenName"}'
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": far})
        self.assertFalse(res.ok)
        self.assertIn("within 300", res.reason)
        self.assertTrue(R.rescan(OLD_QID, "UserByScreenName", {"m.js": far}, window=400).ok)

    def test_anchor_must_stand_as_its_own_name(self):
        only_longer = x_module(NEW_QID, name="FollowersYouKnow")
        res = R.rescan(OLD_QID, "Followers", {"m.js": only_longer})
        self.assertFalse(res.ok)
        self.assertIn("does not stand", res.reason)
        both = x_module(OTHER_QID, name="FollowersYouKnow") + ";" + x_module(NEW_QID, "Followers")
        self.assertEqual(R.rescan(OLD_QID, "Followers", {"m.js": both}).token, NEW_QID)

    def test_code_beats_prose(self):
        # the nearer token sits by the anchor's mention in a log message
        bundle = ('{queryId:"' + NEW_QID + '",a:1,b:2,c:3,d:4,operationName:"UserByScreenName"};'
                  'log("UserByScreenName failed", {queryId:"' + OTHER_QID + '"})')
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": bundle})
        self.assertEqual(res.token, NEW_QID, res.reason)
        self.assertTrue(res.evidence.in_code and res.evidence.anchor_in_code)

    def test_only_a_persisted_id_property_value_is_a_candidate(self):
        """Review item 5: a token planted in user content is never picked."""
        bundle = "e.exports=" + x_module(NEW_QID)[len("e.exports="):]
        evil = "EviL" + NEW_QID[4:]
        page = ('<html><head><script>window.__S={"text":"UserByScreenName:' + evil + '"}'
                '</script><script type="application/json">{"queryId":"' + evil +
                '","operationName":"UserByScreenName"}</script></head><body><p>'
                'UserByScreenName ' + evil + '</p><a title="queryId:' + evil +
                '">UserByScreenName</a></body></html>')
        state = '{"queryId":"' + evil + '","operationName":"UserByScreenName"}'
        res = R.rescan(OLD_QID, "UserByScreenName",
                       {"doc.html": page, "main.js": bundle, "state.json": state})
        self.assertEqual(res.token, NEW_QID, res.reason)
        self.assertEqual(res.evidence.file, "main.js")
        alone = R.rescan(OLD_QID, "UserByScreenName", {"doc.html": page, "state.json": state})
        self.assertFalse(alone.ok, alone.reason)
        # an inline script of a JavaScript type is code
        inline = '<html><script type="module">' + x_module(NEW_QID) + "</script></html>"
        self.assertEqual(R.rescan(OLD_QID, "UserByScreenName", {"d": inline}).token, NEW_QID)

    def _planted(self, quote):
        evil = "EviL" + NEW_QID[4:]
        state = ('window.__STATE__={"full_text":"queryId:' + quote + evil + quote +
                 ',SearchTimeline"};')
        return evil, "<html><head><script>" + state + "</script></head><body></body></html>"

    def test_a_quoted_token_inside_a_json_string_is_no_candidate(self):
        """Second review, item 1: ``key:'value'`` and backtick forms planted in a
        JSON string of an inline script lose to the bundle and alone are no answer."""
        bundle = "var pad=1;" + x_module(NEW_QID, name="SearchTimeline")
        for quote in ("'", "`"):
            evil, page = self._planted(quote)
            res = R.rescan(OLD_QID, "SearchTimeline", {"doc.html": page, "main.js": bundle})
            self.assertEqual(res.token, NEW_QID, (quote, res.reason))
            alone = R.rescan(OLD_QID, "SearchTimeline", {"doc.html": page})
            self.assertIsNone(alone.token, (quote, alone.reason))
            spans = R.literal_spans(page)
            at = page.index(evil)
            self.assertNotIn((at - 1, at + len(evil)), spans)

    def test_inline_scripts_answer_only_when_no_bundle_does(self):
        inline = ('<html><script>' + x_module(OTHER_QID, name="SearchTimeline") +
                  '</script></html>')
        bundle = "var pad=1;" + "x=0;" * 30 + x_module(NEW_QID, name="SearchTimeline")
        self.assertEqual(R.rescan(OLD_QID, "SearchTimeline",
                                  {"doc.html": inline, "main.js": bundle}).token, NEW_QID)
        self.assertEqual(R.rescan(OLD_QID, "SearchTimeline", {"doc.html": inline}).token,
                         OTHER_QID)

    def test_a_relay_store_entity_id_is_no_candidate(self):
        store = ('x={operationName:"UserByScreenName",node:{__typename:"User",'
                 'id:"' + OTHER_QID + '"}}')
        res = R.rescan(OLD_QID, "UserByScreenName", {"relay.js": store})
        self.assertIsNone(res.token, res.reason)

    def test_an_operation_declared_a_write_is_never_healed_onto(self):
        bundle = x_module(NEW_QID, name="CreateTweet").replace('"query"', '"mutation"')
        res = R.rescan(OLD_QID, "CreateTweet", {"m.js": bundle})
        self.assertFalse(res.ok)
        self.assertIn("write", res.reason)

    def test_an_apostrophe_in_a_comment_opens_no_string(self):
        self.assertEqual(R._significant("// it's here\n}"), ["}"])
        self.assertEqual(R._significant("/* don't */ ) ;"), [")", ";"])
        bundle = ('{operationName:"UserByScreenName", // don\'t inline this\n'
                  ' queryId:"' + NEW_QID + '"};')
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": bundle})
        self.assertEqual(res.token, NEW_QID, res.reason)
        self.assertTrue(res.evidence.same_group)

    def test_own_bracket_group_beats_a_nearer_token(self):
        bundle = ('__d("PrevQuery_facebookRelayOperation",[],(function(a,b,c,d,e,f)'
                  '{e.exports="1111111111111111"}),null);\n'
                  '__d("ProfileCometHeaderQuery_facebookRelayOperation",[],(function(a,b,c,d,e,f)'
                  '{"use strict";e.exports="7145285212184222"}),null);')
        anchor = "ProfileCometHeaderQuery_facebookRelayOperation"
        res = R.rescan("6000000000000000", anchor, {"rsrc.js": bundle})
        self.assertEqual(res.token, "7145285212184222", res.reason)
        self.assertTrue(res.evidence.same_group)
        a = bundle.index(anchor)
        prev = bundle.index("1111111111111111")
        own = bundle.index("7145285212184222")
        span = (a, a + len(anchor))
        self.assertFalse(R.same_group(bundle, span, (prev, prev + 16)))
        self.assertTrue(R.same_group(bundle, span, (own, own + 16)))
        self.assertLess(a - (prev + 16), own - (a + len(anchor)))   # the loser WAS nearer

    def test_a_tie_is_no_answer(self):
        res = R.rescan(OLD_QID, "UserByScreenName",
                       {"a.js": x_module(NEW_QID), "b.js": x_module(OTHER_QID)})
        self.assertFalse(res.ok)
        self.assertIn("tie", res.reason)
        same = R.rescan(OLD_QID, "UserByScreenName",
                        {"a.js": x_module(NEW_QID), "b.js": x_module(NEW_QID)})
        self.assertEqual(same.token, NEW_QID)

    def test_the_recorded_token_still_there_is_no_heal(self):
        res = R.rescan(OLD_QID, "UserByScreenName", [("m.js", x_module(OLD_QID))])
        self.assertFalse(res.ok)
        self.assertTrue(res.unchanged)


def x_template():
    return RequestTemplate(
        method="GET", url=f"https://x.example/i/api/graphql/{OLD_QID}/UserByScreenName",
        query={"variables": '{"screen_name":"{q}"}', "features": '{"a":true}'},
        headers={"accept": "application/json"}, params=["q"],
        extract={"items": "data.user.legacy.name"})


class Templates(unittest.TestCase):

    def test_location_must_carry_the_recorded_token(self):
        res = R.rescan("BbCdEfGhIjKlMnOpQrStUv", "UserByScreenName", {"m.js": x_module(NEW_QID)},
                       template=x_template(), location=["path:3"])
        self.assertFalse(res.ok)
        self.assertIn("not the recorded token", res.reason)
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": x_module(NEW_QID)},
                       template=x_template(), location=["path:3"])
        self.assertEqual(res.token, NEW_QID)

    def test_apply_in_the_path_of_a_schema_2_template(self):
        old = x_template()
        new = R.apply_token(old, ["path:3"], OLD_QID, NEW_QID)
        self.assertEqual(new.url, old.url.replace(OLD_QID, NEW_QID))
        self.assertEqual(old.url.count(OLD_QID), 1)                   # the original is untouched
        self.assertEqual(new.problems(), [])
        self.assertEqual(R.default_anchor(old, ["path:3"]), "UserByScreenName")
        with self.assertRaises(ValueError):
            R.apply_token(old, ["path:3"], OLD_QID, "too-short")

    def test_apply_in_a_query_const_keeps_the_template_valid(self):
        tmpl = RequestTemplate(method="GET", url="https://api.example/graphql",
                               query={"doc_id": "6000000000000000", "q": "{q}"},
                               headers={"accept": "application/json"}, params=["q"])
        self.assertEqual(R.token_at(tmpl, ["query:doc_id"]), "6000000000000000")
        new = R.apply_token(tmpl, ["query:doc_id"], "6000000000000000", "7145285212184222")
        self.assertEqual(new.query["doc_id"], "7145285212184222")
        self.assertEqual(new.problems(), [])

    def test_apply_in_a_slotted_form_body(self):
        body = ("fb_api_req_friendly_name=ProfileCometHeaderQuery&variables="
                + urllib.parse.quote('{"id":"granite"}', safe="") + "&doc_id=6000000000000000")
        tmpl = RequestTemplate(
            method="POST", url="https://www.meta.example/api/graphql/", readonly_post=True,
            headers={"content-type": "application/x-www-form-urlencoded"}, body=body,
            params=["q"], slots=[{"param": "q", "at": ["form:variables", "json:/id"]}])
        self.assertEqual(tmpl.problems(), [])
        anchor = R.default_anchor(tmpl, ["form:doc_id"])
        self.assertEqual(anchor, "ProfileCometHeaderQuery_facebookRelayOperation")
        # the bundle match exists for that form, and the doc_id is found
        bundle = ('__d("ProfileCometHeaderQuery_facebookRelayOperation",[],(function(a,b,c,d,e,f)'
                  '{e.exports="7145285212184222"}),null);')
        res = R.rescan("6000000000000000", anchor, {"rsrc.js": bundle}, template=tmpl,
                       location=["form:doc_id"])
        self.assertEqual(res.token, "7145285212184222", res.reason)
        new = R.apply_token(tmpl, ["form:doc_id"], "6000000000000000", "7145285212184222")
        self.assertEqual(new.body, body.replace("6000000000000000", "7145285212184222"))
        self.assertEqual(new.render({"q": "basalt"})[2],
                         new.body.replace("granite", "basalt"))

    def test_apply_a_sha256_hash_in_a_json_body(self):
        old_hash, new_hash = "a" * 64, "0123456789abcdef" * 4
        body = ('{"operationName":"Search","variables":{"q":"{q}"},"extensions":'
                '{"persistedQuery":{"version":1,"sha256Hash":"' + old_hash + '"}}}')
        tmpl = RequestTemplate(method="POST", url="https://api.example/graphql",
                               readonly_post=True, body=body, params=["q"],
                               headers={"content-type": "application/json"})
        loc = ["body", "json:/extensions/persistedQuery/sha256Hash"]
        self.assertEqual(R.default_anchor(tmpl, loc), "Search")
        bundle = 'x={operationName:"Search",sha256Hash:"' + new_hash + '"}'
        res = R.rescan(old_hash, "Search", {"app.js": bundle}, template=tmpl, location=loc)
        self.assertEqual(res.token, new_hash, res.reason)
        new = R.apply_token(tmpl, loc, old_hash, res.token)
        self.assertEqual(new.body, body.replace(old_hash, new_hash))


class Bundles(unittest.TestCase):

    def test_script_urls_resolved_against_the_final_url(self):
        doc = ('<html><head><base href="/app/">'
               '<script src="main.js"></script>'
               '<script defer src="//cdn.example/vendor.js"></script>'
               '<link rel="modulepreload" href="/chunk.js">'
               '<link rel="preload" as="script" href="a.js?x=1&amp;y=2">'
               '<link rel="preload" as="style" href="s.css">'
               '<!-- <script src="old.js"></script> -->'
               '<script data-src="lazy.js"></script>'
               '<script src="data:text/javascript,1"></script>'
               '<script src=main.js></script></head></html>')
        self.assertEqual(R.bundle_urls(doc, "https://www.site.example/home/index.html"), [
            "https://www.site.example/app/main.js",
            "https://cdn.example/vendor.js",
            "https://www.site.example/chunk.js",
            "https://www.site.example/app/a.js?x=1&y=2",
        ])
        self.assertEqual(R.bundle_urls('<script src="m.js"></script>',
                                       "https://b.example/x/y?z=1"),
                         ["https://b.example/x/m.js"])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class LoopGuard(unittest.TestCase):

    def test_a_byte_identical_heal_is_not_drift(self):
        clock = Clock()
        guard = R.HealGuard(clock=clock)
        tmpl = x_template()
        loc = ["path:3"]
        # the bundles still carry the template's token: rescan's best IS it,
        # and the template apply_token builds from it sends the same bytes
        res = R.rescan(R.token_at(tmpl, loc), "UserByScreenName", {"m.js": x_module(OLD_QID)},
                       template=tmpl, location=loc)
        self.assertTrue(res.unchanged)
        healed = R.apply_token(tmpl, loc, OLD_QID, res.best)
        self.assertFalse(guard.heal_succeeded("x.user", tmpl, healed, {"q": "jack"}))
        self.assertEqual(guard.ops, {})
        self.assertEqual(guard.failed("x.user"), "")
        # a rotated token is a real heal
        res = R.rescan(OLD_QID, "UserByScreenName", {"m.js": x_module(NEW_QID)},
                       template=tmpl, location=loc)
        healed = R.apply_token(tmpl, loc, OLD_QID, res.token)
        self.assertTrue(guard.heal_succeeded("x.user", tmpl, healed, {"q": "jack"}))

    def test_failing_again_soon_after_a_heal_goes_stale(self):
        clock = Clock()
        guard = R.HealGuard(clock=clock)
        old = x_template()
        new = R.apply_token(old, ["path:3"], OLD_QID, NEW_QID)
        self.assertTrue(guard.heal_succeeded("x.user", old, new, {"q": "jack"}))
        clock.t += 300
        why = guard.failed("x.user", "drift")
        self.assertIn("failed again 300 s after a heal", why)
        ok, reason = guard.check("x.user")
        self.assertFalse(ok)
        self.assertIn("failed again", reason)
        clock.t += R.STALE_FOR_S + 1
        self.assertEqual(guard.check("x.user"), (True, ""))

    def test_a_failure_long_after_a_heal_is_not_stale(self):
        clock = Clock()
        guard = R.HealGuard(clock=clock)
        old = x_template()
        guard.heal_succeeded("x.user", old, R.apply_token(old, ["path:3"], OLD_QID, NEW_QID),
                             {"q": "jack"})
        clock.t += R.HEAL_RECENT_S + 1
        self.assertEqual(guard.failed("x.user"), "")
        self.assertTrue(guard.check("x.user")[0])

    def test_a_failed_heal_is_stale_with_its_reason_and_persists(self):
        clock = Clock()
        guard = R.HealGuard(clock=clock)
        guard.heal_failed("meta.profile", "no candidate: tie")
        back = R.HealGuard.from_dict(guard.to_dict(), clock=clock)
        clock.t += 60
        ok, why = back.check("meta.profile")
        self.assertFalse(ok)
        self.assertIn("heal failed: no candidate: tie", why)
        self.assertTrue(back.check("other.op")[0])


if __name__ == "__main__":
    unittest.main()
