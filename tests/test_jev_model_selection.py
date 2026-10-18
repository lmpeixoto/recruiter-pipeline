"""Testes para o wiring do JEV_MODEL nos passos de decisão tipada."""

from recruiter_pipeline.assessor import Assessor
from recruiter_pipeline.classifier import Classifier
from recruiter_pipeline.config import Config


class TestJevModelSelection:
    def test_classifier_uses_jev_model_when_set(self, monkeypatch):
        monkeypatch.setattr(Config, "jev_model", "typesafe/jev-1.14")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        classifier = Classifier()
        assert classifier.llm.model == "typesafe/jev-1.14"

    def test_classifier_falls_back_to_default_model_when_unset(self, monkeypatch):
        monkeypatch.setattr(Config, "jev_model", "")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        monkeypatch.setattr(Config, "openrouter_model", "openai/gpt-4o-mini")
        classifier = Classifier()
        assert classifier.llm.model == "openai/gpt-4o-mini"

    def test_assessor_uses_jev_model_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Config, "jev_model", "typesafe/jev-1.14")
        monkeypatch.setattr(Config, "openrouter_api_key", "test-key")
        profile_path = tmp_path / "profile.md"
        profile_path.write_text("perfil de teste", encoding="utf-8")
        monkeypatch.setattr(Config, "profile_path", profile_path)
        assessor = Assessor()
        assert assessor.llm.model == "typesafe/jev-1.14"

    def test_explicit_llm_override_still_wins(self, monkeypatch):
        """Injetar um LLMClient explícito continua a ignorar JEV_MODEL -- usado
        pelos testes que não querem depender da config global."""
        monkeypatch.setattr(Config, "jev_model", "typesafe/jev-1.14")

        class FakeLLM:
            model = "fake-model"

        classifier = Classifier(llm=FakeLLM())
        assert classifier.llm.model == "fake-model"
