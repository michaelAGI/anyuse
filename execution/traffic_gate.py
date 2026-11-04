"""Task and site concurrency for a run (docs/TRAFFIC_POLICY.md, proposed).

Two rules from the proposal that no request guard can enforce, because they
are about TASKS, not requests:

* **one active task per site**: a site never has two tasks (a discovery, an
  attach session, a proof) running against it at the same time;
* **at most K sites in parallel**: at most K unrelated sites have an active
  task at once.

:class:`SiteGate` is the orchestration lock. Both limits default to ``None``
(off), which is today's behaviour: the benches already run one host at a time,
and nothing here changes a run that does not construct a gate with numbers.

When a gate is given to :class:`cdp_guard.RequestBudget` (``task_gate=``), a
guarded session refuses to navigate to a site that has no active task, so a
task that forgot to take the gate fails loudly instead of running unlocked.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Callable, Dict, Optional


class GateRefused(RuntimeError):
    """A navigation to a site without an active task, under an active gate."""


class SiteGate:
    def __init__(self, per_site: Optional[int] = None, max_sites: Optional[int] = None,
                 site_of: Optional[Callable[[str], str]] = None):
        for name, v in (("per_site", per_site), ("max_sites", max_sites)):
            if v is not None and int(v) < 1:
                raise ValueError(f"{name} must be >= 1 (or None: off)")
        self.per_site = int(per_site) if per_site else None
        self.max_sites = int(max_sites) if max_sites else None
        self.site_of = site_of or (lambda s: str(s or "").lower())
        #: site -> active tasks
        self.active: Dict[str, int] = {}
        self.peak_sites = 0
        self.peak_per_site = 0
        self._cond: Optional[asyncio.Condition] = None

    @property
    def enabled(self) -> bool:
        return bool(self.per_site or self.max_sites)

    def _room(self, site: str) -> bool:
        n = self.active.get(site, 0)
        if self.per_site and n >= self.per_site:
            return False
        if self.max_sites and n == 0 and len(self.active) >= self.max_sites:
            return False
        return True

    def try_acquire(self, site: str) -> bool:
        """Take a task slot for ``site`` now, or return False (never waits)."""
        site = self.site_of(site)
        if not self._room(site):
            return False
        self.active[site] = self.active.get(site, 0) + 1
        self.peak_sites = max(self.peak_sites, len(self.active))
        self.peak_per_site = max(self.peak_per_site, self.active[site])
        return True

    def release(self, site: str) -> None:
        site = self.site_of(site)
        n = self.active.get(site, 0) - 1
        if n <= 0:
            self.active.pop(site, None)
        else:
            self.active[site] = n

    def holds(self, site: str) -> bool:
        """Does ``site`` have an active task? Always True when the gate is off."""
        return (not self.enabled) or self.active.get(self.site_of(site), 0) > 0

    @contextlib.asynccontextmanager
    async def task(self, site: str):
        """``async with gate.task(site):`` waits for a slot, holds it, frees it."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        async with self._cond:
            await self._cond.wait_for(lambda: self._room(self.site_of(site)))
            self.try_acquire(site)
        try:
            yield
        finally:
            async with self._cond:
                self.release(site)
                self._cond.notify_all()

    @contextlib.contextmanager
    def task_sync(self, site: str):
        """Synchronous variant for sequential benches: refuses instead of waiting."""
        if not self.try_acquire(site):
            raise GateRefused(f"no task slot for {self.site_of(site)}: "
                              f"active {dict(self.active)}")
        try:
            yield
        finally:
            self.release(site)
