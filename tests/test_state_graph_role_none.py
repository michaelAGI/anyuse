"""A live AX element can have AXRole = None; the drift gate must not die on it.

In the guest (TextEdit with its find bar open) the recorder's
element list contained ``{"role": None, "title": ""}``. ``_role_frequency``
returned that None as a key, ``_compute_role_hash`` sorted the items and
raised ``TypeError: '<' not supported between instances of 'NoneType' and
'str'`` — from inside ``evaluate_drift``, which is documented as PURE analysis
that never fails a replay. Flow 2 died on it. Run: python3 test_state_graph_role_none.py
"""
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.guides.state_graph import (  # noqa: E402
    StateGraph, UIFingerprint, UIState, _compute_role_hash, _role_frequency, compute_structural_hash)
from anyuse.execution.drift_gate import evaluate_drift  # noqa: E402

LIVE = [{"role": "AXTextArea", "title": ""}, {"role": None, "title": ""}, {"role": "AXButton", "title": "OK"}]


def test_role_none_is_counted_as_unknown_and_hashes():
    freq = _role_frequency(LIVE)
    assert freq == {"AXTextArea": 1, "unknown": 1, "AXButton": 1}
    assert isinstance(_compute_role_hash(LIVE), str) and len(_compute_role_hash(LIVE)) == 16
    assert compute_structural_hash(LIVE) == compute_structural_hash(
        [{"role": "AXTextArea"}, {"role": "unknown"}, {"role": "AXButton", "title": "OK"}])


def test_evaluate_drift_survives_a_role_none_element():
    fp = UIFingerprint(required_elements=[{"role": "AXButton", "title": "OK"}],
                       element_count_range=(1, 10), structural_hash=compute_structural_hash(LIVE),
                       role_frequency=_role_frequency(LIVE))
    g = StateGraph()
    g.add_state(UIState(state_id="s", app="TextEdit", fingerprint=fp))
    report = evaluate_drift(g, "s", LIVE)
    assert report.drifted is False and report.reason == ""


if __name__ == "__main__":
    test_role_none_is_counted_as_unknown_and_hashes()
    test_evaluate_drift_survives_a_role_none_element()
    print("ok")
