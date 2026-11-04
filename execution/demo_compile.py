"""One-demonstration compilation. PURE.

The product claim this module serves: *an agent demonstrates a workflow once, and
AnyUse compiles it into a checked tool, or returns an explicit failure.*

Input: ONE recorded demonstration trace (``execution/demo_policy.py`` records it):
the actions a demonstrator took and the observations of the pages it saw. The
compiler never sees the demonstrator's rationale (it is not recorded) and never a
held-out value. Output: a tool spec with an **explicit input binding** (the search
text is the tool's one input, ``term``) and the **runtime checks** that every
invocation runs (``execution/demo_tool.py``), or ``{"ok": False, "why": ...}``.

Two templated workflows (no authoring):

* ``product_lookup`` (shops): search the input term, open the best-matching product,
  return its name, price, availability and URL;
* ``site_search`` (other sites): search the input term, return the top result
  titles and URLs.

What is compiled from the one trace:

1. **the search**: the demonstrated search step that entered the input value; the
   result URL the site produced for it becomes the tool's search when it carries the
   value (``mode=url``, the template with ``{q}``), else the typed search into the
   same input (``mode=typed``) with the site's own search URLs seen on the start page
   as the fallback (a search whose Enter reloads the start page);
2. **the result region**: the link cluster of the result page that holds the item the
   demonstrator opened (product lookup) or the results it reported (site search),
   as a URL pattern (a regex over the page's own links);
3. **the selection** (product lookup): the region item whose title names the input
   best (all its content words; else >= 75 % of them and a unique best);
4. **the field sources** (product lookup): where the product page states the name,
   price, availability and canonical URL (schema.org JSON-LD, microdata, Open
   Graph, the heading, a text statement), located with the values the demonstrator
   reported, verified against the page;
5. **the checks** every invocation runs: result membership (the output comes from the
   compiled region of THIS result page), the query (the list names the value, or the
   chosen item names it), product identity (the product page names the value, it is
   an item page, not a list), field sanity (a positive price the page shows, a
   same-site URL), and the empty state (the page's own "no results" statement and no
   item naming the value) - else an explicit failure, never a guess.

Nothing here contacts a site or asks a model; ``test_demo_compile.py`` drives it with
fakes.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import model_check as MC
from anyuse.execution import unicode_text as U
from anyuse.execution import web_arch as W

WORKFLOWS = ("product_lookup", "site_search")
#: version 10's read-only BREADTH workflows (``demo_breadth``): a filtered / sorted search, a paged search (top N across
#: pages), a search whose top K results are visited
BREADTH_WORKFLOWS = ("filtered_search", "paged_search", "visit_results")
#: the demonstrator's step cap (one attempt)
MAX_STEPS = 10
#: the observation's visible-text excerpt sent to the demonstrator (characters)
OBS_TEXT_CHARS = 6000
#: the observation's element list (search inputs first)
OBS_ELEMENTS = 120
#: the share of the input's content words a chosen item / product must name when no
#: item names all of them
MIN_NAME_SHARE = 0.75
#: results a site-search tool returns
TOP_K = 5
#: 5: the result region's CARDS (``demo_cards``): one result per result container.
#: 6 (web-watch): the runtime checks of docs/WATCH.md (a structured-vs-visible field CONFLICT, ...).
#: 7: the CLOSEST of several results naming every word
#: of the input (a tie is "ambiguous", never a guess), a feed is never a result page, a search that lands on
#: the product's own page, a value that repeats a tile's title with its subtitle.
#: 8: the CURRENT price next to a regular / struck one (a
#: structured price that disagrees with it is a CONFLICT), the unavailable phrases of the 11 languages, a
#: structured availability the visible page does not support reported as the visible statement or "not stated",
#: a result structure that is a sidebar / widget or reads the same items for any query never compiled, a title as
#: the visible text of its title link without UI affixes.
#: 9: the answer's URL is the URL loaded (the
#: canonical link only when it names the same product), the input's distinguishing words (a colour, a number, a
#: size, a model code) are mandatory in the identity, visible statements read with separators at element
#: boundaries, space-insensitive token matching, an absent producer word confirmed by the page, the "on order"
#: phrases of the 11 languages.
#: 10: a stock-state legend is not a statement, a
#: container whose id / class names a sidebar is a widget, more delivery-time wording; and three read-only workflow
#: types beyond the two above - a FILTERED / SORTED search, a PAGINATED search (top N across pages) and a search
#: whose top K results are VISITED (each read with the product-lookup reader).
#: 11: general fixes of the break and wrong-output classes of drifted sites
#: (each keyed on a version-11 compile's own keys or on ``v11(tool)``).
#: 12: general fixes of the remaining break classes and of a promo block of cloned result cards
#: (each keyed on a version-12 compile's own keys or on ``v12(tool)``).
#: 14: the words of every script: a title in Cyrillic, Greek, Arabic, Chinese, Japanese, Korean, Thai, ...
#: has tokens (``web_arch.tokens_of_folded``) with the marks of letters without case (``web_arch.fold_marked``:
#: ガイド is not カイト), digits of every script read 0-9, and a word of a script without
#: spaces is named by a token it starts (東京 by 東京駅, never 京都 by 東京都). An older tool reads only the a-z
#: and 0-9 runs it read before (``web_arch.scripts``).
#: Also 14: a search the site redirects to a landing page replays the search URL the site formed, and a typed
#: search records the submit control it used and submits the input's own GET form when Enter and the control do
#: nothing (each keyed on the tool's recorded version).
#: 15: the site's own evidence before the hand-written word lists, which stay as the last step: "the same site" is
#: the registrable domain (``web_scope.registrable``); a link text the page gives one target only is that target's
#: name, whatever word it holds, and a text it gives several targets is a label; the parameter that carried the typed
#: input is the site's search parameter whatever its name; an empty or error page is read in the site's learned pages
#: and the statement rule of every script first; and the product's fields read the site's markup and the differential
#: between its results first (each keyed on ``v15(tool)`` or on a version-15 compile's own keys; ``web_arch.site_evidence``
#: turns the web_arch rules on).
#: A tool records the version it was compiled for, and the runtime keys every newer check on it: a
#: version-5 tool runs exactly version 5's runtime, a version-6 tool exactly version 6's, and so on.
#: ``compile_trace(trace, version=7)`` ... ``version=14`` still build exactly that version's tool (the benches of
#: versions 7-9 pin them).
TOOL_VERSION = 15
#: the versions ``compile_trace`` can build (an older one is frozen code, never rebuilt here)
COMPILE_VERSIONS = (7, 8, 9, 10, 11, 12, 13, 14, 15)


#: a new compile's card specs are ``loose`` (``demo_cards.chain_matches``) when that passes its checks
CARDS_LOOSE = True
#: the card read a version-6 compile keeps (``region["cards_version"]``; ``cards_v6``)
CARDS_VERSION = 6
#: the card read a version-8 compile keeps: version 6's plus each title link's VISIBLE text
#: (``demo_cards.CARDS_JS_V8``)
CARDS_VERSION_V8 = 8
#: the card read a version-11 compile keeps: version 10's (each card's whole text) plus version 11's title rule - a title
#: link whose visible text is no title takes the card's other links to the result
CARDS_VERSION_V11 = 11
#: the card read a version-12 compile keeps: version 11's plus each card's promo flag - a card in a promotion /
#: advertising / recommendation block beside the results is never a result
CARDS_VERSION_V12 = 12
#: the card read a version-15 compile keeps: version 12's in the page, and in the reader the page's own evidence first -
#: a link text the page gives several targets is a label, never a title, and so is a text the page marks as a date; a
#: UI affix every title shares is no part of a title; a link parameter only one card carries, or with another value
#: than the page's other results, marks a placement
CARDS_VERSION_V15 = 15


def v6(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run the web-watch runtime (compiled at version >= 6)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 6
    except (TypeError, ValueError):
        return False


def v7(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 7's checks (compiled at version >= 7)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 7
    except (TypeError, ValueError):
        return False


def v8(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 8's checks (compiled at version >= 8)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 8
    except (TypeError, ValueError):
        return False


def v9(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 9's checks (compiled at version >= 9)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 9
    except (TypeError, ValueError):
        return False


def v10(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 10's checks (compiled at version >= 10)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 10
    except (TypeError, ValueError):
        return False


def v11(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 11's checks (compiled at version >= 11)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 11
    except (TypeError, ValueError):
        return False


def v12(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 12's checks (compiled at version >= 12)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 12
    except (TypeError, ValueError):
        return False


def v13(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 13's checks (compiled at version >= 13)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 13
    except (TypeError, ValueError):
        return False


def v14(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 14's checks (compiled at version >= 14)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 14
    except (TypeError, ValueError):
        return False


def v15(tool: Optional[Dict[str, Any]]) -> bool:
    """Does ``tool`` run version 15's checks (compiled at version >= 15)?"""
    try:
        return int((tool or {}).get("version") or 0) >= 15
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# the workflows' goals (the demonstrator's task text; templated, no authoring)
# ---------------------------------------------------------------------------

def goal_text(workflow: str, value: str, k: int = 2) -> str:
    if workflow in ("filtered_search", "paged_search", "visit_results"):
        # version 10: the breadth workflows' goals (``demo_breadth.goal_text``)
        from anyuse.execution import demo_breadth as DB
        return DB.goal_text(workflow, value, k=k)
    if workflow == "product_lookup":
        return (f"Look up the product \"{value}\" on this website: search for it with the site's own search, "
                f"open the best-matching product's own page, and report its name, price (with currency), "
                f"availability and the page URL. If the search shows no results, report no_results.")
    if workflow == "site_search":
        return (f"Search this website for \"{value}\" with the site's own search and report the top results "
                f"(up to {TOP_K}) with their titles and URLs, as the results page lists them. If the search "
                f"shows no results, report no_results.")
    raise ValueError(f"unknown workflow {workflow!r}")


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def name_share(text: Any, value: Any) -> float:
    """The share of ``value``'s content words that ``text`` names (whole tokens, a
    trailing plural 's' tolerated, a compound word of >= 4 letters counts)."""
    want = W.content_tokens(value)
    if not want:
        return 0.0
    return sum(1 for w in want if W.names_all_compound(text, w)) / len(want)


def names_value(text: Any, value: Any) -> bool:
    return bool(W.content_tokens(value)) and W.names_all_compound(text, value)


#: version 9: letters NFKD does not decompose (Polish "ł", Nordic "ø" / "æ", ...): ``web_arch.fold`` keeps them, so
#: "Podkładka" read as the two tokens "podk" "adka" and "Wysyłka" never matched "wysylka"
_FOLD9 = {"ł": "l", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ŀ": "l"}


def _map9(s: Any) -> str:
    """``s`` with the letters NFKD keeps mapped to their base letters (``_FOLD9``)."""
    t = str(s or "")
    if any(c.lower() in _FOLD9 for c in t):
        t = "".join(_FOLD9.get(c.lower(), c) for c in t)
    return t


def fold9(s: Any) -> str:
    """Version 9: ``web_arch.fold`` with the letters NFKD keeps mapped to their base letters (``_FOLD9``)."""
    return W.fold(_map9(s))


def tokens9(s: Any) -> List[str]:
    """Version 9: the folded tokens (``fold9``); version 14: the letters of every script too, with the marks of
    letters without case (``web_arch.tokens_of_folded`` of ``web_arch.fold_marked``), wherever ``web_arch.scripts``
    turns that on."""
    f = fold9(s)
    if not W.every_script():
        return re.findall(r"[0-9a-z]+", f)
    return W.tokens_of_folded(f if f.isascii() else W.fold_marked(_map9(s)))


def content9(s: Any) -> List[str]:
    """Version 9: the content tokens (``web_arch.content_tokens`` on ``fold9``)."""
    return [t for t in tokens9(s) if t not in W.STOPWORDS and (U.match_length(t) >= 2 or t.isdigit())]


#: a page stating that the search found nothing, in these languages (folded:
#: lower case, accents removed; EN DE FR ES IT NL SV DA NO FI PL PT CS)
#: nouns a CART widget counts ("0 Artikel", "No items in your basket"): never a "no results" statement
CART_NOUNS = r"(?:items?|products?|articles?|artikel|produkte?|produits?|articulos|productos|prodotti|articoli|producten|" \
    r"artikelen|produkter|varor|varer|tuotetta|tuotteita|produktow|pozycji|stuck|stueck)"
NO_RESULTS = re.compile(
    r"\b(?:"
    r"no (?:search )?results?(?: (?:were|was))?(?: found)?|no (?:matches|hits)(?: found)?"
    r"|no matching (?:results?|items|products|entries)|nothing (?:was )?found|found no (?:results?|matches)"
    r"|(?:0|zero) (?:search )?results?|(?:0|zero) (?:matches|hits)|we found 0|did(?:nt| not) (?:match|find) any"
    r"|your search .{0,60}?did(?:nt| not) (?:match|return)|could(?:nt| not) find any|sorry,? we could(?:nt| not) find"
    r"|there are no (?:results|products|items) (?:for|matching)|returned no results"
    r"|keine (?:ergebnisse|treffer|suchergebnisse)|0 (?:treffer|ergebnisse)"
    r"|(?:leider )?nichts gefunden|ergab keine|keine passenden|kein ergebnis|kein treffer|keine uebereinstimmung"
    r"|aucun resultat|0 resultats?|n.a donne aucun|pas de resultat|aucune correspondance"
    r"|sin resultados|no se (?:han )?encontr|no hay resultados|0 resultados|ningun resultado|no hemos encontrado"
    r"|nessun risultato|0 risultati|non ha prodotto (?:alcun )?risultat|non abbiamo trovato"
    r"|geen (?:resultaten|zoekresultaten)|0 resultaten|niets gevonden|leverde geen|niks gevonden"
    r"|inga (?:resultat|traffar|sokresultat)|0 (?:traffar|resultat)|hittade inga|gav inga|inget resultat"
    r"|ingen (?:resultater|sogeresultater|treff)|0 (?:resultater|treff)|fandt ingen|fant ingen|ga ingen"
    r"|ei (?:tuloksia|hakutuloksia|osumia)|0 tulosta|hakusi ei tuottanut"
    r"|brak wynikow|0 wynikow|nie znaleziono|nie znalezlismy"
    r"|nenhum resultado|nao encontramos|nao foram encontrados"
    r"|zadne vysledky|nebyly nalezeny|nic nenalezeno"
    # "no <things> found" in each language ("geen recepten gevonden", "nessuna ricetta trovata")
    r"|no \w+ (?:were |was )?found|0 \w+ found|we (?:couldnt|could not|did not|didnt) find (?:any|anything)"
    r"|keine \w+ gefunden|0 \w+ gefunden|konnten (?:leider )?keine"
    r"|geen \w+ gevonden|0 \w+ gevonden"
    r"|aucun\w* \w+ (?:n.a ete )?trouv|0 \w+ trouv"
    r"|ningun\w* \w+ encontrad|0 \w+ encontrad"
    r"|nessun\w* \w+ trovat|0 \w+ trovat|non (?:e stato|sono stati) trovat"
    r"|inga \w+ hittades|0 \w+ hittades|ingen \w+ (?:fundet|funnet|fundne)|0 \w+ (?:fundet|funnet)"
    r"|ei \w+ loytynyt|0 \w+ loytyi|ei loytynyt"
    # up to two words between ("Es konnte leider nichts Passendes gefunden werden")
    r"|nichts (?:\w+ ){1,2}gefunden|keine (?:\w+ ){2}gefunden|wurden (?:leider )?keine (?:\w+ ){0,2}gefunden"
    r"|no (?:\w+ ){2}(?:were |was )?found|nothing (?:\w+ ){1,2}found|geen (?:\w+ ){2}gevonden"
    r"|aucun\w* (?:\w+ ){2}trouv|ningun\w* (?:\w+ ){2}encontrad|nessun\w* (?:\w+ ){2}trovat"
    # a count label stating zero ("Liczba przepisow: 0"); never a cart's count
    r"|(?:number of|anzahl(?: der)?|liczba|antal|aantal|nombre d.?|numero d.?|numero di|lukumaara|antall)"
    r" (?!" + CART_NOUNS + r"\b)\w+ ?: ?0(?![\d.,])"
    r")")
_ERROR_PAGE = re.compile(r"\b(?:internal server error|server error|something went wrong|an (?:unexpected )?error "
                         r"(?:has )?occurred|service (?:is )?(?:temporarily )?unavailable|bad gateway|gateway time-?out|"
                         r"too many requests|404 not found|page not found|access denied|seite nicht gefunden|"
                         r"page introuvable|pagina no encontrada|pagina non trovata|pagina niet gevonden)\b")


#: a line that states a POSITIVE result count ("1 - 30 of 7045 items", "79 Artikel gefunden", "Showing 1-12 of
#: 140 results") - the page then lists results and states no empty search, whatever another line says
_RESULT_NOUNS = r"(?:search )?(?:results?|items?|products?|articles?|hits|matches|treffer|ergebnisse|suchergebnisse|" \
    r"artikel|produkte?|resultats?|produits?|risultati|articoli|prodotti|resultaten|artikelen|producten|resultados|" \
    r"articulos|productos|resultat|traffar|produkter|resultater|treff|tulosta|wynikow|wyniki|produktow|vysledku)"
_POSITIVE_COUNT = re.compile(r"(?:^|[^\d.,])(?P<n>\d[\d.,']*)\s+" + _RESULT_NOUNS + r"\b"
                             r"|\b(?:of|von|sur|di|van|de|av|af|z)\s+(?P<m>\d[\d.,']*)\b")
_CART_LINE = re.compile(r"\b(?:cart|basket|bag|warenkorb|einkaufswagen|merkzettel|wunschliste|wishlist|panier|carrello|"
                        r"winkelwagen|winkelmand|cesta|carrito|carrinho|varukorg|kurv|handlekurv|ostoskori|koszyk|kosik|"
                        r"compare|vergleich)\b")


def positive_count_line(lines: Sequence[str]) -> str:
    """The first of ``lines`` (folded) that states a positive result count; "" when none. A cart, wish-list or
    compare line is never one."""
    for ln in lines:
        f = W.fold(ln)
        if not f or len(f) > 200 or not re.search(r"\d", f) or _CART_LINE.search(f):
            continue
        for m in _POSITIVE_COUNT.finditer(f):
            raw = m.group("n") or m.group("m") or ""
            digits = re.sub(r"[^\d]", "", raw)
            if digits and int(digits) > 0 and (m.group("n") or re.search(_RESULT_NOUNS, f)):
                return ln.strip()[:200]
    return ""


def no_results_statement(text: Any, head_chars: int = 8000) -> str:
    """The page's own "no results" statement near its top ("" when none); never on an
    error page (an error is not an empty result).

    (a cart badge "0" on one line and the heading "Search Results" on the next must not read as
    "0 search results" on a page listing 7,045 items): the statement is read inside ONE line of the visible text
    (one element's text - never a number of a neighbouring control joined to a heading), and a page that states a
    positive result count on any line states no empty search."""
    lines: List[str] = []
    size = 0
    for ln in str(text or "").split("\n"):
        if size >= head_chars:
            break
        lines.append(ln)
        size += len(ln) + 1
    if _ERROR_PAGE.search(W.fold("\n".join(lines))):
        return ""
    if positive_count_line(lines):
        return ""
    for ln in lines:
        m = NO_RESULTS.search(W.fold(ln))
        if m:
            return m.group(0)
    return ""


def error_statement(text: Any, head_chars: int = 3000) -> str:
    m = _ERROR_PAGE.search(W.fold(text)[:head_chars])
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# version 15: an empty or error result page read in the site's own evidence first, the phrase lists last
# ---------------------------------------------------------------------------

#: the one question the check model is asked about a result page's line that reads as an error
ERROR_LINE15 = MC.Question(
    "result_page_error",
    "The page below should list the results of a search for '{target}'. Does the line marked >> say that "
    "the page could not be shown because of an error?")
#: lines of a result page that can state an empty search: a line naming the input and the lines just below it
EMPTY_ZONE15 = 3
#: the longest line asked about (characters)
LINE_CHARS15 = 160


def _lines15(text: Any, head_chars: int) -> List[str]:
    out: List[str] = []
    size = 0
    for ln in str(text or "").split("\n"):
        if size >= head_chars:
            break
        size += len(ln) + 1
        t = " ".join(ln.split())
        if t:
            out.append(t)
    return out


def _link_texts15(read: Dict[str, Any]) -> set:
    """The folded texts of the page's links: a line that is a link's text is a title or a control, never the
    page's own statement."""
    return {W.fold(e.get("text")) for e in read.get("elements") or []
            if isinstance(e, dict) and e.get("tag") == "a" and W.fold(e.get("text"))}


def _ask15(question: Any, value: str, read: Dict[str, Any], lines: Sequence[str], picked: Sequence[int],
           where: str) -> Any:
    """``model_check.check`` about the lines ``picked`` (with the two lines above each), verified against the
    page's lines; asks nothing outside a model-check scope."""
    cands = [MC.Candidate(text=lines[k], context="\n".join(lines[max(0, k - 2):k]), ref=k)
             for k in picked[:MC.MAX_CANDIDATES]]
    shown = set(lines)
    text = str(read.get("text") or read.get("main_text") or "")
    return MC.check(question, value, cands, verify=lambda c: c.text in shown,
                    guard_obs={"url": str(read.get("url") or ""), "text": text[:4000], "items": []}, where=where)


def error_line15(tool: Dict[str, Any], read: Dict[str, Any], value: str,
                 head_chars: int = 3000) -> Tuple[str, Dict[str, Any]]:
    """Version 15: the line by which the result page reports an error, ``(line or "", evidence)``. A line the
    error phrases (``_ERROR_PAGE``) read counts only when the site's own pages do not explain it: never a line
    the site's empty page and its result pages share (``tool["empty_page"]``'s ``shared`` lines: the site's chrome),
    never a line that holds a link's text holding the phrase (a result's title; a link elsewhere on the page
    excuses no other line); then the check model in an open model-check scope (a yes is the error, a no for every
    such line is none); the phrases decide what is left."""
    from anyuse.execution import empty_page as EP
    lines = _lines15(read.get("text"), head_chars)
    hits = [k for k, ln in enumerate(lines) if _ERROR_PAGE.search(W.fold(ln))]
    ev: Dict[str, Any] = {}
    if not hits:
        return "", ev
    sig = tool.get("empty_page")
    shared = set(sig.get("shared") or []) if EP.usable(sig) else set()
    links = _link_texts15(read)
    left: List[int] = []
    for k in hits:
        ln = lines[k]
        fl = W.fold(ln)
        phrase = _ERROR_PAGE.search(fl).group(0)
        if shared and EP._h(EP.mask(ln, value)) in shared:
            ev.setdefault("site_chrome", []).append(ln[:120])
        elif any(phrase in t and t in fl for t in links):
            ev.setdefault("in_a_link", []).append(ln[:120])
        else:
            left.append(k)
    if not left:
        return "", ev
    asked = [k for k in left if len(lines[k]) <= LINE_CHARS15]
    if asked and MC.available(ERROR_LINE15.kind):
        v = _ask15(ERROR_LINE15, value, read, lines, asked, "demo_compile.error_line15")
        ev["model"] = v.to_dict()
        if v.yes and v.candidate is not None:
            return v.candidate.text, dict(ev, by="model_check")
        if v.no and len(asked) == len(left) and len(asked) <= MC.MAX_CANDIDATES:
            return "", dict(ev, by="model_check")
    return _ERROR_PAGE.search(W.fold(lines[left[0]])).group(0), dict(ev, by="page_statement")


def _empty_zone15(lines: Sequence[str], value: str, links: set) -> Tuple[List[int], bool]:
    """The lines that can state that the search for ``value`` found nothing: every line naming it and up to
    ``EMPTY_ZONE15`` lines below it, stopping at a line naming it again or a link's text; ``complete`` when every
    line of the zone is a candidate (a "no" for all of them covers each)."""
    zone: List[int] = []
    complete = True
    for i, t in enumerate(lines):
        if not names_value(t, value):
            continue
        if i not in zone:
            zone.append(i)
        for j in range(i + 1, min(len(lines), i + 1 + EMPTY_ZONE15)):
            if names_value(lines[j], value):
                break
            if W.fold(lines[j]) in links:
                complete = False
                break
            if j not in zone:
                zone.append(j)
    picked = [k for k in zone if len(lines[k]) <= LINE_CHARS15 and W.fold(lines[k]) not in links
              and any(ch.isalnum() for ch in lines[k])]
    if len(picked) < len(zone) or len(picked) > MC.MAX_CANDIDATES:
        complete = False
    return picked, complete


def empty_statement15(read: Dict[str, Any], value: str, model: bool = True) -> Tuple[str, Dict[str, Any]]:
    """Version 15: the evidence that the search for ``value`` found nothing, before the phrase list
    (``no_results_statement``): the check model about the lines that name the input (in an open model-check
    scope; ``model``), then the statement rule of every script the result contract reads, bound to the query in its
    own line, under a heading or below a search summary naming it (``result_contract.empty_evidence_page``).
    ``(line or "", evidence)``; ``evidence["refused"]``: the model read every such line as not saying so, and the
    phrase list is not asked. A page that states a positive result count on a line (``positive_count_line``) states
    no empty search, as before."""
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    ev: Dict[str, Any] = {}
    text = str(read.get("main_text") or read.get("text") or "")
    lines = _lines15(text, 8000)
    counted = positive_count_line(lines)
    if counted:
        return "", {"positive_count": counted[:120]}
    if model and MC.available(EP.EMPTY_STATEMENT.kind):
        picked, complete = _empty_zone15(lines, value, _link_texts15(read))
        if picked:
            v = _ask15(EP.EMPTY_STATEMENT, value, read, lines, picked, "demo_compile.empty_statement15")
            ev["model"] = v.to_dict()
            if v.yes and v.candidate is not None:
                return v.candidate.text, dict(ev, by=EP.BY_MODEL)
            if v.no and complete:
                return "", dict(ev, by=EP.BY_MODEL, refused=True)
    try:
        phrase, why = RC.empty_evidence_page(str(read.get("dom") or ""), value, text=str(read.get("text") or text))
    except Exception as exc:                           # noqa: BLE001 - an unreadable page states nothing
        phrase, why = "", f"the page could not be read ({type(exc).__name__})"
    if phrase:
        # in lower case, as the phrase list gives its statement (the marks of every script kept)
        return " ".join(phrase.casefold().split()), dict(ev, by=EP.BY_STATEMENT, why=why[:200])
    return "", ev


def stated_empty15(read: Dict[str, Any], value: str) -> str:
    """Version 15's empty-search evidence without a model (a demonstration's wait for its results):
    ``empty_statement15``'s statement rule, then the phrase list."""
    line, _ev = empty_statement15(read, value, model=False)
    return line or no_results_statement(read.get("main_text") or read.get("text"))


# ---------------------------------------------------------------------------
# URLs and the result region
# ---------------------------------------------------------------------------

def host_of(url: Any) -> str:
    try:
        return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def same_site(url: Any, site: str) -> bool:
    h = host_of(url)
    return bool(h) and (W._same_site(h, site) or W.same_site_href(str(url), site))


def strip_fragment(url: str) -> str:
    return str(url or "").split("#", 1)[0]


def value_template(url: str, value: str) -> str:
    """The URL with the value replaced by ``{q}`` (query parameter or path), or ""
    (as ``web_arch_live.tier1_template``, tolerant of '+'/'%20' and case)."""
    if not url or not value:
        return ""
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    fv = W.fold(value)
    if any(W.fold(v) == fv for _k, v in q):
        parts = []
        for k, v in q:
            parts.append(f"{urllib.parse.quote_plus(k)}=" + ("{q}" if W.fold(v) == fv else urllib.parse.quote_plus(v)))
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "&".join(parts), ""))
    path = urllib.parse.unquote(p.path)
    low = path.lower()
    for form in (value, value.replace(" ", "-"), value.replace(" ", "+"), value.replace(" ", "_")):
        f = form.lower()
        if f and f in low:
            i = low.index(f)
            return urllib.parse.urlunsplit((p.scheme, p.netloc, path[:i] + "{q}" + path[i + len(form):], p.query, ""))
    return ""


_NOT_SEARCH_PARAM = re.compile(r"(?i)^(e-?mail|mail|vorname|nachname|firstname|first_name|lastname|last_name|name|"
                               r"values\[firstname\]|newsletter|subscribe|phone|telefon|zip|plz)$")
_NOT_SEARCH_PATH = re.compile(r"(?i)(newsletter|subscribe|abonn|anmeld|signup|sign-up|register|login|account|contact|kontakt)")


#: how a template was found on the page that is the site's search by the page's own structure
#: (``web_arch.search_url_templates``)
STRUCTURAL_SEARCH = ("schema.org SearchAction", "GET search form")


def typed_params(url: Any, typed: Any) -> List[str]:
    """The query parameters of ``url`` whose value is the typed input (folded): the site's own search parameter,
    whatever it is called."""
    try:
        q = urllib.parse.parse_qsl(urllib.parse.urlsplit(str(url or "")).query, keep_blank_values=True)
    except ValueError:
        return []
    fv = W.fold(typed)
    return [k for k, v in q if fv and W.fold(v) == fv]


def search_template_ok(template: str, how: str = "", own: Sequence[str] = ()) -> bool:
    """A search URL seen on a page is the site's SEARCH (a newsletter GET form
    'Email={q}', 'email-subscribe={q}' or 'VORNAME={q}' was taken for the search): the
    value's query parameter is a search parameter or the path is a search path, and it
    is never an e-mail / name / newsletter field or path.

    With ``web_arch.site_evidence`` the site's own evidence comes before the names: a template the page's
    structure gives (``how`` in ``STRUCTURAL_SEARCH``) or whose parameter is one the site's own result URL carried
    the typed input in (``own``, ``typed_params``) is the search whatever its parameter is called; the e-mail /
    name / newsletter refusal stays first."""
    t = str(template or "")
    if "{q}" not in t:
        return False
    try:
        p = urllib.parse.urlsplit(t.replace("{q}", "QQQ"))
    except ValueError:
        return False
    if _NOT_SEARCH_PATH.search(p.path or ""):
        return False
    names = [k for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if v == "QQQ"]
    if any(_NOT_SEARCH_PARAM.match(k) for k in names):
        return False
    if names:
        if W.site_evidence_first() and (how in STRUCTURAL_SEARCH or any(k in set(own or ()) for k in names)):
            return True
        return any(k.lower() in W.SEARCH_PARAMS for k in names) or bool(W._SEARCH_PATH.search(p.path or "/"))
    return True


# ---------------------------------------------------------------------------
# a link back to the site's own search is never a result
# ---------------------------------------------------------------------------

_EP_MARK = "QQQSEARCHVALUEQQQ"


def _ep_path(path: Any) -> str:
    return (urllib.parse.unquote(str(path or "/")).rstrip("/") or "/").lower()


def search_endpoints(templates: Sequence[Any] = (), page_url: str = "", value: str = "") -> List[Tuple[str, str, str]]:
    """(a "did you mean" / similar-searches list must not be compiled as the results): the
    site's own search endpoints as ``(host, path, key)`` - read from search templates (the tool's, its fallbacks, the
    page's own search forms) and from the result page's URL when it carries the value. ``key`` is the query parameter
    that carries the search text; "" for a template with the value in its path (the path before the value is the
    endpoint; a template whose whole path is the value gives none)."""
    ts = [str(t) for t in templates if t and "{q}" in str(t)]
    if page_url and value:
        t = value_template(page_url, value)
        if t:
            ts.append(t)
    out: List[Tuple[str, str, str]] = []
    for t in ts:
        try:
            p = urllib.parse.urlsplit(t.replace("{q}", _EP_MARK))
        except ValueError:
            continue
        host = (p.hostname or "").lower()
        keys = [k for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if _EP_MARK in v]
        if keys:
            ep = (host, _ep_path(p.path), keys[0])
        elif _EP_MARK in p.path:
            pre = urllib.parse.unquote(p.path.split(_EP_MARK, 1)[0]).lower()
            if not pre.strip("/"):
                continue
            ep = (host, pre, "")
        else:
            continue
        if ep not in out:
            out.append(ep)
    return out


def search_link(href: Any, endpoints: Sequence[Tuple[str, str, str]]) -> bool:
    """Does ``href`` open the site's own search (a did-you-mean, suggestion, related-search or pagination link
    of a search: the endpoint's path with its search parameter set, or a path below a path-template endpoint)? Such a
    link is never a result."""
    if not endpoints:
        return False
    try:
        p = urllib.parse.urlsplit(str(href or ""))
    except ValueError:
        return False
    h = (p.hostname or "").lower()
    for host, path, key in endpoints:
        if host and h and h != host and not W._same_site(h, host):
            continue
        if key:
            if _ep_path(p.path) == path and any(k == key and v.strip() for k, v in
                                                urllib.parse.parse_qsl(p.query, keep_blank_values=True)):
                return True
        else:
            low = urllib.parse.unquote(p.path or "").lower()
            if low.startswith(path) and low[len(path):].strip("/"):
                return True
    return False


def tool_templates(tool: Dict[str, Any]) -> List[str]:
    """The search templates a tool knows (its URL template and its fallbacks)."""
    s = tool.get("search") or {}
    return [str(t) for t in [s.get("template")] + list(s.get("fallback_templates") or []) if t]


#: version 7 (a demonstration whose last search is the site's RSS feed of the search,
#: ``...&feed=rss2``, would replay the feed's XML): a feed is never a result page
_FEED_PATH = re.compile(r"(?:\.(?:xml|rss|atom|rdf)|/(?:feed|rss2?|atom|rdf)(?:/(?:rss2?|atom|rdf))?)/?$", re.I)
_FEED_PARAMS = ("feed", "rss", "atom")
_FORMAT_PARAMS = ("format", "output", "type", "view", "fmt")
_FEED_FORMATS = ("rss", "rss2", "atom", "xml", "feed", "rdf")
_FEED_TEXT = re.compile(r"^\s*<(?:\?xml\b|rss\b|feed\b|rdf:rdf\b)", re.I)
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/xml", "text/xml")


def feed_url(url: Any) -> str:
    """Why the URL is a FEED (RSS / Atom: a ``feed=`` / ``rss=`` / ``atom=`` parameter, a feed format parameter, a
    ``.xml`` / ``.rss`` / ``.atom`` path or a ``/feed/`` path); "" when it is not one."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    if _FEED_PATH.search(p.path or ""):
        return f"the URL path is a feed ({(p.path or '')[-30:]})"
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in _FEED_PARAMS or (k.lower() in _FORMAT_PARAMS and v.lower() in _FEED_FORMATS):
            return f"the URL asks for a feed ({k}={v[:20]})"
    return ""


def feed_page(page: Dict[str, Any]) -> str:
    """Why a recorded or read page is a FEED document (its URL, an ``application/rss+xml`` / Atom / XML content type,
    or a text that is the XML itself - the browser shows a feed's source); "" when it is not one."""
    why = feed_url(page.get("url"))
    if why:
        return why
    ct = str(page.get("content_type") or page.get("mime") or "").split(";")[0].strip().lower()
    if ct in FEED_TYPES:
        return f"its content type is {ct}"
    t = str(page.get("text") or page.get("main_text") or "")[:800]
    if _FEED_TEXT.match(t):
        return f"the page is an XML document ({' '.join(t.split())[:24]})"
    if "this xml file does not appear to have any style information" in W.fold(t):
        return "the browser shows an XML document"
    return ""


def search_feed(trace: Dict[str, Any], k: int) -> str:
    """Why the demonstration's search step ``k`` produced a feed (the URL it landed on, the URL a ``goto`` asked
    for, its result page); "" when it produced an HTML page."""
    st = (trace.get("steps") or [])[k]
    a, oc = st.get("action") or {}, st.get("outcome") or {}
    for u in (oc.get("url_after"), a.get("url") if a.get("action") == "goto" else ""):
        why = feed_url(u)
        if why:
            return why
    return feed_page(page_after(trace, k))


def fill(template: str, value: str) -> str:
    """The template filled: a query slot takes ``quote_plus``, a path slot ``quote``."""
    t = str(template or "")
    i = t.find("{q}")
    if i < 0:
        return t
    in_query = "?" in t[:i]
    enc = urllib.parse.quote_plus(value) if in_query else urllib.parse.quote(value, safe="")
    return t.replace("{q}", enc)


def regex_of_template(template: str, width: int = 1) -> str:
    """A cluster template (``discover.link_clusters``: one varying path segment or
    query value, ``{v}``) as a full-URL regex (scheme and a leading ``www.``
    tolerated, the fragment ignored)."""
    p = urllib.parse.urlsplit(template.replace("{v}", "__V__"))
    host = re.escape((p.hostname or "").lower()).replace(r"www\.", "")
    seg = r"[^/?#]+" if width <= 1 else r"[^/?#]+(?:/[^/?#]+){0,%d}" % (width - 1)
    path = re.escape(p.path).replace("__V__", seg)
    if p.query:
        pairs = []
        for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
            pairs.append(re.escape(k) + "=" + (r"[^&#]+" if v == "__V__" else re.escape(urllib.parse.quote_plus(v))))
        q = r"\?" + "&".join(pairs)
    else:
        q = r"(?:\?[^#]*)?"
    return r"^https?://(?:www\.)?" + host + path + q + r"(?:#.*)?$"


def _norm(u: str) -> str:
    return W.norm_url(strip_fragment(u))


#: web-ax-tree (measured on the 668 saved records): a cluster counts as the page's chrome when at least this share of
#: its members are links ONLY in the tree's navigation / banner / contentinfo / complementary landmarks
AX_CHROME_MAJORITY = 0.5


def region_from_dom(dom: str, url: str, want_urls: Sequence[str], ax: Optional[Dict[str, Any]] = None
                    ) -> Optional[Dict[str, Any]]:
    """The result region of a result page (``dom`` at ``url``) that holds the
    ``want_urls`` (the item the demonstrator opened, or the results it reported):
    the link cluster containing the most of them (fewest chrome members, then the
    largest), as a URL regex. None when no cluster of >= 2 links holds one.
    ``ax`` (web-ax-tree: the page's accessibility tree, ``ax_tree.read_raw``): among clusters holding as many wanted
    results, a cluster whose members are MOSTLY (``AX_CHROME_MAJORITY``) only in the tree's chrome landmarks
    (navigation, banner, contentinfo, complementary) loses to one that is not; a cluster mixed with a few sidebar or
    footer links is decided as before (its chrome links are dropped as items by ``region_links``); no tree: as
    before."""
    from anyuse.execution import discover as D
    from anyuse.execution import url_identity as UI
    if not dom or not [u for u in want_urls if u]:
        return None
    from anyuse.execution import ax_tree as AXT
    ax_links = {_norm(u): v for u, v in AXT.links(ax).items()} if ax else {}

    def ax_terms(members: set) -> Tuple[float, float]:
        if not ax_links or not members:
            return 0.0, 0.0
        seen = [ax_links[m] for m in members if m in ax_links]
        if not seen:
            return 0.0, 0.0
        only_chrome = sum(1 for occ in seen if all(o["landmark"] in AXT.CHROME for o in occ))
        inside = sum(1 for occ in seen if any(o["landmark"] == "main" or o["item"] for o in occ))
        return round(only_chrome / len(seen), 2), round(inside / len(seen), 2)
    scan = D.scan_page(dom, url)
    # the clusters are formed on the links' identity (tracking and per-item noise parameters dropped), so the
    # wanted results are compared on the same identity
    noise = UI.page_noise([ln.url for ln in scan.links])
    want = {_norm(UI.strip(u, noise)) for u in want_urls if u}
    netloc = (urllib.parse.urlsplit(url).netloc or "").lower()
    hosts = {netloc} | {host_of(u) for u in want_urls if u}
    best = None
    for h in hosts:
        for c in D.link_clusters(scan.links, h, min_members=2):
            urls = {_norm(c.template.replace("{v}", v)) for v in c.values}
            # a path cluster of links whose query varies with the path holds a wanted result by its path
            mine = {_norm(UI.path_only(UI.strip(u, noise))) for u in want_urls if u} if c.param == "item+query" else want
            hit = len(mine & urls)
            if not hit:
                continue
            if D.is_nav_value(c.values[0]) and c.nav_frac() >= 0.6:
                continue
            ax_chrome, ax_inside = ax_terms(urls)
            key = (hit, -int(ax_chrome >= AX_CHROME_MAJORITY), -round(c.frac("chrome"), 2), c.frac("main"), -c.width,
                   c.param != "item+query", len(c.values), ax_chrome, ax_inside)
            if best is None or key > best[0]:
                best = (key, c)
    if best is None:
        return None
    c = best[1]
    out = {"kind": "cluster", "template": c.template, "where": c.where, "width": c.width,
           "regex": regex_of_template(c.template, c.width), "members_at_compile": len(c.values),
           "chrome_frac": round(c.frac("chrome"), 2), "want_in_cluster": best[0][0], "want": len(want)}
    if ax_links:
        out["ax_chrome_frac"], out["ax_inside_frac"] = best[0][-2], best[0][-1]
    return out


#: a result list no URL-shape cluster holds is read from its cards' common container
CONTAINER_REGIONS = True
#: the fewest demonstrated results a cards' container region needs (one result is ``region_from_url``'s)
CONTAINER_MIN_RESULTS = 2


def region_from_cards(dom: str, url: str, want_urls: Sequence[str]) -> Optional[Dict[str, Any]]:
    """(a search that lists computer systems ``/<slug>/system/<id>`` and
    products ``/<slug>/cat-p/c/<id>`` in ONE list - links that differ in two non-adjacent segments form no URL cluster, and
    the list mixes two shapes): the region of the result CARDS' common container, when no URL-shape cluster holds the
    reported results. Every demonstrated result must have a link outside the page's chrome (header / nav / footer /
    aside), the container is the deepest element holding one such link of each (never ``html`` / ``body``), and each
    result sits in its OWN child of it (the repeated cards). The region's URL pattern is then any page of the result
    page's site except the result page's own path (the cards decide what is a result, whatever its URL shape:
    ``compile_cards`` must keep a card or title structure, else the compile refuses). None when any condition fails."""
    from anyuse.execution import demo_cards as K
    urls = [u for u in dict.fromkeys(str(u) for u in want_urls if u)]
    if not dom or len(urls) < CONTAINER_MIN_RESULTS:
        return None
    root, base = K.parse(dom)
    by: Dict[str, List[Any]] = {}
    for n, h in K.anchors(root, url, base):
        if h and K.area_of(n) not in ("header", "nav", "footer", "aside"):
            by.setdefault(_norm(h), []).append(n)
    picks = []
    for u in urls:
        cands = by.get(_norm(u)) or []
        if not cands:
            return None                                   # every demonstrated result must be inside
        picks.append(cands[0])

    def chain(n):
        out = []
        x = n
        while x is not None and not x.tag.startswith("#"):
            out.append(x)
            x = x.parent
        return list(reversed(out))
    chains = [chain(n) for n in picks]
    lca = None
    for level in zip(*chains):
        if all(x is level[0] for x in level):
            lca = level[0]
        else:
            break
    if lca is None or lca.tag in ("html", "body"):
        return None
    depth = len(chain(lca))
    kids = [c[depth] if len(c) > depth else None for c in chains]
    if any(k is None for k in kids) or len({id(k) for k in kids}) < len(urls):
        return None                                       # each result in its own card
    # the cards are REPEATED siblings: one tag, and one class token they all share (when they have classes)
    if len({k.tag for k in kids}) != 1:
        return None
    classes = [set((k.attrs.get("class") or "").split()) for k in kids]
    if any(classes) and not set.intersection(*classes):
        return None
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    own = urllib.parse.urlsplit(url).path.strip("/")
    if not host:
        return None
    hx = re.escape(host).replace(r"www\.", "")
    rx = (r"^https?://(?:www\.)?" + hx + "/" + (r"(?!" + re.escape(own) + r"(?:[?#/]|$))" if own else "") +
          r"[^\s#]+(?:#.*)?$")
    members = len([c for c in lca.elements() if any(h for _a, h in K.anchors(c, url, base))])
    return {"kind": "container", "template": f"https://{host}/{{v}}", "where": "path", "width": 1, "regex": rx,
            "container": K.desc(lca)[:120], "members_at_compile": members, "chrome_frac": 0.0,
            "want_in_cluster": len(urls), "want": len(urls)}


def region_from_url(item_url: str) -> Optional[Dict[str, Any]]:
    """The fallback region when no cluster holds the item (a result page with one
    item): the item URL's own shape - its path with the LAST segment varying."""
    try:
        p = urllib.parse.urlsplit(item_url)
    except ValueError:
        return None
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return None
    tmpl = urllib.parse.urlunsplit((p.scheme, p.netloc, "/" + "/".join(segs[:-1] + ["{v}"]) +
                                    ("/" if p.path.endswith("/") else ""), "", ""))
    return {"kind": "shape", "template": tmpl, "where": "path", "width": 1, "regex": regex_of_template(tmpl, 1),
            "members_at_compile": 1, "chrome_frac": 0.0}


def containers_of(page: Dict[str, Any], urls: Sequence[str]) -> List[str]:
    """The result containers (``demo_tool.LINK_SIG_JS``) of ``urls`` on a recorded page."""
    sigs = page.get("link_sigs") or {}
    if not sigs:
        return []
    by = {}
    for h, ss in sigs.items():
        by.setdefault(_norm(h), set()).update(ss or [])
    out = set()
    for u in urls:
        out |= by.get(_norm(u), set())
    return sorted(out)


def region_links(read: Dict[str, Any], region: Dict[str, Any], exclude: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The page's link elements in the compiled region (document order): the URL pattern, never
    in the header, nav or footer, never a gated control or a file, never an ``exclude`` URL."""
    from anyuse.execution import ax_tree as AXT
    rx = re.compile(region.get("regex") or r"^$", re.I)
    ex = {_norm(u) for u in exclude}
    # version 15: a text the page gives one target is that target's title, whatever word it holds
    labels = W.link_labels(read.get("elements") or []) if W.site_evidence_first() else None
    out = []
    for e in read.get("elements") or []:
        if e.get("tag") != "a" or not e.get("href"):
            continue
        href = str(e.get("href"))
        if e.get("area") in ("header", "nav", "footer") or W.asset_href(href):
            continue
        if AXT.chrome(e):
            # web-ax-tree: the browser's accessibility tree puts the link in the page's chrome (a navigation, banner,
            # contentinfo or complementary landmark): never a result (a read without a tree: as before)
            continue
        if not rx.match(strip_fragment(href)) and not rx.match(href):
            continue
        if W.forbidden_link(e.get("text"), href, labels) or _norm(href) in ex:
            continue                                   # a title that mentions a gated word is no control
        out.append(e)
    return out


def cards_v6(region: Dict[str, Any]) -> bool:
    """Does the region read its cards the web-watch way (``demo_cards.read_region_v6``)?"""
    try:
        return bool(region.get("cards")) and int(region.get("cards_version") or 0) >= 6
    except (TypeError, ValueError):
        return False


def read_cards(read: Dict[str, Any], region: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The card map of a result page (``demo_cards``): the page's own (``CARDS_JS``, read by the
    tool), else computed from the read's DOM (a recorded page, an HTTP body); None when neither.
    A version-6 region (``cards_v6``) reads ``CARDS_JS_V6``'s answer (the cards list included)."""
    from anyuse.execution import demo_cards as K
    got = read.get("cards")
    v6r = cards_v6(region)
    if isinstance(got, dict) and isinstance(got.get("map"), dict) and (not v6r or isinstance(got.get("cards"), list)):
        return got
    if read.get("dom"):
        if cards_v12(region):
            return K.recorded_cards_v12(read, region.get("cards") or [])
        if cards_v11(region):
            return K.recorded_cards_v11(read, region.get("cards") or [])
        if cards_v10(region):
            return K.recorded_cards_v10(read, region.get("cards") or [])
        if cards_v8(region):
            return K.recorded_cards_v8(read, region.get("cards") or [])
        return (K.recorded_cards_v6 if v6r else K.recorded_cards)(read, region.get("cards") or [])
    return None


def cards_v10(region: Dict[str, Any]) -> bool:
    """Does the region read its cards with each card's whole text (``demo_cards.CARDS_JS_V10``; the version-10 breadth
    workflows)?"""
    try:
        return bool(region.get("cards")) and int(region.get("cards_version") or 0) >= 10
    except (TypeError, ValueError):
        return False


def cards_v11(region: Dict[str, Any]) -> bool:
    """Does the region read its cards with version 11's title rule (``demo_cards.read_region_v6``, ``no_title11``)?"""
    try:
        return bool(region.get("cards")) and int(region.get("cards_version") or 0) >= 11
    except (TypeError, ValueError):
        return False


def cards_v12(region: Dict[str, Any]) -> bool:
    """Does the region read its cards with version 12's promo rule (``demo_cards.CARDS_JS_V12``, ``promo_flags12``)?"""
    try:
        return bool(region.get("cards")) and int(region.get("cards_version") or 0) >= 12
    except (TypeError, ValueError):
        return False


def cards_v8(region: Dict[str, Any]) -> bool:
    """Does the region read its cards with the titles' VISIBLE text (``demo_cards.CARDS_JS_V8``; version 8)?"""
    try:
        return bool(region.get("cards")) and int(region.get("cards_version") or 0) >= 8
    except (TypeError, ValueError):
        return False


def region_read(read: Dict[str, Any], region: Dict[str, Any], exclude: Sequence[str] = (),
                scope: Optional[Dict[str, Any]] = None, value: str = "") -> Dict[str, Any]:
    """``{"items": region_items, "incomplete": [...], "promo": [...], "scope_why": ""}``: what a version-6 card
    region could NOT read (``demo_cards.read_region_v6``); an older region reports nothing incomplete. ``scope``:
    the tool's RESULT SCOPE (``result_scope``) - the card reader types a card whose link leaves it as
    ``promo``; None (no tool at hand: the compile's self-checks) leaves the read unchanged. ``value``: the input, for
    a version-15 region's title rule (``demo_cards.read_region_v6``), with the dates the page's markup marks."""
    if not cards_v6(region):
        return {"items": region_items(read, region, exclude), "incomplete": [], "promo": [], "scope_why": ""}
    from anyuse.execution import demo_cards as K
    cmap = read_cards(read, region)
    if cmap is None:
        return {"items": [], "incomplete": [], "promo": [], "scope_why": ""}
    site = host_of(str(region.get("template") or "").replace("{v}", "x")) or host_of(read.get("url"))
    dates = K.page_dates15(str(read.get("dom") or "")) if int(region.get("cards_version") or 0) >= CARDS_VERSION_V15 \
        else None
    got = K.read_region_v6(read.get("elements") or [], cmap, region, site, exclude, scope=scope,
                           names=region_link_names(read, region), value=value, dates=dates)
    if isinstance(got, dict) and isinstance(got.get("items"), list):
        got = dict(got, items=drop_categories(got["items"], read))
    return got


def K_LINK_NAMES() -> bool:
    from anyuse.execution import demo_cards as K
    return bool(K.LINK_NAME)


def region_link_names(read: Dict[str, Any], region: Dict[str, Any]) -> Dict[str, str]:
    """A tool compiled with the link-name title rule (``region["link_names"]``): the names of the read's links that wrap
    their card (``demo_cards.link_names`` on the read's DOM); {} for an older tool (its titles stay as they were)."""
    if not region.get("link_names") or not read.get("dom"):
        return {}
    from anyuse.execution import demo_cards as K
    # a tool compiled with ``link_names`` >= 2 names a link by its card's heading too (``card_heading``)
    v = int(region.get("link_names") or 0)
    return K.link_names(str(read.get("dom")), str(read.get("url") or ""), read.get("elements") or [],
                        card_headings=v >= 2, paragraphs=v >= 3)


def drop_categories(items: List[Dict[str, Any]], read: Dict[str, Any]) -> List[Dict[str, Any]]:
    """(each result card also links its category, "Professionisti"): an item whose
    target the page links ``answer_refs.CATEGORY_REPEATS`` or more times with ONE text is a category, a tag or a menu
    entry, never a result - left out when two other items remain. (A category
    linked once, /news/category/<id>, among the /news/<id>/<slug> articles): an item whose URL SHAPE is not the
    region's majority (``url_identity.off_shape``) is no result either, whatever its repeat count - left out the same
    way. The same rules the answer resolver applies."""
    from anyuse.execution import answer_refs as AR
    from anyuse.execution import url_identity as UI
    if len(items) < 3:
        return items
    occ = AR.occurrences_of(read)
    # (a topic page "Pufferspeicher" that is a result CARD of the site's own search AND an entry of its menu -
    # counted 3 times with the menu's two links, it would be dropped, and the judge would withhold an answer that
    # skips the page's result 2): for an item read from a result card of the compiled card structure (it carries
    # ``own_repeat``), links in the page's chrome (the reader's header / nav / footer, the accessibility tree's
    # chrome landmarks) do not count; an item read by the URL pattern alone keeps the full count (measured: the
    # menu's repeats are how the category rule tells the categories "Gartengestaltung", "E-Commerce" from results)
    from anyuse.execution import ax_tree as AXT
    content = AR.occurrences_of(dict(read, elements=[e for e in read.get("elements") or [] if isinstance(e, dict) and
                                                     str(e.get("area") or "") not in ("header", "nav", "footer") and
                                                     not AXT.chrome(e)]))
    # (a news article in the results AND in the page's "News" box, its card linking it
    # by image and title with one text, counted 3 times): the repeats inside the item's OWN card count once
    cat = [i for i, it in enumerate(items)
           if (content if "own_repeat" in it else occ).get(AR._norm(str(it.get("url") or "")), 0)
           - max(0, int(it.get("own_repeat") or 1) - 1) >= AR.CATEGORY_REPEATS]
    if cat and len(items) - len(cat) >= 2:
        items = [it for i, it in enumerate(items) if i not in cat]
    off = set(UI.off_shape([str(it.get("url") or "") for it in items],
                           counts=UI.link_counts(str(e.get("href") or "") for e in read.get("elements") or []
                                                 if e.get("tag") == "a")))
    return [it for i, it in enumerate(items) if i not in off] if off else items


def region_items(read: Dict[str, Any], region: Dict[str, Any], exclude: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The results of the compiled region, in document order. A tool with result CARDS (version 5,
    ``demo_cards``): ONE item per card - its primary title link; a link outside every card is not a
    result (an unreadable card map gives no item). An older tool: one item per URL, the longest
    text, in the compiled result container when it has one and the page's link containers were
    read. ``exclude``: URLs to leave out (the pre-search page's links, when the results render
    over the start page). Each: ``{title, url, i, ctx}``."""
    if cards_v6(region):
        return region_read(read, region, exclude)["items"]
    if region.get("cards"):
        from anyuse.execution import demo_cards as K
        cmap = read_cards(read, region)
        if cmap is None:
            return []
        return drop_categories(K.group(region_links(read, region, exclude), cmap), read)
    rx = re.compile(region.get("regex") or r"^$", re.I)
    ex = {_norm(u) for u in exclude}
    cont = set(region.get("containers") or [])
    sigs: Dict[str, set] = {}
    if cont and read.get("link_sigs"):
        for h, ss in (read.get("link_sigs") or {}).items():
            sigs.setdefault(_norm(h), set()).update(ss or [])
    else:
        cont = set()
    out: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    labels = W.link_labels(read.get("elements") or []) if W.site_evidence_first() else None
    for e in read.get("elements") or []:
        if e.get("tag") != "a" or not e.get("href"):
            continue
        href = str(e.get("href"))
        if e.get("area") in ("header", "nav", "footer") or W.asset_href(href):
            continue
        if not rx.match(strip_fragment(href)) and not rx.match(href):
            continue
        if W.forbidden_link(e.get("text"), href, labels):
            continue                                   # a title that mentions a gated word is no control
        k = _norm(href)
        if k in ex:
            continue
        if cont and not (sigs.get(k, set()) & cont):
            continue
        text = " ".join(str(e.get("text") or "").split())
        ctx = " ".join(str(e.get("ctx") or "").split())
        if k not in out:
            order.append(k)
            out[k] = {"title": text, "url": strip_fragment(href), "i": e.get("i"), "ctx": ctx, "el": e, "texts": [text]}
        else:
            out[k]["texts"].append(text)
            if len(text) > len(out[k]["title"]):
                out[k].update(title=text, i=e.get("i"), el=e)
            if ctx and not out[k]["ctx"]:
                out[k]["ctx"] = ctx
    items = []
    names = region_link_names(read, region)
    for k in order:
        it = out[k]
        it["title"] = item_title(it.pop("texts")) or it["title"]
        if names.get(str(it.get("i"))):
            it["title"] = names[str(it.get("i"))]          # a link that wraps its card is named by its heading
        if not it["title"]:
            # an image-only tile: its card text names the item
            it["title"] = it["ctx"][:160]
        if it["title"]:
            items.append(it)
    return drop_categories(items, read)


#: a tile's call-to-action link text (folded, whole text): never its title
CTA = re.compile(r"^(?:valg variant|vaelg variant|velg variant|valj variant|choose options?|select options?|quick (?:view|shop)|"
                 r"view (?:details|product)|see (?:details|more)|shop now|learn more|read more|more info|mehr erfahren|"
                 r"mehr infos?|zum produkt|zum artikel|weiterlesen|details ansehen|lire la suite|en savoir plus|voir le produit|"
                 r"leggi (?:tutto|di piu)|scopri di piu|leer mas|ver producto|lees meer|bekijk product|las mer|les mer|lue lisaa|"
                 r"czytaj wiecej|zobacz wiecej|compare|vergleichen|merken|opties kiezen|optionen wahlen)$")


#: version 11 (a quick-view "Schnellansicht", a
#: "Dettaglio" link): more call-to-action texts of a tile (folded, whole text) - never a title
CTA11 = re.compile(r"^(?:schnellansicht|dettaglio|dettagli|details?|zum angebot|jetzt ansehen|vai al prodotto|vedi dettagli|"
                   r"voir (?:le|les) details?|ver detalles|bekijk details|se detaljer|zobacz szczegoly|quick ?look)$")


#: version 11: the first three letters of the month names of the 11 languages (folded) and the small words of a date
_MONTH3_11 = frozenset("""jan feb mar apr may mai mei maj mag jun jul aug sep oct okt ott nov dec dez dic gen giu lug ago
set out ene abr fev avr jui aou sty lut kwi cze lip sie wrz paz lis gru tam hel maa huh tou kes hei elo syy lok jou""".split())
_DATE_WORDS11 = frozenset(("de", "del", "of", "the", "le", "la", "den", "der", "am", "um", "el", "il", "di"))


def date_only11(text: Any) -> bool:
    """Version 11: a text that is only a date ("Sep. 20 2026", "20. September 2026", "2026-09-20"): a digit and no word
    but month names and a date's small words."""
    f = W.fold(text)
    if not re.search(r"\d", f):
        return False
    return all(w[:3] in _MONTH3_11 or w in _DATE_WORDS11 for w in re.findall(r"[^\W\d_]+", f))


def no_title11(text: Any) -> bool:
    """Version 11: a link text that is NO item title - empty, a call to action (``CTA``, ``CTA11``: "Schnellansicht",
    "Dettaglio") or only a date (a date badge "Sep. 20 2026"). A lone word that is no call to action stays a title."""
    t = " ".join(str(text or "").split())
    f = W.fold(t)
    return not f or bool(CTA.match(f) or CTA11.match(f)) or date_only11(t)


def item_title(texts: Sequence[str]) -> str:
    """An item's TITLE among the texts of its links (a card links its image, its title, its
    price, a call to action and its description to the same URL): among the texts of 2-14
    words with >= 2 words of letters that are not a call to action, the longest of <= 10
    words (a button's 'Vaelg variant' is short, a description is long), else the shortest;
    "" when none."""
    import html as _html
    good = []
    for t in texts:
        t = " ".join(_html.unescape(str(t or "")).split())
        if not (2 <= len(t.split()) <= 14) or len([w for w in t.split() if re.search(r"[^\W\d_]{2,}", w)]) < 2:
            continue
        if CTA.match(W.fold(t)):
            continue
        good.append(t)
    short = [t for t in good if len(t.split()) <= 10]
    if short:
        return max(short, key=len)
    return min(good, key=len) if good else ""


#: version 8: the arrows / bullets a title link's visible text may lead or end with ("> Referat anzeigen")
_TITLE_ARROWS = "[>»›→⟶➔➜▶►▸‣•·]"
#: a UI word leading a title with a colon ("Read: Microsoft Whiteboard ...", "Mehr: ...")
_TITLE_UI_LEAD = re.compile(r"^\s*(?:read|lesen|lies|mehr|more|show|zeige|anzeigen|weiter|weiterlesen|lire|voir|leggi|"
                            r"vedi|leer|ver|lees|bekijk|las|læs|les|lue|czytaj|zobacz)\s*:\s*", re.I)
#: UI words ending a call to action ("> Hausaufgabe anzeigen", "Weiterlesen »")
_TITLE_UI_TAIL = re.compile(r"\s*(?:anzeigen|ansehen|lesen|weiterlesen|mehr lesen|mehr erfahren|mehr|read more|read|"
                            r"show more|show|more|weiter|details|zum artikel|zum beitrag|lire la suite|lire plus|"
                            r"en savoir plus|voir plus|leggi tutto|leggi di piu|leggi di più|scopri di piu|scopri di più|"
                            r"leer mas|leer más|ver mas|ver más|lees meer|bekijk|las mer|läs mer|læs mere|les mer|"
                            r"lue lisaa|lue lisää|czytaj wiecej|czytaj więcej|zobacz wiecej|zobacz więcej)\s*$", re.I)


def clean_title(text: Any) -> str:
    """Version 8: a title link's visible text without its UI affixes - leading arrows / bullets, a leading UI
    word with a colon ("Read: ..."), trailing arrows, and a trailing UI word of a call to action (only after an
    arrow or a separator: "> Hausaufgabe anzeigen", "Mehr lesen »"; a title ending "... mehr" stays). "" when
    nothing but UI text is left (``CTA``)."""
    t = " ".join(str(text or "").split())
    arrow = bool(re.match(r"^\s*" + _TITLE_ARROWS, t)) or bool(re.search(_TITLE_ARROWS + r"\s*$|…\s*$", t))
    t = re.sub(r"^(?:\s*" + _TITLE_ARROWS + r"+)+\s*", "", t)
    t = _TITLE_UI_LEAD.sub("", t)
    t = re.sub(r"(?:\s*(?:" + _TITLE_ARROWS + r"+|…))+\s*$", "", t)
    sep = re.search(r"\s[|–—-]\s*" + _TITLE_UI_TAIL.pattern.replace(r"\s*(?:", "(?:", 1), t, re.I)
    if sep:
        t = t[:sep.start()]
    elif arrow:
        t = _TITLE_UI_TAIL.sub("", t)
    t = t.strip(" -|–—:")
    if not t or CTA.match(W.fold(t)) or _TITLE_UI_TAIL.fullmatch(t):
        return ""
    return t


#: version 15: the fewest titles a common UI affix is read from
AFFIX_TITLES15 = 3
#: version 15: the marks that set a UI affix off from a title: a colon, an arrow or a bullet before it, a bar, an
#: arrow or a bullet after it (a dash is left out: "Brand - Product" and "Product - Brand" are titles)
_AFFIX_LEAD15 = re.compile(r"^(?::|" + _TITLE_ARROWS + r")+$")
_AFFIX_TAIL15 = re.compile(r"^(?:\||" + _TITLE_ARROWS + r")+$")


def ui_affixes15(titles: Sequence[str], value: Any = "") -> Tuple[List[str], List[str]]:
    """Version 15: the UI words every title of a list shares at its start and at its end (``AFFIX_TITLES15`` or more
    titles; whole words) that name nothing of the input and that a mark sets off from the title - the page's own
    differential, in any language: a leading run that ends with a colon, an arrow or a bullet ("Lees: ...",
    "Xem thêm » ..."), a trailing run that starts with a bar, an arrow or a bullet ("... | Ver"), or a trailing run
    that ends with an arrow ("... bekijk »"). A brand every title starts with is no affix: no such mark sets it off.
    ``(leading words, trailing words)``; a run that would leave a title empty is no affix."""
    ws = [str(t or "").split() for t in titles if str(t or "").strip()]
    if len(ws) < AFFIX_TITLES15:
        return [], []

    def common(seqs: List[List[str]]) -> List[str]:
        out: List[str] = []
        for group in zip(*seqs):
            if len({W.fold(w) for w in group}) != 1:
                break
            out.append(group[0])
        return out

    run = common(ws)
    # the longest leading run whose last word is a mark or ends with one
    k = max((i + 1 for i, w in enumerate(run) if _AFFIX_LEAD15.match(w) or _AFFIX_LEAD15.match(w[-1:])), default=0)
    lead = run[:k]
    run = list(reversed(common([list(reversed(w)) for w in ws])))
    if run and _AFFIX_TAIL15.match(run[-1]):
        tail = run
    else:
        # the longest trailing run whose first word is a mark or starts with one
        j = min((i for i, w in enumerate(run) if _AFFIX_TAIL15.match(w) or _AFFIX_TAIL15.match(w[:1])),
                default=len(run))
        tail = run[j:]
    shortest = min(len(w) for w in ws)
    if lead and (name_share(" ".join(lead), value) or len(lead) >= shortest):
        lead = []
    if tail and (name_share(" ".join(tail), value) or len(tail) + len(lead) >= shortest):
        tail = []
    return lead, tail


def strip_affixes15(title: str, lead: Sequence[str], tail: Sequence[str]) -> str:
    """``title`` without the UI affixes ``ui_affixes15`` read (and the separators they leave)."""
    w = str(title or "").split()
    if lead and [W.fold(x) for x in w[:len(lead)]] == [W.fold(x) for x in lead]:
        w = w[len(lead):]
    if tail and len(w) > len(tail) and [W.fold(x) for x in w[-len(tail):]] == [W.fold(x) for x in tail]:
        w = w[:-len(tail)]
    return " ".join(w).strip(" -|–—:»›>") or str(title or "")


def best_match(items: Sequence[Dict[str, Any]], value: str, v9: bool = False) -> Tuple[Optional[Dict[str, Any]], str]:
    """The region item that names the input best (product lookup's selection rule):
    the first item naming ALL its content words (title, else its card text); else
    the unique item naming the most of them, at least ``MIN_NAME_SHARE``. ``v9`` (version 9): the words matched
    by ``names_value9`` / ``name_share9`` (``fold9`` tokens; a missing or extra space / punctuation tolerated)."""
    nv, ns = (names_value9, name_share9) if v9 else (names_value, name_share)
    if not (_content_positions(tokens9(value)) if v9 else W.content_tokens(value)):
        return None, "the input has no content word"
    full = [it for it in items if nv(it.get("title"), value)]
    if full:
        return full[0], f"the item names every word of the input ({len(full)} such item(s); the first)"
    full = [it for it in items if nv(it.get("ctx"), value)]
    if full:
        return full[0], f"the item's card names every word of the input ({len(full)}; the first)"
    scored = sorted(((max(ns(it.get("title"), value), ns(it.get("ctx"), value)), n, it)
                     for n, it in enumerate(items)), key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] < MIN_NAME_SHARE:
        top = scored[0][0] if scored else 0.0
        return None, f"no result names the input (best share {top:.2f} < {MIN_NAME_SHARE})"
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None, f"two results name the input equally ({scored[0][0]:.2f}): ambiguous"
    return scored[0][2], f"the item names {scored[0][0]:.0%} of the input's words (unique best)"


#: version 7: words that mark a result as a SUBSTITUTE for, or an accessory
#: to, the product it names ("alternative Patrone", "kompatibel", "Reinigungspatrone fur Canon ..."); folded
#: whole tokens; they count only when the input carries none of them
SUBSTITUTE_MARKERS = frozenset("""
compatible compatibles kompatibel kompatible kompatibler kompatibles kompatiblen compatibel compatibile compatibili
fur for pour para voor passend passende passender passendes geeignet suitable
replacement replacements ersatz remplacement reemplazo ricambio vervanging
alternative alternatives alternativ alternativa alternativo alternatief
""".split())


def _markers(text: Any) -> set:
    return {t for t in W.tokens(text) if t in SUBSTITUTE_MARKERS}


def _token_named(t: str, want: Sequence[str]) -> bool:
    """Is the result token ``t`` one of the input's content words (``names_all_compound``'s rule: equal, a
    plural 's' either way, or a compound word holding an input word of >= 4 letters at its start or end; a word of a
    script without spaces at the start of ``t``)?"""
    for w in want:
        if t == w or (len(t) > 3 and t.endswith("s") and t[:-1] == w) or (len(w) > 3 and w.endswith("s") and w[:-1] == t):
            return True
        if U.unspaced(w[0]):
            if t.startswith(w):
                return True
        elif len(w) >= 4 and len(t) > len(w) and (t.startswith(w) or t.endswith(w)):
            return True
    return False


#: a currency-marked price inside a result title (``closeness``): one number - its thousands separated by '.', ','
#: or a narrow / no-break space, never a plain space, so a model number before a price stays a word
TITLE_PRICE = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[.,\u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?\s?"
                         r"(?:€|eur\b|euro\b|kr\b|sek\b|nok\b|dkk\b|zl\b|pln\b|chf\b|£|\$|:-|,-)"
                         r"|(?:€|£|\$)\s?\d[\d.,]*", re.I)


def closeness(text: Any, value: Any) -> Tuple[int, int, int, int]:
    """Version 7: how closely a result text that names every word of ``value`` matches it - lower is closer, in
    this order: (1) a substitute marker (``SUBSTITUTE_MARKERS``) the input carries none of; (2) the input is not a
    contiguous phrase of the text (its folded tokens, in order); (3) the text's EXTRA content words (not one of
    the input's; a currency-marked price is not a word of the title); (4) the input's first word (the brand, as a
    rule) is not the text's first word."""
    t = TITLE_PRICE.sub(" ", str(text or ""))
    tt, vt = W.tokens(t), W.tokens(value)
    marker = 1 if (_markers(t) and not _markers(value)) else 0
    n = len(vt)
    phrase = 0 if n and any(tt[i:i + n] == vt for i in range(len(tt) - n + 1)) else 1
    want = W.content_tokens(value)
    extra = sum(1 for x in W.content_tokens(t) if not _token_named(x, want))
    first = 0 if (tt and vt and tt[0] == vt[0]) else 1
    return marker, phrase, extra, first


def _closeness_text(key: Tuple[int, int, int, int]) -> str:
    return (("a substitute marker, " if key[0] else "no substitute marker, ") +
            ("not the input as a phrase, " if key[1] else "the input as a phrase, ") +
            f"{key[2]} extra word(s)" + (", the brand not first" if key[3] else ", the brand first"))


def title_key(title: Any) -> str:
    """Version 7 ("first + list variants"): a result title NORMALIZED for the same-title
    rule - ``value_clean``'s cleaning (badges, discount and price tags, stock and UI text attached to the name; a
    title it would drop stays as it is), folded to its tokens. "Nieuw Mexx Lage sneakers" and "-30% Mexx Lage
    sneakers" are the same title; "Cerola Vitamin C Taler Grandel" listed five times (pack sizes) is one title."""
    from anyuse.execution import value_clean as V
    raw = str(title or "")
    got = V.clean_value(raw)
    return " ".join(W.tokens(got.cleaned or raw))


def shown_price(item: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """The price a result list SHOWS for an item (its card text, else its title): the first currency-marked price,
    as shown and as a number; ("", None) when it shows none. Read from the result page only - no item is opened."""
    for src in (item.get("ctx"), item.get("title")):
        m = TITLE_PRICE.search(str(src or ""))
        if m:
            shown = " ".join(m.group(0).split())
            return shown, W.parse_price(shown)
    return "", None


# ---------------------------------------------------------------------------
# version 9 (a tile "Trenażer SARISMag+" - brand and model glued - names 2 of
# the input "Trenażer Saris Mag+"'s 3 words, and "no result names the input"): space- and punctuation-insensitive tokens
# ---------------------------------------------------------------------------

#: the longest run of tokens joined when matching across a missing (or extra) space or punctuation
JOIN_RUN = 3


def _content_positions(toks: Sequence[str]) -> List[int]:
    return [i for i, t in enumerate(toks) if t not in W.STOPWORDS and (U.match_length(t) >= 2 or t.isdigit())]


def _joins(toks: Sequence[str]) -> set:
    """Every run of 2 .. ``JOIN_RUN`` consecutive tokens, joined ("saris" "mag" -> "sarismag")."""
    return {"".join(toks[a:b]) for a in range(len(toks)) for b in range(a + 2, min(len(toks), a + JOIN_RUN) + 1)}


def _hit9(i: int, vt: Sequence[str], tt: Sequence[str], have: set, tjoin: set) -> bool:
    """Version 9: is the input token ``vt[i]`` named by the text tokens ``tt`` - ``names_all_compound``'s rule (equal, a
    plural "s" either way, a compound of >= 4 letters holding it at its start or end), or across a space / punctuation:
    a run of text tokens joined is it ("SARIS Mag+" for "SARISMag+"), or a run of input tokens around it joined is a
    text token ("SARISMag+" for "Saris Mag+")."""
    w = vt[i]
    if w in have or (len(w) > 3 and w.endswith("s") and w[:-1] in have):
        return True
    if U.unspaced(w[0]):
        # a word of a script without spaces: named by a token it starts (東京駅), never by one it ends (東京都)
        if any(t.startswith(w) for t in have):
            return True
    elif len(w) >= 4 and any(len(t) > len(w) and (t.endswith(w) or t.startswith(w)) for t in have):
        return True
    if w in tjoin:
        return True
    for a in range(max(0, i - JOIN_RUN + 1), i + 1):
        for b in range(max(a + 2, i + 1), min(len(vt), a + JOIN_RUN) + 1):
            if "".join(vt[a:b]) in have:
                return True
    return False


def _have9(tt: Sequence[str]) -> set:
    have = set(tt)
    return have | {t[:-1] for t in have if len(t) > 3 and t.endswith("s")}


def name_share9(text: Any, value: Any) -> float:
    """Version 9 (``name_share`` with ``fold9`` tokens and ``_hit9``): the share of ``value``'s content words that
    ``text`` names, a missing or extra space / punctuation between words tolerated."""
    vt = tokens9(value)
    idx = _content_positions(vt)
    if not idx:
        return 0.0
    tt = tokens9(text)
    have, tjoin = _have9(tt), _joins(tt)
    return sum(1 for i in idx if _hit9(i, vt, tt, have, tjoin)) / len(idx)


def names_value9(text: Any, value: Any) -> bool:
    """Version 9: does ``text`` name every content word of ``value`` (``name_share9``)?"""
    return bool(_content_positions(tokens9(value))) and name_share9(text, value) >= 1.0


def _extra9(text: str, value: Any) -> int:
    """Version 9: the text's content words that are not the input's (``_token_named``; a text word that is a run of the
    input's words joined, or part of a run of text words that joined is an input word, is the input's)."""
    tt, vt = tokens9(text), tokens9(value)
    want = [vt[i] for i in _content_positions(vt)]
    vjoin, vset = _joins(vt), set(vt)
    n = 0
    for j in _content_positions(tt):
        x = tt[j]
        if _token_named(x, want) or x in vjoin:
            continue
        if any("".join(tt[a:b]) in vset for a in range(max(0, j - JOIN_RUN + 1), j + 1)
               for b in range(max(a + 2, j + 1), min(len(tt), a + JOIN_RUN) + 1)):
            continue
        n += 1
    return n


def closeness9(text: Any, value: Any) -> Tuple[int, int, int, int]:
    """Version 9: ``closeness`` on ``fold9`` tokens, a missing or extra space / punctuation tolerated (the input as a
    phrase also when its words joined are a run of the text's words joined; the extra words by ``_extra9``)."""
    t = TITLE_PRICE.sub(" ", str(text or ""))
    tt, vt = tokens9(t), tokens9(value)
    marker = 1 if ({x for x in tt if x in SUBSTITUTE_MARKERS} and not {x for x in vt if x in SUBSTITUTE_MARKERS}) else 0
    n = len(vt)
    vj = "".join(vt)
    phrase = 0 if n and (any(tt[i:i + n] == list(vt) for i in range(len(tt) - n + 1)) or
                         any("".join(tt[i:j]) == vj for i in range(len(tt)) for j in range(i + 1, len(tt) + 1))) else 1
    first = 0 if (tt and vt and (tt[0] == vt[0] or tt[0] == "".join(vt[:2]))) else 1
    return marker, phrase, _extra9(t, value), first


def same_title_variants(tied: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``variants_same_title`` (version 7, "first + list variants"): the results that tie for the closest match
    under ONE normalized title - their count and each one's title, the price the result list shows (if any) and
    URL, in the site's order; the first is the answer. Nothing is opened (no request)."""
    vs = []
    for n, it in enumerate(tied):
        shown, value = shown_price(it)
        vs.append({"title": str(it.get("title") or "")[:160], "price": shown or None, "price_value": value,
                   "url": str(it.get("url") or ""), "answered": n == 0})
    return {"count": len(vs), "title": title_key(tied[0].get("title")) if tied else "", "variants": vs}


#: the one question the check model is asked about each of several results that match the input equally closely
SAME_PRODUCT15 = MC.Question(
    "result_is_the_product",
    "The results below were listed for a search for the product '{target}'. Is the result marked >> that "
    "product itself, not an accessory, a spare part or a substitute for it?")


def relation_marked15(texts: Sequence[Any], value: Any) -> set:
    """Version 15: the result texts that name the input together with ANOTHER product, read in the result list
    itself: the input stands as a phrase next to one word that two or more results put between it and different
    other words ("Etui do iPhone 15", "Szkło do iPhone 15"; "iPhone 15 için kılıf", "iPhone 15 için cam") - that word
    relates the input to another product (for, für, do, için, ...) in the site's own language. Only where it is the
    one such word on its side: when another result also puts a word and further words on that side of the input
    ("Pixel Neo Smartphone 128GB", "Pixel Neo Smartphone 256GB" next to "Pixel Neo Hülle Silikon"), the list
    shows the same structure for the product and for the other result, and marks nothing there. Indices of
    ``texts``."""
    vt = tokens9(value)
    n = len(vt)
    if not n:
        return set()
    want = {vt[i] for i in _content_positions(vt)}
    found: List[Tuple[int, str, str, Tuple[str, ...]]] = []
    for k, text in enumerate(texts):
        tt = tokens9(TITLE_PRICE.sub(" ", str(text or "")))
        at = next((i for i in range(len(tt) - n + 1) if tt[i:i + n] == list(vt)), None)
        if at is None:
            continue
        if at >= 2:
            found.append((k, "before", tt[at - 1], tuple(tt[:at - 1])))
        if at + n + 1 < len(tt):
            found.append((k, "after", tt[at + n], tuple(tt[at + n + 1:])))
    others: Dict[Tuple[str, str], set] = {}
    for _k, side, w, rest in found:
        if w not in want and not w.isdigit():
            others.setdefault((side, w), set()).add(rest)
    rel = {key for key, rests in others.items() if len(rests) >= 2}
    # a side where another word stands in the same place is no evidence of a relation word
    rel = {(side, w) for side, w in rel if not any(s2 == side and w2 != w for s2, w2 in others)}
    return {k for k, side, w, _rest in found if (side, w) in rel}


def closest_choice(items: Sequence[Dict[str, Any]], value: str, v9: bool = False) -> Dict[str, Any]:
    """Version 7's selection rule (product lookup; five of six results naming
    every word of "Canon PGI-550 PGBK", where version 6 took the FIRST - a third-party "alternative Patrone" listed
    before the original). Among the results naming every word of the input (by title, else by card text, as
    ``best_match``; one per URL), the CLOSEST (``closeness``). When the closest is shared by two or more results:
    under ONE normalized title (``title_key``: pack sizes, colours or badges of one product name) the FIRST is the
    answer and the others are listed with it (``variants``: ``same_title_variants``;
    "first + list variants"); under different titles the answer is AMBIGUOUS (``tied``), never a guess. One such
    result is taken as before; none: ``best_match``'s unique-best-share rule.
    ``v9`` (version 9): the words matched by ``names_value9`` and the closeness by ``closeness9``.
    Returns ``{"el", "why", "tied", "variants"}``."""
    out: Dict[str, Any] = {"el": None, "why": "", "tied": [], "variants": None}
    nv, close = (names_value9, closeness9) if v9 else (names_value, closeness)
    if not (_content_positions(tokens9(value)) if v9 else W.content_tokens(value)):
        return dict(out, why="the input has no content word")
    full: List[Dict[str, Any]] = []
    key = "title"
    for key in ("title", "ctx"):
        seen = set()
        full = []
        for it in items:
            u = _norm(str(it.get("url") or ""))
            if nv(it.get(key), value) and u not in seen:
                seen.add(u)
                full.append(it)
        if full:
            break
    if not full:
        el, why = best_match(items, value, v9=v9)
        return dict(out, el=el, why=why)
    where = "item" if key == "title" else "item's card"
    if len(full) == 1:
        return dict(out, el=full[0], why=f"the {where} names every word of the input (1 such item)")
    v15 = W.site_evidence_first()
    # version 15: a result the list shows naming the input with another product (``relation_marked15``) is a
    # substitute or an accessory, as a listed marker word says
    rel = relation_marked15([it.get(key) for it in full], value) if v15 else set()

    def key_of(n: int, it: Dict[str, Any]) -> Tuple[int, int, int, int]:
        k = close(it.get(key), value)
        return (max(k[0], 1 if n in rel else 0),) + tuple(k[1:]) if rel else k
    scored = sorted(((key_of(n, it), n, it) for n, it in enumerate(full)), key=lambda x: (x[0], x[1]))
    best = scored[0][0]
    tied = [it for k2, _n, it in scored if k2 == best]
    card = "" if key == "title" else " in their card"
    if len(tied) > 1:
        titles = {title_key(it.get("title")) for it in tied}
        if len(titles) == 1 and "" not in titles:
            return dict(out, el=tied[0], variants=same_title_variants(tied),
                        why=(f"the first of {len(tied)} results with the same title that match the input equally "
                             f"closely ({_closeness_text(best)}); the others are listed as its variants, none opened"))
        if v15 and MC.available(SAME_PRODUCT15.kind):
            # version 15: the check model, in an open scope, picks the product itself among the tied results
            asked = tied[:MC.MAX_CANDIDATES]
            listed = [str(it.get(key) or "") for it in asked]
            pick, v = MC.choose(SAME_PRODUCT15, value,
                                [MC.Candidate(text=t, context="\n".join(x for x in listed if x != t)[:600], ref=i)
                                 for i, t in enumerate(listed)],
                                verify=lambda c: c.text in listed, where="demo_compile.closest_choice",
                                guard_obs={"text": "\n".join(listed), "items": listed})
            if pick is not None:
                return dict(out, el=asked[pick], model=v.to_dict(),
                            why=(f"the check model's pick of the {len(tied)} results naming every word of the input"
                                 f"{card} that match it equally closely ({_closeness_text(best)})"))
        return dict(out, tied=tied, why=(f"ambiguous: {len(tied)} of the {len(full)} results naming every word of the "
                                         f"input{card} match it equally closely ({_closeness_text(best)})"))
    return dict(out, el=scored[0][2], why=(f"the closest of the {len(full)} results naming every word of the input"
                                           f"{card} ({_closeness_text(best)})"))


# ---------------------------------------------------------------------------
# version 9 ("Podkładka czarna na stół okrągła 38 cm drewniana ACME LINEA" answered with
# the BEIGE placemat - its title named 10 of the input's 11 words, the missing one the colour): the input's
# DISTINGUISHING words are mandatory in the identity
# ---------------------------------------------------------------------------

#: the colour words of the gate lexicon's 11 languages (EN DE NL FR ES IT SV DA NO FI PL) with their inflections, by
#: colour (folded with ``fold9``: "beżowa" -> "bezowa", "żółty" -> "zolty", "weiß" -> "weiss"); one colour's words are
#: equivalent across the languages. A short word that is a common word of another language when folded (Swedish
#: "röd" / "rod", "blå" / "bla", "grå" / "gra") counts only as written (``COLOUR_RAW``); Swedish "vit" / "vita"
#: (white; "Vit. C", "vita") are left out.
COLOUR_WORDS = {
    "black": r"black|schwarz(?:e[mnrs]?)?|zwart(?:e)?|noir(?:e|s|es)?|negr(?:o|a|os|as)|ner(?:o|a|i|e)|svart(?:a|e)?|"
             r"sort(?:e)?|musta(?:n|a|t|ta)?|czarn(?:y|a|e|ego|ej|emu|ym|ych|ymi)",
    "white": r"white|weiss(?:e[mnrs]?)?|wit(?:te)?|blanc(?:he|s|hes)?|blanc(?:o|a|os|as)|bianc(?:o|a)|bianch(?:i|e)|"
             r"vitt|hvid(?:e)?|hvit(?:e)?|valkoi(?:nen|sen|set|sta)|bial(?:y|a|e|ego|ej|ym|ych|ymi)",
    "grey": r"gr[ae]y|grau(?:e[mnrs]?)?|grijs|grijze|gris(?:e|es)?|grigi(?:o|a)?|grigie|harmaa(?:n|t|ta)?|"
            r"szar(?:y|a|e|ego|ej|ym|ych|ymi)",
    "anthracite": r"anthrazit|anthracite|antracite|antraciet|antracit|antracyt(?:owy|owa|owe)?|antrasiitti",
    "red": r"red|rot(?:e[mnrs]?)?|rood|rode|rouge(?:s)?|roj(?:o|a|os|as)|ross(?:o|a|i|e)|punai(?:nen|sen|set|sta)|"
           r"czerwon(?:y|a|e|ego|ej|ym|ych|ymi)",
    "blue": r"blue|blau(?:e[mnrs]?)?|blauw(?:e)?|bleu(?:e|s|es)?|azul(?:es)?|blu|azzurr(?:o|a|i|e)|"
            r"sini(?:nen|sen|set|sta)|niebiesk(?:i|a|ie|iego|iej|im|ich)",
    "navy": r"navy|marineblau(?:e[mnrs]?)?|granatow(?:y|a|e|ego|ej|ym|ych)",
    "green": r"green|grun(?:e[mnrs]?)?|groen(?:e)?|vert(?:e|s|es)?|verde(?:s)?|verdi|vihrea(?:n|t|ta)?|"
             r"zielon(?:y|a|e|ego|ej|ym|ych|ymi)",
    "yellow": r"yellow|gelb(?:e[mnrs]?)?|geel|gele|jaune(?:s)?|amarill(?:o|a|os|as)|giall(?:o|a|i|e)|gul(?:a|t|e)?|"
              r"keltai(?:nen|sen|set|sta)|zolt(?:y|a|e|ego|ej|ym|ych|ymi)",
    "orange": r"orange(?:s)?|oranje|naranja(?:s)?|arancion(?:e|i)|arancio|oranssi(?:n)?|"
              r"pomaranczow(?:y|a|e|ego|ej|ym|ych)",
    "brown": r"brown|braun(?:e[mnrs]?)?|bruin(?:e)?|brun(?:e|s|es|a|t)?|marron(?:e|i|es)?|ruskea(?:n|t|ta)?|"
             r"brazow(?:y|a|e|ego|ej|ym|ych|ymi)",
    "beige": r"beige(?:s)?|beis|beesi|bezow(?:y|a|e|ego|ej|ym|ych|ymi)",
    "pink": r"pink|rosa|roze|rose(?:s)?|rosad(?:o|a|os|as)|pinkki|vaaleanpunai(?:nen|sen)|rozow(?:y|a|e|ego|ej|ym|ych|ymi)",
    "purple": r"purple|lila|lilla|violett(?:e[mnrs]?)?|paars(?:e)?|violet(?:te|s|tes)?|viola|morad(?:o|a|os|as)|"
              r"purpura|purppura|violetti|fioletow(?:y|a|e|ego|ej|ym|ych)",
    "gold": r"gold(?:en|ene|ener|enes)?|goud(?:en)?|dore(?:e|s|es)?|dorad(?:o|a|os|as)|oro|dorat(?:o|a|i|e)|guld|gull|"
            r"kulta(?:inen)?|zlot(?:y|a|e|ego|ej|ym|ych|ymi)",
    "silver": r"silver|silber(?:n|ne|ner|nes)?|zilver(?:en)?|argent(?:e|ee|es|ees)?|plata|platead(?:o|a)|"
              r"argent(?:o|ato|ata)|hopea(?:nen)?|srebrn(?:y|a|e|ego|ej|ym|ych)",
    "clear": r"transparent(?:e|es|s)?|transparant(?:e)?|trasparente|durchsichtig(?:e[mnrs]?)?|clear|"
             r"genomskinlig(?:a|t)?|gennemsigtig(?:e)?|gjennomsiktig(?:e)?|lapinakyva|"
             r"przezroczyst(?:y|a|e|ego|ej|ym|ych)",
    "turquoise": r"turquoise|turkis|turkos|turkoois|turchese|turquesa|turkusow(?:y|a|e|ego|ej|ym|ych)|turkoosi",
}
_COLOUR_RX = {k: re.compile(r"(?:" + v + r")") for k, v in COLOUR_WORDS.items()}
#: colour words that count only as written (lower case, with their letter that folding loses)
COLOUR_RAW = {"röd": "red", "röda": "red", "rött": "red", "rød": "red", "røde": "red", "rødt": "red",
              "blå": "blue", "blåa": "blue", "blått": "blue", "grå": "grey", "gråa": "grey", "grått": "grey",
              "grøn": "green", "grønn": "green", "grønne": "green", "grønt": "green", "grön": "green",
              "gröna": "green", "grönt": "green", "sølv": "silver", "silvrig": "silver"}
#: size words (clothing): mandatory as written
SIZE_WORDS = frozenset(("xxs", "xs", "xl", "xxl", "xxxl", "xxxxl"))


def colour_class(word: Any) -> str:
    """The colour (``COLOUR_WORDS``) a single word names, "" when none."""
    w = str(word or "").strip().lower()
    if not w:
        return ""
    if w in COLOUR_RAW:
        return COLOUR_RAW[w]
    f = fold9(w)
    for k, rx in _COLOUR_RX.items():
        if rx.fullmatch(f):
            return k
    return ""


def _raw_words(text: Any) -> List[str]:
    return re.findall(r"[^\W\d_]+", str(text or "").lower())


def _num_forms(s: str) -> set:
    """A number as written -> its values ("1,5" -> 1.5; "1.000" -> 1.0 and 1000; "1.299,00" -> 1299.0)."""
    s = str(s or "")
    parts = re.split(r"[.,]", s)
    out: set = set()
    try:
        if len(parts) == 1:
            out.add(float(int(parts[0])))
        elif len(parts) == 2:
            out.add(float(f"{parts[0]}.{parts[1]}"))
            if len(parts[1]) == 3:
                out.add(float(parts[0] + parts[1]))
        elif all(len(p) == 3 for p in parts[1:-1]):
            if len(parts[-1]) == 3:
                out.add(float("".join(parts)))
            else:
                out.add(float("".join(parts[:-1]) + "." + parts[-1]))
    except ValueError:
        pass
    return out


def _text_numbers(text: Any) -> set:
    vals: set = set()
    for n in re.findall(r"\d+(?:[.,]\d+)*", fold9(text)):
        vals |= _num_forms(n)
    return vals


def distinguishing(value: Any) -> List[Dict[str, Any]]:
    """Version 9: the input's DISTINGUISHING words - each a mark the chosen product must carry: a colour (``kind
    colour``, its ``class``), a number (a size, capacity, quantity, vintage: "38" of "38 cm" / "38cm", each number of
    "22x19x8", "1,5" of "1,5 l"), a model code (a word of letters AND digits: "PGI-550" -> "pgi550", "abc24") and a
    clothing size word ("XL"). ``[]`` when the input has none (version 8's selection then)."""
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def add(m: Dict[str, Any], key: Tuple[str, str]) -> None:
        if key not in seen:
            seen.add(key)
            out.append(m)
    for raw in str(value or "").split():
        w = fold9(raw).strip(".,;:!?()[]{}<>|*+/\\\"'")
        if not w:
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?", w) or re.fullmatch(r"\d+(?:[.,]\d+)?[a-z]{1,4}", w) or \
                re.fullmatch(r"\d+(?:[.,]\d+)?(?:x\d+(?:[.,]\d+)?)+[a-z]{0,3}", w):
            for n in re.findall(r"\d+(?:[.,]\d+)?", w):
                if _num_forms(n):
                    add({"kind": "number", "token": n}, ("number", n))
        elif re.search(r"\d", w) and re.search(r"[a-z]", w):
            code = "".join(re.findall(r"[0-9a-z]+", w))
            add({"kind": "code", "token": code}, ("code", code))
        elif re.search(r"\d", w):
            for n in re.findall(r"\d+(?:[.,]\d+)?", w):
                add({"kind": "number", "token": n}, ("number", n))
        else:
            for t in _raw_words(raw):
                cls = colour_class(t)
                if cls:
                    add({"kind": "colour", "token": t, "class": cls}, ("colour", cls))
                elif fold9(t) in SIZE_WORDS:
                    add({"kind": "size", "token": fold9(t)}, ("size", fold9(t)))
    return out


#: version 15: the most result titles compared pairwise for the site's variant words
VARIANT_TITLES15 = 60


def variant_slots15(titles: Sequence[Any], value: Any = "") -> Dict[str, set]:
    """Version 15: the site's own variant words, read in its result list - two titles with the same words but at
    ONE place (``tokens9``; "Podkładka czarna na stół 38 cm" / "Podkładka beżowa na stół 38 cm") fill that place with
    two values of one variant; values linked through such pairs are one slot. The input ``value`` is compared with
    the titles the same way (an asked-for variant the list does not show). ``{word: the other words of its slot}``;
    a number is never one (the number marks read numbers)."""
    toks = list(dict.fromkeys(tuple(tokens9(t)) for t in [value] + list(titles)[:VARIANT_TITLES15] if t))
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i], toks[j]
            if len(a) != len(b) or len(a) < 2:
                continue
            diff = [k for k in range(len(a)) if a[k] != b[k]]
            if len(diff) != 1 or a[diff[0]].isdigit() or b[diff[0]].isdigit():
                continue
            parent[find(a[diff[0]])] = find(b[diff[0]])
    groups: Dict[str, set] = {}
    for w in list(parent):
        groups.setdefault(find(w), set()).add(w)
    return {w: g - {w} for g in groups.values() for w in g}


def variant_marks15(value: Any, titles: Sequence[Any], marks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Version 15: the input's words that are a value of one of the site's variant slots (``variant_slots15``) and
    no mark of ``marks`` already: each a mark (``kind`` "variant", its ``alts``) the chosen product must carry."""
    slots = variant_slots15(titles, value)
    covered = {fold9(m.get("token")) for m in marks}
    vt = tokens9(value)
    out: List[Dict[str, Any]] = []
    for i in _content_positions(vt):
        t = vt[i]
        if t in slots and slots[t] and t not in covered and not any(m.get("token") == t for m in out):
            out.append({"kind": "variant", "token": t, "alts": sorted(slots[t])[:12]})
    return out


def mark_carried(text: Any, mark: Dict[str, Any]) -> bool:
    """Does ``text`` carry the distinguishing ``mark`` - the same colour (any of its words, any language), the same
    number (its value), the model code across separators ("PGI 550", "PGI-550", "PGI550"), the size word?"""
    kind = mark.get("kind")
    if kind == "colour":
        return any(colour_class(t) == mark.get("class") for t in _raw_words(text))
    if kind == "number":
        return bool(_num_forms(str(mark.get("token"))) & _text_numbers(text))
    if kind == "code":
        tt = tokens9(text)
        code = str(mark.get("token") or "")
        return any("".join(tt[i:j]) == code for i in range(len(tt)) for j in range(i + 1, min(len(tt), i + 4) + 1))
    if kind in ("size", "variant"):
        return str(mark.get("token")) in tokens9(text)
    return False


def missing_marks(text: Any, marks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The marks ``text`` does not carry."""
    return [m for m in marks if not mark_carried(text, m)]


def contradicting(text: Any, marks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The colour marks ``text`` does not carry while it names ANOTHER colour (the beige sibling of a black input):
    ``[{"mark", "names"}]``. Version 15: a variant mark (``variant_marks15``) the text does not carry while it names
    another value of its slot, too."""
    out = []
    names = {colour_class(t) for t in _raw_words(text)} - {""}
    tt: Optional[set] = None
    for m in marks:
        if m.get("kind") == "colour" and m.get("class") not in names and names:
            out.append({"mark": m, "names": sorted(names)})
        elif m.get("kind") == "variant":
            tt = set(tokens9(text)) if tt is None else tt
            other = sorted(tt & set(m.get("alts") or []))
            if m.get("token") not in tt and other:
                out.append({"mark": m, "names": other})
    return out


def _marks_text(marks: Sequence[Dict[str, Any]]) -> str:
    return ", ".join(f"{m.get('token')!r} ({m.get('kind')}" + (f" {m['class']})" if m.get("class") else ")")
                     for m in marks)


# ---------------------------------------------------------------------------
# version 9 (tiles that name the wine without its producer - "Chablis Montserre
# Bio 2022" for "Château de Béru Chablis Montserre Bio 2022", 4 of 6 words): a producer / brand word may be absent
# from a result title when every other word matches and the product page confirms it
# ---------------------------------------------------------------------------

def absent_runs(value: Any) -> List[Tuple[List[str], str]]:
    """Version 9: the runs of the input's words that may be a producer / brand absent from a result title - a
    run of 1 .. ``JOIN_RUN`` content words at the input's START or END, fewer than half of its content words, none of
    them a distinguishing word (``distinguishing``); shortest first, the start before the end. ``[(words, the rest of
    the input)]``."""
    vt = tokens9(value)
    idx = _content_positions(vt)
    out: List[Tuple[List[str], str]] = []
    for k in range(1, JOIN_RUN + 1):
        if 2 * k >= len(idx):
            break
        for run in (idx[:k], idx[-k:]):
            words = [vt[i] for i in run]
            if distinguishing(" ".join(words)):
                continue
            rest = " ".join(t for i, t in enumerate(vt) if i not in run)
            if (words, rest) not in out:
                out.append((words, rest))
    return out


def _absent_choice(pool: Sequence[Dict[str, Any]], value: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Version 9: version 7's rule among the results whose TITLE names every word of the input but one absent
    run (``absent_runs``) - the first run that some title allows; ``(closest_choice's dict, the absent words)``."""
    for words, rest in absent_runs(value):
        full = [it for it in pool if names_value9(it.get("title"), rest)]
        if full:
            got = closest_choice(full, rest, v9=True)
            if got["el"] is not None or got["tied"]:
                return got, words
    return None, []


def _with_absent(base: Dict[str, Any], got: Dict[str, Any], pool: Sequence[Dict[str, Any]], value: str,
                 extra_why: str = "") -> Dict[str, Any]:
    """``got`` (no result names the input) replaced by ``_absent_choice``'s choice when one is allowed."""
    if got["el"] is not None or got["tied"]:
        return {**base, **got}
    alt, words = _absent_choice(pool, value)
    if alt is None:
        return {**base, **got}
    return {**base, **alt, "absent": words,
            "why": alt["why"] + f"; the title omits the input's words {' '.join(words)!r} (a producer / brand at the "
                                f"input's start or end): the product page must name them" + extra_why}


def closest_choice9(items: Sequence[Dict[str, Any]], value: str) -> Dict[str, Any]:
    """Version 9: version 7's selection (``closest_choice``) among the results whose TITLE carries every
    distinguishing word of the input (``distinguishing``; the 75 % word rule stays for the other words). None does:
    among the results whose title names no OTHER colour, the closest is taken with ``need`` - the marks its product
    page's variant attributes must carry (``check_marks``); none such but a result the rule would have taken: INCOMPLETE
    "the asked-for variant is not listed" (``not_listed``: the marks and the closest result), never the sibling.
    No result naming the input: a title naming all its words but a producer / brand run at its start or end is
    taken with ``absent`` (``_absent_choice``; the product page must name the absent words: ``check_pick9``).
    Returns ``closest_choice``'s dict plus ``need``, ``absent`` and ``not_listed``."""
    marks = distinguishing(value)
    if W.site_evidence_first():
        # version 15: the input's words that are values of the site's own variant slots (``variant_marks15``)
        marks = marks + variant_marks15(value, [it.get("title") for it in items], marks)
    base: Dict[str, Any] = {"el": None, "why": "", "tied": [], "variants": None, "need": [], "absent": [],
                            "not_listed": None}
    if not marks:
        return _with_absent(base, closest_choice(items, value, v9=True), items, value)
    carriers = [it for it in items if not missing_marks(it.get("title"), marks)]
    if carriers:
        got = _with_absent(base, closest_choice(carriers, value, v9=True), carriers, value)
        return {**got, "why": got["why"] + f" (among the {len(carriers)} result(s) whose title carries the input's "
                                           f"distinguishing word(s) {_marks_text(marks)})"}
    pool = [it for it in items if not contradicting(it.get("title"), marks)]
    got = closest_choice(pool, value, v9=True) if pool else {"el": None, "why": "no result", "tied": [], "variants": None}
    got = _with_absent(base, got, pool, value)
    if got["el"] is not None:
        need = missing_marks(got["el"].get("title"), marks)
        return {**base, **got, "need": need,
                "why": got["why"] + f"; no result title carries the input's distinguishing word(s) "
                                    f"{_marks_text(need)}: the product's variant attributes must"}
    if got["tied"]:
        return {**base, **got}
    sib = closest_choice(items, value, v9=True)
    near = sib["el"] or (sib["tied"][0] if sib["tied"] else None)
    if near is not None:
        con = contradicting(near.get("title"), marks)
        miss = missing_marks(near.get("title"), marks)
        return {**base, "not_listed": {"marks": list(marks), "missing": miss,
                                       "closest": {"title": str(near.get("title") or "")[:160],
                                                   "url": str(near.get("url") or "")},
                                       "names_instead": [c["names"] for c in con]},
                "why": (f"identity: the asked-for variant is not listed - no result carries all of the input's "
                        f"distinguishing words {_marks_text(marks)}; the closest result {str(near.get('title'))[:80]!r} "
                        f"lacks {_marks_text(miss)}" + (f" and names {', '.join(con[0]['names'])} instead" if con else ""))}
    return {**base, **got}


def deglue(text: Any) -> str:
    """Version 9: glued element texts split where a word changes from lower to upper case ("Produkt
    dostępnyWysyłka" -> "dostępny Wysyłka") or an upper-case run meets a capitalised word ("SARISMag" -> "SARIS Mag");
    digits, punctuation and every other character are left as they are."""
    t = str(text or "")
    out: List[str] = []
    for i, c in enumerate(t):
        if i and c.isupper() and t[i - 1].isalpha():
            p = t[i - 1]
            if p.islower() or (p.isupper() and i >= 2 and t[i - 2].isupper() and i + 1 < len(t) and t[i + 1].islower()):
                out.append(" ")
        out.append(c)
    return "".join(out)


#: the separator read is read instead of the ``innerText`` only when it is the same text with separators: its letters
#: >= ``SEP_COVER`` of the ``innerText``'s (a read cut short by its element budget, or failed, is not) and <=
#: ``SEP_EXCESS`` of them (a read that holds text the ``innerText`` does not show is not - a recorded DOM read without
#: its style sheets holds the page's hidden dialogs and menus)
SEP_COVER = 0.9
SEP_EXCESS = 1.1


def _letters(s: str) -> int:
    return sum(1 for c in s if c.isalpha())


def _vis_text(page: Dict[str, Any]) -> str:
    """Version 9: the visible text a version-9 tool reads its statements in - the main region's (else the
    body's) text with element-boundary separators (``demo_tool.TEXT_JS_V9``) when it is the same text as that region's
    ``innerText`` with separators (its letters within ``SEP_COVER`` .. ``SEP_EXCESS`` of the ``innerText``'s; else - a
    record without the separator read, a read cut short, a read holding hidden text - that ``innerText``), deglued
    (``deglue``)."""
    inner = str(page.get("main_text") or "")
    sep = str(page.get("main_text_sep") or "")
    if not inner:
        inner, sep = str(page.get("text") or ""), str(page.get("text_sep") or "")
    n = _letters(inner)
    if sep and SEP_COVER * n <= _letters(sep) <= SEP_EXCESS * max(n, 1) + (0 if inner else 10 ** 9):
        return deglue(sep)
    return deglue(inner)


def _text_of(page: Dict[str, Any], sources: Dict[str, str]) -> str:
    """The product page text a tool reads its fields and statements in: version 9 (``sources["text_rule"] == "9"``) the
    separator text (``_vis_text``); older versions the main text, else the body text."""
    if sources.get("text_rule") == "9":
        return _vis_text(page)
    return str(page.get("main_text") or page.get("text") or "")


#: a variant attribute's label in the buy box (folded line start; the 11 languages): colour, size, content / volume,
#: weight, version - "Farbe: Schwarz", "Kolor wariantu: żółty", "Inhalt: 0,75 l", "Größe: XL"
_VARIANT_LABEL_V9 = re.compile(
    r"^(?:farbe|colou?r|kolor(?: wariantu)?|couleur|colore|kleur|farg|farve|farge|vari|barwa|"
    r"grosse|size|taille|taglia|talla|tamano|maat|storlek|storrelse|koko|rozmiar|"
    r"inhalt|fullmenge|content|contents|volume|volumen|contenance|capacite|capacity|kapazitat|contenuto|capacita|"
    r"contenido|capacidad|inhoud|innehall|volym|indhold|innhold|sisalto|tilavuus|pojemnosc|"
    r"gewicht|weight|poids|peso|vikt|vaegt|vekt|paino|waga|"
    r"ausfuhrung|variante|variant|wariant|versie|version|modell|model)\b\s*:?\s*(.*)$")


def variant_attributes(page: Dict[str, Any], fields: Dict[str, Any], v15: bool = False) -> List[Tuple[str, str]]:
    """Version 9: where an opened product page names its variant - ``(source, text)``: its name, headings and
    title; its JSON-LD colour / size (the product's own, and the variant whose SKU the URL loaded carries or the product
    names); the buy box's colour / size / content labels (a label line takes its value from the next line). ``v15``
    (version 15): the page's own form state before the labels - each drop-down's selected option and each checked
    choice."""
    name = str(fields.get("name") or "")
    out: List[Tuple[str, str]] = [("name", name)] + [("heading", str(h)) for h in page.get("h1") or []] + \
        [("title", str(page.get("title") or ""))]
    if v15:
        for e in page.get("elements") or []:
            if not isinstance(e, dict):
                continue
            if e.get("tag") == "select":
                out += [("selected option", str(o.get("text") or "")) for o in e.get("options") or []
                        if isinstance(o, dict) and o.get("selected")]
            elif e.get("tag") == "input" and e.get("type") in ("radio", "checkbox") and e.get("checked"):
                out.append(("checked choice", str(e.get("text") or "")))
    prod = page_product(page, "", name) or {}
    for k in ("color", "size"):
        if prod.get(k):
            out.append((f"JSON-LD {k}", str(prod[k])))
    url_toks = set(tokens9(page.get("url")))
    for v in prod.get("variants") or []:
        sku = "".join(tokens9(v.get("sku")))
        if sku and (sku in url_toks or sku == "".join(tokens9(prod.get("sku")))):
            out += [(f"selected variant {k}", str(v[k])) for k in ("color", "size", "name") if v.get(k)]
    lines = [ln.strip() for ln in buy_box(_vis_text(page), deglue(name)).split("\n")]
    for i, ln in enumerate(lines):
        m = _VARIANT_LABEL_V9.match(fold9(ln)) if len(ln) <= 120 else None
        if m:
            val = m.group(1).strip() or next((x for x in lines[i + 1:i + 3] if x), "")
            if val:
                out.append(("label " + repr(ln[:60]), val[:120]))
    return [(s, t) for s, t in out if t.strip()]


def check_marks(page: Dict[str, Any], fields: Dict[str, Any], need: Sequence[Dict[str, Any]], v15: bool = False
                ) -> Tuple[bool, str, Dict[str, Any]]:
    """Version 9: every mark of ``need`` carried by the opened product (``variant_attributes``; ``v15``: its form
    state too); else an identity failure naming the missing marks. Returns ``(ok, why, evidence)``."""
    attrs = variant_attributes(page, fields, v15=v15)
    rows = []
    for m in need:
        by = next((s for s, t in attrs if mark_carried(t, m)), "")
        rows.append(dict(m, carried_by=by))
    miss = [r for r in rows if not r["carried_by"]]
    ev = {"marks": rows}
    if miss:
        shown = sorted({colour_class(t) for _s, t in attrs for t in _raw_words(t)} - {""})
        return False, ("identity: the product does not carry the input's distinguishing word(s) " + _marks_text(miss) +
                       " in its name or its variant attributes" + (f" (it names {', '.join(shown)})" if shown else "")), ev
    return True, "", ev


def check_pick9(page: Dict[str, Any], fields: Dict[str, Any], value: str, pick: Dict[str, Any],
                landed: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
    """Version 9: the opened product's identity beyond ``check_product`` - the distinguishing marks the chosen result's
    title did not carry (``pick["need"]``; a page the search LANDED on: every mark its own name does not carry) must be
    carried by its variant attributes; the producer / brand words the title omitted (``pick["absent"]``)
    must be named by the page's own name, a heading or its JSON-LD brand (each alone, or together) - never by the
    document title alone. Returns ``(ok, why, evidence)`` (evidence ``{}``: nothing to check)."""
    if landed:
        need = missing_marks(str(fields.get("name") or ""), distinguishing(value))
    else:
        need = list(pick.get("need") or [])
    absent = [str(w) for w in pick.get("absent") or []]
    ok, why, ev = True, "", {}
    if need:
        # version 15 (``pick["evidence_rule"]``): the page's form state carries a mark too
        ok, why, ev = check_marks(page, fields, need, v15=pick.get("evidence_rule") == "15")
    if absent:
        name = str(fields.get("name") or "")
        prod = page_product(page, "", name) or {}
        srcs = [("name", name)] + [("heading", str(h)) for h in page.get("h1") or []] + \
            [("JSON-LD brand", str(prod.get("brand") or ""))]
        srcs.append(("name, headings and brand", " ".join(t for _s, t in srcs)))
        by = next((s for s, t in srcs if t.strip() and names_value9(t, " ".join(absent))), "")
        ev = dict(ev, absent={"words": absent, "confirmed_by": by})
        if not by and ok:
            ok, why = False, (f"identity: the chosen result's title omits the input's words {' '.join(absent)!r} and "
                              f"the product page's own name, headings and brand do not name them ({name[:80]!r})")
    return ok, why, ev


def closest_match(items: Sequence[Dict[str, Any]], value: str
                  ) -> Tuple[Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """``closest_choice`` as ``(item, why, tied)`` (the same-title variants left out)."""
    got = closest_choice(items, value)
    return got["el"], got["why"], got["tied"]


def list_ok(items: Sequence[Dict[str, Any]], value: str) -> Tuple[bool, str]:
    """The site-search list belongs to the query (the list contract, compounds
    counted): >= 3 items and one names the value; 1-2 items and each names it."""
    return W.list_contract([str(it.get("title") or "") + " " + str(it.get("ctx") or "") for it in items], value)


# ---------------------------------------------------------------------------
# product pages: field sources and extraction
# ---------------------------------------------------------------------------

_AVAIL_TEXT_YES = re.compile(r"\b(in stock|on stock|available now|ready to ship|auf lager|lieferbar|sofort lieferbar|"
                             r"vorratig|verfugbar|en stock|disponible|disponibile|op voorraad|direct leverbaar|"
                             r"pa lager|i lager|i butikk|lagervare|varastossa|saatavilla|dostepny|w magazynie|"
                             r"em estoque|skladem)\b")
_AVAIL_TEXT_NO = re.compile(r"\b(out of stock|sold out|currently unavailable|not available|ausverkauft|nicht (?:lieferbar|"
                            r"verfugbar|vorratig|auf lager)|vergriffen|rupture de stock|epuise|indisponible|agotado|sin stock|"
                            r"esaurito|non disponibile|uitverkocht|niet (?:op voorraad|leverbaar|beschikbaar)|slutsald|"
                            r"ej i lager|udsolgt|ikke pa lager|utsolgt|ikke pa lager|loppuunmyyty|ei varastossa|"
                            r"niedostepny|wyprzedany|esgotado|vyprodano)\b")


#: version 8 (microdata LimitedAvailability, the page "Dieser
#: Artikel steht derzeit nicht zur Verfügung!" - not in version 6's vocabulary - and its back-in-stock offer
#: "Benachrichtigen Sie mich, sobald der Artikel lieferbar ist" read as "lieferbar": no CONFLICT): the UNAVAILABLE
#: phrases of the gate lexicon's 11 languages (EN DE NL FR ES IT SV DA NO FI PL; folded text) beyond version 6's
_AVAIL_NO_V8_EXTRA = (
    # EN
    r"temporarily (?:unavailable|out of stock|sold out)|no longer available|not in stock|out-of-stock|"
    r"(?:currently|presently) not available|not currently available|(?:item|product) (?:is )?(?:currently )?unavailable|"
    # DE
    r"steht (?:derzeit |zurzeit |zur zeit |momentan |aktuell |leider )?nicht (?:mehr )?zur verfugung|"
    r"(?:derzeit|zurzeit|zur zeit|momentan|aktuell|vorubergehend|leider) nicht (?:verfugbar|lieferbar|bestellbar|vorratig|"
    r"auf lager|erhaltlich)|nicht mehr (?:verfugbar|lieferbar|erhaltlich|bestellbar|vorratig)|nicht bestellbar|"
    r"nicht erhaltlich|vorubergehend ausverkauft|"
    # NL
    r"niet (?:meer )?(?:beschikbaar|leverbaar|op voorraad|verkrijgbaar|bestelbaar)|tijdelijk (?:niet beschikbaar|"
    r"niet leverbaar|uitverkocht)|"
    # FR
    r"(?:momentanement|temporairement|actuellement) (?:indisponible|epuise|en rupture)|non disponible|"
    r"n.?est (?:plus|pas) disponible|plus disponible|en rupture|"
    # ES
    r"no disponible|(?:temporalmente|actualmente) (?:agotado|no disponible|sin stock)|fuera de stock|sin existencias|"
    r"no hay stock|no esta disponible|"
    # IT
    r"(?:momentaneamente|temporaneamente|attualmente) (?:non disponibile|esaurito)|non (?:e |piu )?disponibile|"
    r"non piu disponibile|non acquistabile|"
    # SV
    r"inte i lager|tillfalligt slut|slut i lager|finns ej i lager|finns inte i lager|(?:ej|inte) tillganglig|"
    # DA / NO
    r"midlertidigt? udsolgt|midlertidig utsolgt|ikke (?:pa lager|tilg(?:ae|æ)ngelig|tilgjengelig|leverbar|"
    r"bestillingsvare)|"
    # FI
    r"tilapaisesti (?:loppu|poissa|loppuunmyyty)|loppu varastosta|ei saatavilla|tuote on loppu|"
    # PL
    r"chwilowo niedostepny|produkt niedostepny|brak w magazynie|brak towaru|niedostepne|obecnie niedostepny|"
    r"brak na stanie")
_AVAIL_TEXT_NO_V8 = re.compile(r"\b(" + _AVAIL_NO_V8_EXTRA + "|" + _AVAIL_TEXT_NO.pattern[3:-3] + r")\b")
#: version 8: in-stock phrases beyond version 6's ("Sofort versandfertig", Swiss "an Lager")
_AVAIL_TEXT_YES_V8 = re.compile(r"\b(an lager|am lager|lagernd|sofort versandfertig|versandfertig|sofort versandbereit|"
                                r"versandbereit|ready for dispatch|available to ship|" + _AVAIL_TEXT_YES.pattern[3:-3] +
                                r")\b")
#: version 8: a back-in-stock NOTIFICATION offer ("Benachrichtigen Sie mich, sobald der Artikel lieferbar ist",
#: "Notify me when available"): its "lieferbar" / "available" never states that the item IS available (it is
#: neither an in-stock nor an out-of-stock statement by itself)
_NOTIFY_V8 = re.compile(
    r"benachrichtig\w*[^.!\n]{0,80}?(?:lieferbar|verfugbar|vorratig|auf lager|erhaltlich)(?: ist)?|"
    r"sobald (?:der |die |das |dieser |diese |dieses )?(?:artikel |produkt |ware )?(?:wieder )?(?:lieferbar|verfugbar|"
    r"vorratig|auf lager|erhaltlich)(?: ist)?|"
    r"(?:notify|email|e-mail|alert|tell) me (?:when|if|once)[^.!\n]{0,40}?(?:available|in stock|back in stock)|"
    r"let me know (?:when|if|once)[^.!\n]{0,40}?(?:available|in stock)|when (?:it is |its |this item is )?back in stock|"
    r"(?:m.?avertir|me prevenir|prevenez-moi|alertez-moi|etre (?:averti|alerte|prevenu))[^.!\n]{0,60}?(?:disponible|en stock)|"
    r"avvisami[^.!\n]{0,60}?disponibil\w*|avisame[^.!\n]{0,60}?(?:disponible|stock)|"
    r"(?:mail|bericht|laat|houd) (?:mij|me)[^.!\n]{0,60}?(?:beschikbaar|leverbaar|op voorraad)|"
    r"(?:meddela|bevaka|giv|gi|varsle)[^.!\n]{0,60}?(?:i lager|pa lager|tillganglig|tilg(?:ae|æ)ngelig|tilgjengelig)|"
    r"ilmoita[^.!\n]{0,60}?(?:saatavilla|varastossa)|powiadom[^.!\n]{0,60}?dostepn\w*")
#: version 8: a statement about one OPTION / VARIANT ("(Diese Option ist zurzeit nicht verfügbar.)"): never the
#: product's own availability
_OPTION_NO_V8 = re.compile(
    r"(?:diese|this|cette|questa|esta|deze|detta|denne|dette|tama|ta) (?:option|optie|opzione|opcion|alternativ\w*|"
    r"mulighed|vaihtoehto|opcja|variante?|variant)[^.!\n]{0,40}?(?:nicht verfugbar|not available|unavailable|"
    r"indisponible|pas disponible|non (?:e )?disponibile|no (?:esta )?disponible|niet beschikbaar|inte tillganglig\w*|"
    r"ikke tilg\w*|ei (?:ole )?saatavilla|niedostepn\w*|ausverkauft|sold out|out of stock)")
#: version 8: an availability word about the shop's PAYMENT or SHIPPING options ("Weitere Zahlungsarten verfügbar"):
#: never the product's availability
_SERVICE_V8 = re.compile(
    r"(?:zahlungsart\w*|zahlungsmethode\w*|zahlungsoption\w*|bezahlart\w*|versandart\w*|versandoption\w*|"
    r"lieferart\w*|lieferoption\w*|payment (?:methods?|options?)|shipping (?:methods?|options?)|delivery options?|"
    r"moyens? de paiement|modes? de (?:paiement|livraison)|metodi di (?:pagamento|spedizione)|metodos de (?:pago|envio)|"
    r"betaalmethoden|verzendmethoden|betalningsalternativ\w*|betalingsmetoder|betalingsmuligheder|maksutavat|"
    r"toimitustavat|metody (?:platnosci|dostawy))[^.!\n]{0,30}?(?:verfugbar|available|disponibles?|disponibili|"
    r"beschikbaar|tillgangliga?|tilg\w*|saatavilla|dostepne)")


#: version 9 (a buy box's "Wird bestellt" - no class of version 8's
#: vocabulary, so the answer had no availability): the ON ORDER / backorder phrases of the gate lexicon's 11 languages
#: (folded with ``fold9``) - a visible statement of the class ``backorder``. These words are a statement wherever they
#: stand:
_ORDER_V9 = re.compile(
    r"\b(?:"
    # DE
    r"wird bestellt|wird fur sie bestellt|bestellartikel|bestellware|im zulauf|"
    # EN
    r"on backorder|back-?ordered|available (?:on|to) order|made to order|"
    r"(?:is|item|product|article|currently|now) (?:is |currently )?on order|"
    # FR
    r"en reapprovisionnement|en cours de reapprovisionnement|"
    # IT
    r"in riordino|"
    # ES
    r"bajo pedido|por encargo|sobre pedido|"
    # NL
    r"wordt besteld|"
    # SV / DA / NO
    r"bestallningsvara|bestillingsvare|"
    # FI
    r"tilaustuote|tilaustavara"
    r")\b")
#: ... these (a preposition and "order", also the words of a shipping or discount offer: "Gratis verzending op
#: bestelling vanaf 50 €", "Livraison offerte sur commande de 60 €") only as the statement itself - at the start of a
#: line, or after an availability / product word ("Lieferbar auf Bestellung", "Leveres på bestilling",
#: "Produkt na zamówienie")
_ORDER_PREP_V9 = (r"(?:auf bestellung|op bestelling|in bestelling|pa bestallning|pa bestilling|i bestilling|"
                  r"na zamowienie|su ordinazione|in ordinazione|sur commande|en commande|on order|special order)")
_ORDER_QUALIFIER_V9 = (r"(?:lieferbar|verfugbar|erhaltlich|bestellbar|nur|artikel|produkt|ware|leverbaar|verkrijgbaar|"
                       r"beschikbaar|alleen|leveres|levereres|levereras|finns|tillganglig|kun|bare|disponibile|"
                       r"prodotto|articolo|disponible|solo|solamente|uniquement|seulement|article|produit|livrable|"
                       r"dostepny|dostepne|towar|tylko|tilgjengelig|tilgaengelig|item|product|available|only)")
_ORDER_QUALIFIED_V9 = re.compile(r"\b" + _ORDER_QUALIFIER_V9 + r"\W{1,3}(?:\w+\W{1,3})?" + _ORDER_PREP_V9 + r"\b")
_ORDER_LINE_V9 = re.compile(r"^\W*" + _ORDER_PREP_V9 + r"\b")


def _order9(text: Any, folded: str) -> str:
    """Version 9: the ON ORDER statement of a text (``folded``: its folded form, unavailable phrases removed;
    the line rule on ``text``'s own lines) - the matched words, "" when none."""
    m = _ORDER_V9.search(folded) or _ORDER_QUALIFIED_V9.search(folded)
    if m:
        return folded[max(0, m.start() - 30):m.end() + 30].strip()
    for ln in str(text or "")[:8000].split("\n"):
        f = fold9(deglue(ln))
        if _ORDER_LINE_V9.search(f):
            return f[:80]
    return ""


def _v8_text(t: str) -> str:
    """Version 8: the folded text without the statements that are never the product's availability (a
    back-in-stock notification offer, one option's statement, a payment or shipping option's)."""
    return _SERVICE_V8.sub(" ", _OPTION_NO_V8.sub(" ", _NOTIFY_V8.sub(" ", t)))


def text_availability(text: Any, v8: bool = False, v9: bool = False) -> str:
    """"InStock" / "OutOfStock" from a statement in the page text, "" when it says
    neither or both. ``v8`` (a version-8 tool): the unavailable phrases of the 11 languages (``_AVAIL_TEXT_NO_V8``),
    a few more in-stock phrases, and neither a back-in-stock notification offer, one option's statement nor a
    payment / shipping option's counts (``_v8_text``). ``v9`` (a version-9 tool; implies ``v8``): the text folded with
    ``fold9`` and deglued (``deglue``: "dostępnyWysyłka" is two words), and an ON ORDER statement (``_order9``)
    is "BackOrder" - before "in stock" and "out of stock" ("Nicht auf Lager - wird bestellt" is on order)."""
    v8 = v8 or v9
    t = fold9(deglue(text))[:8000] if v9 else W.fold(text)[:8000]
    if v8:
        t = _v8_text(t)
    rx_no, rx_yes = (_AVAIL_TEXT_NO_V8, _AVAIL_TEXT_YES_V8) if v8 else (_AVAIL_TEXT_NO, _AVAIL_TEXT_YES)
    no = bool(rx_no.search(t))
    t2 = rx_no.sub(" ", t)
    yes = bool(rx_yes.search(t2))
    if v9 and _order9(text, t2):
        return "BackOrder"
    if yes == no:
        return ""
    return "InStock" if yes else "OutOfStock"


#: web-watch: the small availability vocabulary structured data and visible text are compared in
#: (schema.org's values folded: a URL or a bare name, any case)
AVAILABILITY = ("in_stock", "limited", "store_only", "out_of_stock", "preorder", "backorder")
_SCHEMA_AVAILABILITY = {"instock": "in_stock", "onlineonly": "in_stock", "limitedavailability": "limited",
                        "instoreonly": "store_only", "outofstock": "out_of_stock", "soldout": "out_of_stock",
                        "discontinued": "out_of_stock", "reserved": "out_of_stock", "preorder": "preorder",
                        "presale": "preorder", "backorder": "backorder", "madetoorder": "backorder"}
#: a visible statement that only a few are left (folded text): "limited", not plain "in stock"
_AVAIL_TEXT_LIMITED = re.compile(r"\b(only \d+ (?:left|in stock|available)|only a few left|few left|low stock|"
                                 r"limited (?:stock|availability)|nur noch \d+|nur noch wenige|nur \d+ stuck|wenige stuck|"
                                 r"begrenzt verfugbar|plus que \d+|derniers? articles?|ultimi \d+ (?:pezzi|disponibili)|"
                                 r"nog \d+ (?:op voorraad|beschikbaar|stuks)|bara \d+ kvar|kun \d+ (?:tilbage|igjen|pa lager))\b")


def availability_class(v: Any) -> str:
    """A structured availability value (schema.org, a URL or a bare name) or a vocabulary word ->
    ``AVAILABILITY``; "" when it is none of them."""
    s = W.availability_word(v)
    if s in AVAILABILITY:
        return s
    return _SCHEMA_AVAILABILITY.get(re.sub(r"[^a-z]", "", s.lower()), "")


def text_availability_class(text: Any, v8: bool = False, v9: bool = False) -> str:
    """What the page's visible text states about availability, in ``AVAILABILITY`` ("" when it
    states nothing, or both "in stock" and "out of stock"). ``v8``: version 8's vocabulary (``text_availability``);
    ``v9``: version 9's reading (``fold9``, ``deglue``)."""
    base = text_availability(text, v8=v8, v9=v9)
    if base == "BackOrder":
        return "backorder"
    if base == "OutOfStock":
        return "out_of_stock"
    if base == "InStock":
        ft = fold9(deglue(text))[:8000] if v9 else W.fold(text)[:8000]
        return "limited" if _AVAIL_TEXT_LIMITED.search(ft) else "in_stock"
    return ""


def visible_statement(text: Any, cls: str, v8: bool = False, v9: bool = False) -> str:
    """The visible words that state availability class ``cls`` (folded; "" when none). ``v9``: ``fold9``, ``deglue``."""
    v8 = v8 or v9
    t = fold9(deglue(text))[:8000] if v9 else W.fold(text)[:8000]
    if v8:
        t = _v8_text(t)
    rx_no, rx_yes = (_AVAIL_TEXT_NO_V8, _AVAIL_TEXT_YES_V8) if v8 else (_AVAIL_TEXT_NO, _AVAIL_TEXT_YES)
    m = None
    if cls == "backorder" and v9:
        return _order9(text, rx_no.sub(" ", t))
    if cls == "out_of_stock":
        m = rx_no.search(t)
    elif cls == "limited":
        m = _AVAIL_TEXT_LIMITED.search(t)
    elif cls == "in_stock":
        m = rx_yes.search(rx_no.sub(" ", t))
    if not m:
        return ""
    return t[max(0, m.start() - 30):m.end() + 30].strip()


# ---------------------------------------------------------------------------
# version 8 (microdata LimitedAvailability, the page only "Lieferzeit: 13 Wochen";
# JSON-LD OutOfStock, the page "Keine Angaben vom Hersteller"): a structured availability
# the visible page does not support is never answered
# ---------------------------------------------------------------------------

#: what a version-8 tool answers as the availability when the visible page states none
NOT_STATED = "not stated"
#: a visible DELIVERY-TIME statement (a folded line of the buy box), in the 11 languages
_DELIVERY_V8 = re.compile(
    r"\b(?:lieferzeit|lieferzeitraum|lieferdauer|liefertermin|voraussichtliche lieferung|voraussichtlicher liefertermin|"
    r"versandfertig in|versand in|versand innerhalb|lieferbar in|lieferbar innerhalb|lieferung in|"
    r"delivery time|estimated delivery|delivery in|delivered in|dispatch(?:ed)? (?:within|in)|ships (?:within|in)|"
    r"usually ships|delai de livraison|delai d.?expedition|livraison (?:sous|en)|expediee? sous|expedition sous|"
    r"levertijd|leveringstijd|verzonden binnen|leveranstid|levereras inom|leveringstid|leveres innen|leveres inden for|"
    r"toimitusaika|czas (?:dostawy|realizacji|wysylki)|wysylka w|tempi di consegna|tempo di consegna|"
    r"consegna (?:in|entro)|spedizione (?:in|entro)|plazo de entrega|plazo de envio|entrega en|envio en)\b")
#: version 10 ("Ten produkt wysyłany jest w ciągu 2-3 dni od złożenia zamówienia"
#: - a passive "is shipped within" that version 8's vocabulary lacks, answered "not stated"): the delivery-time
#: statements of the 11 languages in their verb forms ("is shipped / dispatched / delivered within", "wird innerhalb
#: ... versandt", "est expédié sous", "se envía en", "viene spedito entro", "wordt verzonden binnen", "skickas inom",
#: "sendes inden for", "sendes innen", "lähetetään", "wysyłany jest w ciągu") - always with a time preposition, so a
#: shipping offer ("Darmowa wysyłka od 199 zł", "Versand ab 50 € kostenlos") is never one
_DELIVERY_V10_EXTRA = (
    # DE
    r"(?:wird|werden) (?:in der regel |innerhalb |in |binnen )[^\n.]{0,30}?(?:versandt|versendet|verschickt|geliefert|"
    r"zugestellt)|(?:versandt|versendet|verschickt|geliefert|zugestellt) (?:innerhalb|binnen|in)|"
    r"versand (?:erfolgt )?(?:innerhalb|binnen)|lieferung (?:erfolgt )?(?:innerhalb|binnen)|zustellung (?:in|innerhalb|binnen)|"
    # EN
    r"(?:is |are |will be |usually |normally |gets )?(?:shipped|dispatched|delivered|sent) (?:out )?(?:within|in)|"
    r"delivery within|ships out (?:within|in)|ready to ship (?:within|in)|arrives (?:within|in)|"
    # FR
    r"(?:est |sera |sont |seront )?(?:expedie|expediee|expedies|expediees|livre|livree|livres|envoye|envoyee) "
    r"(?:sous|en|dans)|expedition (?:en|dans)|envoi (?:sous|en|dans)|"
    # ES
    r"(?:se )?(?:envia|envian|enviado|enviada|enviamos|entrega|entregado|entregada|entregamos) (?:en|dentro de)|"
    r"envio (?:dentro de)|listo para enviar en|"
    # IT
    r"(?:viene |verra |sara )?(?:spedit[oaie]|consegnat[oaie]|inviat[oaie]) (?:entro|in)|spediamo entro|"
    # NL
    r"(?:wordt |worden )?(?:verzonden|geleverd|verstuurd|bezorgd) (?:binnen|in)|"
    r"(?:wordt|worden) (?:binnen|in) [^\n.]{0,30}?(?:verzonden|geleverd|verstuurd|bezorgd)|"
    # SV / DA / NO
    r"(?:skickas|levereras|sands|skickar vi) (?:inom|inom loppet av)|"
    r"(?:sendes|afsendes|forsendes|sender vi) (?:inden for|indenfor|inden|innen|i lopet av)|leveres (?:i lopet av|indenfor)|"
    # FI
    r"(?:lahetetaan|toimitetaan|lahtee) (?:\d|yhden|kahden|kolmen|arkipaiv|paivan|viikon)|"
    # PL
    r"wysyl\w*(?: jest| sa| zostanie| zostana| nastapi| nastepuje)? w ciagu|wysylamy w ciagu|wyslemy w ciagu|"
    r"(?:realizacja|dostawa)(?: zamowienia)? w ciagu|wysylka (?:do|w) \d")
_DELIVERY_V10 = re.compile(r"\b(?:" + _DELIVERY_V8.pattern[5:-3] + "|" + _DELIVERY_V10_EXTRA + r")\b")
#: an AVAILABILITY-labelled line ("Verfügbarkeit Onlineshop" / "Availability:" and its value)
_AVAIL_LABEL_V8 = re.compile(
    r"^(?:verfugbarkeit|lagerstatus|lagerbestand|lieferstatus|availability|stock status|disponibilite|disponibilita|"
    r"disponibilidad|beschikbaarheid|voorraadstatus|tillganglighet|lagersaldo|tilg(?:ae|æ)ngelighed|tilgjengelighet|"
    r"saatavuus|varastotilanne|dostepnosc)\b")
#: a visible "no information" statement ("Keine Angaben vom Hersteller")
_NO_INFO_V8 = re.compile(
    r"\b(?:keine angaben?|keine informationen?|no information|not specified|unknown availability|pas d.?information|"
    r"non renseignee?|nessuna informazione|non specificat[ao]|sin informacion|no especificad[ao]|geen informatie|"
    r"ingen information|ingen informasjon|ingen oplysninger|ei tietoa|brak informacji|brak danych)\b")
#: the buy box after the product's name that is searched for a statement (characters)
STATEMENT_CHARS = 2000


def buy_box(text: Any, name: Any = "") -> str:
    """Version 8: the product's BUY BOX in its page text - ``STATEMENT_CHARS`` characters from the end of the
    product's name (``name_anchor``); the text itself when it does not name the product. Version 8 reads the
    page's availability statements there: a related product's or an accessory's "nicht verfügbar" further down the
    page (a cable's lengths) is not the product's."""
    t = str(text or "")
    at = name_anchor(t, name)
    return t[at:at + STATEMENT_CHARS] if at is not None else t


def availability_statement(text: Any, name: Any = "", v9: bool = False, v10: bool = False) -> Dict[str, str]:
    """Version 8: the buy box's visible availability STATEMENT that is not a class of the vocabulary - the
    first line (after the product's name, within ``STATEMENT_CHARS``; else in the text's first characters) that
    is a delivery time ("Lieferzeit: 13 Wochen"; a label line takes its value from the next line), an
    availability-labelled line ("Verfügbarkeit Onlineshop: Bestellbar") or a "no information" statement ("Keine
    Angaben vom Hersteller"). ``{"kind": "delivery" | "labelled" | "no_information" | "", "statement": the visible
    words}``. ``v9``: the lines folded with ``fold9`` (the text and the name read deglued by the caller). ``v10`` (version
    10): the delivery-time statements in their verb forms too (``_DELIVERY_V10``)."""
    t = str(text or "")[:TEXT_PRICE_CHARS]
    at = name_anchor(t, name)
    window = t[at:at + STATEMENT_CHARS] if at is not None else t
    lines = [ln.strip() for ln in window.split("\n")]
    for i, line in enumerate(lines):
        f = fold9(line) if v9 else W.fold(line)
        if not f or len(f) > 160:
            continue
        kind = "no_information" if _NO_INFO_V8.search(f) else "labelled" if _AVAIL_LABEL_V8.match(f) else \
            "delivery" if (_DELIVERY_V10 if v10 else _DELIVERY_V8).search(f) else ""
        if not kind:
            continue
        st = line
        label_only = re.fullmatch(r"[^:]{1,40}:", line) or (kind == "labelled" and ":" not in line and len(f.split()) <= 3)
        if kind != "no_information" and label_only:
            nxt = next((x for x in lines[i + 1:i + 4] if x), "")
            if nxt:
                st = line.rstrip(":").strip() + ": " + nxt
        st = re.sub(r"[\s*]+$", "", " ".join(st.split()))[:120]
        return {"kind": kind, "statement": st}
    return {"kind": "", "statement": ""}


# ---------------------------------------------------------------------------
# version 10 (a buy box that reads "12,99 € / En stock / 24,33 € / Litres / ...
# Rechercher un magasin / En stock / En rupture temporaire / au magasin / Non vendu au magasin" - the product's own
# "En stock" next to its price, then a store-availability LEGEND naming two classes; version 8's buy-box reading saw
# both classes, stated none, and the structured InStock was answered as "not stated"): a legend is not a statement
# ---------------------------------------------------------------------------

#: a legend's line: a short line (folded characters) ...
LEGEND_LINE_CHARS = 48
#: ... or, inside a legend, a short line without a class that continues the line before it ("au magasin")
LEGEND_GLUE_WORDS = 3
#: the lines after the price line, and around the add-to-cart control, whose class is ATTACHED to the product
ATTACHED_LINES = 2
_CLASS_WORD = {"in_stock": "InStock", "limited": "InStock", "out_of_stock": "OutOfStock", "backorder": "BackOrder",
               "preorder": "PreOrder", "store_only": "InStoreOnly"}


def _line_class10(line: str) -> str:
    """The availability class one SHORT line states (version 9's reading), "" for a longer line or none."""
    if not line or len(fold9(line)) > LEGEND_LINE_CHARS:
        return ""
    return text_availability_class(line, v8=True, v9=True)


def stock_legends(lines: Sequence[str]) -> List[Tuple[int, int, List[str]]]:
    """Version 10: the LEGENDS among a buy box's lines - a run of consecutive short lines (<=
    ``LEGEND_LINE_CHARS``) of which >= 2 each state an availability class, the classes >= 2 DIFFERENT ones ("En stock /
    En rupture temporaire / Non vendu au magasin", "In stock / Low stock / Sold out"); a short line without a class and
    without a digit (<= ``LEGEND_GLUE_WORDS`` words: "au magasin") may continue a line of the run. A line with a price
    or any other line ends a run. A list of all the classes is a KEY, never the product's statement. Returns
    ``[(first line, last line, [classes])]``."""
    out: List[Tuple[int, int, List[str]]] = []
    run: List[Tuple[int, str]] = []

    def close() -> None:
        cls = [c for _i, c in run if c]
        if len(cls) >= 2 and len(set(cls)) >= 2:
            last = max(i for i, c in run if c)
            out.append((run[0][0], last, cls))

    for i, ln in enumerate(lines):
        t = str(ln or "").strip()
        if not t:
            continue
        c = _line_class10(t)
        if c:
            run.append((i, c))
            continue
        glue = len(t.split()) <= LEGEND_GLUE_WORDS and not re.search(r"\d", t) and len(fold9(t)) <= LEGEND_LINE_CHARS
        if glue and run:
            run.append((i, ""))
            continue
        close()
        run = []
    close()
    return out


def _attached_class10(lines: Sequence[str], legend: set, price: Optional[float]) -> Tuple[str, str]:
    """Version 10: the class ATTACHED to the product - stated by a short line within ``ATTACHED_LINES`` lines
    after the line that shows its price, or next to its add-to-cart control (never a legend's line); ("", "") when no
    such line states exactly one class. Returns (class, the line)."""
    anchors: List[Tuple[int, int, int]] = []
    for i, ln in enumerate(lines):
        if price is not None and visible_price(ln, price):
            anchors.append((i + 1, i + ATTACHED_LINES, i))
        if W._BUY_CONTROL.search(fold9(ln)) and len(fold9(ln)) <= LEGEND_LINE_CHARS:
            anchors.append((i - ATTACHED_LINES, i + ATTACHED_LINES, i))
    found: Dict[str, str] = {}
    for lo, hi, at in anchors:
        for j in range(max(0, lo), min(len(lines) - 1, hi) + 1):
            if j == at or j in legend:
                continue
            c = _line_class10(str(lines[j] or "").strip())
            if c:
                found.setdefault(c, str(lines[j]).strip())
                break
    if len(found) == 1:
        c, ln = next(iter(found.items()))
        return c, ln
    return "", ""


def box_class10(box: Any, price: Optional[float] = None) -> Tuple[str, Dict[str, Any]]:
    """Version 10: the availability class a product's BUY BOX states. Version 9's reading first (one class
    stated in the box: that class, unchanged); when it states none or several, the box's LEGENDS (``stock_legends``)
    are no statement - the rest of the box is read; still none or several: the class ATTACHED to the product's price
    or add-to-cart control (``_attached_class10``). Returns ``(class, evidence)`` (evidence ``{}`` when version 9's
    reading decided)."""
    t = str(box or "")
    cls = text_availability_class(t, v8=True, v9=True)
    if cls:
        return cls, {}
    lines = t.split("\n")
    legends = stock_legends(lines)
    skip = {j for a, b, _c in legends for j in range(a, b + 1)}
    ev: Dict[str, Any] = {}
    if legends:
        ev["legends"] = [" / ".join(x.strip() for x in lines[a:b + 1] if x.strip())[:200] for a, b, _c in legends]
        rest = "\n".join(ln for j, ln in enumerate(lines) if j not in skip)
        cls = text_availability_class(rest, v8=True, v9=True)
        if cls:
            return cls, dict(ev, rule="the box without its legend(s)",
                             statement=visible_statement(rest, cls, v9=True)[:120])
    cls, line = _attached_class10(lines, skip, price)
    if cls:
        return cls, dict(ev, rule="the class attached to the product's price / add-to-cart control", statement=line[:120])
    return "", ev


# ---------------------------------------------------------------------------
# version 15: the product's availability read in the site's own rendering before the phrase lists
# ---------------------------------------------------------------------------

#: the longest buy-box line that renders an availability state (folded characters)
AVAIL_LINE15 = LEGEND_LINE_CHARS
#: how the check model's question names each class
_CLASS_SAID15 = {"in_stock": "in stock", "limited": "in stock, with only a few left", "out_of_stock": "out of stock",
                 "backorder": "on order", "preorder": "available for pre-order", "store_only": "available in a store only"}
#: the one question the check model is asked about a buy-box line
AVAIL_LINE_Q15 = MC.Question(
    "availability_line", "On the product page below, does the line marked >> say that the product is {target}?")


def _box_lines15(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str]) -> List[str]:
    """The product's buy-box lines as the tool reads them (``_text_of``, ``buy_box``; a version-13 tool's own box)."""
    v9 = sources.get("text_rule") == "9"
    box = buy_box(_text_of(page, sources), deglue(fields.get("name")) if v9 else fields.get("name"))
    if sources.get("stock_rule") == "13":
        box, _gone = own_box13(box, _price_of(fields.get("price")))
    return [" ".join(ln.split()) for ln in box.split("\n") if ln.strip()]


def _avail_mask15(line: Any) -> str:
    return re.sub(r"\d+", "#", fold9(line))


def _price_line15(lines: Sequence[str], price: Optional[float]) -> Optional[int]:
    return next((i for i, ln in enumerate(lines) if price is not None and visible_price(ln, price)), None)


def _state_line15(line: str) -> bool:
    """A short line without a digit (a state, never a quantity, a date or a delivery time)."""
    f = fold9(line)
    return 0 < len(f) <= AVAIL_LINE15 and not re.search(r"\d", f)


def _legend_lines15(lines: Sequence[str]) -> set:
    """The box's lines inside a stock-state legend (``stock_legends``): a key of states, never the product's."""
    return {j for a, b, _c in stock_legends(lines) for j in range(a, b + 1)}


def learn_availability15(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str],
                         reported: Any) -> Optional[Dict[str, Any]]:
    """Version 15 (compile time): the buy-box line by which the site renders the demonstrated product's structured
    availability - the short state line (``_state_line15``) whose words hold the availability the demonstration
    REPORTED - and where it stands (lines after the price line, else in the box). ``{"class", "line", "after_price",
    "at"}``; None when the structured source is not compiled, the report is a schema word, or no such line is found."""
    if sources.get("availability") not in ("jsonld", "microdata"):
        return None
    cls = availability_class(fields.get("availability"))
    rep = str(reported or "").strip()
    if not cls or not rep or availability_class(rep):
        return None
    want = tokens9(rep)
    if not want:
        return None
    lines = _box_lines15(page, fields, sources)
    legend = _legend_lines15(lines)
    for i, ln in enumerate(lines):
        if not _state_line15(ln) or i in legend:
            continue
        tt = tokens9(ln)
        if any(tt[j:j + len(want)] == want for j in range(len(tt) - len(want) + 1)):
            p = _price_line15(lines, _price_of(fields.get("price")))
            return {"class": cls, "line": _avail_mask15(ln), "after_price": (i - p) if p is not None and p < i else None,
                    "at": i}
    return None


def _same_class15(a: str, b: str, sources: Dict[str, str]) -> bool:
    if sources.get("stock_rule") in ("11", "12", "13"):
        # a few left is in stock (version 11's merged class)
        a, b = ("in_stock" if a == "limited" else a), ("in_stock" if b == "limited" else b)
    return bool(a) and a == b


def site_availability15(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str],
                        value: str = "") -> Dict[str, Any]:
    """Version 15: what the site's own rendering says about the structured availability, before the phrase lists.

    * the line the site rendered the demonstrated product's state with (``learn_availability15``) in this buy box:
      that state's class (``class``); it supports a structured class that is the same;
    * a structured class of ANOTHER state, and the place that line stood (the same number of lines after the price
      line) shows another short state line: the site renders another state there - it supports the structured class;
    * else, in an open model-check scope, the check model about the box's short lines: a yes supports the
      structured class, a no for each supports nothing.

    ``{"supports": bool, "class", "line", "by", ...}``; ``{}`` when the site's evidence says nothing (the phrase
    lists decide)."""
    scls = availability_class(fields.get("availability"))
    if sources.get("availability") not in ("jsonld", "microdata") or not scls:
        return {}
    lines = _box_lines15(page, fields, sources)
    price = _price_of(fields.get("price"))
    anchor = sources.get("avail_anchor") if isinstance(sources.get("avail_anchor"), dict) else None
    legend = _legend_lines15(lines)
    if anchor and anchor.get("line"):
        hit = next((ln for i, ln in enumerate(lines) if _avail_mask15(ln) == anchor["line"] and i not in legend), "")
        if hit:
            return {"supports": _same_class15(str(anchor.get("class") or ""), scls, sources),
                    "class": str(anchor.get("class") or ""), "line": hit[:120], "by": "the site's own state line"}
        if not _same_class15(str(anchor.get("class") or ""), scls, sources):
            p = _price_line15(lines, price)
            d, at = anchor.get("after_price"), anchor.get("at")
            k = (p + int(d)) if (d is not None and p is not None) else (int(at) if d is None and at is not None else None)
            if k is not None and 0 <= k < len(lines) and k not in legend and _state_line15(lines[k]) and \
                    not visible_price(lines[k], price):
                return {"supports": True, "class": "", "line": lines[k][:120],
                        "by": "the site's state place shows another state"}
    if lines and MC.available(AVAIL_LINE_Q15.kind):
        p = _price_line15(lines, price)
        order = sorted(range(len(lines)), key=lambda i: (abs(i - p) if p is not None else i, i))
        picked = [i for i in order if _state_line15(lines[i]) and i not in legend][:MC.MAX_CANDIDATES]
        if picked:
            v = _ask15(AVAIL_LINE_Q15, _CLASS_SAID15.get(scls, scls.replace("_", " ")), page, lines, picked,
                       "demo_compile.site_availability15")
            if v.yes and v.candidate is not None:
                return {"supports": True, "class": scls, "line": v.candidate.text[:120], "by": "model_check",
                        "model": v.to_dict()}
            if v.no:
                return {"supports": False, "class": "", "line": "", "by": "model_check", "model": v.to_dict()}
    return {}


def availability_answer(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str],
                        value: str = "") -> Tuple[Any, Dict[str, Any]]:
    """Version 8 - the small explicit rule: a STRUCTURED availability (the compiled JSON-LD / microdata
    source) is answered only when the visible page states an availability class (version 8's vocabulary; a
    different class is a CONFLICT, checked before). When the page states none, the answer is its visible
    statement - a delivery time or an availability-labelled line (``availability_statement``) - else ``NOT_STATED``
    (no statement, or "no information"); never the structured class. Returns ``(availability, evidence)``
    (evidence ``{}`` when the structured class is answered or the source is not structured)."""
    av = fields.get("availability")
    if sources.get("availability") not in ("jsonld", "microdata") or not av:
        return av, {}
    if sources.get("evidence_rule") == "15":
        # version 15: the site's own rendering supports the structured class (``site_availability15``)
        site = site_availability15(page, fields, sources, value)
        if site.get("supports"):
            return av, {"structured": W.availability_word(av), "structured_class": availability_class(av),
                        "visible_class": site.get("class") or availability_class(av),
                        "visible_statement": site.get("line") or "", "statement_kind": "site evidence",
                        "answered": av, "by": site.get("by") or "",
                        "why": f"the site's own rendering supports the structured {W.availability_word(av)}"}
    if sources.get("stock_rule") in ("12", "13"):
        got = not_now12(page, fields, sources)
        if got:
            return got["answered"], got
    if sources.get("stock_rule") == "13":
        # version 13: the buy box's class picks between two structured words of the merged class
        got = word13(page, fields, sources, value)
        if got:
            return got["answered"], got
    v9 = sources.get("text_rule") == "9"
    text = _text_of(page, sources)
    name = deglue(fields.get("name")) if v9 else fields.get("name")
    if sources.get("statement_rule") == "10":
        # version 10: a stock-state legend is not a statement; the class attached to the product is
        box = buy_box(text, name)
        if sources.get("stock_rule") == "13":
            box, _gone = own_box13(box, _price_of(fields.get("price")))
        cls10, ev10 = box_class10(box, _price_of(fields.get("price")))
        if cls10:
            return av, {}
    elif text_availability_class(buy_box(text, name), v8=True, v9=v9):
        return av, {}
    st = availability_statement(text, name, v9=v9, v10=sources.get("statement_rule") == "10")
    ans = st["statement"] if st["kind"] in ("delivery", "labelled") and st["statement"] else NOT_STATED
    return ans, {"structured": W.availability_word(av), "structured_class": availability_class(av),
                 "visible_class": "", "visible_statement": st["statement"], "statement_kind": st["kind"] or "none",
                 "answered": ans, "why": "the visible page states no availability class: the structured "
                                         f"{W.availability_word(av)} is not answered"}


#: version 12: the visible classes that say the product cannot be bought NOW
NOT_NOW12 = ("out_of_stock", "backorder", "preorder")


def not_now12(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str]) -> Dict[str, Any]:
    """Version 12 (microdata LimitedAvailability next to "Dieser
    Artikel steht derzeit nicht zur Verfügung", InStock next to "Lieferung in 5-7 Werktagen / Wird
    bestellt" - CONFLICTs where the reader judged the structured answer wrong / incomplete): when the product's BUY BOX
    states a NOT-NOW class (out of stock, on order, pre-order) and the structured availability says it is in stock (or
    a few are left), the visible class is the answer (its schema word: ``OutOfStock``, ``BackOrder``, ``PreOrder``) and
    the structured value is kept as evidence. Never the other way round: a visible "in stock" against a structured "out
    of stock" stays a CONFLICT. ``{}`` when the rule does not apply."""
    av = fields.get("availability")
    if not av or availability_class(av) not in ("in_stock", "limited"):
        return {}
    v9 = sources.get("text_rule") == "9"
    text = _text_of(page, sources)
    box = buy_box(text, deglue(fields.get("name")) if v9 else fields.get("name"))
    if sources.get("stock_rule") == "13":
        # version 13: a not-now statement of ANOTHER product (a carousel tile in the box) is not the product's
        box, _gone = own_box13(box, _price_of(fields.get("price")))
    if sources.get("statement_rule") == "10":
        visible, _ev = box_class10(box, _price_of(fields.get("price")))
    else:
        visible = text_availability_class(box, v8=True, v9=v9)
    if visible not in NOT_NOW12:
        return {}
    word = _CLASS_WORD.get(visible, visible)
    return {"structured": W.availability_word(av), "structured_class": availability_class(av), "visible_class": visible,
            "visible_statement": visible_statement(box, visible, v8=True, v9=v9), "statement_kind": "visible class",
            "answered": word, "why": f"the buy box states {visible.replace('_', ' ')}: the visible statement is the "
                                     f"answer, not the structured {W.availability_word(av)}"}


#: version 13: a short line that is a price and nothing else (a tile's price: "22,52 €", "€ 22.52", "22,52 EUR*")
_BARE_PRICE13 = re.compile(r"^(?:(?:€|\$|£|eur|chf|pln|zł|kr|kč)\s*)?\d[\d.,\s\u00a0\u202f]*"
                           r"(?:\s*(?:€|\$|£|eur|chf|pln|zł|kr|kč|,-))?\s*\*?$", re.I)
_CURRENCY13 = re.compile(r"(€|\$|£|\beur\b|\bchf\b|\bpln\b|zł|\bkr\b|kč)", re.I)
#: version 13: the lines before / after a NOT-NOW statement in which its price is looked for
OWN_LINES13 = 2


def _bare_price13(line: Any) -> Optional[float]:
    t = str(line or "").strip()
    if not t or len(t) > 24 or not _BARE_PRICE13.match(t) or not _CURRENCY13.search(t):
        return None
    return W.parse_price(t)


def own_box13(box: Any, price: Optional[float]) -> Tuple[str, List[str]]:
    """Version 13 (a voucher page whose "Lieferzeit: Derzeit nicht verfügbar" over
    "22,52 €" in a carousel of OTHER products inside the voucher's buy box would read as the voucher's out of stock,
    next to the voucher's own "Lieferzeit: 1 bis 3 Werktage"): the buy box without the lines that
    state a NOT-NOW class (out of stock, on order, pre-order) for ANOTHER product - a short line with a bare price line of
    another price within ``OWN_LINES13`` lines and the product's own price nowhere within them. Returns ``(the box, the
    lines left out)``; the box unchanged when the product's price is not known."""
    t = str(box or "")
    if price is None:
        return t, []
    lines = t.split("\n")
    drop: List[int] = []
    for i, ln in enumerate(lines):
        if _line_class10(ln.strip()) not in NOT_NOW12:
            continue
        win = [lines[j] for j in range(max(0, i - OWN_LINES13), min(len(lines), i + OWN_LINES13 + 1)) if j != i]
        bare = [p for p in (_bare_price13(x) for x in win) if p is not None]
        if bare and all(abs(p - float(price)) >= 0.011 for p in bare) and not any(visible_price(x, price) for x in win):
            drop.append(i)
    if not drop:
        return t, []
    return "\n".join(ln for j, ln in enumerate(lines) if j not in drop), [lines[j].strip()[:120] for j in drop]


def _structured_statements(page: Dict[str, Any], fields: Dict[str, Any], value: str) -> List[Dict[str, str]]:
    """The chosen product's STRUCTURED availability statements (each same-named JSON-LD product, the microdata) - the
    ones ``field_conflicts`` compares."""
    prod = page_product(page, value, str(fields.get("name") or ""))
    out: List[Dict[str, str]] = []
    for f in _same_product(page.get("facts") or [], prod):
        if availability_class(f.get("availability")):
            out.append({"source": "jsonld", "value": W.availability_word(f.get("availability")),
                        "class": availability_class(f.get("availability"))})
    md = page.get("microdata") or {}
    if availability_class(md.get("availability")):
        out.append({"source": "microdata", "value": W.availability_word(md.get("availability")),
                    "class": availability_class(md.get("availability"))})
    return out


def word13(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str], value: str = "") -> Dict[str, Any]:
    """Version 13 (JSON-LD that states LimitedAvailability AND InStock for the
    product, its buy box "Online sofort lieferbar"; version 11 merges the two classes and version 12 answers the first
    word, LimitedAvailability - wrong): when the product's structured statements name DIFFERENT words of the two
    classes version 11 merges (a few left / in stock) and the buy box visibly states one of the two, that statement's
    structured word is the answer (the other kept as evidence). ``{}`` when the rule does not apply."""
    av = fields.get("availability")
    st = [x for x in _structured_statements(page, fields, value) if x["class"] in ("in_stock", "limited")]
    if len({x["value"] for x in st}) < 2 or len({x["class"] for x in st}) < 2:
        return {}
    v9 = sources.get("text_rule") == "9"
    price = _price_of(fields.get("price"))
    box, _gone = own_box13(buy_box(_text_of(page, sources), deglue(fields.get("name")) if v9 else fields.get("name")),
                           price)
    if sources.get("statement_rule") == "10":
        visible, _ev = box_class10(box, price)
    else:
        visible = text_availability_class(box, v8=True, v9=v9)
    pick = next((x["value"] for x in st if x["class"] == visible), "")
    if not pick or pick == W.availability_word(av):
        return {}
    return {"structured": [x["value"] for x in st], "structured_class": availability_class(av), "visible_class": visible,
            "visible_statement": visible_statement(box, visible, v8=True, v9=v9), "statement_kind": "visible class",
            "answered": pick, "why": f"the structured data name {' and '.join(sorted({x['value'] for x in st}))}; the buy "
                                     f"box states {visible.replace('_', ' ')}: {pick} is the answer"}


def _same_product(facts: Sequence[Dict[str, Any]], prod: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The page's JSON-LD products that are the chosen product (its name, folded)."""
    if not prod:
        return []
    n = W.fold(prod.get("name"))
    return [f for f in facts if n and W.fold(f.get("name")) == n] or [prod]


def field_conflicts(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str],
                    value: str, v8: bool = False) -> Dict[str, Dict[str, Any]]:
    """Web-watch: the fields on which the page's STRUCTURED data (JSON-LD, microdata) and its
    VISIBLE text disagree - never answered silently by either side (``{field: evidence}``, empty when
    none). Availability: every structured statement of the chosen product (each same-named JSON-LD
    product, the microdata) and the visible text's statement, in ``AVAILABILITY``; more than one
    class is a conflict (a side that states nothing is not a side). Name: a structured name that
    shares no word with the page's heading while the heading or title names the input better.
    ``v8`` (a version-8 tool): the visible statement read in the product's buy box
    (``buy_box``) with version 8's vocabulary (the unavailable phrases of the 11 languages; ``text_availability``),
    and also the price (``price_conflict``)."""
    out: Dict[str, Dict[str, Any]] = {}
    v9 = sources.get("text_rule") == "9"
    text = _text_of(page, sources)
    prod = page_product(page, value, str(fields.get("name") or ""))
    structured: List[Dict[str, str]] = []
    for f in _same_product(page.get("facts") or [], prod):
        if availability_class(f.get("availability")):
            structured.append({"source": "jsonld", "value": W.availability_word(f.get("availability")),
                               "class": availability_class(f.get("availability"))})
    md = page.get("microdata") or {}
    if availability_class(md.get("availability")):
        structured.append({"source": "microdata", "value": W.availability_word(md.get("availability")),
                           "class": availability_class(md.get("availability"))})
    vtext = buy_box(text, deglue(fields.get("name")) if v9 else fields.get("name")) if v8 else text
    if v8 and sources.get("stock_rule") == "13":
        # version 13: another product's not-now statement in the box is no side of a conflict
        vtext, _gone = own_box13(vtext, _price_of(fields.get("price")))
    ev10: Dict[str, Any] = {}
    site = site_availability15(page, fields, sources, value) if sources.get("evidence_rule") == "15" else {}
    if site.get("class") or site.get("supports"):
        # version 15: the site's own rendering is the visible side (``site_availability15``)
        visible = site.get("class") or (availability_class(fields.get("availability")) if site.get("supports") else "")
        ev10 = {"statement": site.get("line") or ""}
    elif v8 and sources.get("statement_rule") == "10":
        # version 10: the box's legends are no statement (``box_class10``)
        visible, ev10 = box_class10(vtext, _price_of(fields.get("price")))
    else:
        visible = text_availability_class(vtext, v8=v8, v9=v9)
    classes = {s["class"] for s in structured} | ({visible} if visible else set())
    if sources.get("stock_rule") in ("11", "12", "13"):
        # version 11 (InStock with "nur noch 6 auf Lager!", "nur noch wenige Artikel",
        # "derniers articles en stock", LimitedAvailability next to InStock): a few left is IN STOCK - "limited" and
        # "in_stock" are one class here
        classes = {"in_stock" if c == "limited" else c for c in classes}
    if sources.get("stock_rule") in ("12", "13") and visible in NOT_NOW12 and structured and \
            all(s["class"] in ("in_stock", "limited") for s in structured):
        # version 12: the visible not-now statement is the answer (``availability_answer``), no CONFLICT
        classes = {visible}
    if structured and len(classes) > 1:
        own = [s["class"] for s in structured if s["source"] == sources.get("availability")] or [structured[0]["class"]]
        why = []
        if visible and visible != own[0]:
            why.append("the page's structured data and its visible text disagree")
        if len({s["class"] for s in structured}) > 1:
            why.append("the page's structured data disagree")
        out["availability"] = {"structured": structured, "visible": visible,
                               "visible_statement": (ev10.get("statement") if ev10.get("statement") else
                                                     visible_statement(vtext, visible, v8=v8, v9=v9)),
                               "compiled_source": sources.get("availability", ""), "why": "; ".join(why)}
        if ev10.get("legends"):
            out["availability"]["legends"] = ev10["legends"]
    name = str(fields.get("name") or "")
    heads = [str(h) for h in page.get("h1") or [] if str(h).strip()]
    if name and heads and sources.get("name") in ("jsonld", "microdata", "og"):
        shared = set(W.content_tokens(name)) & set(W.content_tokens(heads[0]))
        best_head = max(name_share(h, value) for h in heads + [str(page.get("title") or "")])
        if not shared and best_head > name_share(name, value):
            out["name"] = {"structured": [{"source": sources.get("name", ""), "value": name[:160]}],
                           "visible": heads[0][:160], "compiled_source": sources.get("name", ""),
                           "why": "the structured name shares no word with the page's heading, which names the "
                                  "input better"}
    if v8:
        # version 8: a structured price against the buy box's visible CURRENT price (a sale next to a regular)
        out.update(price_conflict(page, fields, sources, value))
    return out


def page_product(page: Dict[str, Any], value: str = "", name_hint: str = "") -> Optional[Dict[str, Any]]:
    """The page's own product (schema.org JSON-LD): the one named by the hint or the
    input, else the only one."""
    facts = page.get("facts") or []
    for want in (name_hint, value):
        if want:
            hits = [f for f in facts if f.get("name") and (names_value(f["name"], want) or
                                                           name_share(f["name"], want) >= MIN_NAME_SHARE)]
            if hits:
                return hits[0]
    return W.main_product(facts)


def visible_price(text: Any, price: Optional[float]) -> bool:
    if price is None:
        return False
    return any(abs(n - float(price)) < 0.011 for n in W.parse_numbers(str(text or "")[:40000]))


def _price_of(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return W.parse_price(str(v))


def _price11(v: Any) -> Optional[float]:
    """Version 11 (microdata ``price`` "8.159" read as 8159.0 and a CONFLICT
    against the shown 8,16 €): a STRUCTURED price (microdata, Open Graph / product meta, JSON-LD) is a machine number - a
    dot is its decimal mark, never a thousands group."""
    if isinstance(v, str) and W._MACHINE_NUMBER.fullmatch(v):
        return float(v.strip())
    return _price_of(v)


def _structured11(page: Dict[str, Any], sources: Dict[str, str]) -> Dict[str, Any]:
    """Version 11: the page with its JSON-LD facts read as machine numbers (``number_rule: "11"``)."""
    if sources.get("number_rule") == "11" and page.get("jsonld"):
        return dict(page, facts=W.product_facts(page.get("jsonld") or [], machine=True))
    return page


def field_sources(page: Dict[str, Any], reported: Dict[str, Any], value: str,
                  v8: bool = False, v9: bool = False, v10: bool = False) -> Tuple[Dict[str, str], List[str]]:
    """Where the demonstrated product page states each field (compile time): the
    source that holds the value the demonstrator reported (verified), else the first
    available in the order JSON-LD, microdata, Open Graph, heading/text. Returns
    (sources, notes); a missing name or price source is a compile failure."""
    notes: List[str] = []
    md = page.get("microdata") or {}
    meta = page.get("meta") or {}
    prod = page_product(page, value, str(reported.get("name") or ""))
    text = str(page.get("main_text") or page.get("text") or "")
    src: Dict[str, str] = {}
    # name
    if prod and prod.get("name"):
        src["name"] = "jsonld"
    elif md.get("name"):
        src["name"] = "microdata"
    elif meta.get("og:title"):
        src["name"] = "og"
    elif page.get("h1"):
        src["name"] = "h1"
    # price
    rp = _price_of(reported.get("price"))
    cands = []
    if prod and prod.get("price") is not None:
        cands.append(("jsonld", float(prod["price"])))
    if _price_of(md.get("price")) is not None:
        cands.append(("microdata", _price_of(md.get("price"))))
    for k in ("product:price:amount", "og:price:amount"):
        if _price_of(meta.get(k)) is not None:
            cands.append(("og", _price_of(meta.get(k))))
            break
    if rp is not None:
        hit = [s for s, p in cands if abs(p - rp) < 0.011]
        if hit:
            src["price"] = hit[0]
            notes.append(f"price: the reported {rp} is the page's {hit[0]} price")
        elif visible_price(text, rp):
            src["price"] = "text"
            notes.append(f"price: the reported {rp} is shown in the text only")
    if "price" not in src and cands:
        src["price"] = cands[0][0]
        notes.append(f"price: {cands[0][0]} (the reported price {rp} matched no source)")
    # availability
    if prod and W.availability_word(prod.get("availability")):
        src["availability"] = "jsonld"
    elif md.get("availability"):
        src["availability"] = "microdata"
    elif v10 and box_class10(buy_box(text, reported.get("name")), _price_of(reported.get("price")))[0]:
        # version 10: the box's legends are no statement
        src["availability"] = "text"
    elif not v10 and text_availability(buy_box(text, reported.get("name")) if (v8 or v9) else text, v8=v8, v9=v9):
        src["availability"] = "text"
    else:
        src["availability"] = "none"
    if v8:
        # version 8: the text source and every check read with version 8's vocabulary
        src["availability_vocab"] = "8"
    # web-watch (a note, the source stays): the demonstrator's reported availability vs the source's
    rav = text_availability_class(reported.get("availability"), v8=v8, v9=v9) or \
        availability_class(reported.get("availability"))
    sav = availability_class(prod.get("availability") if (prod and src["availability"] == "jsonld") else
                             md.get("availability") if src["availability"] == "microdata" else "")
    if rav and sav and rav != sav:
        notes.append(f"availability: the reported {str(reported.get('availability'))[:60]!r} ({rav}) disagrees with "
                     f"the page's {src['availability']} ({sav}); every run checks the page for a conflict")
    src["url"] = "canonical" if page.get("canonical") else "location"
    return src, notes


#: a currency-marked price in a product page's main text (the ``text`` price source), in its first characters
TEXT_PRICE = re.compile(r"(\d[\d .,]*\s?(?:€|eur|kr|sek|nok|dkk|zl|pln|chf|£|\$|:-))|((?:€|£|\$)\s?\d[\d.,]*)", re.I)
TEXT_PRICE_CHARS = 8000


def text_price(text: str, at: str = "", name: str = "") -> Optional["re.Match"]:
    """The text price a tool reads (at run time, and in the compile's self-check): the first currency-marked number
    of the main text (``at`` empty: the version-6 rule), or - ``at == "after_name"`` - the first one AFTER the
    product's name in the text (None when the text does not name it). A page's first price can be its
    free-shipping threshold ("Gratis Versand ... ab 40 €"), above the product's own."""
    t = str(text or "")[:TEXT_PRICE_CHARS]
    start = 0
    if at == "after_name":
        words = [re.escape(w) for w in str(name or "").split() if w]
        m = re.search(r"\s+".join(words), t, re.I) if words else None
        if not m:
            return None
        start = m.end()
    return TEXT_PRICE.search(t, start)


# ---------------------------------------------------------------------------
# version 8 ("Regulärer Preis: 59,99 €" before "Verkaufspreis: 49,99 €", where
# the text price took the first): the CURRENT price where the buy box shows a regular / former price next to it
# ---------------------------------------------------------------------------

def _labels(block: str) -> str:
    """A '|'-separated block of folded phrases as one alternation (spaces flexible, '.' optional)."""
    out = []
    for p in sorted({x.strip() for x in block.replace("\n", "|").split("|") if x.strip()}, key=lambda x: (-len(x), x)):
        out.append(re.escape(p).replace(r"\ ", r"\s+").replace(r"\.", r"\.?"))
    return "(?:" + "|".join(out) + ")"


#: the label of a REGULAR / former / reference price, in the 11 languages (folded; it ends the text before the price)
REGULAR_PRICE_LABELS = _labels("""
regularer preis|regulaerer preis|uvp|unverbindliche preisempfehlung|empfohlener verkaufspreis|statt|anstatt|vorher|
bisher|fruher|alter preis|ursprunglicher preis|ursprunglich|streichpreis|listenpreis|normalpreis|originalpreis|
niedrigster preis der letzten 30 tage|
rrp|was|regular price|original price|list price|retail price|compare at|compare at price|reg. price|msrp|usual price|
previous price|old price|lowest price in the last 30 days|
prix normal|prix barre|au lieu de|ancien prix|prix conseille|prix de reference|prix initial|prix public conseille|
prix le plus bas des 30 derniers jours|
invece di|prezzo di listino|prezzo consigliato|prezzo pieno|prezzo originale|prima|
prezzo piu basso degli ultimi 30 giorni|
precio anterior|antes|pvpr|precio recomendado|precio original|precio de venta recomendado|
precio mas bajo de los ultimos 30 dias|
adviesprijs|van|oude prijs|normale prijs|oorspronkelijke prijs|laagste prijs van de afgelopen 30 dagen|
ordinarie pris|ord. pris|tidigare pris|rek. pris|ursprungligt pris|lagsta pris senaste 30 dagarna|
normalpris|vejl. pris|vejledende pris|tidligere pris|laveste pris de seneste 30 dage|
veil. pris|veiledende pris|ordinaer pris|ordinær pris|før|
normaalihinta|norm. hinta|ovh|aiempi hinta|alin hinta 30 paivan aikana|
cena regularna|cena katalogowa|przed obnizka|stara cena|poprzednia cena|cena poprzednia|najnizsza cena z 30 dni
""")
#: the label of the CURRENT (selling, sale) price
CURRENT_PRICE_LABELS = _labels("""
verkaufspreis|angebotspreis|aktionspreis|sonderpreis|jetzt|jetzt nur|nur|ihr preis|unser preis|
sale|sale price|now|now only|our price|your price|offer price|deal price|special price|
prix promo|prix solde|prix de vente|maintenant|seulement|
prezzo scontato|prezzo promozionale|prezzo di vendita|ora|solo|
precio oferta|precio de venta|ahora|oferta|
nu|voor|nu voor|actieprijs|aanbiedingsprijs|verkoopprijs|
kampanjpris|rea|nu endast|tilbudspris|tilbud|nå|
tarjoushinta|tarjous|nyt|
cena promocyjna|promocja|teraz|cena sprzedazy
""")
#: a saving or a shipping cost shown next to a price: never the product's price
OTHER_PRICE_LABELS = _labels("""
sie sparen|ersparnis|you save|save|economisez|economie|risparmi|ahorra|ahorro|bespaar|je bespaart|spar|du sparer|
saastat|saasta|oszczedzasz|zzgl|versandkosten|shipping|livraison|verzendkosten|frakt|spedizione|envio
""")
RX_REGULAR_LABEL = re.compile(r"(?<![a-z0-9])" + REGULAR_PRICE_LABELS + r"\s*[:.\-–]?\s*$")
RX_CURRENT_LABEL = re.compile(r"(?<![a-z0-9])" + CURRENT_PRICE_LABELS + r"\s*[:.\-–!]?\s*$")
RX_OTHER_LABEL = re.compile(r"(?<![a-z0-9])" + OTHER_PRICE_LABELS + r"[\s:.]*$")
#: a unit or base price ("(145,00 € / 1 Liter)", "16,46 € pro ST", "Grundpreis: 2,50 €"): never the product's price
RX_UNIT_AFTER = re.compile(r"^\s*\*?\s*(?:/|pro\s|per\s|je\s|par\s|al\s|por\s|/\s*\d)")
RX_UNIT_LABEL = re.compile(r"(?:grundpreis|entspricht|unit price|base price|prix au (?:kg|litre|kilo)|prezzo al|"
                           r"precio por|jmf|jamforpris|jamfor|enhetspris|literpreis|kilopreis|\()\s*:?\s*$")
#: prices further apart than this (characters between them) are not one price display
PRICE_CLUSTER_GAP = 120
#: how much text before a price can be its label
PRICE_LABEL_CHARS = 48


#: version 15: the longest label line a site's own price label is learned from (folded characters)
PRICE_LABEL_LINE15 = 40


def label_line15(text: Any, prev: int, at: int) -> str:
    """Version 15: a price's own label line - the last non-empty line of the text between the previous price
    (``prev``) and this one (``at``), folded, without its trailing colon or star; "" when it is longer than
    ``PRICE_LABEL_LINE15`` or holds a digit."""
    seg = str(text or "")[max(0, prev):max(0, at)]
    line = next((ln for ln in reversed(seg.split("\n")) if ln.strip()), "")
    f = W.fold(line).strip(" :.-–*")
    return f if f and len(f) <= PRICE_LABEL_LINE15 and not re.search(r"\d", f) else ""


def price_marks(text: Any, start: int = 0, labels: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Version 8: the currency-marked prices of a product page's text from ``start`` (``TEXT_PRICE`` in its first
    ``TEXT_PRICE_CHARS``), each with its label (the folded text before it, back to the previous price) and its
    kind: "regular" (a regular / former / reference price label), "current" (a selling / sale price label),
    "unit" (a unit or base price), "other" (a saving, a shipping cost) or "" (no label). ``labels`` (version 15:
    the site's own labels a compile learned, ``{"regular": [...], "current": [...]}``): a price whose label line
    (``label_line15``) is one of them takes that kind before the label lists."""
    t = str(text or "")[:TEXT_PRICE_CHARS]
    out: List[Dict[str, Any]] = []
    prev = 0                                           # the first price's label may start before ``start``
    own_regular = set((labels or {}).get("regular") or [])
    own_current = set((labels or {}).get("current") or [])
    for m in TEXT_PRICE.finditer(t, max(0, int(start or 0))):
        raw = " ".join(m.group(0).split())
        val = W.parse_price(raw)
        if val is None:
            continue
        lab = W.fold(t[max(prev, m.start() - PRICE_LABEL_CHARS):m.start()])
        after = W.fold(t[m.end():m.end() + 24])
        own = label_line15(t, prev, m.start()) if (own_regular or own_current) else ""
        if RX_UNIT_AFTER.match(after) or RX_UNIT_LABEL.search(lab):
            kind = "unit"
        elif RX_OTHER_LABEL.search(lab) or re.match(r"^\s*\*?\s*(?:versand|shipping|porto|frakt)", after):
            kind = "other"
        elif own and own in own_regular:
            kind = "regular"
        elif own and own in own_current:
            kind = "current"
        elif RX_REGULAR_LABEL.search(lab):
            kind = "regular"
        elif RX_CURRENT_LABEL.search(lab):
            kind = "current"
        else:
            kind = ""
        out.append({"at": m.start(), "end": m.end(), "raw": raw, "value": val, "label": lab[-PRICE_LABEL_CHARS:],
                    "kind": kind})
        prev = m.end()
    return out


def current_price(text: Any, start: int = 0, struck: Sequence[Any] = (), labels: Optional[Dict[str, Any]] = None,
                  struck_first: bool = False) -> Dict[str, Any]:
    """Version 8: the price display that starts at ``start`` - the first price there and the prices that follow it
    within ``PRICE_CLUSTER_GAP`` characters (``cluster``) - and its CURRENT price: when the display shows a REGULAR
    price (a regular / former label, or - the page's own styling - a price the page strikes through, ``struck``,
    next to one it does not) and another price, the current price is the one labelled as the selling price, else
    the first unlabelled one; ``current`` is None when the display shows no regular price (one price: nothing to
    choose). ``{"first", "current", "regular", "cluster"}``. Version 15: ``labels`` (``price_marks``), and
    ``struck_first`` - the page's own styling before a label: a price it strikes through is a regular price also
    where a label calls it the selling one."""
    marks = price_marks(text, start, labels=labels)
    if not marks:
        return {"first": None, "current": None, "regular": [], "cluster": []}
    cl = [marks[0]]
    for m in marks[1:]:
        if m["at"] - cl[-1]["end"] > PRICE_CLUSTER_GAP:
            break
        cl.append(m)
    sv = set()
    for x in struck or ():
        v = x if isinstance(x, (int, float)) else W.parse_price(str(x or ""))
        if v is not None:
            sv.add(round(float(v), 2))
    shown = {round(m["value"], 2) for m in cl if m["kind"] in ("", "current")}
    if sv and shown - sv:
        for m in cl:
            if (m["kind"] == "" or (struck_first and m["kind"] == "current")) and round(m["value"], 2) in sv:
                m["kind"] = "struck"
    reg = [m for m in cl if m["kind"] in ("regular", "struck")]
    cands = [m for m in cl if m["kind"] in ("", "current")]
    cur = None
    if reg and cands:
        cur = next((m for m in cands if m["kind"] == "current"), cands[0])
    return {"first": cl[0], "current": cur, "regular": reg, "cluster": cl}


def name_anchor(text: Any, name: Any) -> Optional[int]:
    """Where the product's name ends in the text (``text_price``'s "after_name" place); None when not named."""
    t = str(text or "")[:TEXT_PRICE_CHARS]
    words = [re.escape(w) for w in str(name or "").split() if w]
    m = re.search(r"\s+".join(words), t, re.I) if words else None
    return m.end() if m else None


def price_display(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Version 8: the product's buy-box price display (``current_price``) - where the tool reads a TEXT price (its
    compiled place), else after the product's name (else at the text's first price); None when a text price has no
    place on this page."""
    text = _text_of(page, sources)
    name = deglue(fields.get("name")) if sources.get("text_rule") == "9" else str(fields.get("name") or "")
    struck = page.get("struck_prices") or ()
    kw = _price_rules15(sources)
    if sources.get("price") == "text":
        m = text_price(text, str(sources.get("price_at") or ""), name)
        return current_price(text, m.start(), struck, **kw) if m else None
    at = name_anchor(text, name)
    return current_price(text, at or 0, struck, **kw)


def _price_rules15(sources: Dict[str, str]) -> Dict[str, Any]:
    """Version 15's current-price reading for a tool's ``sources``: the page's styling first and the site's own
    labels (``price_labels``) before the lists; ``{}`` for an older tool."""
    if sources.get("evidence_rule") != "15":
        return {}
    labels = sources.get("price_labels")
    return {"struck_first": True, "labels": labels if isinstance(labels, dict) else None}


def price_conflict(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str],
                   value: str) -> Dict[str, Dict[str, Any]]:
    """Version 8: when the buy box shows a regular price next to the current one (``price_display``), every
    STRUCTURED price of the product (each same-named JSON-LD product's offer, the microdata; Open Graph only as the
    compiled source) and the answered price must be the CURRENT price - a disagreement is a CONFLICT naming both
    sides (``{"price": evidence}``), never either side silently. A display with one price checks nothing here
    (``check_product`` still needs the answered price shown on the page)."""
    disp = price_display(page, fields, sources)
    if not disp or disp["current"] is None:
        return {}
    cur = float(disp["current"]["value"])
    sides: List[Dict[str, Any]] = []
    # version 11: the structured sides read as machine numbers (``number_rule``)
    page = _structured11(page, sources)
    pnum = _price11 if sources.get("number_rule") == "11" else _price_of
    prod = page_product(page, value, str(fields.get("name") or ""))
    for f in _same_product(page.get("facts") or [], prod):
        if f.get("price") is not None and pnum(f.get("price")) is not None:
            sides.append({"source": "jsonld", "value": pnum(f.get("price"))})
    md = page.get("microdata") or {}
    if pnum(md.get("price")) is not None:
        sides.append({"source": "microdata", "value": pnum(md.get("price"))})
    src = str(sources.get("price") or "")
    p = fields.get("price")
    if src == "og" and p is not None:
        sides.append({"source": "og", "value": float(p)})
    bad = [x for x in sides if abs(float(x["value"]) - cur) >= 0.011]
    if p is not None and abs(float(p) - cur) >= 0.011 and not any(x["source"] == src for x in sides):
        bad.append({"source": src or "answer", "value": float(p)})
        sides.append(bad[-1])
    if not bad:
        return {}
    shown = " / ".join(f"{m['label'][-28:].strip()} {m['raw']}".strip() for m in disp["cluster"][:4])
    return {"price": {"structured": sides, "visible": cur, "visible_statement": shown,
                      "regular": [m["value"] for m in disp["regular"]], "compiled_source": src,
                      "why": "the page's structured price and its visible current price disagree"}}


# ---------------------------------------------------------------------------
# version 9 (voucher pages that declare ANOTHER voucher's page as their
# canonical link; a canonical link that is the product family's URL, which shows another
# variant): the answer's URL is the URL loaded - the canonical link only when it names the same product
# ---------------------------------------------------------------------------

#: query parameters that never select a product or a variant (tracking, session, search referral): a canonical link
#: that drops only these is the clean URL of the page loaded. Folded names; ``utm_*`` too. Any other parameter may
#: select a variant (``variant``, ``color``, ``size``, ``sku``, ``id``, ``attribute_*``, ...).
URL_NOISE_PARAMS = frozenset("""
gclid gbraid wbraid fbclid msclkid dclid yclid twclid ttclid li_fat_id mc_cid mc_eid _ga _gl _hsenc _hsmi _ke igshid
srsltid ref ref_ referer referrer source src campaign cmp affiliate aff partner
sid session sessionid sess sessid phpsessid jsessionid cfid cftoken zenid oscsid xtcsid
search q query s ssearch searchterm search_term keyword keywords term sq qs _pos _sid _ss _psq _fid _v pos position
searchid search_id hitid queryid query_id fromsearch
""".split())
#: an id published by the product that a canonical link may carry (characters, letters or digits only)
URL_ID_MIN = 4


def _url_noise(k: str) -> bool:
    k = str(k or "").strip().lower()
    return k in URL_NOISE_PARAMS or k.startswith("utm_")


def _url_parts(u: Any, noise: Optional[Any] = None) -> Tuple[str, str, List[Tuple[str, str]]]:
    """(host without "www.", the decoded path without a trailing slash - lower case -, the sorted parameters that may
    select a product or variant). ``noise``: the rule for a parameter that never selects one (``_url_noise``)."""
    try:
        sp = urllib.parse.urlsplit(str(u or "").strip())
    except ValueError:
        return "", "", []
    host = (sp.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = urllib.parse.unquote(sp.path or "").rstrip("/").lower() or "/"
    is_noise = noise or _url_noise
    ident = sorted((k.lower(), v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
                   if not is_noise(k))
    return host, path, ident


def page_url_noise15(page: Dict[str, Any]) -> Any:
    """Version 15: the rule for a parameter that never selects a product, read in the page's own links first. A
    parameter that tells two of the page's links to one path apart (``/p/shirt?s=M``, ``?s=L``) selects a variant,
    whatever its name; one whose value varies from link to link while their paths tell the targets apart
    (``url_identity.page_noise``) does not, nor does a standard tracking or click id (``url_identity.is_tracking``);
    the names of ``URL_NOISE_PARAMS`` decide the rest."""
    from anyuse.execution import url_identity as UI
    hrefs = [str(e.get("href")) for e in page.get("elements") or []
             if isinstance(e, dict) and e.get("tag") == "a" and e.get("href")]
    noise = {k.lower() for k in UI.page_noise(hrefs)}
    by: Dict[Tuple[str, str], set] = {}
    for h in hrefs:
        try:
            sp = urllib.parse.urlsplit(h)
        except ValueError:
            continue
        for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True):
            by.setdefault((k.lower(), (sp.hostname or "").lower() + (sp.path or "")), set()).add(v)
    selects = {k for (k, _path), vs in by.items() if len(vs) >= 2}

    def rule(k: str) -> bool:
        k = str(k or "").strip().lower()
        if k in selects:
            return False
        if k in noise or UI.is_tracking(k):
            return True
        return _url_noise(k)
    return rule


def _numbers9(s: Any) -> set:
    """The digit runs of a text (folded), as written (leading zeros dropped)."""
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", fold9(s))}


def canonical_same_product(loaded: str, canonical: str, page: Dict[str, Any], name: str,
                           v15: bool = False) -> Tuple[bool, str]:
    """Version 9: does the page's canonical link DEMONSTRABLY name the product of the page loaded? True only:
    the same path and the same product-selecting parameters (the canonical is the clean URL of the page loaded:
    only tracking / session / search-referral parameters differ - ``URL_NOISE_PARAMS``); or - another path, and the
    URL loaded selects nothing by a parameter - the canonical carries the product's own SKU / MPN / GTIN (JSON-LD), or
    its slug (the last path segment) has >= 2 words, every one of them a word of the product's name, and the same
    numbers as the name ("...-feuer-25" for "Feuer 100 €" names another voucher). ``v15``
    (version 15): which parameters never select a product is read in the page's own links first
    (``page_url_noise15``)."""
    rule = page_url_noise15(page) if v15 else None
    hl, pl, ql = _url_parts(loaded, rule)
    hc, pc, qc = _url_parts(canonical, rule)
    if not hc or hl != hc:
        return False, "the canonical link is on another host"
    if pl == pc:
        if ql == qc:
            return True, "the canonical link is the clean URL of the page loaded (only tracking / session parameters differ)"
        lost = [f"{k}={v}" for k, v in ql if (k, v) not in qc] + [f"{k}={v}" for k, v in qc if (k, v) not in ql]
        return False, (f"the canonical link and the URL loaded differ in a parameter that may select a variant "
                       f"({', '.join(lost)[:120]}): another variant")
    if ql:
        return False, (f"the URL loaded selects the product by a parameter the canonical link lacks "
                       f"({', '.join(k + '=' + v for k, v in ql)[:120]}), on another path")
    prod = page_product(page, "", name) or {}
    ctoks = set(tokens9(urllib.parse.unquote(urllib.parse.urlsplit(canonical).path + " " +
                                             urllib.parse.urlsplit(canonical).query)))
    for kind, val in (("SKU", prod.get("sku")), ("GTIN", prod.get("gtin"))):
        v = "".join(tokens9(val))
        if len(v) >= URL_ID_MIN and v in ctoks:
            return True, f"the canonical link carries the product's {kind} {val}"
    seg = [x for x in urllib.parse.unquote(urllib.parse.urlsplit(canonical).path).split("/") if x]
    slug = re.sub(r"\.(?:html?|php|aspx?|jsp)$", "", seg[-1].lower()) if seg else ""
    words = content9(slug)
    heads = [str(h) for h in page.get("h1") or []]
    named = set(content9(" ".join([name] + heads)))
    if len(words) >= 2 and all(w in named for w in words) and _numbers9(slug) == _numbers9(name):
        return True, "the canonical link's slug names the product (its every word and number)"
    missing = [w for w in words if w not in named]
    return False, ("the canonical link names another product (its slug " + repr(slug[:80]) +
                   (f" has {', '.join(missing[:4])}, which the product's name has not" if missing else
                    f" and the product's name {name[:60]!r} differ in their numbers or the slug names too little") + ")")


def answer_url(page: Dict[str, Any], fields: Dict[str, Any], sources: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
    """Version 9: the answer's URL - the URL LOADED (the page's location after redirects); its canonical link
    only when it names the same product (``canonical_same_product``). Returns ``(url, evidence)``: the evidence
    (``{"loaded", "canonical", "answered", "same_product", "why"}``) keeps the canonical link whenever it differs from
    the URL loaded; ``{}`` when there is none or it is the URL loaded."""
    here = str(page.get("url") or "")
    canon = str(page.get("canonical") or "")
    if not canon or _norm(canon) == _norm(here):
        return here, {}
    ev: Dict[str, Any] = {"loaded": here[:300], "canonical": canon[:300]}
    if not same_site(canon, host_of(here)) or _norm(canon) == _norm(f"https://{host_of(here)}/"):
        return here, dict(ev, answered=here[:300], same_product=False,
                          why="the canonical link is off the site or the site's home page")
    ok, why = canonical_same_product(here, canon, page, str(fields.get("name") or ""),
                                     v15=sources.get("evidence_rule") == "15")
    url = canon if ok else here
    return url, dict(ev, answered=url[:300], same_product=ok, why=why)


def extract_fields(page: Dict[str, Any], sources: Dict[str, str], value: str) -> Dict[str, Any]:
    """The product fields by the compiled sources (runtime, no model). A version-8 tool's text price
    (``sources["price_rule"] == "current"``) is the CURRENT price of the display it lands on (``current_price``). A
    version-11 tool reads its structured prices as machine numbers (``number_rule``; ``_price11``). A version-15
    tool's fields (``evidence_rule``) read "the same site" of a canonical link as its registrable domain
    (``web_arch.site_evidence``)."""
    with W.site_evidence(W.site_evidence_first() or sources.get("evidence_rule") == "15"):
        return _extract_fields(page, sources, value)


def _extract_fields(page: Dict[str, Any], sources: Dict[str, str], value: str) -> Dict[str, Any]:
    page = _structured11(page, sources)
    pnum = _price11 if sources.get("number_rule") == "11" else _price_of
    md = page.get("microdata") or {}
    meta = page.get("meta") or {}
    prod = page_product(page, value)
    v9 = sources.get("text_rule") == "9"
    text = _text_of(page, sources)
    out: Dict[str, Any] = {"name": "", "price": None, "currency": "", "availability": None, "url": ""}
    s = sources.get("name")
    if s == "jsonld" and prod:
        out["name"] = str(prod.get("name") or "")
    elif s == "microdata":
        out["name"] = str(md.get("name") or "")
    elif s == "og":
        out["name"] = str(meta.get("og:title") or "")
    if not out["name"] and page.get("h1"):
        out["name"] = str((page.get("h1") or [""])[0])
    s = sources.get("price")
    if s == "jsonld" and prod and prod.get("price") is not None:
        out["price"], out["currency"] = float(prod["price"]), str(prod.get("currency") or "")
    elif s == "microdata" and pnum(md.get("price")) is not None:
        out["price"], out["currency"] = pnum(md.get("price")), str(md.get("currency") or "")
    elif s == "og":
        for k in ("product:price:amount", "og:price:amount"):
            if pnum(meta.get(k)) is not None:
                out["price"] = pnum(meta.get(k))
                out["currency"] = str(meta.get(k.replace("amount", "currency")) or "")
                break
    elif s == "text":
        # the first price-like number with a currency mark in the main text; a tool whose compile's self-check
        # needed it (``price_at``): the first one after the product's name
        m = text_price(text, str(sources.get("price_at") or ""), deglue(out["name"]) if v9 else out["name"])
        if m:
            raw = m.group(0)
            if sources.get("price_rule") == "current":
                # version 8: a regular / struck price next to the current one is never the answer
                disp = current_price(text, m.start(), page.get("struck_prices") or (), **_price_rules15(sources))
                if disp["current"] is not None:
                    raw = disp["current"]["raw"]
            out["price"] = W.parse_price(raw)
            cur = W.currencies_in(raw)
            out["currency"] = sorted(cur)[0] if cur else ""
    if not out["currency"] and out["price"] is not None:
        cur = W.currencies_in(text[:8000])
        out["currency"] = sorted(cur)[0] if len(cur) == 1 else ""
    s = sources.get("availability")
    if s == "jsonld" and prod and W.availability_word(prod.get("availability")):
        out["availability"] = W.availability_word(prod.get("availability"))
    elif s == "microdata" and md.get("availability"):
        out["availability"] = W.availability_word(md.get("availability"))
    elif s == "text":
        if sources.get("statement_rule") == "10":
            # version 10: the box's legends are no statement
            cls10, _ev = box_class10(buy_box(text, deglue(out["name"])), out["price"])
            out["availability"] = _CLASS_WORD.get(cls10) or None
        elif v9:
            out["availability"] = text_availability(buy_box(text, deglue(out["name"])), v9=True) or None
        elif sources.get("availability_vocab") == "8":
            out["availability"] = text_availability(buy_box(text, out["name"]), v8=True) or None
        else:
            out["availability"] = text_availability(text) or None
    if sources.get("url_rule") == "same_product":
        # version 9: the URL loaded; the canonical link only when it names the same product
        out["url"] = answer_url(page, out, sources)[0]
        return out
    canon = str(page.get("canonical") or "")
    here = str(page.get("url") or "")
    out["url"] = canon if (sources.get("url") == "canonical" and canon and same_site(canon, host_of(here))
                           and _norm(canon) != _norm(f"https://{host_of(here)}/")) else here
    return out


def repeated_title(value: Any) -> Dict[str, str]:
    """Version 7 (a frozen value "Acme MOVE 2 Schwarz Acme MOVE 2 HiFi-Klang per
    USB-C-Kabel" is a result tile's TITLE followed by its SUBTITLE, which repeats the title's leading phrase "Acme
    MOVE 2"): the value split where it repeats its own leading phrase of >= 2 words (>= 2 content words; folded
    words; the longest such phrase) - ``{"title", "phrase", "subtitle", "deduped"}`` (``deduped``: the value with
    the repeated phrase once); ``{}`` when the value repeats no such phrase."""
    words = str(value or "").split()
    f = [W.fold(w) for w in words]
    best: Optional[Tuple[int, int]] = None
    for j in range(2, len(words)):
        n = 0
        while j + n < len(words) and n < j and f[j + n] == f[n]:
            n += 1
        if n >= 2 and len(W.content_tokens(" ".join(words[:n]))) >= 2 and (best is None or n > best[1]):
            best = (j, n)
    if best is None:
        return {}
    j, n = best
    return {"title": " ".join(words[:j]), "phrase": " ".join(words[:n]), "subtitle": " ".join(words[j:]),
            "deduped": " ".join(words[:j] + words[j + n:])}


def identity_values(value: Any) -> List[str]:
    """Version 7: what a product's identity is ALSO scored on (the better share counts): for a value that repeats a
    tile's title with its subtitle (``repeated_title``), the repeated phrase deduped and the tile's TITLE - the words
    before the repeat; the subtitle's own words are the tile's tagline, not the product's name. ``[]`` otherwise."""
    rep = repeated_title(value)
    return [rep["title"]] if rep else []


def check_product(page: Dict[str, Any], fields: Dict[str, Any], value: str, site: str,
                  ident: Sequence[str] = (), v9: bool = False, v15: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
    """Product identity and field sanity (every invocation): the page is an item page
    (not a list), its product name (or heading, or title) names the input, the price
    is positive and shown in the page text, the URL is on the site. ``ident`` (version 7,
    ``identity_values``): other forms of the input the identity may be scored on, the
    better share counting (recorded as ``scored_on``). ``v15``: a version-15 tool's site is its registrable domain
    (``web_arch.site_evidence``), as at its compile."""
    with W.site_evidence(W.site_evidence_first() or v15):
        return _check_product(page, fields, value, site, ident, v9)


def _check_product(page: Dict[str, Any], fields: Dict[str, Any], value: str, site: str, ident: Sequence[str],
                   v9: bool) -> Tuple[bool, str, Dict[str, Any]]:
    checks: Dict[str, Any] = {}
    pd, why = W.product_detail(page, value)
    checks["item_page"] = {"verdict": pd, "why": why}
    if pd is False:
        return False, f"identity: not an item page ({why})", checks
    names = [str(fields.get("name") or "")] + [str(h) for h in page.get("h1") or []] + [str(page.get("title") or "")]
    ns = name_share9 if v9 else name_share       # version 9: a missing / extra space or punctuation tolerated
    share = max(ns(n, value) for n in names)
    checks["identity"] = {"share": round(share, 2), "name": str(fields.get("name") or "")[:120]}
    raw = share
    for alt in ident or ():
        s2 = max(ns(n, alt) for n in names)
        if s2 > share:
            share = s2
            checks["identity"].update(share=round(s2, 2), scored_on=str(alt)[:120], raw_share=round(raw, 2))
    if share < MIN_NAME_SHARE:
        return False, f"identity: the product page names {share:.0%} of the input's words", checks
    if not str(fields.get("name") or "").strip():
        return False, "fields: no product name", checks
    price = fields.get("price")
    text = str(page.get("text") or "") + " " + str(page.get("main_text") or "")
    if v9:
        # version 9: the price is also looked for in the separator text (a price glued to a number is shown)
        text += " " + str(page.get("main_text_sep") or "") + " " + str(page.get("text_sep") or "")
    checks["price"] = {"price": price, "visible": visible_price(text, price)}
    if price is None or not (0 < float(price) < 1_000_000):
        return False, f"fields: no sane price ({price})", checks
    if not visible_price(text, price):
        return False, f"fields: the price {price} is not shown in the page text", checks
    if not same_site(fields.get("url"), site):
        return False, f"fields: the URL {str(fields.get('url'))[:80]} is not on the site", checks
    return True, "identity and fields checked", checks


def landed_product(tool: Dict[str, Any], read: Dict[str, Any], value: str) -> str:
    """Version 7 (a site's search for one product's full name that goes straight to that
    product's own page - no result list): why the result page of a product lookup is ONE product's own page - a
    product-detail page (``web_arch.product_detail``: one schema.org Product, ``og:type`` product, one Product
    microdata item, ...), or, when that is undecided, the tool's compiled product-page structure (its JSON-LD or
    microdata name source names a product and its price source gives a price); "" when it is not (a list page
    never is)."""
    if tool.get("workflow") != "product_lookup":
        return ""
    pd, why = W.product_detail(read, value)
    if pd is True:
        return why
    if pd is None:
        src = tool.get("fields") or {}
        if src.get("name") == "jsonld":
            name = str((page_product(read, value) or {}).get("name") or "")
        elif src.get("name") == "microdata":
            name = str((read.get("microdata") or {}).get("name") or "")
        else:
            name = ""
        if name and extract_fields(read, src, value).get("price") is not None:
            return f"the compiled product-page structure ({src['name']} name and {src.get('price')} price)"
    return ""


def check_landing(page: Dict[str, Any], fields: Dict[str, Any], value: str, site: str,
                  ident: Sequence[str] = (), v9: bool = False, v15: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
    """Version 7: a product page the search LANDED on is the answer only when it IS the input's product - its OWN
    name (the compiled name source; never the heading or the title alone, which a search page echoes) names the
    input (``MIN_NAME_SHARE``; ``ident`` and ``v15`` as in ``check_product``) - and ``check_product``'s item-page,
    identity and field checks pass; else an explicit failure."""
    name = str(fields.get("name") or "")
    share = max((name_share9 if v9 else name_share)(name, v) for v in [value, *ident])
    ok, why, checks = check_product(page, fields, value, site, ident, v9=v9, v15=v15)
    checks["landing"] = {"own_name": name[:120], "share": round(share, 2)}
    if share < MIN_NAME_SHARE:
        return False, (f"identity: the search landed on a product page whose own name names {share:.0%} of the "
                       f"input's words ({name[:80]!r})"), checks
    return ok, why, checks


# ---------------------------------------------------------------------------
# the demonstration's validity (pre-registered, code only)
# ---------------------------------------------------------------------------

def _search_steps(trace: Dict[str, Any]) -> List[int]:
    """Indices of the steps that entered the input value into the site's search: a
    ``search`` whose text names the input's words (the typed text may be the input
    or a part of it holding at least half its content words), or a ``goto`` whose URL
    carries the input."""
    value = str(trace.get("value") or "")
    out = []
    for k, s in enumerate(trace.get("steps") or []):
        a = s.get("action") or {}
        oc = s.get("outcome") or {}
        if not oc.get("ok"):
            continue
        if a.get("action") == "search":
            t = str(a.get("text") or "")
            if W.fold(t) == W.fold(value) or (names_value(value, t) and name_share(t, value) >= 0.5):
                out.append(k)
        elif a.get("action") == "goto" and value_template(str(a.get("url") or ""), value):
            out.append(k)
    return out


def page_after(trace: Dict[str, Any], k: int) -> Dict[str, Any]:
    steps = trace.get("steps") or []
    if k + 1 < len(steps):
        return steps[k + 1].get("page") or {}
    return trace.get("final_page") or {}


#: a demonstration's "no results" for a value its own site lists is read again
#: on the page it reported from by the VERIFIER's own code (``attribute_verify.read_no_results``: the page's own
#: statement, its result-count statement, its links outside the chrome that name the value). Each outcome is typed,
#: never a silent draft: ``no_results_stated`` (the page itself states that this search found nothing - for a value
#: the site lists, so about THIS way of searching: measured, 3 of the 4 such recordings had a refused data request or
#: POST behind the statement, and a retry with another demonstrator found the results), ``no_results_disputed``
#: (the page disagrees: it states a count or shows items naming the value) and ``no_results_unrendered`` (the page shows
#: neither: its results were not drawn). All three stay an invalid demonstration the cascade retries.
NO_RESULTS_STATED = "no_results_stated"
NO_RESULTS_DISPUTED = "no_results_disputed"
NO_RESULTS_UNRENDERED = "no_results_unrendered"


def no_results_reason(trace: Dict[str, Any], det: Dict[str, Any]) -> str:
    """The typed reason for a demonstration that reported no results (``det["no_results"]`` keeps the reading)."""
    from anyuse.execution import attribute_verify as AV
    ks = _search_steps(trace)
    pg = page_after(trace, ks[-1]) if ks else (trace.get("final_page") or {})
    ax = pg.get("ax") if isinstance(pg.get("ax"), dict) and isinstance(pg["ax"].get("nodes"), list) \
        else AV.ax_from_record(pg.get("ax"))
    page = AV.Page(str(pg.get("dom") or ""), str(pg.get("text") or ""), str(pg.get("url") or ""), ax=ax)
    try:
        got = AV.read_no_results(page, str(trace.get("value") or ""))
    except Exception as exc:                           # noqa: BLE001 - an unreadable page is not a confirmation
        got = {"kind": "unrendered", "why": f"the page could not be read again ({type(exc).__name__})"}
    det["no_results"] = got
    kind = {"stated": NO_RESULTS_STATED, "disputed": NO_RESULTS_DISPUTED}.get(got["kind"], NO_RESULTS_UNRENDERED)
    return f"{kind}: the demonstration reported no results; {got['why'][:200]}"


def demo_validity(trace: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Is the one demonstration valid? Code only:
    (1) it ended with ``done`` and a result within the step cap, no CLI error;
    (2) it entered the input into the site's search; (3) no forbidden action was
    attempted; (4) product lookup: it ended on an item page of the site reached from
    the search's result page, and the reported name and price are on that page;
    site search: it reported >= 1 result whose URL is a link of the result page."""
    det: Dict[str, Any] = {}
    wf = trace.get("workflow")
    if wf in ("filtered_search", "paged_search", "visit_results"):
        # version 10: the breadth workflows' validity (``demo_breadth.validity``)
        from anyuse.execution import demo_breadth as DB
        return DB.validity(trace)
    site = str(trace.get("site") or "")
    if trace.get("stop") != "done":
        return False, f"the demonstration did not finish ({trace.get('stop')})", det
    res = trace.get("result") or {}
    if res.get("no_results"):
        return False, no_results_reason(trace, det), det
    if trace.get("forbidden"):
        return False, f"a forbidden action was attempted ({trace['forbidden'][0]})", det
    ks = _search_steps(trace)
    det["search_steps"] = ks
    if not ks:
        return False, "the input value was never entered into the site's search", det
    k = ks[-1]
    after = page_after(trace, k)
    if not same_site(after.get("url"), site):
        return False, "the search's result page is not on the site", det
    fin = trace.get("final_page") or {}
    if wf == "product_lookup":
        if not same_site(fin.get("url"), site):
            return False, "the demonstration ended off the site", det
        opened = [j for j in range(k + 1, len(trace.get("steps") or []))
                  if ((trace["steps"][j].get("action") or {}).get("action") in ("click", "goto")
                      and (trace["steps"][j].get("outcome") or {}).get("ok"))]
        if not opened:
            return False, "no item was opened after the search", det
        if _norm(str(fin.get("url") or "")) == _norm(str(after.get("url") or "")):
            return False, "it ended on the search's result page, not an item's own page", det
        pd, why = W.product_detail(fin, str(trace.get("value") or ""))
        det["item_page"] = {"verdict": pd, "why": why}
        if pd is False:
            return False, f"it ended on a list, not an item page ({why})", det
        name = str(res.get("name") or "")
        heads = " ".join([str(fin.get("title") or ""), " ".join(str(h) for h in fin.get("h1") or []),
                          " ".join(str(f.get("name") or "") for f in fin.get("facts") or [])])
        if not name or name_share(heads, name) < MIN_NAME_SHARE:
            return False, "the final page's title / heading / product data does not name the reported product", det
        rp = _price_of(res.get("price"))
        facts_prices = [float(f["price"]) for f in fin.get("facts") or [] if f.get("price") is not None]
        if rp is None or not (visible_price(fin.get("text"), rp) or any(abs(rp - p) < 0.011 for p in facts_prices)):
            return False, f"the reported price {res.get('price')!r} is not on the final page", det
        return True, "valid", det
    if wf == "site_search":
        got = [r for r in (res.get("results") or []) if isinstance(r, dict) and r.get("url")]
        pages = [after] + [s.get("page") or {} for s in (trace.get("steps") or [])[k + 1:]] + [fin]
        links = {_norm(e.get("href")) for p in pages for e in p.get("elements") or [] if e.get("href")}
        hit = [r for r in got if _norm(urllib.parse.urljoin(str(after.get("url") or ""), str(r["url"]))) in links]
        det["reported"], det["reported_on_page"] = len(got), len(hit)
        if not hit:
            return False, "no reported result is a link of the result page", det
        return True, "valid", det
    return False, f"unknown workflow {wf!r}", det


# ---------------------------------------------------------------------------
# compile: one trace -> a checked tool (or an explicit failure)
# ---------------------------------------------------------------------------

def _input_descriptor(step: Dict[str, Any], version: int = TOOL_VERSION) -> Dict[str, Any]:
    oc = step.get("outcome") or {}
    inp = oc.get("input") or {}
    out = {k: inp.get(k) for k in ("index", "name", "id") if inp.get(k) not in (None, "")}
    if inp.get("ax"):
        # web-ax-tree: the demonstration typed into a field the accessibility tree called a search box; the replay
        # takes it by the same rule (``cdp_guard.AX_INPUT_JS``)
        out["ax"] = True
    label = str((oc.get("submit") or {}).get("label") or "") if isinstance(oc.get("submit"), dict) else ""
    if version >= 14 and label:
        # version 14: the control the demonstration's search was submitted with, among several next to the input
        out["submit_label"] = label[:80]
    return out


#: most reported titles one repeated text that does not name the input - a card label, not names
LABEL_TITLES = "titles_one_label"
LABEL_TITLES_MIN = 4
LABEL_TITLES_SHARE = 0.6


def compile_trace(trace: Dict[str, Any], version: Optional[int] = None) -> Dict[str, Any]:
    """ONE demonstration -> ``{"ok": True, "tool": {...}}`` or ``{"ok": False, "why"}``.

    Reads only the trace's actions and page observations (never a rationale; a
    held-out value is never in a trace). ``version``: the tool version to build (``COMPILE_VERSIONS``; default
    ``TOOL_VERSION``) - each version builds exactly that version's compile (version 8 adds its checks to version 7's,
    version 9 its own to version 8's, and so on)."""
    version = TOOL_VERSION if version is None else int(version)
    if version not in COMPILE_VERSIONS:
        raise ValueError(f"compile_trace builds versions {COMPILE_VERSIONS}, not {version}")
    # version 14 reads the words of every script; an older version only the a-z and 0-9 runs it read before;
    # version 15 reads the site's own evidence before the word lists
    with W.scripts(version >= 14), W.site_evidence(version >= 15):
        return _compile_trace(trace, version)


def _cards_version(version: int) -> Optional[int]:
    """The card read a compile of ``version`` keeps (None: version 8's or 6's, by ``compile_cards``)."""
    if version >= 15:
        return CARDS_VERSION_V15
    if version >= 12:
        return CARDS_VERSION_V12
    if version >= 11:
        return CARDS_VERSION_V11
    return None


def _compile_trace(trace: Dict[str, Any], version: int) -> Dict[str, Any]:
    wf = str(trace.get("workflow") or "")
    site = str(trace.get("site") or "")
    value = str(trace.get("value") or "")
    report: Dict[str, Any] = {"workflow": wf, "site": site}
    breadth = version >= 10 and wf in BREADTH_WORKFLOWS
    if wf not in WORKFLOWS and not breadth:
        return {"ok": False, "why": f"unknown workflow {wf!r}", "report": report}
    ks = _search_steps(trace)
    if not ks:
        return {"ok": False, "why": "no search step entered the demonstrated value", "report": report}
    k = ks[-1]
    if breadth:
        # version 10: a breadth demonstration's search is its last TYPED search (a later navigation carrying the value
        # is a refinement or a page of the same search: ``demo_breadth.search_step``)
        from anyuse.execution import demo_breadth as DB
        k = DB.search_step(trace, ks)
    # version 7: a search whose result is a FEED is never compiled - the
    # demonstration's own last HTML search is (when its trace shows one), else an explicit compile failure
    k_open = k
    feed = search_feed(trace, k)
    if feed:
        html = [j for j in ks if not search_feed(trace, j)]
        report["feed"] = {"search_step": k, "why": feed, "html_search_step": html[-1] if html else None}
        if not html:
            return {"ok": False, "why": f"the demonstration's result page is a feed ({feed}), not an HTML result "
                                        f"list, and its trace shows no HTML search", "report": report}
        k = html[-1]
    step = trace["steps"][k]
    act = step.get("action") or {}
    rpage = page_after(trace, k)
    rurl = str(rpage.get("url") or (step.get("outcome") or {}).get("url_after") or "")
    start = str(trace.get("start_url") or "")
    typed = str(act.get("text") or "") if act.get("action") == "search" else value
    report["search_step"] = k
    report["typed_equals_input"] = W.fold(typed) == W.fold(value)
    if not same_site(rurl, site):
        return {"ok": False, "why": "the search's result page is off the site", "report": report}
    # 1. the search: the site's result URL when it carries the value, else the typed search
    tmpl = value_template(rurl, typed) or (value_template(str(act.get("url") or ""), value)
                                            if act.get("action") == "goto" else "")
    redirected = ""
    if version >= 14 and act.get("action") == "search":
        # version 14: the site redirected its search URL to a landing page (a brand or collection page): the search
        # URL the site formed is replayed, never a template guessed from the landing page's path, and the browser
        # follows the redirect
        # only a document loaded before the result page itself is a hop of its redirect; one loaded after it
        # (a frame of the result page) is not
        rpath = urllib.parse.urlsplit(rurl).path
        docs = [str(u) for u in (step.get("outcome") or {}).get("documents") or []]
        land = next((i for i, u in enumerate(docs) if urllib.parse.urlsplit(u).path == rpath), 0)
        hop = next((u for u in docs[:land] if value_template(u, typed) and same_site(u, site)), "")
        if hop:
            tmpl, redirected = value_template(hop, typed), rurl
    start_page = (trace.get("steps") or [{}])[0].get("page") or {}
    # version 15: the parameter the site's own result URL carried the typed input in, and how the page showed each
    # template, come before the search parameter names (``search_template_ok``)
    own = typed_params(rurl, typed) if version >= 15 else []
    hows = start_page.get("search_url_how") if version >= 15 and isinstance(start_page.get("search_url_how"), dict) \
        else {}
    fallbacks = [t for t in (start_page.get("search_urls") or [])
                 if isinstance(t, str) and search_template_ok(t, how=str(hows.get(t) or ""), own=own)
                 and not feed_url(t)][:2]
    # results that came from the site's read-only POST search after typing are
    # replayed by typing - a results URL loaded without typing gets no POST
    by_post = act.get("action") == "search" and bool(((step.get("outcome") or {}).get("data") or {}).get("search_post"))
    if by_post:
        report["search_post"] = ((step.get("outcome") or {}).get("data") or {}).get("search_post")
    if tmpl and same_site(tmpl.replace("{q}", "x"), site) and not by_post:
        search = {"mode": "url", "template": tmpl, "fallback_templates": fallbacks, "start_url": start}
    elif act.get("action") == "search":
        search = {"mode": "typed", "input": _input_descriptor(step, version), "fallback_templates": fallbacks,
                  "start_url": start, "overlay": _norm(rurl) == _norm(str(step.get("page", {}).get("url") or start))}
    else:
        return {"ok": False, "why": "the search cannot be replayed (no result URL and no typed search)",
                "report": report}
    report["search"] = {k2: v for k2, v in search.items() if k2 != "input"}
    if redirected and search.get("mode") == "url":
        report["search"]["redirected_to"] = redirected
    dom = str(rpage.get("dom") or "")
    tool: Dict[str, Any] = {"version": version, "workflow": wf, "site": site, "start_url": start,
                            "input": {"name": "term", "binding": "the search text is the input term",
                                      "demonstrated_text_was_the_input": report["typed_equals_input"]},
                            "search": search, "model_calls": 0}
    if breadth:
        from anyuse.execution import demo_breadth as DB
        return DB.compile_breadth(trace, tool, report, k, version)
    if wf == "product_lookup":
        steps = trace.get("steps") or []
        opened = None
        for j in range(k_open + 1, len(steps)):
            a = steps[j].get("action") or {}
            oc = steps[j].get("outcome") or {}
            if a.get("action") in ("click", "goto") and oc.get("ok"):
                opened = strip_fragment(str(oc.get("href") or a.get("url") or oc.get("url_after") or ""))
                landed = strip_fragment(str(oc.get("url_after") or ""))
                src_page = steps[j].get("page") or {}
                break
        if not opened:
            return {"ok": False, "why": "no item was opened from the result page", "report": report}
        if feed and feed_page(src_page):
            # the item was opened from the feed: the tool's region is read where the tool searches, on the
            # demonstration's HTML result page - which must list the item
            if not any(_same_result(opened, str(e.get("href") or ""), True) for e in rpage.get("elements") or []):
                return {"ok": False, "why": "the item the demonstration opened from its feed is not a link of its "
                                            "HTML result page", "report": report}
            report["feed"]["item_on_html_page"] = True
            src_page = rpage
        if _norm(str(src_page.get("url") or "")) != _norm(rurl):
            report["note"] = "the item was opened from a later page than the first result page"
            dom = str(src_page.get("dom") or dom)
            rurl = str(src_page.get("url") or rurl)
        region = region_from_dom(dom, rurl, [opened], ax=rpage.get("ax") if isinstance(rpage, dict) else None) or \
            region_from_url(opened)
        if region is None:
            return {"ok": False, "why": "no result region holds the opened item", "report": report}
        rp_cards = src_page if src_page.get("dom") else rpage
        why = compile_cards(region, rp_cards, [(opened, str((trace.get("result") or {}).get("name") or ""),
                                                [landed])], wf, report,
                            legacy=containers_of(src_page if src_page.get("link_sigs") else rpage, [opened]),
                            v8=version >= 8, value=value, start_page=start_page, overlay=bool(search.get("overlay")),
                            v10=version >= 10, cards_version=_cards_version(version),
                            loose=CARDS_LOOSE and version >= 12)
        if why:
            return {"ok": False, "why": why, "report": report}
        fin = trace.get("final_page") or {}
        if fin.get("dom"):
            # web-watch: the Product item's own microdata (the recorded reading took the first
            # itemprop=name inside the item - a nested brand's)
            from anyuse.execution import demo_cards as K
            fin = dict(fin, microdata=K.microdata_of(str(fin.get("dom"))))
            if version >= 8:
                # version 8: the prices the page strikes through (``demo_tool.PAGE_EXTRA_JS_V8`` at run time)
                fin["struck_prices"] = K.struck_of(str(fin.get("dom")))
        srcs, notes = field_sources(fin, trace.get("result") or {}, value, v8=version >= 8, v9=version >= 9,
                                    v10=version >= 10)
        report["field_notes"] = notes
        if version >= 9:
            # version 9: the answer's URL is the URL loaded, the canonical
            # link only when it names the same product (``answer_url``)
            srcs["url_rule"] = "same_product"
            # version 9: the page's statements and prices read in its text with
            # element-boundary separators, deglued, folded with ``fold9`` (``_text_of``)
            srcs["text_rule"] = "9"
        if version >= 10:
            # version 10: a stock-state legend is not a statement
            # (``box_class10``); more delivery-time wording (``_DELIVERY_V10``)
            srcs["statement_rule"] = "10"
        if version >= 11:
            # version 11: a statement that only a few are left does not contradict "in stock"
            # (``field_conflicts``); structured prices are machine numbers (``_price11``)
            srcs["stock_rule"] = "11"
            srcs["number_rule"] = "11"
        if version >= 12:
            # version 12: the buy box's visible
            # NOT-NOW statement (out of stock, on order, pre-order) against a structured in stock is the answer, never a
            # CONFLICT (``not_now12``); version 11's few-left rule stays
            srcs["stock_rule"] = "12"
        if version >= 13:
            # version 13: the product's OWN availability -
            # another product's not-now statement in the buy box is none (``own_box13``); the buy box's class picks
            # between two structured words of the class version 11 merges (``word13``)
            srcs["stock_rule"] = "13"
        if "name" not in srcs or "price" not in srcs:
            return {"ok": False, "why": f"the product page states no stable {'name' if 'name' not in srcs else 'price'}",
                    "report": report}
        if srcs.get("price") == "text" and version >= 8:
            # version 8: a text price is the CURRENT price of the buy box
            # (``current_price``: never a regular / struck price next to it), read AFTER the product's name when that
            # reads the reported price back (the buy box - never a header's "ab 40 €" threshold), else at the main
            # text's first price; neither: after the name when the text names the product (noted)
            srcs, note = _text_price_place_v8(fin, srcs, trace.get("result") or {}, value)
            notes.append(note)
        elif srcs.get("price") == "text":
            # the place of a text price: when the version-6 rule
            # (the main text's first price) does not read the REPORTED price back on the demonstration's own product
            # page and the first price AFTER the product's name does, the tool reads there (``price_at``); else the
            # version-6 rule stays (noted)
            rp = _price_of((trace.get("result") or {}).get("price"))
            first = extract_fields(fin, srcs, value).get("price")
            if rp is not None and (first is None or abs(first - rp) >= 0.011):
                trial = dict(srcs, price_at="after_name")
                got = extract_fields(fin, trial, value).get("price")
                if got is not None and abs(got - rp) < 0.011:
                    srcs = trial
                    notes.append(f"price: the page's first text price is {first}, not the reported {rp}; the first "
                                 f"price after the product's name reads it back (price_at: after_name)")
                else:
                    notes.append(f"price: the text rule reads {first}, not the reported {rp}, and the first price "
                                 f"after the name reads {got} (the version-6 rule kept)")
        if version >= 15:
            # version 15: the product's fields read the site's own evidence before the phrase lists; the buy-box
            # line the site rendered the demonstrated availability with (``learn_availability15``)
            srcs["evidence_rule"] = "15"
            if srcs.get("price") == "text" and srcs.get("price_rule") == "current":
                # the site's own price labels when the label lists do not read the reported price back
                srcs, note15 = learn_price_labels15(fin, srcs, trace.get("result") or {}, value)
                if note15:
                    notes.append(note15)
            anchor = learn_availability15(fin, extract_fields(fin, srcs, value), srcs,
                                          (trace.get("result") or {}).get("availability"))
            if anchor:
                srcs["avail_anchor"] = anchor
        tool.update(region=region, select={"rule": "best_match", "min_share": MIN_NAME_SHARE},
                    fields=srcs, output=["name", "price", "currency", "availability", "url"])
    else:
        res = trace.get("result") or {}
        urls = [urllib.parse.urljoin(rurl, str(r.get("url"))) for r in res.get("results") or []
                if isinstance(r, dict) and r.get("url")]
        # a reported result that links back to the site's own search (a
        # did-you-mean, suggestion or related-search list) is never a result; reported results that are all such links
        # are no results to compile
        eps = search_endpoints([search.get("template")] + list(search.get("fallback_templates") or []) +
                               list(rpage.get("search_urls") or []), rurl, typed)
        sl = [u for u in urls if search_link(u, eps)]
        if sl:
            report["search_links"] = sl[:12]
            if len(sl) == len(urls):
                return {"ok": False, "why": "search_links: the reported results are links to the site's own search "
                                            "(did-you-mean, suggestion or related-search links), never results",
                        "report": report}
            urls = [u for u in urls if u not in sl]
        # (9 of 10 reported titles "sofort versandbereit" - an availability
        # badge - leave every proof value inconclusive because no compiled title can name a value): a list's
        # titles name DIFFERENT results - when most reported titles are one repeated text that does not name the input,
        # they are a card label, not the results' names (typed; the cascade retries the demonstration)
        tl = [" ".join(str(r.get("title") or "").split()).casefold() for r in res.get("results") or []
              if isinstance(r, dict) and r.get("url")]
        if len(tl) >= LABEL_TITLES_MIN:
            top, n_top = Counter(tl).most_common(1)[0]
            if n_top >= LABEL_TITLES_SHARE * len(tl) and not names_value(top, typed):
                report["label_titles"] = {"title": top[:80], "of": f"{n_top}/{len(tl)}"}
                return {"ok": False, "why": f"{LABEL_TITLES}: {n_top} of {len(tl)} reported titles are one repeated text "
                                            f"({top[:60]!r}) that does not name the input: a card label, not the "
                                            f"results' names", "report": report}
        region = region_from_dom(dom, rurl, urls, ax=rpage.get("ax") if isinstance(rpage, dict) else None)
        if region is None and len(urls) == 1:
            region = region_from_url(urls[0])
        if region is None and CONTAINER_REGIONS:
            # no URL-shape cluster holds the results - the result cards' common container
            region = region_from_cards(dom, rurl, urls)
            if region is not None:
                report["region_from"] = "the result cards' container"
        if region is None:
            return {"ok": False, "why": "no result region holds the reported results", "report": report}
        titled = [(urllib.parse.urljoin(rurl, str(r.get("url"))), str(r.get("title") or ""))
                  for r in res.get("results") or [] if isinstance(r, dict) and r.get("url")
                  and urllib.parse.urljoin(rurl, str(r.get("url"))) not in sl]
        why = compile_cards(region, rpage, titled, wf, report, legacy=containers_of(rpage, urls), v8=version >= 8,
                            value=value, start_page=start_page, overlay=bool(search.get("overlay")), v10=version >= 10,
                            cards_version=_cards_version(version),
                            loose=CARDS_LOOSE and version >= 12)
        if why:
            return {"ok": False, "why": why, "report": report}
        if region.get("kind") == "container" and not region.get("cards"):
            # a container region is read by its cards only (its URL pattern takes any page of the site)
            return {"ok": False, "why": "no result region holds the reported results (the cards' container kept no card "
                                        "structure)", "report": report}
        if K_LINK_NAMES():
            region["link_names"] = 3                       # a card link named by its heading, its card's, or
            #                                                its text before its teaser paragraph
        # the site's results have no links only when the demonstration REPORTED a result without one (measured on
        # the build page); otherwise a linkless card at run time is a section or teaser heading, never a row
        region["linkless_results"] = any(isinstance(r, dict) and not r.get("url") and str(r.get("title") or "").strip()
                                         for r in res.get("results") or [])
        tool.update(region=region, top_k=TOP_K, output=["results"])
    tool["hosts"] = demo_hosts(trace, site)
    tool["result_scope"] = result_scope_from(trace, site)
    tool["checks"] = ["search_page", "membership", "query", "empty_state"] + \
        (["identity", "fields"] if wf == "product_lookup" else [])
    tool["compiled_from"] = {"steps": len(trace.get("steps") or []), "search_step": k,
                             "trace_sha256": trace_digest(trace)}
    return {"ok": True, "tool": tool, "report": report}


def _text_price_place_v8(fin: Dict[str, Any], srcs: Dict[str, str], reported: Dict[str, Any],
                         value: str) -> Tuple[Dict[str, str], str]:
    """Version 8: the place of a TEXT price (``srcs["price"] == "text"``) on the demonstration's product page, read
    with the current-price rule (``price_rule: current``): after the product's name, else the main text's first
    price - the first that reads the reported price back; else after the name when the text names the product, else
    the first price (noted). Returns (sources, note)."""
    rp = _price_of(reported.get("price"))
    base = dict(srcs, price_rule="current")
    base.pop("price_at", None)
    got: Dict[str, Optional[float]] = {}
    for at in ("after_name", ""):
        trial = dict(base, price_at=at) if at else dict(base)
        got[at or "first"] = extract_fields(fin, trial, value).get("price")
        p = got[at or "first"]
        if rp is not None and p is not None and abs(p - rp) < 0.011:
            where = "after the product's name" if at else "at the main text's first price"
            return trial, f"price: the current-price rule reads the reported {rp} back {where} (price_rule: current)"
    keep = dict(base, price_at="after_name") if got.get("after_name") is not None else base
    return keep, (f"price: the current-price rule reads {got.get('after_name')} after the name and {got.get('first')} "
                  f"at the first price, not the reported {rp} (kept: "
                  f"{'after the name' if keep.get('price_at') else 'the first price'})")


def learn_price_labels15(fin: Dict[str, Any], srcs: Dict[str, str], reported: Dict[str, Any],
                         value: str) -> Tuple[Dict[str, str], str]:
    """Version 15 (compile time): the site's own price labels, when the current-price rule with the label lists does
    not read the reported price back from the demonstration's product page: in the price display at the compiled
    place, the label line (``label_line15``) of the price the demonstration reported is a selling-price label and
    that of another price of the display a regular-price label. Kept (``price_labels``) only when the rule with them
    reads the reported price back. Returns (sources, note)."""
    rp = _price_of(reported.get("price"))
    if rp is None:
        return srcs, ""
    got = extract_fields(fin, srcs, value).get("price")
    if got is not None and abs(got - rp) < 0.011:
        return srcs, ""
    disp = price_display(fin, extract_fields(fin, srcs, value), srcs)
    if not disp or not disp.get("cluster"):
        return srcs, ""
    t = str(_text_of(fin, srcs))[:TEXT_PRICE_CHARS]
    cur: List[str] = []
    reg: List[str] = []
    prev = 0
    for m in disp["cluster"]:
        lab = label_line15(t, max(prev, m["at"] - 4 * PRICE_LABEL_CHARS), m["at"])
        prev = m["end"]
        if not lab or m["kind"] in ("unit", "other"):
            continue
        (cur if abs(float(m["value"]) - rp) < 0.011 else reg).append(lab)
    if not reg or set(reg) & set(cur):
        return srcs, ""
    trial = dict(srcs, price_labels={"regular": sorted(set(reg)), "current": sorted(set(cur))})
    got2 = extract_fields(fin, trial, value).get("price")
    if got2 is None or abs(got2 - rp) >= 0.011:
        return srcs, ""
    return trial, (f"price: the site's own labels {sorted(set(reg))!r} (regular) and {sorted(set(cur))!r} (selling) "
                   f"read the reported {rp} back (price_labels)")


def unread_before(items: Sequence[Dict[str, Any]], unread: Sequence[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    """The results the structure could not read that sit among the list's top ``k`` (before its k-th item;
    anywhere when it has fewer than ``k``)."""
    if not unread:
        return []
    cut = items[k - 1].get("pos") if len(items) >= k else None
    return [u for u in unread if cut is None or int(u.get("pos") or 0) < int(cut)]


# ---------------------------------------------------------------------------
# web-watch: the hosts a tool may navigate (its demonstration's, the site's registrable domain)
# ---------------------------------------------------------------------------

def _host_ok(host: str, site: str) -> bool:
    """``host`` is one of the site's own: an exact host name under the site's registrable domain, never a
    host the government rule refuses (``ANYUSE_REFUSE_GOV_HOSTS``; every request of a run is still checked against
    NEVER_CONTACT, robots and the government rule by the guard)."""
    from anyuse.execution.gov_hosts import government_refusal
    from anyuse.execution.web_scope import registrable
    h = str(host or "").lower()
    if not h or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", h):
        return False
    reg = registrable(site)
    return bool(reg) and "." in reg and registrable(h) == reg and not government_refusal(h)


def demo_hosts(trace: Dict[str, Any], site: str) -> List[str]:
    """The hosts the demonstration's pages and navigations were on (its start URL, every page, every
    link it followed, the result page, the final page) that are the site's own (``_host_ok``), sorted -
    the tool's DECLARED hosts: the site gate lets a run of the tool navigate exactly these
    (a site whose search is on another subdomain, which the gate would refuse)."""
    urls = [str(trace.get("start_url") or ""), str((trace.get("final_page") or {}).get("url") or "")]
    for st in trace.get("steps") or []:
        urls.append(str((st.get("page") or {}).get("url") or ""))
        oc = st.get("outcome") or {}
        urls += [str(oc.get("url_after") or ""), str(oc.get("href") or "")]
        a = st.get("action") or {}
        if a.get("action") == "goto":
            urls.append(str(a.get("url") or ""))
    hosts = {host_of(u) for u in urls if u.lower().startswith(("http://", "https://"))}
    hosts.add(str(site or "").lower())
    return sorted(h for h in hosts if h and _host_ok(h, site))


def demo_result_urls(trace: Dict[str, Any]) -> List[str]:
    """The URLs of the demonstration's REPORTED results: a list's ``result.results[].url`` / ``result.items[].url``, a
    product's ``result.url`` and the page the demonstration ended on - resolved against the final page."""
    res = trace.get("result") or {}
    fin = trace.get("final_page") or {}
    base = str(fin.get("url") or trace.get("start_url") or "")
    raw: List[str] = []
    if isinstance(res, dict):
        for key in ("results", "items"):
            raw += [str(r.get("url") or "") for r in res.get(key) or [] if isinstance(r, dict) and r.get("url")]
        if res.get("url"):
            raw.append(str(res["url"]))
            if fin.get("url"):
                raw.append(str(fin["url"]))
    out = []
    for u in raw:
        try:
            full = urllib.parse.urljoin(base, u) if base else u
        except ValueError:
            continue
        if full.lower().startswith(("http://", "https://")):
            out.append(full)
    return out


def result_scope_from(trace: Dict[str, Any], site: str) -> Dict[str, Any]:
    """The tool's RESULT SCOPE at compile time - the hosts (exact host names, never the registrable
    domain) and the first path segments the demonstration's reported results link to. No reported URL: the site
    host alone (what is known). A host the government rule refuses (``ANYUSE_REFUSE_GOV_HOSTS``) is never part of a
    scope."""
    urls = demo_result_urls(trace)
    hosts = sorted({host_of(u) for u in urls if host_of(u) and _host_ok(host_of(u), site)})
    paths = set()
    for u in urls:
        try:
            segs = [x for x in urllib.parse.urlsplit(u).path.split("/") if x]
        except ValueError:
            continue
        paths.add("/" + segs[0] if segs else "/")
    if not hosts:
        hosts = [str(site or "").lower()] if site else []
    return {"hosts": hosts, "paths": sorted(paths), "results": len(urls)}


def result_scope(tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The RESULT SCOPE a run reads cards against: the compiled ``result_scope`` (its hosts); for a tool compiled
    without one its declared ``hosts`` (the demonstration's own hosts), else its ``site``. None when nothing is
    known (no scope, no hosts, no site): the card reader then reports the list uncertain, never ``ok``."""
    sc = tool.get("result_scope")
    if isinstance(sc, dict) and sc.get("hosts"):
        return {"hosts": [str(h).lower() for h in sc["hosts"] if h], "paths": list(sc.get("paths") or []),
                "source": "compiled"}
    hosts = [str(h).lower() for h in tool.get("hosts") or [] if h]
    if hosts:
        return {"hosts": hosts, "paths": [], "source": "hosts"}
    site = str(tool.get("site") or "").lower()
    if site:
        return {"hosts": [site], "paths": [], "source": "site"}
    return None


def site_hosts_policy(tool: Dict[str, Any]) -> Dict[str, List[str]]:
    """The run's origin policy for a tool (``cdp_guard.OriginPolicy`` extra: ``{site: [other hosts]}``):
    its declared hosts that are the site's own (``_host_ok``) and the site's base or its subdomain (the
    guard's own rule for a site's extra hosts). A version-5 tool declares none: ``{}``, today's guard.
    Every host of a site shares ONE pacer slot and ONE request / byte count, so a declared host never adds
    rate or budget, and the site gate (``traffic_gate.SiteGate``, held for the site) covers exactly them."""
    site = str(tool.get("site") or "").lower()
    if not v6(tool) or not site:
        return {}
    base = site[4:] if site.startswith("www.") else site
    extra = [h for h in tool.get("hosts") or [] if h != site and _host_ok(h, site) and
             (h == base or h.endswith("." + base))]
    return {site: sorted(set(extra))} if extra else {}


def _same_result(a: str, b: str, by_path: bool) -> bool:
    from anyuse.execution import demo_cards as K
    return _norm(a) == _norm(b) or (by_path and K._path_key(a) == K._path_key(b))


def structure_check(page: Dict[str, Any], region: Dict[str, Any], wanted: Sequence[Sequence[Any]],
                    workflow: str) -> Tuple[bool, Dict[str, Any]]:
    """Does the compiled result structure read the demonstration's OWN result page as the
    demonstration did (the tool's reading of the recorded page, its DOM and its visible links)?
    Every demonstrated result the page shows in the region is an item, each in its own card; a
    product's link may carry another query (the item a redirect or a typed URL landed on)."""
    by_path = workflow == "product_lookup"
    vis = region_links(page, region)
    urls = [[str(w[0])] + [str(x) for x in (w[2] if len(w) > 2 else []) or [] if x] for w in wanted]
    expect = [us for us in urls if any(_same_result(u, str(e.get("href")), by_path) for u in us for e in vis)]
    items = region_items(page, region)
    got = [it["url"] for it in items]
    missing = [us[0] for us in expect if not any(_same_result(u, g, by_path) for u in us for g in got)]
    det = {"expected_on_page": len(expect), "items": len(items), "missing": missing[:5],
           "first_items": [{"title": it["title"][:80], "url": it["url"][:160], "sub_links": it.get("sub_links")}
                           for it in items[:5]]}
    return bool(items) and not missing, det


#: version 8: a structure whose result-page items and START-page items (the page without the query) are this
#: alike (shared / all, by URL) reads the same items for any query
SAME_ITEMS_SHARE = 0.8
#: version 15: a block whose items differ on the start page depends on the query when its result-page items name the
#: value's words at least this much (mean share) and more than its start-page items; a block that rotates its items
#: names them no more than chance, and its id / class names decide
QUERY_BLOCK_SHARE15 = 0.5


def query_region(page: Dict[str, Any], start_page: Optional[Dict[str, Any]], specs: Sequence[Dict[str, Any]],
                 wanted: Sequence[Sequence[Any]], value: str, workflow: str, overlay: bool = False,
                 v10: bool = False, v15: bool = False) -> Dict[str, Any]:
    """Version 8 (a "NEUE REPORTS" sidebar must not be compiled as the result list):
    is the candidate result structure ``specs`` the QUERY-DEPENDENT region of the demonstration's result page?
    Its cards (``demo_cards.structure_items``) on the result page and on the demonstration's START page (the
    site without the query):

    * no demonstrated result's card is in a sidebar / widget (``aside`` / ``nav`` / ``footer`` / ``header``, a
      container named or labelled related / new / popular / latest / neue / aktuell: ``demo_cards.widget_why``);
    * it does not read the same items for any query: its result-page and start-page item sets are less than
      ``SAME_ITEMS_SHARE`` alike (shared / all, by URL);
    * its items name the demonstration value's words more than chance: the mean share of the value's content words
      its result-page cards name (``demo_cards.card_words``) is above the mean on its start-page cards - chance;
      a structure that shows no card on the start page is the search's own (nothing to compare).

    ``overlay`` (a typed search whose results render over the start page): the run leaves out every link of the
    pre-search page (``demo_tool.run_tool``'s ``exclude``), so its result-page cards are checked without them.
    ``{"ok", "why", ...the measures}``. ``v10`` (version 10): a container whose own id / class names a sidebar
    is a widget too (``demo_cards.sidebar_name``). ``v15`` (version 15): the site's own evidence first - the page's
    landmarks (``aside``, ``nav``, ``header``, ``footer``); then each block of the structure (one spec's cards) that
    holds a demonstrated result and shows two or more cards on the start page is measured against them: the same
    items there are a block that does not depend on the query (``static_specs``: never the results; a widget); other
    items whose result-page cards name the value (``QUERY_BLOCK_SHARE15``) more than its start-page cards clear the
    block; a widget's NAMES (its id / class words, its label) decide what is left - a block the start page shows no
    card of, and one that shows other items there without naming the value more (it rotates them); then the two
    whole-structure tests."""
    from anyuse.execution import demo_cards as K
    by_path = workflow == "product_lookup"
    items = K.structure_items(str(page.get("dom") or ""), str(page.get("url") or ""), specs, v10=v10, v15=v15)
    start = K.structure_items(str((start_page or {}).get("dom") or ""), str((start_page or {}).get("url") or ""),
                              specs, v10=v10, v15=v15) if (start_page or {}).get("dom") else []
    if overlay and start_page:
        pre = {_norm(str(e.get("href"))) for e in start_page.get("elements") or [] if e.get("href")}
        pre |= {_norm(h) for _n, h in K.anchors(K.parse(str(start_page.get("dom") or ""))[0],
                                                 str(start_page.get("url") or "")) if h}
        items = [it for it in items if _norm(it["url"]) not in pre]
    urls = [str(w[0]) for w in wanted]
    mine = [it for it in items if any(_same_result(u, it["url"], by_path) for u in urls)]
    widget = next((it["widget"] for it in mine if it["widget"]), "")

    def named(xs: Sequence[Dict[str, Any]]) -> float:
        return round(sum(name_share(it["text"], value) for it in xs) / len(xs), 3) if xs else 0.0
    ru = {_norm(it["url"]) for it in items if it["url"]}
    su = {_norm(it["url"]) for it in start if it["url"]}
    same = round(len(ru & su) / len(ru | su), 3) if ru else 0.0
    rate, chance = named(items), named(start)
    det = {"items": len(items), "start_items": len(start), "same_items_share": same, "named_share": rate,
           "chance_share": chance, "widget": widget}
    if v15:
        landmark = next((it["widget"] for it in mine if it["widget"].startswith("inside the page's")), "")
        if landmark:
            return dict(det, widget=landmark, ok=False, why=f"a demonstrated result's card is {landmark}")
        static: List[int] = []
        named_widget = ""
        for si in sorted({it.get("spec") for it in mine if it.get("spec") is not None}):
            r_i = {_norm(it["url"]) for it in items if it.get("spec") == si and it["url"]}
            s_i = {_norm(it["url"]) for it in start if it.get("spec") == si and it["url"]}
            if len(s_i) >= 2:
                # the block's own differential: what it shows without the query
                if len(r_i & s_i) >= 2 and len(r_i & s_i) / len(r_i | s_i) >= SAME_ITEMS_SHARE:
                    static.append(si)
                    continue
                here = named([it for it in items if it.get("spec") == si])
                if here >= QUERY_BLOCK_SHARE15 and here > named([it for it in start if it.get("spec") == si]):
                    continue                           # other items that name the value: the query's own block
            if not named_widget:
                named_widget = next((it["widget"] for it in mine if it.get("spec") == si and it["widget"]), "")
        det["widget"] = ""
        if static:
            chains = [" > ".join(str(c) for c in (specs[si].get("chain") or [])[-3:]) for si in static]
            w = f"in a block that shows the same items on the start page ({chains[0][:80]})"
            return dict(det, widget=w, static_specs=static, ok=False,
                        why=f"a demonstrated result's card is {w}: the same items for any query")
        if named_widget:
            return dict(det, widget=named_widget, ok=False, why=f"a demonstrated result's card is {named_widget}")
        if widget:
            det["widget_name_not_asked"] = widget
    elif widget:
        return dict(det, ok=False, why=f"a demonstrated result's card is {widget}")
    if len(start) >= 2 and len(ru & su) >= 2 and same >= SAME_ITEMS_SHARE:
        return dict(det, ok=False, why=f"it reads the same items on the start page (the two item sets {same:.0%} "
                                       f"alike): the same items for any query")
    if len(start) >= 2 and rate <= chance:
        return dict(det, ok=False, why=f"its items name the value's words no more than chance ({rate:.0%} vs "
                                       f"{chance:.0%} on the start page)")
    return dict(det, ok=True, why="the query-dependent region" + ("" if len(start) >= 2 else
                                                                  " (the start page shows none of its cards)"))


def compile_cards(region: Dict[str, Any], page: Dict[str, Any], wanted: Sequence[Sequence[Any]], workflow: str,
                  report: Dict[str, Any], legacy: Sequence[str] = (), v8: bool = False, value: str = "",
                  start_page: Optional[Dict[str, Any]] = None, overlay: bool = False, v10: bool = False,
                  cards_version: Optional[int] = None, loose: bool = False) -> str:
    """``_compile_cards`` (below); a version-11 compile (``cards_version`` 11) describes the cards with
    ``demo_cards.bem_free``'s descriptors, the ones its run-time read (``CARDS_JS_V11``) uses. ``loose``:
    the cards are tried first as ``loose`` specs (``demo_cards.chain_matches``), checked the same way."""
    from anyuse.execution import demo_cards as K
    with K.bem_free(int(cards_version or 0) >= CARDS_VERSION_V11):
        return _compile_cards(region, page, wanted, workflow, report, legacy=legacy, v8=v8, value=value,
                              start_page=start_page, overlay=overlay, v10=v10, cards_version=cards_version, loose=loose)


def _compile_cards(region: Dict[str, Any], page: Dict[str, Any], wanted: Sequence[Sequence[Any]], workflow: str,
                   report: Dict[str, Any], legacy: Sequence[str] = (), v8: bool = False, value: str = "",
                   start_page: Optional[Dict[str, Any]] = None, overlay: bool = False, v10: bool = False,
                   cards_version: Optional[int] = None, loose: bool = False) -> str:
    """The result CARDS of the region (``demo_cards.result_cards`` on the recorded result page),
    kept when the structure reads the demonstration's own results back (``structure_check``); else
    the title links' own chains (``kind: title``); else an explicit failure (returns why). With no
    link of a demonstrated result in the recorded DOM, the older per-URL region (its non-empty
    containers) is kept. ``v8`` (version 8): a structure is kept only when it is also the
    query-dependent region (``query_region``); else the next candidate region - the demonstrated results'
    links outside sidebars, widgets and the rejected structures - is tried the same way; else an explicit
    failure. ``v10`` (version 10): the widget rule names sidebars by their id /
    class too, and when a structure is rejected for a widget the next candidate region leaves out the demonstrated
    results whose every link is in a sidebar / widget (a sidebar item the demonstrator reported:
    ``demo_cards.widget_results``; noted) and avoids only the widgets and the structures rejected for another
    reason."""
    from anyuse.execution import demo_cards as K
    st = K.result_cards(str(page.get("dom") or ""), str(page.get("url") or ""), wanted, region.get("regex") or "")
    rep: Dict[str, Any] = {"results": st["results"]}
    report["structure"] = rep
    if not st["cards"]:
        region["containers"] = [c for c in legacy if c]
        rep["kept"] = "per-URL region (no link of a demonstrated result in the recorded DOM)"
        return ""
    rounds: List[Tuple[str, Dict[str, Any], Sequence[Sequence[Any]]]] = [("", st, wanted)]
    rejected: List[str] = []
    rejected_specs: List[Dict[str, Any]] = []
    static_specs: List[Dict[str, Any]] = []
    widget_rejected = False
    v15c = int(cards_version or 0) >= CARDS_VERSION_V15
    while rounds:
        label, sti, wanted = rounds.pop(0)
        tries = [("cards" + label, sti["cards"])]
        if loose:
            # a card spec that also takes its result list's sibling cards whose further class names a
            # type (``demo_cards.chain_matches``) - kept only when it passes every check the exact spec would
            ls = [dict(sp, loose=True) if sp.get("kind") == "card" else sp for sp in sti["cards"]]
            if ls != sti["cards"]:
                tries.insert(0, ("cards" + label, ls))          # (its checks carry ``loose``; the label stays)
        titles = [{"kind": "title", "chain": s["chain"], "rels": [""]} for s in _title_specs(page, sti)]
        if titles and titles != sti["cards"]:
            tries.append(("title links" + label, titles))
        cv = cards_version or (CARDS_VERSION_V8 if v8 else CARDS_VERSION)
        for name, specs in tries:
            cand = dict(region, cards=specs, cards_version=cv)
            ok, det = structure_check(page, cand, wanted, workflow)
            rep.setdefault("checks", []).append(dict(det, specs=name, ok=ok,
                                                     **({"loose": True} if any(sp.get("loose") for sp in specs) else {})))
            if ok and v8:
                q = query_region(page, start_page, specs, wanted, value, workflow, overlay=overlay, v10=v10,
                                 v15=v15c)
                rep["checks"][-1]["query_region"] = q
                if not q["ok"]:
                    if not any(sp.get("loose") for sp in specs):
                        rejected.append(f"{name}: {q['why']}")     # (a loose try's verdict is in its check record)
                    # version 15: a block that shows the same items without the query is avoided by its cards
                    static_specs.extend(dict(specs[si], loose=False) for si in q.get("static_specs") or ()
                                        if dict(specs[si], loose=False) not in static_specs)
                    if v10 and q.get("widget"):
                        widget_rejected = True
                    elif not any(sp.get("loose") for sp in specs):
                        # (a rejected loose try leaves the next candidate region as the exact try would)
                        rejected_specs.extend(sp for sp in specs if sp not in rejected_specs)
                    continue
            if ok:
                region.pop("containers", None)
                region["cards"] = specs
                region["cards_version"] = cv
                known = known_other(page, region, wanted, workflow)
                if known:
                    region["known_other"] = known
                rep["kept"] = name
                if rejected:
                    rep["rejected"] = rejected
                return ""
        if v8 and rejected and not label:
            # version 8: a rejected structure falls back to the NEXT candidate region - the demonstrated results'
            # links outside sidebars and widgets (``demo_cards.widget_why``) and outside the rejected structures'
            # cards - checked the same way
            want2 = wanted
            if v10 and widget_rejected:
                # version 10: a demonstrated result whose every link is in a sidebar / widget is left out
                drop = K.widget_results(str(page.get("dom") or ""), str(page.get("url") or ""), wanted,
                                        v15=v15c, static_specs=static_specs)
                if drop:
                    rep["dropped_widget_results"] = drop[:10]
                    want2 = [w for w in wanted if str(w[0] or "") not in drop]
            nxt = K.result_cards(str(page.get("dom") or ""), str(page.get("url") or ""), want2,
                                 region.get("regex") or "", avoid_widgets=True,
                                 avoid_specs=rejected_specs + [sp for sp in static_specs if sp not in rejected_specs],
                                 v10=v10, v15=v15c) if want2 else {"cards": []}
            if nxt["cards"] and nxt["cards"] != st["cards"]:
                rounds.append((" (the next candidate region)", nxt, want2))
    if rejected:
        rep["rejected"] = rejected
        return ("the result structure is not the query-dependent region (" + "; ".join(rejected)[:300] + ")")
    return "the result structure does not read the demonstration's own results back"


def known_other(page: Dict[str, Any], region: Dict[str, Any], wanted: Sequence[Sequence[Any]],
                workflow: str) -> List[str]:
    """Web-watch, the calibration: the link chains of the demonstration page's result-pattern links in
    the results container that no compiled card holds and that sit BEFORE its last reported result - a
    layout the demonstrator saw among its results and did not count (topic tiles
    above the articles). At run time such a link is not an unseen result card; any other one is."""
    if not cards_v6(region):
        return []
    rr = region_read(page, region)
    by_path = workflow == "product_lookup"
    urls = [str(w[0]) for w in wanted]
    last = [it["pos"] for it in rr["items"] if any(_same_result(u, it["url"], by_path) for u in urls)]
    if not last:
        return []
    return sorted({u["chain"] for u in rr["incomplete"] if u.get("chain") and u["pos"] < max(last)})


def _title_specs(page: Dict[str, Any], st: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The title links' own chains (the fallback of a card that does not read the page back)."""
    out: List[Dict[str, Any]] = []
    for r in st["results"]:
        spec = {"kind": "title", "chain": r.get("title_chain"), "rels": [""]}
        if r.get("title_role"):
            spec["roles"] = [r["title_role"]]
        if r.get("title_chain") and spec not in out:
            out.append(spec)
    return out


def trace_digest(trace: Dict[str, Any]) -> str:
    slim = {"workflow": trace.get("workflow"), "site": trace.get("site"),
            "actions": [s.get("action") for s in trace.get("steps") or []],
            "urls": [(s.get("page") or {}).get("url") for s in trace.get("steps") or []]}
    return hashlib.sha256(json.dumps(slim, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tool_digest(tool: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------------------
# the checks on a result page (runtime; pure given the page read)
# ---------------------------------------------------------------------------

#: the sparse-list evidence rule (a switch for the measurement's before / after reading only)
SPARSE_EVIDENCE_RULE = True


def sparse_evidence_of(read: Dict[str, Any], value: str, n_rows: int) -> Dict[str, Any]:
    """The independent evidence that a sparse list is the query's (``attribute_verify.sparse_evidence``) from
    the page reader's visible text, headings, breadcrumb and applied filters (checked boxes, selected options)."""
    from anyuse.execution import attribute_verify as AV
    text = str(read.get("main_text") or read.get("text") or "")
    page = AV.Page("", text, str(read.get("url") or ""), str(read.get("title") or ""), h1=list(read.get("h1") or []))
    v = AV.sparse_evidence(value, n_rows, page, headings=list(read.get("headings") or []),
                           breadcrumb=list(read.get("breadcrumb") or []), applied=AV.applied_texts(read.get("elements") or []))
    d = v.to_dict()
    count = next((a.get("status") for a in d["attributes"] if a.get("attr") == "count"), None)
    sources = [r.get("value") for a in d["attributes"] if a.get("attr") == "names" for r in a.get("readings") or []]
    return {"verdict": d["verdict"], "why": d["why"], "count": count, "sources": sources, "accepted": False,
            "attributes": [{k: a.get(k) for k in ("attr", "status", "why")} for a in d["attributes"]]}


def judge_result_page(tool: Dict[str, Any], read: Dict[str, Any], value: str,
                      exclude: Sequence[str] = (), site_page: bool = False) -> Dict[str, Any]:
    """What the tool may say for ``value`` on the result page ``read``:
    ``{"status": "list"|"pick"|"no_results"|"failed"|"incomplete", ...}`` (version 7 also "landed": the page is
    one product's own page, which the run checks; and "incomplete" with ``ambiguous``). The empty state needs
    the page's own "no results" statement AND no region item naming the value; items
    that do not belong to the query are an explicit failure, never an answer.

    ``site_page``: the tool's own run, which may also read the site's own empty page the tool learned at its
    proof (``tool["empty_page"]``, ``empty_page.match_text``) before the statements. Every other caller (the
    oracle, the breadth judge) keeps the statements alone, independent of the tool. A version-15 tool reads the
    site's own evidence first (``web_arch.site_evidence``)."""
    with W.site_evidence(v15(tool)):
        return _judge_result_page(tool, read, value, exclude, site_page)


def _judge_result_page(tool: Dict[str, Any], read: Dict[str, Any], value: str, exclude: Sequence[str],
                       site_page: bool) -> Dict[str, Any]:
    site = str(tool.get("site") or "")
    url = str(read.get("url") or "")
    text = str(read.get("main_text") or "") + "\n" + str(read.get("text") or "")
    out: Dict[str, Any] = {"url": url}
    if not same_site(url, site):
        return dict(out, status="failed", why=f"search_page: the search ended off the site ({url[:80]})")
    if v15(tool):
        # version 15: the site's chrome and a result's title are no error statement (``error_line15``)
        err, err_ev = error_line15(tool, read, value)
        if err_ev:
            out["error_check"] = err_ev
    else:
        err = error_statement(read.get("text"))
    if err:
        return dict(out, status="failed", why=f"search_page: the page reports an error ({err!r})")
    if v7(tool):
        # version 7: a feed is never read as a result list
        fd = feed_page(read)
        if fd:
            return dict(out, status="failed", why=f"search_page: the result page is a feed, not an HTML result "
                                                  f"list ({fd})")
    region = tool.get("region") or {}
    if region.get("cards") and read_cards(read, region) is None:
        return dict(out, status="failed", why="membership: the result containers could not be read")
    # the cards are read against the tool's RESULT SCOPE (``result_scope``); a card whose link leaves it is
    # a typed ``promo`` item, reported but never a result; an unknown scope or a page mostly outside it is uncertain
    rr = region_read(read, region, exclude, scope=result_scope(tool), value=value)
    items, unread = rr["items"], rr["incomplete"]
    # a link back to the site's own search (did-you-mean, suggestion, related-search lists) is never a result
    eps = search_endpoints(tool_templates(tool) + list(read.get("search_urls") or []), url, value)
    if eps and items:
        sl = [it for it in items if search_link(it.get("url"), eps)]
        if sl:
            out["search_links"] = [{"title": str(it.get("title") or "")[:120], "url": it.get("url")} for it in sl[:12]]
            items = [it for it in items if not search_link(it.get("url"), eps)]
    out["n_items"] = len(items)
    out["items"] = [{"title": it["title"][:160], "url": it["url"]} for it in items[:12]]
    if unread:
        out["unread"] = unread[:12]
    if rr.get("linkless"):
        # linkless cards of a tool whose results have links - section / teaser headings, never rows
        out["linkless_cards"] = rr["linkless"][:6]
    if rr.get("promo"):
        out["promo"] = [{"title": str(it.get("title") or "")[:160], "url": it.get("promo_url"), "host": it.get("host"),
                         "why": it.get("promo_why")} for it in rr["promo"][:12]]
    empty = ""
    if site_page and tool.get("empty_page"):
        from anyuse.execution import empty_page as EP
        ev = EP.match_text(tool["empty_page"], read, value)
        empty = ev.line if ev.state == EP.EMPTY else ""
    naming = [it for it in items if name_share(it["title"] + " " + it.get("ctx", ""), value) >= MIN_NAME_SHARE]
    refused = False
    if v15(tool) and not empty and not naming:
        # version 15: the check model in an open scope and the statement rule of every script, bound to the query,
        # before the phrase list (``empty_statement15``)
        empty, ev15 = empty_statement15(read, value)
        refused = bool(ev15.get("refused"))
        if ev15:
            out["empty_check"] = ev15
    if not refused:
        empty = empty or no_results_statement(read.get("main_text") or read.get("text"))
    if empty and not naming:
        return dict(out, status="no_results", empty_statement=empty,
                    why=f"empty_state: the page says {empty!r} and no region item names the value")
    if rr.get("scope_why"):
        # unknown is uncertain: the page's cards mostly link outside the recorded scope (or the tool declares none)
        return dict(out, status="incomplete", why=f"scope: {rr['scope_why']}")
    if not items and v7(tool):
        # version 7: the search went straight to one product's own page - the run checks it is the input's
        landed = landed_product(tool, read, value)
        if landed:
            return dict(out, status="landed", why=f"query: the search landed on a product page ({landed})")
    if not items:
        return dict(out, status="failed", why="membership: no item of the compiled region on the result page "
                                              "and no 'no results' statement")
    if tool.get("workflow") == "site_search":
        ok, why = list_ok(items, value)
        if not ok and "(sparse)" in why and items and SPARSE_EVIDENCE_RULE:
            # a SPARSE list not every item of which names the value is still the query's list when
            # the page itself STATES its result count as the rows read AND the site classifies the rows under the value
            # (an applied filter naming it), or the page names the value (its statement, a heading, the breadcrumb) and
            # at least one item names it as well. The statement and the headings are read from the visible text, never
            # from the card map (``attribute_verify.sparse_evidence``); a page that only echoes the query over items
            # none of which names it stays refused (a fuzzy match, a "did you mean" page); unknown stays a refusal
            ev = sparse_evidence_of(read, value, len(items))
            out["sparse_evidence"] = ev
            named = any(name_share(str(it.get("title") or "") + " " + str(it.get("ctx") or ""), value) >= MIN_NAME_SHARE
                        or W.names_all_compound(str(it.get("title") or "") + " " + str(it.get("ctx") or ""), value)
                        for it in items)
            srcs = set(ev.get("sources") or [])
            if ev.get("count") == "verified" and ("filter" in srcs or (srcs and named)):
                ok = True
                why = (f"{why}; the page states {len(items)} result(s) and " +
                       ("an applied filter names the value" if "filter" in srcs else
                        f"its {', '.join(sorted(srcs))} name the value, as does an item"))
                ev["accepted"] = True
        if not ok:
            return dict(out, status="failed", why=f"query: the list does not belong to the query ({why})")
        k = int(tool.get("top_k") or TOP_K)
        results = [dict({"title": it["title"][:200], "url": it["url"]},
                        **({"link": it["link"]} if it.get("link") not in (None, "site") else {})) for it in items[:k]]
        miss = unread_before(items, unread, k)
        if miss:
            # web-watch: a result the compiled structure cannot read sits among the top k - the list is
            # INCOMPLETE and says so, never a list that silently skipped it
            return dict(out, status="incomplete", results=results, missing=miss,
                        why=f"incomplete: {len(miss)} result(s) among the top {k} could not be read ({miss[0]['why']})")
        return dict(out, status="list", why=f"query: {why}", results=results)
    need: List[Dict[str, Any]] = []
    absent: List[str] = []
    if v9(tool):
        # version 9: version 7's rule among the results carrying the input's distinguishing words
        # (never a sibling variant that lacks the asked-for colour); the asked-for variant not listed: INCOMPLETE
        got9 = closest_choice9([it for it in items if it.get("url")], value)
        if got9["not_listed"]:
            return dict(out, status="incomplete", not_listed=got9["not_listed"], why=f"incomplete: {got9['why']}")
        el, why, need, absent = got9["el"], got9["why"], got9["need"], got9["absent"]
        if got9["tied"]:
            return dict(out, status="incomplete", ambiguous=[{"title": it["title"][:160], "url": it["url"]}
                                                             for it in got9["tied"][:5]], why=f"incomplete: {why}")
        if got9["variants"]:
            out["variants_same_title"] = got9["variants"]
    elif v7(tool):
        # version 7: the closest of several results naming every word; a tie is ambiguous - INCOMPLETE, never a guess
        got = closest_choice([it for it in items if it.get("url")], value)
        el, why, tied, variants = got["el"], got["why"], got["tied"], got["variants"]
        if tied:
            return dict(out, status="incomplete", ambiguous=[{"title": it["title"][:160], "url": it["url"]}
                                                             for it in tied[:5]], why=f"incomplete: {why}")
        if variants:
            # the first of several same-title results, the others listed (never opened)
            out["variants_same_title"] = variants
    else:
        el, why = best_match([it for it in items if it.get("url")], value)
    if el is None:
        nolink = [it for it in items if not it.get("url") and name_share(it.get("title"), value) >= MIN_NAME_SHARE]
        if nolink:
            # web-watch: the matching result has no same-site link - it is never opened, never guessed
            return dict(out, status="failed", why=f"query: {why}; the result naming the input has no same-site "
                                                  f"link ({nolink[0].get('link')})")
        named = [u for u in unread if name_share(u.get("title"), value) >= MIN_NAME_SHARE]
        if named:
            return dict(out, status="incomplete", missing=named[:3],
                        why=f"incomplete: {why}; a result the compiled structure cannot read names it ({named[0]['why']})")
        return dict(out, status="failed", why=f"query: {why}")
    pick = {"title": el["title"][:200], "url": el["url"], "i": el.get("i")}
    if v9(tool) and need:
        # version 9: the marks the chosen result's title does not carry - its product page must
        pick["need"] = need
        if v15(tool):
            # version 15: the product page's form state may carry them (``check_marks``)
            pick["evidence_rule"] = "15"
    if v9(tool) and absent:
        # version 9: the producer / brand words the chosen result's title omits - its product page must name them
        pick["absent"] = absent
    return dict(out, status="pick", pick=pick, el=el.get("el"), why=f"query: {why}")
