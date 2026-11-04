"""
Session Storage - Persistent storage for watch sessions.

Storage structure:
    ~/.anyuse/sessions/
    └── {session_id}/
        ├── session.json      # Session metadata + events
        └── anchors/
            ├── step_001_xxx.png
            └── ...
"""
import json
import math
import re
import shutil
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime

from anyuse.paths import anyuse_data_path
from anyuse.watcher.models import WatchSession, RawWorkflowDraft, AbstractionRecord
from anyuse.watcher import raw_store


class SessionStorage:
    """
    Persistent storage for watch sessions.

    Stores session metadata, events, and anchor images. session.json is
    encrypted at rest with k_raw (raw_store); reads are
    transparent, so legacy plaintext sessions keep loading.
    """

    # See anyuse/paths.py — ANYUSE_DATA_DIR moves this; default unchanged.
    BASE_DIR = anyuse_data_path("sessions")

    @classmethod
    def default_base_dir(cls) -> Path:
        return anyuse_data_path("sessions")

    def __init__(self, base_dir: Optional[Path] = None,
                 crypto: Optional[raw_store.RawArtifactCrypto] = None):
        """
        Initialize storage.

        Args:
            base_dir: Base directory for storage (default: ~/.anyuse/sessions)
            crypto: injectable raw-artifact crypto (tests inject a temp-keyed
                one); None = the default k_raw
        """
        self.base_dir = Path(base_dir) if base_dir else self.default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._crypto = crypto

        # Cache of loaded sessions
        self._cache: Dict[str, WatchSession] = {}

    def get_session_dir(self, session_id: str) -> Path:
        """Get the directory for a session."""
        return self.base_dir / session_id

    def _read_session_json(self, session_file: Path) -> Optional[Dict]:
        """Transparent (encrypted-or-legacy-plaintext) session.json read."""
        data = raw_store.read_json_transparent(session_file, self._crypto)
        return data if isinstance(data, dict) else None

    def save(self, session: WatchSession):
        """
        Save a session to disk (encrypted; skipped fail-closed when no crypto).

        Args:
            session: Session to save
        """
        session_dir = self.get_session_dir(session.id)
        session_dir.mkdir(parents=True, exist_ok=True)

        # Save session metadata and events
        session_file = session_dir / "session.json"
        raw_store.write_json_encrypted(session_file, session.to_dict(),
                                       self._crypto)

        # Update cache
        self._cache[session.id] = session

    def load(self, session_id: str) -> Optional[WatchSession]:
        """
        Load a session from disk.

        Args:
            session_id: ID of session to load

        Returns:
            WatchSession if found, None otherwise
        """
        # Check cache first
        if session_id in self._cache:
            return self._cache[session_id]

        session_dir = self.get_session_dir(session_id)
        session_file = session_dir / "session.json"

        if not session_file.exists():
            return None

        try:
            data = self._read_session_json(session_file)
            if data is None:
                return None

            session = WatchSession.from_dict(data)
            self._cache[session_id] = session
            return session

        except Exception as e:
            print(f"Warning: Failed to load session {session_id}: {e}")
            return None
    
    def delete(self, session_id: str) -> bool:
        """
        Delete a session and its files.
        
        Args:
            session_id: ID of session to delete
        
        Returns:
            True if deleted, False if not found
        """
        session_dir = self.get_session_dir(session_id)
        
        if session_dir.exists():
            shutil.rmtree(session_dir)
            self._cache.pop(session_id, None)
            return True
        
        return False
    
    def list_sessions(self) -> List[Dict]:
        """
        List all sessions with basic metadata.
        
        Returns:
            List of dicts with session info
        """
        sessions = []
        
        for session_dir in sorted(self.base_dir.iterdir(), reverse=True):
            if not session_dir.is_dir():
                continue
            
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue

            try:
                data = self._read_session_json(session_file)
                if data is None:
                    continue

                sessions.append({
                    "id": data["id"],
                    "started_at": data.get("started_at", ""),
                    "ended_at": data.get("ended_at"),
                    "status": data.get("status", "unknown"),
                    "events_count": len(data.get("events", [])) + len(data.get("step_events", [])),
                    "clicks_count": data.get("total_clicks", 0),
                    "workflows_count": data.get("workflows_detected", 0),
                    "apps_used": data.get("apps_used", []),
                })
                
            except Exception:
                continue
        
        return sessions
    
    def get_recent_sessions(self, n: int = 10) -> List[WatchSession]:
        """
        Get the N most recent sessions.
        
        Args:
            n: Number of sessions to return
        
        Returns:
            List of WatchSession objects
        """
        session_infos = self.list_sessions()[:n]
        sessions = []
        
        for info in session_infos:
            session = self.load(info["id"])
            if session:
                sessions.append(session)
        
        return sessions
    
    def get_sessions_with_workflow(self, workflow_name: str) -> List[WatchSession]:
        """
        Get all sessions containing a specific workflow.
        
        Args:
            workflow_name: Name of workflow to search for
        
        Returns:
            List of sessions containing the workflow
        """
        matching = []
        
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue

            try:
                data = self._read_session_json(session_file)
                if data is None:
                    continue

                # Check events for workflow
                for event in data.get("events", []):
                    if event.get("workflow_name") == workflow_name:
                        session = self.load(data["id"])
                        if session:
                            matching.append(session)
                        break
                        
            except Exception:
                continue
        
        return matching
    
    def get_anchor_path(self, session_id: str, anchor_filename: str) -> Path:
        """Get the full path to an anchor image."""
        return self.get_session_dir(session_id) / "anchors" / anchor_filename
    
    def cleanup_old_sessions(self, max_age_days: int = 30):
        """
        Delete sessions older than max_age_days.
        
        Args:
            max_age_days: Maximum age in days
        """
        cutoff = datetime.now().timestamp() - (max_age_days * 24 * 60 * 60)
        
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue
            
            try:
                # Check modification time
                if session_file.stat().st_mtime < cutoff:
                    shutil.rmtree(session_dir)
                    print(f"Cleaned up old session: {session_dir.name}")
                    
            except Exception:
                continue
    
    def get_storage_stats(self) -> Dict:
        """Get storage statistics."""
        total_size = 0
        session_count = 0
        anchor_count = 0
        
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            
            session_count += 1
            
            for file in session_dir.rglob("*"):
                if file.is_file():
                    total_size += file.stat().st_size
                    if file.suffix == ".png":
                        anchor_count += 1
        
        return {
            "session_count": session_count,
            "anchor_count": anchor_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }


# Column order of the abstractions table — kept in one place so row tuples and
# ``_row_to_record`` never drift apart.
_ABS_COLUMNS = (
    "id", "kind", "title", "summary", "structure", "apps", "domains", "tags",
    "confidence", "seen_count", "session_id", "memory_ref", "privacy_tier",
    "created_at", "updated_at",
)


_MAX_JSON_DEPTH = 512


def _nesting_depth(text) -> int:
    """Deepest bracket nesting outside string literals (linear, no parse)."""
    depth = deepest = 0
    in_str = esc = False
    for ch in str(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


def _loads(text) -> object:
    """Best-effort JSON decode of a stored TEXT column."""
    if not text:
        return None
    if _nesting_depth(text) > _MAX_JSON_DEPTH:
        # Python 3.14's C decoder parses thousands of nested brackets without a
        # RecursionError, so the guard below no longer fires on its own; a
        # stored TEXT column has no business nesting this deep either way.
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError,
            RecursionError):
        # TODO #14: OverflowError (huge numeric literals) + RecursionError
        # (pathologically nested JSON) join the parse guards so a corrupt row
        # degrades to None instead of taking down a find()/scan.
        return None


def _parse_iso(ts) -> Optional[datetime]:
    """Best-effort ISO-8601 parse; None on anything unparseable (never raises)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _recency_weight(updated_at, half_life_days: float = 30.0) -> float:
    """0..1 recency score with a ~``half_life_days`` half-life. Neutral 0.5 when
    the timestamp is missing/unparseable so recency never dominates on bad data
    (TODO #14: rank beyond raw FTS relevance)."""
    dt = _parse_iso(updated_at)
    if dt is None:
        return 0.5
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    try:
        age_days = max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)
        return 0.5 ** (age_days / half_life_days)
    except (TypeError, ValueError, OverflowError):
        return 0.5


def _importance_weight(seen_count) -> float:
    """0..1 importance from ``seen_count``, log-scaled and saturating: a much
    re-observed abstraction ranks higher, but not linearly."""
    try:
        n = max(0, int(seen_count))
    except (TypeError, ValueError):
        n = 0
    return min(1.0, math.log1p(n) / math.log1p(20))


class AbstractionStore:
    """Persisted queryable index of :class:`AbstractionRecord`s.

    SQLite at ``~/.anyuse/abstractions.db``, owned by anyuse (Contract I
    sovereignty — this must NOT be written into commander.db). The durable,
    indefinite layer: abstractions are cheap text, so unlike raw obs / sessions
    they are never TTL-pruned here.

    Upsert is keyed on ``(kind, title, primary app)`` — a re-observation bumps
    ``seen_count``/``updated_at`` instead of minting a duplicate.

    Full-text search uses FTS5 when the runtime SQLite provides it, and falls
    back to LIKE scans otherwise (both return the same ``AbstractionRecord``s).

    PII backstop (defense-in-depth): ``upsert``
    runs the deterministic regex net over every durable text field and REDACTS
    machine-shaped PII (emails/phones/cards/amounts) that a producer failed to
    tokenize — a raw email address cannot reach a row even by mistake. This is
    deliberately vault-free (pure redaction, no keys, no Keychain): producers
    are responsible for real derefable tokens; a backstop hit means a producer
    bug and loses derefability rather than leaking.
    """

    DB_PATH = anyuse_data_path("abstractions.db")

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else self.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts = False
        self._init_db()

    # -- schema -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS abstractions(
                    id TEXT PRIMARY KEY, kind TEXT, title TEXT, summary TEXT,
                    structure TEXT, apps TEXT, domains TEXT, tags TEXT,
                    confidence REAL, seen_count INTEGER DEFAULT 1,
                    session_id TEXT, memory_ref TEXT,
                    privacy_tier TEXT DEFAULT 'local_only',
                    created_at TEXT, updated_at TEXT)"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_abs_kind_app "
                "ON abstractions(kind, created_at)"
            )
            # FTS5 virtual table (external content over abstractions) + sync
            # triggers. Guard for SQLite builds without the fts5 extension.
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS abstractions_fts "
                    "USING fts5(title, summary, tags, content=abstractions)"
                )
                conn.execute(
                    """CREATE TRIGGER IF NOT EXISTS abstractions_ai
                       AFTER INSERT ON abstractions BEGIN
                         INSERT INTO abstractions_fts(rowid, title, summary, tags)
                         VALUES (new.rowid, new.title, new.summary, new.tags);
                       END"""
                )
                conn.execute(
                    """CREATE TRIGGER IF NOT EXISTS abstractions_ad
                       AFTER DELETE ON abstractions BEGIN
                         INSERT INTO abstractions_fts(abstractions_fts, rowid, title, summary, tags)
                         VALUES ('delete', old.rowid, old.title, old.summary, old.tags);
                       END"""
                )
                conn.execute(
                    """CREATE TRIGGER IF NOT EXISTS abstractions_au
                       AFTER UPDATE ON abstractions BEGIN
                         INSERT INTO abstractions_fts(abstractions_fts, rowid, title, summary, tags)
                         VALUES ('delete', old.rowid, old.title, old.summary, old.tags);
                         INSERT INTO abstractions_fts(rowid, title, summary, tags)
                         VALUES (new.rowid, new.title, new.summary, new.tags);
                       END"""
                )
                self._fts = True
            except sqlite3.OperationalError:
                # FTS5 not compiled in — LIKE fallback covers find().
                self._fts = False
            conn.commit()
        finally:
            conn.close()

    # -- row <-> record -----------------------------------------------------

    @staticmethod
    def _row_to_record(row) -> AbstractionRecord:
        (id_, kind, title, summary, structure, apps, domains, tags,
         confidence, seen_count, session_id, memory_ref, privacy_tier,
         created_at, updated_at) = row
        return AbstractionRecord(
            kind=kind,
            title=title,
            summary=summary or "",
            structure=_loads(structure) or {},
            id=id_,
            created_at=created_at or "",
            apps=_loads(apps) or [],
            domains=_loads(domains) or [],
            tags=_loads(tags) or [],
            confidence=confidence if confidence is not None else 0.0,
            seen_count=seen_count if seen_count is not None else 1,
            provenance={"session_id": session_id or ""},
            memory_ref=memory_ref or "",
            privacy_tier=privacy_tier or "local_only",
        )

    # -- PII backstop ---------------------------------------------------------

    @staticmethod
    def _redact_backstop(record: AbstractionRecord) -> AbstractionRecord:
        """Redact machine-shaped PII from the record's durable text fields
        (best-effort, never raises; see class docstring)."""
        try:
            from anyuse.watcher.entity_vault import redact_text
        except Exception:
            return record
        try:
            record.title = redact_text(record.title or "")
            record.summary = redact_text(record.summary or "")
            if isinstance(record.tags, list):
                record.tags = [redact_text(t) for t in record.tags]
            structure = record.structure or {}
            if isinstance(structure, dict):
                for key, val in list(structure.items()):
                    if isinstance(val, str) and val:
                        structure[key] = redact_text(val)
                    elif isinstance(val, list):
                        structure[key] = [redact_text(x) if isinstance(x, str)
                                          else x for x in val]
                for step in structure.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    for key in ("target_name", "reason"):
                        if step.get(key):
                            step[key] = redact_text(step[key])
        except Exception:
            pass
        return record

    # -- writes -------------------------------------------------------------

    def upsert(self, record: AbstractionRecord) -> AbstractionRecord:
        """Insert ``record``, or merge into an existing one sharing
        ``(kind, title, primary app)`` by bumping ``seen_count``/``updated_at``.

        Returns the stored record (with the merged id/seen_count/confidence when
        a merge happened).
        """
        record = self._redact_backstop(record)
        primary_app = record.primary_app
        session_id = (record.provenance or {}).get("session_id", "") if isinstance(record.provenance, dict) else ""
        now = datetime.now().isoformat()

        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT id, apps, seen_count, confidence FROM abstractions "
                "WHERE kind = ? AND title = ?",
                (record.kind, record.title),
            )
            match = None
            for rid, apps_json, seen, conf in cur.fetchall():
                existing_apps = _loads(apps_json) or []
                existing_primary = existing_apps[0] if existing_apps else ""
                if existing_primary == primary_app:
                    match = (rid, seen, conf)
                    break

            if match:
                rid, seen, conf = match
                new_seen = (seen or 1) + 1
                new_conf = max(conf if conf is not None else 0.0, record.confidence)
                conn.execute(
                    """UPDATE abstractions SET summary = ?, structure = ?, apps = ?,
                       domains = ?, tags = ?, confidence = ?, seen_count = ?,
                       session_id = ?, memory_ref = ?, privacy_tier = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        record.summary,
                        json.dumps(record.structure, ensure_ascii=False),
                        json.dumps(record.apps, ensure_ascii=False),
                        json.dumps(record.domains, ensure_ascii=False),
                        json.dumps(record.tags, ensure_ascii=False),
                        new_conf,
                        new_seen,
                        session_id,
                        record.memory_ref,
                        record.privacy_tier,
                        now,
                        rid,
                    ),
                )
                conn.commit()
                record.id = rid
                record.seen_count = new_seen
                record.confidence = new_conf
                return record

            conn.execute(
                """INSERT INTO abstractions(
                       id, kind, title, summary, structure, apps, domains, tags,
                       confidence, seen_count, session_id, memory_ref,
                       privacy_tier, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.kind,
                    record.title,
                    record.summary,
                    json.dumps(record.structure, ensure_ascii=False),
                    json.dumps(record.apps, ensure_ascii=False),
                    json.dumps(record.domains, ensure_ascii=False),
                    json.dumps(record.tags, ensure_ascii=False),
                    record.confidence,
                    record.seen_count,
                    session_id,
                    record.memory_ref,
                    record.privacy_tier,
                    record.created_at,
                    now,
                ),
            )
            conn.commit()
            return record
        finally:
            conn.close()

    # -- reads --------------------------------------------------------------

    def get(self, record_id: str) -> Optional[AbstractionRecord]:
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {', '.join(_ABS_COLUMNS)} FROM abstractions WHERE id = ?",
                (record_id,),
            )
            row = cur.fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def find(
        self,
        query: str,
        kinds: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[AbstractionRecord]:
        """Full-text (or LIKE-fallback) search over title/summary/tags.

        ``kinds`` filters by ``AbstractionRecord.kind`` (load-bearing when the
        the host side shares one table across writers). Returns up to ``limit``
        records.
        """
        cols = ", ".join(f"a.{c}" for c in _ABS_COLUMNS)
        conn = self._connect()
        try:
            rows = []
            match_expr = self._fts_match_expr(query) if self._fts else None
            # Over-fetch a candidate pool so recency/importance can re-rank
            # beyond raw FTS ``rank`` before we truncate to ``limit``.
            pool = max(limit * 5, 50)
            if match_expr:
                try:
                    cur = conn.execute(
                        f"SELECT {cols} FROM abstractions a "
                        "JOIN abstractions_fts f ON a.rowid = f.rowid "
                        "WHERE abstractions_fts MATCH ? ORDER BY rank LIMIT ?",
                        (match_expr, pool),
                    )
                    rows = cur.fetchall()
                except sqlite3.OperationalError:
                    rows = self._like_search(conn, query, pool)
            else:
                rows = self._like_search(conn, query, pool)

            n = len(rows)
            scored = []
            for i, row in enumerate(rows):
                record = self._row_to_record(row)
                if kinds and record.kind not in kinds:
                    continue
                # rows arrive best-first (FTS rank / LIKE order); position is the
                # relevance signal, blended with recency + importance so a
                # slightly-less-relevant but fresh/frequent row can surface.
                rel = 1.0 - (i / n) if n else 0.0
                updated_at = row[-1]  # _ABS_COLUMNS[-1] == "updated_at"
                score = (0.60 * rel
                         + 0.25 * _recency_weight(updated_at)
                         + 0.15 * _importance_weight(record.seen_count))
                scored.append((score, i, record))
            # Highest score first; original order is a stable tiebreak.
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [rec for _, _, rec in scored[:limit]]
        finally:
            conn.close()

    def updated_since(
        self, since_iso: str, limit: int = 200
    ) -> List[tuple]:
        """Records touched after ``since_iso``, oldest-first, as
        ``(updated_at, AbstractionRecord)`` tuples — the watcher service's
        Push cursor scan. ``since_iso`` may be
        empty to mean "everything"."""
        cols = ", ".join(_ABS_COLUMNS)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {cols} FROM abstractions WHERE updated_at > ? "
                "ORDER BY updated_at ASC LIMIT ?",
                (since_iso or "", limit),
            )
            return [(row[-1] or "", self._row_to_record(row))
                    for row in cur.fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _fts_match_expr(query: str) -> Optional[str]:
        """Turn a free-text query into a safe FTS5 MATCH expression.

        TODO #14: the old ``[A-Za-z0-9_]+`` net dropped accented / non-Latin
        letters (``café`` -> ``caf``) and split poorly on punctuation, lowering
        recall on chatty or international text. ``[^\\W_]+`` (Unicode) keeps whole
        letter/digit runs and splits on underscore + punctuation exactly the way
        FTS5's ``unicode61`` tokenizer splits the stored content, so query tokens
        line up 1:1 with indexed tokens. Tokens are lowercased — this case-folds
        and also neutralises FTS5's uppercase boolean keywords (AND/OR/NOT/NEAR),
        so free text can't inject an operator; word-char tokens carry no FTS5
        syntax, so interpolation is safe. Tokens >= 3 chars get a prefix ``*`` for
        partial-word recall (``term`` -> ``terminal``). Returns None when nothing
        is matchable (caller falls back to recent listing / LIKE).
        """
        if not query:
            return None
        tokens = re.findall(r"[^\W_]+", query, re.UNICODE)
        if not tokens:
            return None
        parts = [f"{t.lower()}*" if len(t) >= 3 else t.lower() for t in tokens]
        return " OR ".join(parts)

    @staticmethod
    def _like_search(conn: sqlite3.Connection, query: str,
                     pool: int = 200):
        """LIKE fallback for SQLite builds without FTS5 (the normal path is the
        FTS index in ``find``; leading-wildcard LIKE is a scan, so it is kept
        fallback-only). Unicode-aware, punctuation-splitting tokens are matched
        ANY-token-in-ANY-field so chatty multi-word queries recall instead of
        needing one contiguous substring (TODO #14). Bounded by ``pool``."""
        cols = ", ".join(_ABS_COLUMNS)
        if not query or not query.strip():
            cur = conn.execute(
                f"SELECT {cols} FROM abstractions "
                "ORDER BY updated_at DESC, seen_count DESC LIMIT ?",
                (pool,),
            )
            return cur.fetchall()
        tokens = re.findall(r"[^\W_]+", query, re.UNICODE) or [query.strip()]
        clauses, params = [], []
        for tok in tokens:
            pat = f"%{tok.lower()}%"
            clauses.append(
                "(lower(title) LIKE ? OR lower(summary) LIKE ? "
                "OR lower(tags) LIKE ?)"
            )
            params.extend([pat, pat, pat])
        params.append(pool)
        cur = conn.execute(
            f"SELECT {cols} FROM abstractions WHERE {' OR '.join(clauses)} "
            "ORDER BY seen_count DESC, updated_at DESC LIMIT ?",
            params,
        )
        return cur.fetchall()

    def app_graph(self, app: str) -> Dict[str, object]:
        """Return the per-app UI state graph assembled from ``app_transition``
        records — local-only (consumed by the anyuse replay planner, never shared).
        """
        cols = ", ".join(_ABS_COLUMNS)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {cols} FROM abstractions WHERE kind = 'app_transition'"
            )
            transitions = []
            states = set()
            for row in cur.fetchall():
                record = self._row_to_record(row)
                structure = record.structure or {}
                rec_app = structure.get("app") or record.primary_app
                if rec_app != app:
                    continue
                frm = structure.get("from_state_id")
                to = structure.get("to_state_id")
                transitions.append({
                    "from_state_id": frm,
                    "to_state_id": to,
                    "action": structure.get("action"),
                    "element": structure.get("element"),
                    "memory_ref": record.memory_ref,
                    "seen_count": record.seen_count,
                })
                if frm:
                    states.add(frm)
                if to:
                    states.add(to)
            return {
                "app": app,
                "states": sorted(states),
                "transitions": transitions,
            }
        finally:
            conn.close()
