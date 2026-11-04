"""Inert source admission fixtures; no vendor/app ABI or lifetime is proved."""
import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import time

import httpx
import pytest

from anyuse.execution import model_adapter as M
from anyuse.execution import rea_native_path as P
from anyuse.execution import rea_native_path_builder as N
from anyuse.execution import rea_runtime_bridge as B
from test_rea_native_path import _path_actor


def pin(file):
    raw = file.read_bytes()
    return {"file": str(file), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def write(file, value):
    file.write_text(json.dumps(value))
    return pin(file)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(N.R, "assert_guest", lambda: None)
    root = tmp_path / "session"
    source = root / "s0000"
    source.mkdir(parents=True)
    folder = root / "s0001"
    folder.mkdir()
    image = tmp_path / "Owned.app" / "Contents" / "MacOS" / "Owned"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"inert original image bytes; not a real native ABI")
    image_pin = pin(image)
    header = root / "source-originals/owned.h"
    header.parent.mkdir()
    header.write_text("// Inert synthetic source fixture; assertions below are not app proof.\n"
                      "struct Node { unsigned generation; int payload; };\n"
                      "struct Owner { bool ready; Node *child; };\n")
    original_header = tmp_path / "original-owned.h"
    original_header.write_bytes(header.read_bytes())
    code = source / "code.json"
    write(code, {"format": "anyuse.rea-original-instruction-range/1", "image": image_pin,
                 "original_unchanged": True, "task_proof": False,
                 "range": {"image_offset": "0x1000", "bytes": 4, "original_hex": "1f2003d5"}})
    _, recipe = _path_actor()
    old_module = recipe["types"][0]["owner_image"]
    module = {"path": str(image), "sha256": image_pin["sha256"]}

    def substitute(value):
        if type(value) is dict:
            if value == old_module:
                return copy.deepcopy(module)
            return {key: substitute(child) for key, child in value.items()}
        if type(value) is list:
            return [substitute(child) for child in value]
        return value

    recipe = substitute(recipe)
    for owner, key in N._slots(recipe):
        owner.pop(key)
    config = M.ModelConfig("local", "http://fixture.invalid/v1", "independent-inert-reviewer", "",
                           timeout=.01, max_tokens=2048, retries=0)
    state = {"bundle": str(image.parents[2]), "target": {"bundle_id": "inert.owned"},
             "task": "SECRET TASK MUST NOT REACH REVIEWER", "params": [{"name": "payload", "type": "int"}],
             "plan": {"heldout": "SECRET HELDOUT MUST NOT REACH REVIEWER"}, "status": "investigating", "deadline": time.time() + 120,
             "steps": [{"status": "completed", "request": {"op": "static_evidence", "request": {"kind": "original_code"}}},
                       {"status": "started"}],
             "artifacts": {file.relative_to(root).as_posix(): pin(file) for file in (header, code)},
             "native_path_review": {"format": N.POLICY, "mode": "separate_model", "model": asdict(config), "max_calls": 1,
                                    "source_pins": [{"role": "header", **pin(original_header)}]},
             "native_path_source_imports": {"source-originals/owned.h": {"role": "header", "original": pin(original_header),
                                                                          "copy": pin(header)}}}
    request = {"op": "build_native_path", "recipe": recipe,
               "originals": [{"role": "header", "artifact": "source-originals/owned.h", "pointer": ""},
                             {"role": "original_build", "artifact": "s0000/code.json", "pointer": "/image"}],
               "review": {"mode": "model"}}
    state["steps"][1]["request"] = copy.deepcopy(request)
    return root, state, request, folder, config


def model(setup, status="supported", *, hook=None, malformed=None):
    seen = []
    config = setup[-1]

    def post(request):
        value = json.loads(request.content)
        seen.append(value)
        if hook:
            hook()
        data = json.loads(value["messages"][1]["content"])
        reply = {"declarations": [{"index": index, "status": status,
                   "source_artifacts": ["source-originals/owned.h", "s0000/code.json"],
                   "rationale": "Inert fixture judgment only; this does not prove a real native ABI."}
                  for index in range(len(data["declarations"]))]}
        return httpx.Response(200, json={"choices": [{"message": {"content": malformed if malformed is not None else json.dumps(reply)},
                                                       "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

    return M.ModelAdapter(config, transport=httpx.MockTransport(post)), seen


def complete(setup, *, status="supported", malformed=None):
    root, state, request, folder, _ = setup
    adapter, seen = model(setup, status, malformed=malformed)
    try:
        result = N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    if "recipe" in result:
        state["steps"][1]["status"] = "completed"
        for file in folder.glob("*.json"):
            state["artifacts"][file.relative_to(root).as_posix()] = pin(file)
        write(root / "session.json", state)
    return result, seen


def test_separate_model_review_produces_exact_pins_without_task_or_trial_values(setup):
    root, state, request, folder, _ = setup
    before = copy.deepcopy(request)
    result, seen = complete(setup)
    assert request == before and result["status"] == "native_path_source_reviewed"
    assert result["model_calls"] == len(seen) == 1
    assert result["source_review_fallible"] and not result["abi_recovered"] and not result["proof_created"]
    assert not result["actuated"] and result["type_basis"] == "separate_model_source_review"
    payload = json.dumps(seen)
    assert "SECRET TASK" not in payload and "SECRET HELDOUT" not in payload
    assert "original_image_bytes_sent" in payload and "inert original image bytes" not in payload
    recipe = result["recipe"]
    assert B._native_path(recipe) == recipe
    admission = json.loads((folder / "admission.json").read_text())
    contracts = json.loads((folder / "contracts.json").read_text())
    for row in P.evidence_plan(recipe):
        assert B._json_pointer(contracts, row["claim"]["pointer"]) == row["contract"]
        assert row["claim"]["sha256"] == pin(folder / "contracts.json")["sha256"]
        assert row["originals"][-1] == {"role": "independent_review", "file": str(folder / "admission.json"),
                                         "sha256": pin(folder / "admission.json")["sha256"]}
        P.validate_admission(admission, recipe, row)
    resources = N.resources(recipe)
    assert str(folder / "review-input.json") in resources and str(folder / "review-output.json") in resources
    assert str(root / "session.json") not in resources


@pytest.mark.parametrize("status", ["uncertain", "unsupported"])
def test_nonpositive_source_judgment_never_creates_admission_or_recipe(setup, status):
    result, seen = complete(setup, status=status)
    assert result["status"] == "needs_capability" and result["model_calls"] == len(seen) == 1
    assert "recipe" not in result and not (setup[3] / "contracts.json").exists()
    assert not (setup[3] / "admission.json").exists()
    assert (setup[3] / "review-output.json").exists()


def test_malformed_reviewer_answer_is_retained_boundary_not_source_admission(setup):
    result, seen = complete(setup, malformed="{not JSON}")
    assert result["status"] == "needs_capability" and result["model_calls"] == len(seen) == 1
    assert "recipe" not in result
    output = json.loads((setup[3] / "review-output.json").read_text())
    assert output["answer"]["text"] == "{not JSON}"


@pytest.mark.parametrize("change", ["no_source", "no_correlation", "no_policy", "ghidra_as_ir", "binary_dwarf"])
def test_missing_authentic_source_capability_does_not_call_model(setup, change):
    root, state, request, folder, _ = setup
    if change == "no_source":
        request["originals"].pop(0)
    elif change == "no_correlation":
        request["originals"].pop()
    elif change == "no_policy":
        state["native_path_review"] = None
    elif change == "ghidra_as_ir":
        request["originals"][0] = {"role": "original_ir", "artifact": "s0000/code.json", "pointer": ""}
    else:
        file = root / "source-originals/owned.h"
        file.write_bytes(b"\x00\xff\x00DWARF")
        imported = state["native_path_source_imports"]["source-originals/owned.h"]
        original = Path(imported["original"]["file"])
        original.write_bytes(file.read_bytes())
        imported.update(role="dwarf", original=pin(original), copy=pin(file))
        state["artifacts"]["source-originals/owned.h"] = pin(file)
        state["native_path_review"]["source_pins"] = [{"role": "dwarf", **pin(original)}]
        request["originals"][0]["role"] = "dwarf"
    adapter, seen = model(setup)
    try:
        result = N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert result["status"] == "needs_capability" and result["model_calls"] == len(seen) == 0
    assert "recipe" not in result and not (folder / "admission.json").exists()


@pytest.mark.parametrize("change", ["source", "recipe", "policy", "request", "review", "review_input", "admission", "sources"])
def test_reviewed_path_rechecks_full_producer_closure_before_any_root_call(setup, change):
    root, state, request, folder, _ = setup
    result, _ = complete(setup)
    recipe = result["recipe"]
    if change == "source":
        (root / "source-originals/owned.h").write_text("changed source")
    elif change == "recipe":
        recipe["preconditions"][0]["equals"] = False
    elif change in {"policy", "request"}:
        if change == "policy":
            state["native_path_review"]["model"]["model"] = "other-reviewer"
        else:
            state["steps"][1]["request"]["recipe"]["preconditions"][0]["equals"] = False
        write(root / "session.json", state)
    else:
        file = folder / {"review": "review-output.json", "review_input": "review-input.json",
                         "admission": "admission.json", "sources": "contracts.json"}[change]
        value = json.loads(file.read_text())
        value["changed"] = True
        write(file, value)
    with pytest.raises((ValueError, EVIDENCE_ERROR)):
        N.resources(recipe)


EVIDENCE_ERROR = N.E.EvidenceIntegrityError


def test_reviewer_must_be_fresh_exact_config_and_cannot_change_frozen_policy(setup):
    root, state, request, folder, _ = setup
    adapter, seen = model(setup)
    adapter.config = replace(adapter.config, model="discovery-agent")
    try:
        with pytest.raises(ValueError, match="fresh separately configured"):
            N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert seen == []


def test_source_policy_change_during_actual_review_is_rejected(setup):
    root, state, request, folder, _ = setup
    adapter, seen = model(setup, hook=lambda: state["native_path_review"]["model"].update(model="changed"))
    try:
        with pytest.raises(ValueError, match="changed during review"):
            N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert len(seen) == 1 and not (folder / "admission.json").exists()


def test_raw_analysis_text_cannot_be_promoted_to_original_ir(setup):
    root, state, request, folder, _ = setup
    file = root / "s0000/decompiled.txt"
    file.write_text("void guessed_type::guess() { /* decompiler pseudocode */ }")
    state["artifacts"]["s0000/decompiled.txt"] = pin(file)
    state["steps"][0]["request"] = {"op": "analyze"}
    request["originals"][0] = {"role": "original_ir", "artifact": "s0000/decompiled.txt", "pointer": ""}
    adapter, seen = model(setup)
    try:
        result = N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert result["status"] == "needs_capability" and "pseudocode" in result["reason"] and seen == []


def test_original_source_is_rechecked_even_when_retained_copy_is_unchanged(setup):
    root, state, request, folder, _ = setup
    original = Path(state["native_path_source_imports"]["source-originals/owned.h"]["original"]["file"])
    original.write_text("changed original; retained copy is unchanged")
    adapter, seen = model(setup)
    try:
        with pytest.raises(EVIDENCE_ERROR):
            N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert seen == []


def test_reviewer_timeout_must_fit_remaining_session_deadline_before_call(setup):
    root, state, request, folder, _ = setup
    state["deadline"] = time.time() - 1
    adapter, seen = model(setup)
    try:
        with pytest.raises(ValueError, match="remaining session deadline"):
            N.build(root, state, request, folder, adapter=adapter)
    finally:
        adapter.close()
    assert seen == []


def test_source_review_cli_policy_requires_tool_free_provider_and_effective_timeout(setup):
    policy = copy.deepcopy(setup[1]["native_path_review"])
    policy["model"]["provider"] = "codex-cli"
    with pytest.raises(ValueError, match="tool-free"):
        N.validate_policy(policy)
    policy["model"]["provider"] = "claude-cli"
    with pytest.raises(ValueError, match="bounded, retry-free"):
        N.validate_policy(policy)
    policy["model"]["timeout"] = M.CLI_TIMEOUT_S
    assert N.validate_policy(policy) == policy


@pytest.mark.parametrize("filename", ["contracts.json", "admission.json", "review-input.json", "review-output.json"])
def test_rewriting_receipt_and_resealing_actor_cannot_replace_registered_producer_pin(setup, filename):
    _, _, _, folder, _ = setup
    result, _ = complete(setup)
    file = folder / filename
    # Semantically identical JSON used to pass the shape/correlation checks.
    # Its retained producing artifact must still match byte for byte.
    file.write_text(file.read_text() + "\n")
    recipe = result["recipe"]
    for owner, key in N._slots(recipe):
        if filename == "contracts.json":
            owner[key]["claim"]["sha256"] = pin(file)["sha256"]
        elif filename == "admission.json":
            owner[key]["originals"][-1]["sha256"] = pin(file)["sha256"]
    with pytest.raises((ValueError, EVIDENCE_ERROR)):
        N.resources(recipe)


@pytest.mark.parametrize("change", ["placeholder", "root_args", "layout", "unknown_input", "retry_policy"])
def test_existing_type_lifetime_and_call_gates_remain_closed(setup, change):
    _, state, request, _, _ = setup
    if change == "placeholder":
        request["recipe"]["root_evidence"] = {}
    elif change == "root_args":
        request["recipe"]["root"]["getter"]["arguments"] = [{"type": "int32", "value": 0}]
    elif change == "layout":
        request["recipe"]["types"][0]["fields"][1]["offset"] = 1
    elif change == "unknown_input":
        request["originals"][0]["role"] = "decompiler"
    else:
        state["native_path_review"]["model"]["retries"] = 1
        with pytest.raises(ValueError, match="retry-free"):
            N.validate_policy(state["native_path_review"])
        return
    with pytest.raises(ValueError):
        N.validate_request(request)


def test_registered_review_must_match_exact_contracts_and_is_not_invented(setup):
    root, state, request, folder, _ = setup
    reader = N.Reader()
    originals, _, _ = N._originals(reader, root, state, request)
    temporary = N._decorate(request["recipe"], folder / "contracts.json", "0" * 64, originals,
                            folder / "admission.json", "0" * 64)
    admission = {"format": P.ADMISSION, "abi": dict(P.ABI), "images": P.image_claims(temporary),
                 "declarations": [P.admission_declaration(row) for row in P.evidence_plan(temporary)]}
    review_file = root / "s0000/source-review.json"
    state["artifacts"]["s0000/source-review.json"] = write(review_file, admission)
    request["review"] = {"mode": "registered", "artifact": "s0000/source-review.json"}
    state["steps"][1]["request"] = copy.deepcopy(request)
    result = N.build(root, state, request, folder)
    assert result["type_basis"] == "caller_supplied_registered_source_review" and result["model_calls"] == 0
    state["steps"][1]["status"] = "completed"
    for file in folder.glob("*.json"):
        state["artifacts"][file.relative_to(root).as_posix()] = pin(file)
    write(root / "session.json", state)
    assert str(review_file) in N.resources(result["recipe"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
