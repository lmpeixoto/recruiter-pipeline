"""Fixtures e helpers para testes."""

from datetime import datetime, timezone

import pytest

from recruiter_pipeline.models import LinkedInMessage


def make_message(
    body: str,
    sender_name: str = "João Silva",
    sender_headline: str = "Recruiter at Michael Page",
    subject: str = "João Silva sent you a message",
) -> LinkedInMessage:
    """Cria uma LinkedInMessage de teste."""
    return LinkedInMessage(
        message_id="test-message-id",
        thread_id="test-thread-id",
        sender_name=sender_name,
        sender_headline=sender_headline,
        subject=subject,
        body_text=body,
        body_html=None,
        received_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        permalink="https://www.linkedin.com/messaging/thread/123/",
    )
