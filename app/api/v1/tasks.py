"""
Task & Workflow REST API Endpoints (Phase 5 / Task C).

Endpoints:
  GET  /api/v1/tasks                       – List tasks with optional filters
  GET  /api/v1/tasks/{id}                  – Task detail with full audit history
  POST /api/v1/tasks/{id}/acknowledge      – ASSIGNED -> ACKNOWLEDGED
  POST /api/v1/tasks/{id}/start            – ACKNOWLEDGED -> IN_PROGRESS
  POST /api/v1/tasks/{id}/complete         – IN_PROGRESS -> COMPLETED (idempotent)
  POST /api/v1/tasks/{id}/submit-verification – COMPLETED -> PENDING_VERIFICATION
  POST /api/v1/tasks/{id}/verify           – PENDING_VERIFICATION -> VERIFIED
  POST /api/v1/tasks/{id}/reopen           – PENDING_VERIFICATION -> REOPENED
  POST /api/v1/tasks/{id}/reassign         – Reassign from ASSIGNED/ACKNOWLEDGED/IN_PROGRESS
"""
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent
from app.core.security import get_tenant_id
from app.core.exceptions import (
    InvalidStateTransitionError,
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from app.schemas.task_schemas import (
    AcknowledgeRequest,
    CompleteRequest,
    HumanTaskDetailResponse,
    HumanTaskResponse,
    ReassignRequest,
    ReopenRequest,
    StartRequest,
    SubmitForVerificationRequest,
    TaskAuditEventResponse,
    TaskListResponse,
    VerifyRequest,
)
from app.workflows import task_service

router = APIRouter(tags=["Tasks & Workflow"])


# ---------------------------------------------------------------------------
# ORM -> Schema Converters
# ---------------------------------------------------------------------------

def _task_to_response(task: HumanTask) -> HumanTaskResponse:
    return HumanTaskResponse(
        id=task.id,
        tenant_id=task.tenant_id,
        exception_id=task.exception_id,
        task_key=task.task_key,
        status=task.status,
        assignee_id=task.assignee_id,
        assignment_reason=task.assignment_reason,
        canonical_location_id=task.canonical_location_id,
        due_at=task.due_at,
        resolution_code=task.resolution_code,
        resolution_evidence_ref=task.resolution_evidence_ref,
        resolution_notes=task.resolution_notes,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _event_to_response(evt: TaskAuditEvent) -> TaskAuditEventResponse:
    return TaskAuditEventResponse(
        id=evt.id,
        task_id=evt.task_id,
        actor_id=evt.actor_id,
        event_type=evt.event_type,
        from_status=evt.from_status,
        to_status=evt.to_status,
        reason=evt.reason,
        event_payload=evt.event_payload,
        timestamp=evt.timestamp,
    )



# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="List tasks with optional filters",
)
def list_tasks(
    assignee_id: Optional[str] = Query(None),
    canonical_location_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    tasks = task_service.list_tasks(
        session=db,
        assignee_id=assignee_id,
        canonical_location_id=canonical_location_id,
        status=status,
        tenant_id=tenant_id,
    )
    return TaskListResponse(total=len(tasks), items=[_task_to_response(t) for t in tasks])


@router.get(
    "/tasks/{task_id}",
    response_model=HumanTaskDetailResponse,
    summary="Task detail with full audit history",
)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task, events = task_service.get_task_with_audit(db, task_id)
    resp = _task_to_response(task)
    return HumanTaskDetailResponse(
        **resp.model_dump(),
        audit_events=[_event_to_response(e) for e in events],
    )


@router.post(
    "/tasks/{task_id}/acknowledge",
    response_model=HumanTaskResponse,
    summary="Acknowledge an assigned task (ASSIGNED -> ACKNOWLEDGED)",
)
def acknowledge_task(
    task_id: str,
    body: AcknowledgeRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.acknowledge_task(
        db, task_id, actor_id=body.actor_id, idempotency_key=idempotency_key
    )
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/start",
    response_model=HumanTaskResponse,
    summary="Start investigation (ACKNOWLEDGED -> IN_PROGRESS)",
)
def start_task(
    task_id: str,
    body: StartRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.start_task(
        db, task_id, actor_id=body.actor_id, idempotency_key=idempotency_key
    )
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/complete",
    response_model=HumanTaskResponse,
    summary="Complete task with resolution (IN_PROGRESS -> COMPLETED). Idempotent on retry.",
)
def complete_task(
    task_id: str,
    body: CompleteRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.complete_task(
        db,
        task_id,
        actor_id=body.actor_id,
        resolution_code=body.resolution_code,
        resolution_evidence_ref=body.resolution_evidence_ref,
        resolution_notes=body.resolution_notes,
        idempotency_key=idempotency_key,
    )
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/submit-verification",
    response_model=HumanTaskResponse,
    summary="Submit completed task for verification (COMPLETED -> PENDING_VERIFICATION)",
)
def submit_for_verification(
    task_id: str,
    body: SubmitForVerificationRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.submit_for_verification(
        db, task_id, actor_id=body.actor_id, idempotency_key=idempotency_key
    )
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/verify",
    response_model=HumanTaskResponse,
    summary="Verify task and close exception (PENDING_VERIFICATION -> VERIFIED)",
)
def verify_task(
    task_id: str,
    body: VerifyRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.verify_task(
        db, task_id, actor_id=body.actor_id, notes=body.notes, idempotency_key=idempotency_key
    )
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/reopen",
    response_model=HumanTaskResponse,
    summary="Reopen task for rework (PENDING_VERIFICATION -> REOPENED)",
)
def reopen_task(
    task_id: str,
    body: ReopenRequest,
    db: Session = Depends(get_db),
):
    task = task_service.reopen_task(db, task_id, actor_id=body.actor_id, reason=body.reason)
    return _task_to_response(task)


@router.post(
    "/tasks/{task_id}/reassign",
    response_model=HumanTaskResponse,
    summary="Reassign task to a different employee",
    description="Permitted from ASSIGNED, ACKNOWLEDGED, or IN_PROGRESS states.",
)
def reassign_task(
    task_id: str,
    body: ReassignRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    task = task_service.reassign_task(
        db,
        task_id,
        actor_id=body.actor_id,
        new_assignee_id=body.new_assignee_id,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
    return _task_to_response(task)
