#!/usr/bin/env bash
#
# Measure what one audit call actually costs to serve, and compare it to what
# the call sells for.
#
# This is the number the whole per-call business model rests on and it has
# never been measured. At $0.05 a single audit and $0.15 a bundle, the gross
# margin per call decides whether volume is the thing that makes money or the
# thing that loses it faster. Every other traffic decision is downstream of it.
#
# Run this in Cloud Shell (it needs gcloud auth and network access to the
# service, neither of which a build sandbox has):
#
#   bash scripts/measure-call-cost.sh --calls 10
#
# What it does:
#   1. Reads the live service's CPU / memory / concurrency / min-instances.
#   2. Drives N real audit calls, discarding the first as cold-start warmup.
#   3. Reads billable instance time from Cloud Monitoring over that window.
#   4. Computes cost per call two independent ways and prints both.
#
# It costs real money to run: N calls at the published rate, plus the compute.
# Ten bundle calls is about $1.00 of toll. That is the cheapest possible way
# to learn whether the unit economics work.

set -uo pipefail

PROJECT="${PROJECT:-resolver-time}"
SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
BASE_URL="${BASE_URL:-https://hubvibe-io.com}"
ENDPOINT="bundle"
TARGET_URL="${TARGET_URL:-https://example.com}"
CALLS=10

# Cloud Run request-based billing rates, USD. These are DEFAULTS, not gospel:
# rates differ between pricing tiers and change over time. Verify the current
# figures for your region at https://cloud.google.com/run/pricing and override
# with CPU_RATE / MEM_RATE if they differ. Getting these wrong changes the
# answer proportionally, so the script prints them back for checking.
CPU_RATE="${CPU_RATE:-0.000024}"   # per vCPU-second
MEM_RATE="${MEM_RATE:-0.0000025}"  # per GiB-second
REQ_RATE="${REQ_RATE:-0.40}"       # per million requests

PRICE_BUNDLE="0.15"
PRICE_SINGLE="0.05"

while [ $# -gt 0 ]; do
  case "$1" in
    --calls) CALLS="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --url) TARGET_URL="$2"; shift 2 ;;
    --help|-h)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

# Resolve a key rather than demanding an export (see lib-api-key.sh): the
# demand is what made this script unrunnable without a two-step incantation.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-api-key.sh
[ -f "$_LIB_DIR/lib-api-key.sh" ] && . "$_LIB_DIR/lib-api-key.sh"

CALL_KEY=""
if declare -f hv_resolve_api_key >/dev/null 2>&1; then
  hv_resolve_api_key && CALL_KEY="$HV_API_KEY"""
fi

if [ -z "$CALL_KEY" ]; then
  {
    echo "error: no usable API key, so every call would return 402."
    echo "       ${HV_KEY_PROBLEM:-no key could be resolved}"
    echo
    echo "Without a key nothing is audited, and the measurement would report"
    echo "the cost of serving a payment challenge -- not the number you want."
    echo "Override with:  HUBVIBE_API_KEY=your_key bash scripts/measure-call-cost.sh"
  } >&2
  exit 64
fi
echo "Using $HV_KEY_SOURCE"

command -v gcloud >/dev/null || { echo "error: gcloud not found. Run this in Cloud Shell." >&2; exit 64; }
command -v python3 >/dev/null || { echo "error: python3 not found." >&2; exit 64; }

echo "== service configuration =="
# Dump the whole record as JSON and pick fields out of it, rather than asking
# gcloud for a delimited projection. A multi-field --format='value[delimiter](
# a,b,c)' silently produced a repeated, unparseable line here -- every field
# came back as the whole tuple -- and the cut(1) parsing downstream turned
# that into confident garbage ("cpu=2 2Gi 4 memory=2 2Gi 4 ..."). A filtered
# view is not a record.
gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format=json > /tmp/hv_svc.json 2>/dev/null

if [ ! -s /tmp/hv_svc.json ]; then
  echo "error: could not read service $SERVICE in $REGION (project $PROJECT)." >&2
  exit 1
fi

CONFIG=$(python3 - <<'PY'
import json

with open("/tmp/hv_svc.json") as handle:
    svc = json.load(handle)

template = svc.get("spec", {}).get("template", {})
container = (template.get("spec", {}).get("containers") or [{}])[0]
limits = container.get("resources", {}).get("limits") or {}
annotations = template.get("metadata", {}).get("annotations") or {}

print("\t".join([
    str(limits.get("cpu") or "1"),
    str(limits.get("memory") or "512Mi"),
    str(template.get("spec", {}).get("containerConcurrency") or 1),
    str(annotations.get("autoscaling.knative.dev/minScale") or 0),
    str(annotations.get("run.googleapis.com/cpu-throttling") or "true"),
]))
PY
)

CPU=$(echo "$CONFIG" | cut -f1)
MEM=$(echo "$CONFIG" | cut -f2)
CONCURRENCY=$(echo "$CONFIG" | cut -f3)
MIN_SCALE=$(echo "$CONFIG" | cut -f4)
THROTTLING=$(echo "$CONFIG" | cut -f5)

printf '  cpu=%s memory=%s concurrency=%s min-instances=%s cpu-throttling=%s\n' \
  "${CPU:-?}" "${MEM:-?}" "${CONCURRENCY:-?}" "${MIN_SCALE:-0}" "${THROTTLING:-true}"

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo
echo "== driving $CALLS calls to /audit/$ENDPOINT against $TARGET_URL =="
echo "   (first call is discarded as cold-start warmup)"

python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({"url": os.environ["TARGET_URL"]}))' \
  TARGET_URL="$TARGET_URL" > /tmp/hv_req.json 2>/dev/null \
  || TARGET_URL="$TARGET_URL" python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({"url": os.environ["TARGET_URL"]}))' > /tmp/hv_req.json

: > /tmp/hv_times.txt
FAILURES=0
for i in $(seq 0 "$CALLS"); do
  t=$(curl -s -o /tmp/hv_resp.json -w '%{time_total} %{http_code}' \
        --max-time 180 -X POST "$BASE_URL/audit/$ENDPOINT" \
        -H "X-API-Key: $CALL_KEY" \
        -H 'Content-Type: application/json' \
        --data-binary @/tmp/hv_req.json) || t="0 000"
  secs=$(echo "$t" | cut -d' ' -f1)
  code=$(echo "$t" | cut -d' ' -f2)
  if [ "$i" -eq 0 ]; then
    printf '  warmup: %ss (HTTP %s) -- discarded\n' "$secs" "$code"
    continue
  fi
  if [ "$code" != "200" ]; then
    FAILURES=$((FAILURES + 1))
    printf '  call %2d: HTTP %s -- EXCLUDED (no audit ran, nothing billed)\n' "$i" "$code"
    continue
  fi
  printf '  call %2d: %ss\n' "$i" "$secs"
  echo "$secs" >> /tmp/hv_times.txt
done

SUCCEEDED=$(wc -l < /tmp/hv_times.txt | tr -d ' ')
if [ "$SUCCEEDED" -eq 0 ]; then
  echo >&2
  echo "error: no call succeeded, so there is nothing to measure." >&2
  echo "Last HTTP status: $code" >&2
  echo "Last response body:" >&2
  cat /tmp/hv_resp.json >&2
  echo >&2
  # A failed measurement is only useful if it says what to do next. These are
  # the three codes this script can actually provoke, and they mean very
  # different things.
  case "$code" in
    402)
      echo "402 means the key was not accepted. Check HUBVIBE_API_KEY is a real" >&2
      echo "key -- a placeholder string falls through to a payment challenge." >&2
      ;;
    500)
      echo "500 is a server-side fault, not a payment or input problem: the" >&2
      echo "service raised an unhandled exception. Nothing was billed. Get the" >&2
      echo "actual traceback -- it is the only thing that identifies the cause:" >&2
      echo >&2
      echo "  gcloud logging read 'resource.labels.service_name=$SERVICE AND severity>=ERROR' --project=$PROJECT --freshness=1h --limit=5 --format='value(textPayload,jsonPayload.message)'" >&2
      ;;
    502)
      echo "502 means the audit itself could not run (the target page failed to" >&2
      echo "load). Nothing was billed. Try a different --url." >&2
      ;;
    000)
      echo "000 means the request never completed -- network, or a cold start" >&2
      echo "slower than the timeout. Retry, or raise --calls timeout." >&2
      ;;
  esac
  exit 1
fi

END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo
echo "== billable instance time from Cloud Monitoring =="
TOKEN=$(gcloud auth print-access-token 2>/dev/null)
FILTER="metric.type=\"run.googleapis.com/container/billable_instance_time\" AND resource.labels.service_name=\"$SERVICE\""
BILLABLE=$(curl -s -G "https://monitoring.googleapis.com/v3/projects/$PROJECT/timeSeries" \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "filter=$FILTER" \
  --data-urlencode "interval.startTime=$START" \
  --data-urlencode "interval.endTime=$END" \
  --data-urlencode "aggregation.alignmentPeriod=600s" \
  --data-urlencode "aggregation.perSeriesAligner=ALIGN_DELTA" \
  --data-urlencode "aggregation.crossSeriesReducer=REDUCE_SUM" \
  2>/dev/null \
  | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
total = 0.0
for series in d.get("timeSeries", []):
    for pt in series.get("points", []):
        v = pt.get("value", {})
        total += float(v.get("doubleValue", v.get("int64Value", 0)) or 0)
print(total if total else "")
')

if [ -n "$BILLABLE" ]; then
  printf '  billable instance-seconds in window: %s\n' "$BILLABLE"
else
  echo "  (unavailable -- monitoring data lags by a few minutes; re-run the"
  echo "   cost math later with the same window, or rely on the latency method)"
fi

echo
echo "== cost per call =="
CPU_CORES=$(echo "${CPU:-1}" | sed 's/m$//' | awk '{ if (index("'"${CPU:-1}"'","m")) print $1/1000; else print $1 }')
MEM_GIB=$(python3 - <<PY
m = "${MEM:-512Mi}".strip()
mult = {"Gi": 1.0, "G": 0.931, "Mi": 1/1024, "M": 0.000931}
for suf, f in sorted(mult.items(), key=lambda kv: -len(kv[0])):
    if m.endswith(suf):
        print(round(float(m[:-len(suf)]) * f, 6)); break
else:
    print(0.5)
PY
)

MEAN=$(awk '{s+=$1; n++} END {if(n) printf "%.4f", s/n}' /tmp/hv_times.txt)
P95=$(sort -n /tmp/hv_times.txt | awk '{a[NR]=$1} END {printf "%.4f", a[int(NR*0.95+0.999)]}')

python3 - <<PY
cpu   = float("$CPU_CORES")
gib   = float("$MEM_GIB")
mean  = float("$MEAN")
p95   = float("$P95")
n     = int("$SUCCEEDED")
conc  = int("${CONCURRENCY:-1}" or 1)
minsc = int("${MIN_SCALE:-0}" or 0)
billable = "$BILLABLE".strip()

cpu_rate = float("$CPU_RATE")
mem_rate = float("$MEM_RATE")
req_rate = float("$REQ_RATE") / 1_000_000

per_sec = cpu * cpu_rate + gib * mem_rate
print(f"  allocation: {cpu} vCPU, {gib} GiB  ->  \${per_sec:.8f} per instance-second")
print(f"  rates used: cpu=\${cpu_rate}/vCPU-s  mem=\${mem_rate}/GiB-s  req=\${req_rate*1e6}/M")
print()

# Method 1 -- wall-clock latency. This is an UPPER bound at concurrency 1:
# it charges the full allocation for the whole request, including any time the
# instance spent waiting on the audited site's network rather than computing.
lat_cost = mean * per_sec + req_rate
lat_p95  = p95  * per_sec + req_rate
print(f"  Method 1 (latency x allocation, upper bound at concurrency 1)")
print(f"    mean {mean:.3f}s  ->  \${lat_cost:.5f} per call")
print(f"    p95  {p95:.3f}s  ->  \${lat_p95:.5f} per call")
if conc > 1:
    print(f"    concurrency is {conc}: under real concurrent load the compute")
    print(f"    share divides toward \${(mean*per_sec)/conc:.8f} + \${req_rate:.8f} request fee,")
    print(f"    but only if calls actually overlap. These were sequential.")
print()

# Method 2 -- what Cloud Run actually billed. Ground truth, when available.
if billable:
    b = float(billable)
    bill_cost = (b * per_sec) / n + req_rate
    print(f"  Method 2 (billable instance time, ground truth)")
    print(f"    {b:.1f} instance-seconds / {n} calls  ->  \${bill_cost:.5f} per call")
    print(f"    NOTE: this window also absorbed the warmup call and any idle")
    print(f"    billing, so it overstates the marginal cost of one more call.")
    chosen = bill_cost
else:
    print("  Method 2 unavailable (monitoring lag). Using Method 1.")
    chosen = lat_cost
print()

print("== margin ==")
for label, price in (("bundle", 0.15), ("single audit", 0.05)):
    margin = price - chosen
    pct = (margin / price) * 100
    verdict = "WORKS" if margin > 0 else "LOSES MONEY ON EVERY CALL"
    print(f"  {label:<13} \${price:.2f} - \${chosen:.5f} = \${margin:+.5f}  ({pct:+.1f}%)  {verdict}")
print()

if chosen > 0:
    for label, price in (("bundle", 0.15), ("single audit", 0.05)):
        m = price - chosen
        if m > 0:
            calls_for_1m = 1_000_000 / m
            print(f"  \$1M of gross margin at {label} rates = {calls_for_1m:,.0f} paid calls")
            print(f"    = {calls_for_1m/365/86400:,.1f} calls/second sustained for a year")
print()

if minsc > 0:
    idle_month = minsc * per_sec * 60 * 60 * 24 * 30
    print(f"  WARNING: min-instances={minsc}. That is \${idle_month:,.2f}/month of")
    print(f"  idle billing before a single call arrives. At low volume this, not")
    print(f"  per-call compute, is the dominant cost.")
    print()

print("  Not included: egress, Firestore/billing writes, x402 facilitator fees,")
print("  Stripe's cut on subscription keys, or the free tier. Egress and")
print("  facilitator fees push the real number UP; the free tier pushes it down")
print("  only until you have volume, at which point it stops mattering.")
PY

echo
echo "Cross-check against real billing (no estimation at all):"
echo "  console.cloud.google.com/billing -> Reports -> filter Service='Cloud Run'"
echo "  Divide that by the audit call count for the same period."
