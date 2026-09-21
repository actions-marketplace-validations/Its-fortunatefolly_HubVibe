#!/usr/bin/env python3
"""Prove every /work bee end to end: real provider, real payment gate, real
ledger, real discovery -- everything short of USDC moving.

WHY THIS EXISTS

scripts/simulate-paid-call.py proves the AUDIT routes against a stub
facilitator. Nothing did the same for /work: the worker tests mock their
providers, so a bee could pass every test and still 404 on a retired model
(which is exactly what happened to the Imagen and gemini-2.5 bees). This
boots the real node, points it at the same stub facilitator (real EIP-712
signature checks, real Bazaar validation), and for every worker in the
catalog:

  * discovery -- a live bee is listed in /work, /.well-known/agent.json and
    openapi.json (with x-payment-info); an unavailable one is listed in none
    of them and answers 503 instead of quoting a price. (Bees are not MCP
    tools: /mcp sells the audits only, and its paywall is not touched here.)
  * 402       -- the unpaid call is challenged at exactly the catalog price
  * execution -- the call is paid with the real x402 client and the REAL
    provider runs (Vertex, BigQuery, Speech, Base RPC, Coinbase, ...)
  * payment   -- /verify before the work, /settle after it, once each, for
    the catalog amount; the 200 carries the PAYMENT-RESPONSE receipt; the
    Bazaar record rides the payment and passes the library's validator
  * ledger    -- the call is in worker_calls as ok + settled, with its
    provider attempts and cost in worker_provider_calls

Plus the refusal paths: invalid input is a 400 that never settles.

Real provider calls cost real (small) money on the Google project -- a full
run is well under $1, most of it the 4-second video. No USDC moves: the
facilitator is the local stub and the recipient is a throwaway address.

Usage (Cloud Shell or the box; markers unset so the node does not refuse):
    env -u CLOUD_SHELL -u DEVSHELL_PROJECT_ID python3 scripts/simulate-work-calls.py
    ... --only /work/llm/generate,/work/data/query     (a subset)
Exit 0 = every AVAILABLE bee passed every check. Writes a JSON report to
.sim-work-calls/report.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sim = _load("simulate_paid_call", "simulate-paid-call.py")
seed = _load("seed_listings", "seed_listings.py")

API_KEY = "simulate-work-calls"


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def _client(payer, max_usd: float):
    from x402 import max_amount, x402ClientSync
    from x402.http import x402HTTPClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client

    core = x402ClientSync()
    # x402 >= 2.22 caps every payment at $1 by default; every bee above $1
    # needs the buyer to raise it, exactly as a real agent buying them must.
    # Here the cap is the dearest price in the catalog, and nothing above it.
    if hasattr(core, "set_spend_controls"):
        core.set_spend_controls({"max_amount_per_payment": f"${max_usd:.2f}"})
    register_exact_evm_client(core, EthAccountSigner(payer),
                              policies=[max_amount(round(max_usd * 1_000_000))])
    return x402HTTPClientSync(core)


def _bodies() -> dict:
    """The seeding script's bodies -- the ones chosen to complete against the
    live providers -- with its run-time values filled the same way."""
    bodies = {}
    for _, path, body in seed.ROUTES:
        body = dict(body)
        if "hash" in body and body["hash"] is None:
            body["hash"] = seed._latest_tx_hash()
        if "slug" in body and body["slug"] is None:
            body["slug"] = seed._top_market_slug()
        if "audio_base64" in body and body["audio_base64"] is None:
            body["audio_base64"] = seed._tiny_wav_base64()
        bodies[path] = body
    return bodies


def _canonical_hash(value) -> str:
    """Recomputed here, independently of the node, from the delivered body:
    the same recipe the receipt states (sha256 over canonical JSON)."""
    import hashlib
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _excerpt(value, limit=160) -> str:
    text = json.dumps(value, default=str)
    for key in ("image_base64", "audio_base64", "video_base64"):
        if isinstance(value, dict) and isinstance(value.get(key), str):
            text = json.dumps({**value, key: f"<{len(value[key])} b64 chars>"}, default=str)
    return text[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default="", help="comma-separated /work paths")
    args = parser.parse_args()
    only = {p.strip() for p in args.only.split(",") if p.strip()}

    import httpx
    from eth_account import Account
    from x402.http.utils import decode_payment_response_header

    checks = sim.Checks()
    work = Path(os.environ.get("SIM_WORKDIR") or (REPO / ".sim-work-calls"))
    work.mkdir(exist_ok=True)
    ledger_path = work / "workers.db"
    for stale in (ledger_path, work / "node.log"):
        if stale.exists():
            stale.unlink()

    payer = Account.create()
    recipient = Account.create().address
    fac_port, node_port = sim._free_port(), sim._free_port()
    facilitator = f"http://127.0.0.1:{fac_port}"
    base = f"http://127.0.0.1:{node_port}"

    sim.step("Stub facilitator + node (real providers, throwaway recipient)")
    state = sim.start_facilitator(fac_port, recipient, index=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("STRIPE_", "MPP_", "X402_", "FIRESTORE"))}
    env.update({
        "X402_FACILITATOR_URL": facilitator,
        "X402_PAY_TO_ADDRESS": recipient,
        "PUBLIC_BASE_URL": base,
        "AUDIT_API_KEY": API_KEY,
        "WORKER_LEDGER_PATH": str(ledger_path),
        "PYTHONUNBUFFERED": "1",
    })
    env.setdefault("WORKER_GCP_PROJECT", "resolver-time")
    node = sim.start_node(node_port, env, work / "node.log")
    print(f"  node {base}  facilitator {facilitator}  ledger {ledger_path}")

    report = {"started": time.time(), "workers": []}
    try:
        index = _get(f"{base}/work")
        live = {w["path"]: w for w in index["workers"]}
        unavailable = {u["name"]: u["reason"] for u in index["unavailable"]}
        agent = json.dumps(_get(f"{base}/.well-known/agent.json"))
        openapi_paths = _get(f"{base}/openapi.json").get("paths") or {}
        priced_paths = {p for p, ops in openapi_paths.items()
                        if "x-payment-info" in json.dumps(ops)}
        print(f"  {len(live)} live, {len(unavailable)} unavailable")

        W = _load_workers_catalog()
        bodies = _bodies()
        missing = [w.path for w in W.CATALOG if w.path not in bodies]
        checks.expect(not missing, f"every catalog worker has a proof body ({missing or 'all'})")

        http_client = _client(payer, max(w.price_usd for w in W.CATALOG))
        synth_audio = None
        for worker in W.CATALOG:
            if only and worker.path not in only:
                continue
            row = {"worker": worker.name, "path": worker.path, "price_usd": worker.price_usd,
                   "tier": worker.tier, "composes": list(worker.composes)}
            report["workers"].append(row)
            sim.step(f"{worker.name}  ${worker.price_usd:.2f}")

            if worker.path not in live:
                row.update(live=False, reason=unavailable.get(worker.name, "?"))
                status, _, _ = sim._post(f"{base}{worker.path}", bodies.get(worker.path, {}))
                checks.expect(status == 503, f"unavailable -> 503, no price quoted (HTTP {status})")
                checks.expect(worker.path not in priced_paths and worker.path not in agent,
                              "unavailable -> no price advertised in openapi.json or agent.json")
                print(f"  UNAVAILABLE: {row['reason']}")
                continue

            row["live"] = True
            row["discovery"] = checks.expect(
                worker.path in priced_paths and worker.path in agent,
                "listed in /work, agent.json and openapi.json (priced)")

            body = dict(bodies[worker.path])
            if worker.name == "speech.transcribe" and synth_audio:
                body = {"audio_base64": synth_audio}  # chain: synthesize -> transcribe

            before = len(state.log)
            with httpx.Client(timeout=worker.max_seconds + 60) as http:
                first = http.post(f"{base}{worker.path}", json=body)
                quoted = None
                if first.status_code == 402:
                    accepts = (first.json() or {}).get("accepts") or []
                    quoted = int(accepts[0].get("maxAmountRequired") or accepts[0].get("amount")) \
                        if accepts else None
                want = round(worker.price_usd * 1_000_000)
                checks.expect(first.status_code == 402 and quoted == want,
                              f"unpaid -> 402 at the catalog price ({first.status_code}, {quoted} vs {want})")
                if first.status_code != 402:
                    row["error"] = f"no 402: HTTP {first.status_code} {first.text[:200]}"
                    continue
                pay_headers = sim._sign(http_client, first.headers, first.content, f"{base}{worker.path}")
                started = time.time()
                try:
                    paid = http.post(f"{base}{worker.path}", json=body, headers=pay_headers)
                except httpx.HTTPError as exc:
                    row["error"] = f"connection broke on the paid call: {exc}"
                    checks.fail(f"paid call answered ({row['error']})")
                    continue
                row["latency_s"] = round(time.time() - started, 1)

            try:
                content = paid.json()
            except ValueError:
                content = {"raw": paid.text[:300]}
            row["http"] = paid.status_code
            row["executed"] = checks.expect(paid.status_code == 200,
                                            f"paid call delivered (HTTP {paid.status_code})")
            if paid.status_code != 200:
                row["error"] = _excerpt(content, 400)
                print(f"        {row['error']}")
            result = content.get("result", content) if isinstance(content, dict) else content
            row["result_excerpt"] = _excerpt(result)
            print(f"        {row['result_excerpt']}")
            if worker.name == "speech.synthesize" and isinstance(result, dict):
                synth_audio = result.get("audio_base64")

            entries = state.log[before:]
            verifies = [e for e in entries if e.get("path") == "/verify"]
            settles = [e for e in entries if e.get("path") == "/settle" and e.get("transaction")]
            receipt = paid.headers.get("PAYMENT-RESPONSE") or paid.headers.get("X-PAYMENT-RESPONSE")
            tx = decode_payment_response_header(receipt).transaction if receipt else None
            if paid.status_code == 200:
                row["payment"] = checks.expect(
                    len(verifies) == 1 and len(settles) == 1
                    and int(settles[0]["amount"]) == want and tx == settles[0]["transaction"],
                    f"verified once, settled once for {want} atomic, receipt matches")
                row["bazaar"] = checks.expect(verifies and verifies[0].get("bazaar") is None,
                                              f"Bazaar record valid ({verifies[0].get('bazaar') if verifies else 'no verify'})")
            else:
                row["payment"] = checks.expect(not settles, "failed call was NOT settled")

            if paid.status_code == 200 and isinstance(content, dict):
                receipt_id = content.get("receipt_id")
                rec = _get(f"{base}/work/receipts/{receipt_id}") if receipt_id else {}
                row["receipt"] = checks.expect(
                    bool(receipt_id) and rec.get("outcome") == "paid_delivered"
                    and rec.get("payment", {}).get("tx_hash") == tx
                    and rec.get("payment", {}).get("amount_atomic") == want
                    and rec.get("delivery", {}).get("result_hash") == _canonical_hash(result),
                    f"receipt {receipt_id}: paid_delivered, tx + amount match settle, "
                    f"result hash matches delivered result")

            with sqlite3.connect(ledger_path) as db:
                call = db.execute(
                    "SELECT call_id, status, settled, provider_used, provider_cost_micros, "
                    "cost_measured, latency_ms, failure_reason FROM worker_calls WHERE path=? "
                    "ORDER BY started_at DESC LIMIT 1", (worker.path,)).fetchone()
                attempts = db.execute("SELECT COUNT(*) FROM worker_provider_calls WHERE call_id=?",
                                      (call[0],)).fetchone()[0] if call else 0
            if call:
                row.update(ledger_status=call[1], settled=bool(call[2]), provider=call[3],
                           cost_usd=(call[4] or 0) / 1e6, cost_measured=bool(call[5]),
                           attempts=attempts, failure_reason=call[7])
            if paid.status_code == 200:
                row["ledger"] = checks.expect(
                    bool(call) and call[1] == "ok" and call[2] == 1 and attempts >= 1,
                    f"ledger: ok, settled, {attempts} provider attempt(s), "
                    f"cost ${row.get('cost_usd', 0):.4f}{'' if row.get('cost_measured') else ' (estimated)'}")

        if not only or "/work/chain/rpc" in only:
            sim.step("Refusal path: invalid input is a 400 and never settles")
            before = len(state.log)
            with httpx.Client(timeout=60) as http:
                first = http.post(f"{base}/work/chain/rpc",
                                  json={"method": "eth_sendRawTransaction", "params": ["0x00"]})
                if first.status_code == 402:
                    pay = sim._sign(http_client, first.headers, first.content, f"{base}/work/chain/rpc")
                    first = http.post(f"{base}/work/chain/rpc",
                                      json={"method": "eth_sendRawTransaction", "params": ["0x00"]},
                                      headers=pay)
            settled = [e for e in state.log[before:] if e.get("path") == "/settle"]
            checks.expect(first.status_code == 400 and not settled,
                          f"write method refused with 400, nothing settled (HTTP {first.status_code})")
    finally:
        node.terminate()
        report["finished"] = time.time()
        (work / "report.json").write_text(json.dumps(report, indent=2, default=str))

    sim.step("Summary")
    for row in report["workers"]:
        if not row.get("live"):
            mark = "UNAVAILABLE"
        else:
            mark = "OK" if all(row.get(k) for k in ("executed", "payment", "ledger", "discovery")) else "FAIL"
        cost = f"${row['cost_usd']:.4f}" if "cost_usd" in row else "-"
        print(f"  {mark:<11} {row['worker']:<22} ${row['price_usd']:<5.2f} cost {cost:<9} "
              f"{row.get('latency_s', '-')}s  {row.get('provider') or row.get('reason', '')}")
    print(f"\n{checks.passed} passed, {checks.failed} failed  (report: {work / 'report.json'})")
    return 0 if checks.failed == 0 else 1


def _load_workers_catalog():
    pkg = REPO / "wcag-audit-engine" / "app" / "workers"
    spec = importlib.util.spec_from_file_location(
        "sim_workers", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["sim_workers"] = module
    spec.loader.exec_module(module)
    return module.catalog


if __name__ == "__main__":
    sys.exit(main())
