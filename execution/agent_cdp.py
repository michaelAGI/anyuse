"""The only part of the learn loop that speaks to a browser.

Kept in its own module so that importing :mod:`agent_browser` — which the CLI,
the tests and the learn loop all do — pulls in no websocket client and opens no
socket. Everything here is async and container-only; nothing in it can reach the
user's desktop, and :func:`agent_browser.assert_container_endpoint` has already
refused a loopback endpoint before a session is ever constructed.

The protocol surface is deliberately tiny — ``goto``, ``observe``, ``focus``,
``type``, ``press_enter``, ``click``, ``settle``, ``dom``, ``text``, ``url``,
``network`` — because it is also the seam every test fakes.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from anyuse.execution.agent_browser import (BOUND_PICK_FN, MIN_INTERVAL_S, OBSERVE_JS,
                                            PICK_JS, SNAPSHOT_JS, USER_AGENT, pick_refusal)

#: Response types whose bodies are never kept: they cannot explain a list and
#: they are the bulk of the bytes.
_SKIP_MIME = ("image/", "font/", "text/css", "video/", "audio/")
#: a body larger than this (encoded) is not pulled through the websocket
_MAX_BODY_FETCH = 5_000_000
#: the CDP object group that holds the nodes of the last observation
SNAPSHOT_GROUP = "anyuse-snapshot"


class CdpAgentSession:
    """One page target in the container browser, for the duration of one run."""

    #: OFF by default. On, every
    #: session opens its page in a NEW browser context (its own cookie jar,
    #: storage and cache; ``Target.createBrowserContext``), checks that the
    #: context starts with no cookies (``fresh_check``) and disposes of it at
    #: the end: each run starts fresh, nothing carries over from an earlier run.
    fresh_context: bool = False
    #: OFF by default; a demonstration's or the learn agent's own session sets it.
    #: On, several controls next to a search input are put to the configured small
    #: model (``cdp_guard.pick_submit_control``, inside an open model-check scope)
    #: before the word list; a replay never sets it.
    submit_chooser: bool = False

    def __init__(self, endpoint: str, user_agent: str = USER_AGENT,
                 allow_url=None):
        self.endpoint = endpoint.rstrip("/")
        self.user_agent = user_agent
        self._bws = None
        self._ws = None
        #: the browser context of a fresh-context session, and its start check
        self.context_id: Optional[str] = None
        self.fresh_check: Dict[str, Any] = {}
        self._target = None
        self._id = 0
        self._events: List[dict] = []
        self._last_hit: Dict[str, float] = {}
        #: ``(url) -> bool``. Every top-level navigation is checked BEFORE it is
        #: sent and failed if this says no. The recorder can check the one URL
        #: it chose; an agent clicks its own way around a site, so the only
        #: honest place for a robots check is in front of the request.
        self._allow_url = allow_url
        #: navigations this refused, for the report
        self.refused: List[str] = []
        #: per-page crash detection (Inspector/Target crash and detach events)
        from anyuse.execution.browser_supervisor import PageWatch
        self.watch = PageWatch()
        #: one reader at a time on the page websocket (a live solve observes
        #: and relays from another thread through the event loop)
        self._io_lock: Optional[asyncio.Lock] = None
        #: a request interceptor is active (Fetch): a dropped connection would
        #: release its paused requests unchecked, so it is never reconnected
        self._intercepting = allow_url is not None
        self.reconnects = 0
        #: string reads cut in the page (dom/text), for the report
        self.truncated_reads = 0
        #: set by the live-solve wiring (execution/live_solve.py)
        self.live_solve: Optional[dict] = None
        #: the last observation the model was shown: its element table and the
        #: CDP handle on its nodes (``observe``); None until the page is observed
        self._seen: Optional[Dict[str, Any]] = None
        #: what the last focus or click did: ``{"ok", "how", "at"}`` (see
        #: ``agent_browser.BOUND_PICK_FN``; ``how`` is ``index`` without an
        #: observation and ``unseen`` for an index the observation did not list)
        self.last_pick: Dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "CdpAgentSession":
        await self._pin_new_page(self._enable)
        return self

    async def _pin_new_page(self, setup) -> None:
        """Browser websocket, ONE new page target, its websocket, ``setup()``.
        Any failure closes what was opened (the target explicitly)."""
        from anyuse.execution.browser_supervisor import browser_ws_url, page_ws_url
        browser_ws = await browser_ws_url(self.endpoint)
        await self._connect_browser(browser_ws)
        try:
            await self._create_target()
            self.watch.target_id = self._target
            self._ws = await self._ws_connect(page_ws_url(self.endpoint, self._target))
            await setup()
        except BaseException:
            await self._close_target()
            raise

    async def _ws_connect(self, url: str):
        import websockets
        from anyuse.execution.browser_supervisor import (UNREACHABLE, WS_KEEPALIVE,
                                                         InfrastructureFailure)
        try:
            return await websockets.connect(url, max_size=64 * 1024 * 1024, **WS_KEEPALIVE)
        except Exception as exc:                  # noqa: BLE001
            raise InfrastructureFailure(UNREACHABLE, f"websocket: {type(exc).__name__}: "
                                                     f"{str(exc)[:100]}") from exc

    async def _connect_browser(self, browser_ws: str) -> None:
        self._bws = await self._ws_connect(browser_ws)

    async def _bcall(self, mid: int, method: str, params: dict, timeout: float = 15.0) -> dict:
        """One command on the BROWSER websocket (events in between are ignored)."""
        from anyuse.execution.browser_supervisor import STALLED, InfrastructureFailure
        try:
            await self._bws.send(json.dumps({"id": mid, "method": method, "params": params}))
        except Exception as exc:                  # noqa: BLE001
            raise self._infra(exc, method) from exc
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise InfrastructureFailure(STALLED, f"{method} got no answer")
            try:
                msg = json.loads(await asyncio.wait_for(self._bws.recv(), timeout=left))
            except asyncio.TimeoutError:
                continue
            except Exception as exc:              # noqa: BLE001
                raise self._infra(exc, method) from exc
            if msg.get("id") == mid:
                if "error" in msg:
                    raise InfrastructureFailure(STALLED, f"{method}: {msg['error']}")
                return msg.get("result") or {}

    async def _new_context(self) -> None:
        """A new browser context for this session, checked empty (no cookies)."""
        res = await self._bcall(2, "Target.createBrowserContext", {"disposeOnDetach": True})
        self.context_id = str(res.get("browserContextId") or "") or None
        n = None
        try:
            got = await self._bcall(3, "Storage.getCookies", {"browserContextId": self.context_id})
            n = len(got.get("cookies") or [])
        except Exception:                         # noqa: BLE001 - reported, never fatal
            n = None
        self.fresh_check = {"context": bool(self.context_id), "cookies_at_start": n,
                            "fresh": bool(self.context_id) and n == 0}

    async def _create_target(self, timeout: float = 15.0) -> None:
        """One new about:blank page target; its id is pinned for the session."""
        from anyuse.execution.browser_supervisor import STALLED, InfrastructureFailure
        params: Dict[str, Any] = {"url": "about:blank"}
        if self.fresh_context:
            await self._new_context()
            if self.context_id:
                params["browserContextId"] = self.context_id
        try:
            await self._bws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                             "params": params}))
        except Exception as exc:                  # noqa: BLE001
            raise self._infra(exc, "Target.createTarget") from exc
        deadline = time.monotonic() + timeout
        while self._target is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise InfrastructureFailure(STALLED, "Target.createTarget got no answer")
            try:
                msg = json.loads(await asyncio.wait_for(self._bws.recv(), timeout=left))
            except asyncio.TimeoutError:
                continue
            except Exception as exc:              # noqa: BLE001
                raise self._infra(exc, "Target.createTarget") from exc
            if msg.get("id") == 1:
                if "error" in msg:
                    raise InfrastructureFailure(STALLED, f"createTarget: {msg['error']}")
                self._target = msg["result"]["targetId"]

    async def _enable(self) -> None:
        await self.call("Network.enable", {"maxTotalBufferSize": 64_000_000,
                                           "maxResourceBufferSize": 32_000_000})
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        try:
            await self.call("Inspector.enable")     # targetCrashed / detached events
        except RuntimeError:
            pass
        await self.call("Network.setUserAgentOverride", {"userAgent": self.user_agent})
        if self._allow_url is not None:
            # Intercept top-level navigations only. A disallowed one is FAILED
            # before it leaves the browser, so a path robots.txt forbids is
            # never fetched — not fetched-then-noticed.
            await self.call("Fetch.enable", {"patterns": [
                {"urlPattern": "*", "requestStage": "Request",
                 "resourceType": "Document"}]})
        await self.call("Emulation.setDeviceMetricsOverride",
                        {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                         "mobile": False})

    async def __aexit__(self, *exc):
        try:
            if self._ws:
                try:
                    await asyncio.wait_for(self._ws.close(), timeout=5)
                except Exception:                 # noqa: BLE001
                    pass
        finally:
            await self._close_target()

    async def _close_target(self) -> None:
        """Close the pinned page EXPLICITLY: through the browser websocket, or,
        when that is gone, through ``/json/close`` (bounded, off the loop)."""
        closed = False
        if self._bws is not None and self._target:
            try:
                await self._bws.send(json.dumps(
                    {"id": 99, "method": "Target.closeTarget",
                     "params": {"targetId": self._target}}))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    msg = json.loads(await asyncio.wait_for(
                        self._bws.recv(), timeout=max(0.1, deadline - time.monotonic())))
                    if msg.get("id") == 99:
                        closed = "error" not in msg
                        break
            except Exception:                     # noqa: BLE001
                pass
        if not closed and self._target:
            from anyuse.execution.browser_supervisor import BrowserSupervisor
            try:
                await asyncio.to_thread(BrowserSupervisor(self.endpoint).close_target,
                                        self._target)
            except Exception:                     # noqa: BLE001
                pass
        if self.context_id and self._bws is not None:
            # the run's own context (cookies, storage, cache) goes with it
            try:
                await self._bcall(98, "Target.disposeBrowserContext",
                                  {"browserContextId": self.context_id}, timeout=5.0)
                self.fresh_check["disposed"] = True
            except Exception:                     # noqa: BLE001 - disposeOnDetach covers it
                self.fresh_check["disposed"] = False
        if self._bws is not None:
            try:
                await asyncio.wait_for(self._bws.close(), timeout=5)
            except Exception:                     # noqa: BLE001
                pass

    # -- protocol plumbing --------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._io_lock is None:
            self._io_lock = asyncio.Lock()
        return self._io_lock

    def _infra(self, exc: BaseException, what: str):
        from anyuse.execution.browser_supervisor import (InfrastructureFailure, classify)
        kind = classify(exc)
        if kind:
            return exc if isinstance(exc, InfrastructureFailure) else \
                InfrastructureFailure(kind, f"{what}: {type(exc).__name__}: {str(exc)[:120]}")
        return exc

    async def _stalled(self, method: str) -> BaseException:
        """A CDP call got no answer: the browser is down (unreachable) or the
        page is stuck (stalled). Either way it is infrastructure."""
        from anyuse.execution.browser_supervisor import (STALLED, UNREACHABLE,
                                                         InfrastructureFailure, aprobe)
        ok, why = await aprobe(self.endpoint)
        if not ok:
            return InfrastructureFailure(UNREACHABLE, f"{method} timed out; {why}")
        return InfrastructureFailure(STALLED, f"{method} timed out; browser answers, "
                                              f"the page does not")

    async def reconnect(self) -> None:
        """Re-pin the SAME target after a dropped page websocket, or fail.

        Never another target and never a new one; never while a request
        interceptor was active (its paused requests were released unchecked
        when the connection dropped, so the page is closed instead)."""
        from anyuse.execution.browser_supervisor import (CONNECTION_LOST, TARGET_GONE,
                                                         InfrastructureFailure,
                                                         http_get_json, page_ws_url)
        if self._intercepting or self.reconnects >= 1:
            await self._close_target()
            raise InfrastructureFailure(CONNECTION_LOST, "page connection dropped; not "
                                        "reconnected (interceptor active or already "
                                        "reconnected once)")
        try:
            rows = await asyncio.to_thread(http_get_json, f"{self.endpoint}/json/list", 3.0)
        except Exception as exc:                  # noqa: BLE001
            raise InfrastructureFailure(CONNECTION_LOST, f"/json/list: {type(exc).__name__}: "
                                                         f"{str(exc)[:100]}") from exc
        if not any(isinstance(r, dict) and r.get("id") == self._target for r in rows or []):
            raise InfrastructureFailure(TARGET_GONE, "the pinned page no longer exists")
        self.reconnects += 1
        self._ws = await self._ws_connect(page_ws_url(self.endpoint, self._target))
        await self._enable()

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 45.0, retry_safe: bool = False) -> dict:
        try:
            return await self._call(method, params, timeout)
        except Exception as exc:                  # noqa: BLE001
            from anyuse.execution.browser_supervisor import (CONNECTION_LOST,
                                                             InfrastructureFailure)
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and \
                    not isinstance(exc, InfrastructureFailure):
                raise await self._stalled(method) from exc
            err = self._infra(exc, method)
            if err is exc and not isinstance(exc, InfrastructureFailure):
                raise
            if isinstance(err, InfrastructureFailure) and err.kind == CONNECTION_LOST \
                    and retry_safe:
                await self.reconnect()
                return await self._call(method, params, timeout)
            if isinstance(err, InfrastructureFailure) and err.kind == CONNECTION_LOST:
                await self._close_target()
            raise err from (None if err is exc else exc)

    async def _call(self, method: str, params: Optional[dict], timeout: float) -> dict:
        self.watch.check()
        async with self._lock():
            self._id += 1
            mid = self._id
            await self._ws.send(json.dumps({"id": mid, "method": method,
                                            "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(method)
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=left))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})
                if "method" in msg:
                    self._events.append(msg)
                    self.watch.see(msg)
                    await self._maybe_gate(msg)
                    self.watch.check()
                elif "id" in msg:
                    await self._on_reply(msg)

    async def settle(self, seconds: float) -> None:
        self.watch.check()
        try:
            async with self._lock():
                await self._settle(seconds)
        except Exception as exc:                  # noqa: BLE001
            err = self._infra(exc, "settle")
            if err is exc:
                raise
            await self._close_target()
            raise err from exc

    async def _settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=left))
            except asyncio.TimeoutError:
                return
            if "method" in msg:
                self._events.append(msg)
                self.watch.see(msg)
                await self._maybe_gate(msg)
                self.watch.check()
            elif "id" in msg:
                await self._on_reply(msg)

    async def _on_reply(self, msg: dict) -> None:
        """The answer to a command sent without waiting (``GuardedSession``'s
        response-body reads for the web scope); nothing by default."""
        return None

    async def _maybe_gate(self, msg: dict) -> None:
        """Answer a paused navigation: continue it, or fail it on robots."""
        if self._allow_url is None or msg.get("method") != "Fetch.requestPaused":
            return
        params = msg.get("params") or {}
        rid = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        if not rid:
            return
        try:
            ok = bool(self._allow_url(url))
        except Exception:
            ok = False
        if not ok:
            # the ONE exception: while a person solves a challenge on THIS
            # page, its provider's own hosts (GET/HEAD) may load the check
            from anyuse.execution.live_solve import CHALLENGE_EXCEPTIONS
            ok = CHALLENGE_EXCEPTIONS.allows(
                self._target, url, str((params.get("request") or {}).get("method") or "GET"))
        self._id += 1
        if ok:
            await self._ws.send(json.dumps(
                {"id": self._id, "method": "Fetch.continueRequest",
                 "params": {"requestId": rid}}))
            return
        self.refused.append(url)
        await self._ws.send(json.dumps(
            {"id": self._id, "method": "Fetch.failRequest",
             "params": {"requestId": rid, "errorReason": "BlockedByClient"}}))

    async def _pace(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last_hit.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_hit[host] = time.monotonic()

    async def js(self, expr: str, read_only: bool = False) -> Any:
        """``read_only``: the expression only reads, so after a dropped
        connection it may be re-sent once to the SAME re-pinned target."""
        res = await self.call("Runtime.evaluate",
                              {"expression": expr, "returnByValue": True,
                               "awaitPromise": True}, retry_safe=read_only)
        return (res.get("result") or {}).get("value")

    async def _bounded_read(self, expr: str, limit: int) -> str:
        out = await self.js(expr, read_only=True) or ""
        if isinstance(out, str) and len(out) >= limit:
            self.truncated_reads += 1
        return out if isinstance(out, str) else str(out)

    # -- the surface the agent uses ----------------------------------------

    async def goto(self, url: str) -> None:
        self._seen = None
        await self._pace(url)
        await self.call("Page.navigate", {"url": url})
        await self.settle(3.0)

    async def observe(self) -> dict:
        """The page as the model is shown it. The listed nodes are kept behind
        a CDP object handle, so a later focus or click acts on the element the
        model chose even when the page has shifted since (``_pick``)."""
        from anyuse.execution.browser_supervisor import InfrastructureFailure
        handle, obs = None, None
        try:
            if self._seen and self._seen.get("handle"):
                await self.call("Runtime.releaseObjectGroup", {"objectGroup": SNAPSHOT_GROUP})
            res = await self.call("Runtime.evaluate", {"expression": SNAPSHOT_JS,
                                                       "objectGroup": SNAPSHOT_GROUP,
                                                       "returnByValue": False})
            if not res.get("exceptionDetails"):
                handle = (res.get("result") or {}).get("objectId")
            if handle:
                got = await self.call("Runtime.callFunctionOn", {
                    "objectId": handle, "returnByValue": True,
                    "functionDeclaration": "function () { return this.obs; }"})
                obs = (got.get("result") or {}).get("value")
        except InfrastructureFailure:
            raise
        except RuntimeError:
            handle, obs = None, None
        if not isinstance(obs, dict):
            handle, obs = None, await self.js(OBSERVE_JS) or {}
        self._seen = {"handle": handle, "elements": list(obs.get("elements") or [])}
        return obs

    async def _pick(self, index: int, act: bool) -> bool:
        """Focus (``act``: and click) the element the model saw at ``index``.

        Without an observation this is a pick by index alone (``PICK_JS``).
        With one, the page resolves the element the model saw and refuses
        when it cannot (``BOUND_PICK_FN``); ``last_pick`` says which."""
        from anyuse.execution.browser_supervisor import InfrastructureFailure
        self.last_pick = {}
        seen = self._seen
        if seen is None:
            ok = bool(await self.js(PICK_JS % ("true" if act else "false", int(index))))
            self.last_pick = {"ok": ok, "how": "index"}
            return ok
        rows = seen.get("elements") or []

        def row(i: int) -> Optional[Dict[str, str]]:
            r = rows[i] if 0 <= i < len(rows) else None
            return {k: str(r.get(k) or "") for k in ("tag", "type", "name", "href")} \
                if isinstance(r, dict) else None
        index = int(index)
        want = row(index)
        if want is None:
            # the model named an index it was not shown: there is nothing to
            # bind to, and as with a pick by index nothing there to act on
            self.last_pick = {"ok": False, "how": "unseen"}
            return False
        equal = [i for i in range(len(rows)) if row(i) == want]
        opts = {"index": index, "act": bool(act), "expect": want,
                "prev": row(index - 1), "next": row(index + 1),
                "rank": equal.index(index), "count": len(equal)}
        res = None
        if seen.get("handle"):
            try:
                got = await self.call("Runtime.callFunctionOn", {
                    "objectId": seen["handle"], "functionDeclaration": BOUND_PICK_FN,
                    "arguments": [{"objectId": seen["handle"]}, {"value": opts}],
                    "returnByValue": True})
                res = (got.get("result") or {}).get("value")
            except InfrastructureFailure:
                raise
            except RuntimeError:
                # the handle died with its document: match the row alone
                seen["handle"] = None
        if not seen.get("handle"):
            res = await self.js(f"({BOUND_PICK_FN})(null, {json.dumps(opts)})")
        self.last_pick = dict(res) if isinstance(res, dict) else {"ok": False, "how": "error"}
        return bool(self.last_pick.get("ok"))

    async def challenge_observation(self) -> dict:
        """What the live-solve detector reads (url, text, a markup head, the
        request URLs). Only for the solver's own checks; never for a model."""
        obs = dict(await self.js(OBSERVE_JS) or {})
        try:
            obs["markup"] = (await self.js(
                "(document.documentElement ? document.documentElement.outerHTML : '')"
                ".slice(0, 200000)", read_only=True)) or ""
        except Exception as exc:                  # noqa: BLE001
            if self._infra(exc, "x") is not exc:
                raise
        obs["requests"] = [{"url": str(((e.get("params") or {}).get("request") or {})
                                       .get("url") or "")}
                           for e in self._events[-400:]
                           if e.get("method") == "Network.requestWillBeSent"]
        return obs

    def live_bridge(self, loop):
        """This page as the live solver's page (headless mode)."""
        from anyuse.execution.live_solve import MODE_HEADLESS, AsyncPageBridge

        async def call(method, params):
            return await self.call(method, params, timeout=20.0)
        return AsyncPageBridge(loop, str(self._target), self.challenge_observation, call,
                               mode=MODE_HEADLESS, viewport=(1280, 900))

    async def focus(self, index: int) -> bool:
        return await self._pick(index, act=False)

    async def type(self, text: str) -> None:
        await self.call("Input.insertText", {"text": str(text)})
        await self.settle(1.0)

    async def press_enter(self) -> None:
        from anyuse.execution.cdp_guard import PAGE_SIG_JS
        try:
            before = await self.js(PAGE_SIG_JS)
        except Exception:
            before = None
        for kind in ("rawKeyDown", "char", "keyUp"):
            await self.call("Input.dispatchKeyEvent",
                            {"type": kind, "key": "Enter", "code": "Enter",
                             "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                             "text": "\r" if kind == "char" else ""})
        await self.settle(5.0)
        # Enter did nothing (same URL, same page) -> click the search's own
        # submit control next to the focused input, once (a search box whose
        # form listens to its button only)
        if before:
            try:
                after = await self.js(PAGE_SIG_JS)
                if after == before:
                    res = await self.click_submit(-1)
                    if res.get("clicked"):
                        self.submitted_by_button = str(res.get("how") or "button")
                        await self.settle(5.0)
            except Exception:
                pass

    async def click_submit(self, idx: int, prefer_label: str = "", legacy: bool = False,
                           words14: bool = False) -> dict:
        """Click the search's own submit control for input ``idx`` (-1: the
        focused one): the form's default button, else the one control next to
        the input (``cdp_guard.SUBMIT_SEARCH_JS``). Among several: the recorded
        label (``prefer_label``, a replay), then the configured small model's
        pick (only with :attr:`submit_chooser`), then a label the submit words
        name (``cdp_guard.SUBMIT_WORDS``; ``words14``: a web tool before
        version 15, ``SUBMIT_WORDS_V14``), else nothing. The pick is checked
        against the page before the click (``SUBMIT_PICK_JS``). ``legacy``: the
        rule of web tools before version 14 (``SUBMIT_SEARCH_JS_V13``).
        Returns ``{clicked, how, label}``."""
        from anyuse.execution import cdp_guard as G
        if legacy:
            res = await self.js(G.SUBMIT_SEARCH_JS_V13 % int(idx))
            return res if isinstance(res, dict) else {"clicked": False}
        res = await self.js(G.SUBMIT_SEARCH_JS % int(idx))
        if not isinstance(res, dict):
            return {"clicked": False}
        if res.get("clicked") or res.get("how") != "candidates":
            return res
        cands = [c for c in res.get("candidates") or [] if isinstance(c, dict)]
        labels = [str(c.get("label") or "") for c in cands]
        k, how = None, ""
        want = " ".join(str(prefer_label or "").split())
        if want and want in labels:
            k, how = labels.index(want), "the recorded control"
        if k is None and self.submit_chooser:
            try:
                url = str(await self.js("location.href", read_only=True) or "")
            except Exception:                              # noqa: BLE001
                url = ""
            try:
                k = await asyncio.to_thread(G.pick_submit_control, cands,
                                            dict(res.get("field") or {}), url)
            except Exception:                              # noqa: BLE001 - no opinion
                k = None
            how = "the control the model chose" if k is not None else ""
        if k is None:
            words = G.SUBMIT_WORDS_V14 if words14 else G.SUBMIT_WORDS
            k = next((i for i, lab in enumerate(labels) if words.search(lab)), None)
            how = "a control named like a search" if k is not None else ""
        if k is None:
            return {"clicked": False, "how": f"{len(cands)} controls next to the input, none chosen",
                    "candidates": len(cands)}
        got = await self.js(G.SUBMIT_PICK_JS % (int(idx), int(cands[k].get("k", k)),
                                                json.dumps(labels[k])))
        if not isinstance(got, dict) or not got.get("clicked"):
            return {"clicked": False, "how": str((got or {}).get("how") or "the pick was not clicked")}
        return {"clicked": True, "how": how, "label": labels[k]}

    async def form_get_url(self, idx: int, start: str, value: str = "") -> str:
        """The same-site URL input ``idx``'s own GET form submits to, when it is
        not ``start`` and carries ``value`` (``cdp_guard.FORM_GET_URL_JS``; a control
        clicked before may have emptied the box); else ``""``."""
        from anyuse.execution.cdp_guard import FORM_GET_URL_JS
        from anyuse.execution.site_canon import leaves_site
        try:
            got = await self.js(FORM_GET_URL_JS % int(idx))
        except Exception:                                  # noqa: BLE001
            return ""
        if not isinstance(got, dict) or got.get("method") != "get":
            return ""
        url = str(got.get("url") or "")
        if not url.startswith(("http://", "https://")) or leaves_site(url, start) or \
                url.split("#")[0].rstrip("/") == str(start or "").split("#")[0].rstrip("/"):
            return ""
        if value:
            from anyuse.execution.api_compile import locate_param
            if locate_param(url, value).where == "none":
                return ""
        return url

    async def search(self, index: int, text: str) -> dict:
        """The site's search as ONE transaction: focus the control, REPLACE
        its text, read it back (type once more if the page wiped it), submit
        (Enter, then the search's own button when Enter moved nothing), wait
        for the page to settle. Returns what happened."""
        from anyuse.execution.cdp_guard import SELECT_TEXT_JS, VALUE_OF_FOCUSED_JS
        out: dict = {"focused": await self.focus(index)}
        if not out["focused"]:
            return out
        await self.js(SELECT_TEXT_JS)
        await self.call("Input.insertText", {"text": str(text)})
        await self.settle(1.0)
        held = await self.js(VALUE_OF_FOCUSED_JS)
        out["held"] = isinstance(held, str) and str(text).strip().lower() in held.lower()
        if isinstance(held, str) and not out["held"]:
            out["retyped"] = True
            await self.js(SELECT_TEXT_JS)
            await self.call("Input.insertText", {"text": str(text)})
            await self.settle(1.0)
        self.submitted_by_button = ""
        from anyuse.execution.cdp_guard import PAGE_SIG_JS
        try:
            start, sig = str(await self.js("location.href") or ""), await self.js(PAGE_SIG_JS)
        except Exception:                                  # noqa: BLE001
            start, sig = "", None
        await self.submit_search(str(text))
        out["submit"] = self.submitted_by_button or "enter"
        try:
            if sig and await self.js(PAGE_SIG_JS) == sig:
                # Enter and the submit control moved nothing: the input's own GET form
                url = await self.form_get_url(-1, start, str(text))
                if url:
                    out["form_url"] = url
                    await self.goto(url)
        except Exception:                                  # noqa: BLE001
            pass
        try:
            out["url"] = str(await self.js("location.href"))[:200]
        except Exception:
            pass
        return out

    async def submit_search(self, text: str) -> None:
        """Enter in the focused search input (with the button fallback) and
        wait; a guarded session waits for held navigations and the result
        region (``GuardedSession.submit_search``)."""
        await self.press_enter()

    async def click(self, index: int) -> None:
        await self._pick(index, act=True)
        # a refused click only waits for the page to finish changing; the
        # agent observes it again before it acts
        await self.settle(1.0 if pick_refusal(self) else 5.0)

    async def consent_controls(self) -> List[dict]:
        """Visible consent controls, inspected by the reject-only policy."""
        from anyuse.execution.consent import CONTROLS_JS
        return list(await self.js(CONTROLS_JS) or [])

    async def click_consent(self, index: int) -> bool:
        """Click one just-inspected control; there is no accept helper."""
        from anyuse.execution.consent import click_js
        clicked = bool(await self.js(click_js(index)))
        if clicked:
            await self.settle(2.0)
        return clicked

    async def screenshot(self) -> str:
        """A JPEG of THIS container page as a data URL (escalation level L3).

        CDP ``Page.captureScreenshot`` on the container's own target: it sees
        the headless page and nothing else, never the host's screen.
        """
        from anyuse.execution.live_solve import QUARANTINE, ChallengeQuarantined
        if QUARANTINE.active(self._target):
            # this screenshot feeds a model (L3); a challenge page is never sent
            raise ChallengeQuarantined("no model sees a challenge page")
        res = await self.call("Page.captureScreenshot",
                              {"format": "jpeg", "quality": 55,
                               "captureBeyondViewport": False})
        data = res.get("data") or ""
        return f"data:image/jpeg;base64,{data}" if data else ""

    async def dom(self) -> str:
        from anyuse.execution.browser_supervisor import DOM_EXPR, MAX_DOM_CHARS
        return await self._bounded_read(DOM_EXPR, MAX_DOM_CHARS)

    async def text(self) -> str:
        from anyuse.execution.browser_supervisor import MAX_TEXT_CHARS, TEXT_EXPR
        return await self._bounded_read(TEXT_EXPR, MAX_TEXT_CHARS)

    async def url(self) -> str:
        return await self.js("location.href", read_only=True) or ""

    async def network(self, want_bodies: bool = True) -> List[dict]:
        """The request log, in the same shape the human recorder produces."""
        from anyuse.execution.browser_supervisor import InfrastructureFailure
        reqs: Dict[str, dict] = {}
        for e in self._events:
            m = e.get("method")
            p = e.get("params") or {}
            rid = p.get("requestId")
            if not rid:
                continue
            if m == "Network.requestWillBeSent":
                r = p.get("request", {})
                # a redirect reuses the request id: keep the hops it came through
                hops = (list(reqs[rid].get("redirected_from") or []) + [reqs[rid]["url"]]
                        if p.get("redirectResponse") and rid in reqs else [])
                reqs[rid] = {"url": r.get("url", ""), "method": r.get("method", "GET"),
                             "headers": dict(r.get("headers") or {}),
                             "post_data": r.get("postData"),
                             "resource_type": p.get("type", ""),
                             "t_start": p.get("timestamp"), "status": None,
                             "mime": "", "body": None, "bytes": 0,
                             "response_headers": {}}
                if hops:
                    reqs[rid]["redirected_from"] = hops[-10:]
            elif m == "Network.responseReceived" and rid in reqs:
                resp = p.get("response", {})
                reqs[rid].update(status=resp.get("status"),
                                 mime=resp.get("mimeType", ""),
                                 response_headers=dict(resp.get("headers") or {}),
                                 resource_type=p.get("type", reqs[rid]["resource_type"]))
            elif m == "Network.loadingFinished" and rid in reqs:
                reqs[rid]["t_end"] = p.get("timestamp")
                reqs[rid]["bytes"] = p.get("encodedDataLength", 0)

        out = []
        for rid, r in reqs.items():
            if want_bodies and r["status"] and not any(
                    r["mime"].startswith(s) for s in _SKIP_MIME) and \
                    int(r.get("bytes") or 0) <= _MAX_BODY_FETCH:
                try:
                    res = await self.call("Network.getResponseBody",
                                          {"requestId": rid}, timeout=15)
                    r["body"] = None if res.get("base64Encoded") else res.get("body")
                except InfrastructureFailure:
                    raise
                except Exception:
                    r["body"] = None
            out.append(r)
        return out
