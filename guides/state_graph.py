"""
State Graph — UI Knowledge Graph for applications.

Nodes are UI states (identified by AX fingerprints).
Edges are transitions (labeled with intents, implemented with multiple modalities).
The graph grows incrementally with every execution/observation.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from collections import deque
import hashlib
import json
import re


@dataclass
class UIFingerprint:
    """
    Structural identity of a UI page/state.

    Captures the essential AX (accessibility) structure so we can recognize
    a page across sessions, even if positions or minor content change.
    """
    required_elements: List[dict] = field(default_factory=list)
    # Each entry: {"role": str, "title": str}

    element_count_range: Tuple[int, int] = (0, 9999)
    # (min, max) expected interactive element count

    structural_hash: str = ""
    # Hash of the role hierarchy, ignoring titles and positions

    role_frequency: Dict[str, int] = field(default_factory=dict)
    # role -> count distribution: the DECODED structural signal. Stored explicitly
    # because structural_hash is one-way (SHA256) and cannot be reversed. Without
    # this, structural_similarity() could only do exact-hash equality, so two
    # recordings of the SAME screen with any title drift (a filename, a badge, a
    # timestamp) never merged into one node — which silently defeated the whole
    # shared-node map. This is the field that lets near-duplicate screens collapse.

    scoped_counts: Dict[str, int] = field(default_factory=dict)
    # element counts of the recorded screen's parts:
    # "window" (the focused window's subtree) and "menu:<title>" (one menu-bar item's subtree).
    # The drift gate compares the TARGET's part against its ±20 range instead of the whole app,
    # so a second window of the app does not fire it. Empty for a fingerprint recorded before.

    def matches(self, ax_elements: List[dict]) -> dict:
        """
        Compare this fingerprint against live AX elements.

        Returns raw match data for AI evaluation — no thresholds, no booleans.

        Args:
            ax_elements: List of dicts with at least "role" and optionally "title".

        Returns:
            Dict with match signals:
                required_found: elements from required_elements that were found
                required_missing: elements from required_elements that were NOT found
                count_in_range: whether len(ax_elements) falls within element_count_range
                structural_similarity: 0.0-1.0 similarity of role distributions
                new_elements: elements present in ax_elements but not in required_elements
        """
        # Check required elements
        required_found = []
        required_missing = []

        for req in self.required_elements:
            req_role = req.get("role", "")
            req_title = req.get("title", "")
            found = False
            for el in ax_elements:
                el_role = el.get("role", "")
                el_title = el.get("title", "")
                if el_role == req_role and (not req_title or el_title == req_title):
                    found = True
                    break
            if found:
                required_found.append(req)
            else:
                required_missing.append(req)

        # Count range check
        count = len(ax_elements)
        count_in_range = self.element_count_range[0] <= count <= self.element_count_range[1]

        # Structural similarity: compare role frequency distributions
        structural_similarity = self._compute_structural_similarity(ax_elements)

        # Identify new/unknown elements not in required set
        new_elements = []
        for el in ax_elements:
            el_role = el.get("role", "")
            el_title = el.get("title", "")
            is_known = False
            for req in self.required_elements:
                if req.get("role", "") == el_role and (
                    not req.get("title", "") or req.get("title", "") == el_title
                ):
                    is_known = True
                    break
            if not is_known:
                new_elements.append({"role": el_role, "title": el_title})

        return {
            "required_found": required_found,
            "required_missing": required_missing,
            "count_in_range": count_in_range,
            "structural_similarity": structural_similarity,
            "new_elements": new_elements,
        }

    def _compute_structural_similarity(self, ax_elements: List[dict]) -> float:
        """
        Compute structural similarity between this fingerprint's role hierarchy
        and the provided AX elements, using role frequency distribution comparison.
        """
        if not self.structural_hash or not ax_elements:
            return 0.0

        # Build role frequency from live elements
        live_hash = _compute_role_hash(ax_elements)

        if live_hash == self.structural_hash:
            return 1.0

        # Fallback: compare role frequency distributions via Jaccard-like measure.
        # Use the explicitly-stored role_frequency (structural_hash is one-way and
        # cannot be decoded). The _decode_role_frequency() fallback only fires for
        # legacy fingerprints that have neither — and returns {} for them.
        live_roles = _role_frequency(ax_elements)
        fp_roles = self.role_frequency or _decode_role_frequency(self.structural_hash)

        if not fp_roles and not live_roles:
            return 1.0
        if not fp_roles or not live_roles:
            return 0.0

        all_roles = set(fp_roles.keys()) | set(live_roles.keys())
        if not all_roles:
            return 1.0

        # Weighted overlap: for each role, min(count_a, count_b) / max(count_a, count_b)
        similarity_sum = 0.0
        for role in all_roles:
            a = fp_roles.get(role, 0)
            b = live_roles.get(role, 0)
            if max(a, b) > 0:
                similarity_sum += min(a, b) / max(a, b)

        return similarity_sum / len(all_roles)

    def to_dict(self) -> dict:
        return {
            "required_elements": self.required_elements,
            "element_count_range": list(self.element_count_range),
            "structural_hash": self.structural_hash,
            "role_frequency": dict(self.role_frequency),
            **({"scoped_counts": dict(self.scoped_counts)} if self.scoped_counts else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UIFingerprint":
        ecr = data.get("element_count_range", [0, 9999])
        # Backward-compat: legacy fingerprints stored no role_frequency. Derive a
        # usable one from required_elements so old persisted states can still merge.
        role_freq = data.get("role_frequency")
        if not role_freq:
            role_freq = _role_frequency(data.get("required_elements", []))
        return cls(
            required_elements=data.get("required_elements", []),
            element_count_range=(ecr[0], ecr[1]) if len(ecr) >= 2 else (0, 9999),
            structural_hash=data.get("structural_hash", ""),
            role_frequency=role_freq,
            scoped_counts=_scoped_counts(data.get("scoped_counts")),
        )


@dataclass
class UIElement:
    """
    A known interactive element on a UI page.

    Tracks multiple identification strategies (AX path, JS selector,
    relative coordinates) so the execution engine can try each.
    """
    role: str
    title: str
    identifier: Optional[str] = None
    description: str = ""
    element_type: str = "button"
    # button, text_field, checkbox, link, menu_item, popup_button, etc.

    actions: List[str] = field(default_factory=list)
    # e.g., ["press", "set_value", "increment"]

    accepts_input: bool = False

    # Identification strategies
    js_selector: Optional[str] = None
    ax_path: Optional[str] = None

    # Relative position within window (0.0 to 1.0)
    relative_x: Optional[float] = None
    relative_y: Optional[float] = None

    # Stability tracking
    times_seen: int = 0
    last_seen: str = ""
    stable: bool = False

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "title": self.title,
            "identifier": self.identifier,
            "description": self.description,
            "element_type": self.element_type,
            "actions": self.actions,
            "accepts_input": self.accepts_input,
            "js_selector": self.js_selector,
            "ax_path": self.ax_path,
            "relative_x": self.relative_x,
            "relative_y": self.relative_y,
            "times_seen": self.times_seen,
            "last_seen": self.last_seen,
            "stable": self.stable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UIElement":
        return cls(
            role=data.get("role", ""),
            title=data.get("title", ""),
            identifier=data.get("identifier"),
            description=data.get("description", ""),
            element_type=data.get("element_type", "button"),
            actions=data.get("actions", []),
            accepts_input=data.get("accepts_input", False),
            js_selector=data.get("js_selector"),
            ax_path=data.get("ax_path"),
            relative_x=data.get("relative_x"),
            relative_y=data.get("relative_y"),
            times_seen=data.get("times_seen", 0),
            last_seen=data.get("last_seen", ""),
            stable=data.get("stable", False),
        )


@dataclass
class TransitionImpl:
    """
    One concrete way to execute a transition between UI states.

    Multiple modalities may exist for the same logical transition,
    allowing the engine to fall back if one modality fails.
    """
    modality: str
    # "ax", "js", "applescript", "coordinate", "computer_use"

    command: dict = field(default_factory=dict)
    # Modality-specific command data. Examples:
    #   ax: {"element_path": "...", "action": "press"}
    #   js: {"script": "document.querySelector(...).click()"}
    #   applescript: {"script": "tell application ..."}
    #   coordinate: {"x": 500, "y": 300, "action": "click"}
    #   computer_use: {"description": "click the Submit button"}

    times_used: int = 0
    times_succeeded: int = 0

    @property
    def success_rate(self) -> float:
        """Success rate for this implementation."""
        if self.times_used == 0:
            return 0.0
        return self.times_succeeded / self.times_used

    def to_dict(self) -> dict:
        return {
            "modality": self.modality,
            "command": self.command,
            "times_used": self.times_used,
            "times_succeeded": self.times_succeeded,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TransitionImpl":
        return cls(
            modality=data.get("modality", "computer_use"),
            command=data.get("command", {}),
            times_used=data.get("times_used", 0),
            times_succeeded=data.get("times_succeeded", 0),
        )


@dataclass
class UITransition:
    """
    An edge in the state graph — a transition between two UI states.

    Labeled with an intent (what the user/AI is trying to do) and
    backed by one or more concrete implementations.
    """
    from_state: str
    to_state: str
    intent: str
    # e.g., "start refund", "submit form", "navigate to settings"

    implementations: List[TransitionImpl] = field(default_factory=list)

    times_executed: int = 0
    times_succeeded: int = 0
    last_executed: str = ""
    last_failure_reason: str = ""

    @property
    def best_implementation(self) -> Optional[TransitionImpl]:
        """
        Return the implementation with the highest success rate.

        Among implementations with equal success rates, prefers the one
        that has been used more (more data = more confidence).
        Returns None if there are no implementations.
        """
        if not self.implementations:
            return None

        return max(
            self.implementations,
            key=lambda impl: (impl.success_rate, impl.times_used),
        )

    def to_dict(self) -> dict:
        return {
            "from_state": self.from_state,
            "to_state": self.to_state,
            "intent": self.intent,
            "implementations": [impl.to_dict() for impl in self.implementations],
            "times_executed": self.times_executed,
            "times_succeeded": self.times_succeeded,
            "last_executed": self.last_executed,
            "last_failure_reason": self.last_failure_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UITransition":
        return cls(
            from_state=data.get("from_state", ""),
            to_state=data.get("to_state", ""),
            intent=data.get("intent", ""),
            implementations=[
                TransitionImpl.from_dict(i) for i in data.get("implementations", [])
            ],
            times_executed=data.get("times_executed", 0),
            times_succeeded=data.get("times_succeeded", 0),
            last_executed=data.get("last_executed", ""),
            last_failure_reason=data.get("last_failure_reason", ""),
        )


@dataclass
class UIState:
    """
    A recognized page/state in an application.

    Each state has a fingerprint for recognition, a list of known
    interactive elements, and soft identifiers (URL/title patterns).
    """
    state_id: str
    # Unique identifier, e.g. "stripe_payments_list", "github_pr_detail"

    app: str
    # Application name, e.g. "Google Chrome", "Safari", "Finder"

    fingerprint: UIFingerprint = field(default_factory=UIFingerprint)

    elements: List[UIElement] = field(default_factory=list)
    # Known interactive elements on this page

    url_patterns: List[str] = field(default_factory=list)
    # Soft identifiers — URL patterns that hint at this state
    # e.g., ["https://dashboard.stripe.com/payments*"]

    title_patterns: List[str] = field(default_factory=list)
    # Window title patterns that hint at this state
    # e.g., ["Payments — Stripe", "* - Payments"]

    times_seen: int = 0
    last_seen: str = ""

    custom_render_notes: List[str] = field(default_factory=list)
    # Notes about custom-rendered areas (canvas, WebGL, etc.) that
    # AX inspection cannot reach and require coordinate/visual methods

    def to_dict(self) -> dict:
        return {
            "state_id": self.state_id,
            "app": self.app,
            "fingerprint": self.fingerprint.to_dict(),
            "elements": [el.to_dict() for el in self.elements],
            "url_patterns": self.url_patterns,
            "title_patterns": self.title_patterns,
            "times_seen": self.times_seen,
            "last_seen": self.last_seen,
            "custom_render_notes": self.custom_render_notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UIState":
        fp_data = data.get("fingerprint", {})
        fingerprint = UIFingerprint.from_dict(fp_data) if fp_data else UIFingerprint()

        return cls(
            state_id=data.get("state_id", ""),
            app=data.get("app", ""),
            fingerprint=fingerprint,
            elements=[UIElement.from_dict(e) for e in data.get("elements", [])],
            url_patterns=data.get("url_patterns", []),
            title_patterns=data.get("title_patterns", []),
            times_seen=data.get("times_seen", 0),
            last_seen=data.get("last_seen", ""),
            custom_render_notes=data.get("custom_render_notes", []),
        )


@dataclass
class StateGraph:
    """
    The main UI knowledge graph for an application.

    Nodes are UIState objects (pages/screens). Edges are UITransition
    objects (intent-labeled, multi-modal). The graph grows incrementally
    with every agent execution and watcher observation.
    """
    states: Dict[str, UIState] = field(default_factory=dict)
    # state_id -> UIState

    transitions: List[UITransition] = field(default_factory=list)

    def add_state(self, state: UIState) -> None:
        """
        Add or update a state in the graph.

        If a state with the same state_id already exists, merges elements
        and updates metadata. Otherwise adds it as new.
        """
        existing = self.states.get(state.state_id)
        if existing is None:
            self.states[state.state_id] = state
            return

        # Merge: update fingerprint, merge elements, update metadata
        existing.fingerprint = state.fingerprint
        existing.times_seen = max(existing.times_seen, state.times_seen)
        existing.last_seen = max(existing.last_seen, state.last_seen) if state.last_seen else existing.last_seen

        # Merge URL and title patterns (deduplicate)
        for p in state.url_patterns:
            if p not in existing.url_patterns:
                existing.url_patterns.append(p)
        for p in state.title_patterns:
            if p not in existing.title_patterns:
                existing.title_patterns.append(p)

        # Merge elements by (role, title) identity
        existing_keys = {
            (el.role, el.title) for el in existing.elements
        }
        for el in state.elements:
            key = (el.role, el.title)
            if key not in existing_keys:
                existing.elements.append(el)
                existing_keys.add(key)

        # Merge custom render notes
        for note in state.custom_render_notes:
            if note not in existing.custom_render_notes:
                existing.custom_render_notes.append(note)

    def add_transition(self, transition: UITransition) -> None:
        """
        Add or merge a transition.

        If a transition with the same (from_state, to_state, intent) exists,
        merges implementations. Otherwise adds as new.
        """
        existing = self._find_transition(
            transition.from_state, transition.to_state, transition.intent
        )
        if existing is None:
            self.transitions.append(transition)
            return

        # Merge implementations by modality
        existing_modalities = {
            impl.modality for impl in existing.implementations
        }
        for impl in transition.implementations:
            if impl.modality not in existing_modalities:
                existing.implementations.append(impl)
                existing_modalities.add(impl.modality)

        # Update stats — take the higher counts
        existing.times_executed = max(existing.times_executed, transition.times_executed)
        existing.times_succeeded = max(existing.times_succeeded, transition.times_succeeded)
        if transition.last_executed and transition.last_executed > existing.last_executed:
            existing.last_executed = transition.last_executed
        if transition.last_failure_reason:
            existing.last_failure_reason = transition.last_failure_reason

    def add_implementation(
        self,
        from_state: str,
        to_state: str,
        intent: str,
        impl: TransitionImpl,
    ) -> None:
        """
        Add an implementation to an existing transition, or create a new
        transition if one doesn't exist for (from_state, to_state, intent).
        """
        existing = self._find_transition(from_state, to_state, intent)
        if existing is None:
            transition = UITransition(
                from_state=from_state,
                to_state=to_state,
                intent=intent,
                implementations=[impl],
            )
            self.transitions.append(transition)
            return

        # Check if this modality already exists
        for existing_impl in existing.implementations:
            if existing_impl.modality == impl.modality:
                # Update existing implementation's stats and command
                existing_impl.command = impl.command
                existing_impl.times_used += impl.times_used
                existing_impl.times_succeeded += impl.times_succeeded
                return

        existing.implementations.append(impl)

    def identify_state(self, ax_elements: List[dict]) -> Dict[str, dict]:
        """
        Match live AX elements against ALL known states.

        Returns raw match data for every state — no thresholds, no filtering.
        The AI caller evaluates these signals to decide which state is current.

        Args:
            ax_elements: Live accessibility elements from the current screen.

        Returns:
            Dict mapping state_id -> match details dict from UIFingerprint.matches()
        """
        results = {}
        for state_id, state in self.states.items():
            match_data = state.fingerprint.matches(ax_elements)
            # Augment with state metadata for AI context
            match_data["state_id"] = state_id
            match_data["app"] = state.app
            match_data["url_patterns"] = state.url_patterns
            match_data["title_patterns"] = state.title_patterns
            match_data["times_seen"] = state.times_seen
            match_data["element_count"] = len(state.elements)
            results[state_id] = match_data

        return results

    def find_path(self, from_state: str, to_state: str) -> List[UITransition]:
        """
        Find shortest path between two states using BFS.

        Returns the list of transitions forming the shortest path,
        or an empty list if no path exists.
        """
        if from_state == to_state:
            return []

        if from_state not in self.states or to_state not in self.states:
            return []

        # Build adjacency list
        adjacency: Dict[str, List[UITransition]] = {}
        for t in self.transitions:
            adjacency.setdefault(t.from_state, []).append(t)

        # BFS
        queue: deque = deque()
        queue.append((from_state, []))
        visited = {from_state}

        while queue:
            current, path = queue.popleft()

            for transition in adjacency.get(current, []):
                next_state = transition.to_state
                new_path = path + [transition]

                if next_state == to_state:
                    return new_path

                if next_state not in visited:
                    visited.add(next_state)
                    queue.append((next_state, new_path))

        return []

    def get_available_transitions(self, state_id: str) -> List[UITransition]:
        """
        Get all transitions that originate from the given state.

        Returns:
            List of UITransition objects where from_state == state_id.
        """
        return [t for t in self.transitions if t.from_state == state_id]

    def get_state(self, state_id: str) -> Optional[UIState]:
        """Get a state by its ID, or None if not found."""
        return self.states.get(state_id)

    def merge(self, other: "StateGraph") -> None:
        """
        Merge another StateGraph into this one.

        Used for importing shared/community graphs. States and transitions
        are merged using the same add_state/add_transition logic.
        """
        for state in other.states.values():
            self.add_state(state)

        for transition in other.transitions:
            self.add_transition(transition)

    def to_dict(self) -> dict:
        return {
            "states": {
                sid: state.to_dict() for sid, state in self.states.items()
            },
            "transitions": [t.to_dict() for t in self.transitions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StateGraph":
        states = {}
        for sid, state_data in data.get("states", {}).items():
            states[sid] = UIState.from_dict(state_data)

        transitions = [
            UITransition.from_dict(t) for t in data.get("transitions", [])
        ]

        return cls(states=states, transitions=transitions)

    def _find_transition(
        self, from_state: str, to_state: str, intent: str
    ) -> Optional[UITransition]:
        """Find an existing transition by (from_state, to_state, intent)."""
        for t in self.transitions:
            if (
                t.from_state == from_state
                and t.to_state == to_state
                and t.intent == intent
            ):
                return t
        return None


# ---------------------------------------------------------------------------
# Module-level helpers for structural hashing
# ---------------------------------------------------------------------------

#: the note a BUILT (not recorded) state carries first: the map's state of a menu group (its probed options as required
#: elements). The drift gate judges such a baseline only on its required elements (execution/drift_gate.py)
SYNTHETIC_NOTE = "synthetic:"


def _scoped_counts(raw) -> Dict[str, int]:
    """A stored ``scoped_counts`` with only whole, non-negative counts (a record is untrusted data)."""
    out: Dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000:
                out[k] = v
    return out


def _role_frequency(ax_elements: List[dict]) -> Dict[str, int]:
    """Build a role -> count mapping from AX elements."""
    freq: Dict[str, int] = {}
    for el in ax_elements:
        # A live element can carry AXRole = None (TextEdit's find bar has one).
        # `el.get("role", "unknown")` returns that None, and sorting the items in
        # _compute_role_hash then raises TypeError — which takes the drift gate
        # down with it. None is a role we cannot name, not an error.
        role = el.get("role") or "unknown"
        freq[str(role)] = freq.get(str(role), 0) + 1
    return freq


def _compute_role_hash(ax_elements: List[dict]) -> str:
    """
    Compute a deterministic hash of the role hierarchy.

    Ignores titles and positions — only considers the sorted list
    of (role, count) pairs so that structurally identical pages
    produce the same hash.
    """
    freq = _role_frequency(ax_elements)
    # Deterministic serialization: sorted by role name
    canonical = json.dumps(sorted(freq.items()), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _decode_role_frequency(structural_hash: str) -> Dict[str, int]:
    """
    Attempt to decode role frequency from a structural hash.

    Since the hash is one-way, we cannot reverse it. This function
    exists as a fallback that returns an empty dict when the hash
    cannot be decoded — callers should compare hashes directly
    for equality and use element-level comparison for similarity.

    In practice, the UIFingerprint stores the hash for exact-match
    checks and uses required_elements for fuzzy matching.
    """
    # Hashes are one-way; return empty for non-exact comparisons.
    # The structural_similarity computation in UIFingerprint.matches()
    # handles the exact-match case by comparing hashes directly,
    # and falls back to required_elements overlap for fuzzy matching.
    return {}


def compute_structural_hash(ax_elements: List[dict]) -> str:
    """
    Public helper to compute a structural hash from AX elements.

    Use this when building a UIFingerprint from live data.
    """
    return _compute_role_hash(ax_elements)


def generate_state_id(actionable_elements: List[dict], app_name: str) -> str:
    """
    THE state id for a screen. Every producer must call this one function.

    ``actionable_elements`` is the interactive subset of a snapshot — filter it
    with ``context.accessibility.ACTIONABLE_ROLES``, the single public role set
    — as a list of dicts carrying at least ``role``.

    TITLE-INSENSITIVE by design: only the role distribution is hashed, never a
    title. The same screen with different content (a filename, a badge, a
    timestamp, a selected row) therefore produces the SAME id and the nodes
    MERGE. A title-sensitive id re-mints a node on any content drift, which is
    what made ``StateGraph.find_path`` fail to find routes that existed.

    History: this lived in ``memory/recorder._generate_state_id`` while
    ``guides/generator`` minted a second, title-SENSITIVE id of the same shape
    into the same keyspace. Moving it here made one
    producer's ids change — the generator's. Recorder-minted ids are unchanged
    byte-for-byte, and ``tools/migrate_state_graphs.py`` re-keys stored graphs.
    """
    digest = compute_structural_hash(actionable_elements)[:12] if actionable_elements else "empty"
    # A readable prefix; also discriminates same-structure screens across apps.
    safe_app = re.sub(r"[^a-z0-9]", "_", (app_name or "").lower())[:20]
    return f"{safe_app}_{digest}"
