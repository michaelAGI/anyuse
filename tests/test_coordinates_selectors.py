"""Coordinate conversion + selector model — pure logic, no AX/screen."""
from anyuse.context.coordinates import px_to_pt, pt_to_px
from anyuse.context.accessibility import canonical_role, Selector, selector_from_attrs, AXElementDesc


# ── coordinate space (Retina) ───────────────────────────────────────────────
def test_px_to_pt_retina():
    assert px_to_pt(200, 400, scale=2.0) == (100.0, 200.0)


def test_pt_to_px_retina():
    assert pt_to_px(100, 200, scale=2.0) == (200.0, 400.0)


def test_scale_one_is_noop():
    assert px_to_pt(50, 60, scale=1.0) == (50.0, 60.0)


def test_roundtrip():
    x, y = pt_to_px(*px_to_pt(123, 456, scale=2.0), scale=2.0)
    assert (x, y) == (123.0, 456.0)


# ── canonical roles ─────────────────────────────────────────────────────────
def test_canonical_role_known():
    assert canonical_role("AXButton") == "button"
    assert canonical_role("AXSecureTextField") == "passwordfield"


def test_canonical_role_generic():
    assert canonical_role("AXFooBar") == "foobar"


def test_canonical_role_none():
    assert canonical_role(None) == "unknown"


# ── selectors ───────────────────────────────────────────────────────────────
def test_selector_durable_with_name():
    s = selector_from_attrs(role="AXButton", title="Save")
    assert s.canonical_role == "button" and s.name == "Save" and s.is_durable()


def test_selector_durable_with_identifier():
    s = selector_from_attrs(role="AXButton", identifier="save-btn")
    assert s.identifier == "save-btn" and s.is_durable()


def test_selector_not_durable_anonymous():
    # Electron-style anonymous element with no name/identifier → abstain (OCR-anchor instead)
    assert not selector_from_attrs(role="AXGroup").is_durable()


def test_selector_roundtrip():
    s = selector_from_attrs(role="AXButton", title="OK", identifier="ok", path="window/button[0]")
    assert Selector.from_dict(s.to_dict()) == s


def test_desc_to_selector():
    sel = AXElementDesc(role="AXTextField", title="Email").to_selector(path="form/textfield[1]")
    assert sel.canonical_role == "textfield" and sel.name == "Email" and sel.path == "form/textfield[1]"


def test_selector_describe_prefers_identifier():
    s = selector_from_attrs(role="AXButton", title="Save", identifier="save-id")
    d = s.describe()
    assert "button" in d and "#save-id" in d


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
            f += 1; print("FAIL", n, "::", repr(e)[:160])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
