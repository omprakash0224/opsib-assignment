"""
Reconciliation & Exception Detection REST API Endpoints.

Endpoints:
  POST /api/v1/reconciliation/run   – Trigger deterministic reconciliation.
  GET  /api/v1/exceptions           – List exceptions with optional filters.
  GET  /api/v1/exceptions/{id}      – Retrieve a single exception by ID.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.exception import OperationalException
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine
from app.db.models.task import HumanTask
from app.detection.service import DetectionService
from app.core.security import get_tenant_id
from app.schemas.reconciliation_schemas import (
    ExceptionListResponse,
    OperationalExceptionResponse,
    OperationalExceptionDetailResponse,
    CanonicalPOLineSummary,
    CanonicalGoodsReceiptLineSummary,
    LinkedTaskSummary,
    ReconciliationRunResponse,
)

router = APIRouter(tags=["Reconciliation & Exceptions"])


def _orm_to_response(exc: OperationalException) -> OperationalExceptionResponse:
    """Convert an ORM OperationalException to the API response schema."""
    return OperationalExceptionResponse(
        id=exc.id,
        tenant_id=exc.tenant_id,
        exception_key=exc.exception_key,
        exception_type=exc.exception_type,
        status=exc.status,
        po_number=exc.po_number,
        po_line_number=exc.po_line_number,
        canonical_product_id=exc.canonical_product_id,
        canonical_location_id=exc.canonical_location_id,
        expected_qty_base=exc.expected_qty_base,
        received_qty_base=exc.received_qty_base,
        variance_qty_base=exc.variance_qty_base,
        base_uom=exc.base_uom,
        rule_version=exc.rule_version,
        evidence_records=exc.evidence_records or [],
        neutral_summary=exc.neutral_summary,
        missing_evidence=exc.missing_evidence or [],
        detected_at=exc.detected_at,
        resolved_at=exc.resolved_at,
        resolution_notes=exc.resolution_notes,
    )


@router.post(
    "/reconciliation/run",
    response_model=ReconciliationRunResponse,
    status_code=200,
    summary="Trigger deterministic reconciliation against frozen evaluation time",
    description=(
        "Runs the shortfall detection engine against all canonical PO lines "
        "using the frozen evaluation clock (2026-10-16T15:00:00+05:30). "
        "Also auto-creates HumanTasks for each new exception with workforce routing. "
        "This endpoint is idempotent: repeated calls will not create duplicate exceptions or tasks."
    ),
)
def run_reconciliation(
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """Execute reconciliation run, create tasks, return summary with new exceptions."""
    from app.workflows.task_service import create_task_for_exception  # avoid circular at module level

    result = DetectionService.run_reconciliation(session=db, tenant_id=tenant_id)

    # Collect newly detected exceptions; create tasks for each
    new_exceptions: list = []
    for finding in result.findings:
        if finding.is_shortfall:
            exc_obj = (
                db.query(OperationalException)
                .filter_by(exception_key=finding.exception_key)
                .first()
            )
            if exc_obj:
                new_exceptions.append(_orm_to_response(exc_obj))
                # Idempotent: create_task_for_exception skips if task already exists
                create_task_for_exception(session=db, exception=exc_obj, tenant_id=tenant_id)

    return ReconciliationRunResponse(
        run_at=result.run_at,
        evaluation_time=result.evaluation_time,
        total_po_lines_evaluated=result.total_po_lines_evaluated,
        skipped_cancelled=result.skipped_cancelled,
        skipped_future_deadline=result.skipped_future_deadline,
        fully_satisfied=result.fully_satisfied,
        shortfalls_detected=result.shortfalls_detected,
        exceptions_created=result.exceptions_created,
        exceptions_already_existed=result.exceptions_already_existed,
        new_exceptions=new_exceptions,
    )


@router.get(
    "/exceptions",
    response_model=ExceptionListResponse,
    summary="List operational exceptions with optional filters",
)
def list_exceptions(
    status: Optional[str] = Query(None, description="Filter by exception status (e.g. DETECTED, RESOLVED)"),
    canonical_location_id: Optional[str] = Query(None, description="Filter by canonical location (e.g. STORE-014)"),
    po_number: Optional[str] = Query(None, description="Filter by PO number (e.g. PO-501)"),
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """Return all operational exceptions, optionally filtered."""
    query = db.query(OperationalException).filter(OperationalException.tenant_id == tenant_id)

    if status:
        query = query.filter(OperationalException.status == status)
    if canonical_location_id:
        query = query.filter(OperationalException.canonical_location_id == canonical_location_id)
    if po_number:
        query = query.filter(OperationalException.po_number == po_number)

    exceptions = query.order_by(OperationalException.detected_at.desc()).all()

    return ExceptionListResponse(
        total=len(exceptions),
        items=[_orm_to_response(exc) for exc in exceptions],
    )


@router.get(
    "/exceptions/{exception_id}",
    response_model=OperationalExceptionDetailResponse,
    summary="Retrieve a single exception by ID with detailed context",
    description="Returns the exception alongside its canonical purchase order line, matching goods receipts, and active workforce task.",
)
def get_exception(exception_id: str, db: Session = Depends(get_db)):
    """Return detailed exception record including evidence, canonical lines, and task."""
    exc = db.query(OperationalException).filter_by(id=exception_id).first()
    if not exc:
        raise HTTPException(status_code=404, detail=f"Exception '{exception_id}' not found.")

    base_resp = _orm_to_response(exc)

    # 1. Supporting PO Line
    po_line = (
        db.query(PurchaseOrderLine)
        .filter_by(po_number=exc.po_number, line_number=exc.po_line_number)
        .first()
    )
    po_summary = None
    if po_line:
        po_summary = CanonicalPOLineSummary(
            id=po_line.id,
            po_number=po_line.po_number,
            line_number=po_line.line_number,
            supplier_code=po_line.supplier_code,
            canonical_location_id=po_line.canonical_location_id,
            canonical_product_id=po_line.canonical_product_id,
            ordered_qty_base=po_line.ordered_qty_base,
            base_uom=po_line.base_uom,
            status=po_line.status,
            expected_receipt_by=po_line.expected_receipt_by,
        )

    # 2. Supporting Goods Receipt Lines
    receipt_lines = (
        db.query(GoodsReceiptLine)
        .filter_by(po_number=exc.po_number, po_line_number=exc.po_line_number)
        .all()
    )
    grn_summaries = [
        CanonicalGoodsReceiptLineSummary(
            id=rl.id,
            receipt_number=rl.receipt_number,
            receipt_line=rl.receipt_line,
            po_number=rl.po_number,
            po_line_number=rl.po_line_number,
            accepted_qty_base=rl.accepted_qty_base,
            base_uom=rl.base_uom,
            received_at=rl.received_at,
            posting_status=rl.posting_status,
        )
        for rl in receipt_lines
    ]

    # 3. Linked Human Task
    task = db.query(HumanTask).filter_by(exception_id=exc.id).first()
    task_summary = None
    if task:
        task_summary = LinkedTaskSummary(
            id=task.id,
            task_key=task.task_key,
            status=task.status,
            assignee_id=task.assignee_id,
            assignment_reason=task.assignment_reason,
            resolution_code=task.resolution_code,
            resolution_evidence_ref=task.resolution_evidence_ref,
            due_at=task.due_at,
        )

    return OperationalExceptionDetailResponse(
        **base_resp.model_dump(),
        purchase_order_line=po_summary,
        goods_receipt_lines=grn_summaries,
        task=task_summary,
    )
