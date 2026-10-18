"""Cliente para o endpoint de decisões (System One / Jev) via OpenRouter.

Jev não é um modelo de chat/completions -- é um "decisions model": recebe um
`state` (contexto) e um conjunto de perguntas tipadas e devolve uma resposta
por pergunta com uma probabilidade/confiança calibrada, nunca texto livre.
Chamar isto via `/chat/completions` (como uma primeira versão deste projeto
tentou) falha com 400: "is a decisions model and cannot be used with the
chat/completions endpoint".

Tipos de pergunta suportados pelo endpoint:
- noul: sim/não. Resposta `answer["noul"]` é a probabilidade (0-1) do "sim".
- choice: escolha fechada. Resposta `answer["choice"]` é a chave escolhida
  de `criteria`; `answer.get("confidence")` pode vir ausente.
- score: rubric ordenado (poucos níveis). Resposta `answer["score"]`.

Ver https://docs.typesafe.ai/api e o cookbook do OpenRouter
"jev-classification" para os exemplos reais de request/response.
"""

import httpx

from recruiter_pipeline.config import Config

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class JevError(RuntimeError):
    """Erro a chamar o endpoint de decisões do Jev."""


class JevClient:
    """Cliente fino para POST /api/alpha/decisions (API alpha do OpenRouter).

    Mesma OPENROUTER_API_KEY do LLMClient normal -- é a mesma conta, endpoint
    diferente.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or Config.openrouter_api_key
        self.model = model or Config.jev_model or "typesafe/jev-1.13"
        self.timeout = timeout
        self.call_count = 0
        self.total_cost = 0.0

    def decide(self, state: dict, questions: dict) -> dict:
        """Envia state+questions, devolve o dict `answers` da resposta.

        Acumula custo/contagem em self.total_cost/self.call_count para
        permitir reportar a eficiência do modelo no fim de um run.
        """
        response = httpx.post(
            DECISIONS_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model, "state": state, "questions": questions},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise JevError(
                f"/api/alpha/decisions devolveu {response.status_code}: "
                f"{response.text[:500]}"
            )
        data = response.json()
        answers = data.get("answers")
        if not answers:
            raise JevError(f"Resposta sem 'answers': {data}")

        usage = data.get("usage") or {}
        self.call_count += 1
        self.total_cost += usage.get("cost", 0.0)
        return answers

    @staticmethod
    def noul(instructions: str) -> dict:
        """Pergunta booleana -- resposta em `answer["noul"]` (probabilidade 0-1)."""
        return {"type": "noul", "instructions": instructions}

    @staticmethod
    def choice(instructions: str, options: dict[str, str]) -> dict:
        """Pergunta de escolha fechada -- resposta em `answer["choice"]`.

        `options` mapeia cada valor possível (igual ao enum usado no resto do
        pipeline) à sua descrição -- é exatamente o `criteria` que o endpoint
        espera.
        """
        return {"type": "choice", "instructions": instructions, "criteria": options}

    @staticmethod
    def score(instructions: str, levels: list[str]) -> dict:
        """Pergunta de rubric -- `levels` ordenados do pior ao melhor.

        Resposta em `answer["score"]`. Não usado pelo pipeline hoje -- os
        fit scores são 0-100 livres, não um rubric fechado de poucos níveis.
        Fica disponível para quando essa escala for redesenhada.
        """
        return {"type": "score", "instructions": instructions, "criteria": levels}
