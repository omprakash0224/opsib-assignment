"""
FastAPI Application Entrypoint.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import FROZEN_TIME, RULE_VERSION
from app.db.session import init_db
from app.api.router import api_router
from app.core.exceptions import (
    OPSIBException,
    DataQualityError,
    InvalidStateTransitionError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    AIProviderError,
    IneligibleAssigneeError,
)
from sqlalchemy.exc import IntegrityError
from app.schemas.common_schemas import ErrorResponse



tags_metadata = [
    {
        "name": "Imports & Data Quality",
        "description": "Multi-source CSV/JSON dataset ingestion, provenance hashing, and data quality error tracking.",
    },
    {
        "name": "Reconciliation & Exceptions",
        "description": "Deterministic receipt shortfall detection engine running against the frozen evaluation clock.",
    },
    {
        "name": "Tasks & Workflow",
        "description": "Workforce task assignment, lifecycle state machine, and append-only audit trail.",
    },
    {
        "name": "AI Advisory Investigation",
        "description": "Grounded AI investigation summaries powered by OpenRouter with citation guardrails.",
    },
    {
        "name": "Health",
        "description": "System health checks, active rule version, and authoritative frozen time.",
    },
]



@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown hooks."""
    init_db()
    yield


app = FastAPI(
    title="OPSIB Retail Exception & Reliability Engine",
    description=(
        "Operational intelligence backend for retail data integration, exception detection, "
        "and workforce task routing with server-enforced state machine and idempotent retry resilience."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
    lifespan=lifespan,
)

# Enable CORS for frontend/review access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Global Exception Handlers (Phase 6 / RFC 7807 Error Envelopes)
# ---------------------------------------------------------------------------

@app.exception_handler(InvalidStateTransitionError)
async def invalid_state_transition_handler(request: Request, exc: InvalidStateTransitionError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(
            error_code="INVALID_STATE_TRANSITION",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(
            error_code="IDEMPOTENCY_CONFLICT",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(ResourceNotFoundError)
async def resource_not_found_handler(request: Request, exc: ResourceNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=ErrorResponse(
            error_code="RESOURCE_NOT_FOUND",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(DataQualityError)
async def data_quality_error_handler(request: Request, exc: DataQualityError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error_code="DATA_QUALITY_ERROR",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(IneligibleAssigneeError)
async def ineligible_assignee_handler(request: Request, exc: IneligibleAssigneeError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error_code="INELIGIBLE_ASSIGNEE",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error_code="DATABASE_INTEGRITY_ERROR",
            message="Database integrity or foreign key constraint violation.",
            details={"orig": str(exc.orig) if hasattr(exc, "orig") else str(exc)},
        ).model_dump(mode="json"),
    )


@app.exception_handler(AIProviderError)
async def ai_provider_error_handler(request: Request, exc: AIProviderError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(
            error_code="AI_PROVIDER_ERROR",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


@app.exception_handler(OPSIBException)
async def opsib_exception_handler(request: Request, exc: OPSIBException):

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            error_code="OPSIB_ERROR",
            message=exc.message,
            details=exc.details,
        ).model_dump(mode="json"),
    )


app.include_router(api_router)


@app.get("/", tags=["Health"])
def health_check():
    """Health check and service status."""
    return {
        "status": "healthy",
        "service": "opsib-backend",
        "rule_version": RULE_VERSION,
        "authoritative_frozen_time": FROZEN_TIME.isoformat(),
    }
