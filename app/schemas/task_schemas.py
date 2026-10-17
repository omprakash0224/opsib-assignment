"""
Pydantic Schemas for the Task & Workflow API (Phase 5).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Audit Event
# ---------------------------------------------------------------------------

class TaskAuditEventResponse(BaseModel):
    """Single audit trail entry."""
    id: str
    task_id: str
    actor_id: str
    event_type: str
    from_status: Optional[str] = None
    to_status: str
    reason: Optional[str] = None
    event_payload: Optional[Dict[str, Any]] = None
    timestamp: datetime


# ---------------------------------------------------------------------------
# HumanTask Responses
# ---------------------------------------------------------------------------

class HumanTaskResponse(BaseModel):
    """Summary representation of a human task (list view)."""
    id: str
    tenant_id: str
    exception_id: str
    task_key: str
    status: str
    assignee_id: Optional[str] = None
    assignment_reason: Optional[str] = None
    canonical_location_id: Optional[str] = None
    due_at: Optional[datetime] = None
    resolution_code: Optional[str] = None
    resolution_evidence_ref: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class HumanTaskDetailResponse(HumanTaskResponse):
    """Detail representation of a human task including full audit trail."""
    audit_events: List[TaskAuditEventResponse] = []


class TaskListResponse(BaseModel):
    """Paginated list of tasks."""
    total: int
    items: List[HumanTaskResponse]


# ---------------------------------------------------------------------------
# Request Bodies
# ---------------------------------------------------------------------------

class AcknowledgeRequest(BaseModel):
    """Request body for POST /tasks/{id}/acknowledge."""
    actor_id: str = Field(..., description="Employee ID performing the acknowledgement")


class StartRequest(BaseModel):
    """Request body for POST /tasks/{id}/start."""
    actor_id: str = Field(..., description="Employee ID starting investigation")


class CompleteRequest(BaseModel):
    """Request body for POST /tasks/{id}/complete."""
    actor_id: str = Field(..., description="Employee ID completing the task")
    resolution_code: str = Field(
        ...,
        description="Resolution code (e.g. SUPPLIER_SHORT_SHIPMENT_CONFIRMED, GOODS_FOUND_IN_TRANSIT)",
    )
    resolution_evidence_ref: Optional[str] = Field(
        None,
        description="Reference to supporting evidence document or system record",
    )
    resolution_notes: Optional[str] = Field(None, description="Free-text resolution notes")


class ReassignRequest(BaseModel):
    """Request body for POST /tasks/{id}/reassign."""
    actor_id: str = Field(..., description="Employee ID performing the reassignment")
    new_assignee_id: str = Field(..., description="Employee ID of the new assignee")
    reason: str = Field(..., min_length=1, description="Mandatory reason for reassignment")


class VerifyRequest(BaseModel):
    """Request body for POST /tasks/{id}/verify."""
    actor_id: str = Field(..., description="Manager or system ID performing verification")
    notes: Optional[str] = Field(None, description="Optional verification notes")


class ReopenRequest(BaseModel):
    """Request body for POST /tasks/{id}/reopen."""
    actor_id: str = Field(..., description="Employee or manager reopening the task")
    reason: str = Field(..., min_length=1, description="Reason for reopening (e.g. inadequate evidence)")


class SubmitForVerificationRequest(BaseModel):
    """Request body for POST /tasks/{id}/submit-verification."""
    actor_id: str = Field(..., description="Employee submitting task for manager verification")


# ---------------------------------------------------------------------------
# Routing Run Response (used by POST /reconciliation/run when tasks are created)
# ---------------------------------------------------------------------------

class ReconciliationWithTasksResponse(BaseModel):
    """Extended run response including tasks created alongside exceptions."""
    exceptions_created: int
    tasks_created: int
    tasks: List[HumanTaskResponse] = []
