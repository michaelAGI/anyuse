"""Offline A/B bench: demonstration tools read the site's own evidence before the hand-written lists.

The OLD tree builds version-14 tools; the NEW tree builds version-15 tools by default and still builds version 14.
Each case is decided the way a person reading the page would decide it; a row's ``got`` is whether the tool
decided it so. Three kinds of case:

* ``new``: a page in a language or markup the old lists do not hold (Dutch, Portuguese, Turkish, Vietnamese, Thai,
  Czech, Swedish, Indonesian; class names that are not English; vendors in no catalogue) - the OLD tree is expected
  to get these wrong, the NEW tree right;
* ``list``: a case the old lists already decide - both trees must get it right (no regression);
* ``decoy``: a case that must stay refused or unchanged (a brand every title starts with, a per-result link id, a
  real error page, a tie) - both trees must get it right.

Groups: result blocks that show the same items without the query (sidebars named in other languages), UI affixes
of titles, labels and dates in the title role, placement parameters on result links, empty and error result pages,
the product a lookup opens (substitutes, variant words), a product's availability and price labels, and the commit
gate's "asked for" test. Arms: without a check model, and (``right``) with a fake model that reads every line the
way the person would, asked only in a product call's scope.

More groups: a saved web tool's call over MCP (the check model asked in its scope and counted; a re-proof, a proof's
replay and a watch run inside or under a scope asking none), breadth tools (each version-14 run against its decision
recorded with the code of the release before, ``--breadth-fixture``, by default the NEW tree's
``testdata/breadth_frozen/decisions.json``; the version-15 readers), a product page on a sibling subdomain under a
two-label suffix, and a heal's card re-derivation of a version-15 region.

``frozen``: every version-14 tool the NEW tree builds and its run answers on every page of the corpus, as a
digest, against the OLD tree's: they must be byte-identical.

Usage::

    python tests/bench/generalize_demo_bench.py run --old <tree> --new <tree> \\
        [--python <interpreter>] [--out <dir>]
    python tests/bench/generalize_demo_bench.py compare OLD.json NEW.json

``--old`` is a checkout (or worktree) at the commit the feature branch forked from, ``--new`` the feature
checkout. The script copies itself into ``--out`` (outside both trees) and runs that copy once per tree, with a
pkgroot holding an ``anyuse`` symlink to the tree and the tree's ``tools/guarded_tests`` on PYTHONPATH (its
sitecustomize refuses sockets). The fixtures come from each tree's own ``tests`` directory. HOME and every AnyUse
data path point into ``--out``, and no ``ANYUSE_*`` variable is passed through. No network, no model.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MODELS = ("off", "right")
SITE = "www.winkel.test"
BASE = f"https://{SITE}"

# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------

#: per language: the query and five products whose names hold it
CATALOGS = {
    "nl": ("boormachine", ["Bosch boormachine PSB 500", "Makita boormachine HP457", "Metabo boormachine SBE 650",
                           "Einhell boormachine TC-ID", "Ryobi boormachine R18PD"]),
    "pt": ("furadeira", ["Furadeira Bosch GSB 13", "Furadeira Makita HP1630", "Furadeira Dewalt DWD024",
                         "Furadeira Skil 6002 AA", "Furadeira Vonder FIV 650"]),
    "tr": ("matkap", ["Bosch darbeli matkap GSB 13", "Makita darbeli matkap HP1630", "Dewalt matkap DWD024",
                      "Einhell matkap TC-ID 550", "Ryobi matkap RPD800"]),
    "vi": ("máy khoan", ["Máy khoan Bosch GSB 13", "Máy khoan Makita HP1630", "Máy khoan Dewalt DWD024",
                         "Máy khoan Stanley SDH600", "Máy khoan Total TG109136"]),
    "th": ("สว่าน", ["สว่าน Bosch GSB 13", "สว่าน Makita HP1630", "สว่าน Dewalt DWD024", "สว่าน Stanley SDH600",
                     "สว่าน Total TG109136"]),
    "cs": ("vrtačka", ["Příklepová vrtačka Bosch GSB 13", "Příklepová vrtačka Makita HP1630",
                       "Vrtačka Dewalt DWD024 sada", "Vrtačka Narex EV 13", "Vrtačka Einhell TC-ID 550"]),
    "sv": ("borrmaskin", ["Bosch borrmaskin GSB 13", "Makita borrmaskin HP1630", "Dewalt borrmaskin DWD024",
                          "Einhell borrmaskin TC-ID", "Ryobi borrmaskin R18PD"]),
    "id": ("bor", ["Bor listrik Bosch GSB 13", "Bor listrik Makita HP1630", "Bor beton Dewalt DWD024",
                   "Bor tangan Krisbow KW05", "Bor listrik Modern M2130"]),
    "en": ("drill", ["Bosch drill GSB 13", "Makita drill HP1630", "Dewalt drill DWD024", "Einhell drill TC-ID",
                     "Ryobi drill R18PD"]),
    "de": ("bohrmaschine", ["Bosch Bohrmaschine GSB 13", "Makita Bohrmaschine HP1630", "Dewalt Bohrmaschine DWD024",
                            "Einhell Bohrmaschine TC-ID", "Ryobi Bohrmaschine R18PD"]),
}

#: UI affixes of the title link (language, lead, tail, kind)
AFFIXES = [("nl", "Lees meer:", "", "new"), ("pt", "", "| Ver produto", "new"), ("tr", "", "incele »", "new"),
           ("vi", "Xem thêm »", "", "new"), ("th", "อ่านต่อ:", "", "new"), ("cs", "Číst více:", "", "new"),
           ("sv", "Läs mer:", "", "new"), ("id", "", "| Lihat detail", "new"), ("en", "Read:", "", "list")]
#: a label in the title role, the name in another link to the same target (language, label, kind)
LABELS = [("nl", "Snel bekijken", "new"), ("pt", "Visualização rápida", "new"), ("tr", "Hızlı bakış", "new"),
          ("vi", "Xem nhanh", "new"), ("th", "ดูด่วน", "new"), ("cs", "Rychlý náhled", "new"),
          ("sv", "Snabbvisning", "new"), ("id", "Lihat cepat", "new"), ("de", "Schnellansicht", "list"),
          ("en", "Quick view", "list")]
#: a date the page marks with ``<time>`` in the title role (language, date, kind)
DATES = [("cs", "20. září 2026", "new"), ("tr", "20 Eylül 2026", "new"), ("vi", "20 tháng 9 năm 2026", "new"),
         ("th", "20 กันยายน 2569", "new"), ("id", "20 Agustus 2026", "new"), ("en", "Sep. 20 2026", "list"),
         ("de", "20. September 2026", "list")]
#: a sidebar beside the results, by its id (language, id, kind); the wrappers around the results are decoys
SIDEBARS = [("nl", "zijbalk-a", "new"), ("pt", "barra-lateral", "new"), ("tr", "kenar-cubugu", "new"),
            ("vi", "thanh-ben", "new"), ("cs", "postranni-panel", "new"), ("sv", "sidokolumn", "new"),
            ("id", "bilah-sisi", "new"), ("en", "sidebar-a", "list"), ("en", "right-rail", "list")]
WRAPPERS = ["cms-element-sidebar-filter", "cms-section-sidebar-main-content", "zijbalk-filter"]
#: placement parameters on the result links: (case, the query of each of five links, the placements, kind)
PARAMS = [("a value only one link carries", ["ref=zoeken"] * 2 + ["ref=zoeken_uitgelicht_x9"] + ["ref=zoeken"] * 2,
           {2}, "new"),
          ("a parameter only one link carries", ["", "utm_source=uitgelicht", "", "", ""], {1}, "new"),
          ("a listed value word", ["", "ref=search_results_direct_name", "", "", ""], {1}, "list"),
          ("a per-result id", [f"ref=r{k}" for k in range(5)], set(), "decoy"),
          ("one value on every link", ["ref=zoeken"] * 5, set(), "decoy")]

#: empty result pages: (case, language, value, text, the line that says the search found nothing - "" when none
#: does, kind); a statement of a spaced language no list holds is read only by the model
EMPTY = [
    ("ja statement", "ja", "東京タワー", "検索\n「東京タワー」に一致する商品は見つかりませんでした\nおすすめ",
     "「東京タワー」に一致する商品は見つかりませんでした", "new"),
    ("ru statement", "ru", "чайник", "Поиск\nПо запросу «чайник» ничего не найдено\nПопулярное",
     "По запросу «чайник» ничего не найдено", "new"),
    ("zh statement", "zh", "电钻", "搜索\n没有找到与“电钻”相关的结果\n热门推荐", "没有找到与“电钻”相关的结果", "new"),
    ("tr statement", "tr", "matkap", "Arama\n“matkap” için sonuç bulunamadı\nÖnerilen ürünler",
     "“matkap” için sonuç bulunamadı", "new"),
    ("vi statement", "vi", "xyzzy", "Tìm kiếm\nKhông tìm thấy sản phẩm nào cho “xyzzy”\nGợi ý cho bạn",
     "Không tìm thấy sản phẩm nào cho “xyzzy”", "new"),
    ("id statement", "id", "bor", "Pencarian\nTidak ada hasil untuk “bor”\nProduk populer", "Tidak ada hasil untuk “bor”",
     "new"),
    ("cs statement", "cs", "vrtačka", "Hledání\nPro „vrtačka“ nebylo nic nalezeno\nDoporučujeme",
     "Pro „vrtačka“ nebylo nic nalezeno", "new"),
    ("de list", "de", "Zqurvant Oxlembiq", "Keine Treffer für „Zqurvant Oxlembiq“", "Keine Treffer für „Zqurvant Oxlembiq“",
     "list"),
    ("de list without the query", "de", "Zqurvant Oxlembiq", "Keine Treffer gefunden.\nBeliebt", "Keine Treffer gefunden.",
     "list"),
    ("en positive count", "en", "saddel", "No results for 'saddel'\nShowing results for saddle: 12 products", "",
     "decoy"),
    ("tr positive count", "tr", "matkap", "Arama\n“matkap” için 12 sonuç bulundu\nFiltrele", "", "decoy"),
]

#: the product a lookup opens: (case, value, titles, the right answer: a title, "none" or "tie", kind)
CHOICES = [
    ("pl accessory word", "iPhone 15", ["Etui do iPhone 15", "Szkło hartowane do iPhone 15", "Apple iPhone 15 128 GB"],
     "Apple iPhone 15 128 GB", "new"),
    ("tr accessory word after", "iPhone 15", ["iPhone 15 için kılıf", "iPhone 15 için ekran koruyucu cam",
                                              "Apple iPhone 15 128 GB"], "Apple iPhone 15 128 GB", "new"),
    ("de listed marker", "iPhone 15", ["Hülle für iPhone 15", "Apple iPhone 15 128 GB"], "Apple iPhone 15 128 GB",
     "list"),
    ("tr colour sibling only", "Masa Örtüsü Siyah 38 cm", ["Masa Örtüsü Bej 38 cm", "Peçete Seti 6 Adet"], "none",
     "new"),
    ("tr colour among siblings", "Masa Örtüsü Siyah 38 cm", ["Masa Örtüsü Bej 38 cm", "Masa Örtüsü Siyah 38 cm",
                                                             "Masa Örtüsü Gri 38 cm"], "Masa Örtüsü Siyah 38 cm",
     "new"),
    ("pl listed colour", "Podkładka czarna na stół 38 cm", ["Podkładka beżowa na stół 38 cm", "Serwetki 6 sztuk"],
     "none", "list"),
    ("en tie", "iPhone 15", ["Apple iPhone 15 Pro", "Apple iPhone 15 Plus"], "tie", "decoy"),
    ("de tie: the products and the case put a word in one place", "Galaxy S24",
     ["Galaxy S24 Smartphone 128GB", "Galaxy S24 Smartphone 256GB Onyx", "Galaxy S24 Hülle Silikon"], "tie", "decoy"),
    ("de tie: a series word and a spare part", "Lego 42115",
     ["Lego 42115 Technic Lamborghini Sián", "Lego 42115 Technic Bausatz", "Lego 42115 Ersatzteil Rad"], "tie",
     "decoy"),
]

#: the commit gate: (case, task, label, target, the task asks for the step, kind)
ASKED = [
    ("a dropdown asks for no drop", "choose the size in the dropdown", "Drop table", "", False, "new"),
    ("undelete asks for no delete", "undelete my file", "Delete", "", False, "new"),
    ("a buyer's guide asks for no buy", "show the buyer's guide", "Buy now", "", False, "new"),
    ("delete the draft", "Delete the draft note", "Delete", "draft", True, "list"),
    ("deleting the draft", "deleting the draft", "Delete", "draft", True, "list"),
    ("lösche den Entwurf", "Lösche die Notiz Entwurf", "Löschen", "Entwurf", True, "list"),
    ("pay with a label part", "pay the invoice", "PayNow", "", True, "list"),
    ("tidy up", "tidy up my notes", "Delete", "draft", False, "decoy"),
    ("another target", "delete the old invoice", "Delete", "draft", False, "decoy"),
]


# ---------------------------------------------------------------------------
# the fake shop (a ``dom_page.DomSite``)
# ---------------------------------------------------------------------------

def _shop_class():
    from anyuse.execution import dom_page as D

    class Shop(D.DomSite):
        """``/`` (a search form), ``/zoeken?q=`` (result cards), ``/p/<slug>``. ``mode``: how each card's title-role
        link reads at run time - ``plain``, ``affix`` (``lead`` / ``tail`` around the name), ``label`` (``label`` in
        the title role, the name in another link to the same target), ``date`` (a ``<time>`` in the title role) or
        ``dateline`` (an unmarked date before the name); ``params``: a query string per card link."""

        def __init__(self, lang: str, mode: str = "plain", lead: str = "", tail: str = "", label: str = "",
                     date: str = "", params: Sequence[str] = ()):
            super().__init__([])
            self.query, self.names = CATALOGS[lang]
            self.mode, self.lead, self.tail, self.label, self.date = mode, lead, tail, label, date
            self.params = list(params)

        def slug(self, k: int) -> str:
            return f"item-{k}"

        def card(self, k: int, name: str) -> str:
            q = self.params[k] if k < len(self.params) else ""
            href = f"/p/{self.slug(k)}" + (f"?{q}" if q else "")
            other = ""
            if self.mode == "affix":
                text = " ".join(x for x in (html.escape(self.lead), html.escape(name), html.escape(self.tail)) if x)
            elif self.mode == "label":
                text = html.escape(self.label)
                other = f"<a class=\"titel\" href=\"{href}\">{html.escape(name)}</a>"
            elif self.mode == "date":
                text = f"<time datetime=\"2026-09-20\">{html.escape(self.date)}</time>"
                other = f"<a class=\"titel\" href=\"{href}\">{html.escape(name)}</a>"
            elif self.mode == "dateline":
                text = html.escape(f"{self.date} – {name}")
            else:
                text = html.escape(name)
            return (f"<li class=\"kaart\"><a class=\"foto\" href=\"{href}\"><img src=\"/i/{k}.jpg\" alt=\"\"></a>"
                    f"<h3 class=\"naam\"><a href=\"{href}\">{text}</a></h3>{other}"
                    f"<span class=\"prijs\">€ 49,95</span></li>")

        def page(self, main: str) -> str:
            return (f"<html><head><title>Winkel</title></head><body><header><nav><a href=\"/\">Start</a> "
                    f"<a href=\"/klantenservice\">Klantenservice</a></nav><form action=\"/zoeken\" method=\"get\" "
                    f"role=\"search\"><input type=\"search\" name=\"q\"></form></header><main>{main}</main><footer>"
                    f"<a href=\"/over-ons\">Over ons</a></footer></body></html>")

        def hits(self, q: str) -> List[Tuple[int, str]]:
            from anyuse.execution import web_arch as W
            f = W.fold(q)
            return [(k, n) for k, n in enumerate(self.names) if f and f in W.fold(n)]

        def route(self, url: str):
            sp = urllib.parse.urlsplit(url)
            q = dict(urllib.parse.parse_qsl(sp.query))
            if sp.path in ("", "/"):
                return D.Recorded(url, self.page("<h1>Welkom</h1><p>Gereedschap voor iedereen.</p>")), url
            if sp.path == "/zoeken" and "q" in q:
                hits = self.hits(q["q"])
                cards = "".join(self.card(k, n) for k, n in hits)
                body = (f"<h1>Zoekresultaten</h1><div class=\"resultaten\"><ul class=\"lijst\">{cards}</ul></div>"
                        if hits else "<h1>Zoekresultaten</h1><p>Geen resultaten.</p>")
                return D.Recorded(url, self.page(body)), url
            if sp.path.startswith("/p/item-"):
                k = int(sp.path.rsplit("-", 1)[-1])
                return D.Recorded(url, self.page(f"<h1>{html.escape(self.names[k])}</h1><p>€ 49,95</p>")), url
            return D.Recorded(url, D.NOT_FOUND, {}, 404), url
    return Shop


def _page_of(site, url: str) -> Dict[str, Any]:
    rec, u = site.route(url)
    r = dict(site.read(rec, u))
    r.setdefault("search_urls", [])
    return r


def _shop_trace(site) -> Dict[str, Any]:
    """The demonstration on the plain shop: search the query, report the results as the page lists them."""
    start = BASE + "/"
    rurl = f"{BASE}/zoeken?{urllib.parse.urlencode({'q': site.query})}"
    p0, p1 = _page_of(site, start), _page_of(site, rurl)
    rep = [{"title": n, "url": f"{BASE}/p/{site.slug(k)}"} for k, n in site.hits(site.query)[:5]]
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": site.query,
            "steps": [{"k": 0, "page": p0, "action": {"action": "search", "element": 0, "text": site.query},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 1, "page": p1, "action": {"action": "done", "result": {"results": rep}}, "outcome": {}}],
            "final_page": p1, "result": {"results": rep}, "stop": "done", "forbidden": []}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _digest(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def _run_digest(out: Dict[str, Any]) -> str:
    """A run's decision: its status, why, output and checks (no times)."""
    keep = {k: out.get(k) for k in ("status", "why", "output", "checks", "conflicts", "promo")}
    return _digest(keep)


class Rows:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []
        self.frozen: List[Dict[str, str]] = []

    def add(self, group: str, case: str, kind: str, got: bool, why: str = "", model: str = "off",
            lang: str = "") -> None:
        self.rows.append({"group": group, "case": case, "kind": kind, "model": model, "lang": lang,
                          "got": bool(got), "why": str(why or "")[:240]})

    def freeze(self, case: str, value: Any) -> None:
        self.frozen.append({"case": case, "digest": value if isinstance(value, str) else _digest(value)})


def _tools(trace: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """``(default tool, version-14 tool, why)``: the tree's default build and its version-14 build."""
    from anyuse.execution import demo_compile as C
    g, g14 = C.compile_trace(trace), C.compile_trace(trace, version=14)
    return (g.get("tool") if g.get("ok") else None), (g14.get("tool") if g14.get("ok") else None), \
        str(g.get("why") or g14.get("why") or "")


def _titles(out: Dict[str, Any]) -> List[str]:
    return [str(r.get("title") or "") for r in (out.get("output") or {}).get("results") or []]


def _slugs(out: Dict[str, Any]) -> List[str]:
    return [str(r.get("url") or "").split("?")[0].rsplit("/", 1)[-1] for r in (out.get("output") or {}).get("results")
            or []]


# ---------------------------------------------------------------------------
# the groups
# ---------------------------------------------------------------------------

def shop_rows(R: Rows) -> None:
    """Affixes, labels, dates and placement parameters: a tool compiled on the plain shop, run on the changed one."""
    from anyuse.execution import dom_page as D
    Shop = _shop_class()
    cases: List[Tuple[str, str, str, str, Any]] = []
    for lang, lead, tail, kind in AFFIXES:
        cases.append(("affix", f"{lang} {lead or tail}", lang, kind, Shop(lang, "affix", lead=lead, tail=tail)))
    cases.append(("affix", "nl brand every title starts with", "nl", "decoy", Shop("nl", "plain")))
    for lang, label, kind in LABELS:
        cases.append(("label", f"{lang} {label}", lang, kind, Shop(lang, "label", label=label)))
    for lang, date, kind in DATES:
        cases.append(("date", f"{lang} {date}", lang, kind, Shop(lang, "date", date=date)))
    cases.append(("date", "cs unmarked date before the name", "cs", "decoy", Shop("cs", "dateline", date="20. září 2026")))
    for case, params, promos, kind in PARAMS:
        cases.append(("placement", case, "nl", kind, (Shop("nl", "plain", params=params), promos)))
    built: Dict[str, Tuple[Any, Any, str]] = {}
    for group, case, lang, kind, site in cases:
        promos = None
        if isinstance(site, tuple):
            site, promos = site
        if lang not in built:
            built[lang] = _tools(_shop_trace(Shop(lang)))
        tool, tool14, why = built[lang]
        if tool is None:
            R.add(group, case, kind, False, f"no tool: {why}", lang=lang)
            continue
        out = D.run(site, tool, site.query)
        if group == "placement":
            want = [site.slug(k) for k, _n in site.hits(site.query) if k not in promos]
            got = out.get("status") == "ok" and _slugs(out) == want
            R.add(group, case, kind, got, f"{out.get('status')}: {_slugs(out)}", lang=lang)
        elif case.endswith("unmarked date before the name"):
            want = [f"20. září 2026 – {n}" for _k, n in site.hits(site.query)]
            got = out.get("status") == "ok" and _titles(out) == want
            R.add(group, case, kind, got, f"{out.get('status')}: {_titles(out)[:2]}", lang=lang)
        else:
            want = [n for _k, n in site.hits(site.query)]
            got = out.get("status") == "ok" and _titles(out) == want
            R.add(group, case, kind, got, f"{out.get('status')}: {_titles(out)[:2]} {str(out.get('why'))[:80]}",
                  lang=lang)
        if tool14 is not None:
            R.freeze(f"{group}/{case}", _run_digest(D.run(site, tool14, site.query)))
    for lang, (_t, tool14, _w) in sorted(built.items()):
        if tool14 is not None:
            R.freeze(f"tool/{lang}", tool14)


def sidebar_rows(R: Rows, tests_dir: str) -> None:
    """A sidebar beside the results named in another language: rejected by the start page's differential."""
    sys.path.insert(0, tests_dir)
    import test_demo_sidebar_facets as S10
    for lang, ident, kind in SIDEBARS:
        site = S10.Site(ident=ident)
        tool, tool14, why = _tools(S10.trace(site))
        if tool is None:
            R.add("sidebar", f"{lang} {ident}", kind, False, f"no tool: {why}", lang=lang)
            continue
        ok, notes = True, []
        for v in ("Zukunft", "Möbelmesse"):
            out = S10.run(site, tool, v)
            want = [t for _s, t in S10.hits(v)][:5]
            ok = ok and out.get("status") == "ok" and S10.titles(out) == want
            notes.append(f"{v}: {S10.titles(out)[:2]}")
            if tool14 is not None:
                R.freeze(f"sidebar/{ident}/{v}", _run_digest(S10.run(site, tool14, v)))
        R.add("sidebar", f"{lang} {ident}", kind, ok, "; ".join(notes), lang=lang)
        if tool14 is not None:
            R.freeze(f"sidebar/{ident}/tool", tool14)
    other = [(f"meldung-{k}", f"Meldung Nummer {k}") for k in range(6)]

    class Rotating(S10.Site):
        """The sidebar shows other news on the start page (it rotates its items)."""

        def dom(self, url):
            self.news = S10.NEWS if "keywords=" in url else other
            return super().dom(url)
    for ident in ("sidebar-a", "beliebt"):
        site = Rotating(ident=ident)
        tool, tool14, why = _tools(S10.trace(site))
        ok = tool is not None
        if ok:
            out = S10.run(site, tool, "Zukunft")
            ok = out.get("status") == "ok" and S10.titles(out) == [t for _s, t in S10.hits("Zukunft")][:5]
            why = f"{out.get('status')}: {S10.titles(out)[:2]}"
        R.add("sidebar", f"a sidebar that rotates its items: {ident}", "decoy", ok, why)
        if tool14 is not None:
            R.freeze(f"sidebar/rotating/{ident}/tool", tool14)
            R.freeze(f"sidebar/rotating/{ident}/run", _run_digest(S10.run(site, tool14, "Zukunft")))
    for wrapper in WRAPPERS:
        site = S10.Site(news=[], wrapper=wrapper)
        rep = [{"title": t, "url": f"{S10.BASE}/news/a/{s}"} for s, t in S10.hits("cologne")[:5]]
        tool, tool14, why = _tools(S10.trace(site, reported=rep))
        ok = tool is not None
        if ok:
            out = S10.run(site, tool, "Zukunft")
            ok = out.get("status") == "ok" and S10.titles(out) == [t for _s, t in S10.hits("Zukunft")][:5]
            why = f"{out.get('status')}: {S10.titles(out)[:2]}"
        R.add("sidebar", f"a wrapper around the results: {wrapper}", "decoy", ok, why)
        if tool14 is not None:
            R.freeze(f"sidebar/{wrapper}/tool", tool14)


def _fake_for(lines_yes: Sequence[str]) -> Callable:
    def fake(instructions: str, state: str, obs: Any) -> float:
        line = state.rsplit(">> ", 1)[-1] if ">> " in state else ""
        return 0.95 if line and any(y in line for y in lines_yes) else 0.05
    return fake


def judge_rows(R: Rows, tests_dir: str) -> None:
    """Empty and error result pages, and the product a lookup opens (the result-page judge)."""
    sys.path.insert(0, tests_dir)
    from anyuse.execution import demo_compile as C
    from anyuse.execution import empty_page as EP
    from anyuse.execution import model_check as MC
    from test_demo_compile import BASE as TB, NAV, product_trace, read_of, spec_for
    t = C.compile_trace(product_trace())["tool"]
    t14 = C.compile_trace(product_trace(), version=14)["tool"]
    R.freeze("judge/tool", t14)

    def empty_read(text: str) -> Dict[str, Any]:
        r = read_of(spec_for(f"{TB}/search?q=Zqurvant", recommend=False))
        r["text"] = r["main_text"] = text
        return r

    def judged(tool: Dict[str, Any], read: Dict[str, Any], value: str, model: str, yes: Sequence[str]):
        if model == "off":
            return C.judge_result_page(tool, read, value)
        with MC.scope("product"), MC.using(_fake_for(yes)):
            return C.judge_result_page(tool, read, value)

    for case, lang, value, text, statement, kind in EMPTY:
        for model in MODELS:
            j = judged(t, empty_read(text), value, model, [statement] if statement else [])
            got = (j["status"] == "no_results") == bool(statement)
            R.add("empty", case, kind, got, f"{j['status']}: {str(j.get('why'))[:120]}", model=model, lang=lang)
        R.freeze(f"empty/{case}", C.judge_result_page(t14, empty_read(text), value))

    def result_read(first_line: str) -> Dict[str, Any]:
        r = read_of(spec_for(f"{TB}/search?q=Borealis+Toaster+2+Slice"))
        r["text"] = first_line + "\n" + r["text"]
        return r
    value = "Borealis Toaster 2 Slice"
    r = result_read("Page not found errors in WordPress")
    r["elements"].append({"i": 99, "tag": "a", "text": "Page not found errors in WordPress",
                          "href": TB + "/blog/page-not-found", "area": "main"})
    j = C.judge_result_page(t, r, value)
    R.add("error", "an error phrase in a result's title", "new", j["status"] == "pick", j["status"])
    R.freeze("error/title", C.judge_result_page(t14, r, value))
    chrome = "Pagina niet gevonden? Hulp en contact"
    results = [EP.Page("text", "Borealis Toaster", text=f"{chrome}\nBorealis Toaster 2 Slice\n29,00 €"),
               EP.Page("text", "Acme Kettle", text=f"{chrome}\nAcme Kettle 2000 Steel\n49,99 €")]
    probes = [EP.Page("text", v, text=f"{chrome}\nGeen resultaten voor “{v}”") for v in ("zorquavex", "limbraktu")]
    sig = EP.learn("text", results, probes)
    r = result_read(chrome)
    j = C.judge_result_page(dict(t, empty_page=sig), r, value)
    R.add("error", "an error phrase in the site's chrome (nl)", "new", j["status"] == "pick", j["status"])
    R.freeze("error/chrome", C.judge_result_page(dict(t14, empty_page=sig), r, value))
    r = empty_read("Page not found\nNo results found.\nReport a page not found")
    r["elements"].append({"i": 98, "tag": "a", "text": "Report a page not found", "href": TB + "/kontakt",
                          "area": "main"})
    j = C.judge_result_page(t, r, "Acme Kettle")
    R.add("error", "a real error page whose own link holds the phrase", "decoy",
          j["status"] == "failed" and "reports an error" in str(j.get("why")), j["status"])
    R.freeze("error/link", C.judge_result_page(t14, r, "Acme Kettle"))
    for text in ("Something went wrong. Please try again later.", "Es ist ein Fehler aufgetreten"):
        r = empty_read(text)
        j = C.judge_result_page(t, r, "Acme Kettle")
        R.add("error", f"a real error page: {text[:30]}", "decoy", j["status"] == "failed", j["status"])
        R.freeze(f"error/{text[:20]}", C.judge_result_page(t14, r, "Acme Kettle"))

    def listing(value: str, titles: Sequence[str]) -> Dict[str, Any]:
        nav = [(x, TB + h, "nav", "") for x, h in NAV]
        links = [(x, f"{TB}/p/item-{n}", "main", x) for n, x in enumerate(titles)]
        spec = {"url": f"{TB}/search?q={value}", "title": f"Search: {value}", "h1": [f"Search: {value}"],
                "links": nav + links, "text": f"{len(titles)} results for \"{value}\"\n" + "\n".join(titles),
                "jsonld": []}
        return read_of(spec)
    for case, value, titles, want, kind in CHOICES:
        j = C.judge_result_page(t, listing(value, titles), value)
        if want == "none":
            got = j["status"] == "incomplete" and bool(j.get("not_listed"))
        elif want == "tie":
            got = j["status"] == "incomplete" and len(j.get("ambiguous") or []) == 2
        else:
            got = j["status"] == "pick" and (j.get("pick") or {}).get("title") == want
        R.add("choice", case, kind, got, f"{j['status']}: {(j.get('pick') or {}).get('title')}")
        R.freeze(f"choice/{case}", C.judge_result_page(t14, listing(value, titles), value))


def field_rows(R: Rows, tests_dir: str) -> None:
    """A product's availability in a language no list holds and the site's own price labels."""
    sys.path.insert(0, tests_dir)
    from anyuse.execution import demo_compile as C
    import test_demo_saleprice as SP
    price, price_dom = "749,00 €", '<span class="price">749,00 €</span>'
    box = "{name}\nThương hiệu: Acme\n{prices}\n%s\nThêm vào giỏ hàng\nMô tả sản phẩm"

    class Shop(SP.Shop):
        def __init__(self, per_slug):
            super().__init__(prices=price, prices_dom=price_dom, md_price="749.00")
            self.per_slug = per_slug

        def product(self, url):
            self.availability, self.buy_box = self.per_slug[url.rstrip("/").split("/")[-1]]
            return super().product(url)

    def slugs(demo, v1, v2):
        return {SP.CATALOG[2][0]: demo, SP.CATALOG[0][0]: v1, SP.CATALOG[1][0]: v2}
    vi_in = ("InStock", box % "Còn hàng")
    de = "{name}\n{prices}\n%s\nIn den Warenkorb"
    cases = [
        ("vi in stock by the site's own line", "new", Shop(slugs(vi_in, ("OutOfStock", box % "Hết hàng"), vi_in)),
         "Còn hàng", SP.V2, "InStock"),
        ("vi out of stock by the site's own line", "new", Shop(slugs(vi_in, ("OutOfStock", box % "Hết hàng"), vi_in)),
         "Còn hàng", SP.V1, "OutOfStock"),
        ("de listed in stock", "list", Shop(slugs(("InStock", de % "Auf Lager"), ("OutOfStock", de % "Ausverkauft"),
                                                  ("InStock", de % "Auf Lager"))), "Auf Lager", SP.V2, "InStock"),
        ("de listed sold out", "list", Shop(slugs(("InStock", de % "Auf Lager"), ("OutOfStock", de % "Ausverkauft"),
                                                  ("InStock", de % "Auf Lager"))), "Auf Lager", SP.V1, "OutOfStock"),
        ("a delivery time in the place", "decoy",
         Shop(slugs(vi_in, ("OutOfStock", "{name}\n{prices}\nLieferzeit: 13 Wochen\nMô tả sản phẩm"), vi_in)),
         "Còn hàng", SP.V1, "Lieferzeit: 13 Wochen"),
    ]
    for case, kind, shop, reported, v, want in cases:
        g = C.compile_trace(SP.trace(shop, price="749,00", availability=reported))
        g14 = C.compile_trace(SP.trace(shop, price="749,00", availability=reported), version=14)
        if not g.get("ok"):
            R.add("fields", case, kind, False, f"no tool: {g.get('why')}")
            continue
        out = SP.run(shop, g["tool"], v)
        got = out.get("status") == "ok" and (out.get("output") or {}).get("availability") == want
        R.add("fields", case, kind, got, f"{out.get('status')}: {(out.get('output') or {}).get('availability')}")
        if g14.get("ok"):
            R.freeze(f"fields/{case}/tool", g14["tool"])
            R.freeze(f"fields/{case}/run", _run_digest(SP.run(shop, g14["tool"], v)))
    tr_box = "{name}\nMarka: Acme\n{prices}\nSepete ekle\nÜrün açıklaması"

    class TextPriceShop(SP.Shop):
        def __init__(self, prices):
            super().__init__(prices="", prices_dom="", md_price=None, ld_price=None, buy_box=tr_box)
            self.per_slug = prices

        def product(self, url):
            self.prices = self.per_slug[url.rstrip("/").split("/")[-1]]
            return super().product(url)

    def tr(regular, current):
        return f"Liste fiyatı:\n{regular} €\nİndirimli fiyat:\n{current} €"
    shop = TextPriceShop({SP.CATALOG[2][0]: tr("1.299,00", "999,00"), SP.CATALOG[0][0]: tr("2.499,00", "1.999,00"),
                          SP.CATALOG[1][0]: tr("89,00", "69,00")})
    g = C.compile_trace(SP.trace(shop, price="999,00", availability="Stokta"))
    g14 = C.compile_trace(SP.trace(shop, price="999,00", availability="Stokta"), version=14)
    for v, want in ((SP.V1, 1999.0), (SP.V2, 69.0)):
        out = SP.run(shop, g["tool"], v) if g.get("ok") else {}
        got = out.get("status") == "ok" and (out.get("output") or {}).get("price") == want
        R.add("fields", f"tr selling price by the site's labels ({v[:20]})", "new", got,
              f"{out.get('status')}: {(out.get('output') or {}).get('price')}")
        if g14.get("ok"):
            R.freeze(f"fields/price/{v[:20]}", _run_digest(SP.run(shop, g14["tool"], v)))
    shop = SP.Shop()
    g = C.compile_trace(SP.trace(shop))
    for v in (SP.V1, SP.V2):
        out = SP.run(shop, g["tool"], v) if g.get("ok") else {}
        R.add("fields", f"de listed sale price ({v[:20]})", "list", (out.get("output") or {}).get("price") == 49.99,
              str((out.get("output") or {}).get("price")))


# ---------------------------------------------------------------------------
# a saved web tool's call, its breadth runs, its product site and its heal
# ---------------------------------------------------------------------------

#: a search for this input lists nothing and says so in Vietnamese (no phrase list holds it)
VI_VALUE = "xyzzy"
VI_EMPTY = "Tìm kiếm\nKhông tìm thấy sản phẩm nào cho “xyzzy”\nGợi ý cho bạn"


def _empty_page_class(TD):
    from anyuse.execution import web_arch_run as RR

    class EmptyPage(TD.FakeShopPage):
        async def js(self, expr, read_only=False):
            r = await super().js(expr, read_only)
            if expr == RR.READ_JS and "/search" in self.cur and isinstance(r, dict):
                r = dict(r, text=VI_EMPTY, main_text=VI_EMPTY,
                         elements=[e for e in r["elements"] if e.get("area") != "main"])
            return r
    return EmptyPage


class _Asked:
    """The check model: the line that says nothing was found is the empty statement; one request each."""

    def __init__(self):
        self.lines: List[str] = []

    def __call__(self, instructions: str, state: str, obs: Any):
        line = state.rsplit(">> ", 1)[-1]
        self.lines.append(line)
        return 0.95 if "Không tìm thấy" in line else 0.05, 1, 0.0, ""


async def _no_settle(page) -> None:
    return None


def call_rows(R: Rows, tests_dir: str) -> None:
    """A saved web tool's call over MCP: the check model is asked in the call's scope and each question is counted;
    a re-proof, a proof's replay and a watch run ask none, also under a scope opened around or inside them."""
    sys.path.insert(0, tests_dir)
    import asyncio
    from anyuse.execution import demo_compile as C
    from anyuse.execution import demo_tool as T
    from anyuse.execution import harness_compile as HC
    from anyuse.execution import model_check as MC
    from anyuse.execution import repair_ladder as L
    from anyuse.execution import watch as WA
    from anyuse.execution import web_tool_call as WT
    import test_demo_compile as TD
    import test_mcp_web_tools as TM
    import test_repair_ladder as TL
    import test_watch as TW
    EmptyPage = _empty_page_class(TD)
    t15 = C.compile_trace(TD.product_trace())["tool"]
    t14 = C.compile_trace(TD.product_trace(), version=14)["tool"]
    model = _Asked()
    for tool, kind in ((t15, "new"), (t14, "decoy")):
        model.lines.clear()
        fx = TM.Fixture(tool=tool, fallback=False)
        try:
            fx.route.replay = lambda entry, value, caps, ladder=None: asyncio.run(
                T.run_tool(EmptyPage(), entry["tool"], value, _no_settle, result_wait_s=0.0))
            with MC.using(model):
                out = TM.call(fx.server(), WT.tool_specs(fx.storage)[0]["name"], {"value": VI_VALUE})
        finally:
            fx.close()
        if kind == "new":
            R.add("call", "a version-15 tool's call counts its check-model questions", kind,
                  bool(model.lines) and out.get("model_calls") == len(model.lines),
                  f"asked {len(model.lines)}, model_calls {out.get('model_calls')}, {out.get('status')}")
        else:
            R.add("call", "a version-14 tool's call asks nothing", kind,
                  model.lines == [] and out.get("model_calls") == 0, f"asked {len(model.lines)}")
    store = TL.Store(t15)
    model.lines.clear()
    try:
        ladder = L._Ladder(lambda: EmptyPage(), _no_settle, L.LadderConfig(enabled=True, pacing=False), store.s, None,
                           None, None, None, {"result_wait_s": 0.0})

        async def go():
            await ladder._run(store.s.active_tool(), VI_VALUE)
            n0 = len(model.lines)
            ladder.reproving = True
            await ladder._run(store.s.active_tool(), VI_VALUE)
            return n0
        with MC.scope("product"), MC.using(model):
            n0 = asyncio.run(go())
    finally:
        store.close()
    R.add("call", "a re-proof inside the call asks no check model", "new", n0 >= 1 and len(model.lines) == n0,
          f"the call's run asked {n0}, the re-proof {len(model.lines) - n0}")
    model.lines.clear()

    def proof_replay(t, _site, value):
        with MC.scope("product"):
            return asyncio.run(T.run_tool(EmptyPage(), t, value, _no_settle, result_wait_s=0.0))
    with MC.using(model), MC.scope("agent"):
        HC._replay_without_model(proof_replay, t15, None, VI_VALUE)
    R.add("call", "a proof's replay asks no check model, whatever scope is open", "new", model.lines == [],
          f"asked {len(model.lines)}")
    model.lines.clear()

    def watch_replay(item, tool, caps):
        with MC.scope("product"):
            return asyncio.run(T.run_tool(EmptyPage(), tool, str(item["input"]), _no_settle, result_wait_s=0.0))
    with TW.Store() as s, MC.using(model):
        s.st.add(s.tool(t15), VI_VALUE, "1h")
        WA.run_watch(s.st, watch_replay, now=TW.hours(0))
    R.add("call", "a watch run asks no check model, whatever scope is open", "new", model.lines == [],
          f"asked {len(model.lines)}")


#: (tool, shop, value, parameters): every breadth reader a run reads (``tests/test_breadth_frozen.py``'s cases)
BREADTH_CASES = [
    ("param", "plain", "Akku", {"n": 20}), ("param", "seite", "Akku", {"n": 20}), ("param", "seite", "Bosch", {}),
    ("param", "echo", "Akku", {"n": 20}), ("param", "echo", "Bosch", {}),
    ("cursor", "plain", "Akkuschrauber", {}), ("cursor", "textnum", "Akku", {"n": 20}),
    ("cursor", "echo", "Akku", {"n": 20}),
    ("newest", "plain", "Bosch", {}), ("newest", "markup", "Bosch", {}), ("newest", "mixed", "Bosch", {}),
    ("newest", "mixed", "Akkuschrauber", {}), ("newest", "plain", "Bosch", {"sort": "lowpricefirst"}),
    ("price", "plain", "Bosch", {}), ("price", "mixed", "Akkuschrauber", {}),
    ("price", "plain", "Akkuschrauber", {"sort": "lowpricefirst"}),
]
_MONTHS_DE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
              "November", "Dezember")


def _breadth_shop(F, tool: str, kind: str):
    """``test_demo_breadth_filtered``'s shop with one change (as ``tests/test_breadth_frozen.Variant``)."""
    import re as _re

    class Variant(F.Shop):
        def __init__(self, **kw):
            super().__init__(**dict(kw, **({"total": False} if kind == "echo" else {"per": 2} if kind == "seite"
                                            else {})))

        def card(self, c):
            out = super().card(c)
            y, m, d = c[4].split("-")
            shown = f'<div class="date">{d}.{m}.{y}</div>'
            if kind == "markup":
                out = out.replace(shown, f'<div class="date"><time datetime="{c[4]}">kürzlich</time></div>')
            elif kind == "mixed":
                out = out.replace(shown, f'<div class="date">{int(d)}. {_MONTHS_DE[int(m) - 1]} {y} · Stand '
                                         f'2026-{13 - int(m):02d}-01</div>')
            return out

        def search_dom(self, url, state):
            dom = super().search_dom(url, state)
            if kind == "seite":
                dom = _re.sub(r'<a rel="next" href="[^"]*">Weiter</a>', "", dom)
                dom = _re.sub(r'(<a href="/search\?[^"]*page=\d+[^"]*">)(\d+)</a>', r"\1Seite \2</a>", dom)
            elif kind == "textnum":
                def nav(m):
                    nxt = int(m.group(2))
                    return (f'<a href="/search?{m.group(1)}cursor=c{nxt + 1}">Weiter</a>'
                            f'<a href="/search?{m.group(1)}cursor=c{nxt}">{nxt}</a>')
                dom = _re.sub(r'<a rel="next" href="/search\?([^"]*?)cursor=c(\d+)">Weiter</a>', nav, dom)
            elif kind == "echo":
                q = state.get("q", "")
                n = len(self.hits(q, state.get("sort", ""), state.get("brand", ""), state.get("instock", "")))
                dom = dom.replace("</h1>", f"</h1><p class=\"echo\">„{q}“: {n} sonuç</p>", 1)
            return dom
    return Variant(pager="cursor") if tool == "cursor" else Variant()


def _breadth_decision(out: Dict[str, Any]) -> Dict[str, Any]:
    keep = {k: out.get(k) for k in ("status", "why", "output", "checks", "missing", "fields_seen", "accepted")}
    return json.loads(json.dumps(keep, sort_keys=True, default=str))


def breadth_rows(R: Rows, tests_dir: str, fixture: str) -> None:
    """Breadth tools: a version-14 tool decides every run as recorded with the code of the release before (the record
    in ``fixture``); a version-15 tool numbers the pager by its links, reads the total beside the echo, the markup
    date, the numeric date first and a sort word as a whole word."""
    sys.path.insert(0, tests_dir)
    from anyuse.execution import demo_compile as C
    import test_demo_breadth_filtered as F
    import test_demo_breadth_paged as P
    with open(fixture, encoding="utf-8") as fh:
        rec = json.load(fh)
    for (tool, kind, value, params), was in zip(BREADTH_CASES, rec["runs"]):
        out = F.run(_breadth_shop(F, tool, kind), rec["tools"][tool], value, params=dict(params) or None)
        got = _breadth_decision(out)
        R.add("breadth", f"v14 {tool} on {kind} {value} {json.dumps(params)} decides as recorded", "decoy",
              got == was["decision"], f"{got['status']}: {str(got['why'])[:100]}")
    traces = {"param": P.paged_trace(F.Shop()), "cursor": P.paged_trace(F.Shop(pager="cursor")),
              "newest": F.filtered_trace(F.Shop(), option="Neueste")}
    tools = {k: C.compile_trace(t)["tool"] for k, t in traces.items()}

    def run(tool, kind, value, params=None):
        return F.run(_breadth_shop(F, tool, kind), tools[tool], value, params=params)
    akku = [f"{F.BASE}/p/{c[0]}" for c in F.Shop().hits("Akku")]
    d = run("param", "seite", "Akku", {"n": 20})
    R.add("breadth", "v15: page links named 'Seite N' number the pager", "list",
          d["status"] == "ok" and [r["url"] for r in d["output"]["results"]] == akku, d["why"][:120])
    d = run("cursor", "textnum", "Akku", {"n": 20})
    R.add("breadth", "v15: the page number, not a next-page word that skips a page", "list",
          [r["url"] for r in (d.get("output") or {}).get("results") or []] == akku, d["why"][:120])
    d = run("param", "echo", "Akku", {"n": 20})
    R.add("breadth", "v15: the total beside the quoted input", "list",
          d["checks"]["paged"]["stated_total"] == 7, str(d["checks"]["paged"]["stated_total"]))
    d = run("newest", "markup", "Bosch")
    R.add("breadth", "v15: a card's date in its markup", "list", d["status"] == "ok", d["why"][:120])
    d = run("newest", "mixed", "Bosch")
    R.add("breadth", "v15: a numeric date before a month name", "list",
          d["status"] == "failed" and d["why"].startswith("sort:"), d["why"][:120])
    d = run("newest", "plain", "Bosch", {"sort": "lowpricefirst"})
    R.add("breadth", "v15: 'price' inside a longer word is no price sort", "list",
          "date" in d["why"] and "price" not in d["why"], d["why"][:120])


def site_rows(R: Rows, tests_dir: str) -> None:
    """A product page on a sibling subdomain under a two-label suffix: a version-15 tool's site at run time is its
    registrable domain, as at its compile; a version-14 tool keeps its two-label rule."""
    sys.path.insert(0, tests_dir)
    import re as _re
    from anyuse.execution import demo_compile as C
    from anyuse.execution import web_arch_run as RR
    import test_demo_compile as TD
    www, m_au = "https://www.shop.com.au", "https://m.shop.com.au"

    def moved(obj):
        s = _re.sub(r"https://www\.shop\.test/p/", m_au + "/p/", json.dumps(obj))
        return json.loads(s.replace("https://www.shop.test", www))

    class Sibling(TD.FakeShopPage):
        shown = ""

        async def goto(self, url):
            self.shown = url
            await super().goto(url.replace(www, TD.BASE).replace(m_au, TD.BASE))

        async def js(self, expr, read_only=False):
            m = _re.search(r"\}\)\((\d+), (\"(?:[^\"\\\\]|\\\\.)*\")\)\s*$", str(expr))
            if m and "n.click()" in expr:
                e = next((x for x in moved(await super().js(RR.READ_JS))["elements"] if x["i"] == int(m.group(1))),
                         None)
                if e is None or not e.get("href"):
                    return {"ok": False, "why": "gone"}
                await self.goto(e["href"])
                return {"ok": True}
            return moved(await super().js(expr, read_only))

        async def type_search(self, value, want=None, **kw):
            return moved(await super().type_search(value, want, **kw))

        async def network(self, want_bodies=True):
            return [{"resource_type": "Document", "url": self.shown, "status": 200}]

        async def dom(self):
            return moved(await super().dom())

        async def url(self):
            return self.shown
    tr = moved(TD.product_trace(value="Borealis Toaster 2 Slice", open_slug="borealis-toaster-2"))
    tr["site"] = "www.shop.com.au"
    t15, t14 = C.compile_trace(tr)["tool"], C.compile_trace(tr, version=14)["tool"]
    for value, slug in (("Borealis Toaster 2 Slice", "borealis-toaster-2"),
                        ("Acme Kettle 2000 Steel", "acme-kettle-2000")):
        out = TD.run_tool(Sibling(), t15, value)
        R.add("site", f"v15: a product page on a sibling subdomain ({value})", "new",
              out["status"] == "ok" and (out.get("output") or {}).get("url") == f"{m_au}/p/{slug}",
              f"{out['status']}: {out['why'][:100]}")
        out = TD.run_tool(Sibling(), t14, value)
        R.add("site", f"v14: the two-label rule as before ({value})", "decoy",
              out["status"] == "failed" and "is not on the site" in out["why"], f"{out['status']}: {out['why'][:100]}")


def heal_rows(R: Rows) -> None:
    """A heal re-derives a version-15 region's cards by version 15's own reading: result titles holding a gated word
    the page gives one target; a version-12 region as before."""
    from anyuse.execution import demo_compile as C
    from anyuse.execution import dom_page as D
    from anyuse.execution import repair_relocate as RL
    base = "https://www.haus.test"
    items = [("gartenhaus-a", "Gartenhaus Alva kaufen"), ("gartenhaus-b", "Gartenhaus Birke kaufen"),
             ("gartenhaus-c", "Gartenhaus Cedar kaufen"), ("gartenhaus-d", "Gartenhaus Dalia kaufen")]
    cards = "".join(f'<li class="kachel"><a href="/p/{s}">{t}</a><span>1.299,00 €</span></li>' for s, t in items)
    dom = (f'<html><head><title>Suche</title></head><body><header><a href="/">Start</a><a href="/cart">Warenkorb</a>'
           f'</header><main><h1>Suchergebnisse für Gartenhaus</h1><ul class="liste">{cards}</ul></main>'
           f'<footer><a href="/impressum">Impressum</a></footer></body></html>')
    url = base + "/suche?q=gartenhaus"
    site = D.DomSite([D.Recorded(url, dom)])
    read = dict(site.read(site.pages[C._norm(url)], url))
    anchors = [(f"{base}/p/{s}", t) for s, t in items]
    for cv, kind in ((15, "new"), (12, "decoy")):
        region = {"kind": "cluster", "template": base + "/p/{v}", "regex": r"^https://www\.haus\.test/p/[^/?#]+$",
                  "cards": [{"kind": "card", "chain": ["body", "main", "ul.liste", "li.alt"], "rels": ["a"]}],
                  "cards_version": cv}
        tool = {"version": 15 if cv == 15 else 14, "workflow": "site_search", "site": "www.haus.test",
                "region": region, "search": {"mode": "url", "template": base + "/suche?q={q}"}}
        got = RL.candidates("region", tool, "gartenhaus", {"result": read}, anchors=anchors)
        if cv == 15:
            R.add("heal", "v15 region: the cards found again under titles holding a gated word", kind, len(got) == 1,
                  f"{len(got)} candidate(s)")
        else:
            R.add("heal", "v12 region: re-derived as before (its compile refused these titles too)", kind, got == [],
                  f"{len(got)} candidate(s)")


def gate_rows(R: Rows) -> None:
    from anyuse.execution import agent_harness as H
    for case, task, label, target, want, kind in ASKED:
        got = H.asked_for(task, label, target)
        R.add("asked_for", case, kind, got == want, f"asked_for={got}")


def run_arm(label: str, tree: str, out_path: str, breadth_fixture: str = "") -> None:
    os.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    from anyuse.execution import demo_compile as C
    tests_dir = os.path.join(tree, "tests")
    t0 = time.monotonic()
    R = Rows()
    shop_rows(R)
    sidebar_rows(R, tests_dir)
    judge_rows(R, tests_dir)
    field_rows(R, tests_dir)
    gate_rows(R)
    call_rows(R, tests_dir)
    if breadth_fixture:
        breadth_rows(R, tests_dir, breadth_fixture)
    site_rows(R, tests_dir)
    heal_rows(R)
    meta = {"label": label, "tree": tree, "anyuse": real, "tool_version": C.TOOL_VERSION,
            "seconds": round(time.monotonic() - t0, 2)}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "rows": R.rows, "frozen": R.frozen}, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def compare(paths: Sequence[str]) -> str:
    arms: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        arms[d["meta"]["label"]] = d
    labels = [k for k in ("OLD", "NEW") if k in arms] or list(arms)
    lines: List[str] = []
    groups: List[str] = []
    for lab in labels:
        for r in arms[lab]["rows"]:
            if r["group"] not in groups:
                groups.append(r["group"])
    lines.append("right / cases, per arm (tree, check model), by kind of case: new | list | decoy")
    cols = [(lab, m) for lab in labels for m in MODELS]
    lines.append(f"{'group':<12}" + "".join(f"{lab + ' ' + m:>24}" for lab, m in cols))

    def cell(rows: Sequence[Dict[str, Any]]) -> str:
        parts = []
        for kind in ("new", "list", "decoy"):
            ks = [r for r in rows if r["kind"] == kind]
            parts.append(f"{sum(r['got'] for r in ks)}/{len(ks)}")
        return " | ".join(parts)

    def rows_of(lab: str, m: str, g: str) -> List[Dict[str, Any]]:
        # a group without a model arm counts its one arm under both columns
        rs = [r for r in arms[lab]["rows"] if (g == "all" or r["group"] == g)]
        return [r for r in rs if r["model"] == m or (r["model"] == "off" and not any(
            x["model"] == m and x["group"] == r["group"] for x in rs))]
    for g in groups + ["all"]:
        lines.append(f"{g:<12}" + "".join(f"{cell(rows_of(lab, m, g)):>24}" for lab, m in cols))
    lines.append("")
    for lab in labels:
        for m in MODELS:
            wrong = [r for r in rows_of(lab, m, "all") if not r["got"]]
            lines.append(f"{lab} model={m}: {len(wrong)} wrong")
            for r in wrong:
                lines.append(f"  {r['kind']}: {r['group']}: {r['case']} ({r['why'][:120]})")
    verdict = True
    if "OLD" in arms and "NEW" in arms:
        old = {(r["group"], r["case"], r["model"]): r for r in arms["OLD"]["rows"]}
        regress = [r for r in arms["NEW"]["rows"] if not r["got"] and old.get((r["group"], r["case"], r["model"]),
                                                                              {}).get("got")]
        lines.append("")
        lines.append(f"cases OLD decided right and NEW wrong: {len(regress)}"
                     + "".join(f"\n  {r['group']}: {r['case']} ({r['model']})" for r in regress))
        bad_list = [r for r in arms["NEW"]["rows"] if r["kind"] in ("list", "decoy") and not r["got"]]
        lines.append(f"list and decoy cases NEW decided wrong: {len(bad_list)}")
        fo = {f["case"]: f["digest"] for f in arms["OLD"].get("frozen") or []}
        fn = {f["case"]: f["digest"] for f in arms["NEW"].get("frozen") or []}
        diff = sorted(k for k in set(fo) | set(fn) if fo.get(k) != fn.get(k))
        lines.append(f"frozen: {len(fn)} version-14 tools and decisions, {len(diff)} differ from OLD"
                     + "".join(f"\n  {k}" for k in diff))
        lines.append("versions: " + ", ".join(f"{lab} builds {arms[lab]['meta']['tool_version']}" for lab in labels))
        lines.append("seconds: " + ", ".join(f"{lab} {arms[lab]['meta']['seconds']}" for lab in labels))
        verdict = not regress and not bad_list and not diff
        lines.append("PASS" if verdict else "FAIL")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-generalize-demo-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "generalize_demo_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    # the version-14 breadth tools and their runs' decisions recorded with the code of the release before (the NEW
    # tree's record): both arms replay them
    fixture = args.breadth_fixture or os.path.join(trees["NEW"], "testdata", "breadth_frozen", "decisions.json")
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
            "ANYUSE_MODEL_CHECKS": "none", "ANYUSE_ATTRIBUTE_VERIFY": "off",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{label}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--label", label,
                               "--tree", tree, "--out-file", path, "--breadth-fixture", fixture], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as fh:
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
    r.add_argument("--breadth-fixture", default="")
    a = sub.add_parser("arm")
    a.add_argument("--label", required=True, choices=["OLD", "NEW"])
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    a.add_argument("--breadth-fixture", default="")
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.label, os.path.abspath(args.tree), os.path.abspath(args.out_file),
                os.path.abspath(args.breadth_fixture) if args.breadth_fixture else "")
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
