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


class TestFindProposalIdentityGuards:
    """Regressão: duas propostas sem empresa/role não são "a mesma vaga"."""

    def test_no_fuzzy_match_when_company_unknown(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        # Localização diferente força fingerprint diferente -- cai na camada
        # fuzzy em vez do match exato, que é o caminho a testar.
        db.upsert_proposal(
            fingerprint="unknown::unknown::lisboa::",
            notion_page_id="page-1",
            company_client=None,
            agency=None,
            role_title=None,
            location="Lisboa",
        )
        # Outra proposta, empresa/role igualmente desconhecidos -- sem a
        # guarda, "unknown"::"unknown" faz Jaccard=1.0 contra a primeira.
        page_id, fingerprint = db.find_proposal(
            company_client=None,
            agency=None,
            role_title=None,
            location=None,
            contract_type=None,
        )
        assert page_id is None
        assert fingerprint != "unknown::unknown::lisboa::"

    def test_no_fuzzy_match_when_role_missing(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="acme::unknown::lisboa::",
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title=None,
            location="Lisboa",
        )
        page_id, _ = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title=None,
            location=None,
            contract_type=None,
        )
        assert page_id is None

    def test_exact_fingerprint_still_works_with_unknowns(self, tmp_path: Path) -> None:
        """A guarda só bloqueia a camada fuzzy -- o match exato continua."""
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="unknown::unknown::::",
            notion_page_id="page-1",
            company_client=None,
            agency=None,
            role_title=None,
        )
        db.get_proposal_page_id("unknown::unknown::::")
        assert db.get_proposal_page_id("unknown::unknown::::") == "page-1"

    def test_fuzzy_match_still_works_with_real_identity(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="acme::site reliability engineer::::",
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title="Site Reliability Engineer",
        )
        page_id, fingerprint = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="Senior Site Reliability Engineer",
            location=None,
            contract_type=None,
        )
        assert page_id == "page-1"
        assert fingerprint != "acme::site reliability engineer::::"


class TestFingerprintMigrationCascades:
    """Regressão: migrar o fingerprint de uma proposta não pode orfanizar
    as fontes associadas em proposal_sources."""

    def test_proposal_sources_migrates_with_fingerprint(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        old_fingerprint = "acme::site reliability engineer::::"
        db.upsert_proposal(
            fingerprint=old_fingerprint,
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title="Site Reliability Engineer",
            source="LinkedIn DM",
            source_message_id="msg-1",
            source_url="https://example.com/msg-1",
            source_received_at="2026-01-01T00:00:00+00:00",
        )

        page_id, new_fingerprint = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="Senior Site Reliability Engineer",
            location=None,
            contract_type=None,
        )
        assert page_id == "page-1"
        assert new_fingerprint != old_fingerprint

        with db._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint, message_id FROM proposal_sources"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == new_fingerprint
        assert rows[0]["message_id"] == "msg-1"
