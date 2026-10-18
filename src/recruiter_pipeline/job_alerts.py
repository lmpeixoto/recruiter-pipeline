"""Job alerts do LinkedIn: um email lista várias vagas -> uma mensagem por vaga.

Tratar o email inteiro como uma única proposta (o comportamento anterior)
perdia todas as vagas menos uma e sujava a tabela de recruiters com o assunto
do alerta. O id da vaga (/jobs/view/<id>) também é a melhor chave de dedupe:
a mesma vaga chega por vários alertas e por DMs.
"""

import re

import httpx
from bs4 import BeautifulSoup

from recruiter_pipeline.models import RecruiterMessage

JOB_LINK_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\"']*?-)?(\d{6,})")
GENERIC_LINK_TEXT = {
    "view job", "apply", "easy apply", "see all jobs", "view jobs", "see job",
    "view all jobs", "unsubscribe", "",
}
GUEST_JOB_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}"


def extract_job_id(url: str | None) -> str | None:
    """Id da vaga a partir de um link do LinkedIn, se for um link de vaga."""
    if not url:
        return None
    match = JOB_LINK_RE.search(url)
    return match.group(1) if match else None


def _container_for(anchor, other_ids: set[str], own_id: str):
    """Sobe na árvore até apanhar o 'cartão' da vaga sem engolir outras vagas."""
    best = anchor
    node = anchor
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        ids = {
            m.group(1)
            for a in parent.find_all("a", href=True)
            if (m := JOB_LINK_RE.search(a["href"]))
        }
        if ids - {own_id}:
            break
        best = parent
        node = parent
    return best


def split_job_alert(message: RecruiterMessage) -> list[RecruiterMessage]:
    """Divide um email de job alert em mensagens por vaga.

    Devolve [message] inalterada se não encontrar links de vagas.
    """
    if message.email_type != "job_alert" or not message.body_html:
        return [message]
    soup = BeautifulSoup(message.body_html, "html.parser")

    anchors_by_id: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        match = JOB_LINK_RE.search(a["href"])
        if match:
            anchors_by_id.setdefault(match.group(1), []).append(a)
    if not anchors_by_id:
        return [message]

    all_ids = set(anchors_by_id)
    jobs: list[RecruiterMessage] = []
    for job_id, anchors in anchors_by_id.items():
        titles = [
            a.get_text(" ", strip=True)
            for a in anchors
            if a.get_text(" ", strip=True).lower() not in GENERIC_LINK_TEXT
        ]
        if not titles:
            continue
        title = max(titles, key=len)
        container = _container_for(anchors[0], all_ids, job_id)
        lines = [
            line.strip()
            for line in container.get_text("\n", strip=True).splitlines()
            if line.strip() and line.strip().lower() not in GENERIC_LINK_TEXT
        ]
        body_lines = [title] + [line for line in lines if line != title]
        body_lines.append(f"Link da vaga: {job_url(job_id)}")
        jobs.append(
            message.model_copy(
                update={
                    "message_id": f"{message.message_id}#job{job_id}",
                    "sender_name": "LinkedIn Job Alert",
                    "sender_headline": None,
                    "subject": title,
                    "body_text": "\n".join(body_lines),
                    "permalink": job_url(job_id),
                    "job_id": job_id,
                }
            )
        )
    return jobs or [message]


def fetch_job_description(job_id: str, timeout: float = 10.0) -> str | None:
    """Descrição pública completa da vaga (endpoint 'guest' do LinkedIn).

    Falha em silêncio (devolve None): é um enriquecimento oportunista, e o
    LinkedIn pode limitar/bloquear pedidos sem sessão.
    """
    try:
        response = httpx.get(
            GUEST_JOB_URL.format(job_id=job_id),
            headers={"User-Agent": "Mozilla/5.0 (recruiter-pipeline personal use)"},
            timeout=timeout,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    return parse_job_description(response.text)


def parse_job_description(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    criteria = soup.select(".description__job-criteria-list li")
    for item in criteria:
        text = " ".join(item.get_text(" ", strip=True).split())
        if text:
            parts.append(text)
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(
        ".description__text"
    )
    if desc:
        parts.append(desc.get_text("\n", strip=True))
    text = "\n".join(parts).strip()
    return text[:6000] or None
