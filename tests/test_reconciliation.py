"""
Phase 4 Test Suite: Deterministic Reconciliation & Exception Detection (Task B).

Verifies all 6 mandatory scenarios from the assessment specification:
  PO-501 – Split receipts => 8 EA shortfall
  PO-502 – Full receipt => no exception
  PO-503 – Future deadline => not actionable at frozen time
  PO-504 – Cancelled PO => no exception
  PO-505 – 10 BOX (120 EA) vs 96 EA => 24 EA shortfall
  PO-506 – Duplicate WMS receipt deduped => 10 EA shortfall

Also tests idempotency (running reconciliation twice), API endpoints,
and the neutral provenance requirements (ADR-6).
"""
import os
import pytest
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionService
from app.detection.service import DetectionService, ReconciliationRunResult
from app.detection.detector import ReconciliationDetector
from app.detection.rules import build_exception_key, compute_variance
from app.db.models.exception import OperationalException, ExceptionEvent, DataQualityIssue
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent
from app.workflows import task_service
from app.config import DEFAULT_TENANT_ID, get_current_time

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_fixture(filename: str) -> str:
    """Reads a fixture CSV from the workspace root."""
    path = os.path.join(WORKSPACE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_all_fixtures(db_session: Session) -> None:
    """
    Load the full fixture dataset in dependency order.
    This is a shared helper used by many scenario tests.
    """
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))
    IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
    IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))
    IngestionService.ingest_csv(db_session, "purchase_order_lines", read_fixture("purchase_order_lines.csv"))
    IngestionService.ingest_csv(db_session, "goods_receipt_lines", read_fixture("goods_receipt_lines.csv"))


def get_exception_for_po(db_session: Session, po_number: str) -> OperationalException | None:
    """Convenience: retrieve exception for a given PO number."""
    return db_session.query(OperationalException).filter_by(po_number=po_number).first()


# ---------------------------------------------------------------------------
# Unit Tests: Rule Helpers
# ---------------------------------------------------------------------------

class TestRuleHelpers:
    """Unit tests for detection/rules.py helpers."""

    def test_compute_variance_shortfall(self):
        """Positive variance when received < expected."""
        assert compute_variance(100.0, 92.0) == pytest.approx(8.0)

    def test_compute_variance_fully_satisfied(self):
        """Zero variance when received == expected."""
        assert compute_variance(40.0, 40.0) == pytest.approx(0.0)

    def test_compute_variance_over_received(self):
        """Negative variance when received > expected (over-delivery)."""
        assert compute_variance(50.0, 55.0) == pytest.approx(-5.0)

    def test_build_exception_key_format(self):
        """Exception key must follow the deterministic format for idempotency."""
        key = build_exception_key("TENANT-001", "PO-501", 1)
        assert key == "TENANT-001:RECEIPT_SHORTFALL:PO-501:1"


# ---------------------------------------------------------------------------
# Scenario Tests (Integration)
# ---------------------------------------------------------------------------

class TestReconciliationScenarios:
    """
    Integration tests verifying all 6 mandatory scenarios from the assessment spec.
    Each test loads the full fixture set and runs DetectionService.run_reconciliation().
    """

    def test_scenario_po501_split_receipts_shortfall(self, db_session: Session):
        """
        PO-501: 100 EA ordered.
        Split receipts: GRN-501-A (60 EA) + GRN-501-B (32 EA) = 92 EA total received.
        Expected: SHORTFALL of 8 EA detected.
        """
        load_all_fixtures(db_session)
        result = DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-501")
        assert exc is not None, "Expected a RECEIPT_SHORTFALL exception for PO-501"
        assert exc.exception_type == "RECEIPT_SHORTFALL"
        assert exc.status == "DETECTED"
        assert exc.expected_qty_base == pytest.approx(100.0)
        assert exc.received_qty_base == pytest.approx(92.0)
        assert exc.variance_qty_base == pytest.approx(8.0)
        assert exc.base_uom == "EA"

        # Verify split receipt evidence references are both included
        assert "WMS-GRN501-A-L1" in exc.evidence_records
        assert "WMS-GRN501-B-L1" in exc.evidence_records
        assert "ERP-PO501-L1" in exc.evidence_records

    def test_scenario_po502_full_receipt_no_exception(self, db_session: Session):
        """
        PO-502: 40 EA ordered, 40 EA received => 0 variance.
        Expected: NO exception created.
        """
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-502")
        assert exc is None, "PO-502 is fully satisfied; no exception should be created."

    def test_scenario_po503_future_deadline_not_actionable(self, db_session: Session):
        """
        PO-503: 50 EA ordered, 20 EA received.
        Deadline: 2026-10-17T18:00:00+05:30 (future relative to frozen time 2026-10-16T15:00:00+05:30).
        Expected: NO exception created (window still open).
        """
        load_all_fixtures(db_session)
        result = DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-503")
        assert exc is None, (
            "PO-503 deadline is in the future relative to the frozen evaluation time; "
            "no exception should be raised until the window closes."
        )
        # Verify the run correctly counted this as skipped (future deadline)
        assert result.skipped_future_deadline >= 1

    def test_scenario_po504_cancelled_po_no_exception(self, db_session: Session):
        """
        PO-504: Status is CANCELLED.
        Expected: NO exception created.
        """
        load_all_fixtures(db_session)
        result = DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-504")
        assert exc is None, "Cancelled PO-504 must never trigger a shortfall exception."
        assert result.skipped_cancelled >= 1

    def test_scenario_po505_uom_conversion_box_to_ea_shortfall(self, db_session: Session):
        """
        PO-505: 10 BOX ordered (converted to 120 EA base) at STORE-015.
        GRN-505-A received 96 EA.
        Expected: SHORTFALL of 24 EA at STORE-015.
        """
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-505")
        assert exc is not None, "PO-505 should produce a 24 EA shortfall exception."
        assert exc.expected_qty_base == pytest.approx(120.0), (
            "10 BOX * 12 (to_base_factor) should equal 120 EA"
        )
        assert exc.received_qty_base == pytest.approx(96.0)
        assert exc.variance_qty_base == pytest.approx(24.0)
        assert exc.base_uom == "EA"
        assert exc.canonical_location_id == "STORE-015"

    def test_scenario_po506_deduplicated_receipt_shortfall(self, db_session: Session):
        """
        PO-506: 20 EA ordered. WMS receipt row WMS-GRN506-A-L1 (10 EA) transmitted twice.
        After Phase 3 deduplication, only one receipt row (10 EA) exists in canonical store.
        Expected: SHORTFALL of 10 EA (not 0, since duplicate was deduplicated to one 10 EA row).
        """
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-506")
        assert exc is not None, (
            "PO-506 should have a 10 EA shortfall: duplicate receipt was deduplicated, "
            "so only 10 EA is credited, not 20 EA."
        )
        assert exc.expected_qty_base == pytest.approx(20.0)
        assert exc.received_qty_base == pytest.approx(10.0)
        assert exc.variance_qty_base == pytest.approx(10.0)

    def test_all_six_scenarios_together(self, db_session: Session):
        """
        Run all six scenarios end-to-end and assert final exception state:
          - 3 shortfalls detected (PO-501, PO-505, PO-506)
          - 3 non-exceptions (PO-502 satisfied, PO-503 future, PO-504 cancelled)
        """
        load_all_fixtures(db_session)
        result = DetectionService.run_reconciliation(session=db_session)

        # 3 shortfalls should have been detected
        assert result.shortfalls_detected == 3, (
            f"Expected 3 shortfalls (PO-501, PO-505, PO-506), got {result.shortfalls_detected}"
        )
        assert result.exceptions_created == 3
        assert result.skipped_cancelled == 1  # PO-504
        assert result.skipped_future_deadline == 1  # PO-503
        assert result.fully_satisfied == 1  # PO-502
        assert result.total_po_lines_evaluated == 6  # 6 unique PO lines after deduplication


# ---------------------------------------------------------------------------
# Idempotency Tests
# ---------------------------------------------------------------------------

class TestReconciliationIdempotency:
    """
    Verify that running reconciliation multiple times never produces
    duplicate OperationalException records (ADR-2, ADR-5).
    """

    def test_double_reconciliation_idempotent(self, db_session: Session):
        """Running reconciliation twice produces identical DB state (no duplicates)."""
        load_all_fixtures(db_session)

        result1 = DetectionService.run_reconciliation(session=db_session)
        assert result1.exceptions_created == 3
        assert result1.exceptions_already_existed == 0

        result2 = DetectionService.run_reconciliation(session=db_session)
        assert result2.exceptions_created == 0, (
            "Second run must not create new exceptions; all should already exist."
        )
        assert result2.exceptions_already_existed == 3

        # Total exception count must remain 3
        total = db_session.query(OperationalException).count()
        assert total == 3, "Duplicate reconciliation runs must not create duplicate exceptions."

    def test_reconciliation_on_empty_db_returns_zero_findings(self, db_session: Session):
        """No fixtures loaded => reconciliation finds nothing to evaluate."""
        result = DetectionService.run_reconciliation(session=db_session)
        assert result.total_po_lines_evaluated == 0
        assert result.shortfalls_detected == 0
        assert result.exceptions_created == 0


# ---------------------------------------------------------------------------
# Neutral Provenance / Epistemic Neutrality Tests (ADR-6)
# ---------------------------------------------------------------------------

class TestNeutralProvenance:
    """
    Verify the epistemic neutrality requirements:
    - Exceptions are labelled RECEIPT_SHORTFALL (not SUPPLIER_SHORT_SHIPMENT).
    - neutral_summary does not contain blame assertions.
    - missing_evidence explicitly acknowledges absent ASN/dispatch data.
    """

    def test_exception_type_is_neutral(self, db_session: Session):
        """Exception type must be RECEIPT_SHORTFALL, never supplier-blaming."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        for exc in db_session.query(OperationalException).all():
            assert exc.exception_type == "RECEIPT_SHORTFALL", (
                f"Exception {exc.exception_key} has non-neutral type '{exc.exception_type}'"
            )
            # Must not assert supplier fault in the type field
            assert "SUPPLIER" not in exc.exception_type

    def test_neutral_summary_does_not_assert_blame(self, db_session: Session):
        """neutral_summary must describe the arithmetic fact without asserting fault.

        The correct neutral phrasing is 'findings do not establish supplier fault'.
        The summary must explicitly negate blame rather than assert it.
        """
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        for exc in db_session.query(OperationalException).all():
            summary = exc.neutral_summary.lower()
            # Summary must acknowledge the missing evidence
            assert "absent" in summary or "asn" in summary, (
                f"neutral_summary for {exc.exception_key} must acknowledge missing ASN data"
            )
            # Summary must NOT positively assert supplier fault.
            # "do not establish supplier fault" is correct; "establishes supplier fault" is not.
            if "supplier fault" in summary:
                # Acceptable only if it appears in a negation context
                assert "do not establish supplier fault" in summary or \
                       "not establish supplier fault" in summary, (
                    f"neutral_summary for {exc.exception_key} asserts supplier fault without negation: {summary}"
                )
            # Summary must NOT conclude carrier loss positively
            if "carrier loss" in summary:
                assert "do not" in summary or "not" in summary, (
                    f"neutral_summary for {exc.exception_key} positively asserts carrier loss: {summary}"
                )

    def test_missing_evidence_explicitly_documented(self, db_session: Session):
        """missing_evidence must explicitly record the absent ASN and POD fields."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        for exc in db_session.query(OperationalException).all():
            assert exc.missing_evidence is not None
            assert len(exc.missing_evidence) > 0
            # At least one of the two sentinel values must be present
            assert any(
                "ASN" in item or "POD" in item or "DISPATCH" in item
                for item in exc.missing_evidence
            ), f"Exception {exc.exception_key} missing_evidence does not reference ASN or POD"

    def test_evidence_records_cite_source_ids(self, db_session: Session):
        """Each exception must cite at least the PO source record ID in evidence."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        po501_exc = get_exception_for_po(db_session, "PO-501")
        assert "ERP-PO501-L1" in po501_exc.evidence_records

        po505_exc = get_exception_for_po(db_session, "PO-505")
        assert "ERP-PO505-L1" in po505_exc.evidence_records
        assert "WMS-GRN505-A-L1" in po505_exc.evidence_records


# ---------------------------------------------------------------------------
# REST API Endpoint Tests
# ---------------------------------------------------------------------------

class TestReconciliationAPI:
    """
    Tests for the POST /reconciliation/run and GET /exceptions endpoints
    using the FastAPI TestClient.
    """

    def test_api_run_reconciliation_returns_summary(self, client):
        """POST /reconciliation/run should return a valid summary after loading fixtures."""
        # Load fixtures via the import API
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            fixture_csv = read_fixture(f"{dataset}.csv")
            resp = client.post(
                f"/api/v1/imports/{dataset}",
                content=fixture_csv,
                headers={"Content-Type": "text/csv"},
            )
            assert resp.status_code == 201, f"Import of {dataset} failed: {resp.text}"

        # Trigger reconciliation
        recon_resp = client.post("/api/v1/reconciliation/run")
        assert recon_resp.status_code == 200
        data = recon_resp.json()

        assert data["shortfalls_detected"] == 3
        assert data["exceptions_created"] == 3
        assert data["skipped_cancelled"] == 1
        assert data["skipped_future_deadline"] == 1
        assert data["fully_satisfied"] == 1
        assert len(data["new_exceptions"]) == 3

    def test_api_run_reconciliation_idempotent(self, client):
        """Running POST /reconciliation/run twice must not create duplicate exceptions."""
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )

        client.post("/api/v1/reconciliation/run")

        # Second run
        resp2 = client.post("/api/v1/reconciliation/run")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["exceptions_created"] == 0
        assert data2["exceptions_already_existed"] == 3

    def test_api_list_exceptions(self, client):
        """GET /exceptions should list all detected exceptions."""
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )
        client.post("/api/v1/reconciliation/run")

        resp = client.get("/api/v1/exceptions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        po_numbers = {item["po_number"] for item in data["items"]}
        assert po_numbers == {"PO-501", "PO-505", "PO-506"}

    def test_api_list_exceptions_filter_by_po_number(self, client):
        """GET /exceptions?po_number=PO-501 should return only PO-501 exception."""
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )
        client.post("/api/v1/reconciliation/run")

        resp = client.get("/api/v1/exceptions?po_number=PO-501")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["po_number"] == "PO-501"
        assert data["items"][0]["variance_qty_base"] == pytest.approx(8.0)

    def test_api_list_exceptions_filter_by_location(self, client):
        """GET /exceptions?canonical_location_id=STORE-015 should return only PO-505."""
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )
        client.post("/api/v1/reconciliation/run")

        resp = client.get("/api/v1/exceptions?canonical_location_id=STORE-015")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["po_number"] == "PO-505"

    def test_api_get_exception_by_id(self, client):
        """GET /exceptions/{id} should return a single exception with full detail."""
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )
        client.post("/api/v1/reconciliation/run")

        # Get list first to find an ID
        list_resp = client.get("/api/v1/exceptions?po_number=PO-501")
        exc_id = list_resp.json()["items"][0]["id"]

        detail_resp = client.get(f"/api/v1/exceptions/{exc_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["po_number"] == "PO-501"
        assert detail["variance_qty_base"] == pytest.approx(8.0)
        assert "ERP-PO501-L1" in detail["evidence_records"]

    def test_api_get_exception_not_found(self, client):
        """GET /exceptions/{nonexistent_id} should return 404."""
        resp = client.get("/api/v1/exceptions/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bug 5 Tests: Late-Arriving Data Re-Evaluates Exceptions (FIX.md §5)
# ---------------------------------------------------------------------------

class TestBug5LateArrivingDataReevaluatesExceptions:
    """
    Bug 5 tests per FIX.md:
    - Late receipt that clears shortfall: exception auto-closes, task cancelled, audit trail present.
    - Late receipt that only partly clears it: quantities and evidence updated, revision bumped, no duplicate exception or task.
    - Re-run with no changes: no revision bump (idempotent).
    - Deadline == frozen time boundary test.
    - Resolved exception does not silently reopen on quantity change; suggests reopen for human review.
    - Fresh session API test (multi-session).
    """

    def test_late_receipt_clears_shortfall_auto_closes_exception_and_cancels_task(
        self, db_session: Session
    ):
        load_all_fixtures(db_session)
        recon1 = DetectionService.run_reconciliation(session=db_session)
        assert recon1.shortfalls_detected == 3

        exc = get_exception_for_po(db_session, "PO-501")
        assert exc is not None
        assert exc.status == "DETECTED"
        assert exc.variance_qty_base == pytest.approx(8.0)
        assert exc.revision == 1

        # Provision task for PO-501
        task = task_service.create_task_for_exception(session=db_session, exception=exc)
        assert task.status == "ASSIGNED"

        # Late receipt arrives covering the 8 EA shortfall
        late_receipt_csv = (
            "source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,8,EA,2026-10-16T14:50:00+05:30,POSTED\n"
        )
        IngestionService.ingest_csv(db_session, "goods_receipt_lines", late_receipt_csv)

        # Re-run reconciliation
        DetectionService.run_reconciliation(session=db_session)

        # Assert exception is AUTO_CLOSED with variance 0 and revision bumped
        db_session.refresh(exc)
        assert exc.status == "AUTO_CLOSED"
        assert exc.received_qty_base == pytest.approx(100.0)
        assert exc.variance_qty_base == pytest.approx(0.0)
        assert exc.resolution_notes == "Shortfall cleared by late receipt"
        assert exc.resolved_at is not None
        assert exc.revision == 2

        # Assert task is CANCELLED_NO_LONGER_NEEDED
        db_session.refresh(task)
        assert task.status == "CANCELLED_NO_LONGER_NEEDED"

        # Assert task audit trail has cancellation event
        cancellation_audit = (
            db_session.query(TaskAuditEvent)
            .filter_by(task_id=task.id, event_type="CANCELLED")
            .first()
        )
        assert cancellation_audit is not None
        assert cancellation_audit.to_status == "CANCELLED_NO_LONGER_NEEDED"
        assert "Shortfall cleared by late receipt" in cancellation_audit.reason

        # Assert exception events has AUTO_CLOSED event
        event = (
            db_session.query(ExceptionEvent)
            .filter_by(exception_id=exc.id, event_type="AUTO_CLOSED")
            .first()
        )
        assert event is not None
        assert event.new_state["status"] == "AUTO_CLOSED"

    def test_late_receipt_partly_clears_shortfall_updates_quantities_and_bumps_revision(
        self, db_session: Session
    ):
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-501")
        assert exc is not None
        assert exc.revision == 1
        initial_exc_id = exc.id

        task = task_service.create_task_for_exception(session=db_session, exception=exc)
        assert task.status == "ASSIGNED"

        # Late receipt arrives covering 4 EA out of 8 EA shortfall
        late_receipt_csv = (
            "source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,4,EA,2026-10-16T14:50:00+05:30,POSTED\n"
        )
        IngestionService.ingest_csv(db_session, "goods_receipt_lines", late_receipt_csv)

        # Re-run reconciliation
        DetectionService.run_reconciliation(session=db_session)

        db_session.refresh(exc)
        assert exc.id == initial_exc_id
        assert exc.status == "DETECTED"
        assert exc.received_qty_base == pytest.approx(96.0)
        assert exc.variance_qty_base == pytest.approx(4.0)
        assert exc.revision == 2
        assert "WMS-GRN501-C-L1" in exc.evidence_records

        # Ensure no duplicate exception or task
        assert db_session.query(OperationalException).filter_by(po_number="PO-501").count() == 1
        assert db_session.query(HumanTask).filter_by(exception_id=exc.id).count() == 1

        # Assert exception event logged
        event = (
            db_session.query(ExceptionEvent)
            .filter_by(exception_id=exc.id, event_type="QUANTITY_UPDATED")
            .first()
        )
        assert event is not None
        assert event.previous_state["received_qty_base"] == pytest.approx(92.0)
        assert event.new_state["received_qty_base"] == pytest.approx(96.0)

    def test_rerun_with_no_changes_is_idempotent_no_revision_bump(
        self, db_session: Session
    ):
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        # Add partial receipt and update
        late_receipt_csv = (
            "source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,4,EA,2026-10-16T14:50:00+05:30,POSTED\n"
        )
        IngestionService.ingest_csv(db_session, "goods_receipt_lines", late_receipt_csv)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-501")
        assert exc.revision == 2

        # Re-run immediately without any new data
        events_count_before = db_session.query(ExceptionEvent).filter_by(exception_id=exc.id).count()
        DetectionService.run_reconciliation(session=db_session)
        db_session.refresh(exc)

        assert exc.revision == 2, "Revision must not bump when data has not changed"
        events_count_after = db_session.query(ExceptionEvent).filter_by(exception_id=exc.id).count()
        assert events_count_after == events_count_before, "No new events should be created on idempotent run"

    def test_deadline_equals_frozen_time_boundary_is_actionable(
        self, db_session: Session
    ):
        from datetime import datetime, timedelta
        from app.config import get_current_time, KOLKATA_TZ
        from app.db.models.canonical import PurchaseOrderLine

        # Ingest aliases and required setup
        IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
        IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

        frozen = get_current_time()

        # PO with deadline EXACTLY equal to frozen time -> actionable (not skipped)
        po_exact = PurchaseOrderLine(
            tenant_id="TENANT-001",
            source_system="ERP",
            source_record_id="ERP-PO-EXACT-L1",
            po_number="PO-EXACT",
            line_number=1,
            supplier_code="SUP-TEST",
            canonical_location_id="STORE-014",
            raw_location_code="WH-MUM-01",
            canonical_product_id="SKU-CHAIR-01",
            raw_item_code="SKU-CHAIR-01",
            ordered_qty_source=10.0,
            ordered_uom_source="EA",
            ordered_qty_base=10.0,
            base_uom="EA",
            status="OPEN",
            expected_receipt_by=frozen,
        )
        # PO with deadline 1 second into future -> not actionable (skipped)
        po_future = PurchaseOrderLine(
            tenant_id="TENANT-001",
            source_system="ERP",
            source_record_id="ERP-PO-FUT-L1",
            po_number="PO-FUT",
            line_number=1,
            supplier_code="SUP-TEST",
            canonical_location_id="STORE-014",
            raw_location_code="WH-MUM-01",
            canonical_product_id="SKU-CHAIR-01",
            raw_item_code="SKU-CHAIR-01",
            ordered_qty_source=10.0,
            ordered_uom_source="EA",
            ordered_qty_base=10.0,
            base_uom="EA",
            status="OPEN",
            expected_receipt_by=frozen + timedelta(seconds=1),
        )
        db_session.add_all([po_exact, po_future])
        db_session.commit()

        result = DetectionService.run_reconciliation(session=db_session)
        exc_exact = get_exception_for_po(db_session, "PO-EXACT")
        exc_future = get_exception_for_po(db_session, "PO-FUT")

        assert exc_exact is not None, "Boundary deadline == frozen time must be actionable"
        assert exc_exact.variance_qty_base == pytest.approx(10.0)
        assert exc_future is None, "Deadline > frozen time must be skipped as future deadline"

    def test_exception_already_resolved_or_dismissed_does_not_silently_reopen_on_quantity_change(
        self, db_session: Session
    ):
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(session=db_session)

        exc = get_exception_for_po(db_session, "PO-501")
        assert exc is not None
        # Mark exception as RESOLVED
        exc.status = "RESOLVED"
        exc.resolution_notes = "Manually verified and resolved"
        db_session.commit()

        # Late receipt arrives changing numbers
        late_receipt_csv = (
            "source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,4,EA,2026-10-16T14:50:00+05:30,POSTED\n"
        )
        IngestionService.ingest_csv(db_session, "goods_receipt_lines", late_receipt_csv)

        DetectionService.run_reconciliation(session=db_session)
        db_session.refresh(exc)

        # Must not silently reopen
        assert exc.status == "RESOLVED"

        # Reopen suggested event and DQ issue should be created
        event = (
            db_session.query(ExceptionEvent)
            .filter_by(exception_id=exc.id, event_type="REOPEN_SUGGESTED")
            .first()
        )
        assert event is not None

        dq_issue = (
            db_session.query(DataQualityIssue)
            .filter_by(source_record_id=exc.exception_key, issue_type="REOPEN_SUGGESTED")
            .first()
        )
        assert dq_issue is not None

    def test_fresh_session_api_late_receipt_auto_closes_exception_and_cancels_task(
        self, fresh_client
    ):
        # Ingest all fixtures
        for dataset in [
            "product_aliases",
            "location_aliases",
            "employees",
            "workforce_status",
            "purchase_order_lines",
            "goods_receipt_lines",
        ]:
            resp = fresh_client.post(
                f"/api/v1/imports/{dataset}",
                content=read_fixture(f"{dataset}.csv"),
                headers={"Content-Type": "text/csv"},
            )
            assert resp.status_code == 201

        # Trigger reconciliation
        recon_resp = fresh_client.post("/api/v1/reconciliation/run")
        assert recon_resp.status_code == 200

        # Retrieve PO-501 exception and task
        exc_list = fresh_client.get("/api/v1/exceptions?po_number=PO-501").json()
        assert exc_list["total"] == 1
        exc_id = exc_list["items"][0]["id"]
        assert exc_list["items"][0]["status"] == "DETECTED"
        assert exc_list["items"][0]["variance_qty_base"] == pytest.approx(8.0)

        tasks = fresh_client.get("/api/v1/tasks?canonical_location_id=STORE-014").json()
        task_501 = next(t for t in tasks["items"] if t["exception_id"] == exc_id)
        assert task_501["status"] == "ASSIGNED"

        # Import late receipt covering the remaining 8 EA
        late_receipt_csv = (
            "source_record_id,receipt_number,receipt_line,po_number,po_line_number,location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS-GRN501-C-L1,GRN-501-C,1,PO-501,1,WH-MH-014,WMS-BRC-001,8,EA,2026-10-16T14:50:00+05:30,POSTED\n"
        )
        import_resp = fresh_client.post(
            "/api/v1/imports/goods_receipt_lines",
            content=late_receipt_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert import_resp.status_code == 201

        # Re-run reconciliation
        recon_resp2 = fresh_client.post("/api/v1/reconciliation/run")
        assert recon_resp2.status_code == 200

        # Check in a fresh session: exception is AUTO_CLOSED
        exc_detail = fresh_client.get(f"/api/v1/exceptions/{exc_id}").json()
        assert exc_detail["status"] == "AUTO_CLOSED"
        assert exc_detail["variance_qty_base"] == pytest.approx(0.0)
        assert exc_detail["received_qty_base"] == pytest.approx(100.0)
        assert exc_detail["revision"] == 2

        # Check in a fresh session: task is CANCELLED_NO_LONGER_NEEDED
        task_detail = fresh_client.get(f"/api/v1/tasks/{task_501['id']}").json()
        assert task_detail["status"] == "CANCELLED_NO_LONGER_NEEDED"

