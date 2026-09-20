#!/usr/bin/env bash
#
# Repair Secret Manager references for the deployed service, and report the
# env-var -> secret wiring.
#
# Why this exists: a secret was written to by hand with the wrong name, and
# then the "fix" (disabling the bad version) made things worse, because the
# `latest` alias resolves to the HIGHEST VERSION NUMBER regardless of whether
# that version is enabled. Disabling the newest version does not fall back to
# the previous one -- it makes `latest` unreadable, and a container that
# mounts that secret then fails to start at all.
#
# The safe repair is always the same shape: find the newest version that is
# both enabled and plausible, and copy it forward as a NEW version, so
# `latest` points at good data again. That is additive -- nothing is disabled,
# nothing is destroyed, every old version stays exactly where it was, and the
# whole operation is reversible.
#
# Usage:
#   bash scripts/repair-secrets.sh            # report only, changes nothing
#   bash scripts/repair-secrets.sh --apply    # perform the repairs
#
# It never prints a secret value. Only names, versions, states, lengths, and
# a short non-sensitive prefix used to tell key kinds apart.

set -uo pipefail

PROJECT="${PROJECT:-resolver-time}"
SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
APPLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --help|-h) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

command -v gcloud >/dev/null || { echo "error: gcloud not found. Run this in Cloud Shell." >&2; exit 64; }
command -v python3 >/dev/null || { echo "error: python3 not found." >&2; exit 64; }

PROBLEMS=0
REPAIRED=0

echo
echo "== env var -> secret wiring on $SERVICE =="

gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format=json > /tmp/hv_svc_secrets.json 2>/dev/null

if [ ! -s /tmp/hv_svc_secrets.json ]; then
  echo "error: could not read service $SERVICE in $REGION (project $PROJECT)." >&2
  exit 1
fi

# One line per env var backed by a secret: NAME<TAB>SECRET<TAB>VERSION.
python3 - <<'PY' > /tmp/hv_secret_refs.txt
import json

with open("/tmp/hv_svc_secrets.json") as handle:
    svc = json.load(handle)

container = (
    svc.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [{}]
)[0]

for entry in container.get("env") or []:
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
    if ref:
        # `key` holds the version ("latest" or a number); `name` the secret.
        print("\t".join([entry.get("name", "?"), ref.get("name", "?"), ref.get("key", "?")]))
PY

if [ ! -s /tmp/hv_secret_refs.txt ]; then
  echo "  (no env vars are backed by Secret Manager)"
else
  printf '  %-28s %-32s %s\n' "ENV VAR" "SECRET" "VERSION"
  while IFS=$'\t' read -r env_name secret_name version; do
    printf '  %-28s %-32s %s\n' "$env_name" "$secret_name" "$version"
  done < /tmp/hv_secret_refs.txt
fi

echo
echo "== secret health =="

# Deduplicate: two env vars can legitimately point at the same secret, and
# repairing it twice would create two identical versions for no reason.
SECRETS=$(cut -f2 /tmp/hv_secret_refs.txt 2>/dev/null | sort -u)

# describe_value <secret> <version> -> "<len> <prefix>", or empty if unreadable.
# Prints a length and a 3-char prefix, never the value: enough to tell an
# sk_live_ Stripe key from a random string, useless to anyone reading a log.
describe_value() {
  local secret="$1" version="$2" value
  value=$(gcloud secrets versions access "$version" --secret="$secret" \
            --project="$PROJECT" 2>/dev/null) || return 1
  [ -n "$value" ] || return 1
  printf '%s %s' "${#value}" "$(printf '%s' "$value" | cut -c1-3)"
}

# expected_prefix <secret> -> a prefix the value must start with, or empty.
#
# Readable is not the same as correct. A secret can hold a perfectly valid
# string that is simply the wrong string -- which is exactly what happened
# here: a hand-written test value landed in the Stripe key secret, so `latest`
# read back fine and every Stripe call would have failed at the API instead.
# Checking the shape catches that; checking only readability does not.
expected_prefix() {
  case "$1" in
    *[Ww]ebhook*) printf 'whsec_' ;;
    *STRIPE*|*[Ss]tripe*) printf 'sk_' ;;
    *) printf '' ;;
  esac
}

# looks_right <secret> <version> -> 0 if the value matches the expected shape.
looks_right() {
  local secret="$1" version="$2" want value
  want=$(expected_prefix "$secret")
  [ -n "$want" ] || return 0  # nothing known about this secret's shape
  value=$(gcloud secrets versions access "$version" --secret="$secret" \
            --project="$PROJECT" 2>/dev/null) || return 1
  case "$value" in
    "$want"*) return 0 ;;
    # Stripe restricted keys are also valid secret keys.
    rk_*) [ "$want" = "sk_" ] && return 0 || return 1 ;;
    *) return 1 ;;
  esac
}

# newest_good_version <secret> -> the highest enabled version whose value has
# a value and the expected shape.
newest_good_version() {
  local secret="$1" version
  for version in $(gcloud secrets versions list "$secret" --project="$PROJECT" \
                     --filter="state:ENABLED" --format="value(name)" 2>/dev/null | sort -rn); do
    if describe_value "$secret" "$version" >/dev/null && looks_right "$secret" "$version"; then
      printf '%s' "$version"
      return 0
    fi
  done
  return 1
}

repair_from() {
  local secret="$1" candidate="$2" info
  if gcloud secrets versions access "$candidate" --secret="$secret" --project="$PROJECT" 2>/dev/null \
      | gcloud secrets versions add "$secret" --data-file=- --project="$PROJECT" >/dev/null 2>&1; then
    if info=$(describe_value "$secret" latest) && looks_right "$secret" latest; then
      echo "    REPAIRED: latest now readable (${info%% *} bytes, starts '${info##* }')"
      REPAIRED=$((REPAIRED + 1))
      return 0
    fi
    echo "    copy succeeded but latest is still wrong -- stopping rather than"
    echo "    guessing further."
    return 1
  fi
  echo "    copy FAILED. Check you have secretmanager.versions.add on $secret."
  return 1
}

for secret in $SECRETS; do
  [ -n "$secret" ] || continue
  echo
  echo "  $secret"

  if info=$(describe_value "$secret" latest); then
    len=${info%% *}
    prefix=${info##* }

    if ! looks_right "$secret" latest; then
      want=$(expected_prefix "$secret")
      echo "    latest: readable but WRONG SHAPE (${len} bytes, starts"
      echo "            '${prefix}' -- expected something starting '${want}')."
      echo "            Readable is not correct: this would fail at the API,"
      echo "            not at startup, which is much harder to notice."
      PROBLEMS=$((PROBLEMS + 1))

      if candidate=$(newest_good_version "$secret"); then
        cinfo=$(describe_value "$secret" "$candidate")
        echo "    newest correct-looking version: $candidate (${cinfo%% *} bytes,"
        echo "            starts '${cinfo##* }')"
        if [ "$APPLY" -eq 0 ]; then
          echo "    WOULD copy version $candidate forward so latest is correct again."
          echo "    (re-run with --apply to do it)"
        else
          repair_from "$secret" "$candidate"
        fi
      else
        echo "    no enabled version has the expected shape. The correct value"
        echo "    has to come from its source (the Stripe dashboard and so on)."
      fi
      continue
    fi

    echo "    latest: readable (${len} bytes, starts '${prefix}')"

    # A trailing newline is invisible everywhere except an exact-match
    # comparison, and it cannot be reproduced in an HTTP header -- a key
    # stored with one can never be sent correctly by any client.
    if gcloud secrets versions access latest --secret="$secret" --project="$PROJECT" 2>/dev/null \
        | od -c | tail -2 | grep -q '\\n'; then
      echo "    WARNING: value ends in a newline. If this is compared exactly"
      echo "             (an API key), no HTTP client can ever match it."
      PROBLEMS=$((PROBLEMS + 1))
    fi
    continue
  fi

  echo "    latest: NOT READABLE -- a container mounting this fails to start."
  PROBLEMS=$((PROBLEMS + 1))

  if candidate=$(newest_good_version "$secret"); then
    cinfo=$(describe_value "$secret" "$candidate")
    echo "    newest usable version: $candidate (${cinfo%% *} bytes, starts '${cinfo##* }')"
    if [ "$APPLY" -eq 0 ]; then
      echo "    WOULD copy version $candidate forward so latest resolves again."
      echo "    (re-run with --apply to do it)"
    else
      repair_from "$secret" "$candidate"
    fi
  else
    echo "    no enabled version holds a usable value. This cannot be repaired"
    echo "    from inside the project -- the value has to come from its source"
    echo "    (the Stripe dashboard, the facilitator's portal, and so on)."
  fi
done

echo
echo "-----------------------------------------------"
if [ "$APPLY" -eq 0 ]; then
  printf '  %d problem(s) found. Nothing was changed.\n' "$PROBLEMS"
  [ "$PROBLEMS" -gt 0 ] && echo "  Re-run with --apply to repair what is repairable."
else
  printf '  %d problem(s) found, %d repaired.\n' "$PROBLEMS" "$REPAIRED"
  [ "$REPAIRED" -gt 0 ] && echo "  Redeploy so containers pick up the new versions:" \
    && echo "    bash scripts/repair-and-deploy.sh"
fi
echo "-----------------------------------------------"
echo
echo "Nothing here disables or destroys anything. Every repair adds a new"
echo "version copied from an existing one, so all previous versions remain."
echo
