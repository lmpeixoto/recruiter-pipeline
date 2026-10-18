"""Regras determinísticas aplicadas à avaliação, antes de chegar ao card.

O LLM/Jev decidem com a informação que têm; quando a mensagem traz pouco
(job alerts, DMs vagas) a opção "ignore" é a mais fácil e acaba a esconder
oportunidades que só estavam mal documentadas. Aqui:

- `ignore` só sobrevive com evidência (dealbreaker do perfil, ou fit baixo
  com confiança alta e identidade da vaga conhecida);
- falta de informação nunca é motivo para descartar -- vira "pedir mais info"
  ou "monitorizar";
- `reply`/`apply` sem empresa nem role não fazem sentido -- vira "pedir info".
"""

import re
from dataclasses import dataclass, field

from recruiter_pipeline.models import (
    OpportunityAssessment,
    ProposalExtraction,
    _normalize,
    missing_information,
)

IGNORE_MAX_FIT = 40
IGNORE_MIN_CONFIDENCE = 60

TRIAGE_BY_ACTION = {
    "reply": "🟢 Responder",
    "apply": "🟢 Responder",
    "ask_for_more_information": "🟡 Pedir info",
    "monitor": "⚪ Monitorizar",
    "connect": "⚪ Monitorizar",
    "ignore": "🔴 Descartar",
}


def triage(action: str) -> str:
    """Veredicto curto e ordenável para o board."""
    return TRIAGE_BY_ACTION.get(action, "⚪ Monitorizar")


@dataclass
class Rules:
    """Dealbreakers explícitos do perfil (secção opcional '## Regras')."""

    avoid_stack: list[str] = field(default_factory=list)
    avoid_companies: list[str] = field(default_factory=list)
    avoid_agencies: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.avoid_stack or self.avoid_companies or self.avoid_agencies)


def parse_rules(profile: str | None) -> Rules:
    """Lê linhas `- avoid_stack: a, b` da secção '## Regras' do profile.md."""
    rules = Rules()
    if not profile:
        return rules
    in_section = False
    for line in profile.splitlines():
        if line.startswith("#"):
            in_section = line.lstrip("#").strip().lower().startswith("regras")
            continue
        if not in_section:
            continue
        match = re.match(r"\s*[-*]?\s*(avoid_stack|avoid_companies|avoid_agencies)\s*:\s*(.+)", line)
        if not match:
            continue
        values = [v.strip() for v in match.group(2).split(",") if v.strip()]
        getattr(rules, match.group(1)).extend(values)
    return rules


def _dealbreaker(extraction: ProposalExtraction, rules: Rules) -> str | None:
    stack = {_normalize(s) for s in extraction.stack}
    for tech in rules.avoid_stack:
        if _normalize(tech) in stack:
            return f"stack a evitar: {tech}"
    companies = {_normalize(c) for c in (extraction.company_client, extraction.agency) if c}
    for name in rules.avoid_companies:
        if _normalize(name) in companies:
            return f"empresa a evitar: {name}"
    agency = _normalize(extraction.agency or "")
    for name in rules.avoid_agencies:
        if agency and _normalize(name) == agency:
            return f"agência a evitar: {name}"
    return None


def _set_action(
    assessment: OpportunityAssessment, action: str, reason: str, source: str
) -> None:
    previous = assessment.recommended_action
    note = f"{source}: {previous} → {action} ({reason})"
    assessment.decision_trace = "; ".join(filter(None, [assessment.decision_trace, note]))
    assessment.recommended_action = action
    assessment.action_source = source


def apply_guardrails(
    assessment: OpportunityAssessment,
    extraction: ProposalExtraction,
    rules: Rules | None = None,
) -> OpportunityAssessment:
    """Corrige a ação recomendada in-place e devolve a avaliação."""
    rules = rules or Rules()
    missing = missing_information(extraction)
    identity_known = bool(
        (extraction.company_client or extraction.agency) and extraction.role_title
    )

    reason = _dealbreaker(extraction, rules)
    if reason:
        if assessment.recommended_action != "ignore":
            _set_action(assessment, "ignore", reason, "rule")
        assessment.action_reason = f"Dealbreaker do perfil — {reason}."
        return assessment

    action = assessment.recommended_action
    if action == "ignore":
        evidence = (
            assessment.fit_score is not None
            and assessment.fit_score < IGNORE_MAX_FIT
            and assessment.fit_confidence >= IGNORE_MIN_CONFIDENCE
            and identity_known
        )
        if not evidence:
            target = (
                "ask_for_more_information"
                if (not extraction.company_client or len(missing) >= 3)
                else "monitor"
            )
            detail = (
                f"fit={assessment.fit_score}, confiança={assessment.fit_confidence}, "
                f"em falta: {', '.join(missing) or 'nada'}"
            )
            _set_action(assessment, target, f"sem evidência para descartar; {detail}", "guardrail")
            assessment.action_reason = (
                "Informação insuficiente para descartar"
                + (f" — falta: {', '.join(missing[:4])}." if missing else ".")
            )
    elif action in ("reply", "apply") and not identity_known:
        _set_action(
            assessment,
            "ask_for_more_information",
            "sem empresa ou role identificáveis",
            "guardrail",
        )
        assessment.action_reason = (
            "Ainda não dá para responder com critério — falta identificar "
            + ("a empresa" if not (extraction.company_client or extraction.agency) else "o role")
            + "."
        )
    return assessment
