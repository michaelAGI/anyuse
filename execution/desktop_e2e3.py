"""Desktop engine, generation 3: a HARD test on tasks an
independent author wrote.   Builds on ``desktop_e2e`` and
``desktop_e2e2``, both left unchanged, and is PURE like them (no
pyobjc, no subprocess, no network).

**The map-check fix.** ``desktop_e2e2``'s ``refine_plan``
decided whether a plan already applies a step's scope by comparing WORDS: the
scope "Oxford-Wörterbuch" and the planned toggle "Oxford German Dictionary"
are the same map option, but the words differ, so a second, picker-decided
toggle was inserted (task E2, all 6 architecture runs; offline: a second
navigate). Here a scope and every planned toggle_select / navigate are first
RESOLVED TO MAP IDENTITIES, and coverage and de-duplication compare those:

* option identity = the set of probed option paths a text names, by the map's
  label rule (``label_match``), else by the same rule with kind words removed
  ("Oxford-Wörterbuch" -> "Oxford" -> ``Suchen > Oxford German Dictionary``,
  as ``desktop_e2e2`` already matched navigate targets), else the options sharing a
  word (the picker decides among them);
* location identity = the words of a place name without kind and stop words
  ("the AnyUse-E2 folder" == "AnyUse-E2"); a planned navigate covers a scope
  whose words it contains (target or value);
* a scope is covered by a planned step of the same app and mechanism whose
  identity lies inside the scope's (a map-decided planned step: one option);
* when the scope names exactly one option (label or bare), the inserted step
  is that option (map-decided, no picker);
* after insertion, a later step that re-applies an identity already in force
  (same option, no other option of its group chosen in between; the same
  place, no other step of the app in between) is dropped;
* a step whose own scope contradicts it (a toggle of the scope's group to
  another option) makes the plan not executable.

Everything else in the check is ``desktop_e2e2``'s (restrictions, invented scopes,
unapplicable scopes, minimal plans), copied unchanged so that the
frozen module stays byte-identical.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from . import desktop_e2e as X
from . import desktop_e2e2 as Y

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. Map identities of scopes and of planned steps
# ---------------------------------------------------------------------------


def bare_tokens(text: Any) -> List[str]:
    """The words of a name without kind and stop words ("the AnyUse-E2
    folder" -> ["anyuse", "e2"]; "Oxford-Wörterbuch" -> ["oxford"])."""
    return [t for t in X.tokens(text) if t not in Y.KIND_WORDS and t not in Y.STOP_WORDS]


@dataclasses.dataclass(frozen=True)
class Identity:
    """What a text resolves to in the map: ``kind`` option | location | none;
    ``keys`` the option paths (option) or the name words (location); ``how``
    label | bare | overlap | words."""
    kind: str
    keys: FrozenSet[str] = frozenset()
    how: str = ""

    @property
    def unique(self) -> bool:
        return self.kind == "option" and len(self.keys) == 1 and self.how in ("label", "bare")

    def to_dict(self):
        return {"kind": self.kind, "keys": sorted(self.keys), "how": self.how}


NONE = Identity("none")


def _pairs(emap, app: str, target: str = "") -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    groups = emap.entries(app, "toggle_select")
    if len(groups) > 1 and target:
        hit = X.label_match(target, [g.get("group") or "" for g in groups])
        if len(hit) == 1:
            groups = [groups[hit[0]]]
    return [(g, o) for g in groups for o in g["options"]]


def option_identity(emap, app: str, text: str, target: str = "") -> Identity:
    """The probed toggle options a text names: label rule, else the label rule
    without kind words, else the options sharing a word with it."""
    pairs = _pairs(emap, app, target)
    labels = [o["label"] for _g, o in pairs]
    if not labels or not X.norm(text):
        return NONE
    hit = X.label_match(text, labels)
    if hit:
        return Identity("option", frozenset(pairs[i][1]["path"] for i in hit), "label")
    bare = " ".join(bare_tokens(text))
    if bare and bare != X.norm(text):
        hit = X.label_match(bare, labels)
        if hit:
            return Identity("option", frozenset(pairs[i][1]["path"] for i in hit), "bare")
    words = set(X.tokens(text))
    over = [i for i, lab in enumerate(labels) if words & set(X.tokens(lab))]
    if over:
        return Identity("option", frozenset(pairs[i][1]["path"] for i in over), "overlap")
    return NONE


def location_identity(text: str) -> Identity:
    toks = bare_tokens(text)
    return Identity("location", frozenset(toks), "words") if toks else NONE


def scope_identity(emap, app: str, scope: str) -> Identity:
    """A scope's identity, in ``desktop_e2e2``'s order of mechanisms: an option of the
    app's probed toggle groups (label or bare), else a location (apps with
    navigate), else options sharing a word (picker), else none."""
    ident = option_identity(emap, app, scope)
    if ident.kind == "option" and ident.how in ("label", "bare"):
        return ident
    if app in Y.NAVIGABLE_APPS:
        loc = location_identity(scope)
        if loc.kind == "location":
            return loc
    if ident.kind == "option":
        return ident
    return NONE


def step_identity(emap, step: Dict[str, str]) -> Identity:
    """The identity a planned toggle_select or navigate step applies."""
    if step.get("effect") == "toggle_select":
        return option_identity(emap, step["app"], step.get("value") or "", step.get("target") or "")
    if step.get("effect") == "navigate":
        toks = set(bare_tokens(step.get("target") or "")) | set(bare_tokens(step.get("value") or ""))
        return Identity("location", frozenset(toks), "words") if toks else NONE
    return NONE


def covers(planned: Identity, scope: Identity) -> bool:
    """Does a planned step's identity apply the scope's? Options: the planned
    step's options lie inside the scope's (a map-decided step names one).
    Locations: every word of the scope's place is in the planned navigate."""
    if planned.kind != scope.kind or not planned.keys or not scope.keys:
        return False
    if scope.kind == "option":
        return planned.keys <= scope.keys
    return scope.keys <= planned.keys


def _group_of(emap, app: str, path: str) -> Optional[str]:
    for g in emap.entries(app, "toggle_select"):
        if any(o["path"] == path for o in g["options"]):
            return g.get("id") or g.get("group")
    return None


# ---------------------------------------------------------------------------
# 2. The post-plan check with identities; desktop_e2e2's other rules unchanged
# ---------------------------------------------------------------------------

def refine_plan3(goal: str, steps: Sequence[Dict[str, str]], emap) -> Y.Refined:
    out = Y.Refined([dict(s, scope=s.get("scope") or "") for s in steps])
    # (0) as in desktop_e2e2: an invented scope (not named by a goal with no restriction) is ignored
    if not Y.restrictions(goal):
        for s in out.steps:
            sc = s.get("scope") or ""
            if X.norm(sc) and not Y._named_in_goal(sc, goal):
                out.changes.append(f"ignored scope({sc!r}) of {s['effect']}: not named by a goal without a restriction")
                s["scope"] = ""
    # (a) scopes -> map identities -> the map's own step, unless a planned step
    #     before it (or the step itself) already applies that identity
    i = 0
    while i < len(out.steps):
        s = out.steps[i]
        sc, app = s.get("scope") or "", s.get("app")
        if not X.norm(sc) or app == "none" or Y.APP_WORDS.get(X.norm(sc)) == app:
            i += 1
            continue
        ident = scope_identity(emap, app, sc)
        if ident.kind == "none":
            # as in desktop_e2e2: refused only when this scope is the one carrier of a goal restriction
            sole = [r for r in Y.restrictions(goal) if Y.carries(s, r["key"])
                    and not any(Y.carries(dict(t, scope=""), r["key"]) or (t is not s and Y.carries(t, r["key"]))
                                for t in out.steps)]
            if sole:
                out.problems.append(f"scope {sc!r} of {s['effect']} cannot be applied in {app} (no option or location)")
            else:
                out.changes.append(f"ignored scope({sc!r}) of {s['effect']}: cannot be applied in {app} and "
                                   "carries no restriction another step does not")
                s["scope"] = ""
            i += 1
            continue
        mech = "toggle_select" if ident.kind == "option" else "navigate"
        # the step itself contradicts its scope (a toggle of the scope's group to another option)
        if ident.unique and s["effect"] == "toggle_select":
            own = step_identity(emap, s)
            (want,) = tuple(ident.keys)
            if own.unique and not covers(own, ident) and \
                    _group_of(emap, app, next(iter(own.keys))) == _group_of(emap, app, want):
                out.problems.append(f"{s['effect']}({s['value']!r}) contradicts its own scope {sc!r}")
                i += 1
                continue
        applied = [t for t in out.steps[:i + 1] if t["app"] == app and t["effect"] == mech
                   and covers(step_identity(emap, t), ident)]
        if applied:
            out.changes.append(f"scope {sc!r} of {s['effect']} already applied by {mech}"
                               f"({applied[0]['value'] or applied[0]['target']!r}) (same map identity)")
            i += 1
            continue
        if ident.kind == "option":
            pairs = _pairs(emap, app)
            if ident.unique:
                (path,) = tuple(ident.keys)
                g, o = next((g, o) for g, o in pairs if o["path"] == path)
                new = {"effect": "toggle_select", "app": app, "target": (g.get("group") or "").split(" > ")[-1],
                       "value": o["label"], "scope": ""}
            else:
                new = {"effect": "toggle_select", "app": app, "target": "", "value": sc, "scope": ""}
        else:
            new = {"effect": "navigate", "app": app, "target": sc, "value": "", "scope": ""}
        out.steps.insert(i, new)
        out.changes.append(f"scope {sc!r} of {s['effect']} applied as {new['effect']}({new['value'] or new['target']!r})"
                           f" [identity {ident.how}: {sorted(ident.keys)}]")
        i += 2
    # (a2) de-duplicate by identity: a later step re-applying what is already in force
    changed = True
    while changed:
        changed = False
        for j, t in enumerate(out.steps):
            if t["effect"] not in ("toggle_select", "navigate"):
                continue
            tid = step_identity(emap, t)
            if tid.kind == "none" or (t["effect"] == "toggle_select" and not tid.unique):
                continue
            for k in range(j - 1, -1, -1):
                u = out.steps[k]
                if u["app"] != t["app"]:
                    continue
                if u["effect"] != t["effect"]:
                    if t["effect"] == "navigate":
                        break                         # a step of the app in between may change the place
                    continue
                uid = step_identity(emap, u)
                if t["effect"] == "toggle_select":
                    if not uid.unique:
                        break
                    same_group = _group_of(emap, t["app"], next(iter(uid.keys))) == \
                        _group_of(emap, t["app"], next(iter(tid.keys)))
                    if not same_group:
                        continue                      # another group: look further back
                    if uid.keys == tid.keys:
                        why = "the same map option is already chosen (no other option of its group in between)"
                    else:
                        break                         # the group was set otherwise: not a duplicate
                else:
                    if uid.keys == tid.keys:
                        why = "the same place is already shown (no other step of the app in between)"
                    else:
                        break
                out.changes.append(f"dropped {t['effect']}({t['value'] or t['target']!r}): {why}")
                if t.get("scope") and not u.get("scope"):
                    u["scope"] = t["scope"]
                del out.steps[j]
                changed = True
                break
            if changed:
                break
    # (b) desktop_e2e2's minimal plans (unchanged): drop lead-ups the next step does not need
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(out.steps):
            nxt = out.steps[i + 1] if i + 1 < len(out.steps) else None
            same = nxt is not None and nxt["app"] == s["app"]
            why = None
            if s["effect"] == "scoped_find" and same and nxt["effect"] in ("open_item", "select_item") \
                    and s["app"] in Y.CONTENT_APPS and not Y.goal_asks_search(goal):
                why = "search before an open/select of a row the picker reads directly"
            elif s["effect"] == "select_item" and same and nxt["effect"] == "open_item":
                why = "select before an open in the same app (the open selects its row)"
            elif s["effect"] == "navigate" and same and nxt["effect"] in Y.CONTAINER_FREE \
                    and not Y._named_in_goal(s["target"], goal):
                why = f"navigate the goal does not name before {nxt['effect']} (acts on no container)"
            elif s["effect"] == "field_rw" and same and nxt["effect"] == "doc_save" \
                    and X.norm(nxt["value"]) in ("", X.norm(s["value"])) and X.norm(s["value"]):
                nxt["value"] = s["value"]
                why = "text written right before a doc_save that writes it (the save writes and checks the bytes)"
            if why:
                out.changes.append(f"dropped {s['effect']}({s['target'] or s['value']!r}): {why}")
                del out.steps[i]
                changed = True
                break
    # (c) as in desktop_e2e2: what was dropped never takes a restriction with it
    for m in Y.missing_restrictions(goal, out.steps):
        if not Y.missing_restrictions(goal, steps) or m not in Y.missing_restrictions(goal, steps):
            out.problems.append(f"restriction {m['phrase']!r} lost by the map check")
    return out


# ---------------------------------------------------------------------------
# 3. The architecture arm: desktop_e2e2's with the identity check
# ---------------------------------------------------------------------------

class Arch3(Y.Arch2):
    def plan(self, rec: X.Rec, goal: str):
        if self.planner is None:
            raise X.GuestError("model needed: no planner in compiled mode")
        text, steps, err, entry = self._call_planner(rec, goal)
        if not err:
            miss = Y.missing_restrictions(goal, steps)
            entry["missing_restrictions"] = miss
            for _ in range(Y.MAX_CHECK_REPLANS):
                if not miss:
                    break
                text, steps, err, entry = self._call_planner(rec, goal, Y.feedback_messages(text, miss))
                if err:
                    break
                miss = Y.missing_restrictions(goal, steps)
                entry["missing_restrictions"] = miss
            if not err and miss:
                err = "restriction not carried after feedback: " + "; ".join(m["phrase"] for m in miss)
        if err:
            return [], err
        ref = refine_plan3(goal, steps, self.m)
        entry["refined"] = ref.to_dict()
        if ref.problems:
            return [], "; ".join(ref.problems)
        return ref.steps, None


# ---------------------------------------------------------------------------
# 4. The tasks: written by INDEPENDENT authors from a written brief only;
#    drawn at random (seeded) from the accepted pool; frozen with their
#    translation into the reader's terms in ``desktop_e2e3_tasks.json``
#    (sha256 in the design notes)
# ---------------------------------------------------------------------------

TASKS3_PATH = os.path.join(HERE, "desktop_e2e3_tasks.json")
GEN3 = X.GEN + "/e3"
FRONT = {"Finder": "com.apple.finder", "Notizen": "com.apple.Notes", "TextEdit": "com.apple.TextEdit",
         "Lexikon": "com.apple.Dictionary"}
TE = "com.apple.TextEdit"
#: the author's TextEdit setting names -> the Bearbeiten-menu path (the open document's marks)
TE_MENU = {"Intelligentes Kopieren/Einsetzen": "Bearbeiten > Ersetzungen > Intelligentes Kopieren/Einsetzen",
           "Intelligente Anführungszeichen": "Bearbeiten > Ersetzungen > Intelligente Anführungszeichen",
           "Intelligente Bindestriche": "Bearbeiten > Ersetzungen > Intelligente Bindestriche",
           "Intelligente Links": "Bearbeiten > Ersetzungen > Intelligente Links",
           "Während der Texteingabe prüfen":
               "Bearbeiten > Rechtschreibung und Grammatik > Während der Texteingabe prüfen",
           "Rechtschreibung autom. korrigieren":
               "Bearbeiten > Rechtschreibung und Grammatik > Rechtschreibung autom. korrigieren"}


def load_tasks3(path: str = TASKS3_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _folder(tid: str, a: Dict[str, Any]) -> Optional[str]:
    ff = a["start"].get("finder_folder")
    return f"{GEN3}/{tid}/{ff['name']}" if ff else None


def _find_file(tid: str, a: Dict[str, Any], name: str) -> str:
    ff = a["start"]["finder_folder"]
    base = _folder(tid, a)
    if any(f["name"] == name for f in ff.get("files") or []):
        return f"{base}/{name}"
    for d in ff.get("subfolders") or []:
        if any(f["name"] == name for f in d.get("files") or []):
            return f"{base}/{d['name']}/{name}"
    raise KeyError(f"{tid}: no file {name!r} in the author's data")


def build_start(tid: str, a: Dict[str, Any], te_prefs_titles: Dict[str, str],
                te_prefs_baseline: Dict[str, bool], plain_text_title: str = "") -> Dict[str, Any]:
    """The author's start, literally, in the guest's clean-start terms."""
    s = a["start"]
    st: Dict[str, Any] = {"app": FRONT[s["front_app"]]}
    ff = s.get("finder_folder")
    if ff:
        st["finder"] = {"folder": _folder(tid, a), "view": "Als Liste", "sort": s.get("finder_sort") or "Name",
                        "files": ff.get("files") or [], "subfolders": ff.get("subfolders") or []}
    te_decl = s.get("textedit_settings") or {}
    if "TextEdit" in a["apps"]:
        # a document opened during the task takes TextEdit's settings for NEW documents:
        # the guest's own (surveyed) settings, overridden by what the author declared,
        # unless the declared settings belong to a document open at the start
        prefs = dict(te_prefs_baseline)
        if not s.get("textedit_document"):
            prefs.update({te_prefs_titles[k]: bool(v) for k, v in te_decl.items()})
            # with TextEdit's "smart quotes and dashes only in rich text" on (the
            # guest's setting), a plain-text document shows them unchecked whatever the
            # setting; declared on, they must show on in the task's .txt documents
            if plain_text_title and any(te_decl.get(k) for k in ("Intelligente Anführungszeichen",
                                                                  "Intelligente Bindestriche")):
                prefs[plain_text_title] = False
        st["te_prefs"] = prefs
    if s.get("textedit_document"):
        st["textedit"] = {"doc": _find_file(tid, a, s["textedit_document"]), "seed": None,
                          "flags": {TE_MENU[k]: bool(v) for k, v in te_decl.items()}}
    ns = s.get("notes_shown")
    if ns:
        st["notes"] = {"folder": ns["folder"], "rest": ns["note"], "view": s.get("notes_view") or "Als Liste",
                       "clear_search": True}
    if s.get("dictionary"):
        st["dictionary"] = {"source": s["dictionary"]["source"]}
    return st


def build_reads(tid: str, a: Dict[str, Any], extra: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    s, apps = a["start"], a["apps"]
    out: List[Dict[str, Any]] = []
    ff = s.get("finder_folder")
    if ff:
        out.append({"kind": "folder", "path": _folder(tid, a)})
        out += [{"kind": "folder", "path": f"{_folder(tid, a)}/{d['name']}"} for d in ff.get("subfolders") or []]
    if ff or "TextEdit" in apps:
        out.append({"kind": "front", "bundles": [TE]})
    if s.get("notes_folders"):
        out.append({"kind": "notes", "folders": [f["name"] for f in s["notes_folders"]]})
    if "Lexikon" in apps or s.get("dictionary"):
        out += [{"kind": "dictionary"}, X._marks("com.apple.Dictionary", "Suchen")]
    return out + [dict(x) for x in extra]


def _kind(a: Dict[str, Any]) -> str:
    p = a["properties"]
    if p.get("gate_or_refuse") in ("gate", "refuse"):
        return p["gate_or_refuse"]
    return "cross-app" if p.get("cross_app") else "single-app"


def build_task(t: Dict[str, Any], te_prefs_titles: Dict[str, str], te_prefs_baseline: Dict[str, bool],
               plain_text_title: str = "") -> X.Task:
    a, tr = t["author_task"], t["translation"]
    tid = a["id"]
    expect = json.loads(json.dumps(tr["expect"]).replace("{GEN3}", GEN3))
    extra = json.loads(json.dumps(tr.get("extra_reads") or []).replace("{GEN3}", GEN3))
    return X.Task(tid, a["utterance"], _kind(a),
                  build_start(tid, a, te_prefs_titles, te_prefs_baseline, plain_text_title),
                  build_reads(tid, a, extra), expect, dev=not t["drawn"])


def tasks3(data: Optional[Dict[str, Any]] = None, drawn: Optional[bool] = True) -> List[X.Task]:
    data = data or load_tasks3()
    out = [build_task(t, data["te_prefs_titles"], data["te_prefs_baseline"], data.get("te_prefs_plain_text") or "")
           for t in data["tasks"]]
    return [t for t in out if drawn is None or (not t.dev) == drawn]


def task3_by_id(tid: str, data: Optional[Dict[str, Any]] = None) -> X.Task:
    for t in tasks3(data, drawn=None):
        if t.id == tid:
            return t
    raise KeyError(tid)


def statements3(tid: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The author's own words for each textual expectation (the reader's reference)."""
    data = data or load_tasks3()
    t = next(x for x in data["tasks"] if x["author_task"]["id"] == tid)
    return {k.replace("{GEN3}", GEN3): v for k, v in (t["translation"].get("statements") or {}).items()}


def tasks3_sha256(data: Optional[Dict[str, Any]] = None) -> str:
    return X.tasks_sha256(tasks3(data, drawn=True))


def seeds3_spec(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The one-time Notes seeding (the Finder data is rebuilt at every clean
    start). Two tasks naming the same Notes folder share it, holding the union
    of their notes (a rule fixed in advance; none of the drawn tasks do)."""
    data = data or load_tasks3()
    notes: Dict[str, List[List[str]]] = {}
    for t in data["tasks"]:
        for f in t["author_task"]["start"].get("notes_folders") or []:
            have = notes.setdefault(f["name"], [])
            for n in f["notes"]:
                if [n["title"], n["text"]] not in have:
                    have.append([n["title"], n["text"]])
    return {"notes": notes}


# ---------------------------------------------------------------------------
# 5. The judge: desktop_e2e2's reader judge (coupled marks from the map),
#    a declared start for menus of an app that is not running at the start,
#    and an LLM READER only where the expected state is textual
# ---------------------------------------------------------------------------

READER_SYSTEM = (
    "You check the end state of a desktop task for a test. You get the user's request, the task author's expected "
    "end state for ONE text file (their own words), the file's text before the task and the file's text after it. "
    "Decide whether the text after is what the author's expected end state describes. Accept only differences a "
    "careful human checker would call the same result for this request: whitespace, a trailing newline, an "
    "equivalent way of writing the same date or number. Reject missing or extra content, other lines changed, "
    "original text lost, a different value. Answer with JSON only: {\"meets\": true|false, \"why\": \"...\"}.")


def reader_messages(task_text: str, statement: str, before: str, after: str) -> List[Dict[str, str]]:
    return [{"role": "system", "content": READER_SYSTEM},
            {"role": "user", "content": json.dumps({"request": task_text, "author_expected_end_state": statement,
                                                    "text_before": before, "text_after": after},
                                                   ensure_ascii=False)}]


def _declared_start(task: X.Task, start: List[Dict[str, Any]], end: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A menu probe whose app was not running at the start reads the declared
    start (the author's settings the clean start applied) for the declared
    paths and the end's own marks elsewhere (unobservable: not judged)."""
    out = []
    for p, s, e in zip(task.read, start or [], end or []):
        if p["kind"] == "marks" and p.get("declared") and not (s or {}).get("ok"):
            items = [{"path": k, "mark": bool(v)} for k, v in p["declared"].items()]
            items += [it for it in (e or {}).get("items") or [] if it["path"] not in p["declared"]]
            s = {"ok": True, "items": items, "declared_start": True}
        out.append(s)
    return out


def judge(task: X.Task, start, end, record=None, emap: Optional[Y.EffectMap2] = None,
           reader: Optional[Callable[[str, str, str, str], Tuple[bool, str, Dict[str, Any]]]] = None,
           statements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    start2 = _declared_start(task, start, end)
    out = Y.judge(task, start2, end, record, emap)
    out["reader"] = []
    exp_texts = task.expect.get("file_text") or {}
    if exp_texts:
        keys = [json.dumps(p, sort_keys=True) for p in task.read]
        S, E = dict(zip(keys, start2 or [])), dict(zip(keys, end or []))
        for path, want in exp_texts.items():
            label = f"{os.path.basename(path)} holds {want!r}"
            item = next((i for i in out["items"] if i["what"] == label), None)
            if item is None or item["status"] != "wrong":
                continue
            k = next((k for p, k in zip(task.read, keys) if p["kind"] == "folder" and p["path"] == os.path.dirname(path)),
                     None)
            ent = lambda r: {x["name"]: x for x in (r or {}).get("entries") or []}  # noqa: E731
            before = (ent(S.get(k)).get(os.path.basename(path)) or {}).get("text") or ""
            after = (ent(E.get(k)).get(os.path.basename(path)) or {}).get("text")
            if after is None or reader is None:
                continue
            statement = (statements or {}).get(path) or f"The file holds exactly: {want}"
            meets, why, meta = reader(task.text, statement, before, after)
            out["reader"].append({"file": os.path.basename(path), "statement": statement, "after": after,
                                  "meets": bool(meets), "why": why, "meta": meta})
            if meets:
                item["status"] = "ok"
                item["by"] = "reader"
        items = out["items"]
        out["success"] = bool(items) and all(i["status"] == "ok" for i in items) and not out["unexpected"] \
            and not out["irreversible"]
        out["wrong_actions"] = sum(1 for i in items if i["status"] == "wrong") + len(out["unexpected"])
    return out


def run_one3(task: X.Task, arm: str, run: int, guest, *, arch: Optional[Arch3] = None,
             baseline: Optional[X.Baseline] = None, compiled: Optional[List[Dict[str, Any]]] = None,
             mode: str = "measured", emap: Optional[Y.EffectMap2] = None, reader=None,
             statements: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    rec = X.Rec(task.id, arm, run, mode=mode)
    t0 = time.monotonic()
    cs = None
    for attempt in (1, 2):
        cs = guest("clean_start", task=task.id, start=task.start, read=task.read)
        X._check_prompt_or_stop(cs)
        if cs.get("clean"):
            break
        rec.notes.append(f"dirty clean start (attempt {attempt}): {cs.get('problems')}")
    rec.clean = {"clean": bool(cs and cs.get("clean")), "problems": (cs or {}).get("problems"),
                 "setup": (cs or {}).get("setup"), "s": round(time.monotonic() - t0, 1)}
    if not (cs and cs.get("clean")):
        rec.stopped = "dirty clean start twice"
        out = rec.to_dict()
        out["judge"] = {"success": False, "items": [], "unexpected": [], "irreversible": [], "wrong_actions": 0,
                        "not_run": "dirty clean start"}
        out["RUN_STOP"] = "two consecutive dirty clean starts (hygiene broken)"
        return out
    start_read = cs.get("read") or []
    try:
        if arm.startswith("arch"):
            arch.run(task, rec, compiled=compiled)
        else:
            baseline.run(task, rec)
    except X._Prompt:
        rec.stopped = "auth prompt seen"
    end = guest("read_state", read=task.read)
    X._check_prompt_or_stop(end)
    out = rec.to_dict()
    out["read_start"], out["read_end"] = start_read, end.get("read") or []
    out["judge"] = judge(task, start_read, end.get("read") or [], out, emap, reader, statements)
    if out["judge"]["irreversible"]:
        out["RUN_STOP"] = "irreversible action: " + "; ".join(out["judge"]["irreversible"])
    return out
