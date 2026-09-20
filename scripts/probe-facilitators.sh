#!/usr/bin/env bash
# Which facilitator can actually put this node in the Bazaar?
#
# Two properties decide it, and no session has ever measured either:
#
#   1. Does it SETTLE on Base mainnet (eip155:8453, scheme "exact")?
#      Without this it cannot take a payment for this service at all.
#   2. Does it serve GET /discovery/resources -- i.e. does it keep a Bazaar
#      index? A facilitator catalogs a resource when it receives a
#      PaymentPayload carrying the bazaar extension (x402 spec,
#      specs/extensions/bazaar.md, "Facilitator Behavior"). If it keeps no
#      index, paying through it indexes NOTHING, however correct our 402 is.
#
# facilitator.xpay.sh settles and has no index (verified 2026-08-25:
# /discovery/resources -> {"message":"Not Found"}). That is exactly why this
# node is payable and still invisible. The x402 FAQ is explicit that the
# protocol is permissionless and that multiple organizations run production
# facilitators -- so the answer is a matter of measurement, not permission.
#
# Run this from Cloud Shell (the build sandbox cannot reach facilitator
# hosts). It only issues GETs: it moves no money and needs no wallet.
#
# Usage:  bash scripts/probe-facilitators.sh
#         bash scripts/probe-facilitators.sh https://another-facilitator.example
#
# Add candidates from https://www.x402.org/ecosystem?filter=facilitators as
# arguments; the defaults below are only the ones already known to this repo.

set -uo pipefail

CANDIDATES=(
  "https://facilitator.xpay.sh"
  "https://x402.org/facilitator"
  "https://facilitator.x402.org"
)
[ "$#" -gt 0 ] && CANDIDATES+=("$@")

# The network this service charges on, in the ONE vocabulary that counts.
#
# This used to accept either the CAIP-2 name ("eip155:8453") or the legacy
# v1 name ("base") as a match. Simulation against a stub facilitator that
# lists only the legacy name showed why that was wrong: the x402 server
# library builds every payment's requirements under the CAIP-2 name and
# only does so when the facilitator's /supported lists that exact name
# (ExactEvmServerScheme.parse_price("$0.05", "base") raises "Unsupported
# network format"). Against a legacy-only facilitator the node can verify
# nothing -- v1 or v2 -- and, since #82, correctly advertises nothing.
# So a legacy-only answer is reported as what it is: unusable here.
WANT_CAIP="eip155:8453"
WANT_LEGACY="base"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m' "$1"; }
red() { printf '\033[31m%s\033[0m' "$1"; }
yellow() { printf '\033[33m%s\033[0m' "$1"; }

# fetch <url> -> "<http_code>|<body first 4000 bytes>"
fetch() {
  curl -sS -m 20 -w '\n__CODE__%{http_code}' "$1" 2>/dev/null | head -c 4000
}

echo
bold "Probing x402 facilitators for Base-mainnet settlement + a Bazaar index"
echo "  want network: $WANT_CAIP, scheme \"exact\" (the legacy \"$WANT_LEGACY\" alone is unusable)"
echo

WINNERS=()

for base in "${CANDIDATES[@]}"; do
  base="${base%/}"
  bold "$base"

  # --- 1. /supported : which scheme+network kinds will it settle? ---
  raw=$(fetch "$base/supported")
  code=$(printf '%s' "$raw" | sed -n 's/.*__CODE__\([0-9]*\)$/\1/p')
  body=$(printf '%s' "$raw" | sed 's/__CODE__[0-9]*$//')

  settles="no"
  case "$code" in
    200)
      # Delimited on purpose: "eip155:84532" (Base Sepolia) contains
      # "eip155:8453", and a bare substring match reported a testnet-only
      # facilitator as a mainnet settler (x402.org/facilitator, 2026-09-12).
      if printf '%s' "$body" | grep -qE "\"network\"[: ]*\"$WANT_CAIP\""; then
        settles="yes"
        printf '  /supported            %s  Base mainnet listed as %s\n' "$(green PASS)" "$WANT_CAIP"
      elif printf '%s' "$body" | grep -qE "\"network\"[: ]*\"$WANT_LEGACY\""; then
        # Looks like Base, is not usable: this server library cannot build
        # requirements under the legacy name. Say so, distinctly, so nobody
        # reads "Base is listed" and deploys against it.
        printf '  /supported            %s  legacy name only ("%s") -- this server library builds\n' "$(red FAIL)" "$WANT_LEGACY"
        printf '                              requirements under %s and cannot use this facilitator\n' "$WANT_CAIP"
      else
        printf '  /supported            %s  200, but Base mainnet not offered\n' "$(red FAIL)"
      fi
      ;;
    401|403)
      settles="auth"
      printf '  /supported            %s  HTTP %s -- credentials required\n' "$(yellow AUTH)" "$code"
      ;;
    000|"")
      printf '  /supported            %s  unreachable\n' "$(red FAIL)"
      ;;
    *)
      printf '  /supported            %s  HTTP %s\n' "$(red FAIL)" "$code"
      ;;
  esac

  # --- 2. /discovery/resources : does it keep a Bazaar index? ---
  raw=$(fetch "$base/discovery/resources?limit=1")
  code=$(printf '%s' "$raw" | sed -n 's/.*__CODE__\([0-9]*\)$/\1/p')
  body=$(printf '%s' "$raw" | sed 's/__CODE__[0-9]*$//')

  indexes="no"
  case "$code" in
    200)
      # An index answers with an items[] array -- even an empty one. A 200
      # carrying {"message":"Not Found"} is not an index, and this is exactly
      # how xpay.sh answered, so the body is checked, not just the status.
      if printf '%s' "$body" | grep -q '"items"'; then
        indexes="yes"
        n=$(printf '%s' "$body" | grep -o '"resource"' | wc -l | tr -d ' ')
        printf '  /discovery/resources  %s  Bazaar index present (%s resource(s) on page 1)\n' "$(green PASS)" "$n"
      else
        printf '  /discovery/resources  %s  200 but no items[] -- not an index\n' "$(red FAIL)"
      fi
      ;;
    401|403)
      indexes="auth"
      printf '  /discovery/resources  %s  HTTP %s -- credentials required\n' "$(yellow AUTH)" "$code"
      ;;
    404)
      printf '  /discovery/resources  %s  404 -- keeps no Bazaar index\n' "$(red FAIL)"
      ;;
    000|"")
      printf '  /discovery/resources  %s  unreachable\n' "$(red FAIL)"
      ;;
    *)
      printf '  /discovery/resources  %s  HTTP %s\n' "$(red FAIL)" "$code"
      ;;
  esac

  if [ "$settles" = "yes" ] && [ "$indexes" = "yes" ]; then
    printf '  %s  settles Base mainnet AND indexes -- this one can list us\n' "$(green '=> KEYLESS WINNER')"
    WINNERS+=("$base")
  elif [ "$settles" = "auth" ] || [ "$indexes" = "auth" ]; then
    printf '  => needs credentials. Usable only if you can get them without a\n'
    printf '     business review; set X402_FACILITATOR_AUTH_HEADERS if so.\n'
  fi
  echo
done

echo "-----------------------------------------------"
if [ "${#WINNERS[@]}" -gt 0 ]; then
  bold "Use this facilitator:"
  for w in "${WINNERS[@]}"; do echo "  $w"; done
  echo
  echo "  Switch to it and deploy (one command, keeps the pay-to address):"
  echo "    X402_FACILITATOR_URL=${WINNERS[0]} bash scripts/repair-and-deploy.sh"
  echo
  echo "  Then make the one payment that puts this node in its index:"
  echo "    bash scripts/first-paid-call.sh"
else
  bold "No keyless facilitator here both settles Base mainnet and indexes."
  echo
  echo "  That is a real answer, not a failure: it means the Bazaar is reached"
  echo "  through a credentialed facilitator, and the next step is finding one"
  echo "  whose credentials do NOT require a business review. Add candidates"
  echo "  from the ecosystem list and re-run:"
  echo
  echo "    https://www.x402.org/ecosystem?filter=facilitators"
  echo "    bash scripts/probe-facilitators.sh https://candidate-one https://candidate-two"
  echo
  echo "  Settling and indexing can also be split: keep the facilitator that"
  echo "  settles, and the node stays payable -- it is only discovery that"
  echo "  waits."
fi
echo "-----------------------------------------------"
echo
