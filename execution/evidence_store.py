"""Recompile evidence kept in the tool's own folder.

An automatic recompile (execution/auto_recompile.py) compiles from recordings: result
pages with the requests that answered them. A browser replay captures them live. Without
one, the recordings a tool was compiled from were often gone (a HAR, a ``learn`` run,
attach sessions removed after their retention), and a tool only the agent still answered
could not be rebuilt.

This module keeps them in the tool's folder (``evidence/`` beside ``compiled_api.json``),
so they move with the tool when it is renamed and go when it is deleted:

* **compile recordings** (:func:`keep_compile_evidence`): a compile from a recording
  folder, a HAR, attach or ``learn``, and a heal or a live automatic recompile that
  swapped a new request in, keep the recordings it was proved from.
  ``record["source"]["paths"]`` then names them relative to the folder.
* **the agent rung's pages** (:func:`keep_call_page`): the page the agent ended on, its
  visible text and the GET responses it made (credential-free and redacted, as the
  browser replay keeps them, ``cdp_replay.traffic_from_events``), when the agent's answer
  stands on that page. The newest :data:`MAX_KEPT_CALLS` inputs are kept, each at most
  :data:`MAX_CALL_AGE_S` old; once :data:`MIN_CALLS` inputs are kept they recompile the
  tool (:func:`call_evidence`): the newest compiles, the next selects, the others validate.

A signed-in tool, and a signed-in capture, keep nothing. ``ANYUSE_KEEP_EVIDENCE=off``
keeps nothing new. Files are written to a temporary name and moved into place.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

SETTING = "ANYUSE_KEEP_EVIDENCE"
DIR = "evidence"
CALLS_DIR = "calls"
#: pages of calls the agent answered that are kept, newest first
MAX_KEPT_CALLS = 6
#: distinct inputs a recompile from those pages needs: compile, selection, two held out
#: (as ``step_heal.MIN_VALUES``)
MIN_CALLS = 4
#: a kept page older than this is not used
MAX_CALL_AGE_S = 86400.0
#: share of the agent's answer its page must show
ON_PAGE = 0.8
FROM_CALLS = "the pages of calls the agent answered"
_ROLES = ("fitting", "selection", "fresh")


def enabled() -> bool:
    """``ANYUSE_KEEP_EVIDENCE``: on unless switched off."""
    try:
        from anyuse.settings import get as get_setting
        return bool(get_setting(SETTING))
    except Exception:  # noqa: BLE001 - an unreadable settings file keeps the default
        return str(os.environ.get(SETTING, "on")).strip().lower() not in ("0", "off", "false", "no")


def folder(storage, workflow_id: str) -> str:
    return os.path.join(str(storage.get_workflow_dir(workflow_id)), DIR)


def _norm(value: Any) -> str:
    from anyuse.execution.api_compile import normalise
    return normalise(str(value or ""))


def _write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _clean(recording: Dict[str, Any]) -> Dict[str, Any]:
    """The recording without the caller's own keys (``_path``), and its requests without the
    values of credential headers (a cookie, an ``Authorization``, a session or API key):
    a recording folder holds what its recorder saw. The names stay, since the compile
    judges a request by them."""
    out = {k: v for k, v in dict(recording or {}).items() if not str(k).startswith("_")}
    if isinstance(out.get("network"), list):
        out["network"] = [_without_credentials(e) if isinstance(e, dict) else e
                          for e in out["network"]]
    return out


def _without_credentials(entry: Dict[str, Any]) -> Dict[str, Any]:
    from anyuse.execution.api_step import CREDENTIAL_HEADERS, signed_param_reason
    out = dict(entry)
    for key in ("headers", "response_headers"):
        headers = out.get(key)
        if isinstance(headers, dict):
            out[key] = {name: ("" if str(name).lower() in CREDENTIAL_HEADERS
                               or signed_param_reason(str(name)) else value)
                        for name, value in headers.items()}
    return out


def _signed(record: Dict[str, Any], recordings: Sequence[Dict[str, Any]] = ()) -> bool:
    from anyuse.execution.auto_recompile import signed_in
    try:
        if signed_in(record or {}):
            return True
    except Exception:  # noqa: BLE001 - an unreadable template keeps nothing
        return True
    return any((r or {}).get("login_replay_site") for r in recordings)


# ---------------------------------------------------------------------------
# compile recordings
# ---------------------------------------------------------------------------

def keep_compile_evidence(storage, workflow_id: str, record: Dict[str, Any], *,
                          recording: Optional[Dict[str, Any]] = None,
                          fitting: Sequence[Dict[str, Any]] = (),
                          selection: Sequence[Dict[str, Any]] = (),
                          fresh: Sequence[Dict[str, Any]] = (),
                          sessions: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Keep the recordings ``record`` was proved from in its folder and point
    ``record["source"]["paths"]`` at them (relative to the folder). ``sessions``: attach
    sessions, compile session first, when no ``recording`` is given. Returns the paths
    kept, ``{}`` when nothing was (the setting is off, a signed-in tool or capture, a
    write that failed). Never raises."""
    if recording is None and sessions:
        recording, fresh = sessions[0], list(sessions[1:])
    if not recording or not enabled():
        return {}
    roles = {"fitting": list(fitting or ()), "selection": list(selection or ()),
             "fresh": list(fresh or ())}
    if _signed(record, [recording, *roles["fitting"], *roles["selection"], *roles["fresh"]]):
        return {}
    try:
        base = folder(storage, workflow_id)
        paths: Dict[str, Any] = {"recording": f"{DIR}/recording.json"}
        _write(os.path.join(base, "recording.json"), _clean(recording))
        for role in _ROLES:
            paths[role] = []
            for i, rec in enumerate(roles[role]):
                name = f"{role}-{i}.json"
                _write(os.path.join(base, name), _clean(rec))
                paths[role].append(f"{DIR}/{name}")
        kept = {os.path.basename(p) for role in _ROLES for p in paths[role]} | {"recording.json"}
        for name in os.listdir(base):
            # an older set's files this set does not have
            if name.endswith(".json") and name not in kept:
                os.remove(os.path.join(base, name))
    except OSError as exc:
        logger.debug("could not keep the compile evidence of %r: %s", workflow_id, exc)
        return {}
    source = dict(record.get("source") or {})
    source["paths"] = paths
    source["kept"] = True
    record["source"] = source
    return paths


# ---------------------------------------------------------------------------
# the agent rung's pages
# ---------------------------------------------------------------------------

def _visible_text(dom: str) -> str:
    from anyuse.execution.html_extract import parse
    from anyuse.execution.result_contract import visible_lines
    try:
        return "\n".join(t for t, _b, _h in visible_lines(parse(dom or "", visible_only=True)))
    except ValueError:
        return ""


def _calls_dir(storage, workflow_id: str) -> str:
    return os.path.join(folder(storage, workflow_id), CALLS_DIR)


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def keep_call_page(storage, workflow_id: str, record: Dict[str, Any], *, param: str, value: str,
                   page: Optional[Dict[str, Any]], items: Sequence[Any], rung: str = "agent",
                   now: Optional[float] = None) -> str:
    """Keep the page ``rung`` answered ``value`` on (``page``: ``final_url``, ``dom``,
    ``text``, ``network``) with its answer ``items`` as a recording; an older page of the
    same input is replaced and the newest :data:`MAX_KEPT_CALLS` are kept. Kept only when
    at least :data:`ON_PAGE` of the answer stands in the page's visible text. Returns
    ``"kept"`` or why not. Never raises."""
    from anyuse.execution.api_recompile import workflow_lock
    from anyuse.execution.attach import redact_html, redact_text
    if not enabled():
        return f"keeping evidence is off ({SETTING})"
    if _signed(record):
        return "a signed-in tool keeps no page"
    page = dict(page or {})
    dom = str(page.get("dom") or "")
    names = [str(x) for x in (items or []) if str(x or "").strip()][:10]
    if not dom or not names or not _norm(value):
        return "no page or no answer to keep"
    text = str(page.get("text") or "") or _visible_text(dom)
    shown = _norm(text)
    on = sum(1 for n in names if _norm(n) and _norm(n) in shown) / len(names)
    if on < ON_PAGE:
        return f"{on:.0%} of the answer stands on its page, {ON_PAGE:.0%} needed"
    final_url = str(page.get("final_url") or "")
    network = []
    for entry in page.get("network") or []:
        if not isinstance(entry, dict) or entry.get("credential_bound"):
            continue
        body = entry.get("body")
        if isinstance(body, str):
            body = redact_html(body) if "html" in str(entry.get("mime") or "").lower() \
                else redact_text(body)
        network.append({**_clean(entry), "body": body})
    rec = {"workflow": str(record.get("workflow") or ""), "tag": "call", "origin": rung,
           "site": urllib.parse.urlsplit(final_url).netloc,
           "param": {"name": str(param), "value": str(value)}, "final_url": final_url,
           "dom": redact_html(dom), "network": network,
           "ui": {"items": names, "text": redact_text(text)[:20000]},
           "kept_at": float(time.time() if now is None else now)}
    name = hashlib.sha256(_norm(value).encode("utf-8")).hexdigest()[:16] + ".json"
    try:
        with workflow_lock(storage, workflow_id):
            base = _calls_dir(storage, workflow_id)
            _write(os.path.join(base, name), rec)
            pages = []
            for fn in os.listdir(base):
                if fn.endswith(".json"):
                    got = _read(os.path.join(base, fn)) or {}
                    pages.append((float(got.get("kept_at") or 0.0), fn))
            for _at, fn in sorted(pages, reverse=True)[MAX_KEPT_CALLS:]:
                os.remove(os.path.join(base, fn))
    except OSError as exc:
        logger.debug("could not keep a call page of %r: %s", workflow_id, exc)
        return f"the page could not be written ({type(exc).__name__})"
    return "kept"


def kept_calls(storage, workflow_id: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The kept call pages as recordings, newest first, none older than
    :data:`MAX_CALL_AGE_S`."""
    base = _calls_dir(storage, workflow_id)
    now = time.time() if now is None else now
    out = []
    try:
        names = [fn for fn in os.listdir(base) if fn.endswith(".json")]
    except OSError:
        return []
    for fn in names:
        rec = _read(os.path.join(base, fn))
        if rec and now - float(rec.get("kept_at") or 0.0) <= MAX_CALL_AGE_S:
            out.append(rec)
    out.sort(key=lambda r: float(r.get("kept_at") or 0.0), reverse=True)
    return out


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def call_evidence(storage, workflow_id: str, record: Dict[str, Any],
                  now: Optional[float] = None) -> Dict[str, Any]:
    """The kept call pages in the shape ``RecompileManager.recompile_now`` loads: the newest
    compiles, the next selects, the others validate. Raises ``api_recompile.NoEvidence``
    (nothing spent) below :data:`MIN_CALLS` distinct inputs."""
    from anyuse.execution.api_recompile import NoEvidence
    if _signed(record):
        raise NoEvidence("a signed-in tool keeps no page", spent=False)
    calls, seen = [], set()
    for rec in kept_calls(storage, workflow_id, now):
        key = _norm((rec.get("param") or {}).get("value"))
        if key and key not in seen:
            seen.add(key)
            calls.append(rec)
    if len(calls) < MIN_CALLS:
        raise NoEvidence(f"{len(calls)} of {MIN_CALLS} agent pages kept", spent=False)
    origins = list(dict.fromkeys(o for r in calls for o in (
        _origin(r.get("final_url") or ""), *(_origin(n.get("url") or "") for n in r.get("network") or []))
        if o))
    return {"recording": calls[0], "selection": [calls[1]], "fresh": calls[2:],
            "allowed_origins": origins, "from": FROM_CALLS}
