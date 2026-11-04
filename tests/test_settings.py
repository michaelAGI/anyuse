"""Persistent behavior settings and CLI (offline, no desktop imports)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from anyuse import settings
from anyuse.cli import cli


class IsolatedSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        names = set(settings.SETTINGS) | {"ANYUSE_DATA_DIR"}
        self.saved = {name: os.environ.get(name) for name in names}
        for name in settings.SETTINGS:
            os.environ.pop(name, None)
        os.environ["ANYUSE_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_no_file_keeps_the_existing_defaults(self):
        from anyuse.execution.actions import drive_enabled
        from anyuse.execution.agent_browser import escalation_enabled
        from anyuse.execution.api_permission import ROBOTS_RESPECT, _ttl, robots_policy
        from anyuse.execution.model_adapter import RunBudget, resolve_model_config
        from anyuse.mcpserver.server import mcp_profile

        self.assertFalse(settings.settings_path().exists())
        self.assertEqual(mcp_profile(), "default")
        self.assertEqual(RunBudget.from_env().limit_usd, 1.0)
        self.assertFalse(escalation_enabled())
        self.assertFalse(drive_enabled())
        self.assertEqual(robots_policy(), ROBOTS_RESPECT)
        self.assertEqual(_ttl(), 86_400)
        self.assertEqual(resolve_model_config("agent").retries, 2)

        # The historical readers treated blank budget/retry environment values
        # as unset, rather than as malformed configuration.
        os.environ["ANYUSE_RUN_BUDGET_USD"] = ""
        os.environ["ANYUSE_MODEL_RETRIES"] = ""
        self.assertEqual(RunBudget.from_env().limit_usd, 1.0)
        self.assertEqual(resolve_model_config("agent").retries, 2)

    def test_precedence_is_explicit_then_env_then_file_then_default(self):
        settings.set_value("ROBOTS_POLICY", "ignore")
        self.assertEqual(settings.resolve("ROBOTS_POLICY").source, "file")
        self.assertEqual(settings.get("ROBOTS_POLICY"), "ignore")
        os.environ["ANYUSE_ROBOTS_POLICY"] = "respect"
        self.assertEqual(settings.resolve("ROBOTS_POLICY").source, "env")
        self.assertEqual(settings.get("ROBOTS_POLICY"), "respect")
        resolved = settings.resolve("ROBOTS_POLICY", "ignore")
        self.assertEqual((resolved.value, resolved.source), ("ignore", "explicit"))
        os.environ.pop("ANYUSE_ROBOTS_POLICY")
        settings.reset_value("ROBOTS_POLICY")
        self.assertEqual(settings.resolve("ROBOTS_POLICY").source, "default")

    def test_tiny_writer_round_trips_typed_toml(self):
        path, value = settings.set_value("RUN_BUDGET_USD", "0.125")
        self.assertEqual(value, 0.125)
        self.assertEqual(path, Path(self.tmp.name) / "settings.toml")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        settings.set_value("API_MODALITY", "off")
        loaded = settings.load_file(strict=True)
        self.assertEqual(loaded["ANYUSE_RUN_BUDGET_USD"], 0.125)
        self.assertIs(loaded["ANYUSE_API_MODALITY"], False)

    def test_cli_lists_sets_gets_and_resets(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["settings", "set", "MODEL_RETRIES", "4"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ANYUSE_MODEL_RETRIES = 4", result.output)
        result = runner.invoke(cli, ["settings", "get", "ANYUSE_MODEL_RETRIES"])
        self.assertEqual((result.exit_code, result.output.strip()), (0, "4"))
        result = runner.invoke(cli, ["settings"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("SOURCE", result.output)
        self.assertIn("ANYUSE_MODEL_RETRIES", result.output)
        self.assertIn("file", result.output)
        result = runner.invoke(cli, ["settings", "reset", "MODEL_RETRIES"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(settings.get("MODEL_RETRIES"), 2)

    def test_invalid_persistent_values_are_rejected_without_writing(self):
        result = CliRunner().invoke(
            cli, ["settings", "set", "ANYUSE_MODEL_RETRIES", "9"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("0 through 5", result.output)
        self.assertFalse(settings.settings_path().exists())

    def test_gui_capture_modes_round_trip_and_invalid_mode_keeps_prior_setting(self):
        self.assertEqual(settings.get("REA_GUI_CAPTURE"), "auto")
        runner = CliRunner()
        for mode in ("off", "windows", "live", "auto"):
            result = runner.invoke(cli, ["settings", "set", "REA_GUI_CAPTURE", mode])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(settings.get("REA_GUI_CAPTURE"), mode)
        original = settings.settings_path().read_bytes()
        invalid = runner.invoke(cli, ["settings", "set", "REA_GUI_CAPTURE", "native-auto"])
        self.assertNotEqual(invalid.exit_code, 0)
        self.assertIn("auto, off, windows, live", invalid.output)
        self.assertEqual(settings.settings_path().read_bytes(), original)

    def test_security_relaxation_uses_the_existing_tty_gate(self):
        from anyuse.execution import human_gate

        refused = mock.Mock(return_value="refused: test needs a person")
        with mock.patch.object(human_gate, "TtyGate", return_value=refused):
            result = CliRunner().invoke(
                cli, ["settings", "set", "DRIVE_ENABLED", "on"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("needs a person", result.output)
        self.assertFalse(settings.settings_path().exists())
        refused.assert_called_once()

        allowed = mock.Mock(return_value=None)
        with mock.patch.object(human_gate, "TtyGate", return_value=allowed):
            result = CliRunner().invoke(
                cli, ["settings", "set", "DRIVE_ENABLED", "on"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(settings.load_file()["ANYUSE_DRIVE_ENABLED"])
        summary, digest = allowed.call_args.args
        self.assertEqual(summary["purpose"], "change-setting")
        self.assertEqual(len(digest), 64)

    def test_reset_that_relaxes_human_holds_also_needs_confirmation(self):
        from anyuse.execution import human_gate

        settings.set_value("HIL_MODE", "all")
        refused = mock.Mock(return_value="refused: keep the holds")
        with mock.patch.object(human_gate, "TtyGate", return_value=refused):
            result = CliRunner().invoke(cli, ["settings", "reset", "HIL_MODE"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(settings.load_file()["ANYUSE_HIL_MODE"], "all")

    def test_role_model_override_uses_the_file_and_environment_still_wins(self):
        from anyuse.execution.model_adapter import resolve_model_config

        settings.set_value("ANYUSE_AGENT_L2_MODEL_NAME", "file-model")
        self.assertEqual(resolve_model_config("agent_l2").model, "file-model")
        os.environ["ANYUSE_MODEL_NAME"] = "global-env-model"
        self.assertEqual(resolve_model_config("agent_l2").model, "global-env-model")
        os.environ["ANYUSE_AGENT_L2_MODEL_NAME"] = "env-model"
        self.assertEqual(resolve_model_config("agent_l2").model, "env-model")


if __name__ == "__main__":
    unittest.main()
