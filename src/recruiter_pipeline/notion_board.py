"""Integração com Notion: criação de database e upsert de cards."""

import time

import httpx
from notion_client import Client

from recruiter_pipeline.config import Config
from recruiter_pipeline.db import StateDB
from recruiter_pipeline.models import (
    CompanyEnrichment,
    OpportunityAssessment,
    ProposalExtraction,
    RecruiterMessage,
    missing_information,
)

DATABASE_TITLE = "Propostas"
TITLE_PROPERTY = "Name"  # O Notion exige que a propriedade title se chame "Name"

# Cores do Notion para estados (funil de Career CRM)
STATE_COLORS = {
    "Nova": "gray",
    "A avaliar": "yellow",
    "Respondida": "blue",
    "Recruiter screen": "purple",
    "Entrevista": "pink",
    "Proposta": "orange",
    "Aceite": "green",
    "Rejeitada": "red",
    "Arquivada": "brown",
}

CONFIDENCE_COLORS = {
    "Alta": "green",
    "Média": "yellow",
    "Baixa": "red",
}

OPPORTUNITY_TYPES = {
    "job_opportunity": "Job Opportunity",
    "recruiter_outreach": "Recruiter Outreach",
    "connection": "Connection",
    "referral": "Referral",
    "generic": "Generic",
    "unknown": "Unknown",
}

RECOMMENDED_ACTIONS = {
    "reply": "Responder",
    "ask_for_more_information": "Pedir mais info",
    "apply": "Candidatar",
    "ignore": "Ignorar",
    "monitor": "Monitorizar",
    "connect": "Conectar",
}


class NotionBoard:
    """Board de propostas no Notion."""

    def __init__(
        self,
        token: str | None = None,
        parent_page_id: str | None = None,
        db: StateDB | None = None,
    ) -> None:
        self.token = token or Config.notion_token
        self.parent_page_id = parent_page_id or Config.notion_parent_page_id
        self.client = Client(auth=self.token)
        self.db = db or StateDB()
        self.database_id: str | None = None

    def find_existing_database(self) -> str | None:
        """Procura uma database 'Propostas' na página pai."""
        children = self.client.blocks.children.list(block_id=self.parent_page_id)
        for child in children.get("results", []):
            if child.get("type") == "child_database":
                if child.get("child_database", {}).get("title") == DATABASE_TITLE:
                    return child["id"]
        return None

    def _notion_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _properties_schema() -> dict:
        """Schema único das propriedades da database (criação e migração)."""
        return {
            TITLE_PROPERTY: {"title": {}},
            "Empresa": {"rich_text": {}},
            "Agencia": {"rich_text": {}},
            "Recruiter": {"rich_text": {}},
            "Role": {"rich_text": {}},
            "Stack": {"multi_select": {"options": []}},
            "Localizacao": {"rich_text": {}},
            "Remote": {"select": {"options": []}},
            "Salario": {"rich_text": {}},
            "Contrato": {"select": {"options": []}},
            "Estado": {
                "select": {
                    "options": [
                        {"name": name, "color": color}
                        for name, color in STATE_COLORS.items()
                    ]
                }
            },
            "Tipo Oportunidade": {
                "select": {
                    "options": [{"name": name} for name in OPPORTUNITY_TYPES.values()]
                }
            },
            "Fit": {"number": {"format": "number"}},
            "Fit Confianca": {"number": {"format": "number"}},
            "Acao": {
                "select": {
                    "options": [{"name": name} for name in RECOMMENDED_ACTIONS.values()]
                }
            },
            "Confianca": {
                "select": {
                    "options": [
                        {"name": name, "color": color}
                        for name, color in CONFIDENCE_COLORS.items()
                    ]
                }
            },
            "Website": {"url": {}},
            "LinkedIn": {"url": {}},
            "Teamlyzer": {"url": {}},
            "Fonte": {
                "select": {
                    "options": [
                        {"name": "LinkedIn DM"},
                        {"name": "LinkedIn Job Alert"},
                        {"name": "LinkedIn Email"},
                        {"name": "Recruiter Email"},
                        {"name": "Manual"},
                    ]
                }
            },
            "Setor": {"select": {"options": []}},
            "Consultora": {"checkbox": {}},
            "Feedback": {"rich_text": {}},
            "Recebida": {"date": {}},
            "Link": {"url": {}},
        }

    def _create_database_payload(self) -> dict:
        return {
            "parent": {"type": "page_id", "page_id": self.parent_page_id},
            "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
            "properties": self._properties_schema(),
        }

    def ensure_database(self) -> str:
        """Garante que a database existe com o schema correto e devolve o ID."""
        if self.database_id:
            return self.database_id

        db_id = self.find_existing_database()
        if db_id:
            self._validate_database_schema(db_id, retries=3)
            self.database_id = db_id
            return self.database_id

        # Cria nova database via API REST direta para ter controlo total do payload
        response = httpx.post(
            "https://api.notion.com/v1/databases",
            headers=self._notion_headers(),
            json=self._create_database_payload(),
            timeout=30.0,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Falha ao criar database no Notion: {response.status_code} - {response.text}"
            )
        data = response.json()
        self.database_id = data["id"]
        return self.database_id

    def upsert_proposal(
        self,
        fingerprint: str,
        extraction: ProposalExtraction,
        message: RecruiterMessage,
        enrichment: CompanyEnrichment | None,
        assessment: OpportunityAssessment | None = None,
    ) -> str:
        """Cria ou atualiza um card de proposta. Devolve o page ID."""
        database_id = self.ensure_database()
        # Dedupe: fingerprint exato, com fuzzy match como segunda camada.
        # find_proposal devolve o fingerprint efetivo (pode migrar um antigo).
        existing_page_id, fingerprint = self.db.find_proposal(
            company_client=extraction.company_client,
            agency=extraction.agency,
            role_title=extraction.role_title,
            location=extraction.location,
            contract_type=extraction.contract_type,
        )

        properties = self._build_properties(extraction, message, enrichment, assessment)
        page_content = self._build_page_content(
            message, extraction, enrichment, assessment
        )

        if existing_page_id:
            try:
                self.client.pages.update(
                    page_id=existing_page_id,
                    properties=properties,
                )
                self._replace_page_content(existing_page_id, page_content)
                page_id = existing_page_id
                # Regista este avistamento na proveniência, mesmo em update
                self.db.upsert_proposal(
                    fingerprint=fingerprint,
                    notion_page_id=page_id,
                    company_client=extraction.company_client,
                    agency=extraction.agency,
                    role_title=extraction.role_title,
                    location=extraction.location,
                    contract_type=extraction.contract_type,
                    source=message.source,
                    source_message_id=message.message_id,
                    source_url=message.permalink,
                    source_received_at=message.received_at.isoformat(),
                )
            except Exception:
                # A página mapeada pode ter sido arquivada/apagada (ex.: reset da database)
                existing_page_id = None

        if not existing_page_id:
            # "Estado" só é definido na criação -- um update nunca o toca, para
            # não repor "Nova" por cima de um estágio que o humano já avançou
            # manualmente no Notion (bug real: todo upsert definia sempre
            # "Nova", incluindo updates de propostas já em "Entrevista" etc.).
            properties["Estado"] = {"select": {"name": "Nova"}}
            page = self.client.pages.create(
                parent={"database_id": database_id},
                properties=properties,
                children=page_content,
            )
            page_id = page["id"]
            self.db.upsert_proposal(
                fingerprint=fingerprint,
                notion_page_id=page_id,
                company_client=extraction.company_client,
                agency=extraction.agency,
                role_title=extraction.role_title,
                location=extraction.location,
                contract_type=extraction.contract_type,
                source=message.source,
                source_message_id=message.message_id,
                source_url=message.permalink,
                source_received_at=message.received_at.isoformat(),
            )

        return page_id

    def _validate_database_schema(self, db_id: str, retries: int = 1) -> None:
        """Valida que a database tem as propriedades esperadas com os tipos certos."""
        last_error = ""
        schema = self._properties_schema()
        expected = {
            name: next(iter(defn.keys())) for name, defn in schema.items()
        }
        for attempt in range(retries):
            response = httpx.get(
                f"https://api.notion.com/v1/databases/{db_id}",
                headers=self._notion_headers(),
                timeout=30.0,
            )
            response.raise_for_status()
            props = response.json().get("properties", {})
            missing = {
                name: schema[name]
                for name, expected_type in expected.items()
                if name not in props
            }
            type_mismatches = [
                f"{name}: esperado {expected_type}, encontrado {props.get(name, {}).get('type')}"
                for name, expected_type in expected.items()
                if name in props and props[name].get("type") != expected_type
            ]

            # Auto-migração: adiciona propriedades em falta sem apagar dados
            if missing:
                patch = httpx.patch(
                    f"https://api.notion.com/v1/databases/{db_id}",
                    headers=self._notion_headers(),
                    json={"properties": missing},
                    timeout=30.0,
                )
                patch.raise_for_status()

            if not type_mismatches:
                return
            last_error = ", ".join(type_mismatches)
            if attempt < retries - 1:
                time.sleep(1.0)

        raise RuntimeError(
            f"A database '{DATABASE_TITLE}' existe mas tem propriedades com tipos incorretos. "
            f"Diferenças: {last_error}. "
            "Corre 'uv run -m recruiter_pipeline.main reset-notion' para a recriar, "
            "ou corrige manualmente no Notion."
        )

    def delete_database(self) -> bool:
        """Apaga a database 'Propostas' existente na página pai."""
        db_id = self.find_existing_database()
        if not db_id:
            return False
        self.client.blocks.delete(block_id=db_id)
        self.database_id = None
        return True

    def list_cards(self) -> list[dict]:
        """Lista todos os cards da database com empresa, links e estado de enriquecimento."""
        database_id = self.ensure_database()
        cards: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            response = httpx.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=self._notion_headers(),
                json=body,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            for page in data.get("results", []):
                cards.append(self._parse_card(page))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return cards

    @staticmethod
    def _parse_card(page: dict) -> dict:
        props = page.get("properties", {})

        def rich_text(name: str) -> str:
            parts = props.get(name, {}).get("rich_text", [])
            return parts[0].get("plain_text", "") if parts else ""

        def title() -> str:
            parts = props.get(TITLE_PROPERTY, {}).get("title", [])
            return parts[0].get("plain_text", "") if parts else ""

        def url(name: str) -> str | None:
            return props.get(name, {}).get("url") or None

        company = rich_text("Empresa") or title()
        return {
            "page_id": page["id"],
            "company": company,
            "role": rich_text("Role") or None,
            "agency": rich_text("Agencia") or None,
            "website": url("Website"),
            "linkedin": url("LinkedIn"),
            "teamlyzer": url("Teamlyzer"),
        }

    def update_card_links(
        self,
        page_id: str,
        website: str | None,
        linkedin: str | None,
        teamlyzer: str | None,
    ) -> None:
        """Atualiza apenas as propriedades de links do card."""
        properties: dict = {}
        if website:
            properties["Website"] = {"url": website}
        if linkedin:
            properties["LinkedIn"] = {"url": linkedin}
        if teamlyzer:
            properties["Teamlyzer"] = {"url": teamlyzer}
        if properties:
            self.client.pages.update(page_id=page_id, properties=properties)

    def has_enrichment_section(self, page_id: str) -> bool:
        """True se o card já tem uma secção 'Sobre a empresa'."""
        blocks = self.client.blocks.children.list(block_id=page_id)
        for block in blocks.get("results", []):
            if block.get("type") == "heading_2":
                texts = block.get("heading_2", {}).get("rich_text", [])
                content = "".join(t.get("plain_text", "") for t in texts)
                if "Sobre a empresa" in content:
                    return True
        return False

    def append_enrichment_section(
        self, page_id: str, enrichment: CompanyEnrichment
    ) -> None:
        """Adiciona a secção 'Sobre a empresa' no fim do card."""
        self.client.blocks.children.append(
            block_id=page_id, children=self._enrichment_blocks(enrichment)
        )

    def _build_properties(
        self,
        extraction: ProposalExtraction,
        message: RecruiterMessage,
        enrichment: CompanyEnrichment | None,
        assessment: OpportunityAssessment | None = None,
    ) -> dict:
        empresa = extraction.company_client or extraction.agency or "Confidencial"
        confianca = self._confidence_label(extraction.confidence)

        # Website / LinkedIn / Teamlyzer: enriquecimento tem prioridade, depois extração
        website = (enrichment.website if enrichment else None) or extraction.company_website
        linkedin = (
            (enrichment.linkedin_url if enrichment else None)
            or extraction.company_linkedin
            or message.company_linkedin_guess
        )
        teamlyzer = enrichment.teamlyzer_url if enrichment else None

        properties: dict = {
            TITLE_PROPERTY: {"title": [{"text": {"content": empresa}}]},
            "Empresa": {"rich_text": [{"text": {"content": empresa}}]},
            "Agencia": {"rich_text": [{"text": {"content": extraction.agency or ""}}]},
            "Recruiter": {
                "rich_text": [
                    {"text": {"content": extraction.recruiter_name or ""}}
                ]
            },
            "Role": {"rich_text": [{"text": {"content": extraction.role_title or ""}}]},
            "Stack": {
                "multi_select": [
                    {"name": self._sanitize_select_option(tech)}
                    for tech in (extraction.stack or [])
                    if self._sanitize_select_option(tech)
                ]
            },
            "Localizacao": {
                "rich_text": [{"text": {"content": extraction.location or ""}}]
            },
            "Remote": {"select": {"name": extraction.remote_policy or ""}},
            "Salario": {"rich_text": [{"text": {"content": extraction.salary or ""}}]},
            "Contrato": {"select": {"name": extraction.contract_type or ""}},
            "Confianca": {"select": {"name": confianca}},
            "Fonte": {"select": {"name": message.source}},
            "Recebida": {"date": {"start": message.received_at.date().isoformat()}},
            "Link": {"url": message.permalink},
        }

        # Setor (do enrichment) e flag Consultora
        if enrichment and enrichment.industry:
            setor = self._sanitize_select_option(enrichment.industry)
            if setor:
                properties["Setor"] = {"select": {"name": setor}}
        properties["Consultora"] = {
            "checkbox": self._is_consultancy(extraction, enrichment)
        }

        if website:
            properties["Website"] = {"url": website}
        if linkedin:
            properties["LinkedIn"] = {"url": linkedin}
        if teamlyzer:
            properties["Teamlyzer"] = {"url": teamlyzer}

        if assessment:
            if assessment.fit_score is not None:
                properties["Fit"] = {"number": assessment.fit_score}
            if assessment.fit_confidence:
                properties["Fit Confianca"] = {"number": assessment.fit_confidence}
            opp_type = OPPORTUNITY_TYPES.get(assessment.opportunity_type)
            if opp_type:
                properties["Tipo Oportunidade"] = {"select": {"name": opp_type}}
            action = RECOMMENDED_ACTIONS.get(assessment.recommended_action)
            if action:
                properties["Acao"] = {"select": {"name": action}}

        # Remove propriedades vazias de select (Notion rejeita string vazia)
        for key in ["Remote", "Contrato"]:
            if not properties[key]["select"]["name"]:
                del properties[key]

        return properties

    def _build_page_content(
        self,
        message: RecruiterMessage,
        extraction: ProposalExtraction,
        enrichment: CompanyEnrichment | None,
        assessment: OpportunityAssessment | None = None,
    ) -> list[dict]:
        blocks: list[dict] = []

        # 1) Cabeçalho de decisão: o que é, quão bom, o que fazer
        if assessment:
            opp_type = OPPORTUNITY_TYPES.get(
                assessment.opportunity_type, "Unknown"
            )
            action = RECOMMENDED_ACTIONS.get(
                assessment.recommended_action, "Monitorizar"
            )
            fit = (
                f"{assessment.fit_score}/100"
                if assessment.fit_score is not None
                else "—"
            )
            decision_lines = [
                f"Tipo: {opp_type}",
                f"Fit: {fit} (confiança: {assessment.fit_confidence}/100)",
                f"Ação recomendada: {action} — {assessment.action_reason}",
            ]
            if assessment.dimensions_markdown():
                decision_lines.append("Dimensões:")
                decision_lines.extend(assessment.dimensions_markdown())

            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {"type": "text", "text": {"content": "Decisão"}}
                        ]
                    },
                }
            )
            for line in decision_lines:
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": line}}
                            ]
                        },
                    }
                )

        # 2) O que falta + perguntas a fazer
        missing = missing_information(extraction)
        questions = assessment.questions_to_ask if assessment else []
        if missing or questions:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "Informação em falta"},
                            }
                        ]
                    },
                }
            )
            for item in missing:
                blocks.append(
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [
                                {"type": "text", "text": {"content": item}}
                            ]
                        },
                    }
                )
            if questions:
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": "Perguntas a fazer:"},
                                }
                            ]
                        },
                    }
                )
                for question in questions:
                    blocks.append(
                        {
                            "object": "block",
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [
                                    {"type": "text", "text": {"content": question}}
                                ]
                            },
                        }
                    )

        # 3) Resumo (extraído da mensagem)
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Resumo"}}]
                },
            }
        )
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": extraction.summary}}
                    ]
                },
            }
        )

        if enrichment:
            blocks.extend(self._enrichment_blocks(enrichment))

        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Mensagem original"}}
                    ]
                },
            }
        )
        # Divide o texto em blocos de parágrafo e respeita o limite de 2000 chars do Notion
        body = self._strip_email_footer(message.body_text)
        for paragraph in body.split("\n\n"):
            if not paragraph.strip():
                continue
            for chunk in self._chunk_text(paragraph.strip(), max_length=1990):
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": chunk}}
                            ]
                        },
                    }
                )

        return blocks

    @staticmethod
    def _sanitize_select_option(value: str) -> str:
        """Sanitiza uma opção de select da Notion.

        A Notion não permite vírgulas em opções de select e limita a 100 chars.
        """
        import re

        value = re.sub(r"\s*,\s*", " / ", value).strip()
        return value[:100]

    @staticmethod
    def _is_consultancy(
        extraction: ProposalExtraction, enrichment: CompanyEnrichment | None
    ) -> bool:
        """True se a empresa for uma consultora/agência de recrutamento.

        Heurística: veio de agência sem empresa cliente conhecida, ou o setor
        indica consultoria/IT services/recruitment.
        """
        if extraction.agency and not extraction.company_client:
            return True
        industry = (enrichment.industry or "").lower() if enrichment else ""
        consultancy_markers = (
            "consult",
            "recruit",
            "staffing",
            "it services",
            "outsourc",
        )
        return any(marker in industry for marker in consultancy_markers)

    @staticmethod
    def _strip_email_footer(body: str) -> str:
        """Remove o rodapé boilerplate do LinkedIn (unsubscribe, help, copyright)."""
        # Corta tudo a partir do separador típico do rodapé
        for marker in (
            "This email was intended for",
            "You are receiving LinkedIn",
            "----------------------------------------",
        ):
            idx = body.find(marker)
            if idx != -1:
                body = body[:idx]
        return body.rstrip()

    def _enrichment_blocks(self, enrichment: CompanyEnrichment) -> list[dict]:
        """Constrói a secção 'Sobre a empresa' como blocos Notion nativos.

        Links ficam clicáveis (url) em vez de markdown cru.
        """
        blocks: list[dict] = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Sobre a empresa"}}
                    ]
                },
            }
        ]

        def bullet(label: str, value: str) -> dict:
            return {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": f"{label}: "},
                            "annotations": {"bold": True},
                        },
                        {"type": "text", "text": {"content": value}},
                    ]
                },
            }

        def link_bullet(label: str, url: str) -> dict:
            return {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": f"{label}: "},
                            "annotations": {"bold": True},
                        },
                        {
                            "type": "text",
                            "text": {"content": url, "link": {"url": url}},
                        },
                    ]
                },
            }

        if enrichment.website:
            blocks.append(link_bullet("Website", enrichment.website))
        if enrichment.linkedin_url:
            blocks.append(link_bullet("LinkedIn", enrichment.linkedin_url))
        if enrichment.teamlyzer_url:
            blocks.append(link_bullet("Teamlyzer", enrichment.teamlyzer_url))
        if enrichment.industry:
            blocks.append(bullet("Setor", enrichment.industry))
        if enrichment.size:
            blocks.append(bullet("Tamanho", enrichment.size))
        if enrichment.headquarters:
            blocks.append(bullet("Sede", enrichment.headquarters))

        def para(text: str) -> dict:
            return {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }

        if enrichment.description:
            blocks.append(para(enrichment.description))
        if enrichment.tech_stack:
            blocks.append(bullet("Stack", ", ".join(enrichment.tech_stack)))
        if enrichment.recent_news:
            for news in enrichment.recent_news:
                blocks.append(
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [
                                {"type": "text", "text": {"content": news}}
                            ]
                        },
                    }
                )
        if enrichment.notes:
            blocks.append(para(enrichment.notes))
        return blocks

    @staticmethod
    def _chunk_text(text: str, max_length: int = 1990) -> list[str]:
        """Divide um texto em chunks que respeitem o limite de caracteres do Notion."""
        chunks: list[str] = []
        while text:
            chunk = text[:max_length]
            chunks.append(chunk)
            text = text[max_length:]
        return chunks or [""]

    def _replace_page_content(self, page_id: str, new_blocks: list[dict]) -> None:
        """Apaga o conteúdo existente e adiciona o novo."""
        existing = self.client.blocks.children.list(block_id=page_id)
        for block in existing.get("results", []):
            self.client.blocks.delete(block_id=block["id"])
        self.client.blocks.children.append(block_id=page_id, children=new_blocks)

    @staticmethod
    def _confidence_label(confidence: float) -> str:
        if confidence >= 0.8:
            return "Alta"
        if confidence >= 0.5:
            return "Média"
        return "Baixa"
