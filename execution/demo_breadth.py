"""Web tool version 10: BREADTH - three READ-ONLY workflow types beyond product lookup
and site search, each demonstrated ONCE by an agent (``demo_policy``) and compiled into a checked tool with NO model at
run time. PURE but for the async runner (the page is duck-typed, as in ``demo_tool``).

* ``filtered_search`` (a): search the input, then the demonstrated FILTERS / SORT ORDER on the result list (a price
  range, a category / brand / any labelled facet, in-stock only; sort by price or by date). Compiled as BOUND
  PARAMETERS: the URL parameters the refinement set (``refine.mode == "url"``: applied to the search's own result URL
  at run time), else the controls the demonstrator used (``"controls"``: the select option / the link / the checkbox
  found again by its label). Runtime checks: the list is sorted as asked (verified on the prices / dates the result
  CARDS show) and filtered as asked (the filter value in every result, or in the page's active-filter chips; a price
  range on every shown price; in-stock only: no result shown out of stock).
* ``paged_search`` (b): collect the top N results across result pages (the URL page parameter / path the
  demonstration's page 2 used, else its next-page link found again), stopping at N or at the last page. Runtime
  checks: no duplicates, page order kept, the site's stated total.
* ``visit_results`` (c): open the top K results of a search and read each product's fields (name, price,
  availability, URL) with the product-lookup reader (field sources compiled from the demonstration's opened pages).
  Runtime checks per item as a product lookup's (identity against the result's title, field sanity, a structured /
  visible CONFLICT, version 8's availability rule); with ``ANYUSE_ATTRIBUTE_VERIFY`` on, the independent verifier
  then checks each opened row on its own item page (``attribute_verify.verify_visit``: its identity against its card
  on the list page, its fields) and withholds a row it does not verify. The output is a TABLE with a per-item status.

What the demonstration applied and what a page states are read from the site's own pages first, in any language: a
sort's key from the order the refined page's cards satisfy, a price range from the prices they keep, the next page
from ``rel=next`` and the page number its URL increments, the stated total from the number beside the page's quoted
echo of the input, a card's date from its markup; the demonstrator's report next; the lexicons of 11 languages last,
matched as whole words.

Every navigation is a GET of the site's own pages or a click on a link / filter / sort / pagination control
(``web_arch.forbidden_control`` refuses carts, checkout, login, account; the traffic guard judges every request). A
check that cannot be verified is INCOMPLETE, a violated check a failure - never a silent answer.
"""
from __future__ import annotations

import math
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_compile as C
from anyuse.execution import web_arch as W

BREADTH = ("filtered_search", "paged_search", "visit_results")
#: results a filtered list answers
FILTER_TOP_K = 5
#: the result pages a paged run may read (its traffic counts against the site's caps)
MAX_PAGES = 5
#: the most results a paged run collects
MAX_N = 100
#: the most results a visit run opens
MAX_VISIT = 5
#: a sort / filter check needs this share of the listed results to show what it is verified on
VERIFY_SHARE = 0.6
#: the refinement steps a demonstration may take after its search
MAX_REFINE = 3


# ---------------------------------------------------------------------------
# the goals (the demonstrator's task; templated, no authoring)
# ---------------------------------------------------------------------------

def goal_text(workflow: str, value: str, k: int = 2) -> str:
    if workflow == "filtered_search":
        return (f"Search this website for \"{value}\" with the site's own search. On the results page, sort the "
                f"results by price, lowest first, with the page's own sort control (a drop-down or links); if the page "
                f"offers no price sort, sort by newest first; if it offers no sort at all, apply ONE filter it offers (a "
                f"brand, category, price range or in-stock only). Then report the top results (up to {FILTER_TOP_K}) in "
                f"the order the page lists them, with their titles, URLs and the price or date each shows, and the sort "
                f"order or filter you applied. If the search shows no results, report no_results.")
    if workflow == "paged_search":
        return (f"Search this website for \"{value}\" with the site's own search, then go to the NEXT page of the "
                f"results with the page's own pagination (the next-page link or the page number 2). On that second page "
                f"report its results (up to {FILTER_TOP_K}) in the order it lists them, with their titles and URLs, and "
                f"the total number of results if the site states one. If the search shows no results, report "
                f"no_results.")
    if workflow == "visit_results":
        return (f"Search this website for \"{value}\" with the site's own search, then open the top {k} results one "
                f"after the other (each result's own page) and report for each its name, price (with currency), "
                f"availability and page URL. Use \"read\" to carry what you read on a page (and the result URLs you "
                f"still need) to the next step. If the search shows no results, report no_results.")
    raise ValueError(f"unknown workflow {workflow!r}")


# ---------------------------------------------------------------------------
# the LAST rung: lexicons of the 11 languages (folded with ``demo_compile.fold9``), each matched as whole words or
# a word's start / end - never inside a word. The demonstration's own pages come first: the order its refined
# page's cards satisfy (``shown_orders``), a price range its cards keep (``range_kept``), the next-page link by
# ``rel=next`` or by the page number its URL increments (``next_link``), the count beside the page's quoted echo
# of the input (``stated_total``), the date a card's markup states (``markup_dates``)
# ---------------------------------------------------------------------------

#: sort labels, whole words: "sort" in its English and German forms only ("Sortland" is a place)
_SORT_LABEL = re.compile(r"\b(?:sort(?:s|ed|ing|ieren|ierung|iert|iere)?|order by|ordered by|reihenfolge|trier|tri|"
                         r"trie par|ordenar\w*|orden|ordina\w*|ordinamento|sorteer\w*|sortering|sortera|sorter|"
                         r"jarjest\w*|lajittel\w*|sortuj\w*|sortowanie)\b")
_SORT_PARAM = re.compile(r"(?<![a-z])(?:sort|order|orden|ordine|reihenfolge)|\btri\b|^o$|^dir$|^direction$|^srt$")
_PRICE_W = re.compile(r"\b(?:price|prices|preis\w*|prix|precio\w*|prezzo|prezzi|prijs\w*|pris\w*|hinta|hinnan|cena|ceny|"
                      r"ceny|cost)\b|(?<![a-z])price|price(?![a-z])")
#: the price words a tool before version 15 reads at run time ("price" anywhere in a word)
_PRICE_W14 = re.compile(r"\b(?:price|prices|preis\w*|prix|precio\w*|prezzo|prezzi|prijs\w*|pris\w*|hinta|hinnan|cena|"
                        r"ceny|ceny|cost)\b|price")
_ASC_W = re.compile(r"\b(?:asc|ascending|aufsteigend|low(?:est)? to high(?:est)?|lowest|cheapest|lowest first|"
                    r"gunstigst\w*|niedrigst\w*|billigst\w*|croissant\w*|moins cher|ascendente|menor a mayor|"
                    r"mas barat\w*|crescente|dal piu basso|piu economic\w*|oplopend|laag naar hoog|laagste|goedkoopst\w*|"
                    r"stigande|lagsta|billigaste|stigende|laveste|billigste|nouseva\w*|halvin\w*|rosnac\w*|"
                    r"od najtansz\w*|najtansz\w*|min|up)\b")
_DESC_W = re.compile(r"\b(?:desc|descending|absteigend|high(?:est)? to low(?:est)?|highest|most expensive|hochst\w*|"
                     r"teuerst\w*|decroissant\w*|plus cher|descendente|mayor a menor|mas car\w*|decrescente|"
                     r"dal piu alto|piu costos\w*|aflopend|hoog naar laag|hoogste|duurst\w*|fallande|hogsta|dyraste|"
                     r"faldende|synkende|hoyeste|hojeste|dyreste|laskeva\w*|kallein\w*|malejac\w*|od najdrozsz\w*|"
                     r"najdrozsz\w*|max|down)\b")
_NEW_W = re.compile(r"\b(?:newest|latest|most recent|recent|new|date|datum|neuest\w*|neuheit\w*|neu|zuletzt\w*|"
                    r"aktuellst\w*|nouveaut\w*|plus recent\w*|recents?|mas recient\w*|recientes?|nuev\w*|novedad\w*|"
                    r"fecha|piu recent\w*|novita|recenti|data|nieuwst\w*|nieuw|recentst\w*|nyast\w*|nyheter|senaste|"
                    r"nyeste|nyheder|uusin\w*|uutuu\w*|najnowsz\w*|nowosci|created|published)\b")
_OLD_W = re.compile(r"\b(?:oldest|altest\w*|plus ancien\w*|mas antigu\w*|piu vecch\w*|oudst\w*|aldsta|aeldste|eldste|"
                    r"vanhin\w*|najstarsz\w*)\b")
#: a filter parameter / label that is a PRICE RANGE (a word's start or end: "price_min", "maxprice")
_RANGE_WORDS = r"(?:price|preis|prix|precio|prezzo|prijs|pris|cena|hinta|range)"
_RANGE_PARAM = re.compile(r"(?<![a-z])" + _RANGE_WORDS + r"|" + _RANGE_WORDS + r"(?![a-z])|^min|^max|min$|max$|from$|"
                          r"^to$|^von$|^bis$")
#: one that is IN-STOCK ONLY: in a label, whole words ("stock", "lager"), words starting with a stem ("available",
#: "verfügbar"), "stock" ending a word ("instock"), and phrases - never a word that only contains one ("Stockholm");
#: in a parameter name, whose words run together, a part that holds one
_STOCK_WORDS = frozenset(("stock", "instock", "onstock", "lager", "lagernd", "lagerware", "voorraad", "vorratig"))
_STOCK_STEMS = ("avail", "verfugbar", "lieferbar", "disponib", "saatav", "dostep")
_STOCK_PHRASES = ("auf lager", "op voorraad", "pa lager", "en stock", "in stock", "on stock")
#: a control that REMOVES an active filter (a chip: "Bosch ×", "Filter entfernen")
_REMOVE_MARK = re.compile(r"[×✕✖✗╳]|\b(?:remove|clear|entfernen|loschen|zurucksetzen|supprimer|effacer|eliminar|quitar|"
                          r"rimuovi|cancella|verwijder\w*|wissen|ta bort|rensa|fjern|nollaa|poista|usun|wyczysc)\b|"
                          r"(?:^|\s)x$")
#: the next-page link's text (the last rung: ``rel=next``, the URL's page number and the link text p + 1 first)
_NEXT_TEXT = re.compile(r"^(?:next|next page|more results|weiter|nachste\w*( seite)?|vorwarts|suivant\w*|page suivante|"
                        r"siguiente|pagina siguiente|avanti|successiv\w*|pagina successiva|volgende( pagina)?|"
                        r"nasta( sida)?|naeste( side)?|neste( side)?|seuraava( sivu)?|nastepn\w*( strona)?|dalej|"
                        r"[›»>→]+|next\s*[›»>→]+|[›»>→]+\s*next)$")
#: the site's stated TOTAL of results ("1-24 of 123", "123 Treffer", "123 résultats", ...)
_TOTAL_OF = re.compile(r"\b\d[\d.,]*\s*[-–]\s*\d[\d.,]*\s+(?:of|von|sur|de|di|van|av|af|z|/)\s+(\d[\d.,\s]*\d|\d)\b")
_TOTAL_N = re.compile(r"(?<![\d.,])(\d[\d.,\s]*\d|\d)\s+(?:results?|treffer|ergebnisse?|suchergebnisse|produkte|artikel|"
                      r"resultats?|produits|articles|resultados|productos|articulos|risultati|prodotti|articoli|"
                      r"resultaten|producten|artikelen|traffar|resultat|produkter|artiklar|resultater|varer|tulosta|"
                      r"tuotetta|wynik\w*|produkt\w*|items|products|hits)\b")


def _f(s: Any) -> str:
    return C.fold9(s)


def names_stock(text: Any, identifier: bool = False) -> bool:
    """Does a control label name an in-stock filter (the lexicon, whole words)? With ``identifier`` - a parameter
    name, an identifier whose words run together ("instockonly", "lagerstatus", "hideoutofstock") - a part of the
    name that holds a word or a stem of the lexicon."""
    words = [w for w in re.split(r"[^a-z0-9]+", _f(re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or "")))) if w]
    if identifier:
        return any(x in w for w in words for x in (*_STOCK_WORDS, *_STOCK_STEMS))
    joined = f" {' '.join(words)} "
    return any(w in _STOCK_WORDS or w.startswith(_STOCK_STEMS) for w in words) or \
        any(f" {p} " in joined for p in _STOCK_PHRASES)


def sort_key(*texts: Any, v15: bool = True) -> str:
    """The sort ORDER a control label / parameter value names: "price_asc", "price_desc", "date_desc", "date_asc", or
    "" (another order: relevance, name, rating - not verifiable on the cards). ``v15`` False: a tool before version
    15, whose run reads "price" anywhere in a word (``_PRICE_W14``)."""
    t = " ".join(_f(re.sub(r"([a-z])([A-Z])", r"\1 \2", str(x or ""))).replace("_", " ").replace("-", " ")
                 .replace(".", " ").replace("|", " ").replace(":", " ") for x in texts)
    if (_PRICE_W if v15 else _PRICE_W14).search(t):
        return "price_desc" if _DESC_W.search(t) and not _ASC_W.search(t) else "price_asc"
    if _OLD_W.search(t):
        return "date_asc"
    if _NEW_W.search(t):
        return "date_asc" if (_ASC_W.search(t) and re.search(r"\basc\b", t)) else "date_desc"
    return ""


def is_sort(name: Any, label: Any = "") -> bool:
    """Does a parameter name / control label name a SORT order? A name's camelCase parts are words of their own
    ("productSort", "listOrder")."""
    return bool(_SORT_PARAM.search(_f(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))))) or \
        bool(_SORT_LABEL.search(_f(label)))


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def clean_label(t: Any) -> str:
    """A control's label without a count ("Bosch (12)", "Bosch 12") or a remove mark."""
    s = " ".join(str(t or "").split())
    s = re.sub(r"\s*[\(\[]\s*\d[\d.,]*\s*[\)\]]\s*$", "", s)
    s = re.sub(r"\s*[×✕✖✗╳]\s*$", "", s)
    return s.strip(" :-")


def classify_param(k: str, v: str, label: str = "") -> Dict[str, Any]:
    """One refinement parameter (``k=v``, the control's label): a SORT (``key``), a PRICE RANGE (``min`` / ``max``), an
    IN-STOCK filter, or a FACET (a labelled value checked on the results / the active-filter chips)."""
    fk, fv, fl = _f(k), _f(urllib.parse.unquote_plus(str(v or ""))), _f(label)
    if is_sort(k, label) or (sort_key(v) and not label):
        return {"kind": "sort", "param": k, "value": v, "key": sort_key(v, label), "label": clean_label(label)}
    nums = [x for x in re.split(r"[-_:;,~]|\.\.|\bto\b|\bbis\b", fv) if x.strip()]
    if _RANGE_PARAM.search(fk) and nums and all(_num(x) is not None for x in nums):
        lo = hi = None
        if len(nums) >= 2:
            lo, hi = _num(nums[0]), _num(nums[1])
        elif re.search(r"max|to$|bis|high|upper", fk):
            hi = _num(nums[0])
        else:
            lo = _num(nums[0])
        return {"kind": "price_range", "param": k, "value": v, "min": lo, "max": hi, "label": clean_label(label)}
    if names_stock(k, identifier=True) or (names_stock(label) and not fv.isdigit() and len(fl) <= 40):
        return {"kind": "availability", "param": k, "value": v, "label": clean_label(label)}
    return {"kind": "facet", "param": k, "value": v, "label": clean_label(label) or
            urllib.parse.unquote_plus(str(v or "")).replace("-", " ").replace("_", " ")}


# ---------------------------------------------------------------------------
# what a result CARD shows: its current price, its date
# ---------------------------------------------------------------------------

#: a UNIT / base price after a card's price ("/ kg", "/ pr. Ltr.", "pro 100 g", "je Stück"): "/" alone is a separator
#: between two prices ("600,00 kr / 800,00 kr / pr. Ltr.")
_CARD_UNIT_AFTER = re.compile(r"^\s*\*?\s*(?:/|pro|per|pr\.?|je|par|al|por|a)\s*(?:\d+[.,]?\d*\s*)?(?:kg|g|gr|l|ltr|liter|litre|"
                              r"ml|cl|m|m2|m²|qm|lfm|stk|st|stuck|stück|piece|pcs?|unit|einheit|kpl|szt)\b\.?", re.I)


def card_price(text: Any) -> Optional[float]:
    """The price a result card shows: the LOWEST of its currency-marked prices that are not a unit / base price, a
    saving or a shipping cost (a sale price next to the regular one: the price the site sorts by)."""
    t = str(text or "")[:3000]
    vals = []
    prev = 0
    for m in C.TITLE_PRICE.finditer(t):
        # one price: thousands separated by '.', ',' or a narrow space - never a plain space ("DF333 79,50 €")
        v = W.parse_price(" ".join(m.group(0).split()))
        lab = W.fold(t[max(prev, m.start() - C.PRICE_LABEL_CHARS):m.start()])
        after = W.fold(t[m.end():m.end() + 24])
        prev = m.end()
        if _CARD_UNIT_AFTER.match(after) or C.RX_UNIT_LABEL.search(lab) or C.RX_OTHER_LABEL.search(lab) or \
                re.match(r"^\s*\*?\s*(?:versand|shipping|porto|frakt)", after):
            continue
        if v is not None and 0 < v < 10_000_000:
            vals.append(v)
    return min(vals) if vals else None


_MONTHS = {}
for _i, _names in enumerate((
        "january jan januar janvier enero gennaio januari tammikuu tammikuuta stycznia styczen ene gen jänner janner",
        "february feb februar fevrier febrero febbraio februari helmikuu helmikuuta lutego luty",
        "march mar marz maerz mars marzo maart maaliskuu maaliskuuta marca marzec mrz",
        "april apr avril abril aprile huhtikuu huhtikuuta kwietnia kwiecien abr",
        "may mai mayo maggio mei maj toukokuu toukokuuta maja",
        "june jun juni juin junio giugno kesakuu kesakuuta czerwca czerwiec",
        "july jul juli juillet julio luglio heinakuu heinakuuta lipca lipiec",
        "august aug aout agosto augustus augusti elokuu elokuuta sierpnia sierpien ago",
        "september sep sept septembre septiembre setiembre settembre syyskuu syyskuuta wrzesnia wrzesien set",
        "october oct oktober octobre octubre ottobre lokakuu lokakuuta pazdziernika pazdziernik okt ott",
        "november nov novembre noviembre marraskuu marraskuuta listopada listopad",
        "december dec dezember decembre diciembre dicembre desember joulukuu joulukuuta grudnia grudzien dez dic")):
    for _n in _names.split():
        _MONTHS[_n] = _i + 1
_MONTH_RX = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_NUM = re.compile(r"(?<!\d)(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})[./-](\d{1,2})[./-](\d{4}))(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})\.?\s+(?:de\s+)?(" + _MONTH_RX + r")\.?,?\s+(?:de\s+)?(\d{4})(?!\d)")
_DATE_MDY = re.compile(r"\b(" + _MONTH_RX + r")\.?\s+(\d{1,2}),?\s+(\d{4})(?!\d)")


def card_date(text: Any, v15: bool = True) -> Optional[Tuple[int, int, int]]:
    """The first DATE a result card's text shows (year, month, day): a numeric date first, in any language -
    "2025-03-12", "12.03.2025", "12/03/2025" -, and only where the card shows none, a date with a month name of the 11
    languages (folded): "12. März 2025", "12 March 2025", "March 12, 2025", "12 de marzo de 2025", "12 maart 2025";
    None when it shows none (a relative date - "2 days ago" - is not read). The date a card's markup states comes
    before both (``markup_dates``). ``v15`` False: a tool before version 15, whose run reads the date that comes
    first in the text, numeric or not (``_card_date14``)."""
    t = _f(text)
    if not v15:
        return _card_date14(t)
    for m in _DATE_NUM.finditer(t):
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
            return (y, mo, d)
    best: Optional[Tuple[int, Tuple[int, int, int]]] = None
    for m in _DATE_DMY.finditer(t):
        cand = (int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
        if 1 <= cand[2] <= 31:
            best = (m.start(), cand)
        break
    for m in _DATE_MDY.finditer(t):
        cand = (int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        if 1 <= cand[2] <= 31 and (best is None or m.start() < best[0]):
            best = (m.start(), cand)
        break
    return best[1] if best else None


def _card_date14(t: str) -> Optional[Tuple[int, int, int]]:
    """The date a tool before version 15 reads in a card's folded text ``t``: of the first numeric date, the first
    month-name date and the first month-first date, the one that comes first."""
    best: Optional[Tuple[int, Tuple[int, int, int]]] = None
    for m in _DATE_NUM.finditer(t):
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
            best = (m.start(), (y, mo, d))
            break
    for rx, order in ((_DATE_DMY, (3, 2, 1)), (_DATE_MDY, (3, 1, 2))):
        for m in rx.finditer(t):
            cand = (int(m.group(order[0])), _MONTHS[m.group(order[1])], int(m.group(order[2])))
            if 1 <= cand[2] <= 31 and (best is None or m.start() < best[0]):
                best = (m.start(), cand)
            break
    return best[1] if best else None


#: the schema.org date properties a card's markup may carry with a machine-readable value
_DATE_PROPS = ("datepublished", "datecreated", "datemodified", "uploaddate", "releasedate", "startdate")
_ISO_DATE = re.compile(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})")


def _machine_date(n: Any) -> Optional[Tuple[int, int, int]]:
    """The first machine-readable date inside node ``n`` (itself included): a ``<time datetime>``, or an element whose
    ``itemprop`` is a schema.org date property with a ``content`` / ``datetime`` value."""
    from anyuse.execution import demo_cards as K
    for m in [n] + list(K.walk(n)):
        if m.tag == "time" and m.attrs.get("datetime"):
            v = m.attrs.get("datetime")
        elif _f(m.attrs.get("itemprop")) in _DATE_PROPS:
            v = m.attrs.get("content") or m.attrs.get("datetime")
        else:
            continue
        g = _ISO_DATE.match(str(v or ""))
        if g and 1 <= int(g.group(2)) <= 12 and 1 <= int(g.group(3)) <= 31:
            return int(g.group(1)), int(g.group(2)), int(g.group(3))
    return None


def markup_dates(dom: Any, url: str, rows: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[int, int, int]]:
    """The date each listed result's CARD states in its markup, by structure and in any language: the nearest
    ancestor of the result's link (at most 8 up) that holds a machine-readable date (``_machine_date``) and no link
    to another listed result. ``{normalised URL: (year, month, day)}``; ``{}`` without a DOM."""
    if not dom or not rows:
        return {}
    from anyuse.execution import demo_cards as K
    root, base = K.parse(str(dom))
    want = {C._norm(str(r.get("url") or "")) for r in rows if r.get("url")}
    by: Dict[str, List[Any]] = {}
    for n, href in K.anchors(root, url, base):
        k = C._norm(href)
        if k in want:
            by.setdefault(k, []).append(n)
    out: Dict[str, Tuple[int, int, int]] = {}
    for k, nodes in by.items():
        for a in nodes:
            x, depth = a, 0
            while x is not None and depth < 8 and not str(x.tag).startswith("#") and k not in out:
                others = {C._norm(K.href_of(m, url, base)) for m in K.walk(x) if m.tag == "a" and "href" in m.attrs}
                if others & (want - {k}):
                    break
                d = _machine_date(x)
                if d:
                    out[k] = d
                x, depth = x.parent, depth + 1
            if k in out:
                break
    return out


def row_date(it: Dict[str, Any], dates: Optional[Dict[str, Tuple[int, int, int]]] = None, v15: bool = True
             ) -> Optional[Tuple[int, int, int]]:
    """A listed result's date: its card's markup (``markup_dates``) first, else its card's text (``card_date``;
    ``v15`` as there)."""
    return (dates or {}).get(C._norm(str(it.get("url") or ""))) or card_date(item_text(it), v15=v15)


def item_text(it: Dict[str, Any]) -> str:
    """What a listed result shows: its card's whole text (a version-10 card read), else its card context and title."""
    return " ".join(str(it.get(k) or "") for k in ("card_text", "ctx", "title") if it.get(k))


def _quote_open(ch: str) -> bool:
    """An opening quote mark: an ASCII quote, an opening bracket (Ps: 「 „) or a quotation mark of Unicode's initial
    or final class (Pi, Pf), which languages use on either side („…“ in German and Czech, ”…” in Swedish)."""
    import unicodedata
    return ch in "\"'`" or unicodedata.category(ch) in ("Pi", "Pf", "Ps")


def _quote_close(ch: str) -> bool:
    """A closing quote mark: an ASCII quote, a closing bracket (Pe) or a quotation mark of class Pi or Pf."""
    import unicodedata
    return ch in "\"'`" or unicodedata.category(ch) in ("Pi", "Pf", "Pe")


_WHOLE_NUMBER = re.compile(r"(?<![\d.,])\d{1,3}(?:[.,\u00a0\u202f ]\d{3})+(?!\d)|(?<![\d.,])\d+(?![\d.,]\d)")
_DATE_OR_TIME = re.compile(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}:\d{2}")


def _fold_keep_quotes(s: Any) -> str:
    """Casefolded, accents removed, whitespace collapsed - the quote marks kept (``web_arch.fold`` drops them)."""
    import unicodedata
    t = "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c))
    return " ".join(t.casefold().split())


def echo_total(text: Any, value: Any) -> Optional[int]:
    """The stated total beside the page's own ECHO of the typed input, by structure and in any language: a line
    (of the first 80, at most 160 characters) that holds the input between quote marks and exactly one other whole
    number - no price, no date ("'bisiklet' için 128 sonuç", "“xe đạp”: 57 kết quả") - or the shown range and the
    total of a pager summary (``_range_total``). None otherwise."""
    fv = _fold_keep_quotes(value)
    if not fv:
        return None
    for ln in str(text or "").split("\n")[:80]:
        fl = _fold_keep_quotes(ln)
        if not fl or len(fl) > 160 or C.TITLE_PRICE.search(ln) or _DATE_OR_TIME.search(fl):
            continue
        at = fl.find(fv)
        while at >= 0 and not (at > 0 and at + len(fv) < len(fl) and _quote_open(fl[at - 1]) and
                               _quote_close(fl[at + len(fv)])):
            at = fl.find(fv, at + 1)
        if at < 0:
            continue
        rest = fl[:at - 1] + " " + fl[at + len(fv) + 1:]
        nums = _WHOLE_NUMBER.findall(rest)
        if len(nums) == 3:
            got = _range_total(rest, nums)
            if got is not None:
                return got
        if len(nums) == 1:
            n = re.sub(r"\D", "", nums[0])
            if n.isdigit() and 0 < int(n) < 10_000_000:
                return int(n)
    return None


#: the range of the results a page shows ("1-24", "1 – 24") in front of its total
_SHOWN_RANGE = re.compile(r"(?<![\d.,])(\d{1,6})\s*[-–—]\s*(\d{1,6})(?!\d)")


def _range_total(rest: str, nums: Sequence[str]) -> Optional[int]:
    """The total of a pager summary beside the echo, in any language: the line's three numbers are the shown range
    ``a-b`` and then the total ``n``, with ``1 <= a <= b <= n`` ("Menampilkan 1-24 dari 340 hasil untuk ...")."""
    m = _SHOWN_RANGE.search(rest)
    vals = [re.sub(r"\D", "", x) for x in nums]
    if not m or not all(v.isdigit() for v in vals):
        return None
    a, b, n = (int(v) for v in vals)
    if (a, b) != (int(m.group(1)), int(m.group(2))) or not 1 <= a <= b <= n < 10_000_000:
        return None
    return n


def stated_total(text: Any, value: Any = None) -> Optional[int]:
    """The site's stated TOTAL of results in a result page's text: the number beside the page's quoted echo of
    ``value`` first (``echo_total``), then the lexicon's statements in the first 4,000 characters - "1-24 of 123",
    "1 – 24 von 123", "123 Treffer", "123 résultats", "123 wyników", ...; None when it states none."""
    got = echo_total(text, value) if value is not None else None
    if got is not None:
        return got
    t = _f(text)[:4000]
    for rx in (_TOTAL_OF, _TOTAL_N):
        m = rx.search(t)
        if m:
            n = re.sub(r"[.,\s]", "", m.group(1))
            if n.isdigit() and 0 < int(n) < 10_000_000:
                return int(n)
    return None


# ---------------------------------------------------------------------------
# the runtime checks (pure)
# ---------------------------------------------------------------------------

#: a list SHOWS an order when at least this many of its results show the value, not all the same
ORDER_MIN = 3
ORDERS = ("price_asc", "price_desc", "date_asc", "date_desc")


def _value_of(r: Dict[str, Any], key: str, dates: Optional[Dict[str, Tuple[int, int, int]]] = None,
              v15: bool = True) -> Any:
    return card_price(item_text(r)) if key.startswith("price") else row_date(r, dates, v15=v15)


def shown_orders(rows: Sequence[Dict[str, Any]], dates: Optional[Dict[str, Tuple[int, int, int]]] = None) -> set:
    """The sort orders a list's own cards satisfy: >= ``ORDER_MIN`` results show the price / date, not all equal, and
    every next one keeps the order (ties allowed)."""
    out = set()
    for key in ORDERS:
        vals = [v for v in (_value_of(r, key, dates) for r in rows) if v is not None]
        if len(vals) < ORDER_MIN or len(set(vals)) < 2:
            continue
        up = key.endswith("_asc")
        if all((b >= a) if up else (b <= a) for a, b in zip(vals, vals[1:])):
            out.add(key)
    return out


def refinement_orders(before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]],
                      dates_before: Optional[Dict[str, Any]] = None, dates_after: Optional[Dict[str, Any]] = None
                      ) -> set:
    """What the refinement sorted by, read on the demonstration's own pages: the ONE order the refined list shows,
    when the list before the refinement did not show it. A refined list that satisfies two orders at once (its
    prices descend and its dates happen to rise) tells nothing: the empty set."""
    after_orders = shown_orders(after, dates_after)
    if len(after_orders) != 1:
        return set()
    return after_orders - shown_orders(before, dates_before)


def range_kept(before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]], value: Any, label: Any = ""
               ) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """``(min, max)`` when a parameter value of one or two numbers is a PRICE RANGE by the demonstration's own pages:
    every price the refined cards show keeps within it (>= ``ORDER_MIN`` shown) and the list before had one outside.
    One number is the bound the cards keep (all at most it: the maximum; all at least it: the minimum), and a control
    with a ``label`` must show that number in it: a facet's numeric id ("brand=60" labelled "Bosch") bounds nothing.
    None otherwise."""
    nums = [x for x in re.split(r"[-_:;,~]|\.\.|\bto\b|\bbis\b", _f(urllib.parse.unquote_plus(str(value or ""))))
            if x.strip()]
    vals = [_num(x) for x in nums]
    if not vals or len(vals) > 2 or any(v is None for v in vals):
        return None
    if len(vals) == 1 and _f(label) and not any(
            _num(x) == vals[0] for x in re.findall(r"\d+(?:[.,]\d+)?", _f(label))):
        return None
    pa = [p for p in (card_price(item_text(r)) for r in after) if p is not None]
    pb = [p for p in (card_price(item_text(r)) for r in before) if p is not None]
    if len(pa) < ORDER_MIN or not pb:
        return None
    if len(vals) == 2:
        lo, hi = min(vals), max(vals)
        if all(lo - 0.005 <= p <= hi + 0.005 for p in pa) and any(p < lo - 0.005 or p > hi + 0.005 for p in pb):
            return lo, hi
        return None
    v = vals[0]
    if all(p <= v + 0.005 for p in pa) and any(p > v + 0.005 for p in pb):
        return None, v
    if all(p >= v - 0.005 for p in pa) and any(p < v - 0.005 for p in pb):
        return v, None
    return None


def shown_as_facet(rows: Sequence[Dict[str, Any]], read: Dict[str, Any], label: Any) -> bool:
    """Does the refined page itself show ``label`` as an applied facet: named by every listed result, or an active-filter
    chip (``check_filter``'s facet reading)?"""
    lab = clean_label(label)
    return bool(lab) and bool(rows) and check_filter(rows, read, {"kind": "facet", "label": lab})[0] == "ok"


def check_sorted(rows: Sequence[Dict[str, Any]], key: str, dates: Optional[Dict[str, Tuple[int, int, int]]] = None,
                 v15: bool = True) -> Tuple[str, str, Dict[str, Any]]:
    """Is the list sorted as asked? ``key``: "price_asc" / "price_desc" (the cards' prices, ``card_price``) or
    "date_desc" / "date_asc" (``row_date``: the card's markup, ``dates``, then its text; ``v15`` as there). Returns
    ``(status, why, evidence)``: "ok" when >= 2 results and >= ``VERIFY_SHARE`` of them show the value and the shown
    values keep the order (ties allowed); "failed" when two shown values break it; "incomplete" when too few show one
    (the order cannot be verified) or the key is not verifiable."""
    if key not in ("price_asc", "price_desc", "date_desc", "date_asc"):
        return "incomplete", f"sort: the order {key or 'asked for'!r} cannot be verified on the result cards", {}
    vals = []
    for i, r in enumerate(rows):
        vals.append(_value_of(r, key, dates, v15=v15))
    shown = [(i, v) for i, v in enumerate(vals) if v is not None]
    ev = {"key": key, "values": [v if not isinstance(v, tuple) else "-".join(map(str, v)) for v in vals],
          "shown": len(shown), "listed": len(rows)}
    up = key.endswith("_asc")
    for (i, a), (j, b) in zip(shown, shown[1:]):
        if key.startswith("price"):
            bad = b < a - 0.005 if up else b > a + 0.005
        else:
            bad = b < a if up else b > a
        if bad:
            return "failed", (f"sort: the list is not sorted by {key.replace('_', ', ')} (#{i + 1} shows {ev['values'][i]}, "
                              f"#{j + 1} {ev['values'][j]})"), ev
    if len(rows) >= 2 and (len(shown) < 2 or len(shown) < VERIFY_SHARE * len(rows)):
        return "incomplete", (f"sort: the order cannot be verified ({len(shown)} of {len(rows)} results show a "
                              f"{'price' if key.startswith('price') else 'date'})"), ev
    return "ok", f"sort: {key.replace('_', ', ')} verified on {len(shown)} of {len(rows)} results", ev


def active_chips(read: Dict[str, Any], label: str) -> List[str]:
    """The page's ACTIVE-FILTER evidence for ``label``: a checked checkbox / radio labelled with it, a drop-down whose
    selected option names it, a control naming it with a remove mark ("Bosch ×", "Bosch entfernen"), or the page's
    heading / breadcrumb / title naming it (a category page)."""
    lab = _f(clean_label(label))
    if not lab:
        return []
    out: List[str] = []

    def names(t: Any) -> bool:
        ft = _f(clean_label(t))
        return bool(ft) and (ft == lab or C.names_value9(ft, lab))
    for e in read.get("elements") or []:
        tag, typ = str(e.get("tag") or ""), str(e.get("type") or "")
        txt = str(e.get("text") or "")
        if tag == "input" and typ in ("checkbox", "radio") and e.get("checked") and names(txt):
            out.append(f"checked {typ} {txt[:60]!r}")
        elif tag == "select" and any(o.get("selected") and names(o.get("text")) for o in e.get("options") or []):
            out.append(f"selected option of {txt[:40]!r}")
        elif tag in ("a", "button") and names(re.sub(_REMOVE_MARK, " ", _f(txt))) and _REMOVE_MARK.search(_f(txt)):
            out.append(f"active-filter chip {txt[:60]!r}")
    for src, t in (("heading", " ".join(str(h) for h in read.get("h1") or [])),
                   ("breadcrumb", " ".join(str(b) for b in read.get("breadcrumb") or [])),
                   ("title", str(read.get("title") or ""))):
        if t and C.names_value9(t, lab):
            out.append(f"the page's {src} names it")
    return out


def check_filter(rows: Sequence[Dict[str, Any]], read: Dict[str, Any], filt: Dict[str, Any],
                 loaded_url: str = "") -> Tuple[str, str, Dict[str, Any]]:
    """Is the list filtered as asked? A FACET: its label named by every listed result (title, card), else in the page's
    active-filter chips (``active_chips``). A PRICE RANGE: every shown price within it (>= ``VERIFY_SHARE`` of the
    results show one, or a chip). IN-STOCK ONLY: no listed result shown out of stock. A URL-bound filter must still be
    in the URL loaded (a site that dropped it filtered nothing). Returns ``(status, why, evidence)``."""
    kind = filt.get("kind")
    ev: Dict[str, Any] = {"kind": kind, "label": filt.get("label")}
    if filt.get("param") and loaded_url and filt.get("mode") == "url":
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loaded_url).query, keep_blank_values=True))
        if str(q.get(filt["param"], "")) != str(filt.get("value")):
            return "failed", (f"filter: the page loaded does not carry {filt['param']}={filt.get('value')} (the site "
                              f"dropped the filter)"), ev
    chips = active_chips(read, str(filt.get("label") or "")) if filt.get("label") else []
    ev["chips"] = chips[:4]
    if kind == "price_range":
        lo, hi = filt.get("min"), filt.get("max")
        prices = [card_price(item_text(r)) for r in rows]
        ev["prices"] = prices
        out = [(i, p) for i, p in enumerate(prices) if p is not None and
               ((lo is not None and p < lo - 0.005) or (hi is not None and p > hi + 0.005))]
        if out:
            i, p = out[0]
            return "failed", f"filter: #{i + 1} shows {p}, outside the price range {lo}-{hi}", ev
        shown = sum(1 for p in prices if p is not None)
        if rows and shown < VERIFY_SHARE * len(rows) and not chips:
            return "incomplete", f"filter: the price range cannot be verified ({shown} of {len(rows)} show a price)", ev
        return "ok", f"filter: every shown price within {lo}-{hi}" + (f" ({chips[0]})" if chips else ""), ev
    if kind == "availability":
        outs = [i for i, r in enumerate(rows) if C.text_availability_class(item_text(r), v8=True, v9=True) == "out_of_stock"]
        if outs:
            return "failed", f"filter: #{outs[0] + 1} is shown out of stock (in-stock only was asked)", ev
        return "ok", "filter: no listed result is shown out of stock" + (f" ({chips[0]})" if chips else ""), ev
    lab = str(filt.get("label") or "")
    named = [i for i, r in enumerate(rows) if C.names_value9(item_text(r), lab)]
    ev["named_by"] = len(named)
    if rows and len(named) == len(rows):
        return "ok", f"filter: every listed result names {lab!r}", ev
    if chips:
        return "ok", f"filter: {lab!r} is an active filter ({chips[0]})", ev
    return "failed", f"filter: {lab!r} is neither named by every result ({len(named)} of {len(rows)}) nor an active filter", ev


# ---------------------------------------------------------------------------
# compile: the search step, the steps after it
# ---------------------------------------------------------------------------

def search_step(trace: Dict[str, Any], ks: Sequence[int]) -> int:
    """The demonstration's SEARCH step for a breadth workflow: the last typed search entering the value, else the first
    navigation carrying it (a later navigation carrying the value is a refinement or a page of the same search)."""
    steps = trace.get("steps") or []
    typed = [k for k in ks if (steps[k].get("action") or {}).get("action") == "search"]
    return typed[-1] if typed else ks[0]


def post_steps(trace: Dict[str, Any], k0: int) -> List[int]:
    """The successful navigations after the search (click, goto, select), in order."""
    out = []
    for j, st in enumerate(trace.get("steps") or []):
        if j <= k0:
            continue
        a, oc = st.get("action") or {}, st.get("outcome") or {}
        if a.get("action") in ("click", "goto", "select") and oc.get("ok"):
            out.append(j)
    return out


def _query(u: str) -> List[Tuple[str, str]]:
    try:
        return urllib.parse.parse_qsl(urllib.parse.urlsplit(str(u or "")).query, keep_blank_values=True)
    except ValueError:
        return []


def url_changes(before: str, after: str, ignore: Sequence[str] = ()) -> Tuple[Dict[str, str], List[str], bool]:
    """The query parameters ``after`` adds or changes against ``before`` (tracking parameters and ``ignore`` left out),
    the ones it drops, and whether the path is the same."""
    try:
        a, b = urllib.parse.urlsplit(str(before or "")), urllib.parse.urlsplit(str(after or ""))
    except ValueError:
        return {}, [], False
    qa, qb = dict(_query(before)), dict(_query(after))
    skip = {x.lower() for x in ignore}
    add = {k: v for k, v in qb.items() if qa.get(k) != v and not C._url_noise(k) and k.lower() not in skip}
    drop = [k for k in qa if k not in qb and not C._url_noise(k) and k.lower() not in skip]
    same_path = (a.hostname or "").lower().removeprefix("www.") == (b.hostname or "").lower().removeprefix("www.") and \
        (a.path or "/").rstrip("/") == (b.path or "/").rstrip("/")
    return add, drop, same_path


def set_params(url: str, params: Dict[str, str]) -> str:
    """``url`` with ``params`` set (replaced or added; order kept, new ones appended)."""
    sp = urllib.parse.urlsplit(url)
    q = _query(url)
    seen = set()
    out = []
    for k, v in q:
        if k in params:
            if k in seen:
                continue
            seen.add(k)
            out.append((k, str(params[k])))
        else:
            out.append((k, v))
    for k, v in params.items():
        if k not in seen and not any(k == x for x, _y in q):
            out.append((k, str(v)))
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, urllib.parse.urlencode(out), ""))


def _search_param(trace: Dict[str, Any], url: str) -> List[str]:
    """The query parameter names of ``url`` that carry the demonstrated value (the search's own)."""
    fv = W.fold(trace.get("value"))
    return [k for k, v in _query(url) if W.fold(v) == fv]


def _control(st: Dict[str, Any]) -> Dict[str, Any]:
    """The control a refinement / pagination step used (from the step's recorded mapping and outcome)."""
    a, oc = st.get("action") or {}, st.get("outcome") or {}
    m = (st.get("mapping") or {}).get(str(a.get("element"))) or {}
    els = {str(e.get("i")): e for e in (st.get("page") or {}).get("elements") or []}
    e = els.get(str(m.get("i"))) or {}
    ctl = {"action": a.get("action"), "tag": m.get("tag") or e.get("tag") or "", "text": str(m.get("text") or e.get("text") or ""),
           "href": str(m.get("href") or e.get("href") or ""), "type": str(e.get("type") or ""), "rel": str(e.get("rel") or ""),
           "role": str(e.get("role") or "")}
    if a.get("action") == "select":
        ctl.update(option_text=str(oc.get("option_text") or a.get("option") or ""), option_value=str(oc.get("option_value") or ""))
    if a.get("action") == "goto":
        ctl.update(tag="goto", href=str(a.get("url") or oc.get("href") or ""))
    return ctl


def _reported_kind(trace: Dict[str, Any], i: int) -> str:
    """The kind ("sort" / "filter") the demonstrator reported for its ``i``-th refinement ("" when it said none)."""
    rep = (trace.get("result") or {}).get("applied") or []
    rep = [r for r in rep if isinstance(r, dict)] if isinstance(rep, list) else []
    if i < len(rep):
        k = _f(rep[i].get("kind"))
        return "sort" if "sort" in k else "filter" if k else ""
    return ""


def _applied_label(trace: Dict[str, Any], ctl: Dict[str, Any]) -> str:
    """The label of what a refinement applied: a select's option, else the control's text, else the demonstrator's
    reported "applied" entry."""
    if ctl.get("option_text"):
        return ctl["option_text"]
    if ctl.get("text") and ctl.get("tag") != "goto":
        return ctl["text"]
    rep = ((trace.get("result") or {}).get("applied") or [])
    for r in rep if isinstance(rep, list) else []:
        if isinstance(r, dict) and (r.get("option") or r.get("value") or r.get("control")):
            return str(r.get("option") or r.get("value") or r.get("control"))
    return ""


# ---------------------------------------------------------------------------
# the demonstration's validity per type (code only)
# ---------------------------------------------------------------------------

def _result_links(pages: Sequence[Dict[str, Any]], base: str, got: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    links = {C._norm(e.get("href")) for p in pages for e in p.get("elements") or [] if e.get("href")}
    return [r for r in got if C._norm(urllib.parse.urljoin(base, str(r.get("url")))) in links]


def validity(trace: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Version 10: is a breadth demonstration valid? Common: ``done`` with a result within the step cap, no forbidden
    action, the value entered into the site's search, the result page on the site. Then per type:

    * filtered_search: a refinement (a click, a drop-down choice or a same-site navigation after the search) that
      CHANGED the result page (its URL, or its list) was applied, and >= 1 reported result is a link of the final page;
    * paged_search: >= 2 result pages visited (a later page reached from the search's result page, another URL) and
      >= 1 reported result is a link of the LAST page and not of the first page's list;
    * visit_results: >= 2 results OPENED (item pages reached from the result page, each a link of it) and the reported
      items' names / prices are on their pages."""
    det: Dict[str, Any] = {}
    wf = trace.get("workflow")
    site = str(trace.get("site") or "")
    if trace.get("stop") != "done":
        return False, f"the demonstration did not finish ({trace.get('stop')})", det
    res = trace.get("result") or {}
    if res.get("no_results"):
        return False, C.no_results_reason(trace, det), det
    if trace.get("forbidden"):
        return False, f"a forbidden action was attempted ({trace['forbidden'][0]})", det
    ks = C._search_steps(trace)
    if not ks:
        return False, "the input value was never entered into the site's search", det
    k0 = search_step(trace, ks)
    det["search_step"] = k0
    p1 = C.page_after(trace, k0)
    if not C.same_site(p1.get("url"), site):
        return False, "the search's result page is not on the site", det
    steps = trace.get("steps") or []
    post = post_steps(trace, k0)
    det["post_steps"] = post
    fin = trace.get("final_page") or {}
    if wf == "filtered_search":
        if not post:
            return False, "no filter or sort order was applied after the search", det
        if len(post) > MAX_REFINE:
            return False, f"more than {MAX_REFINE} steps after the search (a filtered search refines its list)", det
        if not C.same_site(fin.get("url"), site):
            return False, "the demonstration ended off the site", det
        changed = C._norm(str(fin.get("url") or "")) != C._norm(str(p1.get("url") or "")) or \
            [e.get("href") for e in W.content_links(fin)][:20] != [e.get("href") for e in W.content_links(p1)][:20]
        if not changed:
            return False, "the refinement did not change the result page (its URL and its list are the same)", det
        got = [r for r in res.get("results") or [] if isinstance(r, dict) and r.get("url")]
        hit = _result_links([fin], str(fin.get("url") or ""), got)
        det["reported"], det["reported_on_page"] = len(got), len(hit)
        if not hit:
            return False, "no reported result is a link of the refined result page", det
        return True, "valid", det
    if wf == "paged_search":
        later = [j for j in post if C.same_site(C.page_after(trace, j).get("url"), site)
                 and C._norm(str(C.page_after(trace, j).get("url") or "")) != C._norm(str(p1.get("url") or ""))]
        det["pages_visited"] = 1 + len({C._norm(str(C.page_after(trace, j).get("url") or "")) for j in later})
        if not later:
            return False, "only one result page was visited (a paged search visits >= 2)", det
        p2 = C.page_after(trace, later[-1])
        got = [r for r in res.get("results") or [] if isinstance(r, dict) and r.get("url")]
        on2 = _result_links([p2], str(p2.get("url") or ""), got)
        first = {C._norm(e.get("href")) for e in W.content_links(p1)}
        new = [r for r in on2 if C._norm(urllib.parse.urljoin(str(p2.get("url") or ""), str(r["url"]))) not in first]
        det["reported"], det["on_last_page"], det["not_on_first_page"] = len(got), len(on2), len(new)
        if not new:
            return False, "no reported result is a link of the later page that the first page does not list", det
        return True, "valid", det
    if wf == "visit_results":
        opened = opened_items(trace, k0)
        det["opened"] = [o["url"] for o in opened]
        if len(opened) < 2:
            return False, f"{len(opened)} result(s) opened (a visit opens >= 2)", det
        items = [x for x in res.get("items") or [] if isinstance(x, dict)]
        det["reported_items"] = len(items)
        good = 0
        for o in opened:
            rep = reported_for(items, o["url"], opened.index(o))
            pg = o["page"]
            if not rep:
                continue
            heads = " ".join([str(pg.get("title") or ""), " ".join(str(h) for h in pg.get("h1") or []),
                              " ".join(str(f.get("name") or "") for f in pg.get("facts") or [])])
            rp = C._price_of(rep.get("price"))
            prices = [float(f["price"]) for f in pg.get("facts") or [] if f.get("price") is not None]
            if rep.get("name") and C.name_share(heads, rep.get("name")) >= C.MIN_NAME_SHARE and rp is not None and \
                    (C.visible_price(pg.get("text"), rp) or any(abs(rp - p) < 0.011 for p in prices)):
                good += 1
        det["reported_on_their_pages"] = good
        if good < 2:
            return False, f"{good} reported item(s) are named and priced on their own pages (>= 2 needed)", det
        return True, "valid", det
    return False, f"unknown workflow {wf!r}", det


def opened_items(trace: Dict[str, Any], k0: int) -> List[Dict[str, Any]]:
    """visit_results: the results the demonstration OPENED - each navigation after the search that left the result
    page for an item page of the site (``web_arch.product_detail`` not False) through a link the result page showed
    (the result page's own links, or a URL it listed) - with its URL (the link), the URL landed on and the page."""
    site = str(trace.get("site") or "")
    p1 = C.page_after(trace, k0)
    listed = {C._norm(e.get("href")) for e in p1.get("elements") or [] if e.get("href")}
    out: List[Dict[str, Any]] = []
    steps = trace.get("steps") or []
    for j in post_steps(trace, k0):
        a, oc = steps[j].get("action") or {}, steps[j].get("outcome") or {}
        if a.get("action") not in ("click", "goto"):
            continue
        href = C.strip_fragment(str(oc.get("href") or a.get("url") or oc.get("url_after") or ""))
        landed = C.strip_fragment(str(oc.get("url_after") or href))
        pg = C.page_after(trace, j)
        if not C.same_site(landed, site) or C._norm(landed) == C._norm(str(p1.get("url") or "")):
            continue
        if C._norm(href) not in listed and C._norm(landed) not in listed:
            continue
        if W.product_detail(pg, str(trace.get("value") or ""))[0] is False:
            continue
        if any(C._norm(o["url"]) == C._norm(href) for o in out):
            continue
        out.append({"url": href, "landed": landed, "page": pg, "step": j})
    return out


def reported_for(items: Sequence[Dict[str, Any]], url: str, i: int) -> Dict[str, Any]:
    """The demonstrator's reported item for an opened URL (by URL, else by its position)."""
    for x in items:
        if x.get("url") and C._norm(str(x.get("url"))) == C._norm(url):
            return x
    for x in items:
        if x.get("url") and C._url_parts(x.get("url"))[1] == C._url_parts(url)[1]:
            return x
    return items[i] if i < len(items) and not items[i].get("url") else {}


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

def compile_breadth(trace: Dict[str, Any], tool: Dict[str, Any], report: Dict[str, Any], k0: int,
                    version: int) -> Dict[str, Any]:
    """The breadth part of ``demo_compile.compile_trace`` (the search is compiled there, from step ``k0``):
    ``{"ok": True, "tool"}`` or ``{"ok": False, "why"}``."""
    wf = str(trace.get("workflow") or "")
    ok, why, det = validity(trace)
    report["validity"] = det
    if not ok:
        return {"ok": False, "why": f"the demonstration is not valid ({why})", "report": report}
    if wf == "filtered_search":
        why = _compile_filtered(trace, tool, report, k0, version)
    elif wf == "paged_search":
        why = _compile_paged(trace, tool, report, k0, version)
    else:
        why = _compile_visit(trace, tool, report, k0, version)
    if why:
        return {"ok": False, "why": why, "report": report}
    tool["hosts"] = C.demo_hosts(trace, str(trace.get("site") or ""))
    tool["result_scope"] = C.result_scope_from(trace, str(trace.get("site") or ""))
    tool["compiled_from"] = {"steps": len(trace.get("steps") or []), "search_step": k0,
                             "trace_sha256": C.trace_digest(trace)}
    return {"ok": True, "tool": tool, "report": report}


def _list_structure(trace: Dict[str, Any], page: Dict[str, Any], results: Sequence[Dict[str, Any]],
                    report: Dict[str, Any], tool: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The result structure of a LIST page (``page``: the refined page / page 2) holding the reported ``results``
    (version 8's and 10's query-region rules; the version-10 card read with each card's text)."""
    url = str(page.get("url") or "")
    urls = [urllib.parse.urljoin(url, str(r.get("url"))) for r in results if isinstance(r, dict) and r.get("url")]
    region = C.region_from_dom(str(page.get("dom") or ""), url, urls)
    if region is None and len(urls) == 1:
        region = C.region_from_url(urls[0])
    if region is None:
        return None, "no result region holds the reported results"
    titled = [(urllib.parse.urljoin(url, str(r.get("url"))), str(r.get("title") or ""))
              for r in results if isinstance(r, dict) and r.get("url")]
    start_page = (trace.get("steps") or [{}])[0].get("page") or {}
    why = C.compile_cards(region, page, titled, "site_search", report, legacy=C.containers_of(page, urls), v8=True,
                          value=str(trace.get("value") or ""), start_page=start_page,
                          overlay=bool((tool.get("search") or {}).get("overlay")), v10=True, cards_version=10)
    return (None, why) if why else (region, "")


def _compile_filtered(trace: Dict[str, Any], tool: Dict[str, Any], report: Dict[str, Any], k0: int,
                      version: int) -> str:
    """The refinement's kind and key, the demonstration's own pages first: the order its refined page's cards satisfy
    and the list before did not (``refinement_orders``), a price range its cards keep (``range_kept``) - for a
    refinement the page does not show as a facet itself (``shown_as_facet``) and that no name reads as another
    filter; then the kind the demonstrator reported; the lexicons last (``classify_param``, ``sort_key``)."""
    steps = trace.get("steps") or []
    p1 = C.page_after(trace, k0)
    fin = trace.get("final_page") or {}
    post = post_steps(trace, k0)
    u0, u1 = str(p1.get("url") or ""), str(fin.get("url") or "")
    ignore = _search_param(trace, u0) + _search_param(trace, u1) + ["page", "p", "pg", "offset", "start"]
    add, drop, same_path = url_changes(u0, u1, ignore)
    ctls = [_control(steps[j]) for j in post]
    labels = [_applied_label(trace, c) for c in ctls]
    # the result structure first: the demonstration's own refined page and the list before it are the evidence
    region, why = _list_structure(trace, fin, (trace.get("result") or {}).get("results") or [], report, tool)
    if region is None:
        return why
    rows = C.region_read(fin, region)["items"][:FILTER_TOP_K]
    before = C.region_read(p1, region)["items"][:FILTER_TOP_K]
    dates_fin = markup_dates(fin.get("dom"), u1, rows)
    orders = refinement_orders(before, rows, markup_dates(p1.get("dom"), u0, before), dates_fin)
    report["refine_evidence"] = {"orders": sorted(orders)}
    refine: Dict[str, Any] = {}
    applied: List[Dict[str, Any]] = []
    if add and same_path and not drop:
        # the refinement's URL parameters: bound parameters, applied to the search's own result URL at run time
        lab = labels[-1] if len(add) == 1 and labels else ""
        said = _reported_kind(trace, 0) if len(add) == 1 else ""
        for k, v in add.items():
            named = classify_param(k, v, lab if len(add) == 1 else "")
            # the cards' order or price bounds read a refinement nothing else names: never one the page shows as a
            # facet itself (its label on every refined card, or an active-filter chip), nor one its name reads as
            # another filter
            open_kind = len(add) == 1 and (named["kind"] == "sort" or
                                           (named["kind"] == "facet" and not shown_as_facet(rows, fin, lab)))
            rng = range_kept(before, rows, v, lab) if len(add) == 1 and said != "sort" and \
                ((open_kind and named["kind"] == "facet") or named["kind"] == "price_range") else None
            if open_kind and len(orders) == 1 and said != "filter":
                a = {"kind": "sort", "param": k, "value": v, "key": next(iter(orders)), "label": clean_label(lab),
                     "by": "the refined page's cards"}
            elif rng is not None:
                a = {"kind": "price_range", "param": k, "value": v, "min": rng[0], "max": rng[1],
                     "label": clean_label(lab), "by": "the refined page's cards"}
            elif said == "sort":
                a = {"kind": "sort", "param": k, "value": v, "key": sort_key(v, lab), "label": clean_label(lab)}
            else:
                a = named
                if said == "filter" and a["kind"] == "sort":
                    a = {"kind": "facet", "param": k, "value": v, "label": clean_label(lab) or
                         urllib.parse.unquote_plus(str(v or "")).replace("-", " ").replace("_", " ")}
            if a["kind"] == "facet" and len(add) > 1:
                # several parameters: each control's label for the parameter whose value it names
                a["label"] = next((clean_label(x) for x in labels if x and (W.fold(v) in W.fold(x) or
                                                                             W.fold(x) in W.fold(v))), a["label"])
            a["mode"] = "url"
            applied.append(a)
        refine = {"mode": "url", "params": dict(add)}
    else:
        if any(c["action"] == "goto" and not c.get("text") for c in ctls):
            return ("the refinement cannot be replayed (a navigation that is no parameter of the result URL and no "
                    "control of the page)")
        for i, (c, lab) in enumerate(zip(ctls, labels)):
            if not lab:
                return "a refinement control has no label to find it again"
            said = _reported_kind(trace, i)
            if said:
                kind = "sort" if said == "sort" else ""
            elif len(orders) == 1 and c.get("type") not in ("checkbox", "radio") and not names_stock(lab) and \
                    not shown_as_facet(rows, fin, lab):
                kind = "sort"                          # the refined page's cards show the order it set
            else:
                # a sort control: one labelled as a sort, or a drop-down / link naming a sort order (never a checkbox)
                kind = "sort" if is_sort("", c.get("text")) or (sort_key(lab) and c.get("type") not in
                                                                ("checkbox", "radio")) else ""
            if kind == "sort":
                key = next(iter(orders)) if len(orders) == 1 else sort_key(lab, c.get("option_value"))
                applied.append({"kind": "sort", "key": key, "label": clean_label(lab), "mode": "controls"})
            elif names_stock(lab) and len(_f(lab)) <= 40:
                applied.append({"kind": "availability", "label": clean_label(lab), "mode": "controls"})
            else:
                applied.append({"kind": "facet", "label": clean_label(lab), "mode": "controls"})
        refine = {"mode": "controls", "controls": [{k: v for k, v in c.items() if v} for c in ctls]}
    if not applied:
        return "no filter or sort order could be read from the refinement"
    # the demonstration's own refined page must pass the checks the tool runs
    report["refine_self_check"] = []
    for a in applied:
        if a["kind"] == "sort":
            st, why2, ev = check_sorted(rows, a.get("key") or "", dates_fin)
        else:
            st, why2, ev = check_filter(rows, fin, a, u1)
        report["refine_self_check"].append({"applied": a, "status": st, "why": why2})
        if st == "failed":
            return f"the demonstration's own refined page fails the check ({why2})"
    tool.update(region=region, top_k=FILTER_TOP_K, refine=refine, applied=applied,
                params=[{"name": a.get("param") or f"control{i + 1}", "kind": a["kind"], "default": a.get("value") or
                         a.get("label"), **({"key": a["key"]} if a.get("key") else {})} for i, a in enumerate(applied)],
                output=["results", "applied"], checks=["search_page", "membership", "query", "empty_state", "refined"])
    return ""


def _page_mech(trace: Dict[str, Any], u1: str, u2: str, n1: int) -> Dict[str, Any]:
    """How page 2 is addressed: a URL parameter (a page number, base 1 or 0, or an offset of the first page's item count),
    a path segment, else {} (the next-page link)."""
    ignore = _search_param(trace, u1) + _search_param(trace, u2)
    add, drop, same_path = url_changes(u1, u2, ignore)
    q1 = dict(_query(u1))
    if same_path and len(add) == 1 and not drop:
        k, v = next(iter(add.items()))
        if v.isdigit():
            v2, v1 = int(v), q1.get(k)
            v1n = int(v1) if str(v1 or "").isdigit() else None
            if v2 == 2 and v1n in (None, 1):
                return {"mode": "param", "key": k, "first": 1, "stride": 1}
            if v2 == 1 and v1n in (None, 0):
                return {"mode": "param", "key": k, "first": 0, "stride": 1}
            if n1 and v2 == n1 and v1n in (None, 0):
                return {"mode": "param", "key": k, "first": 0, "stride": n1}
            if n1 and v2 == n1 + 1 and v1n in (None, 1):
                return {"mode": "param", "key": k, "first": 1, "stride": n1}
    try:
        a, b = urllib.parse.urlsplit(u1), urllib.parse.urlsplit(u2)
    except ValueError:
        return {}
    if a.query == b.query or not (set(dict(_query(u1)).items()) ^ set(dict(_query(u2)).items())):
        s1 = [x for x in a.path.split("/") if x]
        s2 = [x for x in b.path.split("/") if x]
        if s2[:len(s1)] == s1 and 1 <= len(s2) - len(s1) <= 2 and s2[-1] == "2":
            return {"mode": "path_append", "suffix": "/".join(s2[len(s1):-1] + ["{page}"])}
        if len(s1) == len(s2):
            diff = [i for i in range(len(s1)) if s1[i] != s2[i]]
            if len(diff) == 1:
                i = diff[0]
                m = re.fullmatch(r"(.*?)2(\D*)", s2[i])
                if m and (s1[i] == m.group(1) + "1" + m.group(2)):
                    return {"mode": "path_segment", "index": i, "pattern": m.group(1) + "{page}" + m.group(2)}
    return {}


def page_url(url1: str, mech: Dict[str, Any], p: int) -> str:
    """The URL of result page ``p`` (1-based) from page 1's loaded URL and the compiled mechanism; "" for a link-mode
    tool."""
    m = mech.get("mode")
    if m == "param":
        v = int(mech.get("first", 1)) + (p - 1) * int(mech.get("stride", 1)) if int(mech.get("stride", 1)) == 1 else \
            int(mech.get("first", 0)) + (p - 1) * int(mech.get("stride", 1))
        return set_params(url1, {mech["key"]: str(v)})
    sp = urllib.parse.urlsplit(url1)
    segs = [x for x in sp.path.split("/") if x]
    if m == "path_append":
        path = "/" + "/".join(segs + [mech["suffix"].replace("{page}", str(p))])
    elif m == "path_segment" and int(mech["index"]) < len(segs):
        segs[int(mech["index"])] = mech["pattern"].replace("{page}", str(p))
        path = "/" + "/".join(segs)
    else:
        return ""
    if sp.path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, sp.query, ""))


def pager_keys(cur: str, hrefs: Sequence[str]) -> set:
    """The query keys the page's links number like a pager: links that differ from ``cur`` only in that key, with at
    least two different whole numbers."""
    seen: Dict[str, set] = {}
    for h in hrefs:
        add, drop, same_path = url_changes(cur, h)
        if same_path and len(add) == 1 and not drop:
            k, v = next(iter(add.items()))
            if v.isdigit():
                seen.setdefault(k, set()).add(int(v))
    return {k for k, vs in seen.items() if len(vs) >= 2}


def increments(cur: str, href: str, p: int, n_items: int = 0, pagers: Optional[set] = None) -> bool:
    """Is ``href`` the URL of the page after result page ``p`` at ``cur`` by its URL alone: the same path, and one
    query parameter whose number grows by one (or by the page's item count: an offset) - or, where ``cur`` has none,
    is 2 / ``p + 1`` / the item count on a key the page's links number like a pager (``pagers``); or one path segment
    whose number grows by one. Language-independent."""
    add, drop, same_path = url_changes(cur, href)
    if same_path and len(add) == 1 and not drop:
        k, v = next(iter(add.items()))
        if not v.isdigit():
            return False
        c = dict(_query(cur)).get(k)
        if c is not None and str(c).isdigit():
            return int(v) == int(c) + 1 or (n_items > 1 and int(v) == int(c) + n_items)
        return (pagers is None or k in pagers) and (int(v) in (2, p + 1) or (n_items > 1 and int(v) == n_items))
    try:
        a, b = urllib.parse.urlsplit(cur), urllib.parse.urlsplit(href)
    except ValueError:
        return False
    if (a.hostname or "").lower() != (b.hostname or "").lower() or a.query != b.query:
        return False
    s1, s2 = [x for x in a.path.split("/") if x], [x for x in b.path.split("/") if x]
    if len(s1) == len(s2):
        diff = [i for i in range(len(s1)) if s1[i] != s2[i]]
        if len(diff) == 1:
            m1 = re.fullmatch(r"(\D*)(\d+)(\D*)", s1[diff[0]])
            m2 = re.fullmatch(r"(\D*)(\d+)(\D*)", s2[diff[0]])
            return bool(m1 and m2 and m1.group(1) == m2.group(1) and m1.group(3) == m2.group(3) and
                        int(m2.group(2)) == int(m1.group(2)) + 1)
    return len(s2) == len(s1) + 2 and s2[:len(s1)] == s1 and s2[-1] == str(p + 1) or \
        (len(s2) == len(s1) + 1 and s2[:len(s1)] == s1 and s2[-1] == str(p + 1))


def numbered_keys(cur: str, links: Sequence[Dict[str, Any]], keys: set) -> set:
    """Of the pager-like ``keys``, those a link numbers with its own text: the link's text is the page number its URL
    carries (a pager's "3" for ``page=3``), never a facet's label ("4 Sterne" for ``rating=4``)."""
    out = set()
    for e in links:
        add, drop, same_path = url_changes(cur, str(e.get("href") or ""))
        if same_path and len(add) == 1 and not drop:
            k, v = next(iter(add.items()))
            if k in keys and _f(e.get("text")).strip() == v:
                out.add(k)
    return out


def next_link(read: Dict[str, Any], site: str, p: int, visited: Sequence[str], n_items: int = 0,
              v15: bool = True) -> Optional[Dict[str, Any]]:
    """The NEXT-page link of result page ``p``, by the page's own structure first: a same-site link with
    ``rel=next``; else one whose URL is the next page's by the number it increments (``increments``: a parameter or
    path segment of the current URL); else one whose text is the page number ``p + 1``; else one whose text says next
    (the 11 languages, arrows); last, on a page whose URL carries no page number yet, one that sets a key the page's
    links number like a pager (``pager_keys``; the keys that links number with their own text first, so a facet with
    numeric values - a rating, a brand id - is no pager while a pager is in sight). Never a page already visited.
    ``v15`` False: a tool before version 15, whose run takes ``rel=next``, then a next-page word, then the page
    number ``p + 1``."""
    seen = {C._norm(u) for u in visited}
    cur = str(read.get("url") or "")
    cands = [e for e in read.get("elements") or [] if e.get("tag") == "a" and e.get("href") and
             C.same_site(e.get("href"), site) and C._norm(e.get("href")) not in seen and
             e.get("area") not in ("header", "footer") and not W.forbidden_control(e.get("text"), e.get("href"))]
    if not v15:
        for test in (lambda e: "next" in str(e.get("rel") or "").split(),
                     lambda e: bool(_NEXT_TEXT.match(_f(e.get("text")).strip())),
                     lambda e: _f(e.get("text")).strip() == str(p + 1)):
            hit = [e for e in cands if test(e)]
            if hit:
                return hit[0]
        return None
    pagers = pager_keys(cur, [str(e.get("href")) for e in cands]) if cur else set()
    pagers = numbered_keys(cur, cands, pagers) or pagers
    for test in (lambda e: "next" in str(e.get("rel") or "").split(),
                 lambda e: bool(cur) and increments(cur, str(e.get("href")), p, n_items, set()),
                 lambda e: _f(e.get("text")).strip() == str(p + 1),
                 lambda e: bool(_NEXT_TEXT.match(_f(e.get("text")).strip())),
                 lambda e: bool(cur) and increments(cur, str(e.get("href")), p, n_items, pagers)):
        hit = [e for e in cands if test(e)]
        if hit:
            return hit[0]
    return None


def next_control(read: Dict[str, Any], site: str, p: int, visited: Sequence[str], v15: bool = True) -> bool:
    """Does result page ``p`` show a next-page CONTROL - a next-page link (``next_link``; ``v15`` as there), or a
    button / role=button whose text says next or is the page number ``p + 1`` (a script-driven pagination)?"""
    if next_link(read, site, p, visited, v15=v15) is not None:
        return True
    for e in read.get("elements") or []:
        if e.get("tag") == "button" or e.get("role") == "button" or (e.get("tag") == "input" and e.get("type") == "radio"):
            t = _f(e.get("text")).strip()
            if _NEXT_TEXT.match(t) or t == str(p + 1):
                return True
    return False


def _compile_paged(trace: Dict[str, Any], tool: Dict[str, Any], report: Dict[str, Any], k0: int, version: int) -> str:
    steps = trace.get("steps") or []
    p1 = C.page_after(trace, k0)
    post = post_steps(trace, k0)
    later = [j for j in post if C._norm(str(C.page_after(trace, j).get("url") or "")) != C._norm(str(p1.get("url") or ""))]
    j2 = later[0]
    # page 2: the page the first navigation away from page 1 reached; the results were reported on the LAST page
    p2 = C.page_after(trace, j2)
    pl = C.page_after(trace, later[-1])
    results = (trace.get("result") or {}).get("results") or []
    onl = _result_links([pl], str(pl.get("url") or ""), [r for r in results if isinstance(r, dict) and r.get("url")])
    region, why = _list_structure(trace, pl, onl or results, report, tool)
    if region is None:
        return why
    n1 = len(C.region_read(p1, region)["items"])
    n2 = len(C.region_read(p2, region)["items"])
    report["pages"] = {"page1_items": n1, "page2_items": n2}
    if n1 < 1:
        return "the result structure of page 2 does not read page 1"
    mech = _page_mech(trace, str(p1.get("url") or ""), str(p2.get("url") or ""), n1)
    ctl = _control(steps[j2])
    link = {"text": ctl.get("text"), "rel": ctl.get("rel")}
    if not mech:
        # the clicked link is a next-page link by its rel, by the page number its URL increments, by its text "2" -
        # the lexicon's next words last
        if ctl.get("tag") != "a" or not (
                link["rel"] == "next" or increments(str(p1.get("url") or ""), str(ctl.get("href") or ""), 1, n1,
                                                    pager_keys(str(p1.get("url") or ""),
                                                               [str(e.get("href")) for e in p1.get("elements") or []
                                                                if e.get("tag") == "a" and e.get("href")])) or
                _f(link["text"]).strip() == "2" or _NEXT_TEXT.match(_f(link["text"]).strip())):
            return "the pagination cannot be replayed (no page parameter or path, and no next-page / page-2 link)"
    pager = dict(mech) if mech else {"mode": "link"}
    pager["link"] = link
    n = min(MAX_N, max(2, n1 + n2))
    total = stated_total(p1.get("main_text") or p1.get("text"), trace.get("value"))
    tool.update(region=region, pager=pager, n=n, max_pages=MAX_PAGES,
                params=[{"name": "n", "kind": "count", "default": n}],
                output=["results", "total", "pages"], checks=["search_page", "membership", "query", "empty_state",
                                                              "no_duplicates", "page_order", "stated_total"])
    report["paged"] = {"mechanism": pager, "n": n, "stated_total_page1": total}
    return ""


def _compile_visit(trace: Dict[str, Any], tool: Dict[str, Any], report: Dict[str, Any], k0: int, version: int) -> str:
    opened = opened_items(trace, k0)
    p1 = C.page_after(trace, k0)
    items = [x for x in (trace.get("result") or {}).get("items") or [] if isinstance(x, dict)]
    wanted = []
    pages = []
    for i, o in enumerate(opened):
        rep = reported_for(items, o["url"], i)
        wanted.append((o["url"], str(rep.get("name") or ""), [o["landed"]]))
        pages.append((o, rep))
    url = str(p1.get("url") or "")
    region = C.region_from_dom(str(p1.get("dom") or ""), url, [w[0] for w in wanted])
    if region is None:
        return "no result region holds the opened results"
    start_page = (trace.get("steps") or [{}])[0].get("page") or {}
    why = C.compile_cards(region, p1, wanted, "product_lookup", report, legacy=C.containers_of(p1, [w[0] for w in wanted]),
                          v8=True, value=str(trace.get("value") or ""), start_page=start_page,
                          overlay=bool((tool.get("search") or {}).get("overlay")), v10=True, cards_version=10)
    if why:
        return why
    from anyuse.execution import demo_cards as K
    tried: List[Tuple[int, Dict[str, str], List[str]]] = []
    for o, rep in pages:
        pg = dict(o["page"])
        if pg.get("dom"):
            pg["microdata"] = K.microdata_of(str(pg.get("dom")))
            pg["struck_prices"] = K.struck_of(str(pg.get("dom")))
        o["page_c"] = pg
    for o, rep in pages:
        if not rep.get("name"):
            continue
        srcs, notes = C.field_sources(o["page_c"], rep, str(rep.get("name")), v8=True, v9=True, v10=True)
        if "name" not in srcs or "price" not in srcs:
            continue
        srcs.update(url_rule="same_product", text_rule="9", statement_rule="10")
        if srcs.get("price") == "text":
            srcs, note = C._text_price_place_v8(o["page_c"], srcs, rep, str(rep.get("name")))
            notes.append(note)
        back = 0
        for o2, rep2 in pages:
            rp = C._price_of(rep2.get("price"))
            got = C.extract_fields(o2["page_c"], srcs, str(rep2.get("name") or ""))
            if rp is not None and got.get("price") is not None and abs(got["price"] - rp) < 0.011:
                back += 1
        tried.append((back, srcs, notes))
    if not tried:
        return "the opened product pages state no stable name and price"
    tried.sort(key=lambda t: -t[0])
    back, srcs, notes = tried[0]
    report["field_notes"] = notes
    report["fields_read_back"] = f"{back} of {len(pages)} opened pages"
    if back < 1:
        return "the compiled field sources read no reported price back"
    k = min(MAX_VISIT, max(2, len(opened)))
    tool.update(region=region, fields=srcs, k=k, params=[{"name": "k", "kind": "count", "default": k}],
                output=["items"], checks=["search_page", "membership", "query", "empty_state", "identity", "fields"])
    return ""


# ---------------------------------------------------------------------------
# the run (no model)
# ---------------------------------------------------------------------------

async def _read_list(page, tool: Dict[str, Any], value: str, out: Dict[str, Any], exclude: Sequence[str],
                     result_wait_s: float, with_dom: bool = False
                     ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """Wait for the result list, read it (the version-10 card read), judge it as a site search's list (all items):
    ``(read, judge, items)``. ``with_dom``: keep the page's DOM (the dates its cards' markup states)."""
    from anyuse.execution import demo_tool as T
    trail: List[Any] = []
    out.setdefault("results_after_s", []).append(await T.wait_results(page, tool, value, result_wait_s, trail=trail))
    read = await T.read_full(page, with_dom=with_dom, tool=tool, own_microdata=True, struck=True)
    out["pages"].append(T.page_record(read))
    j = C.judge_result_page(dict(tool, workflow="site_search", top_k=10_000), read, value, exclude)
    # the same read against the tool's RESULT SCOPE (a promo card is never collected)
    items = C.region_read(read, tool.get("region") or {}, exclude, scope=C.result_scope(tool))["items"] \
        if j["status"] in ("list", "incomplete") else []
    return read, j, items


def _row(it: Dict[str, Any]) -> Dict[str, Any]:
    r = {"title": str(it.get("title") or "")[:200], "url": it.get("url")}
    if it.get("link") not in (None, "site"):
        r["link"] = it["link"]
    return r


async def run_breadth(page, tool: Dict[str, Any], value: str, settle: Callable, result_wait_s: float = 8.0,
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One invocation of a version-10 breadth tool (no model): the search as every tool's (``demo_tool._search``), then
    the type's steps and checks. ``params``: bound parameters to override (a refinement parameter's value, ``n``,
    ``k``); their compiled defaults otherwise."""
    from anyuse.execution import demo_tool as T
    t0 = time.monotonic()
    out: Dict[str, Any] = {"value": value, "status": "", "output": None, "why": "", "checks": {}, "pages": [],
                           "model_calls": 0, "search_via": "", "tool_sha256": C.tool_digest(tool),
                           "workflow": tool.get("workflow"), "params": dict(params or {})}
    page = T.RetryNav(page, out)
    try:
        got = await T._search(page, tool, value, settle, out, t0)
        if got.get("done"):
            return got["done"]
        exclude = got["exclude"]
        wf = tool.get("workflow")
        if wf == "filtered_search":
            return await _run_filtered(page, tool, value, settle, out, t0, exclude, result_wait_s, params or {})
        if wf == "paged_search":
            return await _run_paged(page, tool, value, settle, out, t0, exclude, result_wait_s, params or {})
        return await _run_visit(page, tool, value, settle, out, t0, exclude, result_wait_s, params or {})
    except Exception as exc:                           # noqa: BLE001 - an invocation failure is a record
        from anyuse.execution.browser_supervisor import classify
        if classify(exc):
            raise
        out.update(status="error", why=f"{type(exc).__name__}: {exc}"[:200])
        return T._done(out, t0)


async def _first_list(page, tool, value, out, t0, exclude, result_wait_s) -> Optional[Dict[str, Any]]:
    """Page 1 of the search: a failure / no results / a block ends the run (returned); else None (the list is there)."""
    from anyuse.execution import demo_tool as T
    read, j, items = await _read_list(page, tool, value, out, exclude, result_wait_s)
    blk = await T._blocked(page, read)
    out["checks"]["result_page"] = {k: v for k, v in j.items() if k not in ("el", "results")}
    if blk:
        out.update(status="blocked", why=blk)
        return T._done(out, t0)
    if j["status"] == "failed":
        out.update(status="failed", why=j["why"])
        return T._done(out, t0)
    if j["status"] == "no_results":
        out.update(status="no_results", output={"no_results": True}, why=j["why"])
        return T._done(out, t0)
    out["_page1"] = (read, j, items)
    return None


async def apply_control(page, ctl: Dict[str, Any], settle: Callable) -> Dict[str, Any]:
    """A refinement CONTROL found again on the current page by its label: a drop-down whose options hold the option
    (chosen), a checkbox / radio, a link or a button with the same text (clicked; a same-site link followed)."""
    from anyuse.execution import web_arch_run as RR
    read = await RR.read_page(page)
    want = _f(ctl.get("text"))
    for e in read.get("elements") or []:
        tag = str(e.get("tag") or "")
        if ctl.get("tag") == "select" and tag == "select":
            opt = next((o for o in e.get("options") or [] if _f(o.get("text")) == _f(ctl.get("option_text"))), None)
            if opt is not None:
                if opt.get("selected"):
                    return {"ok": True, "how": "already selected"}
                return await RR.choose_option(page, {"i": e.get("i"), "option": opt.get("value")}, settle)
    for e in read.get("elements") or []:
        tag = str(e.get("tag") or "")
        if ctl.get("tag") == "select" or tag != ctl.get("tag"):
            continue
        if _f(clean_label(e.get("text"))) != _f(clean_label(ctl.get("text"))) or not want:
            continue
        if tag == "input" and e.get("checked"):
            return {"ok": True, "how": "already checked"}
        if tag == "a" and e.get("href"):
            return await RR.open_link(page, e, settle)
        return await RR.click(page, e, settle)
    return {"ok": False, "why": f"the control {ctl.get('option_text') or ctl.get('text')!r} is not on the result page"}


async def _run_filtered(page, tool, value, settle, out, t0, exclude, result_wait_s, params) -> Dict[str, Any]:
    from anyuse.execution import demo_tool as T
    done = await _first_list(page, tool, value, out, t0, exclude, result_wait_s)
    if done:
        return done
    out.pop("_page1", None)
    ref = tool.get("refine") or {}
    applied = [dict(a) for a in tool.get("applied") or []]
    # a version-15 tool reads the site's own evidence first (a sort word as a whole word, the cards' markup dates, a
    # numeric date before a month name); an older tool reads its pages exactly as it did
    v15 = C.v15(tool)
    if ref.get("mode") == "url":
        prm = {k: str(params.get(k, v)) for k, v in (ref.get("params") or {}).items()}
        for a in applied:
            if a.get("param") in prm:
                a["value"] = prm[a["param"]]
                if a["kind"] == "sort" and a["param"] in params:
                    a["key"] = sort_key(prm[a["param"]], v15=v15) or a.get("key")
        here = str(await page.url())
        target = set_params(here, prm)
        out["checks"]["refine"] = {"mode": "url", "from": here[:300], "to": target[:300]}
        await page.goto(target)
        await settle(page)
    else:
        trail = []
        for ctl in ref.get("controls") or []:
            res = await apply_control(page, ctl, settle)
            trail.append({"control": (ctl.get("option_text") or ctl.get("text") or "")[:80], "ok": res.get("ok"),
                          "how": res.get("how"), "why": res.get("why")})
            if not res.get("ok"):
                out["checks"]["refine"] = {"mode": "controls", "trail": trail}
                out.update(status="failed", why=f"refine: {res.get('why')}")
                return T._done(out, t0)
        out["checks"]["refine"] = {"mode": "controls", "trail": trail}
    by_date = v15 and any(a.get("kind") == "sort" and str(a.get("key") or "").startswith("date") for a in applied)
    read, j, items = await _read_list(page, tool, value, out, (), result_wait_s, with_dom=by_date)
    blk = await T._blocked(page, read)
    out["checks"]["result_page"] = {k: v for k, v in j.items() if k not in ("el", "results")}
    if blk:
        out.update(status="blocked", why=blk)
        return T._done(out, t0)
    if j["status"] == "failed":
        out.update(status="failed", why=j["why"])
        return T._done(out, t0)
    if j["status"] == "no_results":
        out.update(status="no_results", output={"no_results": True, "applied": applied}, why=j["why"])
        return T._done(out, t0)
    rows = items[:int(tool.get("top_k") or FILTER_TOP_K)]
    dates = markup_dates(read.get("dom"), str(read.get("url") or ""), rows) if by_date else {}
    verdicts = []
    worst = "ok"
    loaded = str(read.get("url") or "")
    for a in applied:
        if a["kind"] == "sort":
            st, why, ev = check_sorted(rows, a.get("key") or "", dates, v15=v15)
        else:
            st, why, ev = check_filter(rows, read, a, loaded)
        verdicts.append({"applied": {k: a.get(k) for k in ("kind", "label", "key", "param", "value", "min", "max")
                                     if a.get(k) not in (None, "")}, "status": st, "why": why, "evidence": ev})
        worst = "failed" if "failed" in (worst, st) else "incomplete" if "incomplete" in (worst, st) else "ok"
    out["checks"]["refined"] = verdicts
    res = []
    for it in rows:
        r = _row(it)
        p = card_price(item_text(it))
        d = row_date(it, dates, v15=v15)
        if p is not None:
            r["price"] = p
        if d is not None:
            r["date"] = "%04d-%02d-%02d" % d
        res.append(r)
    output = {"results": res, "applied": [v["applied"] for v in verdicts]}
    if j["status"] == "incomplete":
        worst = "failed" if worst == "failed" else "incomplete"
        out["missing"] = j.get("missing")
    if worst == "failed":
        bad = next(v for v in verdicts if v["status"] == "failed") if any(v["status"] == "failed" for v in verdicts) else None
        out.update(status="failed", why=bad["why"] if bad else j["why"], fields_seen=output)
    elif worst == "incomplete":
        bad = next((v for v in verdicts if v["status"] == "incomplete"), None)
        out.update(status="incomplete", why=bad["why"] if bad else j["why"], output=dict(output, incomplete=True))
    else:
        out.update(status="ok", output=output, why="; ".join(v["why"] for v in verdicts))
        from anyuse.execution import attribute_verify as AV
        if AV.enabled():
            # the refined list is verified on the refined page the run ends on
            out = await AV.verify_run(page, dict(tool, workflow="filtered_search"), value, out)
    return T._done(out, t0)


async def _run_paged(page, tool, value, settle, out, t0, exclude, result_wait_s, params) -> Dict[str, Any]:
    from anyuse.execution import demo_tool as T
    done = await _first_list(page, tool, value, out, t0, exclude, result_wait_s)
    if done:
        return done
    read, j, items = out.pop("_page1")
    from anyuse.execution import attribute_verify as AV
    # with ANYUSE_ATTRIBUTE_VERIFY on, each result page is kept as the run read it
    # (local reads, no request) and every row is verified on its own page at the end (AV.verify_paged)
    av_on = AV.enabled()
    av_pages = {1: await AV.read_page(page)} if av_on else {}
    n = max(1, min(MAX_N, int(params.get("n") or tool.get("n") or 20)))
    max_pages = int(tool.get("max_pages") or MAX_PAGES)
    pager = tool.get("pager") or {}
    url1 = str(read.get("url") or "")
    # a version-15 tool reads the total beside the page's quoted echo of the input and the next page by the number its
    # URL increments; an older tool reads its pages exactly as it did
    v15 = C.v15(tool)
    total = stated_total(read.get("main_text") or read.get("text"), value if v15 else None)
    if total is not None and total < len([it for it in items if it.get("url")]):
        # a number smaller than page 1's own list is no total of the results (a cart count, a filter's count)
        total = None
    broken = False
    broken_cards = j["status"] == "incomplete"
    collected: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    dups: List[Dict[str, Any]] = []
    pages_log: List[Dict[str, Any]] = []
    visited = [url1]
    unread: List[Any] = list((j.get("unread") or []))
    end = ""
    p = 1
    cur_items = items
    while True:
        new = 0
        for it in cur_items:
            if not it.get("url"):
                continue
            key = C._norm(it["url"])
            if key in seen:
                dups.append({"page": p, "url": it["url"], "first_page": seen[key]})
                continue
            seen[key] = p
            if len(collected) < n:
                collected.append(dict(_row(it), page=p))
            new += 1
        pages_log.append({"page": p, "url": str(read.get("url") or "")[:300], "items": len(cur_items), "new": new})
        if len(collected) >= n:
            end = "n"
            break
        if p >= max_pages:
            end = "page cap"
            break
        if p > 1 and new == 0:
            end = "last page (it repeats the earlier results)"
            break
        if total is not None and len(seen) >= total and \
                next_link(read, str(tool.get("site") or ""), p, visited, len(cur_items), v15=v15) is None:
            # every result the site states is read and the page shows no next page: no request past the end
            end = "last page (the stated total is read)"
            break
        if pager.get("mode") in ("param", "path_append", "path_segment") and not next_control(read, str(tool.get("site") or ""),
                                                                                         p, visited, v15=v15):
            # (one page of 18 results, whose "page 2" URL served page 1 again): a page that shows no next-page
            # control at all is the last one - no request past the end
            end = "last page (no next-page control)"
            break
        nxt = page_url(url1, pager, p + 1) if pager.get("mode") in ("param", "path_append", "path_segment") else ""
        if not nxt:
            el = next_link(read, str(tool.get("site") or ""), p, visited, len(cur_items), v15=v15)
            if el is None:
                end = "last page (no next-page link)"
                break
            nxt = str(el.get("href"))
        if any(C._norm(nxt) == C._norm(v) for v in visited):
            end = "last page (the next page is one already read)"
            break
        visited.append(nxt)
        await page.goto(nxt)
        await settle(page)
        p += 1
        read, jj, cur_items = await _read_list(page, tool, value, out, (), result_wait_s)
        if av_on:
            av_pages[p] = await AV.read_page(page)
        blk = await T._blocked(page, read)
        if blk:
            out.update(status="blocked", why=blk)
            return T._done(out, t0)
        if jj["status"] == "failed":
            # a later page that is an error page, or no list: the collection is cut short - never silently
            pages_log.append({"page": p, "url": str(read.get("url") or "")[:300], "items": 0, "new": 0,
                              "status": "failed", "why": str(jj.get("why"))[:160]})
            end = f"page {p} failed ({str(jj.get('why'))[:120]})"
            broken = True
            break
        if jj["status"] == "no_results" or not cur_items:
            pages_log.append({"page": p, "url": str(read.get("url") or "")[:300], "items": 0, "new": 0,
                              "status": jj["status"]})
            end = f"last page (page {p} lists no result)"
            break
        if jj["status"] == "incomplete":
            broken_cards = True
        unread += list(jj.get("unread") or [])
    checks: Dict[str, Any] = {"pages": pages_log, "end": end, "duplicates": dups[:20], "stated_total": total,
                              "collected": len(collected)}
    out["checks"]["paged"] = checks
    output = {"results": collected, "total": total, "pages": len(pages_log)}
    keys = [C._norm(r["url"]) for r in collected]
    order_ok = all(a["page"] <= b["page"] for a, b in zip(collected, collected[1:]))
    if len(set(keys)) != len(keys) or not order_ok:
        out.update(status="failed", why="paged: the collected list has duplicates or breaks the page order")
        return T._done(out, t0)
    if broken or broken_cards:
        out.update(status="incomplete", output=dict(output, incomplete=True),
                   why=(f"paged: {end}" if broken else "paged: a result card the compiled structure cannot read") +
                   f" ({len(collected)} results read)")
        if unread:
            out["missing"] = unread[:12]
        return T._done(out, t0)
    if total is not None and len(collected) > total:
        out.update(status="failed", why=f"paged: {len(collected)} results read, more than the site's stated {total}",
                   fields_seen=output)
        return T._done(out, t0)
    if total is not None and end.startswith("last page") and len(collected) < min(total, n):
        out.update(status="incomplete", why=(f"paged: the site states {total} results, {len(collected)} were read "
                                             f"before the {end}"), output=dict(output, incomplete=True))
        return T._done(out, t0)
    # a page that only repeats the earlier results ended the run as the last page (a site serving its last page again):
    # its repeats are no overlap; the pages that mix new and repeated results are
    full = {pl["page"] for pl in pages_log if pl["page"] > 1 and pl.get("items") and not pl.get("new")}
    partial = [d for d in dups if d["page"] > 1 and d["page"] not in full]
    checks["overlap"] = len(partial)
    if partial and len(partial) >= max(3, len(collected) // 4):
        out.update(status="incomplete", why=f"paged: the pages overlap ({len(partial)} results repeated): the site's order "
                                             f"is not stable", output=dict(output, incomplete=True))
        return T._done(out, t0)
    if end == "page cap" and len(collected) < n:
        out.update(status="incomplete", why=f"paged: {len(collected)} of {n} results within the {max_pages}-page cap",
                   output=dict(output, incomplete=True))
        return T._done(out, t0)
    out.update(status="ok", output=output, why=f"paged: {len(collected)} results from {len(pages_log)} page(s) ({end})")
    if av_on:
        v = AV.verify_paged(output, av_pages, AV.default_resolver(), AV.scope_hosts(tool))
        out = AV.withhold(out, v, "answer")
    return T._done(out, t0)


def check_item(prod: Dict[str, Any], fields: Dict[str, Any], title: str, site: str, v15: bool = False
               ) -> Tuple[bool, str, Dict[str, Any]]:
    """visit_results: an opened result's identity and fields - the product-lookup check (``check_product``: an item
    page, its name / heading / title naming >= 75 % of the RESULT TITLE's words, a positive price shown, a same-site
    URL, by the registrable domain for a version-15 tool, ``v15``); a result title with extra words (a tile's tagline)
    passes when the title names >= 75 % of the product's OWN name's words and the page's other checks hold (recorded
    as ``scored_on: "the product's name"``)."""
    ok, why, checks = C.check_product(prod, fields, title, site, (), v9=True, v15=v15)
    if ok or not why.startswith("identity: the product page names"):
        return ok, why, checks
    name = str(fields.get("name") or "")
    if name and C.name_share9(title, name) >= C.MIN_NAME_SHARE:
        ok2, why2, ch2 = C.check_product(prod, fields, name, site, (), v9=True, v15=v15)
        if ok2:
            ch2["identity"] = dict(ch2.get("identity") or {}, scored_on="the product's name",
                                   title_names_share=round(C.name_share9(title, name), 2))
            return ok2, why2, ch2
    return ok, why, checks


async def _run_visit(page, tool, value, settle, out, t0, exclude, result_wait_s, params) -> Dict[str, Any]:
    from anyuse.execution import demo_tool as T
    from anyuse.execution import web_arch_run as RR
    done = await _first_list(page, tool, value, out, t0, exclude, result_wait_s)
    if done:
        return done
    read, j, items = out.pop("_page1")
    from anyuse.execution import attribute_verify as AV
    # with ANYUSE_ATTRIBUTE_VERIFY on, the list page and each opened item page are kept as the run read them (local
    # reads, no request) and every opened row is verified on its own page at the end (AV.verify_visit)
    av_on = AV.enabled() and AV.VISIT_CHECK
    list_pg = await AV.read_page(page) if av_on else None
    item_pgs: Dict[int, Any] = {}
    opened: Dict[int, str] = {}
    k = max(1, min(MAX_VISIT, int(params.get("k") or tool.get("k") or 2)))
    top = [it for it in items if it.get("url")][:k]
    miss = C.unread_before(items, j.get("unread") or [], k) if j["status"] == "incomplete" else []
    site = str(tool.get("site") or "")
    srcs = tool.get("fields") or {}
    rows: List[Dict[str, Any]] = []
    for rank, it in enumerate(top, start=1):
        row: Dict[str, Any] = {"rank": rank, "title": str(it.get("title") or "")[:200], "url": it["url"]}
        opened[rank] = it["url"]
        res = await RR.open_link(page, {"href": it["url"], "text": it.get("title"), "i": it.get("i")}, settle)
        if not res.get("ok"):
            rows.append(dict(row, status="failed", why=f"membership: the result did not open ({res.get('why')})"))
            continue
        prod = await T.read_full(page, own_microdata=True, struck=True, sep=True)
        out["pages"].append(T.page_record(prod))
        blk = await T._blocked(page, prod)
        if blk:
            rows.append(dict(row, status="blocked", why=blk))
            out["checks"]["items"] = rows
            out.update(status="blocked", why=blk)
            return T._done(out, t0)
        if av_on:
            item_pgs[rank] = await AV.read_page(page)
        title = str(it.get("title") or "")
        fields = C.extract_fields(prod, srcs, title)
        ok, why, checks = check_item(prod, fields, title, site, v15=C.v15(tool))
        row.update({kk: fields.get(kk) for kk in ("name", "price", "currency", "availability")})
        if ok and fields.get("url"):
            # the answer's URL: the URL loaded (the canonical link only when it names the same product; version 9)
            row["url"] = fields["url"]
        row["checks"] = checks
        if not ok:
            rows.append(dict(row, status="failed", why=why))
            continue
        conf = C.field_conflicts(prod, fields, srcs, title, v8=True)
        if conf:
            rows.append(dict(row, status="conflict", why=T.conflict_why(conf), conflicts=conf,
                             **{f: T.CONFLICT for f in conf}))
            continue
        av, ev = C.availability_answer(prod, fields, srcs, title)
        if ev:
            row["availability"] = av
            row["checks"] = dict(checks, availability=ev)
        rows.append(dict(row, status="ok", why=why))
    if av_on:
        verdicts = AV.verify_visit({"items": rows}, list_pg, item_pgs, opened, AV.default_resolver(),
                                   AV.scope_hosts(tool), listed=[it["url"] for it in items if it.get("url")])
        for r in rows:
            v = verdicts.get(r["rank"])
            if v is None:
                continue
            r["checks"] = dict(r.get("checks") or {}, attribute_verify=v.to_dict())
            out["model_calls"] = int(out.get("model_calls") or 0) + v.model_calls
            if v.verdict != AV.ACCEPT:
                # withheld as AV.withhold withholds an answer: the values stay as evidence, never as the answer
                r["unverified"] = {kk: r.get(kk) for kk in ("name", "price", "currency", "availability")}
                r.update(name=None, price=None, currency=None, availability=None,
                         status=AV.NEEDS_REVIEW if v.verdict == AV.NEEDS_REVIEW else "withheld",
                         why=f"attribute verify: {v.why()}"[:300])
        if verdicts:
            # the table's verdict, calls and spend in one place, as a single answer's are (AV.withhold)
            per_row = {rank: v.verdict for rank, v in verdicts.items()}
            out["checks"]["attribute_verify"] = {
                "verdict": next((x for x in (AV.WITHHOLD, AV.NEEDS_REVIEW) if x in per_row.values()), AV.ACCEPT),
                "where": "visit", "rows": per_row, "model_calls": sum(v.model_calls for v in verdicts.values()),
                "usd": round(sum(v.usd for v in verdicts.values()), 6)}
    out["checks"]["items"] = [{kk: r.get(kk) for kk in ("rank", "url", "status", "why", "checks")} for r in rows]
    table = [dict({kk: r.get(kk) for kk in ("rank", "title", "url", "name", "price", "currency", "availability",
                                            "status", "why")}, **({"unverified": r["unverified"]}
                                                                  if r.get("unverified") else {}))
             for r in rows]
    output = {"items": table, "k": k, "listed": len([it for it in items if it.get("url")])}
    good = sum(1 for r in rows if r["status"] == "ok")
    if miss:
        # a result among the top k the compiled structure cannot read: the table is INCOMPLETE, never silently short
        out["missing"] = miss
        out.update(status="incomplete", output=dict(output, incomplete=True),
                   why=f"visit: {len(miss)} result(s) among the top {k} could not be read ({miss[0].get('why')})")
        return T._done(out, t0)
    if rows and good == len(rows):
        out.update(status="ok", output=output, why=f"visit: {good} of {len(rows)} results read and checked")
    elif good:
        out.update(status="incomplete", output=dict(output, incomplete=True),
                   why=f"visit: {good} of {len(rows)} results read and checked; " +
                   "; ".join(f"#{r['rank']} {r['status']}: {r['why']}" for r in rows if r["status"] != "ok")[:300])
    else:
        out.update(status="failed", fields_seen=output, why="visit: no result read and checked; " +
                   "; ".join(f"#{r['rank']} {r['status']}: {r['why']}" for r in rows)[:300])
    return T._done(out, t0)
