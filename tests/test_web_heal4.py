"""www.reiseuhu.de: a result card belongs to the tool's RESULT SCOPE - the hosts its
recorded results link to, stored by the compile (``demo_compile.result_scope_from`` / ``result_scope``). The shared
card reader (``demo_cards.read_region_v6`` -> ``scope_items``) types a card whose link leaves that scope (another host
or subdomain) or carries a promotional marker (``rel=nofollow`` / ``sponsored``, a placement parameter such as
``ref=search_results_direct_name``) as ``promo``: reported, never an ``ok`` result row. Unknown is uncertain.

Offline, on the fake shop of ``test_repair_ladder`` (generated DOMs; no browser, no network, no model). What must hold:

1. the compile stores the result scope (the hosts and first path segments of the demonstration's reported results); a
   composed tool (``map_compile.compose_web_tool``) carries the same scope;
2. the reiseuhu shape: a deal card styled like the results, linking a sibling subdomain with ``rel=nofollow`` and a
   placement parameter, sits first under the results - the site_search answer and the composed paged_search skip it,
   type it (``checks.result_page.promo``) and answer the real results;
3. a promotional marker alone (the same host, ``rel=sponsored`` or ``?ref=...sponsored...``) types the card; a page
   whose EVERY result link is ``nofollow`` is the page's norm, nothing is typed;
4. real results on a sibling subdomain the demonstration's results use are kept (the scope is theirs);
5. a page whose cards mostly link outside the scope is uncertain (``incomplete``, "scope: ..."), never ``ok``; a scope
   without a host is uncertain;
6. a product_lookup's pick never lands on a promo card, even when its title names the value;
7. ``result_scope``: compiled -> hosts -> site -> None, in that order; a tool compiled without a scope reads
   against its declared hosts.

The judge independent of the extractor, and the sandbox's user:

8. the harness proof (``harness_compile.prove_web``) accepts an ``ok`` replay only with the INDEPENDENT judge's accept
   on the page the replay ended on, and keeps that page (URL, title, text) for every case - a refused value's page
   can be judged later; the live-checks runner keeps the replays' page records as well;
9. the independent scope verification (``attribute_verify.verify_scope``) reads the page itself: a host outside the
   declared scope or a placement relation is a conflict, no declared host is uncertain; the composed judge's cascade
   (rules -> the reader's rows -> the resolver): a scope conflict or a reader "wrong" is wrong, a title conflict of
   the rules alone is uncertain, never wrong; the live checks' paged judgement carries the independent verdict;
10. the container sandbox plan runs the tool as an unprivileged user (``--user 65534:65534``, accepted by Apple
    ``container`` 1.1.0's ``-u, --user``); the runner refuses a plan without one or as root; the staged ``out``
    directory is writable for that user; the probe's argv carries the user;
11. the repair canary reads ``result_scope`` as the tool's metadata (data the card reader checks against), not as a
    step of unknown effect (``repair_canary.METADATA_KEYS``).
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_web_heal4.py
"""
import asyncio
import html
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import pytest  # noqa: E402

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import harness_compile as HC  # noqa: E402
from anyuse.execution import harness_sandbox_runner as SR  # noqa: E402
from anyuse.execution import harness_toolwrite as TW  # noqa: E402
from anyuse.execution import map_compile as M  # noqa: E402
from test_repair_ladder import BASE, PRODUCTS, SITE, Shop, eur, trace  # noqa: E402

Q = "Akkuschrauber"
DEALS = "deals.ladder-shop.test"
SHOP = "shop.ladder-shop.test"


def card(name, href, extra=""):
    return (f"<li class=\"product\"><div class=\"info\"><h3 class=\"name\"><a class=\"title\" href=\"{href}\"{extra}>"
            f"{html.escape(name)}</a></h3><span class=\"price\">{eur(9.99)}</span></div></li>")


class CardShop(Shop):
    """The shop with extra cards (``extra_cards``: HTML before the hits) and a link attribute on every result
    (``every_rel``); ``ppath`` may be an absolute URL on another host."""

    def __init__(self, extra_cards="", every_rel="", **kw):
        super().__init__(**kw)
        self.extra_cards, self.every_rel = extra_cards, every_rel

    def results(self, q):
        out = super().results(q)
        if self.every_rel:
            out = out.replace("<a class=\"title\" href=", f"<a class=\"title\" rel=\"{self.every_rel}\" href=")
        return out.replace("<ul class=\"products\">", "<ul class=\"products\">" + self.extra_cards, 1)


DEAL = card("Ortea Palace Spa", f"https://{DEALS}/angebote?giataId=435893&ref=search_results_direct_name",
            " target=\"_blank\" rel=\"noopener noreferrer nofollow\"")
SPONSORED = card("Akkuschrauber Mega-Deal", "/p/bosch-gbh", " rel=\"sponsored\"")
REF_ONLY = card("Akkuschrauber Partnerangebot", "/p/makita-hr2470?ref=search_sponsored_placement")


def compiled(site=None, workflow="site_search", version=12):
    g = C.compile_trace(trace(site or Shop(), Q, workflow), version=version)
    assert g["ok"], g.get("why")
    return g["tool"]


def run(site, tool, value=Q, params=None):
    return asyncio.run(T.run_tool(D.DomPage(site), tool, value, D.no_settle, result_wait_s=0.0, params=params))


def slugs(out):
    return [r["url"].rsplit("/", 1)[-1].split("?")[0] for r in (out.get("output") or {}).get("results") or []]


def test_the_compile_stores_the_result_scope_and_a_composed_tool_carries_it():
    t = compiled()
    assert t["result_scope"] == {"hosts": [SITE], "paths": ["/p"], "results": 5}, t["result_scope"]
    assert C.result_scope(t) == {"hosts": [SITE], "paths": ["/p"], "source": "compiled"}
    p = compiled(workflow="product_lookup")
    assert p["result_scope"]["hosts"] == [SITE] and p["result_scope"]["paths"] == ["/p"]
    pg = M.compose_web_tool(t, "paged_search", n=10)
    assert pg["result_scope"] == t["result_scope"]
    # the scope is the demonstration's RESULT hosts, never a third-party or government host of the trace
    tr = trace(Shop(), Q, "site_search")
    tr["result"]["results"].append({"title": "x", "url": "https://tracker.example/x"})
    tr["result"]["results"].append({"title": "y", "url": "https://www.example.gov/y"})
    assert C.result_scope_from(tr, SITE)["hosts"] == [SITE]


def test_the_reiseuhu_shape_a_deal_card_on_a_sibling_subdomain_is_typed_never_a_row():
    t = compiled()
    plain = run(Shop(), t)
    assert plain["status"] == "ok" and len(slugs(plain)) == 5
    got = run(CardShop(extra_cards=DEAL), t)
    assert got["status"] == "ok" and slugs(got) == slugs(plain), (got["status"], got["why"], slugs(got))
    promo = (got["checks"]["result_page"].get("promo") or [])
    assert len(promo) == 1 and promo[0]["title"] == "Ortea Palace Spa" and promo[0]["host"] == DEALS
    assert "leaves the result scope" in promo[0]["why"] and promo[0]["url"].startswith(f"https://{DEALS}/")
    assert all(r.get("url") for r in got["output"]["results"]) and "Ortea" not in str(got["output"])
    # the composed paged_search on the same page: the deal card is skipped, n real results
    pg = M.compose_web_tool(t, "paged_search", n=4)
    out = run(CardShop(extra_cards=DEAL), pg)
    assert out["status"] == "ok" and slugs(out) == slugs(plain)[:4], (out["status"], out["why"], slugs(out))
    # the SAME page read with the scope ignored (no tool at hand) still shows every card: the rule is the tool's
    rd = D.DomPage(CardShop(extra_cards=DEAL)).site
    page = rd.read(*rd.route(f"{BASE}/search?q={Q}"))
    assert len(C.region_read(page, t["region"])["items"]) == 6
    assert len(C.region_read(page, t["region"], scope=C.result_scope(t))["items"]) == 5


def test_a_promotional_marker_alone_types_a_card_unless_it_is_the_pages_norm(monkeypatch):
    # the extractor's own card typing (arm S): the verifier is pinned off - on by default, and without a
    # model key it withholds what only the model could settle (test_fresh_coverage / test_verifier_defaults cover that)
    monkeypatch.setenv(AV.SETTING, "off")
    t = compiled()
    for extra, title in ((SPONSORED, "Akkuschrauber Mega-Deal"), (REF_ONLY, "Akkuschrauber Partnerangebot")):
        got = run(CardShop(extra_cards=extra), t)
        assert got["status"] == "ok" and len(slugs(got)) == 5 and title not in str(got["output"]), (extra, got["why"])
        promo = got["checks"]["result_page"]["promo"]
        assert len(promo) == 1 and promo[0]["title"] == title and "promotional marker" in promo[0]["why"], promo
    # every result link nofollow: the page's norm, nothing is typed, the answer is the plain one
    got = run(CardShop(every_rel="nofollow"), t)
    assert got["status"] == "ok" and slugs(got) == slugs(run(Shop(), t)) and not got["checks"]["result_page"].get("promo")
    # the marker reader itself
    assert K.promo_marker({"rel": "noopener nofollow"}, "https://x.test/a") == "rel=nofollow"
    assert K.promo_marker({"rel": ""}, "https://x.test/a?ref=search_results_direct_name").startswith("ref=")
    assert K.promo_marker({"rel": ""}, "https://x.test/a?utm_medium=cpc") == "utm_medium=cpc"
    assert K.promo_marker({"rel": ""}, "https://x.test/a?ref=header&utm_source=newsletter") == ""
    assert K.promo_marker(None, "https://x.test/a?page=2&sort=price") == ""


def test_real_results_on_a_sibling_subdomain_the_demonstration_used_are_kept():
    shop = CardShop(ppath=f"https://{SHOP}/p/")
    t = compiled(site=shop)
    # the scope is the RESULTS' host (the declared ``hosts`` stay the demonstration's navigations: www only here)
    assert t["result_scope"]["hosts"] == [SHOP] and t["hosts"] == [SITE], (t["result_scope"], t["hosts"])
    got = run(shop, t)
    assert got["status"] == "ok" and len(slugs(got)) == 5 and not got["checks"]["result_page"].get("promo"), got["why"]
    assert all(r["url"].startswith(f"https://{SHOP}/p/") for r in got["output"]["results"])


def test_a_page_mostly_outside_the_scope_is_uncertain_never_ok():
    t = compiled()                                     # the scope is www.ladder-shop.test
    moved = CardShop(ppath=f"https://{SHOP}/p/")       # the site now lists its results on shop.ladder-shop.test
    got = run(moved, t)
    assert got["status"] == "incomplete" and got["why"].startswith("scope:"), (got["status"], got["why"])
    assert "outside the tool's result scope" in got["why"] and got.get("output") is None
    # the typed items are reported with the page
    assert len(got["checks"]["result_page"]["promo"]) == 5
    # a scope without a host is unknown: uncertain
    sc = K.scope_items([{"title": "a", "url": f"{BASE}/p/x", "link": "site", "el": {}}], {"hosts": []})
    assert sc["uncertain"] and sc["items"] and not sc["promo"]
    # no scope given (the compile's self-checks): unchanged
    sc = K.scope_items([{"title": "a", "url": f"https://{DEALS}/x", "link": "site", "el": {}}], None)
    assert not sc["uncertain"] and len(sc["items"]) == 1


def test_a_product_pick_never_lands_on_a_promo_card(monkeypatch):
    # the extractor's own card typing (arm S): the verifier is pinned off - on by default, and without a
    # model key it withholds what only the model could settle (test_fresh_coverage / test_verifier_defaults cover that)
    monkeypatch.setenv(AV.SETTING, "off")
    p = compiled(workflow="product_lookup")
    value = "Bosch GSR 12V Akkuschrauber"
    decoy = card(value, f"https://{DEALS}/angebote?ref=search_results_direct_name", " rel=\"nofollow\"")
    got = run(CardShop(extra_cards=decoy), p, value)
    assert got["status"] == "ok", (got["status"], got["why"])
    assert got["output"]["url"] == f"{BASE}/p/bosch-gsr-12v" and DEALS not in got["output"]["url"]
    assert got["checks"]["result_page"]["promo"][0]["host"] == DEALS
    # the same decoy as the ONLY card naming the value: no in-scope result names it - never the decoy's URL
    far = "Ryobi R18 Bohrhammer"
    got = run(CardShop(extra_cards=card(far, f"https://{DEALS}/r18?ref=search_results_direct_name", " rel=\"nofollow\"")),
              p, far)
    assert got["status"] != "ok" or DEALS not in str(got.get("output") or {})


def test_result_scope_falls_back_from_the_compiled_scope_to_hosts_to_site_to_none():
    t = compiled()
    legacy = {k: v for k, v in t.items() if k != "result_scope"}
    assert C.result_scope(legacy) == {"hosts": [SITE], "paths": [], "source": "hosts"}
    assert C.result_scope({"site": "www.x.test"}) == {"hosts": ["www.x.test"], "paths": [], "source": "site"}
    assert C.result_scope({"workflow": "site_search"}) is None
    assert C.result_scope({"result_scope": {"hosts": ["A.X.TEST"], "paths": ["/a"]}}) == \
        {"hosts": ["a.x.test"], "paths": ["/a"], "source": "compiled"}
    # a tool compiled without a scope (declared hosts only) applies the rule through its hosts
    got = run(CardShop(extra_cards=DEAL), legacy)
    assert got["status"] == "ok" and len(slugs(got)) == 5 and got["checks"]["result_page"]["promo"][0]["host"] == DEALS


# ---------------------------------------------------------------------------
# the judges independent of the extractor
# ---------------------------------------------------------------------------

def test_the_harness_proof_needs_the_independent_judge_and_keeps_every_cases_page():
    shop = Shop()
    t = compiled()
    p = HC.prove_web(t, shop, ["Makita", "Ryobi"])
    assert p["passed"] and p["independent"] and all(c["independent"]["verdict"] == "accept" for c in p["cases"]), p["reason"]
    assert all(c["page"] and c["page"]["url"].startswith(BASE) and c["page"]["text"] for c in p["cases"])
    # the replay says ok, but its rows are not the page's results: the tool's own status no longer vouches for it
    page = HC.replay_web(t, shop, "Makita")["final_page"]
    assert page and page["dom"]

    def wrong_rows(tool, site, value):
        # (the rows differ per value - two values with the SAME rows fail as not query dependent)
        return {"status": "ok", "model_calls": 0, "why": "query: fine",
                "output": {"results": [{"title": "Not on this page", "url": f"{BASE}/p/x-{value.lower()}"},
                                       {"title": "Nor this", "url": f"{BASE}/p/y-{value.lower()}"}]}, "final_page": page}
    p = HC.prove_web(t, shop, ["Makita", "Ryobi"], replay=wrong_rows)
    assert not p["passed"] and "independent judge does not accept" in p["reason"], p["reason"]
    assert HC.prove_web(t, shop, ["Makita", "Ryobi"], replay=wrong_rows, independent=False)["passed"]
    # no page kept: unknown is uncertain, never ready
    p = HC.prove_web(t, shop, ["Makita", "Ryobi"], replay=lambda *a: dict(wrong_rows(*a), final_page=None))
    assert not p["passed"] and "uncertain" in p["reason"]
    # W-L2: a refused fresh value keeps the page it was refused on
    p = HC.prove_web(t, shop, ["Akkuschrauber Zzzq", "Ryobi"])
    refused = p["cases"][0]
    assert not p["passed"] and refused["status"] == "failed" and "does not belong" in refused["why"], refused
    assert refused["page"]["url"].startswith(f"{BASE}/search?") and "Akkuschrauber" in refused["page"]["text"]


def listing(cards, every_rel=""):
    rel = f' rel="{every_rel}"' if every_rel else ""
    items = "".join(f'<li class="product"><h3><a{rel if not x else x} href="{h}">{html.escape(n)}</a></h3>'
                    f'<p>{html.escape(d)}</p></li>' for n, h, d, x in cards)
    return f'<html><head><title>Suche</title></head><body><main><ul class="products">{items}</ul></main></body></html>'


def test_the_independent_scope_verification_reads_the_page_itself():
    cards = [("Bosch GSR 12V", f"{BASE}/p/bosch-gsr-12v", "89,90", ""),
             ("Makita DF333", f"{BASE}/p/makita-df333", "99,00", ""),
             ("Ortea Palace Spa", f"https://{DEALS}/angebote?ref=search_results_direct_name", "Flug + Hotel", ' rel="nofollow"'),
             ("Metabo PowerMaxx", f"{BASE}/p/metabo-powermaxx", "119,00", ' rel="sponsored"')]
    dom = listing(cards)
    page = AV.Page(dom, "Suche\nBosch GSR 12V\nMakita DF333\nOrtea Palace Spa\nMetabo PowerMaxx", f"{BASE}/search?q=x")
    rows = [{"title": n, "url": h} for n, h, _d, _x in cards]
    v = AV.verify_scope({"results": rows}, page, [SITE])
    st = {d.attr: d.status for d in v.decisions}
    assert st == {"scope[0]": AV.VERIFIED, "scope[1]": AV.VERIFIED, "scope[2]": AV.CONFLICT, "scope[3]": AV.CONFLICT}, st
    assert "outside the tool's result scope" in v.decisions[2].why and "placement" in v.decisions[3].why
    # no declared host: uncertain, never verified; a row with no link on the page: uncertain
    assert {d.status for d in AV.verify_scope({"results": rows[:2]}, page, []).decisions} == {AV.UNCERTAIN}
    v = AV.verify_scope({"results": [{"title": "x", "url": f"{BASE}/p/not-linked"}]}, page, [SITE])
    assert v.decisions[0].status == AV.UNCERTAIN and "no link" in v.decisions[0].why
    # every link nofollow: the page's norm, not a placement
    dom2 = listing([(n, h, d, "") for n, h, d, _x in cards[:2]], every_rel="nofollow")
    v = AV.verify_scope({"results": rows[:2]}, AV.Page(dom2, "", f"{BASE}/search?q=x"), [SITE])
    assert {d.status for d in v.decisions} == {AV.VERIFIED}
    # check() carries the scope decisions beside the titles; scope_hosts reads what the tool declares
    v = AV.check("site_search", {"results": rows[:2]}, page, None, scope=[SITE])
    assert {d.attr for d in v.decisions} >= {"title[0]", "title[1]", "scope[0]", "scope[1]"} and v.verdict == AV.ACCEPT
    assert AV.scope_hosts({"result_scope": {"hosts": ["A.X"]}, "hosts": ["b.x"], "site": "c.x"}) == ["a.x"]
    assert AV.scope_hosts({"hosts": ["b.x"], "site": "c.x"}) == ["b.x"] and AV.scope_hosts({"site": "c.x"}) == ["c.x"]
    assert AV.scope_hosts({}) == []


# ---------------------------------------------------------------------------
# the sandbox's user
# ---------------------------------------------------------------------------


def test_the_canary_reads_the_result_scope_as_metadata_not_as_a_step():
    from anyuse.execution import repair_canary as RC
    t = compiled()
    assert "result_scope" in t and RC.rerun_refusal(t) is None, RC.rerun_refusal(t)
    assert RC.effect_class(t)["class"] == RC.READ_ONLY
    assert RC.rerun_refusal(dict(t, result_scope_v2={"hosts": []}))["code"] == RC.UNKNOWN_EFFECT   # an unknown field still refuses


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
