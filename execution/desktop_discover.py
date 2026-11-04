"""Desktop discovery: point AnyUse at a macOS app and list candidate tools
without a recording.

``execution/desktop_compile.py`` and ``execution/blender_compile.py`` start
from a recording: somebody used the app once, and the recorded vocabulary
decides the operation class. This module starts one step earlier. It reads
what the app already declares about itself and turns that into candidates:

* the menu bar, through the accessibility tree: every leaf ``AXMenuItem``
  with its role, title, ``AXIdentifier`` (on Cocoa menus this is the action
  selector, e.g. ``saveDocument:``), keyboard shortcut
  (``AXMenuItemCmdChar`` / ``AXMenuItemCmdModifiers`` /
  ``AXMenuItemCmdVirtualKey``) and ``AXEnabled``;
* the controls of the front window that carry an ``AXIdentifier`` or are
  text-entry roles;
* the scripting dictionary a scriptable app ships (an ``.sdef``, or the
  legacy ``.scriptSuite`` / ``.scriptTerminology`` pair), parsed into typed
  commands keyed by their four-plus-four Apple event code;
* for Blender, the operator catalog read through ``bpy`` introspection
  (``execution/guest/blender/capture.py``: ``dump_operator_catalog``), each
  operator with its RNA properties as typed parameters.

The guest side (``execution/guest/discover_desktop.py``) only collects these
facts into a JSON dump. Everything below is pure: no pyobjc, no subprocess,
no ``bpy``. It runs on the host under the guarded test runner.

Keys are language-independent wherever the app allows it. The guest runs in
German, so a title such as ``Sichern`` is kept for display only. The key is
the identifier if there is one, else the shortcut, else the menu position.
For scripting dictionaries it is the Apple event code, and for Blender the
operator idname.

**Discovered is not proved.** Each candidate ends in one of three states:

``unresolved``  found, but no existing compile class matches it; the reason
                says why.
``mapped``      an existing class rule matches (see below); nothing has been
                checked against the app yet.
``proved``      the class's existing proof routine (``desktop_compile.prove``
                or ``blender_compile.prove``) agreed on every row, over at
                least two inputs that were never used before
                (:func:`attach_proof` enforces both).

**Mapping reuses the existing class rules instead of new ones.** Each
mapping rule below finds the discovered elements a class keys on, builds the
recording those elements would produce, and hands it to the existing
``desktop_compile.infer_class`` or ``blender_compile.infer_class``. The
candidate is mapped only if that function returns the rule's class. If a
class rule changes, discovery follows it or stops mapping; it cannot drift
into a looser rule of its own. Steps a flow only produces after the trigger
fires (Finder's rename-on-create field) are marked ``implied``.

The classes are bounded on purpose: five
AX classes, most tied to one bundle id, and one ``app_script`` shape. Most
of an app's menu is expected to stay unresolved. That is the honest result,
not a gap to be filled with guesses.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import blender_compile as bc
from . import desktop_compile as dc

#: Localized titles of the Services submenu. Its items belong to OTHER apps,
#: so the whole subtree is left out. The one place a title decides anything,
#: and only for an exclusion.
SERVICES_TITLES = frozenset({"Services", "Dienste"})

#: Front-window roles kept as control candidates even without an identifier
#: (text entry is what the file_write rule keys on).
TEXT_ENTRY_ROLES = frozenset({"AXTextArea", "AXTextField", "AXComboBox"})

#: Front-window roles kept as control candidates when they carry an identifier.
CONTROL_ROLES = frozenset({"AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
                           "AXMenuButton", "AXSlider", "AXIncrementor", "AXSegmentedControl",
                           "AXTextArea", "AXTextField", "AXComboBox", "AXLink"})

#: The operand vocabulary arithmetic's direct operation needs (a op b):
#: the Calculator button identifiers ``execution/guest/flows.py`` (CALC_IDS)
#: already records by. A Calculator without all of them is not the class.
ARITHMETIC_IDS = frozenset({"Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                            "Eight", "Nine", "Add", "Subtract", "Multiply", "Divide",
                            "Equals", "AllClear"})


# ---------------------------------------------------------------------------
# 1. Candidates
# ---------------------------------------------------------------------------

@dataclass
class Param:
    """One typed parameter of a candidate."""
    name: str
    type: str
    optional: bool = False
    code: str = ""
    array_length: int = 0
    enum: Tuple[str, ...] = ()
    default: Any = None
    description: str = ""

    def to_dict(self):
        d = {"name": self.name, "type": self.type, "optional": self.optional}
        if self.code:
            d["code"] = self.code
        if self.array_length:
            d["array_length"] = self.array_length
        if self.enum:
            d["enum"] = list(self.enum)
        if self.default is not None:
            d["default"] = self.default
        if self.description:
            d["description"] = self.description
        return d


@dataclass
class Candidate:
    """One discovered operation from one source."""
    source: str                    # "menu" | "control" | "sdef" | "bpy"
    bundle_id: str
    key: str                       # language-independent where possible
    key_kind: str                  # "identifier" | "shortcut" | "position" | "role" | "event_code" | "idname"
    label: str                     # display only (may be localized)
    role: str = ""
    enabled: Optional[bool] = None
    params: List[Param] = field(default_factory=list)
    status: str = "unresolved"     # "unresolved" | "mapped" | "proved"
    reason: str = ""
    maps_to: Optional[str] = None  # id of the ClassCandidate it supports
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        d = {"source": self.source, "bundle_id": self.bundle_id, "key": self.key,
             "key_kind": self.key_kind, "label": self.label, "status": self.status,
             "reason": self.reason}
        if self.role:
            d["role"] = self.role
        if self.enabled is not None:
            d["enabled"] = self.enabled
        if self.params:
            d["params"] = [p.to_dict() for p in self.params]
        if self.maps_to:
            d["maps_to"] = self.maps_to
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class ClassCandidate:
    """A candidate tool: one existing compile class, matched by rule over
    discovered source candidates. Starts ``mapped``, becomes ``proved`` only
    through :func:`attach_proof`."""
    id: str
    bundle_id: str
    op_class: str
    subtype: Optional[str]
    triggers: List[str]                       # keys of the source candidates it rests on
    implied: List[str] = field(default_factory=list)
    params: List[Param] = field(default_factory=list)
    inferred_reason: str = ""
    status: str = "mapped"
    proof: Optional[Dict[str, Any]] = None
    proof_reason: str = ""

    def to_dict(self):
        return {"id": self.id, "bundle_id": self.bundle_id, "op_class": self.op_class,
                "subtype": self.subtype, "triggers": list(self.triggers),
                "implied": list(self.implied), "params": [p.to_dict() for p in self.params],
                "inferred_reason": self.inferred_reason, "status": self.status,
                "proof_reason": self.proof_reason, "proof": self.proof}


# ---------------------------------------------------------------------------
# 2. Sources -> candidates
# ---------------------------------------------------------------------------

def _mods_str(mods: Any) -> str:
    if mods is None or mods == "":
        return "0"
    return str(mods).strip()


def menu_key(item: Dict[str, Any]) -> Tuple[str, str]:
    """(key, key_kind) of one menu item, language-independent where possible.

    Identifier first: on Cocoa menus ``AXIdentifier`` is the action selector
    (``saveDocument:``) or a stable id the app set. Then the shortcut: command
    character (lower-cased), modifier mask, and the virtual key when the
    character is a function-key glyph. Last the position path, which is
    language-independent but moves when the app adds a menu item."""
    ident = (item.get("identifier") or "").strip()
    if ident:
        return f"id:{ident}", "identifier"
    cmd = item.get("cmd_char") or ""
    vk = item.get("cmd_virtual_key")
    if cmd or vk not in (None, "", 0):
        ch = cmd.lower() if cmd and cmd.isprintable() and ord(cmd[0]) < 0xF700 else ""
        tail = ch if ch else f"vk{vk}"
        return f"key:{_mods_str(item.get('cmd_mods'))}:{tail}", "shortcut"
    return "pos:" + "/".join(str(i) for i in item.get("index_path") or []), "position"


def menu_candidates(bundle_id: str, menu_items: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """Leaf menu items -> candidates. ``menu_items`` is the flat list the
    guest dump writes (one dict per AXMenuItem, with ``index_path``,
    ``title_path``, ``has_submenu``). Left out: the Apple menu (menu-bar
    index 0, system-wide, not the app's), the Services subtree (other apps'
    items), submenu parents (they open a menu, they do nothing), and
    separators (no title, no identifier)."""
    out: List[Candidate] = []
    kept = []
    for it in menu_items:
        idx = list(it.get("index_path") or [])
        titles = list(it.get("title_path") or [])
        if not idx or idx[0] == 0:
            continue
        if any(t in SERVICES_TITLES for t in titles[:-1]):
            continue
        if it.get("has_submenu"):
            continue
        title = it.get("title") or ""
        if not title and not (it.get("identifier") or ""):
            continue
        kept.append((it, idx, titles, title) + menu_key(it))
    freq: Dict[str, int] = {}
    for *_rest, key, _kind in kept:
        freq[key] = freq.get(key, 0) + 1
    for it, idx, titles, title, key, kind in kept:
        # a key shared by several items (SwiftUI's generic "menuAction:", a
        # selector reused in two menus) does not identify one item: every
        # holder gets its position appended, still language-independent
        if freq[key] > 1:
            key = f"{key}@{'/'.join(str(i) for i in idx)}"
            kind = f"{kind}+position"
        out.append(Candidate(
            source="menu", bundle_id=bundle_id, key=key, key_kind=kind,
            label=" > ".join(t for t in titles if t) or title, role="AXMenuItem",
            enabled=it.get("enabled"),
            extra={"cmd_char": it.get("cmd_char") or "", "cmd_mods": _mods_str(it.get("cmd_mods")),
                   "cmd_virtual_key": it.get("cmd_virtual_key"),
                   "identifier": it.get("identifier") or "", "index_path": idx}))
    return out


def control_candidates(bundle_id: str, controls: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """Front-window controls with an identifier, plus text-entry controls
    with or without one."""
    out: List[Candidate] = []
    seen: Dict[str, int] = {}
    for c in controls:
        role = c.get("role") or ""
        ident = (c.get("identifier") or "").strip()
        if role not in CONTROL_ROLES:
            continue
        if not ident and role not in TEXT_ENTRY_ROLES:
            continue
        key, kind = (f"id:{ident}", "identifier") if ident else (f"role:{role}", "role")
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n:
            key = f"{key}#{n}"
        out.append(Candidate(
            source="control", bundle_id=bundle_id, key=key, key_kind=kind,
            label=c.get("title") or c.get("description") or ident or role, role=role,
            enabled=c.get("enabled"),
            extra={"identifier": ident, "subrole": c.get("subrole") or ""}))
    return out


# --- scripting dictionaries ------------------------------------------------

def _sdef_type(el: ET.Element) -> str:
    t = el.get("type")
    if t:
        return t
    kids = [k.get("type") for k in el.findall("type") if k.get("type")]
    if not kids:
        return "any"
    return " | ".join(kids) + (" (list)" if any(k.get("list") == "yes" for k in el.findall("type")) else "")


def parse_sdef(xml_text: str, includes: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Commands of an ``.sdef`` document, resolving ``xi:include`` from
    ``includes`` (href -> document text, collected by the guest dump; an
    include that is missing is reported, never fetched). One dict per
    command: suite, name, code, direct parameter, typed parameters, result."""
    includes = includes or {}
    xml_text = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text, count=1)
    root = ET.fromstring(xml_text)
    out: List[Dict[str, Any]] = []
    missing: List[str] = []

    def suites(node: ET.Element) -> Iterable[ET.Element]:
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "suite":
                yield child
            elif tag == "include":
                href = child.get("href") or ""
                text = includes.get(href)
                if text is None:
                    missing.append(href)
                    continue
                sub = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1))
                yield from suites(sub)

    for suite in suites(root):
        for cmd in suite.findall("command"):
            dp = cmd.find("direct-parameter")
            res = cmd.find("result")
            params = [{"name": p.get("name") or "", "code": p.get("code") or "",
                       "type": _sdef_type(p), "optional": p.get("optional") == "yes"}
                      for p in cmd.findall("parameter")]
            out.append({
                "suite": suite.get("name") or "", "name": cmd.get("name") or "",
                "code": cmd.get("code") or "",
                "direct_parameter": None if dp is None else {
                    "type": _sdef_type(dp), "optional": dp.get("optional") == "yes"},
                "params": params,
                "result": None if res is None else _sdef_type(res),
                "hidden": cmd.get("hidden") == "yes",
            })
    if missing:
        out.append({"_missing_includes": missing})
    return out


def parse_script_suite(suite: Dict[str, Any], terminology: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Commands of a legacy ``.scriptSuite`` (property list, already decoded)
    with names from its ``.scriptTerminology``. Same output shape as
    :func:`parse_sdef`. The event code is ``AppleEventClassCode`` plus the
    command's ``AppleEventCode``."""
    out: List[Dict[str, Any]] = []
    klass = suite.get("AppleEventCode") or ""
    term_cmds = (terminology or {}).get("Commands") or {}
    for cname, c in sorted((suite.get("Commands") or {}).items()):
        tc = term_cmds.get(cname) or {}
        targs = tc.get("Arguments") or {}
        params = []
        for aname, a in sorted((c.get("Arguments") or {}).items()):
            params.append({"name": (targs.get(aname) or {}).get("Name") or aname,
                           "code": a.get("AppleEventCode") or "",
                           "type": a.get("Type") or "any",
                           "optional": str(a.get("Optional", "NO")).upper() in ("YES", "TRUE", "1")})
        out.append({"suite": suite.get("Name") or "", "name": tc.get("Name") or cname,
                    "code": (c.get("AppleEventClassCode") or klass) + (c.get("AppleEventCode") or ""),
                    "direct_parameter": None if not c.get("Type") else {"type": c.get("Type"),
                                                                         "optional": True},
                    "params": params, "result": c.get("ResultAppleEventCode"), "hidden": False})
    return out


def sdef_candidates(bundle_id: str, commands: Sequence[Dict[str, Any]]) -> List[Candidate]:
    out: List[Candidate] = []
    seen: Dict[str, int] = {}
    for c in commands:
        if "_missing_includes" in c or c.get("hidden"):
            continue
        code = c.get("code") or ""
        key = f"aevt:{code}" if code else f"name:{c.get('name')}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n:
            key = f"{key}#{n}"
        params: List[Param] = []
        dp = c.get("direct_parameter")
        if dp:
            params.append(Param("direct", dp.get("type") or "any", bool(dp.get("optional"))))
        for p in c.get("params") or []:
            params.append(Param(p.get("name") or "", p.get("type") or "any",
                                bool(p.get("optional")), code=p.get("code") or ""))
        out.append(Candidate(source="sdef", bundle_id=bundle_id, key=key,
                             key_kind="event_code" if code else "name", label=c.get("name") or "",
                             params=params, extra={"suite": c.get("suite") or "",
                                                   "result": c.get("result")}))
    return out


# --- Blender operators -----------------------------------------------------

def idname_py(idname: str) -> str:
    """``MESH_OT_primitive_cube_add`` -> ``mesh.primitive_cube_add``."""
    if "_OT_" not in idname:
        return idname
    mod, func = idname.split("_OT_", 1)
    return f"{mod.lower()}.{func}"


def bpy_candidates(bundle_id: str, catalog: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """Operator catalog (``capture.dump_operator_catalog``) -> candidates,
    one per operator, each RNA property a typed parameter."""
    out: List[Candidate] = []
    for op in catalog:
        params = []
        for p in op.get("props") or []:
            params.append(Param(p.get("identifier") or "", (p.get("type") or "").lower(),
                                optional=True, array_length=int(p.get("array_length") or 0),
                                enum=tuple(p.get("enum_items") or ()), default=p.get("default"),
                                description=(p.get("subtype") or "")))
        out.append(Candidate(source="bpy", bundle_id=bundle_id, key=op.get("idname_py") or
                             idname_py(op.get("idname") or ""), key_kind="idname",
                             label=op.get("label") or "", params=params,
                             extra={"idname": op.get("idname") or ""}))
    return out


# ---------------------------------------------------------------------------
# 3. Mapping rules: find what a class keys on, then ask the EXISTING
#    infer_class whether it agrees.
# ---------------------------------------------------------------------------

@dataclass
class RuleHit:
    op_class: str
    subtype: Optional[str]
    triggers: List[Candidate]
    implied: List[str]
    params: List[Param]
    inferred_reason: str


def _ax_recording(bundle_id: str, flow: str, steps: Sequence[dc.RecordedStep]) -> dc.ClassInference:
    return dc.infer_class(dc.DesktopRecording(flow=flow, app="", bundle_id=bundle_id,
                                              steps=tuple(steps)))


def rule_file_write(bundle_id: str, cands: Sequence[Candidate]) -> Optional[RuleHit]:
    """A Save item (⌘S, no extra modifier) plus a text area in the front
    window: the two steps ``desktop_compile``'s file_write rule keys on."""
    save = next((c for c in cands if c.source == "menu" and c.extra.get("cmd_char", "").lower() == "s"
                 and c.extra.get("cmd_mods") == "0"), None)
    area = next((c for c in cands if c.source == "control" and c.role == "AXTextArea"), None)
    if save is None or area is None:
        return None
    inf = _ax_recording(bundle_id, "discovered:file_write", [
        dc.RecordedStep("area", role="AXTextArea", identifier=area.extra.get("identifier", "")),
        dc.RecordedStep("save", role="AXMenuItem", identifier=save.extra.get("identifier", ""),
                        cmd=save.extra.get("cmd_char", ""), mods=save.extra.get("cmd_mods", "")),
    ])
    if inf.op_class != "file_write":
        return None
    return RuleHit("file_write", None, [area, save], [],
                   [Param("path", "file path"), Param("text", "text")], inf.reason)


def rule_directory_create(bundle_id: str, cands: Sequence[Candidate]) -> Optional[RuleHit]:
    """New Folder (⇧⌘N): ``desktop_compile``'s directory_op/create rule keys
    on it plus the rename-on-create text field, which only exists after the
    item fires, so that step is implied, not discovered."""
    nf = next((c for c in cands if c.source == "menu" and c.extra.get("cmd_char", "").lower() == "n"
               and c.extra.get("cmd_mods") == "1"), None)
    if nf is None:
        return None
    inf = _ax_recording(bundle_id, "discovered:directory_create", [
        dc.RecordedStep("new_folder", role="AXMenuItem", identifier=nf.extra.get("identifier", ""),
                        cmd=nf.extra.get("cmd_char", ""), mods=nf.extra.get("cmd_mods", "")),
        dc.RecordedStep("name_field", role="AXTextField"),
    ])
    if (inf.op_class, inf.subtype) != ("directory_op", "create"):
        return None
    return RuleHit("directory_op", "create", [nf],
                   ["AXTextField (rename-on-create field, appears after the item fires)"],
                   [Param("path", "directory path")], inf.reason)


def rule_arithmetic(bundle_id: str, cands: Sequence[Candidate]) -> Optional[RuleHit]:
    """Every digit, operator, Equals and AllClear button, found by
    identifier: the operand vocabulary arithmetic's direct operation needs."""
    buttons = {c.extra.get("identifier"): c for c in cands
               if c.source == "control" and c.role == "AXButton" and c.extra.get("identifier")}
    if not ARITHMETIC_IDS <= set(buttons):
        return None
    trig = [buttons[i] for i in sorted(ARITHMETIC_IDS)]
    inf = _ax_recording(bundle_id, "discovered:arithmetic",
                        [dc.RecordedStep(c.extra["identifier"], role="AXButton",
                                         identifier=c.extra["identifier"]) for c in trig])
    if inf.op_class != "arithmetic":
        return None
    return RuleHit("arithmetic", None, trig, [],
                   [Param("a", "number"), Param("op", "enum", enum=("+", "-", "*", "/")),
                    Param("b", "number")], inf.reason)


def rule_read_value(bundle_id: str, cands: Sequence[Candidate]) -> Optional[RuleHit]:
    """System Settings: navigation buttons and no text entry. Not among the
    measured apps; kept so every AX class has its discovery rule."""
    nav = [c for c in cands if c.source == "control" and c.role == "AXButton"]
    if not nav or any(c.source == "control" and c.role in TEXT_ENTRY_ROLES for c in cands):
        return None
    inf = _ax_recording(bundle_id, "discovered:read_value",
                        [dc.RecordedStep("nav", role="AXButton",
                                         identifier=nav[0].extra.get("identifier", ""))])
    if inf.op_class != "read_value":
        return None
    return RuleHit("read_value", None, nav[:1], [], [Param("label", "text")], inf.reason)


def rule_app_script(bundle_id: str, cands: Sequence[Candidate]) -> List[RuleHit]:
    """One hit per ``blender_compile.KNOWN_SHAPES`` sequence whose operators
    are all in the catalog AND whose parameter kwarg exists on the named
    operator as a numeric property of the right array length. The synthetic
    recording carries ONLY the parameter kwargs (no constants: nothing was
    recorded), and ``blender_compile.infer_class`` must agree."""
    by_id = {c.extra.get("idname"): c for c in cands if c.source == "bpy"}
    hits: List[RuleHit] = []
    for seq, shape in bc.KNOWN_SHAPES.items():
        if not all(i in by_id for i in seq):
            continue
        ok = True
        params: List[Param] = []
        kwargs_per_op: List[Dict[str, Any]] = [{} for _ in seq]
        for spec in shape.params:
            op = by_id[seq[spec.op_index]]
            prop = next((p for p in op.params if p.name == spec.kwarg), None)
            want_len = spec.count if spec.count > 1 else 0
            if prop is None or prop.type not in ("float", "int") or prop.array_length != want_len:
                ok = False
                break
            kwargs_per_op[spec.op_index][spec.kwarg] = [0.0] * spec.count if spec.count > 1 else 0.0
            for i in range(spec.count):
                params.append(Param(f"{spec.kwarg}[{i}]" if spec.count > 1 else spec.kwarg, prop.type))
        if not ok:
            continue
        rec = bc.BlenderRecording(flow="discovered:" + shape.subtype, ops=tuple(
            bc.RecordedOperator(idname=i, kwargs=kw) for i, kw in zip(seq, kwargs_per_op)))
        inf = bc.infer_class(rec)
        if (inf.op_class, inf.subtype) != ("app_script", shape.subtype):
            continue
        hits.append(RuleHit("app_script", shape.subtype, [by_id[i] for i in seq], [], params,
                            inf.reason))
    return hits


AX_RULES: Tuple[Callable[[str, Sequence[Candidate]], Optional[RuleHit]], ...] = (
    rule_file_write, rule_directory_create, rule_arithmetic, rule_read_value)


def synthetic_blender_recording(hit: "ClassCandidate") -> bc.BlenderRecording:
    """The recording a discovered app_script candidate compiles from: its
    shape's operators with only the parameter kwargs set (placeholders the
    generated script replaces from argv)."""
    seq = next(s for s, sh in bc.KNOWN_SHAPES.items() if sh.subtype == hit.subtype)
    shape = bc.KNOWN_SHAPES[seq]
    kw: List[Dict[str, Any]] = [{} for _ in seq]
    for spec in shape.params:
        kw[spec.op_index][spec.kwarg] = [0.0] * spec.count if spec.count > 1 else 0.0
    return bc.BlenderRecording(flow="discovered:" + shape.subtype, ops=tuple(
        bc.RecordedOperator(idname=i, kwargs=k) for i, k in zip(seq, kw)))


# ---------------------------------------------------------------------------
# 4. One app
# ---------------------------------------------------------------------------

_UNRESOLVED_REASONS = {
    "menu": "no compile class rule keys on this menu item",
    "control": "no compile class rule keys on this control",
    "sdef": ("scripting-dictionary command, typed signature only here: discovery maps no class to "
             "Apple events; execution/osa_compile.py compiles a few through fixed shapes, and their "
             "proof needs a per-app Automation grant, an owner click"),
    "bpy": ("bpy operator with typed RNA parameters; app_script compiles only the fixed "
            "blender_compile.KNOWN_SHAPES sequences"),
}


@dataclass
class AppDiscovery:
    bundle_id: str
    app: str
    candidates: List[Candidate]
    classes: List[ClassCandidate]
    notes: List[str] = field(default_factory=list)
    timing_ms: Dict[str, float] = field(default_factory=dict)

    def counts(self) -> Dict[str, Any]:
        by_src: Dict[str, int] = {}
        mapped_src: Dict[str, int] = {}
        kinds: Dict[str, int] = {}
        for c in self.candidates:
            by_src[c.source] = by_src.get(c.source, 0) + 1
            if c.status != "unresolved":
                mapped_src[c.source] = mapped_src.get(c.source, 0) + 1
            kinds[c.key_kind] = kinds.get(c.key_kind, 0) + 1
        menu = [c for c in self.candidates if c.source == "menu"]
        lang_free = sum(1 for c in menu if c.key_kind != "position")
        return {
            "discovered": len(self.candidates), "by_source": by_src,
            "source_candidates_mapped": sum(mapped_src.values()), "mapped_by_source": mapped_src,
            "class_candidates": len(self.classes),
            "class_candidates_proved": sum(1 for k in self.classes if k.status == "proved"),
            "classes": [k.op_class + (f"/{k.subtype}" if k.subtype else "") for k in self.classes],
            "key_kinds": kinds,
            "menu_language_independent": f"{lang_free}/{len(menu)}",
        }

    def to_dict(self):
        return {"bundle_id": self.bundle_id, "app": self.app, "counts": self.counts(),
                "timing_ms": self.timing_ms, "notes": self.notes,
                "classes": [k.to_dict() for k in self.classes],
                "candidates": [c.to_dict() for c in self.candidates]}


def discover_app(dump: Dict[str, Any]) -> AppDiscovery:
    """Everything this module does for one app, from one guest dump:
    ``{"bundle_id", "app", "menu_items", "window_controls",
    "sdef": {"xml", "includes"} | None, "script_suite": {"suite",
    "terminology"} | None, "bpy_catalog": [...] | None, "timing_ms"}``."""
    t0 = time.perf_counter()
    bid = dump.get("bundle_id") or ""
    notes: List[str] = list(dump.get("notes") or [])
    cands: List[Candidate] = []
    cands += menu_candidates(bid, dump.get("menu_items") or [])
    cands += control_candidates(bid, dump.get("window_controls") or [])

    commands: List[Dict[str, Any]] = []
    if dump.get("sdef"):
        commands = parse_sdef(dump["sdef"].get("xml") or "", dump["sdef"].get("includes") or {})
        miss = [c for c in commands if "_missing_includes" in c]
        if miss:
            notes.append(f"sdef includes not collected: {miss[0]['_missing_includes']}")
    elif dump.get("script_suite"):
        commands = parse_script_suite(dump["script_suite"].get("suite") or {},
                                      dump["script_suite"].get("terminology") or {})
        notes.append("legacy .scriptSuite: the implicit Cocoa standard suite is not listed")
    cands += sdef_candidates(bid, commands)
    if dump.get("bpy_catalog") is not None:
        cands += bpy_candidates(bid, dump["bpy_catalog"])

    classes: List[ClassCandidate] = []
    hits: List[RuleHit] = []
    for rule in AX_RULES:
        h = rule(bid, cands)
        if h is not None:
            hits.append(h)
    hits += rule_app_script(bid, cands)
    for h in hits:
        cid = f"{bid}:{h.op_class}" + (f"/{h.subtype}" if h.subtype else "")
        classes.append(ClassCandidate(id=cid, bundle_id=bid, op_class=h.op_class, subtype=h.subtype,
                                      triggers=[c.key for c in h.triggers], implied=list(h.implied),
                                      params=list(h.params), inferred_reason=h.inferred_reason))
        for c in h.triggers:
            c.status = "mapped"
            c.maps_to = cid
            c.reason = f"trigger of {cid} ({h.inferred_reason})"
    for c in cands:
        if c.status == "unresolved" and not c.reason:
            c.reason = _UNRESOLVED_REASONS[c.source]
    timing = dict(dump.get("timing_ms") or {})
    timing["map_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return AppDiscovery(bid, dump.get("app") or "", cands, classes, notes, timing)


# ---------------------------------------------------------------------------
# 5. Proof: only the existing routines, only on unseen inputs.
# ---------------------------------------------------------------------------

def attach_proof(k: ClassCandidate, report: Dict[str, Any], inputs: Sequence[Any],
                 seen_inputs: Sequence[Any] = ()) -> ClassCandidate:
    """Record a proof report from ``desktop_compile.prove`` or
    ``blender_compile.prove`` (``to_dict()`` form) on a class candidate.

    ``proved`` only if the report says so, every row agrees, at least
    ``MIN_FRESH_CASES`` distinct inputs were used, and none of them is in
    ``seen_inputs`` (anything recorded or replayed in an earlier round).
    Otherwise the candidate stays ``mapped`` and ``proof_reason`` says why."""
    k.proof = report
    distinct = {repr(i) for i in inputs}
    reused = sorted(distinct & {repr(i) for i in seen_inputs})
    rows = report.get("rows") or []
    if report.get("refusal_reason"):
        k.status, k.proof_reason = "mapped", f"proof refused: {report['refusal_reason']}"
    elif len(distinct) < dc.MIN_FRESH_CASES:
        k.status, k.proof_reason = "mapped", f"only {len(distinct)} distinct input(s)"
    elif reused:
        k.status, k.proof_reason = "mapped", f"inputs already seen in an earlier round: {reused}"
    elif not rows:
        k.status, k.proof_reason = "mapped", "proof has no rows"
    elif not report.get("proved") or not all(r.get("agree") for r in rows):
        bad = sum(1 for r in rows if not r.get("agree"))
        k.status, k.proof_reason = "mapped", f"proof failed: {bad}/{len(rows)} rows disagree"
    else:
        k.status = "proved"
        k.proof_reason = f"{len(rows)}/{len(rows)} rows agree over {len(distinct)} unseen inputs"
    return k


def summary_table(apps: Sequence[AppDiscovery]) -> List[Dict[str, Any]]:
    rows = []
    for a in apps:
        c = a.counts()
        rows.append({"app": a.app, "bundle_id": a.bundle_id, "discovered": c["discovered"],
                     "by_source": c["by_source"], "class_candidates": c["class_candidates"],
                     "classes": c["classes"], "source_candidates_mapped": c["source_candidates_mapped"],
                     "proved": c["class_candidates_proved"],
                     "menu_language_independent": c["menu_language_independent"],
                     "timing_ms": a.timing_ms})
    return rows
