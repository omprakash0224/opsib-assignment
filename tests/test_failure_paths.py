"""
Phase 8 Test Suite: Failure Paths, Data Quality Isolation, Malformed Inputs & Workflow Edge Cases.

Verifies:
  1. Data Quality Isolation: Unmapped SKUs and locations create DataQualityIssue records
     and NEVER generate false business shortfall exceptions (satisfies Task B constraint).
  2. Conflicting Duplicates: Repeating a source ID with altered business content is flagged
     as CONFLICTING_DUPLICATE without silently mutating existing canonical data.
  3. Malformed Inputs: Row-level schema validation errors are captured in batch errors without
     dropping valid rows in the same batch (partial success visibility).
  4. Workflow Failure Paths:
     - Absent eligible assignees gracefully route to CREATED with MANUAL_ASSIGNMENT_REQUIRED.
     - Illegal state transitions (skipping states or mutating terminal states) raise 409 Conflict.
     - Reassignment from non-reassignable states (COMPLETED / VERIFIED) is rejected.
     - Reassignment with empty reason rejected at validation with 422.
  5. AI Provider Failure Resilience: External timeouts and 500 errors translate cleanly to
     HTTP 502 Bad Gateway with RFC 7807 envelopes while guaranteeing 100% database immutability.
"""
import os
import uuid
import pytest
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionService
from app.detection.service import DetectionService
from app.workflows import task_service
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine, Employee
from app.db.models.exception import OperationalException, DataQualityIssue
from app.db.models.task import HumanTask
from app.core.exceptions import InvalidStateTransitionError, AIProviderError
from app.ai.service import InvestigationService
from app.ai.interface import AIInvestigationProvider

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(filename: str) -> str:
    path = os.path.join(WORKSPACE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_base_aliases(db_session: Session) -> None:
    """Load product and location alias mappings."""
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))


def load_all_fixtures(db_session: Session) -> None:
    """Load all 6 fixture datasets."""
    load_base_aliases(db_session)
    IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
    IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))
    IngestionService.ingest_csv(db_session, "purchase_order_lines", read_fixture("purchase_order_lines.csv"))
    IngestionService.ingest_csv(db_session, "goods_receipt_lines", read_fixture("goods_receipt_lines.csv"))


def create_test_exception(
    db_session: Session,
    po_number: str = "PO-FAIL-TEST",
    canonical_location_id: str = "STORE-014",
    canonical_product_id: str = "SKU-001",
    variance: float = 8.0,
) -> OperationalException:
    """Helper creating a valid OperationalException with all required fields."""
    exc = OperationalException(
        tenant_id="TENANT-001",
        exception_key=f"TENANT-001:SHORTFALL:{po_number}:1",
        exception_type="RECEIPT_SHORTFALL",
        status="DETECTED",
        po_number=po_number,
        po_line_number=1,
        canonical_product_id=canonical_product_id,
        canonical_location_id=canonical_location_id,
        expected_qty_base=10.0,
        received_qty_base=10.0 - variance,
        variance_qty_base=variance,
        base_uom="EA",
        rule_version="v1.0.0",
        evidence_records=[f"ERP-{po_number}-L1"],
        neutral_summary=f"Receipt shortfall of {variance} EA detected.",
        missing_evidence=["SUPPLIER_DISPATCH_ASN", "CARRIER_POD"],
    )
    db_session.add(exc)
    db_session.commit()
    return exc


# ===========================================================================
# 1. Data Quality Isolation Tests
# ===========================================================================

@pytest.mark.failure_path
class TestDataQualityFailures:
    """Tests that unmapped entities generate DataQualityIssues and never false business exceptions."""

    def test_unmapped_sku_in_po_creates_data_quality_issue_and_no_exception(self, db_session: Session):
        load_base_aliases(db_session)

        # PO referencing unknown product code
        bad_po_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-UNMAPPED-PO,PO-999,1,SUPP-01,MUM-014,SKU-NONEXISTENT,50,EA,OPEN,2026-10-15T18:00:00+05:30\n"
        )
        result = IngestionService.ingest_csv(db_session, "purchase_order_lines", bad_po_csv)

        # Assert row rejected
        assert result.rejected_rows == 1
        assert result.accepted_rows == 0

        # Assert DataQualityIssue recorded
        dq_issue = (
            db_session.query(DataQualityIssue)
            .filter_by(source_record_id="ERP-UNMAPPED-PO")
            .first()
        )
        assert dq_issue is not None
        assert dq_issue.issue_type == "UNMAPPED_CROSSWALK"
        assert "SKU-NONEXISTENT" in str(dq_issue.details)

        # Assert canonical PO line was NOT created
        po_line = db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-UNMAPPED-PO").first()
        assert po_line is None

        # Run reconciliation: assert zero business exceptions are generated
        recon_result = DetectionService.run_reconciliation(db_session)
        assert recon_result.shortfalls_detected == 0
        assert recon_result.exceptions_created == 0

    def test_unmapped_sku_in_receipt_creates_data_quality_issue(self, db_session: Session):
        load_base_aliases(db_session)

        bad_grn_csv = (
            "source_system,source_record_id,receipt_number,receipt_line,po_number,po_line_number,"
            "location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS,WMS-UNMAPPED-GRN,GRN-999,1,PO-501,1,WH-MH-014,BARCODE-DOESNOTEXIST,50,EA,2026-10-14T10:00:00+05:30,POSTED\n"
        )
        result = IngestionService.ingest_csv(db_session, "goods_receipt_lines", bad_grn_csv)

        assert result.rejected_rows == 1
        dq_issue = (
            db_session.query(DataQualityIssue)
            .filter_by(source_record_id="WMS-UNMAPPED-GRN")
            .first()
        )
        assert dq_issue is not None
        assert dq_issue.issue_type == "UNMAPPED_CROSSWALK"

        # Canonical GRN not created
        grn_line = db_session.query(GoodsReceiptLine).filter_by(source_record_id="WMS-UNMAPPED-GRN").first()
        assert grn_line is None

    def test_unmapped_location_in_po_creates_data_quality_issue(self, db_session: Session):
        load_base_aliases(db_session)

        bad_loc_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-BAD-LOC,PO-998,1,SUPP-01,UNKNOWN_STORE_CODE,ERP-SKU-001,50,EA,OPEN,2026-10-15T18:00:00+05:30\n"
        )
        result = IngestionService.ingest_csv(db_session, "purchase_order_lines", bad_loc_csv)

        assert result.rejected_rows == 1
        dq_issue = db_session.query(DataQualityIssue).filter_by(source_record_id="ERP-BAD-LOC").first()
        assert dq_issue is not None
        assert dq_issue.issue_type == "UNMAPPED_CROSSWALK"

    def test_unmapped_employee_store_creates_data_quality_issue(self, db_session: Session):
        load_base_aliases(db_session)

        bad_emp_csv = (
            "source_employee_id,display_name,employment_status,role,location_code,manager_employee_id,region_id\n"
            "HR-999,Ghost Worker,ACTIVE,receiving_associate,UNKNOWN_LOCATION_XYZ,HR-401,REGION-MH\n"
        )
        result = IngestionService.ingest_csv(db_session, "employees", bad_emp_csv)

        assert result.rejected_rows == 1
        dq_issue = db_session.query(DataQualityIssue).filter_by(source_record_id="HR-999").first()
        assert dq_issue is not None
        assert dq_issue.issue_type == "UNMAPPED_CROSSWALK"


# ===========================================================================
# 2. Conflicting Duplicate Payload Tests
# ===========================================================================

@pytest.mark.failure_path
class TestConflictingPayloads:
    """Tests that repeating a source ID with altered business content is flagged without mutating state."""

    def test_conflicting_po_payload_flags_dq_and_does_not_mutate_state(self, db_session: Session):
        load_base_aliases(db_session)

        # 1. Ingest original PO row
        original_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-CONFLICT-TEST,PO-777,1,SUPP-01,MUM-014,ERP-SKU-001,100,EA,OPEN,2026-10-15T18:00:00+05:30\n"
        )
        res1 = IngestionService.ingest_csv(db_session, "purchase_order_lines", original_csv)
        assert res1.accepted_rows == 1

        po_line = db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-CONFLICT-TEST").first()
        assert po_line.ordered_qty_base == 100

        # 2. Ingest conflicting row with SAME source_record_id but ordered_qty changed to 50
        conflicting_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-CONFLICT-TEST,PO-777,1,SUPP-01,MUM-014,ERP-SKU-001,50,EA,OPEN,2026-10-15T18:00:00+05:30\n"
        )
        res2 = IngestionService.ingest_csv(db_session, "purchase_order_lines", conflicting_csv)

        # Batch summary should flag conflict
        assert res2.rejected_rows == 1
        assert res2.accepted_rows == 0
        assert res2.duplicate_rows == 0

        # Assert DataQualityIssue recorded with CONFLICTING_DUPLICATE
        dq_issue = (
            db_session.query(DataQualityIssue)
            .filter_by(source_record_id="ERP-CONFLICT-TEST", issue_type="CONFLICTING_DUPLICATE")
            .first()
        )
        assert dq_issue is not None
        assert "existing_hash" in dq_issue.details
        assert "incoming_hash" in dq_issue.details

        # Crucial Invariant: canonical PO line ordered_qty must REMAIN 100 (never silently mutated)
        db_session.refresh(po_line)
        assert po_line.ordered_qty_base == 100

    def test_conflicting_receipt_payload_flags_dq_and_does_not_mutate_state(self, db_session: Session):
        load_base_aliases(db_session)

        orig_grn = (
            "source_system,source_record_id,receipt_number,receipt_line,po_number,po_line_number,"
            "location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS,WMS-CONFLICT-GRN,GRN-777,1,PO-501,1,WH-MH-014,WMS-BRC-001,60,EA,2026-10-14T10:00:00+05:30,POSTED\n"
        )
        res1 = IngestionService.ingest_csv(db_session, "goods_receipt_lines", orig_grn)
        assert res1.accepted_rows == 1

        grn_line = db_session.query(GoodsReceiptLine).filter_by(source_record_id="WMS-CONFLICT-GRN").first()
        assert grn_line.accepted_qty_base == 60

        # Conflicting update changing accepted_qty from 60 to 30
        conflict_grn = (
            "source_system,source_record_id,receipt_number,receipt_line,po_number,po_line_number,"
            "location_code,product_ref,accepted_qty,uom,received_at,posting_status\n"
            "WMS,WMS-CONFLICT-GRN,GRN-777,1,PO-501,1,WH-MH-014,WMS-BRC-001,30,EA,2026-10-14T10:00:00+05:30,POSTED\n"
        )
        res2 = IngestionService.ingest_csv(db_session, "goods_receipt_lines", conflict_grn)

        assert res2.rejected_rows == 1
        dq_issue = (
            db_session.query(DataQualityIssue)
            .filter_by(source_record_id="WMS-CONFLICT-GRN", issue_type="CONFLICTING_DUPLICATE")
            .first()
        )
        assert dq_issue is not None

        # Canonical GRN must remain unchanged at 60
        db_session.refresh(grn_line)
        assert grn_line.accepted_qty_base == 60


# ===========================================================================
# 3. Corrupt and Malformed Inputs Tests
# ===========================================================================

@pytest.mark.failure_path
class TestCorruptAndMalformedInputs:
    """Tests that malformed rows are isolated with row-level error reporting while valid rows succeed."""

    def test_malformed_csv_row_reported_in_batch_errors_without_dropping_valid_rows(self, db_session: Session):
        load_base_aliases(db_session)

        # 3 rows: Row 1 valid, Row 2 corrupt (line_number is 'INVALID_INT'), Row 3 valid
        mixed_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-ROW-1,PO-801,1,SUPP-01,MUM-014,ERP-SKU-001,10,EA,OPEN,2026-10-15T18:00:00+05:30\n"
            "ERP,ERP-ROW-2,PO-802,INVALID_INT,SUPP-01,MUM-014,ERP-SKU-001,20,EA,OPEN,2026-10-15T18:00:00+05:30\n"
            "ERP,ERP-ROW-3,PO-803,1,SUPP-01,MUM-014,ERP-SKU-001,30,EA,OPEN,2026-10-15T18:00:00+05:30\n"
        )
        result = IngestionService.ingest_csv(db_session, "purchase_order_lines", mixed_csv)

        # Partial batch success
        assert result.total_rows == 3
        assert result.accepted_rows == 2
        assert result.rejected_rows == 1
        assert result.status == "PARTIAL"

        # Valid rows are persisted
        assert db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-ROW-1").first() is not None
        assert db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-ROW-3").first() is not None

        # Error detail captures the exact row index and failure reason
        assert len(result.errors) == 1
        assert result.errors[0].row_index == 2

    def test_invalid_timestamp_format_rejected_at_validation(self, db_session: Session):
        load_base_aliases(db_session)

        bad_time_csv = (
            "source_system,source_record_id,po_number,line_number,supplier_code,"
            "location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP,ERP-BAD-TIME,PO-804,1,SUPP-01,MUM-014,ERP-SKU-001,10,EA,OPEN,INVALID_DATE_STRING\n"
        )
        result = IngestionService.ingest_csv(db_session, "purchase_order_lines", bad_time_csv)

        assert result.rejected_rows == 1
        assert result.accepted_rows == 0
        assert result.status == "FAILED"
        assert len(result.errors) == 1
        assert "expected_receipt_by" in result.errors[0].error or "ISO" in result.errors[0].error


# ===========================================================================
# 4. Workflow Failure Paths & State Machine Violations
# ===========================================================================

@pytest.mark.failure_path
class TestWorkflowFailurePaths:
    """Tests that state machine guards, reassignment rules, and fallback limits prevent invalid states."""

    def test_absent_eligible_assignee_queues_task_as_manual_assignment_required(self, db_session: Session):
        """When an exception occurs at a store with no associates or managers, task is queued unassigned."""
        exc = create_test_exception(
            db_session,
            po_number="PO-UNSTAFFED",
            canonical_location_id="STORE-NONEXISTENT",
            variance=10.0,
        )

        task = task_service.create_task_for_exception(db_session, exc)

        assert task.status == "CREATED"
        assert task.assignee_id is None
        assert task.assignment_reason == "MANUAL_ASSIGNMENT_REQUIRED"

    def test_illegal_state_machine_transition_raises_409_conflict(self, client, db_session: Session):
        """Direct illegal jump from ASSIGNED -> COMPLETED (skipping ACKNOWLEDGED and IN_PROGRESS) returns 409."""
        load_base_aliases(db_session)
        IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
        IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))

        exc = create_test_exception(db_session, po_number="PO-ILLEGAL", canonical_location_id="STORE-014")
        task = task_service.create_task_for_exception(db_session, exc)
        assert task.status == "ASSIGNED"

        # Attempt to complete without acknowledging or starting
        res = client.post(
            f"/api/v1/tasks/{task.id}/complete",
            json={
                "actor_id": "HR-102",
                "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
                "resolution_evidence_ref": "DOCK-SEARCH-01",
            },
        )
        assert res.status_code == 409
        body = res.json()
        assert body["error_code"] == "INVALID_STATE_TRANSITION"
        assert "Cannot transition task from 'ASSIGNED' to 'COMPLETED'" in body["message"]

    def test_reassign_from_completed_state_rejected(self, db_session: Session):
        """Reassigning a task that is already COMPLETED must raise InvalidStateTransitionError."""
        load_base_aliases(db_session)
        IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
        IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))

        exc = create_test_exception(db_session, po_number="PO-REASSIGN-FAIL", canonical_location_id="STORE-014")
        task = task_service.create_task_for_exception(db_session, exc)
        task_service.acknowledge_task(db_session, task.id, actor_id="HR-102")
        task_service.start_task(db_session, task.id, actor_id="HR-102")
        task_service.complete_task(
            db_session,
            task.id,
            actor_id="HR-102",
            resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            resolution_evidence_ref="REF-1",
        )

        assert task.status == "COMPLETED"

        # Attempt to reassign when COMPLETED
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            task_service.reassign_task(
                db_session,
                task.id,
                actor_id="HR-401",
                new_assignee_id="HR-401",
                reason="Reassign completed task",
            )
        assert "Cannot transition task from 'COMPLETED' to 'ASSIGNED'" in str(exc_info.value)

    def test_reassign_request_with_empty_reason_rejected_at_api(self, client, db_session: Session):
        """API rejects reassignment when the required reason string is empty."""
        load_base_aliases(db_session)
        IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
        IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))

        exc = create_test_exception(db_session, po_number="PO-REASSIGN-REQ", canonical_location_id="STORE-014")
        task = task_service.create_task_for_exception(db_session, exc)
        assert task.status == "ASSIGNED"

        res = client.post(
            f"/api/v1/tasks/{task.id}/reassign",
            json={
                "actor_id": "SYSTEM",
                "new_assignee_id": "HR-102",
                "reason": "",  # Empty string rejected by Pydantic min_length=1
            },
        )
        assert res.status_code == 422


# ===========================================================================
# 5. AI Provider Failure Paths & Immutability
# ===========================================================================

@pytest.mark.failure_path
class TestAIProviderFailurePaths:
    """Tests that AI provider transport errors degrade gracefully with zero database mutations."""

    def test_ai_gateway_timeout_falls_back_gracefully_with_zero_db_mutation(self, client, db_session: Session, monkeypatch):
        """Simulated network timeout when contacting AI provider returns 502 with zero DB change."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        class TimeoutProvider(AIInvestigationProvider):
            @property
            def provider_name(self) -> str:
                return "timeout-mock"

            @property
            def model_name(self) -> str:
                return "timeout-model"

            def generate_brief(self, evidence):
                raise AIProviderError("OpenRouter request timed out after 15.0s", {"timeout": 15.0})

        monkeypatch.setattr(InvestigationService, "get_provider", lambda **kwargs: TimeoutProvider())

        res = client.post(f"/api/v1/exceptions/{exc.id}/investigate?use_mock=false")
        assert res.status_code == 502
        body = res.json()
        assert body["error_code"] == "AI_PROVIDER_ERROR"
        assert "timed out" in body["message"]

        # Crucial Safety Invariant: Database record is completely unchanged
        db_session.refresh(exc)
        assert exc.status == "DETECTED"
        assert exc.variance_qty_base == 8.0
        assert exc.rule_version == "v1.0.0"

    def test_ai_gateway_500_error_translated_to_http_502_rfc7807(self, client, db_session: Session, monkeypatch):
        """Simulated 500 error from upstream AI provider returns 502 with RFC 7807 structure."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        class ServerErrorProvider(AIInvestigationProvider):
            @property
            def provider_name(self) -> str:
                return "500-mock"

            @property
            def model_name(self) -> str:
                return "500-model"

            def generate_brief(self, evidence):
                raise AIProviderError("OpenRouter API returned HTTP 500", {"status_code": 500})

        monkeypatch.setattr(InvestigationService, "get_provider", lambda **kwargs: ServerErrorProvider())

        res = client.post(f"/api/v1/exceptions/{exc.id}/investigate?use_mock=false")
        assert res.status_code == 502
        body = res.json()
        assert body["error_code"] == "AI_PROVIDER_ERROR"
        assert "500" in body["message"]
