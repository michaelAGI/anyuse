"""Web tool version 12: a result page that did not render is reloaded ONCE.

The case (www.garrafeiranacional.com): every run's result document ended after its ``<head>`` - the parser waited for
a script a cold browser cache did not hold (a warm cache held it). Version 11 waits once more and then fails
explicitly ("the page did not render"). A second load of the same URL finds the script cached.

What must hold (a fake page that renders only from its second load on):

1. a version-12 tool whose result page shows no text after the extra wait loads the same URL ONCE more (one GET) and
   answers when it renders (``render_reload`` in the record);
2. a page that never renders: exactly one reload, then the explicit failure "... and a reload)";
3. a version-11 tool runs exactly as before (no reload; the version-11 failure); a page that renders at once is never
   reloaded.
"""
import os as _os_arm
# the extractor's / replay's own answers as these tests were written: the in-run verifier is ON by default
# and is pinned off here unless a test sets it (test_verifier_defaults.py covers it on)
_os_arm.environ.setdefault("ANYUSE_ATTRIBUTE_VERIFY", "off")
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import demo_tool as T  # noqa: E402
from test_demo_unrendered import BASE, RESULTS, Page, tool  # noqa: E402


class ColdPage(Page):
    """The result URL renders from its ``render_from``-th load on (never when None)."""

    def __init__(self, render_from=None):
        super().__init__(render_after=None)
        self.render_from, self.loads, self.gotos = render_from, {}, []

    async def goto(self, url):
        await super().goto(url)
        self.gotos.append(url)
        self.loads[url] = self.loads.get(url, 0) + 1
        self.render_after = 0 if (self.render_from is not None and self.loads[url] >= self.render_from) else None


async def _settle(page):
    return None


def run(version, page, wait=0.1):
    return asyncio.run(T.run_tool(page, tool(version), "Casal Garcia", _settle, result_wait_s=wait))


def test_one_reload_renders_the_page():
    p = ColdPage(render_from=2)
    out = run(12, p)
    assert out["status"] == "ok" and [r["title"] for r in out["output"]["results"]] == [t for t, _u in RESULTS], out
    assert out["render_reload"].startswith(BASE + "/search") and p.gotos == [p.gotos[0], p.gotos[0]], p.gotos


def test_a_page_that_never_renders_is_reloaded_once_then_fails():
    p = ColdPage(render_from=None)
    out = run(12, p)
    assert out["status"] == "failed" and out["why"].endswith("and a reload)"), out["why"]
    assert len(p.gotos) == 2 and p.gotos[0] == p.gotos[1]


def test_version_11_unchanged_and_a_rendered_page_never_reloaded():
    p = ColdPage(render_from=2)
    out = run(11, p)
    assert out["status"] == "failed" and out["why"].startswith("result_page: the page did not render") and \
        out["why"].endswith("of result wait)") and len(p.gotos) == 1 and "render_reload" not in out
    p = ColdPage(render_from=1)
    out = run(12, p)
    assert out["status"] == "ok" and len(p.gotos) == 1 and "render_reload" not in out and "render_wait_s" not in out


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok {name}")
    print(f"{n} tests ok")
