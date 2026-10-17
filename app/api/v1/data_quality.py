"""
Data Quality API Endpoints.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.exception import DataQualityIssue
from app.schemas.import_schemas import DataQualityIssueResponse
from app.core.security import get_tenant_id

router = APIRouter(prefix="/data-quality", tags=["Imports & Data Quality"])


@router.get("", response_model=List[DataQualityIssueResponse])
def get_data_quality_issues(
    issue_type: Optional[str] = Query(None, description="Filter by issue type (e.g. UNMAPPED_CROSSWALK, CONFLICTING_DUPLICATE)"),
    dataset: Optional[str] = Query(None, description="Filter by dataset name"),
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Retrieves unresolved data quality issues, unmapped crosswalks, and conflicting duplicates.
    Keeps bad or ambiguous data visible without contaminating business exception reporting.
    """
    query = db.query(DataQualityIssue).filter(DataQualityIssue.tenant_id == tenant_id)
    if issue_type:
        query = query.filter(DataQualityIssue.issue_type == issue_type)
    if dataset:
        query = query.filter(DataQualityIssue.dataset_name == dataset)

    issues = query.order_by(DataQualityIssue.created_at.desc()).all()
    return [
        DataQualityIssueResponse(
            id=issue.id,
            tenant_id=issue.tenant_id,
            source_system=issue.source_system,
            dataset_name=issue.dataset_name,
            source_record_id=issue.source_record_id,
            issue_type=issue.issue_type,
            details=issue.details,
            created_at=issue.created_at,
        )
        for issue in issues
    ]
