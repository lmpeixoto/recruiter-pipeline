"""Divisão de job alerts em vagas individuais."""

from datetime import UTC, datetime

from recruiter_pipeline.job_alerts import (
    extract_job_id,
    parse_job_description,
    split_job_alert,
)
from recruiter_pipeline.models import RecruiterMessage

ALERT_HTML = """
<html><body>
<table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4012345678?trk=x">Site Reliability Engineer</a>
  <div>Acme · Porto, Portugal</div>
  <a href="https://www.linkedin.com/comm/jobs/view/4012345678?trk=y">View job</a>
</td></tr>
<tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/site-reliability-4099999999/?trk=x">Platform Engineer</a>
  <div>Globex · Remote</div>
</td></tr></table>
<a href="https://www.linkedin.com/jobs/search/">See all jobs</a>
</body></html>
"""


def _alert(html: str | None = ALERT_HTML, email_type: str = "job_alert") -> RecruiterMessage:
    return RecruiterMessage(
        message_id="<alert@x>",
        sender_name="Your job alert for SRE",
        subject="Your job alert for SRE in Porto",
        body_text="texto",
        body_html=html,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        email_type=email_type,
    )


def test_splits_one_message_per_job():
    jobs = split_job_alert(_alert())
    assert [j.job_id for j in jobs] == ["4012345678", "4099999999"]
    first = jobs[0]
    assert first.message_id == "<alert@x>#job4012345678"
    assert first.subject == "Site Reliability Engineer"
    assert "Acme" in first.body_text and "Porto" in first.body_text
    assert "Globex" not in first.body_text  # não engole a vaga seguinte
    assert first.permalink == "https://www.linkedin.com/jobs/view/4012345678"
    assert first.sender_name == "LinkedIn Job Alert"


def test_non_alert_or_no_links_is_untouched():
    dm = _alert(email_type="dm")
    assert split_job_alert(dm) == [dm]
    empty = _alert(html="<p>nada</p>")
    assert split_job_alert(empty) == [empty]
    no_html = _alert(html=None)
    assert split_job_alert(no_html) == [no_html]


def test_extract_job_id():
    assert extract_job_id("https://www.linkedin.com/jobs/view/4012345678/") == "4012345678"
    assert extract_job_id("https://www.linkedin.com/jobs/view/sre-at-acme-4012345678?x=1") == "4012345678"
    assert extract_job_id("https://www.linkedin.com/messaging/thread/1/") is None
    assert extract_job_id(None) is None


def test_parse_job_description():
    html = (
        '<ul class="description__job-criteria-list"><li>Seniority level Senior</li></ul>'
        '<div class="show-more-less-html__markup"><p>Terraform</p><p>AWS</p></div>'
    )
    text = parse_job_description(html)
    assert "Seniority level Senior" in text and "Terraform" in text
    assert parse_job_description("<html></html>") is None
