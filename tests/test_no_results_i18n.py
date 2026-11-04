"""An empty result page in Japanese, Chinese, Korean, Russian, Greek, Arabic or Hebrew.

The "no results" statements once covered Latin-script languages only, so a correct
search that found nothing on such a site failed as if its markup had changed. The
statements of these languages now count, under the rules the Latin ones follow
(execution/result_contract.py):

* the statement counts when its own line names the query, when it stands right under
  a heading that names it, or a few lines below a search summary that names it with
  no result row between;
* a statement about another term is refused, also below a summary naming the query
  (these languages put the subject before the statement as often as after it);
* a sentence in a footer or a help box on a page whose selector broke is refused,
  even when the rows it no longer matches name the query;
* an error page in these languages is never an empty result;
* a bare "not found" verb (a missing page's message, a help link) is not a statement,
  and a count of 0 is not read inside 10;
* "no results" in a condition, a help instruction or a question ("If no results for x,
  contact us", "Если по запросу «x» ничего не найдено, напишите нам") is not a statement,
  in any language; a statement followed by help still is;
* under a heading naming the query, as below a summary, a statement naming another term
  is about that term.

Offline: inline pages and a fake driver. No browser, no network, no model.
"""
import sys

import pytest

from anyuse.execution import result_contract as RC
from anyuse.execution.cdp_replay import PageResult, ReplayExecutor, ReplayTemplate
from anyuse.execution.html_extract import extract, parse

SELECTOR = "ul.hits li.hit"
NAV = "https://h.test/s?q={q}"

#: per language: the query, another term, three result labels naming the query, a statement naming the query
#: ("{q}"), a bare statement, a heading and a search summary naming it, and an error page's sentence
LANGS = {
    "ja": dict(q="東京", other="大阪", rows=["東京タワー", "東京駅", "東京大学"],
               line=["「{q}」に一致する商品は見つかりませんでした。", "「{q}」の検索結果はありません。",
                     "「{q}」に該当する商品はありません", "「{q}」の検索結果 0件"],
               bare=["検索結果はありません。", "該当する商品はありません。",
                     "申し訳ございませんが、一致する結果は見つかりませんでした。"],
               heading="「{q}」の検索結果", summary="「{q}」の検索結果",
               error="エラーが発生しました。「{q}」の検索結果はありません。"),
    "zh": dict(q="东京", other="大阪", rows=["东京塔", "东京站", "东京大学"],
               line=["没有找到与“{q}”相关的结果", "找不到“{q}”的相关结果", "“{q}”无结果"],
               bare=["没有找到相关结果。", "无结果", "抱歉，没有找到相关的商品。"],
               heading="“{q}”的搜索结果", summary="搜索结果：{q}",
               error="服务器错误，没有找到与“{q}”相关的结果"),
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


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))


class Driver:
    """Serves one DOM; its text is the DOM's visible text on one line, as in test_replay_result_contract."""

    def __init__(self, dom):
        self.dom = dom
        self.text = parse(dom, visible_only=True).text()

    def open(self, url, settle_s, timeout_s):
        return PageResult(url=url, dom=self.dom, text=self.text, status=200, elapsed_ms=10.0)


def tmpl(contract=True):
    expect = {"selector": SELECTOR, "min_items": 3, "identity": {"final_url": "rendered_url"}}
    if contract:
        expect[RC.CONTRACT_KEY] = RC.replay_contract("q")
    return ReplayTemplate(url=NAV, params=["q"], extract={"items": SELECTOR}, expect=expect)


def run(dom, value, contract=True):
    return ReplayExecutor(driver=Driver(dom)).execute(tmpl(contract), {"q": value})


def page(body, q):
    return (f"<html><body><header><a href='/'>Home</a><form><input value='{q}'>"
            f"</form></header><main>{body}</main><footer><p>© h.test</p></footer></body></html>")


def hits(rows, cls="hit"):
    return "<ul class='hits'>" + "".join(f"<li class='{cls}'><a href='/i/{i}'>{r}</a></li>"
                                         for i, r in enumerate(rows)) + "</ul>"


def served_empty(out):
    return out["success"] and out["extracted"]["items"] == [] and out["result_contract"]["kind"] == "empty"


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_a_statement_that_names_the_query_is_an_empty_result(lang):
    L = LANGS[lang]
    for line in L["line"]:
        dom = page(f"<ul class='hits'></ul><p class='empty'>{line.format(q=L['q'])}</p>", L["q"])
        for contract in (True, False):
            out = run(dom, L["q"], contract)
            assert served_empty(out), (lang, line, out.get("error"))
        # the compiled request's HTML rung reads the same statements
        v = RC.check_html(RC.replay_contract("q"), dom, SELECTOR, {"q": L["q"]}, [])
        assert v.ok and v.kind == "empty", (lang, line, v.reason)


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_a_bare_statement_under_a_heading_or_below_a_summary_is_bound(lang):
    L = LANGS[lang]
    for bare in L["bare"]:
        for body in (f"<h2>{L['heading'].format(q=L['q'])}</h2><p class='empty'>{bare}</p>",
                     f"<p class='summary'>{L['summary'].format(q=L['q'])}</p>"
                     f"<ul class='hits'></ul><p class='empty'>{bare}</p>"):
            out = run(page(body, L["q"]), L["q"])
            assert served_empty(out), (lang, body, out.get("error"))


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_a_statement_about_another_term_is_refused(lang):
    L = LANGS[lang]
    for line in L["line"]:
        other = line.format(q=L["other"])
        for body in (f"<p class='empty'>{other}</p>",
                     # a summary naming the query, then a statement naming its own subject
                     f"<p class='summary'>{L['summary'].format(q=L['q'])}</p><p class='empty'>{other}</p>"):
            out = run(page(body, L["q"]), L["q"])
            assert not out["success"], (lang, body)


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_an_unbound_sentence_on_a_drifted_page_is_refused(lang):
    L = LANGS[lang]
    for bare in L["bare"]:
        # the selector broke (a class renamed); the rows still name the query
        drifted = hits(L["rows"], cls="hit-v2")
        for body in (f"{drifted}<div class='help'><p>{bare}</p></div>",
                     f"<p class='summary'>{L['summary'].format(q=L['q'])}</p>{drifted}"
                     f"<div class='help'><p>{bare}</p></div>"):
            dom = page(body, L["q"])
            assert extract(dom, SELECTOR) == []
            out = run(dom, L["q"])
            assert not out["success"] and "not as an empty result" in out["error"], (lang, body)
        dom = page(drifted, L["q"]).replace("<p>© h.test</p>", f"<p>{bare}</p>")
        assert not run(dom, L["q"])["success"], (lang, bare)


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_an_error_page_is_not_an_empty_result(lang):
    L = LANGS[lang]
    dom = page(f"<ul class='hits'></ul><p class='alert'>{L['error'].format(q=L['q'])}</p>", L["q"])
    out = run(dom, L["q"])
    assert not out["success"] and "reports an error" in out["error"], (lang, out.get("error"))


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_the_result_list_still_serves(lang):
    L = LANGS[lang]
    out = run(page(hits(L["rows"]), L["q"]), L["q"])
    assert out["success"] and out["extracted"]["items"] == L["rows"], out


def test_a_bare_verb_or_a_count_inside_another_number_is_not_a_statement():
    for text, q in (("「東京」 お探しのページは見つかりませんでした。", "東京"),
                    ("找不到您要的东京商品？联系我们", "东京"),
                    ("「東京」の検索結果 10件", "東京"),
                    ("“东京”共10条结果", "东京"),
                    ("«Москва» 10 результатов", "Москва"),
                    # "in time": no delivery fits, not a search that found nothing
                    ("「東京」行きは明日に間に合う便がありません", "東京")):
        phrase, why = RC.empty_evidence_text(text, q)
        assert not phrase, (text, phrase)
        assert "no 'no results' statement" in why, (text, why)


def test_a_help_question_is_not_a_statement():
    # "Can't find it?" asks the reader; it does not say the search found nothing. On a page whose
    # selector broke, such a line under a heading or a summary naming the query, or naming it itself,
    # is not an empty result.
    ja, zh = hits(LANGS["ja"]["rows"], cls="hit-v2"), hits(LANGS["zh"]["rows"], cls="hit-v2")
    for body, q in ((f"<h2>「東京」の検索結果</h2><p>該当する商品が見つかりませんか？</p>{ja}", "東京"),
                    (f"<p>「東京」の検索結果</p><p>一致する結果は見つかりませんでしたか？</p>{ja}", "東京"),
                    (f"{ja}<p>「東京」の検索結果はありませんか？</p>", "東京"),
                    (f"<p>搜索结果：东京</p><p>找不到商品？</p>{zh}", "东京"),
                    (f"{zh}<p>找不到“东京”？联系客服</p>", "东京"),
                    (f"{zh}<p>没有找到“东京”的相关结果吗？</p>", "东京")):
        assert not run(page(body, q), q)["success"], body
    # a statement followed by a suggestion in another clause still states it
    out = run(page("<ul class='hits'></ul><p>没有找到与“东京”相关的结果，换个关键词试试？</p>", "东京"), "东京")
    assert served_empty(out), out.get("error")


def test_a_statement_about_the_search_conditions_is_bound_by_the_summary():
    # "no results matching your search conditions" names the search, not a subject of its own
    body = "<p>搜索结果：东京</p><ul class='hits'></ul><p>{}</p>"
    out = run(page(body.format("没有找到与您的搜索条件匹配的结果。"), "东京"), "东京")
    assert served_empty(out), out.get("error")
    # the same statement about another term is still about that term
    assert not run(page(body.format("没有找到与“大阪”匹配的结果。"), "东京"), "东京")["success"]


#: help lines naming the query ("{q}") in which "no results" is a condition, a help instruction or a
#: question, per language: the query, then the lines
CONDITIONS = {
    "en": ("kafka", ["If no results for {q}, contact us", "When no results for {q} show up, try a shorter word",
                     "No results for {q}? Check the spelling", "Contact us about no results for {q}"]),
    "de": ("kafka", ["Wenn Sie zu {q} keine Ergebnisse finden, kontaktieren Sie uns",
                     "Falls keine Treffer für {q} erscheinen, prüfen Sie die Schreibweise"]),
    "fr": ("kafka", ["Si aucun résultat pour {q}, contactez-nous"]),
    "es": ("kafka", ["Si no se han encontrado resultados para {q}, escríbanos",
                     "¿Sin resultados para {q}? Escríbanos"]),
    "it": ("kafka", ["Se nessun risultato per {q}, contattaci"]),
    "ru": ("Москва", ["Если по запросу «{q}» ничего не найдено, напишите нам",
                      "Ничего не найдено по запросу «{q}»? Напишите нам"]),
    "el": ("Αθήνα", ["Εάν δεν βρέθηκαν αποτελέσματα για «{q}», επικοινωνήστε μαζί μας",
                     "Δεν βρέθηκαν αποτελέσματα για «{q}»; Επικοινωνήστε μαζί μας"]),
    "ar": ("القاهرة", ["إذا لم يتم العثور على نتائج لـ «{q}»، اتصل بنا"]),
    "he": ("חיפה", ["אם לא נמצאו תוצאות עבור {q}, צרו קשר"]),
    "ja": ("東京", ["もし「{q}」に該当する商品はありませんでしたら、お問い合わせください"]),
    "zh": ("东京", ["如果没有找到与“{q}”相关的结果，请联系我们"]),
    "ko": ("서울", ["'{q}' 검색 결과가 없어요? 고객센터에 문의하세요"]),
}
#: statements naming the query that a help sentence or clause follows: still statements
STATED = [("kafka", "No results for {q}"), ("kafka", "No results found for {q}. Contact us if you need help."),
          ("kafka", "Sorry, no results for {q}, try another term."),
          ("kafka", "Keine Ergebnisse für {q}. Wenn Sie Hilfe brauchen, schreiben Sie uns."),
          ("Москва", "Ничего не найдено по запросу «{q}»"),
          ("Москва", "По запросу «{q}» ничего не найдено. Если нужна помощь, напишите нам."),
          ("Αθήνα", "Δεν βρέθηκαν αποτελέσματα για «{q}». Επικοινωνήστε μαζί μας."),
          ("서울", "'{q}'에 대한 검색 결과가 없습니다. 고객센터에 문의하세요.")]


def test_a_menu_before_the_statement_is_not_a_help_instruction():
    # a site menu's bare "Contact" or "Try Pro" runs into the statement in the page's text
    menu = "<nav><a href='/'>Home</a> <a href='/c'>Contact</a> <a href='/p'>Try Pro</a></nav>"
    assert RC.empty_evidence_text("Home Contact Try Pro zzqx No results found for zzqx", "zzqx")[0]
    out = run(page(f"{menu}<ul class='hits'></ul><p class='empty'>No results found for zzqx</p>", "zzqx"), "zzqx")
    assert served_empty(out), out.get("error")
    # a line above the statement is not in its sentence
    dom = page("<p>If you need help, write to us</p><ul class='hits'></ul>"
               "<p class='empty'>No results found for zzqx</p>", "zzqx")
    assert served_empty(run(dom, "zzqx"))
    assert RC.empty_evidence_text("If you need help, write to us\nNo results found for zzqx", "zzqx")[0]
    v = RC.check_html(RC.replay_contract("q"), dom, SELECTOR, {"q": "zzqx"}, [])
    assert v.ok and v.kind == "empty", v.reason


def test_the_querys_own_punctuation_neither_ends_the_sentence_nor_asks():
    assert not RC.empty_evidence_text("Wenn Sie zu node.js keine Ergebnisse finden, schreiben Sie uns",
                                      "node.js")[0]
    assert RC.empty_evidence_text('No results found for "what is kafka?"', "what is kafka?")[0]


@pytest.mark.parametrize("lang", sorted(CONDITIONS))
def test_a_condition_a_help_instruction_or_a_question_is_not_a_statement(lang):
    q, lines = CONDITIONS[lang]
    rows = LANGS.get(lang, {}).get("rows") or [f"{q} client", f"{q} server", f"{q} tools"]
    drifted = hits(rows, cls="hit-v2")
    for line in lines:
        text = line.format(q=q)
        # the compiled request's HTML rung reads the same statements
        assert not RC.empty_evidence_text(text, q)[0], (lang, text)
        # a help box on a page whose selector broke, its own line, or under a heading or a summary
        # naming the query on an empty page
        for body in (f"{drifted}<div class='help'><p>{text}</p></div>",
                     f"<div class='help'><p>{text}</p></div>{drifted}",
                     f"<ul class='hits'></ul><p class='help'>{text}</p>",
                     f"<h2>Results for {q}</h2><ul class='hits'></ul><p class='help'>{text}</p>",
                     f"<p>Search results for {q}</p><ul class='hits'></ul><p class='help'>{text}</p>"):
            out = run(page(body, q), q)
            assert not out["success"], (lang, body, out.get("empty_state"))


def test_a_statement_followed_by_help_still_states_it():
    for q, line in STATED:
        out = run(page(f"<ul class='hits'></ul><p class='empty'>{line.format(q=q)}</p>", q), q)
        assert served_empty(out), (line, out.get("error"))


def test_a_statement_about_another_term_under_a_heading_naming_the_query_is_refused():
    latin = [("Results for kafka", "No results found for zookeeper."),
             ("Results for kafka", "Your search for zookeeper did not match any documents."),
             ("Suchergebnisse für kafka", "Keine Ergebnisse für zookeeper."),
             ("Suchergebnisse für kafka", "Ihre Suche nach zookeeper ergab keine Treffer."),
             ("Résultats pour kafka", "Aucun résultat pour zookeeper."),
             ("Resultados para kafka", "Sin resultados para zookeeper."),
             ("Risultati per kafka", "Nessun risultato per zookeeper.")]
    for heading, statement in latin:
        out = run(page(f"<h2>{heading}</h2><ul class='hits'></ul><p class='empty'>{statement}</p>", "kafka"),
                  "kafka")
        assert not out["success"], (heading, statement, out.get("empty_state"))
    for lang, L in sorted(LANGS.items()):
        for line in L["line"]:
            body = (f"<h2>{L['heading'].format(q=L['q'])}</h2><ul class='hits'></ul>"
                    f"<p class='empty'>{line.format(q=L['other'])}</p>")
            out = run(page(body, L["q"]), L["q"])
            assert not out["success"], (lang, line, out.get("empty_state"))
    # a statement about the search itself stays bound by the heading
    for statement in ("Your search did not match any documents.", "No results found for your search.",
                      "Keine Treffer zu Ihrer Suche."):
        out = run(page(f"<h2>Results for kafka</h2><p class='empty'>{statement}</p>", "kafka"), "kafka")
        assert served_empty(out), (statement, out.get("error"))


#: more statement forms, per language: the query, another term, the statements, and the same naming
#: the other term ("{o}")
MORE = {
    "zh": ("东京", "大阪", ["暂无相关结果", "没有搜索结果"], ["“{o}”暂无相关结果", "“{o}”没有搜索结果"]),
    "ru": ("Москва", "Казань", ["Товары не найдены", "Товаров не найдено."],
           ["По запросу «{o}» товары не найдены", "По запросу «{o}» товаров не найдено"]),
    "he": ("חיפה", "אילת", ["נמצאו 0 תוצאות"], ["נמצאו 0 תוצאות עבור {o}"]),
    "ar": ("القاهرة", "دبي", ["عذراً، ولا توجد نتائج.", "ولا توجد نتائج"], ["ولا توجد نتائج لـ «{o}»"]),
    "ko": ("서울", "부산", ["검색 결과가 없어요"], ["'{o}' 검색 결과가 없어요"]),
    "ja": ("東京", "大阪", ["条件に合う商品はありません"], ["「{o}」の条件に合う商品はありません"]),
}


@pytest.mark.parametrize("lang", sorted(MORE))
def test_more_statement_forms_are_read_with_the_same_rules(lang):
    q, other, statements, others = MORE[lang]
    L = LANGS[lang]
    for st in statements:
        for body in (f"<h2>{L['heading'].format(q=q)}</h2><ul class='hits'></ul><p class='empty'>{st}</p>",
                     f"<p class='summary'>{L['summary'].format(q=q)}</p><ul class='hits'></ul>"
                     f"<p class='empty'>{st}</p>"):
            out = run(page(body, q), q)
            assert served_empty(out), (lang, body, out.get("error"))
        drifted = hits(L["rows"], cls="hit-v2")
        for body in (f"{drifted}<div class='help'><p>{st}</p></div>",
                     f"<p class='summary'>{L['summary'].format(q=q)}</p>{drifted}<div class='help'><p>{st}</p></div>",
                     # a question is not a statement
                     f"<h2>{L['heading'].format(q=q)}</h2><p class='help'>{st.rstrip('.')}？</p>{drifted}",
                     f"<h2>{L['heading'].format(q=q)}</h2><p class='help'>{st.rstrip('.')}?</p>{drifted}"):
            assert not run(page(body, q), q)["success"], (lang, body)
    for st in others:
        for body in (f"<ul class='hits'></ul><p class='empty'>{st.format(o=other)}</p>",
                     f"<h2>{L['heading'].format(q=q)}</h2><ul class='hits'></ul>"
                     f"<p class='empty'>{st.format(o=other)}</p>",
                     f"<p class='summary'>{L['summary'].format(q=q)}</p><ul class='hits'></ul>"
                     f"<p class='empty'>{st.format(o=other)}</p>"):
            assert not run(page(body, q), q)["success"], (lang, body)
        # naming the query itself, it states the empty result on its own line
        out = run(page(f"<ul class='hits'></ul><p class='empty'>{st.format(o=q)}</p>", q), q)
        assert served_empty(out), (lang, st, out.get("error"))
    # a count of 0 is not read inside another number
    assert not RC.empty_evidence_text("נמצאו 10 תוצאות עבור חיפה", "חיפה")[0]


#: help lines naming the query ("{q}") whose condition is elided or written against its word ("S'il",
#: "وإذا", "ואם"), closes after the phrase ("没有结果时", "...的话"), or takes another form
FURTHER_CONDITIONS = [
    ("kafka", "S'il n'y a aucun résultat pour {q}, contactez-nous"),
    ("kafka", "S’il n’y a aucun résultat pour {q}, contactez-nous"),
    ("kafka", "Should there be no results for {q}, contact us"),
    ("kafka", "In the case of no results for {q}, write to us"),
    ("kafka", "Sollten keine Treffer für {q} erscheinen, schreiben Sie uns"),
    ("kafka", "Nel caso non ci sia nessun risultato per {q}, scrivici"),
    ("kafka", "Qualora non ci sia nessun risultato per {q}, scrivici"),
    ("القاهرة", "وإذا لم يتم العثور على نتائج لـ «{q}»، اتصل بنا"),
    ("القاهرة", "فإذا لم يتم العثور على نتائج لـ «{q}»، اتصل بنا"),
    ("חיפה", "ואם לא נמצאו תוצאות עבור {q}, צרו קשר"),
    ("东京", "搜索“{q}”没有结果时，请联系我们"),
    ("东京", "“{q}”暂无相关结果的话，请联系我们"),
    ("东京", "若搜索“{q}”没有结果，请联系我们"),
]
#: statements naming the query ("{q}") with a word of the condition list that is no condition: a
#: reflexive, "or", a suggestion and its question
NOT_CONDITIONS = [
    ("kafka", "Se encontraron 0 resultados para {q}"),
    ("kafka", "Lo sentimos, se han encontrado 0 resultados para {q}."),
    ("kafka", "Non si è trovato nessun risultato per {q}"),
    ("東京", "「{q}」もしくは「大阪」に一致する商品はありません"),
    ("東京", "もしかして「東京都」　「{q}」に一致する商品はありません"),
    ("kafka", "No results for {q} – did you mean {q}s?"),
    ("kafka", "Keine Treffer für {q} – meinten Sie {q}s?"),
]


def test_a_condition_against_its_word_or_after_the_phrase_is_not_a_statement():
    for q, line in FURTHER_CONDITIONS:
        text = line.format(q=q)
        assert not RC.empty_evidence_text(text, q)[0], text
        drifted = hits([f"{q} client", f"{q} server", f"{q} tools"], cls="hit-v2")
        for body in (f"{drifted}<div class='help'><p>{text}</p></div>",
                     f"<ul class='hits'></ul><p class='help'>{text}</p>",
                     f"<h2>Results for {q}</h2><ul class='hits'></ul><p class='help'>{text}</p>"):
            out = run(page(body, q), q)
            assert not out["success"], (body, out.get("empty_state"))


def test_a_reflexive_or_a_suggestion_is_not_a_condition():
    for q, line in NOT_CONDITIONS:
        text = line.format(q=q)
        assert RC.empty_evidence_text(text, q)[0], text
        out = run(page(f"<ul class='hits'></ul><p class='empty'>{text}</p>", q), q)
        assert served_empty(out), (text, out.get("error"))


def test_a_statement_about_your_search_term_stays_bound_by_the_heading_and_the_summary():
    above = ("<h2>Suchergebnisse für kafka</h2>", "<p class='summary'>Suchergebnisse für kafka</p>")
    for statement in ("Zu Ihrem Suchbegriff wurden keine Ergebnisse gefunden.",
                      "Für Ihren Suchbegriff wurden keine Treffer gefunden.",
                      "No results found for your keywords.", "Aucun résultat pour ce terme.",
                      "No se han encontrado resultados para este término."):
        for line in above:
            out = run(page(f"{line}<ul class='hits'></ul><p class='empty'>{statement}</p>", "kafka"), "kafka")
            assert served_empty(out), (line, statement, out.get("error"))
    # a term after a bare word for it, or quoted after "your search", is that term
    for statement in ("No results for keyword kafkaa.", "No results found for the term kafkaa.",
                      "No results found for your search “kafkaa”.",
                      "Keine Treffer für Ihren Suchbegriff „kafkaa“."):
        for line in above:
            out = run(page(f"{line}<ul class='hits'></ul><p class='empty'>{statement}</p>", "kafka"), "kafka")
            assert not out["success"], (line, statement, out.get("empty_state"))


def test_latin_statements_are_read_as_before():
    # a statement about "your search" stays bound by the summary above it
    for summary, statement in (("Résultats pour zzqx", "Aucun résultat pour votre recherche."),
                               ("Results for zzqx", "No results found for your search.")):
        out = run(page(f"<p>{summary}</p><p class='empty'>{statement}</p>", "zzqx"), "zzqx")
        assert served_empty(out), (summary, out.get("error"))
    out = run(page("<p>Results for zzqx</p><p class='empty'>No results found for kafka.</p>", "zzqx"), "zzqx")
    assert not out["success"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
