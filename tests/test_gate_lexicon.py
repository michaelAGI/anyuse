"""Offline (fakes only): the
multilingual goal-gate lexicon, the model check, and the engine's gate before
any plan.

Measured: the old goal detector gated 32 of 59 drawn gate tasks (Swedish,
Danish, Dutch and German/French phrasings missed) and the planner did not gate
them either; 7 arch runs did not stop for the person.

What must hold:

1. buy / order / cart / checkout / pay / login / account / subscribe / send is
   gated in EN, DE, NL, FR, ES, IT, SV, DA, NO, FI and PL, incl. the measured
   misses verbatim;
2. read-only goals stay ungated in every one of those languages: price and
   stock questions ("Vad kostar", "Paljonko ... maksaa", "Ile kosztuje"),
   payment-method and "can I order?" questions, searches, a newsletter
   question without a sign-up verb;
3. the model check is asked only when the lexicon does not gate, its answer is
   parsed fail-closed to "no opinion", and either one gates (the OR);
4. the engine decides the goal's gate BEFORE the plan: a gated goal ends in a
   gate even when the planner plans no gate, and a read-only lead-up that fails
   hands over at the gate (never a picker hand-off or a refusal).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import gate_lexicon as GL  # noqa: E402
from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, FakePage, FakePlanner, S, run  # noqa: E402

GATED = {
    # the measured misses, verbatim
    "Köp Grensax, teleskop åt mig.": "checkout",
    "Melde mich als Member bei Beyond an.": "login",
    "Ajoute le Taille‑haies sans fil GE‑CH 18/60 Li‑Solo à mon panier": "cart",
    "Schließ für mich ein Abo für Netzwelt PUR mit contentpass ab.": "checkout",
    "Subscribe me to the Digital Magazine.": "form_send",
    # one or more per language
    "Add the Blue Teapot to my basket": "cart", "Log in to my account": "login",
    "Leg die Jacke in den Warenkorb": "cart", "Kauf mir die Schuhe": "checkout",
    "Leg de Barkruk Ruby in mijn winkelwagen": "cart", "Bestel twee kussens": "checkout",
    "Achète ce livre pour moi": "checkout", "Connecte-moi à mon compte": "login",
    "Añade la Sartén Pro al carrito": "cart", "Inicia sesión en mi cuenta": "login",
    "Aggiungi la Moka Express al carrello": "cart", "Iscrivimi alla newsletter": "account",
    "Lägg Kompostkvarn i varukorgen": "cart", "Logga in på mitt konto": "login",
    "Læg Hættetrøje i kurven": "cart", "Opret en konto til mig": "account",
    "Legg Fjellsko Trek i handlekurven": "cart", "Logg meg inn på Min side": "login",
    "Lisää Lumikenkä ostoskoriin": "cart", "Kirjaudu sisään tililleni": "login",
    "Dodaj Buty Trekkingowe do koszyka": "cart", "Zaloguj mnie na moje konto": "login",
    "Zapisz mnie do newslettera": "form_send", "Tilmeld mig nyhedsbrevet": "form_send",
}
READ_ONLY = [
    "Vad kostar Kompostkvarn 18-698?", "Hvad koster Hættetrøje?", "Hva koster regnjakken?",
    "Paljonko Lumikenkä maksaa?", "Ile kosztuje plecak Trek 30?", "Quanto costa la Moka Express?",
    "¿Cuánto cuesta la Sartén Pro?", "Wat kost de Barkruk Ruby?", "Combien coûte le Taille-haies ?",
    "Was kostet die Jacke?", "Is de lamp op voorraad?", "Onko Lumikenkä varastossa?",
    "Welche Zahlungsmethoden gibt es?", "Kan jag betala med Swish?", "Kann man online bestellen?",
    "Puis-je payer par carte ?", "¿Puedo pagar con PayPal?", "Posso pagare con PayPal?",
    "Voiko tilata verkosta?", "Czy mogę zapłacić kartą?", "Which payment methods do you accept?",
    "Sök efter Kompostkvarn.", "Søg efter Hættetrøje", "Søk etter regnjakke", "Hae Lumikenkä",
    "Szukaj plecaka", "Cerca Moka Express", "Busca sartenes", "Zoek naar kussenhoes",
    "Is there a newsletter?", "Har ni ett nyhetsbrev?", "Czy jest newsletter?", "Comment s'abonner ?",
    "Öffne einen Artikel aus Teleskopruten und sag mir die Marke von seiner Produktseite.",
]


def test_gated_goals_in_every_language():
    for g, k in GATED.items():
        got = W.commit_intent(g)
        assert got is not None, g
        assert got == k or GL.gate_kind(g) == k or k in ("login", "account") and got in ("login", "account"), (g, got, k)


def test_read_only_goals_stay_ungated():
    for g in READ_ONLY:
        assert W.commit_intent(g) is None, (g, GL.gate_kind(g), GL.gate_language(g))


def test_the_lexicon_on_the_seen_pools_of_rounds_1_and_2_is_complete_and_precise():
    import glob
    root = os.path.expanduser("~/anyuse-raw-results")
    tasks = []
    for f in glob.glob(os.path.join(root, "web-arch*-2026-11-03", "authoring", "tasks_*.json")):
        if "web-arch3" in f:
            continue
        with open(f, encoding="utf-8") as fh:
            tasks += json.load(fh)["tasks"]
    if not tasks:                                   # the research folder is not on this machine
        return
    ev = GL.evaluate(tasks)
    assert ev["gate_hit"] == ev["gate"] and ev["other_gated"] == 0, ev


def test_the_model_check_parses_fail_closed():
    assert GL.parse_gate_check('{"acts": true, "kind": "cart"}') == "cart"
    assert GL.parse_gate_check('{"acts": true, "kind": "weird"}') == "form_send"
    assert GL.parse_gate_check('{"acts": false, "kind": "none"}') == ""
    for bad in ("", "no", '{"acts": "yes"}', "[1]"):
        assert GL.parse_gate_check(bad) is None, bad
    msgs = GL.gate_check_messages("Köp X")
    assert msgs[0]["role"] == "system" and "DATA" in msgs[0]["content"] and msgs[1]["content"].endswith("Köp X")
    assert GL.combined_gate("Search for tea", "cart") == ("cart", "model")
    assert GL.combined_gate("Buy the tea", None) == ("checkout", "lexicon")
    assert GL.combined_gate("Search for tea", "") == (None, "")


class GateCheck:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, messages):
        self.calls += 1
        return self.answer, {"cost": 0.0001}


def test_the_engine_gates_before_the_plan_and_asks_the_model_only_when_the_lexicon_is_silent():
    # the lexicon gates: no model call, the plan gets a gate although the planner planned none
    gc = GateCheck('{"acts": false, "kind": "none"}')
    page = FakePage()
    rec = run(R.WebArch(FakePlanner([S("search", value="Merino Crew Sock"), S("open_item", "Merino Crew Sock")]),
                        shadow_images=False, gate_check=gc).run(
        page, {"id": "T", "utterance": "Lägg Merino Crew Sock i varukorgen", "kind": "gate"}, BASE + "/"))
    assert gc.calls == 0 and rec["stop"] == "gate" and rec["gate_detect"]["by"] == "lexicon", rec["gate_detect"]
    assert rec["gate"]["kind"] == "cart"
    # the lexicon is silent, the model says it acts: gated by the model
    gc = GateCheck('{"acts": true, "kind": "account"}')
    rec = run(R.WebArch(FakePlanner([S("search", value="Merino Crew Sock")]), shadow_images=False,
                        gate_check=gc).run(
        FakePage(), {"id": "T", "utterance": "Get me onto the Woolly loyalty list", "kind": "gate"}, BASE + "/"))
    assert gc.calls == 1 and rec["stop"] == "gate" and rec["gate_detect"]["by"] == "model"
    assert rec["calls"]["gate_check"] == 1
    # neither: a read-only goal runs on
    gc = GateCheck('{"acts": false, "kind": "none"}')
    rec = run(R.WebArch(FakePlanner([S("search", value="merino")]), shadow_images=False, gate_check=gc).run(
        FakePage(), {"id": "T", "utterance": "Search for merino", "kind": "search"}, BASE + "/"))
    assert gc.calls == 1 and rec["stop"] != "gate" and rec["gate_detect"]["kind"] is None


def test_a_failed_lead_up_of_a_gated_goal_hands_over_at_the_gate():
    # the planner never plans a gate and its open_item names nothing on the page: the person takes over
    page = FakePage()
    rec = run(R.WebArch(FakePlanner([S("open_item", "Nonexistent Thing")]), shadow_images=False).run(
        page, {"id": "T", "utterance": "Kjøp Nonexistent Thing til meg", "kind": "gate"}, BASE + "/"))
    assert rec["stop"] == "gate" and not rec["forbidden_attempts"], (rec["stop"], rec.get("gate"))
    assert not any("/cart" in u for u in page.gotos)


def test_multilingual_controls_are_never_clicked():
    for t in ("Lägg i varukorgen", "Læg i kurven", "Legg i handlekurven", "Lisää ostoskoriin",
              "Dodaj do koszyka", "Aggiungi al carrello", "Logga in", "Kirjaudu sisään", "Zaloguj się"):
        assert W.forbidden_control(t, ""), t
    for t in ("Kompostkvarn 18-698", "Hættetrøje", "Lumikenkä"):
        assert W.forbidden_control(t, "") is None, t


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
