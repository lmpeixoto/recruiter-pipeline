"""CLI do pipeline de propostas de recruiters."""

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from recruiter_pipeline.assessor import Assessor
from recruiter_pipeline.classifier import Classifier
from recruiter_pipeline.config import Config
from recruiter_pipeline.db import StateDB
from recruiter_pipeline.decision import triage
from recruiter_pipeline.digest import analyze_feedback, build_digest, notify
from recruiter_pipeline.enricher import Enricher
from recruiter_pipeline.extractor import Extractor
from recruiter_pipeline.gmail_client import GmailClient, is_notification_email
from recruiter_pipeline.job_alerts import fetch_job_description, split_job_alert
from recruiter_pipeline.jev_client import JevClient
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import Classification, Proposal, RecruiterMessage
from recruiter_pipeline.notion_board import NotionBoard

app = typer.Typer(help="Pipeline de propostas de recruiters do LinkedIn")
console = Console()


def _backup_state(state_db: StateDB) -> None:
    """Backup da state.db antes de qualquer operação que a altere em bloco."""
    path = state_db.backup()
    state_db.prune_backups(keep=10)
    console.print(f"[dim]Backup da state.db: {path.name}[/dim]")


def _expand_job_alerts(
    messages: list[RecruiterMessage], state_db: StateDB, reprocess: bool
) -> list[RecruiterMessage]:
    """Job alerts passam a uma mensagem por vaga (alertas já processados ficam como estão)."""
    expanded: list[RecruiterMessage] = []
    for message in messages:
        if message.email_type == "job_alert" and (
            reprocess or not state_db.is_email_processed(message.message_id)
        ):
            expanded.extend(split_job_alert(message))
        else:
            expanded.append(message)
    return expanded


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Converte string YYYY-MM-DD para datetime (início do dia UTC)."""
    if not date_str:
        return None
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


@app.command()
def auth() -> None:
    """Executa o fluxo OAuth do Gmail (guarda token.json)."""
    try:
        GmailClient()
        console.print("[green]Autenticação Gmail concluída com sucesso.[/green]")
    except Exception as exc:
        console.print(f"[red]Erro na autenticação: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def setup_notion() -> None:
    """Cria/valida a database no Notion."""
    Config.validate()
    try:
        board = NotionBoard()
        database_id = board.ensure_database()
        console.print(f"[green]Database do Notion pronta: {database_id}[/green]")
    except Exception as exc:
        console.print(f"[red]Erro ao configurar Notion: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def debug_notion() -> None:
    """Mostra o schema real da database 'Propostas' no Notion."""
    Config.validate()
    try:
        import json

        import httpx

        board = NotionBoard()
        db_id = board.find_existing_database()
        if not db_id:
            console.print("[red]Database 'Propostas' não encontrada.[/red]")
            raise typer.Exit(1)

        # Via notion-client
        db = board.client.databases.retrieve(database_id=db_id)
        console.print(f"[cyan]Database ID: {db_id}[/cyan]")
        console.print("[cyan]Via notion-client (properties):[/cyan]")
        console.print(json.dumps(db.get("properties", {}), indent=2, ensure_ascii=False))

        # Via API REST direta
        response = httpx.get(
            f"https://api.notion.com/v1/databases/{db_id}",
            headers={
                "Authorization": f"Bearer {Config.notion_token}",
                "Notion-Version": "2022-06-28",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        console.print("[cyan]Via API REST direta (properties):[/cyan]")
        console.print(json.dumps(data.get("properties", {}), indent=2, ensure_ascii=False))
        console.print("[cyan]Database object keys:[/cyan]")
        console.print(json.dumps(list(data.keys()), indent=2, ensure_ascii=False))
    except Exception as exc:
        console.print(f"[red]Erro: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def reset_notion(
    force: bool = typer.Option(False, "--force", help="Não pede confirmação")
) -> None:
    """Apaga a database 'Propostas' existente e cria uma nova."""
    Config.validate()
    try:
        board = NotionBoard()
        if not force:
            confirmed = typer.confirm(
                "Isto apaga a database 'Propostas' e todos os cards. Continuar?"
            )
            if not confirmed:
                console.print("[yellow]Cancelado.[/yellow]")
                raise typer.Exit(0)
        _backup_state(board.db)
        deleted = board.delete_database()
        if deleted:
            board.db.clear_proposals()
            console.print(
                "[yellow]Database antiga apagada (mapeamento local de cards limpo).[/yellow]"
            )
        database_id = board.ensure_database()
        console.print(f"[green]Database do Notion recriada: {database_id}[/green]")
    except Exception as exc:
        console.print(f"[red]Erro ao recriar Notion: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def estimate(
    since: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    until: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),
    label: Optional[str] = typer.Option(None, help="Label do Gmail (ex.: linkedin/messages)"),
    list_emails: bool = typer.Option(
        False, "--list", help="Lista remetente e assunto de cada email"
    ),
) -> None:
    """Conta quantos emails do LinkedIn existem na janela (sem chamar LLM)."""
    try:
        client = GmailClient()
        since_dt = _parse_date(since)
        until_dt = _parse_date(until)
        query = client.build_query(since=since_dt, until=until_dt, label=label)
        console.print(f"Query: [cyan]{query}[/cyan]")

        messages = client.fetch_messages(
            since=since_dt, until=until_dt, label=label
        )
        console.print(
            f"[green]{len(messages)} emails de notificação do LinkedIn encontrados.[/green]"
        )
        if list_emails:
            table = Table(title="Emails encontrados")
            table.add_column("Data", style="cyan")
            table.add_column("Remetente")
            table.add_column("Assunto")
            for msg in messages:
                table.add_row(
                    msg.received_at.date().isoformat(),
                    msg.sender_name,
                    msg.subject[:80],
                )
            console.print(table)
    except Exception as exc:
        console.print(f"[red]Erro: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def run(
    since: Optional[str] = typer.Option(None, help="YYYY-MM-DD (inclusive)"),
    until: Optional[str] = typer.Option(None, help="YYYY-MM-DD (exclusive)"),
    label: Optional[str] = typer.Option(None, help="Label do Gmail"),
    limit: Optional[int] = typer.Option(None, help="Máximo de emails a processar"),
    dry_run: bool = typer.Option(False, help="Não escreve no Notion"),
    skip_enrichment: bool = typer.Option(False, help="Não enriquece empresas"),
    skip_assessment: bool = typer.Option(
        False, "--skip-assessment", help="Não avalia fit/ação (mesmo com profile.md)"
    ),
    process_all: bool = typer.Option(
        False,
        "--process-all",
        help="Processa todas as mensagens, incluindo notificações óbvias",
    ),
    show_skipped: bool = typer.Option(
        False, "--show-skipped", help="Mostra mensagens ignoradas pelo classificador"
    ),
    reprocess: bool = typer.Option(
        False,
        "--reprocess",
        help="Reprocessa emails que já constam na base de dados local",
    ),
    unread_only: bool = typer.Option(
        False,
        "--unread-only",
        help="Processa apenas emails não lidos no Gmail",
    ),
    since_last_run: bool = typer.Option(
        False,
        "--since-last-run",
        help="Processa apenas emails desde a última execução do pipeline",
    ),
    mark_as_read: bool = typer.Option(
        False,
        "--mark-as-read",
        help="Marca os emails processados como lidos no Gmail",
    ),
    fetch_job_details: bool = typer.Option(
        False,
        "--fetch-job-details",
        help="Job alerts: vai buscar a descrição completa da vaga ao LinkedIn (pedido público, sem login)",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Mostra cada proposta e pede confirmação antes de criar/atualizar o card",
    ),
    notify_done: bool = typer.Option(
        False, "--notify", help="Notificação macOS com o resumo no fim do run"
    ),
) -> None:
    """Executa o pipeline completo."""
    Config.validate()
    state_db = StateDB()
    gmail = GmailClient()
    # Um único LLMClient/JevClient partilhado por Classifier/Extractor/
    # Assessor -- permite somar custo/nº de chamadas do run inteiro, dos
    # dois lados, no resumo final.
    llm = LLMClient()
    jev = JevClient() if Config.jev_model else None
    classifier = Classifier(llm=llm, jev=jev)
    extractor = Extractor(llm=llm)
    enricher = None if skip_enrichment else Enricher()
    assessor = None if skip_assessment else Assessor(llm=llm, jev=jev)
    board = None if dry_run else NotionBoard()

    if jev:
        console.print(
            f"[cyan]Jev ativo ({jev.model}): is_opportunity, opportunity_type e "
            "recommended_action correm no modelo de decisão; o resto (extração, "
            "scores, texto) fica no LLM.[/cyan]"
        )

    if assessor and not assessor.available:
        console.print(
            "[yellow]Avaliação de fit ativa, mas falta profile.md. "
            "Copia profile.example.md para profile.md e adapta ao teu perfil.[/yellow]"
        )

    if enricher and not enricher.available:
        console.print(
            "[yellow]Enriquecimento de empresas ativo, mas TAVILY_API_KEY não está definida no .env. "
            "Os cards ficam apenas com os links extraídos das mensagens.[/yellow]"
        )

    if since_last_run:
        last_run = state_db.get_last_run_at()
        if last_run:
            since_dt = last_run
            console.print(
                f"[cyan]Última execução: {last_run.date().isoformat()}. A processar emails desde essa data.[/cyan]"
            )
        else:
            since_dt = _parse_date(since)
            console.print(
                "[yellow]Nenhuma execução anterior encontrada. Usando --since fornecido.[/yellow]"
            )
    else:
        since_dt = _parse_date(since)

    until_dt = _parse_date(until)

    console.print(
        f"[bold]A procurar mensagens do LinkedIn[/bold] desde {since_dt.date().isoformat() if since_dt else 'sempre'} até {until_dt.date().isoformat() if until_dt else 'hoje'}..."
    )
    messages = gmail.fetch_messages(
        since=since_dt,
        until=until_dt,
        label=label,
        max_results=limit,
        unread_only=unread_only,
    )
    console.print(f"[cyan]{len(messages)} emails encontrados[/cyan]")
    messages = _expand_job_alerts(messages, state_db, reprocess)
    console.print(f"[cyan]{len(messages)} mensagens a avaliar (job alerts divididos por vaga)[/cyan]")
    if not dry_run:
        _backup_state(state_db)

    proposals: list[Proposal] = []
    skipped = 0
    errors = 0

    for message in messages:
        if not reprocess and state_db.is_email_processed(message.message_id):
            skipped += 1
            if show_skipped:
                console.print(
                    f"  [dim]⊘ {message.sender_name}: já processado anteriormente[/dim]"
                )
            continue

        if not process_all and message.email_type == "notification":
            if not dry_run:
                state_db.mark_email_processed(message.message_id, "not_opportunity")
            skipped += 1
            if show_skipped:
                console.print(
                    f"  [dim]⊘ {message.sender_name}: notificação do LinkedIn[/dim]"
                )
            continue

        try:
            if process_all:
                classification = Classification(
                    is_opportunity=True, confidence=1.0, reason="--process-all ativo"
                )
            elif message.email_type == "job_alert" and message.job_id:
                # Uma vaga listada num alerta é uma oportunidade por definição --
                # não vale gastar classificação (nem arriscar um falso negativo).
                classification = Classification(
                    is_opportunity=True, confidence=1.0, reason="vaga de job alert"
                )
            else:
                classification = classifier.classify(message)

            if not classification.is_opportunity:
                if not dry_run:
                    state_db.mark_email_processed(
                        message.message_id, "not_opportunity"
                    )
                skipped += 1
                if show_skipped:
                    console.print(
                        f"  [dim]⊘ {message.sender_name}: {classification.reason}[/dim]"
                    )
                continue

            if fetch_job_details and message.job_id:
                description = fetch_job_description(message.job_id)
                if description:
                    message = message.model_copy(
                        update={"body_text": f"{message.body_text}\n\n{description}"}
                    )
                time.sleep(1.0)  # simpático com o LinkedIn

            extraction = extractor.extract(message)
            enrichment = None
            # Enriquece a empresa cliente; se for confidencial, enriquece a agência
            enrich_target = extraction.company_client or extraction.agency
            if enricher and enrich_target:
                enrichment = enricher.enrich(
                    enrich_target,
                    website_hint=extraction.company_website,
                    linkedin_hint=extraction.company_linkedin
                    or message.company_linkedin_guess,
                )
            assessment = None
            if assessor and assessor.available:
                assessment = assessor.assess(message, extraction, enrichment)

            proposal = Proposal(
                message=message,
                classification=classification,
                extraction=extraction,
                enrichment=enrichment,
                assessment=assessment,
            )
            if review:
                label = extraction.company_client or extraction.agency or "Confidencial"
                action = assessment.recommended_action if assessment else "—"
                fit = assessment.fit_score if assessment else "—"
                console.print(
                    f"  [bold]{label}[/bold] — {extraction.role_title or 'role desconhecido'} "
                    f"| fit {fit} | {triage(action) if assessment else action}"
                )
                if not typer.confirm("  Criar/atualizar este card?", default=True):
                    skipped += 1
                    continue
            proposals.append(proposal)

            if not dry_run and board:
                page_id = board.upsert_proposal(
                    fingerprint=proposal.fingerprint(),
                    extraction=extraction,
                    message=message,
                    enrichment=enrichment,
                    assessment=assessment,
                )
                state_db.record_assessment(
                    message,
                    proposal.fingerprint(),
                    classification,
                    extraction,
                    assessment,
                )
                # Só DMs têm um recruiter real: num job alert o "remetente" é
                # o assunto do alerta (ou o próprio utilizador).
                is_self = bool(Config.user_name) and (
                    extraction.recruiter_name or ""
                ).strip().lower() == Config.user_name.strip().lower()
                if extraction.recruiter_name and message.email_type == "dm" and not is_self:
                    state_db.record_recruiter(
                        extraction.recruiter_name,
                        extraction.agency,
                        received_at=message.received_at.isoformat(),
                        message_id=message.message_id,
                    )
                state_db.mark_email_processed(
                    message.message_id,
                    "proposal",
                    fingerprint=proposal.fingerprint(),
                )
                if mark_as_read:
                    gmail.mark_as_read(message.message_id)
                console.print(
                    f"  [green]✓[/green] {extraction.company_client or extraction.agency or 'Confidencial'} — {extraction.role_title or 'Role desconhecido'}"
                )
            else:
                # dry-run: não grava nada em state_db -- uma pré-visualização
                # não pode mudar o que um run real a seguir vai processar.
                console.print(
                    f"  [yellow]~[/yellow] {extraction.company_client or extraction.agency or 'Confidencial'} — {extraction.role_title or 'Role desconhecido'}"
                )

        except Exception as exc:
            console.print(f"  [red]✗ Erro a processar {message.message_id}: {exc}[/red]")
            errors += 1

    _print_summary(proposals, skipped, errors, dry_run)
    console.print(
        f"[cyan]LLM: {llm.call_count} chamadas, {llm.total_tokens} tokens totais[/cyan]"
    )
    if jev:
        console.print(
            f"[cyan]Jev: {jev.call_count} chamadas, custo total "
            f"${jev.total_cost:.4f}[/cyan]"
        )
    if proposals:
        title, body = build_digest(proposals)
        console.print(f"[bold]{title}[/bold]\n{body}")
        if notify_done and not dry_run and not notify(title, body):
            console.print("[yellow]Não consegui enviar a notificação macOS.[/yellow]")
    if not dry_run:
        # dry-run não avança o checkpoint de --since-last-run nem grava no
        # histórico de runs -- é só uma pré-visualização.
        state_db.record_run(
            emails_processed=len(messages),
            proposals_created=len(proposals),
            errors=errors,
        )


@app.command()
def enrich_board(
    refresh_cache: bool = typer.Option(
        False, "--refresh-cache", help="Ignora a cache local e pesquisa de novo na web"
    ),
    all_cards: bool = typer.Option(
        False, "--all", help="Reenriquece mesmo cards que já têm links"
    ),
    dry_run: bool = typer.Option(False, help="Mostra o que faria sem escrever no Notion"),
) -> None:
    """Reenriquece cards existentes cujas empresas passaram a ser conhecidas.

    Caso de uso: após a primeira entrevista descobres qual é a empresa cliente,
    editas o campo 'Empresa' no card do Notion e depois corres este comando para
    preencher Website, LinkedIn, Teamlyzer e a secção 'Sobre a empresa'.
    """
    Config.validate()
    board = NotionBoard()
    enricher = Enricher()
    state_db = StateDB()

    if not enricher.available:
        console.print(
            "[yellow]Aviso: TAVILY_API_KEY não definida. Só serão usados dados já em cache "
            "ou hints locais — sem pesquisa web nova.[/yellow]"
        )

    cards = board.list_cards()
    console.print(f"[cyan]{len(cards)} cards no board[/cyan]")

    enriched = 0
    skipped = 0
    for card in cards:
        company = (card["company"] or "").strip()
        if not company or company.lower() in ("confidencial", "null", "unknown", "—"):
            skipped += 1
            console.print(f"  [dim]⊘ empresa desconhecida ({card['role'] or 'sem role'})[/dim]")
            continue

        already_has_links = card["website"] or card["linkedin"]
        if already_has_links and not all_cards:
            skipped += 1
            console.print(f"  [dim]⊘ {company}: já tem links[/dim]")
            continue

        enrichment = enricher.enrich(
            company,
            website_hint=card["website"],
            linkedin_hint=card["linkedin"],
            force_refresh=refresh_cache,
        )
        if not enrichment or not (
            enrichment.website or enrichment.linkedin_url or enrichment.teamlyzer_url
        ):
            skipped += 1
            console.print(f"  [dim]⊘ {company}: sem dados de enriquecimento[/dim]")
            continue

        console.print(f"  [green]✓ {company}[/green]")
        if not dry_run:
            board.update_card_links(
                card["page_id"],
                website=enrichment.website,
                linkedin=enrichment.linkedin_url,
                teamlyzer=enrichment.teamlyzer_url,
            )
            has_substance = bool(
                enrichment.description or enrichment.industry or enrichment.tech_stack
            )
            if has_substance and not board.has_enrichment_section(card["page_id"]):
                board.append_enrichment_section(card["page_id"], enrichment)
            # Mantém o mapeamento local coerente com a empresa (agora conhecida),
            # sem criar uma linha nova: o fingerprint existente é o da página.
            fingerprint = state_db.get_fingerprint_by_page(card["page_id"])
            if fingerprint:
                state_db.upsert_proposal(
                    fingerprint=fingerprint,
                    notion_page_id=card["page_id"],
                    company_client=company,
                    agency=card["agency"],
                    role_title=card["role"],
                )
        enriched += 1

    console.print(
        f"[green]{enriched} cards enriquecidos[/green], {skipped} ignorados"
        + (" [yellow](dry-run)[/yellow]" if dry_run else "")
    )


@app.command()
def recruiters() -> None:
    """Mostra estatísticas dos recruiters que já contactaram."""
    state_db = StateDB()
    rows = state_db.list_recruiters()
    if not rows:
        console.print("[yellow]Ainda sem recruiters registados.[/yellow]")
        return
    table = Table(title="Recruiters")
    table.add_column("Nome")
    table.add_column("Agência")
    table.add_column("Contactos", justify="right")
    table.add_column("Primeiro contacto")
    table.add_column("Último contacto")
    for row in rows:
        table.add_row(
            row["name"],
            row["agency"] or "—",
            str(row["total"]),
            (row["first_contact"] or "")[:10] or "—",
            (row["last_contact"] or "")[:10],
        )
    console.print(table)


@app.command()
def stats() -> None:
    """Auditoria das decisões: ações, taxa de ignore por fonte, Jev vs LLM, guardrails."""
    rows = StateDB().list_assessments()
    if not rows:
        console.print("[yellow]Ainda sem avaliações registadas (só runs novos gravam).[/yellow]")
        return
    total = len(rows)
    with_action = [r for r in rows if r["action"]]
    console.print(f"[bold]{total} avaliações[/bold] ({len(with_action)} com ação recomendada)")

    actions = Counter(r["action"] for r in with_action)
    table = Table(title="Ações recomendadas")
    table.add_column("Ação")
    table.add_column("Nº", justify="right")
    table.add_column("%", justify="right")
    for action, n in actions.most_common():
        table.add_row(f"{triage(action)} ({action})", str(n), f"{100 * n / len(with_action):.0f}%")
    console.print(table)

    by_source: dict[str, Counter] = {}
    for r in with_action:
        by_source.setdefault(r["source"] or "?", Counter())[r["action"]] += 1
    table = Table(title="Taxa de ignore por fonte")
    table.add_column("Fonte")
    table.add_column("Nº", justify="right")
    table.add_column("Ignore", justify="right")
    table.add_column("Info média em falta", justify="right")
    for source, counter in by_source.items():
        n = sum(counter.values())
        missing = [r["missing_count"] for r in with_action if (r["source"] or "?") == source and r["missing_count"] is not None]
        table.add_row(
            source,
            str(n),
            f"{100 * counter['ignore'] / n:.0f}%",
            f"{sum(missing) / len(missing):.1f}/8" if missing else "—",
        )
    console.print(table)

    sources = Counter(r["action_source"] or "llm" for r in with_action)
    jev_changes = sum(1 for r in with_action if "jev:" in (r["decision_trace"] or ""))
    guardrail = sum(1 for r in with_action if "guardrail:" in (r["decision_trace"] or ""))
    console.print(
        "Origem da decisão final: "
        + ", ".join(f"{k} {v}" for k, v in sources.most_common())
        + f"\nJev alterou a ação do LLM em {jev_changes} casos; guardrails corrigiram {guardrail}."
    )
    unknown_fit = sum(1 for r in with_action if r["fit_score"] is None)
    console.print(f"Sem fit calculável (info insuficiente): {unknown_fit}/{len(with_action)}")


@app.command()
def followups(
    days: int = typer.Option(7, help="Dias sem atividade para contar como parado"),
    new_days: int = typer.Option(2, help="Dias para uma proposta 'Responder' ainda em 'Nova'"),
) -> None:
    """Cards parados: à espera de resposta há muito tempo, ou boas propostas por tocar."""
    Config.validate()
    board = NotionBoard()
    now = datetime.now(timezone.utc)
    waiting_states = {"Respondida", "Recruiter screen", "Entrevista", "Proposta"}
    rows = []
    for card in board.list_cards():
        if card.get("archived") or not card.get("last_edited"):
            continue
        age = (now - datetime.fromisoformat(card["last_edited"].replace("Z", "+00:00"))).days
        estado = card.get("estado")
        label = card["company"] or "Confidencial"
        if estado in waiting_states and age >= days:
            rows.append((age, label, card["role"] or "—", f"{estado} parado há {age}d", card.get("url") or ""))
        elif (
            estado in (None, "Nova")
            and (card.get("triagem") or "").startswith("🟢")
            and age >= new_days
        ):
            rows.append((age, label, card["role"] or "—", f"boa proposta por tocar há {age}d", card.get("url") or ""))
    if not rows:
        console.print("[green]Nada parado.[/green]")
        return
    table = Table(title="Follow-ups")
    for col in ("Empresa", "Role", "Porquê", "Link"):
        table.add_column(col)
    for _, label, role, why, url in sorted(rows, reverse=True):
        table.add_row(label, role, why, url)
    console.print(table)


@app.command()
def learn() -> None:
    """Cruza as tuas decisões no Notion (estado + motivo) com o que o pipeline previu."""
    Config.validate()
    report = analyze_feedback(NotionBoard().list_cards())
    console.print(
        f"{report['advanced']} avançaram (fit médio {report['avg_fit_advanced']}), "
        f"{report['rejected']} rejeitadas/arquivadas (fit médio {report['avg_fit_rejected']})."
    )
    if report["rejected_without_reason"]:
        console.print(
            f"[yellow]{report['rejected_without_reason']} rejeitadas sem 'Motivo' -- "
            "preenche o campo para o pipeline aprender.[/yellow]"
        )
    if report["reasons"]:
        table = Table(title="Motivos de rejeição")
        table.add_column("Motivo")
        table.add_column("Nº", justify="right")
        for reason, n in report["reasons"].most_common():
            table.add_row(reason, str(n))
        console.print(table)
        top, n = report["reasons"].most_common(1)[0]
        if n >= 3:
            console.print(
                f"[cyan]Sugestão:[/cyan] '{top}' domina ({n}x) -- torna isso um dealbreaker explícito "
                "na secção '## Regras' do profile.md."
            )
    if report["optimistic"]:
        console.print(
            f"[yellow]{len(report['optimistic'])} propostas recomendadas 'Responder' foram rejeitadas "
            "-- o perfil está demasiado otimista nesses casos.[/yellow]"
        )
    if report["pessimistic"]:
        console.print(
            f"[yellow]{len(report['pessimistic'])} propostas marcadas 'Descartar' acabaram a avançar "
            "-- os descartes estão demasiado agressivos.[/yellow]"
        )


@app.command()
def reconcile(
    fix: bool = typer.Option(False, "--fix", help="Aplica as correções à state.db"),
) -> None:
    """Compara a state.db com os cards reais do Notion (órfãos e mapeamentos mortos)."""
    Config.validate()
    board = NotionBoard()
    state_db = board.db
    cards = {c["page_id"]: c for c in board.list_cards() if not c.get("archived")}
    proposals = state_db.list_proposals()
    mapped = {p["notion_page_id"] for p in proposals}

    stale = [p for p in proposals if p["notion_page_id"] not in cards]
    orphans = [c for pid, c in cards.items() if pid not in mapped]
    console.print(
        f"{len(cards)} cards no Notion, {len(proposals)} linhas na state.db: "
        f"[red]{len(stale)} mapeamentos mortos[/red], [yellow]{len(orphans)} cards sem mapeamento[/yellow]."
    )
    for p in stale:
        console.print(f"  mapeamento morto: {p['company_client'] or p['agency']} — {p['role_title']}")
    for c in orphans:
        console.print(f"  card órfão: {c['company']} — {c['role']} ({c.get('url')})")
    if not fix:
        if stale or orphans:
            console.print("Corre com [bold]--fix[/bold] para corrigir (faz backup antes).")
        return
    _backup_state(state_db)
    for p in stale:
        state_db.delete_proposal(p["fingerprint"])
    for c in orphans:
        company = (c["company"] or "").strip()
        fingerprint = f"{company.lower()}::{(c['role'] or 'unknown').lower()}::reconciled::{c['page_id'][:8]}"
        state_db.upsert_proposal(
            fingerprint=fingerprint,
            notion_page_id=c["page_id"],
            company_client=None if company.lower() == "confidencial" else company,
            agency=c["agency"],
            role_title=c["role"],
        )
    console.print(f"[green]{len(stale)} removidos, {len(orphans)} registados.[/green]")


@app.command()
def dedupe(
    apply: bool = typer.Option(
        False, "--apply", help="Arquiva os cards duplicados no Notion e junta as linhas locais"
    ),
) -> None:
    """Encontra propostas duplicadas já existentes (mantém a mais antiga de cada grupo)."""
    state_db = StateDB()
    groups = state_db.find_duplicate_groups()
    if not groups:
        console.print("[green]Sem duplicados prováveis.[/green]")
        return
    for group in groups:
        keep, *drops = group
        console.print(
            f"[bold]{keep['company_client'] or keep['agency'] or 'Confidencial'} — {keep['role_title']}[/bold]"
        )
        console.print(f"  manter: {keep['location'] or '—'} ({keep['notion_page_id']})")
        for d in drops:
            console.print(f"  [red]duplicado:[/red] {d['location'] or '—'} ({d['notion_page_id']})")
    total = sum(len(g) - 1 for g in groups)
    if not apply:
        console.print(f"\n{total} duplicados em {len(groups)} grupos. Corre com [bold]--apply[/bold] para os arquivar.")
        return
    Config.validate()
    if not typer.confirm(f"Arquivar {total} cards no Notion (recuperáveis no lixo) e juntar na state.db?"):
        raise typer.Exit(0)
    _backup_state(state_db)
    board = NotionBoard(db=state_db)
    for group in groups:
        keep, *drops = group
        for d in drops:
            try:
                board.archive_page(d["notion_page_id"])
            except Exception as exc:
                console.print(f"  [red]Falha a arquivar {d['notion_page_id']}: {exc}[/red]")
                continue
            state_db.merge_proposals(keep["fingerprint"], d["fingerprint"])
    console.print("[green]Duplicados arquivados e juntados.[/green]")


@app.command()
def maintenance() -> None:
    """Backup + limpeza de lixo histórico (dry-runs gravados, alertas como recruiters)."""
    state_db = StateDB()
    _backup_state(state_db)
    result = state_db.cleanup_invalid_state(user_name=Config.user_name or None)
    console.print(
        f"[green]Removidos: {result['dry_run_emails']} emails 'proposal_dry_run', "
        f"{result['invalid_recruiters']} recruiters inválidos.[/green]"
    )
    if not Config.user_name:
        console.print(
            "[dim]Define USER_NAME no .env para também excluir o teu nome da tabela de recruiters.[/dim]"
        )


def _print_summary(
    proposals: list[Proposal], skipped: int, errors: int, dry_run: bool
) -> None:
    table = Table(title="Resumo do pipeline")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", style="magenta")
    table.add_row("Propostas encontradas", str(len(proposals)))
    table.add_row("Ignorados / duplicados", str(skipped))
    table.add_row("Erros", str(errors))
    table.add_row("Modo dry-run", "Sim" if dry_run else "Não")
    console.print(table)

    if proposals:
        detail = Table(title="Propostas")
        detail.add_column("Empresa")
        detail.add_column("Agência")
        detail.add_column("Role")
        detail.add_column("Confiança")
        for p in proposals:
            detail.add_row(
                p.extraction.company_client or "Confidencial",
                p.extraction.agency or "—",
                p.extraction.role_title or "—",
                f"{p.extraction.confidence:.2f}",
            )
        console.print(detail)


if __name__ == "__main__":
    app()
