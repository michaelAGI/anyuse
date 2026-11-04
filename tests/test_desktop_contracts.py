"""Offline tests for ``execution/desktop_contracts.py``.

Structure-only mapping over the five real guest dumps
(``testdata/discovery/``), and every contract's proof with fakes: a good run
proves against the contract's own prediction; injected shared failures, a
wrong target (other document / other app), a preserved-state violation and
Finder's mid-word search mismatch all fail.

Run: ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_desktop_contracts.py
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
from anyuse.execution import desktop_compile as dc  # noqa: E402

DUMPS = _HERE / "testdata" / "discovery"


def _dump(bid):
    with gzip.open(DUMPS / f"dump_{bid}.json.gz") as fh:
        return json.load(fh)


def _by(cands):
    out = {}
    for c in cands:
        out.setdefault(c.contract, []).append(c)
    return out


def test_real_dumps_map_by_structure():
    got = {}
    for bid in ("com.apple.TextEdit", "com.apple.Notes", "com.apple.finder",
                "com.apple.calculator", "org.blenderfoundation.blender"):
        cands, counts = C.map_contracts(_dump(bid))
        got[bid] = (_by(cands), counts)
        print(f"    {bid}: {counts['contract_candidates']} candidates "
              f"{ {k: v for k, v in counts['by_contract'].items() if v} } "
              f"source mapped {counts['source_candidates_mapped']}/{counts['discovered']}")
        assert counts["triggers_keyed_by_position"] == 0
    te, _ = got["com.apple.TextEdit"]
    assert [c.recipe["trigger"]["cmd_char"] for c in te["doc_save"]] == ["s"]
    assert "object_create" not in te and "scoped_find" not in te     # no container
    fi, _ = got["com.apple.finder"]
    oc = sorted((c.recipe["trigger"]["cmd_mods"]) for c in fi["object_create"])
    assert oc == ["0", "1", "4"], oc   # ⌘N (a window), ⇧⌘N, ⌃⌘N (needs a selection): all fit the structure
    assert len(fi["scoped_find"]) == 1 and "property_update" in fi
    no, _ = got["com.apple.Notes"]
    assert no["object_create"] and "doc_save" not in no          # Notes has no ⌘S
    ca, cc = got["com.apple.calculator"]
    assert ca == {} and not cc["menu_marks_in_dump"]   # old dump: no marks read, so no radio groups
    bl, _ = got["org.blenderfoundation.blender"]
    assert [c.recipe.get("backend") for c in bl["object_create"]] == ["bpy"]
    print("OK  real dumps map by structure: TextEdit doc_save, Finder/Notes object_create + "
          "scoped_find, Finder slider property_update, Blender bpy object_create; no position keys")


def test_mapping_never_consults_the_bundle_id():
    for bid in ("com.apple.TextEdit", "com.apple.Notes", "com.apple.finder"):
        d = _dump(bid)
        a, _ = C.map_contracts(d)
        d2 = copy.deepcopy(d)
        d2["bundle_id"] = "org.example.unknown"
        b, _ = C.map_contracts(d2)
        assert [(x.contract, x.triggers, x.recipe) for x in a] == \
               [(x.contract, x.triggers, x.recipe) for x in b], bid
    print("OK  renaming the bundle id changes no mapping (structure only)")


def test_radio_groups_need_exactly_one_mark():
    d = _dump("com.apple.calculator")
    for it in d["menu_items"]:
        it["mark_char"] = ""
        if it["index_path"] == [3, 1]:
            it["mark_char"] = "✓"
    cands, counts = C.map_contracts(d)
    groups = [c for c in cands if c.contract == "toggle_select"]
    assert len(groups) == 1, [g.triggers for g in groups]
    g = groups[0]
    assert [o["cmd_char"] for o in g.recipe["options"]] == ["1", "2", "3"] and g.provable
    assert all(k.startswith("id:menuAction:@") for k in g.triggers)    # shared id -> +position key
    for it in d["menu_items"]:
        if it["index_path"] == [3, 2]:
            it["mark_char"] = "✓"             # two marked: independent toggles, not a radio group
    cands, _ = C.map_contracts(d)
    assert not [c for c in cands if c.contract == "toggle_select"]
    # a two-option group is mapped but not provable (1 to record on, < 2 unseen)
    def item(path, title, ch, mark=""):
        return {"index_path": path, "title_path": ["App", "View", title], "title": title,
                "identifier": f"_NS:{path[-1]}", "cmd_char": ch, "cmd_mods": 0,
                "cmd_virtual_key": None, "enabled": True, "has_submenu": False, "mark_char": mark}
    d = {"bundle_id": "org.example.two", "menu_items": [
        item([1, 0], "Quit", "q"), item([2, 0], "As list", "1", "\u2713"), item([2, 1], "As gallery", "2"),
        {"index_path": [2, 2], "title_path": ["App", "View", ""], "title": "", "identifier": "",
         "cmd_char": "", "cmd_mods": 0, "enabled": False, "has_submenu": False, "mark_char": ""},
        item([2, 3], "Zoom", "+")], "window_controls": []}
    two = [c for c in C.map_contracts(d)[0] if c.contract == "toggle_select"]
    assert len(two) == 1 and not two[0].provable and "unseen" in two[0].note, [t.to_dict() for t in two]
    print("OK  toggle_select: a sibling run with exactly one mark is a radio group; two marks is not; "
          "a 2-option group is mapped but not provable")


# --- proofs with fakes ------------------------------------------------------

ID = {"bundle_id": "org.example.app", "window": "w", "document": None}


def _obs(target, sentinel="S", complete=True, identity=ID):
    return {"identity": dict(identity), "complete": complete, "target": target, "sentinel": sentinel}


START = {
    "field_rw": _obs("old"),
    "toggle_select": _obs({"options": ["a", "b", "c"], "selected": ["a"]}),
    "scoped_find": _obs({"names": [], "scope": ["apple_pie.txt", "blueberry_pie.txt", "fig_roll.txt"]}),
    "doc_save": _obs({"text": "seed\n"}),
    "object_create": _obs({"names": ["keep"]}),
    "property_update": _obs({"value": 64.0}),
    "flag_toggle": _obs({"options": ["a", "b", "c", "d"], "marked": ["a"]}),
}
INPUTS = {"field_rw": ["x1", "x2"], "toggle_select": ["b", "c"], "scoped_find": ["pie", "roll"],
          "doc_save": ["t1", "t2"], "object_create": ["n1", "n2"], "property_update": [32.0, 96.0],
          "flag_toggle": ["c", "d"]}
RECORDED = {"field_rw": ["x0"], "toggle_select": ["a"], "scoped_find": ["tart"], "doc_save": ["t0"],
            "object_create": ["n0"], "property_update": [64.0], "flag_toggle": ["b"]}


def _prove(name, ui_fn, **kw):
    c = C.CONTRACTS[name]
    args = dict(recorded_inputs=RECORDED[name], start_state=START[name])
    args.update(kw)
    return C.prove_contract(c, "fake", INPUTS[name], 3, ui_fn, **args)


def test_every_contract_proves_a_correct_fake_against_its_prediction():
    for name, c in C.CONTRACTS.items():
        good = lambda i, r, c=c, s=START[name]: c.predict(s, i, r)  # noqa: E731
        rep = _prove(name, good)
        assert rep.proved and len(rep.rows) == 6, (name, rep.to_dict())
    print(f"OK  all {len(C.CONTRACTS)} contracts prove a correct fake over 2 unseen inputs x 3 runs")


def test_every_contract_rejects_injected_shared_failures():
    for name, c in C.CONTRACTS.items():
        s = START[name]
        injections = {
            "None": lambda i, r: None,
            "error": lambda i, r: {"error": "window gone"},
            "unchanged start": lambda i, r, s=s: copy.deepcopy(s),
            "incomplete": lambda i, r, c=c, s=s: {**c.predict(s, i, r), "complete": False},
            "raises": lambda i, r: (_ for _ in ()).throw(RuntimeError("AX timeout")),
        }
        for label, fn in injections.items():
            # the "direct" side fails the same way: a shared failure
            rep = _prove(name, fn, direct_fn=fn)
            if name == "scoped_find" and label == "unchanged start":
                # a read with no hits is a legitimate observation; the start
                # state here has no hits, and 'pie'/'roll' do: prediction differs
                rep = _prove(name, fn)
            assert not rep.proved and not any(r.agree for r in rep.rows), (name, label)
    print("OK  every contract fails every row on shared None / error / unchanged / incomplete / raise")


def test_wrong_target_and_preserved_state_fail():
    c = C.CONTRACTS["doc_save"]
    start = _obs({"text": "seed\n"}, identity={**ID, "document": "/scratch/a.txt"})
    other = {**ID, "document": "/scratch/b.txt"}
    wrong_doc = lambda i, r: _obs({"text": str(i)}, identity=other)  # noqa: E731
    rep = C.prove_contract(c, "fake", ["t1", "t2"], 3, wrong_doc, recorded_inputs=[], start_state=start)
    assert not rep.proved and all("observed document" in r.mismatch for r in rep.rows)
    wrong_app = lambda i, r: _obs({"text": str(i)}, identity={**ID, "bundle_id": "other.app"})  # noqa: E731
    rep = C.prove_contract(c, "fake", ["t1", "t2"], 3, wrong_app, recorded_inputs=[], start_state=START["doc_save"])
    assert not rep.proved and all("observed app" in r.mismatch for r in rep.rows)
    clobber = lambda i, r: _obs({"text": str(i)}, sentinel="overwritten")  # noqa: E731
    rep = _prove("doc_save", clobber)
    assert not rep.proved and all("sentinel changed" in r.mismatch for r in rep.rows)
    extra = lambda i, r: _obs({"names": ["keep", C.object_name(i, r), "stray"]})  # noqa: E731
    rep = _prove("object_create", extra)
    assert not rep.proved and all("unexpected additional" in r.mismatch for r in rep.rows)
    lost = lambda i, r: _obs({"names": [C.object_name(i, r)]})  # noqa: E731
    rep = _prove("object_create", lost)
    assert not rep.proved and all("missing afterwards" in r.mismatch for r in rep.rows)
    two = lambda i, r: _obs({"options": ["a", "b", "c"], "selected": ["a", i]})  # noqa: E731
    rep = _prove("toggle_select", two)
    assert not rep.proved and all("want exactly" in r.mismatch for r in rep.rows)
    print("OK  other document, other app, clobbered sentinel, stray/lost object, two marks: all fail")


def test_scoped_find_mid_word_mismatch_stays_a_failure():
    scope = ["apple_pie.txt", "blueberry_pie.txt", "elderberry_tart.txt", "fig_roll.txt"]
    start = _obs({"names": [], "scope": scope})

    def finder_like(p, r):     # Spotlight: word-prefix matching only
        toks = {n: [t for t in n.replace(".", "_").split("_")] for n in scope}
        return _obs({"names": sorted(n for n in scope if any(t.startswith(p) for t in toks[n])),
                     "scope": scope})
    rep = C.prove_contract(C.SCOPED_FIND, "fake", ["roll", "berry"], 3, finder_like,
                           recorded_inputs=["pie"], start_state=start)
    bad = [r for r in rep.rows if not r.agree]
    assert not rep.proved and len(bad) == 3 and all(r.input == "berry" for r in bad)
    assert all(r.mismatch == "ui and direct observations differ" for r in bad)
    print("OK  scoped_find: 'roll' proves, 'berry' (mid-word) fails 3/3 against the literal-substring contract")


def test_reset_must_reach_the_declared_start():
    c = C.CONTRACTS["toggle_select"]
    good = lambda i, r: c.predict(START["toggle_select"], i, r)  # noqa: E731
    starts = lambda i, r: _obs({"options": ["a", "b", "c"], "selected": ["c" if r == 1 else "a"]})  # noqa: E731
    rep = _prove("toggle_select", good, observe_start=starts)
    bad = [r for r in rep.rows if not r.agree]
    assert len(bad) == 2 and all("declared start state" in r.mismatch for r in bad)
    print("OK  a run whose reset left the previous option selected fails")


def test_object_create_reset_runs_accumulate_run_named_objects():
    """Reset runs of object_create start from the declared objects plus the
    earlier runs' objects; each run is judged against its own observed
    start, and a run whose start lost a declared object fails."""
    made = []

    def observe(i, r):
        return _obs({"names": ["keep"] + list(made)})

    def ui(i, r):
        made.append(C.object_name(i, r))
        return _obs({"names": ["keep"] + list(made)})
    rep = _prove("object_create", ui, observe_start=observe)
    assert rep.proved, rep.to_dict()
    made.clear()
    rep = _prove("object_create", ui, observe_start=lambda i, r: _obs({"names": list(made)}))
    assert not rep.proved and all("declared start" in r.mismatch for r in rep.rows)
    print("OK  object_create across reset runs: run-suffixed names, each run against its observed start")


TESTS = [
    test_real_dumps_map_by_structure,
    test_mapping_never_consults_the_bundle_id,
    test_radio_groups_need_exactly_one_mark,
    test_every_contract_proves_a_correct_fake_against_its_prediction,
    test_every_contract_rejects_injected_shared_failures,
    test_wrong_target_and_preserved_state_fail,
    test_scoped_find_mid_word_mismatch_stays_a_failure,
    test_reset_must_reach_the_declared_start,
    test_object_create_reset_runs_accumulate_run_named_objects,
]


def main():
    for t in TESTS:
        t()
    print("\nDESKTOP CONTRACTS TEST PASSED SUCCESSFULLY!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
