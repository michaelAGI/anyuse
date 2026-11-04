"""
Context Graph + RAG - Unified knowledge graph for retrieval-augmented generation.

Sits on top of existing storage + embedding store.
Doesn't duplicate data -- references into WorkflowStorage, GuideStorage,
and LocalEmbeddingStore.

Graph traversal is one-hop only (fast + predictable):
    - Workflow found -> get its guide (via GuideStorage.get_guides_for_workflow)
    - Guide found   -> get its linked_workflows
    - Session found  -> (future: get workflows it produced)

Context boosting adjusts scores based on current app/URL.
Boundary filtering removes nodes for blocked apps/domains.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from anyuse.guides.storage import GuideStorage
    from anyuse.memory.embeddings import LocalEmbeddingStore
    from anyuse.memory.storage import WorkflowStorage


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """A single node in the context graph result."""
    doc_id: str
    store: str       # "workflows", "guides", "sessions"
    score: float     # combined score after boosting
    source: str      # "embedding", "graph:workflow->guide", "graph:guide->workflow", "graph:session->workflow"


@dataclass
class ContextResult:
    """Result of a context graph query."""
    items: List[GraphNode] = field(default_factory=list)

    @property
    def workflows(self) -> List[GraphNode]:
        """All workflow nodes, ordered by score (highest first)."""
        return [i for i in self.items if i.store == "workflows"]

    @property
    def guides(self) -> List[GraphNode]:
        """All guide nodes, ordered by score (highest first)."""
        return [i for i in self.items if i.store == "guides"]

    @property
    def sessions(self) -> List[GraphNode]:
        """All session nodes, ordered by score (highest first)."""
        return [i for i in self.items if i.store == "sessions"]

    @property
    def best_workflow(self) -> Optional[GraphNode]:
        """Highest-scoring workflow, or None."""
        wfs = self.workflows
        return wfs[0] if wfs else None

    def to_prompt_context(self, workflow_storage, guide_storage) -> str:
        """Format top results as markdown for agent prompt injection.

        Includes up to 2 workflows (with steps) and up to 2 guides
        (with short summaries).
        """
        sections: list[str] = []

        for node in self.workflows[:2]:
            wf = workflow_storage.get(node.doc_id)
            if wf:
                steps_list = []
                for i, s in enumerate(wf.steps[:10]):
                    steps_list.append(f"  {i + 1}. {s.description}")
                steps = "\n".join(steps_list)
                sections.append(
                    f"Known workflow '{wf.name}' (match: {node.score:.0%}):\n{steps}"
                )

        for node in self.guides[:2]:
            guide = guide_storage.get(node.doc_id)
            if guide:
                sections.append(
                    f"App guide for {guide.name}:\n{guide.get_short_summary()}"
                )

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# ContextGraph
# ---------------------------------------------------------------------------

class ContextGraph:
    """Unified knowledge graph for RAG queries.

    Constructor accepts optional storages so the graph degrades gracefully
    when stores are missing (e.g. no embedding store yet).
    """

    def __init__(
        self,
        workflow_storage: Optional[WorkflowStorage] = None,
        guide_storage: Optional[GuideStorage] = None,
        embedding_store: Optional[LocalEmbeddingStore] = None,
    ):
        self._workflows = workflow_storage
        self._guides = guide_storage
        self._embeddings = embedding_store

    # -- public API ---------------------------------------------------------

    def query(
        self,
        task: str,
        context: Optional[dict] = None,
        boundary: Optional[dict] = None,
        max_results: int = 10,
    ) -> ContextResult:
        """Main RAG query: find all relevant context for a task.

        Steps:
            1. Embedding search via find_similar()
            2. One-hop graph expansion
            3. Context boosting (if context provided)
            4. Boundary filtering (if boundary provided)
            5. Deduplicate and rank
        """
        result = ContextResult()

        # 1. Embedding search
        if self._embeddings is not None:
            ranked = self._embeddings.find_similar(task, top_k=max_results)
        else:
            ranked = []

        # 2. One-hop graph expansion
        nodes = self._expand_graph(ranked, context)

        # 3. Context-based boosting
        if context:
            nodes = self._boost_by_context(nodes, context)

        # 4. Boundary filtering
        if boundary:
            nodes = self._filter_by_boundary(nodes, boundary)

        # 5. Deduplicate and rank
        result.items = self._deduplicate_and_rank(nodes)

        return result

    # -- internals ----------------------------------------------------------

    def _expand_graph(self, initial, context) -> List[GraphNode]:
        """One-hop expansion from initial embedding results.

        - Workflow found -> get guide via GuideStorage.get_guides_for_workflow
        - Guide found   -> get its linked_workflows (up to 3)
        """
        nodes: list[GraphNode] = []

        for item in initial:
            node = GraphNode(
                doc_id=item.doc_id,
                store=item.store,
                score=item.boosted_score,
                source="embedding",
            )
            nodes.append(node)

            if item.store == "workflows" and self._guides is not None:
                # Find guides linked to this workflow
                guides = self._guides.get_guides_for_workflow(item.doc_id)
                for guide in guides:
                    nodes.append(GraphNode(
                        doc_id=guide.name,
                        store="guides",
                        score=item.boosted_score * 0.8,
                        source="graph:workflow->guide",
                    ))

            elif item.store == "guides" and self._workflows is not None:
                guide = self._guides.get(item.doc_id) if self._guides else None
                if guide and guide.linked_workflows:
                    for wf_id in guide.linked_workflows[:3]:
                        nodes.append(GraphNode(
                            doc_id=wf_id,
                            store="workflows",
                            score=item.boosted_score * 0.9,
                            source="graph:guide->workflow",
                        ))

            elif item.store == "sessions" and self._workflows is not None:
                # Future: session -> produced workflows
                # Currently a no-op placeholder.
                pass

        return nodes

    def _boost_by_context(
        self, nodes: List[GraphNode], context: dict
    ) -> List[GraphNode]:
        """Boost nodes matching current app/URL context.

        Boosts:
            Guide matches current app  -> +0.15
            Guide matches current URL  -> +0.20
            Workflow tags match app     -> +0.10
        """
        current_app = context.get("app", "")
        current_url = context.get("url", "")

        for node in nodes:
            if node.store == "guides" and self._guides is not None:
                guide = self._guides.get(node.doc_id)
                if guide:
                    if guide.matches_app(current_app):
                        node.score += 0.15
                    if current_url and guide.matches_url(current_url):
                        node.score += 0.20

            elif node.store == "workflows" and self._workflows is not None:
                wf = self._workflows.get(node.doc_id)
                if wf and wf.tags:
                    if current_app.lower() in [t.lower() for t in wf.tags]:
                        node.score += 0.10

        return nodes

    def _filter_by_boundary(
        self, nodes: List[GraphNode], boundary: dict
    ) -> List[GraphNode]:
        """Remove nodes for blocked apps/domains using RestrictionPolicy."""
        from anyuse.core.restriction import RestrictionPolicy

        policy = RestrictionPolicy.from_boundary(boundary)
        filtered: list[GraphNode] = []

        for node in nodes:
            if node.store == "guides" and self._guides is not None:
                guide = self._guides.get(node.doc_id)
                if guide:
                    # Check domain
                    if guide.domain:
                        blocked, _ = policy.is_url_blocked(guide.domain)
                        if blocked:
                            continue
                    # Check app
                    if guide.is_application:
                        blocked, _ = policy.is_app_blocked(guide.name)
                        if blocked:
                            continue

            elif node.store == "workflows" and self._workflows is not None:
                wf = self._workflows.get(node.doc_id)
                if wf and wf.tags:
                    skip = False
                    for tag in wf.tags:
                        # Treat tag as potential app name or domain
                        app_blocked, _ = policy.is_app_blocked(tag)
                        url_blocked, _ = policy.is_url_blocked(tag)
                        if app_blocked or url_blocked:
                            skip = True
                            break
                    if skip:
                        continue

            filtered.append(node)

        return filtered

    def _deduplicate_and_rank(
        self, nodes: List[GraphNode]
    ) -> List[GraphNode]:
        """Keep highest score per (store, doc_id), sort descending."""
        best: dict[tuple[str, str], GraphNode] = {}
        for node in nodes:
            key = (node.store, node.doc_id)
            if key not in best or node.score > best[key].score:
                best[key] = node

        ranked = sorted(best.values(), key=lambda n: n.score, reverse=True)
        return ranked
