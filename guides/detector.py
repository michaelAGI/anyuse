"""
Guide Detector - Detects relevant apps/websites from task text using Gemini Flash.

Uses the fast Flash model to quickly analyze a task and determine
which applications or websites will likely be needed.
"""
import json
from typing import List, Optional, Dict, Any

from anyuse.execution.model_adapter import ModelAdapter
from anyuse.guides.storage import GuideStorage

_DETECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["detected_apps", "detected_websites", "reasoning"],
    "properties": {
        "detected_apps": {"type": "array", "items": {"type": "string"}},
        "detected_websites": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
}


class GuideDetector:
    """
    Detects relevant applications and websites from task descriptions.
    
    Uses Gemini Flash for fast analysis to determine which guides
    should be pre-loaded into context.
    """
    
    DETECTION_PROMPT = """Analyze this task and identify which applications or websites will likely be needed.

TASK: {task}

AVAILABLE GUIDES (these have documentation we can provide):
{available_guides}

CURRENTLY RUNNING APPS:
{running_apps}

CURRENT BROWSER TABS:
{browser_tabs}

Return ONLY valid JSON with:
1. "detected_apps": List of application names that will be needed
2. "detected_websites": List of website domains that will be needed
3. "reasoning": Brief explanation of why these are needed

Example response:
{{
    "detected_apps": ["Google Chrome", "Terminal"],
    "detected_websites": ["github.com"],
    "reasoning": "Task involves creating a GitHub repo, which requires browser for GitHub and potentially terminal for git commands"
}}

Focus on apps/websites that:
- Are explicitly mentioned in the task
- Are commonly needed for this type of task
- Have available guides (prioritize these)
- Are currently running (if relevant to task)

Return empty lists if no specific apps/websites are clearly needed."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        guide_storage: Optional[GuideStorage] = None,
        adapter: Optional[ModelAdapter] = None,
    ):
        """
        Initialize the detector.
        
        Args:
            api_key: Gemini API key (uses config if not provided)
            guide_storage: GuideStorage instance (creates new one if not provided)
        """
        self.api_key = api_key  # compatibility only; adapter owns call-time lookup
        self.adapter = adapter or ModelAdapter.for_role("planner")
        self.model = self.adapter.config.model
        self.storage = guide_storage or GuideStorage()
    
    def detect_apps(
        self,
        task: str,
        running_apps: Optional[List[str]] = None,
        browser_tabs: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Detect which apps/websites are relevant to a task.
        
        Args:
            task: Task description
            running_apps: List of currently running app names
            browser_tabs: List of open browser tabs [{"title": ..., "url": ...}]
        
        Returns:
            Dict with:
                - detected_apps: List of app names
                - detected_websites: List of website domains
                - guides: List of AppGuide objects to include
                - reasoning: Explanation of detection
        """
        # Build context about available guides
        available_guides = self._format_available_guides()
        
        # Format running apps
        running_str = ", ".join(running_apps) if running_apps else "None detected"
        
        # Format browser tabs
        tabs_str = "None"
        if browser_tabs:
            tab_lines = []
            for tab in browser_tabs[:10]:  # Limit to 10 tabs
                title = tab.get("title", "")[:50]
                url = tab.get("url", "")
                tab_lines.append(f"  - {title} ({url})")
            tabs_str = "\n".join(tab_lines) if tab_lines else "None"
        
        prompt = self.DETECTION_PROMPT.format(
            task=task,
            available_guides=available_guides,
            running_apps=running_str,
            browser_tabs=tabs_str,
        )
        
        try:
            response = self.adapter.complete(
                [{"role": "user", "content": prompt}], temperature=0.1,
                json_schema=_DETECTION_SCHEMA, schema_name="guide_detection",
            )
            if not response.ok:
                raise RuntimeError(response.error or "guide detection provider failed")
            result = json.loads(response.text)
            
            detected_apps = result.get("detected_apps", [])
            detected_websites = result.get("detected_websites", [])
            reasoning = result.get("reasoning", "")
            
            # Find matching guides
            guides = []
            seen_names = set()
            
            # Match apps
            for app_name in detected_apps:
                guide = self.storage.get_by_app(app_name)
                if guide and guide.name not in seen_names:
                    guides.append(guide)
                    seen_names.add(guide.name)
            
            # Match websites
            for domain in detected_websites:
                guide = self.storage.get_by_domain(domain)
                if guide and guide.name not in seen_names:
                    guides.append(guide)
                    seen_names.add(guide.name)
            
            return {
                "detected_apps": detected_apps,
                "detected_websites": detected_websites,
                "guides": guides,
                "reasoning": reasoning,
            }
        except Exception as e:
            print(f"Warning: Guide detection failed: {e}")
            return {
                "detected_apps": [],
                "detected_websites": [],
                "guides": [],
                "reasoning": f"Detection failed: {e}",
            }
    
    def _format_available_guides(self) -> str:
        """Format available guides for the prompt."""
        guides = self.storage.list_all()
        
        if not guides:
            return "No guides available yet."
        
        lines = []
        for guide in guides:
            type_label = "Website" if guide.is_website else "App"
            use_cases = ", ".join(guide.use_cases[:3]) if guide.use_cases else "general"
            lines.append(f"- {guide.name} ({type_label}): {use_cases}")
        
        return "\n".join(lines)
    
    def detect_from_context(
        self,
        task: str,
        frontmost_app: Optional[str] = None,
        running_apps: Optional[List[str]] = None,
        current_url: Optional[str] = None,
        browser_tabs: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Detect guides with full macOS context.
        
        This is the main entry point that combines:
        1. AI detection of task-relevant apps
        2. Frontmost app (always included if has guide)
        3. Current URL (if in browser)
        
        Args:
            task: Task description
            frontmost_app: Currently active application
            running_apps: All running applications
            current_url: URL in active browser tab
            browser_tabs: All open browser tabs
        
        Returns:
            Dict with detected apps, websites, guides, and context
        """
        # Get AI-detected apps/websites
        detection = self.detect_apps(task, running_apps, browser_tabs)
        
        guides = detection["guides"]
        seen_names = {g.name for g in guides}
        
        # Always include frontmost app's guide if available
        if frontmost_app:
            frontmost_guide = self.storage.get_by_app(frontmost_app)
            if frontmost_guide and frontmost_guide.name not in seen_names:
                guides.insert(0, frontmost_guide)  # Put frontmost first
                seen_names.add(frontmost_guide.name)
        
        # Include current URL's guide if available
        if current_url:
            url_guide = self.storage.get_by_url(current_url)
            if url_guide and url_guide.name not in seen_names:
                guides.append(url_guide)
                seen_names.add(url_guide.name)
        
        return {
            "detected_apps": detection["detected_apps"],
            "detected_websites": detection["detected_websites"],
            "guides": guides,
            "reasoning": detection["reasoning"],
            "frontmost_app": frontmost_app,
            "current_url": current_url,
        }
    
    def build_context_injection(
        self,
        task: str,
        frontmost_app: Optional[str] = None,
        running_apps: Optional[List[str]] = None,
        current_url: Optional[str] = None,
        browser_tabs: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Build the full context string to inject into the prompt.
        
        Includes:
        1. Full guides for detected/relevant apps
        2. Short summaries of all available guides
        
        Args:
            task: Task description
            frontmost_app: Currently active application
            running_apps: All running applications
            current_url: URL in active browser tab
            browser_tabs: All open browser tabs
        
        Returns:
            Formatted context string for prompt injection
        """
        detection = self.detect_from_context(
            task=task,
            frontmost_app=frontmost_app,
            running_apps=running_apps,
            current_url=current_url,
            browser_tabs=browser_tabs,
        )
        
        lines = []
        
        # Include full guides for detected apps
        if detection["guides"]:
            lines.append("=" * 50)
            lines.append("APPLICATION/WEBSITE GUIDES (Relevant to Task)")
            lines.append("=" * 50)
            lines.append("")
            
            for guide in detection["guides"]:
                lines.append(guide.get_full_context())
                lines.append("")
        
        # Include summaries of all available guides
        all_summaries = self.storage.get_all_summaries()
        if all_summaries and "No application guides available" not in all_summaries:
            lines.append("-" * 50)
            lines.append("OTHER AVAILABLE GUIDES (use get_app_guide to access)")
            lines.append("-" * 50)
            
            # Filter out guides we already included in full
            included_names = {g.name for g in detection["guides"]}
            summary_lines = []
            
            for guide in self.storage.list_all():
                if guide.name not in included_names:
                    summary_lines.append(guide.get_short_summary())
            
            if summary_lines:
                lines.extend(summary_lines)
            else:
                lines.append("(All relevant guides already included above)")
            
            lines.append("")
        
        return "\n".join(lines) if lines else ""


def quick_detect(task: str) -> List[str]:
    """
    Quick detection without full context.
    
    Simpler function for basic app detection without
    running the full detection pipeline.
    
    Args:
        task: Task description
    
    Returns:
        List of detected app/website names
    """
    detector = GuideDetector()
    result = detector.detect_apps(task)
    
    detected = []
    detected.extend(result.get("detected_apps", []))
    detected.extend(result.get("detected_websites", []))
    
    return detected


if __name__ == "__main__":
    # Test the detector
    print("Testing Guide Detector...")
    
    detector = GuideDetector()
    
    # Test task
    task = "Create a new repository on GitHub and clone it to my computer"
    
    print(f"\nTask: {task}")
    print("\nDetecting relevant apps/websites...")
    
    result = detector.detect_apps(
        task,
        running_apps=["Google Chrome", "Finder", "Terminal"],
        browser_tabs=[
            {"title": "GitHub", "url": "https://github.com"},
        ],
    )
    
    print(f"\nDetected apps: {result['detected_apps']}")
    print(f"Detected websites: {result['detected_websites']}")
    print(f"Reasoning: {result['reasoning']}")
    print(f"Matching guides: {[g.name for g in result['guides']]}")
