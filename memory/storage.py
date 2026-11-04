"""
Workflow Storage - Persistent storage for workflows with visual anchors.

Storage format:
    ~/.anyuse/workflows/
    └── workflow_{id}/
        ├── workflow.json      # Metadata + steps
        ├── step_001_target.png  # Visual anchor
        ├── step_001_before.png  # Optional screenshot
        └── ...

Workflow matching uses LLM for semantic understanding - no fuzzy/substring matching.
"""
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

from anyuse.paths import anyuse_data_path
from anyuse.memory.models import Workflow


# LLM model for workflow matching. Use the shared Flash model id (env-overridable
# via ANYUSE_GEMINI_MODEL_FLASH). The previous hardcoded preview id 404'd, and the
# callers swallow the error → workflow matching silently returned None for everyone.
try:
    import config as _config
    MATCHER_MODEL = _config.GEMINI_MODEL_FLASH
except Exception:  # pragma: no cover - config import guard
    MATCHER_MODEL = os.environ.get("ANYUSE_GEMINI_MODEL_FLASH", "gemini-3.5-flash")


def _matcher_adapter():
    """The planner role's adapter for workflow matching. ``MATCHER_MODEL`` is a Gemini model id: it is
    sent only when the planner resolves to Gemini with no model name configured; any other provider
    (a local OpenAI-compatible server, ``ANYUSE_MODEL_NAME``) keeps the model its role resolves to."""
    from anyuse.execution import model_adapter as MA
    if MA.resolve_model_config("planner").provider == "gemini" and not MA._env("planner", "NAME"):
        return MA.ModelAdapter.for_role("planner", model=MATCHER_MODEL)
    return MA.ModelAdapter.for_role("planner")


def _matcher_model_name() -> str:
    try:
        return _matcher_adapter().config.model
    except Exception:  # noqa: BLE001 - a broken model config is logged as such
        return "unresolved"


_MATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["match", "workflow_name", "confidence", "reason"],
    "properties": {
        "match": {"type": "boolean"},
        "workflow_name": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string"},
    },
}


class WorkflowStorage:
    """
    Persistent storage for workflows.
    
    Stores workflows as JSON with associated image files.
    """
    
    # Resolved through anyuse.paths so ANYUSE_DATA_DIR can move it; the
    # default is unchanged (~/.anyuse/workflows). Kept as a class attribute
    # for back-compat readers; new code should call default_base_dir().
    BASE_DIR = anyuse_data_path("workflows")

    @classmethod
    def default_base_dir(cls) -> Path:
        """Re-resolved on every call, so a late ANYUSE_DATA_DIR still works."""
        return anyuse_data_path("workflows")
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize storage.
        
        Args:
            base_dir: Base directory for storage (uses ~/.anyuse/workflows by default)
        """
        self.base_dir = Path(base_dir) if base_dir else self.default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # Cache
        self._cache: Dict[str, Workflow] = {}
        self._load_all()
    
    def _load_all(self):
        """Load all workflows from disk."""
        for workflow_dir in self.base_dir.iterdir():
            if workflow_dir.is_dir():
                workflow_file = workflow_dir / "workflow.json"
                if workflow_file.exists():
                    try:
                        workflow = self._load_from_dir(workflow_dir)
                        if workflow:
                            self._cache[workflow.name] = workflow
                    except Exception as e:
                        print(f"Warning: Could not load workflow from {workflow_dir}: {e}")
    
    def _load_from_dir(self, workflow_dir: Path) -> Optional[Workflow]:
        """Load a workflow from a directory."""
        workflow_file = workflow_dir / "workflow.json"
        
        if not workflow_file.exists():
            return None
        
        with open(workflow_file) as f:
            data = json.load(f)
        
        return Workflow.from_dict(data)
    
    def get_workflow_dir(self, workflow_id: str) -> Path:
        """Get the directory for a workflow."""
        return self.base_dir / f"workflow_{workflow_id}"
    
    def save(self, workflow: Workflow):
        """
        Save a workflow to disk.
        
        Args:
            workflow: Workflow to save
        """
        existing = self.get(workflow.name)
        if existing is not None and existing.id != workflow.id:
            raise ValueError(f"workflow {workflow.name!r} already exists; choose a new name or explicitly delete it")
        workflow_dir = self.get_workflow_dir(workflow.id)
        workflow_dir.mkdir(parents=True, exist_ok=True)
        
        workflow_file = workflow_dir / "workflow.json"

        from anyuse.guides.storage import write_json_atomic
        write_json_atomic(workflow_file, workflow.to_dict())
        
        self._cache[workflow.name] = workflow
    
    def get(self, name: str) -> Optional[Workflow]:
        """Get a workflow by name."""
        return self._cache.get(name)
    
    def get_by_id(self, workflow_id: str) -> Optional[Workflow]:
        """Get a workflow by ID."""
        for workflow in self._cache.values():
            if workflow.id == workflow_id:
                return workflow
        return None
    
    def find(self, query: str, use_llm: bool = True) -> Optional[Workflow]:
        """
        Find a workflow matching a query using LLM semantic matching.
        
        Uses Gemini Flash to understand query intent and match to workflows.
        No fuzzy/substring matching - pure semantic understanding.
        
        Args:
            query: Natural language query or task description
            use_llm: Whether to use LLM for matching (default True)
        
        Returns:
            Matching Workflow or None
        """
        # Fast path: exact name match (no API needed)
        if query in self._cache:
            return self._cache[query]
        
        # Case-insensitive exact match
        query_lower = query.lower()
        for name in self._cache:
            if name.lower() == query_lower:
                return self._cache[name]
        
        # No workflows to search
        if not self._cache:
            return None
        
        # LLM-based semantic matching
        if use_llm:
            result = self._llm_find_workflow(query)
            if result:
                return result
        
        return None
    
    def _llm_find_workflow(self, query: str) -> Optional[Workflow]:
        """
        Use LLM to semantically match a query to workflows.
        
        Args:
            query: Natural language query or task description
            
        Returns:
            Best matching Workflow or None
        """
        try:
            
            # Build workflow descriptions for prompt
            workflow_list = []
            for i, wf in enumerate(self._cache.values(), 1):
                desc = wf.description or "No description"
                triggers = ", ".join(wf.trigger_phrases[:3]) if wf.trigger_phrases else "none"
                steps_preview = ", ".join([s.description[:30] for s in wf.steps[:3]]) if wf.steps else "no steps"
                
                workflow_list.append(
                    f'{i}. "{wf.name}"\n'
                    f'   Description: {desc}\n'
                    f'   Triggers: {triggers}\n'
                    f'   Steps: {steps_preview}...'
                )
            
            prompt = f"""Given this user query, find the best matching workflow.

USER QUERY: "{query}"

AVAILABLE WORKFLOWS:
{chr(10).join(workflow_list)}

Return JSON:
{{
    "match": true/false,
    "workflow_name": "exact workflow name if match, null otherwise",
    "confidence": 0.0-1.0,
    "reason": "brief explanation"
}}

Rules:
- Only return match=true if the workflow can fulfill the query's intent
- workflow_name must be EXACTLY as shown (case-sensitive)
- High confidence (>0.8) = workflow clearly matches the task
- Medium confidence (0.5-0.8) = workflow might help but not exact match
- Low confidence (<0.5) = no good match, return match=false
"""
            
            adapter = _matcher_adapter()
            response = adapter.complete(
                [{"role": "user", "content": prompt}], temperature=0.1,
                json_schema=_MATCH_SCHEMA, schema_name="workflow_match",
            )
            if not response.ok:
                raise RuntimeError(response.error or "workflow match provider failed")
            result = json.loads(response.text)
            
            if result.get("match") and result.get("workflow_name"):
                workflow_name = result["workflow_name"]
                if workflow_name in self._cache:
                    return self._cache[workflow_name]
            
            return None
            
        except Exception as e:
            # Matching is best-effort, but log loudly — a silent None here hid a
            # dead-model bug for a long time and makes request→workflow look broken.
            logger.warning("LLM workflow match failed (model=%s): %s", _matcher_model_name(), e)
            return None

    def find_with_details(self, query: str) -> Optional[Tuple[Workflow, float, str]]:
        """
        Find a workflow with match details (confidence and reason).
        
        Args:
            query: Natural language query or task description
            
        Returns:
            Tuple of (Workflow, confidence, reason) or None
        """
        # Fast path: exact name match
        if query in self._cache:
            return (self._cache[query], 1.0, "Exact name match")
        
        query_lower = query.lower()
        for name in self._cache:
            if name.lower() == query_lower:
                return (self._cache[name], 1.0, "Exact name match (case-insensitive)")
        
        if not self._cache:
            return None
        
        # LLM matching with details
        try:
            
            workflow_list = []
            for i, wf in enumerate(self._cache.values(), 1):
                desc = wf.description or "No description"
                workflow_list.append(f'{i}. "{wf.name}": {desc}')
            
            prompt = f"""Match this query to a workflow.

QUERY: "{query}"

WORKFLOWS:
{chr(10).join(workflow_list)}

Return JSON: {{"match": bool, "workflow_name": str|null, "confidence": float, "reason": str}}
"""
            
            adapter = _matcher_adapter()
            response = adapter.complete(
                [{"role": "user", "content": prompt}], temperature=0.1,
                json_schema=_MATCH_SCHEMA, schema_name="workflow_match",
            )
            if not response.ok:
                raise RuntimeError(response.error or "workflow match provider failed")
            result = json.loads(response.text)
            
            if result.get("match") and result.get("workflow_name"):
                workflow_name = result["workflow_name"]
                if workflow_name in self._cache:
                    return (
                        self._cache[workflow_name],
                        result.get("confidence", 0.8),
                        result.get("reason", "LLM match")
                    )
            
            return None

        except Exception as e:
            logger.warning("LLM workflow match (details) failed (model=%s): %s", _matcher_model_name(), e)
            return None

    def list_names(self) -> List[str]:
        """Get all workflow names."""
        return sorted(self._cache.keys())
    
    def list_all(self) -> List[Workflow]:
        """Get all workflows."""
        return list(self._cache.values())
    
    def delete(self, name: str) -> bool:
        """Delete a workflow and its files."""
        workflow = self._cache.get(name)
        if not workflow:
            return False
        
        # Older releases could save several IDs under one name. Delete every
        # matching on-disk entry so a forgotten workflow cannot reappear.
        for directory in self.base_dir.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("workflow_"):
                continue
            try:
                saved = self._load_from_dir(directory)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if saved is not None and saved.name == name:
                shutil.rmtree(directory)
        
        del self._cache[name]
        return True
    
    def rename(self, name: str, new_name: str) -> Optional[Workflow]:
        """Rename a workflow: its workflow.json and, when it has one, its template. None when no
        workflow has that name or the new name is taken."""
        workflow = self._cache.get(name)
        if workflow is None or new_name in self._cache:
            return None
        template = self.get_template(workflow.id)
        workflow.name = new_name
        workflow.updated_at = datetime.now().isoformat()
        self.save(workflow)
        if template is not None:
            template.name = new_name
            self.save_template(workflow.id, template)
        del self._cache[name]
        return workflow

    def update_stats(self, name: str, success: bool, error: str = ""):
        """Update workflow statistics after execution."""
        workflow = self._cache.get(name)
        if workflow:
            workflow.run_count += 1
            if success:
                workflow.success_count += 1
            else:
                workflow.failure_count += 1
                workflow.last_error = error
            
            workflow.last_run = datetime.now().isoformat()
            self.save(workflow)
    
    def export(self, name: str, filepath: str):
        """
        Export a workflow to a JSON file.
        
        Note: Visual anchor images are not included in the export.
        """
        workflow = self.get(name)
        if workflow:
            directory = self.get_workflow_dir(workflow.id)
            if "from-draft" in workflow.tags or any((directory / leaf).exists() for leaf in
                    ("draft_workflow.json", "compiled_api.json")):
                raise ValueError("compiled workflows require their local proof package; export as a plain workflow is unsupported")
            template = self.get_template(workflow.id)
            if self.has_template(workflow.id) and template is None:
                raise ValueError("cannot export an unreadable workflow template")
            if not workflow.steps:
                raise ValueError("cannot export a workflow without executable steps")
            with open(filepath, 'w') as f:
                json.dump({"format": "anyuse.workflow-export/1", "workflow": workflow.to_dict(),
                           "template": template.to_dict() if template is not None else None}, f, indent=2)
    
    def import_from(self, filepath: str) -> Optional[Workflow]:
        """
        Import a workflow from a JSON file.
        
        Note: Visual anchors need to be re-recorded.
        """
        try:
            with open(filepath) as f:
                data = json.load(f)
            
            if not isinstance(data, dict):
                raise ValueError("workflow export must be an object")
            if data.get("format") == "anyuse.workflow-export/1":
                if set(data) != {"format", "workflow", "template"}:
                    raise ValueError("complete versioned workflow export required")
                raw, template_data = data["workflow"], data["template"]
            else:
                if "format" in data or any(key in data for key in
                        ("template", "workflow_template", "compiled", "recorded_graph", "approval")):
                    raise ValueError("workflow execution contracts require a versioned export")
                raw, template_data = data, None
            workflow = Workflow.from_dict(raw)
            if "from-draft" in workflow.tags or not workflow.steps:
                raise ValueError("compiled or empty workflows require their original proof package")
            if any(not isinstance(step.action, str) or not step.action or not isinstance(step.args, dict)
                   or step.visual_anchor is not None for step in workflow.steps):
                raise ValueError("workflow steps must be executable without missing visual anchor resources")
            # An imported receipt is not a verification in this environment.
            workflow.verified = False
            workflow.last_verification = ""
            workflow.robustness_score = 0.0
            template = None
            if template_data is not None:
                from anyuse.memory.models import WorkflowTemplate
                template = WorkflowTemplate.from_dict(template_data)
                if template.name != workflow.name or not template.steps:
                    raise ValueError("exported template must belong to the workflow and contain executable steps")
            
            # Create new ID for imported workflow
            import uuid
            workflow.id = uuid.uuid4().hex[:8]
            
            self.save(workflow)
            if template is not None:
                template.id = workflow.id
                try:
                    self.save_template(workflow.id, template)
                except Exception:
                    # Never leave a newly imported workflow executable without
                    # the template carrying its approvals and replay contract.
                    self.delete(workflow.name)
                    raise
            return workflow
        except Exception as e:
            print(f"Error importing workflow: {e}")
            return None
    
    def get_workflow_summary(self) -> str:
        """
        Get a summary of all workflows for AI selection.

        Used when asking AI to select the best matching workflow.
        """
        if not self._cache:
            return "No saved workflows."

        lines = ["Available workflows:"]
        for i, workflow in enumerate(self._cache.values(), 1):
            lines.append(f"{i}. \"{workflow.name}\" - {workflow.description or 'No description'}")
            lines.append(f"   Success rate: {workflow.success_rate:.0%}, Runs: {workflow.run_count}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Execution trace storage (Process Mining)
    # ------------------------------------------------------------------

    def save_trace(self, trace: "ExecutionTrace"):
        """
        Save an execution trace to the traces directory.

        Traces are stored as individual JSON files under:
            ~/.anyuse/workflows/traces/trace_{id}.json

        If the trace is associated with a workflow, it is also stored
        under the workflow's directory at:
            ~/.anyuse/workflows/workflow_{id}/traces/trace_{id}.json

        Args:
            trace: ExecutionTrace to persist.
        """
        # Global traces directory
        global_traces_dir = self.base_dir / "traces"
        global_traces_dir.mkdir(parents=True, exist_ok=True)

        trace_data = trace.to_dict()
        trace_filename = f"trace_{trace.trace_id}.json"

        # Save to global traces directory
        global_path = global_traces_dir / trace_filename
        with open(global_path, "w") as f:
            json.dump(trace_data, f, indent=2)

        # Also save under the workflow directory if we can find the workflow
        if trace.workflow_name:
            workflow = self.get(trace.workflow_name)
            if workflow:
                wf_traces_dir = self.get_workflow_dir(workflow.id) / "traces"
                wf_traces_dir.mkdir(parents=True, exist_ok=True)
                wf_path = wf_traces_dir / trace_filename
                with open(wf_path, "w") as f:
                    json.dump(trace_data, f, indent=2)

    def get_traces(
        self,
        workflow_name: Optional[str] = None,
        limit: int = 100,
    ) -> List["ExecutionTrace"]:
        """
        Load execution traces, optionally filtered by workflow.

        Returns most recent first (sorted by timestamp descending).

        Args:
            workflow_name: Filter by workflow name (optional).
            limit: Maximum number of traces to return.

        Returns:
            List of ExecutionTrace objects.
        """
        from anyuse.execution.process_mining import ExecutionTrace

        traces: List[ExecutionTrace] = []

        # Determine where to look
        if workflow_name:
            # Try workflow-specific directory first
            workflow = self.get(workflow_name)
            if workflow:
                wf_traces_dir = self.get_workflow_dir(workflow.id) / "traces"
                if wf_traces_dir.exists():
                    traces.extend(self._load_traces_from_dir(wf_traces_dir))

            # Also check global traces directory for matching workflow name
            global_traces_dir = self.base_dir / "traces"
            if global_traces_dir.exists():
                for t in self._load_traces_from_dir(global_traces_dir):
                    if t.workflow_name == workflow_name:
                        # Avoid duplicates by trace_id
                        existing_ids = {tr.trace_id for tr in traces}
                        if t.trace_id not in existing_ids:
                            traces.append(t)
        else:
            # Load all traces from global directory
            global_traces_dir = self.base_dir / "traces"
            if global_traces_dir.exists():
                traces = self._load_traces_from_dir(global_traces_dir)

        # Sort by timestamp descending (most recent first)
        traces.sort(key=lambda t: t.timestamp, reverse=True)

        return traces[:limit]

    def _load_traces_from_dir(self, traces_dir: Path) -> List["ExecutionTrace"]:
        """Load all trace files from a directory."""
        from anyuse.execution.process_mining import ExecutionTrace

        traces: List["ExecutionTrace"] = []
        if not traces_dir.exists():
            return traces

        for trace_file in traces_dir.glob("trace_*.json"):
            try:
                with open(trace_file) as f:
                    data = json.load(f)
                traces.append(ExecutionTrace.from_dict(data))
            except Exception as e:
                logger.warning(
                    "Could not load trace from %s: %s", trace_file, e
                )

        return traces

    # ------------------------------------------------------------------
    # WorkflowTemplate storage (Plan 10 Phase 5)
    # ------------------------------------------------------------------

    def get_template(self, workflow_id: str) -> "Optional[WorkflowTemplate]":
        """
        Load a WorkflowTemplate from template.json in the workflow directory.

        Args:
            workflow_id: The workflow ID whose template to load.

        Returns:
            WorkflowTemplate or None if not found.
        """
        from anyuse.memory.models import WorkflowTemplate

        template_path = self.get_workflow_dir(workflow_id) / "template.json"
        if not template_path.exists():
            return None

        try:
            with open(template_path) as f:
                data = json.load(f)
            return WorkflowTemplate.from_dict(data)
        except Exception as e:
            print(f"Warning: Could not load template from {template_path}: {e}")
            return None

    def save_template(self, workflow_id: str, template: "WorkflowTemplate"):
        """
        Save a WorkflowTemplate to template.json in the workflow directory.

        Creates the workflow directory if it does not exist.

        Args:
            workflow_id: The workflow ID to associate the template with.
            template: The WorkflowTemplate to save.
        """
        workflow_dir = self.get_workflow_dir(workflow_id)
        workflow_dir.mkdir(parents=True, exist_ok=True)

        template_path = workflow_dir / "template.json"
        # a failed write leaves the previous template whole, never a truncated one
        from anyuse.guides.storage import write_json_atomic
        write_json_atomic(template_path, template.to_dict())

    def has_template(self, workflow_id: str) -> bool:
        """
        Check if a WorkflowTemplate exists for this workflow.

        Args:
            workflow_id: The workflow ID to check.

        Returns:
            True if template.json exists in the workflow directory.
        """
        template_path = self.get_workflow_dir(workflow_id) / "template.json"
        return template_path.exists()


# Backwards compatibility with simple workflow format
def migrate_simple_workflow(
    name: str,
    steps: List[Dict],
    storage: WorkflowStorage
) -> Workflow:
    """
    Migrate a simple workflow (without visual anchors) to the new format.
    
    Args:
        name: Workflow name
        steps: List of step dicts with 'action' and 'args'
        storage: Storage backend
    
    Returns:
        Migrated Workflow
    """
    from anyuse.memory.models import WorkflowStep
    
    workflow = Workflow.create_new(name)
    
    for step_data in steps:
        step = WorkflowStep(
            action=step_data.get("action", "unknown"),
            args=step_data.get("args", {}),
            description=step_data.get("description", ""),
        )
        workflow.steps.append(step)
    
    storage.save(workflow)
    return workflow
