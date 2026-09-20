#!/usr/bin/env bash
# One command, from any Cloud Shell, to a first paid call.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Its-fortunatefolly/HubVibe/main/scripts/launch.sh)
#
# It does, in order, stopping at the first thing that is wrong and saying
# exactly what to do about it:
#   1. billing on the project -- nothing serves without it, so it is checked
#      first and the enable link is printed if it is off;
#   2. a checkout at origin/main (clone or reset), so the deploy carries
#      current code and not whatever an old directory had;
#   3. bash scripts/repair-and-deploy.sh   (preflight, capacity, spend alert,
#      source deploy, live verification);
#   4. bash scripts/first-paid-call.sh     (one real $0.05 payment, receipt
#      printed as a Basescan link).
#
# Every step is the same script the handoff documents; this only removes the
# need to type them in the right order from a phone.

set -uo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1  # never hang on a gcloud (y/N)

PROJECT="${PROJECT:-resolver-time}"
DIR="${HUBVIBE_DIR:-${HOME:-/tmp}/HubVibe-deploy4}"
REPO="https://github.com/Its-fortunatefolly/HubVibe"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

step "Billing on $PROJECT"
BILLING=$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>&1)
case "$BILLING" in
  True)
    ok "billing is enabled"
    ;;
  False)
    die "billing is OFF on $PROJECT. Nothing on it can serve until an account is linked.
        Open this, link one, wait five minutes, then run this command again:
        https://console.developers.google.com/billing/enable?project=$PROJECT"
    ;;
  *)
    die "could not read billing for $PROJECT. gcloud said: $(printf '%s' "$BILLING" | tr -s '[:space:]' ' ' | cut -c1-300)"
    ;;
esac

step "Checkout at origin/main in $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin main || die "could not fetch origin/main"
  git -C "$DIR" reset -q --hard origin/main || die "could not reset to origin/main"
else
  git clone -q "$REPO" "$DIR" || die "could not clone $REPO"
fi
cd "$DIR" || die "cannot enter $DIR"
ok "at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s | cut -c1-70)"

step "Anything billing this project that is not the tollbooth? (scripts/cost-sweep.sh)"
# The August bill was $193.84 and none of it was Cloud Run: a Cloud
# Workstations cluster in us-central1 billed its control plane every hour.
# With billing just re-enabled, that meter restarts before the node does.
# So it is found and, with DELETE_IDLE=1, removed, before anything deploys.
if ! DELETE_IDLE="${DELETE_IDLE:-1}" bash scripts/cost-sweep.sh; then
  die "something idle is still billing $PROJECT (see above). Remove it, then run this again.
        A node that earns cents cannot carry a workstation that costs dollars."
fi

step "Deploying (scripts/repair-and-deploy.sh)"
bash scripts/repair-and-deploy.sh || die "the deploy stopped. The STOP above says why; fix that and run this again."

step "First paid call (scripts/first-paid-call.sh)"
exec bash scripts/first-paid-call.sh
