"""Tests for the shareable app-map projection (guides/projection.py) and the
measured chrome-vs-content classifier behind it (guides/locator_census.py).

The four core cases, plus the census unit tests and the fingerprint rule:

  - a guide containing a data-valued OCR anchor projects WITHOUT it
  - a guide of pure chrome projects intact
  - screenshot bytes never appear in the projection under ANY input
  - an unsure locator is excluded
  - a fingerprint that embeds an on-screen value drops the WHOLE node, loudly
  - the census measures: variance -> content, recurrence -> chrome, thin -> unsure

Run (anyuse's own venv has no pytest; the backend venv does):
    python -m pytest anyuse/test_guide_projection.py -q
or standalone:
    python anyuse/test_guide_projection.py
"""

import json
import pathlib
import sys
import tempfile

# anyuse uses mixed flat + package imports -> both the package dir and its parent
# must be importable.
_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

import pytest  # noqa: E402

from anyuse.guides.models import ActivityRecord, AppGuide, GuideType  # noqa: E402
from anyuse.guides.state_graph import (  # noqa: E402
    StateGraph, TransitionImpl, UIElement, UIFingerprint, UIState, UITransition,
)
from anyuse.guides.locator_census import (  # noqa: E402
    CHROME, CONTENT, UNSURE, KIND_AX_TITLE, LocatorCensus,
    element_slots, ocr_slot,
)
from anyuse.guides import projection as P  # noqa: E402
from execution.visual_verify import OCRAnchor  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CUSTOMER = "Mueller, Anna"


# ── fixtures ────────────────────────────────────────────────────────────────
def _el(role, title, identifier=None, el_type="button", actions=("press",)):
    return UIElement(role=role, title=title, identifier=identifier,
                     element_type=el_type, actions=list(actions),
                     accepts_input=role == "AXTextField")


def _state(state_id, app, elements, struct_hash, required=None):
    """A UIState. ``required`` defaults to the elements' (role, title) pairs —
    which is exactly what the real producers write into the fingerprint."""
    if required is None:
        required = [{"role": e.role, **({"title": e.title} if e.title else {})}
                    for e in elements]
    fp = UIFingerprint(required_elements=required,
                       element_count_range=(len(elements), len(elements) + 20),
                       structural_hash=struct_hash,
                       role_frequency={e.role: sum(1 for x in elements
                                                   if x.role == e.role)
                                       for e in elements})
    return UIState(state_id=state_id, app=app, fingerprint=fp,
                   elements=list(elements))


def _graph(states, transitions=()):
    g = StateGraph()
    for s in states:
        g.add_state(s)
    for t in transitions:
        g.add_transition(t)
    return g


def _transition(a, b, intent, modality="ax", used=4, ok=4):
    return UITransition(
        from_state=a, to_state=b, intent=intent,
        implementations=[TransitionImpl(
            modality=modality,
            command={"element_path": "AXWindow/AXButton[Speichern]"},
            times_used=used, times_succeeded=ok)],
        times_executed=used, times_succeeded=ok)


def _guide(graph, name="SAP Logon", identity_provenance=P.IDENTITY_OS_APP_NAME):
    """A desktop-app guide. ``identity_provenance`` is what the generator would
    have recorded when the key arrived on an ``app=`` argument; pass None to get
    a guide from before that fact was recorded at all."""
    g = AppGuide(name=name, guide_type=GuideType.APPLICATION,
                 short_description="local prose", full_guide="# local markdown",
                 bundle_id="com.sap.gui", app_aliases=["SAP GUI"],
                 identity_provenance=identity_provenance)
    g.set_state_graph(graph)
    return g


def _booking_screen(customer, clean_fingerprint=False):
    """The screen under test: a Save/Book toolbar (chrome) over a customer cell
    (content). Only the cell's text changes between records.

    ``clean_fingerprint`` writes only the chrome titles into the fingerprint's
    required_elements — the case where the node's IDENTITY is safe and only its
    anchors carry data. The default is what the real producers write: every
    actionable title, customer cell included.
    """
    elements = [_el("AXButton", "Speichern"), _el("AXButton", "Buchen"),
                _el("AXTextField", "Betrag", el_type="text_field",
                    actions=("set_value",)),
                _el("AXCell", customer, el_type="cell", actions=())]
    required = [{"role": "AXButton", "title": "Speichern"},
                {"role": "AXButton", "title": "Buchen"}] if clean_fingerprint else None
    return _state("sap_h1", "SAP Logon", elements, "h1booking", required=required)


def _confirm_screen():
    return _state("sap_h2", "SAP Logon",
                  [_el("AXButton", "Schliessen")], "h2confirm")


def _fed_census(customers=("Mueller, Anna", "Schmidt, Peter", "Weber, Karl"),
                anchors_for=None, clean_fingerprint=False):
    """Feed one session per customer record. Chrome stays put; the cell varies."""
    census = LocatorCensus()
    for i, customer in enumerate(customers):
        graph = _graph([_booking_screen(customer, clean_fingerprint),
                        _confirm_screen()],
                       [_transition("sap_h1", "sap_h2", "buchung abschliessen")])
        anchors = {"sap_h1": anchors_for(customer)} if anchors_for else None
        census.observe_state_graph(graph, session_id=f"s{i}", ocr_anchors=anchors)
    return census


def _anchors(customer):
    """Two OCR anchors on the same screen: a toolbar label and a data value."""
    return [
        OCRAnchor(text="Buchen", region=(120, 40, 80, 24)),
        OCRAnchor(text=customer, region=(320, 260, 200, 20)),
    ]


def _walk(node, path="$"):
    """Yield (path, key, value) for every mapping entry in a structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


# ── the census: measured, not guessed ───────────────────────────────────────
def test_census_variance_is_content_recurrence_is_chrome():
    census = _fed_census()
    # "Speichern" sat at AXButton#0 in all three sessions, unchanged.
    v = census.classify(app="SAP Logon", state_ref="h1booking", kind=KIND_AX_TITLE,
                        slot="AXButton#0", text="Speichern")
    assert v.verdict == CHROME and v.sessions == 3 and v.variants == 1

    # AXCell#0 carried a different name in every session -> data slot.
    v = census.classify(app="SAP Logon", state_ref="h1booking", kind=KIND_AX_TITLE,
                        slot="AXCell#0", text=CUSTOMER)
    assert v.verdict == CONTENT
    assert "varies_per_record" in v.reason
    assert CUSTOMER not in v.reason  # a verdict never quotes the text


def test_census_single_session_is_unsure_not_chrome():
    census = _fed_census(customers=("Mueller, Anna",))
    v = census.classify(app="SAP Logon", state_ref="h1booking", kind=KIND_AX_TITLE,
                        slot="AXButton#0", text="Speichern")
    assert v.verdict == UNSURE and v.sessions == 1


def test_census_replaying_one_session_cannot_manufacture_chrome():
    census = LocatorCensus()
    for _ in range(9):
        census.observe_state_graph(_graph([_booking_screen(CUSTOMER)]),
                                   session_id="same-session")
    v = census.classify(app="SAP Logon", state_ref="h1booking", kind=KIND_AX_TITLE,
                        slot="AXButton#0", text="Speichern")
    assert v.verdict == UNSURE and v.sessions == 1


def test_census_roundtrip_on_disk():
    census = _fed_census()
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "census.json"
        census.save(path)
        again = LocatorCensus.load(path)
    assert again.slots == census.slots
    assert LocatorCensus.load(pathlib.Path("/nonexistent/census.json")).slots == {}


def test_slot_addressing():
    assert element_slots([_el("AXButton", "a"), _el("AXCell", "b"),
                          _el("AXButton", "c")]) == \
        ["AXButton#0", "AXCell#0", "AXButton#1"]
    assert ocr_slot((320, 260, 200, 20)) == "region:40,32,25,2"
    assert ocr_slot(None) is None      # no region -> no structural address
    assert ocr_slot((1, 2)) is None


# ── the four core cases ─────────────────────────────────────────────────────
def test_data_valued_ocr_anchor_does_not_travel():
    census = _fed_census(anchors_for=_anchors, clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()],
                          [_transition("sap_h1", "sap_h2", "buchung abschliessen")]))

    result = P.project_app_guide(
        guide, census, ocr_anchors={"sap_h1": _anchors(CUSTOMER)})
    blob = json.dumps(result)

    booking = next(s for s in result["map"]["states"]
                   if s["state_ref"] == "h1booking")
    texts = [a["text"] for a in booking["ocr_anchors"]]
    assert texts == ["Buchen"]                       # chrome travelled
    assert CUSTOMER not in blob                      # content did not, anywhere
    assert result["map"]["completeness"]["ocr_anchors_withheld"] == 1
    assert any(e["kind"] == "ocr_anchor" and e["reason"].startswith(CONTENT)
               for e in result["report"]["excluded"])

    # The cell the customer name sat in still travels as an anonymous slot, so
    # the other tenant can find their OWN value's field. Excluding the locator
    # cost the structure nothing.
    cell = next(e for e in booking["elements"] if e["slot"] == "AXCell#0")
    assert cell["label"] is None
    assert cell["withheld"]["label"].startswith(CONTENT)
    assert cell["role"] == "AXCell"


def test_pure_chrome_guide_projects_intact():
    """A guide with no per-record locator at all keeps every state, every edge,
    every label and a usable step order."""
    census = LocatorCensus()
    chrome_state = _state("sap_h1", "SAP Logon",
                          [_el("AXButton", "Speichern", identifier="SaveBtn"),
                           _el("AXButton", "Buchen")], "h1chrome")
    graph = _graph([chrome_state, _confirm_screen()],
                   [_transition("sap_h1", "sap_h2", "buchung abschliessen")])
    for i in range(3):
        census.observe_state_graph(graph, session_id=f"s{i}",
                                   ocr_anchors={"sap_h1": [
                                       OCRAnchor(text="Buchen",
                                                 region=(120, 40, 80, 24))]})

    result = P.project_app_guide(_guide(graph), census, ocr_anchors={
        "sap_h1": [OCRAnchor(text="Buchen", region=(120, 40, 80, 24),
                             click_offset=(2, -3))]})
    m = result["map"]

    assert m["projection_version"] == P.PROJECTION_VERSION
    assert m["kind"] == "anyuse_app_map"
    # The machine reported "SAP Logon". What travels is the CURATED name that
    # matched, and the handle is slugified from that — so a tenant whose OS says
    # "SAP GUI" lands on this same map instead of a second one. Nothing in the
    # identity block came off the machine: two strings out of the vendor file,
    # two literals. No bundle id, and no ``guide.app_aliases`` either — the
    # aliases are the entry's OTHER identity tokens.
    assert m["ref"] == "anyuse:map/sap_gui"
    assert m["app"] == {"name": "SAP GUI", "guide_type": "application",
                        "identity_provenance": P.IDENTITY_OS_APP_NAME,
                        "aliases": ["SAP Logon"]}
    assert result["report"]["identity"]["established"] is True
    assert result["report"]["identity"]["recognised"] is True
    # The identity is named once, at map["app"] — never again per screen.
    assert all("app" not in s for s in m["states"]), m["states"]
    assert [s["state_ref"] for s in m["states"]] == ["h1chrome", "h2confirm"]

    head = m["states"][0]
    assert head["role_frequency"] == {"AXButton": 2}
    assert head["structural_hash"] == "h1chrome"
    assert [(e["slot"], e["label"], e["identifier"]) for e in head["elements"]] == [
        ("AXButton#0", "Speichern", "SaveBtn"), ("AXButton#1", "Buchen", None)]
    assert all("withheld" not in e for e in head["elements"])
    assert head["ocr_anchors"] == [{
        "slot": "region:15,5,10,3", "text": "Buchen",
        "region": [120, 40, 80, 24], "click_offset": [2, -3],
        "evidence": {"sessions": 3, "variants": 1}}]

    assert len(m["transitions"]) == 1
    t = m["transitions"][0]
    assert (t["from"], t["to"], t["intent"]) == ("h1chrome", "h2confirm",
                                                 "buchung abschliessen")
    assert t["intent_withheld"] is None
    assert t["modalities"] == [{"modality": "ax", "times_used": 4,
                                "times_succeeded": 4, "success_rate": 1.0}]
    assert m["step_order"] == [{"path_id": "p0",
                                "states": ["h1chrome", "h2confirm"],
                                "transitions": [0]}]
    assert m["completeness"]["states_withheld"] == 0
    assert m["completeness"]["labels_withheld"] == 0
    assert result["report"]["leaks"] == []


def test_screenshot_bytes_never_appear_under_any_input():
    """Every string-typed field on the guide, its states, its elements and its
    transition commands is replaced with raw PNG bytes. The projection is an
    allow-list, so none of it is even read."""
    census = LocatorCensus()
    chrome_state = _state("sap_h1", "SAP Logon", [_el("AXButton", "Speichern")], "h1chrome")
    graph = _graph([chrome_state, _confirm_screen()],
                   [_transition("sap_h1", "sap_h2", "buchung abschliessen")])
    for i in range(3):
        census.observe_state_graph(graph, session_id=f"s{i}")

    png = PNG_MAGIC + b"screenshot-of-the-customer-record" * 8
    guide = _guide(graph)
    guide.full_guide = png
    guide.short_description = png
    guide.ui_landmarks = [png]
    guide.navigation_tips = [png]
    guide.common_tasks = [png]
    guide.use_cases = [png]
    guide.common_shortcuts = {"save": png}
    guide.user_activities = [ActivityRecord(description=png, notes=png, task=png)]
    guide.agent_activities = [ActivityRecord(description=png, notes=png)]
    guide.linked_workflows = [png]

    dirty = guide.get_state_graph()
    victim = dirty.states["sap_h1"]
    victim.url_patterns = [png]
    victim.title_patterns = [png]
    victim.custom_render_notes = [png]
    victim.last_seen = png
    for el in victim.elements:
        el.ax_path = png          # a Selector in production; bytes here
        el.js_selector = png
        el.description = png
    for t in dirty.transitions:
        t.last_failure_reason = png
        for impl in t.implementations:
            impl.command = {"screenshot": png, "script": png}
    guide.set_state_graph(dirty)

    result = P.project_app_guide(guide, census, ocr_anchors={"sap_h1": [
        OCRAnchor(text=png, region=(1, 2, 3, 4)),          # bytes as anchor text
        OCRAnchor(text="Speichern", region=(1, 2, 3, 4)),  # never measured
    ]})

    P._assert_shareable(result)                             # no bytes, no objects
    blob = json.dumps(result).encode()
    assert PNG_MAGIC not in blob
    assert b"screenshot-of-the-customer-record" not in blob
    assert result["map"]["states"][0]["ocr_anchors"] == []
    assert result["map"]["states"][0]["elements"][0]["label"] == "Speichern"


def test_leak_guard_is_fatal_not_filtering():
    with pytest.raises(P.ProjectionLeak) as exc:
        P._assert_shareable({"states": [{"thumb": PNG_MAGIC}]})
    assert "$.states[0].thumb" in str(exc.value)

    class Selector:
        pass

    with pytest.raises(P.ProjectionLeak):
        P._assert_shareable({"ax_path": Selector()})


def test_unsure_locator_is_excluded():
    """Two shapes of unsure, both excluded: a label seen in only one session,
    and an OCR anchor with no region (nothing to measure a slot against)."""
    census = LocatorCensus()
    two_button = _state("sap_h1", "SAP Logon",
                        [_el("AXButton", "Speichern"), _el("AXButton", "Drucken")],
                        "h1chrome",
                        required=[{"role": "AXButton", "title": "Speichern"}])
    for i in range(3):
        census.observe_state_graph(
            _graph([_state("sap_h1", "SAP Logon", [_el("AXButton", "Speichern")],
                           "h1chrome",
                           required=[{"role": "AXButton", "title": "Speichern"}])]),
            session_id=f"s{i}")
    # "Drucken" at AXButton#1 appears in exactly ONE session.
    census.observe_state_graph(_graph([two_button]), session_id="s-late")

    result = P.project_app_guide(_guide(_graph([two_button])), census, ocr_anchors={
        "sap_h1": [OCRAnchor(text="Speichern", region=None)]})
    m = result["map"]

    elements = {e["slot"]: e for e in m["states"][0]["elements"]}
    assert elements["AXButton#0"]["label"] == "Speichern"        # measured chrome
    assert elements["AXButton#1"]["label"] is None               # unsure -> withheld
    assert elements["AXButton#1"]["withheld"]["label"] == \
        "unsure:seen_in_1_of_min_2_sessions"

    assert m["states"][0]["ocr_anchors"] == []                   # no region
    assert m["completeness"]["ocr_anchors_withheld"] == 1
    assert [e["reason"] for e in result["report"]["excluded"]
            if e["kind"] == "ocr_anchor"] == ["unsure:no_structural_slot"]

    # Raising min_sessions retracts more, never less: at 5, even "Speichern"
    # (4 sessions) is unsure, so the fingerprint carrying it drops the node.
    stricter = P.project_app_guide(_guide(_graph([two_button])), census,
                                   min_sessions=5)
    assert stricter["map"]["states"] == []
    assert stricter["map"]["completeness"]["states_withheld"] == 1


# ── the fingerprint leak ────────────────────────────────────────────────────
def test_fingerprint_embedding_a_value_drops_the_whole_node():
    """UIFingerprint.required_elements carries raw AX titles next to a clean
    role-only hash. When one of them is not measured chrome, the node's IDENTITY
    is content — so the node is dropped, not sanitised."""
    census = _fed_census()
    guide = _guide(_graph([_booking_screen(CUSTOMER), _confirm_screen()],
                          [_transition("sap_h1", "sap_h2", "buchung abschliessen")]))

    result = P.project_app_guide(guide, census,
                                 ocr_anchors={"sap_h1": _anchors(CUSTOMER)})
    blob = json.dumps(result)

    assert [s["state_ref"] for s in result["map"]["states"]] == ["h2confirm"]
    # Anchors on a withheld node are withheld anchors, and are counted as such.
    assert result["map"]["completeness"]["ocr_anchors_projected"] == 0
    assert result["map"]["completeness"]["ocr_anchors_withheld"] == 2
    assert CUSTOMER not in blob

    leak = result["report"]["leaks"][0]
    assert leak["where"] == "UIFingerprint.required_elements"
    assert leak["state_id"] == "sap_h1" and leak["state_ref"] == "h1booking"
    assert [s["slot"] for s in leak["slots"]] == ["AXCell#0"]
    assert leak["slots"][0]["verdict"] == CONTENT

    # The edge into the dropped node goes with it — a half-graph is not shipped.
    assert result["map"]["transitions"] == []
    assert result["map"]["step_order"] == []
    assert any(e["kind"] == "transition" and e["reason"] == "endpoint_not_projected"
               for e in result["report"]["excluded"])


def test_a_state_sharing_a_tainted_structural_ref_is_dropped_too():
    """Two local state_ids collapse onto one structural ref (the generator hashes
    role+title, so a fresh id is minted per record). If either one's fingerprint
    is content, the ref itself is tainted."""
    census = _fed_census()
    clean = _state("sap_rec_a", "SAP Logon",
                   [_el("AXButton", "Speichern"), _el("AXButton", "Buchen"),
                    _el("AXTextField", "Betrag", el_type="text_field",
                        actions=("set_value",))],
                   "h1booking",
                   required=[{"role": "AXButton", "title": "Speichern"}])
    result = P.project_app_guide(
        _guide(_graph([clean, _booking_screen(CUSTOMER)])), census)
    assert result["map"]["states"] == []
    assert {e["reason"] for e in result["report"]["excluded"]} == {
        "fingerprint_embeds_content", "sibling_state_fingerprint_embeds_content"}


def test_empty_census_projects_nothing():
    """The conservative default at the whole-map scale: nothing measured, so
    nothing is known to be chrome, so nothing travels."""
    graph = _graph([_booking_screen(CUSTOMER), _confirm_screen()],
                   [_transition("sap_h1", "sap_h2", "buchung abschliessen")])
    result = P.project_app_guide(_guide(graph), LocatorCensus())
    assert result["map"]["states"] == []
    assert result["map"]["transitions"] == []
    assert result["map"]["completeness"]["states_withheld"] == 2


def test_intent_prose_obeys_the_same_measured_rule():
    """An intent is free prose about a real screen; it can name a record. Same
    ledger, same conservative default — the edge survives, the label does not."""
    census = LocatorCensus()
    a = _state("sap_h1", "SAP Logon", [_el("AXButton", "Speichern")], "h1chrome")
    b = _confirm_screen()
    for i in range(3):
        census.observe_state_graph(_graph([a, b]), session_id=f"s{i}")
    graph = _graph([a, b], [_transition("sap_h1", "sap_h2",
                                        f"buchung fuer {CUSTOMER} anlegen")])

    result = P.project_app_guide(_guide(graph), census)
    t = result["map"]["transitions"][0]
    assert (t["from"], t["to"]) == ("h1chrome", "h2confirm")
    assert t["intent"] is None
    assert t["intent_withheld"] == "unsure:slot_never_measured"
    assert CUSTOMER not in json.dumps(result)


# ── app identity: RECORDED provenance, never a look at the string ───────────
TENANT_URL = "https://mueller-anna-gmbh.billing.acme-erp.example/fi/invoices/4711"
TENANT_HOST = "mueller-anna-gmbh.billing.acme-erp.example"

# The form that refuted the first version of this rule. An on-prem ERP behind a
# flat intranet name: _get_key reduces the URL to a netloc with NO DOT, so
# _create_guide's `"." in key` said "application", the guide was born without a
# domain, and the provenance was read back off that as os_app_name. A URL-derived
# identity laundered into an OS app name by a dot.
INTRANET_URL = "http://angebote-mueller-gmbh/erp/kunde/4711"
INTRANET_HOST = "angebote-mueller-gmbh"


def _chrome_states(app):
    """Two screens of pure app chrome, recorded under ``app``."""
    return [_state("w_h1", app, [_el("AXButton", "Speichern", "SaveBtn"),
                                 _el("AXButton", "Buchen")], "h1chrome"),
            _state("w_h2", app, [_el("AXButton", "Schliessen")], "h2confirm")]


def _census_for(app, sessions=3):
    census = LocatorCensus()
    for i in range(sessions):
        census.observe_state_graph(
            _graph(_chrome_states(app),
                   [_transition("w_h1", "w_h2", "buchung abschliessen")]),
            session_id=f"s{i}")
    return census


def _generator(tmp_path):
    """A real GuideGenerator on a throwaway store. The LLM client is cleared:
    these are unit tests and a synthesized description is not what is under
    examination."""
    from anyuse.guides.generator import GuideGenerator
    from anyuse.guides.storage import GuideStorage

    gen = GuideGenerator(GuideStorage(base_dir=tmp_path / "guides"),
                         min_observations=1, auto_generate=False)
    gen.client = None
    return gen


def _generated_guide(tmp_path, *, app="", url=""):
    """A guide built by the REAL chain, with nothing stamped by hand.

        ``generator._get_key(app=/url=)``  -> the storage key, and the recorded
                                              fact of which argument it came on
        ``generator._create_guide(key)``   -> the guide, carrying that fact

    Returns ``(key, guide)``.
    """
    gen = _generator(tmp_path)
    key = gen._get_key(app=app, url=url)
    guide = gen._create_guide(key, gen._get_observations(key))
    guide.set_state_graph(_graph(_chrome_states(key),
                                 [_transition("w_h1", "w_h2",
                                              "buchung abschliessen")]))
    return key, guide


def test_an_intranet_host_is_url_provenance_however_it_reads(tmp_path):
    """THE REFUTATION OF THE PROVENANCE-BY-KIND RULE, run end to end.

    ``angebote-mueller-gmbh`` is the netloc of a visited URL and it has no dot,
    no scheme and no slash. Every string test that tried to spot a host — the
    shipped ``"." in key`` included — calls it an application, and the tenant
    then travels stamped ``os_app_name``. The recorded fact does not care how it
    reads: it came in on ``url=``, so it is ``url_host``."""
    key, guide = _generated_guide(tmp_path, url=INTRANET_URL)

    assert key == INTRANET_HOST, key
    # The blind spot itself, asserted rather than described: the old test says
    # "application" about a URL's host.
    assert not ("." in key and not key.startswith("/")), key

    assert guide.identity_provenance == P.IDENTITY_URL_HOST, guide.identity_provenance
    assert guide.guide_type is GuideType.WEBSITE
    assert guide.domain == key

    result = P.project_app_guide(guide, _census_for(key))
    assert result["map"] is None, result["map"]
    assert result["report"]["identity"]["provenance"] == P.IDENTITY_URL_HOST

    blob = json.dumps(result, ensure_ascii=False).lower()
    assert INTRANET_HOST not in blob and "mueller" not in blob


def test_a_url_derived_identity_does_not_project_at_all(tmp_path):
    """THE REFUTED EXEMPTION. name/domain/guide_type used to skip the census as
    "app identity, never observed content". For a website guide they are the host
    of a visited URL, so the customer travelled in the clear."""
    key, guide = _generated_guide(tmp_path, url=TENANT_URL)
    assert key == TENANT_HOST, key
    result = P.project_app_guide(guide, _census_for(key))

    assert result["map"] is None, result["map"]
    verdict = result["report"]["identity"]
    assert verdict["established"] is False, verdict
    assert verdict["provenance"] == P.IDENTITY_URL_HOST, verdict

    # Not the map (there is none), not the report, not the refusal reason.
    blob = json.dumps(result, ensure_ascii=False).lower()
    assert TENANT_HOST not in blob and "mueller-anna-gmbh" not in blob


def test_a_plain_vendor_host_is_refused_on_the_same_provenance(tmp_path):
    """github.com is not tenant-scoped and it STILL does not travel.

    The refusal is on where the name came from, not on how it reads. The moment
    it turned on the string it would be a denylist — and a tenant host looks
    exactly like an ordinary host."""
    key, guide = _generated_guide(tmp_path, url="https://github.com/anthropics")
    assert key == "github.com", key
    result = P.project_app_guide(guide, _census_for(key))
    assert result["map"] is None
    assert result["report"]["identity"]["provenance"] == P.IDENTITY_URL_HOST


def test_a_desktop_app_identity_travels_with_its_provenance():
    """The capability the refusal must not cost: a RECOGNISED vendor application
    still crosses, under the curated name rather than the observed one."""
    census = _fed_census(clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()],
                          [_transition("sap_h1", "sap_h2", "buchung abschliessen")]))
    result = P.project_app_guide(guide, census)
    assert result["map"]["app"]["identity_provenance"] == P.IDENTITY_OS_APP_NAME
    assert result["map"]["app"]["name"] == "SAP GUI"
    assert "domain" not in result["map"]["app"]
    assert result["map"]["states"], result["report"]


def test_a_generated_desktop_guide_records_os_app_name_and_projects(tmp_path):
    """The same capability, but through the generator rather than a hand-built
    guide: an ``app=`` key records ``os_app_name`` and the map still crosses."""
    key, guide = _generated_guide(tmp_path, app="SAP Logon")
    assert key == "sap logon", key
    assert guide.identity_provenance == P.IDENTITY_OS_APP_NAME
    assert guide.guide_type is GuideType.APPLICATION
    assert guide.domain is None

    result = P.project_app_guide(guide, _census_for(key))
    assert result["map"] is not None, result["report"]["identity"]
    assert result["map"]["app"]["identity_provenance"] == P.IDENTITY_OS_APP_NAME
    assert result["map"]["states"], result["report"]


def test_a_guide_with_no_recorded_provenance_is_refused(tmp_path):
    """Every guide file written before the fact was recorded. Its name may well
    be a vendor's — but nobody wrote down where it came from, and reading it back
    off the string is the bug. Fail closed, and say so in the verdict."""
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]), identity_provenance=None)
    result = P.project_app_guide(guide, _fed_census(clean_fingerprint=True))
    assert result["map"] is None
    assert result["report"]["identity"]["provenance"] == P.IDENTITY_UNRECORDED

    # And it stays refused across a save/load: from_dict does not invent one.
    reloaded = AppGuide.from_dict(guide.to_dict())
    assert reloaded.identity_provenance is None
    assert P.project_app_guide(reloaded, _fed_census(clean_fingerprint=True))["map"] is None


def test_the_recorded_provenance_survives_storage(tmp_path):
    """It has to be PERSISTED, not recomputed: a guide is written once and
    projected on some later run, by which time the URL is long gone."""
    from anyuse.guides.storage import GuideStorage

    _, url_guide = _generated_guide(tmp_path, url=INTRANET_URL)
    _, app_guide = _generated_guide(tmp_path / "b", app="SAP Logon")

    store = GuideStorage(base_dir=tmp_path / "roundtrip")
    store.save(url_guide)
    store.save(app_guide)
    reread = GuideStorage(base_dir=tmp_path / "roundtrip")

    assert reread.get(INTRANET_HOST).identity_provenance == P.IDENTITY_URL_HOST
    assert reread.get("sap logon").identity_provenance == P.IDENTITY_OS_APP_NAME


def test_url_provenance_is_sticky_across_a_later_app_sighting(tmp_path):
    """A host and an app name are the same kind of string, so the same key can
    arrive on both arguments. Once it has been a URL's host it stays one: no
    later observation unlearns that the string may be the tenant."""
    gen = _generator(tmp_path)
    assert gen._get_key(url=INTRANET_URL) == INTRANET_HOST
    assert gen._get_key(app=INTRANET_HOST) == INTRANET_HOST

    guide = gen._create_guide(INTRANET_HOST, gen._get_observations(INTRANET_HOST))
    assert guide.identity_provenance == P.IDENTITY_URL_HOST


def test_asking_about_a_key_does_not_invent_a_provenance(tmp_path):
    """A lookup is not an observation.

    ``get_observations(key)`` used to normalise through ``_get_key(app=key)``, so
    merely ASKING about an unrecorded host would have recorded it as an OS app
    name — a query laundering an identity, which is the same bug one level up."""
    gen = _generator(tmp_path)
    assert gen.get_observations(INTRANET_HOST) is None
    assert gen._key_provenance == {}, gen._key_provenance

    guide = gen._create_guide(INTRANET_HOST, gen._get_observations(INTRANET_HOST))
    assert guide.identity_provenance is None
    result = P.project_app_guide(guide, _census_for(INTRANET_HOST))
    assert result["map"] is None
    assert result["report"]["identity"]["provenance"] == P.IDENTITY_UNRECORDED


def test_an_application_guide_carrying_a_domain_is_refused():
    """Mixed provenance is unestablished provenance: a domain can only have come
    from a URL, whatever the record says."""
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]))
    guide.domain = TENANT_HOST
    result = P.project_app_guide(guide, _fed_census(clean_fingerprint=True))
    assert result["map"] is None
    assert result["report"]["identity"]["provenance"] == P.IDENTITY_URL_HOST


def test_a_state_recorded_under_a_foreign_app_does_not_travel():
    """UIState.app was copied onto every projected node, so the identity string
    rode the wire once per screen — and a screen recorded under a browser on a
    tenant host put THAT there. A foreign scope is an identity nobody established,
    so the node is dropped, exactly like a content-bearing fingerprint."""
    foreign = _state("web_h1", TENANT_HOST,
                     [_el("AXButton", "Speichern"), _el("AXButton", "Buchen")],
                     "h9foreign")
    census = _fed_census(clean_fingerprint=True)
    for i in range(3):
        census.observe_state_graph(_graph([foreign]), session_id=f"w{i}")

    graph = _graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                    _confirm_screen(), foreign],
                   [_transition("sap_h1", "sap_h2", "buchung abschliessen")])
    result = P.project_app_guide(_guide(graph), census)

    refs = [s["state_ref"] for s in result["map"]["states"]]
    assert refs == ["h1booking", "h2confirm"], refs
    assert "foreign_app_identity" in {e.get("reason")
                                      for e in result["report"]["excluded"]}
    assert TENANT_HOST not in json.dumps(result, ensure_ascii=False)


# ── where the app NAME came from: the watcher, one link before the key ───────
#
# The rule above asks where a guide KEY came from and trusts the answer; one
# link earlier, the question is where the ARGUMENT came from. For generic
# process names the watcher SYNTHESISED one out of the focused window's title:
#
#     if current_app.lower() in ("electron", "helper", "node"):
#         display_app = f"{current_app} ({window_title.split(' - ')[0][:30]})"
#
# — and Electron is VS Code, Slack, Teams and Notion. "Electron (Angebot Mueller
# GmbH)" then arrived on `app=`, was recorded `os_app_name` truthfully, and every
# check downstream passed on a string that was the customer.

CUSTOMER_WINDOW_TITLE = "Angebot Mueller GmbH - Notion"
SYNTHESISED_NAME = "Electron (Angebot Mueller GmbH)"


def test_no_never_travels_key_appears_in_the_map():
    census = _fed_census(anchors_for=_anchors, clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()],
                          [_transition("sap_h1", "sap_h2", "buchung abschliessen")]))
    result = P.project_app_guide(guide, census,
                                 ocr_anchors={"sap_h1": _anchors(CUSTOMER)})
    offenders = [path for path, key, _ in _walk(result["map"])
                 if key in P.NEVER_TRAVELS]
    assert offenders == []


def test_the_map_never_carries_a_local_state_id():
    census = _fed_census(anchors_for=_anchors, clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()],
                          [_transition("sap_h1", "sap_h2", "buchung abschliessen")]))
    blob = json.dumps(P.project_app_guide(
        guide, census, ocr_anchors={"sap_h1": _anchors(CUSTOMER)})["map"])
    assert "sap_h1" not in blob and "sap_h2" not in blob


def test_the_bundle_id_is_not_projected_whatever_it_holds():
    """THE FIX ON THE PRODUCING SIDE.

    ``identity_provenance`` records where the NAME came from. It never covered
    ``bundle_id``, which was free text on the map and became the transport's app
    name whenever ``name`` was empty. The fix is subtraction: the key is not
    emitted, its VALUE never leaves this machine, and ``NEVER_TRAVELS`` pins the
    name so the walk above fails if a later edit puts it back."""
    census = _fed_census(anchors_for=_anchors, clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]))
    # An honest desktop guide whose bundle id is a content string.
    guide.bundle_id = f"{CUSTOMER} / {TENANT_HOST}"

    result = P.project_app_guide(guide, census)
    m = result["map"]
    assert result["report"]["identity"]["established"] is True
    assert "bundle_id" in P.NEVER_TRAVELS
    assert "bundle_id" not in m["app"], m["app"]
    blob = json.dumps(m, ensure_ascii=False)
    assert CUSTOMER not in blob and TENANT_HOST not in blob, blob
    # ...and the map still names the app, so the receiver can still identify it.
    assert m["app"]["name"] == "SAP GUI", m["app"]


# ── identity is RECOGNISED, not derived ──────────────────────────────────────
#
# A synthesised name and a model-reported one are one class through two
# producers: the WATCHER synthesised an app name out of a window title, and the
# MODEL reported a screen as the application.
# Both were fixed at the producer, and neither fix says anything about a third
# producer. So the identity is no longer derived from the machine at all — it is
# MATCHED against a curated list of vendor software, exactly, and an application
# we do not recognise does not travel.

# The model-reported name: what the observing model writes into a
# CONTEXT_CHANGE event when it reads the screen's own title as the application
# it is looking at.
MODEL_REPORTED_SCREEN = "Angebot Mueller GmbH - Kalkulation"

# Runtimes and shells that MANY applications report themselves as. An entry for
# one of these would let every in-house app built on it travel under a vendor's
# name, so the list must not contain them.
GENERIC_RUNTIMES = ("electron", "node", "helper", "python", "java", "javaw",
                    "wine", "python3", "sh", "bash")


def _projected_under(tmp_path, app):
    """The real chain for one observed application name: key -> guide -> map."""
    key, guide = _generated_guide(tmp_path, app=app)
    return P.project_app_guide(guide, _census_for(key))


def test_the_r5_synthesised_name_matches_nothing_and_does_not_travel(tmp_path):
    """A SYNTHESISED NAME against the recognition rule, not against its producer.

    "Electron (Angebot Mueller GmbH)" is what the watcher used to hand the
    generator. Even arriving with a perfectly honest ``os_app_name`` record — as
    it did — it matches no entry, so there is no identity and no map. And it does
    NOT match "Electron": a near-match is a non-match, which is the property the
    whole rule rests on."""
    result = _projected_under(tmp_path, SYNTHESISED_NAME)
    assert result["map"] is None, result["map"]
    verdict = result["report"]["identity"]
    assert verdict["established"] is False and verdict["recognised"] is False
    # The refusal does not quote the name it refused.
    blob = json.dumps(result, ensure_ascii=False).lower()
    for leaked in ("mueller", "angebot", "gmbh", "electron"):
        assert leaked not in blob, (leaked, blob)

    # Exactness, stated directly: the decorated form matches nothing, and it
    # would not match even if the bare runtime WERE on the list.
    assert P.recognised_vendor(SYNTHESISED_NAME) is None
    assert P.recognised_vendor("Electron") is None


def test_a_model_reported_screen_name_matches_nothing_either(tmp_path):
    """A MODEL-REPORTED NAME: the other producer of the same class.

    ``{"type": "CONTEXT_CHANGE", "app": "Angebot Mueller GmbH - Kalkulation"}``
    — the model naming the screen it is looking at as the application. Nothing
    about the string says it is not an app name, which is exactly why nothing
    tries to read it: it is not on the list, so it does not travel."""
    result = _projected_under(tmp_path, MODEL_REPORTED_SCREEN)
    assert result["map"] is None, result["map"]
    assert result["report"]["identity"]["recognised"] is False
    assert P.recognised_vendor(MODEL_REPORTED_SCREEN) is None
    blob = json.dumps(result, ensure_ascii=False).lower()
    for leaked in ("mueller", "angebot", "kalkulation"):
        assert leaked not in blob, (leaked, blob)


def test_a_vendor_name_with_a_customer_suffix_is_a_non_match(tmp_path):
    """THE EXACTNESS PROPERTY, over the whole list rather than one example.

    Every decoration of a listed name — a customer suffix, a document prefix, a
    parenthesised tail, a path — is a NON-MATCH. Nothing here is a substring
    test, so nothing here can be talked into one by a longer string."""
    listed = sorted(P.load_vendor_applications())
    assert listed, "the shipped list is empty"
    customer = "Kunde Mueller GmbH"
    for token in listed:
        for decorated in (f"{token} - {customer}", f"{token} ({customer})",
                          f"{customer} - {token}", f"{token}.{customer}",
                          f"{token}{customer}", f"/Applications/{token}.app",
                          token[:-1], token + "2"):
            assert P.recognised_vendor(decorated) is None, decorated

    # And end to end, on a decorated vendor title.
    result = _projected_under(tmp_path, f"Microsoft Excel - {customer}")
    assert result["map"] is None, result["map"]
    assert "mueller" not in json.dumps(result, ensure_ascii=False).lower()


def test_a_recognised_vendor_application_still_projects(tmp_path):
    """The capability. A genuine "Microsoft Excel" map projects intact, with the
    curated name on it and the handle slugified from that."""
    result = _projected_under(tmp_path, "Microsoft Excel")
    m = result["map"]
    assert m is not None, result["report"]["identity"]
    assert m["app"]["name"] == "Microsoft Excel", m["app"]
    assert m["app"]["aliases"] == [], m["app"]
    assert m["ref"] == "anyuse:map/microsoft_excel", m["ref"]
    assert m["states"], result["report"]
    assert result["report"]["identity"]["recognised"] is True


def test_the_name_that_travels_is_the_file_s_not_the_machine_s(tmp_path):
    """Two machines, two OS spellings, ONE identity and ONE handle.

    The observed name stays local — it is the scope the census was measured
    under — and the map is named by the entry that matched. That is what makes a
    second tenant's map of the same application mergeable with the first's, and
    it is also why no observed string can reach the map's identity: the map does
    not carry the machine's string at all."""
    logon = _projected_under(tmp_path / "a", "SAP Logon")["map"]
    gui = _projected_under(tmp_path / "b", "SAP GUI")["map"]
    assert logon["app"] == gui["app"] == {
        "name": "SAP GUI", "guide_type": "application",
        "identity_provenance": P.IDENTITY_OS_APP_NAME, "aliases": ["SAP Logon"]}
    assert logon["ref"] == gui["ref"] == "anyuse:map/sap_gui"
    # ...and the local scope really was different: the states were recorded and
    # measured under each machine's own key, and still projected.
    assert logon["states"] and gui["states"]


def test_the_guides_own_aliases_never_travel():
    """``guide.app_aliases`` is machine-derived text that nothing attested.

    It used to be copied onto the map because "the name's provenance covers the
    same name's other spellings" — the reasoning the synthesised name broke. The
    map's ``aliases`` are the matched ENTRY's other identity tokens; the guide's
    field is not read, so a customer string in it reaches nothing."""
    census = _fed_census(clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]))
    guide.app_aliases = [f"{CUSTOMER} GmbH", {"id": TENANT_HOST}]

    m = P.project_app_guide(guide, census)["map"]
    assert m["app"]["aliases"] == ["SAP Logon"], m["app"]
    assert "app_aliases" in P.NEVER_TRAVELS
    blob = json.dumps(m, ensure_ascii=False)
    assert CUSTOMER not in blob and TENANT_HOST not in blob, blob


def test_a_list_that_cannot_be_read_recognises_nothing(tmp_path, monkeypatch):
    """FAIL CLOSED. A missing or malformed file shares NOTHING.

    Under-sharing is recoverable by fixing the file; a list that defaulted to
    "recognise everything" when it could not be read would reopen the leak with an
    apology. The reason lands on the local report so an operator can see which
    file to fix."""
    census = _fed_census(clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]))

    broken = tmp_path / "not-json.json"
    broken.write_text("{oh dear", encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"applications": []}), encoding="utf-8")
    ambiguous = tmp_path / "ambiguous.json"
    ambiguous.write_text(json.dumps({"applications": [
        {"application": "Alpha", "vendor": "A", "identity": ["Alpha", "Shared"]},
        {"application": "Beta", "vendor": "B", "identity": ["Beta", "shared"]}]}),
        encoding="utf-8")
    invented = tmp_path / "invented.json"
    invented.write_text(json.dumps({"applications": [
        {"application": "A Name No OS Reports", "vendor": "V",
         "identity": ["SAP Logon"]}]}), encoding="utf-8")

    for path in (tmp_path / "does-not-exist.json", broken, empty, ambiguous,
                 invented):
        monkeypatch.setenv(P.VENDOR_LIST_ENV, str(path))
        result = P.project_app_guide(guide, census)
        assert result["map"] is None, (path.name, result["map"])
        verdict = result["report"]["identity"]
        assert verdict["recognised"] is False, (path.name, verdict)
        with pytest.raises(P.VendorListError):
            P.load_vendor_applications(path)

    # And an operator's own longer list is honoured — the override is a real
    # extension point, not a way to fail.
    extended = tmp_path / "extended.json"
    extended.write_text(json.dumps({"applications": [
        {"application": "Acme ERP Client", "vendor": "Acme",
         "identity": ["Acme ERP Client", "SAP Logon"]}]}), encoding="utf-8")
    monkeypatch.setenv(P.VENDOR_LIST_ENV, str(extended))
    assert P.project_app_guide(guide, census)["map"]["app"]["name"] == \
        "Acme ERP Client"


def test_the_list_is_data_and_nothing_observed_can_write_it():
    """The list gates the identity, so what can EDIT the list matters.

    It is a file on disk read by path — not a field on the guide, not a key on
    the map, not a row the ingest writes. Projecting a hostile guide does not
    touch it, and the only two ways to change it are an operator's editor and an
    operator's environment."""
    before = P.DEFAULT_VENDOR_LIST.read_bytes()
    stat_before = P.DEFAULT_VENDOR_LIST.stat().st_mtime_ns

    census = _fed_census(clean_fingerprint=True)
    guide = _guide(_graph([_booking_screen(CUSTOMER, clean_fingerprint=True),
                           _confirm_screen()]), name=MODEL_REPORTED_SCREEN)
    guide.app_aliases = ["Microsoft Excel"]      # "just add me to the list"
    guide.bundle_id = "Microsoft Excel"
    result = P.project_app_guide(guide, census)

    assert result["map"] is None, result["map"]
    assert P.DEFAULT_VENDOR_LIST.read_bytes() == before
    assert P.DEFAULT_VENDOR_LIST.stat().st_mtime_ns == stat_before


def test_the_shipped_list_is_a_claim_not_a_wish():
    """The file's own invariants, asserted rather than trusted.

    Every entry names a vendor and an application; the name that travels is one
    the operating system actually reports; no token is claimed twice; and no
    generic runtime is listed, because "Electron" is not an application — it is
    what four different vendors' applications call themselves."""
    raw = json.loads(P.DEFAULT_VENDOR_LIST.read_text(encoding="utf-8"))
    entries = raw["applications"]
    assert 5 <= len(entries) <= 40, len(entries)     # small, on purpose

    table = P.load_vendor_applications()
    for entry in entries:
        assert entry["vendor"].strip(), entry
        assert entry["application"].strip(), entry
        assert entry["application"] in entry["identity"], entry
        for token in entry["identity"]:
            assert table[token.strip().casefold()]["application"] == \
                entry["application"], token

    assert len(table) == sum(len(e["identity"]) for e in entries), "duplicate token"
    for runtime in GENERIC_RUNTIMES:
        assert runtime not in table, runtime


# ── the map's numbers are numbers ────────────────────────────────────────────
#
# ``completeness`` is read downstream AS COUNTS: the transport interpolates them
# into the one paragraph of prose it sends to a company knowledge base. So a value in
# that block that is not a count is not a cosmetic problem — it is free text
# wearing a number's name, and the leak guard cannot see it, because a string is
# a perfectly good JSON scalar.
#
# ``min_sessions`` is the way in. It is annotated ``int``, it is a CALLER's
# argument, and it is copied verbatim into ``completeness``. An annotation
# enforces nothing at runtime, so before this guard the projection could and did
# emit a completeness counter of any type the caller handed it.

def _projected(**kwargs):
    census = LocatorCensus()
    graph = _graph([_state("sap_h1", "SAP Logon", [_el("AXButton", "Speichern")],
                           "h1chrome")])
    for i in range(3):
        census.observe_state_graph(graph, session_id=f"s{i}")
    return P.project_app_guide(_guide(graph), census, **kwargs)


def test_every_completeness_value_is_a_count():
    """Not "is JSON" — IS A COUNT. The type is what the wire side reads."""
    completeness = _projected()["map"]["completeness"]
    assert completeness, completeness
    for key, value in completeness.items():
        assert isinstance(value, int) and not isinstance(value, bool), (key, value)
        assert value >= 0, (key, value)


@pytest.mark.parametrize("bad", ["3", {"n": 3}, [3], 3.0, True, -1])
def test_a_min_sessions_that_is_not_a_count_never_reaches_the_map(bad):
    """The second bug, stated as a rule: the projection refuses to BUILD a map
    whose counter is not a count, rather than shipping one and relying on the
    transport to notice. Two independent checks, same as the provenance token —
    this one is on the side that writes the number."""
    with pytest.raises(P.ProjectionLeak) as caught:
        _projected(min_sessions=bad)
    assert "min_sessions" in str(caught.value)
    assert str(bad) not in str(caught.value), "the refusal quoted the value"


@pytest.mark.parametrize("field,bad", [
    ("name", {"host": "mueller-anna-gmbh.example"}),
    ("name", ["SAP Logon"]),
    ("identity_provenance", ["os_app_name"]),
])
def test_an_identity_field_that_is_not_a_string_is_refused_not_stringified(field, bad):
    """``str(value)`` is the same defect one field over: it turns a dict into
    perfectly good-looking prose and puts it where a vendor's app name goes. The
    map either got a string or it did not get a name."""
    census = LocatorCensus()
    graph = _graph([_state("sap_h1", "SAP Logon", [_el("AXButton", "Speichern")],
                           "h1chrome")])
    for i in range(3):
        census.observe_state_graph(graph, session_id=f"s{i}")
    guide = _guide(graph)
    setattr(guide, field, bad)
    with pytest.raises(P.ProjectionLeak) as caught:
        P.project_app_guide(guide, census)
    assert field in str(caught.value)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
