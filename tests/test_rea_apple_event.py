"""Offline fakes for recovered-event proof rules. No native frameworks or apps."""
import copy
from pathlib import Path
from html import escape
import plistlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-rea-event-pkg-")
(Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
from anyuse.execution import rea_mapping as R
from anyuse.execution import rea_apple_event as AE
from anyuse.execution import surface_discovery as S

TEXT = {"one": "one\nFirst definition.", "two": "two\nSecond definition.",
        "three": "three\nThird definition.", "four": "four\nFourth definition."}


def rows(query, definition, dictionary="Apple"):
    return [{"parent": None, "role": "AXWindow"},
            {"parent": 0, "role": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": query},
            {"parent": 0, "role": "AXRadioButton", "AXTitle": dictionary, "AXValue": "1"},
            {"parent": 0, "role": "AXWebArea"},
            {"parent": 3, "role": "AXStaticText", "AXValue": definition}]


@pytest.fixture
def fixture(tmp_path):
    app = tmp_path / "Dictionary.app"
    (app / "Contents/Resources").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.apple.Dictionary"}))
    resource = app / "Contents/Resources/data"
    resource.write_text("original app resources")
    dictionary = tmp_path / "Apple.dictionary"
    (dictionary / "Contents/Resources").mkdir(parents=True)
    (dictionary / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.apple.dictionary.AppleDictionary", "CFBundleName": "Apple"}))
    (dictionary / "Contents/Resources/body").write_text("original dictionary resources")
    (dictionary / "Contents/Resources/Entries.xml").write_text(
        '<d:dictionary xmlns="http://www.w3.org/1999/xhtml" xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng">' +
        "".join(f'<d:entry><d:index><d:index_value>{escape(q)}</d:index_value></d:index>'
                f'<h1>{escape(q)}</h1><p>{escape(text.split(chr(10),1)[1])}</p></d:entry>' for q, text in TEXT.items()) +
        '</d:dictionary>')
    candidate = {"format": R.CANDIDATE, "backend": AE.BACKEND, "id": "dictionary_search", "label": "Search dictionary",
                 "target": R.fingerprint(app), "effect": "scoped_find", "item_kind": "",
                 "params": [{"name": "query", "type": "string", "source": "lookup"}],
                 "apple_event": AE.EVENT, "observer": {"kind": "dictionary_ax", "dictionary_path": str(dictionary),
                    "dictionary_label": "Apple", "dictionary_sha256": AE.dictionary_identity(dictionary)["sha256"],
                    "reference_xml": "Contents/Resources/Entries.xml"},
                 "evidence": [{"file": "handler.json", "pointer": "/raw_result", "sha256": "a" * 64}]}
    return app, dictionary, candidate


@pytest.fixture
def guest(monkeypatch):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    monkeypatch.setattr(AE, "reference", lambda c, query: TEXT[query])
    monkeypatch.setattr(AE.time, "sleep", lambda delay: None)
    class FakeRuntime:
        sent = []
        initial = ("seed", "Seed definition.")
        wrong = False
        def __init__(self, bundle):
            self.state = self.initial
        def read(self):
            return rows(*self.state)
        def send(self, c, inputs, timeout):
            self.sent.append(inputs["query"])
            self.state = (inputs["query"], "Unrelated definition." if self.wrong else TEXT[inputs["query"]])
            return {"returncode": 0, "timed_out": False, "permission": 0}
    monkeypatch.setattr(AE, "Runtime", FakeRuntime)
    return FakeRuntime


def cases():
    return [{"inputs": {"query": query}} for query in ("one", "two", "three")]


def prove(fixture, tmp_path):
    app, _, candidate = fixture
    return R.verify(candidate, cases(), str(app), str(tmp_path / "proof"))


def test_native_execution_refuses_before_frameworks_or_files(monkeypatch, fixture, tmp_path):
    def refuse(): raise PermissionError("VM-only")
    monkeypatch.setattr(R, "assert_guest", refuse)
    app, _, c = fixture
    for call in (lambda: R.verify(c, cases(), str(app), str(tmp_path / "proof")),
                 lambda: R.replay(c, {}, {"query": "one"}, str(app), str(tmp_path / "replay")),
                 lambda: AE.reference(c, "one"), lambda: AE.Runtime(app)):
        with pytest.raises(PermissionError, match="VM-only"):
            call()
    assert not (tmp_path / "proof").exists()


def test_reference_comes_from_requested_asset_and_refuses_ambiguity(monkeypatch, fixture):
    monkeypatch.setattr(R, "assert_guest", lambda: None)
    _, dictionary, c = fixture
    assert AE.normalize(AE.reference(c, "ONE")) == AE.normalize(TEXT["one"])
    with pytest.raises(ValueError, match="no unique"):
        AE.reference(c, "absent")
    xml = dictionary / c["observer"]["reference_xml"]
    xml.write_text(xml.read_text().replace("</d:dictionary>",
        '<d:entry><d:index d:value="one" d:title="alternate one"/><h1>alternate one</h1>'
        '<p>an alias definition</p></d:entry></d:dictionary>'))
    assert AE.normalize(AE.reference(c, "one")) == AE.normalize(TEXT["one"])
    xml.write_text(xml.read_text().replace("</d:dictionary>",
        '<d:entry><d:index d:value="one"/><h1>one</h1><p>another definition</p></d:entry></d:dictionary>'))
    with pytest.raises(ValueError, match="no unique"):
        AE.reference(c, "one")
    xml.write_text('<!DOCTYPE data [<!ENTITY expanded "untrusted">]><data/>')
    with pytest.raises(ValueError, match="entities or a DTD"):
        AE.reference(c, "one")


def test_raw_gui_dom_order_keeps_nested_link_before_punctuation():
    raw = rows("Finder", "Finder A definition. See ")
    raw += [{"parent": 3, "role": "AXLink"}, {"parent": 3, "role": "AXStaticText", "AXValue": "."},
            {"parent": 5, "role": "AXStaticText", "AXValue": "Dock"}]
    assert AE.observation(raw)["definition"] == AE.normalize("Finder A definition. See Dock.")
    echo = rows("Finder", "Finder")
    assert AE.observation(echo)["definition"] != AE.normalize("Finder A real definition.")
    echo.append({"parent": 0, "role": "AXWebArea"})
    assert AE.observation(echo)["definition"] is None


def test_proof_replay_and_explicit_map_scope(guest, fixture, tmp_path):
    app, dictionary, c = fixture
    proof = prove(fixture, tmp_path)
    assert R.proof_status(c, proof, R.fingerprint(app))[0]
    assert guest.sent == ["one", "two", "three"]
    result = R.replay(c, proof, {"query": "four"}, str(app), str(tmp_path / "replay"))
    assert result["verdict"] == "verified" and not result["retry"]
    surface = R.candidate_surface(c, proof, R.fingerprint(app), app="Dictionary")
    need = S.Need(app="Dictionary", effect="scoped_find", values={"lookup": "four"})
    assert R.map_bindings(surface, need)[0] == "uncertain"
    need.values["path"] = str(dictionary)
    assert R.map_bindings(surface, need)[0] == "served"
    assert S.readback_for(surface, need)["readback"]["via"] == "ax"
    assert S.readback_for(surface, need, ["file"])["readback"]["via"] == "none"
    spec = S.readback_for(surface, need)
    readback = {"source": "ax", "values": need.values}
    assert S.judge_readback(spec, "rea_adapter", readback)["verdict"] == "uncertain"
    readback["rea_result"] = result
    assert S.judge_readback(spec, "rea_adapter", readback)["verdict"] == "verified"
    wrong_scope = S.readback_for(surface, S.Need(app="Dictionary", effect="scoped_find",
        values={"lookup": "four", "path": str(dictionary) + "-other"}))
    assert S.judge_readback(wrong_scope, "rea_adapter", readback)["verdict"] == "refuted"
    readback["rea_result"] = {**result, "verdict": "refuted"}
    assert S.judge_readback(spec, "rea_adapter", readback)["verdict"] == "refuted"
    invocation = S.invocation(surface, {"query": "four"})
    assert invocation["via"] == "rea_mapping.replay" and "argv" not in invocation
    assert invocation["invocation_preview"]["parameters"]["stxt"]["value"] == "four"


def test_wrong_definition_stops_after_one_event(guest, fixture, tmp_path, monkeypatch):
    guest.wrong = True
    clock = iter(range(100))
    monkeypatch.setattr(AE.time, "monotonic", lambda: next(clock))
    app, _, c = fixture
    with pytest.raises(ValueError, match="definition verification failed"):
        R.verify(c, cases(), str(app), str(tmp_path / "proof"), timeout=1)
    proof = R.load_json(tmp_path / "proof/proof.json")
    assert proof["status"] == "incomplete" and proof["cases"][0]["verdict"] == "refuted"
    assert guest.sent == ["one"]


def test_noop_cannot_be_verification(guest, fixture, tmp_path):
    guest.initial = ("one", TEXT["one"])
    with pytest.raises(ValueError, match="no-op"):
        prove(fixture, tmp_path)
    assert guest.sent == []


def test_refs_and_all_inputs_checked_before_first_event(guest, fixture, tmp_path):
    app, _, c = fixture
    unknown = cases() + [{"inputs": {"query": "no_reference"}}]
    with pytest.raises(KeyError):
        R.verify(c, unknown, str(app), str(tmp_path / "proof"))
    invalid = cases() + [{"inputs": {"query": "unsafe\0text"}}]
    with pytest.raises(ValueError, match="without NUL"):
        R.verify(c, invalid, str(app), str(tmp_path / "proof"))
    assert guest.sent == []


@pytest.mark.parametrize("change", ["app", "dictionary", "candidate"])
def test_changed_target_resource_or_candidate_refuses_before_event(guest, fixture, tmp_path, change):
    app, dictionary, c = fixture
    proof = prove(fixture, tmp_path)
    if change == "app": (app / "Contents/Resources/data").write_text("changed without version bump")
    elif change == "dictionary": (dictionary / "Contents/Resources/body").write_text("changed dictionary")
    else: c = {**c, "label": "changed candidate"}
    with pytest.raises(ValueError):
        R.replay(c, proof, {"query": "four"}, str(app), str(tmp_path / "replay"))
    assert guest.sent == ["one", "two", "three"]


def test_incomplete_observations_and_imported_flag_do_not_authorize(guest, fixture, tmp_path):
    app, _, c = fixture
    assert "verified" not in R.validate_candidate({**c, "verified": True})
    proof = prove(fixture, tmp_path)
    bad = copy.deepcopy(proof)
    bad["cases"][0]["expected"]["definition"] = ""
    bad["cases"][0]["seen"]["definition"] = ""
    assert not R.proof_status(c, bad, R.fingerprint(app))[0]
    bad = copy.deepcopy(proof)
    bad["cases"][0]["before"] = bad["cases"][0]["expected"]
    assert not R.proof_status(c, bad, R.fingerprint(app))[0]
    bad = copy.deepcopy(proof)
    bad["cases"][1] = bad["cases"][0]
    assert not R.proof_status(c, bad, R.fingerprint(app))[0]


def test_denied_permission_never_sends_event(fixture):
    _, _, c = fixture
    runtime = AE.Runtime.__new__(AE.Runtime)
    runtime._ready = lambda: None
    runtime.app = SimpleNamespace(isTerminated=lambda: False)
    runtime.pid = 123
    disposed, sent = [], []
    runtime.lib = SimpleNamespace(AECreateDesc=lambda *args: 0,
        AEDeterminePermissionToAutomateTarget=lambda *args: -1744,
        AEDisposeDesc=lambda *args: disposed.append(True),
        AESendMessage=lambda *args: sent.append(True))
    with pytest.raises(PermissionError, match="no event sent"):
        runtime.send(R.validate_candidate(c), {"query": "one"}, 10)
    assert not sent and len(disposed) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
