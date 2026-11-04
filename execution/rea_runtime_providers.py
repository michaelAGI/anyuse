"""One explicit provider registry for reusable internal-runtime recipes.

Providers supply their own typed contracts, resource pins and observations.
The registry never launches a target, enables a debugger, changes permissions,
or retries a failed provider through another provider. Importing this module
does not import native runtimes or open inspector connections.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib


@dataclass(frozen=True)
class Provider:
    name: str
    module_name: str
    actor_kinds: frozenset[str]
    discovery_operations: frozenset[str]

    def module(self):
        return importlib.import_module(self.module_name, package=__package__)

    def validate_actor(self, actor, *, allow_bindings=True):
        if not isinstance(actor, dict) or actor.get("kind") not in self.actor_kinds:
            raise ValueError("actor does not belong to the selected runtime provider")
        return self.module().validate_actor(actor, allow_bindings=allow_bindings)

    def bind_actor(self, actor, inputs):
        self.validate_actor(actor, allow_bindings=True)
        return self.module().bind_actor(actor, inputs)

    def invocation_request(self, actor):
        self.validate_actor(actor, allow_bindings=False)
        return self.module().invocation_request(actor)

    def resources(self, actor):
        self.validate_actor(actor, allow_bindings=True)
        module = self.module()
        if self.name == "native":
            return module.native_resources(actor)
        return module.resources(actor)

    def validate_discovery(self, request):
        module = self.module()
        validated = module.validate_request(request)
        if any(operation.get("op") not in self.discovery_operations
               for operation in validated["operations"]):
            raise ValueError("discovery accepts provider metadata and graphs, not task invocation")
        return validated

    def collect(self, bundle, out, request, *, timeout=30):
        validated = self.validate_discovery(request)
        # An explicit provider is selected once. Failures remain that provider's
        # failures; there is no automatic alternate transport or launch path.
        return self.module().collect(bundle, out, validated, timeout=timeout)

    def transaction(self, bundle, out, request, *, timeout=30, expected_identity=None):
        """Explicit investigation; the selected provider validates every operation."""
        if expected_identity is not None and self.name != "native":
            raise ValueError("expected runtime process identity is supported only by the native provider")
        validated = self.module().validate_request(request)
        if expected_identity is not None:
            return self.module().collect(bundle, out, validated, timeout=timeout,
                                         expected_identity=expected_identity)
        return self.module().collect(bundle, out, validated, timeout=timeout)

    def invoke(self, bundle, actor, out, *, timeout=30):
        validated = self.validate_actor(actor, allow_bindings=False)
        return self.module().invoke(bundle, validated, out, timeout=timeout)


_PROVIDERS = (
    Provider("native", ".rea_runtime_bridge",
             frozenset({"objc_method", "objc_sequence", "native_function", "runtime_sequence", "qt_moc_method"}),
             frozenset({"classes", "methods", "ivars", "instances", "graph", "modules",
                        "symbols", "swift_types", "swift_fields", "memory_ranges", "memory_range",
                        "memory_bytes", "memory_pointer_range"})),
    Provider("inspector", ".rea_inspector_bridge",
             frozenset({"inspector_method"}),
             frozenset({"describe", "dom_query", "function_scopes", "graph", "map_keys", "method", "prototype_instances"})),
)
_BY_NAME = {provider.name: provider for provider in _PROVIDERS}
_BY_KIND = {kind: provider for provider in _PROVIDERS for kind in provider.actor_kinds}
if sum(len(provider.actor_kinds) for provider in _PROVIDERS) != len(_BY_KIND):
    raise RuntimeError("runtime actor kinds must have exactly one provider")


def for_name(name):
    if not isinstance(name, str) or name not in _BY_NAME:
        raise ValueError("select one registered runtime provider explicitly")
    return _BY_NAME[name]


def for_actor(actor):
    if not isinstance(actor, dict) or actor.get("kind") not in _BY_KIND:
        raise ValueError("unsupported internal-runtime actor")
    return _BY_KIND[actor["kind"]]


def actor_kinds():
    return frozenset(_BY_KIND)


def capabilities():
    """Declared shared primitives; these are not observed target eligibility."""
    return [{"provider": provider.name, "actor_kinds": sorted(provider.actor_kinds),
             "discovery_operations": sorted(provider.discovery_operations),
             "access_observed": False, "automatic_fallback": False}
            for provider in _PROVIDERS]


def agent_contract():
    """App-independent investigation requirements for the generic tool loop.

    These are descriptions of installed mechanisms, not target observations.
    Every submitted recipe still goes through its provider's actual validator.
    Keep this separate from the deliberately narrower full_native_v1 protocol.
    """
    common = ["current-session evidence", "fresh receiver resolution",
              "typed argument and return contract", "independent task observer",
              "confirmed runtime cleanup"]
    requirements = {
        "objc_method": ["measured Objective-C signature", "class or unique instance/getter recipe"],
        "objc_sequence": ["2..8 measured Objective-C methods", "declared ordered effects"],
        "native_function": ["pinned original module and callable instruction section",
                            "attributable ABI evidence", "declared argument ownership and execution context"],
        "runtime_sequence": ["2..8 admitted native/Objective-C calls", "declared ordered effects"],
        "qt_moc_method": ["original Qt revision-13 static catalog", "original dispatcher code and uint16 table",
                          "retained supported Qt source protocol", "constructor/destructor lifetime evidence",
                          "independently reviewed primary QObject base at offset zero"],
        "inspector_method": ["available explicitly enabled local inspector", "fresh literal receiver path",
                             "observed function identity", "declared typed JavaScript contract"],
    }
    return {
        "format": "anyuse.rea-runtime-agent-contract/1",
        "decision_protocol": "tool_object_v1",
        "providers": capabilities(),
        "families": [{"kind": kind, "provider": _BY_KIND[kind].name,
                      "requires": common + requirements[kind]}
                     for kind in sorted(_BY_KIND)],
        "instructions": [
            "Choose a provider explicitly for discover/runtime; use native or inspector.",
            "The generic object protocol can submit every registered actor family. full_native_v1 is a separate Objective-C-only wire format.",
            "Provider metadata and raw bytes are observations. A method name, address, readable pointer or decompilation alone does not establish a callable contract.",
            "Build static artifacts with static_evidence, using original bundle bytes. Generated artifacts remain investigation evidence; they confer no receiver, ABI, lifetime or task authority.",
            "Bind proposals to the frozen observer_profile_sha256 and cite current-session evidence. Never generate an observer from the candidate's reported result.",
            "Use the existing provider validators. Report the precise missing evidence or mechanism when a route cannot be admitted; never replace it with generated app code.",
        ],
        "access_observed": False,
        "proof_created": False,
    }
