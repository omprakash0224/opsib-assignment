"""
Root API Router Registration.
"""
from fastapi import APIRouter
from app.api.v1.imports import router as imports_router
from app.api.v1.data_quality import router as data_quality_router
from app.api.v1.reconciliation import router as reconciliation_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.investigation import router as investigation_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(imports_router)
api_router.include_router(data_quality_router)
api_router.include_router(reconciliation_router)
api_router.include_router(tasks_router)
api_router.include_router(investigation_router)

