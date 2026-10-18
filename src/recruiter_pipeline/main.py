"""CLI do pipeline de propostas de recruiters."""

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
from recruiter_pipeline.enricher import Enricher
from recruiter_pipeline.extractor import Extractor
from recruiter_pipeline.gmail_client import GmailClient, is_notification_email
from recruiter_pipeline.jev_client import JevClient
from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import Classification, Proposal
from recruiter_pipeline.notion_board import NotionBoard

app = typer.Typer(help="Pipeline de propostas de recruiters do LinkedIn")
console = Console()


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
            proposals.append(proposal)

            if not dry_run and board:
                page_id = board.upsert_proposal(
                    fingerprint=proposal.fingerprint(),
                    extraction=extraction,
                    message=message,
                    enrichment=enrichment,
                    assessment=assessment,
                )
                if extraction.recruiter_name:
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
            # Atualiza o mapeamento local para manter o dedupe consistente
            fingerprint = f"{company.lower().strip()}::{(card['role'] or 'unknown').lower().strip()}"
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
