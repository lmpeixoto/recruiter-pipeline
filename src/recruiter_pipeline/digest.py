"""Resumo curto do run (consola + notificação macOS) e análise de feedback."""

import subprocess
from collections import Counter

from recruiter_pipeline.decision import triage
from recruiter_pipeline.models import Proposal

TOP_N = 5


def build_digest(proposals: list[Proposal]) -> tuple[str, str]:
    """Devolve (título, corpo) com o que merece atenção, não o que foi processado."""
    if not proposals:
        return "Recruiter pipeline", "Sem propostas novas."
    counts = Counter(
        triage(p.assessment.recommended_action) if p.assessment else "⚪ Monitorizar"
        for p in proposals
    )
    title = f"{len(proposals)} propostas · " + " ".join(
        f"{label.split()[0]} {n}" for label, n in sorted(counts.items())
    )

    def sort_key(p: Proposal) -> int:
        return -(p.assessment.fit_score or 0) if p.assessment else 0

    top = [
        p
        for p in sorted(proposals, key=sort_key)
        if p.assessment and p.assessment.recommended_action in ("reply", "apply")
    ][:TOP_N]
    lines = [
        f"{p.extraction.company_client or p.extraction.agency or 'Confidencial'} — "
        f"{p.extraction.role_title or 'role desconhecido'} "
        f"(fit {p.assessment.fit_score if p.assessment.fit_score is not None else '—'})"
        for p in top
    ]
    asks = counts.get("🟡 Pedir info", 0)
    body = "\n".join(lines) if lines else "Nada para responder já."
    if asks:
        body += f"\n{asks} a precisar de mais informação."
    return title, body


def notify(title: str, body: str) -> bool:
    """Notificação do macOS via osascript (sem shell: argumentos como lista)."""
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, title, body],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def analyze_feedback(cards: list[dict]) -> dict:
    """Cruza decisões do utilizador (estado + motivo) com o fit previsto."""
    advanced_states = {"Recruiter screen", "Entrevista", "Proposta", "Aceite"}
    rejected_states = {"Rejeitada", "Arquivada"}
    advanced = [c for c in cards if c.get("estado") in advanced_states]
    rejected = [c for c in cards if c.get("estado") in rejected_states]

    def avg_fit(items: list[dict]) -> float | None:
        values = [c["fit"] for c in items if c.get("fit") is not None]
        return round(sum(values) / len(values), 1) if values else None

    reasons = Counter(c["motivo"] for c in rejected if c.get("motivo"))
    # Descartes que o pipeline recomendou responder = o perfil está demasiado otimista
    optimistic = [c for c in rejected if c.get("triagem", "") and c["triagem"].startswith("🟢")]
    # Cards que o pipeline mandou descartar mas o utilizador fez avançar
    pessimistic = [c for c in advanced if c.get("triagem", "") and c["triagem"].startswith("🔴")]
    return {
        "advanced": len(advanced),
        "rejected": len(rejected),
        "avg_fit_advanced": avg_fit(advanced),
        "avg_fit_rejected": avg_fit(rejected),
        "reasons": reasons,
        "rejected_without_reason": sum(1 for c in rejected if not c.get("motivo")),
        "optimistic": optimistic,
        "pessimistic": pessimistic,
    }
