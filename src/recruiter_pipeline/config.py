"""Configuração e variáveis de ambiente."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configurações carregadas do ambiente."""

    project_root = Path(__file__).resolve().parent.parent.parent

    gmail_credentials_path: Path = Path(
        os.getenv("GMAIL_CREDENTIALS_PATH", project_root / "credentials.json")
    )
    gmail_token_path: Path = Path(
        os.getenv("GMAIL_TOKEN_PATH", project_root / "token.json")
    )

    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv(
        "OPENROUTER_MODEL", "openai/gpt-4o-mini"
    )
    # Modelo opcional de decisões tipadas (Classifier, Assessor) -- ex.
    # "typesafe/jev-1.13" via OpenRouter. Vazio por omissão: Classifier usa
    # openrouter_model (LLM normal) e Assessor nunca chama o Jev. Quando
    # definido, chama /api/alpha/decisions (ver jev_client.py) -- NÃO é um
    # model= normal de chat/completions. A extração (Extractor) nunca usa
    # isto -- tem campos de texto livre que um decisions model não gera.
    jev_model: str = os.getenv("JEV_MODEL", "")

    notion_token: str = os.getenv("NOTION_TOKEN", "")
    notion_parent_page_id: str = os.getenv("NOTION_PARENT_PAGE_ID", "")

    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")

    state_db_path: Path = Path(
        os.getenv("STATE_DB_PATH", project_root / "state.db")
    )

    profile_path: Path = Path(
        os.getenv("PROFILE_PATH", project_root / "profile.md")
    )

    @classmethod
    def validate(cls) -> None:
        """Valida que as variáveis obrigatórias estão definidas."""
        missing = []
        if not cls.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if not cls.notion_token:
            missing.append("NOTION_TOKEN")
        if not cls.notion_parent_page_id:
            missing.append("NOTION_PARENT_PAGE_ID")
        if missing:
            raise RuntimeError(
                f"Variáveis de ambiente em falta: {', '.join(missing)}"
            )