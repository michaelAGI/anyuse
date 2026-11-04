"""Configuration precedence and key-redacted doctor reporting."""
import json
import os

from click.testing import CliRunner

from anyuse.cli import cli
from anyuse.execution.model_adapter import resolve_model_config


NAMES = [
    "ANYUSE_MODEL_PROVIDER", "ANYUSE_MODEL_BASE_URL", "ANYUSE_MODEL_NAME",
    "ANYUSE_MODEL_API_KEY_ENV", "ANYUSE_PLANNER_MODEL_PROVIDER",
    "ANYUSE_PLANNER_MODEL_BASE_URL", "ANYUSE_PLANNER_MODEL_NAME",
    "ANYUSE_PLANNER_MODEL_API_KEY_ENV",
]


def _restore(before):
    for name in NAMES:
        if before[name] is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = before[name]


def test_role_overrides_global_and_explicit_overrides_role():
    before = {name: os.environ.get(name) for name in NAMES}
    try:
        os.environ.update({
            "ANYUSE_MODEL_PROVIDER": "cerebras",
            "ANYUSE_MODEL_BASE_URL": "https://global.invalid/v1",
            "ANYUSE_MODEL_NAME": "global-model",
            "ANYUSE_MODEL_API_KEY_ENV": "GLOBAL_KEY",
            "ANYUSE_PLANNER_MODEL_PROVIDER": "openrouter",
            "ANYUSE_PLANNER_MODEL_BASE_URL": "https://role.invalid/v1",
            "ANYUSE_PLANNER_MODEL_NAME": "role-model",
            "ANYUSE_PLANNER_MODEL_API_KEY_ENV": "ROLE_KEY",
        })
        role = resolve_model_config("planner")
        assert (role.provider, role.base_url, role.model, role.api_key_env) == (
            "openrouter", "https://role.invalid/v1", "role-model", "ROLE_KEY")
        explicit = resolve_model_config(
            "planner", provider="local", base_url="http://127.0.0.1:9999/v1",
            model="explicit", api_key_env="",
        )
        assert explicit.provider == "local" and explicit.model == "explicit"
        assert explicit.api_key_env == ""
    finally:
        _restore(before)


def test_doctor_prints_each_resolved_role_and_honest_unset_without_secret():
    missing = "ANYUSE_DOCTOR_MISSING_KEY"
    secret = "must-not-appear-in-doctor"
    os.environ.pop(missing, None)
    env = {
        "ANYUSE_MODEL_PROVIDER": "local",
        "ANYUSE_MODEL_BASE_URL": "http://127.0.0.1:11434/v1",
        "ANYUSE_MODEL_NAME": "doctor-model",
        "ANYUSE_MODEL_API_KEY_ENV": missing,
        "ANYUSE_MODEL_ENV_FILE": "/private/tmp/anyuse-doctor-does-not-exist",
        "UNRELATED_SECRET_FOR_ASSERTION": secret,
    }
    result = CliRunner().invoke(cli, ["doctor", "--json"], env=env)
    assert result.output, result.exception
    data = json.loads(result.output)
    roles = {row["check"]: row for row in data["checks"]
             if row["check"].startswith("model role ")}
    assert set(roles) == {f"model role {name}" for name in ("planner", "visual", "agent", "judge")}
    assert all("provider=local model=doctor-model credential=unset" in row["found"]
               for row in roles.values())
    assert all(row["ok"] is False for row in roles.values())
    assert secret not in result.output


if __name__ == "__main__":
    test_role_overrides_global_and_explicit_overrides_role()
    print("OK  explicit > role > global > preset")
    test_doctor_prints_each_resolved_role_and_honest_unset_without_secret()
    print("OK  doctor reports four roles and redacts credentials")
