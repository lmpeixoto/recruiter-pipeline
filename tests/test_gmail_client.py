"""Testes para o parser de emails do LinkedIn."""

from recruiter_pipeline.gmail_client import (
    _extract_permalink,
    _extract_sender_info,
    classify_email_type,
)


def test_extract_sender_info_from_subject():
    body = """João Silva
Talent Acquisition Specialist at Michael Page

Olá Luís,
Temos uma oportunidade interessante para ti.
"""
    name, headline = _extract_sender_info(body, "João Silva sent you a message")
    assert name == "João Silva"
    assert "Michael Page" in (headline or "")


def test_extract_permalink_finds_linkedin_message():
    html = """
    <html>
      <body>
        <a href="https://www.linkedin.com/messaging/thread/123/">View message</a>
      </body>
    </html>
    """
    link = _extract_permalink(html)
    assert link == "https://www.linkedin.com/messaging/thread/123/"


def test_extract_permalink_returns_none_when_missing():
    assert _extract_permalink("<html><body>no links</body></html>") is None


class TestClassifyEmailType:
    def test_dm_by_sender(self):
        assert (
            classify_email_type(
                "Oportunidade – Banco de Portugal",
                "messages-noreply@linkedin.com",
            )
            == "dm"
        )

    def test_inmail_by_sender(self):
        assert (
            classify_email_type(
                "Your InMail from João is awaiting response",
                "inmail-hit-reply@linkedin.com",
            )
            == "dm"
        )

    def test_job_alert_by_sender_even_with_job_title_subject(self):
        assert (
            classify_email_type(
                "Platform Engineer (Analytics, Data & AI) at ISS A/S",
                "jobalerts-noreply@linkedin.com",
            )
            == "job_alert"
        )

    def test_job_alert_by_subject(self):
        assert (
            classify_email_type(
                "Your job alert for Engenheiro de plataforma in Porto",
                "notifications-noreply@linkedin.com",
            )
            == "job_alert"
        )

    def test_invitation_is_notification(self):
        assert (
            classify_email_type(
                "You have an invitation",
                "invitations@linkedin.com",
            )
            == "notification"
        )

    def test_group_digest_is_notification(self):
        assert (
            classify_email_type(
                "Don't miss conversations in Python Developers Community",
                "groups-noreply@linkedin.com",
            )
            == "notification"
        )

    def test_unknown_sender_with_dm_subject(self):
        assert (
            classify_email_type("Maria sent you a message", "x@linkedin.com") == "dm"
        )

    def test_unknown_sender_without_dm_subject_defaults_to_notification(self):
        assert (
            classify_email_type(
                "New remote job opportunity - Fullstack Developer",
                "x@linkedin.com",
            )
            == "notification"
        )

    def test_no_sender_defaults_to_notification_unless_dm_subject(self):
        assert classify_email_type("You have 2 new messages") == "dm"
        assert classify_email_type("Exciting opportunity at Acme") == "notification"
