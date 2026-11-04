"""Independent, VM-only observations of complete SVG DOM label text.

The collector reads the browser's serialized DOM through standard CDP commands,
never an application's graph, JavaScript getter, actor result or callable method.
It preserves the ordered content of every SVG ``text`` element, including empty
labels and all nested ``tspan`` text. This is DOM content evidence, not a claim
about pixels, visibility, geometry, connectivity, persistence or task success.
An explicit separate interpretation includes native SVG ``foreignObject``
labels containing only inert HTML div/span wrappers and complete DOM text.
Nothing in this module integrates its observations into harness task proofs.
Imports and pure schema/parser functions perform no IO or native operations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from xml.parsers import expat

from . import rea_mapping as R
from . import rea_runtime_bridge as B
from . import rea_inspector_bridge as I

SCHEMA = "anyuse.rea-svg-observer/1"
CONTRACT = "anyuse.rea-svg-observer-contract/1"
OBSERVATION = "anyuse.rea-svg-observation/1"
DOM_CONTEXT = "anyuse.rea-svg-dom-namespace-context/1"
KIND = "svg_text_elements"
MIXED_KIND = "svg_mixed_label_text"
MIXED_MODE = "dom_svg_html_elements"
MIXED_WITNESS = "anyuse.rea-svg-mixed-dom-witness/1"
MIXED_CAPTURE = "anyuse.rea-svg-mixed-label-capture/1"
SVG_NS = "http://www.w3.org/2000/svg"
HTML_NS = "http://www.w3.org/1999/xhtml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_FILE_BYTES = 2 * 1024**2
MAX_SELECTOR_BYTES = 1024
MAX_MATCHES = 4096
MAX_ATTRIBUTES = 128
MAX_ATTRIBUTE_BYTES = 128 * 1024
LIMITS = {"max_markup_bytes": 1024**2, "max_nodes": 4096, "max_depth": 64,
          "max_text_elements": 1024, "max_text_bytes": 256 * 1024}
_IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
_TAG = r"[A-Za-z][A-Za-z0-9_-]*"
_SIMPLE = re.compile(r"(?P<tag>" + _TAG + r")?(?P<id>\#" + _IDENT + r")?(?P<classes>(?:\." + _IDENT + r")*)\Z")
# Closed SVG vocabulary: no script/style, foreignObject, use/shadow references,
# external documents, HTML, animation or namespace-changing custom elements.
_TAGS = {"svg", "g", "rect", "path", "line", "polyline", "polygon", "circle", "ellipse",
         "text", "tspan", "defs", "clipPath", "marker", "linearGradient", "radialGradient", "stop"}
_NONLABEL_BRANCHES = {"defs", "clipPath", "marker", "linearGradient", "radialGradient", "stop"}
_HTML_TAGS = {"div", "span"}
MAX_DOM_TEXT_UTF16 = 10000  # Chromium describeNode truncates longer text and appends an ellipsis.
_SOURCE_MODULES = {"mapping": R, "runtime_bridge": B, "inspector_bridge": I}


class Unsupported(ValueError):
    pass


class Ambiguous(ValueError):
    pass


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) != set(required) | (set(value) & set(optional)):
        raise ValueError("SVG observer data has missing or unknown fields")


def _text(value, maximum=4096, *, empty=False):
    if not isinstance(value, str) or not empty and not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _path(value):
    if (not _text(value) or not Path(value).is_absolute() or ".." in Path(value).parts or
            str(Path(value)) != value):
        raise ValueError("SVG observer paths must be canonical absolute paths")
    return value


def _limits(value):
    _keys(value, LIMITS)
    if any(type(value[name]) is not int or not 1 <= value[name] <= maximum for name, maximum in LIMITS.items()):
        raise ValueError("SVG observer budgets must be positive bounded integers")
    return copy.deepcopy(value)


def structural_selector(value):
    """A fixed tag/id/class chain, never output-dependent or positional CSS."""
    if not _text(value, MAX_SELECTOR_BYTES) or value != value.strip() or any(c in value for c in "\t\r\n"):
        raise ValueError("SVG root needs a bounded structural selector")
    # Canonical combinators keep the reviewed request literal and unambiguous.
    components = re.split(r" > | ", value)
    if not 1 <= len(components) <= 16 or any(not component for component in components):
        raise ValueError("SVG selector requires a canonical structural chain")
    for component in components:
        match = _SIMPLE.fullmatch(component)
        if match is None or not any(match.group(name) for name in ("tag", "id", "classes")):
            raise ValueError("SVG selector allows only tags, one id and classes")
        classes = match.group("classes").split(".")[1:]
        if len(classes) > 8 or len(set(classes)) != len(classes):
            raise ValueError("SVG selector has duplicated or excessive classes")
    if _SIMPLE.fullmatch(components[-1]).group("tag") != "svg":
        raise ValueError("SVG selector must end at an explicit svg content root")
    return value


def validate(schema):
    """Pure closed validation of a concrete, independently pinned observation."""
    _keys(schema, {"format", "kind", "target", "selector", "limits", "reference", "contract"}, {"namespace_mode"})
    if schema["format"] != SCHEMA or schema["kind"] not in {KIND, MIXED_KIND}:
        raise Unsupported("unsupported SVG observation interpretation")
    target = schema["target"]
    _keys(target, {"bundle_id", "version", "build", "sha256", "files", "bytes"})
    if (not _text(target["bundle_id"], 1024) or not _text(target["version"], 1024, empty=True) or
            not _text(target["build"], 1024, empty=True) or not isinstance(target["sha256"], str) or
            not R.SHA.fullmatch(target["sha256"]) or type(target["files"]) is not int or target["files"] < 1 or
            type(target["bytes"]) is not int or not 1 <= target["bytes"] <= R.MAX_BUNDLE_BYTES):
        raise ValueError("SVG observer requires the complete original app fingerprint")
    structural_selector(schema["selector"])
    _limits(schema["limits"])
    if (schema["kind"] == MIXED_KIND and schema.get("namespace_mode") != MIXED_MODE or
            schema["kind"] == KIND and "namespace_mode" in schema and schema["namespace_mode"] != "dom_svg_elements"):
        raise Unsupported("SVG namespace context requires the explicit collector-backed mode")
    reference = schema["reference"]
    _keys(reference, {"format", "file", "sha256"})
    if reference["format"] != "json_string_list":
        raise Unsupported("SVG references must be complete ordered JSON string lists")
    _path(reference["file"])
    if not isinstance(reference["sha256"], str) or not R.SHA.fullmatch(reference["sha256"]):
        raise ValueError("SVG expected content needs an independently frozen hash")
    claim = schema["contract"]
    _keys(claim, {"file", "sha256", "pointer"})
    _path(claim["file"])
    if (not isinstance(claim["sha256"], str) or not R.SHA.fullmatch(claim["sha256"]) or
            not _text(claim["pointer"], 1024) or not claim["pointer"].startswith("/") or
            re.search(r"~(?![01])", claim["pointer"])):
        raise ValueError("SVG observer needs an exact pinned independent declaration")
    return copy.deepcopy(schema)


def _request(schema):
    value = {"selector": schema["selector"], "root": "unique_outermost_svg", "namespace": SVG_NS,
             "read": "DOM.getOuterHTML", "interpretation": KIND, "limits": schema["limits"],
             "normalization": "none", "nested_text": "all_tspan_content", "ordering": "document_order"}
    if "namespace_mode" in schema:
        value["namespace_mode"] = schema["namespace_mode"]
        value["namespace_evidence"] = {"source": "DOM.describeNode", "depth": -1, "pierce": False,
                                       "element_coverage_only": True, "whitespace_source": "DOM.getOuterHTML"}
    if schema["kind"] == MIXED_KIND:
        value.update(interpretation=MIXED_KIND, read="DOM.describeNode.nodeValue", nested_text="all_descendant_text_nodes",
                     label_units=["svg_text", "svg_foreign_object"], foreign_object_wrappers=sorted(_HTML_TAGS),
                     concatenation="exact_no_inserted_separators", max_text_node_utf16_exclusive=MAX_DOM_TEXT_UTF16,
                     markup_role="unchanged_serialization_hash_only",
                     namespace_evidence={"source": "DOM.describeNode", "depth": -1, "pierce": False,
                         "complete_typed_tree": True, "whitespace_source": "DOM.enable(includeWhitespace=all)",
                         "html_attribution": "unprefixed_uppercase_nodeName_and_lowercase_localName_below_svg_foreignObject"})
    return value


def declaration(schema):
    """Exact reviewed interpretation, independent of an actor or its result."""
    schema = validate(schema)
    request = _request(schema)
    return {"format": CONTRACT, "kind": schema["kind"], "target": schema["target"], "request": request,
            "request_sha256": R.digest(request), "reference": schema["reference"],
            "purpose": "complete_svg_mixed_dom_label_text" if schema["kind"] == MIXED_KIND else "complete_svg_dom_text",
            "read_only": True, "independent_of_actor": True,
            "app_javascript": False, "input_echo_or_helper_completion": False,
            "visibility_proved": False, "geometry_proved": False, "connectivity_proved": False,
            "persistence_proved": False, "task_proof_authority": False}


def _read(path, maximum=MAX_FILE_BYTES, *, owned=True):
    path = Path(_path(str(path)))
    before = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
            owned and before.st_uid != os.getuid() or not 1 <= before.st_size <= maximum):
        raise ValueError("SVG resources must be canonical owned regular files without aliases or oversized content")
    identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
                             info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if identity(opened) != identity(before):
            raise ValueError("SVG resource changed before capture")
        raw = stream.read(maximum + 1)
        if (len(raw) != opened.st_size or identity(os.fstat(stream.fileno())) != identity(opened) or
                identity(path.lstat()) != identity(opened)):
            raise ValueError("SVG resource changed during capture")
    return raw, {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}, (opened.st_dev, opened.st_ino)


def _json(raw):
    def pairs(items):
        value = {}
        for name, item in items:
            if name in value:
                raise ValueError("duplicate SVG declaration/reference JSON key")
            value[name] = item
        return value
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite SVG JSON")))


def _values(value, limits):
    if (not isinstance(value, list) or len(value) > limits["max_text_elements"] or
            any(not _text(item, limits["max_text_bytes"], empty=True) for item in value) or
            sum(len(item.encode("utf-8")) for item in value) > limits["max_text_bytes"]):
        raise ValueError("SVG text elements must be a complete bounded ordered UTF-8 string list")
    return copy.deepcopy(value)


def adapter_identity():
    R.assert_guest()
    source = _read(Path(__file__).resolve(strict=True), owned=False)[1]
    dependencies = {name: _read(Path(module.__file__).resolve(strict=True), owned=False)[1]
                    for name, module in _SOURCE_MODULES.items()}
    return {"source": source, "dependencies": dependencies, "inspector": I.adapter_identity(),
            "protocol": "chromium_renderer_cdp_dom", "caller_javascript": False,
            "interpretation": KIND, "supported_interpretations": [KIND, MIXED_KIND], "limits": copy.deepcopy(LIMITS)}


def _material(app, schema, config_file):
    raw_reference, reference_pin, reference_inode = _read(schema["reference"]["file"])
    if reference_pin["sha256"] != schema["reference"]["sha256"]:
        raise ValueError("independent SVG reference changed")
    expected = _values(_json(raw_reference), schema["limits"])
    raw_contract, contract_pin, contract_inode = _read(schema["contract"]["file"])
    if contract_pin["sha256"] != schema["contract"]["sha256"]:
        raise ValueError("independent SVG declaration changed")
    if B._json_pointer(_json(raw_contract), schema["contract"]["pointer"]) != declaration(schema):
        raise ValueError("SVG declaration does not establish the exact independent read-only request")
    configured = config_file or os.environ.get("ANYUSE_REA_INSPECTOR_CONFIG")
    configured_path = Path(_path(configured))
    if configured_path.resolve(strict=True) != configured_path:
        raise ValueError("SVG operator configuration cannot use a symlink or parent alias")
    config, config_pin = I._config(app, schema["target"], str(configured_path))
    _, canonical_config_pin, _ = _read(configured_path, 16384)
    if any(config_pin.get(key) != canonical_config_pin[key] for key in ("file", "sha256", "bytes")):
        raise ValueError("SVG operator configuration changed during independent pinning")
    if reference_inode == contract_inode:
        raise ValueError("SVG reference and declaration cannot overlap or alias")
    for pin in (reference_pin, contract_pin, config_pin):
        if Path(pin["file"]).is_relative_to(app):
            raise ValueError("SVG expectation/declaration/configuration must be outside the original app")
    pins = {"reference": reference_pin, "declaration": contract_pin,
            "adapter": adapter_identity(), "transport": I._transport(config, config_pin)}
    return config, expected, pins


def resources(bundle, schema, *, config_file=None):
    """Pin resources without connecting, launching, observing or creating proof."""
    R.assert_guest()
    checked = validate(schema)
    app = Path(_path(str(bundle))).resolve(strict=True)
    if R.fingerprint(app) != checked["target"]:
        raise ValueError("SVG target fingerprint changed")
    return _material(app, checked, config_file)[2]


def _known_hidden(name, value):
    lower = value.strip().lower()
    if (name == "hidden" or name == "aria-hidden" and lower == "true" or
            name == "display" and lower == "none" or
            name == "visibility" and lower in {"hidden", "collapse"} or
            name == "content-visibility" and lower == "hidden"):
        raise Unsupported("known hidden SVG branches are outside this observation contract")
    if name in {"opacity", "fill-opacity", "stroke-opacity"}:
        try:
            zero = float(lower.removesuffix("%")) == 0
        except ValueError:
            zero = False
        if zero:
            raise Unsupported("zero-opacity SVG branches are outside this observation contract")


def _attribute(name, value):
    pieces = name.rsplit("}", 1)
    namespace, local = (pieces[0], pieces[1]) if len(pieces) == 2 else (None, name)
    if namespace not in {None, SVG_NS, XML_NS, XLINK_NS}:
        raise Unsupported("foreign SVG attribute namespace")
    if local.lower().startswith("on"):
        raise Unsupported("SVG event attributes are outside this observation contract")
    if namespace == XML_NS and (local != "space" or value not in {"default", "preserve"}):
        raise Unsupported("unsupported XML SVG attribute")
    if local == "href" or namespace == XLINK_NS:
        raise Unsupported("SVG reference/embedded resources are outside this observation contract")
    _known_hidden(local.lower(), value)
    if local == "style":
        if any(token in value for token in ("\\", "/*", "*/", "@")):
            raise Unsupported("unsupported SVG inline style syntax")
        seen = set()
        for item in value.split(";"):
            if not item.strip():
                continue
            if ":" not in item:
                raise Unsupported("malformed SVG inline style")
            key, content = item.split(":", 1)
            key = key.strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9-]*", key) or key in seen:
                raise Unsupported("ambiguous SVG inline style")
            seen.add(key)
            # This deliberately detects known inline hiding only. External CSS,
            # clipping and compositing are not inspected or proved by DOM text.
            _known_hidden(key, re.sub(r"\s*!important\s*$", "", content, flags=re.I))


def _parse_svg(markup, limits=None, *, dom_context=None):
    """Parse complete SVG serialization, preserving ordered text exactly.

    Literal CR is refused: XML parsers otherwise normalize it silently. Numeric
    CR character references remain exact data. Predefined/numeric XML escapes
    are decoded; DTDs, custom entities, CDATA, declarations and comments refuse.
    ``node_count`` explicitly counts SVG element nodes, not serializer text
    chunks or hidden browser handles. No markup is executed or rendered.
    """
    limits = _limits(LIMITS if limits is None else limits)
    if not _text(markup, limits["max_markup_bytes"]) or "\r" in markup or markup != markup.strip():
        raise ValueError("SVG markup must be complete bounded UTF-8 without literal CR or exterior text")
    if dom_context is not None:
        _keys(dom_context, {"format", "namespace", "source", "root_identity_sha256", "elements", "elements_sha256",
                            "element_count", "dom_node_count", "metadata_tree_sha256", "complete", "element_coverage_only",
                            "metadata_whitespace_mode", "whitespace_source"})
        if (dom_context["format"] != DOM_CONTEXT or dom_context["namespace"] != SVG_NS or
                dom_context["source"] != "DOM.describeNode" or dom_context["complete"] is not True or
                dom_context["element_coverage_only"] is not True or
                dom_context["metadata_whitespace_mode"] != "all_requested" or
                dom_context["whitespace_source"] != "DOM.getOuterHTML" or
                not isinstance(dom_context["elements"], list) or
                dom_context["elements_sha256"] != R.digest(dom_context["elements"]) or
                type(dom_context["element_count"]) is not int or
                not 1 <= dom_context["element_count"] == len(dom_context["elements"]) <= limits["max_nodes"] or
                type(dom_context["dom_node_count"]) is not int or
                not dom_context["element_count"] <= dom_context["dom_node_count"] <= LIMITS["max_nodes"] * 4 or
                any(not isinstance(dom_context[key], str) or not R.SHA.fullmatch(dom_context[key])
                    for key in ("root_identity_sha256", "metadata_tree_sha256"))):
            raise ValueError("SVG namespace context is not a complete bounded collector witness")
    values, stack, element_stack, elements, text_parts = [], [], [], [], None
    count, text_bytes, attribute_bytes, depth_seen = 0, 0, 0, 0
    root_seen = root_closed = False
    parser = expat.ParserCreate(namespace_separator="}")

    def refuse(*_args):
        raise Unsupported("SVG DTD/entity/CDATA/comment/declaration constructs are unsupported")

    def namespace(prefix, uri):
        if uri not in {SVG_NS, XML_NS, XLINK_NS}:
            raise Unsupported("SVG subtree changes to an unsupported namespace")

    def start(name, attrs):
        nonlocal count, attribute_bytes, depth_seen, root_seen, text_parts
        if name.startswith(SVG_NS + "}"):
            tag = name[len(SVG_NS) + 1:]
        elif dom_context is not None and "}" not in name:
            # HTML serialization may omit xmlns for actual SVG namespace nodes.
            # A same-root complete DOM witness supplies attribution; markup is
            # neither rewritten nor given an invented namespace declaration.
            tag = name
        else:
            raise Unsupported("every SVG element requires the proper SVG namespace")
        if tag not in _TAGS:
            raise Unsupported("unsupported or foreign SVG subtree element")
        if not stack:
            if root_seen or tag != "svg":
                raise Unsupported("complete SVG serialization requires one svg root")
            root_seen = True
        if text_parts is not None and tag != "tspan":
            raise Unsupported("SVG text content permits only nested tspan elements")
        if tag == "tspan" and text_parts is None:
            raise Unsupported("orphan SVG tspan outside a complete text element")
        if tag == "text":
            if any(parent in _NONLABEL_BRANCHES for parent in stack):
                raise Unsupported("text inside non-rendering SVG definitions is unsupported")
            if len(values) >= limits["max_text_elements"]:
                raise ValueError("SVG text-element budget exceeded")
            values.append(None)
            text_parts = []
        stack.append(tag)
        count += 1
        depth_seen = max(depth_seen, len(stack))
        if count > limits["max_nodes"] or len(stack) > limits["max_depth"]:
            raise ValueError("SVG complete subtree exceeds its element/depth budget")
        if len(attrs) > MAX_ATTRIBUTES:
            raise ValueError("SVG element exceeds its attribute budget")
        for key, value in attrs.items():
            attribute_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            if attribute_bytes > MAX_ATTRIBUTE_BYTES:
                raise ValueError("SVG subtree exceeds its complete attribute budget")
            _attribute(key, value)
        if element_stack:
            elements[element_stack[-1]]["element_children"] += 1
        element_stack.append(len(elements))
        elements.append({"tag": tag, "depth": len(stack), "element_children": 0,
                         "attributes_sha256": R.digest(sorted(attrs.items()))})

    def end(name):
        nonlocal root_closed, text_parts
        tag = name[len(SVG_NS) + 1:] if name.startswith(SVG_NS + "}") else name
        if not stack or stack.pop() != tag:
            raise ValueError("malformed SVG subtree closing element")
        element_stack.pop()
        if tag == "text":
            values[-1] = "".join(text_parts)
            text_parts = None
        if not stack:
            root_closed = True

    def data(value):
        nonlocal text_bytes
        if text_parts is None:
            if value.strip():
                raise Unsupported("SVG character data outside text elements is unsupported")
            return
        text_bytes += len(value.encode("utf-8"))
        if text_bytes > limits["max_text_bytes"]:
            raise ValueError("SVG complete text exceeds its byte budget")
        text_parts.append(value)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = data
    parser.StartNamespaceDeclHandler = namespace
    parser.StartDoctypeDeclHandler = parser.EntityDeclHandler = parser.ExternalEntityRefHandler = refuse
    parser.SkippedEntityHandler = parser.StartCdataSectionHandler = refuse
    parser.CommentHandler = parser.ProcessingInstructionHandler = parser.XmlDeclHandler = refuse
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(markup.encode("utf-8"), True)
    except expat.ExpatError as error:
        raise ValueError("malformed or incomplete SVG serialization") from error
    if not root_seen or not root_closed or stack or element_stack or text_parts is not None:
        raise ValueError("SVG subtree was incomplete")
    if dom_context is not None and elements != dom_context["elements"]:
        raise ValueError("serialized SVG element topology/attributes differ from the complete DOM namespace witness")
    values = _values(values, limits)
    result = {"values": values, "values_sha256": R.digest(values), "complete": True,
              "markup_sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(),
              "markup_bytes": len(markup.encode("utf-8")), "node_count": count,
              "node_count_kind": "svg_elements", "max_depth": depth_seen,
              "text_element_count": len(values), "text_bytes": text_bytes,
              "visibility_proved": False, "normalization": "none"}
    if dom_context is not None:
        result.update(namespace_mode="dom_svg_elements", namespace_context=copy.deepcopy(dom_context))
    return result


def parse_svg(markup, limits=None):
    """Pure strict-namespace parser; caller flags cannot assert DOM attribution."""
    return _parse_svg(markup, limits)


def _mixed_attributes(attributes, *, html=False):
    """Check the full native attribute list, without changing its raw witness."""
    if (not isinstance(attributes, list) or len(attributes) % 2 or len(attributes) > MAX_ATTRIBUTES * 2 or
            any(not _text(item, MAX_ATTRIBUTE_BYTES, empty=True) for item in attributes) or
            any(not _text(name, 256) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", name)
                for name in attributes[::2]) or len(set(attributes[::2])) != len(attributes) // 2):
        raise ValueError("mixed DOM attributes must be complete, bounded and unique")
    size = 0
    for name, value in zip(attributes[::2], attributes[1::2]):
        size += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if name == "xmlns" or name.startswith("xmlns:"):
            if (name == "xmlns" and value != (HTML_NS if html else SVG_NS) or
                    name != "xmlns" and value not in {SVG_NS, XML_NS, XLINK_NS}):
                raise Unsupported("mixed DOM has a conflicting explicit namespace declaration")
            continue
        if html and (name != name.lower() or name in {"is", "contenteditable", "tabindex", "src", "srcset"}):
            raise Unsupported("mixed HTML wrappers must have inert native attributes")
        if ":" in name:
            if name != "xml:space":
                raise Unsupported("mixed DOM has an unsupported prefixed attribute")
            name = XML_NS + "}space"
        _attribute(name, value)
    return size


def _mixed_content(witness, limits, *, check_hash=True):
    """Pure recomputation from raw native attribution, never claimed namespaces."""
    _keys(witness, {"format", "source", "root_identity_sha256", "metadata_tree_sha256", "nodes", "nodes_sha256",
                    "node_count", "complete", "metadata_whitespace_mode"})
    nodes = witness["nodes"]
    if (witness["format"] != MIXED_WITNESS or witness["source"] != "DOM.describeNode" or
            witness["complete"] is not True or witness["metadata_whitespace_mode"] != "all_requested" or
            not isinstance(nodes, list) or not 1 <= len(nodes) <= limits["max_nodes"] or
            type(witness["node_count"]) is not int or witness["node_count"] != len(nodes) or
            any(not isinstance(witness[key], str) or not R.SHA.fullmatch(witness[key])
                for key in ("root_identity_sha256", "metadata_tree_sha256", "nodes_sha256"))):
        raise ValueError("mixed DOM witness must be complete and bounded with exact hashes")
    values, kinds = [], []
    text_bytes = attribute_bytes = max_depth = 0

    def visit(index, depth, unit=None, html_context=False, nonlabel=False):
        nonlocal text_bytes, attribute_bytes, max_depth
        if index >= len(nodes) or depth > limits["max_depth"]:
            raise ValueError("mixed DOM topology or depth is incomplete or exceeds its budget")
        node = nodes[index]
        if not isinstance(node, dict) or type(node.get("node_type")) is not int:
            raise ValueError("mixed DOM witness has an unsupported typed node")
        kind = node["node_type"]
        if kind == 3:
            _keys(node, {"node_type", "node_name", "local_name", "value", "child_count", "depth"})
        elif kind == 1:
            _keys(node, {"node_type", "node_name", "local_name", "is_svg", "attributes", "child_count", "depth"})
        else:
            raise Unsupported("mixed DOM permits only complete element and text nodes")
        count = node["child_count"]
        if (type(node["depth"]) is not int or node["depth"] != depth or type(count) is not int or
                not 0 <= count <= limits["max_nodes"]):
            raise ValueError("mixed DOM child coverage/depth is malformed")
        max_depth = max(max_depth, depth)
        if kind == 3:
            value = node["value"]
            if (index == 0 or node["node_name"] != "#text" or node["local_name"] != "" or count or
                    not _text(value, limits["max_text_bytes"], empty=True)):
                raise ValueError("mixed DOM text must be complete bounded valid UTF-8")
            # describeNode truncates above this native UTF-16 boundary. The
            # boundary itself is refused; a shorter literal ellipsis is data.
            if len(value.encode("utf-16-le")) // 2 >= MAX_DOM_TEXT_UTF16:
                raise Unsupported("mixed DOM text reaches the protocol truncation boundary")
            text_bytes += len(value.encode("utf-8"))
            if text_bytes > limits["max_text_bytes"]:
                raise ValueError("mixed DOM exceeds its complete text byte budget")
            if unit is None:
                if value.strip():
                    raise Unsupported("mixed DOM has nonwhitespace text outside a label unit")
            else:
                values[unit] += value
            return index + 1
        tag, name = node["local_name"], node["node_name"]
        if type(node["is_svg"]) is not bool or not isinstance(tag, str) or not isinstance(name, str):
            raise ValueError("mixed DOM needs explicit native element attribution")
        svg = node["is_svg"] is True
        if svg:
            if (html_context or tag not in _TAGS | {"foreignObject"} or
                    not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_-]*:)?" + re.escape(tag), name)):
                raise Unsupported("mixed DOM contains an unsupported SVG branch")
            if index == 0 and tag != "svg" or index != 0 and tag == "svg":
                raise Unsupported("mixed DOM requires one outermost SVG root without nested SVG")
            if unit is not None and (kinds[unit] != "svg_text" or tag != "tspan"):
                raise Unsupported("SVG text units permit only nested tspan elements")
            if tag == "tspan" and unit is None:
                raise Unsupported("mixed DOM has an orphan SVG tspan")
            if tag in {"text", "foreignObject"}:
                if nonlabel or unit is not None:
                    raise Unsupported("mixed label units cannot nest or appear in SVG definitions")
                if len(values) >= limits["max_text_elements"]:
                    raise ValueError("mixed DOM exceeds its complete label-unit budget")
                unit = len(values)
                values.append("")
                kinds.append("svg_text" if tag == "text" else "svg_foreign_object")
            child_html_context = tag == "foreignObject"
        else:
            # DOM.nodeName is HTML-uppercased only for native HTML elements in
            # an HTML document. Missing isSVG alone is never namespace proof.
            if not html_context or tag not in _HTML_TAGS or name != tag.upper() or ":" in name:
                raise Unsupported("mixed HTML needs native uppercase attribution below SVG foreignObject")
            child_html_context = True
        attribute_bytes += _mixed_attributes(node["attributes"], html=not svg)
        if attribute_bytes > MAX_ATTRIBUTE_BYTES:
            raise ValueError("mixed DOM exceeds its complete attribute byte budget")
        next_index = index + 1
        for _ in range(count):
            next_index = visit(next_index, depth + 1, unit, child_html_context,
                               nonlabel or svg and tag in _NONLABEL_BRANCHES)
        return next_index

    if visit(0, 1) != len(nodes):
        raise ValueError("mixed DOM witness has unconsumed or omitted topology")
    if check_hash and witness["nodes_sha256"] != R.digest(nodes):
        raise ValueError("mixed DOM typed witness hash differs")
    return {"values": _values(values, limits), "unit_kinds": kinds, "text_bytes": text_bytes,
            "max_depth": max_depth, "node_count": len(nodes), "label_unit_count": len(values)}


def _mixed_witness(node, root_id, root_metadata, limits):
    """Project a complete native snapshot into a typed tree without live IDs."""
    nodes, backend_ids = [], set()

    def visit(current, depth):
        if not isinstance(current, dict) or len(nodes) >= limits["max_nodes"] or depth > limits["max_depth"]:
            raise ValueError("mixed DOM complete node/depth budget exceeded or malformed child")
        kind, identity = current.get("nodeType"), current.get("backendNodeId")
        if (type(kind) is not int or kind not in {1, 3} or type(identity) is not int or
                not 0 < identity < 2**31 or identity in backend_ids or
                type(current.get("nodeId")) is not int or not 0 <= current["nodeId"] < 2**31):
            raise I.ProtocolError("mixed DOM witness has unsupported or repeated native identities")
        backend_ids.add(identity)
        if any(key in current for key in ("contentDocument", "templateContent", "assignedSlot", "frameId", "shadowRootType", "pseudoType")):
            raise Unsupported("mixed DOM contains an embedded/frame/template/shadow branch")
        if any(key in current and current[key] != [] for key in ("shadowRoots", "pseudoElements", "distributedNodes")):
            raise Unsupported("mixed DOM contains a shadow/pseudo/distributed branch")
        children = current.get("children", [])
        count = current.get("childNodeCount", 0 if kind == 3 else None)
        if (not isinstance(children, list) or type(count) is not int or count != len(children) or
                not 0 <= count <= limits["max_nodes"]):
            raise I.ProtocolError("mixed DOM child coverage is incomplete or truncated")
        item = {"node_type": kind, "node_name": current.get("nodeName"), "local_name": current.get("localName"),
                "child_count": count, "depth": depth}
        if kind == 3:
            if children or current.get("attributes", []) != [] or current.get("isSVG", False) is not False:
                raise I.ProtocolError("mixed DOM text has unsupported children or element metadata")
            item["value"] = current.get("nodeValue")
        else:
            if current.get("nodeValue") != "" or type(current.get("isSVG", False)) is not bool:
                raise I.ProtocolError("mixed DOM element metadata is malformed")
            item.update(is_svg=current.get("isSVG", False), attributes=copy.deepcopy(current.get("attributes")))
        nodes.append(item)
        for child in children:
            visit(child, depth + 1)

    if (not isinstance(node, dict) or node.get("localName") != "svg" or
            type(node.get("backendNodeId")) is not int or type(node.get("nodeId")) is not int or
            node["nodeId"] not in {0, root_id} or
            R.digest({"node_id": root_id, "backend_node_id": node["backendNodeId"]}) != root_metadata["identity_sha256"]):
        raise I.ProtocolError("mixed DOM witness is not bound to the exact selected root")
    visit(node, 1)
    # Validate values before any canonical JSON digest can encounter a malformed
    # protocol string. The temporary hashes carry no authority on this pass.
    witness = {"format": MIXED_WITNESS, "source": "DOM.describeNode", "nodes": nodes,
               "root_identity_sha256": root_metadata["identity_sha256"], "metadata_tree_sha256": "0" * 64,
               "nodes_sha256": "0" * 64, "node_count": len(nodes), "complete": True,
               "metadata_whitespace_mode": "all_requested"}
    _mixed_content(witness, limits, check_hash=False)
    if (R.digest(node["attributes"]) != root_metadata["attributes_sha256"] or
            node["childNodeCount"] != root_metadata["child_count"]):
        raise I.ProtocolError("mixed DOM root attributes/children differ from exact selection metadata")
    witness.update(nodes_sha256=R.digest(nodes), metadata_tree_sha256=R.digest(node))
    return witness


def validate_mixed_capture(capture, *, limits=None):
    """Pure self-consistency check; origin and task proof require the caller's pins.

    Recompute every value from complete typed native metadata. No IO, rendering,
    XML parsing, namespace assertion, live ID, actor or task execution occurs.
    """
    _keys(capture, {"format", "kind", "namespace_mode", "limits", "text_source", "normalization", "markup_sha256",
                    "markup_bytes", "markup_interpreted", "values", "values_sha256", "unit_kinds", "node_count",
                    "node_count_kind", "label_unit_count", "text_bytes", "max_depth", "complete", "visibility_proved",
                    "geometry_proved", "connectivity_proved", "persistence_proved", "witness", "witness_after_sha256",
                    "witness_stable"})
    selected_limits = _limits(capture["limits"])
    if limits is not None and selected_limits != _limits(limits):
        raise ValueError("mixed capture limits differ from the independently pinned request")
    if (capture["format"] != MIXED_CAPTURE or capture["kind"] != MIXED_KIND or capture["namespace_mode"] != MIXED_MODE or
            capture["text_source"] != "DOM.describeNode.nodeValue" or capture["normalization"] != "none" or
            capture["node_count_kind"] != "svg_html_elements_and_text" or capture["complete"] is not True or
            capture["witness_stable"] is not True or capture["markup_interpreted"] is not False or
            any(capture[key] is not False for key in ("visibility_proved", "geometry_proved", "connectivity_proved", "persistence_proved")) or
            type(capture["markup_bytes"]) is not int or not 1 <= capture["markup_bytes"] <= selected_limits["max_markup_bytes"] or
            not isinstance(capture["markup_sha256"], str) or not R.SHA.fullmatch(capture["markup_sha256"])):
        raise ValueError("mixed capture interpretation, completeness, effects or markup pin differs")
    actual = _mixed_content(capture["witness"], selected_limits)
    if (any(capture[key] != value or type(capture[key]) is not type(value) for key, value in actual.items()) or
            capture["values_sha256"] != R.digest(actual["values"]) or
            capture["witness_after_sha256"] != R.digest(capture["witness"])):
        raise ValueError("mixed capture values/counts or complete before/after witness differ")
    return copy.deepcopy(actual["values"])


def _mixed_capture(markup, before, after, limits):
    if not _text(markup, limits["max_markup_bytes"]):
        raise ValueError("mixed DOM markup must be complete bounded unchanged UTF-8")
    if before != after:
        raise I.Unavailable("complete mixed SVG/HTML DOM text/attributes/topology changed; no retry")
    capture = {"format": MIXED_CAPTURE, "kind": MIXED_KIND, "namespace_mode": MIXED_MODE,
               "limits": copy.deepcopy(limits), "text_source": "DOM.describeNode.nodeValue", "normalization": "none",
               "markup_sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(), "markup_bytes": len(markup.encode("utf-8")),
               "markup_interpreted": False, "values_sha256": None, "node_count_kind": "svg_html_elements_and_text",
               "complete": True, "visibility_proved": False, "geometry_proved": False, "connectivity_proved": False,
               "persistence_proved": False, "witness": copy.deepcopy(before), "witness_after_sha256": R.digest(after),
               "witness_stable": True, **_mixed_content(before, limits)}
    capture["values_sha256"] = R.digest(capture["values"])
    validate_mixed_capture(capture, limits=limits)
    return capture


def _dom_context(node, root_id, root_metadata, limits):
    """Validate a same-root full protocol description, exporting no live IDs.

    The witness covers SVG ELEMENT namespaces/topology/attributes. DOM text
    metadata is bounded and hashed for snapshot stability, but exact output text
    and whitespace come only from the unchanged outerHTML serialization.
    """
    elements, backend_ids = [], set()
    total = text_bytes = attribute_bytes = 0

    def visit(current, depth):
        nonlocal total, text_bytes, attribute_bytes
        if not isinstance(current, dict):
            raise I.ProtocolError("SVG DOM witness contains a malformed child node")
        total += 1
        if total > LIMITS["max_nodes"] * 4:
            raise ValueError("SVG DOM witness exceeds its separate complete-node budget")
        kind, identity = current.get("nodeType"), current.get("backendNodeId")
        if (type(kind) is not int or kind not in {1, 3} or type(identity) is not int or
                not 0 < identity < 2**31 or identity in backend_ids or
                type(current.get("nodeId")) is not int or not 0 <= current["nodeId"] < 2**31):
            raise I.ProtocolError("SVG DOM witness contains unsupported/repeated/malformed node identities")
        backend_ids.add(identity)
        for key in ("contentDocument", "templateContent", "assignedSlot", "frameId", "shadowRootType", "pseudoType"):
            if key in current:
                raise Unsupported("SVG DOM witness contains an unsupported embedded/shadow/frame/template branch")
        for key in ("shadowRoots", "pseudoElements", "distributedNodes"):
            if key in current and current[key] != []:
                raise Unsupported("SVG DOM witness contains an unsupported shadow/pseudo/distributed branch")
        children = current.get("children", [])
        if not isinstance(children, list):
            raise I.ProtocolError("SVG DOM witness children must be completely enumerated")
        count = current.get("childNodeCount", 0 if kind == 3 else None)
        if type(count) is not int or not 0 <= count == len(children) <= LIMITS["max_nodes"] * 4:
            raise I.ProtocolError("SVG DOM witness child coverage is incomplete or truncated")
        if kind == 3:
            if (children or current.get("nodeName") != "#text" or
                    not _text(current.get("nodeValue"), limits["max_markup_bytes"], empty=True) or
                    current.get("attributes", []) != []):
                raise I.ProtocolError("SVG DOM witness has a malformed or unsupported text node")
            text_bytes += len(current["nodeValue"].encode("utf-8"))
            if text_bytes > limits["max_markup_bytes"]:
                raise ValueError("SVG DOM witness text metadata exceeds its byte budget")
            return
        tag = current.get("localName")
        if (current.get("isSVG") is not True or not isinstance(tag, str) or tag not in _TAGS or current.get("nodeValue") != "" or
                not isinstance(current.get("nodeName"), str) or
                not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_-]*:)?" + re.escape(tag), current["nodeName"])):
            raise Unsupported("every DOM witness element must be an explicitly attributed supported SVG element")
        if depth > limits["max_depth"] or len(elements) >= limits["max_nodes"]:
            raise ValueError("SVG DOM witness exceeds its complete element/depth budget")
        attributes = current.get("attributes")
        if (not isinstance(attributes, list) or len(attributes) % 2 or len(attributes) > MAX_ATTRIBUTES * 2 or
                any(not _text(item, MAX_ATTRIBUTE_BYTES, empty=True) for item in attributes) or
                any(not _text(name, 256) for name in attributes[::2]) or
                len(set(attributes[::2])) != len(attributes) // 2):
            raise I.ProtocolError("SVG DOM witness attributes are incomplete or malformed")
        normalized = {}
        for name, value in zip(attributes[::2], attributes[1::2]):
            attribute_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
            if attribute_bytes > MAX_ATTRIBUTE_BYTES:
                raise ValueError("SVG DOM witness exceeds its complete attribute budget")
            if name == "xmlns" or name.startswith("xmlns:"):
                if value not in {SVG_NS, XML_NS, XLINK_NS} or name == "xmlns" and value != SVG_NS:
                    raise Unsupported("SVG DOM witness contains a conflicting explicit namespace declaration")
                continue
            if ":" in name:
                if name != "xml:space":
                    raise Unsupported("unsupported prefixed attribute in SVG DOM namespace witness")
                name = XML_NS + "}space"
            _attribute(name, value)
            normalized[name] = value
        elements.append({"tag": tag, "depth": depth,
                         "element_children": sum(isinstance(child, dict) and child.get("nodeType") == 1 for child in children),
                         "attributes_sha256": R.digest(sorted(normalized.items()))})
        for child in children:
            visit(child, depth + 1)

    if (not isinstance(node, dict) or node.get("localName") != "svg" or
            type(node.get("backendNodeId")) is not int or
            node.get("nodeId") not in {0, root_id} or type(node.get("nodeId")) is not int or
            R.digest({"node_id": root_id, "backend_node_id": node["backendNodeId"]}) != root_metadata["identity_sha256"]):
        raise I.ProtocolError("SVG namespace witness is not bound to this exact selected root")
    visit(node, 1)
    return {"format": DOM_CONTEXT, "namespace": SVG_NS, "source": "DOM.describeNode",
            "root_identity_sha256": root_metadata["identity_sha256"], "elements": elements,
            "elements_sha256": R.digest(elements), "element_count": len(elements), "dom_node_count": total,
            "metadata_tree_sha256": R.digest(node), "complete": True, "element_coverage_only": True,
            "metadata_whitespace_mode": "all_requested", "whitespace_source": "DOM.getOuterHTML"}


def _node_ids(value):
    if (not isinstance(value, list) or len(value) > MAX_MATCHES or
            any(type(node) is not int or not 0 < node < 2**31 for node in value) or len(set(value)) != len(value)):
        raise I.ProtocolError("SVG query returned malformed, repeated or oversized node identities")
    return value


def _document_current(page):
    if any(event.get("method") == "DOM.documentUpdated" for event in page.events):
        raise I.Unavailable("SVG document changed; no stale-document retry or rebootstrap")


def _dom_call(page, method, params):
    _document_current(page)
    result = page.call(method, params)
    _document_current(page)
    return result


def _document(page):
    # Chromium getDocument discards frontend node bindings. Bootstrap exactly
    # once, retaining this ephemeral document for both before/after selections.
    if getattr(page, "svg_document_bootstrapped", False):
        raise I.Unavailable("SVG document was already bootstrapped; no binding reset")
    document = _dom_call(page, "DOM.getDocument", {"depth": 0, "pierce": False}).get("root")
    if (not isinstance(document, dict) or type(document.get("nodeType")) is not int or document["nodeType"] != 9 or
            type(document.get("nodeId")) is not int or not 0 < document["nodeId"] < 2**31):
        raise I.ProtocolError("SVG observation omitted its exact document root")
    page.svg_document_bootstrapped = True
    return document["nodeId"]


def _root(page, selector, document_id):
    selected = _node_ids(_dom_call(page, "DOM.querySelectorAll", {"nodeId": document_id, "selector": selector}).get("nodeIds"))
    if len(selected) > 1:
        raise Ambiguous("SVG content selector matched multiple independent roots")
    if not selected:
        raise I.Unavailable("SVG content root is absent")
    nested = _node_ids(_dom_call(page, "DOM.querySelectorAll", {"nodeId": document_id, "selector": "svg svg"}).get("nodeIds"))
    if selected[0] in nested:
        raise Unsupported("SVG content selector must select an outermost SVG root")
    node = _dom_call(page, "DOM.describeNode", {"nodeId": selected[0], "depth": 0, "pierce": False}).get("node")
    if (not isinstance(node, dict) or type(node.get("nodeType")) is not int or node["nodeType"] != 1 or
            node.get("localName") != "svg" or node.get("nodeName") not in {"svg", "SVG"} or
            node.get("isSVG") is not True or type(node.get("backendNodeId")) is not int or not 0 < node["backendNodeId"] < 2**31 or
            "nodeId" in node and (type(node["nodeId"]) is not int or node["nodeId"] not in {0, selected[0]})):
        raise I.ProtocolError("SVG root metadata is not the exact selected SVG element")
    if any(node.get(key) for key in ("shadowRoots", "contentDocument", "templateContent", "pseudoElements", "distributedNodes", "assignedSlot")):
        raise Unsupported("SVG root has unsupported shadow/frame/template/pseudo content")
    attributes = node.get("attributes")
    count = node.get("childNodeCount")
    if (not isinstance(attributes, list) or len(attributes) % 2 or len(attributes) > MAX_ATTRIBUTES * 2 or
            any(not _text(item, MAX_ATTRIBUTE_BYTES, empty=True) for item in attributes) or
            any(not _text(name, 256) for name in attributes[::2]) or
            len(set(attributes[::2])) != len(attributes) // 2 or
            sum(len(item.encode("utf-8")) for item in attributes) > MAX_ATTRIBUTE_BYTES or
            type(count) is not int or not 0 <= count <= LIMITS["max_nodes"]):
        raise I.ProtocolError("SVG root metadata is incomplete or exceeds its bounded attributes/children")
    public = {"tag": "svg", "is_svg": True, "attribute_count": len(attributes) // 2,
              "attributes_sha256": R.digest(attributes), "child_count": count,
              "identity_sha256": R.digest({"node_id": selected[0], "backend_node_id": node["backendNodeId"]})}
    return selected[0], public


def _frame(page, selected):
    _document_current(page)
    frame = page.call("Page.getFrameTree").get("frameTree", {}).get("frame")
    _document_current(page)
    if not isinstance(frame, dict) or not I._text(frame.get("id"), 128) or I._effective_frame_url(frame) != selected["url"]:
        raise I.Unavailable("SVG observer frame differs from the exact owned app URL")
    return frame


def collect(bundle, schema, out, *, timeout=15, config_file=None, expected_identity=None):
    """Collect one independent observation; no actor, retry, publication or proof."""
    R.assert_guest()  # Before all IO, output directories, connections or helpers.
    expected_identity = B._expected_identity(expected_identity)
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("SVG observer deadline must be an integer from1 to60 seconds")
    started, deadline = time.monotonic(), time.monotonic() + timeout
    receipt = {"format": OBSERVATION, "status": "unknown", "kind": KIND, "target": None,
               "schema_sha256": None, "request_sha256": None, "resources": None, "identity": None,
               "after_identity": None, "endpoint_identity": None, "root_before": None, "root_after": None,
               "expected": None, "values": [], "capture": None, "complete": False, "matches": None,
               "state_uncertain": False, "resource_review_required": False, "retry": False,
               "evidence_only": True, "proof_created": False, "task_result_verified": False,
               "model_calls": 0, "app_evaluations": 0, "method_calls": 0, "getters_invoked": 0,
               "visibility_proved": False, "geometry_proved": False, "connectivity_proved": False,
               "persistence_proved": False, "actuated": False,
               "prebootstrap_document_updates": 0,
               "cleanup": {"page_socket_closed": True, "browser_socket_closed": True,
                           "requested_reads_completed": False, "local_live_ids_discarded": True}}
    if expected_identity is not None:
        receipt["expected_identity"] = dict(expected_identity)
    folder = page = browser = None
    succeeded = False
    try:
        checked = validate(schema)
        receipt["kind"] = checked["kind"]
        receipt["schema_sha256"], receipt["request_sha256"] = R.digest(checked), R.digest(_request(checked))
        app = Path(_path(str(bundle))).resolve(strict=True)
        target = receipt["target"] = R.fingerprint(app)
        if target != checked["target"]:
            raise ValueError("SVG original app fingerprint changed")
        if expected_identity is not None and (expected_identity["bundle"] != str(app) or
                                              expected_identity["bundle_id"] != target["bundle_id"]):
            raise ValueError("SVG expected identity must name this exact app bundle")
        proposed = Path(_path(str(out)))
        if proposed.resolve() != proposed or proposed.is_relative_to(app):
            raise ValueError("SVG receipts must be canonical and outside the app")
        for path in (checked["reference"]["file"], checked["contract"]["file"]):
            if proposed.is_relative_to(Path(path)) or Path(path).is_relative_to(proposed):
                raise ValueError("SVG receipts cannot overlap independent reference/declaration files")
        proposed.mkdir(parents=True, mode=0o700, exist_ok=False)
        folder = proposed
        config, expected, pins = _material(app, checked, config_file)
        if Path(pins["transport"]["config"]["file"]).is_relative_to(folder):
            raise ValueError("SVG receipts cannot contain operator configuration")
        receipt.update(resources=pins, expected=expected)
        main = receipt["identity"] = B.process_identity(app, target["bundle_id"])
        if expected_identity is not None and main != expected_identity:
            raise I.Unavailable("SVG process differs from the caller's expected identity; no successor adoption")
        listener = I._listener(config["endpoint"], main, deadline)
        browser_url, selected = I._select_target(app, config, deadline)
        browser = I._CDP(I._socket(browser_url, deadline), deadline)
        receipt["cleanup"]["browser_socket_closed"] = False
        renderers = I._renderer_processes(browser, app, main, deadline)
        page = I._CDP(I._socket(selected["webSocketDebuggerUrl"], deadline), deadline)
        receipt["cleanup"]["page_socket_closed"] = False
        frame = _frame(page, selected)
        receipt["endpoint_identity"] = {"listener": listener, "renderer_processes": renderers,
                                        "target_type": selected["type"], "target_url": selected["url"],
                                        "target_identity_sha256": R.digest(selected), "frame_sha256": R.digest(frame)}
        # No Runtime domain commands, app evaluation, remote objects or getters.
        mixed = checked["kind"] == MIXED_KIND
        contextual = mixed or checked.get("namespace_mode") == "dom_svg_elements"
        if contextual:
            page.call("DOM.enable", {"includeWhitespace": "all"})
        # Establish a baseline exactly once before acquiring any DOM IDs. Only
        # already-received documentUpdated startup notices may be discarded;
        # updates during the frame recheck/bootstrap or later are fatal.
        receipt["prebootstrap_document_updates"] = sum(event.get("method") == "DOM.documentUpdated" for event in page.events)
        page.events[:] = [event for event in page.events if event.get("method") != "DOM.documentUpdated"]
        if _frame(page, selected) != frame:
            raise I.Unavailable("SVG app frame changed before its sole document bootstrap")
        document_id = _document(page)
        root_id, root_before = _root(page, checked["selector"], document_id)
        receipt["root_before"] = root_before
        namespace_context = None
        if contextual:
            described = _dom_call(page, "DOM.describeNode", {"nodeId": root_id, "depth": -1, "pierce": False}).get("node")
            namespace_context = (_mixed_witness if mixed else _dom_context)(described, root_id, root_before, checked["limits"])
        if (_material(app, checked, pins["transport"]["config"]["file"]) != (config, expected, pins) or
                B.process_identity(app, target["bundle_id"]) != main or R.fingerprint(app) != target or
                I._listener(config["endpoint"], main, deadline) != listener or
                I._select_target(app, config, deadline) != (browser_url, selected) or _frame(page, selected) != frame):
            raise I.Unavailable("SVG app/config/source/listener/frame changed before complete read")
        result = _dom_call(page, "DOM.getOuterHTML", {"nodeId": root_id})
        _keys(result, {"outerHTML"})
        if not mixed:
            capture = _parse_svg(result["outerHTML"], checked["limits"], dom_context=namespace_context)
        elif not _text(result["outerHTML"], checked["limits"]["max_markup_bytes"]):
            raise ValueError("mixed DOM markup must be complete bounded unchanged UTF-8")
        after_id, root_after = _root(page, checked["selector"], document_id)
        receipt["root_after"] = root_after
        if after_id != root_id or root_after != root_before:
            raise I.Unavailable("SVG root was replaced or changed during the independent capture; no retry")
        if contextual:
            described = _dom_call(page, "DOM.describeNode", {"nodeId": root_id, "depth": -1, "pierce": False}).get("node")
            after_context = (_mixed_witness if mixed else _dom_context)(described, root_id, root_after, checked["limits"])
            if after_context != namespace_context:
                raise I.Unavailable("complete SVG DOM namespace metadata changed during the capture; no retry")
            if mixed:
                capture = _mixed_capture(result["outerHTML"], namespace_context, after_context, checked["limits"])
                receipt["mixed_witness_after_sha256"] = R.digest(after_context)
                receipt["mixed_witness_stable"] = True
            else:
                receipt["namespace_context_after_sha256"] = R.digest(after_context)
                receipt["namespace_context_stable"] = True
        receipt["after_identity"] = B.process_identity(app, target["bundle_id"])
        if (receipt["after_identity"] != main or R.fingerprint(app) != target or
                _material(app, checked, pins["transport"]["config"]["file"]) != (config, expected, pins) or
                I._listener(config["endpoint"], main, deadline) != listener or
                I._select_target(app, config, deadline) != (browser_url, selected) or
                _frame(page, selected) != frame or I._renderer_processes(browser, app, main, deadline) != renderers):
            raise I.Unavailable("SVG target/reference/config/source/frame/renderer identity changed during capture")
        receipt.update(values=capture["values"], capture=capture)
        receipt["cleanup"]["requested_reads_completed"] = not page.pending and not browser.pending
        succeeded = receipt["cleanup"]["requested_reads_completed"]
    except Exception as error:
        receipt["status"] = "ambiguous" if isinstance(error, Ambiguous) else "unsupported" if isinstance(error, Unsupported) else "unknown"
        receipt["error"] = {"type": type(error).__name__, "message": str(error)[:1024]}
    finally:
        for label, channel in (("page", page), ("browser", browser)):
            if channel is not None:
                try:
                    channel.close()
                    receipt["cleanup"][label + "_socket_closed"] = True
                except Exception:
                    receipt["resource_review_required"] = True
        receipt["rpc_count"] = sum(channel.next_id for channel in (page, browser) if channel is not None)
        # Only hashes, typed metadata and complete label text survive. No raw
        # markup, WebSocket URL, DOM ID or remote handle is exported, and no
        # remote object group was created.
        for channel in (page, browser):
            if channel is not None:
                channel.events.clear()
                channel.pending.clear()
        if succeeded and not receipt["resource_review_required"]:
            receipt["complete"] = True
            receipt["matches"] = receipt["values"] == receipt["expected"]
            receipt["status"] = "observed" if receipt["matches"] else "refuted"
        elif receipt["resource_review_required"]:
            receipt.update(status="unknown", complete=False, matches=None)
        receipt["elapsed_s"] = round(time.monotonic() - started, 4)
        if folder is not None:
            receipt["receipt"] = str(folder / "observation.json")
            R.write_json(folder / "observation.json", receipt)
    return receipt
