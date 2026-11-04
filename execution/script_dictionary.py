"""An app's scripting dictionary, read at runtime from its own bundle.

The script rung plans from this model, for ANY scriptable app: no per-app table of codes.

* **Reading** (:func:`read_bundle`): FILE READS ONLY. ``Contents/Info.plist`` -> ``OSAScriptingDefinition`` -> the
  bundle's ``.sdef`` with its ``xi:include``s (only from the bundle itself or ``/System/Library/ScriptingDefinitions/``),
  or the legacy ``.scriptSuite`` / ``.scriptTerminology`` (old-style property lists, parsed here, no ``plutil``),
  whose ``NSCoreSuite`` classes stand on the Cocoa standard suite (``CocoaStandard.sdef``). NEVER ``/usr/bin/sdef``:
  in the guest it is an Xcode shim that opens the Command Line Tools installer. No app is launched.
* **The model** (:class:`AppDictionary`): classes (plural, inheritance, ``responds-to``), properties (types, list flag,
  access, description), elements, commands with typed parameters, enumerations, value types; hidden terms flagged.
  Keys are the four-character codes; names are the dictionary's own (English) terms.
* **The cache** (:class:`DictionaryCache`): per app version (bundle id, short version, build) and the dictionary
  files' size and mtime; in memory and, when given a directory, as JSON.

Pure apart from reading files. No model call, no network.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

COCOA_STANDARD = "/System/Library/ScriptingDefinitions/CocoaStandard.sdef"
#: the legacy NSCoreSuite a ``.scriptSuite`` app's classes subclass (Foundation ships it as files)
NSCORE_SUITE = "/System/Library/Frameworks/Foundation.framework/Resources/NSCoreSuite.scriptSuite"
NSCORE_TERMINOLOGY = "/System/Library/Frameworks/Foundation.framework/Resources/NSCoreSuite.scriptTerminology"
ALLOWED_INCLUDE_ROOTS = ("/System/Library/ScriptingDefinitions/",)
FORMAT = "anyuse.script-dictionary/1"

# ---------------------------------------------------------------------------
# 1. The model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prop:
    name: str
    code: str
    types: Tuple[str, ...] = ("any",)
    is_list: bool = False
    access: str = "rw"                       # "r" | "w" | "rw"
    hidden: bool = False
    description: str = ""

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access


@dataclass
class Klass:
    name: str
    code: str = ""
    plural: str = ""
    inherits: str = ""
    hidden: bool = False
    description: str = ""
    props: Dict[str, Prop] = field(default_factory=dict)            # by code
    elements: List[str] = field(default_factory=list)               # class names
    responds_to: List[str] = field(default_factory=list)            # command names
    suite: str = ""


@dataclass(frozen=True)
class Param:
    name: str
    code: str
    types: Tuple[str, ...] = ("any",)
    optional: bool = False
    is_list: bool = False
    hidden: bool = False


@dataclass
class Command:
    name: str
    code: str                                # eight characters: event class + event id
    suite: str = ""
    hidden: bool = False
    description: str = ""
    direct: Optional[Param] = None           # name "direct"
    params: List[Param] = field(default_factory=list)
    result: Tuple[str, ...] = ()

    def param(self, code: str) -> Optional[Param]:
        return next((p for p in self.params if p.code == code), None)


@dataclass
class Enumeration:
    name: str
    code: str
    enumerators: List[Tuple[str, str]] = field(default_factory=list)      # (name, code)
    hidden: bool = False


def _nfc(s: Any) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


@dataclass
class AppDictionary:
    """One app's scripting dictionary. ``source``: ``sdef`` | ``script_suite`` | ``none``."""
    bundle_id: str
    version: str = ""
    build: str = ""
    source: str = "none"
    path: str = ""
    digest: str = ""
    classes: Dict[str, Klass] = field(default_factory=dict)                # by name
    commands: List[Command] = field(default_factory=list)
    enums: Dict[str, Enumeration] = field(default_factory=dict)             # by code
    value_types: Dict[str, str] = field(default_factory=dict)              # name -> code
    notes: List[str] = field(default_factory=list)

    # -- lookups ---------------------------------------------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not self.classes and not self.commands

    def klass(self, name: str) -> Optional[Klass]:
        if name in self.classes:
            return self.classes[name]
        low = _nfc(name).casefold()
        return next((k for k in self.classes.values() if k.name.casefold() == low or k.plural.casefold() == low),
                    None)

    def class_by_code(self, code: str) -> Optional[Klass]:
        return next((k for k in self.classes.values() if k.code == code), None)

    def application(self) -> Optional[Klass]:
        return self.class_by_code("capp")

    def chain(self, name: str) -> List[Klass]:
        """The class and its ancestors, most specific first."""
        out, seen = [], set()
        k = self.klass(name)
        while k is not None and k.name not in seen:
            seen.add(k.name)
            out.append(k)
            k = self.klass(k.inherits) if k.inherits else None
        return out

    def is_a(self, name: str, base: str) -> bool:
        b = self.klass(base)
        return b is not None and any(k.name == b.name for k in self.chain(name))

    def props(self, name: str) -> Dict[str, Prop]:
        """Own properties plus inherited ones (own wins), by code."""
        out: Dict[str, Prop] = {}
        for k in reversed(self.chain(name)):
            out.update(k.props)
        return out

    def prop_owner(self, name: str, code: str) -> str:
        """The MOST GENERAL class of ``name``'s chain that declares property ``code``."""
        owner = ""
        for k in self.chain(name):
            if code in k.props:
                owner = k.name
        return owner

    def elements(self, name: str) -> List[str]:
        out: List[str] = []
        for k in reversed(self.chain(name)):
            for e in k.elements:
                if e not in out:
                    out.append(e)
        return out

    def responds(self, name: str) -> Set[str]:
        return {c for k in self.chain(name) for c in k.responds_to}

    def subclasses(self, name: str) -> List[str]:
        return [k.name for k in self.classes.values() if k.name != name and self.is_a(k.name, name)]

    def command(self, code: str) -> Optional[Command]:
        return next((c for c in self.commands if c.code == code and not c.hidden), None)

    def enum_of(self, type_name: str) -> Optional[Enumeration]:
        if type_name in self.enums:
            return self.enums[type_name]
        low = type_name.casefold()
        return next((e for e in self.enums.values() if e.name.casefold() == low), None)

    def enumerator(self, code: str) -> Optional[Tuple[Enumeration, str]]:
        for e in self.enums.values():
            for nm, c in e.enumerators:
                if c == code:
                    return e, nm
        return None

    # -- the codes an independent checker allows -----------------------------------------------------------
    def class_codes(self) -> Set[str]:
        return {k.code for k in self.classes.values() if k.code}

    def prop_codes(self) -> Set[str]:
        return {p.code for k in self.classes.values() for p in k.props.values()}

    def param_codes(self) -> Set[str]:
        return {p.code for c in self.commands for p in c.params}

    def event_codes(self) -> Set[str]:
        return {c.code for c in self.commands if c.code}

    def enumerator_codes(self) -> Set[str]:
        return {c for e in self.enums.values() for _n, c in e.enumerators}

    # -- serialisation (the cache) -------------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        def prop(p: Prop) -> Dict[str, Any]:
            return {"name": p.name, "code": p.code, "types": list(p.types), "list": p.is_list, "access": p.access,
                    "hidden": p.hidden, "description": p.description}

        def par(p: Optional[Param]) -> Optional[Dict[str, Any]]:
            return None if p is None else {"name": p.name, "code": p.code, "types": list(p.types),
                                           "optional": p.optional, "list": p.is_list, "hidden": p.hidden}
        return {"format": FORMAT, "bundle_id": self.bundle_id, "version": self.version, "build": self.build,
                "source": self.source, "path": self.path, "digest": self.digest, "notes": list(self.notes),
                "classes": [{"name": k.name, "code": k.code, "plural": k.plural, "inherits": k.inherits,
                             "hidden": k.hidden, "description": k.description, "suite": k.suite,
                             "props": [prop(p) for p in k.props.values()], "elements": list(k.elements),
                             "responds_to": list(k.responds_to)} for k in self.classes.values()],
                "commands": [{"name": c.name, "code": c.code, "suite": c.suite, "hidden": c.hidden,
                              "description": c.description, "direct": par(c.direct),
                              "params": [par(p) for p in c.params], "result": list(c.result)}
                             for c in self.commands],
                "enums": [{"name": e.name, "code": e.code, "hidden": e.hidden,
                           "enumerators": [list(x) for x in e.enumerators]} for e in self.enums.values()],
                "value_types": dict(self.value_types)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppDictionary":
        if d.get("format") != FORMAT:
            raise ValueError("not a script dictionary record")

        def par(p: Optional[Dict[str, Any]]) -> Optional[Param]:
            return None if p is None else Param(p["name"], p["code"], tuple(p["types"]), p["optional"], p["list"],
                                                p.get("hidden", False))
        out = cls(d["bundle_id"], d.get("version", ""), d.get("build", ""), d.get("source", "none"),
                  d.get("path", ""), d.get("digest", ""), notes=list(d.get("notes") or []))
        for k in d.get("classes") or []:
            kk = Klass(k["name"], k["code"], k["plural"], k["inherits"], k["hidden"], k.get("description", ""),
                       suite=k.get("suite", ""), elements=list(k["elements"]), responds_to=list(k["responds_to"]))
            for p in k["props"]:
                kk.props[p["code"]] = Prop(p["name"], p["code"], tuple(p["types"]), p["list"], p["access"],
                                           p["hidden"], p.get("description", ""))
            out.classes[kk.name] = kk
        out.commands = [Command(c["name"], c["code"], c["suite"], c["hidden"], c.get("description", ""),
                                par(c["direct"]), [par(p) for p in c["params"]], tuple(c["result"]))
                        for c in d.get("commands") or []]
        for e in d.get("enums") or []:
            out.enums[e["code"]] = Enumeration(e["name"], e["code"], [tuple(x) for x in e["enumerators"]],
                                               e["hidden"])
        out.value_types = dict(d.get("value_types") or {})
        return out


# ---------------------------------------------------------------------------
# 2. Parsing an sdef (with its includes)
# ---------------------------------------------------------------------------

def _strip_doctype(text: str) -> str:
    return re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1)


def _tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1]


def _types(el: ET.Element) -> Tuple[Tuple[str, ...], bool]:
    t = el.get("type")
    if t:
        return (t,), False
    kids = [k for k in el if _tag(k) == "type" and k.get("type")]
    if not kids:
        return ("any",), False
    return tuple(k.get("type") for k in kids), any(k.get("list") == "yes" for k in kids)


def _access(el: ET.Element) -> str:
    a = el.get("access")
    return a if a in ("r", "w", "rw") else "rw"


_XP_NODES = re.compile(r"^xpointer\(/dictionary/suite/node\(\)(?:\[not\(self::([\w-]+) and (.+)\)\])?\)$")


def xpointer_filter(xp: Optional[str]) -> Optional[Tuple[str, str, frozenset]]:
    """The include forms sdefs use: ``xpointer(/dictionary/suite)`` (whole suites) and
    ``xpointer(/dictionary/suite/node()[not(self::command and @name = 'save')])`` (every node of every suite but the
    named ones of one kind; several names as ``((@name = 'a') or (@name = 'b'))``). (mode, excluded tag, excluded
    names); None for a form not understood (then everything is included and that is noted)."""
    xp = (xp or "").strip()
    if not xp or xp == "xpointer(/dictionary/suite)":
        return ("suites", "", frozenset())
    m = _XP_NODES.match(xp)
    if m:
        return ("nodes", m.group(1) or "", frozenset(re.findall(r"@name\s*=\s*'([^']*)'", m.group(2) or "")))
    return None


def _included(child: ET.Element, includes: Dict[str, str], missing: List[str], notes: List[str],
              depth: int) -> Iterable[Tuple[ET.Element, bool]]:
    """The suites an ``xi:include`` brings (as (suite, False)) or, for a node filter, the suites' kept nodes (as
    (node, True)). A missing include is reported, never fetched."""
    href = child.get("href") or ""
    text = includes.get(href)
    if text is None:
        missing.append(href)
        return
    flt = xpointer_filter(child.get("xpointer"))
    if flt is None:
        notes.append(f"xpointer not understood, everything included: {child.get('xpointer')}")
        flt = ("suites", "", frozenset())
    mode, tag, names = flt
    for suite in _suites(ET.fromstring(_strip_doctype(text)), includes, missing, notes, depth + 1):
        if mode == "suites":
            yield suite, False
            continue
        for node in suite:
            if tag and _tag(node) == tag and (node.get("name") or "") in names:
                continue
            yield node, True


def _suites(root: ET.Element, includes: Dict[str, str], missing: List[str], notes: Optional[List[str]] = None,
            depth: int = 0) -> Iterable[ET.Element]:
    """The suites of a document, following top-level ``xi:include``s (whole included suites)."""
    notes = notes if notes is not None else []
    for child in root:
        tag = _tag(child)
        if tag == "suite":
            yield child
        elif tag == "include" and depth < 4:
            for el, is_node in _included(child, includes, missing, notes, depth):
                if not is_node:
                    yield el


def _suite_nodes(suite: ET.Element, includes: Dict[str, str], missing: List[str], notes: List[str],
                 depth: int = 0) -> Iterable[ET.Element]:
    """A suite's own nodes, with an ``xi:include`` INSIDE the suite expanded in place (Calendar, Contacts and Mail
    include the Cocoa standard suite's nodes this way, minus the commands they redefine)."""
    for node in suite:
        if _tag(node) == "include" and depth < 4:
            for el, is_node in _included(node, includes, missing, notes, depth):
                if is_node:
                    yield el
                else:
                    yield from _suite_nodes(el, includes, missing, notes, depth + 1)
        else:
            yield node


def _merge_class(d: AppDictionary, el: ET.Element, suite: str, hidden_suite: bool, extends: bool) -> None:
    name = el.get("extends") if extends else el.get("name")
    if not name:
        return
    k = d.classes.get(name)
    if k is None:
        k = Klass(name, suite=suite)
        d.classes[name] = k
    if not extends:
        k.code = k.code or el.get("code") or ""
        k.plural = k.plural or el.get("plural") or ""
        k.inherits = k.inherits or el.get("inherits") or ""
        k.hidden = k.hidden or hidden_suite or el.get("hidden") == "yes"
        k.description = k.description or el.get("description") or ""
    for c in el:
        tag = _tag(c)
        if tag in ("property", "contents") and c.get("code"):
            types, is_list = _types(c)
            k.props[c.get("code")] = Prop(c.get("name") or ("contents" if tag == "contents" else ""), c.get("code"),
                                          types, is_list, _access(c),
                                          hidden_suite or c.get("hidden") == "yes", c.get("description") or "")
        elif tag == "element" and c.get("type") and c.get("hidden") != "yes":
            if c.get("type") not in k.elements:
                k.elements.append(c.get("type"))
        elif tag == "responds-to":
            cmd = c.get("command") or c.get("name")
            if cmd and cmd not in k.responds_to:
                k.responds_to.append(cmd)


def _param(el: ET.Element, name: str = "") -> Param:
    types, is_list = _types(el)
    return Param(name or el.get("name") or "", el.get("code") or "", types, el.get("optional") == "yes", is_list,
                 el.get("hidden") == "yes")


def parse_sdef(bundle_id: str, xml_text: str, includes: Optional[Dict[str, str]] = None,
               d: Optional[AppDictionary] = None) -> AppDictionary:
    """An sdef document (and its collected ``xi:include``s, href -> text) as an :class:`AppDictionary`. An include
    that was not collected is noted, never fetched."""
    d = d or AppDictionary(bundle_id, source="sdef")
    missing: List[str] = []
    root = ET.fromstring(_strip_doctype(xml_text))
    for suite in _suites(root, includes or {}, missing, d.notes):
        sname, shidden = suite.get("name") or "", suite.get("hidden") == "yes"
        for el in _suite_nodes(suite, includes or {}, missing, d.notes):
            tag = _tag(el)
            if tag == "class":
                _merge_class(d, el, sname, shidden, extends=False)
            elif tag == "class-extension":
                _merge_class(d, el, sname, shidden, extends=True)
            elif tag == "command" and el.get("code"):
                dp = next((c for c in el if _tag(c) == "direct-parameter"), None)
                res = next((c for c in el if _tag(c) == "result"), None)
                cmd = Command(el.get("name") or "", el.get("code"), sname, shidden or el.get("hidden") == "yes",
                              el.get("description") or "", None if dp is None else _param(dp, "direct"),
                              [_param(p) for p in el if _tag(p) == "parameter"],
                              () if res is None else _types(res)[0])
                prev = next((c for c in d.commands if c.code == cmd.code), None)
                if prev is not None and not prev.hidden:
                    cmd.hidden = False      # redefined hidden on top of a visible one (Calendar's make): still shown
                d.commands = [c for c in d.commands if c.code != cmd.code] + [cmd]
            elif tag == "enumeration" and el.get("code"):
                d.enums[el.get("code")] = Enumeration(
                    el.get("name") or el.get("code"), el.get("code"),
                    [(e.get("name") or "", e.get("code")) for e in el if _tag(e) == "enumerator" and e.get("code")
                     and e.get("hidden") != "yes"], shidden or el.get("hidden") == "yes")
            elif tag == "value-type" and el.get("name"):
                d.value_types[el.get("name")] = el.get("code") or ""
    if missing:
        d.notes.append(f"sdef includes not collected: {missing}")
    return d


# ---------------------------------------------------------------------------
# 3. Legacy .scriptSuite / .scriptTerminology
# ---------------------------------------------------------------------------

class _OpenStep:
    """An old-style (OpenStep) property list: ``{ k = v; }``, ``( a, b )``, quoted / bare strings, comments."""

    def __init__(self, text: str):
        self.t, self.i = text, 0

    def ws(self) -> None:
        while self.i < len(self.t):
            if self.t.startswith("//", self.i):
                j = self.t.find("\n", self.i)
                self.i = len(self.t) if j < 0 else j + 1
            elif self.t.startswith("/*", self.i):
                j = self.t.find("*/", self.i + 2)
                self.i = len(self.t) if j < 0 else j + 2
            elif self.t[self.i] in " \t\r\n":
                self.i += 1
            else:
                break

    def value(self) -> Any:
        self.ws()
        c = self.t[self.i]
        if c == "{":
            self.i += 1
            out: Dict[str, Any] = {}
            while True:
                self.ws()
                if self.t[self.i] == "}":
                    self.i += 1
                    return out
                k = self.value()
                self.ws()
                if self.t[self.i] != "=":
                    raise ValueError(f"expected = at {self.i}")
                self.i += 1
                out[str(k)] = self.value()
                self.ws()
                if self.t[self.i] == ";":
                    self.i += 1
        if c == "(":
            self.i += 1
            items: List[Any] = []
            while True:
                self.ws()
                if self.t[self.i] == ")":
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws()
                if self.t[self.i] == ",":
                    self.i += 1
        if c == '"':
            self.i += 1
            out_s: List[str] = []
            esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
            while self.t[self.i] != '"':
                if self.t[self.i] == "\\":
                    out_s.append(esc.get(self.t[self.i + 1], self.t[self.i + 1]))
                    self.i += 2
                else:
                    out_s.append(self.t[self.i])
                    self.i += 1
            self.i += 1
            return "".join(out_s)
        m = re.compile(r"[A-Za-z0-9_$+/:.\-]+").match(self.t, self.i)
        if not m:
            raise ValueError(f"unexpected {c!r} at {self.i}")
        self.i = m.end()
        return m.group(0)


def load_plist_bytes(data: bytes) -> Any:
    """A property list in XML, binary or old-style OpenStep format (pure Python, no ``plutil``)."""
    try:
        return plistlib.loads(data)
    except Exception:  # noqa: BLE001 - not XML/binary: old-style
        return _OpenStep(data.decode("utf-8", "replace")).value()


#: legacy superclass -> the Cocoa standard class it stands for
_NSCORE = {"NSCoreSuite.NSDocument": "document", "NSCoreSuite.NSApplication": "application",
           "NSCoreSuite.NSWindow": "window"}


def _legacy_type(t: str, tclasses: Dict[str, Any]) -> str:
    t = str(t or "any")
    if t.endswith("NSTextStorage") or t.endswith("NSString"):
        return "text"
    if t in ("NSNumber<Bool>",):
        return "boolean"
    if t.startswith("NSNumber"):
        return "number"
    if t == "NSDate":
        return "date"
    return (tclasses.get(t) or {}).get("Name") or t


def parse_script_suite(bundle_id: str, suite: Dict[str, Any], terminology: Dict[str, Any],
                       cocoa_standard_xml: Optional[str], nscore: Optional[Dict[str, Any]] = None) -> AppDictionary:
    """A legacy ``.scriptSuite``: its classes and commands, on the implicit Cocoa standard suite when its classes
    subclass ``NSCoreSuite.*`` (``cocoa_standard_xml`` None: noted, no standard term is listed). With the legacy
    NSCoreSuite itself (``nscore``: Foundation's ``NSCoreSuite.scriptSuite`` / ``.scriptTerminology``), its classes'
    attributes the standard suite lacks are added to the classes they stand for: a legacy app answers through them
    (TextEdit's document location is NSCoreSuite's ``path``, ``ppth``; CocoaStandard's ``file`` is not answered,
    -1728 live, vm-checks-2 e902feb)."""
    classes_raw = suite.get("Classes") or {}
    implicit = any(str((c or {}).get("Superclass", "")).startswith("NSCoreSuite.") for c in classes_raw.values())
    d = AppDictionary(bundle_id, source="script_suite")
    if implicit and cocoa_standard_xml:
        parse_sdef(bundle_id, cocoa_standard_xml, None, d)
        d.notes.append("legacy .scriptSuite on NSCoreSuite: the Cocoa standard suite (CocoaStandard.sdef) added")
    elif implicit:
        d.notes.append("legacy .scriptSuite on NSCoreSuite, but CocoaStandard.sdef was not collected")
    if implicit and nscore and nscore.get("suite"):
        ncls = (nscore.get("suite") or {}).get("Classes") or {}
        nterm = ((nscore.get("terminology") or {}).get("Classes")) or {}
        added = []
        for legacy, std in _NSCORE.items():
            c = ncls.get(legacy.split(".", 1)[1]) or {}
            k = d.classes.get(std)
            if k is None:
                continue
            tattr = (nterm.get(legacy.split(".", 1)[1]) or {}).get("Attributes") or {}
            for aname, a in sorted((c.get("Attributes") or {}).items()):
                code = (a or {}).get("AppleEventCode") or ""
                if not code or any(code in x.props for x in [k] + d.chain(k.name)[1:]):
                    continue
                ro = str((a or {}).get("ReadOnly", "NO")).upper() in ("YES", "TRUE", "1")
                k.props[code] = Prop((tattr.get(aname) or {}).get("Name") or aname, code,
                                     (_legacy_type(a.get("Type"), {}),), False, "r" if ro else "rw")
                added.append(f"{std}.{k.props[code].name} ({code})")
        if added:
            d.notes.append(f"legacy NSCoreSuite attributes added: {added}")
    tclasses = (terminology or {}).get("Classes") or {}
    sname = suite.get("Name") or ""
    for cname, c in sorted(classes_raw.items()):
        c = c or {}
        tc = tclasses.get(cname) or {}
        base = _NSCORE.get(str(c.get("Superclass", "")), "")
        name = tc.get("Name") or cname
        k = d.classes.get(name)
        if k is None:
            k = Klass(name, c.get("AppleEventCode") or "", tc.get("PluralName") or "",
                      base if base and base != name else "", description=tc.get("Description") or "", suite=sname)
            d.classes[name] = k
        tprops = {**(tc.get("Attributes") or {}), **(tc.get("ToOneRelationships") or {})}
        for group in ("Attributes", "ToOneRelationships"):
            for pname, p in sorted((c.get(group) or {}).items()):
                p = p or {}
                code = p.get("AppleEventCode") or ""
                if not code:
                    continue
                ro = str(p.get("ReadOnly", "NO")).upper() in ("YES", "TRUE", "1")
                tp = tprops.get(pname) or {}
                k.props[code] = Prop(tp.get("Name") or pname, code, (_legacy_type(p.get("Type"), tclasses),), False,
                                     "r" if ro else "rw", False, tp.get("Description") or "")
        for _pname, p in sorted((c.get("ToManyRelationships") or {}).items()):
            tname = _legacy_type((p or {}).get("Type"), tclasses)
            if tname and tname not in k.elements:
                k.elements.append(tname)
    klass = suite.get("AppleEventCode") or ""
    tcmds = (terminology or {}).get("Commands") or {}
    for cname, c in sorted((suite.get("Commands") or {}).items()):
        tc = tcmds.get(cname) or {}
        targs = tc.get("Arguments") or {}
        params = [Param((targs.get(an) or {}).get("Name") or an, a.get("AppleEventCode") or "",
                        (_legacy_type(a.get("Type"), tclasses),),
                        str(a.get("Optional", "NO")).upper() in ("YES", "TRUE", "1"))
                  for an, a in sorted((c.get("Arguments") or {}).items())]
        code = (c.get("AppleEventClassCode") or klass) + (c.get("AppleEventCode") or "")
        d.commands = [x for x in d.commands if x.code != code] + [
            Command(tc.get("Name") or cname, code, sname, False, tc.get("Description") or "",
                    None if not c.get("Type") else Param("direct", "", (_legacy_type(c.get("Type"), tclasses),), True),
                    params, ())]
    return d


def from_dump(dump: Dict[str, Any], cocoa_standard_xml: Optional[str] = None) -> AppDictionary:
    """From the ``discover_desktop.py dump`` shape (``sdef`` or ``script_suite``), as the guest collects it and as
    :func:`read_bundle` returns it. An app with neither has an empty dictionary."""
    bid = dump.get("bundle_id") or ""
    if dump.get("sdef"):
        s = dump["sdef"]
        d = parse_sdef(bid, s.get("xml") or "", s.get("includes") or {})
        d.path = s.get("path") or ""
    elif dump.get("script_suite"):
        ss = dump["script_suite"]
        d = parse_script_suite(bid, ss.get("suite") or {}, ss.get("terminology") or {}, cocoa_standard_xml,
                               dump.get("nscore_suite"))
        d.path = ss.get("path") or ""
    else:
        d = AppDictionary(bid)
        d.notes.append("no scripting dictionary shipped")
    d.version, d.build = str(dump.get("version") or ""), str(dump.get("build") or "")
    d.digest = str(dump.get("digest") or "")
    return d


# ---------------------------------------------------------------------------
# 4. Reading a bundle (file reads only; never the ``sdef`` tool)
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'<xi:include[^>]*href="([^"]+)"')


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def bundle_info(app_path: str) -> Dict[str, Any]:
    with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as fh:
        info = load_plist_bytes(fh.read())
    return info if isinstance(info, dict) else {}


def _dictionary_files(app_path: str, info: Dict[str, Any]) -> Tuple[str, List[str]]:
    """(kind, the dictionary's own files) of a bundle: ``sdef`` + its path, ``script_suite`` + suite and terminology,
    or ``none``."""
    res = os.path.join(app_path, "Contents", "Resources")
    name = info.get("OSAScriptingDefinition")
    if name:
        return "sdef", [os.path.join(res, name)]
    suites = sorted(f for f in os.listdir(res) if f.endswith(".scriptSuite")) if os.path.isdir(res) else []
    if suites:
        base = suites[0][:-len(".scriptSuite")]
        files = [os.path.join(res, suites[0])]
        for sub in ("", "en.lproj", "English.lproj", "Base.lproj"):
            p = os.path.join(res, sub, base + ".scriptTerminology")
            if os.path.exists(p):
                files.append(p)
                break
        return "script_suite", files
    return "none", []


def read_bundle(app_path: str, cocoa_standard_path: str = COCOA_STANDARD) -> Dict[str, Any]:
    """An app bundle's scripting dictionary as the ``dump`` shape (:func:`from_dump`), plus its version, build and a
    digest of the dictionary's bytes. FILE READS ONLY: never ``/usr/bin/sdef``, never launches the app. Includes
    are read only from the bundle itself or ``/System/Library/ScriptingDefinitions/``."""
    app_path = os.path.abspath(app_path)
    info = bundle_info(app_path)
    out: Dict[str, Any] = {"bundle_id": info.get("CFBundleIdentifier") or "", "app_path": app_path,
                           "version": str(info.get("CFBundleShortVersionString") or ""),
                           "build": str(info.get("CFBundleVersion") or ""), "sdef": None, "script_suite": None}
    kind, files = _dictionary_files(app_path, info)
    h = hashlib.sha256()
    if kind == "sdef":
        xml = _read_text(files[0])
        h.update(xml.encode("utf-8"))
        includes: Dict[str, str] = {}
        for href in _INCLUDE_RE.findall(xml):
            local = re.sub(r"^file://(localhost)?", "", href)
            if local.startswith(ALLOWED_INCLUDE_ROOTS) or local.startswith(app_path + "/"):
                try:
                    includes[href] = _read_text(local)
                    h.update(includes[href].encode("utf-8"))
                except OSError:
                    pass
        out["sdef"] = {"xml": xml, "includes": includes, "path": files[0]}
    elif kind == "script_suite":
        with open(files[0], "rb") as fh:
            raw = fh.read()
        h.update(raw)
        term: Any = {}
        if len(files) > 1:
            with open(files[1], "rb") as fh:
                traw = fh.read()
            h.update(traw)
            term = load_plist_bytes(traw)
        out["script_suite"] = {"suite": load_plist_bytes(raw), "terminology": term, "path": files[0]}
        if os.path.exists(cocoa_standard_path):
            out["cocoa_standard"] = _read_text(cocoa_standard_path)
        if os.path.exists(NSCORE_SUITE):
            with open(NSCORE_SUITE, "rb") as fh:
                nsuite = load_plist_bytes(fh.read())
            nterm: Any = {}
            if os.path.exists(NSCORE_TERMINOLOGY):
                with open(NSCORE_TERMINOLOGY, "rb") as fh:
                    nterm = load_plist_bytes(fh.read())
            out["nscore_suite"] = {"suite": nsuite, "terminology": nterm}
    out["digest"] = h.hexdigest()[:16] if kind != "none" else ""
    return out


def read_app_dictionary(app_path: str, cocoa_standard_path: str = COCOA_STANDARD) -> AppDictionary:
    dump = read_bundle(app_path, cocoa_standard_path)
    return from_dump(dump, dump.get("cocoa_standard"))


class DictionaryCache:
    """Dictionaries per app VERSION: the key is (bundle id, short version, build) plus the dictionary files' size and
    mtime, so an updated app is read again. In memory; also JSON files under ``root`` when given (one per key).
    ``reads`` counts the bundles actually parsed (a hit parses nothing)."""

    def __init__(self, root: Optional[str] = None, cocoa_standard_path: str = COCOA_STANDARD):
        self.root, self.cocoa = root, cocoa_standard_path
        self.mem: Dict[str, AppDictionary] = {}
        self.reads = 0

    def key(self, app_path: str) -> str:
        info = bundle_info(app_path)
        _kind, files = _dictionary_files(app_path, info)
        stamp = [(os.path.basename(f), os.stat(f).st_size, os.stat(f).st_mtime_ns) for f in files]
        raw = json.dumps([info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString"),
                          info.get("CFBundleVersion"), stamp])
        return f"{info.get('CFBundleIdentifier') or 'unknown'}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    def get(self, app_path: str) -> AppDictionary:
        k = self.key(app_path)
        if k in self.mem:
            return self.mem[k]
        path = os.path.join(self.root, k + ".json") if self.root else None
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    d = AppDictionary.from_dict(json.load(fh))
                self.mem[k] = d
                return d
            except (ValueError, KeyError, OSError):
                pass
        d = read_app_dictionary(app_path, self.cocoa)
        self.reads += 1
        self.mem[k] = d
        if path:
            os.makedirs(self.root, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d.to_dict(), fh, ensure_ascii=False)
            os.replace(tmp, path)
        return d


def summary(d: AppDictionary) -> Dict[str, Any]:
    """Counts for a report: classes (with elements), properties, commands, enumerations (visible ones)."""
    vis = [k for k in d.classes.values() if not k.hidden]
    return {"bundle_id": d.bundle_id, "version": d.version, "build": d.build, "source": d.source,
            "classes": len(vis), "classes_with_elements": sum(1 for k in vis if k.elements),
            "properties": sum(len(k.props) for k in vis),
            "commands": sum(1 for c in d.commands if not c.hidden),
            "enumerations": sum(1 for e in d.enums.values() if not e.hidden), "notes": list(d.notes)}
