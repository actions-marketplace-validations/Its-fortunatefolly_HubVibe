"""The HubVibe worker network -- the bees, behind the existing hive.

WHAT THIS IS

The audit routes are unchanged and remain what they are: five deterministic
site audits at $0.05-$0.15, sold over x402. This package adds a second family
of capabilities beside them, sharing the ONE payment implementation this
service already has.

WHAT IT DELIBERATELY IS NOT

Not a second payment system: router.py is handed app.main's own gate
functions and cannot reach x402_payments at all.
Not a second price list for the audits: catalog.py covers only /work/* routes.
Not a replacement for anything: nothing here is imported by the audit path,
so a worker that breaks cannot take an audit down with it.

LAYOUT

    providers/  thin adapters to services that already exist (Vertex,
                BigQuery, Base RPC, Coinbase public market data, Polymarket,
                and this service's own browser pool)
    skills/     the workers themselves -- a defined task in, a completed
                result out; composites call other skills rather than
                reimplementing them
    runtime     timeouts, retry with backoff, circuit breaking, fallback
    context     per-job step accounting, so composites bill honestly
    ledger      per-call price, measured provider cost, latency, payer
    catalog     one row per sellable worker
    router      the /work/* routes, using the core's payment gate
"""

from . import catalog, context, ledger, providers, router, runtime, skills  # noqa: F401

configure = router.configure
is_configured = router.is_configured


def health() -> dict:
    """Worker-network status for /health. Cheap by design -- no outbound calls."""
    from .providers import health as provider_health

    return {
        "configured": router.is_configured(),
        "workers": len(catalog.CATALOG),
        "ledger": ledger.status(),
        "providers": provider_health(),
        "breakers": runtime.breaker_state(),
    }
