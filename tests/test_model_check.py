"""Model checks (execution/model_check.py): every backend, the auto order, the
scope gate, verification, the deadline, the challenge guard and the log line.
Fakes only: a fake OpenJev poster, a fake warm session, a fake Jev post, a fake
ModelAdapter and an httpx MockTransport. No model, no container, no socket.
"""
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager

from anyuse.execution import confirmer as C
from anyuse.execution import judge as J
from anyuse.execution import model_adapter as MA
from anyuse.execution import model_check as MC
from anyuse.execution import openjev as O

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EMPTY = MC.Question("test_empty_statement",
                    "The page answers a search for '{target}'. Does the line marked >> "
                    "say that this search found nothing?")
PAGE = MC.Question("test_page", "Is this page about '{target}'?")
CHALLENGE_TEXT = "Just a moment... checking your browser. Verify you are human"
CHALLENGE_OBS = {"url": "https://h.test/cdn-cgi/challenge-platform/h/b/orchestrate",
                 "text": "Just a moment... Verify you are human"}

#: every variable that could pick or reach a model; Env unsets all of them
MODEL_ENV = ("ANYUSE_MODEL_CHECKS", "ANYUSE_MODEL_CHECK_TIMEOUT_S", "ANYUSE_OPENJEV_URL",
             "ANYUSE_OPENJEV_TIMEOUT_S", "ANYUSE_JUDGE", "ANYUSE_CONFIRMER",
             "ANYUSE_CONFIRMER_LAYA_DIR", "ANYUSE_CONFIRMER_CHECKPOINT",
             "ANYUSE_JUDGE_MODEL_PROVIDER", "ANYUSE_MODEL_PROVIDER", "OPENROUTER_API_KEY")


class Env:
    """The given variables over an environment with no model configured."""

    def __init__(self, **env):
        self.env = {k: None for k in MODEL_ENV}
        self.env["ANYUSE_DATA_DIR"] = tempfile.mkdtemp()
        self.env.update(env)

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.env}
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        C._SESSIONS.clear()
        MC._WARM.clear()
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        C._SESSIONS.clear()
        MC._WARM.clear()


@contextmanager
def patched(obj, name, value):
    saved = vars(obj)[name]
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


class Poster:
    """A fake OpenJev transport: ``score(body) -> P(yes)`` answers each request."""

    def __init__(self, score):
        self.score, self.calls = score, []

    def __call__(self, url, body, headers, timeout):
        data = json.loads(body)
        self.calls.append({"url": url, "body": data, "timeout": timeout})
        reply = {"answers": {"q": {"noul": self.score(data)}}, "usage": {"input_tokens": 50}}
        return 200, {}, json.dumps(reply).encode()


def last_line(state):
    return state.rsplit(">> ", 1)[-1]


# -- where a model may be asked -------------------------------------------------

def test_outside_any_scope_nothing_is_asked():
    asked = []
    with Env(ANYUSE_OPENJEV_URL="https://oj.test"), \
            MC.using(lambda i, s, o: asked.append(s) or 0.99):
        assert not MC.available()
        v = MC.check(EMPTY, "rust", ["No results for rust"], state="page")
        assert v.none and v.calls == 0 and not asked and "scope" in v.why
        assert not os.path.exists(MC.log_path())
        with MC.scope("product"):
            assert MC.available() and MC.current_scope() == "product"
            with MC.scope("heal"):
                assert MC.current_scope() == "heal"
            assert MC.current_scope() == "product"
            assert MC.check(EMPTY, "rust", ["No results for rust"], state="page").yes
        assert MC.current_scope() == "" and len(asked) == 1
    try:
        with MC.scope("watch"):
            pass
        raise AssertionError("an unknown scope was accepted")
    except ValueError:
        pass


def test_the_scope_reaches_asyncio_tasks_and_to_thread():
    import asyncio
    with MC.scope("agent"):
        async def inner():
            return MC.current_scope(), await asyncio.to_thread(MC.current_scope)
        assert asyncio.run(inner()) == ("agent", "agent")


def test_inside_a_closed_block_nothing_is_asked_whatever_scope_is_open():
    import asyncio
    asked = []
    with Env(), MC.using(lambda i, s, o: asked.append(s) or 0.99), MC.scope("product"):
        with MC.closed("a proof replay asks no model"):
            assert not MC.available() and MC.current_scope() == ""
            v = MC.check(EMPTY, "rust", ["No results for rust"], state="page")
            assert v.none and v.calls == 0 and v.why.startswith("a proof replay asks no model"), v.why
            with MC.scope("agent"):                        # a scope opened inside stays closed
                assert not MC.available() and not MC.check(PAGE, "rust", state="page").yes

            async def inner():
                return await asyncio.to_thread(lambda: MC.check(PAGE, "rust", state="page").calls)
            assert asyncio.run(inner()) == 0
        assert asked == [] and MC.current_scope() == "product"
        assert MC.check(PAGE, "rust", state="page").yes and len(asked) == 1


def test_a_tally_counts_the_requests_and_cost_of_every_check_inside_it():
    import asyncio
    with Env(), MC.using(lambda i, s, o: (0.99, 1, 0.002, "")), MC.scope("product"):
        with MC.tally() as outer:
            MC.check(EMPTY, "rust", ["a", "b"], state="page")          # two questions, one per candidate
            with MC.tally() as inner:
                async def go():
                    return await asyncio.to_thread(lambda: MC.check(PAGE, "rust", state="page"))
                asyncio.run(go())
            with MC.closed():
                MC.check(PAGE, "rust", state="page")                    # nothing asked, nothing counted
        assert inner["calls"] == 1 and round(inner["usd"], 6) == 0.002, inner
        assert outer["calls"] == 3 and round(outer["usd"], 6) == 0.006, outer
        MC.check(PAGE, "rust", state="page")
        assert outer["calls"] == 3                                      # a tally counts only inside its block
        assert outer["noted"] == 0                                      # the fake noted no request


def test_a_tally_says_which_requests_the_process_counter_holds_too():
    from anyuse.execution import model_adapter as MA

    def backend(i, s, o):
        MA.note_external_model_call("fake", "fake", feature="model_check")      # as every backend does
        return 0.99, 1, 0.0, ""
    with Env(), MC.scope("product"), MC.tally() as box:
        with MC.using(backend):
            MC.check(EMPTY, "rust", ["a", "b"], state="page")
        with MC.using(lambda i, s, o: 0.99):
            MC.check(PAGE, "rust", state="page")
    assert (box["calls"], box["noted"]) == (3, 2), box


# -- which backend --------------------------------------------------------------

def test_auto_resolves_in_order_and_only_on_what_the_user_set():
    laya = tempfile.mkdtemp()
    cases = [
        ({}, "none"),
        ({"ANYUSE_OPENJEV_URL": "https://oj.test", "ANYUSE_CONFIRMER_LAYA_DIR": laya,
          "ANYUSE_JUDGE": "llm"}, "openjev"),
        ({"ANYUSE_OPENJEV_URL": "http://10.0.0.2:8080", "ANYUSE_CONFIRMER_LAYA_DIR": laya,
          "ANYUSE_JUDGE": "jev"}, "laya"),
        ({"ANYUSE_CONFIRMER_LAYA_DIR": os.path.join(laya, "missing"), "ANYUSE_JUDGE": "jev"},
         "jev"),
        ({"ANYUSE_JUDGE": "llm"}, "llm"),
        ({"ANYUSE_JUDGE_MODEL_PROVIDER": "local"}, "none"),
        ({"ANYUSE_MODEL_CHECKS": "none", "ANYUSE_OPENJEV_URL": "https://oj.test"}, "none"),
        ({"ANYUSE_MODEL_CHECKS": "llm", "ANYUSE_OPENJEV_URL": "https://oj.test"}, "llm"),
        ({"ANYUSE_MODEL_CHECKS": "bogus", "ANYUSE_JUDGE": "jev"}, "jev"),
    ]
    for env, want in cases:
        with Env(**env):
            assert MC.backend() == want, (env, MC.backend())
    assert MC.backend("laya") == "laya"


def test_a_reachable_judge_model_alone_asks_nothing():
    with Env(ANYUSE_JUDGE_MODEL_PROVIDER="local"), MC.scope("product"):
        assert MA.role_problem("judge") == ""          # the judge model could be called
        assert not MC.available()
        v = MC.check(PAGE, "x", state="s")
        assert v.none and v.calls == 0 and v.backend == "none"
        assert not os.path.exists(MC.log_path())


def test_an_explicit_backend_without_its_setup_is_not_available():
    with Env():
        assert MC.problem("auto") == MC.problem() == "no model check is configured " \
            "(ANYUSE_MODEL_CHECKS)"
    with Env(ANYUSE_MODEL_CHECKS="openjev"), MC.scope("product"):
        assert MC.problem("auto") == O.UNAVAILABLE
        assert not MC.available() and MC.problem() == O.UNAVAILABLE
        v = MC.check(PAGE, "x", state="s")
        assert v.none and v.calls == 0 and v.error == O.UNAVAILABLE
    with Env(ANYUSE_MODEL_CHECKS="laya"), MC.scope("product"):
        assert not MC.available()
        v = MC.check(PAGE, "x", state="s")
        assert v.none and v.calls == 0 and "ANYUSE_CONFIRMER_LAYA_DIR" in v.error


# -- the backends ---------------------------------------------------------------

def test_openjev_asks_one_noul_per_candidate_and_takes_the_verified_yes():
    page = "Results\nYour search\nNo results for rust\nSearch tips"
    p = Poster(lambda body: 0.95 if last_line(body["state"]) == "No results for rust" else 0.05)
    lines = [MC.Candidate("Search tips", context="Search"),
             MC.Candidate("No results for rust", context="Results\nYour search", ref=7)]
    with Env(ANYUSE_OPENJEV_URL="https://oj.test", ANYUSE_OPENJEV_TIMEOUT_S="3"), \
            patched(O, "_urllib_post", p), MC.scope("product"):
        mark = MA.process_call_count()
        idx, v = MC.choose(EMPTY, "rust", lines, state="URL: shop.test/search?q=rust",
                           verify=lambda c: c.text in page.splitlines(),
                           guard_obs={"url": "https://shop.test/search?q=rust", "text": page})
        assert MA.calls_since(mark) == 2
    assert idx == 1 and v.yes and v.candidate.ref == 7 and v.backend == "openjev"
    assert v.calls == 2 and v.scores == [0.05, 0.95]
    assert len(p.calls) == 2 and p.calls[0]["url"] == "https://oj.test/v1/systemone"
    body = p.calls[1]["body"]
    assert body["questions"] == {"q": {"type": "noul", "instructions": EMPTY.instructions("rust")}}
    assert body["state"] == "URL: shop.test/search?q=rust\nResults\nYour search\n>> No results for rust"
    assert "7" not in json.dumps(body["state"])
    assert all(0 < c["timeout"] <= 3.0 for c in p.calls)


class FakeSession:
    """A warm session whose cold start waits for ``gate``."""

    def __init__(self, gate=None, noul=0.9):
        self._lock = threading.Lock()
        self.proc, self.ready = None, {}
        self.started = threading.Event()
        self.gate, self.noul, self.requests = gate, noul, []

    def alive(self):
        return self.proc is not None

    def ask_many(self, requests):
        with self._lock:
            if not self.alive():
                self.started.set()
                if self.gate is not None:
                    self.gate.wait(5)
                self.proc, self.ready = object(), {"ready": True}
            out = []
            for r in requests:
                self.requests.append(r)
                out.append({"id": r["id"], "noul": self.noul, "ms": 200.0})
            return out


def test_laya_cold_start_is_never_waited_for():
    with patched(MC, "_STAYS_UP", True):
        gate = threading.Event()
        sess = FakeSession(gate=gate)
        with Env(ANYUSE_CONFIRMER_LAYA_DIR=tempfile.mkdtemp()), MC.scope("agent"):
            C._SESSIONS["en"] = sess
            t0 = time.monotonic()
            v = MC.check(PAGE, "rust", state="Results for rust")
            assert time.monotonic() - t0 < 1.0
            assert v.none and v.backend == "laya" and v.calls == 0 and "starting" in v.error
            assert sess.started.wait(2)                         # warming up in the background
            warm = MC._WARM["en"]["thread"]
            t0 = time.monotonic()
            v = MC.check(PAGE, "rust", state="Results for rust")
            assert time.monotonic() - t0 < 1.0
            assert v.none and "starting" in v.error and not sess.requests
            assert MC._WARM["en"]["thread"] is warm             # no second warm-up
            gate.set()
            warm.join(2)
            mark = MA.process_call_count()
            v = MC.check(PAGE, "rust", state="Results for rust")
            assert v.yes and v.calls == 1 and MA.calls_since(mark) == 1
        assert sess.requests == [{"id": sess.requests[0]["id"], "state": "Results for rust",
                                  "question": {"type": "noul",
                                               "instructions": PAGE.instructions("rust")}}]


def test_laya_is_started_only_by_a_process_that_stays_up():
    sess = FakeSession()
    with Env(ANYUSE_CONFIRMER_LAYA_DIR=tempfile.mkdtemp()), MC.scope("compile"), \
            patched(MC, "_STAYS_UP", False):
        C._SESSIONS["en"] = sess
        MC._WARM.pop("en", None)
        v = MC.check(PAGE, "rust", state="s")
        assert v.none and v.calls == 0 and "only the MCP server" in v.error
        assert not sess.started.is_set() and not MC._WARM.get("en", {}).get("thread")
        sess.proc, sess.ready = object(), {"ready": True}      # already warm: it is asked
        assert MC.check(PAGE, "rust", state="s").yes


def test_a_judge_role_on_a_cli_provider_is_not_asked():
    with Env(ANYUSE_JUDGE="llm", ANYUSE_MODEL_PROVIDER="claude-cli"), MC.scope("product"), \
            patched(MA.ModelAdapter, "for_role", classmethod(lambda *a, **k: 1 / 0)):
        assert "claude-cli" in MC.problem("llm") and not MC.available()
        v = MC.check(PAGE, "rust", state="s")
        assert v.none and v.calls == 0 and "claude-cli" in v.error


def test_laya_busy_with_another_question_is_no_opinion_not_a_wait():
    sess = FakeSession()
    sess.proc, sess.ready = object(), {"ready": True}
    with Env(ANYUSE_CONFIRMER_LAYA_DIR=tempfile.mkdtemp()), MC.scope("agent"):
        C._SESSIONS["en"] = sess
        with sess._lock:
            v = MC.check(PAGE, "rust", state="s")
        assert v.none and "busy" in v.error and not sess.requests
        assert MC.check(PAGE, "rust", state="s").yes


def test_laya_closed_after_a_start_warms_up_again_and_a_failed_start_waits():
    with patched(MC, "_STAYS_UP", True):
        sess = FakeSession(gate=threading.Event())
        sess.ready = {"ready": True}                 # set by an earlier start; the session closed since
        with Env(ANYUSE_CONFIRMER_LAYA_DIR=tempfile.mkdtemp()), MC.scope("agent"):
            C._SESSIONS["en"] = sess
            t0 = time.monotonic()
            v = MC.check(PAGE, "rust", state="s")
            assert time.monotonic() - t0 < 1.0 and v.none and "starting" in v.error
            assert sess.started.wait(2) and not sess.requests
            sess.gate.set()
            MC._WARM["en"]["thread"].join(2)

        class NeverStarts(FakeSession):
            def ask_many(self, requests):
                self.started.set()
                return [{"id": r["id"], "error": "FileNotFoundError: no container"}
                        for r in requests]

        dead = NeverStarts()
        with Env(ANYUSE_CONFIRMER_LAYA_DIR=tempfile.mkdtemp()), MC.scope("agent"):
            C._SESSIONS["en"] = dead
            assert "starting" in MC.check(PAGE, "rust", state="s").error
            MC._WARM["en"]["thread"].join(2)
            v = MC.check(PAGE, "rust", state="s")
            assert v.none and "again within a minute" in v.error
            assert MC._WARM["en"]["thread"].is_alive() is False
            with patched(MC, "WARM_RETRY_S", 0.0):
                assert "starting" in MC.check(PAGE, "rust", state="s").error


def test_jev_posts_one_noul_per_candidate_with_the_openrouter_key():
    posts = []

    def fake_post(url, payload, key, timeout=60.0):
        posts.append((url, payload, key, timeout))
        return {"answers": {"q": {"noul": 0.1}}, "usage": {"cost": 0.0002}}, 12.0

    def key(name):
        return "k-test" if name == "OPENROUTER_API_KEY" else ""

    with Env(ANYUSE_JUDGE="jev"), patched(J, "_read_key", key), \
            patched(J, "_post", fake_post), MC.scope("compile"):
        v = MC.check(EMPTY, "rust", ["Search tips", "Popular items"], state="s")
    assert v.no and v.backend == "jev" and v.calls == 2 and abs(v.usd - 0.0004) < 1e-12
    url, payload, k, timeout = posts[0]
    assert url == J.JEV_URL and k == "k-test" and payload["model"] == J.JEV_MODEL
    assert payload["questions"] == {"q": {"type": "noul",
                                          "instructions": EMPTY.instructions("rust")}}
    assert payload["state"] == "s\n>> Search tips" and 0 < timeout <= MC.DEFAULT_TIMEOUT_S
    with Env(ANYUSE_JUDGE="jev"), patched(J, "_read_key", lambda name: ""), \
            patched(J, "_post", fake_post), MC.scope("compile"):
        assert not MC.available()
        v = MC.check(EMPTY, "rust", ["Search tips"], state="s")
    assert v.none and v.calls == 0 and len(posts) == 2 and "OPENROUTER_API_KEY" in v.error


class FakeAdapter:
    """The ModelAdapter surface the llm backend uses."""

    def __init__(self, p):
        self.p, self.calls, self.closed = p, [], False
        self.budget = MA.RunBudget(1.0)

    def complete(self, messages, *, temperature=0.1, max_tokens=None, json_schema=None,
                 schema_name="response", images=()):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, "json_schema": json_schema})
        self.budget.settle(self.budget.reserve(max_tokens, 0.25, 0.69), 0.00002)
        return MA.ModelResult(ok=True, text=json.dumps({"p": self.p}), parsed={"p": self.p},
                              llm_calls=1, provider="local", model="m")

    def close(self):
        self.closed = True


def test_llm_calls_complete_on_the_judge_role_adapter():
    made = []

    def for_role(role, **kw):
        made.append((role, kw, FakeAdapter(0.9)))
        return made[-1][2]

    with Env(ANYUSE_JUDGE="llm"), patched(MA.ModelAdapter, "for_role", staticmethod(for_role)), \
            MC.scope("product"):
        v = MC.check(PAGE, "rust", state="Results for rust: 3 items")
    role, kw, adapter = made[0]
    assert role == "judge" and kw["feature"] == "model_check" and kw["retries"] == 0
    assert 1.0 <= kw["timeout"] <= MC.DEFAULT_TIMEOUT_S
    call = adapter.calls[0]
    assert call["temperature"] == 0.0 and call["json_schema"]["required"] == ["p"]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][0]["content"].startswith(PAGE.instructions("rust"))
    assert call["messages"][1] == {"role": "user", "content": "DATA:\nResults for rust: 3 items"}
    assert v.yes and v.backend == "llm" and v.calls == 1 and abs(v.usd - 0.00002) < 1e-12
    assert adapter.closed


def test_llm_books_its_spend_under_model_check():
    import httpx
    from anyuse.execution import spend
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": '{"p": 0.05}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}})

    real = vars(MA.ModelAdapter)["for_role"].__func__

    def for_role(role, **kw):
        return real(MA.ModelAdapter, role, transport=httpx.MockTransport(handler), **kw)

    with Env(ANYUSE_JUDGE="llm", ANYUSE_JUDGE_MODEL_PROVIDER="local"), \
            patched(MA.ModelAdapter, "for_role", staticmethod(for_role)), MC.scope("product"):
        spend.arm()
        try:
            v = MC.check(PAGE, "rust", state="Results for rust")
            with open(spend.ledger_path(), encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
        finally:
            spend.disarm()
    assert v.no and v.calls == 1 and v.usd > 0
    assert seen[0]["temperature"] == 0.0 and seen[0]["max_tokens"] == MC.LLM_MAX_TOKENS
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert rows[-1]["feature"] == "model_check" and abs(rows[-1]["cost_usd"] - v.usd) < 1e-9


# -- the verdict ----------------------------------------------------------------

def test_a_yes_counts_only_for_a_candidate_that_passes_verify():
    scores = {"A": 0.97, "B": 0.85, "C": 0.1}
    with Env(), MC.using(lambda i, s, o: scores[last_line(s)]), MC.scope("product"):
        idx, v = MC.choose(EMPTY, "q", ["A", "B", "C"], verify=lambda c: c.text != "A")
        assert idx == 1 and v.candidate.text == "B" and v.p == 0.85
        idx, v = MC.choose(EMPTY, "q", ["A", "B", "C"])
        assert idx == 0
        idx, v = MC.choose(EMPTY, "q", ["A", "B", "C"], verify=lambda c: False)
        assert idx is None and v.none and "verification" in v.why
        idx, v = MC.choose(EMPTY, "q", ["A", "B"], verify=lambda c: 1 / 0)
        assert idx is None and v.none and "ZeroDivisionError" in v.error


def test_no_only_when_every_candidate_was_answered_no():
    with Env(), MC.scope("product"):
        with MC.using(lambda i, s, o: 0.05):
            v = MC.check(EMPTY, "q", ["A", "B", "C"])
            assert v.no and v.calls == 3
            v = MC.check(EMPTY, "q", ["A", "B", "C"], max_candidates=2)
            assert v.none and v.calls == 2 and v.scores == [0.05, 0.05, None]
            v = MC.check(EMPTY, "q", ["A", "B", "C"], max_calls=1)
            assert v.none and v.calls == 1
        answers = iter([0.05, None, 0.05])
        with MC.using(lambda i, s, o: next(answers)):
            v = MC.check(EMPTY, "q", ["A", "B", "C"])
            assert v.none and v.calls == 1 and v.error == "no answer"
        with MC.using(lambda i, s, o: 0.5):
            v = MC.check(EMPTY, "q", ["A", "B"])
            assert v.none and "unclear" in v.why


def test_without_candidates_one_question_about_the_state():
    with Env(), MC.scope("product"):
        for p, want in ((0.9, "yes"), (0.8, "yes"), (0.5, "none"), (0.2, "no"), (0.1, "no")):
            with MC.using(lambda i, s, o, p=p: p):
                v = MC.check(PAGE, "x", state="s")
                assert v.answer == want and v.scores == [p], (p, v)
        try:
            MC.check(PAGE, "x", state="s", threshold=0.5)
            raise AssertionError("a threshold of 0.5 was accepted")
        except ValueError:
            pass


def test_any_exception_or_bad_answer_is_no_opinion():
    with Env(), MC.scope("product"):
        with MC.using(lambda i, s, o: 1 / 0):
            v = MC.check(PAGE, "q", state="s")
            assert v.none and "ZeroDivisionError" in v.error
        with MC.using(lambda i, s, o: 1.7):
            assert MC.check(PAGE, "q", state="s").none
        with MC.using(lambda i, s, o: (0.9, 2, 0.001, "")):
            v = MC.check(PAGE, "q", state="s")
            assert v.yes and v.calls == 2 and v.usd == 0.001


def test_one_deadline_for_all_questions():
    asked = []

    def slow(i, s, o):
        asked.append(s)
        time.sleep(0.25)
        return 0.05

    with Env(), MC.using(slow), MC.scope("product"):
        t0 = time.monotonic()
        v = MC.check(EMPTY, "q", ["A", "B", "C", "D"], timeout_s=0.4)
        took = time.monotonic() - t0
    assert v.none and v.error == "no answer within 0.4 s" and took < 0.6 and v.calls == 2
    time.sleep(0.4)
    assert len(asked) == 2                     # no question starts after the deadline
    with Env(ANYUSE_MODEL_CHECK_TIMEOUT_S="0.2"), MC.using(slow), MC.scope("product"):
        v = MC.check(PAGE, "q", state="s")
    assert v.none and v.error == "no answer within 0.2 s"


def test_a_question_that_ends_at_the_deadline_is_read_with_the_others():
    started_last, release = threading.Event(), threading.Event()

    class EndsAtTheDeadline(threading.Thread):
        """The wait times out while the last question is in flight, and that
        question ends right after: before the check looks at the worker again."""

        def join(self, timeout=None):
            if release.is_set():
                return super().join(timeout)
            started_last.wait(2)

        def is_alive(self):
            if not release.is_set():
                release.set()
                super().join(2)
            return super().is_alive()

    def ask(i, s, o):
        if last_line(s) == "C":
            started_last.set()
            release.wait(2)
            return 0.99
        return 0.9 if last_line(s) == "A" else 0.05

    with Env(), MC.using(ask), MC.scope("product"), \
            patched(MC.threading, "Thread", EndsAtTheDeadline):
        idx, v = MC.choose(EMPTY, "q", ["A", "B", "C"])
    assert v.scores == [0.9, 0.05, 0.99] and idx == 2 and v.calls == 3


def test_the_challenge_guard_runs_before_any_backend():
    asked = []
    with Env(), MC.using(lambda i, s, o: asked.append(s) or 0.99), MC.scope("product"):
        v = MC.check(PAGE, "q", state=CHALLENGE_TEXT)        # the default guard_obs is the state
        assert v.none and v.error.startswith(O.GUARDED) and v.calls == 0
        v = MC.check(EMPTY, "q", [CHALLENGE_TEXT])           # and the candidates' text
        assert v.none and v.error.startswith(O.GUARDED)
        v = MC.check(PAGE, "q", state="Results", guard_obs=CHALLENGE_OBS)
        assert v.none and v.error.startswith(O.GUARDED)
    assert not asked
    p = Poster(lambda body: 0.99)
    with Env(ANYUSE_OPENJEV_URL="https://oj.test"), patched(O, "_urllib_post", p), \
            MC.scope("product"):
        v = MC.check(PAGE, "q", state=CHALLENGE_TEXT)
    assert v.none and v.calls == 0 and not p.calls


def test_one_log_line_per_check_without_page_text_or_the_input():
    state = "Account of Jane Roe, 12 Elm St"
    value = "zebra-velvet-42"
    with Env(), MC.using(lambda i, s, o: 0.95 if value in s else 0.05), MC.scope("product"):
        MC.check(EMPTY, value, [MC.Candidate("Search tips", context=state),
                                f"No results for {value}"],
                 state=state, guard_obs={"url": f"https://shop.test/s?q={value}", "text": state},
                 where="result_contract.empty")
        MC.check(PAGE, value, state=state, where="visit_results")
        with open(MC.log_path(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert {"where", "kind", "backend", "host", "n", "scores", "answer", "calls", "ms", "usd",
            "error"} <= set(rec)
    assert (rec["where"], rec["kind"], rec["backend"], rec["host"], rec["n"]) == \
        ("result_contract.empty", EMPTY.kind, "fake", "shop.test", 2)
    assert rec["scores"] == [0.05, 0.95] and rec["answer"] == "yes" and rec["calls"] == 2
    raw = "\n".join(lines)
    for text in ("zebra", "velvet", "Jane", "Elm", "Search tips", "No results"):
        assert text not in raw, text


# -- questions, settings, the runner ----------------------------------------------

def test_one_wording_per_kind_and_the_target_is_cut():
    q = MC.Question("test_wording", "Is this about '{target}'?")
    assert MC.Question("test_wording", "Is this about '{target}'?") == q
    try:
        MC.Question("test_wording", "Is this about '{target}', or a list, or a help page?")
        raise AssertionError("a second wording for one kind was accepted")
    except ValueError:
        pass
    assert q.instructions("it's  " + "x" * 200) == "Is this about 'it’s " + "x" * 75 + "'?"


def test_settings_and_their_docs():
    from anyuse import settings as S
    spec = S.SETTINGS["ANYUSE_MODEL_CHECKS"]
    assert spec.default == "auto" and tuple(spec.allowed_values) == MC.BACKENDS
    assert S.SETTINGS["ANYUSE_MODEL_CHECK_TIMEOUT_S"].default == MC.DEFAULT_TIMEOUT_S
    for raw, want in (("2.5", 2.5), ("0", MC.DEFAULT_TIMEOUT_S), ("abc", MC.DEFAULT_TIMEOUT_S)):
        with Env(ANYUSE_MODEL_CHECK_TIMEOUT_S=raw):
            assert MC.resolve_timeout() == want, raw
    with open(os.path.join(HERE, "docs", "SETTINGS.md"), encoding="utf-8") as fh:
        table = fh.read()
    assert "| `ANYUSE_MODEL_CHECKS` | `auto`, `none`, `openjev`, `laya`, `jev`, `llm` | `auto` |" \
        in table and "| `ANYUSE_MODEL_CHECK_TIMEOUT_S` |" in table
    with open(os.path.join(HERE, "docs", "MODEL_CONFIGURATION.md"), encoding="utf-8") as fh:
        doc = fh.read()
    section = doc.split("## Small-model checks", 1)[1].split("\n## ", 1)[0]
    for name in MC.BACKENDS:
        assert f"`{name}`" in section, name
    assert "Nothing is asked unless" in section


def test_the_guarded_runner_turns_model_checks_off():
    try:
        import sitecustomize
    except ImportError:
        return
    if getattr(sitecustomize, "MARK", "") != "DESKTOP GUARD":
        return                                 # only under tools/guarded_tests/run_allowed.sh
    assert os.environ.get("ANYUSE_MODEL_CHECKS") == "none"
    for name in ("ANYUSE_OPENJEV_URL", "ANYUSE_JUDGE", "ANYUSE_CONFIRMER",
                 "ANYUSE_CONFIRMER_LAYA_DIR"):
        assert name not in os.environ, name
    assert MC.backend() == "none"
    with MC.scope("product"):
        assert not MC.available()


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
