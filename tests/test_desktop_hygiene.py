"""Desktop test hygiene: a run starts from the declared clean state or does not run.

The repeatability study's Finder "new folder" went 0/20 because a guest job left a folder selected and another
process's authorization prompt on screen; every run then ACTED on that state
(13 moved the selection into the new folder). These tests pin the pre-run
check that refuses such a state before anything is actuated, the file-system
reset of the disposable folder (marker-guarded, never through the UI), and
the prove harness's refusal to call the UI path on a dirty row.

Offline: observations are dicts, the folder is a temp directory. No desktop,
no AX, no subprocess.
"""
import os
import tempfile

from anyuse.execution import desktop_contracts as C
from anyuse.execution import desktop_hygiene as H

FINDER = "com.apple.finder"
SEEDS = ["kiwano_pie.txt", "salak_tart.md", "blueberry_jam.txt", "gen_sentinel.txt"]


def declared(folder=""):
    return H.CleanState(bundle_id=FINDER, window="finder", folder=folder,
                        entries=tuple((n, "file") for n in SEEDS) if folder else ())


def clean_obs(**kw):
    obs = {"bundle_id": FINDER, "front_window": "finder", "selection": [], "dialogs": [],
           "foreign_windows": [{"owner": "Dock", "title": ""},
                               {"owner": "Window Server", "title": "Menubar"}],
           "folder_listing": list(SEEDS) + [H.MARKER]}
    obs.update(kw)
    return obs


def test_a_clean_state_passes():
    assert H.check(declared("/scratch/finder"), clean_obs()) == []


def test_the_studys_dirty_finder_state_is_refused_before_acting():
    # section 5: a folder selected in the test folder, a SecurityAgent prompt
    # on screen, and earlier runs' objects in the folder
    obs = clean_obs(selection=["genlychee-2"],
                    foreign_windows=[{"owner": "SecurityAgent",
                                      "title": "Finder möchte „blueberry_jam.txt“ duplizieren"}],
                    folder_listing=list(SEEDS) + ["genlychee-2", "Neuer Ordner 2"])
    probs = H.check(declared("/scratch/finder"), obs)
    assert len(probs) == 3, probs
    assert any("selected" in p and "genlychee-2" in p for p in probs)
    assert any("SecurityAgent" in p for p in probs)
    assert any("unexpected" in p and "Neuer Ordner 2" in p for p in probs)
    acted = []
    out = H.run_from_clean_state(declared("/scratch/finder"), lambda: obs,
                                 lambda: acted.append(1), reset=None)
    assert out["refused"] and not out["ran"] and acted == []


def test_each_declared_condition_is_checked():
    d = declared()
    assert "not 'com.apple.finder'" in H.check(d, clean_obs(bundle_id="com.apple.TextEdit"))[0]
    assert "front window" in H.check(d, clean_obs(front_window="Downloads"))[0]
    assert "sheet/dialog" in H.check(d, clean_obs(dialogs=[("sheet", (0, 0, 9, 9))]))[0]
    assert "text selection" in H.check(d, clean_obs(selection=["text selection: 'x'"]))[0]
    anywin = H.CleanState(bundle_id=FINDER)
    assert H.check(anywin, clean_obs(front_window="Downloads")) == []


def test_an_unreadable_state_is_never_assumed_clean():
    d = declared("/scratch/finder")
    for key in ("bundle_id", "front_window", "selection", "dialogs", "foreign_windows",
                "folder_listing"):
        probs = H.check(d, clean_obs(**{key: None}))
        assert probs and "could not read" in probs[0], key
    assert H.check(d, None) == ["the start state could not be observed"]

    def boom():
        raise RuntimeError("AX not trusted")
    clean, probs, _obs, _ = H.require_clean(d, boom, reset=None)
    assert not clean and "AX not trusted" in probs[0]


def test_the_disposable_folder_is_reset_through_the_file_system_only():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
        folder = os.path.join(root, "finder")
        H.mark_disposable(folder)
        for n in SEEDS[1:]:
            open(os.path.join(folder, n), "w").close()          # SEEDS[0] is missing
        os.makedirs(os.path.join(folder, "genlychee-2", "moved"))
        open(os.path.join(folder, "Neuer Ordner 2"), "w").close()
        precious = os.path.join(outside, "keep.txt")
        open(precious, "w").close()
        os.symlink(outside, os.path.join(folder, "link-out"))
        d = declared(folder)
        done = H.reset_folder(d)
        assert "recreated kiwano_pie.txt" in done and "removed genlychee-2" in done
        assert sorted(n for n in os.listdir(folder) if n != H.MARKER) == sorted(SEEDS)
        assert os.path.exists(precious), "a symlink is removed as a link, never followed"
        out = H.run_from_clean_state(
            d, lambda: clean_obs(folder_listing=os.listdir(folder)), lambda: "acted")
        assert out["ran"] and out["result"] == "acted"


def test_reset_refuses_an_unmarked_or_foreign_folder():
    with tempfile.TemporaryDirectory() as root:
        folder = os.path.join(root, "finder")
        os.makedirs(folder)
        open(os.path.join(folder, "important.txt"), "w").close()
        try:
            H.reset_folder(declared(folder))
            raise AssertionError("an unmarked folder was reset")
        except H.DirtyStartState as exc:
            assert "marker" in str(exc)
        assert os.path.exists(os.path.join(folder, "important.txt"))
        other = os.path.join(root, "other")
        H.mark_disposable(other)
        os.replace(os.path.join(other, H.MARKER), os.path.join(folder, H.MARKER))
        clean, probs, _o, _a = H.require_clean(declared(folder), clean_obs)
        assert not clean and "marker" in probs[0]
        assert os.path.exists(os.path.join(folder, "important.txt"))
    try:
        H.reset_folder(declared("relative/finder"))
        raise AssertionError("a relative path was accepted")
    except H.DirtyStartState:
        pass


def _obs(names):
    return {"identity": {"bundle_id": FINDER, "window": "w", "document": None},
            "complete": True, "target": {"names": names}, "sentinel": "S"}


def test_the_prove_harness_never_acts_on_a_dirty_row():
    calls = []
    start = _obs(["keep"])

    def ui(i, r):
        calls.append((i, r))
        return C.OBJECT_CREATE.predict(start, i, r)
    dirty = {("n1", 1)}
    rep = C.prove_contract(C.OBJECT_CREATE, "fake", ["n1", "n2"], 3, ui,
                           recorded_inputs=["n0"], start_state=start,
                           clean_check=lambda i, r: (["1 item(s) selected (genlychee-2)"]
                                                     if (i, r) in dirty else []))
    assert not rep.proved
    refused = [r for r in rep.rows if r.mismatch and r.mismatch.startswith("refused before acting")]
    assert len(refused) == 1 and refused[0].ui_value is None
    assert ("n1", 1) not in calls and len(calls) == 5
    rep = C.prove_contract(C.OBJECT_CREATE, "fake", ["n1", "n2"], 3, ui,
                           recorded_inputs=["n0"], start_state=start,
                           clean_check=lambda i, r: [])
    assert rep.proved


def test_a_reset_that_misses_the_declared_start_refuses_before_acting_in_hygiene_mode():
    calls = []
    start = _obs(["keep"])

    def ui(i, r):
        calls.append((i, r))
        return C.OBJECT_CREATE.predict(start, i, r)
    rep = C.prove_contract(C.OBJECT_CREATE, "fake", ["n1", "n2"], 3, ui,
                           recorded_inputs=["n0"], start_state=start,
                           observe_start=lambda i, r: _obs([]),       # "keep" is gone
                           clean_check=lambda i, r: [])
    assert not rep.proved and calls == []
    assert all("refused before acting" in r.mismatch for r in rep.rows)


def test_leftover_windows_above_the_normal_layer_are_found_and_tidied_by_role():
    """A dictation panel (an input method's modal
    panel at layer 8, no accessibility tree, left by an earlier
    probe) dirtied all 36 proof rows. The pre-job check lists every window
    above the normal layer that is not session chrome and tidies by the
    owner's role: a helper is ended, an auth agent never touched, a regular
    app's panel reported. Normal-layer windows (Terminal, Blender behind the
    app) and chrome layers are not leftovers."""
    wins = [{"owner": "Window Server", "pid": 181, "layer": 2147483630, "title": "Cursor"},
            {"owner": "Kontrollzentrum", "pid": 391, "layer": 25, "title": "Clock"},
            {"owner": "Mitteilungszentrale", "pid": 400, "layer": 23, "title": ""},
            {"owner": "Dock", "pid": 390, "layer": 20, "title": "Dock"},
            {"owner": "Diktat", "pid": 1724, "layer": 8, "title": ""},
            {"owner": "SecurityAgent", "pid": 900, "layer": 1000, "title": "Finder möchte …"},
            {"owner": "Terminal", "pid": 1400, "layer": 0, "title": "runner"},
            {"owner": "Blender", "pid": 1500, "layer": 0, "title": "Blender"},
            {"owner": "Vorschau", "pid": 1600, "layer": 3, "title": "Info"},
            {"owner": "Palette", "pid": 1700, "layer": 21, "title": "not a leftover: chrome layer"}]
    left = H.leftover_windows(wins, regular_pids=[1400, 1500, 1600])
    assert [(w["owner"], w["tidy"]) for w in left] == [("Diktat", "end"), ("SecurityAgent", "never"),
                                                        ("Vorschau", "report")]
    assert H.leftover_windows(wins[:4] + wins[6:8], [1400, 1500]) == []
    # an unreadable entry is skipped, never ended
    assert H.leftover_windows([{"owner": "X", "pid": "?", "layer": 8}], []) == []


if __name__ == "__main__":
    import inspect
    import sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:400])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
