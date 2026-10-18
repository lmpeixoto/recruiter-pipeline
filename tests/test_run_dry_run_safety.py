"""Regressão P0.1: --dry-run não pode deixar efeitos persistentes.

Sem isto, um preview grava em state_db ("proposal_dry_run" e marca
notificações/não-oportunidades como processadas, e avança o checkpoint de
--since-last-run via record_run). Um run real a seguir salta mensagens que
nunca foram processadas de verdade -- foi exatamente isto que aconteceu
em produção (ver sessão de 2026-10-18).
"""

from datetime import UTC, datetime

from typer.testing import CliRunner

from recruiter_pipeline import main as main_module
from recruiter_pipeline.config import Config
from recruiter_pipeline.db import StateDB
from recruiter_pipeline.models import (
    Classification,
    ProposalExtraction,
    RecruiterMessage,
)

runner = CliRunner()


class FakeGmailClient:
    def __init__(self, *a, **k):
        pass

    def fetch_messages(self, **kwargs):
        return [
            RecruiterMessage(
                message_id="m1",
                sender_name="Recruiter A",
                subject="Vaga Python",
                body_text="Temos uma vaga.",
                received_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            RecruiterMessage(
                message_id="m2",
                sender_name="Recruiter B",
                subject="Vaga Java",
                body_text="Outra vaga.",
                received_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]

    def mark_as_read(self, message_id):
        pass


class FakeClassifier:
    def __init__(self, *a, **k):
        pass

    def classify(self, message):
        return Classification(is_opportunity=True, confidence=0.9, reason="teste")


class FakeExtractor:
    def __init__(self, *a, **k):
        pass

    def extract(self, message):
        return ProposalExtraction(
            company_client="Acme", role_title="SRE", summary="s", confidence=0.9
        )


class FakeNotionBoard:
    def __init__(self, *a, **k):
        self.pages = {}

    def upsert_proposal(self, fingerprint, extraction, message, enrichment, assessment):
        page_id = f"page-{fingerprint}"
        self.pages[fingerprint] = page_id
        return page_id


def _patch_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "state_db_path", tmp_path / "state.db")
    monkeypatch.setattr(Config, "validate", classmethod(lambda cls: None))
    monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
    monkeypatch.setattr(Config, "jev_model", "")
    monkeypatch.setattr(main_module, "GmailClient", FakeGmailClient)
    monkeypatch.setattr(main_module, "Classifier", FakeClassifier)
    monkeypatch.setattr(main_module, "Extractor", FakeExtractor)
    monkeypatch.setattr(main_module, "NotionBoard", FakeNotionBoard)


class TestDryRunHasNoSideEffects:
    def test_dry_run_leaves_processed_emails_untouched(self, monkeypatch, tmp_path):
        _patch_pipeline(monkeypatch, tmp_path)

        result = runner.invoke(
            main_module.app,
            ["run", "--dry-run", "--skip-enrichment", "--skip-assessment"],
        )

        assert result.exit_code == 0, result.output
        db = StateDB(db_path=tmp_path / "state.db")
        assert not db.is_email_processed("m1")
        assert not db.is_email_processed("m2")
        assert db.get_last_run_at() is None

    def test_dry_run_then_real_run_processes_same_messages(self, monkeypatch, tmp_path):
        _patch_pipeline(monkeypatch, tmp_path)

        preview = runner.invoke(
            main_module.app,
            ["run", "--dry-run", "--skip-enrichment", "--skip-assessment"],
        )
        assert preview.exit_code == 0, preview.output

        real = runner.invoke(
            main_module.app,
            ["run", "--skip-enrichment", "--skip-assessment"],
        )
        assert real.exit_code == 0, real.output
        assert "Propostas encontradas" in real.output

        db = StateDB(db_path=tmp_path / "state.db")
        assert db.is_email_processed("m1")
        assert db.is_email_processed("m2")
        assert db.get_last_run_at() is not None
