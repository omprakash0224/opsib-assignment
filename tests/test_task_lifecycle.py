"""
Phase 5 Test Suite – Part 2: Task Lifecycle, State Machine & Idempotency (Task C).

Verifies:
  - Full sequential lifecycle: CREATED -> ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS
    -> COMPLETED -> PENDING_VERIFICATION -> VERIFIED
  - Invalid transitions raise InvalidStateTransitionError / HTTP 409
  - Idempotent completion retry: repeated POST /complete returns 200 OK
  - Reassignment from ASSIGNED, ACKNOWLEDGED, IN_PROGRESS
  - Audit trail integrity (every transition has an event)
  - Task creation idempotency (same exception creates only one task)
  - Exception is marked RESOLVED when task is VERIFIED
  - REST API endpoints for all lifecycle operations
"""
import os
import pytest
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionService
from app.detection.service import DetectionService
from app.workflows import task_service
from app.workflows.state_machine import (
    assert_transition,
    assert_reassignable,
    is_idempotent_completion,
    ALLOWED_TRANSITIONS,
)
from app.core.exceptions import InvalidStateTransitionError, IdempotencyConflictError
from app.db.models.exception import OperationalException
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(name: str) -> str:
    with open(os.path.join(WORKSPACE_DIR, name), encoding="utf-8") as f:
        return f.read()


def load_all_fixtures(db_session: Session) -> None:
    for ds in [
        "product_aliases", "location_aliases", "employees",
        "workforce_status", "purchase_order_lines", "goods_receipt_lines",
    ]:
        IngestionService.ingest_csv(db_session, ds, read_fixture(f"{ds}.csv"))


def run_detection_and_get_exception(db_session: Session, po_number: str) -> OperationalException:
    """Helper: run reconciliation and return the exception for the given PO."""
    DetectionService.run_reconciliation(session=db_session)
    exc = db_session.query(OperationalException).filter_by(po_number=po_number).first()
    assert exc is not None, f"No exception found for {po_number}"
    return exc


# ---------------------------------------------------------------------------
# Unit Tests: State Machine Rules
# ---------------------------------------------------------------------------

class TestStateMachine:
    """Unit tests for the state machine transition table and guards."""

    def test_allowed_transitions_table_completeness(self):
        """Every valid state must appear as a key in the transition table."""
        all_states = {
            "CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS",
            "BLOCKED", "COMPLETED", "PENDING_VERIFICATION", "VERIFIED", "REOPENED",
        }
        assert all_states == set(ALLOWED_TRANSITIONS.keys())

    def test_verified_is_terminal_no_transitions(self):
        """VERIFIED is a terminal state; no outgoing transitions allowed."""
        assert ALLOWED_TRANSITIONS["VERIFIED"] == []

    def test_legal_transition_does_not_raise(self):
        """assert_transition must not raise for a permitted pair."""
        assert_transition("CREATED", "ASSIGNED")          # no exception
        assert_transition("ASSIGNED", "ACKNOWLEDGED")
        assert_transition("ACKNOWLEDGED", "IN_PROGRESS")
        assert_transition("IN_PROGRESS", "COMPLETED")
        assert_transition("COMPLETED", "PENDING_VERIFICATION")
        assert_transition("PENDING_VERIFICATION", "VERIFIED")

    def test_illegal_transition_raises(self):
        """Illegal transitions must raise InvalidStateTransitionError."""
        with pytest.raises(InvalidStateTransitionError):
            assert_transition("CREATED", "COMPLETED")   # Jump across states

        with pytest.raises(InvalidStateTransitionError):
            assert_transition("VERIFIED", "CREATED")    # Terminal -> beginning

        with pytest.raises(InvalidStateTransitionError):
            assert_transition("CREATED", "VERIFIED")

        with pytest.raises(InvalidStateTransitionError):
            assert_transition("ASSIGNED", "COMPLETED")

    def test_reassign_allowed_from_correct_states(self):
        """Reassignment to ASSIGNED must be permitted from ASSIGNED, ACKNOWLEDGED, IN_PROGRESS."""
        assert_reassignable("ASSIGNED")
        assert_reassignable("ACKNOWLEDGED")
        assert_reassignable("IN_PROGRESS")

    def test_reassign_rejected_from_completed(self):
        """Reassignment from COMPLETED must raise InvalidStateTransitionError."""
        with pytest.raises(InvalidStateTransitionError):
            assert_reassignable("COMPLETED")

    def test_reassign_rejected_from_verified(self):
        with pytest.raises(InvalidStateTransitionError):
            assert_reassignable("VERIFIED")

    def test_idempotent_completion_same_payload(self):
        """Identical resolution payload on already-COMPLETED task => idempotent."""
        assert is_idempotent_completion(
            task_status="COMPLETED",
            stored_resolution_code="CODE-A",
            stored_evidence_ref="REF-001",
            incoming_resolution_code="CODE-A",
            incoming_evidence_ref="REF-001",
        ) is True

    def test_idempotent_completion_different_code(self):
        """Different resolution_code on already-COMPLETED task => NOT idempotent."""
        assert is_idempotent_completion(
            task_status="COMPLETED",
            stored_resolution_code="CODE-A",
            stored_evidence_ref="REF-001",
            incoming_resolution_code="CODE-B",
            incoming_evidence_ref="REF-001",
        ) is False

    def test_idempotent_completion_not_completed_status(self):
        """Task not in COMPLETED status => always returns False."""
        assert is_idempotent_completion(
            task_status="IN_PROGRESS",
            stored_resolution_code=None,
            stored_evidence_ref=None,
            incoming_resolution_code="CODE-A",
            incoming_evidence_ref="REF-001",
        ) is False


# ---------------------------------------------------------------------------
# Integration Tests: Full Lifecycle
# ---------------------------------------------------------------------------

class TestTaskLifecycle:
    """
    Integration tests covering the complete task lifecycle:
      CREATED/ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS -> COMPLETED
      -> PENDING_VERIFICATION -> VERIFIED
    """

    def test_task_created_for_shortfall_exception(self, db_session: Session):
        """Running reconciliation creates a task for each shortfall exception."""
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")

        task = task_service.create_task_for_exception(db_session, exc)
        assert task is not None
        assert task.exception_id == exc.id
        assert task.status in ("ASSIGNED", "CREATED")

    def test_task_creation_idempotent(self, db_session: Session):
        """Creating a task twice for the same exception produces exactly one task."""
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")

        t1 = task_service.create_task_for_exception(db_session, exc)
        t2 = task_service.create_task_for_exception(db_session, exc)

        assert t1.id == t2.id, "Same task must be returned on duplicate creation attempt"
        total = db_session.query(HumanTask).filter_by(exception_id=exc.id).count()
        assert total == 1

    def test_po501_task_assigned_to_hr102_at_store014(self, db_session: Session):
        """
        PO-501 exception is at STORE-014.
        Expected assignee: HR-102 (Active, ON_SHIFT receiving associate).
        """
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")
        task = task_service.create_task_for_exception(db_session, exc)

        assert task.assignee_id == "HR-102"
        assert task.status == "ASSIGNED"
        assert "PRIMARY" in task.assignment_reason

    def test_po505_task_assigned_to_hr401_fallback_at_store015(self, db_session: Session):
        """
        PO-505 exception is at STORE-015 (no receiving associate).
        Expected assignee: HR-401 (fallback store_manager).
        """
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-505")
        task = task_service.create_task_for_exception(db_session, exc)

        assert task.assignee_id == "HR-401"
        assert task.status == "ASSIGNED"
        assert "FALLBACK" in task.assignment_reason

    def test_full_sequential_lifecycle(self, db_session: Session):
        """
        Verify the complete lifecycle:
        ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS -> COMPLETED
        -> PENDING_VERIFICATION -> VERIFIED
        with correct status at each step and audit events written.
        """
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")
        task = task_service.create_task_for_exception(db_session, exc)
        task_id = task.id

        # ASSIGNED -> ACKNOWLEDGED
        t = task_service.acknowledge_task(db_session, task_id, actor_id="HR-102")
        assert t.status == "ACKNOWLEDGED"

        # ACKNOWLEDGED -> IN_PROGRESS
        t = task_service.start_task(db_session, task_id, actor_id="HR-102")
        assert t.status == "IN_PROGRESS"

        # IN_PROGRESS -> COMPLETED
        t = task_service.complete_task(
            db_session, task_id,
            actor_id="HR-102",
            resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            resolution_evidence_ref="REF-8842",
        )
        assert t.status == "COMPLETED"
        assert t.resolution_code == "SUPPLIER_SHORT_SHIPMENT_CONFIRMED"

        # COMPLETED -> PENDING_VERIFICATION
        t = task_service.submit_for_verification(db_session, task_id, actor_id="HR-102")
        assert t.status == "PENDING_VERIFICATION"

        # PENDING_VERIFICATION -> VERIFIED
        t = task_service.verify_task(db_session, task_id, actor_id="HR-201", notes="Verified by manager")
        assert t.status == "VERIFIED"

        # Exception should now be RESOLVED
        db_session.expire(exc)
        exc_refreshed = db_session.get(OperationalException, exc.id)
        assert exc_refreshed.status == "RESOLVED"

    def test_audit_trail_captures_all_transitions(self, db_session: Session):
        """Every lifecycle step must produce a corresponding TaskAuditEvent."""
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")
        task = task_service.create_task_for_exception(db_session, exc)
        task_id = task.id

        task_service.acknowledge_task(db_session, task_id, actor_id="HR-102")
        task_service.start_task(db_session, task_id, actor_id="HR-102")
        task_service.complete_task(
            db_session, task_id, actor_id="HR-102",
            resolution_code="GOODS_FOUND_IN_TRANSIT", resolution_evidence_ref="REF-XYZ",
        )

        events = (
            db_session.query(TaskAuditEvent)
            .filter_by(task_id=task_id)
            .order_by(TaskAuditEvent.timestamp)
            .all()
        )
        event_types = [e.event_type for e in events]

        # At minimum: ASSIGNED (from routing), ACKNOWLEDGED, STATUS_CHANGE (IN_PROGRESS), COMPLETED
        assert "ASSIGNED" in event_types or "CREATED" in event_types
        assert "ACKNOWLEDGED" in event_types
        assert "COMPLETED" in event_types

        # Every event must record a to_status
        for e in events:
            assert e.to_status is not None

    def test_reopen_lifecycle(self, db_session: Session):
        """PENDING_VERIFICATION -> REOPENED -> IN_PROGRESS is a valid rework path."""
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-506")
        task = task_service.create_task_for_exception(db_session, exc)
        task_id = task.id

        task_service.acknowledge_task(db_session, task_id, "HR-102")
        task_service.start_task(db_session, task_id, "HR-102")
        task_service.complete_task(
            db_session, task_id, "HR-102",
            resolution_code="INVESTIGATION_PENDING", resolution_evidence_ref=None,
        )
        task_service.submit_for_verification(db_session, task_id, "HR-102")

        # Manager reopens due to inadequate evidence
        t = task_service.reopen_task(db_session, task_id, "HR-201", reason="Evidence insufficient")
        assert t.status == "REOPENED"

        # Now can move back to IN_PROGRESS
        t = task_service.start_task(db_session, task_id, "HR-102")
        assert t.status == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# Illegal Transition Tests
# ---------------------------------------------------------------------------

class TestIllegalTransitions:
    """Verify that illegal state machine jumps are rejected at the service level."""

    def _setup_task(self, db_session: Session) -> HumanTask:
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")
        return task_service.create_task_for_exception(db_session, exc)

    def test_direct_created_to_completed_rejected(self, db_session: Session):
        """ASSIGNED -> COMPLETED (skipping intermediate states) must be rejected."""
        task = self._setup_task(db_session)
        with pytest.raises(InvalidStateTransitionError):
            task_service.complete_task(
                db_session, task.id, "HR-102",
                resolution_code="CODE", resolution_evidence_ref="REF",
            )

    def test_verified_to_any_state_rejected(self, db_session: Session):
        """No transition is valid from VERIFIED (terminal state)."""
        task = self._setup_task(db_session)
        tid = task.id
        task_service.acknowledge_task(db_session, tid, "HR-102")
        task_service.start_task(db_session, tid, "HR-102")
        task_service.complete_task(db_session, tid, "HR-102", "CODE", "REF")
        task_service.submit_for_verification(db_session, tid, "HR-102")
        task_service.verify_task(db_session, tid, "HR-201")

        with pytest.raises(InvalidStateTransitionError):
            task_service.acknowledge_task(db_session, tid, "HR-102")

    def test_reassign_from_completed_rejected(self, db_session: Session):
        """Reassignment from COMPLETED must be rejected."""
        task = self._setup_task(db_session)
        tid = task.id
        task_service.acknowledge_task(db_session, tid, "HR-102")
        task_service.start_task(db_session, tid, "HR-102")
        task_service.complete_task(db_session, tid, "HR-102", "CODE", "REF")

        with pytest.raises(InvalidStateTransitionError):
            task_service.reassign_task(db_session, tid, "HR-201", "HR-401", "test reason")

    def test_reassign_from_assigned(self, db_session: Session):
        """Reassignment from ASSIGNED is permitted; task moves to ASSIGNED with new assignee."""
        task = self._setup_task(db_session)
        t = task_service.reassign_task(db_session, task.id, "HR-201", "HR-201", "Manager override")
        assert t.status == "ASSIGNED"
        assert t.assignee_id == "HR-201"

    def test_reassign_from_in_progress(self, db_session: Session):
        """Reassignment from IN_PROGRESS is permitted."""
        task = self._setup_task(db_session)
        task_service.acknowledge_task(db_session, task.id, "HR-102")
        task_service.start_task(db_session, task.id, "HR-102")
        t = task_service.reassign_task(db_session, task.id, "HR-201", "HR-201", "Shift change")
        assert t.status == "ASSIGNED"
        assert t.assignee_id == "HR-201"


# ---------------------------------------------------------------------------
# Idempotency Tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Verify idempotent retry behavior on completion (ADR-5)."""

    def _task_in_completed(self, db_session: Session) -> HumanTask:
        load_all_fixtures(db_session)
        exc = run_detection_and_get_exception(db_session, "PO-501")
        task = task_service.create_task_for_exception(db_session, exc)
        task_service.acknowledge_task(db_session, task.id, "HR-102")
        task_service.start_task(db_session, task.id, "HR-102")
        task_service.complete_task(
            db_session, task.id, "HR-102",
            resolution_code="CONFIRMED", resolution_evidence_ref="REF-001",
        )
        return task

    def test_identical_completion_retry_returns_same_task(self, db_session: Session):
        """
        Client retries POST /complete with identical payload after a timeout.
        Server must return the existing COMPLETED task (200 OK) without error.
        """
        task = self._task_in_completed(db_session)

        # Retry with identical payload
        result = task_service.complete_task(
            db_session, task.id, "HR-102",
            resolution_code="CONFIRMED", resolution_evidence_ref="REF-001",
        )
        assert result.id == task.id
        assert result.status == "COMPLETED"

        # Audit must not have a duplicate COMPLETED event
        events = db_session.query(TaskAuditEvent).filter_by(task_id=task.id).all()
        completed_events = [e for e in events if e.event_type == "COMPLETED"]
        assert len(completed_events) == 1, (
            "Idempotent retry must not write a second COMPLETED audit event"
        )

    def test_conflicting_completion_raises_conflict_error(self, db_session: Session):
        """
        Client retries POST /complete with DIFFERENT resolution_code.
        Server must raise IdempotencyConflictError (mapped to 409 Conflict by API layer).
        """
        task = self._task_in_completed(db_session)

        with pytest.raises(IdempotencyConflictError):
            task_service.complete_task(
                db_session, task.id, "HR-102",
                resolution_code="DIFFERENT_CODE", resolution_evidence_ref="REF-001",
            )


# ---------------------------------------------------------------------------
# REST API Endpoint Tests (Task Lifecycle via TestClient)
# ---------------------------------------------------------------------------

class TestTaskAPI:
    """Tests for the task lifecycle REST API endpoints."""

    def _setup_client_with_fixtures(self, client):
        """Import all fixtures and run reconciliation via the API."""
        for ds in [
            "product_aliases", "location_aliases", "employees",
            "workforce_status", "purchase_order_lines", "goods_receipt_lines",
        ]:
            resp = client.post(
                f"/api/v1/imports/{ds}",
                content=open(os.path.join(WORKSPACE_DIR, f"{ds}.csv"), encoding="utf-8").read(),
                headers={"Content-Type": "text/csv"},
            )
            assert resp.status_code == 201, f"Import {ds} failed: {resp.text}"
        recon = client.post("/api/v1/reconciliation/run")
        assert recon.status_code == 200
        return recon.json()

    def test_api_reconciliation_creates_tasks(self, client):
        """POST /reconciliation/run must auto-create tasks for each exception."""
        self._setup_client_with_fixtures(client)

        tasks_resp = client.get("/api/v1/tasks")
        assert tasks_resp.status_code == 200
        data = tasks_resp.json()
        assert data["total"] == 3  # 3 shortfall exceptions -> 3 tasks

    def test_api_full_lifecycle_via_endpoints(self, client):
        """Exercise the full HTTP lifecycle for a task."""
        self._setup_client_with_fixtures(client)

        # Get task for PO-501
        tasks = client.get("/api/v1/tasks").json()["items"]
        task = next(t for t in tasks if t["assignee_id"] == "HR-102")
        tid = task["id"]

        # ASSIGNED -> ACKNOWLEDGED
        r = client.post(f"/api/v1/tasks/{tid}/acknowledge", json={"actor_id": "HR-102"})
        assert r.status_code == 200
        assert r.json()["status"] == "ACKNOWLEDGED"

        # ACKNOWLEDGED -> IN_PROGRESS
        r = client.post(f"/api/v1/tasks/{tid}/start", json={"actor_id": "HR-102"})
        assert r.status_code == 200
        assert r.json()["status"] == "IN_PROGRESS"

        # IN_PROGRESS -> COMPLETED
        r = client.post(f"/api/v1/tasks/{tid}/complete", json={
            "actor_id": "HR-102",
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "INV-2024-09-07",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "COMPLETED"

        # COMPLETED -> PENDING_VERIFICATION
        r = client.post(f"/api/v1/tasks/{tid}/submit-verification", json={"actor_id": "HR-102"})
        assert r.status_code == 200
        assert r.json()["status"] == "PENDING_VERIFICATION"

        # PENDING_VERIFICATION -> VERIFIED
        r = client.post(f"/api/v1/tasks/{tid}/verify", json={
            "actor_id": "HR-201",
            "notes": "Confirmed via supplier invoice audit",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "VERIFIED"

    def test_api_illegal_transition_returns_409(self, client):
        """Attempting an illegal state jump via the API must return HTTP 409."""
        self._setup_client_with_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        tid = tasks[0]["id"]

        # Task is ASSIGNED; jump directly to COMPLETED should fail
        r = client.post(f"/api/v1/tasks/{tid}/complete", json={
            "actor_id": "HR-102",
            "resolution_code": "JUMP",
            "resolution_evidence_ref": None,
        })
        assert r.status_code == 409

    def test_api_idempotent_completion_retry_returns_200(self, client):
        """Repeated POST /complete with identical payload returns 200 OK."""
        self._setup_client_with_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task = next(t for t in tasks if t["assignee_id"] == "HR-102")
        tid = task["id"]

        client.post(f"/api/v1/tasks/{tid}/acknowledge", json={"actor_id": "HR-102"})
        client.post(f"/api/v1/tasks/{tid}/start", json={"actor_id": "HR-102"})

        payload = {"actor_id": "HR-102", "resolution_code": "X", "resolution_evidence_ref": "Y"}
        r1 = client.post(f"/api/v1/tasks/{tid}/complete", json=payload)
        assert r1.status_code == 200

        # Retry with same payload
        r2 = client.post(f"/api/v1/tasks/{tid}/complete", json=payload)
        assert r2.status_code == 200
        assert r2.json()["status"] == "COMPLETED"

    def test_api_reassign_task(self, client):
        """POST /tasks/{id}/reassign changes the assignee and records audit event."""
        self._setup_client_with_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task = next(t for t in tasks if t["assignee_id"] == "HR-102")
        tid = task["id"]

        r = client.post(f"/api/v1/tasks/{tid}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-201",
            "reason": "HR-102 is now unavailable",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["assignee_id"] == "HR-201"
        assert data["status"] == "ASSIGNED"

    def test_api_task_detail_includes_audit_events(self, client):
        """GET /tasks/{id} must include full audit_events history."""
        self._setup_client_with_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        tid = tasks[0]["id"]

        client.post(f"/api/v1/tasks/{tid}/acknowledge", json={"actor_id": "HR-102"})

        detail = client.get(f"/api/v1/tasks/{tid}").json()
        assert "audit_events" in detail
        assert len(detail["audit_events"]) >= 2  # ASSIGNED (creation) + ACKNOWLEDGED

    def test_api_filter_tasks_by_assignee(self, client):
        """GET /tasks?assignee_id=HR-102 filters by assignee correctly."""
        self._setup_client_with_fixtures(client)
        r = client.get("/api/v1/tasks?assignee_id=HR-102")
        assert r.status_code == 200
        data = r.json()
        for t in data["items"]:
            assert t["assignee_id"] == "HR-102"

    def test_api_filter_tasks_by_status(self, client):
        """GET /tasks?status=ASSIGNED returns only ASSIGNED tasks."""
        self._setup_client_with_fixtures(client)
        r = client.get("/api/v1/tasks?status=ASSIGNED")
        assert r.status_code == 200
        for t in r.json()["items"]:
            assert t["status"] == "ASSIGNED"

    def test_api_get_task_not_found(self, client):
        """GET /tasks/{unknown_id} must return 404."""
        r = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Bug 1 Tests: Exception Resolution Persistence and Atomicity
# ---------------------------------------------------------------------------

class TestBug1VerifyExceptionResolutionPersistence:
    """
    Bug 1 Verification:
    - Fresh-session API test: complete full lifecycle via HTTP, then GET /exceptions/{id}
      in a fresh session and assert status == RESOLVED and resolved_at is set.
    - Atomicity test: force _resolve_exception to raise, and assert the task is still
      PENDING_VERIFICATION (nothing half-applied, no audit event committed).
    """

    def _setup_fresh_fixtures(self, fresh_client):
        for ds in [
            "product_aliases", "location_aliases", "employees",
            "workforce_status", "purchase_order_lines", "goods_receipt_lines",
        ]:
            resp = fresh_client.post(
                f"/api/v1/imports/{ds}",
                content=open(os.path.join(WORKSPACE_DIR, f"{ds}.csv"), encoding="utf-8").read(),
                headers={"Content-Type": "text/csv"},
            )
            assert resp.status_code == 201
        recon = fresh_client.post("/api/v1/reconciliation/run")
        assert recon.status_code == 200

    def test_fresh_session_api_verify_persists_exception_resolution(self, fresh_client):
        """
        Verify must persist exception resolution across fresh DB sessions.
        Full HTTP lifecycle completed, then a fresh GET /exceptions/{id} request
        must return RESOLVED and resolved_at set.
        """
        self._setup_fresh_fixtures(fresh_client)

        # Get task for PO-501
        tasks = fresh_client.get("/api/v1/tasks").json()["items"]
        task = next(t for t in tasks if t["assignee_id"] == "HR-102")
        tid = task["id"]
        exception_id = task["exception_id"]

        # Initial check on exception in a fresh session: should be DETECTED
        init_exc = fresh_client.get(f"/api/v1/exceptions/{exception_id}").json()
        assert init_exc["status"] == "DETECTED"
        assert init_exc["resolved_at"] is None

        # Advance through lifecycle: ASSIGNED -> ACK -> IN_PROGRESS -> COMPLETED -> PENDING_VERIFICATION -> VERIFIED
        assert fresh_client.post(f"/api/v1/tasks/{tid}/acknowledge", json={"actor_id": "HR-102"}).status_code == 200
        assert fresh_client.post(f"/api/v1/tasks/{tid}/start", json={"actor_id": "HR-102"}).status_code == 200
        assert fresh_client.post(f"/api/v1/tasks/{tid}/complete", json={
            "actor_id": "HR-102",
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "REF-PO501",
        }).status_code == 200
        assert fresh_client.post(f"/api/v1/tasks/{tid}/submit-verification", json={"actor_id": "HR-102"}).status_code == 200
        verify_resp = fresh_client.post(f"/api/v1/tasks/{tid}/verify", json={
            "actor_id": "HR-201",
            "notes": "Verified by manager",
        })
        assert verify_resp.status_code == 200
        assert verify_resp.json()["status"] == "VERIFIED"

        # Fresh request to GET /exceptions/{id}
        exc_resp = fresh_client.get(f"/api/v1/exceptions/{exception_id}")
        assert exc_resp.status_code == 200
        exc_data = exc_resp.json()
        assert exc_data["status"] == "RESOLVED"
        assert exc_data["resolved_at"] is not None

    def test_verify_atomicity_failure_does_not_half_apply(self, fresh_db_engine, monkeypatch):
        """
        Atomicity test: force _resolve_exception to raise, and assert the task is still
        PENDING_VERIFICATION (nothing half-applied, task and exception cannot disagree).
        """
        from sqlalchemy.orm import sessionmaker
        SessionTest = sessionmaker(bind=fresh_db_engine, expire_on_commit=False)
        db = SessionTest()
        try:
            load_all_fixtures(db)
            exc = run_detection_and_get_exception(db, "PO-501")
            task = task_service.create_task_for_exception(db, exc)
            tid = task.id

            # Advance to PENDING_VERIFICATION
            task_service.acknowledge_task(db, tid, actor_id="HR-102")
            task_service.start_task(db, tid, actor_id="HR-102")
            task_service.complete_task(
                db, tid, actor_id="HR-102",
                resolution_code="CODE-1", resolution_evidence_ref="REF-1",
            )
            task_service.submit_for_verification(db, tid, actor_id="HR-102")

            # Confirm task is PENDING_VERIFICATION before verify
            task_before = db.get(HumanTask, tid)
            assert task_before.status == "PENDING_VERIFICATION"

            # Mock _resolve_exception to fail
            def mock_resolve_exception_failure(session, exception_id, actor_id):
                raise RuntimeError("Simulated failure during exception resolution")

            monkeypatch.setattr(task_service, "_resolve_exception", mock_resolve_exception_failure)

            # Call verify_task and expect RuntimeError
            with pytest.raises(RuntimeError, match="Simulated failure"):
                task_service.verify_task(db, tid, actor_id="HR-201")

            # Verify atomicity in a fresh session: Task must NOT be VERIFIED! It must remain PENDING_VERIFICATION!
            fresh_session = SessionTest()
            try:
                task_after = fresh_session.get(HumanTask, tid)
                assert task_after is not None
                assert task_after.status == "PENDING_VERIFICATION", (
                    f"Expected task status to remain PENDING_VERIFICATION on exception failure, but got {task_after.status}"
                )

                # No VERIFIED audit event should have been committed
                events = fresh_session.query(TaskAuditEvent).filter_by(task_id=tid).all()
                event_types = [e.event_type for e in events]
                assert "VERIFIED" not in event_types, "VERIFIED audit event must not be committed on failure"

                # OperationalException must remain DETECTED
                exc_after = fresh_session.get(OperationalException, exc.id)
                assert exc_after.status == "DETECTED"
                assert exc_after.resolved_at is None
            finally:
                fresh_session.close()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Bug 3 Tests: Assignment Validation and Manual-Queue Assignment
# ---------------------------------------------------------------------------

class TestBug3AssignmentValidationAndManualQueue:
    """
    Bug 3 Verification:
    - Reassign to inactive HR-103 returns 422.
    - Reassign to off-shift HR-101 returns 422.
    - Reassign to unknown ID returns 404 or 422 (never 500).
    - Reassign to wrong-location employee returns 422.
    - Manual-queue flow: STORE-015 with no eligible person creates CREATED task,
      assigned via POST /tasks/{id}/assign, reaches ASSIGNED with audit event.
    - Reassign from CREATED is allowed and treated as an assign.
    - Audit history preserves previous assignee after reassign.
    """

    def _setup_fixtures(self, client):
        for ds in [
            "product_aliases", "location_aliases", "employees",
            "workforce_status", "purchase_order_lines", "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{ds}",
                content=open(os.path.join(WORKSPACE_DIR, f"{ds}.csv"), encoding="utf-8").read(),
                headers={"Content-Type": "text/csv"},
            )
        client.post("/api/v1/reconciliation/run")

    def test_reassign_to_inactive_employee_rejected_422(self, client):
        """Reassigning to inactive HR-103 must be rejected with 422 Unprocessable Entity."""
        self._setup_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store14 = next(t for t in tasks if t["canonical_location_id"] == "STORE-014")

        r = client.post(f"/api/v1/tasks/{task_store14['id']}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-103",
            "reason": "Test reassignment to inactive",
        })
        assert r.status_code == 422
        assert "INACTIVE" in r.text or "ACTIVE" in r.text

    def test_reassign_to_off_shift_employee_rejected_422(self, client):
        """Reassigning to active but off-shift HR-101 must be rejected with 422."""
        self._setup_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store14 = next(t for t in tasks if t["canonical_location_id"] == "STORE-014")

        r = client.post(f"/api/v1/tasks/{task_store14['id']}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-101",
            "reason": "Test reassignment to off shift",
        })
        assert r.status_code == 422
        assert "OFF_SHIFT" in r.text or "ON_SHIFT" in r.text or "shift" in r.text.lower()

    def test_reassign_to_unknown_employee_returns_404_or_422_never_500(self, client):
        """Reassigning to non-existent employee ID must return 404 or 422, never an unhandled 500."""
        self._setup_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        tid = tasks[0]["id"]

        r = client.post(f"/api/v1/tasks/{tid}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-NONEXISTENT-999",
            "reason": "Test reassignment to non-existent employee",
        })
        assert r.status_code in (404, 422)
        assert r.status_code != 500

    def test_reassign_to_wrong_location_employee_rejected_422(self, client):
        """Reassigning a STORE-014 task to STORE-015 manager HR-401 must be rejected with 422."""
        self._setup_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store14 = next(t for t in tasks if t["canonical_location_id"] == "STORE-014")

        r = client.post(f"/api/v1/tasks/{task_store14['id']}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-401",  # Store manager at STORE-015
            "reason": "Wrong location reassignment",
        })
        assert r.status_code == 422
        assert "location" in r.text.lower() or "STORE" in r.text

    def test_manual_queue_assign_flow(self, client, fresh_db_engine):
        """
        Manual-queue flow:
        - When STORE-015 has no eligible person, task is queued as CREATED / MANUAL_ASSIGNMENT_REQUIRED.
        - Calling POST /tasks/{id}/assign assigns the task (CREATED -> ASSIGNED).
        - Audit trail records ASSIGNED event with actor, reason, and assignee.
        """
        # Load aliases
        client.post(
            "/api/v1/imports/product_aliases",
            content=read_fixture("product_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/location_aliases",
            content=read_fixture("location_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )
        # Load employees but make HR-401 inactive so STORE-015 has no eligible candidates
        emp_csv = read_fixture("employees.csv").replace("HR-401,Rohan Iyer,ACTIVE,", "HR-401,Rohan Iyer,INACTIVE,")
        client.post(
            "/api/v1/imports/employees",
            content=emp_csv,
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/workforce_status",
            content=read_fixture("workforce_status.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/purchase_order_lines",
            content=read_fixture("purchase_order_lines.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/goods_receipt_lines",
            content=read_fixture("goods_receipt_lines.csv"),
            headers={"Content-Type": "text/csv"},
        )

        # Run reconciliation
        client.post("/api/v1/reconciliation/run")

        # PO-505 is at STORE-015 -> task must be CREATED with MANUAL_ASSIGNMENT_REQUIRED
        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store15 = next(t for t in tasks if t["canonical_location_id"] == "STORE-015")
        assert task_store15["status"] == "CREATED"
        assert task_store15["assignee_id"] is None
        assert "MANUAL_ASSIGNMENT_REQUIRED" in task_store15["assignment_reason"]
        tid = task_store15["id"]

        # Assign via POST /tasks/{id}/assign to regional operations HR-501 (REGION-MH covers STORE-015)
        assign_resp = client.post(f"/api/v1/tasks/{tid}/assign", json={
            "actor_id": "HR-201",
            "assignee_id": "HR-501",
            "reason": "Supervisor manual allocation to regional specialist",
        })
        assert assign_resp.status_code == 200
        assigned_data = assign_resp.json()
        assert assigned_data["status"] == "ASSIGNED"
        assert assigned_data["assignee_id"] == "HR-501"

        # Check audit trail
        detail = client.get(f"/api/v1/tasks/{tid}").json()
        events = detail["audit_events"]
        assert len(events) >= 2
        assign_event = events[-1]
        assert assign_event["event_type"] == "ASSIGNED"
        assert assign_event["from_status"] == "CREATED"
        assert assign_event["to_status"] == "ASSIGNED"
        assert assign_event["actor_id"] == "HR-201"
        assert "Supervisor manual allocation" in assign_event["reason"]

    def test_audit_history_preserves_previous_assignee(self, client):
        """Audit trail preserves previous assignee after reassignment."""
        self._setup_fixtures(client)
        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store14 = next(t for t in tasks if t["canonical_location_id"] == "STORE-014")
        tid = task_store14["id"]
        assert task_store14["assignee_id"] == "HR-102"

        # Reassign to store manager HR-201 (active, on-shift at STORE-014)
        r = client.post(f"/api/v1/tasks/{tid}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-201",
            "reason": "Store manager taking over investigation",
        })
        assert r.status_code == 200

        detail = client.get(f"/api/v1/tasks/{tid}").json()
        reassign_events = [e for e in detail["audit_events"] if e["event_type"] == "REASSIGNED"]
        assert len(reassign_events) >= 1
        payload = reassign_events[-1]["event_payload"]
        assert payload["previous_assignee_id"] == "HR-102"
        assert payload["new_assignee_id"] == "HR-201"

    def test_reassign_from_created_delegates_to_assign(self, client, fresh_db_engine):
        """Calling POST /tasks/{id}/reassign on a CREATED task delegates to assign_task."""
        client.post(
            "/api/v1/imports/product_aliases",
            content=read_fixture("product_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/location_aliases",
            content=read_fixture("location_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )
        # Make HR-401 inactive so STORE-015 task is CREATED
        emp_csv = read_fixture("employees.csv").replace("HR-401,Rohan Iyer,ACTIVE,", "HR-401,Rohan Iyer,INACTIVE,")
        client.post(
            "/api/v1/imports/employees",
            content=emp_csv,
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/workforce_status",
            content=read_fixture("workforce_status.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/purchase_order_lines",
            content=read_fixture("purchase_order_lines.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post(
            "/api/v1/imports/goods_receipt_lines",
            content=read_fixture("goods_receipt_lines.csv"),
            headers={"Content-Type": "text/csv"},
        )
        client.post("/api/v1/reconciliation/run")

        tasks = client.get("/api/v1/tasks").json()["items"]
        task_store15 = next(t for t in tasks if t["canonical_location_id"] == "STORE-015")
        assert task_store15["status"] == "CREATED"
        tid = task_store15["id"]

        # Call reassign on the CREATED task
        reassign_resp = client.post(f"/api/v1/tasks/{tid}/reassign", json={
            "actor_id": "HR-201",
            "new_assignee_id": "HR-501",
            "reason": "Reassigning CREATED task to regional operations specialist",
        })
        assert reassign_resp.status_code == 200
        assigned_data = reassign_resp.json()
        assert assigned_data["status"] == "ASSIGNED"
        assert assigned_data["assignee_id"] == "HR-501"


