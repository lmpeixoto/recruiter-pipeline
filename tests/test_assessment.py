"""Testes para avaliação de oportunidades e helpers do modelo."""

from datetime import datetime, timezone

from recruiter_pipeline.db import StateDB
from recruiter_pipeline.models import (
    Classification,
    LinkedInMessage,
    OpportunityAssessment,
    Proposal,
    ProposalExtraction,
    _normalize,
    missing_information,
    opportunities_match,
)


class TestMissingInformation:
    def test_full_extraction_has_no_missing(self):
        e = ProposalExtraction(
            company_client="Feedzai",
            role_title="SRE",
            seniority="senior",
            stack=["AWS"],
            salary="€70k",
            remote_policy="remote",
            location="Lisboa",
            contract_type="permanent",
            summary="s",
            confidence=0.9,
        )
        assert missing_information(e) == []

    def test_empty_extraction_lists_everything(self):
        e = ProposalExtraction(summary="s", confidence=0.5)
        missing = missing_information(e)
        assert "empresa cliente" in missing
        assert "intervalo salarial" in missing
        assert "política remote" in missing
        assert len(missing) == 8


class TestNormalize:
    def test_strips_accents_and_punctuation(self):
        assert _normalize("Banco de Portugal, S.A.") == "banco de portugal s a"

    def test_case_and_whitespace(self):
        assert _normalize("  Feedzai  ") == "feedzai"


def _make_proposal(company: str, role: str) -> Proposal:
    msg = LinkedInMessage(
        message_id="m1",
        sender_name="R",
        subject="s",
        body_text="b",
        received_at=datetime.now(timezone.utc),
    )
    return Proposal(
        message=msg,
        classification=Classification(is_opportunity=True, confidence=1.0, reason="r"),
        extraction=ProposalExtraction(
            company_client=company, role_title=role, summary="s", confidence=0.9
        ),
    )


class TestFingerprint:
    def test_same_opportunity_different_formatting_matches(self):
        a = _make_proposal("Banco de Portugal", "Platform Engineer")
        b = _make_proposal("banco de portugal", "platform engineer")
        assert a.fingerprint() == b.fingerprint()

    def test_different_roles_differ(self):
        a = _make_proposal("Feedzai", "SRE")
        b = _make_proposal("Feedzai", "DevOps")
        assert a.fingerprint() != b.fingerprint()


class TestOpportunitiesMatch:
    def test_same_company_similar_role_matches(self):
        assert opportunities_match(
            "Acme", "Senior SRE", None, None,
            "Acme", "SRE", None, None,
        )

    def test_different_company_no_match(self):
        assert not opportunities_match(
            "Acme", "SRE", None, None,
            "Beta", "SRE", None, None,
        )

    def test_conflicting_location_no_match(self):
        assert not opportunities_match(
            "Acme", "SRE", "Lisboa", None,
            "Acme", "SRE", "Porto", None,
        )

    def test_conflicting_contract_no_match(self):
        assert not opportunities_match(
            "Acme", "SRE", None, "permanent",
            "Acme", "SRE", None, "contract",
        )

    def test_same_location_matches(self):
        assert opportunities_match(
            "Acme", "SRE", "Porto", "permanent",
            "Acme", "SRE", "Porto", "permanent",
        )


class TestFindProposalFuzzy:
    def test_fuzzy_match_migrates_and_returns_page(self, tmp_path):
        db = StateDB(db_path=tmp_path / "s.db")
        # Proposta antiga sem location/contrato (fingerprint de 2 partes)
        db.upsert_proposal(
            fingerprint="acme::senior platform engineer",
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title="Senior Platform Engineer",
        )
        # Nova com role sem "Senior" e location preenchida → fuzzy match
        page_id, fp = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="Platform Engineer",
            location="Porto",
            contract_type=None,
        )
        assert page_id == "page-1"
        assert fp == "acme::platform engineer::porto::"

    def test_no_match_returns_new_fingerprint(self, tmp_path):
        db = StateDB(db_path=tmp_path / "s.db")
        page_id, fp = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="SRE",
            location=None,
            contract_type=None,
        )
        assert page_id is None
        assert fp == "acme::sre::::"


class TestDimensionsMarkdown:
    def test_only_known_dimensions(self):
        a = OpportunityAssessment(role_fit=80, stack_fit=70)
        lines = a.dimensions_markdown()
        assert any("Role: 80" in line for line in lines)
        assert any("Stack: 70" in line for line in lines)
        assert not any("Remote" in line for line in lines)

    def test_all_unknown_gives_empty(self):
        assert OpportunityAssessment().dimensions_markdown() == []
