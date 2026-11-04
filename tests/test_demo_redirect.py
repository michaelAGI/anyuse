"""Web tools version 14: a demonstration whose search the site redirected to a
landing page (a brand or collection page that does not carry the typed value)
compiles to the search URL the site formed; the browser follows the redirect on
every run. Version 13 still builds the template it guessed from the landing page's
path. Offline (``dom_page``).
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_repair_ladder import BASE, SITE, Shop, page_dict  # noqa: E402

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import dom_page as D  # noqa: E402


class LandingShop(Shop):
    """``/search?q=X`` lands on ``/c/<x>``: the collection page holds the results."""

    def route(self, url):
        sp = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(sp.query))
        if sp.path == "/search" and q.get("q"):
            landing = f"{BASE}/c/{urllib.parse.quote(q['q'].split()[0].lower())}"
            return D.Recorded(landing, self.results(q["q"])), landing
        if sp.path.startswith("/c/"):
            return D.Recorded(url, self.results(urllib.parse.unquote(sp.path[3:]))), url
        return super().route(url)


def landing_trace(value="Makita"):
    site = LandingShop()
    start = BASE + "/"
    p0 = page_dict(site, start)
    hop = f"{BASE}/search?{urllib.parse.urlencode({'q': value})}"
    rec, landed = site.route(hop)
    p1 = dict(site.read(rec, landed))
    p1.setdefault("search_urls", [])
    links = [e for e in p1["elements"] if e["tag"] == "a" and "/p/" in (e.get("href") or "")]
    res = {"results": [{"title": e["text"], "url": e["href"]} for e in links[:5]]}
    steps = [{"k": 0, "page": p0, "action": {"action": "search", "element": 0, "text": value},
              "outcome": {"ok": True, "how": "typed", "input": {"index": 0, "name": "q"},
                          "url_after": landed, "documents": [hop, landed]}},
             {"k": 1, "page": p1, "action": {"action": "done", "result": res}, "outcome": {}}]
    return {"version": 1, "workflow": "site_search", "site": SITE, "start_url": start, "value": value,
            "steps": steps, "final_page": p1, "result": res, "stop": "done", "forbidden": []}, landed


def test_version_14_replays_the_search_url_the_site_formed():
    tr, landed = landing_trace()
    got = C.compile_trace(tr, version=14)
    assert got["ok"], got.get("why")
    search = got["tool"]["search"]
    assert search["mode"] == "url" and search["template"] == f"{BASE}/search?q={{q}}", search
    assert got["report"]["search"]["redirected_to"] == landed


def test_a_document_after_the_result_page_is_not_the_search_url():
    # no redirect: the search URL is the result page; a same-site frame the result page
    # loads afterwards (it carries the value too) is not the search
    tr, _landed = landing_trace()
    site = LandingShop()
    hop = f"{BASE}/c/makita"
    rec, landed = site.route(hop)
    tr["steps"][0]["outcome"].update(url_after=landed,
                                     documents=[landed, f"{BASE}/frames/ads?q=Makita"])
    tr["steps"][1]["page"] = dict(site.read(rec, landed), search_urls=[])
    got = C.compile_trace(tr, version=14)
    assert got["ok"], got.get("why")
    assert got["tool"]["search"]["template"] == f"{BASE}/c/{{q}}", got["tool"]["search"]
    assert "redirected_to" not in got["report"]["search"]


def test_version_13_keeps_the_template_it_guessed_from_the_landing_path():
    tr, _landed = landing_trace()
    got = C.compile_trace(tr, version=13)
    assert got["ok"], got.get("why")
    assert got["tool"]["search"]["template"] == f"{BASE}/c/{{q}}"
    assert "redirected_to" not in got["report"]["search"]


def test_the_compiled_tool_runs_through_the_redirect():
    tr, _landed = landing_trace()
    tool = C.compile_trace(tr, version=14)["tool"]
    out = D.run(LandingShop(), tool, "Bosch")
    assert out["status"] == "ok", out
    assert out["gotos"][0] == f"{BASE}/search?q=Bosch"
    assert out["output"]["results"] and all("Bosch" in r["title"] for r in out["output"]["results"])


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
