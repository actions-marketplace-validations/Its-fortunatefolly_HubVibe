"""Guards for scripts/prospect_scan.py.

This spends real money per prospect and produces text that gets sent to
strangers, so the two things that matter are: it cannot quietly run up a bill,
and it cannot claim a finding that did not fire. An opener that overstates is
worse than none, because the recipient checks their own site in thirty
seconds and the claim either holds or the sender is done.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "prospect_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("prospect_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CLEAN = {"pass": True, "wcag": {"violations": []}, "seo": {"findings": []}}

_DIRTY = {
    "pass": False,
    "wcag": {
        "violations": [
            {"id": "color-contrast", "impact": "serious", "nodes_affected": 4,
             "help": "Elements must have sufficient contrast"},
            {"id": "image-alt", "impact": "critical", "nodes_affected": 2,
             "help": "Images must have alternate text"},
        ]
    },
    "security": {"findings": [{"id": "no-hsts", "severity": "moderate",
                               "detail": "HSTS missing"}]},
}


def test_targets_are_normalised(tmp_path):
    module = _load()
    assert module._normalise("acme.com") == "https://acme.com"
    assert module._normalise("https://acme.com/") == "https://acme.com"
    assert module._normalise("  ") == ""


def test_findings_are_flattened_across_dimensions():
    module = _load()
    findings = module._findings(_DIRTY)
    assert {f["id"] for f in findings} == {"color-contrast", "image-alt", "no-hsts"}
    assert {f["dimension"] for f in findings} == {"wcag", "security"}


def test_ranking_puts_the_worst_prospect_first(monkeypatch):
    """A list sorted any other way gets worked from the top and burns the best
    evidence on whoever happened to be first in the CRM export."""
    module = _load()
    responses = {
        "https://clean.example": (200, _CLEAN),
        "https://dirty.example": (200, _DIRTY),
    }
    monkeypatch.setattr(
        module, "_post",
        lambda base, path, payload, key, timeout: responses[payload["url"]],
    )
    results = module.scan(
        ["clean.example", "dirty.example"], "https://stub", "k", 10, "bundle"
    )
    assert results[0]["host"] == "dirty.example"
    assert results[0]["score"] > results[1]["score"]


def test_the_opener_only_names_findings_that_fired(monkeypatch):
    module = _load()
    opener = module._opener("dirty.example", module._findings(_DIRTY))
    assert "image-alt" in opener
    assert "critical" in opener
    # Two high-impact findings: one critical, one serious.
    assert "2 high-impact" in opener


def test_a_clean_site_gets_no_pitch(monkeypatch):
    """Sending a remediation pitch to a site with no findings is the fastest
    way to be marked as spam."""
    module = _load()
    opener = module._opener("clean.example", [])
    assert "passes every check" in opener
    assert "failures" not in opener


def test_an_unpaid_call_is_reported_not_counted_as_clean(monkeypatch):
    """A 402 means we learned nothing. Recording it as a pass would put a
    prospect in the 'no findings' bucket having never audited them."""
    module = _load()
    monkeypatch.setattr(
        module, "_post", lambda *a, **k: (402, {"error": "payment_required"})
    )
    results = module.scan(["acme.com"], "https://stub", "", 10, "bundle")
    assert results[0]["status"] == 402
    assert "findings" not in results[0]
    assert "not paid for" in results[0]["error"]


def test_an_unreachable_target_does_not_abort_the_run(monkeypatch):
    """One dead domain in a 500-row CRM export must not lose the other 499."""
    module = _load()
    calls = {"n": 0}

    def _flaky(base, path, payload, key, timeout):
        calls["n"] += 1
        if payload["url"] == "https://dead.example":
            return 0, {"error": "ConnectionError: refused"}
        return 200, _DIRTY

    monkeypatch.setattr(module, "_post", _flaky)
    results = module.scan(
        ["dead.example", "live.example"], "https://stub", "k", 10, "bundle"
    )
    assert calls["n"] == 2
    hosts = {r["host"] for r in results}
    assert hosts == {"dead.example", "live.example"}
    assert any("findings" in r for r in results)


def test_a_large_run_refuses_without_explicit_confirmation(tmp_path, capsys):
    """500 domains is $50 at the bundle rate. That is fine if intended and a
    nasty surprise if not, so the number is shown and the spend is gated."""
    module = _load()
    listing = tmp_path / "prospects.txt"
    listing.write_text("\n".join("site%d.example" % i for i in range(200)))
    code = module.main(["--file", str(listing)])
    assert code == 3
    assert "re-run with --yes" in capsys.readouterr().err


def test_comments_and_blank_lines_in_a_list_are_ignored(tmp_path, monkeypatch, capsys):
    module = _load()
    listing = tmp_path / "prospects.txt"
    listing.write_text("# leads from Q3\n\nacme.com\n\n# skip this\nexample.org\n")
    monkeypatch.setattr(module, "_post", lambda *a, **k: (200, _CLEAN))
    code = module.main(["--file", str(listing), "--yes"])
    assert code == 0
    assert "Scanning 2 prospect(s)" in capsys.readouterr().err


def test_the_report_and_json_are_written(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "_post", lambda *a, **k: (200, _DIRTY))
    out = tmp_path / "leads.md"
    raw = tmp_path / "leads.json"
    assert module.main(["acme.com", "--out", str(out), "--json-out", str(raw)]) == 0

    report = out.read_text()
    assert "acme.com" in report
    assert "color-contrast" in report
    assert "Opener:" in report

    rows = json.loads(raw.read_text())
    assert rows[0]["host"] == "acme.com"
    assert rows[0]["score"] > 0


def test_no_targets_is_an_error():
    module = _load()
    with pytest.raises(SystemExit):
        module.main([])
