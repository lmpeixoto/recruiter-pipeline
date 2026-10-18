"""Fixtures e helpers para testes."""

from datetime import UTC, datetime

import pytest

from recruiter_pipeline.config import Config
from recruiter_pipeline.models import RecruiterMessage


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """Os testes não podem depender do .env real (ex.: JEV_MODEL ligava o Jev a sério)."""
    monkeypatch.setattr(Config, "jev_model", "")


def make_message(
    body: str,
    sender_name: str = "João Silva",
    sender_headline: str = "Recruiter at Michael Page",
    subject: str = "João Silva sent you a message",
) -> RecruiterMessage:
    """Cria uma RecruiterMessage de teste."""
    return RecruiterMessage(
        message_id="test-message-id",
        thread_id="test-thread-id",
        sender_name=sender_name,
        sender_headline=sender_headline,
        subject=subject,
        body_text=body,
        body_html=None,
        received_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
        permalink="https://www.linkedin.com/messaging/thread/123/",
    )
