"""Web page retrieval and extraction.

REUSES THE BROWSER THIS SERVICE ALREADY RUNS. The audit engine keeps a warm
per-thread Chromium pool (app/browser_pool.py); rendering a page for a worker
is the same operation an audit already performs. So this adapter does not
launch anything -- it is handed the pool's `with_page` at configure() time.

Two providers, in fallback order:
  1. browser   -- real Chromium, so JS-rendered pages extract correctly
  2. http-fetch -- plain GET, no JS, for when the browser is unavailable or
                   the page is static anyway

That fallback is not decoration: Chromium is the heaviest thing on the box and
the first casualty of memory pressure, and a static page extracts perfectly
well without it.

INJECTION RATHER THAN IMPORT: the workers package is loaded by file path in
this repo's tests, where `from .. import browser_pool` does not resolve. The
pool arrives through configure(), which also keeps this module importable --
and unit-testable -- with no browser present at all.
"""

import asyncio
import os
import re
from typing import Callable, Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_TIMEOUT = float(os.environ.get("WORKER_FETCH_TIMEOUT_SECONDS", "45"))
MAX_TEXT_CHARS = int(os.environ.get("WORKER_MAX_EXTRACT_CHARS", "40000"))

_with_page: Optional[Callable] = None
_executor = None
_goto_guarded: Optional[Callable] = None
_blocked_target_reason: Optional[Callable] = None


def configure(with_page: Optional[Callable] = None, executor=None,
              goto_guarded: Optional[Callable] = None,
              blocked_target_reason: Optional[Callable] = None) -> None:
    """Hand this module the audit engine's browser pool and its target guard.

    `goto_guarded` is the audit code's own navigation guard, and
    `blocked_target_reason` the rule behind it (private, loopback, link-local
    and reserved addresses, plus internal hostnames). Both are REUSED rather
    than reimplemented: a second copy of an SSRF rule is a second thing to
    forget to update, and the copy that drifts is the one guarding the fetch.
    """
    global _with_page, _executor, _goto_guarded, _blocked_target_reason
    _with_page = with_page
    _executor = executor
    _goto_guarded = goto_guarded
    _blocked_target_reason = blocked_target_reason


async def _get_guarded(url: str):
    """Follow redirects BY HAND, one hop at a time, checking every hop
    against the same guard as the first.

    httpx's own follow_redirects would take us wherever the target says to
    go: a perfectly public URL that answers 302 to http://169.254.169.254/
    turns a paid fetch into a cloud credential read. Checking only the URL
    the caller supplied is not a guard, it is a formality. Returns
    (response, final_url).
    """
    current = url
    response = None
    for _hop in range(6):
        problem = target_problem(current)
        if problem:
            raise runtime.InvalidRequest(f"`url` {problem}.")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                response = await client.get(current, headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{current} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{current} unreachable: {exc}") from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise runtime.InvalidProviderResponse(
                    f"{current} redirected without a destination.")
            current = str(httpx.URL(current).join(location))
            continue
        return response, current
    raise runtime.InvalidRequest("`url` redirected too many times.")


def target_problem(url: str) -> Optional[str]:
    """Why this URL must not be fetched, or None.

    FAILS CLOSED when the guard was not injected: a deployment that did not
    wire it refuses every fetch rather than fetching whatever it is told to.
    An unguarded fetcher behind a paywall is a service that will read a cloud
    metadata endpoint for anyone with $0.10.
    """
    if _blocked_target_reason is None:
        return ("this deployment has no target guard configured, so no URL "
                "can be fetched")
    return _blocked_target_reason(url)


_TAG_STRIP = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>",
                        re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    text = _TAG_STRIP.sub(" ", html or "")
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = _TAGS.sub(" ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", "\n".join(l.strip() for l in text.splitlines())).strip()


def _title_of(html: str) -> Optional[str]:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    return _WS.sub(" ", _TAGS.sub("", match.group(1))).strip() if match else None


def _meta_description(html: str) -> Optional[str]:
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html or "", re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _truncate(text: str) -> tuple:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


class _BrowserExtractor:
    id = "browser"

    def available(self) -> bool:
        return _with_page is not None and _executor is not None

    def unavailable_reason(self) -> str:
        return "browser pool not configured on this deployment"

    async def extract(self, url: str) -> runtime.ProviderResult:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())

        # Checked here as well as in the skill: this is the last line before a
        # socket is opened, and it is the only one a future caller cannot skip.
        problem = target_problem(url)
        if problem:
            raise runtime.InvalidRequest(f"`url` {problem}.")

        def _render(page):
            if _goto_guarded is not None:
                _goto_guarded(page, url, wait_until="networkidle", timeout=30000)
            else:  # pragma: no cover - only when the guard was not injected
                page.goto(url, wait_until="networkidle", timeout=30000)
            return {
                "title": page.title(),
                "text": page.evaluate("document.body ? document.body.innerText : ''"),
                "html_length": page.evaluate("document.documentElement.outerHTML.length"),
                "links": page.evaluate(
                    "Array.from(document.querySelectorAll('a[href]')).slice(0,100)"
                    ".map(a => ({text: (a.innerText||'').trim().slice(0,120), href: a.href}))"),
                "description": page.evaluate(
                    "(document.querySelector('meta[name=\\\"description\\\"]')||{}).content || null"),
                "final_url": page.url,
            }

        loop = asyncio.get_running_loop()
        try:
            rendered = await loop.run_in_executor(_executor, lambda: _with_page(_render, user_agent=USER_AGENT))
        except Exception as exc:
            # A navigation failure is the SITE failing, not the browser. Still
            # transient from our side: sites time out and come back.
            raise runtime.TransientProviderError(
                f"Could not render {url}: {type(exc).__name__}: {exc}") from exc

        text, truncated = _truncate((rendered.get("text") or "").strip())
        if not text:
            raise runtime.InvalidProviderResponse(f"{url} rendered with no readable text.")
        return runtime.ProviderResult(
            value={"url": url, "final_url": rendered.get("final_url") or url,
                   "title": rendered.get("title"), "description": rendered.get("description"),
                   "text": text, "text_chars": len(text), "truncated": truncated,
                   "links": rendered.get("links") or [], "rendered": True},
            # Our own flat-rate box: the marginal provider cost really is zero.
            cost_micros=0, cost_measured=True, usage=f"chars={len(text)}")


class _HttpExtractor:
    id = "http-fetch"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def extract(self, url: str) -> runtime.ProviderResult:
        response, current = await _get_guarded(url)

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"{url} returned {response.status_code}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"{url} returned {response.status_code}; nothing to extract.")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise runtime.PermanentProviderError(
                f"{url} is {content_type or 'an unknown type'}; this worker extracts "
                "HTML and text.")

        html = response.text
        text, truncated = _truncate(_html_to_text(html))
        if not text:
            raise runtime.InvalidProviderResponse(f"{url} contained no readable text.")
        return runtime.ProviderResult(
            value={"url": url, "final_url": current, "title": _title_of(html),
                   "description": _meta_description(html), "text": text,
                   "text_chars": len(text), "truncated": truncated, "links": [],
                   "rendered": False},
            cost_micros=0, cost_measured=True, usage=f"chars={len(text)}")


PROVIDERS = [_BrowserExtractor(), _HttpExtractor()]

# --- raw fetch: status/headers/body, no extraction, no browser ------------
#
# A worker whose whole point is showing the RAW response must never fall
# back to the browser -- rendering would hide the very thing (redirect
# chain, status code, actual bytes) a caller paying for raw fetch is asking
# to see.

_TEXTUAL_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
                  "application/javascript", "application/ld+json", "application/rss",
                  "application/atom")
MAX_FETCH_TEXT_CHARS = int(os.environ.get("WORKER_MAX_FETCH_CHARS", "500000"))


class _RawFetcher:
    id = "http-fetch-raw"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def fetch(self, url: str) -> runtime.ProviderResult:
        response, final_url = await _get_guarded(url)

        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        textual = any(content_type.startswith(t) for t in _TEXTUAL_TYPES) or not content_type
        text, truncated = None, False
        if textual:
            body = response.text or ""
            truncated = len(body) > MAX_FETCH_TEXT_CHARS
            text = body[:MAX_FETCH_TEXT_CHARS]

        return runtime.ProviderResult(
            value={"url": url, "final_url": final_url, "status": response.status_code,
                   "content_type": content_type or None, "bytes": len(response.content or b""),
                   "text": text, "truncated": truncated,
                   "headers": {k.lower(): v for k, v in response.headers.items()}},
            # Every status code IS the answer -- a 404 or 500 from the
            # target is not a failed fetch, it is a completed one.
            cost_micros=0, cost_measured=True, usage=f"status={response.status_code}")


FETCH_PROVIDERS = [_RawFetcher()]
