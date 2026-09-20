#!/usr/bin/env bash
# The funnel, from the node's own logs: who arrives, who bounces on the
# 402, who pays -- per route, per client, per facilitator.
#
#     bash scripts/traffic-ledger.sh            # last 24h, on the box
#     SINCE=7d bash scripts/traffic-ledger.sh   # last 7 days
#     ... | bash scripts/traffic-ledger.sh --stdin   # summarise piped log lines
#
# Reads two streams `docker compose logs` already holds:
#   * Caddy's JSON access log (deploy/vps/Caddyfile enables it): one line per
#     request with client_ip, method, uri, status, duration and User-Agent.
#   * The app log: `x402 SETTLED` lines (one per paid call, with the
#     facilitator) and `x402 audit WITHHELD` lines (a settle the
#     facilitator refused).
#
# Discovery work is measured by whether machine traffic arrives, trusts and
# pays. A 402 with no follow-up payment from the same client is an agent
# that found the node and could not or would not pay -- the number to watch.
# Nothing here is a metric to optimise a website for; there is no website
# funnel. It is the tollbooth's own count.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$REPO_ROOT/deploy/vps}"
SINCE="${SINCE:-24h}"

# The summariser is passed with -c, not on stdin: stdin carries the log lines.
read -r -d '' LEDGER_PY <<'PY'
import json, re, sys
from collections import Counter, defaultdict

since = sys.argv[1]
requests = []          # (client, ua, method, path, status)
settled = Counter()    # facilitator -> count
withheld = 0
for raw in sys.stdin:
    line = raw.rstrip("\n")
    # `docker compose logs` prefixes "name  | "; strip it.
    line = re.sub(r"^\S+\s+\|\s?", "", line)
    # Timestamps from `logs -t`.
    line = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s", "", line)
    if line.startswith("{"):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        req = entry.get("request")
        if not isinstance(req, dict) or "status" not in entry:
            continue
        ua = (req.get("headers", {}).get("User-Agent") or ["-"])[0]
        requests.append((
            req.get("client_ip") or req.get("remote_ip") or "-",
            ua, req.get("method", "-"), (req.get("uri") or "-").split("?")[0],
            int(entry.get("status") or 0),
        ))
        continue
    m = re.search(r"x402 SETTLED \(settle\).*?facilitator=(\S+)", line)
    if m:
        settled[m.group(1)] += 1
    elif "x402 SETTLED" in line:
        settled["(facilitator not in line)"] += 1
    elif "x402 audit WITHHELD" in line:
        withheld += 1

audit = [r for r in requests if r[3].startswith("/audit") or r[3] == "/mcp"]
by_route = defaultdict(Counter)
for client, ua, method, path, status in audit:
    by_route[path][status] += 1
clients = Counter(r[0] for r in audit)
agents = Counter(r[1][:60] for r in audit)
challenged = {r[0] for r in audit if r[4] == 402}
# A 200 on /mcp is mostly initialize/tools/list -- free discovery, not a sale.
paid = {r[0] for r in audit if r[4] == 200 and r[3] != "/mcp"}
bounced = challenged - paid

print(f"Traffic ledger -- last {since}")
print(f"  audit/mcp requests: {len(audit)}   distinct clients: {len(clients)}")
print(f"  paid (200): {sum(c[200] for p, c in by_route.items() if p != '/mcp')}   "
      f"challenged (402): {sum(c[402] for c in by_route.values())}   "
      f"failed (502): {sum(c[502] for c in by_route.values())}   "
      f"withheld on refused settle: {withheld}")
print(f"  clients that saw a 402 and never paid: {len(bounced)}")
# An unpaid probe is priced before routing or validation, so these should be
# near zero; a climb here means crawlers are bouncing before the 402 again.
unpriced = sum(c[s] for p, c in by_route.items() if p != "/mcp" for s in (400, 405, 422))
print(f"  audit requests refused before the price (400/405/422): {unpriced}")
if by_route:
    print("  per route (status: count):")
    for path in sorted(by_route):
        counts = ", ".join(f"{s}: {n}" for s, n in sorted(by_route[path].items()))
        print(f"    {path:<22} {counts}")
if agents:
    print("  top agents (User-Agent):")
    for ua, n in agents.most_common(8):
        print(f"    {n:>5}  {ua}")
if settled:
    print("  settlements by facilitator (app log):")
    for fac, n in settled.most_common():
        print(f"    {n:>5}  {fac}")
else:
    print("  settlements: none in this window")
if not requests:
    print("  no access-log lines seen: is Caddy's `log` directive deployed? "
          "(deploy/vps/Caddyfile is bind-mounted and read only on start, so a "
          "rebuild is not enough: restart the caddy container from deploy/vps)")
PY

summarise() {
  python3 -c "$LEDGER_PY" "$SINCE"
}

if [ "${1:-}" = "--stdin" ]; then
  summarise
  exit 0
fi

command -v docker >/dev/null 2>&1 || {
  printf 'docker is not on PATH. Run this on the box, or pipe log lines in with --stdin.\n' >&2
  exit 1
}
docker compose -f "$COMPOSE_DIR/docker-compose.yml" --project-directory "$COMPOSE_DIR" \
  logs --no-color --since "$SINCE" caddy hubvibe 2>/dev/null | summarise
