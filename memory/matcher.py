"""
Template Matcher - Fast pixel-based UI element matching.

Uses PyAutoGUI's locateOnScreen for template matching.
Falls back to AI when matching fails.
"""
import time
from pathlib import Path
from typing import Optional, Tuple, Callable
from dataclasses import dataclass

import pyautogui

from anyuse.memory.models import VisualAnchor


@dataclass
class MatchResult:
    """Result of a template matching attempt."""
    found: bool
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    method: str = "none"  # "pixel", "ai", "original"
    
    @property
    def center(self) -> Tuple[int, int]:
        return (self.x, self.y)


class TemplateMatcher:
    """
    Fast template matching for workflow replay.
    
    Uses pixel-based matching first (fast, no API cost),
    falls back to AI when matching fails.
    
    Usage:
        matcher = TemplateMatcher(workflow_dir)
        result = matcher.find_target(anchor, original_coords, ai_fallback)
        
        if result.found:
            click(result.x, result.y)
    """
    
    def __init__(
        self,
        workflow_dir: Path,
        default_confidence: float = 0.8,
        grayscale: bool = True,
    ):
        """
        Initialize the template matcher.
        
        Args:
            workflow_dir: Directory containing visual anchor images
            default_confidence: Default confidence threshold for matching
            grayscale: Use grayscale matching (faster, more robust)
        """
        self.workflow_dir = Path(workflow_dir)
        self.default_confidence = default_confidence
        self.grayscale = grayscale
    
    def find_target(
        self,
        anchor: VisualAnchor,
        original_coords: Tuple[int, int],
        ai_fallback: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
    ) -> MatchResult:
        """
        Find a UI element using template matching with fallbacks.
        
        Strategy:
        1. Try pixel-based template matching
        2. If fails, try AI-based element finding
        3. If AI fails, use original recorded coordinates
        
        Args:
            anchor: Visual anchor with image filename
            original_coords: Original (x, y) from recording
            ai_fallback: Optional function to call AI for element finding
        
        Returns:
            MatchResult with found coordinates
        """
        # Try pixel matching first
        anchor_path = self.workflow_dir / anchor.filename
        
        if anchor_path.exists():
            result = self._pixel_match(anchor_path, anchor.confidence)
            if result.found:
                # Apply offset if any
                result.x += anchor.offset[0]
                result.y += anchor.offset[1]
                return result
        
        # Try AI fallback
        if ai_fallback:
            ai_coords = ai_fallback()
            if ai_coords:
                return MatchResult(
                    found=True,
                    x=ai_coords[0],
                    y=ai_coords[1],
                    confidence=0.9,
                    method="ai"
                )
        
        # Fall back to original coordinates
        return MatchResult(
            found=True,
            x=original_coords[0],
            y=original_coords[1],
            confidence=0.5,
            method="original"
        )
    
    def _pixel_match(
        self,
        anchor_path: Path,
        confidence: float
    ) -> MatchResult:
        """
        Perform pixel-based template matching.
        
        Args:
            anchor_path: Path to the anchor image
            confidence: Minimum confidence threshold
        
        Returns:
            MatchResult with found status and coordinates
        """
        try:
            # a watcher crop is encrypted at rest (k_raw): decrypted in memory only; a
            # recorder's plaintext PNG passes through
            from anyuse.watcher.anchors import load_anchor_image
            needle = load_anchor_image(anchor_path)
            if needle is None:
                return MatchResult(found=False, method="none")
            location = pyautogui.locateOnScreen(
                needle,
                confidence=confidence,
                grayscale=self.grayscale
            )
            
            if location:
                center = pyautogui.center(location)
                return MatchResult(
                    found=True,
                    x=center.x,
                    y=center.y,
                    confidence=confidence,
                    method="pixel"
                )
        except Exception as e:
            # PyAutoGUI can throw if OpenCV is missing
            print(f"Warning: Pixel matching failed: {e}")
        
        return MatchResult(found=False, method="none")
    
    def verify_match(
        self,
        anchor: VisualAnchor,
        threshold: float = 0.7
    ) -> bool:
        """
        Verify that a visual anchor is still visible on screen.
        
        Useful for checkpoint verification.
        """
        anchor_path = self.workflow_dir / anchor.filename
        
        if not anchor_path.exists():
            return False
        
        result = self._pixel_match(anchor_path, threshold)
        return result.found


class AnchorCapture:
    """
    Capture visual anchors during workflow recording.
    """
    
    def __init__(self, workflow_dir: Path, anchor_size: Tuple[int, int] = (80, 40)):
        """
        Initialize anchor capture.
        
        Args:
            workflow_dir: Directory to save anchor images
            anchor_size: Default (width, height) for anchor regions
        """
        self.workflow_dir = Path(workflow_dir)
        self.workflow_dir.mkdir(parents=True, exist_ok=True)
        self.anchor_size = anchor_size
        self.step_counter = 0
    
    def capture_target(
        self,
        x: int,
        y: int,
        size: Optional[Tuple[int, int]] = None
    ) -> VisualAnchor:
        """
        Capture a visual anchor around a target point.
        
        Args:
            x: Center X coordinate
            y: Center Y coordinate
            size: Optional (width, height), uses default if not specified
        
        Returns:
            VisualAnchor with saved image filename
        """
        self.step_counter += 1
        size = size or self.anchor_size
        width, height = size
        
        # Calculate region bounds
        left = max(0, x - width // 2)
        top = max(0, y - height // 2)
        
        # Capture region
        filename = f"step_{self.step_counter:03d}_target.png"
        filepath = self.workflow_dir / filename
        
        try:
            screenshot = pyautogui.screenshot(region=(left, top, width, height))
            screenshot.save(str(filepath))
            
            return VisualAnchor(
                filename=filename,
                size=size,
                offset=(0, 0),
                confidence=0.8
            )
        except Exception as e:
            print(f"Warning: Failed to capture anchor: {e}")
            return None
    
    def capture_full_screenshot(self, prefix: str = "before") -> str:
        """
        Capture a full screenshot.
        
        Args:
            prefix: Filename prefix (e.g., "before", "after", "checkpoint")
        
        Returns:
            Filename of saved screenshot
        """
        filename = f"step_{self.step_counter:03d}_{prefix}.png"
        filepath = self.workflow_dir / filename
        
        try:
            screenshot = pyautogui.screenshot()
            screenshot.save(str(filepath))
            return filename
        except Exception as e:
            print(f"Warning: Failed to capture screenshot: {e}")
            return None

