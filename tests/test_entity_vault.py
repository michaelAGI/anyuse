"""Tests for the entity vault.

Covers:
  - token determinism + canonicalisation (case/whitespace variants merge)
  - class separation (same value, different class → different token)
  - tokenize/deref roundtrip under a grant; values decrypt to the original
  - vault_grant classes deref WITHOUT presence even on requires_presence grants
  - vault_presence classes: presence provider denies → presence_denied;
    provider approves → ok; audit rows written for allow AND deny
  - no grant → no_grant; expired / revoked grants don't match; mode ranking
    (search < deref < bulk); token-scoped grants
  - never_captured classes are never stored (deref → not_found)
  - regex net: email, Luhn-valid card, phone, currency amounts; Luhn-invalid
    numbers ignored; existing [[ent:...]] markers protected (idempotency)
  - tokenize_text with model-extracted ENTITIES spans + regex backstop
  - tokenize_record: summary/structure/steps/tags tokenized, title regex-netted
  - redact_text / tokenize_or_redact vault-free fail-closed fallback
  - search_token: grant-gated existence check without decryption

Self-executing (pytest is not installed in this env). Never touches the real
~/.anyuse or the Keychain — keys and db paths are injected.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_entity_vault.py
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

from cryptography.fernet import Fernet

from anyuse.watcher import entity_vault as ev
from anyuse.watcher.entity_vault import (
    EntityVault, MARKER_RE, redact_text, regex_net, tier_for,
    tokenize_or_redact,
)
from anyuse.watcher.models import AbstractionRecord


# Shared across every instance in a test — two vaults over the same db must
# be able to decrypt each other's ciphertexts (stable-key requirement).
_K_TOK = b"\x01" * 32
_K_ENC = Fernet.generate_key()


def _vault(tmpdir, presence=lambda reason: True) -> EntityVault:
    return EntityVault(
        db_path=pathlib.Path(tmpdir) / "vault.db",
        k_tok=_K_TOK,
        k_enc=_K_ENC,
        presence_provider=presence,
    )


def test_token_determinism_and_canonicalisation():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        t1 = v.token_for("Sarah Connor", "person")
        t2 = v.token_for("  sarah   connor ", "person")
        assert t1 == t2, "case/whitespace variants must merge to one token"
        assert len(t1) == 12 and all(c in "0123456789abcdef" for c in t1)
        t3 = v.token_for("Sarah Connor", "project")
        assert t3 != t1, "same value in a different class must differ"
    print("OK  test_token_determinism_and_canonicalisation")


def test_roundtrip_deref_with_grant():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        token = v.tokenize("Acme Corp", "project", app="Mail")
        # no grant yet
        r = v.deref(token, "brain-local", purpose="test")
        assert r["ok"] is False and r["reason"] == "no_grant"
        v.grant("brain-local", ["project"], mode="deref")
        r = v.deref(token, "brain-local", purpose="test")
        assert r["ok"] is True and r["value"] == "Acme Corp"
        assert r["class"] == "project"
        # audit captured both the deny and the allow
        log = v.audit_log()
        assert len(log) == 2
        assert log[0]["allowed"] is True and log[1]["allowed"] is False
    print("OK  test_roundtrip_deref_with_grant")


def test_vault_grant_class_skips_presence():
    calls = []

    def presence(reason):
        calls.append(reason)
        return True

    with tempfile.TemporaryDirectory() as td:
        v = _vault(td, presence=presence)
        token = v.tokenize("myproject", "project")
        v.grant("brain-local", ["project"], requires_presence=True)
        r = v.deref(token, "brain-local")
        assert r["ok"] is True
        assert calls == [], "vault_grant tier must not prompt for presence"
        assert tier_for("project") == "vault_grant"
    print("OK  test_vault_grant_class_skips_presence")


def test_vault_presence_class_gated_by_presence():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td, presence=lambda reason: False)
        token = v.tokenize("sarah@example.com", "email_address")
        v.grant("brain-local", ["email_address"], requires_presence=True)
        r = v.deref(token, "brain-local")
        assert r["ok"] is False and r["reason"] == "presence_denied"
        # audit row records the presence attempt
        assert v.audit_log()[0]["presence_used"] is True

        v2 = _vault(td, presence=lambda reason: True)
        # same keys, same db → same token still there
        v2.grant("brain-local", ["email_address"], requires_presence=True)
        r2 = v2.deref(token, "brain-local")
        assert r2["ok"] is True and r2["value"] == "sarah@example.com"

        # requires_presence=False grant skips the prompt entirely
        v3 = _vault(td, presence=lambda reason: (_ for _ in ()).throw(AssertionError))
        v3.grant("cloud:gemini", ["email_address"], requires_presence=False)
        r3 = v3.deref(token, "cloud:gemini")
        assert r3["ok"] is True
    print("OK  test_vault_presence_class_gated_by_presence")


def test_grant_expiry_revoke_mode_and_scope():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        token = v.tokenize("proj-x", "project")
        other = v.tokenize("proj-y", "project")

        # expired
        gid = v.grant("a", ["project"], expires_at="2000-01-01T00:00:00")
        assert v.deref(token, "a")["reason"] == "no_grant"

        # revoked
        gid = v.grant("a", ["project"])
        assert v.deref(token, "a")["ok"] is True
        assert v.revoke(gid) is True
        assert v.deref(token, "a")["reason"] == "no_grant"

        # mode ranking: a search grant must NOT allow deref; bulk must
        v.grant("b", ["project"], mode="search")
        assert v.deref(token, "b")["reason"] == "no_grant"
        v.grant("c", ["project"], mode="bulk")
        assert v.deref(token, "c")["ok"] is True

        # token scope
        v.grant("d", ["project"], scope={"tokens": [token]})
        assert v.deref(token, "d")["ok"] is True
        assert v.deref(other, "d")["reason"] == "no_grant"
    print("OK  test_grant_expiry_revoke_mode_and_scope")


def test_never_captured_not_stored():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        token = v.tokenize("4111111111111111", "payment_card")
        assert token  # a token is returned (deterministic marker possible)
        v.grant("brain-local", ["payment_card"], requires_presence=False)
        r = v.deref(token, "brain-local")
        assert r["ok"] is False and r["reason"] == "not_found", \
            "never_captured classes must have no ciphertext to deref"
    print("OK  test_never_captured_not_stored")


def test_regex_net_shapes():
    hits = regex_net(
        "Mail sarah@example.com or +1 (415) 555-0134 about the "
        "$1,234.56 invoice paid with 4111 1111 1111 1111 ok"
    )
    classes = [h[3] for h in hits]
    assert "email_address" in classes
    assert "phone" in classes
    assert "amount" in classes
    assert "payment_card" in classes

    # Luhn-invalid long number is NOT a card (and has separators, so it's not
    # phone-shaped either once digits exceed the phone pattern)
    hits2 = regex_net("build id 4111 1111 1111 1112 done")
    assert "payment_card" not in [h[3] for h in hits2]

    # already-tokenized markers are protected
    text = "email [[ent:email_address:abcdef012345]] sent"
    assert regex_net(text) == []
    print("OK  test_regex_net_shapes")


def test_tokenize_text_entities_and_idempotency():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        out = v.tokenize_text(
            "Replied to Sarah Connor about the Acme invoice at sarah@acme.com",
            entities=[
                {"span": "Sarah Connor", "class": "person"},
                {"span": "Acme", "class": "project"},
            ],
        )
        assert "Sarah Connor" not in out
        assert "sarah@acme.com" not in out
        assert len(MARKER_RE.findall(out)) == 3
        # idempotent: a second pass must not double-wrap
        again = v.tokenize_text(out)
        assert again == out

        # never_captured span in model entities → redacted, not vaulted
        pw = v.tokenize_text("typed hunter2 into the field",
                             entities=[{"span": "hunter2", "class": "password"}])
        assert "hunter2" not in pw and "[[redacted:password]]" in pw

        # Regression: a shorter model span occurring inside an ALREADY-inserted
        # marker's class name ("mail" ⊂ "email_address", replaced longest-first)
        # must not corrupt that marker.
        tricky = v.tokenize_text(
            "sent mail to sarah@acme.com",
            entities=[
                {"span": "sarah@acme.com", "class": "email_address"},
                {"span": "mail", "class": "other"},
            ],
        )
        assert "sarah@acme.com" not in tricky
        markers = MARKER_RE.findall(tricky)
        assert ("email_address", v.token_for("sarah@acme.com", "email_address")) in markers, tricky
        assert ("other", v.token_for("mail", "other")) in markers, tricky
    print("OK  test_tokenize_text_entities_and_idempotency")


def test_tokenize_record_fields():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        record = AbstractionRecord(
            kind="task",
            title="reply_to_client_email",
            summary="Replied to bob@client.com about the order",
            structure={
                "goal": "answer bob@client.com quickly",
                "trigger_phrases": [],
                "steps": [{
                    "action": "click",
                    "target_role": "button",
                    "target_name": "Reply to bob@client.com",
                    "app": "Mail",
                    "reason": "start the reply",
                }],
            },
            apps=["Mail"],
            tags=["email bob@client.com"],
        )
        v.tokenize_record(record)
        flat = record.summary + record.structure["goal"] + \
            record.structure["steps"][0]["target_name"] + "".join(record.tags)
        assert "bob@client.com" not in flat
        assert MARKER_RE.search(record.summary)
        # title had no machine-shaped PII → unchanged
        assert record.title == "reply_to_client_email"
        # but a title WITH an email gets the regex net
        r2 = AbstractionRecord(kind="task", title="email_bob@client.com_reply",
                               summary="", apps=["Mail"])
        v.tokenize_record(r2)
        assert "bob@client.com" not in r2.title
    print("OK  test_tokenize_record_fields")


def test_redact_fallback():
    out = redact_text("mail sarah@example.com now")
    assert "sarah@example.com" not in out
    assert "[[redacted:email_address]]" in out

    # tokenize_or_redact with a forced-unavailable default vault
    ev._default_vault = None
    ev._default_vault_failed = True
    try:
        out2 = tokenize_or_redact("pay $42.50 to sarah@example.com")
        assert "sarah@example.com" not in out2 and "[[redacted:" in out2
    finally:
        ev._default_vault_failed = False
    print("OK  test_redact_fallback")


def test_search_token_grant_gated():
    with tempfile.TemporaryDirectory() as td:
        v = _vault(td)
        v.tokenize("Acme Corp", "project")
        # no grant → None (audited deny)
        assert v.search_token("Acme Corp", "project", "brain-local") is None
        v.grant("brain-local", ["project"], mode="search")
        token = v.search_token("acme  corp", "project", "brain-local")
        assert token == v.token_for("Acme Corp", "project")
        # value not in the vault → None even with a grant
        assert v.search_token("Globex", "project", "brain-local") is None
    print("OK  test_search_token_grant_gated")


def test_store_upsert_backstop_redacts():
    # Defense-in-depth: a producer that FAILS to tokenize cannot land
    # machine-shaped PII in a durable row — upsert redacts it (vault-free).
    from anyuse.watcher.storage import AbstractionStore
    with tempfile.TemporaryDirectory() as td:
        store = AbstractionStore(db_path=pathlib.Path(td) / "abs.db")
        rec = AbstractionRecord(
            kind="task",
            title="email_bob@leak.com_reply",
            summary="Replied to bob@leak.com about the $99.95 order",
            structure={"goal": "answer bob@leak.com",
                       "steps": [{"action": "click",
                                  "target_name": "Reply to bob@leak.com",
                                  "reason": "reply", "app": "Mail"}]},
            apps=["Mail"],
            tags=["bob@leak.com"],
        )
        stored = store.upsert(rec)
        got = store.get(stored.id)
        import json as _json
        flat = got.title + got.summary + _json.dumps(got.structure) + "".join(got.tags)
        assert "bob@leak.com" not in flat, "raw email must never reach a row"
        assert "$99.95" not in flat
        assert "[[redacted:email_address]]" in flat
    print("OK  test_store_upsert_backstop_redacts")


def main():
    print("=" * 64)
    print("entity vault — tests")
    print("=" * 64)
    test_token_determinism_and_canonicalisation()
    test_roundtrip_deref_with_grant()
    test_vault_grant_class_skips_presence()
    test_vault_presence_class_gated_by_presence()
    test_grant_expiry_revoke_mode_and_scope()
    test_never_captured_not_stored()
    test_regex_net_shapes()
    test_tokenize_text_entities_and_idempotency()
    test_tokenize_record_fields()
    test_redact_fallback()
    test_search_token_grant_gated()
    test_store_upsert_backstop_redacts()
    print("\nALL ENTITY VAULT TESTS PASSED")


if __name__ == "__main__":
    main()
