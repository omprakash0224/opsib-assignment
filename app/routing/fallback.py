"""
Routing Fallback: Store-manager fallback logic for the workforce routing engine.

Delegates eligibility verification to check_assignable in policy.py.
"""
from typing import Optional
from sqlalchemy.orm import Session

from app.config import DEFAULT_TENANT_ID
from app.core.logging import logger
from app.db.models.canonical import Employee


def find_fallback_manager(
    session: Session,
    canonical_location_id: str,
    eligibility_log: list,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Optional[str]:
    """
    Searches for an active, ON_SHIFT store_manager at the given location.

    Returns the employee_id of the first eligible manager, or None if no
    eligible fallback exists.

    Eligibility rules are evaluated via check_assignable.
    """
    from app.routing.policy import check_assignable

    managers = (
        session.query(Employee)
        .filter(
            Employee.tenant_id == tenant_id,
            Employee.canonical_location_id == canonical_location_id,
            Employee.role == "store_manager",
        )
        .all()
    )

    for manager in managers:
        is_ok, reason, _ = check_assignable(
            session, manager, location_id=canonical_location_id
        )
        if not is_ok:
            eligibility_log.append({
                "employee_id": manager.employee_id,
                "tier": "fallback_store_manager",
                "rejected_reason": reason,
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
