"""Run completo: job alert dividido por vaga, avaliações persistidas, sem lixo em recruiters."""

from datetime import UTC, datetime

from typer.testing import CliRunner

from recruiter_pipeline import main as main_module
from recruiter_pipeline.config import Config
from recruiter_pipeline.db import StateDB
from recruiter_pipeline.models import (
    Classification,
    OpportunityAssessment,
    ProposalExtraction,
    RecruiterMessage,
)

runner = CliRunner()

ALERT_HTML = """
<table><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678?x=1">Site Reliability Engineer</a>
<div>Acme · Porto</div></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4099999999?x=1">Platform Engineer</a>
<div>Globex · Lisboa</div></td></tr></table>
"""


class FakeGmail:
    def __init__(self, *a, **k):
        pass

    def fetch_messages(self, **kwargs):
        return [
            RecruiterMessage(
                message_id="alert-1",
                sender_name="Your job alert for SRE",
                subject="Your job alert for SRE",
                body_text="x",
                body_html=ALERT_HTML,
                received_at=datetime(2026, 1, 1, tzinfo=UTC),
                email_type="job_alert",
            )
        ]

    def mark_as_read(self, message_id):
        pass


class ExplodingClassifier:
    """Vagas de job alert não devem gastar classificação."""

    def __init__(self, *a, **k):
        pass

    def classify(self, message):
        raise AssertionError("job alert não deve ser classificado")


class FakeExtractor:
    def __init__(self, *a, **k):
        pass

    def extract(self, message):
        company, _, _ = message.body_text.splitlines()[1].partition(" · ")
        return ProposalExtraction(
            recruiter_name="Your job alert for SRE",
            company_client=company,
            role_title=message.subject,
            summary="s",
            confidence=0.9,
        )


class FakeAssessor:
    available = True

    def __init__(self, *a, **k):
        pass

    def assess(self, message, extraction, enrichment):
        return OpportunityAssessment(
            recommended_action="monitor",
            fit_score=None,
            fit_confidence=20,
            decision_trace="jev: ignore → monitor",
            action_source="guardrail",
        )


class FakeBoard:
    def __init__(self, *a, **k):
        self.calls = []

    def upsert_proposal(self, fingerprint, extraction, message, enrichment, assessment):
        self.calls.append((fingerprint, message.job_id))
        return f"page-{message.job_id}"


def test_job_alert_becomes_one_proposal_per_job(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "state_db_path", tmp_path / "state.db")
    monkeypatch.setattr(Config, "validate", classmethod(lambda cls: None))
    monkeypatch.setattr(Config, "openrouter_api_key", "k")
    monkeypatch.setattr(main_module, "GmailClient", FakeGmail)
    monkeypatch.setattr(main_module, "Classifier", ExplodingClassifier)
    monkeypatch.setattr(main_module, "Extractor", FakeExtractor)
    monkeypatch.setattr(main_module, "Assessor", FakeAssessor)
    monkeypatch.setattr(main_module, "NotionBoard", FakeBoard)
    monkeypatch.setattr(main_module, "LLMClient", lambda *a, **k: type("L", (), {"call_count": 0, "total_tokens": 0})())

    result = runner.invoke(main_module.app, ["run", "--skip-enrichment"])
    assert result.exit_code == 0, result.output

    db = StateDB(db_path=tmp_path / "state.db")
    assert db.is_email_processed("alert-1#job4012345678")
    assert db.is_email_processed("alert-1#job4099999999")
    # o assunto do alerta nunca é um recruiter
    assert db.list_recruiters() == []
    # avaliação e rasto de decisão persistidos para auditoria
    rows = {r["message_id"]: r for r in db.list_assessments()}
    assert len(rows) == 2
    row = rows["alert-1#job4012345678"]
    assert row["action"] == "monitor" and row["action_source"] == "guardrail"
    assert row["decision_trace"] == "jev: ignore → monitor"
    assert row["company"] == "Acme" and row["email_type"] == "job_alert"
    # backup criado antes de escrever
    assert list(tmp_path.glob("state.db.bak-*"))
    assert "Propostas encontradas" in result.output
