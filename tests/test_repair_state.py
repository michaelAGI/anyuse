"""The repair ladder's tool state (``execution/repair_ladder.py``), offline on the
fake shop of ``test_repair_ladder``: no browser, no network, no model.

What must hold:

1. PINNING: a pinned revision is never rewritten automatically - a drift it cannot read ends at ``needs_human`` with
   the evidence (no repair attempt, US$0), the pin stays active; after ``unpin`` the same drift is repaired;
2. the RELIABILITY STATE, from the run ledger, one test per transition: healthy (no run; clean runs) -> watch (an
   accepted repair) -> healthy (N clean runs later); healthy -> watch (a "changed, unverified" outcome); healthy ->
   needs_human (no tier found a repair) -> watch (the next clean run) -> healthy (N clean runs); stored next to the
   revisions (``state.json``);
3. the CAP on automatic rewrites: at most 2 accepted repairs in a rolling 7 days, then ``needs_human``; 8 days later a
   repair is accepted again;
4. the PER-RUN READOUT: every run carries the tier reached, the model calls, the US$, the revision used and one line;
   an unverified repair is never reported as a success (the run keeps its tier-0 failure).
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import RENAMED, Shop, Store, compiled, ladder_run  # noqa: E402

V = "Bosch GSR 12V Akkuschrauber"
RENAMED2 = {"product": "card", "info": "card-body", "name": "card-title", "title": "card-link", "products": "cards",
            "listing": "result-list", "price": "card-price"}
RENAMED3 = {"product": "hit", "info": "hit-body", "name": "hit-name", "title": "hit-link", "products": "hits",
            "listing": "hit-list", "price": "hit-price"}


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)


def cfg(**kw):
    return L.LadderConfig(enabled=True, watch_runs=3, **kw)


def test_pinning():
    tool = compiled(V)
    st = Store(tool)
    try:
        assert ladder_run(Shop(), tool, V, st.s, config=cfg())["status"] == "ok"
        st.s.pin()
        assert st.s.pinned() == 0
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())
        lad = out["ladder"]
        assert lad["outcome"] == "needs_human" and lad["tier"] == 4 and out["status"] == "failed", lad
        assert "pinned" in lad["evidence"]["why"] and lad["evidence"]["attempts"] == [] and lad["usd"] == 0.0
        assert lad["evidence"]["tier0"]["why"].startswith("membership") and lad["state"] == "needs_human"
        assert st.s.active_rev() == 0 and len(st.s.revisions()) == 1
        try:
            st.s.rollback(to=5)
            raise AssertionError("a rollback away from the pin")
        except ValueError:
            pass
        st.s.unpin()
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())
        assert out["ladder"]["outcome"] == "repaired" and st.s.active_rev() == 1
    finally:
        st.close()


def test_reliability_state_transitions():
    tool = compiled(V)
    st = Store(tool)
    try:
        assert st.s.state(3)["state"] == "healthy"                                   # no run yet
        assert ladder_run(Shop(), tool, V, st.s, config=cfg())["ladder"]["state"] == "healthy"
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())             # healthy -> watch (a repair)
        assert out["ladder"]["outcome"] == "repaired" and out["ladder"]["state"] == "watch"
        states = [ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())["ladder"]["state"] for _ in range(3)]
        assert states == ["watch", "watch", "healthy"], states                       # watch -> healthy (N clean runs)
        saved = json.load(open(os.path.join(st.s.dir, "state.json")))
        assert saved["state"] == "healthy" and os.path.exists(os.path.join(st.s.dir, "revisions.json"))
        out = ladder_run(Shop(ppath="/produkt/"), tool, V, st.s, config=cfg())       # healthy -> needs_human
        assert out["ladder"]["outcome"] == "needs_human" and out["ladder"]["state"] == "needs_human", out["ladder"]
        assert out["ladder"]["evidence"]["why"].startswith("no tier found")
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())             # needs_human -> watch
        assert out["ladder"]["outcome"] == "ok" and out["ladder"]["state"] == "watch"
        for _ in range(2):
            out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())
        assert out["ladder"]["state"] == "healthy"                                    # watch -> healthy
    finally:
        st.close()
    st = Store(tool)
    try:
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())             # healthy -> watch (unverified)
        assert out["ladder"]["outcome"] == "changed_unverified" and out["ladder"]["state"] == "watch"
    finally:
        st.close()


def test_the_cap_on_automatic_rewrites():
    tool = compiled(V)
    clock = Clock()
    st = Store(tool, clock=clock)
    try:
        assert ladder_run(Shop(), tool, V, st.s, config=cfg())["status"] == "ok"
        for cls in (RENAMED, RENAMED2):
            clock.advance(days=1)
            assert ladder_run(Shop(cls=cls), tool, V, st.s, config=cfg())["ladder"]["outcome"] == "repaired"
        clock.advance(days=1)
        out = ladder_run(Shop(cls=RENAMED3), tool, V, st.s, config=cfg())
        lad = out["ladder"]
        assert lad["outcome"] == "needs_human" and "cap on automatic rewrites" in lad["evidence"]["why"], lad
        assert st.s.active_rev() == 2 and lad["state"] == "needs_human"
        clock.advance(days=7)
        out = ladder_run(Shop(cls=RENAMED3), tool, V, st.s, config=cfg())
        assert out["ladder"]["outcome"] == "repaired" and st.s.active_rev() == 3
    finally:
        st.close()


def test_the_per_run_readout():
    tool = compiled(V)
    st = Store(tool)
    try:
        lines = []
        for site in (Shop(), Shop(cls=RENAMED), Shop(cls=RENAMED), Shop(ppath="/produkt/")):
            out = ladder_run(site, tool, V, st.s, config=cfg())
            lad = out["ladder"]
            for k in ("tier", "model_calls", "usd", "revision", "line", "outcome", "state"):
                assert k in lad, k
            lines.append(lad["line"])
        assert lines[0] == "tier 0: ok; status ok; 0 model call(s), US$0.0000; rev 0"
        assert lines[1].startswith("tier 1: REPAIRED; status ok;") and lines[1].endswith("rev 1 (was 0)")
        assert lines[2] == "tier 0: ok; status ok; 0 model call(s), US$0.0000; rev 1"
        assert lines[3].startswith("tier 4: NEEDS HUMAN; status failed;")
        rows = st.s.ledger()
        assert [r["outcome"] for r in rows] == ["ok", "repaired", "ok", "needs_human"] and all("line" in r for r in rows)
    finally:
        st.close()
    st = Store(tool)
    try:
        out = ladder_run(Shop(cls=RENAMED), tool, V, st.s, config=cfg())
        lad = out["ladder"]
        assert out["status"] == "failed" and out.get("accepted") is False and lad["outcome"] == "changed_unverified"
        assert "REPAIRED" not in lad["line"] and "not an answer" in lad["line"] and "rev 0" in lad["line"]
    finally:
        st.close()


if __name__ == "__main__":
    test_pinning()
    test_reliability_state_transitions()
    test_the_cap_on_automatic_rewrites()
    test_the_per_run_readout()
    print("test_repair_state: 4 ok")
