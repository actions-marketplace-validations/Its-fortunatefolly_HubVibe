"""The /work/* routes.

THE ONE THING THIS FILE IS FOR: reusing the existing payment gate rather than
building a second one. A worker route does exactly what an audit route does,
in the same order, through the same functions --

    authorize + rate limit  (refuses before any payment instrument is touched)
    run the job
    _bill                   (settles x402 only after a result exists)
    _deliver                (receipt headers, refused-settle withholding)
    failure -> _failed_audit_response  (502, nothing charged)

-- and those four are INJECTED from app.main at startup. This module never
imports x402_payments, never constructs a challenge, and has no idea how
settlement works. There is one payment implementation in this service and it
is the one that already takes real money.

TWO DELIBERATE DIFFERENCES FROM THE AUDIT ROUTES

1. These handlers are `async def`. The audit routes are sync, so FastAPI runs
   them in anyio's threadpool, which app.main caps at MAX_CONCURRENT_AUDITS
   (2 on the production box) because each audit holds a Chromium context. A
   slow worker sharing that pool would starve the paid audits. So workers run
   on the event loop, the blocking payment gate goes to a dedicated executor,
   and worker concurrency is bounded by its own semaphore.

2. They accept an Idempotency-Key. See ledger.claim_idempotency: it closes
   the duplicate-charge hole the x402 nonce guard cannot see, which is a
   caller that timed out and re-signed.
"""

import asyncio
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from . import catalog, ledger, runtime
from .context import JobContext
from .providers import health as provider_health
from .skills import REGISTRY

log = logging.getLogger("hubvibe.workers.router")

router = APIRouter()

# Injected by app.main at import time. Nothing here works until configure()
# runs, which is deliberate: a worker that could serve a request without the
# payment gate would be a free capability accidentally exposed.
_authorize: Optional[Callable] = None
_bill: Optional[Callable] = None
_deliver: Optional[Callable] = None
_failed: Optional[Callable] = None
_configured = False
# The node's SERVICE_VERSION, stamped on every ledger row so a receipt names
# the build that ran the job. Injected because this module cannot import
# app.main.
_node_version: Optional[str] = None
# For a job paid on the MPP `evm` hash rail: a callable mapping the
# credential to the on-chain facts its verification recorded (payer, amount,
# asset, network, tx). Injected like the gate itself; None means the ledger
# simply has no payment facts for MPP-paid jobs.
_mpp_payment_facts: Optional[Callable] = None

MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", "8"))
_semaphore: Optional[asyncio.Semaphore] = None

# Separate from anyio's threadpool ON PURPOSE (see module docstring): this one
# exists so the blocking payment gate and the blocking browser never consume
# the slots the audit routes need.
_executor: Optional[ThreadPoolExecutor] = None


def configure(authorize_and_rate_limit, bill, deliver, failed_response,
              with_page=None, goto_guarded=None, blocked_target_reason=None,
              node_version=None, mpp_payment_facts=None) -> None:
    """Hand the worker network the core's payment gate and browser pool."""
    global _authorize, _bill, _deliver, _failed, _executor, _semaphore, _configured
    global _node_version, _mpp_payment_facts
    _node_version = node_version
    _mpp_payment_facts = mpp_payment_facts
    _authorize = authorize_and_rate_limit
    _bill = bill
    _deliver = deliver
    _failed = failed_response
    _executor = ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_WORKERS + 2, thread_name_prefix="hubvibe-worker")
    _semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

    from .providers import google_auth, web

    web.configure(with_page=with_page, executor=_executor, goto_guarded=goto_guarded,
                  blocked_target_reason=blocked_target_reason)
    # Resolve Google credentials in the background now, so the one-off cost
    # (and its timeout, if the metadata probe is wedged) lands at startup
    # rather than inside the first agent's paid request.
    google_auth.prime()
    _configured = True
    log.info("worker network configured: %d workers, concurrency %d",
             len(catalog.CATALOG), MAX_CONCURRENT_WORKERS)


def is_configured() -> bool:
    return _configured


def _error_status(reason: str) -> int:
    """HTTP status for a failure reason.

    502 for "the provider failed us", 400 for "your request was wrong", 503
    for "this capability is not configured here". The distinction matters to
    an autonomous caller deciding whether to retry, switch endpoint, or fix
    its own body.
    """
    if reason == runtime.InvalidRequest.reason:
        return 400
    if reason == runtime.ProviderUnavailable.reason:
        return 503
    if reason == runtime.DeadlineExceeded.reason:
        return 504
    return 502


def _mpp_facts(auth) -> Optional[dict]:
    """On-chain facts of an MPP-paid call, from the injected lookup. None
    for x402 (which carries its own pending payment) and for anything else."""
    if _mpp_payment_facts is None or getattr(auth, "payment_method", None) != "mpp":
        return None
    credential = getattr(auth, "mpp_credential", None)
    if not credential:
        return None
    try:
        return _mpp_payment_facts(credential) or None
    except Exception:
        return None


def _rail_of(auth) -> Optional[str]:
    if getattr(auth, "pending_payment", None) is not None:
        return "x402"
    if getattr(auth, "payment_method", None) == "mpp":
        return "mpp"
    return None


def _payer_of(auth) -> Optional[str]:
    """Best-effort payer address, for the repeat-customer signal in the ledger.

    Wrapped in a broad except on purpose: the payload shape belongs to the
    x402 library, and an analytics field is never worth failing a paid call.
    """
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        facts = _mpp_facts(auth)
        return facts.get("payer") if facts else None
    try:
        payload = getattr(pending, "payload", None)
        inner = getattr(payload, "payload", None) or {}
        authorization = inner.get("authorization") or {}
        return authorization.get("from") or None
    except Exception:
        return None


def _payment_facts_of(auth) -> dict:
    """The on-chain facts of this call's payment, for the receipt: network,
    asset, pay-to wallet, exact settled amount, transaction hash.

    Read off the accepted requirement and the facilitator's settle response
    the same way _payer_of reads the payer: by attribute, never by importing
    the payment layer. Any shape surprise yields an empty field, never a
    failed delivery -- and the receipt then says that field is unknown.
    """
    facts = {"network": None, "asset": None, "pay_to": None,
             "amount_atomic": None, "tx_hash": None}
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        mpp = _mpp_facts(auth)
        if mpp:
            facts.update({k: mpp.get(k) for k in facts})
        return facts

    def field(obj, *names):
        for name in names:
            value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if value not in (None, ""):
                return value
        return None

    try:
        requirements = getattr(pending, "requirements", None) or []
        accepted = requirements[0] if requirements else None
        if accepted is not None:
            facts["network"] = field(accepted, "network")
            facts["asset"] = field(accepted, "asset")
            facts["pay_to"] = field(accepted, "pay_to", "payTo")
            amount = field(accepted, "amount", "max_amount_required", "maxAmountRequired")
            facts["amount_atomic"] = int(amount) if amount is not None else None
        result = getattr(pending, "settle_result", None)
        if result is not None:
            facts["tx_hash"] = field(result, "transaction")
            facts["network"] = field(result, "network") or facts["network"]
            amount = field(result, "amount")
            if amount is not None:
                facts["amount_atomic"] = int(amount)
    except Exception:
        pass
    return facts


def _tx_of(auth) -> Optional[str]:
    pending = getattr(auth, "pending_payment", None)
    if pending is None:
        return None
    try:
        result = getattr(pending, "settle_result", None)
        return getattr(result, "transaction", None) if result is not None else None
    except Exception:
        return None


async def _run_job(worker, payload: dict, call_id: str):
    """Execute the worker's skill inside the envelope. Raises WorkerError."""
    skill = REGISTRY.get(worker.skill)
    if skill is None:  # pragma: no cover - guarded by a catalog test
        raise runtime.WorkerError(
            f"Worker {worker.name} has no implementation registered.",
            reason="not_implemented")
    ctx = JobContext(call_id, worker.name, worker.max_seconds)
    result = await skill(ctx, payload)
    return result, ctx


def _record_attempts(call_id: str, ctx_or_attempts) -> None:
    attempts = getattr(ctx_or_attempts, "attempts", ctx_or_attempts) or []
    for attempt in attempts:
        ledger.record_provider_call(
            call_id=call_id, provider=attempt.provider, attempt=attempt.attempt,
            started_at=attempt.started_at, ok=attempt.ok,
            latency_ms=attempt.latency_ms, failure_reason=attempt.failure_reason,
            cost_micros=attempt.cost_micros, cost_measured=attempt.cost_measured,
            usage=attempt.usage)


def _make_handler(worker):
    async def handler(
        request: Request,
        x_api_key: Optional[str] = Header(None),
        x_payment: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ):
        if not _configured:  # pragma: no cover - startup guard
            return JSONResponse(status_code=503, content={
                "status": "error",
                "detail": "Worker network is not configured on this deployment."})

        # Body first, and validated BEFORE payment is read. Same ordering the
        # audit routes use, for the same reason: a 400 raised after the
        # facilitator has verified burns the payer's nonce, so their corrected
        # retry is refused as a replay.
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={
                "status": "error", "reason": "invalid_request",
                "detail": "Body must be a JSON object.", "billed": False})

        skill = REGISTRY.get(worker.skill)
        if skill is None:  # pragma: no cover
            return JSONResponse(status_code=503, content={
                "status": "error", "detail": f"{worker.name} is not implemented here."})

        # Fail closed BEFORE the payment gate. A worker whose provider has no
        # credential on this deployment cannot be delivered, so it must never
        # quote a price -- taking money for a capability we cannot run is the
        # worst outcome available here.
        if not worker.available():
            return JSONResponse(status_code=503, content={
                "status": "error", "reason": "capability_unavailable", "billed": False,
                "detail": (f"{worker.name} is not available on this deployment: "
                           f"{worker.unavailable_reason()}"),
            })

        # Cheap input validation before the gate. The skills raise
        # InvalidRequest from their own validators; run them dry by letting
        # the job start only after payment, but catch the obvious shape errors
        # here via the schema's required keys.
        missing = [key for key in (worker.input_schema.get("required") or [])
                   if key not in payload]
        if missing:
            return JSONResponse(status_code=400, content={
                "status": "error", "reason": "invalid_request",
                "detail": f"Missing required field(s): {', '.join(missing)}.",
                "input_schema": worker.input_schema, "billed": False})

        auth, err = await asyncio.get_running_loop().run_in_executor(
            _executor, lambda: _authorize(
                x_api_key, x_payment, authorization, request,
                price_usd=worker.price_usd))
        if err is not None:
            return err

        call_id = uuid.uuid4().hex
        payer = _payer_of(auth)

        # Duplicate protection. A "done" key returns the stored result and
        # NEVER calls _bill -- so the second payment stays verified but
        # unsettled, and no money moves. That is why no refund path is needed.
        claimed = False
        if idempotency_key:
            state, stored = await asyncio.get_running_loop().run_in_executor(
                _executor, lambda: ledger.claim_idempotency(
                    idempotency_key, call_id, worker.name))
            if state == "done":
                try:
                    content = json.loads(stored) if stored else {}
                except json.JSONDecodeError:
                    content = {}
                content["idempotent_replay"] = True
                content["billed"] = False
                content["note"] = (
                    "Returned the stored result for this Idempotency-Key. This "
                    "request was not charged.")
                return JSONResponse(status_code=200, content=content)
            if state == "in_progress":
                return JSONResponse(status_code=409, headers={"Retry-After": "5"}, content={
                    "status": "error", "reason": "in_progress", "billed": False,
                    "detail": ("A request with this Idempotency-Key is still running. "
                               "Retry shortly to collect its result.")})
            claimed = state == "claimed"

        ledger.open_call(call_id=call_id, worker=worker.name, path=worker.path,
                         price_usd=worker.price_usd, idempotency_key=idempotency_key,
                         payer=payer, rail=_rail_of(auth),
                         request_hash=ledger.canonical_hash(payload),
                         node_version=_node_version)

        try:
            async with _semaphore:
                result, ctx = await _run_job(worker, payload, call_id)
        except runtime.WorkerError as exc:
            _record_attempts(call_id, getattr(exc, "attempts", []))
            ledger.close_call(call_id, "failed", failure_reason=exc.reason,
                              failure_stage="execute", payer=payer)
            if claimed and idempotency_key:
                # The job failed and was not billed, so the caller is entitled
                # to retry the same key.
                ledger.release_idempotency(idempotency_key)
            status = _error_status(exc.reason)
            if status == 400:
                # The caller's fault: nothing was billed, and _failed's 502
                # wording would be wrong.
                return JSONResponse(status_code=400, content={
                    "status": "error", "reason": exc.reason, "detail": exc.detail,
                    "input_schema": worker.input_schema, "billed": False})
            # Everything else goes through the CORE's failure path, so an
            # unbilled worker failure unwinds exactly like an unbilled audit.
            response = _failed(auth, exc.detail)
            response.status_code = status
            try:
                body = json.loads(bytes(response.body).decode())
                body["reason"] = exc.reason
                body["worker"] = worker.name
                body["receipt_id"] = ledger.receipt_id_for(call_id)
                # The core's headers minus the ones describing ITS body: the
                # body just grew, and a copied Content-Length made every
                # failed worker call die mid-response instead of a clean 502.
                headers = {k: v for k, v in response.headers.items()
                           if k.lower() not in ("content-length", "content-type")}
                return JSONResponse(status_code=status, content=body, headers=headers)
            except Exception:  # pragma: no cover
                return response
        except Exception as exc:  # pragma: no cover - unexpected adapter bug
            log.exception("worker %s crashed", worker.name)
            ledger.close_call(call_id, "failed", failure_reason="internal_error",
                              failure_stage="execute", payer=payer)
            if claimed and idempotency_key:
                ledger.release_idempotency(idempotency_key)
            return _failed(auth, f"{worker.name} failed: {type(exc).__name__}")

        _record_attempts(call_id, ctx)

        # Only now, with a real result in hand, is anything charged.
        warning = await asyncio.get_running_loop().run_in_executor(
            _executor, lambda: _bill(auth, worker.price_usd))

        # The receipt: the ledger row read back, at /work/receipts/{id}. Its
        # id is in the delivered body (so an idempotent replay returns the
        # same one) and the result hash is written to the row before the
        # response leaves, so the receipt can never describe a delivery the
        # caller did not get.
        receipt_id = ledger.receipt_id_for(call_id)
        content = {
            "status": "ok",
            "worker": worker.name,
            "price_usd": worker.price_usd,
            "result": result,
            "provenance": ctx.provenance(),
            "receipt_id": receipt_id,
            "receipt_url": f"/work/receipts/{receipt_id}",
        }
        if warning:
            content["billing_warning"] = warning

        delivered = _deliver(content, auth)
        facts = _payment_facts_of(auth)

        # _deliver returns the payable 402 instead when the facilitator
        # REFUSED to settle. Nothing was earned, so nothing is stored under
        # the idempotency key and the ledger says refused.
        refused = getattr(delivered, "status_code", 200) == 402
        ledger.close_call(
            call_id, "refused" if refused else "ok",
            latency_ms=ctx.provenance()["elapsed_ms"],
            provider_used=",".join(ctx.providers_used) or None,
            providers_tried=",".join(ctx.providers_used) or None,
            attempts=len(ctx.attempts), settled=not refused and payer is not None,
            tx_hash=_tx_of(auth) or facts["tx_hash"], payer=payer,
            result_hash=ledger.canonical_hash(result),
            network=facts["network"], asset=facts["asset"],
            pay_to=facts["pay_to"], amount_atomic=facts["amount_atomic"])

        if idempotency_key and claimed:
            if refused:
                ledger.release_idempotency(idempotency_key)
            else:
                ledger.complete_idempotency(idempotency_key, json.dumps(content))
        return delivered

    handler.__name__ = f"work_{worker.name.replace('.', '_')}"
    return handler


def register_routes() -> None:
    """Add one POST route per catalog row."""
    for worker in catalog.CATALOG:
        router.add_api_route(
            worker.path, _make_handler(worker), methods=["POST"],
            name=worker.tool_name, tags=["workers"], summary=worker.title,
            description=f"{worker.description} ${worker.price_usd:.2f} per call.",
            # The 200 contract, in openapi.json: the envelope with this
            # worker's own result schema, and an example generated from it.
            # Without this every /work route documented its response as `{}`.
            responses={
                200: {
                    "description": f"Delivered result of {worker.name}; a receipt is at receipt_url.",
                    "content": {"application/json": {
                        "schema": catalog.response_schema(worker),
                        "example": catalog.response_example(worker),
                    }},
                },
            })


register_routes()


@router.get("/work", tags=["workers"])
async def work_index():
    """Free, unpaid index of the worker network.

    Discovery is free here exactly as it is on /mcp: an agent must be able to
    find out what is for sale and what it costs before deciding to pay.
    """
    live = catalog.live()
    unavailable = [w for w in catalog.CATALOG if w not in live]
    return {
        "workers": [
            {
                "name": worker.name,
                "path": worker.path,
                "title": worker.title,
                "price_usd": worker.price_usd,
                "tier": worker.tier,
                "description": worker.description,
                "tags": worker.tags,
                "input_schema": worker.input_schema,
                "output_schema": worker.output_schema,
                "returns": worker.returns,
                "max_seconds": worker.max_seconds,
                "pricing_basis": worker.pricing_basis,
                "composes": worker.composes,
                **({"buyer_note": catalog.buyer_note(worker)}
                   if catalog.buyer_note(worker) else {}),
            }
            for worker in live
        ],
        "count": len(live),
        "response_envelope": catalog.contract.RESPONSE_ENVELOPE,
        "spend_cap": (
            f"x402 client libraries cap a single payment at ${catalog.SPEND_CAP_USD:.2f} "
            "by default. Workers priced above that carry a buyer_note saying how to "
            "raise the cap; the price itself is fixed."),
        # Named, but NOT sold: an agent that saw this node yesterday can tell
        # "switched off here" apart from "never existed", without us quoting a
        # price for something we cannot run.
        "unavailable": [
            {"name": w.name, "reason": w.unavailable_reason()} for w in unavailable
        ],
        "payment": (
            "Per call over HTTP 402 / x402, the same rail as the audit routes. "
            "POST without payment to receive the challenge."),
        "idempotency": (
            "Send an Idempotency-Key header to make retries safe: a repeated key "
            "returns the stored result and is not charged again."),
        "receipts": (
            "Every response carries a receipt_id. GET /work/receipts/{receipt_id} "
            "(or /work/receipts/{request_id}) returns the machine-readable receipt: "
            "payer, pay_to, amount, asset, network, transaction hash, execution "
            "status, and sha256 hashes of the request and the delivered result."),
        "providers": provider_health(),
    }


@router.get("/work/receipts/{receipt_id}", tags=["workers"])
async def work_receipt(receipt_id: str):
    """The receipt for one job, by receipt_id or request_id. Free: it holds
    on-chain facts and hashes, never the delivered result."""
    call_id = ledger.call_id_for(receipt_id) or (receipt_id if receipt_id.isalnum() else None)
    receipt = ledger.receipt_for(call_id) if call_id else None
    if receipt is None:
        return JSONResponse(status_code=404, content={
            "status": "error", "reason": "unknown_receipt",
            "detail": "No job with this receipt_id or request_id on this node."})
    return receipt
