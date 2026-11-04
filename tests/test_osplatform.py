"""Platform seam: abstract verbs translate to native AX, delegation works, and the
package does NOT shadow the stdlib `platform` module."""
from anyuse.osplatform.base import ABSTRACT_VERBS, Verb
from anyuse.osplatform.macos import MacOSPlatform, _VERB_TO_AX


class _FakeAX:
    def __init__(self):
        self.calls = []

    def perform_action(self, el, action):
        self.calls.append(("perform", el, action)); return True

    def set_value(self, el, value):
        self.calls.append(("set_value", el, value)); return True

    def find_at(self, x, y):
        return None


class _FakeActuator:
    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append((action, params)); return {"success": True}


def test_invoke_maps_to_axpress():
    ax = _FakeAX()
    p = MacOSPlatform(ax_tree=ax, action_executor=_FakeActuator())
    assert p.perform_verb("EL", Verb.INVOKE) is True
    assert ax.calls == [("perform", "EL", "AXPress")]


def test_set_value_uses_ax_set_value():
    ax = _FakeAX()
    p = MacOSPlatform(ax_tree=ax, action_executor=_FakeActuator())
    assert p.perform_verb("EL", Verb.SET_VALUE, value="hello") is True
    assert ax.calls == [("set_value", "EL", "hello")]


def test_increment_maps_to_axincrement():
    ax = _FakeAX()
    MacOSPlatform(ax_tree=ax, action_executor=_FakeActuator()).perform_verb("E", Verb.INCREMENT)
    assert ax.calls == [("perform", "E", "AXIncrement")]


def test_actuate_delegates_to_actuator():
    act = _FakeActuator()
    MacOSPlatform(ax_tree=_FakeAX(), action_executor=act).actuate("click_at", {"x": 1, "y": 2})
    assert act.calls == [("click_at", {"x": 1, "y": 2})]


def test_every_abstract_verb_is_handled():
    for v in ABSTRACT_VERBS:
        assert v in _VERB_TO_AX or v == Verb.SET_VALUE, f"verb {v} has no native mapping"


def test_name_is_macos():
    assert MacOSPlatform(ax_tree=_FakeAX(), action_executor=_FakeActuator()).name == "macos"


def test_does_not_shadow_stdlib_platform():
    import platform  # must resolve to the stdlib, not anyuse/osplatform
    assert hasattr(platform, "system") and isinstance(platform.system(), str)


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
            f += 1; print("FAIL", n, "::", repr(e)[:200])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
