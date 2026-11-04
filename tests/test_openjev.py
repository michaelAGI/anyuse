"""OpenJev backend: fail-closed client, challenge guard,
confirmer wiring (shadow unless a kind passed on held-out sites) and the
routine shadow ranker. Fakes only: injected in-memory request transports. No model, no container, no desktop.
"""
import json
import os
import sys
import tempfile

from anyuse.execution import confirmer as C
from anyuse.execution import openjev as O

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Poster:
    """Records every request; answers with a scripted reply."""

    def __init__(self, reply=None, status=200, boom=None, timing="total;dur=41.5"):
        self.reply = reply if reply is not None else {"answers": {"q": {"noul": 0.9}},
                                                      "usage": {"input_tokens": 120}}
        self.status, self.boom, self.timing, self.calls = status, boom, timing, []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": json.loads(body), "headers": dict(headers),
                           "timeout": timeout})
        if self.boom:
            raise self.boom
        raw = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return self.status, {"server-timing": self.timing}, raw


class Env:
    def __init__(self, **env):
        self.env = dict(env)
        self.env.setdefault("ANYUSE_DATA_DIR", tempfile.mkdtemp())

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.env}
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        C._SESSIONS.clear()
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        C._SESSIONS.clear()


def client(poster=None, url="https://oj.test", **kw):
    return O.Client(url=url, poster=poster or Poster(), key="", **kw)


# -- the client: fail-closed ----------------------------------------------------

def test_no_url_means_unavailable_and_no_socket():
    p = Poster()
    with Env(ANYUSE_OPENJEV_URL=None):
        c = O.Client(poster=p, key="")
        assert not c.available
        v, a = c.noul("URL: h.test/", "Is this an error page?")
    assert v is None and a.error == O.UNAVAILABLE and not p.calls


def test_only_https_or_loopback_http_without_credentials():
    for bad in ("http://oj.example.com", "ftp://x", "https://u:p@oj.test", "oj.test",
                "https://oj.test/?k=1", "http://10.0.0.2:8080"):
        assert O.valid_base_url(bad) == "", bad
    assert O.valid_base_url("https://abc-8080.proxy.runpod.net/") == \
        "https://abc-8080.proxy.runpod.net"
    assert O.valid_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert O.endpoint("http://127.0.0.1:8080") == "http://127.0.0.1:8080/v1/systemone"
    assert O.endpoint("https://x.test/v1/systemone") == "https://x.test/v1/systemone"


def test_noul_request_shape_and_answer():
    p = Poster()
    v, a = client(p, model="openjev-latest").noul("URL: h.test/a\nTitle: A",
                                                  "Is this an error page?", steps=3)
    assert v == 0.9 and a.server_ms == 41.5 and a.input_tokens == 120 and not a.error
    body = p.calls[0]["body"]
    assert p.calls[0]["url"] == "https://oj.test/v1/systemone"
    assert body["model"] == "openjev-latest" and body["steps"] == 3
    assert body["questions"] == {"q": {"type": "noul", "instructions": "Is this an error page?"}}
    assert "images" not in body and "Authorization" not in p.calls[0]["headers"]


def test_every_failure_is_no_opinion():
    cases = [Poster(status=500), Poster(status=429), Poster(reply=b"<html>"),
             Poster(reply={"answers": {"q": {"noul": 1.7}}}),
             Poster(reply={"answers": {"q": {}}}), Poster(reply={"answers": []}),
             Poster(boom=TimeoutError("slow")), Poster(boom=ConnectionRefusedError())]
    for p in cases:
        v, a = client(p).noul("s", "q?")
        assert v is None and a.error, (p.status, p.reply, a)


def test_images_are_data_urls_and_capped():
    p = Poster()
    client(p).noul("s", "q?", images=[PNG, "data:image/png;base64,AAAA", b"not an image",
                                      "https://evil.test/x.png"])
    imgs = p.calls[0]["body"]["images"]
    assert len(imgs) == 2 and all(i.startswith("data:image/png;base64,") for i in imgs)
    assert O.image_url(b"\x89PNG\r\n\x1a\n" + b"0" * (O.MAX_IMAGE_BYTES + 1)) == ""


def test_choice_maps_back_to_the_callers_options_and_rejects_out_of_range():
    p = Poster(reply={"answers": {"q": {"choice": "c1", "confidence": 0.8}}})
    idx, conf, a = client(p).choose("s", "Which?", ["first", "second", "third"])
    assert idx == 1 and conf == 0.8
    crit = p.calls[0]["body"]["questions"]["q"]["criteria"]
    assert crit == {"c0": "first", "c1": "second", "c2": "third"}
    for bad in ({"choice": "c9", "confidence": 0.9}, {"choice": "second", "confidence": 0.9},
                {"choice": "c0", "confidence": "x"}):
        idx, _, a = client(Poster(reply={"answers": {"q": bad}})).choose("s", "W?", ["a", "b"])
        assert idx is None and a.error


# -- the challenge guard ----------------------------------------------------------

CHALLENGE_OBS = {"url": "https://h.test/cdn-cgi/challenge-platform/h/b/orchestrate",
                 "text": "Just a moment... Verify you are human"}


def test_a_challenge_page_is_never_sent():
    p = Poster()
    v, a = client(p).noul("URL: h.test", "Is this a CAPTCHA?", guard_obs=CHALLENGE_OBS)
    assert v is None and a.error.startswith(O.GUARDED) and not p.calls
    short = {"url": "https://h.test/", "text": "Please complete the security check to access"}
    v, a = client(p).noul("s", "q?", guard_obs=short)
    assert v is None and not p.calls, a.error
    # the runner guards the state text itself when the caller has no observation
    r = O.ConfirmerRunner(client(p))
    out = r([{"id": "1", "state": "Just a moment... checking your browser. Verify you are human",
              "question": C.question("block_page"), "kind": "block_page"}])
    assert "error" in out[0] and not p.calls
    v, a = client(p).noul("s", "q?", guard_obs={"url": "https://h.test/list",
                                                "text": "Results for rust: a b c " * 20,
                                                "items": ["a", "b"]})
    assert v == 0.9 and len(p.calls) == 1


def test_the_guard_itself_never_asks_a_model():
    from anyuse.execution import live_solve
    with Env(ANYUSE_CONFIRMER="shadow", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        orig = C.observe_block
        C.observe_block = lambda *a, **k: (_ for _ in ()).throw(AssertionError("model asked"))
        try:
            assert live_solve.challenge_evidence(CHALLENGE_OBS)
            assert live_solve.challenge_evidence({"url": "https://h.test/a", "text": "x" * 500,
                                                  "items": ["a"]}) == ""
        finally:
            C.observe_block = orig


# -- confirmer wiring ---------------------------------------------------------------

def test_backend_setting_defaults_to_laya_and_selects_openjev():
    with Env(ANYUSE_CONFIRMER_BACKEND=None, ANYUSE_CONFIRMER="shadow"):
        assert C.backend() == "laya" and C.active_checkpoint() == "en"
    with Env(ANYUSE_CONFIRMER_BACKEND="openjev", ANYUSE_CONFIRMER="shadow",
             ANYUSE_OPENJEV_URL=None):
        assert C.backend() == "openjev" and C.active_checkpoint() == "openjev"
        assert isinstance(C.session(), O.ConfirmerRunner)
        v, t = C.consult("error_page", "URL: h.test/x\nText: Page not found",
                         url="https://h.test/x", deterministic="ok", where="t")
        assert v is not None and v.p_bad is None and O.UNAVAILABLE in v.error and t is None
    with Env(ANYUSE_CONFIRMER_BACKEND="bogus"):
        assert C.backend() == "laya"


def test_steps_per_kind_and_kind_travel_with_the_request():
    p = Poster()
    saved = dict(O.STEPS)
    O.STEPS.clear()
    O.STEPS["wrong_page"] = 4
    try:
        r = O.ConfirmerRunner(client(p), send_images=True)
        r([{"id": "a", "state": "URL: h.test/x", "question": C.question("wrong_page", "x"),
            "kind": "wrong_page", "images": [PNG]},
           {"id": "b", "state": "URL: h.test/y", "question": C.question("error_page"),
            "kind": "error_page"}])
    finally:
        O.STEPS.clear()
        O.STEPS.update(saved)
    assert p.calls[0]["body"]["steps"] == 4 and "images" in p.calls[0]["body"]
    assert "steps" not in p.calls[1]["body"]
    p2 = Poster()
    O.ConfirmerRunner(client(p2), send_images=False)(
        [{"id": "a", "state": "s", "question": C.question("error_page"), "kind": "error_page",
          "images": [PNG]}])
    assert "images" not in p2.calls[0]["body"]          # images are opt-in


def test_openjev_is_shadow_only_until_a_kind_passes_on_held_out_sites():
    assert set(C.VETO_KINDS.get("openjev", {})) <= set(C.KINDS)
    with Env(ANYUSE_CONFIRMER="veto", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        for kind in C.KINDS:
            assert C.veto_threshold(kind) == C.VETO_KINDS["openjev"].get(kind)
        p = Poster(reply={"answers": {"q": {"noul": 0.0}}})     # confident "not results"
        C._SESSIONS["openjev"] = O.ConfirmerRunner(client(p))
        v, t = C.consult("result_page", "URL: h.test/s?q=x", "x", url="https://h.test/s?q=x")
        assert v.p_bad == 1.0 and t == C.VETO_KINDS["openjev"].get("result_page")


def test_block_observer_asks_the_tightening_kinds_and_never_relaxes():
    from anyuse.execution import blocked as B
    p = Poster(reply={"answers": {"q": {"noul": 0.99}}})
    with Env(ANYUSE_CONFIRMER="shadow", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        C._SESSIONS["openjev"] = O.ConfirmerRunner(client(p))
        out = C.observe_block(B.BlockedState(), "Sorry, page not found. " * 5,
                              "https://h.test/x", [], [])
        assert not out.blocked                                 # shadow never acts
        asked = [c["body"]["questions"]["q"]["instructions"] for c in p.calls]
        assert asked == [C.question(k)["instructions"] for k in
                         ("error_page", "login_wall", "consent_banner", "block_page")]
        n = len(p.calls)
        hard = B.BlockedState(blocked=True, reason=B.CAPTCHA_TURNSTILE, host="h.test")
        out = C.observe_block(hard, "Just a moment... Verify you are human",
                              "https://h.test/cdn-cgi/challenge-platform/x", [], [])
        assert out.blocked and len(p.calls) == n               # strong: not asked at all
    with Env(ANYUSE_CONFIRMER="veto", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        C._SESSIONS["openjev"] = O.ConfirmerRunner(client(Poster(
            reply={"answers": {"q": {"noul": 0.99}}})))
        saved = dict(C.VETO_KINDS["openjev"])
        C.VETO_KINDS["openjev"].clear()
        C.VETO_KINDS["openjev"]["login_wall"] = 0.8
        try:
            out = C.observe_block(B.BlockedState(), "Welcome back. " * 30,
                                  "https://h.test/x", [], [])
        finally:
            C.VETO_KINDS["openjev"].clear()
            C.VETO_KINDS["openjev"].update(saved)
        assert out.blocked and out.reason == B.LOGIN_WALL


def test_a_weak_block_that_is_a_challenge_is_not_sent_to_openjev():
    from anyuse.execution import blocked as B
    p = Poster(reply={"answers": {"q": {"noul": 0.01}}})
    with Env(ANYUSE_CONFIRMER="shadow", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        C._SESSIONS["openjev"] = O.ConfirmerRunner(client(p))
        weak = B.BlockedState(blocked=True, reason=B.CAPTCHA_GENERIC, host="h.test")
        out = C.observe_block(weak, "Please verify you are a human to continue",
                              "https://h.test/s", [], [])
        assert out.blocked and not p.calls


def test_shadow_log_keeps_no_page_text():
    p = Poster(reply={"answers": {"q": {"noul": 0.2}}})
    with Env(ANYUSE_CONFIRMER="shadow", ANYUSE_CONFIRMER_BACKEND="openjev",
             ANYUSE_OPENJEV_URL="https://oj.test"):
        C._SESSIONS["openjev"] = O.ConfirmerRunner(client(p))
        C.consult("wrong_page", "URL: h.test/s?q=private-query\nTitle: secret title", "x",
                  url="https://h.test/s?q=private-query", deterministic="landing:pass", where="t")
        log = [json.loads(x) for x in open(C.log_path())]
    blob = json.dumps(log)
    assert log[-1]["checkpoint"] == "openjev" and log[-1]["acted"] == "no"
    assert "private-query" not in blob and "secret title" not in blob


# -- routine shadow ranker ---------------------------------------------------------------

class FakeResume:
    def __init__(self, boom=False):
        self.calls, self.boom = [], boom

    def rank(self, step_id, candidates, context):
        self.calls.append((step_id, list(candidates), dict(context)))
        if self.boom:
            raise RuntimeError("down")
        return {"pick": 0}


def _routine(tmp, shadow):
    import test_routine_runtime as R
    from anyuse.execution.routine import Engine, ReceiptStore
    return Engine(R.base(), {"compiled_tool": R.FakeRunner()}, ReceiptStore(tmp),
                  shadow_ranker=shadow)


def _strip(res):
    res = json.loads(json.dumps(res, default=str))
    res.pop("run_id", None)
    return res


def test_routine_resume_ranking_is_shadow_only():
    import test_routine_runtime as R
    outs = []
    for shadow in (None, FakeResume(), FakeResume(boom=True)):
        with R.Tmp() as tmp, Env(ANYUSE_ROUTINE_SHADOW_RANKER=None):
            eng = _routine(tmp, shadow)
            eng.id_factory = lambda: "0" * 32
            eng.clock = lambda: 1.0
            res = eng.run({"q": "x"})
            outs.append((_strip(res), shadow))
    assert outs[0][0] == outs[1][0] == outs[2][0]              # receipts/hand-off unchanged
    fr = outs[1][1]
    if outs[0][0].get("handoff", {}).get("candidates"):
        assert fr.calls and fr.calls[0][1] == outs[0][0]["handoff"]["candidates"]


def test_routine_setting_builds_the_openjev_ranker_lazily():
    import test_routine_runtime as R
    with R.Tmp() as tmp, Env(ANYUSE_ROUTINE_SHADOW_RANKER="openjev", ANYUSE_OPENJEV_URL=None):
        eng = _routine(tmp, None)
        eng._shadow_rank({"id": "pick", "action": {"into": "x"}}, ["a", "b"])
        assert isinstance(eng.shadow_ranker, O.ResumeRanker)
        log = [json.loads(x) for x in open(O.shadow_log_path())]
        assert log[-1]["where"] == "routine.resume" and log[-1]["pick"] is None


# -- in-memory typed request round trip and timeout -------------------------

def test_fake_server_round_trip_and_timeout():
    seen = []
    def post(url, body, headers, timeout):
        seen.append(json.loads(body))
        return 200, {"server-timing": "model;dur=20.0, server;dur=1.0, total;dur=21.0"}, json.dumps({
            "answers": {"q": {"noul": .25}}, "usage": {"input_tokens": 77}}).encode()
    client = O.Client(url="http://127.0.0.1:19003", key="", timeout_s=2., poster=post)
    value, answer = client.noul("URL: h.test/x", "Is this an error page?", images=[PNG])
    assert value == .25 and answer.server_ms == 21. and answer.input_tokens == 77
    assert seen[-1]["images"][0].startswith("data:image/png;base64,")
    def timeout(*args): raise TimeoutError("inert provider timeout")
    value, answer = O.Client(url="http://127.0.0.1:19003", key="", timeout_s=.2, poster=timeout).noul("s", "q?")
    assert value is None and answer.error


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
            import traceback
            f += 1; print("FAIL", n, "::", repr(e)[:300]); traceback.print_exc()
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
