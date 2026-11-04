"""
Click Anchor Capture - Captures small visual anchors on mouse clicks.

Uses pynput to monitor mouse clicks globally and captures a small crop
(~80x40 pixels) around each click point for visual matching during replay.
"""
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from datetime import datetime
from io import BytesIO

try:
    from pynput import mouse
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from anyuse.watcher.models import ClickAnchor
from anyuse.watcher import raw_store
from anyuse.paths import anyuse_data_path


def load_anchor_image(path, crypto=None):
    """An anchor crop as a PIL image, decrypted in memory only (``raw_store``: an
    encrypted file is decrypted, a legacy plaintext PNG passes through). None when the
    file is missing or cannot be decrypted."""
    data = raw_store.read_bytes_transparent(path, crypto)
    if not data:
        return None
    try:
        img = Image.open(BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


class AnchorCapture:
    """
    Captures small visual anchors around mouse click points.
    
    Uses pynput for global click monitoring and PIL for image cropping.
    Anchors are stored as small PNG files for efficient storage.
    """
    
    DEFAULT_ANCHOR_SIZE = (80, 40)  # Width x Height
    
    def __init__(
        self,
        session_id: str,
        storage_dir: Optional[Path] = None,
        anchor_size: Tuple[int, int] = DEFAULT_ANCHOR_SIZE,
        on_anchor_captured: Optional[Callable[[ClickAnchor], None]] = None,
        raw_crypto: Optional[Any] = None,
    ):
        """
        Initialize anchor capture.
        
        Args:
            session_id: ID of the current watch session
            storage_dir: Directory to store anchor images
            anchor_size: Size of anchor crops (width, height)
            on_anchor_captured: Callback when anchor is captured
        """
        if not PYNPUT_AVAILABLE:
            raise ImportError(
                "pynput is required for click monitoring. "
                "Install with: pip install pynput"
            )
        
        if not PIL_AVAILABLE:
            raise ImportError(
                "PIL/Pillow is required for image capture. "
                "Install with: pip install pillow"
            )
        
        self.session_id = session_id
        self.anchor_size = anchor_size
        self.on_anchor_captured = on_anchor_captured
        # k_raw (raw_store): crops are encrypted at rest like session.json and the
        # observations; None = the process default
        self._raw_crypto = raw_crypto
        
        # Storage setup
        self.storage_dir = storage_dir or (
            anyuse_data_path("sessions", session_id, "anchors")
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        # State
        self._listener: Optional[mouse.Listener] = None
        self._running = False
        self._click_count = 0
        self._lock = threading.Lock()
        
        # Current app context (set externally)
        self._current_app = ""
        # Lazy AccessibilityTree for click-target selectors (needs AX TCC)
        self._ax = None
    
    def start(self):
        """Start monitoring for clicks."""
        if self._running:
            return
        
        self._running = True
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()
    
    def stop(self):
        """Stop monitoring for clicks."""
        self._running = False
        if self._listener:
            self._listener.stop()
            self._listener = None
    
    def set_current_app(self, app_name: str):
        """Update the current app context (called from watcher)."""
        self._current_app = app_name
    
    def _on_click(self, x: int, y: int, button, pressed: bool):
        """Handle mouse click event."""
        # Only capture on mouse down (pressed=True), not release
        if not pressed or not self._running:
            return
        
        # Only capture left clicks
        if button != mouse.Button.left:
            return
        
        # Capture in a thread to avoid blocking the listener
        threading.Thread(
            target=self._capture_anchor,
            args=(int(x), int(y)),
            daemon=True,
        ).start()
    
    def _capture_anchor(self, x: int, y: int):
        """Capture a small anchor image around the click point."""
        with self._lock:
            self._click_count += 1
            click_num = self._click_count
        
        try:
            # Capture screen region
            image_bytes = self._capture_region(x, y)
            
            if not image_bytes:
                return
            
            # Create anchor object
            anchor = ClickAnchor.create(
                x=x,
                y=y,
                session_id=self.session_id,
                step_num=click_num,
                app=self._current_app,
            )

            # Semantic identity of the click target (AX role/name/identifier)
            # — the recorded-run equivalent of a DOM check. Native apps have
            # no HTML to ask "is this still the button we recorded?", so at
            # replay that question is answered by THIS selector plus the
            # visual crop. Best-effort: needs Accessibility TCC; ~ms hit-test.
            try:
                from anyuse.context.accessibility import AccessibilityTree

                if self._ax is None:
                    self._ax = AccessibilityTree()
                sel = self._ax.selector_at(float(x), float(y))
                if sel is not None:
                    bits = [sel.canonical_role or sel.raw_role]
                    if sel.name:
                        bits.append(repr(sel.name))
                    if sel.identifier:
                        bits.append(f"id={sel.identifier}")
                    if sel.path:
                        bits.append(f"path={sel.path}")
                    anchor.target_description = " ".join(b for b in bits if b)
            except Exception:
                pass
            
            # Save the crop encrypted (k_raw); without crypto nothing is written and no
            # anchor is reported - never a plaintext crop on disk
            image_path = self.storage_dir / anchor.filename
            if not raw_store.write_bytes_encrypted(image_path, image_bytes, self._raw_crypto):
                return
            
            # Notify callback
            if self.on_anchor_captured:
                self.on_anchor_captured(anchor)
                
        except Exception as e:
            print(f"Warning: Failed to capture anchor at ({x}, {y}): {e}")
    
    def _capture_region(self, center_x: int, center_y: int) -> Optional[bytes]:
        """
        Capture a small region around the given center point.
        
        Returns PNG bytes of the cropped region.
        """
        width, height = self.anchor_size
        
        # Calculate crop bounds
        left = max(0, center_x - width // 2)
        top = max(0, center_y - height // 2)
        
        try:
            import pyautogui
            
            # Capture the region
            screenshot = pyautogui.screenshot(region=(left, top, width, height))
            
            # Convert to bytes
            buffer = BytesIO()
            screenshot.save(buffer, format='PNG', optimize=True)
            return buffer.getvalue()
            
        except Exception as e:
            print(f"Warning: Region capture failed: {e}")
            return None
    
    @property
    def click_count(self) -> int:
        """Get total clicks captured."""
        return self._click_count
    
    @property
    def is_running(self) -> bool:
        """Check if capture is running."""
        return self._running


class AnchorMatcher:
    """
    Matches visual anchors during workflow replay.
    
    Uses template matching to find the location of a saved anchor
    on the current screen.
    """
    
    def __init__(self, confidence_threshold: float = 0.8, raw_crypto: Optional[Any] = None):
        """
        Initialize anchor matcher.
        
        Args:
            confidence_threshold: Minimum confidence for a match (0-1)
            raw_crypto: the k_raw crypto the crops were written with (None = default)
        """
        self.confidence_threshold = confidence_threshold
        self._raw_crypto = raw_crypto
    
    def find_anchor(
        self,
        anchor_path: Path,
        search_region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[Tuple[int, int, float]]:
        """
        Find an anchor image on the current screen.
        
        Args:
            anchor_path: Path to anchor image file
            search_region: Optional (x, y, width, height) to limit search
        
        Returns:
            Tuple of (x, y, confidence) if found, None otherwise
        """
        try:
            import pyautogui

            needle = load_anchor_image(anchor_path, self._raw_crypto)
            if needle is None:
                return None
            # Use pyautogui's locate functionality (the decrypted crop, in memory)
            location = pyautogui.locateOnScreen(
                needle,
                confidence=self.confidence_threshold,
                region=search_region,
            )
            
            if location:
                # Get center of match
                center_x = location.left + location.width // 2
                center_y = location.top + location.height // 2
                
                # pyautogui doesn't return confidence, so use threshold
                return (center_x, center_y, self.confidence_threshold)
            
            return None
            
        except Exception as e:
            print(f"Warning: Anchor matching failed: {e}")
            return None
    
    def find_anchor_with_tolerance(
        self,
        anchor_path: Path,
        original_x: int,
        original_y: int,
        tolerance: int = 100,
    ) -> Optional[Tuple[int, int, float]]:
        """
        Find an anchor near its original location with some tolerance.
        
        Searches in a region around the original click point first,
        then expands to full screen if not found.
        
        Args:
            anchor_path: Path to anchor image
            original_x: Original X coordinate
            original_y: Original Y coordinate  
            tolerance: Pixels to search around original location
        
        Returns:
            Tuple of (x, y, confidence) if found, None otherwise
        """
        # First try near original location
        search_region = (
            max(0, original_x - tolerance),
            max(0, original_y - tolerance),
            tolerance * 2,
            tolerance * 2,
        )
        
        result = self.find_anchor(anchor_path, search_region)
        
        if result:
            return result
        
        # Fall back to full screen search
        return self.find_anchor(anchor_path)
