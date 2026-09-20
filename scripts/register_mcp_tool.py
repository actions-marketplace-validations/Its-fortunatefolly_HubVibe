"""Pay for one MCP tool call, carrying the Bazaar record, so the tool gets
listed in the facilitator's index.

WHY THIS EXISTS SEPARATELY FROM first-paid-call.sh

A Bazaar record reaches a facilitator on the PAYMENT PAYLOAD -- the resource
server declares it on the 402, the paying client echoes it back, and the
facilitator catalogs what it sees (`validate_and_extract` on the payload's
`extensions.bazaar`). The HTTP client in the x402 package does that echo for
you. **The MCP client does not:** `x402.mcp.client` calls
`create_payment_payload(payment_required)` with neither `resource` nor
`extensions`, so a normal paying MCP agent can never register an MCP tool.

Measured 2026-09-13: Coinbase's Bazaar held 15,352 resources and exactly 5 of
type `mcp`, from two servers. That is not a coincidence, it is the client gap
above. PayAI's index held 31, so some servers do get listed -- with a client
that carries the record, which is what this script is.

So the tools this node sells over MCP are invisible to capability search
unless we register them ourselves, once per facilitator that indexes them.
Our node's MCP paywall already declares a valid `type: mcp` record (verified
against the library's own facilitator-side validator); this only makes sure a
payment actually carries it.

Costs one real call at that tool's price. Run it on the box, where the payer
wallet and its key live.

    TOOL=audit_wcag python3 scripts/register_mcp_tool.py

    TOOL    which tool to pay for   (default audit_wcag)
    BASE    the node                (default https://hubvibe-io.com)
    TARGET  the URL to audit        (default https://example.com)

Exits 0 only when the paid call came back with a result and no billing
warning -- a settle that did not land registers nothing, so reporting success
there would be the same false pass this codebase exists to avoid.
"""

import json
import os
import sys
import urllib.error
import urllib.request

TOOL = os.environ.get("TOOL", "audit_wcag")
BASE = os.environ.get("BASE", "https://hubvibe-io.com").rstrip("/")
TARGET = os.environ.get("TARGET", "https://example.com")
WALLET_FILE = os.environ.get("HUBVIBE_WALLET_FILE", os.path.expanduser("~/.hubvibe-wallet-key"))


def fail(message):
    print("  STOP  %s" % message, file=sys.stderr)
    raise SystemExit(1)


def rpc(body):
    """One JSON-RPC call to the node's MCP endpoint.

    Accepts either a plain JSON response or an SSE stream: the endpoint is
    streamable-http, and which one comes back depends on the Accept header
    and the server's mood. Reading only JSON here would fail intermittently.
    """
    request = urllib.request.Request(
        BASE + "/mcp",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode()
    except urllib.error.URLError as exc:
        fail("could not reach %s/mcp: %s" % (BASE, exc))
    if raw.startswith("event:") or "\ndata:" in raw:
        data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        if not data_lines:
            fail("the MCP endpoint answered with an SSE stream carrying no data frame")
        raw = data_lines[-1]
    try:
        return json.loads(raw)
    except ValueError:
        fail("the MCP endpoint did not answer with JSON: %s" % raw[:200])


def main():
    try:
        wallet_key = open(WALLET_FILE).read().strip()
    except OSError as exc:
        fail("no payer key at %s (%s). Run this on the box." % (WALLET_FILE, exc))

    try:
        from eth_account import Account
        from x402 import x402ClientSync
        from x402.mcp.types import MCPToolResult
        from x402.mcp.utils import extract_payment_required_from_result
        from x402.mechanisms.evm import EthAccountSigner
        from x402.mechanisms.evm.exact import register_exact_evm_client
    except ImportError as exc:
        fail("the x402 client extras are missing (%s). pip install 'x402[evm,mcp]' eth-account" % exc)

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"url": TARGET}},
    }
    first = rpc(call).get("result") or {}
    if not first.get("isError"):
        fail(
            "the unpaid call was NOT challenged (isError false). Either %s is free "
            "or x402 is not configured -- nothing to register." % TOOL
        )

    challenge = extract_payment_required_from_result(
        MCPToolResult(
            content=first.get("content") or [],
            is_error=True,
            meta=first.get("_meta"),
            structured_content=first.get("structuredContent"),
        )
    )
    if challenge is None:
        fail("the paywall did not carry a v2 PaymentRequired an MCP client could parse")
    if not challenge.extensions or "bazaar" not in challenge.extensions:
        fail(
            "the challenge carries no bazaar record, so paying it would register "
            "nothing. Check x402_payments.bazaar_extension_for_mcp_tool."
        )

    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(Account.from_key(wallet_key)))

    # The whole point of this script: the resource and the extensions travel
    # WITH the payment. x402.mcp's own client omits both (client.py:130,269),
    # which is why the MCP lane of every Bazaar is nearly empty.
    payload = client.create_payment_payload(challenge, challenge.resource, challenge.extensions)

    call["id"] = 2
    call["params"]["_meta"] = {"x402/payment": payload.model_dump(by_alias=True)}
    result = (rpc(call) or {}).get("result") or {}

    if result.get("isError"):
        text = (result.get("content") or [{}])[0].get("text", "")
        fail("the paid call was refused: %s" % text[:300])

    body = result.get("structuredContent") or {}
    warning = body.get("billing_warning")
    receipt = (result.get("_meta") or {}).get("x402/payment-response") or {}
    transaction = receipt.get("transaction") if isinstance(receipt, dict) else None

    if warning:
        # Delivered but not collected: the settle never reached the
        # facilitator, so no record was catalogued either.
        fail("delivered but NOT charged (%s) -- nothing was registered" % warning)

    print("  OK    %s registered; tx %s" % (TOOL, transaction or "(no receipt)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
