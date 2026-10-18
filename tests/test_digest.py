from datetime import UTC, datetime

from recruiter_pipeline.digest import analyze_feedback, build_digest
from recruiter_pipeline.models import (
    Classification,
    OpportunityAssessment,
    Proposal,
    ProposalExtraction,
    RecruiterMessage,
)


def _proposal(company, action, fit):
    return Proposal(
        message=RecruiterMessage(
            message_id=company, sender_name="r", subject="s", body_text="b",
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        classification=Classification(is_opportunity=True, confidence=1, reason="x"),
        extraction=ProposalExtraction(company_client=company, role_title="SRE", summary="s", confidence=1),
        assessment=OpportunityAssessment(recommended_action=action, fit_score=fit, fit_confidence=70),
    )


def test_digest_lists_best_replies_first_and_counts_info_requests():
    title, body = build_digest(
        [_proposal("A", "reply", 60), _proposal("B", "apply", 90),
         _proposal("C", "ask_for_more_information", None), _proposal("D", "ignore", 10)]
    )
    assert title.startswith("4 propostas")
    lines = body.splitlines()
    assert lines[0].startswith("B") and lines[1].startswith("A")
    assert "1 a precisar de mais informação" in body


def test_digest_empty():
    assert build_digest([])[1] == "Sem propostas novas."


def test_feedback_analysis():
    cards = [
        {"estado": "Entrevista", "fit": 80, "triagem": "🟢 Responder"},
        {"estado": "Recruiter screen", "fit": 40, "triagem": "🔴 Descartar"},
        {"estado": "Rejeitada", "fit": 70, "triagem": "🟢 Responder", "motivo": "Salário"},
        {"estado": "Arquivada", "fit": 30, "triagem": "🔴 Descartar", "motivo": "Salário"},
        {"estado": "Rejeitada", "fit": 50, "triagem": "🟡 Pedir info"},
        {"estado": "Nova", "fit": 90},
    ]
    r = analyze_feedback(cards)
    assert r["advanced"] == 2 and r["rejected"] == 3
    assert r["reasons"]["Salário"] == 2
    assert r["rejected_without_reason"] == 1
    assert len(r["optimistic"]) == 1 and len(r["pessimistic"]) == 1
    assert r["avg_fit_advanced"] == 60.0
