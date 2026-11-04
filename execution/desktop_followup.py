"""Window follow-ups for the effect probe.

The effect probe alone gives every press that "opens a window" (or a dialog/sheet) no class.
Some of those windows ARE the operation: Finder's search opens a window with
a focused search field, and a naming sheet asks for the new object's name
before the object exists. So the probe now looks INTO the new window:

    press ── new window/dialog/sheet ── facts (fields, default button,
             containers, auth) ── plan: fill + confirm, or read only
             ── classify the change ── map to scoped_find / object_create /
             field_rw, or no class with the reason ── close the window

Safety rules decide whether a window is filled at all (roles and the probe
start's model only; never a title):

* an authentication prompt (a secure field) is never filled;
* a combo box is never filled: by convention it offers remembered entries
  (server addresses, locations); a text area is a document body, not a name;
* a search field is filled only in a new standard window of an app whose
  probe start is a file-system folder (the only scope with a same-rule
  backend); anywhere else a search may reach a network source;
* a plain text field is filled only when it is the window's ONLY editable
  field, the window holds no container (a file panel or a form has several
  fields and a browser) and the app's probe start has a model in which a new
  object could be observed (a folder, or a scope list with counts).

The fill value is a probe token (no dot, no slash, no scheme: never a host
name or a path). Everything here is pure; the guest supplies dicts.

Facts of a new window (the guest reads them right after the trigger):

    {"kind": "window" | "dialog" | "sheet",
     "fields": [{"role", "subrole", "identifier", "focused", "settable", "secure"}],
     "default_button": bool, "cancel_button": bool, "close_button": bool,
     "containers": [container keys inside the new window], "auth": bool}

A follow-up observation (``pre`` right after the trigger, ``post`` after the
fill + confirm):

    {"windows": int,                         # AXWindow count of the app
     "sheets": int,
     "containers": {key: [item labels]},     # inside the new window
     "field": str | None,                    # the filled field's value
     "reopened_field": str | None,           # its value after the trigger opened it again
     "model": <model_state>, "labels": [static labels of the main window]}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

TEXT_ENTRY = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
SEARCH = "AXSearchField"
SECURE = "AXSecureTextField"


def _has_model(model: Any) -> bool:
    m = model or {}
    return m.get("kind") == "folder" or isinstance(m.get("scopes"), dict)


def plan(facts: Optional[Dict[str, Any]], start_model: Any) -> Tuple[Optional[str], str]:
    """("search" | "name" | None, why). None = the window is only read and
    then closed."""
    if not isinstance(facts, dict):
        return None, "the new window could not be read"
    fields = list(facts.get("fields") or [])
    if facts.get("auth") or any(f.get("secure") or f.get("role") == SECURE or f.get("subrole") == SECURE
                                for f in fields):
        return None, "authentication prompt: never filled"
    focused = [f for f in fields if f.get("focused")]
    if not focused:
        return None, "no focused field in the new window: read only"
    f = focused[0]
    role, sub = f.get("role") or "", f.get("subrole") or ""
    editable = [x for x in fields if (x.get("role") or "") in TEXT_ENTRY and x.get("settable") is not False]
    if sub == SEARCH:
        if facts.get("kind") != "window":
            return None, "search field in a dialog or sheet: not filled"
        if (start_model or {}).get("kind") != "folder":
            return None, ("search field, but the probe start is not a file-system folder: not filled "
                          "(no same-rule backend; the search may reach a network source)")
        return "search", "search field focused in a new window over a folder scope"
    if role == "AXComboBox":
        return None, "combo box (remembered entries such as addresses): never filled"
    if role == "AXTextArea":
        return None, "text area (a document body, not a name): not filled"
    if role != "AXTextField":
        return None, f"focused {role or 'element'} is not a text entry: read only"
    if f.get("settable") is False:
        return None, "the focused field is not editable: read only"
    if len(editable) != 1:
        return None, f"{len(editable)} editable fields (a form or a file panel): not filled"
    if facts.get("containers"):
        return None, "the window holds a container (a browser or a list): not filled"
    if not _has_model(start_model):
        return None, ("no model in which a created object could be observed (no folder, no scope "
                      "list): not filled")
    return "name", "a single plain text field focused: a naming step"


def classify(pre: Dict[str, Any], post: Dict[str, Any], action: Optional[str], value: str) -> Dict[str, Any]:
    """The follow-up's effects after the fill + confirm."""
    eff: List[str] = []
    closed = (post.get("windows") or 0) < (pre.get("windows") or 0) or \
        (post.get("sheets") or 0) < (pre.get("sheets") or 0)
    if closed:
        eff.append("window_closed_on_confirm")
    pc, qc = pre.get("containers") or {}, post.get("containers") or {}
    changed = sorted(k for k in set(pc) | set(qc) if pc.get(k) != qc.get(k))
    if changed and not closed:
        eff.append("results_changed")
    bm, am = pre.get("model") or {}, post.get("model") or {}
    grew_items = sorted(set(am.get("items") or []) - set(bm.get("items") or []))
    grew_scopes = sorted(set((am.get("scopes") or {})) - set((bm.get("scopes") or {})))
    new_labels = sorted(set(post.get("labels") or []) - set(pre.get("labels") or []))
    if grew_items or grew_scopes:
        eff.append("object_created")
    if value and (value in grew_items or value in grew_scopes or value in new_labels):
        eff.append("object_named")
    if action == "name" and not closed and post.get("field") == value:
        eff.append("field_kept")
    if action == "name" and closed and post.get("reopened_field") == value:
        # the surface was opened again by the same trigger and its field
        # shows the committed value: the field persists it
        eff.append("value_persisted")
    # ``grew`` are OBJECT NAMES (never container keys); ``seen_through`` says
    # where the committed name was seen, which is how the proof will observe
    # it: the backend listing, the model's scopes, or a container label
    seen = "model" if value in grew_items else "scopes" if value in grew_scopes else \
        "labels" if value in new_labels else None
    return {"effects": eff, "changed": changed, "grew": grew_items + grew_scopes, "seen_through": seen}


def map_followup(action: Optional[str], effects: List[str], why: str = ""
                 ) -> Tuple[Optional[str], str]:
    """(contract class | None, reason)."""
    e = set(effects or [])
    if action is None:
        return None, f"follow-up read only: {why}" if why else "follow-up read only"
    if action == "search":
        if "results_changed" in e:
            return "scoped_find", "the search field in the new window changed its result container"
        return None, "search filled, no container in the new window changed"
    if action == "name":
        if "object_named" in e:
            return "object_create", "the name committed in the window created an object with that name"
        if "object_created" in e:
            return None, "an object appeared, but not with the committed name"
        if "value_persisted" in e and "object_created" not in e:
            return "field_rw", "the surface closed on confirm and shows the value when opened again"
        if "field_kept" in e:
            return None, ("the confirm did not complete (the surface stayed open with the value): "
                          "no object, and a kept value alone is not a field write")
        return None, "name committed, no object with that name appeared"
    return None, f"unknown follow-up action {action!r}"
