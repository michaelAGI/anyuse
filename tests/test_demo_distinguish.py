"""Web tool version 9, offline (a fake shop built from saved evidence; no browser, no network, no
model): the input's DISTINGUISHING words are mandatory in the identity - a colour (a lexicon of the 11 gate-lexicon
languages, inflections and synonyms across languages), a number (a size, capacity, quantity, vintage), a size word
(XS ... XXXL), a model code (letters and digits). The chosen result must carry each one (or an equivalent) in its
title, else in the opened product's variant attributes (its JSON-LD colour / size, the selected variant, a colour or
size label of the buy box); otherwise it is not the match - the next result that carries them, or an honest
failure. The overall 75 % word rule stays for the other words.

The wrong output: duka.com - the input "Podkładka czarna na stół okrągła 38 cm drewniana DUKA SENSU" (black)
answered with the BEIGE placemat ("Podkładka na stół beżowa okrągła 38 cm drewniana DUKA SENSU": 10 of 11 words,
"unique best"); the result list showed no black one. The brown value (unaudited) answered the same beige placemat.

What must hold:

1. on duka's result list a version-9 tool answers INCOMPLETE ("the asked-for variant is not listed") for the black
   and the brown placemat; the same demonstration compiled as version 8 answers the beige one (the measured output);
2. a result that carries the distinguishing word is chosen over a sibling that does not (where version 8 found two
   results naming the input equally: ambiguous);
3. the lexicon: inflections and other languages' words of one colour are equivalent, another colour is not; numbers
   with their units ("38cm" / "38 cm", "1,5 l" / "1.5 L"), model codes across separators ("PGI-550" / "PGI 550");
4. a result title without the word (a family title) is opened and the product's variant attributes decide: its
   JSON-LD colour or a buy-box colour label naming it is the match, another colour an identity failure.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from test_demo_canonical import BASE, Shop, compiled, product, run  # noqa: E402

BEIGE = "Podkładka na stół beżowa okrągła 38 cm drewniana DUKA SENSU"
BLACK_V = "Podkładka czarna na stół okrągła 38 cm drewniana DUKA SENSU"
BROWN_V = "Podkładka drewniana na stół okrągła 38 cm brązowa DUKA SENSU"
#: duka.com's result list for both values (the saved list: the beige placemat and unrelated products)
OTHERS = [("dyspenser-duka-tap", "Dyspenser na napoje ze stojakiem DUKA TAP 6200 ml szkło", 199.0),
          ("kubek-duka-siren", "Kubek kropki paski DUKA SIREN 300 ml granatowy złoty kamionkowy", 24.9),
          ("torba-duka-mini", "Torba termiczna na lunch DUKA MINI 22x19x8 cm turkusowa", 39.9)]


def duka_shop(extra=()):
    cat = [product(s, n, p) for s, n, p in OTHERS[:2]]
    cat.append(product("podkladka-duka-sensu-38-cm-bezowa", BEIGE, 29.9, sku="2220831"))
    cat += [product(s, n, p) for s, n, p in OTHERS[2:]] + list(extra)
    return Shop(cat)


def test_prior_sibling_variant_is_not_answered_and_version_8_unchanged():
    shop = duka_shop()
    k = 2                                    # the demonstration opened the beige placemat (its own value)
    t9, t8 = compiled(shop, k), compiled(shop, k, version=8)
    for value in (BLACK_V, BROWN_V):
        r9, r8 = run(shop, t9, value), run(shop, t8, value)
        assert r9["status"] == "incomplete" and not r9["accepted"], r9
        assert "not listed" in r9["why"] and r9["not_listed"]["closest"]["title"] == BEIGE, r9
        assert r8["status"] == "ok" and r8["output"]["name"] == BEIGE, r8              # the measured wrong output
    # the demonstrated value itself: carried, answered as before
    r9 = run(shop, t9, BEIGE)
    assert r9["status"] == "ok" and r9["output"]["name"] == BEIGE, r9


def test_the_result_carrying_the_distinguishing_word_is_chosen():
    black = "Podkładka na stół czarna 38 cm drewniana DUKA SENSU"         # 'okrągła' missing: 10 of 11 words too
    shop = duka_shop(extra=[product("podkladka-duka-sensu-38-cm-czarna", black, 29.9)])
    t9, t8 = compiled(shop, 2), compiled(shop, 2, version=8)
    r9, r8 = run(shop, t9, BLACK_V), run(shop, t8, BLACK_V)
    assert r9["status"] == "ok" and r9["output"]["name"] == black, r9
    assert r8["status"] == "failed" and "ambiguous" in r8["why"], r8               # version 8: two results tie


def test_the_lexicon_numbers_and_codes():
    d = C.distinguishing(BLACK_V)
    kinds = sorted((m["kind"], m.get("class") or m["token"]) for m in d)
    assert kinds == [("colour", "black"), ("number", "38")], kinds
    for value, title, want in [
            ("Podkładka czarna", "Podkładka czarny", True),                    # inflection
            ("Kettle schwarz", "Kettle Black", True), ("Kettle noir", "Kettle czarny", True),
            ("Kettle Weiß", "Kettle white", True), ("Kettle grøn", "Kettle Grün", True),
            ("Kettle zwart", "Kettle grijs", False), ("Podkładka czarna", BEIGE, False),
            ("Lunchbox 38cm", "Lunchbox 38 cm", True), ("Flasche 1,5 l", "Flasche 1.5 L", True),
            ("Flasche 2 l", "Flasche 1,7 l", False), ("Canon PGI-550 PGBK", "Canon PGI 550 PGBK", True),
            ("Canon PGI-550 PGBK", "Canon PGI550 PGBK Tinte", True), ("Canon PGI-550 PGBK", "Canon PGI-580 PGBK", False),
            ("Shirt Gr. XL", "Shirt XL", True), ("Shirt XL", "Shirt XXL", False),
            ("Chablis Montserre Bio 2022", "Chablis Montserre Bio 2021", False)]:
        miss = C.missing_marks(title, C.distinguishing(value))
        assert (not miss) is want, (value, title, miss)
    # contradiction: another colour (not merely none)
    assert C.contradicting(BEIGE, C.distinguishing(BLACK_V)) and not C.contradicting("Podkładka DUKA SENSU",
                                                                                     C.distinguishing(BLACK_V))
    # a value without such a word has none: the selection is version 8's
    assert C.distinguishing("Soczewki Accent Shot") == [] and C.distinguishing("Thank You Gift Box Small") == []


def test_the_product_s_variant_attributes_decide_when_the_title_has_no_such_word():
    fam = "Acme Wasserkocher Classic"
    for attrs, buy, ok in [({"color": "Black"}, None, True), ({"color": "Weiß"}, None, False),
                           ({}, ["{price} €", "Farbe: Schwarz", "Auf Lager"], True),
                           ({}, ["{price} €", "Farbe: Rot", "Auf Lager"], False)]:
        kw = dict(attrs)
        if buy:
            kw["buy"] = buy
        shop = Shop([product("acme-wasserkocher-classic", fam, 39.0, **kw)])
        t9 = compiled(shop, 0)
        r9 = run(shop, t9, "Acme Wasserkocher Classic schwarz")
        if ok:
            assert r9["status"] == "ok" and r9["output"]["name"] == fam, (attrs, buy, r9)
            assert r9["checks"]["identity9"]["marks"][0]["carried_by"], r9["checks"]
        else:
            assert r9["status"] == "failed" and r9["why"].startswith("identity:") and "schwarz" in r9["why"], r9
    assert BASE


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
