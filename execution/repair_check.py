"""The repair ladder's INDEPENDENT CHECKER: asked by the acceptance gate only
when a repaired answer differs from the last known good answer for the same input (or none is known). It never reads
the tool's compiled search, structure, refinement or pager: it renders the site's own search through the ORACLE's path
(``demo_oracle``: the first visible search box, the page's generic content links, its own control finder and next-page
detection, the item page as shown) on fresh pages and checks the answer against that render deterministically:

* a product: the answered URL's own page shows the answered name (>= 75 % of its words) and its price, and the oracle's
  search lists that product (its URL, or a link naming its title);
* a site search: every answered result is one of the oracle's content links, in the same order; EVERY answered result
  (its title or its card) names a word of the input; and the answer covers at least half of the render's links naming
  the input (up to the tool's top k) - a list that over-reads (a promo block) or under-reads (one item) is not agreed;
* a filtered / sorted list: ``demo_oracle.check_refined`` on the oracle's refined render (by the applied LABELS);
* a paged list: ``demo_oracle.check_paged`` on the oracle's own pages;
* a visit table: ``demo_oracle.check_items`` on the oracle's render of each row's page.

``{"verdict": "agrees" | "disagrees" | "undeterminable", "why", ...}``; a block or a failed oracle search is
``undeterminable`` - never an agreement. Its traffic is a few GETs of the site's own pages per question.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from anyuse.execution import demo_compile as C
from anyuse.execution import demo_oracle as O
from anyuse.execution import web_arch as W


#: the checker's availability check is OFF by default (``OracleChecker(availability=True)``)
AVAILABILITY_CHECK_BY_DEFAULT = False
#: the checker's rate-limit reading - ON by default (with pacing;
#: ``OracleChecker(rate_limit=False)`` turns it off)
RATE_LIMIT_CHECK_BY_DEFAULT = True
#: the not-now classes an attached delivery time contradicts (an on-order or a pre-order product has delivery times)
_GONE = ("out_of_stock",)


#: the availability check's second rule set (``availability_contradiction(v2=True)``): an attached line saying the
#: goods are ON THEIR WAY to the shop (on order, inbound) - a not-now class version 9's vocabulary does not read
#: ("Ware befindet sich in Zulauf" was accepted as in stock)
_INBOUND_V2 = re.compile(r"\b(?:(?:ware |artikel )?(?:befindet sich |ist )?(?:in|im) zulauf|wird nachgeliefert|"
                         r"on order|awaiting (?:stock|delivery)|en cours d.approvisionnement|en reapprovisionnement|"
                         r"in bestelling|wordt nabesteld|w drodze do magazynu|oczekuje na dostawe|in arrivo|"
                         r"in riassortimento|en camino al almacen|pendiente de recibir)\b")


def _attached_lines(lines: Sequence[str], price: Optional[float]) -> List[str]:
    """The short lines ATTACHED to the product (within ``ATTACHED_LINES`` of its price or of its add-to-cart control;
    ``demo_compile._attached_class10``'s anchors)."""
    out: List[str] = []
    for i, ln in enumerate(lines):
        near = (price is not None and C.visible_price(ln, price)) or \
            (W._BUY_CONTROL.search(C.fold9(ln)) and len(C.fold9(ln)) <= C.LEGEND_LINE_CHARS)
        if not near:
            continue
        for j in range(max(0, i - C.ATTACHED_LINES), min(len(lines), i + C.ATTACHED_LINES + 1)):
            t = str(lines[j] or "").strip()
            if j != i and t and len(C.fold9(t)) <= C.LEGEND_LINE_CHARS and t not in out:
                out.append(t)
    return out


def availability_contradiction(text: Any, answered: Any, price: Any = None, v2: bool = False) -> str:
    """``""`` or why the product page CONTRADICTS the answered availability (a checker that never reads it accepts
    a LimitedAvailability next to the buy box's "Online sofort lieferbar", an OutOfStock next to "Lieferzeit: 1 bis 3
    Werktage"). Only the statement ATTACHED to the product counts
    (``demo_compile._attached_class10``: a short line within two lines of the product's price or of its add-to-cart
    control): a stated class that is not the answer's (a few left IS in stock, as in version 11: the readers judged
    both ways), or a delivery time next to an answered out of stock. An answer that is no schema word (a visible
    statement), a page that states nothing attached: no contradiction (the check never guesses)."""
    merge = {"limited": "in_stock"}
    raw = C.availability_class(answered)
    want = merge.get(raw, raw)
    if not want:
        return ""
    lines = str(text or "").split("\n")
    p = C._price_of(price)
    cls, line = C._attached_class10(lines, set(), p)
    if v2:
        # (1) goods on their way to the shop contradict "in stock" / "a few left"; (2) a PLAIN in-stock
        # statement (no few-left wording) contradicts an answered "a few left" (a LimitedAvailability next to "Auf
        # Lager, in 2 - 4 Werktagen", or next to "Online sofort lieferbar")
        if want == "in_stock":
            for t in _attached_lines(lines, p):
                if _INBOUND_V2.search(C.fold9(t)):
                    return f"the product's page states it is on order ({t[:60]!r}), the answer {W.availability_word(answered)}"
        if raw == "limited" and cls == "in_stock":
            return (f"the product's page states plain in stock ({line[:60]!r}), the answer a few left "
                    f"({W.availability_word(answered)})")
    if cls:
        return "" if merge.get(cls, cls) == want else \
            f"the product's page states {cls.replace('_', ' ')} ({line[:60]!r}), the answer {W.availability_word(answered)}"
    if want in _GONE:
        for i, ln in enumerate(lines):
            near = (p is not None and C.visible_price(ln, p)) or (W._BUY_CONTROL.search(C.fold9(ln)) and
                                                                  len(C.fold9(ln)) <= C.LEGEND_LINE_CHARS)
            if not near:
                continue
            for j in range(max(0, i - C.ATTACHED_LINES), min(len(lines), i + C.ATTACHED_LINES + 1)):
                if j != i and C._DELIVERY_V10.search(C.fold9(lines[j])):
                    return f"the product's page shows a delivery time ({lines[j].strip()[:60]!r}), the answer out of stock"
    return ""


def _av_kw(checker: Any) -> Dict[str, Any]:
    """The verifier-independence keywords, passed only when set (``judge``'s callers and wrappers that predate them
    keep working; unset, ``judge`` reads the setting itself)."""
    kw: Dict[str, Any] = {}
    if getattr(checker, "attribute_verify", None) is not None:
        kw["attribute_verify"] = checker.attribute_verify
    if getattr(checker, "resolver", None) is not None:
        kw["resolver"] = checker.resolver
    return kw


class OracleChecker:
    """``checker(tool, value, out)`` for ``repair_ladder.gate``; ``page_factory`` opens a fresh page per render.
    ``availability`` (off by default): a product answer must also not be contradicted by the
    availability its page states next to the product (``availability_contradiction``)."""

    def __init__(self, page_factory: Callable[[], Any], settle: Callable, wait_s: float = O.ORACLE_WAIT_S,
                 availability: bool = AVAILABILITY_CHECK_BY_DEFAULT, rate_limit: bool = RATE_LIMIT_CHECK_BY_DEFAULT,
                 attribute_verify: Optional[bool] = None, resolver: Any = None):
        self.factory, self.settle, self.wait_s = page_factory, settle, wait_s
        #: verifier independence (None: the setting): the product fields read by ``attribute_verify``
        self.attribute_verify, self.resolver = attribute_verify, resolver
        self.availability = availability
        #: off by default: a render that is a RATE-LIMIT page answers ``undeterminable`` with
        #: ``rate_limited`` (the stated wait) - never a disagreement (the paced ladder retries it once)
        self.rate_limit = rate_limit
        self.renders = 0

    def _rate_limited(self, ev: Dict[str, Any]) -> Any:
        if not self.rate_limit:
            return None
        from anyuse.execution import repair_pace as RP
        why, wait = RP.rate_limit_why(ev.get("text"), ev.get("blocked") if ev.get("how") == "blocked" or
                                      ev.get("blocked") else "")
        return {"verdict": "undeterminable", "rate_limited": wait, "why": f"rate limit: {why}"} if why else None

    async def _page(self):
        p = self.factory()
        self.renders += 1
        return await p if inspect.isawaitable(p) else p

    def _templates(self, tool: Dict[str, Any]) -> List[str]:
        # the site's search URL seen on its start page is the oracle's fallback (never the tool's bound template)
        return [t for t in (tool.get("search") or {}).get("fallback_templates") or [] if "{q}" in t]

    async def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        wf = str(tool.get("workflow") or "")
        start = str((tool.get("search") or {}).get("start_url") or tool.get("start_url") or "")
        o = out.get("output") or {}
        if out.get("status") != "ok":
            return {"verdict": "undeterminable", "why": f"no answer to check ({out.get('status')})"}
        if wf == "filtered_search":
            labels = [str(a.get("label") or "") for a in tool.get("applied") or [] if a.get("label")]
            ev = await O.oracle_refined(await self._page(), start, value, labels, self._templates(tool), self.settle,
                                        self.wait_s)
            return O.check_refined(out, ev, tool.get("applied") or [])
        if wf == "paged_search":
            pages = max(r.get("page", 1) for r in o.get("results") or [{"page": 1}])
            evs = await O.oracle_pages(await self._page(), start, value, pages, self._templates(tool), self.settle,
                                       self.wait_s)
            return O.check_paged(out, evs)
        if wf == "visit_results":
            evs = {}
            for r in o.get("items") or []:
                if r.get("url"):
                    evs[str(r["url"])] = await O.oracle_item(await self._page(), str(r["url"]), self.settle)
            return O.check_items(out, evs)
        ev = await O.oracle_search(await self._page(), start, value, self._templates(tool), self.settle, self.wait_s)
        rl = self._rate_limited(ev)
        if rl:
            return rl
        if ev.get("blocked") or ev.get("search_failed"):
            return {"verdict": "undeterminable", "why": "the oracle's search failed or was blocked"}
        items: Dict[str, Any] = {}
        if wf == "product_lookup":
            url = str(o.get("url") or "")
            items[url] = await O.oracle_item(await self._page(), url, self.settle)
            rl = self._rate_limited(items[url] or {})
            if rl:
                return rl
        return judge(tool, value, out, ev, items, availability=self.availability, **_av_kw(self))


def judge(tool: Dict[str, Any], value: str, out: Dict[str, Any], ev: Dict[str, Any],
          items: Dict[str, Dict[str, Any]], availability: bool = AVAILABILITY_CHECK_BY_DEFAULT,
          attribute_verify: Optional[bool] = None, resolver: Any = None) -> Dict[str, Any]:
    """The checker's verdict on a product / a site search answer from the oracle's RENDERS (pure; the same
    judgement on a recorded render - ``RecordedChecker``): ``ev`` the oracle's search render, ``items`` its render of
    the answered product page by URL."""
    wf = str(tool.get("workflow") or "")
    o = out.get("output") or {}
    if out.get("status") != "ok":
        return {"verdict": "undeterminable", "why": f"no answer to check ({out.get('status')})"}
    if ev.get("blocked") or ev.get("search_failed"):
        return {"verdict": "undeterminable", "why": "the oracle's search failed or was blocked"}
    links = ev.get("links") or []
    if wf == "product_lookup":
        url = str(o.get("url") or "")
        listed = any(C._same_result(url, str(x.get("url") or ""), True) for x in links) or \
            any(W.tokens(x.get("text")) and " ".join(W.tokens(x.get("text"))) == " ".join(W.tokens(o.get("name")))
                for x in links)
        item = items.get(url)
        if item is None:
            return {"verdict": "undeterminable", "why": "no render of the item page"}
        if item.get("blocked"):
            return {"verdict": "undeterminable", "why": "the item page was blocked"}
        from anyuse.execution import attribute_verify as AV
        if AV.enabled(attribute_verify):
            # verifier independence: the answered FIELDS are read on the oracle's render by the shared independent
            # verifier (its own reading and phrases; unknown is uncertain), never with the extractor's vocabulary
            # (``name_share9``, ``visible_price``, ``availability_contradiction``). The checker is a repair's gate, a
            # re-proof: without an injected resolver it asks no model, and what only a model could settle disagrees
            av = AV.check("product_lookup", o, AV.oracle_page(item), resolver if resolver is not None else
                          AV.rules_resolver("a repair gate's checker asks no model"))
            ok = listed and av.verdict == AV.ACCEPT
            return {"verdict": "agrees" if ok else "disagrees", "listed": listed, "attribute_verify": av.to_dict(),
                    "why": "the oracle's search lists the product and every answered attribute agrees with an "
                           "independent reading of its page" if ok else
                           ("the oracle's search does not list the product" if not listed else
                            f"attribute verify: {av.why()}")}
        text = " ".join([str(item.get("title") or ""), " ".join(str(h) for h in item.get("h1") or []),
                         str(item.get("text") or "")])
        name_ok = C.name_share9(text, o.get("name")) >= C.MIN_NAME_SHARE
        price_ok = C.visible_price(text, o.get("price"))
        ok = listed and name_ok and price_ok
        if ok and availability:
            # off by default: the availability the page states next to the product
            contra = availability_contradiction(item.get("text"), o.get("availability"), o.get("price"),
                                                v2=availability == 2)
            if contra:
                return {"verdict": "disagrees", "listed": listed, "name_shown": name_ok, "price_shown": price_ok,
                        "availability": contra, "why": f"the oracle's render contradicts the availability: {contra}"}
        return {"verdict": "agrees" if ok else "disagrees", "listed": listed, "name_shown": name_ok,
                "price_shown": price_ok, "why": "the oracle's search lists the product and its page shows its name "
                                                "and price" if ok else "the oracle's render does not support the answer"}
    res = o.get("results") or []
    if not res:
        return {"verdict": "undeterminable", "why": "no results answered"}
    missing, ordered = O._in_order([r["url"] for r in res], [x["url"] for x in links],
                                   [r.get("title") or "" for r in res], [x.get("text") or "" for x in links])
    if missing or not ordered:
        return {"verdict": "disagrees", "missing": missing[:5], "same_order": ordered,
                "why": "answered results the oracle's render does not list" if missing else "another order"}
    card = {C._norm(str(x.get("url") or "")): f"{x.get('text') or ''} {x.get('card') or ''}" for x in links}
    unnamed = [r for r in res if C.name_share(f"{r.get('title') or ''} {card.get(C._norm(str(r.get('url') or '')), '')}",
                                              value) <= 0]
    qn = {C._norm(str(x.get("url") or "")) for x in links if C.name_share(f"{x.get('text') or ''} {x.get('card') or ''}",
                                                                           value) > 0}
    want = min(len(qn), int(tool.get("top_k") or C.TOP_K))
    if unnamed:
        # ``coverage_ok`` - the rest of the list rule holds (the ladder's block-aware list rule waives
        # the naming requirement when no answered result sits in a block on the answer's own page)
        return {"verdict": "disagrees", "unnamed": [str(r.get("title"))[:60] for r in unnamed[:3]],
                "coverage_ok": not (len(res) * 2 < want),
                "why": f"{len(unnamed)} answered result(s) name no word of the input (not the query's results)"}
    if len(res) * 2 < want:
        return {"verdict": "disagrees", "query_links": len(qn),
                "why": f"{len(res)} answered result(s) of the render's {len(qn)} links naming the input"}
    return {"verdict": "agrees", "why": "the oracle's render lists the answered results in order, each naming the "
                                        "input, covering the render's query results"}


class RecordedChecker:
    """The checker on RECORDED oracle renders (offline measurements): ``renders(tool, value)`` -> ``(search
    render, {url: item render})`` or None (``undeterminable``). The same judgement as ``OracleChecker`` (``judge``)."""

    def __init__(self, renders: Callable[[Dict[str, Any], str], Any], availability: bool = AVAILABILITY_CHECK_BY_DEFAULT,
                 attribute_verify: Optional[bool] = None, resolver: Any = None):
        self.renders = renders
        self.availability = availability
        self.attribute_verify, self.resolver = attribute_verify, resolver

    def __call__(self, tool: Dict[str, Any], value: str, out: Dict[str, Any]) -> Dict[str, Any]:
        got = self.renders(tool, value)
        if not got:
            return {"verdict": "undeterminable", "why": "no recorded render of the site's search for this input"}
        ev, items = got
        return judge(tool, value, out, ev or {}, items or {}, availability=self.availability, **_av_kw(self))
