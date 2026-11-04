"""Where AnyUse keeps its data — ONE resolver, one environment override.

A module that resolves ``Path.home() / ".anyuse" / …`` at import time takes
no override, so any code path that constructs a storage object without an
explicit ``base_dir`` writes into the user's live data directory — including
from a test. That is how ``~/.anyuse/api_blocked_hosts.json`` once appeared
during a test run.

    ANYUSE_DATA_DIR=/tmp/anyuse-scratch python -m anyuse …

**The default**: with no environment variable set,
``anyuse_data_dir()`` is ``~/.anyuse``.

Secret-bearing paths deliberately do NOT follow ``ANYUSE_DATA_DIR``
-------------------------------------------------------------------
Encrypted artefacts are useless without the key that encrypted them. If the
entity-vault key file followed the data directory, moving the data directory
would silently mint a *new* key and orphan every artefact encrypted under the
old one — data loss triggered by what looks like a relocation. The same holds
for the credential vault and the key-custody sidecar.

So there are two roots, and the rule is one line:

* **data** — workflows, guides, sessions, caches, logs, registries, eval runs —
  follows ``ANYUSE_DATA_DIR``;
* **secrets** — the entity-vault key file, the credential vault, the custody
  sidecar — stays in the home directory and moves only when someone says so,
  through ``ANYUSE_SECRETS_DIR`` (or ``ANYUSE_VAULT_KEYFILE`` for that one file).

A harness that redirects ``ANYUSE_DATA_DIR`` should redirect
``ANYUSE_SECRETS_DIR`` too if the code under test can mint a key. (The split
is reversible in one function; ``test_data_paths.py`` pins the current one.)

Every resolver here reads the environment at CALL time, not at import time, so
a process can point the paths somewhere else before constructing a store.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "ANYUSE_DATA_DIR"
ENV_SECRETS_DIR = "ANYUSE_SECRETS_DIR"
ENV_VAULT_KEYFILE = "ANYUSE_VAULT_KEYFILE"

DEFAULT_DIR_NAME = ".anyuse"
VAULT_KEYFILE_NAME = "entity_vault.keys"


def anyuse_data_dir() -> Path:
    """The root of AnyUse's on-disk state. ``~/.anyuse`` unless overridden."""
    raw = os.environ.get(ENV_DATA_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


def anyuse_data_path(*parts: str) -> Path:
    """``anyuse_data_dir()`` joined with ``parts`` (no directory is created)."""
    return anyuse_data_dir().joinpath(*parts)


def anyuse_secret_dir() -> Path:
    """Where key material lives. Pinned to the home directory by design — see
    the module docstring for why it does not follow ``ANYUSE_DATA_DIR``."""
    raw = os.environ.get(ENV_SECRETS_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


def anyuse_secret_path(*parts: str) -> Path:
    """``anyuse_secret_dir()`` joined with ``parts`` (no directory is created)."""
    return anyuse_secret_dir().joinpath(*parts)


TYPING_SUPPRESSION_FLAG_NAME = ".typing_suppression"


def typing_suppression_flag() -> Path:
    """The file ``credentials/vault.CredentialVault.use_credential`` touches while it types a
    secret at the cursor, and the watcher reads to drop capture meanwhile. ONE resolver for
    both sides, read at call time: with the vault writing it under the secrets dir and the
    watcher looking under the data dir, a set ``ANYUSE_DATA_DIR`` would hide it from the
    watcher."""
    return anyuse_secret_path(TYPING_SUPPRESSION_FLAG_NAME)


def vault_key_file() -> Path:
    """The entity-vault key file. ``ANYUSE_VAULT_KEYFILE`` overrides this one
    file; otherwise it sits in ``anyuse_secret_dir()``."""
    raw = os.environ.get(ENV_VAULT_KEYFILE, "").strip()
    if raw:
        return Path(raw).expanduser()
    return anyuse_secret_path(VAULT_KEYFILE_NAME)


def describe() -> dict:
    """What the resolvers currently answer — for ``anyuse doctor`` and bug reports."""
    return {
        "data_dir": str(anyuse_data_dir()),
        "data_dir_overridden": bool(os.environ.get(ENV_DATA_DIR, "").strip()),
        "data_dir_exists": anyuse_data_dir().exists(),
        "secret_dir": str(anyuse_secret_dir()),
        "secret_dir_overridden": bool(os.environ.get(ENV_SECRETS_DIR, "").strip()),
        "vault_key_file": str(vault_key_file()),
        "vault_key_file_overridden": bool(os.environ.get(ENV_VAULT_KEYFILE, "").strip()),
        "secrets_follow_data_dir": False,
    }
