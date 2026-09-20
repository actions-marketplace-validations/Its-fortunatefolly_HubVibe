#!/usr/bin/env bash
# One command. Start it, send the wallet some USDC whenever you get to it, and
# walk away -- the box does the rest and prints the receipt.
#
# WHY THIS EXISTS
#
# The first paid call is what registers this node in the Bazaar index, and it
# needs exactly two things that no script can do for the owner: money moved
# from their own wallet, and their hands on their own accounts. Everything
# else on that path was still being handed to them as homework -- create a
# wallet, read an address off the screen, go fund it, come back to a terminal
# that has since disconnected, run a second command, read a refusal, run it
# again. Five steps, four of them the machine's own work handed to a person
# who is on a phone.
#
# So this is the whole path as one unattended run: it makes the wallet if
# there is none, prints the one address that needs funding, watches the chain
# until the money lands, and fires the paid call the moment it can pay. The
# owner's part shrinks to a single transfer, done whenever they like, from
# wherever they are.
#
# Usage, on the box:
#     cd ~/HubVibe && bash scripts/go.sh
#
# Overrides:
#     MIN_USDC       balance that counts as fundable   (default 0.05)
#     POLL_SECONDS   seconds between balance reads     (default 30)
#     WAIT_SECONDS   give up waiting after this        (default 7200 = 2h)
#     BASE           the node to pay                   (default the domain)
#     BASE_RPC       comma-separated Base RPC list

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${BASE:-https://hubvibe-io.com}"
MIN_USDC="${MIN_USDC:-0.05}"
POLL_SECONDS="${POLL_SECONDS:-30}"
WAIT_SECONDS="${WAIT_SECONDS:-7200}"
BASE_RPC="${BASE_RPC:-https://mainnet.base.org,https://base.publicnode.com,https://base-rpc.publicnode.com}"
WALLET_FILE="${HUBVIBE_WALLET_FILE:-${HOME:-/tmp}/.hubvibe-wallet-key}"
export BASE BASE_RPC WALLET_FILE

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

# Same refusal as vps-install.sh and first-paid-call.sh, for the same reason:
# the wallet, the key and the node all live on the box. In a temporary
# terminal this would create a wallet nobody can reach, print its address, and
# invite the owner to send real money to it. That is worse than failing.
if [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  die "this is Google Cloud Shell -- a temporary terminal, not the node. A wallet made here disappears with the session, and money sent to it goes with it. Run this on the box (Hostinger: VPS -> Browser terminal; or ssh root@YOUR_VPS_IP), in ~/HubVibe."
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

# ---------------------------------------------------------------------------
# The wallet. Reuse whatever is already there -- never generate a second one
# while the first may hold funds.
# ---------------------------------------------------------------------------
step "Finding the paying wallet"
if [ ! -r "$WALLET_FILE" ]; then
  warn "no wallet on this box yet -- making one"
  bash "$REPO_ROOT/scripts/first-paid-call.sh" --new-wallet >/dev/null 2>&1 \
    || die "could not create a wallet. Run: bash scripts/first-paid-call.sh --new-wallet"
fi

PAYER=$(python3 -c '
import os, sys
try:
    from eth_account import Account
    print(Account.from_key(open(os.environ["WALLET_FILE"]).read().strip()).address)
except Exception as exc:
    sys.exit("%s: %s" % (type(exc).__name__, exc))
' 2>&1) || die "$WALLET_FILE does not hold a readable private key ($PAYER). If it holds nothing, replace it: HUBVIBE_FORCE_NEW_WALLET=1 bash scripts/first-paid-call.sh --new-wallet"
ok "payer $PAYER"

# ---------------------------------------------------------------------------
# The wait. Reading a balance is the only thing between "funded" and "paid",
# and a person should not have to be the one polling it.
# ---------------------------------------------------------------------------
read_balance() {
  python3 -c '
import json, os, sys, urllib.request

RPCS = [u.strip() for u in os.environ["BASE_RPC"].split(",") if u.strip()]
# mainnet.base.org (Cloudflare) answers 403 to "Python-urllib/3.x", so name
# ourselves. An unreadable balance must never read as zero -- that would look
# like "not funded yet" forever, which is the one failure this loop cannot
# afford.
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
data = "0x70a08231" + os.environ["PAYER"][2:].rjust(64, "0").lower()
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": USDC, "data": data}, "latest"]}).encode()
for rpc in RPCS:
    try:
        req = urllib.request.Request(rpc, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "hubvibe-go/1.0"})
        with urllib.request.urlopen(req, timeout=25) as response:
            result = json.load(response).get("result")
        print("%.6f" % (0.0 if not result or result == "0x" else int(result, 16) / 1_000_000))
        sys.exit(0)
    except Exception:
        continue
sys.exit("unreadable")
' 2>/dev/null
}

export PAYER
step "Checking what the payer holds"
BALANCE=$(read_balance) || BALANCE=""

# `--check` answers the two questions a person actually has about an address
# their server made: is it mine, and where is my money. Both were unanswerable
# from a phone on 2026-09-08 -- the owner scanned the address, saw an empty
# page, and could not tell whether the wallet was theirs or the funds were
# gone. Neither was wrong: a wallet holding only USDC and no ETH has NO normal
# transactions, so an explorer's default tab is empty and the money sits one
# tab over under ERC-20 transfers. Reading the balance on-chain answers it
# without a browser, and without spending anything.
if [ "${1:-}" = "--check" ]; then
  printf '\n  \033[1mAddress:\033[0m %s\n' "$PAYER"
  printf '  \033[1mUSDC on Base:\033[0m $%s\n\n' "${BALANCE:-unreadable}"
  printf '  This address is derived from the private key in %s\n' "$WALLET_FILE"
  printf '  on THIS box. That file is the only thing that controls it -- it is\n'
  printf '  not in any wallet app and there is no recovery phrase, which is why\n'
  printf '  it looks unfamiliar. Keep only what one call costs in it.\n\n'
  printf '  In a browser the USDC is under the ERC-20 tab, not Transactions:\n'
  printf '    https://basescan.org/token/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913?a=%s\n\n' "$PAYER"
  # This reads ONE token on ONE chain. A zero here is not proof of a missing
  # transfer -- USDC on Ethereum, bridged USDbC, or plain ETH all read $0.00
  # and look identical to "never arrived". Hand that question to the tool
  # that actually answers it rather than leaving a scary number alone.
  if python3 -c "import sys; sys.exit(0 if float('${BALANCE:-0}') > 0 else 1)"; then
    printf '  Enough to pay: drop --check and this makes the call.\n\n'
  else
    printf '  \033[1mThis reads only native USDC on Base.\033[0m A zero here does NOT mean\n'
    printf '  the money is gone: USDC on another chain, bridged USDbC, or ETH all\n'
    printf '  read $0.00. Look across every chain:\n\n'
    printf '      bash %s/scripts/find-my-money.sh\n\n' "$REPO_ROOT"
  fi
  exit 0
fi

if [ -z "$BALANCE" ]; then
  warn "the chain is unreadable from this box right now -- will keep trying"
  BALANCE="0"
fi

FUNDED=$(python3 -c "import sys; sys.exit(0 if float('${BALANCE:-0}') >= float('$MIN_USDC') else 1)" && echo yes || echo no)

if [ "$FUNDED" = "no" ]; then
  printf '\n  \033[1mSend USDC on Base to this address:\033[0m\n\n'
  printf '      \033[1m%s\033[0m\n\n' "$PAYER"
  printf '  $0.25 is plenty -- the call spends $0.05. This wallet needs NO ETH:\n'
  printf '  x402 signs off-chain and the facilitator pays the gas. (The transfer\n'
  printf '  that funds it comes out of YOUR wallet and pays gas as usual -- that\n'
  printf '  is the one place on this path where gas is yours to cover.)\n\n'
  printf '  Nothing else to do. This will notice the money and make the call.\n'
  printf '  Safe to leave running; Ctrl-C any time and re-run later.\n\n'

  step "Waiting for the money"
  WAITED=0
  LAST_SHOWN=""
  while [ "$WAITED" -lt "$WAIT_SECONDS" ]; do
    sleep "$POLL_SECONDS"
    WAITED=$((WAITED + POLL_SECONDS))
    BALANCE=$(read_balance) || BALANCE=""
    [ -z "$BALANCE" ] && continue
    if [ "$BALANCE" != "$LAST_SHOWN" ]; then
      printf '  %s  balance $%s USDC\n' "$(date -u +%H:%M:%SZ)" "$BALANCE"
      LAST_SHOWN="$BALANCE"
    fi
    if python3 -c "import sys; sys.exit(0 if float('$BALANCE') >= float('$MIN_USDC') else 1)"; then
      FUNDED=yes
      break
    fi
  done
fi

if [ "$FUNDED" != "yes" ]; then
  die "no money arrived within ${WAIT_SECONDS}s. Nothing was spent. Send USDC on Base to $PAYER and run this again."
fi

ok "funded: \$$BALANCE USDC"

# ---------------------------------------------------------------------------
# The call. Everything this script exists for happens in the next line.
# ---------------------------------------------------------------------------
step "Making the paid call"
exec bash "$REPO_ROOT/scripts/first-paid-call.sh"
