"""The web_arch4_live bench, offline: fakes only, no browser, no network,
no model.

What must hold:

1. the values come from a RESULT page: the result list is the link cluster naming the seed (an
   inflected form counts: "kurczak" for "kurczaka"), an item's title is its short link text, not its
   description; shops draw 3 distinct product titles (none a word-subset of another, prices and
   badges removed); other sites draw words shared by >= 2 result titles (no function word, not the
   seed); the empty terms are seeded nonsense;
2. the cohort: the first 16 shops and 14 others eligible in frame order, a short stratum filled by
   shops, the demonstrators 15 / 15 drawn by the seed, the parts A/B/C alternating;
3. the CLIs never get a key and run with the real HOME in an empty working directory.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))

import web_arch4_live as B  # noqa: E402


def page(links):
    return "<html><body><header><nav><a href='/konto'>Konto</a></nav></header><main>" + "".join(
        f"<div class='tile'><a href='{h}'><img src='x.jpg'></a><a href='{h}'>{t}</a><a href='{h}'>{d}</a></div>"
        for h, t, d in links) + "</main></body></html>"


def test_values_from_a_result_page():
    html = page([(f"/przepis/r{i}", t, "Opis bardzo dlugi tego przepisu z kurczaka i warzyw dla calej rodziny")
                 for i, t in enumerate(["Kurczak w galarecie", "Pierś z kurczaka w galarecie", "Kurczak pieczony z ryżem",
                                        "Pierś z kurczaka pieczona", "Kurczak curry z ryżem"])])
    rows, how = B.result_titles(html, "https://www.x.pl/przepisy?q=kurczaka", "kurczaka")
    assert [t for t, _u in rows][:2] == ["Kurczak w galarecie", "Pierś z kurczaka w galarecie"], (rows, how)
    vals = B.word_values([t for t, _u in rows], "www.x.pl", "kurczaka", k=5)
    assert set(vals) <= {"galarecie", "Pierś", "pieczony", "ryżem"} and "kurczak" not in [v.lower() for v in vals], vals
    assert "ktora" not in B.word_values(["Zupa ktora smakuje", "Kotlet ktora smakuje"], "www.x.pl", "zupa", k=5)
    shop = ["Acme Kettle 2000 Steel 49,99 € (12) NEU", "Acme Kettle 2000 Steel", "Acme Kettle 3000 Glass",
            "Borealis Toaster 4 Slice -20%", "Borealis Toaster", "Cumulus Blender X", "Mehr"]
    pv = B.product_values(shop, "www.shop.test", k=5)
    assert "Acme Kettle 2000 Steel" in pv and len(pv) == len(set(pv))
    for a in pv:
        for b in pv:
            if a != b:
                assert not set(B.W.content_tokens(a)) <= set(B.W.content_tokens(b)), (a, b)
    assert all("€" not in v and "NEU" not in v for v in pv)
    e1, e2 = B.empty_terms("www.shop.test")
    assert e1 != e2 and B.empty_terms("www.shop.test") == [e1, e2] and len(e1.split()) == 2


def test_the_cohort():
    sites = {}
    for i in range(40):
        sites[f"s{i}.shop"] = {"stratum": "shop", "screen": "eligible", "index": i, "values": {"demo": "a"}}
    for i in range(10):
        sites[f"o{i}.other"] = {"stratum": "other", "screen": "eligible", "index": 100 + i, "values": {"demo": "a"}}
    sites["x.other"] = {"stratum": "other", "screen": "no_search", "index": 5}
    c = B.cohort_from({"sites": sites})
    assert len(c["main"]["shop"]) == 20 and len(c["main"]["other"]) == 10 and len(c["order"]) == 30
    assert c["filled_other_slots_with_shops"] == ["s16.shop", "s17.shop", "s18.shop", "s19.shop"]
    assert sorted(c["demonstrator"].values()).count("claude") == 15
    assert set(c["part"].values()) == {"A", "B", "C"} and c["reserves"]["shop"][0] == "s20.shop"
    assert B.cohort_from({"sites": sites}) == c


def test_the_clis_get_no_key_and_the_real_home():
    os.environ["CEREBRAS_API_KEY"] = "x"
    os.environ["ANYUSE_MODEL_ENV_FILE"] = "/somewhere"
    env = B.clean_env()
    assert "CEREBRAS_API_KEY" not in env and not any(k.startswith("ANYUSE_") for k in env)
    assert env["HOME"] == B.REAL_HOME
    del os.environ["CEREBRAS_API_KEY"], os.environ["ANYUSE_MODEL_ENV_FILE"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
