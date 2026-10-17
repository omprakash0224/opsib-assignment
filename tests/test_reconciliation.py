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
from app.db.models.exception import OperationalException
from app.config import DEFAULT_TENANT_ID

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
