"""Navigation retry, offline (fake pages; no browser, no network, no model): ONE bounded retry of a
navigation that got no answer.

www.vibilagare.se: the tool's first document (its compiled search URL) got no answer for 60 s while
the browser answered ("Page.navigate timed out; browser answers, the page does not"); the bench
filed the whole site as infrastructure (3 requests lost). Diagnosis (offline): the guard sent the
one document, the site did not answer it.

What must hold:

1. a navigation that stalls once is retried once with the same URL, and the invocation records the
   retry as infrastructure (``infra_retries``); its answer is the answer (not a new attempt);
2. a second stall in the same invocation raises (the bench files infrastructure as before);
3. another infrastructure failure (the browser unreachable) and an ordinary error are never retried;
4. the oracle's renders retry the same way and record it.
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

from anyuse.execution import demo_compile as C  # noqa: E402
from anyuse.execution import demo_oracle as O  # noqa: E402
from anyuse.execution import demo_tool as T  # noqa: E402
from anyuse.execution.browser_supervisor import (STALLED, UNREACHABLE,  # noqa: E402
                                                 InfrastructureFailure)
from test_demo_cards import FakePage, compiled, gear_html  # noqa: E402

STALL = InfrastructureFailure(STALLED, "Page.navigate timed out; browser answers, the page does not")


class StallingPage(FakePage):
    """``stalls``: how many of the first navigations raise ``exc`` (as the guarded session does after
    its 60 s Page.navigate timeout)."""

    def __init__(self, pages, stalls=1, exc=STALL):
        super().__init__(pages)
        self.stalls, self.exc = stalls, exc

    async def goto(self, url):
        self.gotos.append(url)
        if self.stalls > 0:
            self.stalls -= 1
            raise self.exc
        self.cur = url


async def _settle(page):
    return None


def run(page, t, value):
    return asyncio.run(T.run_tool(page, t, value, _settle, result_wait_s=0.5))


def test_one_stall_is_retried_once_and_recorded():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    page = StallingPage({url: gear_html()}, stalls=1)
    out = run(page, t, "Gear")
    assert out["status"] == "ok" and out["accepted"] and len(out["output"]["results"]) == 5, out
    assert page.gotos == [url, url], page.gotos
    r = out["infra_retries"]
    assert len(r) == 1 and r[0]["url"] == url and "Page.navigate" in r[0]["why"], r


def test_a_second_stall_raises():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    page = StallingPage({url: gear_html()}, stalls=2)
    try:
        run(page, t, "Gear")
    except InfrastructureFailure as exc:
        assert exc.kind == STALLED and page.gotos == [url, url], page.gotos
    else:
        raise AssertionError("a second stall must raise")


def test_other_failures_are_never_retried():
    t = compiled()
    url = C.fill(t["search"]["template"], "Gear")
    page = StallingPage({url: gear_html()}, stalls=1, exc=InfrastructureFailure(UNREACHABLE, "/json/version: refused"))
    try:
        run(page, t, "Gear")
    except InfrastructureFailure as exc:
        assert exc.kind == UNREACHABLE and page.gotos == [url], page.gotos
    else:
        raise AssertionError("an unreachable browser must raise")
    page = StallingPage({url: gear_html()}, stalls=1, exc=RuntimeError("Page.navigate: net::ERR_NAME_NOT_RESOLVED"))
    out = run(page, t, "Gear")
    assert out["status"] == "error" and not out["accepted"] and page.gotos == [url], out
    assert T.nav_stalled(STALL) and not T.nav_stalled(InfrastructureFailure(STALLED, "Runtime.evaluate timed out"))


def test_the_oracle_retries_the_same_way():
    t = compiled()
    start = t["start_url"]
    url = C.fill(t["search"]["template"], "Gear")
    page = StallingPage({start: gear_html(), url: gear_html()}, stalls=1)
    ev = asyncio.run(O.oracle_item(page, url, _settle))
    assert ev.get("url") == url and len(ev["infra_retries"]) == 1 and page.gotos == [url, url], ev


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
