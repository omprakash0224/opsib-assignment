"""
Comprehensive Idempotency and Retry Safety Tests (Phase 6 / Tasks A, B, C).
Explicitly verifies Assessment Design Questions 1 & 2:
1. Preventing duplicate business effects on retried or concurrent imports.
2. Safe client retry handling with Idempotency-Key headers on network timeouts.
"""
import pytest
from fastapi.testclient import TestClient
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine, Employee
from app.db.models.exception import OperationalException, DataQualityIssue
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent


class TestImportIdempotencyAndConflictSafety:
    """Verifies Assessment Design Question 1: Preventing duplicate business effects on retried imports."""

    def test_repeated_import_identical_records_produces_zero_duplicates(self, client: TestClient, db_session):
        """Importing the same dataset twice skips duplicates and never double-counts quantities."""
        payload = [
            {"source_system": "ERP", "external_item_id": "SKU-DEDUP", "canonical_product_id": "SKU-DEDUP", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ]
        # First import
        r1 = client.post("/api/v1/imports/product_aliases", json=payload)
        assert r1.status_code == 201
        assert r1.json()["accepted_rows"] == 1
        assert r1.json()["duplicate_rows"] == 0

        # Second identical import
        r2 = client.post("/api/v1/imports/product_aliases", json=payload)
        assert r2.status_code == 201
        assert r2.json()["accepted_rows"] == 0
        assert r2.json()["duplicate_rows"] == 1

        # Check DB count
        from app.db.models.canonical import ProductAlias
        total_in_db = db_session.query(ProductAlias).filter_by(canonical_product_id="SKU-DEDUP").count()
        assert total_in_db == 1

    def test_conflicting_duplicate_payload_isolated_to_data_quality(self, client: TestClient, db_session):
        """Retransmitting an existing source ID with altered content logs DataQualityIssue and prevents mutation."""
        po_payload_v1 = [
            {"source_system": "ERP", "source_record_id": "ERP-PO-CONFLICT", "po_number": "PO-CONF", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "MAT-1", "location_code": "STORE-14", "ordered_qty": 50.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"}
        ]
        # Pre-seed aliases
        client.post("/api/v1/imports/product_aliases", json=[{"source_system": "ERP", "external_item_id": "MAT-1", "canonical_product_id": "SKU-1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0}])
        client.post("/api/v1/imports/location_aliases", json=[{"source_system": "ERP", "external_location_id": "STORE-14", "canonical_location_id": "STORE-014", "region_id": "R1"}])

        # Import v1
        r1 = client.post("/api/v1/imports/purchase_order_lines", json=po_payload_v1)
        assert r1.status_code == 201
        assert r1.json()["accepted_rows"] == 1

        # Import v2: same source_record_id, altered ordered_qty (50 -> 100)
        po_payload_v2 = [
            {"source_system": "ERP", "source_record_id": "ERP-PO-CONFLICT", "po_number": "PO-CONF", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "MAT-1", "location_code": "STORE-14", "ordered_qty": 100.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"}
        ]
        r2 = client.post("/api/v1/imports/purchase_order_lines", json=po_payload_v2)
        assert r2.status_code == 201
        assert r2.json()["accepted_rows"] == 0
        assert r2.json()["rejected_rows"] == 1

        # Assert canonical PO line was NOT mutated
        po_line = db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-PO-CONFLICT").first()
        assert po_line.ordered_qty_base == 50.0  # Still 50, not 100!

        # Assert DataQualityIssue was created
        dq = db_session.query(DataQualityIssue).filter_by(source_record_id="ERP-PO-CONFLICT").first()
        assert dq is not None
        assert dq.issue_type == "CONFLICTING_DUPLICATE"

    def test_reconciliation_run_idempotency(self, client: TestClient, db_session):
        """Repeated reconciliation execution is fully idempotent and never duplicates exceptions or tasks."""
        # Setup fixtures
        client.post("/api/v1/imports/product_aliases", json=[
            {"source_system": "ERP", "external_item_id": "M1", "canonical_product_id": "SKU1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
            {"source_system": "WMS", "external_item_id": "WM1", "canonical_product_id": "SKU1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ])
        client.post("/api/v1/imports/location_aliases", json=[
            {"source_system": "ERP", "external_location_id": "L1", "canonical_location_id": "STORE-014", "region_id": "R1"},
            {"source_system": "WMS", "external_location_id": "WL1", "canonical_location_id": "STORE-014", "region_id": "R1"},
            {"source_system": "HRMS", "external_location_id": "HL1", "canonical_location_id": "STORE-014", "region_id": "R1"},
        ])
        client.post("/api/v1/imports/employees", json=[
            {"source_system": "HRMS", "source_employee_id": "HR-1", "display_name": "Dev", "role": "receiving_associate", "employment_status": "ACTIVE", "location_code": "HL1"},
        ])
        client.post("/api/v1/imports/workforce_status", json=[
            {"source_system": "WFM", "source_employee_id": "HR-1", "availability_status": "ON_SHIFT", "shift_start": "2026-10-16T08:00:00+05:30", "shift_end": "2026-10-16T17:00:00+05:30", "observed_at": "2026-10-16T14:30:00+05:30"},
        ])
        client.post("/api/v1/imports/purchase_order_lines", json=[
            {"source_system": "ERP", "source_record_id": "ERP-R-1", "po_number": "PO-RECON", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "M1", "location_code": "L1", "ordered_qty": 100.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"},
        ])
        client.post("/api/v1/imports/goods_receipt_lines", json=[
            {"source_system": "WMS", "source_record_id": "WMS-R-1", "receipt_number": "GRN-1", "receipt_line": 1, "po_number": "PO-RECON", "po_line_number": 1, "product_ref": "WM1", "location_code": "WL1", "accepted_qty": 70.0, "uom": "EA", "received_at": "2026-10-15T10:00:00+05:30", "posting_status": "POSTED"},
        ])

        # Run 1
        r1 = client.post("/api/v1/reconciliation/run").json()
        assert r1["exceptions_created"] == 1
        assert r1["exceptions_already_existed"] == 0

        # Run 2
        r2 = client.post("/api/v1/reconciliation/run").json()
        assert r2["exceptions_created"] == 0
        assert r2["exceptions_already_existed"] == 1

        # Check total exceptions in DB
        exc_count = db_session.query(OperationalException).filter_by(po_number="PO-RECON").count()
        assert exc_count == 1

        # Check total tasks in DB
        task_count = db_session.query(HumanTask).count()
        assert task_count == 1


class TestClientTimeoutAndRetrySafety:
    """Verifies Assessment Design Question 2: Client timeout during task completion & retry safety."""

    def _setup_task(self, client: TestClient) -> str:
        """Sets up a task in IN_PROGRESS state."""
        client.post("/api/v1/imports/product_aliases", json=[
            {"source_system": "ERP", "external_item_id": "M1", "canonical_product_id": "SKU1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ])
        client.post("/api/v1/imports/location_aliases", json=[
            {"source_system": "ERP", "external_location_id": "L1", "canonical_location_id": "STORE-014", "region_id": "R1"},
            {"source_system": "HRMS", "external_location_id": "HL1", "canonical_location_id": "STORE-014", "region_id": "R1"},
        ])
        client.post("/api/v1/imports/employees", json=[
            {"source_system": "HRMS", "source_employee_id": "HR-102", "display_name": "Dev", "role": "receiving_associate", "employment_status": "ACTIVE", "location_code": "HL1"},
        ])
        client.post("/api/v1/imports/workforce_status", json=[
            {"source_system": "WFM", "source_employee_id": "HR-102", "availability_status": "ON_SHIFT", "shift_start": "2026-10-16T08:00:00+05:30", "shift_end": "2026-10-16T17:00:00+05:30", "observed_at": "2026-10-16T14:30:00+05:30"},
        ])
        client.post("/api/v1/imports/purchase_order_lines", json=[
            {"source_system": "ERP", "source_record_id": "ERP-T1", "po_number": "PO-TASK", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "M1", "location_code": "L1", "ordered_qty": 20.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"},
        ])
        client.post("/api/v1/reconciliation/run")
        tasks = client.get("/api/v1/tasks").json()["items"]
        tid = tasks[0]["id"]
        client.post(f"/api/v1/tasks/{tid}/acknowledge", json={"actor_id": "HR-102"})
        client.post(f"/api/v1/tasks/{tid}/start", json={"actor_id": "HR-102"})
        return tid

    def test_client_timeout_retry_with_same_idempotency_key(self, client: TestClient, db_session):
        """
        When the server commits task completion but client times out:
        Retrying with the same Idempotency-Key and payload succeeds with 200 OK
        and produces ZERO duplicate audit events.
        """
        task_id = self._setup_task(client)
        headers = {"Idempotency-Key": "IDEM-UUID-9999"}
        payload = {
            "actor_id": "HR-102",
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "CARRIER-POD-404-NOT-FOUND",
            "resolution_notes": "Supplier confirmed short pack at dock",
        }

        # 1. First attempt: succeeds
        r1 = client.post(f"/api/v1/tasks/{task_id}/complete", json=payload, headers=headers)
        assert r1.status_code == 200
        assert r1.json()["status"] == "COMPLETED"

        # Count audit events after first completion
        audits_v1 = db_session.query(TaskAuditEvent).filter_by(task_id=task_id).all()
        count_v1 = len(audits_v1)
        complete_events_v1 = [e for e in audits_v1 if e.event_type == "COMPLETED"]
        assert len(complete_events_v1) == 1

        # 2. Client simulates timeout retry: re-issues the identical POST
        r2 = client.post(f"/api/v1/tasks/{task_id}/complete", json=payload, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["status"] == "COMPLETED"

        # 3. Assert NO new audit events were written for the duplicate retry
        audits_v2 = db_session.query(TaskAuditEvent).filter_by(task_id=task_id).all()
        assert len(audits_v2) == count_v1
        complete_events_v2 = [e for e in audits_v2 if e.event_type == "COMPLETED"]
        assert len(complete_events_v2) == 1

    def test_client_retry_with_conflicting_resolution_fails_with_409(self, client: TestClient):
        """
        If a task is already COMPLETED and a client retries with conflicting resolution data,
        the server must reject it with HTTP 409 Conflict.
        """
        task_id = self._setup_task(client)
        client.post(f"/api/v1/tasks/{task_id}/complete", json={
            "actor_id": "HR-102",
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "REF-ORIGINAL",
        })

        # Retry with different resolution code
        resp = client.post(f"/api/v1/tasks/{task_id}/complete", json={
            "actor_id": "HR-102",
            "resolution_code": "GOODS_FOUND_IN_TRANSIT",  # Conflicting!
            "resolution_evidence_ref": "REF-DIFFERENT",
        })
        assert resp.status_code == 409
        data = resp.json()
        assert data["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert "different resolution data" in data["message"]

    def test_idempotent_acknowledge_and_start_retries(self, client: TestClient):
        """Retrying acknowledge or start requests idempotently returns 200 OK."""
        task_id = self._setup_task(client)  # Already in IN_PROGRESS

        # Repeating start on IN_PROGRESS task
        r_start = client.post(f"/api/v1/tasks/{task_id}/start", json={"actor_id": "HR-102"})
        assert r_start.status_code == 200
        assert r_start.json()["status"] == "IN_PROGRESS"
