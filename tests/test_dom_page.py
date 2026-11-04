"""The offline page over recorded DOMs (``execution/dom_page.py``): no browser,
no network.

What must hold:

1. ``serialize`` writes a parsed document back so that it parses to the same tree (text escaped, raw script kept);
2. the READ_JS emulation lists the visible controls with ``CONTROL_SEL``'s numbering (a hidden one skipped, its number
   kept), a link's URL, area and card context, a drop-down's options; the recorded reader texts are kept when given;
   ``mark_visibility`` carries the recorded reader's visibility into the DOM;
3. the site routes a URL to its exact page, else to the one same-path page that shares the most query parameters and
   contradicts none, else a 404 page;
4. ``type_search`` uses the recorded input (name / id, else index; else types nothing) and submits a GET form; a POST
   form is never submitted; a drop-down inside a GET form submits it.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_cards as K  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

URL = "https://www.dom.test/search?q=tee"
DOM = ("<html><head><title>Tee &amp; mehr</title><script>var a = 1 < 2 && \"x\";</script></head><body>"
       "<header><nav><a href=\"/\">Start</a></nav></header><main><h1>Ergebnisse</h1>"
       "<form action=\"/search\" method=\"get\"><input type=\"search\" name=\"q\" value=\"tee\">"
       "<select name=\"sort\"><option value=\"rel\" selected>Relevanz</option><option value=\"price_asc\">Preis aufsteigend"
       "</option></select></form>"
       "<ul><li class=\"product\"><a href=\"/p/gruen\">Grüner Tee</a> <span>4,50 €</span></li>"
       "<li class=\"product\"><a href=\"/p/schwarz\" style=\"display:none\">Schwarzer Tee</a></li></ul>"
       "<form method=\"post\" action=\"/login\"><input type=\"text\" name=\"user\"><input type=\"password\" name=\"pw\">"
       "</form></main></body></html>")


def test_serialize_round_trip():
    root, _b = K.parse(DOM)
    again, _b2 = K.parse(D.serialize(root))

    def shape(n):
        return [(c.tag, sorted(c.attrs.items()), shape(c)) if isinstance(c, K.Node) else c for c in n.children]
    assert shape(root) == shape(again)
    assert "1 < 2" in D.serialize(root) and "Tee &amp; mehr" in D.serialize(root)


def test_the_reader_emulation_and_visibility():
    site = D.DomSite([D.Recorded(URL, DOM)])
    r = site.read(site.pages[next(iter(site.pages))], URL)
    links = [e for e in r["elements"] if e["tag"] == "a"]
    assert [e["href"] for e in links] == ["https://www.dom.test/", "https://www.dom.test/p/gruen"]
    assert links[0]["area"] == "nav" and links[1]["ctx"].startswith("Grüner Tee") and links[1]["area"] == "main"
    sel = next(e for e in r["elements"] if e["tag"] == "select")
    assert [o["text"] for o in sel["options"]] == ["Relevanz", "Preis aufsteigend"] and sel["options"][0]["selected"]
    ctl = K.control_nodes(K.parse(DOM)[0])
    assert links[1]["i"] == next(k for k, n in enumerate(ctl) if n.attrs.get("href") == "/p/gruen")
    assert r["title"] == "Tee & mehr" and r["h1"] == ["Ergebnisse"] and "Grüner Tee" in r["main_text"]
    kept = D.DomSite([D.Recorded(URL, DOM, {"text": "recorded text", "main_text": "recorded main"})])
    r2 = kept.read(kept.pages[next(iter(kept.pages))], URL)
    assert r2["text"] == "recorded text" and r2["main_text"] == "recorded main"
    # the recorded reader's visibility carried into the DOM: the reader did not list "Start"
    rec = {"url": URL, "elements": [e for e in r["elements"] if e.get("href") != "https://www.dom.test/"]}
    marked, share = D.mark_visibility(DOM, rec)
    assert share == 1.0 and 'data-au-vis="0"' in marked
    s3 = D.DomSite([D.Recorded(URL, marked)])
    r3 = s3.read(s3.pages[next(iter(s3.pages))], URL)
    assert [e["href"] for e in r3["elements"] if e["tag"] == "a"] == ["https://www.dom.test/p/gruen"]
    assert D.link_sigs(*K.parse(DOM)[:2], URL)["https://www.dom.test/p/gruen"][0].endswith("li.product")
    assert "https://www.dom.test/p/gruen" in D.priced_links(*K.parse(DOM)[:2], URL)


def test_routing():
    a = D.Recorded("https://www.dom.test/s?q=tee", "<p>a</p>")
    b = D.Recorded("https://www.dom.test/s?q=tee&p=1&o=3", "<p>b</p>")
    site = D.DomSite([a, b])
    assert site.route("https://www.dom.test/s?q=tee")[0] is a
    assert site.route("https://www.dom.test/s?q=tee&o=3")[0] is b
    assert site.route("https://www.dom.test/s?q=tee&o=4")[0] is a
    assert site.route("https://www.dom.test/other")[0].status == 404


def test_typed_search_and_forms():
    res = D.Recorded("https://www.dom.test/search?q=kaffee&sort=rel", "<html><body><main><h1>Kaffee</h1></main></body></html>")
    sorted_ = D.Recorded("https://www.dom.test/search?q=tee&sort=price_asc", "<html><body><h1>sorted</h1></body></html>")
    site = D.DomSite([D.Recorded(URL, DOM), res, sorted_])
    pg = D.DomPage(site)
    asyncio.run(pg.goto(URL))
    got = asyncio.run(pg.type_search("kaffee", want={"name": "q"}))
    assert got["input"]["name"] == "q" and pg.cur_url == "https://www.dom.test/search?q=kaffee&sort=rel"
    asyncio.run(pg.goto(URL))
    got = asyncio.run(pg.type_search("kaffee", want={"name": "nope", "index": 99}))
    assert got["input"] is None and "not on the page" in got["note"] and pg.cur_url == URL
    # a POST form (login) is never submitted: its input is not a search input at all
    assert all(i["name"] != "user" for i in asyncio.run(pg.search_inputs()))
    root, base = K.parse(DOM)
    form = next(x for x in K.walk(root) if x.tag == "form" and x.attrs.get("method") == "post")
    assert D.form_url(form, URL, base, {"user": "x"}) is None
    # a drop-down inside a GET form submits the form
    asyncio.run(pg.goto(URL))
    r = pg.read()
    sel = next(e for e in r["elements"] if e["tag"] == "select")
    asyncio.run(pg.js(RR.SELECT_JS % (sel["i"], json.dumps("price_asc"))))
    assert pg.cur_url == "https://www.dom.test/search?q=tee&sort=price_asc"


if __name__ == "__main__":
    test_serialize_round_trip()
    test_the_reader_emulation_and_visibility()
    test_routing()
    test_typed_search_and_forms()
    print("test_dom_page: 4 ok")
