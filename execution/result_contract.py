"""Runtime result contracts: what a proved list tool may return, and when.

In a repeatability study 37 of 38
failures were one product behaviour. The proof finalised ``min_items`` to the
smallest item count it saw (``api_verify.finalize_recorded_minimum``) and the
executor then enforced that count on EVERY later run. A proof-time count is
not a property of the site: a correct search for ``boost`` in a package
repository has one result, and a floor of 4 refused it. The floor also
never proved anything about the items it let through.

This module replaces the floor at runtime with the contract the proof checked,
stated per result size:

* **list** (``>= MIN_LIST`` items): the proof's list contract (>= 80 % of the
  visible labels, in order) is what made the selector trustworthy. At runtime
  the list is accepted without a count floor; for an HTML rung the extraction
  must still cover the result REGION it sits in (the records beside the
  matched ones), when the proof measured that coverage on every validation
  page (``region_coverage_min``). A class rename on some rows then fails
  loudly instead of returning a partial list. When the proof saw one matched
  item per result record on every validation page (``one_item_per_record``;
  :func:`record_units` reads the page's repeated records), a record that later
  holds a second matched item (an ad or a variant link beside its title)
  answers one item: its title's.
* **sparse** (1 .. ``MIN_LIST - 1`` items): accepted only when EACH item's
  identity key names the query: the key (the first text of the record: a
  package name, a title) contains the query's words as one contiguous run of
  whole tokens after normalisation (``mingw-w64-boost`` names ``boost``;
  ``boostnote`` and a description that merely mentions it do not). A small
  page of the wrong thing (another query's answer, a sidebar, a "popular"
  box) fails here. In a script written without spaces between words the run
  may start a longer run of letters (東京駅 names 東京); at the end of or
  inside one (京都 in 東京都庁) it counts only when the site's own result lists
  did that on the fitting inputs (``query_positions``) or, on a product call,
  the configured check model reads it as a word there (execution/query_match.py).
* **empty** (0 items): accepted only with evidence that the site itself says
  this search found nothing (:func:`empty_verdict`), in three steps:

  1. the site's own empty page (execution/empty_page.py): the page the site
     serves for made-up inputs, learned from the site in any language. A page
     that is that page for this input is empty; a page that shows it plus lines
     it lacks (rows the selector no longer reads, an error), or a search that no
     longer reads its input, is not;
  2. the configured check model (execution/model_check.py), asked about the
     page's lines that name the input, only on a product call;
  3. the statements of :data:`_NO_RESULTS` in the listed languages, bound to
     the query (:func:`empty_evidence_page`; JSON: an empty list whose response
     echoes the query and states a zero count). A page that reports an error
     never qualifies, and neither does a "no results" phrase in a condition, a
     help instruction or a question ("If no results for x, contact us", "No
     results for x? Check the spelling"), or a line the site's result pages
     show too.

  Without such evidence an empty extraction is a FAILURE (the markup probably
  changed), exactly as before; "it worked, here are no results" is never inferred.

The contract lives in the template's ``expect`` (``result_contract``), so the
proof's ``template_digest`` pins it, the learned empty page with it: a record
whose contract changed must be re-proved (:func:`upgrade_record`). A template
without one keeps the old floor (records written before this change behave
exactly as they did).

The browser replay below the compiled request carries the same contract
(:func:`replay_contract`), so it never serves a page this rung refuses. Its
statement rule (:func:`empty_evidence_page`) also requires the statement to be
on screen and to name the query in its own line, in the heading right above it,
or in a search summary a few lines above it.

No I/O of its own: the only requests are the ``fetch`` callables a caller
passes (its own executor), and the only model is the one ``model_check`` asks
inside a caller's scope (about an empty page's lines, or a sparse key or a "no
results" statement that holds the query inside a longer word). Every function
returns a verdict with a reason a person can check against the page.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import query_match as QM
from anyuse.execution.unicode_text import STRICT_POSITIONS

#: key in ``RequestTemplate.expect``
CONTRACT_KEY = "result_contract"
VERSION = 1
#: a result list of this size is judged by the list contract (the proof's
#: ``api_verify.MIN_COMPARED``)
MIN_LIST = 3
#: share of the result region's records an HTML extraction must cover (the
#: proof's ``api_verify.MIN_COVERAGE``)
REGION_COVERAGE = 0.8
#: how far (characters of visible text) the query may sit from a "no results"
#: statement for the statement to count as being about THIS query
EMPTY_WINDOW = 160
#: extracted items the executor keeps (``html_extract.extract``'s default)
HTML_LIMIT = 50

#: A page saying, in its own words, that the search found nothing: "no results
#: (found)", "nothing found", "0 results", "your search did not match", and the
#: same in a few languages. Whole phrases only; the compiled rung and the
#: browser replay both read it, as the empty-answer cascade's last step, after
#: the site's own empty page and the check model. "No matching" counts with a
#: result noun, or with a one- or two-word noun followed by "for" ("No matching
#: packages for zzqx"). A bare "no match", "no matching" or "found no" does not.
#:
#: Russian, Greek, Arabic and Hebrew statements are words between spaces, like
#: the Latin ones; an Arabic conjunction is written against the word after it
#: ("ولا توجد نتائج"). Japanese, Chinese and Korean ones have no word boundary: a
#: particle or the query is written right against them ("「東京」に一致する商品は
#: 見つかりませんでした"), so they are matched without one. Their bare verbs are
#: not enough ("見つかりませんでした" alone is also a missing page's message, "找不到"
#: alone a help link's): each form names results, items or a match. A count of 0
#: is not read after another digit ("10件"). A Japanese or Chinese form that asks
#: ("該当する商品が見つかりませんか？", "找不到商品？", "...吗") is a help line's
#: question, not a statement; :func:`_statements` drops the questions and
#: conditions of every language. "に合う" ("that fits") is read as a match, not
#: "間に合う" ("in time").
_NO_RESULTS = re.compile(
    r"(?i)\b(?:"
    r"no (?:results?|matches|hits|items|entries|records|documents|packages|datasets)"
    r"(?: (?:were|was))?(?: found)?"
    r"|no matching (?:results?|items|entries|records|documents|packages"
    r"|[a-z]+(?: [a-z]+)?(?= for\b))"
    r"|nothing (?:was )?found|found no (?:results?|matches|items|entries)"
    r"|(?:0|zero) (?:results?|matches|hits|items|entries|records)"
    r"|your search .{0,60}?did(?:n't| not) match|did(?:n't| not) match any"
    r"|could not find any|keine (?:ergebnisse|treffer)|aucun r[ée]sultat"
    r"|sin resultados|nessun risultato|no se han encontrado"
    # Russian
    r"|ничего не (?:найдено|нашлось)|нет (?:результатов|совпадений)|результатов не найдено"
    r"|не найдено (?:ни одного (?:результата|товара)|результатов|товаров|совпадений)"
    r"|(?:товары|результаты|совпадения) не найдены|(?:товаров|совпадений) не найдено"
    r"|0 (?:результатов|товаров|совпадений)"
    # Greek
    r"|δεν βρ[εέ]θηκαν (?:αποτελ[εέ]σματα|προϊ[οό]ντα)|δεν υπ[αά]ρχουν αποτελ[εέ]σματα"
    r"|καν[εέ]να αποτ[εέ]λεσμα"
    # Arabic
    r"|[وف]?لا (?:توجد|يوجد) (?:أي )?نتائج|[وف]?لا نتائج"
    r"|[وف]?لم (?:يتم العثور على|نعثر على|يتم إيجاد|يتم ايجاد) (?:أي |اي )?(?:نتائج|منتجات)"
    # Hebrew
    r"|לא נמצאו (?:תוצאות|מוצרים|פריטים)|אין תוצאות|לא נמצאה אף תוצאה|(?:נמצאו )?0 תוצאות)"
    # Japanese
    r"|検索結果(?:は|が)(?:(?:ありません|見つかりません)(?!(?:でした)?か)(?:でした)?|0件です(?!か))"
    r"|(?:(?:一致|該当|合致)する|(?<=に)(?<!間に)合う)[^\s、。！？!?]{1,12}?(?:は|が)(?:ありません|見つかりません)"
    r"(?!(?:でした)?か)(?:でした)?"
    r"|(?:検索結果|結果|商品)(?:は|が)見つかりませんでした(?!か)"
    r"|検索結果[:：]?\s?(?<![0-9０-９])[0０]\s?件|(?<![0-9０-９])[0０]\s?件(?:の(?:検索)?結果|ヒット)"
    # Chinese, simplified and traditional; not when its clause ends in a question
    r"|(?:(?:[没沒]有找到|找不到|未找到|查[无無])(?:相[关關]|匹配|符合[条條]件)?的?(?:搜索|搜尋)?"
    r"(?:[结結]果|商品|[内內]容|[产產]品|[记記][录錄])"
    r"|(?:[没沒]有找到|找不到|未找到)(?=[与與和跟]|[关關]于|有[关關]|\s?[“\"「『'‘])"
    r"|(?:搜索|搜尋)?[无無](?:搜索|搜尋)?[结結]果"
    r"|[暂暫][无無](?:相[关關]|匹配|符合[条條]件)?的?(?:搜索|搜尋)?(?:[结結]果|商品|[产產]品)"
    r"|[没沒]有(?:相[关關]|匹配|符合[条條]件)?的?(?:搜索|搜尋)?[结結]果"
    r"|(?<![0-9０-９])[0０]\s?(?:条|條|个|個|件)?(?:搜索|搜尋)?[结結]果)"
    r"(?![^\s，,。.！!；;]{0,24}[？?吗嗎])"
    # Korean, formal and polite
    r"|검색\s?결과가\s?(?:없습니다|없어요)|검색된\s?(?:결과|상품)[이가]?\s?(?:없습니다|없어요)"
    r"|(?:일치하는|해당하는|관련된?)\s?(?:검색\s?)?(?:결과|상품)[이가]?\s?(?:없습니다|없어요)"
    r"|검색\s?결과\s?없음")

#: Error phrases in the listed languages. A line that reports an error is never
#: completion evidence (:func:`_reports_error`): in the empty-answer cascade it is
#: read only on lines the site's result pages do not show too, the check model
#: judges each such line first on a product call, and this list is the last rung.
#: "Try again later" is an instruction, as :data:`_CONDITION` reads it, not a
#: report of an error; a phrase in a condition or a question ("Something went
#: wrong? Contact us") is help text.
_ERROR_PAGE = re.compile(
    r"(?i)\b(?:internal server error|server error|something went wrong"
    r"|an (?:unexpected )?error (?:has )?occurred|service (?:is )?(?:temporarily )?unavailable"
    r"|bad gateway|gateway time-?out|too many requests|try again later"
    r"|page not found|404 not found|access denied|request failed"
    r"|произошла ошибка|ошибка сервера|сервис (?:временно )?недоступен"
    r"|παρουσι[αά]στηκε (?:ένα |ενα )?σφ[αά]λμα|σφ[αά]λμα διακομιστ[ηή]"
    r"|حدث خطأ|خطأ في الخادم|אירעה שגיאה|שגיאת שרת)\b"
    r"|エラーが発生しました|サーバーエラー|問題が発生しました"
    r"|[发發]生[错錯][误誤]|服[务務]器[错錯][误誤]|出[错錯]了"
    r"|오류가\s?발생했습니다|서버\s?오류")

#: JSON keys that state how many results there are (the statement rule; the
#: site's own empty answer names its count field itself)
_COUNT_KEYS = ("total", "count", "total_count", "totalcount", "totalresults",
               "total_results", "num_results", "numresults", "nbhits", "nb_hits",
               "hits_total", "found", "results_count", "resultcount", "size")


# ---------------------------------------------------------------------------
# the contract object
# ---------------------------------------------------------------------------

def supports(template: Any) -> bool:
    """A single-request list template whose items are one extraction path."""
    if template is None or getattr(template, "steps", None):
        return False
    if getattr(template, "shape", "list") != "list":
        return False
    if getattr(template, "extract_kind", "json") not in ("html", "json", "embedded_json"):
        return False
    return "items" in (getattr(template, "extract", None) or {})


def _query_param(template: Any) -> str:
    try:
        names = list(template.input_params)
    except Exception:                                   # noqa: BLE001
        names = []
    names = names or list(getattr(template, "params", None) or [])
    return str(names[0]) if names else ""


def replay_contract(query_param: str,
                    positions: Optional[Sequence[str]] = STRICT_POSITIONS) -> Dict[str, Any]:
    """The contract a browser replay template carries (``cdp_replay``): the same
    list, sparse and empty rules as a compiled request's. A replay template is
    not calibrated by :func:`finalize`, so it has no region or record checks.
    ``positions``: where in a run of an unspaced script the query may stand
    (``query_positions``); ``None`` leaves the key out, which accepts every
    position (a template proved before positions existed)."""
    c = {"version": VERSION, "kind": "ordered_list",
         "query_param": str(query_param or ""),
         "list": {"min_items": MIN_LIST},
         "sparse": {"max_items": MIN_LIST - 1, "identity": "key_names_query"},
         "empty": {"requires": "query_bound_no_results"},
         "region_coverage_min": None,
         "calibration": "browser replay: no region or record calibration"}
    if positions is not None:
        c[QM.CONTRACT_KEY] = list(positions)
    return c


def new_contract(template: Any) -> Dict[str, Any]:
    c = replay_contract(_query_param(template))
    # set from the validation pages by :func:`finalize`
    c["calibration"] = "pending"
    return c


def attach(template: Any) -> bool:
    """Put the contract on ``template`` before its final validation, so the
    proof runs the same runtime checks it certifies. Idempotent: an existing
    contract is kept with its learned empty page, which a caller whose request
    changed drops (:func:`drop_empty_page`) and learns again."""
    if not supports(template):
        return False
    expect = template.expect
    if not isinstance(expect.get(CONTRACT_KEY), dict):
        expect[CONTRACT_KEY] = new_contract(template)
    return True


def set_positions(template: Any, positions: Sequence[str], why: str) -> bool:
    """Store the positions the site's own result lists showed on the fitting
    inputs (``query_match.calibrate``) in a contract that has the key."""
    c = contract_of(template)
    if c is None or QM.CONTRACT_KEY not in c:
        return False
    c[QM.CONTRACT_KEY] = list(positions)
    c[QM.CALIBRATION_KEY] = str(why or "")[:300]
    return True


def carry_positions(previous: Any, template: Any) -> bool:
    """Give ``template``'s contract the query positions of ``previous``'s (a heal
    changes the extraction, not how the site matches its input): its list and why,
    or none, for a template proved before positions existed."""
    old, c = contract_of(previous), contract_of(template)
    if old is None or c is None:
        return False
    for key in (QM.CONTRACT_KEY, QM.CALIBRATION_KEY):
        if key in old:
            c[key] = copy.deepcopy(old[key])
        else:
            c.pop(key, None)
    return True


def contract_of(template_or_expect: Any) -> Optional[Dict[str, Any]]:
    expect = template_or_expect if isinstance(template_or_expect, dict) else \
        getattr(template_or_expect, "expect", None) or {}
    c = expect.get(CONTRACT_KEY)
    return c if isinstance(c, dict) and c.get("version") else None


def finalize(template: Any, proof: Any) -> None:
    """Calibrate the region-coverage check from what the proof MEASURED.

    Enforced only when every passing validation page had a measured coverage
    of at least :data:`REGION_COVERAGE`: a site whose result container also
    holds non-result children of the same tag would otherwise be refused on a
    correct page. The decision and its evidence are stored in the contract.
    ``one_item_per_record`` is set when every passing validation page showed a
    record structure with one matched item per record, and removed otherwise.
    """
    c = contract_of(template)
    if c is None:
        return
    measured = []
    for case in getattr(proof, "cases", None) or []:
        if not getattr(case, "passed", False):
            continue
        rt = (getattr(case, "contract", None) or {}).get("runtime") or {}
        measured.append(rt.get("region_coverage"))
    depths = []
    for case in getattr(proof, "cases", None) or []:
        if getattr(case, "passed", False):
            rt = (getattr(case, "contract", None) or {}).get("runtime") or {}
            depths.append(rt.get("record_depth"))
    kind = getattr(template, "extract_kind", "json")
    sigs, maxes = [], []
    for case in getattr(proof, "cases", None) or []:
        if getattr(case, "passed", False):
            rt = (getattr(case, "contract", None) or {}).get("runtime") or {}
            sigs.append(rt.get("key_signature"))
            maxes.append(rt.get("max_keys_per_item"))
    if kind == "html" and sigs and len(set(sigs)) == 1 and sigs[0] \
            and all(isinstance(m, int) for m in maxes):
        # the record key's element signature and how many of them one item
        # held at most: an item holding more at runtime spans merged records
        c["key_signature"] = sigs[0]
        c["max_keys_per_item"] = max(maxes)
    if kind == "html" and depths and len(set(depths)) == 1 and isinstance(depths[0], int):
        # the level of one result record below the matched node, the same on
        # every validation page: identity keys and the region are read there
        c["record_depth"] = depths[0]
    per_record = []
    for case in getattr(proof, "cases", None) or []:
        if getattr(case, "passed", False):
            rt = (getattr(case, "contract", None) or {}).get("runtime") or {}
            per_record.append(rt.get("max_items_per_record"))
    if kind == "html" and per_record and all(m == 1 for m in per_record):
        # every validation page showed one matched item per result record: a
        # later page with a second link in a record (an ad, a variant) keeps one
        c["one_item_per_record"] = True
    else:
        # not measured by this proof: a flag from an earlier one is not kept
        c.pop("one_item_per_record", None)
    if kind != "html":
        c["region_coverage_min"] = None
        c["calibration"] = "no region check: the JSON list path is the whole result list"
    elif measured and all(isinstance(m, (int, float)) and m >= REGION_COVERAGE
                          for m in measured):
        c["region_coverage_min"] = REGION_COVERAGE
        c["calibration"] = (f"region coverage >= {REGION_COVERAGE} measured on all "
                            f"{len(measured)} validation page(s)")
    else:
        c["region_coverage_min"] = None
        shown = ", ".join("n/a" if m is None else f"{m:.2f}" for m in measured) or "none"
        c["calibration"] = (f"region coverage not enforced: measured {shown} on the "
                            f"validation page(s), below {REGION_COVERAGE} or not measurable")


# ---------------------------------------------------------------------------
# identity: does an item's key name the query?
# ---------------------------------------------------------------------------

def key_naming(key: Any, value: Any, reading: Optional[QM.Reading] = None) -> Tuple[bool, str]:
    """``(named, how)`` for :func:`key_names_query` (``query_match.naming``)."""
    from anyuse.execution.api_compile import normalise
    q, k = normalise(value), normalise(key)
    if not q or not k or q.count(" ") > k.count(" "):
        return False, QM.ABSENT
    return QM.naming(k, q, reading, whole_value=True)


def key_names_query(key: Any, value: Any, reading: Optional[QM.Reading] = None) -> bool:
    """The key contains the query's tokens as ONE contiguous run of whole
    tokens (normalised: casefolded, punctuation as separators). In a script
    written without spaces between words the run may sit inside a longer
    token ("東京タワー" names "東京"; ``api_compile.word_spans``): at the
    positions ``reading`` accepts (every one without a reading), or where its
    model reads the run as a word."""
    return key_naming(key, value, reading)[0]


def _has_alnum(text: str) -> bool:
    """Does ``text`` hold a letter or a digit, in any script?"""
    return any(ch.isalnum() for ch in text)


def _own_text(n: Any) -> str:
    return " ".join(t for t in getattr(n, "_text", []) if t and t.strip()).strip()


_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_NO_KEY_TAGS = ("script", "style", "noscript", "template", "button", "label")


def key_element(record: Any) -> Any:
    """The element carrying a record's identity: its first heading (a card's
    title), else its first link with text (a row's name cell), else the first
    element with text of its own; the record itself as a last resort."""
    nodes = [n for n in record.walk() if getattr(n, "tag", "") not in _NO_KEY_TAGS]
    for n in nodes:
        if n.tag in _HEADING_TAGS and _has_alnum(n.text()):
            return n
    for n in nodes:
        if n.tag == "a" and _has_alnum(n.text()):
            return n
    for n in nodes:
        if _has_alnum(_own_text(n)):
            return n
    return record


def html_item_key(record: Any) -> str:
    """A record's identity key (the text of :func:`key_element`)."""
    el = key_element(record)
    if el is record and not any(True for _ in (c for c in record.children if c.tag)):
        return record.text()
    return el.text() if el.tag in _HEADING_TAGS or el.tag == "a" else (_own_text(el) or el.text())


def signature(node: Any) -> str:
    return node.tag + "".join("." + c for c in sorted(node.classes))


def keys_in(node: Any, sig: str) -> int:
    """How many elements with the key signature ``sig`` the item node holds
    (itself included): one per record it spans."""
    return sum(1 for n in node.walk() if getattr(n, "tag", "") and signature(n) == sig)


def json_item_key(item: Any) -> str:
    if isinstance(item, dict):
        for v in item.values():
            if isinstance(v, str) and v.strip():
                return v
        return ""
    if isinstance(item, list):
        return json_item_key(item[0]) if item else ""
    return "" if item is None else str(item)


# ---------------------------------------------------------------------------
# region coverage (HTML): did the extraction take the whole result region?
# ---------------------------------------------------------------------------

def _ancestors(node: Any) -> List[Any]:
    out, cur = [], node
    while cur is not None and getattr(cur, "tag", ""):
        out.append(cur)
        cur = cur.parent
    return out


def _lca(nodes: Sequence[Any]) -> Any:
    common = _ancestors(nodes[0])
    for n in nodes[1:]:
        anc = {id(a) for a in _ancestors(n)}
        common = [a for a in common if id(a) in anc]
    return common[0] if common else None


def _up(node: Any, depth: int) -> Any:
    cur = node
    for _ in range(depth):
        if cur.parent is None or not getattr(cur.parent, "tag", ""):
            return None
        cur = cur.parent
    return cur


def records_of(nodes: Sequence[Any], depth: Optional[int] = None
               ) -> Tuple[List[Any], Any, Optional[int]]:
    """``(records, region, depth)`` for the matched nodes.

    A record is the element that holds one result (a row, a card); the region
    is the element holding the records. With a proved ``depth`` (how many
    levels a matched node sits below its record, calibrated on the validation
    pages) the records are those ancestors. Without one: for two or more
    nodes, the children of their lowest common ancestor that hold them; for a
    single node, the node itself."""
    nodes = [n for n in nodes if n is not None]
    if not nodes:
        return [], None, depth
    if depth is not None:
        recs: List[Any] = []
        for n in nodes:
            r = _up(n, depth)
            if r is None:
                return [], None, None
            if all(r is not x for x in recs):
                recs.append(r)
        parents = {id(r.parent) for r in recs}
        region = recs[0].parent if len(parents) == 1 else _lca(recs)
        return recs, region, depth
    if len(nodes) == 1:
        return [nodes[0]], nodes[0].parent, None
    region = _lca(nodes)
    if region is None or any(n is region for n in nodes):
        return [], None, None
    recs, depths = [], set()
    for n in nodes:
        cur, d = n, 0
        while cur is not None and cur.parent is not region:
            cur, d = cur.parent, d + 1
        if cur is None:
            return [], None, None
        depths.add(d)
        if all(cur is not r for r in recs):
            recs.append(cur)
    return recs, region, (depths.pop() if len(depths) == 1 else None)


def region_coverage(records: Sequence[Any], region: Any) -> Optional[float]:
    """Share of the region's records that the extraction holds: the region's
    children of the same tag as the held records, with text. ``None`` when
    nothing can be measured."""
    from anyuse.execution.html_extract import MIN_ROW_TEXT
    if not records or region is None or not getattr(region, "tag", ""):
        return None
    tags = {r.tag for r in records}
    pool = [c for c in region.children if c.tag in tags and len(c.text()) >= MIN_ROW_TEXT]
    if not pool:
        return None
    covered = sum(1 for c in pool if any(c is r for r in records))
    return covered / len(pool)


# ---------------------------------------------------------------------------
# record units: the repeated record each node sits in
# ---------------------------------------------------------------------------

#: never a record (``demo_cards.NEVER_CARD``'s document levels)
_NO_RECORD = frozenset(("html", "body", "main"))
#: how far above a node its record may sit (``demo_cards.MAX_UP``)
RECORD_MAX_UP = 14


@dataclass
class RecordUnits:
    """:func:`record_units`' answer, one entry per node. Nodes of one record share
    an id; ``None`` is no record found. ``primary``: the node sits inside its
    record where the records' nodes mostly sit (a card's title link, not its ad
    or variant link). ``how`` the record level was found: ``separates``,
    ``siblings``, ``depth``, or ``""`` (no level: every id is None)."""
    ids: List[Optional[int]]
    records: List[Any]
    primary: List[bool]
    how: str = ""

    def most_per_record(self) -> Optional[int]:
        """The most nodes one record holds; None when no record level was found."""
        if not self.how:
            return None
        counts: Dict[int, int] = {}
        for i in self.ids:
            if i is not None:
                counts[i] = counts.get(i, 0) + 1
        return max(counts.values()) if counts else 1


def _rsig(n: Any) -> str:
    """A node's tag and its classes without a digit (a per-item class is not structure)."""
    return n.tag + "".join("." + c for c in sorted(n.classes) if not any(ch.isdigit() for ch in c))


def _pdesc(n: Any) -> str:
    """:func:`_rsig`, with the node's position among its same-signature siblings
    (``div@1``) when it has any."""
    sig = _rsig(n)
    p = n.parent
    if p is None:
        return sig
    same = [c for c in p.children if c.tag and _rsig(c) == sig]
    if len(same) < 2:
        return sig
    return sig + "@" + str(next(k for k, c in enumerate(same) if c is n))


def _inner_path(n: Any, top: Any) -> Tuple[str, ...]:
    """The positional path from ``top`` down to ``n`` (``top`` itself not included)."""
    out: List[str] = []
    cur = n
    while cur is not None and cur is not top:
        out.append(_pdesc(cur))
        cur = cur.parent
    return tuple(reversed(out))


def _loose(path: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(step.split("@", 1)[0] for step in path)


def _contains(top: Any, n: Any) -> bool:
    cur = n
    while cur is not None:
        if cur is top:
            return True
        cur = cur.parent
    return False


def _holds_path(top: Any, path: Tuple[str, ...]) -> bool:
    level = [top]
    for step in path:
        level = [c for x in level for c in x.children if c.tag and _pdesc(c) == step]
        if not level:
            return False
    return True


def _root_depth(n: Any) -> int:
    d, cur = 0, n
    while cur is not None and cur.parent is not None:
        d, cur = d + 1, cur.parent
    return d


def _own_record(n: Any, others: Sequence[Any], max_up: int = RECORD_MAX_UP,
                text_len: Optional[Callable[[Any], int]] = None) -> Tuple[Any, str]:
    """The record ``n`` sits in, found from ``n`` alone (``demo_cards.find_card``'s
    rule): the lowest ancestor (or ``n`` itself) among two or more siblings of
    its signature with text whose sibling holds another of ``others`` at the same
    place (``separates``); else the lowest one whose siblings, two or more of
    them, hold an element at that place (``siblings``). The climb stops at an
    element that holds another of ``others`` where ``n`` sits (a row of records)
    and at the document levels. ``(None, "")`` when there is none."""
    from anyuse.execution.html_extract import MIN_ROW_TEXT
    size = text_len or (lambda x: len(x.text()))
    lone = None
    x = n
    for _ in range(max_up):
        if x is None or not x.tag or x.tag in _NO_RECORD:
            break
        path = _inner_path(n, x)
        loose = _loose(path)
        if any(o is not n and _contains(x, o) and _loose(_inner_path(o, x)) == loose
               for o in others):
            break
        p = x.parent
        if p is not None and p.tag:
            sig = _rsig(x)
            sibs = [c for c in p.children if c.tag and _rsig(c) == sig and size(c) >= MIN_ROW_TEXT]
            if len(sibs) >= 2 and any(s is x for s in sibs):
                if any(s is not x and _contains(s, o) and _inner_path(o, s) == path
                       for s in sibs for o in others):
                    return x, "separates"
                if lone is None and sum(1 for s in sibs if s is not x and _holds_path(s, path)) >= 2:
                    lone = x
        x = p
    return (lone, "siblings") if lone is not None else (None, "")


def record_units(nodes: Sequence[Any], depth: Optional[int] = None,
                 max_up: int = RECORD_MAX_UP) -> RecordUnits:
    """The repeated record (a result card, a row) each of ``nodes`` (parsed
    ``html_extract`` nodes of one page) sits in, read from the page's structure.

    1. The record level is the most common ``(signature, parent's signature)`` of
       the nodes' own records (:func:`_own_record`); a tie goes to the level
       nearer the document root, the record that holds the others. With a
       calibrated ``depth`` (levels from a node up to its record) it is the most
       common level ``depth`` above the nodes.
    2. Each node is placed in its lowest ancestor of that level; a node with none
       has no record (id None).
    3. A node is primary when its path inside its record is the one most nodes
       with a record of their own sit at.
    4. A record that holds two primaries is records merged: its nodes keep ids of
       their own. Every other record's nodes share one id.

    No level found: every id is None (no opinion)."""
    nodes = list(nodes)
    k = len(nodes)
    none = RecordUnits([None] * k, [None] * k, [False] * k, "")
    if not nodes:
        return none
    sizes: Dict[int, int] = {}

    def text_len(x: Any) -> int:
        if id(x) not in sizes:
            sizes[id(x)] = len(x.text())
        return sizes[id(x)]

    if depth is not None:
        own = [_up(x, depth) for x in nodes]
        hows = ["depth"] * k
    else:
        found = [_own_record(x, [o for o in nodes if o is not x], max_up, text_len) for x in nodes]
        own = [r for r, _h in found]
        hows = [h for _r, h in found]
    levels: Dict[Tuple[str, str], List[int]] = {}
    for i, r in enumerate(own):
        if r is not None and r.parent is not None and r.parent.tag:
            levels.setdefault((_rsig(r), _rsig(r.parent)), []).append(i)
    if not levels:
        return none
    level = min(levels, key=lambda lv: (-len(levels[lv]),
                                        sum(_root_depth(own[i]) for i in levels[lv]) / len(levels[lv])))

    def place(x: Any) -> Any:
        cur = x
        for _ in range(max_up + 1):
            if cur is None or not cur.tag or cur.parent is None:
                return None
            if (_rsig(cur), _rsig(cur.parent)) == level:
                return cur
            cur = cur.parent
        return None

    recs = [place(x) for x in nodes]
    paths = [_inner_path(x, r) if r is not None else None for x, r in zip(nodes, recs)]
    basis = [p for p, o, r in zip(paths, own, recs) if p is not None and o is not None and o is r] \
        or [p for p in paths if p is not None]
    counts: Dict[Tuple[str, ...], int] = {}
    for p in basis:
        counts[p] = counts.get(p, 0) + 1
    common = max(counts, key=lambda p: (counts[p], -basis.index(p))) if counts else None
    primary = [p is not None and p == common for p in paths]
    members: Dict[int, List[int]] = {}
    for i, r in enumerate(recs):
        if r is not None:
            members.setdefault(id(r), []).append(i)
    ids: List[Optional[int]] = [None] * k
    nxt = 0
    for i, r in enumerate(recs):
        if r is None or ids[i] is not None:
            continue
        group = members[id(r)]
        if sum(1 for j in group if primary[j]) >= 2:
            for j in group:
                ids[j], nxt = nxt, nxt + 1
        else:
            for j in group:
                ids[j] = nxt
            nxt += 1
    found_by = {hows[i] for i in levels[level]}
    how = next((h for h in ("depth", "separates", "siblings") if h in found_by), "")
    return RecordUnits(ids, recs, primary, how)


# ---------------------------------------------------------------------------
# empty: query-bound completion evidence
# ---------------------------------------------------------------------------

def _names_all(text: str, value: str, reading: Optional[QM.Reading] = None) -> bool:
    """Does ``text`` name every word of ``value`` (``query_match.names``)?"""
    return QM.names(text, value, reading)


#: A word that opens a condition, or a help instruction, in the sentence before a
#: "no results" phrase: "If no results for kafka, contact us", "Wenn Sie zu kafka
#: keine Ergebnisse finden, ...", "Если по запросу «Москва» ничего не найдено,
#: напишите нам", "如果没有找到相关结果，请联系我们", "Contact us about no results
#: for kafka". The phrase then says what to do when a search finds nothing, not
#: that this search found nothing. An instruction is read with its object ("contact
#: us", "versuchen Sie"), so a menu's bare "Contact" or "Try Pro" is not one. An
#: Arabic or Hebrew conjunction is written against the word after it ("وإذا",
#: "ואם"). The Spanish "se" and Italian "si" before a past "found" are the
#: statement's own reflexive ("Se encontraron 0 resultados", "Non si è trovato
#: nessun risultato"), and "もしくは" ("or") and "もしかして" ("did you mean") are
#: not "もし" ("if").
_CONDITION = re.compile(
    r"(?i)(?<!\w)(?:if|when|whenever|unless|in (?:the )?case|should (?:you|there|your)|wenn|falls"
    r"|sofern|sollten?|s['’]ils?|als|quand|lorsque|cuando|quando|nel caso|qualora|если|когда"
    r"|в случае|εάν|εαν|αν|όταν|οταν"
    r"|[وف]?(?:إذا|اذا|لو|عندما)|ו?(?:אם|כאשר)"
    r"|contact (?:us|our|the)|try (?:again|another|a different|a shorter|a broader|other|searching)"
    r"|check (?:the|your) spelling|tell us|let us know|report (?:it|this|a|the)"
    r"|(?:kontaktieren|versuchen|prüfen|überprüfen|melden) sie|schreiben sie uns"
    r"|contactez-nous|essayez|vérifiez|signalez|cont[áa]ctenos|escr[íi]banos|intente|pruebe"
    r"|compruebe|contattaci|scrivici|напишите нам|свяжитесь с нами|попробуйте|проверьте"
    r"|сообщите нам)(?!\w)"
    r"|(?<!\w)si(?!\w)(?!\s(?:è|sono)\s)"
    r"|(?<!\w)se(?!\w)(?!\s(?:encontr(?:aron|ó)|han? encontrado|hallaron|obtuvieron)(?!\w))"
    r"|如果|假如|要是|倘若|若(?![干い者手く])|万一|萬一|もし(?!くは|かして)|만약|만일")
#: a Chinese condition that closes after the phrase: "没有结果时" ("when there are no
#: results"), "没有结果的话" ("if there are no results")
_CONDITION_AFTER = re.compile(r"\s?(?:的?[时時](?![间間])|的[话話])")
#: a suggestion after the phrase opens a question of its own: "No results for kafka -
#: did you mean kafkaa?" states that the search found nothing
_SUGGESTION = re.compile(
    r"(?i)did you mean|meinten sie|vouliez-vous dire|quisiste decir|quiso decir|intendevi"
    r"|forse cercavi|вы имели в виду|もしかして|是不是要找|是否要找")
#: how far before a "no results" phrase, in its sentence, a condition is looked for
CONDITION_REACH = 80
#: how far after a "no results" phrase, in its clause, a question mark makes it a question
QUESTION_REACH = 60
#: where a sentence ends for a condition: a line of its own ends it too
_SENTENCE_OR_LINE_END = re.compile(r"[.!?。！？\n]")
#: where a clause ends
_CLAUSE_END = re.compile(r"[.!?。！？,，、;；:：،؟\u037e\n]")
#: the marks that end a clause as a question (U+037E is the Greek one)
_QUESTION = frozenset("?？؟\u037e")


def _greek(text: str) -> bool:
    """Does ``text`` hold a Greek letter?"""
    return any("\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff" for ch in text)


def _stated(text: str, m: Any, value: str = "") -> bool:
    """Does the "no results" phrase ``m`` in ``text`` state that a search found
    nothing? Not when its sentence opens a condition or a help instruction before
    it (:data:`_CONDITION`; the query's own words are not read as one), or a
    Chinese condition closes right after it (:data:`_CONDITION_AFTER`), and not
    when its clause ends in a question mark ("No results for kafka? Contact us",
    "검색 결과가 없어요?"); a suggestion's question ("did you mean x?") is its own.
    Greek writes its question mark as ";". The query is taken out of both sides
    first: its own punctuation ("node.js", "what is x?") neither ends the sentence
    nor asks."""
    lo = max(0, m.start() - CONDITION_REACH)
    window = text[lo:m.start()]
    if lo > 0 and text[lo - 1].isalnum():
        # the reach cut a word: its tail is not a word of its own
        window = re.sub(r"^\w+", "", window)
    after = text[m.end():m.end() + QUESTION_REACH]
    if value:
        window = re.sub(re.escape(value), " ", window, flags=re.I)
        after = re.sub(re.escape(value), " ", after, flags=re.I)
    if _CONDITION.search(_SENTENCE_OR_LINE_END.split(window)[-1]) or _CONDITION_AFTER.match(after):
        return False
    suggestion = _SUGGESTION.search(after)
    if suggestion:
        after = after[:suggestion.start()]
    end = _CLAUSE_END.search(after)
    if end is None:
        return True
    mark = end.group(0)
    return not (mark in _QUESTION or (mark == ";" and _greek(m.group(0))))


def _statements(text: str, value: str = "") -> List[Any]:
    """The "no results" phrases in ``text`` that state it (:func:`_stated`)."""
    return [m for m in _NO_RESULTS.finditer(text) if _stated(text, m, value)]


def _reports_error(text: str, value: str = "") -> Optional[Any]:
    """The first error phrase of :data:`_ERROR_PAGE` that ``text`` states: not an
    instruction itself ("try again later" opens :data:`_CONDITION`), and not in a
    condition or a question (:func:`_stated`: "If something went wrong, write to
    us", "Something went wrong? Contact us"), which are help text."""
    for m in _ERROR_PAGE.finditer(text or ""):
        if _CONDITION.match(m.group(0)) or not _stated(text, m, value):
            continue
        return m
    return None


def empty_evidence_text(text: str, value: str, stated_only: bool = True, *,
                        reading: Optional[QM.Reading] = None,
                        check_errors: bool = True) -> Tuple[str, str]:
    """``(phrase, why)`` when ``text`` states "no results" for ``value``; else
    ``("", why_not)``. ``stated_only=False`` also takes a phrase in a condition or
    a question: :func:`empty_evidence_page` reads those per line, where a menu
    above the statement is not in its sentence. A line break in ``text`` ends
    the sentence a condition is looked for in. ``reading``: where in a longer
    word of an unspaced script the query may stand (``query_match``; every
    place without one). ``check_errors=False``: the caller has judged the
    page's error lines itself (:func:`empty_verdict`)."""
    text = re.sub(r"\s+", lambda w: "\n" if "\n" in w.group(0) else " ", text or "")
    if not value:
        return "", "no query value to bind a 'no results' statement to"
    err = _reports_error(text, value) if check_errors else None
    if err:
        return "", f"the page reports an error ({err.group(0)!r}), not an empty result"
    seen = []
    for m in (_statements(text, value) if stated_only else _NO_RESULTS.finditer(text)):
        lo, hi = max(0, m.start() - EMPTY_WINDOW), min(len(text), m.end() + EMPTY_WINDOW)
        if _names_all(text[lo:hi], value, reading):
            return m.group(0), (f"the page states {m.group(0)!r} with the query {value!r} "
                                f"named within {EMPTY_WINDOW} characters")
        seen.append(m.group(0))
    if seen:
        return "", (f"the page says {seen[0]!r} but not next to the query {value!r} "
                    f"(not query-bound)")
    asked = _NO_RESULTS.search(text)
    if asked:
        return "", (f"the page says {asked.group(0)!r} only in a condition, a help instruction "
                    f"or a question, not as a statement that this search found nothing")
    return "", "the page has no 'no results' statement"


def empty_evidence_html(markup: str, value: str, root: Any = None) -> Tuple[str, str]:
    """A raw response's statement: the line rule of :func:`empty_evidence_page`,
    so a statement that names another term than the query is not taken because
    the query stands within a few characters of it (a heading, a summary)."""
    return empty_evidence_page(markup, value, text="", root=root)


#: elements whose text continues the line they sit in
_INLINE_TAGS = frozenset(
    "a abbr b bdi bdo br cite code data dfn em font i img input kbd label mark nobr q s samp "
    "small span strike strong sub sup time tt u var wbr".split())
_NO_TEXT_TAGS = frozenset(("script", "style", "noscript", "template", "head", "title"))


def visible_lines(root: Any) -> List[Tuple[str, Any, Any]]:
    """``(text, block, heading)`` for each line of a parsed page, in document order.

    A line is what a browser shows on one line: the text of a block element
    and of the inline elements inside it. A block nested in another starts a
    line of its own, and the enclosing block's text after it starts the next.
    ``block`` is the element the line belongs to; ``heading`` is the h1-h6
    element when the line is a heading's, else ``None``.
    """
    lines: List[Tuple[str, Any, Any]] = []
    buf: List[str] = []

    def flush(block: Any, heading: Any) -> None:
        text = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if text:
            lines.append((text, block, heading))
        buf.clear()

    def walk(node: Any, heading: Any) -> None:
        chunks = list(getattr(node, "_text", []) or [])
        at = list(getattr(node, "_text_at", []) or [])
        if len(at) != len(chunks):
            at = [0] * len(chunks)
        ti = 0
        for ci, child in enumerate(node.children):
            while ti < len(chunks) and at[ti] <= ci:
                buf.append(chunks[ti])
                ti += 1
            if child.tag in _NO_TEXT_TAGS:
                continue
            if child.tag in _INLINE_TAGS:
                walk(child, heading)
                continue
            inner = heading if heading is not None else (
                child if child.tag in _HEADING_TAGS else None)
            flush(node, heading)
            walk(child, inner)
            flush(child, inner)
        buf.extend(chunks[ti:])

    walk(root, None)
    flush(root, None)
    return lines


#: A line that reads as a search's summary of what it looked for: "Showing
#: results for x", "Search results for x", "Results for x", "You searched for x",
#: and the same in a few languages. The line starts with it, or, in Japanese,
#: Chinese and Korean, with the query it names ("「東京」の検索結果"). A summary
#: word followed by a particle or a negation is a statement, not a summary
#: ("検索結果はありません", "검색 결과가 없습니다").
_SEARCH_SUMMARY = re.compile(
    r"(?i)^\W*(?:(?:showing )?(?:search )?results? (?:for|matching)\b|you searched for\b"
    r"|(?:such)?ergebnisse f(?:ü|ue)r\b|sie suchten nach\b"
    r"|r[ée]sultats (?:de (?:la )?recherche )?pour\b"
    r"|resultados (?:de (?:la )?b[úu]squeda )?(?:para|de)\b|risultati (?:della ricerca )?per\b"
    r"|результаты (?:поиска )?(?:по запросу|для)\b|результаты поиска\b|вы искали\b|поиск по запросу\b"
    r"|αποτελ[εέ]σματα (?:αναζ[ηή]τησης )?για\b|αποτελ[εέ]σματα αναζ[ηή]τησης\b"
    r"|نتائج البحث\b|نتائج (?:عن|ل)\b|بحثت عن\b"
    r"|תוצאות (?:ה)?חיפוש\b|תוצאות עבור\b|חיפשת\b"
    r"|(?:検索結果|(?:搜索|搜尋|[检檢]索)[结結]果|검색\s?결과)(?!\s?(?:は|が|가|이|[无無]|없))"
    r"|.{1,60}?(?:の検索結果|的(?:搜索|搜尋)[结結]果|(?:에\s?대한\s?|의\s?)?검색\s?결과)"
    r"(?!\s?(?:は|が|가|이|없)))")
#: how many lines below a search summary its "no results" statement may stand
SUMMARY_LINES = 3
#: a longer line is a paragraph, not a search summary
SUMMARY_MAX = 160
#: not "your search", "your search term" or a translation of them after a
#: preposition: that names the search itself, not a subject of its own ("Zu Ihrem
#: Suchbegriff wurden keine Ergebnisse gefunden"). A word for a term counts only
#: after "your" or "this" ("for the term x" names x), and no noun counts when a
#: quoted term follows it ("for search “x”")
_NOT_THE_SEARCH = (
    r"(?!\W*(?:(?:your|this|that|the|ihr(?:e[mnrs]?)?|dein(?:e[mnrs]?)?|diese[mnrs]?|votre|cette"
    r"|su|tu|esta|questa|la (?:tua|sua)) )?(?:search(?! terms?\b)|query|such(?:e|anfrage)|anfrage"
    r"|recherche|requ[êe]te|b[úu]squeda|consulta|ricerca)\b(?!\s*:?\s*[\"'“”„«»‘’])"
    r"|\W*(?:your|this|ihr(?:e[mnrs]?)?|dein(?:e[mnrs]?)?|diese[mnrs]?|votre|vos|ce|cette|su|sus"
    r"|tu|tus|este|esta|questo|questa|il (?:tuo|suo)|la (?:tua|sua)) (?:search terms?|keywords?"
    r"|terms?|such(?:begriffe?|worte?)|begriffe?|stichw(?:orte?|örter)|termes?(?: de recherche)?"
    r"|mots?[- ]cl[ée]s?|t[ée]rminos?(?: de b[úu]squeda)?|palabras? clave|termini?(?: di ricerca)?"
    r"|parol[ae] chiave)\b(?!\s*:?\s*[\"'“”„«»‘’]))")
#: what follows a "no results" phrase that names the statement's own subject, in
#: the same sentence: at most two words, then "for", "match" or a preposition of
#: the summary's languages, then a word ("No results found for kafka", "No results
#: match kafka", "Keine Treffer zu kafka"). "For your search" and its translations
#: name the search itself.
_OWN_SUBJECT = re.compile(
    r"(?i)^[^\w.!?]*(?:\w+ ){0,2}?(?:for|matching|match(?:es|ed)?|f(?:ü|ue)r|zu|pour|à|para"
    r"|per)\b" + _NOT_THE_SEARCH + r"\W*\w")
#: the same subject named before the phrase, or inside it, in its sentence: "Your
#: search for kafka did not match any documents", "Ihre Suche nach kafka ergab keine
#: Treffer", "Zu kafka wurden keine Ergebnisse gefunden"
_SUBJECT_BEFORE = re.compile(
    r"(?i)\b(?:for|f(?:ü|ue)r|nach|zu|pour|para|per)\b" + _NOT_THE_SEARCH + r"\W*\w")


#: what a Russian, Greek, Arabic, Hebrew, Japanese, Chinese or Korean "no results"
#: sentence says beside its statement without naming a subject of its own: an
#: apology, "for your search", "matching your conditions", and the results the
#: statement is about ("匹配的结果")
_GENERIC_AROUND = re.compile(
    r"(?i)к сожалению|извините|по (?:вашему|этому|данному) запросу"
    r"|συγγν[ωώ]μη|δυστυχ[ωώ]ς|για (?:την|τη) αναζ[ηή]τησ[ηή] σας"
    r"|عذر[اً]+|للأسف|(?:ل|عن )?(?:بحثك|طلبك)"
    r"|מצטערים|לצערנו|(?:עבור |ל)?החיפוש שלך|לחיפוש שלך"
    r"|申し訳(?:ございません|ありません)(?:が|でした)?|すみません|残念ながら|お探しの|ご指定の"
    r"|(?:検索)?条件に|キーワードに"
    r"|(?:很)?抱歉|[对對]不起|[与與]?您的?(?:搜索|搜尋)?(?:[条條]件)?"
    r"|(?:相[关關]|匹配|相符|符合(?:[条條]件)?)的?(?:搜索|搜尋)?(?:[结結]果|商品|[内內]容|[产產]品|[记記][录錄])"
    r"|죄송합니다|죄송하지만|요청하신|입력하신|검색어(?:에|와)?(?:\s?대한)?")
#: a sentence ends here
_SENTENCE_END = re.compile(r"[.!?。！？]")


def _latin(text: str) -> bool:
    """Are all of ``text``'s letters Latin (basic, accented, extended)?"""
    return all(not ch.isalpha() or ord(ch) <= 0x24F or 0x1E00 <= ord(ch) <= 0x1EFF for ch in text)


def _names_own_subject(line: str, m: Any) -> bool:
    """Does the sentence of a "no results" statement in a non-Latin script name a
    subject of its own? Those languages put the subject before the statement as
    often as after it ("По запросу «кафка» ничего не найдено", "「カフカ」に一致する
    商品は見つかりませんでした"), so the rest of the statement's sentence, on both
    sides, must hold nothing but :data:`_GENERIC_AROUND` and punctuation."""
    before = _SENTENCE_END.split(line[:m.start()])[-1]
    after = _SENTENCE_END.split(line[m.end():])[0]
    rest = _GENERIC_AROUND.sub(" ", f"{before} {after}")
    return any(ch.isalnum() for ch in rest)


def _about_own_subject(line: str, m: Any) -> bool:
    """Does the sentence of the "no results" phrase ``m`` name a subject of its own?
    It is then about that subject, not about the query a heading or a search
    summary above it names. A Latin statement names it after the phrase ("No
    results found for kafka") or before it ("Your search for kafka did not
    match any documents"); a statement in another script, with any word in its
    sentence that is not :data:`_GENERIC_AROUND` (:func:`_names_own_subject`)."""
    if _OWN_SUBJECT.match(line[m.end():]):
        return True
    if not _latin(m.group(0)):
        return _names_own_subject(line, m)
    before = _SENTENCE_END.split(line[:m.start()])[-1]
    return bool(_SUBJECT_BEFORE.search(before + m.group(0)))


def _links_in_line(block: Any) -> bool:
    """The line ``block`` shows holds a link with text: an ``a`` among its inline
    content, or an ``a`` around ``block`` (a card whose link wraps its lines)."""
    cur = block
    while cur is not None:
        if cur.tag == "a":
            return True
        cur = cur.parent
    stack = list(getattr(block, "children", None) or [])
    while stack:
        n = stack.pop()
        if n.tag == "a" and _has_alnum(n.text()):
            return True
        if n.tag in _INLINE_TAGS:
            stack.extend(n.children)
    return False


def _summary_above(lines: Sequence[Tuple[str, Any, Any]], i: int, value: str,
                   on_screen, reading: Optional[QM.Reading] = None) -> Tuple[str, int]:
    """``(summary, distance)`` when a search-summary line naming the query stands at
    most :data:`SUMMARY_LINES` lines above the statement on line ``i``; else ``("", 0)``.

    The nearest summary-like line decides. The lines between them are page
    controls ("Sort by name"), not results: no line from the summary down to the
    statement may hold a link, and no line between them may name the query. A
    result row links to its result and names the query, so a summary above the
    rows does not bind a help box below them. A statement that names a subject
    of its own (:func:`_about_own_subject`) is about that subject. Lines read
    from the browser's text alone carry no markup to check for links, so there
    only the line right above counts."""
    line, block, _h = lines[i]
    found = _statements(line, value)
    if not found or _about_own_subject(line, found[0]):
        return "", 0
    reach = SUMMARY_LINES if block is not None else 1
    for j in range(i - 1, max(-1, i - 1 - reach), -1):
        text, b, _hj = lines[j]
        if b is not None and _links_in_line(b):
            return "", 0
        if _SEARCH_SUMMARY.match(text):
            if len(text) <= SUMMARY_MAX and _names_all(text, value, reading) and on_screen(text):
                return text, i - j
            return "", 0
        if _names_all(text, value):
            return "", 0
    return "", 0


def _in_section(heading: Any, block: Any) -> bool:
    """``block`` sits in the section ``heading`` opens: inside the heading's parent.
    A result card's own title is not the heading of a sentence after the card."""
    box, cur = getattr(heading, "parent", None), block
    while box is not None and cur is not None:
        if cur is box:
            return True
        cur = cur.parent
    return False


def empty_evidence_page(markup: str, value: str, text: str = "",
                        root: Any = None, *, reading: Optional[QM.Reading] = None
                        ) -> Tuple[str, str]:
    """``(phrase, why)`` for a rendered page that extracted nothing; else ``("", why_not)``.

    The browser replay's empty evidence. The visible text must pass
    :func:`empty_evidence_text`, and the statement must also name the query in
    its own line, or stand directly under the heading of its section and that
    heading names the query, or stand a few lines below a search summary that
    names it ("Showing results for x", then "No results found.") with no link
    in between (:func:`_summary_above`). Under a heading or a summary, a
    statement that names a subject of its own is about that subject
    (:func:`_about_own_subject`). A phrase in a condition, a help instruction
    or a question states nothing (:func:`_stated`). On a page whose selector broke, the
    rows it no longer matches still name the query. The character window around
    a footer or help-box sentence can reach those rows; the sentence's own line
    does not, and the rows' links keep a summary above them from reaching it.

    ``text`` is what the browser shows (``innerText``); without it the
    markup's visible text is read. Lines come from the markup's visible blocks,
    and with ``text`` a line counts only when ``text`` holds it: markup hidden
    by a stylesheet class stays in the DOM but is not on screen. When the
    markup holds no such statement (one drawn outside the serialised DOM),
    the lines of ``text`` are read instead. ``reading``: where in a longer word
    of an unspaced script the query may stand (``query_match``; every place
    without one).
    """
    phrase, why, _line = _page_statement(markup, value, text=text, root=root, reading=reading)
    return phrase, why


def _page_statement(markup: str, value: str, text: str = "", root: Any = None, *,
                    reading: Optional[QM.Reading] = None,
                    check_errors: bool = True) -> Tuple[str, str, str]:
    """:func:`empty_evidence_page`, with the page line the statement stands in.
    ``check_errors=False``: the caller has judged the page's error lines itself."""
    from anyuse.execution.html_extract import parse
    try:
        root = root if root is not None else parse(markup or "", visible_only=True)
    except ValueError as exc:
        return "", f"HTML parser refused input: {exc}", ""
    lines = visible_lines(root)
    shown = text if (text or "").strip() else "\n".join(t for t, _b, _h in lines)
    phrase, why = empty_evidence_text(shown, value, stated_only=False, reading=reading,
                                      check_errors=check_errors)
    if not phrase:
        return "", why, ""
    # whitespace dropped: the parser and innerText space inline text differently
    screen = re.sub(r"\s+", "", text).casefold() if (text or "").strip() else ""

    def on_screen(line: str) -> bool:
        return not screen or re.sub(r"\s+", "", line).casefold() in screen

    stated = [i for i, (t, _b, _h) in enumerate(lines) if _statements(t, value) and on_screen(t)]
    if not stated:
        lines = [(ln.strip(), None, None) for ln in (text or "").splitlines() if ln.strip()]
        stated = [i for i, (t, _b, _h) in enumerate(lines) if _statements(t, value)]
    if not stated:
        return "", (f"the page says {phrase!r} only in a condition, a help instruction or a "
                    f"question, not as a statement that this search found nothing"), ""
    for i in stated:
        line, block, _h = lines[i]
        own, _why = empty_evidence_text(line, value, reading=reading, check_errors=check_errors)
        if own:
            return own, (f"the page states {line[:120]!r}, which names the query {value!r}"), line
        above, _b, heading = lines[i - 1] if i > 0 else ("", None, None)
        m = _statements(line, value)[0]
        said = m.group(0)
        if heading is not None and _in_section(heading, block) \
                and _names_all(above, value, reading) \
                and on_screen(above) and not _about_own_subject(line, m):
            return said, (f"the page states {said!r} under the heading {above[:80]!r}, "
                          f"which names the query {value!r}"), line
        summary, distance = _summary_above(lines, i, value, on_screen, reading)
        if summary:
            return said, (f"the page states {said!r} {distance} line(s) below the search "
                          f"summary {summary[:80]!r}, which names the query {value!r}"), line
    return "", (f"the page says {phrase!r}, but that line does not name the query {value!r}, "
                f"and neither does a heading right above it or a search summary a few "
                f"lines above it"), ""


def empty_evidence_json(data: Any, list_node: Any, value: str) -> Tuple[str, str]:
    """An existing EMPTY list, a zero count stated beside it, and the query
    echoed somewhere in the response."""
    from anyuse.execution.api_compile import normalise
    if not isinstance(list_node, list) or list_node:
        return "", "the result path is not an empty list"
    zero = ""
    stack = [data]
    while stack and not zero:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    stack.append(v)
                elif re.sub(r"[^a-z]", "", str(k).lower()) in {re.sub(r"[^a-z]", "", c)
                                                                for c in _COUNT_KEYS} \
                        and v == 0 and not isinstance(v, bool):
                    zero = f"{k}=0"
        elif isinstance(node, list):
            stack.extend(x for x in node if isinstance(x, (dict, list)))
    if not zero:
        return "", "the response states no zero result count"
    want = normalise(value)
    echoed = False
    stack = [data]
    while stack and not echoed:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and want and normalise(node) == want:
            echoed = True
    if not echoed:
        return "", f"the response states {zero} but does not echo the query {value!r}"
    return zero, f"the response states {zero} and echoes the query {value!r}"


# ---------------------------------------------------------------------------
# the runtime check
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    ok: bool
    kind: str                 # "list" | "sparse" | "empty" | "refused"
    reason: str
    items: int = 0
    empty_state: str = ""
    region_coverage: Optional[float] = None
    record_depth: Optional[int] = None
    key_signature: str = ""
    max_keys_per_item: Optional[int] = None
    #: which step of the empty-answer cascade accepted an empty answer
    #: (``site_empty_page``, ``model_check``, ``page_statement``)
    empty_by: str = ""
    #: the cascade's requests and model calls, and what it learned
    empty_detail: Dict[str, Any] = field(default_factory=dict)
    #: the check model's requests for sparse keys that hold the query inside a longer word
    model_calls: int = 0
    #: the most matched items one result record held (:func:`record_units`); None
    #: when the page shows no record structure
    items_per_record: Optional[int] = None
    #: with the proved one-item-per-record rule: the indices of the items kept, one
    #: per record, and how many items in a record beside it were left out
    keep: Optional[List[int]] = None
    collapsed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        out = {"ok": self.ok, "kind": self.kind, "reason": self.reason, "items": self.items}
        if self.model_calls:
            out["model_calls"] = self.model_calls
        if self.empty_state:
            out["empty_state"] = self.empty_state
        if self.empty_by:
            out["empty_by"] = self.empty_by
        if self.empty_detail:
            out["empty_detail"] = dict(self.empty_detail)
        if self.region_coverage is not None:
            out["region_coverage"] = round(self.region_coverage, 3)
        if self.record_depth is not None:
            out["record_depth"] = self.record_depth
        if self.key_signature:
            out["key_signature"] = self.key_signature
            out["max_keys_per_item"] = self.max_keys_per_item
        if self.items_per_record is not None:
            out["max_items_per_record"] = self.items_per_record
        if self.keep is not None:
            out["collapsed"] = self.collapsed
        return out


def _value(contract: Dict[str, Any], params: Optional[Dict[str, Any]]) -> str:
    params = params or {}
    name = str(contract.get("query_param") or "")
    if name and name in params:
        return str(params[name])
    if len(params) == 1:
        return str(next(iter(params.values())))
    return ""


def _judge(contract, n, keys, value, cov, empty, reading: Optional[QM.Reading] = None
           ) -> Verdict:
    need = contract.get("region_coverage_min")
    if n == 0:
        from anyuse.execution import empty_page as EP
        ev = empty()
        if ev.state == EP.EMPTY:
            return Verdict(True, "empty", "empty result with query-bound completion evidence: "
                           + ev.why, 0, empty_state=ev.line, empty_by=ev.by,
                           empty_detail=ev.detail())
        return Verdict(False, "refused", (
            "the extraction returned nothing and there is no query-bound completion "
            f"evidence ({ev.why}); reported as a FAILURE, not as an empty result — the "
            "markup probably changed"), 0, empty_detail=ev.detail())
    if isinstance(need, (int, float)) and cov is not None and cov < need:
        return Verdict(False, "refused", (
            f"the extraction covers {cov:.2f} of the result region's records, "
            f"{need} proved: a partial list (the markup probably changed)"), n,
            region_coverage=cov)
    if n < MIN_LIST:
        if not value:
            return Verdict(False, "refused", "sparse result and no query value to check "
                           "the items' identity against", n, region_coverage=cov)
        bad, how = [], ""
        for k in keys:
            named, got = key_naming(k, value, reading)
            if not named:
                bad.append(k)
                how = how or got
        calls = reading.model_calls if reading is not None else 0
        if bad or len(keys) != n:
            shown = (bad[0] if bad else "?")[:80]
            what = (f"holds the query {value!r} only inside a longer word"
                    if QM.inside_a_word(how) else f"does not name the query {value!r}")
            return Verdict(False, "refused", (
                f"sparse result ({n} item(s)): the item key {shown!r} {what}; a small answer "
                f"is accepted only when every item's key names the query"), n,
                region_coverage=cov, model_calls=calls)
        return Verdict(True, "sparse", (
            f"sparse result ({n} item(s)): every item's key names {value!r}"
            + (" (the check model read it as a word where it stands)" if calls else "")), n,
            region_coverage=cov, model_calls=calls)
    return Verdict(True, "list", f"list of {n} item(s) (no count floor)", n, region_coverage=cov)


def check_html(contract: Dict[str, Any], markup: str, selector: str,
               params: Optional[Dict[str, Any]], items: Sequence[Any],
               visible_only: bool = False, root: Any = None,
               implied_end_tags: bool = False, *, empty: Any = None,
               fetch: Optional[Callable[[str], Any]] = None,
               positive_ok: Optional[Callable[[Any], bool]] = None,
               url: str = "") -> Verdict:
    """The contract's verdict on an HTML answer. ``root``: ``markup`` already
    parsed with the same ``visible_only`` and ``implied_end_tags``, so the page
    is not parsed again. ``empty``, ``fetch`` and ``positive_ok`` reach the
    empty-answer cascade (:func:`empty_verdict`); ``url``, the page's address,
    tells it which links search the site for another input. ``empty`` (a product
    call's context) also lets the check model read a sparse key that holds the
    query inside a longer word (``query_match``)."""
    from anyuse.execution.html_extract import parse, select
    value = _value(contract, params)
    try:
        root = root if root is not None else parse(markup, visible_only=visible_only,
                                                   implied_end_tags=implied_end_tags)
        # every match, not only the first HTML_LIMIT the executor returns: a
        # long list truncated by the executor is not a partial extraction
        every = [n for n in select(root, selector) if n.text()] if selector else []
    except ValueError as exc:
        return Verdict(False, "refused", f"HTML parser refused input: {exc}", len(items or []))
    nodes = every[:HTML_LIMIT]
    n = len(items or [])
    depth = contract.get("record_depth")
    depth = int(depth) if isinstance(depth, int) and not isinstance(depth, bool) else None
    recs, region, measured_depth = records_of(nodes[:n], depth)
    keyed = recs if len(recs) == n else nodes[:n]
    keys = [html_item_key(r) for r in keyed]
    all_recs, all_region, _d = records_of(every, depth if depth is not None else measured_depth)
    cov = region_coverage(all_recs, all_region)
    # merged records (a wrapper now matched as ONE item, a region's ``> *``
    # taking a new wrapper): an item spanning more records' keys than any
    # validation page showed is refused, whatever its size
    sigs = [signature(key_element(r)) for r in keyed]
    sig = contract.get("key_signature") or (max(set(sigs), key=sigs.count) if sigs else "")
    per_item = [keys_in(nd, sig) for nd in nodes[:n]] if sig else []
    most = max(per_item) if per_item else None
    allowed = contract.get("max_keys_per_item")
    if contract.get("key_signature") and isinstance(allowed, int) and most is not None \
            and most > allowed:
        return Verdict(False, "refused", (
            f"an extracted item holds {most} record keys ({sig}); the proof saw at most "
            f"{allowed} per item: records were merged (the page's segmentation changed)"),
            n, region_coverage=cov, record_depth=measured_depth, key_signature=sig,
            max_keys_per_item=most)
    # one result record holding more matched items than one (a card's ad or
    # variant link beside its title): measured on every page; with the proved
    # one-item-per-record rule, one item per record is kept
    units = record_units(nodes[:n], depth)
    per_record = units.most_per_record()
    keep: Optional[List[int]] = None
    if contract.get("one_item_per_record") and per_record is not None and per_record > 1:
        keep = _one_per_record(units, nodes[:n])
        kept = [nodes[i] for i in keep]
        recs, region, measured_depth = records_of(kept, depth)
        keyed = recs if len(recs) == len(kept) else kept
        keys = [html_item_key(r) for r in keyed]

    def empty_answer():
        from anyuse.execution import empty_page as EP
        page = EP.Page("html", value, markup=markup, implied_end_tags=implied_end_tags,
                       root=root if visible_only else None, url=str(url or ""))
        return empty_verdict(contract, page, root=root, ctx=empty, fetch=fetch,
                             positive_ok=positive_ok)

    count = len(keep) if keep is not None else n
    verdict = _judge(contract, count, keys, value, cov, empty_answer,
                     _sparse_reading(contract, empty, count))
    if empty is not None and verdict.model_calls:
        empty.model_calls += verdict.model_calls
    verdict.record_depth = measured_depth
    if sig and len(set(sigs)) <= 1:
        verdict.key_signature, verdict.max_keys_per_item = sig, most
    verdict.items_per_record = per_record
    if keep is not None:
        verdict.keep, verdict.collapsed = keep, n - len(keep)
    return verdict


def _one_per_record(units: RecordUnits, nodes: Sequence[Any]) -> List[int]:
    """The indices kept when each record answers one item: in a record of several,
    the node at the records' primary place (where the other records' items sit),
    else the one that is, holds or sits inside the record's key element (its
    title), else the first. A record whose key is a heading and that holds a
    second heading of its signature spans records and keeps every item."""
    groups: Dict[int, List[int]] = {}
    keep: List[int] = []
    for i, rid in enumerate(units.ids):
        if rid is None:
            keep.append(i)
        else:
            groups.setdefault(rid, []).append(i)
    for idx in groups.values():
        rec = units.records[idx[0]]
        ke = key_element(rec) if rec is not None else None
        if len(idx) == 1 or rec is None or ke is rec or (
                ke.tag in _HEADING_TAGS and keys_in(rec, signature(ke)) > 1):
            keep.extend(idx)
            continue
        lead = [i for i in idx if units.primary[i]]
        keep.append(lead[0] if len(lead) == 1 else next(
            (i for i in idx if _contains(nodes[i], ke) or _contains(ke, nodes[i])), idx[0]))
    return sorted(keep)


def check_json(contract: Dict[str, Any], data: Any, list_path: str,
               params: Optional[Dict[str, Any]], items: Any, *, empty: Any = None,
               fetch: Optional[Callable[[str], Any]] = None,
               positive_ok: Optional[Callable[[Any], bool]] = None) -> Verdict:
    value = _value(contract, params)
    seq = items if isinstance(items, list) else ([] if items is None else [items])
    keys = [json_item_key(i) for i in seq]

    def empty_answer():
        from anyuse.execution import empty_page as EP
        page = EP.Page("json", value, data=data, list_path=str(list_path or ""))
        return empty_verdict(contract, page, ctx=empty, fetch=fetch, positive_ok=positive_ok)

    verdict = _judge(contract, len(seq), keys, value, None, empty_answer,
                     _sparse_reading(contract, empty, len(seq)))
    if empty is not None and verdict.model_calls:
        empty.model_calls += verdict.model_calls
    return verdict


def _sparse_reading(contract: Dict[str, Any], ctx: Any, n: int) -> Optional[QM.Reading]:
    """How a sparse answer's keys are read (``query_match.for_call``)."""
    if not 0 < n < MIN_LIST:
        return None
    return QM.for_call(contract, ctx, where="result_contract.sparse")


# ---------------------------------------------------------------------------
# the empty-answer cascade
# ---------------------------------------------------------------------------

#: the ``model_check`` scopes in which the cascade's model steps may ask, with a context
#: from the caller: a product call (``run_compiled``) and the heal's question about the
#: page of a made-up search (``fresh_truth.query_bound``)
EMPTY_MODEL_SCOPES = ("product", "heal")


def _error_report(page: Any, sig: Optional[Dict[str, Any]], asks: bool
                  ) -> Tuple[bool, str, Any]:
    """``(model_may_ask, why, verdict)`` about the page's lines that report an error.

    Read only on the lines the site's result pages do not show too (the learned
    signature's shared lines are its chrome and help text), and only where an
    error phrase is stated (:func:`_reports_error`). ``why`` is the refusal when
    such a line is there, ``""`` when none is. When ``asks``, the check model
    judges each such line first (``empty_page.error_check``): a yes is the error
    and nothing more is asked; a no for every line lets the model step ask
    whether the page states that the search found nothing (``model_may_ask``).
    The phrase list stays the statement rule's last veto: an error line the
    model did not call an error still refuses that rule. ``verdict``: the
    model's, when it was asked."""
    from anyuse.execution import empty_page as EP
    seen: List[str] = []
    texts = [t for t, _b, _h in EP.visible(page)]
    texts += [ln.strip() for ln in (page.text or "").splitlines() if ln.strip()]
    for t in texts:
        # the phrase test first: it is cheaper than masking every line for the signature
        if t not in seen and _reports_error(t, page.value) and not EP.veto(sig, t, page.value):
            seen.append(t)
    if not seen:
        return True, "", None
    v = EP.error_check(page, seen) if asks else None
    if v is not None and v.yes and v.candidate is not None:
        line = v.candidate.text
        return False, (f"the page reports an error ({line[:80]!r}, read so by the check model), "
                       f"not an empty result"), v
    m = _reports_error(seen[0], page.value)
    why = f"the page reports an error ({m.group(0)!r}), not an empty result"
    return bool(v is not None and v.no), why, v


def empty_verdict(contract: Optional[Dict[str, Any]], page: Any, *, root: Any = None,
                  ctx: Any = None, fetch: Optional[Callable[[str], Any]] = None,
                  positive_ok: Optional[Callable[[Any], bool]] = None) -> Any:
    """Is the 0-item answer ``page`` (an ``empty_page.Page``) an empty result?
    Returns ``empty_page.Evidence`` with state EMPTY or not.

    1. The site's own empty page (``empty_page.match``): the record's signature
       for this template (``ctx.learned``), else the contract's pinned one, else
       one learned now (``empty_page.learn_now``, when ``ctx`` may learn and
       ``fetch`` is set). A page the site has changed since it was learned (a
       made-up search now lacks its lines) is learned again the same way. EMPTY
       or NOT_EMPTY ends the cascade.
    2. HTML, with a context and a scope of :data:`EMPTY_MODEL_SCOPES` open (a
       product call: ``run_compiled``; the heal's question about a made-up
       search): the configured check model. First about each line that reports
       an error (:func:`_error_report`; lines the site's result pages show too
       are its chrome and are not read): a yes refuses the page, and only a no
       for every such line lets it go on. Then about the lines that name the
       input (``empty_page.model_step``): a yes is EMPTY; a no for every line
       that could state it is NOT_EMPTY.
    3. The statement rule: :func:`_page_statement` (``root``: the caller's tree
       of the markup) or :func:`empty_evidence_json`. A line the site's result
       pages show too (the signature's ``shared`` lines) is help text. A stated
       error phrase on any other line refuses the page here (the phrase list
       is this rule's last veto). Nothing counts on a page that is not the
       site's empty page while a made-up search at the same moment still shows
       that page.

    Steps 2 and 3 accept nothing on a page with lines that read as result rows
    (``empty_page.unread_rows``: they name the input and hold a link). In step 3
    the statement names the query at the places the contract accepts
    (``query_match``); on a product call the check model may read a query that
    stands inside a longer word.

    ``fetch(value)`` sends one more search through the caller's executor; a
    caller that may not send any passes none. Requests and model calls are
    counted on the evidence and on ``ctx``."""
    from anyuse.execution import empty_page as EP
    from anyuse.execution import model_check as MC
    f = EP.Fetcher(fetch) if fetch is not None else None
    notes: List[str] = []
    model_calls, backend, p = 0, "", None

    def done(ev):
        ev.requests += f.requests if f is not None else 0
        ev.model_calls += model_calls
        if backend and not ev.backend:
            ev.backend, ev.p = backend, p
        if ctx is not None:
            ctx.requests += ev.requests
            ctx.model_calls += ev.model_calls
            if ev.learned is None and ctx.new_learned and EP.usable(ctx.new_learned) \
                    and not ctx.new_learned.get("retry_after"):
                ev.learned = ctx.new_learned
        return ev

    sig = None
    pinned = ((contract or {}).get("empty") or {}).get("page")
    may_learn = ctx is not None and ctx.learn and f is not None and positive_ok is not None
    if ctx is not None and EP.usable(ctx.learned):
        # the record's page for this very template: a tool proved before, or a pinned page
        # the site has since changed
        sig = ctx.learned
    elif EP.usable(pinned):
        sig = pinned
    elif may_learn:
        sig, refused = EP.learn_now(page, ctx, f, positive_ok)
        if refused is not None:
            return done(refused)
    not_site_page = False
    if sig is not None and sig.get("kind") == page.kind:
        positives = list(ctx.positives) if ctx is not None else []
        ev = EP.match(sig, page, fetch=f, positives=positives, positive_ok=positive_ok)
        if ev.stale and may_learn:
            # the site's empty page changed since it was learned: learn it again, on the record
            relearned, refused = EP.learn_now(page, ctx, f, positive_ok, again=sig)
            if refused is not None:
                return done(refused)
            if relearned is not None:
                sig = relearned
                ev = EP.match(sig, page, fetch=f, positives=positives, positive_ok=positive_ok)
        if ev.state != EP.UNKNOWN:
            return done(ev)
        notes.append(ev.why)
        not_site_page = ev.not_site_page
    elif sig is not None and sig.get("why"):
        notes.append(f"no learned empty page: {sig['why']}")

    if page.kind == "html":
        # lines that read as result rows (they name the input and hold a link): a page that
        # shows them extracted nothing because the selector no longer reads them
        rows = EP.unread_rows(page)
        # the caller hands over the context and opens the scope: run_compiled on a product
        # call, the heal around its one question about a made-up search
        asks = ctx is not None and MC.current_scope() in EMPTY_MODEL_SCOPES
        # rows the selector no longer reads refuse the page whatever the model says about
        # its error lines, so none is asked then
        may_ask, error_why, v = _error_report(page, sig, asks and not rows)
        if v is not None:
            model_calls = v.calls
            if v.backend and v.backend != "none":       # "none": no model was asked
                backend, p = v.backend, v.p
            if v.none and (v.calls or v.error):
                notes.append(f"the check model about the error lines: {v.why[:120]}")
        # a page with an error line the check model did not clear is refused below, whatever
        # a model would say about its lines, so none is asked whether it states that the
        # search found nothing
        if asks and not rows and may_ask:
            ev = EP.model_step(page, sig)
            model_calls += ev.model_calls
            if ev.backend:
                backend, p = ev.backend, ev.p
            if ev.state != EP.UNKNOWN:
                ev.model_calls = 0
                return done(ev)
            if ev.model_calls or ev.backend:
                notes.append(ev.why)
        reading = QM.for_call(contract, ctx, where="result_contract.empty")
        if error_why:
            phrase, why, line = "", error_why, ""
        else:
            phrase, why, line = _page_statement(page.markup, page.value, text=page.text,
                                                root=root, reading=reading, check_errors=False)
        model_calls += reading.model_calls
        backend = backend or reading.backend
        others = [r for r in rows if r != line]
        if phrase and others:
            phrase, why = "", (f"the page holds {len(others)} line(s) that name the input and "
                               f"link somewhere (first {others[0][:80]!r}): results the selector "
                               f"no longer reads")
        elif phrase and not_site_page:
            phrase, why = "", (f"the page states {phrase!r}, but it is not the site's own empty "
                               f"page, which a made-up search still shows")
        elif phrase and EP.veto(sig, line, page.value):
            phrase, why = "", (f"the line {line[:80]!r} also stands on the site's result pages: "
                               f"help text, not a statement that this search found nothing")
    else:
        from anyuse.execution.api_step import json_path
        listed = json_path(page.data, page.list_path) if page.list_path else page.data
        phrase, why = empty_evidence_json(page.data, listed, page.value)
    if phrase:
        return done(EP.Evidence(EP.EMPTY, line=phrase, why=why, by=EP.BY_STATEMENT))
    # no step found evidence: the statement rule's reason first, then the others'
    return done(EP.Evidence(EP.UNKNOWN, why="; ".join([why] + notes)[:600]))


def page_from_raw(template: Any, value: str, raw: Dict[str, Any]) -> Any:
    """An ``empty_page.Page`` from ``ApiStepExecutor.execute(..., _probe=True)``:
    the response read the way ``template`` reads it."""
    import json as _json
    from anyuse.execution import empty_page as EP
    from anyuse.execution.api_step import check_expect, check_expect_embedded, json_path
    kind = "html" if getattr(template, "extract_kind", "json") == "html" else "json"
    rules = bool(getattr(template, "implied_end_tags", False))
    expect = getattr(template, "expect", None) or {}
    list_path = str(expect.get("path") or "")
    status = raw.get("status") if isinstance(raw, dict) else None
    status = int(status) if isinstance(status, int) else 0
    url = str((raw or {}).get("url") or "")
    if not isinstance(raw, dict) or not raw.get("success"):
        definite = 400 <= status < 500 and status != 429 and not (raw or {}).get("blocked")
        return EP.Page(kind, value, ok=False, definite=definite, status=status, url=url,
                       error=str((raw or {}).get("error") or "no answer")[:200])
    text = str(raw.get("text") or "")
    if kind == "html":
        from anyuse.execution.html_extract import extract
        try:
            items = extract(text, str(template.extract.get("items") or ""), limit=HTML_LIMIT,
                            implied_end_tags=rules)
        except ValueError as exc:
            return EP.Page(kind, value, ok=False, definite=True, status=status, url=url,
                           error=f"HTML parser refused input: {exc}"[:200])
        return EP.Page(kind, value, markup=text, items=items, status=status, url=url,
                       implied_end_tags=rules)
    ctype = str(raw.get("content_type") or "")
    if template.extract_kind == "embedded_json":
        ok, data, reason = check_expect_embedded(status, ctype, text, template.embedded, expect)
    else:
        try:
            data = _json.loads(text)
        except (ValueError, RecursionError) as exc:
            return EP.Page(kind, value, ok=False, definite=True, status=status, url=url,
                           error=f"the answer is not JSON ({type(exc).__name__})")
        ok, reason = check_expect(status, ctype, data, expect)
    if not ok:
        return EP.Page(kind, value, ok=False, definite=True, status=status, url=url,
                       error=str(reason)[:200])
    got = json_path(data, str(template.extract.get("items") or "")) \
        if template.extract.get("items") else None
    items = got if isinstance(got, list) else ([] if got is None else [got])
    return EP.Page(kind, value, data=data, items=list(items), status=status, url=url,
                   list_path=list_path)


def positive_ok_for(contract: Optional[Dict[str, Any]], selector: str = "", list_path: str = "",
                    *, visible_only: bool = False, min_items: int = 1) -> Callable[[Any], bool]:
    """Does a page for an input known to have results pass the rung's own check?
    With a contract: its list or sparse verdict; without one: ``min_items`` items."""
    def ok(page: Any) -> bool:
        if not page.ok or not page.items:
            return False
        if contract is None:
            return len(page.items) >= max(1, int(min_items or 1))
        params = {str(contract.get("query_param") or "q"): page.value}
        if page.kind == "html":
            v = check_html(contract, page.markup, selector, params, page.items,
                           visible_only=visible_only, implied_end_tags=page.implied_end_tags)
        else:
            v = check_json(contract, page.data, list_path, params, page.items)
        return v.ok and v.kind in ("list", "sparse")
    return ok


# ---------------------------------------------------------------------------
# learning the empty page when a template is proved
# ---------------------------------------------------------------------------

_REQUEST_KEYS = ("method", "url", "query", "headers", "body", "path_params", "param_transforms")


def request_of(template: Any) -> Dict[str, Any]:
    """What a template sends (method, URL, query, headers, body, how it binds
    its input), without what it reads from the answer."""
    d = template.to_dict() if hasattr(template, "to_dict") else dict(template or {})
    return {k: d.get(k) for k in _REQUEST_KEYS}


def takes_probe(executor: Any) -> bool:
    """Can ``executor`` send a raw search (``execute(..., _probe=True)``)? The
    signature is inspected, so a TypeError inside the executor is never taken
    for a missing keyword."""
    import inspect
    try:
        return "_probe" in inspect.signature(executor.execute).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def probe_unsupported() -> Dict[str, Any]:
    """What a wrapper answers for a raw search its executor cannot send: no answer."""
    return {"success": False, "error": "this executor cannot send a raw search"}


def drop_empty_page(template: Any) -> bool:
    """Forget a template's learned empty page (its request changed)."""
    c = contract_of(template)
    empty = (c or {}).get("empty")
    if isinstance(empty, dict) and "page" in empty:
        del empty["page"]
        return True
    return False


def learn_empty_page(template: Any, proof: Any, executor: Any) -> Optional[Dict[str, Any]]:
    """Learn the site's empty page for a template that just passed its proof:
    :data:`empty_page.PROBES` searches for made-up inputs through ``executor``,
    compared with the proof's result pages. The signature is written into the
    contract (``empty.page``), so the proof's digest pins it; a learning that got
    no answer removes an older one. Nothing is sent when ``ANYUSE_EMPTY_PROBE``
    is off or ``executor`` cannot send a raw search (``execute(..., _probe=True)``)."""
    from anyuse.execution import empty_page as EP
    c = contract_of(template)
    if c is None or not supports(template) or not getattr(proof, "passed", False) \
            or not EP.probe_enabled() or not takes_probe(executor):
        return None
    pages = [p for p in getattr(proof, "pages", None) or [] if isinstance(p, dict)]
    qp = str(c.get("query_param") or _query_param(template))
    if not pages or not qp:
        return None
    kind = "html" if template.extract_kind == "html" else "json"
    rules = bool(getattr(template, "implied_end_tags", False))
    list_path = str((template.expect or {}).get("path") or "")
    results, values = [], []
    for p in pages:
        value = str((p.get("params") or {}).get(qp, ""))
        values.append(value)
        items = p.get("items") if isinstance(p.get("items"), list) else []
        if kind == "html":
            results.append(EP.Page("html", value, markup=str(p.get("data") or ""), items=items,
                                   implied_end_tags=rules))
        else:
            results.append(EP.Page("json", value, data=p.get("data"), items=items,
                                   list_path=list_path))
    base = dict(pages[0].get("params") or {})
    probes = [page_from_raw(template, v, executor.execute(template, {**base, qp: v}, _probe=True))
              for v in EP.nonsense_values(values, seed=str(getattr(template, "url", "")))]
    sig = EP.learn(kind, results, probes)
    if sig is None:
        drop_empty_page(template)
        return None
    sig.update({"learned": "proof", "at": EP._now()})
    c.setdefault("empty", {})["page"] = sig
    return sig


# ---------------------------------------------------------------------------
# upgrading a record proved before the contract existed
# ---------------------------------------------------------------------------

def upgrade_record(record: Dict[str, Any], validation_cases: Sequence[Dict[str, Any]],
                   executor=None, min_cases: int = 2) -> Tuple[Dict[str, Any], Any]:
    """Re-prove a stored record WITH the contract; return ``(new_record, proof)``.

    The template's request and extraction are unchanged; only the contract is
    added, and the result is a NEW proof pinned to the new digest. The
    validation cases (``[{"value", "ui_items"}]``) must be inputs the record
    never used for fitting or selection: the caller declares them, as the HAR
    path does. The new status is never stronger than the one it replaces
    (``proof_split.weaker``). On failure the returned record is the old one.
    """
    from anyuse.execution import proof_split as PS
    from anyuse.execution.api_step import load_command
    from anyuse.execution.api_verify import verify_template
    from anyuse.execution.workflow_schema import template_digest

    template = load_command(record.get("template") or {})
    if not attach(template):
        from anyuse.execution.api_verify import EquivalenceResult
        return record, EquivalenceResult(reason="this template kind has no result contract")
    proof = verify_template(template, list(validation_cases), executor, min_cases)
    split = PS.declared(None, list(validation_cases), min_validation=min_cases,
                        why="contract upgrade: validation inputs declared by the caller")
    split.finalize()
    proof.status = split.status(proof.passed)
    proof.split = split.to_dict(proof.passed_count)
    proof.split["compile"] = 1
    proof.scope = PS.scope_text({"status": proof.status, "split": proof.split,
                                 "case_count": len(proof.cases)}, "list")
    if not proof.passed:
        return record, proof
    finalize(template, proof)
    from anyuse.execution.api_step import ApiStepExecutor
    learn_empty_page(template, proof, executor or ApiStepExecutor())
    prior = record.get("proof") or {}
    prior_status = str(prior.get("status") or PS.PROVED) if prior.get("passed") else PS.NOT_PROVED
    status = PS.weaker(proof.effective_status, prior_status)
    new = copy.deepcopy(record)
    new["template"] = template.to_dict()
    new_proof = proof.to_dict()
    anchor = new_proof.pop("drift_anchor", None)
    from anyuse.execution.api_step import signed_in_template
    if isinstance(anchor, dict) and not new.get("drift_anchor") \
            and not signed_in_template(template):
        new["drift_anchor"] = anchor
    new_proof["status"] = status
    new_proof["template_digest"] = template_digest(new["template"])
    new_proof["upgraded_from"] = {k: prior.get(k) for k in ("status", "template_digest",
                                                             "passed_count", "case_count")}
    new["proof"] = new_proof
    new["contract_upgrade"] = {"validation_values": [str(c.get("value", ""))
                                                     for c in validation_cases]}
    return new, proof
