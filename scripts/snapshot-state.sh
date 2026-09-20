#!/usr/bin/env bash
# Capture EVERY recoverable detail of this deployment into one file, so the
# whole design survives the project, the shell, and the host.
#
# From Cloud Shell (or any machine with gcloud), one line:
#
#     curl -fsSL https://raw.githubusercontent.com/Its-fortunatefolly/HubVibe/main/scripts/snapshot-state.sh | bash
#
# or, from a checkout:  bash scripts/snapshot-state.sh
#
# It writes hubvibe-snapshot-<timestamp>.txt and prints how to download it.
# Read-only everywhere: it changes nothing, and it NEVER prints a secret
# value or a private key -- secret NAMES yes, wallet ADDRESSES yes (an EVM
# address is public), values never. Every probe tolerates failure: with
# billing disabled most Google APIs answer errors, and the error text is
# itself part of the record (it says exactly what state the project is in).

set -uo pipefail

# This script is pasted into shells that are not in the repo (Cloud Shell,
# the box), so the repo may not be here at all -- every read below tolerates
# that and falls back rather than failing the whole snapshot.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd || echo /nonexistent)"

PROJECT="${PROJECT:-resolver-time}"
SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
BASE="${BASE:-https://hubvibe-io.com}"
OUT="hubvibe-snapshot-$(date +%Y%m%d-%H%M%S).txt"

section() { printf '\n\n================================================================\n== %s\n================================================================\n' "$1"; }
try()     { printf -- '-- %s\n' "$*"; "$@" 2>&1 || printf '(unavailable: exit %s)\n' "$?"; }

{
  section "SNAPSHOT $(date -u +%Y-%m-%dT%H:%M:%SZ) -- project=$PROJECT service=$SERVICE region=$REGION"

  section "Who and where (gcloud identity + config)"
  try gcloud config list
  try gcloud auth list

  section "Project + billing state (BILLING_DISABLED here explains everything dark)"
  try gcloud projects describe "$PROJECT"
  try gcloud billing projects describe "$PROJECT"
  try gcloud billing accounts list

  section "THE DEPLOYED DESIGN: the full Cloud Run service (env vars, image, scaling, everything)"
  try gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format=yaml

  section "Revision history (which build is live, which came before)"
  try gcloud run revisions list --service="$SERVICE" --project="$PROJECT" --region="$REGION"

  section "Secret NAMES in Secret Manager (values are never read by this script)"
  try gcloud secrets list --project="$PROJECT"

  section "What else exists on the project (the Workstations cluster here was the \$194/month)"
  try gcloud workstations clusters list --project="$PROJECT"
  try gcloud compute instances list --project="$PROJECT"
  try gcloud artifacts repositories list --project="$PROJECT"
  try gcloud firestore databases list --project="$PROJECT"

  section "Is the node serving right now? (health + the manifest that lists every rail)"
  try curl -sS -m 30 "$BASE/health"
  try curl -sS -m 30 "$BASE/.well-known/agent.json"

  section "A live 402 (the payment challenge itself: price, payTo, network, Bazaar record)"
  try curl -sS -m 30 -D - -X POST "$BASE/audit/wcag" -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'

  section "GitHub: current heads of both repos (public API, no auth needed)"
  try curl -sS -m 30 https://api.github.com/repos/Its-fortunatefolly/HubVibe/branches/main
  try curl -sS -m 30 https://api.github.com/repos/Its-fortunatefolly/hubvibe-audit-action/branches/main

  section "Local checkout state (if one exists in this shell)"
  for dir in "$HOME/HubVibe-deploy4" "$HOME/HubVibe"; do
    if [ -d "$dir/.git" ]; then
      printf -- '-- %s\n' "$dir"
      try git -C "$dir" log --oneline -3
      try git -C "$dir" status --short
    fi
  done

  section "Paying wallet ADDRESS (derived locally; the private key is never printed or copied)"
  if [ -r "$HOME/.hubvibe-wallet-key" ]; then
    python3 - <<'PYEOF' 2>&1 || echo "(could not derive the address; the key file stays untouched)"
import os
from eth_account import Account
print("payer address:", Account.from_key(open(os.path.expanduser("~/.hubvibe-wallet-key")).read().strip()).address)
print("fund/inspect:  https://basescan.org/address/" + Account.from_key(open(os.path.expanduser("~/.hubvibe-wallet-key")).read().strip()).address)
PYEOF
  else
    echo "no ~/.hubvibe-wallet-key in this shell (the paying wallet lives wherever first-paid-call.sh ran)"
  fi

  section "Fixed facts (from the repo's own records, so the snapshot is self-contained)"
  # The facilitator is READ, never restated. A hardcoded one here said
  # xpay.sh for a day after the repo moved to Dexter, which is exactly how a
  # saved snapshot becomes a record of something that is not true. Order:
  # what the live box is configured with, else what a fresh install would
  # write.
  SNAP_FACILITATOR=$(grep -h '^X402_FACILITATOR_URL=' "$REPO_ROOT/deploy/vps/.env" 2>/dev/null | head -1 | cut -d= -f2-)
  if [ -z "$SNAP_FACILITATOR" ]; then
    SNAP_FACILITATOR=$(sed -n 's/^FACILITATOR="\${X402_FACILITATOR_URL:-\(https:[^}]*\)}"/\1/p' \
      "$REPO_ROOT/scripts/vps-install.sh" 2>/dev/null | head -1)
    SNAP_FACILITATOR="${SNAP_FACILITATOR:-(unknown -- read scripts/vps-install.sh)} (repo default; this box has no deploy/vps/.env)"
  else
    SNAP_FACILITATOR="$SNAP_FACILITATOR (from deploy/vps/.env on this box)"
  fi
  cat <<FACTS
receiving wallet (x402 pay-to, owner-affirmed): 0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd  (hubvibe.base.eth)
facilitator: $SNAP_FACILITATOR
NEVER use as recipient: 0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256 (unidentified),
                        0x32b08c5e927c69877d0fcab35618c265674922bc (test constant), the zero address
Stripe account: see the Stripe dashboard (id deliberately not in this public repo)
human plans: RETIRED 2026-09-06 (per call is the only price; the three Stripe payment links are unpublished)
repos: github.com/Its-fortunatefolly/HubVibe (product) + hubvibe-audit-action (Marketplace action, tags v1/v1.0.0)
full history and decisions: docs/HANDOFF.md in the HubVibe repo
FACTS
} | tee "$OUT"

printf '\n\033[1mSaved to %s\033[0m\n' "$OUT"
printf 'Download it from Cloud Shell with:\n\n    cloudshell download %s\n\n' "$OUT"
printf '(or the three-dot menu -> Download). Keep it with your records.\n'
