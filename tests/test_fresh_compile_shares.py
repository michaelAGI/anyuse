"""Compiling on unseen sites (the replay's scripts, titles, the verifier), offline. Pinned:
* the run cache records each refusal with the response's own cache headers (www.drako.it: its scripts were never
  stored, the reason unknown); a proof replay may send up to its own share of SAME-ORIGIN scripts past the static share,
  counted, within the site cap (``REPLAY_DOC_RESERVE`` left for its documents and data); a third-party script, a
  closed share or a nearly spent cap sends nothing more; the product opens the share around each proof replay;
* a link whose own text is not its card's heading (an image, a label "Personenseite", a teaser) is titled by the CARD'S
  heading when that heading links the same target (www.crescendo.de) - in the agent's answer, never replaced as a
  label; a list of cards (headings linking other targets) is never one card;
* the proof's site vocabulary drops the page's own interface words by structure (header / nav / footer controls, a
  link text used for two or more targets);
* the verifier: a title that IS the card's heading is not shadowed by a longer teaser link (a date badge still is).
No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_fresh_compile_shares.py
"""
import asyncio
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import answer_refs as AR  # noqa: E402
from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_product as HP  # noqa: E402
from anyuse.execution import harness_types as T  # noqa: E402

import test_static_share as SS  # noqa: E402

H = SS.H


# ------------------------------------------------------------------------------------------------- the replay's scripts
def test_a_proof_replay_sends_its_own_share_of_same_origin_scripts():
    pacer, b = SS.budget(static_reserve=120)                 # the static share is 180 of 300
    pacer.count[H] = 190                                      # the agent's run spent it
    b.open_replay_share(5)
    script = [SS.paused(0, f"https://{H}/", "Document")] + \
        [SS.paused(i, f"https://{H}/s{i}.js", "Script") for i in range(1, 11)] + \
        [SS.paused(11, f"https://{H}/a.css", "Stylesheet"), SS.paused(12, f"https://{H}/api?q=x", "XHR")]
    s = SS.run_script(b, script)
    sent = s._ws.answered("Fetch.continueRequest")
    assert sent == ["f0", "f1", "f2", "f3", "f4", "f5", "f12"]              # the document, 5 scripts, the data
    assert b.replay_scripts_sent == [5] and pacer.count[H] == 197          # counted
    b.close_replay_share()
    assert not b.replay_script_ok(H, H)
    # a nearly spent cap leaves the replay's documents their reserve
    pacer2, b2 = SS.budget(static_reserve=120)
    pacer2.count[H] = 300 - G.REPLAY_DOC_RESERVE
    b2.open_replay_share(5)
    assert not b2.replay_script_ok(H, H)
    # a third-party script is never in the share
    pacer3, b3 = SS.budget(static_reserve=120)
    b3.open_replay_share(5)
    assert not b3.replay_script_ok(H, "cdn.other.example")
    assert HP.PROOF_SCRIPT_SHARE > 0


def test_the_run_cache_records_why_it_refused_with_the_headers():
    _pacer, b = SS.budget()
    b.cache_enabled = True
    assert not b.remember(f"https://{H}/app.js", 200, {"Cache-Control": "no-store", "Vary": "Accept-Encoding"},
                          "x=1", rtype="Script")
    r = b.cache_refusals[-1]
    assert r["why"] == "no-store" and r["cache-control"] == "no-store" and r["url"].endswith("/app.js")


# ------------------------------------------------------------------------------------------------- titles
CARD = ('<div class="grid">{}</div>')
ONE = ('<div class="item"><a href="/p/{slug}/"><img src="/i.jpg" alt=""></a><div class="m"><a href="/p/{slug}/" '
       'class="label">Personenseite</a><h3><a href="/p/{slug}/">{name}</a></h3><a href="/p/{slug}/"><p>Hier finden Sie '
       'alles zu {name}.</p></a></div></div>')


def page_of(*names):
    dom = "<html><body><main>" + CARD.format("".join(ONE.format(slug=n.lower().replace(" ", "-"), name=n)
                                                      for n in names)) + "</main></body></html>"
    root, base = K.parse(dom)
    return dom, root, base


def test_a_cards_heading_names_its_image_label_and_teaser_links():
    dom, root, base = page_of("Ludwig van Beethoven", "Gustav Mahler")
    links = [n for n in K.walk(root) if n.tag == "a"]
    u = "https://www.music.test/search?s=x"
    got = [K.card_heading(a, u, base) for a in links]
    assert got == ["Ludwig van Beethoven", "Ludwig van Beethoven", "", "Ludwig van Beethoven",
                   "Gustav Mahler", "Gustav Mahler", "", "Gustav Mahler"]
    # the grid (headings linking two targets) is never one card
    grid = next(n for n in K.walk(root) if n.attrs.get("class") == "grid")
    stray = K.Node("a", {"href": "/elsewhere/"}, grid)
    grid.children.append(stray)
    assert K.card_heading(stray, u, base) == ""


def test_the_agents_answer_is_titled_by_the_cards_heading_never_as_a_label():
    dom, root, base = page_of("Ludwig van Beethoven", "Gustav Mahler")
    u = "https://www.music.test/search?s=x"
    els, n = [], 0
    for k, a in enumerate(K.control_nodes(root)):
        if a.tag == "a":
            els.append({"i": k, "tag": "a", "href": K.href_of(a, u, base), "text": K.visible_text(a)})
    # the heading text is also a link text of two targets (a tag link elsewhere) - still the title
    obs = T.Observation(kind="web", url=u, elements=[T.Element(index=j + 1, kind="link", text=e["text"] or
                                                               "Personenseite Ludwig van Beethoven Hier finden",
                                                               href=e["href"]) for j, e in enumerate(els)],
                        page={"dom": dom, "url": u, "elements": els},
                        mapping={j + 1: {"src": "el", "i": e["i"], "href": e["href"]} for j, e in enumerate(els)})
    res, _rep = AR.resolve_on_observation("site_search", {"results": [{"element": 1}, {"element": 5}]}, obs)
    assert [r["title"] for r in res["results"]] == ["Ludwig van Beethoven", "Gustav Mahler"]


def test_the_proof_vocabulary_drops_the_pages_interface_words():
    demo = {"workflow": "site_search",
            "result": {"results": [{"title": "Personenseite Ludwig Hier finden Sie", "url": "u1"},
                                   {"title": "Personenseite Mahler Hier finden Sie", "url": "u2"}]},
            "final_page": {"elements": [
                {"tag": "a", "text": "Hier geht es zum Shop", "href": "/shop", "area": "header"},
                {"tag": "a", "text": "Personenseite", "href": "/p/ludwig/", "area": "main"},
                {"tag": "a", "text": "Personenseite", "href": "/p/mahler/", "area": "main"}]}}
    vals = [v["value"] for v in HC.site_vocabulary(demo, "Beethoven")]
    assert "Personenseite" not in vals and "Hier" not in vals
    assert "Ludwig" in vals and "Mahler" in vals


# ------------------------------------------------------------------------------------------------- the verifier
def test_a_heading_title_is_not_shadowed_by_a_longer_teaser():
    dom = ('<html><body><main><div class="grid"><div class="item"><a href="/p/lvb/"><img src="/i.jpg" alt=""></a>'
           '<h3><a href="/p/lvb/">Ludwig van Beethoven</a></h3><a href="/p/lvb/">Hier finden Sie alle gesammelten '
           'Beitraege zu diesem Komponisten und seinem Werk</a></div><div class="item"><h3><a href="/p/gm/">Gustav '
           'Mahler</a></h3></div></div></main></body></html>')
    ans = {"results": [{"title": "Ludwig van Beethoven", "url": "https://www.music.test/p/lvb/"}]}
    d = AV.verify_list(ans, AV.Page(dom, "", "https://www.music.test/?s=x")).decisions[0]
    assert d.status == AV.VERIFIED, d.why


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
