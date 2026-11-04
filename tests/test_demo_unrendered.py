"""Web tool version 11: a result page that shows no text is loading.

The case (www.garrafeiranacional.com): every run's result document ended after its ``<head>`` - the parser waited
for a script, the page had no body and no text - and the tool judged it "membership: no item of the compiled
region on the result page" (S and U, all three values: 4 breaks, 2 still failing), a structure
failure for a page that never rendered.

What must hold (a fake page shaped on the saved page):

1. a version-11 tool waits once more when the result page shows no text, and fails EXPLICITLY ("the page did not
   render") when it still shows none - never a judgement of an unrendered page;
2. a page that renders during the extra wait is read and answered;
3. a version-10 tool runs exactly as before (the membership failure).
"""
import os as _os_arm
# the extractor's / replay's own answers (arm S) as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution import web_arch_run as RR  # noqa: E402

SITE = "www.wine.test"
BASE = f"https://{SITE}"
REGION = {"kind": "cluster", "template": BASE + "/{v}", "where": "path", "width": 1,
          "regex": r"^https?://(?:www\.)?wine\.test/[^/?#]+(?:\?[^#]*)?(?:#.*)?$"}
RESULTS = [("Casal Garcia Branco", "casal-garcia-branco"), ("Casal Garcia Rosé", "casal-garcia-rose")]


class Page:
    """The result URL renders ``render_after`` seconds after its navigation (never when None): before, no body and no
    text."""

    def __init__(self, render_after=None):
        self.cur, self.render_after, self.reads, self.t0 = "about:blank", render_after, 0, time.monotonic()

    def _rendered(self):
        return self.render_after is not None and time.monotonic() - self.t0 >= self.render_after

    async def goto(self, url):
        self.cur, self.t0 = url, time.monotonic()

    async def settle(self, s):
        await asyncio.sleep(0.02)

    async def js(self, expr, read_only=False):
        if expr in (RR.READ_JS, RR.READ_JS_V11):
            self.reads += 1
            if not self._rendered():
                return {"url": self.cur, "title": "Resultados", "elements": [], "text": "", "main_text": ""}
            els = [{"i": k, "tag": "a", "href": f"{BASE}/{u}", "text": t, "area": "main"}
                   for k, (t, u) in enumerate(RESULTS)]
            return {"url": self.cur, "title": "Resultados", "elements": els,
                    "text": "Resultados\n" + "\n".join(t for t, _u in RESULTS), "main_text": ""}
        return None

    async def search_inputs(self):
        return []

    async def network(self, want_bodies=True):
        return [{"resource_type": "Document", "url": self.cur, "status": 200}]

    async def url(self):
        return self.cur

    async def dom(self):
        return "<html><head><script src='/merged.js'></script></head></html>"


async def _settle(page):
    return None


def tool(version):
    return {"version": version, "workflow": "site_search", "site": SITE, "start_url": BASE + "/",
            "search": {"mode": "url", "template": BASE + "/search?q={q}", "start_url": BASE + "/"},
            "region": REGION, "top_k": 5, "output": ["results"], "hosts": [SITE],
            "checks": ["search_page", "membership", "query", "empty_state"]}


def run(version, page, wait=0.2):
    return asyncio.run(T.run_tool(page, tool(version), "Casal Garcia", _settle, result_wait_s=wait))


def test_an_unrendered_page_is_an_explicit_failure_in_version_11():
    out = run(11, Page())
    assert out["status"] == "failed" and out["why"].startswith("result_page: the page did not render"), out["why"]
    old = run(10, Page())
    assert old["status"] == "failed" and old["why"].startswith("membership: no item"), old["why"]


def test_a_page_that_renders_during_the_extra_wait_is_answered():
    out = run(11, Page(render_after=1.5), wait=1.0)
    assert out["status"] == "ok" and [r["title"] for r in out["output"]["results"]] == [t for t, _u in RESULTS], out
    assert "render_wait_s" in out


def test_a_rendered_page_is_read_once_as_before():
    p = Page(render_after=0)
    out = run(11, p)
    assert out["status"] == "ok" and "render_wait_s" not in out


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
