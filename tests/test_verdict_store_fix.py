"""The VERDICT STORE's two fixes from the draft seam's review (``execution/verdict_store.py``), offline with fakes (no
browser, no network, no model).

What must hold:

1. a verdict marked wrong, then correct, then wrong again (same tool, input, answer, source and reference) is three
   rows, and the LATEST one counts: the answer is known wrong again (before the fix the third row was dropped as a
   repeat of the first, so the answer passed as correct);
2. equal times and equal sources: the row written last wins;
3. inputs are stored as their digest by default (a watcher's or a desktop routine's values can be personal data):
   the file never holds the input's text, and lookups still match; ``plain_inputs=True`` keeps the folded text;
4. rows written before the fix (plain text inputs) are still matched, and one input in both forms is one key.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import verdict_store as V  # noqa: E402

TOOL = {"version": 12, "workflow": "product_lookup", "site": "www.spirits.test", "search": {"mode": "url",
        "template": "https://www.spirits.test/suche?q={q}"}, "region": {"regex": "^https://www\\.spirits\\.test/p/"}}
VALUE = "Laphroaig 10 Jahre Islay Single Malt 0,7l"
PRIVATE = "Erika Mustermann, Hauptstr. 5"
OUT = {"status": "ok", "output": {"name": "Laphroaig 10 Jahre", "price": 39.9, "currency": "EUR",
                                  "availability": "InStock", "url": "https://www.spirits.test/p/laphroaig-10/"}}


class Tmp:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="anyuse-verdicts-fix-test-")
        return os.path.join(self.d, "verdicts.jsonl")

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


def _add(s, verdict, at, value=VALUE, ref="watch"):
    return s.add(tool=C.tool_digest(TOOL), site=TOOL["site"], value=value, status=OUT["status"],
                 output=OUT["output"], verdict=verdict, source="checker", ref=ref, at=at)


def test_wrong_correct_wrong_keeps_the_last():
    with Tmp() as p:
        s = V.VerdictStore(p)
        assert _add(s, "wrong", "2026-10-08T10:00:00Z") is not None
        assert _add(s, "correct", "2026-10-08T11:00:00Z") is not None
        assert _add(s, "wrong", "2026-10-08T12:00:00Z") is not None, "the re-marked wrong was dropped"
        assert s.known_wrong(TOOL, VALUE, OUT)["at"] == "2026-10-08T12:00:00Z"
        # a true repeat of the latest verdict is still stored once; the file re-opens the same way
        assert _add(s, "wrong", "2026-10-08T12:30:00Z") is None
        again = V.VerdictStore(p)
        assert len(again.rows()) == 3 and again.known_wrong(TOOL, VALUE, OUT) is not None
        assert _add(again, "wrong", "2026-10-08T13:00:00Z") is None


def test_equal_times_the_last_row_wins():
    with Tmp() as p:
        s = V.VerdictStore(p)
        t = "2026-10-08T10:00:00Z"
        for v in ("wrong", "correct", "wrong"):
            assert _add(s, v, t) is not None
        assert s.known_wrong(TOOL, VALUE, OUT) is not None
        assert _add(s, "correct", t) is not None
        assert s.known_wrong(TOOL, VALUE, OUT) is None


def test_inputs_are_stored_as_digests():
    with Tmp() as p:
        s = V.VerdictStore(p)
        assert _add(s, "wrong", "2026-10-08T10:00:00Z", value=PRIVATE) is not None
        text = open(p, encoding="utf-8").read()
        assert "mustermann" not in text.lower() and "hauptstr" not in text.lower()
        row = json.loads(text.splitlines()[0])
        assert row["input"] == V.input_ref(PRIVATE) and row["input"].startswith("sha256:")
        assert V.input_ref(row["input"]) == row["input"]
        assert V.input_ref("  ERIKA mustermann,  Hauptstr. 5 ") == row["input"]
        assert s.known_wrong(TOOL, PRIVATE, OUT) is not None
        assert s.known_wrong(TOOL, "Max Mustermann", OUT) is None
    with Tmp() as p:
        s = V.VerdictStore(p, plain_inputs=True)
        _add(s, "wrong", "2026-10-08T10:00:00Z")
        assert json.loads(open(p, encoding="utf-8").readline())["input"] == V.input_key(VALUE)
        assert s.known_wrong(TOOL, VALUE, OUT) is not None


def test_rows_written_before_the_fix_still_match():
    with Tmp() as p:
        old = {"tool": C.tool_digest(TOOL), "site": "spirits.test", "input": V.input_key(VALUE),
               "answer": V.answer_digest("ok", OUT["output"]), "verdict": "wrong", "source": "audit",
               "at": "2026-10-04T10:00:00Z", "ref": "eval8", "why": "", "workflow": "product_lookup"}
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(old, sort_keys=True) + "\n")
        s = V.VerdictStore(p)
        assert s.known_wrong(TOOL, VALUE, OUT)["ref"] == "eval8"
        # the same row again, now as a digest: one key in both forms, a repeat - stored once
        assert s.add_row(dict(old)) is None
        assert s.add_row(dict(old, verdict="correct", at="2026-10-05T10:00:00Z")) is not None
        assert s.known_wrong(TOOL, VALUE, OUT) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
