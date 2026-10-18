"""
Security and Request Context Dependencies.
Provides Tenant and Actor context extraction, authentication, and task authorization.
"""
from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import DEFAULT_TENANT_ID
from app.core.exceptions import AuthorizationError
from app.db.session import get_db
from app.db.models.canonical import Employee, LocationAlias
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent


def get_tenant_id(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID", description="Tenant Identifier"),
    tenant_id: Optional[str] = None,
) -> str:
    """Extracts tenant ID from header or query parameter, or falls back to DEFAULT_TENANT_ID."""
    return x_tenant_id or tenant_id or DEFAULT_TENANT_ID


def get_actor_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID", description="Actor / User Identifier")
) -> str:
    """
    Extracts actor ID from header.
    Returns 401 Unauthorized if header is missing or blank on mutating endpoints.
    """
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required header 'X-User-ID'. Authentication is required.",
        )
    return x_user_id.strip()


def get_current_actor(
    db: Session = Depends(get_db),
    actor_id: str = Depends(get_actor_id),
    tenant_id: str = Depends(get_tenant_id),
) -> Employee:
    """
    Validates that the actor exists, is ACTIVE, and belongs to the request tenant.
    Returns the authenticated Employee record.
    """
    employee = db.query(Employee).filter_by(employee_id=actor_id).first()
    if not employee:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Actor '{actor_id}' not found in employee directory.",
        )
    if employee.employment_status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor_id}' is not ACTIVE (employment_status: '{employee.employment_status}').",
        )
    if employee.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor_id}' belongs to tenant '{employee.tenant_id}', not '{tenant_id}'.",
        )
    return employee


def authorize(session: Session, task: HumanTask, actor: Employee, action: str) -> None:
    """
    Central authorization rules for human task actions (PLAN.md / FIX.md §4):
      - acknowledge, start, complete, submit-verification: actor must be current assignee.
      - reassign, assign, reopen: actor must be a manager role at task's location
        (or current assignee for reassign self-handoff).
      - verify: actor must be a manager role at the location AND must not be the person
        who completed the task (separation of duties).

    Raises:
        AuthorizationError: with a clear descriptive message if unauthorized (HTTP 403).
    """
    # 1. Assignee self-actions
    if action in ("acknowledge", "start", "complete", "submit-verification"):
        if actor.employee_id != task.assignee_id:
            raise AuthorizationError(
                f"Actor '{actor.employee_id}' is not the current assignee of task '{task.id}' "
                f"(current assignee: '{task.assignee_id}'). Cannot perform '{action}'."
            )
        return

    # Helper: Check if actor is a manager for task location/region
    def _is_manager_for_task() -> bool:
        if actor.role == "store_manager":
            return actor.canonical_location_id == task.canonical_location_id
        if actor.role == "regional_operations":
            if not task.canonical_location_id:
                return True
            loc_alias = (
                session.query(LocationAlias)
                .filter_by(canonical_location_id=task.canonical_location_id)
                .first()
            )
            loc_region = loc_alias.region_id if loc_alias else None
            return bool(loc_region and actor.region_id == loc_region)
        return False

    # 2. Reassign, Assign, Reopen
    if action in ("reassign", "assign", "reopen"):
        if action == "reassign" and task.assignee_id and actor.employee_id == task.assignee_id:
            return  # Assignee self-handoff permitted
        if not _is_manager_for_task():
            raise AuthorizationError(
                f"Actor '{actor.employee_id}' (role='{actor.role}', location='{actor.canonical_location_id}') "
                f"is not an authorized manager at task location '{task.canonical_location_id}'. Cannot perform '{action}'."
            )
        return

    # 3. Verify
    if action == "verify":
        if not _is_manager_for_task():
            raise AuthorizationError(
                f"Actor '{actor.employee_id}' (role='{actor.role}', location='{actor.canonical_location_id}') "
                f"is not an authorized manager at task location '{task.canonical_location_id}'. Cannot verify task."
            )
        # Separation of duties: The completer cannot verify their own work
        completer_id = getattr(task, "completed_by", None)
        if not completer_id:
            completed_evt = (
                session.query(TaskAuditEvent)
                .filter_by(task_id=task.id, to_status="COMPLETED")
                .order_by(TaskAuditEvent.timestamp.desc())
                .first()
            )
            if completed_evt:
                completer_id = completed_evt.actor_id

        if completer_id and actor.employee_id == completer_id:
            raise AuthorizationError(
                f"Separation of duties violation: Actor '{actor.employee_id}' completed task '{task.id}' "
                f"and cannot also verify it."
            )
        return

    raise AuthorizationError(f"Unknown action '{action}' on task '{task.id}'.")
