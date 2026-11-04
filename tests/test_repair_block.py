"""web-heal-live, fix 3 (ladder-only, OFF by default): the BLOCK sentinel (``repair_ladder.block_results`` /
``LadderConfig(block_sentinel=True)``), offline on fakes built from the round's snapshots (no browser, no network, no
model).

What must hold:

1. ``block_results``: the round's www.traumflieger.de result page (the answered links in ``aside > div#box_recent >
   ul.tf-sidelist``) and www.woodworker.de's (``div#sidebar-a > ul``) are blocks; www.raumfahrer.net's real results in
   ``article.post.type-post.format-aside`` inside ``main`` are not (a post format, no sidebar); a result linked once in
   the results and once in a sidebar is no block; a promotion / recommendation container is;
2. through the ladder: a list answer from a sidebar is reported with the sentinel off (exactly as before) and never
   accepted with it on (the run is "incomplete", the answer kept as evidence); a version-12 upgrade candidate that reads
   the real results is accepted through the gate; a candidate that reads the sidebar too is no answer.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import Store  # noqa: E402

BASE = "https://www.blocks.test"
MAIN = [(f"{BASE}/test/sony-rx10-v", "Sony RX10 V Test"), (f"{BASE}/test/rollei-fotoliner", "Rollei Fotoliner Ocean"),
        (f"{BASE}/test/lumix-tz300", "Panasonic Lumix DC-TZ300")]
SIDE = [(f"{BASE}/foto-praxis/bildkontrolle", "Bildkontrolle vor dem Klick"),
        (f"{BASE}/ratgeber/online-casinos", "Beste Online Casinos in Deutschland"),
        (f"{BASE}/foto-praxis/makro", "Makro mit dem Objektiv")]


def links(xs):
    return "".join(f'<li class="tf-sidelist__item"><a href="{u}">{t}</a></li>' for u, t in xs)


TRAUM = ('<html><body><div id="frame_main_content"><div id="main"><h1>Suchergebnisse für: Objektiv</h1>'
         '<div class="tf-results">' + "".join(f'<div class="tf-card tf-card--img-sm"><a href="{u}">{t}</a></div>' for u, t in MAIN)
         + '</div><div class="clearfix"><aside><div id="box_recent" class="tf-box"><div class="tf-box__body">'
         '<h3>NEUE REPORTS</h3><ul class="tf-sidelist">' + links(SIDE) + '</ul></div></div></aside></div></div></div>'
         '</body></html>')
WOOD = ('<html><body><div id="content"><h1>Suchergebnis</h1><table>' +
        "".join(f'<tr><td><a href="{u}">{t}</a></td></tr>' for u, t in MAIN) +
        '</table></div><div id="sidebar-a"><h3>weiter News</h3><ul>' + links(SIDE) + '</ul></div></body></html>')
RAUM = ('<html><body><div id="content" class="site-content"><div class="ast-container"><div id="primary" class="content-area">'
        '<main id="main" class="site-main"><div class="ast-row">' + "".join(
            f'<article class="post type-post format-aside"><div class="ast-post-format-standard"><div '
            f'class="ast-blog-featured-section post-thumb"><a href="{u}">{t}</a></div></div></article>' for u, t in MAIN) +
        '</div></main></div></div></div></body></html>')
PROMO = ('<html><body><main><div class="recommendations"><a href="' + SIDE[0][0] + '">x</a><a href="' + SIDE[1][0] +
         '">y</a></div><ul class="results">' + links(MAIN) + '</ul></main></body></html>')


def test_block_results_on_the_rounds_structures():
    side = [u for u, _t in SIDE]
    main = [u for u, _t in MAIN]
    got = L.block_results(TRAUM, BASE + "/?s=Objektiv", side + main)
    assert [g[0] for g in got] == side and "aside" in got[0][1], got
    got = L.block_results(WOOD, BASE + "/suche", side[:2] + main)
    assert [g[0] for g in got] == side[:2] and "sidebar" in got[0][1], got
    assert L.block_results(RAUM, BASE + "/?s=Artemis", main) == []
    both = TRAUM.replace('<div class="tf-results">', f'<div class="tf-results"><div class="tf-card"><a href="{side[0]}">x</a></div>')
    assert L.block_results(both, BASE + "/?s=Objektiv", [side[0]]) == []        # also linked in the results
    got = L.block_results(PROMO, BASE + "/?q=x", side[:2] + main)
    assert [g[0] for g in got] == side[:2] and ("promotion" in got[0][1] or "widget" in got[0][1]), got
    assert L.LadderConfig().block_sentinel is False


class Page:
    def __init__(self, dom, url, answer):
        self._dom, self._url, self.answer = dom, url, answer

    async def dom(self):
        return self._dom

    async def url(self):
        return self._url


def _tool(version=10, read="side"):
    return {"workflow": "site_search", "site": "www.blocks.test", "version": version, "start_url": BASE + "/",
            "search": {"mode": "url", "template": BASE + "/?s={q}"}, "region": {"cards": [{"kind": "title"}]},
            "checks": {}, "params": [], "read": read, "hosts": ["www.blocks.test"]}


def _run(tool_reads, cfg, upgrade=None, checker=None):
    """run_with_ladder with run_tool answering the tool's ``read`` (the sidebar's links or the results) on TRAUM."""
    saved = T.run_tool

    async def fake(page, tool, value, settle, **kw):
        xs = SIDE if tool.get("read") == "side" else MAIN
        return {"value": value, "status": "ok", "why": "query: 3 items", "model_calls": 0,
                "output": {"results": [{"title": t, "url": u} for u, t in xs]},
                "checks": {"result_page": {"n_items": 3}}, "pages": [{"title": "Suche: Objektiv", "h1": ["Objektiv"]}]}
    T.run_tool = fake
    st = Store(tool_reads)
    try:
        out = asyncio.run(L.run_with_ladder(lambda: Page(TRAUM, BASE + "/?s=Objektiv", None), tool_reads, "Objektiv",
                                            lambda p: None, config=cfg, store=st.s, upgrade=upgrade, checker=checker,
                                            result_wait_s=0.0))
        return out, st.s
    finally:
        T.run_tool = saved
        st.close()


def test_the_sentinel_through_the_ladder():
    off = L.LadderConfig(enabled=True, tier1=False, tier3=False)
    out, _st = _run(_tool(), off)
    assert out["status"] == "ok" and out["ladder"]["outcome"] == L.OK          # off: reported, exactly as before
    on = L.LadderConfig(enabled=True, tier1=False, tier3=False, block_sentinel=True)
    out, _st = _run(_tool(), on)
    assert out["status"] == "incomplete" and out["output"] is None and "block" in out["why"], out
    assert out["unverified_output"]["results"][0]["url"] == SIDE[0][0]
    # an upgrade candidate that reads the real results: accepted through the gate (the checker agrees)
    agree = lambda t, v, o: {"verdict": "agrees", "why": "fake"}                  # noqa: E731
    out, st = _run(_tool(), on, upgrade=lambda t: _tool(12, "main") if t["version"] < 12 else None, checker=agree)
    assert out["status"] == "ok" and out["ladder"]["outcome"] == L.UPGRADED and \
        out["output"]["results"][0]["url"] == MAIN[0][0], out.get("ladder")
    # a candidate that reads the sidebar too: never an answer
    out, st = _run(_tool(), on, upgrade=lambda t: _tool(12, "side") if t["version"] < 12 else None, checker=agree)
    assert out["status"] != "ok" and out["output"] is None, out.get("ladder")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
