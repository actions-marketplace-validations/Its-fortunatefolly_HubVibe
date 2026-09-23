#!/usr/bin/env python3
"""HubVibe Router -- the buyer-side gateway an agent runs on its own machine.

What it does, in one loop, for any of the node's paid routes (the 37
/work/* workers and the /audit/* checks):

  1. POST the request to hubvibe-io.com. A 200 is the result.
  2. On 402, clear it locally: sign the payment with the agent's OWN wallet
     (USDC on Base or on Solana, the private key never leaves this machine)
     using the official x402 client library, and retry once with the
     PAYMENT-SIGNATURE header. The 200 carries the result and the
     settlement receipt.
  3. For the analytical routes (BigQuery-backed: /work/data/*, /work/stats/*)
     keep the delivered result in a local cache for 24 hours, keyed by an
     MD5 of route + body, so a looping agent that asks the same question
     twice pays once and gets the second answer instantly.
  4. Refuse, before any signature exists, anything over the per-call cap,
     the process budget, or the daily cap. Fail closed, never open.

Nothing of the agent's passes through any server but the HubVibe node it is
buying from: the request goes straight to the route, the payment goes
straight to the facilitator inside the x402 library, and this file talks to
no other host. There is no HubVibe-side proxy in the path.

Two ways to use it
------------------
As a library (copy this; the wallet comes from the environment or from
the files named below):

    from hubvibe_router import HubVibeRouter
    client = HubVibeRouter(endpoint="https://hubvibe-io.com", wallet_type="base")
    response = client.execute_task(tool="stats.probability", payload={...})

`tool` is a catalog name ("stats.probability", "market.quote") or a route
("/work/stats/probability", "/audit/wcag"); `payload` is the route's JSON
body. `wallet_type` is "base" or "solana". `response` is the node's
delivered body: status, worker, price_usd, result, provenance, receipt_url.

The same object under its plain name:

    from hubvibe_router import Router
    r = Router.from_env()
    quote = r.quote("/work/market/quote", {"product_id": "BTC-USD"})   # free
    out = r.call("/work/market/quote", {"product_id": "BTC-USD"})      # pays $0.02

As a local proxy (the ClawRouter shape -- point any HTTP-speaking agent at
it and it never has to know x402 exists):

    python hubvibe_router.py serve --port 8402
    curl -X POST http://127.0.0.1:8402/work/market/quote -d '{"product_id":"BTC-USD"}'

Configuration (environment; a CLI flag of the same name overrides it)
--------------------------------------------------------------------
  HUBVIBE_BASE_URL        default https://hubvibe-io.com
  HUBVIBE_WALLET_KEY      EVM private key (0x...) for USDC on Base
  HUBVIBE_WALLET_FILE     ...or the file holding it (default ~/.hubvibe-wallet-key)
  HUBVIBE_SOLANA_KEY      Solana keypair, base58, for USDC on Solana
  HUBVIBE_SOLANA_FILE     ...or the file holding it (default ~/.hubvibe-solana-key)
  HUBVIBE_RAIL            base | solana -- which wallet clears the 402 when both
                          are configured (default: base if it has a key, else solana)
  HUBVIBE_SOLANA_RPC      default https://api.mainnet-beta.solana.com
  HUBVIBE_API_KEY         a prepaid key; sent as X-API-Key, the wallet is only
                          used if the node still answers 402
  HUBVIBE_MAX_PRICE_USD   per-call ceiling (default 1.00; the premium routes
                          cost $5.00 and $10.00 -- raise it on purpose)
  HUBVIBE_BUDGET_USD      what one process may spend in its lifetime (default 25.00)
  HUBVIBE_DAILY_CAP_USD   what this machine may spend per UTC day, across
                          processes, from the local ledger (default 100.00)
  HUBVIBE_CACHE_TTL       seconds a cached analytical result stays fresh (default 86400)
  HUBVIBE_CACHE_PATHS     comma-separated path prefixes to cache
                          (default /work/data/,/work/stats/)
  HUBVIBE_HOME            where the cache and ledger live (default ~/.hubvibe-router)

Requirements: httpx, and the official x402 client for the rail you use --
`pip install "x402[evm]"` for Base (adds eth-account), `pip install
"x402[svm]"` for Solana (adds solders). Both may be installed together.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

__version__ = "1.0.0"

_ATOMIC_PER_USD = 1_000_000  # USDC has 6 decimals on Base and on Solana
_DEFAULT_CACHE_PATHS = ("/work/data/", "/work/stats/")
_FREE_GET_PATHS = ("/work", "/work/receipts/", "/.well-known/", "/openapi.json",
                   "/mcp.json", "/llms.txt", "/health")


# --- errors: machine-readable, never carrying a local path -------------------


class RouterError(RuntimeError):
    reason = "router_error"

    def __init__(self, detail: str, **extra: Any):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def as_json(self) -> dict:
        return {"status": "error", "reason": self.reason, "detail": self.detail, **self.extra}


class NotConfigured(RouterError):
    reason = "payment_not_configured"


class CapExceeded(RouterError):
    reason = "spend_cap_exceeded"


class PaymentRefused(RouterError):
    reason = "payment_refused"


class Upstream(RouterError):
    reason = "upstream_error"


# --- helpers ------------------------------------------------------------------


def _read_secret(env_value: Optional[str], file_env: Optional[str], default_file: str) -> Optional[str]:
    if env_value and env_value.strip():
        return env_value.strip()
    path = Path(os.path.expanduser(file_env or default_file))
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _b64json(raw: str) -> Optional[dict]:
    try:
        return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- the router ---------------------------------------------------------------


class Router:
    """One buyer, one node, one local wallet per rail, one local cache."""

    def __init__(
        self,
        base_url: str = "https://hubvibe-io.com",
        *,
        evm_key: Optional[str] = None,
        solana_key: Optional[str] = None,
        rail: Optional[str] = None,
        solana_rpc: str = "https://api.mainnet-beta.solana.com",
        api_key: Optional[str] = None,
        max_price_usd: float = 1.00,
        budget_usd: float = 25.00,
        daily_cap_usd: float = 100.00,
        cache_ttl: int = 86400,
        cache_paths: tuple = _DEFAULT_CACHE_PATHS,
        home: Optional[str] = None,
        timeout: float = 300.0,
        user_agent: str = f"hubvibe-router/{__version__}",
    ):
        if max_price_usd <= 0 or budget_usd <= 0 or daily_cap_usd <= 0:
            raise CapExceeded("every spending cap must be a positive amount")
        self.base_url = base_url.rstrip("/")
        self._evm_key = evm_key
        self._solana_key = solana_key
        self.rail = (rail or ("base" if evm_key else "solana" if solana_key else None))
        if self.rail not in (None, "base", "solana"):
            raise NotConfigured("HUBVIBE_RAIL must be 'base' or 'solana'")
        self.solana_rpc = solana_rpc
        self.api_key = api_key
        self.max_price_usd = float(max_price_usd)
        self.budget_usd = float(budget_usd)
        self.daily_cap_usd = float(daily_cap_usd)
        self.cache_ttl = int(cache_ttl)
        self.cache_paths = tuple(cache_paths)
        self.home = Path(os.path.expanduser(home or os.environ.get("HUBVIBE_HOME") or "~/.hubvibe-router"))
        self.cache_dir = self.home / "cache"
        self.ledger_path = self.home / "ledger.jsonl"
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent})
        self._x402_http = None
        self._lock = threading.Lock()
        self._spent_usd = 0.0
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- construction --------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides: Any) -> "Router":
        env = os.environ
        kwargs: dict = {
            "base_url": env.get("HUBVIBE_BASE_URL", "https://hubvibe-io.com"),
            "evm_key": _read_secret(env.get("HUBVIBE_WALLET_KEY"), env.get("HUBVIBE_WALLET_FILE"), "~/.hubvibe-wallet-key"),
            "solana_key": _read_secret(env.get("HUBVIBE_SOLANA_KEY"), env.get("HUBVIBE_SOLANA_FILE"), "~/.hubvibe-solana-key"),
            "rail": env.get("HUBVIBE_RAIL") or None,
            "solana_rpc": env.get("HUBVIBE_SOLANA_RPC", "https://api.mainnet-beta.solana.com"),
            "api_key": env.get("HUBVIBE_API_KEY") or None,
            "max_price_usd": float(env.get("HUBVIBE_MAX_PRICE_USD", "1.00")),
            "budget_usd": float(env.get("HUBVIBE_BUDGET_USD", "25.00")),
            "daily_cap_usd": float(env.get("HUBVIBE_DAILY_CAP_USD", "100.00")),
            "cache_ttl": int(env.get("HUBVIBE_CACHE_TTL", "86400")),
            "cache_paths": tuple(p.strip() for p in env.get("HUBVIBE_CACHE_PATHS", ",".join(_DEFAULT_CACHE_PATHS)).split(",") if p.strip()),
            "home": env.get("HUBVIBE_HOME") or None,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def spent_usd(self) -> float:
        return round(self._spent_usd, 6)

    def payer_address(self) -> Optional[str]:
        """The public address that will pay on the chosen rail, or None."""
        try:
            if self.rail == "base" and self._evm_key:
                from eth_account import Account
                return Account.from_key(self._evm_key).address
            if self.rail == "solana" and self._solana_key:
                from solders.keypair import Keypair
                return str(Keypair.from_base58_string(self._solana_key).pubkey())
        except Exception:
            return None
        return None

    # ---- the x402 client (built lazily, once) ----------------------------------

    def _x402(self):
        if self._x402_http is not None:
            return self._x402_http
        if self.rail is None:
            raise NotConfigured("no wallet: set HUBVIBE_WALLET_KEY/FILE (Base) or HUBVIBE_SOLANA_KEY/FILE (Solana)")
        try:
            from x402 import max_amount, x402ClientSync
            from x402.http import x402HTTPClientSync
        except ImportError as exc:
            raise NotConfigured("the x402 client library is not installed: pip install 'x402[evm]' or 'x402[svm]'") from exc
        client = x402ClientSync()
        cap_atomic = int(round(self.max_price_usd * _ATOMIC_PER_USD))
        # The library's own guard (default $1.00) is set to the configured cap
        # so a payment above it is never signed, whatever the node asks for.
        if hasattr(client, "set_spend_controls"):
            client.set_spend_controls({"max_amount_per_payment": f"${self.max_price_usd:.2f}"})
        if self.rail == "base":
            if not self._evm_key:
                raise NotConfigured("HUBVIBE_RAIL=base but no EVM key is configured")
            try:
                from eth_account import Account
                from x402.mechanisms.evm import EthAccountSigner
                from x402.mechanisms.evm.exact import register_exact_evm_client
            except ImportError as exc:
                raise NotConfigured("Base rail needs: pip install 'x402[evm]'") from exc
            register_exact_evm_client(client, EthAccountSigner(Account.from_key(self._evm_key)),
                                      policies=[max_amount(cap_atomic)])
        else:
            if not self._solana_key:
                raise NotConfigured("HUBVIBE_RAIL=solana but no Solana key is configured")
            try:
                from solders.keypair import Keypair
                from x402.mechanisms.svm import KeypairSigner
                from x402.mechanisms.svm.exact import register_exact_svm_client
            except ImportError as exc:
                raise NotConfigured("Solana rail needs: pip install 'x402[svm]'") from exc
            register_exact_svm_client(client, KeypairSigner(Keypair.from_base58_string(self._solana_key)),
                                      policies=[max_amount(cap_atomic)], rpc_url=self.solana_rpc)
        self._x402_http = x402HTTPClientSync(client)
        return self._x402_http

    def _sign(self, response: httpx.Response, url: str) -> dict:
        """Headers that clear this 402: the signed payment for the accepted
        rail. Produced by the official client; nothing here touches the key."""
        headers, _payload = self._x402().handle_402_response(dict(response.headers), response.content, url)
        return dict(headers)

    # ---- caps ------------------------------------------------------------------

    def _spent_today(self) -> float:
        total, today = 0.0, _utc_day()
        if not self.ledger_path.is_file():
            return 0.0
        with self.ledger_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("day") == today and row.get("settled"):
                    total += float(row.get("price_usd") or 0)
        return total

    def _check_caps(self, price_usd: float, path: str) -> None:
        if price_usd > self.max_price_usd:
            raise CapExceeded(
                f"{path} costs ${price_usd:.2f}, above the per-call cap of ${self.max_price_usd:.2f}",
                price_usd=price_usd, cap_usd=self.max_price_usd, cap="per_call")
        with self._lock:
            if self._spent_usd + price_usd > self.budget_usd:
                raise CapExceeded(
                    f"${price_usd:.2f} would take this process past its ${self.budget_usd:.2f} budget "
                    f"(spent ${self._spent_usd:.2f})", price_usd=price_usd, cap_usd=self.budget_usd, cap="process_budget")
            if self._spent_today() + price_usd > self.daily_cap_usd:
                raise CapExceeded(
                    f"${price_usd:.2f} would take this machine past its ${self.daily_cap_usd:.2f} daily cap",
                    price_usd=price_usd, cap_usd=self.daily_cap_usd, cap="daily")

    # ---- cache -----------------------------------------------------------------

    def cacheable(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.cache_paths)

    @staticmethod
    def cache_key(path: str, body: Any) -> str:
        # MD5 as a fast deterministic key, not as a security primitive.
        return hashlib.md5(_canonical({"path": path, "body": body}).encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[dict]:
        file = self.cache_dir / f"{key}.json"
        if not file.is_file():
            return None
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except Exception:
            return None
        age = time.time() - float(record.get("stored_at") or 0)
        if age > self.cache_ttl:
            try:
                file.unlink()
            except OSError:
                pass
            return None
        record["age_seconds"] = int(age)
        return record

    def _cache_put(self, key: str, path: str, payload: Any) -> None:
        file = self.cache_dir / f"{key}.json"
        tmp = file.with_suffix(".tmp")
        tmp.write_text(_canonical({"path": path, "stored_at": time.time(), "payload": payload}), encoding="utf-8")
        os.replace(tmp, file)

    def cache_clear(self) -> int:
        n = 0
        for file in self.cache_dir.glob("*.json"):
            file.unlink()
            n += 1
        return n

    # ---- ledger ----------------------------------------------------------------

    def _record(self, row: dict) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "day": _utc_day(), **row}
        with self._lock:
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(row) + "\n")

    def ledger(self, limit: int = 50) -> list:
        if not self.ledger_path.is_file():
            return []
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    # ---- the loop ----------------------------------------------------------------

    @staticmethod
    def _price_of(response: httpx.Response) -> Optional[float]:
        """The quoted price of a 402: the node's own price_usd, or the first
        accepted requirement's amount in the v2 header."""
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("price_usd") is not None:
                return float(body["price_usd"])
        except Exception:
            pass
        raw = response.headers.get("PAYMENT-REQUIRED") or response.headers.get("payment-required")
        challenge = _b64json(raw) if raw else None
        for accepted in (challenge or {}).get("accepts") or []:
            amount = accepted.get("amount") or accepted.get("maxAmountRequired")
            if amount is not None:
                return int(amount) / _ATOMIC_PER_USD
        return None

    @staticmethod
    def _settlement_of(response: httpx.Response) -> Optional[dict]:
        raw = response.headers.get("PAYMENT-RESPONSE") or response.headers.get("X-PAYMENT-RESPONSE")
        return _b64json(raw) if raw else None

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def quote(self, path: str, body: Any) -> dict:
        """The price and rails of a route, from its 402. Free; nothing is signed."""
        url = self.base_url + path
        response = self._http.post(url, json=body, headers={"Content-Type": "application/json"})
        if response.status_code != 402:
            return {"path": path, "status": response.status_code, "price_usd": 0.0 if response.status_code == 200 else None}
        raw = response.headers.get("PAYMENT-REQUIRED") or response.headers.get("payment-required")
        challenge = _b64json(raw) if raw else {}
        return {
            "path": path, "status": 402, "price_usd": self._price_of(response),
            "rails": [a.get("network") for a in (challenge or {}).get("accepts") or []],
        }

    @staticmethod
    def route_for(tool: str) -> str:
        """A catalog name or a route, to the route: "stats.probability" ->
        /work/stats/probability, "audit.wcag" -> /audit/wcag, a path is kept."""
        tool = tool.strip()
        if tool.startswith("/"):
            return tool
        if tool.startswith("audit."):
            return "/audit/" + tool.split(".", 1)[1]
        if tool in ("bundle", "wcag", "seo", "security", "performance"):
            return "/audit/" + tool
        return "/work/" + tool.replace(".", "/")

    def execute_task(self, tool: str, payload: Any = None, *, use_cache: bool = True) -> dict:
        """Buy one job by tool name or route. Same loop as `call`."""
        return self.call(self.route_for(tool), payload if payload is not None else {}, use_cache=use_cache)

    def call(self, path: str, body: Any, *, use_cache: bool = True) -> dict:
        """Buy one job: POST, clear the 402 with the local wallet if needed,
        return the delivered body. Cached analytical results are returned
        without a network call and marked `router.cache == "hit"`."""
        if not path.startswith("/"):
            path = "/" + path
        key = self.cache_key(path, body) if (use_cache and self.cacheable(path)) else None
        if key:
            hit = self._cache_get(key)
            if hit is not None:
                payload = hit["payload"]
                if isinstance(payload, dict):
                    payload = {**payload, "router": {"cache": "hit", "age_seconds": hit["age_seconds"], "ttl_seconds": self.cache_ttl}}
                return payload

        url = self.base_url + path
        headers = {"Content-Type": "application/json", **self._headers()}
        first = self._http.post(url, json=body, headers=headers)
        response, price, settlement = first, 0.0, None
        if first.status_code == 402:
            price = self._price_of(first)
            if price is None:
                raise PaymentRefused("the node answered 402 without a readable price", path=path)
            self._check_caps(price, path)
            try:
                paid_headers = self._sign(first, url)
            except RouterError:
                raise
            except Exception as exc:  # the library refused to sign: cap, unsupported rail, bad key
                raise PaymentRefused(f"could not sign the payment: {type(exc).__name__}: {str(exc)[:160]}", path=path, price_usd=price)
            response = self._http.post(url, json=body, headers={**headers, **paid_headers})
            settlement = self._settlement_of(response)
            if response.status_code == 402:
                self._record({"path": path, "price_usd": price, "rail": self.rail, "settled": False,
                              "outcome": "refused", "detail": self._short_error(response)})
                raise PaymentRefused(f"payment refused: {self._short_error(response)}", path=path, price_usd=price)

        if response.status_code != 200:
            raise Upstream(f"HTTP {response.status_code}: {self._short_error(response)}",
                           path=path, http_status=response.status_code, billed=False)

        payload = self._safe_json(response)
        if price:
            with self._lock:
                self._spent_usd += price
            self._record({"path": path, "price_usd": price, "rail": self.rail, "settled": True,
                          "tx": (settlement or {}).get("transaction"), "payer": (settlement or {}).get("payer"),
                          "receipt_url": payload.get("receipt_url") if isinstance(payload, dict) else None})
        if key and isinstance(payload, dict):
            self._cache_put(key, path, payload)
            payload = {**payload, "router": {"cache": "miss", "ttl_seconds": self.cache_ttl}}
        return payload

    def get(self, path: str) -> Any:
        """A free GET on the node (discovery, receipts, health)."""
        response = self._http.get(self.base_url + path)
        if response.status_code != 200:
            raise Upstream(f"HTTP {response.status_code}", path=path, http_status=response.status_code)
        return self._safe_json(response)

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {"status": "error", "reason": "non_json_response", "text": response.text[:500]}

    @staticmethod
    def _short_error(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                return str(body.get("detail") or body.get("error") or body.get("reason") or body)[:200]
        except Exception:
            pass
        return " ".join(response.text.split())[:200]


class HubVibeRouter(Router):
    """The copyable client: `HubVibeRouter(endpoint=..., wallet_type="base")`.

    Wallets, caps and cache settings come from the environment (see the
    module docstring), so an agent's code carries no secrets. `wallet_type`
    picks the rail: "base" signs USDC on Base with HUBVIBE_WALLET_KEY/FILE,
    "solana" signs USDC on Solana with HUBVIBE_SOLANA_KEY/FILE.
    """

    def __init__(self, endpoint: str = "https://hubvibe-io.com", wallet_type: Optional[str] = None,
                 **overrides: Any):
        rail = {"base": "base", "evm": "base", "solana": "solana", "svm": "solana", None: None}.get(
            wallet_type.lower() if isinstance(wallet_type, str) else None, "?")
        if rail == "?":
            raise NotConfigured("wallet_type must be 'base' or 'solana'")
        env = os.environ
        kwargs: dict = {
            "base_url": endpoint,
            "evm_key": _read_secret(env.get("HUBVIBE_WALLET_KEY"), env.get("HUBVIBE_WALLET_FILE"), "~/.hubvibe-wallet-key"),
            "solana_key": _read_secret(env.get("HUBVIBE_SOLANA_KEY"), env.get("HUBVIBE_SOLANA_FILE"), "~/.hubvibe-solana-key"),
            "rail": rail or env.get("HUBVIBE_RAIL") or None,
            "solana_rpc": env.get("HUBVIBE_SOLANA_RPC", "https://api.mainnet-beta.solana.com"),
            "api_key": env.get("HUBVIBE_API_KEY") or None,
            "max_price_usd": float(env.get("HUBVIBE_MAX_PRICE_USD", "1.00")),
            "budget_usd": float(env.get("HUBVIBE_BUDGET_USD", "25.00")),
            "daily_cap_usd": float(env.get("HUBVIBE_DAILY_CAP_USD", "100.00")),
            "cache_ttl": int(env.get("HUBVIBE_CACHE_TTL", "86400")),
            "cache_paths": tuple(p.strip() for p in env.get("HUBVIBE_CACHE_PATHS", ",".join(_DEFAULT_CACHE_PATHS)).split(",") if p.strip()),
            "home": env.get("HUBVIBE_HOME") or None,
        }
        kwargs.update(overrides)
        super().__init__(**kwargs)


# --- local proxy: the agent talks HTTP to 127.0.0.1, the router does the rest --


def serve(router: Router, host: str = "127.0.0.1", port: int = 8402) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = f"hubvibe-router/{__version__}"

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/router/status":
                return self._send(200, {"status": "ok", "node": router.base_url, "rail": router.rail,
                                        "payer": router.payer_address(), "spent_usd": router.spent_usd,
                                        "caps": {"per_call": router.max_price_usd, "process": router.budget_usd,
                                                 "daily": router.daily_cap_usd},
                                        "cache": {"ttl_seconds": router.cache_ttl, "paths": list(router.cache_paths)}})
            if path == "/router/ledger":
                return self._send(200, {"rows": router.ledger()})
            if any(path == p.rstrip("/") or path.startswith(p) for p in _FREE_GET_PATHS):
                try:
                    return self._send(200, router.get(path))
                except RouterError as exc:
                    return self._send(502, exc.as_json())
            return self._send(404, {"status": "error", "reason": "not_found", "detail": "GET is for discovery paths; paid routes are POST"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                return self._send(400, {"status": "error", "reason": "invalid_json", "detail": "body must be a JSON object"})
            if path == "/router/cache/clear":
                return self._send(200, {"status": "ok", "cleared": router.cache_clear()})
            use_cache = self.headers.get("X-HubVibe-Cache", "").lower() != "bypass"
            try:
                return self._send(200, router.call(path, body, use_cache=use_cache))
            except CapExceeded as exc:
                return self._send(402, exc.as_json())
            except NotConfigured as exc:
                return self._send(503, exc.as_json())
            except PaymentRefused as exc:
                return self._send(402, exc.as_json())
            except Upstream as exc:
                return self._send(int(exc.extra.get("http_status") or 502), exc.as_json())
            except RouterError as exc:
                return self._send(500, exc.as_json())

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("router  %s %s\n" % (self.command, fmt % args))

    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"hubvibe-router {__version__} on http://{host}:{port} -> {router.base_url} "
                     f"(rail {router.rail or 'none'}, payer {router.payer_address() or 'none'})\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# --- CLI --------------------------------------------------------------------------


def _cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="hubvibe-router", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--rail", choices=["base", "solana"], default=None)
    parser.add_argument("--max-price-usd", type=float, default=None)
    parser.add_argument("--budget-usd", type=float, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_quote = sub.add_parser("quote", help="price and rails of a route (free)")
    p_quote.add_argument("path"); p_quote.add_argument("body", nargs="?", default="{}")
    p_call = sub.add_parser("call", help="buy one job (pays if the node asks)")
    p_call.add_argument("path"); p_call.add_argument("body", nargs="?", default="{}")
    p_call.add_argument("--no-cache", action="store_true")
    p_serve = sub.add_parser("serve", help="local proxy for agents")
    p_serve.add_argument("--host", default="127.0.0.1"); p_serve.add_argument("--port", type=int, default=8402)
    sub.add_parser("status", help="wallet, caps, spend")
    sub.add_parser("ledger", help="last local purchases")
    p_cache = sub.add_parser("cache", help="cache maintenance"); p_cache.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)

    overrides = {k: v for k, v in {"base_url": args.base_url, "rail": args.rail, "max_price_usd": args.max_price_usd,
                                    "budget_usd": args.budget_usd}.items() if v is not None}
    try:
        router = Router.from_env(**overrides)
        if args.cmd == "quote":
            print(json.dumps(router.quote(args.path, json.loads(args.body)), indent=2))
        elif args.cmd == "call":
            print(json.dumps(router.call(args.path, json.loads(args.body), use_cache=not args.no_cache), indent=2, default=str))
        elif args.cmd == "serve":
            serve(router, args.host, args.port)
        elif args.cmd == "status":
            print(json.dumps({"node": router.base_url, "rail": router.rail, "payer": router.payer_address(),
                              "api_key": bool(router.api_key), "caps": {"per_call": router.max_price_usd,
                              "process": router.budget_usd, "daily": router.daily_cap_usd},
                              "spent_today_usd": router._spent_today(), "cache_ttl_seconds": router.cache_ttl,
                              "cache_paths": list(router.cache_paths)}, indent=2))
        elif args.cmd == "ledger":
            print(json.dumps(router.ledger(), indent=2))
        elif args.cmd == "cache":
            print(json.dumps({"cleared": router.cache_clear()} if args.clear else {"dir": "cache", "ttl_seconds": router.cache_ttl}))
        return 0
    except RouterError as exc:
        print(json.dumps(exc.as_json()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
