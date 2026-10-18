"""Guardrails determinísticos e comparação tolerante de localizações."""

from recruiter_pipeline.decision import Rules, apply_guardrails, parse_rules, triage
from recruiter_pipeline.models import (
    OpportunityAssessment,
    ProposalExtraction,
    locations_compatible,
    opportunities_match,
)


def _extraction(**kw) -> ProposalExtraction:
    base = dict(company_client="Acme", role_title="SRE", summary="s", confidence=0.9)
    base.update(kw)
    return ProposalExtraction(**base)


class TestIgnoreNeedsEvidence:
    def test_ignore_with_little_info_is_downgraded(self):
        a = OpportunityAssessment(recommended_action="ignore", fit_score=None, fit_confidence=20)
        apply_guardrails(a, _extraction(company_client=None))
        assert a.recommended_action == "ask_for_more_information"
        assert a.action_source == "guardrail"
        assert "ignore → ask_for_more_information" in a.decision_trace
        assert "Informação insuficiente" in a.action_reason

    def test_ignore_with_low_fit_and_high_confidence_survives(self):
        a = OpportunityAssessment(
            recommended_action="ignore", fit_score=20, fit_confidence=85, action_reason="onsite"
        )
        apply_guardrails(a, _extraction(stack=["Go"], salary="x", seniority="senior",
                                        remote_policy="onsite", location="Lisboa",
                                        contract_type="permanent"))
        assert a.recommended_action == "ignore"
        assert a.decision_trace == ""

    def test_ignore_with_known_company_and_few_gaps_becomes_monitor(self):
        a = OpportunityAssessment(recommended_action="ignore", fit_score=55, fit_confidence=70)
        apply_guardrails(a, _extraction(stack=["Go"], salary="x", seniority="senior",
                                        remote_policy="remote", location="Porto"))
        assert a.recommended_action == "monitor"


class TestReplyNeedsIdentity:
    def test_reply_without_company_and_role_asks_for_info(self):
        a = OpportunityAssessment(recommended_action="reply", fit_score=80, fit_confidence=70)
        apply_guardrails(a, _extraction(company_client=None, role_title=None))
        assert a.recommended_action == "ask_for_more_information"

    def test_reply_with_identity_untouched(self):
        a = OpportunityAssessment(recommended_action="reply", fit_score=80, fit_confidence=70)
        apply_guardrails(a, _extraction())
        assert a.recommended_action == "reply"


class TestProfileRules:
    PROFILE = """# Perfil
## Regras
- avoid_stack: .NET, Java
- avoid_agencies: Bodyshop SA
## Outras
- avoid_stack: Cobol
"""

    def test_parse_only_regras_section(self):
        rules = parse_rules(self.PROFILE)
        assert rules.avoid_stack == [".NET", "Java"]
        assert rules.avoid_agencies == ["Bodyshop SA"]

    def test_no_section_means_no_rules(self):
        assert parse_rules("# Perfil\n- Roles alvo: SRE").empty
        assert parse_rules(None).empty

    def test_stack_dealbreaker_forces_ignore_with_explicit_reason(self):
        a = OpportunityAssessment(recommended_action="reply", fit_score=70, fit_confidence=70)
        apply_guardrails(a, _extraction(stack=["Java", "AWS"]), parse_rules(self.PROFILE))
        assert a.recommended_action == "ignore"
        assert a.action_source == "rule"
        assert "Java" in a.action_reason

    def test_agency_dealbreaker(self):
        a = OpportunityAssessment(recommended_action="monitor")
        apply_guardrails(a, _extraction(agency="Bodyshop SA"), parse_rules(self.PROFILE))
        assert a.recommended_action == "ignore"


def test_triage_labels():
    assert triage("ignore").startswith("🔴")
    assert triage("apply") == triage("reply")


class TestLocations:
    def test_same_city_with_country_suffix(self):
        assert locations_compatible("Porto", "Porto, Portugal")

    def test_typo_variants(self):
        assert locations_compatible("Leça do Balio, Grande Porto, PT", "Leça do Bailio")

    def test_generic_country_is_compatible_with_any_city(self):
        assert locations_compatible("Portugal", "Lisboa")
        assert locations_compatible("PT", "Porto")

    def test_different_cities_conflict(self):
        assert not locations_compatible("Lisboa", "Porto")

    def test_portugal_vs_foreign_country_conflicts(self):
        assert not locations_compatible("Portugal", "United Kingdom")
        assert not locations_compatible("Porto", "London")
        assert locations_compatible("London", "London, UK")

    def test_unknown_is_compatible(self):
        assert locations_compatible(None, "Porto")

    def test_opportunities_match_uses_tolerant_locations(self):
        assert opportunities_match(
            "Vestas", "Platform Engineering Specialist", "Leça do Balio, Grande Porto, PT", None,
            "Vestas", "Platform Engineering Specialist", "Leça do Bailio", None,
        )
