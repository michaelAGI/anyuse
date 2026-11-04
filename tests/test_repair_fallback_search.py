"""Ladder-only: tier 1's search step also tries the tool's recorded fallback
search URLs when the site shows no GET search form (``repair_relocate._fallback_search`` /
``LadderConfig(fallback_search=True)``), offline on a fake shop shaped like www.anisearch.de (no browser, no
network, no model).

The shape: the compiled search URL carries a stale token the site now ignores (it serves an index page that lists
nothing for the input), the site's own search form is a POST (never submitted), and the compile saw the site's GET
search URL on its start page (``fallback_templates``).

What must hold:

1. ``_fallback_search``: a ``rebind_param`` to the fallback's path and parameter, on the tool's own scheme and host only;
   none for a fallback on another host or the same path and parameter;
2. with the flag off (the default) tier 1 has no search candidate - "needs human", exactly as before; with it on, the
   fallback is tried and the gate decides: the checker agrees -> a tier-1 revision answering the site's results.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from anyuse.execution import repair_relocate as R  # noqa: E402
from test_repair_ladder import BASE, Shop, Store, compiled, ladder_run  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"
STALE = BASE + "/index/page-1?char=all&text={q}&kev=937aeede"


class PostShop(Shop):
    """The site's search form is a POST; its old index URL lists nothing for any input."""

    def start(self):
        return super().start().replace('method="get"', 'method="post"').replace('action="/search"', 'action="/"')

    def route(self, url):
        if "/index/" in url:
            return D.Recorded(url, "<html><head><title>Index</title></head><body><main><h1>Index</h1><ul class=\"idx\">"
                                   "<li><a href=\"/p/x\">Anderes</a></li></ul></main></body></html>"), url
        return super().route(url)


def stale_tool():
    t = compiled(V)
    t["search"] = dict(t["search"], mode="url", template=STALE, fallback_templates=[BASE + "/search?q={q}"])
    return t


def test_fallback_candidates():
    t = stale_tool()
    got = R._fallback_search(t)
    assert len(got) == 1 and got[0]["patch"]["ops"][0] == {"op": "rebind_param", "param": "q",
                                                            "to": {"in": "query", "key": "q", "path": "/search"}}
    other = dict(t, search=dict(t["search"], fallback_templates=["https://www.other.test/search?q={q}", STALE]))
    assert R._fallback_search(other) == []
    assert L.LadderConfig().fallback_search is True                 # ON by default


def test_tier1_tries_the_fallback_with_the_flag():
    t = stale_tool()
    agree = lambda tool, v, out: {"verdict": "agrees", "why": "fake"}            # noqa: E731
    st = Store(t)
    try:
        out = ladder_run(PostShop(), t, V, st.s, config=L.LadderConfig(enabled=True, tier3=False, fallback_search=False), checker=agree)
        assert out["status"] != "ok" and out["ladder"]["outcome"] == L.NEEDS_HUMAN, out.get("ladder")
        out = ladder_run(PostShop(), t, V, st.s, config=L.LadderConfig(enabled=True, tier3=False, fallback_search=True),
                         checker=agree)
        lad = out["ladder"]
        assert out["status"] == "ok" and lad["outcome"] == L.REPAIRED and lad["tier"] == 1, lad
        assert out["output"]["name"] == V and st.s.active_tool()["search"]["template"].startswith(BASE + "/search?")
    finally:
        st.close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
