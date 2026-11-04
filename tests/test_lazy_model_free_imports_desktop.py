"""The model-free ladder must import without the model SDK and without pyautogui.

This is the regression test for the import failures found the first time
the desktop ladder was imported inside a real macOS GUI session (a throwaway guest
VM with pyobjc installed but deliberately NO ``google-genai``, no ``pyautogui``
and no API key). Every one of these failed there:

    import anyuse.execution.drift_gate    -> ModuleNotFoundError: 'google'
    import anyuse.execution.self_healing  -> ModuleNotFoundError: 'google'
    import anyuse.execution.deterministic -> ModuleNotFoundError: 'google'
    import anyuse.execution.hil_gate      -> ModuleNotFoundError: 'google'
    import anyuse.memory.recorder         -> ModuleNotFoundError: 'pyautogui'

Not one of those modules calls a model. ``drift_gate``'s own docstring says it is
"PURE analysis … NEVER calls a model"; it still could not be loaded without the
Gemini SDK on the box, because four package ``__init__`` files and two module-top
imports reached through model-bearing modules:

    execution/__init__.py -> planner        -> provider SDK import
    guides/__init__.py    -> detector       -> provider SDK import
    memory/recorder.py    -> matcher        -> import pyautogui
    execution/deterministic.py, execution/self_healing.py -> planner/executor

Why the assertion is on ``sys.modules`` and not on a raised ImportError: the host
test environment may well HAVE ``google-genai`` installed, in which case an
eager import would succeed and the defect would go unnoticed. Asserting that the
heavy package was never *loaded* catches the regression on any machine, with or
without the SDK present.

Run from the anyuse package dir:
    cd anyuse && python3 test_lazy_model_free_imports.py
"""

import pathlib
import subprocess
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent

# NOT ``sys.path.insert(0, _HERE.parent)``, which is the house idiom elsewhere in
# this suite. The repository root IS the ``anyuse`` package, so that idiom only
# works when the checkout happens to be named ``anyuse``. In a git worktree named
# anything else it silently imports a SIBLING checkout: run from
# ``anyuse-wt/anyuse-desktop`` it loaded ``anyuse-wt/anyuse/memory/recorder.py``
# and reported that worktree's defects as this one's. Instead, a private
# directory holding a symlink ``anyuse`` -> this checkout is put on the path, so
# the package always resolves to the tree the test file lives in.
_LINKDIR = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
_PKGROOT = pathlib.Path(_LINKDIR.name)
(_PKGROOT / "anyuse").symlink_to(_HERE, target_is_directory=True)

# The modules that must never need a model SDK to be importable, each with the
# reason it belongs on this list.
MODEL_FREE = [
    ("anyuse.execution.drift_gate", "pure AX analysis; the token-saver decision"),
    ("anyuse.execution.deterministic", "the token-free replay rung itself"),
    ("anyuse.execution.hil_gate", "the human-in-the-loop classifier"),
    ("anyuse.execution.self_healing", "persists learnings; does no model call"),
    # Found running Calculator through the real
    # product executor in the guest: StateGraphExecutor — the product's own
    # step dispatcher, not the VM harness's hand-rolled stand-in — and
    # the plain PlannedStep/ExecutionPlan dataclasses it and deterministic
    # replay both construct, previously needed google-genai just to import,
    # because they lived beside or imported at module top the one
    # class that actually calls Gemini. Superseded by
    # the model_adapter design (execution/plan_types.py, no google import
    # anywhere in the planner/executor path) — kept here as a regression test.
    ("anyuse.execution.executor", "the product's step dispatcher (D13)"),
    ("anyuse.guides.state_graph", "the recorded-state data structures"),
    ("anyuse.context.element_scorer", "the deterministic weighted matcher"),
    ("anyuse.context.accessibility", "the AX tree reader"),
    ("anyuse.memory.recorder", "the recorder; AX capture needs no mouse stack"),
    ("anyuse.memory.storage", "workflow persistence"),
    ("anyuse.guides.storage", "guide persistence"),
]

# Top-level packages that must not be pulled in as a side effect.
HEAVY = ("google", "pyautogui", "pynput")

_CHILD = r"""
import sys
sys.path.insert(0, {parent!r})
mods = {mods!r}
heavy = {heavy!r}
for name in mods:
    __import__(name)
# D14: constructing a PlannedStep (what every deterministic-replay step and
# every real StateGraphExecutor dispatch needs) must not need google-genai
# either. On this unified branch it lives in the dependency-free
# execution/plan_types.py, not beside execution/planner.py's ExecutionPlanner.
from anyuse.execution.plan_types import PlannedStep, ExecutionPlan
PlannedStep(intent="i", source="workflow", target_element="", modality="ax", command={{}})
ExecutionPlan(current_state="s", steps=[])
loaded = sorted({{m.split(".")[0] for m in sys.modules}} & set(heavy))
print("LOADED:" + ",".join(loaded))
"""


def main():
    print("=" * 66)
    print("model-free ladder imports without a model SDK (defects D1-D4)")
    print("=" * 66)

    names = [n for n, _ in MODEL_FREE]
    # A child interpreter, so the assertion is about THIS import chain and is not
    # polluted by whatever the parent process already loaded.
    code = _CHILD.format(parent=str(_PKGROOT), mods=names, heavy=list(HEAVY))
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError(
            "a model-free module failed to import; see the traceback above. "
            "Something re-introduced an eager import through a model-bearing "
            "module (planner/executor/detector/matcher).")

    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LOADED:")]
    assert line, f"child produced no LOADED line: {proc.stdout!r}"
    loaded = [p for p in line[0][len("LOADED:"):].split(",") if p]

    for name, why in MODEL_FREE:
        print(f"OK  {name:38s} {why}")

    assert not loaded, (
        f"importing the model-free ladder pulled in {loaded!r}. "
        f"These modules must stay loadable on a machine with no model SDK and "
        f"no mouse/screen stack — that is what 'token-free replay' means. Use a "
        f"TYPE_CHECKING import or a function-level import instead.")
    print(f"OK  none of {list(HEAVY)} was loaded as a side effect")

    # The public surface of the lazily re-exported packages must be unchanged.
    sys.path.insert(0, str(_PKGROOT))
    import anyuse.execution as ex
    import anyuse.guides as gu
    for pkg, expected in ((ex, "ExecutionPlanner"), (gu, "GuideDetector")):
        assert expected in pkg.__all__, f"{pkg.__name__}.__all__ lost {expected}"
        assert expected in dir(pkg), f"dir({pkg.__name__}) lost {expected}"
    print("OK  __all__/__dir__ still advertise the eager-era public names")

    try:
        ex.NoSuchName
    except AttributeError:
        print("OK  an unknown attribute still raises AttributeError")
    else:
        raise AssertionError("unknown attribute did not raise AttributeError")

    print("\nLAZY MODEL-FREE IMPORT TEST PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
