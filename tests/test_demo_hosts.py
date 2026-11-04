"""Declared hosts, offline (a fake demonstration of the www.viridea.it shape and the fake CDP of
``test_cdp_guard``; no browser, no network, no model): the compiled tool DECLARES the hosts its demonstration
used, and the site gate lets a run navigate exactly those of the site's own (its registrable domain) - no
third-party host, and the traffic guard's audit unchanged.

The failures: all three www.viridea.it requests ended ``GateRefused: no active task for shop.viridea.it: take
the run's SiteGate before navigating`` - the demonstration went from www.viridea.it to the shop on
shop.viridea.it and searched there; the run held the site gate for www.viridea.it only, so the compiled search
URL was refused before anything was sent.

What must hold:

1. the compile declares the demonstration's own hosts (start, pages, followed links, result, product),
   never a third-party or government host;
2. the run's origin policy from the tool maps the declared host to the site: one pacer slot, one request
   count; a version-5 tool declares nothing (the guard unchanged);
3. with the site gate held for the site, a navigation to the declared host goes out; an undeclared host
   of the same domain and a third-party host are refused (a version-5 tool's policy refuses the declared one);
4. a request of a page on the declared host to a third-party host is still refused by the guard;
5. ``LoadGuard.audit`` is byte-for-byte the frozen audit (its source digest).
"""
import hashlib
import inspect
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import test_cdp_guard as TG  # noqa: E402
from anyuse.execution import cdp_guard as G  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import discover as D  # noqa: E402
from anyuse.execution.traffic_gate import GateRefused, SiteGate  # noqa: E402
from test_demo_cards import read_of_html  # noqa: E402

WWW, SHOP = "www.viridea.test", "shop.viridea.test"
#: ``inspect.getsource(cdp_guard.LoadGuard.audit)`` of the frozen cdp_guard.py
# Re-pinned after one of its comments changed; its code did not.
AUDIT_SHA256 = "59b5e508989d449c43749a6759329cbe4dcc1f7dea1ea15d35e16621fae5b360"


def home_html():
    return (f'<html><body><header><nav><a href="https://{SHOP}/?utm_source=viridea">Shop online</a>'
            f'<a href="https://{WWW}/consigli/">Consigli</a></nav></header><main><h1>Viridea</h1></main></body></html>')


def result_html(q):
    tiles = "".join(f'<li class="item product"><div class="product-item-info"><div class="product details">'
                    f'<strong class="product name"><a class="product-item-link" href="https://{SHOP}/{s}">{n}</a>'
                    f'</strong></div></div></li>'
                    for s, n in (("semi-bis-di-lattughe-da-taglio-299379", "Semi bis di lattughe da taglio"),
                                 ("lattuga-gentilina-12", "Lattuga gentilina"), ("indivia-riccia-34", "Indivia riccia")))
    return (f'<html><body><div class="search results"><div class="products wrapper"><ol class="products list">'
            f'{tiles}</ol></div></div></body></html>')


def trace():
    value = "Indivie e Lattughe in varietà in pack da 9 piante"
    start = f"https://{WWW}/"
    shop = f"https://{SHOP}/?utm_source=viridea"
    rurl = f"https://{SHOP}/catalogsearch/result/?q={urllib.parse.quote_plus(value)}"
    purl = f"https://{SHOP}/semi-bis-di-lattughe-da-taglio-299379"
    prod = {"url": purl, "title": "Semi bis di lattughe da taglio", "h1": ["Semi bis di lattughe da taglio"],
            "text": "Semi bis di lattughe da taglio 1,55 € Disponibile", "main_text": "", "elements": [], "facts": [],
            "jsonld": [], "canonical": purl, "og_type": "product", "microdata": {}, "meta": {}}
    rep = {"name": "Semi bis di lattughe da taglio", "price": "1,55 €", "currency": "EUR", "url": purl}
    return {"version": 1, "workflow": "product_lookup", "site": WWW, "start_url": start, "value": value,
            "steps": [{"k": 0, "page": read_of_html(home_html(), start), "action": {"action": "click", "element": 0},
                       "outcome": {"ok": True, "href": "", "url_after": shop}},
                      {"k": 1, "page": read_of_html(home_html(), shop),
                       "action": {"action": "search", "element": 0, "text": value},
                       "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"}, "url_after": rurl}},
                      {"k": 2, "page": read_of_html(result_html(value), rurl), "action": {"action": "click", "element": 0},
                       "outcome": {"ok": True, "how": "goto", "href": purl, "url_after": purl}},
                      # a consent redirect through a third-party host on the way (never declared)
                      {"k": 3, "page": dict(prod, url="https://consent.cmp-vendor.test/r?u=x"),
                       "action": {"action": "goto", "url": "https://consent.cmp-vendor.test/r?u=x"},
                       "outcome": {"ok": True, "url_after": purl}},
                      {"k": 4, "page": prod, "action": {"action": "done", "result": rep}, "outcome": {}}],
            "final_page": prod, "result": rep, "stop": "done", "forbidden": []}


def compiled():
    got = C.compile_trace(trace())
    assert got["ok"], got
    return got["tool"]


def budget(gate):
    clock = TG.Clock()
    pacer = D.Pacer(1.0, clock=clock, sleeper=clock.sleep)
    return G.RequestBudget(pacer, asleep=clock.asleep, task_gate=gate)


def test_the_compile_declares_the_demonstrations_own_hosts():
    t = compiled()
    assert t["hosts"] == [SHOP, WWW], t["hosts"]
    assert t["search"]["template"].startswith(f"https://{SHOP}/catalogsearch/result/?q="), t["search"]
    assert not C._host_ok("consent.cmp-vendor.test", WWW) and C._host_ok(SHOP, WWW)
    assert not C._host_ok("www.viridea.test.evil.test", WWW) and not C._host_ok("*.viridea.test", WWW)


def test_the_origin_policy_maps_the_declared_host_to_the_site():
    t = compiled()
    assert C.site_hosts_policy(t) == {WWW: [SHOP]}
    assert C.site_hosts_policy(dict(t, version=5)) == {}           # version-5 tools: today's guard
    gate = SiteGate(per_site=1)
    b = budget(gate)
    assert T.apply_site_hosts(b, t) == {WWW: [SHOP]}
    assert b.site_of(SHOP) == WWW and b.site_of("other.viridea.test") == "other.viridea.test"
    assert b.origin_policy.allows(SHOP, WWW) and b.origin_policy.allows(WWW, SHOP)
    assert not b.origin_policy.allows(SHOP, "consent.cmp-vendor.test")


def test_the_site_gate_lets_the_run_navigate_exactly_the_declared_hosts():
    t = compiled()
    url = C.fill(t["search"]["template"], "Preziose di Natura")
    gate = SiteGate(per_site=1)
    # without the declared hosts: the gate holds www.viridea.test, the policy knows no other host - refused, nothing sent
    b0 = budget(gate)
    with gate.task_sync(WWW):
        s = TG.session(b0, [TG.paused(0, url, "Document")])
        try:
            TG.run(s.goto(url))
            raise AssertionError("the prior policy let the shop host through")
        except GateRefused as exc:
            assert f"no active task for {SHOP}" in str(exc)
        assert s._ws.sent == []
    # the tool's declared hosts
    b = budget(gate)
    T.apply_site_hosts(b, t)
    with gate.task_sync(WWW):
        s = TG.session(b, [TG.paused(0, url, "Document"), TG.paused(1, f"https://{WWW}/static/a.js"),
                           TG.paused(2, "https://cdn.third-party.test/x.js")])
        TG.run(s.goto(url))
        assert any(m.get("method") == "Page.navigate" and m["params"]["url"] == url for m in s._ws.sent)
        assert s._ws.answered("Fetch.continueRequest") == ["f0", "f1"], s._ws.sent
        assert [u for u, w in s.guard.failed] == ["https://cdn.third-party.test/x.js"], s.guard.failed
        assert "third-party host cdn.third-party.test" in s.guard.failed[0][1]
        s.guard.close([])
        assert b.site_total(WWW) == 2 and b.site_total(SHOP) == 2        # one count for the site
        for bad in ("https://other.viridea.test/", "https://www.example.test/"):
            s2 = TG.session(b, [])
            try:
                TG.run(s2.goto(bad))
                raise AssertionError(f"{bad} went out")
            except GateRefused:
                pass
            assert s2._ws.sent == []
    # without the gate held for the site nothing goes out, declared host or not
    s3 = TG.session(b, [])
    try:
        TG.run(s3.goto(url))
        raise AssertionError("navigated without the site's task")
    except GateRefused:
        pass


def test_the_traffic_guards_audit_is_unchanged():
    src = inspect.getsource(G.LoadGuard.audit)
    assert hashlib.sha256(src.encode()).hexdigest() == AUDIT_SHA256


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
