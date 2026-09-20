#!/usr/bin/env bash
# Print the last x402 payment decisions the deployed node logged.
#
#   bash scripts/x402-log.sh
#
# This exists because the equivalent `gcloud logging read` is a 200-character
# line with nested quotes, and the first time it was pasted from a phone it
# arrived with a newline in the middle of the filter and failed with
# "Unparseable filter: syntax error at line 2". The one-short-line rule is not
# a style preference; it is what survives a mobile clipboard.
#
# Reads both payload shapes. Python's default logging goes to stderr, which
# Cloud Run records as textPayload; a structured-logging revision would land
# in jsonPayload.message instead. Asking for both means a future change to
# how the app logs does not make this print nothing and read as "no
# payments were attempted".
#
# What to expect after a refused payment (see app/x402_payments.py):
#   x402 verify REJECTED by the facilitator (...): reason=... message=...
#   x402 verify FAILED before the facilitator could answer (...): <Error>: ...
#   x402 settle REFUSED ...
# A REJECTED line is the facilitator saying no and naming why. A FAILED line
# means the node never got an answer -- a different problem with a different
# fix. Nothing at all, on a revision that carries #79, means no payment
# reached the verify step: look one layer out, at the client.

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
PROJECT="${PROJECT:-resolver-time}"
FRESHNESS="${FRESHNESS:-2h}"
LIMIT="${LIMIT:-20}"

command -v gcloud >/dev/null 2>&1 || {
  printf 'gcloud is not on PATH. Run this in Cloud Shell.\n' >&2
  exit 1
}

# Filter kept on ONE line on purpose (see the header).
FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\" AND (textPayload:\"x402\" OR jsonPayload.message:\"x402\")"

printf 'x402 log lines for %s in %s, last %s (newest first):\n\n' "$SERVICE" "$PROJECT" "$FRESHNESS"
OUT=$(gcloud logging read "$FILTER" --project="$PROJECT" --freshness="$FRESHNESS" \
        --limit="$LIMIT" --format='value(timestamp,textPayload,jsonPayload.message)' 2>&1)
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  printf '%s\n' "$OUT" >&2
  exit "$STATUS"
fi
if [ -z "$OUT" ]; then
  printf '  (none)\n\n'
  printf '  No x402 line in the last %s. Either no payment reached the verify step,\n' "$FRESHNESS"
  printf '  or the running revision predates #79 and discards the reason. Check the\n'
  printf '  revision: bash scripts/verify-live.sh\n'
  exit 0
fi
printf '%s\n' "$OUT"
