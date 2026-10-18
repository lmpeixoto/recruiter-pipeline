"""Estado local em SQLite: dedupe, cache de empresas e emails processados."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.models import (
    Classification,
    OpportunityAssessment,
    ProposalExtraction,
    RecruiterMessage,
    _normalize,
    locations_compatible,
    missing_information,
    opportunities_match,
    roles_similar,
)


class StateDB:
    """Base de dados SQLite para estado do pipeline."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Config.state_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_emails (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    fingerprint TEXT
                );

                CREATE TABLE IF NOT EXISTS proposals (
                    fingerprint TEXT PRIMARY KEY,
                    notion_page_id TEXT,
                    company_client TEXT,
                    agency TEXT,
                    role_title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS company_cache (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    emails_processed INTEGER NOT NULL DEFAULT 0,
                    proposals_created INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS recruiters (
                    name TEXT PRIMARY KEY,
                    agency TEXT,
                    total INTEGER NOT NULL DEFAULT 0,
                    last_contact TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proposal_sources (
                    fingerprint TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source TEXT,
                    url TEXT,
                    received_at TEXT,
                    PRIMARY KEY (fingerprint, message_id)
                );

                CREATE TABLE IF NOT EXISTS recruiter_messages (
                    name TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    received_at TEXT,
                    PRIMARY KEY (name, message_id)
                );

                -- O que o pipeline decidiu e porquê, por mensagem. Sem isto não
                -- há forma de auditar a taxa de "ignore" nem comparar Jev vs LLM.
                CREATE TABLE IF NOT EXISTS assessments (
                    message_id TEXT PRIMARY KEY,
                    fingerprint TEXT,
                    created_at TEXT NOT NULL,
                    email_type TEXT,
                    source TEXT,
                    company TEXT,
                    agency TEXT,
                    role_title TEXT,
                    seniority TEXT,
                    remote_policy TEXT,
                    salary TEXT,
                    stack TEXT,
                    missing_count INTEGER,
                    classification_confidence REAL,
                    classification_reason TEXT,
                    extraction_confidence REAL,
                    opportunity_type TEXT,
                    fit_score INTEGER,
                    fit_confidence INTEGER,
                    action TEXT,
                    action_source TEXT,
                    jev_confidence REAL,
                    decision_trace TEXT
                );
                """
            )
            # Migração incremental de colunas (ignora se já existirem)
            for stmt in (
                "ALTER TABLE recruiters ADD COLUMN first_contact TEXT",
                "ALTER TABLE proposals ADD COLUMN location TEXT",
                "ALTER TABLE proposals ADD COLUMN contract_type TEXT",
                "ALTER TABLE proposals ADD COLUMN source TEXT",
                "ALTER TABLE proposals ADD COLUMN source_message_id TEXT",
                "ALTER TABLE proposals ADD COLUMN source_url TEXT",
                "ALTER TABLE proposals ADD COLUMN source_received_at TEXT",
                "ALTER TABLE proposals ADD COLUMN job_id TEXT",
                "ALTER TABLE proposals ADD COLUMN recruiter_name TEXT",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass

    def is_email_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_emails WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return row is not None

    def mark_email_processed(
        self, message_id: str, outcome: str, fingerprint: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_emails
                (message_id, processed_at, outcome, fingerprint)
                VALUES (?, ?, ?, ?)
                """,
                (
                    message_id,
                    datetime.now(timezone.utc).isoformat(),
                    outcome,
                    fingerprint,
                ),
            )

    def get_proposal_page_id(self, fingerprint: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notion_page_id FROM proposals WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            return row["notion_page_id"] if row else None

    def find_proposal(
        self,
        company_client: str | None,
        agency: str | None,
        role_title: str | None,
        location: str | None,
        contract_type: str | None,
        recruiter_name: str | None = None,
        job_id: str | None = None,
    ) -> tuple[str | None, str]:
        """Encontra uma proposta existente (id da vaga, fingerprint ou fuzzy).

        Devolve (notion_page_id, fingerprint_a_usar). Camadas, da mais para a
        menos fiável: id da vaga do LinkedIn; fingerprint exato; fuzzy por
        empresa (cliente OU agência) + role; fuzzy por recruiter + role quando
        a empresa é desconhecida. Num fuzzy match o fingerprint antigo migra
        para o novo formato.
        """
        company = company_client or agency or "unknown"
        fingerprint = "::".join(
            [
                _normalize(company),
                _normalize(role_title or "unknown"),
                _normalize(location or ""),
                _normalize(contract_type or ""),
            ]
        )

        # 1) Mesma vaga do LinkedIn (vem em job alerts e links de DM)
        if job_id:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT fingerprint, notion_page_id FROM proposals WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if row:
                return row["notion_page_id"], row["fingerprint"]

        # 2) Fingerprint exato
        page_id = self.get_proposal_page_id(fingerprint)
        if page_id:
            return page_id, fingerprint

        # Nunca fazer fuzzy match sem role: sem identidade mínima duas
        # propostas "sem dados" não são "a mesma vaga" (Jaccard de vazios).
        if not role_title:
            return None, fingerprint

        names = [n for n in (company_client, agency) if n]
        recruiter_key = _normalize(recruiter_name or "")

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint, notion_page_id, company_client, agency, "
                "role_title, location, contract_type, recruiter_name FROM proposals"
            ).fetchall()
            for row in rows:
                if not row["role_title"]:
                    continue
                other_names = [n for n in (row["company_client"], row["agency"]) if n]
                matched = False
                if names and other_names:
                    # Cliente num email e agência noutro é a mesma vaga
                    matched = any(
                        opportunities_match(
                            name, role_title, location, contract_type,
                            other, row["role_title"], row["location"], row["contract_type"],
                        )
                        for name in names
                        for other in other_names
                    )
                elif recruiter_key and recruiter_key == _normalize(row["recruiter_name"] or ""):
                    # Empresa desconhecida de um dos lados: o mesmo recruiter a
                    # enviar o mesmo role é a melhor evidência disponível.
                    matched = (
                        roles_similar(role_title, row["role_title"])
                        and locations_compatible(location, row["location"])
                        and (
                            not contract_type
                            or not row["contract_type"]
                            or _normalize(contract_type) == _normalize(row["contract_type"])
                        )
                    )
                if matched:
                    # proposal_sources usa o fingerprint na chave -- migrar na
                    # mesma transação ou a proveniência fica órfã.
                    if row["fingerprint"] != fingerprint:
                        conn.execute(
                            "UPDATE proposals SET fingerprint = ? WHERE fingerprint = ?",
                            (fingerprint, row["fingerprint"]),
                        )
                        conn.execute(
                            "UPDATE proposal_sources SET fingerprint = ? WHERE fingerprint = ?",
                            (fingerprint, row["fingerprint"]),
                        )
                        conn.execute(
                            "UPDATE assessments SET fingerprint = ? WHERE fingerprint = ?",
                            (fingerprint, row["fingerprint"]),
                        )
                    return row["notion_page_id"], fingerprint
        return None, fingerprint

    def upsert_proposal(
        self,
        fingerprint: str,
        notion_page_id: str,
        company_client: str | None,
        agency: str | None,
        role_title: str | None,
        location: str | None = None,
        contract_type: str | None = None,
        source: str | None = None,
        source_message_id: str | None = None,
        source_url: str | None = None,
        source_received_at: str | None = None,
        job_id: str | None = None,
        recruiter_name: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO proposals
                (fingerprint, notion_page_id, company_client, agency, role_title,
                 location, contract_type, source, source_message_id, source_url,
                 source_received_at, job_id, recruiter_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    notion_page_id = excluded.notion_page_id,
                    company_client = excluded.company_client,
                    agency = excluded.agency,
                    role_title = excluded.role_title,
                    location = COALESCE(excluded.location, proposals.location),
                    contract_type = COALESCE(excluded.contract_type, proposals.contract_type),
                    source = excluded.source,
                    source_message_id = excluded.source_message_id,
                    source_url = excluded.source_url,
                    source_received_at = excluded.source_received_at,
                    job_id = COALESCE(excluded.job_id, proposals.job_id),
                    recruiter_name = COALESCE(excluded.recruiter_name, proposals.recruiter_name),
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    notion_page_id,
                    company_client,
                    agency,
                    role_title,
                    location,
                    contract_type,
                    source,
                    source_message_id,
                    source_url,
                    source_received_at,
                    job_id,
                    recruiter_name,
                    now,
                    now,
                ),
            )
            if source_message_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO proposal_sources
                    (fingerprint, message_id, source, url, received_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        source_message_id,
                        source,
                        source_url,
                        source_received_at,
                    ),
                )

    def get_company_cache(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM company_cache WHERE name = ?",
                (name,),
            ).fetchone()
            return row["payload"] if row else None

    def set_company_cache(self, name: str, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO company_cache
                (name, payload, fetched_at)
                VALUES (?, ?, ?)
                """,
                (
                    name,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_recruiter(
        self,
        name: str,
        agency: str | None,
        received_at: str,
        message_id: str | None = None,
    ) -> None:
        """Regista/atualiza um recruiter. Cada mensagem conta apenas uma vez.

        As datas vêm do `received_at` da mensagem (não de quando o pipeline correu).
        """
        with self._connect() as conn:
            is_new_message = True
            if message_id:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO recruiter_messages (name, message_id, received_at) "
                    "VALUES (?, ?, ?)",
                    (name, message_id, received_at),
                )
                is_new_message = cur.rowcount == 1
            if not is_new_message:
                return
            conn.execute(
                """
                INSERT INTO recruiters (name, agency, total, first_contact, last_contact)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    total = total + 1,
                    agency = COALESCE(excluded.agency, recruiters.agency),
                    first_contact = CASE
                        WHEN recruiters.first_contact IS NULL
                             OR excluded.first_contact < recruiters.first_contact
                        THEN excluded.first_contact
                        ELSE recruiters.first_contact
                    END,
                    last_contact = CASE
                        WHEN recruiters.last_contact IS NULL
                             OR excluded.last_contact > recruiters.last_contact
                        THEN excluded.last_contact
                        ELSE recruiters.last_contact
                    END
                """,
                (name, agency, received_at, received_at),
            )

    def list_recruiters(self) -> list[dict]:
        """Devolve estatísticas de recruiters ordenadas por nº de contactos."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, agency, total, first_contact, last_contact FROM recruiters "
                "ORDER BY total DESC, last_contact DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def clear_proposals(self) -> None:
        """Remove o mapeamento fingerprint→notion_page_id (ex.: após apagar a database)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM proposals")

    def get_last_run_at(self) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_at FROM runs ORDER BY run_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return datetime.fromisoformat(row["run_at"])
        return None

    def record_run(
        self,
        emails_processed: int,
        proposals_created: int,
        errors: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_at, emails_processed, proposals_created, errors)
                VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    emails_processed,
                    proposals_created,
                    errors,
                ),
            )

    # --- avaliações (auditoria das decisões) ---------------------------------

    def record_assessment(
        self,
        message: RecruiterMessage,
        fingerprint: str,
        classification: Classification,
        extraction: ProposalExtraction,
        assessment: OpportunityAssessment | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assessments
                (message_id, fingerprint, created_at, email_type, source, company,
                 agency, role_title, seniority, remote_policy, salary, stack,
                 missing_count, classification_confidence, classification_reason,
                 extraction_confidence, opportunity_type, fit_score, fit_confidence,
                 action, action_source, jev_confidence, decision_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    fingerprint,
                    datetime.now(timezone.utc).isoformat(),
                    message.email_type,
                    message.source,
                    extraction.company_client,
                    extraction.agency,
                    extraction.role_title,
                    extraction.seniority,
                    extraction.remote_policy,
                    extraction.salary,
                    json.dumps(extraction.stack, ensure_ascii=False),
                    len(missing_information(extraction)),
                    classification.confidence,
                    classification.reason,
                    extraction.confidence,
                    assessment.opportunity_type if assessment else None,
                    assessment.fit_score if assessment else None,
                    assessment.fit_confidence if assessment else None,
                    assessment.recommended_action if assessment else None,
                    assessment.action_source if assessment else None,
                    assessment.jev_confidence if assessment else None,
                    assessment.decision_trace if assessment else None,
                ),
            )

    def list_assessments(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM assessments ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    # --- manutenção ----------------------------------------------------------

    def backup(self, dest: Path | None = None) -> Path:
        """Cópia consistente da DB (API de backup do SQLite) antes de operações destrutivas."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest or self.db_path.with_name(f"{self.db_path.name}.bak-{stamp}")
        src = sqlite3.connect(self.db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return dest

    def prune_backups(self, keep: int = 10) -> int:
        """Mantém só os `keep` backups mais recentes. Devolve quantos apagou."""
        backups = sorted(self.db_path.parent.glob(f"{self.db_path.name}.bak-*"))
        old = backups[:-keep] if keep > 0 else backups
        for path in old:
            path.unlink(missing_ok=True)
        return len(old)

    def get_fingerprint_by_page(self, page_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM proposals WHERE notion_page_id = ?", (page_id,)
            ).fetchone()
            return row["fingerprint"] if row else None

    def cleanup_invalid_state(self, user_name: str | None = None) -> dict[str, int]:
        """Remove lixo histórico: dry-runs gravados, alertas/o próprio utilizador como recruiter."""
        with self._connect() as conn:
            dry = conn.execute(
                "DELETE FROM processed_emails WHERE outcome = 'proposal_dry_run'"
            ).rowcount
            clauses = ["name LIKE 'Your job alert%'", "name LIKE '%job alert has been created%'"]
            params: list[str] = []
            if user_name:
                clauses.append("lower(name) = lower(?)")
                params.append(user_name)
            where = " OR ".join(clauses)
            conn.execute(
                f"DELETE FROM recruiter_messages WHERE name IN "
                f"(SELECT name FROM recruiters WHERE {where})",
                params,
            )
            rec = conn.execute(f"DELETE FROM recruiters WHERE {where}", params).rowcount
        return {"dry_run_emails": dry, "invalid_recruiters": rec}

    def list_proposals(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_proposal(self, fingerprint: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM proposals WHERE fingerprint = ?", (fingerprint,))

    def find_duplicate_groups(self) -> list[list[dict]]:
        """Grupos de propostas locais que parecem a mesma vaga (mais antiga primeiro)."""
        proposals = self.list_proposals()
        parent = list(range(len(proposals)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, a in enumerate(proposals):
            for j in range(i + 1, len(proposals)):
                b = proposals[j]
                if not a["role_title"] or not b["role_title"]:
                    continue
                if a["job_id"] and a["job_id"] == b["job_id"]:
                    same = True
                else:
                    an = [n for n in (a["company_client"], a["agency"]) if n]
                    bn = [n for n in (b["company_client"], b["agency"]) if n]
                    same = any(
                        opportunities_match(
                            x, a["role_title"], a["location"], a["contract_type"],
                            y, b["role_title"], b["location"], b["contract_type"],
                        )
                        for x in an
                        for y in bn
                    )
                    if not same and (not an or not bn):
                        ra, rb = _normalize(a["recruiter_name"] or ""), _normalize(b["recruiter_name"] or "")
                        same = bool(ra) and ra == rb and roles_similar(a["role_title"], b["role_title"])
                if same:
                    parent[find(j)] = find(i)

        groups: dict[int, list[dict]] = {}
        for i, p in enumerate(proposals):
            groups.setdefault(find(i), []).append(p)
        return [g for g in groups.values() if len(g) > 1]

    def merge_proposals(self, keep_fingerprint: str, drop_fingerprint: str) -> None:
        """Junta `drop` em `keep`: proveniência, emails e avaliações passam para `keep`."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE OR IGNORE proposal_sources SET fingerprint = ? WHERE fingerprint = ?",
                (keep_fingerprint, drop_fingerprint),
            )
            conn.execute("DELETE FROM proposal_sources WHERE fingerprint = ?", (drop_fingerprint,))
            conn.execute(
                "UPDATE processed_emails SET fingerprint = ? WHERE fingerprint = ?",
                (keep_fingerprint, drop_fingerprint),
            )
            conn.execute(
                "UPDATE assessments SET fingerprint = ? WHERE fingerprint = ?",
                (keep_fingerprint, drop_fingerprint),
            )
            conn.execute("DELETE FROM proposals WHERE fingerprint = ?", (drop_fingerprint,))
