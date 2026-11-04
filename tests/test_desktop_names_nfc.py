"""Every name comparison of the object checks, the routines and the judge compares Unicode NFC (``desktop_fixes``
item 3, ``desktop_routine``, ``desktop_contracts``, the guest's ``listing``), offline with fakes.

GG05 (arm E): the escalation's ``object_create 'Rückfragen'`` succeeded on screen, but Finder listed the
new folder DECOMPOSED ('Ru\\u0308ckfragen') and the executor's check reported "no object named 'Rückfragen'
afterwards (new: ['Rückfragen'])": the step was not compiled, the routine lacked the create step, the
compiled repeat and two variant replays failed. Pinned here, each with the decomposed-vs-composed case:

* the escalation's executor (and every arm with the layer) creates the folder, the check passes, the step is
  compiled and the routine's replay creates it again; the frozen Arch12 without the layer fails exactly as
  recorded (its bytes are pinned);
* the layer's boundary: row labels, the folder / note shown, paths, a listing's names reach the arm composed; file
  contents are never touched;
* the routine: one name in two forms is ONE parameter; a caller's decomposed value names the recorded item; the
  identity check, the folder check, a name already taken and the item before a gate compare composed;
* the object contract: expected, start and preserved names compare composed;
* the judge: ``run_one_nfc`` gives the frozen judges their inputs composed (a decomposed new folder is the expected
  one, not an unexpected entry); the frozen judge alone would still fail it;
* the guest's ``listing`` answers composed names.

No desktop, no AX, no subprocess, no network.
"""
import importlib
import os
import sys
import unicodedata

from anyuse.execution import desktop_contracts as C
from anyuse.execution import desktop_e2e as X
from anyuse.execution import desktop_e2e4 as W
from anyuse.execution import desktop_e2e9 as N
from anyuse.execution import desktop_e2e12 as Z
from anyuse.execution import desktop_escalate as E
from anyuse.execution import desktop_fixes as F2
from anyuse.execution import desktop_routine as R

import test_desktop_e2e4 as T4
import test_desktop_e2e9 as T9

EMAP = W.EffectMap4()
S = T4.S
ROOT = T4.ROOT
NFD = lambda s: unicodedata.normalize("NFD", s)                                             # noqa: E731
NAME = "Rückfragen"
# GG05's world (the pool's start): the folder the escalation had navigated into
GG05 = {"Angebote Herbstmarkt/Übersicht.txt": "Angebote getrennt nach Außenflächen und Innenräumen.",
        "Angebote Herbstmarkt/Außenflächen/Hof.txt": "Hof\nTagespreis: 240 Euro",
        "Angebote Herbstmarkt/Außenflächen/Garten.txt": "Garten\nTagespreis: 280 Euro",
        "Angebote Herbstmarkt/Innenräume/Saal.txt": "Saal\nTagespreis: 320 Euro"}


class FinderNFD(T9.FakeGuest9):
    """Finder as the guest saw it: a folder it creates is listed DECOMPOSED."""

    def op_enter(self, bundle, target, text, commit=False, keys=False):
        if target.get("focused") and self.focus_new:
            self.fs[self.shown + "/" + NFD(text)] = None
            self.focus_new = False
            return {"status": "done"}
        return super().op_enter(bundle, target, text, commit, keys)


def _guest(shown="Angebote Herbstmarkt/Außenflächen"):
    return FinderNFD(GG05, shown=shown)


def _create(ex, g):
    task = X.Task("GG05", "Da brauch ich einen leeren Ordner namens Rückfragen.", "single-app", {}, [], {})
    rec = X.Rec("GG05", "arch8e:esc", 1)
    ex.run(task, rec, compiled=[])                          # the executor's state, no step (as Escalation15 does)
    step, fixed, err = E.to_step15({"effect": "object_create", "app": "Finder", "value": NAME})
    assert err == "" and fixed is None
    log = {"i": 0, "step": step, "esc": True}
    return ex.do_step(task, rec, step, log, fixed), log, rec, step


def test_gg05_the_escalations_create_is_checked_composed_and_compiled_and_replays():
    g = _guest()
    ex = E.EscExec(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    out, log, rec, step = _create(ex, g)
    assert out == "ok", log
    assert NFD(NAME) in {p.rsplit("/", 1)[1] for p in g.fs}                     # created decomposed, as Finder does
    assert log["check"] == "object_create contract met (one new object with the name)"
    # compiled like the engine's step, the routine replays it on a new folder with 0 model calls
    comp = [{"step": S("navigate", "Finder", "Innenräume"), "resolved": {"op": "navigate", "label": "Außenflächen",
                                                                       "via": "sub"}},
            {"step": step, "resolved": dict(log["resolved"], by15="escalation")}]
    comp[0]["step"]["target"] = "Außenflächen"
    rt = R.derive_routine(comp, "x", task="GG05", arm="arch8e")
    assert {p["name"] for p in rt["params"]} == {"folder", "name"}
    g2 = _guest(shown="Angebote Herbstmarkt")
    a = R.ReplayArch(g2, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    steps, goal, probs = a.prepare(rt, {"folder": "Innenräume"})
    rec2 = X.Rec("GG05", "replay", 1, mode="compiled")
    a.run(X.Task("GG05", goal, "single-app", {}, [], {}), rec2, compiled=steps)
    assert rec2.status == "completed", (rec2.stopped, rec2.steps[-1])
    assert f"{ROOT}/Angebote Herbstmarkt/Innenräume/{NFD(NAME)}" in g2.fs and sum(rec2.calls.values()) == 0


def test_prior_frozen_arm_without_the_layer_fails_as_in_prior_version():
    g = _guest()
    ex = Z.Arch12(g, EMAP, planner=None, spend=None, inbox=None, sleep=lambda s: None, precondition13=True)
    out, log, _rec, _step = _create(ex, g)
    assert out == "failed" and log["why"] == f"no object named {NAME!r} afterwards (new: [{NFD(NAME)!r}])"


def test_the_layers_boundary_hands_names_composed_and_leaves_contents_alone():
    r = F2.nfc_names("rows", {"where": NFD("Außenflächen"), "shown_note": NFD("Grüne Kiste"), "rows": [
        {"label": NFD("Rückfragen"), "text": NFD("Rückfragen | Ordner"), "selected": False}]})
    assert r["where"] == "Außenflächen" and r["shown_note"] == "Grüne Kiste" and r["rows"][0]["label"] == NAME
    assert F2.nfc_names("listing", {"names": [NFD(NAME), "Hof.txt"]})["names"] == [NAME, "Hof.txt"]
    assert F2.nfc_names("front", {"document": NFD("/x/Übergabe.txt"), "title": NFD("Übergabe.txt")})["title"] == \
        "Übergabe.txt"
    f = {"ok": True, "text": NFD("Grüße"), "sha256": "ab"}
    assert F2.nfc_names("file", f)["text"] == NFD("Grüße")                       # contents are never touched
    assert F2.nfc_names("item_dates", {"items": {NFD("Saal.txt"): 1}})["items"] == {"Saal.txt": 1}
    assert F2.same_name(NFD(NAME), NAME) and not F2.same_name("Ruckfragen", NAME)


def test_the_routine_compares_composed():
    comp = [{"step": S("launch_app", "Finder"), "resolved": {"op": "launch", "bundle": T4.FINDER}},
            {"step": S("navigate", "Finder", NFD("Außenflächen")), "resolved": {"op": "navigate",
                                                                                "label": "Außenflächen", "via": "sub"}},
            {"step": S("object_create", "Finder", "folder", NAME), "resolved": {"op": "object_create",
                                                                                 "entry": T4.NEWF}}]
    rt = R.derive_routine(comp, f"Geh in die {NFD('Außenflächen')} und leg {NAME} an.")
    ps = {p["name"]: p for p in rt["params"]}
    assert set(ps) == {"folder", "name"} and len(ps["folder"]["bind"]) == 2         # one name in two forms: ONE
    assert ps["folder"]["goal_spans"] == 1
    assert R._same_value(rt, ps["name"], NFD(NAME))                                  # a caller's decomposed value
    steps, goal, probs = R.instantiate(rt, {"name": NFD("Rückfragen 2")})
    assert probs == [] and steps[2]["step"]["value"] == "Rückfragen 2"               # bound composed
    a = R.ReplayArch(_guest(shown="Angebote Herbstmarkt"), EMAP, spend=None, inbox=None, sleep=lambda s: None)
    a._bound15 = {}
    assert a._identity15(S("navigate", "Finder", "x"), {"op": "navigate", "label": NFD("Außenflächen"),
                                                        "via": "sub"}) is None     # the row is found composed
    g = _guest()
    g.fs[f"{ROOT}/Angebote Herbstmarkt/Außenflächen/{NFD(NAME)}"] = None            # the name is taken (decomposed)
    a = R.ReplayArch(g, EMAP, spend=None, inbox=None, sleep=lambda s: None)
    a._bound15 = {f"folder:{T4.FINDER}": NFD("Außenflächen")}
    assert "already exists" in a._identity_create15(T4.FINDER, NAME)
    a._bound15[f"item:{T4.FINDER}"] = NFD("Hof.txt")
    g.sel = ["Hof.txt"]
    assert a._gate15(S("human_gate", "Finder", "Hof.txt", "delete")) is None


def test_the_object_contract_compares_composed():
    start = {"target": {"names": ["Hof.txt"]}, "sentinel": None}
    obs = {"target": {"names": ["Hof.txt", NFD("Rückfragen-1")]}, "sentinel": None}
    assert C._oc_expect(start, "Rückfragen", 1, obs) is None
    assert "already existed" in C._oc_expect({"target": {"names": [NFD("Rückfragen-1")]}}, "Rückfragen", 1, obs)
    assert C._oc_preserve({"target": {"names": [NFD("Grüße.txt")]}}, "x", 1, {"target": {"names": ["Grüße.txt"]}}) \
        is None
    assert C._oc_start_ok({"identity": 1, "sentinel": None, "target": {"names": ["Grüße.txt"]}},
                          {"identity": 1, "sentinel": None, "target": {"names": [NFD("Grüße.txt")]}})


class JudgeGuest:
    """clean_start / read_state of GG05's three folders, the new folder listed decomposed at the end."""

    def __init__(self, task):
        self.task = task

    def __call__(self, op, **kw):
        ents = lambda *names: {"ok": True, "entries": [{"name": n, "type": "dir"} for n in names]}  # noqa: E731
        if op == "clean_start":
            return {"status": "done", "clean": True, "read": [ents("Hof")]}
        if op == "read_state":
            return {"status": "done", "read": [ents("Hof", NFD(NAME))]}
        raise AssertionError(op)


class NoArch:
    def run(self, task, rec, compiled=None):
        rec.status = "completed"
        return rec


def test_the_judges_inputs_are_composed():
    folder = "~/anyuse-guest/scratch/e2e/gen/e15/GG05/Angebote Herbstmarkt/Außenflächen"
    task = X.Task("GG05", "x", "single-app", {}, [{"kind": "folder", "path": folder}],
                  {"folder_added": {folder: [[NAME, "dir"]]}})
    out = F2.run_one_nfc(task, "arch8e", 1, JudgeGuest(task), arch=NoArch(), emap=EMAP)
    assert out["judge"]["success"] and out["judge"]["items"] == [{"what": f"new dir {NAME!r}", "status": "ok"}]
    old = N.run_one9(task, "arch8e", 1, JudgeGuest(task), arch=NoArch(), emap=EMAP)     # the frozen judge alone
    assert not old["judge"]["success"]
    t2, s2, e2 = F2.nfc_judge(X.Task("T", "x", "k", {}, [{"path": NFD("Ü")}], {"a": [NFD("Ü")]}), [NFD("Ü")], {})
    assert t2.read == [{"path": "Ü"}] and t2.expect == {"a": ["Ü"]} and s2 == ["Ü"]


def test_the_guests_listing_answers_composed_names(monkeypatch=None):
    vm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "execution", "guest")
    if vm not in sys.path:
        sys.path.insert(0, vm)
    J = importlib.import_module("desktop_e2e_job")
    real_listdir, real_ours = J.os.listdir, J.ours
    J.os.listdir = lambda p: [NFD(NAME), "Hof.txt", ".DS_Store"]
    J.ours = lambda p: True
    try:
        assert J.op_listing({"path": "~/anyuse-guest/scratch/e2e/gen/x"})["names"] == ["Hof.txt", NAME]
    finally:
        J.os.listdir, J.ours = real_listdir, real_ours


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
