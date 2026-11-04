"""An item page's values named from the recording itself (``execution/field_names.py``).

A fields-shaped proof case compares each value under its field, so a recording that only lists the values the page
showed (a HAR's ``--har-expect``, a typed ``--expect``) is named from its own evidence: the JSON key holding each value
in its response, else the label its page puts beside it, else the configured small model's pick checked against the
page, else (a typed case) its order. Offline: an injected transport, a faked model.
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import field_names as FN  # noqa: E402
from anyuse.execution import html_extract as H  # noqa: E402
from anyuse.execution import model_check as MC  # noqa: E402
from anyuse.execution.api_compile import compile_recording, normalise  # noqa: E402
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor  # noqa: E402
from anyuse.execution.api_verify import compile_and_verify  # noqa: E402

ORIGIN = "https://registry.test"
PKGS = {
    "serde": {"title": "Serde Package", "summary": "a serialization framework", "downloads": 1307},
    "tokio": {"title": "Tokio Runtime", "summary": "an asynchronous runtime", "downloads": 2211},
    "rand": {"title": "Rand Crate", "summary": "random number generators", "downloads": 3407},
    "regex": {"title": "Regex Engine", "summary": "regular expressions for text", "downloads": 4519},
}


def labels(markup, value, implied=False):
    return [(k, s) for k, s, _t in FN.page_labels(FN.parse_page(markup, implied), normalise(value))]


# ---------------------------------------------------------------------------------------- the labels beside a value

def test_the_label_a_page_puts_beside_a_value():
    assert ("downloads", "dl") in labels("<dl><dt>Downloads</dt><dd>1307</dd></dl>", "1307")
    assert ("license", "table") in labels("<table><tr><th>License</th><td>MIT</td></tr></table>", "MIT")
    col = ("<table><thead><tr><th>Version</th><th>Downloads</th></tr></thead>"
           "<tbody><tr><td>1.2.3</td><td>1307</td></tr></tbody></table>")
    assert labels(col, "1307") == [("downloads", "table")]
    assert labels(col, "1.2.3") == [("version", "table")]
    assert ("version", "colon") in labels("<p>Version: 1.2.3</p>", "1.2.3")
    # a "Label: value" line whose value is most of its text is still read with its label
    assert labels("<p>Name: Serde Package</p>", "Serde Package") == [("name", "colon")]
    wax = ('<div class="rcb-box-text-item"><span class="label">Preis:</span>'
           '<span class="value">3,99 €</span></div>')
    assert ("preis", "colon") in labels(wax, "3,99 €")
    ld = ('<script type="application/ld+json">{"@type": "SoftwareApplication", '
          '"softwareVersion": "1.2.3"}</script><p>1.2.3</p>')
    assert ("jsonld:softwareVersion", "jsonld") in labels(ld, "1.2.3")
    assert labels('<span itemprop="version">1.2.3</span>', "1.2.3") == [("itemprop:version", "itemprop")]
    assert labels('<meta itemprop="version" content="1.2.3"><p>x</p>', "1.2.3") == [
        ("itemprop:version", "itemprop")]
    assert labels('<span aria-label="Downloads">1307</span>', "1307") == [("aria:downloads", "aria")]
    assert labels("<dl><dt>ダウンロード数</dt><dd>1307</dd></dl>", "1307") == [("ダウンロード数", "dl")]
    # a section's aria-label or an item of its own names no single value
    assert labels('<section aria-label="Details"><p>1307</p><p>MIT</p></section>', "1307") == []
    assert labels('<div itemprop="offers" itemscope><span>3,99 €</span></div>', "3,99 €") == []
    assert labels("<p>1307</p>", "1307") == []


def test_a_node_keeps_only_the_attributes_that_name_what_it_holds():
    markup = ('<ul><li class="r" id="x" data-k="1" style="color:red"><a href="/a" itemprop="url" '
              'onclick="go()" aria-label="Alpha">Alpha</a></li><li class="r"><a href="/b">Beta</a></li></ul>')
    a = next(n for n in H.parse(markup).walk() if n.tag == "a")
    assert a.attrs == {"href": "/a", "itemprop": "url", "aria-label": "Alpha"}
    li = a.parent
    assert li.attrs == {} and li.classes == ("r",) and li.node_id == "x"
    assert H.extract(markup, "li.r a") == ["Alpha", "Beta"]


# ------------------------------------------------------------------------------------------- a fake package registry

def detail(name):
    return {"item": {"id": f"p{len(name)}{ord(name[0])}", **PKGS[name]}}


def page_html(name, layout="dl", swap=False):
    p = PKGS[name]
    rows = [("Name", p["title"]), ("Summary", p["summary"]), ("Downloads", str(p["downloads"]))]
    if swap:
        rows = [("Name", p["title"]), ("Summary", str(p["downloads"])), ("Downloads", p["summary"])]
    if layout == "dl":
        body = "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"
    else:                                    # plain sibling blocks: no dt, th or colon
        body = "".join(f"<div class=k>{k}</div><div class=v>{v}</div>" for k, v in rows)
    return f"<html><head><title>{p['title']}</title></head><body><main>{body}</main></body></html>"


def recording(name, json_traffic=True, page=True, dom=None, fields=None, shown=None):
    p = PKGS[name]
    network = []
    if json_traffic:
        network.append({"url": f"{ORIGIN}/api/package?q={name}", "method": "GET", "status": 200,
                        "mime": "application/json", "bytes": 200, "body": json.dumps(detail(name))})
    if page and dom is None:
        network.append({"url": f"{ORIGIN}/package?q={name}", "method": "GET", "status": 200,
                        "mime": "text/html", "bytes": 900, "body": page_html(name)})
    items = shown if shown is not None else [p["title"], p["summary"], str(p["downloads"])]
    rec = {"param": {"name": "q", "value": name}, "ui": {"items": items, "text": "\n".join(items)},
           "network": network, "final_url": f"{ORIGIN}/package?q={name}"}
    if dom is not None:
        rec["dom"] = dom
    if fields is not None:
        rec["ui"]["fields"] = fields
    return rec


class Registry:
    def __init__(self):
        self.sent = []

    def send(self, method, url, headers, body, timeout_s):
        self.sent.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("q", [""])[0]
        if q not in PKGS:
            return ApiResponse(404, {"Content-Type": "application/json"}, "{}")
        return ApiResponse(200, {"Content-Type": "application/json"}, json.dumps(detail(q)))


def template():
    compiled = compile_recording(recording("serde"))
    assert compiled.template is not None and compiled.template.shape == "fields", compiled.reason
    return compiled.template


def test_the_compile_recording_anchors_each_field():
    t = template()
    anchors = FN.field_anchors(t, recording("serde"))
    by_path = {a.path: a for a in anchors.values()}
    assert set(by_path) == {"item.title", "item.summary", "item.downloads"}, by_path
    assert [by_path[p].position for p in ("item.title", "item.summary", "item.downloads")] == [0, 1, 2]
    assert by_path["item.downloads"].labels == ["downloads"] and by_path["item.title"].labels == ["name"]
    assert by_path["item.summary"].label_texts == ["Summary"]


def names_of(t, case, compile_rec=None, model_scope=""):
    anchors = FN.field_anchors(t, compile_rec or recording("serde"))
    named, how, why = FN.name_case(anchors, t, case, model_scope=model_scope)
    paths = {a.name: a.path for a in anchors.values()}
    return ({paths[k]: v for k, v in (named or {}).items()}, {paths[k]: v for k, v in how.items()}, why,
            named)


def test_a_case_is_named_by_its_response_its_page_or_its_order():
    t = template()
    # its own response holds each value at the field's path
    got, how, _why, _n = names_of(t, recording("tokio", page=False))
    assert got == {"item.title": "Tokio Runtime", "item.summary": "an asynchronous runtime",
                   "item.downloads": "2211"} and set(how.values()) == {FN.BY_JSON_KEY}
    # only its page (a DOM snapshot): the labels the compile page put beside the same fields
    got, how, _why, _n = names_of(t, recording("rand", json_traffic=False, dom=page_html("rand")))
    assert got["item.downloads"] == "3407" and set(how.values()) == {"page:dl"}, how
    # typed: no traffic and no page, the compile recording's order
    got, how, _why, _n = names_of(t, recording("regex", json_traffic=False, page=False))
    assert got["item.summary"] == "regular expressions for text" and set(how.values()) == {FN.BY_PAGE_ORDER}
    # typed with another number of values: not named, and the reason says what to do
    _g, _h, why, named = names_of(t, recording("regex", json_traffic=False, page=False,
                                               shown=["Regex Engine", "regular expressions for text"]))
    assert named is None and "order the page shows them" in why, why
    # a recorded response that does not hold what the case says: never named by order instead
    bad = recording("tokio", page=False, shown=["Tokio Runtime", "an asynchronous runtime", "9999"])
    _g, _h, why, named = names_of(t, bad)
    assert named is None and "item.downloads" in why, why


def test_the_model_names_a_value_only_on_a_line_with_its_label():
    t = template()
    plain = recording("rand", json_traffic=False, dom=page_html("rand", layout="plain"))
    # plain sibling blocks give no label: without a model the order decides (no response to read it from)
    _g, _h, _why, named = names_of(t, plain)
    assert named is not None and set(_h.values()) == {FN.BY_PAGE_ORDER}

    def asker(target_line_holds):
        def ask(instructions, state, obs):
            line = state.rsplit(">> ", 1)[-1]
            return 0.95 if target_line_holds(instructions, line) else 0.05
        return ask

    # the fake says yes to the value line under the asked label: named by the model
    labels_of = {"Name": "Rand Crate", "Summary": "random number generators", "Downloads": "3407"}
    good = asker(lambda i, line: any(f"'{k}'" in i and v in line for k, v in labels_of.items()))
    with MC.using(good):
        got, how, _why, _n = names_of(t, plain, model_scope="compile")
    assert got["item.downloads"] == "3407" and how["item.downloads"] == FN.BY_MODEL, how
    # a yes for a line whose label is another field's: the pick fails verification, the next rule decides
    wrong = asker(lambda i, line: "'Downloads'" in i and "random number" in line)
    with MC.using(wrong):
        _g, how, _why, _n = names_of(t, plain, model_scope="compile")
    assert how.get("item.downloads") != FN.BY_MODEL, how
    # a yes for the unlabelled line below another field's value: the label above it is that field's
    unlabelled = recording("rand", json_traffic=False, dom=(
        "<html><body><main><div class=k>Name</div><div class=v>Rand Crate</div><p>random number generators</p>"
        "<div class=k>Downloads</div><div class=v>3407</div></main></body></html>"))
    below = asker(lambda i, line: "'Name'" in i and "random number" in line)
    with MC.using(below):
        _g, how, _why, _n = names_of(t, unlabelled, model_scope="compile")
    assert how.get("item.title") != FN.BY_MODEL, how
    # outside a named scope (a proof's replay, a heal) nothing is asked
    calls = []
    with MC.using(lambda i, s, o: calls.append(1) or 0.95):
        names_of(t, plain)
    assert calls == []


# -------------------------------------------------------------------------------------------------- end to end

def test_a_detail_page_without_named_fields_is_proved():
    compiled, proof = compile_and_verify(recording("serde"), [recording("tokio"), recording("rand")],
                                         executor=ApiStepExecutor(transport=Registry()))
    assert proof.passed, proof.reason
    # the new template's keys are the labels its page put beside the values
    assert sorted(compiled.template.extract) == ["downloads", "name", "summary"], compiled.template.extract
    assert all(c.contract["fields_named_by"] == {"downloads": "json_key", "name": "json_key",
                                                 "summary": "json_key"} for c in proof.cases)
    # a page that names the values but carries no JSON: named by its labels, still proved
    dom_only = [recording(n, json_traffic=False, dom=page_html(n)) for n in ("tokio", "rand")]
    _c, proof = compile_and_verify(recording("serde"), dom_only, executor=ApiStepExecutor(transport=Registry()))
    assert proof.passed and proof.cases[0].contract["fields_named_by"]["downloads"] == "page:dl", proof.reason
    # a page that puts the summary under "Downloads": the swapped meaning is still refused
    swapped = [recording("tokio", json_traffic=False, dom=page_html("tokio", swap=True)),
               recording("rand", json_traffic=False, dom=page_html("rand"))]
    _c, proof = compile_and_verify(recording("serde"), swapped, executor=ApiStepExecutor(transport=Registry()))
    assert not proof.passed and "field meaning mismatch" in proof.reason, proof.reason
    # a recording that names its fields keeps them: positional keys, its own names compared
    named = {"field_1": "Serde Package", "field_2": "a serialization framework", "field_3": "1307"}
    compiled, proof = compile_and_verify(
        recording("serde", fields=named),
        [recording(n, fields={"field_1": PKGS[n]["title"], "field_2": PKGS[n]["summary"],
                              "field_3": str(PKGS[n]["downloads"])}) for n in ("tokio", "rand")],
        executor=ApiStepExecutor(transport=Registry()))
    assert proof.passed and sorted(compiled.template.extract) == ["field_1", "field_2", "field_3"], proof.reason
    assert all("fields_named_by" not in c.contract for c in proof.cases)


def test_fresh_recordings_that_name_their_fields_keep_the_template_keys_they_use():
    fresh = [recording(n, fields={"field_1": PKGS[n]["title"], "field_2": PKGS[n]["summary"],
                                  "field_3": str(PKGS[n]["downloads"])}) for n in ("tokio", "rand")]
    compiled, proof = compile_and_verify(recording("serde"), fresh, executor=ApiStepExecutor(transport=Registry()))
    assert proof.passed and sorted(compiled.template.extract) == ["field_1", "field_2", "field_3"], proof.reason


def test_a_recompile_keeps_the_keys_a_stored_template_was_published_with():
    from anyuse.execution.api_recompile import _labelled_keys
    from anyuse.execution.api_step import PlanStep, RequestPlan, RequestTemplate
    single = template()
    single.extract = {f"field_{i + 1}": p for i, p in enumerate(single.extract.values())}
    assert not _labelled_keys({"template": single.to_dict()})
    single.extract = {"name": "item.title", "summary": "item.summary"}
    assert _labelled_keys({"template": single.to_dict()})
    search = RequestTemplate(method="GET", url=f"{ORIGIN}/api/search?q={{q}}", params=["q"],
                             extract={"items": "rows[*].id"}, shape="list")
    opened = RequestTemplate(method="GET", url=f"{ORIGIN}/api/item/{{id}}", params=["id"], shape="fields",
                             extract={"field_1": "item.title", "field_2": "item.summary"})
    chain = RequestPlan(steps=[PlanStep(name="search", template=search, bind={"id": "rows[0].id"}),
                               PlanStep(name="item", template=opened)], params=["q"], shape="fields")
    assert not _labelled_keys({"template": chain.to_dict()})
    # what the recompile passes: the keys stay positional
    compiled, proof = compile_and_verify(recording("serde"), [recording("tokio"), recording("rand")],
                                         executor=ApiStepExecutor(transport=Registry()), label_keys=False)
    assert proof.passed and sorted(compiled.template.extract) == ["field_1", "field_2", "field_3"], proof.reason


def test_two_documents_holding_as_many_values_name_them_by_the_later_one():
    # a chain's search page lists the item with its values, then the item page it lands on holds the same values: the
    # item page, the later document, is the page the values are named on
    listing = ("<html><body><ul><li><a href='/package?q=serde'>Serde Package</a> 1307 downloads</li>"
               "<li><a href='/package?q=serde_json'>Serde Json</a> 88 downloads</li></ul></body></html>")
    item = page_html("serde")
    rec = recording("serde", json_traffic=False, page=False)
    rec["network"] = [{"url": f"{ORIGIN}/search?q=serde", "method": "GET", "status": 200, "mime": "text/html",
                       "bytes": len(listing), "body": listing},
                      {"url": f"{ORIGIN}/package?q=serde", "method": "GET", "status": 200, "mime": "text/html",
                       "bytes": len(item), "body": item}]
    markup, implied = FN.page_of(rec, ["Serde Package", "1307"])
    assert markup == item and implied
    # a document holding more of the values still wins, wherever it stands
    rec["network"].reverse()
    rec["network"][1]["body"] = listing.replace("1307 downloads", "")
    assert FN.page_of(rec, ["Serde Package", "1307"])[0] == item


def test_label_keys_are_unique_identifiers_else_positional():
    t = template()
    page = ("<dl><dt>Downloads</dt><dd>Serde Package</dd><dt>Downloads</dt><dd>1307</dd>"
            "<dt>1st summary line</dt><dd>a serialization framework</dd></dl>")
    renamed = FN.label_keys(t, recording("serde", page=False, dom=page))
    keys = {path: name for name, path in t.extract.items()}
    assert keys["item.title"] == "downloads" and keys["item.downloads"].startswith("field_"), keys
    assert keys["item.summary"].startswith("field_") and len(set(keys.values())) == 3, keys
    assert renamed and all(new in t.extract for new in renamed.values())


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
