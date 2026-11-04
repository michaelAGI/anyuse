"""Explicit backend routing and accounting never imply an executed app action."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

pkgroot = Path(tempfile.mkdtemp(prefix="anyuse-rea-picker-pkg-"))
(pkgroot / "anyuse").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
sys.path.insert(0, str(pkgroot))

from anyuse.execution import openjev as O, rea_picker as P
from anyuse.execution.model_adapter import RunBudget, process_call_count


QUESTION = {"pick": {"type": "choice", "instructions": "Choose the matching operation.",
                     "criteria": {"a": "Pause", "none": "No match"}}}


def response(url, body, headers, timeout):
    return 200, {}, json.dumps({"answers": {"pick": {"choice": "a", "confidence": .95}},
                               "usage": {"input_tokens": 12, "output_tokens": 1, "cost": .0001}}).encode()


@pytest.mark.parametrize("kind,path,with_model", [(P.DjevClient, "/v1/request", False),
                                                  (P.RecordedClient, "/v1/systemone", True)])
def test_actual_wire_receipt_matches_provider_payload(kind, path, with_model):
    sent = []
    def post(url, body, headers, timeout):
        sent.append((url, body, headers))
        return response(url, body, headers, timeout)
    client = kind(url="http://127.0.0.1:8000", poster=post, key="")
    mark = process_call_count()
    answer = client.decide("An app is playing video.", QUESTION)
    assert not answer.error and answer.answers["pick"]["choice"] == "a"
    url, body, headers = sent[0]
    assert url.endswith(path) and ("model" in json.loads(body)) is with_model
    assert "Authorization" not in headers
    assert client.last_call["request_sha256"] == hashlib.sha256(body).hexdigest()
    assert client.last_call["request"] == json.loads(body)
    assert client.last_call["model_calls"] == 1 and process_call_count() - mark == 1
    assert client.last_call["cost_usd"] == .0001
    if kind is P.DjevClient:
        assert client.last_call["request"]["options"] == {"seed": 0, "samples": 1}


def test_hosted_jev_is_explicit_and_uses_decisions_protocol():
    sent = []
    def post(url, body, headers, timeout):
        sent.append((url, json.loads(body), headers))
        return response(url, body, headers, timeout)
    client = P.JevClient(poster=post, key="inert-test-key")
    assert client.available
    assert not client.decide("A video is playing", QUESTION).error
    assert sent[0][0] == "https://openrouter.ai/api/alpha/decisions"
    assert sent[0][1]["model"] == "typesafe/jev-1.13"
    assert client.metadata["backend"] == "jev"
    assert "inert-test-key" not in json.dumps(client.last_call)


def test_budget_refusal_and_missing_endpoint_make_no_request():
    sent = []
    def forbidden(*args):
        sent.append(args)
        raise AssertionError("no request allowed")
    client = P.DjevClient(url="http://127.0.0.1:8000", poster=forbidden, key="", budget=RunBudget(0))
    mark = process_call_count()
    assert client.decide("state", QUESTION).error
    assert client.last_call["model_calls"] == 0
    missing = P.RecordedClient(url="", poster=forbidden, key="")
    assert missing.decide("state", QUESTION).error
    assert sent == [] and process_call_count() == mark


def test_provider_timeout_remains_one_failed_attempt_without_retry():
    sent = []
    def timeout(*args):
        sent.append(args)
        raise TimeoutError("inert timeout")
    client = P.DjevClient(url="http://127.0.0.1:8000", poster=timeout, key="")
    mark = process_call_count()
    assert client.decide("state", QUESTION).error == "TimeoutError"
    assert len(sent) == 1 and process_call_count() - mark == 1
    assert client.last_call["model_calls"] == 1
    assert client.last_call["cost_basis"] == "reservation"


def test_auto_does_not_select_hosted_or_local_model_implicitly(monkeypatch):
    monkeypatch.setenv("ANYUSE_REA_PICKER", "auto")
    monkeypatch.delenv("ANYUSE_DJEV_URL", raising=False)
    monkeypatch.delenv("DJEV_URL", raising=False)
    assert type(P.configured_client()) is P.RecordedClient
    monkeypatch.setenv("ANYUSE_DJEV_URL", "http://127.0.0.1:9000")
    assert isinstance(P.configured_client(), P.DjevClient)
    assert type(P.configured_client("openjev")) is P.RecordedClient
    with pytest.raises(ValueError, match="unknown"):
        P.configured_client("unrecognized")


def test_choose_view_cli_owns_output_before_model_and_needs_no_guest(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from anyuse.execution import rea_actions as A, rea_cli as C
    view = {"format": A.CHOICE_VIEW, "catalog_sha256": "a" * 64, "task": "Pause video",
            "actions": [{"id": "pause", "summary": "Pause video", "details": "Measured fixed transition.",
                         "contract": {"effect": "fixed_state_transition", "params": []},
                         "candidate_sha256": "b" * 64, "card_sha256": "c" * 64}]}
    file = tmp_path / "view.json"
    file.write_text(json.dumps(view))
    seen = []
    def configured(backend, **kwargs):
        seen.append(backend)
        def choose(url, body, headers, timeout):
            return 200, {}, json.dumps({"answers": {"pick": {"choice": "a0", "confidence": .95}}}).encode()
        return P.RecordedClient(url="http://127.0.0.1:8000", poster=choose, key="")
    monkeypatch.setattr(P, "configured_client", configured)
    monkeypatch.setattr(A.R, "assert_guest", lambda: (_ for _ in ()).throw(AssertionError("no guest access")))
    out = tmp_path / "choice"
    run = CliRunner().invoke(C.rea_commands, ["choose-view", str(file), "--backend", "openjev", "--out", str(out)])
    assert run.exit_code == 0, run.output
    assert json.loads((out / "selection.json").read_text())["status"] == "selected"
    assert json.loads((out / "view.json").read_text()) == view
    collision = CliRunner().invoke(C.rea_commands, ["choose-view", str(file), "--out", str(out)])
    assert collision.exit_code != 0 and seen == ["openjev"]
    configured_default = CliRunner().invoke(C.rea_commands, ["choose-view", str(file), "--out", str(tmp_path / "default-choice")])
    assert configured_default.exit_code == 0, configured_default.output
    assert seen == ["openjev", None]  # An omitted flag must not override env/settings with auto.


@pytest.mark.parametrize("primary_available,cleanup_ok", [(True, True), (False, True), (True, False)])
def test_cli_explicit_fallback_shares_budget_closes_both_and_retains_correct_receipts(
        tmp_path, monkeypatch, primary_available, cleanup_ok):
    from click.testing import CliRunner
    from anyuse.execution import rea_actions as A, rea_cli as C
    view = {"format": A.CHOICE_VIEW, "catalog_sha256": "a" * 64, "task": "Pause video",
            "actions": [{"id": "pause", "summary": "Pause video", "details": "Measured fixed transition.",
                         "contract": {"effect": "fixed_state_transition", "params": []},
                         "candidate_sha256": "b" * 64, "card_sha256": "c" * 64}]}
    file = tmp_path / "view.json"
    file.write_text(json.dumps(view))
    readers, budgets = [], []
    class Reader:
        def __init__(self, backend):
            self.metadata = {"backend": backend}
            self.available = primary_available if backend == "laya" else True
            self.last_call = {"model_calls": 0}
            self.last_receipt = {"backend": backend}
            self.cleanup = {"attempted": False, "confirmed": False}
            self.closed = False
        def decide(self, state, questions):
            self.last_call["model_calls"] = 1
            return O.Answer(answers={"pick": {"choice": "a0", "confidence": .2 if self.metadata["backend"] == "laya" else .95}})
        def close(self):
            self.closed = True
            used = bool(self.last_call["model_calls"])
            self.cleanup = {"attempted": used, "confirmed": used and (cleanup_ok or self.metadata["backend"] == "reasoning")}
            self.last_receipt["cleanup"] = self.cleanup.copy()
    def configured(backend, *, budget):
        budgets.append(budget)
        reader = Reader(backend)
        readers.append(reader)
        return reader
    monkeypatch.setattr(P, "configured_client", configured)
    monkeypatch.setattr(A.R, "assert_guest", lambda: (_ for _ in ()).throw(AssertionError("no guest access")))
    out = tmp_path / "choice"
    run = CliRunner().invoke(C.rea_commands, ["choose-view", str(file), "--backend", "laya",
                                            "--fallback", "reasoning", "--out", str(out)])
    assert run.exit_code == 0, run.output
    result = json.loads((out / "selection.json").read_text())
    assert budgets[0] is budgets[1] and all(r.closed for r in readers)
    assert result["fallback_attempted"] and result["model_calls"] == (2 if primary_available else 1)
    assert result["model_receipt"]["backend"] == "reasoning"
    assert [a["selection"]["model_receipt"]["backend"] for a in result["selection_attempts"]] == ["laya", "reasoning"]
    assert result["status"] == ("selected" if cleanup_ok else "abstained")
    assert result["actuated"] is False



@pytest.mark.parametrize("destination", ["https://other.invalid/sink", "http://chooser.invalid/sink"])
def test_chooser_refuses_credential_redirect_before_following(monkeypatch, destination):
    import io
    import urllib.request as U
    import urllib.response
    from email.message import Message
    sent = []
    def response(req):
        sent.append(req)
        headers = Message(); headers["Location"] = destination
        value = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, code=302)
        value.msg = "fixture redirect"
        return value
    class HTTP(U.HTTPHandler):
        def http_open(self, req): return response(req)
    class HTTPS(U.HTTPSHandler):
        def https_open(self, req): return response(req)
    build = U.build_opener
    monkeypatch.setattr(U, "build_opener", lambda *hs: build(*hs, HTTP(), HTTPS()))
    status, _, _ = O._urllib_post("https://chooser.invalid/v1/systemone", b"{}",
                                 {"Authorization": "Bearer INERT"}, 1.)
    assert status == 302 and len(sent) == 1


@pytest.mark.parametrize("usage,headers", [([1], {}), ({}, {"server-timing": "total;dur=1.2.3"})])
def test_optional_chooser_metadata_never_escapes_decide(usage, headers):
    client = P.RecordedClient(url="https://chooser.invalid", key="", poster=lambda *a: (
        200, headers, json.dumps({"answers": {"pick": {"choice": "a", "confidence": .95}}, "usage": usage}).encode()))
    result = client.decide("fixture state", QUESTION)
    assert isinstance(result, O.Answer)
    if isinstance(usage, list):
        assert result.error
    else:
        assert result.server_ms is None and not result.error


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
