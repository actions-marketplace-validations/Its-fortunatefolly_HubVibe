"""Stripe usage-based billing for the audit endpoint.

Customers subscribe once via Checkout (no upfront line-item charge), and
every real audit call reports one Meter Event. Stripe aggregates usage and
invoices on its own billing cycle -- this module never tracks a balance
itself, so there's no custom balance-tracking code that can drift from what
Stripe actually bills.

Uses the current Stripe Billing Meters API (stripe.billing.MeterEvent), not
the legacy SubscriptionItem.create_usage_record, which Stripe retired for
new integrations.

Requires, at deploy time (see README.md):
- STRIPE_SECRET_KEY        (Secret Manager)
- STRIPE_WEBHOOK_SECRET    (Secret Manager; from the Stripe Dashboard webhook config)
- STRIPE_METERED_PRICE_ID  (Stripe Dashboard: a recurring Price with
                             usage_type=metered, attached to a Billing Meter)
- STRIPE_METER_EVENT_NAME  (the `event_name` configured on that Meter)

Firestore (via Cloud Run's default service account / ADC) stores only the
api_key -> Stripe customer_id mapping -- nothing about balances or pricing,
since Stripe is the source of truth for both.
"""

import logging
import os
import secrets
import uuid
from typing import Optional

import stripe

# .strip() because a secret piped in with `echo` carries a trailing newline,
# which is the single most common way a correct key arrives broken. Stripe
# rejects the whitespace-padded value, and without stripping here the padding
# would also defeat the prefix check below and silently pull every plan out of
# the manifest. Empty after stripping is None, not "", so a blank secret reads
# the same as an absent one.
stripe.api_key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip() or None
_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() or None
_METERED_PRICE_ID = os.environ.get("STRIPE_METERED_PRICE_ID")
_METER_EVENT_NAME = os.environ.get("STRIPE_METER_EVENT_NAME", "wcag_audit_call")

# How the Stripe Meter behind that Price aggregates events. `count` ignores
# the event's value and counts events; `sum` adds the values up. It is not
# editable after the meter is created, so it is configuration here, not an
# assumption. Default `count`, which is what this account's meter was created
# with -- see record_usage for what each one means for how usage is reported.
_METER_AGGREGATION = (os.environ.get("STRIPE_METER_AGGREGATION") or "count").strip().lower()

# What one unit on the metered Price is worth, in cents. The live Price
# (price_1U2Hqm...) is $0.01 per unit, so a $0.05 audit is 5 units and a $0.15
# bundle is 10. See record_usage: the meter counts cents, not calls, and this
# is the one number that ties the two together. If the Price is ever changed,
# change it here in the same breath -- a mismatch here is a silent, uniform
# mis-bill, which is the worst kind.
_METER_UNIT_CENTS = int(os.environ.get("STRIPE_METER_UNIT_CENTS", "1"))

# Stripe secret keys are sk_/rk_ prefixed -- live, test, or restricted.
# https://docs.stripe.com/keys
_STRIPE_KEY_PREFIXES = ("sk_", "rk_")


def stripe_key_looks_valid() -> bool:
    """Whether STRIPE_SECRET_KEY is shaped like a Stripe secret key at all.

    Truthiness is not enough to decide the Stripe rail is live. Any non-empty
    string made is_configured() return True, so a variable holding the wrong
    value -- an payout address, a price ID, a publishable pk_ key, a partly
    pasted secret -- would have had the manifest advertise all three plans
    and the stripe_api_key rail to every agent that asked, and sent buyers to
    a checkout that could not authenticate. Advertising a rail that cannot
    settle is the one thing this service must never do (it already omits x402
    rather than publish payTo:null), so a key that cannot possibly work has
    to count as no key.

    Shape-only, deliberately: it cannot tell a revoked or wrong-account key
    from a good one, and it does not call Stripe, because resolving this at
    import time would tie container startup to Stripe being reachable. It
    catches the class of failure that actually occurs -- the wrong value in
    the right variable -- not authorization.
    """
    key = stripe.api_key
    return bool(key) and key.startswith(_STRIPE_KEY_PREFIXES)


# RETIRED. The old single flat "Agency / Developer" subscription, replaced
# by the per-site plans below. Read only so a deployment still configured
# with it keeps working; nothing advertises it and no new checkout selects
# it. Delete once no live deployment sets it.
_FLAT_SUBSCRIPTION_PRICE_ID = os.environ.get("STRIPE_FLAT_SUBSCRIPTION_PRICE_ID")

# Fallback included-scans cap for a subscriber whose plan we don't know
# (legacy customers activated before the plan was recorded at checkout).
SAAS_MONTHLY_QUOTA = int(os.environ.get("SAAS_MONTHLY_QUOTA", "1500"))

# Per-plan monthly call caps.
#
# One global 1,500 cap for every subscriber was a plan-breaking bug. Agency
# was sold as "50 sites, audited daily": 50 bundle calls a day is 1,550 in a
# 31-day month, so that customer got cut off before month end -- and if they
# audited per-dimension rather than bundling (4 calls per site per day) they
# hit the wall around day 7 and started getting 402s on a plan they had
# already paid for. Pro and Agency also shared the same ceiling, so tripling
# the price bought no extra capacity at all.
#
# These are sized to the promise with real headroom, because the cap exists
# to stop runaway abuse, not to meter value: marginal cost is ~$0.00007 per
# audit, so even 10,000 audits is about $0.70 against a plan's revenue.
# Under-sizing this costs a customer; over-sizing it costs pennies. Kept
# for keys issued before the plans were retired (2026-09-06).
PLAN_MONTHLY_QUOTA = {
    "pro": int(os.environ.get("QUOTA_PRO", "2000")),  # 5 sites x 4 checks x 31d = 620
    "agency": int(os.environ.get("QUOTA_AGENCY", "10000")),  # 50 x 4 x 31 = 6,200
}


def monthly_quota_for(plan: Optional[str]) -> int:
    return PLAN_MONTHLY_QUOTA.get(plan or "", SAAS_MONTHLY_QUOTA)


# Human-facing plans, priced per SITE MONITORED rather than per scan.
# Denominating a plan in scans invites the obvious arithmetic against the
# $0.05 machine rate; the old scan-denominated plan worked out dearer per
# scan than paying per call, so nobody rational would buy it. Sites are the
# unit a human actually cares about and aren't comparable to the machine
# rate, so the two audiences stop competing with each other.
#
# Each is a Stripe Price you create in the Dashboard; a tier with no price ID
# configured is simply not offered rather than half-working.
PLAN_PRICE_IDS = {
    "pro": os.environ.get("STRIPE_PRICE_PRO"),
    "agency": os.environ.get("STRIPE_PRICE_AGENCY"),
}

# One-time purchase (mode="payment", not a subscription): a single full
# bundle report on one URL, for the visitor who will never subscribe. Pure
# margin and it captures traffic that would otherwise bounce.
ONEOFF_REPORT_PRICE_ID = os.environ.get("STRIPE_PRICE_ONEOFF_REPORT")

# What each plan costs and covers, kept here beside the price IDs rather than
# retyped in the manifest and the landing page. The agent manifest went on
# advertising the retired subscription long after Stripe had stopped selling
# it, because the number lived in a second place nobody thought to update --
# a quoted price that no checkout will honour is worse than no price at all.
#
# RETIRED 2026-09-06, owner's call: "why would anyone pay that when the
# scans are 5 cents". The per-call rails are the product and the only
# thing sold. Empty on purpose: human_plans_live() is [] on every deploy,
# no surface advertises a tier, and /billing/checkout refuses a plan. The
# quota and price-ID plumbing below stays only so a key issued before this
# date keeps working until it lapses.
HUMAN_PLANS: list = []


def _plan_offered(plan_id: str) -> bool:
    """Only a plan in HUMAN_PLANS can be sold; a configured Price ID for a
    retired plan is not an offer."""
    return any(p["id"] == plan_id for p in HUMAN_PLANS)


def plan_available(plan: str) -> bool:
    return bool(_plan_offered(plan) and stripe_key_looks_valid() and PLAN_PRICE_IDS.get(plan))


def oneoff_report_available() -> bool:
    return bool(_plan_offered("report") and stripe_key_looks_valid() and ONEOFF_REPORT_PRICE_ID)


def human_plans_live() -> list:
    """The plans this deployment can actually take money for.

    Same discipline as the payment-rail list: a plan whose Stripe Price ID
    isn't configured is omitted rather than advertised, so nothing in the
    manifest points at a checkout that would fail.
    """
    live = []
    for plan in HUMAN_PLANS:
        available = (
            oneoff_report_available()
            if plan["id"] == "report"
            else plan_available(plan["id"])
        )
        if available:
            live.append(plan)
    return live


_db = None

# Which store holds the api_key -> record mapping (and prepaid balances,
# quotas, reports). "firestore" is the Cloud Run deployment's store
# and the default; "sqlite" backs the same operations with one local file
# (KEY_STORE_SQLITE_PATH), which is what makes this service deployable on a
# host that is not Google -- the per-call rails never needed Google, but the
# key the MPP top-up sells has to be written SOMEWHERE, and until this
# existed that somewhere was Firestore only.
_KEY_STORE_BACKEND = (os.environ.get("KEY_STORE") or "firestore").strip().lower()
_KEY_STORE_SQLITE_PATH = os.environ.get("KEY_STORE_SQLITE_PATH", "/data/hubvibe-keys.db")


def _any_sellable_price() -> bool:
    """True if Stripe has at least one Price this service can charge.

    The current catalogue is the three per-site plans; the flat and metered
    IDs are the retired ones, kept here only so a deployment still running on
    them doesn't regress. Gating on the retired pair alone was a live bug: a
    node configured with today's plans and nothing else reported
    is_configured() == False, so /billing/checkout answered 501 while
    /.well-known/agent.json cheerfully advertised all three tiers.
    """
    return bool(
        _FLAT_SUBSCRIPTION_PRICE_ID
        or _METERED_PRICE_ID
        or ONEOFF_REPORT_PRICE_ID
        or any(PLAN_PRICE_IDS.values())
    )


def is_configured() -> bool:
    return bool(stripe_key_looks_valid() and _WEBHOOK_SECRET and _any_sellable_price())


def _load_keystore_sqlite():
    """Import the sibling module under either load style (package import, or
    the by-file-path loading main.py documents), same discipline as main.py's
    _load_sibling_module: one instance per process, cached in sys.modules."""
    try:
        from . import keystore_sqlite

        return keystore_sqlite
    except ImportError:
        import importlib.util
        import sys
        from pathlib import Path

        name = "wcag_audit_engine_keystore_sqlite"
        cached = sys.modules.get(name)
        if cached is not None:
            return cached
        module_path = Path(__file__).resolve().parent / "keystore_sqlite.py"
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def _firestore():
    global _db
    if _db is None:
        if _KEY_STORE_BACKEND == "sqlite":
            _db = _load_keystore_sqlite().SqliteKeyStore(_KEY_STORE_SQLITE_PATH)
        elif _KEY_STORE_BACKEND == "firestore":
            from google.cloud import firestore

            _db = firestore.Client()
        else:
            # Refuse to guess: a typo'd backend silently falling through to
            # Firestore on a box with no Google credentials would fail every
            # keyed call at runtime with the least useful possible error.
            raise ValueError(
                f"KEY_STORE is {_KEY_STORE_BACKEND!r}; it must be 'firestore' or 'sqlite'"
            )
    return _db


def _run_transactional(fn):
    """Run fn(transaction) atomically on whichever key store is live.

    The SQLite store brings its own transaction runner (BEGIN IMMEDIATE, so
    two debits of one key serialize); Firestore's is the library's own
    `transactional` decorator. One helper rather than a branch in each
    caller, so the two spots that need atomicity -- the prepaid debit and
    the quota increment -- cannot end up on different contracts.
    """
    db = _firestore()
    if hasattr(db, "run_in_transaction"):
        return db.run_in_transaction(fn)
    from google.cloud import firestore

    return firestore.transactional(fn)(db.transaction())


def create_checkout_session(
    email: str, success_url: str, cancel_url: str, plan: Optional[str] = None
) -> str:
    """Start a subscription for one of the named per-site plans.

    `plan` is what the landing page always sends and is the only supported
    way to buy. The no-plan fallback to the retired flat/metered price is
    kept solely for deployments still configured that way; where those IDs
    are unset it raises rather than reaching Stripe with price=None, which
    surfaced as an opaque 500 instead of telling the caller what to pick.
    """
    if plan:
        if not _plan_offered(plan):
            raise ValueError(
                f"Plan {plan!r} is retired: there are no subscriptions. "
                "Pay per call ($0.05 an audit, $0.15 the bundle) with a rail from the 402."
            )
        price_id = PLAN_PRICE_IDS.get(plan)
        if not price_id:
            raise ValueError(f"Plan {plan!r} is not configured on this deployment")
    else:
        price_id = _FLAT_SUBSCRIPTION_PRICE_ID or _METERED_PRICE_ID
        if not price_id:
            offered = sorted(p for p, pid in PLAN_PRICE_IDS.items() if pid)
            raise ValueError(
                "No plan specified and this deployment has no default price. "
                f"Pass one of: {', '.join(offered) or '(none configured)'}"
            )
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        line_items=[{"price": price_id}],
        # Which plan was bought, so activate_customer can record it against
        # the key and the monthly cap can match what the customer paid for.
        # Reading it back off the Price ID would mean an extra expanded
        # lookup on every webhook for something we already know here.
        metadata={"plan": plan} if plan else {},
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
    )
    return session.url


def verify_webhook(payload: bytes, sig_header: Optional[str]) -> dict:
    return stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)


def activate_customer(checkout_session: dict) -> str:
    """Mint an API key for a completed checkout and persist the mapping.

    Idempotent: re-delivering the same webhook event (Stripe retries on
    non-2xx responses) returns the existing key instead of minting a new one.
    """
    customer_id = checkout_session["customer"]
    plan = (checkout_session.get("metadata") or {}).get("plan")
    db = _firestore()

    customer_ref = db.collection("customers").document(customer_id)
    existing = customer_ref.get()
    if existing.exists:
        return existing.to_dict()["api_key"]

    api_key = secrets.token_urlsafe(32)
    # `plan` rides on the key document so the auth path gets it from the
    # lookup it already does, rather than a second Firestore read per call.
    db.collection("api_keys").document(api_key).set(
        {"customer_id": customer_id, "active": True, "plan": plan}
    )
    customer_ref.set({"api_key": api_key, "plan": plan})
    return api_key


def api_key_for_session(session_id: str) -> Optional[str]:
    """Best-effort lookup for the checkout success page.

    Returns None while the webhook hasn't landed yet -- callers should treat
    that as "pending" and poll briefly, not as an error.
    """
    session = stripe.checkout.Session.retrieve(session_id)
    if session.status != "complete" or not session.customer:
        return None
    doc = _firestore().collection("customers").document(session.customer).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("api_key")


_key_store_warned = False


def _warn_key_store_unavailable(exc: Exception) -> None:
    """Say so, once, when the key store cannot be reached at all.

    Logged once rather than per request because this fires on every keyed
    call, and a paid endpoint under load would otherwise bury the logs.
    """
    global _key_store_warned
    if _key_store_warned:
        return
    _key_store_warned = True
    logging.getLogger(__name__).error(
        "API key lookup failed against Firestore (%s: %s). Every X-API-Key "
        "call will fall through to a 402 payment challenge until this is "
        "fixed -- subscribers cannot authenticate. If this is "
        "'The database (default) does not exist', no Firestore database has "
        "been created for this project; create one, because checkout also "
        "writes keys there.",
        type(exc).__name__,
        exc,
    )


def lookup_key(api_key: str) -> Optional[dict]:
    """The record behind an API key, or None if there isn't a usable one.

    Returns None rather than raising when the key store itself is
    unreachable. That distinction matters more than it looks: an
    unhandled exception here becomes an HTTP 500, and a 500 tells a machine
    caller nothing it can act on -- it cannot pay, cannot retry usefully, and
    cannot tell a broken backend from a rejected key. Returning None lets the
    caller fall through to x402/MPP, which do not touch Firestore at all, and
    answer with the 402 challenge the caller can actually act on.

    This is still fail-closed: an unreachable key store grants nobody access,
    it only changes an unactionable 500 into an actionable 402. The cost is
    that a genuine subscriber is asked to pay per call while the outage
    lasts, which is why it is logged at ERROR rather than swallowed.
    """
    try:
        doc = _firestore().collection("api_keys").document(api_key).get()
    except Exception as exc:
        _warn_key_store_unavailable(exc)
        return None
    if not doc.exists or not doc.to_dict().get("active"):
        return None
    return doc.to_dict()


def record_usage(customer_id: str, price_cents: int) -> None:
    """Bill a completed audit call, for what the call actually costs.

    Only call this after a real audit ran -- never for requests that errored
    out before producing a result. Uses a fresh idempotency identifier per
    call so a retried request can't double-bill.

    **The meter counts cents, not calls.** That is the whole fix. This used to
    report one event per "unit", where a unit was a call ($0.05) or a third of
    a bundle -- and the metered Price on the account is $0.01 per unit, so a
    $0.05 audit metered $0.01 and a $0.15 bundle metered $0.03 under the old
    per-call unit count. Every invoice
    this ever produced would have been for roughly a third of the money owed.
    It was invisible because the human plans are `licensed` flat prices with
    no metered item on the subscription: the events were accepted by Stripe,
    aggregated by the meter, and charged to nobody. A silent 3x undercharge
    waiting for the day someone attached the Price.

    Reporting the price in cents against a $0.01/unit Price makes the two
    reconcile exactly -- 5 units for $0.05, 15 for $0.15 -- with no second
    meter, no second Price, and no per-route arithmetic anywhere else.

    Two facts live on the Stripe side, which is why both are variables here
    rather than literals. Getting either wrong is a silent, uniform mis-bill,
    so the code states what it assumes instead of hoping:

    1. `STRIPE_METER_UNIT_CENTS` -- what one unit costs on the Price that gets
       attached to the metered subscription item ($0.01 today). Change the
       Price, change the variable, in that order: reconcile BEFORE attaching.
    2. `STRIPE_METER_AGGREGATION` -- how the Meter adds events up, and it
       decides how usage has to be reported:
         * `count` (the default, and what this account's meter was created
           with): the event's `value` is IGNORED and each event counts as one
           unit, so N units means N events.
         * `sum`: one event carrying `value: N`.
       Reporting `value: 3` to a `count` meter bills one unit -- the same
       undercharge this function exists to fix, wearing a different hat. A
       meter's formula cannot be edited after creation, so moving to `sum`
       (one API call per audit instead of three) means a new Meter, a new
       Price reconciled against it, and then this variable.

    Raises rather than guessing when the price is not a whole number of meter
    units: silently rounding is how a rate becomes wrong by a few percent
    forever. The caller turns that into a visible billing_warning on the
    response rather than failing the audit the customer already received.
    """
    if _METER_UNIT_CENTS <= 0:
        raise ValueError("STRIPE_METER_UNIT_CENTS must be a positive number of cents")
    if _METER_AGGREGATION not in ("count", "sum"):
        raise ValueError(
            f"STRIPE_METER_AGGREGATION is {_METER_AGGREGATION!r}; it must be "
            "'count' or 'sum' -- the two shapes a Stripe Meter can aggregate. "
            "Guessing would mis-bill every call."
        )
    units, remainder = divmod(int(price_cents), _METER_UNIT_CENTS)
    if remainder or units < 1:
        raise ValueError(
            f"{price_cents} cents is not a whole number of "
            f"{_METER_UNIT_CENTS}-cent meter units; refusing to meter an "
            "amount that would not reconcile with the attached Price"
        )

    def _send(value: int) -> None:
        stripe.billing.MeterEvent.create(
            event_name=_METER_EVENT_NAME,
            # A fresh identifier per event: Stripe dedupes on it, so reusing
            # one across the N events of a single bundle would collapse them
            # into one unit and undercharge by 90%.
            payload={"value": str(value), "stripe_customer_id": customer_id},
            identifier=str(uuid.uuid4()),
        )

    if _METER_AGGREGATION == "sum":
        _send(units)
        return
    for _ in range(units):
        _send(1)


def issue_prepaid_key(credit_cents: int) -> str:
    """Mint an API key carrying a prepaid balance, with no account behind it.

    This is what makes the MPP `stripe` rail usable at all here. Stripe
    requires a minimum 0.50 USD charge for a card payment made with a Shared
    Payment Token, and every route on this service is $0.05-$0.15 -- so a
    per-call SPT charge is rejected by Stripe on amount alone, and no amount
    of correct protocol work changes that. The rail can only settle if what it
    sells is a BLOCK, not a call.

    So an agent pays once, above the floor, and receives a key with the
    balance it just bought. No email, no checkout, no browser, no
    subscription: the key IS the receipt, returned in the response to the
    call that paid for it. That keeps the A2A contract intact -- a machine
    arrives, pays, and leaves with something it can spend -- while satisfying
    a floor that exists on Stripe's side and not ours.

    `customer_id` is None deliberately: there is no Stripe Customer, nothing
    to invoice, and no metering. The balance in Firestore is the whole record,
    and it can only go down.
    """
    if credit_cents <= 0:
        raise ValueError("a prepaid key must be issued with a positive balance")
    api_key = secrets.token_urlsafe(32)
    _firestore().collection("api_keys").document(api_key).set(
        {
            "customer_id": None,
            "active": True,
            "plan": None,
            "prepaid_balance_cents": int(credit_cents),
        }
    )
    return api_key


def spend_prepaid(api_key: str, cents: int) -> bool:
    """Draw `cents` off a prepaid key. True if it was spent, False otherwise.

    Transactional, because two concurrent calls on the same key must not both
    read the same balance and both succeed -- that is free audits at exactly
    the moment a caller is fanning out, which is when it would be worth doing.

    Fails CLOSED, unlike check_and_increment_quota above. That asymmetry is
    deliberate: a subscriber's monthly cap is a business limit, so a Firestore
    hiccup there should not cut off someone who has already paid for the
    month. A prepaid balance is the payment itself, so an error here means we
    do not know whether there is money left, and serving on "don't know" is
    serving for free.
    """
    if cents <= 0:
        return False

    try:
        ref = _firestore().collection("api_keys").document(api_key)

        def _debit(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            record = snapshot.to_dict()
            if not record.get("active"):
                return False
            balance = record.get("prepaid_balance_cents")
            if balance is None or balance < cents:
                return False
            transaction.update(ref, {"prepaid_balance_cents": balance - cents})
            return True

        return bool(_run_transactional(_debit))
    except Exception:
        _warn_key_store_unavailable_for_prepaid()
        return False


def refund_prepaid(api_key: str, cents: int) -> bool:
    """Put `cents` back on a prepaid key whose audit failed to run.

    The debit is taken at authentication, before the audit, so a key with no
    balance is refused before a browser is spent on it. The other half of
    that ordering is this: an audit that then fails must hand the cents back,
    or "you are charged only for an audit that produced a result" is true for
    x402 payers and false for prepaid ones. Transactional for the same reason
    the debit is. Never raises; a refund that could not be made is logged at
    ERROR, because it is money the caller is owed.
    """
    if cents <= 0:
        return False
    try:
        ref = _firestore().collection("api_keys").document(api_key)

        def _credit(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            balance = snapshot.to_dict().get("prepaid_balance_cents")
            if balance is None:
                return False
            transaction.update(ref, {"prepaid_balance_cents": int(balance) + int(cents)})
            return True

        refunded = bool(_run_transactional(_credit))
    except Exception:
        refunded = False
    if not refunded:
        logging.getLogger(__name__).error(
            "could not refund %d cents to a prepaid key after a failed audit; "
            "the caller is owed it.",
            cents,
        )
    return refunded


_prepaid_store_warned = False


def _warn_prepaid_store_unavailable() -> None:
    global _prepaid_store_warned
    if _prepaid_store_warned:
        return
    _prepaid_store_warned = True
    logging.getLogger(__name__).error(
        "A prepaid balance could not be read or written. Prepaid keys will be "
        "refused until this is fixed -- a caller who has already paid is being "
        "turned away, which is visible to them and must not be silent."
    )


def _warn_key_store_unavailable_for_prepaid() -> None:
    _warn_prepaid_store_unavailable()


def check_and_increment_quota(customer_id: str, plan: Optional[str] = None) -> bool:
    """Returns True and increments the counter if this call is within the
    subscription's included monthly quota; returns False if the customer
    has already used their included scans for this calendar month.

    The cap comes from the plan the customer actually bought
    (monthly_quota_for). A subscriber activated before plans were recorded
    has no plan on their key and falls back to SAAS_MONTHLY_QUOTA.

    Callers should treat False as "the API key alone is no longer
    sufficient" and require x402/MPP payment for this specific call,
    matching the SaaS plan's advertised overage behavior -- Stripe still
    bills every call via the meter either way, this only gates whether the
    bare API key is enough on its own.

    This is a business limit, not a security boundary, so unlike
    authentication elsewhere in this codebase it fails OPEN on error
    (treats the call as within quota) rather than closed: a transient
    Firestore hiccup should not cut off a paying, already-authenticated
    subscriber, and the worst case of failing open here is a small amount
    of temporary under-enforcement, not unauthorized access.
    """
    import datetime

    try:
        period = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        ref = _firestore().collection("quota_usage").document(f"{customer_id}:{period}")

        def _increment(transaction):
            snapshot = ref.get(transaction=transaction)
            count = snapshot.get("count") if snapshot.exists else 0
            if count >= monthly_quota_for(plan):
                return False
            transaction.set(
                ref,
                {"count": count + 1, "period": period, "customer_id": customer_id},
                merge=True,
            )
            return True

        return _run_transactional(_increment)
    except Exception:
        return True


def create_report_checkout(email: str, url: str, success_url: str, cancel_url: str) -> str:
    """One-time Checkout for a single full-bundle report on one URL.

    mode="payment", not "subscription": this buyer is explicitly not
    subscribing. The audited URL rides along in session metadata so the
    report can be produced after payment without asking for it twice.
    """
    if not ONEOFF_REPORT_PRICE_ID:
        raise ValueError("One-off reports are not configured on this deployment")
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email,
        line_items=[{"price": ONEOFF_REPORT_PRICE_ID, "quantity": 1}],
        metadata={"audit_url": url, "kind": "oneoff_report"},
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
    )
    return session.url


def paid_report_request(session_id: str) -> Optional[dict]:
    """Return {"url": ...} if this session is a genuinely PAID one-off report.

    Verified against Stripe on every call rather than trusting a webhook
    having landed, so a report can never be produced for an unpaid session --
    the report URL is the only thing standing between a stranger and a free
    audit. Returns None for anything unpaid, unknown, or not a report order.
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return None
    if session.get("payment_status") != "paid":
        return None
    metadata = session.get("metadata") or {}
    if metadata.get("kind") != "oneoff_report":
        return None
    url = metadata.get("audit_url")
    return {"url": url} if url else None


def load_report(session_id: str) -> Optional[dict]:
    """Previously generated report, if any -- so a refresh doesn't re-run
    (and re-pay for) an audit the buyer already purchased."""
    try:
        doc = _firestore().collection("reports").document(session_id).get()
    except Exception:
        return None
    return doc.to_dict() if doc.exists else None


def save_report(session_id: str, url: str, result: dict) -> None:
    import time

    try:
        _firestore().collection("reports").document(session_id).set(
            {"url": url, "result": result, "created_at": time.time()}
        )
    except Exception:
        # Storage is a convenience for re-viewing. The buyer already has
        # their report rendered in the response; losing the cache must not
        # fail the purchase they just completed.
        pass
