#!/usr/bin/env bash
# WHAT IS THE MONEY DOING? One command, from any machine with curl + python3.
#
#     bash scripts/payment-status.sh
#
# or, with nothing checked out (Cloud Shell, the VPS, a laptop):
#
#     curl -fsSL https://raw.githubusercontent.com/Its-fortunatefolly/HubVibe/main/scripts/payment-status.sh | bash
#
# It answers the four questions that decide whether a dollar can move:
#
#   1. WALLETS   -- how much USDC is actually sitting in the receiving
#                   wallets right now, read off Base itself, not from a
#                   dashboard. This is the revenue counter: x402 money lands
#                   on-chain and never appears in Stripe.
#   2. NODE      -- is the service answering at all, and what does its 402
#                   really advertise: price, recipient, network, which rails
#                   can settle.
#   3. THE MATCH -- is the recipient the node advertises actually YOUR
#                   wallet? This check exists because this project once ran
#                   for weeks advertising an address nobody held the key to,
#                   and every gate said "well-formed". Shape is not
#                   ownership; this compares against the affirmed wallets.
#   4. PAYER     -- can the first paid call even be made? (Its wallet needs
#                   USDC on Base, and NO ETH -- the facilitator pays gas.)
#
# Read-only and safe to run anywhere: it makes no payment, changes nothing,
# and never prints a private key. Addresses are public; keys never leave
# the file they live in.
#
# Overrides: BASE (node URL), BASE_RPC (Base RPC endpoint),
# HUBVIBE_WALLET_FILE (paying wallet key file), X402_PAY_TO_ADDRESS.

set -uo pipefail

BASE="${BASE:-https://hubvibe-io.com}"
# Comma-separated, tried in order. The public Base RPCs sit behind bot
# filters that answer HTTP 403 to Python's default User-Agent; the request
# below sends its own, and the next endpoint is tried on any failure.
BASE_RPC="${BASE_RPC:-https://mainnet.base.org,https://base.publicnode.com,https://base-rpc.publicnode.com}"
WALLET_FILE="${HUBVIBE_WALLET_FILE:-${HOME:-/tmp}/.hubvibe-wallet-key}"
export BASE BASE_RPC WALLET_FILE
export EXPECTED_PAY_TO="${X402_PAY_TO_ADDRESS:-0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd}"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }

python3 <<'PYEOF'
import json
import os
import urllib.error
import urllib.request

BASE = os.environ["BASE"].rstrip("/")
RPCS = [u.strip() for u in os.environ["BASE_RPC"].split(",") if u.strip()]
# mainnet.base.org (Cloudflare) answers 403 to "Python-urllib/3.x" -- found
# 2026-09-06 when this script reported the chain unreachable from a machine
# where curl read it fine. Name ourselves, and never fall back to zero.
USER_AGENT = "hubvibe-payment-status/1.0"
WALLET_FILE = os.environ["WALLET_FILE"]
EXPECTED = os.environ["EXPECTED_PAY_TO"]

# USDC on Base mainnet. The asset every x402 payment here moves.
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# The wallets the owner has affirmed as theirs (docs/HANDOFF.md, 2026-09-05).
# Revenue landing in EITHER of these is money in the owner's hands.
OWNER_WALLETS = {
    "0x837c40e2b4e976f43ffb4451ee281a00fa9477dd": "primary (hubvibe.base.eth)",
    "0x37555e884c5eba10f6e816dbecea30965b9b38c0": "alternate (Coinbase/Base app)",
}

BOLD, GREEN, RED, YELLOW, DIM, OFF = "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def head(text):
    print(f"\n{BOLD}== {text}{OFF}")


def ok(text):
    print(f"  {GREEN}OK{OFF}    {text}")


def warn(text):
    print(f"  {YELLOW}NOTE{OFF}  {text}")


def bad(text):
    print(f"  {RED}STOP{OFF}  {text}")


def usdc_balance(address):
    """USDC balance on Base, read straight off the chain. None if unreadable."""
    data = "0x70a08231" + address[2:].rjust(64, "0").lower()
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC, "data": data}, "latest"],
    }).encode()
    for rpc in RPCS:
        try:
            req = urllib.request.Request(
                rpc, data=body,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.load(response).get("result")
            if not result or result == "0x":
                return 0.0
            return int(result, 16) / 1_000_000
        except Exception:
            continue
    return None


def get(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return None, str(exc).encode()


def post_json(url, payload, timeout=60):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:
        return None, {}, str(exc).encode()


# --------------------------------------------------------------------------
head("1. THE WALLETS -- revenue, read off Base itself")
# --------------------------------------------------------------------------
# x402 revenue lands on-chain and never shows in Stripe, so the wallet IS
# the counter. A non-zero balance here is the only proof of x402 income.

total = 0.0
unreadable = False
for address, label in [
    ("0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", "primary (hubvibe.base.eth)"),
    ("0x37555E884c5EbA10f6E816DbecEA30965B9b38C0", "alternate (Coinbase/Base app)"),
]:
    balance = usdc_balance(address)
    if balance is None:
        unreadable = True
        warn(f"{label}: could not read the chain (RPC unreachable from here)")
        print(f"        check by hand: https://basescan.org/address/{address}")
        continue
    total += balance
    marker = ok if balance > 0 else print
    line = f"{label}: ${balance:,.6f} USDC"
    (ok if balance > 0 else (lambda t: print(f"  {DIM}--{OFF}    {t}")))(line)
    print(f"        {DIM}https://basescan.org/address/{address}{OFF}")

if not unreadable:
    if total > 0:
        print(f"\n  {BOLD}{GREEN}Total received: ${total:,.6f} USDC{OFF}")
    else:
        print(f"\n  {BOLD}Total received: $0.00 -- no x402 payment has ever settled.{OFF}")

# --------------------------------------------------------------------------
head(f"2. THE NODE -- is {BASE} serving?")
# --------------------------------------------------------------------------

status, health = get(f"{BASE}/health", timeout=30)
node_live = False
if status == 200:
    node_live = True
    ok(f"the node is up: {health.decode(errors='replace')[:120]}")
elif status is None:
    bad(f"cannot reach {BASE} ({health.decode(errors='replace')[:100]})")
    print("        The service is not deployed there yet, or DNS does not point at it.")
else:
    bad(f"{BASE}/health answered HTTP {status}")
    print(f"        body: {health.decode(errors='replace')[:150]}")

advertised_pay_to = None
if node_live:
    status, headers, raw = post_json(f"{BASE}/audit/wcag", {"url": "https://example.com"})
    try:
        body = json.loads(raw)
    except Exception:
        body = None

    if status == 402 and isinstance(body, dict):
        ok(f"an unpaid call is challenged with HTTP 402 (price {body.get('price')})")
        accepts = body.get("accepts") or []
        x402 = next((a for a in accepts if a.get("scheme") == "exact"), None)
        if x402:
            advertised_pay_to = x402.get("payTo")
            amount = x402.get("maxAmountRequired")
            dollars = f"${int(amount)/1_000_000:.2f}" if str(amount).isdigit() else amount
            ok(f"x402 is LIVE: {dollars} to {advertised_pay_to} on {x402.get('network')}")
        else:
            warn("x402 is NOT advertised on this 402 -- no on-chain rail can settle here")
            print("        (facilitator unreachable, or X402_PAY_TO_ADDRESS unset/refused)")

        lower = {k.lower() for k in headers}
        if "payment-required" in lower:
            ok("the v2 PAYMENT-REQUIRED header is present (modern x402 clients can pay)")
        elif x402:
            warn("no v2 PAYMENT-REQUIRED header -- only older v1 clients can pay")

        rails = [r.get("method") or r.get("protocol") for r in (body.get("other_rails") or [])]
        print(f"        other rails offered: {', '.join(filter(None, rails)) or 'none'}")

        bazaar = ((body.get("extensions") or {}).get("bazaar") or {}).get("info")
        if bazaar:
            ok("the 402 carries a Bazaar discovery record (a payment will index this node)")
        elif x402:
            warn("no Bazaar record on the 402 -- a payment would settle but index nothing")
    elif status is not None:
        bad(f"the paid route answered HTTP {status}, not 402")
        print(f"        body: {raw.decode(errors='replace')[:200]}")

    status, manifest_raw = get(f"{BASE}/.well-known/agent.json")
    if status == 200:
        try:
            methods = json.loads(manifest_raw).get("payment", {}).get("methods", [])
            print(f"        manifest says these rails can settle: {', '.join(methods) or 'NONE'}")
        except Exception:
            pass

# --------------------------------------------------------------------------
head("3. THE MATCH -- is the advertised recipient actually yours?")
# --------------------------------------------------------------------------
# This project once advertised, for weeks, an address nobody held the key
# to. Every format gate passed it. Shape is not ownership -- so compare the
# address the node is telling the world to pay against the affirmed wallets.

if advertised_pay_to is None:
    warn("nothing to check: the node is not advertising an x402 recipient right now")
else:
    owner = OWNER_WALLETS.get(advertised_pay_to.lower())
    if owner:
        ok(f"the node pays YOU -- {advertised_pay_to} is your {owner}")
    else:
        bad(f"the node advertises {advertised_pay_to}, which is NOT an affirmed wallet.")
        print("        Every payment would go to an address you have not claimed.")
        print("        Fix before any traffic arrives: redeploy with")
        print(f"        X402_PAY_TO_ADDRESS={EXPECTED}")

# --------------------------------------------------------------------------
head("4. THE PAYER -- can the first paid call be made from here?")
# --------------------------------------------------------------------------
# The bootstrap payment needs its own wallet: USDC on Base, no ETH (x402
# signs off-chain and the facilitator pays the gas). Never pay from the
# receiving wallet -- a self-transfer proves nothing about a real buyer.

if not os.path.isfile(WALLET_FILE):
    warn(f"no paying wallet on this machine ({WALLET_FILE} not found)")
    print("        Make one:  bash scripts/first-paid-call.sh --new-wallet")
else:
    try:
        from eth_account import Account

        payer = Account.from_key(open(WALLET_FILE).read().strip()).address
    except ImportError:
        payer = None
        warn("eth-account is not installed here, so the payer address cannot be derived")
        print("        pip install eth-account   (the key file itself is untouched)")
    except Exception as exc:
        payer = None
        bad(f"{WALLET_FILE} does not hold a readable private key ({type(exc).__name__})")

    if payer:
        balance = usdc_balance(payer)
        if balance is None:
            warn(f"payer {payer} -- balance unreadable from here")
            print(f"        check: https://basescan.org/address/{payer}")
        elif balance >= 0.05:
            ok(f"payer {payer} holds ${balance:,.6f} USDC -- enough for the first call")
        else:
            bad(f"payer {payer} holds ${balance:,.6f} USDC -- needs at least $0.05")
            print("        Send it USDC ON BASE (no ETH needed; the facilitator pays gas):")
            print(f"        https://basescan.org/address/{payer}")
        if payer.lower() in OWNER_WALLETS:
            bad("the paying wallet IS a receiving wallet -- that is a self-transfer.")
            print("        It proves nothing about whether a real buyer can pay.")
            print("        Use a separate payer: bash scripts/first-paid-call.sh --new-wallet")

# --------------------------------------------------------------------------
head("WHAT IS BLOCKING THE FIRST DOLLAR")
# --------------------------------------------------------------------------

if not node_live:
    print(f"  The node is not serving at {BASE}. Nothing can pay it.")
    print("  Next:  bash scripts/vps-install.sh hubvibe-io.com   (on the box)")
elif advertised_pay_to is None:
    print("  The node is up but advertises no x402 rail, so no agent can pay on-chain.")
    print("  Next:  check X402_FACILITATOR_URL and X402_PAY_TO_ADDRESS in deploy/vps/.env,")
    print("         then: cd deploy/vps && docker compose up -d --build")
elif advertised_pay_to.lower() not in OWNER_WALLETS:
    print("  The node is payable but pays a wallet you have not affirmed. Fix that FIRST.")
else:
    print("  Nothing structural. The node is live and payable to your wallet.")
    print(f"  Next:  BASE={BASE} bash scripts/first-paid-call.sh")
print()
PYEOF
