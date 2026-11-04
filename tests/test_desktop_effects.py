"""Offline tests for the desktop generalisation:
``execution/desktop_effects.py`` (effect probe, scoped-find semantics,
stable model reading) and the contract additions in
``execution/desktop_contracts.py`` (flag_toggle, declared coupled settings,
model-based preserved state, semantics-aware scoped_find).

Run: ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_effects.py
"""
import copy
import gzip
import json
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

DUMPS = _HERE / "testdata" / "discovery"
ID = {"bundle_id": "org.example.app", "window": None, "document": None}


def _dump(bid):
    with gzip.open(DUMPS / f"dump_{bid}.json.gz") as fh:
        return json.load(fh)


def _obs(target, sentinel=None, model=None):
    o = {"identity": dict(ID), "complete": True, "target": target, "sentinel": sentinel}
    if model is not None:
        o["model"] = model
    return o


def _item(path, ch="", mods=0, ident=None, mark="", vkey=None):
    return {"index_path": list(path), "title_path": ["App", "M", f"t{path[-1]}"], "title": f"t{path[-1]}",
            "identifier": ident if ident is not None else f"_NS:{'_'.join(map(str, path))}",
            "cmd_char": ch, "cmd_mods": mods, "cmd_virtual_key": vkey, "enabled": True,
            "has_submenu": False, "mark_char": mark}


# a synthetic app: File menu (2), View menu (3) with a radio group, a flag group
# and a coupled sort group
DUMP = {"bundle_id": "org.example.app", "window_controls": [
    {"role": "AXWindow", "subrole": "AXStandardWindow", "identifier": "w"},
    {"role": "AXOutline", "subrole": "", "identifier": "list"},
    {"role": "AXTextArea", "subrole": "", "identifier": "body"}],
    "menu_items": [
        _item([1, 0], "q"), _item([1, 1], ","),
        _item([2, 0], "n"), _item([2, 1], "n", 1), _item([2, 2], "s"), _item([2, 3], "f"),
        _item([2, 4], "w"), _item([2, 5], "\x08", 0, vkey=51), _item([2, 6], "", ident=""),
        _item([2, 7], "", ident="plainItem:"),
        _item([3, 0], "1", mark="✓"), _item([3, 1], "2"), _item([3, 2], "3"), _item([3, 3], "4"),
        {"index_path": [3, 4], "title_path": ["App", "M", ""], "title": "", "identifier": "", "cmd_char": "",
         "cmd_mods": 0, "cmd_virtual_key": None, "enabled": False, "has_submenu": False, "mark_char": ""},
        _item([3, 5], mark="✓"), _item([3, 6]), _item([3, 7]), _item([3, 8]),
        {"index_path": [3, 9], "title_path": ["App", "M", ""], "title": "", "identifier": "", "cmd_char": "",
         "cmd_mods": 0, "cmd_virtual_key": None, "enabled": False, "has_submenu": False, "mark_char": ""},
        _item([3, 10], mark="✓"), _item([3, 11]), _item([3, 12])]}


def test_probe_plan_excludes_by_convention_and_identity_never_by_title():
    plan, skipped = E.probe_plan(DUMP)
    kinds = {p.trigger["index_path"][0:2] and tuple(p.trigger["index_path"]): p.kind for p in plan}
    assert (1, 0) not in kinds and (1, 1) not in kinds          # application menu
    assert (2, 4) not in kinds and (2, 5) not in kinds          # ⌘W, delete key
    assert kinds[(2, 0)] == kinds[(2, 1)] == "shortcut"
    assert all(kinds[(3, i)] == "run" for i in (0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12))
    assert any("no key equivalent" in v for v in skipped.values())           # plainItem:
    d3 = copy.deepcopy(DUMP)
    d3["menu_items"].append(_item([2, 8], "", ident="plainItem:"))          # a second holder of the id
    _pl, sk3 = E.probe_plan(d3)
    assert sum("identity shared" in v for v in sk3.values()) == 2
    # renaming every title changes nothing
    d2 = copy.deepcopy(DUMP)
    for it in d2["menu_items"]:
        if it["title"]:                     # separators stay separators
            it["title"] = "renamed"
            it["title_path"] = ["X", "Y", "renamed"]
    plan2, _ = E.probe_plan(d2)
    assert [p.trigger for p in plan] == [p.trigger for p in plan2]
    # real dumps: nothing in the application menu, no ⌘Q/⌘W/⌘P/delete
    for bid in ("com.apple.TextEdit", "com.apple.Notes", "com.apple.finder"):
        pl, sk = E.probe_plan(_dump(bid))
        assert pl and not any(p.trigger["index_path"][0] == 1 for p in pl), bid
        assert not any(p.trigger["cmd_char"] in ("q", "w", "p") and p.trigger["cmd_mods"] == "0" for p in pl)
        print(f"    {bid}: {len(pl)} probed, {len(sk)} not probed")
    print("OK  probe plan: app menu and quit/close/print/delete keys excluded; identity-only; titles irrelevant")


def _snap(**kw):
    s = {"windows": [{"role": "AXWindow", "subrole": "AXStandardWindow", "identifier": "w", "document": "/d"}],
         "sheets": 0, "focused": {"role": "AXOutline", "subrole": "", "identifier": "list"},
         "containers": {"AXOutline:list": {"rows": 7}}, "model": {"kind": "folder", "items": ["a", "b"]},
         "text": "t0", "selection": "s0", "marks": {}, "enabled": True}
    s.update(kw)
    return s


def _probe(key, path, before, after, kind="shortcut", run=None, members=(), enabled=True):
    it = next(m for m in DUMP["menu_items"] if m["index_path"] == path)
    return {"key": next(c for c in C._leaf_menu(DUMP) if c["index_path"] == path)["key"],
            "trigger": C._trigger_recipe({**it, "key": key}), "kind": kind, "run": run,
            "run_members": list(members), "enabled": enabled, "pressed_path": E.path_key(path),
            "before": before, "after": after}


def test_effect_classification_and_mapping():
    base = _snap()
    win = _snap(windows=base["windows"] + [{"role": "AXWindow", "subrole": "AXStandardWindow",
                                             "identifier": "w2", "document": None}])
    created = _snap(model={"kind": "folder", "items": ["a", "b", "untitled folder"]},
                    containers={"AXOutline:list": {"rows": 8}},
                    focused={"role": "AXTextField", "subrole": "", "identifier": ""})
    search = _snap(focused={"role": "AXTextField", "subrole": "AXSearchField", "identifier": "q"})
    saved_b = _snap(model={"kind": "file", "sha": "1"}, text="dirty")
    saved_a = _snap(model={"kind": "file", "sha": "2"}, text="dirty")
    # a non-window element in AXWindows (Finder's inline rename field) is not a window
    created = dict(created, windows=created["windows"] + [{"role": "AXTextField", "subrole": None,
                                                            "identifier": "rename", "document": None}])
    # rows that grew while the folder did not: a view change, not an object
    viewch = _snap(containers={"AXOutline:list": {"rows": 30}})
    assert E.classify(base, viewch)["effects"] == ["view_changed"]
    pane = _snap(containers={"AXOutline:list": {"rows": 7}, "AXTable:notes": {"rows": 3}})
    assert "layout_changed" in E.classify(base, pane)["effects"]
    probes = [_probe("n", [2, 0], base, win), _probe("N", [2, 1], base, created),
              _probe("s", [2, 2], saved_b, saved_a), _probe("f", [2, 3], base, search)]
    # radio group 3/0..3/3; sort group 3/5..3/8 is coupled to it; flag group 3/10..3/12
    radio = [E.path_key([3, i]) for i in range(4)]
    sort_ = [E.path_key([3, i]) for i in (5, 6, 7, 8)]
    flags = [E.path_key([3, i]) for i in (10, 11, 12)]

    def marks(on):
        return {p: (p in on) for p in radio + sort_ + flags}
    keys = {tuple(m["index_path"]): m["key"] for m in C._leaf_menu(DUMP)}
    rk = [keys[(3, i)] for i in range(4)]
    fk = [keys[(3, i)] for i in (10, 11, 12)]
    start_on = {radio[0], sort_[0], flags[0]}
    for i in (1, 2, 3):
        after_on = (start_on - {radio[0]}) | {radio[i]}
        if i == 2:
            after_on = (after_on - {sort_[0]}) | {sort_[2]}      # coupled: the sort mark moves
        probes.append(_probe(rk[i], [3, i], _snap(marks=marks(start_on)), _snap(marks=marks(after_on)),
                             "run", radio[0], rk))
    for i, fp in enumerate(flags):
        after_on = start_on ^ {fp}
        probes.append(_probe(fk[i], [3, (10, 11, 12)[i]], _snap(marks=marks(start_on)),
                             _snap(marks=marks(after_on)), "run", flags[0], fk))
    probes.append(_probe("disabled", [2, 6] if False else [2, 7], base, base, enabled=False))
    sheet = _snap(sheets=1, focused={"role": "AXTextField", "subrole": "", "identifier": "name"})
    made = _snap(model={"kind": "folder", "items": ["a", "b", "gen2probe-0"]})
    p_sheet = _probe("n", [2, 0], base, sheet)
    p_sheet["after_commit"] = made
    c_sheet, n_sheet = E.map_from_probes(DUMP, [p_sheet])
    assert [c.contract for c in c_sheet] == ["object_create"] and c_sheet[0].recipe["naming"] == "sheet"
    p_sheet["after_commit"] = sheet                  # the name went nowhere: a dialog, no class
    c_sheet, n_sheet = E.map_from_probes(DUMP, [p_sheet])
    assert c_sheet == [] and n_sheet["per_item"][p_sheet["key"]] == "opens a dialog or sheet"
    moved = _snap(model={"kind": "folder", "items": ["b", "new folder"]},
                  focused={"role": "AXTextField", "subrole": "", "identifier": ""})
    c_mv, n_mv = E.map_from_probes(DUMP, [_probe("N", [2, 1], base, moved)])
    assert c_mv == [] and "removes or moves" in list(n_mv["per_item"].values())[0]
    cands, counts = E.map_from_probes(DUMP, probes)
    by = {}
    for c in cands:
        by.setdefault(c.contract, []).append(c)
    assert [c.recipe["trigger"]["cmd_mods"] for c in by["object_create"]] == ["1"]     # ⇧⌘N only
    assert counts["per_item"][keys[(2, 0)]] == "opens a window"                     # ⌘N: a window
    assert len(by["doc_save"]) == 1 and len(by["scoped_find"]) == 1
    assert by["object_create"][0].recipe["containers"] == ["AXOutline:list"]
    auto = [dict(p, control_saved=True) if p["pressed_path"] == "2/2" else p for p in probes]
    c2, n2 = E.map_from_probes(DUMP, auto)
    assert not [c for c in c2 if c.contract == "doc_save"] and "autosave" in n2["per_item"][keys[(2, 2)]]
    dict_ = [dict(p, before=dict(p["before"], bundle_id="org.example.app"),
                  after=dict(p["after"], frontmost_during_press=["org.example.app", "com.apple.dictation"],
                            frontmost_source="ax_system_wide"))
             if p["pressed_path"] == "2/2" else p for p in probes]
    c3, n3 = E.map_from_probes(DUMP, dict_)
    assert not [c for c in c3 if c.contract == "doc_save"] and "deactivation" in n3["per_item"][keys[(2, 2)]]
    assert by["scoped_find"][0].recipe["semantics"] is None
    ts = by["toggle_select"][0]
    assert ts.triggers == rk[1:] and ts.provable
    # only 3/5 and 3/7 were seen moving; the whole coupled group 3/5..3/8 is declared
    assert ts.recipe["side_effects"] == sorted({E.identity_string(m) for m in DUMP["menu_items"]
                                                if m["index_path"] in ([3, 5], [3, 6], [3, 7], [3, 8])})
    sm = {"menu_items": [{"index_path": [3, 12], "identifier": "sortmenu", "cmd_char": "", "cmd_mods": 8},
                         {"index_path": [3, 12, 1], "identifier": "kind", "cmd_char": "2", "cmd_mods": 6}]}
    assert E.declare_coupled(sm, {"3/12/1": sm["menu_items"][1]}, ["3/12/1"]) == ["sortmenu||8>*"]
    by_path = {E.path_key(p): {} for p in ([3, 12, 0], [3, 12, 2], [3, 12, 3], [3, 13], [3, 14])}
    assert E.sibling_run(by_path, "3/12/3") == ["3/12/0", "3/12/2", "3/12/3"]      # whole submenu
    assert E.sibling_run(by_path, "3/13") == ["3/13", "3/14"]                       # top-level run
    ft = by["flag_toggle"][0]
    assert ft.triggers == fk and ft.provable and ft.recipe["side_effects"] == []
    assert counts["per_item"][keys[(2, 7)]] == "disabled in the probe start"
    assert counts["triggers_keyed_by_position"] == 0
    print("OK  effects: ⌘N opens a window (no class), ⇧⌘N creates+names (object_create), ⌘S saves, "
          "⌘F focuses a search field; exclusive marks -> toggle_select with the coupled sort group "
          "declared; single-mark flips -> flag_toggle; disabled -> unmapped")


def test_checkbox_group_is_never_an_option_group():
    flags = [E.path_key([3, i]) for i in (10, 11, 12)]
    keys = {tuple(m["index_path"]): m["key"] for m in C._leaf_menu(DUMP)}
    fk = [keys[(3, i)] for i in (10, 11, 12)]
    start = {flags[0]}
    probes = []
    for i, fp in enumerate(flags):
        b = {p: (p in start) for p in flags}
        a = {p: (p in (start ^ {fp})) for p in flags}
        probes.append(_probe(fk[i], [3, (10, 11, 12)[i]], _snap(marks=b), _snap(marks=a), "run", flags[0], fk))
    cands, _ = E.map_from_probes(DUMP, probes)
    assert [c.contract for c in cands] == ["flag_toggle"]
    # mixed behaviour: one exclusive, one flag -> no class at all
    b = {p: (p in start) for p in flags}
    probes[1]["after"] = _snap(marks={flags[0]: False, flags[1]: True, flags[2]: False})
    cands, counts = E.map_from_probes(DUMP, probes)
    assert cands == [] and "mixed" in counts["per_item"][fk[0]]
    print("OK  a checkbox group maps to flag_toggle, never toggle_select; mixed behaviour maps to nothing")


def test_scoped_find_semantics_calibration():
    for sem in E.SEMANTICS:
        obs = {p: E.expected_calibration(sem, p) for p in E.CALIBRATION_PATTERNS}
        got, why = E.classify_semantics(obs)
        assert got == sem, (sem, why)
    got, why = E.classify_semantics({"amber": ["amberly.txt"], "quartz": []})
    assert got is None and "no" in why
    assert E.match_token_prefix(["blueberry_jam.txt", "kiwano_pie.txt", "rambutan_pie.txt"], "pie") == \
        ["kiwano_pie.txt", "rambutan_pie.txt"]
    assert E.match_token_prefix(["blueberry_jam.txt", "elderberry_wine.txt"], "berry") == []
    assert E.match_substring(["blueberry_jam.txt", "elderberry_wine.txt"], "berry") == \
        ["blueberry_jam.txt", "elderberry_wine.txt"]
    assert E.pure_backend("spotlight") is None
    print("OK  calibration tells substring / token_prefix / spotlight apart; unknown results -> refused")


def test_scoped_find_compares_with_the_same_semantics_only():
    scope = ["blueberry_jam.txt", "elderberry_wine.txt", "kiwano_pie.txt", "rambutan_pie.txt"]
    start = _obs({"names": [], "scope": scope})

    def token_app(p, r):
        return _obs({"names": E.match_token_prefix(scope, p), "scope": scope})
    # same rule on both sides: proves, including the mid-word pattern (empty is the rule's answer)
    c = C.scoped_find_contract("token_prefix", E.match_token_prefix)
    rep = C.prove_contract(c, "fake", ["pie", "berry"], 3, token_app, recorded_inputs=["jam"], start_state=start)
    assert rep.proved, rep.to_dict()
    # a different rule (substring) on the direct side: berry still fails, never loosened
    c2 = C.scoped_find_contract("substring", E.match_substring)
    rep = C.prove_contract(c2, "fake", ["pie", "berry"], 3, token_app, recorded_inputs=["jam"], start_state=start)
    assert not rep.proved and {r.input for r in rep.rows if not r.agree} == {"berry"}
    # spotlight: no pure backend; the direct side is the system index. An
    # index that also misses a file agrees; one that lists a file the app
    # did not show disagrees
    c3 = C.scoped_find_contract("spotlight")
    index = lambda p, r: _obs({"names": E.match_token_prefix(scope, p), "scope": scope})  # noqa: E731
    shown = lambda p, r: _obs({"names": E.match_token_prefix(scope, p)[:1], "scope": scope})  # noqa: E731
    rep = C.prove_contract(c3, "fake", ["pie", "berry"], 3, token_app, direct_fn=index,
                           recorded_inputs=["jam"], start_state=start)
    assert rep.proved
    rep = C.prove_contract(c3, "fake", ["pie", "berry"], 3, shown, direct_fn=index,
                           recorded_inputs=["jam"], start_state=start)
    assert not rep.proved and {r.input for r in rep.rows if not r.agree} == {"pie"}
    print("OK  scoped_find: same-rule backend proves; a different rule or a missing file still fails")


def test_declared_coupled_settings():
    c = C.CONTRACTS["toggle_select"]
    start = _obs({"options": ["a", "b", "c"], "selected": ["a"]}, sentinel=["sort|1|0", "other|x|0"])

    def ui(i, r, sort="sort|3|0", extra=()):
        return _obs({"options": ["a", "b", "c"], "selected": [i]}, sentinel=sorted([sort, "other|x|0", *extra]))
    kw = dict(recorded_inputs=["a"], start_state=start)
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, ui, **kw)
    assert not rep.proved and all("sentinel changed" in r.mismatch for r in rep.rows)
    side = ["sort|1|0", "sort|2|0", "sort|3|0"]
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, ui, side_effects=side, **kw)
    assert rep.proved, rep.to_dict()
    und = lambda i, r: ui(i, r, extra=("dock|d|0",))  # noqa: E731
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, und, side_effects=side, **kw)
    assert not rep.proved and all("undeclared side effect" in r.mismatch and "dock" in r.mismatch
                                  for r in rep.rows)
    gone = lambda i, r: _obs({"options": ["a", "b", "c"], "selected": [i]}, sentinel=["sort|3|0"])  # noqa: E731
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, gone, side_effects=side, **kw)
    assert not rep.proved and all("other|x|0" in r.mismatch for r in rep.rows)
    # a submenu declaration covers items the submenu shows only in some
    # states (Finder's sort submenu in list view)
    start2 = _obs({"options": ["a", "b", "c"], "selected": ["a"]}, sentinel=["sortmenu|x|0>none|0|6"])

    def ui2(i, r):
        return _obs({"options": ["a", "b", "c"], "selected": [i]}, sentinel=["sortmenu|x|0>kind|2|6"])
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, ui2, recorded_inputs=["a"], start_state=start2,
                           side_effects=["sortmenu|x|0>*"])
    assert rep.proved, rep.to_dict()
    rep = C.prove_contract(c, "fake", ["b", "c"], 3, ui2, recorded_inputs=["a"], start_state=start2,
                           side_effects=["othermenu|y|0>*"])
    assert not rep.proved and all("undeclared side effect" in r.mismatch for r in rep.rows)
    print("OK  coupled settings: a declared coupled change proves; an undeclared one is still a wrong target")


def test_preserved_state_through_the_model_not_the_viewport():
    """Observed: the new folder got selected, the starting notes
    left the list, and the view-based preserved check failed. The model
    (every folder with its note count) is unchanged, so it passes; a folder
    that lost a note fails."""
    scopes = {"Notes": 129, "All on My Mac": 131, "genrec-0": 2}
    start = _obs({"names": ["Notes", "genrec-0", "Einkauf 0", "Einkauf 1"]}, model={"scopes": scopes})

    def ui(i, r, lose=False):
        s = dict(scopes, **{C.object_name(i, r): 0})
        if lose:
            s["Notes"] = 128
        return _obs({"names": ["Notes", "genrec-0", C.object_name(i, r)]}, model={"scopes": s})
    c = C.CONTRACTS["object_create"]
    assert c.preserve(start, "g", 7, ui("g", 7)) is None
    assert "lost objects" in c.preserve(start, "g", 7, ui("g", 7, lose=True))
    s2 = dict(scopes)
    s2.pop("genrec-0")
    assert "missing" in C.model_preserved({"scopes": scopes}, {"scopes": s2})
    assert "missing" in C.model_preserved({"items": ["a", "b"]}, {"items": ["a"]})
    assert C.model_preserved({"items": ["a"]}, {"items": ["a", "new"]}) is None
    assert c.preserve(_obs({"names": ["x"]}), "g", 0, _obs({"names": []})).startswith("starting objects")
    assert E.scopes_from_rows([["Auf meinem Mac"], ["Notizen", "129"], ["genrec-0", "2"]]) == \
        {"Notizen": 129, "genrec-0": 2}
    print("OK  object_create preserved state: model scopes/listing, not the visible rows")


def test_authentication_prompt_is_refused_never_answered():
    """Finder's Duplicate Exactly opened a system password
    prompt. A prompt is recognised by roles alone (a dialog or sheet holding
    a secure text field); the probed item gets no class, with that reason."""
    field = {"role": "AXTextField", "subrole": "AXSecureTextField", "children": []}
    agent = {"role": "AXApplication", "children": [
        {"role": "AXWindow", "subrole": "AXDialog", "children": [
            {"role": "AXStaticText"}, {"role": "AXTextField", "subrole": ""}, field,
            {"role": "AXButton"}, {"role": "AXButton"}]}]}
    sheet = {"role": "AXWindow", "subrole": "AXStandardWindow", "children": [
        {"role": "AXSheet", "children": [{"role": "AXGroup", "children": [
            {"role": "AXSecureTextField", "children": []}]}]}]}
    login_form = {"role": "AXWindow", "subrole": "AXStandardWindow", "children": [field]}
    plain = {"role": "AXWindow", "subrole": "AXDialog", "children": [{"role": "AXTextField"}]}
    assert E.is_auth_prompt(agent) and E.is_auth_prompt(sheet)
    assert not E.is_auth_prompt(login_form)      # a secure field in a document window is not a prompt
    assert not E.is_auth_prompt(plain) and not E.is_auth_prompt(None)
    base = _snap(auth_prompt=False)
    after = _snap(auth_prompt=True, model={"kind": "folder", "items": ["a", "b", "a copy"]})
    assert E.classify(base, after)["effects"][0] == "auth_required"
    cands, counts = E.map_from_probes(DUMP, [_probe("N", [2, 1], base, after)])
    assert cands == [] and "requires authentication" in list(counts["per_item"].values())[0]
    # an item recorded as raising a prompt is never pressed again
    ident = E.identity_string(next(m for m in DUMP["menu_items"] if m["index_path"] == [2, 1]))
    plan, sk = E.probe_plan(DUMP, refused={ident: "requires authentication"})
    assert not any(p.trigger["index_path"] == [2, 1] for p in plan)
    assert any("authentication prompt" in v for v in sk.values())
    print("OK  an authentication prompt (dialog/sheet + secure field) -> refused, no class, never answered")


TESTS = [
    test_authentication_prompt_is_refused_never_answered,
    test_probe_plan_excludes_by_convention_and_identity_never_by_title,
    test_effect_classification_and_mapping,
    test_checkbox_group_is_never_an_option_group,
    test_scoped_find_semantics_calibration,
    test_scoped_find_compares_with_the_same_semantics_only,
    test_declared_coupled_settings,
    test_preserved_state_through_the_model_not_the_viewport,
]


def main():
    for t in TESTS:
        t()
    print("\nDESKTOP EFFECTS TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
