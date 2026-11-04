"""Deterministic value cleaning, before any check.

Running the web value check (OpenJev's typed question, ``execution/value_check.py``) showed what a type
question cannot catch: a REAL product name with UI, stock or site-name text ATTACHED - "Produktkort Anker
Nano Powerbank 22.5W USB-C", "I lager Haakaa Ladybug Bröstmjölksuppsamlare", "... | Stockmann", "... + more
variants" - and a leading list position - "7 Familienspiele-Überraschungsbox 2026 Milan Spiele". OpenJev
calls those product names at >= 0.99. They need cleaning, not a type question.

:func:`clean_value` strips, in a loop until nothing changes:

1. leading bullets and symbols, a trailing bullet;
2. a leading LIST POSITION ("7 ", "7. ", "7) ", "#7 ") - only when the value's SIBLINGS (the other titles of
   the same list) are numbered the same way with distinct numbers: a name that starts with a number ("7
   Wonders", "1. FC Köln Trikot") is kept when its list is not numbered;
3. attached UI / call-to-action text as a prefix ("Produktkort", "Se alle størrelser på produktet",
   "Weiter zum Produkt", "Gå til", "Quick view") or a tail ("Jetzt kaufen", "Add to cart"); badges as a
   prefix ("Nyhet", "Deal", "Limitiert"; an ambiguous one - "New", "Top", "Hot", "Ny" - only when set off
   by a separator or written in capitals before a name that is not);
4. stock / availability phrases as a prefix or a tail ("I lager", "6 Kvar", "Fast ausverkauft", "Op
   voorraad", "Sold out", "Nur noch 3 Stück");
5. "+ N more" / "+ more variants" / "weitere Farben" / "3 kleuren" tails;
6. the page's OWN site name as a prefix or a tail set off by a separator ("... | Stockmann", "Stockmann -
   ...") or a place preposition ("... bei Otto", "... bij Bol.com"); the names come from the host, the page
   title and ``og:site_name`` (:func:`site_names_of`); a brand that is also the site's name and is part of
   the title without a separator ("Bialetti Moka Express" on www.bialetti.com) is kept;
7. prices glued to a name ("JANET 1.299,95 kr", "ab 12,99 €"), unit prices, discount badges ("-20 %"),
   price labels ("Ursprungligt pris: Reapris", "Vorher:", "reduziert zu", "UVP"), and size selectors or
   size labels glued to a name ("Größe: S M L XL", "Sizes 36 37 38 39"); a size that belongs to the item
   ("Cleansing Balm 100 ml", "160 x 80 cm") is kept.

A value that becomes EMPTY or GENERIC (nothing left but function words, UI / stock / price / badge words,
month or weekday names, image placeholders or the site's own name) is DROPPED. The record keeps both the
original and the cleaned value, and the steps that fired (:class:`Cleaned`).

Multilingual: the gate lexicon's 11 languages (``execution/gate_lexicon.py``): EN DE NL FR ES IT SV DA NO
FI PL; matching tolerates case and accents ("storrelser" matches "størrelser"). Pure: no model, no network.
"""
from __future__ import annotations

import html as _html
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LANGS: Tuple[str, ...] = ("en", "de", "nl", "fr", "es", "it", "sv", "da", "no", "fi", "pl")
VERSION = 1
MAX_PASSES = 6


# ---------------------------------------------------------------------------
# the lexicon (lower case; "#" = a number). Every list covers the 11 languages.
# ---------------------------------------------------------------------------

#: UI / call-to-action text ATTACHED BEFORE a name (a card's link text that starts with its button)
CTA_PREFIX = """
view product|view details|view item|quick view|quick shop|quick buy|shop now|buy now|see all sizes for|see all sizes of|
see all sizes|see all colours for|see all colors for|see details|go to product|go to|product card|learn more about|
read more about|more about|select options|choose options|
zum produkt|zum artikel|zur produktseite|weiter zum produkt|weiter zum artikel|zu den produktdetails|produktdetails|
details ansehen|artikel ansehen|produkt ansehen|mehr erfahren über|mehr erfahren|mehr infos zu|mehr zu|jetzt kaufen|
jetzt entdecken|jetzt ansehen|schnellansicht|alle größen von|alle größen|alle farben von|optionen wählen|
bekijk product|bekijk alle maten van|bekijk alle maten|bekijk|naar product|naar het product|ga naar|snel bekijken|
nu kopen|koop nu|opties kiezen|meer over|
voir le produit|voir le détail|voir les détails|voir toutes les tailles de|voir toutes les tailles|aller au produit|
aperçu rapide|en savoir plus sur|choisir les options|acheter maintenant|
ver producto|ver detalles|ver todas las tallas de|ver todas las tallas|ir al producto|comprar ahora|vista rápida|
saber más sobre|elegir opciones|
vedi prodotto|vedi dettagli|vedi tutte le taglie di|vedi tutte le taglie|vai al prodotto|acquista ora|scopri di più su|
visualizzazione rapida|anteprima rapida|scegli opzioni|
produktkort|visa produkt|visa alla storlekar för|visa alla storlekar|gå till produkten|gå till|se alla storlekar för|
se alla storlekar på|se alla storlekar|köp nu|snabbvy|läs mer om|välj alternativ|
se alle størrelser på produktet|se alle størrelser på|se alle størrelser|se produkt|gå til produktet|gå til|køb nu|
kvikvisning|læs mere om|vælg variant|
se alle størrelser|se produktet|kjøp nå|hurtigvisning|les mer om|velg variant|
tuotekortti|katso tuote|katso kaikki koot|siirry tuotteeseen|osta nyt|pikakatselu|lue lisää tuotteesta|valitse vaihtoehto|
karta produktu|zobacz produkt|zobacz wszystkie rozmiary|przejdź do produktu|kup teraz|szybki podgląd|czytaj więcej o|
wybierz opcje
"""
#: UI / call-to-action text ATTACHED AFTER a name (a card's button text glued to its title)
CTA_SUFFIX = """
add to cart|add to basket|add to bag|add to wishlist|add to favourites|add to favorites|buy now|shop now|view product|
view details|quick view|read more|learn more|more info|see details|select options|choose options|compare|
in den warenkorb|in den einkaufswagen|zum warenkorb hinzufügen|jetzt kaufen|zum produkt|details ansehen|mehr erfahren|
weiterlesen|auf die merkliste|auf den merkzettel|merken|vergleichen|optionen wählen|
in winkelwagen|in de winkelwagen|toevoegen aan winkelwagen|bekijk product|lees meer|vergelijk|opties kiezen|
ajouter au panier|voir le produit|en savoir plus|lire la suite|comparer|choisir les options|
añadir al carrito|añadir a la cesta|ver producto|leer más|comparar|elegir opciones|
aggiungi al carrello|vedi prodotto|leggi di più|scopri di più|confronta|scegli opzioni|
lägg i varukorgen|lägg i kundvagnen|köp nu|läs mer|jämför|välj alternativ|
læg i kurv|læg i kurven|køb nu|læs mere|sammenlign|vælg variant|
legg i handlekurven|legg i handlekurv|kjøp nå|les mer|velg variant|
lisää ostoskoriin|osta nyt|lue lisää|vertaa|valitse vaihtoehto|
dodaj do koszyka|kup teraz|czytaj więcej|porównaj|wybierz opcje
"""
#: a badge BEFORE a name ("Nyhet Vas Nikita", "Deal Blik. ...", "Limitiert ...")
BADGE = """
deal|deals|hot deal|top deal|bestseller|bestsellers|best seller|top seller|topseller|online exclusive|exclusive online|
online only|only online|web exclusive|limited edition|coming soon|pre-order|preorder|back in stock|last chance|
clearance|special offer|price drop|staff pick|
neu|neuheit|neuheiten|nur online|online exklusiv|exklusiv|limitiert|vorbestellung|vorbestellbar|wieder da|
wieder verfügbar|angebot|sonderangebot|aktion|preishit|preisknaller|restposten|letzte chance|tipp|empfehlung|
nieuw|exclusief|online exclusief|aanbieding|actie|uitverkoop|beperkte oplage|weer op voorraad|laatste kans|topper|
nouveau|nouveauté|nouveautés|exclusivité|exclusif|exclusif web|promo|soldes|offre spéciale|meilleure vente|
top vente|édition limitée|précommande|dernière chance|bon plan|coup de cœur|de retour|
nuevo|novedad|novedades|exclusivo|exclusivo online|oferta|ofertas|rebajas|promoción|más vendido|edición limitada|
preventa|última oportunidad|chollo|
nuovo|novità|esclusiva|esclusivo|offerta|saldi|promozione|più venduto|edizione limitata|preordine|ultima occasione|
imperdibile|di nuovo disponibile|
nyhet|nyheter|exklusivt|erbjudande|kampanj|bästsäljare|topplista|begränsad upplaga|förhandsboka|sista chansen|fynd|
tillbaka i lager|webbexklusiv|
nyhed|nyheder|eksklusiv|udsalg|tilbud|kampagne|begrænset oplag|forudbestil|sidste chance|tilbage på lager|
nyheter|eksklusivt|salg|kampanje|bestselger|begrenset opplag|forhåndsbestill|siste sjanse|tilbake på lager|
uutuus|uutuudet|tarjous|kampanja|myydyin|suosittu|eksklusiivinen|rajoitettu erä|ennakkotilaus|
viimeinen mahdollisuus|takaisin varastossa|
nowość|nowości|promocja|wyprzedaż|okazja|ekskluzywny|edycja limitowana|przedsprzedaż|ostatnia szansa|
ponownie dostępny
"""
#: badges that are also ordinary words or name parts ("New Balance", "Top Gun", "Hot Wheels", "NY Yankees"):
#: stripped only when set off by a separator, or written in capitals before a name that is not
BADGE_AMBIGUOUS = "new|top|hot|hit|eco|sale|ny|rea|ale|uusi|nowy|neuf|nuova|ny!|limited|popular|offer|exclusive"
#: stock / availability (a prefix or a tail)
STOCK = """
in stock|out of stock|sold out|low stock|only # left|# left|# in stock|available now|available online|
available in store|in store only|temporarily out of stock|currently unavailable|unavailable|ready to ship|ships today|
auf lager|nicht auf lager|sofort lieferbar|sofort verfügbar|lieferbar|nicht lieferbar|ausverkauft|fast ausverkauft|
vergriffen|nur noch # stück|nur noch # verfügbar|nur noch #|noch # stück|nicht verfügbar|online verfügbar|
im markt verfügbar|lagernd|artikel ist vorrätig|vorrätig|
op voorraad|niet op voorraad|uitverkocht|tijdelijk uitverkocht|direct leverbaar|leverbaar|niet leverbaar|
niet beschikbaar|nog # op voorraad|nog # beschikbaar|morgen in huis|
en stock|en rupture de stock|rupture de stock|épuisé|indisponible|non disponible|plus que #|il reste #|
derniers articles|stock limité|
agotado|no disponible|sin stock|quedan #|últimas unidades|últimas # unidades|stock limitado|
non disponibile|esaurito|in esaurimento|ultimi #|solo # disponibili|disponibilità immediata|pronta consegna|
i lager|finns i lager|på lager|ej i lager|inte i lager|slut i lager|tillfälligt slut|slutsåld|# kvar|få kvar|
beställningsvara|
ikke på lager|udsolgt|midlertidigt udsolgt|# tilbage|få tilbage|få på lager|på fjernlager|bestillingsvare|
på nettlager|utsolgt|midlertidig utsolgt|# igjen|få igjen|
varastossa|ei varastossa|loppu varastosta|loppuunmyyty|tilapäisesti loppu|# jäljellä|vain # jäljellä|
heti saatavilla|tilaustuote|
dostępny|niedostępny|w magazynie|brak w magazynie|wyprzedany|ostatnie sztuki|zostało #|tylko # szt|
produkt niedostępny|dostępny od ręki|na stanie
"""
#: "+ N more / more variants" tails, as phrases (the "+" forms are a regex: _MORE_PLUS)
MORE_TAIL = """
more colours available|more colors available|more variants available|more options available|available in # colours|
available in # colors|available in # sizes|also available in|# colours|# colors|# variants|
weitere farben|weitere varianten|weitere größen|in # farben erhältlich|in # farben|# farben|# varianten|
meer kleuren|meer varianten|meer maten|# kleuren|
plus de couleurs|autres coloris|# couleurs|# coloris|# variantes|
más colores|# colores|más variantes|
altri colori|# colori|altre varianti|# varianti|
fler färger|finns i fler färger|# färger|fler varianter|# varianter|
flere farver|fås i flere farver|# farver|flere varianter|
flere farger|finnes i flere farger|# farger|
lisää värejä|# väriä|useita värejä|
więcej kolorów|# kolory|# kolorów|więcej wariantów
"""
_MORE_WORDS = "more|weitere|meer|plus|más|altri|altre|fler|flere|lisää|więcej|mehr"
_VARIANT_WORDS = ("variants?|varianten|varianter|variantes|varianti|warianty|wariantów|vaihtoehtoa|colou?rs?|farben?|"
                  "kleuren|couleurs|coloris|colores|colori|färger|farver|farger|väriä|värejä|kolory|kolorów|sizes?|"
                  "größen|maten|tailles|tallas|taglie|storlekar|størrelser|kokoa|rozmiary|rozmiarów|options?|styles?|"
                  "modelle|modellen|modèles|modelos|modelli|modeller|mallia")
#: price labels that never belong to a name (a prefix or a tail, with or without the price)
PRICE_LABEL = """
rrp|original price|regular price|sale price|list price|our price|unit price|you save|
vorher|statt|uvp|unverbindliche preisempfehlung|reduziert zu|reduziert von|reduziert auf|grundpreis|sie sparen|
ursprünglicher preis|streichpreis|inkl. mwst.|inkl. mwst|zzgl. versandkosten|zzgl. versand|inkl. versand|
adviesprijs|van-prijs|je bespaart|incl. btw|
au lieu de|prix barré|prix conseillé|économisez|
precio recomendado|precio anterior|ahorra|
invece di|prezzo consigliato|risparmi|iva inclusa|
ordinarie pris|ord. pris|ord.pris|ursprungligt pris|reapris|tidigare pris|jmf-pris|jmfpris|jämförpris|lägsta pris|
normalpris|vejl. pris|vejledende pris|førpris|
veil. pris|veiledende pris|ordinær pris|tidligere pris|
normaalihinta|norm. hinta|ovh.|ovh|
cena regularna|cena katalogowa|najniższa cena z 30 dni|najniższa cena|przed obniżką|oszczędzasz
"""
#: words that are a price label only next to a price or before a colon ("Pris:", "ab 12,99 €"); elsewhere
#: they can be name parts ("Grand Prix", "Cena", "from Burgon & Ball", "da 9 piante")
PRICE_LABEL_AMBIGUOUS = """
price|was|now|from|only|save|preis|ab|nur|jetzt|ca.|prijs|van|voor|nu|vanaf|prix|dès|à partir de|des|precio|antes|
ahora|desde|pvp|prezzo|prima|ora|da|a partire da|pris|från|spara|nu|før|spar|fra|nå|hinta|alkaen|nyt|säästä|cena|od|
teraz|ord.
"""
#: a size label glued to a name ("Größe: S M L") - stripped with the sizes after it
SIZE_LABEL = ("größen|größe|grösse|gr\\.|sizes|size|maten|maat|tailles|taille|tallas|talla|taglie|taglia|storlekar|"
              "storlek|strl\\.|str\\.|størrelser|størrelse|koot|koko|rozmiary|rozmiar")
#: the place prepositions before a site name ("... bei Otto", "... bij Bol.com", "... hos Magasin"); never the
#: maker's "from" / "von" / "van" / "de" / "da" (the site is often the brand: "Apple picker from Burgon & Ball")
SITE_PREP = "at|bei|im|bij|chez|en|su|på|hos|w"

MONTHS = """
january february march april may june july august september october november december
januar februar märz maerz april mai juni juli august september oktober november dezember jänner
januari februari maart april mei juni juli augustus september oktober november december
janvier février mars avril mai juin juillet août septembre octobre novembre décembre
enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre
gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre
januari februari mars april maj juni juli augusti september oktober november december
januar februar marts april maj juni juli august september oktober november december
januar februar mars april mai juni juli august september oktober november desember
tammikuu helmikuu maaliskuu huhtikuu toukokuu kesäkuu heinäkuu elokuu syyskuu lokakuu marraskuu joulukuu
styczeń luty marzec kwiecień maj czerwiec lipiec sierpień wrzesień październik listopad grudzień
monday tuesday wednesday thursday friday saturday sunday montag dienstag mittwoch donnerstag freitag samstag sonntag
maandag dinsdag woensdag donderdag vrijdag zaterdag zondag lundi mardi mercredi jeudi vendredi samedi dimanche
lunes martes miércoles jueves viernes sábado domingo lunedì martedì mercoledì giovedì venerdì sabato domenica
måndag tisdag onsdag torsdag fredag lördag söndag mandag tirsdag onsdag torsdag fredag lørdag søndag
maanantai tiistai keskiviikko torstai perjantai lauantai sunnuntai
poniedziałek wtorek środa czwartek piątek sobota niedziela
"""
#: function words of the 11 languages (beyond ``web_arch.STOPWORDS``)
FUNCTION_WORDS = """
a an the of for and or with in on at to by from my me i you your our this that these those it its is are be was were
all more most other some such no not only own same so than too very can will just here there about over under up out
der die das den dem des ein eine einen einem einer und oder mit im in am an auf zu zum zur von vom für fur bei nach
aus ist sind wir sie ihr ihre unser unsere uns alle mehr hier jetzt noch nur auch
de het een en of met voor op mijn bij van naar aan uit is zijn wij ons onze alle meer hier nu ook deze dit die dat
over om te
le la les un une des du de et ou avec pour sur au aux mon ma mes en dans par plus tous toutes ici nous notre nos
el los las una unos y o con para por del al mi mis en sobre más todo todos todas aquí nosotros nuestro nuestra
il lo gli uno e con per su al mio mia di da in del della delle degli dei alla alle tutti tutte più qui noi nostro
och eller med för till av på i om från den det de en ett alla mer här nu vi oss vår våra mina min mitt
og eller med for til af på i om fra den det de en et alle mere her nu vi os vores mine min mit
og eller med for til av på i om fra den det de en et alle mer her nå vi oss vår våre mine min mitt
ja tai kanssa kaikki lisää tässä nyt me meidän minun
i lub z ze dla do na w we o od po przez wszystkie więcej tutaj teraz my nasz nasza nasze moje mój
"""
#: site chrome: navigation, account, service, legal and image-placeholder words (a value made of them only)
CHROME_WORDS = """
home start startseite startsida forside forsiden etusivu accueil inicio strona główna menu menü navigate navigation
login log logga logg anmelden einloggen inloggen connexion iniciar sesión accedi kirjaudu zaloguj account konto
compte cuenta tili sidor sider sivut pages page seite pagina strona mijn mein mon mi omat moje
cart basket bag warenkorb winkelwagen panier carrito carrello varukorg kurv handlekurv ostoskori koszyk checkout kasse
kassa caisse search suche zoeken recherche buscar cerca sök søg søk haku szukaj filter filtern filtrer filtra
sort sortieren sorteren trier ordenar ordina sortera sorter lajittele sortuj newsletter kontakt contact contacto
contatti kontakta yhteystiedot impressum datenschutz privacy cookies cookie agb terms conditions faq hilfe help aide
ayuda aiuto hjälp hjælp hjelp apua pomoc service kundenservice klantenservice kundtjänst kundeservice asiakaspalvelu
storefinder filialen filialfinder stores store butiker butikker myymälät sklepy winkels magasins tiendas negozi
opening hours öffnungszeiten openingstijden horaires horarios orari öppettider åbningstider åpningstider aukioloajat
godziny otwarcia click klicken klik cliquez haga clic clicca klicka klikk napsauta kliknij next previous weiter
zurück terug retour volver indietro tillbaka tilbage tilbake takaisin wstecz back read lesen lees lire leer leggi
läs læs les lue czytaj bestellen order commander pedir ordina beställ bestil bestill tilaa zamów anzeige advertisement
advertentie publicité publicidad pubblicità annons annonce reklame mainos reklama sponsored gesponsert gallery galerie
bildergalerie galleria galleri galleria galeria image images img bild bilder afbeelding imagen immagine billede bilde
kuva zdjęcie banner hero logo icon thumbnail placeholder video videos share teilen delen partager compartir condividi
dela del jaa udostępnij compare vergleichen vergelijken comparer comparar confronta jämför sammenlign vertaa porównaj
details detail produktdetails info infos information informationen informatie informations
delivery shipping returns versand versandkosten lieferung rücksendung retouren levering verzending bezorging
retouren livraison retours envío envio envíos devoluciones spedizione spedizioni resi leverans frakt returer
fragt returnering toimitus palautus dostawa wysyłka zwroty skip content inhalt inhoud contenu contenido contenuto
innehåll indhold innhold sisältö treść
"""


# ---------------------------------------------------------------------------
# accent- and case-tolerant phrase patterns
# ---------------------------------------------------------------------------

_CLS = {"a": "aàáâãäåāąă", "c": "cçćč", "e": "eèéêëēęě", "i": "iìíîïīı", "l": "lł", "n": "nñńň",
        "o": "oòóôõöøōő", "s": "sśšş", "u": "uùúûüūů", "y": "yýÿ", "z": "zźżž", "r": "rř", "d": "dď", "t": "tť",
        "g": "gğ"}
_SPECIAL = {"ø": "o", "ł": "l", "đ": "d", "ı": "i"}


def _base(s: str) -> str:
    """Lower case, accents dropped; æ -> ae, œ -> oe, ß -> ss, ø -> o, ł -> l."""
    s = str(s or "").lower().replace("æ", "ae").replace("œ", "oe").replace("ß", "ss")
    s = "".join(_SPECIAL.get(c, c) for c in s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _phrase_rx(p: str) -> str:
    """One phrase as a regex over the ORIGINAL text: case- and accent-tolerant, flexible spaces and hyphens,
    "#" a number, "." optional."""
    b = _base(p.strip())
    out, i = [], 0
    while i < len(b):
        two = b[i:i + 2]
        if two in ("ae", "oe", "ue", "ss"):
            out.append({"ae": "(?:ae|æ|ä)", "oe": "(?:oe|œ|ø|ö)", "ue": "(?:ue|ü)", "ss": "(?:ss|ß)"}[two])
            i += 2
            continue
        ch = b[i]
        if ch == " ":
            out.append(r"[\s ]+")
        elif ch == "-":
            out.append(r"[\s\-‐‑–—]*")
        elif ch == ".":
            out.append(r"\.?")
        elif ch == "#":
            out.append(r"\d+")
        elif ch in _CLS:
            out.append("[" + _CLS[ch] + "]")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _alt(phrases: Iterable[str]) -> str:
    ps = sorted({p.strip() for p in phrases if p.strip()}, key=lambda p: (-len(p), p))
    return "(?:" + "|".join(_phrase_rx(p) for p in ps) + ")"


def _split(block: str) -> List[str]:
    return [p.strip() for p in re.split(r"[|\n]", block) if p.strip()]


_NOT_ALNUM_AFTER = r"(?![^\W_])"
_NOT_ALNUM_BEFORE = r"(?<![^\W_])"
#: what may follow a stripped prefix: a separator, else whitespace (a hyphen only with spaces round it:
#: "Tipp-Ex" is a name, "Tipp - Ex" is not)
_AFTER_PREFIX = r"(?:\s*[:!|•·»›>]+\s*|\s+[-–—/]\s+|\s+)"
#: what may precede a stripped tail
_BEFORE_SUFFIX = r"(?:\s*[|•·»›,:(]+\s*|\s+[-–—/]\s+|\s+)"
_TAIL_END = r"[\s.!:)*]*$"

_CTA_P = _alt(_split(CTA_PREFIX))
_CTA_S = _alt(_split(CTA_SUFFIX))
_BADGE = _alt(_split(BADGE))
_BADGE_AMB = _alt(_split(BADGE_AMBIGUOUS))
_STOCK = _alt(_split(STOCK))
_MORE = _alt(_split(MORE_TAIL))
_PLABEL = _alt(_split(PRICE_LABEL))
_PLABEL_AMB = _alt(_split(PRICE_LABEL_AMBIGUOUS))

RX_CTA_PREFIX = re.compile(r"^\s*(" + _CTA_P + ")" + _NOT_ALNUM_AFTER + _AFTER_PREFIX, re.I)
RX_CTA_SUFFIX = re.compile(_BEFORE_SUFFIX + "(" + _CTA_S + ")" + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
RX_BADGE = re.compile(r"^\s*(" + _BADGE + ")" + _NOT_ALNUM_AFTER + _AFTER_PREFIX, re.I)
RX_BADGE_AMB = re.compile(r"^\s*(" + _BADGE_AMB + ")" + _NOT_ALNUM_AFTER + r"(\s*[:!|•·»›>]+\s*|\s+[-–—/]\s+|\s+)",
                          re.I)
RX_STOCK_PREFIX = re.compile(r"^\s*(" + _STOCK + ")" + _NOT_ALNUM_AFTER + r"(?:[.!])?" + _AFTER_PREFIX, re.I)
RX_STOCK_SUFFIX = re.compile(_BEFORE_SUFFIX + "(" + _STOCK + ")" + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
RX_MORE_PHRASE = re.compile(_BEFORE_SUFFIX + "(" + _MORE + ")" + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
RX_MORE_PLUS = re.compile(r"\s*(\+\s*(?:\d+(?:\s+(?:" + _MORE_WORDS + r"))?(?:\s+(?:" + _VARIANT_WORDS + r"))?|(?:"
                          + _MORE_WORDS + r")(?:\s+(?:" + _VARIANT_WORDS + r"))?|(?:" + _VARIANT_WORDS + r")))"
                          + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
RX_PLABEL_PREFIX = re.compile(r"^\s*(" + _PLABEL + ")" + _NOT_ALNUM_AFTER + _AFTER_PREFIX, re.I)
RX_PLABEL_SUFFIX = re.compile(_BEFORE_SUFFIX + "(" + _PLABEL + ")" + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
#: an ambiguous label only before a colon
RX_PLABEL_AMB_COLON_PREFIX = re.compile(r"^\s*(" + _PLABEL_AMB + r")\s*:\s*", re.I)
RX_PLABEL_AMB_COLON_SUFFIX = re.compile(_BEFORE_SUFFIX + "(" + _PLABEL_AMB + r")\s*:" + _TAIL_END, re.I)

_CUR = r"(?:€|eur|euro|kr\.?|sek|nok|dkk|zł|zl|pln|chf|fr\.|£|gbp|\$|usd|:-|,-|\.-)"
_NUM = r"\d{1,3}(?:[ . ']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_PRICE = (r"(?:(?:" + _NUM + r")\s?" + _CUR + r"(?![^\W_])|(?:€|£|\$|eur|chf|usd|gbp)\s?(?:" + _NUM + r")"
          r"(?:\s?" + _CUR + r")?)")
_UNIT = r"(?:kg|g|l|ml|cl|dl|m|m2|m²|cm|stk|st|stück|stuk|pcs|piece|kpl|szt|kappale)"
#: a price with its label and its unit price ("ab 12,99 €", "statt 19,99 €", "12,99 €/kg", "(1 kg = 5,99 €)")
RX_PRICE = re.compile(
    r"(?:(?:" + _PLABEL + "|" + _PLABEL_AMB + r")\s*:?\s*)?" + _PRICE + r"(?:\s*\*)?(?:\s*/\s*(?:\d+(?:[.,]\d+)?\s*)?"
    + _UNIT + r"\b)?", re.I)
RX_UNIT_PRICE_BRACKET = re.compile(r"\(\s*(?:\d+(?:[.,]\d+)?\s*)?" + _UNIT + r"\s*=\s*" + _PRICE + r"\s*\)", re.I)
RX_DISCOUNT = re.compile(
    r"(?:^|(?<=\s))[-–−]\s?\d{1,2}(?:[.,]\d)?\s?%(?=\s|$)|"
    r"\b(?:save|spar|spare|sparen|économisez|ahorra|risparmia|säästä|oszczędź|bespaar|bis zu|up to|jusqu'à|hasta|fino al)"
    r"\s+(?:-\s?)?\d{1,2}\s?%|"
    r"\b\d{1,2}\s?%\s?(?:off|rabatt|korting|rabat|alennus|sconto|descuento|de remise|de réduction|reduziert|rabattert)\b",
    re.I)
_SIZE_TOKEN = r"(?:x{0,3}s|m|x{0,4}l|[2-6]xl|\d{2,3}(?:[.,]5)?(?:/\d{2,3})?|one size|onesize|einheitsgröße|uni)"
RX_SIZE_LABEL = re.compile(r"(?:\s*[|,:(]\s*|\s+[-–—]\s+|\s+)(?:" + SIZE_LABEL + r")\s*:?\s*" + _SIZE_TOKEN +
                           r"(?:\s*[,/|\-–]?\s*" + _SIZE_TOKEN + r")*" + _NOT_ALNUM_AFTER + _TAIL_END, re.I)
#: a trailing size selector without a label: three or more size tokens ("... XS S M L XL", "... 36 37 38 39")
RX_SIZE_RUN = re.compile(r"(?:\s+(?:xxs|xs|s|m|l|xl|xxl|xxxl|[2-6]xl)){3,}\s*$|(?:\s+(?:[1-5]\d)(?:[.,]5)?){3,}\s*$", re.I)

RX_BULLET_LEAD = re.compile(r"^\s*(?:[•·●▪■□◆◇►▶▸»›→✓✔✗★☆✱*]+|[-–—]\s)\s*")
RX_BULLET_TAIL = re.compile(r"\s+[•·●▪■□◆◇►▶▸✓✔★☆]+\s*$")
#: a leading list position: "7 X", "7. X", "7) X", "7: X", "7 - X", "#7 X", "Nr. 7 X", "No. 7 X"
RX_POSITION = re.compile(r"^\s*(#\s?|(?:nr|no|n[°º])\.?\s?)?(\d{1,3})(?:\s*([.):])\s*|\s+[-–—]\s+|\s+)(?=\S)", re.I)
#: a number followed by these is a count or a size, never a list position ("50 Stuks", "6 Kvar", "12 x")
_COUNT_AFTER = re.compile(r"^(?:x\b|×|kg\b|g\b|ml\b|cl\b|l\b|cm\b|mm\b|m\b|stk|st\b|stück|stuks|stuk|pcs|pieces|pack|pak|"
                          r"er\b|teile|delig|deler|dele|osaa|szt|pz|pezzi|piezas|pièces|kvar|igjen|tilbage|left|"
                          r"jäljellä|%)", re.I)


def _tokens(s: str) -> List[str]:
    return re.findall(r"[0-9a-z]+", _base(s))


def _word_set(*blocks: str) -> frozenset:
    out = set()
    for b in blocks:
        for p in _split(b):
            out.update(t for t in _tokens(p) if not t.isdigit())
    return frozenset(out)


#: the words a GENERIC value is made of: function words, the lexicon's UI / badge / stock / price / tail
#: words, months and weekdays, site chrome and image placeholders
GENERIC = _word_set(FUNCTION_WORDS.replace(" ", "|"), CTA_PREFIX, CTA_SUFFIX, BADGE, BADGE_AMBIGUOUS, STOCK, MORE_TAIL,
                    PRICE_LABEL, PRICE_LABEL_AMBIGUOUS, MONTHS.replace(" ", "|"), CHROME_WORDS.replace(" ", "|"),
                    _VARIANT_WORDS.replace("?", "").replace("(", "").replace(")", ""), _MORE_WORDS)


# ---------------------------------------------------------------------------
# site names
# ---------------------------------------------------------------------------

def _compact(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", _base(str(s or "")).replace("&", "and"))


def _host_label(host: str) -> str:
    """The registrable domain's first label ("www.milan-spiele.de" -> "milan-spiele")."""
    from anyuse.execution.web_scope import registrable
    h = str(host or "").lower().strip().strip(".")
    if not h:
        return ""
    try:
        reg = registrable(h)
    except Exception:                                  # noqa: BLE001 - a malformed host: its first label
        reg = h
    return reg.split(".")[0]


def site_names_of(host: str = "", title: str = "", og_site_name: str = "") -> List[str]:
    """The site's own names: ``og:site_name``; the page title's segments (split at | - – — · : ») that ARE the
    host's name (compared letters only: "Milan Spiele" = milan-spiele, "Burgon & Ball" = burgonandball) or
    start with it ("Magasin du Nord" on magasin.dk); the host's label with spaces for hyphens; the registrable
    domain itself ("bol.com"). Longest first."""
    out: List[str] = []
    label = _host_label(host)
    cl = _compact(label)

    def add(n: str) -> None:
        n = " ".join(str(n or "").split()).strip(" -|:·»")
        if n and len(_compact(n)) >= 3 and n.lower() not in {x.lower() for x in out}:
            out.append(n)
    if og_site_name and len(str(og_site_name).split()) <= 6:
        add(og_site_name)
    for seg in re.split(r"\s+[|\-–—·:»/]\s+|\s*[|·»]\s*", str(title or "")):
        cs = _compact(seg)
        if cl and cs and len(seg.split()) <= 5 and (cs == cl or (cs.startswith(cl) and len(cl) >= 4) or
                                                     (cl.startswith(cs) and len(cs) >= 4)):
            add(seg)
    if label:
        add(label.replace("-", " "))
        if host:
            from anyuse.execution.web_scope import registrable
            try:
                add(registrable(str(host).lower().strip(".")))
            except Exception:                          # noqa: BLE001
                pass
    return sorted(out, key=lambda n: (-len(n), n))


def _name_rx(name: str) -> str:
    """A site name as a regex: its words joined by optional spaces / hyphens / dots ("Milan-Spiele",
    "Milan Spiele", "MILANSPIELE"); "&" or "and"."""
    words = re.findall(r"[^\W_]+|&", str(name))
    parts = []
    for w in words:
        if w == "&" or w.lower() in ("and", "und", "en", "et", "og", "och"):
            parts.append(r"(?:&|and|und|en|et|og|och)")
        else:
            parts.append(_phrase_rx(w))
    return r"[\s\-‐.]*".join(parts)


# ---------------------------------------------------------------------------
# the cleaning
# ---------------------------------------------------------------------------

@dataclass
class Cleaned:
    """One value before and after cleaning; ``cleaned`` is "" when the value was dropped."""
    original: str
    cleaned: str
    dropped: bool = False
    why: str = ""                            # "" | "empty" | "generic: ..."
    steps: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.dropped or self.cleaned != self.original

    def to_dict(self) -> Dict[str, Any]:
        return dict(asdict(self), changed=self.changed)


def _norm(s: str) -> str:
    s = _html.unescape(str(s or ""))
    s = re.sub(r"[​-‏⁠﻿­]", "", s)
    return " ".join(s.replace(" ", " ").split())


def _tidy(s: str) -> str:
    """Leftovers of a strip: empty brackets, edge separators, doubled spaces."""
    for _ in range(3):
        t = re.sub(r"\(\s*[^\w()]*\s*\)|\[\s*[^\w\[\]]*\s*\]", " ", s)
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r"^(?:[-–—|·•,:;/]+\s*)+", "", t)
        t = re.sub(r"(?:\s+[-–—|·•,:;/]+|[|·•,:;/])+$", "", t).strip()
        if t == s:
            break
        s = t
    return s


def numbered_siblings(siblings: Sequence[str]) -> bool:
    """Is the list numbered? >= 2 siblings and >= 60 % of them start with a list position whose numbers are
    distinct (a count or a size after the number - "50 Stuks", "6 Kvar" - is no position)."""
    nums = []
    sib = [s for s in siblings if str(s or "").strip()]
    for s in sib:
        m = RX_POSITION.match(_norm(s))
        if m and not _COUNT_AFTER.match(_norm(s)[m.end():]):
            nums.append(int(m.group(2)))
    return len(nums) >= 2 and len(nums) >= 0.6 * len(sib) and len(set(nums)) == len(nums)


def _ambiguous_badge_ok(m: "re.Match[str]", rest: str) -> bool:
    """An ambiguous badge goes only when set off by a separator, or written in capitals before a word that
    is not ("NEW Nike Air Max" yes; "NEW BALANCE 574", "New Balance 574", "Top Gun" no)."""
    if m.group(2).strip():
        return True
    nxt = rest.split()[0] if rest.split() else ""
    word = m.group(1)
    return word.isupper() and len(word) >= 2 and bool(nxt) and not nxt.isupper()


def _compacts(s: str) -> set:
    """A string's letters-only forms: umlauts as plain vowels and as "ae" / "oe" / "ue" ("nähPark" = naehpark)."""
    raw = str(s or "").lower()
    out = {_compact(raw)}
    out.add(_compact(raw.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("å", "aa")
                     .replace("ø", "oe")))
    return {x for x in out if x}


def _generic(s: str, names: Sequence[str] = ()) -> str:
    """Why ``s`` is generic ("" = it is not): no word left; the WHOLE string is the site's own name ("nähPark"
    on www.naehpark.com - a word of the name is not: "Android" on www.androidplanet.nl stays); or every word is a
    function, UI, stock, price, badge, month, weekday or chrome word."""
    toks = [t for t in _tokens(s) if not t.isdigit() and len(t) >= 2]
    if not toks:
        return "no word left"
    if names and _compacts(s) & {c for n in names for c in _compacts(n)}:
        return "only the site's name"
    if all(t in GENERIC for t in toks):
        return "only function, UI, stock, price, month or chrome words"
    return ""


def clean_value(value: Any, site_names: Sequence[str] = (), siblings: Sequence[str] = ()) -> Cleaned:
    """The deterministic cleaning (module docstring). ``site_names``: the page's own site names
    (:func:`site_names_of`); ``siblings``: the other titles of the same list (the list-position rule)."""
    orig = str(value if value is not None else "")
    s = _norm(orig)
    steps: List[str] = []
    names = [n for n in site_names if str(n or "").strip()]
    name_alt = "(?:" + "|".join(_name_rx(n) for n in sorted(names, key=len, reverse=True)) + ")" if names else ""
    site_suffix = re.compile(r"(?:\s*[|·•»–—:]\s*|\s+[-/]\s+|\s+(?:" + SITE_PREP + r")\s+)(" + name_alt + ")"
                             + _NOT_ALNUM_AFTER + r"[\s.!]*$", re.I) if names else None
    site_prefix = re.compile(r"^\s*(" + name_alt + ")" + _NOT_ALNUM_AFTER + r"(?:\s*[|·•»–—:]\s*|\s+[-/]\s+)",
                             re.I) if names else None
    numbered = numbered_siblings(siblings)
    position_done = False

    def cut(m: "re.Match[str]", kind: str, rest: str) -> str:
        steps.append(f"{kind}: {m.group(0).strip()[:60]}")
        return rest

    for _ in range(MAX_PASSES):
        before = s
        # 1. bullets
        m = RX_BULLET_LEAD.match(s)
        if m and s[m.end():].strip():
            s = cut(m, "bullet", s[m.end():])
        m = RX_BULLET_TAIL.search(s)
        if m and s[:m.start()].strip():
            s = cut(m, "bullet", s[:m.start()])
        # 2. a list position (a numbered list only)
        m = RX_POSITION.match(s)
        if m and numbered and not position_done and not _COUNT_AFTER.match(s[m.end():]) and s[m.end():].strip():
            position_done = True                       # once: "3 7 Wonders" -> "7 Wonders"
            s = cut(m, "list position", s[m.end():])
        # 6. the site's own name (before the CTA rules: "| Stockmann" is a tail)
        if site_suffix is not None:
            m = site_suffix.search(s)
            if m and s[:m.start()].strip():
                s = cut(m, "site name", s[:m.start()])
            m = site_prefix.match(s)
            if m and s[m.end():].strip():
                s = cut(m, "site name", s[m.end():])
        # 7. prices, discounts, unit prices
        for rx, kind in ((RX_UNIT_PRICE_BRACKET, "unit price"), (RX_PRICE, "price"), (RX_DISCOUNT, "discount")):
            m = rx.search(s)
            while m and (s[:m.start()] + s[m.end():]).strip():
                s = cut(m, kind, (s[:m.start()] + " " + s[m.end():]).strip())
                m = rx.search(s)
        # 7. sizes glued to a name
        for rx, kind in ((RX_SIZE_LABEL, "size label"), (RX_SIZE_RUN, "size selector")):
            m = rx.search(s)
            if m and s[:m.start()].strip():
                s = cut(m, kind, s[:m.start()])
        # 3-5, 7. prefixes: CTA, stock, badges, price labels
        for rx, kind in ((RX_CTA_PREFIX, "cta"), (RX_STOCK_PREFIX, "stock"), (RX_BADGE, "badge"),
                         (RX_PLABEL_PREFIX, "price label"), (RX_PLABEL_AMB_COLON_PREFIX, "price label")):
            m = rx.match(s)
            if m:
                s = cut(m, kind, s[m.end():])
        m = RX_BADGE_AMB.match(s)
        if m and s[m.end():].strip() and _ambiguous_badge_ok(m, s[m.end():]):
            s = cut(m, "badge", s[m.end():])
        # tails: CTA, stock, "+ more", price labels
        for rx, kind in ((RX_CTA_SUFFIX, "cta"), (RX_STOCK_SUFFIX, "stock"), (RX_MORE_PLUS, "more tail"),
                         (RX_MORE_PHRASE, "more tail"), (RX_PLABEL_SUFFIX, "price label"),
                         (RX_PLABEL_AMB_COLON_SUFFIX, "price label")):
            m = rx.search(s)
            if m:
                s = cut(m, kind, s[:m.start()])
        s = _tidy(s)
        if s == before:
            break
    s = _tidy(s)
    if not s:
        return Cleaned(orig, "", True, "empty", steps)
    g = _generic(s, names)
    if g:
        return Cleaned(orig, "", True, f"generic: {g}" + (f" ({s[:60]!r})" if s != _norm(orig) else ""), steps)
    return Cleaned(orig, s, False, "", steps)


def clean_values(values: Sequence[str], host: str = "", title: str = "", og_site_name: str = "",
                 siblings: Sequence[str] = ()) -> List[Cleaned]:
    """:func:`clean_value` for every value of one page (the site names from the host, the title, og:site_name;
    the siblings: the page's list titles)."""
    names = site_names_of(host, title, og_site_name)
    return [clean_value(v, names, siblings) for v in values]


def og_site_name(read: Optional[Dict[str, Any]]) -> str:
    """``og:site_name`` from a page read (its meta tags, else the DOM)."""
    r = read or {}
    meta = r.get("meta") or {}
    if isinstance(meta, dict) and meta.get("og:site_name"):
        return str(meta["og:site_name"])
    m = re.search(r"(?is)<meta[^>]+property\s*=\s*[\"']og:site_name[\"'][^>]*content\s*=\s*[\"']([^\"']{1,120})",
                  str(r.get("dom") or "")[:200000])
    if not m:
        m = re.search(r"(?is)<meta[^>]+content\s*=\s*[\"']([^\"']{1,120})[\"'][^>]*property\s*=\s*[\"']og:site_name",
                      str(r.get("dom") or "")[:200000])
    return _html.unescape(m.group(1)).strip() if m else ""
