"""A reused, per-thread Chromium pool for every audit that needs a real browser.

Launching a browser costs roughly a second of wall time and a few hundred MB
of RSS. Doing that per request -- which is what this service used to do --
made process startup, not the actual audit, the dominant cost of a call and
put a hard ceiling on how many audits a single container could serve. Keeping
one browser alive per worker thread and opening a cheap, isolated context per
request removes that cost from the hot path entirely.

Why thread-local rather than a shared pool object: Playwright's *sync* API is
bound to the thread that created it and is not safe to drive from another
thread. FastAPI runs these sync routes in anyio's threadpool, which app.main
caps at MAX_CONCURRENT_AUDITS, so the number of live browsers is bounded by
that cap rather than growing without limit.

Isolation is still per request: each call gets a fresh BrowserContext (its own
cookie jar, storage, and cache), so one customer's audit can never observe or
be influenced by another's. Only the expensive process is shared.
"""

import threading
from typing import Callable, TypeVar

from playwright.sync_api import sync_playwright

T = TypeVar("T")

_state = threading.local()

# --disable-dev-shm-usage matters specifically on Cloud Run: the container
# gets a small /dev/shm, and without this Chromium intermittently crashes
# mid-navigation on heavier pages rather than returning a result.
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _close_thread_browser() -> None:
    browser = getattr(_state, "browser", None)
    _state.browser = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            # Already dead or unreachable -- dropping the reference is the
            # whole point; a failure to close cleanly must not propagate.
            pass


def _get_browser():
    browser = getattr(_state, "browser", None)
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
        _close_thread_browser()

    playwright = getattr(_state, "playwright", None)
    if playwright is None:
        playwright = sync_playwright().start()
        _state.playwright = playwright

    browser = playwright.chromium.launch(args=_LAUNCH_ARGS)
    _state.browser = browser
    return browser


def with_page(fn: Callable[..., T], **context_kwargs) -> T:
    """Run `fn(page)` on a fresh context of this thread's pooled browser.

    Retries exactly once, and only when the pooled browser itself turns out
    to be dead (crashed, or reaped while the thread sat idle). An exception
    raised by `fn` is a real audit failure -- a navigation timeout, an
    unreachable host -- and is propagated unchanged rather than retried, so a
    genuinely failing audit still fails honestly instead of being masked by a
    second attempt.
    """
    last_error: Exception

    for attempt in (1, 2):
        browser = _get_browser()
        try:
            context = browser.new_context(**context_kwargs)
        except Exception as exc:
            # Could not even open a context: treat the browser as dead.
            _close_thread_browser()
            last_error = exc
            if attempt == 2:
                raise
            continue

        try:
            page = context.new_page()
            return fn(page)
        finally:
            try:
                context.close()
            except Exception:
                # A context that won't close means the browser is unhealthy;
                # drop it so the next request gets a fresh one.
                _close_thread_browser()

    raise last_error
