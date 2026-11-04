"""Compiling on unseen sites, offline. Pinned here:
* URL identity (execution/url_identity.py): tracking / per-session parameters are dropped from every identity
  key and from region forming (a parameter that varies per item while the path tells the targets apart is noise; one
  that tells two links of the same path apart is identity); a shop whose result links carry ?_pos=..&_sid=.. compiles
  and its replay still navigates the full URL;
* the results page lists the region that CHANGED (link containers absent before the search) first, even when a
  result's link was already on the start page in a teaser block;
* a target the page links three or more times with one text is a category, never a result; read-more
  labels in the covered languages are never titles;
* proof values from the site's own vocabulary, deterministic and with their source; a blind caller value is
  skipped; a value without results on the site is INCONCLUSIVE - neither a failure nor a pass - and the next value is
  tried. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_compile_identity.py
"""
import html
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import agent_harness as H  # noqa: E402
from anyuse.execution import answer_refs as AR  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import discover as DV  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_policies as P  # noqa: E402
from anyuse.execution import harness_tools as HT  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402
from anyuse.execution import url_identity as UI  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402

import test_harness_compile as THC  # noqa: E402
import test_repair_ladder as SHOP  # noqa: E402

BASE, SITE = SHOP.BASE, SHOP.SITE


# --------------------------------------------------------------------------------------------------- URL identity
def test_tracking_and_per_item_noise_never_identify_a_target():
    u = "https://s.test/products/fin-x?_pos=1&_sid=5e59&_ss=r&utm_source=nl&color=blue"
    assert UI.strip(u) == "https://s.test/products/fin-x?color=blue"
    assert W.norm_url(u) == "https://s.test/products/fin-x?color=blue"          # every identity key drops them
    shop = [f"https://s.test/products/{s}?_pos={i}&_sid=ab&rank={i}" for i, s in enumerate(["a", "b", "c"], 1)]
    assert UI.page_noise(shop) >= {"rank"}                                     # varies per item, path tells apart
    ids = ["https://s.test/item?id=3", "https://s.test/item?id=4"]
    assert UI.page_noise(ids) == set()                                         # the id IS the identity
    assert UI.identity(ids[0]) != UI.identity(ids[1])


class SessionShop(SHOP.Shop):
    """Result links carry Shopify-style per-item session parameters."""

    def results(self, q):
        dom = super().results(q)
        for i, (_n, _p, slug) in enumerate(SHOP.PRODUCTS, 1):
            dom = dom.replace(f'href="/p/{slug}"', f'href="/p/{slug}?_pos={i}&amp;_sid=5e59a2878&amp;_ss=r"')
        return dom


def test_a_session_parameter_shop_forms_its_region_compiles_and_navigates_the_full_url():
    site = SessionShop()
    page = site.read(*site.route(BASE + "/search?q=Akkuschrauber"))
    want = [e["href"] for e in page["elements"] if e.get("tag") == "a" and "/p/" in str(e.get("href"))][:3]
    assert want and all("_sid=" in w for w in want)
    assert C.region_from_dom(page["dom"], page["url"], want) is not None       # was None (www.mikesdivestore.com)
    tools = HT.BrowserTools(D.DomPage(site), BASE + "/", SITE)
    pol = P.ScriptedPolicy([T.Search(0, "Akkuschrauber"), lambda t, o, h: T.Done(
        {"results": [{"element": e.index} for e in o.elements if "/p/" in e.href][:3]})])
    tr = H.Harness(HT.Toolbox(browser=tools), pol, H.HarnessConfig(), run_id="sid").run("search for {q}",
                                                                                       {"q": "Akkuschrauber"})
    c = HC.compile_web(tr)
    assert c["ok"], c["why"]
    out = HC.replay_web(c["tool"], site, "Bohrhammer")
    assert out["status"] == "ok" and all("_sid=" in r["url"] for r in out["output"]["results"])


# --------------------------------------------------------------------------------------------- the changed region
class TeaserShop(SHOP.Shop):
    """The start page and the results page both carry a teaser block that links a product the results also list."""

    TEASER = '<div class="teaser-strip"><a href="/p/bosch-gsr-12v">Bosch GSR</a> <a href="/p/ryobi-r18">Ryobi</a></div>'

    def start(self):
        return super().start().replace("<main>", "<main>" + self.TEASER)

    def results(self, q):
        return super().results(q).replace("<main>", "<main>" + self.TEASER)


def test_the_changed_region_is_listed_first_even_when_a_result_was_already_teased():
    tools = HT.BrowserTools(D.DomPage(TeaserShop()), BASE + "/", SITE)
    o1 = tools.observe()
    tools.dispatch(T.Search(0, "Bosch"), o1)
    o2 = tools.observe()
    order = [(e.text, e.href) for e in o2.elements if e.kind != "search box"]
    first_teaser = next(i for i, (t, _h) in enumerate(order) if t in ("Bosch GSR", "Ryobi"))
    results = [i for i, (t, h) in enumerate(order) if "/p/" in h and t not in ("Bosch GSR", "Ryobi")]
    assert results and max(results) < first_teaser                              # every result before the teasers
    assert any(t == "Bosch GSR 12V Akkuschrauber" for t, _h in order[:len(results)])


def test_the_changed_regions_links_are_listed_even_past_the_observations_cap():
    tools = HT.BrowserTools(None, BASE + "/", SITE)
    tools._prev_sigs = {"nav.mega>ul>li"}
    els = [T.Element(index=0, kind="link", text="Kamin", href=BASE + "/kamin")]
    mapping = {0: {"src": "el", "i": 1, "text": "Kamin", "href": BASE + "/kamin", "tag": "a"}}
    rec = {"elements": [{"i": 1, "tag": "a", "text": "Kamin", "href": BASE + "/kamin", "area": "body"},
                        {"i": 900, "tag": "a", "text": "Aduro 14 Kaminofen", "href": BASE + "/kaminofen-aduro-14",
                         "area": "body"},
                        {"i": 901, "tag": "a", "text": "Weiterlesen", "href": BASE + "/x", "area": "body"}],
           "link_sigs": {BASE + "/kamin": ["body.a>nav.mega>ul>li"],
                         BASE + "/kaminofen-aduro-14": ["body.b>div.listing>div.row>div.col"],
                         BASE + "/x": ["body.b>div.listing>div.row>div.col"]}}
    got = tools._with_changed_region(els, mapping, rec)
    added = [e for e in got if e.index != 0]
    assert [e.text for e in added] == ["Aduro 14 Kaminofen"] and mapping[added[0].index]["i"] == 900


def test_the_tools_region_reading_leaves_categories_out():
    read = {"elements": [{"tag": "a", "href": "https://n.test/cat/pro", "text": "Professionisti"}] * 4 +
            [{"tag": "a", "href": f"https://n.test/news/{i}", "text": f"Story {i}"} for i in range(3)]}
    items = [{"url": "https://n.test/cat/pro", "title": "Professionisti"}] + \
        [{"url": f"https://n.test/news/{i}", "title": f"Story {i}"} for i in range(3)]
    assert [it["title"] for it in C.drop_categories(items, read)] == ["Story 0", "Story 1", "Story 2"]
    assert C.drop_categories(items[:2], read) == items[:2]                    # never below two results


# -------------------------------------------------------------------------------- categories and read-more labels
def test_a_category_repeated_in_every_card_is_never_a_result_and_labels_are_never_titles():
    cat = "https://n.test/category/pro"
    links = [AR.Link(1, "https://n.test/a", "Story A"), AR.Link(2, cat, "Professionisti"),
             AR.Link(3, "https://n.test/b", "Story B"), AR.Link(4, "https://n.test/c", "Story C")]
    occ = {AR._norm(cat): 4, AR._norm("https://n.test/a"): 1}
    got, rep = AR.resolve_answer("site_search", {"results": [{"element": 1}, {"element": 2}, {"element": 3},
                                                             {"element": 4}]}, links, occurrences=occ)
    assert [r["title"] for r in got["results"]] == ["Story A", "Story B", "Story C"] and rep["category"] == 1
    for t in ("Weiterlesen", "Read more »", "Lire la suite…", "Leggi tutto", "Leer más", "Lees meer", "Läs mer", "→ mehr"):
        assert AR.is_label(t), t
    assert not AR.is_label("Morelli wins the Giro") and not AR.is_label("Mehrwertsteuer")
    # a lexicon label on one target only is still never the title: the card's own title link names it
    links2 = [AR.Link(1, "https://n.test/x", "Read more"), AR.Link(2, "https://n.test/x", "The real headline")]
    got, _ = AR.resolve_answer("site_search", {"results": [{"element": 1}]}, links2)
    assert got["results"][0]["title"] == "The real headline"


# ---------------------------------------------------------------------------------------------------- proof values
def test_proof_values_come_from_the_sites_own_vocabulary_with_their_source():
    tr, site = THC.web_run()
    demo = HC.trace_to_demo(tr)
    v1 = HC.site_vocabulary(demo, "Bosch")
    assert v1 == HC.site_vocabulary(demo, "Bosch")                              # deterministic
    assert v1[0]["value"] == "Akkuschrauber" and v1[0]["source"] == "result_titles" and v1[0]["titles"] == 2
    assert all("bosch" not in v["value"].lower() for v in v1)
    # the caller's values are held-out inputs by construction: tried first, in order, with where they come from;
    # only the demonstrated value itself (and a repeat) is skipped
    tries, skipped = HC.proof_values(demo, ["Makita", "Bohrhammer", "Bosch", "makita"])
    assert tries[0] == {"value": "Makita", "source": "caller"}
    assert tries[1] == {"value": "Bohrhammer", "source": "caller, on the site's own page"}
    assert skipped == [{"value": "Bosch", "why": "the demonstrated value itself proves nothing"},
                       {"value": "makita", "why": "given twice"}]
    assert tries[2]["source"] == "result_titles"                            # then the site's own vocabulary


def test_a_saved_web_tool_is_ready_on_the_callers_fresh_values(monkeypatch):
    # the caller's values are the proof (no Cerebras key, any provider); one the site has no results for is
    # inconclusive and the next value is tried
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    tr, site = THC.web_run()
    rep = HC.compile_and_save_web(tr, THC.storage(), site, ["Zxqvbn Plorf", "Bohrhammer", "Akkuschrauber"])
    assert rep["status"] == "ready", rep["reasons"]
    sources = [c["source"] for c in rep["proof"]["cases"]]
    assert sources[:2] == ["caller", "caller, on the site's own page"], sources


def test_a_value_without_results_is_inconclusive_never_a_pass_and_never_a_failure():
    tr, site = THC.web_run()
    tool = HC.compile_web(tr)["tool"]
    p = HC.prove_web(tool, site, ["Zxqvbn Plorf", "Akkuschrauber", "Bohrhammer"])
    assert p["passed"] and p["inconclusive"] == 1 and p["cases"][0]["inconclusive"] and not p["cases"][0]["passed"]
    assert "1 value(s) without results on the site" in p["reason"]
    p = HC.prove_web(tool, site, ["Zxqvbn Plorf", "Qwrtz Blorx", "Akkuschrauber"])
    assert not p["passed"] and p["inconclusive"] == 2 and "only 1 fresh value(s) passed of 3 tried" in p["reason"]
    rep = HC.compile_and_save_web(tr, THC.storage(), site, [])
    assert rep["status"] == "ready", rep["reasons"]
    assert [c["source"] for c in rep["proof"]["cases"]] == ["result_titles", "result_titles"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
