"""Ladder-only, OFF by default: the availability check's v2 (``repair_check.availability_contradiction
(v2=True)``, ``OracleChecker(availability=2)``) and the AVAILABILITY sentinel (``LadderConfig(availability_sentinel=
True)``), offline on fakes built from the live run's saved pages (no browser, no network, no model).

The live run's false accepts with a wrong availability word, all through probation's checker (it agreed):

* www.feuerdepot.de (held-out): the upgraded revision answered InStock; the buy box says "Lieferzeit ca.: 5-10
  Werktage *" and "Ware befindet sich in Zulauf" (the goods are on their way to the shop) - version 9's vocabulary has
  no class for it;
* teufel.de (in-sample): LimitedAvailability next to a plain "Auf Lager, in 2 - 4 Werktagen bei dir" (a clearance
  label "RESTPOSTEN" above) - the check merged "a few left" into "in stock".

What must hold:

1. v2 contradicts both (v1 does not); a few-left statement still agrees with LimitedAvailability, a plain in-stock line
   with InStock; a sold-out line contradicts "a few left" in both versions;
2. ``judge(availability=2)`` disagrees with the false accept, ``judge(availability=True)`` agrees;
3. through the ladder: the tier-0 answer is reported with the sentinel off (exactly as before) and withheld with it on.
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import repair_check as RC  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import Store  # noqa: E402

FEUER = "\n".join(["Kachelofeneinsatz Buderus Logaflame HLS117 8 kW", "-35 %", "3.099,95 €", "2.029,00 €",
                   "Preise inkl. USt.", "Gewünschte Menge:", "1", "IN DEN WARENKORB", "Lieferzeit ca.: 5-10 Werktage *",
                   "Ware befindet sich in Zulauf", "BESTPREISANGEBOT", "GÜNSTIGER GESEHEN?"])
TEUFEL = "\n".join(["RESTPOSTEN", "Noch keine Bewertungen", "Teufel MASSIVE + Shure MV5C", "Farbe: Schwarz",
                    "219,99 €", "Inkl. MwSt und zzgl. Versandkosten 4,99 €", "In den Warenkorb",
                    "Auf Lager, in 2 – 4 Werktagen bei dir", "Sicher einkaufen mit 8 Wochen Rückgaberecht"])
FEW = "\n".join(["Teufel MASSIVE", "219,99 €", "In den Warenkorb", "Nur noch 2 Stück auf Lager"])
SOLD = "\n".join(["Mozart Chocolate Gold Cream", "24,90 €", "Ausverkauft"])


def test_v2_on_the_rounds_buy_boxes():
    assert RC.availability_contradiction(FEUER, "InStock", 2029.0) == ""                       # v1: no statement read
    assert "on order" in RC.availability_contradiction(FEUER, "InStock", 2029.0, v2=True)
    assert RC.availability_contradiction(TEUFEL, "LimitedAvailability", 219.99) == ""          # v1: merged
    assert "plain in stock" in RC.availability_contradiction(TEUFEL, "LimitedAvailability", 219.99, v2=True)
    assert RC.availability_contradiction(TEUFEL, "InStock", 219.99, v2=True) == ""
    assert RC.availability_contradiction(FEW, "LimitedAvailability", 219.99, v2=True) == ""
    for v2 in (False, True):
        assert "out of stock" in RC.availability_contradiction(SOLD, "LimitedAvailability", 24.90, v2=v2)
    # a visible statement answer (no schema word) is never contradicted; an on-order answer agrees with the Zulauf line
    assert RC.availability_contradiction(FEUER, "Lieferzeit ca.: 5-10 Werktage", 2029.0, v2=True) == ""
    assert RC.availability_contradiction(FEUER, "BackOrder", 2029.0, v2=True) == ""


def test_judge_with_v2():
    tool = {"workflow": "product_lookup", "site": "www.feuerdepot.test"}
    url = "https://www.feuerdepot.test/kachelofeneinsatz-buderus-logaflame-hls117-8kw/"
    out = {"status": "ok", "output": {"name": "KACHELOFENEINSATZ BUDERUS LOGAFLAME HLS117 8 KW", "price": 2029.0,
                                      "currency": "EUR", "availability": "InStock", "url": url}}
    ev = {"links": [{"url": url, "text": "Kachelofeneinsatz Buderus Logaflame HLS117 8 kW"}]}
    items = {url: {"url": url, "title": "Kachelofeneinsatz Buderus Logaflame HLS117 8 kW", "text": FEUER}}
    assert RC.judge(tool, "Kachelofeneinsatz Buderus Logaflame HLS117 8 kW", out, ev, items, availability=True)["verdict"] == "agrees"
    v = RC.judge(tool, "Kachelofeneinsatz Buderus Logaflame HLS117 8 kW", out, ev, items, availability=2)
    assert v["verdict"] == "disagrees" and "on order" in v["availability"], v


class Page:
    def __init__(self, text):
        self._text = text

    async def text(self):
        return self._text

    async def dom(self):
        return "<html></html>"

    async def url(self):
        return "https://www.feuerdepot.test/p"


def test_the_sentinel_through_the_ladder():
    tool = {"workflow": "product_lookup", "site": "www.feuerdepot.test", "version": 10, "start_url": "https://www.feuerdepot.test/",
            "search": {"mode": "url", "template": "https://www.feuerdepot.test/suche?q={q}"}, "region": {"cards": []},
            "checks": {}, "params": [], "hosts": ["www.feuerdepot.test"]}
    saved = T.run_tool

    async def fake(page, tool, value, settle, **kw):
        return {"value": value, "status": "ok", "why": "identity and fields checked", "model_calls": 0,
                "output": {"name": value, "price": 2029.0, "currency": "EUR", "availability": "InStock",
                           "url": "https://www.feuerdepot.test/p"}, "checks": {}, "pages": [{"title": value, "h1": [value]}]}
    T.run_tool = fake
    try:
        for on in (False, True):
            st = Store(tool)
            try:
                cfg = L.LadderConfig(enabled=True, tier1=False, tier3=False, availability_sentinel=on)
                out = asyncio.run(L.run_with_ladder(lambda: Page(FEUER), tool, "Kachelofeneinsatz Buderus Logaflame HLS117 8 kW",
                                                    lambda p: None, config=cfg, store=st.s, result_wait_s=0.0))
                if on:
                    assert out["status"] == "incomplete" and out["output"] is None and "on order" in out["why"], out["why"]
                else:
                    assert out["status"] == "ok" and out["output"]["availability"] == "InStock"
            finally:
                st.close()
    finally:
        T.run_tool = saved
    assert L.LadderConfig().availability_sentinel is False


if __name__ == "__main__":
    test_v2_on_the_rounds_buy_boxes()
    test_judge_with_v2()
    test_the_sentinel_through_the_ladder()
    print("test_repair_availability2: 3 ok")
