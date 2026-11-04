"""Deterministic token-free replay: ranked fall-through over best_implementation,
driving the executor's per-modality dispatch with ZERO LLM calls."""
import asyncio
import pathlib
import sys
import tempfile

# A bare `from anyuse... import ...` resolves
# through whatever "anyuse"-named directory is first on sys.path/PYTHONPATH.
# The guarded runner's PYTHONPATH includes this checkout's PARENT directory,
# which in this repository layout also contains the actual `anyuse`
# web-branch checkout as a sibling — this file was found silently
# importing THAT checkout's (pre-unification) `execution/deterministic.py`
# instead of this one's, so its checked-path assertions passed against
# the wrong code. A private directory holding a symlink `anyuse` -> this
# checkout, put at the FRONT of ``sys.path``, makes the package always
# resolve to the tree this test file lives in.
_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINKDIR = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
_PKGROOT = pathlib.Path(_LINKDIR.name)
(_PKGROOT / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, str(_PKGROOT))

from anyuse.execution.deterministic import rank_implementations, replay_transitions
from anyuse.guides.state_graph import TransitionImpl, UITransition


class _FakeExecutor:
    """Stands in for StateGraphExecutor — records dispatch, no real actuation/LLM."""
    def __init__(self, fail_modalities=()):
        self.fail = set(fail_modalities)
        self.dispatched = []

    async def _execute_step(self, step):
        self.dispatched.append((step.modality, dict(step.command)))
        return {"success": step.modality not in self.fail}


def _t(frm, to, intent, impls):
    return UITransition(from_state=frm, to_state=to, intent=intent, implementations=impls)


# ── ranking (the ladder) ─────────────────────────────────────────────────────
def test_rank_proven_beats_unproven():
    t = _t("a", "b", "x", [
        TransitionImpl(modality="ax", times_used=0, times_succeeded=0),          # unproven, most stable
        TransitionImpl(modality="coordinate", times_used=4, times_succeeded=4),  # proven 1.0
    ])
    assert rank_implementations(t)[0].modality == "coordinate"


def test_rank_stability_when_all_unproven():
    t = _t("a", "b", "x", [TransitionImpl(modality="coordinate"),
                           TransitionImpl(modality="ax"),
                           TransitionImpl(modality="computer_use")])
    assert [i.modality for i in rank_implementations(t)] == ["ax", "coordinate", "computer_use"]


# ── replay ───────────────────────────────────────────────────────────────────
def test_replay_token_free_success():
    t1 = _t("a", "b", "open", [TransitionImpl(modality="ax", command={"element_path": "/btn", "action": "press"})])
    t2 = _t("b", "c", "save", [TransitionImpl(modality="coordinate", command={"x": 10, "y": 20, "action": "click"})])
    ex = _FakeExecutor()
    res = asyncio.run(replay_transitions([t1, t2], ex))
    assert res["success"] is True and res["llm_calls"] == 0
    assert [d[0] for d in ex.dispatched] == ["ax", "coordinate"]
    assert t1.implementations[0].times_succeeded == 1  # usage recorded


def test_replay_aggregates_actual_model_calls_from_every_attempt():
    class CountingExecutor(_FakeExecutor):
        async def _execute_step(self, step):
            self.dispatched.append((step.modality, dict(step.command)))
            if step.modality == "ax":
                return {"success": False, "outcome_kind": "retryable_failure", "llm_calls": 2}
            return {"success": True, "model_calls": 1}

    t = _t("a", "b", "x", [TransitionImpl(modality="ax"),
                              TransitionImpl(modality="computer_use")])
    out = asyncio.run(replay_transitions([t], CountingExecutor()))
    assert out["success"] is True
    assert out["llm_calls"] == 3, out
    assert [row["llm_calls"] for row in out["steps"][0]["tried"]] == [2, 1]


def test_deterministic_import_does_not_load_model_backed_modules():
    assert "anyuse.execution.planner" not in sys.modules
    assert "anyuse.guides.generator" not in sys.modules


def test_replay_fall_through_on_failure():
    t = _t("a", "b", "x", [TransitionImpl(modality="ax", command={"element_path": "/x"}),
                           TransitionImpl(modality="coordinate", command={"x": 1, "y": 2})])
    ex = _FakeExecutor(fail_modalities={"ax"})
    res = asyncio.run(replay_transitions([t], ex))
    assert res["success"] is True
    assert [d[0] for d in ex.dispatched] == ["ax", "coordinate"]
    assert res["steps"][0]["modality_used"] == "coordinate"


def test_ladder_stops_on_terminal_refusal_and_preserves_kind():
    class Refusing(_FakeExecutor):
        async def _execute_step(self, step):
            self.dispatched.append((step.modality, dict(step.command)))
            return {"success": False, "blocked": True,
                    "requires_human": True, "outcome_kind": "terminal_refusal"}

    t = _t("a", "b", "x", [TransitionImpl(modality="api"),
                              TransitionImpl(modality="coordinate")])
    ex = Refusing()
    out = asyncio.run(replay_transitions([t], ex))
    assert out["outcome_kind"] == "terminal_refusal"
    assert [row[0] for row in ex.dispatched] == ["api"]


def test_ladder_stops_on_uncertain_completion_instead_of_retrying():
    class Uncertain(_FakeExecutor):
        async def _execute_step(self, step):
            self.dispatched.append((step.modality, dict(step.command)))
            return {"success": False, "outcome_kind": "uncertain_completion",
                    "error": "write may have completed"}

    t = _t("a", "b", "x", [TransitionImpl(modality="ax"),
                              TransitionImpl(modality="coordinate")])
    ex = Uncertain()
    out = asyncio.run(replay_transitions([t], ex))
    assert out["outcome_kind"] == "uncertain_completion"
    assert len(ex.dispatched) == 1


def test_ladder_retries_only_retryable_failure():
    class Retry(_FakeExecutor):
        async def _execute_step(self, step):
            self.dispatched.append((step.modality, dict(step.command)))
            if step.modality == "ax":
                return {"success": False, "outcome_kind": "retryable_failure"}
            return {"success": True, "outcome_kind": "success"}

    t = _t("a", "b", "x", [TransitionImpl(modality="ax"),
                              TransitionImpl(modality="coordinate")])
    ex = Retry()
    out = asyncio.run(replay_transitions([t], ex))
    assert out["success"] is True
    assert len(ex.dispatched) == 2


def test_replay_stops_on_total_failure():
    t = _t("a", "b", "x", [TransitionImpl(modality="ax")])
    ex = _FakeExecutor(fail_modalities={"ax"})
    res = asyncio.run(replay_transitions([t], ex))
    assert res["success"] is False and res["stopped_at"] == "x" and res["llm_calls"] == 0


def test_impl_to_step_carries_typed_text():
    # The type-text-loss fix: a recorded type impl now carries "text" → impl_to_step
    # surfaces it as input_data so the executor types it (was silently dropped).
    from anyuse.execution.deterministic import impl_to_step
    t = _t("a", "b", "type", [TransitionImpl(modality="coordinate",
        command={"absolute_x": 1, "absolute_y": 2, "action": "type_text_at", "text": "Hello"})])
    step = impl_to_step(t, t.implementations[0])
    assert step.input_data == "Hello", step.input_data
    assert step.command["action"] == "type" and step.command["x"] == 1


def test_replay_no_implementations():
    res = asyncio.run(replay_transitions([_t("a", "b", "x", [])], _FakeExecutor()))
    assert res["success"] is False and "no implementations" in res["steps"][0]["reason"]


def test_coordinate_command_translated_to_executor_shape():
    # THE real recorder shape — absolute_x/y + a *_at verb (not the synthetic
    # {'x','y','action':'click'} the earlier test used). Without the fix this
    # replayed at (0,0) and downgraded the type to a click.
    t = _t("a", "b", "x", [TransitionImpl(modality="coordinate", command={
        "absolute_x": 100, "absolute_y": 200, "relative_x": 0.5, "relative_y": 0.5,
        "action": "type_text_at",
    })])
    ex = _FakeExecutor()
    res = asyncio.run(replay_transitions([t], ex))
    assert res["success"] is True
    mod, cmd = ex.dispatched[0]
    assert mod == "coordinate"
    assert cmd["x"] == 100 and cmd["y"] == 200, cmd   # was (0,0) before the fix
    assert cmd["action"] == "type", cmd               # was downgraded to click before


# ── replay goes through the checked path, not _execute_step raw ─────────────
class _CheckedFakeExecutor:
    """Implements execute_checked_step (the checked path) AND _execute_step,
    so a test can tell which one replay_transitions actually calls."""
    def __init__(self, fail_modalities=()):
        self.fail = set(fail_modalities)
        self.checked_calls = []
        self.raw_calls = []

    async def execute_checked_step(self, step):
        self.checked_calls.append((step.modality, dict(step.command)))
        return {"success": step.modality not in self.fail}, {"ax_diff_summary": "no changes"}

    async def _execute_step(self, step):
        # Present so a raw-path caller COULD reach it; asserted never called below.
        self.raw_calls.append((step.modality, dict(step.command)))
        return {"success": step.modality not in self.fail}


def test_replay_uses_the_checked_path_not_execute_step_raw():
    t = _t("a", "b", "open", [TransitionImpl(modality="ax", command={"role": "AXButton", "action": "press"})])
    ex = _CheckedFakeExecutor()
    res = asyncio.run(replay_transitions([t], ex))
    assert res["success"] is True
    assert ex.checked_calls == [("ax", {"role": "AXButton", "action": "press"})], ex.checked_calls
    assert ex.raw_calls == [], "replay_transitions must not call _execute_step directly when the checked path exists"


def test_replay_reports_verification_evidence_alongside_the_outcome():
    t = _t("a", "b", "open", [TransitionImpl(modality="ax", command={"role": "AXButton", "action": "press"})])
    ex = _CheckedFakeExecutor()
    res = asyncio.run(replay_transitions([t], ex))
    assert res["steps"][0]["tried"][0]["ax_diff_summary"] == "no changes", res


def test_replay_falls_back_to_execute_step_when_checked_path_absent():
    # Byte-for-byte prior behavior for a fake/spy that only implements
    # _execute_step (existing tests above use exactly this shape).
    t = _t("a", "b", "x", [TransitionImpl(modality="ax", command={"role": "AXButton"})])
    ex = _FakeExecutor()
    res = asyncio.run(replay_transitions([t], ex))
    assert res["success"] is True
    assert ex.dispatched == [("ax", {"role": "AXButton"})]


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:200])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
