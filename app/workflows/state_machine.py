"""
Task State Machine: Server-enforced lifecycle transitions for HumanTask.

Implements PLAN.md §5.2 and the stateDiagram in ARCHITECTURE.md §3.4.

Permitted transitions (exact set — all others raise InvalidStateTransitionError):
  CREATED            -> ASSIGNED
  CREATED            -> CREATED   (idempotent no-op for unassigned queue writes)
  ASSIGNED           -> ACKNOWLEDGED
  ASSIGNED           -> ASSIGNED   (reassignment stays in ASSIGNED after new assignment)
  ACKNOWLEDGED       -> IN_PROGRESS
  ACKNOWLEDGED       -> ASSIGNED   (reassignment)
  IN_PROGRESS        -> COMPLETED
  IN_PROGRESS        -> BLOCKED
  IN_PROGRESS        -> ASSIGNED   (reassignment)
  BLOCKED            -> IN_PROGRESS
  COMPLETED          -> PENDING_VERIFICATION
  PENDING_VERIFICATION -> VERIFIED
  PENDING_VERIFICATION -> REOPENED
  REOPENED           -> IN_PROGRESS

Reassignment is a horizontal move: the task lands back in ASSIGNED with the
new assignee regardless of its prior ASSIGNED/ACKNOWLEDGED/IN_PROGRESS state.

VERIFIED is a terminal state; no further transitions are permitted.
"""
from typing import Optional

from app.core.exceptions import InvalidStateTransitionError
from app.core.logging import logger

# ---------------------------------------------------------------------------
# Transition Table
# ---------------------------------------------------------------------------
# Maps each status to the set of statuses it may legally transition to.

ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "CREATED":               ["ASSIGNED"],
    "ASSIGNED":              ["ACKNOWLEDGED", "ASSIGNED"],  # ASSIGNED->ASSIGNED for reassign
    "ACKNOWLEDGED":          ["IN_PROGRESS", "ASSIGNED"],
    "IN_PROGRESS":           ["COMPLETED", "BLOCKED", "ASSIGNED"],
    "BLOCKED":               ["IN_PROGRESS"],
    "COMPLETED":             ["PENDING_VERIFICATION"],
    "PENDING_VERIFICATION":  ["VERIFIED", "REOPENED"],
    "REOPENED":              ["IN_PROGRESS"],
    "VERIFIED":              [],   # Terminal
}

# States that allow reassignment (lateral move back to ASSIGNED).
REASSIGNABLE_STATES: frozenset[str] = frozenset({
    "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS",
})

# States from which a task completion must provide resolution data.
COMPLETION_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "IN_PROGRESS",
})


# ---------------------------------------------------------------------------
# Transition Functions
# ---------------------------------------------------------------------------

def assert_transition(from_status: str, to_status: str) -> None:
    """
    Assert that a state transition is legally permitted.

    Raises:
        InvalidStateTransitionError: if the transition is not in the allowed set.
    """
    allowed = ALLOWED_TRANSITIONS.get(from_status, [])
    if to_status not in allowed:
        logger.warning(
            f"[STATE_MACHINE] Illegal transition attempted: {from_status} -> {to_status}"
        )
        raise InvalidStateTransitionError(
            from_status=from_status,
            to_status=to_status,
            allowed=allowed,
        )
    logger.info(f"[STATE_MACHINE] Transition approved: {from_status} -> {to_status}")


def assert_reassignable(current_status: str) -> None:
    """
    Assert that the task is in a state that allows reassignment.

    Reassignment returns the task to ASSIGNED. It is permitted from
    ASSIGNED, ACKNOWLEDGED, and IN_PROGRESS states.

    Raises:
        InvalidStateTransitionError: if reassignment is not permitted.
    """
    if current_status not in REASSIGNABLE_STATES:
        raise InvalidStateTransitionError(
            from_status=current_status,
            to_status="ASSIGNED",
            allowed=sorted(REASSIGNABLE_STATES),
        )


def is_idempotent_completion(
    task_status: str,
    stored_resolution_code: Optional[str],
    stored_evidence_ref: Optional[str],
    incoming_resolution_code: str,
    incoming_evidence_ref: Optional[str],
) -> bool:
    """
    Determine whether a repeated 'complete' request is an idempotent retry.

    Safe retry: task is already COMPLETED and the submitted resolution_code
    and evidence_ref match the stored values.

    If the task is already COMPLETED but the resolution payload differs,
    this is a conflicting update — the caller must raise a 409.

    Returns:
        True  → idempotent retry; return existing task with 200 OK.
        False → conflicting payload; caller should raise IdempotencyConflictError.
    """
    if task_status != "COMPLETED":
        return False
    return (
        stored_resolution_code == incoming_resolution_code
        and stored_evidence_ref == incoming_evidence_ref
    )
