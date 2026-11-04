"""Registered REA actions as an ordinary harness tool set."""
from . import harness_types as T


class ReaTools:
    toolset = "rea"

    def __init__(self, store, *, expected_catalog_sha256=None, expected_identity=None):
        self.store = store
        self._expected = {key: value for key, value in
                          (("expected_catalog_sha256", expected_catalog_sha256),
                           ("expected_identity", expected_identity)) if value is not None}
        self.last_result = None

    def observe(self):
        return T.Observation(kind="tool", rea_actions=self.store.list_tools())

    def target_label(self, action, obs):
        specs = [s for s in self.store.list_tools() if s["name"] == action.name]
        return specs[0]["description"] if len(specs) == 1 else action.name

    def effect_of(self, action, obs):
        # Proof of an effect is not a risk classification. Keep the normal
        # commit/approval gate for an operation without a declared risk class.
        return T.UNKNOWN

    def gate(self, action, obs):
        try:
            if sum(s["name"] == action.name for s in self.store.list_tools()) == 1:
                return "", None
            return "unknown or ambiguous registered REA action", T.Stop.REFUSED
        except (ValueError, OSError, RuntimeError) as exc:
            return str(exc), T.Stop.REFUSED

    def dispatch(self, action, obs):
        result = self.store.execute(action.name, action.inputs, **self._expected)
        self.last_result = result
        return T.Outcome(bool(result.get("ok")), str(result.get("reason") or result.get("status") or ""),
                         how="rea", data=result,
                         status="" if result.get("ok") else "rea_" + result.get("status", "uncertain"))
