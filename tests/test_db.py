"""Testes para a base de dados de estado."""

from pathlib import Path

from recruiter_pipeline.db import StateDB


def test_dedupe_and_proposal_tracking(tmp_path: Path) -> None:
    db = StateDB(db_path=tmp_path / "test.db")

    assert not db.is_email_processed("msg-1")
    db.mark_email_processed("msg-1", "proposal", fingerprint="acme::sre")
    assert db.is_email_processed("msg-1")

    assert db.get_proposal_page_id("acme::sre") is None
    db.upsert_proposal(
        fingerprint="acme::sre",
        notion_page_id="page-123",
        company_client="Acme",
        agency=None,
        role_title="SRE",
    )
    assert db.get_proposal_page_id("acme::sre") == "page-123"

    db.upsert_proposal(
        fingerprint="acme::sre",
        notion_page_id="page-456",
        company_client="Acme",
        agency=None,
        role_title="SRE",
    )
    assert db.get_proposal_page_id("acme::sre") == "page-456"


def test_company_cache(tmp_path: Path) -> None:
    db = StateDB(db_path=tmp_path / "test.db")
    assert db.get_company_cache("Acme") is None
    db.set_company_cache("Acme", '{"fetched_at": "2025-06-15T10:00:00+00:00"}')
    assert db.get_company_cache("Acme") is not None


class TestFindProposalIdentityGuards:
    """Regressão: duas propostas sem empresa/role não são "a mesma vaga"."""

    def test_no_fuzzy_match_when_company_unknown(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        # Localização diferente força fingerprint diferente -- cai na camada
        # fuzzy em vez do match exato, que é o caminho a testar.
        db.upsert_proposal(
            fingerprint="unknown::unknown::lisboa::",
            notion_page_id="page-1",
            company_client=None,
            agency=None,
            role_title=None,
            location="Lisboa",
        )
        # Outra proposta, empresa/role igualmente desconhecidos -- sem a
        # guarda, "unknown"::"unknown" faz Jaccard=1.0 contra a primeira.
        page_id, fingerprint = db.find_proposal(
            company_client=None,
            agency=None,
            role_title=None,
            location=None,
            contract_type=None,
        )
        assert page_id is None
        assert fingerprint != "unknown::unknown::lisboa::"

    def test_no_fuzzy_match_when_role_missing(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="acme::unknown::lisboa::",
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title=None,
            location="Lisboa",
        )
        page_id, _ = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title=None,
            location=None,
            contract_type=None,
        )
        assert page_id is None

    def test_exact_fingerprint_still_works_with_unknowns(self, tmp_path: Path) -> None:
        """A guarda só bloqueia a camada fuzzy -- o match exato continua."""
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="unknown::unknown::::",
            notion_page_id="page-1",
            company_client=None,
            agency=None,
            role_title=None,
        )
        db.get_proposal_page_id("unknown::unknown::::")
        assert db.get_proposal_page_id("unknown::unknown::::") == "page-1"

    def test_fuzzy_match_still_works_with_real_identity(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        db.upsert_proposal(
            fingerprint="acme::site reliability engineer::::",
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title="Site Reliability Engineer",
        )
        page_id, fingerprint = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="Senior Site Reliability Engineer",
            location=None,
            contract_type=None,
        )
        assert page_id == "page-1"
        assert fingerprint != "acme::site reliability engineer::::"


class TestFingerprintMigrationCascades:
    """Regressão: migrar o fingerprint de uma proposta não pode orfanizar
    as fontes associadas em proposal_sources."""

    def test_proposal_sources_migrates_with_fingerprint(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "test.db")
        old_fingerprint = "acme::site reliability engineer::::"
        db.upsert_proposal(
            fingerprint=old_fingerprint,
            notion_page_id="page-1",
            company_client="Acme",
            agency=None,
            role_title="Site Reliability Engineer",
            source="LinkedIn DM",
            source_message_id="msg-1",
            source_url="https://example.com/msg-1",
            source_received_at="2026-01-01T00:00:00+00:00",
        )

        page_id, new_fingerprint = db.find_proposal(
            company_client="Acme",
            agency=None,
            role_title="Senior Site Reliability Engineer",
            location=None,
            contract_type=None,
        )
        assert page_id == "page-1"
        assert new_fingerprint != old_fingerprint

        with db._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint, message_id FROM proposal_sources"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == new_fingerprint
        assert rows[0]["message_id"] == "msg-1"


class TestDedupeLayers:
    def _seed(self, db, **kw):
        base = dict(
            fingerprint="vestas::platform engineering specialist::leca do balio::",
            notion_page_id="page-1",
            company_client="Vestas",
            agency=None,
            role_title="Platform Engineering Specialist",
            location="Leça do Balio, Grande Porto, PT",
        )
        base.update(kw)
        db.upsert_proposal(**base)

    def test_location_typo_matches_same_job(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        self._seed(db)
        page_id, fp = db.find_proposal(
            "Vestas", None, "Platform Engineering Specialist", "Leça do Bailio", None
        )
        assert page_id == "page-1"

    def test_job_id_wins_over_everything(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        self._seed(db, job_id="12345")
        page_id, fp = db.find_proposal(
            "Other Co", None, "Totally different", "Lisboa", None, job_id="12345"
        )
        assert page_id == "page-1"
        assert fp == "vestas::platform engineering specialist::leca do balio::"

    def test_agency_in_one_email_client_in_another(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        self._seed(db, company_client=None, agency="Jobgether",
                   role_title="Senior Platform Engineer", location="Portugal",
                   fingerprint="jobgether::senior platform engineer::portugal::")
        page_id, _ = db.find_proposal(
            "Scopely", "Jobgether", "Senior Platform Engineer (Reliability)", "Portugal", None
        )
        assert page_id == "page-1"

    def test_same_recruiter_and_role_without_company(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        self._seed(db, company_client=None, agency=None, role_title="Site Reliability Engineer",
                   location=None, fingerprint="unknown::site reliability engineer::::",
                   recruiter_name="Ana Silva")
        page_id, _ = db.find_proposal(
            None, None, "Site Reliability Engineer", None, "contract", recruiter_name="Ana Silva"
        )
        assert page_id == "page-1"
        # outro recruiter, mesmo role, sem empresa: não é evidência
        # (DB nova: o match acima migra o fingerprint e daria match exato)
        db = StateDB(db_path=tmp_path / "t2.db")
        self._seed(db, company_client=None, agency=None, role_title="Site Reliability Engineer",
                   location=None, fingerprint="unknown::site reliability engineer::::",
                   recruiter_name="Ana Silva")
        page_id, _ = db.find_proposal(
            None, None, "Site Reliability Engineer", None, "contract", recruiter_name="Rui Costa"
        )
        assert page_id is None

    def test_different_city_is_a_different_job(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        self._seed(db, location="Lisboa")
        page_id, _ = db.find_proposal(
            "Vestas", None, "Platform Engineering Specialist", "Porto", None
        )
        assert page_id is None


class TestMaintenance:
    def test_cleanup_removes_dry_runs_and_fake_recruiters(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        db.mark_email_processed("a", "proposal_dry_run")
        db.mark_email_processed("b", "proposal")
        db.record_recruiter("Your job alert for X in Porto", None, "2026-01-01", "m1")
        db.record_recruiter("Luís Peixoto", None, "2026-01-01", "m2")
        db.record_recruiter("Ana Silva", "Acme", "2026-01-01", "m3")
        result = db.cleanup_invalid_state(user_name="Luís Peixoto")
        assert result == {"dry_run_emails": 1, "invalid_recruiters": 2}
        assert [r["name"] for r in db.list_recruiters()] == ["Ana Silva"]
        assert db.is_email_processed("b") and not db.is_email_processed("a")

    def test_backup_is_a_readable_copy(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        db.mark_email_processed("a", "proposal")
        copy = db.backup()
        assert copy.exists() and copy != db.db_path
        assert StateDB(db_path=copy).is_email_processed("a")

    def test_duplicate_groups_and_merge(self, tmp_path: Path) -> None:
        db = StateDB(db_path=tmp_path / "t.db")
        for i, loc in enumerate(["Leça do Balio", "Leça do Bailio"]):
            db.upsert_proposal(
                fingerprint=f"fp{i}", notion_page_id=f"p{i}", company_client="Vestas",
                agency=None, role_title="Platform Engineering Specialist", location=loc,
                source_message_id=f"m{i}", source="LinkedIn Job Alert",
            )
        groups = db.find_duplicate_groups()
        assert len(groups) == 1 and len(groups[0]) == 2
        keep, drop = groups[0][0]["fingerprint"], groups[0][1]["fingerprint"]
        db.merge_proposals(keep, drop)
        assert [p["fingerprint"] for p in db.list_proposals()] == [keep]
        with db._connect() as conn:
            n = conn.execute("SELECT count(*) FROM proposal_sources WHERE fingerprint=?", (keep,)).fetchone()[0]
        assert n == 2
