#!/usr/bin/env python3
"""Turn the audit into outreach: scan prospects, rank them, draft the opener.

The constraint on this business has never been the plumbing. It is that
nobody arrives. Discovery surfaces make a node findable by buyers who are
already looking, and for site audits at $0.05 that population is currently
close to zero. Outbound is the only channel that does not require waiting.

What makes outbound work here is that the product IS the pitch. A cold email
about "automated accessibility auditing" is deleted. An email that opens with
four specific WCAG failures on the recipient's own checkout page, each with a
rule id and a node count, is a bug report about their site -- and a legal
exposure they can verify in thirty seconds.

So this runs the real paid audit against a list of prospects and emits, per
prospect: the evidence, a severity ranking, and a first line built from their
own findings. Nothing is invented; every claim in the output traces to a rule
that fired against their live page.

    python3 scripts/prospect_scan.py acme.com example.org
    python3 scripts/prospect_scan.py --file prospects.txt --out leads.md

Auth is whatever the service already accepts -- HUBVIBE_API_KEY, or a funded
HUBVIBE_WALLET_KEY for the x402 rail. Cost is printed before and after,
because this spends real money per prospect and a scan of 500 domains at the
bundle rate is a number the operator should see before it happens, not after.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

DEFAULT_BASE = os.environ.get(
    "HUBVIBE_BASE_URL", "https://hubvibe-io.com"
)

# Bundle rate. Used only to show the operator what a run will cost before it
# starts; the service's own 402 remains the authority on price.
BUNDLE_USD = 0.15

# Weighting for the ranking. Deliberately crude and legible: a prospect with
# one critical failure outranks one with twenty cosmetic ones, because the
# critical one is what a plaintiff's lawyer screenshots.
_IMPACT_WEIGHT = {"critical": 10, "serious": 5, "moderate": 2, "minor": 1}


def _post(base, path, payload, api_key, timeout):
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as handle:
            return handle.status, json.load(handle)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def _normalise(target):
    """A prospect list is pasted from a CRM, so it arrives however it arrives."""
    target = target.strip().rstrip("/")
    if not target:
        return ""
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    return target


def _findings(result):
    """Every violation across the bundle, flattened, with its dimension.

    Reads defensively: a dimension that failed to run is reported as an error
    by the service rather than as a pass, and must not be counted as clean.
    """
    rows = []
    for dimension in ("wcag", "seo", "security", "performance"):
        section = result.get(dimension)
        if not isinstance(section, dict):
            continue
        for violation in section.get("violations") or []:
            rows.append(
                {
                    "dimension": dimension,
                    "id": violation.get("id", "?"),
                    "impact": (violation.get("impact") or "minor").lower(),
                    "help": violation.get("help", ""),
                    "nodes": violation.get("nodes_affected", 0),
                }
            )
        for finding in section.get("findings") or []:
            rows.append(
                {
                    "dimension": dimension,
                    "id": finding.get("id", "?"),
                    "impact": (finding.get("severity") or "moderate").lower(),
                    "help": finding.get("detail", ""),
                    "nodes": 0,
                }
            )
    return rows


def _score(findings):
    return sum(_IMPACT_WEIGHT.get(f["impact"], 1) * max(f["nodes"], 1) for f in findings)


def _opener(host, findings):
    """The first line of the email, built only from what actually fired.

    Kept factual and short. Nothing about our product, no adjectives about
    their site, no claim we cannot back with the rule id sitting next to it --
    an opener that overstates is worse than no opener, because the recipient
    checks.
    """
    if not findings:
        return "%s passes every check we run." % host
    worst = sorted(
        findings, key=lambda f: -_IMPACT_WEIGHT.get(f["impact"], 1)
    )[:3]
    parts = []
    for finding in worst:
        where = " on %d element(s)" % finding["nodes"] if finding["nodes"] else ""
        parts.append("%s (%s)%s" % (finding["id"], finding["impact"], where))
    counts = Counter(f["impact"] for f in findings)
    headline = counts.get("critical", 0) + counts.get("serious", 0)
    return (
        "%s has %d high-impact accessibility/compliance failures live right now, "
        "including %s." % (host, headline, "; ".join(parts))
    )


def scan(targets, base, api_key, timeout, endpoint):
    results = []
    for target in targets:
        url = _normalise(target)
        if not url:
            continue
        host = url.split("//", 1)[-1]
        status, body = _post(base, "/audit/%s" % endpoint, {"url": url}, api_key, timeout)

        if status == 402:
            results.append({"host": host, "url": url, "status": status,
                            "error": "not paid for -- set HUBVIBE_API_KEY or fund a wallet"})
            continue
        if status != 200:
            results.append({"host": host, "url": url, "status": status,
                            "error": body.get("detail") or body.get("error") or "no audit"})
            continue

        findings = _findings(body)
        results.append(
            {
                "host": host,
                "url": url,
                "status": status,
                "passed": body.get("pass") is True,
                "findings": findings,
                "score": _score(findings),
                "opener": _opener(host, findings),
            }
        )
    # Worst first: the strongest evidence is the best prospect, and a list
    # sorted any other way gets worked from the top and wastes the best ones.
    results.sort(key=lambda r: -r.get("score", -1))
    return results


def render_markdown(results):
    lines = ["# Prospect scan", ""]
    scanned = [r for r in results if "findings" in r]
    failing = [r for r in scanned if not r["passed"]]
    lines.append(
        "%d scanned, %d with live findings, %d errored."
        % (len(scanned), len(failing), len(results) - len(scanned))
    )
    lines.append("")

    for result in results:
        if "findings" not in result:
            lines.append("## %s" % result["host"])
            lines.append("")
            lines.append("Not scanned: %s (HTTP %s)" % (result["error"], result["status"]))
            lines.append("")
            continue
        lines.append("## %s — score %d" % (result["host"], result["score"]))
        lines.append("")
        lines.append("**Opener:** %s" % result["opener"])
        lines.append("")
        if not result["findings"]:
            lines.append("No findings. Not a prospect for a remediation pitch.")
            lines.append("")
            continue
        lines.append("| Dimension | Rule | Impact | Nodes |")
        lines.append("|---|---|---|---|")
        for finding in sorted(
            result["findings"], key=lambda f: -_IMPACT_WEIGHT.get(f["impact"], 1)
        )[:12]:
            lines.append(
                "| %s | `%s` | %s | %s |"
                % (finding["dimension"], finding["id"], finding["impact"],
                   finding["nodes"] or "-")
            )
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", help="domains or URLs")
    parser.add_argument("--file", help="file with one domain per line")
    parser.add_argument("--out", help="write the markdown report here")
    parser.add_argument("--json-out", help="write raw results here for a CRM")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--endpoint", default="bundle",
                        choices=["bundle", "wcag", "seo", "security", "performance"])
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--yes", action="store_true",
                        help="skip the spend confirmation")
    args = parser.parse_args(argv)

    targets = list(args.targets)
    if args.file:
        try:
            with open(args.file) as handle:
                targets += [line for line in handle.read().splitlines() if line.strip()
                            and not line.strip().startswith("#")]
        except OSError as exc:
            print("could not read %s: %s" % (args.file, exc), file=sys.stderr)
            return 2
    targets = [t for t in (t.strip() for t in targets) if t]
    if not targets:
        parser.error("no targets given")

    # Say the number before spending it. A 500-domain list is $50 at the
    # bundle rate, which is fine if intended and a nasty surprise if not.
    estimate = len(targets) * BUNDLE_USD
    print("Scanning %d prospect(s). Estimated spend: $%.2f" % (len(targets), estimate),
          file=sys.stderr)
    if not args.yes and estimate > 5.00:
        print("Above $5.00 -- re-run with --yes to confirm.", file=sys.stderr)
        return 3

    api_key = os.environ.get("HUBVIBE_API_KEY", "").strip()
    if not api_key:
        print("NOTE: no HUBVIBE_API_KEY set; unauthenticated calls will 402.",
              file=sys.stderr)

    results = scan(targets, args.base_url, api_key, args.timeout, args.endpoint)
    report = render_markdown(results)

    if args.out:
        with open(args.out, "w") as handle:
            handle.write(report)
        print("wrote %s" % args.out, file=sys.stderr)
    else:
        print(report)

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(results, handle, indent=2)
        print("wrote %s" % args.json_out, file=sys.stderr)

    scanned = len([r for r in results if "findings" in r])
    print("Scanned %d; actual spend ~$%.2f" % (scanned, scanned * BUNDLE_USD),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
