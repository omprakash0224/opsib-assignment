"""
OpenRouter AI Investigation Provider (Task D / ADR-7).

Integrates with OpenRouter (https://openrouter.ai/api/v1/chat/completions) using httpx.
Provides grounded advisory briefs formatted as structured JSON.
"""
import json
import logging
from typing import Optional
import httpx

from app.ai.interface import AIInvestigationProvider
from app.core.exceptions import AIProviderError
from app.config import (

    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_BASE_URL,
    OPENROUTER_HTTP_REFERER,
    AI_REQUEST_TIMEOUT_SECONDS,
)
from app.schemas.ai_schemas import EvidencePackage, InvestigationBrief

logger = logging.getLogger(__name__)

OPENROUTER_SYSTEM_PROMPT = """
You are an expert retail operational copilot for OPSIB (Operational Intelligence & Task Reliability).
Your task is to review factual evidence regarding a detected Goods Receipt Shortfall against a Purchase Order and produce a structured advisory investigation brief.

CRITICAL INVARIANTS & SAFETY RULES:
1. EPISTEMIC NEUTRALITY: The available dataset contains NO supplier dispatch notice or Advanced Shipping Notice (ASN). Therefore, you MUST NOT assert that the supplier is at fault, that goods were lost by a carrier, or that theft occurred. Presume neither supplier fault nor internal loss.
2. ZERO ARITHMETIC OVERRIDE: Do not recalculate or contradict the variance calculated by the deterministic engine.
3. GROUNDED CITATIONS ONLY: You must list all cited source record IDs in 'cited_record_ids'. Every single ID cited in this field MUST be an exact match to one of the IDs provided in the 'valid_source_record_ids' whitelist. Do not invent or hallucinate record IDs.
4. STRICT JSON FORMAT: Output MUST be a single, valid JSON object matching the requested schema.

JSON SCHEMA REQUIREMENT:
{
  "executive_summary": "Concise summary of the shortfall and context",
  "analysis_of_evidence": "Factual breakdown of PO expected vs receipts accepted",
  "missing_context": ["Missing fact 1 (e.g. absent ASN)", "Missing fact 2"],
  "recommended_investigation_steps": ["Step 1", "Step 2", "Step 3"],
  "cited_record_ids": ["ID_1", "ID_2"],
  "confidence_score": 0.95,
  "safety_warnings": [],
  "neutrality_disclaimer": "Advisory summary only. Supplier dispatch / ASN data absent; findings do not establish supplier fault or transit loss."
}
""".strip()


class OpenRouterInvestigationProvider(AIInvestigationProvider):
    """
    Live AI investigation provider using OpenRouter's OpenAI-compatible completions API.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self._api_key = api_key or OPENROUTER_API_KEY
        self._model = model or OPENROUTER_MODEL
        self._base_url = (base_url or OPENROUTER_BASE_URL).rstrip("/")
        self._timeout = timeout or AI_REQUEST_TIMEOUT_SECONDS
        self._client = http_client

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout)

    def _build_user_prompt(self, evidence: EvidencePackage) -> str:
        evidence_dict = evidence.model_dump(mode="json")
        return (
            f"Please analyze the following operational exception evidence package and produce "
            f"a grounded advisory investigation brief in JSON format:\n\n"
            f"{json.dumps(evidence_dict, indent=2)}"
        )

    def generate_brief(self, evidence: EvidencePackage) -> InvestigationBrief:
        if not self._api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not configured. Please set the environment variable "
                "or specify use_mock=True."
            )

        endpoint = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": OPENROUTER_HTTP_REFERER,
            "X-Title": "OPSIB Operational Intelligence",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": OPENROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_user_prompt(evidence)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        client = self._get_client()
        try:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            logger.error("OpenRouter request timed out after %s seconds: %s", self._timeout, exc)
            raise AIProviderError(f"OpenRouter request timed out after {self._timeout}s", {"timeout": self._timeout}) from exc
        except httpx.HTTPStatusError as exc:
            logger.error("OpenRouter API error HTTP %s: %s", exc.response.status_code, exc.response.text)
            raise AIProviderError(
                f"OpenRouter API returned HTTP {exc.response.status_code}",
                {"status_code": exc.response.status_code, "response": exc.response.text},
            ) from exc
        except Exception as exc:
            logger.error("Unexpected error connecting to OpenRouter: %s", exc)
            raise AIProviderError(f"Failed to communicate with OpenRouter: {exc}") from exc
        finally:
            # If we created our own temporary client, close it
            if self._client is None:
                client.close()

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            parsed_content = json.loads(content)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            logger.error("Failed to parse valid JSON response from OpenRouter: %s", exc)
            raise AIProviderError(f"Malformed JSON response received from OpenRouter: {exc}") from exc


        return InvestigationBrief(**parsed_content)
