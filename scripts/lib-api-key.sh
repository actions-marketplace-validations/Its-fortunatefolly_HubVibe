# shellcheck shell=bash
#
# Resolve an API key for the live service without a human in the loop.
#
# Sourced by verify-live.sh and measure-call-cost.sh. Not executable on its own.
#
# Why this exists: the API key broke six separate ways in one evening, and
# every one of them was the same root cause -- the key had to be hand-exported
# into the shell, so anything needing it was one forgotten step away from
# failing:
#
#   * `HUBVIBE_API_KEY=<real key>` pasted literally; bash read `<` as a
#     redirect. Twice.
#   * the key lives in Secret Manager, so using it first required knowing
#     which secret backs AUDIT_API_KEY
#   * guessing that name wrote a test string into the Stripe key secret
#   * a fresh Cloud Shell drops the export, so verify-live.sh printed SKIP on
#     every run afterwards
#
# The consequence was worse than the friction: the paid path -- the one check
# that answers "can this service take money" -- was skipped by default,
# permanently. Everything else could be green while the thing that earns was
# dead, which is exactly what happened for an unknown number of days.
#
# So: resolve it automatically. The service already records which secret backs
# AUDIT_API_KEY, and these scripts already run with gcloud auth. Nothing about
# that needed a human.
#
# The key is never printed. On failure the caller is told what could not be
# resolved, never the value.

# hv_resolve_api_key
#
# Sets HV_API_KEY to a usable key and returns 0, or returns 1 with
# HV_KEY_PROBLEM explaining why not. HV_KEY_SOURCE carries provenance.
#
# It sets a variable rather than echoing, deliberately. An echoing version
# forces callers into `k=$(hv_resolve_api_key)`, and command substitution runs
# in a subshell -- so HV_KEY_SOURCE and HV_KEY_PROBLEM were set in a process
# that exits immediately, and every caller saw them empty. The diagnosis was
# there and invisible, which is the failure mode this whole file exists to
# stop.
hv_resolve_api_key() {
  HV_API_KEY=""
  HV_KEY_SOURCE=""
  HV_KEY_PROBLEM=""

  # An explicit export always wins -- someone testing a specific customer key
  # must not be silently overridden by the internal one.
  if [ -n "${HUBVIBE_API_KEY:-}" ]; then
    HV_KEY_SOURCE="HUBVIBE_API_KEY from the environment"
    HV_API_KEY="$HUBVIBE_API_KEY"
    return 0
  fi

  local service="${SERVICE:-hubvibe}"
  local region="${REGION:-us-south1}"
  # Always passed, never inherited: a fresh Cloud Shell has no default
  # project, and gcloud answers that with an empty result rather than an
  # error -- which this function would then report as "could not read
  # service hubvibe", about a service that is running.
  local project="${PROJECT:-resolver-time}"
  local project_args=(--project="$project")

  if ! command -v gcloud >/dev/null 2>&1; then
    HV_KEY_PROBLEM="gcloud is not on PATH, so the key cannot be resolved automatically"
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    HV_KEY_PROBLEM="python3 is not on PATH, so the service config cannot be parsed"
    return 1
  fi

  local svc_json
  svc_json=$(mktemp) || return 1
  if ! gcloud run services describe "$service" --region="$region" "${project_args[@]}" \
       --format=json > "$svc_json" 2>/dev/null || [ ! -s "$svc_json" ]; then
    rm -f "$svc_json"
    HV_KEY_PROBLEM="could not read service $service in $region"
    return 1
  fi

  # Which secret backs AUDIT_API_KEY. Read from JSON, never grepped out of
  # `flattened` output -- gcloud pads that format with alignment spaces, and
  # a pattern that silently matches nothing is how several of this evening's
  # bugs stayed invisible.
  local secret_name
  secret_name=$(python3 -c '
import json, sys
try:
    svc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == "AUDIT_API_KEY":
        ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
        if ref.get("name"):
            print(ref["name"])
        elif entry.get("value"):
            print("__PLAIN__" + entry["value"])
        break
' "$svc_json" 2>/dev/null)
  rm -f "$svc_json"

  if [ -z "$secret_name" ]; then
    HV_KEY_PROBLEM="the service has no AUDIT_API_KEY set, so there is no internal key to use"
    return 1
  fi

  # Set as a literal rather than a secret reference.
  case "$secret_name" in
    __PLAIN__*)
      HV_KEY_SOURCE="AUDIT_API_KEY (literal value on the service)"
      HV_API_KEY="${secret_name#__PLAIN__}"
      return 0
      ;;
  esac

  # `$( )` strips trailing newlines, which is exactly the byte that has to be
  # detected here -- so capture with a sentinel appended and remove it after,
  # leaving any real trailing newline intact.
  local raw
  raw=$(gcloud secrets versions access latest --secret="$secret_name" \
          "${project_args[@]}" 2>/dev/null; printf 'x') || true
  raw="${raw%x}"
  if [ -z "$raw" ]; then
    HV_KEY_PROBLEM="secret $secret_name backs AUDIT_API_KEY but its latest version could not be read"
    return 1
  fi

  # A trailing newline in the stored secret is unfixable from the client side:
  # the container's env var keeps the newline, `$( )` strips it here, and an
  # HTTP header cannot carry one -- so no request can ever match. Say so
  # rather than handing back a key that will always 402.
  # Strip one trailing newline; if that changed the value, there was one.
  # NOT `case "$raw" in *"$(printf '\n')")` -- command substitution strips
  # trailing newlines, so that pattern evaluates to *"" and matches every
  # key. It did, and reported a perfectly good key as broken.
  if [ "${raw%$'\n'}" != "$raw" ]; then
    HV_KEY_PROBLEM="secret $secret_name ends in a newline. The container compares
        exactly, and an HTTP header cannot carry a newline, so NO client can ever
        authenticate with it. Rewrite it without one:
          printf '%s' 'THE_KEY' | gcloud secrets versions add $secret_name --data-file=-"
    return 1
  fi

  HV_KEY_SOURCE="AUDIT_API_KEY via Secret Manager secret '$secret_name'"
  HV_API_KEY="$raw"
  return 0
}
