"""Python 3.14 compatibility: ``asyncio.get_event_loop()`` raises when no loop
is "current" for the thread and none is running — confirmed directly against
this interpreter, not assumed:

    >>> import asyncio; asyncio.get_event_loop()
    RuntimeError: There is no current event loop in thread 'MainThread'.

``watcher/batch_watcher.py`` called exactly that, inside a
coroutine that is always entered via ``await`` — so in practice a loop IS
always running there and ``get_event_loop()`` does not actually raise at
those call sites today (verified: it returns the running loop when one is
running, on both 3.11 and 3.14). The fix moves them to
``asyncio.get_running_loop()`` anyway, on purpose: it is the correct,
warning-free idiom for "the loop that is definitely already running here",
it is what ``mcpserver/guard_evaluator.py`` already uses for the same
reason, and it removes the LAST use of the deprecated, soon-fully-removed
spelling from a call site that would break the moment either method is ever
reached from a context asyncio didn't set up for it (e.g. a bare ``await``
inside a generator driven by something other than
``asyncio.run``/``run_until_complete``).

This file does two things without ever calling the real ``BatchWatcher.start()``
(which starts a real Quartz event tap — blocked by the desktop guard
and not something a host test may risk touching, ``docs/TEST_POLICY.md``):

1. A static regression guard: the recorder never spells the deprecated call again.
2. A synthetic, isolated reproduction of the exact mechanism the fix relies
   on — ``get_running_loop()`` succeeds under precisely the condition
   (``get_event_loop()`` raising with "no current event loop") that is
   confirmed above to be real on this interpreter.
"""
import asyncio
import os
import re
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(relpath):
    with open(os.path.join(_HERE, relpath)) as fh:
        return fh.read()


def test_batch_watcher_no_longer_spells_the_deprecated_call():
    src = _source(os.path.join("watcher", "batch_watcher.py"))
    assert "asyncio.get_event_loop()" not in src, (
        "watcher/batch_watcher.py still calls the deprecated asyncio.get_event_loop()")
    assert re.search(r"self\._loop\s*=\s*asyncio\.get_running_loop\(\)", src), (
        "watcher/batch_watcher.py:start() must capture asyncio.get_running_loop()")
    print("OK  batch_watcher.py uses get_running_loop(), not get_event_loop()")




def test_get_event_loop_raises_with_no_current_loop_on_this_interpreter():
    """The actual 3.14 mechanism the fix defends against, confirmed live (not
    assumed): outside any running loop and with no loop ever set as current
    for this thread, get_event_loop() raises. (On 3.11 this same call instead
    emits a DeprecationWarning and creates one — so this assertion is
    expected to legitimately differ per interpreter; that difference IS the
    defect the fix removes reliance on.) This is the LAST use of the current
    event loop in this process on purpose — asyncio.run() below manages its
    own loop and does not consult "the current loop", so clearing it here has
    no effect on the rest of this file.
    """
    asyncio.set_event_loop(None)
    try:
        asyncio.get_event_loop()
        got_no_raise = True
    except RuntimeError as e:
        got_no_raise = False
        assert "no current event loop" in str(e).lower(), e

    if sys.version_info >= (3, 14):
        assert got_no_raise is False, (
            "expected asyncio.get_event_loop() to raise with no current loop on 3.14+")
        print("OK  confirmed on this (3.14+) interpreter: get_event_loop() raises")
    else:
        print(f"OK  interpreter is {sys.version_info[:2]} (<3.14); "
              f"get_event_loop()-with-no-loop raised={not got_no_raise} (informational)")


def test_get_running_loop_succeeds_inside_a_running_loop_regardless():
    """The actual fix: from INSIDE a running coroutine, get_running_loop()
    always succeeds — the exact replacement for the pattern start() used, on every interpreter this runs under."""
    async def inner():
        return asyncio.get_running_loop()

    loop = asyncio.run(inner())
    assert loop is not None
    print("OK  get_running_loop() succeeds from inside a running coroutine "
          f"(python {sys.version_info[:2]})")


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in sorted(vars(mod).items()):
        if not (n.startswith("test_") and callable(fn)):
            continue
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1
        except Exception as e:
            f += 1
            print("FAIL", n, "::", repr(e)[:300])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
