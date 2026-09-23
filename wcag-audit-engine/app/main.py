import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

try:
    from . import ard, audits, billing, browser_pool, mpp_payments, x402_payments
except ImportError:
    # Loaded directly by file path (e.g. by tooling/tests) rather than as
    # part of the `app` package -- fall back to loading each sibling module
    # from its file path under a service-specific name, not a bare `import
    # billing`. Another service in this repo also has a module literally
    # named billing.py; a bare `import billing` caches whichever one loads
    # first in sys.modules and silently hands a second service the wrong
    # module if both get imported into the same process (as happens in
    # this repo's shared test suite).
    import importlib.util
    import sys

    def _load_sibling_module(name: str):
        # Register under the unique name in sys.modules BEFORE executing, so
        # a sibling that imports the same module by this name (audits.py ->
        # browser_pool) gets this exact instance instead of loading a second
        # copy with its own thread-local browser state.
        unique_name = f"wcag_audit_engine_{name}"
        cached = sys.modules.get(unique_name)
        if cached is not None:
            return cached
        module_path = Path(__file__).resolve().parent / f"{name}.py"
        spec = importlib.util.spec_from_file_location(unique_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
        return module

    browser_pool = _load_sibling_module("browser_pool")  # type: ignore
    audits = _load_sibling_module("audits")  # type: ignore
    billing = _load_sibling_module("billing")  # type: ignore
    mpp_payments = _load_sibling_module("mpp_payments")  # type: ignore
    x402_payments = _load_sibling_module("x402_payments")  # type: ignore
    ard = _load_sibling_module("ard")  # type: ignore

# The worker network: additional machine-payable capabilities that run BESIDE
# the audits. Kept in its own import block so the audit imports above are
# untouched, and wrapped so that a fault anywhere in it can never stop the
# audits from serving -- the audits are the working business, and a new
# capability failing to load must cost us the capability, not the revenue.
try:
    try:
        from . import workers  # type: ignore
    except ImportError:
        # Same by-file-path fallback the siblings use, adapted for a package:
        # a package needs its own submodule_search_locations so that the
        # relative imports inside it resolve.
        import importlib.util
        import sys

        _workers_dir = Path(__file__).resolve().parent / "workers"
        _workers_spec = importlib.util.spec_from_file_location(
            "wcag_audit_engine_workers",
            _workers_dir / "__init__.py",
            submodule_search_locations=[str(_workers_dir)],
        )
        workers = importlib.util.module_from_spec(_workers_spec)  # type: ignore
        sys.modules["wcag_audit_engine_workers"] = workers
        _workers_spec.loader.exec_module(workers)
except Exception as _workers_import_error:  # pragma: no cover - defensive
    workers = None  # type: ignore
    logging.getLogger("hubvibe").warning(
        "worker network unavailable, audits unaffected: %s", _workers_import_error)

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://hubvibe-io.com"
)

# One version number for this service, quoted by everything that publishes
# one: the OpenAPI spec, the MCP `initialize` handshake's serverInfo, and the
# static /mcp.json. Three literals had already drifted to two values -- the
# registry entry and the manifest said 1.1.2 while every MCP client that
# completed a handshake was told 1.1.0. A client caching capabilities per
# version, or a crawler reconciling the registry against the live node, is
# reading a version that names the wrong build. Kept in step with
# server.json (the official registry's copy) by a test, since that file is
# outside the container's build context and cannot be read at runtime.
SERVICE_VERSION = "1.4.1"

# The revenue counter in the log -- "x402 SETTLED ..." -- is an INFO line.
# Python's root logger defaults to WARNING and uvicorn configures only its
# own loggers, so in the shipped container every INFO line this app wrote
# was dropped: the 2026-09-06 rehearsal paid the image three times and
# `docker logs` showed zero SETTLED lines, while the runbook told the owner
# to grep for them. Configure the root once; a host that already did wins.
_root_logger = logging.getLogger()
if not _root_logger.handlers:
    logging.basicConfig(format="%(levelname)s:%(name)s:%(message)s")
_configured_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
if _root_logger.level == logging.NOTSET or _root_logger.level > _configured_level:
    _root_logger.setLevel(_configured_level)

# Each in-flight audit holds a Chromium browser (see browser_pool), so the
# ceiling on concurrent audits is really a memory ceiling, not a CPU one.
# FastAPI runs these sync routes in anyio's threadpool, which defaults to 40
# threads -- 40 simultaneous Chromium instances would OOM any reasonably
# sized container, so cap it explicitly and size the container to match
# (see README: --memory / --cpu / --concurrency should agree with this).
MAX_CONCURRENT_AUDITS = int(os.environ.get("MAX_CONCURRENT_AUDITS", "4"))


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = MAX_CONCURRENT_AUDITS
        # Off the loop: Playwright's sync driver raises inside a running one.
        await anyio.to_thread.run_sync(_resolve_browser_path)
    except Exception:
        # Not fatal: worst case we run on anyio's default thread count.
        pass
    yield


# The one name every discovery surface uses -- openapi.json, agent.json,
# ard.json and (by hand, in the static files) mcp.json and the registry entry.
# Crawlers scored this node as a five-tool audit service while it sold 37
# more routes, because each surface carried its own audit-era title.
SERVICE_TITLE = "HubVibe: 38 Machine-Payable Dev Utilities and WCAG Audits"

app = FastAPI(
    lifespan=_lifespan,
    title=SERVICE_TITLE,
    version=SERVICE_VERSION,
    description=(
        "38 machine-payable dev utilities under /work -- LLM inference, web "
        "search and page extraction, Base chain reads, market and "
        "prediction-market data, BigQuery analysis and forecasting, a "
        "deterministic regression and probability engine, "
        "image/speech/video generation, sandboxed Python, maps, and cited "
        "research, verification and company briefs that compose several of "
        "them in one call -- plus five deterministic site audits: "
        "accessibility (axe-core), SEO, security headers, performance, and "
        "the $0.15 bundle, at $0.05 per single audit. Every /work route "
        "declares its request schema and a typed 200 response schema with an "
        "example; every delivered job has a receipt at /work/receipts/{id}.\n\n"
        "Built for agent-to-agent use: every paid route answers an "
        "unauthenticated request with HTTP 402 carrying a machine-readable "
        "payment challenge, so a paying agent can discover the price and "
        "settle without a human in the loop. The challenge names the rails "
        "this deployment can actually settle; see /.well-known/agent.json."
        "\n\n"
        "Every result is a rule-based check against the actual page. Nothing "
        "here is an LLM judging quality, and a check that could not run is "
        "reported as an error, never as a passing result.\n\n"
        "Discovery: /.well-known/agent.json, /.well-known/ard.json, "
        "/llms.txt, /mcp.json, /openapi.json"
    ),
    servers=[{"url": PUBLIC_BASE_URL, "description": "Production"}],
    openapi_tags=[
        {"name": "audit", "description": "Paid, machine-payable audit routes."},
        {"name": "discovery", "description": "Manifests agents use to find and price these tools."},
        {"name": "billing", "description": "One-off report purchase and retrieval. Audits themselves are paid per call."},
    ],
)

# Agents call this from browsers, edge workers, and other origins. There are
# no cookies or sessions here -- authentication is an explicit per-request
# header -- so a wildcard origin grants no ambient authority.
#
# Every header a paying client has to READ must be listed in expose_headers,
# or a browser-resident caller literally cannot see it: the browser strips
# unlisted response headers from cross-origin responses before script ever
# sees them. That is every rail's challenge and receipt --
#   WWW-Authenticate      the MPP challenge on a 402
#   PAYMENT-REQUIRED      the x402 v2 challenge on a 402 (a v2 client reads
#                         this FIRST; without it a browser client is silently
#                         downgraded to the v1 body, or sees no x402 at all)
#   PAYMENT-RESPONSE /    the x402 settlement receipt on the paid 200 -- the
#   X-PAYMENT-RESPONSE    transaction hash the payer reconciles against
#   Retry-After           when to come back after a 429, so a browser agent
#                         backs off instead of giving up on the node.
# Request headers (X-PAYMENT, PAYMENT-SIGNATURE, Authorization, X-API-Key) are
# covered by allow_headers=["*"].
CORS_EXPOSED_HEADERS = [
    "WWW-Authenticate",
    "Cache-Control",
    "PAYMENT-REQUIRED",
    "PAYMENT-RESPONSE",
    "X-PAYMENT-RESPONSE",
    "Retry-After",
]

# The largest request body this node will read, in bytes. The biggest
# legitimate body is an `html` audit at MAX_HTML_BYTES (2 MiB) plus JSON
# escaping; anything past this is refused before it is buffered. Without a
# cap, `Body(...)` read and parsed whatever arrived: a 300 MB unpaid POST to
# /mcp took the worker to ~1 GB RSS before the html-size gate ever ran, and
# three of them in flight exceed the container's 3 GB limit -- the box
# OOM-kills the node, every paid audit in flight dies unbilled, and the
# attacker paid nothing but bandwidth. Caddy enforces the same cap in front
# (deploy/vps/Caddyfile); this one holds on any host, Cloud Run included.
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(4 * 1024 * 1024)))


def _body_too_large_content(path: str, size: int, max_bytes: int) -> dict:
    detail = (
        f"request body is {size} bytes; this node reads at most {max_bytes}. "
        "Nothing was charged for this request."
    )
    if path == "/mcp":
        # JSON-RPC callers get a JSON-RPC error, not a REST shape.
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": detail}}
    return {"status": "error", "detail": detail, "billed": False, "max_request_bytes": max_bytes}


class _BodyTooLarge(HTTPException):
    """Raised from the counting `receive` when a chunked body passes the cap.

    An HTTPException on purpose: FastAPI's route handler turns any OTHER
    exception raised while it reads the body into a generic 400 before an
    exception handler can see it, and re-raises HTTPExceptions untouched --
    so this one reaches the handler below and answers as a 413.
    """

    def __init__(self, size: int, max_bytes: int):
        super().__init__(status_code=413, detail="request body too large")
        self.size = size
        self.max_bytes = max_bytes


@app.exception_handler(_BodyTooLarge)
async def _body_too_large(request: Request, exc: _BodyTooLarge):
    return JSONResponse(
        status_code=413, content=_body_too_large_content(request.url.path, exc.size, exc.max_bytes)
    )


class _RequestBodyLimit:
    """Pure-ASGI middleware: refuse a request body over `max_bytes` with 413.

    Two checks. A declared Content-Length over the cap is refused before a
    byte of body is read. A body that arrives without one (chunked) is
    counted as it streams, and cut off the moment it passes the cap -- a
    missing header must not be the way around the limit.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > self.max_bytes:
            await self._refuse(scope, send, declared)
            return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise _BodyTooLarge(received, self.max_bytes)
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge as exc:
            if started:
                raise
            await self._refuse(scope, send, exc.size)

    async def _refuse(self, scope, send, size: int):
        response = JSONResponse(
            status_code=413,
            content=_body_too_large_content(scope.get("path") or "", size, self.max_bytes),
        )
        await response(scope, _empty_receive, send)


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


# The rate a caller is quoted when the route cannot be resolved -- a
# signature default or a price string that would not parse. It is the single
# audit rate, read from the catalog below rather than written twice, because
# a stale literal here quotes one price while the route charges another.
_DEFAULT_PRICE_USD = 0.05


def _price_of(path: str) -> float:
    """The catalog price for a route this module serves. Raises on an unknown
    path rather than defaulting: a route that cannot say what it charges must
    not quietly bill whatever the cheapest one costs."""
    price = _paid_route_price(path)
    if price is None:
        raise KeyError(f"{path} is not a priced route in _CATALOG")
    return price


def _paid_route_price(path: Optional[str]) -> Optional[float]:
    """This route's price, or None when the path is not a paid route.

    Read from `_CATALOG` -- the one row per sellable route -- rather than
    from a second table beside it. A hardcoded copy here would let the price
    a probe is quoted drift from the price the handler charges, which is the
    same class of fault as advertising a rail that cannot settle: the node
    would be telling an agent one number and billing another. Resolved at
    call time because _CATALOG is defined further down this module; the
    middleware only ever runs per-request, long after import.
    """
    if not path:
        return None
    resolved = _CATALOG_ALIASES.get(path, path)
    for entry in _CATALOG:
        if entry["path"] == resolved:
            return entry["price_usd"]
    # The worker network prices its own routes from its own catalog, and
    # only after the audit catalog, so it can never shadow or alter what an
    # audit route charges.
    if workers is not None:
        worker_price = workers.catalog.price_of(resolved)
        if worker_price is not None:
            return worker_price
    return None


_CREDENTIAL_HEADERS = ("x-api-key", "x-payment", "payment-signature", "authorization")


def _carries_credential(request: Request) -> bool:
    return any(request.headers.get(name) for name in _CREDENTIAL_HEADERS)


class _PriceUnpaidProbes:
    """A paid route answers a request that carries no credential with its
    price, whatever else is wrong with the request.

    The indexers and verifiers that decide whether this node is listed
    (uvd-bazaar-health, PayAI-Uptime-Monitor, x402lens-indexer,
    x402-directory-verifier, allow402-quote, hermes) cannot know the input
    schema before they have seen the challenge, so they GET, HEAD, or POST an
    empty body. Until 2026-09-13 method routing and body validation ran
    before the payment gate, so those probes got 405, 422 or 400: in one day
    the box's ledger showed ~240 crawler POSTs and ~500 GET/HEADs bouncing
    that way while 25 requests in total saw a 402. A crawler that never sees
    the price cannot list it, and a listing is where every paying agent
    comes from.

    Validation still runs first for a request that DOES carry a credential:
    a payer's malformed body has to be refused before its signature reaches
    the facilitator, or the nonce is burned and the corrected retry is
    refused as a replay (see _reject_missing_input). With no credential there
    is no nonce to protect, so the challenge can go first.

    The 402 is the very one the auth gate issues, rate limiter included;
    a non-POST probe also learns the method. OPTIONS passes through to CORS.
    Pure ASGI, like _RequestBodyLimit, and registered before it so the cap
    wraps this: a declared body over the cap is a 413, credential or not. A
    Starlette BaseHTTPMiddleware here would wrap the body reader and turn the
    cap's _BodyTooLarge into a generic 400 for a credentialed caller.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        method = scope.get("method")
        price = _paid_route_price(scope.get("path")) if scope["type"] == "http" else None
        if price is None or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        request = Request(scope, _empty_receive)  # headers only; the body is never read here
        if _carries_credential(request):
            await self.app(scope, receive, send)
            return
        _, challenge = _authorize_and_rate_limit(None, None, None, request, price_usd=price)
        if challenge is None:  # pragma: no cover -- nothing authenticates without a credential
            await self.app(scope, receive, send)
            return
        if method != "POST":
            challenge.headers["allow"] = "POST"
        if method == "HEAD":
            # Status and headers only: a body on a HEAD response is a protocol
            # error at the HTTP layer, and h11 refuses to send it.
            challenge = Response(
                status_code=challenge.status_code,
                headers={k: v for k, v in challenge.headers.items() if k.lower() != "content-length"},
            )
        await challenge(scope, _empty_receive, send)


app.add_middleware(_PriceUnpaidProbes)


# Added BEFORE CORS so CORS wraps it: a browser caller can read the 413.
app.add_middleware(_RequestBodyLimit, max_bytes=MAX_REQUEST_BYTES)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=CORS_EXPOSED_HEADERS,
    max_age=86400,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_axe = Axe()

# The manifest advertises "WCAG 2.1 AA" -- constrain axe-core's rule set to
# match, so a "pass" actually means what it claims instead of whatever
# axe-core's full default rule set happens to cover.
AXE_OPTIONS = {
    "resultTypes": ["violations"],
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
}

# An internal/testing key that bypasses Stripe billing entirely. Leave unset
# in production once real customers are onboarded through /billing/checkout.
API_KEY = os.environ.get("AUDIT_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional; enables remediation notes only

# Ceiling per API key (or per IP for x402/MPP callers, who have no key).
# This exists to stop a runaway client from exhausting the browser pool --
# it is NOT a monetisation lever. Every request past the paywall has already
# been paid for, so throttling a paying agent is refusing revenue: keep this
# well above any legitimate caller's burst rate. 600/min = 10 req/s per key.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "600"))


class _SlidingWindowLimiter:
    """Sliding-window rate limiter with bounded memory.

    The bounded part is the point. A plain dict-of-deques keyed by caller IP
    never removes the entry for an IP that made one request and went away,
    so the table grows for the lifetime of the process -- at the request
    volume this service is built for that is an eventual OOM, not a
    theoretical concern. Expired windows are swept periodically and the
    table is hard-capped as a backstop.

    Still best-effort across instances: Cloud Run runs many containers that
    don't share this state, so treat it as per-instance overload protection
    and put Cloud Armor in front for real abuse policy.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 50_000,
                 sweep_interval: float = 60.0):
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._sweep_interval = sweep_interval
        self._log: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def check(self, key: str) -> bool:
        """Record a hit and return True if allowed, False if over the limit.

        Returns a bool rather than raising so the caller decides the response
        shape -- agents need a machine-readable 429 with Retry-After, not an
        opaque error.
        """
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            window = self._log.get(key)
            if window is None:
                window = deque()
                self._log[key] = window
            cutoff = now - self._window
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self._limit:
                return False
            window.append(now)
            return True

    def _sweep_locked(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval and len(self._log) <= self._max_keys:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for key in [k for k, w in self._log.items() if not w or w[-1] <= cutoff]:
            del self._log[key]
        if len(self._log) > self._max_keys:
            # Pathological key cardinality (e.g. a spoofed-source flood).
            # Drop the least-recently-seen half; discarding limiter state can
            # only ever forgive a request, never wrongly deny one.
            oldest = sorted(self._log, key=lambda k: self._log[k][-1])[: len(self._log) // 2]
            for key in oldest:
                del self._log[key]


_audit_limiter = _SlidingWindowLimiter(RATE_LIMIT_PER_MINUTE, 60.0)

# How many proxy hops sit between the public internet and this container, i.e.
# which X-Forwarded-For entry is the address the platform itself appended.
# Cloud Run's front end appends the connecting client's address as the LAST
# entry (1). A deployment that later puts an external HTTPS load balancer in
# front gains one more trusted hop and sets 2. Anything a client supplies in
# the header sits BEFORE the platform's entries and is never read.
RATE_LIMIT_PROXY_DEPTH = max(1, int(os.environ.get("RATE_LIMIT_PROXY_DEPTH", "1")))


def _client_ip(request: Request) -> str:
    """The address the rate limiter keys an unkeyed caller on.

    `request.client.host` is the TCP peer, and on Cloud Run the TCP peer is
    the platform's own front-end proxy -- the same address for every caller on
    earth. Keyed on that, every x402 and MPP payer, and every agent reading an
    unpaid 402, shared ONE bucket of RATE_LIMIT_PER_MINUTE per instance. Past
    ten unpaid reads a second, per instance, every paying agent got a 429:
    refused revenue, keyed on nothing to do with the agent. The limiter exists
    to stop a single runaway client; it was stopping everyone.

    The address Cloud Run vouches for is the one IT appended to
    X-Forwarded-For -- the last entry, counting back RATE_LIMIT_PROXY_DEPTH
    hops. A client can prepend whatever it likes to that header; it cannot
    append after the platform. Without the header (local test, direct
    uvicorn) the TCP peer is the client and is used as before.
    """
    forwarded = request.headers.get("x-forwarded-for") if request else None
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if len(hops) >= RATE_LIMIT_PROXY_DEPTH:
            return hops[-RATE_LIMIT_PROXY_DEPTH]
        if hops:
            return hops[0]
    if request is not None and request.client:
        return request.client.host
    return "unknown"


# Largest raw-HTML body an audit accepts. A page is tens of kilobytes; two
# megabytes is a generous ceiling for anything a browser would render. Without
# a cap the field was unbounded, and one caller posting a few hundred MB of
# "html" would take the instance down for everyone queued behind it -- for
# free, since a 402 costs nothing to trigger.
MAX_HTML_BYTES = int(os.environ.get("MAX_HTML_BYTES", str(2 * 1024 * 1024)))


class AuditRequest(BaseModel):
    html: Optional[str] = Field(
        None, description="Raw HTML source to audit", max_length=MAX_HTML_BYTES
    )
    url: Optional[str] = Field(None, description="Live URL to audit instead of raw HTML")


class UrlAuditRequest(BaseModel):
    """For audit routes that need a live, fetchable URL -- security and
    performance checks inspect real HTTP responses and real network
    behavior, so raw HTML alone (no server to talk to) isn't enough."""

    url: str


class CheckoutRequest(BaseModel):
    email: str
    plan: Optional[str] = None


class ReportCheckoutRequest(BaseModel):
    email: str
    url: str


class AuthContext:
    __slots__ = (
        "stripe_billable",
        "customer_id",
        "payment_method",
        "pending_payment",
        # A prepaid key minted by the call that paid for it. The agent has no
        # account and no second channel, so the response IS the delivery
        # mechanism -- drop it and the money is taken with nothing handed back.
        "issued_key",
        # What authentication already took, so a failed audit can hand it
        # back (_unbill_failed_audit): the prepaid key debited for this call
        # and how many cents, and the MPP credential marked as spent.
        "prepaid_key",
        "prepaid_cents",
        "mpp_credential",
        # Set when a top-up was CHARGED but its key could not be minted, so the
        # response can tell the payer that credit is owed instead of going quiet.
        "credit_owed",
        # The host and route the x402 challenge was issued for, so a
        # settlement the facilitator refuses is answered with the same payable
        # 402 the caller started from (see _settlement_refused).
        "challenge_host",
        "challenge_path",
    )

    def __init__(
        self,
        stripe_billable: bool,
        customer_id: Optional[str] = None,
        payment_method: str = "api_key",
        pending_payment=None,
        issued_key: Optional[str] = None,
        credit_owed: Optional[str] = None,
        prepaid_key: Optional[str] = None,
        prepaid_cents: int = 0,
        mpp_credential: Optional[str] = None,
        challenge_host: Optional[str] = None,
        challenge_path: Optional[str] = None,
    ):
        self.stripe_billable = stripe_billable
        self.customer_id = customer_id
        self.payment_method = payment_method
        # An x402 payment that is verified but deliberately not yet settled --
        # see _bill. None for every other payment method.
        self.pending_payment = pending_payment
        self.issued_key = issued_key
        self.credit_owed = credit_owed
        self.prepaid_key = prepaid_key
        self.prepaid_cents = prepaid_cents
        self.mpp_credential = mpp_credential
        self.challenge_host = challenge_host
        self.challenge_path = challenge_path


def _worker_input_example(worker) -> dict:
    """A plausible body for a worker, built from its own JSON Schema.

    The Bazaar record carries an example an agent may generate its first
    request from, so the example has to satisfy the route's required fields --
    an example the route would 400 on is worse than none.
    """
    if not worker.input_schema.get("required"):
        # Nothing is required at the top level because the schema chooses
        # between alternatives (oneOf); the catalog's own example is the
        # body that satisfies it, and {} would not.
        return dict(workers.catalog.example_for(worker))
    example = {}
    properties = worker.input_schema.get("properties") or {}
    for field in worker.input_schema.get("required") or []:
        spec = properties.get(field) or {}
        kind = spec.get("type")
        if kind == "array":
            example[field] = ["example"]
        elif kind == "integer":
            example[field] = 10
        elif kind == "number":
            example[field] = 1.0
        elif field in ("url", "final_url"):
            example[field] = "https://example.com"
        else:
            example[field] = "example"
    return example


def _bazaar_extension_for_path(path: Optional[str]) -> dict:
    """Bazaar discovery data for the route this 402 is answering for.

    Reuses the same JSON Schemas the MCP tools advertise rather than writing
    a second copy: a discovery index that describes a different input shape
    than the route accepts sends agents to a call that 400s.

    Returns {} for an unknown path or when x402 is not configured, so this
    can be spliced into any 402 unconditionally.
    """
    if not path:
        return {}
    # /audit is an alias of /audit/wcag and is deliberately absent from the
    # catalog (one row per sellable route, so prices cannot be duplicated).
    # Without this it was also absent from Bazaar discovery, which made the
    # shortest and most guessable paid path on the service the one path an
    # agent could not find by capability.
    path = _CATALOG_ALIASES.get(path, path)
    entry = next((e for e in _CATALOG if e["path"] == path), None)
    if entry is None:
        # A worker route. It needs Bazaar discovery data for exactly the same
        # reason an audit does: a paid route with none is payable but
        # invisible to capability search, which is indistinguishable from
        # nobody wanting to buy it.
        if workers is not None:
            worker = workers.catalog.get(path)
            if worker is not None:
                # The output half used to be the placeholder {"status": "ok"}
                # on all 37 routes -- the one field the Bazaar ranks on
                # (completeness of the output schema) and the one an agent
                # reads to decide whether the result fits its pipeline.
                return x402_payments.bazaar_extension_for_body(
                    input_example=_worker_input_example(worker),
                    input_schema=worker.input_schema,
                    output_example=workers.catalog.response_example(worker),
                    output_schema=workers.catalog.response_schema(worker),
                )
        return {}
    schema = (
        _MCP_URL_SCHEMA if entry["input"] is _URL_INPUT_SCHEMA else _MCP_HTML_OR_URL_SCHEMA
    )
    return x402_payments.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema=schema,
        output_example={"pass": True},
    )


def _route_description(path: Optional[str]) -> str:
    """One line naming what this specific route sells.

    Goes into the payment challenge, where it is the only human-readable clue
    an agent's operator gets about what a signature is about to buy. Taken
    from the catalog so it cannot describe something the route does not do.
    """
    if path:
        resolved = _CATALOG_ALIASES.get(path, path)
        entry = next((e for e in _CATALOG if e["path"] == resolved), None)
        if entry is not None:
            return entry["description"]
        if workers is not None:
            worker_description = workers.catalog.description_of(resolved)
            if worker_description:
                return worker_description
    return "HubVibe site audit"


def _payment_required_response(
    host: Optional[str] = None,
    price_usd: float = _DEFAULT_PRICE_USD,
    path: Optional[str] = None,
    error: Optional[str] = None,
    error_detail: Optional[str] = None,
    retry_after: Optional[int] = None,
) -> JSONResponse:
    """The 402 shape a caller needs to pay via x402, MPP, or get a Stripe
    API key -- returned whenever none of those is attached and valid (or
    a subscriber is over their included monthly quota). This is the sole
    "access denied" outcome for a paid audit route; there is no path that
    falls through to granting access.

    Carries a WWW-Authenticate: Payment header per configured MPP method
    (spec-required for MPP conformance -- see mpp_payments.py), bound to the
    request's own Host header as the MPP realm and priced at this specific
    route's rate, plus the x402-style JSON body for callers that read
    price/payTo from the body instead. Cache-Control: no-store is required
    by the MPP core spec on every 402.

    `error` / `error_detail` / `retry_after` are set when this 402 answers a
    payment that was TRIED and refused. The x402 reference server re-issues
    its 402 with the facilitator's `invalid_reason` as `error`, and the
    official client does not retry a second 402 -- so a bare re-challenge
    leaves the agent nothing to act on. An empty wallet, a signature for the
    wrong route and a facilitator outage used to produce byte-identical
    responses; the outage now also carries Retry-After, because the payer
    did nothing wrong.
    """
    price = f"${price_usd:.2f}"
    resource_url = f"{PUBLIC_BASE_URL}{path}" if path else PUBLIC_BASE_URL

    # `accepts` is not ours to design. A conforming x402 client hands this
    # whole body to the library, which validates EVERY entry in `accepts`
    # against PaymentRequirementsV1 and raises on the first one that does not
    # fit -- before it produces any signature. So `accepts` carries x402
    # entries and nothing else. The other rails live in `other_rails` below;
    # they lost nothing but the array they were in, and MPP's real channel
    # was always the WWW-Authenticate headers further down.
    #
    # This array previously held our own invented shape plus the MPP and
    # API-key rails, which made the paid path unpayable twice over. See
    # x402_payments.accepts_entry for what that cost.
    accepts = []
    x402_entry = x402_payments.accepts_entry(
        price=price, resource_url=resource_url, description=_route_description(path)
    )
    if x402_entry:
        accepts.append(x402_entry)

    other_rails = list(mpp_payments.accepts_entries(price_usd=price_usd))
    # The API-key rail belongs in the machine-readable rail list too. It was
    # listed in /.well-known/agent.json's payment.methods and described in
    # `alternative` below, but nowhere an agent could iterate. A CI pipeline
    # holding a pre-funded key had no way to discover from the challenge alone
    # that its key is spendable here; it had to parse prose. Gated on
    # billing.is_configured() like every other rail: with no Stripe
    # configured, no key can be issued or metered, so advertising it would be
    # advertising a rail that cannot settle.
    # The SPT top-up. Advertised only when a per-call SPT charge is
    # impossible -- Stripe's 0.50 USD floor against a cents-priced route -- because
    # that is exactly when buying a block is the only way this rail can
    # settle at all. An agent iterating `other_rails` sees a fiat option it
    # can actually use, instead of a rail that is simply missing.
    if mpp_payments.topup_available() and not mpp_payments.stripe_available_for(
        round(price_usd * 100)
    ):
        other_rails.append(
            {
                "protocol": "mpp",
                "method": "stripe",
                "intent": "topup",
                "asset": "usd",
                "amount_minor_units": str(mpp_payments.topup_cents()),
                "send_via_header": "Authorization: Payment ...",
                "challenge_in": "WWW-Authenticate",
                "detail": (
                    "Stripe requires a minimum charge above this call's price, "
                    "so this rail sells prepaid credit rather than one call. "
                    "Pay it and the response returns an api_key holding the "
                    "balance, minus this call. No account, no checkout."
                ),
            }
        )

    # The subscription-backed key rail is listed only while a plan is for
    # sale. With the human tiers retired (2026-09-06) that is never; the
    # prepaid key the MPP top-up sells is advertised by that rail instead.
    if billing.is_configured() and billing.human_plans_live():
        other_rails.append(
            {
                "protocol": "api_key",
                "method": "stripe_api_key",
                "send_via_header": "X-API-Key",
                "price_usd": price_usd,
                "detail": (
                    "Pre-funded key issued against a Stripe subscription; calls "
                    "are metered against it. Usable unattended -- no browser "
                    "step at call time."
                ),
                "get_one": f"{PUBLIC_BASE_URL}/billing/checkout",
            }
        )

    # The key rail's human doorway. X-API-Key is read on every deployment
    # (the internal key, prepaid keys), but a key can only be BOUGHT where
    # Stripe billing is configured -- /billing/checkout answers 501
    # everywhere else. Naming that URL on an x402-only node sent an agent's
    # operator to a dead end and read as "the service is broken".
    # ...and when NOTHING is live, that doorway is a dead end too: pointing a
    # caller at `other_rails` for a key while `other_rails` is empty is the
    # same wrong turn one level down. Say the true thing instead.
    if accepts or other_rails:
        alternative = {
            "header": "X-API-Key",
            "detail": (
                "A prepaid key: bought with the MPP top-up rail in `other_rails` "
                "where that rail is live, and spent per call at the same rates. "
                "There are no subscriptions; pay per call with a rail in `accepts`."
            ),
        }
    else:
        alternative = {
            "header": "X-API-Key",
            "detail": (
                "No payment rail is live on this deployment right now -- `accepts` "
                "and `other_rails` are both empty, so this call cannot be bought "
                "and no retry will change that. A prepaid key issued earlier still "
                "spends. This is a configuration state on our side, not a "
                "rejection of your request."
            ),
        }

    body = {
        "error": error or "payment_required",
        "price_usd": price_usd,
        "price": price,
        # x402Version marks this body as a v1 challenge. It is what makes a
        # client parse `accepts` at all -- the library reads the body only
        # when it says 1, and otherwise looks for the PAYMENT-REQUIRED header
        # (which is also sent, below, for v2 clients).
        "x402Version": 1,
        "accepts": accepts,
        "other_rails": other_rails,
        "alternative": alternative,
        "docs": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
    }
    if error:
        body["error_detail"] = error_detail or error
        body["billed"] = False
    # Bazaar discovery. Facilitators catalog x402 resources by reading this
    # off their 402s, and agents shop that index by capability -- without it
    # this endpoint is findable only by someone who already has the URL.
    bazaar = _bazaar_extension_for_path(path)
    if bazaar:
        body["extensions"] = bazaar
    # Workers priced above the x402 client libraries' default $1 per-payment
    # cap: a stock agent refuses these locally and reads THIS body to learn
    # why, so it says how to lift the cap. Every 402 for a /work route is
    # built here (the probe path answers before the router runs), which is
    # why the note lives here and nowhere else. The price is unchanged.
    worker = workers.catalog.get(path) if workers is not None and path else None
    note = workers.catalog.buyer_note(worker) if worker is not None else None
    if note:
        body["buyer_note"] = note

    response = JSONResponse(status_code=402, content=body)

    # The v2 challenge, which does not live in the body at all. A client looks
    # for this header FIRST and only falls back to parsing the body as v1, so
    # sending both serves every conforming client from one response. It also
    # carries `extensions` in the slot v2 actually defines for them, and names
    # the service for the Bazaar index -- neither of which the v1 body has
    # anywhere to put.
    for name, value in x402_payments.payment_required_header(
        price=price,
        resource_url=resource_url,
        description=_route_description(path),
        extensions=bazaar or None,
        error=error,
    ).items():
        response.headers[name] = value
    response.headers["Cache-Control"] = "no-store"
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    for header_value in mpp_payments.www_authenticate_headers(realm=host, price_usd=price_usd):
        response.headers.append("WWW-Authenticate", header_value)
    return response


# How long a payer should wait when the FACILITATOR, not the payer, was the
# reason a payment could not be checked. Short: outages that matter are
# minutes long, and an agent that waits an hour for a 30-second blip is lost.
_FACILITATOR_RETRY_AFTER_SECONDS = 30


def _authenticate(
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
    host: Optional[str] = None,
    price_usd: float = _DEFAULT_PRICE_USD,
    path: Optional[str] = None,
    client_ip: Optional[str] = None,
):
    """Returns an AuthContext on success, or a 402 JSONResponse on failure
    (a 429 when a key that did not authenticate came from an address that
    is over its limit -- see below).

    Three independent paths, checked cheapest-first, any one sufficient:
    1. X-API-Key -- internal test key, or a real Stripe-issued key that
       still has quota left in its included monthly allowance (once a
       subscriber exceeds SAAS_MONTHLY_QUOTA scans this calendar month,
       the bare key stops being sufficient on its own and falls through
       to x402/MPP below, same as the landing page describes).
    2. X-PAYMENT -- x402 (crypto only), verified against a facilitator,
       for exactly `price_usd`.
    3. Authorization: Payment ... -- MPP (Stripe SPT for fiat, or Tempo for
       crypto), verified directly against Stripe / the Tempo network, for
       exactly `price_usd`. `host` (the request's own Host header) must
       match the realm the challenge was originally issued with.
    """
    if x_api_key:
        # Compared as bytes: compare_digest on str raises TypeError for a
        # non-ASCII character, and a header can carry one (Starlette decodes
        # header bytes as latin-1). A stray byte in a key must be a 402, not
        # a 500.
        if API_KEY and secrets.compare_digest(
            x_api_key.encode("utf-8"), API_KEY.encode("utf-8")
        ):
            # Internal/testing key: unlimited, unmetered, never billed,
            # never quota-limited.
            return AuthContext(stripe_billable=False, payment_method="internal")
        # Spending a prepaid key is gated on the key store answering, NOT on
        # billing.is_configured(). That function asks whether Stripe could sell
        # a SUBSCRIPTION -- it wants a webhook secret and a sellable price ID --
        # and the MPP top-up that mints prepaid keys needs neither. A box
        # configured for the top-up and nothing else therefore sold a $0.50 key
        # and then refused every call made with it. lookup_key already returns
        # None when the store cannot answer, so it is safe to ask first.
        record = billing.lookup_key(x_api_key)
        if record is not None:
            # A prepaid key carries its own money and has no Stripe Customer
            # behind it, so it is spent rather than metered or quota-checked.
            if record.get("prepaid_balance_cents") is not None:
                call_cents = round(price_usd * 100)
                if billing.spend_prepaid(x_api_key, call_cents):
                    return AuthContext(
                        stripe_billable=False,
                        payment_method="prepaid",
                        prepaid_key=x_api_key,
                        prepaid_cents=call_cents,
                    )
                # Out of credit: fall through to the 402, which offers a
                # top-up. Refusing loudly beats serving on an empty balance.
            elif billing.is_configured() and billing.check_and_increment_quota(
                record["customer_id"], plan=record.get("plan")
            ):
                return AuthContext(
                    stripe_billable=True,
                    customer_id=record["customer_id"],
                    payment_method="stripe",
                )

    if x_api_key and client_ip:
        # The key did not authenticate, so it earns no rate-limit bucket of
        # its own: the limiter keys on the presented key, and a caller minting
        # a fresh bogus key per request would otherwise never meet it at all.
        # Charge the address the request came from instead -- before any
        # payment instrument below is read, so an over-limit caller costs
        # this node no facilitator call.
        if not _audit_limiter.check(client_ip):
            return _rate_limited_response()

    refusal = None
    if x_payment:
        # Verify only -- do NOT settle here. Settlement happens in _bill, after
        # an audit has actually produced a result, so a caller whose audit
        # fails to run is never charged for nothing.
        #
        # resource_url: the URL this route advertised in its 402. A v1 payer's
        # requirements are rebuilt from the same values it was challenged
        # with, and the resource is one of them.
        pending = x402_payments.verify_only_sync(
            x_payment,
            price=f"${price_usd:.2f}",
            resource_url=f"{PUBLIC_BASE_URL}{path}" if path else PUBLIC_BASE_URL,
        )
        if pending is not None:
            return AuthContext(
                stripe_billable=False, payment_method="x402", pending_payment=pending,
                challenge_host=host, challenge_path=path,
            )
        refusal = x402_payments.last_rejection()

    if authorization and authorization.startswith("Payment "):
        credential = authorization[len("Payment "):].strip()
        # Top-up first: it is a different intent with a different meaning, and
        # letting it fall through to the per-call path would consume a $0.50
        # purchase as payment for one single audit.
        if credential:
            bought_cents = mpp_payments.settle_topup_sync(credential, realm=host)
            if bought_cents:
                # This call is served out of the credit just bought, so the
                # key is issued with the remainder. Charging for it separately
                # would mean paying twice for one request.
                call_cents = round(price_usd * 100)
                remaining = max(bought_cents - call_cents, 0)
                key = None
                mint_error = None
                if remaining:
                    # Top up the key the caller already holds, when they sent
                    # one. Minting a fresh key instead makes every refill a
                    # re-setup: a CI pipeline has to rotate the secret it
                    # stored, and whatever was left on the old key is stranded,
                    # because nothing else can ever spend it.
                    existing = billing.lookup_key(x_api_key) if x_api_key else None
                    if existing is not None and existing.get("prepaid_balance_cents") is not None:
                        if billing.refund_prepaid(x_api_key, remaining):
                            key = x_api_key
                    if key is None:
                        try:
                            key = billing.issue_prepaid_key(remaining)
                        except Exception as exc:
                            # Stripe has ALREADY taken the money: settle_topup_sync
                            # only returns cents after the PaymentIntent confirmed.
                            # It must not break the audit the caller paid for, but
                            # the payer is owed credit and this response is the
                            # only channel they have -- so it is said on the body
                            # and logged at ERROR for the operator to make good.
                            key = None
                            mint_error = (
                                "paid %d cents but the prepaid key could not be issued; "
                                "%d cents of credit is owed" % (bought_cents, remaining)
                            )
                            logging.getLogger(__name__).error(
                                "MPP top-up CHARGED %d cents and FAILED to issue the key "
                                "(%d cents owed to the payer): %s: %s",
                                bought_cents, remaining, type(exc).__name__, exc,
                            )
                return AuthContext(
                    stripe_billable=False,
                    payment_method="mpp-topup",
                    issued_key=key,
                    credit_owed=mint_error,
                    # If this first audit fails, the call it paid for goes
                    # back on the key, so the payer leaves holding everything
                    # it bought.
                    prepaid_key=key,
                    prepaid_cents=call_cents if key else 0,
                )
        if credential and mpp_payments.verify_and_settle_sync(credential, realm=host):
            # Already charged/settled (Stripe PaymentIntent or on-chain
            # Tempo transfer) inside verify_and_settle_sync -- nothing
            # further to bill. Note the credential itself carries the
            # price (embedded in its HMAC-bound challenge), so there's
            # nothing further to pass here beyond the realm check.
            return AuthContext(
                stripe_billable=False, payment_method="mpp", mpp_credential=credential
            )

    if refusal:
        reason, detail = refusal
        return _payment_required_response(
            host=host,
            price_usd=price_usd,
            path=path,
            error=reason,
            error_detail=detail,
            retry_after=(
                _FACILITATOR_RETRY_AFTER_SECONDS
                if x402_payments.rejection_is_transient(reason)
                else None
            ),
        )
    return _payment_required_response(host=host, price_usd=price_usd, path=path)


def _authorize_and_rate_limit(
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
    request: Request,
    price_usd: float,
):
    """Shared fail-closed auth + best-effort rate limiting for every paid
    audit route. Returns (AuthContext, None) on success, or (None,
    JSONResponse) when the caller should get that response immediately
    instead of the route continuing.
    """
    # Order matters, and it is load-bearing: _authenticate SETTLES REAL MONEY
    # for x402/MPP callers (verify_and_settle_sync moves funds on-chain or
    # confirms a Stripe PaymentIntent). Checking the rate limit after that
    # point meant an over-limit caller paid, then got a 429 -- money taken,
    # no audit delivered, no refund path. Anyone who is going to be rejected
    # must be rejected before their payment instrument is touched.
    #
    # x402/MPP payers have no API key to key the limiter on -- fall back to
    # the client's address (the one the platform vouches for, see
    # _client_ip). Either way this is per-instance overload protection, not
    # the billing boundary: that's Stripe usage records / on-chain settlement.
    rate_limit_key = x_api_key or _client_ip(request)
    if not _audit_limiter.check(rate_limit_key):
        return None, _rate_limited_response()

    # x402 v1 clients send X-PAYMENT; v2 clients send PAYMENT-SIGNATURE. The
    # challenge now offers both protocol versions, so both headers have to be
    # read -- accepting only X-PAYMENT while advertising v2 would hand a v2
    # client a challenge it can satisfy and then ignore the signature it sends
    # back, 402ing it forever. The decoder handles either payload shape.
    payment_header = x_payment or request.headers.get("PAYMENT-SIGNATURE")

    auth = _authenticate(
        x_api_key,
        payment_header,
        authorization,
        host=_mpp_realm(request),
        price_usd=price_usd,
        path=request.url.path,
        client_ip=_client_ip(request),
    )
    if isinstance(auth, JSONResponse):
        return None, auth

    return auth, None


def _attach_issued_key(result: dict, auth) -> None:
    """Hand back a prepaid key bought by this very call.

    The agent that paid has no account, no email and no second channel, so
    this response is the only place the key can be delivered. Dropping it
    would mean taking the money and returning nothing spendable -- the worst
    outcome available on a rail whose whole promise is that a machine can pay
    without a human.

    Named `api_key` because that is the header it goes in, and stated in
    prose beside it because an agent reading this once should not have to
    guess whether the value is a receipt or a credential.
    """
    owed = getattr(auth, "credit_owed", None)
    if owed:
        # The charge went through and the credit did not. Saying so is the
        # minimum: the payer is owed money and this response is the only
        # channel they have.
        result["billing_warning"] = owed
        result["billed"] = True

    key = getattr(auth, "issued_key", None)
    if not key:
        return
    result["api_key"] = key
    result["api_key_note"] = (
        "Prepaid credit from your top-up, minus this call. Send it as the "
        "X-API-Key header on subsequent requests until the balance runs out; "
        "there is no account and nothing to log in to."
    )


def _with_receipt(content, auth):
    """Return `content`, carrying the x402 settlement receipt headers when
    this call was paid per-call and settled.

    Routes return plain dicts, which FastAPI serialises with no headers of
    ours on them. A settled x402 payment has a receipt to deliver -- the
    facilitator's settle response, transaction hash included -- and the spec
    puts it in the PAYMENT-RESPONSE header of the 200. So a paid delivery
    becomes a JSONResponse with those headers; every other delivery is
    returned exactly as before.
    """
    pending = getattr(auth, "pending_payment", None)
    headers = x402_payments.receipt_headers(pending) if pending is not None else {}
    if not headers:
        return content
    return JSONResponse(content=content, headers=headers)


def _deliver(result: dict, auth):
    """The last line of every paid route: attach what the payer is owed
    besides the audit -- a prepaid key it just bought, the settlement
    receipt -- and return.

    Unless the facilitator REFUSED to settle: then the audit is withheld and
    the caller gets the payable 402 back with the reason. See
    _settlement_refused."""
    refused = _settlement_refused(auth)
    if refused is not None:
        return refused
    _attach_issued_key(result, auth)
    return _with_receipt(result, auth)


def _unbill_failed_audit(auth) -> None:
    """Undo what authentication took, for an audit that did not run.

    x402 needs nothing here: it is settled only in _bill, which the failure
    paths never reach. A prepaid debit taken at authentication goes back on
    the key -- including the call a top-up just paid for, so the key the
    payer receives holds everything it bought -- and an MPP credential is
    released, so the same receipt is accepted on the retry instead of being
    refused as already spent. Without this, "charged only for an audit that
    produced a result" was true for x402 and false for every other rail.
    """
    key = getattr(auth, "prepaid_key", None)
    cents = getattr(auth, "prepaid_cents", 0) or 0
    if key and cents:
        billing.refund_prepaid(key, cents)
    credential = getattr(auth, "mpp_credential", None)
    if credential:
        mpp_payments.release_credential(credential)


def _failed_audit_response(auth, detail: str) -> JSONResponse:
    """The 502 every paid route answers with when the audit could not run:
    nothing charged, and anything the payer is owed regardless -- the prepaid
    key a top-up just bought -- still delivered."""
    _unbill_failed_audit(auth)
    content = {
        "status": "error",
        "pass": None,
        "detail": f"{detail}. Nothing was charged for this request.",
        "billed": False,
    }
    _attach_issued_key(content, auth)
    return JSONResponse(status_code=502, content=content)


def _bill(auth, price_usd: float) -> Optional[str]:
    """Collect payment for an audit that actually produced a result.

    `price_usd` is this route's real rate, and it is passed through to the
    meter rather than converted into "units" here. Routes used to hand this
    function a unit count -- 1 for an audit, 3 for the bundle -- against a
    $0.01/unit Price, which metered a third of what the route charged. The
    price is the fact each route already knows; a unit count is a derived
    number that was derived wrongly, in the wrong place. It has no default
    for the same reason: a route that forgets to say what it charges should
    not quietly bill whatever the cheapest route happens to cost.

    Called only on the success path, which is the whole point: every route
    returns 502 without reaching here when an audit fails to run, so a failed
    audit is never charged for. That guarantee used to hold only for Stripe
    subscribers -- x402 callers were settled during authentication, so they
    paid for failed audits too. Settling here closes that gap.

    Never raises. Returns a warning string to surface on the response, or
    None on success/no-op. One outcome does withhold the result: a settle the
    facilitator definitely REFUSED leaves `settle_state == "refused"` on the
    pending payment, and _deliver answers that with the payable 402 instead
    of the audit (see _settlement_refused). Every other billing hiccup is
    surfaced beside the result, never by corrupting or withholding it.
    """
    if auth.pending_payment is not None:
        if not x402_payments.settle_sync(auth.pending_payment):
            # Not settled -- but "not charged" is only true for a refusal.
            # A settle the facilitator broadcast and has not confirmed, or
            # one this node stopped waiting for, may still move the money;
            # the payer is told exactly that, with the hash where there is
            # one, instead of a false "free".
            state = getattr(auth.pending_payment, "settle_state", None)
            if state == "pending":
                result = getattr(auth.pending_payment, "settle_result", None)
                transaction = getattr(result, "transaction", None) or "unknown"
                return (
                    "payment settlement is pending on-chain "
                    f"(transaction {transaction}); this call is being charged "
                    "and the receipt header carries the transaction"
                )
            # WHY it failed goes on the body, not only into our log.
            #
            # The node is the only party that knows: the payer sees a 200
            # with an audit in it, and the operator has to be logged into the
            # box to read the reason. On 2026-09-08 that cost the owner a
            # night of grepping for a one-line answer the node already had in
            # hand and threw away. The reason is about the payer's own
            # payment, and a settle that failed is exactly when a machine
            # client needs to know whether retrying is safe.
            reason = getattr(auth.pending_payment, "settle_error", None)
            because = f" -- {reason}" if reason else ""
            if state == "unknown":
                return (
                    "payment settlement status is unknown: this node did not "
                    f"get an answer{because}. The transfer may still complete "
                    "on-chain; do not re-pay for this call"
                )
            # We delivered without collecting. Deliberately the lesser evil
            # versus charging for undelivered work, but it must be visible.
            # A False with no state is a refusal by settle_sync's contract;
            # say so on the handle, so _deliver withholds on exactly one flag.
            auth.pending_payment.settle_state = "refused"
            return (
                "payment settlement failed after the audit ran; this call was "
                f"not charged{because}"
            )
        return None

    if not auth.stripe_billable:
        return None
    try:
        billing.record_usage(auth.customer_id, price_cents=round(price_usd * 100))
        return None
    except Exception as exc:
        return f"usage recording failed: {exc}"


def _settlement_refused(auth) -> Optional[JSONResponse]:
    """The 402 that answers a payment the facilitator refused to SETTLE.

    Settlement runs after the audit has produced a result (_bill), so a
    refusal there used to be answered with the audit anyway, plus a warning
    that nothing was charged. That gave the work away on every refusal -- a
    payer whose balance moved between verify and settle, a facilitator whose
    settlement signer had run out of gas (2026-09-11: every call through it
    was delivered free) -- and made "not charged" a feature from the payer's
    side. The reference x402 server discards the handler's response on a
    failed settle and re-issues the 402; this does the same.

    Only a definite refusal withholds. "pending" (the facilitator broadcast
    a transfer it has not seen confirm) and "unknown" (it did not answer in
    time) may have moved the money, and withholding a result the payer may
    have paid for is the worse failure; those still deliver, with the state
    and the hash on the body. Nothing is charged on a refusal: settle is the
    only step that moves funds, and the admitted nonce expires on its own,
    so the payer signs a fresh authorization and retries.
    """
    pending = getattr(auth, "pending_payment", None)
    if pending is None or getattr(pending, "settle_state", None) != "refused":
        return None
    reason = getattr(pending, "settle_error", None) or "the facilitator refused to settle it"
    try:
        price_usd = float(str(getattr(pending, "price", "") or "").lstrip("$"))
    except ValueError:
        price_usd = _DEFAULT_PRICE_USD
    logging.getLogger(__name__).warning(
        "x402 audit WITHHELD: settle refused after the audit ran (%s); "
        "nothing charged, result not delivered", reason,
    )
    return _payment_required_response(
        host=getattr(auth, "challenge_host", None),
        price_usd=price_usd,
        path=getattr(auth, "challenge_path", None),
        error="settlement_refused",
        error_detail=(
            "the payment verified but the facilitator refused to settle it "
            f"after the audit ran: {reason}. Nothing was charged and the result "
            "was not delivered. Sign a fresh authorization and retry."
        ),
    )


# ---------------------------------------------------------------------------
# Target URL gate.
#
# Every audit fetches the caller's URL from inside this deployment: httpx for
# SEO/security, a real Chromium for WCAG/performance. A public API that
# fetches arbitrary URLs is a proxy into wherever it runs unless it refuses
# to: the cloud metadata endpoint (169.254.169.254, metadata.google.internal),
# loopback (this very service, its own /billing routes), the VPC, link-local.
# None of those are "sites" anyone audits, and each is a way to make the node
# do something on the caller's behalf for a 402 that costs them nothing.
#
# Checked BEFORE rate limiting and payment, so a refused URL costs the caller
# nothing and costs this node no facilitator call. The hostname is resolved
# here and every address it resolves to must be globally routable -- a name
# check alone is beaten by a DNS record pointing at 10.0.0.1.
#
# ALLOW_PRIVATE_TARGETS=1 turns the gate off for a node under local test,
# where the target genuinely is 127.0.0.1. Never set it on the deployed node.
# ---------------------------------------------------------------------------
_ALLOW_PRIVATE_TARGETS = os.environ.get("ALLOW_PRIVATE_TARGETS") == "1"
_BLOCKED_TARGET_HOSTS = {"localhost", "metadata", "metadata.google.internal"}


def _target_url_problem(url: Optional[str]) -> Optional[str]:
    """Why `url` must not be fetched, or None when it may be.

    Delegates to audits.blocked_target_reason, which is also what every
    redirect hop is checked against. Two copies of this rule would drift, and
    the copy that drifts is the one guarding the fetch.
    """
    return audits.blocked_target_reason(url)


def _reject_unfetchable_target(url: Optional[str]) -> Optional[JSONResponse]:
    """A 400 the caller can act on, or None when the URL is fetchable.

    400 rather than 402: the request is malformed for this service whatever
    the caller pays, so it is refused before any payment instrument is read.
    """
    problem = _target_url_problem(url)
    if problem is None:
        return None
    return JSONResponse(
        status_code=400,
        content={
            "status": "error",
            "detail": f"'url' {problem}. Nothing was charged for this request.",
            "billed": False,
        },
    )


def _reject_missing_input(payload) -> Optional[JSONResponse]:
    """A 400 for a body with neither `html` nor `url`, or None.

    Runs before the payment is read, like the target gate: a request this
    service cannot act on costs the caller nothing and costs this node no
    facilitator call, and says so.
    """
    if getattr(payload, "html", None) or getattr(payload, "url", None):
        return None
    return JSONResponse(
        status_code=400,
        content={
            "status": "error",
            "detail": "Provide 'html' or 'url'. Nothing was charged for this request.",
            "billed": False,
        },
    )


def _rate_limited_response() -> JSONResponse:
    """429 an agent can actually act on: Retry-After tells a machine caller
    when to come back instead of hammering the endpoint or giving up on it
    permanently. Nothing has been billed at this point -- the limiter runs
    before any payment is settled."""
    response = JSONResponse(
        status_code=429,
        content={
            "status": "error",
            "detail": (
                f"Rate limit exceeded ({RATE_LIMIT_PER_MINUTE} requests/minute). "
                "Nothing was charged for this request."
            ),
            "limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "retry_after_seconds": 60,
            "billed": False,
        },
    )
    response.headers["Retry-After"] = "60"
    return response


_BROWSER_PATH: Optional[str] = None
_BROWSER_PATH_RESOLVED = False


def _resolve_browser_path() -> None:
    """Ask Playwright once, at startup, where its Chromium lives.

    Blocking and sync (Playwright's driver raises inside a running loop), so it
    is called from the lifespan through a worker thread, never from a request.
    """
    global _BROWSER_PATH, _BROWSER_PATH_RESOLVED
    if _BROWSER_PATH_RESOLVED:
        return
    _BROWSER_PATH_RESOLVED = True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            _BROWSER_PATH = p.chromium.executable_path
    except Exception:
        _BROWSER_PATH = None


def _browser_status() -> dict:
    """Whether the Chromium this Playwright expects is on disk.

    One stat() against a path resolved at startup, so /health stays on the
    event loop: a sync route would take one of the MAX_CONCURRENT_AUDITS
    threadpool tokens and queue the probe behind a stranger's page load, which
    marks the instance unhealthy exactly when it is busiest earning.
    """
    if not _BROWSER_PATH:
        return {"ok": True, "detail": "undetermined"}
    if os.path.exists(_BROWSER_PATH):
        return {"ok": True, "detail": "chromium present"}
    return {"ok": False, "detail": "chromium missing at %s" % _BROWSER_PATH}


def _run_axe_all_frames(page) -> dict:
    """Run axe with the harness present in every frame, and disclose any gaps.

    axe-playwright-python injects its bundle with a single page.evaluate, which
    reaches the main frame only. axe's cross-frame protocol needs the bundle
    present in EVERY frame it is asked to reach, so violations inside an iframe
    -- a cookie banner, an embedded booking widget, a payment form, all of them
    common and all of them in scope for WCAG -- were simply absent from the
    result. Absent, not reported: the page came back cleaner than it is, which
    is the one thing this service promises never to do.

    Injecting into each frame first lets axe reach them. Any frame that refuses
    the injection (cross-origin without CORS, about:blank, torn down mid-run)
    is counted and disclosed on the result rather than passed over in silence.
    """
    unreachable = 0
    frames = list(getattr(page, "frames", []) or [])
    for frame in frames[1:]:  # frames[0] is the main frame, which run() handles
        try:
            frame.evaluate(_axe.axe_script)
        except Exception:
            unreachable += 1

    result = _axe.run(page, options=AXE_OPTIONS).response
    if isinstance(result, dict):
        result["frames_audited"] = max(len(frames) - unreachable, 1)
        result["frames_unreachable"] = unreachable
    return result


def _run_axe(html: Optional[str], url: Optional[str]) -> dict:
    def _audit(page) -> dict:
        if url:
            audits.goto_guarded(page, url, wait_until="networkidle", timeout=15000)
        else:
            page.set_content(html, wait_until="networkidle", timeout=15000)
        return _run_axe_all_frames(page)

    # Pooled browser, fresh isolated context per call -- see browser_pool.
    return browser_pool.with_page(_audit)


def _run_axe_and_performance(url: str):
    """One page load serving BOTH the accessibility and performance audits.

    /audit/bundle used to hit the target URL four times for a single call:
    two full Chromium page loads (axe, then performance) plus two separate
    HTTP GETs (SEO, then security). That is four times the latency on the
    most expensive route, and four hits on a stranger's origin per call is
    how an audit bot gets its user agent blocked -- which would cost us the
    ability to audit that site at all.

    Both browser-based checks need exactly the same thing: the page,
    rendered, once. The response listener must be attached before navigation
    or the measurement misses the requests it is meant to count.
    """
    stats = {"bytes": 0, "requests": 0, "unmeasured": 0}

    def _on_response(response):
        stats["requests"] += 1
        if stats["bytes"] > audits.HEAVY_PAGE_BYTES:
            return
        measured = audits.response_bytes(response)
        if measured is None:
            stats["unmeasured"] += 1
        else:
            stats["bytes"] += measured

    def _both(page):
        page.on("response", _on_response)
        audits.goto_guarded(page, url, wait_until="networkidle", timeout=30000)
        dom_node_count = page.evaluate("document.querySelectorAll('*').length")
        # axe runs against the already-loaded page rather than reloading it.
        return _run_axe_all_frames(page), dom_node_count

    axe_raw, dom_node_count = browser_pool.with_page(_both, user_agent=audits.USER_AGENT)
    performance = audits.performance_result_from_metrics(
        dom_node_count, stats["bytes"], stats["requests"]
    )
    return axe_raw, performance


def _remediation_notes(violations: list) -> Optional[dict]:
    """Best-effort, clearly-labeled AI remediation suggestions.

    This never influences pass/fail -- axe-core's findings are the sole
    source of truth for the audit result. If this fails or is disabled,
    the audit result is returned without it; it never gets folded into
    a fabricated pass.
    """
    if not GEMINI_API_KEY or not violations:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        summary = "\n".join(f"- {v['id']}: {v['help']}" for v in violations[:20])
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Given these axe-core WCAG violations, write a short, "
                f"actionable remediation note for each:\n{summary}"
            ),
            config={"temperature": 0.2},
        )
        return {"ai_generated": True, "notes": response.text}
    except Exception as exc:
        return {"ai_generated": True, "notes": None, "error": str(exc)}


@app.get("/", response_class=FileResponse)
async def landing_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/billing/success", response_class=FileResponse)
async def checkout_success_page():
    return FileResponse(STATIC_DIR / "success.html")


@app.get("/billing/cancel", response_class=FileResponse)
async def checkout_cancel_page():
    return FileResponse(STATIC_DIR / "cancel.html")


@app.get("/llms.txt", response_class=FileResponse)
async def llms_txt():
    return FileResponse(STATIC_DIR / "llms.txt", media_type="text/plain")


@app.get("/mcp.json", tags=["discovery"])
async def mcp_manifest():
    """The MCP tool manifest, with prices and rails taken from live config.

    The tool names, descriptions and input schemas come from the static file
    -- they are documentation and change with the product, not with the
    deployment. Two things do NOT come from it, because they are deployment
    state and the file cannot know them:

    `auth.methods`, because the static file asserted x402 unconditionally. It
    went on asserting it after x402 was switched off, so an agent reading this
    manifest -- which is what the MCP registry points at -- would construct a
    payment for a rail this deployment cannot settle. That is the one thing
    this codebase refuses to do everywhere else: /.well-known/agent.json and
    every 402 already omit rails that cannot settle. This route was the hole
    in that rule.

    Per-tool prices, because _CATALOG exists precisely so the manifest, the
    price an agent reads, and the price the route actually charges cannot
    drift apart -- and a second hand-maintained copy of the numbers defeats
    that by construction.

    Per-tool SCHEMAS, for the same reason and it is not hypothetical: this
    file already carried its own inputSchema copies, and they had drifted
    from what /mcp serves -- the file omitted the html-or-url either-or that
    the routes enforce, so an agent reading this manifest could construct a
    body the route rejects. Schemas, titles and annotations are now taken from
    _mcp_tools(), the same function the live MCP endpoint answers
    tools/list with, so the two cannot disagree again. What the static file
    still owns is the prose: tool names and descriptions.
    """
    with open(STATIC_DIR / "mcp.json", encoding="utf-8") as handle:
        manifest = json.load(handle)

    live_methods = _payment_methods_live()
    prices = {entry["path"]: entry["price_usd"] for entry in _CATALOG}
    if workers is not None:
        prices.update({w.path: w.price_usd for w in workers.catalog.CATALOG})
    live_tools = {tool["name"]: tool for tool in _mcp_tools()}
    # A worker-backed tool this deployment cannot deliver is not listed,
    # exactly as /work omits it: the static file names it, the live catalog
    # decides whether it is for sale here.
    manifest["tools"] = [
        tool for tool in manifest.get("tools", [])
        if tool.get("name") not in _MCP_WORKER_TOOLS or tool.get("name") in live_tools
    ]

    # Absolute URLs in the static file are written against the production
    # host, because a crawler fetching the raw file out of the repo has no
    # base to resolve a relative path against. Served, they must name the
    # host actually answering -- a manifest that hands a client someone
    # else's MCP endpoint is worse than one that omits it.
    base = PUBLIC_BASE_URL.rstrip("/")
    manifest["websiteUrl"] = f"{base}/"
    manifest["documentationUrl"] = f"{base}/.well-known/agent.json"
    for remote in manifest.get("remotes", []):
        remote["url"] = f"{base}/mcp"
    for icon in manifest.get("icons", []):
        icon["src"] = f"{base}/favicon.svg"

    manifest["auth"]["methods"] = live_methods
    manifest["auth"]["description"] = (
        "Every tool below requires one of the methods in `methods`, which "
        "lists only the rails this deployment can actually settle. An "
        "unauthenticated call returns HTTP 402 with the price and payment "
        "challenge, not an error."
    )

    for tool in manifest.get("tools", []):
        endpoint = tool.get("httpEndpoint") or {}
        path = endpoint.get("path")
        if path in prices:
            endpoint["price_usd"] = prices[path]
        live = live_tools.get(tool.get("name"))
        if live is not None:
            for field in ("title", "inputSchema", "outputSchema", "annotations"):
                tool[field] = live[field]

    return JSONResponse(content=manifest, media_type="application/json")


@app.get("/favicon.svg", response_class=FileResponse, tags=["discovery"])
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/og-image.png", response_class=FileResponse, tags=["discovery"])
async def og_image():
    # Referenced by og:image/twitter:image. Social scrapers fetch this
    # unauthenticated and cache aggressively, so it must stay a stable,
    # public URL -- a link with no preview card is a link people don't click.
    return FileResponse(STATIC_DIR / "og-image.png", media_type="image/png")


@app.get("/hero.jpg", response_class=FileResponse, include_in_schema=False)
async def hero_image():
    # Landing-page artwork (robot mascot over Earth), cut from the owner's design.
    return FileResponse(STATIC_DIR / "hero.jpg", media_type="image/jpeg")


@app.get("/logo-hv.png", response_class=FileResponse, include_in_schema=False)
async def logo_hv():
    # The owner's glowing HV mark, used in the landing page header and footer.
    return FileResponse(STATIC_DIR / "logo-hv.png", media_type="image/png")


@app.get("/robots.txt", response_class=FileResponse, tags=["discovery"])
async def robots_txt():
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", response_class=FileResponse)
async def sitemap_xml():
    return FileResponse(STATIC_DIR / "sitemap.xml", media_type="application/xml")


# Registered at BOTH paths on purpose. Google Cloud Run's frontend
# intercepts /healthz and answers it itself -- a request never reaches this
# container, and the caller gets Google's own HTML 404 page rather than
# anything FastAPI produced. Verified against the live service: the body is
# Google's "Error 404 (Not Found)!!1" page, not FastAPI's
# {"detail":"Not Found"}. No application code can serve /healthz on this
# platform, so /health is the one that actually works here, while /healthz
# is kept for any environment (local, other hosts) that does not reserve it.
@app.get("/health", tags=["discovery"])
@app.get("/healthz", tags=["discovery"])
async def health_check():
    """Liveness AND the one dependency every paid route needs: a browser.

    This returned a constant, so it could only distinguish "uvicorn is
    listening" from "the box is down". The failure that actually costs money
    sits between those: Chromium absent, or at a revision this Playwright does
    not expect. Every audit then 502s, nothing is billed, no customer is
    served -- and the container reports itself healthy throughout, so nothing
    restarts and nobody is paged.

    Stays `async`: a sync route would take one of the MAX_CONCURRENT_AUDITS
    threadpool tokens and queue this probe behind a stranger's page load, which
    is how a busy, earning instance gets marked unhealthy. The path Playwright
    expects is resolved once in the lifespan, off the loop, so the probe itself
    is a single stat().

    Fails OPEN when the answer is unknown: a probe that cannot tell reports ok,
    because a false 503 restart-loops a node that is serving fine. Only a
    definite "the executable is not there" degrades.
    """
    browser = _browser_status()
    body = {
        "status": "ok" if browser["ok"] else "degraded",
        "service": "wcag-audit-engine",
        "browser": browser,
    }
    # The worker network reports beside the audits, never into them: its
    # status is informational and CANNOT change this endpoint's status code.
    # A wedged provider must not restart-loop a container that is still
    # selling audits perfectly well.
    if workers is not None:
        try:
            body["workers"] = workers.health()
        except Exception as exc:  # pragma: no cover - defensive
            body["workers"] = {"configured": False, "error": f"{type(exc).__name__}"}
    return JSONResponse(status_code=200 if browser["ok"] else 503, content=body)


_AUTH_DESCRIPTION = (
    "WHICH of these a given deployment accepts is not fixed and this schema "
    "cannot know it: read `payment.methods` in /.well-known/agent.json, or "
    "the `accepts[]` array in any 402 response. Both list only rails that can "
    "genuinely settle right now. The headers each scheme uses: X-API-Key (a "
    "prepaid key bought through the MPP top-up rail and spent per call at "
    "the same rates; there are no subscriptions); X-PAYMENT "
    "(x402 -- price/network/payTo arrive in the 402 body); Authorization: "
    "Payment ... (MPP -- Stripe SPT for fiat or Tempo for crypto, challenges "
    "arrive in the WWW-Authenticate headers on a 402)"
)
_URL_INPUT_SCHEMA = {"url": "string (required)"}
_HTML_OR_URL_INPUT_SCHEMA = {"html": "string (optional)", "url": "string (optional, one of html/url required)"}

# One row per sellable route. Kept as data rather than hand-written JSON so
# the manifest, the pricing an agent reads, and the prices the routes
# actually charge cannot drift apart.
_CATALOG = [
    {
        "path": "/audit/wcag",
        "price_usd": 0.05,
        "input": _HTML_OR_URL_INPUT_SCHEMA,
        "description": (
            "Website accessibility audit (a11y, WCAG compliance check): WCAG "
            "2.1 level A and AA conformance of any live URL or raw HTML, tested "
            "with axe-core in a real headless browser. Returns every violation "
            "with rule id, impact, help text and affected node count. "
            "Deterministic rules; a check that cannot run returns an error, "
            "never a pass."
        ),
        "returns": "pass (bool), violations[] with id/impact/help/help_url/nodes_affected.",
    },
    {
        "path": "/audit/seo",
        "price_usd": 0.05,
        "input": _HTML_OR_URL_INPUT_SCHEMA,
        "description": (
            "SEO audit of a web page (on-page SEO check) for any live URL or "
            "raw HTML: title tag, meta description, H1 heading structure, "
            "canonical link, OpenGraph and social tags, JSON-LD structured "
            "data, html lang attribute. Rule-by-rule findings with severity and "
            "what is missing or malformed."
        ),
        "returns": "pass (bool), findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/security",
        "price_usd": 0.05,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "Security headers check for a website: audits HTTPS, HSTS, Content- "
            "Security-Policy (CSP), X-Content-Type-Options, X-Frame-Options / "
            "clickjacking protection, Referrer-Policy and CORS from the real "
            "HTTP response of any live URL. Findings with severity for what is "
            "missing. Header posture only, not a penetration test."
        ),
        "returns": "pass (bool), findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/performance",
        "price_usd": 0.05,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "Page speed and page weight audit of a web page from one real "
            "browser load: total bytes transferred, HTTP request count and DOM "
            "node count, measured rather than estimated, with findings when the "
            "page is heavy. Page-weight signals, not Core Web Vitals."
        ),
        "returns": "pass (bool), metrics{}, findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/bundle",
        "price_usd": 0.15,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "Full website audit in one call: accessibility (WCAG 2.1 A/AA via "
            "axe-core), on-page SEO, HTTP security headers and page speed / "
            "page weight, from a single browser load of one URL. Cheaper than "
            "four separate calls; if any part cannot run, nothing is billed."
        ),
        "returns": "pass (bool) plus wcag{}, seo{}, security{}, performance{} sub-results.",
    },
]


def _schema_for(prose_input: dict) -> dict:
    """The JSON Schema matching a catalog row's prose `input` description.

    One function rather than a per-row field so the manifest, the MCP tools
    and the Bazaar index cannot end up describing different bodies for the
    same route -- the failure mode being an agent that generates a request
    from the manifest and gets a 400 from the route.
    """
    return _MCP_URL_SCHEMA if prose_input is _URL_INPUT_SCHEMA else _MCP_HTML_OR_URL_SCHEMA


# Paths that are served but are not their own catalog row. /audit predates
# the named dimensions and is kept working for callers wired to it.
_CATALOG_ALIASES = {"/audit": "/audit/wcag"}


_openapi_default = app.openapi


def _openapi_with_payment_info() -> dict:
    """The OpenAPI document, annotated the way MPP tooling reads it.

    MPP's reference implementation discovers paid endpoints from the OpenAPI
    document itself: an operation is payable iff it carries `x-payment-info`.
    FastAPI's generated document had none, so `mppx validate` -- and any
    MPP-aware agent using the same discovery path -- saw this service as
    having zero paid endpoints, warned `No endpoints with x-payment-info`,
    and skipped every challenge and payment check. A tollbooth whose own
    directory says "no tolls here".

    Annotated per request rather than cached: the offers are gated on which
    rails can settle (and at what amount), and a cached copy would freeze a
    rail decision past a config change. The base document IS cached by
    FastAPI; only the annotation is recomputed, on a copy, so repeated calls
    cannot accumulate onto the cached base.

    Paths are relative in `x-service-info.docs` so a self-hosted copy cannot
    hand its clients the production endpoints -- same rule as /mcp.json.
    """
    import copy

    doc = copy.deepcopy(_openapi_default())
    reverse_aliases: dict = {}
    for alias, target in _CATALOG_ALIASES.items():
        reverse_aliases.setdefault(target, []).append(alias)
    for entry in _CATALOG + _worker_discovery_entries():
        offers = list(
            mpp_payments.discovery_offers(entry["price_usd"], description=entry["description"])
        )
        # The x402 rail, too. On an x402-only deploy the MPP offers are empty
        # and every paid route used to read as free here while the 402,
        # agent.json, llms.txt and mcp.json all priced it.
        x402_offer = x402_payments.discovery_offer(f"${entry['price_usd']:.2f}")
        if x402_offer:
            offers.append(x402_offer)
        if not offers:
            continue
        for path in (entry["path"], *reverse_aliases.get(entry["path"], [])):
            operation = doc.get("paths", {}).get(path, {}).get("post")
            if operation is None:
                continue
            operation["x-payment-info"] = {"offers": offers}
            if entry.get("buyer_note"):
                # Workers above the x402 clients' $1 default cap: see
                # workers.catalog.buyer_note. A standard extension key, kept
                # out of x-payment-info so mppx's offer validation is untouched.
                operation["x-buyer-note"] = entry["buyer_note"]
            # The discovery spec requires a declared 402 on any operation
            # carrying x-payment-info; mppx validate fails the document
            # without it ("Operation with x-payment-info MUST have a 402
            # response").
            operation.setdefault("responses", {}).setdefault(
                "402", {"description": "Payment Required"}
            )
            # A concrete example, because the reference validator (and any
            # client that probes for a challenge) derives its probe body
            # from here: with only a schema to go on it generates a guess,
            # and against the html-or-url anyOf that guess fails validation
            # -- the probe gets 422 forever and the route reads as broken
            # when it is merely under-documented. A bare url satisfies
            # every paid route's schema.
            json_content = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json")
            )
            if json_content is None and entry.get("input_schema") is not None:
                # Worker handlers read their body by hand, so FastAPI documents
                # none, and a request generator reading this spec would send
                # nothing. Their catalog schema IS the contract; publish it.
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": entry["input_schema"]}},
                }
                json_content = operation["requestBody"]["content"]["application/json"]
            if json_content is not None:
                json_content.setdefault(
                    "example", entry.get("input_example") or {"url": "https://example.com"}
                )
    doc["x-service-info"] = {
        "categories": ["accessibility", "seo", "security", "performance"],
        "docs": {
            "apiReference": "/docs",
            "homepage": "/",
            "llms": "/llms.txt",
            "agent": "/.well-known/agent.json",
            "ard": "/.well-known/ard.json",
        },
    }
    return doc


def _worker_discovery_entries() -> list:
    """Live workers, shaped like catalog rows for the annotator above.

    Without these, every /work route read as free in openapi.json while its
    402, agent.json and /work all priced it -- invisible to the crawlers that
    find paid endpoints by x-payment-info. Only workers this deployment can
    deliver, by the same rule the manifest follows.
    """
    if workers is None or not workers.is_configured():
        return []
    return [
        {
            "path": worker.path,
            "price_usd": worker.price_usd,
            "description": worker.description,
            "input_schema": worker.input_schema,
            "input_example": workers.catalog.example_for(worker),
            **({"buyer_note": workers.catalog.buyer_note(worker)}
               if workers.catalog.buyer_note(worker) else {}),
        }
        for worker in workers.catalog.live()
    ]


app.openapi = _openapi_with_payment_info


def _max_catalog_price_cents() -> int:
    """The dearest single call this node sells, in cents.

    Used to answer whether a rail with a minimum charge has anything at all it
    could settle here.
    """
    prices = [round(entry["price_usd"] * 100) for entry in _CATALOG]
    return max(prices)


def _payment_methods_live() -> list:
    """Only the rails that can actually settle on this deployment.

    An agent picks a payment method from this list, so listing a method that
    isn't configured would send it down a path that cannot possibly succeed.
    """
    methods = []
    if x402_payments.is_configured():
        methods.append("x402")
    # The SPT rail is listed only if SOME sellable route clears Stripe's
    # minimum card charge. This list is deployment-wide while the floor is
    # per-amount, so the honest question is "is there anything here this rail
    # could ever settle" -- and with the catalog priced in cents, the
    # answer today is no. Listing it anyway would put a method in the array an
    # agent picks from that fails at the Stripe API every single time.
    if mpp_payments.stripe_available_for(_max_catalog_price_cents()):
        methods.append("mpp-stripe")
    if mpp_payments.tempo_configured():
        methods.append("mpp-tempo")
    if billing.is_configured() and billing.human_plans_live():
        methods.append("stripe_api_key")
    return methods


@app.get("/.well-known/ard.json", tags=["discovery"])
async def ard_manifest():
    """Agentic Resource Discovery manifest (agenticresourcediscovery.org).

    One entry per audit and per LIVE worker, plus the MCP server card, the
    OpenAPI document and agent.json, each with the representative queries
    and capability tokens a federated registry indexes on. Built from the
    catalogs the routes charge from; see app/ard.py.
    """
    return ard.build_manifest(
        base_url=PUBLIC_BASE_URL,
        version=SERVICE_VERSION,
        display_title=SERVICE_TITLE,
        audits=_CATALOG,
        audit_output_schemas=_MCP_OUTPUT_SCHEMAS,
        audit_input_schema_for=lambda row: _schema_for(row["input"]),
        mcp_tool_names=[tool["name"] for tool in _mcp_tools()],
        workers_catalog=(workers.catalog if workers is not None and workers.is_configured() else None),
        worker_contract=(workers.catalog.contract if workers is not None else None),
    )


@app.get("/.well-known/agent.json", tags=["discovery"])
async def agent_manifest(request: Request):
    base = PUBLIC_BASE_URL
    live_methods = _payment_methods_live()
    return {
        "schema_version": "1.0",
        "name": SERVICE_TITLE,
        "base_url": base,
        "description": (
            "38 machine-payable dev utilities (the `workers` section: LLM "
            "inference, web search and extraction, Base chain reads, market "
            "and prediction-market data, BigQuery analysis and forecasting, "
            "deterministic regression and probability statistics, "
            "image/speech/video generation, sandboxed Python, maps, cited "
            "research and verification) and five deterministic site audits "
            "(the `endpoints` section: accessibility via axe-core, SEO, "
            "security headers, performance, bundle). One price per call, "
            "payable by software over HTTP 402 with no account. Every "
            "capability carries its input and output JSON Schema here and in "
            "/openapi.json; every delivered /work job has a receipt. The "
            "audits are rule-based checks against the actual page; a check "
            "that could not run is never reported as a pass."
        ),
        "pricing": {
            "model": "per-call",
            "currency": "USD",
            "single_audit_usd": _price_of("/audit/wcag"),
            "bundle_usd": _price_of("/audit/bundle"),
            "note": (
                "Per-call pricing is the product and is what a machine caller "
                "should use -- no account, no minimum, no subscription."
            ),
        },
        "payment": {
            "methods": live_methods,
            "challenge": (
                "Unauthenticated calls return HTTP 402 with a machine-readable "
                "`accepts` array in the body and, for MPP, one signed "
                "WWW-Authenticate: Payment challenge per method."
            ),
            "note": (
                "Only methods actually configured on this deployment are listed; "
                "an empty list means no machine payment rail is live right now."
            ),
            "receipt": (
                "A settled x402 payment returns the facilitator's settle "
                "response -- transaction hash, network, payer -- on the 200 in "
                "the PAYMENT-RESPONSE header (X-PAYMENT-RESPONSE for v1 clients). "
                "Over MCP the same receipt is in the tool result's "
                "_meta[\"x402/payment-response\"]."
            ),
            "mcp": (
                "The /mcp endpoint speaks the x402 MCP protocol: an unpaid "
                "tools/call returns isError with the v2 PaymentRequired in "
                "structuredContent; send the signed PaymentPayload in "
                "params._meta[\"x402/payment\"] (the x402.mcp client does this "
                "for you). HTTP headers X-PAYMENT / PAYMENT-SIGNATURE / "
                "X-API-Key on the POST work too."
            ),
        },
        "limits": {
            "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "on_limit": "HTTP 429 with Retry-After; nothing is billed.",
        },
        "discovery": {
            "openapi": f"{base}/openapi.json",
            "ard": f"{base}/.well-known/ard.json",
            "mcp_endpoint": f"{base}/mcp",
            "mcp": f"{base}/mcp.json",
            "llms_txt": f"{base}/llms.txt",
            "docs": f"{base}/docs",
        },
        "guarantees": [
            "You are charged only for an audit that produced a result. A check "
            "that could not run returns HTTP 502, is never settled, and is "
            "never reported as a pass -- an x402 payment is verified to grant "
            "access and settled only once the audit has actually produced a "
            "result (a settlement the facilitator refuses withholds the result "
            "and charges nothing), a prepaid key debited for it is refunded, "
            "and an MPP credential it consumed is accepted again on the retry.",
            "Rate-limited requests are rejected before any payment is settled, "
            "so a 429 never costs you anything.",
            "Results are deterministic rule-based checks against the live page, "
            "never an LLM's opinion.",
        ],
        "endpoints": [
            {
                "path": entry["path"],
                "method": "POST",
                "payment_required": True,
                "price_usd": entry["price_usd"],
                "input": entry["input"],
                # The prose `input` above is for a human skimming the
                # manifest. `input_schema`/`output_schema` are the same facts
                # as JSON Schema, and they are the same objects the MCP tools
                # and the Bazaar index advertise, so a crawler scoring this
                # node -- or an agent generating a request from it -- gets a
                # parseable contract instead of "string (required)".
                "input_schema": _schema_for(entry["input"]),
                "output_schema": _MCP_OUTPUT_SCHEMAS[entry["path"]],
                "returns": entry["returns"],
                "description": entry["description"],
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": live_methods,
                "example_request": {
                    "url": f"{base}{entry['path']}",
                    "method": "POST",
                    "headers": {"Content-Type": "application/json", "X-API-Key": "<your key>"},
                    "body": {"url": "https://example.com"},
                },
            }
            for entry in _CATALOG
        ]
        + [
            {
                "path": "/audit",
                "method": "POST",
                "payment_required": True,
                "price_usd": _price_of("/audit"),
                "input": _HTML_OR_URL_INPUT_SCHEMA,
                "input_schema": _schema_for(_HTML_OR_URL_INPUT_SCHEMA),
                "output_schema": _MCP_OUTPUT_SCHEMAS["/audit/wcag"],
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": live_methods,
                "note": "Alias of /audit/wcag, kept for backward compatibility.",
            },
        ],
        # The worker network is listed SEPARATELY from `endpoints`, not folded
        # into it. These are not audits: an audit is a deterministic rule
        # check against a page, while a worker is a task carried out against
        # somebody's live API or model. Mixing them in one array would tell a
        # buying agent that a market quote carries the audits' determinism
        # guarantee, which it does not.
        "workers": _worker_manifest_entries(live_methods),
    }


def _worker_manifest_entries(live_methods: list) -> dict:
    """The worker network as its own section of the manifest.

    Empty and clearly marked when the network is not configured, rather than
    absent: an agent that read this manifest yesterday should be able to tell
    "turned off here" apart from "this node is too old to have it".
    """
    if workers is None or not workers.is_configured():
        return {"available": False, "count": 0, "capabilities": []}
    # Only workers this deployment can actually deliver. A capability whose
    # provider has no credential here is omitted rather than listed, for the
    # same reason `payment.methods` lists only rails that can settle.
    live_workers = workers.catalog.live()
    return {
        "available": True,
        "count": len(live_workers),
        "index": f"{PUBLIC_BASE_URL}/work",
        "note": (
            "Machine-payable tasks carried out against live providers, priced "
            "per call on the same x402 rail as the audits. Unlike the audits, "
            "these are not deterministic rule checks -- each states its own "
            "sources in the result."
        ),
        "idempotency": (
            "Send an Idempotency-Key header to make a retry safe: a repeated "
            "key returns the stored result and is not charged again."
        ),
        # The 200 body every capability below returns: the envelope once,
        # each capability's own `result` schema on its row.
        "response_envelope": workers.catalog.contract.RESPONSE_ENVELOPE,
        "receipts": (
            f"Every delivered job's body carries receipt_id and receipt_url; "
            f"GET {PUBLIC_BASE_URL}/work/receipts/{{receipt_id}} (free) returns "
            "payer, pay_to, amount, asset, network, transaction hash, execution "
            "status and sha256 hashes of the request and the delivered result."
        ),
        "capabilities": [
            {
                "path": worker.path,
                "method": "POST",
                "name": worker.name,
                "title": worker.title,
                "payment_required": True,
                "price_usd": worker.price_usd,
                "tier": worker.tier,
                "description": worker.description,
                "tags": worker.tags,
                "input_schema": worker.input_schema,
                "output_schema": worker.output_schema,
                "returns": worker.returns,
                "max_seconds": worker.max_seconds,
                "composes": worker.composes,
                "payment_methods": live_methods,
                **({"buyer_note": workers.catalog.buyer_note(worker)}
                   if workers.catalog.buyer_note(worker) else {}),
            }
            for worker in live_workers
        ],
    }


_REPORT_CSS = """
body{margin:0;background:#000;color:#f4ede6;font-family:Inter,-apple-system,
BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6}
.w{max-width:820px;margin:0 auto;padding:56px 28px 80px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:#8d8d94;font-size:15px;margin:0 0 40px;word-break:break-all}
h2{font-size:13px;font-family:ui-monospace,monospace;letter-spacing:.14em;
text-transform:uppercase;color:#63636a;font-weight:500;margin:38px 0 14px}
.card{border:1px solid #1e1e21;border-radius:10px;padding:22px;margin-bottom:14px}
.verdict{display:flex;justify-content:space-between;align-items:center;gap:16px;
font-weight:600;margin-bottom:14px}
.ok{color:#4ade80}.bad{color:#ff8a2a}
ul{list-style:none;padding:0;margin:0}
li{padding:11px 0;border-top:1px solid #1e1e21;color:#8d8d94;font-size:14.5px}
li b{color:#f4ede6;font-weight:600}
.tag{font-family:ui-monospace,monospace;font-size:11px;color:#ff8a2a;
text-transform:uppercase;letter-spacing:.08em}
.metrics{display:flex;gap:28px;flex-wrap:wrap;color:#8d8d94;font-size:14px}
.metrics b{display:block;color:#f4ede6;font-size:20px;font-weight:700}
footer{margin-top:48px;padding-top:22px;border-top:1px solid #1e1e21;
color:#63636a;font-size:13px}
"""


def _esc(value) -> str:
    """Escape before interpolating into the report.

    Everything in an audit finding originates from a third-party page we were
    asked to audit -- element snippets, header values, URLs. Injecting that
    into HTML unescaped would let an audited site write markup into a report
    its owner is about to read.
    """
    import html as _html

    return _html.escape(str(value), quote=True)


def _verdict(passed: bool) -> str:
    cls, label = ("ok", "PASS") if passed else ("bad", "ATTENTION NEEDED")
    return f'<span class="{cls}">{label}</span>'


def _findings_list(findings: list) -> str:
    if not findings:
        return '<ul><li>No issues found in this check.</li></ul>'
    rows = "".join(
        f'<li><span class="tag">{_esc(f.get("severity", "info"))}</span> '
        f'<b>{_esc(f.get("id", "finding"))}</b><br>{_esc(f.get("detail", ""))}</li>'
        for f in findings
    )
    return f"<ul>{rows}</ul>"


def _render_report(url: str, result: dict) -> str:
    wcag = result.get("wcag", {})
    violations = wcag.get("violations", [])
    wcag_rows = "".join(
        f'<li><span class="tag">{_esc(v.get("impact") or "unknown")}</span> '
        f'<b>{_esc(v.get("id", ""))}</b><br>{_esc(v.get("help", ""))} '
        f'&middot; {_esc(v.get("nodes_affected", 0))} element(s)</li>'
        for v in violations
    ) or "<li>No accessibility violations found in this snapshot.</li>"

    perf = result.get("performance", {})
    m = perf.get("metrics", {})
    seo = result.get("seo", {})
    sec = result.get("security", {})

    overall = all(
        section.get("pass") for section in (wcag, seo, sec, perf) if isinstance(section, dict)
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compliance report — {_esc(url)}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="robots" content="noindex">
<style>{_REPORT_CSS}</style></head><body><div class="w">
<h1>Site compliance report</h1>
<p class="sub">{_esc(url)}</p>

<div class="card"><div class="verdict"><span>Overall</span>{_verdict(overall)}</div>
<p style="color:#8d8d94;margin:0;font-size:14.5px">Four independent checks run against
the live page. Every result below is a deterministic rule, not an opinion.</p></div>

<h2>Accessibility — WCAG 2.1 A/AA</h2>
<div class="card"><div class="verdict"><span>axe-core</span>
{_verdict(bool(wcag.get("pass")))}</div><ul>{wcag_rows}</ul></div>

<h2>SEO</h2>
<div class="card"><div class="verdict"><span>Structure &amp; metadata</span>
{_verdict(bool(seo.get("pass")))}</div>{_findings_list(seo.get("findings", []))}</div>

<h2>Security headers</h2>
<div class="card"><div class="verdict"><span>Response headers</span>
{_verdict(bool(sec.get("pass")))}</div>{_findings_list(sec.get("findings", []))}</div>

<h2>Performance</h2>
<div class="card"><div class="verdict"><span>Single page load</span>
{_verdict(bool(perf.get("pass")))}</div>
<div class="metrics">
<div><b>{_esc(m.get("dom_node_count", "-"))}</b>DOM nodes</div>
<div><b>{_esc(round(m.get("total_bytes_transferred", 0) / 1000))} KB</b>transferred</div>
<div><b>{_esc(m.get("request_count", "-"))}</b>requests</div>
</div>{_findings_list(perf.get("findings", []))}</div>

<footer>Generated by HubVibe. These are narrow, automated checks — a meaningful
share of issues, not all of them. Automated scanning is not a compliance
certification and does not replace a manual accessibility audit.
Bookmark this page to return to the report.</footer>
</div></body></html>"""


def _render_report_error(url: str, detail: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report could not be generated</title>
<style>{_REPORT_CSS}</style></head><body><div class="w">
<h1>We couldn't complete this report</h1>
<p class="sub">{_esc(url)}</p>
<div class="card"><p style="margin:0;color:#8d8d94">The audit could not run against
that URL: {_esc(detail)}</p></div>
<footer>Your purchase still stands and nothing partial was saved — reload this page
to try again once the site is reachable. If it keeps failing, reply to your Stripe
receipt and we'll sort it out.</footer>
</div></body></html>"""


# --- MCP over Streamable HTTP ----------------------------------------------
#
# A real MCP endpoint, not the static /mcp.json manifest. The official MCP
# registry accepts remote servers via `remotes: [{type: "streamable-http"}]`,
# which needs a live endpoint at a public URL -- that is what this is, and it
# is what makes this node listable there without publishing a package.
#
# Implemented directly rather than with the `mcp` SDK on purpose: that package
# requires a newer Starlette than this service pins for FastAPI (which is why
# integrations/mcp_server.py has to be a standalone script). A tools-only MCP
# server over Streamable HTTP is just JSON-RPC 2.0 over POST, so hand-rolling
# the five methods avoids dragging an incompatible dependency into the
# deployed image.
#
# Shapes below were taken from the official SDK's own types rather than from
# memory, and the endpoint was driven with the real SDK client to confirm it.
#
# These are specifically the HANDSHAKE versions. The SDK's newest constant is
# 2026-07-28, but that is a "modern" version negotiated out-of-band and is NOT
# valid to return from initialize -- a client checks the initialize result
# against HANDSHAKE_PROTOCOL_VERSIONS and hard-errors on anything else. Echoing
# the newest constant here made the real client refuse to connect at all, so
# the list below is the handshake set, newest first.
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

# Input schemas are advertised to MCP clients, to the x402 Bazaar index, and
# in /.well-known/agent.json. They must describe EXACTLY what the routes
# accept: a schema stricter than the route makes a conforming client refuse
# to send a call that would have worked, and a schema looser than the route
# sends it into a 400 it paid nothing for but still burned a round trip on.
#
# `format: "uri"` is annotation-only in JSON Schema and the routes take a
# plain string, so it documents intent without inventing a constraint the
# server does not enforce. `additionalProperties` is deliberately left unset
# for the same reason -- the Pydantic models ignore unknown keys, so
# declaring `false` would advertise a rejection that never happens.
_MCP_URL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "title": "Live URL to audit",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "description": "Live, fetchable http(s) URL to audit.",
            "examples": ["https://example.com"],
        }
    },
    "required": ["url"],
}
# The either-or is a real route rule: both routes 400 on a body carrying
# neither field. Stating it as `anyOf` rather than leaving `required` off
# entirely is the difference between an agent knowing the constraint and
# discovering it by spending a call on a 400.
_MCP_HTML_OR_URL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "title": "Live URL or raw HTML to audit",
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "description": "Live, fetchable http(s) URL to audit.",
            "examples": ["https://example.com"],
        },
        "html": {
            "type": "string",
            "description": "Raw HTML source to audit instead of fetching a URL.",
        },
    },
    "anyOf": [{"required": ["url"]}, {"required": ["html"]}],
}

# Output schemas. MCP has carried `outputSchema` since 2025-06-18 and
# capability crawlers score on it, but the operational reason is narrower:
# without one, an agent cannot tell before paying whether this tool returns a
# shape its pipeline can consume. `pass` and `status` are the two fields every
# audit response carries and the two an automated caller actually branches on.
_FINDINGS_SCHEMA = {
    "type": "array",
    "description": "One entry per rule that did not pass. Empty when the check is clean.",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Stable rule identifier."},
            "severity": {"type": "string", "description": "Rule severity."},
            "detail": {"type": "string", "description": "What failed and where."},
        },
        "required": ["id"],
    },
}
_MCP_STATUS_PROPERTIES = {
    "status": {"type": "string", "const": "ok", "description": "Present only on a completed audit."},
    "pass": {"type": "boolean", "description": "Whether every rule in this dimension passed."},
}
_MCP_WCAG_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        **_MCP_STATUS_PROPERTIES,
        "engine": {"type": "string", "const": "axe-core"},
        "violations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "axe-core rule id."},
                    # Nullable because the value is `v.get("impact")`: axe-core
                    # sets it on every violation in practice, but the schema
                    # describes what the route can emit, and a strict client
                    # validating structuredContent against this would reject
                    # a paid, delivered audit over a null it was never told
                    # about.
                    "impact": {"type": ["string", "null"], "description": "axe-core impact level."},
                    "help": {"type": ["string", "null"]},
                    "help_url": {"type": "string", "format": "uri"},
                    "nodes_affected": {"type": "integer", "minimum": 0},
                },
                "required": ["id"],
            },
        },
    },
    "required": ["pass"],
}
_MCP_FINDINGS_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {**_MCP_STATUS_PROPERTIES, "findings": _FINDINGS_SCHEMA},
    "required": ["pass"],
}
_MCP_PERFORMANCE_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        **_MCP_STATUS_PROPERTIES,
        "metrics": {
            "type": "object",
            "properties": {
                "dom_node_count": {"type": "integer", "minimum": 0},
                "total_bytes_transferred": {"type": "integer", "minimum": 0},
                "request_count": {"type": "integer", "minimum": 0},
            },
        },
        "findings": _FINDINGS_SCHEMA,
    },
    "required": ["pass"],
}
_MCP_BUNDLE_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        **_MCP_STATUS_PROPERTIES,
        "wcag": _MCP_WCAG_OUTPUT_SCHEMA,
        "seo": _MCP_FINDINGS_OUTPUT_SCHEMA,
        "security": _MCP_FINDINGS_OUTPUT_SCHEMA,
        "performance": _MCP_PERFORMANCE_OUTPUT_SCHEMA,
    },
    "required": ["pass"],
}


# Output schema per sellable path. Keyed off the catalog rather than added
# to it because the schemas are defined below the catalog, and the catalog
# has to stay importable by the manifest routes that sit above them.
_MCP_OUTPUT_SCHEMAS = {
    "/audit/wcag": _MCP_WCAG_OUTPUT_SCHEMA,
    "/audit/seo": _MCP_FINDINGS_OUTPUT_SCHEMA,
    "/audit/security": _MCP_FINDINGS_OUTPUT_SCHEMA,
    "/audit/performance": _MCP_PERFORMANCE_OUTPUT_SCHEMA,
    "/audit/bundle": _MCP_BUNDLE_OUTPUT_SCHEMA,
}

# Human-facing tool titles. MCP clients show `title` in preference to `name`,
# and a crawler indexing capability reads it as the label.
_MCP_TOOL_TITLES = {
    "/audit/wcag": "Accessibility audit (WCAG 2.1 A/AA)",
    "/audit/seo": "SEO and metadata audit",
    "/audit/security": "Security header audit",
    "/audit/performance": "Page performance audit",
    "/audit/bundle": "Full site compliance bundle (all four audits)",
}

# Every tool here reads a third-party page and returns a report. Nothing it
# does mutates state on the caller's side or on the audited site, and the same
# URL audited twice returns the same verdict for the same page -- so
# readOnly/idempotent are accurate, and openWorld is accurate because the tool
# reaches an arbitrary external host. These are the hints an orchestrator uses
# to decide whether it may retry or parallelise a call without asking, so
# stating them is what lets an agent drive this node unattended.
_MCP_AUDIT_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _mcp_tools() -> list:
    """Tool list, derived from the same catalog the REST routes and the agent
    manifest use, so a tool can never advertise a price the route won't
    charge.

    Carries inputSchema, outputSchema, title and annotations because an agent
    deciding whether to spend money here has to answer three questions before
    it calls: what do I send, what comes back, and is it safe to retry. A tool
    that leaves any of them to prose is one an automated caller skips.
    """
    tools = []
    for entry in _CATALOG:
        path = entry["path"]
        name = "audit_" + path.rsplit("/", 1)[-1]
        tools.append(
            {
                "name": name,
                "title": _MCP_TOOL_TITLES[path],
                "description": (
                    f"{entry['description']} ${entry['price_usd']:.2f} per call. "
                    f"Returns: {entry['returns']}"
                ),
                "inputSchema": (
                    _MCP_URL_SCHEMA
                    if entry["input"] is _URL_INPUT_SCHEMA
                    else _MCP_HTML_OR_URL_SCHEMA
                ),
                "outputSchema": _MCP_OUTPUT_SCHEMAS[path],
                "annotations": dict(_MCP_AUDIT_ANNOTATIONS, title=_MCP_TOOL_TITLES[path]),
            }
        )
    for name, worker in _mcp_worker_tools().items():
        tools.append(
            {
                "name": name,
                "title": worker.title,
                "description": (
                    f"{worker.description} ${worker.price_usd:.2f} per call. "
                    f"Returns: {worker.returns}"
                ),
                "inputSchema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    **worker.input_schema,
                },
                # The route's whole 200 body: the same envelope openapi.json
                # documents, with this worker's own result schema inside it.
                "outputSchema": workers.catalog.response_schema(worker),
                "annotations": dict(_MCP_AUDIT_ANNOTATIONS, title=worker.title),
            }
        )
    return tools


# Worker-backed MCP tools: a /work catalog row sold under an MCP tool name.
# One row, so the tool and the route cannot quote different prices or
# schemas; tools/call hands the arguments to workers.router.serve, the same
# function the HTTP route runs, so the job is gated, billed, receipted and
# recorded once, the same way. Listed only while the worker is deliverable
# here (same fail-closed rule as /work).
_MCP_WORKER_TOOLS = {
    "hubvibe_predictive_probability_engine": "stats.probability",
}


def _mcp_worker_tools() -> dict:
    """{tool name: Worker} for the worker-backed tools this node can sell."""
    if workers is None:
        return {}
    found = {}
    for name, worker_name in _MCP_WORKER_TOOLS.items():
        worker = workers.catalog.BY_NAME.get(worker_name)
        if worker is not None and worker.available():
            found[name] = worker
    return found


def _mcp_tool_example(name: str) -> dict:
    """The example call the Bazaar record shows for a tool."""
    worker = _mcp_worker_tools().get(name)
    if worker is not None:
        return workers.catalog.example_for(worker)
    return {"url": "https://example.com"}


_MCP_TOOL_PRICES = {
    "audit_" + entry["path"].rsplit("/", 1)[-1]: entry["price_usd"] for entry in _CATALOG
}


def _mcp_run_tool(name: str, args: dict) -> dict:
    """Execute one audit tool. Assumes payment has already been authorised."""
    url = args.get("url")
    html = args.get("html")

    if name == "audit_wcag":
        raw = _run_axe(html, url)
        violations = raw.get("violations", [])
        return {
            "status": "ok",
            "pass": len(violations) == 0,
            "engine": "axe-core",
            "violations": [
                {
                    "id": v["id"],
                    "impact": v.get("impact"),
                    "help": v.get("help"),
                    "nodes_affected": len(v.get("nodes", [])),
                }
                for v in violations
            ],
        }
    if name == "audit_seo":
        return audits.run_seo_audit(html, url)
    if name == "audit_security":
        return audits.run_security_audit(url)
    if name == "audit_performance":
        return audits.run_performance_audit(url)
    if name == "audit_bundle":
        wcag_raw, performance = _run_axe_and_performance(url)
        violations = wcag_raw.get("violations", [])
        shared = audits.fetch_once(url)
        wcag = {
            "pass": len(violations) == 0,
            "violations": [
                {"id": v["id"], "impact": v.get("impact"), "help": v.get("help")}
                for v in violations
            ],
        }
        seo = audits.run_seo_audit(None, url, response=shared)
        security = audits.run_security_audit(url, response=shared)
        return {
            "status": "ok",
            "pass": all(r["pass"] for r in (wcag, seo, security, performance)),
            "wcag": wcag,
            "seo": seo,
            "security": security,
            "performance": performance,
        }
    raise KeyError(name)


def _jsonrpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _mcp_tool_error(request_id, message: str, details: Optional[dict] = None) -> dict:
    """A tool-level failure is a RESULT with isError, not a JSON-RPC error.

    JSON-RPC errors mean the protocol call itself was malformed; a payment
    requirement or an unreachable audit target is a normal outcome the model
    should see and can act on, so it belongs in the result.

    When there is machine-readable detail -- above all the 402 challenge,
    which carries the price and the rails that can settle it -- the text is
    the JSON itself with the sentence inside it, not a sentence with JSON
    stringified into the middle. An agent should be able to json.loads() the
    content and read `price_usd` and `accepts`, rather than substring-scrape
    a payment challenge out of prose. That prose-embedding is exactly what
    made this endpoint's paywall unusable to the machine buyers it exists
    for.
    """
    if details is None:
        text = message
    else:
        import json as _json

        text = _json.dumps({"message": message, **details}, indent=2)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """/mcp answers in JSON-RPC, even when the request never parsed.

    FastAPI's default is a 422 with a `detail` list -- a REST shape a
    JSON-RPC client cannot read; the official MCP client surfaces it as an
    opaque transport error. Invalid JSON is -32700 (parse error), anything
    else -32600 (invalid request). Every other route keeps the default.
    """
    if request.url.path != "/mcp":
        return await request_validation_exception_handler(request, exc)
    errors = exc.errors() if hasattr(exc, "errors") else []
    parse_error = any(e.get("type") == "json_invalid" for e in errors)
    code, message = (-32700, "Parse error: the body is not valid JSON") if parse_error else (
        -32600, "Invalid Request: expected a JSON-RPC request object"
    )
    return JSONResponse(status_code=400, content=_jsonrpc_error(None, code, message))


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """The last boundary: an exception nothing else caught still answers in
    machine-readable JSON, never Starlette's text/plain "Internal Server
    Error".

    The worker routes catch everything themselves (workers/router.py) and
    /mcp shapes its own errors, so in practice this is for the audit routes
    and the discovery surfaces. A crawler or a buying agent that gets a
    text body cannot tell a crashed node from a blocked one; a JSON body
    with `billed: false` says exactly what happened and that nothing was
    charged. The status stays 500 -- it IS a server fault -- and the
    exception is logged with its traceback so it is fixed, not hidden.
    """
    logging.getLogger(__name__).exception(
        "unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path
    )
    if request.url.path == "/mcp":
        return JSONResponse(
            status_code=500,
            content=_jsonrpc_error(None, -32603, f"Internal error: {type(exc).__name__}"),
        )
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "reason": "internal_error",
            "detail": f"{type(exc).__name__}: the node hit an unexpected fault serving this request.",
            "billed": False,
        },
    )


@app.post("/mcp", tags=["discovery"])
async def mcp_streamable_http(
    payload: Any = Body(...),
    request: Request = None,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """MCP Streamable HTTP endpoint.

    Discovery (initialize, tools/list) is free and unauthenticated -- an agent
    must be able to find out what this node sells and what it costs before
    deciding to buy. Execution (tools/call) goes through exactly the same
    fail-closed authorisation as the REST routes, including verify-then-settle,
    rather than a second copy of the payment logic that could drift from it.

    A coroutine, like every other discovery route: see _mcp_tools_call for
    why. The handshake and the tool list are answered on the event loop;
    only tools/call is handed to the audit thread pool.
    """
    # Shape first. A JSON array (the batch form MCP dropped in 2025-06-18), a
    # bare string, a params that is not an object: each of these used to
    # reach `.get` on the wrong type and come back as an HTTP 500 with a
    # text/plain body. A JSON-RPC client can act on -32600; it cannot act on
    # "Internal Server Error".
    if not isinstance(payload, dict):
        return _jsonrpc_error(
            None, -32600,
            "Invalid Request: expected one JSON-RPC request object (batches are not supported)",
        )
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params: `params` must be an object")

    # Notifications carry no id and must not be answered with a body.
    if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=202)

    if not isinstance(method, str):
        return _jsonrpc_error(request_id, -32600, "Invalid Request: `method` must be a string")

    if method == "initialize":
        client_version = (params or {}).get("protocolVersion")
        version = (
            client_version if client_version in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hubvibe-site-audit", "version": SERVICE_VERSION},
                "instructions": (
                    "Rule-based site compliance audits and a deterministic "
                    "statistics engine. Every tool costs money and returns a "
                    "deterministic result, never an LLM's opinion. Calls "
                    "must be paid for; this deployment currently settles: "
                    f"{', '.join(_payment_methods_live()) or 'no rail is configured'}"
                    f" -- see {PUBLIC_BASE_URL}/.well-known/agent.json and the 402 "
                    "challenge for how to pay. A tool that cannot run reports an "
                    "error and is not charged for."
                ),
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _mcp_tools()}}

    if method == "tools/call":
        from starlette.concurrency import run_in_threadpool

        if (params or {}).get("name") in _MCP_WORKER_TOOLS:
            # A worker job runs on the event loop with its own limiter, like
            # its HTTP route -- never in the audit thread pool, whose two
            # slots on the box are what the browser audits wait on.
            return await _mcp_worker_tool_call(
                payload, request, x_api_key, x_payment, authorization
            )
        return await run_in_threadpool(
            _mcp_tools_call, payload, request, x_api_key, x_payment, authorization
        )

    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


def _mcp_tools_call(
    payload: dict,
    request: Request,
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
):
    """Execute one paid tool call. Runs in the audit thread pool, never on
    the event loop.

    Why the split: MAX_CONCURRENT_AUDITS caps anyio's thread pool, and every
    SYNC route handler runs in that pool -- so while four audits held their
    Chromium contexts, `/health`, `/.well-known/agent.json`, `/mcp.json` and
    the MCP handshake queued behind them. Cloud Run's health probe, a crawler
    scoring the manifest, and an agent's `initialize` all waited on a
    stranger's page load, and a probe that times out is an instance marked
    unhealthy at exactly the moment it is earning. Discovery is pure CPU on
    static data; it belongs on the event loop. Only this -- the part that
    runs a browser and waits on the facilitator -- belongs in the pool.
    """
    request_id = payload.get("id")
    params = payload.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params: `arguments` must be an object")
    for field in ("url", "html"):
        if args.get(field) is not None and not isinstance(args[field], str):
            return _jsonrpc_error(
                request_id, -32602, f"Invalid params: `{field}` must be a string"
            )

    price = _MCP_TOOL_PRICES.get(name)
    if price is None:
        return _mcp_tool_error(request_id, f"Unknown tool: {name}")
    if not args.get("url") and not args.get("html"):
        return _mcp_tool_error(request_id, "Provide 'url' (or 'html' for wcag/seo).")
    # Same gates as the REST routes, before any payment is read: a URL
    # this service will not fetch, or a body no browser would render.
    html_arg = args.get("html")
    if isinstance(html_arg, str) and len(html_arg) > MAX_HTML_BYTES:
        return _mcp_tool_error(
            request_id,
            f"'html' is {len(html_arg)} bytes; the limit is {MAX_HTML_BYTES}. "
            "Nothing was charged.",
        )
    url_problem = _target_url_problem(args.get("url"))
    if url_problem is not None:
        return _mcp_tool_error(
            request_id, f"'url' {url_problem}. Nothing was charged."
        )

    # The x402 MCP transport carries the payment INSIDE the JSON-RPC
    # call, as `params._meta["x402/payment"]` -- an MCP client has no
    # access to HTTP headers at all, so a header was never something it
    # could send. Re-encoded into the header form so the one verify path
    # (nonce ledger, facilitator loop, logging) serves both transports;
    # an explicit header still wins when a caller sends both.
    meta_payment = x402_payments.payment_header_from_meta(
        (params.get("_meta") or {}).get(x402_payments.MCP_PAYMENT_META_KEY)
        if isinstance(params.get("_meta"), dict)
        else None
    )

    auth, err = _authorize_and_rate_limit(
        x_api_key, x_payment or meta_payment, authorization, request, price_usd=price
    )
    if err is not None:
        if err.status_code == 429:
            # Over the limit is not "pay me": an x402 MCP client answered
            # with a challenge signs a payment and retries once, gets the
            # same challenge, and reports its wallet as refused. Tell it to
            # wait, with the same Retry-After the REST route sends.
            envelope = _mcp_tool_error(
                request_id,
                f"Rate limit exceeded ({RATE_LIMIT_PER_MINUTE} requests/minute). "
                "Nothing was charged. Retry after 60 seconds.",
                {"error": "rate_limited", "retry_after_seconds": 60, "billed": False},
            )
            return JSONResponse(content=envelope, headers={"Retry-After": "60"})
        return _mcp_payment_required(request_id, name, price, err)

    try:
        result = _mcp_run_tool(name, args)
    except Exception as exc:
        # Not billed: _bill only runs on success, same as the REST routes,
        # and whatever authentication already took is handed back.
        _unbill_failed_audit(auth)
        details = {"billed": False}
        _attach_issued_key(details, auth)
        return _mcp_tool_error(
            request_id, f"Audit could not complete: {exc}. Nothing was charged.", details
        )

    warning = _bill(auth, price_usd=price)
    refused = _settlement_refused(auth)
    if refused is not None:
        # Same rule as the REST routes: a refused settle withholds the audit
        # and re-issues the paywall, here in the shape the MCP client pays.
        return _mcp_payment_required(request_id, name, price, refused)
    if warning:
        result["billing_warning"] = warning
    _attach_issued_key(result, auth)

    import json as _json

    # `structuredContent` is not optional here. Every tool advertises an
    # outputSchema, and the official MCP SDK client (mcp >= 1.10) enforces
    # the spec's consequence on every non-error result: a tool with an
    # output schema that returns no structuredContent raises RuntimeError in
    # the CLIENT, after the call. On a paid call that is the worst order of
    # events this node can produce -- the payment settled, the audit ran,
    # and the agent's SDK threw the result away on delivery. The dict IS the
    # structured result; the text is the same JSON for clients that only
    # read content.
    tool_result = {
        "content": [{"type": "text", "text": _json.dumps(result, indent=2)}],
        "structuredContent": result,
        "isError": False,
    }
    # The settlement receipt, where the x402 MCP client reads it:
    # `_meta["x402/payment-response"]` on the CallToolResult. The HTTP
    # header copy below still goes out for clients that can see headers.
    receipt = x402_payments.receipt_meta(getattr(auth, "pending_payment", None))
    if receipt:
        tool_result["_meta"] = receipt
    envelope = {"jsonrpc": "2.0", "id": request_id, "result": tool_result}
    return _with_receipt(envelope, auth)


async def _mcp_worker_tool_call(
    payload: dict,
    request: Request,
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
):
    """tools/call for a worker-backed tool: the route's own serve() with the
    tool arguments as the body, and its answer re-shaped for JSON-RPC.

    The route decides everything -- availability, pre-payment validation,
    the gate, the run, billing, the receipt, the ledger row -- and this only
    translates its HTTP outcome: 200 becomes a structured result, 402 the
    v2 paywall the x402 MCP client pays, 429 the wait-and-retry result, and
    every refusal or failure an isError result that says nothing was charged.
    """
    import json as _json

    request_id = payload.get("id")
    params = payload.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params: `arguments` must be an object")

    worker = _mcp_worker_tools().get(name)
    if worker is None or not workers.is_configured():
        return _mcp_tool_error(
            request_id, f"{name} is not available on this deployment. Nothing was charged.",
            {"billed": False},
        )
    price = worker.price_usd

    # Same as the audit tools: the payment rides inside the JSON-RPC call.
    meta_payment = x402_payments.payment_header_from_meta(
        (params.get("_meta") or {}).get(x402_payments.MCP_PAYMENT_META_KEY)
        if isinstance(params.get("_meta"), dict)
        else None
    )

    sink: dict = {}
    served = await workers.router.serve(
        worker, args, request, x_api_key, x_payment or meta_payment, authorization,
        sink=sink,
    )
    auth = sink.get("auth")
    if isinstance(served, dict):
        status, body = 200, served
    else:
        status = served.status_code
        try:
            body = _json.loads(bytes(served.body).decode())
        except Exception:  # pragma: no cover - the route always writes JSON
            body = {"status": "error", "detail": "unreadable response"}

    if status == 200:
        tool_result = {
            "content": [{"type": "text", "text": _json.dumps(body, indent=2)}],
            "structuredContent": body,
            "isError": False,
        }
        receipt = x402_payments.receipt_meta(getattr(auth, "pending_payment", None))
        if receipt:
            tool_result["_meta"] = receipt
        envelope = {"jsonrpc": "2.0", "id": request_id, "result": tool_result}
        return _with_receipt(envelope, auth)
    if status == 402:
        return _mcp_payment_required(request_id, name, price, served)
    if status == 429:
        envelope = _mcp_tool_error(
            request_id,
            f"Rate limit exceeded ({RATE_LIMIT_PER_MINUTE} requests/minute). "
            "Nothing was charged. Retry after 60 seconds.",
            {"error": "rate_limited", "retry_after_seconds": 60, "billed": False},
        )
        return JSONResponse(content=envelope, headers={"Retry-After": "60"})
    detail = body.get("detail") or f"{name} could not run."
    details = {k: v for k, v in body.items() if k != "detail"}
    details.setdefault("billed", False)
    details["http_status"] = status
    return _mcp_tool_error(request_id, f"{detail} Nothing was charged.", details)


def _mcp_payment_required(request_id, name: str, price: float, err: JSONResponse) -> dict:
    """The MCP paywall, in the shape the x402 MCP client actually pays.

    The x402 MCP protocol (x402.mcp, server and client alike) is precise
    about this and it is NOT the HTTP 402 shape: the tool result is
    `isError: true` with a **v2** `PaymentRequired` object in
    `structuredContent` (and the same JSON as the text content). The client
    reads `structuredContent` first, parses it with the library's own
    `parse_payment_required`, signs for the first entry in `accepts`, and
    retries with the payload in `params._meta["x402/payment"]`.

    What this endpoint served before was the REST 402 body -- a v1 object
    with a v1 `accepts[]` -- as prose-free JSON text. A v2 MCP client either
    parsed that as v1 and signed a v1 payment, or found no v2 challenge at
    all; and whatever it signed was sent in `_meta`, which nothing here read.
    Every conforming x402 MCP client was answered with the same paywall
    twice and gave up, and from this side that is indistinguishable from no
    MCP agent ever calling. The same shape of fault as the unpayable REST
    402 (#61), one transport over.

    The v2 object is the same challenge the HTTP path encodes into the
    PAYMENT-REQUIRED header, built by the same function, so the price, the
    recipient and the network cannot differ between the two transports.
    `resource.url` is this node's MCP endpoint -- what an agent that finds the
    tool in the Bazaar connects to -- and the discovery record names the
    tool and its transport, so it is indexed as an MCP resource rather than
    as the HTTP route the REST 402 would have described.

    The extra keys (`message`, `price_usd`, `other_rails`, `docs`) are kept
    for an LLM reading the result: the x402 models ignore unknown fields, so
    they cost the paying client nothing. When x402 is off there is no v2
    object to send; the REST body (with its empty `accepts`) goes out
    unchanged, which the client correctly reads as "nothing here I can pay".
    """
    import json as _json

    try:
        rest_body = _json.loads(err.body.decode())
    except Exception:
        # Never let a formatting problem turn a payment prompt into a
        # crash: the caller still needs to know what it costs.
        rest_body = {"error": "payment_required", "price_usd": price}

    tool = next((t for t in _mcp_tools() if t["name"] == name), None)
    mcp_bazaar = {}
    if tool is not None:
        mcp_bazaar = x402_payments.bazaar_extension_for_mcp_tool(
            tool_name=name,
            description=tool["description"],
            input_schema=tool["inputSchema"],
            example=_mcp_tool_example(name),
        )

    # The REST body's `error` is "payment_required" on a fresh challenge and
    # the refusal reason when a payment was tried; carry it into the v2
    # object so an MCP payer learns why exactly as an HTTP payer does.
    rest_error = rest_body.get("error") if isinstance(rest_body, dict) else None
    challenge = x402_payments.payment_required_v2_dict(
        price=f"${price:.2f}",
        resource_url=f"{PUBLIC_BASE_URL}/mcp",
        description=tool["description"] if tool is not None else _route_description(None),
        extensions=mcp_bazaar or None,
        error=rest_error if rest_error and rest_error != "payment_required" else None,
    )
    if challenge:
        # v2 wins the keys both objects carry (x402Version, accepts, error,
        # extensions); the REST body contributes the human-facing rest.
        for key, value in rest_body.items():
            if key not in ("x402Version", "accepts", "extensions", "error", "resource"):
                challenge.setdefault(key, value)
    else:
        challenge = rest_body
        if mcp_bazaar:
            challenge["extensions"] = mcp_bazaar

    # Rails an MCP caller can actually use. The MPP rails are paid through
    # HTTP headers (`Authorization: Payment`, challenge in WWW-Authenticate)
    # that a tool result cannot carry, so listing them here names a rail
    # this transport cannot settle. The API-key rail rides the JSON-RPC
    # request's own X-API-Key header and stays.
    if isinstance(challenge.get("other_rails"), list):
        challenge["other_rails"] = [
            rail for rail in challenge["other_rails"]
            if not (isinstance(rail, dict) and rail.get("protocol") == "mpp")
        ]

    message = (
        f"Payment required (${price:.2f} for {name}). Attach X-API-Key, "
        f"or pay per call with a rail listed in `accepts`."
    )
    challenge = {"message": message, **challenge}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": _json.dumps(challenge, indent=2)}],
            "structuredContent": challenge,
            "isError": True,
        },
    }


@app.post("/billing/checkout")
def start_checkout(payload: CheckoutRequest, request: Request):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    # Default to this same deployment's own success/cancel pages so the
    # funnel works out of the box; override via env vars only if the
    # public-facing URL differs (e.g. a custom domain in front of Cloud Run).
    base = str(request.base_url).rstrip("/")
    success_url = os.environ.get("CHECKOUT_SUCCESS_URL", f"{base}/billing/success")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL", f"{base}/billing/cancel")
    try:
        checkout_url = billing.create_checkout_session(
            payload.email, success_url, cancel_url, plan=payload.plan
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"checkout_url": checkout_url}


@app.post("/billing/report", tags=["billing"])
def start_report_checkout(payload: ReportCheckoutRequest, request: Request):
    """One-time purchase of a single full-bundle report on one URL."""
    if not billing.oneoff_report_available():
        raise HTTPException(
            status_code=501, detail="One-off reports are not configured on this deployment"
        )
    base = str(request.base_url).rstrip("/")
    success_url = os.environ.get("REPORT_SUCCESS_URL", f"{base}/report")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL", f"{base}/billing/cancel")
    try:
        checkout_url = billing.create_report_checkout(
            payload.email, payload.url, success_url, cancel_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"checkout_url": checkout_url}


@app.get("/report", response_class=HTMLResponse, tags=["billing"])
def report_page(session_id: str):
    """Render a purchased report.

    Payment is re-verified against Stripe on every view rather than trusting
    that a webhook landed, because this URL is the only thing between a
    stranger and a free audit -- an unpaid or unknown session gets nothing.

    The audit runs on first view and is then cached, so a refresh re-reads
    the stored result instead of re-running (and re-costing) an audit the
    buyer already paid for exactly once.
    """
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")

    order = billing.paid_report_request(session_id)
    if order is None:
        # Deliberately identical for unpaid, unknown, and malformed sessions:
        # no oracle for probing which session IDs exist.
        raise HTTPException(status_code=404, detail="No paid report found for that session")

    cached = billing.load_report(session_id)
    if cached and cached.get("result"):
        return HTMLResponse(_render_report(cached["url"], cached["result"]))

    url = order["url"]
    try:
        wcag_raw, performance_result = _run_axe_and_performance(url)
        wcag_violations = wcag_raw.get("violations", [])
        shared_response = audits.fetch_once(url)
        result = {
            "wcag": {
                "pass": len(wcag_violations) == 0,
                "violations": [
                    {
                        "id": v["id"],
                        "impact": v.get("impact"),
                        "help": v.get("help"),
                        "nodes_affected": len(v.get("nodes", [])),
                    }
                    for v in wcag_violations
                ],
            },
            "seo": audits.run_seo_audit(None, url, response=shared_response),
            "security": audits.run_security_audit(url, response=shared_response),
            "performance": performance_result,
        }
    except Exception as exc:
        # They paid and we could not deliver. Say so plainly and tell them
        # the purchase still stands -- the report is cached only on success,
        # so a retry re-runs rather than serving a broken result forever.
        return HTMLResponse(
            _render_report_error(url, str(exc)),
            status_code=502,
        )

    billing.save_report(session_id, url, result)
    return HTMLResponse(_render_report(url, result))


@app.get("/billing/api-key")
def get_api_key(session_id: str):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    api_key = billing.api_key_for_session(session_id)
    if api_key is None:
        # The webhook that mints the key may not have landed yet -- this is
        # a normal, expected state right after checkout, not an error.
        return JSONResponse(status_code=202, content={"status": "pending"})
    return {"api_key": api_key}


@app.post("/billing/webhook")
async def stripe_webhook(request: Request):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = billing.verify_webhook(payload, sig_header)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {exc}")
    if event["type"] == "checkout.session.completed":
        billing.activate_customer(event["data"]["object"])
    return {"received": True}


def _mpp_realm(request: Request) -> Optional[str]:
    """MPP realm SHOULD be the server's bare hostname -- strip the port off
    the Host header (":8811" locally, absent behind Cloud Run's HTTPS
    frontend, but strip it either way rather than depend on that)."""
    host = request.headers.get("host")
    return host.split(":")[0] if host else None


@app.post("/audit")
def audit(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    # Before any payment is read: a body with nothing to audit is refused for
    # free. Checked after the facilitator, this 400 burned a verify round
    # trip and the signature's nonce, so the payer's corrected retry with the
    # same authorization was refused as a replay.
    missing = _reject_missing_input(payload)
    if missing is not None:
        return missing
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit"))
    if err:
        return err

    try:
        raw = _run_axe(payload.html, payload.url)
    except Exception as exc:
        # Honest failure: an audit that didn't run is never reported as a
        # compliance pass, and it is never billed -- callers only pay for
        # an audit that actually happened.
        return _failed_audit_response(auth, f"Audit could not complete: {exc}")

    violations = raw.get("violations", [])
    result = {
        "status": "ok",
        "pass": len(violations) == 0,
        "engine": "axe-core",
        "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
        "violations": [
            {
                "id": v["id"],
                "impact": v.get("impact"),
                "help": v.get("help"),
                "help_url": v.get("helpUrl"),
                "nodes_affected": len(v.get("nodes", [])),
            }
            for v in violations
        ],
    }

    warning = _bill(auth, price_usd=_price_of("/audit"))
    if warning:
        result["billing_warning"] = warning
    remediation = _remediation_notes(violations)
    if remediation is not None:
        result["remediation"] = remediation
    return _deliver(result, auth)


@app.post("/audit/wcag")
def audit_wcag(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Identical to /audit -- same axe-core check, same price, kept
    as its own path alongside the other 4 audit dimensions so a caller can
    request accessibility specifically without relying on /audit's name."""
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    # Before any payment is read: a body with nothing to audit is refused for
    # free. Checked after the facilitator, this 400 burned a verify round
    # trip and the signature's nonce, so the payer's corrected retry with the
    # same authorization was refused as a replay.
    missing = _reject_missing_input(payload)
    if missing is not None:
        return missing
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit/wcag"))
    if err:
        return err

    try:
        raw = _run_axe(payload.html, payload.url)
    except Exception as exc:
        return _failed_audit_response(auth, f"Audit could not complete: {exc}")

    violations = raw.get("violations", [])
    result = {
        "status": "ok",
        "pass": len(violations) == 0,
        "engine": "axe-core",
        "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
        "violations": [
            {
                "id": v["id"],
                "impact": v.get("impact"),
                "help": v.get("help"),
                "help_url": v.get("helpUrl"),
                "nodes_affected": len(v.get("nodes", [])),
            }
            for v in violations
        ],
    }
    warning = _bill(auth, price_usd=_price_of("/audit/wcag"))
    if warning:
        result["billing_warning"] = warning
    return _deliver(result, auth)


@app.post("/audit/seo")
def audit_seo(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    # Before any payment is read: a body with nothing to audit is refused for
    # free. Checked after the facilitator, this 400 burned a verify round
    # trip and the signature's nonce, so the payer's corrected retry with the
    # same authorization was refused as a replay.
    missing = _reject_missing_input(payload)
    if missing is not None:
        return missing
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit/seo"))
    if err:
        return err

    try:
        result = audits.run_seo_audit(payload.html, payload.url)
    except Exception as exc:
        return _failed_audit_response(auth, f"Audit could not complete: {exc}")

    warning = _bill(auth, price_usd=_price_of("/audit/seo"))
    if warning:
        result["billing_warning"] = warning
    return _deliver(result, auth)


@app.post("/audit/security")
def audit_security(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit/security"))
    if err:
        return err

    try:
        result = audits.run_security_audit(payload.url)
    except Exception as exc:
        return _failed_audit_response(auth, f"Audit could not complete: {exc}")

    warning = _bill(auth, price_usd=_price_of("/audit/security"))
    if warning:
        result["billing_warning"] = warning
    return _deliver(result, auth)


@app.post("/audit/performance")
def audit_performance(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit/performance"))
    if err:
        return err

    try:
        result = audits.run_performance_audit(payload.url)
    except Exception as exc:
        return _failed_audit_response(auth, f"Audit could not complete: {exc}")

    warning = _bill(auth, price_usd=_price_of("/audit/performance"))
    if warning:
        result["billing_warning"] = warning
    return _deliver(result, auth)


@app.post("/audit/bundle")
def audit_bundle(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Runs all four audits against one URL. Priced and billed as a single
    one unit, not four separate single-audit charges -- if any dimension fails
    to run, the whole call fails (502) and nothing is billed, since a
    partial bundle isn't the product being sold here."""
    refused = _reject_unfetchable_target(payload.url)
    if refused is not None:
        return refused
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=_price_of("/audit/bundle"))
    if err:
        return err

    try:
        # Two fetches of the target, not four: one rendered page load feeding
        # both browser-based checks, and one HTTP GET feeding both
        # response-based checks. See _run_axe_and_performance.
        wcag_raw, performance_result = _run_axe_and_performance(payload.url)
        wcag_violations = wcag_raw.get("violations", [])
        wcag_result = {
            "status": "ok",
            "pass": len(wcag_violations) == 0,
            "engine": "axe-core",
            "violations": [
                {
                    "id": v["id"],
                    "impact": v.get("impact"),
                    "help": v.get("help"),
                    "help_url": v.get("helpUrl"),
                    "nodes_affected": len(v.get("nodes", [])),
                }
                for v in wcag_violations
            ],
        }
        shared_response = audits.fetch_once(payload.url)
        seo_result = audits.run_seo_audit(None, payload.url, response=shared_response)
        security_result = audits.run_security_audit(payload.url, response=shared_response)
    except Exception as exc:
        return _failed_audit_response(auth, f"Bundle audit could not complete: {exc}")

    result = {
        "status": "ok",
        "pass": all(
            r["pass"] for r in (wcag_result, seo_result, security_result, performance_result)
        ),
        "wcag": wcag_result,
        "seo": seo_result,
        "security": security_result,
        "performance": performance_result,
    }
    warning = _bill(auth, price_usd=_price_of("/audit/bundle"))
    if warning:
        result["billing_warning"] = warning
    return _deliver(result, auth)


# --- worker network ---------------------------------------------------------
#
# Mounted LAST, after every audit route is registered, so nothing here can
# shadow a path the audits already serve.
#
# The four functions handed over are this module's OWN payment gate -- the
# same ones every audit route calls, in the same order. The worker package
# imports no payment code of its own and cannot reach x402_payments; there is
# one settlement implementation in this service and this is it.
#
# browser_pool.with_page and audits.goto_guarded are passed too, so a worker
# that needs a rendered page reuses the warm Chromium the audits already keep
# (and the same SSRF/redirect guard), rather than launching a second browser.
if workers is not None:
    try:
        workers.configure(
            authorize_and_rate_limit=_authorize_and_rate_limit,
            node_version=SERVICE_VERSION,
            mpp_payment_facts=mpp_payments.settlement_for,
            bill=_bill,
            deliver=_deliver,
            failed_response=_failed_audit_response,
            with_page=browser_pool.with_page,
            goto_guarded=getattr(audits, "goto_guarded", None),
            # The SAME rule the audit routes refuse targets with. Injected
            # rather than reimplemented so a worker can never fetch something
            # an audit would refuse -- one guard, one place to fix it.
            blocked_target_reason=audits.blocked_target_reason,
        )
        app.include_router(workers.router.router)
    except Exception as _workers_mount_error:  # pragma: no cover - defensive
        logging.getLogger("hubvibe").warning(
            "worker network not mounted, audits unaffected: %s", _workers_mount_error)
