"""v2 OS-enforced key custody (watcher/key_custody.py).

Exercises the whole custody logic over a FAKE Security shim (no Keychain, no
Touch ID — a headless run can't answer a presence prompt).

Self-executing (pytest is not installed in this env). Run from the anyuse
package dir:
    cd anyuse && .venv/bin/python test_key_custody.py
"""

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.watcher import key_custody  # noqa: E402


class FakeSec:
    """Scripted Security shim: items {account: bytes}; can refuse presence."""

    def __init__(self):
        self.items = {}
        self.refuse_presence = False
        self.reads = []

    def make_access_control(self):
        return object()  # opaque, like the real SecAccessControlRef

    def add_item(self, account, secret, access_control):
        assert access_control is not None
        if account in self.items:
            return key_custody._ERR_DUPLICATE
        self.items[account] = bytes(secret)
        return 0

    def read_item(self, account, reason):
        self.reads.append((account, reason))
        if account not in self.items:
            return key_custody._ERR_NOT_FOUND, None
        if self.refuse_presence:
            return key_custody._ERR_USER_CANCELED, None
        return 0, self.items[account]

    def delete_item(self, account):
        if account in self.items:
            del self.items[account]
            return 0
        return key_custody._ERR_NOT_FOUND


def main():
    print("=" * 66)
    print("v2 OS-enforced key custody (SecAccessControl) — key_custody.py")
    print("=" * 66)

    sec = FakeSec()

    # 1. store semantics
    assert key_custody.store_protected_key("k1", b"secret-1", sec=sec) is True
    assert sec.items["k1"] == b"secret-1"
    assert key_custody.store_protected_key("k1", b"EVIL", sec=sec) is False
    assert sec.items["k1"] == b"secret-1", "existing item never overwritten"
    print("OK  (1) store: fresh OK; duplicate never overwritten")

    # 2. read semantics (fail-closed)
    assert key_custody.read_protected_key("k1", sec=sec) == b"secret-1"
    assert key_custody.read_protected_key("ghost", sec=sec) is None
    sec.refuse_presence = True
    assert key_custody.read_protected_key("k1", sec=sec) is None
    sec.refuse_presence = False
    assert sec.reads and sec.reads[0][1], "reason string carried to the prompt"
    print("OK  (2) read: present→bytes; absent/canceled→None (fail-closed)")

    # 3. load_or_create: read-first / mint-on-absent / REFUSED ≠ absent
    got = key_custody.load_or_create_protected_key("k1", sec=sec)
    assert got == b"secret-1", got
    minted = key_custody.load_or_create_protected_key(
        "k2", generate=lambda: b"fresh-2", sec=sec)
    assert minted == b"fresh-2" and sec.items["k2"] == b"fresh-2"
    sec.refuse_presence = True
    refused = key_custody.load_or_create_protected_key("k2", sec=sec)
    assert refused is None, "a refused read must NOT mint a replacement key"
    assert sec.items["k2"] == b"fresh-2", "no orphaning overwrite"
    sec.refuse_presence = False
    print("OK  (3) load_or_create: read-first; mint-on-absent; refused read "
          "mints nothing (orphan hazard closed)")

    # 4. delete = rotation primitive
    assert key_custody.delete_protected_key("k2", sec=sec) is True
    assert key_custody.delete_protected_key("k2", sec=sec) is False
    print("OK  (4) delete: explicit rotation primitive")

    # 5. migrate: v1 bytes land under OS custody unchanged
    assert key_custody.migrate_key("k_raw", b"v1-key-bytes", sec=sec) is True
    assert sec.items["k_raw"] == b"v1-key-bytes"
    assert key_custody.migrate_key("k_raw", b"other", sec=sec) is False
    assert key_custody.migrate_key("k_none", b"", sec=sec) is False
    print("OK  (5) migrate: same bytes, no re-encryption; never re-migrates")

    # 6. env gating
    os.environ.pop("ANYUSE_VAULT_CUSTODY", None)
    assert key_custody.enabled() is False
    os.environ["ANYUSE_VAULT_CUSTODY"] = "os"
    assert key_custody.enabled() is True
    os.environ.pop("ANYUSE_VAULT_CUSTODY", None)
    print("OK  (6) ANYUSE_VAULT_CUSTODY=os gating")

    print("\nALL KEY-CUSTODY (v2) TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
