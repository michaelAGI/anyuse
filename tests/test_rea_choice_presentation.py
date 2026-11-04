"""Semantic projection preserves task meaning and its complete source binding."""
import copy
import hashlib
import json

import pytest

from anyuse.execution import rea_choice_presentation as P
from anyuse.execution import rea_actions as A
from anyuse.execution import rea_mapping as R


def fixture():
    return {"format": A.CHOICE_VIEW, "catalog_sha256": "0" * 64,
            "task": "Pause the current video.", "actions": [{
                "id": "pause_current", "summary": "Pause the current playing video",
                "details": "OPERATIONAL_EVIDENCE_PROSE: nine trials on special storage",
                "candidate_sha256": "1" * 64, "card_sha256": "2" * 64,
                "contract": {"effect": "fixed_state_transition", "params": [],
                    "scope": "fixed_transition_across_declared_trials_with_heldout_restart",
                    "reference": None, "policy": {"format": P.PLAYBACK, "id": "pause_current",
                        "kind": "fixed_state_transition", "params": [], "before": "playing", "after": "paused",
                        "input_scope": "fixed_no_actor_inputs", "heldout_scope": "fresh_trials_with_declared_restart",
                        "target": {"bundle_id": "org.example.video", "version": "1.5.0", "build": "180",
                                   "sha256": "3" * 64, "bytes": 12345, "files": 8},
                        "observer": {"format": "anyuse.rea-temporal-playback-observer/1",
                            "control": {"path": "AX_INDEX_SECRET", "identifier": "CONTROL_ID_SECRET"},
                            "video": {"region": [.1, .55, .8, .2], "black_luma": 10,
                                      "min_nonblack_fraction": .9}},
                        "phase_minimums": {"learning": 3, "fresh": 3, "heldout": 3}}}}]}


def payload(state):
    return json.loads(state.split("\n", 1)[1])


def test_playback_preserves_before_after_actual_inputs_target_scope_and_observation_boundary():
    view = fixture()
    state, questions, manifest = P.render(view)
    data = payload(state)
    card = data["available_actions"][0]
    assert data["requested_task"] == view["task"]
    assert card["before"] == "video is playing" and card["after"] == "video is paused"
    assert card["arguments"] == {"params": [], "input_scope": "fixed_no_actor_inputs"}
    assert card["application"] == {"bundle_id": "org.example.video", "version": "1.5.0", "build": "180"}
    assert card["measured_scope"] == view["actions"][0]["contract"]["scope"]
    assert card["heldout_scope"] == "fresh_trials_with_declared_restart"
    assert card["observation_boundary"] == "Only the declared video region on one display was observed."
    assert questions["pick"]["instructions"] == P.QUESTION
    assert manifest["confidence_basis"]["default_minimum_confidence"] == .8
    assert manifest["confidence_basis"]["laya_choice"] == "normalized_shannon_entropy"


def test_operational_fields_and_prose_are_omitted_explicitly_without_leaking_into_input():
    view = fixture()
    state, questions, manifest = P.render(view)
    model_text = state + json.dumps(questions)
    for text in ("AX_INDEX_SECRET", "CONTROL_ID_SECRET", "OPERATIONAL_EVIDENCE_PROSE",
                 "min_nonblack_fraction", "black_luma", "phase_minimums", "3" * 64):
        assert text not in model_text
    omitted = manifest["omitted_fields"]
    assert "/actions/0/details" in omitted
    assert "/actions/0/contract/policy/observer/control" in omitted
    assert "/actions/0/contract/policy/observer/video" in omitted
    assert "/actions/0/contract/policy/target/sha256" in omitted
    assert manifest["actions"][0]["policy_sha256"] == R.digest(view["actions"][0]["contract"]["policy"])


def test_no_mutation_and_hashes_are_deterministic_even_for_reordered_object_keys():
    view = fixture()
    original = copy.deepcopy(view)
    first = P.render(view)
    assert view == original
    second = P.render(json.loads(json.dumps(view, sort_keys=True)))
    assert first == second
    state, questions, manifest = first
    assert manifest["choice_view_sha256"] == R.digest(view)
    assert manifest["state_sha256"] == hashlib.sha256(state.encode()).hexdigest()
    assert manifest["questions_sha256"] == R.digest(questions)
    assert manifest["input_sha256"] == R.digest({"state": state, "questions": questions})


def test_changed_omitted_evidence_changes_full_source_binding_without_changing_model_input():
    view = fixture()
    state, questions, manifest = P.render(view)
    view["actions"][0]["contract"]["policy"]["observer"]["control"]["path"] = "NEW_AX_INDEX_SECRET"
    changed_state, changed_questions, changed_manifest = P.render(view)
    assert (state, questions) == (changed_state, changed_questions)
    assert manifest["choice_view_sha256"] != changed_manifest["choice_view_sha256"]
    assert manifest["actions"][0]["policy_sha256"] != changed_manifest["actions"][0]["policy_sha256"]


def test_changed_actual_target_changes_model_input_and_keeps_exact_value():
    view = fixture()
    original = P.render(view)
    view["actions"][0]["contract"]["policy"]["target"]["build"] = "181-preview"
    changed = P.render(view)
    assert original[0] != changed[0]
    assert payload(changed[0])["available_actions"][0]["application"]["build"] == "181-preview"


def test_other_contract_families_keep_complete_contract_summary_and_details():
    view = fixture()
    card = view["actions"][0]
    card["contract"] = {"effect": "create_document", "params": [{"name": "text", "type": "string"}],
                        "scope": {"app": "notes", "restrictions": ["new documents only"]},
                        "policy": {"format": "other_policy/1", "unknown_future_semantics": "must survive"}}
    state, _, manifest = P.render(view)
    rendered = payload(state)["available_actions"][0]
    assert rendered["contract"] == card["contract"]
    assert rendered["summary"] == card["summary"] and rendered["details"] == card["details"]
    assert manifest["actions"][0]["projection"] == "complete_contract"
    assert not any(p.startswith("/actions/0/contract") for p in manifest["omitted_fields"])


def test_eight_action_ids_use_exact_a_labels_and_disjoint_none_even_when_an_id_is_none():
    view = fixture()
    card = view["actions"][0]
    card["contract"] = {"effect": "opaque", "params": [], "scope": "declared"}
    card["details"] = "Complete generic contract"
    view["actions"] = [dict(copy.deepcopy(card), id=("none" if i == 0 else "action_" + str(i))) for i in range(8)]
    state, questions, manifest = P.render(view)
    assert list(questions["pick"]["criteria"]) == ["a" + str(i) for i in range(8)] + ["none"]
    assert questions["pick"]["criteria"]["none"] == P.NONE
    assert [c["action_id"] for c in payload(state)["available_actions"]] == [c["id"] for c in view["actions"]]
    assert [a["choice"] for a in manifest["actions"]] == ["a" + str(i) for i in range(8)]
    assert manifest["actions"][0]["action_id"] == "none" and manifest["none_label"] == "none"


@pytest.mark.parametrize("change", [
    lambda c: c["policy"].update(before="paused"),
    lambda c: c["policy"].update(after="playing"),
    lambda c: c.update(params=[{"name": "unexpected_input"}]),
    lambda c: c["policy"].update(params=["unexpected_input"]),
    lambda c: c["policy"]["target"].pop("version"),
])
def test_contradictory_playback_contract_is_refused_instead_of_inventing_pause_semantics(change):
    view = fixture()
    change(view["actions"][0]["contract"])
    with pytest.raises(ValueError, match="measured zero-input"):
        P.render(view)


def test_full_view_validation_refuses_duplicate_action_ids_before_rendering():
    view = fixture()
    view["actions"].append(copy.deepcopy(view["actions"][0]))
    with pytest.raises(ValueError):
        P.render(view)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
