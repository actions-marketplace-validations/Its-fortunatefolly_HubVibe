#!/usr/bin/env python3
"""Pay a HubVibe 402 with x402, from inside someone else's CI.

Why this exists: the action's only payment path used to be an API key, and a
key costs a subscription bought through a browser. That makes the first run of
a newly added CI step fail with "HTTP 402 -- go buy a plan", and a step that
fails on its first execution is deleted on the next push. The adoption funnel
was closed before it opened, however good the discovery surfaces were.

With a funded wallet the pipeline pays $0.05 a run by itself: no account, no
checkout, no human. That is the machine-payable thesis applied to the channel
with the most volume in it.

Contract with the calling shell (action.yml):
- reads BASE_URL, AUDIT_ENDPOINT, TARGET_URL, TIMEOUT_SECONDS,
  HUBVIBE_WALLET_KEY, MAX_PRICE_USD from the environment
- writes the response body to ./response.json, exactly as the curl path does,
  so every downstream step (summary rendering, pass/fail parsing) is unchanged
- prints ONE line to stdout: the HTTP status, or 402 when payment could not be
  made. Never raises past main(): an exception here would surface as a broken
  action rather than as an unpaid call, and the two need different fixes.

Spending someone else's money in their pipeline is the sharp edge, so the
per-call cap is enforced BEFORE a signature exists, as an x402 spend policy on
the signer. A 402 quoting more than the cap is refused rather than paid.
"""

import json
import os
import sys

# USDC and every other x402 EVM asset in use here carries 6 decimals, so a
# dollar cap converts to atomic units by 1e6. Kept as a named constant because
# a wrong exponent here is a 1,000,000x spending error in the safe direction
# once and the unsafe direction the other way.
_ATOMIC_PER_USD = 1_000_000


def _emit(status: str, body: dict) -> None:
    """Write the body where the shell expects it and report the status."""
    try:
        with open("response.json", "w") as handle:
            json.dump(body, handle)
    except OSError:
        pass
    print(status)


def _fail(message: str) -> None:
    """Report an unpaid call, loudly in the log and quietly to the shell.

    402 rather than 000: the call genuinely was not paid for, and the action
    already has a branch that says so usefully. Inventing a network-error code
    would send the reader looking for an outage that did not happen.
    """
    print("::error::%s" % message, file=sys.stderr)
    _emit("402", {"error": message})


def main() -> None:
    try:
        base = os.environ["BASE_URL"].rstrip("/")
        endpoint = os.environ["AUDIT_ENDPOINT"]
        target_url = os.environ["TARGET_URL"]
        wallet_key = os.environ["HUBVIBE_WALLET_KEY"].strip()
        timeout = float(os.environ.get("TIMEOUT_SECONDS", "90"))
        cap_usd = float(os.environ.get("MAX_PRICE_USD", "0.15"))
    except (KeyError, ValueError) as exc:
        _fail("x402 payment is misconfigured: %s" % exc)
        return

    if cap_usd <= 0:
        _fail("max-price-usd must be greater than zero (got %r)" % cap_usd)
        return

    try:
        from eth_account import Account
        from x402 import max_amount, x402ClientSync
        from x402.http import x402HTTPClientSync
        from x402.mechanisms.evm import EthAccountSigner
        from x402.mechanisms.evm.exact import register_exact_evm_client
    except ImportError as exc:
        _fail(
            "the x402 client extras are not installed (%s). "
            "Install with: pip install 'x402[evm]' eth-account" % exc
        )
        return

    try:
        account = Account.from_key(wallet_key)
    except Exception:
        # Deliberately does not echo the value or its length: this runs in a
        # log that CI keeps, and a private key must not be reconstructible
        # from a diagnostic.
        _fail("wallet-key is not a valid EVM private key (expected 0x + 64 hex characters)")
        return

    client = x402ClientSync()
    register_exact_evm_client(
        client,
        EthAccountSigner(account),
        policies=[max_amount(int(round(cap_usd * _ATOMIC_PER_USD)))],
    )
    http = x402HTTPClientSync(client)

    print(
        "x402: paying from %s, capped at $%.2f per call" % (account.address, cap_usd),
        file=sys.stderr,
    )

    # x402HTTPClientSync does not speak HTTP itself -- it has no .post(). It
    # encodes and decodes the protocol around a request WE make: send the call
    # unpaid, hand it the 402 to turn into payment headers, then send the same
    # call again carrying them. Driving it any other way raises AttributeError
    # before a single byte reaches the node.
    url = "%s/audit/%s" % (base, endpoint)
    body = {"url": target_url}

    try:
        import httpx

        with httpx.Client(timeout=timeout, follow_redirects=False) as session:
            response = session.post(url, json=body)

            if response.status_code == 402:
                payment_headers, payment_payload = http.handle_402_response(
                    dict(response.headers), response.content, url
                )
                response = session.post(url, json=body, headers=payment_headers)
                if payment_payload is not None:
                    # Reads the settle receipt off the 200 and lets the client
                    # record it; a failed settle is reported by the node in the
                    # body, which the caller writes out below either way.
                    http.process_payment_result(
                        payment_payload,
                        lambda name: response.headers.get(name),
                        response.status_code,
                    )
    except Exception as exc:
        # The x402 client raises with the whole 402 body attached -- schema and
        # all -- and a screenful of JSON buries the one line that says why.
        detail = " ".join(str(exc).split())
        if len(detail) > 300:
            detail = detail[:300] + " ...[truncated]"
        _fail("x402 payment failed: %s: %s" % (type(exc).__name__, detail))
        return

    try:
        with open("response.json", "w") as handle:
            handle.write(response.text)
    except OSError as exc:
        _fail("could not write response.json: %s" % exc)
        return

    print(str(response.status_code))


if __name__ == "__main__":
    main()
