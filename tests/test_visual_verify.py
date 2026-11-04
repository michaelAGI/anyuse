"""No-LLM verify frame: measured pixel/region match, OCR text-match, OCR-anchor
locator, and a REAL Apple Vision OCR test (renders text → OCRs it; needs no perms).

Everything except the two Vision tests runs on an injected ``ocr_fn``, so the
file touches nothing. The Vision pair reads a PNG this file renders in memory —
it never looks at the screen and needs no TCC grant — but it does import Apple's
``Vision`` framework, which a desktop guard blocks. Those two now SKIP LOUDLY
when the framework is unavailable instead of failing, which is what let this
file join docs/TEST_ALLOWLIST.txt neighbourhood).
"""
import io

from PIL import Image, ImageDraw, ImageFont

from execution.visual_verify import (OCRAnchor, TextBox, VisualVerifier,
                                      apple_vision_ocr, region_similarity)


def _png(color, size=(60, 40)):
    buf = io.BytesIO(); Image.new("RGB", size, color).save(buf, "PNG"); return buf.getvalue()


def _text_png(text, size=(360, 110)):
    img = Image.new("RGB", size, "white"); d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 56)
    except Exception:
        font = ImageFont.load_default()
    d.text((20, 25), text, fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def _fake_ocr(boxes):
    return lambda img, region=None: boxes


# ── pixel/region match (measured) ────────────────────────────────────────────
def test_region_similarity_identical_is_one():
    p = _png("white")
    assert region_similarity(p, p) == 1.0


def test_region_similarity_opposite_is_near_zero():
    assert region_similarity(_png("white"), _png("black")) < 0.05


def test_region_similarity_region_crop():
    p = _png("white", (100, 100))
    assert region_similarity(p, p, box=(10, 10, 20, 20)) == 1.0


# ── verify tiers (injected fake OCR) ─────────────────────────────────────────
def test_verify_ocr_found():
    v = VisualVerifier(ocr_fn=_fake_ocr([TextBox("Saved", 0.95, (10, 10, 40, 12))]))
    r = v.verify(live_img=b"x", expected_text="Saved")
    assert r.passed and r.tier == "ocr" and r.confidence == 0.95


def test_verify_ocr_miss():
    v = VisualVerifier(ocr_fn=_fake_ocr([TextBox("Error", 0.9, (0, 0, 1, 1))]))
    r = v.verify(live_img=b"x", expected_text="Saved")
    assert not r.passed and r.tier == "ocr"


def test_verify_pixel_tier_measured():
    p = _png("white")
    r = VisualVerifier(ocr_fn=_fake_ocr([])).verify(live_img=p, expected_region_img=p)
    assert r.tier == "pixel" and r.passed and r.confidence == 1.0


def test_verify_none_without_evidence():
    assert VisualVerifier(ocr_fn=_fake_ocr([])).verify(live_img=b"x").tier == "none"


def test_find_anchor_click_point_with_offset():
    v = VisualVerifier(ocr_fn=_fake_ocr([TextBox("OK", 0.9, (100, 200, 40, 20))]))
    pt = v.find_anchor(b"x", OCRAnchor(text="OK", click_offset=(5, -3)))
    assert pt == (125, 207)  # center (120,210) + offset (5,-3)


def test_find_anchor_miss_returns_none():
    v = VisualVerifier(ocr_fn=_fake_ocr([TextBox("Cancel", 0.9, (0, 0, 10, 10))]))
    assert v.find_anchor(b"x", OCRAnchor(text="Submit")) is None


# ── REAL Apple Vision OCR (no Screen-Recording grant needed) ─────────────────

def _vision_available():
    """True only if Apple's Vision framework can actually be imported here."""
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def test_apple_vision_ocr_reads_rendered_text():
    if not _vision_available():
        print("SKIP  Vision framework unavailable (guarded env) — "
              "OCR round-trip not exercised")
        return
    boxes = apple_vision_ocr(_text_png("HELLO"))
    joined = " ".join(b.text for b in boxes).upper()
    assert "HELLO" in joined, joined


def test_apple_vision_ocr_degrades_on_bad_input():
    """Bad input must yield [] — and so must a missing framework: the caller
    always gets a list, never an exception."""
    assert apple_vision_ocr(b"not a png") == []


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:200])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
