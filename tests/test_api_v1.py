"""
Comprehensive REST API Layer Tests (Phase 6).
Tests Ingestion (CSV, JSON, Multipart), Data Quality, Reconciliation, Detailed Exceptions,
Task Workflows, and Global RFC 7807 Error Handling Envelopes.
"""
import io
import pytest
from fastapi.testclient import TestClient


class TestAPIImportsAndDataQuality:
    """Tests for POST /api/v1/imports/{dataset} and GET /api/v1/data-quality."""

    def test_import_json_list_payload(self, client: TestClient):
        """POST /api/v1/imports/product_aliases with direct JSON array."""
        payload = [
            {
                "source_system": "ERP",
                "external_item_id": "ERP-SKU-999",
                "canonical_product_id": "SKU-999",
                "source_uom": "BOX",
                "base_uom": "EA",
                "to_base_factor": 12.0,
            }
        ]
        resp = client.post("/api/v1/imports/product_aliases", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["dataset_name"] == "product_aliases"
        assert data["accepted_rows"] == 1
        assert data["status"] == "COMPLETED"

    def test_import_json_dict_with_records_key(self, client: TestClient):
        """POST /api/v1/imports/location_aliases with {'records': [...]} structure."""
        payload = {
            "records": [
                {
                    "source_system": "ERP",
                    "external_location_id": "LOC-STORE-99",
                    "canonical_location_id": "STORE-099",
                    "region_id": "REGION-TEST",
                }
            ]
        }
        resp = client.post("/api/v1/imports/location_aliases", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["accepted_rows"] == 1

    def test_import_csv_text_body(self, client: TestClient):
        """POST /api/v1/imports/product_aliases with raw CSV string."""
        csv_data = "source_system,external_item_id,canonical_product_id,source_uom,base_uom,to_base_factor\nERP,ERP-SKU-888,SKU-888,EA,EA,1.0\n"
        resp = client.post(
            "/api/v1/imports/product_aliases",
            content=csv_data,
            headers={"Content-Type": "text/csv"},
        )
        assert resp.status_code == 201
        assert resp.json()["accepted_rows"] == 1

    def test_import_multipart_form_data(self, client: TestClient):
        """POST /api/v1/imports/product_aliases via multipart file upload."""
        csv_data = "source_system,external_item_id,canonical_product_id,source_uom,base_uom,to_base_factor\nERP,ERP-SKU-777,SKU-777,EA,EA,1.0\n"
        files = {"file": ("aliases.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        resp = client.post("/api/v1/imports/product_aliases", files=files)
        assert resp.status_code == 201
        assert resp.json()["accepted_rows"] == 1

    def test_import_unknown_dataset_returns_400(self, client: TestClient):
        """POST /api/v1/imports/unknown_dataset returns 400 Bad Request."""
        resp = client.post("/api/v1/imports/unknown_dataset", json=[{"foo": "bar"}])
        assert resp.status_code == 400
        assert "Unknown dataset name" in resp.json()["detail"]

    def test_get_import_batch_status(self, client: TestClient):
        """GET /api/v1/imports/{import_id} retrieves batch summary."""
        payload = [
            {
                "source_system": "ERP",
                "external_item_id": "ERP-SKU-666",
                "canonical_product_id": "SKU-666",
                "source_uom": "EA",
                "base_uom": "EA",
                "to_base_factor": 1.0,
            }
        ]
        created = client.post("/api/v1/imports/product_aliases", json=payload).json()
        batch_id = created["batch_id"]

        resp = client.get(f"/api/v1/imports/{batch_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_id"] == batch_id
        assert data["accepted_rows"] == 1

    def test_get_import_batch_not_found(self, client: TestClient):
        """GET /api/v1/imports/{unknown_id} returns 404."""
        resp = client.get("/api/v1/imports/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    def test_data_quality_endpoint_with_filters(self, client: TestClient):
        """GET /api/v1/data-quality returns logged crosswalk anomalies."""
        # Ingest a PO line referencing unmapped SKU to trigger DataQualityIssue
        po_line = [
            {
                "source_system": "ERP",
                "source_record_id": "ERP-UNMAPPED-PO-1",
                "po_number": "PO-999",
                "line_number": 1,
                "supplier_code": "SUPP-01",
                "item_code": "UNKNOWN-SKU",
                "location_code": "UNKNOWN-LOC",
                "ordered_qty": 10.0,
                "ordered_uom": "EA",
                "status": "OPEN",
                "expected_receipt_by": "2026-10-15T18:00:00+05:30",
            }
        ]
        client.post("/api/v1/imports/purchase_order_lines", json=po_line)

        # Query data quality
        resp = client.get("/api/v1/data-quality?dataset=purchase_order_lines")
        assert resp.status_code == 200
        issues = resp.json()
        assert len(issues) >= 1
        assert any(i["source_record_id"] == "ERP-UNMAPPED-PO-1" for i in issues)


class TestAPIReconciliationAndDetailedException:
    """Tests for Reconciliation and Detailed Exception retrieval."""

    def _setup_po501_scenario(self, client: TestClient):
        """Loads fixtures for PO-501 (ordered 100, received 60+32 = 92 -> 8 shortfall)."""
        client.post("/api/v1/imports/product_aliases", json=[
            {"source_system": "ERP", "external_item_id": "MAT-1001", "canonical_product_id": "SKU-001", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
            {"source_system": "WMS", "external_item_id": "WMS-MAT-1001", "canonical_product_id": "SKU-001", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ])
        client.post("/api/v1/imports/location_aliases", json=[
            {"source_system": "ERP", "external_location_id": "STORE-14", "canonical_location_id": "STORE-014", "region_id": "REGION-MH"},
            {"source_system": "WMS", "external_location_id": "WH-STORE-14", "canonical_location_id": "STORE-014", "region_id": "REGION-MH"},
            {"source_system": "HRMS", "external_location_id": "MUM-STORE-014", "canonical_location_id": "STORE-014", "region_id": "REGION-MH"},
        ])
        client.post("/api/v1/imports/employees", json=[
            {"source_system": "HRMS", "source_employee_id": "HR-102", "display_name": "Dev Shah", "role": "receiving_associate", "employment_status": "ACTIVE", "location_code": "MUM-STORE-014"},
        ])
        client.post("/api/v1/imports/workforce_status", json=[
            {"source_system": "WFM", "source_employee_id": "HR-102", "availability_status": "ON_SHIFT", "shift_start": "2026-10-16T08:00:00+05:30", "shift_end": "2026-10-16T17:00:00+05:30", "observed_at": "2026-10-16T14:30:00+05:30"},
        ])
        client.post("/api/v1/imports/purchase_order_lines", json=[
            {"source_system": "ERP", "source_record_id": "ERP-PO501-L1", "po_number": "PO-501", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "MAT-1001", "location_code": "STORE-14", "ordered_qty": 100.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"},
        ])
        client.post("/api/v1/imports/goods_receipt_lines", json=[
            {"source_system": "WMS", "source_record_id": "WMS-GRN501-A-L1", "receipt_number": "GRN-501-A", "receipt_line": 1, "po_number": "PO-501", "po_line_number": 1, "product_ref": "WMS-MAT-1001", "location_code": "WH-STORE-14", "accepted_qty": 60.0, "uom": "EA", "received_at": "2026-10-15T10:00:00+05:30", "posting_status": "POSTED"},
            {"source_system": "WMS", "source_record_id": "WMS-GRN501-B-L1", "receipt_number": "GRN-501-B", "receipt_line": 1, "po_number": "PO-501", "po_line_number": 1, "product_ref": "WMS-MAT-1001", "location_code": "WH-STORE-14", "accepted_qty": 32.0, "uom": "EA", "received_at": "2026-10-15T14:00:00+05:30", "posting_status": "POSTED"},
        ])

    def test_run_reconciliation_creates_task_and_exception(self, client: TestClient):
        """POST /api/v1/reconciliation/run detects PO-501 shortfall and routes task."""
        self._setup_po501_scenario(client)
        resp = client.post("/api/v1/reconciliation/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["shortfalls_detected"] == 1
        assert len(data["new_exceptions"]) == 1

        exc = data["new_exceptions"][0]
        assert exc["po_number"] == "PO-501"
        assert exc["variance_qty_base"] == 8.0

    def test_get_detailed_exception_endpoint(self, client: TestClient):
        """GET /api/v1/exceptions/{id} returns composite details with PO line, GRNs, and task."""
        self._setup_po501_scenario(client)
        run_resp = client.post("/api/v1/reconciliation/run").json()
        exc_id = run_resp["new_exceptions"][0]["id"]

        detail_resp = client.get(f"/api/v1/exceptions/{exc_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()

        # Check base exception fields
        assert detail["id"] == exc_id
        assert detail["variance_qty_base"] == 8.0
        assert "neutral_summary" in detail
        assert "missing_evidence" in detail

        # Check associated PO line details
        assert detail["purchase_order_line"] is not None
        assert detail["purchase_order_line"]["po_number"] == "PO-501"
        assert detail["purchase_order_line"]["ordered_qty_base"] == 100.0

        # Check associated Goods Receipt lines
        assert len(detail["goods_receipt_lines"]) == 2
        receipt_numbers = {g["receipt_number"] for g in detail["goods_receipt_lines"]}
        assert "GRN-501-A" in receipt_numbers
        assert "GRN-501-B" in receipt_numbers

        # Check associated active task
        assert detail["task"] is not None
        assert detail["task"]["status"] == "ASSIGNED"
        assert detail["task"]["assignee_id"] == "HR-102"

    def test_get_exception_not_found_returns_error_envelope(self, client: TestClient):
        """GET /api/v1/exceptions/{unknown_id} returns 404."""
        resp = client.get("/api/v1/exceptions/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data


class TestAPITaskWorkflowsAndGlobalErrorHandling:
    """Tests for Task workflows and Global RFC 7807 Exception Envelopes."""

    def _setup_active_task(self, client: TestClient) -> str:
        """Sets up an assigned task and returns its ID."""
        client.post("/api/v1/imports/product_aliases", json=[
            {"source_system": "ERP", "external_item_id": "M-1", "canonical_product_id": "SKU-1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
            {"source_system": "WMS", "external_item_id": "W-1", "canonical_product_id": "SKU-1", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ])
        client.post("/api/v1/imports/location_aliases", json=[
            {"source_system": "ERP", "external_location_id": "L-1", "canonical_location_id": "STORE-001", "region_id": "REG-1"},
            {"source_system": "WMS", "external_location_id": "WL-1", "canonical_location_id": "STORE-001", "region_id": "REG-1"},
            {"source_system": "HRMS", "external_location_id": "HL-1", "canonical_location_id": "STORE-001", "region_id": "REG-1"},
        ])
        client.post("/api/v1/imports/employees", json=[
            {"source_system": "HRMS", "source_employee_id": "HR-888", "display_name": "Test Associate", "role": "receiving_associate", "employment_status": "ACTIVE", "location_code": "HL-1"},
            {"source_system": "HRMS", "source_employee_id": "MGR-01", "display_name": "Store Manager", "role": "store_manager", "employment_status": "ACTIVE", "location_code": "HL-1"},
        ])
        client.post("/api/v1/imports/workforce_status", json=[
            {"source_system": "WFM", "source_employee_id": "HR-888", "availability_status": "ON_SHIFT", "shift_start": "2026-10-16T08:00:00+05:30", "shift_end": "2026-10-16T17:00:00+05:30", "observed_at": "2026-10-16T14:30:00+05:30"},
        ])
        client.post("/api/v1/imports/purchase_order_lines", json=[
            {"source_system": "ERP", "source_record_id": "ERP-P1-L1", "po_number": "PO-888", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "M-1", "location_code": "L-1", "ordered_qty": 50.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"},
        ])
        client.post("/api/v1/reconciliation/run")
        tasks = client.get("/api/v1/tasks").json()["items"]
        return tasks[0]["id"]

    def test_global_handler_invalid_state_transition(self, client: TestClient):
        """Illegal state jump ASSIGNED -> COMPLETED directly returns HTTP 409 with structured error."""
        task_id = self._setup_active_task(client)

        resp = client.post(f"/api/v1/tasks/{task_id}/complete", json={
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "REF-INVALID-JUMP",
        }, headers={"X-User-ID": "HR-888"})
        assert resp.status_code == 409
        data = resp.json()
        assert data["error_code"] == "INVALID_STATE_TRANSITION"
        assert "Cannot transition task" in data["message"]
        assert "from_status" in data["details"]

    def test_global_handler_resource_not_found(self, client: TestClient):
        """Requesting an unknown task ID returns HTTP 404 with structured error."""
        resp = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error_code"] == "RESOURCE_NOT_FOUND"
        assert "not found" in data["message"].lower()

    def test_task_verification_resolves_linked_exception(self, client: TestClient):
        """Verifying a task transitions it to VERIFIED and marks OperationalException as RESOLVED."""
        task_id = self._setup_active_task(client)

        # Progression: ASSIGNED -> ACKNOWLEDGED -> IN_PROGRESS -> COMPLETED -> PENDING_VERIFICATION -> VERIFIED
        client.post(f"/api/v1/tasks/{task_id}/acknowledge", headers={"X-User-ID": "HR-888"})
        client.post(f"/api/v1/tasks/{task_id}/start", headers={"X-User-ID": "HR-888"})
        client.post(f"/api/v1/tasks/{task_id}/complete", json={
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "REF-9999",
        }, headers={"X-User-ID": "HR-888"})
        client.post(f"/api/v1/tasks/{task_id}/submit-verification", headers={"X-User-ID": "HR-888"})
        verify_resp = client.post(f"/api/v1/tasks/{task_id}/verify", json={"notes": "Audited"}, headers={"X-User-ID": "MGR-01"})
        assert verify_resp.status_code == 200
        assert verify_resp.json()["status"] == "VERIFIED"

        # Check linked exception status
        task_detail = client.get(f"/api/v1/tasks/{task_id}").json()
        exc_id = task_detail["exception_id"]
        exc_detail = client.get(f"/api/v1/exceptions/{exc_id}").json()
        assert exc_detail["status"] == "RESOLVED"
        assert exc_detail["resolved_at"] is not None
