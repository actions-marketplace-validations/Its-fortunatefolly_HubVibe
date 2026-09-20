#!/usr/bin/env bash
# Make the first real x402 payment to this node, and see whether it lands in
# the Bazaar index.
#
# WHY THIS EXISTS
#
# The Bazaar spec is explicit about how a resource gets catalogued:
#
#     "When a facilitator receives a PaymentPayload containing the `bazaar`
#      extension, it should: 1. Validate the `info` field against the
#      provided `schema`  2. Extract the discovery information"
#
# That is the ONLY ingestion path. There is no registration endpoint, no
# submit form, no crawler. `/discovery/resources` is read-only -- it lists
# what payments have already taught the facilitator about. The x402 client
# library does its half automatically (client_base._merge_extensions copies
# the server's declared extensions into the payment payload), so the chain is:
#
#     our 402 declares the extension
#       -> a paying client echoes it in the payment payload
#         -> the facilitator validates it and catalogs the resource
#           -> other agents find us in /discovery/resources
#
# Every link after the first requires a payment to actually happen. This node
# has taken zero payments, ever. So it has never been catalogued, and could
# not have been, on ANY facilitator -- swapping facilitators does not fix
# that. An unpaid resource is an uncatalogued resource by construction.
#
# Which is a deadlock: agents find us by capability only if we are indexed,
# and we are indexed only once someone pays. Nobody breaks that from the
# outside. This script breaks it from the inside, for $0.05, by being the
# first payer ourselves.
#
# It does two things nothing else has done:
#   1. Proves the settle side end to end. The handoff has said "settlement is
#      unproven until the first real agent payment" since the rail went live.
#      If settlement is broken, every agent that ever arrives bounces silently
#      and we would read it as no demand -- the single most expensive way this
#      business can be wrong.
#   2. Registers the node in the facilitator's Bazaar index, if that
#      facilitator runs one.
#
# Usage:
#     bash scripts/first-paid-call.sh
#
# The paying wallet is read from HUBVIBE_WALLET_KEY, or from
# ~/.hubvibe-wallet-key if that variable is empty -- an exported variable does
# not survive a Cloud Shell reconnect, and the file does. Have no Base wallet?
#
#     bash scripts/first-paid-call.sh --new-wallet
#
# generates one, saves it mode 600, and prints the address to fund. It needs
# USDC only, NO ETH: x402 signs the transfer off-chain and the facilitator
# pays the gas.
#
# Optional:
#     TARGET_URL   the site to audit         (default https://example.com)
#     ROUTE        which paid route          (default /audit/wcag -- cheapest)
#     FACILITATOR  facilitator base URL      (default https://facilitator.payai.network)
#     BASE         the node under test       (default https://hubvibe-io.com)

set -uo pipefail

BASE="${BASE:-https://hubvibe-io.com}"
ROUTE="${ROUTE:-/audit/wcag}"
TARGET_URL="${TARGET_URL:-https://example.com}"
# Which facilitator sees this payment is the NODE's decision, not this
# script's: in x402 the resource server calls its own facilitator to verify
# and settle, and the payer is never told which one. A Bazaar entry appears
# only where the PaymentPayload actually landed. So reading Dexter's index
# after paying a node configured for xpay.sh answers a question about someone
# else's facilitator, and reports "indexing may lag" when the truth is "your
# payment never went near this index." The box was installed on xpay.sh
# before the defaults moved to Dexter, so this is not hypothetical.
#
# This script runs on the box, so the node's own `.env` is right here and is
# the only authority. Read it. An explicitly exported FACILITATOR still wins
# -- that is someone deliberately asking a different question.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_ENV_FILE="${HUBVIBE_NODE_ENV_FILE:-$SCRIPT_DIR/../deploy/vps/.env}"
NODE_FACILITATOR=""
if [ -r "$NODE_ENV_FILE" ]; then
  NODE_FACILITATOR=$(sed -n 's/^[[:space:]]*X402_FACILITATOR_URL[[:space:]]*=[[:space:]]*//p' \
    "$NODE_ENV_FILE" | tail -n 1 | tr -d '"'"'"'' | tr -d '\r' | sed 's:/*$::')
fi
if [ -n "${FACILITATOR:-}" ]; then
  FACILITATOR_SOURCE="the FACILITATOR variable you exported"
elif [ -n "$NODE_FACILITATOR" ]; then
  FACILITATOR="$NODE_FACILITATOR"
  FACILITATOR_SOURCE="the node's $NODE_ENV_FILE"
else
  FACILITATOR="https://facilitator.payai.network"
  FACILITATOR_SOURCE="the default -- the node's own .env was not readable from here, so which facilitator settles is UNCONFIRMED"
fi

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

# How we recognise ourselves in someone else's index.
#
# The Bazaar record is serialised by the facilitator, not by us, so match it
# the way THEIR serialiser might have written it. An EIP-55 checksummed
# address and a lowercased one are the same address, and `grep` without -i
# calls them different -- so a node that IS listed reads as "not indexed",
# forever, indistinguishably from never having been listed. An index keyed by
# resource URL names our host and no address at all, so accept that too.
# This repo has shipped three green checks that asked whether a field was
# present in OUR shape rather than whether the consumer's shape matched.
index_hits() {
  local host
  host=$(printf '%s' "$BASE" | sed -e 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##' \
                                   -e 's#[/?].*$##' -e 's/:[0-9]*$//')
  printf '%s' "$1" \
    | grep -oiE "$(printf '%s|%s' "$PAY_TO" "$host" | sed 's/\./\\./g')" \
    | wc -l | tr -d ' '
}

# ---------------------------------------------------------------------------
# Preflight. Every check below is here to avoid spending money on a call that
# cannot accomplish what it is being spent for. This is the one payment that
# bootstraps discovery; burning it on a stale revision buys nothing back.
# ---------------------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

# ---------------------------------------------------------------------------
# Resolve the paying wallet.
#
# An exported variable does not survive a Cloud Shell reconnect, and Cloud
# Shell drops idle sessions in minutes. `export HUBVIBE_WALLET_KEY=...` then
# running this some minutes later is the normal way to arrive here with an
# empty variable and no idea why -- the shell looks identical either way.
# So the key also persists in a file, and the file is what makes this
# repeatable instead of a thing that works once.
#
# What this wallet actually needs is narrower than it sounds, and the wrong
# belief here is what stalls people: **USDC only, no ETH.** x402's exact-EVM
# scheme signs an EIP-3009 authorization off-chain -- the client never touches
# an RPC, never broadcasts, and pays no gas (verified against the library:
# its client module has no provider, no send_raw_transaction, one sign call).
# The facilitator submits the transfer and pays the gas. So a wallet holding a
# dollar of USDC on Base and zero ETH is a fully working payer.
# ---------------------------------------------------------------------------

# The paying wallet lives on the box, and so does the money. Run from Google
# Cloud Shell -- a temporary container with a different home directory -- this
# script finds no key, or worse finds a stale file from an old session, and
# reports a wallet problem when the only problem is the machine. The owner hit
# exactly that on 2026-09-08: a five-word leftover in Cloud Shell's home
# stopped the run with "expected a 12- or 24-word recovery phrase", which says
# nothing about being in the wrong terminal. vps-install.sh has refused Cloud
# Shell by name since the owner pasted IT there; this is the same mistake one
# script over. Refuse before reading any wallet, so the diagnosis is the cause
# and not the symptom.
if [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  die "this is Google Cloud Shell -- a temporary terminal, not the node. The paying wallet and its key live on the VPS. Run this on the box (Hostinger: VPS -> Browser terminal; or ssh root@YOUR_VPS_IP), in ~/HubVibe. No payment was attempted."
fi

# ${HOME:-} because set -u makes a bare $HOME fatal in an environment that
# does not set it -- cron, a bare `env -i`, some CI runners. Dying on an
# unbound variable before the wallet message prints is the least useful
# possible failure here.
WALLET_FILE="${HUBVIBE_WALLET_FILE:-${HOME:-/tmp}/.hubvibe-wallet-key}"
export WALLET_FILE

new_wallet() {
  local generated
  generated=$(python3 -c '
from eth_account import Account
a = Account.create()
# Normalise to 0x-prefixed. HexBytes.hex() dropped the prefix in newer
# eth-account, and an unprefixed key works here but is rejected by plenty of
# other tooling the owner may paste it into.
key = a.key.hex()
print("%s\t%s" % (key if key.startswith("0x") else "0x" + key, a.address))
' 2>/dev/null) || die "could not generate a wallet -- is eth-account installed?"
  printf '%s' "${generated%%$'\t'*}" > "$WALLET_FILE"
  chmod 600 "$WALLET_FILE"
  printf '%s' "${generated##*$'\t'}"
}

# Said before a cent moves, because it decides what the last step can mean.
warn "Bazaar index will be read from $FACILITATOR -- from $FACILITATOR_SOURCE"

step "Checking the client dependencies are installed"
if ! python3 -c 'import x402, eth_account, httpx' 2>/dev/null; then
  # A fresh Ubuntu box (the VPS) ships python3 with no pip and no venv
  # module, and refuses system-wide pip installs anyway (PEP 668). So the
  # client lives in its own environment beside the wallet key, and every
  # python3 below resolves to it through PATH. Found on the owner's VPS
  # 2026-09-06: `pip: command not found` at the first paid call.
  VENV="${HUBVIBE_VENV:-${HOME:-/tmp}/.hubvibe-venv}"
  if [ ! -x "$VENV/bin/python3" ]; then
    warn "creating a private Python environment at $VENV"
    if ! python3 -m venv "$VENV" 2>/dev/null; then
      rm -rf "$VENV"
      command -v apt-get >/dev/null 2>&1 \
        || die "python3 cannot create a venv here and there is no apt-get. Install python3-venv and re-run."
      warn "installing python3-venv (apt)"
      { apt-get install -y -q python3-venv >/dev/null 2>&1 \
        || { apt-get update -q >/dev/null 2>&1 && apt-get install -y -q python3-venv >/dev/null 2>&1; }; } \
        || die "apt-get could not install python3-venv (run as root, or install it by hand and re-run)"
      python3 -m venv "$VENV" || die "could not create a Python environment at $VENV"
    fi
  fi
  export PATH="$VENV/bin:$PATH"
  warn "installing the x402 client extras into $VENV"
  python3 -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  python3 -m pip install --quiet "x402[evm,extensions]==2.22.0" eth-account httpx \
    || die "could not install the x402 client extras"
  python3 -c 'import x402, eth_account, httpx' 2>/dev/null \
    || die "the x402 client installed but does not import; read the errors above"
fi
ok "x402 client is importable"

# Explicit and never implicit: a script that quietly mints a wallet when it
# cannot find one would send the owner funding a fresh address every time the
# real key went missing.
if [ "${1:-}" = "--new-wallet" ]; then
  if [ -r "$WALLET_FILE" ] && [ -z "${HUBVIBE_FORCE_NEW_WALLET:-}" ]; then
    # Refusing is right -- overwriting a key that may hold funds destroys them.
    # Refusing SILENTLY is not: the wallet you already have is the answer to
    # the question you just asked, and its address is what you need next. Say
    # it. Stopping without it sends someone hunting for a wallet they own.
    EXISTING=$(python3 -c '
import os, sys
from eth_account import Account
try:
    print(Account.from_key(open(os.environ["WALLET_FILE"]).read().strip()).address)
except Exception as exc:
    print("UNREADABLE\t%s" % exc)
' 2>/dev/null)
    printf '\n  \033[1mYou already have a wallet.\033[0m Not overwriting it.\n\n'
    case "$EXISTING" in
      UNREADABLE*|"")
        printf '  But %s does not contain a readable private key.\n' "$WALLET_FILE"
        printf '  If it holds no funds, replace it:\n\n'
        printf '      HUBVIBE_FORCE_NEW_WALLET=1 bash scripts/first-paid-call.sh --new-wallet\n\n'
        ;;
      *)
        printf '      address: \033[1m%s\033[0m\n\n' "$EXISTING"
        printf '  Send it USDC on Base -- $1 is plenty. This wallet needs NO ETH:\n'
        printf '  it never broadcasts anything. (The send itself is an ordinary\n'
        printf '  transfer out of your own wallet, which pays gas as it always does.)\n'
        printf '  Then just run:  bash scripts/first-paid-call.sh\n\n'
        printf '  (Only if you are certain it holds nothing and want a fresh one:\n'
        printf '   HUBVIBE_FORCE_NEW_WALLET=1 bash scripts/first-paid-call.sh --new-wallet)\n\n'
        ;;
    esac
    exit 1
  fi
  ADDRESS=$(new_wallet)
  printf '\n  \033[1mNew Base wallet created.\033[0m Key saved to %s (mode 600).\n\n' "$WALLET_FILE"
  printf '      address: \033[1m%s\033[0m\n\n' "$ADDRESS"
  printf '  Send it USDC on Base -- $1 is plenty for a $0.05 call. THIS wallet\n'
  printf '  needs NO ETH: x402 signs off-chain and the facilitator pays the gas,\n'
  printf '  so it never broadcasts and never spends gas. (Sending it the dollar\n'
  printf '  is an ordinary transfer out of YOUR wallet, which pays gas as usual --\n'
  printf '  that is the one place on this path where gas is yours to cover.)\n\n'
  printf '  Then re-run:  bash scripts/first-paid-call.sh\n\n'
  exit 0
fi

# Pay from a recovery phrase, IF one exists. The Base app and Coinbase Wallet
# export a 12/24-word phrase rather than a raw key, so the phrase is accepted
# and the account is derived in memory: the key never touches disk. This path
# is here for anyone who has a phrase -- the owner does NOT (affirmed
# 2026-09-07, "there is no twelve words"), and their supported route is the
# key file below. Delete the phrase file once a call has settled.
PHRASE_FILE="${HUBVIBE_WALLET_PHRASE_FILE:-${HOME:-/tmp}/.hubvibe-wallet-phrase}"
PHRASE_KEY=""
if [ -n "${HUBVIBE_WALLET_MNEMONIC:-}" ] || [ -r "$PHRASE_FILE" ]; then
  export PHRASE_FILE
  # One phrase, many accounts: a wallet app shows account 0 by default but the
  # address the owner actually holds funds in may be any of them (the Base app
  # lets you add accounts, and hubvibe.base.eth is not necessarily the first).
  # So when HUBVIBE_EXPECT_ADDRESS names the wallet, the first 10 accounts are
  # derived and the matching one is used; without it, account 0. Failing on
  # "wrong phrase" when the phrase was right and only the index differed would
  # send the owner hunting for a second seed that does not exist.
  DERIVED=$(python3 -c '
import os, sys
from eth_account import Account
Account.enable_unaudited_hdwallet_features()
phrase = os.environ.get("HUBVIBE_WALLET_MNEMONIC") or open(os.environ["PHRASE_FILE"]).read()
phrase = " ".join(phrase.split())
if len(phrase.split()) not in (12, 15, 18, 21, 24):
    # MALFORMED is load-bearing: the caller treats a file that is not a phrase
    # as junk to step over, and anything else as a phrase that failed.
    sys.exit("MALFORMED: expected a 12- or 24-word recovery phrase, got %d words"
             % len(phrase.split()))
q = "\x27"
want = (os.environ.get("HUBVIBE_EXPECT_ADDRESS") or "").strip().lower()
seen = []
for i in range(10 if want else 1):
    acct = Account.from_mnemonic(phrase, account_path="m/44%s/60%s/0%s/0/%d" % (q, q, q, i))
    seen.append(acct.address)
    if not want or acct.address.lower() == want:
        key = acct.key.hex()
        print("%s\t%s\t%d" % (key if key.startswith("0x") else "0x" + key, acct.address, i))
        break
else:
    sys.exit("this phrase does not hold %s in its first 10 accounts. It derives: %s"
             % (os.environ["HUBVIBE_EXPECT_ADDRESS"], ", ".join(seen)))
' 2>&1) || DERIVED="FAILED:$DERIVED"
  case "$DERIVED" in
    # A file that is not a recovery phrase is not a payment instruction, and
    # must not stop a run that has a perfectly good key sitting next to it.
    # The owner hit this from Cloud Shell on 2026-09-08: a five-word leftover
    # in a temporary home directory aborted the whole script with a word
    # count, hiding both the real wallet and the real problem. An explicit
    # HUBVIBE_WALLET_MNEMONIC is different -- that IS an instruction, so a
    # malformed one is an error, not litter.
    FAILED:MALFORMED*)
      if [ -n "${HUBVIBE_WALLET_MNEMONIC:-}" ]; then
        die "HUBVIBE_WALLET_MNEMONIC is not a recovery phrase: ${DERIVED#FAILED:MALFORMED: }"
      fi
      warn "ignoring $PHRASE_FILE -- ${DERIVED#FAILED:MALFORMED: }. Not a recovery phrase, so not a payment instruction. Delete it: rm -f $PHRASE_FILE"
      ;;
    # A well-formed phrase that will not derive the wanted account IS an
    # instruction that failed. Stopping is right; guessing another payer is not.
    FAILED:*)
      die "could not derive a wallet from the recovery phrase: ${DERIVED#FAILED:}"
      ;;
    *)
      PHRASE_KEY="${DERIVED%%$'\t'*}"
      PHRASE_ADDRESS=$(printf '%s' "$DERIVED" | cut -f2)
      PHRASE_INDEX=$(printf '%s' "$DERIVED" | cut -f3)
      ;;
  esac
fi

if [ -n "$PHRASE_KEY" ]; then
  HUBVIBE_WALLET_KEY="$PHRASE_KEY"
  export HUBVIBE_WALLET_KEY
  ok "paying from your own wallet (recovery phrase, account $PHRASE_INDEX): $PHRASE_ADDRESS"
  warn "delete the phrase once this settles:  rm -f $PHRASE_FILE"
elif [ -n "${HUBVIBE_WALLET_KEY:-}" ]; then
  ok "wallet key from HUBVIBE_WALLET_KEY"
elif [ -r "$WALLET_FILE" ]; then
  HUBVIBE_WALLET_KEY=$(cat "$WALLET_FILE")
  export HUBVIBE_WALLET_KEY
  ok "wallet key from $WALLET_FILE"
else
  printf '\n  \033[31mSTOP\033[0m  No paying wallet.\n\n'
  printf '  HUBVIBE_WALLET_KEY is empty and %s does not exist.\n\n' "$WALLET_FILE"
  printf '  If you DID export it: the export was lost. Cloud Shell drops env on\n'
  printf '  reconnect, and an idle tab reconnects silently. Re-export and re-run\n'
  printf '  in the SAME shell, or better, save it once so this stops recurring:\n\n'
  printf '      printf %%s "0xYOUR_KEY" > %s && chmod 600 %s\n\n' "$WALLET_FILE" "$WALLET_FILE"
  printf '  If you do NOT have a Base wallet, make one here -- it needs USDC only,\n'
  printf '  no ETH, because the facilitator pays the gas:\n\n'
  printf '      bash scripts/first-paid-call.sh --new-wallet\n\n'
  exit 1
fi

step "Reading the live 402 challenge from $BASE$ROUTE"
# min-instances is 0 (#85), so the first request after an idle spell cold-
# starts a browser-sized container. That can run past 30 seconds, and while
# it boots Cloud Run answers with its own HTML error page -- which this step
# used to report as "the response was not JSON -- is the node up?" with the
# status and the body thrown away (2026-09-04). This read is free, so it gets
# a longer timeout and two retries on a 5xx or a timeout. The PAYMENT below
# is never retried; that rule is unchanged.
RETRY_SLEEP="${HUBVIBE_RETRY_SLEEP:-10}"
CHALLENGE=""
STATUS="000"
for attempt in 1 2 3; do
  RAW=$(curl -sS -m 120 -w '\n%{http_code}' -X POST "$BASE$ROUTE" \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"$TARGET_URL\"}" 2>/dev/null)
  STATUS="${RAW##*$'\n'}"
  CHALLENGE="${RAW%$'\n'*}"
  case "$STATUS" in
    000|5??)
      if [ "$attempt" -lt 3 ]; then
        warn "HTTP $STATUS from the node (attempt $attempt of 3 -- a cold start?); retrying in ${RETRY_SLEEP}s"
        sleep "$RETRY_SLEEP"
        continue
      fi
      ;;
  esac
  break
done
[ -n "$CHALLENGE" ] || die "no response body from $BASE$ROUTE (HTTP $STATUS after 3 attempts).
      Is the service up?  gcloud run services describe hubvibe --project=resolver-time --region=us-south1 --format='value(status.url,status.conditions[0].message)'"
export STATUS

# One python pass over the challenge: it has to answer four questions, and
# reading it four times invites the four answers to disagree.
PREFLIGHT=$(printf '%s' "$CHALLENGE" | python3 -c '
import json, sys
import os

raw = sys.stdin.read()
try:
    body = json.loads(raw)
except Exception:
    # Show what came back. "not JSON" alone sent the owner guessing whether
    # the node was up; the status and the first line of the body say.
    excerpt = " ".join(raw.split())[:200]
    status = os.environ.get("STATUS", "?")
    where = ("Cloud Run answered for the service, so the container did not "
             "answer in time: still cold-starting, or the latest revision "
             "failed to start. Check: gcloud run services describe hubvibe "
             "--project=resolver-time --region=us-south1"
             if status.startswith("5") else "is the node up?")
    print("FAIL\tthe response was not JSON (HTTP %s). It said: %r -- %s"
          % (status, excerpt, where))
    sys.exit()

# accepts[] is the x402 spec array as of #61: spec-shaped entries only, no
# `protocol` key, and payTo not pay_to. Reading the pre-#61 names here found
# nothing and reported "does not advertise x402" about a node that does.
if body.get("x402Version") != 1:
    print("FAIL\tthe 402 body does not say x402Version:1, so no v1 client will "
          "read accepts[] at all. The deployed revision predates #61 -- run "
          "scripts/repair-and-deploy.sh first.")
    sys.exit()

accepts = body.get("accepts") or []
entry = next((a for a in accepts if a.get("scheme") == "exact" and a.get("payTo")), None)
if entry is None:
    print("FAIL\tno payable x402 entry in accepts[]. Present: %s"
          % (json.dumps(accepts)[:200] or "nothing"))
    sys.exit()

missing = sorted({"maxAmountRequired", "asset", "maxTimeoutSeconds", "network"} - set(entry))
if missing:
    print("FAIL\taccepts[0] is missing %s -- a conforming client raises before "
          "signing, so this rail cannot be paid. Deploy #61." % ", ".join(missing))
    sys.exit()

pay_to = entry["payTo"]
if set(pay_to[2:]) == {"0"}:
    print("FAIL\tpayTo is the zero address -- USDC reverts transfers to it. "
          "Nothing would arrive.")
    sys.exit()

info = (((body.get("extensions") or {}).get("bazaar") or {}).get("info") or {}).get("input")
if not info:
    print("FAIL\tthe 402 carries no Bazaar discovery record, so this payment would "
          "settle but index nothing. Deploy first.")
    sys.exit()
if info.get("type") == "http" and not info.get("method"):
    print("FAIL\tthe Bazaar record names no HTTP method, so the facilitator will "
          "discard it on validation and the payment buys no index entry. The "
          "deployed revision predates #52 -- run scripts/repair-and-deploy.sh first.")
    sys.exit()

print("OK\t%s\t%s\t%s\t%s\t%s" % (body.get("price"), pay_to, entry["network"],
                                    entry["asset"], entry["maxAmountRequired"]))
')

case "$PREFLIGHT" in
  FAIL*) die "$(printf '%s' "$PREFLIGHT" | cut -f2-)" ;;
esac

PRICE=$(printf '%s' "$PREFLIGHT" | cut -f2)
PAY_TO=$(printf '%s' "$PREFLIGHT" | cut -f3)
NETWORK=$(printf '%s' "$PREFLIGHT" | cut -f4)
ASSET=$(printf '%s' "$PREFLIGHT" | cut -f5)
AMOUNT=$(printf '%s' "$PREFLIGHT" | cut -f6)
ok "x402 advertised: $PRICE to $PAY_TO on $NETWORK"
ok "the Bazaar record on this 402 is well-formed and will survive validation"

# ---------------------------------------------------------------------------
# The paying wallet must not BE the recipient.
#
# It is an easy mistake and nothing else catches it: the owner's Base wallet
# is the natural thing to reach for, and it is also X402_PAY_TO_ADDRESS. The
# x402 client raises no objection -- verified by running this script against a
# node whose payTo was the payer's own address; a signature was produced
# normally.
#
# So the failure would land at the facilitator, on the one call whose entire
# purpose is to prove the facilitator settles. `exact` has the payer sign an
# EIP-3009 transferWithAuthorization from -> to; with from == to that is a
# degenerate self-transfer nothing here has ever tested. Whatever came back,
# it would say nothing about whether a real buyer can pay -- the question this
# call exists to answer -- while consuming the bootstrap attempt.
#
# Overridable, because a self-transfer may well be valid and refusing to let
# the owner try it is not this script's call to make.
# ---------------------------------------------------------------------------
PAYER=$(python3 -c '
import os
from eth_account import Account
try:
    print(Account.from_key(os.environ["HUBVIBE_WALLET_KEY"].strip()).address)
except Exception:
    print("")
' 2>/dev/null)

if [ -n "$PAYER" ] && [ "$(printf '%s' "$PAYER" | tr 'A-Z' 'a-z')" = \
                        "$(printf '%s' "$PAY_TO" | tr 'A-Z' 'a-z')" ]; then
  if [ -z "${HUBVIBE_ALLOW_SELF_PAYMENT:-}" ]; then
    printf '\n  \033[31mSTOP\033[0m  The paying wallet IS the recipient.\n\n'
    printf '      paying:    %s\n' "$PAYER"
    printf '      paying to: %s\n\n' "$PAY_TO"
    printf '  x402 would sign this without complaining, so nothing before the\n'
    printf '  facilitator stops it -- but it is a self-transfer, and this call\n'
    printf '  exists to prove the facilitator settles a REAL payment. Whatever\n'
    printf '  came back would not answer that.\n\n'
    printf '  Pay from a different wallet. The money still lands in yours:\n\n'
    printf '      bash scripts/first-paid-call.sh --new-wallet\n\n'
    printf '  then send that address ~$1 of USDC on Base and re-run.\n\n'
    printf '  (To try the self-payment anyway: HUBVIBE_ALLOW_SELF_PAYMENT=1)\n\n'
    exit 1
  fi
  warn "paying wallet IS the recipient -- self-transfer, allowed by override"
fi

# ---------------------------------------------------------------------------
# Does the wallet actually hold the asset this challenge asks for?
#
# Without this, an unfunded or wrongly-funded wallet fails deep inside the
# facilitator, and what comes back is a settlement error that reads like the
# rail is broken. It is not: it is an empty wallet, or USDC sent on Ethereum
# instead of Base, or the funds sitting at a different address than the key
# signs for. Those are minutes to fix and hours to diagnose from a 402.
#
# Read straight off a public Base RPC -- balanceOf(address) is selector
# 0x70a08231 -- so this asks the chain rather than trusting anything local.
# ---------------------------------------------------------------------------

step "Checking the paying wallet holds enough USDC on Base"
BAL=$(python3 -c '
import json, os, sys, urllib.request
from eth_account import Account

asset = sys.argv[1]
need  = int(sys.argv[2])
try:
    addr = Account.from_key(os.environ["HUBVIBE_WALLET_KEY"].strip()).address
except Exception as exc:
    print("FAIL|the wallet key is not a valid EVM private key (%s). It must be "
          "0x + 64 hex characters." % type(exc).__name__)
    sys.exit()

body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[
    {"to": asset, "data": "0x70a08231" + addr[2:].rjust(64, "0").lower()}, "latest"]}).encode()
try:
    req = urllib.request.Request(os.environ.get("BASE_RPC", "https://mainnet.base.org"),
                                 data=body, headers={"Content-Type": "application/json", "User-Agent": "hubvibe-first-paid-call/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        result = json.load(r).get("result")
    balance = int(result, 16) if result and result != "0x" else 0
except Exception as exc:
    # Not fatal. A balance we could not read is not a balance we know to be
    # wrong, and refusing to try on an RPC hiccup would be its own dead end.
    print("SKIP|%s|could not reach the Base RPC (%s); proceeding without the check"
          % (addr, type(exc).__name__))
    sys.exit()

verdict = "OK" if balance >= need else "FAIL"
print("%s|%s|%.6f" % (verdict, addr, balance / 1_000_000))
' "$ASSET" "$AMOUNT" 2>/dev/null)

case "$BAL" in
  OK*)
    ok "$(printf '%s' "$BAL" | cut -d'|' -f2) holds \$$(printf '%s' "$BAL" | cut -d'|' -f3) USDC"
    ;;
  SKIP*)
    # The RPC has failed from Cloud Shell on every run so far (HTTPError from
    # mainnet.base.org), which left "is the wallet funded?" as the one open
    # question after two rejected attempts. When this script cannot answer
    # it, hand the human the page that can -- one tap on a phone, no gcloud.
    SKIP_ADDR=$(printf '%s' "$BAL" | cut -d'|' -f2)
    warn "$(printf '%s' "$BAL" | cut -d'|' -f3)"
    warn "check the balance yourself before reading a rejection as anything else:"
    warn "  https://basescan.org/address/$SKIP_ADDR"
    ;;
  FAIL\|0x*)
    WALLET_ADDR=$(printf '%s' "$BAL" | cut -d'|' -f2)
    HAVE=$(printf '%s' "$BAL" | cut -d'|' -f3)
    printf '  \033[31mSTOP\033[0m  the paying wallet is short.\n\n'
    printf '        address: \033[1m%s\033[0m\n' "$WALLET_ADDR"
    printf '        holds:   $%s USDC on Base\n' "$HAVE"
    printf '        needs:   $%s\n\n' "$(python3 -c "print('%.2f' % ($AMOUNT/1000000))")"
    printf '  Send USDC to that address ON BASE. No ETH is required -- x402 signs\n'
    printf '  off-chain and the facilitator pays the gas. USDC sent on Ethereum\n'
    printf '  mainnet or another chain will not show up here.\n\n'
    exit 1
    ;;
  *)
    die "$(printf '%s' "$BAL" | cut -d'|' -f2-)"
    ;;
esac

# ---------------------------------------------------------------------------
# Baseline the index BEFORE paying, so "we appeared" is a measured change
# rather than an assumption. A facilitator with no index answers 404 here;
# that is a real answer and the script keeps going -- proving settlement is
# worth the $0.05 on its own.
# ---------------------------------------------------------------------------

step "Baselining the facilitator's Bazaar index"
BEFORE=$(curl -sS -m 30 "$FACILITATOR/discovery/resources" 2>/dev/null)
INDEX_LIVE=yes
if printf '%s' "$BEFORE" | grep -qi 'not found'; then
  INDEX_LIVE=no
  warn "$FACILITATOR serves no /discovery/resources -- it settles payments and"
  warn "runs no index. This payment will prove settlement but cannot register"
  warn "the node anywhere. To get indexed, settle through a facilitator that"
  warn "runs a Bazaar:"
  warn "  cd $SCRIPT_DIR/.. && bash scripts/switch-facilitator.sh https://facilitator.payai.network"
  warn "then run this script once more."
else
  BEFORE_COUNT=$(index_hits "$BEFORE")
  ok "index reachable; entries already naming this node: $BEFORE_COUNT"
fi

# ---------------------------------------------------------------------------
# The payment. Exactly one attempt -- no retry loop anywhere near a signature.
# A retried payment is a double charge, and this script exists to establish
# trust in the rail, not to spend twice proving it.
# ---------------------------------------------------------------------------

step "Paying for one real call ($PRICE)"
PAID=$(HUBVIBE_BASE_URL="$BASE" \
       HUBVIBE_MAX_PRICE_USD=0.15 \
       HUBVIBE_BUDGET_USD=0.15 \
       ROUTE="$ROUTE" \
       TARGET_URL="$TARGET_URL" \
       python3 -c '
import json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "wcag-audit-engine", "integrations"))
from hubvibe_tollbooth import HubVibeTollbooth

route = os.environ["ROUTE"].rsplit("/", 1)[-1]
booth = HubVibeTollbooth.from_env()
try:
    result = booth.audit(os.environ["TARGET_URL"], endpoint=route)
except Exception as exc:
    # Capped: the client raises with the whole 402 body attached, schema and
    # all, and a screenful of JSON buries the one line that says why.
    detail = " ".join(str(exc).split())
    if len(detail) > 300:
        detail = detail[:300] + " ...[truncated]"
    print("FAIL\t%s: %s" % (type(exc).__name__, detail))
    sys.exit()
# The tx hash rides ahead of the result: it is the one field a human needs
# next, and json.dumps never emits a raw tab, so the columns stay stable.
receipt = booth.last_settlement or {}
print("OK\t%.4f\t%s\t%s" % (booth.spent_usd, receipt.get("transaction") or "",
                            json.dumps(result)[:400]))
' 2>&1)

case "$PAID" in
  FAIL*)
    printf '  \033[31mSTOP\033[0m  the payment did not go through:\n'
    printf '%s\n' "$PAID" | cut -f2- | sed 's/^/        /'
    # An empty wallet is not a broken rail, and saying so is not a nicety:
    # the generic wording below told the owner to "fix this before spending
    # any effort on demand" when the facilitator had merely reported that a
    # freshly created wallet holds no USDC (2026-09-07). Telling someone
    # their payment rail is broken when it is working is the same class of
    # error as the reverse -- it is a checker lying about the thing it
    # exists to report.
    case "$PAID" in
      *insufficient_balance*|*insufficient_funds*)
        printf '\n  \033[1mThe rail is fine. The paying wallet is empty.\033[0m The facilitator\n'
        printf '  checked the signature, found it valid, and refused only for want of\n'
        printf '  funds -- which means everything up to the money is proven working.\n\n'
        if [ -n "${PAYER:-}" ]; then
          printf '  Send USDC on Base (\$1 is plenty; NO ETH needed) to:\n\n'
          printf '      \033[1m%s\033[0m\n\n' "$PAYER"
        fi
        printf '  Then run this same command again.\n'
        ;;
      *)
        printf '\n  This is the answer worth having. Settlement was never proven\n'
        printf '  before now, and an agent hitting this would have bounced in\n'
        printf '  silence -- which reads as nobody buying. Fix this before\n'
        printf '  spending any effort on demand.\n'
        ;;
    esac
    exit 1
    ;;
esac

SPENT=$(printf '%s' "$PAID" | cut -f2)
TX=$(printf '%s' "$PAID" | cut -f3)
RESULT=$(printf '%s' "$PAID" | cut -f4-)

# A 200 with an audit in it is NOT the same as having been charged for it.
#
# The node finishes and delivers an audit whose settlement failed -- on
# purpose, as the lesser evil versus charging for undelivered work -- and
# admits it in `billing_warning` on the body it returns. Reading only the
# HTTP status and announcing "settled" converts that admission into a
# success report. On 2026-09-08 this script printed `settled $0.05 and the
# audit returned a result` directly above a body reading "payment settlement
# failed after the audit ran; this call was not charged", and the owner was
# told revenue had started when no money had moved at all.
#
# The body is the authority on whether we were paid. Read it first.
case "$RESULT" in
  *"settlement failed after the audit ran"*)
    printf '  \033[31mSTOP\033[0m  the audit ran and was delivered, but the node was NOT paid:\n'
    printf '%s\n' "$RESULT" | sed 's/^/        /'
    printf '\n  \033[1mVerify passed; the settle did not complete.\033[0m The signature,\n'
    printf '  the rail, the route and the audit all work. Nothing moved after the\n'
    printf '  work was delivered, so nothing can be indexed either. The node\n'
    printf '  logged WHICH of three ways it failed, and they are different\n'
    printf '  problems with different fixes:\n\n'
    # `x402 settle` is the only token common to all three failure lines --
    # REFUSED (the facilitator said no), TIMED OUT (it never answered), and
    # FAILED before the facilitator could answer (an exception on this side,
    # logged through _log_rejection). Grep the wording of one and the other
    # two return nothing, and empty output reads as "no failure was logged"
    # rather than "wrong question asked". That happened on 2026-09-08: the
    # owner was handed `grep "settle REFUSED"` for a run whose body said
    # settlement FAILED, got silence, and the box looked broken.
    # test_the_settle_diagnostic_matches_every_failure_log pins this string
    # against x402_payments.py so the two cannot drift.
    printf '    cd %s/../deploy/vps && docker compose logs --since 3h 2>&1 | grep -i "x402 settle" | tail -20\n\n' "$SCRIPT_DIR"
    printf '  REFUSED  the facilitator declined the transfer.\n'
    printf '  FAILED   the call never got an answer out of it -- our side.\n'
    printf '  TIMED OUT  unknown; the money may yet move. Do NOT re-run on that\n'
    printf '           one until you have checked the wallet.\n\n'
    printf '  If compose finds no containers, ask docker directly:\n\n'
    printf '    docker logs --since 3h $(docker ps -qf name=hubvibe | head -1) 2>&1 | grep -i x402 | tail -30\n\n'
    printf '  On REFUSED or FAILED the wallet still holds the money; nothing is\n'
    printf '  lost and re-running after the fix is safe.\n'
    exit 1
    ;;
  *"settlement is pending on-chain"*)
    warn "settled, but the transfer is not confirmed yet -- this call IS charged."
    warn "the receipt header carries the hash; do not pay again."
    ;;
  *"settlement status is unknown"*)
    warn "the facilitator did not answer the settle in time. The transfer may"
    warn "still complete on-chain -- do NOT re-run this until you know, or you"
    warn "may pay twice for one call. Check the wallet first."
    ;;
  *)
    ok "settled \$$SPENT and the audit returned a result"
    ;;
esac
printf '%s\n' "$RESULT" | sed 's/^/        /'

# The receipt is the proof. A settled payment has a transaction hash, and
# the node hands it back in the PAYMENT-RESPONSE header (x402 spec step 10).
# Print the explorer link for it, so "did the money move" is one tap and not
# a wallet-app hunt.
if [ -n "$TX" ]; then
  ok "on-chain: https://basescan.org/tx/$TX"
else
  # There are two reasons for no header, and they are not close: a settle
  # that never happened has no hash to report, and blaming the deployed
  # revision for that sends the reader to rebuild a node that is fine. Only
  # say "old revision" once the body has confirmed we WERE paid.
  warn "no PAYMENT-RESPONSE receipt came back. If the body above reports no"
  warn "billing problem, the deployed revision predates the receipt header"
  warn "(rebuild: cd deploy/vps && docker compose up -d --build)."
  warn "look for the transfer at https://basescan.org/address/$PAY_TO"
fi

# ---------------------------------------------------------------------------
# Did the payment register us?
# ---------------------------------------------------------------------------

step "Re-reading the Bazaar index"
if [ "$INDEX_LIVE" = no ]; then
  warn "skipped -- this facilitator runs no index"
  printf '\n  \033[1mFIRST PAID CALL: SETTLED.\033[0m Revenue is no longer zero and the\n'
  printf '  settle path is proven. Capability discovery still needs a\n'
  printf '  facilitator that runs a Bazaar; one payment through such a\n'
  printf '  facilitator is all it then takes to get listed.\n'
  exit 0
fi

sleep 5
AFTER=$(curl -sS -m 30 "$FACILITATOR/discovery/resources" 2>/dev/null)
AFTER_COUNT=$(index_hits "$AFTER")

if [ "$AFTER_COUNT" -gt "${BEFORE_COUNT:-0}" ]; then
  printf '\n  \033[1;32mINDEXED.\033[0m %s entries now name this node (was %s).\n' \
    "$AFTER_COUNT" "${BEFORE_COUNT:-0}"
  printf '  An agent shopping the Bazaar by capability can now find this node.\n'
elif [ -n "$NODE_FACILITATOR" ] && [ "$FACILITATOR" = "$NODE_FACILITATOR" ]; then
  # This index belongs to the facilitator that actually settled, so a missing
  # entry really can be lag, and waiting is a reasonable thing to do.
  warn "no new entry yet (still $AFTER_COUNT). This IS the facilitator the node"
  warn "settles through, so indexing may simply lag; re-check with:"
  warn "  curl -s $FACILITATOR/discovery/resources | grep -ci $PAY_TO"
else
  # It is not. Telling someone to wait for an index their payment never
  # reached costs them the days it takes to stop believing it.
  warn "no new entry (still $AFTER_COUNT) -- and this index may not be the one"
  warn "that could ever have it. The payment settled through whichever"
  warn "facilitator the NODE is configured for; this index was read from"
  warn "$FACILITATOR_SOURCE. Confirm which one settled:"
  warn "  grep X402_FACILITATOR_URL $NODE_ENV_FILE"
  warn "If that is not $FACILITATOR, waiting will never help. Point the node at"
  warn "a facilitator that runs a Bazaar and pay once more:"
  warn "  cd $SCRIPT_DIR/.. && bash scripts/switch-facilitator.sh https://facilitator.payai.network"
fi
