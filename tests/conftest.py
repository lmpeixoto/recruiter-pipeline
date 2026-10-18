"""Fixtures e helpers para testes."""

from datetime import UTC, datetime

from recruiter_pipeline.models import RecruiterMessage


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
