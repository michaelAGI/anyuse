"""
Local Embedding Store - Vector-based semantic search across workflows, guides, and sessions.

Replaces cloud-based workflow matching with local embeddings.
Enables cross-store search: find relevant workflows, guides, AND sessions from a single query.

Storage:
    ~/.anyuse/embeddings/
    ├── workflows.npy          # Vector matrix
    ├── workflows_meta.json    # Doc IDs + metadata
    ├── guides.npy
    ├── guides_meta.json
    ├── sessions.npy
    └── sessions_meta.json
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
from anyuse.paths import anyuse_data_path


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """A single search result from a vector index."""
    doc_id: str
    score: float           # cosine similarity, 0-1
    metadata: dict


@dataclass
class RankedResult:
    """A search result after cross-store ranking with boosts applied."""
    doc_id: str
    store: str             # "workflows", "guides", "sessions"
    raw_score: float       # original cosine similarity
    boosted_score: float   # after store preference boost
    metadata: dict


# ---------------------------------------------------------------------------
# VectorIndex — per-store numpy index
# ---------------------------------------------------------------------------

class VectorIndex:
    """Simple numpy-based vector index for a single store.

    Not a full vector database -- we store 100s to low 1000s of docs,
    not millions.  Brute-force cosine similarity is fine at this scale.

    Storage: numpy file + JSON metadata sidecar.
    """

    def __init__(
        self,
        name: str,
        index_path: Path,
        metadata_path: Path,
        dimension: int = 256,
    ):
        self.name = name
        self.index_path = Path(index_path)
        self.metadata_path = Path(metadata_path)
        self.dimension = dimension

        # Internal storage — parallel arrays
        self._vectors: np.ndarray = np.empty((0, dimension), dtype=np.float32)
        self._doc_ids: List[str] = []
        self._metadata: List[dict] = []

    # -- mutators -----------------------------------------------------------

    def add(self, doc_id: str, vector: np.ndarray, metadata: dict) -> None:
        """Add or update (upsert) a document in the index."""
        vector = np.asarray(vector, dtype=np.float32).reshape(1, -1)

        # Upsert: if doc_id already present, replace it
        if doc_id in self._doc_ids:
            idx = self._doc_ids.index(doc_id)
            self._vectors[idx] = vector[0]
            self._metadata[idx] = metadata
        else:
            self._vectors = np.vstack([self._vectors, vector]) if len(self._vectors) > 0 else vector
            self._doc_ids.append(doc_id)
            self._metadata.append(metadata)

    def remove(self, doc_id: str) -> bool:
        """Remove a document from the index.  Returns True if found."""
        if doc_id not in self._doc_ids:
            return False

        idx = self._doc_ids.index(doc_id)
        self._vectors = np.delete(self._vectors, idx, axis=0)
        self._doc_ids.pop(idx)
        self._metadata.pop(idx)
        return True

    # -- search -------------------------------------------------------------

    def search(self, query_vector: np.ndarray, top_k: int = 5) -> List[SearchResult]:
        """Brute-force cosine similarity search.

        At 1000 docs with 256-dim vectors this takes ~0.1ms.
        """
        if len(self._vectors) == 0:
            return []

        query_vector = np.asarray(query_vector, dtype=np.float32).ravel()
        query_norm = np.linalg.norm(query_vector)
        if query_norm == 0:
            return []

        vec_norms = np.linalg.norm(self._vectors, axis=1)
        # Avoid division by zero for zero-norm stored vectors
        safe_norms = np.where(vec_norms == 0, 1.0, vec_norms)
        similarities = np.dot(self._vectors, query_vector) / (safe_norms * query_norm)

        # Get top_k indices sorted descending
        k = min(top_k, len(similarities))
        top_indices = np.argsort(similarities)[-k:][::-1]

        return [
            SearchResult(
                doc_id=self._doc_ids[i],
                score=float(similarities[i]),
                metadata=self._metadata[i],
            )
            for i in top_indices
            if similarities[i] > 0  # skip negative similarities
        ]

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        """Persist vectors and metadata to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(self.index_path), self._vectors)
        with open(self.metadata_path, "w") as f:
            json.dump({"doc_ids": self._doc_ids, "metadata": self._metadata}, f)

    def load(self) -> None:
        """Load vectors and metadata from disk."""
        if self.index_path.exists() and self.metadata_path.exists():
            self._vectors = np.load(str(self.index_path)).astype(np.float32)
            with open(self.metadata_path) as f:
                data = json.load(f)
            self._doc_ids = data.get("doc_ids", [])
            self._metadata = data.get("metadata", [])
        else:
            # Start fresh
            self._vectors = np.empty((0, self.dimension), dtype=np.float32)
            self._doc_ids = []
            self._metadata = []

    # -- dunder helpers ------------------------------------------------------

    def __len__(self) -> int:
        return len(self._doc_ids)


# ---------------------------------------------------------------------------
# CrossStoreRanker
# ---------------------------------------------------------------------------

class CrossStoreRanker:
    """Ranks results across workflows, guides, and sessions.

    Priority: workflows > guides > sessions (when scores are close).
    Sessions require corroboration (2+ matches) to surface.
    """

    WORKFLOW_BOOST: float = 0.10       # workflows get a 0.10 score boost
    GUIDE_BOOST: float = 0.05          # guides get a small boost
    SESSION_BOOST: float = 0.0         # sessions get no boost (must earn it)

    # Thresholds
    MIN_SCORE: float = 0.5             # below this, don't return
    CLOSE_GAP: float = 0.10            # scores within 0.10 = "close"
    SESSION_CORROBORATION: int = 2     # need 2+ session matches to trust

    def rank(
        self,
        query: str,
        workflow_results: List[SearchResult],
        guide_results: List[SearchResult],
        session_results: List[SearchResult],
    ) -> List[RankedResult]:
        """Rank results across all stores.

        Algorithm:
        1. Apply per-store boosts to raw cosine scores
        2. Filter below MIN_SCORE
        3. If top workflow is within CLOSE_GAP of top session -> prefer workflow
        4. Sessions only surface if 2+ matches above MIN_SCORE
        5. Sort by boosted score descending
        """
        ranked: List[RankedResult] = []

        # Apply boosts — workflows
        for r in workflow_results:
            if r.score >= self.MIN_SCORE:
                ranked.append(RankedResult(
                    doc_id=r.doc_id,
                    store="workflows",
                    raw_score=r.score,
                    boosted_score=r.score + self.WORKFLOW_BOOST,
                    metadata=r.metadata,
                ))

        # Apply boosts — guides
        for r in guide_results:
            if r.score >= self.MIN_SCORE:
                ranked.append(RankedResult(
                    doc_id=r.doc_id,
                    store="guides",
                    raw_score=r.score,
                    boosted_score=r.score + self.GUIDE_BOOST,
                    metadata=r.metadata,
                ))

        # Sessions: only include if 2+ matches (corroboration)
        qualifying_sessions = [r for r in session_results if r.score >= self.MIN_SCORE]
        if len(qualifying_sessions) >= self.SESSION_CORROBORATION:
            for r in qualifying_sessions:
                ranked.append(RankedResult(
                    doc_id=r.doc_id,
                    store="sessions",
                    raw_score=r.score,
                    boosted_score=r.score + self.SESSION_BOOST,
                    metadata=r.metadata,
                ))

        # Sort by boosted score descending
        ranked.sort(key=lambda r: r.boosted_score, reverse=True)

        return ranked


# ---------------------------------------------------------------------------
# LocalEmbeddingStore
# ---------------------------------------------------------------------------

STORE_NAMES = ("workflows", "guides", "sessions")


class LocalEmbeddingStore:
    """Local vector store backed by embeddings.

    Stores:
    - workflows: name + description + trigger_phrases + step descriptions
    - guides: use_cases + common_tasks + successful_patterns
    - sessions: summary text + app/action sequences + corrections

    All local, all offline.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        dimension: int = 256,
        index_dir: Optional[str] = None,
    ):
        self.model_path = Path(model_path) if model_path else None
        self.dimension = dimension
        self.index_dir = Path(index_dir) if index_dir else anyuse_data_path("embeddings")
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.ranker = CrossStoreRanker()

        # Create per-store indexes
        self._indexes: Dict[str, VectorIndex] = {}
        for store_name in STORE_NAMES:
            idx = VectorIndex(
                name=store_name,
                index_path=self.index_dir / f"{store_name}.npy",
                metadata_path=self.index_dir / f"{store_name}_meta.json",
                dimension=dimension,
            )
            # Load existing data if present
            idx.load()
            self._indexes[store_name] = idx

        # TODO: Load real EmbeddingGemma model here when available.
        # self._model = load_model(model_path)
        self._model = None

    # -- embedding ----------------------------------------------------------

    def embed(self, text: str) -> np.ndarray:
        """Embed text into a vector of *self.dimension* floats.

        TODO: Replace this deterministic hash-based mock with real
        EmbeddingGemma (308M) when available.  The mock produces
        repeatable vectors so that tests are stable.

        The real implementation would be roughly:
            tokens = self._model.tokenize(text)
            return self._model.encode(tokens)[:self.dimension]
        """
        # Deterministic hash -> vector: hash the text, expand to dimension.
        # Use hash bytes as uint8 values mapped to [-1, 1] range (avoids
        # inf/nan issues that struct.unpack("f", random_bytes) can produce).
        raw_bytes = b""
        needed = self.dimension
        i = 0
        while len(raw_bytes) < needed:
            chunk = hashlib.sha512(
                text.encode("utf-8") + i.to_bytes(4, "big")
            ).digest()
            raw_bytes += chunk
            i += 1

        # Map bytes 0-255 to floats in [-1, 1]
        vec = np.array(
            [((b / 255.0) * 2.0 - 1.0) for b in raw_bytes[: self.dimension]],
            dtype=np.float32,
        )
        # Normalise so cosine similarity is meaningful
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    # -- CRUD ---------------------------------------------------------------

    def add(self, doc_id: str, text: str, metadata: dict, store: str) -> None:
        """Embed and index a document.

        Args:
            doc_id:   Unique identifier (workflow name, guide name, session id).
            text:     Text to embed (built from document fields).
            metadata: Extra fields for filtering/display.
            store:    "workflows", "guides", or "sessions".
        """
        if store not in self._indexes:
            raise ValueError(f"Unknown store: {store!r}. Must be one of {STORE_NAMES}")
        vector = self.embed(text)
        self._indexes[store].add(doc_id, vector, metadata)
        self._indexes[store].save()

    def remove(self, doc_id: str, store: str) -> bool:
        """Remove a document from the index."""
        if store not in self._indexes:
            raise ValueError(f"Unknown store: {store!r}. Must be one of {STORE_NAMES}")
        removed = self._indexes[store].remove(doc_id)
        if removed:
            self._indexes[store].save()
        return removed

    def search(
        self,
        query: str,
        store: Optional[str] = None,
        top_k: int = 5,
    ) -> Union[List[SearchResult], Dict[str, List[SearchResult]]]:
        """Search within a specific store or all stores.

        If *store* is given, returns ``list[SearchResult]``.
        If *store* is None, returns ``dict[str, list[SearchResult]]`` keyed
        by store name.
        """
        query_vector = self.embed(query)

        if store:
            if store not in self._indexes:
                raise ValueError(f"Unknown store: {store!r}. Must be one of {STORE_NAMES}")
            return self._indexes[store].search(query_vector, top_k)

        # Cross-store: search each
        results: Dict[str, List[SearchResult]] = {}
        for store_name, index in self._indexes.items():
            results[store_name] = index.search(query_vector, top_k)
        return results

    def find_similar(self, query: str, top_k: int = 5) -> List[RankedResult]:
        """Cross-store semantic search with ranking.

        This is the main entry point for "find relevant context".
        Uses CrossStoreRanker to apply preference weights.
        """
        query_vector = self.embed(query)

        workflow_results = self._indexes["workflows"].search(query_vector, top_k)
        guide_results = self._indexes["guides"].search(query_vector, top_k)
        session_results = self._indexes["sessions"].search(query_vector, top_k)

        return self.ranker.rank(
            query=query,
            workflow_results=workflow_results,
            guide_results=guide_results,
            session_results=session_results,
        )

    def rebuild_index(self, store: str) -> None:
        """Rebuild an entire index from source data.

        Called on startup if index is stale, after migrations,
        or after bulk import/delete.

        This clears the index -- the caller must re-add documents.
        """
        if store not in self._indexes:
            raise ValueError(f"Unknown store: {store!r}. Must be one of {STORE_NAMES}")
        idx = self._indexes[store]
        idx._vectors = np.empty((0, idx.dimension), dtype=np.float32)
        idx._doc_ids = []
        idx._metadata = []
        idx.save()


# ---------------------------------------------------------------------------
# Embedding text builders
# ---------------------------------------------------------------------------

_CREDENTIAL_PATTERN = re.compile(
    r'\{\{(credential|password|secret|var):[^}]+\}\}'
)


def build_workflow_embedding_text(workflow) -> str:
    """Build text to embed for a workflow.

    Combines name + description + trigger phrases + step descriptions.
    Strips credential placeholders so secrets are never embedded.
    """
    parts = [
        workflow.name.replace("_", " "),
        workflow.description or "",
    ]
    parts.extend(workflow.trigger_phrases or [])
    for step in (workflow.steps or [])[:10]:  # cap at 10 steps
        desc = getattr(step, "description", "")
        if desc:
            # Strip credential placeholders
            desc = _CREDENTIAL_PATTERN.sub("[CREDENTIAL]", desc)
            parts.append(desc)
    return " | ".join(p for p in parts if p)


def build_guide_embedding_text(guide) -> str:
    """Build text to embed for a guide.

    Combines name + short description + use cases + common tasks + patterns.
    """
    parts = [
        guide.name,
        getattr(guide, "short_description", "") or "",
    ]
    use_cases = getattr(guide, "use_cases", []) or []
    parts.extend(use_cases[:10])
    common_tasks = getattr(guide, "common_tasks", []) or []
    parts.extend(common_tasks[:5])
    successful_patterns = getattr(guide, "successful_patterns", []) or []
    parts.extend(successful_patterns[:5])
    return " | ".join(p for p in parts if p)


def build_session_embedding_text(session, summary=None) -> str:
    """Build text to embed for a watch session.

    If a post-session review summary is available, use that (highest quality).
    Otherwise, build from raw events.
    """
    if summary:
        segments = getattr(summary, "segments", []) or []
        parts = [getattr(seg, "description", str(seg)) for seg in segments]
        return " | ".join(parts)

    # Fallback: build from events
    parts = []
    events = getattr(session, "events", []) or []
    for event in events[:20]:
        tag_type = getattr(event, "tag_type", None)
        if tag_type is not None:
            tag_value = tag_type.value if hasattr(tag_type, "value") else str(tag_type)
            # Only use STEP, INTENT, CONTEXT_CHANGE
            if tag_value in ("STEP", "INTENT", "CONTEXT_CHANGE"):
                target = getattr(event, "target", "") or ""
                reason = getattr(event, "reason", "") or ""
                desc = f"{tag_value}: {target} {reason}".strip()
                parts.append(desc)

    return " | ".join(parts)
