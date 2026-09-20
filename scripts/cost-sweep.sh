#!/usr/bin/env bash
# Find what is billing this project that is NOT the tollbooth, and stop it.
#
#   bash scripts/cost-sweep.sh              # report, exit 1 if something idle bills
#   DELETE_IDLE=1 bash scripts/cost-sweep.sh  # also delete Cloud Workstations
#
# WHY: the billing report for resolver-time read $193.84 for August and the
# same for July -- $189.73 of it from us-central1, split between "Cloud
# Workstations" and "Cloud Workstations control plane fee". Not Cloud Run.
# A workstation cluster bills its control plane every hour it exists, whether
# or not anyone has opened a workstation, and nothing in this repo ever used
# one. That, not the node, was the bill. The tollbooth at min-instances 0
# costs about $0.25/month.
#
# Cloud Workstations is deleted (with DELETE_IDLE=1) because it is pure loss
# here. Compute Engine VMs, Cloud SQL and GKE are only listed: they might be
# someone's, and a list is enough to decide.

set -uo pipefail
PROJECT="${PROJECT:-resolver-time}"
# gcloud asks "API not enabled. Enable and retry? (y/N)" on `sql instances
# list` and friends. With stderr silenced that prompt is invisible and the
# sweep hung on it (2026-09-04). Never prompt; a "no" is the right answer.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mBILLING\033[0m  %s\n' "$1"; }

command -v gcloud >/dev/null 2>&1 || { bad "gcloud is not on PATH"; exit 2; }
FOUND=0

step "Cloud Workstations in $PROJECT (control plane bills every hour it exists)"
# Full resource names: projects/P/locations/REGION/workstationClusters/NAME
# --uri, not value(name): the latter printed the short name, the region
# parsed to "", and the delete ran with --region= (2026-09-04).
CLUSTERS=$(gcloud workstations clusters list --project="$PROJECT" --uri 2>/dev/null)
if [ -z "$CLUSTERS" ]; then
  # Some gcloud versions need a region to list; the billed one was us-central1.
  for region in us-central1 us-east1 us-west1 us-south1 europe-west1; do
    CLUSTERS+=$(gcloud workstations clusters list --project="$PROJECT" --region="$region" --uri 2>/dev/null)
    CLUSTERS+=$'\n'
  done
fi
CLUSTERS=$(printf '%s\n' "$CLUSTERS" | sed '/^$/d' | sort -u)

if [ -z "$CLUSTERS" ]; then
  ok "no workstation clusters"
else
  while IFS= read -r cluster_path; do
    [ -n "$cluster_path" ] || continue
    region=$(printf '%s' "$cluster_path" | sed -n 's|.*/locations/\([^/]*\)/.*|\1|p')
    cluster=${cluster_path##*/}
    if [ -z "$region" ]; then
      # Belt and braces: the only region ever billed for this.
      region=us-central1
    fi
    FOUND=1
    bad "workstation cluster '$cluster' in $region -- about \$0.20/hour for the control plane alone"
    CONFIGS=$(gcloud workstations configs list --project="$PROJECT" --region="$region" \
      --cluster="$cluster" --uri 2>/dev/null | sed 's|.*/||')
    if [ "${DELETE_IDLE:-}" = "1" ]; then
      for config in $CONFIGS; do
        for ws in $(gcloud workstations list --project="$PROJECT" --region="$region" \
                      --cluster="$cluster" --config="$config" --uri 2>/dev/null | sed 's|.*/||'); do
          gcloud workstations delete "$ws" --project="$PROJECT" --region="$region" \
            --cluster="$cluster" --config="$config" --quiet >/dev/null 2>&1 \
            && ok "deleted workstation $ws" || warn "could not delete workstation $ws"
        done
        gcloud workstations configs delete "$config" --project="$PROJECT" --region="$region" \
          --cluster="$cluster" --quiet >/dev/null 2>&1 \
          && ok "deleted config $config" || warn "could not delete config $config"
      done
      if gcloud workstations clusters delete "$cluster" --project="$PROJECT" --region="$region" \
           --force --quiet >/dev/null 2>&1 \
         || gcloud workstations clusters delete "$cluster" --project="$PROJECT" --region="$region" \
           --quiet >/dev/null 2>&1; then
        ok "deleted cluster $cluster -- the control plane meter is off"
      else
        warn "could not delete cluster $cluster. By hand:"
        warn "  gcloud workstations clusters delete $cluster --region=$region --project=$PROJECT --force --quiet"
      fi
    else
      warn "to remove it (workstations, configs, then the cluster):"
      warn "  DELETE_IDLE=1 bash scripts/cost-sweep.sh"
    fi
  done <<< "$CLUSTERS"
fi

step "Other things that bill while idle (listed, not touched)"
VMS=$(gcloud compute instances list --project="$PROJECT" --format='value(name,zone,status)' 2>/dev/null)
if [ -n "$VMS" ]; then
  FOUND=1
  bad "Compute Engine VMs:"; printf '%s\n' "$VMS" | sed 's/^/          /'
  warn "  gcloud compute instances delete NAME --zone=ZONE --project=$PROJECT"
else
  ok "no Compute Engine VMs"
fi
SQL=$(gcloud sql instances list --project="$PROJECT" --format='value(name,region)' 2>/dev/null)
if [ -n "$SQL" ]; then
  FOUND=1
  bad "Cloud SQL instances:"; printf '%s\n' "$SQL" | sed 's/^/          /'
else
  ok "no Cloud SQL"
fi
GKE=$(gcloud container clusters list --project="$PROJECT" --format='value(name,location)' 2>/dev/null)
if [ -n "$GKE" ]; then
  FOUND=1
  bad "GKE clusters:"; printf '%s\n' "$GKE" | sed 's/^/          /'
else
  ok "no GKE clusters"
fi
MIN=$(gcloud run services describe hubvibe --project="$PROJECT" --region=us-south1 \
  --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])' 2>/dev/null)
if [ -n "$MIN" ] && [ "$MIN" != "0" ]; then
  FOUND=1
  bad "Cloud Run hubvibe min-instances=$MIN (a warm instance bills 24/7; repair-and-deploy.sh sets it to 0)"
else
  ok "Cloud Run hubvibe min-instances is 0 (idle costs nothing)"
fi

echo
if [ "$FOUND" -ne 0 ]; then
  if [ "${DELETE_IDLE:-}" = "1" ]; then
    warn "re-run without DELETE_IDLE to confirm the list is empty"
  fi
  exit 1
fi
ok "nothing idle is billing $PROJECT"
