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
from datetime import datetime
from typing import Optional, Union
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models.canonical import Employee, WorkforceShift, LocationAlias


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
# Eligibility Evaluation Function (check_assignable)
# ---------------------------------------------------------------------------

def check_assignable(
    session: Session,
    employee_or_id: Union[str, Employee],
    location_id: Optional[str] = None,
    at_time: Optional[datetime] = None,
) -> tuple[bool, str, Optional[Employee]]:
    """
    Unified eligibility evaluator used by routing, fallback, and reassignment/assignment.

    Eligibility Rules:
      1. Employee must exist in the database.
      2. employment_status must be 'ACTIVE' (HRMS hard gate).
      3. Location check:
         - Direct match: employee.canonical_location_id == location_id.
         - Regional match: if employee has regional scope (canonical_location_id is None
           or role == 'regional_operations'), employee.region_id must match the location's region.
         - Different store location assignment is rejected.
      4. Shift check:
         - Latest WorkforceShift (observed_at <= at_time if at_time specified, or latest overall)
           must show availability_status == 'ON_SHIFT'.

    Returns:
        (is_eligible: bool, reason: str, employee: Optional[Employee])
    """
    if isinstance(employee_or_id, Employee):
        employee = employee_or_id
    else:
        employee = session.query(Employee).filter_by(employee_id=employee_or_id).first()

    if employee is None:
        return False, f"Employee '{employee_or_id}' not found", None

    # Gate 1: HRMS employment_status == 'ACTIVE'
    if employee.employment_status != "ACTIVE":
        return (
            False,
            f"employment_status={employee.employment_status} (must be ACTIVE)",
            employee,
        )

    # Gate 2: Location / Region matching
    if location_id:
        if employee.canonical_location_id == location_id:
            pass  # Direct store location match
        elif employee.canonical_location_id is None or employee.role == "regional_operations":
            loc_alias = (
                session.query(LocationAlias)
                .filter_by(canonical_location_id=location_id)
                .first()
            )
            loc_region = loc_alias.region_id if loc_alias else None
            if not loc_region or employee.region_id != loc_region:
                return (
                    False,
                    f"Employee region '{employee.region_id}' does not match location '{location_id}' region '{loc_region}'",
                    employee,
                )
        else:
            return (
                False,
                f"Employee location '{employee.canonical_location_id}' does not match task location '{location_id}'",
                employee,
            )

    # Gate 3: Shift availability
    shift_query = session.query(WorkforceShift).filter(
        WorkforceShift.employee_id == employee.employee_id
    )
    if at_time is not None:
        shift_query = shift_query.filter(WorkforceShift.observed_at <= at_time)

    shift = shift_query.order_by(WorkforceShift.observed_at.desc()).first()

    if shift is None:
        return (
            False,
            f"No workforce shift record found for employee '{employee.employee_id}'",
            employee,
        )

    if shift.availability_status != "ON_SHIFT":
        return (
            False,
            f"availability_status={shift.availability_status} (must be ON_SHIFT)",
            employee,
        )

    return True, "Active, on-shift, and location/region eligible", employee


def _is_eligible(
    session: Session,
    employee: Employee,
    eligibility_log: list,
    location_id: Optional[str] = None,
) -> bool:
    """
    Applies unified eligibility checks via check_assignable.
    """
    is_ok, reason, _ = check_assignable(session, employee, location_id=location_id)
    if not is_ok:
        eligibility_log.append({
            "employee_id": employee.employee_id,
            "rejected_reason": reason,
        })
        logger.info(f"[ROUTING] {employee.employee_id} REJECTED: {reason}")
        return False

    eligibility_log.append({
        "employee_id": employee.employee_id,
        "selected": True,
        "reason": reason,
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
            if _is_eligible(session, candidate, eligibility_log, location_id=canonical_location_id):
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
