"""Tests for the watcher abstraction layer.

Covers abstraction storage, historical session compatibility, step configuration,
and composer text refinement, explicit cloud consent and basic fallback.

Self-executing (pytest is not installed in this env). Never writes to the real
~/.anyuse — every AbstractionStore uses a tmp db path — and never reaches the
macOS login Keychain: the raw-artifact key is INJECTED below. Before that injection this was the one and only
test in the suite that called ``keyring.set_password``, minting AnyUse's
``k_raw`` as a side effect of running tests and, under a sandboxed HOME,
raising a Security-framework dialog on the user's desktop.

Run from the anyuse package dir:
    cd anyuse && .venv/bin/python test_watcher_abstractions.py
"""

import os
import pathlib
import sys
import tempfile

# anyuse uses mixed flat + package imports → both the package dir and its parent
# must be importable.
_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))            # flat: import config / context / execution
# Resolve `anyuse` to THIS checkout, whatever its directory is called. The old
# `sys.path.insert(0, str(_HERE.parent))` picked up whichever sibling directory
# was named `anyuse` — in a worktree called anyuse-desktop that is the OTHER
# branch's checkout, and 27 test files were quietly testing it.
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.watcher.models import (
    AbstractionRecord, TagEvent, TagType, RawWorkflowDraft, WatchSession,
)
from anyuse.watcher.storage import AbstractionStore
from anyuse.watcher.composer import WorkflowComposer
from anyuse.watcher.batch_watcher import BatchWatcherConfig

# --- key custody: never the real Keychain -----------------------------------
# Keep raw-artifact access isolated from real keys for the whole file.
from cryptography.fernet import Fernet as _Fernet
from anyuse.watcher import raw_store as _raw_store

_raw_store.set_default_raw_crypto(_raw_store.RawArtifactCrypto(key=_Fernet.generate_key()))

from datetime import datetime
import uuid


class _NoRefineBackend:
    """Injected into WorkflowComposer to force the deterministic no-AI basic
    refinement path — returns no JSON, so compose() falls back to basic +
    event-derived steps without calling a model."""
    name = "no-refine-stub"

    def generate_text_sync(self, prompt, max_tokens=2000, json_mode=False):
        return ""


# ---------------------------------------------------------------------------
# 1. AbstractionRecord roundtrip
# ---------------------------------------------------------------------------

def test_record_roundtrip():
    for kind, structure in (
        ("task", {"goal": "reply to email", "trigger_phrases": ["reply"],
                  "steps": [{"action": "click", "target_role": "button",
                             "target_name": "Reply", "app": "Gmail", "reason": "to reply"}]}),
        ("app_transition", {"app": "Gmail", "from_state_id": "s1", "to_state_id": "s2",
                            "action": "click", "element": {"role": "button", "name": "Reply"}}),
        ("step_pattern", {"action": "click", "target_role": "button",
                          "target_name": "Reply", "app": "Gmail", "frequency": 4}),
    ):
        rec = AbstractionRecord(
            kind=kind,
            title="reply_email",
            summary="The human replies to an email in Gmail.",
            structure=structure,
            apps=["Gmail"],
            domains=["mail.google.com"],
            tags=["email", "gmail"],
            confidence=0.8,
            seen_count=3,
            provenance={"session_id": "sess123", "obs_batch_range": "1-5", "anchor_ids": ["a1"]},
        )
        # memory_ref auto-derived from session_id + id
        assert rec.memory_ref == f"anyuse:session/sess123#abs/{rec.id}", rec.memory_ref

        d = rec.to_dict()
        rec2 = AbstractionRecord.from_dict(d)

        assert rec2.to_dict() == d, f"{kind} roundtrip mismatch"
        assert rec2.kind == kind
        assert rec2.structure == structure
        assert rec2.time_range == (None, None)
        assert rec2.primary_app == "Gmail"
        assert rec2.memory_ref == rec.memory_ref

    # No session id => memory_ref still well-formed
    r = AbstractionRecord(kind="task", title="x")
    assert r.memory_ref == f"anyuse:session/#abs/{r.id}", r.memory_ref
    print("OK  test_record_roundtrip")


def test_watchsession_raw_observations_field():
    # raw_observations is a real serialized field now (no more hasattr bolt-on).
    s = WatchSession.create_new()
    s.raw_observations.append({"batch_number": 1, "ai_response": "hello"})
    d = s.to_dict()
    assert d["raw_observations"][0]["ai_response"] == "hello", d
    s2 = WatchSession.from_dict(d)
    assert s2.raw_observations == s.raw_observations
    # Absent key deserialises to an empty list (back-compat with old sessions).
    s3 = WatchSession.from_dict({"id": "x", "started_at": s.started_at.isoformat()})
    assert s3.raw_observations == []
    print("OK  test_watchsession_raw_observations_field")


# ---------------------------------------------------------------------------
# 2. AbstractionStore upsert-merge / find / get / app_graph
# ---------------------------------------------------------------------------

def _store(tmp):
    return AbstractionStore(db_path=pathlib.Path(tmp) / "abstractions.db")

def _vault(tmp):
    # Injected everywhere a producer could lazily reach get_default_vault():
    # tests must never touch the real ~/.anyuse or the macOS Keychain.
    from cryptography.fernet import Fernet
    from anyuse.watcher.entity_vault import EntityVault
    return EntityVault(
        db_path=pathlib.Path(tmp) / "entity_vault.db",
        k_tok=b"\x02" * 32,
        k_enc=Fernet.generate_key(),
        presence_provider=lambda reason: True,
    )

def _task(title, app, summary="", conf=0.6, session="s1", tags=None):
    return AbstractionRecord(
        kind="task", title=title, summary=summary,
        structure={"goal": summary, "steps": []},
        apps=[app] if app else [], tags=tags or [], confidence=conf,
        provenance={"session_id": session},
    )

def test_store_upsert_merge():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        r1 = store.upsert(_task("reply_email", "Gmail", "reply v1", conf=0.6))
        first_id = r1.id
        assert r1.seen_count == 1

        # Same (kind, title, primary app) => merge, bump seen_count, keep id, max conf
        r2 = store.upsert(_task("reply_email", "Gmail", "reply v2", conf=0.9))
        assert r2.id == first_id, "merge must reuse the existing id"
        assert r2.seen_count == 2, r2.seen_count
        assert r2.confidence == 0.9, r2.confidence

        got = store.get(first_id)
        assert got is not None and got.seen_count == 2
        assert got.summary == "reply v2", "merge updates summary to newest"

        # Different primary app => distinct row
        r3 = store.upsert(_task("reply_email", "Outlook", "reply outlook"))
        assert r3.id != first_id
        assert r3.seen_count == 1

        # Different title => distinct row
        r4 = store.upsert(_task("compose_email", "Gmail", "compose"))
        assert r4.id != first_id

        # Exactly two rows share title reply_email (Gmail merged, Outlook new)
        rows = store.find("reply_email", limit=50)
        titles = sorted((r.title, r.primary_app) for r in rows)
        assert ("reply_email", "Gmail") in titles
        assert ("reply_email", "Outlook") in titles
    print("OK  test_store_upsert_merge")

def test_store_find_and_get():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.upsert(_task("reply_email", "Gmail", "The human replies to an email", tags=["email"]))
        store.upsert(_task("open_terminal", "iTerm", "Opens a terminal window", tags=["shell"]))
        store.upsert(AbstractionRecord(
            kind="step_pattern", title="click_reply", summary="clicks reply button",
            structure={"action": "click", "target_name": "Reply", "frequency": 3},
            apps=["Gmail"], confidence=0.6, provenance={"session_id": "s1"}))

        # FTS path (env has fts5)
        assert store._fts is True, "expected FTS5 available in this environment"
        hits = store.find("terminal")
        assert any(h.title == "open_terminal" for h in hits), [h.title for h in hits]

        # kinds filter is load-bearing (shared table)
        only_patterns = store.find("reply", kinds=["step_pattern"])
        assert only_patterns and all(h.kind == "step_pattern" for h in only_patterns)
        tasks_only = store.find("reply", kinds=["task"])
        assert all(h.kind == "task" for h in tasks_only)

        # LIKE fallback branch (force FTS off) returns the same record
        store._fts = False
        hits2 = store.find("terminal")
        assert any(h.title == "open_terminal" for h in hits2), [h.title for h in hits2]
        # empty query => recent listing, not a crash
        assert len(store.find("")) >= 3

        assert store.get("nonexistent") is None
    print("OK  test_store_find_and_get")

def test_store_app_graph():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        def trans(app, frm, to):
            return AbstractionRecord(
                kind="app_transition", title=f"{app}_{frm}_{to}",
                summary=f"{frm}->{to}",
                structure={"app": app, "from_state_id": frm, "to_state_id": to,
                           "action": "click", "element": {"role": "button", "name": "Reply"}},
                apps=[app], confidence=0.7, provenance={"session_id": "s1"})
        store.upsert(trans("Gmail", "inbox", "email_detail"))
        store.upsert(trans("Gmail", "email_detail", "compose_reply"))
        store.upsert(trans("Safari", "tab_a", "tab_b"))

        g = store.app_graph("Gmail")
        assert g["app"] == "Gmail"
        assert len(g["transitions"]) == 2, g["transitions"]
        assert set(g["states"]) == {"inbox", "email_detail", "compose_reply"}, g["states"]
        # Safari transition is excluded
        assert all("tab_" not in (s or "") for s in g["states"])

        g_safari = store.app_graph("Safari")
        assert len(g_safari["transitions"]) == 1
        assert store.app_graph("Nonexistent")["transitions"] == []
    print("OK  test_store_app_graph")


# ---------------------------------------------------------------------------
# 4. Config defaults
# ---------------------------------------------------------------------------

def test_config_defaults():
    cfg = BatchWatcherConfig()
    assert cfg.steps_only is True
    assert not hasattr(cfg, "backend") and not hasattr(cfg, "keep_raw_observations")
    print("OK  test_config_defaults")


# ---------------------------------------------------------------------------
# 5. Composer task-record minting with coordinate stripping
# ---------------------------------------------------------------------------

def test_composer_mints_stripped_task_record():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        # _NoRefineBackend forces the no-AI basic refinement (steps == []) =>
        # steps derived from events, independent of the model configuration.
        composer = WorkflowComposer(api_key=None, abstraction_store=store,
                                    entity_vault=_vault(tmp),
                                    model_backend=_NoRefineBackend())

        ev = TagEvent(
            id=uuid.uuid4().hex[:12], tag_type=TagType.STEP, timestamp=datetime.now(),
            data={"action": "click"}, target="Reply button", reason="to reply",
            x=450, y=120,
        )
        draft = RawWorkflowDraft(
            name="reply_email", goal="reply to an email",
            events=[ev], apps_involved=["Gmail"],
            urls_visited=["https://mail.google.com/inbox"], session_id="sess-xyz",
        )

        wf = composer.compose(draft)
        # Concrete workflow keeps coordinates for replay.
        assert wf.steps and wf.steps[0].args.get("x") == 450

        # Abstraction record minted, keyed task/reply_email/Gmail.
        recs = store.find("reply_email", kinds=["task"])
        assert recs, "composer must mint a kind=task abstraction"
        rec = recs[0]
        assert rec.kind == "task"
        assert rec.title == "reply_email"
        assert rec.primary_app == "Gmail"
        assert rec.memory_ref == f"anyuse:session/sess-xyz#abs/{rec.id}"
        assert "mail.google.com" in rec.domains, rec.domains

        steps = rec.structure["steps"]
        assert len(steps) == 1, steps
        step = steps[0]
        # Coordinates STRIPPED; generalizable target retained.
        assert "x" not in step and "y" not in step, step
        assert "filename" not in step and "anchor" not in step, step
        assert step["target_name"] == "Reply button", step
        assert step["app"] == "Gmail"
        assert step["action"] == "click"
    print("OK  test_composer_mints_stripped_task_record")


def test_composer_snakecases_title_and_merges():
    # Regression: composer must snake_case the title so a task first distilled by
    # intent analysis ("reply_email") merges with the later composed record instead
    # of minting a duplicate row (spec §2 title:snake_case + §4 merge-not-duplicate).
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        # Pre-existing record as intent analysis would have written it.
        pre = store.upsert(_task("reply_email", "Gmail", "reply v1", conf=0.6))
        pre_id = pre.id

        # Composer sees a non-conforming display name "Reply Email".
        composer = WorkflowComposer(api_key=None, abstraction_store=store,
                                    entity_vault=_vault(tmp),
                                    model_backend=_NoRefineBackend())
        draft = RawWorkflowDraft(
            name="Reply Email", goal="reply to an email",
            events=[], apps_involved=["Gmail"],
            urls_visited=[], session_id="sess-xyz",
        )
        composer.compose(draft)

        recs = store.find("reply_email", kinds=["task"])
        titles = {r.title for r in recs}
        assert titles == {"reply_email"}, f"title must be snake_cased, got {titles}"
        gmail_recs = [r for r in recs if r.primary_app == "Gmail"]
        assert len(gmail_recs) == 1, f"must MERGE, not duplicate: {gmail_recs}"
        assert gmail_recs[0].id == pre_id, "merged record must reuse the intent-analysis id"
        assert gmail_recs[0].seen_count >= 2, "merge must bump seen_count"
    print("OK  test_composer_snakecases_title_and_merges")


# ---------------------------------------------------------------------------
# Store recall
# ---------------------------------------------------------------------------


def test_store_recall_unicode_punct_and_ranking():
    """TODO #14: Unicode/punctuation-aware recall + recency/importance ranking.

    Locks in the fixes so the ASCII-only tokenizer / rank-only ordering / narrow
    parse guard can't silently regress.
    """
    from anyuse.watcher.storage import (
        _recency_weight, _importance_weight, _loads,
    )
    fx = AbstractionStore._fts_match_expr

    # -- tokenizer: the core recall fix (deterministic, no DB) --------------
    # accented letters survive whole instead of truncating ("café" -> "caf")
    assert fx("café") == "café*", fx("café")
    assert fx("Müller") == "müller*", fx("Müller")
    # underscore + punctuation split into aligned sub-tokens (prefix, OR)
    assert fx("open_terminal") == "open* OR terminal*", fx("open_terminal")
    assert fx("e-mail") == "e OR mail*", fx("e-mail")
    # short tokens stay exact; uppercase boolean keywords are neutralised so
    # free text cannot inject an FTS5 operator
    assert fx("or") == "or" and fx("OR") == "or", fx("OR")
    assert fx("term") == "term*", fx("term")
    assert fx("") is None and fx("!!!") is None      # nothing matchable

    # -- end-to-end: accented / partial / sub-token queries recall ----------
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.upsert(_task("open_terminal", "iTerm",
                           "the terminal window is réservé", tags=["café"]))
        assert store._fts is True
        # accented query (old ASCII net -> "rserv"/"caf" -> would miss)
        assert any(h.title == "open_terminal" for h in store.find("réservé")), \
            "unicode summary recall"
        assert any(h.title == "open_terminal" for h in store.find("café")), \
            "unicode tag recall"
        # prefix recall: a partial word finds the whole
        assert any(h.title == "open_terminal" for h in store.find("term")), \
            "prefix recall"
        # sub-token recall: one underscore-half alone finds it (the old
        # single-token form required the whole "open terminal" phrase)
        assert any(h.title == "open_terminal" for h in store.find("terminal")), \
            "subtoken recall"

    # -- ranking signals: monotonic, bounded, guarded ----------------------
    assert 0.0 <= _importance_weight(0) < _importance_weight(1) \
        < _importance_weight(10) <= 1.0
    assert _importance_weight("bad") == _importance_weight(0)   # guarded, no raise
    assert _recency_weight(datetime.now().isoformat()) \
        > _recency_weight("2000-01-01T00:00:00")
    assert _recency_weight(None) == 0.5 and _recency_weight("garbage") == 0.5

    # -- parse guard: pathological JSON degrades to None, never raises ------
    assert _loads("[" * 6000 + "]" * 6000) is None   # RecursionError guarded
    assert _loads("not json") is None and _loads(None) is None

    print("OK  test_store_recall_unicode_punct_and_ranking")


def test_composer_cloud_consent_and_fallback():
    from unittest.mock import patch
    from anyuse.watcher.backends import ObserverModel
    draft = RawWorkflowDraft(name="open_mail", goal="Open mail", events=[])
    with patch.dict(os.environ, {"ANYUSE_COMPOSER_CLOUD": "", "GEMINI_API_KEY": "test-key"}):
        composer = WorkflowComposer()
        assert composer._get_model_backend() is None
        assert composer._refine_with_model(draft)["name"] == draft.name
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "ANYUSE_COMPOSER_CLOUD": "1", "GEMINI_API_KEY": "",
            "ANYUSE_VISUAL_MODEL_PROVIDER": "gemini",
            "ANYUSE_VISUAL_MODEL_API_KEY_ENV": "GEMINI_API_KEY",
            "ANYUSE_MODEL_ENV_FILE": str(pathlib.Path(td) / "missing.env")}):
        assert WorkflowComposer()._get_model_backend() is None
    with patch.dict(os.environ, {"ANYUSE_COMPOSER_CLOUD": "1", "GEMINI_API_KEY": "test-key"}):
        class TextBackend(ObserverModel):
            def is_available(self):
                return True

            def generate_text_sync(self, prompt, max_tokens=2000, json_mode=False):
                assert "open_mail" in prompt and json_mode
                return '{"name": "refined_mail", "description": "Open mail"}'
        backend = TextBackend()
        with patch("anyuse.watcher.backends.TextRefinementBackend", return_value=backend) as factory:
            composer = WorkflowComposer()
            assert composer._refine_with_model(draft)["name"] == "refined_mail"
            assert composer._get_model_backend() is backend
            assert factory.call_count == 1
    # An explicitly injected backend continues to work without cloud selection.
    assert WorkflowComposer(model_backend=backend)._get_model_backend() is backend


def test_composer_text_backend():
    from types import SimpleNamespace
    from anyuse.watcher.backends import TextRefinementBackend
    calls = []
    class Adapter:
        def complete(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return SimpleNamespace(ok=True, text='{"name":"mail"}')
    backend = TextRefinementBackend(adapter=Adapter())
    assert backend.generate_text_sync("refine mail", json_mode=True) == '{"name":"mail"}'
    assert calls[0][0] == [{"role": "user", "content": "refine mail"}]
    assert "images" not in calls[0][1] and calls[0][1]["json_schema"]


def test_composer_resolves_provider_and_model_together():
    """Exercise automatic backend selection and real ModelAdapter HTTP requests."""
    import json
    import httpx
    from unittest.mock import patch
    from anyuse.execution.model_adapter import ModelAdapter, resolve_model_config
    from anyuse.watcher.backends import TextRefinementBackend

    draft = RawWorkflowDraft(name="open_mail", goal="Open mail", events=[])
    cases = (
        ("gemini", "gemini-visual-configured", "anthropic", "claude-planner-configured",
         "generativelanguage.googleapis.com"),
        ("anthropic", "claude-visual-configured", "gemini", "gemini-planner-configured",
         "api.anthropic.com"),
    )
    for provider, model, planner_provider, planner_model, host in cases:
        with patch.dict(os.environ, {
                "ANYUSE_COMPOSER_CLOUD": "1",
                "ANYUSE_VISUAL_MODEL_PROVIDER": provider,
                "ANYUSE_VISUAL_MODEL_NAME": model,
                "ANYUSE_VISUAL_MODEL_BASE_URL": "",
                "ANYUSE_VISUAL_MODEL_API_KEY_ENV": "TEST_COMPOSER_KEY",
                "ANYUSE_PLANNER_MODEL_PROVIDER": planner_provider,
                "ANYUSE_PLANNER_MODEL_NAME": planner_model,
                "TEST_COMPOSER_KEY": "test-key",
                "GEMINI_API_KEY": "", "ANYUSE_MODEL_API_KEY_ENV": "",
                "ANYUSE_MODEL_BASE_URL": ""}):
            composer = WorkflowComposer()
            backend = composer._get_model_backend()  # no backend or adapter factory mock
            assert isinstance(backend, TextRefinementBackend)
            adapter = backend._adapter
            assert isinstance(adapter, ModelAdapter)
            assert (adapter.config.provider, adapter.config.model) == (provider, model)
            assert backend.name == f"{provider} ({model})"
            # Composition retains main's visual role; planner overrides remain intact.
            planner = resolve_model_config("planner")
            assert (planner.provider, planner.model) == (planner_provider, planner_model)
            requests = []

            def respond(request):
                requests.append(request)
                assert request.url.host == host
                assert json.loads(request.content)["model"] == model
                return httpx.Response(200, json={"choices": [{"message": {
                    "content": '{"name":"refined_mail","description":"Open mail"}'}}]})

            adapter._transport = httpx.MockTransport(respond)
            assert composer._refine_with_model(draft)["name"] == "refined_mail"
            assert len(requests) == 1
            # A caller-supplied model overrides the name, with the same provider.
            explicit = WorkflowComposer(model="explicit-visual-model")._get_model_backend()
            assert (explicit._adapter.config.provider, explicit._adapter.config.model) == (
                provider, "explicit-visual-model")


def main():
    print("=" * 64)
    print("watcher abstraction layer — tests")
    print("=" * 64)
    test_record_roundtrip()
    test_watchsession_raw_observations_field()
    test_store_upsert_merge()
    test_store_find_and_get()
    test_store_app_graph()
    test_config_defaults()
    test_composer_cloud_consent_and_fallback()
    test_composer_text_backend()
    test_composer_resolves_provider_and_model_together()
    test_composer_mints_stripped_task_record()
    test_composer_snakecases_title_and_merges()
    test_store_recall_unicode_punct_and_ranking()
    print("\nALL WATCHER ABSTRACTION TESTS PASSED")


if __name__ == "__main__":
    main()
