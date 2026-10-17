"""
Routing Policy: Eligibility evaluator for workforce assignment.

Implements the exact eligibility rules from PLAN.md §5.1:

Primary assignment (receiving_associate):
  1. Employee must be at the same canonical_location_id as the exception.
  2. Employee role must be 'receiving_associate'.
  3. employment_status must be 'ACTIVE' (HRMS wins over shift data — HR-103 rule).
  4. The latest WorkforceShift record must show availability_status == 'ON_SHIFT'.

Fallback (store_manager):
  If no primary candidate qualifies, query active ON_SHIFT store managers at the
  same location.

Unassigned queue:
  If no candidate at any tier is eligible, task is queued with
  assignment_reason='MANUAL_ASSIGNMENT_REQUIRED'.
"""
from dataclasses import dataclass
from typing import Optional
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models.canonical import Employee, WorkforceShift


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutingDecision:
    """
    Encapsulates the result of the workforce routing engine for one exception.
    """
    assignee_id: Optional[str]          # employee_id or None
    assignment_reason: str              # e.g. PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE
    eligibility_log: list               # list of dicts recording each candidate's fate


# ---------------------------------------------------------------------------
# Assignment Reason Constants
# ---------------------------------------------------------------------------

REASON_PRIMARY = "PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE"
REASON_FALLBACK_MANAGER = "FALLBACK_STORE_MANAGER"
REASON_MANUAL = "MANUAL_ASSIGNMENT_REQUIRED"


# ---------------------------------------------------------------------------
# Eligibility Helpers
# ---------------------------------------------------------------------------

def _get_latest_shift(session: Session, employee_id: str) -> Optional[WorkforceShift]:
    """
    Returns the most recent WorkforceShift record for the given employee,
    ordered by observed_at descending.
    """
    return (
        session.query(WorkforceShift)
        .filter(WorkforceShift.employee_id == employee_id)
        .order_by(WorkforceShift.observed_at.desc())
        .first()
    )


def _is_eligible(
    session: Session,
    employee: Employee,
    eligibility_log: list,
) -> bool:
    """
    Applies the two-gate eligibility check:
      Gate 1: HRMS employment_status == 'ACTIVE'  (hard gate; wins over shift data).
      Gate 2: Latest workforce shift availability_status == 'ON_SHIFT'.
    """
    # Gate 1: HR status
    if employee.employment_status != "ACTIVE":
        eligibility_log.append({
            "employee_id": employee.employee_id,
            "rejected_reason": f"employment_status is '{employee.employment_status}' (not ACTIVE)",
        })
        logger.info(
            f"[ROUTING] {employee.employee_id} REJECTED: employment_status={employee.employment_status}"
        )
        return False

    # Gate 2: Shift status
    shift = _get_latest_shift(session, employee.employee_id)
    if shift is None:
        eligibility_log.append({
            "employee_id": employee.employee_id,
            "rejected_reason": "No shift record found",
        })
        logger.info(f"[ROUTING] {employee.employee_id} REJECTED: no shift record")
        return False

    if shift.availability_status != "ON_SHIFT":
        eligibility_log.append({
            "employee_id": employee.employee_id,
            "rejected_reason": f"availability_status is '{shift.availability_status}' (not ON_SHIFT)",
        })
        logger.info(
            f"[ROUTING] {employee.employee_id} REJECTED: availability_status={shift.availability_status}"
        )
        return False

    eligibility_log.append({
        "employee_id": employee.employee_id,
        "selected": True,
        "reason": "Active and ON_SHIFT",
    })
    return True


# ---------------------------------------------------------------------------
# Core Policy Engine
# ---------------------------------------------------------------------------

class RoutingPolicy:
    """
    Workforce routing engine.

    Given an exception's canonical_location_id, evaluates employees in two tiers:
      Tier 1: receiving_associate at the same location who is ACTIVE + ON_SHIFT.
      Tier 2 (fallback): store_manager at the same location who is ACTIVE + ON_SHIFT.
      Tier 3: Unassigned queue (MANUAL_ASSIGNMENT_REQUIRED).
    """

    @classmethod
    def route(
        cls,
        session: Session,
        canonical_location_id: str,
    ) -> RoutingDecision:
        """
        Execute routing for an exception at the given location.

        Returns a RoutingDecision with the selected assignee_id (or None),
        the assignment_reason, and a full eligibility_log for audit purposes.
        """
        eligibility_log: list = []

        # --- Tier 1: Primary — receiving associates at this location ---
        primary_candidates = (
            session.query(Employee)
            .filter(
                Employee.canonical_location_id == canonical_location_id,
                Employee.role == "receiving_associate",
            )
            .all()
        )

        selected_candidate = None
        for candidate in primary_candidates:
            if _is_eligible(session, candidate, eligibility_log):
                if selected_candidate is None:
                    selected_candidate = candidate

        if selected_candidate:
            logger.info(
                f"[ROUTING] Selected primary: {selected_candidate.employee_id} "
                f"({selected_candidate.display_name}) at {canonical_location_id}"
            )
            return RoutingDecision(
                assignee_id=selected_candidate.employee_id,
                assignment_reason=REASON_PRIMARY,
                eligibility_log=eligibility_log,
            )

        logger.info(
            f"[ROUTING] No eligible receiving_associate at {canonical_location_id}; "
            f"escalating to fallback."
        )

        # --- Tier 2: Fallback — store managers at this location ---
        from app.routing.fallback import find_fallback_manager  # avoid circular
        fallback_id = find_fallback_manager(session, canonical_location_id, eligibility_log)
        if fallback_id:
            return RoutingDecision(
                assignee_id=fallback_id,
                assignment_reason=REASON_FALLBACK_MANAGER,
                eligibility_log=eligibility_log,
            )

        # --- Tier 3: Unassigned ---
        logger.info(
            f"[ROUTING] No eligible candidate at {canonical_location_id}; "
            f"task queued as MANUAL_ASSIGNMENT_REQUIRED."
        )
        eligibility_log.append({"result": "UNASSIGNED", "location": canonical_location_id})
        return RoutingDecision(
            assignee_id=None,
            assignment_reason=REASON_MANUAL,
            eligibility_log=eligibility_log,
        )
