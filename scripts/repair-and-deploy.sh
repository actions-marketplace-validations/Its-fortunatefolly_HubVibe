#!/usr/bin/env bash
# Repair the Cloud Run config, deploy, and verify -- in one short command.
#
# This exists because the repair is three long gcloud invocations, and long
# commands pasted into a phone terminal land on a line that already has text
# in it and run together into garbage. `bash scripts/repair-and-deploy.sh` is
# short enough to type by hand, and does the steps in an order where a failure
# stops before it can make things worse.
#
# Safe to re-run. Every step checks the current state first and skips itself
# when there is nothing to do, so running it twice does not create pointless
# revisions or undo the first run.
#
# Usage:  bash scripts/repair-and-deploy.sh
#         STRIPE_SECRET_NAME=Other-Secret bash scripts/repair-and-deploy.sh

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
# Passed to EVERY gcloud call below, never inherited from gcloud config.
# A fresh Cloud Shell has no default project, and gcloud treats that as an
# empty answer rather than an error: `secrets describe` fails, `secrets list`
# prints nothing, and this script then said "no secret named
# SECRET_STRIPE_KEY" over a secret that exists -- and refused to deploy. That
# happened on the second Cloud Shell session of the night, after the first
# one (which had the project set) disconnected.
PROJECT="${PROJECT:-resolver-time}"
SOURCE_DIR="${SOURCE_DIR:-wcag-audit-engine}"

# The Secret Manager secret holding the Stripe secret key. A revision was once
# deployed pointing at a secret name that does not exist, which does not break
# the running instance but makes every future deploy fail at revision
# creation -- and would fail the next cold start too, because Cloud Run
# re-resolves secrets whenever an instance boots.
STRIPE_SECRET_NAME="${STRIPE_SECRET_NAME:-SECRET_STRIPE_KEY}"

# Placeholder values that must never reach a live revision: a facilitator URL
# that does not resolve would make the service advertise an x402 rail and
# Bazaar discovery it cannot actually settle.
PLACEHOLDER_HOSTS="your-facilitator.example"

step()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()    { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn()  { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

step "Checking the Stripe secret exists before pointing anything at it"
if gcloud secrets describe "$STRIPE_SECRET_NAME" --project="$PROJECT" >/dev/null 2>&1; then
  ok "secret $STRIPE_SECRET_NAME exists"
else
  # Keep gcloud's stderr. This branch used to discard it and then GUESS the
  # cause ("gcloud config set project"), and on 2026-09-04 the guess was
  # wrong: the prompt already read (resolver-time), --project was passed, and
  # the real reason -- whatever gcloud printed -- was thrown away. The
  # instruction it printed instead could not have fixed anything.
  LIST_ERR=$(mktemp)
  AVAILABLE=$(gcloud secrets list --project="$PROJECT" --format='value(name)' 2>"$LIST_ERR")
  GCLOUD_SAID=$(tr -s '[:space:]' ' ' <"$LIST_ERR" | cut -c1-600)
  rm -f "$LIST_ERR"
  if [ -z "$AVAILABLE" ]; then
    # Zero secrets is not a project with no secrets -- this one has several.
    # It is gcloud not seeing the project at all, and gcloud's own message
    # says why: a permission it lacks, an API not enabled, an expired login.
    printf '\n  gcloud said: %s\n\n' "${GCLOUD_SAID:-(nothing -- an empty list with no error)}"
    case "$GCLOUD_SAID" in
      # First, because Google words it as a permission error ("does not have
      # permission to access projects instance") and the IAM hint below would
      # match it and send the owner to the wrong console. This is what stopped
      # the 2026-09-04 deploy: billing off on the project shuts Secret Manager,
      # Cloud Run and the node itself -- every symptom that night, one cause.
      *BILLING_DISABLED*|*"billing to be enabled"*|*"enable billing"*)
        HINT="BILLING IS DISABLED on $PROJECT. Nothing on it serves -- not Secret Manager, not Cloud Run, not the node. Link a billing account here, then wait a few minutes and re-run:
        https://console.developers.google.com/billing/enable?project=$PROJECT" ;;
      *PERMISSION_DENIED*|*"does not have"*|*403*)
        HINT="this account is not authorised on $PROJECT. It needs roles/secretmanager.viewer (to read) and roles/run.admin (to deploy) on the project -- grant them in IAM, or run this from the account that owns the project." ;;
      *"not been used"*|*"is disabled"*|*"Enable it"*)
        HINT="the Secret Manager API is off in $PROJECT. Run: gcloud services enable secretmanager.googleapis.com --project=$PROJECT" ;;
      *[Rr]eauthentication*|*"credentials"*|*"auth login"*)
        HINT="the gcloud login has expired. Run: gcloud auth login" ;;
      *"not found"*|*"could not be found"*)
        HINT="there is no project with id $PROJECT visible to this account. Check: gcloud projects list" ;;
      *)
        HINT="read the message above; it is the fault. Check: gcloud config list, then gcloud config set project $PROJECT" ;;
    esac
    die "gcloud lists NO secrets in project $PROJECT -- it is not seeing the project.
        $HINT"
  fi
  printf '\n  Available secrets:\n'
  printf '%s\n' "$AVAILABLE" | sed 's/^/    /'
  die "no secret named $STRIPE_SECRET_NAME. Re-run as:
        STRIPE_SECRET_NAME=<one of the above> bash scripts/repair-and-deploy.sh"
fi

step "Reading the service's current configuration"
# Read once as JSON and reuse it. `flattened` pads names with alignment
# spaces, so `grep 'name: STRIPE_SECRET_KEY'` never matched -- CURRENT_SECRET
# came back empty on every run, the script concluded the repair was always
# needed, and each invocation minted a pointless revision. The service reached
# revision 62 this way, and the docstring's promise that re-running "does not
# create pointless revisions" was quietly false.
SVC_JSON=/tmp/hv_svc.json
gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format=json > "$SVC_JSON" 2>/dev/null
[ -s "$SVC_JSON" ] || die "could not read service $SERVICE in $REGION"

# env_secret <ENV_VAR> -> the secret backing it, or empty.
env_secret() {
  python3 -c '
import json, sys
try:
    svc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == sys.argv[2]:
        ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
        if ref.get("name"):
            print(ref["name"])
        break
' "$SVC_JSON" "$1" 2>/dev/null
}

# env_value <ENV_VAR> -> its plain value, "__FROM_SECRET__" if it is a secret
# reference, or empty if unset.
env_value() {
  python3 -c '
import json, sys
try:
    svc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == sys.argv[2]:
        print(entry["value"] if "value" in entry else "__FROM_SECRET__")
        break
' "$SVC_JSON" "$1" 2>/dev/null
}

CURRENT_SECRET=$(env_secret STRIPE_SECRET_KEY)

if [ -n "$CURRENT_SECRET" ]; then
  echo "  STRIPE_SECRET_KEY currently references: $CURRENT_SECRET"
else
  echo "  STRIPE_SECRET_KEY is not currently a Secret Manager reference"
fi

# Build the repair as a single revision, so a half-applied fix is impossible.
UPDATE_ARGS=()

if [ "$CURRENT_SECRET" != "$STRIPE_SECRET_NAME" ]; then
  UPDATE_ARGS+=("--update-secrets=STRIPE_SECRET_KEY=${STRIPE_SECRET_NAME}:latest")
  warn "will repoint STRIPE_SECRET_KEY at $STRIPE_SECRET_NAME"
else
  ok "STRIPE_SECRET_KEY already points at $STRIPE_SECRET_NAME"
fi

REMOVE_VARS=""
add_removal() {
  case ",$REMOVE_VARS," in
    *",$1,"*) return ;;
  esac
  REMOVE_VARS="${REMOVE_VARS:+$REMOVE_VARS,}$1"
}

# Recipients that are well-formed and that nobody here can claim.
#
# Removing them is deliberately a REPAIR and not a refusal, and the reasoning
# is the opposite way round from the malformed case: refusing the deploy
# leaves the running revision advertising the address, so stopping is the
# option that keeps money pointed at a stranger for longer. Stripping the
# variables turns the rail off on the next revision, which is exactly the
# manual command this was written to replace.
#
#   0x2b3b... sat deployed here as X402_PAY_TO_ADDRESS and the owner does not
#             recognise it. It is 0x + 40 hex, so every shape gate said yes.
#   0x32b0... is the test-suite constant. It exists to make the rail
#             inspectable on a local boot; nobody holds its key, and a
#             truncated paste of it once shipped as the tempo recipient.
UNAFFIRMED_ADDRESSES="
0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256
0x32b08c5e927c69877d0fcab35618c265674922bc
"

is_unaffirmed() {
  needle=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
  for candidate in $UNAFFIRMED_ADDRESSES; do
    [ "$needle" = "$(printf '%s' "$candidate" | tr 'A-Z' 'a-z')" ] && return 0
  done
  return 1
}

PAY_TO_RAW=$(env_value X402_PAY_TO_ADDRESS)
if [ -n "$PAY_TO_RAW" ] && [ "$PAY_TO_RAW" != "__FROM_SECRET__" ] \
   && is_unaffirmed "$PAY_TO_RAW"; then
  add_removal X402_PAY_TO_ADDRESS
  add_removal X402_FACILITATOR_URL
  warn "X402_PAY_TO_ADDRESS is an address NOBODY HERE HOLDS THE KEY TO."
  warn "Removing both x402 variables -- the rail goes off rather than settling"
  warn "to a stranger. Turn it back on with a recipient you control:"
  warn "  X402_PAY_TO_ADDRESS=0x... bash scripts/go-live.sh"
fi

for host in $PLACEHOLDER_HOSTS; do
  if grep -q "$host" "$SVC_JSON"; then
    add_removal X402_FACILITATOR_URL
    add_removal X402_PAY_TO_ADDRESS
    warn "found placeholder $host -- will remove the x402 variables"
  fi
done

# A malformed MPP tempo recipient is REPAIRED here, not merely refused in the
# preflight below.
#
# The distinction matters because this script is called repair-and-deploy, and
# refusing was the wrong half of that: the rail cannot settle with a malformed
# recipient either way -- the app's own guard already refuses to advertise it,
# so nothing is lost by stripping it -- and stopping the deploy over a value
# that has no valid use turned a one-command go-live into "run this other
# gcloud command first, then start again". That round trip is pure friction on
# the one path that puts a service back into service.
#
# Only a PLAIN env var is repaired. A secret-backed value cannot be read here,
# so it stays a preflight warning rather than something this silently deletes.
# And only the tempo recipient: X402_PAY_TO_ADDRESS still stops the deploy,
# because it is where money lands and a human should choose its replacement
# rather than have it quietly removed.
TEMPO_TO_RAW=$(env_value MPP_TEMPO_RECIPIENT_ADDRESS)
if [ -n "$TEMPO_TO_RAW" ] && [ "$TEMPO_TO_RAW" != "__FROM_SECRET__" ]; then
  if [ "$TEMPO_TO_RAW" = "0x0000000000000000000000000000000000000000" ]; then
    add_removal MPP_TEMPO_RECIPIENT_ADDRESS
    warn "MPP_TEMPO_RECIPIENT_ADDRESS is the zero address -- removing it. The"
    warn "tempo rail stays off until a real recipient is set."
  elif is_unaffirmed "$TEMPO_TO_RAW"; then
    add_removal MPP_TEMPO_RECIPIENT_ADDRESS
    warn "MPP_TEMPO_RECIPIENT_ADDRESS is an address nobody here holds the key"
    warn "to -- removing it. Mint a real Stripe crypto deposit address with"
    warn "  bash scripts/go-live.sh"
  elif ! printf '%s' "$TEMPO_TO_RAW" | grep -qiE '^0x[0-9a-f]{40}$'; then
    add_removal MPP_TEMPO_RECIPIENT_ADDRESS
    warn "MPP_TEMPO_RECIPIENT_ADDRESS is not a valid EVM address (needs 0x +"
    warn "exactly 40 hex chars; this one has $(( ${#TEMPO_TO_RAW} - 2 )) ) --"
    warn "removing it so the deploy can proceed. The tempo rail was already"
    warn "unusable with it. To turn that rail on later, mint a Stripe crypto"
    warn "deposit address and set MPP_TEMPO_RECIPIENT_ADDRESS to it."
  fi
fi

[ -n "$REMOVE_VARS" ] && UPDATE_ARGS+=("--remove-env-vars=$REMOVE_VARS")

if [ ${#UPDATE_ARGS[@]} -gt 0 ]; then
  step "Applying the configuration repair"
  gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" "${UPDATE_ARGS[@]}" \
    || die "config repair failed -- nothing was deployed, the running revision is untouched"
  ok "configuration repaired"
  # The service just changed; the cached JSON is stale for preflight.
  gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
    --format=json > "$SVC_JSON" 2>/dev/null
else
  step "Configuration is already correct -- nothing to repair"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- preflight ----------------------------------------------------------
#
# Everything below checks live infrastructure that the code cannot check for
# itself, and that has silently been wrong before. Each of these cost real
# money or real downtime, and none of them were caught by tests, by a deploy,
# or by verify-live.sh:
#
#   * no Firestore database existed for the project, so every authenticated
#     call 500'd -- for an unknown length of time, while the checker showed
#     28/28 passing
#   * a secret held a hand-written test string where a Stripe key belonged,
#     which reads back fine and fails at the API instead of at startup
#   * min-instances=1 on a browser-sized container burned ~$137/month against
#     approximately zero paid traffic
#   * a malformed pay-to address would make the service advertise an x402
#     rail that can never settle
#
# The pattern behind all four: nothing verified the deployed environment, so
# drift accumulated invisibly and only surfaced by accident. They are checked
# here, before the deploy, because a deploy into a broken environment produces
# a healthy-looking revision that cannot take money.

step "Preflight: checking the live environment"

PREFLIGHT_FAILED=0

# 1. Firestore. billing.lookup_key hits it on every keyed request.
if gcloud firestore databases describe --database='(default)' --project="$PROJECT" \
     --format='value(name)' >/dev/null 2>&1; then
  ok "Firestore (default) database exists"
else
  warn "no Firestore (default) database. Every API-key call will fail to"
  warn "authenticate, and checkout cannot store issued keys. Create it with:"
  warn "  gcloud firestore databases create --location=$REGION --project=$PROJECT"
  PREFLIGHT_FAILED=1
fi

# 2. Secrets: readable AND the right shape. Delegated to repair-secrets.sh in
#    report mode so there is one implementation of "is this secret sane".
if [ -x "$SCRIPT_DIR/repair-secrets.sh" ] || [ -f "$SCRIPT_DIR/repair-secrets.sh" ]; then
  if bash "$SCRIPT_DIR/repair-secrets.sh" 2>/dev/null | grep -qE 'NOT READABLE|WRONG SHAPE'; then
    warn "one or more secrets are unreadable or hold the wrong kind of value."
    warn "Run:  bash scripts/repair-secrets.sh          (to see what)"
    warn "Then: bash scripts/repair-secrets.sh --apply  (to fix it)"
    PREFLIGHT_FAILED=1
  else
    ok "every referenced secret is readable and correctly shaped"
  fi
fi

# 3. The x402 pay-to address. An EVM address is 0x plus exactly 40 hex
#    characters. One character short still looks right at a glance, and the
#    service would advertise a crypto rail while every settlement fails --
#    indistinguishable, from outside, from nobody buying.
#    Parsed out of JSON rather than grepped from `flattened` output. That
#    format pads names with alignment spaces, so a `grep 'name: X'` pattern
#    matches nothing and the check silently skips -- which is worse than not
#    having the check at all, because the output then looks like it passed.
PAY_TO=$(python3 -c '
import json, sys
try:
    svc = json.load(open("/tmp/hv_svc.json"))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == "X402_PAY_TO_ADDRESS":
        # A secret-backed value cannot be read here. Say so, rather than
        # reporting a pass that was never actually checked.
        print(entry["value"] if "value" in entry else "__FROM_SECRET__")
        break
' 2>/dev/null)

if grep -q 'X402_FACILITATOR_URL' /tmp/hv_svc.json 2>/dev/null; then
  HAS_FACILITATOR=1
else
  HAS_FACILITATOR=0
fi

if [ "$PAY_TO" = "__FROM_SECRET__" ]; then
  warn "X402_PAY_TO_ADDRESS comes from Secret Manager, so its shape was NOT"
  warn "checked here. Verify by hand that it is 0x + 40 hex characters."
elif [ -n "$PAY_TO" ]; then
  if [ "$PAY_TO" = "0x0000000000000000000000000000000000000000" ]; then
    # Shape-valid, unownable. This exact value shipped once: it passes the
    # 40-hex check below, the app advertised x402 as live, and USDC's
    # contract reverts transfers to address(0) -- no payment could ever
    # arrive, and from our side that is indistinguishable from no demand.
    warn "X402_PAY_TO_ADDRESS is the ZERO ADDRESS (0x + 40 zeros). It is"
    warn "well-formed but unownable: USDC transfers to address(0) revert."
    warn "Set a real recipient address you control."
    PREFLIGHT_FAILED=1
  elif printf '%s' "$PAY_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
    ok "X402_PAY_TO_ADDRESS is a well-formed EVM address"
  else
    warn "X402_PAY_TO_ADDRESS is NOT a valid EVM address (needs 0x + exactly"
    warn "40 hex chars; this one has $(( ${#PAY_TO} - 2 ))). Every x402"
    warn "settlement would fail while the rail is advertised as live."
    PREFLIGHT_FAILED=1
  fi
elif [ "$HAS_FACILITATOR" -eq 1 ]; then
  warn "an x402 facilitator is configured but X402_PAY_TO_ADDRESS is not set."
  warn "The service would advertise a crypto rail with no destination."
  PREFLIGHT_FAILED=1
else
  ok "x402 is not configured (no rail advertised, nothing to check)"
fi

# 3b. The MPP tempo recipient. Same check, same reason, and it was missing
#     here for exactly as long as it was missing in the app: on 2026-08-29
#     `mppx validate` (the protocol's own reference client) reported "Valid
#     recipient address" FAILING on all six paid routes against a recipient
#     of 39 hex characters -- a truncated paste of the test-suite constant --
#     while every check on this side was green. The x402 address has had this
#     guard since the 16-hex incident; the tempo one carries real money too.
TEMPO_TO=$(python3 -c '
import json
try:
    svc = json.load(open("/tmp/hv_svc.json"))
except Exception:
    raise SystemExit
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == "MPP_TEMPO_RECIPIENT_ADDRESS":
        print(entry["value"] if "value" in entry else "__FROM_SECRET__")
        break
' 2>/dev/null)

if [ "$TEMPO_TO" = "__FROM_SECRET__" ]; then
  warn "MPP_TEMPO_RECIPIENT_ADDRESS comes from Secret Manager, so its shape was"
  warn "NOT checked here. Verify by hand that it is 0x + 40 hex characters."
elif [ -n "$TEMPO_TO" ]; then
  if [ "$TEMPO_TO" = "0x0000000000000000000000000000000000000000" ]; then
    warn "MPP_TEMPO_RECIPIENT_ADDRESS is the ZERO ADDRESS. Well-formed and"
    warn "unownable: no payment could ever arrive. Set a real recipient."
    PREFLIGHT_FAILED=1
  elif printf '%s' "$TEMPO_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
    ok "MPP_TEMPO_RECIPIENT_ADDRESS is a well-formed EVM address"
  else
    warn "MPP_TEMPO_RECIPIENT_ADDRESS is NOT a valid EVM address (needs 0x +"
    warn "exactly 40 hex chars; this one has $(( ${#TEMPO_TO} - 2 ))). The MPP"
    warn "tempo rail would be advertised and every settlement would fail."
    PREFLIGHT_FAILED=1
  fi
else
  ok "the MPP tempo rail is not configured (nothing to check)"
fi

# 4. Idle billing. Not fatal, but it is pure loss at low volume and nothing
#    else reports it.
MIN_SCALE=$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])' 2>/dev/null)
if [ -n "$MIN_SCALE" ] && [ "$MIN_SCALE" -gt 0 ] 2>/dev/null; then
  # This used to only WARN, printing the gcloud command for a human to run.
  # It warned on every deploy for weeks while the meter ran, and the bill
  # reached $300 before anyone acted on it. A warning nobody reads is not a
  # control; the money leaves either way.
  #
  # So it is repaired, like the malformed tempo recipient above, and for the
  # same reason: keeping an instance warm has no valid use at zero paid
  # traffic. Cold starts cost an agent a few seconds on the first call after
  # an idle period. Idle billing costs real dollars every hour forever.
  # KEEP_WARM=1 opts back in once paid volume makes the trade worth it.
  if [ -n "${KEEP_WARM:-}" ]; then
    warn "min-instances=$MIN_SCALE and KEEP_WARM is set -- leaving it warm."
    warn "That is real money per hour against current traffic. Unset KEEP_WARM"
    warn "to have this set it back to 0."
  else
    warn "min-instances=$MIN_SCALE: paying to keep an instance warm 24/7."
    warn "Setting it to 0 -- at zero paid traffic that is pure loss."
    if gcloud run services update "$SERVICE" --project="$PROJECT" \
         --region="$REGION" --min-instances=0 >/dev/null 2>&1; then
      ok "min-instances set to 0; idle billing stops"
    else
      warn "could not set it. Run this yourself, it is the whole bill:"
      warn "  gcloud run services update $SERVICE --project=$PROJECT --region=$REGION --min-instances=0"
    fi
  fi
fi

# 5. Spend guard. The bill reached $300 with zero revenue before anyone saw
#    it, because nothing was watching. A budget alert is Google's own
#    tripwire: an email to the billing admins at half and at all of the
#    amount. It costs nothing, and it is created here so it cannot be
#    forgotten the way the min-instances warning was. Not fatal: a deploy
#    must not be blocked by an alert failing to register -- but it is never
#    silent about it either.
step "Spend guard: a \$${SPEND_ALERT_USD:-5}/month billing alert on $PROJECT"
BILLING_ACCOUNT=$(gcloud billing projects describe "$PROJECT" \
  --format='value(billingAccountName)' 2>/dev/null | sed 's|billingAccounts/||')
if [ -z "$BILLING_ACCOUNT" ]; then
  warn "no billing account is linked to $PROJECT -- nothing on it can serve, and there"
  warn "is nothing to alert on. Link one: https://console.developers.google.com/billing/enable?project=$PROJECT"
elif gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
       --format='value(displayName)' 2>/dev/null | grep -qx "hubvibe-spend-alert"; then
  ok "budget alert 'hubvibe-spend-alert' already exists on billing account $BILLING_ACCOUNT"
else
  gcloud services enable billingbudgets.googleapis.com --project="$PROJECT" >/dev/null 2>&1 || true
  if gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
       --display-name="hubvibe-spend-alert" \
       --budget-amount="${SPEND_ALERT_USD:-5}USD" \
       --filter-projects="projects/$PROJECT" \
       --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 >/dev/null 2>&1; then
    ok "budget alert created: email at 50% and 100% of \$${SPEND_ALERT_USD:-5}/month"
  else
    warn "could not create the budget alert. Do it once by hand:"
    warn "  gcloud billing budgets create --billing-account=$BILLING_ACCOUNT --display-name=hubvibe-spend-alert --budget-amount=${SPEND_ALERT_USD:-5}USD --filter-projects=projects/$PROJECT --threshold-rule=percent=1.0"
  fi
fi

if [ "$PREFLIGHT_FAILED" -ne 0 ]; then
  die "preflight found problems above. Nothing was deployed and the running
        revision is untouched. Fix them and re-run -- deploying into a broken
        environment produces a revision that looks healthy but cannot take money."
fi
ok "preflight passed"

step "Deploying the current source"
# Capacity is pinned here rather than inherited from whatever the last
# revision happened to have. Each setting is a bill-or-outage decision:
#   --memory/--cpu      four Chromium contexts plus Python need ~2 GiB; the
#                       512 MiB default OOMs under concurrent audits. Billed
#                       only while a request is in flight (min-instances 0).
#   --concurrency       MAX_CONCURRENT_AUDITS (4) audits run per instance;
#                       at 8 in-flight requests Cloud Run adds an instance
#                       instead of queueing 80 behind one browser.
#   --max-instances     the blast radius of a flood of unpaid 402s, and the
#                       hard ceiling on what a day can cost: 3 instances at
#                       full tilt is ~$12/day worst case, while 3 x 4 audits
#                       is ~2 audits/s ~ 170k audits/day ~ $5k/day of PAID
#                       capacity. Raise it when revenue says so, not before.
#   --timeout           a bundle is four page loads; 120s covers it and
#                       stops a hung audit from holding an instance for 5 min.
#   --cpu-boost         full CPU during cold start, so a paying agent's
#                       first call after idle is not the one that times out.
# The identity every 402 and manifest advertises. The code's default is the
# domain (https://hubvibe-io.com) -- right on the VPS, where Caddy serves it,
# and WRONG here until that domain points at Cloud Run: deployed without
# this, every resource URL and the Bazaar record would name a parked page.
# So on Cloud Run the identity is this service's own URL, unless the operator
# says otherwise (PUBLIC_BASE_URL=https://hubvibe-io.com once the domain is
# mapped to this service). Fail closed if neither is known.
PUBLIC_URL="${PUBLIC_BASE_URL:-$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("status", {}).get("url", ""))
except Exception:
    pass
' "$SVC_JSON")}"
[ -n "$PUBLIC_URL" ] || die "cannot tell which URL this node should advertise; set PUBLIC_BASE_URL=https://... and re-run"

gcloud run deploy "$SERVICE" --source="$SOURCE_DIR" --project="$PROJECT" --region="$REGION" \
  --memory="${MEMORY:-2Gi}" --cpu="${CPU:-2}" --concurrency="${CONCURRENCY:-8}" \
  --max-instances="${MAX_INSTANCES:-3}" --timeout="${REQUEST_TIMEOUT:-120}" --cpu-boost \
  --update-env-vars="PUBLIC_BASE_URL=$PUBLIC_URL" \
  || die "deploy failed. The previous revision keeps serving; fix the error above and re-run."
ok "deployed -- advertising $PUBLIC_URL as this node's identity"

# SKIP_VERIFY exists so a deploy and its verification can be run separately --
# verify-live.sh makes real network calls with retries, which is right after a
# deploy but wrong when something else is driving this script.
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  step "Skipping live verification (SKIP_VERIFY=1)"
  echo "  Run it yourself with: bash scripts/verify-live.sh"
else
  step "Verifying the live service"
  bash "$SCRIPT_DIR/verify-live.sh"
fi
