"""A row's container scope: Finder's and Notes' windows
carry no document, and a file list's identity is the same for every folder, so a ``select_row``
records WHICH folder or note list its window showed (selector field ``scope``: the salted key of
``context/container_scope.scope_key``, with its strength) and applies that module's policy before
anything is selected.

The module lives on the resolver lane (``feat/resolver-safety``); here it is plugged in as
``FakeScopeAPI``, which follows its documented API and comparison (``container_scope``,
``scope_key``, ``same_scope``, ``scope_strength``, ``in_container_content``, ``has_reader``) over
fake windows that carry their scope in ``_scope``. Pinned, on fakes only (the real
``AccessibilityTree`` on ``FakeAX``; the proof's ``SimAXWorld``):
* ``different`` (another path, or another name) is never the row: a candidate list or twin row
  in such a window is dropped, the only one is refused (``wrong_container``), live and in the
  proof, nothing selected;
* recorded by name only (title strength), or recorded without a scope: a window of the same
  name acts (a name confirms nothing, another name would be different);
* recorded by path, unconfirmed now (only the name readable, another machine's key): a content
  row is refused (``container_unconfirmed``), a sidebar row acts;
* no live scope readable: no container rule, the one-list rule stays (two lists: ambiguous);
* without the module plugged in nothing changes;
* the fill lists rows with their scope and a filled ``select_row`` records it.

    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_row_scope.py
"""
import asyncio
import hashlib
import hmac
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

os.environ["ANYUSE_DRIVE_ENABLED"] = "1"            # the fake AX below is the only "desktop"
os.environ["ANYUSE_INTERLOCK_TEST_BYPASS"] = "1"    # honoured only under the guarded runner

import anyuse  # noqa: E402
assert pathlib.Path(anyuse.__file__).resolve().parent == _HERE, "wrong checkout (D9)"

import test_ax_runner_executor as TX  # noqa: E402  (FakeAX, FakeAppKit, outline, install)
from anyuse.context import accessibility as acc  # noqa: E402
from anyuse.execution import ax_actions as AX  # noqa: E402
from anyuse.execution import draft_compile as DC  # noqa: E402
from anyuse.execution import draft_fill as DF  # noqa: E402
from anyuse.execution import draft_proof as PR  # noqa: E402
from anyuse.memory.storage import WorkflowStorage  # noqa: E402

FINDER, NOTES = "com.apple.finder", "com.apple.Notes"
ALPHA = {"strength": "path", "key": "/Users/fixture/Projekte/Alpha", "title": "Alpha"}
BETA = {"strength": "path", "key": "/Users/fixture/Projekte/Beta", "title": "Beta"}
#: another folder of the same name: by its name alone it cannot be told from ALPHA
ALPHA_NAME_ONLY = {"strength": "title", "key": "", "title": "Alpha"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def yes(_ctx):
    return True


class FakeScopeAPI:
    """``context/container_scope.py`` as documented: a window's
    scope is its ``_scope`` attribute (``{strength, key, title}``) in an app with a reader; the
    key format, the salt (the document salt) and the comparison are the module's. Counts reads."""
    SAME, DIFFERENT, UNCONFIRMED = "same", "different", "unconfirmed"
    READERS = {FINDER: "finder_folder", NOTES: "notes_folder"}
    SIDEBARS = {FINDER: {"Seitenleiste", "Sidebar"}, NOTES: {"Ordner", "Folders"}}

    def __init__(self):
        self.reads = 0

    def has_reader(self, app):
        return bool(app) and app in self.READERS

    def container_scope(self, tree, window, app=None):
        self.reads += 1
        if window is None or not self.has_reader(app):
            return None
        sc = getattr(window, "attrs", {}).get("_scope")
        return dict(sc, kind=self.READERS[app], source="fake") if sc else None

    @staticmethod
    def _mac(salt, part, text):
        return hmac.new(salt, f"anyuse scope {part}\0{text}".encode(), hashlib.sha256).hexdigest()[:32] if text else "-"

    def scope_key(self, scope, salt=None, create=False):
        if not isinstance(scope, dict) or not scope.get("kind"):
            return ""
        salt = salt or acc._document_salt(create=create)
        if salt is None:
            return ""
        return ":".join(["scopekey:" + acc._salt_id(salt), scope["kind"], scope.get("strength") or "title",
                         self._mac(salt, "key", scope.get("key") or ""), self._mac(salt, "title", scope.get("title") or "")])

    def same_scope(self, recorded, live, salt=None):
        if not recorded or not isinstance(live, dict) or not live.get("kind"):
            return self.UNCONFIRMED
        salt = salt or acc._document_salt(create=False)
        parts = str(recorded)[len("scopekey:"):].split(":")
        if salt is None or len(parts) != 5 or parts[0] != acc._salt_id(salt):
            return self.UNCONFIRMED
        rec = dict(zip(("sid", "kind", "strength", "key", "title"), parts))
        got = dict(zip(("sid", "kind", "strength", "key", "title"), self.scope_key(live, salt)[9:].split(":")))
        if rec["kind"] != got["kind"]:
            return self.DIFFERENT
        if rec["strength"] == got["strength"] == "path" and rec["key"] != "-" and got["key"] != "-":
            return self.SAME if rec["key"] == got["key"] else self.DIFFERENT
        if rec["title"] != "-" and got["title"] != "-" and rec["title"] != got["title"]:
            return self.DIFFERENT
        return self.UNCONFIRMED

    def scope_strength(self, scope):
        if isinstance(scope, dict):
            return str(scope.get("strength") or "") if scope.get("kind") else ""
        parts = str(scope or "")[len("scopekey:"):].split(":") if str(scope or "").startswith("scopekey:") else []
        return parts[2] if len(parts) == 5 else ""

    def in_container_content(self, tree, element, app=None):
        if not self.has_reader(app):
            return False
        read = getattr(tree, "attr", None) or acc._attr
        cur = element
        for _ in range(5):
            if cur is None or read(cur, "AXRole") in ("AXRow", "AXOutlineRow"):
                break
            cur = read(cur, "AXParent")
        box = read(cur, "AXParent") if cur is not None else None
        return box is None or read(box, "AXDescription") not in self.SIDEBARS.get(app, set())


class plugged:
    """The scope plug (``ax_actions._scope_api``) returns ``api`` for the block."""

    def __init__(self, api):
        self.api = api

    def __enter__(self):
        self.prev, AX._scope_api = getattr(AX, "_scope_api", None), (lambda: self.api)
        return self.api

    def __exit__(self, *exc):
        if self.prev is None:
            del AX._scope_api
        else:
            AX._scope_api = self.prev


def key(api, scope, app=FINDER):
    return api.scope_key(dict(scope, kind=api.READERS[app]), create=True)


def finder_list(scope, labels=("Rechnung.pdf", "Angebot.pdf"), bundle=FINDER, box_attrs=None):
    app, win, box, rows = TX.outline(list(labels), box_attrs)
    if scope:
        win.attrs["_scope"] = dict(scope)
    fake, kit = TX.install(app, kit=TX.FakeAppKit({1: (bundle, bundle.split(".")[-1])}, front=bundle))
    return fake, win, rows


def row_step(label, scope_key="", box=None):
    sel = dict(AX.row_selector(label, box or TX.LIST))
    if scope_key:
        sel["scope"] = scope_key
    return TX.step("select_row", sel)


# ── live ──────────────────────────────────────────────────────────────────────────────────────

SIDE = {"role": "outline", "identifier": "", "description": "Seitenleiste", "path": "scrollarea[0]/outline[0]"}
BETA_NAME_ONLY = {"strength": "title", "key": "", "title": "Beta"}


def test_a_row_in_a_different_folder_is_refused_live_and_nothing_is_selected():
    with plugged(FakeScopeAPI()) as api:
        for recorded, live in ((ALPHA, BETA),                       # another path
                               (ALPHA_NAME_ONLY, BETA_NAME_ONLY),   # another name: also another folder
                               (ALPHA, BETA_NAME_ONLY)):            # a path recorded, another name now
            fake, win, rows = finder_list(live)
            res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, recorded)))
            assert res.get("refused") and res["reason_code"] == "wrong_container", (recorded, live, res)
            assert fake.sets == [] and not any(r.attrs.get("AXSelected") for r in rows)
        # the recorded folder, read as a full path: same, selected
        fake, win, rows = finder_list(ALPHA)
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA)))
        assert res["success"] and res["readback"] == ["Rechnung.pdf"], res
        # two windows, the recorded folder in one: the other's list is dropped, not ambiguous
        app = TX.El("AXApplication")
        lists = {}
        for name, scope in (("Alpha", ALPHA), ("Beta", BETA)):
            w = TX.El("AXWindow", app, AXTitle=name, _scope=dict(scope))
            box = TX.El("AXOutline", TX.El("AXScrollArea", w), AXWindow=w)
            row = TX.El("AXRow", box, AXIdentifier="NSTableViewRowViewKey", AXWindow=w)
            TX.El("AXTextField", TX.El("AXCell", row, AXWindow=w), AXValue="Rechnung.pdf")
            lists[name] = row
        fake, _kit = TX.install(app, kit=TX.FakeAppKit({1: (FINDER, "Finder")}, front=FINDER))
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA)))
        assert res.get("success"), res
        assert lists["Alpha"].attrs.get("AXSelected") and not lists["Beta"].attrs.get("AXSelected")
        # both windows another folder: refused as wrong_container, nothing selected
        lists["Alpha"].parent.parent.parent.attrs["_scope"] = dict(BETA, key="/Users/fixture/Projekte/Gamma")
        fake.sets.clear()
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA)))
        assert res.get("refused") and res["reason_code"] == "wrong_container" and fake.sets == [], res


def test_a_name_recorded_scope_acts_on_the_same_name_and_a_path_one_needs_confirmation_for_content():
    with plugged(FakeScopeAPI()) as api:
        # recorded by its name only (no path readable then), the window shows that name: acts
        fake, win, rows = finder_list(ALPHA_NAME_ONLY)
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA_NAME_ONLY)))
        assert res.get("success"), res
        # an old record without a scope counts as a name: acts (another window could only be ambiguous)
        fake, win, rows = finder_list(ALPHA_NAME_ONLY)
        assert TX.run_ax(TX.tree(), row_step("Rechnung.pdf")).get("success")
        # recorded by its PATH, only the name readable now: a file row is refused, never guessed
        fake, win, rows = finder_list(ALPHA_NAME_ONLY)
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA)))
        assert res.get("refused") and res["reason_code"] == "container_unconfirmed", res
        assert fake.sets == [] and "full path" in res["error"]
        # ... and so is a key salted on another machine (it confirms nothing)
        fake, win, rows = finder_list(ALPHA)
        parts = key(api, ALPHA).split(":")
        foreign = ":".join(["scopekey", "00000000"] + parts[2:])
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", foreign))
        assert res.get("refused") and res["reason_code"] == "container_unconfirmed" and fake.sets == [], res
        # the sidebar (chrome, the same in every window) acts on unconfirmed; not on different
        fake, win, rows = finder_list(ALPHA_NAME_ONLY, labels=("Downloads",), box_attrs={"AXDescription": "Seitenleiste"})
        assert TX.run_ax(TX.tree(), row_step("Downloads", key(api, ALPHA), box=SIDE)).get("success")
        fake, win, rows = finder_list(BETA, labels=("Downloads",), box_attrs={"AXDescription": "Seitenleiste"})
        res = TX.run_ax(TX.tree(), row_step("Downloads", key(api, ALPHA), box=SIDE))
        assert res.get("refused") and res["reason_code"] == "wrong_container", res


def test_no_readable_scope_falls_back_to_the_one_list_rule_and_no_module_changes_nothing():
    with plugged(FakeScopeAPI()) as api:
        # nothing readable now (container_scope None): no container rule, one list: acts
        fake, win, rows = finder_list(None)
        assert TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA))).get("success")
        # two windows, neither readable: the one-list rule refuses (ambiguous), nothing selected
        app = TX.El("AXApplication")
        for name in ("Alpha", "Beta"):
            w = TX.El("AXWindow", app, AXTitle=name)
            box = TX.El("AXOutline", TX.El("AXScrollArea", w), AXWindow=w)
            row = TX.El("AXRow", box, AXIdentifier="NSTableViewRowViewKey", AXWindow=w)
            TX.El("AXTextField", TX.El("AXCell", row, AXWindow=w), AXValue="Rechnung.pdf")
        fake, _kit = TX.install(app, kit=TX.FakeAppKit({1: (FINDER, "Finder")}, front=FINDER))
        res = TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA)))
        assert res.get("refused") and res["reason_code"] == "ambiguous" and fake.sets == [], res
        # an app without a reader: as before
        fake, win, rows = finder_list(BETA, bundle="com.example.files")
        assert TX.run_ax(TX.tree(), row_step("Rechnung.pdf", key(api, ALPHA))).get("success")
    # the module not plugged in (this branch on its own): no container rule anywhere
    with plugged(None):
        fake, win, rows = finder_list(BETA)
        assert TX.run_ax(TX.tree(), row_step("Rechnung.pdf", "scopekey:x:finder_folder:path:a:b")).get("success")


# ── the proof ─────────────────────────────────────────────────────────────────────────────────

def storage():
    return WorkflowStorage(base_dir=pathlib.Path(tempfile.mkdtemp(prefix="anyuse-rowscope-")))


def vstore():
    from anyuse.execution.verdict_store import VerdictStore
    return VerdictStore(os.path.join(tempfile.mkdtemp(prefix="anyuse-rowscope-v-"), "verdicts.jsonl"))


ROW_PATH = "window[0]/splitgroup[0]/scrollarea[1]/outline[0]/row[0]/cell[0]"


def finder_draft(scope_key="", bundle=FINDER, app="Finder"):
    box = {"role": "outline", "identifier": "FileList", "description": "",
           "path": "splitgroup[0]/scrollarea[1]/outline[0]", "columns": 2}
    sel = {"canonical_role": "cell", "name": "Rechnung 2026.pdf", "identifier": "", "path": ROW_PATH,
           "raw_role": "AXCell"}
    if scope_key:
        sel["scope"] = scope_key
    data = [({"action": "click", "selector": sel, "container": box}, "select the file"),
            ({"action": "click", "selector": {"canonical_role": "menuitem", "name": "Öffnen",
                                              "identifier": "openDocument:", "path": "", "raw_role": "AXMenuItem"}},
             "open it")]
    ev = [{"id": "e0", "tag_type": "WORKFLOW_START", "timestamp": "2026-10-10T10:00:00", "data": {}}]
    for i, (d, reason) in enumerate(data, 1):
        ev.append({"id": f"e{i}", "tag_type": "STEP", "timestamp": f"2026-10-10T10:00:0{i}", "reason": reason,
                   "data": d})
    ev.append({"id": "e9", "tag_type": "WORKFLOW_END", "timestamp": "2026-10-10T10:00:09", "data": {}})
    return {"name": "open_file", "goal": "g", "session_id": "s", "apps_involved": [app], "urls_visited": [],
            "app": app, "bundle_id": bundle, "events": ev, "anchors": [], "ax_states": {}}


PARAMS = [{"name": "datei", "ref": "@c1", "type": "choice"}]


def _replay(rec, values, world):
    return run(DC.run_steps(rec, values, executor=DC.checked_executor(world), approver=yes,
                            decide_as_driving=True, on_step=world.at_step))


def test_the_proof_takes_the_row_only_in_its_recorded_folder():
    with plugged(FakeScopeAPI()) as api:
        st = storage()
        rep = run(DC.draft_to_workflow(finder_draft(key(api, ALPHA)), overrides=PARAMS, storage=st,
                                       verdicts=vstore()))
        assert rep["status"] == "ready" and rep["proof"]["passed"], rep["reasons"]
        _wf, rec = DC.find_saved("open_file", st)
        assert rec["steps"][0]["command"]["selector"]["scope"] == key(api, ALPHA)
        values = {"datei": "Angebot 7.pdf"}
        # the same-named row, its window showing another folder: refused, nothing selected
        world = PR.SimAXWorld(rec)
        world.add_row_values(values)
        for e in world.elements:
            if e.sel.get("kind") == "row":
                e.sel["scope"] = key(api, BETA)
        out = _replay(rec, values, world)
        assert not out["ok"] and "another folder or note list" in out["stopped"], out["stopped"]
        assert not any(e["op"] == "select_row" for e in world.effects)
        assert any(s.get("reason_code") == "wrong_container" for s in out["steps"])
        # a twin of the row in another folder next to it: never taken; the recorded folder's is
        world = PR.SimAXWorld(rec)
        world.add_row_values(values)
        row = next(e for e in world.elements if e.sel.get("kind") == "row" and e.sel.get("name") == "Angebot 7.pdf")
        twin = PR._Element(dict(row.sel, scope=key(api, BETA)), "AXRow")
        world.elements.insert(0, twin)
        out = _replay(rec, values, world)
        assert out["ok"], out["stopped"]
        picked = [e for box in world.selected.values() for e in box]
        assert picked == [row] and twin not in picked
        # recorded by its name only (the recorded worlds read no path): the same name acts, it proves
        rep = run(DC.draft_to_workflow(finder_draft(key(api, ALPHA_NAME_ONLY)), overrides=PARAMS, save=False,
                                       verdicts=vstore()))
        assert rep["status"] == "ready", rep["reasons"]


def test_an_old_finder_row_without_scope_proves_as_a_name_and_a_twin_of_another_name_is_dropped():
    with plugged(FakeScopeAPI()) as api:
        rep = run(DC.draft_to_workflow(finder_draft(), overrides=PARAMS, save=False, verdicts=vstore()))
        assert rep["status"] == "ready", rep["reasons"]
        st = storage()
        rep = run(DC.draft_to_workflow(finder_draft(key(api, ALPHA_NAME_ONLY)), overrides=PARAMS, storage=st,
                                       verdicts=vstore()))
        _wf, rec = DC.find_saved("open_file", st)
        values = {"datei": "Angebot 7.pdf"}
        world = PR.SimAXWorld(rec)
        world.add_row_values(values)
        row = next(e for e in world.elements if e.sel.get("kind") == "row" and e.sel.get("name") == "Angebot 7.pdf")
        twin = PR._Element(dict(row.sel, scope=key(api, BETA_NAME_ONLY)), "AXRow")
        world.elements.insert(0, twin)
        out = _replay(rec, values, world)
        assert out["ok"], out["stopped"]
        assert [e for box in world.selected.values() for e in box] == [row]


# ── the fill ──────────────────────────────────────────────────────────────────────────────────

def test_the_fill_lists_rows_with_their_scope_and_a_filled_select_row_records_it():
    with plugged(FakeScopeAPI()) as api:
        fake, win, rows = finder_list(ALPHA)
        snap = TX.tree().snapshot(row_labels=True)
        listed = [e for e in snap.elements if getattr(e, "row_label", None)]
        assert listed and {getattr(e, "row_scope", None) for e in listed} == {key(api, ALPHA)}
        assert api.reads == 1                            # read once for the window, not per row
        cands = DF.candidates(snap, rows=True)
        row = next(c for c in cands if c["role"] == "AXRow" and c["label"] == "Rechnung.pdf")
        assert row["selector"].get("scope") == key(api, ALPHA)
        assert "Alpha" not in str(cands) and "fixture" not in str(cands)
        step = DF.planned_step({"action": "select_row", "element": row, "param": "datei"},
                               {"draft_index": 1}, ("Finder", FINDER))
        assert step["command"]["selector"]["scope"] == key(api, ALPHA)
        assert step["command"]["selector"]["name"] == "${datei}"
    # the module not plugged in: rows are listed as before, without a scope
    with plugged(None):
        fake, win, rows = finder_list(ALPHA)
        snap = TX.tree().snapshot(row_labels=True)
    assert all(getattr(e, "row_scope", None) is None for e in snap.elements)


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if getattr(fn, "__module__", "") != __name__:
            continue
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:600]); traceback.print_exc()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f or p != 6 else 0)
