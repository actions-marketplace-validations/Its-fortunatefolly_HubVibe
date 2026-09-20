#!/usr/bin/env bash
# Where did the money go? Reads one address across every chain it could have
# landed on, and says which one holds it.
#
# WHY THIS EXISTS
#
# On 2026-09-08 the owner funded the payer wallet and the box reported
# nothing had arrived. Nothing was lost -- but every tool here reads exactly
# one token on exactly one chain (native USDC on Base), so USDC sent on
# Ethereum, or bridged USDbC on Base, or plain ETH, all read as $0.00. On
# screen that is identical to "never received", which is the single most
# alarming thing this project can say to someone who just sent real money.
#
# An EVM address is the same address on every EVM chain. Funds sent to the
# payer on the wrong chain are sitting at the payer's address on THAT chain,
# spendable with the same private key. This script finds them.
#
# Usage, anywhere with internet (it is read-only and touches no keys beyond
# deriving an address from the local wallet file, if there is one):
#     bash scripts/find-my-money.sh
#     bash scripts/find-my-money.sh 0xSOMEADDRESS

set -uo pipefail

WALLET_FILE="${HUBVIBE_WALLET_FILE:-${HOME:-/tmp}/.hubvibe-wallet-key}"
# The payer this project created on the box. Used only when no address is
# given and no wallet file is readable, so the script still answers the
# question it exists to answer.
DEFAULT_ADDRESS="0x104feA79F30b4fB4Da86B6D65951217F914bdd35"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

ADDRESS="${1:-}"
SOURCE="the address you gave"
if [ -z "$ADDRESS" ]; then
  if [ -r "$WALLET_FILE" ]; then
    ADDRESS=$(python3 -c '
import os, sys
try:
    from eth_account import Account
    print(Account.from_key(open(os.environ["WALLET_FILE"]).read().strip()).address)
except Exception:
    sys.exit(1)
' 2>/dev/null) && SOURCE="this box's wallet file ($WALLET_FILE)"
  fi
fi
if [ -z "$ADDRESS" ]; then
  ADDRESS="$DEFAULT_ADDRESS"
  SOURCE="the payer this project created (no readable wallet file here)"
fi
export ADDRESS

case "$ADDRESS" in
  0x*) [ ${#ADDRESS} -eq 42 ] || die "that is not a 42-character 0x address: $ADDRESS" ;;
  *) die "an EVM address starts with 0x: $ADDRESS" ;;
esac

step "Looking for money held by"
printf '      \033[1m%s\033[0m\n' "$ADDRESS"
printf '      (%s)\n' "$SOURCE"

step "Reading every chain it could be on"
python3 <<'PY'
import json, os, sys, urllib.request

ADDRESS = os.environ["ADDRESS"]

# One entry per place the money could be. USDC is a DIFFERENT contract on
# every chain -- reading Base's contract address on Ethereum returns nothing,
# which is exactly how "the money vanished" gets reported.
CHAINS = [
    ("Base", "https://mainnet.base.org", "ETH", [
        ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        ("USDbC (bridged)", "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", 6),
    ]),
    ("Ethereum", "https://ethereum-rpc.publicnode.com", "ETH", [
        ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    ]),
    ("Arbitrum", "https://arbitrum-one-rpc.publicnode.com", "ETH", [
        ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    ]),
    ("Optimism", "https://optimism-rpc.publicnode.com", "ETH", [
        ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
    ]),
    ("Polygon", "https://polygon-bor-rpc.publicnode.com", "POL", [
        ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    ]),
]

GREEN, YELLOW, DIM, OFF = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def call(rpc, method, params):
    """None means the chain could not be read. Never conflate that with zero:
    an unreachable RPC reporting $0 is how a checker tells someone their money
    is gone when it is sitting right there."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    try:
        req = urllib.request.Request(rpc, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "hubvibe-find-my-money/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response).get("result")
    except Exception:
        return None


found = []
unreachable = []
for name, rpc, coin, tokens in CHAINS:
    native = call(rpc, "eth_getBalance", [ADDRESS, "latest"])
    if native is None:
        unreachable.append(name)
        print(f"  {YELLOW}NOTE{OFF}  {name}: could not be read from here "
              f"-- this is NOT a zero balance")
        continue
    lines = []
    amount = int(native, 16) / 1e18
    if amount > 0:
        lines.append((f"{amount:.6f} {coin}", amount))
    for label, contract, decimals in tokens:
        data = "0x70a08231" + ADDRESS[2:].rjust(64, "0").lower()
        raw = call(rpc, "eth_call", [{"to": contract, "data": data}, "latest"])
        if raw is None or raw == "0x":
            continue
        value = int(raw, 16) / (10 ** decimals)
        if value > 0:
            lines.append((f"${value:,.6f} {label}", value))
    if lines:
        for text, _ in lines:
            print(f"  {GREEN}FOUND{OFF} {name}: {text}")
        found.append(name)
    else:
        print(f"  {DIM}--{OFF}    {name}: empty")

print()
if found:
    print(f"  The money is on: {', '.join(found)}.")
    if found == ["Base"]:
        print("  That is the right chain. If the paid call still says the")
        print("  wallet is empty, re-run it -- the balance is there now.")
    else:
        others = [c for c in found if c != "Base"]
        if others:
            print(f"  {', '.join(others)} is NOT where this node settles.")
            print("  Nothing is lost: the same private key controls this address")
            print("  on every one of these chains. Bridge it to Base, or send a")
            print("  fresh $0.25 of USDC on Base and deal with the stranded")
            print("  funds later -- the call only needs $0.05.")
elif unreachable:
    print("  Nothing found, but some chains could not be read from here, so")
    print("  this is not a conclusion. Try again, or check in a browser:")
    print(f"  https://blockscan.com/address/{ADDRESS}")
    sys.exit(2)
else:
    print("  Every chain above reads empty for this address.")
    print("  If a send left your wallet, open the transaction in your wallet")
    print("  app and check the destination address character by character --")
    print("  a mistyped address is the one case the funds are not recoverable.")
    print(f"  Full history: https://blockscan.com/address/{ADDRESS}")
    sys.exit(1)
PY
