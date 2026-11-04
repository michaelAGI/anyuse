"""(n) the design notes / the design notes: the bench ladder records the
runner-up binding counter.

``execution/web_runtime/ladder.py`` keeps ``binding_alternatives`` / ``binding_rejected`` /
``runner_up_chosen`` on every row and totals them in the report, so one run
over stored recordings says whether the runner-up binding machinery ever fires
(it was to be kept only if it did).

Run from the anyuse package dir:
    cd anyuse && python3 test_ladder_runner_up.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.execution.web_runtime import ladder as L  # noqa: E402


class _Compiled:
    def __init__(self, alternatives=0, rejected=0, where=""):
        self.binding_alternatives = [{"site": i} for i in range(alternatives)]
        self.binding_rejected = rejected
        self.binding = {"where": where} if where else {}


def test_the_ladder_keeps_the_runner_up_binding_counter():
    assert L.runner_up_counts(_Compiled()) == {"binding_alternatives": 0, "binding_rejected": 0,
                                                "runner_up_chosen": False}
    rows = [dict(L.runner_up_counts(_Compiled(2, 1, "alternative")), tier=L.PROVED_TIER1),
            dict(L.runner_up_counts(_Compiled(1, 1)), tier="tier3_agent"),
            dict(L.runner_up_counts(_Compiled()), tier="tier3_agent")]
    s = L.runner_up_summary(rows)
    assert s == {"workflows": 3, "offered": 2, "alternatives": 3, "rejected": 2, "chosen": 1,
                 "chosen_and_proved": 1}, s
    table_rows = [dict(r, id=f"w{i}", site="s", category="c", cls="list", in_d_allowed=True,
                       rung="", correct=None, reason="", correctness_note="",
                       tier1_model_calls=0, tier2_model_calls=0, tier1_ms=None, tier2_ms=None)
                  for i, r in enumerate(rows)]
    md = L.tables(table_rows, "on")
    assert "## Runner-up binding counter" in md and "| 3 | 2 | 3 | 2 | 1 | 1 |" in md, md[-600:]


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:  # noqa: BLE001
            f += 1; print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
