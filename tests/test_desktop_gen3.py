"""Offline tests for the desktop generalisation:
state-conditioned probing (``execution/desktop_preconditions.py``), window
follow-ups (``execution/desktop_followup.py``), their mapping in
``execution/desktop_effects.py``, and the field_rw / property_update /
object_create additions in ``execution/desktop_contracts.py``. Fakes only.

Run: ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_gen3.py
"""
import copy
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, "wrong checkout (D9)"

from anyuse.execution import desktop_contracts as C  # noqa: E402
from anyuse.execution import desktop_effects as E  # noqa: E402
from anyuse.execution import desktop_followup as F  # noqa: E402
from anyuse.execution import desktop_hygiene as H  # noqa: E402
from anyuse.execution import desktop_preconditions as P  # noqa: E402

ID = {"bundle_id": "org.example.app", "window": None, "document": None}


def _item(path, ch="", mods=0, ident=None, mark=""):
    return {"index_path": list(path), "title_path": ["App", "M", f"t{path[-1]}"], "title": f"t{path[-1]}",
            "identifier": ident if ident is not None else f"_NS:{'_'.join(map(str, path))}",
            "cmd_char": ch, "cmd_mods": mods, "cmd_virtual_key": None, "enabled": True,
            "has_submenu": False, "mark_char": mark}


DUMP = {"bundle_id": "org.example.app", "window_controls": [
    {"role": "AXWindow", "subrole": "AXStandardWindow", "identifier": "w"},
    {"role": "AXOutline", "subrole": "", "identifier": "folders"},
    {"role": "AXTextArea", "subrole": "", "identifier": "body"}],
    "menu_items": [_item([1, 0], "q"),
                   _item([2, 0], "n"), _item([2, 1], "n", 1), _item([2, 2], "f"), _item([2, 3], "k"),
                   _item([2, 4], "g", 1), _item([2, 5], "d", 24), _item([2, 6], "f", 24),
                   _item([3, 0], "x"), _item([3, 1], "c")]}


def _win(sub="AXStandardWindow"):
    return {"role": "AXWindow", "subrole": sub, "identifier": "w", "document": None}


def _snap(containers=None, windows=None, sheets=0, focused=None, model=None, labels=None, selection="s0"):
    return {"windows": windows or [_win()], "sheets": sheets, "focused": focused,
            "containers": containers if containers is not None else {"AXOutline:folders": {"rows": 3}},
            "model": model, "text": None, "selection": selection, "marks": None, "bundle_id": "org.example.app",
            "labels": labels}


def _row(key, before, after, enabled=True, **kw):
    it = next(m for m in E.C._leaf_menu(DUMP) if m["key"] == key)
    r = {"key": key, "trigger": C._trigger_recipe(it), "kind": "shortcut", "run": None, "run_members": [],
         "enabled": enabled, "pressed_path": E.path_key(it["index_path"]), "before": before, "after": after}
    r.update(kw)
    return r


def _key(path):
    return next(m["key"] for m in E.C._leaf_menu(DUMP) if m["index_path"] == list(path))


# ── 1. state-conditioned probing ────────────────────────────────────────────

FACTS = {"containers": {"AXOutline:folders": {"rows": 3, "selected": 1, "selectable": True},     # navigation
                        "AXTable:files": {"rows": 7, "selected": 0, "selectable": True},
                        "AXList:": {"rows": 42, "selected": 0, "selectable": True, "unique": False},
                        "AXTable:empty": {"rows": 0, "selected": 0, "selectable": True}},
         "text_areas": {"AXTextArea:body": {"chars": 12, "selected": 0, "selectable": True},
                        "AXTextArea:blank": {"chars": 0, "selected": 0, "selectable": True}}}


def test_preconditions_are_derived_from_roles_and_state_only():
    toggle = _row(_key([3, 1]), _snap(), _snap(containers={"AXOutline:folders": {"rows": 3},
                                                           "AXList:notes": {"rows": 5}}))
    pcs = P.derive(FACTS, [toggle])
    kinds = [(p.kind, p.target) for p in pcs]
    # least invasive first; navigation (already selected), a shared key and
    # an empty container are never targets
    assert kinds == [("text_selected", "AXTextArea:body"), ("one_selected", "AXTable:files"),
                     ("pane_shown", _key([3, 1]))]
    pane = pcs[-1]
    assert pane.establish["adds"] == ["AXList:notes"] and pane.restore["trigger"]["key"] == _key([3, 1])
    # a combined precondition: a selection inside what the pane added
    now = {"containers": dict(FACTS["containers"], **{"AXList:notes": {"rows": 5, "selected": 0,
                                                                        "selectable": True}}),
           "text_areas": FACTS["text_areas"]}
    inner = P.derive_inside(pane, FACTS, now)
    assert [(p.kind, p.target) for p in inner] == [("one_selected", "AXList:notes")]
    assert inner[0].key == f"pane_shown:{_key([3, 1])}+one_selected:AXList:notes"
    assert [p.kind for p in inner[0].chain()] == ["pane_shown", "one_selected"]
    assert P.Precondition.from_dict(inner[0].to_dict()).key == inner[0].key
    # an object selected, then the text of the document it opened (depth 2);
    # never another selection inside a selection, never deeper
    sel = pcs[1]
    opened = {"containers": dict(FACTS["containers"], **{"AXTable:files": {"rows": 7, "selected": 1,
                                                                            "selectable": True}}),
              "text_areas": dict(FACTS["text_areas"], **{"AXTextArea:blank": {"chars": 30, "selected": 0,
                                                                               "selectable": True}})}
    assert [(p.kind, p.target) for p in P.derive_inside(sel, FACTS, opened)] == \
        [("text_selected", "AXTextArea:blank")]
    assert P.derive_inside(inner[0], FACTS, opened) == []
    assert P.derive_inside(pcs[0], FACTS, opened) == []                       # nothing inside a text selection
    # a toggle that opened a window is not a pane toggle
    win = _row(_key([3, 1]), _snap(), _snap(windows=[_win(), _win()], containers={"AXList:x": {"rows": 1}}))
    assert P.pane_toggles([win]) == []


def test_precondition_established_is_checked_from_fresh_facts():
    one = P.Precondition("one_selected", "AXTable:files", {"container": "AXTable:files", "row": 0})
    ok, _ = P.established(one, {"containers": {"AXTable:files": {"selected": 1}}})
    assert ok
    ok, why = P.established(one, {"containers": {"AXTable:files": {"selected": 2}}})
    assert not ok and "2 selected" in why
    assert P.established(one, None)[0] is False
    txt = P.Precondition("text_selected", "AXTextArea:body")
    assert P.established(txt, {"text_areas": {"AXTextArea:body": {"selected": 4}}})[0]
    pane = P.Precondition("pane_shown", "k", {"adds": ["AXList:notes"]})
    assert not P.established(pane, {"containers": {}})[0]
    assert P.clean_problems(one.to_dict(), {"containers": {"AXTable:files": {"selected": 0}}})
    assert P.clean_problems(None, {}) == []


def test_first_enabling_precondition_is_recorded_and_rows_merge():
    got = P.attribute(["a", "b", "c"], [("text_selected:x", ["a"]), ("one_selected:y", ["a", "b"])])
    assert got == {"a": "text_selected:x", "b": "one_selected:y"}          # c never enabled
    plain = [{"key": "a", "enabled": False}, {"key": "b", "enabled": True}, {"key": "c", "enabled": False}]
    cond = [{"key": "a", "enabled": True, "precondition": {"key": "text_selected:x"}}]
    merged = P.merge_rows(plain, cond)
    assert [r["key"] for r in merged] == ["a", "b", "c"]
    assert merged[0]["precondition"]["key"] == "text_selected:x" and merged[2]["enabled"] is False


def test_item_disabled_in_plain_start_maps_under_its_precondition():
    """Notes' New Note, disabled while the note list is hidden: probed with
    the pane shown, the list grows and the body takes focus -> object_create
    whose clean start declares the precondition."""
    k = _key([2, 0])
    pane = P.Precondition("pane_shown", _key([3, 1]), {"trigger": {"key": _key([3, 1])},
                                                       "adds": ["AXList:notes"]}).to_dict()
    before = _snap(containers={"AXOutline:folders": {"rows": 3}, "AXList:notes": {"rows": 2}},
                   model={"scopes": {"Notes": 2}})
    after = _snap(containers={"AXOutline:folders": {"rows": 3}, "AXList:notes": {"rows": 3}},
                  model={"scopes": {"Notes": 3}}, focused={"role": "AXTextArea", "subrole": "", "identifier": "b"})
    plain = [_row(k, _snap(), _snap(), enabled=False)]
    cond = [_row(k, before, after, precondition=pane, disabled_plain=True)]
    cands, counts = E.map_from_probes(DUMP, P.merge_rows(plain, cond))
    oc = [c for c in cands if c.contract == "object_create"]
    assert len(oc) == 1 and oc[0].recipe["precondition"]["key"] == pane["key"]
    assert counts["enabled_by_precondition"] == {k: pane["key"]}
    assert counts["probed_under_precondition"] == 1 and counts["candidates_under_precondition"] == 1
    # a count badge that grows is an object created, even with no new scope
    assert "object_created" in E.classify(_snap(model={"scopes": {"Notes": 2}}),
                                          _snap(model={"scopes": {"Notes": 3}}))["effects"]
    # still disabled under every precondition: reported, no class
    _c2, counts2 = E.map_from_probes(DUMP, [_row(k, _snap(), _snap(), enabled=False, precondition_tried=True)])
    assert counts2["per_item"][k].endswith("enabled by no derived precondition")


def test_a_conditioned_member_never_joins_a_plain_run():
    runrow = {"kind": "run", "run": "4/0", "run_members": ["a", "b", "c"]}
    plain = [dict(runrow, key="a", enabled=True), dict(runrow, key="b", enabled=True),
             dict(runrow, key="c", enabled=False)]
    cond = [dict(runrow, key="c", enabled=True, precondition={"key": "pane_shown:x"})]
    merged = P.merge_rows(plain, cond)
    c = next(r for r in merged if r["key"] == "c")
    assert c["kind"] == "shortcut" and c["run"] is None and c["run_alone"] == "4/0"
    # a run whose members were all disabled in the plain start stays a run
    # the mapping applies the same rule to rows merged before this rule existed
    dump = {"bundle_id": "x", "window_controls": [], "menu_items": [
        _item([4, 0], "1", mark="✓"), _item([4, 1], "2"), _item([4, 2], "3"), _item([4, 3], "")]}
    keys = [m["key"] for m in E.C._leaf_menu(dump)]
    paths = ["4/0", "4/1", "4/2", "4/3"]

    def mrow(i, b, a, **kw):
        it = E.C._leaf_menu(dump)[i]
        return dict({"key": keys[i], "trigger": C._trigger_recipe(it), "kind": "run", "run": "4/0",
                     "run_members": keys, "enabled": True, "pressed_path": paths[i],
                     "before": _snap(), "after": _snap()}, **kw, **{"before": dict(_snap(), marks=b),
                                                                    "after": dict(_snap(), marks=a)})
    rows = [mrow(1, {"4/0": 1}, {"4/1": 1}), mrow(2, {"4/1": 1}, {"4/2": 1}), mrow(0, {"4/2": 1}, {"4/0": 1}),
            mrow(3, {"4/0": 1}, {"4/0": 1, "4/3": 1}, precondition={"key": "pane_shown:x"})]
    cands, counts = E.map_from_probes(dump, rows)
    assert [c.contract for c in cands] == ["toggle_select"] and keys[3] not in cands[0].triggers
    assert "mark_flag" in counts["per_item"][keys[3]]
    plain2 = [dict(runrow, key=k, enabled=False) for k in "abc"]
    cond2 = [dict(runrow, key=k, enabled=True, precondition={"key": "text_selected:t"}) for k in "abc"]
    assert all(r["kind"] == "run" for r in P.merge_rows(plain2, cond2))


def test_run_members_needing_different_preconditions_are_not_provable():
    rows = [{"precondition": {"key": "a"}}, {"precondition": {"key": "b"}}]
    pc, why = P.candidate_precondition(rows)
    assert pc is None and "different preconditions" in why
    assert P.candidate_precondition([{}, {}]) == (None, None)
    assert P.candidate_precondition([{"precondition": {"key": "a"}}] * 2)[0]["key"] == "a"


def test_globe_key_system_functions_are_never_probed():
    plan, skipped = E.probe_plan(DUMP)
    keys = {p.key for p in plan}
    assert _key([2, 5]) not in keys and _key([2, 6]) not in keys
    assert "globe" in skipped[_key([2, 5])]


# ── 2. window follow-ups ────────────────────────────────────────────────────

def _facts(kind="window", fields=None, containers=(), default=False, auth=False):
    return {"kind": kind, "fields": fields or [], "default_button": default, "cancel_button": True,
            "close_button": kind == "window", "containers": list(containers), "auth": auth}


SEARCH_F = {"role": "AXTextField", "subrole": "AXSearchField", "identifier": "s", "focused": True, "settable": True}
NAME_F = {"role": "AXTextField", "subrole": "", "identifier": "n", "focused": True, "settable": True}
COMBO_F = {"role": "AXComboBox", "subrole": "", "identifier": "c", "focused": True, "settable": True}
FOLDER = {"kind": "folder", "items": ["a.txt"]}
SCOPES = {"scopes": {"Notes": 3}}


def test_followup_safety_rules():
    assert F.plan(_facts(fields=[SEARCH_F], containers=["AXOutline:side"]), FOLDER)[0] == "search"
    act, why = F.plan(_facts(fields=[SEARCH_F]), None)
    assert act is None and "network" in why                     # e.g. a dictionary lookup window
    assert F.plan(_facts(kind="sheet", fields=[SEARCH_F]), FOLDER)[0] is None
    act, why = F.plan(_facts(fields=[COMBO_F]), FOLDER)
    assert act is None and "combo box" in why                   # a server address: never filled
    assert F.plan(_facts(fields=[dict(NAME_F, role="AXTextArea")]), FOLDER)[0] is None
    assert F.plan(_facts(kind="sheet", fields=[NAME_F]), SCOPES)[0] == "name"
    act, why = F.plan(_facts(kind="sheet", fields=[NAME_F, dict(NAME_F, focused=False, identifier="t")]), FOLDER)
    assert act is None and "2 editable" in why                  # a save panel: name + tags
    assert F.plan(_facts(kind="sheet", fields=[NAME_F], containers=["AXBrowser:b"]), FOLDER)[0] is None
    act, why = F.plan(_facts(kind="sheet", fields=[NAME_F]), {"kind": "file", "sha": "x"})
    assert act is None and "no model" in why                    # a URL/link dialog in a document app
    act, why = F.plan(_facts(kind="dialog", fields=[dict(NAME_F, secure=True)]), FOLDER)
    assert act is None and "authentication" in why
    assert F.plan(_facts(fields=[dict(NAME_F, focused=False)]), FOLDER)[0] is None


def test_followup_classification_and_mapping():
    pre = {"windows": 2, "sheets": 0, "containers": {"AXList:res": []}, "model": FOLDER, "labels": []}
    post = dict(pre, containers={"AXList:res": ["kiwano_pie.txt"]})
    c = F.classify(pre, post, "search", "kiwano")
    assert c["effects"] == ["results_changed"]
    assert F.map_followup("search", c["effects"])[0] == "scoped_find"
    assert F.map_followup("search", [])[0] is None
    pre = {"windows": 1, "sheets": 1, "containers": {}, "model": SCOPES, "labels": ["Notes"]}
    post = {"windows": 1, "sheets": 0, "containers": {}, "model": {"scopes": {"Notes": 3, "gen3probe-4": 0}},
            "labels": ["Notes", "gen3probe-4"]}
    c = F.classify(pre, post, "name", "gen3probe-4")
    assert set(c["effects"]) == {"window_closed_on_confirm", "object_created", "object_named"}
    assert F.map_followup("name", c["effects"])[0] == "object_create"
    wrong = dict(post, model={"scopes": {"Notes": 3, "Neuer Ordner": 0}}, labels=["Notes", "Neuer Ordner"])
    c = F.classify(pre, wrong, "name", "gen3probe-4")
    assert F.map_followup("name", c["effects"])[0] is None      # created, but not with the committed name
    # a naming sheet whose confirm did not complete (e.g. "name in use") keeps
    # the value: that is NOT a field write
    kept = F.classify({"windows": 2, "sheets": 1, "containers": {}}, {"windows": 2, "sheets": 1, "containers": {},
                                                                      "field": "v"}, "name", "v")
    assert kept["effects"] == ["field_kept"] and F.map_followup("name", kept["effects"])[0] is None
    persisted = F.classify({"windows": 2, "sheets": 1, "containers": {}},
                           {"windows": 2, "sheets": 0, "containers": {}, "reopened_field": "v"}, "name", "v")
    assert F.map_followup("name", persisted["effects"])[0] == "field_rw"


def test_window_opening_items_map_through_their_followup():
    k_find, k_new, k_conn = _key([2, 2]), _key([2, 1]), _key([2, 3])
    before = _snap(model=FOLDER)
    two = _snap(windows=[_win(), _win()], model=FOLDER,
                focused={"role": "AXTextField", "subrole": "AXSearchField", "identifier": "s"})
    find = _row(k_find, before, two, followup={"action": "search", "why": "", "facts": _facts(fields=[SEARCH_F]),
                                               "effects": ["results_changed"]})
    sheet = _row(k_new, _snap(model=SCOPES), _snap(model=SCOPES, sheets=1),
                 followup={"action": "name", "why": "", "facts": _facts("sheet", [NAME_F], default=True),
                           "effects": ["window_closed_on_confirm", "object_created", "object_named"],
                           "grew": ["gen3probe-2"]})
    conn = _row(k_conn, before, two, followup={"action": None, "why": "combo box (remembered entries such as "
                                                                      "addresses): never filled",
                                               "facts": _facts(fields=[COMBO_F]), "effects": []})
    cands, counts = E.map_from_probes(DUMP, [find, sheet, conn])
    by = {c.triggers[0]: c for c in cands}
    assert by[k_find].contract == "scoped_find" and by[k_find].recipe["window"] == "new"
    assert by[k_new].contract == "object_create" and by[k_new].recipe["commit"] == "default_button"
    # the follow-up's ``grew`` are object names; they were
    # written as container KEYS and the proof then saw no object at all. The
    # recipe names no container and observes the object where the follow-up
    # saw it (a new scope of the model, here)
    assert by[k_new].recipe["containers"] == [] and by[k_new].recipe["observe"] == "scopes"
    seen = F.classify({"windows": 1, "sheets": 1, "containers": {}, "model": SCOPES, "labels": ["Notes"]},
                      {"windows": 1, "sheets": 0, "containers": {}, "model": {"scopes": {"Notes": 3, "gen3probe-2": 0}},
                       "labels": ["Notes", "gen3probe-2"]}, "name", "gen3probe-2")
    assert seen["seen_through"] == "scopes" and seen["grew"] == ["gen3probe-2"]
    named_only = F.classify({"windows": 1, "sheets": 1, "containers": {}, "labels": []},
                            {"windows": 1, "sheets": 0, "containers": {}, "labels": ["gen3probe-2"]}, "name",
                            "gen3probe-2")
    assert named_only["seen_through"] == "labels" and "object_named" in named_only["effects"]
    assert k_conn not in by and "combo box" in counts["per_item"][k_conn]
    assert counts["candidates_via_followup"] == 2 and counts["followups"] == 3
    # without a follow-up, a window-opening press still gets no class
    bare = _row(k_find, before, two)
    cands2, _ = E.map_from_probes(DUMP, [bare])
    assert cands2 == []


def test_inline_naming_seen_as_a_new_label_maps_object_create():
    """Calendar's New Event: an inline entry, no container grew; after the
    name is committed the name shows as a new label of the main window."""
    k = _key([2, 0])
    before = _snap(containers={"AXList:": {"rows": 3}}, labels=["Holiday"])
    after = _snap(containers={"AXList:": {"rows": 3}}, labels=["Holiday"],
                  focused={"role": "AXTextField", "subrole": "", "identifier": "QuickEventCell"})
    commit = _snap(containers={"AXList:": {"rows": 3}}, labels=["Holiday", "gen3probe-0"])
    row = _row(k, before, after, after_commit=commit, commit_value="gen3probe-0")
    cands, _ = E.map_from_probes(DUMP, [row])
    assert [c.contract for c in cands] == ["object_create"] and cands[0].recipe["observe"] == "labels"
    # a label equal to the value inside an editable field never counts
    row2 = _row(k, before, after, after_commit=_snap(containers={"AXList:": {"rows": 3}}, labels=["Holiday"]),
                commit_value="gen3probe-0")
    assert E.map_from_probes(DUMP, [row2])[0] == []


# ── 3. field_rw / property_update / object_create in the proof ──────────────

def _prove(contract, inputs, ui, start, recorded=("rec",), **kw):
    return C.prove_contract(contract, "cand", inputs, 3, ui, recorded_inputs=list(recorded), start_state=start,
                            observe_start=lambda i, r: start, **kw)


def test_field_rw_proves_only_a_field_that_reads_back_and_keeps_its_sentinel():
    start = {"identity": ID, "complete": True, "target": "seed", "sentinel": [["other", "abc"]]}
    good = _prove(C.FIELD_RW, ["gen3-quince 11", "gen3-sapote 29"], lambda i, r: dict(start, target=i), start)
    assert good.proved
    ro = _prove(C.FIELD_RW, ["gen3-quince 11", "gen3-sapote 29"], lambda i, r: dict(start), start)
    assert not ro.proved and "field reads 'seed'" in ro.rows[0].mismatch      # a read-only display
    clob = _prove(C.FIELD_RW, ["gen3-quince 11", "gen3-sapote 29"],
                  lambda i, r: dict(start, target=i, sentinel=[["other", i]]), start)
    assert not clob.proved and "sentinel changed" in clob.rows[0].mismatch    # wrote the wrong field too
    refused = _prove(C.FIELD_RW, ["gen3-quince 11", "gen3-sapote 29"],
                     lambda i, r: {"error": "2 controls carry the identifier", "refused": True}, start)
    assert not refused.proved


def test_property_update_inputs_come_from_the_control():
    rec, unseen, _ = C.slider_inputs("1.0", "11.0", "1.0")
    assert rec == 4.0 and unseen == [6.0, 9.0]
    rec, unseen, _ = C.slider_inputs(0.0, 1.0, 0.5)
    assert rec == 0.25 and unseen == [0.75, 0.375]      # 0.5 is the current value: skipped
    assert C.slider_inputs(5, 5, 5)[0] is None
    rec, unseen, _ = C.option_inputs(["a", "b", "c", "d"], "c")
    assert rec == "d" and unseen == ["a", "b"]
    assert C.option_inputs(["a", "b"], "a")[0] is None
    assert C.option_inputs(["a", "b", "c"], "x")[0] is None
    start = {"identity": ID, "complete": True, "target": {"value": 1.0}, "sentinel": [["p", "Native"]]}
    rep = _prove(C.PROPERTY_UPDATE, [6.0, 9.0], lambda i, r: dict(start, target={"value": i}), start, recorded=[4.0])
    assert rep.proved
    snapped = _prove(C.PROPERTY_UPDATE, [6.0, 9.0], lambda i, r: dict(start, target={"value": i + 0.5}), start,
                     recorded=[4.0])
    assert not snapped.proved


def test_object_create_counts_items_and_scoped_find_counts_rows():
    start = {"identity": ID, "complete": True, "target": {"names": ["a"], "count": 1}, "sentinel": None}
    one = dict(start, target={"names": ["a", "genquince-0"], "count": 2})
    assert C.OBJECT_CREATE.expect(start, "genquince", 0, one) is None
    two = dict(start, target={"names": ["a", "genquince-0"], "count": 3})     # two same-named objects
    assert "2 new items" in C.OBJECT_CREATE.expect(start, "genquince", 0, two)
    assert C.OBJECT_CREATE.predict(start, "genquince", 0)["target"]["count"] == 2
    legacy = {"identity": ID, "complete": True, "target": {"names": ["a"]}, "sentinel": None}
    assert "count" not in C.OBJECT_CREATE.predict(legacy, "g", 0)["target"]
    sf = C.scoped_find_contract("substring", E.pure_backend("substring"))
    s0 = {"identity": ID, "complete": True, "target": {"names": [], "scope": ["x_wine.txt", "y.txt"]},
          "sentinel": None}
    ok = dict(s0, target={"names": ["x_wine.txt"], "scope": ["x_wine.txt", "y.txt"], "rows": 1})
    assert sf.expect(s0, "wine", 0, ok) is None
    dup = dict(s0, target={"names": ["x_wine.txt"], "scope": ["x_wine.txt", "y.txt"], "rows": 2})
    assert "outside the scope" in sf.expect(s0, "wine", 0, dup)
    assert "outside the scope" in C.SCOPED_FIND.expect(s0, "wine", 0, dup)


def test_mapping_never_reads_the_bundle_id_prior():
    k = _key([2, 2])
    row = _row(k, _snap(model=FOLDER), _snap(windows=[_win(), _win()], model=FOLDER),
               followup={"action": "search", "why": "", "facts": _facts(fields=[SEARCH_F]),
                         "effects": ["results_changed"]})
    a, _ = E.map_from_probes(DUMP, [copy.deepcopy(row)])
    d2 = dict(DUMP, bundle_id="com.other.renamed")
    b, _ = E.map_from_probes(d2, [copy.deepcopy(row)])
    assert [(c.contract, c.triggers) for c in a] == [(c.contract, c.triggers) for c in b]


# ── harness fixes from the first proof run ─────────────────────────

def _fn_source(src, name, cls=None):
    """The source of a function (or of method ``name`` of class ``cls``) in a
    guest driver, read without importing it (it drives a GUI session)."""
    import ast
    tree = ast.parse(src)
    scope = tree.body
    if cls is not None:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    node = next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, node)


def _guest(name):
    return (_HERE / "execution" / "guest" / name).read_text()


def test_ax_values_compare_by_their_numbers_never_by_their_address():
    """FieldRW.window_ok and _ekey compared str(AXPosition) /
    str(AXSize). The text of an AXValue carries the wrapper's memory
    address, so the same frame read twice compared unequal whenever the
    address was not reused: all 7 field_rw/property_update workflows
    stopped at "main window ... is not the declared window" with identical
    frames. Fake AXValue texts with different addresses."""
    p1 = "<AXValue 0xbe9098e70> {value = x:0.000000 y:30.000000 type = kAXValueCGPointType}"
    p2 = "<AXValue 0xbe9c64630> {value = x:0.000000 y:30.000000 type = kAXValueCGPointType}"
    s1 = "<AXValue 0xbe9098e70> {value = w:1440.000000 h:797.000000 type = kAXValueCGSizeType}"
    s2 = "<AXValue 0x600003a1c0f0> {value = w:1440.000000 h:797.000000 type = kAXValueCGSizeType}"
    assert p1 != p2 and s1 != s2                          # what the old check compared
    assert H.ax_value_plain(p1) == H.ax_value_plain(p2) == ("point", 0.0, 30.0)
    assert H.ax_value_plain(s1) == H.ax_value_plain(s2) == ("size", 1440.0, 797.0)
    assert H.ax_value_plain(s1) != H.ax_value_plain(s1.replace("797.0", "796.0"))
    assert H.ax_value_plain("<AXValue 0x1> {value = x:1.5 y:-2 w:3 h:4 type = kAXValueCGRectType}") == \
        ("rect", 1.5, -2.0, 3.0, 4.0)
    assert H.ax_value_plain("<AXValue 0x2> {value = location:3 length:5 type = kAXValueCFRangeType}") == \
        ("range", 3.0, 5.0)
    # the window key FieldRW.window_ok builds, read at two starts
    assert ["_NS:34", H.ax_value_plain(p1), H.ax_value_plain(s1)] == \
        ["_NS:34", H.ax_value_plain(p2), H.ax_value_plain(s2)]
    # and an element key (_ekey) of one sheet read twice
    k1 = ("AXSheet", None, "_NS:87", None, H.ax_value_plain(p1), H.ax_value_plain(s1))
    k2 = ("AXSheet", None, "_NS:87", None, H.ax_value_plain(p2), H.ax_value_plain(s2))
    assert k1 == k2 and len({k1, k2}) == 1
    # anything else comes back without an address; None stays None
    assert "be9098e70" not in H.ax_value_plain("<AXUIElement 0xbe9098e70> {pid=3}")
    assert H.ax_value_plain(None) is None


def test_a_workflow_cancels_only_a_sheet_its_own_step_opened():
    """A sheet left by one workflow refused all 18 rows of it and of
    the next two. A reset may press Cancel on a sheet or dialog that this
    workflow's own step opened; never an authentication prompt, never one
    that was there when the workflow began, never one without Cancel
    (TextEdit's autosave-conflict sheet has none: it stays a refusal)."""
    key = ("AXSheet", None, "_NS:87", None, ("point", 0.0, 52.0), ("size", 480.0, 170.0))
    own = {"key": key, "auth": False, "cancel_button": True}
    assert H.own_surface_action(own, foreign=[]) == "cancel"
    assert H.own_surface_action(own, foreign=[key]) == "leave"
    assert H.own_surface_action(dict(own, cancel_button=False), foreign=[]) == "leave"
    assert H.own_surface_action(dict(own, auth=True), foreign=[]) == "never"
    assert H.own_surface_action(dict(own, auth=True, cancel_button=False), foreign=[key]) == "never"


NOT_A_TOGGLE = ("object_created", "object_removed", "object_created_on_commit", "document_changed", "doc_saved",
                "window_opened", "dialog_opened", "auth_required", "entry_focused", "search_focused")


def _lrow(ident, before, after, effects, kind="shortcut", run=None, pc=None, wins=(1, 1)):
    r = {"key": f"k:{ident}", "trigger": {"key": f"k:{ident}", "identifier": ident}, "kind": kind, "run": run,
         "before": {"containers": {k: {"rows": 1} for k in before}, "windows": [{}] * wins[0]},
         "after": {"containers": {k: {"rows": 1} for k in after}, "windows": [{}] * wins[1]},
         "effects": list(effects)}
    if pc:
        r["precondition"] = {"key": pc}
    return r


#: Finder's recorded layout changes in the measuring probe
FINDER_ROWS = [
    _lrow("icons", ["AXList:GalleryView"], ["AXList:IconView"], ["layout_changed", "mark_other"], "run", "4/0"),
    _lrow("list", ["AXList:IconView"], ["AXOutline:ListView"], ["layout_changed", "mark_exclusive"], "run", "4/0"),
    _lrow("columns", ["AXOutline:ListView"], ["AXBrowser:ColumnView"], ["layout_changed", "mark_exclusive"],
          "run", "4/0"),
    _lrow("gallery", ["AXBrowser:ColumnView"], ["AXList:GalleryView"], ["layout_changed", "mark_other"], "run", "4/0"),
    _lrow("sidebar", ["AXList:GalleryView", "AXOutline:_NS:8"], ["AXList:GalleryView"],
          ["layout_changed", "selection_changed"], pc="pane_shown:id:toolbar"),
    _lrow("toolbar", ["AXList:GalleryView"], ["AXList:GalleryView", "AXOutline:_NS:8"],
          ["layout_changed", "selection_changed"]),
    _lrow("back", ["AXList:GalleryView", "AXOutline:_NS:8"], ["AXList:IconView", "AXOutline:_NS:8"],
          ["layout_changed", "focus_changed", "selection_changed"],
          pc="pane_shown:id:toolbar+one_selected:AXOutline:_NS:8"),
    _lrow("next-window", ["AXList:GalleryView"], [], ["layout_changed", "focus_changed"]),
    _lrow("new-window", ["AXList:GalleryView"], ["AXList:GalleryView", "AXOutline:_NS:8"],
          ["window_opened", "selection_changed"], wins=(1, 2)),
]


def test_layout_restore_follows_the_recorded_transitions_and_never_presses_back():
    """The old restore pressed every recorded layout changer in
    turn and pressed one AGAIN when it did not help. A view-mode radio item
    pressed twice stays set, and Finder's 'Back' and 'Next Window' passed
    the toggle filter: 10 of the 21 Finder restores logged in one proof run ended
    with no container at all, and the next reset opened in gallery view.
    The planner presses only
    what the recorded transitions predict to reach the reference, each
    once."""
    T = H.layout_toggles(FINDER_ROWS, NOT_A_TOGGLE)
    assert [t["trigger"]["identifier"] for t in T] == ["icons", "list", "columns", "gallery", "sidebar", "toolbar"]
    ref = ["AXBrowser:ColumnView"]

    def plan(cur):
        return [t["trigger"]["identifier"] for t in H.plan_layout(cur, ref, T)[0]]
    assert plan(["AXBrowser:ColumnView", "AXOutline:_NS:8"]) == ["sidebar"]
    assert plan(["AXList:GalleryView"]) == ["columns"]
    assert plan(["AXOutline:ListView", "AXOutline:_NS:8"]) == ["columns", "sidebar"]
    assert plan(ref) == []
    assert plan(["AXBrowser:ColumnView", "AXTable:x"]) == []          # nothing recorded helps: nothing pressed
    groups = {"4/0": {"AXList:GalleryView", "AXList:IconView", "AXOutline:ListView", "AXBrowser:ColumnView"}}
    by = {t["trigger"]["identifier"]: t for t in T}
    # a radio item sets its view whatever was shown; pressed again it changes nothing
    assert H.predict_layout(["AXBrowser:ColumnView"], by["columns"], groups) == frozenset(ref)
    assert H.predict_layout(["AXList:GalleryView", "AXOutline:_NS:8"], by["columns"], groups) == \
        frozenset(["AXBrowser:ColumnView", "AXOutline:_NS:8"])
    # a self-inverse toggle, forward and from the other side
    assert H.predict_layout(["AXBrowser:ColumnView"], by["toolbar"], groups) == \
        frozenset(["AXBrowser:ColumnView", "AXOutline:_NS:8"])
    assert H.predict_layout(["AXBrowser:ColumnView", "AXOutline:_NS:8"], by["toolbar"], groups) == frozenset(ref)
    # normalised keys (Notes' grid is _asList or _asGallery by view mode)
    norm = lambda k: k.rsplit("_as", 1)[0]                              # noqa: E731
    notes = [_lrow("gal", ["AXOutline:f"], ["AXList:grid_asGallery", "AXOutline:f"], ["layout_changed"])]
    Tn = H.layout_toggles(notes, NOT_A_TOGGLE)
    assert [t["trigger"]["identifier"] for t in
            H.plan_layout(["AXOutline:f"], ["AXList:grid_asList", "AXOutline:f"], Tn, norm)[0]] == ["gal"]


# ── 5. the element a trigger focused (desktop_effects.read_trigger_focus) ────────────────────

class _Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def now(self):
        return self.t


def _reader(focused, *, pid=41, front=41, owner=None, pre=None, role="AXTextField", subrole="",
            window=None, pending=None, clock=None):
    """``read_trigger_focus`` over fakes: elements are strings, ``window`` the focused element's
    window facts; every read the reader asks for is logged (``asked``)."""
    asked = []
    clock = clock or _Clock()

    def describe(el):
        asked.append(("describe", el))
        return {"role": role, "subrole": subrole}

    def window_of(el):
        asked.append(("window", el))
        return window if window is not None else {"document": "/Users/probe/Gen3/Probe Notiz.txt",
                                                  "subrole": "AXStandardWindow", "sheet": False}
    out = E.read_trigger_focus(pid, pre, bundle_id="com.apple.finder", front_pid=lambda: front,
                               focused=lambda p: focused if p == pid else "the wrong app's element",
                               pid_of=lambda el: owner if owner is not None else pid, describe=describe,
                               window_of=window_of, same=lambda a, b: a == b, input_pending=pending,
                               sleep=clock.sleep, clock=clock.now)
    return out, asked, clock


def test_the_trigger_focus_is_read_from_the_probed_apps_pid_and_holds_no_value_or_title():
    out, asked, clock = _reader("name field", pre="search field")
    assert out == {"read": True, "role": "AXTextField", "subrole": "", "canonical_role": "textfield",
                   "app": "com.apple.finder", "document": out["document"], "window": "AXStandardWindow",
                   "moved": True, "text_entry": True, "secure": False}
    assert out["document"].startswith("sha256:") and len(out["document"]) == 7 + 16
    assert "Probe" not in str(out) and "/Users" not in str(out)
    assert clock.slept[0] == E.FOCUS_SETTLE_S                       # the watcher's 0.3 s
    assert asked == [("describe", "name field"), ("window", "name field")]
    # another app in front, or a focus that belongs to another process: not read at all
    out, asked, _c = _reader("name field", front=99)
    assert out == {"read": False, "why": "another app is in front after the trigger"} and asked == []
    out, asked, _c = _reader("name field", owner=99)
    assert not out["read"] and "another process" in out["why"] and asked == []
    out, _a, _c = _reader(None)
    assert not out["read"] and "no element" in out["why"]
    # the focus did not move: read, not moved; a password field: no role for a focused hop
    out, _a, _c = _reader("search field", pre="search field")
    assert out["read"] and out["moved"] is False and E.focused_target({"focus": out, "trigger": {}}) is None
    out, _a, _c = _reader("pin", role="AXTextField", subrole="AXSecureTextField", pre="search field")
    assert out["secure"] and not out["text_entry"] and out["canonical_role"] == ""
    assert E.focused_target({"focus": out, "trigger": {}}) is None
    # a search field (AXTextField + AXSearchField) is a textfield to the runner; a sheet's field
    out, _a, _c = _reader("find", subrole="AXSearchField", window={"document": None, "subrole": "AXStandardWindow",
                                                                    "sheet": True})
    assert (out["canonical_role"], out["document"], out["window"]) == ("textfield", "", "sheet")


def test_the_trigger_focus_waits_while_input_is_queued_and_gives_up_unread():
    queue = [True, True, False]
    out, _a, clock = _reader("name field", pending=lambda: queue.pop(0))
    assert out["read"] and clock.slept == [E.FOCUS_SETTLE_S, E.FOCUS_POLL_S, E.FOCUS_POLL_S]
    out, asked, clock = _reader("name field", pending=lambda: True)
    assert out == {"read": False, "why": f"input still queued {E.FOCUS_WAIT_S:.1f} s after the trigger"}
    assert asked == [] and sum(clock.slept) >= E.FOCUS_SETTLE_S + E.FOCUS_WAIT_S


def test_an_inline_create_maps_with_the_focused_target_the_map_projection_reads():
    """Finder's "Neuer Ordner" (an inline create): the row's ``focus`` becomes the candidate's
    ``focus``, the shape the map projection turns into a ``focused`` hop; an older row
    (no ``focus``) maps exactly as before."""
    k = _key([2, 0])
    before = _snap(containers={"AXOutline:folders": {"rows": 3}})
    after = _snap(containers={"AXOutline:folders": {"rows": 4}},
                  focused={"role": "AXTextField", "subrole": "", "identifier": ""})
    focus, _a, _c = _reader("name field", pre="list")
    row = _row(k, before, after, focus=focus)
    cands, _ = E.map_from_probes(DUMP, [row])
    assert [c.contract for c in cands] == ["object_create"]
    assert cands[0].recipe["focus"] == {"kind": "focused", "canonical_role": "textfield", "raw_role": "AXTextField",
                                        "app": "com.apple.finder", "document": focus["document"],
                                        "window": "AXStandardWindow", "trigger": row["trigger"]}
    old, _ = E.map_from_probes(DUMP, [_row(k, before, after)])
    assert "focus" not in old[0].recipe and {**old[0].recipe, "focus": focus} != cands[0].recipe
    assert {kk: v for kk, v in cands[0].recipe.items() if kk != "focus"} == old[0].recipe
    # a search field focused in place (scoped_find) carries it too
    k2 = _key([2, 2])
    after2 = _snap(focused={"role": "AXTextField", "subrole": "AXSearchField", "identifier": "search"})
    f2, _a, _c = _reader("find", subrole="AXSearchField")
    sf, _ = E.map_from_probes(DUMP, [_row(k2, _snap(), after2, focus=f2)])
    assert sf[0].contract == "scoped_find" and sf[0].recipe["focus"]["canonical_role"] == "textfield"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    for t in TESTS:
        t()
        print("ok", t.__name__)
    print("\nDESKTOP GEN3 TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
