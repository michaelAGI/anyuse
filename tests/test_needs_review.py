"""needs_review: the attribute verifier's last cascade stage. Fakes only (pages built inline, a scripted model), no network, no real model.

The rule (``attribute_verify.dispute_of``): a product answer whose identity (name, URL) is verified becomes
``needs_review`` - not accepted, not silently withheld - when every other open critical attribute is a TWO-SOURCE
DISAGREEMENT: an attribute with a structured and a visible source (availability, price, currency), left UNCERTAIN by the
rules (never a conflict, and no box line the rules class against the answer), the structured source stating exactly the
answer, and the cascade's own model call reading the visible buy box as a definite other value, citing a line that
states it (for availability: a statement no rule reads). What must hold:

1. www.menzels-lokschuppen.de's class (structured BackOrder, the box says only "Bestellbar", the model reads in stock)
   is ``needs_review`` with BOTH sources' evidence; the decision itself stays ``withheld``; no extra model call;
2. the run record is never ``ok``: status ``needs_review``, ``output`` None, ``accepted`` False, the answer in
   ``unverified_output`` and in the ``needs_review`` field (the caller-facing view ``review_view``: the MCP field shape);
3. a RULES conflict stays withheld (teufel.de's LimitedAvailability beside "Auf Lager", the version-13 InStock beside
   "Ware befindet sich in Zulauf"), also when an unknown line beside the known class made the rules uncertain;
4. an identity failure, an unreadable page / no structured reading, a structured source that says otherwise, a model
   that abstains, fails, reads no class or cites no line, a cited line that states no availability the rules cannot
   read (an add-to-cart button, a delivery time), an answer that only quotes the line, and every LIST answer stay
   withheld;
5. a price disagreement of the same shape is ``needs_review``; a structured price against the visible answered price is
   a rules conflict and stays withheld;
6. every caller treats it as NOT accepted: the ladder's checker (``repair_check.judge``) disagrees, the verdict store
   books ``disputed``, never ``correct``;
7. the CLI (``anyuse watch``): a needs_review replay is its own changelog entry - never a content change, never a
   baseline, never drift - rendered with both sources, and the agent hook line says NEEDS REVIEW;
8. off by default: the run seam is not called with the setting off (the record is unchanged).
"""
from __future__ import annotations

import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import attribute_verify as AV  # noqa: E402

NAME = "DPM DPM90107 Street/Dock Level Freight Door"
URL = "https://shop.example/dpm-dpm90107-street-dock-level-freight-door"


def jsonld(availability="BackOrder", price="12.20", name=NAME):
    return ('<script type="application/ld+json">' + json.dumps(
        {"@context": "https://schema.org", "@type": "Product", "name": name,
         "offers": {"@type": "Offer", "price": price, "priceCurrency": "EUR",
                    "availability": f"https://schema.org/{availability}"}}) + "</script>") if availability else ""


def page(box, availability="BackOrder", price="12.20", h1=NAME, extra_lines=()):
    dom = (f"<html><head><title>{h1} | Shop</title>{jsonld(availability, price)}"
           f"<link rel='canonical' href='{URL}'></head><body><h1>{h1}</h1></body></html>")
    text = "\n".join(["Home", "Modellbahn", h1] + list(box) + list(extra_lines) + ["Beschreibung", "Ein Tor."])
    return AV.Page(dom, text, URL)


MENZELS_BOX = ["12,20 €", "In den Warenkorb", "Verfügbarkeit Onlineshop", "Bestellbar", "Verfügbarkeit Ladengeschäft",
               "Bestellbar"]


def answer(av="BackOrder", price=12.2, name=NAME):
    return {"name": name, "price": price, "currency": "EUR", "availability": av, "url": URL}


class Model:
    """A scripted second stage: the same reply to every question (a choice and a cited line), counted."""

    def __init__(self, choice, line=1):
        self.choice, self.line, self.calls = choice, line, []

    def __call__(self, prompt, schema):
        self.calls.append(prompt)
        line = self.line
        if isinstance(line, str):                     # cite the numbered evidence line that holds this text
            line = next(int(ln.split(".", 1)[0]) for ln in prompt.split("\n")
                        if ln.split(".", 1)[0].isdigit() and line in ln)
        return {"choice": self.choice, "line": line, "calls": 1, "usd": 0.0003}


def verify(pg, ans, model):
    return AV.check("product_lookup", ans, pg, AV.Resolver(model))


def att(v, name):
    return next(d for d in v.decisions if d.attr == name)


class TheMenzelsClass(unittest.TestCase):
    def test_a_two_source_disagreement_is_needs_review_with_both_sources(self):
        m = Model("in_stock", line=2)
        v = verify(page(MENZELS_BOX), answer(), m)
        self.assertEqual(v.verdict, AV.NEEDS_REVIEW, v.why())
        d = att(v, "availability")
        self.assertEqual((d.rules, d.status), (AV.UNCERTAIN, AV.WITHHELD))     # never "verified"
        self.assertEqual(len(m.calls), 1)                                      # the cascade's own call, no extra
        self.assertEqual(v.model_calls, 1)
        rv = v.review(answer())
        self.assertEqual(rv["status"], AV.NEEDS_REVIEW)
        self.assertEqual(rv["answer"], answer())
        self.assertEqual(sorted(rv["verified"]), ["currency", "name", "price", "url"])
        (dis,) = rv["disputed"]
        st, vis = dis["sources"]
        self.assertEqual((st["source"], st["reads"], st["agrees_with_answer"], st["read_by"]),
                         ("structured", "backorder", True, "rules"))
        self.assertIn("schema.org/BackOrder", st["evidence"][0])
        self.assertEqual((vis["source"], vis["reads"], vis["agrees_with_answer"], vis["read_by"], vis["rules"]),
                         ("visible", "in_stock", False, "model", "unknown"))
        self.assertEqual(vis["evidence"], [d.span[1]])
        self.assertTrue(any(ln.get("cited") for ln in dis["lines"]))
        self.assertTrue(all("rules" in ln for ln in dis["lines"]))

    def test_the_run_record_is_never_ok_and_carries_the_review(self):
        class P:
            async def dom(self):
                return page(MENZELS_BOX)._dom

            async def text(self):
                return page(MENZELS_BOX).text

            async def url(self):
                return URL
        out = {"status": "ok", "output": answer(), "checks": {}, "model_calls": 0, "accepted": True}
        got = asyncio.run(AV.verify_run(P(), {"workflow": "product_lookup"}, NAME, out, AV.Resolver(Model("in_stock", 2))))
        self.assertEqual(got["status"], AV.NEEDS_REVIEW)
        self.assertIsNone(got["output"])
        self.assertFalse(got["accepted"])
        self.assertEqual(got["unverified_output"], answer())
        self.assertEqual(got["checks"]["attribute_verify"]["verdict"], AV.NEEDS_REVIEW)
        self.assertEqual(got["model_calls"], 1)
        self.assertIn("needs review (availability)", got["why"])
        view = AV.review_view(got)                                             # the MCP / JSON field
        self.assertEqual((view["ok"], view["status"], view["answer"]), (False, AV.NEEDS_REVIEW, answer()))
        self.assertEqual([s["source"] for s in view["disputed"][0]["sources"]], ["structured", "visible"])
        self.assertIsNone(AV.review_view(dict(got, status="incomplete")))
        lines = AV.review_lines(got)
        self.assertTrue(lines[0].startswith("NEEDS REVIEW - not accepted"))
        self.assertTrue(any("structured: 'backorder' (agrees" in ln for ln in lines), lines)
        self.assertTrue(any("visible: 'in_stock' (disagrees" in ln and "Bestellbar" in ln for ln in lines), lines)


class KnownWrongStaysWithheld(unittest.TestCase):
    def test_teufels_limited_beside_auf_lager_is_a_rules_conflict(self):
        p = page(["249,00 €", "In den Warenkorb", "Auf Lager"], availability="LimitedAvailability", price="249.00")
        v = verify(p, answer("LimitedAvailability", 249.0), Model("in_stock", 1))
        d = att(v, "availability")
        self.assertEqual((d.rules, v.verdict, d.dispute), (AV.CONFLICT, AV.WITHHOLD, None))

    def test_the_v13_in_stock_beside_goods_inbound_is_a_rules_conflict(self):
        p = page(["2.029,00 €", "IN DEN WARENKORB", "Lieferzeit ca.: 5-10 Werktage *", "Ware befindet sich in Zulauf"],
                 availability="InStock", price="2029.00")
        v = verify(p, answer("InStock", 2029.0), Model("backorder", 2))
        self.assertEqual((att(v, "availability").rules, v.verdict), (AV.CONFLICT, AV.WITHHOLD))

    def test_a_known_class_beside_an_unknown_line_stays_a_known_wrong(self):
        # the unknown line makes the RULES uncertain (unknown is read first), but "Auf Lager" is a class the rules
        # read against the answer: still the known wrong pattern, never a review
        p = page(["249,00 €", "In den Warenkorb", "Auf Lager", "Bestellbar"], availability="LimitedAvailability",
                 price="249.00")
        v = verify(p, answer("LimitedAvailability", 249.0), Model("in_stock", 1))
        self.assertEqual((att(v, "availability").rules, v.verdict), (AV.UNCERTAIN, AV.WITHHOLD))

    def test_a_delivery_time_against_out_of_stock_stays_withheld(self):
        p = page(["12,20 €", "Lieferzeit 2-3 Werktage", "Bestellbar"], availability="OutOfStock")
        v = verify(p, answer("OutOfStock"), Model("in_stock", 2))
        self.assertEqual(v.verdict, AV.WITHHOLD)


class OtherDoubtsStayWithheld(unittest.TestCase):
    def test_an_identity_failure(self):
        p = page(MENZELS_BOX, h1="Faller 120166 Lokschuppen mit Werkstatt")
        v = verify(p, answer(), Model("in_stock", 2))
        self.assertNotEqual(att(v, "name").status, AV.VERIFIED)
        self.assertEqual(v.verdict, AV.WITHHOLD)

    def test_no_structured_reading_or_another_structured_class(self):
        self.assertEqual(verify(page(MENZELS_BOX, availability=""), answer(), Model("in_stock", 2)).verdict, AV.WITHHOLD)
        self.assertEqual(verify(page(MENZELS_BOX, availability="PreOrder"), answer(), Model("in_stock", 2)).verdict,
                         AV.WITHHOLD)

    def test_an_unreadable_page(self):
        v = verify(AV.Page("", "", URL), answer(), Model("in_stock", 1))
        self.assertEqual(v.verdict, AV.WITHHOLD)

    def test_a_model_without_a_definite_reading(self):
        for reply in (("unclear", 0), ("delivery_time", 2), ("not_stated", 0), ("in_stock", 0), ("in_stock", 99)):
            v = verify(page(MENZELS_BOX), answer(), Model(*reply))
            self.assertEqual(v.verdict, AV.WITHHOLD, reply)
        boom = AV.Resolver(lambda p, s: {"error": "provider_unreachable", "calls": 1})
        self.assertEqual(AV.check("product_lookup", answer(), page(MENZELS_BOX), boom).verdict, AV.WITHHOLD)
        self.assertEqual(AV.check("product_lookup", answer(), page(MENZELS_BOX), AV.Resolver(None)).verdict,
                         AV.WITHHOLD)
        self.assertEqual(AV.check("product_lookup", answer(), page(MENZELS_BOX), None).verdict, AV.WITHHOLD)

    def test_a_cited_line_that_states_no_availability_is_no_second_source(self):
        # the box states nothing (an add-to-cart button): the model inferred in stock - one source, not two
        p = page(["12,20 €", "IN DEN WARENKORB"], availability="OutOfStock")
        m = Model("in_stock", "IN DEN WARENKORB")
        v = verify(p, answer("OutOfStock"), m)
        self.assertEqual((len(m.calls), att(v, "availability").rules, v.verdict), (1, AV.UNCERTAIN, AV.WITHHOLD))
        # a delivery time the rules DO read: compatible with goods inbound, no disagreement
        p = page(["12,20 €", "In den Warenkorb", "Lieferzeit 3-5 Werktage"])
        v = verify(p, answer(), Model("in_stock", "Lieferzeit"))
        self.assertEqual((att(v, "availability").rules, v.verdict), (AV.UNCERTAIN, AV.WITHHOLD))

    def test_an_answer_that_only_quotes_the_line(self):
        v = verify(page(MENZELS_BOX), answer("Verfügbarkeit Onlineshop: Bestellbar"), Model("in_stock", 2))
        self.assertEqual(v.verdict, AV.WITHHOLD)

    def test_an_agreeing_model_still_accepts(self):
        self.assertEqual(verify(page(MENZELS_BOX), answer(), Model("backorder", 2)).verdict, AV.ACCEPT)

    def test_a_list_answer_is_never_needs_review(self):
        dom = ("<html><body><h1>Suche</h1><ul><li><a href='https://shop.example/a'>Alpha Tor</a><p>Beta Tor</p></li>"
               "<li><a href='https://shop.example/b'>Gamma Tor</a></li></ul></body></html>")
        p = AV.Page(dom, "Suche\nAlpha Tor\nBeta Tor\nGamma Tor", "https://shop.example/suche?q=tor")
        ans = {"results": [{"title": "Beta Tor", "url": "https://shop.example/a"},
                           {"title": "Gamma Tor", "url": "https://shop.example/b"}]}
        v = AV.check("site_search", ans, p, AV.Resolver(lambda q, s: {"choice": s["properties"]["choice"]["enum"][0],
                                                                      "line": 1, "calls": 1}), scope=["shop.example"])
        self.assertNotEqual(v.verdict, AV.ACCEPT)
        self.assertEqual(v.verdict, AV.WITHHOLD)


class Price(unittest.TestCase):
    def test_a_price_disagreement_of_the_same_shape(self):
        # the structured price is the answer's; the buy box shows another price; the answer's price is on the page,
        # far below the box (an accessory set): the rules are uncertain, the model reads the box price
        far = ["Beschreibung"] + [f"Absatz {i}" for i in range(40)] + ["Zubehör-Set 12,20 €"]
        p = page(["14,90 €", "In den Warenkorb", "Auf Lager"], availability="InStock", price="12.20", extra_lines=far)
        v0 = AV.verify_product(answer("InStock"), p)
        self.assertEqual(att(v0, "price").status, AV.UNCERTAIN, att(v0, "price").why)
        v = verify(p, answer("InStock"), Model("14.90", 1))
        self.assertEqual(v.verdict, AV.NEEDS_REVIEW, v.why())
        dis = v.review()["disputed"][0]
        self.assertEqual((dis["attribute"], dis["sources"][0]["reads"], dis["sources"][1]["reads"]),
                         ("price", 12.2, "14.90"))
        self.assertIn("JSON-LD offers.price: 12.20", dis["sources"][0]["evidence"][0])

    def test_a_structured_price_against_the_visible_answer_is_a_conflict(self):
        p = page(["12,20 €", "In den Warenkorb", "Auf Lager"], availability="InStock", price="14.90")
        v = verify(p, answer("InStock"), Model("14.90", 1))
        self.assertEqual((att(v, "price").rules, v.verdict), (AV.CONFLICT, AV.WITHHOLD))


class Callers(unittest.TestCase):
    def test_the_ladders_checker_never_agrees(self):
        from anyuse.execution import repair_check as RCK
        tool = {"workflow": "product_lookup", "site": "shop.example", "version": 10}
        out = {"status": "ok", "output": answer()}
        ev = {"links": [{"url": URL, "text": NAME}]}
        p = page(MENZELS_BOX)
        items = {URL: {"url": URL, "title": f"{NAME} | Shop", "h1": [NAME], "text": p.text, "dom": p._dom}}
        j = RCK.judge(tool, "x", out, ev, items, attribute_verify=True, resolver=AV.Resolver(Model("in_stock", 2)))
        self.assertEqual(j["attribute_verify"]["verdict"], AV.NEEDS_REVIEW)
        self.assertEqual(j["verdict"], "disagrees")

    def test_the_store_books_disputed(self):
        rows = []

        class Store:
            def add(self, **kw):
                rows.append(kw)
        v = verify(page(MENZELS_BOX), answer(), Model("in_stock", 2))
        AV.to_store(Store(), {"site": "shop.example", "workflow": "product_lookup"}, NAME,
                    {"status": AV.NEEDS_REVIEW, "unverified_output": answer()}, v)
        self.assertEqual(rows[0]["verdict"], "disputed")

    def test_off_the_seam_is_not_called(self):
        # the verifier is on by default - "off" still skips the seam entirely
        from anyuse.execution import demo_tool as T
        old = os.environ.get(AV.SETTING)
        os.environ[AV.SETTING] = "off"
        try:
            out = {"status": "ok", "output": answer(), "checks": {}}
            got = asyncio.run(T._attribute_verify(None, {"workflow": "product_lookup"}, NAME, out))
            self.assertIs(got, out)
        finally:
            if old is not None:
                os.environ[AV.SETTING] = old


class Watch(unittest.TestCase):
    TOOL = {"version": 6, "workflow": "product_lookup", "site": "shop.example", "start_url": "https://shop.example/",
            "search": {"mode": "url", "template": "https://shop.example/search?q={q}"},
            "fields": {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"},
            "region": {"regex": "^x$"}, "hosts": ["shop.example"], "model_calls": 0}

    def rec(self, review=False):
        base = {"status": "ok", "output": answer(), "why": "identity and fields checked", "model_calls": 0,
                "requests": 5, "bytes": 1000, "pages": [{"url": URL, "title": NAME, "h1": [NAME]}], "checks": {}}
        if not review:
            return base
        v = verify(page(MENZELS_BOX), answer(), Model("in_stock", 2))
        return dict(AV.withhold(base, v), requests=5, bytes=1000)

    def test_a_needs_review_replay_is_its_own_entry(self):
        from anyuse.execution import watch as WA
        d = tempfile.mkdtemp(prefix="anyuse-nr-watch-")
        try:
            st = WA.WatchStore(d)
            p = os.path.join(d, "t.tool.json")
            with open(p, "w") as fh:
                json.dump(self.TOOL, fh)
            item = st.add(p, NAME, "1h")
            script = [self.rec(), self.rec(review=True)]
            t0 = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.timezone.utc)
            WA.run_watch(st, lambda i, t, c: script.pop(0), now=t0)
            good = st.items()[item["id"]]["last_good"]
            r = WA.run_watch(st, lambda i, t, c: script.pop(0), now=t0 + dt.timedelta(hours=2))
            e = r["entries"][0]
            self.assertEqual((e["kind"], e["action"], e["changes"]), ("needs_review", "review", []))
            self.assertEqual(r["items"][0]["needs_review"]["status"], AV.NEEDS_REVIEW)
            it = st.items()[item["id"]]
            self.assertEqual((it["last_good"], it["last_status"], it.get("drift_since")), (good, AV.NEEDS_REVIEW, None))
            md = WA.render_entry(e)
            self.assertIn("NEEDS REVIEW - not accepted", md)
            self.assertIn("structured: 'backorder' (agrees", md)
            self.assertIn("Bestellbar", md)
            self.assertNotIn("DRIFT", md)
            hook = WA.hook_text(st, since="2026-10-12T00:00:00Z")
            self.assertIn("1 needing review", hook)
            self.assertIn("NEEDS REVIEW, not accepted (availability: structured 'backorder' vs visible 'in_stock')",
                          hook)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
