"""``anyuse watch``: change monitoring with compiled tools (docs/WATCH.md).

A WATCHED ITEM is one compiled tool and one input (``resolve_tool``): a compiled demonstration tool
(``execution/demo_compile.py``, bare or saved as ``compiled_demo.json``) or a compiled web tool (``compiled_api.json``).
``watch run`` replays the tool for every DUE item - 0 model calls; a demonstration tool in the guarded browser under
the traffic guard (``execution/watch_live.py``), a compiled web tool through its proved request with the checks of
every call (``make_api_replay``) - and stores a dated SNAPSHOT: the structured output and its evidence (the
page or request URL, where each field came from, the time). A deterministic DIFF against the item's last GOOD snapshot
(field changes; price and availability changes; results added, removed, retitled or moved in a list) goes
into the changelog (``CHANGELOG.md`` for people, ``changelog.jsonl`` for tools) with the evidence of both
sides. A failed or incomplete replay, or a CONFLICT field, is DRIFT ("re-demonstrate") and NEVER a content
change: the last good snapshot stays the baseline. A compiled web tool whose site did not answer (a refused
connection, a timeout, a server error) is an OUTAGE: neither drift nor a content change, nothing to recompile,
and the last good snapshot stays the baseline. A ``needs_review`` replay (the attribute verifier on: the page's
two sources disagree; ``attribute_verify.dispute_of``) is neither: an entry of kind ``needs_review`` with both
sources' evidence, never a content change, never a baseline, never accepted.

Also: dated snapshots; ``watch run --dry-run`` prints what the changelog would say
and writes nothing to the store; a changelog that carries its evidence; an advisory agent hook that is only
PRINTED (``install_hook_text``), never installed.

Safety: every browser contact goes through the benches' guarded browser and traffic guard, and a compiled web
tool's request through the same executor as ``anyuse workflow play --no-fallback``; per-site caps per run
and per day; a minimum interval of 1 h per watched item (``MIN_INTERVAL_S``), for dry runs too - the
guard's traffic ledger (``guard/traffic.json``) books every contact, and it is the one file a dry run
writes; robots.txt respected; a site that blocks is never contacted again; a traffic violation stops every
run (``guard/HARD_STOP.json``).

Optional ``--explain`` (off by default): only when a diff is non-empty, a model says what changed - the
self-hostable OpenJev server (a typed choice among the diff's own lines, with a fixed last option "nothing
important changed" for a cosmetic diff) or Cerebras through the model adapter (one short summary); never the
hosted Jev API, never OpenRouter. Every model call is logged per run.

This module is pure but for the store's files; the replay and the explainer are injected (fakes in the
tests: ``test_watch.py``).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: the minimum interval between two contacts for one watched item (seconds); never lower
MIN_INTERVAL_S = 3600
DEFAULT_EVERY = "1d"
#: per-site caps: one run (the guard's share), and one UTC day over every run and dry run
RUN_REQUESTS = 60
RUN_BYTES = 8 * 1024 * 1024
DAY_REQUESTS = 200
DAY_BYTES = 24 * 1024 * 1024


def caps() -> Dict[str, int]:
    """The per-site caps in force: the defaults, LOWERED (never raised) by ``ANYUSE_WATCH_RUN_REQUESTS``,
    ``ANYUSE_WATCH_RUN_BYTES``, ``ANYUSE_WATCH_DAY_REQUESTS``, ``ANYUSE_WATCH_DAY_BYTES``."""
    out = {"run_requests": RUN_REQUESTS, "run_bytes": RUN_BYTES, "day_requests": DAY_REQUESTS, "day_bytes": DAY_BYTES}
    for k in list(out):
        raw = os.environ.get("ANYUSE_WATCH_" + k.upper(), "").strip()
        try:
            if raw and int(raw) > 0:
                out[k] = min(out[k], int(raw))
        except ValueError:
            pass
    return out


#: an output a replay may leave as the item's content
GOOD = ("ok", "no_results")
CONFLICT = "CONFLICT"


# ---------------------------------------------------------------------------
# time and small helpers
# ---------------------------------------------------------------------------

def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_every(text: Any) -> int:
    """``1d`` / ``12h`` / ``90m`` / ``3600s`` / a number of seconds -> seconds; refused below 1 h."""
    s = str(text or DEFAULT_EVERY).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw]?)", s)
    if not m:
        raise ValueError(f"--every {text!r}: use a number with s, m, h, d or w (e.g. 1d, 6h)")
    n = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    if n < MIN_INTERVAL_S:
        raise ValueError(f"--every {text!r} is below the minimum interval of 1 h per watched item")
    return int(n)


def fold(s: Any) -> str:
    from anyuse.execution import web_arch as W
    return " ".join(W.fold(s).split())


def norm_url(u: Any) -> str:
    from anyuse.execution import web_arch as W
    return W.norm_url(str(u or "").split("#", 1)[0])


def _slug(s: str, n: int = 40) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", fold(s)).strip("-") or "item")[:n]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def default_root() -> Path:
    raw = os.environ.get("ANYUSE_WATCH_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    from anyuse.paths import anyuse_data_path
    return anyuse_data_path("watch")


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class WatchStore:
    """``items.json``, ``tools/<sha256>.tool.json`` (the pinned tool copies), ``snapshots/<item>/<time>.json``,
    ``changelog.jsonl`` + ``CHANGELOG.md``, ``runs.jsonl``; the guard's ``guard/traffic.json`` (every contact,
    dry runs included), ``guard/HARD_STOP.json``, ``guard/cache/`` (the static-asset cache)."""

    def __init__(self, root: Any = None):
        self.root = Path(root) if root else default_root()

    # -- paths -------------------------------------------------------------------------------------
    @property
    def items_path(self) -> Path:
        return self.root / "items.json"

    @property
    def traffic_path(self) -> Path:
        return self.root / "guard" / "traffic.json"

    @property
    def hard_stop_path(self) -> Path:
        return self.root / "guard" / "HARD_STOP.json"

    @property
    def cache_dir(self) -> Path:
        return self.root / "guard" / "cache"

    def snap_dir(self, item_id: str) -> Path:
        return self.root / "snapshots" / item_id

    # -- items -------------------------------------------------------------------------------------
    def items(self) -> Dict[str, Dict[str, Any]]:
        return (_read_json(self.items_path, {}) or {}).get("items") or {}

    def save_items(self, items: Dict[str, Dict[str, Any]]) -> None:
        _write_json(self.items_path, {"version": 1, "items": items})

    def add(self, tool_path: str, value: str, every: Any = DEFAULT_EVERY,
            now: Optional[_dt.datetime] = None, params: Optional[Dict[str, str]] = None,
            storage: Any = None, hosts_path: Optional[str] = None) -> Dict[str, Any]:
        """Watch ``value`` with TOOL (``resolve_tool``: a tool name or a tool file; a pinned copy is kept).
        ``params``: the other inputs of a compiled web tool that takes more than one."""
        from anyuse.execution import demo_compile as C
        found = resolve_tool(tool_path, storage, hosts_path)
        if not str(value or "").strip():
            raise ValueError("the input is empty")
        secs = parse_every(every)
        if found["kind"] == "api":
            return self._add_api(found, value, secs, now, dict(params or {}))
        if params:
            raise ValueError("--param is only for a compiled web tool that takes more than one input")
        tool = found["tool"]
        sha = C.tool_digest(tool)
        _write_json(self.root / "tools" / f"{sha}.tool.json", tool)
        iid = f"{_slug(str(tool.get('site')), 30)}--{_slug(value)}--{hashlib.sha256((sha + value).encode()).hexdigest()[:8]}"
        items = self.items()
        if iid in items:
            raise ValueError(f"already watched: {iid}")
        item = {"id": iid, "site": str(tool.get("site") or ""), "workflow": tool.get("workflow"), "input": value,
                "tool_sha256": sha, "tool_version": tool.get("version"), "tool_source": found["source"],
                "every_s": secs, "added_at": iso(now or utcnow()), "last_run_at": None, "last_status": None,
                "last_good": None, "drift_since": None}
        if found["name"]:
            item["tool_name"] = found["name"]
        items[iid] = item
        self.save_items(items)
        return item

    def _add_api(self, found: Dict[str, Any], value: str, secs: int, now: Optional[_dt.datetime],
                 params: Dict[str, str]) -> Dict[str, Any]:
        """A compiled web tool: ``value`` binds to its query input, ``params`` to the others."""
        from anyuse.execution import demo_compile as C
        record, info, name = found["record"], found["info"], found["name"]
        inputs = [str(p) for p in info.get("parameters") or []]
        if not inputs:
            raise ValueError(f"{name} takes no input; watch needs a tool with an input")
        param = _query_param(record, inputs)
        unknown = sorted(k for k in params if k not in inputs)
        if unknown:
            raise ValueError(f"{name} has no input {', '.join(unknown)}; its inputs are {', '.join(inputs)}")
        if param in params:
            raise ValueError(f"{param} is the INPUT argument; give it once")
        missing = [p for p in inputs if p != param and p not in params]
        if missing:
            raise ValueError(f"{name} also needs {', '.join(missing)}: add --param {missing[0]}=VALUE")
        sha = C.tool_digest(record)
        _write_json(self.root / "tools" / f"{sha}.tool.json", record)
        key = sha + value + (json.dumps(params, sort_keys=True) if params else "")
        site = str(info.get("host") or "")
        iid = f"{_slug(site, 30)}--{_slug(value)}--{hashlib.sha256(key.encode()).hexdigest()[:8]}"
        items = self.items()
        if iid in items:
            raise ValueError(f"already watched: {iid}")
        from anyuse.mcpserver.compiled_tools import KIND_BROWSER
        item = {"id": iid, "site": site, "workflow": API_WORKFLOW, "tool_name": name, "param": param,
                "params": params, "input": value, "hosts": list(info.get("hosts") or [site]),
                "needs_browser": info.get("kind") == KIND_BROWSER, "tool_sha256": sha,
                "tool_version": record.get("version"), "proof_id": (info.get("proof") or {}).get("id"),
                "tool_source": found["source"], "every_s": secs, "added_at": iso(now or utcnow()),
                "last_run_at": None, "last_status": None, "last_good": None, "drift_since": None}
        items[iid] = item
        self.save_items(items)
        return item

    def remove(self, item_id: str) -> bool:
        items = self.items()
        if items.pop(item_id, None) is None:
            return False
        self.save_items(items)
        return True

    def tool_of(self, item: Dict[str, Any]) -> Dict[str, Any]:
        from anyuse.execution import demo_compile as C
        tool = _read_json(self.root / "tools" / f"{item['tool_sha256']}.tool.json", None)
        if not isinstance(tool, dict) or C.tool_digest(tool) != item["tool_sha256"]:
            raise ValueError(f"{item['id']}: its pinned tool is missing or changed")
        return tool

    # -- snapshots and logs ------------------------------------------------------------------------
    def write_snapshot(self, snap: Dict[str, Any]) -> str:
        name = snap["at"].replace(":", "").replace("-", "") + ".json"
        _write_json(self.snap_dir(snap["item"]) / name, snap)
        return name

    def snapshot(self, item_id: str, name: Optional[str]) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        return _read_json(self.snap_dir(item_id) / name, None)

    def snapshots(self, item_id: str) -> List[str]:
        d = self.snap_dir(item_id)
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []

    def append_changelog(self, entry: Dict[str, Any]) -> None:
        _append(self.root / "changelog.jsonl", json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        md = self.root / "CHANGELOG.md"
        if not md.exists():
            _append(md, CHANGELOG_HEAD)
        _append(md, render_entry(entry))

    def changelog(self) -> List[Dict[str, Any]]:
        out = []
        try:
            with open(self.root / "changelog.jsonl", encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip():
                        out.append(json.loads(ln))
        except OSError:
            pass
        return out

    def append_run(self, report: Dict[str, Any]) -> None:
        _append(self.root / "runs.jsonl", json.dumps(report, ensure_ascii=False, default=str) + "\n")

    # -- the guard's ledger ------------------------------------------------------------------------
    def traffic(self) -> Dict[str, Any]:
        return _read_json(self.traffic_path, {}) or {}

    def book(self, site: str, requests: int, nbytes: int, item_id: str, at: _dt.datetime, dry_run: bool) -> None:
        """Book one contact (a run or a dry run: the ledger is never skipped)."""
        t = self.traffic()
        day = at.strftime("%Y-%m-%d")
        d = t.setdefault("sites", {}).setdefault(site, {}).setdefault(day, {"requests": 0, "bytes": 0, "contacts": 0})
        d["requests"] += int(requests or 0)
        d["bytes"] += int(nbytes or 0)
        d["contacts"] += 1
        t.setdefault("contacts", {})[item_id] = {"at": iso(at), "dry_run": bool(dry_run)}
        _write_json(self.traffic_path, t)

    def day_left(self, site: str, at: _dt.datetime) -> Tuple[int, int]:
        d = ((self.traffic().get("sites") or {}).get(site) or {}).get(at.strftime("%Y-%m-%d")) or {}
        c = caps()
        return c["day_requests"] - int(d.get("requests", 0)), c["day_bytes"] - int(d.get("bytes", 0))

    def last_contact(self, item_id: str) -> Optional[_dt.datetime]:
        c = (self.traffic().get("contacts") or {}).get(item_id) or {}
        return parse_iso(c.get("at"))

    def blocked(self) -> Dict[str, str]:
        return self.traffic().get("blocked") or {}

    def block(self, site: str, why: str) -> None:
        t = self.traffic()
        t.setdefault("blocked", {})[site] = why[:200]
        _write_json(self.traffic_path, t)


# ---------------------------------------------------------------------------
# what TOOL can be: a compiled web tool, a saved browser tool, a compiled demonstration tool file
# ---------------------------------------------------------------------------

#: the ``workflow`` of a watched compiled web tool (``compiled_api.json``): its proved request replays, no browser
API_WORKFLOW = "compiled_api"
#: a compiled web tool's run status when the site did not answer (``run_compiled``'s ``transient``)
UNAVAILABLE = "unavailable"
#: a compile record's run state, left out of the pinned copy: it changes with every call, not with the tool
VOLATILE_RECORD_KEYS = ("health", "drift_anchor", "recompile", "empty_page")
COMPILED_API_FILE = "compiled_api.json"
SAVED_WEB_FILE = "compiled_demo.json"
SAVED_WEB_FORMAT = "agent_harness.web_tool/1"
TOOL_HELP = ("TOOL is a name from `anyuse workflow list` (a compiled web tool), or the path to a "
             f"{COMPILED_API_FILE} or {SAVED_WEB_FILE} file.")


def is_api_item(item: Dict[str, Any]) -> bool:
    return item.get("workflow") == API_WORKFLOW


def _demo_tool_ok(tool: Any) -> bool:
    from anyuse.execution import demo_compile as C
    if not isinstance(tool, dict) or not tool.get("search"):
        return False
    return tool.get("workflow") in C.WORKFLOWS or (tool.get("workflow") in BREADTH and C.v10(tool))


def _workflow_store(storage: Any = None) -> Any:
    if storage is not None:
        return storage
    from anyuse.memory.storage import WorkflowStorage
    return WorkflowStorage()


def resolve_tool(arg: Any, storage: Any = None, hosts_path: Optional[str] = None) -> Dict[str, Any]:
    """What ``watch add TOOL`` names, as ``{"kind": "demo", "tool", "name", "source"}`` (a compiled demonstration
    tool, bare or inside a saved web tool's ``compiled_demo.json``) or ``{"kind": "api", "record", "info", "name",
    "source"}`` (a compiled web tool's compile record, its run state left out). A file (or a workflow folder) is read
    first; otherwise ``arg`` is a tool name in the workflow store. Anything else raises ``ValueError`` saying what
    TOOL can be."""
    text = str(arg or "").strip()
    path = Path(text).expanduser() if text else None
    if path is not None and path.is_dir():
        inside = next((path / f for f in (COMPILED_API_FILE, SAVED_WEB_FILE) if (path / f).is_file()), None)
        if inside is None:
            # a folder in the working directory may share a tool's name: the tool wins
            found = _tool_by_name(text, storage, hosts_path)
            if found is not None:
                return found
            raise ValueError(f"{text}: this folder holds no {COMPILED_API_FILE} or {SAVED_WEB_FILE}. {TOOL_HELP}")
        path = inside
    if path is not None and path.is_file():
        return _tool_from_file(path, hosts_path)
    found = _tool_by_name(text, storage, hosts_path) if text else None
    if found is not None:
        return found
    raise ValueError(_unknown_tool(text, storage))


def _tool_from_file(path: Path, hosts_path: Optional[str]) -> Dict[str, Any]:
    source = os.path.abspath(str(path))
    data = _read_json(path, None)
    if isinstance(data, dict) and data.get("format") == SAVED_WEB_FORMAT:
        return _saved_web_tool(data, source)
    if isinstance(data, dict) and "template" in data and "proof" in data:
        folder = os.path.basename(os.path.dirname(source))
        name = str(data.get("workflow") or (folder[len("workflow_"):] if folder.startswith("workflow_") else ""))
        return _api_tool(data, source, name or path.stem, hosts_path)
    if _demo_tool_ok(data):
        return {"kind": "demo", "tool": data, "name": "", "source": source}
    raise ValueError(f"{path}: not a compiled demonstration tool, and not a compiled web tool. {TOOL_HELP}")


def _saved_web_tool(rec: Dict[str, Any], source: str) -> Dict[str, Any]:
    """A saved web tool (``anyuse agent run --compile``): its demonstration tool, only when its proof passed."""
    from anyuse.execution import demo_compile as C
    name = str(rec.get("workflow") or "")
    if rec.get("status") != "ready" or not (rec.get("proof") or {}).get("passed"):
        why = "; ".join(str(r) for r in rec.get("status_reasons") or []) or "its proof did not pass"
        raise ValueError(f"{name or source} is a draft ({why}); only a tool whose proof passed can be watched")
    tool = rec.get("tool")
    if not _demo_tool_ok(tool) or rec.get("tool_sha256") != C.tool_digest(tool):
        raise ValueError(f"{name or source}: the saved tool does not match its digest; compile it again")
    return {"kind": "demo", "tool": tool, "name": name, "source": source}


def _api_tool(record: Dict[str, Any], source: str, name: str, hosts_path: Optional[str]) -> Dict[str, Any]:
    """A compiled web tool: refused when it can never run (a failed or stale proof, a request that is not
    read-only); the pinned copy leaves out the record's run state."""
    from anyuse.execution.api_compile import migrate_compiled_record
    from anyuse.mcpserver import compiled_tools as CT
    record = migrate_compiled_record(json.loads(json.dumps(record)))
    info = CT.describe_compiled(name, record, hosts_path)
    if not info["proof"]["passed"]:
        raise ValueError(f"{name}: its proof did not pass, so it never runs; compile it again")
    if not info["proof"]["current"]:
        raise ValueError(f"{name}: its request changed after the proof; compile it again")
    if info["risk_class"] != CT.RUNNABLE_CLASS:
        raise ValueError(f"{name}: not a read-only request ({info['risk_class']}); watch runs read-only tools only")
    pinned = {k: v for k, v in record.items() if k not in VOLATILE_RECORD_KEYS}
    return {"kind": "api", "record": pinned, "info": info, "name": name, "source": source}


def _compiled_by_name(name: str, storage: Any) -> Optional[Tuple[Dict[str, Any], str]]:
    """The compile record named ``name`` in the workflow store and its path (the listing's own rule)."""
    import glob
    from anyuse.execution.api_compile import migrate_compiled_record
    st = _workflow_store(storage)
    base = str(getattr(st, "base_dir", "") or os.path.dirname(str(st.get_workflow_dir("x"))))
    for path in sorted(glob.glob(os.path.join(base, "workflow_*", COMPILED_API_FILE))):
        record = _read_json(Path(path), None)
        if not isinstance(record, dict):
            continue
        record = migrate_compiled_record(record)
        if (record.get("workflow") or os.path.basename(os.path.dirname(path))[len("workflow_"):]) == name:
            return record, path
    return None


def _tool_by_name(name: str, storage: Any, hosts_path: Optional[str]) -> Optional[Dict[str, Any]]:
    hit = _compiled_by_name(name, storage)
    if hit is not None:
        return _api_tool(hit[0], hit[1], name, hosts_path)
    from anyuse.execution.harness_compile import WEB_DRAFT_FILE, WEB_TOOL_FILE, load_web_tool
    st = _workflow_store(storage)
    rec = load_web_tool(st, name)
    if isinstance(rec, dict) and rec.get("format") == SAVED_WEB_FORMAT:
        wf = st.get(name)
        d = str(st.get_workflow_dir(wf.id))
        f = WEB_TOOL_FILE if os.path.isfile(os.path.join(d, WEB_TOOL_FILE)) else WEB_DRAFT_FILE
        return _saved_web_tool(rec, os.path.join(d, f))
    return None


def known_tool_names(storage: Any = None) -> List[str]:
    """The names ``watch add`` takes: the compiled web tools and the saved browser tools ready to run."""
    names: List[str] = []
    try:
        from anyuse.mcpserver.compiled_tools import list_compiled
        names += [str(t.get("workflow")) for t in list_compiled(_workflow_store(storage)) if t.get("workflow")]
    except Exception:                                  # noqa: BLE001 - an unreadable store lists nothing
        pass
    try:
        from anyuse.execution.web_tool_call import saved_tools
        names += [t["name"] for t in saved_tools(_workflow_store(storage)) if t.get("name")]
    except Exception:                                  # noqa: BLE001
        pass
    return list(dict.fromkeys(names))


def _unknown_tool(text: str, storage: Any) -> str:
    looks_like_file = text.endswith(".json") or os.sep in text or "/" in text
    desktop = False
    if text and not looks_like_file:
        try:
            desktop = _workflow_store(storage).get(text) is not None
        except Exception:                              # noqa: BLE001
            desktop = False
    if looks_like_file:
        head = f"{text}: no such file, and no tool has that name."
    elif desktop:
        head = f"{text} is a recorded desktop workflow; watch takes compiled web tools."
    else:
        head = f"no tool called {text!r}."
    names = known_tool_names(storage)
    have = (f" Your tools: {', '.join(names[:12])}{', ...' if len(names) > 12 else ''}." if names else
            " You have no compiled web tools yet: docs/QUICKSTART_COMPILE.md shows how to make one.")
    return f"{head} {TOOL_HELP}{have}"


def _query_param(record: Dict[str, Any], inputs: List[str]) -> str:
    """The input INPUT binds to: the result contract's query input, else the tool's first input."""
    try:
        from anyuse.execution import result_contract as RC
        from anyuse.execution.api_step import load_command
        qp = str((RC.contract_of(load_command(record.get("template") or {})) or {}).get("query_param") or "")
    except Exception:                                  # noqa: BLE001 - no contract: the first input
        qp = ""
    return qp if qp in inputs else inputs[0]


CHANGELOG_HEAD = ("# Watch changelog\n\nOne entry per change, drift or baseline of a watched item, with the evidence of "
                  "both sides. A DRIFT entry is never a content change: the tool could not read the page as it was "
                  "demonstrated (re-demonstrate); the last good snapshot stays the baseline.\n\n")


# ---------------------------------------------------------------------------
# snapshots, drift and the diff
# ---------------------------------------------------------------------------

def due(store: WatchStore, item: Dict[str, Any], now: _dt.datetime) -> Tuple[bool, str]:
    """Is the item due: its interval (never below ``MIN_INTERVAL_S``) since its last contact (a run or a dry
    run, from the guard's ledger)?"""
    last = store.last_contact(item["id"])
    if last is None:
        return True, "never run"
    need = max(int(item.get("every_s") or 0), MIN_INTERVAL_S)
    gap = (now - last).total_seconds()
    if gap < need:
        return False, f"not due: last contact {iso(last)}, {int((need - gap) // 60)} min to go (interval {need // 60} min)"
    return True, f"due: last contact {iso(last)}"


def snapshot_of(item: Dict[str, Any], tool: Dict[str, Any], rec: Dict[str, Any], at: _dt.datetime) -> Dict[str, Any]:
    """The dated snapshot of one replay: its content (when GOOD) or its drift, with the evidence."""
    if is_api_item(item):
        return api_snapshot_of(item, tool, rec, at)
    st = str(rec.get("status") or "error")
    out = rec.get("output") or {}
    conf = rec.get("conflicts") or {}
    pages = rec.get("pages") or []
    content: Optional[Dict[str, Any]] = None
    if st == "ok" and not conf and item["workflow"] in BREADTH:
        # tool format version 10: a list of N (filtered / paged search) or a table of K items (visited results)
        content = breadth_content(item["workflow"], out)
    elif st == "ok" and not conf:
        content = {"results": out.get("results") or []} if item["workflow"] == "site_search" else \
            {"fields": {k: out.get(k) for k in ("name", "price", "currency", "availability", "url")}}
        if item["workflow"] != "site_search" and out.get("variants_same_title"):
            # a version-7 answer that is the first of several same-title results: the list is content too
            content["variants_same_title"] = variants_of(out["variants_same_title"])
    elif st == "no_results":
        content = {"no_results": True}
    good = content is not None
    last = pages[-1] if pages else {}
    evidence = {"at": iso(at), "search_url": (pages[0] or {}).get("url") if pages else None,
                "url": (out.get("url") if isinstance(out, dict) and out.get("url") else last.get("url")),
                "page_title": last.get("title"), "h1": (last.get("h1") or [])[:2],
                "sources": dict(tool.get("fields") or {}) if item["workflow"] in ("product_lookup", "visit_results") else
                {"results": "the compiled result cards (tool v%s)" % tool.get("version")},
                "picked": rec.get("picked"), "search_via": rec.get("search_via"),
                "tool_sha256": item["tool_sha256"], "model_calls": int(rec.get("model_calls") or 0),
                "requests": rec.get("requests"), "bytes": rec.get("bytes")}
    drift = None
    # needs-review lane (attribute_verify, ANYUSE_ATTRIBUTE_VERIFY on): the page's two sources disagree - NOT a good
    # snapshot (never a content change, never a baseline) and NOT drift (the tool read the page as demonstrated): a
    # question for a person, with both sources' evidence
    review = rec.get("needs_review") if st == "needs_review" and isinstance(rec.get("needs_review"), dict) else None
    if not good and review is None:
        rp = (rec.get("checks") or {}).get("result_page") or {}
        drift = {"status": st, "why": str(rec.get("why") or "")[:300],
                 "conflicts": conf or None, "missing": rec.get("missing"), "fields_seen": rec.get("fields_seen"),
                 # the diagnosis for re-demonstrating: what the tool read, how the wait went, the page's head
                 "diagnosis": {"result_page": {k: rp.get(k) for k in ("url", "status", "n_items", "items", "unread", "why")
                                               if k in rp},
                               "wait": {"results_after_s": rec.get("results_after_s"),
                                        "trail": (rec.get("wait_trail") or [])[-6:]},
                               "pages": [{"url": p.get("url"), "title": p.get("title"),
                                          "text_head": str(p.get("text") or "")[:1500]} for p in pages[-2:]],
                               "infra_retries": rec.get("infra_retries"), "robots": rec.get("robots"),
                               "hosts_sent": rec.get("hosts_sent")}}
    snap = {"item": item["id"], "at": iso(at), "status": "conflict" if conf else st, "good": good,
            "content": content, "drift": drift, "evidence": evidence, "input": item["input"],
            "workflow": item["workflow"]}
    if review is not None:
        snap["review"] = review
    return snap


def api_snapshot_of(item: Dict[str, Any], record: Dict[str, Any], rec: Dict[str, Any],
                    at: _dt.datetime) -> Dict[str, Any]:
    """The dated snapshot of one compiled web tool replay (``api_run_record``): the extracted result as content (when
    GOOD) or its drift, with the request it was read from."""
    st = str(rec.get("status") or "error")
    content: Optional[Dict[str, Any]] = None
    if st == "ok":
        out = rec.get("output")
        content = {"api": out if isinstance(out, dict) else {"result": out}}
    elif st == "no_results":
        content = {"no_results": True}
    good = content is not None
    evidence = {"at": iso(at), "url": rec.get("url"), "search_url": None, "page_title": None,
                "sources": {"request": list(rec.get("urls") or [])}, "served_by": rec.get("served_by"),
                "tool": item.get("tool_name"), "tool_sha256": item["tool_sha256"],
                "model_calls": int(rec.get("model_calls") or 0), "requests": rec.get("requests"),
                "bytes": rec.get("bytes")}
    failure = {"status": st, "why": str(rec.get("why") or "")[:400], "rungs": rec.get("rungs")}
    # the site did not answer: an outage, not drift (nothing says the request changed)
    outage = failure if st == UNAVAILABLE else None
    drift = None if good or outage else failure
    if drift is not None and rec.get("policy_refused"):
        drift["policy_refused"] = True
    snap = {"item": item["id"], "at": iso(at), "status": st, "good": good, "content": content, "drift": drift,
            "evidence": evidence, "input": item["input"], "workflow": item["workflow"]}
    if outage is not None:
        snap["outage"] = outage
    return snap


#: tool format version 10's breadth workflows (``demo_breadth``) and what their snapshots keep
BREADTH = ("filtered_search", "paged_search", "visit_results")
TABLE_KEYS = ("rank", "url", "title", "name", "price", "currency", "availability", "status")


def breadth_content(workflow: str, out: Dict[str, Any]) -> Dict[str, Any]:
    """A GOOD breadth replay's content: a filtered / paged search's list of N (title, URL, the price / date / page the
    run read; the refinement applied; the site's stated total), a visit's table of K rows (each row's fields and
    status)."""
    if workflow == "visit_results":
        return {"table": [{k: r.get(k) for k in TABLE_KEYS} for r in out.get("items") or [] if isinstance(r, dict)]}
    c: Dict[str, Any] = {"results": [{k: r.get(k) for k in ("title", "url", "price", "date", "page") if r.get(k) is not None}
                                     for r in out.get("results") or [] if isinstance(r, dict)]}
    if workflow == "filtered_search":
        c["applied"] = out.get("applied") or []
    if workflow == "paged_search":
        c["total"] = out.get("total")
    return c


def _avail_class(v: Any) -> str:
    from anyuse.execution import demo_compile as C
    return C.availability_class(v) or C.text_availability_class(v)


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def diff(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The deterministic diff of two GOOD snapshots' contents (empty: unchanged)."""
    if not before or not before.get("content") or not after.get("content"):
        return []
    b, a = before["content"], after["content"]
    if bool(b.get("no_results")) != bool(a.get("no_results")):
        ch: List[Dict[str, Any]] = [{"kind": "presence", "before": "no results" if b.get("no_results") else "listed",
                                     "after": "no results" if a.get("no_results") else "listed"}]
        if after.get("workflow") in ("site_search", "filtered_search", "paged_search"):
            ch += _list_diff(b.get("results") or [], a.get("results") or [])
        elif after.get("workflow") == "visit_results":
            ch += _table_diff(b.get("table") or [], a.get("table") or [])
        elif after.get("workflow") == API_WORKFLOW:
            ch += _api_diff(b.get("api") or {}, a.get("api") or {})
        return ch
    if b.get("no_results"):
        return []
    if "api" in a:
        return _api_diff(b.get("api") or {}, a.get("api") or {})
    if "table" in a:
        # tool format version 10: a visit's table of K rows
        return _table_diff(b.get("table") or [], a.get("table") or [])
    if "total" in a or "applied" in a:
        # tool format version 10: a filtered / paged list of N (its stated total, the result prices it read)
        ch = _list_diff(b.get("results") or [], a.get("results") or []) + _shown_diff(b.get("results") or [],
                                                                                 a.get("results") or [])
        if "total" in a and b.get("total") != a.get("total"):
            ch.append({"kind": "total", "before": b.get("total"), "after": a.get("total")})
        return ch
    if "fields" in a:
        return _fields_diff(b.get("fields") or {}, a.get("fields") or {}) + \
            _variants_diff(b.get("variants_same_title"), a.get("variants_same_title"))
    return _list_diff(b.get("results") or [], a.get("results") or [])


def variants_of(v: Any) -> Dict[str, Any]:
    """What a snapshot keeps of a version-7 answer's ``variants_same_title`` (the answer is the first of several
    results with one normalized title; the others as the result list showed them - never opened)."""
    v = v if isinstance(v, dict) else {}
    return {"count": int(v.get("count") or 0), "title": v.get("title") or "",
            "variants": [{k: x.get(k) for k in ("title", "url", "price", "price_value", "answered")}
                         for x in v.get("variants") or [] if isinstance(x, dict)]}


def _variants_diff(b: Optional[Dict[str, Any]], a: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same-title variants listed with an answer, keyed by URL: a variant added or
    removed, retitled, or its shown price changed (a variant's price change is a change, as the answer's is);
    nothing when neither side lists variants."""
    vb = [x for x in (b or {}).get("variants") or []]
    va = [x for x in (a or {}).get("variants") or []]
    if not vb and not va:
        return []
    kb = {_key(x): x for x in reversed(vb)}                # the first of a key wins; the site's order is kept below
    ka = {_key(x): x for x in reversed(va)}
    out: List[Dict[str, Any]] = []
    for x in vb:
        if _key(x) not in ka and kb.get(_key(x)) is x:
            out.append({"kind": "variant_removed", "title": x.get("title"), "url": x.get("url"), "price": x.get("price")})
    for x in va:
        if _key(x) not in kb and ka.get(_key(x)) is x:
            out.append({"kind": "variant_added", "title": x.get("title"), "url": x.get("url"), "price": x.get("price")})
    for x in va:
        k = _key(x)
        if k not in kb or ka.get(k) is not x:
            continue
        y = kb[k]
        pb, pa = _num(y.get("price_value")), _num(x.get("price_value"))
        moved = (pb is None) != (pa is None) or (pb is not None and abs(pb - pa) > 0.005) or \
            (pb is None and pa is None and fold(y.get("price")) != fold(x.get("price")))
        if moved:
            c = {"kind": "variant_price", "title": x.get("title"), "url": x.get("url"), "before": y.get("price"),
                 "after": x.get("price"), "before_value": pb, "after_value": pa}
            if pb and pa is not None:
                c["delta"] = round(pa - pb, 2)
                c["pct"] = round(100.0 * (pa - pb) / pb, 1)
            out.append(c)
        if fold(y.get("title")) != fold(x.get("title")):
            out.append({"kind": "variant_retitled", "before": y.get("title"), "after": x.get("title"), "url": x.get("url")})
    return out


def _fields_diff(b: Dict[str, Any], a: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if fold(b.get("name")) != fold(a.get("name")):
        out.append({"kind": "field", "field": "name", "before": b.get("name"), "after": a.get("name")})
    pb, pa = _num(b.get("price")), _num(a.get("price"))
    if (pb is None) != (pa is None) or (pb is not None and abs(pb - pa) > 0.005) or \
            str(b.get("currency") or "") != str(a.get("currency") or ""):
        c = {"kind": "price", "before": pb, "after": pa, "currency_before": b.get("currency"),
             "currency_after": a.get("currency")}
        if pb and pa is not None and b.get("currency") == a.get("currency"):
            c["delta"] = round(pa - pb, 2)
            c["pct"] = round(100.0 * (pa - pb) / pb, 1)
        out.append(c)
    ab, aa = b.get("availability"), a.get("availability")
    cb, ca = _avail_class(ab), _avail_class(aa)
    if (cb or ca) and cb != ca or (not cb and not ca and fold(ab) != fold(aa)):
        out.append({"kind": "availability", "before": ab, "after": aa, "before_class": cb, "after_class": ca})
    if norm_url(b.get("url")) != norm_url(a.get("url")):
        out.append({"kind": "field", "field": "url", "before": b.get("url"), "after": a.get("url")})
    return out


def _key(it: Dict[str, Any]) -> str:
    return norm_url(it.get("url")) if it.get("url") else "title:" + fold(it.get("title"))


def _list_diff(b: Sequence[Dict[str, Any]], a: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kb = {_key(x): (i, x) for i, x in reversed(list(enumerate(b)))}
    ka = {_key(x): (i, x) for i, x in reversed(list(enumerate(a)))}
    out: List[Dict[str, Any]] = []
    for k, (i, x) in sorted(kb.items(), key=lambda t: t[1][0]):
        if k not in ka:
            out.append({"kind": "removed", "rank": i + 1, "title": x.get("title"), "url": x.get("url")})
    for k, (i, x) in sorted(ka.items(), key=lambda t: t[1][0]):
        if k not in kb:
            out.append({"kind": "added", "rank": i + 1, "title": x.get("title"), "url": x.get("url")})
    for k, (i, x) in sorted(ka.items(), key=lambda t: t[1][0]):
        if k in kb:
            j, y = kb[k]
            if fold(y.get("title")) != fold(x.get("title")):
                out.append({"kind": "retitled", "rank": i + 1, "before": y.get("title"), "after": x.get("title"),
                            "url": x.get("url")})
            if i != j:
                out.append({"kind": "moved", "before_rank": j + 1, "after_rank": i + 1, "title": x.get("title"),
                            "url": x.get("url")})
    return out


def _shown_diff(b: Sequence[Dict[str, Any]], a: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tool format version 10: the price a listed result SHOWS changed (a filtered / sorted list keeps the card
    prices it read)."""
    kb = {_key(x): x for x in reversed(list(b))}
    out: List[Dict[str, Any]] = []
    for i, x in enumerate(a):
        y = kb.get(_key(x))
        if y is None:
            continue
        pb, pa = _num(y.get("price")), _num(x.get("price"))
        if (pb is None) != (pa is None) or (pb is not None and abs(pb - pa) > 0.005):
            out.append({"kind": "shown_price", "rank": i + 1, "title": x.get("title"), "url": x.get("url"),
                        "before": pb, "after": pa})
    return out


def _table_diff(b: Sequence[Dict[str, Any]], a: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tool format version 10: a visit's table - rows added, removed or moved (keyed by URL; ``_list_diff`` on the
    rows' names), and per row present on both sides its field changes (``_fields_diff``: name, price, availability,
    URL) and a status change (a row ok before, a conflict now)."""
    def as_item(r: Dict[str, Any]) -> Dict[str, Any]:
        return {"title": r.get("name") or r.get("title"), "url": r.get("url")}
    out = [dict(c, kind="row_" + c["kind"]) for c in _list_diff([as_item(r) for r in b], [as_item(r) for r in a])
           if c["kind"] in ("added", "removed", "moved")]
    kb = {_key(x): x for x in reversed(list(b))}
    for i, x in enumerate(a):
        y = kb.get(_key(x))
        if y is None:
            continue
        if str(y.get("status") or "") != str(x.get("status") or ""):
            out.append({"kind": "row_status", "rank": i + 1, "url": x.get("url"), "before": y.get("status"),
                        "after": x.get("status")})
        for c in _fields_diff(y, x):
            if c.get("field") == "url":
                continue
            out.append({"kind": "row_field", "rank": i + 1, "url": x.get("url"), "name": x.get("name"), "change": c})
    return out


#: the keys that name one row of a compiled web tool's list, in order (the URL keys it when present)
ROW_TITLE_KEYS = ("title", "name", "label", "id")
ROW_URL_KEYS = ("url", "link", "href")


def _api_rows(v: Any) -> List[Dict[str, Any]]:
    """A compiled web tool's list as ``_list_diff`` rows: a value is its own title; a record is titled by its first
    naming key (else its whole JSON) and keyed by its URL when it has one (``keys``: the two it used)."""
    out: List[Dict[str, Any]] = []
    for x in v if isinstance(v, list) else ([] if v is None else [v]):
        if isinstance(x, dict):
            tk = next((k for k in ROW_TITLE_KEYS if x.get(k) not in (None, "")), None)
            uk = next((k for k in ROW_URL_KEYS if isinstance(x.get(k), str) and x.get(k)), None)
            title = str(x[tk]) if tk else json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)[:200]
            out.append({"title": title, "url": x[uk] if uk else None, "row": x, "keys": (tk, uk)})
        else:
            out.append({"title": "" if x is None else str(x)})
    return out


def _api_diff(b: Dict[str, Any], a: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The diff of two compiled web tool results, per extracted name: a list's items added, removed, retitled or
    moved (and a record's own fields changed), a value changed."""
    out: List[Dict[str, Any]] = []
    for name in sorted(set(b) | set(a)):
        vb, va = b.get(name), a.get(name)
        if isinstance(vb, list) or isinstance(va, list):
            rb, ra = _api_rows(vb), _api_rows(va)
            out += [dict(c, list=name) for c in _list_diff(rb, ra)]
            kb = {_key(x): x for x in reversed(rb)}
            for i, x in enumerate(ra):
                y = kb.get(_key(x))
                if y is None or not isinstance(x.get("row"), dict) or not isinstance(y.get("row"), dict):
                    continue
                for f in sorted(set(y["row"]) | set(x["row"])):
                    if f in x["keys"] or _same(y["row"].get(f), x["row"].get(f)):
                        continue
                    out.append({"kind": "item_field", "list": name, "rank": i + 1, "title": x.get("title"),
                                "url": x.get("url"), "field": f, "before": y["row"].get(f), "after": x["row"].get(f)})
        elif not _same(vb, va):
            out.append({"kind": "field", "field": name, "before": vb, "after": va})
    return out


def _same(x: Any, y: Any) -> bool:
    if isinstance(x, (dict, list)) or isinstance(y, (dict, list)):
        return json.dumps(x, sort_keys=True, default=str) == json.dumps(y, sort_keys=True, default=str)
    nx, ny = _num(x), _num(y)
    if nx is not None and ny is not None:
        return abs(nx - ny) <= 0.005
    return fold(x) == fold(y) and (x is None) == (y is None)


def change_line(c: Dict[str, Any]) -> str:
    """One changelog line for one change (a compiled web tool's change names its list first)."""
    line = _change_line(c)
    return f"{c['list']}: {line}" if c.get("list") else line


def _change_line(c: Dict[str, Any]) -> str:
    k = c.get("kind")
    if k == "item_field":
        return f"#{c.get('rank')} {str(c.get('title'))[:60]}: {c.get('field')} {str(c.get('before'))[:80]} -> " \
               f"{str(c.get('after'))[:80]}"
    if k == "price":
        cur = c.get("currency_after") or ""
        tail = f" ({c['delta']:+.2f}, {c['pct']:+.1f} %)" if "delta" in c else ""
        return f"price {c.get('before')} {c.get('currency_before') or ''} -> {c.get('after')} {cur}".replace("  ", " ") + tail
    if k == "availability":
        return f"availability {c.get('before')!s} ({c.get('before_class') or '?'}) -> {c.get('after')!s} " \
               f"({c.get('after_class') or '?'})"
    if k == "field":
        return f"{c.get('field')} {str(c.get('before'))[:80]!r} -> {str(c.get('after'))[:80]!r}"
    if k == "presence":
        return f"{c.get('before')} -> {c.get('after')}"
    if k in ("added", "removed"):
        return f"{k} #{c.get('rank')}: {str(c.get('title'))[:80]} {c.get('url') or '(no link)'}"
    if k == "retitled":
        return f"retitled #{c.get('rank')}: {str(c.get('before'))[:60]!r} -> {str(c.get('after'))[:60]!r}"
    if k == "moved":
        return f"moved #{c.get('before_rank')} -> #{c.get('after_rank')}: {str(c.get('title'))[:80]}"
    if k in ("variant_added", "variant_removed"):
        return f"variant {k.split('_')[1]}: {str(c.get('title'))[:80]} {c.get('price') or '(no price shown)'} {c.get('url')}"
    if k == "variant_price":
        tail = f" ({c['delta']:+.2f}, {c['pct']:+.1f} %)" if "delta" in c else ""
        return f"variant price {c.get('before') or '(none)'} -> {c.get('after') or '(none)'}{tail}: " \
               f"{str(c.get('title'))[:60]} {c.get('url')}"
    if k in ("row_added", "row_removed"):
        return f"row {k.split('_')[1]} #{c.get('rank')}: {str(c.get('title'))[:80]} {c.get('url') or ''}"
    if k == "row_moved":
        return f"row moved #{c.get('before_rank')} -> #{c.get('after_rank')}: {str(c.get('title'))[:80]}"
    if k == "row_status":
        return f"row #{c.get('rank')} {c.get('url')}: status {c.get('before')} -> {c.get('after')}"
    if k == "row_field":
        return f"row #{c.get('rank')} {str(c.get('name'))[:60]}: " + change_line(c.get("change") or {})
    if k == "shown_price":
        return f"shown price #{c.get('rank')} {c.get('before')} -> {c.get('after')}: {str(c.get('title'))[:60]}"
    if k == "total":
        return f"stated total {c.get('before')} -> {c.get('after')}"
    if k == "variant_retitled":
        return f"variant retitled: {str(c.get('before'))[:60]!r} -> {str(c.get('after'))[:60]!r} {c.get('url')}"
    return json.dumps(c, ensure_ascii=False)[:160]


def _side(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not snap:
        return None
    ev = snap.get("evidence") or {}
    return {"snapshot": snap.get("at"), "status": snap.get("status"), "url": ev.get("url"),
            "search_url": ev.get("search_url"), "sources": ev.get("sources"), "page_title": ev.get("page_title")}


def entry_of(item: Dict[str, Any], base: Optional[Dict[str, Any]], snap: Dict[str, Any]) -> Dict[str, Any]:
    """The changelog entry of one replay: ``baseline`` (the first good snapshot), ``change`` (a non-empty
    diff), ``unchanged`` (an empty one; runs log only), ``drift`` (never a content change) or ``outage`` (the
    site did not answer: neither a content change nor drift)."""
    e = {"at": snap["at"], "item": item["id"], "site": item["site"], "input": item["input"],
         "workflow": item["workflow"], "tool_sha256": item["tool_sha256"]}
    if is_api_item(item):
        e["tool_name"] = item.get("tool_name")
    if snap.get("review"):
        return dict(e, kind="needs_review", changes=[], action="review", review=snap["review"],
                    before=_side(base), after=_side(snap))
    if snap.get("outage"):
        return dict(e, kind="outage", changes=[], action="", outage=snap["outage"],
                    before=_side(base), after=_side(snap))
    if not snap["good"]:
        return dict(e, kind="drift", changes=[], action=redo_of(dict(e, drift=snap["drift"])), drift=snap["drift"],
                    before=_side(base), after=_side(snap))
    if base is None:
        return dict(e, kind="baseline", changes=[], before=None, after=_side(snap), content=snap["content"])
    ch = diff(base, snap)
    return dict(e, kind="change" if ch else "unchanged", changes=ch, before=_side(base), after=_side(snap))


def redo_of(e: Dict[str, Any]) -> str:
    """What a drifted entry asks for: a browser tool is demonstrated again; a compiled web tool whose request failed
    is recompiled; a refused, blocked, rate-limited or crashed one names its reason instead ("")."""
    if e.get("workflow") != API_WORKFLOW:
        return "re-demonstrate"
    return "recompile" if (e.get("drift") or {}).get("status") == "failed" else ""


def _api_drift_lines(e: Dict[str, Any]) -> List[str]:
    import shlex
    d = e.get("drift") or {}
    if d.get("policy_refused"):
        return [f"**REFUSED (nothing sent)** - {str(d.get('why'))[:300]}",
                "Not a content change and not a changed site: the pre-send policy refused this destination before "
                "anything was sent; the last good snapshot stays the baseline.",
                "Next: the reason above names the setting or the address that stopped the request. Nothing needs "
                "recompiling."]
    if redo_of(e):
        name = str(e.get("tool_name") or "TOOL")
        return [f"**DRIFT (recompile)** - {d.get('status')}: {str(d.get('why'))[:300]}",
                "Not a content change: the compiled request did not answer as it did when it was proved; the last "
                "good snapshot stays the baseline.",
                f"Next: if the site changed, record the search again and recompile {name}, then watch the new "
                f"version: `anyuse watch remove {e['item']}` and `anyuse watch add {name} {shlex.quote(e['input'])}`."]
    return [f"**DRIFT** - {d.get('status')}: {str(d.get('why'))[:300]}",
            "Not a content change: nothing was read; the last good snapshot stays the baseline."]


def _outage_lines(e: Dict[str, Any]) -> List[str]:
    o = e.get("outage") or {}
    return [f"**SITE DID NOT ANSWER** - {str(o.get('why'))[:300]}",
            "Not a content change and not drift: a refused connection, a timeout or a server error says nothing "
            "about the request, so nothing needs recompiling. The last good snapshot stays the baseline.",
            "Next: the next `anyuse watch run` tries again. If the site still does not answer, check that it is up."]


def render_entry(e: Dict[str, Any]) -> str:
    """The Markdown of one changelog entry."""
    api = e.get("workflow") == API_WORKFLOW
    what = f"{e.get('tool_name')} (compiled web tool)" if api else e['workflow']
    head = f"## {e['at']} - {e['item']}\n\n{e['site']}, {what}, input {e['input']!r} (tool {e['tool_sha256'][:12]})\n\n"
    L: List[str] = []
    if e["kind"] == "drift" and (e.get("drift") or {}).get("status") == "known_wrong":
        d = e.get("drift") or {}
        L.append(f"**KNOWN WRONG (not accepted; re-demonstrate)** - {str(d.get('why'))[:240]}")
        L.append("The answer matches one judged wrong before: not a content change, not a baseline.")
    elif e["kind"] == "drift" and api:
        L += _api_drift_lines(e)
    elif e["kind"] == "outage":
        L += _outage_lines(e)
    elif e["kind"] == "drift":
        d = e.get("drift") or {}
        L.append(f"**DRIFT (re-demonstrate)** - {d.get('status')}: {str(d.get('why'))[:240]}")
        L.append("Not a content change: the tool could not read the page as it was demonstrated; the last good "
                 "snapshot stays the baseline.")
    elif e["kind"] == "needs_review":
        from anyuse.execution import attribute_verify as AV
        rl = AV.review_lines({"status": AV.NEEDS_REVIEW, "needs_review": e.get("review")})
        L.append(f"**{rl[0]}**" if rl else "**NEEDS REVIEW** - not accepted")
        L += [f"- {x}" for x in rl[1:]]
        L.append("Not a content change and not drift: the last good snapshot stays the baseline.")
    elif e["kind"] == "baseline":
        L.append("**baseline** - the first good snapshot of this item")
    elif e["kind"] == "change":
        L.append(f"**change** ({len(e['changes'])})")
        L += [f"- {change_line(c)}" for c in e["changes"]]
    else:
        L.append("unchanged")
    for side in ("before", "after"):
        s = e.get(side)
        if s:
            L.append(f"- {side}: {s.get('snapshot')} {s.get('status')} {s.get('url') or ''}"
                     + (f" (sources {s.get('sources')})" if s.get("sources") else ""))
    if e.get("explain"):
        x = e["explain"]
        L.append(f"- explain ({x.get('backend')}, {x.get('model') or ''}): {x.get('text') or x.get('error')}")
    return head + "\n".join(L) + "\n\n"


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

Replay = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, int]], Dict[str, Any]]


def make_api_replay(run: Optional[Callable[..., Dict[str, Any]]] = None, browser: str = "", storage: Any = None,
                    **run_kwargs: Any) -> Replay:
    """``replay(item, record, caps) -> run record`` for a watched compiled web tool: its PINNED compile record runs
    through ``compiled_tools.run_compiled`` with the checks every call gets (a passed and current proof, an
    acknowledged host, a read-only bound request), without the fall-back (0 model calls) and without writing
    anything back to the tool. A tool with no compiled request is served by its browser replay on ``browser``.
    ``run_kwargs`` reach ``run_compiled`` (tests pass a fake executor and host file)."""
    def replay(item: Dict[str, Any], record: Dict[str, Any], caps: Dict[str, int]) -> Dict[str, Any]:
        fn = run
        if fn is None:
            from anyuse.mcpserver.compiled_tools import run_compiled as fn
        params = dict(item.get("params") or {})
        params[str(item.get("param") or "q")] = str(item.get("input") or "")
        kw = dict(run_kwargs)
        if storage is not None:
            kw.setdefault("storage", storage)
        if browser and item.get("needs_browser") and "browser_executor" not in kw:
            from anyuse.execution.cdp_replay import ReplayExecutor
            kw["browser_executor"] = ReplayExecutor(endpoint=browser)
        try:
            out = fn(str(item.get("tool_name") or ""), params, record=record, cascade=False, record_outcome=False,
                     post_card=False, **kw)
        except Exception as exc:                       # noqa: BLE001 - a replay failure is a record (drift)
            return dict(api_run_record({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}, record, params),
                        status="error")
        rec = api_run_record(out, record, params)
        if rec["status"] == "failed":
            live = _live_proof_id(str(item.get("tool_name") or ""), storage)
            if live and item.get("proof_id") and live != item["proof_id"]:
                rec["why"] = (f"{rec['why']}; {item.get('tool_name')} was recompiled since this item was added: "
                              f"watch it again to use the new version")[:400]
        return rec
    return replay


def api_run_record(out: Dict[str, Any], record: Optional[Dict[str, Any]] = None,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``run_compiled``'s answer as a watch run record: ``ok`` / ``no_results`` with the extracted result (``no_results``
    also when a chain's search listed no result with the asked-for name), ``blocked`` or ``rate_limited`` (the site
    asked for a person, or to wait), ``refused`` (stopped before any request: the proof, the host acknowledgement, the
    read-only check, or the pre-send policy, then with ``policy_refused``), ``unavailable`` (the site did not answer:
    a refused connection, a timeout, a server error) or ``failed`` (the site answered, but not as the proof did).
    ``requests`` counts what was sent: the compiled request (each step of a chain) and the browser replay's page."""
    from anyuse.execution.blocked import RATE_LIMITED
    tried = [t for t in out.get("rungs_tried") or [] if isinstance(t, dict)]
    sent = [t for t in tried if not t.get("skipped") and t.get("rung") in ("api", "browser_replay")]
    urls = [str(u) for u in (out.get("provenance") or {}).get("urls") or []]
    requests = sum((len(urls) or 1) if t.get("rung") == "api" else 1 for t in sent)
    rec: Dict[str, Any] = {"model_calls": int(out.get("model_calls") or 0), "requests": requests,
                           "bytes": int(out.get("bytes") or 0), "served_by": out.get("served_by"), "urls": urls,
                           "url": out.get("url") or _bound_url(record, params),
                           "rungs": [{k: t.get(k) for k in ("rung", "ok", "skipped", "why") if k in t} for t in tried]}
    if out.get("ok"):
        result = out.get("result")
        status = "no_results" if out.get("empty_state") else "ok"
        return dict(rec, status=status, output=result if isinstance(result, dict) else {"result": result},
                    why=f"served by {str(out.get('served_by') or 'api').replace('_', ' ')}")
    if out.get("not_found"):
        # the search answered: the watched name is not listed, which is a presence change, not a broken request
        return dict(rec, status="no_results", output={}, why=str(out.get("error") or "")[:400])
    blocked = out.get("blocked") if isinstance(out.get("blocked"), dict) else None
    if blocked:
        status = "rate_limited" if blocked.get("reason") == RATE_LIMITED else "blocked"
    elif out.get("policy_refused"):
        # the pre-send policy stopped the request before it was sent: the request it refused is not counted
        return dict(rec, status="refused", policy_refused=True, requests=max(0, requests - 1),
                    why=str(out.get("error") or "the pre-send policy refused the request")[:400])
    elif out.get("transient"):
        status = UNAVAILABLE
    else:
        status = "failed" if sent else "refused"
    return dict(rec, status=status, why=str(out.get("error") or "the compiled request failed")[:400])


def _bound_url(record: Optional[Dict[str, Any]], params: Optional[Dict[str, Any]]) -> Optional[str]:
    """The URL a single compiled request was sent to (a chain has several: its template URLs are the sources)."""
    try:
        from anyuse.execution.api_step import load_command
        t = load_command((record or {}).get("template") or {})
        if getattr(t, "steps", None):
            return None
        return t.render(dict(params or {}))[0]
    except Exception:                                  # noqa: BLE001 - evidence only
        return None


def _live_proof_id(name: str, storage: Any = None) -> str:
    """The proof id of the tool now stored under ``name`` ("" when there is none)."""
    try:
        from anyuse.mcpserver.compiled_tools import proof_id
        hit = _compiled_by_name(name, storage)
        return proof_id(hit[0]) if hit else ""
    except Exception:                                  # noqa: BLE001
        return ""


def run_watch(store: WatchStore, replay: Replay, now: Optional[_dt.datetime] = None, dry_run: bool = False,
              explainer: Any = None, only: Iterable[str] = (), log: Callable[[str], None] = lambda s: None,
              verdicts: Any = None) -> Dict[str, Any]:
    """One ``watch run``: every due item replayed (``replay(item, tool, caps) -> run record``: the tool's
    ``demo_tool.run_tool`` output plus the guard's ``requests``, ``bytes``, ``violations``), snapshotted,
    diffed against its last good snapshot and logged; ``dry_run`` writes nothing to the store (the guard's
    traffic ledger still books every contact). Returns the run report (with its model calls).

    ``verdicts`` (a ``verdict_store.VerdictStore``; the CLI passes the web tools' store, the one the MCP route checks):
    a replay whose answer matches an answer the store holds as WRONG is a KNOWN WRONG drift - never a content change,
    never a baseline, shown as such in the changelog and the hook line (never silent)."""
    now = now or utcnow()
    report: Dict[str, Any] = {"at": iso(now), "dry_run": dry_run, "items": [], "model_calls": {"replay": 0},
                              "sites": {}, "hard_stop": None, "caps": caps()}
    if store.hard_stop_path.exists():
        report["hard_stop"] = (_read_json(store.hard_stop_path, {}) or {}).get("why") or "hard stop"
        log(f"refused: a traffic violation stopped watch ({store.hard_stop_path})")
        return report
    items = store.items()
    only = set(only or ())
    entries: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
    for iid in sorted(items):
        item = items[iid]
        if only and iid not in only:
            continue
        row: Dict[str, Any] = {"item": iid, "site": item["site"]}
        if item["site"] in store.blocked():
            report["items"].append(dict(row, skipped=f"the site blocked us: {store.blocked()[item['site']]}"))
            continue
        ok, why = due(store, item, now)
        if not ok:
            report["items"].append(dict(row, skipped=why))
            log(f"{iid}: {why}")
            continue
        req_left, bytes_left = store.day_left(item["site"], now)
        c = caps()
        cap = {"requests": min(c["run_requests"], req_left), "bytes": min(c["run_bytes"], bytes_left)}
        if cap["requests"] <= 0 or cap["bytes"] <= 0:
            report["items"].append(dict(row, skipped=f"the site's daily cap is used ({c['day_requests']} requests / "
                                                     f"{c['day_bytes'] // (1024 * 1024)} MB)"))
            continue
        try:
            tool = store.tool_of(item)
        except ValueError as exc:
            report["items"].append(dict(row, skipped=str(exc)))
            continue
        # a watch run asks no model: the in-run verifier gets the rules alone, and what only its model could
        # settle is withheld (drift, never a change)
        from anyuse.execution import attribute_verify as AV
        with AV.no_model("a watch run asks no model"):
            rec = replay(item, tool, cap)
        if verdicts is not None:
            from anyuse.execution.web_tool_call import known_wrong_check
            rec = known_wrong_check(verdicts, tool, str(item.get("input") or ""), rec)
        store.book(item["site"], int(rec.get("requests") or 0), int(rec.get("bytes") or 0), iid, now, dry_run)
        report["model_calls"]["replay"] += int(rec.get("model_calls") or 0)
        s = report["sites"].setdefault(item["site"], {"requests": 0, "bytes": 0})
        s["requests"] += int(rec.get("requests") or 0)
        s["bytes"] += int(rec.get("bytes") or 0)
        if rec.get("violations"):
            _write_json(store.hard_stop_path, {"why": "a traffic violation", "at": iso(now), "item": iid,
                                               "violations": rec.get("violations")})
            report["hard_stop"] = "a traffic violation"
        if rec.get("status") == "blocked":
            store.block(item["site"], str(rec.get("why") or "blocked"))
        snap = snapshot_of(item, tool, rec, now)
        base = store.snapshot(iid, item.get("last_good"))
        e = entry_of(item, base, snap)
        entries.append((item, snap, e))
        report["items"].append(dict(row, status=snap["status"], kind=e["kind"], changes=e["changes"],
                                    drift=(snap["drift"] or {}).get("why"), requests=rec.get("requests"),
                                    **({"needs_review": snap["review"]} if snap.get("review") else {}),
                                    **({"outage": snap["outage"].get("why")} if snap.get("outage") else {})))
        failed = snap["drift"] or snap.get("outage")
        log(f"{iid}: {e['kind']}" + (f" ({len(e['changes'])} changes)" if e["changes"] else "") +
            (f": {failed['status']}: {str(failed['why'])[:120]}" if failed else ""))
        if report["hard_stop"]:
            break
    # --explain: a model call only for a non-empty diff
    if explainer is not None:
        calls = {"backend": getattr(explainer, "backend", "?"), "model": getattr(explainer, "model", ""),
                 "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "errors": []}
        for _item, _snap, e in entries:
            if e["kind"] == "change" and e["changes"]:
                x = explainer.explain(e)
                e["explain"] = {k: x.get(k) for k in ("backend", "model", "text", "error", "calls")}
                if "important" in x:                   # OpenJev's fixed "nothing important" option was answered
                    e["explain"]["important"] = x["important"]
                calls["calls"] += int(x.get("calls") or 0)
                calls["prompt_tokens"] += int(x.get("prompt_tokens") or 0)
                calls["completion_tokens"] += int(x.get("completion_tokens") or 0)
                calls["cost_usd"] = round(calls["cost_usd"] + float(x.get("cost_usd") or 0.0), 6)
                if x.get("error"):
                    calls["errors"].append(str(x["error"])[:160])
        report["model_calls"]["explain"] = calls
    report["entries"] = [e for _i, _s, e in entries]
    if dry_run:
        return report
    items = store.items()
    for item, snap, e in entries:
        it = items.get(item["id"])
        if it is None:
            continue
        name = store.write_snapshot(snap)
        it["last_run_at"] = snap["at"]
        it["last_status"] = snap["status"]
        if snap["good"]:
            it["last_good"] = name
            it["drift_since"] = None
        elif not snap.get("review") and not snap.get("outage") and not it.get("drift_since"):
            it["drift_since"] = snap["at"]
        if e["kind"] != "unchanged":
            store.append_changelog(e)
    store.save_items(items)
    store.append_run({k: v for k, v in report.items() if k != "entries"})
    return report


# ---------------------------------------------------------------------------
# --explain: OpenJev (self-hostable) or Cerebras (the model adapter); never the Jev API, never OpenRouter
# ---------------------------------------------------------------------------

#: hosts an explainer never sends to (the hosted Jev API lives on OpenRouter)
FORBIDDEN_MODEL_HOSTS = ("openrouter.ai",)
EXPLAIN_MAX_TOKENS = 300


def _host(url: str) -> str:
    import urllib.parse
    try:
        return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _forbidden(url: str) -> bool:
    h = _host(url)
    return any(h == f or h.endswith("." + f) for f in FORBIDDEN_MODEL_HOSTS)


def explain_prompt(e: Dict[str, Any]) -> str:
    """What the model sees: the item and the deterministic diff lines only (never page text)."""
    lines = "\n".join(f"- {change_line(c)}" for c in e.get("changes") or [])
    kind = {"product_lookup": "product", "filtered_search": "filtered / sorted search", "paged_search": "paged search",
            "visit_results": "table of visited results", API_WORKFLOW: "compiled web tool"}.get(e["workflow"],
                                                                                              "site search")
    return (f"A watched {kind} on {e['site']} "
            f"(input: {e['input']!r}) changed between {((e.get('before') or {}).get('snapshot'))} and "
            f"{((e.get('after') or {}).get('snapshot'))}. The deterministic diff:\n{lines}\n\n"
            f"In one or two plain sentences, say what changed and why it may matter. Use only the lines above.")


class CerebrasExplainer:
    """One short summary per non-empty diff, through the model adapter's Cerebras preset (never OpenRouter).
    ``adapter`` is injectable (tests); the run budget caps the spend."""
    backend = "cerebras"

    def __init__(self, adapter: Any = None, budget_usd: float = 0.10):
        if adapter is None:
            from anyuse.execution import model_adapter as M
            p = M.PROVIDER_PRESETS["cerebras"]
            price_in, price_out = M._ROLE_PRICES["agent"]
            cfg = M.ModelConfig(provider="cerebras", base_url=p.base_url, model=p.model, api_key_env=p.api_key_env,
                                timeout=60.0, max_tokens=EXPLAIN_MAX_TOKENS, retries=1, price_in=price_in,
                                price_out=price_out)
            adapter = M.ModelAdapter(cfg, budget=M.RunBudget(budget_usd), feature="watch_explain")
        cfg = getattr(adapter, "config", None)
        if cfg is not None and (_forbidden(cfg.base_url) or getattr(cfg, "provider", "") != "cerebras"):
            raise ValueError("--explain cerebras: the adapter is not Cerebras (never OpenRouter)")
        self.adapter = adapter
        self.model = getattr(cfg, "model", "")

    def explain(self, e: Dict[str, Any]) -> Dict[str, Any]:
        r = self.adapter.complete([{"role": "system", "content": "You summarize a website change for its owner. "
                                                                  "Be factual and brief."},
                                   {"role": "user", "content": explain_prompt(e)}], temperature=0.0,
                                  max_tokens=EXPLAIN_MAX_TOKENS)
        u = getattr(r, "usage", None)
        cost = None
        if u is not None:
            cost = (u.prompt_tokens * getattr(self.adapter, "price_in", 0.0) +
                    u.completion_tokens * getattr(self.adapter, "price_out", 0.0)) / 1_000_000
        return {"backend": self.backend, "model": self.model, "text": (r.text or "").strip()[:600] if r.ok else "",
                "error": "" if r.ok else r.error, "calls": int(getattr(r, "llm_calls", 0) or 0),
                "prompt_tokens": getattr(u, "prompt_tokens", 0), "completion_tokens": getattr(u, "completion_tokens", 0),
                "cost_usd": round(cost or 0.0, 6)}


#: the FIXED last option of the OpenJev explain question: a diff whose lines are all cosmetic
#: (two results swapped, a URL detail) can be answered as unimportant instead of a forced pick of one line
NOTHING_IMPORTANT = "nothing important changed (only cosmetic: an order, a format or a URL detail)"
EXPLAIN_QUESTION = ("Which line is the most important change for someone watching this item? If no line matters "
                    "to them (only cosmetic changes), choose the last option.")


class OpenJevExplainer:
    """The self-hostable OpenJev server (``ANYUSE_OPENJEV_URL``): a typed CHOICE of the most important line of
    the diff (OpenJev answers typed questions, not free text), with a FIXED last option ``NOTHING_IMPORTANT``:
    a cosmetic diff (a reorder only) can be answered as unimportant (``important: False``). One line needs no
    call."""
    backend = "openjev"

    def __init__(self, client: Any = None):
        if client is None:
            from anyuse.execution import openjev
            client = openjev.Client()
        if _forbidden(getattr(client, "base", "")):
            raise ValueError("--explain openjev: the URL is the hosted Jev API / OpenRouter (never used)")
        if not getattr(client, "available", False):
            raise ValueError("--explain openjev: ANYUSE_OPENJEV_URL is not set (a self-hosted OpenJev server)")
        self.client = client
        self.model = getattr(client, "model", "")

    def explain(self, e: Dict[str, Any]) -> Dict[str, Any]:
        lines = [change_line(c) for c in e.get("changes") or []]
        if len(lines) == 1:
            return {"backend": self.backend, "model": self.model, "text": f"the change: {lines[0]}", "calls": 0}
        idx, conf, a = self.client.choose(explain_prompt(e), EXPLAIN_QUESTION, lines + [NOTHING_IMPORTANT])
        if idx is None:
            return {"backend": self.backend, "model": self.model, "text": "", "error": a.error or "no opinion",
                    "calls": 0 if "unavailable" in (a.error or "") else 1}
        if idx == len(lines):
            return {"backend": self.backend, "model": self.model, "calls": 1, "prompt_tokens": a.input_tokens or 0,
                    "important": False, "text": f"nothing important ({conf:.2f}): only cosmetic changes"}
        return {"backend": self.backend, "model": self.model, "calls": 1, "prompt_tokens": a.input_tokens or 0,
                "important": True, "text": f"most important ({conf:.2f}): {lines[idx]}"}


# ---------------------------------------------------------------------------
# the advisory agent hook: PRINTED, never installed
# ---------------------------------------------------------------------------

HOOK_MAX_CHARS = 1800


def hook_text(store: WatchStore, since: Optional[str] = None) -> str:
    """The hook's one advisory line for the agent: the watch changes, drift and outages since ``since`` (default:
    the last 24 h), from the local changelog only (no network, no browser, no model); "" when there is none."""
    cut = parse_iso(since) if since else utcnow() - _dt.timedelta(days=1)
    rows = [e for e in store.changelog() if e.get("kind") in ("change", "drift", "needs_review", "outage") and
            (parse_iso(e.get("at")) or utcnow()) >= (cut or utcnow())]
    if not rows:
        return ""
    ch = [e for e in rows if e["kind"] == "change"]
    dr = [e for e in rows if e["kind"] == "drift"]
    nr = [e for e in rows if e["kind"] == "needs_review"]
    down = [e for e in rows if e["kind"] == "outage"]
    parts = [f"anyuse watch: {len(ch)} change(s), {len(dr)} drift(s)" + (f", {len(nr)} needing review" if nr else "")
             + (f", {len(down)} replay(s) the site did not answer" if down else "")
             + f" since {iso(cut) if cut else since}."]
    for e in ch[:5]:
        parts.append(f"{e['site']} {e['input']!r}: " + "; ".join(change_line(c) for c in e["changes"][:3]))
    for e in dr[:3]:
        st = (e.get("drift") or {}).get("status")
        redo = redo_of(e)
        parts.append(f"{e['site']} {e['input']!r}: " + ("KNOWN WRONG answer, not accepted (re-demonstrate)"
                                                        if st == "known_wrong" else
                                                        f"DRIFT, {redo} ({st})" if redo else f"DRIFT ({st})"))
    for e in nr[:3]:
        # needs-review lane: NOT accepted; the two sources (page text, quoted as data) are in `anyuse watch log`
        dis = ((e.get("review") or {}).get("disputed") or [])
        parts.append(f"{e['site']} {e['input']!r}: NEEDS REVIEW, not accepted (" + "; ".join(
            f"{x.get('attribute')}: " + " vs ".join(f"{s.get('source')} {s.get('reads')!r}" for s in x.get("sources") or [])
            for x in dis[:2]) + ")")
    for e in down[:3]:
        parts.append(f"{e['site']} {e['input']!r}: the site did not answer, nothing to recompile")
    parts.append("Details: anyuse watch log")
    return " ".join(parts)[:HOOK_MAX_CHARS]


def hook_output(store: WatchStore) -> str:
    """The hook command's stdout: the Claude Code / Codex hook JSON with the advisory as additional context,
    or "" (nothing to say). The command always exits 0 and never blocks."""
    try:
        text = hook_text(store)
    except Exception:                                  # noqa: BLE001 - a hook never fails the agent
        return ""
    if not text:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def install_hook_text(command: str = "anyuse watch hook-run") -> str:
    """The hook blocks to paste, PRINTED only (``anyuse watch install-hook --print``): nothing is installed."""
    claude = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]}}
    codex = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]}}
    return ("# anyuse watch: an ADVISORY agent hook (printed only; anyuse installs nothing).\n"
            "# At session start it tells the agent what changed or drifted in the watched items (the local\n"
            "# changelog only: no network, no browser, no model); it always exits 0 and never blocks.\n\n"
            "# Claude Code: merge into ~/.claude/settings.json or <repo>/.claude/settings.json\n"
            + json.dumps(claude, indent=2) + "\n\n"
            "# OpenAI Codex: merge into ~/.codex/hooks.json or <repo>/.codex/hooks.json - UNVERIFIED: no Codex\n"
            "# session was run with it; check your Codex version documents a SessionStart hook event.\n"
            + json.dumps(codex, indent=2) + "\n")
