"""Testes para os pickers de URLs do Enricher."""

from recruiter_pipeline.enricher import Enricher


class TestPickWebsite:
    def test_picks_official_site(self):
        results = [
            {"url": "https://pt.linkedin.com/company/feedzai"},
            {"url": "https://feedzai.com/about"},
        ]
        assert Enricher._pick_website("Feedzai", results) == "https://feedzai.com/about"

    def test_ignores_aggregators(self):
        results = [
            {"url": "https://pt.teamlyzer.com/companies/feedzai"},
            {"url": "https://www.glassdoor.com/feedzai"},
        ]
        assert Enricher._pick_website("Feedzai", results) is None

    def test_handles_hyphenated_slug(self):
        results = [{"url": "https://www.hn-services.com/careers"}]
        assert (
            Enricher._pick_website("HN Services", results)
            == "https://www.hn-services.com/careers"
        )

    def test_no_match_returns_none(self):
        results = [{"url": "https://random-blog.com/article"}]
        assert Enricher._pick_website("Feedzai", results) is None


class TestPickFirstUrlContaining:
    def test_linkedin_company(self):
        results = [
            {"url": "https://feedzai.com"},
            {"url": "https://pt.linkedin.com/company/feedzai"},
        ]
        assert (
            Enricher._pick_first_url_containing(results, "linkedin.com/company/")
            == "https://pt.linkedin.com/company/feedzai"
        )

    def test_teamlyzer(self):
        results = [{"url": "https://pt.teamlyzer.com/companies/feedzai"}]
        assert (
            Enricher._pick_first_url_containing(results, "teamlyzer.com")
            == "https://pt.teamlyzer.com/companies/feedzai"
        )

    def test_missing_returns_none(self):
        results = [{"url": "https://feedzai.com"}]
        assert Enricher._pick_first_url_containing(results, "teamlyzer.com") is None
