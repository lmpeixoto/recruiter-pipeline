"""Estado local em SQLite: dedupe, cache de empresas e emails processados."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from recruiter_pipeline.config import Config
from recruiter_pipeline.models import _normalize, opportunities_match


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
    ) -> tuple[str | None, str]:
        """Encontra uma proposta existente por fingerprint exato ou fuzzy match.

        Devolve (notion_page_id, fingerprint_a_usar). Se der fuzzy match numa
        proposta antiga, migra o fingerprint dela para o novo formato.
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
        page_id = self.get_proposal_page_id(fingerprint)
        if page_id:
            return page_id, fingerprint

        # Segunda camada: fuzzy match contra propostas existentes
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint, notion_page_id, company_client, agency, "
                "role_title, location, contract_type FROM proposals"
            ).fetchall()
            for row in rows:
                other_company = row["company_client"] or row["agency"] or "unknown"
                if opportunities_match(
                    company,
                    role_title or "unknown",
                    location,
                    contract_type,
                    other_company,
                    row["role_title"] or "unknown",
                    row["location"],
                    row["contract_type"],
                ):
                    # Migra o fingerprint da proposta antiga para o novo formato
                    conn.execute(
                        "UPDATE proposals SET fingerprint = ? WHERE fingerprint = ?",
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
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO proposals
                (fingerprint, notion_page_id, company_client, agency, role_title,
                 location, contract_type, source, source_message_id, source_url,
                 source_received_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
