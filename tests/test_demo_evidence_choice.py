"""Web tool version 15, offline (fake result pages; no browser, no network; the check model is faked): the result
a product lookup opens is chosen by the differential between the site's own results before the word lists.

What must hold:

1. a result naming the input together with another product is a substitute or an accessory when the list itself
   shows the word that relates them twice with different products ("Etui do iPhone 15", "Szkło hartowane do iPhone
   15"): a version-15 tool picks the phone, a version-14 tool the case; the marker list's own case ("Hülle für") is
   decided as before; a list without such a word keeps its tie;
2. the check model, in an open scope only, picks the product among results that tie;
3. the input's word that fills a place the site's titles vary (a Turkish colour no list holds: "Siyah" against
   "Bej") is a mark the chosen result must carry: the sibling of another value is never answered (a version-14 tool
   answers it); a listed carrier is chosen; the colour list's own case is decided as before;
4. the product page's form state (the selected option) carries such a mark.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from test_demo_compile import BASE, NAV, product_trace, read_of  # noqa: E402


def tools():
    return C.compile_trace(product_trace())["tool"], C.compile_trace(product_trace(), version=14)["tool"]


def listing(value, titles):
    nav = [(t, BASE + h, "nav", "") for t, h in NAV]
    links = [(t, f"{BASE}/p/item-{n}", "main", t) for n, t in enumerate(titles)]
    spec = {"url": f"{BASE}/search?q={value}", "title": f"Search: {value}", "h1": [f"Search: {value}"],
            "links": nav + links, "text": f"{len(titles)} results for \"{value}\"\n" + "\n".join(titles), "jsonld": []}
    return read_of(spec)


def judge(t, value, titles):
    return C.judge_result_page(t, listing(value, titles), value)


def test_a_relation_word_the_list_repeats_marks_an_accessory():
    t15, t14 = tools()
    titles = ["Etui do iPhone 15", "Szkło hartowane do iPhone 15", "Apple iPhone 15 128 GB"]
    j15, j14 = judge(t15, "iPhone 15", titles), judge(t14, "iPhone 15", titles)
    assert j15["status"] == "pick" and j15["pick"]["title"] == "Apple iPhone 15 128 GB", j15
    assert j14["status"] == "pick" and j14["pick"]["title"] == "Etui do iPhone 15", j14
    # the same after the input (a language that puts the relation word after it)
    titles = ["iPhone 15 için kılıf", "iPhone 15 için ekran koruyucu cam", "Apple iPhone 15 128 GB"]
    assert judge(t15, "iPhone 15", titles)["pick"]["title"] == "Apple iPhone 15 128 GB"
    # the marker list's own case
    titles = ["Hülle für iPhone 15", "Apple iPhone 15 128 GB"]
    for t in (t15, t14):
        assert judge(t, "iPhone 15", titles)["pick"]["title"] == "Apple iPhone 15 128 GB"
    # no repeated relation word: the tie stays a tie
    titles = ["Apple iPhone 15 Pro", "Apple iPhone 15 Plus"]
    for t in (t15, t14):
        j = judge(t, "iPhone 15", titles)
        assert j["status"] == "incomplete" and len(j["ambiguous"]) == 2, j
    assert C.relation_marked15(["Etui do iPhone 15", "Szkło do iPhone 15", "Apple iPhone 15"], "iPhone 15") == {0, 1}


def test_a_word_the_product_and_another_result_both_put_next_to_the_input_marks_nothing():
    # the products repeat a word after the input ("Smartphone"), and the accessory puts another word there: the
    # list shows one structure for both, so the tie stays a tie (never the accessory)
    t15, t14 = tools()
    for value, titles in (("Galaxy S24", ["Galaxy S24 Smartphone 128GB", "Galaxy S24 Smartphone 256GB Onyx",
                                          "Galaxy S24 Hülle Silikon"]),
                          ("Lego 42115", ["Lego 42115 Technic Lamborghini Sián", "Lego 42115 Technic Bausatz",
                                          "Lego 42115 Ersatzteil Rad"])):
        assert C.relation_marked15(titles, value) == set(), titles
        for t in (t15, t14):
            j = judge(t, value, titles)
            assert j["status"] == "incomplete" and j.get("ambiguous"), (titles, j)


def test_the_check_model_in_a_scope_picks_the_product_among_tied_results():
    t15, _t14 = tools()
    titles = ["Apple iPhone 15 Pro", "Apple iPhone 15 Plus"]
    asked = []

    def fake(instructions, state, obs):
        line = state.rsplit(">> ", 1)[-1]
        asked.append(line)
        return 0.9 if line.endswith("Plus") else 0.1
    with MC.using(fake):
        j = judge(t15, "iPhone 15", titles)
    assert j["status"] == "incomplete" and not asked, j
    with MC.scope("product"), MC.using(fake):
        j = judge(t15, "iPhone 15", titles)
    assert j["status"] == "pick" and j["pick"]["title"] == "Apple iPhone 15 Plus" and asked, j


def test_a_word_in_a_place_the_titles_vary_is_a_mark_the_result_must_carry():
    t15, t14 = tools()
    value = "Masa Örtüsü Siyah 38 cm"
    titles = ["Masa Örtüsü Bej 38 cm", "Peçete Seti 6 Adet"]
    j15, j14 = judge(t15, value, titles), judge(t14, value, titles)
    assert j15["status"] == "incomplete" and j15["not_listed"]["names_instead"] == [["bej"]], j15
    assert j14["status"] == "pick" and j14["pick"]["title"] == "Masa Örtüsü Bej 38 cm", j14     # the sibling
    titles = ["Masa Örtüsü Bej 38 cm", "Masa Örtüsü Siyah 38 cm", "Masa Örtüsü Gri 38 cm"]
    j15 = judge(t15, value, titles)
    assert j15["status"] == "pick" and j15["pick"]["title"] == "Masa Örtüsü Siyah 38 cm", j15
    # the colour list's own case
    value = "Podkładka czarna na stół 38 cm"
    titles = ["Podkładka beżowa na stół 38 cm", "Serwetki 6 sztuk"]
    for t in (t15, t14):
        j = judge(t, value, titles)
        assert j["status"] == "incomplete" and j["not_listed"], j
    assert C.variant_slots15(["Masa Örtüsü Bej 38 cm", "Masa Örtüsü Gri 38 cm"], "Masa Örtüsü Siyah 38 cm") == {
        "bej": {"gri", "siyah"}, "gri": {"bej", "siyah"}, "siyah": {"bej", "gri"}}


def test_the_product_pages_selected_option_carries_a_variant_mark():
    mark = {"kind": "variant", "token": "siyah", "alts": ["bej"]}
    page = {"h1": ["Masa Örtüsü 38 cm"], "title": "Masa Örtüsü", "facts": [], "text": "", "main_text": "",
            "elements": [{"i": 3, "tag": "select", "text": "Renk", "options": [
                {"value": "1", "text": "Bej", "selected": False}, {"value": "2", "text": "Siyah", "selected": True}]}]}
    fields = {"name": "Masa Örtüsü 38 cm"}
    ok15, _w, ev = C.check_pick9(page, fields, "Masa Örtüsü Siyah 38 cm", {"need": [mark], "evidence_rule": "15"})
    ok14, why14, _e = C.check_pick9(page, fields, "Masa Örtüsü Siyah 38 cm", {"need": [mark]})
    assert ok15 and ev["marks"][0]["carried_by"] == "selected option", ev
    assert not ok14 and "distinguishing" in why14, why14


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
