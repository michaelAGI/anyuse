"""Guest drive helpers and host-relayed model adapter for ``desktop_e2e_job.py``.

Used by the guest job agent's ``record_te`` and ``fill_draft`` operations:
fixed live AX target resolution rules (menu items by command key, front text
area, save sheet controls), UIState fingerprinting, and ``RelayModel`` (the
file-based model relay under ``~/anyuse-guest/relay`` so the guest holds no API
key and makes no network call).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

FILE_MENU = ("Ablage", "File")
NEW_DOC_BUTTON = ("Neues Dokument", "New Document")
SAVE_BUTTON = ("Sichern", "Save")


def fingerprint_state(tree, state_id: str, app: str):
    """A UIState fingerprinted the way ``execution/guest/flows.py`` ``make_state_graph`` does."""
    from anyuse.context.accessibility import ACTIONABLE_ROLES, drift_excludes_menubar, volatile_anchor
    from anyuse.guides import state_graph as sg
    descs, refs = [], []
    tree._walk(tree._root(), 0, descs, refs)
    live = [{"role": d.role, "title": d.title or ""} for _e, d in refs]
    excl = drift_excludes_menubar()
    actionable = [{"role": d.role, "title": d.title or "", "identifier": d.identifier or ""} for _e, d in refs
                  if d.role in ACTIONABLE_ROLES and not (excl and d.in_menubar) and not volatile_anchor(d)]
    fp = sg.UIFingerprint(
        required_elements=actionable,
        element_count_range=(int(len(live) * 0.6), int(len(live) * 1.4) + 10),
        structural_hash=sg.compute_structural_hash(live),
        role_frequency=sg._role_frequency(live))
    return sg.UIState(state_id=state_id, app=app, fingerprint=fp)


def _refs(tree):
    descs, refs = [], []
    tree._walk(tree._root(), 0, descs, refs)
    return refs


def _attr(el, name):
    from anyuse.context import accessibility as acc
    return acc._attr(el, name)


def resolve_menu_item(tree, cmd_char: str, modifiers=("", "0")):
    for el, d in _refs(tree):
        if d.role != "AXMenuItem":
            continue
        c = str(_attr(el, "AXMenuItemCmdChar") or "")
        m = str(_attr(el, "AXMenuItemCmdModifiers") if _attr(el, "AXMenuItemCmdModifiers") is not None else "")
        if c.lower() == cmd_char.lower() and m in modifiers:
            return el, d
    return None, None


def resolve_menu_bar_item(tree, titles):
    """A top-level menu (Ablage/File, Format)."""
    for el, d in _refs(tree):
        if d.role == "AXMenuBarItem" and (d.title or "") in titles:
            return el, d
    return None, None


def resolve_button(tree, titles):
    for el, d in _refs(tree):
        if d.role == "AXButton" and (d.title or "") in titles:
            return el, d
    return None, None


def resolve_front_text_area(tree):
    win = _attr(tree._root(), "AXFocusedWindow")
    for el, d in _refs(tree):
        if d.role == "AXTextArea" and (win is None or _attr(el, "AXWindow") == win):
            return el, d
    return None, None


def resolve_save_name_field(tree):
    """The save sheet's name field: AXIdentifier saveAsNameTextField, else the
    sheet's one text field whose value ends in .txt / .rtf."""
    fields = [(el, d) for el, d in _refs(tree) if d.role == "AXTextField"]
    for el, d in fields:
        if (d.identifier or "") == "saveAsNameTextField":
            return el, d
    named = [(el, d) for el, d in fields
             if str(d.value or "").lower().endswith((".txt", ".rtf"))]
    return named[0] if len(named) == 1 else (None, None)


def save_location_title(tree) -> Optional[str]:
    for el, d in _refs(tree):
        if d.role == "AXPopUpButton" and (d.value or d.title):
            v = str(d.value or d.title)
            if v in ("Dokumente", "Documents", "Schreibtisch", "Desktop", "iCloud Drive", "TextEdit"):
                return v
    return None


def step_for(tree, el, d, intent: str, action: str, input_data: Optional[str] = None, **extra):
    from anyuse.execution.plan_types import PlannedStep
    sel = tree.to_selector(el)
    cmd = {"role": d.role, "title": d.title or "", "action": action, "selector": sel.to_dict(), **extra}
    return PlannedStep(intent=intent, source="workflow", target_element=f"{d.role} {d.title or ''}",
                       modality="ax", command=cmd, input_data=input_data)


RELAY_PROTOCOL = "anyuse.vm-relay/1"
RELAY_WAIT_S = 240.0
RELAY_POLL_S = 0.5


def relay_name(kind: str, n: int) -> str:
    """``req-<n>.json`` / ``resp-<n>.json``, numbered from 1 in the order the fill asks."""
    if kind not in ("req", "resp") or int(n) < 1:
        raise ValueError(f"relay file {kind!r} {n!r}")
    return f"{kind}-{int(n)}.json"


def write_atomic(path: str, data: bytes) -> None:
    """Write a dot-file beside ``path``, then rename it into place."""
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class RelayError(RuntimeError):
    """The relay gave no usable answer; ``meta`` is what the fill's model gate books."""

    def __init__(self, message: str, meta: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.meta = dict(meta or {})


class RelayModel:
    """``draft_fill``'s ``llm(messages, schema) -> (text, meta)`` with the model on the HOST.

    Each call writes the next ``req-<n>.json`` and waits for ``resp-<n>.json``. No answer within
    ``wait_s`` raises (``calls`` 1: the host may have sent the request); so does an answer that is
    not the protocol's or not numbered ``n`` (malformed), and an ``ok: false`` answer (with the
    host's meta). Files are never deleted.
    """

    def __init__(self, relay_dir: str, *, wait_s: float = RELAY_WAIT_S, poll_s: float = RELAY_POLL_S,
                 log: Optional[Any] = None, clock=time.monotonic, sleep=time.sleep):
        self.dir, self.wait_s, self.poll_s, self.log = relay_dir, wait_s, poll_s, log
        self.clock, self.sleep = clock, sleep
        self.n = 0
        os.makedirs(relay_dir, exist_ok=True)

    def _event(self, kind: str, **kw) -> None:
        if self.log is not None:
            self.log.event(kind, **kw)

    def __call__(self, messages: List[Dict[str, str]], schema: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        self.n += 1
        n = self.n
        req = {"protocol": RELAY_PROTOCOL, "n": n, "messages": messages, "schema": schema,
               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        data = json.dumps(req, ensure_ascii=False).encode("utf-8")
        write_atomic(os.path.join(self.dir, relay_name("req", n)), data)
        self._event("relay_request", n=n, bytes=len(data))
        path = os.path.join(self.dir, relay_name("resp", n))
        t0 = self.clock()
        while not os.path.exists(path):
            if self.clock() - t0 >= self.wait_s:
                self._event("relay_timeout", n=n, waited_s=round(self.clock() - t0, 1))
                raise RelayError(f"no relay answer to request {n} within {self.wait_s:.0f} s", {"calls": 1})
            self.sleep(self.poll_s)
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            resp = None
        why = ("not a JSON object" if not isinstance(resp, dict) else
               f"protocol {str(resp.get('protocol'))[:40]!r}" if resp.get("protocol") != RELAY_PROTOCOL else
               f"numbered {resp.get('n')!r}, not {n}" if resp.get("n") != n else
               "ok must be true or false" if not isinstance(resp.get("ok"), bool) else
               "meta must be an object" if not isinstance(resp.get("meta"), dict) else
               "text must be text" if resp["ok"] and not isinstance(resp.get("text"), str) else "")
        if why:
            self._event("relay_malformed", n=n, why=why)
            raise RelayError(f"malformed relay answer to request {n}: {why}", {"calls": 1})
        self._event("relay_response", n=n, ok=resp["ok"], seconds=resp.get("seconds"),
                    error=str(resp.get("error") or "")[:200], waited_s=round(self.clock() - t0, 1))
        if not resp["ok"]:
            from anyuse.execution.draft_params import ModelCallFailed
            raise ModelCallFailed(f"the host's model: {str(resp.get('error') or 'no answer')[:200]}",
                                  resp["meta"])
        return resp["text"], resp["meta"]
