# recruiter-pipeline — Status & Handoff para outro agente

Documento de contexto completo: o que a aplicação faz, como faz, e o estado atual.

## Objetivo

Pipeline local (macOS) que transforma mensagens de recruiters do LinkedIn num **Career/Recruiting CRM pessoal** num board do Notion. Lê emails de notificação do LinkedIn no Gmail, classifica, extrai dados estruturados, enriquece empresas, avalia o fit contra o perfil do utilizador, e cria/atualiza cards decision-first no Notion.

Stack: Python 3.12, `uv`, Gmail API (OAuth), OpenRouter (LLM, `openai/gpt-4o-mini` por defeito), Tavily (pesquisa web), Notion API, SQLite (estado).

## Arquitetura (fluxo de dados)

```
LinkedIn envia email para Gmail
  → GmailClient (OAuth, query from:linkedin.com, parse de HTML/texto)
  → classify_email_type() determinístico: dm | job_alert | notification
      (primário: endereço de envio; fallback: assunto; desconhecido → notification)
  → [se DM] Classifier (LLM) — é oportunidade? is_opportunity + confidence
  → [se sim] Extractor (LLM) → ProposalExtraction (dados estruturados)
  → Assessor (LLM, com profile.md) → OpportunityAssessment (fit, ação, perguntas)
  → Enricher (Tavily + LLM) → CompanyEnrichment (website, LinkedIn, Teamlyzer, descrição)
  → NotionBoard.upsert_proposal() → cria/atualiza card
  → StateDB (SQLite) → dedupe, cache de empresas, recruiters, runs
```

## Módulos (`src/recruiter_pipeline/`)

| Ficheiro | Responsabilidade |
|---|---|
| `config.py` | Carrega `.env`; caminhos para credentials/token/profile. |
| `models.py` | Pydantic: `LinkedInMessage`, `Classification`, `ProposalExtraction`, `CompanyEnrichment`, `OpportunityAssessment`, `Proposal`. Helpers: `missing_information()`, `_normalize()` (dedupe). |
| `gmail_client.py` | OAuth Gmail, `fetch_messages()`, parse de MIME, `classify_email_type()`, extrai permalink e `linkedin.com/company/*` do HTML. |
| `classifier.py` | LLM: a mensagem é uma proposta de emprego? |
| `extractor.py` | LLM: extrai dados estruturados; `_sanitize()` converte "null"/"None" em None. |
| `assessor.py` | LLM: avalia fit/ação/perguntas contra `profile.md`. Sem perfil → devolve None (Fit vazio). |
| `enricher.py` | Pesquisa web (Tavily) + síntese LLM; cache 30 dias; deterministic pickers de URLs. Graceful sem chave Tavily. |
| `notion_board.py` | Cria/valida database, auto-migra schema (PATCH), upsert de cards decision-first, `enrich-board`. |
| `db.py` | SQLite: `processed_emails`, `proposals` (fingerprint→page), `company_cache`, `runs`, `recruiters`. |
| `main.py` | CLI Typer: `run`, `enrich-board`, `recruiters`, `estimate`, `auth`, `setup-notion`, `reset-notion`, `debug-notion`. |
| `llm.py` | Cliente OpenRouter com structured output (response_model Pydantic). |

Prompts em `prompts/`: `classify.txt`, `extract.txt`, `assess.txt`.

## Modelo de dados-chave

**ProposalExtraction** (só factos da mensagem; nunca inventa): recruiter_name/headline, agency vs company_client (confidencial → company_client=None, company_confidential=True), company_website/linkedin, role_title, seniority, stack[], location, remote_policy, salary, contract_type, language, summary, confidence.

**OpportunityAssessment**: opportunity_type (job_opportunity|recruiter_outreach|connection|referral|generic|unknown), fit_score (0-100, null se info insuficiente), fit_confidence (0-100), dimensões (role/seniority/stack/remote/location/company, null se desconhecido), recommended_action (reply|ask_for_more_information|apply|ignore|monitor|connect), action_reason, questions_to_ask[].

**CompanyEnrichment**: name, website, linkedin_url, teamlyzer_url, industry, size, headquarters, description, tech_stack[], recent_news[], notes. `to_markdown()`.

**Dedupe (duas camadas)**:
- Primário: `fingerprint = normalize(company)::normalize(role)::normalize(location)::normalize(contract)`. Localização/contrato distinguem vagas com o mesmo título na mesma empresa/agência; ficam vazios quando desconhecidos.
- Secundário (fuzzy): `opportunities_match()` — mesma empresa + role com similaridade de tokens (Jaccard ≥ 0.5) e sem contradição de localização/contrato. Corre só quando o fingerprint exato falha; ao dar match migra o fingerprint antigo para o novo formato. Implementado em `StateDB.find_proposal()`.

**Source / proveniência**: `LinkedInMessage.source` (LinkedIn DM / Job Alert / Email / Recruiter Email / Manual). Cada proposta guarda `source_message_id/url/received_at`; a tabela `proposal_sources` acumula cada avistamento (mesma oportunidade chegada por vários emails/canais). No Notion há a propriedade **Fonte** (select).

## Card do Notion (decision-first)

Ordem dos blocos: **Decisão** (tipo, Fit + confiança, ação recomendada + motivo, scores por dimensão) → **Informação em falta** (lista determinística) + **Perguntas a fazer** → **Resumo** → **Sobre a empresa** (enrichment) → **Mensagem original** (chunked a 1990 chars).

Propriedades: Name/Empresa/Agencia/Recruiter/Role/Stack/Localizacao/Remote/Salario/Contrato, Estado (funil CRM: Nova→A avaliar→Respondida→Recruiter screen→Entrevista→Proposta→Aceite/Rejeitada/Arquivada), Tipo Oportunidade, Fit, Fit Confianca, Acao, Confianca (Alta/Média/Baixa, da extração), Tipo (DM/Job Alert/Notification), **Fonte** (select de proveniência), Website/LinkedIn/Teamlyzer (urls), **Feedback** (rich_text, para o feedback loop futuro), Recebida (date), Link (url).

`ensure_database()` valida o schema e **auto-adiciona propriedades em falta** via PATCH (sem apagar dados); falha só em type mismatch.

## Comandos principais

```bash
uv run -m recruiter_pipeline.main auth                 # OAuth Gmail
uv run -m recruiter_pipeline.main setup-notion         # cria/valida database
uv run -m recruiter_pipeline.main run --since 2026-01-01 [--reprocess] [--dry-run]
uv run -m recruiter_pipeline.main run --since-last-run # incremental (usado pelo launchd)
uv run -m recruiter_pipeline.main enrich-board [--all] [--refresh-cache]  # reenriquece cards
uv run -m recruiter_pipeline.main recruiters           # stats de recruiters
uv run -m recruiter_pipeline.main estimate --since ... --list
uv run pytest                                          # 29 testes
```

Flags úteis do `run`: `--skip-assessment`, `--skip-enrichment`, `--include-job-alerts`, `--process-all`, `--unread-only`, `--mark-as-read`, `--show-skipped`.

## Decisões de design importantes

- **Classificação DM/job_alert por remetente, não por assunto** — bug anterior: padrão `"you have"` e fallback "dm" classificavam job alerts como DM. Agora `SENDER_TYPE_MAP` (messages-noreply→dm, jobalerts-noreply→job_alert, invitations/notifications/newsletters/groups→notification); assunto "job alert" tem prioridade; desconhecido→notification (conservador, não gasta LLM).
- **Fit ≠ confiança**; **factos ≠ inferências** (extração só da mensagem; enrichment marcado como tal; null onde desconhecido).
- **Enrichment gracioso**: sem Tavily ou 401 → avisa uma vez, desativa no run, usa só hints da mensagem; não cacheia resultados pobres.
- **Agência ≠ empresa cliente** — distinção explícita; confidencial → enrich a agência.
- **`enrich-board`** cobre o caso "empresa confidencial revelada após 1ª entrevista": editas Empresa no card, corres o comando, e preenche links + secção "Sobre a empresa" (só se ainda não existir — não sobrescreve notas do user).
- Recuperação de páginas arquivadas: `upsert_proposal` apanha exceção e recria a página; `reset-notion` limpa o mapeamento local (`clear_proposals`).

## Estado atual

- **36 testes a passar** (`tests/`: gmail_client, db, enricher, assessment).
- Último run real: **37 propostas** no board, **18 recruiters** registados.
- `profile.md` existe mas é uma cópia do exemplo — **precisa de ser personalizado** (o fit_score só é tão bom quanto o perfil).
- `TAVILY_API_KEY` é placeholder → enrichment web limitado a hints das mensagens até obter chave real.
- launchd corre `run --since-last-run` às 9h e 19h.

## Atualização (branch feat/decision-quality-and-dedupe)

- `decision.py`: guardrails determinísticos (`ignore` precisa de evidência; `reply/apply` precisam de empresa+role) e `## Regras` do profile.md (`avoid_stack/companies/agencies`). Triagem 🟢🟡⚪🔴.
- `OpportunityAssessment`: + `reply_draft`, `action_source`, `decision_trace`, `jev_confidence`. O rasto do Jev já NÃO vai para `action_reason`/card.
- `state.db`: nova tabela `assessments` (auditoria por mensagem); `proposals` + `job_id`, `recruiter_name`.
- Dedupe em 4 camadas em `StateDB.find_proposal`: job_id → fingerprint → fuzzy cliente-OU-agência (localização tolerante, `locations_compatible`) → recruiter+role quando a empresa é desconhecida.
- `job_alerts.py`: um alerta → uma mensagem por vaga (`<msg_id>#job<id>`); `--fetch-job-details` opcional.
- Notion: `Triagem` e `Motivo`; card com âncora 🤖 + secção "Minhas notas" preservada em updates (`_refresh_page_content`).
- Comandos novos: `stats`, `followups`, `learn`, `reconcile`, `dedupe`, `maintenance`; `run --review/--notify/--fetch-job-details`; backup automático da state.db.
- `enrich-board` deixou de criar linhas de proposals com fingerprint de formato errado.
- Testes: `tests/conftest.py` neutraliza `JEV_MODEL` do `.env` (antes um teste chamava o Jev a sério).

## Trabalho futuro (não implementado)

Ligado ao feedback-loop: o campo **Feedback** + estados do Kanban começam a capturar decisões do utilizador. Próximos passos por prioridade:
1. Ler Feedback/estado dos cards e injetar preferências aprendidas no prompt do Assessor.
2. Recruiter quality score (relevância das oportunidades enviadas por cada recruiter, da tabela `recruiters` + fit histórico).
3. Histórico de interações por oportunidade (timeline no card).
