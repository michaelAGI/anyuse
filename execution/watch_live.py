"""The live replay of ``anyuse watch run``: one watched item's compiled tool, in a guarded browser page under
the traffic guard - the SAME guard stack every measured web bench runs (``execution/web_runtime/web_arch3_live.SiteCtx``:
robots.txt respected on every host, the shared pacer, >= 3 s between documents, GET only but the web scope's
read-only search rules, no third-party host, the per-site request and byte caps, the NEVER_CONTACT refusals (and
the government ones with ANYUSE_REFUSE_GOV_HOSTS); ``execution/web_runtime/web_arch2_live.session_class``: a NEW anonymous browser context per replay).

Per replay: the site gate (``traffic_gate.SiteGate``, one task per site) is held for the tool's site; the
tool's DECLARED hosts become the run's origin policy (``demo_tool.apply_site_hosts``) and
their robots.txt is read first; the caps are the watch run's (``watch.RUN_REQUESTS`` / ``RUN_BYTES``, less
what the site's day has used); a traffic violation is a hard stop; a block is recorded (the site is never
contacted again by watch, and it goes to NEVER_CONTACT as for every bench). 0 model calls in a watch run: no
``model_check`` scope is open and ``watch.run_watch`` gives the in-run verifier its rules alone
(``attribute_verify.no_model``). The MCP web-tool route replays through the same stack inside a tool call's scope.

Nothing here is imported unless a live run is asked for (``anyuse watch run --browser``).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(HERE, "execution", "web_runtime")
#: the watch's user agent: the benches' (read-only, person-paced)
PHASE = "watch"
RESULT_WAIT_S = 8.0


def _bench():
    if BENCH not in sys.path:
        sys.path.insert(0, BENCH)
    import web_arch3_live as L3  # noqa: E402
    import web_arch4_live as A4  # noqa: E402
    import web_arch_live as L  # noqa: E402
    return L, L3, A4


#: the workflows that walk pages beyond the search (breadth exploration)
BREADTH_WORKFLOWS = ("paged_search", "visit_results")


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _route_traffic(ctx, traffic: Dict[str, Any]) -> Dict[str, Any]:
    """The replay's traffic record with the ROUTE's own robots reading (the record's ``robots_refused`` reads its status
    from the product module's cache, which the route does not use, so it says "absent" for hosts whose robots.txt
    the route could not read) and what an ``ANYUSE_ROBOTS`` override let through."""
    tr = dict(traffic or {})
    notes = getattr(ctx.robots, "notes", {}) or {}
    rr = {}
    for h, rec in (tr.get("robots_refused") or {}).items():
        rec = dict(rec)
        if h in notes:
            n = str(notes[h])
            rec["why"] = n[:400]
            rec["robots"] = "rules" if n.startswith("parsed") else "unreadable" if n.endswith("disallow all") else \
                "absent"
        elif rec.get("robots") != "unreadable":
            rec["robots"], rec["why"] = "unknown", "robots.txt not read by the route"
        rr[h] = rec
    tr["robots_refused"] = rr
    if getattr(ctx.robots, "override", ""):
        tr["robots_override"] = ctx.robots.override
        tr["robots_overridden"] = {h: dict(v) for h, v in list((ctx.robots.overrides or {}).items())[:20]}
    return tr


def make_replay(endpoint: str, cache_dir: str, log: Callable[[str], None] = lambda s: None, *,
                contact: Any = None, inner: Optional[Callable[..., Dict[str, Any]]] = None):
    """``replay(item, tool, caps) -> record`` for ``watch.run_watch``: ``demo_tool.run_tool``'s output plus the
    guard's ``requests``, ``bytes``, ``hosts_sent``, ``violations`` (a traffic violation) and the robots note.

    The MCP web-tool route (``execution/web_tool_call.py``) replays through this same guard stack. Two optional seams,
    unused by ``anyuse watch run`` (its replay is unchanged): ``contact`` - an object with ``why()`` ("" when a contact
    may be made now) and ``allow(allow_url)`` (``contact_guard.ContactGuard``), asked before the site
    and wrapped around every request (a refused request is never sent); ``inner(ctx, endpoint, tool, value) -> record`` - what runs inside the site's
    guarded context instead of one ``run_tool`` page (the repair ladder's pages, the fallback agent's page), on the
    same budget, robots and caps."""
    from anyuse.execution import demo_compile as C
    from anyuse.execution import demo_tool as T
    from anyuse.execution.cdp_guard import TrafficViolation
    from anyuse.execution.site_modes import add_user_never_contact
    from anyuse.execution.traffic_gate import SiteGate
    from anyuse.execution.web_scope import contact_refusal
    L, L3, A4 = _bench()

    def replay(item: Dict[str, Any], tool: Dict[str, Any], caps: Dict[str, int]) -> Dict[str, Any]:
        site = str(tool.get("site") or item.get("site"))
        value = str(item.get("input") or "")
        rec: Dict[str, Any] = {"status": "error", "why": "", "model_calls": 0}
        why = contact_refusal(site)
        if why:
            return dict(rec, status="refused", why=f"refused before any request: {why}", requests=0, bytes=0)
        if contact is not None:
            paused = str(contact.why() or "")
            if paused:
                return dict(rec, status="refused", why=f"the contact guard pauses before any request: {paused}"[:300],
                            requests=0, bytes=0, paused=True)
        gate = SiteGate(per_site=1, max_sites=1)
        # the agent path's browser-statics rule - static assets load like a normal
        # browser visit, counted apart; the watch run's request and byte caps count documents and data
        # ANYUSE_ROBOTS: a tool that walks pages beyond the one search (paged / visited
        # results) is breadth exploration - robots.txt applies to it under user_initiated
        from anyuse.execution import api_permission as AP
        breadth = str(tool.get("workflow") or "") in BREADTH_WORKFLOWS
        with (AP.unattended("breadth") if breadth else _nullcontext()):
            ctx = L3.SiteCtx(site, PHASE, int(caps["requests"]), int(caps["bytes"]), endpoint, gate,
                             browser_statics=True, search_post=True, refuse_competitors=False)
        if contact is not None:
            ctx.allowed = contact.allow(ctx.allowed)  # per request: a refused request is never sent
        pol = T.apply_site_hosts(ctx.budget, tool)
        old_cache = L.CACHE_DIR
        L.CACHE_DIR = cache_dir
        t0 = time.monotonic()
        try:
            L.load_cache(ctx.budget, site)
            with gate.task_sync(site):
                for h in tool.get("hosts") or [site]:
                    if contact_refusal(h):
                        continue
                    ctx.robots.load(h)
                first = C.fill((tool.get("search") or {}).get("template") or tool.get("start_url") or "", value) \
                    if (tool.get("search") or {}).get("mode") == "url" else str(tool.get("start_url") or "")
                if not ctx.allowed(first):
                    rec.update(status="refused", why=f"robots.txt (or the guard) refuses {first[:120]}")
                else:
                    if inner is None:
                        from anyuse.execution import typed_readiness as TR
                        out = A4.in_page(ctx, endpoint, lambda page: T.run_tool(page, tool, value, TR.PageSettle(),
                                                                              RESULT_WAIT_S))
                    else:
                        out = inner(ctx, endpoint, tool, value)
                    ctx.budget.check()
                    rec = dict(out)
        except TrafficViolation as exc:
            rec.update(status="error", why=f"traffic violation: {exc}"[:300])
        except Exception as exc:                       # noqa: BLE001 - a replay failure is a record (drift)
            rec.update(status="error", why=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            try:
                L.save_cache(ctx.budget, site)
            except Exception:                          # noqa: BLE001
                pass
            L.CACHE_DIR = old_cache
        from collections import Counter
        rec.update(requests=ctx.used(), bytes=ctx.nbytes(), seconds=round(time.monotonic() - t0, 2),
                   violations=list(ctx.budget.violations) or None,
                   hosts_sent=dict(Counter(h for (h, _t, _u, _ty) in ctx.budget.sent)), origin_policy=pol,
                   # every third-party host on the run record (mode, grants, refusals)
                   third_party=ctx.scope.site_report(site) if getattr(ctx, "scope", None) is not None else None,
                   robots={h: ctx.robots.notes.get(h) for h in ctx.robots.notes},
                   # the browser statics counted apart (documents + data are ``requests`` / ``bytes``)
                   traffic=_route_traffic(ctx, traffic=ctx.budget.traffic(site)))
        if rec.get("status") == "blocked":
            try:
                add_user_never_contact(site, f"anyuse watch: {str(rec.get('why'))[:120]}")
            except Exception:                          # noqa: BLE001
                pass
        log(f"{item.get('id')}: {rec.get('status')} ({rec['requests']} requests, {rec['bytes']} bytes)")
        return rec

    return replay
