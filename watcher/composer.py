"""
Workflow Composer - Assembles tag events and anchors into replayable Workflows.

Takes raw workflow drafts from watch sessions and composes them into
clean Workflow objects that can be replayed by the agent.

Integration with Guide System:
- Notifies GuideGenerator when workflows are composed
- Auto-links workflows to relevant guides
- Extracts app knowledge for guide enrichment
"""
import os
from typing import List, Optional, Dict, Any, Callable
from datetime import datetime
from pathlib import Path

from anyuse.watcher.models import (
    WatchSession,
    RawWorkflowDraft,
    TagEvent,
    TagType,
    ClickAnchor,
    AbstractionRecord,
    to_snake_case,
)
from anyuse.memory.models import Workflow, WorkflowStep, VisualAnchor
from anyuse.paths import anyuse_data_path


REFINEMENT_PROMPT = """You are refining a raw workflow draft into a clean, replayable workflow.

## Raw Workflow Draft

Name: {name}
Goal: {goal}

Events:
{events_text}

## Your Task

Analyze this workflow and produce a refined version with:

1. **Better Name**: A clear, descriptive name (snake_case, e.g., "create_github_repo")

2. **Description**: 1-2 sentences explaining what this workflow does

3. **Trigger Phrases**: 3-5 natural language phrases that would trigger this workflow
   - e.g., "create a new repository", "make a github repo", "new repo"

4. **Refined Steps**: Clean up the steps:
   - Remove redundant steps
   - Merge rapid clicks on same target
   - Add better descriptions
   - Keep essential coordinates

5. **Tags**: 2-5 tags for categorization (e.g., "github", "repository", "web")

Output as JSON:
{{
    "name": "refined_workflow_name",
    "description": "What this workflow does",
    "trigger_phrases": ["phrase1", "phrase2", "phrase3"],
    "tags": ["tag1", "tag2"],
    "steps": [
        {{
            "action": "click|type|scroll|key",
            "description": "What this step does",
            "target": "UI element",
            "x": 450,
            "y": 120,
            "text": "for type actions",
            "reason": "Why this step is needed"
        }}
    ]
}}
"""


class WorkflowComposer:
    """
    Composes raw workflow drafts into clean, replayable Workflows.
    
    Uses the configured visual role to refine names, descriptions, and steps.
    Links visual anchors for replay matching.
    
    Guide Integration:
    - Calls on_workflow_composed callback when workflows are created
    - Extracts app tags for guide linking
    - Adds related_guides field to workflows
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        verbose: bool = False,
        on_workflow_composed: Optional[Callable[[Workflow], None]] = None,
        on_draft_processed: Optional[Callable[[RawWorkflowDraft], None]] = None,
        abstraction_store: Optional[Any] = None,
        entity_vault: Optional[Any] = None,
        model_backend: Optional[Any] = None,
    ):
        """
        Initialize composer.

        Args:
            api_key: Legacy Gemini credential presence signal; ModelAdapter
                reads credentials from the configured visual role.
            model: Explicit refinement model override, otherwise the visual
                role's configured model (paired with that role's provider).
            verbose: Print debug output
            on_workflow_composed: Callback when a workflow is composed (for guide learning)
            on_draft_processed: Callback when a draft is processed (for guide learning)
            abstraction_store: Optional AbstractionStore for minting task records
                (lazily created against ~/.anyuse when not injected)
            entity_vault: Optional EntityVault for tokenizing private entities in
                minted records (lazily resolved to the default vault when not
                injected — tests inject a temp-dir vault)
            model_backend: Optional injected text backend. Automatic cloud
                selection requires ANYUSE_COMPOSER_CLOUD and the configured
                provider's credentials (when required);
                otherwise composition uses basic refinement.
        """
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        self.verbose = verbose

        # Callbacks for guide integration
        self.on_workflow_composed = on_workflow_composed
        self.on_draft_processed = on_draft_processed

        # Durable distillation index (lazy; tests inject a temp-dir store).
        self._abstraction_store = abstraction_store
        self._abstraction_store_failed = False

        # Entity vault (lazy like the store).
        self._entity_vault = entity_vault
        self._entity_vault_failed = False

        # Refinement backend (ObserverModel). Injected, or lazily auto-selected
        # with explicit consent on first use (never at construction — keeps init cheap).
        self._model_backend = model_backend
        self._model_backend_selected = model_backend is not None

        # Compatibility presence signal only; refinement calls ObserverModel,
        # whose cloud implementation uses the shared model adapter.
        self.client = bool(self.api_key)

    def _get_model_backend(self):
        """The refinement ObserverModel, selected FAIL-CLOSED on first use.

        An injected backend wins. Otherwise cloud refinement requires explicit
        ANYUSE_COMPOSER_CLOUD consent and configured credentials; absent consent or an
        available backend, composition uses its basic fallback.
        """
        if self._model_backend_selected:
            return self._model_backend
        self._model_backend_selected = True
        self._model_backend = None
        try:
            from anyuse.watcher.backends import TextRefinementBackend
            # CLOUD only on EXPLICIT opt-in — the pre-port silent
            # cloud-by-default is exactly the privacy blocker this removes.
            cloud_ok = str(os.environ.get("ANYUSE_COMPOSER_CLOUD", "")
                           ).strip().lower() in ("1", "true", "yes", "on")
            if cloud_ok:
                backend = TextRefinementBackend(model=self.model)
                if backend.is_available():
                    self._model_backend = backend
        except Exception:
            self._model_backend = None
        return self._model_backend
    
    def compose(
        self,
        draft: RawWorkflowDraft,
        session_storage_path: Optional[Path] = None,
    ) -> Workflow:
        """
        Compose a raw draft into a clean Workflow.
        
        Args:
            draft: Raw workflow draft from watching
            session_storage_path: Path to session storage (for anchor files)
        
        Returns:
            Clean Workflow object ready for replay
        """
        # Get refined metadata from the consent-gated refinement backend
        refined = self._refine_with_model(draft)
        
        # Create workflow
        workflow = Workflow.create_new(
            name=refined.get("name", draft.name),
            description=refined.get("description", draft.goal),
        )
        
        # Set trigger phrases and tags
        workflow.trigger_phrases = refined.get("trigger_phrases", [draft.name.lower()])
        workflow.tags = refined.get("tags", [])
        
        # Build steps
        workflow.steps = self._build_steps(
            draft=draft,
            refined_steps=refined.get("steps", []),
            session_storage_path=session_storage_path,
        )
        
        # Mark draft as composed
        draft.composed = True
        draft.workflow_id = workflow.id
        
        # Add app tags for guide linking
        if draft.apps_involved:
            workflow.tags = list(set(workflow.tags + [
                app.lower().replace(" ", "_") for app in draft.apps_involved
            ]))
        
        # Add URL-based tags
        for url in draft.urls_visited:
            domain = self._extract_domain(url)
            if domain and domain not in workflow.tags:
                workflow.tags.append(domain)
        
        if self.verbose:
            print(f"Composed workflow: {workflow.name}")
            print(f"  Steps: {len(workflow.steps)}")
            print(f"  Triggers: {workflow.trigger_phrases}")
            print(f"  Tags: {workflow.tags}")
        
        # Fire callbacks for guide integration
        if self.on_draft_processed:
            self.on_draft_processed(draft)

        # Mint a durable, generalizable kind=task AbstractionRecord from the
        # refined output (steps stripped of x/y coords + anchor filenames) BEFORE
        # notifying the guide layer of the concrete replayable workflow.
        self._mint_abstraction(draft, refined)

        if self.on_workflow_composed:
            self.on_workflow_composed(workflow)

        return workflow

    def _get_abstraction_store(self):
        """Lazily obtain the durable AbstractionStore (best-effort, never raises)."""
        if self._abstraction_store is not None:
            return self._abstraction_store
        if self._abstraction_store_failed:
            return None
        try:
            from anyuse.watcher.storage import AbstractionStore
            self._abstraction_store = AbstractionStore()
        except Exception:
            self._abstraction_store_failed = True
            return None
        return self._abstraction_store

    def _get_entity_vault(self):
        """Lazily obtain the EntityVault (best-effort, never raises). None ⇒
        the store's regex-redaction backstop still applies."""
        if self._entity_vault is not None:
            return self._entity_vault
        if self._entity_vault_failed:
            return None
        try:
            from anyuse.watcher.entity_vault import get_default_vault
            self._entity_vault = get_default_vault()
        except Exception:
            self._entity_vault = None
        if self._entity_vault is None:
            self._entity_vault_failed = True
        return self._entity_vault

    def _abstraction_steps(
        self,
        draft: RawWorkflowDraft,
        refined: Dict[str, Any],
        primary_app: str,
    ) -> List[Dict[str, Any]]:
        """Project steps into the generalizable {action, target_role, target_name,
        app, reason} shape — STRIPPING x/y coordinates and anchor filenames.

        Uses the refined steps when present, else falls back to the draft's raw
        STEP events (both drop the concrete replay coordinates)."""
        steps: List[Dict[str, Any]] = []
        refined_steps = refined.get("steps") or []
        if refined_steps:
            for s in refined_steps:
                steps.append({
                    "action": s.get("action", "click"),
                    "target_role": s.get("target_role", ""),
                    "target_name": s.get("target") or s.get("target_name", ""),
                    "app": s.get("app", primary_app),
                    "reason": s.get("reason", ""),
                })
        else:
            for event in draft.events:
                if event.tag_type != TagType.STEP:
                    continue
                steps.append({
                    "action": event.data.get("action", "click"),
                    "target_role": "",
                    "target_name": event.target,
                    "app": primary_app,
                    "reason": event.reason,
                })
        return steps

    def _mint_abstraction(
        self,
        draft: RawWorkflowDraft,
        refined: Dict[str, Any],
    ) -> Optional[AbstractionRecord]:
        """Mint + upsert a kind=task AbstractionRecord from the refined draft."""
        store = self._get_abstraction_store()
        if not store:
            return None
        try:
            primary_app = draft.apps_involved[0] if draft.apps_involved else ""
            domains = [self._extract_domain(u) for u in draft.urls_visited]
            domains = [d for d in domains if d]
            record = AbstractionRecord(
                kind="task",
                title=to_snake_case(refined.get("name") or draft.name),
                summary=refined.get("description") or draft.goal,
                structure={
                    "goal": draft.goal,
                    "trigger_phrases": refined.get("trigger_phrases", []),
                    "steps": self._abstraction_steps(draft, refined, primary_app),
                },
                apps=list(draft.apps_involved),
                domains=domains,
                tags=refined.get("tags", []),
                confidence=0.7,
                provenance={
                    "session_id": draft.session_id,
                    "obs_batch_range": "",
                    "anchor_ids": [a.id for a in draft.anchors],
                },
            )
            vault = self._get_entity_vault()
            if vault:
                vault.tokenize_record(record)
            return store.upsert(record)
        except Exception as e:
            if self.verbose:
                print(f"Abstraction minting failed: {e}")
            return None
    
    def _extract_domain(self, url: str) -> Optional[str]:
        """Extract domain from URL for tagging."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url if "://" in url else f"https://{url}")
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            return domain if domain else None
        except Exception:
            return None
    
    def compose_from_session(
        self,
        session: WatchSession,
        workflow_name: Optional[str] = None,
    ) -> List[Workflow]:
        """
        Compose all workflows from a session.
        
        Args:
            session: Watch session to process
            workflow_name: Optional specific workflow to compose
        
        Returns:
            List of composed Workflow objects
        """
        workflows = []
        
        # Get workflow names to process
        if workflow_name:
            names = [workflow_name] if workflow_name in session.workflow_names else []
        else:
            names = session.workflow_names
        
        for name in names:
            # Extract draft
            draft = RawWorkflowDraft.from_session(session, name)
            
            # Compose
            workflow = self.compose(
                draft=draft,
                session_storage_path=anyuse_data_path("sessions", session.id),
            )
            workflows.append(workflow)
        
        # Mark session as composed
        if workflows:
            session.status = "composed"
        
        return workflows
    
    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        """Tolerant JSON extraction: whole-string parse first, then the last
        balanced top-level {...} block (a local model may wrap the object in
        prose or a code fence — constrained decoding is cloud-only)."""
        import json
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
        try:
            obj = json.loads(stripped)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            pass
        depth, start, candidate = 0, -1, None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            candidate = obj
                    except (ValueError, TypeError):
                        pass
        return candidate

    def _refine_with_model(self, draft: RawWorkflowDraft) -> Dict[str, Any]:
        """Refine metadata with explicit cloud consent or an injected backend.

        Any failure uses basic refinement.
        """
        basic = {
            "name": draft.name,
            "description": draft.goal,
            "trigger_phrases": [draft.name.lower()],
            "tags": draft.apps_involved[:3],
            "steps": [],
        }
        backend = self._get_model_backend()
        if backend is None:
            return basic

        events_text = self._format_events_for_prompt(draft.events)
        prompt = REFINEMENT_PROMPT.format(
            name=draft.name,
            goal=draft.goal,
            events_text=events_text,
        )
        try:
            text = backend.generate_text_sync(prompt, json_mode=True)
            refined = self._extract_json_object(text)
            if isinstance(refined, dict) and refined:
                return refined
            if self.verbose:
                print("Refinement produced no usable JSON; using basic refinement")
        except Exception as e:
            if self.verbose:
                print(f"Refinement failed: {e}")
        return basic

    # Backward-compat alias (pre-port name; refinement no longer calls Gemini
    # directly — it rides the consent-gated ObserverModel backend).
    def _refine_with_gemini(self, draft: RawWorkflowDraft) -> Dict[str, Any]:
        return self._refine_with_model(draft)
    
    def _format_events_for_prompt(self, events: List[TagEvent]) -> str:
        """Format events as text for the refinement prompt."""
        lines = []
        
        for i, event in enumerate(events, 1):
            if event.tag_type == TagType.STEP:
                action = event.data.get("action", "unknown")
                line = f"{i}. [{action}] {event.target}"
                if event.reason:
                    line += f" - {event.reason}"
                if event.x and event.y:
                    line += f" @ ({event.x}, {event.y})"
                lines.append(line)
            
            elif event.tag_type == TagType.INTENT:
                lines.append(f"{i}. [INTENT] {event.data.get('description', '')}")
            
            elif event.tag_type == TagType.CONTEXT_CHANGE:
                app = event.data.get("app", "")
                url = event.data.get("url", "")
                lines.append(f"{i}. [CONTEXT] App: {app}, URL: {url}")
        
        return "\n".join(lines) if lines else "No events recorded"
    
    def _build_steps(
        self,
        draft: RawWorkflowDraft,
        refined_steps: List[Dict],
        session_storage_path: Optional[Path],
    ) -> List[WorkflowStep]:
        """Build WorkflowStep objects from draft events and refined steps."""
        steps = []
        
        # If we have refined steps from Gemini, use those
        if refined_steps:
            for i, step_data in enumerate(refined_steps):
                step = WorkflowStep(
                    action=step_data.get("action", "click"),
                    args=self._build_step_args(step_data),
                    description=step_data.get("description", ""),
                    timestamp=datetime.now().timestamp(),
                )
                
                # Try to find matching anchor
                anchor = self._find_matching_anchor(
                    draft.anchors,
                    step_data.get("x"),
                    step_data.get("y"),
                )
                
                if anchor and session_storage_path:
                    step.visual_anchor = VisualAnchor(
                        filename=anchor.filename,
                        size=anchor.size,
                    )
                    step.replay_mode = "visual_match"
                
                steps.append(step)
        
        else:
            # Build from raw events
            for event in draft.events:
                if event.tag_type != TagType.STEP:
                    continue
                
                action = event.data.get("action", "click")
                step = WorkflowStep(
                    action=action,
                    args=self._build_args_from_event(event),
                    description=event.reason or event.target,
                    timestamp=event.timestamp.timestamp(),
                )
                
                # Link anchor if available
                if event.anchor_id:
                    for anchor in draft.anchors:
                        if anchor.id == event.anchor_id:
                            step.visual_anchor = VisualAnchor(
                                filename=anchor.filename,
                                size=anchor.size,
                            )
                            step.replay_mode = "visual_match"
                            break
                
                steps.append(step)
        
        return steps
    
    def _build_step_args(self, step_data: Dict) -> Dict[str, Any]:
        """Build args dict for a step."""
        args = {}
        
        action = step_data.get("action", "click")
        
        if action == "click":
            if step_data.get("x") is not None:
                args["x"] = step_data["x"]
            if step_data.get("y") is not None:
                args["y"] = step_data["y"]
        
        elif action == "type":
            args["text"] = step_data.get("text", "")
        
        elif action == "key":
            args["keys"] = step_data.get("keys", [])
        
        elif action == "scroll":
            args["direction"] = step_data.get("direction", "down")
            args["amount"] = step_data.get("amount", 3)
        
        return args
    
    def _build_args_from_event(self, event: TagEvent) -> Dict[str, Any]:
        """Build args dict from a tag event."""
        args = {}
        action = event.data.get("action", "click")
        
        if action == "click":
            if event.x is not None:
                args["x"] = event.x
            if event.y is not None:
                args["y"] = event.y
        
        elif action == "type":
            args["text"] = event.data.get("text", "")
        
        elif action == "key":
            args["keys"] = event.data.get("keys", [])
        
        elif action == "scroll":
            args["direction"] = event.data.get("direction", "down")
            args["amount"] = event.data.get("amount", 3)
        
        return args
    
    def _find_matching_anchor(
        self,
        anchors: List[ClickAnchor],
        x: Optional[int],
        y: Optional[int],
        tolerance: int = 50,
    ) -> Optional[ClickAnchor]:
        """Find an anchor near the given coordinates."""
        if x is None or y is None:
            return None
        
        for anchor in anchors:
            if abs(anchor.x - x) < tolerance and abs(anchor.y - y) < tolerance:
                return anchor
        
        return None


def compose_session_workflows(
    session_id: str,
    storage_path: Optional[Path] = None,
    api_key: Optional[str] = None,
) -> List[Workflow]:
    """
    Convenience function to compose workflows from a session.
    
    Args:
        session_id: ID of session to process
        storage_path: Path to session storage
        api_key: Gemini API key
    
    Returns:
        List of composed Workflow objects
    """
    from anyuse.watcher.storage import SessionStorage
    
    storage = SessionStorage(storage_path)
    session = storage.load(session_id)
    
    if not session:
        raise ValueError(f"Session not found: {session_id}")
    
    composer = WorkflowComposer(api_key=api_key)
    return composer.compose_from_session(session)
