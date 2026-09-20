#!/usr/bin/env bash
# What the worker network earned, what it cost, and who came back.
#
#     bash scripts/worker-ledger.sh            # all time
#     HOURS=24 bash scripts/worker-ledger.sh   # last 24h
#
# WHY THIS EXISTS
#
# The pay-to wallet answers "how much money arrived". It cannot answer the two
# questions that decide what to build next: WHICH capability earned it, and
# whether that capability is profitable after the provider's bill. This reads
# the per-call ledger the worker routes write.
#
# WHAT IT WILL NOT DO
#
# It never prints a margin it cannot stand behind. A call whose provider cost
# was not measured is counted separately as "unmeasured" -- never folded in at
# zero cost, which would report a capability as pure profit precisely when we
# do not know what it cost. To turn unmeasured into measured, configure the
# published rates the adapters ask for:
#     GEMINI_PRICE_PER_MTOK_IN / GEMINI_PRICE_PER_MTOK_OUT   (Vertex)
#     BQ_PRICE_PER_TIB                                        (BigQuery)
# Token counts and bytes scanned are ALWAYS measured; only the rate is config.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${WORKER_LEDGER_PATH:-/data/hubvibe-workers.db}"
HOURS="${HOURS:-}"

if [ ! -f "$DB" ]; then
  echo "No worker ledger at $DB"
  echo "(Set WORKER_LEDGER_PATH, or run this on the node where the workers serve.)"
  exit 1
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" - "$DB" "$HOURS" "$REPO_ROOT" <<'PYEOF'
import importlib.util
import os
import sys
from pathlib import Path

db, hours, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["WORKER_LEDGER_PATH"] = db

pkg = Path(repo_root) / "wcag-audit-engine" / "app" / "workers"
spec = importlib.util.spec_from_file_location(
    "wcag_audit_engine_workers", pkg / "__init__.py",
    submodule_search_locations=[str(pkg)])
workers = importlib.util.module_from_spec(spec)
sys.modules["wcag_audit_engine_workers"] = workers
spec.loader.exec_module(workers)
ledger = workers.ledger

since = float(hours) * 3600 if hours else None
window = f"last {hours}h" if hours else "all time"

rows = ledger.summary(since)
if not rows:
    print(f"No worker calls recorded ({window}).")
    raise SystemExit(0)

print(f"\nHubVibe worker network -- {window}\n")
header = (f"{'worker':<22} {'calls':>6} {'ok':>5} {'fail':>5} {'paid':>5} "
          f"{'revenue':>9} {'cost':>9} {'margin':>8} {'unmeas':>7} {'p50ms':>7}")
print(header)
print("-" * len(header))

total_revenue = total_cost = 0.0
total_calls = total_ok = 0
for row in rows:
    margin = row["measured_gross_margin_pct"]
    margin_text = f"{margin:.1f}%" if margin is not None else "  n/a"
    cost = row["measured_provider_cost_usd"]
    cost_text = f"${cost:.5f}" if cost is not None else "    n/a"
    print(f"{row['worker']:<22} {row['calls']:>6} {row['ok']:>5} {row['failed']:>5} "
          f"{row['settled']:>5} ${row['revenue_usd']:>8.4f} {cost_text:>9} "
          f"{margin_text:>8} {row['unmeasured_calls']:>7} "
          f"{(row['avg_latency_ms'] or 0):>7}")
    total_revenue += row["revenue_usd"] or 0
    total_cost += row["measured_provider_cost_usd"] or 0
    total_calls += row["calls"] or 0
    total_ok += row["ok"] or 0

print("-" * len(header))
rate = (100.0 * total_ok / total_calls) if total_calls else 0.0
print(f"{'TOTAL':<22} {total_calls:>6} {total_ok:>5} {'':>5} {'':>5} "
      f"${total_revenue:>8.4f} ${total_cost:>8.5f}")
print(f"\nMeasured completion rate: {rate:.2f}%  ({total_ok}/{total_calls} calls)")
print("This is the measured figure. It is not a target and not a claim.")

unmeasured = sum(r["unmeasured_calls"] or 0 for r in rows)
if unmeasured:
    print(f"\n{unmeasured} settled call(s) had unmeasured provider cost and are "
          f"EXCLUDED from every margin above.")
    print("Set GEMINI_PRICE_PER_MTOK_IN/_OUT and BQ_PRICE_PER_TIB to measure them.")

print("\nProviders")
for row in ledger.provider_health(since):
    success = row["success_rate_pct"]
    print(f"  {row['provider']:<32} {row['attempts']:>5} attempts  "
          f"{(f'{success:.1f}%' if success is not None else 'n/a'):>7} ok  "
          f"{(row['avg_latency_ms'] or 0):>6}ms avg")

repeats = ledger.repeat_payers(since)
print(f"\nRepeat payers: {len(repeats)}")
for row in repeats[:15]:
    print(f"  {row['payer']:<44} {row['paid_calls']:>4} calls  "
          f"{row['distinct_workers']:>2} workers  ${row['spend_usd']:.4f}")
if not repeats:
    print("  (none yet -- a payer appears here only after a SECOND paid call)")
print()
PYEOF
