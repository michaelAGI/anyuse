"""Offline A/B bench: the browser replay rung with and without the runtime result contract.

The question: can a broken browser read be served as "it worked, no results",
or a small page of the wrong thing as an answer? Each case is a real saved
result page (or a synthetic hide-filter page), possibly with a drift applied,
replayed through ``ReplayExecutor`` with a fake driver. Nothing opens a socket,
starts a browser or calls a model.

Usage::

    python tests/bench/replay_contract_bench.py run --old <tree> --new <tree> \
        [--python <interpreter>] [--out <dir>]

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per tree with the same interpreter. Each run's
PYTHONPATH is a pkgroot directory holding an ``anyuse`` symlink to the tree,
then the tree, then its ``tools/guarded_tests`` (whose sitecustomize refuses
sockets). HOME and every AnyUse data path point into ``--out``. The runs write
``OLD.json`` and ``NEW.json``; ``compare`` prints the table.

Arms:

* ``OLD``: the old code. It reads the same template dicts; the keys it does not
  know (``visible_only``, ``expect.result_contract``) are ignored. Both template
  variants are run and must agree. An old tree that already has the contract
  (``result_contract.replay_contract``) is shown as ``OLD-legacy`` and
  ``OLD-contract``, like the new one.
* ``NEW-legacy``: the new code, template without ``result_contract`` (how a
  tool stored before the change behaves).
* ``NEW-contract``: the new code, template with it.

Case groups (gold in brackets):

* ``D0``: unmodified list and sparse pages, at floor 1 and at the site's
  realistic floor (the smallest count among its pages with 3 or more rows)
  [the extraction].
* ``D1``: the selector broken by a class rename, rows still on the page [fail].
* ``D2``: D1 plus a footer sentence ("No results found.", "0 results",
  "Nothing found", "No matches") [fail].
* ``D3``: D1 plus a sidebar sentence naming another query [fail].
* ``D4``: D1 plus an error banner naming the query [fail].
* ``D2-summary``: D1 plus a search summary naming the query right above the
  rows and a help-box sentence right after them [fail].
* ``D3-near``: rows removed; a search summary naming the query, then a
  statement naming another query, six phrasings in five languages [fail].
* ``D5``: rows removed, a statement naming the query; container kept or
  removed [empty success].
* ``S1``: another query's 1-2 row page served for this query [fail].
* ``V1``: a hide-filter page, template fitted visible-only [the visible rows].
* ``ODD``: msys2 "uuid" (an empty table, no statement) and cheese.com
  "roquefort" (a detail page) [fail].
* cost groups, not in the pass criterion (all are genuinely empty pages):
  ``D5-far`` (statement more than 160 characters from the query),
  ``D5-near`` (a search summary naming the query in the paragraph above the
  statement, four phrasings), ``D5-near-i18n`` (the same in German, French,
  Spanish and Italian), ``D5-near-gap`` (a sort line between them),
  ``D5-head`` (query in the heading above the statement), ``D5-noun``
  ("No matching crates for "x"", a noun outside the result nouns).
* ``CASCADE``: ``run_compiled`` on the crates.io record; the API answers an
  empty list with no echo (the contract refuses it); the browser serves D0, D2
  or D5; with a fake agent and without one.
* non-Latin pages (synthetic, ``I18N``: Japanese, Chinese, Korean, Russian,
  Greek, Arabic, Hebrew; three result rows naming the query, at the floor 3):
  ``I-list`` the result list [the extraction]; ``I-line`` rows removed, a statement
  naming the query on its line, ``I-head`` under a heading naming it,
  ``I-summary`` below a search summary naming it [empty success];
  ``I-other`` a statement naming another term, ``I-other-near`` the same below
  a summary naming the query, ``I-footer`` and ``I-help`` the selector broken
  by a class rename with a bare statement in the footer or a help box after the
  rows, ``I-summary-rows`` a summary above the broken rows and the help box
  below them, ``I-error`` an error sentence beside a statement naming the query,
  ``I-question`` a help line's question ("該当する商品が見つかりませんか？",
  "找不到商品？") under a heading or a summary naming the query, or naming it
  itself, with the rows' selector broken [fail].
* conditions, help instructions and questions (``HELP_LINES``, ``I18N_HELP``):
  ``C-help`` on the saved pages, a line naming the query in which "no results"
  is a condition ("If no results for x, contact us"), a help instruction or a
  question, in English, German, French, Spanish and Italian: in a help box
  after the rows with their selector broken, or with the rows removed on its
  own, under a heading or below a search summary naming the query [fail];
  ``IC-help`` the same in Russian, Greek, Arabic, Hebrew, Japanese, Chinese and
  Korean on the synthetic pages [fail]; ``C-stated`` (cost) rows removed, a
  statement naming the query followed by a help sentence or clause, a
  suggestion, or with a reflexive "se" / "si" [empty success]; ``C-search``
  (cost) rows removed, a statement about "your search term" ("Zu Ihrem
  Suchbegriff wurden keine Ergebnisse gefunden") under a heading or below a
  summary naming the query [empty success].
* a heading naming the query above a statement naming another term:
  ``H-other`` on the saved pages, seven phrasings in five languages, and
  ``IH-other`` on the synthetic pages in every non-Latin language [fail].
* more statement forms (``I18N_MORE``: "暂无相关结果", "没有搜索结果", "Товары не
  найдены", "товаров не найдено", "נמצאו 0 תוצאות", "ولا توجد نتائج", "검색 결과가
  없어요", "条件に合う商品はありません"): ``I-more`` under a heading or below a
  summary naming the query [empty success]; ``I-more-decoy`` in a help box or
  the footer of a page whose selector broke, as a question under a heading
  naming the query, or naming another term [fail].
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

MSYS2 = "div.card-body.overflow-auto table.table.table-hover.table-sm tbody tr"
CHEESE = ("div.center-block.col-md-9 div.catalog.internal.search-results "
          "div.product-list.product-list-category div.product-item")
COCKTAIL = "div.center div.row div.col-sm-3"
HIDE = "ul.list li.pkg"
FOOTER_PHRASES = ("No results found.", "0 results", "Nothing found", "No matches")
#: search summaries a results page shows above its list ("{v}" is the query)
SUMMARIES = ('Showing results for "{v}"', "Search results for {v}", "Results for {v}",
             "You searched for {v}")
#: the same with a statement of its own, in the summary rule's other languages
NEAR_I18N = (("Suchergebnisse für {v}", "Keine Ergebnisse gefunden."),
             ("Résultats de la recherche pour {v}", "Aucun résultat."),
             ("Resultados de búsqueda para {v}", "No se han encontrado resultados."),
             ("Risultati della ricerca per {v}", "Nessun risultato trovato."))
#: a summary naming the query ("{v}"), then a statement naming another one ("{o}")
OTHER_NEAR = (('Showing results for "{v}"', 'No results found for "{o}".'),
              ("Results for {v}", "No results available for {o}."),
              ("Suchergebnisse für {v}", "Keine Ergebnisse für {o}."),
              ("Résultats pour {v}", "Aucun résultat pour {o}."),
              ("Resultados para {v}", "Sin resultados para {o}."),
              ("Risultati per {v}", "Nessun risultato trovato per {o}."))
#: help lines naming the query ("{v}") in which "no results" is a condition, a help instruction or a
#: question: they say what to do when a search finds nothing, not that this one did
HELP_LINES = ("If no results for {v}, contact us", "When no results for {v} show up, try a shorter word",
              "No results for {v}? Check the spelling", "Contact us about no results for {v}",
              "Wenn Sie zu {v} keine Ergebnisse finden, kontaktieren Sie uns",
              "Si aucun résultat pour {v}, contactez-nous",
              "Si no se han encontrado resultados para {v}, escríbanos",
              "¿Sin resultados para {v}? Escríbanos", "Se nessun risultato per {v}, contattaci",
              "S'il n'y a aucun résultat pour {v}, contactez-nous",
              "Should there be no results for {v}, contact us",
              "Sollten keine Treffer für {v} erscheinen, schreiben Sie uns",
              "Qualora non ci sia nessun risultato per {v}, scrivici")
#: statements naming the query ("{v}") that a help sentence or clause follows: still statements
STATED_HELP = ('No results found for "{v}". Contact us if you need help.',
               'Sorry, no results for "{v}", try another term.',
               'Keine Ergebnisse für "{v}". Wenn Sie Hilfe brauchen, schreiben Sie uns.',
               # a reflexive "se" / "si" and a suggestion's question are no condition
               'Se encontraron 0 resultados para "{v}".', 'Non si è trovato nessun risultato per "{v}".',
               'No results for "{v}" – did you mean "{v}s"?')
#: statements about "your search term" below a heading or a summary naming the query: about this search
SEARCH_TERM = ("Zu Ihrem Suchbegriff wurden keine Ergebnisse gefunden.",
               "Für Ihren Suchbegriff wurden keine Treffer gefunden.",
               "No results found for your keywords.", "Aucun résultat pour ce terme.")
#: a heading naming the query ("{v}"), then a statement naming another term ("{o}")
HEAD_OTHER = OTHER_NEAR + (("Results for {v}", 'Your search for "{o}" did not match any documents.'),
                           ("Results for {v}", 'No results found for your search “{o}”.'))
FILLER = ("Search tips: use fewer words, check the spelling, and try a more general term. "
          "You can also browse the categories in the menu or contact the support team for help. ")
ROWS = ["alpha parser", "beta server", "gamma client", "delta parser", "epsilon tool"]
I18N_SELECTOR = "ul.hits li.hit"
#: the non-Latin pages' floor: their result lists have three rows
I18N_FLOOR = 3
#: per language: the query, another term, three result labels naming the query, statements naming
#: the query ("{q}"), bare statements, a heading and a search summary naming it, an error sentence, and
#: (Japanese, Chinese) a help line's questions
I18N = {
    "ja": dict(q="東京", other="大阪", rows=["東京タワー", "東京駅", "東京大学"],
               line=["「{q}」に一致する商品は見つかりませんでした。", "「{q}」の検索結果はありません。",
                     "「{q}」に該当する商品はありません", "「{q}」の検索結果 0件"],
               bare=["検索結果はありません。", "該当する商品はありません。",
                     "申し訳ございませんが、一致する結果は見つかりませんでした。"],
               heading="「{q}」の検索結果", summary="「{q}」の検索結果",
               error="エラーが発生しました。「{q}」の検索結果はありません。",
               question=["該当する商品が見つかりませんか？", "一致する結果は見つかりませんでしたか？",
                         "「{q}」の検索結果はありませんか？"]),
    "zh": dict(q="东京", other="大阪", rows=["东京塔", "东京站", "东京大学"],
               line=["没有找到与“{q}”相关的结果", "找不到“{q}”的相关结果", "“{q}”无结果"],
               bare=["没有找到相关结果。", "无结果", "抱歉，没有找到相关的商品。", "没有找到与您的搜索条件匹配的结果。"],
               heading="“{q}”的搜索结果", summary="搜索结果：{q}",
               error="服务器错误，没有找到与“{q}”相关的结果",
               question=["找不到商品？", "找不到“{q}”？联系客服", "没有找到“{q}”的相关结果吗？"]),
    "ko": dict(q="서울", other="부산", rows=["서울역", "서울대학교", "서울숲"],
               line=["'{q}'에 대한 검색 결과가 없습니다.", "'{q}' 검색결과가 없습니다"],
               bare=["검색 결과가 없습니다.", "죄송합니다. 검색된 상품이 없습니다."],
               heading="'{q}' 검색 결과", summary="'{q}'에 대한 검색 결과",
               error="오류가 발생했습니다. '{q}'에 대한 검색 결과가 없습니다."),
    "ru": dict(q="Москва", other="Казань", rows=["Москва Сити", "Москва Река", "Москва Кремль"],
               line=["По запросу «{q}» ничего не найдено", "Нет результатов для «{q}»",
                     "По запросу {q} ничего не найдено."],
               bare=["Ничего не найдено.", "По вашему запросу ничего не найдено.",
                     "К сожалению, нет результатов."],
               heading="Результаты поиска: {q}", summary="Результаты поиска по запросу «{q}»",
               error="Произошла ошибка. По запросу «{q}» ничего не найдено"),
    "el": dict(q="Αθήνα", other="Πάτρα", rows=["Αθήνα Κέντρο", "Αθήνα Πλάκα", "Αθήνα Γκάζι"],
               line=["Δεν βρέθηκαν αποτελέσματα για «{q}»", "ΔΕΝ ΒΡΕΘΗΚΑΝ ΑΠΟΤΕΛΕΣΜΑΤΑ ΓΙΑ {q}"],
               bare=["Δεν βρέθηκαν αποτελέσματα.", "Δυστυχώς, δεν βρέθηκαν αποτελέσματα."],
               heading="Αποτελέσματα για {q}", summary="Αποτελέσματα αναζήτησης για {q}",
               error="Παρουσιάστηκε σφάλμα. Δεν βρέθηκαν αποτελέσματα για «{q}»"),
    "ar": dict(q="القاهرة", other="دبي", rows=["القاهرة الجديدة", "مطار القاهرة", "جامعة القاهرة"],
               line=["لا توجد نتائج لـ «{q}»", "لم يتم العثور على نتائج للبحث عن {q}"],
               bare=["لم يتم العثور على نتائج.", "عذراً، لا توجد نتائج."],
               heading="نتائج البحث عن {q}", summary="نتائج البحث عن {q}",
               error="حدث خطأ. لا توجد نتائج لـ «{q}»"),
    "he": dict(q="חיפה", other="אילת", rows=["חיפה מרכז", "נמל חיפה", "אוניברסיטת חיפה"],
               line=["לא נמצאו תוצאות עבור „{q}”", "אין תוצאות עבור {q}"],
               bare=["לא נמצאו תוצאות.", "מצטערים, לא נמצאו תוצאות."],
               heading="תוצאות חיפוש עבור {q}", summary="תוצאות חיפוש עבור {q}",
               error="אירעה שגיאה. לא נמצאו תוצאות עבור {q}"),
}
#: per non-Latin language: help lines naming the query ("{q}") in which "no results" is a condition or
#: a question
I18N_HELP = {
    "ru": ["Если по запросу «{q}» ничего не найдено, напишите нам",
           "Ничего не найдено по запросу «{q}»? Напишите нам"],
    "el": ["Εάν δεν βρέθηκαν αποτελέσματα για «{q}», επικοινωνήστε μαζί μας",
           "Δεν βρέθηκαν αποτελέσματα για «{q}»; Επικοινωνήστε μαζί μας"],
    "ar": ["إذا لم يتم العثور على نتائج لـ «{q}»، اتصل بنا", "وإذا لم يتم العثور على نتائج لـ «{q}»، اتصل بنا"],
    "he": ["אם לא נמצאו תוצאות עבור {q}, צרו קשר", "ואם לא נמצאו תוצאות עבור {q}, צרו קשר"],
    "ja": ["もし「{q}」に該当する商品はありませんでしたら、お問い合わせください"],
    "zh": ["如果没有找到与“{q}”相关的结果，请联系我们", "搜索“{q}”没有结果时，请联系我们",
           "“{q}”暂无相关结果的话，请联系我们"],
    "ko": ["'{q}' 검색 결과가 없어요? 고객센터에 문의하세요"],
}
#: per language: more statement forms, and the same naming another term ("{o}")
I18N_MORE = {
    "zh": (["暂无相关结果", "没有搜索结果"], ["“{o}”暂无相关结果", "“{o}”没有搜索结果"]),
    "ru": (["Товары не найдены", "Товаров не найдено."],
           ["По запросу «{o}» товары не найдены", "По запросу «{o}» товаров не найдено"]),
    "he": (["נמצאו 0 תוצאות"], ["נמצאו 0 תוצאות עבור {o}"]),
    "ar": (["عذراً، ولا توجد نتائج.", "ولا توجد نتائج"], ["ولا توجد نتائج لـ «{o}»"]),
    "ko": (["검색 결과가 없어요"], ["'{o}' 검색 결과가 없어요"]),
    "ja": (["条件に合う商品はありません"], ["「{o}」の条件に合う商品はありません"]),
}
#: the contract a NEW template carries (``result_contract.replay_contract("q")``),
#: written out so the same dict reaches both trees
CONTRACT = {"version": 1, "kind": "ordered_list", "query_param": "q",
            "list": {"min_items": 3},
            "sparse": {"max_items": 2, "identity": "key_names_query"},
            "empty": {"requires": "query_bound_no_results"},
            "region_coverage_min": None,
            "calibration": "browser replay: no region or record calibration",
            "query_positions": ["word", "start"]}


# ---------------------------------------------------------------------------
# page surgery (string edits located with the tree the product parses)
# ---------------------------------------------------------------------------

def _spans_builder():
    from anyuse.execution import html_extract as X

    class Spans(X._TreeBuilder):
        """The product's tree builder, also recording each element's start offset."""

        def __init__(self, raw: str):
            super().__init__(visible_only=False)
            self._starts = [0] + [i + 1 for i, ch in enumerate(raw) if ch == "\n"]

        def handle_starttag(self, tag, attrs):
            line, col = self.getpos()
            start = self._starts[line - 1] + col
            depth = len(self._stack)
            super().handle_starttag(tag, attrs)
            node = (self._stack[-1] if len(self._stack) > depth
                    else (self._stack[-1].children[-1] if self._stack[-1].children else None))
            if node is not None:
                node.bench_start = start
    return Spans


def _tree(raw: str):
    builder = _spans_builder()(raw)
    builder.feed(raw)
    builder.close()
    return builder.root


def _element_end(raw: str, start: int, tag: str) -> Optional[int]:
    pat = re.compile(r"<(/?)%s\b[^>]*>" % re.escape(tag), re.I)
    depth = 0
    for m in pat.finditer(raw, start):
        if m.group(1):
            depth -= 1
        elif not m.group(0).endswith("/>"):
            depth += 1
        if depth == 0:
            return m.end()
    return None


def _span(raw: str, node) -> Optional[Tuple[int, int]]:
    start = getattr(node, "bench_start", None)
    if start is None:
        return None
    end = _element_end(raw, start, node.tag)
    return (start, end) if end else None


def _container(nodes):
    """The lowest element holding every matched row."""
    def chain(n):
        out = []
        while n is not None and n.tag:
            out.append(n)
            n = n.parent
        return out
    common = chain(nodes[0].parent)
    for n in nodes[1:]:
        ids = {id(a) for a in chain(n)}
        common = [a for a in common if id(a) in ids]
    return common[0] if common else None


def rows_replaced(raw: str, selector: str, block: str, keep_container: bool) -> Optional[str]:
    """The page with the matched rows (or their container) replaced by ``block``."""
    from anyuse.execution.html_extract import select
    nodes = select(_tree(raw), selector)
    if not nodes:
        return None
    if keep_container:
        spans = [_span(raw, n) for n in nodes]
    else:
        box = _container(nodes)
        spans = [_span(raw, box) if box is not None else None]
    if any(s is None for s in spans):
        return None
    spans = sorted(set(spans))
    merged: List[Tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out = raw
    for i, (s, e) in enumerate(reversed(merged)):
        out = out[:s] + (block if i == len(merged) - 1 else "") + out[e:]
    return out


def rename_class(raw: str, selector: str) -> Tuple[str, str]:
    """The page with one class of the selector renamed wherever it appears, and the class."""
    from anyuse.execution.html_extract import parse_selector
    steps = parse_selector(selector)
    cls = ""
    for _tag, classes in reversed(steps):
        named = [c for c in classes if not c.startswith("#")]
        if named:
            cls = max(named, key=len)
            break

    def sub(m):
        tokens = [t + "-v2" if t == cls else t for t in m.group(3).split()]
        return m.group(1) + m.group(2) + " ".join(tokens) + m.group(2)
    return re.sub(r"(class=)([\"'])(.*?)\2", sub, raw, flags=re.S), cls


def before_rows(raw: str, selector: str, block: str) -> Optional[str]:
    """The page with ``block`` right before the element holding the matched rows."""
    from anyuse.execution.html_extract import select
    nodes = select(_tree(raw), selector)
    box = _container(nodes) if nodes else None
    span = _span(raw, box) if box is not None else None
    return raw[:span[0]] + block + raw[span[0]:] if span else None


def after_rows(raw: str, selector: str, block: str) -> Optional[str]:
    """The page with ``block`` right after the element holding the matched rows."""
    from anyuse.execution.html_extract import select
    nodes = select(_tree(raw), selector)
    box = _container(nodes) if nodes else None
    span = _span(raw, box) if box is not None else None
    return raw[:span[1]] + block + raw[span[1]:] if span else None


def before_body_end(raw: str, block: str) -> str:
    i = raw.lower().rfind("</body>")
    return raw[:i] + block + raw[i:] if i >= 0 else raw + block


def after_body_start(raw: str, block: str) -> str:
    m = re.search(r"<body\b[^>]*>", raw, re.I)
    return raw[:m.end()] + block + raw[m.end():] if m else block + raw


def listing(show: str) -> str:
    hide = ' style="display: none"'
    lis = "".join(f'<li class="pkg"{"" if show in r else hide}><a>{r}</a></li>' for r in ROWS)
    return f'<html><body><input id="q"><ul class="list">{lis}</ul></body></html>'


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------

def corpus(tree: str) -> Dict[str, Dict[str, Any]]:
    """site -> {selector, host, visible_only, pages: {value: dom}, odd: {value: dom}}"""
    from anyuse.execution import attach as A
    with gzip.open(os.path.join(tree, "testdata", "runtime_contract",
                                "saved_pages.json.gz"), "rt") as fh:
        saved = json.load(fh)["pages"]
    with gzip.open(os.path.join(tree, "testdata", "web_sessions",
                                "thecocktaildb_sparse.sessions.json.gz"), "rt") as fh:
        ct = json.load(fh)
    crates = crates_fixture(tree)
    return {
        "msys2": {"selector": MSYS2, "host": "packages.msys2.org", "visible_only": False,
                  "pages": {saved[k]["value"]: saved[k]["html"]
                            for k in ("msys2_gcc", "msys2_openssl", "msys2_ffmpeg", "msys2_boost")},
                  "odd": {"uuid": saved["msys2_uuid"]["html"]}},
        "cheese": {"selector": CHEESE, "host": "www.cheese.com", "visible_only": False,
                   "pages": {"gruyere": saved["cheese_gruyere"]["html"]},
                   "odd": {"roquefort": saved["cheese_roquefort"]["html"]}},
        "cocktaildb": {"selector": COCKTAIL, "host": "www.thecocktaildb.com",
                       "visible_only": False,
                       "pages": {v: A.result_snapshot(s, v)["dom"]
                                 for s, v in zip(ct["sessions"], ct["values"])},
                       "odd": {}},
        "crates": {"selector": crates["selector"], "host": "crates.io", "visible_only": False,
                   "pages": dict(crates["doms"]), "odd": {}},
        "hidefilter": {"selector": HIDE, "host": "ex.test", "visible_only": True,
                       "pages": {v: listing(v) for v in ("parser", "server", "client")},
                       "odd": {}},
        # the non-Latin pages are built by i18n_cases
        "i18n": {"selector": I18N_SELECTOR, "host": "i18n.test", "visible_only": False,
                 "pages": {}, "odd": {}},
    }


def i18n_page(body: str, q: str, footer: str = "© i18n.test") -> str:
    return (f"<html><body><header><a href='/'>Home</a><form><input value='{q}'></form></header>"
            f"<main>{body}</main><footer><p>{footer}</p></footer></body></html>")


def i18n_hits(rows: List[str], cls: str = "hit") -> str:
    return "<ul class='hits'>" + "".join(f"<li class='{cls}'><a href='/i/{i}'>{r}</a></li>"
                                         for i, r in enumerate(rows)) + "</ul>"


def i18n_cases() -> List[Tuple[str, str, str, str, Any, str]]:
    """(group, language, value, dom, gold, note) for the non-Latin pages."""
    out: List[Tuple[str, str, str, str, Any, str]] = []
    for lang, L in I18N.items():
        q, other = L["q"], L["other"]
        out.append(("I-list", lang, q, i18n_page(i18n_hits(L["rows"]), q), list(L["rows"]), lang))
        empty = "<ul class='hits'></ul>"
        for line in L["line"]:
            out.append(("I-line", lang, q, i18n_page(f"{empty}<p class='empty'>{line.format(q=q)}</p>", q),
                        [], f"{lang}: {line}"))
            out.append(("I-other", lang, q, i18n_page(f"{empty}<p class='empty'>{line.format(q=other)}</p>", q),
                        None, f"{lang}: {line}"))
            out.append(("I-other-near", lang, q, i18n_page(
                f"<p class='summary'>{L['summary'].format(q=q)}</p>{empty}"
                f"<p class='empty'>{line.format(q=other)}</p>", q), None, f"{lang}: {line}"))
        drifted = i18n_hits(L["rows"], cls="hit-v2")
        for bare in L["bare"]:
            out.append(("I-head", lang, q, i18n_page(
                f"<h2>{L['heading'].format(q=q)}</h2>{empty}<p class='empty'>{bare}</p>", q), [], f"{lang}: {bare}"))
            out.append(("I-summary", lang, q, i18n_page(
                f"<p class='summary'>{L['summary'].format(q=q)}</p>{empty}<p class='empty'>{bare}</p>", q),
                [], f"{lang}: {bare}"))
            out.append(("I-footer", lang, q, i18n_page(drifted, q, footer=bare), None, f"{lang}: {bare}"))
            out.append(("I-help", lang, q, i18n_page(f"{drifted}<div class='help'><p>{bare}</p></div>", q),
                        None, f"{lang}: {bare}"))
            out.append(("I-summary-rows", lang, q, i18n_page(
                f"<p class='summary'>{L['summary'].format(q=q)}</p>{drifted}"
                f"<div class='help'><p>{bare}</p></div>", q), None, f"{lang}: {bare}"))
        out.append(("I-error", lang, q, i18n_page(
            f"{empty}<p class='alert'>{L['error'].format(q=q)}</p>", q), None, lang))
        for question in L.get("question", ()):
            ask = f"<p class='help'>{question.format(q=q)}</p>"
            for body in (f"<h2>{L['heading'].format(q=q)}</h2>{ask}{drifted}",
                         f"<p class='summary'>{L['summary'].format(q=q)}</p>{ask}{drifted}",
                         f"{drifted}{ask}"):
                out.append(("I-question", lang, q, i18n_page(body, q), None, f"{lang}: {question}"))
        heading, summary = L["heading"].format(q=q), L["summary"].format(q=q)
        for line in I18N_HELP.get(lang, ()):
            help_p = f"<p class='help'>{line.format(q=q)}</p>"
            for where, body in (("after rows", f"{drifted}<div class='help'>{help_p}</div>"),
                                ("alone", f"{empty}{help_p}"),
                                ("under heading", f"<h2>{heading}</h2>{empty}{help_p}"),
                                ("below summary", f"<p class='summary'>{summary}</p>{empty}{help_p}")):
                out.append(("IC-help", lang, q, i18n_page(body, q), None, f"{lang} {where}: {line}"))
        for line in L["line"]:
            out.append(("IH-other", lang, q, i18n_page(
                f"<h2>{heading}</h2>{empty}<p class='empty'>{line.format(q=other)}</p>", q), None,
                f"{lang}: {line}"))
        stated, others = I18N_MORE.get(lang, ((), ()))
        for st in stated:
            for where, body in (("under heading", f"<h2>{heading}</h2>{empty}<p class='empty'>{st}</p>"),
                                ("below summary", f"<p class='summary'>{summary}</p>{empty}"
                                                  f"<p class='empty'>{st}</p>")):
                out.append(("I-more", lang, q, i18n_page(body, q), [], f"{lang} {where}: {st}"))
            asked = f"<p class='help'>{st.rstrip('.')}?</p>"
            for where, dom in (("help box", i18n_page(f"{drifted}<div class='help'><p>{st}</p></div>", q)),
                               ("footer", i18n_page(drifted, q, footer=st)),
                               ("question", i18n_page(f"<h2>{heading}</h2>{asked}{drifted}", q))):
                out.append(("I-more-decoy", lang, q, dom, None, f"{lang} {where}: {st}"))
        for st in others:
            named = f"<p class='empty'>{st.format(o=other)}</p>"
            for where, body in (("alone", f"{empty}{named}"), ("under heading", f"<h2>{heading}</h2>{empty}{named}"),
                                ("below summary", f"<p class='summary'>{summary}</p>{empty}{named}")):
                out.append(("I-more-decoy", lang, q, i18n_page(body, q), None, f"{lang} {where}: {st}"))
    return out


def crates_fixture(tree: str) -> Dict[str, Any]:
    """The crates.io record compiled from the four attach sessions, as the runtime
    cascade tests build it."""
    from anyuse.execution import attach as A
    from anyuse.execution.api_compile import compiled_record, with_runtime_rungs
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    data = os.path.join(tree, "testdata", "attach_crates")
    sessions = [json.load(open(os.path.join(data, n))) for n in sorted(os.listdir(data))]
    captured = {}
    for s in sessions:
        for r in s["network"]:
            sp = urllib.parse.urlsplit(r["url"])
            if sp.path == "/api/v1/crates":
                captured[urllib.parse.parse_qs(sp.query)["q"][0]] = r["body"]

    class Api:
        def send(self, method, url, headers, body, timeout_s):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
            return ApiResponse(200, {"Content-Type": "application/json"}, captured[q])
    compiled, proof, _report, recs = A.compile_sessions(
        sessions, executor=ApiStepExecutor(transport=Api()), preflight=lambda _c: "")
    if not proof.passed:
        raise SystemExit(f"crates fixture did not prove: {proof.reason}")
    record = compiled_record(compiled.template, proof.to_dict(), workflow="crates_search",
                             source={"site": "crates.io", "origin": "attach"})
    with_runtime_rungs(record, recs[0], origin="attach")
    return {"record": record, "captured": captured,
            "selector": record["browser"]["template"]["extract"]["items"],
            "doms": {r["param"]["value"]: r["dom"] for r in recs}}


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

def visible_text(dom: str) -> str:
    """What the fake driver returns as ``page.text``: the markup's visible text."""
    from anyuse.execution.html_extract import parse
    return parse(dom, visible_only=True).text()


class FakeDriver:
    def __init__(self, dom: str):
        self.dom, self.text, self.opened = dom, visible_text(dom), []

    def open(self, url, settle_s, timeout_s):
        from anyuse.execution.cdp_replay import PageResult
        self.opened.append(url)
        return PageResult(url=url, dom=self.dom, text=self.text, status=200, elapsed_ms=5.0)


def template_dict(site: Dict[str, Any], floor: int, contract: bool) -> Dict[str, Any]:
    sel = site["selector"]
    expect: Dict[str, Any] = {"selector": sel, "min_items": floor,
                              "identity": {"final_url": "rendered_url"}}
    if contract:
        expect["result_contract"] = json.loads(json.dumps(CONTRACT))
    return {"url": f"https://{site['host']}/search?q={{q}}", "params": ["q"],
            "extract": {"items": sel}, "expect": expect,
            "visible_only": bool(site["visible_only"]), "read_labels": False}


def replay(tdict: Dict[str, Any], dom: str, value: str, scratch: str, n: int) -> Dict[str, Any]:
    from anyuse.execution.cdp_replay import ReplayExecutor, ReplayTemplate
    # a fresh blocked-host registry per case: one case's block never decides another's
    os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(scratch, f"blocked_{n}.json")
    out = ReplayExecutor(driver=FakeDriver(dom)).execute(ReplayTemplate.from_dict(tdict),
                                                        {"q": value})
    items = (out.get("extracted") or {}).get("items")
    return {"success": bool(out.get("success")),
            "items": list(items) if out.get("success") else None,
            "empty_state": out.get("empty_state") or "",
            "contract_kind": (out.get("result_contract") or {}).get("kind") or "",
            "blocked": bool(out.get("blocked")),
            "error": str(out.get("error") or "")[:220]}


def cases(sites: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every case: {case, group, site, value, dom, floor, gold, gold_items}."""
    from anyuse.execution.html_extract import extract
    out: List[Dict[str, Any]] = []

    def add(group, site_name, value, dom, floor, gold, gold_items=None, note=""):
        out.append({"case": f"{group}|{site_name}|{value}|F{floor}" + (f"|{note}" if note else ""),
                    "group": group, "site": site_name, "value": value, "dom": dom,
                    "floor": floor, "gold": gold, "gold_items": gold_items, "note": note})

    for name, site in sites.items():
        sel, vis = site["selector"], site["visible_only"]
        counts = {v: len(extract(d, sel, limit=100, visible_only=vis))
                  for v, d in site["pages"].items()}
        listed = [c for c in counts.values() if c >= 3]
        site["floor"] = min(listed) if listed else 1
        site["counts"] = counts
        floors = sorted({1, site["floor"]})
        if name == "hidefilter":
            for v, d in site["pages"].items():
                add("V1", name, v, d, 1, "items", extract(d, sel, limit=100, visible_only=True))
            continue
        values = list(site["pages"])
        for v, d in site["pages"].items():
            gold = extract(d, sel, limit=100, visible_only=vis)
            for f in floors:
                add("D0", name, v, d, f, "items", gold)
            f = site["floor"]
            drifted, cls = rename_class(d, sel)
            if extract(drifted, sel, limit=100) or not gold or gold[0][:20] not in visible_text(drifted):
                add("SKIP", name, v, d, f, "fail", note=f"rename of {cls!r} did not break the selector")
                continue
            add("D1", name, v, drifted, f, "fail", note=f"renamed {cls}")
            for phrase in FOOTER_PHRASES:
                add("D2", name, v, before_body_end(drifted, f"<footer><p>{phrase}</p></footer>"),
                    f, "fail", note=phrase)
                # a help box right after the result list: the rows are the lines above it
                page = after_rows(d, sel, f'<div class="help"><p>{phrase}</p></div>')
                if page is None:
                    add("SKIP", name, v, d, f, "fail", note="D2-after: no result container")
                else:
                    add("D2-after", name, v, rename_class(page, sel)[0], f, "fail", note=phrase)
            for k, phrase in enumerate(FOOTER_PHRASES):
                # a summary above the rows and a help box below them: the rows sit between
                page = after_rows(d, sel, f'<div class="help"><p>{phrase}</p></div>')
                page = page and before_rows(page, sel, f'<p class="summary">'
                                                       f'{SUMMARIES[k].format(v=v)}</p>')
                if page is None:
                    add("SKIP", name, v, d, f, "fail", note="D2-summary: no result container")
                else:
                    add("D2-summary", name, v, rename_class(page, sel)[0], f, "fail", note=phrase)
            other = next((o for o in values if o != v), "zzqx")
            add("D3", name, v, before_body_end(
                drifted, f'<aside class="sidebar"><p>No results found for "{other}".</p></aside>'),
                f, "fail", note=f"other={other}")
            add("D4", name, v, after_body_start(
                drifted, f'<div class="alert"><p>Something went wrong while searching for '
                         f'"{v}": 0 results</p></div>'), f, "fail")
            stated = f'<p class="empty-state">No results found for "{v}".</p>'
            for keep in (True, False):
                page = rows_replaced(d, sel, stated, keep)
                label = "container kept" if keep else "container removed"
                if page is None or extract(page, sel, limit=100):
                    add("SKIP", name, v, d, f, "fail", note=f"D5 {label}: rows not removed")
                    continue
                add("D5", name, v, page, f, "empty", [], note=label)
            far = (f"<p>{FILLER * 2}</p><p class='empty-state'>No results found.</p>"
                   f"<p>{FILLER * 2}</p>")
            bare = '<p class="empty-state">No results found.</p>'
            near = [("D5-near", f"<p>{s.format(v=v)}</p>{bare}", s) for s in SUMMARIES]
            near += [("D5-near-i18n", f'<p>{s.format(v=v)}</p><p class="empty-state">{t}</p>', s)
                     for s, t in NEAR_I18N]
            gap = f'<p>Search results for "{v}"</p><p>Sort by: relevance</p>{bare}'
            head = f'<h3>Results for "{v}"</h3>{bare}'
            word = {"crates": "crates", "cheese": "cheeses", "cocktaildb": "drinks"}.get(name, "builds")
            noun = f'<p class="empty-state">No matching {word} for "{v}"</p>'
            other_near = [("D3-near", f'<p>{s.format(v=v)}</p>'
                                      f'<p class="empty-state">{t.format(o=other)}</p>', t)
                          for s, t in OTHER_NEAR]
            # a condition, a help instruction or a question naming the query: no statement
            help_lines = [(line, f'<p class="help">{line.format(v=v)}</p>') for line in HELP_LINES]
            for line, help_p in help_lines:
                page = after_rows(d, sel, f'<div class="help">{help_p}</div>')
                if page is None:
                    add("SKIP", name, v, d, f, "fail", note="C-help: no result container")
                else:
                    add("C-help", name, v, rename_class(page, sel)[0], f, "fail", note=f"after rows: {line}")
            head_other = [("H-other", f'<h3>{s.format(v=v)}</h3>'
                                      f'<p class="empty-state">{t.format(o=other)}</p>', t)
                          for s, t in HEAD_OTHER]
            help_alone = [("C-help", block, f"{where}: {line}")
                          for line, help_p in help_lines
                          for where, block in (("alone", help_p),
                                               ("under heading", f'<h3>Results for "{v}"</h3>{help_p}'),
                                               ("below summary", f'<p>Search results for "{v}"</p>{help_p}'))]
            stated_help = [("C-stated", f'<p class="empty-state">{t.format(v=v)}</p>', t) for t in STATED_HELP]
            search_term = [("C-search", f'{above}<p class="empty-state">{t}</p>', f"{where}: {t}")
                           for t in SEARCH_TERM
                           for where, above in (("under heading", f'<h3>Results for "{v}"</h3>'),
                                                ("below summary", f'<p>Search results for "{v}"</p>'))]
            for group, block, note in (("D5-far", far, ""), *near, ("D5-near-gap", gap, ""),
                                       ("D5-head", head, ""), ("D5-noun", noun, ""),
                                       *other_near, *head_other, *help_alone, *stated_help,
                                       *search_term):
                page = rows_replaced(d, sel, block, True)
                if page is not None and not extract(page, sel, limit=100):
                    gold = "fail" if group in ("D3-near", "H-other", "C-help") else "empty"
                    add(group, name, v, page, f, gold, [] if gold == "empty" else None,
                        note=note)
        sparse = [v for v, c in counts.items() if 1 <= c <= 2]
        for v in sparse:
            for q in sparse:
                if q != v:
                    for f in floors:
                        add("S1", name, q, site["pages"][v], f, "fail", note=f"page of {v}")
        for v, d in site["odd"].items():
            for f in floors:
                add("ODD", name, v, d, f, "fail")
    sites["i18n"]["floor"] = I18N_FLOOR
    for group, lang, value, dom, gold, note in i18n_cases():
        if group != "I-list" and extract(dom, I18N_SELECTOR, limit=100):
            add("SKIP", "i18n", value, dom, I18N_FLOOR, "fail", note=f"{group} {note}: rows still match")
            continue
        add(group, "i18n", value, dom, I18N_FLOOR,
            "fail" if gold is None else ("empty" if gold == [] else "items"), gold, note=note)
    return out


def verdict(case: Dict[str, Any], res: Dict[str, Any]) -> str:
    if case["gold"] == "fail":
        return "false_success" if res["success"] else "true_fail"
    if not res["success"]:
        return "true_loss"
    return "true_success" if res["items"] == case["gold_items"] else "false_success"


def cascade(tree: str, scratch: str, contract: Optional[bool]) -> List[Dict[str, Any]]:
    """run_compiled on the crates record. ``contract``: None keeps the record as this tree
    compiled it; False strips the browser template's contract (a record stored before)."""
    import copy
    from anyuse.execution import api_permission as perm
    from anyuse.execution.api_compile import load_compiled, persist_compiled
    from anyuse.execution.api_permission import acknowledge_host
    from anyuse.execution.api_step import ApiResponse, ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.execution.html_extract import extract
    from anyuse.mcpserver.compiled_tools import run_compiled
    perm._default_fetch = lambda robots_url, *a, **kw: "User-agent: *\nAllow: /\n"
    fx = crates_fixture(tree)
    sel = fx["selector"]
    rows = []

    class Api:
        def send(self, method, url, headers, body, timeout_s):
            return ApiResponse(200, {"Content-Type": "application/json"}, '{"crates": []}')

    class Storage:
        def __init__(self, base):
            self.base_dir = base

        def get_workflow_dir(self, workflow_id):
            return os.path.join(self.base_dir, f"workflow_{workflow_id}")

        def get(self, name):
            return None

    n = 0
    for value in ("tokio", "clap"):
        dom = fx["doms"][value]
        drifted, _cls = rename_class(dom, sel)
        pages = {
            "D0": (dom, "items", extract(dom, sel, limit=100, labels=True)),
            "D2": (before_body_end(drifted, "<footer><p>No results found.</p></footer>"),
                   "fail", None),
            "D5": (rows_replaced(dom, sel, f'<p class="empty-state">No results found for '
                                           f'"{value}".</p>', True), "empty", []),
        }
        for kind, (page, gold, gold_items) in pages.items():
            if page is None:
                raise SystemExit(f"cascade {kind} page for {value!r} could not be built")
            for with_agent in (True, False):
                n += 1
                base = os.path.join(scratch, f"cascade_{contract}_{n}")
                os.makedirs(base, exist_ok=True)
                os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(base, "blocked.json")
                record = copy.deepcopy(fx["record"])
                if contract is False:
                    record["browser"]["template"]["expect"].pop("result_contract", None)
                storage = Storage(base)
                persist_compiled(storage, "crates_search", record)
                hosts = os.path.join(base, "hosts.json")
                acknowledge_host("crates.io", path=hosts)
                calls: List[str] = []

                def agent(task, **kw):
                    calls.append(task)
                    return {"ok": True, "outcome": "done",
                            "result": [f"{value}", f"{value}-util", f"{value}-macros"],
                            "model_calls": 2, "cost_usd": 0.02, "steps": 3}
                out = run_compiled("crates_search", {"q": value}, storage=storage,
                                   hosts_path=hosts, executor=ApiStepExecutor(transport=Api()),
                                   browser_executor=ReplayExecutor(driver=FakeDriver(page)),
                                   agent_runner=agent if with_agent else None)
                stored = load_compiled(storage, "crates_search") or {}
                served = out.get("served_by")
                items = (out.get("result") or {}).get("items") if out.get("ok") else None
                browser_served = bool(out.get("ok")) and served == "browser_replay"
                if gold == "fail":
                    v = "false_success" if browser_served else "true_fail"
                elif not browser_served:
                    v = "true_loss"
                else:
                    v = "true_success" if items == gold_items else "false_success"
                rows.append({"case": f"CASCADE|{kind}|{value}|agent={with_agent}",
                             "group": "CASCADE-" + kind, "site": "crates", "value": value,
                             "floor": record["browser"]["template"]["expect"].get("min_items"),
                             "gold": gold, "success": bool(out.get("ok")), "served_by": served,
                             "items": items, "agent_calls": len(calls),
                             "demoted": (stored.get("health") or {}).get("preferred", True) is False,
                             "verdict": v, "error": str(out.get("error") or "")[:160],
                             "browser_why": next((str(t.get("why") or "")[:160]
                                                  for t in out.get("rungs_tried") or []
                                                  if t.get("rung") == "browser_replay"), "")})
    return rows


def run_arm(label: str, tree: str, out_path: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    from anyuse.execution import result_contract as RC
    scratch = tempfile.mkdtemp(prefix=f"arm-{label}-", dir=os.path.dirname(out_path))
    # every key the tree's own contract has must be the bench's: a tree whose contract
    # has fewer keys (no query positions yet) ignores the rest
    if hasattr(RC, "replay_contract") and any(
            CONTRACT.get(k) != v for k, v in RC.replay_contract("q").items()):
        raise SystemExit("the bench's contract dict differs from result_contract.replay_contract")
    sites = corpus(tree)
    all_cases = cases(sites)
    variants = [("legacy", False), ("contract", True)]
    rows: List[Dict[str, Any]] = []
    n = 0
    for case in all_cases:
        if case["group"] == "SKIP":
            rows.append({"case": case["case"], "group": "SKIP", "arm": label, "note": case["note"]})
            continue
        site = sites[case["site"]]
        per = {}
        for vname, with_contract in variants:
            n += 1
            res = replay(template_dict(site, case["floor"], with_contract), case["dom"],
                         case["value"], scratch, n)
            per[vname] = res
        # the plain contract rule (a 160-character window), for the empty-page groups
        window = ""
        if case["group"] not in ("D0", "S1", "V1", "I-list"):
            window = RC.empty_evidence_text(visible_text(case["dom"]), case["value"])[0]
        for vname, res in per.items():
            arm = f"{label}-{vname}" if label == "NEW" or hasattr(RC, "replay_contract") else label
            rows.append({"case": case["case"], "group": case["group"], "arm": arm,
                         "variant": vname, "site": case["site"], "value": case["value"],
                         "floor": case["floor"], "gold": case["gold"], "note": case["note"],
                         "gold_items": case["gold_items"], "window_rule_accepts": bool(window),
                         **res, "verdict": verdict(case, res)})
    split = label == "NEW" or hasattr(RC, "replay_contract")
    if not split:
        diff = [r["case"] for r in rows if r.get("variant") == "legacy"
                and next(x for x in rows if x["case"] == r["case"] and x.get("variant") == "contract"
                         )["success"] != r["success"]]
        meta_old_agree = not diff
        rows = [r for r in rows if r.get("variant") in (None, "legacy")]
    else:
        meta_old_agree = None
    variants_c = ([(f"{label}-legacy", False), (f"{label}-contract", None)] if split
                  else [("OLD", None)])
    for arm, contract in variants_c:
        for r in cascade(tree, scratch, contract):
            rows.append({**r, "arm": arm})
    meta = {"label": label, "tree": tree, "anyuse": real, "cases": len(all_cases),
            "arms": [a for a, _c in variants_c],
            "old_variants_agree": meta_old_agree,
            "floors": {k: s.get("floor") for k, s in sites.items()},
            "counts": {k: s.get("counts") for k, s in sites.items()}}
    with open(out_path, "w") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, indent=1, default=str)
    print(f"{label}: {len(rows)} rows -> {out_path}")


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

ORDER = ["D0", "D1", "D2", "D2-after", "D2-summary", "D3", "D3-near", "D4", "D5", "S1", "V1",
         "ODD", "D5-far", "D5-near", "D5-near-i18n", "D5-near-gap", "D5-head", "D5-noun",
         "I-list", "I-line", "I-head", "I-summary", "I-other", "I-other-near", "I-footer", "I-help",
         "I-summary-rows", "I-error", "I-question",
         "C-help", "IC-help", "C-stated", "C-search", "H-other", "IH-other", "I-more", "I-more-decoy",
         "CASCADE-D0",
         "CASCADE-D2",
         "CASCADE-D5"]
#: the drift groups whose gold is a failure
DRIFT = ("D1", "D2", "D2-after", "D2-summary", "D3", "D3-near", "D4")
#: the non-Latin groups: genuinely empty pages, and decoys whose gold is a failure
I18N_EMPTY = ("I-line", "I-head", "I-summary")
I18N_DECOYS = ("I-other", "I-other-near", "I-footer", "I-help", "I-summary-rows", "I-error", "I-question")
#: conditions, help instructions and questions naming the query; a statement naming another term under a
#: heading naming the query; the decoys of the further statement forms: all gold failures
EDGE_DECOYS = ("C-help", "IC-help", "H-other", "IH-other", "I-more-decoy")


def compare(paths: List[str]) -> str:
    rows: List[Dict[str, Any]] = []
    metas = []
    for p in paths:
        with open(p) as fh:
            data = json.load(fh)
        rows.extend(data["rows"])
        metas.append(data["meta"])
    arms = [a for m in metas for a in m.get("arms") or [m["label"]]]
    lines = []
    for m in metas:
        lines.append(f"{m['label']}: anyuse -> {m['anyuse']}; {m['cases']} cases; floors "
                     f"{m['floors']}" + ("" if m["old_variants_agree"] is None else
                                         f"; OLD agrees across template variants: "
                                         f"{m['old_variants_agree']}"))
    skips = [r for r in rows if r["group"] == "SKIP" and r["arm"] == "OLD"]
    for r in skips:
        lines.append(f"skipped: {r['case']} ({r['note']})")
    lines.append("")
    lines.append("group           n | " + " | ".join(f"{a:>22}" for a in arms))
    lines.append("                  | " + " | ".join(f"{'true  false  loss':>22}" for _ in arms))

    def split_floor(group):
        return group in ("D0", "S1", "ODD")

    for group in ORDER:
        subsets = [("@F1", 1), ("@Freal", "real")] if split_floor(group) else [("", None)]
        for tag, floor in subsets:
            cells, n = [], 0
            for arm in arms:
                if floor is None:
                    sel = [r for r in rows if r["group"] == group and r["arm"] == arm]
                elif floor == 1:
                    sel = [r for r in rows if r["group"] == group and r["arm"] == arm
                           and r.get("floor") == 1]
                else:
                    sel = [r for r in rows if r["group"] == group and r["arm"] == arm
                           and r.get("floor") != 1]
                n = max(n, len(sel))
                cells.append(f"{sum(r['verdict'] == 'true_success' for r in sel):>5}  "
                             f"{sum(r['verdict'] == 'false_success' for r in sel):>5}  "
                             f"{sum(r['verdict'] == 'true_loss' for r in sel):>4}")
            if n:
                lines.append(f"{(group + tag):<14} {n:>3} | " + " | ".join(f"{c:>22}" for c in cells))
    lines.append("")
    lines.append("true = served and equal to gold; false = served and wrong (or gold is a failure);"
                 " loss = not served where gold is a success")
    sparse = [r for r in rows if r["group"] == "D0" and r.get("floor") != 1
              and r.get("gold_items") is not None and 1 <= len(r["gold_items"]) <= 2]
    for arm in arms:
        got = [r for r in sparse if r["arm"] == arm]
        lines.append(f"sparse pages at the realistic floor, {arm}: "
                     f"{sum(r['verdict'] == 'true_success' for r in got)}/{len(got)} served")
    window = [r for r in rows if r["arm"] == "NEW-contract"
              and r["group"] in DRIFT and r.get("window_rule_accepts")]
    lines.append(f"drift cases the plain 160-character window would have accepted as empty: "
                 f"{len(window)} ({', '.join(sorted({r['group'] + ' ' + r['site'] for r in window}))})")
    # D0 list pages: the same answer in every arm
    d0 = {}
    for r in rows:
        if r["group"] == "D0" and r.get("gold_items") is not None and len(r["gold_items"]) >= 3:
            d0.setdefault((r["case"]), {})[r["arm"]] = (r["success"], r.get("items"))
    same = all(len(set(json.dumps(v, sort_keys=True) for v in arms.values())) == 1
               for arms in d0.values())
    lines.append(f"D0 list pages identical in all arms: {same} ({len(d0)} cases)")
    for arm in arms:
        casc = [r for r in rows if r["arm"] == arm and r["group"].startswith("CASCADE")]
        for r in casc:
            lines.append(f"{arm:<13} {r['case']:<34} ok={r['success']!s:<5} "
                         f"served_by={r['served_by']!s:<14} agent_calls={r['agent_calls']} "
                         f"demoted={r['demoted']!s:<5} items={json.dumps(r['items'])[:60]}")

    def false_in(arm, groups, floor=None):
        return sum(1 for r in rows if r["arm"] == arm and r["group"] in groups
                   and r["verdict"] == "false_success"
                   and (floor is None or (floor == "real" and r.get("floor") != 1)))
    core = DRIFT + ("V1", "ODD")
    near = [r for r in rows if r["arm"].startswith("NEW")
            and r["group"] in ("D5-near", "D5-near-i18n", "D5-near-gap")]
    i18n_empty = [r for r in rows if r["arm"].startswith("NEW") and r["group"] in I18N_EMPTY]
    edge = [r for r in rows if r["arm"].startswith("NEW") and r["group"] in EDGE_DECOYS]
    more = [r for r in rows if r["arm"].startswith("NEW") and r["group"] == "I-more"]
    stated = [r for r in rows if r["arm"].startswith("NEW") and r["group"] == "C-stated"]
    search_term = [r for r in rows if r["arm"].startswith("NEW") and r["group"] == "C-search"]
    checks = {
        "NEW-contract: 0 false successes on D1-D4 (with D2-after, D2-summary, D3-near), S1, V1, ODD":
            false_in("NEW-contract", core + ("S1",)) == 0,
        "NEW-legacy: 0 false successes on D1-D4 (with D2-after, D2-summary, D3-near), V1, ODD, "
        "S1 at the realistic floor":
            false_in("NEW-legacy", core) == 0 and false_in("NEW-legacy", ("S1",), "real") == 0,
        "D5-near, D5-near-i18n and D5-near-gap served as empty in NEW":
            bool(near) and all(r["verdict"] == "true_success" for r in near),
        "D0 list pages identical in all arms": same,
        "sparse pages at the realistic floor: NEW-contract serves all":
            all(r["verdict"] == "true_success" for r in sparse if r["arm"] == "NEW-contract"),
        "D5 served as empty in all arms":
            all(r["verdict"] == "true_success" for r in rows if r["group"] == "D5"),
        "cascade D2: the browser never serves in NEW":
            all(r["verdict"] != "false_success" for r in rows
                if r["group"] == "CASCADE-D2" and r["arm"].startswith("NEW")),
        "non-Latin empty pages (I-line, I-head, I-summary) served as empty in NEW":
            bool(i18n_empty) and all(r["verdict"] == "true_success" for r in i18n_empty),
        "non-Latin decoys: 0 false successes in every arm":
            not any(r["verdict"] == "false_success" for r in rows if r["group"] in I18N_DECOYS),
        "non-Latin result lists (I-list) served in every arm":
            all(r["verdict"] == "true_success" for r in rows if r["group"] == "I-list"),
        "NEW: 0 false successes on the condition, heading and further-form decoys "
        "(C-help, IC-help, H-other, IH-other, I-more-decoy)":
            bool(edge) and not any(r["verdict"] == "false_success" for r in edge),
        "further statement forms (I-more) served as empty in NEW":
            bool(more) and all(r["verdict"] == "true_success" for r in more),
        "statements followed by help (C-stated) served as empty in NEW":
            bool(stated) and all(r["verdict"] == "true_success" for r in stated),
        "statements about your search term (C-search) served as empty in NEW":
            bool(search_term) and all(r["verdict"] == "true_success" for r in search_term),
    }
    lines.append("")
    for k, ok in checks.items():
        lines.append(f"{'PASS' if ok else 'FAIL'}  {k}")
    lines.append(f"pass criterion: {'holds' if all(checks.values()) else 'does NOT hold'}")
    legacy_s1 = false_in("NEW-legacy", ("S1",)) - false_in("NEW-legacy", ("S1",), "real")
    lines.append(f"known, by design: NEW-legacy serves {legacy_s1} S1 case(s) at floor 1 (a tool "
                 f"stored before the change keeps its floor and gets only the empty rule)")
    for arm in arms:
        if arm.startswith("NEW"):
            continue
        old_false = sum(1 for r in rows if r["arm"] == arm and r["group"] in EDGE_DECOYS
                        and r["verdict"] == "false_success")
        old_lost = sum(1 for r in rows if r["arm"] == arm and r["group"] == "I-more"
                       and r["verdict"] == "true_loss")
        lines.append(f"{arm}: {old_false} false success(es) on the condition, heading and further-form "
                     f"decoys; {old_lost} further-form empty page(s) lost")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-replay-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "replay_contract_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for label, tree in trees.items():
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, tree, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{label}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{label}.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", f"robots_{label}.json"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--label", label,
                               "--tree", tree, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
