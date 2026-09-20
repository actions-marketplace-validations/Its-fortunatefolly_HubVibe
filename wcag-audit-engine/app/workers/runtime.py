"""The execution envelope every worker job runs inside: timeouts, bounded
retry with backoff, per-provider circuit breaking, and fallback.

WHY THIS IS A SEPARATE LAYER

The audit routes get their reliability from depending on nothing but our own
browser. A worker depends on somebody else's API, and at volume somebody
else's API is down, slow, rate-limiting, or returning a shape it did not
return yesterday. If each worker handled that itself we would get a dozen
slightly different retry policies and no way to measure any of them.

THE RULE THAT SHAPES EVERYTHING HERE

A failed job must cost the caller nothing. The payment gate settles only
after a job returns a result (main._bill), so every path out of this module
that is not a success RAISES -- never a partial object the route would then
bill for. Half a research brief is not a product.

ASYNC ON PURPOSE: provider calls are async so asyncio.wait_for imposes a real
deadline. The synchronous payment gate runs in its own executor instead,
which keeps a slow provider away from the small anyio threadpool the paid
audit routes depend on.
"""

import asyncio
import logging
import os
import random
import time
from typing import Any, Callable, Optional

log = logging.getLogger("hubvibe.workers.runtime")


class WorkerError(Exception):
    """`reason` is a short machine-readable slug for the caller and the
    ledger; `detail` is the human sentence."""
    reason = "worker_failed"

    def __init__(self, detail: str, reason: Optional[str] = None):
        super().__init__(detail)
        self.detail = detail
        if reason:
            self.reason = reason


class InvalidRequest(WorkerError):
    """The caller's body is wrong. Never retried, never billed, and refused
    BEFORE any payment is read -- the same ordering the audit routes use, for
    the same reason: a 400 after a verify burns the payer's nonce."""
    reason = "invalid_request"


class TransientProviderError(WorkerError):
    """Worth retrying: timeout, 429, 5xx, connection reset."""
    reason = "provider_transient"


class PermanentProviderError(WorkerError):
    """Not worth retrying HERE: 400/401/403/unsupported. A different provider
    may still succeed."""
    reason = "provider_permanent"


class ProviderUnavailable(WorkerError):
    """Not configured on this deployment. Fail-closed: never advertised, and
    never counted as a failure against its own health."""
    reason = "provider_unavailable"


class InvalidProviderResponse(WorkerError):
    """The provider answered, but not with something we can sell."""
    reason = "invalid_provider_response"


class DeadlineExceeded(WorkerError):
    reason = "deadline_exceeded"


class ProviderResult:
    """What a provider hands back: the value, plus what it cost us.

    `cost_measured=False` is the honest default. A provider that cannot tell
    us what a call cost records an unmeasured attempt, and the margin report
    excludes it rather than assuming it was free.
    """
    __slots__ = ("value", "cost_micros", "cost_measured", "usage")

    def __init__(self, value: Any, cost_micros: Optional[int] = None,
                 cost_measured: bool = False, usage: Optional[str] = None):
        self.value = value
        self.cost_micros = cost_micros
        self.cost_measured = cost_measured
        self.usage = usage


# --- circuit breaker --------------------------------------------------------
# A provider that just failed N times will almost certainly fail again, and
# every attempt spends latency the caller is paying for. Opening the circuit
# sends the job straight to the fallback instead of proving the obvious.

_BREAKER_THRESHOLD = int(os.environ.get("WORKER_BREAKER_THRESHOLD", "5"))
_BREAKER_COOLDOWN = float(os.environ.get("WORKER_BREAKER_COOLDOWN_SECONDS", "60"))


class _Breaker:
    __slots__ = ("failures", "opened_at")

    def __init__(self):
        self.failures = 0
        self.opened_at = 0.0


_breakers: dict = {}


def _breaker(pid: str) -> _Breaker:
    b = _breakers.get(pid)
    if b is None:
        b = _Breaker()
        _breakers[pid] = b
    return b


def circuit_open(pid: str) -> bool:
    b = _breaker(pid)
    if b.failures < _BREAKER_THRESHOLD:
        return False
    if (time.monotonic() - b.opened_at) >= _BREAKER_COOLDOWN:
        b.failures = _BREAKER_THRESHOLD - 1  # half-open: let one through
        return False
    return True


def note_success(pid: str) -> None:
    _breaker(pid).failures = 0


def note_failure(pid: str) -> None:
    b = _breaker(pid)
    b.failures += 1
    if b.failures >= _BREAKER_THRESHOLD:
        b.opened_at = time.monotonic()
        log.warning("circuit opened for provider %s after %d failures", pid, b.failures)


def reset_breakers() -> None:
    _breakers.clear()


def breaker_state() -> dict:
    return {p: {"failures": b.failures, "open": circuit_open(p)} for p, b in _breakers.items()}


_MAX_ATTEMPTS = int(os.environ.get("WORKER_MAX_ATTEMPTS", "3"))
_BACKOFF_BASE = float(os.environ.get("WORKER_BACKOFF_BASE_SECONDS", "0.4"))
_BACKOFF_CAP = float(os.environ.get("WORKER_BACKOFF_CAP_SECONDS", "8.0"))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter. Jitter is not decoration: without
    it a hundred agents hitting the same outage retry in lockstep and hammer
    the provider back down the moment it recovers."""
    return random.uniform(0, min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** max(0, attempt - 1))))


class Attempt:
    __slots__ = ("provider", "attempt", "started_at", "latency_ms", "ok",
                 "failure_reason", "cost_micros", "cost_measured", "usage")

    def __init__(self, provider: str, attempt: int, started_at: float):
        self.provider, self.attempt, self.started_at = provider, attempt, started_at
        self.latency_ms = self.failure_reason = self.cost_micros = self.usage = None
        self.ok = False
        self.cost_measured = False


class Execution:
    __slots__ = ("value", "provider_used", "attempts", "providers_tried", "latency_ms")

    def __init__(self):
        self.value = self.provider_used = self.latency_ms = None
        self.attempts = []
        self.providers_tried = []


async def run_with_policy(providers: list, call: Callable, deadline_seconds: float,
                          per_attempt_seconds: Optional[float] = None,
                          validate: Optional[Callable] = None,
                          max_attempts: Optional[int] = None) -> Execution:
    """Run `call(provider)` across `providers` in order until one succeeds.

    Order is the fallback order. Within a provider, transient failures retry
    with backoff until the attempt budget or the deadline runs out; a
    permanent failure moves straight to the next provider rather than burning
    retries on a 400 that will never change.

    Raises on total failure, reporting the last meaningful error -- "every
    provider failed" without saying how is not a reason anyone can act on.
    """
    started = time.monotonic()
    ends_at = started + deadline_seconds
    execution = Execution()
    last_error: Optional[WorkerError] = None

    usable = [p for p in providers if p.available()]
    if not usable:
        raise ProviderUnavailable(
            "No provider for this capability is configured on this deployment.")

    for provider in usable:
        if circuit_open(provider.id):
            log.info("skipping provider %s: circuit open", provider.id)
            last_error = last_error or TransientProviderError(
                f"{provider.id} is failing repeatedly and was skipped.",
                reason="provider_circuit_open")
            continue

        execution.providers_tried.append(provider.id)

        attempt_cap = max_attempts if max_attempts is not None else _MAX_ATTEMPTS
        for n in range(1, attempt_cap + 1):
            remaining = ends_at - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded(
                    f"Job exceeded its {deadline_seconds:.0f}s budget before completing.")

            attempt = Attempt(provider.id, n, time.time())
            execution.attempts.append(attempt)
            t0 = time.monotonic()
            window = min(remaining, per_attempt_seconds or remaining)

            try:
                result = await asyncio.wait_for(call(provider), timeout=window)
                if validate is not None:
                    validate(result.value)
            except asyncio.TimeoutError:
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.failure_reason = "timeout"
                note_failure(provider.id)
                last_error = TransientProviderError(
                    f"{provider.id} did not answer within {window:.0f}s.",
                    reason="provider_timeout")
            except InvalidRequest:
                # The CALLER's input is wrong, not any provider's fault -- no
                # retry, no fallback to the next provider (it would refuse
                # the same input too), straight out to the 400 the router
                # already maps `invalid_request` to.
                raise
            except ProviderUnavailable as exc:
                # Not a health signal: it was never there. Leave its breaker alone.
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.failure_reason = exc.reason
                last_error = exc
                break
            except PermanentProviderError as exc:
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.failure_reason = exc.reason
                note_failure(provider.id)
                last_error = exc
                break
            except (TransientProviderError, InvalidProviderResponse) as exc:
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.failure_reason = exc.reason
                note_failure(provider.id)
                last_error = exc
            except Exception as exc:
                # Adapter bug or unmapped client error: treated as transient
                # once rather than trusted -- an unknown exception is not
                # evidence that a retry is useless.
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.failure_reason = f"unexpected:{type(exc).__name__}"
                note_failure(provider.id)
                last_error = TransientProviderError(
                    f"{provider.id} failed unexpectedly: {type(exc).__name__}: {exc}",
                    reason="provider_error")
            else:
                attempt.latency_ms = int((time.monotonic() - t0) * 1000)
                attempt.ok = True
                attempt.cost_micros = result.cost_micros
                attempt.cost_measured = result.cost_measured
                attempt.usage = result.usage
                note_success(provider.id)
                execution.value = result.value
                execution.provider_used = provider.id
                execution.latency_ms = int((time.monotonic() - started) * 1000)
                return execution

            if n < attempt_cap:
                delay = _backoff_delay(n)
                if (time.monotonic() + delay) >= ends_at:
                    break
                await asyncio.sleep(delay)

    execution.latency_ms = int((time.monotonic() - started) * 1000)
    failure = last_error or TransientProviderError("No provider produced a result.")
    # Carry the attempts out on the exception: they happened, they cost the
    # caller latency, and the ledger must record them even though the job
    # failed. Without this the only calls with attempt rows would be the
    # successful ones, and measured reliability would read as 100%.
    failure.attempts = execution.attempts
    raise failure
