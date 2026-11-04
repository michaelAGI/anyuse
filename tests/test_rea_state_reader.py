"""Closed state-reader selection controls; no live frameworks or VM activity."""
import copy

import pytest

from anyuse.execution import rea_ax_state as S
from anyuse.execution import rea_owned_ax_state as O
from anyuse.execution import rea_state_reader as SR
import test_rea_ax_state as X
import test_rea_owned_ax_state as Y


def test_fixed_capability_registry_has_only_two_readonly_modules_and_no_dynamic_import_selector():
    assert SR.strictcap() == SR.STRICT_CAPABILITY == "ax_focused_readonly/1"
    assert SR.ownedcap() == SR.OWNED_CAPABILITY == "ax_owned_visible_readonly/1"
    assert SR.for_capability(SR.STRICT_CAPABILITY) is S and SR.for_capability(SR.OWNED_CAPABILITY) is O
    for name in (None, True, {}, [], "anyuse.execution.rea_owned_ax_state", "__import__", "ax_owned_visible_readonly/2"):
        with pytest.raises(ValueError): SR.for_capability(name)


def test_policy_version_branches_are_explicit_and_legacy_never_adopts_owned_results():
    strict = {"format": SR.POLICY1}
    owned = {"format": SR.POLICY2, "observer": {"capability": SR.OWNED_CAPABILITY}}
    assert SR.capability(strict) == SR.STRICT_CAPABILITY and SR.for_policy(strict) is S
    assert SR.capability(owned) == SR.OWNED_CAPABILITY and SR.for_policy(owned) is O
    old, new = X.observe(), Y.observe()
    assert S.matches(old, X.contract(old)) and O.matches(new, Y.contract(new))
    with pytest.raises(ValueError): SR.for_policy(strict).validate_contract(Y.contract(new))
    with pytest.raises(ValueError): SR.for_policy(owned).validate_contract(X.contract(old))
    assert strict == {"format": SR.POLICY1} and owned["observer"] == {"capability": SR.OWNED_CAPABILITY}


@pytest.mark.parametrize("policy", [None, [], {}, {"format": True}, {"format": "unknown"},
    {"format": SR.POLICY1, "observer": None}, {"format": SR.POLICY1, "observer": {"capability": SR.OWNED_CAPABILITY}},
    {"format": SR.POLICY2}, {"format": SR.POLICY2, "observer": {}},
    {"format": SR.POLICY2, "observer": {"capability": SR.STRICT_CAPABILITY}},
    {"format": SR.POLICY2, "observer": {"capability": SR.OWNED_CAPABILITY, "module": "caller_source"}},
    {"format": SR.POLICY2, "observer": {"capability": "ax_owned_visible_readonly/2"}}])
def test_unknown_or_retrospectively_modified_selector_fails_before_reader_import_or_io(policy, monkeypatch):
    def no_selection(name): raise AssertionError("invalid policy reached a reader")
    monkeypatch.setattr(SR, "for_capability", no_selection)
    saved = copy.deepcopy(policy)
    with pytest.raises(ValueError): SR.for_policy(policy)
    assert policy == saved


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
