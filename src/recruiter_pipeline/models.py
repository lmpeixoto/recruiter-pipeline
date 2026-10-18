"""Modelos Pydantic usados no pipeline."""

from datetime import datetime

from pydantic import BaseModel, Field


class RecruiterMessage(BaseModel):
    """Uma mensagem de recruiter normalizada, seja qual for a origem.

    Hoje só o GmailClient (notificações do LinkedIn) produz isto -- os
    campos LinkedIn-específicos (sender_headline, company_linkedin_guess)
    ficam None para qualquer fonte futura que não os tenha. O resto do
    pipeline (Classifier/Extractor/Assessor/NotionBoard) já é agnóstico
    de fonte; só esta classe e o GmailClient ainda não são.
    """

    message_id: str
    thread_id: str | None = None
    sender_name: str
    sender_headline: str | None = None
    subject: str
    body_text: str
    body_html: str | None = None
    received_at: datetime
    permalink: str | None = None
    email_type: str = "dm"  # dm, job_alert, notification
    company_linkedin_guess: str | None = None  # link linkedin.com/company/* detetado no HTML
    job_id: str | None = None  # id da vaga no LinkedIn (/jobs/view/<id>), chave de dedupe

    @property
    def source(self) -> str:
        """Origem da mensagem (proveniência de primeira classe)."""
        return {
            "dm": "LinkedIn DM",
            "job_alert": "LinkedIn Job Alert",
            "notification": "LinkedIn Email",
        }.get(self.email_type, "LinkedIn Email")


class Classification(BaseModel):
    """Resultado da classificação de uma mensagem."""

    is_opportunity: bool = Field(
        description="True se a mensagem for uma proposta de emprego/recrutamento"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confiança da classificação"
    )
    reason: str = Field(description="Breve justificação da classificação")


class ProposalExtraction(BaseModel):
    """Dados estruturados extraídos de uma proposta."""

    recruiter_name: str | None = None
    recruiter_headline: str | None = None
    agency: str | None = Field(
        default=None,
        description="Agência de recrutamento do remetente, se aplicável",
    )
    company_client: str | None = Field(
        default=None,
        description="Empresa cliente da vaga (pode ser diferente da agência)",
    )
    company_confidential: bool = Field(
        default=False,
        description="True se a empresa cliente não for mencionada na mensagem",
    )
    company_website: str | None = Field(
        default=None,
        description="Website da empresa, se mencionado na mensagem",
    )
    company_linkedin: str | None = Field(
        default=None,
        description="URL da página LinkedIn da empresa, se mencionada",
    )
    role_title: str | None = None
    seniority: str | None = Field(
        default=None, description="junior, mid, senior, staff, principal ou null"
    )
    stack: list[str] = Field(default_factory=list)
    location: str | None = None
    remote_policy: str | None = Field(
        default=None, description="remote, hybrid, onsite ou null"
    )
    salary: str | None = None
    contract_type: str | None = Field(
        default=None, description="permanent, contract, freelance ou null"
    )
    language: str | None = Field(default=None, description="pt ou en")
    summary: str = Field(description="Resumo de 2-3 frases em português")
    confidence: float = Field(ge=0.0, le=1.0)


class CompanyEnrichment(BaseModel):
    """Informação enriquecida sobre uma empresa."""

    name: str
    website: str | None = None
    linkedin_url: str | None = None
    teamlyzer_url: str | None = None
    industry: str | None = None
    size: str | None = None
    headquarters: str | None = None
    description: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    notes: str | None = None

    def to_markdown(self) -> str:
        """Converte o enriquecimento para markdown legível."""
        lines = [f"# {self.name}"]
        if self.website:
            lines.append(f"- **Website:** {self.website}")
        if self.linkedin_url:
            lines.append(f"- **LinkedIn:** {self.linkedin_url}")
        if self.teamlyzer_url:
            lines.append(f"- **Teamlyzer:** {self.teamlyzer_url}")
        if self.industry:
            lines.append(f"- **Setor:** {self.industry}")
        if self.size:
            lines.append(f"- **Tamanho:** {self.size}")
        if self.headquarters:
            lines.append(f"- **Sede:** {self.headquarters}")
        if self.description:
            lines.append(f"\n## Descrição\n{self.description}")
        if self.tech_stack:
            lines.append(f"\n## Stack pública\n{', '.join(self.tech_stack)}")
        if self.recent_news:
            lines.append("\n## Notícias recentes")
            for news in self.recent_news:
                lines.append(f"- {news}")
        if self.notes:
            lines.append(f"\n## Notas\n{self.notes}")
        return "\n".join(lines)


class OpportunityAssessment(BaseModel):
    """Avaliação da oportunidade face ao perfil do candidato."""

    opportunity_type: str = Field(
        default="unknown",
        description="job_opportunity, recruiter_outreach, connection, referral, generic, unknown",
    )
    fit_score: int | None = Field(
        default=None, ge=0, le=100, description="Fit com o perfil; null se info insuficiente"
    )
    fit_confidence: int = Field(
        default=0, ge=0, le=100, description="Confiança na avaliação"
    )
    role_fit: int | None = Field(default=None, ge=0, le=100)
    seniority_fit: int | None = Field(default=None, ge=0, le=100)
    stack_fit: int | None = Field(default=None, ge=0, le=100)
    remote_fit: int | None = Field(default=None, ge=0, le=100)
    location_fit: int | None = Field(default=None, ge=0, le=100)
    company_fit: int | None = Field(default=None, ge=0, le=100)
    recommended_action: str = Field(
        default="monitor",
        description="reply, ask_for_more_information, apply, ignore, monitor, connect",
    )
    action_reason: str = ""
    questions_to_ask: list[str] = Field(default_factory=list)
    reply_draft: str = Field(
        default="", description="Rascunho curto de resposta ao recruiter, em português"
    )
    # Metadados internos (não vêm do LLM; não aparecem no card, ficam na DB)
    action_source: str = "llm"  # llm | jev | guardrail | rule
    decision_trace: str = ""  # histórico de mudanças de ação (Jev, guardrails, regras)
    jev_confidence: float | None = None

    def dimensions_markdown(self) -> list[str]:
        """Linhas legíveis com os scores por dimensão (ignora as desconhecidas)."""
        labels = {
            "role_fit": "Role",
            "seniority_fit": "Senioridade",
            "stack_fit": "Stack",
            "remote_fit": "Remote",
            "location_fit": "Localização",
            "company_fit": "Empresa",
        }
        lines = []
        for field, label in labels.items():
            value = getattr(self, field)
            if value is not None:
                lines.append(f"  - {label}: {value}/100")
        return lines


def missing_information(e: ProposalExtraction) -> list[str]:
    """Lista determinística do que falta para decidir (sem LLM)."""
    missing = []
    if not e.company_client:
        missing.append("empresa cliente")
    if not e.role_title:
        missing.append("título da vaga")
    if not e.seniority:
        missing.append("senioridade")
    if not e.stack:
        missing.append("tech stack")
    if not e.salary:
        missing.append("intervalo salarial")
    if not e.remote_policy:
        missing.append("política remote")
    if not e.location:
        missing.append("localização")
    if not e.contract_type:
        missing.append("modelo de contrato")
    return missing


def _normalize(text: str) -> str:
    """Normaliza texto para dedupe: minúsculas, sem acentos nem pontuação."""
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", text.lower().strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


_LOCATION_NOISE = {"portugal", "pt", "area", "metropolitan", "metropolitana", "grande", "de", "do", "da"}


def _location_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in _LOCATION_NOISE}


# Países/cidades estrangeiras comuns: "Portugal" genérico não é compatível com estas.
_FOREIGN_TOKENS = {
    "kingdom", "uk", "england", "london", "spain", "espana", "madrid", "barcelona",
    "germany", "berlin", "munich", "france", "paris", "ireland", "dublin",
    "netherlands", "amsterdam", "usa", "states", "canada", "brazil", "brasil",
    "poland", "romania", "india", "switzerland", "italy", "belgium", "sweden",
}


def _similar_token(a: str, b: str) -> bool:
    """Tokens iguais ou quase (typos como balio/bailio)."""
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio() >= 0.8
    return False


def locations_compatible(a: str | None, b: str | None) -> bool:
    """True se duas localizações não se contradizem.

    Desconhecida ou genérica ("Portugal") é compatível com tudo; "Porto" e
    "Porto, Portugal" são a mesma; "Leça do Balio" e "Leça do Bailio" também.
    "remote" é compatível com tudo (a vaga pode listar sede + remote).
    """
    if not a or not b:
        return True
    ta, tb = _location_tokens(a), _location_tokens(b)
    if bool(ta & _FOREIGN_TOKENS) != bool(tb & _FOREIGN_TOKENS):
        return False  # um estrangeiro, o outro não (nem que seja só "Portugal")
    if not ta or not tb:
        return True  # só "Portugal"/"PT" -> genérico
    if "remote" in ta or "remote" in tb or "remoto" in ta or "remoto" in tb:
        return True
    return any(_similar_token(x, y) for x in ta for y in tb)


def roles_similar(role: str, other_role: str, threshold: float = 0.5) -> bool:
    """Jaccard de tokens entre dois títulos de vaga."""
    a, b = _tokens(role), _tokens(other_role)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


def opportunities_match(
    company: str,
    role: str,
    location: str | None,
    contract: str | None,
    other_company: str,
    other_role: str,
    other_location: str | None,
    other_contract: str | None,
) -> bool:
    """Segunda camada de dedupe (fuzzy), para quando o fingerprint exato falha.

    Mesma empresa + role semanticamente semelhante, sem contradição de
    localização ou tipo de contrato quando ambos são conhecidos.
    """
    if _normalize(company) != _normalize(other_company):
        return False
    if not roles_similar(role, other_role):
        return False
    if not locations_compatible(location, other_location):
        return False
    if contract and other_contract and _normalize(contract) != _normalize(other_contract):
        return False
    return True


class Proposal(BaseModel):
    """Proposta processada, pronta a enviar para o Notion."""

    message: RecruiterMessage
    classification: Classification
    extraction: ProposalExtraction
    enrichment: CompanyEnrichment | None = None
    assessment: OpportunityAssessment | None = None

    def fingerprint(self) -> str:
        """Dedupe primário: empresa + role + localização + contrato (normalizados).

        Localização/contrato ajudam a distinguir vagas diferentes com o mesmo
        título na mesma empresa/agência; ficam vazios quando desconhecidos.
        """
        company = self.extraction.company_client or self.extraction.agency or "unknown"
        return "::".join(
            [
                _normalize(company),
                _normalize(self.extraction.role_title or "unknown"),
                _normalize(self.extraction.location or ""),
                _normalize(self.extraction.contract_type or ""),
            ]
        )
