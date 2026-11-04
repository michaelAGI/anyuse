"""Model-free imports stay usable while every provider SDK is refused."""
import importlib
import sys
from pathlib import Path


def test_execution_model_paths_import_without_a_provider_sdk():
    modules = [
        "anyuse.execution.plan_types",
        "anyuse.execution.model_adapter",
        "anyuse.execution.planner",
        "anyuse.execution.visual_step",
        "anyuse.execution.agent_browser",
        "anyuse.execution.judge",
        "anyuse.execution.intervention",
        "anyuse.execution.verifier",
        "anyuse.execution.params",
        "anyuse.execution.process_mining",
        "anyuse.execution.orchestrator",
        "anyuse.memory.storage",
    ]
    for name in modules:
        sys.modules.pop(name, None)
        imported = importlib.import_module(name)
        assert imported is not None, name


def test_shared_plan_types_remain_dependency_free():
    text = (Path(__file__).parent.parent / "execution" / "plan_types.py").read_text()
    assert "model_adapter" not in text
    assert "google" not in text and "openai" not in text and "httpx" not in text


def test_ported_paths_contain_no_provider_sdk_imports():
    root = Path(__file__).parent.parent
    for relative in (
        "execution/model_adapter.py", "execution/planner.py",
        "execution/visual_step.py", "execution/agent_browser.py",
        "execution/judge.py", "execution/intervention.py",
        "execution/verifier.py", "execution/params.py",
        "execution/process_mining.py", "memory/storage.py",
    ):
        text = (root / relative).read_text()
        assert "from google" not in text and "import google" not in text, relative
        assert "import openai" not in text and "import cerebras" not in text, relative


if __name__ == "__main__":
    test_execution_model_paths_import_without_a_provider_sdk()
    print("OK  execution model paths import with provider SDKs blocked")
    test_shared_plan_types_remain_dependency_free()
    print("OK  plan_types remains dependency-free")
    test_ported_paths_contain_no_provider_sdk_imports()
    print("OK  ported paths contain no provider SDK imports")
