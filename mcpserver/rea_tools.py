"""REA's MCP projection retains the native result and its verification tier."""
from . import meta_tools as M


def operations(specs):
    return [M.Operation(site="rea." + spec["registration"], name=spec["action"], kind="rea",
                        target=spec["name"], input_schema=spec["input_schema"],
                        row={"operation": spec["action"], "kind": "rea", "description": spec["description"],
                             "parameters": spec["input_schema"], "ready": True,
                             "verification_tier": spec["verification_tier"],
                             "contract": spec["contract"], "require_restoration": spec["require_restoration"],
                             "automatic_retry": False, "terminal_on_failure": True}) for spec in specs]
