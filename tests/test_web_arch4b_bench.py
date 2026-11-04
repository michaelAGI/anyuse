"""The web_arch4b_live bench, offline: fakes only, no browser, no network,
no model.

What must hold:

1. its seed, files, labels and caps are set on the web_arch4_live bench and report (module globals read at
   call time);
2. the frame: a host an earlier bench contacted is dropped by REGISTRABLE domain (a www/apex variant too), from
   web_arch4_live's remainder and from the new names alike; a new name in the prior-host snapshot, a government
   host or NEVER_CONTACT is dropped; the new names are drawn per stratum up to the target; DEV sites are
   the stratum tails and never in the measured frame; the order interleaves 3 shops : 2 others;
3. a traffic violation writes the raw events of the flagged requests and the HARD_STOP flag to runs/
   BEFORE the process stops, and the flag stops every lane (the DEV folder's flag too);
4. DEV keeps every screen render for the offline re-run of the value rules; nothing is kept otherwise.
"""
import gzip
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import web_arch4b_live as B  # noqa: E402

A4 = B.A4


def test_the_frame():
    B.configure()
    r4 = [("www.old-shop.de", "shop", "x"), ("old-apex.nl", "shop", "x"), ("www.kept-shop.fr", "shop", "x"),
          ("www.kept-blog.de", "other", "x")]
    contacted = {"old-shop.de", "www.old-apex.nl"}
    raw = [(f"www.newshop{i}.de", "shop", "y") for i in range(40)] + \
          [(f"www.newblog{i}.de", "other", "y") for i in range(30)] + \
          [("shop.old-shop.de", "shop", "y"), ("www.prior-host.com", "other", "y"), ("www.agency.gov", "other", "y"),
           ("www.nc-host.de", "shop", "y")]
    old = dict(B.FRAME_TARGET), dict(B.N_DEV), B.DEV_EXTRA_SHOPS, B.SHOP_SUPPLEMENT
    try:
        B.FRAME_TARGET.update(shop=20, other=12)
        B.N_DEV.update(shop=3, other=2)
        B.SHOP_SUPPLEMENT = True
        B.DEV_EXTRA_SHOPS = 2
        withsup, _dv, _dd = B.frame_from(r4, raw, contacted, {"prior-host.com"}, {"www.nc-host.de"},
                                         gov=lambda h: h.endswith(".gov"))
        B.SHOP_SUPPLEMENT = False
        B.DEV_EXTRA_SHOPS = 0
        base, _dev0, _d0 = B.frame_from(r4, raw, contacted, {"prior-host.com"}, {"www.nc-host.de"},
                                        gov=lambda h: h.endswith(".gov"))
        B.DEV_EXTRA_SHOPS = 2
        frame, dev, dropped = B.frame_from(r4, raw, contacted, {"prior-host.com"}, {"www.nc-host.de"},
                                           gov=lambda h: h.endswith(".gov"))
    finally:
        B.FRAME_TARGET.clear(), B.FRAME_TARGET.update(old[0])
        B.N_DEV.clear(), B.N_DEV.update(old[1])
        B.DEV_EXTRA_SHOPS = old[2]
        B.SHOP_SUPPLEMENT = old[3]
    # the DEV extension: the last 2 shops of the measured order become DEV, 2 further new shops are
    # appended at the end; the rest of the order is unchanged
    moved = [e["site"] for e in base if e["stratum"] == "shop"][-2:]
    assert [e["site"] for e in dev if e.get("dev_extension")] == moved
    assert [e["site"] for e in frame[:-2]] == [e["site"] for e in base if e["site"] not in moved]
    assert all(e["source"] == "new_4b_after_dev_extension" for e in frame[-2:])
    # the shop supplement: the undrawn new shops follow the frame (shops only, in the draw's order)
    sup = [e for e in withsup if e["source"] == "shop_supplement"]
    assert withsup[:len(frame)] == frame and withsup[len(frame):] == sup and sup
    assert all(e["stratum"] == "shop" for e in sup) and not ({e["site"] for e in sup} & {e["site"] for e in frame})
    sites = [e["site"] for e in frame]
    assert "www.old-shop.de" not in sites and "old-apex.nl" not in sites and "shop.old-shop.de" not in sites
    assert set(dropped["arch4_contacted_prior"]) == {"www.old-shop.de", "old-apex.nl"}
    assert "www.prior-host.com" in dropped["new_prior"] and "www.agency.gov" in dropped["new_government"]
    assert "www.nc-host.de" in dropped["new_never_contact"]
    allk = frame + dev
    assert sum(e["stratum"] == "shop" for e in allk) == 22 and sum(e["stratum"] == "other" for e in allk) == 12
    assert {"www.kept-shop.fr", "www.kept-blog.de"} <= {e["site"] for e in allk}
    assert len(dev) == 7 and not ({e["site"] for e in dev} & set(sites))
    assert [e["stratum"] for e in frame[:5]] == ["shop", "shop", "shop", "other", "other"]
    again = B.frame_from(r4, raw, contacted, {"prior-host.com"}, {"www.nc-host.de"}, gov=lambda h: h.endswith(".gov"))
    assert again[0] == B.frame_from(r4, raw, contacted, {"prior-host.com"}, {"www.nc-host.de"},
                                    gov=lambda h: h.endswith(".gov"))[0]


def test_the_cohort_fills_short_shop_slots_with_others():
    B.configure()
    sites = {}
    for i in range(12):
        sites[f"s{i}.shop"] = {"stratum": "shop", "screen": "eligible", "index": i, "values": {"demo": "a"}}
    for i in range(24):
        sites[f"o{i}.other"] = {"stratum": "other", "screen": "eligible", "index": 100 + i, "values": {"demo": "a"}}
    c = A4.cohort_from({"sites": sites})
    assert len(c["main"]["shop"]) == 12 and len(c["main"]["other"]) == 18 and len(c["order"]) == 30
    assert c["filled_shop_slots_with_others"] == ["o14.other", "o15.other", "o16.other", "o17.other"]
    assert c["reserves"]["other"][0] == "o18.other" and c["reserves"]["shop"] == []
    assert sorted(c["demonstrator"].values()).count("claude") == 15
    assert A4.EXTENSION_TARGET == {"shop": 20, "other": 24} and A4.DEMO_CALL_CAP == 280
    # enough shops: nothing is filled
    for i in range(12, 22):
        sites[f"s{i}.shop"] = {"stratum": "shop", "screen": "eligible", "index": i, "values": {"demo": "a"}}
    c = A4.cohort_from({"sites": sites})
    assert len(c["main"]["shop"]) == 16 and len(c["main"]["other"]) == 14 and not c["filled_shop_slots_with_others"]


def test_the_frozen_value_rules():
    """The DEV rules: stock / CTA prefixes and variant tails cleaned,
    a long title's name part, chrome and promotions never a value, the seed's product cluster strict,
    the wider 'no results' lexicon, the search URL's HTTP error status."""
    import asyncio
    B.configure()
    assert A4.clean_title("I lager Silverette Bröstvårtsskydd 925 Silver") == "Silverette Bröstvårtsskydd 925 Silver"
    assert A4.clean_title("Produktkort Samsung SmartTag2 Svart") == "Samsung SmartTag2 Svart"
    assert A4.clean_title("Weiter zum Produkt The North Face ANTORA Regenjacke Damen") == \
        "The North Face ANTORA Regenjacke Damen"
    assert A4.clean_title("Røros Tweed Lambswool blanket Bauhaus style, Red + more variants").endswith("Red")
    assert A4.clean_title("Arktic Wijnkoelkast 46 Flessen Op voorraad") == "Arktic Wijnkoelkast 46 Flessen"
    assert A4.short_name("Sola Braadpan Pompoen Oranje - Gietijzer - ø 24 cm / 2.8 Liter") == "Sola Braadpan Pompoen Oranje"
    assert A4.short_name("Hand Fork - RHS Endorsed") == "Hand Fork - RHS Endorsed"
    for chrome in ("Order by 1am for Next Day Delivery", "zzgl. Versandkosten", "Store Opening Hours",
                   "Earn Credit Points", "06 S. 56 Min. Bis zu Rabatt", "Korting in Servies & Glazen: Code 20OFF"):
        assert A4.chrome_value(chrome), chrome
    for prod in ("KIKI Health Omega 3 Fish Oil", "Dessertgabel POM-Kunststoff", "New Balance 574 Sneaker"):
        assert not A4.chrome_value(prod), prod
    vals = A4.product_values(["Free Standard Delivery On Orders", "mao feng green tea", "Sencha Green Tea Bags",
                              "Earl Grey Loose Tea", "SKIP TO CONTENT"], "www.x.test", k=5)
    assert set(vals) == {"mao feng green tea", "Sencha Green Tea Bags", "Earl Grey Loose Tea"}, vals
    rows = [("Promo Banner Deal", "https://www.x.test/deals"), ("Kettle Acme One", "https://www.x.test/p/kettle-1"),
            ("Toaster Acme Two", "https://www.x.test/p/toaster-2")]
    assert A4.product_cluster(rows, strict=True) == [] and A4.product_cluster(rows) == rows
    rows.append(("Blender Acme Three", "https://www.x.test/p/blender-3"))
    assert [t for t, _u in A4.product_cluster(rows, strict=True)] == ["Kettle Acme One", "Toaster Acme Two",
                                                                       "Blender Acme Three"]
    assert A4.C.no_results_statement("Es konnte leider nichts Passendes gefunden werden.")
    assert not A4.C.no_results_statement("Wir haben 24 Produkte gefunden")

    class Page:
        def __init__(self, status):
            self.status = status

        async def network(self, want_bodies=False):
            return [{"resource_type": "Document", "url": "https://www.x.test/search/?text=a", "status": self.status},
                    {"resource_type": "Script", "url": "https://www.x.test/a.js", "status": 200}]

        async def url(self):
            return "https://www.x.test/search/?text=a#top"
    assert asyncio.run(A4.document_status(Page(404))) == 404
    assert asyncio.run(A4.document_status(Page(200))) == 200


class _Budget:
    def __init__(self, events):
        self.violations = ["request to www.x.test reached the network without the guard: https://www.x.test/a.css"]
        self.violation_events = events
        self.browser_cache_hits = []
        self.sent = [("www.x.test", 0.0, "https://www.x.test/", "Document")]
        self.third_party_refused = []
        self.refused_methods = []


class _Ctx:
    def __init__(self, events):
        self.budget = _Budget(events)

    def used(self):
        return 3

    def nbytes(self):
        return 1000


def test_dev_keeps_the_screen_renders_only_when_asked():
    tmp = tempfile.mkdtemp()
    old = A4.DUMP_DIR
    try:
        A4.DUMP_DIR = ""
        A4.dump("www.x.test", "seed Tee", {"url": "u"})
        assert os.listdir(tmp) == []
        A4.DUMP_DIR = tmp
        A4.dump("www.x.test", "seed Grüner Tee", {"url": "u", "dom": "<a>"})
        A4.dump("www.x.test", "empty Zorqua Vexlim", {"url": "v"})
        files = sorted(os.listdir(os.path.join(tmp, "www_x_test")))
        assert files[0].startswith("00_seed_gr") and files[1].startswith("01_empty_zorqua")
        with gzip.open(os.path.join(tmp, "www_x_test", files[0]), "rt") as fh:
            assert json.load(fh)["dom"] == "<a>"
    finally:
        A4.DUMP_DIR = old
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
