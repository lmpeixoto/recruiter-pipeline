"""Classifica mensagens do LinkedIn como propostas ou não."""

from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import Classification, LinkedInMessage

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "classify.txt"


class Classifier:
    """Classifica uma mensagem do LinkedIn."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        # is_opportunity + confidence é exatamente uma decisão tipada --
        # usa JEV_MODEL quando definido (ex. "typesafe/jev-1.14"), senão
        # cai no modelo generativo normal (Config.openrouter_model).
        self.llm = llm or LLMClient(model=Config.jev_model or None)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def classify(self, message: LinkedInMessage) -> Classification:
        """Classifica a mensagem como proposta de emprego ou não."""
        user_prompt = self._build_prompt(message)
        return self.llm.complete(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            response_model=Classification,
        )

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
