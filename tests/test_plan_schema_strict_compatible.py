"""Every object node in the planner's
response-format schema must declare a closed ``properties``/
``additionalProperties: false`` — no bare ``{"type": "object"}`` anywhere,
recursively. ``execution/model_adapter.py`` sends every structured-output
request with ``"strict": True`` unconditionally, and a real strict-mode
provider (Cerebras) rejects a free-form object outright: "Object fields require at
least one of: 'properties' or 'anyOf'". This is a static, offline check —
no network, no model call — so it catches a regression the very next time
someone adds a field to ``_PLAN_SCHEMA``/``_COMMAND_SCHEMA`` without also
closing it.
"""
import pathlib
import sys
import tempfile

# Resolve `anyuse` to THIS checkout regardless of
# what a sibling worktree on PYTHONPATH happens to be named or contain.
_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINKDIR = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
_PKGROOT = pathlib.Path(_LINKDIR.name)
(_PKGROOT / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, str(_PKGROOT))

from anyuse.execution.planner import _PLAN_SCHEMA


def _walk_object_nodes(node, path="$"):
    """Yield (path, node) for every schema node reachable from ``node``."""
    if not isinstance(node, dict):
        return
    yield path, node
    props = node.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            yield from _walk_object_nodes(sub, f"{path}.{name}")
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_object_nodes(items, f"{path}[]")


def _is_object_type(node) -> bool:
    t = node.get("type")
    return t == "object" or (isinstance(t, list) and "object" in t)


def test_every_object_node_is_closed():
    offenders = []
    for path, node in _walk_object_nodes(_PLAN_SCHEMA):
        if not _is_object_type(node):
            continue
        if "properties" not in node and "anyOf" not in node:
            offenders.append((path, "no properties/anyOf"))
            continue
        if node.get("additionalProperties") is not False:
            offenders.append((path, "additionalProperties is not False"))
    assert not offenders, f"schema nodes incompatible with strict mode: {offenders}"


def test_command_schema_has_no_selector_field():
    # A freshly PLANNED step never has a prior recording to select by — that
    # field belongs only to the replay path (execution/deterministic.py's
    # impl_to_step), never to this schema.
    command_props = _PLAN_SCHEMA["properties"]["steps"]["items"]["properties"]["command"]["properties"]
    assert "selector" not in command_props


if __name__ == "__main__":
    test_every_object_node_is_closed()
    print("OK  every object node in _PLAN_SCHEMA is closed (strict-mode compatible)")
    test_command_schema_has_no_selector_field()
    print("OK  command schema carries no replay-only selector field")
