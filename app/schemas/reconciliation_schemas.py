"""
Pydantic Schemas for the Reconciliation & Exception Detection API (Phase 4).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# OperationalException Response
# ---------------------------------------------------------------------------

class OperationalExceptionResponse(BaseModel):
    """Full representation of a detected operational exception."""
    id: str
    tenant_id: str
    exception_key: str
    exception_type: str
    status: str
    po_number: str
    po_line_number: int
    canonical_product_id: str
    canonical_location_id: str
    expected_qty_base: float
    received_qty_base: float
    variance_qty_base: float
    base_uom: str
    rule_version: str
    evidence_records: List[str]
    neutral_summary: str
    missing_evidence: List[str]
    detected_at: datetime
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None


class CanonicalPOLineSummary(BaseModel):
    """Summary of the associated canonical purchase order line."""
    id: str
    po_number: str
    line_number: int
    supplier_code: str
    canonical_location_id: str
    canonical_product_id: str
    ordered_qty_base: float
    base_uom: str
    status: str
    expected_receipt_by: datetime


class CanonicalGoodsReceiptLineSummary(BaseModel):
    """Summary of an associated canonical goods receipt line."""
    id: str
    receipt_number: str
    receipt_line: int
    po_number: str
    po_line_number: int
    accepted_qty_base: float
    base_uom: str
    received_at: datetime
    posting_status: str


class LinkedTaskSummary(BaseModel):
    """Summary of the active workforce task linked to this exception."""
    id: str
    task_key: str
    status: str
    assignee_id: Optional[str] = None
    assignment_reason: Optional[str] = None
    resolution_code: Optional[str] = None
    resolution_evidence_ref: Optional[str] = None
    due_at: Optional[datetime] = None


class OperationalExceptionDetailResponse(OperationalExceptionResponse):
    """Detailed exception view including supporting PO line, receipt lines, and workforce task."""
    purchase_order_line: Optional[CanonicalPOLineSummary] = None
    goods_receipt_lines: List[CanonicalGoodsReceiptLineSummary] = []
    task: Optional[LinkedTaskSummary] = None


# ---------------------------------------------------------------------------
# Reconciliation Run Response
# ---------------------------------------------------------------------------

class ReconciliationRunResponse(BaseModel):
    """
    Summary returned by POST /api/v1/reconciliation/run.

    Includes counters for each outcome category and the list of all
    newly created exceptions so the caller can immediately inspect findings.
    """
    run_at: datetime
    evaluation_time: datetime
    total_po_lines_evaluated: int
    skipped_cancelled: int
    skipped_future_deadline: int
    fully_satisfied: int
    shortfalls_detected: int
    exceptions_created: int
    exceptions_already_existed: int
    new_exceptions: List[OperationalExceptionResponse] = []


# ---------------------------------------------------------------------------
# Exception List / Filter Response
# ---------------------------------------------------------------------------

class ExceptionListResponse(BaseModel):
    """Paginated list of operational exceptions."""
    total: int
    items: List[OperationalExceptionResponse]
