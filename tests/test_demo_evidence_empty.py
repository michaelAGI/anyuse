"""Web tool version 15, offline (fake result pages; no browser, no network; the check model is faked): an empty
or error result page is read in the site's own evidence before the phrase lists.

What must hold:

1. the empty state: the site's learned empty page first (as before), then the configured check model about the
   lines that name the input - only inside an open model-check scope -, then the statement rule of every script
   the result contract reads, bound to the query, then the phrase list. A Japanese or Russian statement naming the
   query is an empty search for a version-15 tool and a failure for a version-14 one; a statement in a language
   no list holds (Vietnamese) is one only by the model, in a scope; outside a scope nothing is asked; a model that
   reads no line as saying so keeps the phrase list from deciding; the phrase list's own cases ("Keine Treffer",
   with or without the query) stay empty searches;
2. the error page: a phrase in a result's link text ("Page not found errors in WordPress") or in a line the site's
   empty page and result pages share (the site's chrome) is no error for a version-15 tool; a version-14 tool fails
   both pages, as before; a real error page fails both; the model in a scope can clear a line;
3. a demonstration's wait for its results stops on the statement rule of every script too.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import empty_page as EP  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from test_demo_compile import BASE, product_trace, read_of, spec_for  # noqa: E402


def tools():
    t15 = C.compile_trace(product_trace())["tool"]
    t14 = C.compile_trace(product_trace(), version=14)["tool"]
    return t15, t14


def empty_read(value, text):
    r = read_of(spec_for(f"{BASE}/search?q=Zqurvant", recommend=False))
    r["text"] = r["main_text"] = text
    return r


def test_a_statement_of_another_script_bound_to_the_query_is_an_empty_search_for_version_15():
    t15, t14 = tools()
    for value, text in (("東京タワー", "検索\n「東京タワー」に一致する商品は見つかりませんでした\nおすすめ"),
                        ("чайник", "Поиск\nПо запросу «чайник» ничего не найдено\nПопулярное")):
        r = empty_read(value, text)
        j15 = C.judge_result_page(t15, r, value)
        assert j15["status"] == "no_results", j15
        assert j15["empty_check"]["by"] == EP.BY_STATEMENT, j15
        j14 = C.judge_result_page(t14, r, value)
        assert j14["status"] == "failed" and "no 'no results' statement" in j14["why"], j14


def test_the_phrase_lists_own_cases_stay_empty_searches():
    t15, t14 = tools()
    for text in ("Keine Treffer für „Zqurvant Oxlembiq“", "Keine Treffer gefunden.\nBeliebt",
                 "Es konnte leider nichts Passendes gefunden werden."):
        for t in (t15, t14):
            j = C.judge_result_page(t, empty_read("Zqurvant Oxlembiq", text), "Zqurvant Oxlembiq")
            assert j["status"] == "no_results", (text, j)
    # a page stating a positive count is no empty search, whatever another line says
    j = C.judge_result_page(t15, empty_read("saddel", "No results for 'saddel'\nShowing results for saddle: 12 products"),
                            "saddel")
    assert j["status"] != "no_results", j


VI = "Tìm kiếm\nKhông tìm thấy sản phẩm nào cho “xyzzy”\nGợi ý cho bạn"


def test_a_statement_no_list_holds_is_an_empty_search_by_the_model_in_a_scope():
    t15, t14 = tools()
    r = empty_read("xyzzy", VI)
    asked = []

    def fake(instructions, state, obs):
        line = state.rsplit(">> ", 1)[-1]
        asked.append(line)
        return 0.95 if "Không tìm thấy" in line else 0.05
    # outside a scope: nothing is asked, the phrase list finds nothing
    with MC.using(fake):
        j = C.judge_result_page(t15, r, "xyzzy")
    assert j["status"] == "failed" and not asked, (j, asked)
    with MC.scope("product"), MC.using(fake):
        j = C.judge_result_page(t15, r, "xyzzy")
        j14 = C.judge_result_page(t14, r, "xyzzy")
    assert j["status"] == "no_results" and "Không tìm thấy" in j["empty_statement"], j
    assert j["empty_check"]["by"] == EP.BY_MODEL and asked, j
    assert j14["status"] == "failed", j14                       # a version-14 tool asks nothing


def test_a_model_that_reads_no_line_as_empty_keeps_the_phrase_list_from_deciding():
    t15, t14 = tools()
    r = empty_read("Zqurvant Oxlembiq", "Keine Treffer für „Zqurvant Oxlembiq“")
    with MC.scope("product"), MC.using(lambda i, s, o: 0.02):
        j = C.judge_result_page(t15, r, "Zqurvant Oxlembiq")
        j14 = C.judge_result_page(t14, r, "Zqurvant Oxlembiq")
    assert j["status"] == "failed" and j["empty_check"].get("refused"), j
    assert j14["status"] == "no_results", j14


def result_read(first_line):
    r = read_of(spec_for(f"{BASE}/search?q=Borealis+Toaster+2+Slice"))
    r["text"] = first_line + "\n" + r["text"]
    return r


def test_a_title_holding_an_error_phrase_is_no_error_page_for_version_15():
    t15, t14 = tools()
    r = result_read("Page not found errors in WordPress")
    r["elements"].append({"i": 99, "tag": "a", "text": "Page not found errors in WordPress",
                          "href": BASE + "/blog/page-not-found", "area": "main"})
    j15 = C.judge_result_page(t15, r, "Borealis Toaster 2 Slice")
    assert j15["status"] == "pick", j15
    assert j15["error_check"]["in_a_link"], j15
    j14 = C.judge_result_page(t14, r, "Borealis Toaster 2 Slice")
    assert j14["status"] == "failed" and "reports an error" in j14["why"], j14


def test_a_link_holding_an_error_phrase_excuses_no_other_line():
    # a real error page whose own link ("Report a page not found") holds the phrase: its heading is still the error
    t15, _t14 = tools()
    r = empty_read("Acme", "Page not found\nNo results found.\nReport a page not found")
    r["elements"].append({"i": 98, "tag": "a", "text": "Report a page not found", "href": BASE + "/kontakt",
                          "area": "main"})
    j = C.judge_result_page(t15, r, "Acme Kettle")
    assert j["status"] == "failed" and "reports an error" in j["why"], j
    assert j["error_check"]["in_a_link"] == ["Report a page not found"], j


def test_the_sites_chrome_holding_an_error_phrase_is_no_error_page_for_version_15():
    t15, t14 = tools()
    chrome = "Seite nicht gefunden? Hilfe und Kontakt"
    results = [EP.Page("text", "Borealis Toaster", text=f"{chrome}\nBorealis Toaster 2 Slice\n29,00 €"),
               EP.Page("text", "Acme Kettle", text=f"{chrome}\nAcme Kettle 2000 Steel\n49,99 €")]
    probes = [EP.Page("text", v, text=f"{chrome}\nKeine Treffer für „{v}“") for v in ("zorquavex", "limbraktu")]
    sig = EP.learn("text", results, probes)
    assert sig and EP._h(EP.mask(chrome, "x")) in sig["shared"], sig
    r = result_read(chrome)
    j15 = C.judge_result_page(dict(t15, empty_page=sig), r, "Borealis Toaster 2 Slice")
    assert j15["status"] == "pick" and j15["error_check"]["site_chrome"], j15
    j14 = C.judge_result_page(dict(t14, empty_page=sig), r, "Borealis Toaster 2 Slice")
    assert j14["status"] == "failed", j14


def test_a_real_error_page_fails_and_the_model_in_a_scope_can_clear_a_line():
    t15, t14 = tools()
    r = empty_read("Acme", "Something went wrong. Please try again later.")
    for t in (t15, t14):
        assert C.judge_result_page(t, r, "Acme Kettle")["status"] == "failed"
    j = C.judge_result_page(t15, r, "Acme Kettle")
    assert "reports an error" in j["why"] and j["error_check"]["by"] == "page_statement", j
    r = result_read("Access denied to the members area - log in for prices")
    with MC.scope("product"), MC.using(lambda i, s, o: 0.03):
        j = C.judge_result_page(t15, r, "Borealis Toaster 2 Slice")
    assert j["status"] == "pick" and j["error_check"]["by"] == "model_check", j
    with MC.scope("product"), MC.using(lambda i, s, o: 0.97):
        j = C.judge_result_page(t15, r, "Borealis Toaster 2 Slice")
    assert j["status"] == "failed", j


def test_a_demonstrations_wait_stops_on_a_statement_of_another_script():
    r = {"text": "検索\n「東京タワー」に一致する商品は見つかりませんでした", "main_text": ""}
    assert C.stated_empty15(r, "東京タワー")
    assert not C.no_results_statement(r["text"])
    assert C.stated_empty15({"text": "Keine Treffer"}, "x") == "keine treffer"
    assert not C.stated_empty15({"text": "12 results for x"}, "x")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
