#!/usr/bin/env bash
# scripts/test_solana_route.sh -- exercise the Solana x402 rail on one premium
# route, end to end, from an SVM-funded wallet.
#
# The node advertises two `exact` rails in the x402 v2 PAYMENT-REQUIRED
# header of every 402: USDC on Base and USDC on Solana mainnet. The Solana
# rail has never been paid live. This script is the proof harness for it.
#
# What it does, by mode:
#
#   (default, DRY RUN -- no key needed, nothing signed, nothing spent)
#     1. POST the route unpaid, receive the 402.
#     2. Decode the PAYMENT-REQUIRED header (x402 v2, base64 JSON).
#     3. Pick the Solana requirement and check it against what this node is
#        known to advertise: recipient J1K4m...icSh, facilitator feePayer
#        present, amount 5000000 atomic USDC (= $5.00), USDC mint EPjF...t1v.
#     4. Print the exact requirement and the curl syntax for both requests.
#
#   --sign-only   (needs SOLANA_PAYER_SECRET or ~/.hubvibe-solana-key)
#     Builds and signs the x402 payment payload with the official x402 SVM
#     client -- the same library a buying agent uses -- and prints the paid
#     curl command with its PAYMENT-SIGNATURE header. Does NOT send it.
#     Note: the signed transaction carries a recent blockhash and expires in
#     about a minute; run the printed curl promptly or use --pay.
#
#   --pay         (needs the key; SPENDS 5.00 USDC on Solana)
#     Signs and sends. Prints the HTTP status, the settlement receipt from
#     the PAYMENT-RESPONSE header, the receipt_url, and the result keys.
#
# Environment:
#   BASE                 node base URL           (default https://hubvibe-io.com)
#   ROUTE                route to buy            (default /work/research/brief, $5.00)
#   EXPECT_PRICE_USD     refuse if quoted price differs (default 5.00)
#   SOLANA_PAYER_SECRET  payer keypair, base58 (solders/Phantom export format);
#                        or put it in SOLANA_PAYER_FILE (default ~/.hubvibe-solana-key)
#   SOLANA_RPC_URL       RPC for the recent blockhash (default https://api.mainnet-beta.solana.com)
#   EXPECT_PAY_TO        recipient the 402 must name (default J1K4mdbvXEbJLRKpgxFcA7s66LWMCYHu71ye6Hz2icSh)
#
# Requirements: python3 with x402[svm] (`pip install "x402[svm]"`), httpx.
# On the box: /root/.hubvibe-venv/bin/python3 has them.
#
# Exit codes: 0 = the checks (and, with --pay, the purchase) passed;
#             2 = the 402 does not offer a Solana rail that matches;
#             3 = missing key / library;  1 = anything else.

set -euo pipefail

MODE="dry-run"
case "${1:-}" in
  --sign-only) MODE="sign-only" ;;
  --pay)       MODE="pay" ;;
  ""|--dry-run) MODE="dry-run" ;;
  -h|--help)   sed -n '2,45p' "$0"; exit 0 ;;
  *) echo "unknown option: $1 (use --dry-run, --sign-only or --pay)" >&2; exit 1 ;;
esac

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "STOP  $PY is not on PATH" >&2; exit 3; }
if ! "$PY" -c 'import x402.mechanisms.svm, httpx' 2>/dev/null; then
  echo "STOP  $PY lacks x402[svm] and/or httpx. Install: $PY -m pip install 'x402[svm]' httpx" >&2
  echo "      (on the box use PYTHON=/root/.hubvibe-venv/bin/python3)" >&2
  exit 3
fi

MODE="$MODE" "$PY" - <<'PY'
import base64, json, os, sys

import httpx

MODE = os.environ["MODE"]
BASE = os.environ.get("BASE", "https://hubvibe-io.com").rstrip("/")
ROUTE = os.environ.get("ROUTE", "/work/research/brief")
EXPECT_PRICE = float(os.environ.get("EXPECT_PRICE_USD", "5.00"))
EXPECT_PAY_TO = os.environ.get("EXPECT_PAY_TO", "J1K4mdbvXEbJLRKpgxFcA7s66LWMCYHu71ye6Hz2icSh")
RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ATOMIC_PER_USD = 1_000_000
BODY = {"url": "https://example.com", "question": "What is this page for?"}
UA = "hubvibe-solana-route-test/1.0"


def step(msg):
    print(f"\n==> {msg}")


def ok(msg):
    print(f"  OK    {msg}")


def stop(code, msg):
    print(f"  STOP  {msg}", file=sys.stderr)
    sys.exit(code)


def decode_header(raw: str) -> dict:
    return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))


with httpx.Client(timeout=240, headers={"User-Agent": UA}) as s:
    url = BASE + ROUTE
    step(f"Unpaid POST {ROUTE} -> expecting 402 with a Solana rail")
    unpaid = s.post(url, json=BODY)
    if unpaid.status_code != 402:
        stop(1, f"HTTP {unpaid.status_code} before payment: {unpaid.text[:200]}")
    body = unpaid.json()
    quoted = float(body.get("price_usd") or 0)
    ok(f"402 received, quoted ${quoted:.2f}")
    if abs(quoted - EXPECT_PRICE) > 1e-9:
        stop(2, f"quoted ${quoted:.2f}, expected ${EXPECT_PRICE:.2f}; refusing (set EXPECT_PRICE_USD to override)")

    raw = unpaid.headers.get("PAYMENT-REQUIRED") or unpaid.headers.get("payment-required")
    if not raw:
        stop(2, "no PAYMENT-REQUIRED header: the node is not offering x402 v2")
    challenge = decode_header(raw)
    accepts = challenge.get("accepts") or []
    solana = [a for a in accepts if str(a.get("network", "")).startswith("solana:")]
    ok(f"header offers {len(accepts)} rail(s): {[a['network'].split(':')[0] for a in accepts]}")
    if not solana:
        stop(2, "the 402 header offers no Solana rail (node not configured, or facilitator lists no Solana feePayer)")
    req = solana[0]
    fee_payer = (req.get("extra") or {}).get("feePayer")

    step("Solana requirement as advertised")
    print(json.dumps(req, indent=2))
    checks = [
        (req.get("scheme") == "exact", "scheme exact"),
        (req.get("payTo") == EXPECT_PAY_TO, f"payTo is {EXPECT_PAY_TO}"),
        (req.get("asset") == USDC_MINT, "asset is Solana mainnet USDC"),
        (str(req.get("amount")) == str(int(round(EXPECT_PRICE * ATOMIC_PER_USD))), f"amount is {int(EXPECT_PRICE * ATOMIC_PER_USD)} atomic"),
        (bool(fee_payer), "facilitator feePayer present"),
        (int(req.get("maxTimeoutSeconds") or 0) >= 60, "maxTimeoutSeconds sane"),
    ]
    bad = [label for passed, label in checks if not passed]
    for passed, label in checks:
        print(f"  {'OK   ' if passed else 'FAIL '} {label}")
    if bad:
        stop(2, f"requirement mismatch: {bad}")

    step("curl syntax (unpaid request; the 402 is the price quote)")
    print(f"  curl -sS -D - -X POST '{url}' -H 'content-type: application/json' \\\n"
          f"       -d '{json.dumps(BODY)}'")
    print("  # decode the PAYMENT-REQUIRED response header: base64 -> JSON, pick accepts[network=solana:...]")
    step("curl syntax (paid request; PAYMENT-SIGNATURE is the base64 x402 payload the SVM client builds)")
    print(f"  curl -sS -D - -X POST '{url}' -H 'content-type: application/json' \\\n"
          f"       -H 'PAYMENT-SIGNATURE: <base64 payload: x402Version=2, scheme=exact, network={req['network']}, "
          f"payload.transaction=<base64 signed VersionedTransaction: feePayer {fee_payer[:6]}..., "
          f"SPL transfer {req['amount']} of USDC to {EXPECT_PAY_TO[:6]}...>>' \\\n"
          f"       -d '{json.dumps(BODY)}'")
    print("  # 200 + PAYMENT-RESPONSE header (settlement: tx signature, network, payer); body carries receipt_url")

    if MODE == "dry-run":
        step("DRY RUN complete: nothing signed, nothing spent")
        sys.exit(0)

    # ---- sign (and with --pay, send) with the official x402 SVM client -------
    secret = os.environ.get("SOLANA_PAYER_SECRET", "").strip()
    if not secret:
        path = os.path.expanduser(os.environ.get("SOLANA_PAYER_FILE", "~/.hubvibe-solana-key"))
        if os.path.isfile(path):
            secret = open(path).read().strip()
    if not secret:
        stop(3, "no payer key: set SOLANA_PAYER_SECRET (base58 keypair) or SOLANA_PAYER_FILE")

    from solders.keypair import Keypair
    from x402 import max_amount, x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.svm import KeypairSigner
    from x402.mechanisms.svm.exact import register_exact_svm_client

    keypair = Keypair.from_base58_string(secret)
    step(f"Payer {keypair.pubkey()} on {req['network']} via {RPC}")
    client = x402ClientSync()
    # Buyer-side guards: the library's default $1 cap must be raised to the
    # quoted price, and the policy refuses anything above it.
    client.set_spend_controls({"max_amount_per_payment": "$%.2f" % EXPECT_PRICE})
    register_exact_svm_client(client, KeypairSigner(keypair), networks=req["network"],
                              policies=[max_amount(int(round(EXPECT_PRICE * ATOMIC_PER_USD)))],
                              rpc_url=RPC)
    http = x402HTTPClientSync(client)
    headers, payload = http.handle_402_response(dict(unpaid.headers), unpaid.content, url)
    sig = headers.get("PAYMENT-SIGNATURE") or headers.get("payment-signature") or ""
    if not sig:
        stop(1, f"the client produced no PAYMENT-SIGNATURE header (headers: {list(headers)})")
    ok(f"payment payload built and signed ({len(sig)} chars)")

    print(f"\n  curl -sS -D - -X POST '{url}' -H 'content-type: application/json' \\\n"
          f"       -H 'PAYMENT-SIGNATURE: {sig}' \\\n"
          f"       -d '{json.dumps(BODY)}'")
    if MODE == "sign-only":
        step("SIGN-ONLY complete: payload printed, NOT sent (blockhash expires in ~1 minute)")
        sys.exit(0)

    step("Sending the paid request (this spends 5.00 USDC on Solana)")
    paid = s.post(url, json=BODY, headers=headers)
    print(f"  HTTP {paid.status_code}")
    hdr = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE") or ""
    if hdr:
        try:
            print("  settlement:", json.dumps(decode_header(hdr))[:400])
        except Exception as exc:  # pragma: no cover
            print("  settlement header (undecoded):", hdr[:120], exc)
    if paid.status_code != 200:
        stop(1, f"not delivered: {paid.text[:300]}")
    out = paid.json()
    ok(f"status {out['status']} | worker {out['worker']} | price ${out['price_usd']:.2f}")
    ok(f"receipt {BASE}{out['receipt_url']}")
    ok(f"result keys {list(out['result'].keys())}")
    step("PAID CALL SETTLED ON SOLANA")
PY
