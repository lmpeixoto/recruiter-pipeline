"""Testes para a base de dados de estado."""

from pathlib import Path

from recruiter_pipeline.db import StateDB


def test_dedupe_and_proposal_tracking(tmp_path: Path) -> None:
    db = StateDB(db_path=tmp_path / "test.db")

    assert not db.is_email_processed("msg-1")
    db.mark_email_processed("msg-1", "proposal", fingerprint="acme::sre")
    assert db.is_email_processed("msg-1")

    assert db.get_proposal_page_id("acme::sre") is None
    db.upsert_proposal(
        fingerprint="acme::sre",
        notion_page_id="page-123",
        company_client="Acme",
        agency=None,
        role_title="SRE",
    )
    assert db.get_proposal_page_id("acme::sre") == "page-123"

    db.upsert_proposal(
        fingerprint="acme::sre",
        notion_page_id="page-456",
        company_client="Acme",
        agency=None,
        role_title="SRE",
    )
    assert db.get_proposal_page_id("acme::sre") == "page-456"


def test_company_cache(tmp_path: Path) -> None:
    db = StateDB(db_path=tmp_path / "test.db")
    assert db.get_company_cache("Acme") is None
    db.set_company_cache("Acme", '{"fetched_at": "2025-06-15T10:00:00+00:00"}')
    assert db.get_company_cache("Acme") is not None
