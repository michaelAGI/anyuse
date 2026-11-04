"""A choice parameter bound by the option's IDENTITY, not its title (``execution/choice_identity.py``,
``execution/desktop_routine.py`` item 3, the guest's ``resolve_menu_trigger`` / ``marks_by_identity``), offline with
FAKES.

Pinned here, on the RECORDED routines (``testdata/desktop_routines/choice_routines.json``: 27
recorded routines with a choice parameter) and on synthetic reordering and renaming:

* every recorded choice parameter is bound by a position-free identity: a unique AXIdentifier (Finder's sort
  options, Notes' views), or a shared identifier with its shortcut and recorded title (Lexikon's dictionaries);
* a value names an option by that key, by its recorded key or by its RECORDED label; a title of another UI language
  is not a value (it names nothing in the recording), the key works there;
* a replay with the recorded option (by key or label) is today's routine byte for byte; a parameterized replay
  carries the identity to the guest in the option's trigger;
* replayed on a LIVE menu in English and in another order, the routine marks the right option (the fake guest
  resolves exactly as the real guest does); with the recorded trigger alone a reordered Lexikon list would press
  the wrong dictionary (its shortcuts follow the order) -- the identity does not;
* the real guest code (``desktop_e2e_job.resolve_menu_trigger``, ``marks_by_identity``) on a fake AX menu bar.

No desktop, no AX, no subprocess, no network.
"""
import copy
import importlib
import json
import os
import sys
import types

import pytest

from anyuse.execution import choice_identity as CI
from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_routine as R

import test_desktop_e2e9 as T9

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VM = os.path.join(HERE, "execution", "guest")
FIXTURE = os.path.join(HERE, "testdata", "desktop_routines", "choice_routines.json")
RECORDED = json.load(open(FIXTURE, encoding="utf-8"))["routines"]
EMAP = W.EffectMap4()
FINDER, DICT = "com.apple.finder", "com.apple.Dictionary"


def _gen():
    if VM not in sys.path:
        sys.path.insert(0, VM)
    return importlib.import_module("desktop_gen")


def group(app, name):
    return next(e for e in EMAP.entries(app, "toggle_select") if e["group"] == name)


SORT = group("Finder", "Darstellung > Sortieren nach")
LEXIKON = group("Dictionary", "Suchen")
ENGLISH = {"Name": "Name", "Art": "Kind", "Zuletzt geöffnet": "Date Last Opened", "Hinzugefügt am": "Date Added",
           "Änderungsdatum": "Date Modified", "Erstellungsdatum": "Date Created", "Als Liste": "as List",
           "Als Galerie": "as Gallery"}


def live_items(entry, order=None, rename=None, shortcuts_follow_order=False, marked=None):
    """The group as a LIVE menu: each option's identity (identifier, shortcut, title) in ``order`` (indices into
    the recorded options), titles renamed, and -- as Lexikon does -- shortcuts reassigned by position."""
    opts = entry["options"]
    order = list(order if order is not None else range(len(opts)))
    cmds = [opts[i]["trigger"]["cmd_char"] for i in range(len(opts))]
    out = []
    for pos, i in enumerate(order):
        t = opts[i]["trigger"]
        out.append({"role": "AXMenuItem", "identifier": t["identifier"],
                    "cmd": (cmds[pos] if shortcuts_follow_order else t["cmd_char"]).lower(),
                    "mods": t.get("cmd_mods") or "0", "title": (rename or {}).get(opts[i]["label"], opts[i]["label"]),
                    "mark": "✓" if marked == opts[i]["key"] else ""})
    return out


# ── 1. the identities of the recorded groups ────────────────────────────────────────────────────────────────

def test_the_recorded_groups_have_position_free_keys():
    assert CI.stable_keys(SORT["options"]) == ["id:_NS:102", "id:_NS:105", "id:_NS:114", "id:_NS:117", "id:_NS:120",
                                              "id:_NS:123"]
    lex = CI.stable_keys(LEXIKON["options"])
    assert lex[0] == "id:_NS:226"                                           # "Alle Lexika": its own identifier
    assert lex[1:] == ["id:chooseDictionary:|cmd:1/0|title:Duden-Wissensnetz deutsche Sprache",
                       "id:chooseDictionary:|cmd:2/0|title:Oxford German Dictionary",
                       "id:chooseDictionary:|cmd:3/0|title:Apple Dictionary",
                       "id:chooseDictionary:|cmd:4/0|title:Wikipedia"]
    calc = CI.stable_keys(group("Calculator", "Darstellung")["options"])
    assert calc[0].startswith("id:menuAction:|cmd:1/0|") and len(set(calc)) == 3
    for key in lex:
        assert CI.parse_key(key)["identifier"] in ("_NS:226", "chooseDictionary:")
    # nothing position-free: two options with one identifier and no shortcut
    twins = [{"key": "a", "label": "A", "trigger": {"identifier": "x"}},
             {"key": "b", "label": "B", "trigger": {"identifier": "x"}}]
    assert CI.stable_keys(twins) == [None, None]
    assert "no position-free identity" in CI.resolve_value("A", twins)[1]
    # a row with a value and no identifier
    rows = [{"title": "Oben", "identifier": "", "value": "top"}, {"title": "Unten", "identifier": "", "value": "bottom"}]
    assert CI.stable_keys(rows) == ["value:top", "value:bottom"]
    assert CI.resolve_live(CI.identity_for(rows, 1), [dict(rows[1], title="Bottom"), dict(rows[0], title="Top")]) \
        == (0, "")


def test_a_value_names_an_option_by_key_recorded_key_or_recorded_label_never_a_live_title():
    for v in ("id:_NS:120", "Änderungsdatum", "änderungsdatum"):
        assert CI.resolve_value(v, SORT["options"]) == (4, ""), v
    assert CI.resolve_value("id:chooseDictionary:@5/3", LEXIKON["options"]) == (2, "")      # the recorded key
    assert CI.resolve_value("Date Modified", SORT["options"])[0] is None                   # another language
    assert CI.resolve_value("", SORT["options"])[0] is None


def test_resolve_live_language_and_order_free():
    # unique identifiers: the title and the place do not matter
    live = live_items(SORT, order=[5, 4, 3, 2, 1, 0], rename=ENGLISH)
    for k in range(len(SORT["options"])):
        at, _ = CI.resolve_live(CI.identity_for(SORT["options"], k), live)
        assert live[at]["identifier"] == SORT["options"][k]["trigger"]["identifier"]
    # a shared identifier in a list reordered by the user (Lexikon's shortcuts follow the order): the title decides
    live = live_items(LEXIKON, order=[0, 3, 1, 4, 2], shortcuts_follow_order=True)
    oxford = CI.identity_for(LEXIKON["options"], 2)
    at, _ = CI.resolve_live(oxford, live)
    assert live[at]["title"] == "Oxford German Dictionary" and live[at]["cmd"] != "2"
    # the recorded trigger alone (identifier + shortcut) points at another dictionary there
    G = _gen()
    by_trigger = [it for it in live if G.trigger_matches(it, LEXIKON["options"][2]["trigger"])]
    assert len(by_trigger) == 1 and by_trigger[0]["title"] != "Oxford German Dictionary"
    # renamed (titles absent from the recording): the shortcut decides
    renamed = live_items(LEXIKON, rename={o["label"]: o["label"].upper() + " (EN)" for o in LEXIKON["options"]})
    at, _ = CI.resolve_live(oxford, renamed)
    assert renamed[at]["title"] == "OXFORD GERMAN DICTIONARY (EN)"
    # the documented limit: reordered AND renamed at once, the recorded shortcut decides
    both = live_items(LEXIKON, order=[0, 3, 1, 4, 2], shortcuts_follow_order=True,
                      rename={"Oxford German Dictionary": "Oxford"})
    k, _why = CI.resolve_live(oxford, both)
    assert both[k]["cmd"] == "2"
    # two live items with the recorded title, or none and no shortcut: refused
    dup = live_items(LEXIKON) + [dict(live_items(LEXIKON)[2], cmd="9")]
    assert CI.resolve_live(oxford, dup)[0] is None
    assert CI.resolve_live(dict(oxford, cmd="7"), renamed) == (None, CI.resolve_live(dict(oxford, cmd="7"),
                                                                                       renamed)[1])


# ── 2. the recorded routines ───────────────────────────────────────────────────────────────────────────────

def test_every_recorded_choice_parameter_is_bound_by_identity():
    assert len(RECORDED) == 27
    seen = set()
    for rt in RECORDED:
        r = R.derive_routine(rt["steps"], rt["goal"], task=rt["task"])
        for p in r["params"]:
            if p["kind"] != "choice":
                continue
            assert p["by"] == "identity" and p["key"], (rt["source"], rt["task"], p)
            labels = [o["label"] for o in p["options"]]
            assert p["value"] in labels and all(o["key"] for o in p["options"])
            seen.add(p["key"].split("|")[0])
    assert seen == {"id:_NS:81", "id:_NS:89", "id:_NS:105", "id:_NS:120", "id:chooseDictionary:"}


@pytest.mark.parametrize("rt", RECORDED, ids=[f"{r['source']}:{r['task']}" for r in RECORDED])
def test_each_recorded_routine_takes_any_other_option_by_its_key(rt):
    r = R.derive_routine(rt["steps"], rt["goal"])
    p = next(p for p in r["params"] if p["kind"] == "choice")
    i = p["bind"][0][0]
    # the recorded option by key or by label: today's routine byte for byte
    for same in (p["key"], p["value"]):
        steps, _goal, probs = R.instantiate(r, {p["name"]: same})
        assert probs == [] and steps is r["steps"]
    for o in p["options"]:
        if o["key"] == p["key"]:
            continue
        steps, _goal, probs = R.instantiate(r, {p["name"]: o["key"]})
        if any(x in o["label"].lower() for x in R.EXTERNAL_LABELS15):
            assert probs and "outside the Mac" in probs[0]
            continue
        assert probs == [], probs
        opt = steps[i]["resolved"]["option"]
        assert opt["label"] == o["label"] and steps[i]["step"]["value"] == o["label"]
        assert opt["trigger"]["identity"]["key"] == o["key"]
        assert all(x["trigger"]["identity"]["key"] for x in steps[i]["resolved"]["entry"]["options"])
        # the recorded routine itself is untouched
        assert "identity" not in r["steps"][i]["resolved"]["option"]["trigger"]
    english = next((ENGLISH[o["label"]] for o in p["options"]
                    if o["key"] != p["key"] and ENGLISH.get(o["label"], o["label"]) != o["label"]), None)
    if english:
        assert R.parameter_problems(r, {p["name"]: english})                 # a live title names nothing


# ── 3. replays on a live menu in English and in another order ─────────────────────────────────────────────

class MenuGuest(T9.FakeGuest9):
    """``FakeGuest9`` with LIVE menus: ``press`` and ``marks`` resolve an option exactly as the real guest
    does (``choice_identity.resolve_live`` for a trigger that carries its identity, else the recorded trigger,
    ``desktop_gen.trigger_matches``, exactly one item)."""

    def __init__(self, menus, **kw):
        super().__init__({}, shown=None, **kw)
        self.menus = menus                              # bundle -> [live item]
        self.pressed = []

    def _find(self, bundle, trig):
        items = self.menus.get(bundle) or []
        if trig.get("identity"):
            k, _why = CI.resolve_live(trig["identity"], items)
            return k
        hit = [k for k, it in enumerate(items) if _gen().trigger_matches(it, trig)]
        return hit[0] if len(hit) == 1 else None

    def op_press(self, bundle, trigger, label):
        k = self._find(bundle, trigger)
        if k is None:
            return {"status": "refused", "detail": "unresolved"}
        for j, it in enumerate(self.menus[bundle]):
            it["mark"] = "✓" if j == k else ""
        self.pressed.append(self.menus[bundle][k]["title"])
        return {"status": "done"}

    def op_marks(self, bundle, parent, options):
        keys, sel = [], []
        for o in options:
            k = self._find(bundle, o)
            if k is not None:
                keys.append(o["key"])
                if self.menus[bundle][k]["mark"]:
                    sel.append(o["key"])
        return {"status": "done", "ok": True, "target": {"options": keys, "selected": sel}, "sentinel": []}


def replay(routine, values, menus):
    g = MenuGuest(menus)
    a = R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, probs = a.prepare(routine, values)
    rec = X.Rec("T", "replay", 1, mode="compiled")
    a.run(X.Task("T", goal, "single-app", {}, [], {}), rec, compiled=steps)
    return g, rec, probs


def recorded(source, task):
    return next(r for r in RECORDED if r["source"] == source and r["task"] == task)


def test_a_finder_sort_routine_replays_on_an_english_reordered_menu_by_identity():
    rt = recorded("set A", "SG04")                         # "Show me the files ordered by when I last edited them"
    r = R.derive_routine(rt["steps"], rt["goal"])
    entry = r["steps"][1]["resolved"]["entry"]
    created = CI.stable_keys(entry["options"])[5]
    live = {FINDER: live_items(entry, order=[3, 5, 0, 4, 1, 2], rename=ENGLISH, marked="id:_NS:102")}
    g, rec, probs = replay(r, {"choice": created}, live)
    assert probs == [] and rec.status == "completed", rec.stopped
    assert g.pressed == ["Date Created"] and rec.calls["planner"] == 0 and rec.calls["picker"] == 0
    assert [it["title"] for it in live[FINDER] if it["mark"]] == ["Date Created"]


def test_a_dictionary_choice_replays_on_a_reordered_list_where_the_recorded_shortcut_moved():
    rt = recorded("set A", "GG09")
    tog = next(k for k, c in enumerate(rt["steps"]) if c["step"]["effect"] == "toggle_select")
    two = [rt["steps"][tog - 1], rt["steps"][tog]]                    # launch Lexikon, choose the dictionary
    assert two[0]["step"]["effect"] == "launch_app" and two[0]["step"]["app"] == "Dictionary"
    r = R.derive_routine(two, "")
    entry = r["steps"][1]["resolved"]["entry"]
    oxford = CI.stable_keys(entry["options"])[2]
    menus = {DICT: live_items(entry, order=[0, 3, 1, 4, 2], shortcuts_follow_order=True, marked=entry["options"][3]["key"])}
    g, rec, _ = replay(r, {"choice": oxford}, menus)
    assert rec.status == "completed", rec.stopped
    assert g.pressed == ["Oxford German Dictionary"]
    # the same live list, the recorded trigger alone (no identity): the press lands on another dictionary
    menus2 = {DICT: live_items(entry, order=[0, 3, 1, 4, 2], shortcuts_follow_order=True,
                               marked=entry["options"][3]["key"])}
    g2 = MenuGuest(menus2)
    g2.op_press(DICT, entry["options"][2]["trigger"], "Suchen > Oxford German Dictionary")
    assert g2.pressed and g2.pressed != ["Oxford German Dictionary"]


def test_an_external_option_is_never_bound_and_an_unknown_value_refuses_before_acting():
    rt = recorded("set A", "GG09")
    tog = next(k for k, c in enumerate(rt["steps"]) if c["step"]["effect"] == "toggle_select")
    r = R.derive_routine([rt["steps"][tog - 1], rt["steps"][tog]], "")
    entry = r["steps"][1]["resolved"]["entry"]
    wiki = CI.stable_keys(entry["options"])[4]
    menus = {DICT: live_items(entry)}
    g, rec, probs = replay(r, {"choice": wiki}, menus)
    assert probs and "outside the Mac" in probs[0] and rec.stopped.startswith("refused (parameter check)")
    g, rec, probs = replay(r, {"choice": "Collins"}, menus)
    assert probs and g.pressed == []


# ── 4. the real guest code on a fake AX menu bar ─────────────────────────────────────────────────────────────

class El:
    def __init__(self, role, kids=(), **attrs):
        self.a = {"AXRole": role, **attrs}
        self.a["AXChildren"] = list(kids)


class FakeAS:
    def __init__(self, app):
        self.app, self.calls = app, []

    def AXUIElementCreateApplication(self, pid):
        return self.app

    def AXUIElementPerformAction(self, el, action):
        self.calls.append((el.a.get("AXTitle"), action))
        return 0


def menu_bar(entry, live, top_index, top_title):
    items = [El("AXMenuItem", AXTitle=it["title"], AXIdentifier=it["identifier"], AXMenuItemCmdChar=it["cmd"],
                AXMenuItemCmdModifiers=int(it["mods"]), AXMenuItemMarkChar=it["mark"]) for it in live]
    sub = El("AXMenuItem", [El("AXMenu", items)], AXTitle="Sort By", AXIdentifier="sortby")
    bars = [El("AXMenuBarItem", [El("AXMenu", [])], AXTitle=f"M{j}") for j in range(top_index)]
    bars.append(El("AXMenuBarItem", [El("AXMenu", [sub] if entry is SORT else items)], AXTitle=top_title))
    return El("AXApplication", AXMenuBar=El("AXMenuBar", bars)), items


@pytest.fixture
def guest(monkeypatch):
    sys.path.insert(0, VM) if VM not in sys.path else None
    J = importlib.import_module("desktop_e2e_job")
    G = J.G
    monkeypatch.setenv("ANYUSE_INTERLOCK_TEST_BYPASS", "1")      # fakes only; honoured under the runner's guard

    def at(el, name):
        return el.a.get(name) if isinstance(el, El) else None
    monkeypatch.setattr(G, "_at", at)
    monkeypatch.setattr(J, "_at", at)
    monkeypatch.setattr(J, "_s", lambda v: None if v is None else str(v))
    monkeypatch.setattr(G.ddg, "_int", lambda v: int(v) if v is not None else None)
    monkeypatch.setattr(J.time, "sleep", lambda s: None)
    return J, G, monkeypatch


def test_the_guest_resolves_and_reads_marks_by_identity_on_an_english_reordered_menu(guest):
    J, G, mp = guest
    top = SORT["options"][0]["trigger"]["index_path"][0]
    live = live_items(SORT, order=[5, 4, 3, 2, 1, 0], rename=ENGLISH, marked="id:_NS:105")
    app, items = menu_bar(SORT, live, top, "View")
    ax = FakeAS(app)
    mp.setattr(G, "_AS", lambda: ax)
    mp.setattr(J, "_AS_BASE", lambda: ax)
    runs = []

    class CheckedEX:                       # a menu press goes through the checked executor, never raw
        def run(self, el, pid, selector, action, **kw):
            runs.append((el.a.get("AXTitle"), action))
            return {"success": True, "refused": False}
    mp.setattr(J, "EX", CheckedEX())
    mp.setattr(G, "record_selector", lambda pid, el: {"role": el.a.get("AXRole")})
    opts = copy.deepcopy(SORT["options"])
    for k, o in enumerate(opts):
        o["trigger"]["identity"] = CI.identity_for(SORT["options"], k)
    el, ident, why = J.resolve_menu_trigger(4242, opts[4]["trigger"])           # Änderungsdatum
    assert why is None and el.a["AXTitle"] == "Date Modified" and ident["identifier"] == "_NS:120"
    t, others = J.marks_by_identity(4242, [top], [o["trigger"] for o in opts])
    assert t == {"options": [o["key"] for o in SORT["options"]], "selected": ["id:_NS:105"]} and others == []
    assert runs == [("View", "press"), ("View", "cancel")] and ax.calls == []   # opened and closed, checked
    # a trigger without identity resolves as before
    el2, _i, why2 = J.resolve_menu_trigger(4242, SORT["options"][4]["trigger"])
    assert why2 is None and el2 is el


# ── 5. the VM harness's fingerprints: a draft workflow's choice bound by the option's identifier ─────────────

WEB = os.path.join(HERE, "testdata", "drafts", "web_form_draft.json")
PAY_IDS = {"Überweisung": "pay-transfer", "Lastschrift": "pay-debit", "Rechnung": "pay-invoice"}
PAY_EN = {"Überweisung": "Bank transfer", "Lastschrift": "Direct debit", "Rechnung": "Invoice"}


def web_draft(with_ids=True):
    """The web-form draft as the guest harness now records it: the fingerprints keep each element's AX
    identifier (``interlock_drive_e2e.fingerprint_state``); the chosen radio button's selector carries its own."""
    d = json.load(open(WEB, encoding="utf-8"))
    if with_ids:
        for st in d["ax_states"].values():
            for el in st["fingerprint"]["required_elements"]:
                if el.get("role") == "AXRadioButton":
                    el["identifier"] = PAY_IDS[el["title"]]
        for e in d["events"]:
            sel = (e.get("data") or {}).get("selector") or {}
            if sel.get("raw_role") == "AXRadioButton":
                sel["identifier"] = PAY_IDS[sel["name"]]
    return d


def compile_web(with_ids=True):
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_params as DP
    from anyuse.execution import draft_proof as PR
    import asyncio
    src = DC.load_source(web_draft(with_ids))
    cands = DP.collect_candidates(src.steps)
    ovs = [{"name": "name", "ref": "@c1"}, {"name": "betrag", "ref": "@c2", "type": "number"},
           {"name": "zahlungsart", "ref": "@c3"}]
    choice = DC.choose_parameters(src, cands, overrides=ovs)
    rec = DC.compile_record(src, choice["parameters"], choice["candidates"])
    proof = asyncio.run(PR.prove(rec, choice["candidates"]))
    return DC, PR, rec, proof


def test_a_draft_choice_over_identified_options_is_bound_by_identifier_and_proved():
    DC, PR, rec, proof = compile_web()
    step = next(s for s in rec["steps"] if s.get("choice"))
    assert step["choice"]["by"] == "identifier" and step["command"]["selector"]["identifier"] == "${zahlungsart}"
    assert step["command"]["selector"]["name"] == "" and {o["identifier"] for o in step["choice"]["options"]} \
        == set(PAY_IDS.values())
    assert proof["passed"], proof.get("reason")
    targets = {e["target"] for c in proof["cases"] for e in PR.expected_effects(rec, c["values"])
               if e["target"].startswith("radiobutton")}
    assert targets == {"radiobutton:#pay-debit", "radiobutton:#pay-invoice"}
    # the value names an option by its identifier or its RECORDED title; a live title of another language not
    assert DC.value_problems(rec, {"name": "x", "betrag": "1,00", "zahlungsart": "Rechnung"}) == []
    assert DC.value_problems(rec, {"name": "x", "betrag": "1,00", "zahlungsart": "id:pay-invoice"}) == []
    assert DC.value_problems(rec, {"name": "x", "betrag": "1,00", "zahlungsart": "Invoice"})
    bound = DC.bind_step(step, {"zahlungsart": "Rechnung"})
    assert bound["command"]["selector"]["identifier"] == "pay-invoice" and "choice" not in bound
    # an unknown value keeps the placeholder: the identity lookup finds nothing and refuses (never the first radio)
    assert DC.bind_step(step, {"zahlungsart": "Kreditkarte"})["command"]["selector"]["identifier"] == "${zahlungsart}"
    # without identifiers in the fingerprint the choice is bound by its title, as before
    _DC, _PR, rec0, proof0 = compile_web(with_ids=False)
    s0 = next(s for s in rec0["steps"] if "${zahlungsart}" in json.dumps(s))
    assert "choice" not in s0 and s0["command"]["selector"]["name"] == "${zahlungsart}" and proof0["passed"]


class LiveWorld:
    """The recorded app with its options RENAMED (English) and in another ORDER: the elements a live tree
    would show; the drift snapshot is the recorded one (the option resolution is what is tested here; the
    drift gate's own title comparison is a separate check)."""

    def __init__(self, PR, rec):
        self.w = PR.SimAXWorld(rec)
        self.w._present = lambda e: True
        radios = [e for e in self.w.elements if e.raw_role == "AXRadioButton"]
        others = [e for e in self.w.elements if e.raw_role != "AXRadioButton"]
        for e in radios:
            e.sel["name"] = PAY_EN.get(e.sel.get("name"), e.sel.get("name"))
        self.w.elements[:] = others + list(reversed(radios))


def test_a_draft_choice_replays_on_a_renamed_reordered_screen_by_identifier():
    import asyncio
    DC, PR, rec, _proof = compile_web()
    live = LiveWorld(PR, rec).w

    async def approve(_ctx):
        return True
    out = asyncio.run(DC.run_steps(rec, {"name": "Neu", "betrag": "9,99", "zahlungsart": "Rechnung"},
                                   executor=DC.checked_executor(live), approver=approve, decide_as_driving=True,
                                   on_step=live.at_step, hil_mode="off"))
    assert out["ok"], out.get("stopped")
    pressed = [e["target"] for e in live.effects if e["target"].startswith("radiobutton")]
    assert pressed == ["radiobutton:#pay-invoice"]
    # the same screen, the choice bound by title (no identifiers recorded): "Rechnung" is not on it
    _DC, PR0, rec0, _p0 = compile_web(with_ids=False)
    live0 = LiveWorld(PR0, rec0).w
    out0 = asyncio.run(DC.run_steps(rec0, {"name": "Neu", "betrag": "9,99", "zahlungsart": "Rechnung"},
                                    executor=DC.checked_executor(live0), approver=approve,
                                    decide_as_driving=True, on_step=live0.at_step, hil_mode="off"))
    assert not out0["ok"] and not [e for e in live0.effects if e["target"].startswith("radiobutton")]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
