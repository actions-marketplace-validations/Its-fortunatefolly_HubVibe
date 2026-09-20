"""Drop-in HubVibe audit client for autonomous agents, with automatic
per-call x402 payment.

This is the piece `langchain_tool.py` and `mcp_server.py` deliberately left
out: those two only speak X-API-Key and raise on HTTP 402. An agent running
unattended cannot go get a subscription key, so this module lets it settle
the 402 itself from a wallet and continue, with no human in the loop.

    from hubvibe_tollbooth import HubVibeTollbooth

    booth = HubVibeTollbooth.from_env()
    result = booth.audit("https://example.com")     # pays $0.15 if needed
    if not result["pass"]:
        ...

LangChain / CrewAI:

    from hubvibe_tollbooth import hubvibe_tools
    agent = initialize_agent(tools=hubvibe_tools(), ...)

Configuration (environment)
---------------------------
Pick ONE auth path:

  HUBVIBE_API_KEY          a subscription key -- cheapest if you already have
                           one, and no wallet is involved.

  HUBVIBE_WALLET_KEY       an EVM private key (0x...) used to sign x402
                           payments per call. Fund it with USDC on Base.

Optional:
  HUBVIBE_BASE_URL         default https://hubvibe-io.com
  HUBVIBE_MAX_PRICE_USD    per-call ceiling, default 0.25
  HUBVIBE_BUDGET_USD       total this process may ever spend, default 5.00

Spending limits are not optional decoration
-------------------------------------------
Handing an LLM-driven loop a funded wallet with no ceiling is how an agent
bug becomes a drained wallet. Two independent limits apply, both enforced
before any signature is produced:

  * a per-call cap, passed down to x402's own `max_amount` policy, so a
    payment above it is never signed even if the server asks for it; and
  * a process-lifetime budget tracked here, so a retry loop that would
    individually pass the per-call cap still cannot run away.

Both fail closed: exceeding either raises `BudgetExceeded` instead of paying.
Neither can be disabled by passing None -- an unbounded default is the one
configuration this must not make easy.
"""

from __future__ import annotations

import inspect
import os
import threading
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://hubvibe-io.com"

# USDC on Base has 6 decimals. x402's max_amount policy works in atomic
# units, so dollars have to be converted before it can be applied.
_USDC_DECIMALS = 6

# Published rates. Used only to pre-check the budget and to describe the tools
# to an LLM -- the authoritative price is always the one in the 402 challenge,
# and that is what actually gets paid.
PRICES_USD = {
    "wcag": 0.05,
    "seo": 0.05,
    "security": 0.05,
    "performance": 0.05,
    "bundle": 0.15,
}


class HubVibeError(RuntimeError):
    """Any failure that stopped an audit from being delivered."""


class PaymentNotConfigured(HubVibeError):
    """The server asked for payment and this client has no way to pay."""


class BudgetExceeded(HubVibeError):
    """A payment was refused locally because it would breach a limit."""


def _usd_to_atomic(usd: float) -> int:
    return int(round(usd * (10**_USDC_DECIMALS)))


class HubVibeTollbooth:
    """A HubVibe audit client that settles its own 402s.

    Thread-safe: the budget counter is mutex-guarded so a fan-out of worker
    threads sharing one instance cannot race past the ceiling.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        wallet_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        max_price_usd: float = 0.25,
        budget_usd: float = 5.00,
        timeout: float = 90.0,
    ) -> None:
        if max_price_usd <= 0 or budget_usd <= 0:
            raise ValueError("max_price_usd and budget_usd must both be positive")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_price_usd = max_price_usd
        self.budget_usd = budget_usd
        self.timeout = timeout

        self._spent_usd = 0.0
        self._lock = threading.Lock()
        self._http_client = None
        # The facilitator's settle response for the most recent paid call,
        # decoded from the PAYMENT-RESPONSE header: transaction hash, network,
        # payer. None until a call has been paid, and None again if the server
        # sent no receipt. This is the only on-chain reference the payer gets.
        self.last_settlement: Optional[dict] = None

        if wallet_key:
            self._http_client = self._build_x402_client(wallet_key, max_price_usd)

    @property
    def spent_usd(self) -> float:
        """Total actually paid via x402 by this instance, in USD."""
        with self._lock:
            return self._spent_usd

    @classmethod
    def from_env(cls, **overrides: Any) -> "HubVibeTollbooth":
        """Build from the documented environment variables.

        Raises if neither auth path is configured: a client that can neither
        authenticate nor pay will 402 on its first call, and failing at
        construction makes that a deploy-time error instead of a runtime one
        somewhere deep inside an agent loop.
        """
        api_key = os.environ.get("HUBVIBE_API_KEY") or None
        wallet_key = os.environ.get("HUBVIBE_WALLET_KEY") or None
        if not api_key and not wallet_key:
            raise PaymentNotConfigured(
                "Set HUBVIBE_API_KEY (subscription) or HUBVIBE_WALLET_KEY "
                "(x402 per-call payment) before constructing HubVibeTollbooth."
            )
        kwargs: dict = {
            "api_key": api_key,
            "wallet_key": wallet_key,
            "base_url": os.environ.get("HUBVIBE_BASE_URL", DEFAULT_BASE_URL),
            "max_price_usd": float(os.environ.get("HUBVIBE_MAX_PRICE_USD", "0.25")),
            "budget_usd": float(os.environ.get("HUBVIBE_BUDGET_USD", "5.00")),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @staticmethod
    def _build_x402_client(wallet_key: str, max_price_usd: float):
        """Wire up x402's sync HTTP client with a spend policy.

        Imported lazily so that the API-key path -- which most callers will
        use -- does not require the x402 and eth-account dependency tree to be
        installed at all.
        """
        try:
            from eth_account import Account
            from x402 import max_amount, x402ClientSync
            from x402.http import x402HTTPClientSync
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact import register_exact_evm_client
        except ImportError as exc:
            raise PaymentNotConfigured(
                "x402 payment needs the client extras: "
                "pip install 'x402[evm]' eth-account"
            ) from exc

        account = Account.from_key(wallet_key)
        client = x402ClientSync()
        register_exact_evm_client(
            client,
            EthAccountSigner(account),
            policies=[max_amount(_usd_to_atomic(max_price_usd))],
        )
        return x402HTTPClientSync(client)

    def _reserve(self, price_usd: float) -> None:
        """Take `price_usd` out of the remaining budget, or raise.

        Reserved before signing rather than recorded after settlement: a
        payment that is signed and sent has left the building whether or not
        this process ever sees the response, so it must be counted first.
        """
        if price_usd > self.max_price_usd:
            raise BudgetExceeded(
                f"The server asked for ${price_usd:.4f}, above the per-call cap "
                f"of ${self.max_price_usd:.2f}. Nothing was paid."
            )
        with self._lock:
            if self._spent_usd + price_usd > self.budget_usd:
                raise BudgetExceeded(
                    f"Paying ${price_usd:.4f} would exceed this client's "
                    f"${self.budget_usd:.2f} budget (${self._spent_usd:.4f} already "
                    "spent). Nothing was paid."
                )
            self._spent_usd += price_usd

    def _refund(self, price_usd: float) -> None:
        """Give budget back when a reserved payment was never actually made."""
        with self._lock:
            self._spent_usd = max(0.0, self._spent_usd - price_usd)

    def _sign_402(self, response: httpx.Response, request_url: str):
        """Turn a 402 into payment headers, across x402 client versions.

        x402 2.21 added a REQUIRED third parameter, `request_url`, to
        `handle_402_response`. 2.18 -- the version this repo pinned until
        2026-09-04 (2.22 now) -- does not accept it. Calling with the wrong
        arity raises TypeError *before* any signature exists, so the caller
        sees "could not construct an x402 payment" with nothing on-chain to
        inspect and no facilitator involved: the exact silent-bounce shape
        that made revenue look like absent demand in #61.

        The pin protects the service, but not the two places that matter most
        here. `scripts/first-paid-call.sh` shells out to bare `python3`, which
        resolves to whatever x402 the machine has rather than the pinned one --
        and this module ships to agent authors who install x402 themselves.
        Both must work on either version, so the arity is read off the
        installed callable instead of assumed.

        Introspection rather than `except TypeError`: a TypeError raised from
        *inside* the library would otherwise be silently retried with different
        arguments and reported as an arity problem.
        """
        handler = self._http_client.handle_402_response
        args = [dict(response.headers), response.content]
        try:
            takes_url = "request_url" in inspect.signature(handler).parameters
        except (TypeError, ValueError):
            # A callable with no introspectable signature (a C function, some
            # mocks). Fall back to the two-argument (<= 2.20) arity rather
            # than guess.
            takes_url = False
        if takes_url:
            args.append(request_url)
        return handler(*args)

    @staticmethod
    def _challenge_price_usd(body: Any, fallback: float) -> float:
        """The price the 402 actually asked for, in USD.

        The service quotes `"$0.05"`. Anything unparseable falls back to the
        published rate for the route rather than to zero -- a price of zero
        would silently defeat both spending limits.
        """
        price = None
        if isinstance(body, dict):
            price = body.get("price")
            if price is None:
                accepts = body.get("accepts")
                if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
                    price = accepts[0].get("price")
        if isinstance(price, (int, float)):
            return float(price)
        if isinstance(price, str):
            try:
                return float(price.strip().lstrip("$"))
            except ValueError:
                pass
        return fallback

    def _post(self, path: str, payload: dict, price_hint: float) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        with httpx.Client(timeout=self.timeout) as http:
            response = http.post(f"{self.base_url}{path}", json=payload, headers=headers)

            if response.status_code != 402:
                return self._unwrap(response)

            # Paid path. Everything below runs only when the server has
            # actually challenged us.
            if self._http_client is None:
                raise PaymentNotConfigured(
                    f"HubVibe returned 402 for {path} and no wallet is configured. "
                    "Set HUBVIBE_WALLET_KEY to pay per call, or HUBVIBE_API_KEY to "
                    f"use a subscription. Challenge: {self._safe_json(response)}"
                )

            price_usd = self._challenge_price_usd(self._safe_json(response), price_hint)
            self._reserve(price_usd)
            try:
                pay_headers, _payload = self._sign_402(
                    response, f"{self.base_url}{path}"
                )
            except Exception as exc:
                # No signature was produced, so no money moved.
                self._refund(price_usd)
                raise HubVibeError(f"Could not construct an x402 payment: {exc}") from exc

            retried = http.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={**headers, **pay_headers},
            )
            if retried.status_code == 402:
                # The facilitator rejected the payment, so it was never
                # settled and the budget should not be charged for it.
                self._refund(price_usd)
                raise HubVibeError(
                    f"Payment was rejected by HubVibe: {self._safe_json(retried)}"
                )
            self.last_settlement = self._read_receipt(retried)
            return self._unwrap(retried)

    @staticmethod
    def _read_receipt(response: httpx.Response) -> Optional[dict]:
        """Decode the x402 settlement receipt off a paid response, or None.

        Base64 JSON in PAYMENT-RESPONSE (v2) or X-PAYMENT-RESPONSE (v1). Read
        by hand rather than through the x402 library so a receipt in a shape
        a newer library rejects still reaches the caller: this is evidence
        for a human to reconcile, not an input anything here acts on. Never
        raises -- a malformed receipt must not turn a delivered, paid audit
        into an exception.
        """
        header = response.headers.get("PAYMENT-RESPONSE") or response.headers.get(
            "X-PAYMENT-RESPONSE"
        )
        if not header:
            return None
        try:
            import base64
            import json

            padded = header + "=" * (-len(header) % 4)
            decoded = json.loads(base64.b64decode(padded).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else None
        except Exception:
            return None

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text[:500]

    def _unwrap(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            raise HubVibeError(
                f"HubVibe returned HTTP {response.status_code}: "
                f"{self._safe_json(response)}"
            )
        body = self._safe_json(response)
        if not isinstance(body, dict):
            raise HubVibeError(f"HubVibe returned an unexpected body: {body!r}")
        return body

    def audit(self, url: str, endpoint: str = "bundle") -> dict:
        """Audit `url` and return the parsed result.

        `endpoint` is one of wcag, seo, security, performance, bundle.
        Raises rather than returning a falsy result on any failure, so an
        agent can never mistake "the audit did not run" for "the site passed".
        """
        if endpoint not in PRICES_USD:
            raise ValueError(
                f"Unknown endpoint {endpoint!r}; expected one of {sorted(PRICES_USD)}"
            )
        return self._post(f"/audit/{endpoint}", {"url": url}, PRICES_USD[endpoint])

    def audit_html(self, html: str, endpoint: str = "wcag") -> dict:
        """Audit raw HTML that has not been deployed anywhere yet.

        Only wcag and seo can run without a live URL; security and performance
        need a real HTTP response and a real page load.
        """
        if endpoint not in ("wcag", "seo"):
            raise ValueError("Only 'wcag' and 'seo' can audit raw HTML")
        return self._post(f"/audit/{endpoint}", {"html": html}, PRICES_USD[endpoint])


_shared_lock = threading.Lock()
_shared: Optional[HubVibeTollbooth] = None


def shared_client() -> HubVibeTollbooth:
    """One process-wide client, so the spending budget is actually shared.

    A per-call client would give every call a fresh budget, which quietly
    turns the ceiling into no ceiling at all.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = HubVibeTollbooth.from_env()
        return _shared


def hubvibe_tools():
    """LangChain tools an agent can use to check its own web output.

    CrewAI accepts LangChain @tool functions directly, so the same list works
    there. Imported lazily: the client above is useful without LangChain.
    """
    from langchain_core.tools import tool

    @tool
    def hubvibe_audit_site(url: str) -> dict:
        """Audit a live URL for accessibility (WCAG 2.1 A/AA via axe-core),
        SEO, security headers, and performance in one call.

        Use this to verify a page you built or deployed actually meets
        standards before reporting it as done. Returns a dict with `pass`
        (bool, true only if every dimension passed) plus per-dimension
        `wcag`, `seo`, `security` and `performance` results, each with its
        own `pass` and findings. Costs $0.15, paid automatically.
        """
        return shared_client().audit(url, "bundle")

    @tool
    def hubvibe_audit_accessibility(url: str) -> dict:
        """Run only the WCAG 2.1 A/AA accessibility audit (axe-core) against a
        live URL. Returns `pass` and a list of violations with rule id,
        impact, and how many nodes are affected. Costs $0.05, paid
        automatically. Use the full site audit instead if you also care about
        SEO, security headers, or performance.
        """
        return shared_client().audit(url, "wcag")

    @tool
    def hubvibe_audit_html(html: str) -> dict:
        """Check raw HTML for accessibility violations before it is deployed
        anywhere. Takes the HTML source as a string rather than a URL.
        Returns `pass` and a list of violations. Costs $0.05, paid
        automatically.
        """
        return shared_client().audit_html(html, "wcag")

    return [hubvibe_audit_site, hubvibe_audit_accessibility, hubvibe_audit_html]
