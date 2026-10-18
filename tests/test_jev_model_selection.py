"""Testes para o wiring do Jev (decisions model) em Classifier/Assessor."""

from datetime import UTC, datetime

from recruiter_pipeline.assessor import Assessor
from recruiter_pipeline.classifier import Classifier
from recruiter_pipeline.config import Config
from recruiter_pipeline.models import OpportunityAssessment, RecruiterMessage


class FakeJev:
    """Stub de JevClient -- devolve answers fixas, sem HTTP."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return self.answers


class FakeLLM:
    def __init__(self, result=None):
        self.model = "fake-model"
        self.result = result

    def complete(self, **kwargs):
        return self.result


def _message():
    return RecruiterMessage(
        message_id="m1",
        sender_name="R",
        subject="Vaga Python",
        body_text="Temos uma vaga para Python engineer.",
        received_at=datetime.now(UTC),
    )


class TestClassifierJev:
    def test_uses_jev_when_injected_and_skips_llm(self):
        jev = FakeJev({"is_opportunity": {"type": "noul", "noul": 0.87}})
        classifier = Classifier(jev=jev)

        assert classifier.using_jev
        result = classifier.classify(_message())

        assert result.is_opportunity is True
        assert result.confidence == 0.87
        assert "Jev" in result.reason
        assert len(jev.calls) == 1

    def test_below_threshold_is_not_opportunity(self):
        jev = FakeJev({"is_opportunity": {"type": "noul", "noul": 0.2}})
        classifier = Classifier(jev=jev)

        result = classifier.classify(_message())

        assert result.is_opportunity is False
        assert result.confidence == 0.8  # 1 - 0.2

    def test_uses_jev_from_config_when_not_injected(self, monkeypatch):
        monkeypatch.setattr(Config, "jev_model", "typesafe/jev-1.13")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        classifier = Classifier()
        assert classifier.using_jev

    def test_falls_back_to_llm_when_jev_model_unset(self, monkeypatch):
        monkeypatch.setattr(Config, "jev_model", "")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        classifier = Classifier()
        assert not classifier.using_jev
        assert classifier.llm is not None

    def test_explicit_llm_used_for_generative_path_when_jev_unset(self, monkeypatch):
        monkeypatch.setattr(Config, "jev_model", "")
        fake_llm = FakeLLM()
        classifier = Classifier(llm=fake_llm)
        assert not classifier.using_jev
        assert classifier.llm is fake_llm

    def test_explicit_llm_does_not_disable_configured_jev(self, monkeypatch):
        # llm partilhado (p.ex. para agregar call_count no run inteiro) e jev
        # são independentes -- só o jev/Config.jev_model decide se o Jev liga.
        monkeypatch.setattr(Config, "jev_model", "typesafe/jev-1.13")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        fake_llm = FakeLLM()
        classifier = Classifier(llm=fake_llm)
        assert classifier.using_jev
        assert classifier.llm is fake_llm


class TestAssessorJev:
    def _assessor(self, jev=None, llm_result=None):
        return Assessor(
            llm=FakeLLM(llm_result or OpportunityAssessment()),
            jev=jev,
            profile="perfil de teste",
        )

    def test_jev_overrides_opportunity_type_and_action(self):
        jev = FakeJev(
            {
                "opportunity_type": {"type": "choice", "choice": "job_opportunity"},
                "recommended_action": {"type": "choice", "choice": "apply"},
            }
        )
        llm_result = OpportunityAssessment(
            opportunity_type="unknown",
            recommended_action="monitor",
            fit_score=80,
            action_reason="bom fit",
        )
        assessor = self._assessor(jev=jev, llm_result=llm_result)

        result = assessor.assess(_message(), _extraction(), None)

        assert result.opportunity_type == "job_opportunity"
        assert result.recommended_action == "apply"
        # Campos de texto/score vêm do LLM, intocados pelo Jev.
        assert result.fit_score == 80
        # Jev mudou monitor->apply: o action_reason fica limpo (é o que o
        # card mostra); o rasto do override vai para decision_trace (DB).
        assert result.action_reason == "bom fit"
        assert result.action_source == "jev"
        assert "jev: monitor → apply" in result.decision_trace
        assert len(jev.calls) == 1

    def test_jev_same_action_leaves_reason_unchanged(self):
        jev = FakeJev(
            {
                "opportunity_type": {"type": "choice", "choice": "job_opportunity"},
                "recommended_action": {"type": "choice", "choice": "monitor"},
            }
        )
        llm_result = OpportunityAssessment(
            recommended_action="monitor", action_reason="bom fit"
        )
        assessor = self._assessor(jev=jev, llm_result=llm_result)

        result = assessor.assess(_message(), _extraction(), None)

        assert result.action_reason == "bom fit"

    def test_low_confidence_jev_choice_keeps_llm_result(self):
        """Confidence < 0.5 no Choice do Jev -- fica a escolha do LLM."""
        jev = FakeJev(
            {
                "opportunity_type": {
                    "type": "choice",
                    "choice": "generic",
                    "confidence": 0.2,
                },
                "recommended_action": {
                    "type": "choice",
                    "choice": "ignore",
                    "confidence": 0.3,
                },
            }
        )
        llm_result = OpportunityAssessment(
            opportunity_type="job_opportunity",
            recommended_action="apply",
            action_reason="bom fit",
        )
        assessor = self._assessor(jev=jev, llm_result=llm_result)

        result = assessor.assess(_message(), _extraction(), None)

        assert result.opportunity_type == "job_opportunity"
        assert result.recommended_action == "apply"
        assert result.action_reason == "bom fit"

    def test_missing_confidence_still_overrides(self):
        """Confidence ausente (opcional na API) -- confia e substitui."""
        jev = FakeJev(
            {
                "opportunity_type": {"type": "choice", "choice": "job_opportunity"},
                "recommended_action": {"type": "choice", "choice": "apply"},
            }
        )
        llm_result = OpportunityAssessment(
            opportunity_type="unknown", recommended_action="monitor"
        )
        assessor = self._assessor(jev=jev, llm_result=llm_result)

        result = assessor.assess(_message(), _extraction(), None)

        assert result.opportunity_type == "job_opportunity"
        assert result.recommended_action == "apply"

    def test_no_jev_keeps_llm_result_unchanged(self):
        llm_result = OpportunityAssessment(
            opportunity_type="job_opportunity", recommended_action="reply"
        )
        assessor = self._assessor(jev=None, llm_result=llm_result)

        result = assessor.assess(_message(), _extraction(), None)

        assert result.opportunity_type == "job_opportunity"
        assert result.recommended_action == "reply"


def _extraction():
    from recruiter_pipeline.models import ProposalExtraction

    return ProposalExtraction(
        company_client="Acme", role_title="SRE", summary="s", confidence=0.9
    )
