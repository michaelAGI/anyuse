"""Value crops in the screen (``execution/web_runtime/web_valuecheck2.py`` §2, ``execution/web_runtime/web_arch4_live.VALUE_CROPS``), offline: a fake page (no browser, no network), synthetic renders.

What must hold:

1. OFF by default: with the module default and with ``value_crops=False`` the screen is the golden one;
2. ``value_crops=True``: for every candidate of the seed render (the rules' pool, in its order) the element's
   box plus a margin is captured with ``Page.captureScreenshot`` (PNG, a clip inside the viewport, never beyond
   it) on the page the screen already has - no navigation, no extra render, the values unchanged; the capture
   redaction (``web_arch_run.REDACT_JS``) is on during the captures and removed after;
3. a box off-screen, hidden, covered by another element, with no element, or with an e-mail address / phone
   number in its text gets no crop (the reason is recorded);
4. a page with a login form (a password field) or a personal-data form (e-mail, phone, name, address fields;
   search fields aside), a challenge, a login wall, a consent wall, an error page or a block is never captured:
   no screenshot call at all;
5. the PNGs are written beside the screen record (``<CROPS_DIR>/<site>/t<try>_<k>.png``), the try record keeps
   each crop's file, size and digest; a failing capture never fails the screen.
"""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "execution", "web_runtime"))
sys.path.insert(0, os.path.join(HERE, "testdata", "web_valuecheck"))

import fixtures as F  # noqa: E402
import web_arch4b_live as B  # noqa: E402
import web_valuecheck2 as VK2  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

A4 = B.A4
GOLDEN = os.path.join(HERE, "testdata", "web_valuecheck", "rules_golden.json")


def png(w=2, h=1):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakePage:
    """The open page: ``js`` answers the crop read from ``box_for(k, target)`` and the form fields; ``call``
    answers Page.captureScreenshot with a PNG; any navigation would be recorded (there must be none)."""

    def __init__(self, box_for=None, password=0, personal=0, fail_capture=False, newsletter=0):
        self.box_for = box_for or (lambda k, t: {"found": True, "visible": True, "covered": False, "x": 20,
                                                  "y": 20 + 50 * k, "w": 300, "h": 40, "text": t["title"]})
        self.password, self.personal, self.fail_capture = password, personal, fail_capture
        self.newsletter = newsletter
        self.calls, self.js_calls, self.navigations = [], [], []

    async def js(self, expr, read_only=False):
        if expr == RR.REDACT_JS:
            self.js_calls.append("redact")
            return 0
        if expr == RR.REDACT_OFF_JS:
            self.js_calls.append("redact_off")
            return True
        if "SEARCHY" in expr:
            e = expr.rstrip()
            targets = json.loads(e[e.rindex("})(") + 3:-1])
            self.js_calls.append(("crops", len(targets)))
            return {"vw": 1280, "vh": 900, "sx": 0, "sy": 0, "password": self.password, "personal": self.personal,
                    "newsletter": self.newsletter, "fields": (["password"] * self.password + ["email:newsletter"] * self.personal),
                    "boxes": [self.box_for(k, t) for k, t in enumerate(targets)]}
        self.js_calls.append(expr[:30])
        return None

    async def call(self, method, params=None, **kw):
        self.calls.append((method, dict(params or {})))
        if self.fail_capture:
            raise RuntimeError("capture broke")
        return {"data": base64.b64encode(png()).decode()}

    async def network(self, want_bodies=True):
        return []

    async def goto(self, url):
        self.navigations.append(url)


def run(page_factory, **kw):
    """``screen_values`` on the synthetic shop, every browser call faked; the page is ``page_factory()``."""
    empties = {F.SHOP: A4.empty_terms(F.SHOP), F.MAG: A4.empty_terms(F.MAG)}
    fake = F.Renders(A4, empties)
    pages = []

    def in_page(ctx, endpoint, fn):
        p = page_factory()
        pages.append(p)
        return asyncio.run(fn(p))
    saved = (A4.render_search, A4.in_page, A4.dump)
    site, rec = kw.pop("site", F.SHOP), kw.pop("rec", None) or F.shop_rec()
    try:
        A4.render_search = fake
        A4.in_page = in_page
        A4.dump = lambda *a, **k: None
        out = A4.screen_values(F.Ctx(), "http://127.0.0.1:1", site, rec["stratum"], dict(rec), False, **kw)
    finally:
        A4.render_search, A4.in_page, A4.dump = saved
    return out, list(fake.calls), pages


def _norm(x):
    return json.loads(json.dumps(x, ensure_ascii=False, sort_keys=True))


def _configured(fn):
    old = A4.SEED, A4.CROPS_DIR
    try:
        B.configure()
        return fn()
    finally:
        A4.SEED, A4.CROPS_DIR = old


def test_off_is_the_golden():
    g = json.load(open(GOLDEN, encoding="utf-8"))
    assert A4.VALUE_CROPS is False and A4.CROPS_DIR == ""
    for kw in ({}, {"value_crops": False}):
        out, calls, pages = _configured(lambda: run(FakePage, **kw))
        assert _norm(out) == g["sites"][F.SHOP]["record"] and [list(c) for c in calls] == g["sites"][F.SHOP]["renders"]
        assert all(not p.calls and not p.js_calls for p in pages)


def test_crops_on_the_seed_render():
    g = json.load(open(GOLDEN, encoding="utf-8"))
    with tempfile.TemporaryDirectory() as d:
        def go():
            A4.CROPS_DIR = os.path.join(d, "screen", "crops")
            return run(FakePage, value_crops=True)
        out, calls, pages = _configured(go)
        tr = out["value_tries"][0]
        gold = g["sites"][F.SHOP]["record"]["value_tries"][0]
        # the values and the renders are the golden's: the crops change nothing else, send nothing
        assert tr["values"] == gold["values"] and [list(c) for c in calls] == g["sites"][F.SHOP]["renders"]
        assert {k: v for k, v in tr.items() if k not in ("crops", "crops_page")} == gold
        seedpage = pages[0]
        assert not seedpage.navigations and seedpage.js_calls[0][0] == "crops"
        assert seedpage.js_calls[1] == "redact" and seedpage.js_calls[-1] == "redact_off"
        shots = [c for c in seedpage.calls if c[0] == "Page.captureScreenshot"]
        assert len(shots) == len(tr["crops"]) == seedpage.js_calls[0][1] >= 5
        for _m, p in shots:
            assert p["format"] == "png" and p["captureBeyondViewport"] is False
            clip = p["clip"]
            assert clip["x"] >= 0 and clip["y"] >= 0 and clip["x"] + clip["width"] <= 1280 and clip["y"] + clip["height"] <= 900
        # the value / empty-term renders get no crops
        assert all(not p.calls for p in pages[1:])
        assert tr["crops_page"] == ""
        for c in tr["crops"]:
            assert c["file"].startswith("crops/www_shop_test/t0_") and c["bytes"] > 0 and len(c["sha256"]) == 16
            with open(os.path.join(d, "screen", c["file"]), "rb") as fh:
                assert fh.read().startswith(b"\x89PNG")
        # the crops follow the rules' pool order; the drawn values are among them
        assert set(gold["values"]) <= {c["value"] for c in tr["crops"]}


def test_boxes_without_a_crop():
    def box_for(k, t):
        b = {"found": True, "visible": True, "covered": False, "x": 20, "y": 20 + 50 * k, "w": 300, "h": 40,
             "text": t["title"]}
        if k == 0:
            b.update(y=950)                                            # below the fold
        elif k == 1:
            b.update(visible=False)
        elif k == 2:
            b.update(covered=True)                                     # a cookie banner over it
        elif k == 3:
            b = {"found": False}
        elif k == 4:
            b.update(text="Bestel via info@shop.test of +31 20 123 4567")
        elif k == 5:
            b.update(x=1200)                                           # past the right edge
        return b
    out, _calls, pages = _configured(lambda: run(lambda: FakePage(box_for), value_crops=True))
    crops = out["value_tries"][0]["crops"]
    why = [c.get("skipped") for c in crops[:6]]
    assert why == ["off-screen", "hidden", "covered by another element", "no element", "personal data in its text",
                   "off-screen"], why
    shots = [c for c in pages[0].calls if c[0] == "Page.captureScreenshot"]
    assert len(shots) == len(crops) - 6 and all(c.get("skipped") == "no crops folder" for c in crops[6:])


def test_refused_pages_are_never_captured():
    for kw, want in (({"password": 1}, "a login form"), ({"personal": 2}, "a personal-data form")):
        out, _c, pages = _configured(lambda kw=kw: run(lambda: FakePage(**kw), value_crops=True))
        tr = out["value_tries"][0]
        assert tr["crops_page"].startswith(want) and all(c["skipped"] == "page refused" for c in tr["crops"])
        assert not any(c[0] == "Page.captureScreenshot" for p in pages for c in p.calls)
        assert "redact" not in pages[0].js_calls
    read = F.shop_read("https://www.shop.test/zoeken?q=x", F.SHOP_TILES)
    for text, want in (("Please sign in to continue to your account", "a login wall"),
                       ("Checking your browser before accessing. Verify you are human. Ray ID: 8a1b", "challenge"),
                       ("We use cookies. Accept all cookies to continue.", "a consent wall")):
        r = dict(read, text=text, main_text=text)
        assert VK2.page_refusal(r).startswith(want) or (want == "challenge" and VK2.page_refusal(r) ==
                                                        "a challenge or block page"), (text, VK2.page_refusal(r))
        p = FakePage()
        res = asyncio.run(VK2.capture_crops(p, r, F.SHOP, "shop", "Hoekbank"))
        assert res["page"] and not p.calls and all(c["skipped"] == "page refused" for c in res["crops"])
    assert VK2.page_refusal(dict(read, blocked="captcha_recaptcha (x)")).startswith("blocked")
    assert VK2.page_refusal(read) == ""


def test_an_empty_newsletter_field_alone_does_not_refuse_the_page():
    # a page whose only personal-data fields are empty, lone e-mail fields (a newsletter
    # sign-up) is captured - the tile boxes only, with the redaction on; anything more still refuses the page
    out, _c, pages = _configured(lambda: run(lambda: FakePage(personal=1, newsletter=1), value_crops=True))
    tr = out["value_tries"][0]
    assert not tr["crops_page"], tr["crops_page"]
    assert any(c[0] == "Page.captureScreenshot" for p in pages for c in p.calls)
    assert "redact" in pages[0].js_calls
    for kw in ({"personal": 2, "newsletter": 1},            # e-mail + another personal field (a contact form)
               {"personal": 1, "newsletter": 0},            # a filled, a phone, or a multi-field e-mail form
               {"password": 1, "personal": 1, "newsletter": 1}):
        out, _c, pages = _configured(lambda kw=kw: run(lambda: FakePage(**kw), value_crops=True))
        tr = out["value_tries"][0]
        assert tr["crops_page"], kw
        assert not any(c[0] == "Page.captureScreenshot" for p in pages for c in p.calls), kw


def test_the_crop_read_counts_only_empty_lone_mail_fields_as_newsletter():
    js = VK2.CROPS_JS
    assert "newsletter++" in js and "others === 0" in js and "!String(i.value || '').trim()" in js
    assert "type !== 'tel'" in js and VK2.CROPS_ALLOW_EMPTY_NEWSLETTER is True


def test_clip_and_skip_rules():
    info = {"vw": 1280, "vh": 900, "sx": 0, "sy": 300}
    c = VK2.crop_clip({"x": 4, "y": 10, "w": 200, "h": 30}, info)
    assert c == {"x": 0.0, "y": 302.0, "width": 212.0, "height": 46.0, "scale": 1}      # margin clipped at 0; scroll
    big = VK2.crop_clip({"x": 100, "y": 100, "w": 2000, "h": 2000}, info)
    assert big["width"] <= VK2.CROP_MAX_W and big["height"] <= VK2.CROP_MAX_H
    edge = VK2.crop_clip({"x": 1270, "y": 890, "w": 8, "h": 8}, info)
    assert edge["x"] + edge["width"] <= 1280 and edge["y"] - 300 + edge["height"] <= 900
    ok = {"found": True, "visible": True, "covered": False, "x": 1, "y": 1, "w": 50, "h": 20, "text": "Hoekbank Regalo"}
    assert VK2.box_skip(ok, 1280, 900) == ""
    assert VK2.box_skip(dict(ok, w=2), 1280, 900) == "hidden"
    assert VK2.box_skip(dict(ok, y=-5), 1280, 900) == "off-screen"
    assert VK2.box_skip({}, 1280, 900) == "no element"
    for t in ("EISA Awards 2025/2026 Gewinner", "Lash Clusters Brown 0.2", "DC Components 1N4007 1A 1000V", "0,85 g"):
        assert not VK2.personal_text(t), t
    for t in ("info@shop.test", "Tel.: 04405 - 98 46 155", "+31 20 123 4567"):
        assert VK2.personal_text(t), t


def test_save_and_failures():
    good = base64.b64encode(png()).decode()
    with tempfile.TemporaryDirectory() as d:
        out = VK2.save_crops({"page": "", "crops": [{"value": "A b", "png_b64": good},
                                                    {"value": "C d", "png_b64": base64.b64encode(b"GIF89a").decode()},
                                                    {"value": "E f", "skipped": "hidden"}]},
                             os.path.join(d, "crops"), "www.shop.test", 1)
        a, b, c = out["crops"]
        assert a["file"] == "crops/www_shop_test/t1_00.png" and os.path.exists(os.path.join(d, a["file"]))
        assert b["skipped"] == "not a PNG" and c["skipped"] == "hidden" and "png_b64" not in a
    assert VK2.save_crops(None, "", "www.shop.test", 0) == {"crops_page": "no capture", "crops": []}
    # a capture that breaks never fails the screen; the render is returned with the reason
    out, _calls, _pages = _configured(lambda: run(lambda: FakePage(fail_capture=True), value_crops=True))
    tr = out["value_tries"][0]
    assert tr["crops_page"].startswith("capture failed") and tr["values"]


def test_other_sites_crop_the_first_naming_link():
    with tempfile.TemporaryDirectory() as d:
        def go():
            A4.CROPS_DIR = os.path.join(d, "crops")
            return run(FakePage, value_crops=True, site=F.MAG, rec=F.mag_rec())
        out, _calls, pages = _configured(go)
    tr = out["value_tries"][0]
    assert tr["crops"] and all(c.get("file") for c in tr["crops"])
    titles = {F.slug(t): t for t in F.MAG_TITLES}
    for c in tr["crops"]:
        assert c["href"].startswith("https://www.mag.test/artikel/")
        title = titles[c["href"].rstrip("/").rsplit("/", 1)[1]]
        assert c["value"].lower() in title.lower(), (c["value"], title)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
