"""Real, rule-based checks for the SEO, security, and performance audit
endpoints -- same philosophy as the WCAG/axe-core audit in app/main.py:
deterministic, verifiable signals only. Nothing here is an LLM guessing at
quality, and nothing here is ever coerced into a false pass on error; a
check that couldn't run raises, and the caller (main.py) turns that into
an honest 502, never a fabricated result.

Each function returns `{"status": "ok", "pass": bool, ..., "findings": [...]}`
where every finding is `{"id": str, "severity": str, "detail": str}`,
the one finding shape every route in this service returns.

These are real, narrow, disclosed signals -- not a replacement for a full
SEO audit, a penetration test, or a Lighthouse run. Each function's
docstring says exactly what it does and does not check.
"""

from html.parser import HTMLParser
from typing import Optional

import httpx

try:
    from . import browser_pool
except ImportError:
    # Loaded by file path rather than as part of the `app` package (see the
    # matching fallback in main.py). Register under the same canonical name
    # main.py uses and reuse anything already loaded, so both entry points
    # share ONE browser_pool -- two copies would mean two thread-local
    # browsers per worker thread, doubling memory for no benefit.
    import importlib.util
    import sys
    from pathlib import Path as _Path

    _POOL_NAME = "wcag_audit_engine_browser_pool"
    browser_pool = sys.modules.get(_POOL_NAME)
    if browser_pool is None:
        _spec = importlib.util.spec_from_file_location(
            _POOL_NAME, _Path(__file__).resolve().parent / "browser_pool.py"
        )
        browser_pool = importlib.util.module_from_spec(_spec)
        sys.modules[_POOL_NAME] = browser_pool
        _spec.loader.exec_module(browser_pool)

USER_AGENT = "HubVibeAuditBot/1.0 (+https://hubvibe.dev)"
_USER_AGENT = USER_AGENT  # backwards-compatible alias
_HTTP_TIMEOUT = 15.0

_SEVERITY_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


def _sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(f["severity"], 4))


def _has_blocking_finding(findings: list) -> bool:
    return any(f["severity"] in ("critical", "serious") for f in findings)


class _SEOParser(HTMLParser):
    """Collects only the tags an SEO/social-sharing check cares about --
    not a general-purpose HTML parser."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta: dict = {}
        self.canonical: Optional[str] = None
        self.h1_count = 0
        self.has_json_ld = False
        self.html_lang: Optional[str] = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs.get("name") or attrs.get("property")
            if name and attrs.get("content") is not None:
                self.meta[name.lower()] = attrs["content"]
        elif tag == "link" and (attrs.get("rel") or "").lower() == "canonical":
            self.canonical = attrs.get("href")
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self.has_json_ld = True
        elif tag == "html" and attrs.get("lang"):
            self.html_lang = attrs["lang"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class TargetNotFetchable(Exception):
    """A hop in this fetch pointed somewhere this service will not go."""


_BLOCKED_TARGET_HOSTS = {"localhost", "metadata", "metadata.google.internal"}
_MAX_REDIRECTS = 5


def blocked_target_reason(url: Optional[str]) -> Optional[str]:
    """Why `url` must not be fetched, or None when it may be.

    The single implementation behind both the pre-payment gate in main.py and
    the per-hop checks below, so the two can never disagree about what is
    reachable. Reads ALLOW_PRIVATE_TARGETS per call rather than at import, so a
    reloaded module and a patched environment both see the truth.
    """
    import ipaddress
    import os
    import socket
    from urllib.parse import urlparse

    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return "is not a valid URL"
    if parsed.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "has no host"
    if os.environ.get("ALLOW_PRIVATE_TARGETS") == "1":
        return None
    if host in _BLOCKED_TARGET_HOSTS or host.endswith(".internal") or host.endswith(".localhost"):
        return "points at an internal host, which this service will not fetch"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OverflowError):
        return "does not resolve to any address"
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return "resolves to an unparseable address"
        if not address.is_global:
            return (
                "resolves to a private, loopback, link-local or reserved address, "
                "which this service will not fetch"
            )
    return None


def fetch_once(url: str):
    """One HTTP GET whose response can serve several audits.

    The SEO and security audits both need the same GET of the same URL --
    SEO wants the body, security wants the headers and the post-redirect
    URL. Fetching separately doubles the load we put on the site being
    audited for no new information, and a bundle that hits a stranger's
    origin four times per call is how an audit bot earns itself a block.

    Redirects are followed BY HAND, re-checking every hop. httpx's own
    follow_redirects=True made the caller's URL the only address this service
    ever validated: a public host answering `302 Location:
    http://169.254.169.254/...` walked the fetch straight past the gate and
    into the deployment, which is the proxy the gate exists to refuse. The
    destination is what decides, and it is known only one hop at a time.
    """
    seen = url
    for _ in range(_MAX_REDIRECTS + 1):
        problem = blocked_target_reason(seen)
        if problem is not None:
            raise TargetNotFetchable(f"redirected to a URL that {problem}")
        response = httpx.get(
            seen,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        # Read the status rather than httpx's is_redirect: this must decide
        # correctly for a stubbed response too, and an attribute that is
        # merely present would read as "redirect" on any mock.
        if getattr(response, "status_code", None) not in (301, 302, 303, 307, 308):
            return response
        try:
            location = response.headers.get("location")
        except Exception:
            location = None
        if not location:
            return response
        seen = str(httpx.URL(seen).join(location))
    raise TargetNotFetchable(f"followed more than {_MAX_REDIRECTS} redirects")


HEAVY_PAGE_BYTES = 3_000_000


def response_bytes(response) -> Optional[int]:
    """Bytes this response transferred, or None when that cannot be measured.

    `content-length` alone undercounts badly: HTTP/1.1 chunked responses omit
    it entirely, which is the normal shape for compressed or streamed HTML.
    Those responses were counted as zero, so a genuinely heavy page could stay
    under the weight threshold and be reported clean -- a check that never ran
    reported as a pass, which this service exists not to do.

    Falls back to the body the browser already holds. Returns None rather than
    zero when even that is unavailable (a redirect, a preflight, a body the
    browser dropped), so the caller can tell "nothing" from "unknown".
    """
    try:
        length = response.headers.get("content-length")
    except Exception:
        length = None
    if length and str(length).isdigit():
        return int(length)
    try:
        return len(response.body())
    except Exception:
        return None


def goto_guarded(page, url: str, **goto_kwargs):
    """Navigate `page` to `url` with every request checked, not just the first.

    Chromium follows redirects itself, so a gate on the caller's URL stopped at
    hop one exactly as httpx did. A route handler is the only place the browser
    lets us see each destination before it is fetched, so it decides per
    request and aborts anything this service will not reach. Subresources go
    through the same check: a page that cannot navigate to the metadata
    endpoint can still ask for it with an <img> or a fetch().

    Hosts are resolved once per page and remembered, so a page with a hundred
    images costs one lookup per distinct host rather than a hundred.
    """
    problem = blocked_target_reason(url)
    if problem is not None:
        raise TargetNotFetchable(f"target {problem}")

    verdicts = {}

    def _route(route):
        try:
            from urllib.parse import urlparse

            request_url = route.request.url
            host = (urlparse(request_url).hostname or "").lower()
            if host not in verdicts:
                verdicts[host] = blocked_target_reason(request_url)
            if verdicts[host] is not None:
                route.abort()
                return
            route.continue_()
        except Exception:
            # A guard that errors must not become a guard that allows.
            try:
                route.abort()
            except Exception:
                pass

    page.route("**/*", _route)
    response = page.goto(url, **goto_kwargs)

    # Playwright raises only on a transport failure, so a 404 or 500 error page
    # loads perfectly well and would be audited as if it were the page that was
    # asked for. A customer with a typo in their URL would be handed a clean
    # bill of health for their host's error page. There is no page to audit
    # here, so this fails as an audit that could not run -- which the caller
    # turns into a 502 that bills nothing -- rather than a passing result about
    # the wrong document.
    status = getattr(response, "status", None)
    if isinstance(status, int) and status >= 400:
        raise TargetNotFetchable(
            f"responded HTTP {status}, so there was no page at that URL to audit"
        )
    return response


def run_seo_audit(html: Optional[str], url: Optional[str], response=None) -> dict:
    """Checks title, meta description, H1 structure, canonical link,
    OpenGraph tags, JSON-LD structured data, and the <html lang> attribute.

    Deliberately fetches raw HTML (no JS execution) rather than a
    Playwright-rendered DOM: that's what search-engine and social-card
    crawlers actually see, so it's the more representative signal for SEO
    specifically -- unlike WCAG/performance, which need the rendered page.
    """
    if not html and not url:
        raise ValueError("Provide 'html' or 'url'")
    if not html:
        resp = response if response is not None else fetch_once(url)
        resp.raise_for_status()
        html = resp.text

    parser = _SEOParser()
    parser.feed(html)

    findings = []
    title = parser.title.strip()
    if not title:
        findings.append({"id": "missing-title", "severity": "critical", "detail": "No <title> tag found"})
    elif len(title) > 60:
        findings.append(
            {
                "id": "title-too-long",
                "severity": "moderate",
                "detail": f"Title is {len(title)} characters (recommended <= 60)",
            }
        )

    description = parser.meta.get("description")
    if not description:
        findings.append(
            {"id": "missing-meta-description", "severity": "critical", "detail": "No meta description found"}
        )
    elif len(description) > 160:
        findings.append(
            {
                "id": "meta-description-too-long",
                "severity": "moderate",
                "detail": f"Meta description is {len(description)} characters (recommended <= 160)",
            }
        )

    if parser.h1_count == 0:
        findings.append({"id": "missing-h1", "severity": "serious", "detail": "No <h1> tag found"})
    elif parser.h1_count > 1:
        findings.append(
            {
                "id": "multiple-h1",
                "severity": "moderate",
                "detail": f"{parser.h1_count} <h1> tags found (expected exactly 1)",
            }
        )

    if not parser.canonical:
        findings.append(
            {"id": "missing-canonical", "severity": "minor", "detail": "No canonical <link> tag found"}
        )

    missing_og = [tag for tag in ("og:title", "og:description", "og:image", "og:type") if tag not in parser.meta]
    if missing_og:
        findings.append(
            {
                "id": "incomplete-opengraph",
                "severity": "moderate",
                "detail": f"Missing OpenGraph tags: {', '.join(missing_og)}",
            }
        )

    if not parser.has_json_ld:
        findings.append(
            {"id": "missing-structured-data", "severity": "minor", "detail": "No JSON-LD structured data found"}
        )

    if not parser.html_lang:
        findings.append(
            {"id": "missing-lang-attribute", "severity": "minor", "detail": "No lang attribute on <html>"}
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings),
        "checks": "title, meta-description, h1-structure, canonical, opengraph, structured-data, lang",
        "findings": findings,
    }


def run_security_audit(url: Optional[str], response=None) -> dict:
    """Checks the final response's transport (HTTPS) and a handful of
    security-relevant response headers: HSTS, CSP, X-Content-Type-Options,
    clickjacking protection, Referrer-Policy, and CORS.

    This is a real HTTP response inspection, not a TLS/cipher-suite scan
    and not a penetration test -- it reports what's observable in a single
    plain request's response headers.
    """
    if not url:
        raise ValueError("Provide 'url'")
    resp = response if response is not None else fetch_once(url)
    final_url = str(resp.url)
    headers = {k.lower(): v for k, v in resp.headers.items()}

    findings = []
    if not final_url.startswith("https://"):
        findings.append(
            {"id": "no-https", "severity": "critical", "detail": f"Final URL is not HTTPS: {final_url}"}
        )

    hsts = headers.get("strict-transport-security")
    if hsts is None:
        findings.append(
            {"id": "missing-hsts", "severity": "serious", "detail": "No Strict-Transport-Security header"}
        )
    else:
        # Presence is not protection. `max-age=0` is the spec's own way to
        # SWITCH HSTS OFF and tell browsers to forget the pin, and a header
        # with no readable max-age directs nothing at all -- reporting either
        # as protected is the false pass this audit exists to avoid.
        max_age = None
        for directive in str(hsts).split(";"):
            name, _, value = directive.strip().partition("=")
            if name.strip().lower() == "max-age":
                digits = value.strip().strip('"')
                if digits.isdigit():
                    max_age = int(digits)
                break
        if max_age is None:
            findings.append(
                {
                    "id": "invalid-hsts",
                    "severity": "serious",
                    "detail": f"Strict-Transport-Security has no readable max-age: {hsts!r}",
                }
            )
        elif max_age == 0:
            findings.append(
                {
                    "id": "hsts-disabled",
                    "severity": "serious",
                    "detail": "Strict-Transport-Security is max-age=0, which switches HSTS off",
                }
            )

    if "content-security-policy" not in headers:
        findings.append(
            {"id": "missing-csp", "severity": "moderate", "detail": "No Content-Security-Policy header"}
        )

    if headers.get("x-content-type-options", "").lower() != "nosniff":
        findings.append(
            {
                "id": "missing-x-content-type-options",
                "severity": "moderate",
                "detail": "X-Content-Type-Options: nosniff not set",
            }
        )

    # Again value, not presence: X-Frame-Options only protects when it reads
    # DENY or SAMEORIGIN. ALLOWALL and any unrecognised token are ignored by
    # browsers, which is the same as having sent nothing.
    xfo = str(headers.get("x-frame-options", "")).strip().lower()
    has_frame_protection = xfo in ("deny", "sameorigin") or "frame-ancestors" in headers.get(
        "content-security-policy", ""
    )
    if not has_frame_protection:
        findings.append(
            {
                "id": "missing-frame-protection",
                "severity": "moderate",
                "detail": "No X-Frame-Options header or CSP frame-ancestors directive",
            }
        )

    if "referrer-policy" not in headers:
        findings.append(
            {"id": "missing-referrer-policy", "severity": "minor", "detail": "No Referrer-Policy header"}
        )

    if headers.get("access-control-allow-origin") == "*":
        findings.append(
            {
                "id": "wildcard-cors",
                "severity": "minor",
                "detail": "Access-Control-Allow-Origin is '*' -- confirm this is intentional",
            }
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings),
        "checks": "https, hsts, csp, x-content-type-options, frame-protection, referrer-policy, cors",
        "findings": findings,
    }


def performance_result_from_metrics(
    dom_node_count: int,
    resource_bytes: int,
    request_count: int,
    unmeasured_responses: int = 0,
) -> dict:
    """Score already-measured page metrics.

    Split out from run_performance_audit so a caller that has already loaded
    the page for another reason -- /audit/bundle, which needs the same page
    rendered for the accessibility check -- can score it without paying for a
    second page load of the same URL.
    """
    findings = []
    if dom_node_count > 1500:
        findings.append(
            {
                "id": "high-dom-complexity",
                "severity": "moderate",
                "detail": f"{dom_node_count} DOM nodes (recommended <= 1500)",
            }
        )
    if resource_bytes > 3_000_000:
        findings.append(
            {
                "id": "heavy-page-weight",
                "severity": "moderate",
                "detail": f"{resource_bytes / 1_000_000:.1f} MB transferred (recommended <= 3 MB)",
            }
        )
    if request_count > 100:
        findings.append(
            {
                "id": "high-request-count",
                "severity": "minor",
                "detail": f"{request_count} network requests (recommended <= 100)",
            }
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings) and len(findings) == 0,
        "metrics": {
            "dom_node_count": dom_node_count,
            "total_bytes_transferred": resource_bytes,
            "request_count": request_count,
            # Responses whose size could not be established at all. Reported
            # rather than hidden: it is the difference between "this page is
            # light" and "we could not weigh part of it", and the buyer is
            # entitled to tell those apart.
            "unmeasured_responses": unmeasured_responses,
        },
        "findings": findings,
        "disclosure": "Single-page-load measurement, not a full Lighthouse-style audit.",
    }


def run_performance_audit(url: Optional[str]) -> dict:
    """Loads the page in a real browser (Playwright/Chromium) and reports
    DOM node count, total transferred bytes (from Content-Length response
    headers), and request count from a single page load.

    Real measured values from one load on this server's network, not a
    full Lighthouse-style audit (no field data, no repeated-run averaging,
    no render-timing metrics).
    """
    if not url:
        raise ValueError("Provide 'url'")

    resource_bytes = 0
    request_count = 0
    unmeasured = 0

    def _on_response(response):
        nonlocal resource_bytes, request_count, unmeasured
        request_count += 1
        # Once the page is already over the reporting threshold the exact
        # total changes no verdict, so stop pulling bodies for it.
        if resource_bytes > HEAVY_PAGE_BYTES:
            return
        measured = response_bytes(response)
        if measured is None:
            unmeasured += 1
        else:
            resource_bytes += measured

    def _measure(page) -> int:
        page.on("response", _on_response)
        goto_guarded(page, url, wait_until="networkidle", timeout=30000)
        return page.evaluate("document.querySelectorAll('*').length")

    # Pooled browser, fresh isolated context per call -- see browser_pool.
    # Isolation matters for this audit in particular: a shared cache would
    # make transferred-bytes and request-count read low on any URL a previous
    # audit had already warmed, silently reporting a page as lighter than it is.
    dom_node_count = browser_pool.with_page(_measure, user_agent=_USER_AGENT)

    return performance_result_from_metrics(
        dom_node_count, resource_bytes, request_count, unmeasured_responses=unmeasured
    )
