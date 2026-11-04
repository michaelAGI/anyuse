"""ITEM-level proof values (PURE).

The site's navigation CATEGORY names make poor proof values ("Batting", "Danske
bøger", "Family + Relationships"); the proof's list contract asks that a result ITEM
names the value, which product and article titles rarely do for a category word.
This module draws single words of the site's own ITEM titles (the product names of its
schema.org JSON-LD, its item clusters, its content links) on the start page, before
any arm contacts the site; they are never tuned on validation (``proof_split``)."""
from __future__ import annotations

import random
import re
import urllib.parse
from collections import Counter
from typing import Dict, List, Sequence, Tuple

import web_arch2_live as L2
from anyuse.execution import discover as D
from anyuse.execution import web_arch as W

SEED = "anyuse-web-arch3-2026-11-03"



#: words that never make an item-level proof value (colours, sizes, marketing, units)
GENERIC = frozenset("""black white blue red green grey gray pink brown beige navy yellow orange purple silver gold
new sale offer offers pack set sets size small medium large extra mini maxi women men kids ladies mens womens
unisex free best top edition collection original classic premium deal deals shop online price prices only
schwarz weiss blau grun grau rosa braun neu angebot damen herren kinder noir blanc bleu rouge vert gris rose
femme homme enfant nouveau zwart wit blauw rood groen grijs dames heren kinderen nieuw svart vit bla rod gron
dam herr barn sort hvid hvit musta valkoinen sininen punainen czarny bialy nero bianco rosso negro blanco rojo
with from your pour avec sans ohne fur und oder voor met och med fran fra dla oraz per para della delle degli
recipe recipes rezept rezepte recette recettes recept recepten opskrift oppskrift resepti przepis ricetta receta
article articles blog guide guides review reviews news video videos
infos info mehr more service services beratung kostenfreie kostenlos gratis sortiment lieferservice lieferservices
bestell angebote angeboten aktion aktionen bestseller bestsellers neuheit neuheiten highlight highlights tipp tipps
exklusiv exclusive discover shop now view read more learn weitere alle entdecken jetzt hier zum zur nach beim
decouvrir voir tous toutes nouveaute nouveautes ontdek bekijk meer alles upptack visa fler oppdag opdag katso lisaa
zobacz wiecej scopri tutti descubre todos ofertas oferta promo promotion promotions rabatt rabatte reduziert
bundle bundles gift gifts geschenk geschenke cadeau regalo lahja prezent
about after again also back been before being best better both could does done down each even every first from
good great have here high into just know last like little long look made make many most much must need never next
only other over part people place play right same should since some still such take than that their them then there
these they thing things this those through time today under until very want well were what when where which while
will with within without work would year years your yours shopping continue business program programme user users
taste order ordered orders week weeks night nights days hours minutes minute
aber alle allem allen aller alles also andere anderen auch bereits bitte dann dass denn deine diese dieser dieses doch
dort durch eine einem einen einer eines etwas euch ganz gerne gibt haben hast hatte heute hier ihre immer jede jeden
jeder jetzt kann kein keine koennen konnen machen macht mein meine muss nicht noch nur oder ohne rein schau schon sehr
selber sein seine seit sich sind soll sowie ueber uber unser unsere unter viel viele voll wann warum weil weiter wenn
werden wieder will wird wurde zwischen fragen antworten kochen backen jahre jahr minuten stunden tage woche
aan alle alleen andere beter bijna daar door eens elke enkele geen goed goede haar heel hier hoe jaar jouw kunnen maar
meer mijn naar niet onze ook over omdat vanaf veel vers voor waar want welke wordt worden zelf zijn zoals maat
aussi autre autres avant avec bien cette chez comme dans depuis donc elle elles encore entre fait faire leur leurs
mais meme mois moins notre nous plus pour quand sans selon sont sous tout toute toutes tres votre vous annee
ahora algo antes cada como con desde donde ellos esta este esto estos hacer hasta mismo mucho muchos nada otro otros
para pero poco porque puede sobre solo tambien tiene todo todos usted anche ancora come con cosa dalla della delle
dopo dove essere fare fino loro meno molto nella nelle ogni oggi ogni perche piu poco prima quando quanto quella
quello questa questo sempre senza sono sotto stato tanto testa tutti tutto alla dell allt alla andra annan bara
blir eller efter finns fler flera fram genom hade hela inom inte kommer kunna mera mest mycket nagon nagot nara
over precis sedan sina sitt skall ska spara till under vara varje vilka vill ocksa alle andre bare blev blive efter
eller flere godt hele hvad hvor ikke inden kunne lige meget mere naar noget nogle over sine skal ogsa vores bruk prov
hvordan hvis ingen noen noe skal sine ogsa kanskje aina ehka etta ilman joka jotka kaikki koska kuin mika mita myos
niin nyt ovat paljon sitten tama vain voit wiecej bardzo gdzie jako jest jestem juz kiedy mamy moze nawet przez tylko
wszystko wszystkie""".split())


def item_titles(body: str, url: str, limit: int = 80) -> List[str]:
    """The item titles a site's start page shows (PURE): the product names of its
    schema.org JSON-LD, then the link texts of its item clusters (``discover``'s detail
    candidates), then same-site content links of 3-14 words; never site chrome, a
    gated control, a file or an image-only link."""
    out: List[str] = []
    seen = set()

    def add(t: str) -> None:
        t = " ".join(str(t or "").split())
        k = W.fold(t)
        if not t or k in seen or len(t) > 160:
            return
        seen.add(k)
        out.append(t)
    blocks = re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body or "")
    for f in W.product_facts(blocks):
        add(f.get("name") or "")
    scan = D.scan_page(body or "", url)
    host = D.host_of(url)
    try:
        clusters = D.link_clusters(scan.links, (urllib.parse.urlsplit(url).netloc or host).lower())
        caps = D.detail_candidates(clusters)[:2]
        for cap in caps:
            cl = next((c for c in clusters if c.template == cap.template), None)
            for (_v, t) in (cl.members if cl is not None else []):
                if 2 <= len(W.tokens(t)) <= 14 and not W.forbidden_control(t, ""):
                    add(t)
    except Exception:                                  # noqa: BLE001 - the plain links below
        pass
    for ln in scan.links:
        t = " ".join((ln.text or "").split())
        if ln.chrome or D.host_of(ln.url) != host or W.asset_href(ln.url) or W.forbidden_control(t, ln.url):
            continue
        if 3 <= len(W.tokens(t)) <= 14:
            add(t)
        if len(out) >= limit:
            break
    return out[:limit]


_TITLE_PRICE = re.compile(r"(\d[\d .,]*\s?(€|eur|kr|sek|nok|dkk|zl|pln|chf|£|\$|:-))|((€|£|\$)\s?\d[\d.,]*)", re.I)


def scored_titles(body: str, url: str) -> List[Tuple[int, str]]:
    """(score, title): JSON-LD product names 3, item-cluster members 2, a content link
    with a price +2, with >= 4 words +1 (an item tile names a product and its price; a
    call to action is short)."""
    blocks = re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body or "")
    ld = {W.fold(f.get("name") or "") for f in W.product_facts(blocks) if f.get("name")}
    out = []
    for t in item_titles(body, url):
        k = W.fold(t)
        sc = 3 if k in ld else 0
        if sc == 0:
            sc = (2 if _TITLE_PRICE.search(t) else 0) + (1 if len(W.tokens(t)) >= 4 else 0)
        out.append((sc, t))
    return out


def item_terms(body: str, url: str, limit: int = 24) -> List[str]:
    """ITEM-level proof values (PURE) - single words of the site's own item titles
    (``item_titles``) that result titles contain: content words of >= 4 letters, not a
    number, not an interface or generic word, not the site's own name; words shared by
    several titles (but not by most of them) first, then the rest in page order. Navigation
    categories ("Batting", "Danske bøger") are words result items rarely
    name, so the proof's list contract failed on them."""
    scored = scored_titles(body, url)
    good = [t for sc, t in scored if sc >= 2]
    titles = good + [t for sc, t in scored if sc < 2]
    n_good = len(good)
    host_words = set(re.split(r"[.-]", D.host_of(url).lower()))
    df: Counter = Counter()
    first: Dict[str, Tuple[int, str]] = {}
    for i, t in enumerate(titles):
        words = []
        for raw in re.findall(r"[^\W\d_][^\W_]*", t):
            k = W.fold(raw)
            if len(k) < 4 or not k.isalpha() or k in GENERIC or k in L2.CHROME or k in W.STOPWORDS or \
                    k in host_words or k.startswith(L2.CHROME_STEMS):
                continue
            words.append((k, raw))
        for k, raw in dict(words).items():
            df[k] += 1
            first.setdefault(k, (i, raw))
    n = max(1, len(titles))
    ok = [k for k in df if df[k] <= max(2, n // 2)]
    # words of item tiles (a product with its price, a JSON-LD product) first, then the rest;
    # within each: shared by several titles first, then in page order
    tier = {k: (0 if first[k][0] < n_good else 1) for k in ok}
    ok.sort(key=lambda k: (tier[k], -min(df[k], 3), first[k][0]))
    return [first[k][1] for k in ok][:limit]


def draw_proof_values(site: str, terms: Sequence[str], search_value: str, k: int = 4, first_n: int = 12) -> List[str]:
    """Before any arm: ``k`` of the first ``first_n`` item terms that do not overlap the
    drawn search value (seeded per site): fitting, validation 1, validation 2, spare."""
    sv = set(W.content_tokens(search_value))
    pool = [t for t in terms[:first_n] if not (set(W.content_tokens(t)) & sv)]
    rng = random.Random(f"{SEED}:values:{site}")
    pick = pool[:]
    rng.shuffle(pick)
    return pick[:k]


