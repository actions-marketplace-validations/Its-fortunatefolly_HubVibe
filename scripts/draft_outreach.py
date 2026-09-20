#!/usr/bin/env python3
"""Turn a prospect scan into sendable email, one draft per prospect.

scripts/prospect_scan.py answers "who is broken and how badly". This answers
"what do we send them". It is the second half of the only channel that does
not require waiting for buyers to find us.

The whole edge is that the product is the pitch: a cold email opening with
four specific rule failures on the recipient's own checkout page is a bug
report they can verify in thirty seconds, not marketing. That edge survives
exactly as long as every claim in the draft is true. One inflated number and
the recipient checks their own site, finds we exaggerated, and the sender is
done -- so this refuses to ship a draft it cannot trace back to a finding
that actually fired.

    python3 scripts/prospect_scan.py --file prospects.txt --json-out leads.json
    python3 scripts/draft_outreach.py --leads leads.json --out drafts.md

Needs `pip install anthropic` and ANTHROPIC_API_KEY. The model is a flag,
because the choice is a budget decision and belongs to the operator, not to
this script: --model claude-haiku-4-5 --batch is roughly twenty times cheaper
per draft than the default, and the run prints the number before it spends it.
"""

import argparse
import json
import os
import re
import sys
import time

# Published per-million-token rates, input/output. Used only for the estimate
# printed before a run; the invoice remains the authority.
_RATES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_BATCH_DISCOUNT = 0.5

# Models that accept output_config.effort and adaptive thinking. Sending
# either to a model that does not support it is a 400, so the request is
# built per model rather than assuming the newest shape everywhere.
_SUPPORTS_EFFORT = {"claude-opus-5", "claude-sonnet-5"}

_MODEL = "claude-opus-5"
_MAX_TOKENS = 1024

# A draft is a human-facing page under this project's rules: it never carries
# a price. It is also the one artefact a stranger reads, so a leaked internal
# rate is both a rule break and a negotiating position given away for free.
_PRICE_PATTERN = re.compile(r"\$|\bUSD\b|\bcents?\b|\bper (?:call|audit|scan)\b", re.I)

_SYSTEM = """You write one short cold email to whoever operates a website, \
from a report of defects an automated audit found on their live site.

Absolute rules:
- Every defect you name must appear in the FINDINGS block, written with its \
exact rule id. Never mention a defect that is not in the block.
- Never state a number that is not in the block. Do not round up, do not \
total things yourself, do not write "dozens" or "many" for a small count.
- No adjectives about their company, their design, or their business. No \
flattery, no "I love what you're building".
- Never mention price, cost, dollars, discounts, or a free trial.
- No sign-off, no signature, no links, no markdown. Plain text.
- 120 words maximum.
- Close with one question that invites a reply.

Output exactly this shape and nothing else:
SUBJECT: <one line>

<body>"""


def _evidence(prospect, limit=6):
    """The findings block the model is allowed to draw from, and only that."""
    rows = sorted(
        prospect.get("findings") or [],
        key=lambda f: -{"critical": 10, "serious": 5, "moderate": 2}.get(
            f.get("impact"), 1
        ),
    )[:limit]
    lines = ["SITE: %s" % prospect.get("host", "?"), "", "FINDINGS:"]
    for finding in rows:
        nodes = finding.get("nodes") or 0
        where = " affecting %d element(s)" % nodes if nodes else ""
        lines.append(
            "- %s [%s, %s]%s: %s"
            % (
                finding.get("id", "?"),
                finding.get("dimension", "?"),
                finding.get("impact", "?"),
                where,
                finding.get("help", ""),
            )
        )
    return "\n".join(lines)


def _allowed_numbers(prospect, limit=6):
    """Numbers the draft is permitted to contain.

    Anything else is either invented or arithmetic the model did on its own,
    and both are how an email stops being a bug report.
    """
    rows = (prospect.get("findings") or [])[:limit]
    numbers = {finding.get("nodes") or 0 for finding in prospect.get("findings") or []}
    numbers.add(len(prospect.get("findings") or []))
    numbers.add(len(rows))
    numbers.discard(0)
    return numbers


def _rule_ids(prospect):
    return {f.get("id") for f in prospect.get("findings") or [] if f.get("id")}


def validate(draft, prospect, vocabulary):
    """Reasons this draft must not be sent, or an empty list.

    Fails closed: an unrecognised shape is a rejection, never a pass. The
    caller writes rejections out marked for review rather than dropping them,
    because a silently missing prospect is a lead nobody ever works.
    """
    problems = []
    body = (draft or "").strip()
    if not body:
        return ["empty draft"]
    if not body.startswith("SUBJECT:"):
        problems.append("no SUBJECT line")

    mine = _rule_ids(prospect)
    if not any(rule in body for rule in mine):
        problems.append("names none of this site's findings")
    for rule in sorted(vocabulary - mine):
        if rule in body:
            problems.append("names %s, which did not fire on this site" % rule)

    allowed = _allowed_numbers(prospect)
    for token in re.findall(r"\b\d+\b", body):
        if int(token) not in allowed:
            problems.append("states %s, which is not a number we measured" % token)

    if _PRICE_PATTERN.search(body):
        problems.append("mentions price")
    return problems


def _request_kwargs(model):
    kwargs = {"model": model, "max_tokens": _MAX_TOKENS, "system": _SYSTEM}
    if model in _SUPPORTS_EFFORT:
        # Low effort: drafting four sentences from a supplied list is not a
        # hard problem, and at outreach volume the thinking tokens are the
        # bill. Thinking stays on -- disabling it leaks internal tags.
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "low"}
    return kwargs


def _text(message):
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def generate_sync(client, model, prospects):
    drafts = []
    for prospect in prospects:
        message = client.messages.create(
            messages=[{"role": "user", "content": _evidence(prospect)}],
            **_request_kwargs(model)
        )
        drafts.append(_text(message))
    return drafts


def generate_batch(client, model, prospects, poll_seconds=20, sleep=time.sleep):
    """Same drafts at half the token price, for runs where latency is free.

    Results come back in arbitrary order, so they are keyed by custom_id and
    re-aligned; reading them positionally would silently send each prospect
    somebody else's findings.
    """
    requests = [
        {
            "custom_id": "p%d" % index,
            "params": dict(
                messages=[{"role": "user", "content": _evidence(prospect)}],
                **_request_kwargs(model)
            ),
        }
        for index, prospect in enumerate(prospects)
    ]
    batch = client.messages.batches.create(requests=requests)
    while True:
        state = client.messages.batches.retrieve(batch.id)
        if state.processing_status == "ended":
            break
        sleep(poll_seconds)

    by_id = {}
    for entry in client.messages.batches.results(batch.id):
        if entry.result.type == "succeeded":
            by_id[entry.custom_id] = _text(entry.result.message)
        else:
            by_id[entry.custom_id] = ""
    return [by_id.get("p%d" % index, "") for index in range(len(prospects))]


def estimate_usd(prospects, model, batch):
    """Rough spend, printed before the run rather than discovered after it."""
    rate_in, rate_out = _RATES.get(model, _RATES[_MODEL])
    if batch:
        rate_in, rate_out = rate_in * _BATCH_DISCOUNT, rate_out * _BATCH_DISCOUNT
    total_in = sum(
        (len(_SYSTEM) + len(_evidence(p))) / 4.0 for p in prospects
    )
    total_out = len(prospects) * 220.0
    return (total_in * rate_in + total_out * rate_out) / 1_000_000


def render_markdown(rows):
    sendable = [r for r in rows if not r["problems"]]
    lines = [
        "# Outreach drafts",
        "",
        "%d drafted, %d ready to send, %d held for review."
        % (len(rows), len(sendable), len(rows) - len(sendable)),
        "",
    ]
    for row in rows:
        lines.append("## %s" % row["host"])
        lines.append("")
        if row["problems"]:
            lines.append("**HELD -- do not send.** " + "; ".join(row["problems"]))
            lines.append("")
        lines.append("```")
        lines.append(row["draft"].strip() or "(no draft returned)")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leads", required=True,
                        help="leads.json from prospect_scan.py --json-out")
    parser.add_argument("--out", help="write the markdown drafts here")
    parser.add_argument("--json-out", help="write drafts here for a mail merge")
    parser.add_argument("--model", default=_MODEL, help="default %s" % _MODEL)
    parser.add_argument("--batch", action="store_true",
                        help="use the Batch API: half price, not immediate")
    parser.add_argument("--limit", type=int, help="draft only the first N")
    parser.add_argument("--yes", action="store_true", help="skip the spend gate")
    args = parser.parse_args(argv)

    try:
        with open(args.leads) as handle:
            leads = json.load(handle)
    except (OSError, ValueError) as exc:
        print("could not read %s: %s" % (args.leads, exc), file=sys.stderr)
        return 2

    # A prospect with no findings gets no pitch. Emailing a site that passed
    # every check is the fastest way to be marked as spam, and the scan
    # already told us which ones those are.
    prospects = [lead for lead in leads if lead.get("findings")]
    if args.limit:
        prospects = prospects[: args.limit]
    if not prospects:
        print("no prospects with findings in %s" % args.leads, file=sys.stderr)
        return 3

    estimate = estimate_usd(prospects, args.model, args.batch)
    print(
        "Drafting %d email(s) with %s%s. Estimated spend: $%.2f"
        % (len(prospects), args.model, " (batch)" if args.batch else "", estimate),
        file=sys.stderr,
    )
    if not args.yes and estimate > 5.00:
        print("Above $5.00 -- re-run with --yes to confirm.", file=sys.stderr)
        return 3

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 4

    try:
        import anthropic
    except ImportError:
        print("pip install anthropic", file=sys.stderr)
        return 4

    client = anthropic.Anthropic()
    generate = generate_batch if args.batch else generate_sync
    drafts = generate(client, args.model, prospects)

    # The vocabulary of every rule seen anywhere in this run. A draft that
    # names a rule from a different prospect's audit is the failure mode that
    # looks most convincing and is most wrong.
    vocabulary = set()
    for prospect in prospects:
        vocabulary |= _rule_ids(prospect)

    rows = []
    for prospect, draft in zip(prospects, drafts):
        rows.append(
            {
                "host": prospect.get("host", "?"),
                "url": prospect.get("url", ""),
                "score": prospect.get("score", 0),
                "draft": draft,
                "problems": validate(draft, prospect, vocabulary),
            }
        )

    report = render_markdown(rows)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(report)
        print("wrote %s" % args.out, file=sys.stderr)
    else:
        print(report)

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(rows, handle, indent=2)
        print("wrote %s" % args.json_out, file=sys.stderr)

    held = len([r for r in rows if r["problems"]])
    print("%d ready, %d held for review" % (len(rows) - held, held), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
