"""Cliente OpenRouter com suporte a structured outputs."""

import json
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from recruiter_pipeline.config import Config

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Cliente simples para OpenRouter."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or Config.openrouter_api_key
        self.model = model or Config.openrouter_model
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key,
        )

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        temperature: float = 0.1,
        max_retries: int = 2,
    ) -> T:
        """Completa um prompt e devolve um modelo Pydantic validado."""
        schema = response_model.model_json_schema()
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    response_format={
                        "type": "json_object",
                    },
                )
                content = response.choices[0].message.content or "{}"
                data = json.loads(content)
                return response_model.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt < max_retries:
                    user_prompt = (
                        user_prompt
                        + "\n\nA tua resposta anterior não era JSON válido ou não respeitava o schema. "
                        "Devolve APENAS um objeto JSON válido."
                    )

        raise RuntimeError(
            f"Falha ao obter resposta válida do LLM após {max_retries + 1} tentativas: {last_error}"
        )
