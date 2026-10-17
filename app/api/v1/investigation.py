"""
AI Advisory Investigation REST API Endpoints.

Endpoints:
  POST /api/v1/exceptions/{id}/investigate  – Generate a grounded AI advisory brief using OpenRouter / Mock.
"""
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.security import get_tenant_id
from app.ai.service import InvestigationService
from app.schemas.ai_schemas import InvestigationBriefResponse

router = APIRouter(tags=["AI Advisory Investigation"])


@router.post(
    "/exceptions/{id}/investigate",
    response_model=InvestigationBriefResponse,
    status_code=200,
    summary="Generate a grounded AI advisory investigation brief for an exception",
    description=(
        "Invokes the AI copilot (powered by OpenRouter or deterministic mock) to analyze the "
        "evidence package for the specified operational exception. "
        "Strictly advisory: does NOT mutate database records, does NOT calculate authoritative quantities, "
        "and strips ungrounded or hallucinated citations via safety guardrails."
    ),
)
def investigate_exception(
    id: str,
    use_mock: Optional[bool] = Query(

        None,
        description="Explicitly force mock provider (defaults to mock if OPENROUTER_API_KEY is unset)",
    ),
    model: Optional[str] = Query(
        None,
        description="Optional model override for OpenRouter (e.g. 'anthropic/claude-3.5-sonnet')",
    ),
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Produce a grounded investigation brief for an OperationalException.
    Verifies cited record IDs against the factual evidence package.
    """
    return InvestigationService.investigate(
        session=db,
        exception_id=id,
        use_mock=use_mock,
        model_override=model,
    )
