"""Typed compiled MCP outputs and bounded proof claims (offline)."""
import unittest

from anyuse.execution.api_step import RequestTemplate
from anyuse.execution.workflow_schema import json_schema_for_value
from anyuse.mcpserver.compiled_tools import output_schema, proof_scope


def no_empty_schema(value):
    if isinstance(value, dict):
        if value == {}:
            return False
        return all(no_empty_schema(child) for child in value.values())
    if isinstance(value, list):
        return all(no_empty_schema(child) for child in value)
    return True


class OutputSchemaTests(unittest.TestCase):
    def test_proved_field_names_and_primitive_types_are_preserved_without_values(self):
        template = RequestTemplate(
            url="https://example.test/detail", shape="fields",
            extract={"title": "item.title", "score": "item.score",
                     "active": "item.active", "tags": "item.tags"})
        observed = {"title": "Secret title", "score": 4.5,
                    "active": True, "tags": ["private", "labels"]}
        proof = {"cases": [{"contract": {
            "output_schema": json_schema_for_value(observed)}}]}
        schema = output_schema(template, proof)["properties"]["result"]
        self.assertEqual(schema["required"], ["title", "score", "active", "tags"])
        self.assertEqual(schema["properties"]["title"], {"type": "string"})
        self.assertEqual(schema["properties"]["score"], {"type": "number"})
        self.assertEqual(schema["properties"]["active"], {"type": "boolean"})
        self.assertEqual(schema["properties"]["tags"], {
            "type": "array", "items": {"type": "string"}})
        self.assertNotIn("Secret title", str(schema))
        self.assertTrue(no_empty_schema(schema))

    def test_legacy_list_records_have_a_typed_fallback_not_an_empty_item_schema(self):
        template = RequestTemplate(
            url="https://example.test/search", shape="list",
            extract={"items": "results[*].id"})
        items = output_schema(template)["properties"]["result"]["properties"]["items"]
        self.assertEqual(items["type"], "array")
        self.assertEqual(items["items"]["type"], ["string", "number", "boolean"])
        self.assertTrue(no_empty_schema(items))

    def test_tool_proof_scope_does_not_claim_list_completeness(self):
        listing = RequestTemplate(url="https://example.test/search", shape="list",
                                  extract={"items": "results[*].name"})
        fields = RequestTemplate(url="https://example.test/detail", shape="fields",
                                 extract={"name": "item.name", "score": "item.score"})
        self.assertEqual(
            proof_scope(listing, {"case_count": 2, "passed": True}),
            "List proof: >=80% of the first 10 UI labels, in UI order, on 2 held-out "
            "input(s) never used for fitting or selection; status proved; not a "
            "completeness guarantee.")
        checked = proof_scope(listing, {"case_count": 2, "passed": True,
                                        "status": "checked (selection not held out)"})
        self.assertIn("not held out", checked)
        self.assertNotIn("held-out input", checked)
        detail_scope = proof_scope(fields, {"case_count": 2})
        self.assertIn("named fields (name, score)", detail_scope)
        self.assertIn("not a completeness guarantee", detail_scope)


if __name__ == "__main__":
    unittest.main()
