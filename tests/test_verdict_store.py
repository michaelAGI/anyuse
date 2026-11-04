"""The VERDICT STORE (``execution/verdict_store.py``), offline with fakes built from the drift census's
evidence (no browser, no network, no model).

What must hold:

1. an answer is normalised the census's way (titles folded, URLs without fragment / trailing slash, a product's
   name / price / currency / availability / URL; a failure has no answer);
2. a verdict is found by the tool's digest, the input and the answer's digest; else by the same site, input and answer
   from another tool (a recompile repeating a known wrong); never across sites or inputs; the latest verdict wins;
3. a run whose answer matches a known WRONG verdict becomes ``known_wrong``: never ok, not accepted, its answer kept
   as evidence only; a failure, an unknown answer or a "correct" verdict leave the run as it is;
4. the gate's ``StoreChecker``: a stored "correct" on the same answer agrees, a stored "wrong" disagrees, else the
   independent checker decides and its verdict is recorded ("correct" / "disputed"; a dispute never flags a run);
5. the store is a file: re-opened, it holds the same rows; a repeated row is stored once.
"""
import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import verdict_store as V  # noqa: E402

TOOL = {"version": 9, "workflow": "product_lookup", "site": "www.spirits.test", "search": {"mode": "url",
        "template": "https://www.spirits.test/suche?q={q}"}, "region": {"regex": "^https://www\\.spirits\\.test/p/"}}
OTHER = dict(TOOL, version=11)
VALUE = "Laphroaig 10 Jahre Islay Single Malt 0,7l"
# the census's spirituosenworld.de case, abstracted: the structured LimitedAvailability the audit judged wrong
WRONG = {"status": "ok", "output": {"name": "Laphroaig 10 Jahre Islay Single Malt 0,7l", "price": 39.9, "currency": "EUR",
                                    "availability": "LimitedAvailability", "url": "https://www.spirits.test/p/laphroaig-10/"}}
RIGHT = {"status": "ok", "output": dict(WRONG["output"], availability="dieser artikel steht derzeit nicht zur verfugung")}
LIST = {"status": "ok", "output": {"results": [{"title": "Objektiv-Test: Das Sigma 18-50", "url": "https://x.test/a/#top"},
                                               {"title": "  Objektive   im Vergleich ", "url": "https://X.test/b/"}]}}


def test_the_census_normalisation():
    assert V.answer_key("ok", WRONG["output"]) == ["product", "laphroaig 10 jahre islay single malt 0,7l", 39.9, "EUR",
                                                   "limitedavailability", "https://www.spirits.test/p/laphroaig-10"]
    k = V.answer_key("ok", LIST["output"])
    assert k[0] == "results" and k[1][1] == "https://x.test/a" and k[2][1] == "https://x.test/b"
    assert V.answer_key("no_results", None) == ["no_results"] and V.answer_key("ok", {"no_results": True}) == ["no_results"]
    for st in ("failed", "incomplete", "blocked", "error", V.KNOWN_WRONG):
        assert V.answer_key(st, WRONG["output"]) is None and V.answer_digest(st, WRONG["output"]) is None
    # the same answer with a trailing slash / another case of the host is the same answer
    same = dict(WRONG, output=dict(WRONG["output"], url="https://WWW.spirits.test/p/laphroaig-10#reviews"))
    assert V.answer_digest("ok", same["output"]) == V.answer_digest("ok", WRONG["output"])
    assert V.input_key("  Laphroaig  10 ") == V.input_key("laphroaig 10")


class Tmp:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="anyuse-verdicts-test-")
        return os.path.join(self.d, "verdicts.jsonl")

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


def test_lookup_scope_and_the_latest_verdict():
    with Tmp() as p:
        s = V.VerdictStore(p)
        d = C.tool_digest(TOOL)
        assert s.add(tool=d, site="www.spirits.test", value=VALUE, status="ok", output=WRONG["output"],
                     verdict="wrong", source="audit", ref="r6|delayed|v2", at="2026-10-04T17:48:06") is not None
        hit = s.lookup(d, "spirits.test", VALUE, "ok", WRONG["output"])
        assert hit["verdict"] == "wrong" and hit["scope"] == "tool"
        # another tool of the same site repeating the answer: matched by the site
        hit = s.lookup(C.tool_digest(OTHER), "www.spirits.test", VALUE.upper(), "ok", WRONG["output"])
        assert hit["verdict"] == "wrong" and hit["scope"] == "site"
        # never across sites, inputs or answers
        assert s.lookup(C.tool_digest(dict(TOOL, site="www.other.test")), "www.other.test", VALUE, "ok",
                        WRONG["output"]) is None
        assert s.lookup(d, "www.spirits.test", "Laphroaig 18", "ok", WRONG["output"]) is None
        assert s.lookup(d, "www.spirits.test", VALUE, "ok", RIGHT["output"]) is None
        assert s.lookup(d, "www.spirits.test", VALUE, "failed", None) is None
        # a later "correct" on the same answer supersedes the "wrong" (the site changed)
        s.add(tool=d, site="www.spirits.test", value=VALUE, status="ok", output=WRONG["output"], verdict="correct",
              source="reader", ref="census", at="2026-10-06T22:29:00")
        assert s.lookup(d, "www.spirits.test", VALUE, "ok", WRONG["output"])["verdict"] == "correct"
        assert s.known_wrong(TOOL, VALUE, WRONG) is None
        # a repeated row is stored once; the file re-opens with the same rows
        n = len(s.rows())
        assert s.add(tool=d, site="www.spirits.test", value=VALUE, status="ok", output=WRONG["output"],
                     verdict="correct", source="reader", ref="census", at="2026-10-06T22:29:00") is None
        assert len(V.VerdictStore(p).rows()) == n == 2
        assert V.VerdictStore(p).summary()["by_source"] == {"audit": 1, "reader": 1}


def test_a_known_wrong_answer_is_never_ok():
    with Tmp() as p:
        s = V.VerdictStore(p)
        s.add(tool=C.tool_digest(TOOL), site=TOOL["site"], value=VALUE, status="ok", output=WRONG["output"],
              verdict="wrong", source="audit", ref="r6|delayed|v2", why="LimitedAvailability for an unavailable product")
        out = dict(WRONG, why="identity and fields checked", accepted=True)
        got = V.check(s, TOOL, VALUE, out)
        assert got["status"] == V.KNOWN_WRONG and got["accepted"] is False and got["output"] is None
        assert got["unverified_output"] == WRONG["output"] and got["known_wrong"]["source"] == "audit"
        assert got["why"].startswith("known wrong:")
        # the same answer from a recompiled tool of the site: flagged too
        assert V.check(s, OTHER, VALUE, out)["status"] == V.KNOWN_WRONG
        # another answer, a failure, no store: unchanged
        for o in (dict(RIGHT, accepted=True), {"status": "failed", "why": "CONFLICT", "output": None}):
            assert V.check(s, TOOL, VALUE, o) == o
        assert V.check(None, TOOL, VALUE, out) == out
        # an "incomplete" verdict is stored but does not flag
        s.add(tool=C.tool_digest(TOOL), site=TOOL["site"], value=VALUE, status="ok", output=RIGHT["output"],
              verdict="incomplete", source="reader")
        assert V.check(s, TOOL, VALUE, dict(RIGHT)) == dict(RIGHT)


def test_the_store_checker():
    with Tmp() as p:
        s = V.VerdictStore(p)
        d = C.tool_digest(TOOL)
        s.add(tool=d, site=TOOL["site"], value=VALUE, status="ok", output=RIGHT["output"], verdict="correct",
              source="reader", ref="census")
        s.add(tool=d, site=TOOL["site"], value=VALUE, status="ok", output=WRONG["output"], verdict="wrong", source="audit")
        asked = []

        def fallback(tool, value, out):
            asked.append(out["output"]["price"])
            return {"verdict": "agrees" if out["output"]["price"] == 1.0 else "disagrees", "why": "render"}
        ck = V.StoreChecker(s, fallback)
        assert asyncio.run(ck(OTHER, VALUE, RIGHT))["verdict"] == "agrees" and not asked
        assert asyncio.run(ck(OTHER, VALUE, WRONG))["verdict"] == "disagrees" and not asked
        new = {"status": "ok", "output": dict(RIGHT["output"], price=1.0)}
        bad = {"status": "ok", "output": dict(RIGHT["output"], price=2.0)}
        assert asyncio.run(ck(OTHER, VALUE, new))["verdict"] == "agrees"
        assert asyncio.run(ck(OTHER, VALUE, bad))["verdict"] == "disagrees" and asked == [1.0, 2.0]
        # recorded: the agreement as "correct", the disagreement as "disputed" (never a known wrong)
        assert s.lookup(C.tool_digest(OTHER), TOOL["site"], VALUE, "ok", new["output"])["verdict"] == "correct"
        assert s.lookup(C.tool_digest(OTHER), TOOL["site"], VALUE, "ok", bad["output"])["verdict"] == "disputed"
        assert V.check(s, OTHER, VALUE, bad) == bad
        assert asyncio.run(V.StoreChecker(s)(OTHER, VALUE, {"status": "ok", "output": dict(RIGHT["output"], price=3.0)}))[
            "verdict"] == "undeterminable"


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"test_verdict_store: {n} ok")
