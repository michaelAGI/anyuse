"""One data-dir resolver, one environment override, default unchanged.

The failure this closes: any storage object constructed without an explicit
``base_dir`` wrote into the user's live ``~/.anyuse`` — including from a test,
which is how ``~/.anyuse/api_blocked_hosts.json`` once appeared during a run.

Pure logic: nothing here creates a store, touches a keychain or writes outside a
temp directory. The env vars it sets are restored in a ``finally``.
"""

import os
import tempfile
from pathlib import Path

from anyuse import paths
from anyuse.paths import (
    ENV_DATA_DIR, ENV_VAULT_KEYFILE, anyuse_data_dir, anyuse_data_path,
    vault_key_file,
)


class _env:
    """Set/clear environment variables for the duration of a block."""

    def __init__(self, **kv):
        self.kv = kv
        self.old = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# --------------------------------------------------------------------------
# the resolver itself
# --------------------------------------------------------------------------

def test_default_path_is_unchanged():
    """The whole point of shipping this quietly: nobody's data moves."""
    with _env(**{ENV_DATA_DIR: None}):
        assert anyuse_data_dir() == Path.home() / ".anyuse"
        assert anyuse_data_path("workflows") == Path.home() / ".anyuse" / "workflows"
    print("  (i) default is ~/.anyuse, unchanged")


def test_env_override_moves_the_data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(**{ENV_DATA_DIR: tmp}):
            assert anyuse_data_dir() == Path(tmp)
            assert anyuse_data_path("workflows", "x.json") == Path(tmp) / "workflows" / "x.json"
    print("  (i) ANYUSE_DATA_DIR moves everything under one root")


def test_override_is_read_at_call_time_not_import_time():
    """A test harness that sets the variable after importing anyuse still wins."""
    with _env(**{ENV_DATA_DIR: "/tmp/a-one"}):
        first = anyuse_data_dir()
    with _env(**{ENV_DATA_DIR: "/tmp/a-two"}):
        second = anyuse_data_dir()
    assert first != second, (first, second)
    print("  (i) resolved per call, not pinned at import")


def test_blank_and_tilde_are_handled():
    with _env(**{ENV_DATA_DIR: "   "}):
        assert anyuse_data_dir() == Path.home() / ".anyuse", "a blank value must not win"
    with _env(**{ENV_DATA_DIR: "~/anyuse-elsewhere"}):
        assert anyuse_data_dir() == Path.home() / "anyuse-elsewhere"
    print("  (i) blank falls back; ~ is expanded")


# --------------------------------------------------------------------------
# the vault key file decision
# --------------------------------------------------------------------------

def test_key_file_does_not_follow_the_data_dir():
    """The documented decision (anyuse/paths.py): moving the data directory must
    NOT silently mint a new key and orphan every artefact encrypted with the old
    one. Key material moves only when someone says so."""
    with tempfile.TemporaryDirectory() as tmp:
        with _env(**{ENV_DATA_DIR: tmp, ENV_VAULT_KEYFILE: None}):
            assert anyuse_data_dir() == Path(tmp)
            assert vault_key_file() == Path.home() / ".anyuse" / "entity_vault.keys"
            assert Path(tmp) not in vault_key_file().parents
    print("  (i) key file stays put when only ANYUSE_DATA_DIR moves")


def test_key_file_has_its_own_explicit_override():
    with tempfile.TemporaryDirectory() as tmp:
        target = str(Path(tmp) / "scratch.keys")
        with _env(**{ENV_VAULT_KEYFILE: target}):
            assert vault_key_file() == Path(target)
            from anyuse.watcher import entity_vault
            assert entity_vault._keyfile() == Path(target), "the vault must re-resolve per call"
    print("  (i) ANYUSE_VAULT_KEYFILE redirects key custody explicitly")


def test_describe_reports_the_decision():
    d = paths.describe()
    assert d["secrets_follow_data_dir"] is False
    assert set(d) >= {"data_dir", "data_dir_overridden", "secret_dir", "vault_key_file"}
    print("  (i) paths.describe() states where things are and what was overridden")


def test_secret_dir_is_separate_from_the_data_dir():
    """The rule in one test: data moves, secrets stay unless told otherwise."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as sec:
        with _env(**{ENV_DATA_DIR: data, paths.ENV_SECRETS_DIR: None,
                     ENV_VAULT_KEYFILE: None}):
            assert paths.anyuse_secret_dir() == Path.home() / ".anyuse"
            from anyuse.credentials.vault import CredentialVault
            # class attribute, bound at import: the resolver, not a literal
            assert ".anyuse" in str(CredentialVault.VAULT_DIR)
        with _env(**{ENV_DATA_DIR: data, paths.ENV_SECRETS_DIR: sec,
                     ENV_VAULT_KEYFILE: None}):
            assert paths.anyuse_secret_dir() == Path(sec)
            assert vault_key_file() == Path(sec) / "entity_vault.keys"
            assert paths.anyuse_secret_path("entity_vault_custody.json").parent == Path(sec)
    print("  (i) ANYUSE_SECRETS_DIR is the only thing that moves key material")


# --------------------------------------------------------------------------
# the four stores that used to hard-code the path
# --------------------------------------------------------------------------

def test_every_store_default_follows_the_resolver():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(**{ENV_DATA_DIR: tmp}):
            from anyuse.memory.storage import WorkflowStorage
            from anyuse.guides.storage import GuideStorage
            from anyuse.watcher.storage import SessionStorage

            assert WorkflowStorage.default_base_dir() == Path(tmp) / "workflows"
            assert GuideStorage.default_base_dir() == Path(tmp) / "app_guides"
            assert SessionStorage.default_base_dir() == Path(tmp) / "sessions"
    print("  (i) workflows / app_guides / sessions all follow one resolver")


def test_host_registries_follow_the_resolver_too():
    from anyuse.execution import api_permission, blocked
    with tempfile.TemporaryDirectory() as tmp:
        with _env(**{ENV_DATA_DIR: tmp, "ANYUSE_API_HOSTS_FILE": None,
                     "ANYUSE_BLOCKED_HOSTS_FILE": None}):
            assert api_permission.hosts_file() == str(Path(tmp) / "api_hosts.json")
            assert blocked.blocked_hosts_file() == str(Path(tmp) / "api_blocked_hosts.json")
        # The per-file variables still win over the data dir.
        with _env(**{ENV_DATA_DIR: tmp,
                     "ANYUSE_API_HOSTS_FILE": "/tmp/explicit-hosts.json"}):
            assert api_permission.hosts_file() == "/tmp/explicit-hosts.json"
    print("  (i) api_hosts / blocked_hosts follow it; their own vars still win")


def test_no_module_hard_codes_the_home_path_any_more():
    """A grep test, deliberately: this defect came back once already."""
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        if rel.startswith(("test_", "tests/", "tools/")) or rel == "paths.py":
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if 'Path.home() / ".anyuse"' in line or 'expanduser("~"), ".anyuse"' in line:
                offenders.append(f"{rel}:{i}")
    assert not offenders, "hard-coded AnyUse data paths: " + ", ".join(offenders)
    print(f"  (i) no module outside paths.py hard-codes ~/.anyuse ({len(list(root.rglob('*.py')))} files scanned)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"test_data_paths: {len(tests)} tests")
    for t in tests:
        t()
    print(f"OK — {len(tests)} passed")
