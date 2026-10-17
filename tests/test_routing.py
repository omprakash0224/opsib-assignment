"""
Phase 5 Test Suite – Part 1: Workforce Routing Policy & Fallback (Task C).

Verifies all routing scenarios from PLAN.md §5.1 and ARCHITECTURE.md §3.3:
  HR-101  – ACTIVE but OFF_SHIFT           -> rejected (primary tier)
  HR-103  – INACTIVE despite ON_SHIFT      -> rejected (HRMS status wins)
  HR-102  – ACTIVE and ON_SHIFT            -> selected at STORE-014
  HR-401  – Fallback store_manager         -> selected at STORE-015
  HR-501  – Regional role, no location     -> handled without DB error
  No candidate at all                      -> MANUAL_ASSIGNMENT_REQUIRED
"""
import os
import pytest
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionService
from app.routing.policy import RoutingPolicy, REASON_PRIMARY, REASON_FALLBACK_MANAGER, REASON_MANUAL
from app.routing.fallback import find_fallback_manager
from app.db.models.canonical import Employee, WorkforceShift

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(name: str) -> str:
    with open(os.path.join(WORKSPACE_DIR, name), encoding="utf-8") as f:
        return f.read()


def load_hr_fixtures(db_session: Session) -> None:
    """Load location aliases, employees, and workforce_status fixtures."""
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))
    IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
    IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))


# ---------------------------------------------------------------------------
# Unit Tests: Eligibility Logic
# ---------------------------------------------------------------------------

class TestEligibilityGates:
    """Unit tests for the two-gate eligibility check."""

    def test_inactive_employee_rejected_regardless_of_shift(self, db_session: Session):
        """
        HR-103 is INACTIVE in HRMS but has an ON_SHIFT workforce record.
        HRMS employment_status must win: HR-103 must be rejected.
        """
        load_hr_fixtures(db_session)
        hr103 = db_session.get(Employee, "HR-103")
        assert hr103 is not None
        assert hr103.employment_status == "INACTIVE"

        # Confirm shift record says ON_SHIFT
        shift = db_session.query(WorkforceShift).filter_by(employee_id="HR-103").first()
        assert shift is not None
        assert shift.availability_status == "ON_SHIFT"

        log = []
        from app.routing.policy import _is_eligible
        eligible = _is_eligible(db_session, hr103, log)
        assert not eligible
        assert any("INACTIVE" in entry.get("rejected_reason", "") for entry in log)

    def test_off_shift_active_employee_rejected(self, db_session: Session):
        """HR-101 is ACTIVE but has availability_status == OFF_SHIFT => rejected."""
        load_hr_fixtures(db_session)
        hr101 = db_session.get(Employee, "HR-101")
        assert hr101.employment_status == "ACTIVE"

        shift = db_session.query(WorkforceShift).filter_by(employee_id="HR-101").first()
        assert shift.availability_status == "OFF_SHIFT"

        log = []
        from app.routing.policy import _is_eligible
        eligible = _is_eligible(db_session, hr101, log)
        assert not eligible
        assert any("OFF_SHIFT" in entry.get("rejected_reason", "") for entry in log)

    def test_active_on_shift_employee_eligible(self, db_session: Session):
        """HR-102 is ACTIVE and ON_SHIFT => must be eligible."""
        load_hr_fixtures(db_session)
        hr102 = db_session.get(Employee, "HR-102")
        assert hr102.employment_status == "ACTIVE"

        shift = db_session.query(WorkforceShift).filter_by(employee_id="HR-102").first()
        assert shift.availability_status == "ON_SHIFT"

        log = []
        from app.routing.policy import _is_eligible
        eligible = _is_eligible(db_session, hr102, log)
        assert eligible


# ---------------------------------------------------------------------------
# Integration Tests: RoutingPolicy.route()
# ---------------------------------------------------------------------------

class TestRoutingPolicy:
    """Integration tests running the full three-tier routing engine."""

    def test_store014_primary_selected_hr102(self, db_session: Session):
        """
        STORE-014 has 3 receiving associates: HR-101 (OFF_SHIFT), HR-103 (INACTIVE), HR-102 (eligible).
        Expected: HR-102 selected as PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE.
        """
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-014")

        assert decision.assignee_id == "HR-102", (
            "HR-102 (Dev Shah) is the only ACTIVE and ON_SHIFT receiving associate at STORE-014"
        )
        assert decision.assignment_reason == REASON_PRIMARY

    def test_hr101_never_assigned_to_store014(self, db_session: Session):
        """HR-101 is OFF_SHIFT; must never be the selected assignee."""
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-014")
        assert decision.assignee_id != "HR-101"

    def test_hr103_never_assigned_inactive_overrides_shift(self, db_session: Session):
        """HR-103 has ON_SHIFT record but INACTIVE HRMS status; must never be assigned."""
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-014")
        assert decision.assignee_id != "HR-103"

    def test_store015_no_receiving_associate_falls_back_to_hr401(self, db_session: Session):
        """
        STORE-015 has no receiving associates.
        Fallback: HR-401 (ACTIVE, ON_SHIFT, store_manager) must be selected.
        """
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-015")

        assert decision.assignee_id == "HR-401", (
            "HR-401 (Rohan Iyer) is the active, on-shift store_manager at STORE-015 "
            "and must be selected as the fallback assignee."
        )
        assert decision.assignment_reason == REASON_FALLBACK_MANAGER

    def test_no_candidates_returns_manual_assignment_required(self, db_session: Session):
        """A location with no employees at all returns MANUAL_ASSIGNMENT_REQUIRED."""
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-NONEXISTENT")

        assert decision.assignee_id is None
        assert decision.assignment_reason == REASON_MANUAL

    def test_eligibility_log_records_all_candidates(self, db_session: Session):
        """The eligibility_log must record an entry for every evaluated candidate."""
        load_hr_fixtures(db_session)
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-014")

        # HR-101, HR-102, HR-103 should all appear in the log
        logged_ids = {e.get("employee_id") for e in decision.eligibility_log if "employee_id" in e}
        assert "HR-101" in logged_ids, "HR-101 should be logged (rejected: OFF_SHIFT)"
        assert "HR-102" in logged_ids, "HR-102 should be logged (selected)"
        assert "HR-103" in logged_ids, "HR-103 should be logged (rejected: INACTIVE)"

    def test_regional_role_hr501_has_null_location_no_db_error(self, db_session: Session):
        """
        HR-501 has role=regional_operations and canonical_location_id=NULL.
        Routing at any store location must not raise an error or accidentally select HR-501.
        """
        load_hr_fixtures(db_session)
        hr501 = db_session.get(Employee, "HR-501")
        assert hr501 is not None
        assert hr501.canonical_location_id is None  # regional role has no store

        # Routing at STORE-014 must succeed without touching HR-501
        decision = RoutingPolicy.route(db_session, canonical_location_id="STORE-014")
        assert decision.assignee_id != "HR-501"


# ---------------------------------------------------------------------------
# Fallback Module Tests
# ---------------------------------------------------------------------------

class TestFallbackModule:
    """Unit tests for the fallback store_manager finder."""

    def test_fallback_finds_hr401_at_store015(self, db_session: Session):
        """find_fallback_manager returns HR-401 at STORE-015."""
        load_hr_fixtures(db_session)
        log = []
        result = find_fallback_manager(db_session, "STORE-015", log)
        assert result == "HR-401"
        assert any(e.get("selected") for e in log)

    def test_fallback_returns_none_for_unknown_location(self, db_session: Session):
        """No manager at an unknown location returns None."""
        load_hr_fixtures(db_session)
        log = []
        result = find_fallback_manager(db_session, "STORE-UNKNOWN", log)
        assert result is None

    def test_fallback_rejects_inactive_manager(self, db_session: Session):
        """An inactive store_manager at the target location must not be returned."""
        load_hr_fixtures(db_session)
        # Make HR-401 inactive for this test
        hr401 = db_session.get(Employee, "HR-401")
        hr401.employment_status = "INACTIVE"
        db_session.flush()

        log = []
        result = find_fallback_manager(db_session, "STORE-015", log)
        assert result is None
        assert any("INACTIVE" in e.get("rejected_reason", "") for e in log)
