#!/usr/bin/env bash
# Turn on every payment rail that can actually settle -- in ONE deploy.
#
#   bash scripts/go-live.sh
#
# There are two go-live scripts already, one per rail, and each ends by
# exec'ing repair-and-deploy.sh. Running both means two source deploys, two
# waits, and a window in between where one rail is live and the other is in
# whatever state the first script left it. On a phone that is the difference
# between doing this and not doing it.
#
# This resolves both recipients first, writes them in a single
# `services update`, and deploys once.
#
#   x402       -- self-custody Base wallet. USDC stays on-chain; it does NOT
#                 appear in Stripe. Facilitator settles, we hold the key.
#   mpp-tempo  -- Stripe-custodied Tempo deposit address. USDC is offramped by
#                 Stripe into the Stripe balance. 1c minimum, so it is the one
#                 Stripe rail that works at $0.05.
#
# mpp-stripe (cards / Shared Payment Tokens) is deliberately absent: Stripe's
# minimum for an SPT charge is 0.50 USD and every route here is $0.05-$0.15.
# It is gated on the amount in code and is not something a deploy turns on.
#
# Each rail is independent. One that cannot be configured is left OFF and the
# other still goes live -- never advertised with a recipient that cannot
# receive. That is the whole discipline of this codebase.
#
# Usage:
#   bash scripts/go-live.sh
#   X402_PAY_TO_ADDRESS=0x...          bash scripts/go-live.sh   # different wallet
#   MPP_TEMPO_RECIPIENT_ADDRESS=0x...  bash scripts/go-live.sh   # skip minting
#   RAILS=x402  bash scripts/go-live.sh                          # one rail only
#   RAILS=tempo bash scripts/go-live.sh

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
PROJECT="${PROJECT:-resolver-time}"
RAILS="${RAILS:-both}"

# PayAI: free, keyless, Base mainnet, x402 v1 and v2, and it indexes paid
# endpoints in its /discovery/resources on first settlement so agents can
# find this node by capability -- no registration, no API key, no business
# review. xpay.sh settles but runs no discovery index, so every caller who
# would have found this node through capability search never arrived; Dexter
# indexes but its settlement signer ran out of gas (read 2026-09-12), and a
# facilitator that cannot settle sells nothing. Coinbase CDP (the x402
# Bazaar's facilitator) needs CDP_API_KEY_ID/CDP_API_KEY_SECRET beside it;
# the CDP guard ignores those credentials for non-Coinbase hosts, so this
# stays exactly one env var either way.
FACILITATOR="${X402_FACILITATOR:-https://facilitator.payai.network}"

STRIPE_SECRET_NAME="${STRIPE_SECRET_NAME:-SECRET_STRIPE_KEY}"
# Preview-only and version-pinned. An older version 404s the endpoint, which
# reads as "crypto is not enabled on this account" rather than "wrong API
# version" -- a wrong diagnosis that has already cost this project days.
STRIPE_API_VERSION="${STRIPE_API_VERSION:-2026-07-29.preview}"
TEMPO_NETWORK="${TEMPO_NETWORK:-tempo}"

# The owner's own Base wallet, affirmed in docs/HANDOFF.md ("the x402
# recipient is RESOLVED -- a self-custody Base wallet"). It is the default so
# that the common case is one short line with nothing to paste; any address
# supplied in the environment wins over it.
DEFAULT_X402_PAY_TO="0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"

# Well-formed addresses that must NEVER be advertised.
#
# Shape is not ownership. `0x2b3b...` is 0x + 40 hex, passes every format gate
# in this repo, sat deployed on this service as X402_PAY_TO_ADDRESS -- and the
# owner does not recognise it. `0x32b0...` is the test-suite constant, which
# exists to make the rail inspectable locally and whose key nobody holds.
# Reusing either because it "looks fine" is the same error as the zero
# address, one level up: a format check answers a question nobody asked.
UNAFFIRMED_ADDRESSES="
0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256
0x32b08c5e927c69877d0fcab35618c265674922bc
"
ZERO_ADDRESS="0x0000000000000000000000000000000000000000"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mOFF\033[0m   %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

case "$RAILS" in
  both|x402|tempo) ;;
  *) die "RAILS must be one of: both, x402, tempo (got: $RAILS)" ;;
esac

lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

# Returns 0 when the address is one this repo knows nobody can claim.
is_unaffirmed() {
  local needle candidate
  needle="$(lower "$1")"
  for candidate in $UNAFFIRMED_ADDRESSES; do
    [ "$needle" = "$(lower "$candidate")" ] && return 0
  done
  return 1
}

# Prints nothing and returns non-zero when the address cannot receive money.
# The reason goes to stdout so the caller can say it out loud -- a rail that
# silently fails closed reads as "this was never configured".
address_fault() {
  local addr="$1"
  if [ -z "$addr" ]; then
    echo "is not set"; return 1
  fi
  if [ "$addr" = "__FROM_SECRET__" ]; then
    echo "comes from Secret Manager, so its shape cannot be checked from here (an EVM address is public -- keep it a plain env var)"
    return 1
  fi
  if [ "$(lower "$addr")" = "$ZERO_ADDRESS" ]; then
    echo "is the ZERO ADDRESS -- well-formed and unownable; USDC reverts transfers to it"
    return 1
  fi
  if ! printf '%s' "$addr" | grep -qiE '^0x[0-9a-f]{40}$'; then
    echo "is not 0x + 40 hex (this one has $(( ${#addr} - 2 )) characters)"
    return 1
  fi
  if is_unaffirmed "$addr"; then
    echo "is an address nobody here holds the key to (see UNAFFIRMED_ADDRESSES in this script)"
    return 1
  fi
  return 0
}

# --- read the live service once ---------------------------------------------

SVC_JSON="$(mktemp)"
trap 'rm -f "$SVC_JSON"' EXIT

step "Reading the live service"
# --format=json, never flattened: flattened pads names with alignment spaces,
# so every grep against it silently matches nothing. That shipped three times.
gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format=json > "$SVC_JSON" 2>/dev/null \
  || die "cannot read $SERVICE in $REGION (project $PROJECT). Wrong project, or not authenticated?"
ok "read $SERVICE in $REGION"

read_env() {
  python3 -c '
import json, sys
name = sys.argv[1]
spec = json.load(open(sys.argv[2]))["spec"]["template"]["spec"]
for c in spec.get("containers", []):
    for e in c.get("env") or []:
        if e.get("name") == name:
            print(e["value"] if "value" in e else "__FROM_SECRET__")
            sys.exit(0)
' "$1" "$SVC_JSON" 2>/dev/null
}

LIVE_X402="$(read_env X402_PAY_TO_ADDRESS)"
LIVE_FACILITATOR="$(read_env X402_FACILITATOR_URL)"
LIVE_TEMPO="$(read_env MPP_TEMPO_RECIPIENT_ADDRESS)"

SET_VARS=()
REMOVE_VARS=()
X402_ON=0
TEMPO_ON=0
X402_ADDR=""
TEMPO_ADDR=""

# --- rail 1: x402 (self-custody Base) ----------------------------------------

step "x402 -- USDC on Base, into a wallet you hold"
if [ "$RAILS" = "tempo" ]; then
  warn "skipped (RAILS=$RAILS)"
else
  X402_ADDR="${X402_PAY_TO_ADDRESS:-$DEFAULT_X402_PAY_TO}"

  if FAULT="$(address_fault "$X402_ADDR")"; then
    ok "recipient ${X402_ADDR:0:6}...${X402_ADDR: -4} can receive"
    X402_ON=1
  else
    bad "the x402 recipient $FAULT"
    if [ -n "${X402_PAY_TO_ADDRESS:-}" ]; then
      die "X402_PAY_TO_ADDRESS was supplied by hand and cannot be used. Nothing was changed."
    fi
    warn "x402 stays OFF. Supply one with:"
    warn "  X402_PAY_TO_ADDRESS=0x... bash scripts/go-live.sh"
  fi

  if [ "$X402_ON" -eq 1 ]; then
    # Both variables or neither: is_configured() needs the pair, so setting one
    # alone leaves the rail inert while looking half-done in the console.
    [ "$(lower "$LIVE_X402")" != "$(lower "$X402_ADDR")" ] \
      && SET_VARS+=("X402_PAY_TO_ADDRESS=$X402_ADDR")
    [ "$LIVE_FACILITATOR" != "$FACILITATOR" ] \
      && SET_VARS+=("X402_FACILITATOR_URL=$FACILITATOR")
    if [ "$(lower "$LIVE_X402")" = "$(lower "$X402_ADDR")" ] \
       && [ "$LIVE_FACILITATOR" = "$FACILITATOR" ]; then
      ok "already live with this recipient and facilitator -- nothing to change"
    else
      ok "will set the recipient and facilitator ($FACILITATOR)"
    fi
    warn "x402 revenue lands ON-CHAIN, not in Stripe. The wallet is the counter."
  else
    # Never leave a rail advertised at a recipient we just refused.
    if [ -n "$LIVE_X402" ] && [ "$LIVE_X402" != "__FROM_SECRET__" ]; then
      REMOVE_VARS+=("X402_PAY_TO_ADDRESS")
      [ -n "$LIVE_FACILITATOR" ] && REMOVE_VARS+=("X402_FACILITATOR_URL")
      warn "removing the deployed x402 variables so nothing unsettleable is advertised"
    fi
  fi
fi

# --- rail 2: mpp-tempo (Stripe-custodied) ------------------------------------

step "mpp-tempo -- USDC on Tempo, offramped by Stripe into the Stripe balance"
if [ "$RAILS" = "x402" ]; then
  warn "skipped (RAILS=$RAILS)"
else
  TEMPO_ADDR="${MPP_TEMPO_RECIPIENT_ADDRESS:-}"
  TEMPO_SOURCE="the environment"

  if [ -z "$TEMPO_ADDR" ] && [ -n "$LIVE_TEMPO" ] && address_fault "$LIVE_TEMPO" >/dev/null; then
    # Reuse what is already deployed rather than minting a second address on
    # the account every time this runs. Addresses accumulate otherwise.
    TEMPO_ADDR="$LIVE_TEMPO"
    TEMPO_SOURCE="the live service"
  fi

  if [ -z "$TEMPO_ADDR" ]; then
    step "  Minting a Stripe crypto deposit address on $TEMPO_NETWORK"
    # Read the key from Secret Manager rather than having it pasted: a live
    # Stripe secret in shell history is a credential leak with a long tail,
    # and this is meant to be run from a phone.
    STRIPE_KEY="${STRIPE_SECRET_KEY:-}"
    [ -n "$STRIPE_KEY" ] || STRIPE_KEY="$(gcloud secrets versions access latest \
      --secret="$STRIPE_SECRET_NAME" --project="$PROJECT" 2>/dev/null)"

    if [ -z "$STRIPE_KEY" ]; then
      bad "could not read secret $STRIPE_SECRET_NAME -- cannot mint an address"
    else
      RESPONSE="$(curl -sS -m 30 https://api.stripe.com/v1/crypto/deposit_addresses \
        -u "$STRIPE_KEY:" \
        -H "Stripe-Version: $STRIPE_API_VERSION" \
        -d "network=$TEMPO_NETWORK" 2>&1)"
      MINTED="$(printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit()
if "error" in body:
    err = body["error"]
    print("ERROR\t%s" % (err.get("message") or err.get("type") or "unknown"))
    sys.exit()
# The address may come back flat or nested under the network name. Read
# whichever is there rather than assuming one shape of a preview API.
addr = body.get("address")
if not addr:
    for value in body.values():
        if isinstance(value, dict) and value.get("address"):
            addr = value["address"]
            break
print(addr or "")
' 2>/dev/null)"

      case "$MINTED" in
        ERROR*)
          bad "Stripe refused: $(printf '%s' "$MINTED" | cut -f2-)"
          warn "if it says the endpoint does not exist, stablecoins are not enabled"
          warn "on the account yet; if it names the API version, override it with"
          warn "  STRIPE_API_VERSION=<newer> bash scripts/go-live.sh"
          ;;
        "")
          bad "could not read an address out of Stripe's response"
          printf '        %s\n' "$(printf '%s' "$RESPONSE" | head -c 300)"
          ;;
        *)
          TEMPO_ADDR="$MINTED"
          TEMPO_SOURCE="a fresh Stripe mint"
          ;;
      esac
    fi
  fi

  if [ -n "$TEMPO_ADDR" ]; then
    if FAULT="$(address_fault "$TEMPO_ADDR")"; then
      ok "recipient ${TEMPO_ADDR:0:6}...${TEMPO_ADDR: -4} from $TEMPO_SOURCE can receive"
      TEMPO_ON=1
      if [ "$(lower "$LIVE_TEMPO")" = "$(lower "$TEMPO_ADDR")" ]; then
        ok "already live with this recipient -- nothing to change"
      else
        SET_VARS+=("MPP_TEMPO_RECIPIENT_ADDRESS=$TEMPO_ADDR")
      fi
    else
      bad "the tempo recipient $FAULT"
      [ -n "${MPP_TEMPO_RECIPIENT_ADDRESS:-}" ] \
        && die "MPP_TEMPO_RECIPIENT_ADDRESS was supplied by hand and cannot be used. Nothing was changed."
    fi
  fi

  if [ "$TEMPO_ON" -eq 0 ]; then
    bad "mpp-tempo stays OFF"
    if [ -n "$LIVE_TEMPO" ] && [ "$LIVE_TEMPO" != "__FROM_SECRET__" ]; then
      REMOVE_VARS+=("MPP_TEMPO_RECIPIENT_ADDRESS")
      warn "removing the deployed recipient so nothing unsettleable is advertised"
    fi
  fi
fi

# --- write both, once --------------------------------------------------------

step "Applying the configuration"
if [ ${#SET_VARS[@]} -eq 0 ] && [ ${#REMOVE_VARS[@]} -eq 0 ]; then
  ok "already correct -- no revision needed for configuration"
else
  UPDATE_ARGS=()
  if [ ${#SET_VARS[@]} -gt 0 ]; then
    IFS=','; UPDATE_ARGS+=("--update-env-vars=${SET_VARS[*]}"); unset IFS
  fi
  if [ ${#REMOVE_VARS[@]} -gt 0 ]; then
    IFS=','; UPDATE_ARGS+=("--remove-env-vars=${REMOVE_VARS[*]}"); unset IFS
  fi
  # One call, so both rails land in one revision. Two calls would mean two
  # revisions and a window where the service is half-configured.
  gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" \
    "${UPDATE_ARGS[@]}" \
    || die "setting the variables failed -- the running revision is untouched"
  ok "written in one revision"
fi

step "Rails that will be advertised"
if [ "$X402_ON" -eq 1 ]; then
  ok "x402      -> on-chain, wallet ${X402_ADDR:0:6}...${X402_ADDR: -4}"
elif [ "$RAILS" = "tempo" ]; then
  warn "x402      -> not touched (RAILS=$RAILS)"
else
  bad "x402      -> off"
fi
if [ "$TEMPO_ON" -eq 1 ]; then
  ok "mpp-tempo -> Stripe balance, ${TEMPO_ADDR:0:6}...${TEMPO_ADDR: -4}"
elif [ "$RAILS" = "x402" ]; then
  warn "mpp-tempo -> not touched (RAILS=$RAILS)"
else
  bad "mpp-tempo -> off"
fi
warn "mpp-stripe -> off by design: Stripe's SPT floor is 50c, routes are 3-10c"

if [ "$X402_ON" -eq 0 ] && [ "$TEMPO_ON" -eq 0 ] && [ "$RAILS" = "both" ]; then
  warn ""
  warn "NO machine payment rail will be advertised. That is correct -- every"
  warn "rail available is one that cannot settle -- and it is also the whole"
  warn "blocker to a first paid call. Deploying anyway so the code is current."
fi

# --- deploy the SOURCE -------------------------------------------------------
#
# Config alone is not a deploy: `services update` mints a revision carrying
# the SAME container image, so the variables can be right while the container
# serves code from before the fixes those variables activate. That has
# happened twice here and both times read as a broken checker.
step "Deploying the current source"
warn "env vars alone do not ship code; this deploys the image too"
exec bash "$REPO_ROOT/scripts/repair-and-deploy.sh"
