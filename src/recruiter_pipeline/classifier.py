"""Classifica mensagens do LinkedIn como propostas ou não."""

from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.jev_client import JevClient
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import Classification, RecruiterMessage

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "classify.txt"

IS_OPPORTUNITY_QUESTION = (
    "A mensagem seguinte é uma proposta de emprego, contacto de recrutamento ou "
    "pedido de entrevista de um recruiter/talent acquisition? Considera proposta "
    "quando menciona vaga, cargo, oportunidade concreta, ou é claramente um "
    "recruiter a contactar sobre uma posição (mesmo que a mensagem seja curta). "
    "NÃO é proposta: vendas/marketing/InMail promocional, convites de conexão "
    "puramente sociais, networking sem intenção de recrutamento, agradecimentos "
    "ou mensagens de RH administrativas. Em caso de dúvida, tende para 'sim'."
)


class Classifier:
    """Classifica uma mensagem do LinkedIn.

    is_opportunity é uma decisão binária pura -- o caso ideal para testar um
    decisions model (Jev) isolado, sem geração de texto nenhuma. Quando
    Config.jev_model está definido, a classificação inteira corre no Jev via
    /api/alpha/decisions (uma pergunta Noul) e NÃO há chamada ao LLM
    generativo -- é o ponto do teste de eficiência. O resto do pipeline
    (Extractor/Assessor) continua a usar o LLM normal.

    llm e jev são independentes (mesmo padrão do Assessor) -- partilhar um
    LLMClient (para agregar call_count/total_tokens entre Classifier/
    Extractor/Assessor) não desliga o Jev; isso é só o param/config jev.
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        jev: JevClient | None = None,
    ) -> None:
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.jev = jev if jev is not None else (JevClient() if Config.jev_model else None)
        # Só constrói um LLMClient real se vai ser mesmo usado -- com Jev
        # ativo, classify() nunca toca em self.llm.
        self.llm = llm or (None if self.jev else LLMClient())

    @property
    def using_jev(self) -> bool:
        return self.jev is not None

    def classify(self, message: RecruiterMessage) -> Classification:
        """Classifica a mensagem como proposta de emprego ou não."""
        if self.jev:
            return self._classify_with_jev(message)
        return self.llm.complete(
            system_prompt=self.system_prompt,
            user_prompt=self._build_prompt(message),
            response_model=Classification,
        )

    def _classify_with_jev(self, message: RecruiterMessage) -> Classification:
        answers = self.jev.decide(
            state={"message": self._build_prompt(message)},
            questions={"is_opportunity": JevClient.noul(IS_OPPORTUNITY_QUESTION)},
        )
        probability = answers["is_opportunity"]["noul"]
        is_opportunity = probability >= 0.5
        return Classification(
            is_opportunity=is_opportunity,
            confidence=probability if is_opportunity else 1.0 - probability,
            reason=f"Jev (noul): P(oportunidade)={probability:.2f}",
        )

    def _build_prompt(self, message: RecruiterMessage) -> str:
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
