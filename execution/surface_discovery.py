"""SURFACE DISCOVERY: for every app and site, mapping first finds the BEST automation surface; clicking is the last
resort.

Pure parsers, a ranker, a read-only bundle reader and the map integration. No model, no network, no subprocess, no
AX, no pyobjc. The bundle reader only ``open()``s files INSIDE a bundle (and the system's scripting-definition
includes) for reading; it never launches, scripts or signals an app. Compiled once, while mapping - never on the fly.

THE LADDER (best first; ``LADDER``), discovered automatically and recorded per app / site with a citation each:

1. official interfaces: a documented REST / GraphQL / OpenAPI spec (well-known locations, RFC 8631 links, an RFC 9727
   API catalog), plugin APIs, official CLIs;
2. macOS app-bundle surfaces, read from the bundle: App Intents / Shortcuts actions (``Metadata.appintents/
   extract.actionsdata``; legacy SiriKit ``.intentdefinition``), URL schemes (``CFBundleURLTypes``), ``NSServices``,
   bundled CLIs (``Contents/MacOS``, ``Contents/Helpers``, ``Contents/SharedSupport/bin``, ``Contents/Resources``),
   the scripting dictionary (``OSAScriptingDefinition``; the AppleScript route is its rung);
3. web apps: the app's own XHR / fetch API (the API tier's compiled HAR captures), JS-bundle endpoints, embedded
   OpenAPI;
4. open-source code as a knowledge source (OSS apps only, license respected, a local checkout, read-only, never
   run);
5. AX / DOM;
6. vision - the explicit last resort. A canvas web app (a canvas, almost no elements) is detected and steered to 1-3.

This module reads public, declared surfaces. The opt-in REA mapping module performs deeper analysis only in a VM.
A surface whose interface the bundle
does not declare (a URL scheme's paths, a CLI's arguments) is listed but ``uncertain``: it never replaces an AX step
until something proves it (unknown = uncertain).

THE MAP: a discovered action whose declared effect is one of the map's effects becomes a ``map_compile.Transition``
(``surface_transitions``) carrying its surface kind, its invocation, its expected effect and its read-back in the
transition's source record; ``prefer_surfaces`` then chooses, per plan step, the best surface that SERVES the step
(the effect, the item kind and every required parameter bind), with the AX projection kept as the fallback. Each
surface step declares its read-back through a source OTHER than the actor (``readback_for``; none available =
``uncertain``, never verified) - the verifier principles of ``execution/attribute_verify.py``.
"""
from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import plistlib
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin, urlsplit

FORMAT = "anyuse.surfaces/1"
KNOWLEDGE_FORMAT = "anyuse.surface-knowledge/1"

#: the ladder: (kind, tier, what it is). The within-tier order is the brief's order and only breaks ties.
LADDER: Tuple[Tuple[str, int, str], ...] = (
    ("openapi", 1, "a documented REST / OpenAPI operation"),
    ("graphql", 1, "a documented GraphQL field (introspection)"),
    ("api_catalog", 1, "an RFC 9727 API catalog / RFC 8631 service-desc link to a spec"),
    ("plugin_api", 1, "an official plugin API"),
    ("official_cli", 1, "an official, documented CLI"),
    ("app_intent", 2, "an App Intents / Shortcuts action declared in the bundle (runs through `shortcuts run`)"),
    ("url_scheme", 2, "a URL scheme the bundle declares (CFBundleURLTypes)"),
    ("service", 2, "an NSServices entry the bundle declares"),
    ("bundled_cli", 2, "an executable shipped inside the bundle"),
    ("scripting", 2, "the bundle's scripting dictionary (the AppleScript route)"),
    ("web_api", 3, "the web app's own XHR / fetch request, compiled and proved by the API tier"),
    ("js_endpoint", 3, "an endpoint literal in the app's JS bundle (unproved until compiled)"),
    ("embedded_openapi", 3, "an OpenAPI document embedded in the page"),
    ("oss_registry", 4, "a command registry read from the app's open-source code (local checkout, never run)"),
    ("rea_adapter", 4, "a recovered interface, independently tested in the VM for this exact app fingerprint"),
    ("ax", 5, "the accessibility tree"),
    ("dom", 5, "the DOM"),
    ("vision", 6, "screenshots + a vision model: the explicit last resort"),
)
TIER: Dict[str, int] = {k: t for k, t, _ in LADDER}
KIND_ORDER: Dict[str, int] = {k: i for i, (k, _, _) in enumerate(LADDER)}

#: the map's effects (``map_compile.EFFECTS``) a surface may serve, plus ``delete`` (irreversible: never a substitute
#: unless the step itself is that delete) and ``other`` (a declared action with no map effect)
MAP_EFFECTS = ("launch_app", "navigate", "open_item", "select_item", "object_create", "doc_save", "scoped_find",
               "toggle_select", "flag_toggle", "read_content", "field_rw")
IRREVERSIBLE = ("delete",)

#: schemes many apps claim; opening one goes to the system's default handler, not necessarily this app
GENERIC_SCHEMES = frozenset({"http", "https", "file", "ftp", "mailto", "tel", "sms", "webcal", "ssh", "telnet",
                             "vnc", "afp", "smb", "cifs", "x-man-page", "help", "facetime", "facetime-audio"})

#: entity / noun kinds that are containers: opening one is a navigation
CONTAINER_KINDS = frozenset({"folder", "account", "tag", "album", "playlist", "mailbox", "notebook", "location",
                             "directory"})

#: App Intents primitive type identifiers, by the role they carry in the system's own bundles (Notes: name and
#: content are 12, subtitle 0, isPinned 1, creationDate 8). Anything else stays ``primitive:<n>`` (unknown).
PRIMITIVE_TYPES = {0: "string", 1: "bool", 8: "date", 12: "text"}
TEXT_TYPES = frozenset({"string", "text", "search_criteria"})

#: App Intents system protocols -> the effect they declare
PROTOCOL_EFFECT = {
    "com.apple.link.systemProtocol.CreateEntity": "create",
    "com.apple.link.systemProtocol.OpenEntity": "open",
    "com.apple.link.systemProtocol.DeleteEntity": "delete",
    "com.apple.link.systemProtocol.ShowInAppStringSearchResults": "search",
    "com.apple.link.systemProtocol.ChangeBinarySetting": "setting",
}
#: declared title / identifier verbs -> the effect they declare (weaker than a protocol; still the app's own words)
VERB_EFFECT = {
    "open": "open", "show": "open", "go": "navigate", "goto": "navigate", "reveal": "select",
    "create": "create", "new": "create", "make": "create", "append": "append",
    "search": "search", "find": "search", "look": "search", "lookup": "search",
    "delete": "delete", "trash": "delete", "remove": "delete", "erase": "delete",
    "get": "read", "read": "read",
    "set": "setting", "change": "setting", "toggle": "setting", "turn": "setting",
}
_SUFFIX_TOKENS = {"intent", "link", "action", "app", "v2", "v3", "service", "handler"}
_PREPOSITIONS = frozenset({"from", "to", "in", "into", "with", "for", "on", "of", "and", "as", "by"})
#: nouns that name no item kind of their own: the target entity's kinds decide
GENERIC_NOUNS = frozenset({"item", "items", "node", "nodes", "location", "locations", "entity", "entities", "object",
                           "objects"})


def _singular(noun: str) -> str:
    return noun[:-1] if noun.endswith("s") and noun[:-1] in ("note", "folder", "file", "tag", "document", "item") \
        else noun


# ---------------------------------------------------------------------------
# 1. The surface record
# ---------------------------------------------------------------------------

@dataclass
class Surface:
    """One automation surface of one app or site, with its citation.

    ``effect``: ``{"effect": <a MAP_EFFECTS name | delete | other | "">, "verb", "item_kinds": [...], "mode",
    "source": protocol | title | declared | none, "options": [...]}``. ``interface``: ``declared`` (typed parameters and
    an effect the app declares), ``partial`` (the surface is declared, its parameters or effect are not: a URL scheme)
    or ``unknown`` (a CLI whose arguments are unread). ``requires``: one-time grants a runner needs (``shortcut_import``,
    ``automation_grant``). ``cite``: ``{"file", "pointer", "sha256"}`` - where in the bundle / document it was read."""
    kind: str
    key: str
    label: str
    app: str = ""
    bundle_id: str = ""
    params: List[Dict[str, Any]] = field(default_factory=list)
    effect: Dict[str, Any] = field(default_factory=dict)
    invoke: Dict[str, Any] = field(default_factory=dict)
    requires: List[str] = field(default_factory=list)
    interface: str = "declared"
    cite: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def tier(self) -> int:
        return TIER.get(self.kind, 9)

    def to_dict(self) -> Dict[str, Any]:
        d = {k: copy.deepcopy(v) for k, v in self.__dict__.items()}
        d["tier"] = self.tier
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Surface":
        return cls(**{k: copy.deepcopy(d[k]) for k in cls.__dataclass_fields__ if k in d})  # type: ignore[arg-type]


def _cite(file: str, pointer: str = "", sha: str = "") -> Dict[str, Any]:
    c: Dict[str, Any] = {"file": file}
    if pointer:
        c["pointer"] = pointer
    if sha:
        c["sha256"] = sha
    return c


def _key(x: Any) -> str:
    """A localizable title (``{"key": ..., "alternatives": [...]}``) or a plain string -> its text."""
    if isinstance(x, dict):
        return str(x.get("key") or "")
    return str(x or "")


def camel_tokens(name: str) -> List[str]:
    """``CreateNoteLinkAction`` -> ``["create", "note", "link", "action"]``; ``FINodeEntity`` -> ``["fi", "node",
    "entity"]``."""
    name = re.sub(r"[vV]\d+$", "", name or "")
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b|_)|[A-Z]?[a-z]+|[A-Z]+|\d+", name)
    return [p.lower() for p in parts if p]


def _verb_effect(*texts: str) -> Tuple[str, str]:
    """The first declared verb of a title / identifier -> (effect, verb)."""
    for t in texts:
        toks = camel_tokens(t) if t and " " not in t else re.findall(r"[a-z]+", (t or "").lower())
        for i, w in enumerate(toks[:3]):
            if w == "go" and i + 1 < len(toks) and toks[i + 1] == "to":
                return "navigate", "go to"
            if w == "look" and i + 1 < len(toks) and toks[i + 1] == "up":
                return "search", "look up"
            if w in VERB_EFFECT:
                return VERB_EFFECT[w], w
    return "", ""


def _noun_kind(identifier: str) -> str:
    """The noun of an action identifier, after its verb: ``CreateFolderIntent`` -> ``folder``."""
    toks = [t for t in camel_tokens(identifier) if t not in _SUFFIX_TOKENS]
    if len(toks) >= 2 and toks[0] in VERB_EFFECT:
        rest = toks[1:]
        if rest and rest[0] in ("to", "up"):
            rest = rest[1:]
        cut = next((i for i, t in enumerate(rest) if t in _PREPOSITIONS), len(rest))
        return "_".join(_singular(t) for t in rest[:cut])
    return ""


def entity_kind(type_name: str) -> str:
    """``NoteEntity`` -> ``note``; ``FolderEntity`` -> ``folder``; ``ChecklistItemEntity`` -> ``checklist_item``."""
    toks = [t for t in camel_tokens(type_name) if t not in ("entity", "app", "intent")]
    return "_".join(toks)


def map_effect(declared: str, kinds: Sequence[str]) -> str:
    """A declared effect + the item kinds it acts on -> the map effect it serves (or ``other`` / ``delete``)."""
    container = bool(kinds) and all(k in CONTAINER_KINDS for k in kinds)
    return {"create": "object_create", "open": "navigate" if container else "open_item", "navigate": "navigate",
            "select": "select_item", "search": "scoped_find", "append": "doc_save", "read": "read_content",
            "setting": "flag_toggle", "delete": "delete"}.get(declared, "other" if declared else "")


# ---------------------------------------------------------------------------
# 2. Bundle parsers (pure: dicts / bytes in, surfaces out)
# ---------------------------------------------------------------------------

def url_scheme_surfaces(info: Dict[str, Any], *, app: str = "", bundle_id: str = "",
                        cite_file: str = "Contents/Info.plist") -> List[Surface]:
    """``CFBundleURLTypes`` -> one surface per scheme. The bundle declares the scheme, never its paths or
    parameters, so every scheme is ``partial`` with no declared effect; a scheme many apps claim is ``generic``."""
    out: List[Surface] = []
    seen = set()
    for i, t in enumerate(info.get("CFBundleURLTypes") or []):
        if not isinstance(t, dict):
            continue
        for s in t.get("CFBundleURLSchemes") or []:
            s = str(s).strip().lower()
            if not s or s in seen or not re.fullmatch(r"[a-z][a-z0-9+.\-]*", s):
                continue
            seen.add(s)
            generic = s in GENERIC_SCHEMES
            out.append(Surface(kind="url_scheme", key=f"url_scheme:{s}", label=f"{s}:", app=app, bundle_id=bundle_id,
                               effect={"effect": "", "source": "none"},
                               invoke={"via": "open_url", "scheme": s, "role": str(t.get("CFBundleTypeRole") or "")},
                               interface="partial", cite=_cite(cite_file, f"CFBundleURLTypes[{i}]"),
                               notes=(["generic scheme: the system's default handler answers it"] if generic else [])
                               + ["paths and parameters are not declared by the bundle"]))
            if generic:
                out[-1].invoke["generic"] = True
    return out


def _send_kind(types: Sequence[str]) -> str:
    ts = {str(t) for t in types or []}
    if ts & {"NSFilenamesPboardType", "public.file-url", "NSURLPboardType"}:
        return "file"
    if ts & {"NSStringPboardType", "public.plain-text", "public.utf8-plain-text", "public.rtf", "NSRTFPboardType"}:
        return "text"
    if ts:
        return "data"
    return ""


def service_surfaces(info: Dict[str, Any], *, app: str = "", bundle_id: str = "",
                     cite_file: str = "Contents/Info.plist") -> List[Surface]:
    """``NSServices`` -> one surface per service: its menu title (the declared verb), what it takes
    (``NSSendTypes``) and returns (``NSReturnTypes``). A runner performs it through the public
    ``NSPerformService(title, pasteboard)``."""
    out: List[Surface] = []
    for i, s in enumerate(info.get("NSServices") or []):
        if not isinstance(s, dict):
            continue
        title = _key((s.get("NSMenuItem") or {}).get("default") if isinstance(s.get("NSMenuItem"), dict)
                     else s.get("NSMenuItem"))
        msg = str(s.get("NSMessage") or "")
        if not title and not msg:
            continue
        shown = title.split("/")[-1] if title else msg
        declared, verb = _verb_effect(shown, msg)
        send = _send_kind(s.get("NSSendTypes") or [])
        ret = _send_kind(s.get("NSReturnTypes") or [])
        kinds: List[str] = []
        words = re.findall(r"[a-z]+", shown.lower())
        if declared in ("open", "select") and len(words) > 1 and \
                not set(words[1:]) & {"selected", "selection", "file", "files", "item", "items", "in"}:
            declared = "other"
        if declared == "create" and re.search(r"\bwindow|document|sticky|note\b", shown.lower()):
            kinds = ["document"]
        elif declared in ("open", "select") and send == "file":
            kinds = ["file", "folder"]
        eff = map_effect(declared, kinds)
        params = [{"name": "input", "type": "text" if send == "text" else send or "none", "optional": False}] if send else []
        notes = []
        if s.get("NSRestricted"):
            notes.append("NSRestricted: the system may require the user to enable it")
        if s.get("NSRequiredContext"):
            notes.append(f"NSRequiredContext {json.dumps(s.get('NSRequiredContext'), sort_keys=True)}")
        out.append(Surface(kind="service", key=f"service:{msg or shown}", label=shown, app=app, bundle_id=bundle_id,
                           params=params, effect={"effect": eff, "verb": verb, "item_kinds": kinds,
                                                  "source": "title" if declared else "none", "returns": ret},
                           invoke={"via": "perform_service", "title": title, "message": msg,
                                   "send_types": list(s.get("NSSendTypes") or [])},
                           interface="declared" if declared and send else "partial",
                           cite=_cite(cite_file, f"NSServices[{i}]"), notes=notes))
    return out


def _value_type(vt: Any, enums: Dict[str, List[str]]) -> Dict[str, Any]:
    """An App Intents ``valueType`` -> ``{"type", "entity"?, "enum"?, "array"?}``."""
    if not isinstance(vt, dict) or not vt:
        return {"type": "unknown"}
    k, v = next(iter(vt.items()))
    w = (v or {}).get("wrapper") if isinstance(v, dict) else None
    if k == "primitive":
        n = (w or {}).get("typeIdentifier")
        return {"type": PRIMITIVE_TYPES.get(n, f"primitive:{n}")}
    if k == "entity":
        return {"type": "entity", "entity": str((w or {}).get("typeName") or "")}
    if k == "linkEnumeration":
        ident = str((w or {}).get("identifier") or "")
        return {"type": "enum", "enum": ident, "cases": list(enums.get(ident, []))}
    if k == "searchCriteria":
        return {"type": "search_criteria"}
    if k == "array":
        inner = _value_type((w or {}).get("memberValueType"), enums)
        inner["array"] = True
        return inner
    return {"type": k}


def _entity_facts(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per entity: its kinds, whether a file path resolves it, the properties a declared property query compares,
    and its declared properties (for the read surfaces)."""
    ents = doc.get("entities") or {}
    queries = doc.get("queries") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, e in ents.items():
        if not isinstance(e, dict):
            continue
        prot = e.get("systemProtocolMetadata") or []
        prot_names = [p for p in prot if isinstance(p, str)]
        tct = e.get("transferableContentTypes") or {}
        importable = [str(t.get("contentType")) for t in tct.get("importableTypes") or [] if isinstance(t, dict)]
        file_backed = any("PersistentFileIdentifiable" in p or p.endswith(".FileEntity") for p in prot_names) or \
            any(t in ("public.item", "public.folder", "public.data", "public.content") for t in importable)
        by_prop: Dict[str, List[int]] = {}
        for q in queries.values():
            if not isinstance(q, dict) or q.get("entityType") != name:
                continue
            for p in q.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("propertyIdentifier") or p.get("identifier") or "")
                if pid:
                    by_prop[pid] = sorted({int(c.get("comparatorTypeValue", -1)) for c in p.get("comparators") or []
                                           if isinstance(c, dict)})
        props = []
        for p in e.get("properties") or []:
            if isinstance(p, dict):
                props.append({"name": str(p.get("identifier") or ""), "title": _key(p.get("title")),
                              **_value_type(p.get("valueType"), {})})
        kind = entity_kind(name)
        kinds = ["file", "folder", "document"] if file_backed else ([kind] if kind else [])
        out[name] = {"kinds": kinds, "file_backed": file_backed, "query_props": by_prop, "properties": props,
                     "display": _key(e.get("displayTypeName"))}
    return out


def _resolution(entity: str, facts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    f = facts.get(entity)
    if f is None:
        return {"by": "none", "why": f"the entity {entity!r} is not declared"}
    if f["file_backed"]:
        return {"by": "file_path"}
    for prop in ("name", "title"):
        if prop in f["query_props"]:
            return {"by": "name_query", "property": prop, "comparators": f["query_props"][prop],
                    "unique": "checked at run time: none or several matches refuse"}
    return {"by": "none", "why": f"{entity} declares no file identity and no property query on its name"}


def appintent_surfaces(doc: Dict[str, Any], *, app: str = "", bundle_id: str = "",
                       cite_file: str = "Contents/Resources/Metadata.appintents/extract.actionsdata",
                       sha: str = "") -> List[Surface]:
    """``extract.actionsdata`` (the App Intents metadata Xcode compiles into the bundle) -> one surface per action:
    its declared title, typed parameters (an entity parameter with how a value resolves it), the effect its system
    protocol declares (else its title / identifier verb), and whether it opens the app. Also one ``read`` surface per
    text property of an entity a value can resolve (Shortcuts' "get details" reads declared entity properties).
    Runs through a Shortcut that holds the action (``shortcuts run``; one manual import per shortcut)."""
    acts = doc.get("actions") or {}
    enums = {str(e.get("identifier") or ""): [str(c.get("identifier") or "") for c in e.get("cases") or []
                                              if isinstance(c, dict)]
             for e in doc.get("enums") or [] if isinstance(e, dict)}
    facts = _entity_facts(doc)
    out: List[Surface] = []
    for aid in sorted(acts):
        a = acts[aid]
        if not isinstance(a, dict):
            continue
        title = _key(a.get("title")) or " ".join(t for t in camel_tokens(aid) if t not in _SUFFIX_TOKENS).title()
        params = []
        for p in a.get("parameters") or []:
            if not isinstance(p, dict):
                continue
            vt = _value_type(p.get("valueType"), enums)
            rec = {"name": str(p.get("name") or ""), "title": _key(p.get("title")), "optional": bool(p.get("isOptional")),
                   **vt}
            if vt.get("type") == "entity":
                rec["resolve"] = _resolution(vt["entity"], facts)
            tsm = p.get("typeSpecificMetadata") or []
            for j, x in enumerate(tsm):
                if x == "LNValueTypeMetadataKeyFileSupportedTypes" and j + 1 < len(tsm):
                    els = (((tsm[j + 1] or {}).get("array") or {}).get("elements")) or []
                    rec["file_types"] = [((e.get("string") or {}).get("wrapper")) for e in els if isinstance(e, dict)]
            params.append(rec)
        protos = [p for p in a.get("systemProtocols") or [] if isinstance(p, str)]
        declared = next((PROTOCOL_EFFECT[p] for p in protos if p in PROTOCOL_EFFECT), "")
        source = "protocol" if declared else ""
        verb = ""
        if not declared:
            declared, verb = _verb_effect(title, aid)
            source = "title" if declared else "none"
        # the item kinds it acts on: the identifier's noun, else the target / output entity
        noun = _noun_kind(aid)
        target = next((p for p in params if p.get("type") == "entity" and not p.get("optional")), None)
        out_ent = (((a.get("outputType") or {}).get("entity") or {}).get("wrapper") or {}).get("typeName") \
            if isinstance(a.get("outputType"), dict) else None
        kinds: List[str] = []
        if declared == "create" and out_ent and (not noun or noun in GENERIC_NOUNS):
            noun = ""
        if noun and noun not in GENERIC_NOUNS:
            kinds = [noun]
        elif declared == "create" and out_ent:
            kinds = list((facts.get(out_ent) or {}).get("kinds") or [entity_kind(out_ent)])
        elif target is not None:
            kinds = list((facts.get(target["entity"]) or {}).get("kinds") or [entity_kind(target["entity"])])
            if target.get("file_types"):
                ft = set(target["file_types"])
                kinds = (["file", "document"] if "public.item" in ft or "public.data" in ft else []) + \
                        (["folder"] if "public.folder" in ft or "public.item" in ft else [])
        elif out_ent:
            kinds = [entity_kind(out_ent)]
        if declared == "open" and target is None:
            enum_t = next((p for p in params if p.get("type") == "enum" and not p.get("optional")), None)
            if enum_t is not None:
                kinds = ["view"]
        eff = map_effect(declared, kinds)
        if declared == "open" and kinds == ["view"]:
            eff = "navigate"
        mode = "append" if declared == "append" else ""
        options: List[str] = []
        if declared == "setting":
            for p in params:
                if p.get("type") == "enum" and p.get("name") != "changeOperation" and \
                        not set(p.get("cases") or []) <= {"enable", "disable", "toggle", "on", "off"}:
                    options = list(p.get("cases") or [])
        requires = ["shortcut_import"]
        notes = []
        if a.get("openAppWhenRun"):
            notes.append("opens the app when run")
        if not a.get("isDiscoverable", True) or (a.get("visibilityMetadata") or {}).get("assistantOnly"):
            notes.append("not discoverable in Shortcuts (assistant only)")
        out.append(Surface(kind="app_intent", key=f"app_intent:{aid}", label=title, app=app, bundle_id=bundle_id,
                           params=params, effect={"effect": eff, "declared": declared, "verb": verb,
                                                  "protocols": protos, "item_kinds": kinds, "mode": mode,
                                                  "source": source, "options": options,
                                                  "output": out_ent or ""},
                           invoke={"via": "shortcuts", "action": aid, "bundle_id": bundle_id,
                                   "type": str(a.get("fullyQualifiedTypeName") or ""),
                                   "opens_app": bool(a.get("openAppWhenRun"))},
                           requires=requires,
                           interface="declared" if declared else "partial",
                           cite=_cite(cite_file, f"/actions/{aid}", sha), notes=notes))
    # declared entity properties a value can resolve: a read surface each (text-typed properties only)
    for ename, f in sorted(facts.items()):
        res = _resolution(ename, facts)
        if res["by"] == "none":
            continue
        for p in f["properties"]:
            if p.get("type") not in TEXT_TYPES or p.get("array"):
                continue
            kind = entity_kind(ename)
            out.append(Surface(kind="app_intent", key=f"app_intent:entity_property:{ename}.{p['name']}",
                               label=f"Get {p.get('title') or p['name']} of {f.get('display') or kind}", app=app,
                               bundle_id=bundle_id,
                               params=[{"name": "entity", "type": "entity", "entity": ename, "optional": False,
                                        "resolve": res}],
                               effect={"effect": "read_content", "declared": "read", "item_kinds": f["kinds"] or [kind],
                                       "source": "declared", "property": p["name"]},
                               invoke={"via": "shortcuts", "action": "entity_property", "entity": ename,
                                       "property": p["name"], "bundle_id": bundle_id},
                               requires=["shortcut_import"], cite=_cite(cite_file, f"/entities/{ename}/properties", sha),
                               notes=["Shortcuts' Get Details of a declared entity property"]))
    return out


def intentdefinition_surfaces(doc: Dict[str, Any], *, app: str = "", bundle_id: str = "",
                              cite_file: str = "", sha: str = "") -> List[Surface]:
    """A legacy SiriKit ``.intentdefinition`` (property list) -> one ``app_intent`` surface per custom intent
    (``INIntents``): title, verb, parameters. Internal intents are skipped. Their effect comes from the declared verb /
    title; a parameter of an app object type has no declared resolution (``uncertain`` until proved)."""
    out: List[Surface] = []
    for i, it in enumerate(doc.get("INIntents") or []):
        if not isinstance(it, dict) or it.get("INIntentInternal"):
            continue
        name = str(it.get("INIntentName") or "")
        title = str(it.get("INIntentTitle") or name)
        declared, verb = _verb_effect(str(it.get("INIntentVerb") or ""), title)
        params = []
        for p in it.get("INIntentParameters") or []:
            if not isinstance(p, dict):
                continue
            typ = str(p.get("INIntentParameterType") or "")
            rec = {"name": str(p.get("INIntentParameterName") or ""), "optional": True,
                   "type": "string" if typ == "String" else ("entity" if typ == "Object" else typ.lower() or "unknown")}
            if typ == "Object":
                rec["entity"] = str(p.get("INIntentParameterObjectType") or "")
                rec["resolve"] = {"by": "none", "why": "a SiriKit object type declares no resolution by name"}
            params.append(rec)
        kinds = [entity_kind(params[0]["entity"])] if params and params[0].get("entity") else []
        kinds = [k[len("notes"):] if k.startswith("notes") and len(k) > 5 else k for k in kinds]
        out.append(Surface(kind="app_intent", key=f"app_intent:intentdefinition:{name}", label=title, app=app,
                           bundle_id=bundle_id, params=params,
                           effect={"effect": map_effect(declared, kinds), "declared": declared, "verb": verb,
                                   "item_kinds": kinds, "source": "title" if declared else "none",
                                   "category": str(it.get("INIntentCategory") or "")},
                           invoke={"via": "shortcuts", "action": name, "legacy": "sirikit", "bundle_id": bundle_id},
                           requires=["shortcut_import"], interface="declared" if declared else "partial",
                           cite=_cite(cite_file, f"INIntents[{i}]", sha)))
    return out


#: the scripting dictionary's effect table, by Apple event code (``osa_compile.EFFECTS``) -> the ladder's effects
_OSA_TO_DECLARED = {"read": "read", "create": "create", "write": "write", "destructive": "delete", "ui": "ui",
                    "never": "delete"}


def scripting_surfaces(commands: Sequence[Dict[str, Any]], *, app: str = "", bundle_id: str = "",
                       cite_file: str = "", source: str = "sdef") -> List[Surface]:
    """Typed scripting-dictionary commands (``desktop_discover.parse_sdef``'s shape, or another
    reader: see :func:`set_sdef_reader`) -> one ``scripting`` surface per command, its effect from the fixed
    event-code table (``osa_compile.EFFECTS``; a code outside it is ``undeclared``). The AppleScript route
    (``execution/applescript_route.py``) is the rung that runs them; this lists them on the ladder."""
    from anyuse.execution import osa_compile as oc
    out: List[Surface] = []
    for c in commands:
        if not isinstance(c, dict) or "_missing_includes" in c or c.get("hidden"):
            continue
        code = str(c.get("code") or "")
        osa = oc.EFFECTS.get(code, "undeclared")
        declared = _OSA_TO_DECLARED.get(osa, "")
        params = []
        dp = c.get("direct_parameter")
        if dp:
            params.append({"name": "direct", "type": str(dp.get("type") or "any"), "optional": bool(dp.get("optional"))})
        for p in c.get("params") or []:
            params.append({"name": str(p.get("name") or ""), "type": str(p.get("type") or "any"),
                           "optional": bool(p.get("optional")), "code": str(p.get("code") or "")})
        out.append(Surface(kind="scripting", key=f"scripting:{code or c.get('name')}", label=str(c.get("name") or ""),
                           app=app, bundle_id=bundle_id, params=params,
                           effect={"effect": "delete" if declared == "delete" else "", "declared": declared or "undeclared",
                                   "osa_effect": osa, "source": "declared" if declared else "none",
                                   "suite": str(c.get("suite") or "")},
                           invoke={"via": "applescript_route", "event": code},
                           requires=["automation_grant"], interface="declared",
                           cite=_cite(cite_file, f"command {c.get('name')} ({code})"), notes=[f"source: {source}"]))
    return out


# --- Mach-O: is this file an executable (a CLI candidate) or a library? -----------------------------------------------

_MH = {0xFEEDFACE: "<", 0xFEEDFACF: "<", 0xCEFAEDFE: ">", 0xCFFAEDFE: ">"}
_FAT = (0xCAFEBABE, 0xCAFEBABF)
MH_FILETYPES = {1: "object", 2: "execute", 6: "dylib", 8: "bundle", 7: "dylinker"}


def macho_filetype(head: bytes, read_at: Optional[Callable[[int, int], bytes]] = None) -> str:
    """The Mach-O file type of a file from its first bytes (``execute``, ``dylib``, ``bundle``, ...), ``script`` for a
    ``#!`` script, ``""`` for anything else. A universal (fat) binary reads its first slice's header through
    ``read_at(offset, n)``. Reads bytes only; nothing is executed."""
    if len(head) >= 2 and head[:2] == b"#!":
        return "script"
    if len(head) < 16:
        return ""
    magic_be = struct.unpack(">I", head[:4])[0]
    magic_le = struct.unpack("<I", head[:4])[0]
    if magic_be in _FAT:
        if read_at is None or len(head) < 8 + 20:
            return "fat"
        off = struct.unpack(">I", head[16:20])[0]
        sub = read_at(off, 16)
        return macho_filetype(sub) if sub else "fat"
    for magic, endian in ((magic_le, "<"),):
        if magic in (0xFEEDFACE, 0xFEEDFACF):
            ft = struct.unpack(endian + "I", head[12:16])[0]
            return MH_FILETYPES.get(ft, f"type{ft}")
    if magic_be in (0xFEEDFACE, 0xFEEDFACF):
        ft = struct.unpack(">I", head[12:16])[0]
        return MH_FILETYPES.get(ft, f"type{ft}")
    return ""


_LIB_SUFFIX = (".dylib", ".so", ".a", ".o", ".bundle", ".plugin", ".framework", ".app", ".appex", ".xpc", ".node")


def cli_surfaces(listing: Sequence[Dict[str, Any]], *, main_executable: str = "", app: str = "",
                 bundle_id: str = "") -> List[Surface]:
    """Executables shipped inside the bundle (a listing of ``{"path": <relative>, "filetype": <macho_filetype>,
    "executable": <x bit>}``) -> one ``bundled_cli`` surface per Mach-O executable or ``#!`` script that is not the
    app's main executable and not inside a nested bundle. Their arguments are not declared: ``unknown`` until a VM
    check documents them; nothing is run here."""
    out: List[Surface] = []
    for it in listing:
        rel = str(it.get("path") or "")
        base = os.path.basename(rel)
        if not rel or not it.get("executable") or base.endswith(_LIB_SUFFIX):
            continue
        if any(seg.endswith((".app", ".appex", ".framework", ".bundle", ".xpc")) for seg in rel.split("/")[:-1]):
            continue
        if rel == f"Contents/MacOS/{main_executable}":
            continue
        ft = str(it.get("filetype") or "")
        if ft not in ("execute", "script", "fat"):
            continue
        out.append(Surface(kind="bundled_cli", key=f"bundled_cli:{rel}", label=base, app=app, bundle_id=bundle_id,
                           effect={"effect": "", "source": "none"},
                           invoke={"via": "exec", "path": rel, "filetype": ft}, interface="unknown",
                           cite=_cite(rel), notes=["arguments undeclared: unknown until documented"]))
    return out


# ---------------------------------------------------------------------------
# 3. Web: official interfaces, the app's own API, canvas detection (pure; offline - nothing is fetched here)
# ---------------------------------------------------------------------------

#: well-known locations of an official interface; a probe list for a later, approved online phase
WELL_KNOWN = (
    ("/.well-known/api-catalog", "api_catalog", "RFC 9727 API catalog (a linkset)"),
    ("/openapi.json", "openapi", "conventional OpenAPI location"),
    ("/swagger.json", "openapi", "conventional Swagger 2 location"),
    ("/v3/api-docs", "openapi", "springdoc default"),
    ("/api-docs", "openapi", "conventional"),
    ("/.well-known/ai-plugin.json", "openapi", "plugin manifest pointing at an OpenAPI document"),
    ("/graphql", "graphql", "GraphQL endpoint; introspection is a POST: only with an owner opt-in"),
)


def well_known_probes(origin: str) -> List[Dict[str, str]]:
    """The URLs an online phase would read for ``origin`` (an https origin), in order. Pure: builds strings."""
    o = origin.rstrip("/")
    if not re.match(r"^https?://", o):
        o = "https://" + o
    return [{"url": o + path, "kind": kind, "why": why} for path, kind, why in WELL_KNOWN]


_LINK_TAG = re.compile(r"<link\b([^>]*)>", re.I)
_ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
SERVICE_RELS = ("service-desc", "service-doc", "service-meta", "api-catalog")


def _attrs(s: str) -> Dict[str, str]:
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4) if m.group(4) is not None
                                 else m.group(5)) or "" for m in _ATTR.finditer(s)}


def service_links(html: str = "", link_header: str = "", base: str = "") -> List[Dict[str, str]]:
    """RFC 8631 / RFC 9727 links (``service-desc``, ``service-doc``, ``service-meta``, ``api-catalog``) from an HTML
    document's ``<link>`` tags and an HTTP ``Link`` header, resolved against ``base``."""
    out: List[Dict[str, str]] = []
    for m in _LINK_TAG.finditer(html or ""):
        a = _attrs(m.group(1))
        rels = (a.get("rel") or "").lower().split()
        for r in rels:
            if r in SERVICE_RELS and a.get("href"):
                out.append({"rel": r, "href": urljoin(base, a["href"]) if base else a["href"], "type": a.get("type", ""),
                            "from": "html"})
    for part in re.split(r",\s*(?=<)", link_header or ""):
        m = re.match(r"\s*<([^>]+)>\s*;(.*)$", part)
        if not m:
            continue
        params = dict((k.lower(), v.strip('"')) for k, v in re.findall(r"([a-zA-Z*-]+)\s*=\s*(\"[^\"]*\"|[^;]+)",
                                                                        m.group(2)))
        for r in (params.get("rel") or "").lower().split():
            if r in SERVICE_RELS:
                out.append({"rel": r, "href": urljoin(base, m.group(1)) if base else m.group(1),
                            "type": params.get("type", ""), "from": "link_header"})
    return out


def api_catalog_surfaces(linkset: Dict[str, Any], *, site: str = "", cite_file: str = "") -> List[Surface]:
    """An RFC 9727 API catalog (``application/linkset+json``) -> one ``api_catalog`` surface per described API:
    its ``service-desc`` (the spec to read next) and ``service-doc`` links."""
    out: List[Surface] = []
    for i, ent in enumerate((linkset or {}).get("linkset") or []):
        if not isinstance(ent, dict):
            continue
        anchor = str(ent.get("anchor") or "")
        desc = [x.get("href") for x in ent.get("service-desc") or [] if isinstance(x, dict) and x.get("href")]
        doc = [x.get("href") for x in ent.get("service-doc") or [] if isinstance(x, dict) and x.get("href")]
        if not (desc or doc):
            continue
        out.append(Surface(kind="api_catalog", key=f"api_catalog:{anchor or i}", label=anchor or f"api {i}", app=site,
                           effect={"effect": "", "source": "declared"},
                           invoke={"via": "read_spec", "service_desc": desc, "service_doc": doc},
                           interface="partial", cite=_cite(cite_file, f"/linkset/{i}"),
                           notes=["the catalog points at specs; their operations are the surfaces"]))
    return out


_SEARCH_PARAM = re.compile(r"^(q|query|search|term|keyword|keywords|text|s)$", re.I)


def openapi_surfaces(spec: Dict[str, Any], *, site: str = "", cite_file: str = "",
                     kind: str = "openapi") -> List[Surface]:
    """An OpenAPI 3 / Swagger 2 document (already decoded from JSON) -> one surface per operation: method, path,
    typed parameters, the effect its method declares (GET/HEAD read; a GET with a search parameter search; POST / PUT /
    PATCH write; DELETE delete). A credential-bearing security scheme is noted, never filled."""
    out: List[Surface] = []
    if not isinstance(spec, dict) or not (str(spec.get("openapi") or "").startswith("3") or spec.get("swagger")):
        return out
    servers = [s.get("url") for s in spec.get("servers") or [] if isinstance(s, dict) and s.get("url")]
    if not servers and spec.get("host"):
        servers = [f"{(spec.get('schemes') or ['https'])[0]}://{spec['host']}{spec.get('basePath') or ''}"]
    secured = bool(spec.get("security") or spec.get("securityDefinitions") or
                   ((spec.get("components") or {}).get("securitySchemes")))
    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        shared = [p for p in item.get("parameters") or [] if isinstance(p, dict)]
        for method in ("get", "head", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = []
            for p in shared + [p for p in op.get("parameters") or [] if isinstance(p, dict)]:
                if "$ref" in p:
                    params.append({"name": p["$ref"].split("/")[-1], "type": "ref", "optional": True})
                    continue
                sch = p.get("schema") or {}
                params.append({"name": str(p.get("name") or ""), "in": str(p.get("in") or ""),
                               "type": str(sch.get("type") or p.get("type") or "any"),
                               "optional": not bool(p.get("required"))})
            if op.get("requestBody"):
                params.append({"name": "body", "in": "body", "type": "object",
                               "optional": not bool((op.get("requestBody") or {}).get("required"))})
            m = method.upper()
            declared = {"GET": "read", "HEAD": "read", "DELETE": "delete"}.get(m, "write")
            if declared == "read" and any(_SEARCH_PARAM.match(p["name"]) for p in params if p.get("in") == "query"):
                declared = "search"
            opid = str(op.get("operationId") or f"{m} {path}")
            notes = []
            if secured or op.get("security"):
                notes.append("declares a security scheme: needs the user's own credential; never filled here")
            out.append(Surface(kind=kind, key=f"{kind}:{m} {path}", label=str(op.get("summary") or opid), app=site,
                               params=params, effect={"effect": "", "declared": declared, "source": "declared",
                                                      "method": m},
                               invoke={"via": "http", "method": m, "path": path, "servers": servers,
                                       "operation_id": opid},
                               cite=_cite(cite_file, f"/paths/{path.replace('~', '~0').replace('/', '~1')}/{method}"),
                               notes=notes))
    return out


def graphql_surfaces(introspection: Dict[str, Any], *, site: str = "", cite_file: str = "") -> List[Surface]:
    """A GraphQL introspection result (``{"data": {"__schema": ...}}``) -> one surface per root field: query fields
    read, mutation fields write, subscription fields skipped. Typed arguments with their non-null requirement."""
    schema = ((introspection or {}).get("data") or introspection or {}).get("__schema") or {}
    types = {t.get("name"): t for t in schema.get("types") or [] if isinstance(t, dict)}
    out: List[Surface] = []
    for root, declared in (("queryType", "read"), ("mutationType", "write")):
        name = ((schema.get(root) or {}).get("name"))
        t = types.get(name) if name else None
        for f in (t or {}).get("fields") or []:
            if not isinstance(f, dict):
                continue
            params = []
            for a in f.get("args") or []:
                ty = a.get("type") or {}
                params.append({"name": str(a.get("name") or ""), "type": str(ty.get("name") or (ty.get("ofType") or {})
                                                                            .get("name") or "any"),
                               "optional": ty.get("kind") != "NON_NULL"})
            out.append(Surface(kind="graphql", key=f"graphql:{root[:-4]}.{f.get('name')}", label=str(f.get("name") or ""),
                               app=site, params=params, effect={"effect": "", "declared": declared, "source": "declared"},
                               invoke={"via": "graphql", "operation": root[:-4], "field": f.get("name")},
                               cite=_cite(cite_file, f"/__schema/{root}/{f.get('name')}"),
                               notes=["a mutation is a write: never compiled into the read-only rungs"]
                               if declared == "write" else []))
    return out


def compiled_api_surfaces(compiled: Dict[str, Any], *, site: str = "", cite_file: str = "") -> List[Surface]:
    """The API tier's proved compiled request (a ``compiled_api.json`` / ``CompileResult.to_dict``) -> one
    ``web_api`` surface: its method, URL template and parameter. The API tier's fresh-input proof is its proof."""
    t = (compiled or {}).get("template") if isinstance((compiled or {}).get("template"), dict) else compiled
    if not isinstance(t, dict) or not t.get("url"):
        return []
    m = str(t.get("method") or "GET").upper()
    pname = ((compiled or {}).get("param") or {}).get("name") or ""
    params = [{"name": pname, "type": "text", "optional": False}] if pname else \
        [{"name": n, "type": "text", "optional": False} for n in re.findall(r"\{([a-z_][a-z0-9_]*)\}", t["url"])]
    return [Surface(kind="web_api", key=f"web_api:{m} {t['url']}", label=f"{m} {urlsplit(t['url']).path}", app=site,
                    params=params, effect={"effect": "", "declared": "read" if m in ("GET", "HEAD") else "write",
                                           "source": "declared"},
                    invoke={"via": "api_step", "method": m, "url": t["url"]},
                    cite=_cite(cite_file, "/template"), notes=["proved by the API tier's fresh-input proof"])]


_ENDPOINT = re.compile(r"^(?:https://api\.[a-z0-9.-]+)?/(?:api|graphql|v\d+|rest|rpc)(?:/[\w\-.{}:]*)*/?(?:\?[\w=&{}.-]*)?$",
                       re.I)


def js_endpoint_surfaces(texts: Dict[str, str], *, site: str = "") -> List[Surface]:
    """Endpoint literals in the app's JS bundles: a whole string literal of the code (``api_rescan.code_segments`` /
    ``literal_spans``: comments, JSON data and markup are never read) shaped like an API path (``/api/...``,
    ``/graphql``, ``/v2/...``, ``https://api.<host>/...``). Unproved: the API tier compiles and proves one before
    it serves anything."""
    from anyuse.execution import api_rescan as AR
    seen: Dict[str, Tuple[str, int]] = {}
    for name, text in (texts or {}).items():
        for off, code in AR.code_segments(name, text):
            for a, b in sorted(AR.literal_spans(code)):
                lit = code[a + 1:b]
                if 3 <= len(lit) <= 200 and _ENDPOINT.match(lit) and lit not in seen:
                    seen[lit] = (name, off + a)
    return [Surface(kind="js_endpoint", key=f"js_endpoint:{lit}", label=lit, app=site,
                    effect={"effect": "", "source": "none"}, invoke={"via": "api_tier", "path": lit},
                    interface="unknown", cite=_cite(f, f"offset {o}"),
                    notes=["a literal in the bundle: compile + prove with the API tier before use"])
            for lit, (f, o) in sorted(seen.items())]


_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)


def embedded_openapi(html: str) -> List[Dict[str, Any]]:
    """OpenAPI / Swagger documents embedded in a page's JSON script blocks (a docs page often ships its spec inline)."""
    out = []
    for m in _SCRIPT_BLOCK.finditer(html or ""):
        a = _attrs(m.group(1))
        if "json" not in (a.get("type") or "").lower():
            continue
        try:
            d = json.loads(m.group(2))
        except (ValueError, RecursionError):
            continue
        if isinstance(d, dict) and (str(d.get("openapi") or "").startswith("3") or d.get("swagger")):
            out.append(d)
    return out


_INTERACTIVE = re.compile(r"<(a\b[^>]*\bhref|button\b|input\b|select\b|textarea\b|[a-z]+\b[^>]*\brole=[\"']?"
                          r"(?:button|link|menuitem|tab|checkbox|textbox|option)|[a-z]+\b[^>]*\bcontenteditable)", re.I)
_CANVAS = re.compile(r"<canvas\b([^>]*)>", re.I)
CANVAS_MAX_INTERACTIVE = 40
CANVAS_MIN_AREA_SHARE = 0.5


def canvas_profile(html: str = "", *, dom: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Is this a canvas app (Figma-like: the work surface is a canvas with no elements)? From a saved HTML document
    (``<canvas>`` tags, interactive elements) or a DOM summary a live reader produced (``{"canvas_area_share",
    "interactive", "viewport"}``). A canvas app steers mapping to the tiers 1-3; the DOM keeps only its chrome, and
    vision is the explicit last resort."""
    if dom is not None:
        share = float(dom.get("canvas_area_share") or 0.0)
        inter = int(dom.get("interactive") or 0)
        canv = int(dom.get("canvases") or (1 if share > 0 else 0))
    else:
        canv = 0
        share = 0.0
        vw, vh = 1440.0, 900.0
        for m in _CANVAS.finditer(html or ""):
            canv += 1
            a = _attrs(m.group(1))
            try:
                w, h = float(a.get("width") or 0), float(a.get("height") or 0)
            except ValueError:
                w = h = 0.0
            if w and h:
                share = max(share, min(1.0, (w * h) / (vw * vh)))
            elif re.search(r"(100(vw|vh|%))", a.get("style", "") + " " + a.get("class", "")):
                share = max(share, 1.0)
        inter = len(_INTERACTIVE.findall(html or ""))
    is_canvas = canv > 0 and (share >= CANVAS_MIN_AREA_SHARE or inter <= CANVAS_MAX_INTERACTIVE)
    return {"canvases": canv, "canvas_area_share": round(share, 3), "interactive": inter, "canvas_app": is_canvas,
            "steer": ("canvas app: use tiers 1-3 (official API / plugin API, the app's own requests); the DOM serves "
                      "only the chrome; vision is the explicit last resort") if is_canvas else ""}


# ---------------------------------------------------------------------------
# 4. Open-source code as a knowledge source (the hook and the policy; no network in this phase)
# ---------------------------------------------------------------------------

#: OSI-approved licenses whose code may be READ for interface facts (a fact - a command id, a file format field - is
#: not code; nothing is copied, nothing is run). Unknown or proprietary: not used.
OSS_LICENSES = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "MPL-2.0", "GPL-2.0-only",
                          "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later", "LGPL-2.1-only",
                          "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later", "AGPL-3.0-only",
                          "AGPL-3.0-or-later", "EPL-2.0", "Zlib", "Unlicense", "0BSD", "BSL-1.0"})


def oss_policy(source: Dict[str, Any]) -> Dict[str, Any]:
    """May this open-source checkout be read for interface facts? ``source``: ``{"app", "license" (SPDX id from its
    LICENSE file), "path" (a LOCAL checkout) | "url", "network": bool}``. Rules: an OSI license from the list; a local
    checkout (no fetch in this offline phase: a URL alone is ``deferred``); read-only; never executed, imported or
    built; only facts (command ids, titles, file-format fields) are kept, each with its file and license."""
    lic = str(source.get("license") or "")
    if lic not in OSS_LICENSES:
        return {"allowed": False, "reason": f"license {lic or 'unknown'!r} is not an OSI license on the list: not used"}
    if not source.get("path"):
        if source.get("url"):
            return {"allowed": False, "deferred": True,
                    "reason": "network fetch of source code is disabled in this offline phase (a local checkout only)"}
        return {"allowed": False, "reason": "no local checkout"}
    return {"allowed": True, "reason": "", "rules": ["read-only", "never run / import / build",
                                                     "facts only, each cited with file and license"]}


def vscode_contributes_surfaces(package: Dict[str, Any], *, app: str = "", license: str = "",
                                cite_file: str = "package.json") -> List[Surface]:
    """A VS Code-style ``package.json`` command registry (``contributes.commands`` + ``keybindings``) -> one
    ``oss_registry`` surface per command id: its title and declared key binding. Facts only."""
    contrib = (package or {}).get("contributes") or {}
    keys = {}
    for kb in contrib.get("keybindings") or []:
        if isinstance(kb, dict) and kb.get("command"):
            keys.setdefault(kb["command"], kb.get("mac") or kb.get("key") or "")
    out = []
    for i, c in enumerate(contrib.get("commands") or []):
        if not isinstance(c, dict) or not c.get("command"):
            continue
        title = _key(c.get("title"))
        declared, verb = _verb_effect(title)
        out.append(Surface(kind="oss_registry", key=f"oss_registry:{c['command']}", label=title or c["command"], app=app,
                           effect={"effect": "", "declared": declared, "verb": verb,
                                   "source": "title" if declared else "none"},
                           invoke={"via": "command_registry", "command": c["command"], "keybinding": keys.get(c["command"], "")},
                           interface="partial", cite=_cite(cite_file, f"/contributes/commands/{i}"),
                           notes=[f"license {license}" if license else "license unrecorded"]))
    return out


# ---------------------------------------------------------------------------
# 5. The ranker
# ---------------------------------------------------------------------------

def strength(s: Surface) -> int:
    """Within a tier, smaller is better: a declared effect with typed parameters (0), a declared effect from a title
    verb (1), a declared surface without an effect (2), a partial surface (3), an unknown interface (4); a surface
    whose effect is irreversible sorts last within its tier (+5)."""
    src = (s.effect or {}).get("source") or "none"
    base = {"protocol": 0, "declared": 0, "title": 1}.get(src, 2)
    if s.interface == "partial":
        base = max(base, 3)
    elif s.interface == "unknown":
        base = 4
    if (s.effect or {}).get("effect") in IRREVERSIBLE or (s.effect or {}).get("declared") == "delete":
        base += 5
    return base


def rank(surfaces: Iterable[Surface]) -> List[Surface]:
    """The ladder order: tier, then strength, then fewer one-time grants, then the brief's kind order, then key."""
    return sorted(surfaces, key=lambda s: (s.tier, strength(s), len(s.requires), KIND_ORDER.get(s.kind, 99), s.key))


def ladder_report(surfaces: Sequence[Surface], *, target: str = "", canvas: Optional[Dict[str, Any]] = None,
                  notes: Sequence[str] = ()) -> Dict[str, Any]:
    """The ranked surfaces of one app / site with per-kind counts, the best surface per map effect, and the
    steering: what serves before AX, and (for a canvas app) that vision is the explicit last resort."""
    ranked = rank(surfaces)
    counts: Dict[str, int] = {}
    for s in ranked:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    best: Dict[str, Dict[str, Any]] = {}
    for s in ranked:
        e = (s.effect or {}).get("effect")
        if e in MAP_EFFECTS and s.interface == "declared" and e not in best:
            best[e] = {"kind": s.kind, "key": s.key, "label": s.label, "tier": s.tier}
    steer = []
    if canvas and canvas.get("canvas_app"):
        steer.append(canvas["steer"])
    top = ranked[0].tier if ranked else 5
    if top >= 5:
        steer.append("no declared surface above AX/DOM: AX/DOM, then vision as the explicit last resort")
    return {"format": FORMAT, "target": target, "counts": counts, "surfaces": [s.to_dict() for s in ranked],
            "best_by_effect": best, "canvas": canvas or {}, "steer": steer, "notes": list(notes),
            "ladder": [{"kind": k, "tier": t, "what": w} for k, t, w in LADDER]}


# ---------------------------------------------------------------------------
# 6. The bundle reader (read-only file reads inside a bundle; never launches or scripts an app)
# ---------------------------------------------------------------------------

#: where host apps live (the census's corpus) and Finder's place (task measure only)
APP_ROOTS = ("/Applications", "/System/Applications")
CORE_SERVICES = "/System/Library/CoreServices"
MAX_READ = 16 * 1024 * 1024
SYSTEM_SDEF_DIR = "/System/Library/ScriptingDefinitions/"

#: the scripting-dictionary reader: ``(sdef_xml, include_reader) -> commands`` (``desktop_discover.parse_sdef``'s
#: shape). :func:`set_sdef_reader` plugs a dictionary-driven AppleScript reader in here instead of keeping two
#: readers.
_SDEF_READER: Optional[Callable[[str, Dict[str, str]], List[Dict[str, Any]]]] = None


def set_sdef_reader(fn: Optional[Callable[[str, Dict[str, str]], List[Dict[str, Any]]]]) -> None:
    global _SDEF_READER
    _SDEF_READER = fn


_DOCTYPE = re.compile(r"<!DOCTYPE\s[^\[>]*(?:\[.*?\]\s*)?>", re.S)


def strip_doctype(xml_text: str) -> str:
    """Remove a DOCTYPE, an internal subset included (iWork's dictionaries declare ``<!ENTITY ...>`` inside one, which
    a plain ``<!DOCTYPE[^>]*>`` strip leaves half in place: the XML parser then fails)."""
    return _DOCTYPE.sub("", xml_text, count=1)


def _default_sdef_reader(xml_text: str, includes: Dict[str, str]) -> List[Dict[str, Any]]:
    from anyuse.execution import desktop_discover as DD
    return DD.parse_sdef(xml_text, includes)


def _read(path: str, limit: int = MAX_READ) -> Optional[bytes]:
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _inside(root: str, path: str) -> bool:
    r = os.path.realpath(root)
    p = os.path.realpath(path)
    return p == r or p.startswith(r.rstrip("/") + "/")


def find_bundle(name: str, roots: Sequence[str] = APP_ROOTS + (CORE_SERVICES,)) -> Optional[str]:
    """``Notes`` / ``Notes.app`` / a bundle path -> the bundle's path (case-insensitive, one subfolder deep)."""
    if name.endswith(".app") and os.path.isdir(name):
        return name
    want = (name[:-4] if name.lower().endswith(".app") else name).lower()
    for root in roots:
        for pat in ("*.app", "*/*.app"):
            for p in sorted(glob.glob(os.path.join(root, pat))):
                if os.path.basename(p)[:-4].lower() == want:
                    return p
    return None


def list_bundles(roots: Sequence[str] = APP_ROOTS) -> List[str]:
    """Every ``.app`` directly in a root or one folder below it (``/Applications/Adobe Acrobat DC/...``,
    ``/System/Applications/Utilities/...``), sorted, de-duplicated by real path."""
    seen, out = set(), []
    for root in roots:
        for pat in ("*.app", "*/*.app"):
            for p in sorted(glob.glob(os.path.join(root, pat))):
                rp = os.path.realpath(p)
                if rp not in seen and os.path.isdir(p):
                    seen.add(rp)
                    out.append(p)
    return out


def _plist(data: Optional[bytes]) -> Dict[str, Any]:
    if not data:
        return {}
    try:
        d = plistlib.loads(data)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 - an unreadable plist yields nothing (old-style text plists included)
        return {}


def _listing(bundle: str) -> List[Dict[str, Any]]:
    """Files in the CLI places, with their Mach-O type read from their first bytes."""
    out = []
    for sub in ("Contents/MacOS", "Contents/Helpers", "Contents/SharedSupport/bin", "Contents/Resources"):
        d = os.path.join(bundle, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            x = bool(st.st_mode & 0o111)
            ft = ""
            if x and not name.endswith(_LIB_SUFFIX):
                try:
                    with open(p, "rb") as fh:
                        head = fh.read(32)

                        def read_at(off: int, n: int, _fh=fh) -> bytes:
                            _fh.seek(off)
                            return _fh.read(n)
                        ft = macho_filetype(head, read_at)
                except OSError:
                    ft = ""
            out.append({"path": f"{sub}/{name}", "executable": x, "filetype": ft})
    return out


@dataclass
class BundleReport:
    path: str
    app: str
    bundle_id: str
    version: str = ""
    surfaces: List[Surface] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    scripting: str = ""                       # sdef | script_suite | standard_suite | ""

    def kinds(self) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for s in self.surfaces:
            c[s.kind] = c.get(s.kind, 0) + 1
        return c

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "app": self.app, "bundle_id": self.bundle_id, "version": self.version,
                "scripting": self.scripting, "kinds": self.kinds(), "files_read": list(self.files_read),
                "notes": list(self.notes), "surfaces": [s.to_dict() for s in rank(self.surfaces)]}


def discover_bundle(path: str) -> BundleReport:
    """Every declared surface of one app bundle, read-only. Reads ``Contents/Info.plist``, the App Intents
    metadata (the app's and its extensions'), ``.intentdefinition`` files, the scripting dictionary named by
    ``OSAScriptingDefinition`` (with includes from the system's ScriptingDefinitions folder only) and the first
    bytes of executables in the CLI places. Opens nothing for writing, spawns nothing, follows no symlink out."""
    info_p = os.path.join(path, "Contents", "Info.plist")
    info = _plist(_read(info_p))
    bid = str(info.get("CFBundleIdentifier") or "")
    app = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or os.path.basename(path)[:-4])
    rep = BundleReport(path=path, app=app, bundle_id=bid,
                       version=str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""))
    if not info:
        rep.notes.append("no readable Info.plist")
        return rep
    rep.files_read.append("Contents/Info.plist")
    rep.surfaces += url_scheme_surfaces(info, app=app, bundle_id=bid)
    rep.surfaces += service_surfaces(info, app=app, bundle_id=bid)
    # App Intents: the app's own metadata and its extensions'
    ai_paths = [os.path.join(path, "Contents/Resources/Metadata.appintents/extract.actionsdata")]
    for ext in ("Contents/PlugIns/*.appex", "Contents/Extensions/*.appex"):
        ai_paths += [os.path.join(p, "Contents/Resources/Metadata.appintents/extract.actionsdata")
                     for p in sorted(glob.glob(os.path.join(path, ext)))]
    for p in ai_paths:
        data = _read(p)
        if data is None or not _inside(path, p):
            continue
        rel = os.path.relpath(p, path)
        rep.files_read.append(rel)
        try:
            doc = json.loads(data)
        except (ValueError, RecursionError):
            rep.notes.append(f"{rel}: not JSON")
            continue
        rep.surfaces += appintent_surfaces(doc, app=app, bundle_id=bid, cite_file=rel,
                                           sha=hashlib.sha256(data).hexdigest()[:16])
    # legacy SiriKit intent definitions (Base.lproj or the Resources root)
    for p in sorted(set(glob.glob(os.path.join(path, "Contents/Resources/*.intentdefinition")) +
                        glob.glob(os.path.join(path, "Contents/Resources/*.lproj/*.intentdefinition")))):
        data = _read(p)
        if data is None or not _inside(path, p):
            continue
        rel = os.path.relpath(p, path)
        rep.files_read.append(rel)
        rep.surfaces += intentdefinition_surfaces(_plist(data), app=app, bundle_id=bid, cite_file=rel,
                                                  sha=hashlib.sha256(data).hexdigest()[:16])
    # the scripting dictionary
    sdef_name = str(info.get("OSAScriptingDefinition") or "")
    if sdef_name:
        sp = os.path.join(path, "Contents/Resources", sdef_name if "." in sdef_name else sdef_name + ".sdef")
        data = _read(sp) if _inside(path, sp) else None
        if data is not None:
            rel = os.path.relpath(sp, path)
            rep.files_read.append(rel)
            text = data.decode("utf-8", "replace")
            includes: Dict[str, str] = {}
            for href in re.findall(r"<xi:include[^>]*href=\"([^\"]+)\"", text):
                local = re.sub(r"^file://(localhost)?", "", href)
                if local.startswith(SYSTEM_SDEF_DIR) and ".." not in local:
                    inc = _read(local)
                    if inc is not None:
                        includes[href] = strip_doctype(inc.decode("utf-8", "replace"))
                        rep.files_read.append(local)
            try:
                cmds = (_SDEF_READER or _default_sdef_reader)(strip_doctype(text), includes)
                rep.surfaces += scripting_surfaces(cmds, app=app, bundle_id=bid, cite_file=rel)
                rep.scripting = "sdef"
            except (ET.ParseError, ValueError) as exc:
                rep.notes.append(f"{rel}: unreadable dictionary ({exc.__class__.__name__})")
        else:
            rep.notes.append(f"OSAScriptingDefinition {sdef_name!r} not found in Resources")
    scriptable = info.get("NSAppleScriptEnabled") in (True, "YES", "true", 1)
    suites = glob.glob(os.path.join(path, "Contents/Resources/*.scriptSuite"))
    if not rep.scripting and suites and scriptable:
        rep.scripting = "script_suite"
        rep.notes.append("legacy .scriptSuite dictionary (commands from the implicit Cocoa suites)")
    elif not rep.scripting and suites:
        rep.notes.append("a .scriptSuite file without NSAppleScriptEnabled: not scriptable")
    if not rep.scripting and scriptable:
        rep.scripting = "standard_suite"
        rep.notes.append("NSAppleScriptEnabled without a dictionary of its own: the standard suite only")
    rep.surfaces += cli_surfaces(_listing(path), main_executable=str(info.get("CFBundleExecutable") or ""), app=app,
                                 bundle_id=bid)
    return rep


# ---------------------------------------------------------------------------
# 7. Sites (offline: artifacts given, nothing fetched)
# ---------------------------------------------------------------------------

def site_host(target: str) -> str:
    t = target.strip()
    if "://" not in t:
        t = "https://" + t
    return (urlsplit(t).hostname or "").lower()


def looks_like_site(target: str) -> bool:
    t = target.strip().lower()
    return "://" in t or bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?(/.*)?", t)) and not t.endswith(".app")


def discover_site(target: str, artifacts: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A site's surfaces from offline artifacts (``{name: text}``: a saved page ``*.html``, a ``Link`` header file
    ``*.link``, an API catalog / OpenAPI / introspection / compiled_api ``*.json``, JS bundles ``*.js``). With
    ``ANYUSE_REFUSE_GOV_HOSTS`` on, a government or public-body host is refused before anything. Nothing is fetched:
    the well-known probes are
    listed for a later, approved online phase."""
    from anyuse.execution import gov_hosts
    host = site_host(target)
    why = gov_hosts.government_refusal(host)
    if why:
        return {"format": FORMAT, "target": host, "refused": f"government / public-body host: {why}", "surfaces": []}
    arts = artifacts or {}
    surfaces: List[Surface] = []
    notes: List[str] = []
    html = "\n".join(t for n, t in arts.items() if n.endswith((".html", ".htm")))
    links = service_links(html, "\n".join(t for n, t in arts.items() if n.endswith(".link")), base=f"https://{host}/")
    for ln in links:
        surfaces.append(Surface(kind="api_catalog", key=f"api_catalog:{ln['rel']}:{ln['href']}", label=ln["href"],
                                app=host, effect={"effect": "", "source": "declared"},
                                invoke={"via": "read_spec", "rel": ln["rel"], "href": ln["href"]}, interface="partial",
                                cite=_cite(ln["from"], ln["rel"])))
    js: Dict[str, str] = {n: t for n, t in arts.items() if n.endswith((".js", ".mjs"))}
    for name, text in sorted(arts.items()):
        if not name.endswith(".json"):
            continue
        try:
            d = json.loads(text)
        except (ValueError, RecursionError):
            notes.append(f"{name}: not JSON")
            continue
        if isinstance(d, dict) and "linkset" in d:
            surfaces += api_catalog_surfaces(d, site=host, cite_file=name)
        elif isinstance(d, dict) and (str(d.get("openapi") or "").startswith("3") or d.get("swagger")):
            surfaces += openapi_surfaces(d, site=host, cite_file=name)
        elif isinstance(d, dict) and ("__schema" in d or "__schema" in (d.get("data") or {})):
            surfaces += graphql_surfaces(d, site=host, cite_file=name)
        elif isinstance(d, dict) and (d.get("template") or d.get("url")):
            surfaces += compiled_api_surfaces(d, site=host, cite_file=name)
    for spec in embedded_openapi(html):
        surfaces += openapi_surfaces(spec, site=host, cite_file="page (embedded)", kind="embedded_openapi")
    if html:
        js[f"https://{host}/ (inline)"] = html
    surfaces += js_endpoint_surfaces(js, site=host)
    canvas = canvas_profile(html) if html else None
    if html:
        surfaces.append(Surface(kind="dom", key="dom:page", label="the DOM", app=host,
                                effect={"effect": "", "source": "none"}, invoke={"via": "dom"},
                                interface="partial", cite=_cite("page"),
                                notes=["chrome only: the work surface is a canvas"] if canvas and canvas["canvas_app"]
                                else []))
    surfaces.append(Surface(kind="vision", key="vision", label="screenshots + a vision model", app=host,
                            effect={"effect": "", "source": "none"}, invoke={"via": "vision"}, interface="partial",
                            notes=["the explicit last resort"]))
    rep = ladder_report(surfaces, target=host, canvas=canvas, notes=notes)
    rep["deferred_probes"] = well_known_probes(host)
    rep["deferred_reason"] = "offline phase: nothing fetched; these are read only in an owner-approved online phase"
    return rep


# ---------------------------------------------------------------------------
# 8. The map: surface transitions, serving a step, the read-back, the planner's preference
# ---------------------------------------------------------------------------

#: the item kind a recorded step acts on, per (app, effect) - the bench apps (desktop_e2e.BUNDLES)
STEP_ITEM_KIND = {
    ("Finder", "open_item"): "file", ("Finder", "select_item"): "file", ("Finder", "navigate"): "folder",
    ("Finder", "read_content"): "file", ("Notes", "open_item"): "note", ("Notes", "select_item"): "note",
    ("Notes", "read_content"): "note", ("Notes", "navigate"): "folder", ("Notes", "field_rw"): "note",
    ("TextEdit", "open_item"): "document", ("TextEdit", "doc_save"): "document",
    ("TextEdit", "read_content"): "document",
}


@dataclass
class Need:
    """What a step needs: the app, the map effect, the item kind, the mode (doc_save), the option (a menu group's
    choice) and the values it has (``name``, ``path``, ``text``, ``lookup``, ``option``...)."""
    app: str
    effect: str
    item_kind: str = ""
    mode: str = ""
    option: str = ""
    values: Dict[str, str] = field(default_factory=dict)


def need_from_record(step: Dict[str, Any], context: Optional[Dict[str, str]] = None) -> Need:
    """A recorded step (``{step, resolved, ...}``, the recorded format) + the run's context (``finder_folder``,
    ``notes_folder``, ``document``) -> its need. A value is present only when the record or the context holds it."""
    st = step.get("step") or {}
    res = step.get("resolved") or {}
    ctx = context or {}
    app, eff = str(st.get("app") or ""), str(st.get("effect") or "")
    kind = STEP_ITEM_KIND.get((app, eff), "")
    if eff == "object_create":
        kind = str(st.get("target") or res.get("kind") or "")
    vals: Dict[str, str] = {}
    label = str(res.get("label") or st.get("target") or "")
    if eff in ("open_item", "select_item", "navigate", "object_create") and label:
        vals["name"] = str(st.get("value") or label) if eff == "object_create" else label
    if app == "Finder" and ctx.get("finder_folder"):
        base = ctx["finder_folder"]
        if eff == "navigate":
            chk = re.match(r"^container verified: (/.+)$", str(step.get("check") or ""))
            vals["path"] = chk.group(1) if chk else ""
        elif eff in ("open_item", "select_item") and label:
            vals["path"] = base.rstrip("/") + "/" + label
        elif eff == "object_create":
            vals["path"] = base
        vals = {k: v for k, v in vals.items() if v}
    if app == "TextEdit" and ctx.get("document"):
        vals["path"] = ctx["document"]
    if eff in ("read_content", "field_rw") and app == "Notes" and ctx.get("note_name"):
        vals["name"] = ctx["note_name"]
    if eff == "scoped_find" and st.get("value"):
        vals["lookup"] = str(st.get("value"))
    if eff in ("doc_save", "field_rw") and st.get("value"):
        vals["text"] = str(st.get("value"))
    opt = ""
    if eff in ("toggle_select", "flag_toggle"):
        opt = str(st.get("value") if eff == "toggle_select" else st.get("target") or "")
        if eff == "flag_toggle":
            vals["setting"] = str(st.get("value") or "")
    return Need(app=app, effect=eff, item_kind=kind, mode=str(st.get("mode") or ""), option=opt, values=vals)


def _option_matches(option: str, cases: Sequence[str]) -> str:
    """A menu option label (German / English, the map's synonym table) -> the enum case it names, or ``""``."""
    if not option:
        return ""
    try:
        from anyuse.execution import map_compile as M
        phrases = [p.lower() for p in M.SYNONYMS.get(option, ())] + [option.lower()]
    except Exception:  # noqa: BLE001
        phrases = [option.lower()]
    for c in cases:
        spoken = " ".join(camel_tokens(c))
        if any(spoken == p or spoken in p.split(" / ") or p == c.lower() for p in phrases):
            return c
    return ""


def _kinds_fit(need_kind: str, surface_kinds: Sequence[str]) -> bool:
    if not need_kind or not surface_kinds:
        return not need_kind
    if need_kind in surface_kinds:
        return True
    return need_kind == "document" and "file" in surface_kinds


#: effects whose step names an item: the surface must take that identity as a parameter
IDENTITY_EFFECTS = frozenset({"open_item", "select_item", "navigate", "read_content", "doc_save", "field_rw"})
#: which of the step's values a text parameter takes, per effect
TEXT_SOURCE = {"scoped_find": "lookup", "doc_save": "text", "field_rw": "text", "object_create": "name"}


def _bind_param(p: Dict[str, Any], need: Need) -> Tuple[str, str]:
    """One parameter against the need's values -> (``bound`` | ``unbound`` | ``unsure``, what binds it / why)."""
    t = p.get("type") or "unknown"
    name = p.get("name") or ""
    if t == "entity":
        r = (p.get("resolve") or {}).get("by")
        if r == "file_path":
            return ("bound", "path") if need.values.get("path") else ("unbound", f"{name}: a file path is needed")
        if r == "name_query":
            return ("bound", "name") if need.values.get("name") else ("unbound", f"{name}: a name is needed")
        return "unsure", f"{name}: {(p.get('resolve') or {}).get('why') or 'no declared resolution'}"
    if t == "file":
        return ("bound", "path") if need.values.get("path") else ("unbound", f"{name}: a file path is needed")
    if t in TEXT_TYPES:
        src = TEXT_SOURCE.get(need.effect, "")
        if src and need.values.get(src):
            return "bound", src
        if not src and need.effect == "scoped_find" and need.values.get("lookup"):
            return "bound", "lookup"
        return "unbound", f"{name}: no {src or 'text'} value in the step"
    if t == "enum":
        cases = p.get("cases") or []
        if name in ("changeOperation", "operation") and {"enable", "disable"} <= set(cases):
            return "bound", "enable"
        case = _option_matches(need.option, cases)
        return ("bound", f"case {case}") if case else ("unbound", f"{name}: no case of {p.get('enum')} named by the step")
    return "unsure", f"{name}: type {t} (no declared binding)"


def serves(s: Surface, need: Need) -> Tuple[str, str, Dict[str, str]]:
    """Does surface ``s`` serve ``need``? -> (``served`` | ``uncertain`` | ``no``, why, bindings param -> value
    source). Served only when the declared effect equals the need's, the item kind fits, the doc mode matches, every
    required parameter binds (an entity by a file path or a declared name query; text from the step; an enum case
    named by the step's option), and the step's identity is TAKEN by a parameter (a surface that cannot name the item
    - "show the quick note", "go to folder" without a target - does not serve "open the note X"). An undeclared
    interface, an entity without a declared resolution, or a binary setting standing for an option group is
    ``uncertain``: listed, never chosen. An irreversible surface never substitutes for another effect."""
    if s.kind == "rea_adapter":
        from anyuse.execution.rea_mapping import map_bindings
        return map_bindings(s, need)
    e = s.effect or {}
    eff = e.get("effect") or ""
    if eff in IRREVERSIBLE and need.effect not in IRREVERSIBLE:
        return "no", "irreversible", {}
    if s.interface == "unknown":
        return "no", "interface unknown", {}
    if need.effect == "toggle_select" and eff == "flag_toggle" and e.get("options"):
        case = _option_matches(need.option, e["options"])
        if case:
            return "uncertain", f"a binary setting {case!r} stands for an option group (exclusivity undeclared)", \
                {"setting": case}
        return "no", "no case names the option", {}
    if eff != need.effect:
        if s.kind == "url_scheme" and s.interface == "partial" and not s.invoke.get("generic") and \
                need.effect in ("scoped_find", "open_item", "navigate"):
            return "uncertain", "the scheme is declared, its paths / parameters are not", {}
        return "no", f"effect {eff or 'undeclared'} != {need.effect}", {}
    if not _kinds_fit(need.item_kind, e.get("item_kinds") or []):
        return "no", f"item kind {need.item_kind!r} not in {e.get('item_kinds')}", {}
    if need.effect == "doc_save" and (e.get("mode") or "") != (need.mode or ""):
        return "no", f"mode {e.get('mode')!r} != {need.mode!r}", {}
    if s.interface != "declared":
        return "uncertain", "partially declared", {}
    binds: Dict[str, str] = {}
    unsure: List[str] = []
    for p in s.params:
        state, what = _bind_param(p, need)
        if state == "bound":
            binds[p.get("name") or ""] = what
        elif p.get("optional"):
            continue
        elif state == "unbound":
            return "no", what, {}
        else:
            unsure.append(what)
    used = set(binds.values())
    if need.effect in IDENTITY_EFFECTS and (need.values.get("name") or need.values.get("path")) and \
            not used & {"name", "path"}:
        why = "the surface takes no item identity: it cannot name the step's item"
        return ("uncertain", "; ".join(unsure + [why]), binds) if unsure else ("no", why, {})
    if need.effect == "object_create":
        if need.values.get("name") and "name" not in used:
            return "no", "the surface takes no name for the new item", {}
        if need.values.get("path") and "path" not in used:
            return "no", "the surface takes no container for the new item", {}
    if need.effect == "scoped_find" and need.values.get("lookup") and "lookup" not in used:
        return "no", "the surface takes no search term", {}
    if need.effect in ("doc_save", "field_rw") and need.values.get("text") and "text" not in used:
        return "no", "the surface takes no text", {}
    if unsure:
        return "uncertain", "; ".join(unsure), binds
    return "served", "declared effect, kinds and parameters bind", binds


#: which source can read back which effect, independently of the actor
READBACK = {
    "open_item": "the front item's identity (the window / editor title, or the document's path)",
    "navigate": "the container the window shows (container_scope: Apple Events path or AX)",
    "select_item": "the selection's identity",
    "object_create": "the new item exists in its container with the requested name",
    "doc_save": "the saved bytes equal the composed text (desktop_e2e4.check_text)",
    "scoped_find": "a search field holds the term / the result list is for the term",
    "read_content": "a second read through another source returns the same text",
    "toggle_select": "the group's one mark sits on the chosen option",
    "flag_toggle": "the flag's mark reads the requested state",
    "field_rw": "the field's value reads back as written",
}


def readback_for(s: Surface, need: Need, available: Sequence[str] = ("ax", "scripting", "file")) -> Dict[str, Any]:
    """The declared expected effect + an INDEPENDENT read-back for a surface step: a reader of another kind than the
    actor (an App Intent step is read back by AX or the dictionary, a script step by AX or the file, never by itself).
    No independent reader available -> ``{"via": "none"}``: the step's outcome can only be ``uncertain``."""
    actor = s.kind
    order = {"doc_save": ("file", "scripting", "ax"), "read_content": ("scripting", "ax", "file")}.get(
        need.effect, ("ax", "scripting", "file"))
    if actor == "rea_adapter":
        observer = (s.invoke.get("candidate") or {}).get("observer") or {}
        order = ("ax",) if observer.get("kind") == "dictionary_ax" or s.invoke["candidate"].get("backend") == "guest_learned_ax_v1" else ("file",)
    via = next((r for r in order if r in available and r != actor and not (actor == "scripting" and r == "scripting")),
               "none")
    spec = {"expect": {"effect": need.effect, "item_kind": need.item_kind, "mode": need.mode,
                       "values": {k: v for k, v in need.values.items() if k in ("name", "path", "text", "lookup")},
                       "option": need.option},
            "readback": {"via": via, "check": READBACK.get(need.effect, "")} if via != "none" else
            {"via": "none", "check": "", "verdict_if_unread": "uncertain"}}
    if actor == "rea_adapter" and observer.get("kind") == "dictionary_ax" and via == "ax":
        from .rea_mapping import digest
        spec["readback"].update({"observer": "dictionary_ax", "candidate_sha256": digest(s.invoke["candidate"]),
            "dictionary_label": observer.get("dictionary_label"),
            "dictionary_path": observer.get("dictionary_path"),
            "check": "search term, selected dictionary and complete definition match the independent asset reference"})
    if actor == "rea_adapter" and s.invoke["candidate"].get("backend") == "guest_learned_ax_v1" and via == "ax":
        from .rea_mapping import digest
        candidate = s.invoke["candidate"]
        spec["readback"].update(observer="learned_ax", candidate_sha256=digest(candidate),
                               fields=list(candidate["reference"]["fields"]),
                               param_sources={p["name"]: p["source"] for p in candidate["params"]},
                               scope=candidate.get("scope"),
                               check="complete independently referenced output matches the frozen learned recipe")
    return spec


def judge_readback(spec: Dict[str, Any], actor: str, observation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The surface step's verdict from its read-back: ``verified`` only when an observation from a source OTHER than
    the actor matches the expected values; a mismatch is ``refuted``; no observation, the actor's own answer, or an
    observation without the expected fields is ``uncertain`` (unknown = uncertain)."""
    rb = (spec or {}).get("readback") or {}
    if rb.get("via") in (None, "none") or not observation:
        return {"verdict": "uncertain", "why": "no independent read-back"}
    if observation.get("source") in (actor, None) or observation.get("source") != rb.get("via"):
        return {"verdict": "uncertain", "why": f"read-back source {observation.get('source')!r} is not the declared "
                                               f"independent reader {rb.get('via')!r}"}
    exp = ((spec.get("expect") or {}).get("values")) or {}
    if rb.get("observer") == "learned_ax":
        result = observation.get("rea_result") or {}
        if (not isinstance(result, dict) or result.get("observer") != "learned_ax" or
                result.get("candidate_sha256") != rb.get("candidate_sha256")):
            return {"verdict": "uncertain", "why": "complete independent learned-recipe receipt required"}
        expected, read, run, inputs = (result.get(k) or {} for k in ("expected", "seen", "run", "inputs"))
        values = exp
        scope = rb.get("scope")
        if (result.get("verdict") != "verified" or not all(isinstance(x, dict) for x in (expected, read, run, inputs)) or
                expected != read or set(expected) != set(rb["fields"]) or not all(expected.values()) or
                run.get("returncode") != 0 or run.get("timed_out") or run.get("permission") not in (0, "not_required") or
                any(inputs.get(name) != values.get(source) for name, source in rb["param_sources"].items()) or
                (scope and values.get(scope["source"]) != scope["value"])):
            return {"verdict": "refuted", "why": "learned recipe did not verify this task's complete output"}
        return {"verdict": "verified", "why": "complete result matches the independent reference through shared AX readers"}
    if rb.get("observer") == "dictionary_ax":
        result = observation.get("rea_result") or {}
        if (not isinstance(result, dict) or result.get("observer") != "ax_dictionary" or
                result.get("candidate_sha256") != rb.get("candidate_sha256")):
            return {"verdict": "uncertain", "why": "complete independent REA definition receipt required"}
        if result.get("verdict") != "verified":
            return {"verdict": "refuted" if result.get("verdict") == "refuted" else "uncertain",
                    "why": "dictionary replay did not verify the definition"}
        expected, read = result.get("expected") or {}, result.get("seen") or {}
        run = result.get("run") or {}
        if not all(isinstance(v, dict) for v in (expected, read, run)) or not exp.get("path"):
            return {"verdict": "uncertain", "why": "complete definition and explicit dictionary scope required"}
        if (expected != read or not expected.get("definition") or expected.get("query") != exp.get("lookup") or
                exp.get("path") != rb.get("dictionary_path") or
                expected.get("dictionary") != rb.get("dictionary_label") or run.get("returncode") != 0 or
                run.get("timed_out") or run.get("permission") != 0):
            return {"verdict": "refuted", "why": "search field alone does not establish the requested definition"}
    seen = observation.get("values") or {}
    keys = [k for k in exp if k in seen]
    if not keys:
        return {"verdict": "uncertain", "why": "the observation carries none of the expected values"}
    bad = [k for k in keys if str(seen[k]) != str(exp[k])]
    if bad:
        return {"verdict": "refuted", "why": f"read back {bad[0]}={seen[bad[0]]!r}, expected {exp[bad[0]]!r}"}
    return {"verdict": "verified", "why": f"{', '.join(keys)} read back by {rb['via']}"}


def surface_transitions(surfaces: Sequence[Surface], *, app: str = "", bundle_id: str = "") -> List[Any]:
    """Discovered actions whose declared effect is a map effect -> ``map_compile.Transition``s (effect, intent,
    params, item kind, mode; ``ax`` / ``routine`` None: the surface is no AX hop). The transition's first source
    record carries the surface: kind, tier, invocation and the whole spec - the citation the planner carries into
    the plan."""
    from anyuse.execution import map_compile as M
    out = []
    for s in rank(surfaces):
        e = s.effect or {}
        eff = e.get("effect") or ""
        if eff not in MAP_EFFECTS or s.interface == "unknown":
            continue
        kinds = e.get("item_kinds") or []
        out.append(M.Transition(
            id=f"{bundle_id or s.bundle_id}:surface:{s.key}", app=app or s.app, bundle_id=bundle_id or s.bundle_id,
            effect=eff, intent=s.label,
            params=[{"name": p.get("name") or "", "kind": p.get("type") or ""} for p in s.params if not p.get("optional")],
            options=[{"label": o} for o in e.get("options") or []], ax=None, routine=None,
            sources=[{"kind": "surface", "ref": f"{s.cite.get('file', '')}#{s.cite.get('pointer', '')}",
                      "surface": s.kind, "tier": s.tier, "spec": s.to_dict()}],
            mode=str(e.get("mode") or ""), item_kind=kinds[0] if kinds else ""))
    return out


def surface_of(transition: Any) -> Optional[Surface]:
    """The surface a map transition stands for (its first source record), or None for an AX transition."""
    for src in getattr(transition, "sources", None) or []:
        if isinstance(src, dict) and src.get("kind") == "surface" and isinstance(src.get("spec"), dict):
            return Surface.from_dict(src["spec"])
    return None


def attach_surfaces(amap: Any, surfaces: Sequence[Surface], *, app: str = "", bundle_id: str = "") -> int:
    """Add the surface transitions to an ``AppMap`` (after its own transitions: the lexicon planner, which takes the
    first transition of an effect, keeps choosing what it chose; :func:`prefer_surfaces` upgrades steps). Returns how
    many were added."""
    n = 0
    for t in surface_transitions(surfaces, app=app, bundle_id=bundle_id):
        if amap.get(t.id) is None:
            n += 1
        amap.add(t)
    src = {"kind": "surfaces", "ref": f"surface_discovery:{bundle_id or app}", "count": n}
    if src not in amap.sources:
        amap.sources.append(src)
    return n


def need_from_plan_step(step: Any, amap: Any) -> Need:
    """A ``map_compile.PlanStep`` + its transition -> the need (its bound values; a ``${param}`` is no value)."""
    t = amap.get(step.transition)
    b = {k: str(v) for k, v in (step.bindings or {}).items() if not re.search(r"\$\{|\{\{", str(v))}
    vals: Dict[str, str] = {}
    for k in ("note", "folder", "file", "name"):
        if b.get(k):
            vals["name"] = b[k]
    if b.get("text"):
        vals["text"] = b["text"]
    if b.get("lookup"):
        vals["lookup"] = b["lookup"]
    if b.get("path"):
        vals["path"] = b["path"]
    surface = surface_of(t) if t is not None else None
    if surface is not None and surface.kind == "rea_adapter":
        for param in (surface.invoke.get("candidate") or {}).get("params", []):
            if param.get("name") in b and param.get("source") in ("path", "text", "name"):
                vals[param["source"]] = b[param["name"]]
    kind = (t.item_kind if t is not None else "") or next((k for k in ("note", "folder", "file") if k in b), "")
    if step.effect == "navigate":
        kind = "folder"
    return Need(app=step.app, effect=step.effect, item_kind=kind, mode=(t.mode if t is not None else ""),
                option=step.option or b.get("choice", ""), values=vals)


def prefer_surfaces(plan: Any, amap: Any, *, script_rung: Optional[Callable[[Any, Need], Tuple[bool, str]]] = None,
                    readers: Sequence[str] = ("ax", "scripting", "file")) -> List[Dict[str, Any]]:
    """Per plan step: the best surface above AX that SERVES it (ranked by the ladder), with its declared expected
    effect and independent read-back - or none (the step stays on its AX projection). ``script_rung(step, need)``
    answers for the AppleScript route (``(covered, shape)``); without one, scripting is not claimed. The AX
    projection stays every step's fallback; a failure after a surface acted is reported, never retried on AX."""
    surf: List[Surface] = []
    for t in amap.transitions:
        s = surface_of(t)
        if s is not None:
            surf.append(s)
    out = []
    for i, st in enumerate(plan.steps):
        need = need_from_plan_step(st, amap)
        t = amap.get(st.transition)
        bid = t.bundle_id if t is not None else ""
        cands, unsure = [], []
        for s in rank(x for x in surf if x.app == st.app or (bid and x.bundle_id == bid)):
            status, why, binds = serves(s, need)
            if status == "served":
                cands.append((s, why, binds))
            elif status == "uncertain":
                unsure.append({"kind": s.kind, "key": s.key, "why": why})
        script = None
        if script_rung is not None:
            ok, shape = script_rung(st, need)
            if ok:
                script = {"kind": "scripting", "key": f"applescript_route:{shape}", "tier": TIER["scripting"]}
        chosen = None
        if script is not None and (not cands or script["tier"] < cands[0][0].tier):
            chosen = {**script, "why": "the AppleScript route covers the step",
                      **readback_for(Surface(kind="scripting", key=script["key"], label=""), need, readers)}
        elif cands:
            s, why, binds = cands[0]
            chosen = {"kind": s.kind, "key": s.key, "label": s.label, "tier": s.tier, "bindings": binds,
                      "invoke": s.invoke, "requires": s.requires, "why": why, **readback_for(s, need, readers)}
        out.append({"step": i, "transition": st.transition, "effect": st.effect, "app": st.app,
                    "chosen": chosen, "fallback": "ax", "served_by": [c[0].key for c in cands] +
                    ([script["key"]] if script else []), "uncertain": unsure})
    return out


# ---------------------------------------------------------------------------
# 9. Invocations (pure: what a guest runner would execute; nothing runs here)
# ---------------------------------------------------------------------------

def shortcut_name(s: Surface) -> str:
    """The name of the Shortcut that holds an App Intent action (one manual import per action)."""
    return f"AnyUse {s.bundle_id} {s.invoke.get('action')}"[:120]


def invocation(s: Surface, values: Dict[str, str], *, url_template: str = "") -> Dict[str, Any]:
    """The exact command a guest runner would run for a surface step, from bound values. App Intents run through
    the named Shortcut (``shortcuts run NAME --input-path FILE``; the inputs as a JSON file); a URL scheme needs a
    template from a knowledge entry (the bundle declares none) and every value is percent-encoded; a service is
    ``NSPerformService``. A surface without an invocation answer is refused, never improvised."""
    if s.kind == "rea_adapter":
        from anyuse.execution.rea_mapping import bind, proof_status
        candidate = s.invoke.get("candidate") or {}
        ready, why = proof_status(candidate, s.invoke.get("proof"), s.invoke.get("target") or {})
        if not ready:
            return {"refused": why}
        try:
            preview = bind(candidate, values)
        except ValueError as exc:
            return {"refused": str(exc)}
        # No bare argv: replay must rehash the guest app and run the independent observer.
        return {"via": "rea_mapping.replay", "candidate": candidate, "proof": s.invoke["proof"],
                "inputs": dict(values), "invocation_preview" if isinstance(preview, dict) else "argv_preview": preview,
                "requires_vm": True}
    if s.kind == "app_intent":
        return {"argv": ["/usr/bin/shortcuts", "run", shortcut_name(s), "--input-path", "<inputs.json>"],
                "inputs": dict(values), "shortcut": shortcut_name(s), "action": s.invoke.get("action"),
                "needs": "the owner imports this shortcut once (a one-time grant, like the Automation prompt)"}
    if s.kind == "url_scheme":
        if not url_template:
            return {"refused": "a URL scheme's paths and parameters are not declared: a knowledge entry is needed"}
        scheme = s.invoke.get("scheme") or ""
        if not url_template.lower().startswith(scheme + ":"):
            return {"refused": f"template {url_template!r} is not of scheme {scheme!r}"}
        holes = re.findall(r"\{([a-z_]+)\}", url_template)
        if any(h not in values for h in holes):
            return {"refused": f"unbound: {[h for h in holes if h not in values]}"}
        url = url_template
        for h in holes:
            url = url.replace("{" + h + "}", quote(str(values[h]), safe=""))
        return {"argv": ["/usr/bin/open", url], "url": url}
    if s.kind == "service":
        return {"call": "NSPerformService", "service": s.invoke.get("title"), "input": values.get("input") or
                next(iter(values.values()), ""), "send_types": s.invoke.get("send_types")}
    if s.kind == "scripting":
        return {"via": "applescript_route", "event": s.invoke.get("event")}
    return {"refused": f"no invocation for a {s.kind} surface"}


# ---------------------------------------------------------------------------
# 10. Knowledge entries (a URL template, a documented API): hints until a live check proves them
# ---------------------------------------------------------------------------

def load_knowledge(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """An ``anyuse.surface-knowledge/1`` document -> its entries (``bundle_id``, ``kind``, ``key``, ``template``,
    ``effect``, ``item_kinds``, ``params``, ``cite``, ``verified``). A wrong format raises."""
    if (doc or {}).get("format") != KNOWLEDGE_FORMAT:
        raise ValueError(f"not a surface-knowledge document ({(doc or {}).get('format')!r})")
    return [e for e in doc.get("entries") or [] if isinstance(e, dict) and e.get("key")]


def apply_knowledge(surfaces: Sequence[Surface], entries: Sequence[Dict[str, Any]]) -> List[Surface]:
    """Copies of ``surfaces`` with knowledge entries applied: a matching URL scheme gets the entry's template,
    effect and parameters. An entry that is not ``verified`` makes the surface declared only as a HYPOTHESIS:
    ``interface`` stays ``partial`` (never served) and the entry is cited, so only a live check can promote it."""
    out = []
    for s in surfaces:
        s2 = copy.deepcopy(s)
        for e in entries:
            if e.get("key") == s.key and (not e.get("bundle_id") or e.get("bundle_id") == s.bundle_id):
                s2.invoke["template"] = e.get("template") or ""
                s2.effect = {"effect": e.get("effect") or "", "item_kinds": list(e.get("item_kinds") or []),
                             "source": "knowledge", "verified": bool(e.get("verified"))}
                s2.params = list(e.get("params") or [])
                s2.interface = "declared" if e.get("verified") else "partial"
                s2.notes = list(s2.notes) + [f"knowledge entry ({'verified' if e.get('verified') else 'UNVERIFIED'}): "
                                             f"{e.get('cite') or 'no citation'}"]
        out.append(s2)
    return out
