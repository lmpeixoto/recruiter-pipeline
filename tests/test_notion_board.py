"""Testes para helpers do NotionBoard (sem chamadas à API)."""

from datetime import UTC, datetime

from recruiter_pipeline.models import (
    CompanyEnrichment,
    ProposalExtraction,
    RecruiterMessage,
)
from recruiter_pipeline.notion_board import NotionBoard


def _extraction(agency=None, company_client=None):
    return ProposalExtraction(
        agency=agency, company_client=company_client, summary="s", confidence=0.9
    )


def _message():
    return RecruiterMessage(
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



class _FakeBlocks:
    def __init__(self, children):
        self.children_store = children
        self.deleted, self.appended, self.updated = [], [], []
        outer = self

        class _Children:
            def list(self, **kw):
                return {"results": outer.children_store, "has_more": False}

            def append(self, block_id, children, after=None):
                outer.appended.append((children, after))

        self.children = _Children()

    def delete(self, block_id):
        self.deleted.append(block_id)

    def update(self, block_id, **kw):
        self.updated.append((block_id, kw))


def _board_with(children):
    board = NotionBoard.__new__(NotionBoard)
    board.client = type("C", (), {"blocks": _FakeBlocks(children)})()
    return board


def _anchor(id_="anchor"):
    return {"id": id_, "type": "callout", "callout": {"icon": {"emoji": "🤖"}}}


def _notes(id_="notes"):
    return {
        "id": id_,
        "type": "heading_2",
        "heading_2": {"rich_text": [{"plain_text": "Minhas notas"}]},
    }


class TestRefreshPreservesUserNotes:
    def test_only_generated_blocks_between_anchor_and_notes_are_replaced(self):
        children = [
            _anchor(),
            {"id": "gen1", "type": "paragraph"},
            {"id": "gen2", "type": "paragraph"},
            _notes(),
            {"id": "user-note", "type": "paragraph"},
        ]
        board = _board_with(children)
        new = NotionBoard._build_page_content.__get__(board)  # noqa: F841 (smoke)
        new_blocks = [NotionBoard._anchor_block(_message()), {"object": "block", "type": "paragraph"}]
        board._refresh_page_content("page", new_blocks)
        blocks = board.client.blocks
        assert blocks.deleted == ["gen1", "gen2"]  # notas e bloco do utilizador intactos
        assert blocks.appended == [(new_blocks[1:], "anchor")]
        assert blocks.updated[0][0] == "anchor"

    def test_legacy_card_is_rewritten_and_gains_notes_zone(self):
        board = _board_with([{"id": "old1", "type": "paragraph"}])
        new_blocks = [NotionBoard._anchor_block(_message())]
        board._refresh_page_content("page", new_blocks)
        blocks = board.client.blocks
        assert blocks.deleted == ["old1"]
        sent, after = blocks.appended[0]
        assert after is None
        assert sent[-2]["heading_2"]["rich_text"][0]["text"]["content"] == "Minhas notas"


class TestCardContent:
    def test_triage_property_and_reply_draft_and_no_debug_in_card(self):
        from recruiter_pipeline.models import OpportunityAssessment

        board = NotionBoard.__new__(NotionBoard)
        assessment = OpportunityAssessment(
            recommended_action="ask_for_more_information",
            fit_score=70,
            fit_confidence=30,
            action_reason="Falta salário",
            reply_draft="Olá, obrigado pelo contacto. Qual o intervalo salarial?",
            decision_trace="jev: ignore → ask_for_more_information",
        )
        extraction = _extraction(company_client="Acme")
        props = board._build_properties(extraction, _message(), None, assessment)
        assert props["Triagem"]["select"]["name"].startswith("🟡")
        text = str(board._build_page_content(_message(), extraction, None, assessment))
        assert "Rascunho de resposta" in text
        assert "provisório" in text
        assert "jev:" not in text  # o rasto de debug nunca vai para o card
