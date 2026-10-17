"""
Phase 8 End-to-End Test Suite: Comprehensive Multi-Dataset Integration & Scenario Verification.

Executes the entire OPSIB operational slice using the authentic fixture pack:
  1. Sequential ingestion of all 6 CSV fixtures in strict dependency order.
  2. Deterministic shortfall reconciliation against the frozen evaluation clock (2026-10-16T15:00:00+05:30).
  3. Simultaneous verification of all 6 mandatory reconciliation scenarios (PO-501 to PO-506).
  4. Workforce eligibility evaluation and organizational manager fallback routing (HR-102, HR-401).
  5. Full task state machine lifecycle (CREATED -> ASSIGNED -> ACK -> IN_PROGRESS -> COMPLETED -> VERIFIED).
  6. Automatic closure of operational exceptions upon verification.
  7. AI advisory copilot investigation with grounded citation whitelist verification.
  8. Repeat ingestion and reconciliation idempotency (zero double counting, zero duplicate tasks).
"""
import os
import pytest
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.ingestion.service import IngestionService
from app.detection.service import DetectionService
from app.workflows import task_service
from app.ai.service import InvestigationService
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine, Employee
from app.db.models.exception import OperationalException
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent
from app.config import DEFAULT_TENANT_ID, FROZEN_TIME

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(filename: str) -> str:
    path = os.path.join(WORKSPACE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_full_fixture_pack(db_session: Session) -> dict:
    """Ingests all 6 synthetic CSV fixtures in dependency order."""
    results = {}
    results["product_aliases"] = IngestionService.ingest_csv(
        db_session, "product_aliases", read_fixture("product_aliases.csv")
    )
    results["location_aliases"] = IngestionService.ingest_csv(
        db_session, "location_aliases", read_fixture("location_aliases.csv")
    )
    results["employees"] = IngestionService.ingest_csv(
        db_session, "employees", read_fixture("employees.csv")
    )
    results["workforce_status"] = IngestionService.ingest_csv(
        db_session, "workforce_status", read_fixture("workforce_status.csv")
    )
    results["purchase_order_lines"] = IngestionService.ingest_csv(
        db_session, "purchase_order_lines", read_fixture("purchase_order_lines.csv")
    )
    results["goods_receipt_lines"] = IngestionService.ingest_csv(
        db_session, "goods_receipt_lines", read_fixture("goods_receipt_lines.csv")
    )
    return results


@pytest.mark.scenario
@pytest.mark.integration
class TestEndToEndScenarios:
    """Complete End-to-End integration tests covering all assessment requirements."""

    def test_e2e_fixture_loading_and_all_six_scenarios(self, db_session: Session):
        """
        Step 1 & 2: Load entire fixture pack and execute deterministic reconciliation.
        Verifies exact outcomes for PO-501, PO-502, PO-503, PO-504, PO-505, and PO-506.
        """
        import_results = load_full_fixture_pack(db_session)

        # Ingestion Verification: Check duplicate row handling in PO lines and GRN lines
        # purchase_order_lines.csv has 7 data rows: 6 unique, 1 duplicate (ERP-PO506-L1)
        po_import = import_results["purchase_order_lines"]
        assert po_import.total_rows == 7
        assert po_import.accepted_rows == 6
        assert po_import.duplicate_rows == 1
        assert po_import.rejected_rows == 0

        # goods_receipt_lines.csv has 7 data rows: 6 unique, 1 duplicate (WMS-GRN506-A-L1)
        grn_import = import_results["goods_receipt_lines"]
        assert grn_import.total_rows == 7
        assert grn_import.accepted_rows == 6
        assert grn_import.duplicate_rows == 1
        assert grn_import.rejected_rows == 0

        # Run Reconciliation
        recon_result = DetectionService.run_reconciliation(db_session)
        assert recon_result.total_po_lines_evaluated == 6  # 6 unique canonical PO lines
        assert recon_result.skipped_cancelled == 1         # PO-504
        assert recon_result.skipped_future_deadline == 1    # PO-503
        assert recon_result.fully_satisfied == 1           # PO-502
        assert recon_result.shortfalls_detected == 3       # PO-501, PO-505, PO-506
        assert recon_result.exceptions_created == 3

        # Scenario PO-501: 100 EA ordered, split receipts of 60 + 32 = 92 EA -> 8 EA shortfall
        exc_501 = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc_501 is not None
        assert exc_501.expected_qty_base == 100.0
        assert exc_501.received_qty_base == 92.0
        assert exc_501.variance_qty_base == 8.0
        assert exc_501.base_uom == "EA"
        assert exc_501.canonical_location_id == "STORE-014"
        assert exc_501.evidence_records == ["ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"]
        assert "Receipt shortfall of 8.00 EA detected" in exc_501.neutral_summary
        assert exc_501.missing_evidence == ["SUPPLIER_DISPATCH_ASN", "CARRIER_PROOF_OF_DELIVERY"]

        # Scenario PO-502: 40 EA ordered, 40 EA received -> Satisfied, NO exception
        exc_502 = db_session.query(OperationalException).filter_by(po_number="PO-502").first()
        assert exc_502 is None

        # Scenario PO-503: 50 EA ordered, 20 received, deadline in future -> Window open, NO exception
        exc_503 = db_session.query(OperationalException).filter_by(po_number="PO-503").first()
        assert exc_503 is None

        # Scenario PO-504: 20 EA ordered, status CANCELLED -> Excluded, NO exception
        exc_504 = db_session.query(OperationalException).filter_by(po_number="PO-504").first()
        assert exc_504 is None

        # Scenario PO-505: 10 BOX (120 EA base), 96 EA received -> 24 EA shortfall
        exc_505 = db_session.query(OperationalException).filter_by(po_number="PO-505").first()
        assert exc_505 is not None
        assert exc_505.expected_qty_base == 120.0
        assert exc_505.received_qty_base == 96.0
        assert exc_505.variance_qty_base == 24.0
        assert exc_505.base_uom == "EA"
        assert exc_505.canonical_location_id == "STORE-015"
        assert exc_505.evidence_records == ["ERP-PO505-L1", "WMS-GRN505-A-L1"]

        # Scenario PO-506: 20 EA ordered, duplicate 10 EA receipt counted once -> 10 EA shortfall
        exc_506 = db_session.query(OperationalException).filter_by(po_number="PO-506").first()
        assert exc_506 is not None
        assert exc_506.expected_qty_base == 20.0
        assert exc_506.received_qty_base == 10.0
        assert exc_506.variance_qty_base == 10.0
        assert exc_506.canonical_location_id == "STORE-014"

    def test_e2e_workforce_routing_and_full_task_lifecycle(self, db_session: Session):
        """
        Step 3 & 4: Verify workforce routing policies and advance a task through the complete lifecycle.
        Assigned -> Acknowledged -> In Progress -> Completed -> Verified -> Exception Resolved.
        """
        load_full_fixture_pack(db_session)
        DetectionService.run_reconciliation(db_session)

        # Retrieve exceptions
        exc_501 = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        exc_505 = db_session.query(OperationalException).filter_by(po_number="PO-505").first()

        # Create tasks via task_service
        task_501 = task_service.create_task_for_exception(db_session, exc_501)
        task_505 = task_service.create_task_for_exception(db_session, exc_505)

        # Workforce Routing Assertions:
        # At STORE-014 (PO-501):
        # HR-101 is OFF_SHIFT -> rejected
        # HR-103 is INACTIVE -> rejected (HRMS status overrides shift)
        # HR-102 (Dev Shah) is ACTIVE and ON_SHIFT receiving associate -> selected!
        assert task_501.assignee_id == "HR-102"
        assert task_501.status == "ASSIGNED"
        assert task_501.assignment_reason == "PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE"

        # At STORE-015 (PO-505):
        # No receiving associates exist at STORE-015.
        # Fallback to active, on-shift store manager HR-401 (Rohan Iyer)!
        assert task_505.assignee_id == "HR-401"
        assert task_505.status == "ASSIGNED"
        assert task_505.assignment_reason == "FALLBACK_STORE_MANAGER"

        # Execute Full Task Lifecycle on task_501:
        # 1. Acknowledge task
        task_501 = task_service.acknowledge_task(db_session, task_501.id, actor_id="HR-102")
        assert task_501.status == "ACKNOWLEDGED"

        # 2. Start investigation
        task_501 = task_service.start_task(db_session, task_501.id, actor_id="HR-102")
        assert task_501.status == "IN_PROGRESS"

        # 3. Complete task with resolution code and evidence reference
        task_501 = task_service.complete_task(
            db_session,
            task_501.id,
            actor_id="HR-102",
            resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            resolution_evidence_ref="DOCK-INSPECTION-LOG-882",
            resolution_notes="Dock search confirmed 92 EA received. Supplier short-shipped 8 EA.",
        )
        assert task_501.status == "COMPLETED"
        assert task_501.resolution_code == "SUPPLIER_SHORT_SHIPMENT_CONFIRMED"

        # 4. Submit for verification
        task_501 = task_service.submit_for_verification(db_session, task_501.id, actor_id="HR-102")
        assert task_501.status == "PENDING_VERIFICATION"

        # 5. Manager verification
        task_501 = task_service.verify_task(
            db_session,
            task_501.id,
            actor_id="HR-201",
            notes="Approved invoice adjustment for 8 EA.",
        )
        assert task_501.status == "VERIFIED"

        # Crucial Verification Invariant: Linked OperationalException is automatically marked RESOLVED
        db_session.refresh(exc_501)
        assert exc_501.status == "RESOLVED"
        assert exc_501.resolved_at is not None
        assert "Resolved via task verification by HR-201" in exc_501.resolution_notes

        # Verify Append-Only Audit Trail: All transitions recorded chronologically
        audit_events = (
            db_session.query(TaskAuditEvent)
            .filter_by(task_id=task_501.id)
            .order_by(TaskAuditEvent.timestamp.asc())
            .all()
        )
        event_types = [e.event_type for e in audit_events]
        assert event_types == [
            "ASSIGNED",
            "ACKNOWLEDGED",
            "STATUS_CHANGE",  # start
            "COMPLETED",
            "STATUS_CHANGE",  # submit_verification
            "VERIFIED",
        ]

    def test_e2e_ai_advisory_investigation_brief(self, client: TestClient, db_session: Session):
        """
        Step 5: Verify AI investigation advisory boundary and grounded citations via REST API.
        """
        load_full_fixture_pack(db_session)
        DetectionService.run_reconciliation(db_session)

        exc_501 = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc_501 is not None

        # Request AI investigation brief via API with mock provider
        resp = client.post(f"/api/v1/exceptions/{exc_501.id}/investigate?use_mock=true")
        assert resp.status_code == 200
        data = resp.json()

        assert data["exception_id"] == str(exc_501.id)
        assert data["provider_used"] == "mock"
        assert "brief" in data

        brief = data["brief"]
        assert "PO-501" in brief["executive_summary"]
        assert len(brief["recommended_investigation_steps"]) >= 3

        # Grounded Citations Guardrail Invariant:
        # All cited record IDs must belong to the whitelist [ERP-PO501-L1, WMS-GRN501-A-L1, WMS-GRN501-B-L1]
        valid_whitelist = {"ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"}
        for cited_id in brief["cited_record_ids"]:
            assert cited_id in valid_whitelist

        # Epistemic Neutrality Invariant:
        assert "Supplier dispatch" in brief["neutrality_disclaimer"]
        assert "do not establish supplier fault" in brief["neutrality_disclaimer"]

    def test_e2e_repeat_import_and_reconciliation_idempotency(self, db_session: Session):
        """
        Step 6: Verify double ingestion idempotency (Design Question 1).
        Importing the entire fixture pack twice produces 0 duplicate records and 0 stock double-counting.
        """
        # First import and reconciliation
        load_full_fixture_pack(db_session)
        res1 = DetectionService.run_reconciliation(db_session)
        assert res1.shortfalls_detected == 3
        assert res1.exceptions_created == 3

        po_lines_count_1 = db_session.query(PurchaseOrderLine).count()
        grn_lines_count_1 = db_session.query(GoodsReceiptLine).count()
        employees_count_1 = db_session.query(Employee).count()
        exceptions_count_1 = db_session.query(OperationalException).count()

        # Second import of all 6 CSV files
        reimport_results = load_full_fixture_pack(db_session)

        # Every file must report 100% duplicate skipped and 0 new accepted rows
        for dataset_name, result in reimport_results.items():
            assert result.accepted_rows == 0, f"Dataset {dataset_name} accepted rows on reimport!"
            assert result.duplicate_rows > 0, f"Dataset {dataset_name} did not detect duplicate rows!"

        # Database record counts must be exactly identical
        assert db_session.query(PurchaseOrderLine).count() == po_lines_count_1
        assert db_session.query(GoodsReceiptLine).count() == grn_lines_count_1
        assert db_session.query(Employee).count() == employees_count_1
        assert db_session.query(OperationalException).count() == exceptions_count_1

        # Second reconciliation run must create ZERO new exceptions
        res2 = DetectionService.run_reconciliation(db_session)
        assert res2.shortfalls_detected == 3
        assert res2.exceptions_created == 0
        assert res2.exceptions_already_existed == 3
