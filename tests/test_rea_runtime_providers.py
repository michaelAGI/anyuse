"""Provider boundaries and failure attribution, independent of native apps."""
import copy
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from anyuse.execution import rea_runtime_providers as P


class Providers(unittest.TestCase):
    def test_native_transaction_forwards_explicit_process_precondition_and_preserves_default_calls(self):
        collector = Mock(return_value={"status": "observed"})
        module = types.SimpleNamespace(validate_request=copy.deepcopy, collect=collector)
        request = {"operations": [{"op": "classes"}]}
        identity = {"fixture": "caller process precondition; B validates its full fields"}
        with patch.object(P.importlib, "import_module", return_value=module):
            P.for_name("native").transaction("/app", "/out", request)
            collector.assert_called_once_with("/app", "/out", request, timeout=30)
            collector.reset_mock()
            P.for_name("native").transaction("/app", "/out", request, timeout=7, expected_identity=identity)
            collector.assert_called_once_with("/app", "/out", request, timeout=7, expected_identity=identity)
        with patch.object(P.importlib, "import_module") as load:
            with self.assertRaisesRegex(ValueError, "only by the native"):
                P.for_name("inspector").transaction("/app", "/out", request, expected_identity=identity)
            load.assert_not_called()

    def test_qt_actor_is_native_only_and_declaration_never_claims_connected_access(self):
        with patch.object(P.importlib, "import_module") as load:
            provider = P.for_actor({"kind": "qt_moc_method"})
            self.assertEqual(provider.name, "native")
            self.assertNotIn("qt_moc_invoke", provider.discovery_operations)
            native = next(row for row in P.capabilities() if row["provider"] == "native")
            self.assertIn("qt_moc_method", native["actor_kinds"])
            self.assertFalse(native["access_observed"])
            self.assertFalse(native["automatic_fallback"])
            load.assert_not_called()

    def test_qt_action_is_not_discovery_and_native_failure_never_falls_back(self):
        collector = Mock()
        module = types.SimpleNamespace(validate_request=copy.deepcopy, collect=collector,
            validate_actor=lambda actor, **kwargs: copy.deepcopy(actor),
            invoke=Mock(side_effect=PermissionError("retained Qt native denial")))
        with patch.object(P.importlib, "import_module", return_value=module) as load:
            with self.assertRaisesRegex(ValueError, "not task invocation"):
                P.for_name("native").collect("/app", "/out", {"operations": [{"op": "qt_moc_invoke"}]})
            collector.assert_not_called()
            with self.assertRaisesRegex(PermissionError, "Qt native denial"):
                P.for_actor({"kind": "qt_moc_method"}).invoke("/app", {"kind": "qt_moc_method"}, "/out")
            self.assertTrue(all(call.args[0] == ".rea_runtime_bridge" for call in load.call_args_list))
            module.invoke.assert_called_once()

    def test_selected_provider_failure_does_not_open_other_provider(self):
        native = types.SimpleNamespace(
            validate_actor=lambda actor, **kw: actor,
            invoke=Mock(side_effect=PermissionError("actual native access denied")))
        inspector = types.SimpleNamespace(invoke=Mock())
        imported = []

        def load(name, **kwargs):
            imported.append(name)
            return native if name == ".rea_runtime_bridge" else inspector

        with patch.object(P.importlib, "import_module", side_effect=load):
            with self.assertRaisesRegex(PermissionError, "native access denied"):
                P.for_actor({"kind": "native_function"}).invoke("/app", {"kind": "native_function"}, "/out")
        self.assertEqual(imported, [".rea_runtime_bridge", ".rea_runtime_bridge"])
        inspector.invoke.assert_not_called()

    def test_unknown_provider_or_actor_has_no_import_or_effect(self):
        with patch.object(P.importlib, "import_module") as load:
            for name in ("auto", "frida-or-inspector", "", None):
                with self.assertRaises(ValueError):
                    P.for_name(name)
            for actor in ({"kind": "inspector_eval"}, {"kind": "shell"}, {}, None):
                with self.assertRaises(ValueError):
                    P.for_actor(actor)
            with self.assertRaises(ValueError):
                P.for_name("native").invoke("/app", {"kind": "inspector_method"}, "/out")
            load.assert_not_called()

    def test_discovery_uses_selected_schema_and_stops_call_before_collection(self):
        collector = Mock()
        module = types.SimpleNamespace(validate_request=lambda value: value, collect=collector)
        with patch.object(P.importlib, "import_module", return_value=module) as load:
            request = {"operations": [{"op": "describe", "receiver": {"root": "data"}}]}
            P.for_name("inspector").collect("/app", "/out", request)
            collector.assert_called_once_with("/app", "/out", request, timeout=30)
            collector.reset_mock()
            with self.assertRaisesRegex(ValueError, "not task invocation"):
                P.for_name("inspector").collect("/app", "/out", {"operations": [{"op": "call"}]})
            collector.assert_not_called()
            self.assertTrue(all(call.args[0] == ".rea_inspector_bridge" for call in load.call_args_list))

    def test_declared_capabilities_never_claim_connected_access(self):
        with patch.object(P.importlib, "import_module") as load:
            rows = P.capabilities()
        self.assertEqual({row["provider"] for row in rows}, {"native", "inspector"})
        inspector = next(row for row in rows if row["provider"] == "inspector")
        self.assertEqual(inspector["discovery_operations"], ["describe", "dom_query", "function_scopes", "graph", "map_keys", "method", "prototype_instances"])
        self.assertTrue(all(row["access_observed"] is False and row["automatic_fallback"] is False for row in rows))
        load.assert_not_called()

    def test_dom_query_forwards_only_the_explicit_inspector_data_request(self):
        request = {"operations": [{"op": "dom_query", "selector": "#app > .editor", "max_nodes": 8}]}
        frozen = copy.deepcopy(request)
        validator = Mock(side_effect=copy.deepcopy)
        collector = Mock(return_value={"status": "observed", "actuated": False})
        inspector = types.SimpleNamespace(validate_request=validator, collect=collector)
        with patch.object(P.importlib, "import_module", return_value=inspector) as load:
            observed = P.for_name("inspector").collect("/app", "/out", request, timeout=12)
        self.assertEqual(observed, {"status": "observed", "actuated": False})
        self.assertEqual(request, frozen)
        validator.assert_called_once_with(frozen)
        collector.assert_called_once_with("/app", "/out", frozen, timeout=12)
        self.assertIsNot(collector.call_args.args[2], request)
        self.assertTrue(all(call.args[0] == ".rea_inspector_bridge" for call in load.call_args_list))

    def test_dom_query_does_not_select_inspector_for_native_discovery(self):
        collector = Mock()
        module = types.SimpleNamespace(validate_request=copy.deepcopy, collect=collector)
        request = {"operations": [{"op": "dom_query", "selector": "#app", "max_nodes": 8}]}
        with patch.object(P.importlib, "import_module", return_value=module) as load:
            with self.assertRaisesRegex(ValueError, "not task invocation"):
                P.for_name("native").collect("/app", "/out", request)
        collector.assert_not_called()
        self.assertTrue(all(call.args[0] == ".rea_runtime_bridge" for call in load.call_args_list))

    def test_function_scopes_forwards_the_selected_inspector_request_without_proof(self):
        request = {"operations": [{"op": "function_scopes",
            "recipe": {"root": {"kind": "global", "name": "candidateFunction"}, "steps": []},
            "function_sha256": "a" * 64, "max_scopes": 4, "max_bindings": 8}]}
        frozen = copy.deepcopy(request)
        validator = Mock(side_effect=copy.deepcopy)
        receipt = {"status": "observed", "actuated": False,
                   "proof_created": False, "task_result_verified": False}
        collector = Mock(return_value=receipt)
        inspector = types.SimpleNamespace(validate_request=validator, collect=collector)
        with patch.object(P.importlib, "import_module", return_value=inspector) as load:
            observed = P.for_name("inspector").collect("/app", "/out", request, timeout=12)
        self.assertEqual(observed, receipt)
        self.assertEqual(request, frozen)
        validator.assert_called_once_with(frozen)
        collector.assert_called_once_with("/app", "/out", frozen, timeout=12)
        self.assertIsNot(collector.call_args.args[2], request)
        self.assertTrue(all(call.args[0] == ".rea_inspector_bridge" for call in load.call_args_list))
        self.assertIs(observed["proof_created"], False)
        self.assertIs(observed["task_result_verified"], False)

    def test_function_scopes_on_native_provider_is_refused_without_inspector_fallback(self):
        collector = Mock()
        module = types.SimpleNamespace(validate_request=copy.deepcopy, collect=collector)
        request = {"operations": [{"op": "function_scopes",
            "recipe": {"root": {"kind": "global", "name": "candidateFunction"}, "steps": []},
            "function_sha256": "a" * 64, "max_scopes": 4, "max_bindings": 8}]}
        with patch.object(P.importlib, "import_module", return_value=module) as load:
            with self.assertRaisesRegex(ValueError, "not task invocation"):
                P.for_name("native").collect("/app", "/out", request)
        collector.assert_not_called()
        self.assertTrue(all(call.args[0] == ".rea_runtime_bridge" for call in load.call_args_list))

    def test_prototype_instances_forwards_only_the_selected_inspector_heap_request(self):
        request = {"operations": [{"op": "prototype_instances",
            "recipe": {"root": {"kind": "global", "name": "candidateConstructor"},
                       "steps": [{"kind": "property", "name": "prototype", "access": "data"}]},
            "constructor_sha256": "a" * 64, "max_instances": 4, "max_properties": 8}]}
        frozen = copy.deepcopy(request)
        validator = Mock(side_effect=copy.deepcopy)
        receipt = {"status": "observed", "actuated": False,
                   "proof_created": False, "task_result_verified": False}
        collector = Mock(return_value=receipt)
        inspector = types.SimpleNamespace(validate_request=validator, collect=collector)
        with patch.object(P.importlib, "import_module", return_value=inspector) as load:
            observed = P.for_name("inspector").collect("/app", "/out", request, timeout=12)
        self.assertEqual(observed, receipt)
        self.assertEqual(request, frozen)
        validator.assert_called_once_with(frozen)
        collector.assert_called_once_with("/app", "/out", frozen, timeout=12)
        self.assertIsNot(collector.call_args.args[2], request)
        self.assertTrue(all(call.args[0] == ".rea_inspector_bridge" for call in load.call_args_list))
        self.assertIs(observed["proof_created"], False)
        self.assertIs(observed["task_result_verified"], False)

    def test_prototype_instances_native_discovery_refuses_before_heap_collection(self):
        collector = Mock()
        module = types.SimpleNamespace(validate_request=copy.deepcopy, collect=collector)
        request = {"operations": [{"op": "prototype_instances",
            "recipe": {"root": {"kind": "global", "name": "candidateConstructor"},
                       "steps": [{"kind": "property", "name": "prototype", "access": "data"}]},
            "constructor_sha256": "a" * 64, "max_instances": 4, "max_properties": 8}]}
        with patch.object(P.importlib, "import_module", return_value=module) as load:
            with self.assertRaisesRegex(ValueError, "not task invocation"):
                P.for_name("native").collect("/app", "/out", request)
        collector.assert_not_called()
        self.assertTrue(all(call.args[0] == ".rea_runtime_bridge" for call in load.call_args_list))


class InspectorDeclarations(unittest.TestCase):
    """Real declaration/provenance code over the existing fake CDP transport."""

    def setUp(self):
        import pytest
        from test_rea_inspector_bridge import environment
        from anyuse.execution import rea_inspector_bridge as I, rea_inspector_contract as C, rea_mapping as R
        self.I, self.C, self.R = I, C, R
        self.scratch = tempfile.TemporaryDirectory(prefix="inspector-declaration-test-")
        self.addCleanup(self.scratch.cleanup)
        self.mp = pytest.MonkeyPatch()
        self.addCleanup(self.mp.undo)
        self.env = environment.__wrapped__(Path(self.scratch.name).resolve(), self.mp)
        self.mp.setenv("ANYUSE_REA_INSPECTOR_CONFIG", str(self.env.config))
        self.root = Path(self.scratch.name).resolve() / "session"
        self.root.mkdir(mode=0o700)
        first = self.root / "s0000"; first.mkdir(mode=0o700)
        self.discovery = {"op": "discover", "provider": "inspector", "timeout": 10,
            "request": {"operations": [{"op": "method", "recipe": copy.deepcopy(self.env.actor["receiver"]),
                                          "method": self.env.actor["method"]}]}}
        self.receipt = I.collect(str(self.env.bundle), str(first / "runtime"), self.discovery["request"],
                                 timeout=10, config_file=str(self.env.config))
        self.assertEqual(self.receipt["status"], "observed", self.receipt.get("error"))
        row = {"step": 0, "request": copy.deepcopy(self.discovery), "status": "completed", "started": 1., "finished": 2.,
               "feedback": {"status": "observed", "provider": "inspector", "error": None,
                            "evidence_only": True, "proof_created": False}}
        self.state = {"status": "investigating", "bundle": str(self.env.bundle), "target": R.fingerprint(self.env.bundle),
            "task": "Set the requested task value", "params": [{"name": "value", "type": "string", "source": "text"}],
            "plan": {role: [{"inputs": {"value": str(i)}}] for i, role in enumerate(("learning", "verification", "heldout"))},
            "success_criteria": ["Independent output must match"], "steps": [row], "artifacts": {}, "frozen_files": {}}
        R.write_json(first / "request.json", self.discovery)
        R.write_json(first / "feedback.json", row)
        for name in ("request.json", "feedback.json", "runtime/inspector.json"):
            self.register("s0000/" + name)
        self.actor = {key: copy.deepcopy(value) for key, value in self.env.actor.items() if key != "signature_evidence"}
        self.actor["arguments"][0]["value"] = {"input": "value"}
        self.request = {"op": "declare_inspector", "actor": self.actor,
                        "citations": [{"artifact": "s0000/runtime/inspector.json", "pointer": "/operations/0"}]}
        self.folder = self.root / "s0001"; self.folder.mkdir(mode=0o700)
        self.state["steps"].append({"step": 1, "request": copy.deepcopy(self.request), "status": "running", "started": 3.})
        self.save_state()
        self.R.write_json(self.folder / "request.json", self.request)

    def register(self, name):
        file = self.root / name
        self.state["artifacts"][name] = {"file": str(file), "bytes": file.stat().st_size,
                                          "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}

    def save_state(self):
        self.R.write_json(self.root / "session.json", self.state)

    def complete(self):
        result = self.C.declare(str(self.root), self.state, self.request, str(self.folder))
        row = self.state["steps"][-1]
        row.update(status="completed", finished=4., feedback=result)
        self.R.write_json(self.folder / "feedback.json", row)
        for name in ("request.json", "feedback.json", "inspector-declaration.json"):
            self.register("s0001/" + name)
        self.save_state()
        return result["actor"]

    def rewrite_observation(self, change):
        change(self.receipt)
        self.R.write_json(self.root / "s0000/runtime/inspector.json", self.receipt)
        self.register("s0000/runtime/inspector.json")
        self.save_state()

    def test_real_declaration_keeps_model_types_distinct_and_never_invokes_or_proves(self):
        actor = self.complete()
        self.assertEqual(actor["signature_evidence"]["kind"], "declared_js")
        validated = self.C.validate_declaration(actor, state=self.state, current=True)
        self.assertEqual(validated["type_basis"], self.C.TYPE_BASIS)
        self.assertFalse(validated["independently_reviewed"])
        self.assertFalse(validated["proof_created"])
        self.assertEqual(validated["document"]["actor"], self.actor)
        self.assertEqual(validated["document"]["contract"], self.I.call_contract(actor))
        self.assertIn(str(self.folder / "inspector-declaration.json"), validated["resources"])
        self.assertIn(str(self.root / "s0000/runtime/inspector.json"), validated["resources"])
        self.assertNotIn(str(self.root / "session.json"), validated["resources"])
        self.assertEqual(self.env.server.dispatches, [])

    def test_declared_binding_can_replay_new_value_with_actual_provider_gate(self):
        actor = self.complete()
        bound = self.I.bind_actor(actor, {"value": "11"})
        resources = self.I.resources(actor)
        receipt = self.I.invoke(str(self.env.bundle), bound, str(self.root / "actual-fake-call"),
                                timeout=10, config_file=str(self.env.config))
        self.assertEqual(receipt["status"], "executed", receipt.get("error"))
        self.assertEqual(receipt["declaration_resources"], resources)
        self.assertEqual(self.env.server.dispatches, [[11, True]])
        self.assertTrue(self.I.receipt_valid(receipt, bound, receipt["target"], receipt["adapter"], receipt["transport"]))
        self.assertTrue(self.I.receipt_valid(receipt, bound, receipt["target"], receipt["adapter"], receipt["transport"], check_files=False))
        changed = copy.deepcopy(receipt); changed["declaration_resources"].pop(bound["signature_evidence"]["file"])
        self.assertFalse(self.I.receipt_valid(changed, bound, receipt["target"], receipt["adapter"], receipt["transport"], check_files=False))

    def visual_declaration(self, kind="utf8", *, extra_input=False):
        from anyuse.execution import rea_visual_effect as X
        self.actor["arguments"][0]["type"] = kind
        if extra_input:
            self.state["params"].append({"name": "other", "type": "string", "source": "text"})
        self.state["steps"][-1]["request"] = copy.deepcopy(self.request)
        self.R.write_json(self.folder / "request.json", self.request); self.save_state()
        actor = self.complete()
        policy = X.validate_policy({"format": X.POLICY, "id": "declared-value", "target": self.state["target"],
            "kind": "visible_state_transition", "params": self.state["params"], "observer": {"window_title": "Fixture"},
            "before": "The owned fixture is ready.", "after": "The requested input is visible.",
            "minimum_confidence": .8, "model": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1",
                "model": "test-visual", "api_key_env": "", "retries": 0, "timeout": 30, "max_tokens": 512}})
        return X, policy, actor

    def test_visual_declared_utf8_values_reopen_bound_resources_and_actual_runtime_evidence(self):
        X, policy, actor = self.visual_declaration()
        original = copy.deepcopy(actor)
        resources = self.I.resources(actor)
        for index, value in enumerate(("first literal", "different literal")):
            bound = X.bind_actor(actor, policy, {"value": value})
            self.assertEqual(bound["arguments"], [{"type": "utf8", "value": value}, {"type": "bool", "value": True}])
            # Saved-template validation remains exact; only the invocation
            # resource path permits the declared input's literal substitution.
            with self.assertRaisesRegex(ValueError, "input binding or constant"):
                self.I.resources(bound)
            actual_resources = X._provider_resources(bound, self.env.bundle)
            self.assertEqual(actual_resources["resources"], resources)
            folder = self.root / ("visual-call-" + str(index))
            receipt = self.I.invoke(str(self.env.bundle), bound, str(folder), timeout=10,
                config_file=str(self.env.config), expected_identity=self.env.main)
            self.assertEqual(receipt["status"], "executed", receipt.get("error"))
            self.assertEqual(receipt["declaration_resources"], resources)
            reader = X._Reader(); X._sources(reader)
            counts = X._runtime(reader, X._pin(folder / "inspector.json"), bound,
                self.env.main, self.state["target"], actual_resources)
            self.assertEqual(counts["method_dispatch_attempts"], 1)
            self.assertEqual(counts["getter_calls"], 0)
            config_file = actual_resources["transport"]["config"]["file"]
            self.assertEqual(reader.files[config_file]["domain"], "transport_config")
            self.assertEqual(reader.files[str(Path(self.I.__file__).resolve())]["domain"], "source")
            # The exact shared source/config dependencies are compatible;
            # declaring an ordinary trial artifact to be a resource is not.
            wrong_role = X._Reader()
            wrong_role.json(resources[actor["signature_evidence"]["file"]], "trial")
            with self.assertRaisesRegex(ValueError, "roles overlap"):
                X._runtime(wrong_role, X._pin(folder / "inspector.json"), bound,
                    self.env.main, self.state["target"], actual_resources)
        self.assertEqual(actor, original)
        self.assertEqual(self.env.server.dispatches, [["first literal", True], ["different literal", True]])

    def test_visual_declared_json_literal_input_key_is_not_rebound_as_a_template(self):
        X, policy, actor = self.visual_declaration("json")
        bound = X.bind_actor(actor, policy, {"value": '{"input":"literal data"}'})
        self.assertEqual(bound["arguments"][0], {"type": "json", "value": {"input": "literal data"}})
        self.assertEqual(X._provider_resources(bound, self.env.bundle)["resources"], self.I.resources(actor))
        receipt = self.I.invoke(str(self.env.bundle), bound, str(self.root / "visual-json"), timeout=10,
            config_file=str(self.env.config), expected_identity=self.env.main)
        self.assertEqual(receipt["status"], "executed", receipt.get("error"))
        self.assertEqual(self.env.server.dispatches, [[{"input": "literal data"}, True]])

    def test_full_visual_declared_trial_retains_template_through_final_validation(self):
        import httpx
        from test_rea_visual import PNG
        from anyuse.execution import rea_visual as V, rea_process_restart as RR, model_adapter as M
        X, policy, actor = self.visual_declaration()
        original = copy.deepcopy(actor); identity = copy.deepcopy(self.env.main)
        state = {"session": {"uid": identity["uid"], "on_console": True},
            "apps": [{**{key: identity[key] for key in ("bundle_id", "bundle", "pid", "launch_time")}, "terminated": False}],
            "frontmost_pid": identity["pid"], "windows": [{"id": 456, "pid": identity["pid"],
                "title": "Fixture", "layer": 0, "on_screen": True, "alpha": 1.,
                "bounds": {"X": 20., "Y": 10., "Width": 900., "Height": 700.}}]}
        captures, requests = [], []
        def inspect(bundle_id):
            self.assertEqual(bundle_id, identity["bundle_id"])
            return copy.deepcopy(state)
        def capture(argv, *, timeout=V.CAPTURE_TIMEOUT):
            captures.append(copy.deepcopy(argv)); Path(argv[-1]).write_bytes(PNG)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.mp.setattr(V, "Backend", lambda: types.SimpleNamespace(inspect=inspect, capture=capture))
        self.mp.setattr(X.F, "require_clear", lambda where: types.SimpleNamespace(allowed=True))
        kernel = {key: identity[key] for key in ("pid", "uid", "executable")}
        kernel.update(ppid=1, started_at="Tue Nov 14 22:13:20 2023")
        def psquery(pid, folder, deadline):
            self.assertEqual(pid, identity["pid"]); folder.mkdir(mode=0o700)
            query = {"test_kernel": True}; return query, X._save(folder / "receipt.json", query)
        self.mp.setattr(RR, "_ps_query", psquery)
        self.mp.setattr(RR, "_kernel", lambda *args, **kwargs: copy.deepcopy(kernel))
        self.mp.setattr(RR, "_bind_epoch", lambda *args: None)
        def response(request):
            requests.append(json.loads(request.content))
            verdict = {"result": "success", "is_success": True, "confidence": .95,
                "explanation": "The frozen visible effect is satisfied.", "evidence": ["The owned fixture shows the requested value."]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
        adapter = M.ModelAdapter(M.ModelConfig(**policy["model"]), transport=httpx.MockTransport(response), budget=M.RunBudget(0))
        mark = M.process_call_count()
        for index, value in enumerate(("first complete trial", "fresh complete trial")):
            intent = X.make_intent(str(self.env.bundle), policy, "declared-" + str(index), "learning", {"value": value},
                expected_identity=identity)
            folder = self.root / ("complete-visual-trial-" + str(index))
            result = X.run_trial(str(self.env.bundle), actor, policy, intent, str(folder),
                expected_identity=identity, adapter=adapter)
            self.assertEqual(result["status"], "model_judged", result["error"])
            self.assertEqual(result["model_calls"], 2); self.assertIs(result["actuated"], True)
            self.assertFalse(result["resource_review_required"])
            bound = X.bind_actor(actor, policy, {"value": value})
            journal = self.R.load_json(folder / "journal.json")
            self.assertEqual(journal["status"], "completed")
            self.assertEqual(journal["actor_sha256"], self.R.digest(bound))
            self.assertEqual(result["measurement"]["actor_sha256"], self.R.digest(bound))
            self.assertEqual(result["measurement"], X.validate_trial(policy, result["case"], actor))
            with self.assertRaisesRegex(ValueError, "input binding or constant"):
                X.validate_trial(policy, result["case"], bound)
            self.assertEqual(actor, original)
        self.assertEqual(M.process_call_count() - mark, 4)
        self.assertEqual(len(requests), 4); self.assertEqual(len(captures), 4)
        self.assertEqual(self.env.server.dispatches, [["first complete trial", True], ["fresh complete trial", True]])

    def test_visual_rejects_changed_saved_template_before_binding_or_dispatch(self):
        X, policy, actor = self.visual_declaration(extra_input=True)
        before = len(self.env.server.connections)
        for mutate in (lambda a: a["arguments"][0].update(value={"input": "other"}),
                       lambda a: a["arguments"][0].update(value="11"),
                       lambda a: a["arguments"][1].update(value=False),
                       lambda a: a["arguments"][0].update(type="number"),
                       lambda a: a.update(method="differentMethod"),
                       lambda a: a.update(function_sha256="b" * 64),
                       lambda a: a.update(receiver={"root": {"kind": "global", "name": "other"}, "steps": []}),
                       lambda a: a["signature_evidence"].update(sha256="b" * 64)):
            changed = copy.deepcopy(actor); mutate(changed)
            with self.subTest(actor=changed), patch.object(self.I, "bind_actor", wraps=self.I.bind_actor) as bind:
                with self.assertRaises(ValueError):
                    X.bind_actor(changed, policy, {"value": "11", "other": "11"})
                bind.assert_not_called()
        self.assertEqual(len(self.env.server.connections), before)
        self.assertEqual(self.env.server.dispatches, [])

    def test_complete_declared_inspector_coordinator_promotes_restarts_publishes_and_replays(self):
        import httpx
        from test_rea_visual import PNG
        from test_rea_process_restart import _kernel, _stdout
        from anyuse.execution import rea_agent as G, rea_campaign_coordinator as Q, rea_actions as A
        from anyuse.execution import rea_visual as V, rea_visual_campaign as C, rea_process_restart as RR
        from anyuse.execution import model_adapter as M, api_step
        X, policy, _ = self.visual_declaration()
        policy["input_constraints"] = {"value": {"kind": "decimal", "minimum": "0", "maximum": "10"}}
        policy = X.validate_policy(policy)
        profile = {"format": C.PROFILE, "id": "declared_pipeline", "label": "Set the visible fixture value",
            "effect": "visible_state_transition", "params": policy["params"], "policy": policy, "scope": C.SCOPE}
        plan = {"format": C.PLAN}
        for role, start in zip(G.ROLES, (0, 3, 6)):
            plan[role] = [{"id": role + "_" + str(i), "inputs": {"value": str(i)},
                "restart": role == "heldout" and i == start} for i in range(start, start + 3)]
        rea = self.root.parent / "inert-rea"; rea.write_text("never executed"); rea.chmod(0o700)
        root = self.root.parent / "complete-agent-session"
        G.begin(str(self.env.bundle), str(rea), "Set the requested visible fixture value", policy["params"],
            ["The visible value must match the frozen input"], plan, str(root), max_steps=64,
            budget_seconds=7200, observer_profile=profile)
        discovered = G.step(root, self.discovery)
        self.assertEqual(discovered["feedback"]["status"], "completed", discovered["feedback"])
        citation = {"artifact": "s0000/runtime/inspector.json", "pointer": "/operations/0"}
        declared = G.step(root, {"op": "declare_inspector", "actor": copy.deepcopy(self.actor), "citations": [citation]})
        self.assertEqual(declared["feedback"]["status"], "completed", declared["feedback"])
        actor = declared["feedback"]["feedback"]["actor"]
        original = copy.deepcopy(actor)
        self.assertTrue(Path(actor["signature_evidence"]["file"]).is_relative_to(root))
        observer = G.context(root)["observer_profile_sha256"]
        proposed = G.step(root, {"op": "propose", "observer_profile_sha256": observer, "actor": actor,
            "rationale": "The observed method and declared types remain a hypothesis until the full campaign.",
            "citations": [citation]})
        self.assertEqual(proposed["feedback"]["status"], "completed", proposed["feedback"])
        initial_identity = copy.deepcopy(self.env.main)
        running, captures, requests, hooks = [True], [], [], []
        def registrations(bundle_id):
            self.assertEqual(bundle_id, self.env.main["bundle_id"])
            return [{**{key: self.env.main[key] for key in ("pid", "bundle_id", "bundle", "executable", "launch_time")},
                "terminated": False}] if running[0] else []
        def inspect(bundle_id):
            return {"session": {"uid": os.getuid(), "on_console": True}, "apps": registrations(bundle_id),
                "frontmost_pid": self.env.main["pid"], "windows": [{"id": 456, "pid": self.env.main["pid"],
                    "title": "Fixture", "layer": 0, "on_screen": True, "alpha": 1.,
                    "bounds": {"X": 20., "Y": 10., "Width": 900., "Height": 700.}}]}
        def capture(argv, *, timeout=V.CAPTURE_TIMEOUT):
            captures.append(copy.deepcopy(argv)); Path(argv[-1]).write_bytes(PNG)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.mp.setattr(V, "Backend", lambda: types.SimpleNamespace(inspect=inspect, capture=capture))
        self.mp.setattr(X.F, "require_clear", lambda where: types.SimpleNamespace(allowed=True))
        def query(pid, folder, deadline):
            folder.mkdir(mode=0o700); started = time.monotonic()
            kernel = _kernel(self.env.main) if running[0] and pid == self.env.main["pid"] else None
            (folder / "stdout").write_bytes(_stdout(kernel)); (folder / "stderr").write_bytes(b"")
            row = {"format": RR.QUERY, "command": RR._argv(pid), "started_monotonic": started,
                "finished_monotonic": time.monotonic(), "timeout_seconds": min(2, deadline - started),
                "status": "returned", "owned": {"pid": 900001, "returncode": 0 if kernel else 1,
                    "reaped": True, "term_attempted": False, "kill_attempted": False, "errors": []},
                "stdout": RR._pin(folder / "stdout"), "stderr": RR._pin(folder / "stderr"), "error": None}
            return row, RR._save(folder / "receipt.json", row)
        self.mp.setattr(RR, "_ps_query", query)
        self.mp.setattr(RR, "_Mac", lambda: types.SimpleNamespace(registrations=registrations,
            query=query, tick=lambda deadline: None))
        server_response = self.env.server.response
        def current_processes(connection, message):
            result = server_response(connection, message)
            if message["method"] == "SystemInfo.getProcessInfo":
                result["processInfo"][0]["id"] = self.env.main["pid"]
            return result
        self.mp.setattr(self.env.server, "response", current_processes)
        def response(request):
            requests.append(json.loads(request.content))
            verdict = {"result": "success", "is_success": True, "confidence": .95,
                "explanation": "The frozen visible fixture condition is satisfied.", "evidence": ["The requested value is visible."]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
        self.mp.setattr(api_step, "import_module_httpx_transport", lambda: httpx.MockTransport(response))
        document = self.root.parent / "owned-fixture-document"; document.write_text("unchanged owned bytes")
        producer = self.root.parent / "ordinary-hook-producer"; producer.write_text("inert callback provenance")
        cfg = {"format": Q.VISUAL_CONFIG, "session": str(root), "target": self.state["target"],
            "plan_sha256": self.R.digest(plan), "observer_profile_sha256": observer, "owned": True,
            "expected_identity": initial_identity, "resources": [Q._pin(document)],
            "limits": {"seconds": 1800, "checkpoint_seconds": 300, "trial_seconds": 120, "max_actions": 32},
            "publication": {"summary": "Set a visible fixture value", "details": "Model-judged finite campaign with witnessed restart"},
            "selection_task": "Set the requested visible value", "replays": [
                {"id": "later_9", "inputs": {"value": "9"}}, {"id": "later_10", "inputs": {"value": "10"}}]}
        out = self.root.parent / "complete-coordinator"; mark = M.process_call_count()
        journal = Q.run_visual(root, cfg, out)
        checkpoints = 0
        while journal["status"] != "completed":
            self.assertIn(journal["status"], {"needs_setup", "needs_restart"},
                {"error": journal.get("error"), "steps": [(row["kind"], row["status"]) for row in journal["steps"]],
                 "last_G": G._load(root)["steps"][-1]})
            pending = journal["pending"]; checkpoints += 1
            self.assertLess(checkpoints, 16)
            started = time.monotonic(); restart = None
            if pending["kind"] == "restart":
                predecessor, measured = Q._visual_latest(G._load(root))
                old = copy.deepcopy(self.env.main)
                intent = {"format": RR.INTENT, "id": "ordinary_restart", "target": self.state["target"],
                    "old_identity": old, "stop": {"id": "normal_stop", "producer": RR._pin(producer)},
                    "launch": {"id": "normal_launch", "producer": RR._pin(producer)}}
                def stop(context):
                    hooks.append("stop"); running[0] = False
                    return {"normal_stop_completed": True}
                def launch(context):
                    hooks.append("launch"); self.env.main.update(pid=old["pid"] + 1, launch_time=time.time())
                    self.env.child.update(ppid=self.env.main["pid"], pid=self.env.child["pid"] + 1)
                    self.env.server.renderer_id = self.env.child["pid"]
                    running[0] = True
                    return {"application_pid": self.env.main["pid"], "result": {"normal_launch_completed": True}}
                restarted = RR.run_restart(str(self.env.bundle), old, intent, str(self.root.parent / "ordinary-restart"),
                    stop_hook=stop, launch_hook=launch)
                self.assertEqual(restarted["status"], "restart_measurement_validated", restarted)
                witness = self.R.load_json(restarted["measurement"]["file"])
                empty_outputs = [pin for pin in witness["files"].values() if pin["bytes"] == 0]
                self.assertTrue(empty_outputs, "The real restart closure must exercise empty ps outputs")
                with self.assertRaisesRegex(ValueError, "bounded exact resource pin"):
                    Q._verify(empty_outputs[0], self.I.B.MAX_NATIVE_MODULE_BYTES)
                restart = {"intent": RR._pin(self.root.parent / "ordinary-restart/intent.json"),
                    "receipt": restarted["receipt"], "predecessor": predecessor}
            evidence = X._save(self.root.parent / ("public-observation-" + str(checkpoints) + ".json"),
                {"owned_fixture_ready": True, "action": pending["action"]})
            checkpoint = {"format": Q.VISUAL_CHECKPOINT, "request_sha256": self.R.digest(pending),
                "kind": pending["kind"], "status": "ready", "identity": copy.deepcopy(self.env.main),
                "evidence": [evidence], "started_monotonic": started, "finished_monotonic": time.monotonic()}
            if restart is not None: checkpoint["restart"] = restart
            file = self.root.parent / ("public-checkpoint-" + str(checkpoints) + ".json"); self.R.write_json(file, checkpoint)
            journal = Q.run_visual(root, cfg, out, resume=True, checkpoint=file)
        state = G._load(root); candidate = self.R.load_json(state["candidate"]); proof = self.R.load_json(state["proof"])
        self.assertEqual(state["status"], "published"); self.assertEqual(state["hypothesis"]["actor"], original)
        self.assertEqual(proof["measurement"]["phase_counts"], {"learning": 3, "fresh": 3, "heldout": 3})
        self.assertTrue(proof["measurement"]["restart_deployment_proven"])
        self.assertEqual(journal["model_calls"], 22); self.assertEqual(journal["replay_index"], 2)
        self.assertEqual(M.process_call_count() - mark, 22); self.assertEqual(len(requests), 22)
        self.assertEqual(len(captures), 22); self.assertEqual(len(self.env.server.dispatches), 11)
        self.assertEqual(self.env.server.dispatches, [[str(i), True] for i in range(11)])
        self.assertEqual(hooks, ["stop", "launch"]); self.assertNotEqual(self.env.main, initial_identity)
        self.assertEqual(document.read_text(), "unchanged owned bytes")
        cat = self.R.load_json(journal["published"]["file"]); selection = self.R.load_json(journal["selection"]["file"])
        with self.assertRaises(ValueError):
            A.execute(cat, selection, {"value": "11"}, str(self.root.parent / "out-of-domain"),
                timeout=120, expected_identity=self.env.main)
        self.assertEqual(M.process_call_count() - mark, 22); self.assertEqual(len(self.env.server.dispatches), 11)
        # Exact declared method evidence may share its measurement dependency;
        # ordinary task cases, runtime receipts and images may not.
        case = self.R.load_json(candidate["learning_case_pins"][0]["case"]["file"])
        for pin in (candidate["learning_case_pins"][0]["case"], case["evidence"]["execution"],
                    self.R.load_json(case["evidence"]["before"]["file"])["image"]):
            reader = C._CampaignReader(); reader.read(pin, "measurement", retain=False)
            changed = copy.deepcopy(candidate); changed["evidence"] = [{"file": pin["file"], "sha256": pin["sha256"], "pointer": "/format"}]
            with self.assertRaisesRegex(ValueError, "unrelated task evidence"):
                C._citations(changed, reader)
        resource = self.I.resources(actor)[str(root / "s0000/runtime/inspector.json")]
        reader = C._CampaignReader(); reader.read(resource, "measurement", retain=False)
        changed = copy.deepcopy(candidate); changed["evidence"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "changed resource"):
            C._citations(changed, reader)
        original_config = self.env.config.read_bytes()
        try:
            self.env.config.write_bytes(original_config + b" ")
            with self.assertRaises(ValueError):
                A.execute(cat, selection, {"value": "9"}, str(self.root.parent / "changed-config"), timeout=120)
        finally:
            self.env.config.write_bytes(original_config)
        self.assertEqual(M.process_call_count() - mark, 22); self.assertEqual(len(self.env.server.dispatches), 11)

    def test_missing_or_other_method_cannot_supply_declaration(self):
        self.request["actor"]["method"] = "unobservedMethod"
        self.state["steps"][-1]["request"] = copy.deepcopy(self.request); self.save_state()
        with self.assertRaisesRegex(ValueError, "declared receiver recipe"):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))
        self.assertFalse((self.folder / "inspector-declaration.json").exists())
        self.assertEqual(self.env.server.dispatches, [])

    def test_missing_method_is_completed_bounded_metadata_with_no_callable_body(self):
        request = copy.deepcopy(self.discovery['request'])
        request['operations'][0]['method'] = 'absentLiteralMethod'
        receipt = self.I.collect(str(self.env.bundle), str(self.root / 'absent-method'), request,
            timeout=10, config_file=str(self.env.config), expected_identity=self.env.main)
        self.assertEqual(receipt['status'], 'observed', receipt.get('error'))
        self.assertIsNone(receipt.get('error'))
        self.assertFalse(receipt['resource_review_required']); self.assertFalse(receipt['state_uncertain'])
        self.assertIs(receipt['actuated'], False)
        self.assertEqual(receipt['identity'], receipt['after_identity'])
        self.assertEqual(receipt['expected_identity'], self.env.main)
        self.assertEqual(receipt['request'], request)
        self.assertEqual(receipt['request_sha256'], self.R.digest(request))
        self.assertRegex(receipt['endpoint_identity']['execution_context']['unique_id'], r'^context-[0-9]+$')
        self.assertEqual(receipt['method_dispatch_attempts'], 0); self.assertEqual(receipt['getter_dispatch_attempts'], 0)
        self.assertGreater(receipt['runtime_evaluation_attempts'], 0)
        self.assertEqual(receipt['operations'], [{'op': 'method', 'method': 'absentLiteralMethod', 'status': 'not_found',
            'lookup_scope': 'own_and_prototype_data_descriptors', 'prototype_depth': 3,
            'complete_prototype_chain': True, 'callable': False, 'getters_invoked': 0,
            'types_recovered_automatically': False}])
        self.assertEqual(receipt['cleanup'], {'object_group_released': True, 'page_socket_closed': True,
            'browser_socket_closed': True, 'requested_evaluations_completed': True,
            'remote_execution_cancelled': False, 'local_live_ids_discarded': True})
        self.assertEqual(self.env.server.dispatches, [])

    def test_method_absence_does_not_classify_runtime_protocol_context_or_cleanup_errors(self):
        original = self.env.server.response
        for index, fault in enumerate(('exception', 'command', 'malformed', 'proxy', 'context', 'epoch', 'release')):
            with self.subTest(fault=fault):
                self.env.server.fault = 'release_failure' if fault == 'release' else None
                epoch = self.env.main['launch_time']
                def response(connection, message):
                    if (fault == 'proxy' and message['method'] == 'Runtime.callFunctionOn' and
                            message['params']['functionDeclaration'] == self.I.PROPERTY and
                            message['params']['arguments'][0].get('value') == 'model'):
                        value = original(connection, message)
                        value['result']['subtype'] = 'proxy'
                        return value
                    if (message['method'] == 'Runtime.callFunctionOn' and
                            message['params']['functionDeclaration'] == self.I.METHOD_DISCOVERY):
                        if fault == 'exception': return {'exceptionDetails': {'text': 'property_not_found'}}
                        if fault == 'command': return {'__error__': True}
                        if fault == 'malformed': return {'result': {'type': 'object', 'value': {'status': 'not_found', 'prototype_depth': True}}}
                        if fault == 'context': connection.queue.append({'method': 'Runtime.executionContextDestroyed', 'params': {'executionContextId': 1}})
                        if fault == 'epoch': self.env.main['launch_time'] += 1
                    return original(connection, message)
                self.mp.setattr(self.env.server, 'response', response)
                request = copy.deepcopy(self.discovery['request']); request['operations'][0]['method'] = 'absentMethod'
                receipt = self.I.collect(str(self.env.bundle), str(self.root / ('method-error-' + str(index))), request,
                    timeout=10, config_file=str(self.env.config))
                self.assertEqual(receipt['status'], 'incomplete')
                self.assertTrue(receipt['resource_review_required'])
                self.assertEqual(receipt['method_dispatch_attempts'], 0); self.assertEqual(receipt['getter_dispatch_attempts'], 0)
                self.assertFalse(receipt['proof_created'])
                if fault != 'release': self.assertTrue(receipt['state_uncertain'])
                self.env.main['launch_time'] = epoch
        self.assertEqual(self.env.server.dispatches, [])

    def test_completed_method_absence_cannot_declare_a_function_contract(self):
        original = self.env.server.response
        def absent(connection, message):
            if (message['method'] == 'Runtime.callFunctionOn' and
                    message['params']['functionDeclaration'] == self.I.METHOD_DISCOVERY):
                return {'result': {'type': 'object', 'value': {'status': 'not_found', 'prototype_depth': 3}}}
            return original(connection, message)
        self.mp.setattr(self.env.server, 'response', absent)
        negative = self.I.collect(str(self.env.bundle), str(self.root / 'actual-negative'), self.discovery['request'],
            timeout=10, config_file=str(self.env.config))
        self.assertEqual(negative['status'], 'observed')
        self.rewrite_observation(lambda value: value.update(negative))
        with self.assertRaisesRegex(ValueError, 'actual inspector method body/hash'):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))
        self.assertFalse((self.folder / 'inspector-declaration.json').exists())
        self.assertEqual(self.env.server.dispatches, [])

    def test_unknown_input_is_rejected_before_any_current_identity_or_file_write(self):
        self.request["actor"]["arguments"][0]["value"] = {"input": "guessed_input"}
        self.state["steps"][-1]["request"] = copy.deepcopy(self.request); self.save_state()
        with patch.object(self.C, "_current") as current:
            with self.assertRaisesRegex(ValueError, "undeclared input"):
                self.C.declare(str(self.root), self.state, self.request, str(self.folder))
            current.assert_not_called()
        self.assertFalse((self.folder / "inspector-declaration.json").exists())

    def test_changed_raw_source_pin_is_not_treated_as_missing_type_information(self):
        file = self.root / "s0000/runtime/inspector.json"
        file.write_bytes(file.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "changed or is unavailable"):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))
        self.assertFalse((self.folder / "inspector-declaration.json").exists())

    def test_uncertain_cleanup_is_terminal_even_with_matching_function_body(self):
        self.rewrite_observation(lambda v: v["cleanup"].update(object_group_released=False))
        with self.assertRaisesRegex(ValueError, "cleanup is not confirmed"):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))
        self.assertEqual(self.env.server.dispatches, [])

    def test_other_process_method_evidence_cannot_declare_current_actor(self):
        self.rewrite_observation(lambda v: v["identity"].update(pid=11224))
        with self.assertRaisesRegex(ValueError, "target, process, adapter or request"):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))

    def test_foreign_registered_path_and_nonrow_citations_are_rejected(self):
        self.request["citations"][0]["pointer"] = "/operations/0/function_source"
        self.state["steps"][-1]["request"] = copy.deepcopy(self.request); self.save_state()
        with self.assertRaisesRegex(ValueError, "raw current-session"):
            self.C.declare(str(self.root), self.state, self.request, str(self.folder))

    def test_declared_constant_or_binding_cannot_be_replaced_after_admission(self):
        actor = self.complete()
        for mutate in (lambda a: a["arguments"][1].update(value=False),
                       lambda a: a["arguments"][0].update(value={"input": "other"}),
                       lambda a: a.update(receiver={"root": {"kind": "global", "name": "other"}, "steps": []})):
            changed = copy.deepcopy(actor); mutate(changed)
            with self.subTest(actor=changed), self.assertRaises(ValueError):
                self.I.resources(changed)
        self.assertEqual(self.env.server.dispatches, [])

    def test_bound_json_input_key_is_literal_data_but_saved_bindings_remain_exact(self):
        self.actor["arguments"][0]["type"] = "json"
        self.state["steps"][-1]["request"] = copy.deepcopy(self.request)
        self.R.write_json(self.folder / "request.json", self.request); self.save_state()
        actor = self.complete()
        changed = copy.deepcopy(actor); changed["arguments"][0]["value"] = {"input": "other"}
        with self.assertRaisesRegex(ValueError, "input binding"):
            self.I.resources(changed)
        bound = self.I.bind_actor(actor, {"value": '{"input":"other"}'})
        receipt = self.I.invoke(str(self.env.bundle), bound, str(self.root / "json-literal-call"),
                                timeout=10, config_file=str(self.env.config))
        self.assertEqual(receipt["status"], "executed", receipt.get("error"))
        self.assertEqual(self.env.server.dispatches, [[{"input": "other"}, True]])
        self.assertTrue(self.I.receipt_valid(receipt, bound, receipt["target"], receipt["adapter"], receipt["transport"]))

    def test_changed_observation_after_declaration_stops_before_inspector_connection(self):
        actor = self.complete(); bound = self.I.bind_actor(actor, {"value": "12"})
        file = self.root / "s0000/runtime/inspector.json"; file.write_bytes(file.read_bytes() + b" ")
        before = len(self.env.server.connections)
        with self.assertRaises(ValueError):
            self.I.invoke(str(self.env.bundle), bound, str(self.root / "no-call"), timeout=10, config_file=str(self.env.config))
        self.assertEqual(len(self.env.server.connections), before)
        self.assertEqual(self.env.server.dispatches, [])

    def test_historical_resources_survive_epoch_change_but_new_proposal_requires_current_epoch(self):
        actor = self.complete(); resources = self.I.resources(actor)
        self.mp.setattr(self.I.B, "process_identity", lambda *args: {**self.env.main, "pid": 11224})
        self.assertEqual(self.I.resources(actor), resources)
        with self.assertRaisesRegex(ValueError, "current process or transport changed"):
            self.C.validate_declaration(actor, current=True)

    def test_existing_reviewed_declarations_remain_single_resource(self):
        resources = self.I.resources(self.env.actor)
        self.assertEqual(set(resources), {str(self.env.declaration)})
        self.assertEqual(self.env.actor["signature_evidence"]["kind"], "reviewed_js")

    def test_explicit_expected_pid_and_epoch_refuse_before_any_inspector_connection(self):
        connections = len(self.env.server.connections)
        for index, change in enumerate(({"pid": self.env.main["pid"] + 1},
                                        {"launch_time": self.env.main["launch_time"] + 1})):
            expected = {**self.env.main, **change}
            result = self.I.invoke(str(self.env.bundle), self.env.actor, str(self.root / ('refused-%d' % index)),
                timeout=10, config_file=str(self.env.config), expected_identity=expected)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["expected_identity"], expected)
            self.assertIn("expected identity", result["error"]["message"])
            self.assertFalse(result["actuated"])
            self.assertFalse(result["state_uncertain"])
            self.assertEqual(result["method_dispatch_attempts"], 0)
            self.assertEqual(result["getter_dispatch_attempts"], 0)
            self.assertEqual(len(self.env.server.connections), connections)
        self.assertEqual(self.env.server.dispatches, [])

    def test_invalid_expected_identity_is_pure_preflight_and_ordinary_calls_omit_the_field(self):
        expected = {**self.env.main, 'pid': True}
        with patch.object(self.I.B, 'process_identity') as current:
            with self.assertRaises(ValueError):
                self.I.collect(str(self.env.bundle), str(self.root / 'invalid-pin'), self.discovery['request'],
                    config_file=str(self.env.config), expected_identity=expected)
            current.assert_not_called()
        self.assertNotIn('expected_identity', self.receipt)
        self.assertEqual(self.env.server.dispatches, [])

    def test_owned_identity_is_checked_again_immediately_before_task_call(self):
        observed = []
        def current(*args):
            observed.append(True)
            return copy.deepcopy(self.env.main) if len(observed) < 3 else {**self.env.main, 'pid': 11224}
        self.mp.setattr(self.I.B, 'process_identity', current)
        before = len(self.env.server.requests)
        result = self.I.invoke(str(self.env.bundle), self.env.actor, str(self.root / 'changed-before-call'),
            timeout=10, config_file=str(self.env.config), expected_identity=self.env.main)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('immediately before', result['error']['message'])
        self.assertEqual(result['method_dispatch_attempts'], 0)
        self.assertEqual(result['getter_dispatch_attempts'], 0)
        self.assertFalse(result['state_uncertain'])
        self.assertFalse(any(r['method'] in {'Runtime.evaluate', 'Runtime.callFunctionOn'}
                             for r in self.env.server.requests[before:]))
        self.assertEqual(self.env.server.dispatches, [])

    def test_expected_identity_is_retained_and_consumed_by_the_receipt_validator(self):
        result = self.I.invoke(str(self.env.bundle), self.env.actor, str(self.root / 'identity-bound'),
            timeout=10, config_file=str(self.env.config), expected_identity=self.env.main)
        self.assertEqual(result['status'], 'executed', result.get('error'))
        self.assertEqual(result['expected_identity'], self.env.main)
        self.assertTrue(self.I.receipt_valid(result, self.env.actor, result['target'], result['adapter'], result['transport']))
        changed = copy.deepcopy(result); changed['expected_identity']['launch_time'] += 1
        self.assertFalse(self.I.receipt_valid(changed, self.env.actor, result['target'], result['adapter'], result['transport'], check_files=False))

    def test_descriptor_paging_arguments_cross_only_as_literal_values(self):
        request = {'operations': [{'op': 'describe', 'recipe': copy.deepcopy(self.actor['receiver']),
            'max_properties': 3, 'prototype_depth': 0, 'offset': 40, 'property_query': 'Zoom'}]}
        receipt = self.I.collect(str(self.env.bundle), str(self.root / 'page-wire'), request,
            timeout=10, config_file=str(self.env.config))
        self.assertEqual(receipt['status'], 'observed', receipt.get('error'))
        describe = [r for r in self.env.server.requests if r['method'] == 'Runtime.callFunctionOn'
                    and r['params']['functionDeclaration'] == self.I.DESCRIBE]
        self.assertEqual([v['value'] for v in describe[-1]['params']['arguments']], [3, 0, 40, 'Zoom'])
        self.assertEqual(receipt['method_dispatch_attempts'], 0)
        self.assertEqual(receipt['getter_dispatch_attempts'], 0)
        self.assertEqual(self.env.server.dispatches, [])

    def test_descriptor_paging_bad_bounds_or_unknown_keys_stop_before_transport(self):
        base = {'op': 'describe', 'recipe': copy.deepcopy(self.actor['receiver']), 'max_properties': 4}
        changes = [{'offset': v} for v in (-1, 8193, True, 1.0, '0')]
        changes += [{'property_query': v} for v in (None, True, 5, '\x00', 'x' * 257, '🚀' * 65)]
        changes += [{'script': 'not executable input'}, {'offset': 0, 'unknown': 1}]
        with patch.object(self.I, '_http_json') as http, patch.object(self.I.B, 'process_identity') as identity:
            for index, change in enumerate(changes):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    self.I.collect(str(self.env.bundle), str(self.root / ('bad-page-%d' % index)),
                        {'operations': [{**base, **change}]}, config_file=str(self.env.config))
            http.assert_not_called(); identity.assert_not_called()
        self.assertEqual(self.env.server.dispatches, [])


class InspectorDescriptorV8(unittest.TestCase):
    def test_fixed_method_lookup_distinguishes_absence_without_getter_or_method_execution(self):
        from anyuse.execution import rea_inspector_bridge as I
        node = os.environ.get('ANYUSE_TEST_NODE') or shutil.which('node')
        if not node:
            self.skipTest('an existing Node executable enables the controlled fixed-script behavioral check')
        fixture = r'''
const fs=require('fs'),assert=require('assert');
const F=eval('('+JSON.parse(fs.readFileSync(0,'utf8')).method+')');
let getterCalls=0,methodCalls=0;
const parent=Object.create(null);parent.applyValue=function applyValue(){methodCalls++;};
const receiver=Object.create(parent);
Object.defineProperty(receiver,'accessor',{get(){getterCalls++;throw Error('getter executed');}});
assert.deepStrictEqual(F.call(receiver,'missing'),{status:'not_found',prototype_depth:2});
const found=F.call(receiver,'applyValue');assert.equal(found.status,'found');
assert.equal(found.function_source,Function.prototype.toString.call(parent.applyValue));
assert.throws(()=>F.call(receiver,'accessor'),/method_accessor_unsupported/);
receiver.notFunction=3;assert.throws(()=>F.call(receiver,'notFunction'),/not_function/);
const text="x'); globalThis.changed = true; ('";
assert.equal(F.call(receiver,text).status,'not_found');assert.equal(globalThis.changed,undefined);
let deep=Object.create(null);for(let i=0;i<16;i++)deep=Object.create(deep);
assert.throws(()=>F.call(deep,'absent'),/method_discovery_prototype_budget/);
const proxy=new Proxy({}, {getOwnPropertyDescriptor(){throw Error('proxy descriptor failure');}});
assert.throws(()=>F.call(proxy,'missing'),/proxy descriptor failure/);
assert.equal(getterCalls,0);assert.equal(methodCalls,0);
console.log('fixed method absence, bounded prototype lookup, getter/task isolation: passed');
'''
        node_env = {key: value for key, value in os.environ.items() if key not in {'NODE_OPTIONS', 'NODE_PATH'}}
        result = subprocess.run([node, '-e', fixture], input=json.dumps({'method': I.METHOD_DISCOVERY}),
            text=True, capture_output=True, timeout=15, env=node_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('passed', result.stdout)

    def test_actual_fixed_script_pages_large_namespaces_without_invoking_getters(self):
        from anyuse.execution import rea_inspector_bridge as I
        node = os.environ.get('ANYUSE_TEST_NODE') or shutil.which('node')
        if not node:
            self.skipTest('an existing Node executable enables the controlled fixed-script behavioral check')
        fixture = r'''
const fs = require('fs'), assert = require('assert');
const F = eval('(' + JSON.parse(fs.readFileSync(0, 'utf8')).describe + ')');
const target = Object.create(null); let getterCalls = 0;
for (let i = 0; i < 70; i++) target['browserBuiltin' + i] = i;
target.ZoomController = {value: 1};
Object.defineProperty(target, 'expensiveAccessor', {get() { getterCalls++; return 1; }, configurable: true});
target['.*'] = 'literal';
const injection = "x'); globalThis.changed = true; ('"; target[injection] = 4;
const old = F.call(target, 32, 0);
assert.deepStrictEqual(Object.keys(old).sort(), ['properties','property_count','symbol_key_count',
 'symbol_keys_omitted','truncated','whole_graph_coverage','getters_invoked','prototype_traversed'].sort());
assert.equal(old.properties.length, 32); assert.equal(old.truncated, true);
assert(!old.properties.some(x => x.name === 'ZoomController'));
const page = F.call(target, 4, 0, 68, '');
assert.deepStrictEqual(page.properties.map(x => x.name), ['browserBuiltin68','browserBuiltin69','ZoomController','expensiveAccessor']);
assert.equal(page.total, 74); assert.equal(page.filtered_count, 74); assert.equal(page.next_offset, 72);
assert.equal(page.whole_graph_coverage, false); assert.equal(page.getters_invoked, 0);
const last = F.call(target, 4, 0, page.next_offset, '');
assert.equal(last.next_offset, null); assert.equal(last.pagination_limited, false); assert.equal(last.truncated, true);
const query = F.call(target, 32, 0, 0, 'zOoM');
assert.equal(query.filtered_count, 1); assert.equal(query.properties[0].name, 'ZoomController');
const accessor = F.call(target, 4, 0, 0, 'accessor');
assert.equal(accessor.properties[0].kind, 'accessor'); assert.equal(accessor.properties[0].getter, true);
assert.equal(getterCalls, 0);
assert.deepStrictEqual(F.call(target, 4, 0, 0, '.*').properties.map(x => x.name), ['.*']);
assert.deepStrictEqual(F.call(target, 4, 0, 0, injection).properties.map(x => x.name), [injection]);
assert.equal(globalThis.changed, undefined);
const parent = Object.create(null); parent.inheritedModel = 3;
const child = Object.create(parent); child.ownModel = 2;
const chain = F.call(child, 1, 1, 0, 'model');
assert.equal(chain.filtered_count, 2); assert.equal(chain.next_offset, 1);
assert.equal(F.call(child, 1, 1, 1, 'model').properties[0].owner_depth, 1);
const many = Object.create(null); for(let i=0;i<8256;i++) many['k'+i]=i;
const limited = F.call(many, 32, 0, 8192, '');
assert.equal(limited.properties.length,32); assert.equal(limited.filtered_count,8256);
assert.equal(limited.next_offset,null); assert.equal(limited.pagination_limited,true);
assert.equal(limited.whole_graph_coverage,false); assert.equal(getterCalls,0);
const large = Object.create(null);
large.largeConstructor = new Function('/*' + 'x'.repeat(70000) + '*/ return 1;');
const named = F.call(large, 4, 0, 0, 'constructor');
assert.equal(named.filtered_count,1); assert.equal(named.properties[0].function_source_omitted,true);
assert(named.properties[0].function_source_characters > 65536);
assert(!('function_source' in named.properties[0]));
assert.throws(() => F.call(large,4,0), /function_source_budget/);
console.log('fixed descriptor paging, literal query, legacy shape, getter isolation: passed');
'''
        node_env = {key: value for key, value in os.environ.items() if key not in {'NODE_OPTIONS', 'NODE_PATH'}}
        result = subprocess.run([node, '-e', fixture], input=json.dumps({'describe': I.DESCRIBE}),
            text=True, capture_output=True, timeout=15, env=node_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('passed', result.stdout)


class InspectorDOMRootPreflight(unittest.TestCase):
    """Provider contract over the existing inert CDP transport fixture."""

    def setUp(self):
        import pytest
        from test_rea_inspector_bridge import environment
        from anyuse.execution import rea_inspector_bridge as I
        self.I = I
        self.scratch = tempfile.TemporaryDirectory(prefix="inspector-dom-root-test-")
        self.addCleanup(self.scratch.cleanup)
        self.mp = pytest.MonkeyPatch(); self.addCleanup(self.mp.undo)
        self.env = environment.__wrapped__(Path(self.scratch.name).resolve(), self.mp)

    def request(self, selector):
        return {"operations": [{"op": "describe", "recipe": {"root": {"kind": "dom", "selector": selector},
                                 "steps": []}, "max_properties": 8}]}

    def collect(self, name, request):
        return self.I.collect(self.env.bundle, Path(self.scratch.name).resolve() / name, request,
                              timeout=10, config_file=self.env.config)

    def test_zero_and_duplicate_roots_are_typed_closed_zero_runtime_preflight(self):
        for index, (selector, count) in enumerate((("#absent", 0), ("#app", 2))):
            with self.subTest(selector=selector):
                receipt = self.collect("root-" + str(index), self.request(selector))
                self.assertEqual(receipt["status"], "incomplete")
                self.assertFalse(receipt["state_uncertain"]); self.assertFalse(receipt["resource_review_required"])
                self.assertIs(receipt["actuated"], False)
                self.assertEqual(receipt["runtime_evaluation_attempts"], 0)
                self.assertEqual(receipt["getter_dispatch_attempts"], 0); self.assertEqual(receipt["method_dispatch_attempts"], 0)
                self.assertEqual(receipt["error"]["type"], "DOMRootNotUnique")
                self.assertEqual(receipt["preflight_failure"]["match_count"], count)
                self.assertTrue(receipt["preflight_failure"]["context_revalidated"])
                self.assertEqual(receipt["after_identity"], self.env.main)
                self.assertTrue(receipt["cleanup"]["requested_evaluations_completed"])
                for key in ("object_group_released", "page_socket_closed", "browser_socket_closed", "local_live_ids_discarded"):
                    self.assertTrue(receipt["cleanup"][key])
                self.assertFalse(receipt["proof_created"]); self.assertFalse(receipt["task_result_verified"])
                self.assertNotIn('"nodeId"', json.dumps(receipt)); self.assertNotIn('"objectId"', json.dumps(receipt))
        self.assertFalse(any(row["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn", "DOM.resolveNode"}
                             for row in self.env.server.requests))
        self.assertEqual(self.env.server.dispatches, [])
        self.assertTrue(all(connection.closed for connection in self.env.server.connections))

    def test_unique_dom_root_uses_bound_context_and_group_without_global_evaluation(self):
        receipt = self.collect("unique", self.request("body > #app"))
        self.assertEqual(receipt["status"], "observed", receipt.get("error"))
        self.assertFalse(receipt["state_uncertain"]); self.assertEqual(receipt["runtime_evaluation_attempts"], 1)
        self.assertFalse(any(row["method"] == "Runtime.evaluate" for row in self.env.server.requests))
        self.assertFalse(any(row["method"] == "Runtime.callFunctionOn" and row["params"]["functionDeclaration"] == self.I.DOM_ROOT
                             for row in self.env.server.requests))
        resolved = [row["params"] for row in self.env.server.requests if row["method"] == "DOM.resolveNode"]
        released = [row["params"]["objectGroup"] for row in self.env.server.requests if row["method"] == "Runtime.releaseObjectGroup"]
        self.assertEqual(len(resolved), 1); self.assertEqual(resolved[0]["nodeId"], 7101)
        self.assertEqual(resolved[0]["executionContextId"], 1)
        self.assertEqual([resolved[0]["objectGroup"]], released)
        self.assertEqual(self.env.server.dispatches, [])

    def test_transport_resolution_context_and_release_failures_remain_uncertain(self):
        for index, failure in enumerate(("protocol", "resolve", "resolve_wrapper", "resolve_context", "release")):
            with self.subTest(failure=failure):
                self.env.server.fault = "release_failure" if failure == "release" else None
                self.env.server.dom_fault = failure if failure != "release" else None
                before = len(self.env.server.requests)
                receipt = self.collect("failed-" + str(index), self.request("#absent" if failure == "release" else "body > #app"))
                self.assertEqual(receipt["status"], "incomplete")
                self.assertTrue(receipt["state_uncertain"]); self.assertTrue(receipt["resource_review_required"])
                self.assertIsNone(receipt["actuated"]); self.assertEqual(receipt["runtime_evaluation_attempts"], 0)
                self.assertEqual(receipt["method_dispatch_attempts"], 0); self.assertEqual(receipt["getter_dispatch_attempts"], 0)
                self.assertFalse(any(row["method"] in {"Runtime.evaluate", "Runtime.callFunctionOn"}
                                     for row in self.env.server.requests[before:]))
        self.assertEqual(self.env.server.dispatches, [])

    def test_later_ambiguity_does_not_erase_prior_evaluation_uncertainty(self):
        request = {"operations": self.request("body > #app")["operations"] + self.request("#absent")["operations"]}
        receipt = self.collect("prior-evaluation", request)
        self.assertTrue(receipt["state_uncertain"]); self.assertTrue(receipt["resource_review_required"])
        self.assertEqual(receipt["runtime_evaluation_attempts"], 1)
        self.assertEqual(len(receipt["operations"]), 1)
        self.assertEqual(receipt["preflight_failure"]["operation_index"], 1)
        self.assertFalse(receipt["preflight_failure"]["context_revalidated"])

    def test_known_count_does_not_admit_changed_owned_epoch(self):
        original = self.env.server.response
        def changed(connection, message):
            value = original(connection, message)
            if message["method"] == "DOM.querySelectorAll":
                self.env.main["launch_time"] += 1
            return value
        self.mp.setattr(self.env.server, "response", changed)
        receipt = self.collect("changed-epoch", self.request("#absent"))
        self.assertTrue(receipt["state_uncertain"]); self.assertTrue(receipt["resource_review_required"])
        self.assertFalse(receipt["preflight_failure"]["context_revalidated"])
        self.assertIn("preflight_revalidation_error", receipt)
        self.assertEqual(receipt["runtime_evaluation_attempts"], 0)
        self.assertEqual(self.env.server.dispatches, [])


class InspectorDOMEventStreaming(unittest.TestCase):
    """Real CDP dispatcher over controlled notifications, never real sockets."""

    class Socket:
        def __init__(self, plans, clock=None):
            self.plans = iter(plans)
            self.queue = deque()
            self.sent = []
            self.clock = clock
            self.closed = False

        def send(self, raw):
            request = json.loads(raw)
            self.sent.append(request)
            self.queue.extend(next(self.plans, []))
            self.queue.append({'id': request['id'], 'result': {}})

        def recv(self, timeout):
            if self.clock is not None:
                self.clock[0] += .1
            item = self.queue.popleft()
            if isinstance(item, BaseException):
                raise item
            return json.dumps(item, ensure_ascii=False)

        def close(self):
            self.closed = True

    @staticmethod
    def event(payload='NODE_TREE_MUST_NOT_ENTER_RECEIPTS'):
        return {'method': 'DOM.setChildNodes', 'params': {'parentId': 6001,
            'nodes': [{'nodeId': 7101, 'nodeType': 1, 'nodeName': 'DIV', 'payload': payload}]}}

    def test_more_than_256_dom_notifications_do_not_consume_retained_or_nonstream_budget(self):
        from anyuse.execution import rea_inspector_bridge as I
        socket = self.Socket([[self.event()] * 300])
        channel = I._CDP(socket, time.monotonic() + 10)
        self.assertEqual(channel.call('DOM.getDocument'), {})
        summary = channel.event_summary()
        self.assertEqual(summary['received_count'], 300)
        self.assertEqual(summary['streamed']['count'], 300)
        self.assertEqual(summary['by_method']['DOM.setChildNodes']['received'], 300)
        self.assertEqual(summary['retained_count'], 0); self.assertEqual(channel.events, [])
        self.assertFalse(summary['raw_payloads_in_summary']); self.assertFalse(summary['streamed']['payload_retained'])
        self.assertNotIn('NODE_TREE_MUST_NOT_ENTER_RECEIPTS', json.dumps(summary))
        self.assertEqual(channel.pending, set())

    def test_event_count_and_utf8_byte_limits_accumulate_across_calls(self):
        from anyuse.execution import rea_inspector_bridge as I
        first = I.MAX_TOTAL_EVENTS - 1
        socket = self.Socket([[self.event()] * first, [self.event()] * 2])
        channel = I._CDP(socket, time.monotonic() + 10)
        channel.call('DOM.getDocument')
        with self.assertRaisesRegex(I.ProtocolError, 'total event budget'):
            channel.call('DOM.querySelectorAll')
        summary = channel.event_summary()
        self.assertEqual(summary['received_count'], I.MAX_TOTAL_EVENTS + 1)
        self.assertEqual(summary['streamed']['count'], I.MAX_TOTAL_EVENTS)
        self.assertTrue(summary['total_budget_exceeded']); self.assertEqual(summary['retained_count'], 0)
        # Seventeen MiB of UTF-8 takes fewer events than the count bound. Counting
        # Python characters instead of actual wire bytes would miss this limit.
        large = self.event('é' * (I.MAX_TOTAL_EVENT_BYTES // 32))
        socket = self.Socket([[large] * 8, [large] * 8])
        channel = I._CDP(socket, time.monotonic() + 10)
        channel.call('DOM.getDocument')
        with self.assertRaisesRegex(I.ProtocolError, 'total event budget'):
            channel.call('DOM.querySelectorAll')
        summary = channel.event_summary()
        self.assertGreater(summary['received_utf8_bytes'], I.MAX_TOTAL_EVENT_BYTES)
        self.assertLess(summary['received_count'], I.MAX_TOTAL_EVENTS)
        self.assertTrue(summary['total_budget_exceeded']); self.assertEqual(channel.events, [])

    def test_destroy_clear_replace_and_detach_after_stream_invalidate_before_another_request(self):
        from anyuse.execution import rea_inspector_bridge as I
        events = [
            {'method': 'Runtime.executionContextDestroyed', 'params': {'executionContextId': 1}},
            {'method': 'Runtime.executionContextsCleared', 'params': {}},
            {'method': 'Inspector.detached', 'params': {'reason': 'controlled fixture'}},
            {'method': 'Runtime.executionContextCreated', 'params': {'context': {'id': 2, 'uniqueId': 'replacement',
                'auxData': {'isDefault': True, 'frameId': 'owned-frame'}}}},
        ]
        for event in events:
            with self.subTest(event=event['method']):
                socket = self.Socket([[self.event()] * 300 + [event]])
                channel = I._CDP(socket, time.monotonic() + 10)
                channel.bound_context = {'unique_id': 'original', 'frame_id': 'owned-frame'}
                with self.assertRaises(I.ProtocolError): channel.call('DOM.getDocument')
                self.assertTrue(channel.invalid_context)
                summary = channel.event_summary()
                self.assertEqual(summary['received_count'], 301)
                self.assertEqual(summary['by_method'][event['method']]['received'], 1)
                self.assertEqual(summary['streamed']['count'], 300)
                with self.assertRaisesRegex(I.ProtocolError, 'live IDs are expired'):
                    channel.call('DOM.querySelectorAll')
                self.assertEqual(len(socket.sent), 1)
                # Cleanup can consume the old reply but cannot clear invalidation.
                channel.call('Runtime.releaseObjectGroup', cleanup=True)
                self.assertTrue(channel.invalid_context)

    def test_unknown_document_updated_and_malformed_dom_notifications_keep_old_retained_limit(self):
        from anyuse.execution import rea_inspector_bridge as I
        for name in ('Custom.unusedEvent', 'DOM.documentUpdated', 'DOM.setChildNodes'):
            with self.subTest(method=name):
                event = {'method': name, 'params': {'unsupported_payload': True}}
                channel = I._CDP(self.Socket([[event] * 129]), time.monotonic() + 10)
                with self.assertRaisesRegex(I.ProtocolError, 'inspector event budget exceeded'):
                    channel.call('DOM.getDocument')
                summary = channel.event_summary()
                self.assertEqual(summary['retained_count'], 128); self.assertEqual(summary['streamed']['count'], 0)
                self.assertTrue(summary['retained_budget_exceeded'])
                self.assertEqual(summary['received_count'], 129)

    def test_method_counter_names_and_keys_are_bounded_without_ignoring_unknown_events(self):
        from anyuse.execution import rea_inspector_bridge as I
        events = [{'method': 'Custom.event' + str(i), 'params': {}} for i in range(129)]
        channel = I._CDP(self.Socket([events]), time.monotonic() + 10)
        with self.assertRaises(I.ProtocolError): channel.call('DOM.getDocument')
        summary = channel.event_summary()
        self.assertLessEqual(len(summary['by_method']), I.MAX_EVENT_METHOD_COUNTERS)
        self.assertEqual(summary['unindexed_methods']['received'], 1)
        oversized_name = 'Custom.' + 'x' * I.MAX_EVENT_METHOD_BYTES
        channel = I._CDP(self.Socket([[{'method': oversized_name, 'params': {}}]]), time.monotonic() + 10)
        self.assertEqual(channel.call('DOM.getDocument'), {})
        self.assertEqual(len(channel.events), 1)
        self.assertNotIn(oversized_name, json.dumps(channel.event_summary()))
        self.assertEqual(channel.event_summary()['unindexed_methods']['received'], 1)

    def test_deadline_and_late_reply_cleanup_remain_bounded_during_stream(self):
        from anyuse.execution import rea_inspector_bridge as I
        clock = [0.]
        socket = self.Socket([[self.event()] * 300], clock=clock)
        with patch.object(I.time, 'monotonic', side_effect=lambda: clock[0]):
            channel = I._CDP(socket, .5)
            with self.assertRaisesRegex(TimeoutError, 'deadline exceeded'): channel.call('DOM.getDocument')
        self.assertLess(channel.streamed_event_count, 300)
        socket = self.Socket([[self.event()] * 300 + [TimeoutError('controlled timeout')], [self.event()] * 7])
        channel = I._CDP(socket, time.monotonic() + 10)
        with self.assertRaises(TimeoutError): channel.call('DOM.getDocument')
        self.assertEqual(len(channel.pending), 1)
        self.assertEqual(channel.call('Runtime.releaseObjectGroup', cleanup=True), {})
        self.assertEqual(channel.pending, set()); self.assertEqual(channel.streamed_event_count, 307)

    def test_late_reply_budget_is_not_extended_by_streaming(self):
        from anyuse.execution import rea_inspector_bridge as I
        old = [{'id': index, 'result': {}} for index in range(1, I.MAX_CALL_NONSTREAM_FRAMES + 1)]
        socket = self.Socket([[self.event()] * 300 + old])
        channel = I._CDP(socket, time.monotonic() + 10)
        channel.next_id = I.MAX_RPC
        channel.pending.update(range(1, I.MAX_CALL_NONSTREAM_FRAMES + 1))
        with self.assertRaisesRegex(I.ProtocolError, 'response/event budget exhausted'):
            channel.call('Runtime.releaseObjectGroup', cleanup=True)
        self.assertEqual(channel.streamed_event_count, 300)
        self.assertEqual(channel.pending, {I.MAX_RPC + 1})

    def test_real_dom_query_zero_evaluation_and_cleanup_summary_contains_no_event_nodes(self):
        self._collect_stream_case('success')

    def test_cleanup_failure_keeps_incomplete_state_after_successful_streamed_dom_query(self):
        self._collect_stream_case('release_failure')

    def test_late_dom_reply_and_streamed_cleanup_cannot_clear_prior_evaluation_uncertainty(self):
        self._collect_stream_case('late_after_evaluation')

    def _collect_stream_case(self, fault):
        import pytest
        from test_rea_inspector_bridge import environment
        from anyuse.execution import rea_inspector_bridge as I
        with tempfile.TemporaryDirectory(prefix='inspector-event-stream-') as tmp:
            monkeypatch = pytest.MonkeyPatch()
            try:
                env = environment.__wrapped__(Path(tmp).resolve(), monkeypatch)
                original = env.server.response
                late = []
                def response(connection, message):
                    value = original(connection, message)
                    if message['method'] == 'DOM.querySelectorAll':
                        connection.queue.extend([self.event()] * 300)
                        if fault == 'late_after_evaluation':
                            late.append({'id': message['id'], 'result': value})
                            return None
                    if message['method'] == 'Runtime.releaseObjectGroup':
                        connection.queue.extend(late)
                        connection.queue.extend([self.event()] * 7)
                    return value
                monkeypatch.setattr(env.server, 'response', response)
                env.server.fault = 'release_failure' if fault == 'release_failure' else None
                operations = [{'op': 'dom_query', 'selector': 'body > #app', 'max_nodes': 8}]
                if fault == 'late_after_evaluation':
                    operations.insert(0, {'op': 'method', 'recipe': copy.deepcopy(env.actor['receiver']), 'method': env.actor['method']})
                receipt = I.collect(str(env.bundle), str(Path(tmp).resolve() / 'result'), {'operations': operations},
                    timeout=10, config_file=str(env.config), expected_identity=env.main)
                summary = receipt['event_summary']['page']
                self.assertEqual(summary['streamed']['count'], 307)
                self.assertEqual(summary['by_method']['DOM.setChildNodes']['received'], 307)
                self.assertEqual(summary['retained_count'], 1)
                self.assertEqual(summary['received_count'], 308)
                self.assertEqual(receipt['method_dispatch_attempts'], 0); self.assertEqual(receipt['getter_dispatch_attempts'], 0)
                self.assertNotIn('NODE_TREE_MUST_NOT_ENTER_RECEIPTS', json.dumps(receipt))
                self.assertEqual(env.server.dispatches, [])
                self.assertTrue(all(connection.closed for connection in env.server.connections))
                if fault == 'success':
                    self.assertEqual(receipt['status'], 'observed', receipt.get('error'))
                    self.assertEqual(receipt['runtime_evaluation_attempts'], 0)
                    self.assertFalse(receipt['resource_review_required']); self.assertFalse(receipt['state_uncertain'])
                    self.assertTrue(receipt['cleanup']['object_group_released'])
                elif fault == 'release_failure':
                    self.assertEqual(receipt['status'], 'incomplete')
                    self.assertEqual(receipt['runtime_evaluation_attempts'], 0)
                    self.assertTrue(receipt['resource_review_required'])
                    self.assertFalse(receipt['cleanup']['object_group_released'])
                else:
                    self.assertEqual(receipt['status'], 'incomplete')
                    self.assertTrue(receipt['state_uncertain']); self.assertTrue(receipt['resource_review_required'])
                    self.assertGreater(receipt['runtime_evaluation_attempts'], 0)
                    self.assertIsNone(receipt['actuated'])
                    self.assertTrue(receipt['cleanup']['object_group_released'])
                    self.assertFalse(receipt['cleanup']['requested_evaluations_completed'])
            finally:
                monkeypatch.undo()


if __name__ == "__main__":
    unittest.main()
