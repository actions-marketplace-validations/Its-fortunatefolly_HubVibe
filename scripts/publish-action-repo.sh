#!/usr/bin/env bash
#
# Materialize the standalone repo that GitHub Marketplace requires, from the
# action.yml in this monorepo.
#
# Why this exists, so nobody rediscovers it the hard way:
#
#   GitHub's Marketplace publishing rules require that the repo contain a
#   single action.yml AT ITS ROOT, and that the repo contain NO WORKFLOW
#   FILES AT ALL. HubVibe has .github/workflows/python-app.yml (its CI), so
#   this repo can never itself be published to Marketplace, no matter where
#   action.yml sits.
#
#   Note the two are separate things:
#     * Direct use  -- `uses: Its-fortunatefolly/HubVibe@v1` already works
#                      today off the root action.yml. No Marketplace needed.
#     * Marketplace -- listing/discoverability only, and it needs the clean
#                      standalone repo this script builds.
#
# The monorepo's action.yml stays the single source of truth. This script
# copies it out rather than letting a second hand-maintained copy exist,
# because the copy that drifts is always the one people are running.
#
# Usage:
#   bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action
#
# Then follow the printed steps to push and tag.

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: bash scripts/publish-action-repo.sh <target-directory>" >&2
  exit 64
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "error: $TARGET exists and is not empty. Refusing to overwrite." >&2
  exit 1
fi

mkdir -p "$TARGET/scripts"
cp "$REPO_ROOT/action.yml" "$TARGET/action.yml"
cp "$REPO_ROOT/scripts/render_audit_summary.py" "$TARGET/scripts/render_audit_summary.py"
# action.yml calls this by $GITHUB_ACTION_PATH, so a published copy without it
# turns the wallet path into "No such file or directory" at run time -- on the
# one code path that spends money.
cp "$REPO_ROOT/scripts/x402_pay.py" "$TARGET/scripts/x402_pay.py"

cat > "$TARGET/README.md" <<'MARKDOWN'
# HubVibe WCAG SEO and Security Audit

**Catch accessibility, SEO, security-header and performance regressions in the pull request that caused them — not in an audit six months later.**

Runs against your deployed preview or staging URL and fails the build if it regresses. Every check is a deterministic rule against the real rendered page. No LLM decides whether your site is compliant.

```yaml
- uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://staging.example.com
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}
```

That's the whole integration. `wallet-key` is an EVM private key funded with a
little USDC on Base: the step reads the service's payment challenge and pays
for its own run, so there is no account to create and nothing to provision
first. `max-price-usd` (default `0.15`) is a hard ceiling it refuses to sign
above.

## The whole file, if you'd rather paste one

Save as `.github/workflows/hubvibe-audit.yml`, edit the one URL, add
`HUBVIBE_WALLET_KEY` to your repository secrets. Every push to `main` and every
pull request is audited from then on.

```yaml
name: HubVibe Site Compliance Audit

on:
  push:
    branches: [main]
  pull_request:

env:
  # The one line to edit. Your deployed preview, staging, or production URL.
  AUDIT_URL: https://your-site.example.com

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: Its-fortunatefolly/hubvibe-audit-action@v1
        with:
          url: ${{ env.AUDIT_URL }}
          wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}
          max-price-usd: "0.15"    # hard ceiling per call
          endpoint: bundle          # or: wcag, seo, security, performance
          fail-on-violation: true   # your regressions gate the build
          fail-on-error: false      # our outage does not
```

## What it checks

| | |
|---|---|
| **Accessibility** | WCAG 2.1 A/AA via axe-core, against the rendered DOM — constrained to exactly those rule tags, so a pass means what it says |
| **SEO** | title, meta description, H1 structure, canonical, OpenGraph, structured data, `lang` |
| **Security headers** | HTTPS, HSTS, CSP, X-Content-Type-Options, clickjacking, Referrer-Policy, CORS |
| **Performance** | DOM node count, transferred bytes, request count from one real page load |

## Findings land in the job summary

Not buried in log output. A reviewer opens the Checks tab and sees the rule, its impact, how many nodes it hit, and a link to the fix:

| Rule | Impact | Nodes | Help |
|---|---|---|---|
| `color-contrast` | serious | 4 | [Elements must have sufficient contrast](https://dequeuniversity.com/rules/axe/color-contrast) |
| `image-alt` | critical | 2 | [Images must have alternate text](https://dequeuniversity.com/rules/axe/image-alt) |

## Three things it will not do to you

**It won't lie about a check that didn't run.** If a dimension fails to execute, the call fails and reports an error. It is never counted as a pass. A green build means the checks actually ran.

**It won't block your deploy when our service has a bad day.** Set `fail-on-error: false` and infrastructure failures — network, timeout, our outage — become a warning. Real findings still gate the build:

```yaml
- uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://staging.example.com
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}
    fail-on-error: false      # our outage never blocks your release
    fail-on-violation: true   # your regressions still do
```

**It won't guess.** axe-core for accessibility, a real HTTP response for headers, a real browser page load for performance. Deterministic rules, same input same output, no model in the loop deciding whether your site "looks compliant."

## What it costs

**$0.05** per single audit. **$0.15** for all four as one bundle.

Concretely: a repo merging 100 pull requests a month, running the full bundle on each, spends **$10/month**. Running only the accessibility check, **$3/month**. No subscription, no seat licence, no minimum — you are billed for calls you make.

You can also pay per call over HTTP 402 with no account at all, which is what the API is really built for. See [`/.well-known/agent.json`](https://hubvibe-io.com/.well-known/agent.json) — it lists live prices and the payment rails that can actually settle right now, and it is generated from the same catalog the routes charge from, so it cannot drift from what you are billed.

## Try it before wiring it up

An unauthenticated call tells you the price and how to pay — no signup:

```bash
curl -i -X POST https://hubvibe-io.com/audit/wcag \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

## Paying per run, without an account

If the deployment you point at advertises a machine-payment rail — check
[`/.well-known/agent.json`](https://hubvibe-io.com/.well-known/agent.json),
which lists only rails that can actually settle right now — this action can pay
its own way per call from a funded wallet, with no signup and no checkout:

```yaml
- uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://your-deploy-preview.example
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}
```

Fund it with what you intend to spend and no more — a float, not a treasury.
`max-price-usd` (default `0.15`) is a hard per-call ceiling: the run refuses to
sign above it, so a misquoted price cannot drain the wallet. If no such rail is
advertised, the run reports the payment challenge instead of spending anything.

Prefer a prepaid key? Set `api-key` and it takes precedence — you are never
charged twice.

## Inputs

| Input | Required | Default | |
|---|---|---|---|
| `url` | yes | — | The live URL to audit. |
| `api-key` | no | — | Your key. Without it the run reports the service's 402 and how to pay. |
| `wallet-key` | no | — | EVM private key (repo secret), funded, used to settle the payment challenge per call when the deployment advertises a crypto rail. |
| `max-price-usd` | no | `0.15` | Hard ceiling on what one call may spend with `wallet-key`. Above it the run refuses to sign. |
| `endpoint` | no | `bundle` | `wcag`, `seo`, `security`, `performance`, or `bundle`. |
| `fail-on-violation` | no | `true` | `false` reports findings without failing the build. |
| `fail-on-error` | no | `true` | `false` makes infrastructure failures a warning. |
| `timeout-seconds` | no | `90` | Per-attempt HTTP timeout. |
| `retries` | no | `2` | Retries on network error or 5xx. A 4xx is never retried. |
| `base-url` | no | hosted | Override for self-hosted deployments. |

## Outputs

| Output | |
|---|---|
| `passed` | `"true"` / `"false"` — the overall result. |
| `response` | Raw JSON response body. |
| `http-status` | Status of the final attempt (`000` if it never completed). |

## Gate a deploy on it

```yaml
- name: Audit staging
  id: audit
  uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://staging.example.com
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}

- name: Promote to production
  if: steps.audit.outputs.passed == 'true'
  run: ./deploy-production.sh
```

## Getting a key

Fund a wallet address with USDC on Base and store its private key as the
repository secret `HUBVIBE_WALLET_KEY`. No ETH is needed — the payment is
signed off-chain and the facilitator pays the gas. If you already hold a
prepaid API key, set `api-key` instead.

If you would rather not hold a key at all, the endpoints accept per-call machine payment over HTTP 402 — nothing to store, nothing to rotate.

## Source

Developed in the [HubVibe monorepo](https://github.com/Its-fortunatefolly/HubVibe). This repository is generated from it, because GitHub requires an action repository to contain no workflow files.
MARKDOWN

# Guard the one rule that silently disqualifies the listing.
if find "$TARGET" -path '*/.github/workflows/*' -print -quit | grep -q .; then
  echo "error: generated repo contains workflow files; Marketplace will reject it." >&2
  exit 1
fi

cat <<EOF

Generated the Marketplace repo at: $TARGET

Contents (no workflow files, single action.yml at root):
$(cd "$TARGET" && find . -type f | sort | sed 's/^/  /')

Next steps:

  cd $TARGET
  git init -b main
  git add .
  git commit -m "HubVibe WCAG SEO and Security Audit action"
  git remote add origin https://github.com/Its-fortunatefolly/hubvibe-audit-action.git
  git push -u origin main

  # Tag the release. v1 is the moving major-version tag consumers pin to.
  git tag -a v1.0.0 -m "v1.0.0"
  git tag -f -a v1 -m "v1"
  git push origin v1.0.0
  git push -f origin v1

Then, on github.com: Releases -> Draft a new release -> choose tag v1.0.0 ->
tick "Publish this Action to the GitHub Marketplace" -> accept the agreement
-> Publish release.

The Marketplace name must be globally unique. "HubVibe WCAG SEO and Security Audit"
is set in action.yml; if the publish flow reports it as taken, change the
name there and re-run this script.
EOF
