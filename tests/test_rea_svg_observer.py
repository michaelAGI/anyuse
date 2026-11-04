"""Complete independent SVG DOM observations; guarded guest tests only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_inspector_bridge as I
from anyuse.execution import rea_runtime_bridge as B
from anyuse.execution import rea_svg_observer as S


SVG = '<svg xmlns="http://www.w3.org/2000/svg"><g><text>  Alpha\n<tspan>Grüße 東京</tspan> tail </text><text/><text>same</text><text>same</text></g></svg>'
EXPECTED = ["  Alpha\nGrüße 東京 tail ", "", "same", "same"]


def seal(schema):
    path = Path(schema["contract"]["file"])
    path.write_text(json.dumps({"observer": S.declaration(schema)}, ensure_ascii=False))
    schema["contract"]["sha256"] = R._file_hash(path)
    return schema


class DOMServer:
    """An independent scripted wire peer, not a reimplementation of the parser."""
    def __init__(self, app):
        self.app = app
        self.endpoint = "http://127.0.0.1:28614"
        self.url = (app / "Contents/Resources/view.html").as_uri()
        self.browser_url = "ws://127.0.0.1:28614/devtools/browser/owned"
        self.page_url = "ws://127.0.0.1:28614/devtools/page/owned"
        self.target = {"id": "owned", "type": "page", "url": self.url, "webSocketDebuggerUrl": self.page_url}
        self.markup, self.fault = SVG, None
        self.requests, self.connections = [], []
        self.read_count, self.root_count = 0, 0
        self.full_calls, self.enable_count = 0, 0
        self.namespace_less, self.context_fault = False, None
        self.expected_file, self.contract_file, self.config_file = None, None, None

    def http(self, endpoint, path, deadline):
        assert endpoint == self.endpoint
        if path == "/json/version":
            return {"webSocketDebuggerUrl": self.browser_url}
        assert path == "/json/list"
        value = copy.deepcopy(self.target)
        if self.read_count and self.fault == "target_changed":
            value["id"] = "replacement"
            value["webSocketDebuggerUrl"] = "ws://127.0.0.1:28614/devtools/page/replacement"
        return [value]

    def connect(self, url, deadline):
        assert url in {self.browser_url, self.page_url}
        connection = DOMConnection(self)
        self.connections.append(connection)
        return connection

    def response(self, request):
        self.requests.append(copy.deepcopy(request))
        method, params = request["method"], request["params"]
        assert method in {"Page.getFrameTree", "DOM.enable", "DOM.getDocument", "DOM.querySelectorAll", "DOM.describeNode", "DOM.getOuterHTML"}, "observer attempted app/runtime evaluation"
        if method == "DOM.enable":
            assert params == {"includeWhitespace": "all"} and self.root_count == 0
            self.enable_count += 1
            return {}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-owned", "url": self.url,
                                           "loaderId": "changed" if self.read_count and self.fault == "frame_changed" else "loader-owned"}}}
        if method == "DOM.getDocument":
            assert params == {"depth": 0, "pierce": False}
            self.root_count += 1
            assert self.root_count == 1, "getDocument resets Chromium frontend bindings; no second bootstrap"
            return {"root": {"nodeType": 9, "nodeId": 10}}
        if method == "DOM.querySelectorAll":
            assert params["nodeId"] == 10
            if params["selector"] == "svg svg":
                return {"nodeIds": [20] if self.fault == "nested_root" else []}
            assert params["selector"] == "div.canvas > svg"
            if self.fault == "two_roots":
                return {"nodeIds": [20, 30]}
            if self.fault == "missing_root":
                return {"nodeIds": []}
            if self.fault == "duplicate_id":
                return {"nodeIds": [20, 20]}
            if self.fault == "bool_id":
                return {"nodeIds": [True]}
            return {"nodeIds": [21 if self.read_count and self.fault == "replaced_root" else 20]}
        if method == "DOM.describeNode":
            assert params["pierce"] is False
            if params["depth"] == -1:
                assert params["nodeId"] == 20 and self.enable_count == 1
                self.full_calls += 1
                return {"node": self.full_dom()}
            assert params["depth"] == 0
            node = {"nodeType": 1, "nodeId": 0, "backendNodeId": params["nodeId"] + 100,
                    "nodeName": "svg", "localName": "svg", "isSVG": True,
                    "childNodeCount": 1, "attributes": [] if self.namespace_less else ["xmlns", S.SVG_NS]}
            if self.read_count and self.fault == "metadata_changed":
                node["attributes"] += ["class", "different"]
            if self.fault == "shadow":
                node["shadowRoots"] = [{"nodeId": 999}]
            if self.fault == "child_count":
                node["childNodeCount"] = False
            if self.fault == "not_svg":
                node["isSVG"] = False
            return {"node": node}
        assert method == "DOM.getOuterHTML" and params == {"nodeId": 20}
        self.read_count += 1
        if self.fault == "timeout":
            raise TimeoutError("synthetic read deadline")
        if self.fault == "reference_changed":
            self.expected_file.write_text('["changed"]')
        if self.fault == "declaration_changed":
            self.contract_file.write_text('{}')
        if self.fault == "config_changed":
            self.config_file.write_text(self.config_file.read_text() + " ")
        if self.fault == "app_changed":
            (self.app / "Contents/Resources/view.html").write_text("changed original app bytes")
        return {"outerHTML": self.markup}

    def full_dom(self):
        """Independent hand-authored DOM tree for SVG, including whitespace."""
        counter = 1000
        def identity():
            nonlocal counter
            counter += 1
            return counter
        def text(value):
            return {"nodeId": 0, "backendNodeId": identity(), "nodeType": 3, "nodeName": "#text",
                    "localName": "", "nodeValue": value}
        def element(tag, children=(), attributes=()):
            return {"nodeId": 0, "backendNodeId": identity(), "nodeType": 1, "nodeName": tag,
                    "localName": tag, "nodeValue": "", "isSVG": True,
                    "attributes": list(attributes), "childNodeCount": len(children), "children": list(children)}
        first = element("text", [text("  Alpha\n"), element("tspan", [text("Grüße 東京")]), text(" tail ")])
        group = element("g", [first, element("text"), element("text", [text("same")]), element("text", [text("same")])])
        root = element("svg", [group], [] if self.namespace_less else ["xmlns", S.SVG_NS])
        root["backendNodeId"] = 120
        if self.context_fault == "foreign_same_tag":
            group["isSVG"] = False
        elif self.context_fault == "truncated_children":
            group["children"].pop()
        elif self.context_fault == "missing_children":
            group.pop("children")
        elif self.context_fault == "nested_shadow":
            first["shadowRoots"] = [{"nodeId": 777}]
        elif self.context_fault == "topology":
            group["children"][0] = element("g", [first])
        elif self.context_fault == "attributes":
            group["attributes"] = ["transform", "different"]
        elif self.context_fault == "duplicate_backend":
            first["backendNodeId"] = group["backendNodeId"]
        elif self.context_fault == "wrong_root":
            root["backendNodeId"] = 888
        elif self.context_fault == "unsupported_node":
            first["children"][0]["nodeType"] = 4
        elif self.context_fault == "metadata_changed" and self.read_count:
            first["children"][0]["nodeValue"] = "changed during capture"
        return root


class DOMConnection:
    def __init__(self, server):
        self.server, self.pending, self.closed = server, None, False
        self.queue = []

    def send(self, raw):
        self.pending = json.loads(raw)

    def recv(self, **_kwargs):
        if self.queue:
            return self.queue.pop(0)
        assert self.pending is not None
        request, self.pending = self.pending, None
        raw = json.dumps({"id": request["id"], "result": self.server.response(request)}, ensure_ascii=False)
        if self.server.fault == "document_updated" and request["method"] == "DOM.getOuterHTML":
            self.queue.append(raw)
            return json.dumps({"method": "DOM.documentUpdated", "params": {}})
        if self.server.fault == "initial_document_updated" and request["method"] == "DOM.enable":
            self.queue.append(raw)
            return json.dumps({"method": "DOM.documentUpdated", "params": {}})
        if self.server.fault == "bootstrap_document_updated" and request["method"] == "DOM.getDocument":
            self.queue.append(raw)
            return json.dumps({"method": "DOM.documentUpdated", "params": {}})
        return raw

    def close(self):
        if self.server.fault == "close_failed":
            raise OSError("synthetic socket close failure")
        self.closed = True


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    app = (tmp_path / "Unmodified Fixture.app").resolve()
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.fixture.dom",
                                                            "CFBundleExecutable": "fixture", "CFBundleVersion": "1"}))
    (app / "Contents/MacOS/fixture").write_bytes(b"inert source-test file; never executed")
    (app / "Contents/Resources/view.html").write_text("inert source-test document; never loaded")
    expected = (tmp_path / "expected.json").resolve()
    expected.write_text(json.dumps(EXPECTED, ensure_ascii=False))
    contract = (tmp_path / "contract.json").resolve()
    schema = {"format": S.SCHEMA, "kind": S.KIND, "target": R.fingerprint(app),
              "selector": "div.canvas > svg", "limits": copy.deepcopy(S.LIMITS),
              "reference": {"format": "json_string_list", "file": str(expected), "sha256": R._file_hash(expected)},
              "contract": {"file": str(contract), "sha256": "0" * 64, "pointer": "/observer"}}
    seal(schema)
    server = DOMServer(app)
    config = (tmp_path / "private-operator.json").resolve()
    config.write_text(json.dumps({"format": I.TRANSPORT, "endpoint": server.endpoint,
                                 "relative_url": "Contents/Resources/view.html", "target": schema["target"]}))
    config.chmod(0o600)
    server.expected_file, server.contract_file, server.config_file = expected, contract, config
    main = {"pid": 10101, "uid": os.getuid(), "launch_time": 123.5, "bundle": str(app),
            "bundle_id": schema["target"]["bundle_id"], "executable": str(app / "Contents/MacOS/fixture")}

    def identity(*_args):
        return {**main, "pid": 20202} if server.read_count and server.fault == "pid_changed" else copy.deepcopy(main)

    def listener(*_args):
        return {"pid": main["pid"], "uid": main["uid"], "listen_addresses":
                ["127.0.0.1:28615" if server.read_count and server.fault == "listener_changed" else "127.0.0.1:28614"]}

    def renderers(*_args):
        return [{"pid": 30304 if server.read_count and server.fault == "renderer_changed" else 30303,
                 "ppid": main["pid"], "uid": main["uid"], "started_at": "fixture", "executable": str(app / "Contents/MacOS/fixture")}]

    monkeypatch.setattr(B, "process_identity", identity)
    monkeypatch.setattr(I, "_listener", listener)
    monkeypatch.setattr(I, "_renderer_processes", renderers)
    monkeypatch.setattr(I, "_http_json", server.http)
    monkeypatch.setattr(I, "_socket", server.connect)
    return SimpleNamespace(app=app, schema=schema, root=tmp_path, config=config, server=server)


def collect(env, name="observation"):
    return S.collect(str(env.app), env.schema, str(env.root / name), config_file=str(env.config))


def test_parser_preserves_complete_element_order_tspans_empty_duplicates_and_unicode():
    got = S.parse_svg(SVG)
    assert got["values"] == EXPECTED
    assert got["text_element_count"] == 4 and got["node_count"] == 7
    assert got["node_count_kind"] == "svg_elements" and got["complete"] is True
    assert got["markup_sha256"] == hashlib.sha256(SVG.encode()).hexdigest()
    assert got["values_sha256"] == R.digest(EXPECTED)
    assert got["text_bytes"] == sum(len(value.encode()) for value in EXPECTED)
    assert got["normalization"] == "none" and got["visibility_proved"] is False


def test_parser_decodes_predefined_and_numeric_xml_entities_without_normalizing_values():
    got = S.parse_svg('<svg xmlns="http://www.w3.org/2000/svg"><text>&amp;&lt;&gt;&quot;&apos;&#13;&#10;&#9;Ａ</text><text>  </text></svg>')
    assert got["values"] == ['&<>"\'\r\n\tＡ', '  ']
    assert got["values"][0] != '&<>"\'\r\n\tA'
    nested = S.parse_svg('<svg xmlns="http://www.w3.org/2000/svg"><text>A<tspan>B<tspan> C </tspan>D</tspan>E</text></svg>')
    assert nested["values"] == ["AB C DE"]


@pytest.mark.parametrize("markup", [
    '<svg><text>not namespaced</text></svg>',
    '<svg xmlns="urn:foreign"><text>foreign</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>unfinished',
    '<svg xmlns="http://www.w3.org/2000/svg"><g xmlns=""><text>foreign</text></g></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div>hidden text</div></foreignObject></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><iframe/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><template><text>unobserved</text></template></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><use href="#other"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text><g>not tspan</g></text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><tspan>orphan</tspan></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><defs><text>non-rendering label</text></defs></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg">unaccounted content<text>label</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>A\rB</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>A<![CDATA[B]]>C</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>&unknown;</text></svg>',
    '<!DOCTYPE svg [<!ENTITY e "replacement">]><svg xmlns="http://www.w3.org/2000/svg"><text>&e;</text></svg>',
    '<!DOCTYPE svg SYSTEM "file:///private/secret"><svg xmlns="http://www.w3.org/2000/svg"/>',
    '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><!-- unsupported serialized node --><text>x</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><script>app()</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>text{display:none}</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="app()"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>\ud800</text></svg>',
])
def test_incomplete_foreign_and_unsupported_serializations_never_become_partial_observations(markup):
    with pytest.raises(ValueError):
        S.parse_svg(markup)


@pytest.mark.parametrize("attribute", ['hidden=""', 'aria-hidden="true"', 'display="none"', 'visibility="hidden"',
                                       'opacity="0"', 'style="display: none !important"',
                                       'style="visibility:collapse"', 'style="opacity:0%"'])
def test_known_hidden_branches_are_refused_even_with_the_expected_text(attribute):
    with pytest.raises(S.Unsupported):
        S.parse_svg('<svg xmlns="http://www.w3.org/2000/svg"><g ' + attribute + '><text>desired</text></g></svg>')


@pytest.mark.parametrize("budget,value,markup", [
    ("max_nodes", 2, '<svg xmlns="http://www.w3.org/2000/svg"><g><text>x</text></g></svg>'),
    ("max_depth", 2, '<svg xmlns="http://www.w3.org/2000/svg"><g><text>x</text></g></svg>'),
    ("max_text_elements", 1, '<svg xmlns="http://www.w3.org/2000/svg"><text/><text/></svg>'),
    ("max_text_bytes", 3, '<svg xmlns="http://www.w3.org/2000/svg"><text>東京</text></svg>'),
    ("max_markup_bytes", 8, '<svg xmlns="http://www.w3.org/2000/svg"/>')])
def test_complete_capture_refuses_budgets_instead_of_truncating(budget, value, markup):
    limits = {**S.LIMITS, budget: value}
    with pytest.raises(ValueError):
        S.parse_svg(markup, limits)


@pytest.mark.parametrize("selector", ["svg text", "svg:first-child", "div > svg:nth-child(2)", "svg:has(text)",
                                     'svg[value="desired"]', "svg,svg", "svg + svg", "svg~svg", "svg[title]",
                                     "div..canvas > svg", "div.canvas.canvas > svg", "div  svg", " div > svg", "div>svg"])
def test_structural_selector_cannot_cherry_pick_labels_positions_or_values(selector):
    with pytest.raises(ValueError):
        S.structural_selector(selector)


def test_pure_schema_and_declaration_are_closed_and_do_no_io(environment, monkeypatch):
    schema = environment.schema
    monkeypatch.setattr(Path, "lstat", lambda *_a, **_k: pytest.fail("pure schema performed IO"))
    checked = S.validate(schema)
    declared = S.declaration(schema)
    checked["limits"]["max_nodes"] = 1
    assert schema["limits"]["max_nodes"] == S.LIMITS["max_nodes"]
    assert declared["request_sha256"] == R.digest(declared["request"])
    assert declared["purpose"] == "complete_svg_dom_text" and declared["read_only"] is True
    assert declared["independent_of_actor"] is True and declared["task_proof_authority"] is False
    assert declared["visibility_proved"] is False and declared["app_javascript"] is False
    for extra in ("actor", "javascript", "expected_value", "proof_created"):
        with pytest.raises(ValueError):
            S.validate({**schema, extra: "not permitted"})
    with pytest.raises(ValueError):
        S.validate({**schema, "limits": {**schema["limits"], "max_nodes": True}})


def test_vm_guard_runs_before_any_resource_or_runtime_io(environment, monkeypatch):
    def deny():
        raise PermissionError("not guest")
    monkeypatch.setattr(R, "assert_guest", deny)
    monkeypatch.setattr(Path, "lstat", lambda *_a, **_k: pytest.fail("resource IO preceded VM guard"))
    monkeypatch.setattr(I, "_socket", lambda *_a, **_k: pytest.fail("network preceded VM guard"))
    with pytest.raises(PermissionError, match="not guest"):
        collect(environment)
    with pytest.raises(PermissionError, match="not guest"):
        S.resources(str(environment.app), environment.schema, config_file=str(environment.config))
    with pytest.raises(PermissionError, match="not guest"):
        S.adapter_identity()


def test_collector_compares_the_entire_independent_list_with_no_task_proof_or_live_handles(environment):
    result = collect(environment)
    assert result["status"] == "observed" and result["complete"] is True and result["matches"] is True
    assert result["values"] == EXPECTED and result["expected"] == EXPECTED
    assert result["capture"]["markup_sha256"] == hashlib.sha256(SVG.encode()).hexdigest()
    assert result["root_before"] == result["root_after"]
    assert result["identity"] == result["after_identity"]
    assert result["evidence_only"] is True and result["proof_created"] is False and result["task_result_verified"] is False
    assert result["model_calls"] == result["app_evaluations"] == result["method_calls"] == result["getters_invoked"] == 0
    assert result["actuated"] is False and result["state_uncertain"] is False and result["resource_review_required"] is False
    assert result["visibility_proved"] is False and result["geometry_proved"] is False and result["persistence_proved"] is False
    assert result["cleanup"] == {"page_socket_closed": True, "browser_socket_closed": True,
                                  "requested_reads_completed": True, "local_live_ids_discarded": True}
    assert all(connection.closed for connection in environment.server.connections)
    assert environment.server.read_count == environment.server.root_count == 1
    text = json.dumps(result, ensure_ascii=False)
    assert "webSocketDebuggerUrl" not in text and environment.server.page_url not in text
    assert "backendNodeId" not in text and '"nodeId"' not in text and "outerHTML" not in result
    assert json.loads(Path(result["receipt"]).read_text()) == result


@pytest.mark.parametrize("replacement", [
    EXPECTED[:-1], ["wrong", *EXPECTED[1:]], [*EXPECTED, "extra"],
    [EXPECTED[0].strip(), *EXPECTED[1:]], [EXPECTED[0], "", "same"], list(reversed(EXPECTED)),
    [EXPECTED[0].replace("東京", "Tokyo"), *EXPECTED[1:]]])
def test_wrong_extra_missing_reordered_trimmed_or_collapsed_labels_refute_instead_of_searching(environment, replacement):
    path = Path(environment.schema["reference"]["file"])
    path.write_text(json.dumps(replacement, ensure_ascii=False))
    environment.schema["reference"]["sha256"] = R._file_hash(path)
    seal(environment.schema)
    result = collect(environment)
    assert result["status"] == "refuted" and result["complete"] is True and result["matches"] is False
    assert result["values"] == EXPECTED and result["expected"] == replacement
    assert environment.server.read_count == 1 and result["proof_created"] is False


@pytest.mark.parametrize("fault,status,reads", [
    ("two_roots", "ambiguous", 0), ("missing_root", "unknown", 0), ("nested_root", "unsupported", 0),
    ("duplicate_id", "unknown", 0), ("bool_id", "unknown", 0), ("shadow", "unsupported", 0),
    ("child_count", "unknown", 0), ("not_svg", "unknown", 0), ("replaced_root", "unknown", 1),
    ("metadata_changed", "unknown", 1), ("frame_changed", "unknown", 1), ("target_changed", "unknown", 1),
    ("pid_changed", "unknown", 1), ("listener_changed", "unknown", 1), ("renderer_changed", "unknown", 1),
    ("reference_changed", "unknown", 1), ("declaration_changed", "unknown", 1), ("config_changed", "unknown", 1),
    ("timeout", "unknown", 1), ("document_updated", "unknown", 1), ("app_changed", "unknown", 1)])
def test_identity_wholeness_and_runtime_failures_stop_without_retry_or_proof(environment, fault, status, reads):
    environment.server.fault = fault
    result = collect(environment)
    assert result["status"] == status and result["complete"] is False and result["matches"] is None
    assert result["proof_created"] is False and result["retry"] is False
    assert result["state_uncertain"] is False and result["resource_review_required"] is False
    assert environment.server.read_count == reads
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("malformed", ['<svg xmlns="http://www.w3.org/2000/svg"><text>partial',
                                      '<svg><text>right words with wrong namespace</text></svg>',
                                      '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><svg xmlns="http://www.w3.org/2000/svg"><text>partial match</text></svg></foreignObject></svg>'])
def test_matching_fragments_in_an_incomplete_or_foreign_subtree_cannot_rescue_collection(environment, malformed):
    environment.server.markup = malformed
    result = collect(environment)
    assert result["status"] in {"unknown", "unsupported"} and result["complete"] is False
    assert result["matches"] is None and environment.server.read_count == 1
    assert all(connection.closed for connection in environment.server.connections)


def test_socket_cleanup_failure_preserves_observed_values_but_cannot_complete_the_observation(environment):
    environment.server.fault = "close_failed"
    result = collect(environment)
    assert result["values"] == EXPECTED
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None
    assert result["resource_review_required"] is True and result["state_uncertain"] is False
    assert result["cleanup"]["page_socket_closed"] is False and result["cleanup"]["browser_socket_closed"] is False
    assert environment.server.read_count == 1 and result["retry"] is False


@pytest.mark.parametrize("part", ["hash", "request", "purpose", "proof_authority", "reference_format"])
def test_changed_or_unreviewed_independent_contracts_refuse_before_any_connection(environment, part):
    claim = environment.schema["contract"]
    if part == "hash":
        claim["sha256"] = "f" * 64
    else:
        body = json.loads(Path(claim["file"]).read_text())
        if part == "request":
            body["observer"]["request_sha256"] = "f" * 64
        elif part == "purpose":
            body["observer"]["independent_of_actor"] = False
        elif part == "proof_authority":
            body["observer"]["task_proof_authority"] = True
        else:
            body["observer"]["reference"]["format"] = "text_fragment"
        Path(claim["file"]).write_text(json.dumps(body))
        claim["sha256"] = R._file_hash(Path(claim["file"]))
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False
    assert not environment.server.connections and environment.server.read_count == 0


@pytest.mark.parametrize("raw", ['{"label":"not a list"}', '["x",1]', '["x",null]', '["\\ud800"]', '["x",NaN]', '["x"'])
def test_expected_state_requires_one_complete_typed_json_string_list(environment, raw):
    path = Path(environment.schema["reference"]["file"])
    path.write_text(raw)
    environment.schema["reference"]["sha256"] = R._file_hash(path)
    seal(environment.schema)
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False
    assert not environment.server.connections


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
def test_oracle_files_must_be_canonical_owned_regular_files_without_aliases(environment, alias):
    path = Path(environment.schema["reference"]["file"])
    alternate = environment.root / "oracle-alias.json"
    if alias == "symlink":
        alternate.symlink_to(path)
    elif alias == "hardlink":
        os.link(path, alternate)
    else:
        parent = environment.root / "parent-alias"
        parent.symlink_to(environment.root, target_is_directory=True)
        alternate = parent / path.name
    environment.schema["reference"]["file"] = str(alternate)
    seal(environment.schema)
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False
    assert not environment.server.connections


def test_changed_collector_dependency_bytes_are_refused_after_the_read(environment, monkeypatch):
    original = S.adapter_identity
    def identity():
        value = original()
        if environment.server.read_count:
            value["dependencies"]["inspector_bridge"]["sha256"] = "f" * 64
        return value
    monkeypatch.setattr(S, "adapter_identity", identity)
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None
    assert environment.server.read_count == 1 and result["retry"] is False
    assert all(connection.closed for connection in environment.server.connections)


def contextual(environment):
    environment.schema["namespace_mode"] = "dom_svg_elements"
    environment.server.namespace_less = True
    environment.server.markup = SVG.replace(' xmlns="http://www.w3.org/2000/svg"', '', 1)
    seal(environment.schema)
    return environment


def test_namespace_less_html_serialization_requires_actual_complete_dom_namespace_witness(environment):
    contextual(environment)
    raw = environment.server.markup
    with pytest.raises(S.Unsupported):
        S.parse_svg(raw)
    result = collect(environment)
    assert result["status"] == "observed" and result["values"] == EXPECTED and result["complete"] is True
    capture = result["capture"]
    assert capture["namespace_mode"] == "dom_svg_elements"
    assert capture["markup_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert capture["markup_sha256"] != hashlib.sha256(SVG.encode()).hexdigest()
    witness = capture["namespace_context"]
    assert witness["format"] == S.DOM_CONTEXT and witness["namespace"] == S.SVG_NS
    assert witness["element_coverage_only"] is True and witness["whitespace_source"] == "DOM.getOuterHTML"
    assert witness["element_count"] == len(witness["elements"]) == 7
    assert witness["elements_sha256"] == R.digest(witness["elements"])
    assert witness["root_identity_sha256"] == result["root_before"]["identity_sha256"]
    assert result["namespace_context_after_sha256"] == R.digest(witness) and result["namespace_context_stable"] is True
    assert environment.server.enable_count == environment.server.root_count == environment.server.read_count == 1
    assert environment.server.full_calls == 2
    assert result["proof_created"] is False and result["task_result_verified"] is False
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("context_fault", ["foreign_same_tag", "truncated_children", "missing_children", "nested_shadow",
                                          "topology", "attributes", "duplicate_backend", "wrong_root", "unsupported_node", "metadata_changed"])
def test_namespace_context_requires_complete_svg_elements_and_same_whole_metadata_snapshot(environment, context_fault):
    contextual(environment)
    environment.server.context_fault = context_fault
    result = collect(environment)
    assert result["status"] in {"unknown", "unsupported"} and result["complete"] is False and result["matches"] is None
    assert result["proof_created"] is False and result["retry"] is False and result["state_uncertain"] is False
    assert environment.server.root_count == environment.server.enable_count == 1
    assert environment.server.read_count <= 1
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("conflict", ['xmlns="urn:foreign"', 'xmlns=""'])
def test_dom_svg_attribution_never_overrides_conflicting_explicit_namespace_declarations(environment, conflict):
    contextual(environment)
    environment.server.markup = environment.server.markup.replace("<g>", "<g " + conflict + ">", 1)
    result = collect(environment)
    assert result["status"] == "unsupported" and result["complete"] is False
    assert result["matches"] is None and environment.server.read_count == 1
    assert all(connection.closed for connection in environment.server.connections)


def test_contextual_mode_does_not_accept_caller_assertions_or_change_strict_default(environment):
    schema = copy.deepcopy(environment.schema)
    for key, value in (("namespace_mode", "trust_svg"), ("namespace_context", {"isSVG": True})):
        with pytest.raises(ValueError):
            S.validate({**schema, key: value})
    with pytest.raises(TypeError):
        S.parse_svg(SVG, dom_context={"isSVG": True})
    result = collect(environment)
    assert result["status"] == "observed" and environment.server.enable_count == environment.server.full_calls == 0
    assert "namespace_context" not in result["capture"]


def test_prebootstrap_initial_notice_is_counted_once_but_document_change_during_bootstrap_is_fatal(environment):
    contextual(environment)
    environment.server.fault = "initial_document_updated"
    result = collect(environment)
    assert result["status"] == "observed" and result["prebootstrap_document_updates"] == 1
    assert environment.server.root_count == 1 and environment.server.read_count == 1


def test_namespace_mode_never_rebootstraps_after_a_document_change_during_the_initial_request(environment):
    contextual(environment)
    environment.server.fault = "bootstrap_document_updated"
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False
    assert result["prebootstrap_document_updates"] == 0
    assert environment.server.root_count == 1 and environment.server.read_count == 0
    assert all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
def test_transport_pins_cannot_hide_a_configuration_alias(environment, alias):
    path = environment.config
    alternate = environment.root / "config-alias.json"
    if alias == "symlink":
        alternate.symlink_to(path)
    elif alias == "hardlink":
        os.link(path, alternate)
    else:
        parent = environment.root / "config-parent-alias"
        parent.symlink_to(environment.root, target_is_directory=True)
        alternate = parent / path.name
    environment.config = alternate
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False
    assert not environment.server.connections


MIXED_EXPECTED = ["SVG:Grüße 東京", " line1\r\ne\u0301 Ａ\u00a0&… tail ", "", "same", "same"]
MIXED_MARKUP = ('<svg><g>\n  <text>SVG:<tspan>Grüße 東京</tspan></text>'
                '<foreignObject><div><div><div> line1\r\n<span>e\u0301 Ａ&nbsp;&amp;…</span> tail </div></div></div></foreignObject>'
                '<foreignObject><div></div></foreignObject><foreignObject><div>same</div></foreignObject>'
                '<text>same</text></g></svg>')


def mixed_dom():
    """Independent native metadata, not derived from the markup or observer."""
    counter = 2000
    def identity():
        nonlocal counter
        counter += 1
        return counter
    def text(value):
        return {"nodeId": 0, "backendNodeId": identity(), "nodeType": 3, "nodeName": "#text",
                "localName": "", "nodeValue": value}
    def element(tag, children=(), *, html=False, attributes=()):
        node = {"nodeId": 0, "backendNodeId": identity(), "nodeType": 1, "nodeName": tag.upper() if html else tag,
                "localName": tag, "nodeValue": "", "attributes": list(attributes),
                "childNodeCount": len(children), "children": list(children)}
        if not html:
            node["isSVG"] = True
        return node
    html_text = element("div", [element("div", [element("div", [text(" line1\r\n"),
        element("span", [text("e\u0301 Ａ\u00a0&…")], html=True), text(" tail ")], html=True)], html=True)], html=True)
    group = element("g", [text("\n  "), element("text", [text("SVG:"), element("tspan", [text("Grüße 東京")])]),
        element("foreignObject", [html_text]), element("foreignObject", [element("div", html=True)]),
        element("foreignObject", [element("div", [text("same")], html=True)]), element("text", [text("same")])])
    root = element("svg", [group])
    root["backendNodeId"] = 120
    return root


def dom_nodes(root):
    result = [root]
    for child in root.get("children", []):
        result.extend(dom_nodes(child))
    return result


def mixed(environment, *, root=None, expected=MIXED_EXPECTED):
    environment.schema.update(kind=S.MIXED_KIND, namespace_mode=S.MIXED_MODE)
    environment.server.namespace_less = True
    environment.server.markup = MIXED_MARKUP
    tree = mixed_dom() if root is None else root
    environment.server.full_dom = lambda: copy.deepcopy(tree)
    environment.server.expected_file.write_text(json.dumps(expected, ensure_ascii=False))
    environment.schema["reference"]["sha256"] = R._file_hash(environment.server.expected_file)
    seal(environment.schema)
    return environment


def mixed_witness(root, limits=None):
    limits = copy.deepcopy(S.LIMITS if limits is None else limits)
    metadata = {"identity_sha256": R.digest({"node_id": 20, "backend_node_id": 120}),
                "attributes_sha256": R.digest(root["attributes"]), "child_count": root["childNodeCount"]}
    return S._mixed_witness(root, 20, metadata, limits)


def test_mixed_interpretation_is_explicit_closed_and_does_not_relax_old_svg_modes(environment):
    original = copy.deepcopy(environment.schema)
    for schema in ({**original, "kind": S.MIXED_KIND},
                   {**original, "kind": S.MIXED_KIND, "namespace_mode": "dom_svg_elements"},
                   {**original, "namespace_mode": S.MIXED_MODE}):
        with pytest.raises(ValueError):
            S.validate(schema)
    mixed(environment)
    declaration = S.declaration(environment.schema)
    assert declaration["kind"] == S.MIXED_KIND and declaration["purpose"] == "complete_svg_mixed_dom_label_text"
    assert declaration["request"]["read"] == "DOM.describeNode.nodeValue"
    assert declaration["request"]["concatenation"] == "exact_no_inserted_separators"
    assert declaration["request"]["max_text_node_utf16_exclusive"] == 10000
    assert declaration["request"]["markup_role"] == "unchanged_serialization_hash_only"
    assert declaration["task_proof_authority"] is False
    with pytest.raises(ValueError):
        S.parse_svg(MIXED_MARKUP)
    environment.schema.update(kind=S.KIND, namespace_mode="dom_svg_elements")
    seal(environment.schema)
    result = collect(environment)
    assert result["complete"] is False and result["status"] == "unsupported"
    assert environment.server.read_count == 0


def test_mixed_collector_preserves_complete_order_exact_text_empty_units_and_namespace_evidence(environment):
    mixed(environment)
    result = collect(environment)
    assert result["status"] == "observed" and result["kind"] == S.MIXED_KIND
    assert result["complete"] is True and result["values"] == MIXED_EXPECTED
    capture = result["capture"]
    assert S.validate_mixed_capture(capture, limits=environment.schema["limits"]) == MIXED_EXPECTED
    assert capture["unit_kinds"] == ["svg_text", "svg_foreign_object", "svg_foreign_object", "svg_foreign_object", "svg_text"]
    assert capture["values"][1] != " line1\r\né A &… tail "
    assert capture["markup_sha256"] == hashlib.sha256(MIXED_MARKUP.encode()).hexdigest()
    assert capture["markup_interpreted"] is False and capture["text_source"] == "DOM.describeNode.nodeValue"
    assert capture["node_count_kind"] == "svg_html_elements_and_text"
    assert capture["node_count"] == len(dom_nodes(mixed_dom()))
    assert capture["text_bytes"] == sum(len(node["nodeValue"].encode()) for node in dom_nodes(mixed_dom()) if node["nodeType"] == 3)
    assert capture["witness"]["root_identity_sha256"] == result["root_before"]["identity_sha256"]
    assert capture["witness_after_sha256"] == result["mixed_witness_after_sha256"] == R.digest(capture["witness"])
    assert capture["witness_stable"] is result["mixed_witness_stable"] is True
    assert all(capture[key] is False for key in ("visibility_proved", "geometry_proved", "connectivity_proved", "persistence_proved"))
    assert result["proof_created"] is result["task_result_verified"] is result["actuated"] is False
    assert result["model_calls"] == result["app_evaluations"] == result["method_calls"] == result["getters_invoked"] == 0
    assert result["cleanup"]["requested_reads_completed"] is True
    assert environment.server.enable_count == environment.server.root_count == environment.server.read_count == 1
    assert environment.server.full_calls == 2 and all(connection.closed for connection in environment.server.connections)
    public = json.dumps(result, ensure_ascii=False)
    assert '"backendNodeId"' not in public and '"nodeId"' not in public and MIXED_MARKUP not in public
    assert not any(request["method"].startswith(("Runtime.", "Debugger.")) for request in environment.server.requests)


@pytest.mark.parametrize("expected", [MIXED_EXPECTED[:-1], list(reversed(MIXED_EXPECTED)),
    [MIXED_EXPECTED[0], MIXED_EXPECTED[1].replace("\r\n", "\n"), *MIXED_EXPECTED[2:]],
    [*MIXED_EXPECTED[:2], "same", "same"], [*MIXED_EXPECTED, "extra"]])
def test_mixed_whole_list_wrong_oracle_refutes_without_normalization_or_matching_fragments(environment, expected):
    mixed(environment, expected=expected)
    result = collect(environment)
    assert result["status"] == "refuted" and result["complete"] is True and result["values"] == MIXED_EXPECTED
    assert result["matches"] is False and result["proof_created"] is False and result["retry"] is False


@pytest.mark.parametrize("fault", ["lowercase_html_name", "foreign_html_name", "missing_svg_attribution", "html_outside_foreign_object",
    "nested_svg", "br", "script", "svg_inside_foreign_object", "orphan_tspan", "nested_unit", "definition_label", "shadow",
    "truncated_children", "missing_children", "duplicate_backend", "wrong_root", "comment", "hidden", "interactive",
    "conflicting_xmlns", "text_outside_unit", "bad_attribute", "malformed_svg_flag"])
def test_mixed_metadata_refuses_foreign_hidden_unbounded_or_incomplete_branches_before_markup_read(environment, fault):
    root = mixed_dom()
    nodes = dom_nodes(root)
    html = next(node for node in nodes if node["nodeName"] == "DIV")
    svg_text = next(node for node in nodes if node["localName"] == "text")
    foreign = next(node for node in nodes if node["localName"] == "foreignObject")
    group = root["children"][0]
    if fault == "lowercase_html_name":
        html["nodeName"] = "div"
    elif fault == "foreign_html_name":
        html["nodeName"] = "x:DIV"
    elif fault == "missing_svg_attribution":
        group.pop("isSVG")
    elif fault == "html_outside_foreign_object":
        group["children"][1] = foreign["children"][0]
        foreign["children"] = []
        foreign["childNodeCount"] = 0
    elif fault == "nested_svg":
        svg_text.update(nodeName="svg", localName="svg")
    elif fault in {"br", "script"}:
        html.update(nodeName=fault.upper(), localName=fault)
    elif fault == "svg_inside_foreign_object":
        html.update(nodeName="g", localName="g", isSVG=True)
    elif fault == "orphan_tspan":
        foreign.update(nodeName="tspan", localName="tspan")
    elif fault == "nested_unit":
        next(node for node in nodes if node["localName"] == "tspan").update(nodeName="text", localName="text")
    elif fault == "definition_label":
        group.update(nodeName="defs", localName="defs")
    elif fault == "shadow":
        html["shadowRoots"] = [{"nodeId": 999}]
    elif fault == "truncated_children":
        html["children"].pop()
    elif fault == "missing_children":
        html.pop("children")
    elif fault == "duplicate_backend":
        html["backendNodeId"] = foreign["backendNodeId"]
    elif fault == "wrong_root":
        root["backendNodeId"] = 121
    elif fault == "comment":
        group["children"][0]["nodeType"] = 8
    elif fault == "hidden":
        html["attributes"] = ["style", "display:none !important"]
    elif fault == "interactive":
        html["attributes"] = ["contenteditable", "true"]
    elif fault == "conflicting_xmlns":
        html["attributes"] = ["xmlns", "urn:foreign"]
    elif fault == "text_outside_unit":
        group["children"][0]["nodeValue"] = "unaccounted label"
    elif fault == "bad_attribute":
        html["attributes"] = ["title", "x", "title", "y"]
    elif fault == "malformed_svg_flag":
        html["isSVG"] = 0
    mixed(environment, root=root)
    result = collect(environment)
    assert result["status"] in {"unsupported", "unknown"} and result["complete"] is False and result["matches"] is None
    assert result["proof_created"] is False and result["retry"] is False
    assert result["state_uncertain"] is False and result["resource_review_required"] is False
    assert environment.server.read_count == 0 and all(connection.closed for connection in environment.server.connections)


@pytest.mark.parametrize("value,valid", [("a" * 9999, True), ("a" * 10000, False), ("😀" * 5000, False),
    ("😀" * 4999 + "…", True), ("short literal …", True), ("a" * 9999 + "…", False), ("\ud800", False), ("A\0B", False)])
def test_mixed_text_completeness_uses_native_utf16_limit_not_python_codepoints_or_ellipsis_guess(value, valid):
    root = mixed_dom()
    text = next(node for node in dom_nodes(root) if node["nodeType"] == 3 and node["nodeValue"] == "SVG:")
    text["nodeValue"] = value
    if valid:
        witness = mixed_witness(root)
        capture = S._mixed_capture(MIXED_MARKUP, witness, witness, S.LIMITS)
        assert S.validate_mixed_capture(capture)[0] == value + "Grüße 東京"
    else:
        with pytest.raises(ValueError):
            mixed_witness(root)


@pytest.mark.parametrize("budget,value", [("max_nodes", 8), ("max_depth", 6), ("max_text_elements", 4),
                                        ("max_text_bytes", 5), ("max_markup_bytes", 16)])
def test_mixed_capture_limits_refuse_whole_observation_without_partial_values(environment, budget, value):
    mixed(environment)
    environment.schema["limits"][budget] = value
    seal(environment.schema)
    result = collect(environment)
    assert result["complete"] is False and result["matches"] is None and result["values"] == []
    assert result["proof_created"] is False and result["retry"] is False


@pytest.mark.parametrize("changed", ["text", "attribute", "topology", "native_identity"])
def test_mixed_complete_native_witness_must_remain_identical_across_raw_markup_read(environment, changed):
    mixed(environment)
    def changing_tree():
        root = mixed_dom()
        if environment.server.read_count:
            nodes = dom_nodes(root)
            if changed == "text":
                next(node for node in nodes if node["nodeType"] == 3 and node["nodeValue"] == "same")["nodeValue"] = "different"
            elif changed == "attribute":
                next(node for node in nodes if node["nodeName"] == "DIV")["attributes"] = ["class", "different"]
            elif changed == "topology":
                root["children"][0]["children"][1:3] = list(reversed(root["children"][0]["children"][1:3]))
            else:
                next(node for node in nodes if node["nodeName"] == "DIV")["backendNodeId"] += 5000
        return root
    environment.server.full_dom = changing_tree
    result = collect(environment)
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None
    assert environment.server.read_count == 1 and environment.server.full_calls == 2
    assert result["retry"] is result["proof_created"] is False
    assert all(connection.closed for connection in environment.server.connections)


def test_mixed_pure_validator_recomputes_closed_witness_with_no_io_and_returns_a_copy(environment, monkeypatch):
    mixed(environment)
    result = collect(environment)
    capture = result["capture"]
    monkeypatch.setattr(Path, "lstat", lambda *_a, **_k: pytest.fail("pure mixed validation performed IO"))
    monkeypatch.setattr(I, "_socket", lambda *_a, **_k: pytest.fail("pure mixed validation connected"))
    values = S.validate_mixed_capture(capture, limits=environment.schema["limits"])
    values[0] = "caller mutation"
    assert capture["values"] == MIXED_EXPECTED
    with pytest.raises(ValueError):
        S.validate_mixed_capture(capture, limits={**S.LIMITS, "max_nodes": 128})


@pytest.mark.parametrize("tamper", ["claimed_value", "count", "flags", "mode", "extra_key", "namespace_claim", "live_id",
    "text_node_dropped", "child_coverage", "html_attribution", "hidden", "depth", "before_after"])
def test_mixed_pure_validator_refuses_tampered_values_or_structure_even_after_claimed_hashes_are_recomputed(tamper):
    witness = mixed_witness(mixed_dom())
    capture = S._mixed_capture(MIXED_MARKUP, witness, witness, S.LIMITS)
    nodes = capture["witness"]["nodes"]
    html = next(node for node in nodes if node.get("node_name") == "DIV")
    if tamper == "claimed_value":
        capture["values"][0] = "invented"
    elif tamper == "count":
        capture["node_count"] = True
    elif tamper == "flags":
        capture["visibility_proved"] = True
    elif tamper == "mode":
        capture["namespace_mode"] = "dom_svg_elements"
    elif tamper == "extra_key":
        capture["actor_result"] = "not independent"
    elif tamper == "namespace_claim":
        html["namespace"] = S.HTML_NS
    elif tamper == "live_id":
        html["nodeId"] = 22
    elif tamper == "text_node_dropped":
        nodes.pop(next(index for index, node in enumerate(nodes) if node["node_type"] == 3))
    elif tamper == "child_coverage":
        html["child_count"] += 1
    elif tamper == "html_attribution":
        html["node_name"] = "div"
    elif tamper == "hidden":
        html["attributes"] = ["aria-hidden", "true"]
    elif tamper == "depth":
        html["depth"] += 1
    else:
        capture["witness_after_sha256"] = "0" * 64
    capture["values_sha256"] = R.digest(capture["values"])
    capture["witness"]["nodes_sha256"] = R.digest(nodes)
    if tamper != "before_after":
        capture["witness_after_sha256"] = R.digest(capture["witness"])
    with pytest.raises(ValueError):
        S.validate_mixed_capture(capture)


def test_mixed_socket_release_failure_never_promotes_read_values_to_complete_observation(environment):
    mixed(environment)
    environment.server.fault = "close_failed"
    result = collect(environment)
    assert result["values"] == MIXED_EXPECTED
    assert result["status"] == "unknown" and result["complete"] is False and result["matches"] is None
    assert result["resource_review_required"] is True and result["retry"] is result["proof_created"] is False


if __name__ == "__main__":
    R.assert_guest()
    raise SystemExit(pytest.main([__file__, "-q"]))
