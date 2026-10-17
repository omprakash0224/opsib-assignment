"""
Routing Fallback: Store-manager fallback logic for the workforce routing engine.

Separated from policy.py to keep the primary routing concern clean and to
allow independent unit testing of the fallback path (PLAN.md §5.1 point 4).
"""
from typing import Optional
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.db.models.canonical import Employee, WorkforceShift


def find_fallback_manager(
    session: Session,
    canonical_location_id: str,
    eligibility_log: list,
) -> Optional[str]:
    """
    Searches for an active, ON_SHIFT store_manager at the given location.

    Returns the employee_id of the first eligible manager, or None if no
    eligible fallback exists.

    Eligibility rules are identical to the primary tier:
      - employment_status == 'ACTIVE'
      - latest WorkforceShift.availability_status == 'ON_SHIFT'

    These rules are re-evaluated directly (not delegated to _is_eligible from
    policy.py) to keep the fallback module self-contained.
    """
    managers = (
        session.query(Employee)
        .filter(
            Employee.canonical_location_id == canonical_location_id,
            Employee.role == "store_manager",
        )
        .all()
    )

    for manager in managers:
        # Gate 1: HRMS status
        if manager.employment_status != "ACTIVE":
            eligibility_log.append({
                "employee_id": manager.employee_id,
                "tier": "fallback_store_manager",
                "rejected_reason": f"employment_status='{manager.employment_status}'",
            })
            continue

        # Gate 2: Shift status
        shift = (
            session.query(WorkforceShift)
            .filter(WorkforceShift.employee_id == manager.employee_id)
            .order_by(WorkforceShift.observed_at.desc())
            .first()
        )
        if shift is None:
            eligibility_log.append({
                "employee_id": manager.employee_id,
                "tier": "fallback_store_manager",
                "rejected_reason": "No shift record found",
            })
            continue

        if shift.availability_status != "ON_SHIFT":
            eligibility_log.append({
                "employee_id": manager.employee_id,
                "tier": "fallback_store_manager",
                "rejected_reason": f"availability_status='{shift.availability_status}'",
            })
            continue

        # Eligible fallback found
        eligibility_log.append({
            "employee_id": manager.employee_id,
            "tier": "fallback_store_manager",
            "selected": True,
            "reason": "Active and ON_SHIFT store_manager (fallback)",
        })
        logger.info(
            f"[ROUTING] Fallback selected: {manager.employee_id} "
            f"({manager.display_name}) at {canonical_location_id}"
        )
        return manager.employee_id

    logger.info(
        f"[ROUTING] No eligible store_manager fallback at {canonical_location_id}."
    )
    return None
