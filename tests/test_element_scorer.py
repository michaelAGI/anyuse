"""The opt-in weighted element matcher, the drift-gate tolerance, and the
selfheal fit/eval command.

Synthetic AX-shaped fixtures only. No AX API, no desktop, no network.
"""
import json
import os
import tempfile

from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.context.element_scorer import (
    DEFAULT_MIN_MARGIN,
    WEB_FITTED_WEIGHTS,
    evaluate,
    fit,
    matcher_mode,
    rank,
    resolve,
    resolve_cascade,
    resolve_weighted,
    rewritten_selector,
    score,
)
from anyuse.execution.drift_gate import _classify, _env_tolerance


def ax(role="AXButton", label="", identifier="", path="", order=0.0, **kw):
    el = {"role": role, "label": label, "identifier": identifier,
          "ax_path": path, "order_frac": order}
    el.update(kw)
    return el


SAVE = ax("AXButton", "Save Document", "save-btn", "AXWindow/AXGroup/AXButton", 0.30)


# ---------------------------------------------------------------------------
# default is unchanged
# ---------------------------------------------------------------------------

def test_the_default_matcher_is_the_cascade():
    assert matcher_mode() == "cascade"
    assert matcher_mode("anything-else") == "cascade"
    assert matcher_mode("weighted") == "weighted"


def test_resolve_defaults_to_cascade_behaviour():
    cands = [ax("AXButton", "Save Document", "save-btn"), ax("AXButton", "Cancel")]
    res = resolve(SAVE, cands)
    assert res.mode == "cascade" and res.index == 0 and res.abstained is False


def test_the_env_switch_selects_the_weighted_matcher():
    prev = os.environ.get("ANYUSE_ELEMENT_MATCHER")
    os.environ["ANYUSE_ELEMENT_MATCHER"] = "weighted"
    try:
        assert resolve(SAVE, [ax("AXButton", "Save Document")]).mode == "weighted"
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_ELEMENT_MATCHER", None)
        else:
            os.environ["ANYUSE_ELEMENT_MATCHER"] = prev


# ---------------------------------------------------------------------------
# what the weighted matcher buys
# ---------------------------------------------------------------------------

def test_the_cascade_cannot_match_a_drifted_label_and_the_weighted_one_can():
    # The study's text-drift slice: the cascade scores 0.000 there by
    # construction, because an exact match is impossible once a label changes.
    cands = [ax("AXButton", "Save Doc", path="AXWindow/AXGroup/AXButton", order=0.31),
             ax("AXButton", "Cancel", path="AXWindow/AXGroup/AXButton", order=0.60)]
    assert resolve_cascade(SAVE, cands).abstained is True
    weighted = resolve_weighted(SAVE, cands)
    assert weighted.abstained is False and weighted.index == 0
    assert weighted.margin > 0


def test_an_exact_identifier_still_wins_outright():
    cands = [ax("AXButton", "Totally Different"), ax("AXButton", "x", "save-btn")]
    assert resolve_cascade(SAVE, cands).index == 1


def test_the_scorer_abstains_on_two_plausible_candidates():
    twins = [ax("AXButton", "Save Document", path="AXWindow/AXGroup/AXButton"),
             ax("AXButton", "Save Document", path="AXWindow/AXGroup/AXButton")]
    res = resolve_weighted(SAVE, twins)
    assert res.abstained is True and res.low_margin is True
    assert "abstaining rather than guessing" in res.reason


def test_the_scorer_abstains_when_nothing_is_close_enough():
    res = resolve_weighted(SAVE, [ax("AXStaticText", "unrelated text", order=0.9)])
    assert res.abstained is True and res.element is None
    assert "below" in res.reason


def test_no_candidates_is_an_abstention_not_a_crash():
    res = resolve_weighted(SAVE, [])
    assert res.abstained is True and res.index == -1


def test_confidence_is_tempered_by_the_margin():
    clear = resolve_weighted(SAVE, [ax("AXButton", "Save Document", "save-btn"),
                                    ax("AXStaticText", "nothing", order=0.95)])
    assert clear.confidence > 0.5
    assert clear.margin >= DEFAULT_MIN_MARGIN


def test_scores_are_in_range_and_ranking_is_stable():
    cands = [ax("AXButton", "Save Document"), ax("AXButton", "Save Document")]
    for c in cands:
        assert 0.0 <= score(SAVE, c) <= 1.0
    assert [i for i, _ in rank(SAVE, cands)] == [0, 1], "ties keep document order"


def test_missing_ax_components_are_renormalised_not_penalised():
    # AX elements have no classes; a component that is always 0 must not cap
    # every score (that is renormalisation, NOT a refit — see the docstring).
    # role + label + identifier-bearing match, with no classes/ancestors/siblings
    # on either side: the present components carry it, rather than the absent
    # ones dragging the score toward zero.
    without = score(SAVE, ax("AXButton", "Save Document", "save-btn"))
    with_path = score(SAVE, ax("AXButton", "Save Document", "save-btn",
                               path="AXWindow/AXGroup/AXButton", order=0.30))
    assert without > 0.7, without
    assert with_path > without, "a matching path must still add signal"


# ---------------------------------------------------------------------------
# selector repair (defect (e)) — opt-in path only
# ---------------------------------------------------------------------------

def test_a_confident_weighted_reanchor_yields_a_durable_replacement_selector():
    cands = [ax("AXButton", "Save Document", "save-btn-v2",
                path="AXWindow/AXGroup/AXButton", order=0.30),
             ax("AXStaticText", "unrelated", order=0.95)]
    res = resolve_weighted(SAVE, cands)
    repl = rewritten_selector(res)
    assert repl is not None and repl["identifier"] == "save-btn-v2"
    assert repl["_repair"]["matcher"] == "weighted" and repl["_repair"]["margin"] > 0


def test_no_selector_is_ever_rewritten_in_cascade_mode_or_on_a_thin_margin():
    assert rewritten_selector(resolve_cascade(SAVE, [ax("AXButton", "Save Document",
                                                        "save-btn")])) is None
    twins = [ax("AXButton", "Save Document"), ax("AXButton", "Save Document")]
    assert rewritten_selector(resolve_weighted(SAVE, twins)) is None


def test_a_repair_is_refused_when_the_new_element_is_not_durable():
    cands = [ax("AXGroup", "", path="AXWindow/AXGroup/AXButton", order=0.30)]
    assert rewritten_selector(resolve_weighted(SAVE, cands)) is None


# ---------------------------------------------------------------------------
# drift-gate tolerance
# ---------------------------------------------------------------------------

def _match(missing, found):
    return {"required_missing": ["m"] * missing, "required_found": ["f"] * found}


def test_the_drift_gate_default_tolerance_is_five_percent():
    # 0.05, measured with the resolver fixes in place
    #: one of 20 anchors missing is tolerated, two are not,
    # and with fewer than 20 anchors none is
    assert _env_tolerance() == 0.05
    assert _classify(_match(1, 19)) == (False, "")
    assert _classify(_match(2, 18)) == (True, "required_missing")
    assert _classify(_match(1, 18)) == (True, "required_missing")


def test_a_tolerance_absorbs_a_small_fraction_of_missing_anchors():
    assert _classify(_match(1, 19), 0.10) == (False, "")
    assert _classify(_match(4, 16), 0.10) == (True, "required_missing")


def test_a_single_anchor_cannot_be_tolerated_into_silence():
    assert _classify(_match(1, 0), 0.5) == (True, "required_missing")


def test_a_bad_tolerance_env_value_falls_back_to_zero():
    prev = os.environ.get("ANYUSE_DRIFT_MISSING_TOLERANCE")
    os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = "not-a-number"
    try:
        assert _env_tolerance() == 0.0
    finally:
        if prev is None:
            os.environ.pop("ANYUSE_DRIFT_MISSING_TOLERANCE", None)
        else:
            os.environ["ANYUSE_DRIFT_MISSING_TOLERANCE"] = prev


# ---------------------------------------------------------------------------
# evaluate + fit
# ---------------------------------------------------------------------------

def _pairs():
    return [
        {"target": SAVE, "correct": 0,
         "candidates": [ax("AXButton", "Save Doc", path="AXWindow/AXGroup/AXButton",
                           order=0.31),
                        ax("AXButton", "Cancel", order=0.7)]},
        {"target": ax("AXTextField", "Search packages", "q", order=0.1), "correct": 1,
         "candidates": [ax("AXButton", "Go", order=0.9),
                        ax("AXTextField", "Search packages", "q", order=0.1)]},
        {"target": ax("AXButton", "Deleted Thing", "gone-btn"), "correct": -1,
         "candidates": [ax("AXStaticText", "nothing like it", order=0.95)]},
    ]


def test_evaluate_counts_abstaining_on_a_gone_element_as_correct():
    rep = evaluate(_pairs(), "weighted")
    assert rep.n == 3 and rep.top1 > 0.6 and rep.wrong == 0.0
    assert rep.abstained > 0.0, "the gone element must produce an abstention"


def test_evaluate_shows_the_cascade_losing_on_drifted_text():
    cascade = evaluate(_pairs(), "cascade")
    weighted = evaluate(_pairs(), "weighted")
    assert weighted.top1 >= cascade.top1


def test_fit_returns_normalised_weights_and_never_makes_things_worse():
    weights, report = fit(_pairs(), rounds=2)
    assert abs(sum(weights.values()) - 1.0) < 1e-6
    assert set(weights) == set(WEB_FITTED_WEIGHTS)
    assert report.top1 >= evaluate(_pairs(), "weighted", WEB_FITTED_WEIGHTS).top1


def test_evaluate_on_no_pairs_is_zero_not_a_crash():
    assert evaluate([], "weighted").n == 0


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------

def test_selfheal_eval_compares_both_matchers_and_flags_the_prior():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "pairs.json")
    with open(path, "w") as fh:
        json.dump(_pairs(), fh)
    res = CliRunner().invoke(cli, ["selfheal", "eval", path])
    assert res.exit_code == 0, res.output
    assert "cascade" in res.output and "weighted" in res.output
    assert "WEB-FITTED PRIOR" in res.output
    assert "Refit before trusting it" in res.output


def test_selfheal_fit_writes_weights_and_reports_prior_versus_fitted():
    tmp = tempfile.mkdtemp()
    pairs_path = os.path.join(tmp, "pairs.json")
    out_path = os.path.join(tmp, "w.json")
    with open(pairs_path, "w") as fh:
        json.dump({"pairs": _pairs()}, fh)
    res = CliRunner().invoke(cli, ["selfheal", "fit", pairs_path, "--out", out_path,
                                   "--rounds", "1"])
    assert res.exit_code == 0, res.output
    assert "prior" in res.output and "fitted" in res.output
    assert "ANYUSE_ELEMENT_MATCHER=weighted" in res.output
    with open(out_path) as fh:
        assert set(json.load(fh)) == set(WEB_FITTED_WEIGHTS)


def test_selfheal_refuses_an_empty_pairs_file():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "empty.json")
    with open(path, "w") as fh:
        json.dump([], fh)
    assert CliRunner().invoke(cli, ["selfheal", "eval", path]).exit_code != 0


if __name__ == "__main__":
    import inspect
    import sys
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
