"""The site's own pages as they are now: the evidence a heal, a restore and a demotion use.

A compiled list tool keeps its last good answer (the drift anchor). A list that changes
between reads (newest first, live prices in the names) does not repeat that answer a day
later, although the tool and the site are fine. This module asks the site instead.

:func:`page_truth` (the heal, execution/step_heal.py) decides whether the result pages
read now show this query's list, cheapest evidence first:

1. the list derived from the anchor's and the selection's pages repeats at least
   ``step_repair.MIN_ANCHOR_COVER`` of the last good answer, exactly (no extra request);
2. the page's list passes the list contract and covers its result region;
3. the tool's own fresh response holds the page's values (:func:`provenance`): at least
   ``api_verify.MIN_COVERAGE`` of them is this query's list now, in the API's spelling;
   none of them is another list;
4. the page for a made-up input shows no list there, or is the site's empty page
   (``result_contract.empty_verdict``: the learned empty page, then the configured check
   model in the ``heal`` scope, opened only around this question, then the statement
   rule, the listed-language phrases, as its last step), and the page's list overlaps
   the last good answer (:func:`continuity`);
5. the configured check model (execution/model_check.py, in its ``heal`` scope, opened
   only around this one question): one yes/no question per candidate line, the page's
   list items and other lines of the page. It decides only when it says yes to an item
   of the derived list, which is on the page, and no to every other line.

When a step after the first decides, the page's list becomes the anchor the heal repairs
against (``refreshed``). A swap is still decided by the held-out re-proof, which asks no
model.

:func:`answer_holds_now` (a probe of a request out of service, and a heal that finds
nothing to repair) compares the request's answer with what the site shows now: the
browser replay, else the server-rendered result page, with the proof's list comparison
(``api_verify.compare``). Without either, the answer must change with the input, and the
check model must say its items are results and the other answer's are not.

:func:`site_answers` reads the site's own page once to see whether the site still answers
while the compiled request does not (``api_recompile.gone_check_due``).

Every request goes through the executor or the page reader the caller passes: the
product's executor, with its pre-send policy, robots.txt rules and block detection.
Summaries hold counts and reasons, no page text.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import model_check as MC

#: ``read(value) -> (url, html)`` or None (``step_heal.Reader``)
Reader = Callable[[str], Optional[Tuple[str, str]]]
#: share of the last good answer's items that must have a near item on the page now
CONTINUITY = 0.5
#: token-set Jaccard at which two items are near (a price or a count in a name changed)
NEAR = 0.5
#: page items kept as a refreshed anchor (``step_repair.ANCHOR_ITEMS``)
ANCHOR_ITEMS = 10
#: candidate lines the check model is asked about: list items, then other lines
MODEL_ITEMS = 3
MODEL_OTHERS = 3
#: the one model-check scope this module opens
SCOPE = "heal"
NO_MODEL = "no check model is configured"

RESULT_LINE = MC.Question(
    "result_line",
    "The page below answers a search for '{target}'. Is the line marked >> one of the "
    "results this search found?")


def _norm(value: Any) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(str(value or ""))


def _now() -> str:
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def made_up(values: Sequence[str], seed: str) -> str:
    """The made-up input a page check searches: shaped like ``values``, sharing no word with
    them, the same for the same tool (``empty_page.nonsense_values``)."""
    from anyuse.execution import empty_page as EP
    got = EP.nonsense_values([str(v) for v in values if v], seed=f"fresh-truth|{seed}", n=1)
    return got[0] if got else "zorquavex"


def fresh_anchor(value: str, items: Sequence[Any], source: str = "page") -> Optional[Dict[str, Any]]:
    """A drift anchor from the list a page (or the tool's own response) shows now."""
    texts = [str(x) for x in items if str(x or "").strip()]
    from anyuse.execution.step_repair import MIN_ANCHOR
    if not value or len(texts) < MIN_ANCHOR:
        return None
    return {"value": str(value), "items": texts[:ANCHOR_ITEMS], "count": len(texts),
            "at": _now(), "source": source}


# ---------------------------------------------------------------------------
# the pieces of evidence
# ---------------------------------------------------------------------------

def _tokens(text: Any) -> set:
    return set(_norm(text).split())


def continuity(old_items: Sequence[Any], items: Sequence[Any]) -> float:
    """Share of ``old_items`` with an item in ``items`` whose words overlap at least
    :data:`NEAR` (token-set Jaccard): the same entries with a changed price or count, or
    pushed down by newer ones."""
    old = [_tokens(x) for x in old_items if _tokens(x)]
    new = [_tokens(x) for x in items if _tokens(x)]
    if not old or not new:
        return 0.0
    near = sum(1 for a in old if any(len(a & b) / len(a | b) >= NEAR for b in new))
    return near / len(old)


def provenance(labels: Sequence[Any], data: Any) -> Tuple[Optional[float], List[str]]:
    """Share of the page's ``labels`` (the first :data:`ANCHOR_ITEMS`) found, in order, as
    text leaves of the tool's own fresh response ``data``, and the leaves found. A leaf
    matches a label when they are equal after normalising, or when the leaf (three or more
    characters, a letter among them) stands in the label as whole words and occurs once in
    the response: a value every entry repeats (a currency, a status) names no entry.
    ``None`` when the response has no text leaves."""
    from collections import Counter
    from anyuse.execution.api_compile import contains_words
    from anyuse.execution.api_step import json_leaves
    leaves = [str(v) for _p, v in json_leaves(data) if isinstance(v, str) and v.strip()] \
        if data is not None else []
    if not leaves:
        return None, []
    normed = [_norm(v) for v in leaves]
    seen = Counter(normed)
    wanted = [_norm(x) for x in list(labels)[:ANCHOR_ITEMS] if _norm(x)]
    if not wanted:
        return None, []
    found: List[str] = []
    at = 0
    for want in wanted:
        for j in range(at, len(leaves)):
            n = normed[j]
            if n == want or (len(n) >= 3 and any(ch.isalpha() for ch in n) and seen[n] == 1
                             and contains_words(want, n)):
                found.append(leaves[j])
                at = j + 1
                break
    return len(found) / len(wanted), found


def _parse(html: str, visible_only: bool = True):
    from anyuse.execution.html_extract import parse
    try:
        return parse(html or "", visible_only=visible_only, implied_end_tags=True)
    except ValueError:
        return None


def _lines(html: str) -> List[str]:
    from anyuse.execution.result_contract import visible_lines
    root = _parse(html)
    return [t for t, _b, _h in visible_lines(root)] if root is not None else []


def page_lines(html: str, exclude: Sequence[Any], limit: int = MODEL_OTHERS) -> List[str]:
    """Up to ``limit`` lines of the page that are not part of the list ``exclude``: what the
    check model is asked about beside the list's items."""
    skip = [_norm(x) for x in exclude if _norm(x)]
    out: List[str] = []
    for line in _lines(html):
        n = _norm(line)
        if not n or len(n) < 3 or len(line) > 160 or not any(ch.isalpha() for ch in n):
            continue
        if any(n == s or s in n or n in s for s in skip) or line in out:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def query_bound(read: Reader, selector: str, value: str, *, visible_only: bool = False,
                contract: Optional[Dict[str, Any]] = None
                ) -> Tuple[Optional[bool], str, str, int]:
    """Does the page's list depend on the query? The page for the made-up ``value`` is read
    through ``read``: ``(True, why, "site", calls)`` when ``selector`` reads nothing there,
    or the page is the site's empty page for it; ``(True, why, "model", calls)`` when the
    configured check model reads a line of it as saying the search found nothing (asked in
    the ``heal`` scope, opened around this one question); ``(True, why, "fallback",
    calls)`` when only the statement rule (listed-language phrases) says so; ``(None, why,
    "", calls)`` otherwise. ``calls``: the check model's requests. Never False: a page that
    fills its list region for any input may still list the results."""
    from anyuse.execution import empty_page as EP
    from anyuse.execution import result_contract as RC
    from anyuse.execution.html_extract import extract
    got = read(value)
    if not got:
        return None, "the result page for a made-up input did not answer", "", 0
    url, html = got
    try:
        items = extract(html, selector, limit=RC.HTML_LIMIT, visible_only=visible_only,
                        implied_end_tags=True)
    except ValueError:
        return None, "the result page for a made-up input could not be read", "", 0
    if not items:
        return True, "the list region is empty for a made-up input", "site", 0
    page = EP.Page("html", value, markup=html, url=url, implied_end_tags=True)
    # a context that sends nothing and learns nothing: it only lets the cascade's model
    # step ask, in the heal scope; without a configured model it asks nothing
    ctx = EP.Context(probe=False, learn=False)
    with MC.scope(SCOPE):            # opened for this one question only
        ev = RC.empty_verdict(contract, page, ctx=ctx)
    if ev.state == EP.EMPTY:
        by = {EP.BY_SITE: "site", EP.BY_MODEL: "model"}.get(ev.by, "fallback")
        return True, (f"the page for a made-up input is the site's own empty page beside "
                      f"{len(items)} other entries ({ev.by})"), by, ev.model_calls
    return None, (f"the page for a made-up input still shows {len(items)} entries in the list "
                  f"region and does not say it found nothing"), "", ev.model_calls


def _model_pick(value: str, items: Sequence[str], others: Sequence[str], state: str, *,
                url: str = "", markup: str = "", on_page: Optional[Callable[[str], bool]] = None,
                where: str = "heal") -> Tuple[Optional[bool], str, int]:
    """The check model about ``items`` (the list read from the page) and ``others`` (lines
    that are not in it): ``(True, why, calls)`` only for a yes to one of ``items`` that
    ``on_page`` accepts and a no to each of ``others``; else ``(None, why, calls)``."""
    cands = ([MC.Candidate(text=str(x), ref=("item", i))
              for i, x in enumerate(items[:MODEL_ITEMS])]
             + [MC.Candidate(text=str(x), ref=("other", i))
                for i, x in enumerate(others[:MODEL_OTHERS])])
    if not items:
        return None, "no list items to ask the check model about", 0
    with MC.scope(SCOPE):            # opened for this one question only
        if not MC.available(RESULT_LINE.kind):
            return None, NO_MODEL, 0
        v = MC.check(RESULT_LINE, value, cands, state=state,
                     verify=lambda c: c.ref[0] == "item" and (on_page is None or on_page(c.text)),
                     guard_obs={"url": url, "text": state, "markup": markup,
                                "items": [str(x) for x in items]},
                     where=where)
    n_items = min(len(items), MODEL_ITEMS)
    other_yes = [s for s in v.scores[n_items:] if s is not None and s >= MC.THRESHOLD - 1e-9]
    if v.yes and v.candidate is not None and v.candidate.ref[0] == "item" and not other_yes \
            and all(s is not None for s in v.scores[n_items:]):
        return True, (f"the check model read list item {v.pick + 1} as a result of this "
                      f"search"), v.calls
    if other_yes:
        return None, "the check model also read lines outside the list as results", v.calls
    return None, f"the check model gave no answer that decides ({v.why[:80]})", v.calls


# ---------------------------------------------------------------------------
# the heal's page check
# ---------------------------------------------------------------------------

@dataclass
class PageTruth:
    """What the result pages read now say. ``verdict``: the page shows this query's list
    (``True``) or not (``False``). ``anchor``: the anchor to repair against (the old one, or
    the list the page shows now when ``refreshed``). ``pages``: the anchor's and the
    selection's page recordings, with the ground truth derived on them. ``rung``: which
    step decided (``anchor``, ``site``, ``model``, ``fallback``)."""
    verdict: Optional[bool] = None
    anchor: Optional[Dict[str, Any]] = None
    refreshed: bool = False
    pages: List[Dict[str, Any]] = field(default_factory=list)
    selector: str = ""
    rung: str = ""
    evidence: List[str] = field(default_factory=list)
    #: distinct inputs whose result page this check read
    reads: int = 0
    model_calls: int = 0
    reason: str = ""

    def why(self) -> str:
        return self.reason

    def summary(self) -> Dict[str, Any]:
        """Counts and reasons, no page text."""
        out: Dict[str, Any] = {"verdict": self.verdict, "rung": self.rung,
                               "refreshed": self.refreshed, "reads": self.reads,
                               "model_calls": self.model_calls}
        if self.evidence:
            out["evidence"] = [e[:160] for e in self.evidence][:6]
        return out


def page_truth(read: Reader, anchor: Dict[str, Any], selection: str, param: str, *,
               api_data: Any = None, contract: Optional[Dict[str, Any]] = None,
               seed: str = "", where: str = "heal") -> PageTruth:
    """Do the result pages read now show this query's list (module doc, steps 1-5)?

    ``read``: the result page reader; ``anchor``: the tool's last good answer;
    ``selection``: the input the ground truth is derived beside it; ``api_data``: the tool's
    own fresh response for the anchor's input, when it answered and only its extraction
    broke; ``contract``: the result contract whose learned empty page this page may be (an
    HTML tool's own request); ``seed``: makes the made-up input the same for the same tool."""
    from anyuse.execution import result_contract as RC
    from anyuse.execution import step_heal as H
    from anyuse.execution import step_repair as SR
    from anyuse.execution.learn_loop import derive_ground_truth
    truth = PageTruth(anchor=anchor)
    value = str(anchor["value"])
    pages = []
    for v in (value, selection):
        got = read(v)
        truth.reads += 1
        if not got:
            truth.verdict = False
            truth.reason = f"the result page for {v!r} did not answer with HTML"
            return truth
        pages.append(H._page_recording(v, param, got))
    selector = derive_ground_truth(pages, implied_end_tags=True)
    if not selector:
        truth.verdict, truth.reason = False, (
            f"the result pages for {value!r} and {selection!r} show no list that changes "
            f"with the input")
        return truth
    truth.selector = selector
    ui = pages[0].get("ui") or {}
    items = [str(x) for x in ui.get("items") or []]
    labels = [str(x) for x in ui.get("labels") or []] or items
    old = list(anchor.get("items") or [])
    share = max(SR.exact_cover(old, items)[0], SR.exact_cover(old, labels)[0])
    if share >= SR.MIN_ANCHOR_COVER:
        truth.verdict, truth.rung, truth.pages = True, "anchor", pages
        truth.reason = (f"the result page for {value!r} shows {share:.0%} of the tool's last "
                        f"good answer")
        return truth
    visible = bool(pages[0].get("ground_truth_visible_only"))
    contract_now = {**RC.replay_contract(param), "region_coverage_min": RC.REGION_COVERAGE}
    listed = RC.check_html(contract_now, pages[0].get("dom") or "", selector, {param: value},
                           items, visible_only=visible, implied_end_tags=True)
    if not listed.ok or listed.kind not in ("list", "sparse"):
        truth.verdict, truth.reason = False, (
            f"the result page for {value!r} shows no list that passes the list contract "
            f"({listed.reason[:160]})")
        return truth
    truth.evidence.append(f"the page's list passes the list contract ({listed.kind}, "
                          f"{len(items)} entries)")

    def refreshed(rung: str, values: Sequence[Any], why: str, source: str = "page") -> PageTruth:
        new = fresh_anchor(value, values, source)
        truth.verdict, truth.rung, truth.pages = True, rung, pages
        if new is not None:
            truth.anchor, truth.refreshed = new, True
        truth.reason = why
        return truth

    if api_data is not None:
        share_api, found = provenance(labels, api_data)
        if share_api is not None:
            from anyuse.execution.api_verify import MIN_COVERAGE
            truth.evidence.append(f"the tool's own response holds {share_api:.0%} of the page's "
                                  f"entries")
            if share_api >= MIN_COVERAGE:
                return refreshed("site", found, (
                    f"the result page for {value!r} lists what the tool's own response holds "
                    f"now ({share_api:.0%} of its entries)"), source="response")
            if share_api == 0:
                truth.verdict, truth.reason = False, (
                    f"the result page for {value!r} lists other things than the site's API "
                    f"returns for it now (none of its {len(labels[:ANCHOR_ITEMS])} entries is "
                    f"in the tool's own response)")
                return truth
    nonsense = made_up([value, selection], seed or value)
    bound, why_bound, by, calls = query_bound(read, selector, nonsense, visible_only=visible,
                                              contract=contract)
    truth.model_calls += calls
    truth.reads += 1
    truth.evidence.append(why_bound)
    near = continuity(old, labels)
    truth.evidence.append(f"{near:.0%} of the last good answer's entries have a near entry on "
                          f"the page")
    if bound and near >= CONTINUITY:
        return refreshed(by or "site", labels, (
            f"the result page for {value!r} lists this query's results now: {why_bound}, and "
            f"{near:.0%} of the last good answer's entries are still there"))
    shown = set(_lines(pages[0].get("dom") or ""))
    ok, why_model, calls = _model_pick(
        value, labels, page_lines(pages[0].get("dom") or "", [*items, *labels]),
        "\n".join(_lines(pages[0].get("dom") or "")), url=str(pages[0].get("final_url") or ""),
        markup=str(pages[0].get("dom") or ""),
        on_page=lambda line: line in shown or any(_norm(line) and _norm(line) in _norm(s)
                                                  for s in shown), where=where)
    truth.model_calls += calls
    if why_model != NO_MODEL:
        truth.evidence.append(why_model)
    if ok:
        return refreshed("model", labels, (
            f"the result page for {value!r} lists this query's results now: {why_model}"))
    truth.verdict, truth.reason = False, (
        f"the result page for {value!r} lists other things than the tool's last good answer "
        f"({share:.0%} of its items, {SR.MIN_ANCHOR_COVER:.0%} needed), and nothing on the page "
        f"says it is this query's list now ({'; '.join(truth.evidence)[:300]})")
    return truth


# ---------------------------------------------------------------------------
# a restore: does the request's answer hold now?
# ---------------------------------------------------------------------------

def _items_of(result: Dict[str, Any]) -> List[Any]:
    got = ((result or {}).get("extracted") or {}).get("items")
    return list(got) if isinstance(got, list) else []


def _page_truth_now(record: Dict[str, Any], param: str, value: str, executor, *,
                    hosts_path: Optional[str], base_dir: str) -> Tuple[Optional[List[str]], str]:
    """The server-rendered result page's list for ``value`` now, read with a plain GET, or
    ``(None, why not)``."""
    from anyuse.execution import step_heal as H
    from anyuse.execution.api_permission import check_permission
    from anyuse.execution.auto_recompile import proof_values
    from anyuse.execution.html_extract import extract
    from anyuse.execution.learn_loop import derive_ground_truth
    page = H.page_block(record, base_dir)
    if not page or not page.get("html_holds_items"):
        return None, "no server-rendered result page"
    t = H.page_template(page, param)
    try:
        url = t.render({param: value})[0]
    except (KeyError, ValueError):
        return None, "the result page's address cannot be filled in"
    verdict = check_permission(url, hosts_path=hosts_path)
    if not verdict.may_prefer:
        return None, f"the result page may not be read: {verdict.reason}"
    read = H.page_reader(t, executor)
    got = read(value)
    if not got:
        return None, f"the result page for {value!r} did not answer with HTML"
    browser = (record.get("browser") or {}).get("template") or {}
    selector = str((browser.get("extract") or {}).get("items") or "")
    if selector:
        try:
            labels = extract(got[1], selector, limit=30,
                             visible_only=bool(browser.get("visible_only")), labels=True,
                             implied_end_tags=True)
        except ValueError:
            labels = []
        if len(labels) >= 3:
            return [str(x) for x in labels], "the result page"
    other = next((v for v in [*proof_values(record), *(page.get("values") or [])]
                  if _norm(v) and _norm(v) != _norm(value)), "")
    second = read(other) if other else None
    if not second:
        return None, "no second result page to derive the list with"
    pages = [H._page_recording(value, param, got), H._page_recording(other, param, second)]
    if not derive_ground_truth(pages, implied_end_tags=True):
        return None, "the result pages show no list that changes with the input"
    ui = pages[0].get("ui") or {}
    return [str(x) for x in (ui.get("labels") or ui.get("items") or [])], "the result page"


def answer_holds_now(record: Dict[str, Any], template: Any, anchor: Dict[str, Any],
                     result: Dict[str, Any], *, executor, params: Optional[Dict[str, Any]] = None,
                     hosts_path: Optional[str] = None, base_dir: str = "",
                     replay: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                     other: Optional[Tuple[str, Dict[str, Any]]] = None
                     ) -> Tuple[bool, str, Dict[str, Any]]:
    """Does ``result`` (the request's answer for the anchor's input) hold now?

    1. It must pass the request's checks; repeating the last good answer settles it.
    2. Else the list the site shows for that input now: the browser replay (``replay``),
       else the server-rendered result page; the answer must match it as the proof's list
       comparison requires.
    3. Without either, the answer must change with the input (``other``: the call's own
       input and answer, else one request for a made-up input), and the check model must
       say its items are results and the other answer's are not.

    Returns ``(holds, why, detail)``; ``detail`` has ``by``, ``model_calls`` and
    ``requests`` (the extra requests made here)."""
    from anyuse.execution import step_repair as SR
    from anyuse.execution.api_verify import compare
    from anyuse.execution.result_contract import contract_of
    detail: Dict[str, Any] = {"by": "", "model_calls": 0, "requests": 0}
    value = str(anchor.get("value") or "")
    holds, why = SR.answer_holds(anchor, result)
    if not (result or {}).get("success"):
        return False, why, detail
    if holds:
        detail["by"] = "anchor"
        return True, why, detail
    items = _items_of(result)
    names = list(getattr(template, "input_params", None) or getattr(template, "params", None) or [])
    param = str((contract_of(template) or {}).get("query_param") or (names[0] if names else "q"))
    base = dict(params or {})
    truth, source = None, ""
    rung = record.get("browser") or {}
    if replay is not None and rung.get("template"):
        try:
            res = replay({**base, param: value})
        except Exception:  # noqa: BLE001 - no replay is no ground truth, never a crash
            res = {}
        ext = (res or {}).get("extracted") or {}
        shown = (ext.get("labels") if rung.get("read") == "labels" and ext.get("labels")
                 else ext.get("items"))
        if (res or {}).get("success") and shown:
            truth, source = [str(x) for x in shown], "the browser replay"
            detail["requests"] += 1
    if truth is None:
        truth, source = _page_truth_now(record, param, value, executor, hosts_path=hosts_path,
                                        base_dir=base_dir)
        if truth is None:
            source = ""
    if truth is not None:
        detail["by"] = "browser" if source == "the browser replay" else "page"
        cmp = compare(truth, items, ordered=True, value=value, records=True)
        if cmp.passed:
            return True, (f"{why}; it matches what {source} shows for {value!r} now "
                          f"({cmp.coverage:.0%})"), detail
        return False, (f"{why}; it does not match what {source} shows for {value!r} now "
                       f"({cmp.reason[:160]})"), detail
    # no page and no browser: the answer must at least change with the input
    if other is None or not _norm(other[0]) or _norm(other[0]) == _norm(value):
        nonsense = made_up([value], str(getattr(template, "url", "") or value))
        other = (nonsense, executor.execute(template, {**base, param: nonsense}))
        detail["requests"] += 1
    other_value, other_result = other
    other_result = other_result if isinstance(other_result, dict) else {}
    status = other_result.get("status")
    if not other_result.get("success") and not (
            isinstance(status, int) and not isinstance(status, bool) and status < 500
            and not (other_result.get("blocked") or other_result.get("blocked_state"))):
        # a server error, a timeout, a block or a refusal is no other answer
        return False, (f"{why}; the request for {other_value!r} got no answer from the site, so "
                       f"nothing shows that the answer changes with the input"), detail
    theirs = [str(x) for x in _items_of(other_result)] if other_result.get("success") else []
    if theirs and SR.exact_cover(items, theirs)[0] >= SR.MIN_ANCHOR_COVER \
            and SR.exact_cover(theirs, items)[0] >= SR.MIN_ANCHOR_COVER:
        return False, (f"{why}; the request gives the same list for {value!r} and "
                       f"{other_value!r}"), detail
    mine = [str(x) for x in items]
    state = "\n".join(mine[:10])
    ok, why_model, calls = _model_pick(value, mine, [x for x in theirs if x not in mine][:2], state,
                                       url=str((result or {}).get("url") or ""), on_page=lambda t: t in mine,
                                       where="restore")
    detail["model_calls"] = calls
    if ok:
        detail["by"] = "model"
        return True, f"{why}; the answer changes with the input and {why_model}", detail
    return False, f"{why}; no page or browser replay shows the list now", detail


# ---------------------------------------------------------------------------
# does the site itself answer?
# ---------------------------------------------------------------------------

def _site_urls(record: Dict[str, Any], template: Any, base_dir: str = "") -> List[str]:
    """Pages of the site itself, best first: the tool's result page for the anchor's input,
    the task's start page, the site's front page."""
    from anyuse.execution import step_heal as H
    from anyuse.execution import step_repair as SR
    out: List[str] = []
    page = H.page_block(record, base_dir)
    anchor = SR.anchor_of(record)
    names = list(getattr(template, "input_params", None) or getattr(template, "params", None)
                 or [])
    if page and anchor and len(names) == 1:
        try:
            out.append(H.page_template(page, names[0]).render(
                {names[0]: str(anchor["value"])})[0])
        except (KeyError, ValueError):
            pass
    start = str(((record.get("task") or {}).get("start_url")) or "")
    if start.startswith("https://"):
        out.append(start)
    parts = urllib.parse.urlsplit(str(getattr(template, "url", "") or ""))
    if parts.scheme == "https" and parts.netloc:
        out.append(f"{parts.scheme}://{parts.netloc}/")
    return list(dict.fromkeys(out))


def site_answers(record: Dict[str, Any], template: Any, executor, *,
                 hosts_path: Optional[str] = None, base_dir: str = "",
                 failing_url: str = "") -> Tuple[Optional[bool], str]:
    """Does the site itself answer now? One GET of the first page of the site
    (:func:`_site_urls`) that is not the failing request's own URL, on an acknowledged host.
    robots.txt is the one known locally, as for every call of the compiled request: this
    sends no request but the one GET, also while the site is down. ``True`` on a 2xx;
    ``False`` when the site did not answer that either (a server error, a timeout, a refused
    connection); ``None`` for anything else (a block, a 4xx, no page that may be read),
    which says nothing."""
    from anyuse.execution.api_permission import host_acknowledged, robots_allows_cached
    from anyuse.execution.api_recompile import site_did_not_answer
    from anyuse.execution.api_step import RequestTemplate
    failing = urllib.parse.urldefrag(str(failing_url or ""))[0]
    url = next((u for u in _site_urls(record, template, base_dir) if u != failing), "")
    if not url:
        return None, "no page of the site is known to read"
    host = urllib.parse.urlsplit(url).netloc
    if not host_acknowledged(host, hosts_path):
        return None, f"the site's page host {host} is not acknowledged"
    allowed, note = robots_allows_cached(url)
    if allowed is False:
        return None, f"the site's page {url} may not be read: {note}"
    probe = RequestTemplate(method="GET", url=url, params=[], extract={}, extract_kind="html",
                            shape="list", expect={"status_in": [200]},
                            notes="the site's own page, read to see whether the site answers")
    try:
        res = executor.execute(probe, {})
    except Exception as exc:  # noqa: BLE001 - no answer, never a crash
        return None, f"the site's page could not be read ({type(exc).__name__})"
    res = res if isinstance(res, dict) else {}
    status = res.get("status")
    if res.get("blocked") or res.get("blocked_state") or res.get("requires_human"):
        return None, f"the site's page {url} is blocked"
    if isinstance(status, int) and 200 <= status < 300:
        return True, f"the site's own page {url} answered (HTTP {status})"
    if site_did_not_answer(status, str(res.get("error") or ""), res):
        return False, f"the site's own page {url} did not answer either"
    return None, (f"the site's page {url} answered {status or res.get('error') or 'nothing'}, "
                  f"which says nothing about the request")
