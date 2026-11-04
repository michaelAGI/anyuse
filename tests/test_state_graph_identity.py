"""Defects (a)-(d): one role set, ONE state id, healed fingerprints that match.

Pure logic — no AX, no screen, no keychain, no desktop import. This file exists
because all four defects survived precisely by having no test: the planner's
ImportError, the two state-id schemes, the two ACTIONABLE_ROLES sets and the
similarity-0.0 healed fingerprint are each invisible until something calls them.

Run it through the guarded runner, never with a bare `python test_*.py` sweep
(docs/TEST_POLICY.md).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from anyuse.context.accessibility import ACTIONABLE_ROLES, GENERIC_ROLES
from anyuse.guides.state_graph import (
    UIFingerprint, UIState, StateGraph, UITransition,
    generate_state_id, compute_structural_hash, _role_frequency,
)
from anyuse.tools import migrate_state_graphs as mig


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class _FakeElem:
    """The bit of AXElementDesc that build_perception_context touches."""

    def __init__(self, role, title=""):
        self.role = role
        self.title = title


class _FakeSnapshot:
    def __init__(self, elements):
        self.root_elements = elements


SCREEN = [
    {"role": "AXButton", "title": "Search"},
    {"role": "AXTextField", "title": "Query"},
    {"role": "AXLink", "title": "requests 2.31.0"},
    {"role": "AXLink", "title": "urllib3 2.2.1"},
    {"role": "AXGroup", "title": "results"},
    {"role": "AXStaticText", "title": "5 results"},
]

# The same screen an hour later: the result titles drifted, the structure did not.
SCREEN_LATER = [
    {"role": "AXButton", "title": "Search"},
    {"role": "AXTextField", "title": "Query"},
    {"role": "AXLink", "title": "requests 2.32.0"},
    {"role": "AXLink", "title": "urllib3 2.3.0"},
    {"role": "AXGroup", "title": "results"},
    {"role": "AXStaticText", "title": "7 results"},
]


def _actionable(elements):
    return [e for e in elements if e.get("role") in ACTIONABLE_ROLES]


def _build_state(elements, app, url="", title=""):
    """Drive the generator's builder without constructing storage or a client."""
    from anyuse.guides.generator import GuideGenerator
    gen = GuideGenerator.__new__(GuideGenerator)
    return GuideGenerator._build_state_from_elements(
        gen, elements, app, url, title, "2026-09-21T12:00:00"
    )


# --------------------------------------------------------------------------
# (a) the planner's ImportError
# --------------------------------------------------------------------------

def test_build_perception_context_no_longer_raises():
    """Before: ImportError: cannot import name 'GENERIC_ROLES'. That killed the
    recovery path at exactly the moment a step had failed."""
    from anyuse.execution.planner import ExecutionPlanner

    snap = _FakeSnapshot([
        _FakeElem("AXButton", "Search"), _FakeElem("AXTextField", "Query"),
        _FakeElem("AXGroup"), _FakeElem("AXGroup"), _FakeElem("AXUnknown"),
        _FakeElem("AXWebArea"), _FakeElem("AXCanvas"),
    ])
    ctx = ExecutionPlanner.build_perception_context(snap, {"name": "Safari"})

    assert ctx["ax_element_count"] == 7
    assert ctx["ax_actionable_elements"] == 2, ctx
    # 2×AXGroup + AXUnknown out of 7 elements
    assert ctx["ax_generic_ratio"] == round(3 / 7, 3), ctx
    assert ctx["has_canvas"] is True and ctx["has_web_area"] is True
    print("  (a) build_perception_context runs; generic ratio 3/7")


def test_perception_context_survives_an_empty_snapshot():
    from anyuse.execution.planner import ExecutionPlanner
    ctx = ExecutionPlanner.build_perception_context(None, {})
    assert ctx["ax_element_count"] == 0 and ctx["ax_generic_ratio"] == 0.0
    print("  (a) empty snapshot -> zeros, no division by zero")


# --------------------------------------------------------------------------
# (c) one role set
# --------------------------------------------------------------------------

def test_one_public_role_set():
    from anyuse.context import accessibility
    assert accessibility._ACTIONABLE_ROLES is ACTIONABLE_ROLES, "alias must be the same object"
    assert not (ACTIONABLE_ROLES & GENERIC_ROLES), "a role cannot be both actionable and generic"
    # The roles the generator's discarded local set had that this one does not:
    # dropped on purpose (a container and table content, not controls).
    assert "AXTabGroup" not in ACTIONABLE_ROLES and "AXCell" not in ACTIONABLE_ROLES
    # …and the ones it gains, which are genuine controls.
    for role in ("AXMenuButton", "AXSecureTextField", "AXSearchField",
                 "AXStepper", "AXDisclosureTriangle"):
        assert role in ACTIONABLE_ROLES, role
    print(f"  (c) one set, {len(ACTIONABLE_ROLES)} actionable / {len(GENERIC_ROLES)} generic roles")


def test_generator_defines_no_second_role_set():
    src = Path(__file__).resolve().parent.parent / "guides" / "generator.py"
    text = src.read_text()
    assert "ACTIONABLE_ROLES = {" not in text, "generator.py must import the set, not redefine it"
    assert "from anyuse.context.accessibility import ACTIONABLE_ROLES" in text
    print("  (c) guides/generator.py imports the shared set")


# --------------------------------------------------------------------------
# (b) ONE state id
# --------------------------------------------------------------------------

def test_state_id_is_title_insensitive():
    a = generate_state_id(_actionable(SCREEN), "Safari")
    b = generate_state_id(_actionable(SCREEN_LATER), "Safari")
    assert a == b, f"title drift must not re-mint a node: {a} != {b}"
    print(f"  (b) title drift keeps one id: {a}")


def test_state_id_discriminates_structure_and_app():
    more = SCREEN + [{"role": "AXButton", "title": "Next page"}]
    assert generate_state_id(_actionable(SCREEN), "Safari") != generate_state_id(_actionable(more), "Safari")
    assert generate_state_id(_actionable(SCREEN), "Safari") != generate_state_id(_actionable(SCREEN), "Finder")
    print("  (b) a structural change and a different app still separate states")


def test_generator_and_recorder_mint_the_same_id():
    """The whole point of (b): both producers on the same screen, one id.

    The recorder's call site is ``generate_state_id(actionable_dicts, app)``
    (memory/recorder.py), which is what the left-hand side is; the right-hand
    side is what the generator actually produces.
    """
    recorder_id = generate_state_id(_actionable(SCREEN), "Safari")
    state = _build_state(SCREEN, "Safari")
    assert state is not None
    assert state.state_id == recorder_id, (state.state_id, recorder_id)
    # …and still equal after the titles drift.
    assert _build_state(SCREEN_LATER, "Safari").state_id == recorder_id
    print(f"  (b) recorder id == generator id == {recorder_id}")


def test_recorder_delegates_rather_than_reimplementing():
    src = (Path(__file__).resolve().parent.parent / "memory" / "recorder.py").read_text()
    assert "return generate_state_id(elements, app_name)" in src
    assert "hexdigest" not in src.split("def _generate_state_id")[1].split("def ")[0]
    print("  (b) memory/recorder._generate_state_id is a thin alias")


def test_two_producers_now_share_a_graph_node():
    """End to end: add the same screen twice, once per producer shape."""
    graph = StateGraph()
    recorded = UIState(state_id=generate_state_id(_actionable(SCREEN), "Safari"),
                       app="Safari", times_seen=1)
    observed = _build_state(SCREEN_LATER, "Safari")
    graph.add_state(recorded)
    graph.add_state(observed)
    assert len(graph.states) == 1, f"expected one merged node, got {list(graph.states)}"
    print("  (b) one node, not two")


# --------------------------------------------------------------------------
# (d) healed fingerprints carry role_frequency
# --------------------------------------------------------------------------

class _ResultWithAX:
    """An ExecutionResult enriched the way _fingerprint_for_discovered_state probes for."""

    def __init__(self, mapping):
        self.state_fingerprints = mapping


def test_healed_fingerprint_carries_role_frequency():
    from anyuse.execution.self_healing import SelfHealingManager

    fp = SelfHealingManager._fingerprint_for_discovered_state(
        _ResultWithAX({"safari_abc": SCREEN}), "safari_abc"
    )
    assert fp is not None
    assert fp.role_frequency, "a healed fingerprint with no role_frequency can never match"
    assert fp.role_frequency == _role_frequency(SCREEN)
    print(f"  (d) healed fingerprint carries {len(fp.role_frequency)} roles")


def test_healed_fingerprint_similarity_is_not_zero():
    """The observable consequence of (d): before, this was 0.0 for every screen."""
    from anyuse.execution.self_healing import SelfHealingManager

    fp = SelfHealingManager._fingerprint_for_discovered_state(
        _ResultWithAX({"s": SCREEN}), "s"
    )
    same = fp.matches(SCREEN)
    assert same["structural_similarity"] == 1.0, same
    drifted = fp.matches(SCREEN_LATER)
    assert drifted["structural_similarity"] == 1.0, drifted  # same roles, drifted titles

    other = [{"role": "AXButton"}] * 20
    assert fp.matches(other)["structural_similarity"] < 0.5
    print("  (d) similarity 1.0 on the same screen, <0.5 on a different one")


def test_generator_fingerprint_carries_role_frequency_too():
    state = _build_state(SCREEN, "Safari")
    assert state.fingerprint.role_frequency == _role_frequency(SCREEN)
    assert state.fingerprint.matches(SCREEN)["structural_similarity"] == 1.0
    print("  (d) generator-built fingerprints match their own screen")


def test_placeholder_fingerprint_is_still_marked_not_silently_filled():
    """The bare UIFingerprint() in self_healing is DELIBERATE: it is the marked
    'no AX data was available' placeholder the drift gate knows about. It must
    stay empty rather than get a fabricated role_frequency."""
    from anyuse.execution import self_healing
    fp = self_healing.SelfHealingManager._fingerprint_for_discovered_state(
        _ResultWithAX({}), "missing"
    )
    assert fp is None, "no AX data must yield no fingerprint, not an invented one"
    assert UIFingerprint().role_frequency == {}
    print("  (d) the marked placeholder path is untouched")


# --------------------------------------------------------------------------
# the migration tool
# --------------------------------------------------------------------------

def _legacy_generator_id(elements, app):
    """Reproduce the OLD title-sensitive id, so the fixture is a real artefact."""
    import hashlib
    import re
    old_roles = {
        "AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton",
        "AXPopUpButton", "AXComboBox", "AXSlider", "AXLink", "AXMenuItem",
        "AXMenuBarItem", "AXTab", "AXTabGroup", "AXIncrementor", "AXSwitch", "AXCell",
    }
    actionable = [e for e in elements if e.get("role", "") in old_roles]
    canonical = sorted((e.get("role", ""), e.get("title", "")) for e in actionable)
    payload = json.dumps([app.lower()] + canonical, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"{re.sub(r'[^a-z0-9]', '_', app.lower())[:20]}_{digest}"


def _legacy_guide_doc():
    """A guide as it would have been persisted BEFORE the fix: the same screen
    stored twice under two ids, plus a transition that only the generator's node
    knows about — which is how find_path lost routes that existed."""
    rec_id = generate_state_id(_actionable(SCREEN), "Safari")
    gen_id = _legacy_generator_id(SCREEN_LATER, "Safari")
    assert rec_id != gen_id, "the fixture must actually exhibit the split"

    def state(sid, elements, seen, with_freq):
        required = [{"role": e["role"], "title": e["title"]} for e in _actionable(elements)]
        fp = {
            "required_elements": required,
            "element_count_range": [0, 99],
            "structural_hash": compute_structural_hash(elements),
        }
        if with_freq:
            fp["role_frequency"] = _role_frequency(elements)
        return {"state_id": sid, "app": "Safari", "fingerprint": fp, "elements": [],
                "url_patterns": [f"https://example.org/{sid}"], "title_patterns": [],
                "times_seen": seen, "last_seen": "2026-09-20T10:00:00",
                "custom_render_notes": []}

    detail = generate_state_id(_actionable(SCREEN + [{"role": "AXButton", "title": "Install"}]), "Safari")
    return {
        "name": "example.org",
        "ui_states": {
            rec_id: state(rec_id, SCREEN, 7, True),
            gen_id: state(gen_id, SCREEN_LATER, 2, False),   # legacy: no role_frequency
            detail: state(detail, SCREEN + [{"role": "AXButton", "title": "Install"}], 3, True),
        },
        "ui_transitions": [
            {"from_state": gen_id, "to_state": detail, "intent": "open result",
             "implementations": []},
        ],
    }, rec_id, gen_id, detail


def test_migration_merges_the_split_node():
    doc, rec_id, gen_id, detail = _legacy_guide_doc()
    new_doc, rep = mig.migrate_document(doc)

    assert rep.states_scanned == 3
    assert rep.ids_rekeyed == 1, rep.rekeys
    assert rep.states_merged == 1, rep.merges
    assert rep.role_frequency_added == 1
    assert rep.transitions_rewritten == 1
    assert rep.transitions_dangling == 0

    assert set(new_doc["ui_states"]) == {rec_id, detail}, list(new_doc["ui_states"])
    merged = new_doc["ui_states"][rec_id]
    assert merged["times_seen"] == 9, merged["times_seen"]     # 7 + 2
    assert len(merged["url_patterns"]) == 2                    # both observations kept
    # The richer node (times_seen 7) kept its fingerprint whole.
    assert merged["fingerprint"]["role_frequency"] == _role_frequency(SCREEN)

    t = new_doc["ui_transitions"][0]
    assert t["from_state"] == rec_id and t["to_state"] == detail
    print(f"  migration: {gen_id} -> {rec_id}, 2 nodes into 1, transition re-pointed")


def test_migration_restores_a_route_that_was_lost():
    """The damage (b) actually does: a path exists in reality but not in the graph."""
    doc, rec_id, gen_id, detail = _legacy_guide_doc()

    before = StateGraph.from_dict({"states": doc["ui_states"], "transitions": doc["ui_transitions"]})
    assert before.find_path(rec_id, detail) == [], "the fixture must start broken"

    new_doc, _ = mig.migrate_document(doc)
    after = StateGraph.from_dict({"states": new_doc["ui_states"], "transitions": new_doc["ui_transitions"]})
    path = after.find_path(rec_id, detail)
    assert len(path) == 1 and path[0].to_state == detail, path
    print("  migration: find_path(list -> detail) 0 hops before, 1 after")


def test_migration_is_idempotent():
    doc, *_ = _legacy_guide_doc()
    once, _ = mig.migrate_document(doc)
    twice, rep = mig.migrate_document(once)
    assert once == twice
    assert rep.ids_rekeyed == 0 and rep.states_merged == 0 and rep.role_frequency_added == 0
    print("  migration: running it twice changes nothing the second time")


def test_migration_leaves_placeholder_states_alone():
    """Self-healing placeholders carry app="" — there is no canonical id for them."""
    doc = {"ui_states": {"pending_x": {"state_id": "pending_x", "app": "",
                                       "fingerprint": {"required_elements": [], "role_frequency": {}},
                                       "times_seen": 1}},
           "ui_transitions": []}
    new_doc, rep = mig.migrate_document(doc)
    assert rep.skipped_no_app == 1 and rep.ids_rekeyed == 0
    assert "pending_x" in new_doc["ui_states"]
    print("  migration: app-less placeholders are reported and skipped")


def test_migration_dry_run_writes_nothing():
    doc, *_ = _legacy_guide_doc()
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "example_org.json"
        f.write_text(json.dumps(doc, indent=2))
        before = f.read_bytes()
        rep = mig.migrate_path(Path(tmp))          # no write=
        assert rep.dry_run is True
        assert f.read_bytes() == before, "a dry run must not touch the file"
        assert rep.totals()["ids_rekeyed"] == 1
        assert list(Path(tmp).iterdir()) == [f], "a dry run must not create a backup either"
        print("  migration: dry run is the default and is inert")


def test_migration_write_keeps_a_backup():
    doc, rec_id, gen_id, detail = _legacy_guide_doc()
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "example_org.json"
        f.write_text(json.dumps(doc, indent=2))
        rep = mig.migrate_path(Path(tmp), write=True)
        assert rep.dry_run is False
        after = json.loads(f.read_text())
        assert set(after["ui_states"]) == {rec_id, detail}
        bak = Path(tmp) / "example_org.json.pre-migration.bak"
        assert bak.exists() and set(json.loads(bak.read_text())["ui_states"]) == set(doc["ui_states"])
        print("  migration: --write applies and leaves example_org.json.pre-migration.bak")


def test_migration_refuses_the_live_data_dir():
    """It must be impossible to migrate the user's live guides by accident —
    including from a process whose HOME has been redirected."""
    import pwd
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    live = real_home / ".anyuse" / "app_guides"
    assert mig._is_under_real_home_anyuse(live) is True
    assert mig._is_under_real_home_anyuse(Path(tempfile.gettempdir())) is False
    try:
        mig.migrate_path(live, write=True)
    except PermissionError as exc:
        assert "live AnyUse data directory" in str(exc)
    except FileNotFoundError:
        pass  # the directory does not exist here; the guard above is what matters
    else:
        raise AssertionError("writing into the live data dir must be refused")
    print("  migration: --write into the real ~/.anyuse is refused without an explicit flag")


def test_migration_ignores_non_guide_json():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.json").write_text(json.dumps({"example.org": {"filename": "x.json"}}))
        (Path(tmp) / "notes.json").write_text(json.dumps({"unrelated": True}))
        rep = mig.migrate_path(Path(tmp))
        assert rep.files == [], "only guide documents should be considered"
        print("  migration: index.json and unrelated JSON are skipped")


def test_migration_cli_dry_run_exits_zero():
    import contextlib
    import io

    doc, *_ = _legacy_guide_doc()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "g.json").write_text(json.dumps(doc))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_json = mig.main(["--path", tmp, "--json"])
            rc_human = mig.main(["--path", tmp])
        assert rc_json == 0 and rc_human == 0
        payload = json.loads(buf.getvalue().split("=== state-graph")[0])
        assert payload["dry_run"] is True and payload["totals"]["ids_rekeyed"] == 1
        assert "DRY RUN (nothing written)" in buf.getvalue()
        with contextlib.redirect_stdout(io.StringIO()):
            assert mig.main(["--path", str(Path(tmp) / "missing.json")]) == 2
        print("  migration: CLI dry run exits 0 and says so; a bad path exits 2")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"test_state_graph_identity: {len(tests)} tests")
    for t in tests:
        t()
    print(f"OK — {len(tests)} passed")
