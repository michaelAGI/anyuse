"""The guarded runner's branch gate and pkgroot fix.

``tools/guarded_tests/run_allowed.sh`` runs on any branch and in a plain clone;
``ANYUSE_GUARD_BRANCHES`` optionally restricts it to listed branches. A listed name
(``feat/desktop-on-adapter`` here) is accepted without weakening the check for anything else.

This builds a throwaway git repository + linked worktree (never the real
checkout) and copies only ``run_allowed.sh`` into it — not ``sitecustomize.py``
or ``tripwire.py`` — so a run on an accepted branch is expected to get past
the branch/worktree gate and fail later, deterministically, on the missing
``sitecustomize`` import (exit 3). A run on any other branch name must be
refused at the gate itself (exit 7, "wrong branch").

The last test here covers a second fix in the same file, for a whole class of bug where a bare ``from
anyuse... import ...`` in a test file resolves through whichever
"anyuse"-named directory happens to be first on ``PYTHONPATH`` — which, for
any linked worktree not itself named ``anyuse`` (``anyuse-desktop``,
``anyuse-unified``, this throwaway one), can be a DIFFERENT, independently
changing checkout that merely happens to sit next to it on disk. This was
found live (a test shadow-imported the actual web-branch checkout mid
another process's edits) and is fixed once, centrally, in the runner rather
than per test file.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_RUNNER = _HERE / "tools" / "guarded_tests" / "run_allowed.sh"
# Use the same Git as _make_worktree, including guest-local installations.
# The remaining environment stays minimal so sitecustomize is not inherited.
_TOOL_PATH = str(Path(shutil.which("git") or "/usr/bin/git").parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"


def _make_worktree(tmp_root: Path, branch: str) -> Path:
    origin = tmp_root / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "README.md").write_text("throwaway\n")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "init"],
        cwd=origin, check=True,
    )

    worktree = tmp_root / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", branch],
        cwd=origin, check=True,
    )
    guard_dir = worktree / "tools" / "guarded_tests"
    guard_dir.mkdir(parents=True)
    shutil.copy(_RUNNER, guard_dir / "run_allowed.sh")
    (worktree / "docs").mkdir()
    (worktree / "docs" / "TEST_ALLOWLIST.txt").write_text("")
    return worktree


def _run(worktree: Path, branches: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": _TOOL_PATH, "ANYUSE_TEST_PYTHON": sys.executable}
    if branches:
        env["ANYUSE_GUARD_BRANCHES"] = branches
    return subprocess.run(
        ["zsh", str(worktree / "tools" / "guarded_tests" / "run_allowed.sh")],
        cwd=worktree, capture_output=True, text=True, env=env,
    )


def test_any_branch_is_accepted_by_default():
    with tempfile.TemporaryDirectory(prefix="anyuse-branchgate-") as tmp:
        worktree = _make_worktree(Path(tmp), "some-branch")
        proc = _run(worktree)
        assert "wrong branch" not in proc.stdout, proc.stdout
        # Falls through to the (uncopied) sitecustomize import and stops there —
        # proof the branch gate itself was cleared, not skipped.
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


def test_a_plain_clone_on_main_is_accepted():
    with tempfile.TemporaryDirectory(prefix="anyuse-branchgate-") as tmp:
        worktree = _make_worktree(Path(tmp), "unused")
        origin = Path(tmp) / "origin"
        shutil.copytree(worktree / "tools", origin / "tools")
        shutil.copytree(worktree / "docs", origin / "docs")
        proc = _run(origin)
        assert "wrong branch" not in proc.stdout, proc.stdout
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


def test_a_listed_branch_is_accepted_when_the_gate_is_set():
    with tempfile.TemporaryDirectory(prefix="anyuse-branchgate-") as tmp:
        worktree = _make_worktree(Path(tmp), "feat/desktop-on-adapter")
        proc = _run(worktree, branches="main feat/desktop-on-adapter")
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


def test_an_unlisted_branch_is_refused_when_the_gate_is_set():
    with tempfile.TemporaryDirectory(prefix="anyuse-branchgate-") as tmp:
        worktree = _make_worktree(Path(tmp), "some-other-branch")
        proc = _run(worktree, branches="main")
        assert "wrong branch" in proc.stdout, proc.stdout
        assert proc.returncode == 7, (proc.returncode, proc.stdout)


def test_runner_makes_anyuse_resolve_to_this_worktree_first():
    with tempfile.TemporaryDirectory(prefix="anyuse-branchgate-") as tmp:
        tmp_root = Path(tmp)
        worktree = _make_worktree(tmp_root, "feat/desktop-on-adapter")
        # The hazard: a SIBLING directory literally named "anyuse", right next
        # to the worktree, exactly like the real ~/src/anyuse-worktrees/
        # layout (anyuse, anyuse-desktop, anyuse-unified as siblings).
        sibling = tmp_root / "anyuse"
        sibling.mkdir()
        (sibling / "__init__.py").write_text("MARKER = 'wrong-checkout'\n")

        scratch = tmp_root / "scratch"
        env = {"PATH": _TOOL_PATH,
               "ANYUSE_TEST_PYTHON": sys.executable,
               "ANYUSE_GUARD_SCRATCH": str(scratch)}
        subprocess.run(
            ["zsh", str(worktree / "tools" / "guarded_tests" / "run_allowed.sh")],
            cwd=worktree, capture_output=True, text=True, env=env,
        )  # exit 3 expected (no sitecustomize copied in) — irrelevant here

        link = scratch / "pkgroot" / "anyuse"
        assert link.is_symlink(), "runner did not create the pkgroot symlink"
        assert os.path.realpath(link) == os.path.realpath(worktree), (
            "pkgroot symlink points at the wrong checkout")


if __name__ == "__main__":
    test_any_branch_is_accepted_by_default()
    print("OK  any branch is accepted by default")
    test_a_plain_clone_on_main_is_accepted()
    print("OK  a plain clone on main is accepted")
    test_a_listed_branch_is_accepted_when_the_gate_is_set()
    print("OK  a listed branch is accepted under ANYUSE_GUARD_BRANCHES")
    test_an_unlisted_branch_is_refused_when_the_gate_is_set()
    print("OK  an unlisted branch is refused under ANYUSE_GUARD_BRANCHES")
    test_runner_makes_anyuse_resolve_to_this_worktree_first()
    print("OK  the runner's pkgroot symlink shadows a sibling anyuse checkout")
