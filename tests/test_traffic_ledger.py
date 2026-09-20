"""scripts/traffic-ledger.sh: the tollbooth's own count -- who arrives, who
bounces on the 402, who pays -- from the node's logs. Discovery work is
measured by machine traffic arriving, trusting and paying; this is how that
is read, and it must read the logs the way they are actually written."""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "traffic-ledger.sh"
CADDYFILE = REPO_ROOT / "deploy" / "vps" / "Caddyfile"


def _access(client, ua, path, status):
    return "caddy-1  | " + json.dumps({
        "level": "info", "ts": 1789218598.8, "logger": "http.log.access",
        "msg": "handled request", "status": status, "duration": 0.9,
        "request": {"remote_ip": client, "client_ip": client, "method": "POST",
                    "host": "hubvibe-io.com", "uri": path,
                    "headers": {"User-Agent": [ua]}},
    })


def _run(lines):
    return subprocess.run(
        ["bash", str(SCRIPT), "--stdin"], input="\n".join(lines) + "\n",
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout


def test_the_caddyfile_actually_emits_the_access_log_the_ledger_reads():
    """Without the `log` directive Caddy writes errors only, and the ledger
    would report a dead node. The directive and the JSON format are the
    contract; a console-format log has no `request` object to parse."""
    text = CADDYFILE.read_text()
    site = text[text.index("{$DOMAIN} {"):text.index("www.{$DOMAIN}")]
    assert "log {" in site, "Caddy access logging is not enabled for the site"
    assert "format json" in site


def test_it_counts_arrivals_bounces_and_payments_per_route():
    lines = [
        _access("10.0.0.1", "x402-client/2.22 python", "/audit/wcag", 402),
        _access("10.0.0.1", "x402-client/2.22 python", "/audit/wcag", 200),
        _access("10.0.0.2", "curl/8.18.0", "/audit/seo?x=1", 402),
        _access("10.0.0.3", "some-agent/1.0", "/audit/bundle", 502),
        _access("10.0.0.4", "Mozilla/5.0", "/", 200),
        _access("10.0.0.5", "mcp-client/1.0", "/mcp", 200),
        _access("10.0.0.6", "uvd-bazaar-health/1.0", "/audit/wcag", 405),
        "hubvibe-1  | 2026-09-12T13:10:28Z INFO:app.x402_payments:x402 SETTLED (settle) "
        "price=$0.03 tx=0xabc network=eip155:8453 payer=0x1 amount=None",
        "hubvibe-1  | WARNING:app.main:x402 audit WITHHELD: settle refused after the audit ran (x); "
        "nothing charged, result not delivered",
    ]
    out = _run(lines)
    assert "audit/mcp requests: 6" in out, out
    assert "distinct clients: 5" in out
    # The /mcp 200 is a free tools/list, not a sale; the 405 is a crawler
    # that never saw the price.
    assert "refused before the price (400/405/422): 1" in out
    assert "paid (200): 1" in out and "challenged (402): 2" in out and "failed (502): 1" in out
    assert "withheld on refused settle: 1" in out
    # 10.0.0.1 paid after its 402; 10.0.0.2 bounced. The homepage hit is not traffic.
    assert "saw a 402 and never paid: 1" in out
    assert "/audit/seo" in out and "?x=1" not in out, "query strings must not split a route"
    assert "x402-client/2.22 python" in out
    assert "settlements by facilitator" in out


def test_settlement_lines_name_their_facilitator_when_the_log_carries_it():
    lines = [
        "hubvibe-1  | INFO:app.x402_payments:x402 SETTLED (settle) price=$0.03 tx=0x1 "
        "network=eip155:8453 payer=0x1 amount=None facilitator=https://facilitator.payai.network",
    ]
    out = _run(lines)
    assert "https://facilitator.payai.network" in out


def test_an_empty_window_says_the_access_log_may_be_missing():
    out = _run(["hubvibe-1  | INFO:     172.18.0.3:1 - \"GET /health HTTP/1.1\" 200 OK"])
    assert "no access-log lines seen" in out
    assert "restart the caddy container" in out, "a rebuild does not re-read the bind-mounted Caddyfile"
    assert "settlements: none" in out


def test_the_ledger_only_reads():
    text = SCRIPT.read_text()
    for forbidden in ("docker compose up", "docker compose restart", "rm -", "sed -i", "git "):
        assert forbidden not in text, f"a read-only tool must not run {forbidden!r}"
