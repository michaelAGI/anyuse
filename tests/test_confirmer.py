"""Confirmer: veto-only policy, block asymmetry, settings,
the container runner's command line, and the eval metrics. Fakes only: no
container, no laya/torch import, no network, no desktop."""
import json
import os
import sys
import tempfile
import types

from anyuse.execution import confirmer as C

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def V(kind, p_bad, error=""):
    return C.Verdict(kind, p_bad, None, 1.0, error)


class FakeRunner:
    def __init__(self, noul=None, error="", boom=False):
        self.noul, self.error, self.boom, self.calls = noul, error, boom, []

    def __call__(self, requests, checkpoint):
        self.calls.append((list(requests), checkpoint))
        if self.boom:
            raise RuntimeError("container gone")
        if self.error:
            return [{"id": r["id"], "error": self.error} for r in requests]
        return [{"id": r["id"], "noul": self.noul, "confidence": 0.9, "ms": 5.0}
                for r in requests]


# -- veto-only on proved ------------------------------------------------------

def test_confident_bad_answer_downgrades_proved_to_checked():
    out = C.apply_to_proved("proved", V("result_page", 0.97), 0.9)
    assert out.startswith("checked (flagged by confirmer: "), out
    assert "not a result list" in out


def test_it_never_promotes_anything_that_is_not_proved():
    for status in ("not proved", "discovered", "refused",
                   "checked (selection not held out)", ""):
        for p in (0.0, 0.5, 1.0):
            assert C.apply_to_proved(status, V("result_page", p), 0.5) == status


def test_abstain_low_confidence_or_error_changes_nothing():
    assert C.apply_to_proved("proved", None) == "proved"
    assert C.apply_to_proved("proved", V("result_page", 0.89), 0.9) == "proved"
    assert C.apply_to_proved("proved", V("result_page", None), 0.1) == "proved"
    assert C.apply_to_proved("proved", V("result_page", 0.99, "timeout"), 0.5) == "proved"


def test_answer_polarity_follows_the_kind():
    # "is this a result list?" -> P(yes)=0.1 means P(bad)=0.9
    assert C.verdict_from_answer("result_page", {"noul": 0.1}).p_bad == 0.9
    # "is this an error page?" -> P(yes)=0.8 is P(bad)=0.8
    assert C.verdict_from_answer("error_page", {"noul": 0.8}).p_bad == 0.8
    for bad in (None, {}, {"noul": "x"}, {"noul": 1.5}):
        assert C.verdict_from_answer("result_page", bad).p_bad is None


# -- the asymmetric block policy -----------------------------------------------

def test_rules_ok_and_confident_challenge_stops():
    assert C.apply_to_block(False, "", V("block_page", 0.95), 0.9) == C.STOP
    assert C.apply_to_block(False, "", V("login_wall", 0.95), 0.9) == C.STOP
    assert C.apply_to_block(False, "", V("block_page", 0.5), 0.9) == C.CONTINUE
    assert C.apply_to_block(False, "", None) == C.CONTINUE


def test_weak_block_plus_not_a_challenge_is_only_a_human_card_never_continue():
    got = C.apply_to_block(True, "weak", V("block_page", 0.02), 0.9)
    assert got == C.HUMAN_CHECK_CARD
    for p in (0.0, 0.2, 0.5, 0.99, None):
        assert C.apply_to_block(True, "weak", V("block_page", p), 0.9) != C.CONTINUE


def test_strong_block_evidence_is_not_consulted():
    for p in (0.0, 1.0):
        assert C.apply_to_block(True, "strong", V("block_page", p), 0.5) == C.BLOCKED


def test_evidence_strength_from_the_existing_rule_markers():
    assert C.block_evidence_strength(
        final_url="https://x.test/cdn-cgi/challenge-platform/h/b") == "strong"
    assert C.block_evidence_strength(
        request_urls=["https://x.test/cdn-cgi/challenge-platform/h/b/scripts/"],
        items=[], page_text="short") == "strong"
    # a results page that loads a challenge script: weak (a second look is allowed)
    assert C.block_evidence_strength(
        request_urls=["https://x.test/cdn-cgi/challenge-platform/h/b/scripts/"],
        items=["a", "b"], page_text="results") == "weak"


def test_the_dlang_captcha_word_fixture_is_weak_evidence_and_not_flagged():
    with open(os.path.join(HERE, "testdata/compile/served_page_mentions_captcha.rec.json")) as fh:
        rec = json.load(fh)
    ui = rec["ui"]
    assert C.block_evidence_strength(rec.get("final_url", ""),
                                     [n.get("url", "") for n in rec.get("network", [])],
                                     ui.get("items") or [], ui.get("text", "")) == "weak"
    fake = FakeRunner(noul=0.03)        # "is this a challenge?" -> no
    v = C.ask("block_page", C.compact_state(url=rec.get("final_url", ""),
                                            results=ui["items"], text=ui["text"]),
              runner=fake, checkpoint="en")
    assert C.apply_to_block(False, "", v, 0.9) == C.CONTINUE
    assert C.apply_to_block(True, "weak", v, 0.9) == C.HUMAN_CHECK_CARD


# -- confirm_capability (the discover hook) ------------------------------------

def _cap(status="proved", cap="search"):
    return types.SimpleNamespace(
        cap=cap, status=status, reason="tier 1 proof passed", values=["vim", "curl"],
        proof={"first_url": "https://pk.test/search?q=vim#frag", "title": "Search: vim",
               "preview": ["vim-a", "vim-b", "vim-c"]},
        render=lambda v: f"https://pk.test/search?q={v}")


def test_off_by_default_means_no_question_and_no_change():
    os.environ.pop("ANYUSE_CONFIRMER", None)
    c = _cap()
    assert C.mode() == "off"
    assert C.confirm_capability(c) is None and c.status == "proved"


def test_with_a_runner_a_confident_no_downgrades_and_is_recorded():
    c, fake = _cap(), FakeRunner(noul=0.02)
    v = C.confirm_capability(c, runner=fake, threshold=0.9)
    assert v.p_bad == 0.98 and c.status.startswith(C.FLAGGED_PREFIX)
    (reqs, ck), = fake.calls
    assert "'vim'" in reqs[0]["question"]["instructions"]
    assert "#frag" not in reqs[0]["state"] and "https://" not in reqs[0]["state"]
    assert c.proof["confirmer"]["kind"] == "result_page"


def test_runner_failure_or_not_proved_leaves_status_alone():
    c = _cap()
    C.confirm_capability(c, runner=FakeRunner(boom=True))
    assert c.status == "proved"
    c = _cap()
    C.confirm_capability(c, runner=FakeRunner(error="container exited 1"))
    assert c.status == "proved"
    c, fake = _cap(status="discovered"), FakeRunner(noul=0.0)
    assert C.confirm_capability(c, runner=fake) is None and not fake.calls


def test_capability_to_kind():
    assert C.kind_for_capability("search") == "result_page"
    assert C.kind_for_capability("detail") == "item_page"
    assert C.kind_for_capability("next") == "list_page"
    assert C.kind_for_capability("filter") == "list_page"
    assert C.kind_for_capability("weird") == "any_page"
    assert set(C.KINDS) >= {"result_page", "item_page", "block_page", "login_wall",
                            "error_page", "wrong_page", "consent_banner"}


# -- settings -------------------------------------------------------------------

def test_settings_registered_off_by_default_and_not_security_relevant():
    from anyuse import settings as S
    for name, default in (("ANYUSE_CONFIRMER", "off"), ("ANYUSE_CONFIRMER_LAYA_DIR", ""),
                          ("ANYUSE_CONFIRMER_CHECKPOINT", "en")):
        spec = S.SETTINGS[name]
        assert spec.default == default and not spec.security_relevant
    assert S.get("ANYUSE_CONFIRMER", env={}) == "off"
    for m in ("shadow", "veto"):
        assert S.get("ANYUSE_CONFIRMER", env={"ANYUSE_CONFIRMER": m}) == m
    with open(os.path.join(HERE, "docs/SETTINGS.md")) as fh:
        doc = fh.read()
    assert "`ANYUSE_CONFIRMER`" in doc and "`ANYUSE_CONFIRMER_LAYA_DIR`" in doc


# -- the container runner (fake spawn) -----------------------------------------

def test_container_command_is_offline_and_resource_capped():
    cmd = C.container_command("/l", "/d", "n1", ["en", "typed"])
    s = " ".join(cmd)
    assert cmd[:3] == ["container", "run", "--rm"]
    for part in ("--network laya-offline", "--no-dns", "-c 8", "-m 8G", "-v /l:/work",
                 "-v /d:/data", "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
                 "python:3.12-slim"):
        assert part in s, part
    assert cmd[-2:] == ["en", "typed"]


def test_runner_round_trip_and_the_container_is_always_stopped():
    laya = tempfile.mkdtemp()
    data = tempfile.mkdtemp()
    seen = []

    def spawn(cmd, **kw):
        seen.append(cmd)
        if cmd[:2] == ["container", "run"]:
            assert "--no-index" in open(os.path.join(data, "run.sh")).read()
            assert os.path.exists(os.path.join(data, "laya_worker.py"))
            reqs = [json.loads(x) for x in open(os.path.join(data, "in.jsonl"))]
            with open(os.path.join(data, "out_en.jsonl"), "w") as fh:
                for r in reqs:
                    fh.write(json.dumps({"id": r["id"], "noul": 0.25, "ms": 3.0}) + "\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    r = C.ContainerRunner(laya_dir=laya, spawn=spawn, keep_dir=data)
    out = r([{"id": "a", "state": "s", "question": C.question("error_page")}], "en")
    assert out == [{"id": "a", "noul": 0.25, "ms": 3.0}]
    assert seen[-1][:2] == ["container", "stop"]


def test_runner_errors_are_no_opinion():
    def spawn(cmd, **kw):
        if cmd[:2] == ["container", "run"]:
            raise FileNotFoundError("container")
        return types.SimpleNamespace(returncode=0)
    r = C.ContainerRunner(laya_dir=tempfile.mkdtemp(), spawn=spawn)
    (a,) = r([{"id": "x", "state": "s", "question": C.question("error_page")}], "en")
    assert "error" in a
    v = C.verdict_from_answer("error_page", a, error=a["error"])
    assert C.apply_to_proved("proved", v, 0.0) == "proved"
    r = C.ContainerRunner(laya_dir="/nonexistent-laya-dir", spawn=spawn)
    (a,) = r([{"id": "x", "state": "s", "question": C.question("error_page")}], "en")
    assert "not a directory" in a["error"]


def test_host_never_imports_laya_or_torch():
    assert "laya" not in sys.modules and "torch" not in sys.modules
    src = open(C.__file__).read()
    assert "import laya" not in src and "import torch" not in src


# -- page text ------------------------------------------------------------------

def test_compact_state_is_short_and_secret_free():
    s = C.compact_state(url="https://user:pw@h.test/a/b?q=x#top", title="T" * 500,
                        headings=["h"] * 20, results=["r"] * 20, text="w " * 5000)
    assert "pw" not in s and "user" not in s and "#top" not in s and "https" not in s
    assert len(s) <= C.MAX_STATE_CHARS and s.startswith("URL: h.test/a/b?q=x")
    r = C.read_html("<html><head><title> Hi </title><script>x=1</script></head>"
                    "<body><h1>Head</h1><p>Body text</p></body></html>")
    assert r["title"] == "Hi" and r["headings"] == ["Head"] and "x=1" not in r["text"]


# -- eval metrics ---------------------------------------------------------------


# -- modes: shadow logs and never acts; veto acts only for passed kinds ----------

class _Mode:
    def __init__(self, mode, checkpoint="en"):
        self.env = {"ANYUSE_CONFIRMER": mode, "ANYUSE_CONFIRMER_CHECKPOINT": checkpoint,
                    "ANYUSE_DATA_DIR": tempfile.mkdtemp()}

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def log(self):
        p = C.log_path()
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


def test_shadow_asks_logs_and_never_changes_a_proved_tool():
    with _Mode("shadow") as m:
        fake = FakeRunner(noul=0.0)                     # a confident "not a result list"
        c = _cap()
        v, t = C.consult("result_page", "URL: pk.test/x", url="https://pk.test/x?q=secret",
                         deterministic="proved", where="t", runner=fake)
        assert v.p_bad == 1.0 and t is None
        c = _cap()
        orig = C.ask
        C.ask = lambda *a, **k: v
        try:
            C.confirm_capability(c)
        finally:
            C.ask = orig
        assert c.status == "proved"
        log = m.log()
        assert log and all(r["acted"] == "no" for r in log)
        blob = json.dumps(log)
        assert "secret" not in blob and "pk.test/x" not in blob and log[0]["host"] == "pk.test"


def test_veto_acts_only_for_kinds_that_passed_the_rule():
    with _Mode("veto", "en"):
        assert C.veto_threshold("error_page") == C.VETO_KINDS["en"]["error_page"]
        assert C.veto_threshold("result_page") is None
        v, t = C.consult("result_page", "s", runner=FakeRunner(noul=0.0))
        assert t is None                                # not passed: shadow only
        orig = C.ask
        C.ask = lambda *a, **k: v
        try:
            c = _cap()
            C.confirm_capability(c)
            assert c.status == "proved"
        finally:
            C.ask = orig


def test_block_observer_tightens_on_an_error_page_and_never_relaxes():
    from anyuse.execution import blocked as B
    with _Mode("veto", "en"):
        ok = B.BlockedState()
        out = C.observe_block(ok, "Page not found", "https://h.test/x", [], [],
                              runner=FakeRunner(noul=0.97))
        assert out.blocked and out.reason == B.ERROR_PAGE
        out = C.observe_block(ok, "Results: a b c", "https://h.test/x", [], ["a"],
                              runner=FakeRunner(noul=0.1))
        assert not out.blocked
        hard = B.BlockedState(blocked=True, reason=B.CAPTCHA_TURNSTILE, host="h.test")
        out = C.observe_block(hard, "vibe-captcha results", "https://h.test/s", [], ["a"],
                              runner=FakeRunner(noul=0.0))
        assert out.blocked                              # a card at most, never a continue
    with _Mode("shadow"):
        out = C.observe_block(B.BlockedState(), "Page not found", "https://h.test/x", [], [],
                              runner=FakeRunner(noul=0.99))
        assert not out.blocked                          # shadow never acts


def test_detect_blocked_ui_is_unchanged_when_off():
    from anyuse.execution import blocked as B
    os.environ.pop("ANYUSE_CONFIRMER", None)
    a = B.detect_blocked_ui("Just a moment... Performing security verification", "",
                            "https://x.test/")
    b = B._detect_blocked_ui_rules("Just a moment... Performing security verification", "",
                                   "https://x.test/")
    assert a.to_dict() == b.to_dict()


# -- the warm session (fake container process) -----------------------------------

class _FakeProc:
    def __init__(self, answers_noul=0.3, die_after=None):
        import io
        self.noul, self.die_after, self.n = answers_noul, die_after, 0
        self._out = []
        self.stdout = self
        self.stdin = self
        self._out.append("pip chatter\n")
        self._out.append(json.dumps({"ready": True, "load_s": 1.0}) + "\n")
        self.terminated = False

    # stdin side
    def write(self, line):
        r = json.loads(line)
        self.n += 1
        if self.die_after is not None and self.n > self.die_after:
            return
        self._out.append("some log line\n")
        self._out.append(json.dumps({"id": r["id"], "noul": self.noul, "ms": 200.0}) + "\n")

    def flush(self):
        pass

    def close(self):
        pass

    # stdout side
    def readline(self):
        return self._out.pop(0) if self._out else ""

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


def test_warm_session_starts_once_and_answers_over_the_pipe():
    procs, cmds, stops = [], [], []

    def popen(cmd, **kw):
        cmds.append(cmd)
        procs.append(_FakeProc())
        return procs[-1]

    def spawn(cmd, **kw):
        stops.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="")

    s = C.WarmSession(laya_dir=tempfile.mkdtemp(), popen=popen, spawn=spawn, idle_s=5)
    for i in range(3):
        (a,) = s([{"id": f"r{i}", "state": "s", "question": C.question("error_page")}])
        assert a == {"id": f"r{i}", "noul": 0.3, "ms": 200.0}
    assert len(cmds) == 1 and s.ready.get("ready")
    cmd = " ".join(cmds[0])
    assert "--network laya-offline" in cmd and "--no-dns" in cmd and " -i " in cmd
    assert "target=/work,readonly" in cmd and "-v " not in cmd and "--publish" not in cmd
    assert "-p " not in cmd
    s.close()
    assert stops and stops[-1][:2] == ["container", "stop"] and procs[0].terminated


def test_warm_session_protocol_error_is_no_opinion_and_restarts():
    procs = []

    def popen(cmd, **kw):
        procs.append(_FakeProc(die_after=0 if not procs else None))
        return procs[-1]

    s = C.WarmSession(laya_dir=tempfile.mkdtemp(), popen=popen,
                      spawn=lambda *a, **k: types.SimpleNamespace(returncode=0), idle_s=5)
    (a,) = s([{"id": "x", "state": "s", "question": C.question("error_page")}])
    assert "error" in a and not s.alive()
    (b,) = s([{"id": "y", "state": "s", "question": C.question("error_page")}])
    assert b.get("noul") == 0.3 and len(procs) == 2
    s.close()


def test_warm_session_idle_timeout_stops_the_container():
    import time as _t
    stops = []
    s = C.WarmSession(laya_dir=tempfile.mkdtemp(), popen=lambda *a, **k: _FakeProc(),
                      spawn=lambda cmd, **k: stops.append(cmd), idle_s=0.2)
    s([{"id": "x", "state": "s", "question": C.question("error_page")}])
    deadline = _t.monotonic() + 3
    while s.alive() and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert not s.alive() and stops and stops[-1][:2] == ["container", "stop"]


# -- tools/confirmer/fetch_laya.py (fake opener, no network) --------------------

def _fetch_laya():
    import importlib.util
    path = os.path.join(HERE, "tools", "confirmer", "fetch_laya.py")
    spec = importlib.util.spec_from_file_location("fetch_laya_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, data):
        import io
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_laya_pins_what_the_workers_load():
    import re
    from anyuse.execution import rea_laya_worker as W
    F = _fetch_laya()
    assert set(F.CHECKPOINTS) == set(W.CHECKPOINTS) == {"en", "typed", "multilingual"}
    for name, (repo, rev, files) in F.CHECKPOINTS.items():
        assert repo.startswith("convaiinnovations/laya") and re.fullmatch(r"[0-9a-f]{40}", rev), name
        assert tuple(files) == F.CHECKPOINT_FILES, name
        assert all(re.fullmatch(r"[0-9a-f]{64}", s) for s in files.values()), name
        assert F.checkpoint_url(repo, rev, "model.safetensors") == \
            f"https://huggingface.co/{repo}/resolve/{rev}/model.safetensors"
    # the five files are the set rea_laya.LayaClient.available checks
    with open(os.path.join(HERE, "execution", "rea_laya.py"), encoding="utf-8") as fh:
        source = fh.read()
    assert all(f'"{f}"' in source for f in F.CHECKPOINT_FILES)
    wheels = {w[0]: w for w in F.WHEELS}
    assert wheels["laya"][1] == "0.3.20" and wheels["laya"][2] == "laya-0.3.20-py3-none-any.whl"
    assert wheels["torch"][2].endswith("+cpu-cp312-cp312-manylinux_2_28_aarch64.whl")
    assert all(re.fullmatch(r"[0-9a-f]{64}", w[3]) for w in F.WHEELS)
    assert len({w[2] for w in F.WHEELS}) == len(F.WHEELS)
    assert F.default_dir().name == "laya"


def test_fetch_laya_downloads_verifies_and_resumes():
    import hashlib
    F = _fetch_laya()
    content = {f: f"bytes of {f}".encode() for f in F.CHECKPOINT_FILES}
    wheel = b"wheel bytes"
    sha = lambda b: hashlib.sha256(b).hexdigest()
    F.CHECKPOINTS = {"en": ("convaiinnovations/laya", "a" * 40, {f: sha(b) for f, b in content.items()})}
    F.WHEELS = (("laya", "0.3.20", "laya-0.3.20-py3-none-any.whl", sha(wheel)),)
    served, opened = {}, []
    served["https://pypi.org/pypi/laya/0.3.20/json"] = json.dumps({"urls": [
        {"filename": "laya-0.3.20-py3-none-any.whl", "url": "https://files.example/laya.whl",
         "digests": {"sha256": sha(wheel)}}]}).encode()
    served["https://files.example/laya.whl"] = wheel
    for f, b in content.items():
        served[F.checkpoint_url("convaiinnovations/laya", "a" * 40, f)] = b

    def opener(url):
        opened.append(url)
        return _Resp(served[url])

    target = __import__("pathlib").Path(tempfile.mkdtemp()) / "laya"
    counts = F.fetch(target, ("en",), opener=opener, log=lambda *_: None)
    assert counts == {"downloaded": 6, "kept": 0}, counts
    assert (target / "wheels" / "laya-0.3.20-py3-none-any.whl").read_bytes() == wheel
    for f, b in content.items():
        assert (target / "models" / "en" / f).read_bytes() == b
    # a second run downloads nothing
    opened.clear()
    assert F.fetch(target, ("en",), opener=opener, log=lambda *_: None) == {"downloaded": 0, "kept": 6}
    assert opened == []
    # a corrupted file is fetched again
    (target / "models" / "en" / "rl_agent_config.json").write_bytes(b"tampered")
    assert F.fetch(target, ("en",), opener=opener, log=lambda *_: None)["downloaded"] == 1
    assert (target / "models" / "en" / "rl_agent_config.json").read_bytes() == content["rl_agent_config.json"]
    # a server that sends other bytes leaves no file and no partial file behind
    served[F.checkpoint_url("convaiinnovations/laya", "a" * 40, "model.safetensors")] = b"evil"
    (target / "models" / "en" / "model.safetensors").unlink()
    try:
        F.fetch(target, ("en",), opener=opener, log=lambda *_: None)
        raise AssertionError("a wrong checksum must stop the fetch")
    except F.ChecksumError:
        pass
    leftovers = [p.name for p in (target / "models" / "en").iterdir() if p.name.startswith("model.")]
    assert leftovers == [], leftovers
    # PyPI stating another digest stops before any wheel download
    served["https://pypi.org/pypi/laya/0.3.20/json"] = json.dumps({"urls": [
        {"filename": "laya-0.3.20-py3-none-any.whl", "url": "https://files.example/laya.whl",
         "digests": {"sha256": "0" * 64}}]}).encode()
    (target / "wheels" / "laya-0.3.20-py3-none-any.whl").unlink()
    opened.clear()
    try:
        F.fetch(target, (), opener=opener, log=lambda *_: None)
        raise AssertionError("a PyPI digest mismatch must stop the fetch")
    except F.ChecksumError:
        pass
    assert "https://files.example/laya.whl" not in opened
    # torch comes from PyTorch's CPU index, by its exact file name
    assert F.wheel_url("torch", "2.14.0+cpu", "torch-2.14.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl",
                       "0" * 64, opener=opener) == \
        "https://download.pytorch.org/whl/cpu/torch-2.14.0%2Bcpu-cp312-cp312-manylinux_2_28_aarch64.whl"


def test_fetch_laya_refuses_an_unknown_checkpoint_and_main_reports_errors():
    F = _fetch_laya()
    try:
        F.fetch(__import__("pathlib").Path(tempfile.mkdtemp()), ("huge",), wheels=False,
                opener=lambda url: (_ for _ in ()).throw(AssertionError(url)))
        raise AssertionError("an unknown checkpoint must be refused")
    except ValueError as exc:
        assert "huge" in str(exc)

    def offline(url):
        raise OSError("network is off in tests")

    assert F.main([tempfile.mkdtemp(), "--checkpoint", "en", "--no-wheels"], opener=offline) == 1


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
