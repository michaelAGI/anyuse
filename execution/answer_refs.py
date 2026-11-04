"""ONE mechanism for every consumer of an agent's answer: the references an answer makes to the page it
read are resolved against THAT page, so no consumer ever sees a string the observation had cut.

The agent's observation shows a link's URL cut at 70 characters and its text at 80
(``harness_types.Element.line``). The agent answered the cut strings; the compile refused 4 of the 5 runs that finished
with results ("no reported result is a link of the result page"), because only the fallback resolved cut references
(``web_tool_call.resolve_on_page``). Two parts:

* **The cause.** Every link carries its element number (``[N]``) and a cut string is shown with :data:`CUT_MARK`; the
  policy is told to answer result links by their number (``{"results": [{"element": N}, ...]}``), so it never has to
  quote a URL. :func:`shown` is the one renderer of a cut string.
* **The resolver** (:func:`resolve_answer`), used by the harness on every ``done`` (so the compile, the proof, the
  agent's report and the fallback all read resolved answers) and by the fallback's own read of its final page:

  - ``{"element": N}`` (also ``ref`` / ``el``, or a bare number): that element's full URL and text;
  - a URL that IS a link of the page: kept (a cut title is completed from that link's text);
  - a cut URL (shown with the mark, or as long as the shown prefix, or simply a prefix of >= :data:`MIN_PREFIX`
    characters) that starts exactly ONE distinct link: that link; several: narrowed by the answered title (the link's
    text equals it or starts with it); still several or none: **uncertain** - the row is kept as answered and the
    answer reports it (``uncertain``); a consumer refuses it, nothing guesses;
  - a product answer's cut URL that starts the loaded page's URL: the loaded URL;
  - with the page's DOM, the answered links of ONE result card (its title and an ad, a variant or a shop link inside it)
    are one result (:func:`merge_same_record`): the card's own result is kept, at the first of their places.

Pure: no page access, no network; the links come from the observation (:func:`links_from_observation`) or from a DOM
(:func:`links_from_dom`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: what the observation shows of a link's URL and of an element's text (the rest is cut)
URL_SHOWN = 70
TEXT_SHOWN = 80
#: the mark a cut string ends with in the observation
CUT_MARK = "…"
#: a reference shorter than this never resolves by prefix (too many links share a short start)
MIN_PREFIX = 20
#: the keys an answer row may name an element with
REF_KEYS = ("element", "ref", "el", "index")


@dataclass
class Link:
    index: Optional[int]
    href: str
    text: str
    #: the link's title when it wraps its card (``demo_cards.link_name``: its heading / name element)
    name: str = ""
    #: the browser's computed accessible name (``ax_tree.enrich``) - the LAST title candidate, taken only
    #: when the link's text, its card's name and a same-target link's text give nothing but an empty text or a label
    ax: str = ""


def shown(s: Any, n: int) -> str:
    """How the observation shows ``s``: whole when it fits, else its first ``n`` characters and :data:`CUT_MARK`."""
    s = str(s or "")
    return s if len(s) <= n else s[:n] + CUT_MARK


def _fold(s: Any) -> str:
    return " ".join(str(s or "").split())


def _uncut(s: Any) -> Tuple[str, bool]:
    """(the string without a trailing cut mark, whether it carried one)."""
    s = _fold(s)
    for mark in (CUT_MARK, "...", "…"):
        if s.endswith(mark):
            return s[: -len(mark)].rstrip(), True
    return s, False


def _norm(u: str) -> str:
    from anyuse.execution import demo_compile as C
    try:
        return C._norm(str(u or ""))
    except Exception:                                  # noqa: BLE001 - an odd string normalises to itself
        return str(u or "")


def links_from_observation(obs: Any) -> List[Link]:
    """The links an observation showed: its elements with a URL (``index`` = the number the policy saw)."""
    out: List[Link] = []
    page = getattr(obs, "page", None) or {}
    names: Dict[str, str] = {}
    if isinstance(page, dict) and page.get("dom"):
        from anyuse.execution import demo_cards as K
        names = K.link_names(str(page.get("dom")), str(page.get("url") or getattr(obs, "url", "") or ""),
                             page.get("elements") or [], card_headings=True, paragraphs=True)
    mapping = getattr(obs, "mapping", None) or {}
    # the browser's computed accessible name of each link (``ax_tree.enrich``), a title candidate where the
    # link's own text is empty or a label ("Weiterlesen" named by aria-labelledby, an icon link named by its title)
    ax_names = {str(e.get("i")): " ".join(str(e.get("ax_name") or "").split())
                for e in (page.get("elements") or [] if isinstance(page, dict) else []) if isinstance(e, dict)
                and e.get("ax_name") and e.get("ax_role") == "link" and not e.get("ax_ignored")}
    for e in getattr(obs, "elements", None) or []:
        href = str(getattr(e, "href", "") or "")
        if href:
            i = (mapping.get(int(getattr(e, "index", -1))) or {}).get("i") if isinstance(mapping, dict) else None
            text = _fold(getattr(e, "text", ""))
            name = names.get(str(i), "") if i is not None else ""
            ax = ax_names.get(str(i), "") if i is not None else ""
            out.append(Link(int(getattr(e, "index", -1)), href, text, name,
                            ax if ax and ax != text and not is_label(ax) else ""))
    return out


def links_from_dom(dom: str, page_url: str) -> List[Link]:
    """The links of a DOM (no element numbers), read with the extractor side's parser (``demo_cards``)."""
    from anyuse.execution import demo_cards as K
    try:
        root, base = K.parse(dom or "")
        return [Link(None, href, _fold(K.text_of(n, 400)), K.link_name(n, paragraphs=True) or
                     K.card_heading(n, page_url, base))
                for n, href in K.anchors(root, page_url, base) if href]
    except Exception:                                  # noqa: BLE001 - an unparsable page resolves nothing
        return []


def _ref_of(row: Any) -> Optional[int]:
    if isinstance(row, bool):
        return None
    if isinstance(row, int):
        return row
    if isinstance(row, str) and row.strip().lstrip("[").rstrip("]").isdigit():
        return int(row.strip().lstrip("[").rstrip("]"))
    if isinstance(row, dict):
        for k in REF_KEYS:
            v = row.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
                return int(v)
    return None


def _complete_title(title: str, texts: Sequence[str]) -> str:
    """A cut title completed from the link's own text (the shortest text that starts with it)."""
    t, cut = _uncut(title)
    if not t:
        return texts[0] if texts else ""
    if t in texts:
        return t
    longer = [x for x in texts if x.startswith(t) and x != t]
    if longer and (cut or len(t) >= TEXT_SHOWN - 1 or len(t) >= 10):
        return min(longer, key=len)
    return t


#: link labels that are never a title, in a few languages; compared after folding case, punctuation and arrows /
#: ellipses away. The last rung of :func:`_labels`: the page's own repetition decides first
LABEL_WORDS = frozenset({
    "weiterlesen", "weiter lesen", "mehr", "mehr lesen", "mehr erfahren", "mehr dazu", "zum artikel", "zum beitrag",
    "details", "mehr details", "jetzt lesen", "read more", "more", "learn more", "see more", "view more", "view",
    "continue reading", "read", "full story", "find out more", "lire la suite", "en savoir plus", "plus", "voir plus",
    "lire", "leggi tutto", "leggi di più", "leggi di piu", "continua a leggere", "scopri di più", "scopri di piu",
    "leer más", "leer mas", "ver más", "ver mas", "seguir leyendo", "lees meer", "meer", "verder lezen", "lees verder",
    "läs mer", "las mer", "læs mere", "laes mere", "les mer", "lue lisää", "czytaj więcej", "czytaj dalej", "więcej",
    "zobacz więcej", "zur kategorie", "alle anzeigen", "show all", "see all", "tout voir", "vedi tutti", "ver todo"})


def _fold_label(t: str) -> str:
    import re
    return re.sub(r"[\s.:;,!?…»«›‹→←>\-–—]+", " ", str(t or "").lower()).strip()


def is_label(text: str) -> bool:
    """A link text that never names a result (a "read more" / "show all" label of :data:`LABEL_WORDS`)."""
    return _fold_label(text) in LABEL_WORDS


#: a page shows its label slot when at least this many targets are each linked under two or more texts (a card's
#: heading and its second link): a label there repeats, so a text that does not is no label
LABEL_SLOT_CARDS = 2


def _labels(links: Sequence[Link]) -> set:
    """The page's link labels, never titles (the verifier's rule, attribute_verify section 2), its own evidence first:

    1. a text the page uses for two or more DIFFERENT targets ("Weiterlesen", "Leia mais", in any language);
    2. a text it uses for one target only, as that target's FIRST link (the slot where a card shows its heading), on
       a page whose cards show their label slot (at least :data:`LABEL_SLOT_CARDS` other targets linked under two or
       more texts each), is no label: a label would repeat on those cards, and this text does not (a heading that
       happens to be a lexicon word, "Meer"). A lexicon text after its card's first link stays a label (one card's
       "Weiterlesen" below its heading);
    3. the label lexicon (:func:`is_label`), only where the page gives no such comparison (a single result card)."""
    by: Dict[str, set] = {}
    texts_of: Dict[str, set] = {}
    first: Dict[str, str] = {}
    for l in sorted(links, key=lambda x: (x.index is None, x.index if x.index is not None else 0)):
        if l.text:
            by.setdefault(l.text.lower(), set()).add(_norm(l.href))
            texts_of.setdefault(_norm(l.href), set()).add(l.text.lower())
            first.setdefault(_norm(l.href), l.text.lower())
    out = {t for t, hs in by.items() if len(hs) >= 2}
    slots = [h for h, ts in texts_of.items() if len(ts) >= 2]
    for t, hs in by.items():
        if t in out or not is_label(t):
            continue
        if all(first.get(h) == t for h in hs) and sum(1 for h in slots if h not in hs) >= LABEL_SLOT_CARDS:
            continue
        out.add(t)
    return out


def _title_for(href: str, text: str, links: Sequence[Link], labels: set) -> str:
    """The link's own text, unless it is a label (or empty): then the longest non-label text of a link to the SAME
    target (the card's heading link, where an answer named a card's "Weiterlesen" link)."""
    if text and text.lower() not in labels:
        return text
    nh = _norm(href)
    alts = [l.text for l in links if l.text and _norm(l.href) == nh and l.text.lower() not in labels]
    return max(alts, key=len) if alts else text


def resolve_row(row: Any, links: Sequence[Link]) -> Tuple[Dict[str, Any], str, str]:
    """``(row, how, why)``: ``how`` in ``element`` | ``exact`` | ``prefix`` | ``prefix_title`` | ``title`` |
    ``unmatched`` | ``uncertain`` (``why`` says what is open)."""
    ref = _ref_of(row)
    by_index = {l.index: l for l in links if l.index is not None}
    if ref is not None:
        link = by_index.get(ref)
        if link is None:
            base = dict(row) if isinstance(row, dict) else {}
            return base, "uncertain", f"element {ref} is not a link of the page the answer was read from"
        out = {k: v for k, v in (row.items() if isinstance(row, dict) else ()) if k not in REF_KEYS}
        labels = _labels(links)
        title = link.name or _title_for(link.href, link.text, links, labels)
        if link.ax and (not title or title.lower() in labels):
            title = link.ax               # the computed accessible name, when nothing else names it
        out.update(title=title, url=link.href)
        if link.name:
            out["_named"] = True          # the card's heading names it: never replaced as a label
        return out, "element", ""
    if not isinstance(row, dict):
        return {}, "unmatched", "not a result row"
    url_raw = str(row.get("url") or row.get("href") or "")
    url, url_cut = _uncut(url_raw)
    title = str(row.get("title") or row.get("name") or "")
    nu = _norm(url)
    if url:
        exact = [l for l in links if _norm(l.href) == nu]
        if exact and not url_cut:
            texts = [l.text for l in exact if l.text]
            return dict(row, url=exact[0].href, title=_complete_title(title, texts) if title else (texts[0] if texts
                                                                                                  else title)), \
                "exact", ""
        if len(url) >= MIN_PREFIX or url_cut:
            cands = [l for l in links if _norm(l.href).startswith(nu) or l.href.startswith(url)]
            hrefs = {_norm(l.href) for l in cands}
            how = "prefix"
            if len(hrefs) > 1 and title:
                t, _ = _uncut(title)
                narrowed = [l for l in cands if l.text and (l.text == t or l.text.startswith(t))]
                if len({_norm(l.href) for l in narrowed}) == 1:
                    cands, hrefs, how = narrowed, {_norm(narrowed[0].href)}, "prefix_title"
            if len(hrefs) == 1:
                texts = [l.text for l in cands if l.text]
                return dict(row, url=cands[0].href, title=_complete_title(title, texts) if title else
                            (texts[0] if texts else "")), how, ""
            if len(hrefs) > 1:
                return dict(row), "uncertain", (f"the cut reference {url[:80]!r} starts {len(hrefs)} different links "
                                                f"of the page (the title does not single one out)")
        if exact:
            return dict(row, url=exact[0].href), "exact", ""
        return dict(row), "unmatched", f"{url[:80]!r} is not a link of the page"
    if title:
        t, _ = _uncut(title)
        same = [l for l in links if l.text and l.text == t]
        if len({_norm(l.href) for l in same}) == 1:
            return dict(row, url=same[0].href, title=t), "title", ""
    return dict(row), "unmatched", "no URL and no single link with that title"


#: a target linked this often on one page with one text is a category / tag / menu entry, not a result (FWF-9)
CATEGORY_REPEATS = 3
#: the answered links of one result card are one result (:func:`merge_same_record`; a switch for a measurement's
#: before / after reading)
SAME_RECORD = True
#: how many rows the report names that were read as another row's card
SAME_RECORD_SHOWN = 10
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _kept_of(members: Sequence[int], anchors: Sequence[Any], primary: Sequence[bool], record: Any) -> int:
    """Which row of one card stays: the row at the card's primary place (where the other cards' answered links sit),
    else the one whose link is in a heading of the card, else the URL the card links most often, else the first."""
    lead = [i for i in members if primary[i]]
    if len(lead) == 1:
        return lead[0]

    def in_heading(i: int) -> bool:
        cur = anchors[i]
        while cur is not None and cur is not record:
            if cur.tag in _HEADING_TAGS:
                return True
            cur = cur.parent
        return False
    heads = [i for i in members if in_heading(i)]
    if len(heads) == 1:
        return heads[0]
    links: Dict[str, int] = {}
    for n in record.walk() if record is not None else []:
        if n.tag == "a" and n.href:
            links[n.href] = links.get(n.href, 0) + 1
    count = {i: links.get(anchors[i].href, 0) for i in members}
    most = max(count.values())
    best = [i for i in members if count[i] == most]
    return best[0] if len(best) == 1 else members[0]


def merge_same_record(rows: Sequence[Any], dom: str, page_url: str) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """``(rows, dropped)``: answered rows whose links sit in ONE result card of the page (``dom``) read as that card's
    one result. Each row's link is the page's link to its URL in a heading, else its first (outside the page's
    furniture first); the cards are the page's repeated records (``result_contract.record_units``), and the row kept
    is :func:`_kept_of`'s, at the card's first answer position. ``dropped``: ``{url, kept}`` per row left out. Rows
    are unchanged without two rows that are links of the page, or when the page shows no repeated record."""
    import urllib.parse
    out = list(rows)
    if not SAME_RECORD or not dom:
        return out, []
    from anyuse.execution import html_extract as H
    from anyuse.execution import result_contract as RC
    try:
        root = H.parse(dom)
    except ValueError:
        return out, []
    base = str(page_url or "")
    tag = next((n for n in root.walk() if n.tag == "base" and n.attrs.get("href")), None)
    if tag is not None:
        base = urllib.parse.urljoin(base, tag.attrs["href"])
    by_url: Dict[str, List[Any]] = {}
    for n in root.walk():
        if n.tag == "a" and n.href and not n.href.startswith(("javascript:", "#", "mailto:")):
            by_url.setdefault(_norm(urllib.parse.urljoin(base, n.href)), []).append(n)
    total = len(root.text())

    def in_heading(a: Any) -> bool:
        cur = a
        while cur is not None and cur.tag:
            if cur.tag in _HEADING_TAGS:
                return True
            cur = cur.parent
        return False
    anchors: List[Any] = []
    for r in out:
        cands = (by_url.get(_norm(str(r.get("url") or ""))) or []) if isinstance(r, dict) else []
        cands = [a for a in cands if not H.in_chrome(a, total)] or cands
        # a result's own title link where the page has one, not a mention of it in another card
        anchors.append(next((a for a in cands if in_heading(a)), cands[0] if cands else None))
    placed = [i for i, a in enumerate(anchors) if a is not None]
    if len(placed) < 2:
        return out, []
    units = RC.record_units([anchors[i] for i in placed])
    groups: Dict[int, List[int]] = {}
    for pos, i in enumerate(placed):
        if units.ids[pos] is not None:
            groups.setdefault(units.ids[pos], []).append(pos)
    keep_at: Dict[int, int] = {}
    skip: set = set()
    left_out: Dict[int, int] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        kept = _kept_of(members, [anchors[i] for i in placed], units.primary, units.records[members[0]])
        keep_at[placed[members[0]]] = placed[kept]
        skip.update(placed[pos] for pos in members[1:])
        for pos in members:
            if pos != kept:
                left_out[placed[pos]] = placed[kept]
    if not left_out:
        return out, []
    merged: List[Any] = []
    for i, r in enumerate(out):
        if i in keep_at:
            merged.append(out[keep_at[i]])
        elif i not in skip:
            merged.append(r)
    dropped = [{"url": str(out[i].get("url") or ""), "kept": str(out[k].get("url") or "")}
               for i, k in sorted(left_out.items())]
    return merged, dropped


def resolve_answer(workflow: str, result: Any, links: Sequence[Link], page_url: str = "",
                   occurrences: Optional[Dict[str, int]] = None, dom: str = "") -> Tuple[Any, Dict[str, Any]]:
    """``(resolved result, report)``. The report: counts per ``how`` and ``uncertain`` (the open rows, with why);
    a consumer must not accept an answer whose report lists an uncertain row. Results that are not rows (a summary,
    ``no_results``) pass through unchanged. With the page's ``dom``, rows that are links of one result card are that
    card's one result (:func:`merge_same_record`; the report's ``same_record`` and ``same_record_dropped``)."""
    rep: Dict[str, Any] = {"uncertain": [], "unmatched": []}
    if isinstance(result, list):
        result = {"results": result}
    if not isinstance(result, dict):
        return result, rep
    rows = result.get("results") if isinstance(result.get("results"), list) else \
        (result.get("rows") if isinstance(result.get("rows"), list) else None)
    if rows is not None:
        key = "results" if isinstance(result.get("results"), list) else "rows"
        out = []
        seen = set()
        labels = _labels(links)
        for i, row in enumerate(rows):
            r, how, why = resolve_row(row, links)
            if how in ("uncertain", "unmatched"):
                r.pop("_named", None)
                rep[how].append({"row": i, "why": why})
            else:
                rep[how] = int(rep.get(how, 0)) + 1
                named = bool(r.pop("_named", False))
                if r.get("url") and not named and str(r.get("title") or "").lower() in labels:
                    r["title"] = _title_for(str(r["url"]), str(r.get("title") or ""), links, labels)
                nu = _norm(str(r.get("url") or ""))
                if nu and nu in seen:
                    # a result is listed once: a second row to the same target (a card's image, heading and "more"
                    # links) is dropped, never a second result
                    rep["duplicate"] = int(rep.get("duplicate", 0)) + 1
                    continue
                if nu:
                    seen.add(nu)
            out.append(r)
        if occurrences:
            # (each result card also links its category): a target the page links
            # CATEGORY_REPEATS or more times is a category, a tag or a menu entry, never a result - dropped when the
            # answer keeps at least two rows that are not
            rep_of = {i: int(occurrences.get(_norm(str(r.get("url") or "")), 0)) for i, r in enumerate(out)}
            cat = [i for i, n in rep_of.items() if n >= CATEGORY_REPEATS]
            if cat and len(out) - len(cat) >= 2:
                out = [r for i, r in enumerate(out) if i not in cat]
                rep["category"] = len(cat)
        # (a category linked once, /news/category/<id>, among the /news/<id>/<slug> articles): a row whose URL
        # SHAPE is not the answer's majority is no result, whatever its
        # repeat count (``url_identity.off_shape``; the same rule the region reader applies) - dropped when at least two
        # rows remain
        from anyuse.execution import url_identity as UI
        off = set(UI.off_shape([str(r.get("url") or "") if isinstance(r, dict) else "" for r in out],
                               counts=UI.link_counts(l.href for l in links)))
        if off:
            out = [r for i, r in enumerate(out) if i not in off]
            rep["off_shape"] = len(off)
        if dom:
            out, dropped = merge_same_record(out, dom, page_url)
            if dropped:
                rep["same_record"] = len(dropped)
                rep["same_record_dropped"] = dropped[:SAME_RECORD_SHOWN]
        return dict(result, **{key: out}), rep
    if workflow == "product_lookup" or "url" in result:
        url_raw = str(result.get("url") or "")
        url, cut = _uncut(url_raw)
        if url:
            pu = str(page_url or "").split("#", 1)[0]
            if pu and _norm(pu) != _norm(url) and (cut or len(url) >= MIN_PREFIX) and \
                    (_norm(pu).startswith(_norm(url)) or pu.startswith(url)):
                rep["prefix"] = 1
                return dict(result, url=pu), rep
            if cut:
                r, how, why = resolve_row({"url": url_raw, "title": result.get("name") or ""}, links)
                if how in ("prefix", "prefix_title", "exact"):
                    rep[how] = 1
                    return dict(result, url=r["url"]), rep
                rep["uncertain"].append({"row": 0, "why": why or "a cut URL that is no link of the page"})
        elif page_url:
            return dict(result, url=str(page_url).split("#", 1)[0]), dict(rep, page_url=1)
        return result, rep
    return result, rep


def _repeats(pairs: Sequence[Tuple[str, str]]) -> Dict[str, int]:
    """target -> how often the page links it with ONE and the same text (the most repeated text): a category link
    repeats with its name in every card; a result's image, title and "more" links have different texts."""
    cnt: Dict[Tuple[str, str], int] = {}
    for href, text in pairs:
        t = _fold(text).lower()
        if href and t:
            k = (_norm(href), t)
            cnt[k] = cnt.get(k, 0) + 1
    out: Dict[str, int] = {}
    for (h, _t), n in cnt.items():
        out[h] = max(out.get(h, 0), n)
    return out


def occurrences_of(page: Any) -> Dict[str, int]:
    """The repeats of each target on a page (the READ record's links; the observation lists each (text, URL) pair
    once): the structure FWF-9 reads."""
    els = (page or {}).get("elements") or [] if isinstance(page, dict) else []
    return _repeats([(str(e.get("href") or ""), str(e.get("text") or "")) for e in els
                     if isinstance(e, dict) and e.get("tag") == "a" and e.get("href")])


def resolve_on_observation(workflow: str, result: Any, obs: Any) -> Tuple[Any, Dict[str, Any]]:
    """The harness's call on ``done``: the observation the policy answered from."""
    page = getattr(obs, "page", None)
    dom = str(page.get("dom") or "") if isinstance(page, dict) else ""
    return resolve_answer(workflow, result, links_from_observation(obs), str(getattr(obs, "url", "") or ""),
                          occurrences_of(page), dom=dom)


def resolve_on_dom(workflow: str, result: Any, dom: str, page_url: str) -> Tuple[Any, Dict[str, Any]]:
    """A consumer that holds the final page's DOM (the fallback)."""
    links = links_from_dom(dom, page_url)
    return resolve_answer(workflow, result, links, page_url, _repeats([(l.href, l.text) for l in links]), dom=dom)


def refusal(rep: Optional[Dict[str, Any]]) -> str:
    """'' or why an answer with this resolution report may not be accepted or compiled (an uncertain reference)."""
    unc = list((rep or {}).get("uncertain") or [])
    if not unc:
        return ""
    return (f"{len(unc)} answered reference(s) stay uncertain ({unc[0].get('why')}); a cut or ambiguous reference is "
            f"never guessed")[:400]


__all__ = ["URL_SHOWN", "TEXT_SHOWN", "CUT_MARK", "Link", "shown", "links_from_observation", "links_from_dom",
           "resolve_row", "resolve_answer", "resolve_on_observation", "resolve_on_dom", "refusal", "SAME_RECORD",
           "merge_same_record"]
