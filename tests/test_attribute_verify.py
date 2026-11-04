"""VERIFIER INDEPENDENCE (execution/attribute_verify.py): the shared attribute verifier.
Offline, fakes only: the module's independence from the extractor (no shared import), unknown is uncertain, the second
reading from another source, the conflict rule, the model stage (closed schema, abstention, failure withholds, the cap),
the list title check on the result's own card, the desktop text effect, and the run seam."""
from __future__ import annotations

import ast
import asyncio
import os
import unittest

from anyuse.execution import attribute_verify as AV

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def product_page(box_lines, jsonld_avail="InStock", price="219.99", name="Acme Kettle 9000", h1=None, extra=""):
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"%s",'
          '"offers":{"@type":"Offer","price":"%s","priceCurrency":"EUR","availability":"https://schema.org/%s"}}'
          '</script>' % (name, price, jsonld_avail)) if jsonld_avail else ""
    dom = "<html><head><title>%s | Shop</title>%s</head><body><h1>%s</h1>%s</body></html>" % (name, ld, h1 or name, extra)
    text = "\n".join(["Home", "Kitchen", h1 or name] + list(box_lines) + ["Description", "A fine kettle."])
    return AV.Page(dom, text, "https://shop.example/acme-kettle-9000")


def answer(av="InStock", price=219.99, name="Acme Kettle 9000", cur="EUR"):
    return {"name": name, "price": price, "currency": cur, "availability": av,
            "url": "https://shop.example/acme-kettle-9000"}


def att(v, name):
    return next(d for d in v.decisions if d.attr == name)


class Independence(unittest.TestCase):
    def test_imports_nothing_of_the_extractor(self):
        with open(os.path.join(HERE, "execution", "attribute_verify.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {f"{node.module}.{a.name}" for a in node.names} | {str(node.module)}
        for banned in ("demo_compile", "web_arch", "demo_oracle", "repair_check", "demo_tool", "html_extract",
                       "web_arch_run", "dom_page"):
            self.assertFalse([n for n in names if banned in n], banned)

    def test_on_by_default_and_off_still_turns_it_off(self):
        # the verifier is ON by default (about US$0.07 per 1,000 runs); off / 0 turn it off
        old = os.environ.pop(AV.SETTING, None)
        try:
            self.assertTrue(AV.enabled())
            for off in ("off", "0"):
                os.environ[AV.SETTING] = off
                self.assertFalse(AV.enabled())
            self.assertFalse(AV.enabled(False))                 # an explicit False stays off
        finally:
            os.environ.pop(AV.SETTING, None)
            if old is not None:
                os.environ[AV.SETTING] = old


class Availability(unittest.TestCase):
    def test_visible_class_confirms_a_structured_answer(self):
        v = AV.verify_product(answer(), product_page(["219,99 €", "In den Warenkorb", "Auf Lager, in 2-4 Werktagen"]))
        self.assertEqual(att(v, "availability").status, AV.VERIFIED)
        self.assertEqual(v.verdict, AV.ACCEPT)

    def test_unknown_phrase_is_uncertain_never_in_stock(self):
        # feuerdepot's class, with a phrase in NO vocabulary of the module (a strong cue, no phrase)
        p = product_page(["219,99 €", "In den Warenkorb", "Lagerbestand wird aufgefuellt, Verfuegbarkeit folgt"])
        d = att(AV.verify_product(answer(), p), "availability")
        self.assertEqual(d.status, AV.UNCERTAIN)
        self.assertIn("unknown is uncertain", d.why)

    def test_inbound_goods_conflict_with_in_stock(self):
        p = product_page(["2.029,00 €", "IN DEN WARENKORB", "Lieferzeit ca.: 5-10 Werktage *", "Ware befindet sich in Zulauf"],
                         price="2029.00")
        d = att(AV.verify_product(answer(price=2029.0), p), "availability")
        self.assertEqual(d.status, AV.CONFLICT)

    def test_a_few_left_against_plain_in_stock_is_a_conflict(self):
        # teufel.de / musik-produktiv: LimitedAvailability next to "Auf Lager" / "Online sofort lieferbar"
        for line in ("Auf Lager, in 2 – 4 Werktagen bei dir", "Online sofort lieferbar"):
            p = product_page(["219,99 €", "In den Warenkorb", line], jsonld_avail="LimitedAvailability")
            self.assertEqual(att(AV.verify_product(answer("LimitedAvailability"), p), "availability").status, AV.CONFLICT)

    def test_in_stock_with_a_few_left_is_verified(self):
        p = product_page(["219,99 €", "In den Warenkorb", "Nur noch 3 auf Lager!"])
        self.assertEqual(att(AV.verify_product(answer(), p), "availability").status, AV.VERIFIED)

    def test_delivery_time_against_out_of_stock(self):
        # kamdi24: OutOfStock next to "Lieferzeit: 1 bis 3 Werktage"
        p = product_page(["219,99 €", "In den Warenkorb", "Lieferzeit: 1 bis 3 Werktage"], jsonld_avail="OutOfStock")
        self.assertEqual(att(AV.verify_product(answer("OutOfStock"), p), "availability").status, AV.CONFLICT)

    def test_negated_phrase_is_read_before_the_positive(self):
        self.assertEqual(AV.classify_line("Derzeit nicht auf Lager")[0], "out_of_stock")
        self.assertEqual(AV.classify_line("Nicht lieferbar")[0], "out_of_stock")
        self.assertEqual(AV.classify_line("Benachrichtige mich, sobald es wieder auf Lager ist")[0], "")

    def test_no_second_reading_is_uncertain(self):
        p = product_page(["219,99 €", "In den Warenkorb"])
        self.assertEqual(att(AV.verify_product(answer(), p), "availability").status, AV.UNCERTAIN)

    def test_not_stated_answer(self):
        p = product_page(["219,99 €", "In den Warenkorb"], jsonld_avail="")
        self.assertEqual(att(AV.verify_product(answer("not stated"), p), "availability").status, AV.VERIFIED)
        p2 = product_page(["219,99 €", "In den Warenkorb", "Verfügbarkeit: Vorrätig"], jsonld_avail="")
        self.assertEqual(att(AV.verify_product(answer("not stated"), p2), "availability").status, AV.CONFLICT)

    def test_option_notice_and_shipping_method_lines_are_no_statement(self):
        self.assertEqual(AV.classify_line("Lieferung per Spedition")[0], "")
        self.assertEqual(AV.classify_line("Rückversand")[0], "")
        self.assertEqual(AV.classify_line("Lieferung ca. Di, 13.10. - Do, 15.10.")[0], AV.DELIVERY)
        self.assertEqual(AV.classify_line("Versand derzeit nicht möglich")[0], "unknown")


class PriceNameUrl(unittest.TestCase):
    def test_price_shown_and_structured(self):
        v = AV.verify_product(answer(), product_page(["219,99 €", "In den Warenkorb", "Auf Lager"]))
        self.assertEqual(att(v, "price").status, AV.VERIFIED)
        self.assertEqual(att(v, "currency").status, AV.VERIFIED)

    def test_structured_price_differs_from_the_visible_one(self):
        p = product_page(["249,00 €", "199,00 €", "In den Warenkorb", "Auf Lager"], price="249.00")
        self.assertEqual(att(AV.verify_product(answer(price=199.0), p), "price").status, AV.CONFLICT)

    def test_price_not_on_the_page(self):
        p = product_page(["229,99 €", "In den Warenkorb", "Auf Lager"], price="229.99")
        self.assertEqual(att(AV.verify_product(answer(), p), "price").status, AV.CONFLICT)

    def test_currency_conflict(self):
        p = product_page(["219,99 €", "In den Warenkorb", "Auf Lager"])
        self.assertEqual(att(AV.verify_product(answer(cur="CHF"), p), "currency").status, AV.CONFLICT)

    def test_name_against_the_heading(self):
        p = product_page(["219,99 €", "In den Warenkorb", "Auf Lager"], h1="Other Brand Toaster 2 Slice",
                         name="Other Brand Toaster 2 Slice")
        self.assertEqual(att(AV.verify_product(answer(), p), "name").status, AV.CONFLICT)

    def test_url_identity(self):
        p = product_page(["219,99 €", "In den Warenkorb", "Auf Lager"])
        a = dict(answer(), url="https://www.shop.example/acme-kettle-9000/?utm_source=x")
        self.assertEqual(att(AV.verify_product(a, p), "url").status, AV.VERIFIED)
        a2 = dict(answer(), url="https://shop.example/another-item")
        self.assertEqual(att(AV.verify_product(a2, p), "url").status, AV.UNCERTAIN)


LIST_DOM = """<html><body><main>
<div class="r"><a href="/a1"><img alt="nextmove nextnews 316"></a><h3><a href="/a1">VW ID.Buzz als Robotaxi</a></h3>
<p>snippet one</p></div>
<div class="r"><h3><a href="/a2">Winter-Performance: Batterie und Heizung</a></h3><a href="/a2">Mehr lesen</a></div>
<div class="r"><h3><a href="/a3">Drag Race Model 3</a></h3><a href="/a3">Mehr lesen</a></div>
</main></body></html>"""


class Lists(unittest.TestCase):
    def page(self):
        return AV.Page(LIST_DOM, "VW ID.Buzz als Robotaxi\nsnippet one\nWinter-Performance: Batterie und Heizung\n"
                                 "Mehr lesen\nDrag Race Model 3\nMehr lesen", "https://news.example/?s=x")

    def test_titles_named_by_their_own_card(self):
        res = [{"title": "VW ID.Buzz als Robotaxi", "url": "https://news.example/a1"},
               {"title": "Winter-Performance: Batterie und Heizung", "url": "https://news.example/a2"}]
        self.assertEqual(AV.verify_list({"results": res}, self.page()).verdict, AV.ACCEPT)

    def test_an_image_alternative_is_not_the_title(self):
        # nextmove: the title role reads an image's alternative text
        res = [{"title": "nextmove nextnews 316", "url": "https://news.example/a1"}]
        d = AV.verify_list({"results": res}, self.page()).decisions[0]
        self.assertEqual(d.status, AV.UNCERTAIN)

    def test_a_neighbours_title_is_a_conflict(self):
        res = [{"title": "Drag Race Model 3", "url": "https://news.example/a2"},
               {"title": "Winter-Performance: Batterie und Heizung", "url": "https://news.example/a3"}]
        self.assertEqual(AV.verify_list({"results": res}, self.page()).decisions[0].status, AV.CONFLICT)

    def test_a_label_link_text_is_not_a_title(self):
        res = [{"title": "Mehr lesen", "url": "https://news.example/a2"}]
        self.assertNotEqual(AV.verify_list({"results": res}, self.page()).decisions[0].status, AV.VERIFIED)


class ModelStage(unittest.TestCase):
    def open_verdict(self):
        p = product_page(["219,99 €", "In den Warenkorb", "Lagerbestand wird aufgefuellt"])
        return AV.verify_product(answer(), p)

    def test_agreeing_model_verifies_with_a_cited_line(self):
        asked = []

        def model(prompt, schema):
            asked.append((prompt, schema))
            return {"choice": "in_stock", "line": 1, "calls": 1, "usd": 0.0003}
        v = AV.Resolver(model)(self.open_verdict(), answer())
        self.assertEqual(v.verdict, AV.ACCEPT)
        self.assertEqual(v.model_calls, 1)
        self.assertIn("unclear", asked[0][1]["properties"]["choice"]["enum"])
        self.assertIn("Lagerbestand", asked[0][0])

    def test_disagreeing_abstaining_failing_or_absent_model_withholds(self):
        # a DISAGREEING model on this page (the structured data says the answered
        # in stock, the model reads the unknown box line as backorder) is a two-source disagreement - needs_review,
        # still never accepted (test_needs_review.py has the rule); the other replies stay plain withholds
        v = AV.Resolver(lambda p, s: {"choice": "backorder", "line": 1, "calls": 1})(self.open_verdict(), answer())
        self.assertEqual(v.verdict, AV.NEEDS_REVIEW)
        self.assertNotEqual(v.verdict, AV.ACCEPT)
        for reply in ({"choice": "unclear", "line": 0},
                      {"error": "provider_unreachable"}, {"choice": "in_stock", "line": 0},
                      {"choice": "free text", "line": 1}):
            v = AV.Resolver(lambda p, s, r=reply: dict(r, calls=1))(self.open_verdict(), answer())
            self.assertEqual(v.verdict, AV.WITHHOLD, reply)
        v = AV.Resolver(None)(self.open_verdict(), answer())
        self.assertEqual(v.verdict, AV.WITHHOLD)
        self.assertEqual(v.model_calls, 0)

    def test_raising_model_withholds(self):
        def boom(p, s):
            raise RuntimeError("down")
        self.assertEqual(AV.Resolver(boom)(self.open_verdict(), answer()).verdict, AV.WITHHOLD)

    def test_no_call_when_the_pages_structure_verifies(self):
        # the page's own markup binds its visible words to the class: structure decides, no model is asked
        calls = []
        marked = '<p><link itemprop="availability" href="https://schema.org/InStock">Auf Lager</p>'
        v = AV.verify_product(answer(), product_page(["219,99 €", "In den Warenkorb", "Auf Lager"], extra=marked))
        AV.Resolver(lambda p, s: calls.append(1) or {"choice": "in_stock", "line": 1})(v, answer())
        self.assertEqual(calls, [])
        self.assertEqual(v.verdict, AV.ACCEPT)

    def test_the_phrases_verdict_is_read_by_the_model_first_and_stands_without_an_answer(self):
        def page():
            return AV.verify_product(answer(), product_page(["219,99 €", "In den Warenkorb", "Auf Lager"]))
        self.assertTrue(next(d for d in page().decisions if d.attr == "availability").listed)
        calls = []
        v = AV.Resolver(lambda p, s: calls.append(1) or {"choice": "in_stock", "line": 1, "calls": 1})(page(), answer())
        self.assertEqual((calls, v.verdict), ([1], AV.ACCEPT))
        v = AV.Resolver(lambda p, s: {"choice": "backorder", "line": 1, "calls": 1})(page(), answer())
        self.assertEqual(v.verdict, AV.WITHHOLD)
        for reply in ({"error": "provider_unreachable"}, {"choice": "unclear", "line": 0}, {"choice": "in_stock"}):
            v = AV.Resolver(lambda p, s, r=reply: dict(r, calls=1))(page(), answer())
            self.assertEqual(v.verdict, AV.ACCEPT, reply)
        self.assertEqual(AV.Resolver(None)(page(), answer()).verdict, AV.ACCEPT)

    def test_the_cap(self):
        p = product_page(["229,99 €", "In den Warenkorb", "Lagerbestand wird aufgefuellt"], price="229.99", h1="Other",
                         name="Other")
        v = AV.verify_product(answer(), p)
        r = AV.Resolver(lambda p, s: {"choice": "unclear", "line": 0, "calls": 1}, max_calls=1)(v, answer())
        self.assertEqual(r.model_calls, 1)
        self.assertTrue(any("cap" in str(d.model) for d in r.decisions))

    def test_only_cerebras(self):
        class Cfg:
            provider, base_url, model = "openrouter", "https://openrouter.ai/api/v1", "x"

        class Ad:
            config = Cfg()
        with self.assertRaises(ValueError):
            AV.cerebras_model(adapter=Ad())


class Desktop(unittest.TestCase):
    def test_ax_alone_decides_as_before(self):
        self.assertEqual(AV.verify_text_effect("North gate", "North gate").status, AV.VERIFIED)
        self.assertEqual(AV.verify_text_effect("North gate", "North gat").status, AV.CONFLICT)

    def test_an_independent_file_reading_that_differs_is_a_conflict(self):
        d = AV.verify_text_effect("Collection point: North gate", "Collection point: North gate",
                                  [("file", "Collection point: boat shed\n")])
        self.assertEqual(d.status, AV.CONFLICT)
        ok = AV.verify_text_effect("North gate", "North gate", [("file", "Point: North gate\n")])
        self.assertEqual(ok.status, AV.VERIFIED)

    def test_unicode_form_only_is_uncertain(self):
        d = AV.verify_text_effect("Rückfragen", "Rückfragen", [("file", "Rückfragen")])
        self.assertEqual(d.status, AV.UNCERTAIN)

    def test_typed_values(self):
        self.assertTrue(AV.same_value("date", "2026-10-03", "3. Oktober 2026"))
        self.assertTrue(AV.same_value("count", "1.204 Treffer", "1204"))
        self.assertIsNone(AV.same_value("date", "soon", "2026-10-03"))
        d = AV.verify_value("count", "12", [AV.Reading("count", "13", "file", "13")])
        self.assertEqual(d.status, AV.CONFLICT)


class RunSeam(unittest.TestCase):
    def test_withheld_answer_is_evidence_only(self):
        class P:
            async def dom(self):
                return product_page(["219,99 €", "In den Warenkorb", "Lagerbestand wird aufgefuellt"])._dom

            async def text(self):
                return "\n".join(["Acme Kettle 9000", "219,99 €", "In den Warenkorb", "Lagerbestand wird aufgefuellt"])

            async def url(self):
                return "https://shop.example/acme-kettle-9000"
        out = {"status": "ok", "output": answer(), "checks": {}, "model_calls": 0}
        got = asyncio.run(AV.verify_run(P(), {"workflow": "product_lookup"}, "Acme Kettle", out, AV.Resolver(None)))
        self.assertEqual(got["status"], "incomplete")
        self.assertIsNone(got["output"])
        self.assertEqual(got["unverified_output"], answer())
        self.assertEqual(got["checks"]["attribute_verify"]["verdict"], AV.WITHHOLD)

    def test_store_row(self):
        rows = []

        class Store:
            def add(self, **kw):
                rows.append(kw)
        v = AV.verify_product(answer(), product_page(["219,99 €", "In den Warenkorb", "Auf Lager"]))
        AV.to_store(Store(), {"site": "shop.example", "workflow": "product_lookup"}, "Acme", {"status": "ok",
                                                                                              "output": answer()}, v)
        self.assertEqual(rows[0]["verdict"], "correct")
        self.assertEqual(rows[0]["ref"], AV.FEATURE)



CRATES_DOM = """<html><body><main><ul>
<li><div><a href="/crates/rand">rand</a><span>v0.10.3</span></div><p>Random number generators</p></li>
<li><div><a href="/crates/bevy_rand">bevy_rand</a><span>v0.16.0</span></div><p>A plugin for random numbers</p></li>
</ul></main></body></html>"""


class TitlesThatBeginWithTheLinkText(unittest.TestCase):
    """crates.io: a card names its result with a one-word link ("rand"); the answered title adds the version from
    the card's next span. That title begins with what names the card and the rest is on it: verified."""

    def page(self):
        return AV.Page(CRATES_DOM, "rand\nv0.10.3\nRandom number generators\nbevy_rand\nv0.16.0\n"
                                   "A plugin for random numbers", "https://crates.io/search?q=rand")

    def test_a_title_that_begins_with_its_link_text_is_verified(self):
        res = [{"title": "rand v0.10.3", "url": "https://crates.io/crates/rand"},
               {"title": "bevy_rand v0.16.0", "url": "https://crates.io/crates/bevy_rand"}]
        v = AV.verify_list({"results": res}, self.page())
        self.assertEqual([d.status for d in v.decisions], [AV.VERIFIED, AV.VERIFIED], v.why())

    def test_words_that_are_not_on_the_card_still_withhold(self):
        res = [{"title": "rand v0.10.3 crypto edition", "url": "https://crates.io/crates/rand"}]
        self.assertNotEqual(AV.verify_list({"results": res}, self.page()).decisions[0].status, AV.VERIFIED)

    def test_a_title_that_begins_with_another_cards_link_is_not_named_by_its_own(self):
        res = [{"title": "bevy_rand v0.16.0", "url": "https://crates.io/crates/rand"}]
        self.assertNotEqual(AV.verify_list({"results": res}, self.page()).decisions[0].status, AV.VERIFIED)


class ConfiguredSecondStage(unittest.TestCase):
    """The second stage runs on whatever model the judge role resolves to, not only Cerebras."""

    def adapter(self, provider):
        class Usage:
            prompt_tokens, completion_tokens, cost_usd = 400, 20, 0.0123

        class Result:
            ok, parsed, text, usage, llm_calls, error, budget_exhausted = True, {"choice": "in_stock", "line": 1}, \
                "", Usage(), 1, "", False

        class Cfg:
            base_url, model = "https://api.example/v1", "m"

        Cfg.provider = provider
        sent = []

        class Ad:
            config, price_in, price_out = Cfg(), 0.0, 0.0

            def complete(self, messages, **kw):
                sent.append(kw)
                return Result()
        return Ad(), sent

    def test_any_provider_answers_the_second_stage_and_reports_its_own_cost(self):
        for provider in ("claude-cli", "openai", "anthropic", "codex-cli"):
            ad, sent = self.adapter(provider)
            out = AV.configured_model(adapter=ad)("prompt", {"type": "object"})
            self.assertEqual(out["choice"], "in_stock", provider)
            self.assertEqual(out["usd"], 0.0123, provider)
            self.assertEqual(sent[0]["schema_name"], "attribute_check")

    def test_model_configured_follows_the_configured_provider(self):
        import os
        import shutil
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_") and k not in
               ("CEREBRAS_API_KEY", "OPENAI_API_KEY")}
        with mock.patch.dict(os.environ, dict(env, ANYUSE_MODEL_PROVIDER="openai", ANYUSE_MODEL_NAME="m",
                                              OPENAI_API_KEY="not-a-real-key"), clear=True):
            self.assertTrue(AV.model_configured())
        with mock.patch.dict(os.environ, dict(env, ANYUSE_MODEL_PROVIDER="openai", ANYUSE_MODEL_NAME="m"),
                             clear=True), mock.patch.dict(os.environ, {"ANYUSE_MODEL_ENV_FILE": "/nonexistent"}):
            self.assertFalse(AV.model_configured())
        with mock.patch.dict(os.environ, dict(env, ANYUSE_MODEL_PROVIDER="claude-cli"), clear=True), \
                mock.patch.object(shutil, "which", lambda name: "/usr/local/bin/claude"):
            self.assertTrue(AV.model_configured())
        with mock.patch.dict(os.environ, dict(env, ANYUSE_MODEL_PROVIDER="claude-cli"), clear=True), \
                mock.patch.object(shutil, "which", lambda name: None):
            self.assertFalse(AV.model_configured())

if __name__ == "__main__":
    unittest.main()
