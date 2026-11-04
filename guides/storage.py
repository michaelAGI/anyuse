"""
Guide Storage - Persistent storage for application/website guides.

Storage format:
    ~/.anyuse/app_guides/
    ├── index.json           # Index with all guide names and short descriptions
    ├── google_chrome.json   # Full guide for Chrome
    ├── github.com.json      # Full guide for GitHub
    └── ...
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from anyuse.paths import anyuse_data_path
from anyuse.guides.models import AppGuide, GuideType


def _jsonable(obj: Any) -> Any:
    """``json.dump``'s fallback: an object with ``to_dict`` (a ``Selector`` a recorder left in a
    ``UIElement.ax_path``) is written as that dict; anything else is still refused."""
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` through a temporary file in the same directory and
    ``os.replace``: a write that fails (a value that does not serialize, a full disk) leaves the
    previous file whole. A non-atomic write leaves a truncated guide at the first unserializable
    value (JSONDecodeError at char 29918 of textedit.json)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=_jsonable)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GuideStorage:
    """
    Persistent storage for application and website guides.
    
    Stores guides as JSON files with an index for quick lookups.
    """
    
    # See anyuse/paths.py — ANYUSE_DATA_DIR moves this; default unchanged.
    BASE_DIR = anyuse_data_path("app_guides")

    @classmethod
    def default_base_dir(cls) -> Path:
        return anyuse_data_path("app_guides")
    INDEX_FILE = "index.json"
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize storage.
        
        Args:
            base_dir: Base directory for storage (uses ~/.anyuse/app_guides by default)
        """
        self.base_dir = Path(base_dir) if base_dir else self.default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache
        self._cache: Dict[str, AppGuide] = {}
        self._index: Dict[str, Dict] = {}  # name -> {filename, type, short_description}
        
        # Load existing guides
        self._load_index()
        self._load_all_guides()
    
    def _load_index(self):
        """Load the index file."""
        index_path = self.base_dir / self.INDEX_FILE
        if index_path.exists():
            try:
                with open(index_path) as f:
                    self._index = json.load(f)
            except Exception as e:
                print(f"Warning: Could not load guide index: {e}")
                self._index = {}
    
    def _save_index(self):
        """Save the index file."""
        index_path = self.base_dir / self.INDEX_FILE
        try:
            write_json_atomic(index_path, self._index)
        except Exception as e:
            print(f"Warning: Could not save guide index: {e}")
    
    def _load_all_guides(self):
        """Load all guides from disk into cache."""
        for guide_file in self.base_dir.glob("*.json"):
            if guide_file.name == self.INDEX_FILE:
                continue
            
            try:
                with open(guide_file) as f:
                    data = json.load(f)
                guide = AppGuide.from_dict(data)
                self._cache[guide.name.lower()] = guide
            except Exception as e:
                print(f"Warning: Could not load guide from {guide_file}: {e}")
    
    def save(self, guide: AppGuide):
        """
        Save a guide to disk.
        
        Args:
            guide: Guide to save
        """
        # Update timestamp
        guide.updated_at = datetime.now().isoformat()
        
        # Save guide file
        guide_path = self.base_dir / guide.filename
        write_json_atomic(guide_path, guide.to_dict())
        
        # Update cache
        self._cache[guide.name.lower()] = guide
        
        # Update index
        self._index[guide.name] = {
            "filename": guide.filename,
            "type": guide.guide_type.value,
            "short_description": guide.short_description,
            "use_cases": guide.use_cases[:3],  # First 3 use cases
            "domain": guide.domain,
        }
        self._save_index()
    
    def get(self, name: str) -> Optional[AppGuide]:
        """
        Get a guide by name.
        
        Args:
            name: Guide name (case-insensitive)
        
        Returns:
            AppGuide or None if not found
        """
        return self._cache.get(name.lower())
    
    def get_by_app(self, app_name: str) -> Optional[AppGuide]:
        """
        Find a guide matching an application name.
        
        Handles variations like "Google Chrome" vs "Chrome".
        """
        # Try exact match first
        guide = self.get(app_name)
        if guide:
            return guide
        
        # Search through guides for partial match
        for guide in self._cache.values():
            if guide.matches_app(app_name):
                return guide
        
        return None
    
    def get_by_url(self, url: str) -> Optional[AppGuide]:
        """
        Find a guide matching a URL.
        
        Args:
            url: URL to match against website guides
        
        Returns:
            Matching AppGuide or None
        """
        for guide in self._cache.values():
            if guide.matches_url(url):
                return guide
        return None
    
    def get_by_domain(self, domain: str) -> Optional[AppGuide]:
        """
        Find a guide by domain name.
        
        Args:
            domain: Domain like "github.com"
        
        Returns:
            Matching AppGuide or None
        """
        domain_lower = domain.lower()
        
        for guide in self._cache.values():
            if not guide.is_website:
                continue
            
            if guide.domain and guide.domain.lower() == domain_lower:
                return guide
            
            if guide.name.lower() == domain_lower:
                return guide
        
        return None
    
    def delete(self, name: str) -> bool:
        """
        Delete a guide.
        
        Args:
            name: Guide name
        
        Returns:
            True if deleted, False if not found
        """
        guide = self.get(name)
        if not guide:
            return False
        
        # Remove file
        guide_path = self.base_dir / guide.filename
        if guide_path.exists():
            guide_path.unlink()
        
        # Remove from cache and index
        del self._cache[guide.name.lower()]
        if guide.name in self._index:
            del self._index[guide.name]
            self._save_index()
        
        return True
    
    def list_all(self) -> List[AppGuide]:
        """Get all guides."""
        return list(self._cache.values())
    
    def list_names(self) -> List[str]:
        """Get all guide names."""
        return sorted(g.name for g in self._cache.values())
    
    def list_applications(self) -> List[AppGuide]:
        """Get all application guides."""
        return [g for g in self._cache.values() if g.is_application]
    
    def list_websites(self) -> List[AppGuide]:
        """Get all website guides."""
        return [g for g in self._cache.values() if g.is_website]
    
    def get_all_summaries(self) -> str:
        """
        Get short summaries of all guides.
        
        Used to show Gemini what guides are available.
        """
        if not self._cache:
            return "No application guides available."
        
        lines = ["Available Application/Website Guides:"]
        
        # Group by type
        apps = self.list_applications()
        websites = self.list_websites()
        
        if apps:
            lines.append("\nApplications:")
            for guide in sorted(apps, key=lambda g: g.name):
                lines.append(guide.get_short_summary())
        
        if websites:
            lines.append("\nWebsites:")
            for guide in sorted(websites, key=lambda g: g.name):
                lines.append(guide.get_short_summary())
        
        lines.append("\nUse get_app_guide(name) to get full documentation for any of these.")
        
        return "\n".join(lines)
    
    def get_guides_for_apps(self, app_names: List[str]) -> List[AppGuide]:
        """
        Get guides for a list of application names.
        
        Args:
            app_names: List of app names to find guides for
        
        Returns:
            List of matching guides
        """
        guides = []
        seen = set()
        
        for app_name in app_names:
            guide = self.get_by_app(app_name)
            if guide and guide.name not in seen:
                guides.append(guide)
                seen.add(guide.name)
        
        return guides
    
    def get_guides_for_urls(self, urls: List[str]) -> List[AppGuide]:
        """
        Get guides for a list of URLs.
        
        Args:
            urls: List of URLs to find guides for
        
        Returns:
            List of matching guides
        """
        guides = []
        seen = set()
        
        for url in urls:
            guide = self.get_by_url(url)
            if guide and guide.name not in seen:
                guides.append(guide)
                seen.add(guide.name)
        
        return guides
    
    def find_by_use_case(self, query: str) -> List[AppGuide]:
        """
        Find guides that match a use case query.
        
        Args:
            query: Use case to search for (e.g., "manage files")
        
        Returns:
            List of matching guides
        """
        query_lower = query.lower()
        matches = []
        
        for guide in self._cache.values():
            # Check use cases
            for use_case in guide.use_cases:
                if query_lower in use_case.lower():
                    matches.append(guide)
                    break
            else:
                # Check description
                if query_lower in guide.short_description.lower():
                    matches.append(guide)
        
        return matches
    
    def export_guide(self, name: str, filepath: str):
        """
        Export a guide to a JSON file.
        
        Args:
            name: Guide name
            filepath: Path to export to
        """
        guide = self.get(name)
        if guide:
            write_json_atomic(Path(filepath), guide.to_dict())
    
    def import_guide(self, filepath: str) -> Optional[AppGuide]:
        """
        Import a guide from a JSON file.
        
        Args:
            filepath: Path to import from
        
        Returns:
            Imported guide or None on error
        """
        try:
            with open(filepath) as f:
                data = json.load(f)
            
            guide = AppGuide.from_dict(data)
            guide.updated_at = datetime.now().isoformat()
            
            self.save(guide)
            return guide
        except Exception as e:
            print(f"Error importing guide: {e}")
            return None
    
    def link_workflow_to_guide(self, guide_name: str, workflow_id: str) -> bool:
        """
        Link a workflow to a guide.
        
        Args:
            guide_name: Name of the guide
            workflow_id: Workflow ID or name to link
        
        Returns:
            True if successful
        """
        guide = self.get(guide_name)
        if not guide:
            return False
        
        guide.link_workflow(workflow_id)
        self.save(guide)
        return True
    
    def unlink_workflow_from_guide(self, guide_name: str, workflow_id: str) -> bool:
        """
        Unlink a workflow from a guide.
        
        Args:
            guide_name: Name of the guide
            workflow_id: Workflow ID or name to unlink
        
        Returns:
            True if successful
        """
        guide = self.get(guide_name)
        if not guide:
            return False
        
        guide.unlink_workflow(workflow_id)
        self.save(guide)
        return True
    
    def get_guides_for_workflow(self, workflow_id: str) -> List[AppGuide]:
        """
        Get all guides linked to a specific workflow.
        
        Args:
            workflow_id: Workflow ID or name
        
        Returns:
            List of guides that link to this workflow
        """
        return [
            g for g in self._cache.values()
            if workflow_id in g.linked_workflows
        ]

