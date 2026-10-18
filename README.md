# recruiter-pipeline

Pipeline local que lê notificações de mensagens do LinkedIn no Gmail, classifica propostas de recruiters com LLM (via OpenRouter), extrai dados estruturados — incluindo a distinção entre **agência de recrutamento** e **empresa cliente** —, enriquece cada empresa com pesquisa web e cria/atualiza cards num board do Notion.

## Arquitetura

```
Emails LinkedIn (Gmail API) → parse → LLM: classificação → LLM: extração estruturada
                                                            ↓
                              Notion board ← enriquecimento web da empresa cliente
                                                            ↓
                                       SQLite local (dedupe + cache)
```

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Conta Google com Gmail
- Conta Notion com permissão para criar integrations
- API key da [OpenRouter](https://openrouter.ai/)
- (Opcional) API key da [Tavily](https://tavily.com/) para enriquecimento de empresas

## Setup

### 1. Clona e entra no projeto

```bash
git clone <url-do-repo> recruiter-pipeline
cd recruiter-pipeline
uv sync
```

### 2. Configura as notificações do LinkedIn

No LinkedIn, vai a **Settings & Privacy → Communications → Email**. Ativa as notificações de mensagens para que recebas um email de `messages-noreply@linkedin.com` sempre que alguém te enviar uma mensagem.

> Dica: cria um filtro no Gmail que aplica a label `linkedin/messages` a estes emails. Podes depois usar `--label linkedin/messages` nos comandos.

### 3. Cria credenciais OAuth do Gmail

1. Vai à [Google Cloud Console](https://console.cloud.google.com/).
2. Cria um projeto (ou usa um existente).
3. Ativa a **Gmail API**.
4. Vai a **Credentials → Create Credentials → OAuth client ID**.
5. Seleciona **Desktop app**.
6. Descarrega o JSON e guarda-o como `credentials.json` na raiz do projeto.

### 4. Autentica o Gmail

```bash
uv run -m recruiter_pipeline.main auth
```

Isto abre o browser, pede permissão e guarda `token.json`.

### 5. Configura o Notion

1. Vai a [notion.so/my-integrations](https://www.notion.so/my-integrations) e cria uma integration interna.
2. Copia o **Internal Integration Token**.
3. No Notion, cria uma página onde queres que o board fique e partilha-a com a integration (**Add connections → nome-da-tua-integration**).
4. Copia o ID da página do URL.

### 6. Variáveis de ambiente

```bash
cp .env.example .env
```

Edita `.env` com as tuas chaves:

```env
OPENROUTER_API_KEY=sk-or-v1-...
# Modelo sugerido: openai/gpt-4o-mini (barato, bom structured output)
# Vê outros em https://openrouter.ai/models
OPENROUTER_MODEL=openai/gpt-4o-mini
# Opcional: modelo separado só para Classifier e Assessor -- ambos são
# decisões tipadas (bool/scores/ação de conjunto fechado), candidatos
# naturais a um "System One" model como o typesafe/jev-1.14 (via
# OpenRouter). O Extractor nunca usa isto -- tem campos de texto livre
# (nomes, resumo) que esse tipo de modelo não gera. Vazio = usa
# OPENROUTER_MODEL como os outros passos, sem mudança de comportamento.
JEV_MODEL=
NOTION_TOKEN=secret_...
NOTION_PARENT_PAGE_ID=...
TAVILY_API_KEY=tvly-...
```

### 7. Cria a database no Notion

```bash
uv run -m recruiter_pipeline.main setup-notion
```

Depois abre a página no Notion e adiciona uma vista **Kanban** agrupada por **Estado**.

## Uso

### Correr o pipeline (modo seguro, só mostra o que faria)

```bash
uv run -m recruiter_pipeline.main run --dry-run --since 2025-01-01
```

### Correr o pipeline e escrever no Notion

```bash
uv run -m recruiter_pipeline.main run --since 2025-01-01
```

### Backfill de mensagens antigas

```bash
uv run -m recruiter_pipeline.main run --since 2024-01-01 --until 2024-12-31
```

### Estimar volume antes de gastar tokens

```bash
uv run -m recruiter_pipeline.main estimate --since 2025-01-01 --list
```

### Reenriquecer cards após a primeira entrevista

Quando uma proposta era "Confidencial" e descobres a empresa (ex.: após a primeira entrevista), edita o campo **Empresa** do card no Notion e depois corre:

```bash
uv run -m recruiter_pipeline.main enrich-board
```

Isto preenche Website, LinkedIn, Teamlyzer e a secção "Sobre a empresa" nos cards que ainda não têm links. Opções: `--all` (reenriquece tudo), `--refresh-cache` (ignora cache de 30 dias), `--dry-run`.

### Avaliação de fit (profile.md)

Cada proposta é avaliada contra o teu perfil: tipo de oportunidade, **Fit** (0-100) separado de **Fit Confiança**, scores por dimensão, ação recomendada e perguntas a fazer.

Para ativar, copia o exemplo e adapta ao teu perfil:

```bash
cp profile.example.md profile.md
```

Sem `profile.md`, o pipeline corre normalmente mas sem avaliação (Fit fica vazio). Usa `--skip-assessment` para desligar pontualmente.

Cada card fica com propriedades decisórias no topo: **Decisão** (tipo, fit, ação recomendada), **Informação em falta** + perguntas, depois resumo e mensagem original no fundo.

### Estatísticas de recruiters

O pipeline regista cada recruiter que contacta. Para ver:

```bash
uv run -m recruiter_pipeline.main recruiters
```

### Outras opções

```bash
uv run -m recruiter_pipeline.main run --help
```

## Agendamento automático (launchd)

O ficheiro `launchd/com.example.recruiter-pipeline.plist` é um template que corre o pipeline às 9h e às 19h. Substitui os placeholders `__UV_PATH__` e `__PROJECT_DIR__` ao instalar:

```bash
mkdir -p logs
sed -e "s|__UV_PATH__|$(which uv)|" -e "s|__PROJECT_DIR__|$(pwd)|" \
  launchd/com.example.recruiter-pipeline.plist \
  > ~/Library/LaunchAgents/com.example.recruiter-pipeline.plist
launchctl load ~/Library/LaunchAgents/com.example.recruiter-pipeline.plist
launchctl start com.example.recruiter-pipeline
```

Para ver logs:

```bash
tail -f logs/pipeline.log logs/pipeline.error.log
```

## Testes

```bash
uv run pytest
```

## Troubleshooting

### `Error 403: access_denied` no OAuth do Gmail

Significa que a Google não autorizou a app a aceder à tua conta. Verifica:

1. A **Gmail API** está ativada no projeto do Google Cloud.
2. O **OAuth consent screen** foi configurado:
   - Vai a **APIs & Services → OAuth consent screen**.
   - Seleciona **External** e **Create**.
   - Preenche o nome da app, email de suporte e email de contacto do developer.
3. Se o consent screen estiver em **Testing** (o padrão), adiciona o teu email Gmail em **Test users**.
4. Tenta novamente `uv run -m recruiter_pipeline.main auth`.

Se quiseres evitar a mensagem de "app não verificada", podes publicar a app (botão **Publish app**), mas para uso pessoal basta estar em Testing com o teu email como test user.

## Notas importantes

- O LinkedIn pode truncar o corpo da mensagem no email. Se isso acontecer, a extração fica marcada com confiança baixa e o card inclui o link direto para a conversa.
- A distinção agência ↔ empresa cliente depende do texto da mensagem. Se a empresa cliente não for mencionada, o card fica como **Confidencial**.
- O enriquecimento de empresas usa a API Tavily. Sem chave, o pipeline funciona mas não adiciona o relatório da empresa.
- A avaliação de fit usa o teu `profile.md`. Quanto mais detalhado (dealbreakers, salário mínimo, stack), melhor o score.

## Segurança

Nunca faças commit de `.env`, `credentials.json`, `token.json`, `state.db` ou `profile.md` — já estão no `.gitignore`. Usa `.env.example` e `profile.example.md` como base.
