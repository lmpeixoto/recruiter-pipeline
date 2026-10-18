"""Testes para helpers do NotionBoard (sem chamadas à API)."""

from datetime import UTC, datetime

from recruiter_pipeline.models import (
    CompanyEnrichment,
    LinkedInMessage,
    ProposalExtraction,
)
from recruiter_pipeline.notion_board import NotionBoard


def _extraction(agency=None, company_client=None):
    return ProposalExtraction(
        agency=agency, company_client=company_client, summary="s", confidence=0.9
    )


def _message():
    return LinkedInMessage(
        message_id="m1",
        sender_name="R",
        subject="s",
        body_text="b",
        received_at=datetime.now(UTC),
    )


class TestIsConsultancy:
    def test_agency_without_client_is_consultancy(self):
        assert NotionBoard._is_consultancy(_extraction(agency="Hays"), None)

    def test_product_company_not_consultancy(self):
        e = CompanyEnrichment(name="Feedzai", industry="Software Development")
        assert not NotionBoard._is_consultancy(
            _extraction(company_client="Feedzai"), e
        )

    def test_consulting_industry_is_consultancy(self):
        e = CompanyEnrichment(name="X", industry="IT Services and IT Consulting")
        assert NotionBoard._is_consultancy(_extraction(company_client="X"), e)

    def test_recruitment_industry_is_consultancy(self):
        e = CompanyEnrichment(name="Y", industry="Staffing and Recruiting")
        assert NotionBoard._is_consultancy(_extraction(company_client="Y"), e)


class TestStripEmailFooter:
    def test_strips_intended_for_footer(self):
        body = "Olá, temos uma vaga.\n\nThis email was intended for Luís\nUnsubscribe: x"
        assert NotionBoard._strip_email_footer(body) == "Olá, temos uma vaga."

    def test_strips_separator_footer(self):
        body = "Mensagem aqui.\n\n----------------------------------------\nYou are receiving LinkedIn"
        assert NotionBoard._strip_email_footer(body) == "Mensagem aqui."

    def test_no_footer_unchanged(self):
        body = "Só conteúdo relevante."
        assert NotionBoard._strip_email_footer(body) == body


class TestSanitizeSelectOption:
    def test_replaces_commas(self):
        assert (
            NotionBoard._sanitize_select_option("Financial technology, enterprise software")
            == "Financial technology / enterprise software"
        )

    def test_truncates_to_100(self):
        assert len(NotionBoard._sanitize_select_option("x" * 150)) == 100

    def test_no_comma_unchanged(self):
        assert NotionBoard._sanitize_select_option("Software") == "Software"


class TestEnrichmentBlocks:
    def test_links_are_clickable(self):
        board = NotionBoard.__new__(NotionBoard)  # sem __init__ (evita API)
        e = CompanyEnrichment(
            name="Acme",
            website="https://acme.com",
            linkedin_url="https://linkedin.com/company/acme",
            industry="Software",
        )
        blocks = board._enrichment_blocks(e)
        # Procura um bloco com link
        found_link = False
        for b in blocks:
            for rt in b.get(b["type"], {}).get("rich_text", []):
                if rt.get("text", {}).get("link"):
                    found_link = True
        assert found_link

    def test_heading_present(self):
        board = NotionBoard.__new__(NotionBoard)
        e = CompanyEnrichment(name="Acme")
        blocks = board._enrichment_blocks(e)
        assert blocks[0]["type"] == "heading_2"


class TestEstadoNotStompedOnUpdate:
    """Regressão: _build_properties nunca inclui "Estado" -- só upsert_proposal
    o define, e só no ramo de criação. Sem isto, qualquer update (reprocessar
    um email, ou um fuzzy-match numa proposta já existente) repunha "Nova"
    por cima de um estágio que o humano já tinha avançado à mão no Notion
    (ex.: "Entrevista", "Proposta")."""

    def test_build_properties_never_sets_estado(self):
        board = NotionBoard.__new__(NotionBoard)
        properties = board._build_properties(
            extraction=_extraction(company_client="Acme"),
            message=_message(),
            enrichment=None,
        )
        assert "Estado" not in properties

