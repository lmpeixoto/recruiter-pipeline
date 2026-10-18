"""Avalia oportunidades face ao perfil do candidato."""

import json
from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import (
    CompanyEnrichment,
    LinkedInMessage,
    OpportunityAssessment,
    ProposalExtraction,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "assess.txt"


class Assessor:
    """Avalia fit, confiança e ação recomendada para uma oportunidade."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        profile: str | None = None,
    ) -> None:
        # Os scores 0-100 + a ação de conjunto fechado são decisão tipada --
        # mesma lógica do Classifier: usa JEV_MODEL quando definido.
        # action_reason/questions_to_ask ficam texto livre de qualquer forma,
        # o modelo escolhido devolve-os como string normal dentro do JSON.
        self.llm = llm or LLMClient(model=Config.jev_model or None)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        if profile is None:
            profile = self._load_profile()
        self.profile = profile

    @staticmethod
    def _load_profile() -> str | None:
        path = Config.profile_path
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    @property
    def available(self) -> bool:
        return bool(self.profile)

    def assess(
        self,
        message: LinkedInMessage,
        extraction: ProposalExtraction,
        enrichment: CompanyEnrichment | None,
    ) -> OpportunityAssessment | None:
        """Avalia a oportunidade. Devolve None se não houver perfil configurado."""
        if not self.profile:
            return None

        parts = [
            "PERFIL DO CANDIDATO:",
            self.profile,
            "",
            "DADOS EXTRAÍDOS DA MENSAGEM (JSON):",
            json.dumps(extraction.model_dump(), ensure_ascii=False, default=str),
        ]
        if enrichment:
            parts += [
                "",
                "ENRIQUECIMENTO WEB SOBRE A EMPRESA:",
                enrichment.to_markdown(),
            ]
        parts += ["", "MENSAGEM ORIGINAL:", message.body_text[:4000]]

        return self.llm.complete(
            system_prompt=self.system_prompt,
            user_prompt="\n".join(parts),
            response_model=OpportunityAssessment,
        )
