"""The bees.

A provider adapter is a tool. A skill in here is a WORKER: it takes a defined
task, picks and invokes the right capability, processes what comes back, and
returns a completed result a buying agent can use without further work.

Each skill is `async def run(ctx, payload) -> dict`. It validates its own
input (raising runtime.InvalidRequest, which the router answers before any
payment is read), runs its steps through `ctx.run` so every provider attempt
is accounted for, and returns the finished product.

Composites live in `composites.py` and are built by CALLING these same
skills -- not by reimplementing them. That is what makes the network compose
rather than merely coexist.
"""

from . import (  # noqa: F401
    chain, code, composites, data, extract, fetch, llm, maps, market, media,
    monitor, search, security, stats, verify,
)

REGISTRY = {}
# A skill may also export PRECHECKS: {name: fn(payload)} -- a validator the
# router runs BEFORE the payment gate, raising a WorkerError (InvalidRequest,
# ProviderUnavailable) for a request it can already tell it cannot deliver.
# Refusing there costs the caller nothing and burns no x402 nonce.
PRECHECKS = {}
for _module in (extract, llm, chain, market, data, composites, search, code,
                media, security, monitor, verify, fetch, maps, stats):
    REGISTRY.update(_module.SKILLS)
    PRECHECKS.update(getattr(_module, "PRECHECKS", {}))
