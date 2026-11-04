"""Every successful open task compiled: a ``FlowBrowserTools`` trace -> a FLOW tool that
replays with new values at 0 model calls.

    trace (login, open, search, click, fill, commit clicks, done)
        -> steps with locators (a field by its form + name / id / label; a link by its bound text or its path; a
           button by its text and form) and values bound to the run's parameters ("{product}", "{password}" - a
           secret stays a ``${secret:NAME}`` reference, never a value)
        -> the recorded commit steps (class + bound target): a replay's mandate (``Mandate.for_replay``) covers
           exactly those, with the cap the replay's values carry - "initiation = approval"; a deviation (another
           target, a higher amount, a page the recording does not have) stops
        -> the result: the result links (by their common path) or the final page; the final page's heading checked
        -> the proof on >= 2 FRESH value sets at 0 model calls. A proof never commits on its own: it replays up to the
           first commit step other than a login, checks that step's target, read-back and amount, and stops (dry). A
           flow without such a step is proved by full replays.

The replay is the harness itself with a deterministic policy (``FlowReplayPolicy``): the same tool set, the same
commit gate, the same redactor - only the decisions come from the recording.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import commit_policy as CP
from anyuse.execution import harness_types as T
from anyuse.execution import secret_refs as SR

FORMAT = "anyuse.flow_tool/1"
TOOL_FILE = "compiled_flow.json"
DRAFT_FILE = "compiled_flow.draft.json"
MIN_FRESH_CASES = 2
FLOW_KINDS = (T.Login.kind, T.Fill.kind)
CREDENTIAL_NAMES = ("username", "user", "login", "email", "benutzername", "benutzer", "password", "passwort", "pw",
                    "pass", "kennwort")


def is_flow_trace(trace: T.Trace) -> bool:
    """A trace for the flow compiler: it logged in, filled a form or committed something (a pure search keeps the
    search-tool compile of ``harness_compile``)."""
    return any(s.action is not None and (s.action.kind in FLOW_KINDS or s.commit is not None) for s in trace.steps)


# ---------------------------------------------------------------------------
# binding
# ---------------------------------------------------------------------------
def _params(values: Dict[str, str]) -> List[Tuple[str, str]]:
    # longest values first: "Blue Mug Deluxe" binds before "Blue Mug"
    return sorted(((k, str(v)) for k, v in values.items() if str(v).strip()), key=lambda kv: -len(kv[1]))


def bind(text: Any, values: Dict[str, str]) -> str:
    """``text`` with every run value replaced by its ``{name}`` (whole-value matches first, case-insensitive)."""
    s = str(text or "")
    for k, v in _params(values):
        if CP.fold(s) == CP.fold(v):
            return "{" + k + "}"
    for k, v in _params(values):
        if len(v) >= 3 and v in s:
            s = s.replace(v, "{" + k + "}")
    return s


def fill_in(text: Any, values: Dict[str, str]) -> str:
    s = str(text or "")
    for k, v in values.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def _rel(url: str, site_url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return (p.path or "/") + (("?" + p.query) if p.query else "")


def _locator(m: Dict[str, Any]) -> Dict[str, Any]:
    f = m.get("field") or {}
    return {k: v for k, v in {"form": f.get("form") or m.get("form") or "", "name": f.get("name") or "",
                              "id": f.get("id") or "", "label": f.get("label") or "", "type": f.get("type") or "",
                              "tag": f.get("tag") or ""}.items() if v}


_YES = {"yes", "true", "on", "1", "checked", "selected", "ja", "x"}


def _chosen(m: Dict[str, Any], value: Any) -> str:
    """The value a fill stands for: a radio set with "yes" is the OPTION it chose (its value), so a replay can bind
    another option (a recording kept "yes" on the "Question" radio, and the replay with
    topic=praise sent "question")."""
    f = m.get("field") or {}
    if f.get("type") == "radio" and CP.fold(value) in _YES:
        return str(f.get("value") or f.get("label") or value)
    return str(value)


def unbound_values(tool: Dict[str, Any], values: Dict[str, str]) -> List[str]:
    """The run's values no recorded step, commit or check uses (credentials, secret references and an amount cap
    excepted): a replay with new values could not apply them, so the tool is not proved."""
    used = set(re.findall(r"\{([A-Za-z0-9_]+)\}", json.dumps([tool.get("steps"), tool.get("commits"),
                                                                 tool.get("final")])))
    cap = re.compile(r"max|cap|limit|budget|hochst|amount")
    return [k for k, v in values.items() if k not in used and k.lower() not in CREDENTIAL_NAMES
            and not SR.refs_in(v) and not cap.search(CP.fold(k))]


# ---------------------------------------------------------------------------
# the compile
# ---------------------------------------------------------------------------
def compile_flow(trace: T.Trace) -> Dict[str, Any]:
    """``{"ok", "why", "tool"}``: the flow tool of a finished trace (no proof yet)."""
    if trace.stop != T.Stop.DONE:
        return {"ok": False, "why": f"the run did not finish ({trace.stop})", "tool": None}
    values = dict(trace.values)
    steps: List[Dict[str, Any]] = []
    commits: List[Dict[str, Any]] = []
    last_obs: Optional[T.Observation] = None
    for st in trace.steps:
        a = st.action
        if a is None:
            continue
        obs = st.observation
        if a.kind == T.Done.kind:
            last_obs = obs
            break
        if not st.outcome.ok:
            continue                                    # a refused or failed attempt is not part of the flow
        m = obs.mapping.get(int(getattr(a, "index", -1))) if hasattr(a, "index") else None
        m = m or {}
        if a.kind == T.Login.kind:
            steps.append({"op": "login", "commit": CP.LOGIN})
            commits.append({"class": CP.LOGIN, "target": ""})
        elif a.kind == T.Open.kind:
            url = urllib.parse.urljoin(obs.url or trace.start_url, a.url)
            steps.append({"op": "goto", "path": bind(_rel(url, trace.start_url), values)})
        elif a.kind == T.Search.kind:
            inp = m.get("input") or {}
            steps.append({"op": "search", "input": {k: inp.get(k) for k in ("name", "id", "index") if inp.get(k) not in
                                                    (None, "")}, "text": bind(a.text, values)})
        elif a.kind == T.Click.kind and m.get("field") and m.get("src") == "el" and not st.commit:
            # a click on a radio or checkbox set the field: recorded as a fill, a radio by the option it chose
            f = m["field"]
            v = ("no" if f.get("checked") else "yes") if f.get("type") == "checkbox" else _chosen(m, "yes")
            steps.append({"op": "fill", "fields": [{"locator": _locator(m), "value": bind(v, values)}]})
        elif a.kind == T.Click.kind:
            c = st.commit or {}
            label = str(m.get("text") or "")
            if m.get("tag") == "a" and not c:
                steps.append({"op": "link", "text": bind(label, values),
                              "href": bind(_rel(str(m.get("href") or ""), trace.start_url), values)})
            else:
                step = {"op": "click", "text": label, "form": str(m.get("form") or "")}
                if c:
                    step["commit"] = c.get("class")
                    tgt = c.get("target") or ""
                    if c.get("class") == CP.PAY:
                        tgt = ", ".join(bind(i, values) for i in c.get("items") or [])
                    step["target"] = bind(tgt, values)
                    commits.append({"class": c.get("class"), "target": step["target"]})
                steps.append(step)
        elif a.kind == T.Fill.kind:
            fields = []
            for k, v in a.fields.items():
                fm = obs.mapping.get(int(k)) or {}
                fields.append({"locator": _locator(fm), "value": bind(_chosen(fm, v), values)})
            steps.append({"op": "fill", "fields": fields})
        elif a.kind == T.Select.kind:
            steps.append({"op": "fill", "fields": [{"locator": _locator(m), "value": bind(a.option, values)}]})
        # reads (read_text, read_table, extract_fields, scroll) change nothing: the replay reads the final page
    if not steps:
        return {"ok": False, "why": "the run has no step to replay", "tool": None}
    res = trace.result if isinstance(trace.result, dict) else {}
    rows = res.get("results") or res.get("rows") or []
    result: Dict[str, Any] = {"kind": "page"}
    urls = [urllib.parse.urlsplit(str(r.get("url") or "")).path for r in rows if isinstance(r, dict) and r.get("url")]
    if urls:
        prefix = os.path.commonprefix(urls)
        prefix = prefix[:prefix.rfind("/") + 1] if "/" in prefix else prefix
        if len(prefix) > 1:
            result = {"kind": "links", "prefix": prefix}
    final_h1 = ""
    if last_obs is not None:
        h1 = ((last_obs.page or {}).get("flow") or {}).get("h1") or (last_obs.page or {}).get("h1") or []
        final_h1 = str(h1[0]) if h1 else ""
    from anyuse.execution.request_policy import origin
    tool = {"format": FORMAT, "site": origin(trace.start_url), "start_url": trace.start_url,
            "task": bind(trace.task, values),
            "params": [{"name": k, "example": v if not SR.refs_in(v) else v, "secret_ref": bool(SR.refs_in(v))}
                       for k, v in values.items()],
            "steps": steps, "commits": commits, "result": result,
            "final": {"h1": bind(final_h1, values)} if final_h1 else {},
            "cap": trace.mandate.get("cap") if isinstance(trace.mandate, dict) else None,
            "harness_run_id": trace.run_id, "harness_digest": trace.digest()}
    return {"ok": True, "why": "", "tool": tool, "bytes": len(json.dumps(tool, ensure_ascii=False))}


# ---------------------------------------------------------------------------
# the replay: the harness with a deterministic policy
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return " ".join(str(s or "").split()).lower()


class FlowReplayPolicy:
    """Answers each observation with the recording's next step resolved on THIS page; a step that cannot be
    resolved fails with a deviation reason. ``dry``: stop before the first commit other than a login, after checking
    it against the replay's mandate (target, read-back, amount)."""

    #: no model behind it, so its replay runs under a budget of 0
    calls_model = False

    def __init__(self, tool: Dict[str, Any], values: Dict[str, str], toolbox=None, mandate=None, dry: bool = False):
        self.tool, self.values, self.toolbox, self.mandate, self.dry = tool, dict(values), toolbox, mandate, dry
        self.i = 0
        self.calls: List[Dict[str, Any]] = []

    def _fail(self, why: str) -> T.Decision:
        return T.Decision(T.Fail(f"deviation: step {self.i + 1} of the recording: {why}"))

    def _el(self, obs: T.Observation, pred) -> Optional[int]:
        for e in obs.elements:
            m = obs.mapping.get(e.index) or {}
            if pred(e, m):
                return e.index
        return None

    def _field(self, obs: T.Observation, loc: Dict[str, Any]) -> Optional[int]:
        def same(f, m):
            f = m.get("field") or {}
            if not f:
                return False
            if loc.get("form") and str(f.get("form") or m.get("form") or "") != loc["form"]:
                return False
            if loc.get("name"):
                if f.get("name") != loc["name"]:
                    return False
                if loc.get("type") == "radio":
                    return True                       # any radio of the group: fill picks by the value
                return True
            if loc.get("id"):
                return f.get("id") == loc["id"]
            return _norm(f.get("label")) == _norm(loc.get("label"))
        return self._el(obs, lambda e, m: same(e, m))

    def decide(self, task: str, obs: T.Observation, history: List[str], available: List[str]) -> T.Decision:
        self.calls.append({"step": self.i, "url": obs.url})
        steps = self.tool.get("steps") or []
        if self.i >= len(steps):
            return T.Decision(T.Done(self._result(obs)))
        st = steps[self.i]
        op = st.get("op")
        base = self.tool.get("start_url") or ""
        if op == "login":
            self.i += 1
            return T.Decision(T.Login())
        if op == "goto":
            self.i += 1
            return T.Decision(T.Open(urllib.parse.urljoin(base, fill_in(st.get("path"), self.values))))
        if op == "search":
            inp = st.get("input") or {}
            idx = self._el(obs, lambda e, m: m.get("src") == "input" and (
                (inp.get("name") and (m.get("input") or {}).get("name") == inp["name"]) or
                (inp.get("id") and (m.get("input") or {}).get("id") == inp["id"]) or
                (not inp.get("name") and not inp.get("id"))))
            if idx is None:
                return self._fail("the search box is not on this page")
            self.i += 1
            return T.Decision(T.Search(idx, fill_in(st.get("text"), self.values)))
        if op == "link":
            text, href = fill_in(st.get("text"), self.values), fill_in(st.get("href"), self.values)
            bound_text = "{" in str(st.get("text") or "")
            idx = None
            if bound_text:
                idx = self._el(obs, lambda e, m: m.get("tag") == "a" and _norm(m.get("text")) == _norm(text))
            if idx is None:
                idx = self._el(obs, lambda e, m: m.get("tag") == "a" and
                               _rel(str(m.get("href") or ""), base) == href and
                               (bound_text is False or _norm(m.get("text")) == _norm(text)))
            if idx is None and not bound_text:
                idx = self._el(obs, lambda e, m: m.get("tag") == "a" and _norm(m.get("text")) == _norm(text))
            if idx is None:
                return self._fail(f"no link {text!r} on this page")
            self.i += 1
            return T.Decision(T.Click(idx))
        if op == "click":
            text, form = st.get("text") or "", st.get("form") or ""
            idx = self._el(obs, lambda e, m: m.get("src") == "el" and m.get("tag") != "a" and
                           _norm(m.get("text")) == _norm(text) and (not form or str(m.get("form") or "") == form))
            if idx is None:
                return self._fail(f"no control {text!r} on this page (an extra step, or a changed page)")
            action = T.Click(idx)
            if self.dry and st.get("commit") and st.get("commit") != CP.LOGIN and self.toolbox is not None:
                commit = self.toolbox.commit_of(action, obs)
                if commit is None:
                    return self._fail(f"{text!r} is no longer a {st.get('commit')} step")
                v = CP.decide(self.mandate, commit, None, None) if self.mandate is not None else None
                if v is not None and v.kind != CP.COVERED:
                    return self._fail(f"the {commit.cls} step would not be covered: {v.why}")
                self.i += 1
                return T.Decision(T.Done({"dry": True, "stopped_before": {"class": commit.cls, "label": commit.label},
                                          "commit": commit.to_dict(), "verdict": v.to_dict() if v else None}))
            self.i += 1
            return T.Decision(action)
        if op == "fill":
            fields: Dict[str, str] = {}
            for f in st.get("fields") or []:
                idx = self._field(obs, f.get("locator") or {})
                if idx is None:
                    return self._fail(f"the field {(f.get('locator') or {}).get('label') or (f.get('locator') or {}).get('name')!r}"
                                      " is not on this page")
                fields[str(idx)] = fill_in(f.get("value"), self.values)
            self.i += 1
            return T.Decision(T.Fill(fields))
        return self._fail(f"unknown step {op!r}")

    def _result(self, obs: T.Observation) -> Any:
        want = (self.tool.get("final") or {}).get("h1")
        h1 = ((obs.page or {}).get("flow") or {}).get("h1") or []
        got = str(h1[0]) if h1 else ""
        out: Dict[str, Any] = {"url": obs.url, "title": obs.title, "h1": got}
        if want and _norm(fill_in(want, self.values)) != _norm(got):
            out["final_check"] = f"the final page says {got!r}, the recording ended on {fill_in(want, self.values)!r}"
        r = self.tool.get("result") or {}
        if r.get("kind") == "links":
            rows = []
            for e in obs.elements:
                m = obs.mapping.get(e.index) or {}
                if m.get("tag") == "a" and urllib.parse.urlsplit(str(m.get("href") or "")).path.startswith(r["prefix"]):
                    rows.append({"title": str(m.get("text") or ""), "url": str(m.get("href") or "")})
            out["results"] = rows
            if not rows:
                out["no_results"] = True
        return out


def replay_values(tool: Dict[str, Any], values: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """The values a replay runs with: the recorded credentials (a username, a ``${secret:NAME}`` reference - never a
    secret) unless the caller gives others, then the caller's values."""
    vals = {p["name"]: str(p.get("example")) for p in tool.get("params") or []
            if p.get("example") is not None and (p.get("secret_ref") or p["name"].lower() in CREDENTIAL_NAMES)}
    vals.update({str(k): str(v) for k, v in (values or {}).items() if v is not None})
    return vals


def replay_flow(tool: Dict[str, Any], values: Dict[str, str], toolbox, *, asker=None, dry: bool = False,
                max_steps: int = 0) -> Dict[str, Any]:
    """One replay of a flow tool on ``toolbox`` (fresh pages, built with ``replay_values``) with ``values``: ``{ok,
    outcome, reason, result, model_calls, steps, commits, dry}``. 0 model calls by construction (no policy model);
    a deviation stops."""
    from anyuse.execution import agent_harness as H
    vals = replay_values(tool, values)
    need = sorted(set(re.findall(r"\{([A-Za-z0-9_]+)\}", json.dumps([tool.get("steps"), tool.get("commits"),
                                                                       tool.get("final")]))) - set(vals))
    if need:
        return {"ok": False, "outcome": T.Stop.REFUSED, "reason": f"missing values: {', '.join(need)}", "result": None,
                "model_calls": 0, "steps": 0, "dry": False, "commits": [], "questions": [], "trace": None}
    cap = CP.Money(float(tool["cap"]["amount"]), tool["cap"].get("currency") or "") if tool.get("cap") and not any(
        re.search(r"max|cap|limit|budget|hochst|amount", CP.fold(k)) for k in vals) else None
    mandate = CP.Mandate.for_replay(tool.get("task") or "", tool.get("commits") or [], vals, cap=cap)
    pol = FlowReplayPolicy(tool, vals, toolbox=toolbox, mandate=mandate, dry=dry)
    cfg = H.HarnessConfig(max_steps=max_steps or len(tool.get("steps") or []) + 3, max_usd=0.0,
                          unasked_irreversible=H.UNASKED_STOP, no_progress_steps=0)
    tr = H.Harness(toolbox, pol, cfg, asker=asker, mandate=mandate).run(fill_in(tool.get("task") or "", vals), vals)
    res = tr.result if isinstance(tr.result, dict) else {}
    ok = tr.ok and not res.get("final_check")
    outcome = tr.stop if not res.get("final_check") else T.Stop.DEVIATION
    reason = tr.stop_reason if not res.get("final_check") else f"deviation: {res['final_check']}"
    if tr.stop == T.Stop.FAIL and str(tr.stop_reason).startswith("deviation"):
        outcome = T.Stop.DEVIATION
    return {"ok": ok, "outcome": outcome, "reason": reason, "result": tr.result, "model_calls": tr.model_calls,
            "steps": len(tr.steps), "dry": bool(res.get("dry")),
            "commits": [{"step": s.k, **(s.commit or {}), "verdict": (s.verdict or {}).get("kind"), "ok": s.outcome.ok}
                        for s in tr.steps if s.commit is not None],
            "questions": tr.questions, "trace": tr}


# ---------------------------------------------------------------------------
# the proof and the store
# ---------------------------------------------------------------------------
def has_commit_after_login(tool: Dict[str, Any]) -> bool:
    return any(s.get("commit") and s.get("commit") != CP.LOGIN for s in tool.get("steps") or [])


def prove_flow(tool: Dict[str, Any], fresh_sets: Sequence[Dict[str, str]], make_toolbox: Callable[[Dict[str, str]], Any],
               *, demonstrated: Optional[Dict[str, str]] = None, asker=None) -> Dict[str, Any]:
    """``{"passed", "reason", "cases"}``: each fresh value set replayed (dry when the flow commits beyond a login) at 0
    model calls; never a demonstrated value set; >= ``MIN_FRESH_CASES`` cases."""
    dry = has_commit_after_login(tool)
    sets = [dict(s) for s in fresh_sets if dict(s) != dict(demonstrated or {})]
    cases = []
    if len(sets) < MIN_FRESH_CASES:
        return {"passed": False, "reason": f"a flow proof needs >= {MIN_FRESH_CASES} fresh value sets", "cases": [],
                "dry": dry}
    for vals in sets:
        tb = make_toolbox(vals)
        try:
            r = replay_flow(tool, vals, tb, asker=asker, dry=dry)
        finally:
            close = getattr(tb, "close", None)
            if callable(close):
                close()
        ok = r["ok"] and r["model_calls"] == 0 and (r["dry"] or not dry)
        cases.append({"values": {k: v for k, v in vals.items() if not SR.refs_in(v)}, "ok": ok,
                      "outcome": r["outcome"], "reason": r["reason"][:300], "model_calls": r["model_calls"],
                      "dry": r["dry"]})
    passed = all(c["ok"] for c in cases)
    return {"passed": passed, "reason": "" if passed else next(c["reason"] or c["outcome"] for c in cases if not c["ok"]),
            "cases": cases, "dry": dry}


def save_flow_tool(storage, name: str, tool: Dict[str, Any], proof: Dict[str, Any], goal: str = "") -> Dict[str, Any]:
    """Beside an ordinary ``Workflow``: ``compiled_flow.json`` (``ready``, the proof passed) or the draft with the
    reasons. A ready tool of the same name is never replaced."""
    from anyuse.memory.models import Workflow
    if storage is None:
        from anyuse.memory.storage import WorkflowStorage
        storage = WorkflowStorage()
    existing = storage.get(name)
    status = "ready" if proof.get("passed") else "draft"
    reasons = [] if proof.get("passed") else [str(proof.get("reason") or "proof not passed")]
    if existing is not None and (load_flow_tool(storage, name) or {}).get("status") == "ready":
        status = "draft"
        reasons.append(f"shadow: a ready flow tool {name!r} exists; this compile is kept beside it as a draft")
    wf = existing or Workflow.create_new(name, goal or str(tool.get("task") or ""))
    if existing is None:
        wf.tags = ["agent-harness", "web", "flow", str(tool.get("site") or "")]
        storage.save(wf)
    d = str(storage.get_workflow_dir(wf.id))
    os.makedirs(d, exist_ok=True)
    rec = {"format": FORMAT, "workflow": name, "workflow_id": wf.id, "kind": "web_flow", "tool": tool,
           "tool_sha256": hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
           "proof": proof, "status": status, "status_reasons": reasons,
           "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = os.path.join(d, TOOL_FILE if status == "ready" else DRAFT_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    return {"workflow_id": wf.id, "name": name, "status": status, "path": path, "reasons": reasons}


def load_flow_tool(storage, name: str) -> Optional[Dict[str, Any]]:
    if storage is None:
        from anyuse.memory.storage import WorkflowStorage
        storage = WorkflowStorage()
    wf = storage.get(name)
    if wf is None:
        return None
    d = str(storage.get_workflow_dir(wf.id))
    for f in (TOOL_FILE, DRAFT_FILE):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
            sha = hashlib.sha256(json.dumps(rec.get("tool"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if sha != rec.get("tool_sha256"):
                rec["status"], rec["status_reasons"] = "draft", ["the tool's digest does not match: not run"]
            return rec
    return None


def compile_and_save_flow(trace: T.Trace, storage, fresh_sets: Sequence[Dict[str, str]],
                          make_toolbox: Callable[[Dict[str, str]], Any], *, name: str = "", asker=None
                          ) -> Dict[str, Any]:
    from anyuse.execution.model_adapter import calls_since, process_call_count
    mark = process_call_count()
    name = name or f"{(trace.site or 'site').replace('.', '_').replace(':', '_')}.flow_{trace.run_id[:6]}"
    c = compile_flow(trace)
    if not c["ok"]:
        return {"status": "draft", "name": name, "reasons": [c["why"]], "proof": None, "model_calls": calls_since(mark)}
    base = {k: v for k, v in trace.values.items()}
    sets = [dict(base, **{k: str(v) for k, v in s.items()}) for s in fresh_sets]
    loose = unbound_values(c["tool"], base)
    if loose:
        proof = {"passed": False, "cases": [], "dry": has_commit_after_login(c["tool"]),
                 "reason": f"the run's value(s) {', '.join(loose)} are used by no recorded step: a replay could not "
                           "apply new values for them"}
    else:
        proof = prove_flow(c["tool"], sets, make_toolbox, demonstrated=base, asker=asker)
    saved = save_flow_tool(storage, name, c["tool"], proof, goal=trace.task)
    return {"status": saved["status"], "name": name, "reasons": saved["reasons"], "proof": proof, "tool": c["tool"],
            "saved": saved, "model_calls": calls_since(mark)}


# ---------------------------------------------------------------------------
# a saved trace (``Trace.to_dict(with_pages=True)``) back as a trace, to compile it later
# ---------------------------------------------------------------------------
def trace_from_dict(d: Dict[str, Any]) -> T.Trace:
    def obs_of(o: Dict[str, Any]) -> T.Observation:
        els = [T.Element(**{k: v for k, v in e.items() if k in T.Element.__dataclass_fields__})
               for e in o.get("elements") or []]
        return T.Observation(kind=o.get("kind") or "none", url=o.get("url") or "", title=o.get("title") or "",
                             text=o.get("text") or "", elements=els, inputs=list(o.get("inputs") or []),
                             page=dict(o.get("page") or {}), lists=list(o.get("lists") or []),
                             mapping={int(k): v for k, v in (o.get("mapping") or {}).items()},
                             blocked=o.get("blocked") or "", note=o.get("note") or "")
    tr = T.Trace(kind=d.get("kind") or "web", task=d.get("task") or "", values=dict(d.get("values") or {}),
                 start_url=d.get("start_url") or "", site=d.get("site") or "", stop=d.get("stop") or "",
                 stop_reason=d.get("stop_reason") or "", result=d.get("result"), run_id=d.get("run_id") or "",
                 mandate=d.get("mandate"))
    for st in d.get("steps") or []:
        a = T.action_from_dict(st.get("action")) if st.get("action") else None
        oc = st.get("outcome") or {}
        tr.steps.append(T.Step(int(st.get("k") or 0), obs_of(st.get("observation") or {}), a, st.get("effect") or "read",
                               T.Outcome(bool(oc.get("ok")), oc.get("why") or "", how=oc.get("how") or "",
                                         url_after=oc.get("url_after") or "", href=oc.get("href") or "",
                                         data=dict(oc.get("data") or {}), status=oc.get("status") or ""),
                               commit=st.get("commit"), verdict=st.get("verdict")))
    return tr
