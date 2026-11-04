"""`anyuse learn` judges the pages it opens for generated values by the site's own evidence, and its agent may
ask the configured small model while its proof asks none.

* a generated value's page is the site's own empty page when it shows what two made-up searches show (learned
  once through the confirmed URL), in any language: a Portuguese empty page the phrase list does not know is
  refused, even when a list on it changed;
* an empty-result phrase the confirmed page shows too (a cart line "0 items") is the site's chrome: a page with
  results and that line is kept; a phrase only the generated page shows still refuses it;
* the learn agent's own run is inside the ``agent`` model-check scope (a submit-control question is asked there),
  the proof's requests and the navigations are outside every scope (nothing is asked).

Offline: the fakes of ``test_learn_loop``; the model is a fake (``model_check.using``).
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_learn_loop import (FakeAgent, FakeNavigator, executor_for, nav_for, page,  # noqa: E402
                             run_for)

from anyuse.execution import learn_loop as L  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from anyuse.execution.agent_browser import AgentRun  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("ANYUSE_CDP_ENDPOINT", "ANYUSE_LEARN_ENDPOINT", "ANYUSE_MODEL_PROVIDER",
                 "ANYUSE_AGENT_MODEL_PROVIDER", "ANYUSE_API_HOSTS_FILE", "ANYUSE_EMPTY_PROBE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANYUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANYUSE_BLOCKED_HOSTS_FILE", str(tmp_path / "blocked.json"))
    monkeypatch.setenv("ANYUSE_ROBOTS_CACHE_FILE", str(tmp_path / "robots.json"))
    from anyuse.execution import api_permission as AP
    monkeypatch.setattr(AP, "robots_allows", lambda url, fetcher=None, on_hop=None:
                        (None, "no robots.txt (test)"))
    AP.acknowledge_host("ex.test")
    return tmp_path


# ---------------------------------------------------------------------------------------------------------------------
# a Portuguese shop: its empty page names the input and suggests searches built from it
# ---------------------------------------------------------------------------------------------------------------------
STATIC = ["Café", "Chá", "Leite"]


def shop(value, rows, related):
    items = "".join(f'<li class="produto"><a href="/p/{i}">{r}</a> <span>9,90 €</span></li>'
                    for i, r in enumerate(rows))
    rel = "".join(f'<li><a href="/busca?q={x}">{x}</a></li>' for x in related)
    body = (f'<ul class="lista">{items}</ul>' if rows else
            f'<p>Nenhum produto encontrado para «{value}».</p>')
    return (f"<html><head><title>Busca: {value} - loja.test</title></head><body><nav><a>Início</a>"
            f"<a>Ofertas</a></nav><h1>Resultados para «{value}»</h1>{body}"
            f'<ul class="relacionadas">{rel}</ul></body></html>')


def shop_run(value, rows, related=STATIC):
    dom = shop(value, rows, related)
    text = "\n".join(x.strip() for x in re.sub(r"<[^>]+>", "\n", dom).split("\n") if x.strip())
    url = f"https://loja.test/busca?q={value}"
    return AgentRun(value=value, task="t", start_url="https://loja.test/", final_url=url, dom=dom, text=text,
                    steps=[{"action": "navigate", "url": url}], model_calls=0)


def empty_run(value):
    return shop_run(value, [], [f"{value} preto", f"{value} branco", f"{value} azul"])


def confirmed(run):
    cand = L.Candidate(workflow="w", task="t", start_url="https://loja.test/", param="q", value=run.value,
                       final_url=run.final_url, url_template=run.final_url.replace(run.value, "{q}"))
    cand.recording = run.as_recording("w", "q", "agent0")
    cand.preview_selector, cand.preview = L.preview_items(run.dom)
    return cand


LANDS = staticmethod(lambda cand, run: (True, ""))


def test_the_sites_own_empty_page_refuses_a_generated_value_in_a_language_the_phrases_miss(home):
    cafe = shop_run("cafe", ["cafe torrado", "cafe moido", "cafe em grao"])
    cha = shop_run("cha", ["cha verde", "cha preto", "cha mate"])
    cand = confirmed(cafe)
    opened = []

    def navigate(v):
        opened.append(v)
        return {"cafe": cafe, "cha": cha}.get(v) or empty_run(v)
    gen = empty_run("zuvak")
    # what the phrase rules alone see: a list that changed (the suggestions), no phrase they know
    assert L._changed_list(cand, gen) and not L._EMPTY_RESULT.search(gen.text)
    site = L.SiteEmptyPage(cand, [cha], navigate)
    ok, why = L._generated_page_ok(cand, gen, LANDS, site)
    assert not ok and "site_empty_page" in why, why
    assert len(opened) == 2 and not {"cafe", "cha", "zuvak"} & set(opened)      # two made-up searches, once
    leite = shop_run("leite", ["leite integral", "leite desnatado", "leite em po"])
    assert L._generated_page_ok(cand, leite, LANDS, site) == (True, "")
    assert len(opened) == 2
    # ANYUSE_EMPTY_PROBE=off: no made-up search is sent; the other rungs decide
    home_env = os.environ.copy()
    try:
        os.environ["ANYUSE_EMPTY_PROBE"] = "off"
        quiet = L.SiteEmptyPage(cand, [cha], navigate)
        L._generated_page_ok(cand, gen, LANDS, quiet)
        assert len(opened) == 2
    finally:
        os.environ.clear()
        os.environ.update(home_env)


CART = '<header><a class="cart">Winkelwagen: 0 items</a></header>'


def _cart(value, rows, **kw):
    return nav_for(value, rows, dom=page(rows, value, extra=CART),
                   text=f"Winkelwagen: 0 items {value} " + " ".join(rows), **kw)


def test_a_phrase_the_confirmed_page_shows_too_is_chrome_not_an_empty_result(home):
    first = _cart("koffie", ["koffie bonen", "koffie filter", "koffie molen"])
    cand = confirmed(first)
    thee = _cart("thee", ["thee groen", "thee zwart", "thee munt"])
    assert L._EMPTY_RESULT.search(thee.text)                                       # the old rule refused it
    assert L._generated_page_ok(cand, thee, LANDS) == (True, "")
    # a statement only the generated page shows still refuses it, with a list or without one
    none = nav_for("melk", [], dom=page([], "melk", extra=CART),
                   text="Winkelwagen: 0 items No results found for melk")
    ok, why = L._generated_page_ok(cand, none, LANDS)
    assert not ok and "empty result" in why
    mixed = nav_for("suiker", ["suiker wit", "suiker bruin", "suiker riet"],
                    dom=page(["suiker wit", "suiker bruin", "suiker riet"], "suiker", extra=CART),
                    text="Winkelwagen: 0 items No results found for suikerr suiker wit suiker bruin suiker riet")
    ok, why = L._generated_page_ok(cand, mixed, LANDS)
    assert not ok and "no results found" in why


def test_generated_values_on_a_shop_with_a_cart_line_reach_the_proof(home):
    rows = {"koffie": ["koffie bonen", "koffie filter", "koffie molen"],
            "koffie bonen": ["koffie bonen arabica", "koffie bonen robusta", "koffie bonen mild"],
            "koffie filter": ["koffie filter papier", "koffie filter metaal", "koffie filter stof"],
            "thee": ["thee groen", "thee zwart", "thee munt"]}
    first = run_for("koffie", rows["koffie"], dom=page(rows["koffie"], "koffie", extra=CART),
                    text="Winkelwagen: 0 items koffie " + " ".join(rows["koffie"]))
    others = [_cart(v, rows[v]) for v in ("thee", "koffie bonen", "koffie filter")]

    class Nav(FakeNavigator):
        def __call__(self, url, value):
            self.urls.append(url)
            return self.by_value.get(value) or nav_for(value, [], dom=page([], value, extra=CART),
                                                       text=f"Winkelwagen: 0 items Geen resultaten voor {value}")
    nav = Nav(others)
    res = L.learn("search this site for {q}", "https://ex.test/", ["koffie", "thee"], workflow="ex_search",
                  param="q", agent=FakeAgent([first]), navigator=nav, executor=executor_for([first, *others]),
                  confirm=lambda cand: True, confirmed_by="test", proof_preflight=lambda _c: "", generate=True,
                  model=None)
    assert [g["value"] for g in res.generated] == ["koffie bonen", "koffie filter"], res.discarded
    assert res.outcome == "tier1", res.reason


# ---------------------------------------------------------------------------------------------------------------------
# the agent's own run may ask the small model; learn's proof asks none
# ---------------------------------------------------------------------------------------------------------------------
class Asked:
    def __init__(self):
        self.calls = 0

    def __call__(self, instructions, state, obs):
        self.calls += 1
        return 0.95 if "Search" in state.split(">> ", 1)[-1] else 0.05


def test_the_agents_run_is_in_the_agent_scope_and_the_proof_is_in_none(home):
    from anyuse.execution import cdp_guard as G
    from anyuse.execution.api_step import ApiStepExecutor
    picks, proof_scopes, agent_scopes, nav_scopes = [], [], [], []
    serde = run_for("serde", ["serde v1", "serde_json v1", "serde_derive v1"])
    regex = nav_for("regex", ["regex v1", "regex-lite v0", "regex-automata v0"])
    tokio = nav_for("tokio", ["tokio v1", "tokio-util v0", "tokio-stream v0"])

    class Agent(FakeAgent):
        def run(self, task, start_url, value):
            # several controls next to the search box: the agent's session asks which one submits
            agent_scopes.append(MC.current_scope())
            picks.append(G.pick_submit_control([{"label": "Categories"}, {"label": "Search"}],
                                               {"name": "q", "label": "Search the site"}))
            return super().run(task, start_url, value)

    class Nav(FakeNavigator):
        def __call__(self, url, value):
            nav_scopes.append(MC.current_scope())
            picks.append(G.pick_submit_control([{"label": "Categories"}, {"label": "Search"}], {"name": "q"}))
            return super().__call__(url, value)

    executor = executor_for([serde, regex, tokio])
    send = executor.transport.send

    def watched(*a, **kw):
        proof_scopes.append(MC.current_scope())
        picks.append(G.pick_submit_control([{"label": "Categories"}, {"label": "Search"}], {"name": "q"}))
        return send(*a, **kw)
    executor.transport.send = watched
    assert isinstance(executor, ApiStepExecutor)
    fake = Asked()
    with MC.using(fake):
        res = L.learn("search this site for {q}", "https://ex.test/", ["serde", "regex", "tokio"], workflow="wf",
                      param="q", agent=Agent([serde]), navigator=Nav([regex, tokio]), executor=executor,
                      confirm=lambda cand: True, confirmed_by="test", proof_preflight=lambda _c: "")
    assert res.outcome == "tier1", res.reason
    assert picks[0] == 1                                    # the agent's step: the model chose "Search"
    assert all(p is None for p in picks[1:])                # the navigations and the proof: nothing asked
    assert fake.calls == 2                                  # one question per control, in the agent's step only
    assert agent_scopes == ["agent"] and set(nav_scopes) == {""}
    assert proof_scopes and set(proof_scopes) == {""}
    assert MC.current_scope() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
