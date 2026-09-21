"""MPP (Machine Payments Protocol) support for the /audit endpoint.

Implements the "Payment" HTTP authentication scheme
(https://github.com/tempoxyz/mpp-specs, draft-httpauth-payment-00) directly
in Python. There is no official Python SDK -- only the Node `mppx` package
-- so this hand-implements the wire protocol against the published spec
(core scheme + draft-stripe-charge-00 + draft-tempo-charge-00). Validate an
actual running server against the reference implementation with:

    npx mppx@latest validate http://localhost:$PORT

Two methods are offered, matching Stripe's own MPP docs
(https://docs.stripe.com/payments/machine/mpp):

- "stripe" (draft-stripe-charge-00): fiat via a single-use Shared Payment
  Token (SPT, `spt_...`). The server creates a PaymentIntent with
  `shared_payment_granted_token=<spt>` and confirms it immediately. Stripe
  enforces single-use on the token itself at the API level, so this module
  doesn't need its own replay ledger for that guarantee -- the in-process
  `_used_credentials` set below is defense in depth, not the real backstop.
- "tempo" (draft-tempo-charge-00): USDC on the Tempo network, "push" mode
  only -- the caller broadcasts their own signed transaction and hands us
  the resulting hash. "pull" mode (server broadcasts a pre-signed
  transaction on the payer's behalf) and the zero-amount EIP-712 "proof"
  credential type are NOT implemented; a request using either fails closed
  rather than silently granting access.

Fails closed everywhere, same pattern as x402_payments.py:
- A method with missing configuration is never offered and never accepted.
- Any exception, expired challenge, broken HMAC binding, wrong method,
  amount/recipient/token mismatch, failed RPC call, or non-succeeded
  PaymentIntent all resolve to "invalid" -- there is no path where an error
  results in access being granted.
- A credential (SPT or tx hash) can only grant access once per process;
  this in-memory set is best-effort under Cloud Run's multi-instance
  scaling (same caveat as the existing rate limiter in app/main.py), which
  is exactly why Stripe's own single-use SPT enforcement -- not this set --
  is what actually protects the stripe method.

Requires, at deploy time (all optional; each method is only offered if its
own vars are fully present):

Stripe SPT (fiat):
- STRIPE_SECRET_KEY               (already required by billing.py)
- MPP_STRIPE_NETWORK_PROFILE_ID   Stripe Business Network Profile ID
- MPP_STRIPE_PRICE_CENTS          default "5" ($0.05)
- MPP_STRIPE_CURRENCY             default "usd"
- MPP_STRIPE_API_VERSION          default "2026-05-27.preview"

Tempo (crypto) -- only MPP_TEMPO_RECIPIENT_ADDRESS is actually required;
the rest default to Tempo mainnet's real values (sourced from Tempo's own
SDK, not a guess):
- MPP_TEMPO_RECIPIENT_ADDRESS     wallet/deposit address that receives
                                   payment -- e.g. a Stripe-managed crypto
                                   deposit address (POST
                                   /v1/crypto/deposit_addresses with
                                   network=tempo), which lets Stripe custody
                                   and auto-convert the funds instead of you
                                   running your own wallet
- MPP_TEMPO_RPC_URL               default "https://rpc.tempo.xyz"
- MPP_TEMPO_TOKEN_ADDRESS         default the real mainnet USDC.e contract,
                                   "0x20C000000000000000000000b9537d11c60E8b50"
- MPP_TEMPO_CHAIN_ID              default 4217 (mainnet)
- MPP_TEMPO_PRICE_BASE_UNITS      default "50000" ($0.05 at 6 decimals)

EVM (draft-evm-charge-00, `type="hash"` ONLY) -- USDC on Base, for payers
whose off-chain signatures no facilitator will verify (an EIP-7702 Coinbase
Smart Wallet: proven 2026-09-21, CDP and PayAI both refuse its EIP-3009
authorization at signature verification, and its ERC-1271 isValidSignature
answers 0xffffffff for the EIP-3009 digest). With `hash` the payer broadcasts
its own ERC-20 transfer and presents the confirmed transaction hash; the
server verifies the receipt on Base and never verifies a signature. The
spec's other three types (`permit2`, `authorization`, `transaction`) all
need that signature and are refused here. Off unless the recipient is set:
- MPP_EVM_RECIPIENT_ADDRESS       wallet that receives the USDC (the x402
                                   pay-to, normally)
- MPP_EVM_RPC_URL                 default "https://mainnet.base.org"
- MPP_EVM_CHAIN_ID                default 8453 (Base mainnet)
- MPP_EVM_TOKEN_ADDRESS           default Base USDC,
                                   "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
- MPP_EVM_MAX_BASE_UNITS          default "10000000" ($10): the spec says
                                   restrict `hash` to low value, because a
                                   hash credential binds to amount+recipient,
                                   not to one challenge instance

Shared:
- MPP_REALM                       default "wcag-audit-engine"
- MPP_CHALLENGE_TTL_SECONDS       default "300"
- MPP_CHALLENGE_SECRET            signs challenges when STRIPE_SECRET_KEY is
                                   not set (the EVM rail needs no Stripe)
"""

import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Optional

import httpx
import stripe

_MPP_API_VERSION = os.environ.get("MPP_STRIPE_API_VERSION", "2026-05-27.preview")
# Per spec, realm SHOULD match the server's own hostname. If MPP_REALM isn't
# set, callers (main.py) pass the request's actual Host header per call
# instead -- this fallback only applies to standalone/CLI use of this module.
_REALM_FALLBACK = os.environ.get("MPP_REALM", "wcag-audit-engine")
_CHALLENGE_TTL_SECONDS = int(os.environ.get("MPP_CHALLENGE_TTL_SECONDS", "300"))

_STRIPE_NETWORK_PROFILE_ID = os.environ.get("MPP_STRIPE_NETWORK_PROFILE_ID")
_STRIPE_PRICE_CENTS = os.environ.get("MPP_STRIPE_PRICE_CENTS", "5")
_STRIPE_CURRENCY = os.environ.get("MPP_STRIPE_CURRENCY", "usd")

# Stripe's own minimum for a card payment made with a Shared Payment Token:
# "Stripe requires a minimum 0.50 USD charge (or the equivalent amount) for
# card payments made with SPT" -- https://docs.stripe.com/payments/machine/mpp
#
# This is the whole reason this rail cannot simply be switched on. Every route
# here is priced at $0.05 or $0.15, all of them under the floor, so a caller
# that took the mpp-stripe challenge and issued an SPT for it would have the
# PaymentIntent rejected by Stripe on amount alone -- a rail advertised and
# unable to settle, which is the exact failure that made the x402 rail
# unpayable for months. The floor is enforced here rather than discovered at
# charge time so the rail is simply not offered where it cannot work.
#
# Overridable because it is Stripe's number, not ours, and it is stated in USD
# for card SPTs; a deployment charging in another currency or reading a revised
# minimum should not have to edit code to say so.
_STRIPE_MIN_CENTS = int(os.environ.get("MPP_STRIPE_MIN_CENTS", "50"))

# Defaults below are Tempo mainnet's real, official values (chain ID, RPC,
# and the actual USDC.e token contract) -- pulled directly from Tempo's own
# SDK (the `mppx` package's tempo/internal/defaults.ts), not a placeholder.
# The generic protocol spec's own example address is a *different* token
# (pathUSD), so don't reuse that one here.
_TEMPO_RPC_URL = os.environ.get("MPP_TEMPO_RPC_URL", "https://rpc.tempo.xyz")
_TEMPO_CHAIN_ID = int(os.environ.get("MPP_TEMPO_CHAIN_ID", "4217"))
_TEMPO_TOKEN_ADDRESS = os.environ.get(
    "MPP_TEMPO_TOKEN_ADDRESS", "0x20C000000000000000000000b9537d11c60E8b50"
)
_TEMPO_RECIPIENT_ADDRESS = os.environ.get("MPP_TEMPO_RECIPIENT_ADDRESS")
_TEMPO_PRICE_BASE_UNITS = os.environ.get("MPP_TEMPO_PRICE_BASE_UNITS", "50000")

# EVM `hash` rail. Defaults are Base mainnet and its USDC contract -- the same
# network and asset every x402 402 on this node already names -- so a payer
# that cannot sign for x402 pays the same amount, of the same token, to the
# same wallet, by sending it itself.
_EVM_RPC_URL = os.environ.get("MPP_EVM_RPC_URL", "https://mainnet.base.org")
_EVM_CHAIN_ID = int(os.environ.get("MPP_EVM_CHAIN_ID", "8453"))
_EVM_TOKEN_ADDRESS = os.environ.get(
    "MPP_EVM_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)
_EVM_RECIPIENT_ADDRESS = os.environ.get("MPP_EVM_RECIPIENT_ADDRESS")
_EVM_MAX_BASE_UNITS = int(os.environ.get("MPP_EVM_MAX_BASE_UNITS", "10000000"))

# keccak256("Transfer(address,address,uint256)") -- standard ERC-20/TIP-20 event topic.
_TRANSFER_EVENT_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Best-effort, single-instance replay guard -- see module docstring.
_used_credentials: set = set()


def _secret_key() -> Optional[bytes]:
    # STRIPE_SECRET_KEY stays the source when present (existing rails); the
    # EVM rail involves no Stripe, so a deployment without Stripe can sign
    # its challenges with MPP_CHALLENGE_SECRET instead.
    stripe_key = os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("MPP_CHALLENGE_SECRET")
    if not stripe_key:
        return None
    return hmac.new(stripe_key.encode(), b"mpp-challenge-signing", hashlib.sha256).digest()


def stripe_configured() -> bool:
    """Whether this deployment holds everything the SPT rail needs.

    Configuration only. Whether the rail can settle a PARTICULAR charge also
    depends on the amount -- see stripe_available_for.
    """
    return bool(_secret_key() and stripe.api_key and _STRIPE_NETWORK_PROFILE_ID)


# What one MPP `stripe` top-up buys, in cents. Must clear _STRIPE_MIN_CENTS or
# Stripe rejects the charge on amount alone.
#
# This is the answer to a constraint that cannot be argued with: SPT has a
# 0.50 USD floor and this service sells $0.05 calls, so the rail can only ever
# settle if what it sells is a BLOCK of calls rather than one call. The agent
# pays once, above the floor, and leaves with a prepaid key worth what it paid.
_STRIPE_TOPUP_CENTS = int(os.environ.get("MPP_STRIPE_TOPUP_CENTS", "50"))


def topup_available() -> bool:
    """Whether the SPT rail can sell a prepaid block on this deployment."""
    return stripe_configured() and _STRIPE_TOPUP_CENTS >= _STRIPE_MIN_CENTS


def topup_cents() -> int:
    return _STRIPE_TOPUP_CENTS


def stripe_available_for(price_cents: int) -> bool:
    """Whether the SPT rail can actually settle a charge of this size.

    Configured is not the same as usable. Stripe rejects a card SPT charge
    below its minimum outright, so offering the rail at $0.05 would hand an
    agent a challenge, take its token, and fail at the API -- the caller
    cannot buy and we cannot sell. Splitting this out of stripe_configured()
    keeps "the operator set the variables" and "money can move" as separate
    facts, which is the distinction the zero-address and the unpayable-402
    bugs both turned on.
    """
    try:
        return stripe_configured() and int(price_cents) >= _STRIPE_MIN_CENTS
    except (TypeError, ValueError):
        return False


_tempo_recipient_warned = False


def _warn_tempo_recipient(reason: str) -> None:
    """Say so, once and loudly. A silent fail-closed here reads as "tempo was
    never configured", which sends the next person hunting for a missing
    variable that is in fact present and merely wrong."""
    global _tempo_recipient_warned
    if _tempo_recipient_warned:
        return
    _tempo_recipient_warned = True
    logging.getLogger(__name__).error(
        "MPP_TEMPO_RECIPIENT_ADDRESS is set but %s. The tempo rail will NOT be "
        "advertised until this is corrected -- advertising it would invite "
        "agents to pay into an address that cannot receive.",
        reason,
    )


def _tempo_recipient_is_usable() -> bool:
    """Shape-check the tempo recipient before the rail is ever advertised.

    `bool(_TEMPO_RECIPIENT_ADDRESS)` was the whole test, so any truthy string
    turned the rail on. That is not hypothetical twice over: the x402 rail
    shipped a 16-hex address and later the zero address through exactly this
    gap, and on 2026-08-29 `mppx validate` -- the protocol's own reference
    client -- reported `Valid recipient address` FAILING on all six routes
    against a recipient of 39 hex characters, a truncated paste of the
    test-suite constant. Every one of our own checks was green at the time,
    because none of them looked.

    A shape check proves shape, and shape is not payability -- so the zero
    address is rejected explicitly, as it is for x402: it satisfies every
    format gate and can never receive a transfer.
    """
    address = _TEMPO_RECIPIENT_ADDRESS
    if not address:
        return False
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        _warn_tempo_recipient(
            "is not a valid EVM address (needs 0x + exactly 40 hex characters; "
            "this one has %d)" % max(len(address) - 2, 0)
        )
        return False
    if set(address[2:].lower()) == {"0"}:
        _warn_tempo_recipient(
            "is the zero address (0x + 40 zeros): well-formed but unownable, "
            "so no payment could ever arrive"
        )
        return False
    return True


def tempo_configured() -> bool:
    return bool(
        _secret_key()
        and _TEMPO_RPC_URL
        and _TEMPO_TOKEN_ADDRESS
        and _tempo_recipient_is_usable()
    )


_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _evm_recipient_is_usable() -> bool:
    address = (_EVM_RECIPIENT_ADDRESS or "").strip()
    return bool(_EVM_ADDRESS_RE.match(address)) and int(address, 16) != 0


def evm_configured() -> bool:
    return bool(
        _secret_key()
        and _EVM_RPC_URL
        and _EVM_ADDRESS_RE.match(_EVM_TOKEN_ADDRESS or "")
        and _evm_recipient_is_usable()
    )


def evm_available_for(base_units) -> bool:
    """The rail, gated on the amount: the spec advises restricting `hash`
    credentials to low value, so a price above MPP_EVM_MAX_BASE_UNITS is
    simply not offered on this rail."""
    try:
        return evm_configured() and 0 < int(base_units) <= _EVM_MAX_BASE_UNITS
    except (TypeError, ValueError):
        return False


def is_configured() -> bool:
    return stripe_configured() or tempo_configured() or evm_configured()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _canonical_json(obj: dict) -> str:
    # A pragmatic JCS (RFC 8785) approximation: sorted keys, no whitespace.
    # Every value we serialize here is a string, int, bool, or nested object
    # of the same shapes -- this isn't a general JCS implementation, just
    # enough to produce a byte-stable serialization of our own challenges
    # (which is all that matters, since the same code both writes and
    # re-derives the HMAC over it).
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rfc3339(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def _parse_rfc3339(value: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _challenge_id(realm, method, intent, request_b64, expires, digest, opaque) -> str:
    secret = _secret_key()
    if secret is None:
        raise ValueError("MPP not configured -- no STRIPE_SECRET_KEY to derive a signing key from")
    parts = [realm or "", method or "", intent or "", request_b64 or "", expires or "", digest or "", opaque or ""]
    mac = hmac.new(secret, "|".join(parts).encode(), hashlib.sha256).digest()
    return _b64url_encode(mac)


def _build_challenge(realm: str, method: str, intent: str, request_obj: dict) -> dict:
    request_b64 = _b64url_encode(_canonical_json(request_obj).encode())
    expires = _rfc3339(time.time() + _CHALLENGE_TTL_SECONDS)
    challenge_id = _challenge_id(realm, method, intent, request_b64, expires, None, None)
    return {
        "id": challenge_id,
        "realm": realm,
        "method": method,
        "intent": intent,
        "request": request_b64,
        "expires": expires,
    }


def _www_authenticate_header(challenge: dict) -> str:
    parts = [
        f'id="{challenge["id"]}"',
        f'realm="{challenge["realm"]}"',
        f'method="{challenge["method"]}"',
        f'intent="{challenge["intent"]}"',
        f'expires="{challenge["expires"]}"',
        f'request="{challenge["request"]}"',
    ]
    return "Payment " + ", ".join(parts)


def www_authenticate_headers(realm: Optional[str] = None, price_usd: Optional[float] = None) -> list:
    """One WWW-Authenticate: Payment header per configured method, for a 402
    response -- lets the caller pick whichever method it can fulfill.

    `realm` should be the request's own Host header; per spec it SHOULD
    match the server's hostname, and binding the challenge to it means a
    challenge issued on one hostname can never be replayed against another
    deployment that happens to share the same signing secret.

    `price_usd` overrides the deploy-wide default (e.g. $0.15 for a bundle
    route vs the default $0.05) -- the amount that ends up in each
    method's challenge, and the amount that verification checks a
    credential against, since the challenge (and its HMAC binding) is
    itself the source of truth for what was actually charged.
    """
    realm = realm or _REALM_FALLBACK
    stripe_price_cents = str(round(price_usd * 100)) if price_usd is not None else _STRIPE_PRICE_CENTS
    tempo_price_base_units = (
        str(round(price_usd * 1_000_000)) if price_usd is not None else _TEMPO_PRICE_BASE_UNITS
    )
    headers = []
    # The top-up challenge. Offered whenever the per-call price is BELOW
    # Stripe's floor -- which is exactly when a per-call SPT charge is
    # impossible and a block is the only thing the rail can sell. Above the
    # floor the per-call challenge below is the better offer and this would
    # just be noise.
    if topup_available() and not stripe_available_for(stripe_price_cents):
        headers.append(
            _www_authenticate_header(
                _build_challenge(
                    realm,
                    "stripe",
                    "topup",
                    {
                        "amount": str(_STRIPE_TOPUP_CENTS),
                        "currency": _STRIPE_CURRENCY,
                        "methodDetails": {
                            "networkId": _STRIPE_NETWORK_PROFILE_ID,
                            "paymentMethodTypes": ["card", "link"],
                        },
                    },
                )
            )
        )
    if stripe_available_for(stripe_price_cents):
        challenge = _build_challenge(
            realm,
            "stripe",
            "charge",
            {
                "amount": stripe_price_cents,
                "currency": _STRIPE_CURRENCY,
                "methodDetails": {
                    "networkId": _STRIPE_NETWORK_PROFILE_ID,
                    "paymentMethodTypes": ["card", "link"],
                },
            },
        )
        headers.append(_www_authenticate_header(challenge))
    if tempo_configured():
        challenge = _build_challenge(
            realm,
            "tempo",
            "charge",
            {
                "amount": tempo_price_base_units,
                "currency": _TEMPO_TOKEN_ADDRESS,
                "recipient": _TEMPO_RECIPIENT_ADDRESS,
                "methodDetails": {"chainId": _TEMPO_CHAIN_ID, "supportedModes": ["push"]},
            },
        )
        headers.append(_www_authenticate_header(challenge))
    if evm_available_for(tempo_price_base_units):
        challenge = _build_challenge(
            realm,
            "evm",
            "charge",
            _evm_request(tempo_price_base_units),
        )
        headers.append(_www_authenticate_header(challenge))
    return headers


def _evm_request(base_units: str) -> dict:
    """The EVM method's challenge request (draft-evm-charge-00 request
    schema): amount in base units, ERC-20 contract, EIP-55 recipient, chain,
    and the one credential type this node accepts."""
    return {
        "amount": str(base_units),
        "currency": _EVM_TOKEN_ADDRESS,
        "recipient": _EVM_RECIPIENT_ADDRESS,
        "methodDetails": {"chainId": _EVM_CHAIN_ID, "credentialTypes": ["hash"]},
    }


def accepts_entries(price_usd: Optional[float] = None) -> list:
    """Entries for the 402 body's machine-readable `accepts` list, one per
    method that is actually configured here.

    Same information the WWW-Authenticate challenges carry, restated in the
    JSON body: a browser-based or higher-level agent that never sees raw
    response headers can still discover how to pay. The authoritative,
    signed challenge is still the header -- this is a discovery aid, not a
    credential, so it deliberately carries no HMAC binding or opaque token.
    """
    entries = []
    stripe_price_cents = (
        str(round(price_usd * 100)) if price_usd is not None else _STRIPE_PRICE_CENTS
    )
    if stripe_available_for(stripe_price_cents):
        entries.append(
            {
                "protocol": "mpp",
                "method": "stripe",
                "asset": _STRIPE_CURRENCY,
                "amount_minor_units": stripe_price_cents,
                "send_via_header": "Authorization: Payment ...",
                "challenge_in": "WWW-Authenticate",
            }
        )
    if tempo_configured():
        entries.append(
            {
                "protocol": "mpp",
                "method": "tempo",
                "asset": _TEMPO_TOKEN_ADDRESS,
                "chain_id": _TEMPO_CHAIN_ID,
                "recipient": _TEMPO_RECIPIENT_ADDRESS,
                "amount_minor_units": (
                    str(round(price_usd * 1_000_000))
                    if price_usd is not None
                    else _TEMPO_PRICE_BASE_UNITS
                ),
                "send_via_header": "Authorization: Payment ...",
                "challenge_in": "WWW-Authenticate",
            }
        )
    evm_units = str(round(price_usd * 1_000_000)) if price_usd is not None else _TEMPO_PRICE_BASE_UNITS
    if evm_available_for(evm_units):
        entries.append(
            {
                "protocol": "mpp",
                "method": "evm",
                "credential_types": ["hash"],
                "asset": _EVM_TOKEN_ADDRESS,
                "chain_id": _EVM_CHAIN_ID,
                "network": f"eip155:{_EVM_CHAIN_ID}",
                "recipient": _EVM_RECIPIENT_ADDRESS,
                "amount_minor_units": evm_units,
                "send_via_header": "Authorization: Payment ...",
                "challenge_in": "WWW-Authenticate",
                "note": (
                    "Send the amount of this token to the recipient yourself, wait for "
                    "the receipt, then present the transaction hash. No signature is "
                    "verified, so smart-wallet payers can use this rail."),
            }
        )
    return entries


def discovery_offers(price_usd: float, description: Optional[str] = None) -> list:
    """Offers for the `x-payment-info` OpenAPI extension, one per usable method.

    This is MPP's capability-discovery surface: the reference tooling (the
    `mppx` package -- its validator AND its client-side discovery) walks a
    service's OpenAPI document and treats an operation as payable only if it
    carries `x-payment-info`. Without it this node's openapi.json says
    "nothing paid here" to every MPP-aware agent, however correct the 402s
    are -- verified directly: `mppx validate` against a booted copy of this
    service reported `endpoints: []` and skipped its entire challenge and
    payment validation suite. Same lesson as the Bazaar record: a surface
    consumed by someone else's parser has to be shaped for their parser.

    Unlike the Bazaar this surface needs no facilitator: it lives in our own
    OpenAPI document, so making it right is entirely within reach.

    The offer shape mirrors mppx's own `Metadata.paymentOffer` -- `amount`
    (integer string in the method's own units), `currency`, `description`,
    `intent`, `method` -- and the same per-method gating as the challenges:
    a method that cannot settle this amount is not offered. Discovery is
    advisory (the runtime 402 stays authoritative, mppx's schema says so in
    its docstring), but advisory does not excuse advertising a dead rail.
    """
    offers = []
    stripe_cents = str(round(price_usd * 100))
    if stripe_available_for(stripe_cents):
        offers.append(
            {
                "amount": stripe_cents,
                "currency": _STRIPE_CURRENCY,
                **({"description": description} if description else {}),
                "intent": "charge",
                "method": "stripe",
            }
        )
    if tempo_configured():
        offers.append(
            {
                "amount": str(round(price_usd * 1_000_000)),
                "currency": _TEMPO_TOKEN_ADDRESS,
                **({"description": description} if description else {}),
                "intent": "charge",
                "method": "tempo",
            }
        )
    evm_units = str(round(price_usd * 1_000_000))
    if evm_available_for(evm_units):
        offers.append(
            {
                "amount": evm_units,
                "currency": _EVM_TOKEN_ADDRESS,
                **({"description": description} if description else {}),
                "intent": "charge",
                "method": "evm",
            }
        )
    return offers


def _verify_challenge_binding(challenge: dict, expected_realm: Optional[str] = None) -> bool:
    try:
        expected_id = _challenge_id(
            challenge.get("realm"),
            challenge.get("method"),
            challenge.get("intent"),
            challenge.get("request"),
            challenge.get("expires"),
            challenge.get("digest"),
            challenge.get("opaque"),
        )
    except ValueError:
        return False
    if not hmac.compare_digest(expected_id, str(challenge.get("id", ""))):
        return False
    if challenge.get("realm") != (expected_realm or _REALM_FALLBACK):
        return False
    expiry_epoch = _parse_rfc3339(str(challenge.get("expires", "")))
    if expiry_epoch is None:
        return False
    return time.time() <= expiry_epoch


def _verify_stripe(challenge: dict, payload: dict) -> bool:
    if not stripe_configured():
        return False
    spt = payload.get("spt")
    if not spt or not isinstance(spt, str) or not spt.startswith("spt_"):
        return False
    if spt in _used_credentials:
        return False
    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        # Re-checked here, not just where the challenge is built: a challenge
        # minted by an older revision (or by a deployment with a lower floor)
        # stays valid for its whole TTL, and burning a caller's single-use SPT
        # on a charge Stripe will reject is worse than refusing it outright.
        if not stripe_available_for(int(request_obj["amount"])):
            return False
        intent = stripe.PaymentIntent.create(
            amount=int(request_obj["amount"]),
            currency=request_obj["currency"],
            shared_payment_granted_token=spt,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"challenge_id": challenge["id"]},
            idempotency_key=f'{challenge["id"]}_{spt}',
            stripe_version=_MPP_API_VERSION,
        )
    except Exception:
        return False
    if intent.get("status") != "succeeded":
        return False
    _used_credentials.add(spt)
    return True


def _tempo_rpc(method: str, params: list) -> Optional[dict]:
    resp = httpx.post(
        _TEMPO_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        return None
    return body.get("result")


def _matching_transfer(receipt: dict, request_obj: dict) -> Optional[dict]:
    """The first Transfer log in `receipt` that pays the challenge: right
    token, right recipient, at least the amount. Returns its payer, recipient
    and value, or None."""
    token_address = str(request_obj["currency"]).lower()
    recipient = str(request_obj["recipient"]).lower()
    expected_amount = int(request_obj["amount"])
    for log in receipt.get("logs", []):
        if str(log.get("address", "")).lower() != token_address:
            continue
        topics = log.get("topics", [])
        if not topics or str(topics[0]).lower() != _TRANSFER_EVENT_TOPIC0:
            continue
        if len(topics) < 3:
            continue
        log_recipient = "0x" + str(topics[2])[-40:]
        if log_recipient.lower() != recipient:
            continue
        try:
            amount = int(str(log.get("data", "0x0")), 16)
        except ValueError:
            continue
        if amount >= expected_amount:
            return {
                "payer": "0x" + str(topics[1])[-40:],
                "recipient": log_recipient,
                "amount": amount,
                "token": str(log.get("address", "")),
            }
    return None


def _receipt_matches(receipt: dict, request_obj: dict) -> bool:
    return _matching_transfer(receipt, request_obj) is not None


def _verify_tempo(challenge: dict, payload: dict) -> bool:
    if not tempo_configured():
        return False
    if payload.get("type") != "hash":
        # "transaction" (pull mode) and "proof" (zero-amount EIP-712) are
        # not implemented -- fail closed rather than guess at validity.
        return False
    tx_hash = payload.get("hash")
    if not tx_hash or not isinstance(tx_hash, str):
        return False
    if tx_hash in _used_credentials:
        return False
    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        receipt = _tempo_rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception:
        return False
    if not receipt:
        return False
    if str(receipt.get("status")) not in ("0x1", "1"):
        return False
    if not _receipt_matches(receipt, request_obj):
        return False
    _used_credentials.add(tx_hash)
    return True


def _evm_rpc(method: str, params: list) -> Optional[dict]:
    resp = httpx.post(
        _EVM_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        return None
    return body.get("result")


# What each verified EVM `hash` credential paid, keyed by transaction hash,
# so the worker ledger can record the same facts an x402 settlement records
# (payer, amount, asset, network, tx) and the receipt reads paid_delivered.
# Bounded: entries are only ever needed for the request that just verified.
_settlements: dict = {}
_SETTLEMENTS_MAX = 5000


def _verify_evm(challenge: dict, payload: dict) -> bool:
    """draft-evm-charge-00 `hash` verification, exactly its five steps: hash
    not consumed; eth_getTransactionReceipt; status 0x1; a Transfer log whose
    address/to/value match the challenge; mark consumed. Every other
    credential type needs a signature verified, which is what this rail
    exists to avoid, so they fail closed."""
    if not evm_configured():
        return False
    if payload.get("type") != "hash":
        return False
    tx_hash = payload.get("hash")
    if not isinstance(tx_hash, str) or not re.match(r"^0x[0-9a-fA-F]{64}$", tx_hash):
        return False
    tx_hash = tx_hash.lower()
    if tx_hash in _used_credentials:
        return False
    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        # The challenge is HMAC-bound, so these are ours; still refuse a
        # challenge that names anything but this deployment's token and
        # recipient, in case the configuration changed under a live one.
        if str(request_obj.get("currency", "")).lower() != _EVM_TOKEN_ADDRESS.lower():
            return False
        if str(request_obj.get("recipient", "")).lower() != (_EVM_RECIPIENT_ADDRESS or "").lower():
            return False
        receipt = _evm_rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception:
        return False
    if not receipt:
        return False
    if str(receipt.get("status")) not in ("0x1", "1"):
        return False
    transfer = _matching_transfer(receipt, request_obj)
    if transfer is None:
        return False
    if len(_settlements) >= _SETTLEMENTS_MAX:
        _settlements.clear()
    _settlements[tx_hash] = {
        "rail": "mpp",
        "method": "evm",
        "payer": transfer["payer"],
        "pay_to": transfer["recipient"],
        "amount_atomic": transfer["amount"],
        "asset": transfer["token"],
        "network": f"eip155:{_EVM_CHAIN_ID}",
        "tx_hash": tx_hash,
    }
    _used_credentials.add(tx_hash)
    return True


def settlement_for(authorization_header: str) -> Optional[dict]:
    """The on-chain facts behind a verified EVM `hash` credential, for the
    ledger and the receipt. None for any other credential, or before
    verification. Never raises."""
    try:
        decoded = json.loads(_b64url_decode(authorization_header))
        tx_hash = str((decoded.get("payload") or {}).get("hash") or "").lower()
        facts = _settlements.get(tx_hash)
        return dict(facts) if facts else None
    except Exception:
        return None


def verify_and_settle_sync(authorization_header: str, realm: Optional[str] = None) -> bool:
    """Verify + settle an `Authorization: Payment <base64url-json>` header.

    `realm` should be the request's own Host header, matching whatever was
    passed to www_authenticate_headers() when the challenge was issued.

    Returns True only once, for a genuinely valid and not-yet-used
    credential against a still-binding, unexpired challenge we issued.
    Never raises past this boundary.
    """
    try:
        decoded = json.loads(_b64url_decode(authorization_header))
        challenge = decoded["challenge"]
        payload = decoded["payload"]
        if not _verify_challenge_binding(challenge, realm):
            return False
        # A top-up buys credit, not this call. Refusing it here rather than
        # letting it read as a per-call payment is the difference between
        # "you bought $0.50 of credit" and "you paid $0.50 for a $0.05
        # audit and got nothing back" -- see settle_topup_sync.
        if challenge.get("intent") == "topup":
            return False
        method = challenge.get("method")
        if method == "stripe":
            return _verify_stripe(challenge, payload)
        if method == "tempo":
            return _verify_tempo(challenge, payload)
        if method == "evm":
            return _verify_evm(challenge, payload)
        return False
    except Exception:
        return False


def settle_topup_sync(authorization_header: str, realm: Optional[str] = None):
    """Settle a `topup` credential and return the cents bought, or None.

    Separate from verify_and_settle_sync because the two mean different
    things to the caller: that one says "this call is paid for", this one says
    "this much credit was purchased". Collapsing them would let a $0.50 top-up
    be consumed as payment for one $0.05 audit, silently keeping the other
    $0.47 -- which is theft dressed as a rounding decision.

    The amount is read from the HMAC-bound challenge rather than from the
    caller, so an agent cannot claim to have bought more credit than it paid
    for. Same fail-closed contract as everything else here: any exception, any
    binding failure, any wrong intent all resolve to None.
    """
    try:
        decoded = json.loads(_b64url_decode(authorization_header))
        challenge = decoded["challenge"]
        payload = decoded["payload"]
        if challenge.get("intent") != "topup":
            return None
        if challenge.get("method") != "stripe":
            return None
        if not _verify_challenge_binding(challenge, realm):
            return None
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        cents = int(request_obj["amount"])
        if cents < _STRIPE_MIN_CENTS:
            return None
        if not _verify_stripe(challenge, payload):
            return None
        return cents
    except Exception:
        return None


def release_credential(authorization_header: str) -> None:
    """Forget a per-call credential whose audit then failed to run.

    A credential is marked used when it verifies, before the audit, so two
    concurrent calls cannot both spend it. If the audit fails, the payer has
    paid (a Stripe charge is confirmed at verification; a tempo transfer was
    broadcast before the call) and received nothing. Releasing the mark lets
    the SAME credential be presented again on the retry: Stripe replays the
    confirmed PaymentIntent under its idempotency key rather than charging
    twice, and the tempo receipt is simply re-read. Never raises.
    """
    try:
        decoded = json.loads(_b64url_decode(authorization_header))
        payload = decoded.get("payload") or {}
        for field in ("spt", "hash"):
            value = payload.get(field)
            if isinstance(value, str):
                _used_credentials.discard(value)
    except Exception:
        pass
