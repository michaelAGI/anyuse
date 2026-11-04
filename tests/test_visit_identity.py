"""Every row ``visit_results`` opens is checked on its own item page by the independent verifier
(``attribute_verify.verify_visit``): the row's name on its card of the list page must be what the opened page calls its
item (``verify_identity``), its fields get the product checks, and a row that does not verify is withheld. Offline: the
fake shop of ``test_demo_breadth_filtered``, inline pages, a faked verifier model.
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

from anyuse.execution import attribute_verify as AV  # noqa: E402
from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
import test_demo_breadth_filtered as F  # noqa: E402
from test_demo_breadth_visit import visit_trace  # noqa: E402

BASE = "https://shop.test"


def item(h1, title="", url=f"{BASE}/p/x", extra=""):
    dom = f"<html><head><title>{title or h1}</title></head><body><main><h1>{h1}</h1>{extra}</main></body></html>"
    return AV.Page(dom, f"{h1}\n{extra}", url)


def identity(keys, page, others=()):
    return AV.verify_identity(keys, page, others)


# ------------------------------------------------------------------------------------------------- the identity rule

def test_the_page_names_the_result_its_card_showed():
    assert identity(["Bosch Bohrhammer PBH 2100"], item("Bosch Bohrhammer PBH 2100")).status == AV.VERIFIED
    # a tile title with a tagline holds the page's name
    d = identity(["Bosch Bohrhammer PBH 2100 Profi-Set inkl. Koffer"], item("Bosch Bohrhammer PBH 2100"))
    assert d.status == AV.VERIFIED, d.why
    assert identity(["東京タワー 模型"], item("東京タワー 模型 1/500")).status == AV.VERIFIED
    assert identity(["東京タワー"], item("東京タワー模型")).status == AV.VERIFIED
    # a one-word title of the site names no item
    assert identity(["Bosch Bohrhammer PBH 2100"], item("Shop")).status == AV.UNCERTAIN


def test_a_page_naming_another_listed_result_conflicts():
    d = identity(["Bosch Akkuschrauber GSR 12"], item("Makita Akkuschrauber DF333"), ["Makita Akkuschrauber DF333"])
    assert d.status == AV.CONFLICT and d.final, d.why
    # the page names a wider listed result that holds this one's name
    d = identity(["Guppy"], item("Endlers Guppy"), ["Endlers Guppy"])
    assert d.status == AV.CONFLICT and d.final and "only a part" in d.why, d.why
    assert identity(["Guppy"], item("Endlers Guppy")).status == AV.VERIFIED
    # the set's card opens the plain product's page: the page's name is only a part of the card's name, and it is
    # another listed result's whole name
    d = identity(["Bosch Akkuschrauber GSR 12 Set"], item("Bosch Akkuschrauber GSR 12"),
                 ["Bosch Akkuschrauber GSR 12", "Makita Akkuschrauber DF333"])
    assert d.status == AV.CONFLICT and d.final and "another listed result's" in d.why, d.why
    # a tile title with a tagline over a page no other listed result is named like
    d = identity(["Bosch Bohrhammer PBH 2100 Profi-Set"], item("Bosch Bohrhammer PBH 2100"),
                 ["Bosch Bohrhammer", "Makita Bohrhammer HR2470"])
    assert d.status == AV.VERIFIED, d.why


def row(rank, title, url, **kw):
    return dict({"rank": rank, "title": title, "url": url, "status": "ok"}, **kw)


def model(choice=None, line=1, error=""):
    calls = []

    def ask(prompt, schema):
        calls.append(prompt)
        if error:
            return {"error": error, "calls": 1}
        opts = schema["properties"]["choice"]["enum"]
        return {"choice": opts[0] if choice is None else choice, "line": line, "calls": 1, "usd": 0.0}
    return ask, calls


def visit(rows, pages, resolver=None, list_page=None):
    links = {r["rank"]: r["url"] for r in rows}
    return AV.verify_visit({"items": rows}, list_page, pages, links, resolver)


def ident(v):
    return next(d for d in v.decisions if d.attr == "identity")


def test_a_reordered_name_is_settled_without_the_model_and_another_name_asks_it():
    # the card's words in another order: the strict rule (every word of the name) settles it, no model call
    rows = [row(1, "Akkuschrauber Bosch GSR 12", f"{BASE}/p/a")]
    pages = {1: item("Bosch Akkuschrauber GSR 12", url=f"{BASE}/p/a")}
    ask, calls = model()
    d = ident(visit(rows, pages, AV.Resolver(ask))[1])
    assert d.status == AV.VERIFIED and calls == [] and "holds the result's name" in d.why, d.why
    # the card names it in other words: the model picks the naming line
    rows = [row(1, "Cordless Drill Bosch GSR 12", f"{BASE}/p/a")]
    assert identity([rows[0]["title"]], pages[1]).status == AV.UNCERTAIN
    ask, calls = model()
    d = ident(visit(rows, pages, AV.Resolver(ask))[1])
    assert d.status == AV.VERIFIED and len(calls) == 1 and "the model reads" in d.why, d.why
    ask, _calls = model(AV.NONE_OF_THESE)
    d = ident(visit(rows, pages, AV.Resolver(ask))[1])
    assert d.status == AV.WITHHELD, d.why
    # no answer (an error, a line out of range, no model at all): not verified
    for resolver in (AV.Resolver(model(error="timeout")[0]), AV.Resolver(model(line=99)[0]), AV.Resolver(None),
                     None):
        d = ident(visit(rows, pages, resolver)[1])
        assert d.status == AV.CONFLICT and "holds the result's name" in d.why, (resolver, d.why)
    other = {1: item("Makita Bohrhammer HR2470", url=f"{BASE}/p/a")}
    d = ident(visit(rows, other, AV.Resolver(None))[1])
    assert d.status == AV.CONFLICT, d.why


def test_the_rows_share_one_model_cap_and_one_page_is_one_rows():
    rows = [row(k, f"Cordless Drill Bosch GSR {k}2", f"{BASE}/p/{k}") for k in (1, 2, 3)]
    pages = {k: item(f"Bosch Akkuschrauber GSR {k}2", url=f"{BASE}/p/{k}") for k in (1, 2, 3)}
    ask, calls = model()
    vs = visit(rows, pages, AV.Resolver(ask, max_calls=2))
    assert len(calls) == 2 and sum(v.model_calls for v in vs.values()) == 2
    assert vs[3].decisions[-1].model == {"skipped": "the per-answer cap of 0 model calls"}
    assert [ident(vs[k]).status for k in (1, 2, 3)] == [AV.VERIFIED, AV.VERIFIED, AV.CONFLICT]
    # two rows that opened one page: the row the page names keeps it
    rows = [row(1, "Bosch Bohrhammer PBH 2100", f"{BASE}/p/1"), row(2, "Makita Bohrhammer HR2470", f"{BASE}/p/2")]
    same = item("Makita Bohrhammer HR2470", url=f"{BASE}/p/2")
    vs = visit(rows, {1: same, 2: same}, AV.Resolver(None))
    assert ident(vs[2]).status == AV.VERIFIED and ident(vs[1]).status != AV.VERIFIED, ident(vs[1]).why
    # a row whose page was not kept is not verified
    vs = visit(rows[:1], {}, AV.Resolver(None))
    assert vs[1].verdict == AV.WITHHOLD and ident(vs[1]).final


def test_a_link_outside_the_tools_result_hosts_is_not_verified():
    rows = [row(1, "Bosch Bohrhammer PBH 2100", "https://elsewhere.test/p/1"),
            row(2, "Makita Bohrhammer HR2470", f"{BASE}/p/2")]
    pages = {1: item("Bosch Bohrhammer PBH 2100", url="https://elsewhere.test/p/1"),
             2: item("Makita Bohrhammer HR2470", url=f"{BASE}/p/2")}
    vs = AV.verify_visit({"items": rows}, None, pages, {r["rank"]: r["url"] for r in rows}, AV.Resolver(None),
                         scope=["shop.test"])
    scope = {k: next(d for d in v.decisions if d.attr == "scope") for k, v in vs.items()}
    assert scope[1].status != AV.VERIFIED and scope[1].final and "elsewhere.test" in scope[1].why, scope[1].why
    assert vs[1].verdict == AV.WITHHOLD and ident(vs[1]).status == AV.VERIFIED
    assert scope[2].status == AV.VERIFIED and ident(vs[2]).status == AV.VERIFIED
    # no declared host: no scope decision
    vs = AV.verify_visit({"items": rows}, None, pages, {r["rank"]: r["url"] for r in rows}, AV.Resolver(None))
    assert not any(d.attr == "scope" for v in vs.values() for d in v.decisions)


# ---------------------------------------------------------------------------------------------- through the tool

class TextPage(F.FakePage):
    """The fake shop's page with its visible text, one block per line (``innerText``)."""

    async def text(self):
        return self.read()["text"]


SET = ("bosch-akkuschrauber-gsr-12-set", "Bosch Akkuschrauber GSR 12 Set", 119.0, None, "2025-03-13", "Bosch", True)


class SwappedShop(F.Shop):
    """The card of "Bosch Akkuschrauber GSR 12" links to the page of the set listed beside it."""

    def card(self, c):
        out = super().card(c)
        if c[0] == "bosch-akkuschrauber-gsr-12":
            out = out.replace(f'href="/p/{c[0]}"', f'href="/p/{SET[0]}"')
        return out


class ElsewhereShop(F.Shop):
    """The card of the Makita result links to the page of the Einhell result listed below it."""

    def card(self, c):
        out = super().card(c)
        if c[0] == "makita-akkuschrauber-df333":
            out = out.replace(f'href="/p/{c[0]}"', 'href="/p/einhell-akkuschrauber-te-cd"')
        return out


def run_visit(shop, tool, value, k=None):
    return asyncio.run(T.run_tool(TextPage(shop), tool, value, F._settle, result_wait_s=0.2,
                                  params={"k": k} if k else None))


def test_the_visit_tool_verifies_each_row_on_its_own_page():
    tool = C.compile_trace(visit_trace(F.Shop()))["tool"]
    old_env, old_r = os.environ.get(AV.SETTING), AV._DEFAULT.get("r")
    F.CATALOG.insert(1, SET)
    try:
        os.environ[AV.SETTING] = "on"
        AV._DEFAULT["r"] = AV.Resolver(None)
        out = run_visit(F.Shop(), tool, "Akkuschrauber")
        assert out["status"] == "ok" and out["accepted"], (out["status"], out["why"])
        checks = out["checks"]["items"]
        assert [c["checks"]["attribute_verify"]["verdict"] for c in checks] == ["accept", "accept"], checks
        # the table's verdict and spend, where a caller reads a single answer's
        table = out["checks"]["attribute_verify"]
        assert table["verdict"] == "accept" and table["rows"] == {1: "accept", 2: "accept"}, table
        assert table["model_calls"] == 0 and table["usd"] == 0.0 and table["where"] == "visit"
        # the first card's link opens the set listed beside it: the tool's own check passes, the verifier withholds
        out = run_visit(SwappedShop(), tool, "Akkuschrauber")
        rows = (out.get("output") or {}).get("items") or []
        assert out["status"] == "incomplete" and not out["accepted"], (out["status"], out["why"])
        assert out["checks"]["attribute_verify"]["verdict"] == "withhold", out["checks"]["attribute_verify"]
        assert [r["status"] for r in rows] == ["withheld", "ok"], rows
        assert rows[0]["why"].startswith("attribute verify: identity"), rows[0]["why"]
        assert rows[0]["name"] is None and rows[0]["unverified"]["name"] == SET[1]
        os.environ[AV.SETTING] = "off"
        out = run_visit(SwappedShop(), tool, "Akkuschrauber")
        assert out["status"] == "ok" and all("attribute_verify" not in (c.get("checks") or {})
                                             for c in out["checks"]["items"])
    finally:
        F.CATALOG.remove(SET)
        if old_env is None:
            os.environ.pop(AV.SETTING, None)
        else:
            os.environ[AV.SETTING] = old_env
        if old_r is None:
            AV._DEFAULT.pop("r", None)
        else:
            AV._DEFAULT["r"] = old_r


def test_a_row_that_opens_a_listed_result_it_did_not_open_is_withheld():
    tool = C.compile_trace(visit_trace(F.Shop()))["tool"]
    old_env, old_r = os.environ.get(AV.SETTING), AV._DEFAULT.get("r")
    # the set is listed on the first page below the two results the tool opens
    F.CATALOG.insert(3, SET)
    try:
        os.environ[AV.SETTING] = "on"
        AV._DEFAULT["r"] = AV.Resolver(None)
        out = run_visit(SwappedShop(), tool, "Akkuschrauber")
        rows = (out.get("output") or {}).get("items") or []
        assert out["status"] == "incomplete" and [r["status"] for r in rows] == ["withheld", "ok"], rows
        assert "another listed result" in rows[0]["why"] and rows[0]["unverified"]["name"] == SET[1], rows[0]
    finally:
        F.CATALOG.remove(SET)
        if old_env is None:
            os.environ.pop(AV.SETTING, None)
        else:
            os.environ[AV.SETTING] = old_env
        if old_r is None:
            AV._DEFAULT.pop("r", None)
        else:
            AV._DEFAULT["r"] = old_r


def test_a_row_shares_its_link_with_a_failed_row_and_keeps_its_own_card():
    tool = C.compile_trace(visit_trace(F.Shop()))["tool"]
    old_env, old_r = os.environ.get(AV.SETTING), AV._DEFAULT.get("r")
    try:
        os.environ[AV.SETTING] = "on"
        AV._DEFAULT["r"] = AV.Resolver(None)
        # the Makita card links the Einhell page: the tool's own check fails that row, the Einhell row stays ok
        out = run_visit(ElsewhereShop(), tool, "Akkuschrauber", k=4)
        rows = (out.get("output") or {}).get("items") or []
        assert [r["status"] for r in rows] == ["ok", "failed", "ok", "ok"], [(r["status"], r["why"]) for r in rows]
    finally:
        if old_env is None:
            os.environ.pop(AV.SETTING, None)
        else:
            os.environ[AV.SETTING] = old_env
        if old_r is None:
            AV._DEFAULT.pop("r", None)
        else:
            AV._DEFAULT["r"] = old_r


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
