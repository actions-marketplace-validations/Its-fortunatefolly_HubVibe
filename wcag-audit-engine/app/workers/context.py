"""One job's execution context: runs steps through the envelope and remembers
every provider attempt, so a worker never has to touch the ledger itself.

WHY COMPOSITES NEED THIS

A single-step worker could record its own attempts. A composite cannot: a
research brief is an extraction AND an inference, each with its own retries
and its own cost, and the caller pays ONE price for the whole thing. Without a
shared context the ledger would either see only the last step or need every
worker to remember to report -- and the one that forgets is the one that looks
infinitely profitable.

So every step of every worker goes through `ctx.run(...)`, and the router
writes what the context collected, once, at the end.
"""

import time
from typing import Callable, Optional

from . import runtime


class JobContext:
    __slots__ = ("call_id", "worker", "started", "deadline_at", "attempts",
                 "providers_used", "steps")

    def __init__(self, call_id: str, worker: str, deadline_seconds: float):
        self.call_id = call_id
        self.worker = worker
        self.started = time.monotonic()
        self.deadline_at = self.started + deadline_seconds
        self.attempts = []
        self.providers_used = []
        self.steps = []

    def remaining(self) -> float:
        """Seconds left in the caller's budget.

        Each step gets what is LEFT, not the full budget: three steps that
        each believe they have the whole deadline can together run three times
        past it, and the caller's payment window is finite.
        """
        return max(0.0, self.deadline_at - time.monotonic())

    async def run(self, step: str, providers: list, call: Callable,
                  per_attempt_seconds: Optional[float] = None,
                  validate: Optional[Callable] = None,
                  budget_seconds: Optional[float] = None,
                  max_attempts: Optional[int] = None):
        """Run one step and record it. Returns the step's value."""
        remaining = self.remaining()
        if remaining <= 0:
            raise runtime.DeadlineExceeded(
                f"Ran out of time before the '{step}' step could start.")
        budget = min(remaining, budget_seconds) if budget_seconds else remaining

        started = time.monotonic()
        try:
            execution = await runtime.run_with_policy(
                providers, call, deadline_seconds=budget,
                per_attempt_seconds=per_attempt_seconds, validate=validate,
                max_attempts=max_attempts)
        except runtime.WorkerError as exc:
            # Attempts made before the failure still happened, still cost
            # latency, and still belong in the ledger.
            self.attempts.extend(getattr(exc, "attempts", []) or [])
            self.steps.append({"step": step, "ok": False, "reason": exc.reason,
                               "ms": int((time.monotonic() - started) * 1000)})
            raise

        self.attempts.extend(execution.attempts)
        if execution.provider_used and execution.provider_used not in self.providers_used:
            self.providers_used.append(execution.provider_used)
        self.steps.append({"step": step, "ok": True,
                           "provider": execution.provider_used,
                           "ms": execution.latency_ms})
        return execution.value

    def provenance(self) -> dict:
        """What the buyer gets told about how their result was produced.

        Included in the response on purpose: an agent deciding whether to
        trust and re-buy a result needs to see which providers ran and how
        long each took. It is also the honest disclosure that a composite is
        several calls, not one.
        """
        return {
            "steps": self.steps,
            "providers_used": self.providers_used,
            "attempts": len(self.attempts),
            "elapsed_ms": int((time.monotonic() - self.started) * 1000),
        }
