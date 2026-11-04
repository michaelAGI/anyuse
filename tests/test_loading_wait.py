"""A page that announces it is loading (www.drako.it's results page was read while its grid still said "Caricamento
prodotti..." in a role=status box, and the agent answered without rows). Pinned: the one settle rule
(``typed_readiness.settle_page``: the agent, the proof replays, the MCP route) waits, at most ``LOADING_WAIT_S`` more,
while the page ANNOUNCES it is loading (``LOADING_JS``: a visible status / progressbar / aria-busy region, or a loading
box whose short text says loading); the wait is recorded; a page that never announces anything, or cannot answer, is
settled as before; an announcement that stays ends at the ceiling. No browser, network or model.
    ANYUSE_TEST_PYTHON=... zsh tools/guarded_tests/run_allowed.sh test_loading_wait.py
"""
import asyncio
import pathlib
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent
_LINK = tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)
sys.path.insert(0, str(_HERE))

from anyuse.execution import typed_readiness as TR  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Page:
    """Quiet DOM; announces loading for ``n`` polls (``n=None``: always)."""

    def __init__(self, clock, n=3, answer=True):
        self.clock, self.n, self.answer, self.asked = clock, n, answer, 0
        self._events = []

    async def js(self, expr):
        if expr == TR.LOADING_JS:
            if not self.answer:
                raise RuntimeError("context gone")
            self.asked += 1
            if self.n is None or self.asked <= self.n:
                return {"why": "status", "text": "Caricamento prodotti…"}
            return None
        return {"quiet_ms": 5000, "mutations": 3, "ready": "complete"}

    async def settle(self, s):
        self.clock.t += s


def test_a_loading_announcement_is_waited_for_and_recorded():
    clock = Clock()
    trail = []
    d = asyncio.run(TR.settle_page(Page(clock, n=3), trail=trail, clock=clock))
    assert d["loading"]["ended"] == "gone" and d["loading"]["text"] == "Caricamento prodotti…"
    assert 1.0 <= d["loading"]["waited_s"] <= 2.0 and trail[-1] is d


def test_an_announcement_that_stays_ends_at_the_ceiling():
    clock = Clock()
    d = asyncio.run(TR.settle_page(Page(clock, n=None), clock=clock))
    assert d["loading"]["ended"] == "ceiling" and d["loading"]["waited_s"] >= TR.LOADING_WAIT_S


@pytest.mark.parametrize("page_kw", [{"n": 0}, {"answer": False}])
def test_no_announcement_or_no_answer_settles_as_before(page_kw):
    clock = Clock()
    d = asyncio.run(TR.settle_page(Page(clock, **page_kw), clock=clock))
    assert "loading" not in d and clock.t < 1.0


def test_the_probe_names_the_cases():
    js = TR.LOADING_JS
    for part in ("[role=status]", "[role=progressbar]", "[aria-busy=true]", "caricamento", "laden", "chargement"):
        assert part in js


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
