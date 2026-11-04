"""Offline A/B bench: empty answers judged by the site's own empty page.

The question: does a compiled web tool answer "no results" on sites whose empty
page is in a language the statement rule's lists do not cover, without ever
serving a broken, erroring or wrong page as "no results"? Every site is a
synthetic shop served by a fake transport (the compiled request) and a fake
driver (the browser replay); ``run_compiled`` runs each case. Nothing opens a
socket, starts a browser or calls a model; the model arms use a fake.

Usage::

    python tests/bench/empty_page_bench.py run --old <tree> --new <tree> \
        [--python <interpreter>] [--out <dir>]

``--old`` is a checkout at the commit the feature branch forked from, ``--new``
the feature checkout. The script copies itself into ``--out`` (outside both
trees) and runs that copy once per arm with the same interpreter. Each run's
PYTHONPATH is a pkgroot directory holding an ``anyuse`` symlink to the tree,
then the tree's ``tools/guarded_tests`` (whose sitecustomize refuses sockets).
HOME and every AnyUse data path point into ``--out``. ``compare`` prints the table.

Arms:

* ``OLD``: the old code (the statement rule only).
* ``NEW``: the new code; the tool is compiled by it, so its proof learns the
  site's empty page. No model.
* ``NEW-lazy``: the new code, a tool compiled without the made-up searches
  (``ANYUSE_EMPTY_PROBE=off``): it learns its empty page at its first empty
  answer. Each case starts from the stored tool again, so each pays that once.
* ``NEW-model``: the new code with no made-up searches at all and a fake check
  model: the model step alone, then the statement rule.
* ``NEW-full``: ``NEW`` plus the fake check model.

The fake model reads like a careful one: yes only for the site's own statement
about the asked input when none of the lines it is shown reports the site's
error, no otherwise, and no opinion for one question in six.

Sites: Turkish, Polish, Vietnamese, Indonesian, Hindi, Thai, Swahili, Dutch and
Portuguese (outside the lists), and English, German, Russian and Japanese (in
them). Indonesian, Thai and Portuguese empty pages do not name the input. Every
page carries a help line in the site's language that names the input and says
what to do when nothing is found. ``en-help`` is the English shop with a help
line the statement rule takes ("Seeing no results found for x is rare: write to
us"), on every page from the start.

Cases (gold in brackets), each through the compiled request (``request``) and,
with the request not answering, through the browser replay (``browser``):

* ``R``: a result list [the items];
* ``E``: an input the shop does not have [empty];
* ``E-suggest``: an input one letter off a term the shop has: its empty page
  adds a "did you mean" line linking to the search for that term [empty];
* ``E-suggest-inline``: the same, with the suggestion in the statement's own
  sentence [empty];
* ``D-broken``: the rows' class renamed: the selector reads nothing [fail];
* ``D-broken-stmt``: the same, with the site's own empty statement beside the
  rows [fail];
* ``D-blank``: the search summary only: no rows, no statement [fail];
* ``D-error``: a 200 page with an error message in the site's language [fail];
* ``D-error-stmt``: the error message beside the empty statement [fail];
* ``D-other``: the summary names the input, the statement another term [fail]
  (not on a site whose empty page names no term: that is ``D-ignored``);
* ``D-ignored``: the site no longer reads the input: every search shows the
  page for an empty input [fail];
* ``GAP-help`` (``en-help`` only): the rows' class renamed, the help line still
  naming the input [fail].

The drift is site-wide: the made-up searches and the input known to have
results see it too. Known gaps of the old rule are tagged ``old_gap``:
``GAP-help``; ``D-broken-stmt`` in a listed language (a statement beside rows
the selector no longer reads); ``D-other`` and ``D-ignored`` on the compiled
request in a listed language (the old raw-response rule read a 160-character
window, which reached the input in the summary or the help line); and
``D-error-stmt`` in German, whose error message no list holds.

Pass: no false empty answer in any arm (for OLD, outside the ``old_gap``
cases), NEW answers ``E`` on every site outside the lists, and NEW answers
``E-suggest`` and ``E-suggest-inline`` on every site.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Any, Dict, List, Optional

SEL = "ul.liste li.item"
ARMS = ("OLD", "NEW", "NEW-lazy", "NEW-model", "NEW-full")
RUNGS = ("request", "browser")
GROUPS = ("R", "E", "E-suggest", "E-suggest-inline", "D-broken", "D-broken-stmt", "D-blank",
          "D-error", "D-error-stmt", "D-other", "D-ignored", "GAP-help")
MODES = {"R": "normal", "E": "normal", "E-suggest": "suggest", "E-suggest-inline": "suggest_inline",
         "D-broken": "broken", "D-broken-stmt": "broken_stmt", "D-blank": "blank", "D-error": "error",
         "D-error-stmt": "error_stmt", "D-other": "other", "D-ignored": "ignored", "GAP-help": "broken"}
#: groups whose gold answer is "empty"
EMPTY_GROUPS = ("E", "E-suggest", "E-suggest-inline")

#: one shop per language: catalogue (compile, two validation inputs, one more), an input it
#: does not have, its summary, empty statement, help line, error message and chrome
SITES: Dict[str, Dict[str, Any]] = {
    "tr": {"listed": False, "terms": ["elma", "armut", "kiraz", "incir"], "absent": "karpuz",
           "summary": "“{q}” için sonuçlar", "empty": "“{q}” için sonuç bulunamadı.",
           "help": "“{q}” için sonuç bulamazsanız bize yazın.",
           "error": "Bir hata oluştu. Lütfen daha sonra tekrar deneyin.",
           "chrome": ["Ana sayfa", "Kampanyalar", "Bizi takip edin"], "row": "{q} {n}"},
    "pl": {"listed": False, "terms": ["jabłko", "gruszka", "wiśnia", "śliwka"], "absent": "arbuz",
           "summary": "Wyniki dla „{q}”", "empty": "Brak wyników dla „{q}”.",
           "help": "Jeśli nie widzisz wyników dla „{q}”, napisz do nas.",
           "error": "Wystąpił błąd. Spróbuj ponownie później.",
           "chrome": ["Strona główna", "Promocje", "Obserwuj nas"], "row": "{q} {n}"},
    "vi": {"listed": False, "terms": ["táo", "lê", "nho", "cam"], "absent": "dưa",
           "summary": "Kết quả cho “{q}”", "empty": "Không tìm thấy sản phẩm nào cho “{q}”.",
           "help": "Nếu không thấy kết quả cho “{q}”, hãy liên hệ với chúng tôi.",
           "error": "Đã xảy ra lỗi, vui lòng thử lại sau.",
           "chrome": ["Trang chủ", "Khuyến mãi", "Theo dõi chúng tôi"], "row": "{q} {n}"},
    "id": {"listed": False, "terms": ["apel", "pir", "anggur", "jeruk"], "absent": "semangka",
           "summary": "Hasil untuk “{q}”", "empty": "Produk tidak ditemukan.",
           "help": "Jika hasil untuk “{q}” tidak muncul, hubungi kami.",
           "error": "Terjadi kesalahan. Silakan coba lagi nanti.",
           "chrome": ["Beranda", "Promo", "Ikuti kami"], "row": "{q} {n}"},
    "hi": {"listed": False, "terms": ["चाय", "कॉफ़ी", "शहद", "चावल"], "absent": "तरबूज़",
           "summary": "“{q}” के परिणाम", "empty": "“{q}” के लिए कोई परिणाम नहीं मिला।",
           "help": "अगर “{q}” के परिणाम न दिखें, तो हमें लिखें।",
           "error": "कोई त्रुटि हुई। कृपया बाद में फिर से प्रयास करें।",
           "chrome": ["होम", "ऑफ़र", "हमें फ़ॉलो करें"], "row": "{q} {n}"},
    "th": {"listed": False, "terms": ["กาแฟ", "ชาเขียว", "น้ำผึ้ง", "ข้าวสาร"], "absent": "แตงโม",
           "summary": "ผลการค้นหา “{q}”", "empty": "ไม่พบสินค้าที่ตรงกับการค้นหาของคุณ",
           "help": "หากไม่พบ “{q}” โปรดติดต่อเรา",
           "error": "เกิดข้อผิดพลาด โปรดลองอีกครั้งภายหลัง",
           "chrome": ["หน้าแรก", "โปรโมชั่น", "ติดตามเรา"], "row": "{q} {n}"},
    "sw": {"listed": False, "terms": ["chai", "kahawa", "asali", "mchele"], "absent": "tikiti",
           "summary": "Matokeo ya “{q}”", "empty": "Hakuna matokeo ya “{q}”.",
           "help": "Ukikosa matokeo ya “{q}”, tuandikie.",
           "error": "Hitilafu imetokea. Tafadhali jaribu tena baadaye.",
           "chrome": ["Nyumbani", "Ofa", "Tufuate"], "row": "{q} {n}"},
    "nl": {"listed": False, "terms": ["appel", "peer", "kers", "pruim"], "absent": "meloen",
           "summary": "Resultaten voor “{q}”", "empty": "Geen resultaten voor “{q}”.",
           "help": "Zie je geen resultaten voor “{q}”? Neem contact op.",
           "error": "Er is een fout opgetreden. Probeer het later opnieuw.",
           "chrome": ["Home", "Aanbiedingen", "Volg ons"], "row": "{q} {n}"},
    "pt": {"listed": False, "terms": ["maçã", "pera", "uva", "laranja"], "absent": "melancia",
           "summary": "Resultados para “{q}”", "empty": "Nenhum produto encontrado.",
           "help": "Se não encontrar resultados para “{q}”, fale conosco.",
           "error": "Ocorreu um erro. Tente novamente mais tarde.",
           "chrome": ["Início", "Ofertas", "Siga-nos"], "row": "{q} {n}"},
    "en": {"listed": True, "terms": ["apple", "pear", "cherry", "plum"], "absent": "melon",
           "summary": "Results for “{q}”", "empty": "No results found for “{q}”.",
           "help": "If there are no results for “{q}”, contact us.",
           "error": "Something went wrong. Please try again later.",
           "chrome": ["Home", "Deals", "Follow us"], "row": "{q} {n}"},
    "de": {"listed": True, "terms": ["apfel", "birne", "kirsche", "pflaume"], "absent": "melone",
           "summary": "Ergebnisse für „{q}“", "empty": "Keine Ergebnisse für „{q}“ gefunden.",
           "help": "Wenn Sie zu „{q}“ keine Ergebnisse finden, schreiben Sie uns.",
           "error": "Ein Fehler ist aufgetreten. Bitte versuchen Sie es später erneut.",
           "chrome": ["Start", "Angebote", "Folgen Sie uns"], "row": "{q} {n}"},
    "ru": {"listed": True, "terms": ["яблоко", "груша", "вишня", "слива"], "absent": "арбуз",
           "summary": "Результаты по запросу «{q}»", "empty": "По запросу «{q}» ничего не найдено.",
           "help": "Если по запросу «{q}» ничего не найдено, напишите нам.",
           "error": "Произошла ошибка. Попробуйте позже.",
           "chrome": ["Главная", "Акции", "Мы в соцсетях"], "row": "{q} {n}"},
    "ja": {"listed": True, "terms": ["りんご", "なし", "さくらんぼ", "すもも"], "absent": "すいか",
           "summary": "「{q}」の検索結果", "empty": "「{q}」に一致する商品は見つかりませんでした",
           "help": "「{q}」が見つからない場合はお問い合わせください",
           "error": "エラーが発生しました。しばらくしてから再度お試しください。",
           "chrome": ["ホーム", "セール", "フォローする"], "row": "{q} {n}"},
}
#: each shop's "did you mean", for an input one letter off a term it has; {s} is the link to that search
SUGGEST = {"tr": "Bunu mu demek istediniz: {s}?", "pl": "Czy chodziło Ci o: {s}?",
           "vi": "Có phải bạn muốn tìm: {s}?", "id": "Mungkin maksud Anda: {s}?",
           "hi": "क्या आपका मतलब था: {s}?", "th": "คุณหมายถึง {s} หรือไม่",
           "sw": "Ulimaanisha: {s}?", "nl": "Bedoelde je: {s}?", "pt": "Você quis dizer: {s}?",
           "en": "Did you mean: {s}?", "de": "Meinten Sie: {s}?",
           "ru": "Возможно, вы имели в виду: {s}?", "ja": "もしかして: {s}"}
for _code, _s in SITES.items():
    _s["suggest"] = SUGGEST[_code]
#: the English shop again, with a help line on every page that the statement rule takes
SITES["en-help"] = dict(SITES["en"], help="Seeing no results found for “{q}” is rare: write to us.")
OTHER = "kafka"


def near(code: str) -> str:
    """An input one letter off the shop's first term: it has no results, and the shop suggests the term."""
    term = SITES[code]["terms"][0]
    return term + term[-1]


def rows_for(site: Dict[str, Any], q: str) -> List[str]:
    return [site["row"].format(q=q, n=n) for n in ("1", "2", "3", "4")]


def page(code: str, q: str, mode: str) -> str:
    """The shop's page for ``q`` while the site is in ``mode`` (the drift is site-wide)."""
    s = SITES[code]
    known = q in s["terms"]
    esc = q.replace("<", "&lt;")
    summary = f"<p class='ozet'>{s['summary'].format(q=esc)}</p>"
    stmt = f"<p class='bos'>{s['empty'].format(q=esc)}</p>"

    def rows(cls: str) -> str:
        return "<ul class='liste'>" + "".join(
            f"<li class='{cls}'><a href='/p/{i}'>{t}</a></li>"
            for i, t in enumerate(rows_for(s, esc))) + "</ul>"
    error = f"<div class='alert'><p>{s['error']}</p></div>"
    term = s["terms"][0]
    link = f"<a href='/s?q={urllib.parse.quote(term)}'>{term}</a>"
    if mode in ("suggest", "suggest_inline") and q == near(code):
        said = s["suggest"].format(s=link)
        body = summary + (stmt + f"<p class='oneri'>{said}</p>" if mode == "suggest" else
                          stmt.replace("</p>", f" {said}</p>"))
    elif mode in ("normal", "suggest", "suggest_inline"):
        body = summary + (rows("item") if known else stmt)
    elif mode == "broken":
        body = summary + (rows("item-v2") if known else stmt)
    elif mode == "broken_stmt":
        body = summary + (rows("item-v2") + stmt if known else stmt)
    elif mode == "blank":
        body = summary
    elif mode == "error":
        body = error
    elif mode == "error_stmt":
        body = error + stmt
    elif mode == "other":
        body = summary + f"<p class='bos'>{s['empty'].format(q=OTHER)}</p>"
    elif mode == "ignored":
        body = f"<p class='ozet'>{s['summary'].format(q='')}</p><p class='bos'>{s['empty'].format(q='')}</p>"
    else:
        raise ValueError(mode)
    help_line = s["help"].format(q=esc)
    nav = "".join(f"<a href='/{i}'>{c}</a> " for i, c in enumerate(s["chrome"][:2]))
    return (f"<html><body><header><nav>{nav}</nav><form><input name='q' value='{esc}'></form>"
            f"</header><main>{body}</main><footer><p class='yardim'>{help_line}</p>"
            f"<p>{s['chrome'][2]}</p></footer></body></html>")


class Site:
    """The shop's server, logging every input it was asked for."""

    def __init__(self, code: str):
        self.code, self.mode, self.log = code, "normal", []

    def html(self, q: str) -> str:
        return page(self.code, q, self.mode)

    def send(self, method, url, headers, body, timeout_s):
        from anyuse.execution.api_step import ApiResponse
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.log.append(q)
        return ApiResponse(200, {"content-type": "text/html; charset=utf-8"}, self.html(q))


class Down:
    """The compiled request's transport while the browser replay is measured."""

    def send(self, method, url, headers, body, timeout_s):
        raise ConnectionRefusedError("the bench sends nothing")


class Driver:
    def __init__(self, site: Site):
        self.site, self.opened = site, []

    def open(self, url, settle_s, timeout_s):
        from anyuse.execution.cdp_replay import PageResult
        from anyuse.execution.html_extract import parse
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        self.opened.append(q)
        dom = self.site.html(q)
        return PageResult(url=url, dom=dom, text=parse(dom, visible_only=True).text(),
                          status=200, elapsed_ms=5.0)


class Storage:
    def __init__(self, base: str):
        self.base_dir = base

    def get_workflow_dir(self, workflow_id):
        return os.path.join(self.base_dir, f"workflow_{workflow_id}")

    def get(self, name):
        return None


def host(code: str) -> str:
    return f"shop-{code}.test"


def recording(code: str, q: str) -> Dict[str, Any]:
    url = f"https://{host(code)}/s?q={urllib.parse.quote(q)}"
    return {"param": {"name": "q", "value": q}, "final_url": url,
            "ui": {"items": rows_for(SITES[code], q), "text": ""},
            "network": [{"url": url, "method": "GET", "status": 200, "mime": "text/html",
                         "body": page(code, q, "normal")}]}


# ---------------------------------------------------------------------------
# the fake check model
# ---------------------------------------------------------------------------

def fake_model(code: str):
    """A careful reader: yes only for the shop's own statement about the asked input when
    none of the lines it is shown reports the site's error; no opinion for one question in
    six; no otherwise."""
    tmpl, error = SITES[code]["empty"], SITES[code]["error"]
    rx = re.compile("^" + re.escape(tmpl).replace(re.escape("{q}"), "(.*)") + "$")

    def ask(instructions: str, state: str, obs) -> Any:
        target = instructions.split("search for '", 1)[-1].split("'", 1)[0]
        shown = state.splitlines()
        line = shown[-1][3:] if shown else ""
        if sum(map(ord, line)) % 6 == 0:
            return None
        m = rx.match(line)
        if m and ("{q}" not in tmpl or m.group(1).replace("’", "'") == target) \
                and not any(error in ln for ln in shown[:-1]):
            return 0.95
        return 0.05
    return ask


# ---------------------------------------------------------------------------
# one arm
# ---------------------------------------------------------------------------

def compile_site(code: str, site: Site):
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import compile_and_verify
    terms = SITES[code]["terms"]
    compiled, proof = compile_and_verify(recording(code, terms[0]),
                                         [recording(code, terms[1]), recording(code, terms[2])],
                                         executor=ApiStepExecutor(transport=site))
    return compiled, proof


def run_case(arm: str, code: str, record: Dict[str, Any], group: str, rung: str, scratch: str,
             n: int) -> Dict[str, Any]:
    from anyuse.execution.api_compile import persist_compiled
    from anyuse.execution.api_permission import acknowledge_host
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.mcpserver.compiled_tools import run_compiled
    s = SITES[code]
    base = os.path.join(scratch, f"case_{n}")
    os.makedirs(base, exist_ok=True)
    os.environ["ANYUSE_BLOCKED_HOSTS_FILE"] = os.path.join(base, "blocked.json")
    storage = Storage(base)
    persist_compiled(storage, "shop_search", json.loads(json.dumps(record)))
    hosts = os.path.join(base, "hosts.json")
    acknowledge_host(host(code), path=hosts)
    site = Site(code)
    site.mode = MODES[group]
    q = (near(code) if group in ("E-suggest", "E-suggest-inline") else
         s["absent"] if group in ("E", "D-error-stmt", "D-other") else s["terms"][3])
    driver = Driver(site)
    kw: Dict[str, Any] = {"storage": storage, "hosts_path": hosts}
    if rung == "request":
        kw["executor"] = ApiStepExecutor(transport=site)
    else:
        kw["executor"] = ApiStepExecutor(transport=Down())
        kw["browser_executor"] = ReplayExecutor(driver=driver)
    if arm in ("NEW-model", "NEW-full"):
        from anyuse.execution import model_check as MC
        with MC.using(fake_model(code)):
            out = run_compiled("shop_search", {"q": q}, **kw)
    else:
        out = run_compiled("shop_search", {"q": q}, **kw)
    items = (out.get("result") or {}).get("items") if isinstance(out.get("result"), dict) else None
    got = ("fail" if not out.get("ok") else "empty" if items == [] else "items")
    gold = "items" if group == "R" else "empty" if group in EMPTY_GROUPS else "fail"
    old_gap = (group == "GAP-help"
               # the statement beside rows the selector no longer reads, in a listed language
               or (group == "D-broken-stmt" and s["listed"])
               # the compiled request's 160-character window reaches the input elsewhere
               or (group in ("D-other", "D-ignored") and s["listed"] and rung == "request")
               # an error message no list holds beside a listed statement
               or (group == "D-error-stmt" and code == "de"))
    sent = len(site.log) + len(driver.opened)
    return {"arm": arm, "site": code, "listed": s["listed"], "group": group, "rung": rung,
            "value": q, "gold": gold, "got": got, "old_gap": old_gap,
            "false_empty": got == "empty" and gold != "empty",
            "served_by": out.get("served_by") or "",
            "empty_by": str((out.get("result_contract") or {}).get("empty_by") or ""),
            "requests": sent, "model_calls": int(out.get("model_calls") or 0),
            "error": str(out.get("error") or "")[:200]}


def run_arm(arm: str, tree: str, out_path: str) -> None:
    import socket

    def refuse(*a, **kw):
        raise OSError("the bench resolves no names")
    socket.getaddrinfo = refuse
    import anyuse
    real = os.path.realpath(os.path.dirname(anyuse.__file__))
    if real != os.path.realpath(tree):
        raise SystemExit(f"anyuse resolves to {real}, not {tree}")
    from anyuse.execution.api_compile import compiled_record
    from anyuse.execution import result_contract as RC
    from anyuse.execution.cdp_replay import ReplayTemplate
    scratch = tempfile.mkdtemp(prefix=f"arm-{arm}-", dir=os.path.dirname(out_path))
    os.environ["ANYUSE_AUTO_HEAL"] = "off"
    rows: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"arm": arm, "tree": tree, "anyuse": real, "learned": {}}
    n = 0
    for code in SITES:
        os.environ["ANYUSE_EMPTY_PROBE"] = "on" if arm in ("NEW", "NEW-full") else "off"
        site = Site(code)
        compiled, proof = compile_site(code, site)
        if not proof.passed:
            rows.append({"arm": arm, "site": code, "group": "COMPILE", "got": "fail",
                         "error": proof.reason[:200]})
            continue
        sig = ((RC.contract_of(compiled.template) or {}).get("empty") or {}).get("page")
        meta["learned"][code] = {"kind": (sig or {}).get("kind"), "marks": (sig or {}).get("marks"),
                                 "echo": (sig or {}).get("echo"), "why": (sig or {}).get("why"),
                                 "compile_requests": len(site.log)}
        record = compiled_record(compiled.template, proof.to_dict(), workflow="shop_search",
                                 source={"site": host(code), "origin": "bench"})
        record["browser"] = {"template": ReplayTemplate(
            url=f"https://{host(code)}/s?q={{q}}", params=["q"], extract={"items": SEL},
            expect={"selector": SEL, "min_items": 1, RC.CONTRACT_KEY: RC.replay_contract("q")}
        ).to_dict()}
        os.environ["ANYUSE_EMPTY_PROBE"] = "off" if arm == "NEW-model" else "on"
        for group in GROUPS:
            if (group == "GAP-help") != (code == "en-help"):
                continue
            if group == "D-other" and "{q}" not in SITES[code]["empty"]:
                # an empty page that names no term is the same for every input: D-ignored
                continue
            for rung in RUNGS:
                n += 1
                rows.append(run_case(arm, code, record, group, rung, scratch, n))
    with open(out_path, "w") as fh:
        json.dump({"meta": meta, "rows": rows}, fh, indent=1, ensure_ascii=False, default=str)
    print(f"{arm}: {len(rows)} rows -> {out_path}")


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------

def compare(paths: List[str]) -> str:
    rows: List[Dict[str, Any]] = []
    metas: Dict[str, Any] = {}
    for p in paths:
        with open(p) as fh:
            d = json.load(fh)
        rows.extend(d["rows"])
        metas[d["meta"]["arm"]] = d["meta"]
    arms = [a for a in ARMS if a in metas]
    lines = ["# empty answers: OLD vs NEW", ""]
    hdr = f"{'group':<16}" + "".join(f"{a:>22}" for a in arms)
    lines += ["right answers / cases (false empty answers), both rungs", hdr]
    for group in GROUPS:
        cells = []
        for a in arms:
            rs = [r for r in rows if r["arm"] == a and r.get("group") == group]
            right = sum(1 for r in rs if r["got"] == r["gold"])
            fe = sum(1 for r in rs if r.get("false_empty"))
            cells.append(f"{right}/{len(rs)} ({fe})")
        lines.append(f"{group:<16}" + "".join(f"{c:>22}" for c in cells))
    lines.append("")
    lines.append("E (an input the shop does not have) answered empty, per site and rung:")
    lines.append(f"{'site':<8}{'listed':>8}" + "".join(f"{a:>22}" for a in arms))
    for code in SITES:
        if not any(r.get("site") == code and r.get("group") == "E" for r in rows):
            continue
        cells = []
        for a in arms:
            rs = [r for r in rows if r["arm"] == a and r.get("site") == code and r.get("group") == "E"]
            cells.append(" ".join(f"{r['rung'][0]}:{'Y' if r['got'] == 'empty' else '-'}"
                                  f"{('/' + r['empty_by'][:4]) if r['empty_by'] else ''}" for r in rs))
        lines.append(f"{code:<8}{str(SITES[code]['listed']):>8}" + "".join(f"{c:>22}" for c in cells))
    lines.append("")
    lines.append("requests per call (site requests + page loads), median / max:")
    for a in arms:
        parts = []
        for label, sel in (("E", lambda r: r.get("group") == "E"),
                           ("decoys", lambda r: str(r.get("group", "")).startswith(("D-", "GAP")))):
            for rung in RUNGS:
                rs = sorted(r["requests"] for r in rows if r["arm"] == a and sel(r)
                            and r.get("rung") == rung and "requests" in r)
                if rs:
                    parts.append(f"{label} {rung} {rs[len(rs) // 2]} / {rs[-1]}")
        mc = sum(r.get("model_calls", 0) for r in rows if r["arm"] == a)
        parts.append(f"model calls {mc}")
        lines.append(f"  {a:<10} " + "; ".join(parts))
    lines.append("")
    lines.append("learned at proof (NEW): " + ", ".join(
        f"{c}={v.get('kind')}{'' if v.get('echo') is None else ('/echo' if v.get('echo') else '/no-echo')}"
        for c, v in (metas.get("NEW") or {}).get("learned", {}).items()))
    fails = []
    for a in arms:
        fe = [r for r in rows if r["arm"] == a and r.get("false_empty")
              and not (a == "OLD" and r.get("old_gap"))]
        if fe:
            fails.append(f"{a}: {len(fe)} false empty answer(s): " + ", ".join(
                f"{r['site']}/{r['group']}/{r['rung']}" for r in fe[:8]))
    gaps = [r for r in rows if r["arm"] == "OLD" and r.get("false_empty") and r.get("old_gap")]
    lines.append(f"OLD false empty answers on the old rule's known gaps: {len(gaps)} ("
                 + ", ".join(f"{r['site']}/{r['group']}/{r['rung']}" for r in gaps) + ")")
    unanswered = [r for r in rows if r["arm"] == "NEW" and r.get("group") == "E"
                  and not r.get("listed") and r["got"] != "empty"]
    if unanswered:
        fails.append("NEW leaves unlisted-language empty pages unanswered: " + ", ".join(
            f"{r['site']}/{r['rung']}" for r in unanswered))
    # a "did you mean" empty page: NEW answers it on every site, as OLD does in the listed languages
    missed = [r for r in rows if r["arm"] == "NEW" and r.get("group") in ("E-suggest", "E-suggest-inline")
              and r["got"] != "empty"]
    if missed:
        fails.append("NEW leaves empty pages with a suggestion unanswered: " + ", ".join(
            f"{r['site']}/{r['group']}/{r['rung']}" for r in missed))
    compile_fail = [r for r in rows if r.get("group") == "COMPILE"]
    if compile_fail:
        fails.append("compile failed: " + ", ".join(f"{r['arm']}/{r['site']}" for r in compile_fail))
    lines.append("")
    lines.append("PASS" if not fails else "FAIL: " + " | ".join(fails))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def run(args) -> int:
    out = os.path.abspath(args.out or tempfile.mkdtemp(prefix="anyuse-empty-bench-"))
    os.makedirs(out, exist_ok=True)
    trees = {"OLD": os.path.abspath(args.old), "NEW": os.path.abspath(args.new)}
    for t in trees.values():
        if out.startswith(t + os.sep):
            raise SystemExit("--out must be outside both trees")
    me = os.path.join(out, "empty_page_bench.py")
    shutil.copyfile(os.path.abspath(__file__), me)
    home = os.path.join(out, "home")
    os.makedirs(os.path.join(home, ".anyuse"), exist_ok=True)
    results = []
    for arm in ARMS:
        tree = trees["OLD" if arm == "OLD" else "NEW"]
        label = "OLD" if arm == "OLD" else "NEW"
        pkgroot = os.path.join(out, f"pkgroot_{label}")
        os.makedirs(pkgroot, exist_ok=True)
        link = os.path.join(pkgroot, "anyuse")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(tree, link)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANYUSE_")}
        env.update({
            "PYTHONPATH": os.pathsep.join([pkgroot, os.path.join(tree, "tools", "guarded_tests")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(home, ".cache"),
            "ANYUSE_DATA_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_SECRETS_DIR": os.path.join(home, ".anyuse"),
            "ANYUSE_API_HOSTS_FILE": os.path.join(home, ".anyuse", f"api_hosts_{arm}.json"),
            "ANYUSE_BLOCKED_HOSTS_FILE": os.path.join(home, ".anyuse", f"blocked_{arm}.json"),
            "ANYUSE_ROBOTS_CACHE_FILE": os.path.join(home, ".anyuse", f"robots_{arm}.json"),
            "ANYUSE_MODEL_CHECKS": "none",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
        path = os.path.join(out, f"{arm}.json")
        proc = subprocess.run([args.python or sys.executable, me, "arm", "--arm", arm,
                               "--tree", tree, "--out-file", path], cwd=out, env=env)
        if proc.returncode:
            return proc.returncode
        results.append(path)
    text = compare(results)
    with open(os.path.join(out, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text)
    print(f"\nper-case rows: {', '.join(results)}")
    return 0 if text.rstrip().endswith("PASS") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--python", default="")
    r.add_argument("--out", default="")
    a = sub.add_parser("arm")
    a.add_argument("--arm", required=True, choices=list(ARMS))
    a.add_argument("--tree", required=True)
    a.add_argument("--out-file", required=True)
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return run(args)
    if args.cmd == "arm":
        run_arm(args.arm, os.path.abspath(args.tree), os.path.abspath(args.out_file))
        return 0
    print(compare(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
