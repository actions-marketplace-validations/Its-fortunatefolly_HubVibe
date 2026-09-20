#!/usr/bin/env bash
# Point the running node at a different x402 facilitator -- and put it back
# if that facilitator cannot serve a payable challenge.
#
#     bash scripts/switch-facilitator.sh https://facilitator.payai.network
#
# WHY THIS IS NOT A ONE-LINE ENV EDIT
#
# The node reads the facilitator's /supported at startup and REFUSES to
# advertise any rail that facilitator cannot verify -- deliberately, because
# advertising a rail that cannot settle is the one failure this codebase
# exists to prevent. The consequence is that a facilitator which is down,
# slow, or does not list `exact` on Base mainnet does not produce an error:
# it produces a node that quietly stops offering x402 and takes no money,
# while /health still answers 200 and the site looks fine.
#
# So the switch is only safe if something checks the LIVE 402 afterwards and
# undoes it. That is this script: it edits deploy/vps/.env, restarts, waits
# for the node, and reads a real unpaid POST. If the x402 rail is gone, or
# the recipient changed, or the price moved, it restores the previous
# facilitator, restarts again, and exits non-zero. Nothing is left half-done.
#
# Run it on the box, from the repo root.
#
# Optional:
#     COMPOSE_DIR  where the stack lives   (default deploy/vps beside this repo)
#     BASE         how to reach the node   (default https://$DOMAIN from .env)
#     ROUTE        the route to probe      (default /audit/wcag)

set -uo pipefail

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO_ROOT/deploy/vps}"
ENV_FILE="$COMPOSE_DIR/.env"
ROUTE="${ROUTE:-/audit/wcag}"

NEW_FACILITATOR="${1:-}"
if [ -z "$NEW_FACILITATOR" ]; then
  printf '\nUsage:  bash scripts/switch-facilitator.sh https://facilitator.payai.network\n\n'
  printf 'Switches the running node to that facilitator and rolls back if the\n'
  printf 'node stops advertising a payable x402 rail.\n\n'
  exit 1
fi
case "$NEW_FACILITATOR" in
  https://*) ;;
  *) die "the facilitator must be an https:// URL (got '$NEW_FACILITATOR')" ;;
esac
# A trailing slash reaches the library as a double slash on every call.
NEW_FACILITATOR="${NEW_FACILITATOR%/}"

# Everything below assumes we are ON the box. Say so by name when we are
# demonstrably not: this ran in Google Cloud Shell on 2026-09-07 and the
# only complaint was a missing .env -- which reads as "the stack is broken"
# rather than "you are in the wrong terminal", and sends the reader looking
# for a file instead of switching windows. vps-install.sh has refused Cloud
# Shell by name since the owner pasted IT there; this is the same mistake
# one script over. Checked before the .env, because the missing file is the
# symptom and this is the cause.
if [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  die "this is Google Cloud Shell -- a temporary terminal, not the server running the node. Run this on the VPS itself (Hostinger: VPS -> Browser terminal; or ssh root@YOUR_VPS_IP). Nothing was changed."
fi

[ -f "$ENV_FILE" ] || die "no $ENV_FILE -- run this on the box where the stack is installed."
command -v docker >/dev/null 2>&1 || die "docker is not on PATH. Run this on the box."

OLD_FACILITATOR=$(grep '^X402_FACILITATOR_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DOMAIN=$(grep '^DOMAIN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
BASE="${BASE:-https://$DOMAIN}"
[ -n "$DOMAIN" ] || die "no DOMAIN in $ENV_FILE"

if [ "$OLD_FACILITATOR" = "$NEW_FACILITATOR" ]; then
  ok "already on $NEW_FACILITATOR -- nothing to change"
  exit 0
fi

compose() { docker compose -f "$COMPOSE_DIR/docker-compose.yml" --project-directory "$COMPOSE_DIR" "$@"; }

# Reads the live 402 and answers with the rail's facts, or "none".
probe() {
  curl -s -m 25 -X POST "$BASE$ROUTE" -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com"}' 2>/dev/null | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    print("none"); raise SystemExit
accepts = body.get("accepts") or []
if not accepts:
    print("none"); raise SystemExit
a = accepts[0]
print("%s\t%s\t%s" % (a.get("payTo"), a.get("network"), a.get("maxAmountRequired")))
' 2>/dev/null
}

step "Reading the rail the node advertises right now"
BEFORE=$(probe)
if [ -z "$BEFORE" ] || [ "$BEFORE" = "none" ]; then
  warn "the node advertises no x402 rail even BEFORE the switch"
  warn "(facilitator $OLD_FACILITATOR). Switching cannot make that worse."
else
  ok "now: payTo=$(printf '%s' "$BEFORE" | cut -f1) network=$(printf '%s' "$BEFORE" | cut -f2) amount=$(printf '%s' "$BEFORE" | cut -f3)"
fi

apply() {
  local url="$1"
  # sed -i with a backup suffix, then remove it: the bare form differs
  # between GNU and BSD sed and this must not depend on which the box has.
  sed -i.bak "s|^X402_FACILITATOR_URL=.*|X402_FACILITATOR_URL=$url|" "$ENV_FILE" \
    && rm -f "$ENV_FILE.bak"
  compose up -d >/dev/null 2>&1
}

step "Switching to $NEW_FACILITATOR and restarting"
apply "$NEW_FACILITATOR" || die "could not write $ENV_FILE"

for attempt in $(seq 1 20); do
  curl -sf -m 10 -o /dev/null "$BASE/health" && break
  [ "$attempt" -eq 20 ] && break
  sleep 5
done

step "Reading the live 402 again -- the only thing that proves the rail survived"
AFTER=""
for attempt in $(seq 1 6); do
  AFTER=$(probe)
  [ -n "$AFTER" ] && [ "$AFTER" != "none" ] && break
  sleep 5
done

if [ -z "$AFTER" ] || [ "$AFTER" = "none" ]; then
  warn "the node advertises NO x402 rail on $NEW_FACILITATOR -- rolling back"
  apply "$OLD_FACILITATOR"
  for attempt in $(seq 1 20); do
    curl -sf -m 10 -o /dev/null "$BASE/health" && break
    sleep 5
  done
  RESTORED=$(probe)
  if [ -n "$RESTORED" ] && [ "$RESTORED" != "none" ]; then
    ok "restored $OLD_FACILITATOR; the node is advertising x402 again"
  else
    warn "restored $OLD_FACILITATOR but the node still advertises no rail --"
    warn "read: cd $COMPOSE_DIR && docker compose logs hubvibe | tail -40"
  fi
  die "$NEW_FACILITATOR cannot serve a payable challenge here. Nothing changed."
fi

if [ -n "$BEFORE" ] && [ "$BEFORE" != "none" ] && [ "$AFTER" != "$BEFORE" ]; then
  warn "the rail CHANGED across the switch -- rolling back rather than guessing"
  warn "  before: $(printf '%s' "$BEFORE" | tr '\t' ' ')"
  warn "  after:  $(printf '%s' "$AFTER" | tr '\t' ' ')"
  apply "$OLD_FACILITATOR"
  die "recipient, network or price moved. Nothing changed."
fi

ok "x402 still live: payTo=$(printf '%s' "$AFTER" | cut -f1) network=$(printf '%s' "$AFTER" | cut -f2) amount=$(printf '%s' "$AFTER" | cut -f3)"

step "Does this facilitator run a Bazaar index?"
INDEX=$(curl -s -m 20 -o /tmp/hubvibe-index.$$ -w '%{http_code}' "$NEW_FACILITATOR/discovery/resources" 2>/dev/null)
if [ "$INDEX" = "200" ]; then
  COUNT=$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("?"); raise SystemExit
items = d.get("items") or d.get("resources") or (d if isinstance(d, list) else [])
print(len(items) if isinstance(items, list) else "?")
' "/tmp/hubvibe-index.$$" 2>/dev/null || echo "?")
  ok "$NEW_FACILITATOR serves /discovery/resources ($COUNT entries today)"
  printf '\n  A paid call from here on carries this node'"'"'s discovery record to it.\n'
  printf '  Make it:  BASE=%s bash scripts/first-paid-call.sh\n\n' "$BASE"
else
  warn "$NEW_FACILITATOR/discovery/resources answered HTTP $INDEX -- it settles"
  warn "payments but may run no index, so a paid call will not register the node."
fi
rm -f "/tmp/hubvibe-index.$$"

printf '  Facilitator is now \033[1m%s\033[0m (was %s).\n\n' "$NEW_FACILITATOR" "$OLD_FACILITATOR"
