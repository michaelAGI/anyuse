"""Deterministic value cleaning before any check (``execution/value_clean.py``), offline:
pure strings, no network, no model.

The web value check used to let through real product names with UI, stock or site-name text ATTACHED and
leading list positions; OpenJev called them product names at >= 0.99. What must hold:

1. attached UI / CTA text, stock phrases, badges, "+ more variants" tails, price labels and prices are stripped
   - the observed leaks verbatim - in each of the gate lexicon's 11 languages (EN DE NL FR ES IT SV DA NO FI PL);
2. a leading list position goes only when the siblings are numbered (distinct numbers; a count such as
   "50 Stuks" is no position), once; "7 Wonders" in an unnumbered list is kept;
3. the page's own site name goes as a prefix / tail set off by a separator or a place preposition; the names
   come from the host, the title, og:site_name; a brand that is the site's name inside a title stays;
4. a size that belongs to the item stays ("100 ml", "160 x 80 cm"); a size selector / size label goes;
   ambiguous badges and price words stay inside names ("New Balance", "Top Gun", "Grand Prix", "Tipp-Ex");
5. a value that becomes empty or generic is DROPPED (a word of the site's name is not the site's name);
   the record keeps the original and the cleaned value and the steps.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import value_clean as VC  # noqa: E402

STOCKMANN = VC.site_names_of("www.stockmann.com", "Stockmann - verkkokaupassa ja tavarataloissa | Stockmann")


def c(v, names=(), siblings=()):
    r = VC.clean_value(v, names, siblings)
    return "DROP" if r.dropped else r.cleaned


def test_prior_leaks_verbatim():
    assert c("Produktkort Anker Nano Powerbank 22.5W USB-C") == "Anker Nano Powerbank 22.5W USB-C"
    assert c("I lager Haakaa Ladybug Bröstmjölksuppsamlare") == "Haakaa Ladybug Bröstmjölksuppsamlare"
    assert c("Pomar - HUNAJA pistokkaat - BLACK NAPPA | Stockmann", STOCKMANN) == "Pomar - HUNAJA pistokkaat - BLACK NAPPA"
    assert c("Røros Tweed Lambswool blanket Bauhaus style, Blue + more variants") == \
        "Røros Tweed Lambswool blanket Bauhaus style, Blue"
    assert c("Se alle størrelser på produktet Barbour Sidwood Hiking Boot") == "Barbour Sidwood Hiking Boot"
    assert c("Weiter zum Produkt The North Face ANTORA Regenjacke Damen") == "The North Face ANTORA Regenjacke Damen"
    assert c("Fast ausverkauft Limitiert Wir waren hier Die Nerven LP Coloured") == "Wir waren hier Die Nerven LP Coloured"
    assert c("Limitiert Vorher: Genau hier Viva Single Limited Edition") == "Genau hier Viva Single Limited Edition"
    assert c("6 Kvar Medela Magic InBra Brösttrattar 27 mm 2-Pack") == "Medela Magic InBra Brösttrattar 27 mm 2-Pack"
    assert c("CASAVANTI Schreibtisch 160 x 80 cm Eiche reduziert zu") == "CASAVANTI Schreibtisch 160 x 80 cm Eiche"
    assert c("FAST CROP RUNWAY PINK NIKE Ursprungligt pris: Reapris (- )") == "FAST CROP RUNWAY PINK NIKE"
    assert c("Deal Blik. Blik Self-Stick Lash Clusters Brown 0.2") == "Blik. Blik Self-Stick Lash Clusters Brown 0.2"
    assert c("Gå til Kranier Ø4,5 cm - 12 stk") == "Kranier Ø4,5 cm - 12 stk"
    assert c("ALESIS TURBO PRO KIT •") == "ALESIS TURBO PRO KIT"


def test_eleven_languages():
    cases = {
        "en": ("Quick view Linen Shirt", "Linen Shirt - Out of stock", "Linen Shirt Add to cart"),
        "de": ("Zum Produkt Leinenhemd", "Nur noch 3 Stück Leinenhemd", "Leinenhemd + 3 Farben"),
        "nl": ("Bekijk product Linnen overhemd", "Linnen overhemd Op voorraad", "Nieuw Linnen overhemd"),
        "fr": ("Voir le produit Chemise", "Chemise Rupture de stock", "Chemise Ajouter au panier"),
        "es": ("Ver producto Camisa", "Camisa Agotado", "Novedad Camisa"),
        "it": ("Vedi prodotto Camicia", "Camicia Esaurito", "Camicia + altri colori"),
        "sv": ("Visa produkt Linneskjorta", "Linneskjorta Slutsåld", "Linneskjorta Lägg i varukorgen"),
        "da": ("Se produkt Hørskjorte", "3 tilbage Hørskjorte", "Hørskjorte Læg i kurv"),
        "no": ("Kjøp nå Linskjorte", "2 igjen Linskjorte", "Linskjorte Legg i handlekurven"),
        "fi": ("Katso tuote Pellavapaita", "Pellavapaita Loppuunmyyty", "Uutuus Pellavapaita"),
        "pl": ("Zobacz produkt Lniana koszula", "Lniana koszula Niedostępny", "Nowość Lniana koszula"),
    }
    assert set(cases) == set(VC.LANGS)
    for lang, vals in cases.items():
        for v in vals:
            out = c(v)
            assert out in ("Linen Shirt", "Leinenhemd", "Linnen overhemd", "Chemise", "Camisa", "Camicia",
                           "Linneskjorta", "Hørskjorte", "Linskjorte", "Pellavapaita", "Lniana koszula"), (lang, v, out)
    # accent-tolerant: the same phrase without its accents
    assert c("Se alle storrelser pa produktet JANET") == "JANET"


def test_list_positions_only_in_numbered_lists():
    sib = ["1 Vantage Feuerland Spiele", "2 Kennerspiele-Überraschungsbox 2026 Milan Spiele", "3 Cabo Smiling Monster Gam"]
    assert c("7 Familienspiele-Überraschungsbox 2026 Milan Spiele", (), sib) == "Familienspiele-Überraschungsbox 2026 Milan Spiele"
    for v in ("4 Lost Cities", "4. Lost Cities", "#4 Lost Cities", "4) Lost Cities"):
        assert c(v, (), sib) == "Lost Cities", v
    assert c("3 7 Wonders", (), sib) == "7 Wonders"                       # once
    assert c("7 Wonders", (), ["Catan", "Azul", "Carcassonne"]) == "7 Wonders"
    assert c("1. FC Köln Trikot") == "1. FC Köln Trikot"                  # no siblings: never
    assert c("50 Stuks Golfballen", (), sib) == "50 Stuks Golfballen"     # a count is no position
    assert not VC.numbered_siblings(["50 Stuks a", "24 Stuks b", "12 Stuks c"])
    assert not VC.numbered_siblings(["1 a", "1 b"])                       # not distinct
    assert not VC.numbered_siblings(["1 a"])


def test_site_names():
    names = VC.site_names_of("www.example-shop.de", "Example Shop | Mode online", "")
    assert "Example Shop" in names and "example-shop.de" in names
    assert c("Linen Shirt | Example Shop", names) == "Linen Shirt"
    assert c("Example Shop - Linen Shirt", names) == "Linen Shirt"
    assert c("Linen Shirt bei Example Shop", names) == "Linen Shirt"
    assert c("Example Shop", names) == "DROP"
    assert "Burgon & Ball" in VC.site_names_of("www.burgonandball.com", "Burgon & Ball | Garden tools")
    bb = VC.site_names_of("www.burgonandball.com", "Burgon & Ball | Garden tools")
    assert c("Apple picker from Burgon & Ball", bb) == "Apple picker from Burgon & Ball"   # the maker, kept
    assert c("Bialetti Moka Express", VC.site_names_of("www.bialetti.com", "Bialetti | Moka")) == "Bialetti Moka Express"
    assert VC.site_names_of("www.x.com", "", "Shop Name AB")[0] == "Shop Name AB"
    # a word of the site's name is not the site's name
    assert c("Android", VC.site_names_of("www.androidplanet.nl", "Android Planet - nieuws")) == "Android"
    assert c("nähPark", VC.site_names_of("www.naehpark.com", "")) == "DROP"
    assert VC.og_site_name({"meta": {}, "dom": '<meta property="og:site_name" content="Milan Spiele">'}) == "Milan Spiele"


def test_sizes_prices_and_name_parts_kept():
    assert c("Beauty of Joseon Radiance Cleansing Balm 100 ml") == "Beauty of Joseon Radiance Cleansing Balm 100 ml"
    assert c("Monster Energy Drink Sugarfree 50cl 12er Pack") == "Monster Energy Drink Sugarfree 50cl 12er Pack"
    assert c("Linen Shirt Größe: S M L XL") == c("Linen Shirt XS S M L XL") == "Linen Shirt"
    assert c("Sneaker Fly 36 37 38 39") == "Sneaker Fly"
    assert c("JANET 1.299,95 kr") == "JANET" and c("Hoodie ab 29,99 €") == "Hoodie"
    assert c("Linen Shirt UVP 39,99 €") == c("Linen Shirt (1 kg = 5,99 €)") == c("Linen Shirt -20 %") == "Linen Shirt"
    assert c("Kaffee 500 g 12,99 €/kg") == "Kaffee 500 g"
    assert c("Linen Shirt Pris: 299 kr") == "Linen Shirt"
    for keep in ("New Balance 574", "Top Gun Maverick Blu-ray", "Hot Wheels Monster Trucks", "Monaco Grand Prix Poster",
                 "Cena di Natale Kit", "Tipp-Ex Korrekturroller", "Dr. Jart+", "Clear Whey + Collagen — Vimto®",
                 "Indivie e Lattughe in varietà in pack da 9 piante", "Kleiderschrank Harry weiß B/H/T: ca. 150x177x50 cm"):
        assert c(keep) == keep, keep
    assert c("NEW Nike Air Max 90") == "Nike Air Max 90" and c("New: Linen Shirt") == "Linen Shirt"
    assert c("NEW BALANCE 574") == "NEW BALANCE 574"


def test_dropped_and_the_record():
    for v in ("zurück zur Startseite", "zzgl. Versandkosten", "Leggi di più", "banner image", "Store Opening Hours",
              "Oktober", "SKIP TO CONTENT", "Produktkort", "I lager", "", "  ", "12,99 €"):
        r = VC.clean_value(v)
        assert r.dropped and r.cleaned == "" and r.why, v
    r = VC.clean_value("Produktkort Samsung SmartTag2 Svart")
    d = r.to_dict()
    assert d["original"] == "Produktkort Samsung SmartTag2 Svart" and d["cleaned"] == "Samsung SmartTag2 Svart"
    assert d["changed"] and not d["dropped"] and d["steps"] == ["cta: Produktkort"]
    same = VC.clean_value("Hoekbank Regalo")
    assert not same.changed and same.cleaned == same.original and same.steps == []
    assert [x.cleaned for x in VC.clean_values(["A Shirt | Stockmann", "B Shirt"], host="www.stockmann.com")] == \
        ["A Shirt", "B Shirt"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
