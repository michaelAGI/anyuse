"""web-heal-live, fix 2 (ladder-only, OFF by default): PROBATION for an accepted revision's untested answers and the
checker's availability check (``repair_ladder.LadderConfig(probation=True)``, ``repair_check.availability_contradiction``
/ ``judge(availability=True)``), offline on fakes built from the round's renders (no browser, no network, no model).

What must hold:

1. the availability check reads only the statement attached to the product (its price / add-to-cart lines): the round's
   www.kamdi24.de render (OutOfStock next to "Lieferzeit: 1 bis 3 Werktage") and www.spirituosenworld.de's
   (LimitedAvailability next to "Ausverkauft") are contradicted; "Nur noch 6 auf Lager!" next to InStock (a few left is in
   stock), a visible statement as the answer, a page that states nothing attached are not; the judge's default is
   unchanged (it never reads the availability);
2. probation: on revision 1 (an accepted upgrade) a tier-0 answer for an input without a last known good answer is
   withheld ("changed, unverified") when the checker does not agree, reported (and kept as the last known good one) when
   it agrees; an answer equal to the last known good one needs no checker; revision 0 and probation off: exactly as
   before.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import repair_check as RC  # noqa: E402
from anyuse.execution import repair_ladder as L  # noqa: E402
from test_repair_ladder import Shop, Store, compiled, ladder_run  # noqa: E402

KAMDI = "\n".join(["KAMDI24 GESCHENKGUTSCHEIN FEUER 25 €", "Wert:", "25", "25,00 €", "inkl. MwSt., zzgl. Versandkosten*",
                   "Preisgarantie", "IN DEN WARENKORB", "", "Lieferzeit: 1 bis 3 Werktage", "(Paket)", "BESCHREIBUNG"])
SPIRIT = "\n".join(["Glenfiddich 12 Jahre", "39,90 €", "Ausverkauft", "Benachrichtigen, wenn verfügbar"])
MASSIVUM = "\n".join(["Sideboard Eiche", "134,10 €", "Nur noch 6 auf Lager!", "In den Warenkorb"])
V = "Bosch GSR 12V Akkuschrauber"


def test_the_availability_attached_to_the_product():
    assert "delivery time" in RC.availability_contradiction(KAMDI, "OutOfStock", 25.0)
    assert "out of stock" in RC.availability_contradiction(SPIRIT, "LimitedAvailability", 39.9)
    assert RC.availability_contradiction(MASSIVUM, "InStock", 134.1) == ""          # a few left is in stock
    assert RC.availability_contradiction(KAMDI, "Lieferzeit: 1 bis 3 Werktage", 25.0) == ""   # a statement answer
    assert RC.availability_contradiction("Bosch\n89,90 €\nBeschreibung", "OutOfStock", 89.9) == ""   # nothing attached
    assert RC.AVAILABILITY_CHECK_BY_DEFAULT is False


class Checker:
    """The independent checker on a recorded render (``repair_check.judge``) whose item page states ``text``."""

    def __init__(self, text, availability=True):
        self.text, self.availability, self.asked = text, availability, 0

    def __call__(self, tool, value, out):
        self.asked += 1
        o = out.get("output") or {}
        ev = {"how": "typed", "links": [{"url": o.get("url"), "text": o.get("name")}]}
        item = {"url": o.get("url"), "title": o.get("name"), "h1": [o.get("name")], "text": self.text}
        return RC.judge(tool, value, out, ev, {o.get("url"): item}, availability=self.availability)


def _upgraded(tool):
    st = Store(tool)
    st.s.add_revision(tool, 0, {"upgrade": {"from_version": 9, "to_version": 10}}, {}, 0.0, 0, kind="upgrade")
    assert st.s.active_rev() == 1
    return st


def test_probation_withholds_an_unverified_answer_of_an_accepted_revision():
    tool = compiled(V)
    shown = f"{V}\n89,90 €\nDerzeit nicht verfügbar\nIn den Warenkorb"        # the page says out of stock
    st = _upgraded(tool)
    try:
        chk = Checker(shown)
        out = ladder_run(Shop(), tool, V, st.s, config=L.LadderConfig(enabled=True, probation=True), checker=chk)
        lad = out["ladder"]
        assert out["status"] == "incomplete" and out["output"] is None and out["unverified_output"]["name"] == V, out
        assert lad["outcome"] == L.UNVERIFIED and lad["tier"] == 0 and "probation" in lad["why"] and chk.asked == 1
        assert st.s.lkg(V) is None
        # the checker agrees: reported, kept as the last known good answer; the next run needs no checker
        ok = Checker(f"{V}\n89,90 €\nAuf Lager\nIn den Warenkorb")
        out = ladder_run(Shop(), tool, V, st.s, config=L.LadderConfig(enabled=True, probation=True), checker=ok)
        assert out["status"] == "ok" and out["ladder"]["outcome"] == L.OK and st.s.lkg(V) and ok.asked == 1
        out = ladder_run(Shop(), tool, V, st.s, config=L.LadderConfig(enabled=True, probation=True), checker=ok)
        assert out["status"] == "ok" and ok.asked == 1
    finally:
        st.close()


def test_probation_off_and_revision_0_are_unchanged():
    tool = compiled(V)
    shown = f"{V}\n89,90 €\nDerzeit nicht verfügbar\nIn den Warenkorb"
    for cfg, upgraded in ((L.LadderConfig(enabled=True), True), (L.LadderConfig(enabled=True, probation=True), False)):
        st = _upgraded(tool) if upgraded else Store(tool)
        try:
            chk = Checker(shown)
            out = ladder_run(Shop(), tool, V, st.s, config=cfg, checker=chk)
            assert out["status"] == "ok" and out["ladder"]["outcome"] == L.OK and chk.asked == 0
        finally:
            st.close()
    assert L.LadderConfig().probation is False


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
