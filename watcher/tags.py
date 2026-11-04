"""
Tag Stream Parser - Parses historical JSON semantic events.

Handles action, reason, context and workflow boundaries in saved semantic data.
"""
import json
import re
from typing import List, Optional, Generator, Callable
from datetime import datetime
from collections import deque

from anyuse.watcher.models import TagEvent, TagType


class TagParser:
    """
    Parses historical JSON tag events.
    
    Handles streaming output where tags may arrive as partial JSON
    or multiple tags per line.
    """
    
    # Pattern to match JSON objects (handles nested braces)
    JSON_PATTERN = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}')
    
    def __init__(self):
        self._buffer = ""
        self._parsed_count = 0
    
    def parse_chunk(self, chunk: str) -> List[TagEvent]:
        """
        Parse a chunk of text that may contain JSON tags.
        
        Args:
            chunk: Raw text from Gemini (may be partial)
        
        Returns:
            List of parsed TagEvents (may be empty)
        """
        self._buffer += chunk
        events = []
        
        # Find all complete JSON objects
        matches = list(self.JSON_PATTERN.finditer(self._buffer))
        
        if not matches:
            return events
        
        # Parse each match
        last_end = 0
        for match in matches:
            try:
                json_str = match.group()
                data = json.loads(json_str)
                
                # Check if it has a 'type' field (our tag format)
                if "type" in data:
                    event = TagEvent.from_gemini_json(data)
                    events.append(event)
                    self._parsed_count += 1
                
                last_end = match.end()
                
            except json.JSONDecodeError:
                # Incomplete JSON, keep in buffer
                continue
            except Exception as e:
                print(f"Warning: Failed to parse tag: {e}")
                last_end = match.end()
        
        # Remove parsed content from buffer
        if last_end > 0:
            self._buffer = self._buffer[last_end:]
        
        # Prevent buffer from growing too large
        if len(self._buffer) > 10000:
            self._buffer = self._buffer[-5000:]
        
        return events
    
    def parse_line(self, line: str) -> Optional[TagEvent]:
        """
        Parse a single line that should contain one JSON tag.
        
        Args:
            line: A line of text containing a JSON tag
        
        Returns:
            TagEvent if successfully parsed, None otherwise
        """
        line = line.strip()
        if not line:
            return None
        
        try:
            data = json.loads(line)
            if "type" in data:
                self._parsed_count += 1
                return TagEvent.from_gemini_json(data)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"Warning: Failed to parse line: {e}")
        
        return None
    
    def reset(self):
        """Reset the parser state."""
        self._buffer = ""
    
    @property
    def parsed_count(self) -> int:
        """Get total tags parsed."""
        return self._parsed_count


class TagBuffer:
    """
    Buffers tag events and groups them by workflow.
    
    Maintains sliding windows of events and provides methods
    to extract complete workflows.
    """
    
    def __init__(self, max_events: int = 1000):
        """
        Initialize tag buffer.
        
        Args:
            max_events: Maximum events to keep in buffer
        """
        self.max_events = max_events
        self._events: deque = deque(maxlen=max_events)
        self._current_workflow: Optional[str] = None
        self._workflow_events: List[TagEvent] = []
    
    def add(self, event: TagEvent):
        """Add an event to the buffer."""
        self._events.append(event)
        
        # Track workflow boundaries
        if event.tag_type == TagType.WORKFLOW_START:
            self._current_workflow = event.workflow_name
            self._workflow_events = [event]
        elif self._current_workflow:
            self._workflow_events.append(event)
            if event.tag_type == TagType.WORKFLOW_END:
                self._current_workflow = None
    
    def get_current_workflow_events(self) -> List[TagEvent]:
        """Get events for the currently active workflow."""
        return list(self._workflow_events)
    
    def get_completed_workflows(self) -> List[List[TagEvent]]:
        """
        Get all completed workflows (with WORKFLOW_END).
        
        Returns:
            List of event lists, one per completed workflow
        """
        workflows = []
        current = []
        in_workflow = False
        
        for event in self._events:
            if event.tag_type == TagType.WORKFLOW_START:
                in_workflow = True
                current = [event]
            elif in_workflow:
                current.append(event)
                if event.tag_type == TagType.WORKFLOW_END:
                    workflows.append(current)
                    current = []
                    in_workflow = False
        
        return workflows
    
    def get_events_since(self, timestamp: datetime) -> List[TagEvent]:
        """Get all events since a given timestamp."""
        return [e for e in self._events if e.timestamp >= timestamp]
    
    def get_recent(self, n: int = 10) -> List[TagEvent]:
        """Get the N most recent events."""
        return list(self._events)[-n:]
    
    @property
    def current_workflow(self) -> Optional[str]:
        """Get the name of the currently active workflow."""
        return self._current_workflow
    
    @property
    def event_count(self) -> int:
        """Get total events in buffer."""
        return len(self._events)
    
    def clear(self):
        """Clear the buffer."""
        self._events.clear()
        self._current_workflow = None
        self._workflow_events = []


class TagStream:
    """
    High-level interface for streaming tag parsing.
    
    Combines parsing and buffering with callbacks for
    real-time event handling.
    """
    
    def __init__(
        self,
        on_event: Optional[Callable[[TagEvent], None]] = None,
        on_workflow_start: Optional[Callable[[str], None]] = None,
        on_workflow_end: Optional[Callable[[str, List[TagEvent]], None]] = None,
    ):
        """
        Initialize tag stream.
        
        Args:
            on_event: Called for every tag event
            on_workflow_start: Called when a workflow starts
            on_workflow_end: Called when a workflow ends (with all events)
        """
        self.parser = TagParser()
        self.buffer = TagBuffer()
        
        self.on_event = on_event
        self.on_workflow_start = on_workflow_start
        self.on_workflow_end = on_workflow_end
    
    def process(self, chunk: str):
        """
        Process a chunk of text from Gemini.
        
        Parses tags, buffers them, and fires callbacks.
        """
        events = self.parser.parse_chunk(chunk)
        
        for event in events:
            self.buffer.add(event)
            
            # Fire general callback
            if self.on_event:
                self.on_event(event)
            
            # Fire workflow-specific callbacks
            if event.tag_type == TagType.WORKFLOW_START:
                if self.on_workflow_start and event.workflow_name:
                    self.on_workflow_start(event.workflow_name)
            
            elif event.tag_type == TagType.WORKFLOW_END:
                if self.on_workflow_end and event.workflow_name:
                    workflow_events = self.buffer.get_events_for_workflow(
                        event.workflow_name
                    ) if hasattr(self.buffer, 'get_events_for_workflow') else []
                    self.on_workflow_end(event.workflow_name, workflow_events)
    
    def process_line(self, line: str):
        """Process a single line (for line-delimited JSON)."""
        event = self.parser.parse_line(line)
        if event:
            self.buffer.add(event)
            
            if self.on_event:
                self.on_event(event)
    
    @property
    def current_workflow(self) -> Optional[str]:
        """Get the currently active workflow name."""
        return self.buffer.current_workflow
    
    def get_recent_events(self, n: int = 10) -> List[TagEvent]:
        """Get recent events."""
        return self.buffer.get_recent(n)
