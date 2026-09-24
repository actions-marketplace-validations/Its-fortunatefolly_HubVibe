#!/usr/bin/env bash
# Verify a deployed HubVibe node end to end, from outside.
#
# Every check here hits the real HTTPS URL, because "the code is on main" and
# "the endpoint answers correctly in production" have repeatedly turned out to
# be different things on this service -- a route that passed every local test
# still 500'd in the container because its data file was never COPYed into the
# image. Green tests do not prove a deploy.
#
# Usage:  bash scripts/verify-live.sh [BASE_URL]
#         BASE=https://... bash scripts/verify-live.sh
# Exit:   0 if everything expected is reachable and correct, 1 otherwise.

set -uo pipefail

# Positional first, then the BASE variable, then the production domain.
#
# Reading ONLY $1 was a trap: every other script here takes the node's URL
# from $BASE, so `BASE=http://... bash scripts/verify-live.sh` -- the form
# the runbook and habit both produce -- silently ignored it and checked
# PRODUCTION instead. Against a node that was fine, that read as 34 failures
# (2026-09-06); against a broken production it would read as someone else's
# node passing. A verifier that checks a different thing than it was asked
# to is worse than no verifier.
BASE="${1:-${BASE:-https://hubvibe-io.com}}"
FAILURES=0
PASSES=0

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASSES=$((PASSES + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# expect_status <method> <path> <expected> [description]
#
# Retries before failing. This script is usually run seconds after a deploy,
# which is exactly when Cloud Run is still migrating traffic to the new
# revision -- the first request or two can land mid-cutover and come back 404
# or 503 from the frontend even though the route is fine. A checker that
# reports a scary false failure in its most common usage window is a checker
# people learn to ignore, so a result only counts as a failure if it persists.
expect_status() {
  local method="$1" path="$2" expected="$3" desc="${4:-$path}"
  local code attempt

  for attempt in 1 2 3; do
    if [ "$method" = "POST" ]; then
      code=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$BASE$path" \
        -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
    else
      code=$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "$BASE$path" 2>/dev/null)
    fi

    if [ "$code" = "$expected" ]; then
      if [ "$attempt" -eq 1 ]; then
        pass "$desc -> $code"
      else
        pass "$desc -> $code (after $attempt attempts; first was transient)"
      fi
      return
    fi
    [ "$attempt" -lt 3 ] && sleep 3
  done

  fail "$desc -> got $code, expected $expected (persisted over 3 attempts)"
}

echo
echo "HubVibe live verification: $BASE"

# Say which checker this is, before saying anything about the node.
#
# A stale checkout is the most persistent way this script has lied. It is not
# a hypothetical: the payability checks landed in #61, and the next two runs
# against the deployed node still reported "34 passed, 0 failed" from a
# checkout that predated them -- a green that could not have gone red, read as
# proof the rail worked. The count is the tell, and nothing printed it next to
# what the count should be.
#
# So: name the commit, and if it is behind origin/main say so loudly. Fetch is
# --quiet and failure-tolerant; a checker that dies because the network hiccups
# while looking up its own version is worse than one that cannot tell.
if git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --git-dir >/dev/null 2>&1; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  HEAD_SHA=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)
  git -C "$REPO_DIR" fetch origin main --quiet 2>/dev/null || true
  BEHIND=$(git -C "$REPO_DIR" rev-list --count "HEAD..origin/main" 2>/dev/null || echo 0)
  if [ "${BEHIND:-0}" -gt 0 ]; then
    printf '\n  \033[31mSTALE CHECKOUT\033[0m  this copy is %s commit(s) behind origin/main.\n' "$BEHIND"
    printf '  You are running an OLD checker. Checks added since then cannot fail\n'
    printf '  here, so a clean run below proves less than it appears to. Fix first:\n\n'
    printf '      git -C %s fetch origin main && git -C %s reset --hard origin/main\n\n' \
      "$REPO_DIR" "$REPO_DIR"
  else
    echo "  checker: $HEAD_SHA (current with origin/main)"
  fi
fi
echo

echo "Service up"
# /health, not /healthz: Cloud Run's frontend reserves /healthz and answers
# it with its own 404 before the request reaches the container.
expect_status GET /health 200 "GET /health"
expect_status GET / 200 "GET / (landing page)"

# Is the box running THIS checkout's code? The block above warns when the
# CHECKER is stale; this is the same question pointed the other way, and it
# is the one nothing here used to ask.
#
# The Base app_id is the canary: one exact string, baked into the image at
# build time, changed only on purpose. Not hypothetical -- on 2026-09-07
# main carried a new app_id while the live page served the old one, every
# check here passed (an old image answers 200 on everything), and the only
# symptom was a Base domain verification that silently never completed.
# `git pull` on the box does not rebuild; `docker compose up -d --build`
# does. Skipped, out loud, when there is nothing local to compare against.
LOCAL_INDEX="${REPO_DIR:-}/wcag-audit-engine/app/static/index.html"
if [ -n "${REPO_DIR:-}" ] && [ -f "$LOCAL_INDEX" ]; then
  _app_id() { grep -o 'name="base:app_id" content="[^"]*"' | head -1 | sed 's/.*content="//; s/"$//'; }
  WANT_APP_ID=$(_app_id < "$LOCAL_INDEX")
  LIVE_APP_ID=$(curl -sS -m 30 "$BASE/" 2>/dev/null | _app_id)
  if [ -z "$WANT_APP_ID" ]; then
    printf '  \033[33mNOTE\033[0m  this checkout serves no base:app_id, so the deployed image cannot be compared to it.\n'
  elif [ "$LIVE_APP_ID" = "$WANT_APP_ID" ]; then
    pass "the deployed homepage is this checkout's ($WANT_APP_ID)"
  else
    fail "the node is running an OLDER IMAGE: its homepage serves base:app_id '${LIVE_APP_ID:-none}', this checkout serves '$WANT_APP_ID'. A git pull does not rebuild -- on the box: cd deploy/vps && docker compose up -d --build"
  fi
fi

echo
echo "Discovery surface (how agents find and price this node)"
expect_status GET /.well-known/agent.json 200 "GET /.well-known/agent.json"
expect_status GET /llms.txt 200 "GET /llms.txt"
expect_status GET /mcp.json 200 "GET /mcp.json"
expect_status GET /openapi.json 200 "GET /openapi.json"
expect_status GET /docs 200 "GET /docs"
expect_status GET /robots.txt 200 "GET /robots.txt"
expect_status GET /sitemap.xml 200 "GET /sitemap.xml"

echo
echo "Link-preview assets (a link with no card is a link nobody clicks)"
expect_status GET /favicon.svg 200 "GET /favicon.svg"
expect_status GET /og-image.png 200 "GET /og-image.png"

echo
echo "Paid routes fail closed (402 = route exists AND demands payment)"
for route in /audit /audit/wcag /audit/seo /audit/security /audit/performance; do
  expect_status POST "$route" 402 "POST $route"
done
expect_status POST /audit/bundle 402 "POST /audit/bundle"

echo
echo "402 challenge is machine-actionable"
CHALLENGE=$(curl -sS -m 30 -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
# Headers separately: the v2 challenge is not in the body at all.
CHALLENGE_HEADERS=$(curl -sS -m 30 -D - -o /dev/null -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)

# The manifest's payment.methods is this node's own statement of which rails it
# believes it can settle, and it is read BEFORE the x402 checks below because
# every one of them is a different question depending on the answer. A node with
# x402 deliberately switched off must carry no x402 anywhere; a node with it on
# must carry all of it. Asserting only the "on" half turns a correct, deliberate
# shutdown into two red lines, and a red line that is supposed to be red is how
# a checker teaches people to ignore it -- or worse, how the next session
# "fixes" a rail back on that was turned off on purpose.
MANIFEST=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null)
X402_STATE=$(printf '%s' "$MANIFEST" | python3 -c '
import json, sys
try:
    methods = json.load(sys.stdin)["payment"]["methods"]
except Exception:
    print("unknown"); sys.exit()
print("on" if "x402" in methods else "off")
' 2>/dev/null)
[ -n "$X402_STATE" ] || X402_STATE=unknown
echo "  x402 rail per the manifest: $X402_STATE"

if printf '%s' "$CHALLENGE" | grep -q '"price_usd"'; then
  pass "402 body carries price_usd"
else
  fail "402 body missing price_usd -- agents cannot budget the call"
fi

if printf '%s' "$CHALLENGE" | grep -q '"accepts"'; then
  pass "402 body carries an accepts[] list"
else
  fail "402 body missing accepts[] -- agents cannot pick a payment rail"
fi

# Carrying an accepts[] and carrying a PAYABLE accepts[] are different facts,
# and for as long as this rail has been advertised only the first was true.
# `accepts[]` held a shape of our own invention -- `protocol`/`price`/`pay_to`
# -- missing four fields PaymentRequirementsV1 requires. A client validates
# EVERY entry and raises before signing, so the failure never reached the
# facilitator: nothing rejected, nothing logged, and from this side identical
# to nobody wanting to buy. Every check above stayed green throughout.
#
# Field names rather than the x402 library, because this runs in Cloud Shell
# against the deployed node where the library is not installed. The unit tests
# drive the real client; this catches the shape regressing in production.
PAYABLE=$(printf '%s' "$CHALLENGE" | python3 -c '
import json, sys
state = sys.argv[1]
try:
    body = json.load(sys.stdin)
except Exception:
    print("FAIL|the 402 body is not JSON"); sys.exit()

if state == "unknown":
    print("FAIL|could not read payment.methods from /.well-known/agent.json, so "
          "there is no way to tell whether the x402 rail is meant to be on -- "
          "and every x402 check below asks a different question depending on it")
    sys.exit()

if state == "off":
    # The fail-closed half, asserted rather than assumed. Turning the rail off
    # is a config change on a container that was built to advertise it, so the
    # question "did it actually stop advertising" is exactly as live as "can it
    # be paid", and nothing was asking it.
    accepts = body.get("accepts")
    if accepts is None:
        print("FAIL|402 body has no accepts[] key at all"); sys.exit()
    if accepts:
        print("FAIL|the manifest says x402 is OFF but the 402 still carries %d "
              "accepts[] entr%s -- the node is advertising a rail it does not "
              "claim it can settle" % (len(accepts), "y" if len(accepts) == 1 else "ies"))
        sys.exit()
    print("OK|x402 is OFF and accepts[] is empty -- no unsettleable rail is advertised")
    sys.exit()

if body.get("x402Version") != 1:
    print("FAIL|402 body does not say x402Version:1 -- a v1 client will not read "
          "accepts[] at all"); sys.exit()

required = {"scheme", "network", "maxAmountRequired", "resource",
            "maxTimeoutSeconds", "asset", "payTo"}
accepts = body.get("accepts") or []
if not accepts:
    print("FAIL|accepts[] is empty -- no rail an x402 client can pay"); sys.exit()

for i, entry in enumerate(accepts):
    missing = sorted(required - set(entry))
    if missing:
        print("FAIL|accepts[%d] is missing %s -- a conforming client raises a "
              "ValidationError before signing, so this rail is advertised and "
              "unpayable" % (i, ", ".join(missing)))
        sys.exit()
    if entry.get("protocol") or entry.get("price"):
        print("FAIL|accepts[%d] carries non-spec keys -- a non-x402 rail in "
              "accepts[] fails validation for the whole challenge" % i)
        sys.exit()
print("OK|accepts[] is spec-shaped: %d payable x402 entr%s"
      % (len(accepts), "y" if len(accepts) == 1 else "ies"))
' "$X402_STATE")
case "$PAYABLE" in
  OK*)   pass "$(printf '%s' "$PAYABLE" | cut -d'|' -f2)" ;;
  *)     fail "$(printf '%s' "$PAYABLE" | cut -d'|' -f2)" ;;
esac

# The v2 half. A v2 client reads this header FIRST and only falls back to the
# body, so a node without it serves every modern client the legacy path -- and
# has nowhere to put the v2 extensions slot or the service name the Bazaar
# indexes by.
if printf '%s' "$CHALLENGE_HEADERS" | grep -qi '^payment-required:'; then
  if [ "$X402_STATE" = "off" ]; then
    fail "x402 is off per the manifest, but the 402 still carries a v2 PAYMENT-REQUIRED challenge -- a v2 client reads that header FIRST, so it would still be told to pay"
  else
    pass "402 carries the v2 PAYMENT-REQUIRED challenge header"
  fi
elif [ "$X402_STATE" = "off" ]; then
  pass "x402 is off and no v2 PAYMENT-REQUIRED header is sent"
else
  fail "402 has no PAYMENT-REQUIRED header -- v2 clients fall back to v1, and the service cannot name itself for the Bazaar index"
fi

# A null payTo is worse than no x402 at all: it tells a paying agent to send
# funds nowhere. Absent is correct when x402 is not configured.
if printf '%s' "$CHALLENGE" | grep -q '"payTo": *null'; then
  fail "402 advertises x402 with payTo:null -- agents would pay nowhere"
else
  pass "402 does not advertise an unpayable x402 rail"
fi

# accepts[] and the manifest's payment.methods describe the same fact, and
# they used to disagree: the manifest listed stripe_api_key while accepts[]
# -- the array an agent actually iterates -- omitted it, so a CI pipeline
# holding a pre-funded key could not learn from the challenge that its key
# was spendable here.
if printf '%s' "$MANIFEST" | grep -q 'stripe_api_key'; then
  if printf '%s' "$CHALLENGE" | grep -q '"api_key"'; then
    pass "402 accepts[] offers the API-key rail the manifest advertises"
  else
    fail "manifest lists stripe_api_key but the 402's accepts[] omits it -- an agent reading the challenge cannot find the rail"
  fi
else
  pass "no API-key rail advertised anywhere (consistent: Stripe is not configured)"
fi

# The MPP challenge, checked against whether MPP is meant to be on -- the same
# way the x402 checks above work, and for the same reason.
#
# This asserted the header unconditionally, so it went red the moment MPP was
# deliberately dark: `mpp-stripe` is below Stripe's 0.50 USD SPT floor at these
# prices and `mpp-tempo` has no recipient. Both of those are the fail-closed
# rule working exactly as intended, and a checker that calls the intended state
# a failure is a checker people stop reading.
#
# What actually matters is not "MPP specifically" but "SOME machine rail can
# take money", which the separate check below asserts. Here the question is
# only whether the header agrees with the manifest.
MPP_STATE=$(printf '%s' "$MANIFEST" | python3 -c '
import json, sys
try:
    methods = json.load(sys.stdin)["payment"]["methods"]
except Exception:
    print("unknown"); sys.exit()
print("on" if any(str(m).startswith("mpp-") for m in methods) else "off")
' 2>/dev/null)
[ -n "$MPP_STATE" ] || MPP_STATE=unknown

if curl -sS -m 30 -D - -o /dev/null -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null \
  | grep -qi '^www-authenticate:.*Payment'; then
  if [ "$MPP_STATE" = "off" ]; then
    fail "the manifest advertises no mpp-* method, but the 402 still sends a WWW-Authenticate: Payment challenge -- a caller would be invited to pay a rail this node says it cannot settle"
  else
    pass "MPP WWW-Authenticate: Payment challenge present"
  fi
elif [ "$MPP_STATE" = "off" ]; then
  pass "MPP is off and no WWW-Authenticate: Payment challenge is sent"
else
  fail "the manifest advertises an mpp-* method but the 402 sends no WWW-Authenticate challenge -- MPP's only real channel is that header, so the rail is unusable"
fi

# The question the check above used to be standing in for, asked directly: can
# ANY machine rail take money here? Rails go dark individually for good
# reasons; all of them dark at once is a tollbooth with no coin slot, and
# nothing else in this checker would say so.
LIVE_RAILS=$(printf '%s' "$MANIFEST" | python3 -c '
import json, sys
try:
    methods = json.load(sys.stdin)["payment"]["methods"]
except Exception:
    sys.exit()
machine = [m for m in methods if m != "stripe_api_key"]
print(" ".join(machine))
' 2>/dev/null)

if [ -n "${LIVE_RAILS// /}" ]; then
  pass "a machine payment rail is live: $LIVE_RAILS"
else
  fail "NO machine payment rail is advertised. An agent that arrives cannot pay by any autonomous means -- only the API-key rail is left, and that needs a human checkout first."
fi

echo
echo "MCP endpoint (what the official registry lists as a remote server)"
MCP_INIT=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' 2>/dev/null)
if printf '%s' "$MCP_INIT" | grep -q '"serverInfo"'; then
  pass "MCP initialize handshake responds"
else
  fail "MCP initialize did not respond with serverInfo"
fi
# 2026-07-28 is a modern-only version; returning it here makes every real
# client refuse the connection, so assert we never do.
if printf '%s' "$MCP_INIT" | grep -q '"protocolVersion": *"2026'; then
  fail "MCP returned a non-handshake protocol version -- clients will refuse"
else
  pass "MCP returned a usable handshake protocol version"
fi
MCP_TOOLS=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>/dev/null)
if printf '%s' "$MCP_TOOLS" | grep -q 'audit_bundle'; then
  pass "MCP tools/list advertises the audit tools"
else
  fail "MCP tools/list did not return the audit tools"
fi

# Tool definition quality, checked against the deployed container rather
# than the repo. An agent decides whether to spend money here from the tool
# definition alone: without an outputSchema it cannot tell, before paying,
# whether the response is a shape its pipeline can consume.
if printf '%s' "$MCP_TOOLS" | python3 -c '
import json, sys
try:
    tools = json.load(sys.stdin)["result"]["tools"]
except Exception:
    sys.exit(1)
if not tools:
    sys.exit(1)
for tool in tools:
    if not tool.get("outputSchema") or not tool.get("annotations"):
        sys.exit(1)
    schema = tool.get("inputSchema") or {}
    # Audits take url or html, so their schema must say so. A bee whose
    # inputs are all optional (chain.network) has no constraint to declare.
    if tool["name"].startswith("audit_") and not (
            schema.get("required") or schema.get("anyOf") or schema.get("oneOf")):
        sys.exit(1)
sys.exit(0)
' 2>/dev/null; then
  pass "every MCP tool declares input constraints, an outputSchema and annotations"
else
  fail "MCP tools are under-specified -- agents drop connections rather than guess a contract"
fi

# /mcp.json is what the registry points at; /mcp is what a client calls.
# Two copies of a schema drift, and the one that drifts is the one agents read.
if python3 -c '
import json, sys, urllib.request
base = sys.argv[1]
try:
    with urllib.request.urlopen(base + "/mcp.json", timeout=30) as handle:
        manifest = {t["name"]: t for t in json.load(handle)["tools"]}
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as handle:
        live = {t["name"]: t for t in json.load(handle)["result"]["tools"]}
except Exception:
    sys.exit(1)
if set(manifest) != set(live):
    sys.exit(1)
for name, tool in live.items():
    for field in ("inputSchema", "outputSchema", "annotations", "title"):
        if manifest[name].get(field) != tool.get(field):
            sys.exit(1)
sys.exit(0)
' "$BASE" 2>/dev/null; then
  pass "/mcp.json and /mcp advertise identical tool contracts"
else
  fail "/mcp.json has drifted from /mcp -- the registry points agents at a stale contract"
fi

# The MCP paywall has to be readable by a machine. It used to stringify the
# 402 into the middle of an English sentence, so the price could only be
# recovered by scraping prose -- useless to the agents that would pay it.
MCP_PAY=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"audit_bundle","arguments":{"url":"https://example.com"}}}' 2>/dev/null)
if printf '%s' "$MCP_PAY" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    text = body["result"]["content"][0]["text"]
    challenge = json.loads(text)
except Exception:
    sys.exit(1)
sys.exit(0 if challenge.get("price_usd") == 0.15 else 1)
' 2>/dev/null; then
  pass "MCP paywall parses as JSON and quotes \$0.15 for the bundle"
else
  fail "MCP paywall is not machine-parseable -- an agent cannot read the price"
fi

echo
echo "Human plans: the manifest must only offer what checkout can sell"
PLANS=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null)
if printf '%s' "$PLANS" | grep -q 'included_calls_per_month'; then
  fail "manifest still advertises the retired included-calls plan"
else
  pass "manifest does not advertise the retired plan"
fi
# No f-strings here: the quoting needed to reach into a dict inside one is a
# SyntaxError before Python 3.12, and this script has to run wherever it is
# pasted, not only on the newest interpreter.
printf '%s' "$PLANS" | python3 -c '
import json, sys
try:
    tiers = json.load(sys.stdin)["pricing"]["human_plans"]["tiers"]
except Exception:
    print("  no human_plans block -- the human tiers are retired; per call is the only price")
    sys.exit(0)
if not tiers:
    print("  no tiers offered -- either no Stripe plan Price IDs are set on")
    print("  this node, or STRIPE_SECRET_KEY is not a usable Stripe key.")
    print("  Nothing is for sale to humans here.")
else:
    for t in tiers:
        print("  offers %s: $%s/%s" % (t["id"], t["usd"], t["interval"]))
' 2>/dev/null || echo "  (could not parse pricing block)"

# The three tiers and the stripe_api_key rail all come from the same Stripe
# credential. A node that offers plans but omits the rail (or the reverse) has
# a half-configured Stripe and will fail somewhere a customer can see.
OFFERS_PLANS=$(printf '%s' "$PLANS" | grep -c '"interval"' || true)
OFFERS_RAIL=$(printf '%s' "$PLANS" | grep -c 'stripe_api_key' || true)
if [ "$OFFERS_PLANS" -gt 0 ] && [ "$OFFERS_RAIL" -eq 0 ]; then
  fail "manifest offers human plans but not the stripe_api_key rail -- Stripe is half-configured"
elif [ "$OFFERS_PLANS" -eq 0 ] && [ "$OFFERS_RAIL" -gt 0 ]; then
  fail "manifest advertises the stripe_api_key rail but sells no plan -- Stripe is half-configured"
else
  pass "Stripe plans and the Stripe rail agree with each other"
fi

echo
echo "Machine discovery: is this node findable by agents that would pay it?"
DISC=$(curl -sS -m 30 -X POST "$BASE/audit/bundle" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
if printf '%s' "$DISC" | grep -q '"bazaar"'; then
  # Presence was the whole check, and presence is not indexability. The
  # record shipped for months without `info.input.method` while the schema
  # emitted beside it declared method required, so the Bazaar's own
  # facilitator-side validator rejected every one -- and this line passed
  # every time, including the 34/34 of 2026-08-25. A grep for the word
  # "bazaar" cannot tell a catalogued record from a discarded one.
  #
  # Checked with python rather than the x402 library because this script
  # runs in Cloud Shell against the deployed node, where the library is not
  # installed. `method` is the field that actually broke; the unit tests in
  # tests/test_x402_payments.py run the full validator.
  if printf '%s' "$DISC" | python3 -c '
import json, sys
try:
    info = json.load(sys.stdin)["extensions"]["bazaar"]["info"]["input"]
except Exception:
    sys.exit(1)
# mcp-type records carry no method by design; only body records need one.
sys.exit(0 if info.get("type") != "http" or info.get("method") else 1)
' 2>/dev/null; then
    if [ "$X402_STATE" = "off" ]; then
      fail "x402 is off per the manifest, but the 402 still carries a Bazaar discovery record -- it would advertise this node to facilitators as a payable resource it cannot settle"
    else
      pass "402 carries x402 Bazaar discovery data (indexable by facilitators)"
    fi
  else
    fail "402 carries a Bazaar record that names no HTTP method -- a validating facilitator discards it, so this node is not indexable by capability"
  fi
elif [ "$X402_STATE" = "off" ]; then
  # Counted, not a NOTE. A branch that only prints is a branch that cannot go
  # red, and this file already learned that a silent skip reads as a pass.
  pass "x402 is off and the 402 carries no Bazaar record (nothing to index, correctly)"
  echo "        Agents can only find this node via the MCP registry and the"
  echo "        crawler surfaces while the rail is off, not by capability."
else
  fail "the manifest advertises x402 but the 402 carries no Bazaar record -- a payable node that no facilitator can index by capability"
fi

# /audit is the shortest and most guessable paid path on this service. It is
# an alias of /audit/wcag with no catalog row of its own, which once left it
# as the single paid route absent from capability discovery.
ALIAS=$(curl -sS -m 30 -X POST "$BASE/audit" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
NAMED=$(curl -sS -m 30 -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
if printf '%s\n%s' "$ALIAS" "$NAMED" | python3 -c '
import json, sys
raw = sys.stdin.read().splitlines()
try:
    alias, named = json.loads(raw[0]), json.loads(raw[1])
except Exception:
    sys.exit(1)
sys.exit(0 if alias.get("extensions") == named.get("extensions") else 1)
' 2>/dev/null; then
  pass "/audit advertises the same capability as the route it aliases"
else
  fail "/audit is discoverable differently from /audit/wcag -- a paid path agents cannot find by capability"
fi

# A manifest that describes its inputs only as prose ("string (required)")
# is one a request generator cannot act on.
if printf '%s' "$MANIFEST" | python3 -c '
import json, sys
try:
    endpoints = json.load(sys.stdin)["endpoints"]
except Exception:
    sys.exit(1)
if not endpoints:
    sys.exit(1)
for endpoint in endpoints:
    schema = endpoint.get("input_schema") or {}
    if schema.get("type") != "object" or "properties" not in schema:
        sys.exit(1)
    if (endpoint.get("output_schema") or {}).get("type") != "object":
        sys.exit(1)
sys.exit(0)
' 2>/dev/null; then
  pass "agent.json publishes parseable JSON Schemas for every endpoint"
else
  fail "agent.json describes its endpoints only in prose -- crawlers and request generators cannot use it"
fi

echo
echo "Live payment rails advertised by the manifest"
METHODS=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null \
  | tr ',' '\n' | sed -n '/"methods"/,/]/p' | tr -d ' "' | tr '\n' ' ')
if [ -n "${METHODS// /}" ]; then
  echo "  $METHODS"
else
  echo "  (could not parse; check $BASE/.well-known/agent.json by hand)"
fi

# ---------------------------------------------------------------------------
# The node must not be a proxy into its own network. Every audit fetches the
# caller's URL from inside Cloud Run; a URL naming the metadata endpoint,
# loopback or the VPC has to be refused with a 400 before any payment is read.
# A deployed node that answers anything else here is exploitable for free.
# ---------------------------------------------------------------------------
echo
echo "Target URL gate"
for internal in "http://169.254.169.254/computeMetadata/v1/" "http://127.0.0.1:8080/health" "http://metadata.google.internal/"; do
  # A call with no credential is priced first, whatever its body (#120), so
  # the gate is only reached by a call that carries one. Any key will do: the
  # URL is refused before the key is looked up, and nothing is charged.
  GATE_CODE=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$BASE/audit/wcag" \
    -H 'Content-Type: application/json' -H 'X-API-Key: verify-live-gate-probe' \
    -d "{\"url\":\"$internal\"}" 2>/dev/null)
  if [ "$GATE_CODE" = "400" ]; then
    pass "refuses to fetch $internal -> 400"
  else
    fail "did NOT refuse $internal -> got $GATE_CODE, expected 400. The deployed
        revision predates the target gate, or ALLOW_PRIVATE_TARGETS is set on
        the service -- it must never be. Deploy current main."
  fi
done

echo
echo "The paid path: can a caller who CAN pay actually get an audit?"
# Everything above this point only ever proves that an UNauthenticated call is
# refused with a 402. That is the cheap half of the contract, and on its own it
# is close to worthless: this service returned HTTP 500 to every authenticated
# caller for an unknown length of time -- no Firestore database had ever been
# created, so the API key lookup raised on every keyed request -- while this
# script reported 28/28 passing. The revenue path was dead and nothing said so.
#
# This check costs real money ($0.05), which is why it is opt-in rather than
# always-on. But a skipped check must be loud: silence is exactly what let the
# outage live.
# Resolve a key rather than demanding one. This check used to SKIP on every
# run, because it needed an export that a fresh shell drops -- so the one
# check that answers "can this service take money" was, in practice, never
# run. See scripts/lib-api-key.sh.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-api-key.sh
[ -f "$_LIB_DIR/lib-api-key.sh" ] && . "$_LIB_DIR/lib-api-key.sh"

PAID_KEY=""
if declare -f hv_resolve_api_key >/dev/null 2>&1; then
  hv_resolve_api_key && PAID_KEY="$HV_API_KEY"""
fi

if [ -z "$PAID_KEY" ]; then
  echo "  SKIP  the paid path is NOT verified -- no usable API key."
  echo "        ${HV_KEY_PROBLEM:-no key could be resolved}"
  echo "        This is the check that matters most: everything above only"
  echo "        proves unauthenticated calls are refused. Override with:"
  echo "          HUBVIBE_API_KEY=your_key bash scripts/verify-live.sh"
else
  echo "  using $HV_KEY_SOURCE"
  PAID_BODY=$(mktemp)
  PAID_CODE=$(curl -sS -m 90 -o "$PAID_BODY" -w '%{http_code}' \
    -X POST "$BASE/audit/wcag" \
    -H "X-API-Key: $PAID_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com"}' 2>/dev/null) || PAID_CODE="000"

  case "$PAID_CODE" in
    200)
      # A 200 is necessary but not sufficient: the result has to actually be
      # an audit. A 200 carrying an error object would still be a dead path.
      if grep -q '"pass"' "$PAID_BODY"; then
        pass "authenticated /audit/wcag -> 200 with a real audit result"
      else
        fail "authenticated /audit/wcag -> 200 but the body carries no audit result"
      fi
      ;;
    500)
      fail "authenticated /audit/wcag -> 500. THE PAID PATH IS DEAD. This is a
        server-side fault, not a payment problem -- the service raised an
        unhandled exception. Get the traceback:
          gcloud logging read 'resource.labels.service_name=hubvibe AND severity>=ERROR' --project=resolver-time --freshness=1h --limit=5"
      ;;
    402)
      fail "authenticated /audit/wcag -> 402. The key was not accepted. Either
        it is not a real key, or the key store cannot be reached (check the
        logs for a Firestore error -- a dead key store now degrades to 402
        rather than 500, which is correct but still means no subscriber can
        authenticate)."
      ;;
    502)
      echo "  NOTE  authenticated /audit/wcag -> 502: auth worked, but the audit"
      echo "        itself could not run against example.com. Nothing was billed."
      echo "        The paid path is alive; the target site is the problem."
      ;;
    *)
      fail "authenticated /audit/wcag -> $PAID_CODE (expected 200)"
      ;;
  esac
  rm -f "$PAID_BODY"
fi

echo
echo "-----------------------------------------------"
printf '  %d passed, %d failed  (of %d checks in this checker)\n' \
  "$PASSES" "$FAILURES" "$((PASSES + FAILURES))"
echo "-----------------------------------------------"
# The total is printed because the count is what exposes a stale checkout: a
# run that says 34 when the current checker has 36 is not a pass, it is an old
# script that never asked the two questions that matter.
if [ "${BEHIND:-0}" -gt 0 ]; then
  printf '\n  \033[31mThis was the OLD checker\033[0m (%s commits behind). Whatever it said\n' "$BEHIND"
  printf '  about this node, it did not run the checks added since.\n'
fi
echo

[ "$FAILURES" -eq 0 ] || exit 1
