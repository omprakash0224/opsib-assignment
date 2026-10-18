"""
Task Service: High-level task operations with routing, state transitions, and audit.

This service is the single point of truth for all HumanTask mutations.
Every method:
  1. Validates pre-conditions.
  2. Delegates state validation to state_machine.py.
  3. Mutates the HumanTask ORM record.
  4. Writes a TaskAuditEvent via audit_service.py.
  5. Commits the transaction.

Idempotency (ADR-5):
  - task_key on HumanTask has a UNIQUE constraint: running create_task_for_exception
    twice for the same exception produces only one task.
  - complete_task checks for an identical-payload retry and returns 200 OK instead
    of raising a 409 Conflict.
"""
from datetime import datetime, timezone, timedelta
from typing import Callable, List, Optional
from sqlalchemy.orm import Session

from app.config import DEFAULT_TENANT_ID, get_current_time
from app.core.logging import logger
from app.core.exceptions import (
    InvalidStateTransitionError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    IneligibleAssigneeError,
)
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent
from app.db.models.canonical import Employee
from app.db.models.exception import OperationalException
from app.routing.policy import RoutingPolicy, check_assignable
from app.workflows import audit_service
from app.workflows.state_machine import (
    assert_transition,
    assert_reassignable,
    is_idempotent_completion,
    ALLOWED_TRANSITIONS,
)
from app.schemas.task_schemas import ALLOWED_RESOLUTION_CODES


# Default task due window (hours from detection)
DEFAULT_DUE_HOURS = 24


def _build_task_key(tenant_id: str, exception_id: str) -> str:
    """Deterministic task key prevents duplicate task creation for the same exception."""
    return f"{tenant_id}:TASK:{exception_id}"


# ---------------------------------------------------------------------------
# Task Creation + Routing
# ---------------------------------------------------------------------------

def create_task_for_exception(
    session: Session,
    exception: OperationalException,
    tenant_id: str = DEFAULT_TENANT_ID,
    actor_id: str = "SYSTEM",
) -> HumanTask:
    """
    Create (or retrieve existing) HumanTask for an OperationalException.

    Idempotent: if a task already exists for this exception (matched by task_key),
    the existing task is returned without any state mutation.

    Routing:
      - Runs RoutingPolicy.route() to find the best eligible assignee.
      - If found: task status = ASSIGNED; event_type = ASSIGNED.
      - If not found: task status = CREATED; assignment_reason = MANUAL_ASSIGNMENT_REQUIRED.
    """
    task_key = _build_task_key(tenant_id, exception.id)

    # Idempotency check
    existing = session.query(HumanTask).filter_by(task_key=task_key).first()
    if existing:
        logger.info(f"[TASK] Task already exists for exception {exception.id}: task={existing.id}")
        return existing

    if exception.status in ("AUTO_CLOSED", "RESOLVED", "DISMISSED"):
        logger.info(f"[TASK] Skipping task creation for closed exception {exception.id} (status={exception.status})")
        return None

    # Route
    decision = RoutingPolicy.route(
        session=session,
        canonical_location_id=exception.canonical_location_id,
    )

    initial_status = "ASSIGNED" if decision.assignee_id else "CREATED"
    due_at = get_current_time() + timedelta(hours=DEFAULT_DUE_HOURS)

    task = HumanTask(
        tenant_id=tenant_id,
        exception_id=exception.id,
        task_key=task_key,
        status=initial_status,
        assignee_id=decision.assignee_id,
        assignment_reason=decision.assignment_reason,
        canonical_location_id=exception.canonical_location_id,
        due_at=due_at,
    )
    try:
        session.add(task)
        session.flush()

        event_type = "ASSIGNED" if decision.assignee_id else "CREATED"
        audit_service.record_event(
            session=session,
            task_id=task.id,
            event_type=event_type,
            to_status=initial_status,
            actor_id=actor_id,
            from_status=None,
            reason=decision.assignment_reason,
            payload={
                "exception_id": exception.id,
                "assignee_id": decision.assignee_id,
                "eligibility_log": decision.eligibility_log,
            },
        )

        session.commit()
    except Exception:
        session.rollback()
        raise

    logger.info(
        f"[TASK] Created task {task.id} for exception {exception.id}: "
        f"status={initial_status}, assignee={decision.assignee_id}, "
        f"reason={decision.assignment_reason}"
    )
    return task


# ---------------------------------------------------------------------------
# State Transitions
# ---------------------------------------------------------------------------

def acknowledge_task(
    session: Session,
    task_id: str,
    actor_id: str,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """Transition ASSIGNED -> ACKNOWLEDGED."""
    task = _get_task_or_404(session, task_id)
    if task.status == "ACKNOWLEDGED":
        logger.info(f"[TASK] Idempotent acknowledge retry for task {task_id}.")
        return task
    assert_transition(task.status, "ACKNOWLEDGED")
    if idempotency_key:
        task.idempotency_key = idempotency_key
    _apply_transition(session, task, "ACKNOWLEDGED", "ACKNOWLEDGED", actor_id)
    return task


def start_task(
    session: Session,
    task_id: str,
    actor_id: str,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """Transition ACKNOWLEDGED -> IN_PROGRESS."""
    task = _get_task_or_404(session, task_id)
    if task.status == "IN_PROGRESS":
        logger.info(f"[TASK] Idempotent start retry for task {task_id}.")
        return task
    assert_transition(task.status, "IN_PROGRESS")
    if idempotency_key:
        task.idempotency_key = idempotency_key
    _apply_transition(session, task, "IN_PROGRESS", "STATUS_CHANGE", actor_id)
    return task


def complete_task(
    session: Session,
    task_id: str,
    actor_id: str,
    resolution_code: str,
    resolution_evidence_ref: Optional[str],
    resolution_notes: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """
    Transition IN_PROGRESS -> COMPLETED.

    Idempotent retry handling (ADR-5):
      If the task is already COMPLETED with the same resolution payload,
      returns the existing task with no state change.
      If the task is COMPLETED with a different payload, raises IdempotencyConflictError.
    """
    task = _get_task_or_404(session, task_id)

    # Idempotent retry check
    if task.status == "COMPLETED":
        if is_idempotent_completion(
            task_status=task.status,
            stored_resolution_code=task.resolution_code,
            stored_evidence_ref=task.resolution_evidence_ref,
            incoming_resolution_code=resolution_code,
            incoming_evidence_ref=resolution_evidence_ref,
        ):
            logger.info(f"[TASK] Idempotent completion retry detected for task {task_id}; returning existing state.")
            return task
        else:
            raise IdempotencyConflictError(
                f"Task '{task_id}' is already COMPLETED with different resolution data. "
                f"Stored: code='{task.resolution_code}', ref='{task.resolution_evidence_ref}'. "
                f"Submitted: code='{resolution_code}', ref='{resolution_evidence_ref}'."
            )

    assert_transition(task.status, "COMPLETED")

    if not resolution_code or not resolution_code.strip():
        raise IneligibleAssigneeError("resolution_code must be non-empty.")
    if not resolution_evidence_ref or not resolution_evidence_ref.strip():
        raise IneligibleAssigneeError("resolution_evidence_ref must be non-empty.")
    if resolution_code not in ALLOWED_RESOLUTION_CODES:
        raise IneligibleAssigneeError(
            f"Invalid resolution_code '{resolution_code}'. Allowed: {sorted(ALLOWED_RESOLUTION_CODES)}"
        )

    prev_status = task.status
    task.resolution_code = resolution_code
    task.resolution_evidence_ref = resolution_evidence_ref
    task.resolution_notes = resolution_notes
    task.completed_by = actor_id
    if idempotency_key:
        task.idempotency_key = idempotency_key

    _apply_transition(
        session, task, "COMPLETED", "COMPLETED", actor_id,
        from_status=prev_status,
        payload={
            "resolution_code": resolution_code,
            "resolution_evidence_ref": resolution_evidence_ref,
            "idempotency_key": idempotency_key,
        },
    )
    return task


def verify_task(
    session: Session,
    task_id: str,
    actor_id: str,
    notes: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """Transition PENDING_VERIFICATION -> VERIFIED (closes the exception)."""
    task = _get_task_or_404(session, task_id)
    if task.status == "VERIFIED":
        logger.info(f"[TASK] Idempotent verify retry for task {task_id}.")
        return task
    assert_transition(task.status, "VERIFIED")
    if idempotency_key:
        task.idempotency_key = idempotency_key
    _apply_transition(
        session,
        task,
        "VERIFIED",
        "VERIFIED",
        actor_id,
        payload={"notes": notes, "idempotency_key": idempotency_key},
        after_flush=lambda: _resolve_exception(session, task.exception_id, actor_id),
    )
    return task


def reopen_task(
    session: Session,
    task_id: str,
    actor_id: str,
    reason: str,
) -> HumanTask:
    """Transition PENDING_VERIFICATION -> REOPENED."""
    task = _get_task_or_404(session, task_id)
    assert_transition(task.status, "REOPENED")
    _apply_transition(
        session, task, "REOPENED", "REOPENED", actor_id,
        reason=reason,
    )
    return task


def submit_for_verification(
    session: Session,
    task_id: str,
    actor_id: str,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """Transition COMPLETED -> PENDING_VERIFICATION."""
    task = _get_task_or_404(session, task_id)
    if task.status == "PENDING_VERIFICATION":
        logger.info(f"[TASK] Idempotent submit_for_verification retry for task {task_id}.")
        return task
    assert_transition(task.status, "PENDING_VERIFICATION")
    if idempotency_key:
        task.idempotency_key = idempotency_key
    _apply_transition(session, task, "PENDING_VERIFICATION", "STATUS_CHANGE", actor_id)
    return task


def assign_task(
    session: Session,
    task_id: str,
    actor_id: str,
    assignee_id: str,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """
    Assign a task from CREATED to ASSIGNED.
    Validates assignee eligibility and mandatory reason.
    """
    if not reason or not reason.strip():
        raise IneligibleAssigneeError("Assignment reason is required and cannot be empty.")

    task = _get_task_or_404(session, task_id)
    if task.status == "ASSIGNED" and task.assignee_id == assignee_id:
        logger.info(f"[TASK] Idempotent assign retry for task {task_id} with assignee {assignee_id}.")
        return task

    assert_transition(task.status, "ASSIGNED")

    # Look up employee first
    emp = session.query(Employee).filter_by(employee_id=assignee_id).first()
    if not emp:
        raise ResourceNotFoundError(f"Employee '{assignee_id}' not found.")

    is_eligible, reject_reason, _ = check_assignable(
        session, emp, location_id=task.canonical_location_id
    )
    if not is_eligible:
        raise IneligibleAssigneeError(
            f"Employee '{assignee_id}' is ineligible for task '{task_id}': {reject_reason}",
            details={"employee_id": assignee_id, "task_id": task_id, "reason": reject_reason},
        )

    prev_status = task.status
    task.assignee_id = assignee_id
    task.assignment_reason = reason
    if idempotency_key:
        task.idempotency_key = idempotency_key

    _apply_transition(
        session,
        task,
        "ASSIGNED",
        "ASSIGNED",
        actor_id,
        from_status=prev_status,
        reason=reason,
        payload={
            "assignee_id": assignee_id,
            "actor_id": actor_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
    )
    return task


def reassign_task(
    session: Session,
    task_id: str,
    actor_id: str,
    new_assignee_id: str,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> HumanTask:
    """
    Reassign a task to a different employee.

    Permitted from: ASSIGNED, ACKNOWLEDGED, IN_PROGRESS (PLAN.md §5.2).
    If called on a CREATED task, delegates to assign_task.
    Task lands in ASSIGNED with the new assignee.
    """
    if not reason or not reason.strip():
        raise IneligibleAssigneeError("Reassignment reason is required and cannot be empty.")

    task = _get_task_or_404(session, task_id)

    if task.status == "CREATED":
        return assign_task(
            session=session,
            task_id=task_id,
            actor_id=actor_id,
            assignee_id=new_assignee_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    assert_reassignable(task.status)

    # Look up employee first
    emp = session.query(Employee).filter_by(employee_id=new_assignee_id).first()
    if not emp:
        raise ResourceNotFoundError(f"Employee '{new_assignee_id}' not found.")

    is_eligible, reject_reason, _ = check_assignable(
        session, emp, location_id=task.canonical_location_id
    )
    if not is_eligible:
        raise IneligibleAssigneeError(
            f"Employee '{new_assignee_id}' is ineligible for task '{task_id}': {reject_reason}",
            details={"employee_id": new_assignee_id, "task_id": task_id, "reason": reject_reason},
        )

    if task.status == "ASSIGNED" and task.assignee_id == new_assignee_id:
        logger.info(f"[TASK] Idempotent reassign retry for task {task_id} with assignee {new_assignee_id}.")
        return task

    prev_status = task.status
    prev_assignee = task.assignee_id

    task.assignee_id = new_assignee_id
    task.assignment_reason = f"REASSIGNED_BY_{actor_id}"
    if idempotency_key:
        task.idempotency_key = idempotency_key

    _apply_transition(
        session, task, "ASSIGNED", "REASSIGNED", actor_id,
        from_status=prev_status,
        reason=reason,
        payload={
            "previous_assignee_id": prev_assignee,
            "new_assignee_id": new_assignee_id,
            "idempotency_key": idempotency_key,
        },
    )
    return task


# ---------------------------------------------------------------------------
# Query Helpers
# ---------------------------------------------------------------------------

def get_task_with_audit(
    session: Session,
    task_id: str,
    tenant_id: Optional[str] = None,
) -> tuple[HumanTask, List[TaskAuditEvent]]:
    """Retrieve a task and its full audit trail, scoped by tenant if specified."""
    task = _get_task_or_404(session, task_id, tenant_id=tenant_id)
    events = (
        session.query(TaskAuditEvent)
        .filter_by(task_id=task_id)
        .order_by(TaskAuditEvent.timestamp.asc())
        .all()
    )
    return task, events


def list_tasks(
    session: Session,
    assignee_id: Optional[str] = None,
    canonical_location_id: Optional[str] = None,
    status: Optional[str] = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> List[HumanTask]:
    """Return filtered list of HumanTasks."""
    query = session.query(HumanTask).filter(HumanTask.tenant_id == tenant_id)
    if assignee_id:
        query = query.filter(HumanTask.assignee_id == assignee_id)
    if canonical_location_id:
        query = query.filter(HumanTask.canonical_location_id == canonical_location_id)
    if status:
        query = query.filter(HumanTask.status == status)
    return query.order_by(HumanTask.created_at.desc()).all()


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _get_task_or_404(
    session: Session, task_id: str, tenant_id: Optional[str] = None
) -> HumanTask:
    query = session.query(HumanTask).filter_by(id=task_id)
    if tenant_id:
        query = query.filter_by(tenant_id=tenant_id)
    task = query.first()
    if not task:
        raise ResourceNotFoundError(f"Task '{task_id}' not found.")
    return task


def _stage_transition(
    session: Session,
    task: HumanTask,
    new_status: str,
    event_type: str,
    actor_id: str,
    from_status: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    """Stage status change and audit event without committing."""
    prev = from_status or task.status
    task.status = new_status
    task.updated_at = datetime.now(timezone.utc)
    session.flush()

    audit_service.record_event(
        session=session,
        task_id=task.id,
        event_type=event_type,
        to_status=new_status,
        actor_id=actor_id,
        from_status=prev,
        reason=reason,
        payload=payload,
    )


def _apply_transition(
    session: Session,
    task: HumanTask,
    new_status: str,
    event_type: str,
    actor_id: str,
    from_status: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
    after_flush: Optional[Callable[[], None]] = None,
) -> None:
    """Apply the status change, audit event, optional hook, and commit atomically."""
    try:
        _stage_transition(
            session=session,
            task=task,
            new_status=new_status,
            event_type=event_type,
            actor_id=actor_id,
            from_status=from_status,
            reason=reason,
            payload=payload,
        )
        if after_flush:
            after_flush()
        session.commit()
    except Exception:
        session.rollback()
        raise


def cancel_task_for_exception(
    session: Session,
    exception_id: str,
    reason: str = "Shortfall cleared by late receipt",
    actor_id: str = "SYSTEM",
) -> Optional[HumanTask]:
    """
    Cancel an open task linked to an exception whose shortfall has cleared.
    Transitions task to CANCELLED_NO_LONGER_NEEDED and writes audit trail.
    """
    task = session.query(HumanTask).filter_by(exception_id=exception_id).first()
    if not task:
        return None
    if task.status in ("CANCELLED_NO_LONGER_NEEDED", "VERIFIED"):
        return task

    prev_status = task.status
    assert_transition(prev_status, "CANCELLED_NO_LONGER_NEEDED")
    _stage_transition(
        session=session,
        task=task,
        new_status="CANCELLED_NO_LONGER_NEEDED",
        event_type="CANCELLED",
        actor_id=actor_id,
        from_status=prev_status,
        reason=reason,
        payload={"exception_id": exception_id, "reason": reason},
    )
    return task


def _resolve_exception(session: Session, exception_id: str, actor_id: str) -> None:
    """Mark the linked OperationalException as RESOLVED when a task is VERIFIED."""
    from app.db.models.exception import ExceptionEvent
    exc = session.query(OperationalException).filter_by(id=exception_id).first()
    if exc and exc.status not in ("RESOLVED", "DISMISSED"):
        prev_status = exc.status
        exc.status = "RESOLVED"
        exc.resolved_at = datetime.now(timezone.utc)
        exc.resolution_notes = f"Resolved via task verification by {actor_id}"
        session.flush()

        evt = ExceptionEvent(
            exception_id=exc.id,
            event_type="RESOLVED",
            previous_state={"status": prev_status, "revision": exc.revision},
            new_state={"status": "RESOLVED", "revision": exc.revision},
            actor_id=actor_id,
            notes=f"Resolved via task verification by {actor_id}",
            timestamp=datetime.now(timezone.utc),
        )
        session.add(evt)
        session.flush()
        logger.info(f"[TASK] Exception {exception_id} marked RESOLVED.")
