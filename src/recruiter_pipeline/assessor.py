"""Avalia oportunidades face ao perfil do candidato."""

import json
from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.jev_client import JevClient
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import (
    CompanyEnrichment,
    OpportunityAssessment,
    ProposalExtraction,
    RecruiterMessage,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "assess.txt"

OPPORTUNITY_TYPE_OPTIONS = {
    "job_opportunity": "Vaga concreta: role + empresa ou job description identificável.",
    "recruiter_outreach": "Contacto exploratório de recrutador, sem vaga concreta.",
    "connection": "Apenas um convite de ligação, sem vaga.",
    "referral": "Alguém está a referenciar o candidato para uma oportunidade.",
    "generic": "Mensagem em massa, sem personalização.",
    "unknown": "Não há informação suficiente para decidir.",
}

JEV_MIN_CONFIDENCE = 0.5

RECOMMENDED_ACTION_OPTIONS = {
    "reply": "Fit parece bom, vale responder.",
    "ask_for_more_information": "Fit potencial bom mas faltam dados críticos (empresa, salário, remote).",
    "apply": "Vaga concreta com excelente fit e dados suficientes.",
    "ignore": "Claramente não encaixa (dealbreaker evidente).",
    "monitor": "Incerto mas não urgente.",
    "connect": "Convite de ligação sem vaga.",
}


class Assessor:
    """Avalia fit, confiança e ação recomendada para uma oportunidade.

    opportunity_type e recommended_action são escolhas de um conjunto fechado
    -- decisões tipadas, mapeadas para perguntas Choice do Jev. O resto
    (scores 0-100 por dimensão, action_reason, questions_to_ask) é
    texto/numérico livre e fica sempre no LLM generativo: o Jev não devolve
    texto nem um score contínuo, só uma escolha + confiança. Quando
    Config.jev_model está definido, cada avaliação faz uma chamada ao LLM
    (scores/texto, como sempre) e uma ao Jev (as duas decisões), e o
    resultado do Jev substitui o do LLM nesses dois campos -- a menos que o
    Jev devolva confidence < JEV_MIN_CONFIDENCE, caso em que fica a escolha
    do LLM (confidence é opcional na resposta do Jev; ausente = confia).
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        jev: JevClient | None = None,
        profile: str | None = None,
    ) -> None:
        self.llm = llm or LLMClient()
        self.jev = jev if jev is not None else (JevClient() if Config.jev_model else None)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        if profile is None:
            profile = self._load_profile()
        self.profile = profile

    @property
    def using_jev(self) -> bool:
        return self.jev is not None

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
        message: RecruiterMessage,
        extraction: ProposalExtraction,
        enrichment: CompanyEnrichment | None,
    ) -> OpportunityAssessment | None:
        """Avalia a oportunidade. Devolve None se não houver perfil configurado."""
        if not self.profile:
            return None

        user_prompt = self._build_prompt(message, extraction, enrichment)
        assessment = self.llm.complete(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            response_model=OpportunityAssessment,
        )
        if self.jev:
            self._apply_jev_decisions(assessment, user_prompt)
        return assessment

    def _apply_jev_decisions(
        self, assessment: OpportunityAssessment, user_prompt: str
    ) -> None:
        answers = self.jev.decide(
            state={"context": user_prompt},
            questions={
                "opportunity_type": JevClient.choice(
                    "Classifica o tipo desta oportunidade com base no perfil do "
                    "candidato e nos dados extraídos da mensagem.",
                    OPPORTUNITY_TYPE_OPTIONS,
                ),
                "recommended_action": JevClient.choice(
                    "Decide a ação recomendada para o candidato face a esta "
                    "oportunidade, dado o perfil e o fit descrito.",
                    RECOMMENDED_ACTION_OPTIONS,
                ),
            },
        )
        type_answer = answers["opportunity_type"]
        type_confidence = type_answer.get("confidence")
        if type_confidence is None or type_confidence >= JEV_MIN_CONFIDENCE:
            assessment.opportunity_type = type_answer["choice"]

        action_answer = answers["recommended_action"]
        action_confidence = action_answer.get("confidence")
        if action_confidence is None or action_confidence >= JEV_MIN_CONFIDENCE:
            jev_action = action_answer["choice"]
            if jev_action != assessment.recommended_action:
                # O action_reason do LLM foi escrito a justificar a ação original --
                # sem esta nota, o card no Notion mostra a ação do Jev ao lado de
                # uma justificação que pode argumentar o contrário.
                assessment.action_reason = (
                    f"[Jev mudou a ação de '{assessment.recommended_action}' para "
                    f"'{jev_action}'] {assessment.action_reason}"
                ).strip()
            assessment.recommended_action = jev_action

    def _build_prompt(
        self,
        message: RecruiterMessage,
        extraction: ProposalExtraction,
        enrichment: CompanyEnrichment | None,
    ) -> str:
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
        return "\n".join(parts)
