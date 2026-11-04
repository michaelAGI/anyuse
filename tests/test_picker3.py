"""Fewer picker hand-offs without more confident-wrong picks, offline (fake
choosers).

Before: 26 picker questions, 9 accepted, 17 hand-offs (none in one or both
orders 16, the orders disagreed 1); the options were a link text and a path.

What must hold:

1. richer options within OpenJev's 120 characters: the title, the item's price
   and a snippet of its card when the title lacks them, the link path;
2. the rule recalibrated on SEEN data only (``web_arch3_picker.json``,
   frozen): confident-wrong <= 3 % there; one order
   naming X confidently while the other's "none" is unsure accepts X; the
   pairwise tie-break (the two items + none, both orders) decides only when
   both of its orders name the same item;
3. without a rule the previous behaviour is unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, run  # noqa: E402

RULE = W.load_picker_rule()


def test_the_frozen_rule_was_calibrated_on_seen_data_within_the_cw_cap():
    assert RULE["t2"] == 0.0 and RULE["tie_break"] == "pairwise"
    assert 0.5 <= RULE["asym_item"] <= 1.0 and 0.0 <= RULE["asym_none"] <= 1.0
    at = RULE["at_rule"]
    assert at["cw"] <= RULE["cw_max"] == 0.03 and at["n"] >= 200
    assert at["accepted"] >= RULE["prev_rule"]["accepted"] and at["wrong"] <= RULE["prev_rule"]["wrong"] + 1
    web_wrong = sum(v[1] for k, v in at["by_src"].items() if k.startswith("web"))
    assert web_wrong <= 1


def test_rich_options_fit_and_carry_price_snippet_and_path():
    el = {"text": "Barstoel Ruby", "href": BASE + "/products/barstoel-ruby-beige",
          "ctx": "by fonQ Barstoel Ruby Beige met armleuning € 129,99 Gratis bezorgd"}
    o = W.rich_option(el)
    assert len(o) <= W.OPTION_CHARS and o.startswith("Barstoel Ruby · € 129,99") and "/products/barstoel" in o
    assert "armleuning" in o
    el2 = {"text": "STONE ISLAND Pullover 370 €", "href": BASE + "/p/1", "ctx": "STONE ISLAND Pullover 370 € Neu"}
    assert W.rich_option(el2).count("370") == 1
    long = {"text": "x" * 200, "href": BASE + "/" + "y" * 200, "ctx": "z " * 200}
    assert len(W.rich_option(long)) <= W.OPTION_CHARS


class Chooser:
    """Scripted answers: a list of (choice, confidence) in call order."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, state, q, options):
        self.calls.append(list(options))
        c, conf = self.answers.pop(0)
        return c, conf, {}


def test_asymmetric_none_is_accepted_only_when_calibrated():
    opts = ["A", "B", "C"]
    # o0 = [A, B, C, none]: says none (index 3) at 0.5; o1 = [none, C, B, A]: names A (index 3) at 0.95
    p = W.consistent_pick(Chooser([(3, 0.5), (3, 0.95)]), "s", "q", opts, 0.0, rule=RULE)
    assert p.index == 0 and "one order names it" in p.reason
    p = W.consistent_pick(Chooser([(3, 0.95), (3, 0.95)]), "s", "q", opts, 0.0, rule=RULE)
    assert p.index is None and "none of these in one order" in p.reason
    p = W.consistent_pick(Chooser([(3, 0.5), (3, 0.95)]), "s", "q", opts, 0.0)
    assert p.index is None                          # without a rule: a hand-off


def test_the_pairwise_tie_break_acts_only_when_consistent():
    opts = ["A", "B", "C"]
    # o0 names A (0), o1 = [none, C, B, A] names B (2 -> index 1); tie-break [A, B, none] then [none, B, A]
    ch = Chooser([(0, 0.9), (2, 0.9), (1, 0.8), (1, 0.8)])
    p = W.consistent_pick(ch, "s", "q", opts, 0.0, rule=RULE)
    assert p.index == 1 and p.calls == 4 and "tie-break" in p.reason
    assert ch.calls[2] == ["A", "B", W.NONE_OPTION] and ch.calls[3] == [W.NONE_OPTION, "B", "A"]
    ch = Chooser([(0, 0.9), (2, 0.9), (0, 0.8), (1, 0.8)])       # A, then B: not consistent
    p = W.consistent_pick(ch, "s", "q", opts, 0.0, rule=RULE)
    assert p.index is None and "not consistent" in p.reason
    ch = Chooser([(0, 0.9), (2, 0.9), (2, 0.8), (0, 0.8)])       # none both times
    assert W.consistent_pick(ch, "s", "q", opts, 0.0, rule=RULE).index is None
    p = W.consistent_pick(Chooser([(0, 0.9), (2, 0.9)]), "s", "q", opts, 0.0)
    assert p.index is None and "orders disagree" in p.reason and p.calls == 2


def test_the_engine_uses_the_rule_and_the_rich_options():
    seen = []

    def chooser(state, q, options, images=()):
        seen.append(list(options))
        k = len(options) - 1
        want = next(i for i, o in enumerate(options) if o.startswith("Merino Hiking Sock"))
        return want, 0.99, {}
    page = FakePage()
    run(R.WebArch(FakePlanner([S("search", value="merino"), S("open_item", "the warm one")]), chooser=chooser,
                  shadow_images=False, picker_rule=RULE).run(
        page, {"id": "T", "utterance": "Open the warm merino sock", "kind": "open_item"}, BASE + "/"))
    assert seen and all(len(o) <= W.OPTION_CHARS for o in seen[0])
    assert page.gotos[-1].endswith("/products/merino-hiking-sock"), page.gotos


if __name__ == "__main__":
    import inspect
    import traceback
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn()
            p += 1
            print("OK  ", n)
        except Exception as e:                            # noqa: BLE001
            f += 1
            print("FAIL", n, "::", repr(e)[:400])
            traceback.print_exc(limit=6)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
