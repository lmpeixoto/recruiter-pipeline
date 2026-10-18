"""Enriquecimento de informação sobre empresas via web search."""

import json
from datetime import datetime, timedelta, timezone

import httpx

from recruiter_pipeline.config import Config
from recruiter_pipeline.db import StateDB
from recruiter_pipeline.models import CompanyEnrichment

TAVILY_URL = "https://api.tavily.com/search"
CACHE_TTL_DAYS = 30

# Domínios que não contam como "website oficial" da empresa
NON_WEBSITE_DOMAINS = (
    "linkedin.com",
    "teamlyzer.com",
    "glassdoor.",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "landing.jobs",
    "indeed.",
    "wikipedia.org",
)


class Enricher:
    """Enriquece dados de uma empresa usando pesquisa web (Tavily)."""

    def __init__(
        self,
        api_key: str | None = None,
        db: StateDB | None = None,
    ) -> None:
        self.api_key = api_key or Config.tavily_api_key
        self.db = db or StateDB()
        self.client = httpx.Client(timeout=30.0)
        self._disabled = False  # True após falha de autenticação na Tavily

    @property
    def available(self) -> bool:
        """True se a pesquisa web está configurada e operacional."""
        return bool(self.api_key) and not self._disabled

    def enrich(
        self,
        company_name: str,
        website_hint: str | None = None,
        linkedin_hint: str | None = None,
        force_refresh: bool = False,
    ) -> CompanyEnrichment | None:
        """Devolve informação enriquecida sobre a empresa, com cache."""
        cached = None if force_refresh else self._get_cached(company_name)
        if cached:
            # Mesmo com cache, garante que os hints são aproveitados
            changed = False
            if website_hint and not cached.website:
                cached.website = website_hint
                changed = True
            if linkedin_hint and not cached.linkedin_url:
                cached.linkedin_url = linkedin_hint
                changed = True
            if changed:
                self._set_cached(company_name, cached)
            return cached

        if not self.api_key or self._disabled:
            # Sem pesquisa web, devolve apenas o que sabemos dos hints
            if website_hint or linkedin_hint:
                return CompanyEnrichment(
                    name=company_name,
                    website=website_hint,
                    linkedin_url=linkedin_hint,
                )
            return None

        search_results = self._search(company_name)
        if self._disabled:
            if website_hint or linkedin_hint:
                return CompanyEnrichment(
                    name=company_name,
                    website=website_hint,
                    linkedin_url=linkedin_hint,
                )
            return None

        if not search_results and not (website_hint or linkedin_hint):
            return None

        enrichment = self._synthesize(
            company_name,
            search_results,
            website_hint=website_hint,
            linkedin_hint=linkedin_hint,
        )
        self._set_cached(company_name, enrichment)
        return enrichment

    def _search(self, company_name: str) -> list[dict]:
        """Faz várias pesquisas sobre a empresa."""
        queries = [
            f"{company_name} official website about",
            f"{company_name} company size industry headquarters",
            f"{company_name} engineering tech stack careers",
            f"{company_name} linkedin company page",
            f"{company_name} teamlyzer reviews salários",
        ]
        results: list[dict] = []
        for query in queries:
            if self._disabled:
                break
            try:
                response = self.client.post(
                    TAVILY_URL,
                    json={
                        "api_key": self.api_key,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 5,
                    },
                )
                if response.status_code == 401:
                    self._disabled = True
                    print(
                        "Aviso: chave Tavily inválida ou em falta. "
                        "Enriquecimento web desativado para esta execução. "
                        "Obtém uma chave em https://tavily.com/ e define TAVILY_API_KEY no .env."
                    )
                    break
                response.raise_for_status()
                data = response.json()
                results.extend(data.get("results", []))
            except httpx.HTTPError as exc:
                print(f"Aviso: falha na pesquisa Tavily para '{query}': {exc}")
        return results

    def _synthesize(
        self,
        company_name: str,
        search_results: list[dict],
        website_hint: str | None = None,
        linkedin_hint: str | None = None,
    ) -> CompanyEnrichment:
        """Sintetiza os resultados de pesquisa num CompanyEnrichment."""
        # Extração determinística de URLs a partir dos resultados
        website = website_hint or self._pick_website(company_name, search_results)
        linkedin_url = linkedin_hint or self._pick_first_url_containing(
            search_results, "linkedin.com/company/"
        )
        teamlyzer_url = self._pick_first_url_containing(
            search_results, "teamlyzer.com"
        )

        if not search_results:
            return CompanyEnrichment(
                name=company_name,
                website=website,
                linkedin_url=linkedin_url,
                teamlyzer_url=teamlyzer_url,
            )

        # Junta títulos e snippets para o LLM sintetizar
        context = "\n\n".join(
            f"Título: {r.get('title', '')}\nURL: {r.get('url', '')}\nResumo: {r.get('content', '')}"
            for r in search_results[:15]
        )

        prompt = f"""És um assistente de pesquisa empresarial. Com base nos resultados de pesquisa abaixo, extrai informação estruturada sobre a empresa "{company_name}".

Devolve APENAS um objeto JSON com este schema:
{{
  "name": "string",
  "website": "string | null",
  "linkedin_url": "string | null",
  "teamlyzer_url": "string | null",
  "industry": "string | null",
  "size": "string | null",
  "headquarters": "string | null",
  "description": "string | null",
  "tech_stack": ["string"],
  "recent_news": ["string"],
  "notes": "string | null"
}}

Regras:
- Não inventes informação que não esteja nos resultados.
- "tech_stack" deve conter tecnologias mencionadas (ex.: AWS, Kubernetes, Python, React).
- "recent_news" são 1-3 bullets sobre notícias ou sinais recentes de hiring/crescimento.
- "description" é um parágrafo curto sobre o que a empresa faz.
- "linkedin_url" deve ser um URL linkedin.com/company/... se existir nos resultados.
- "teamlyzer_url" deve ser um URL teamlyzer.com da empresa se existir nos resultados.
- Se encontrares informação do Teamlyzer (rating, nº de reviews, salários), inclui um resumo em "notes".

Resultados de pesquisa:
{context}
"""
        from recruiter_pipeline.llm import LLMClient

        llm = LLMClient()
        synthesized = llm.complete(
            system_prompt="És um analista de informação empresarial. Responde apenas com JSON válido.",
            user_prompt=prompt,
            response_model=CompanyEnrichment,
        )

        # Os valores determinísticos têm prioridade sobre os do LLM
        synthesized.website = website or synthesized.website
        synthesized.linkedin_url = linkedin_url or synthesized.linkedin_url
        synthesized.teamlyzer_url = teamlyzer_url or synthesized.teamlyzer_url
        return synthesized

    @staticmethod
    def _pick_website(company_name: str, results: list[dict]) -> str | None:
        """Escolhe o website oficial a partir dos resultados."""
        slug = company_name.lower().replace(" ", "")
        for result in results:
            url = (result.get("url") or "").lower()
            if not url:
                continue
            if any(domain in url for domain in NON_WEBSITE_DOMAINS):
                continue
            if slug and slug in url.replace("-", "").replace(".", ""):
                return result["url"]
        return None

    @staticmethod
    def _pick_first_url_containing(results: list[dict], needle: str) -> str | None:
        for result in results:
            url = result.get("url") or ""
            if needle in url:
                return url
        return None

    def _get_cached(self, company_name: str) -> CompanyEnrichment | None:
        payload = self.db.get_company_cache(company_name)
        if not payload:
            return None
        try:
            data = json.loads(payload)
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            if datetime.now(timezone.utc) - fetched_at > timedelta(days=CACHE_TTL_DAYS):
                return None
            return CompanyEnrichment.model_validate(data["enrichment"])
        except Exception:
            return None

    def _set_cached(self, company_name: str, enrichment: CompanyEnrichment) -> None:
        payload = json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "enrichment": enrichment.model_dump(),
            },
            ensure_ascii=False,
        )
        self.db.set_company_cache(company_name, payload)
