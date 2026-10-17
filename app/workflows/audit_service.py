"""
Audit Service: Immutable audit trail writer for HumanTask lifecycle events.

Every state change, assignment, reassignment, and resolution writes a
TaskAuditEvent record. These records are append-only; nothing is ever
updated or deleted (PLAN.md §5.4, ADR-5).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models.audit import TaskAuditEvent


def record_event(
    session: Session,
    task_id: str,
    event_type: str,
    to_status: str,
    actor_id: str = "SYSTEM",
    from_status: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> TaskAuditEvent:
    """
    Create and flush an immutable TaskAuditEvent.

    Parameters:
        session      – Active SQLAlchemy session (caller owns commit).
        task_id      – UUID of the HumanTask being audited.
        event_type   – One of: CREATED, ASSIGNED, ACKNOWLEDGED, STATUS_CHANGE,
                       REASSIGNED, COMPLETED, VERIFIED, REOPENED.
        to_status    – The new task status after the event.
        actor_id     – Employee ID performing the action, or 'SYSTEM'.
        from_status  – Previous task status (None for creation events).
        reason       – Human-readable reason for the transition.
        payload      – Arbitrary JSON payload with additional context.

    Returns:
        The created TaskAuditEvent ORM instance (flushed but not committed).
    """
    event = TaskAuditEvent(
        task_id=task_id,
        actor_id=actor_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        event_payload=payload or {},
        timestamp=datetime.now(timezone.utc),
    )
    session.add(event)
    session.flush()

    logger.info(
        f"[AUDIT] task={task_id} actor={actor_id} event={event_type} "
        f"{from_status or 'INIT'} -> {to_status}"
    )
    return event
