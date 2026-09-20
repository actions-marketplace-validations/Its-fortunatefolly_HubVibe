#!/usr/bin/env bash
# Keep this node listed, and freshly listed, in every facilitator's Bazaar.
#
#     bash scripts/refresh-listings.sh             # every facilitator below
#     DRY_RUN=1 bash scripts/refresh-listings.sh   # what it would spend, no money
#     ONLY=payai bash scripts/refresh-listings.sh  # just one
#
# WHY
#
# A facilitator catalogs a resource when a payment carrying the Bazaar record
# reaches it, and it stamps that record with the time. So a listing is only as
# current as the last payment through THAT facilitator: settle everything
# through one and the other's records quietly age, carrying last month's price
# and last month's description. The price change on 2026-09-13 made that
# concrete -- both indexes advertised $0.03 while the node charged $0.05 until
# each was re-paid.
#
# This walks every facilitator in turn: switch to it, pay each route once
# (and each MCP tool, where that facilitator indexes them), then put the box
# back on the facilitator it started on. Safe to run from cron.
#
# WHAT IT COSTS, AND WHERE THE MONEY GOES
#
# Every call is a real payment out of the box's payer wallet and into the
# pay-to wallet -- both the owner's, so a cycle moves money from one pocket to
# the other and costs only what the chain takes, which for x402 is nothing
# (the facilitator pays the gas). The payer wallet still drains, so it needs
# topping up from the pay-to occasionally. The balance is checked BEFORE
# anything is spent: a half-finished cycle leaves one index fresh, one stale,
# and the box possibly on the wrong facilitator.
#
# WHICH FACILITATORS INDEX MCP
#
# Measured 2026-09-13 by reading both indexes end to end: PayAI held 31
# `type: mcp` records, Coinbase's Bazaar exactly 5 (and none of ours, after
# two paid attempts that carried a valid record). So MCP registration is
# attempted only where it has been observed to work. Re-measure before
# changing this -- `type` counts are in the index itself:
#   curl -s '<facilitator>/discovery/resources?limit=100&offset=0' | grep -c '"mcp"'

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO_ROOT/deploy/vps}"
ENV_FILE="$COMPOSE_DIR/.env"
BASE="${BASE:-https://hubvibe-io.com}"
DRY_RUN="${DRY_RUN:-}"
ONLY="${ONLY:-}"

# name|url|register-mcp?
FACILITATORS="
payai|https://facilitator.payai.network|yes
coinbase|https://api.cdp.coinbase.com/platform/v2/x402|no
"
ROUTES="/audit/wcag /audit/seo /audit/security /audit/performance /audit/bundle"
MCP_TOOLS="audit_wcag audit_seo audit_security audit_performance audit_bundle"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

# Every script here that spends money refuses Cloud Shell by name: a temporary
# terminal has its own home directory, so it finds no wallet and reports a
# wallet problem when the only problem is the machine.
if [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  die "this is Google Cloud Shell. The payer wallet and its key live on the VPS -- run this on the box, in ~/HubVibe."
fi

[ -f "$ENV_FILE" ] || die "no $ENV_FILE -- run this on the box where the stack is installed."
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

# The x402 client lives in the private environment first-paid-call.sh builds
# beside the wallet key (a fresh Ubuntu box ships python3 with no pip and
# PEP 668 refuses system installs). Use it when it is there. The box's bare
# python3 has no eth_account, and on 2026-09-14 that came out as "no payer
# wallet" -- which sent the owner looking at the wrong thing.
VENV="${HUBVIBE_VENV:-${HOME:-/tmp}/.hubvibe-venv}"
[ -x "$VENV/bin/python3" ] && export PATH="$VENV/bin:$PATH"
python3 -c 'import x402, eth_account' 2>/dev/null \
  || die "this python3 cannot import x402/eth_account and there is no environment at $VENV. first-paid-call.sh builds one before it pays; by hand: python3 -m venv $VENV && $VENV/bin/pip install 'x402[evm,extensions]==2.22.0' eth-account httpx"

read_env() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r' | sed 's:/*$::'; }
STARTED_ON="$(read_env X402_FACILITATOR_URL)"
[ -n "$STARTED_ON" ] || die "no X402_FACILITATOR_URL in $ENV_FILE"

# Leaving the box on a facilitator it did not start on is the one way this
# script can do lasting harm, so put it back however we exit.
restore() {
  local now
  now="$(read_env X402_FACILITATOR_URL)"
  if [ -n "$STARTED_ON" ] && [ "$now" != "$STARTED_ON" ]; then
    step "Putting the box back on $STARTED_ON"
    bash "$REPO_ROOT/scripts/switch-facilitator.sh" "$STARTED_ON" >/dev/null 2>&1 \
      && ok "restored" \
      || warn "COULD NOT RESTORE -- the box is on $now, not $STARTED_ON. Fix by hand: bash scripts/switch-facilitator.sh $STARTED_ON"
  fi
}
trap restore EXIT

price_of() {
  case "$1" in
    */bundle) printf '0.15' ;;
    *)        printf '0.05' ;;
  esac
}

# --- what this will cost, before a cent moves ------------------------------

step "Planning"
TOTAL=0
PLAN=""
while IFS='|' read -r name url mcp; do
  [ -n "${name:-}" ] || continue
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
  for route in $ROUTES; do
    TOTAL=$(python3 -c "print(round($TOTAL + $(price_of "$route"), 2))")
  done
  PLAN="$PLAN  $name: 5 routes"
  if [ "$mcp" = "yes" ]; then
    for tool in $MCP_TOOLS; do
      case "$tool" in
        *bundle) TOTAL=$(python3 -c "print(round($TOTAL + 0.15, 2))") ;;
        *)       TOTAL=$(python3 -c "print(round($TOTAL + 0.05, 2))") ;;
      esac
    done
    PLAN="$PLAN + 5 MCP tools"
  fi
  PLAN="$PLAN
"
done <<EOF
$(printf '%s\n' "$FACILITATORS" | sed '/^[[:space:]]*$/d')
EOF
[ -n "$PLAN" ] || die "nothing to do (ONLY=$ONLY matched no facilitator)"
printf '%s' "$PLAN"
ok "this cycle spends \$$TOTAL from the payer wallet into the pay-to wallet"

# --- can the payer afford the whole cycle? ---------------------------------

step "Checking the payer wallet"
BALANCE="$(
  HUBVIBE_WALLET_FILE="${HUBVIBE_WALLET_FILE:-$HOME/.hubvibe-wallet-key}" \
  BASE_RPC="${BASE_RPC:-https://mainnet.base.org}" python3 - <<'PY'
import json, os, urllib.request, sys
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
path = os.environ["HUBVIBE_WALLET_FILE"]
try:
    from eth_account import Account
except ImportError as exc:
    print("ERR python3 cannot import eth_account (%s)" % exc); sys.exit(0)
try:
    key = open(path).read().strip()
    address = Account.from_key(key).address
except Exception as exc:
    print("ERR no payer wallet at %s (%s)" % (path, exc)); sys.exit(0)
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
    {"to": USDC, "data": "0x70a08231" + "0" * 24 + address[2:]}, "latest"]}).encode()
try:
    # Every public Base RPC (mainnet.base.org, publicnode, drpc, 1rpc) answers
    # 403 to a request with no User-Agent -- measured from the box 2026-09-14.
    request = urllib.request.Request(os.environ["BASE_RPC"], data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "hubvibe-refresh-listings/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)["result"]
    print("%s %.6f" % (address, int(result, 16) / 1e6))
except Exception as exc:
    # An unreadable chain is not an empty wallet. Say which one it is: this
    # repo has reported "no money" for "no answer" before, and the two call
    # for completely different actions.
    print("ERR could not read the balance (%s)" % exc)
PY
)"
case "$BALANCE" in
  ERR*) die "${BALANCE#ERR }" ;;
esac
PAYER="${BALANCE%% *}"
HELD="${BALANCE##* }"
ok "payer $PAYER holds \$$HELD USDC"
SHORT=$(python3 -c "print('yes' if $HELD < $TOTAL else 'no')")
if [ "$SHORT" = "yes" ]; then
  die "$(python3 -c "print('the cycle needs \$%.2f and the wallet holds \$%.2f -- short \$%.2f. Send USDC on Base to %s (no ETH needed).' % ($TOTAL, $HELD, $TOTAL - $HELD, '$PAYER'))")"
fi

if [ -n "$DRY_RUN" ]; then
  step "DRY_RUN set -- nothing was paid and the facilitator was not changed"
  exit 0
fi

# --- do it -----------------------------------------------------------------

FAILED=0
while IFS='|' read -r name url mcp; do
  [ -n "${name:-}" ] || continue
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue

  step "$name -- $url"
  if [ "$(read_env X402_FACILITATOR_URL)" != "$url" ]; then
    # switch-facilitator.sh reads the live 402 afterwards and rolls itself
    # back if the rail vanished, so a dead facilitator cannot take us offline.
    bash "$REPO_ROOT/scripts/switch-facilitator.sh" "$url" >/dev/null 2>&1 \
      || { warn "could not switch to $url -- skipping it"; FAILED=1; continue; }
  fi
  ok "on $url"

  for route in $ROUTES; do
    printf '  %-24s ' "$route"
    # Captured, not piped into grep -q: under pipefail, grep -q exiting at
    # the first match sends first-paid-call.sh a SIGPIPE on its next write
    # and the pipeline reports failure -- which is how the 2026-09-14 cycle
    # settled all ten routes (tx hashes in the node log) while printing
    # FAILED for every one of them.
    OUT="$(ROUTE="$route" BASE="$BASE" bash "$REPO_ROOT/scripts/first-paid-call.sh" 2>&1)"
    case "$OUT" in
      *"settled \$"*) printf 'listed\n' ;;
      *) printf '\033[31mFAILED\033[0m\n'; FAILED=1
         printf '%s\n' "$OUT" | tail -n 4 | sed 's/^/        /' ;;
    esac
  done

  if [ "$mcp" = "yes" ]; then
    for tool in $MCP_TOOLS; do
      printf '  %-24s ' "mcp:$tool"
      if TOOL="$tool" BASE="$BASE" python3 "$REPO_ROOT/scripts/register_mcp_tool.py" >/dev/null 2>&1; then
        printf 'listed\n'
      else
        printf '\033[31mFAILED\033[0m\n'; FAILED=1
      fi
    done
  fi
done <<EOF
$(printf '%s\n' "$FACILITATORS" | sed '/^[[:space:]]*$/d')
EOF

# --- read the indexes back -------------------------------------------------

step "What each index says now"
while IFS='|' read -r name url mcp; do
  [ -n "${name:-}" ] || continue
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
  BASE="$BASE" FACILITATOR="$url" NAME="$name" python3 - <<'PY'
import json, os, urllib.request
base = os.environ["FACILITATOR"].rstrip("/")
host = os.environ["BASE"].split("://")[-1].split("/")[0].lower()
found, offset, total = [], 0, 10 ** 9
while offset < total:
    url = "%s/discovery/resources?limit=100&offset=%d" % (base, offset)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            page = json.load(response)
    except Exception as exc:
        print("  NOTE  %s: could not read the index (%s)" % (os.environ["NAME"], exc))
        raise SystemExit(0)
    items = page.get("items") or []
    total = (page.get("pagination") or {}).get("total") or total
    if not items:
        break
    for item in items:
        if host in json.dumps(item).lower():
            found.append((item.get("type"), (item.get("lastUpdated") or "")[:16]))
    offset += 100
kinds = {}
for kind, _ in found:
    kinds[kind] = kinds.get(kind, 0) + 1
newest = max((stamp for _, stamp in found), default="never")
print("  %-9s %d listing(s) %s, newest %s"
      % (os.environ["NAME"], len(found), kinds or "", newest))
PY
done <<EOF
$(printf '%s\n' "$FACILITATORS" | sed '/^[[:space:]]*$/d')
EOF

[ "$FAILED" -eq 0 ] || die "some registrations failed -- see above. The indexes that did refresh are still current."
step "Done"
