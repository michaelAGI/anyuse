"""``osa_command``: compile a desktop operation to Apple events.

The classes of ``desktop_compile`` (AX) and ``blender_compile`` (``bpy``)
leave every scripting-dictionary command ``unresolved``
(37 typed commands, no class sends Apple
events). This module is that class. It is pure: it never sends an Apple event
or runs ``osascript`` itself; every effect goes through an injected runner and
an injected permission check, so the host tests use fakes only.

1. **Dictionary model.** Commands, classes, typed properties (with access) and
   elements, from the bundle's own ``.sdef`` (with ``xi:include``s) or legacy
   ``.scriptSuite``/``.scriptTerminology``. A legacy suite whose classes
   subclass ``NSCoreSuite.*`` gets the implicit Cocoa standard suite from the
   guest's ``CocoaStandard.sdef`` (passed in, never fetched). Never via the
   ``sdef`` tool. Keys are four-character codes, not the localized names.
2. **Typed operations.** Every visible command, a getter per property, a
   setter per writable property, each with a fixed effect by event code
   (:data:`EFFECTS`) and a qualification: only ``read``/``create``/``write``
   effects that are observable and reversible in the guest qualify.
3. **Shapes.** A qualifying operation compiles only through a fixed shape
   (:data:`SHAPES`) to a constant AppleScript with raw ``«event»``/``«class»``
   codes. Inputs arrive as ``on run argv`` items and are never interpolated.
4. **Mapping.** ``file_write`` -> ``document_write``;
   ``directory_op/create`` -> ``make_element`` (class ``cfol``). A dictionary
   class with a writable name, a writable text property of its own and a
   ``delete`` command is mapped as a discovered ``make_element``.
5. **Execution guard** (:func:`run_osa`): the Automation permission is checked
   without asking first (no consent dialog can appear); non-read operations
   pass ``hil_gate.HILGate`` (mode ``all``, fail-closed) and keep every path
   argument under a disposable root.
6. **Proof** (:func:`prove`): the ``blender_compile.prove`` discipline. The GUI
   path and the OSA path each run on >= 2 unseen inputs from a declared start
   state; both observations must be valid, meet the expectation and match.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import desktop_discover as dd

MIN_FRESH_CASES = 2

#: ``AEDeterminePermissionToAutomateTarget`` results.
AUTOMATION_GRANTED = 0
AUTOMATION_STATUS = {
    0: "granted",
    -1743: "errAEEventNotPermitted (denied)",
    -1744: "errAEEventWouldRequireUserConsent (never answered)",
    -600: "procNotFound (target not running)",
}

OSASCRIPT = "/usr/bin/osascript"

# ---------------------------------------------------------------------------
# 1. Dictionary model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OsaProperty:
    name: str
    code: str
    type: str
    access: str = "rw"          # "r" | "w" | "rw"

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access


@dataclass
class OsaClass:
    name: str
    code: str
    inherits: str = ""
    properties: Dict[str, OsaProperty] = field(default_factory=dict)   # by code
    elements: List[str] = field(default_factory=list)                  # class names
    suite: str = ""


@dataclass
class OsaDictionary:
    bundle_id: str
    commands: List[Dict[str, Any]] = field(default_factory=list)        # dd.parse_sdef shape
    classes: Dict[str, OsaClass] = field(default_factory=dict)          # by name
    notes: List[str] = field(default_factory=list)

    def command(self, code: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.commands if c.get("code") == code and not c.get("hidden")), None)

    def class_by_code(self, code: str) -> Optional[OsaClass]:
        return next((k for k in self.classes.values() if k.code == code), None)

    def resolved_properties(self, name: str) -> Dict[str, OsaProperty]:
        """Own properties plus inherited ones (own wins), by code."""
        out: Dict[str, OsaProperty] = {}
        seen = set()
        k = self.classes.get(name)
        chain = []
        while k is not None and k.name not in seen:
            seen.add(k.name)
            chain.append(k)
            k = self.classes.get(k.inherits) if k.inherits else None
        for k in reversed(chain):
            out.update(k.properties)
        return out

    def application(self) -> Optional[OsaClass]:
        return self.class_by_code("capp")

    def is_leaf(self, name: str) -> bool:
        return not any(k.inherits == name for k in self.classes.values())


def _strip_doctype(text: str) -> str:
    return re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1)


def _sdef_suites(root: ET.Element, includes: Dict[str, str], missing: List[str]):
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag == "suite":
            yield child
        elif tag == "include":
            href = child.get("href") or ""
            text = includes.get(href)
            if text is None:
                missing.append(href)
                continue
            yield from _sdef_suites(ET.fromstring(_strip_doctype(text)), includes, missing)


def _access(el: ET.Element) -> str:
    a = el.get("access")
    return a if a in ("r", "w", "rw") else "rw"


def _add_class(classes: Dict[str, OsaClass], el: ET.Element, suite: str, extends: bool) -> None:
    name = el.get("extends") if extends else el.get("name")
    if not name:
        return
    k = classes.get(name)
    if k is None:
        k = OsaClass(name=name, code=el.get("code") or "", inherits=el.get("inherits") or "",
                     suite=suite)
        classes[name] = k
    elif not extends:
        k.code = k.code or el.get("code") or ""
        k.inherits = k.inherits or el.get("inherits") or ""
    for p in el.findall("property"):
        if p.get("hidden") == "yes" or not p.get("code"):
            continue
        k.properties[p.get("code")] = OsaProperty(p.get("name") or "", p.get("code"),
                                                   dd._sdef_type(p), _access(p))
    for e in el.findall("element"):
        if e.get("type") and e.get("type") not in k.elements:
            k.elements.append(e.get("type"))


def parse_sdef_dictionary(bundle_id: str, xml_text: str,
                          includes: Optional[Dict[str, str]] = None) -> OsaDictionary:
    """Commands (via ``desktop_discover.parse_sdef``, unchanged) plus classes,
    class extensions, typed properties and elements."""
    includes = includes or {}
    commands = [c for c in dd.parse_sdef(xml_text, includes) if "_missing_includes" not in c]
    d = OsaDictionary(bundle_id, commands)
    missing: List[str] = []
    for suite in _sdef_suites(ET.fromstring(_strip_doctype(xml_text)), includes, missing):
        for el in suite.findall("class"):
            _add_class(d.classes, el, suite.get("name") or "", extends=False)
        for el in suite.findall("class-extension"):
            _add_class(d.classes, el, suite.get("name") or "", extends=True)
    if missing:
        d.notes.append(f"sdef includes not collected: {missing}")
    return d


#: A legacy ``.scriptSuite`` on NSCoreSuite answers a document's location through NSCoreSuite's own NSDocument
#: attribute ``fileName`` (term "path", code 'ppth', text: the POSIX path), NOT through CocoaStandard.sdef's ``file``
#: (fileURL), which such an app does not answer. Live in the guest (TextEdit):
#: ``file of document 1`` -1728 for every document, ``path of document 1`` the POSIX path (NFD; an unsaved document
#: has none). The implicit suite below is CocoaStandard.sdef with that one correction.
_NSCORE_DOCUMENT_PATH = ("path", "ppth", "text", "r")

#: legacy superclass -> the Cocoa standard sdef class it stands for
_NSCORE = {"NSCoreSuite.NSDocument": "document", "NSCoreSuite.NSApplication": "application",
           "NSCoreSuite.NSWindow": "window"}


def dictionary_from_script_suite(bundle_id: str, suite: Dict[str, Any], terminology: Dict[str, Any],
                                 cocoa_standard_xml: Optional[str]) -> OsaDictionary:
    """A legacy ``.scriptSuite``: its classes plus, when they subclass
    ``NSCoreSuite.*``, the implicit Cocoa standard suite read from the
    guest's ``CocoaStandard.sdef`` (``cocoa_standard_xml``; ``None`` = not
    collected, noted, and then no standard command exists)."""
    classes_raw = suite.get("Classes") or {}
    implicit = any(str((c or {}).get("Superclass", "")).startswith("NSCoreSuite.")
                   for c in classes_raw.values())
    if implicit and cocoa_standard_xml:
        d = parse_sdef_dictionary(bundle_id, cocoa_standard_xml)
        d.notes.append("legacy .scriptSuite on NSCoreSuite: implicit Cocoa standard suite "
                       "from /System/Library/ScriptingDefinitions/CocoaStandard.sdef")
        doc = d.classes.get("document")
        if doc is not None:
            doc.properties.pop("file", None)
            doc.properties[_NSCORE_DOCUMENT_PATH[1]] = OsaProperty(*_NSCORE_DOCUMENT_PATH)
            d.notes.append("legacy NSCoreSuite document: its location is 'path' (ppth, NSDocument fileName); "
                           "CocoaStandard's 'file' is not answered")
    else:
        d = OsaDictionary(bundle_id)
        if implicit:
            d.notes.append("legacy .scriptSuite on NSCoreSuite, but CocoaStandard.sdef was not "
                           "collected: no standard command is listed")
    d.commands += dd.parse_script_suite(suite, terminology)
    tclasses = (terminology or {}).get("Classes") or {}
    for cname, c in sorted(classes_raw.items()):
        c = c or {}
        tc = tclasses.get(cname) or {}
        base = _NSCORE.get(str(c.get("Superclass", "")), "")
        name = tc.get("Name") or cname
        k = d.classes.get(name)
        if k is None:
            k = OsaClass(name=name, code=c.get("AppleEventCode") or "",
                         inherits=base if base and base != name else "", suite=suite.get("Name") or "")
            d.classes[name] = k
        tprops = {**(tc.get("Attributes") or {}), **(tc.get("ToOneRelationships") or {})}
        for group in ("Attributes", "ToOneRelationships"):
            for pname, p in sorted((c.get(group) or {}).items()):
                p = p or {}
                code = p.get("AppleEventCode") or ""
                if not code:
                    continue
                ptype = str(p.get("Type") or "any")
                ptype = "text" if ptype.endswith("NSTextStorage") or ptype.endswith("NSString") else ptype
                access = "r" if str(p.get("ReadOnly", "NO")).upper() in ("YES", "TRUE", "1") else "rw"
                k.properties[code] = OsaProperty((tprops.get(pname) or {}).get("Name") or pname,
                                                 code, ptype, access)
        for pname, p in sorted((c.get("ToManyRelationships") or {}).items()):
            t = str((p or {}).get("Type") or "")
            tname = (tclasses.get(t) or {}).get("Name") or t
            if tname and tname not in k.elements:
                k.elements.append(tname)
    return d


def dictionary_from_dump(dump: Dict[str, Any], cocoa_standard_xml: Optional[str] = None) -> OsaDictionary:
    """From the ``discover_desktop.py dump`` shape (``sdef`` or
    ``script_suite``). An app with neither has an empty dictionary."""
    bid = dump.get("bundle_id") or ""
    if dump.get("sdef"):
        return parse_sdef_dictionary(bid, dump["sdef"].get("xml") or "", dump["sdef"].get("includes") or {})
    if dump.get("script_suite"):
        ss = dump["script_suite"]
        return dictionary_from_script_suite(bid, ss.get("suite") or {}, ss.get("terminology") or {},
                                            cocoa_standard_xml)
    d = OsaDictionary(bid)
    d.notes.append("no scripting dictionary shipped")
    return d


# ---------------------------------------------------------------------------
# 2. Typed operations, effects, qualification
# ---------------------------------------------------------------------------

#: Effect of a command, by event code. Fixed, not per app.
EFFECTS: Dict[str, str] = {
    # read
    "coregetd": "read", "corecnte": "read", "coredoex": "read", "coredsiz": "read", "DATASORT": "read",
    # create
    "corecrel": "create", "coreclon": "create",
    # write
    "coresetd": "write", "coresave": "write", "coremove": "write",
    # destructive
    "coredelo": "destructive",
    # user interface / window state / clipboard: not observable as a document effect
    "aevtodoc": "ui", "aevtpdoc": "ui", "coreclos": "ui", "miscactv": "ui", "miscslct": "ui",
    "miscmvis": "ui", "noteshow": "ui", "noteopen": "ui", "fndrovir": "ui", "misccopy": "ui",
    "fndrfclu": "ui", "fndrfupd": "ui",
    # never: irreversible or takes the guest down
    "aevtquit": "never", "fndrempt": "never", "fndrfera": "never", "fndrejct": "never",
    "fndrrest": "never", "fndrshut": "never", "fndrslep": "never",
}

QUALIFYING_EFFECTS = ("read", "create", "write")


@dataclass
class TypedOp:
    bundle_id: str
    key: str                 # "aevt:<code>" | "get:<class>.<prop>" | "set:<class>.<prop>"
    kind: str                # "command" | "get" | "set"
    name: str                # display only
    effect: str
    qualifies: bool
    reason: str
    params: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self):
        return {"bundle_id": self.bundle_id, "key": self.key, "kind": self.kind, "name": self.name,
                "effect": self.effect, "qualifies": self.qualifies, "reason": self.reason,
                "params": self.params}


def _command_qualification(d: OsaDictionary, code: str, effect: str) -> Tuple[bool, str]:
    if effect == "undeclared":
        return False, "event code not in the effect table: effect not declared"
    if effect == "never":
        return False, "irreversible or stops the guest: never compiled"
    if effect == "ui":
        return False, "window/UI/clipboard effect: not observable as a document or file effect"
    if effect == "destructive":
        return False, "destructive: only as the cleanup of an object this class created, behind the gate"
    if effect == "read":
        return True, "read: the result is the observation; nothing to reverse"
    if effect == "create":
        if d.command("coredelo"):
            return True, "create: observable through the dictionary's getters, reversible by its delete"
        return False, "create without a delete command: not reversible through the dictionary"
    if effect == "write":
        if code == "coresave":
            return True, "write to a file: observable on disk, reversible only on disposable files"
        return True, "write: observable through getters, reversible by writing the old value back"
    return False, f"effect {effect!r}"


def typed_operations(d: OsaDictionary) -> List[TypedOp]:
    """Every visible command, a getter per declared property, a setter per
    writable property."""
    ops: List[TypedOp] = []
    for c in d.commands:
        if c.get("hidden") or "_missing_includes" in c:
            continue
        code = c.get("code") or ""
        effect = EFFECTS.get(code, "undeclared")
        ok, why = _command_qualification(d, code, effect)
        params = ([{"name": "direct", **(c.get("direct_parameter") or {})}] if c.get("direct_parameter") else [])
        params += [dict(p) for p in c.get("params") or []]
        ops.append(TypedOp(d.bundle_id, f"aevt:{code}" if code else f"name:{c.get('name')}", "command",
                           c.get("name") or "", effect, ok, why, params))
    for k in sorted(d.classes.values(), key=lambda k: k.name):
        for p in sorted(k.properties.values(), key=lambda p: p.code):
            cls = k.code or k.name
            if p.readable:
                ops.append(TypedOp(d.bundle_id, f"get:{cls}.{p.code}", "get", f"{k.name}.{p.name}",
                                   "read", True, "property getter: read", [{"name": p.name, "type": p.type}]))
            if p.writable:
                ok = p.readable
                ops.append(TypedOp(d.bundle_id, f"set:{cls}.{p.code}", "set", f"{k.name}.{p.name}",
                                   "write", ok,
                                   "property setter: reversible by restoring the value read before" if ok
                                   else "write-only property: the old value cannot be read back",
                                   [{"name": p.name, "type": p.type}]))
    return ops


# ---------------------------------------------------------------------------
# 3. Shapes -> constant AppleScript with argv
# ---------------------------------------------------------------------------

#: argv-passable types and the AppleScript expression that coerces item ``i``.
_ARG_COERCE = {
    "text": "(item {i} of argv)",
    "integer": "((item {i} of argv) as integer)",
    "real": "((item {i} of argv) as real)",
    "number": "((item {i} of argv) as real)",
    "boolean": "((item {i} of argv) is \"true\")",
    "file": "(POSIX file (item {i} of argv))",
}

_CODE_RE = re.compile(r"^[\x20-\x7e]{4}$")


def _code(c: str) -> str:
    if not _CODE_RE.match(c or "") or "»" in c or "«" in c:
        raise ValueError(f"not a four-character code: {c!r}")
    return c


def _bundle(bid: str) -> str:
    if not re.match(r"^[A-Za-z0-9.\-]+$", bid or ""):
        raise ValueError(f"not a bundle id: {bid!r}")
    return bid


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: str                  # "text" | "path" | ...
    under_scratch: bool = False


@dataclass
class OsaCommand:
    """A compiled ``osa_command``: one constant script, typed argv."""
    id: str
    bundle_id: str
    shape: str
    source: str                 # "class:file_write" | "discovered" | "property"
    effect: str                 # "read" | "create" | "write"
    args: Tuple[ArgSpec, ...]
    script: str
    uses: Tuple[str, ...]        # event/class/property codes the script relies on
    observer: str
    reverse: str
    step_class: str             # hil_gate step class

    def argv(self, values: Sequence[Any]) -> List[str]:
        if len(values) != len(self.args):
            raise ValueError(f"{self.id}: expected {len(self.args)} argument(s), got {len(values)}")
        return [OSASCRIPT, "-"] + [str(v) for v in values]

    def to_dict(self):
        return {"id": self.id, "bundle_id": self.bundle_id, "shape": self.shape, "source": self.source,
                "effect": self.effect, "args": [a.__dict__ for a in self.args], "script": self.script,
                "uses": list(self.uses), "observer": self.observer, "reverse": self.reverse,
                "step_class": self.step_class}


@dataclass
class Unmapped:
    shape: str
    reason: str


def _need(d: OsaDictionary, code: str, params: Sequence[str] = ()) -> Optional[str]:
    c = d.command(code)
    if c is None:
        return f"dictionary has no command {code}"
    have = {p.get("code") for p in c.get("params") or []}
    miss = [p for p in params if p not in have]
    return f"command {code} lacks parameter(s) {miss}" if miss else None


def compile_make_element(d: OsaDictionary, class_code: str, prop_codes: Sequence[str],
                         container: str, source: str) -> Any:
    """``make new <K> at <container> with properties {<props>: argv}``.
    ``container``: ``"posix_folder"`` (argv 1 is a folder path under the
    disposable root; the result is returned as a POSIX path) or
    ``"application"`` (the app's default container; the result's ``ID  ``
    is returned)."""
    why = _need(d, "corecrel", ("kocl", "prdt") + (("insh",) if container == "posix_folder" else ()))
    if why:
        return Unmapped("make_element", why)
    k = d.class_by_code(class_code)
    if k is None:
        return Unmapped("make_element", f"dictionary has no class {class_code}")
    props = d.resolved_properties(k.name)
    args: List[ArgSpec] = []
    if container == "posix_folder":
        args.append(ArgSpec("folder", "path", under_scratch=True))
    kv = []
    for pc in prop_codes:
        p = props.get(pc)
        if p is None or not p.writable:
            return Unmapped("make_element", f"class {class_code} has no writable property {pc}")
        if p.type not in ("text",):
            return Unmapped("make_element", f"property {pc} of {class_code} is {p.type}, not text")
        args.append(ArgSpec(p.name, "text"))
        kv.append(f"«class {_code(pc)}»:(item {len(args)} of argv)")
    if container == "posix_folder":
        if "cdis" not in props and "ctnr" not in props:
            return Unmapped("make_element", f"class {class_code} is not a file system item "
                            "(no container/disk property)")
        pre = "\tset theContainer to (POSIX file (item 1 of argv)) as alias\n"
        at = ", «class insh»:theContainer"
        ret = ("\t\tset theAlias to theResult as alias\n\tend tell\n"
               "\treturn POSIX path of theAlias\n")
        observer = "the file system: the created item exists at <folder>/<name>"
        reverse = "remove the created item from the disposable folder"
    elif container == "application":
        if "ID  " not in props or not props["ID  "].readable:
            return Unmapped("make_element", f"class {class_code} has no readable id to read it back by")
        if d.command("coredelo") is None:
            return Unmapped("make_element", "no delete command: not reversible")
        pre, at = "", ""
        ret = "\t\treturn «class ID  » of theResult\n\tend tell\n"
        observer = "the app's own getters (by the returned id) or its store"
        reverse = "delete the created element by its id (coredelo, behind the gate)"
    else:
        return Unmapped("make_element", f"unknown container {container!r}")
    script = (
        "on run argv\n" + pre +
        f"\ttell application id \"{_bundle(d.bundle_id)}\"\n"
        f"\t\tset theResult to «event corecrel» given «class kocl»:«class {_code(class_code)}»{at}, "
        f"«class prdt»:{{{', '.join(kv)}}}\n" + ret +
        "end run\n")
    uses = ("corecrel", "kocl", "prdt", class_code, *prop_codes) + (("insh",) if at else ("ID  ",))
    return OsaCommand(f"{d.bundle_id}:osa_command/make_element/{class_code}", d.bundle_id, "make_element",
                      source, "create", tuple(args), script, uses, observer, reverse, "side_effecting")


def compile_document_write(d: OsaDictionary, source: str) -> Any:
    """Open the file (a disposable document), set its text, save, close:
    ``open``, ``set text``, ``save``, ``close saving no``."""
    for code, params in (("aevtodoc", ()), ("coresave", ()), ("coreclos", ("savo",))):
        why = _need(d, code, params)
        if why:
            return Unmapped("document_write", why)
    doc = d.class_by_code("docu")
    if doc is None:
        return Unmapped("document_write", "dictionary has no document class (docu)")
    text = next((p for p in d.resolved_properties(doc.name).values()
                 if p.type == "text" and p.writable and p.code not in ("pnam",)), None)
    if text is None:
        return Unmapped("document_write", "document class has no writable text property")
    script = (
        "on run argv\n"
        "\tset theFile to POSIX file (item 1 of argv)\n"
        f"\ttell application id \"{_bundle(d.bundle_id)}\"\n"
        "\t\t«event aevtodoc» theFile\n"
        "\t\tset theDoc to «class docu» 1\n"
        f"\t\tset «class {_code(text.code)}» of theDoc to (item 2 of argv)\n"
        "\t\t«event coresave» theDoc\n"
        "\t\t«event coreclos» theDoc given «class savo»:«constant ****no  »\n"
        "\tend tell\n"
        "\treturn POSIX path of theFile\n"
        "end run\n")
    return OsaCommand(f"{d.bundle_id}:osa_command/document_write", d.bundle_id, "document_write", source,
                      "write", (ArgSpec("file", "path", under_scratch=True), ArgSpec(text.name, "text")),
                      script, ("aevtodoc", "docu", text.code, "coresave", "coreclos", "savo"),
                      "the bytes of the file on disk", "restore the declared start content of the "
                      "disposable file", "side_effecting")


def compile_app_property(d: OsaDictionary, prop_code: str, write: bool) -> Any:
    shape = "set_property" if write else "get_property"
    app = d.application()
    if app is None:
        return Unmapped(shape, "dictionary has no application class")
    p = d.resolved_properties(app.name).get(prop_code)
    if p is None:
        return Unmapped(shape, f"application has no property {prop_code}")
    if write and not (p.writable and p.readable):
        return Unmapped(shape, f"property {prop_code} is not read-write")
    if not write and not p.readable:
        return Unmapped(shape, f"property {prop_code} is not readable")
    tell = f"\ttell application id \"{_bundle(d.bundle_id)}\"\n"
    if not write:
        script = ("on run argv\n" + tell + f"\t\treturn «class {_code(prop_code)}»\n\tend tell\nend run\n")
        return OsaCommand(f"{d.bundle_id}:osa_command/get_property/{prop_code}", d.bundle_id, shape,
                          "property", "read", (), script, (prop_code,), "the returned value", "none",
                          "read_only")
    coerce = _ARG_COERCE.get(p.type)
    if coerce is None:
        return Unmapped(shape, f"property {prop_code} is {p.type}: not passable as an argv item")
    script = ("on run argv\n" + tell +
              f"\t\tset theOld to «class {_code(prop_code)}»\n"
              # "of it": without it osacompile rejects the set with -10003
              # (the bare and the variable forms fail too)
              f"\t\tset «class {prop_code}» of it to {coerce.format(i=1)}\n"
              f"\t\treturn «class {prop_code}»\n\tend tell\nend run\n")
    return OsaCommand(f"{d.bundle_id}:osa_command/set_property/{prop_code}", d.bundle_id, shape,
                      "property", "write", (ArgSpec(p.name, p.type),), script, (prop_code,),
                      "the returned value, read back by the getter", "set the value read before back",
                      "side_effecting")


SHAPES = ("make_element", "document_write", "get_property", "set_property")

#: existing desktop class -> the shape that stands for it. Per class, never per app.
CLASS_TO_SHAPE: Dict[Any, Tuple[str, Dict[str, Any]]] = {
    "file_write": ("document_write", {}),
    ("directory_op", "create"): ("make_element", {"class_code": "cfol", "prop_codes": ("pnam",),
                                                  "container": "posix_folder"}),
}


def map_class(d: OsaDictionary, op_class: str, subtype: Optional[str] = None) -> Any:
    """The OSA form of an existing desktop class on this app, if its
    dictionary has what the class's shape needs."""
    key = (op_class, subtype) if subtype else op_class
    entry = CLASS_TO_SHAPE.get(key) or CLASS_TO_SHAPE.get(op_class)
    label = op_class + (f"/{subtype}" if subtype else "")
    if entry is None:
        return Unmapped("", f"no OSA shape for class {label}")
    shape, kw = entry
    src = f"class:{label}"
    if shape == "document_write":
        return compile_document_write(d, src)
    return compile_make_element(d, source=src, **kw)


def map_recording(d: OsaDictionary, rec: Any) -> Any:
    """A recording (``desktop_compile.DesktopRecording``) -> its OSA form,
    through the existing ``infer_class`` only."""
    from anyuse.execution import desktop_compile as dc
    inf = dc.infer_class(rec)
    if inf.op_class == "unresolved":
        return Unmapped("", f"recording unresolved: {inf.reason}")
    if rec.bundle_id != d.bundle_id:
        return Unmapped("", f"recording is for {rec.bundle_id}, dictionary for {d.bundle_id}")
    return map_class(d, inf.op_class, inf.subtype)


def discovered_make_elements(d: OsaDictionary) -> List[OsaCommand]:
    """A leaf class that is an element of the application, with a writable
    ``pnam``, a writable text property declared on the class itself, a
    readable id, and a ``delete`` command: ``make_element`` in the
    application's default container."""
    app = d.application()
    if app is None or d.command("corecrel") is None or d.command("coredelo") is None:
        return []
    out = []
    for ename in app.elements:
        k = d.classes.get(ename)
        if k is None or not k.code or k.code == "capp" or not d.is_leaf(k.name):
            continue
        props = d.resolved_properties(k.name)
        if not (props.get("pnam") and props["pnam"].writable):
            continue
        own_text = [p for p in k.properties.values()
                    if p.code != "pnam" and p.type == "text" and p.writable and p.readable]
        if not own_text:
            continue
        c = compile_make_element(d, k.code, ("pnam", own_text[0].code), "application", "discovered")
        if isinstance(c, OsaCommand):
            out.append(c)
    return out


def app_property_commands(d: OsaDictionary) -> Tuple[List[OsaCommand], List[Unmapped]]:
    app = d.application()
    if app is None:
        return [], []
    ok: List[OsaCommand] = []
    bad: List[Unmapped] = []
    for code, p in sorted(d.resolved_properties(app.name).items()):
        for write in ((False, True) if p.writable else (False,)):
            c = compile_app_property(d, code, write)
            (ok if isinstance(c, OsaCommand) else bad).append(c)
    return ok, bad


def argv_only(script: str) -> bool:
    """True iff the script takes its inputs only from ``on run argv``: no
    ``run script``, ``do shell script`` or string concatenation into code."""
    return (script.startswith("on run argv\n") and "run script" not in script
            and "do shell script" not in script and "&" not in script)


# ---------------------------------------------------------------------------
# 4. Execution guard
# ---------------------------------------------------------------------------

@dataclass
class OsaResult:
    ok: bool
    refused: Optional[str] = None
    rc: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    ms: float = 0.0

    def to_dict(self):
        return dict(self.__dict__)


def _under(path: str, root: str) -> bool:
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip("/") + "/")


def run_osa(cmd: OsaCommand, values: Sequence[Any], *,
            permission: Callable[[str], int],
            runner: Callable[[List[str], str], Tuple[int, str, str]],
            gate: Any = None, drive_enabled: bool = False,
            scratch_root: Optional[str] = None) -> OsaResult:
    """Run one compiled command, or refuse without sending anything.

    Order: argument count; Automation permission (``permission(bundle_id)``
    must be 0; it must never ask the user); for a non-read command, the drive
    gate, every path argument under ``scratch_root`` and an actuate decision
    from ``gate`` (a ``hil_gate.HILGate``); then ``runner(argv, script)``."""
    if len(values) != len(cmd.args):
        return OsaResult(False, refused=f"expected {len(cmd.args)} argument(s), got {len(values)}")
    status = permission(cmd.bundle_id)
    if status != AUTOMATION_GRANTED:
        return OsaResult(False, refused=f"automation_not_granted: {status} "
                                        f"{AUTOMATION_STATUS.get(status, '')}".strip())
    if cmd.effect != "read":
        if not drive_enabled:
            return OsaResult(False, refused="drive gate off: nothing actuates")
        for spec, v in zip(cmd.args, values):
            if spec.under_scratch and (not scratch_root or not _under(str(v), scratch_root)):
                return OsaResult(False, refused=f"path argument {spec.name!r} is not under the disposable root")
        if gate is None:
            return OsaResult(False, refused="no human gate configured for a non-read command")
        action = {"action": "osascript", "args": {"command": cmd.id, "effect": cmd.effect,
                                                  "values": [str(v) for v in values]}}
        try:
            decision = asyncio.run(gate.require_approval(action, drive_enabled=drive_enabled))
        except Exception as exc:  # noqa: BLE001  (ApprovalRequired: fail closed)
            return OsaResult(False, refused=f"human gate: {type(exc).__name__}: {exc}")
        if not decision.actuate:
            return OsaResult(False, refused=f"human gate: {decision.reason}")
        from anyuse.execution.foreground_interlock import refusal
        interlock = refusal(f"osascript {cmd.id}")
        if interlock:
            return OsaResult(False, refused=interlock)
    t0 = time.perf_counter()
    rc, out, err = runner(cmd.argv(values), cmd.script)
    ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return OsaResult(rc == 0, rc=rc, stdout=out, stderr=err, ms=ms)


def disposable_approver(scratch_root: str, allowed_ids: Sequence[str]):
    """An approver for ``HILGate``: approves only a listed command id
    (``allowed_ids``) whose path values all lie under ``scratch_root``. It is a
    standing approval for the listed disposable operations, not a
    general yes."""
    allowed = set(allowed_ids)

    async def approve(context: Dict[str, Any]) -> bool:
        args = context.get("args") or {}
        if args.get("command") not in allowed:
            return False
        for v in args.get("values") or []:
            if str(v).startswith("/") and not _under(str(v), scratch_root):
                return False
        return True
    return approve


# ---------------------------------------------------------------------------
# 5. Proof
# ---------------------------------------------------------------------------

def default_validate(obs: Any) -> Optional[str]:
    """Why ``obs`` is not a valid observation, or ``None``."""
    if obs is None:
        return "empty observation"
    if isinstance(obs, dict) and obs.get("error"):
        return f"error: {obs['error']}"
    if isinstance(obs, (str, list, dict, tuple, set)) and len(obs) == 0:
        return "empty observation"
    return None


@dataclass
class OsaProofRow:
    input: Any
    gui: Any
    osa: Any
    gui_ms: float
    osa_ms: float
    agree: bool
    mismatch: Optional[str] = None

    def to_dict(self):
        return dict(self.__dict__)


@dataclass
class OsaProofReport:
    command_id: str
    shape: str
    rows: List[OsaProofRow] = field(default_factory=list)
    proved: bool = False
    refusal_reason: Optional[str] = None

    def to_dict(self):
        return {"command_id": self.command_id, "shape": self.shape, "proved": self.proved,
                "refusal_reason": self.refusal_reason, "rows": [r.to_dict() for r in self.rows],
                "mismatches": [r.to_dict() for r in self.rows if not r.agree]}


def prove(cmd: OsaCommand, inputs: Sequence[Any],
          gui_fn: Callable[[Any], Any], osa_fn: Callable[[Any], Any], *,
          recorded_inputs: Sequence[Any], start_state: Any,
          expect: Callable[[Any, Any, Any], Optional[str]],
          automation_status: int,
          validate: Callable[[Any], Optional[str]] = default_validate,
          equal: Optional[Callable[[Any, Any], bool]] = None) -> OsaProofReport:
    """GUI path vs OSA path on unseen inputs from a declared start state.

    Refuses (no row runs) when: Automation is not granted; fewer than
    ``MIN_FRESH_CASES`` distinct inputs; any input was recorded or replayed
    before; the start state is not a valid observation. A row agrees only if
    both observations are valid, both meet ``expect(start_state, input,
    obs)`` (``None`` = met) and ``equal(gui, osa)``. A shared failure is
    invalid, never agreement. One disagreeing row fails the report."""
    def refuse(reason):
        return OsaProofReport(cmd.id, cmd.shape, refusal_reason=reason)

    if automation_status != AUTOMATION_GRANTED:
        return refuse(f"automation_not_granted: {automation_status} "
                      f"{AUTOMATION_STATUS.get(automation_status, '')}".strip())
    distinct = len({repr(i) for i in inputs})
    if distinct < MIN_FRESH_CASES:
        return refuse(f"only {distinct} distinct unseen input(s), need >= {MIN_FRESH_CASES}")
    reused = sorted({repr(i) for i in inputs} & {repr(i) for i in recorded_inputs})
    if reused:
        return refuse(f"input(s) already used by the recording or an earlier replay: {reused}")
    bad = validate(start_state) if start_state is not None else "no declared start state"
    if bad:
        return refuse(f"start_state is not a valid observation: {bad}")
    equal = equal or (lambda a, b: a == b)
    rows = []
    for inp in inputs:
        t0 = time.perf_counter()
        g = gui_fn(inp)
        g_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        t0 = time.perf_counter()
        o = osa_fn(inp)
        o_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        mismatch = (
            (lambda r: r and f"gui observation invalid: {r}")(validate(g))
            or (lambda r: r and f"osa observation invalid: {r}")(validate(o))
            or (lambda r: r and f"gui: {r}")(expect(start_state, inp, g))
            or (lambda r: r and f"osa: {r}")(expect(start_state, inp, o))
            or (None if equal(g, o) else "gui and osa observations differ"))
        rows.append(OsaProofRow(inp, g, o, g_ms, o_ms, not mismatch, mismatch or None))
    return OsaProofReport(cmd.id, cmd.shape, rows=rows, proved=all(r.agree for r in rows))


# ---------------------------------------------------------------------------
# 6. One app
# ---------------------------------------------------------------------------

def analyse_app(d: OsaDictionary, classes: Sequence[Tuple[str, Optional[str]]] = ()) -> Dict[str, Any]:
    """Typed operations, qualification and every compiled ``osa_command`` for
    one dictionary. ``classes`` are the existing desktop classes (recorded or
    discovered) on this app, as ``(op_class, subtype)``."""
    ops = typed_operations(d)
    mapped: List[OsaCommand] = []
    unmapped: List[Dict[str, Any]] = []
    for op_class, subtype in classes:
        c = map_class(d, op_class, subtype)
        if isinstance(c, OsaCommand):
            mapped.append(c)
        else:
            unmapped.append({"class": op_class + (f"/{subtype}" if subtype else ""), "shape": c.shape,
                             "reason": c.reason})
    ids = {c.id for c in mapped}
    mapped += [c for c in discovered_make_elements(d) if c.id not in ids]
    props, prop_bad = app_property_commands(d)
    cmds = [op for op in ops if op.kind == "command"]
    return {
        "bundle_id": d.bundle_id, "notes": d.notes,
        "counts": {
            "commands": len(cmds),
            "properties": sum(len(k.properties) for k in d.classes.values()),
            "typed_operations": len(ops),
            "qualifying": sum(1 for op in ops if op.qualifies),
            "qualifying_commands": sum(1 for op in cmds if op.qualifies),
            "effects": {e: sum(1 for op in cmds if op.effect == e)
                        for e in sorted({op.effect for op in cmds})},
            "osa_commands_mapped": len(mapped),
            "app_property_commands": len(props),
        },
        "mapped": [c.to_dict() for c in mapped],
        "unmapped_classes": unmapped,
        "app_property_commands": [c.to_dict() for c in props],
        "app_property_unmapped": [u.__dict__ for u in prop_bad],
        "typed_operations": [op.to_dict() for op in ops],
    }
