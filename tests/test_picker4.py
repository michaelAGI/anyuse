"""The picker's "none of these" in a FIXED last position in both orders, offline (a
scripted chooser; no model); the pairwise tie-break stays off.

Measured before: the reversed order put "none" first and
the model chose it; the pairwise tie-break was consistent in 2 of 35 questions.

What must hold:

1. with the ``none_position`` rule, both orders end with "none of these"; the second order
   reverses only the candidates, and its answer maps back to the right candidate;
2. both orders naming the same candidate at >= t2 is accepted; "none" in both, "none"
   in one (no asymmetric acceptance), disagreeing orders (no tie-break, no extra call)
   are hand-offs;
3. the previous rule (no ``none_position``) is unchanged: the whole list reversed, none
   first in the second order.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402

OPTS = ["Acme Kettle 2000 | /p/acme-2000", "Acme Kettle 3000 | /p/acme-3000", "Delta Mixer 7 | /p/delta-7"]


def chooser_by_text(want, conf=0.97, log=None):
    """Answers the index of the option whose text starts with ``want`` in the list shown."""
    def ch(state, q, lst):
        if log is not None:
            log.append(list(lst))
        for i, o in enumerate(lst):
            if o.startswith(want):
                return i, conf, {}
        return len(lst) - 1, conf, {}
    return ch


def test_none_is_fixed_last_in_both_orders():
    rule = W.load_picker_rule(W.PICKER4_PATH)
    assert rule["none_position"] == "last" and rule["tie_break"] == "off" and rule["asym_item"] is None
    seen = []
    p = W.consistent_pick(chooser_by_text("Acme Kettle 3000", log=seen), "s", "Which?", OPTS, 0.9, rule=rule)
    assert seen[0][-1] == W.NONE_OPTION and seen[1][-1] == W.NONE_OPTION, seen
    assert seen[1][:-1] == OPTS[::-1]
    assert p.index == 1 and p.calls == 2 and "accepted" in p.reason, p
    p = W.consistent_pick(chooser_by_text("Delta Mixer 7"), "s", "Which?", OPTS, 0.9, rule=rule)
    assert p.index == 2


def test_hand_offs_without_tie_break_or_asymmetric_acceptance():
    rule = W.load_picker_rule(W.PICKER4_PATH)
    p = W.consistent_pick(chooser_by_text(W.NONE_OPTION), "s", "Which?", OPTS, 0.9, rule=rule)
    assert p.index is None and "both orders" in p.reason
    calls = []

    def flip(state, q, lst):
        calls.append(1)
        # always the first listed candidate: o0 names OPTS[0], o1 names OPTS[2]
        return 0, 0.99, {}
    p = W.consistent_pick(flip, "s", "Which?", OPTS, 0.9, rule=rule)
    assert p.index is None and "disagree" in p.reason and len(calls) == 2 and p.calls == 2, p

    def one_none(state, q, lst):
        return (len(lst) - 1, 0.5, {}) if lst[0] == OPTS[0] else (lst.index(OPTS[1]), 0.99, {})
    p = W.consistent_pick(one_none, "s", "Which?", OPTS, 0.9, rule=rule)
    assert p.index is None and "one order" in p.reason
    p = W.consistent_pick(chooser_by_text("Acme Kettle 2000", conf=0.5), "s", "Which?", OPTS, 0.9, rule=rule)
    assert p.index is None and "below t2" in p.reason


def test_prior_rule_is_unchanged():
    seen = []
    p = W.consistent_pick(chooser_by_text("Acme Kettle 3000", log=seen), "s", "Which?", OPTS, 0.9,
                          rule=W.load_picker_rule())
    assert seen[1][0] == W.NONE_OPTION and seen[1][1:] == OPTS[::-1] and p.index == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
