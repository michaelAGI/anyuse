"""web-heal-live, fix 4 (ladder-only, OFF by default): no re-demonstration for a CONTENT check failure
(``repair_ladder.content_failure`` / ``LadderConfig(skip_tier3_on_content=True)``), offline on fakes built from the round's
tier-3 records (no browser, no network, no CLI).

What must hold:

1. ``content_failure`` names the round's content checks - www.uhrenlounge.de's and www.michenaud.com's availability
   CONFLICTs, www.inktweb.nl's identity, duka.com's "the asked-for variant is not listed", an ambiguous tie - and none of
   the structural failures (membership, unreadable result cards, no result naming the input, a search page);
2. with the flag a content failure gets no re-demonstration (the attempt is recorded as skipped, no demonstrator call,
   no cost); a structural failure still gets one; with the flag off (the default) both get one - exactly as before.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import Store  # noqa: E402

CONTENT = ["conflict: availability: microdata 'PreOrder' vs visible in_stock ('. zzgl. 5,95 € versandkosten. verfügbar "
           "lieferbar in ca. 4-7')",
           "conflict: availability: jsonld 'OutOfStock' vs visible in_stock ('informe du prochain arrivage en stock')",
           "identity: the product page names 67% of the input's words",
           "incomplete: identity: the asked-for variant is not listed - no result carries all of the input's words",
           "incomplete: ambiguous: 3 of the 3 results naming every word of the input match it equally closely"]
STRUCTURAL = ["membership: no item of the compiled region on the result page and no 'no results' statement",
              "incomplete: 3 result(s) among the top 5 could not be read (a result card with no link in the title)",
              "incomplete: no result names the input (best share 0.60 < 0.75)",
              "search_page: the search ended off the site (chrome-error://chromewebdata/)"]


def test_content_failures():
    for w in CONTENT:
        st = "incomplete" if w.startswith("incomplete") else "failed"
        assert L.content_failure({"status": st, "why": w}), w
    for w in STRUCTURAL:
        st = "incomplete" if w.startswith("incomplete") else "failed"
        assert L.content_failure({"status": st, "why": w}) == "", w
    assert L.content_failure({"status": "ok", "why": CONTENT[0]}) == ""
    assert L.LadderConfig().skip_tier3_on_content is False


TOOL = {"workflow": "product_lookup", "site": "www.uhren.test", "version": 12, "start_url": "https://www.uhren.test/",
        "search": {"mode": "url", "template": "https://www.uhren.test/s?q={q}"}, "region": {}, "checks": {}, "params": [],
        "hosts": ["www.uhren.test"]}


def _run(why, cfg):
    saved = T.run_tool
    calls = []

    async def fake(page, tool, value, settle, **kw):
        return {"value": value, "status": "incomplete" if why.startswith("incomplete") else "failed", "why": why,
                "output": None, "model_calls": 0, "checks": {}, "pages": []}

    def demonstrator(goal, hint):
        calls.append(goal)
        return {"trace": None, "calls": 0, "usd": 0.0}
    T.run_tool = fake
    st = Store(TOOL)
    try:
        out = asyncio.run(L.run_with_ladder(lambda: object(), TOOL, "Junghans Meister", lambda p: None, config=cfg,
                                            store=st.s, demonstrator=demonstrator, result_wait_s=0.0))
        return out, calls, st.s.costs()
    finally:
        T.run_tool = saved
        st.close()


def test_no_redemonstration_for_a_content_failure():
    on = L.LadderConfig(enabled=True, tier1=False, tier3=True, skip_tier3_on_content=True)
    off = L.LadderConfig(enabled=True, tier1=False, tier3=True)
    out, calls, costs = _run(CONTENT[0], on)
    att = out["ladder"]["evidence"]["attempts"]
    assert calls == [] and costs == [] and att and "content check" in att[0]["skipped"], att
    assert out["status"] == "failed" and out["ladder"]["outcome"] == L.NEEDS_HUMAN
    out, calls, costs = _run(STRUCTURAL[0], on)                                     # structural: re-demonstrated
    assert len(calls) == 1 and len(costs) == 1
    for w in (CONTENT[0], STRUCTURAL[0]):                                           # off: exactly as before
        out, calls, costs = _run(w, off)
        assert len(calls) == 1 and len(costs) == 1


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
