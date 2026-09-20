"""Guards for scripts/draft_outreach.py.

This spends real money and produces text sent to strangers, so the guards
are about the two ways that goes wrong: a bill nobody agreed to, and a claim
the recipient can disprove by looking at their own site. The second is the
expensive one -- the entire reason a cold audit email gets read instead of
deleted is that every line in it is checkable, and one inflated number burns
that for every future email from the same domain.

Every check here is written so that removing the guard it covers turns it
red. The validator fails closed on purpose: a draft it cannot vouch for is
held for a human, never dropped and never sent.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "draft_outreach.py"


def _load():
    spec = importlib.util.spec_from_file_location("draft_outreach", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PROSPECT = {
    "host": "acme.example",
    "url": "https://acme.example",
    "score": 60,
    "findings": [
        {"dimension": "wcag", "id": "image-alt", "impact": "critical",
         "nodes": 2, "help": "Images must have alternate text"},
        {"dimension": "wcag", "id": "color-contrast", "impact": "serious",
         "nodes": 4, "help": "Elements must have sufficient contrast"},
    ],
}

_OTHER = {
    "host": "beta.example",
    "url": "https://beta.example",
    "score": 10,
    "findings": [
        {"dimension": "security", "id": "no-hsts", "impact": "moderate",
         "nodes": 0, "help": "HSTS missing"},
    ],
}

_GOOD = (
    "SUBJECT: image-alt failures on acme.example\n\n"
    "Your checkout page is missing alternate text: image-alt fires on 2 "
    "elements, and color-contrast on 4. Want the full report?"
)

_VOCAB = {"image-alt", "color-contrast", "no-hsts"}


def _message(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_a_truthful_draft_passes():
    module = _load()
    assert module.validate(_GOOD, _PROSPECT, _VOCAB) == []


def test_a_draft_naming_another_sites_finding_is_held():
    """The convincing failure: real rule id, wrong site. The recipient checks,
    finds no HSTS problem, and concludes the whole email was generated."""
    module = _load()
    draft = _GOOD.replace("color-contrast on 4", "no-hsts on 4")
    problems = module.validate(draft, _PROSPECT, _VOCAB)
    assert any("no-hsts" in p and "did not fire" in p for p in problems)


def test_an_inflated_number_is_held():
    """4 elements becoming 40 is the difference between a bug report and a
    scare email, and it is the one thing the recipient counts."""
    module = _load()
    draft = _GOOD.replace("on 4", "on 40")
    problems = module.validate(draft, _PROSPECT, _VOCAB)
    assert any("40" in p for p in problems)


def test_a_measured_number_is_not_held():
    """The inflation guard must not fire on the counts we actually supplied,
    or every true draft gets held and the guard is worthless."""
    module = _load()
    assert module.validate(_GOOD, _PROSPECT, _VOCAB) == []
    assert 2 in module._allowed_numbers(_PROSPECT)
    assert 4 in module._allowed_numbers(_PROSPECT)


def test_a_draft_that_quotes_a_price_is_held():
    """Standing rule for this project: no per-call price on anything a human
    reads. An outreach email is the most human-facing surface we have."""
    module = _load()
    for priced in ("It costs $0.03 per page.", "Only 3 cents per call.",
                   "USD pricing on request."):
        draft = _GOOD + " " + priced
        assert any("price" in p for p in module.validate(draft, _PROSPECT, _VOCAB))


def test_a_generic_draft_with_no_findings_is_held():
    """If it names none of their failures it is just another cold email, and
    sending it spends the domain's reputation for nothing."""
    module = _load()
    draft = "SUBJECT: quick question\n\nWe audit websites. Interested?"
    problems = module.validate(draft, _PROSPECT, _VOCAB)
    assert any("names none" in p for p in problems)


def test_an_empty_draft_is_held():
    module = _load()
    assert module.validate("", _PROSPECT, _VOCAB) == ["empty draft"]
    assert module.validate(None, _PROSPECT, _VOCAB) == ["empty draft"]


def test_a_draft_without_a_subject_is_held():
    module = _load()
    body = _GOOD.split("\n\n", 1)[1]
    assert any("SUBJECT" in p for p in module.validate(body, _PROSPECT, _VOCAB))


def test_held_drafts_are_written_out_marked_not_dropped():
    """A prospect that silently vanishes is a lead nobody ever works. The
    report has to show the bad draft and why it was held."""
    module = _load()
    rows = [
        {"host": "acme.example", "url": "", "score": 60,
         "draft": _GOOD, "problems": []},
        {"host": "beta.example", "url": "", "score": 10,
         "draft": "SUBJECT: x\n\nno-hsts on 40 pages", "problems": ["states 40"]},
    ]
    report = module.render_markdown(rows)
    assert "beta.example" in report
    assert "HELD" in report
    assert "states 40" in report
    assert "1 ready to send, 1 held" in report


def test_batch_results_are_realigned_by_id_not_position():
    """Batch results come back in arbitrary order. Reading them positionally
    sends each prospect somebody else's findings -- which reads as a real
    email and is completely wrong."""
    module = _load()
    prospects = [_PROSPECT, _OTHER]

    class _Batches:
        def create(self, requests):
            assert [r["custom_id"] for r in requests] == ["p0", "p1"]
            return SimpleNamespace(id="batch_1")

        def retrieve(self, batch_id):
            return SimpleNamespace(id=batch_id, processing_status="ended")

        def results(self, batch_id):
            # Deliberately reversed.
            return [
                SimpleNamespace(custom_id="p1", result=SimpleNamespace(
                    type="succeeded", message=_message("second"))),
                SimpleNamespace(custom_id="p0", result=SimpleNamespace(
                    type="succeeded", message=_message("first"))),
            ]

    client = SimpleNamespace(messages=SimpleNamespace(batches=_Batches()))
    drafts = module.generate_batch(client, "claude-haiku-4-5", prospects,
                                   sleep=lambda _: None)
    assert drafts == ["first", "second"]


def test_a_failed_batch_entry_becomes_an_empty_draft_not_a_crash():
    """One errored row must not lose the other 499."""
    module = _load()

    class _Batches:
        def create(self, requests):
            return SimpleNamespace(id="b")

        def retrieve(self, batch_id):
            return SimpleNamespace(id="b", processing_status="ended")

        def results(self, batch_id):
            return [
                SimpleNamespace(custom_id="p0", result=SimpleNamespace(
                    type="errored", error="boom")),
                SimpleNamespace(custom_id="p1", result=SimpleNamespace(
                    type="succeeded", message=_message("fine"))),
            ]

    client = SimpleNamespace(messages=SimpleNamespace(batches=_Batches()))
    drafts = module.generate_batch(client, "claude-haiku-4-5",
                                   [_PROSPECT, _OTHER], sleep=lambda _: None)
    assert drafts == ["", "fine"]
    assert module.validate(drafts[0], _PROSPECT, _VOCAB) == ["empty draft"]


def test_effort_is_only_sent_to_models_that_accept_it():
    """output_config.effort is a 400 on Haiku 4.5. Sending it anyway turns
    the cheap model -- the only one the credit budget actually affords at
    volume -- into a run that fails on every request."""
    module = _load()
    assert "output_config" not in module._request_kwargs("claude-haiku-4-5")
    assert "thinking" not in module._request_kwargs("claude-haiku-4-5")
    opus = module._request_kwargs("claude-opus-5")
    assert opus["output_config"] == {"effort": "low"}
    assert opus["thinking"]["type"] == "adaptive"


def test_the_evidence_block_carries_only_this_prospects_findings():
    module = _load()
    block = module._evidence(_PROSPECT)
    assert "image-alt" in block and "color-contrast" in block
    assert "no-hsts" not in block
    assert "acme.example" in block


def test_batch_is_cheaper_than_sync_and_haiku_cheaper_than_opus():
    """The whole reason the model is a flag: the run has to be able to show
    the operator what each choice costs before it spends anything."""
    module = _load()
    prospects = [_PROSPECT] * 100
    sync_opus = module.estimate_usd(prospects, "claude-opus-5", batch=False)
    batch_opus = module.estimate_usd(prospects, "claude-opus-5", batch=True)
    batch_haiku = module.estimate_usd(prospects, "claude-haiku-4-5", batch=True)
    assert batch_opus == pytest.approx(sync_opus / 2)
    assert batch_haiku < batch_opus


def test_a_large_run_refuses_without_explicit_confirmation(tmp_path, capsys):
    module = _load()
    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps([dict(_PROSPECT, host="s%d.example" % i)
                                 for i in range(4000)]))
    assert module.main(["--leads", str(leads)]) == 3
    assert "re-run with --yes" in capsys.readouterr().err


def test_sites_that_passed_every_check_are_never_drafted(tmp_path, capsys):
    """Pitching remediation to a site with no findings is the fastest way to
    be marked as spam, and the scan already told us which those are."""
    module = _load()
    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps([
        {"host": "clean.example", "url": "https://clean.example",
         "passed": True, "findings": [], "score": 0},
        {"host": "dead.example", "status": 0, "error": "refused"},
    ]))
    assert module.main(["--leads", str(leads)]) == 3
    assert "no prospects with findings" in capsys.readouterr().err


def test_a_run_without_a_key_stops_before_it_pretends_to_draft(
    tmp_path, monkeypatch, capsys
):
    module = _load()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps([_PROSPECT]))
    assert module.main(["--leads", str(leads)]) == 4
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_an_end_to_end_run_writes_both_artefacts(tmp_path, monkeypatch, capsys):
    module = _load()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(module, "generate_sync",
                        lambda client, model, prospects: [_GOOD])

    class _FakeAnthropic:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setitem(
        __import__("sys").modules, "anthropic",
        SimpleNamespace(Anthropic=_FakeAnthropic),
    )

    leads = tmp_path / "leads.json"
    leads.write_text(json.dumps([_PROSPECT]))
    out = tmp_path / "drafts.md"
    raw = tmp_path / "drafts.json"
    code = module.main(["--leads", str(leads), "--out", str(out),
                        "--json-out", str(raw)])
    assert code == 0

    report = out.read_text()
    assert "acme.example" in report
    assert "image-alt" in report
    assert "HELD" not in report

    rows = json.loads(raw.read_text())
    assert rows[0]["problems"] == []
    assert "1 ready, 0 held" in capsys.readouterr().err


def test_a_missing_leads_file_is_an_error_not_a_traceback(tmp_path, capsys):
    module = _load()
    assert module.main(["--leads", str(tmp_path / "nope.json")]) == 2
    assert "could not read" in capsys.readouterr().err
