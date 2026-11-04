"""Visual verification + OCR-anchor — the no-LLM verify frame (pillar 6).

The deterministic complement to the AX drift gate: when AX is inconclusive or
absent (canvas / Electron / web), verify the screen against an expectation WITHOUT
an LLM, via two tiers that produce a MEASURED confidence (not the hardcoded
0.9/0.5 the legacy matcher faked):

    Tier OCR    — is the expected TEXT present (in a region)?  (Apple Vision)
    Tier pixel  — does the live REGION match a recorded image within tolerance? (PIL+numpy)

It also provides the OCR-ANCHOR locator (find text on screen → a click point) — the
Tier-3 rung of the locator ladder and the durability story for thin-AX surfaces
where a role:name selector can't be fabricated.

Apple Vision OCR runs on a PROVIDED image, so it needs NO Screen-Recording grant —
only capturing the live screen does. The OCR backend is injectable so the verify
logic is unit-testable with a fake.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

Box = Tuple[int, int, int, int]  # (x, y, w, h) in SCREENSHOT PIXELS, top-left origin


@dataclass
class TextBox:
    text: str
    confidence: float
    box: Box


@dataclass
class OCRAnchor:
    """A durable, position-tolerant locator: find ``text`` on screen and act at its
    center (+ click_offset). The Electron/web durability story when AX is anonymous."""
    text: str
    region: Optional[Box] = None       # search only here (pixels), if known
    tolerance: float = 0.0             # reserved for fuzzy matching
    click_offset: Tuple[int, int] = (0, 0)


@dataclass
class VerifyResult:
    passed: bool
    confidence: float
    tier: str          # "ocr" | "pixel" | "none"
    detail: str = ""


# ── pixel/region match (PIL + numpy; measured, dependency-free) ───────────────
def region_similarity(a_bytes: bytes, b_bytes: bytes, box: Optional[Box] = None,
                      grayscale: bool = True) -> float:
    """Measured similarity (0..1) of two images (optionally a region). 1.0 = identical.

    Crops to ``box`` if given, optionally grayscales (robust to minor color shift),
    resizes b→a if sizes differ, and returns ``1 - mean_abs_diff/255``. This replaces
    the legacy matcher's hardcoded confidence with a real score.
    """
    try:
        from PIL import Image
        import numpy as np
        ia = Image.open(io.BytesIO(a_bytes))
        ib = Image.open(io.BytesIO(b_bytes))
        if box:
            crop = (box[0], box[1], box[0] + box[2], box[1] + box[3])
            ia = ia.crop(crop)
            ib = ib.crop(crop)
        mode = "L" if grayscale else "RGB"
        ia = ia.convert(mode)
        ib = ib.convert(mode)
        if ia.size != ib.size:
            ib = ib.resize(ia.size)
        aa = np.asarray(ia, dtype=np.float32)
        bb = np.asarray(ib, dtype=np.float32)
        if aa.size == 0:
            return 0.0
        mad = float(np.mean(np.abs(aa - bb)))
        return max(0.0, 1.0 - mad / 255.0)
    except Exception as exc:
        logger.debug("region_similarity failed: %s", exc)
        return 0.0


# ── native OCR (Apple Vision; best-effort, graceful degrade) ──────────────────
def apple_vision_ocr(img_bytes: bytes, region: Optional[Box] = None) -> List[TextBox]:
    """OCR a PNG via macOS Vision (VNRecognizeTextRequest). Returns text boxes in
    screenshot-pixel coords (top-left origin). Returns [] (never raises) if Vision is
    unavailable. Needs no Screen-Recording grant — it reads the bytes you pass."""
    ox, oy = 0, 0
    try:
        if region:
            from PIL import Image
            im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            ox, oy = int(region[0]), int(region[1])
            im = im.crop((region[0], region[1], region[0] + region[2], region[1] + region[3]))
            buf = io.BytesIO(); im.save(buf, "PNG"); img_bytes = buf.getvalue()

        import Quartz
        import Vision
        from Foundation import NSData

        nsd = NSData.dataWithBytes_length_(img_bytes, len(img_bytes))
        src = Quartz.CGImageSourceCreateWithData(nsd, None)
        if not src:
            return []
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if not cg:
            return []
        W = Quartz.CGImageGetWidth(cg)
        H = Quartz.CGImageGetHeight(cg)

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)  # accurate
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        handler.performRequests_error_([req], None)

        out: List[TextBox] = []
        for obs in (req.results() or []):
            top = obs.topCandidates_(1)
            if not top:
                continue
            cand = top[0]
            bb = obs.boundingBox()  # normalized, bottom-left origin
            x = bb.origin.x * W + ox
            y = (1.0 - bb.origin.y - bb.size.height) * H + oy  # flip to top-left
            w = bb.size.width * W
            h = bb.size.height * H
            out.append(TextBox(text=str(cand.string()), confidence=float(cand.confidence()),
                               box=(int(x), int(y), int(w), int(h))))
        return out
    except Exception as exc:
        logger.debug("apple_vision_ocr unavailable: %s", exc)
        return []


class VisualVerifier:
    """No-LLM OCR + pixel verification, with an injectable OCR backend for tests."""

    def __init__(self, ocr_fn: Optional[Callable[[bytes, Optional[Box]], List[TextBox]]] = None):
        self._ocr_fn = ocr_fn or apple_vision_ocr

    def ocr(self, img: bytes, region: Optional[Box] = None) -> List[TextBox]:
        return self._ocr_fn(img, region)

    def text_present(self, img: bytes, expected: str, region: Optional[Box] = None) -> Tuple[bool, float]:
        """Is ``expected`` text on screen? Returns (found, confidence)."""
        want = (expected or "").strip().lower()
        if not want:
            return (False, 0.0)
        boxes = self.ocr(img, region)
        matches = [b for b in boxes if want in b.text.lower()]
        if matches:
            return (True, max(b.confidence for b in matches))
        joined = " ".join(b.text for b in boxes).lower()  # expected may span boxes
        return (want in joined, 0.5 if want in joined else 0.0)

    def find_anchor(self, img: bytes, anchor: OCRAnchor) -> Optional[Tuple[int, int]]:
        """Locate ``anchor.text`` on screen → a click point in SCREENSHOT PIXELS
        (the caller converts px→pt before actuating). None if not found."""
        want = anchor.text.strip().lower()
        if not want:
            return None
        for b in self.ocr(img, anchor.region):
            if want in b.text.lower():
                x, y, w, h = b.box
                return (x + w // 2 + anchor.click_offset[0], y + h // 2 + anchor.click_offset[1])
        return None

    def verify(self, *, live_img: Optional[bytes] = None, expected_text: Optional[str] = None,
               expected_region_img: Optional[bytes] = None, region: Optional[Box] = None,
               pass_threshold: float = 0.9) -> VerifyResult:
        """Deterministic verify: OCR text-match first, then pixel/region match.
        (The AX-diff tier lives upstream in the drift gate; this is its complement.)"""
        if expected_text and live_img is not None:
            ok, conf = self.text_present(live_img, expected_text, region)
            if ok:
                return VerifyResult(True, conf, "ocr", f"OCR found {expected_text!r}")
        if expected_region_img is not None and live_img is not None:
            sim = region_similarity(expected_region_img, live_img, region)
            return VerifyResult(sim >= pass_threshold, sim, "pixel", f"region similarity {sim:.3f}")
        if expected_text and live_img is not None:
            return VerifyResult(False, 0.0, "ocr", f"OCR did not find {expected_text!r}")
        return VerifyResult(False, 0.0, "none", "no deterministic evidence provided")
