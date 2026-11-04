"""SURFACE DISCOVERY (execution/surface_discovery.py), offline with fakes only.

Pinned here: every parser on a fake input (URL schemes, NSServices, App Intents metadata, SiriKit intent definitions,
scripting commands, Mach-O headers, bundled CLIs, RFC 8631 links, an RFC 9727 catalog, OpenAPI, GraphQL
introspection, the API tier's compiled request, JS endpoint literals, embedded OpenAPI, canvas detection, the OSS
policy and a command registry); the ranker; the read-only bundle reader on a FAKE bundle built in a temp folder
(nothing written into it, no symlink followed out, no include read from outside the system's folder); the site
discovery (a government host refused, nothing fetched); serving a step (declared effect, kinds, every parameter, the
step's identity taken; unknown = uncertain; irreversible never substitutes); the independent read-back and its
verdict; the map integration (surface transitions, the lexicon plan unchanged, prefer_surfaces); invocations and
knowledge entries; the CLI. No host app bundle is read, nothing is launched, no network, no model.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_surface_discovery.py
"""
import json
import os
import pathlib
import plistlib
import struct
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))
os.environ.setdefault("ANYUSE_DATA_DIR", tempfile.mkdtemp(prefix="anyuse-surf-data-"))

from anyuse.execution import map_compile as M  # noqa: E402
from anyuse.execution import surface_discovery as S  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def _prim(n):
    return {"primitive": {"wrapper": {"typeIdentifier": n}}}


def _ent(name):
    return {"entity": {"wrapper": {"typeName": name}}}


def _param(name, vt, optional=False, **kw):
    return {"name": name, "title": {"key": name.title()}, "isOptional": optional, "valueType": vt, **kw}


def _action(aid, title, params, protocols=(), output=None, open_app=False):
    a = {"identifier": aid, "title": {"key": title} if title else None, "parameters": params,
         "systemProtocols": list(protocols), "openAppWhenRun": open_app, "isDiscoverable": True,
         "fullyQualifiedTypeName": f"Fake.{aid}"}
    if output:
        a["outputType"] = _ent(output)
    return a


P = "com.apple.link.systemProtocol."


def fake_actionsdata():
    """A Notes-like and a Finder-like App Intents metadata document in one (the shapes Xcode compiles)."""
    return {
        "actions": {
            "CreateNoteLinkAction": _action("CreateNoteLinkAction", "Create Note",
                                            [_param("name", _prim(12), True), _param("folder", _ent("FolderEntity"), True)],
                                            [P + "CreateEntity"], output="NoteEntity"),
            "OpenNoteLinkAction": _action("OpenNoteLinkAction", "Open Note", [_param("target", _ent("NoteEntity"))],
                                          [P + "OpenEntity"], open_app=True),
            "OpenFolderLinkAction": _action("OpenFolderLinkAction", "Open Folder", [_param("target", _ent("FolderEntity"))],
                                            [P + "OpenEntity"]),
            "ShowQuickNoteIntent": _action("ShowQuickNoteIntent", "Show Quick Note", []),
            "ShowSearchResultsLinkAction": _action("ShowSearchResultsLinkAction", "Show Search Results",
                                                   [_param("criteria", {"searchCriteria": {"wrapper": {"typeIdentifier": 0}}})],
                                                   [P + "ShowInAppStringSearchResults"]),
            "AppendToNoteLinkAction": _action("AppendToNoteLinkAction", "Append to Note",
                                              [_param("entity", _ent("NoteEntity")), _param("text", _prim(12))]),
            "ChangeFolderSettingLinkAction": _action(
                "ChangeFolderSettingLinkAction", "Change Folder View Setting",
                [_param("changeOperation", {"linkEnumeration": {"wrapper": {"identifier": "ChangeOperation"}}}),
                 _param("setting", {"linkEnumeration": {"wrapper": {"identifier": "FolderViewSetting"}}})],
                [P + "ChangeBinarySetting"]),
            "DeleteNotesLinkAction": _action("DeleteNotesLinkAction", "Delete Notes",
                                             [_param("entities", {"array": {"wrapper": {"memberValueType": _ent("NoteEntity")}}})],
                                             [P + "DeleteEntity"]),
            "CreateFolderIntent": _action("CreateFolderIntent", None,
                                          [_param("fileName", _prim(0), True),
                                           _param("target", _ent("NodeEntity"),
                                                  typeSpecificMetadata=["LNValueTypeMetadataKeyFileSupportedTypes",
                                                                        {"array": {"elements": [
                                                                            {"string": {"wrapper": "public.folder"}}]}}])],
                                          [P + "CreateEntity"], output="NodeEntity"),
            "OpenItemIntent": _action("OpenItemIntent", None,
                                      [_param("target", _ent("NodeEntity"),
                                              typeSpecificMetadata=["LNValueTypeMetadataKeyFileSupportedTypes",
                                                                    {"array": {"elements": [
                                                                        {"string": {"wrapper": "public.item"}},
                                                                        {"string": {"wrapper": "public.folder"}}]}}])],
                                      [P + "OpenEntity"]),
            "GoToFolderIntent": _action("GoToFolderIntent", "Go To Folder", []),
            "RenameThingIntent": _action("RenameThingIntent", "Rename Thing", [_param("newName", _prim(0))]),
        },
        "entities": {
            "NoteEntity": {"displayTypeName": {"key": "Note"},
                           "properties": [{"identifier": "name", "title": {"key": "Name"}, "valueType": _prim(12)},
                                          {"identifier": "content", "title": {"key": "Content"}, "valueType": _prim(12)},
                                          {"identifier": "isPinned", "title": {"key": "Pinned"}, "valueType": _prim(1)}]},
            "FolderEntity": {"displayTypeName": {"key": "Folder"}, "properties": []},
            "NodeEntity": {"displayTypeName": {"key": "Item"}, "properties": [],
                           "systemProtocolMetadata": ["com.apple.appintents.entity.PersistentFileIdentifiable", {"empty": {}}],
                           "transferableContentTypes": {"importableTypes": [{"contentType": "public.item"}]}},
        },
        "queries": {
            "VisibleNotesQuery": {"entityType": "NoteEntity", "capabilities": 78,
                                  "parameters": [{"propertyIdentifier": "name", "comparators": [{"comparatorTypeValue": 6}]}]},
            "VisibleFoldersQuery": {"entityType": "FolderEntity", "capabilities": 70, "parameters": []},
        },
        "enums": [{"identifier": "ChangeOperation", "cases": [{"identifier": c} for c in ("disable", "enable", "toggle")]},
                  {"identifier": "FolderViewSetting", "cases": [{"identifier": "galleryView"}, {"identifier": "listView"}]}],
    }


def _by_key(surfaces):
    return {s.key: s for s in surfaces}


# ---------------------------------------------------------------------------
# 1. bundle parsers
# ---------------------------------------------------------------------------

def test_url_schemes_are_partial_generic_ones_marked_and_invalid_ones_dropped():
    info = {"CFBundleURLTypes": [{"CFBundleURLSchemes": ["dict", "x-dictionary"], "CFBundleURLName": "Dict"},
                                 {"CFBundleURLSchemes": ["https", "DICT", "bad scheme", ""]}]}
    out = S.url_scheme_surfaces(info, app="Dictionary", bundle_id="com.apple.Dictionary")
    assert [s.key for s in out] == ["url_scheme:dict", "url_scheme:x-dictionary", "url_scheme:https"]
    assert all(s.interface == "partial" and s.effect["effect"] == "" for s in out)
    assert out[2].invoke.get("generic") is True and not out[0].invoke.get("generic")
    assert out[0].cite == {"file": "Contents/Info.plist", "pointer": "CFBundleURLTypes[0]"}


def test_services_take_their_effect_from_the_declared_title_and_their_input_from_send_types():
    info = {"NSServices": [
        {"NSMenuItem": {"default": "Look Up in Dictionary"}, "NSMessage": "doLookupService",
         "NSSendTypes": ["NSStringPboardType"]},
        {"NSMenuItem": {"default": "Finder/Reveal"}, "NSMessage": "reveal", "NSSendTypes": ["NSFilenamesPboardType"]},
        {"NSMenuItem": {"default": "Finder/Show Info"}, "NSMessage": "showInfo", "NSSendTypes": ["NSFilenamesPboardType"],
         "NSRestricted": True},
        {"NSMenuItem": {"default": "Make Sticky"}, "NSMessage": "makeSticky", "NSSendTypes": ["public.plain-text"]},
        {"NSMessage": ""}]}
    out = _by_key(S.service_surfaces(info, app="X", bundle_id="x"))
    look = out["service:doLookupService"]
    assert (look.effect["effect"], look.params[0]["type"], look.interface) == ("scoped_find", "text", "declared")
    assert (out["service:reveal"].effect["effect"], out["service:reveal"].effect["item_kinds"]) == ("select_item", ["file", "folder"])
    # "Show Info" acts on its own object (the info panel), not on the item: no map effect
    assert out["service:showInfo"].effect["effect"] == "other" and "NSRestricted" in out["service:showInfo"].notes[0]
    assert out["service:makeSticky"].effect["effect"] == "object_create"
    assert len(out) == 4


def test_app_intents_effects_kinds_and_resolution_come_from_the_declared_metadata():
    out = _by_key(S.appintent_surfaces(fake_actionsdata(), app="Notes", bundle_id="com.apple.Notes", sha="abc"))
    cn = out["app_intent:CreateNoteLinkAction"]
    assert (cn.effect["effect"], cn.effect["item_kinds"], cn.effect["source"]) == ("object_create", ["note"], "protocol")
    assert cn.requires == ["shortcut_import"] and cn.invoke["via"] == "shortcuts"
    assert cn.cite == {"file": "Contents/Resources/Metadata.appintents/extract.actionsdata",
                       "pointer": "/actions/CreateNoteLinkAction", "sha256": "abc"}
    on = out["app_intent:OpenNoteLinkAction"]
    assert on.effect["effect"] == "open_item" and on.params[0]["resolve"]["by"] == "name_query"
    assert "opens the app when run" in on.notes
    of = out["app_intent:OpenFolderLinkAction"]
    assert of.effect["effect"] == "navigate" and of.params[0]["resolve"]["by"] == "none"     # a container: navigate
    assert out["app_intent:ShowSearchResultsLinkAction"].effect["effect"] == "scoped_find"
    ap = out["app_intent:AppendToNoteLinkAction"]
    assert (ap.effect["effect"], ap.effect["mode"], ap.effect["source"]) == ("doc_save", "append", "title")
    st = out["app_intent:ChangeFolderSettingLinkAction"]
    assert st.effect["effect"] == "flag_toggle" and st.effect["options"] == ["galleryView", "listView"]
    assert out["app_intent:DeleteNotesLinkAction"].effect["effect"] == "delete"
    # a file-backed entity resolves by path; the identifier's noun names the kind when there is no title
    cf = out["app_intent:CreateFolderIntent"]
    assert cf.effect["item_kinds"] == ["folder"] and cf.label == "Create Folder"
    assert [p["resolve"]["by"] for p in cf.params if p["type"] == "entity"] == ["file_path"]
    oi = out["app_intent:OpenItemIntent"]
    assert oi.effect["effect"] == "open_item" and set(oi.effect["item_kinds"]) == {"file", "document", "folder"}
    assert out["app_intent:GoToFolderIntent"].effect["effect"] == "navigate"
    assert out["app_intent:RenameThingIntent"].effect["effect"] == "" and out["app_intent:RenameThingIntent"].interface == "partial"
    # declared entity properties of a resolvable entity: one read surface per TEXT property
    reads = sorted(k for k in out if k.startswith("app_intent:entity_property:"))
    assert reads == ["app_intent:entity_property:NoteEntity.content", "app_intent:entity_property:NoteEntity.name"]
    assert out[reads[0]].effect["effect"] == "read_content"


def test_sirikit_intent_definitions_skip_internal_intents_and_leave_object_parameters_unresolved():
    doc = {"INIntents": [
        {"INIntentName": "Note", "INIntentTitle": "Show Note", "INIntentVerb": "",
         "INIntentParameters": [{"INIntentParameterName": "note", "INIntentParameterType": "Object",
                                 "INIntentParameterObjectType": "IntentNote"}]},
        {"INIntentName": "Secret", "INIntentTitle": "Do", "INIntentInternal": True}]}
    out = S.intentdefinition_surfaces(doc, app="Notes", bundle_id="com.apple.Notes", cite_file="Base.lproj/Notes.intentdefinition")
    assert [s.key for s in out] == ["app_intent:intentdefinition:Note"]
    assert out[0].effect["effect"] == "open_item" and out[0].params[0]["resolve"]["by"] == "none"
    assert out[0].invoke["legacy"] == "sirikit"


def test_scripting_commands_take_the_fixed_event_code_effect_table():
    cmds = [{"suite": "Standard", "name": "make", "code": "corecrel", "direct_parameter": None,
             "params": [{"name": "new", "code": "kocl", "type": "type", "optional": False}], "result": "specifier"},
            {"suite": "Standard", "name": "delete", "code": "coredelo", "direct_parameter": {"type": "specifier"}, "params": []},
            {"suite": "X", "name": "frob", "code": "xxxxfrob", "params": []},
            {"suite": "X", "name": "hidden", "code": "xxxxhide", "params": [], "hidden": True},
            {"_missing_includes": ["file:///elsewhere.sdef"]}]
    out = _by_key(S.scripting_surfaces(cmds, app="Notes", bundle_id="com.apple.Notes", cite_file="Contents/Resources/Notes.sdef"))
    assert sorted(out) == ["scripting:corecrel", "scripting:coredelo", "scripting:xxxxfrob"]
    assert out["scripting:corecrel"].effect["declared"] == "create" and out["scripting:corecrel"].requires == ["automation_grant"]
    assert out["scripting:coredelo"].effect["effect"] == "delete"
    assert out["scripting:xxxxfrob"].effect["declared"] == "undeclared"


def _macho(filetype, magic=0xFEEDFACF):
    return struct.pack("<IiiI", magic, 0x0100000C, 0, filetype) + b"\0" * 16


def test_macho_header_tells_executables_from_libraries_including_universal_binaries():
    assert S.macho_filetype(_macho(2)) == "execute"
    assert S.macho_filetype(_macho(6)) == "dylib"
    assert S.macho_filetype(_macho(8)) == "bundle"
    assert S.macho_filetype(b"#!/bin/sh\necho hi\n") == "script"
    assert S.macho_filetype(b"PK\x03\x04" + b"\0" * 28) == ""
    thin = _macho(2)
    fat = struct.pack(">II", 0xCAFEBABE, 1) + struct.pack(">IIIII", 0x0100000C, 0, 4096, len(thin), 14) + b"\0" * 4
    blob = fat + b"\0" * (4096 - len(fat)) + thin
    assert S.macho_filetype(blob[:32], lambda off, n: blob[off:off + n]) == "execute"
    assert S.macho_filetype(blob[:32]) == "fat"


def test_bundled_clis_exclude_the_main_executable_libraries_nested_bundles_and_non_executables():
    listing = [{"path": "Contents/MacOS/Spotify", "executable": True, "filetype": "execute"},
               {"path": "Contents/MacOS/spotify_cli", "executable": True, "filetype": "execute"},
               {"path": "Contents/Resources/tool.sh", "executable": True, "filetype": "script"},
               {"path": "Contents/Resources/libx.dylib", "executable": True, "filetype": "dylib"},
               {"path": "Contents/Resources/readme", "executable": False, "filetype": ""},
               {"path": "Contents/Helpers/Helper.app/Contents/MacOS/Helper", "executable": True, "filetype": "execute"},
               {"path": "Contents/Resources/data.bin", "executable": True, "filetype": ""}]
    out = S.cli_surfaces(listing, main_executable="Spotify", app="Spotify", bundle_id="com.spotify.client")
    assert [s.key for s in out] == ["bundled_cli:Contents/MacOS/spotify_cli", "bundled_cli:Contents/Resources/tool.sh"]
    assert all(s.interface == "unknown" for s in out)


# ---------------------------------------------------------------------------
# 2. web parsers
# ---------------------------------------------------------------------------

def test_service_links_from_html_and_the_link_header():
    html = '<head><link rel="service-desc" href="/openapi.json" type="application/openapi+json">' \
           '<link rel="stylesheet" href="/a.css"><link rel="service-doc api-catalog" href="https://x.example/docs"></head>'
    hdr = '</.well-known/api-catalog>; rel="api-catalog", <https://x.example/o.json>; rel=service-desc; type="application/json"'
    out = S.service_links(html, hdr, base="https://x.example/")
    assert [(l["rel"], l["href"], l["from"]) for l in out] == [
        ("service-desc", "https://x.example/openapi.json", "html"), ("service-doc", "https://x.example/docs", "html"),
        ("api-catalog", "https://x.example/docs", "html"),
        ("api-catalog", "https://x.example/.well-known/api-catalog", "link_header"),
        ("service-desc", "https://x.example/o.json", "link_header")]


def test_api_catalog_openapi_and_graphql_operations_with_declared_effects():
    cat = {"linkset": [{"anchor": "https://x.example/api/v1", "service-desc": [{"href": "https://x.example/openapi.json"}],
                        "service-doc": [{"href": "https://x.example/docs"}]}, {"anchor": "empty"}]}
    c = S.api_catalog_surfaces(cat, site="x.example", cite_file="api-catalog.json")
    assert len(c) == 1 and c[0].invoke["service_desc"] == ["https://x.example/openapi.json"]
    spec = {"openapi": "3.0.0", "servers": [{"url": "https://x.example/api"}],
            "components": {"securitySchemes": {"k": {"type": "apiKey"}}},
            "paths": {"/items": {"get": {"operationId": "searchItems", "parameters": [
                {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}]},
                "post": {"summary": "Create item", "requestBody": {"required": True}}},
                "/items/{id}": {"parameters": [{"name": "id", "in": "path", "required": True}],
                                "get": {}, "delete": {}}}}
    ops = _by_key(S.openapi_surfaces(spec, site="x.example", cite_file="openapi.json"))
    assert ops["openapi:GET /items"].effect["declared"] == "search"
    assert ops["openapi:POST /items"].effect["declared"] == "write"
    assert ops["openapi:DELETE /items/{id}"].effect["declared"] == "delete"
    assert ops["openapi:GET /items/{id}"].effect["declared"] == "read"
    assert ops["openapi:GET /items/{id}"].params[0] == {"name": "id", "in": "path", "type": "any", "optional": False}
    assert ops["openapi:GET /items"].cite["pointer"] == "/paths/~1items/get"
    assert "security scheme" in ops["openapi:GET /items"].notes[0]
    assert S.openapi_surfaces({"paths": {}}) == []                       # not an OpenAPI document
    intro = {"data": {"__schema": {"queryType": {"name": "Q"}, "mutationType": {"name": "Mu"},
                                   "types": [{"name": "Q", "fields": [{"name": "search", "args": [
                                       {"name": "q", "type": {"kind": "NON_NULL", "ofType": {"name": "String"}}}]}]},
                                             {"name": "Mu", "fields": [{"name": "addItem", "args": []}]}]}}}
    g = _by_key(S.graphql_surfaces(intro, site="x.example"))
    assert g["graphql:query.search"].effect["declared"] == "read" and g["graphql:query.search"].params[0]["optional"] is False
    assert g["graphql:mutation.addItem"].effect["declared"] == "write" and "never compiled" in g["graphql:mutation.addItem"].notes[0]


def test_the_api_tiers_compiled_request_and_js_endpoint_literals():
    comp = {"status": "ok", "template": {"method": "GET", "url": "https://crates.io/api/v1/crates?q={q}"},
            "param": {"name": "q", "value": "serde"}}
    w = S.compiled_api_surfaces(comp, site="crates.io", cite_file="compiled_api.json")
    assert w[0].kind == "web_api" and w[0].params == [{"name": "q", "type": "text", "optional": False}]
    js = ('// "/api/commented/out"\nconst a = "/api/v2/search?q={q}"; fetch(\'/graphql\');'
          'const t = "user text /api/no"; const u = "https://api.example.com/v1/items";')
    out = S.js_endpoint_surfaces({"bundle.js": js, "data.json": '{"u": "/api/json-data"}'}, site="example.com")
    assert [s.label for s in out] == ["/api/v2/search?q={q}", "/graphql", "https://api.example.com/v1/items"]
    assert all(s.interface == "unknown" for s in out)
    page = '<script type="application/json">{"openapi": "3.1.0", "paths": {"/a": {"get": {}}}}</script>' \
           '<script>var x = {"openapi": "3.0"};</script>'
    assert [d["openapi"] for d in S.embedded_openapi(page)] == ["3.1.0"]


def test_a_canvas_app_is_detected_and_steered_and_a_normal_page_is_not():
    canvas = '<body><div class="toolbar"><button>Move</button><button>Frame</button></div>' \
             '<canvas width="1440" height="820"></canvas></body>'
    p = S.canvas_profile(canvas)
    assert p["canvas_app"] and p["canvases"] == 1 and p["canvas_area_share"] > 0.5 and "last resort" in p["steer"]
    page = "<body>" + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(80)) + '<canvas width="100" height="50"></canvas></body>'
    assert not S.canvas_profile(page)["canvas_app"]
    assert S.canvas_profile(dom={"canvas_area_share": 0.9, "interactive": 300})["canvas_app"]
    assert not S.canvas_profile("<body><p>text</p></body>")["canvas_app"]


def test_oss_policy_needs_an_osi_license_and_a_local_checkout_and_registries_are_facts_only():
    assert S.oss_policy({"license": "MIT", "path": "/src/app"})["allowed"]
    assert not S.oss_policy({"license": "Proprietary", "path": "/src/app"})["allowed"]
    assert not S.oss_policy({"license": "", "path": "/src/app"})["allowed"]
    d = S.oss_policy({"license": "Apache-2.0", "url": "https://github.com/x/y"})
    assert not d["allowed"] and d["deferred"] and "offline" in d["reason"]
    pkg = {"contributes": {"commands": [{"command": "app.newFile", "title": "New File"},
                                        {"command": "app.toggle", "title": {"key": "Toggle Panel"}}, {"title": "no id"}],
                           "keybindings": [{"command": "app.newFile", "mac": "cmd+n"}]}}
    out = S.vscode_contributes_surfaces(pkg, app="Editor", license="MIT")
    assert [(s.key, s.invoke["keybinding"], s.effect["declared"]) for s in out] == [
        ("oss_registry:app.newFile", "cmd+n", "create"), ("oss_registry:app.toggle", "", "setting")]
    assert out[0].notes == ["license MIT"] and out[0].interface == "partial"


# ---------------------------------------------------------------------------
# 3. the ranker
# ---------------------------------------------------------------------------

def test_rank_orders_by_tier_then_strength_then_grants_then_kind():
    mk = lambda kind, key, src="protocol", iface="declared", eff="open_item", req=(): S.Surface(  # noqa: E731
        kind=kind, key=key, label=key, effect={"effect": eff, "source": src}, interface=iface, requires=list(req))
    surfaces = [mk("ax", "ax:1"), mk("url_scheme", "u", "none", "partial", ""), mk("app_intent", "del", eff="delete"),
                mk("scripting", "s", "declared", req=("automation_grant",)), mk("app_intent", "ai", req=("shortcut_import",)),
                mk("service", "svc", "title"), mk("openapi", "o", "declared"), mk("vision", "v", "none", "partial", "")]
    assert [s.key for s in S.rank(surfaces)] == ["o", "ai", "s", "svc", "u", "del", "ax:1", "v"]
    rep = S.ladder_report(surfaces, target="t")
    assert rep["best_by_effect"]["open_item"]["key"] == "o" and rep["counts"]["app_intent"] == 2
    assert S.ladder_report([mk("ax", "a")])["steer"][0].startswith("no declared surface above AX/DOM")


# ---------------------------------------------------------------------------
# 4. the bundle reader on a FAKE bundle
# ---------------------------------------------------------------------------

def _fake_bundle(root):
    b = root / "Fake.app"
    (b / "Contents/MacOS").mkdir(parents=True)
    (b / "Contents/Resources/Metadata.appintents").mkdir(parents=True)
    (b / "Contents/Resources/Base.lproj").mkdir(parents=True)
    (b / "Contents/Helpers").mkdir(parents=True)
    (b / "Contents/PlugIns/W.appex/Contents/Resources/Metadata.appintents").mkdir(parents=True)
    info = {"CFBundleIdentifier": "com.example.fake", "CFBundleName": "Fake", "CFBundleExecutable": "Fake",
            "CFBundleShortVersionString": "1.2", "OSAScriptingDefinition": "Fake.sdef", "NSAppleScriptEnabled": True,
            "CFBundleURLTypes": [{"CFBundleURLSchemes": ["fake"]}],
            "NSServices": [{"NSMenuItem": {"default": "Look Up in Fake"}, "NSMessage": "look", "NSSendTypes": ["NSStringPboardType"]}]}
    (b / "Contents/Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    (b / "Contents/Resources/Metadata.appintents/extract.actionsdata").write_text(json.dumps(fake_actionsdata()))
    (b / "Contents/PlugIns/W.appex/Contents/Resources/Metadata.appintents/extract.actionsdata").write_text(
        json.dumps({"actions": {"OpenWidgetIntent": _action("OpenWidgetIntent", "Open Widget", [])}}))
    (b / "Contents/Resources/Base.lproj/Fake.intentdefinition").write_bytes(plistlib.dumps(
        {"INIntents": [{"INIntentName": "Show", "INIntentTitle": "Show Thing", "INIntentParameters": []}]}))
    outside = root / "outside.sdef"
    outside.write_text('<dictionary><suite name="S" code="SSSS"><command name="evil" code="eeeeevil"/></suite></dictionary>')
    (b / "Contents/Resources/Fake.sdef").write_text(
        '<?xml version="1.0"?><!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd">'
        '<dictionary xmlns:xi="http://www.w3.org/2003/XInclude">'
        f'<xi:include href="file://{outside}" xpointer="xpointer(/dictionary/suite)"/>'
        '<suite name="Fake" code="Fake"><command name="make" code="corecrel"/><command name="count" code="corecnte"/></suite>'
        '</dictionary>')
    (b / "Contents/MacOS/Fake").write_bytes(_macho(2))
    (b / "Contents/MacOS/fake-cli").write_bytes(_macho(2))
    (b / "Contents/Helpers/libz.dylib").write_bytes(_macho(6))
    (b / "Contents/Resources/run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    for p in ("Contents/MacOS/Fake", "Contents/MacOS/fake-cli", "Contents/Helpers/libz.dylib", "Contents/Resources/run.sh"):
        os.chmod(b / p, 0o755)
    os.symlink(outside, b / "Contents/MacOS/linked-out")
    return b


def _snapshot(path):
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in pathlib.Path(path).rglob("*") if not p.is_symlink())


def test_the_bundle_reader_lists_every_declared_surface_and_writes_nothing(tmp_path):
    b = _fake_bundle(tmp_path)
    before = _snapshot(b)
    rep = S.discover_bundle(str(b))
    assert _snapshot(b) == before                                           # nothing written, nothing touched
    assert (rep.app, rep.bundle_id, rep.version, rep.scripting) == ("Fake", "com.example.fake", "1.2", "sdef")
    k = rep.kinds()
    assert k["url_scheme"] == 1 and k["service"] == 1 and k["bundled_cli"] == 2 and k["scripting"] == 2
    keys = {s.key for s in rep.surfaces}
    assert {"app_intent:OpenWidgetIntent", "app_intent:intentdefinition:Show", "app_intent:OpenNoteLinkAction",
            "bundled_cli:Contents/MacOS/fake-cli", "bundled_cli:Contents/Resources/run.sh"} <= keys
    # the include outside the system's ScriptingDefinitions folder is never read; the symlink out is not followed
    assert "scripting:eeeeevil" not in keys and not any("outside" in f for f in rep.files_read)
    assert not any("linked-out" in s.key for s in rep.surfaces)
    assert "Contents/PlugIns/W.appex/Contents/Resources/Metadata.appintents/extract.actionsdata" in rep.files_read
    d = rep.to_dict()
    assert d["surfaces"][0]["tier"] == 2 and json.dumps(d)


def test_a_bundle_without_an_info_plist_and_a_legacy_suite_without_scripting_enabled(tmp_path):
    b = tmp_path / "Empty.app"
    (b / "Contents").mkdir(parents=True)
    assert S.discover_bundle(str(b)).notes == ["no readable Info.plist"]
    c = tmp_path / "Lex.app"
    (c / "Contents/Resources").mkdir(parents=True)
    (c / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "x.lex"}))
    (c / "Contents/Resources/Browser.scriptSuite").write_text("{}")
    r = S.discover_bundle(str(c))
    assert r.scripting == "" and "without NSAppleScriptEnabled" in r.notes[0]


def test_a_dictionary_whose_doctype_has_an_internal_subset_is_read(tmp_path):
    b = tmp_path / "Doc.app"
    (b / "Contents/Resources").mkdir(parents=True)
    (b / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "x.doc", "OSAScriptingDefinition": "D.sdef"}))
    (b / "Contents/Resources/D.sdef").write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE dictionary SYSTEM "file://localhost/System/Library/DTDs/sdef.dtd"\n[\n'
        '  <!ELEMENT class (cocoa?, type?)>\n  <!ENTITY % common.attrib\n    "xml:base CDATA #IMPLIED">\n]>\n'
        '<dictionary><suite name="D" code="DDDD"><command name="make" code="corecrel"/></suite></dictionary>')
    r = S.discover_bundle(str(b))
    assert r.scripting == "sdef" and [s.key for s in r.surfaces] == ["scripting:corecrel"]
    assert S.strip_doctype('<!DOCTYPE a SYSTEM "x"><a/>') == "<a/>"


def test_find_and_list_bundles_in_given_roots(tmp_path):
    (tmp_path / "A.app").mkdir()
    (tmp_path / "Vendor" / "B.app").mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("x")
    assert S.list_bundles([str(tmp_path)]) == [str(tmp_path / "A.app"), str(tmp_path / "Vendor" / "B.app")]
    assert S.find_bundle("b", [str(tmp_path)]) == str(tmp_path / "Vendor" / "B.app")
    assert S.find_bundle("Nope", [str(tmp_path)]) is None


# ---------------------------------------------------------------------------
# 5. sites (offline)
# ---------------------------------------------------------------------------

def test_site_discovery_refuses_government_hosts_and_fetches_nothing(monkeypatch):
    # off by default: a government host is mapped like any site
    assert not S.discover_site("service.gov.uk").get("refused")
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    r = S.discover_site("https://www.bundestag.de/x", {"p.html": "<canvas></canvas>"})
    assert r["refused"] and r["surfaces"] == []
    r2 = S.discover_site("service.gov.uk")
    assert r2["refused"]


def test_site_discovery_ranks_official_specs_over_own_requests_over_dom_and_steers_a_canvas_app():
    spec = {"openapi": "3.0.0", "paths": {"/v1/files/{key}": {"get": {"operationId": "getFile"}}}}
    arts = {"index.html": '<html><head><link rel="service-desc" href="/openapi.json"></head><body>'
                          '<button>Share</button><canvas style="width:100vw;height:100vh"></canvas>'
                          '<script>fetch("/api/v1/files/abc")</script></body></html>',
            "openapi.json": json.dumps(spec), "app.js": 'x("/api/v1/me")', "broken.json": "{"}
    rep = S.discover_site("canvas.example", arts)
    kinds = [s["kind"] for s in rep["surfaces"]]
    assert kinds[0] in ("openapi", "api_catalog") and kinds[-1] == "vision"
    assert kinds.index("js_endpoint") < kinds.index("dom") < kinds.index("vision")
    assert rep["canvas"]["canvas_app"] and any("canvas app" in s for s in rep["steer"])
    dom = next(s for s in rep["surfaces"] if s["kind"] == "dom")
    assert "chrome only" in dom["notes"][0]
    assert rep["deferred_probes"][0]["url"] == "https://canvas.example/.well-known/api-catalog"
    assert "broken.json: not JSON" in rep["notes"]


# ---------------------------------------------------------------------------
# 6. serving a step; the read-back
# ---------------------------------------------------------------------------

SURF = S.appintent_surfaces(fake_actionsdata(), app="Notes", bundle_id="com.apple.Notes")
SVC = S.service_surfaces({"NSServices": [{"NSMenuItem": {"default": "Look Up in Dictionary"}, "NSMessage": "look",
                                          "NSSendTypes": ["NSStringPboardType"]}]}, app="Dictionary", bundle_id="d")


def _serving(need, surfaces=SURF):
    return {s.key: S.serves(s, need)[0] for s in surfaces if S.serves(s, need)[0] != "no"}


def test_open_a_named_note_is_served_by_the_declared_open_with_a_name_query_only():
    need = S.Need(app="Notes", effect="open_item", item_kind="note", values={"name": "Einkauf Samstag"})
    assert _serving(need) == {"app_intent:OpenNoteLinkAction": "served"}       # not the quick note (no identity)
    st, why, binds = S.serves(S.Surface.from_dict(_by_key(SURF)["app_intent:OpenNoteLinkAction"].to_dict()), need)
    assert st == "served" and binds == {"target": "name"}
    assert S.serves(_by_key(SURF)["app_intent:OpenNoteLinkAction"], S.Need(app="Notes", effect="open_item",
                                                                            item_kind="note"))[0] == "no"


def test_a_folder_without_a_declared_resolution_is_uncertain_and_a_delete_never_substitutes():
    nav = S.Need(app="Notes", effect="navigate", item_kind="folder", values={"name": "Rezepte"})
    assert _serving(nav) == {"app_intent:OpenFolderLinkAction": "uncertain"}
    sel = S.Need(app="Notes", effect="select_item", item_kind="note", values={"name": "x"})
    assert _serving(sel) == {}
    for s in SURF:
        if s.effect.get("effect") == "delete":
            assert S.serves(s, nav)[0] == "no" and S.serves(s, sel)[0] == "no"


def test_search_view_option_append_and_read_needs():
    find = S.Need(app="Notes", effect="scoped_find", values={"lookup": "Zahnarzt"})
    assert _serving(find) == {"app_intent:ShowSearchResultsLinkAction": "served"}
    assert _serving(S.Need(app="Notes", effect="scoped_find")) == {}                  # no term: the criteria cannot bind
    view = S.Need(app="Notes", effect="toggle_select", option="Als Liste")
    st, why, binds = S.serves(_by_key(SURF)["app_intent:ChangeFolderSettingLinkAction"], view)
    assert st == "uncertain" and binds == {"setting": "listView"} and "exclusivity" in why
    assert S.serves(_by_key(SURF)["app_intent:ChangeFolderSettingLinkAction"],
                    S.Need(app="Notes", effect="toggle_select", option="Änderungsdatum"))[0] == "no"
    app_ = S.Need(app="Notes", effect="doc_save", item_kind="note", mode="append", values={"name": "N", "text": "t"})
    assert _serving(app_) == {"app_intent:AppendToNoteLinkAction": "served"}
    assert _serving(S.Need(app="Notes", effect="doc_save", item_kind="note", mode="edit", values={"name": "N", "text": "t"})) == {}
    read = S.Need(app="Notes", effect="read_content", item_kind="note", values={"name": "N"})
    assert set(_serving(read)) == {"app_intent:entity_property:NoteEntity.content", "app_intent:entity_property:NoteEntity.name"}
    assert _serving(S.Need(app="Dictionary", effect="scoped_find", values={"lookup": "Weltschmerz"}), SVC) == {"service:look": "served"}


def test_file_entities_need_a_path_and_object_create_needs_name_and_container():
    o = S.Need(app="Finder", effect="open_item", item_kind="file", values={"name": "a.txt", "path": "/s/a.txt"})
    assert _serving(o) == {"app_intent:OpenItemIntent": "served"}
    assert _serving(S.Need(app="Finder", effect="open_item", item_kind="file", values={"name": "a.txt"})) == {}
    mk = S.Need(app="Finder", effect="object_create", item_kind="folder", values={"name": "Neu", "path": "/s"})
    assert _serving(mk) == {"app_intent:CreateFolderIntent": "served"}
    assert S.serves(_by_key(SURF)["app_intent:CreateFolderIntent"], mk)[2] == {"fileName": "name", "target": "path"}
    # Notes' create note takes a name but no resolvable container: without a container value it serves
    cn = S.Need(app="Notes", effect="object_create", item_kind="note", values={"name": "Neu"})
    assert _serving(cn) == {"app_intent:CreateNoteLinkAction": "served"}
    # a "go to folder" with no target cannot name the step's folder
    assert "app_intent:GoToFolderIntent" not in _serving(S.Need(app="Finder", effect="navigate", item_kind="folder",
                                                                values={"path": "/s/x"}))


def test_url_schemes_and_unknown_clis_never_serve():
    u = S.url_scheme_surfaces({"CFBundleURLTypes": [{"CFBundleURLSchemes": ["dict", "https"]}]}, app="Dictionary")
    need = S.Need(app="Dictionary", effect="scoped_find", values={"lookup": "x"})
    assert [S.serves(s, need)[0] for s in u] == ["uncertain", "no"]
    cli = S.cli_surfaces([{"path": "Contents/MacOS/t", "executable": True, "filetype": "execute"}])
    assert S.serves(cli[0], need)[0] == "no"


def test_need_from_a_recorded_step_reads_only_what_the_record_and_context_hold():
    step = {"step": {"effect": "open_item", "app": "Finder", "target": "a.txt"}, "resolved": {"label": "a.txt"}}
    n = S.need_from_record(step, {"finder_folder": "/s"})
    assert (n.item_kind, n.values) == ("file", {"name": "a.txt", "path": "/s/a.txt"})
    assert S.need_from_record(step, {}).values == {"name": "a.txt"}
    nav = {"step": {"effect": "navigate", "app": "Finder", "target": "x"}, "check": "container verified: /s/x"}
    assert S.need_from_record(nav, {"finder_folder": "/s"}).values["path"] == "/s/x"
    mk = {"step": {"effect": "object_create", "app": "Finder", "target": "folder", "value": "Client copy"}}
    assert S.need_from_record(mk, {"finder_folder": "/s"}).values == {"name": "Client copy", "path": "/s"}
    rd = {"step": {"effect": "read_content", "app": "Notes", "target": "note", "value": "v"}}
    assert S.need_from_record(rd, {"note_name": "N"}).values == {"name": "N"} and S.need_from_record(rd, {}).values == {}
    tg = {"step": {"effect": "toggle_select", "app": "Notes", "target": "Darstellung", "value": "Als Liste"}}
    assert S.need_from_record(tg).option == "Als Liste"


def test_the_readback_is_independent_of_the_actor_and_unknown_is_uncertain():
    on = _by_key(SURF)["app_intent:OpenNoteLinkAction"]
    need = S.Need(app="Notes", effect="open_item", item_kind="note", values={"name": "N"})
    spec = S.readback_for(on, need)
    assert spec["readback"]["via"] == "ax" and spec["expect"]["values"] == {"name": "N"}
    sc = S.Surface(kind="scripting", key="s", label="")
    assert S.readback_for(sc, need, available=("scripting", "ax"))["readback"]["via"] == "ax"
    assert S.readback_for(sc, need, available=("scripting",))["readback"]["via"] == "none"
    assert S.judge_readback(spec, "app_intent", {"source": "ax", "values": {"name": "N"}})["verdict"] == "verified"
    assert S.judge_readback(spec, "app_intent", {"source": "ax", "values": {"name": "M"}})["verdict"] == "refuted"
    assert S.judge_readback(spec, "app_intent", {"source": "app_intent", "values": {"name": "N"}})["verdict"] == "uncertain"
    assert S.judge_readback(spec, "app_intent", {"source": "ax", "values": {}})["verdict"] == "uncertain"
    assert S.judge_readback(spec, "app_intent", None)["verdict"] == "uncertain"
    none = S.readback_for(sc, need, available=())
    assert S.judge_readback(none, "scripting", {"source": "ax", "values": {"name": "N"}})["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# 7. the map
# ---------------------------------------------------------------------------

def _rt(effect, app, target="", value="", resolved=None):
    return {"step": {"effect": effect, "app": app, "target": target, "value": value, "scope": "", "mode": "",
                     "find": "", "target_ui": ""}, "resolved": resolved or {}}


def _notes_map():
    routine = [_rt("launch_app", "Notes", resolved={"op": "launch", "bundle": "com.apple.Notes"}),
               _rt("navigate", "Notes", "Einkauf_Listen", resolved={"op": "navigate", "label": "Einkauf_Listen", "via": None}),
               _rt("open_item", "Notes", "Einkauf Samstag", resolved={"op": "open", "label": "Einkauf Samstag"})]
    return M.build_desktop_map(["Notes"], routines=[(routine, "open a note", "fx:notes")])


def test_surface_transitions_carry_the_surface_and_the_lexicon_plan_is_unchanged():
    ts = S.surface_transitions(SURF, app="Notes", bundle_id="com.apple.Notes")
    on = next(t for t in ts if t.id == "com.apple.Notes:surface:app_intent:OpenNoteLinkAction")
    assert (on.effect, on.item_kind, on.ax, on.routine) == ("open_item", "note", None, None)
    assert on.sources[0]["kind"] == "surface" and on.sources[0]["surface"] == "app_intent" and on.sources[0]["tier"] == 2
    assert S.surface_of(on).key == "app_intent:OpenNoteLinkAction" and S.surface_of(M.Transition("x", "a", "b", "ax", "i")) is None
    assert not any(t.effect == "delete" for t in ts)
    task = "Öffne die Notiz 'Einkauf Samstag'."
    plain = M.plan(task, _notes_map(), caller_values={"note": "Einkauf Samstag"})
    amap = _notes_map()
    n = S.attach_surfaces(amap, SURF, app="Notes", bundle_id="com.apple.Notes")
    assert n == len(ts) and any(s.get("kind") == "surfaces" for s in amap.sources)
    with_s = M.plan(task, amap, caller_values={"note": "Einkauf Samstag"})
    assert [s.transition for s in with_s.steps] == [s.transition for s in plain.steps]
    assert M.AppMap.from_dict(json.loads(json.dumps(amap.to_dict()))).digest() == amap.digest()


def test_prefer_surfaces_upgrades_a_step_a_surface_serves_and_keeps_ax_otherwise():
    amap = _notes_map()
    S.attach_surfaces(amap, SURF, app="Notes", bundle_id="com.apple.Notes")
    p = M.Plan(task="open the note", apps=["Notes"])
    for tid, b in (("com.apple.Notes:launch_app", {}), ("com.apple.Notes:navigate:folder", {"folder": "Einkauf_Listen"}),
                   ("com.apple.Notes:open_item:note", {"note": "Einkauf Samstag"})):
        t = amap.get(tid)
        p.steps.append(M.PlanStep(tid, t.effect, t.app, b, "test", t.sources, "", t.intent, 0))
    out = S.prefer_surfaces(p, amap)
    assert [o["chosen"]["key"] if o["chosen"] else None for o in out] == [None, None, "app_intent:OpenNoteLinkAction"]
    assert out[1]["uncertain"][0]["key"] == "app_intent:OpenFolderLinkAction"           # no declared folder resolution
    ch = out[2]["chosen"]
    assert ch["readback"]["via"] == "ax" and ch["expect"]["values"] == {"name": "Einkauf Samstag"} and out[2]["fallback"] == "ax"
    assert ch["requires"] == ["shortcut_import"]
    # the AppleScript route, when it answers, serves the navigation (tier 2 scripting) with its own read-back
    out2 = S.prefer_surfaces(p, amap, script_rung=lambda st, need: (st.effect == "navigate", "show_folder"))
    assert out2[1]["chosen"]["key"] == "applescript_route:show_folder" and out2[1]["chosen"]["readback"]["via"] == "ax"
    # an unbound content reference is no value: the step stays on AX
    q = M.Plan(task="open some note", apps=["Notes"])
    t = amap.get("com.apple.Notes:open_item:note")
    q.steps.append(M.PlanStep(t.id, t.effect, t.app, {"note": "${note}"}, "test", t.sources, "", t.intent, 0))
    assert S.prefer_surfaces(q, amap)[0]["chosen"] is None
    assert S.need_from_plan_step(q.steps[0], amap).values == {}


# ---------------------------------------------------------------------------
# 8. invocations, knowledge entries
# ---------------------------------------------------------------------------

def test_invocations_are_built_never_run_and_a_scheme_without_a_template_is_refused():
    on = _by_key(SURF)["app_intent:OpenNoteLinkAction"]
    inv = S.invocation(on, {"target": "N"})
    assert inv["argv"][:3] == ["/usr/bin/shortcuts", "run", "AnyUse com.apple.Notes OpenNoteLinkAction"]
    u = S.url_scheme_surfaces({"CFBundleURLTypes": [{"CFBundleURLSchemes": ["dict"]}]})[0]
    assert "refused" in S.invocation(u, {"word": "x"})
    assert S.invocation(u, {"word": "Welt schmerz/&?"}, url_template="dict://{word}")["url"] == "dict://Welt%20schmerz%2F%26%3F"
    assert "refused" in S.invocation(u, {"word": "x"}, url_template="https://evil/{word}")
    assert "refused" in S.invocation(u, {}, url_template="dict://{word}")
    assert S.invocation(SVC[0], {"input": "Weltschmerz"})["call"] == "NSPerformService"
    assert "refused" in S.invocation(S.Surface(kind="ax", key="a", label=""), {})


def test_knowledge_entries_stay_hypotheses_until_verified():
    doc = {"format": S.KNOWLEDGE_FORMAT, "entries": [
        {"bundle_id": "com.apple.Dictionary", "key": "url_scheme:dict", "template": "dict://{lookup}",
         "effect": "scoped_find", "params": [{"name": "lookup", "type": "string", "optional": False}],
         "cite": "hypothesis for the live check", "verified": False}]}
    u = S.url_scheme_surfaces({"CFBundleURLTypes": [{"CFBundleURLSchemes": ["dict"]}]}, app="Dictionary",
                              bundle_id="com.apple.Dictionary")
    hyp = S.apply_knowledge(u, S.load_knowledge(doc))[0]
    need = S.Need(app="Dictionary", effect="scoped_find", values={"lookup": "x"})
    assert hyp.interface == "partial" and S.serves(hyp, need)[0] == "uncertain" and u[0].invoke.get("template") is None
    doc["entries"][0]["verified"] = True
    ok = S.apply_knowledge(u, S.load_knowledge(doc))[0]
    assert S.serves(ok, need)[0] == "served" and S.invocation(ok, {"lookup": "x"}, url_template=ok.invoke["template"])["url"] == "dict://x"
    with pytest.raises(ValueError):
        S.load_knowledge({"format": "other"})


def test_the_module_spawns_nothing_and_imports_no_desktop_library():
    src = (_HERE / "execution" / "surface_discovery.py").read_text()
    for banned in ("import subprocess", "os.system", "Popen", "from AppKit", "import AppKit", "import objc", "urlopen",
                   "import socket", "requests."):
        assert banned not in src, banned


# ---------------------------------------------------------------------------
# 9. the CLI
# ---------------------------------------------------------------------------

def test_cli_map_surfaces_on_a_fake_bundle_and_offline_site_artifacts(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.cli import cli
    b = _fake_bundle(tmp_path)
    r = CliRunner().invoke(cli, ["map", "surfaces", "Fake", "--bundle", str(b), "--limit", "5"], obj={})
    assert r.exit_code == 0, r.output
    assert "Surfaces of Fake (com.example.fake)" in r.output and "[2] app_intent" in r.output and "more (--limit 0" in r.output
    r2 = CliRunner().invoke(cli, ["map", "surfaces", "Fake", "--bundle", str(b), "--json"], obj={})
    d = json.loads(r2.output)
    assert d["bundle"]["version"] == "1.2" and d["counts"]["bundled_cli"] == 2
    arts = tmp_path / "arts"
    arts.mkdir()
    (arts / "index.html").write_text('<link rel="service-desc" href="/openapi.json"><canvas width="1440" height="900"></canvas>')
    r3 = CliRunner().invoke(cli, ["map", "surfaces", "canvas.example", "--artifacts", str(arts)], obj={})
    assert r3.exit_code == 0 and "steer: canvas app" in r3.output and "not fetched (offline)" in r3.output
    monkeypatch.setenv("ANYUSE_REFUSE_GOV_HOSTS", "on")
    r4 = CliRunner().invoke(cli, ["map", "surfaces", "www.bundestag.de"], obj={})
    assert r4.exit_code != 0 and "government" in r4.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
