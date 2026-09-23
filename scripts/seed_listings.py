#!/usr/bin/env python3
"""List the worker network in the Bazaar index.

A facilitator catalogs a paid route only when a payment carrying that route's
discovery record settles through it -- there is no registration endpoint. The
audits are kept listed by refresh-listings.sh; this pays each live /work
route ONCE, with a request body known to complete, through whichever
facilitator the node is on right now.

WHERE THE MONEY GOES: each call pays the route's own price from the payer
wallet to the node's pay-to address, so the USDC lands back in the owner's
wallet. The real cost is provider usage (cents). A job that cannot complete
bills nothing and so is not listed -- rerun and it is retried.

    DRY_RUN=1 python3 scripts/seed_listings.py    # price the run, pay nothing
    python3 scripts/seed_listings.py              # pay, refused above MAX_TOTAL_USD
    ONLY=utility,premium python3 scripts/seed_listings.py

Environment: BASE (default https://hubvibe-io.com); HUBVIBE_WALLET_KEY, or
HUBVIBE_WALLET_FILE (default ~/.hubvibe-wallet-key); MAX_TOTAL_USD (default
25); ONLY (comma list of utility, standard, advanced, premium); DRY_RUN=1.
Needs the x402 client (the box's ~/.hubvibe-venv has it).
"""

import json
import os
import sys
import urllib.request

PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
TEXAS_NAMES = "bigquery-public-data.usa_names.usa_1910_2013"
DAILY_SERIES = "bigquery-public-data.covid19_nyt.us_states"
# A payment never exceeds the dearest price this script expects to meet.
MAX_PER_CALL_USD = 10.00
_ATOMIC_PER_USD = 1_000_000
USER_AGENT = "HubVibe-seed/1.0 (+https://hubvibe-io.com)"

# (tier, route, body). Bodies are the ones proven to complete against the live
# providers; a body that fails validation is refused before payment and lists
# nothing. chain.transaction's hash is filled from the chain at run time.
ROUTES = [
    ("utility", "/work/chain/network", {}),
    ("utility", "/work/chain/address", {"address": PAY_TO}),
    ("utility", "/work/chain/transaction", {"hash": None}),
    ("utility", "/work/market/quote", {"product_id": "BTC-USD"}),
    ("utility", "/work/market/prediction", {"query": "bitcoin", "limit": 5}),
    ("utility", "/work/extract/page", {"url": "https://example.com"}),
    ("standard", "/work/llm/analyze", {
        "text": "HubVibe sells machine-payable site audits at $0.05 per call.",
        "question": "What does HubVibe sell and at what price?"}),
    ("standard", "/work/llm/extract", {
        "text": "HubVibe sells machine-payable site audits at $0.05 per call.",
        "fields": ["product", "price"]}),
    ("standard", "/work/data/query", {
        "sql": ("SELECT name, SUM(number) AS n FROM `%s` WHERE state='TX' "
                "GROUP BY name ORDER BY n DESC LIMIT 3" % TEXAS_NAMES),
        "max_scan_gib": 1}),
    ("advanced", "/work/data/question", {
        "question": "Which three names were most common in Texas overall?",
        "table": TEXAS_NAMES}),
    ("advanced", "/work/research/brief", {
        "url": "https://example.com", "question": "What is this page for?"}),
    ("advanced", "/work/research/page_facts", {
        "url": "https://example.com", "fields": ["purpose", "contact_email"]}),
    ("advanced", "/work/market/intel", {
        "product_id": "BTC-USD", "query": "bitcoin", "limit": 3}),
    # --- wave 1: bees on live Google credentials -----------------------------
    ("utility", "/work/search/web", {"query": "x402 payment protocol"}),
    ("standard", "/work/llm/generate", {"prompt": "In one sentence, what is HubVibe?"}),
    ("standard", "/work/code/execute", {"code": "print(sum(range(10)))"}),
    ("standard", "/work/stats/probability", {
        "points": [[1, 2.1], [2, 3.9], [3, 6.2], [4, 7.8], [5, 10.1]],
        "predict_x": [6], "probability_queries": [{"below": 8}]}),
    ("standard", "/work/image/generate", {
        "prompt": "A beehive built from circuit boards, isometric illustration"}),
    ("standard", "/work/speech/synthesize", {
        "text": "HubVibe sells machine-payable capabilities."}),
    ("standard", "/work/speech/transcribe", {"audio_base64": None}),
    # A real DATE-keyed daily series: AI.FORECAST refuses usa_names' INT64 year.
    ("premium", "/work/data/forecast", {
        "table": DAILY_SERIES, "timestamp_col": "date", "data_col": "confirmed_cases",
        "id_cols": ["state_name"], "horizon": 5}),
    ("premium", "/work/data/anomalies", {
        "history_table": DAILY_SERIES, "target_table": DAILY_SERIES,
        "timestamp_col": "date", "data_col": "confirmed_cases", "id_cols": ["state_name"]}),
    ("advanced", "/work/verify/claims", {
        "claims": ["HubVibe sells machine-payable site audits."],
        "sources": ["https://example.com"]}),
    ("advanced", "/work/research/web", {"question": "What is x402?", "max_sources": 2}),
    ("premium", "/work/research/company", {"company": "Anthropic", "max_sources": 2}),
    ("standard", "/work/monitor/snapshot", {"url": "https://example.com"}),
    ("standard", "/work/monitor/check", {"url": "https://example.com"}),
    ("advanced", "/work/security/mcp_inspect", {"url": "https://hubvibe-io.com/mcp"}),
    # --- wave 2a: keyless bees ported from the (now-removed) /svc catalog ---
    ("utility", "/work/fetch/raw", {"url": "https://example.com"}),
    ("utility", "/work/chain/rpc", {"method": "eth_blockNumber", "params": []}),
    ("utility", "/work/market/rates", {"currency": "USD"}),
    ("utility", "/work/market/ticker", {"product_id": "BTC-USD"}),
    ("utility", "/work/prediction/events", {"limit": 3}),
    ("utility", "/work/prediction/market", {"slug": None}),
    # --- wave 2b: fail-closed until the operator enables one Google product -
    ("utility", "/work/maps/places", {"query": "coffee near the Ferry Building, San Francisco"}),
    ("utility", "/work/maps/route", {"origin": "San Francisco, CA", "destination": "Oakland, CA"}),
    ("utility", "/work/maps/weather", {"location": "San Francisco, CA"}),
    ("premium", "/work/video/generate", {
        "prompt": "A single bee landing on a circuit-board flower, slow motion",
        "duration_seconds": 4}),
]


def select(routes, only):
    """The routes whose tier is in `only` (all when empty)."""
    tiers = {t.strip() for t in (only or "").split(",") if t.strip()}
    return [r for r in routes if not tiers or r[0] in tiers]


def plan_total(priced):
    """Sum of quoted prices for routes that answered a 402, in whole cents."""
    return round(sum(price for _, _, _, price in priced if price is not None), 2)


def _tiny_wav_base64() -> str:
    """A short, genuinely valid PCM WAV (a quarter-second 440Hz tone) so
    speech.transcribe's real paid call has real audio to decode -- silence
    or garbage bytes would just fail Speech-to-Text's own validation and
    list nothing, which is safe but wastes the one paid attempt this script
    budgets per route."""
    import base64
    import math
    import struct

    rate, seconds, amplitude = 8000, 0.15, 3000
    samples = [int(amplitude * math.sin(2 * math.pi * 440 * t / rate))
              for t in range(int(rate * seconds))]
    pcm = b"".join(struct.pack("<h", s) for s in samples)
    byte_rate, block_align = rate * 2, 2
    header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " +
             struct.pack("<IHHIIHH", 16, 1, 1, rate, byte_rate, block_align, 16) +
             b"data" + struct.pack("<I", len(pcm)))
    return base64.b64encode(header + pcm).decode()


def _top_market_slug() -> str:
    """The slug of Polymarket's current highest-volume active market -- a
    slug from three weeks ago is as likely to 404 as to still exist."""
    request = urllib.request.Request(
        "https://gamma-api.polymarket.com/markets"
        "?limit=1&active=true&closed=false&order=volume&ascending=false",
        headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        markets = json.load(response)
    if not markets or not markets[0].get("slug"):
        raise RuntimeError("Polymarket returned no active market to seed prediction.market with")
    return markets[0]["slug"]


def _latest_tx_hash():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                       "params": ["latest", False]}).encode()
    request = urllib.request.Request(
        "https://mainnet.base.org", data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["result"]["transactions"][0]


def _quote(session, base, route, body):
    """(price, reason). The unpaid call must answer 402 with a price."""
    response = session.post(base + route, json=body)
    if response.status_code != 402:
        return None, "HTTP %d (not for sale here)" % response.status_code
    try:
        return float(response.json()["price_usd"]), None
    except Exception:
        return None, "402 without a readable price"


def _wallet_key():
    key = os.environ.get("HUBVIBE_WALLET_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("HUBVIBE_WALLET_FILE") or os.path.expanduser("~/.hubvibe-wallet-key")
    with open(path) as handle:
        return handle.read().strip()


def _pay(session, base, route, body, price, account):
    from x402 import max_amount, x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client

    client = x402ClientSync()
    # x402 >= 2.22 caps every payment at $1.00 on the buyer's side; this is
    # the buyer, and it authorizes exactly the quoted price for this route.
    client.set_spend_controls({"max_amount_per_payment": "$%.2f" % price})
    register_exact_evm_client(client, EthAccountSigner(account),
                              policies=[max_amount(int(round(price * _ATOMIC_PER_USD)))])
    http = x402HTTPClientSync(client)
    url = base + route
    unpaid = session.post(url, json=body)
    if unpaid.status_code != 402:
        return False, "HTTP %d before payment" % unpaid.status_code
    headers, payload = http.handle_402_response(dict(unpaid.headers), unpaid.content, url)
    paid = session.post(url, json=body, headers=headers)
    receipt = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE")
    if paid.status_code == 200 and receipt:
        return True, "settled"
    return False, "HTTP %d: %s" % (paid.status_code, " ".join(paid.text.split())[:160])


def main() -> int:
    import httpx

    base = os.environ.get("BASE", "https://hubvibe-io.com").rstrip("/")
    cap = float(os.environ.get("MAX_TOTAL_USD", "25"))
    dry = os.environ.get("DRY_RUN") == "1"
    chosen = select(ROUTES, os.environ.get("ONLY"))

    with httpx.Client(timeout=240, headers={"User-Agent": USER_AGENT}) as session:
        priced = []
        for tier, route, body in chosen:
            if body.get("hash", "") is None:
                body = {"hash": _latest_tx_hash()}
            if body.get("audio_base64", "") is None:
                body = {"audio_base64": _tiny_wav_base64()}
            if body.get("slug", "") is None:
                body = {"slug": _top_market_slug()}
            price, reason = _quote(session, base, route, body)
            if price is not None and price > MAX_PER_CALL_USD:
                price, reason = None, "quoted $%.2f, above the $%.2f per-call ceiling" % (
                    price, MAX_PER_CALL_USD)
            priced.append((tier, route, body, price))
            print("  %-9s %-28s %s" % (tier, route, "$%.2f" % price if price is not None else "skip: " + reason))

        total = plan_total(priced)
        print("plan: %d routes, $%.2f (cap $%.2f)" % (
            sum(1 for p in priced if p[3] is not None), total, cap))
        if total > cap:
            print("refused: the plan costs more than MAX_TOTAL_USD; nothing was paid")
            return 2
        if dry:
            print("DRY_RUN: nothing was paid")
            return 0

        from eth_account import Account

        account = Account.from_key(_wallet_key())
        print("paying from %s" % account.address)
        failed = 0
        for tier, route, body, price in priced:
            if price is None:
                continue
            try:
                ok, detail = _pay(session, base, route, body, price, account)
            except Exception as exc:
                ok, detail = False, "%s: %s" % (type(exc).__name__, " ".join(str(exc).split())[:160])
            failed += 0 if ok else 1
            print("  %-28s %s" % (route, "listed" if ok else "FAILED " + detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
