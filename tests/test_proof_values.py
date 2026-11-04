"""Proof value selection: weak proof values and an unreadable card. Pinned, by STRUCTURE (where a word comes
from), never a word list:
* a title word the result page shows only in an attribute (an image's alt: www.holzprofi.com's "Picture of ...") is no
  proof value (``harness_compile.visible_words``);
* a word that occurs in the titles only inside a repeated trailing label (www.toolnut.com's ", Tool Only") is no proof
  value (``label_tokens``); a brand that leads many titles stays;
* a result CARD's item that the site's menu also links (heizsparer's topic page "Pufferspeicher") is no category: for
  card items the page's chrome links do not count toward FWF-9's repeats; an item read by the URL pattern keeps the
  full count (the menu's repeats tell categories);
* reading result links, a gated-action phrase counts only in a control-like text (www.heizsparer.de's result "Haus
  kaufen mit alter Heizung" read as a buy control): ``web_arch.forbidden_link``; link paths, queries and schemes always
  count; clicks keep ``forbidden_control``.
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_proof_values.py
"""
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

TOOLNUT = ["DeWalt DCD1007B 20V MAX XR Brushless Hammer Drill, Tool Only", "Festool 576869 CXS 12 Cordless Drill Kit",
           "Festool 576887 CXS 18 BASIC Cordless Drill, Tool Only", "Makita XFD12Z 18V LXT Brushless Drill, Tool Only",
           "DeWalt DCD800B 20V MAX XR Cordless Drill, Tool Only"]


def demo(titles, text, value="drill"):
    return {"workflow": "site_search", "value": value,
            "result": {"results": [{"title": t, "url": f"https://www.shop.test/p/{n}"} for n, t in enumerate(titles)]},
            "final_page": {"text": text, "main_text": text, "elements": []}}


def vocab(d):
    return [v["value"] for v in HC.site_vocabulary(d, d["value"])]


def test_a_repeated_trailing_label_is_no_proof_value():
    assert {"only", "tool"} <= HC.label_tokens(TOOLNUT)
    got = vocab(demo(TOOLNUT, "\n".join(TOOLNUT)))
    assert "Only" not in got and "Tool" not in got and got[:3] == ["Cordless", "DeWalt", "Brushless"]
    # a brand that LEADS many titles is no label
    canon = ["Canon EOS R5 Gehäuse", "Canon EOS R6 Mark II", "Canon RF 50mm F1.8 STM"]
    assert not HC.label_tokens(canon)


def test_a_word_the_page_shows_only_in_an_alt_is_no_proof_value():
    titles = ["Hobelmaschine Hans Schreiner ML393Q 310 mm", "Picture of Hobelmaschinen",
              "Picture of Hobelmaschine Test für Werkstatt-Profis"]
    shown = "Hobelmaschine Hans Schreiner ML393Q 310 mm\nHobelmaschinen\nHobelmaschine Test für Werkstatt-Profis"
    got = vocab(demo(titles, shown, value="Hobelmaschine"))
    assert "Picture" not in got and got[0] in ("Hans", "Schreiner")
    # no visible text kept: no rule (as before)
    assert "Picture" in vocab(demo(titles, "", value="Hobelmaschine"))


@pytest.mark.parametrize("text,href,refused", [
    ("Haus kaufen mit alter Heizung", "https://www.heizsparer.de/heizung/wissen/hauskauf-mit-alter-heizung", False),
    ("Jetzt kaufen", "https://www.shop.test/p/1", True), ("In den Warenkorb", "https://www.shop.test/p/1", True),
    ("Kettle Classic Steel 1.7 l", "https://www.shop.test/cart/add?id=3", True),
    ("Kettle Classic Steel 1.7 l", "mailto:x@shop.test", True)])
def test_reading_a_result_title_that_mentions_a_gated_word(text, href, refused):
    assert bool(W.forbidden_link(text, href)) is refused
    assert W.forbidden_control("Haus kaufen mit alter Heizung", "")         # clicks stay conservative


def test_such_a_title_is_a_region_link():
    read = {"elements": [{"i": 0, "tag": "a", "href": "https://www.heizsparer.de/heizung/wissen/hauskauf", "area": "main",
                          "text": "Haus kaufen mit alter Heizung"},
                         {"i": 1, "tag": "a", "href": "https://www.heizsparer.de/heizung/wissen/kaufen", "area": "main",
                          "text": "Jetzt kaufen"}]}
    got = C.region_links(read, {"regex": r"^https://www\.heizsparer\.de/heizung/wissen/[^/]+$"})
    assert [e["i"] for e in got] == [0]


def test_a_result_card_the_menu_also_links_is_no_category():
    def el(i, href, text, area="main"):
        return {"i": i, "tag": "a", "href": href, "text": text, "area": area}
    u = "https://www.heizsparer.de/heizung/heiztechnik/pufferspeicher"
    read = {"elements": [el(1, u, "Pufferspeicher", "nav"), el(2, u, "Pufferspeicher", "nav"), el(3, u, "Pufferspeicher"),
                         el(4, u, "Pufferspeicher")] +
            [el(10 + k, f"https://www.heizsparer.de/heizung/heiztechnik/artikel-{k}", f"Artikel Nummer {k}") for k in range(3)]}
    card = [{"title": "Pufferspeicher", "url": u, "own_repeat": 2}] + \
        [{"title": f"Artikel Nummer {k}", "url": f"https://www.heizsparer.de/heizung/heiztechnik/artikel-{k}", "own_repeat": 1} for k in range(3)]
    assert [it["url"] for it in C.drop_categories(card, read)][0] == u
    # read by the URL pattern alone (no card): the menu's repeats still make it a category (FWF-9)
    plain = [{k: v for k, v in it.items() if k != "own_repeat"} for it in card]
    assert u not in [it["url"] for it in C.drop_categories(plain, read)]


def test_the_verifier_never_splits_a_word_at_a_soft_hyphen():
    from anyuse.execution import attribute_verify as AV
    assert AV.tokens("For\xadmat\xadkreis\xads\u00e4gen") == AV.tokens("Formatkreiss\u00e4gen") == ["formatkreissagen"]
    assert AV.tokens("Zero\u200bwidth") == ["zerowidth"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
