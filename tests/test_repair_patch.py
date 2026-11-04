"""The repair ladder's CLOSED patch language (``execution/repair_patch.py``), offline:
no page, no network, no model.

What must hold:

1. each of the five operations applies to a COPY of a version-10 tool (the original unchanged), and the patched tool's
   protected view (checks, fields, workflow, version, hosts, start URL, region pattern, counts and caps) and origin
   policy are byte-identical to the original's;
2. ADVERSARIAL: model answers that try to change the checks, the guard or the traffic budgets, the oracle, the origin
   policy (another host), or to do anything that is not read-only (a POST, a form, a login, a cart, another host) are
   ALL rejected - by the schema, by the value rules or by the boundary check on the result;
3. an operation on a step the tool does not have, a malformed or over-long answer, and a tool below version 10 are
   rejected;
4. the third layer alone (``check_boundary``) catches a patched tool that changed a protected key, even if an operation
   had a bug.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import repair_patch as P  # noqa: E402

SITE = "www.shop.test"
CARD = {"kind": "card", "chain": ["div.listing", "div.grid", "ul.products", "li.product"],
        "rels": ["a.title\u0001h3.name\u0001div.info"], "roles": [{"anchor": "a.title", "heading": True}]}


def tool(workflow="paged_search", mode="url"):
    t = {"version": 10, "workflow": workflow, "site": SITE, "start_url": f"https://{SITE}/",
         "input": {"name": "term"},
         "search": {"mode": mode, "template": f"https://{SITE}/search?q={{q}}&cat=all",
                    "fallback_templates": [f"https://{SITE}/search?q={{q}}&cat=all"], "start_url": f"https://{SITE}/"},
         "region": {"kind": "cluster", "regex": r"^https?://www\.shop\.test/p/[^/?#]+$", "cards": [copy.deepcopy(CARD)],
                    "cards_version": 10},
         "checks": ["search_page", "membership", "query", "empty_state"], "hosts": [SITE, "img.shop.test"],
         "model_calls": 0, "compiled_from": {"steps": 3, "trace_sha256": "ab" * 32}}
    if mode == "typed":
        t["search"] = {"mode": "typed", "input": {"index": 0, "name": "q"}, "start_url": f"https://{SITE}/",
                       "fallback_templates": [f"https://{SITE}/search?q={{q}}"], "overlay": False}
    if workflow == "paged_search":
        t.update(pager={"mode": "param", "key": "p", "first": 1, "stride": 1, "link": {"text": "", "rel": ""}}, n=40,
                 params=[{"name": "n", "kind": "count", "default": 40}])
    if workflow == "filtered_search":
        t.update(refine={"mode": "url", "params": {"o": "3"}},
                 params=[{"name": "o", "kind": "sort", "default": "3", "key": "price_asc"}],
                 applied=[{"kind": "sort", "param": "o", "value": "3", "key": "price_asc", "label": "Preis aufsteigend"}])
    return t


def controls_tool():
    t = tool("filtered_search")
    t["refine"] = {"mode": "controls", "controls": [{"tag": "select", "text": "Sortieren", "option_text": "Preis aufsteigend"}]}
    return t


def rejected(t, answer):
    try:
        P.apply(t, answer)
    except P.PatchError as exc:
        return str(exc)
    raise AssertionError(f"accepted: {json.dumps(answer)[:200]}")


def test_each_operation_applies_to_a_copy_inside_the_boundary():
    t = tool()
    before = json.dumps(t, sort_keys=True)
    card2 = {"kind": "card", "chain": ["main", "section.results", "div.grid", "article.tile"],
             "rels": ["a.tile-link\u0001h2"], "roles": [{"anchor": "a.tile-link", "heading": True}]}
    got = P.apply(t, {"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [card2]}], "why": "classes renamed"})
    assert got["region"]["cards"] == [card2] and json.dumps(t, sort_keys=True) == before
    assert P.protected_digest(got) == P.protected_digest(t)
    got = P.apply(t, {"ops": [{"op": "rename_url_param", "scope": "search", "from": "q", "to": "query"}]})
    assert got["search"]["template"] == f"https://{SITE}/search?query={{q}}&cat=all" or \
        got["search"]["template"] == f"https://{SITE}/search?cat=all&query={{q}}", got["search"]["template"]
    assert C.fill(got["search"]["template"], "rote Schuhe").count("query=rote+Schuhe") == 1
    got = P.apply(t, {"ops": [{"op": "rebind_param", "param": "q", "to": {"in": "query", "key": "s", "path": "/suche"}}]})
    assert got["search"]["template"].startswith(f"https://{SITE}/suche?") and "s={q}" in got["search"]["template"]
    got = P.apply(t, {"ops": [{"op": "rebind_param", "param": "q", "to": {"in": "path", "segment": 1}}]})
    assert got["search"]["template"] == f"https://{SITE}/search/{{q}}?cat=all", got["search"]["template"]
    got = P.apply(t, {"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": "page"}]})
    assert got["pager"]["key"] == "page" and got["n"] == 40
    got = P.apply(t, {"ops": [{"op": "replace_anchor", "anchor": "pager",
                               "value": {"mode": "path_append", "suffix": "page/{page}"}}]})
    assert got["pager"] == {"mode": "path_append", "suffix": "page/{page}"}
    got = P.apply(t, {"ops": [{"op": "insert_nav_step", "at": "after_search",
                               "link": {"text": "Alle Ergebnisse anzeigen", "path": "/search/all"}}]})
    assert got["repair_nav"] == [{"at": "after_search", "link": {"text": "Alle Ergebnisse anzeigen", "path": "/search/all"}}]
    back = P.apply(got, {"ops": [{"op": "skip_nav_step", "index": 0}]})
    assert "repair_nav" not in back
    f = tool("filtered_search")
    got = P.apply(f, {"ops": [{"op": "rename_url_param", "scope": "refine", "from": "o", "to": "order"}]})
    assert got["refine"]["params"] == {"order": "3"} and got["params"][0]["name"] == "order" and \
        got["applied"][0]["param"] == "order" and got["applied"][0]["key"] == "price_asc"
    c = controls_tool()
    got = P.apply(c, {"ops": [{"op": "replace_anchor", "anchor": "refine_control", "index": 0,
                               "value": {"option_text": "Günstigste zuerst"}}]})
    assert got["refine"]["controls"][0] == {"tag": "select", "text": "Sortieren", "option_text": "Günstigste zuerst"}
    ty = tool("site_search", mode="typed")
    got = P.apply(ty, {"ops": [{"op": "replace_anchor", "anchor": "search_input", "value": {"name": "search_term"}}]})
    assert got["search"]["input"] == {"name": "search_term"}
    # the model's JSON text is accepted as well
    got = P.apply(t, json.dumps({"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": "seite"}]}))
    assert got["pager"]["key"] == "seite"


ADVERSARIAL = [
    # the checks
    ("checks: an anchor named checks", {"ops": [{"op": "replace_anchor", "anchor": "checks", "value": []}]}),
    ("checks: a free 'set' operation", {"ops": [{"op": "set", "path": "checks", "value": []}]}),
    ("checks: an extra key on a valid op", {"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": "page",
                                                      "checks": []}]}),
    ("checks: a top-level key", {"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": "page"}],
                                 "checks": []}),
    ("checks: the fields' rules", {"ops": [{"op": "replace_anchor", "anchor": "fields", "value": {"name": "text"}}]}),
    ("checks: the workflow", {"ops": [{"op": "replace_anchor", "anchor": "cards", "value": [CARD],
                                       "workflow": "site_search"}]}),
    ("checks: the version", {"ops": [{"op": "replace_anchor", "anchor": "version", "value": 99}]}),
    # the guard and the traffic budgets
    ("budget: the page cap", {"ops": [{"op": "replace_anchor", "anchor": "max_pages", "value": 500}]}),
    ("budget: n through the pager", {"ops": [{"op": "replace_anchor", "anchor": "pager",
                                              "value": {"mode": "param", "key": "p", "first": 1, "stride": 1,
                                                        "n": 10000}}]}),
    ("budget: a guard scope", {"ops": [{"op": "rename_url_param", "scope": "guard", "from": "rate", "to": "x"}]}),
    ("budget: a stride beyond the cap", {"ops": [{"op": "replace_anchor", "anchor": "pager",
                                                  "value": {"mode": "param", "key": "p", "first": 1, "stride": 100000}}]}),
    ("budget: too many operations", {"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": f"p{k}"}
                                             for k in range(4)]}),
    # the oracle
    ("oracle: an anchor", {"ops": [{"op": "replace_anchor", "anchor": "oracle", "value": {"verdict": "agrees"}}]}),
    ("oracle: a verdict key", {"ops": [{"op": "skip_nav_step", "index": 0}], "oracle": "agrees"}),
    # the origin policy / another host
    ("origin: the hosts", {"ops": [{"op": "replace_anchor", "anchor": "hosts", "value": ["evil.test"]}]}),
    ("origin: a rebind to a full URL", {"ops": [{"op": "rebind_param", "param": "q",
                                                 "to": {"in": "query", "key": "q", "path": "https://evil.test/s"}}]}),
    ("origin: a protocol-relative path", {"ops": [{"op": "rebind_param", "param": "q",
                                                   "to": {"in": "query", "key": "q", "path": "//evil.test/s"}}]}),
    ("origin: a host key", {"ops": [{"op": "rebind_param", "param": "q", "to": {"in": "query", "key": "q",
                                                                                "host": "evil.test"}}]}),
    ("origin: a nav step to another host", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                                     "link": {"text": "Mehr", "path": "//evil.test/x"}}]}),
    ("origin: a redirect through a query", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                                     "link": {"text": "Mehr", "path": "/go?url=https://evil.test"}}]}),
    ("origin: a user-info host", {"ops": [{"op": "rebind_param", "param": "q",
                                           "to": {"in": "query", "key": "q", "path": "/@evil.test/s"}}]}),
    ("origin: a URL in a link text", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                               "link": {"text": "https://evil.test", "path": "/all"}}]}),
    ("origin: the start URL", {"ops": [{"op": "replace_anchor", "anchor": "start_url", "value": "https://evil.test/"}]}),
    ("origin: the region pattern", {"ops": [{"op": "replace_anchor", "anchor": "regex", "value": ".*"}]}),
    # not read-only: POST, forms, login, carts, scripts
    ("POST: a method key", {"ops": [{"op": "insert_nav_step", "at": "after_search", "method": "POST",
                                     "link": {"text": "Alle", "path": "/all"}}]}),
    ("POST: a body", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                               "link": {"text": "Alle", "path": "/all", "body": "a=1"}}]}),
    ("form: a submit operation", {"ops": [{"op": "submit_form", "form": 0, "fields": {"q": "x"}}]}),
    ("form: a click operation", {"ops": [{"op": "click", "element": 12}]}),
    ("login: a nav step", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                    "link": {"text": "Anmelden", "path": "/login"}}]}),
    ("login: a search bound on an account path", {"ops": [{"op": "rebind_param", "param": "q",
                                                           "to": {"in": "query", "key": "q", "path": "/account/search"}}]}),
    ("cart: a nav step", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                   "link": {"text": "In den Warenkorb", "path": "/cart/add"}}]}),
    ("cart: a refinement control", None),       # filled below: a controls tool
    ("checkout: a pager path", {"ops": [{"op": "replace_anchor", "anchor": "pager",
                                         "value": {"mode": "path_append", "suffix": "checkout/{page}"}}]}),
    ("API: a nav step", {"ops": [{"op": "insert_nav_step", "at": "after_search", "link": {"text": "x", "path": "/api/v1/q"}}]}),
    ("script: a descriptor", {"ops": [{"op": "replace_anchor", "anchor": "cards",
                                       "value": [dict(CARD, chain=["div", "div", "div", "a[onclick=alert()]"])]}]}),
    ("script: markup in a label", None),        # filled below: a controls tool
    ("script: a javascript path", {"ops": [{"op": "insert_nav_step", "at": "after_search",
                                            "link": {"text": "x", "path": "/javascript:alert()"}}]}),
    ("traversal: a path", {"ops": [{"op": "insert_nav_step", "at": "after_search", "link": {"text": "x", "path": "/a/../login"}}]}),
    # a second inserted step
    ("two inserted steps", {"ops": [{"op": "insert_nav_step", "at": "after_search", "link": {"text": "a", "path": "/a"}},
                                    {"op": "insert_nav_step", "at": "after_search", "link": {"text": "b", "path": "/b"}}]}),
]


def test_adversarial_answers_are_all_rejected():
    t = tool()
    c = controls_tool()
    reasons = {}
    for name, answer in ADVERSARIAL:
        if name == "cart: a refinement control":
            reasons[name] = rejected(c, {"ops": [{"op": "replace_anchor", "anchor": "refine_control", "index": 0,
                                                  "value": {"text": "In den Warenkorb"}}]})
            continue
        if name == "script: markup in a label":
            reasons[name] = rejected(c, {"ops": [{"op": "replace_anchor", "anchor": "refine_control", "index": 0,
                                                  "value": {"option_text": "<img src=x onerror=alert()>"}}]})
            continue
        reasons[name] = rejected(t, answer)
    assert len(reasons) == len(ADVERSARIAL) and all(reasons.values())
    # not JSON, not an object, no operations
    for bad in ("Sure! here is the patch: {", "[]", json.dumps({"ops": []}), json.dumps({"ops": {"op": "x"}}),
                json.dumps({"ops": [{"op": "skip_nav_step", "index": 0}], "why": "x" * 5000})):
        rejected(t, bad)


def test_operations_on_missing_steps_and_old_versions_are_rejected():
    t = tool("site_search")
    rejected(t, {"ops": [{"op": "replace_anchor", "anchor": "pager", "value": {"mode": "link", "link": {"text": "Weiter",
                                                                                                       "rel": "next"}}}]})
    rejected(t, {"ops": [{"op": "rename_url_param", "scope": "refine", "from": "o", "to": "order"}]})
    rejected(t, {"ops": [{"op": "replace_anchor", "anchor": "search_input", "value": {"name": "s"}}]})
    rejected(t, {"ops": [{"op": "skip_nav_step", "index": 0}]})
    rejected(t, {"ops": [{"op": "rename_url_param", "scope": "search", "from": "nope", "to": "q2"}]})
    rejected(t, {"ops": [{"op": "rename_url_param", "scope": "search", "from": "q", "to": "cat"}]})
    old = dict(tool(), version=9)
    rejected(old, {"ops": [{"op": "rename_url_param", "scope": "pager", "from": "p", "to": "page"}]})


def test_the_boundary_check_alone_catches_a_protected_change():
    t = tool()
    for mutate in (lambda x: x["checks"].pop(),
                   lambda x: x.__setitem__("hosts", [SITE, "shop.test", "evil.test"]),
                   lambda x: x.__setitem__("n", 100000),
                   lambda x: x["region"].__setitem__("regex", ".*"),
                   lambda x: x["search"].__setitem__("template", "https://evil.test/search?q={q}"),
                   lambda x: x["search"].__setitem__("template", f"https://{SITE}/login?q={{q}}"),
                   lambda x: x.__setitem__("repair_nav", [{"at": "after_search", "link": {"text": "x", "path": "/cart"}}]),
                   lambda x: x["params"][0].__setitem__("default", 10000)):
        bad = copy.deepcopy(t)
        mutate(bad)
        try:
            P.check_boundary(t, bad)
        except P.PatchError:
            continue
        raise AssertionError("a protected change passed the boundary check")


if __name__ == "__main__":
    test_each_operation_applies_to_a_copy_inside_the_boundary()
    test_adversarial_answers_are_all_rejected()
    test_operations_on_missing_steps_and_old_versions_are_rejected()
    test_the_boundary_check_alone_catches_a_protected_change()
    print("test_repair_patch: 4 ok")
