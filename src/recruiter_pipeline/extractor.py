"""Extrai dados estruturados de propostas de emprego."""

from pathlib import Path

from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import LinkedInMessage, ProposalExtraction

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "extract.txt"


class Extractor:
    """Extrai informação estruturada de uma mensagem de recruiter."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def extract(self, message: LinkedInMessage) -> ProposalExtraction:
        """Extrai os campos estruturados da proposta."""
        user_prompt = self._build_prompt(message)
        extraction = self.llm.complete(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            response_model=ProposalExtraction,
        )
        extraction = self._sanitize(extraction)
        # Preenche campos que o LLM possa ter deixado vazios mas que sabemos
        if not extraction.recruiter_name:
            extraction.recruiter_name = message.sender_name
        if not extraction.recruiter_headline:
            extraction.recruiter_headline = message.sender_headline
        # Usa o LinkedIn da empresa detetado no HTML como fallback
        if not extraction.company_linkedin and message.company_linkedin_guess:
            extraction.company_linkedin = message.company_linkedin_guess
        return extraction

    @staticmethod
    def _sanitize(extraction: ProposalExtraction) -> ProposalExtraction:
        """Converte strings 'null'/'None'/vazias devolvidas pelo LLM em None real."""
        for field in extraction.model_fields:
            value = getattr(extraction, field)
            if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
                setattr(extraction, field, None)
        return extraction

    def _build_prompt(self, message: LinkedInMessage) -> str:
        parts = [
            f"Assunto: {message.subject}",
            f"Remetente: {message.sender_name}",
        ]
        if message.sender_headline:
            parts.append(f"Headline: {message.sender_headline}")
        parts.append(f"Data: {message.received_at.isoformat()}")
        parts.append("---")
        parts.append(message.body_text)
        return "\n".join(parts)
