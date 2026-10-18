"""Cliente Gmail: OAuth, fetch e parse de notificações do LinkedIn."""

import base64
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from recruiter_pipeline.config import Config
from recruiter_pipeline.models import RecruiterMessage

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LINKEDIN_SENDERS = [
    "messages-noreply@linkedin.com",
    "notifications-noreply@linkedin.com",
    "linkedin.com",
]

# Assuntos que indicam mensagem direta (DM) ou InMail no LinkedIn
DM_SUBJECT_PATTERNS = [
    "sent you a message",
    "just messaged you",
    "new message",  # "You have 4 new messages", "You have 1 new message"
    "your inmail",
    "message replied:",
]

# Classificação pelo endereço de envio do LinkedIn (mais fiável que o assunto)
SENDER_TYPE_MAP = [
    ("messages-noreply@linkedin.com", "dm"),
    ("inmail-hit-reply@linkedin.com", "dm"),
    ("jobalerts-noreply@linkedin.com", "job_alert"),
    ("jobs-listings@linkedin.com", "job_alert"),
    ("invitations@linkedin.com", "notification"),
    ("notifications-noreply@linkedin.com", "notification"),
    ("newsletters-noreply@linkedin.com", "notification"),
    ("groups-noreply@linkedin.com", "notification"),
    ("salesnav@", "notification"),
    ("learning-", "notification"),
]

# Assuntos que são claramente notificações do LinkedIn e não mensagens de recruiters
NOTIFICATION_SUBJECT_PATTERNS = [
    "job alert",
    "accepted your invitation",
    "lead suggestions",
    "sales navigator",
    "learning spotlight",
    "conversations in",
    "impressions last week",
    "posts got",
    "just dropped",
    "new ai courses",
    "premium",
    "event",
    "invitation",
    "your job alert",
    "job alert has been created",
    "expand your search",
    "don't miss conversations",
    "shared a post",
]


def is_dm_email(subject: str) -> bool:
    """Retorna True se o assunto indicar uma DM/InMail do LinkedIn."""
    subject_lower = subject.lower()
    return any(pattern in subject_lower for pattern in DM_SUBJECT_PATTERNS)


def is_notification_email(subject: str) -> bool:
    """Retorna True se o assunto indicar uma notificação genérica do LinkedIn."""
    subject_lower = subject.lower()
    return any(pattern in subject_lower for pattern in NOTIFICATION_SUBJECT_PATTERNS)


def _extract_from_email(from_header: str) -> str:
    """Extrai o endereço de email do header From (ex.: 'LinkedIn <x@linkedin.com>')."""
    match = re.search(r"[\w.\-+]+@[\w.\-]+", from_header)
    return match.group(0).lower() if match else ""


def classify_email_type(subject: str, from_email: str = "") -> str:
    """Classifica o email como 'dm', 'job_alert' ou 'notification'.

    O endereço de envio do LinkedIn é o sinal mais fiável; o assunto é o fallback.
    """
    subject_lower = subject.lower()

    # 1) Endereço de envio
    if from_email:
        for sender_key, email_type in SENDER_TYPE_MAP:
            if sender_key in from_email:
                # Exceção: o assunto "job alert" tem prioridade sobre o remetente
                if email_type != "job_alert" and "job alert" in subject_lower:
                    return "job_alert"
                return email_type
        # Remetente LinkedIn desconhecido: cai para o assunto

    # 2) Assunto
    if is_dm_email(subject):
        return "dm"
    if "job alert" in subject_lower:
        return "job_alert"
    if is_notification_email(subject):
        return "notification"

    # 3) Fallback conservador: sem remetente de mensagens e sem assunto de DM,
    # não vale a pena gastar LLM — trata como notificação
    return "notification"


def _load_credentials(
    credentials_path: Path, token_path: Path
) -> Credentials:
    """Carrega ou renova credenciais OAuth do Gmail."""
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Ficheiro de credenciais não encontrado: {credentials_path}. "
                    "Descarrega-o da Google Cloud Console (OAuth desktop client)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def _decode_body(part: dict) -> str:
    """Decodifica o corpo de uma parte MIME."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_text_from_html(html: str) -> str:
    """Converte HTML para texto limpo."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts e styles
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _extract_permalink(body_html: str) -> str | None:
    """Tenta extrair o link direto para a mensagem no LinkedIn."""
    if not body_html:
        return None
    soup = BeautifulSoup(body_html, "html.parser")
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True).lower()
        if "linkedin.com" in href and (
            "view" in text or "message" in text or "msg" in href
        ):
            return href
    return None


def _extract_company_linkedin(body_html: str) -> str | None:
    """Procura links linkedin.com/company/* no HTML do email."""
    if not body_html:
        return None
    soup = BeautifulSoup(body_html, "html.parser")
    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.search(r"(https?://[a-z]{2,3}\.linkedin\.com/company/[^/?\"']+)", href)
        if match:
            return match.group(1)
    return None


def _extract_sender_info(body_text: str, subject: str) -> tuple[str, str | None]:
    """Extrai nome e headline do remetente a partir do texto."""
    # O email do LinkedIn costuma ter algo como:
    # "João Silva
    # Talent Acquisition Specialist at Michael Page"
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]

    # Tenta obter o nome do subject: "João Silva sent you a message"
    name_from_subject = None
    match = re.search(r"^(.*?)\s+sent you a message", subject, re.IGNORECASE)
    if match:
        name_from_subject = match.group(1).strip()

    if name_from_subject:
        # A headline costuma ser a linha seguinte ao nome no corpo
        for i, line in enumerate(lines):
            if name_from_subject in line or line in name_from_subject:
                if i + 1 < len(lines):
                    return name_from_subject, lines[i + 1]
        return name_from_subject, None

    # Fallback: primeira linha como nome, segunda como headline
    if lines:
        return lines[0], lines[1] if len(lines) > 1 else None
    return "Unknown", None


class GmailClient:
    """Cliente para ler emails de notificação do LinkedIn."""

    def __init__(
        self,
        credentials_path: Path | None = None,
        token_path: Path | None = None,
    ) -> None:
        self.credentials_path = credentials_path or Config.gmail_credentials_path
        self.token_path = token_path or Config.gmail_token_path
        self.creds = _load_credentials(self.credentials_path, self.token_path)
        self.service = build("gmail", "v1", credentials=self.creds)

    def build_query(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        label: str | None = None,
    ) -> str:
        """Constrói a query de pesquisa do Gmail."""
        sender_query = " OR ".join(
            f"from:{sender}" for sender in LINKEDIN_SENDERS
        )
        parts = [f"({sender_query})"]
        if since:
            parts.append(f"after:{since.strftime('%Y/%m/%d')}")
        if until:
            parts.append(f"before:{until.strftime('%Y/%m/%d')}")
        if label:
            parts.append(f"label:{label}")
        return " ".join(parts)

    def fetch_messages(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        label: str | None = None,
        max_results: int | None = None,
        unread_only: bool = False,
    ) -> list[RecruiterMessage]:
        """Busca e parseia emails de notificação do LinkedIn."""
        query = self.build_query(since, until, label)
        if unread_only:
            query += " is:unread"
        messages: list[dict] = []
        page_token: str | None = None

        while True:
            params: dict = {"userId": "me", "q": query}
            if page_token:
                params["pageToken"] = page_token
            result = self.service.users().messages().list(**params).execute()
            messages.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
            if max_results and len(messages) >= max_results:
                messages = messages[:max_results]
                break

        parsed: list[RecruiterMessage] = []
        for msg_meta in messages:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=msg_meta["id"], format="full")
                .execute()
            )
            parsed_msg = self._parse_message(msg)
            if parsed_msg:
                parsed.append(parsed_msg)

        return parsed

    def mark_as_read(self, message_id: str) -> None:
        """Marca um email como lido no Gmail."""
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()

    def _parse_message(self, msg: dict) -> RecruiterMessage | None:
        """Parseia um email da Gmail API para RecruiterMessage."""
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        subject = headers.get("subject", "")
        date_header = headers.get("date", "")
        message_id = headers.get("message-id", msg["id"])
        from_email = _extract_from_email(headers.get("from", ""))

        try:
            received_at = parsedate_to_datetime(date_header)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
        except Exception:
            received_at = datetime.now(UTC)

        # Extrai partes HTML e plain text
        html_body = ""
        text_body = ""

        def walk_parts(parts: list[dict]) -> None:
            nonlocal html_body, text_body
            for part in parts:
                mime_type = part.get("mimeType", "")
                if mime_type == "text/html" and not html_body:
                    html_body = _decode_body(part)
                elif mime_type == "text/plain" and not text_body:
                    text_body = _decode_body(part)
                if "parts" in part:
                    walk_parts(part["parts"])

        if "parts" in payload:
            walk_parts(payload["parts"])
        elif payload.get("mimeType") == "text/html":
            html_body = _decode_body(payload)
        elif payload.get("mimeType") == "text/plain":
            text_body = _decode_body(payload)

        body_text = text_body or _extract_text_from_html(html_body)
        if not body_text.strip():
            return None

        sender_name, sender_headline = _extract_sender_info(body_text, subject)
        permalink = _extract_permalink(html_body)
        company_linkedin_guess = _extract_company_linkedin(html_body)

        return RecruiterMessage(
            message_id=message_id,
            thread_id=msg.get("threadId"),
            sender_name=sender_name,
            sender_headline=sender_headline,
            subject=subject,
            body_text=body_text,
            body_html=html_body or None,
            received_at=received_at,
            permalink=permalink,
            email_type=classify_email_type(subject, from_email),
            company_linkedin_guess=company_linkedin_guess,
        )
