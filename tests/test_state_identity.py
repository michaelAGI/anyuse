"""State-identity must be title-insensitive and role-frequency similarity must work.

These pin the fix: previously the state-id hashed (role,title) so any title
drift minted a new node (composition never fired), and structural_similarity always
returned 0.0 because role-frequency was never stored (the hash is one-way).
"""
from anyuse.guides.state_graph import (
    UIFingerprint, compute_structural_hash, generate_state_id as _generate_state_id,
)

# Imported from guides.state_graph, not memory.recorder: the id function moved
# there when the recorder and the guide generator stopped disagreeing about it
#. As a side effect this file no longer pulls
# memory.recorder -> memory.matcher -> pyautogui, so it can run under the
# desktop guard and has joined docs/TEST_ALLOWLIST.txt.


def test_state_id_title_insensitive():
    a = [{"role": "AXButton", "title": "invoice.pdf"}, {"role": "AXButton", "title": "Save"}, {"role": "AXTextField", "title": "name"}]
    b = [{"role": "AXButton", "title": "final_v2.pdf"}, {"role": "AXButton", "title": "Save"}, {"role": "AXTextField", "title": "x"}]
    assert _generate_state_id(a, "Finder") == _generate_state_id(b, "Finder"), "title drift must NOT change the state id"


def test_state_id_differs_on_structure():
    a = [{"role": "AXButton", "title": "x"}]
    b = [{"role": "AXButton", "title": "x"}, {"role": "AXSlider", "title": "y"}]
    assert _generate_state_id(a, "App") != _generate_state_id(b, "App")


def test_state_id_app_scoped():
    a = [{"role": "AXButton", "title": "x"}]
    assert _generate_state_id(a, "Finder") != _generate_state_id(a, "Safari")


def test_similarity_identical_is_one():
    els = [{"role": "AXButton"}, {"role": "AXButton"}, {"role": "AXTextField"}]
    fp = UIFingerprint(structural_hash=compute_structural_hash(els), role_frequency={"AXButton": 2, "AXTextField": 1})
    assert fp.matches(els)["structural_similarity"] == 1.0


def test_similarity_partial_is_between_zero_and_one():
    # THE regression: this used to be 0.0 because role_frequency was never stored.
    fp = UIFingerprint(structural_hash=compute_structural_hash([{"role": "AXButton"}, {"role": "AXButton"}, {"role": "AXTextField"}]),
                       role_frequency={"AXButton": 2, "AXTextField": 1})
    sim = fp.matches([{"role": "AXButton"}, {"role": "AXTextField"}])["structural_similarity"]
    assert 0.0 < sim < 1.0, sim


def test_role_frequency_persists_round_trip():
    fp = UIFingerprint(role_frequency={"AXButton": 3, "AXTextField": 1}, structural_hash="abc")
    fp2 = UIFingerprint.from_dict(fp.to_dict())
    assert fp2.role_frequency == {"AXButton": 3, "AXTextField": 1}


def test_legacy_fingerprint_derives_role_frequency():
    # legacy persisted data: no role_frequency, only required_elements → derive it
    fp = UIFingerprint.from_dict({"required_elements": [{"role": "AXButton"}, {"role": "AXButton"}], "structural_hash": "x"})
    assert fp.role_frequency == {"AXButton": 2}
    assert fp.matches([{"role": "AXButton"}, {"role": "AXButton"}])["structural_similarity"] == 1.0


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:160])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
