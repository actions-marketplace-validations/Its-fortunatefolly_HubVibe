# HubVibe Site Compliance Auditing Suite

A metered A2A service with five real, rule-based audit endpoints:
**accessibility** (WCAG 2.1 A/AA via
[axe-core](https://github.com/dequelabs/axe-core)/
[axe-playwright-python](https://pypi.org/project/axe-playwright-python/)),
**SEO** (title/meta/OpenGraph/structured-data), **security** (HTTPS/HSTS/
CSP/CORS response headers), **performance** (DOM size/payload weight/request
count from a real page load), and a **bundle** that runs all four in one
call. An optional AI layer (Gemini) can generate plain-language remediation
notes on the accessibility results, but it never decides pass/fail — that's
always the deterministic rule engine for each check (see `app/audits.py`
for SEO/security/performance).

## Why this exists

An earlier draft of this service used an LLM as the sole auditor and
silently returned `{"pass": true}` whenever anything errored, on the theory
that a paying customer's CI/CD pipeline should never see a failure. That
design was rejected: a compliance-shaped API that fabricates a "pass" on
error isn't fail-safe, it's a false-positive generator — anyone relying on
it (including for legal accessibility obligations) would have no way to
tell "verified compliant" from "the check never ran." This version instead:

- **Runs a real, deterministic rule engine** (axe-core) instead of asking an
  LLM to guess at conformance.
- **Never coerces an error into a pass.** If the audit can't run, `/audit`
  returns HTTP 502 with `"pass": null`, not `"pass": true`.
- **Requires an API key, a verified x402 payment, or a verified MPP
  payment** (`X-API-Key`, checked with constant-time comparison; `X-PAYMENT`,
  verified and settled against an x402 facilitator; or `Authorization:
  Payment ...`, verified directly against Stripe or the Tempo network) and
  rate-limits per key, since this is a metered endpoint sitting in front of
  a paid LLM call — an open, unauthenticated endpoint is a wallet-drain
  vector, not just a security gap. None present, or all invalid, always
  gets HTTP 402 with everything needed to pay — never a fallback pass.
- **Keeps secrets out of the deploy command.** `GEMINI_API_KEY` and
  `AUDIT_API_KEY` are meant to be provisioned via Secret Manager, not
  `--set-env-vars` (which lands in shell history and Cloud Build logs).

## API

Five paid audit routes, all with identical auth (see below) and identical
honest-failure behavior (502 + `"pass": null` if the check itself couldn't
run, never billed):

| Route | Input | Price | Checks |
|---|---|---|---|
| `POST /audit` | `html` or `url` | $0.05 | Alias of `/audit/wcag`, kept for compatibility |
| `POST /audit/wcag` | `html` or `url` | $0.05 | WCAG 2.1 A/AA via axe-core |
| `POST /audit/seo` | `html` or `url` | $0.05 | Title, meta description, H1s, canonical, OpenGraph, structured data, lang |
| `POST /audit/security` | `url` (required) | $0.05 | HTTPS, HSTS, CSP, X-Content-Type-Options, frame protection, Referrer-Policy, CORS |
| `POST /audit/performance` | `url` (required) | $0.05 | DOM node count, transferred bytes, request count from one real page load |
| `POST /audit/bundle` | `url` (required) | $0.15 | All four above, atomically -- if any fails, the whole call fails and nothing is billed |

`security`/`performance`/`bundle` need a live, fetchable URL (they inspect a
real HTTP response / real browser load) -- raw HTML alone isn't enough for
those three, unlike `wcag`/`seo`.

Headers on every route: `X-API-Key: <key>` **or**
`X-PAYMENT: <x402 signed payment>` **or**
`Authorization: Payment <base64url MPP credential>`

If none of those is present or valid, the response is HTTP 402 with both:
- the x402-style JSON body (for callers reading price/payTo out of the body)
- one `WWW-Authenticate: Payment ...` header per configured MPP method (the
  spec-conformant challenge -- see mppx validate below), each carrying its
  own base64url-encoded `request` with method-specific price/recipient/etc.

```json
{
  "x402Version": 1,
  "scheme": "exact",
  "network": "eip155:8453",
  "price": "$0.05",
  "payTo": "0x...",
  "accepted_payment_header": "X-PAYMENT",
  "alternative": "X-API-Key header (Stripe-based billing) is also accepted"
}
```

```json
{
  "status": "ok",
  "pass": false,
  "engine": "axe-core",
  "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
  "violations": [
    {"id": "image-alt", "impact": "critical", "help": "Images must have alternate text", "help_url": "...", "nodes_affected": 2}
  ],
  "remediation": {"ai_generated": true, "notes": "..."}
}
```

On audit failure (browser crash, invalid input, timeout): HTTP 502,
`{"status": "error", "pass": null, "detail": "..."}`. Failed audits are
never billed.

## The landing page is not a product surface

`/` serves an about-page (`app/static/index.html`), not the health check
(that lives at `/health`; Cloud Run's frontend reserves `/healthz`). It
says what HubVibe is and points at the machine surfaces; it sells nothing,
carries no form, and never prints the per-call rate. HubVibe is software
to software: the buyer is an agent or a pipeline, the product is the HTTP
402 path and the discovery surfaces that lead machines to it.

There is deliberately **no free scan**. An audit costs a real browser page
load, so giving them away funds strangers' compute at our expense and
invites abuse.

## Getting paid

Per call is the only price ($0.05 per audit, $0.15 for the bundle), paid
by the calling software on the request itself. No account, no signup, no
subscription, no human step:

- **x402** (`X-PAYMENT`): USDC on Base, settled by the facilitator into
  the self-custody pay-to wallet. The primary rail; see below.
- **MPP** (`Authorization: Payment ...`): a machine credential verified
  against Stripe or the Tempo network, where those are configured. The
  MPP top-up variant sells a small prepaid block to the calling machine on
  that same request and returns an `api_key` holding the remainder, which
  the machine spends per call as `X-API-Key`. Nothing is bought on a page.

Every call that Stripe bills still reports a Stripe Meter Event
(`billing.record_usage`), priced in meter units: a $0.05 audit is 5 units
of a $0.01 Price, a $0.15 bundle is 15, so `STRIPE_METERED_PRICE_ID`,
`STRIPE_METER_UNIT_CENTS` (default `1`) and `STRIPE_METER_AGGREGATION`
(`count` or `sum`, default `count`) must describe the same Price or every
invoice is wrong uniformly and invisibly. The key store (Firestore on
Cloud Run, SQLite on the box) holds the `api_key -> record` mapping and
prepaid balances.

Legacy, kept only so keys issued before 2026-09-06 keep working: the
human plan tiers are retired, `/billing/checkout` refuses a plan,
`STRIPE_PRICE_PRO` / `STRIPE_PRICE_AGENCY` / `STRIPE_PRICE_ONEOFF_REPORT`
are ignored, and `QUOTA_PRO`, `QUOTA_AGENCY` and `SAAS_MONTHLY_QUOTA`
only govern those old keys. The Stripe webhook at `/billing/webhook`
(`checkout.session.completed`) exists for the same reason. Do not build
on any of it.

`AUDIT_API_KEY` is an internal smoke-test key that bypasses billing
entirely; leave it unset in production.

### Getting paid without Stripe: x402

`/audit` also accepts a per-request x402 payment (`X-PAYMENT` header) as an
alternative to a Stripe-issued API key — for AI agents that can pay
on-the-fly without a human first setting up billing. Verification and
settlement are delegated entirely to a facilitator via the official
[`x402`](https://pypi.org/project/x402/) package
(`wcag-audit-engine/app/x402_payments.py`); this service never hand-rolls
signature checking, and fails closed (rejects with 402) on any missing
config, malformed header, or facilitator error.

Until these are set, `x402_payments.is_configured()` is `False` and every
`X-PAYMENT` header is rejected — the Stripe `X-API-Key` path is unaffected
either way:

- `X402_FACILITATOR_URL` — the facilitator's base URL.
- `X402_PAY_TO_ADDRESS` — the wallet address that receives payment.
- `X402_NETWORK` — CAIP-2 network id (default `eip155:8453`, Base mainnet).
- `X402_PRICE` — default `$0.05`.
- `X402_FACILITATOR_AUTH_HEADERS` — JSON object of headers sent on every
  facilitator call, e.g. `{"Authorization": "Bearer ..."}`. Optional, but in
  practice required: the free public facilitator at `x402.org` is
  **testnet-only** (Base Sepolia), and anything settling real money on
  mainnet authenticates the resource server. A malformed value raises at
  startup rather than being ignored, because silently dropping credentials
  leaves x402 advertised while the facilitator rejects every payment —
  indistinguishable from nobody buying.

#### Choosing a facilitator

The default is `https://facilitator.payai.network`: keyless, Base mainnet
(x402 v1 and v2; its `/supported` lists both `eip155:8453` and the legacy
name `base`), and it indexes a resource in its `/discovery/resources` on
the first settled payment, which is the only path into capability-based
discovery. `facilitator.xpay.sh` also settles on Base mainnet but keeps no
index; `x402.dexter.cash` indexes but its settlement signer can run dry,
and a facilitator that cannot settle sells nothing — read the signer's gas
before trusting one. Coinbase's facilitator
(`https://api.cdp.coinbase.com/platform/v2/x402`) is the one behind the
x402 Bazaar that the official SDKs' discovery reads by default; set
`CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` and the server signs CDP's
per-request JWTs itself, and only ever sends them to a Coinbase host.

A second rail, USDC on Solana mainnet, is advertised in the v2 challenge
when `X402_SOLANA_PAY_TO_ADDRESS` is a Solana public key you hold and the
facilitator lists Solana with a fee payer; a payer's payload is verified
and settled against the rail it chose.
`scripts/probe-facilitators.sh` checks any candidate for the two things
this server library needs: the CAIP-2 network name in `/supported`, and
whether it serves a Bazaar index; `scripts/switch-facilitator.sh` changes
it on a running box and rolls back if the rail vanishes. A facilitator that
authenticates the resource server with a fixed bearer token is covered by
`X402_FACILITATOR_AUTH_HEADERS`; one that signs a fresh credential per
request is not supported.

Turning it on, without disturbing anything else on the service:

```bash
gcloud run services update hubvibe --region=us-south1 --update-env-vars=\
X402_FACILITATOR_URL=https://your-facilitator.example,\
X402_PAY_TO_ADDRESS=0xYourWallet
```

`--update-env-vars` merges; `--set-env-vars` would replace the whole block
and silently unset every other payment variable.

#### Why this is also the discovery switch

x402 is not only a rail — it is how agents *find* this service. Facilitators
catalog x402 resources by reading a
[Bazaar](https://pypi.org/project/x402/) discovery extension off their 402
responses, and agents shop that index by capability. Both the REST routes and
each paid MCP tool emit that extension, describing their real input schema
(the same one the MCP tools advertise, so the index can never disagree with
what the route accepts) and, for tools, the tool name and transport.

It is gated on the same `is_configured()` as everything else: with no
facilitator there is nothing to be indexed *by*, and publishing discovery
data for a resource that cannot take payment would advertise a sale this
node cannot complete. So while x402 is off, agents can only reach this
service through the MCP registry — by name, never by capability.

Building the discovery data can never break a payment challenge: if it
throws, the 402 still goes out with its price and rails intact. Losing the
index is survivable; losing the sale is not.

#### Paying over MCP

`/mcp` speaks the x402 MCP protocol (`x402.mcp` in the library), which is
not the HTTP 402 shape. An unpaid `tools/call` answers with an `isError`
result whose `structuredContent` (and text) is the **v2** `PaymentRequired`
-- the same challenge the HTTP path encodes into `PAYMENT-REQUIRED`, built
by the same function, with `resource.url` naming `/mcp` and a Bazaar record
that names the tool and its transport. The client signs for `accepts[0]`
and retries with the `PaymentPayload` in `params._meta["x402/payment"]`
(the official `x402.mcp` client does this automatically); the facilitator's
settle response comes back in the result's `_meta["x402/payment-response"]`,
beside the `PAYMENT-RESPONSE` header. The `_meta` payload is re-encoded into
the header form and verified by the same path as an HTTP payment, so the
replay guard, the facilitator loop and the logging are shared rather than
duplicated. An explicit `X-PAYMENT` / `PAYMENT-SIGNATURE` header on the POST
still works and wins when both are present.

### Getting paid without Stripe subscriptions: MPP

`/audit` also accepts [MPP](https://docs.stripe.com/payments/machine/mpp)
(Machine Payments Protocol -- an open standard co-authored by Stripe and
Tempo) via `Authorization: Payment <credential>`. Unlike x402 (crypto-only,
Coinbase-authored) or the Stripe subscription flow above (human sets up
billing once, in advance), MPP covers **both** fiat and crypto per-request,
with no advance setup on the payer's side:

- **stripe** method: a single-use Stripe Shared Payment Token (`spt_...`).
  The server creates and confirms a PaymentIntent with
  `shared_payment_granted_token=<spt>` -- Stripe enforces single-use on the
  token itself.
- **tempo** method: USDC on the Tempo network, "push" mode only -- the
  caller broadcasts their own signed transfer and hands us the tx hash; the
  server fetches the receipt and checks the `Transfer` event log matches the
  challenge's amount/recipient/token. (Pull mode and the zero-amount
  EIP-712 "proof" credential type aren't implemented -- both fail closed.)

There's no official Python SDK for MPP (only the Node `mppx` package), so
`wcag-audit-engine/app/mpp_payments.py` hand-implements the wire protocol
directly against the published spec
([tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs)): the
challenge/response headers, the HMAC-based stateless challenge binding
(derived from `STRIPE_SECRET_KEY`, so no separate signing key to manage),
and the per-method request/payload shapes. Validate any running instance
against the reference implementation:

```bash
npx mppx@latest validate http://localhost:8000 \
  --endpoint "POST:/audit" \
  --header "Content-Type:application/json" \
  --body '{"url":"https://example.com"}'
```

Until each method's vars are set, it isn't offered (no `WWW-Authenticate`
header on a 402 for that method) and stays inert:

- **stripe** method needs `MPP_STRIPE_NETWORK_PROFILE_ID` (your Stripe
  profile ID, `profile_...` -- Dashboard → "Stripe profile" → Get started,
  in **live** mode; no Product/Price needed, unlike the subscription flow
  above). `MPP_STRIPE_PRICE_CENTS` (default `5`, i.e. $0.05),
  `MPP_STRIPE_CURRENCY` (default `usd`), and `MPP_STRIPE_API_VERSION`
  (default `2026-05-27.preview`) are optional.

  **It carries a floor: Stripe requires a minimum 0.50 USD charge for card
  payments made with a Shared Payment Token.** This rail is therefore not
  offered on any route priced below `MPP_STRIPE_MIN_CENTS` (default `50`) --
  no `WWW-Authenticate` challenge, no `accepts` entry, not listed in
  `payment.methods` -- and a stale challenge under the floor is refused
  before an SPT is spent on it. Configured is not the same as usable: at the
  $0.05/$0.15 machine rates this rail stays dark on purpose, because
  advertising it would take a caller's single-use token and then fail at the
  Stripe API every time. For sub-50c machine payments through Stripe, use
  stablecoins (below) — their minimum is 1 cent — or price a route at 50c+.
- **tempo** method needs only `MPP_TEMPO_RECIPIENT_ADDRESS` -- everything
  else defaults to Tempo mainnet's real values (sourced from Tempo's own
  SDK, not guessed): `MPP_TEMPO_RPC_URL` defaults to
  `https://rpc.tempo.xyz`, `MPP_TEMPO_TOKEN_ADDRESS` defaults to the actual
  mainnet USDC.e contract `0x20C000000000000000000000b9537d11c60E8b50`, and
  `MPP_TEMPO_CHAIN_ID` defaults to `4217`. `MPP_TEMPO_PRICE_BASE_UNITS`
  defaults to `50000` ($0.05 at USDC's 6 decimals).

  **This method is not actually Tempo-specific — it runs on any EVM chain.**
  Verification is `eth_getTransactionReceipt` over JSON-RPC plus standard
  ERC-20 `Transfer` log matching, so only the four values above tie it to a
  chain. To take direct USDC on **Base** into a self-custody wallet, point
  them at Base and the same code verifies Base:

  ```
  MPP_TEMPO_RPC_URL=https://mainnet.base.org
  MPP_TEMPO_CHAIN_ID=8453
  MPP_TEMPO_TOKEN_ADDRESS=<USDC on Base -- verify, see below>
  MPP_TEMPO_RECIPIENT_ADDRESS=<your Base wallet>
  ```

  Verified against the reference implementation: `npx mppx@latest validate`
  against a node configured this way passes every server-side check --
  including `Valid recipient address` and `Valid currency address (mainnet)`,
  with chain 8453 correctly read as mainnet. (Its payment-roundtrip phase
  still fails, because it auto-provisions a *Tempo testnet* wallet to pay
  with; that is the validator's convenience feature not applying to a Base
  mainnet config, not a fault in the server.)

  USDC on Base is `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` — the value
  Stripe's Dashboard assistant gives for this account, matching the one this
  configuration was validated against.

  **Confirm it against the chain anyway before deploying it.** Two documents
  agreeing is not the chain agreeing, and a wrong `MPP_TEMPO_TOKEN_ADDRESS`
  makes `_receipt_matches` reject every real payment -- fail-closed, but
  silently unsellable, which is this repo's most expensive failure mode. USDC
  exposes `symbol()`, selector `0x95d89b41`:

  ```bash
  curl -s https://mainnet.base.org -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"<candidate address>","data":"0x95d89b41"},"latest"]}'
  ```

  The result hex-decodes to `USDC` for the right contract, and to nothing for
  a wrong one.

  **A caveat worth knowing:** the challenge still advertises `method="tempo"`
  while naming chain 8453 in `methodDetails.chainId`. The reference validator
  reads the chain id and accepts it, and any client that reads the challenge
  rather than assuming defaults will too -- but it is an off-label
  configuration. For Base specifically, **x402 is the native rail** and is
  already implemented here; this is the option for callers that would rather
  broadcast their own transfer and hand over a hash.

  The simplest way to get `MPP_TEMPO_RECIPIENT_ADDRESS`: let Stripe custody
  and auto-convert the funds instead of running your own wallet, via
  Stripe's crypto deposit-address API (needs your live `STRIPE_SECRET_KEY`,
  pulled from Secret Manager rather than typed/pasted anywhere):

  ```bash
  STRIPE_SECRET_KEY=$(gcloud secrets versions access latest --secret=stripe-secret-key)
  curl https://api.stripe.com/v1/crypto/deposit_addresses \
    -u "$STRIPE_SECRET_KEY:" \
    -H "Stripe-Version: 2026-05-27.preview" \
    -d network=tempo
  ```

  The response's `address` field is the value to use.
- `MPP_REALM` — optional; the challenge realm defaults to the request's own
  `Host` header (minus port), which is what the spec calls for and is what
  `mppx validate` checks for. Only set this to override that.

## Pipeline integrations

- `integrations/langchain_tool.py` — a LangChain `@tool`-decorated function
  calling `/audit/bundle`; recent CrewAI versions accept LangChain tools
  directly, so this works for both without a separate wrapper. Auth is
  `HUBVIBE_API_KEY` only (x402/MPP payment construction is out of scope for
  a thin tool wrapper -- use the `x402`/`mppx` client libraries directly if
  an agent should pay per-call instead of holding a subscription key).
- `integrations/github_action.yml` — a copy-paste GitHub Actions workflow
  that runs `/audit/bundle` as a CI/CD gate and fails the build on either a
  failed audit or a failed/unauthenticated request.
- `app/static/mcp.json`, served live at `/mcp.json` — tool definitions for
  all five routes in MCP's `{name, description, inputSchema}` shape, with a
  non-standard `httpEndpoint` extension mapping each onto its actual route
  and price, since this is a plain REST API, not a live MCP stdio/SSE
  server. The same schema is also served live at `/.well-known/agent.json`.

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The `mcr.microsoft.com/playwright/python` base image ships Chromium
pre-installed for the container build; for local runs outside that image,
install it once with `python -m playwright install --with-deps chromium`.

## Deployment (Cloud Run)

Create the secrets once:

```bash
echo -n "your-gemini-key"       | gcloud secrets create gemini-api-key        --data-file=-
echo -n "your-audit-key"        | gcloud secrets create audit-api-key         --data-file=-
echo -n "sk_live_..."           | gcloud secrets create stripe-secret-key     --data-file=-
echo -n "whsec_..."             | gcloud secrets create stripe-webhook-secret --data-file=-
```

Grant the Cloud Run service account Firestore access (Meter Events and
Checkout only need the Stripe secret key, not a separate GCP grant):

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Build and deploy. The service name and region below are the ones the live
node actually runs under — `gcloud run deploy` **creates** a service when the
name doesn't match an existing one, so deploying as `wcag-audit-engine` in
`us-central1` would quietly stand up a second, unreferenced copy rather than
update production:

```bash
gcloud run deploy hubvibe \
  --source=wcag-audit-engine \
  --region=us-south1 \
  --memory=2Gi \
  --cpu=2 \
  --concurrency=4 \
  --min-instances=1 \
  --set-env-vars=STRIPE_METERED_PRICE_ID=price_...,STRIPE_METER_EVENT_NAME=wcag_audit_call,X402_FACILITATOR_URL=https://...,X402_PAY_TO_ADDRESS=0x...,X402_NETWORK=eip155:8453,X402_PRICE=\$0.05,MPP_STRIPE_NETWORK_PROFILE_ID=profile_...,MPP_TEMPO_RPC_URL=https://...,MPP_TEMPO_TOKEN_ADDRESS=0x...,MPP_TEMPO_RECIPIENT_ADDRESS=0x... \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,AUDIT_API_KEY=audit-api-key:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest
```

`STRIPE_PRICE_ONEOFF_REPORT`, `STRIPE_PRICE_PRO` and `STRIPE_PRICE_AGENCY`
belong to the retired human plans and can be omitted; nothing offers a
plan and `/billing/checkout` refuses one.

**Redeploying code only:** leave every flag off.

```bash
gcloud run deploy hubvibe --source=wcag-audit-engine --region=us-south1
```

`--set-env-vars` and `--set-secrets` *replace* the service's configuration
rather than adding to it, so passing a partial list on a routine code deploy
silently unsets everything you left out — which, for the payment variables,
takes the paid rails offline. Omitting them keeps the existing config.

### Why those sizing flags

They are not arbitrary, and they have to agree with `MAX_CONCURRENT_AUDITS`
(default 4) or the container will either waste money or die under load:

- `--memory=2Gi` — each concurrent audit pins a Chromium instance from the
  pool in `app/browser_pool.py`. Chromium wants roughly 300–500 MB under a
  real page load, so four concurrent audits plus the Python process does not
  fit in 1 Gi. Under-provisioning here shows up as OOM-killed requests, which
  Cloud Run reports as a 5xx with no useful traceback.
- `--concurrency=4` — matches `MAX_CONCURRENT_AUDITS`. Letting Cloud Run send
  more simultaneous requests than the app will run in parallel just queues
  them inside the container, where they burn the caller's timeout instead of
  being load-balanced onto another instance.
- `--cpu=2` — a headless page load is CPU-bound during parse/layout; one vCPU
  shared across four page loads makes every one of them slow.
- `--min-instances=1` — cold-starting this image means starting Python *and*
  launching Chromium. An agent with a short client timeout gives up before a
  cold instance ever answers, which reads as an unreliable API rather than a
  slow one. One warm instance is the difference between a machine caller
  retrying and a machine caller dropping you.

Scale the whole set together: to serve more parallel audits, raise
`MAX_CONCURRENT_AUDITS`, `--concurrency`, `--memory`, and `--cpu` in step
rather than any one alone.

(Price/event IDs aren't secrets, so `--set-env-vars` is fine for those; the
actual credentials go through `--set-secrets`. `CHECKOUT_SUCCESS_URL` /
`CHECKOUT_CANCEL_URL` default to this same service's own `/billing/success`
and `/billing/cancel` pages — only set them if you're fronting this with a
different public domain. Omit the `X402_*` vars to deploy without x402, and
the `MPP_*` vars to deploy without MPP — the Stripe `X-API-Key` path works
unchanged regardless of either.)

`--allow-unauthenticated` at the Cloud Run/IAM layer is fine here (or
omit it and front the service with your own gateway) — request-level access
control is enforced in the application via `X-API-Key`, which is what
actually meters and gates paid usage. For real production traffic, put a
quota policy (API Gateway or Cloud Armor) in front of this too: the
in-process rate limiter in `app/main.py` is per-instance and won't hold once
Cloud Run scales out to multiple instances.
