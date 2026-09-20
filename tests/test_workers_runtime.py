"""The reliability envelope: retry, fallback, timeout, circuit breaking.

These are the guarantees the worker network sells. A provider failing is
routine; a PAID call failing because we gave up too early, retried something
that could never succeed, or charged for a half-finished job is not.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app"
PKG = APP / "workers"


def _load_workers():
    """Load the worker package by file path, the same way main.py does when it
    is itself loaded by path (which is how this repo's tests load it)."""
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
runtime = W.runtime


class FakeProvider:
    def __init__(self, pid, behaviour, is_available=True):
        self.id = pid
        self.behaviour = behaviour
        self.calls = 0
        self._available = is_available

    def available(self):
        return self._available


async def _call(provider):
    provider.calls += 1
    behaviour = provider.behaviour
    if behaviour == "ok":
        return runtime.ProviderResult({"from": provider.id}, cost_micros=7,
                                      cost_measured=True)
    if behaviour == "transient":
        raise runtime.TransientProviderError("temporarily unavailable")
    if behaviour == "permanent":
        raise runtime.PermanentProviderError("your request is malformed")
    if behaviour == "unconfigured":
        raise runtime.ProviderUnavailable("no credential here")
    if behaviour == "hang":
        await asyncio.sleep(30)
    if behaviour == "boom":
        raise RuntimeError("adapter bug")
    if behaviour == "invalid":
        raise runtime.InvalidRequest("the caller sent something this cannot use")
    raise AssertionError(behaviour)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(runtime, "_BACKOFF_BASE", 0.001)
    monkeypatch.setattr(runtime, "_BACKOFF_CAP", 0.002)
    runtime.reset_breakers()
    yield
    runtime.reset_breakers()


def test_transient_failure_is_retried_on_the_same_provider():
    provider = FakeProvider("a", "transient")
    with pytest.raises(runtime.WorkerError):
        asyncio.run(runtime.run_with_policy([provider], _call, deadline_seconds=5))
    assert provider.calls == runtime._MAX_ATTEMPTS, (
        "a transient failure is exactly the case retries exist for")


def test_permanent_failure_is_not_retried_but_does_fall_back():
    """A 400 will be a 400 next time. Retrying it spends the caller's deadline
    proving that; moving to the next provider might actually work."""
    bad, good = FakeProvider("bad", "permanent"), FakeProvider("good", "ok")
    execution = asyncio.run(
        runtime.run_with_policy([bad, good], _call, deadline_seconds=5))
    assert bad.calls == 1
    assert execution.provider_used == "good"
    assert execution.value == {"from": "good"}


def test_invalid_request_is_never_retried_and_never_falls_back():
    """The CALLER's input is wrong, not the provider's health -- a second
    provider would refuse the same bad input too, so this must propagate
    immediately rather than being treated as the generic-exception retry
    path takes every OTHER unmapped exception through."""
    bad, good = FakeProvider("bad", "invalid"), FakeProvider("good", "ok")
    with pytest.raises(runtime.InvalidRequest):
        asyncio.run(runtime.run_with_policy([bad, good], _call, deadline_seconds=5))
    assert bad.calls == 1, "must not be retried"
    assert good.calls == 0, "must not fall back to the next provider"


def test_max_attempts_overrides_the_default_when_given():
    """A retried generative call re-bills the vendor for a second image --
    image/TTS workers pass max_attempts=1 so a transient failure is refunded
    to the caller rather than silently regenerated at our own cost."""
    provider = FakeProvider("a", "transient")
    with pytest.raises(runtime.WorkerError):
        asyncio.run(runtime.run_with_policy(
            [provider], _call, deadline_seconds=5, max_attempts=1))
    assert provider.calls == 1


def test_fallback_reaches_the_second_provider_after_the_first_exhausts_retries():
    first, second = FakeProvider("first", "transient"), FakeProvider("second", "ok")
    execution = asyncio.run(
        runtime.run_with_policy([first, second], _call, deadline_seconds=5))
    assert first.calls == runtime._MAX_ATTEMPTS
    assert execution.provider_used == "second"
    assert execution.providers_tried == ["first", "second"]


def test_timeout_is_enforced_per_attempt():
    hung = FakeProvider("slow", "hang")
    with pytest.raises(runtime.WorkerError) as caught:
        asyncio.run(runtime.run_with_policy(
            [hung], _call, deadline_seconds=2, per_attempt_seconds=0.05))
    assert caught.value.reason in ("provider_timeout", "deadline_exceeded")


def test_an_unexpected_adapter_exception_is_not_trusted_as_permanent():
    """An unmapped exception is not evidence that a retry is useless."""
    broken = FakeProvider("broken", "boom")
    with pytest.raises(runtime.WorkerError):
        asyncio.run(runtime.run_with_policy([broken], _call, deadline_seconds=5))
    assert broken.calls == runtime._MAX_ATTEMPTS


def test_unconfigured_provider_is_skipped_without_blaming_its_health():
    """A provider with no credential has not FAILED -- it was never there.
    Counting it as a failure would open its circuit and hide a real outage."""
    missing, good = FakeProvider("missing", "unconfigured"), FakeProvider("good", "ok")
    execution = asyncio.run(
        runtime.run_with_policy([missing, good], _call, deadline_seconds=5))
    assert execution.provider_used == "good"
    assert runtime.breaker_state().get("missing", {}).get("failures", 0) == 0


def test_no_provider_available_raises_rather_than_returning_nothing():
    off = FakeProvider("off", "ok", is_available=False)
    with pytest.raises(runtime.ProviderUnavailable):
        asyncio.run(runtime.run_with_policy([off], _call, deadline_seconds=5))


def test_circuit_opens_after_repeated_failures_and_skips_the_provider():
    failing = FakeProvider("flaky", "transient")
    for _ in range(3):
        with pytest.raises(runtime.WorkerError):
            asyncio.run(runtime.run_with_policy([failing], _call, deadline_seconds=5))
    assert runtime.circuit_open("flaky"), "breaker should be open after repeated failure"

    calls_before = failing.calls
    healthy = FakeProvider("healthy", "ok")
    execution = asyncio.run(
        runtime.run_with_policy([failing, healthy], _call, deadline_seconds=5))
    assert execution.provider_used == "healthy"
    assert failing.calls == calls_before, "an open circuit must not be called at all"


def test_a_success_closes_the_circuit():
    provider = FakeProvider("recovers", "transient")
    for _ in range(3):
        with pytest.raises(runtime.WorkerError):
            asyncio.run(runtime.run_with_policy([provider], _call, deadline_seconds=5))
    runtime.reset_breakers()
    provider.behaviour = "ok"
    asyncio.run(runtime.run_with_policy([provider], _call, deadline_seconds=5))
    assert runtime.breaker_state()["recovers"]["failures"] == 0


def test_response_validation_failure_is_retried_then_raises():
    """A provider that answers with a shape we cannot sell has not succeeded.
    Returning it anyway would bill the caller for an unusable result."""
    provider = FakeProvider("shapeless", "ok")

    def reject(_value):
        raise runtime.InvalidProviderResponse("not the shape we sell")

    with pytest.raises(runtime.WorkerError) as caught:
        asyncio.run(runtime.run_with_policy(
            [provider], _call, deadline_seconds=5, validate=reject))
    assert caught.value.reason == "invalid_provider_response"
    assert provider.calls == runtime._MAX_ATTEMPTS


def test_failed_execution_still_carries_its_attempts_for_the_ledger():
    """Without this the only attempts ever recorded would be successful ones,
    and measured reliability would read as 100% no matter what happened."""
    provider = FakeProvider("a", "transient")
    with pytest.raises(runtime.WorkerError) as caught:
        asyncio.run(runtime.run_with_policy([provider], _call, deadline_seconds=5))
    attempts = getattr(caught.value, "attempts", [])
    assert len(attempts) == runtime._MAX_ATTEMPTS
    assert all(a.ok is False for a in attempts)


def test_deadline_is_shared_across_steps_not_restarted_per_step():
    """A composite runs several steps against ONE payment window. If each step
    got the full deadline, three steps could run three times past it."""
    ctx = W.context.JobContext("c1", "composite", deadline_seconds=0.05)
    assert ctx.remaining() <= 0.05
    provider = FakeProvider("a", "ok")

    async def scenario():
        await asyncio.sleep(0.08)
        await ctx.run("late", [provider], _call)

    with pytest.raises(runtime.DeadlineExceeded):
        asyncio.run(scenario())
    assert provider.calls == 0, "a step that starts past the deadline must not run"
