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
        client.post(f"/api/v1/tasks/{tid}/acknowledge", headers={"X-User-ID": "HR-102"})
        client.post(f"/api/v1/tasks/{tid}/start", headers={"X-User-ID": "HR-102"})
        return tid

    def test_client_timeout_retry_with_same_idempotency_key(self, client: TestClient, db_session):
        """
        When the server commits task completion but client times out:
        Retrying with the same Idempotency-Key and payload succeeds with 200 OK
        and produces ZERO duplicate audit events.
        """
        task_id = self._setup_task(client)
        headers = {"Idempotency-Key": "IDEM-UUID-9999", "X-User-ID": "HR-102"}
        payload = {
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
            "resolution_code": "SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            "resolution_evidence_ref": "REF-ORIGINAL",
        }, headers={"X-User-ID": "HR-102"})

        # Retry with different resolution code
        resp = client.post(f"/api/v1/tasks/{task_id}/complete", json={
            "resolution_code": "GOODS_FOUND_IN_TRANSIT",  # Conflicting!
            "resolution_evidence_ref": "REF-DIFFERENT",
        }, headers={"X-User-ID": "HR-102"})
        assert resp.status_code == 409
        data = resp.json()
        assert data["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert "different resolution data" in data["message"]

    def test_idempotent_acknowledge_and_start_retries(self, client: TestClient):
        """Retrying acknowledge or start requests idempotently returns 200 OK."""
        task_id = self._setup_task(client)  # Already in IN_PROGRESS

        # Repeating start on IN_PROGRESS task
        r_start = client.post(f"/api/v1/tasks/{task_id}/start", headers={"X-User-ID": "HR-102"})
        assert r_start.status_code == 200
        assert r_start.json()["status"] == "IN_PROGRESS"


class TestBug6RaceHandlingAndConcurrency:
    """
    Verifies Bug #6: Race handling on exception and task creation,
    optimistic locking on task transitions, and multi-tenant isolation.
    """

    def test_concurrent_create_task_for_same_exception(self, tmp_path):
        """
        Two concurrent threads/sessions calling create_task_for_exception for the same exception:
        Exactly one task created, no unhandled IntegrityError or 500 raised.
        """
        import concurrent.futures
        import threading
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from app.db.base import Base
        from app.workflows.task_service import create_task_for_exception
        from app.db.models.exception import OperationalException
        from app.db.models.task import HumanTask

        db_path = tmp_path / "race_test.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(bind=engine)

        SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)
        s_setup = SessionMaker()
        exc = OperationalException(
            tenant_id="TENANT-001",
            exception_key="TENANT-001:PO-RACE-1:1",
            exception_type="RECEIPT_SHORTFALL",
            status="DETECTED",
            po_number="PO-RACE-1",
            po_line_number=1,
            canonical_product_id="SKU-1",
            canonical_location_id="STORE-1",
            expected_qty_base=100.0,
            received_qty_base=50.0,
            variance_qty_base=50.0,
            base_uom="EA",
            revision=1,
            evidence_records=["R1"],
            neutral_summary="Shortfall",
            missing_evidence={},
        )
        s_setup.add(exc)
        s_setup.commit()
        exc_id = exc.id
        s_setup.close()

        barrier = threading.Barrier(2)
        tasks_created = []
        errors = []

        def worker():
            s = SessionMaker()
            try:
                local_exc = s.query(OperationalException).filter_by(id=exc_id).first()
                barrier.wait(timeout=5)
                t = create_task_for_exception(s, local_exc, tenant_id="TENANT-001")
                tasks_created.append(t.id)
            except Exception as e:
                errors.append(e)
            finally:
                s.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker), executor.submit(worker)]
            for f in futures:
                f.result()

        assert len(errors) == 0, f"Errors encountered: {errors}"
        assert len(tasks_created) == 2
        assert tasks_created[0] == tasks_created[1]

        s_verify = SessionMaker()
        db_tasks = s_verify.query(HumanTask).filter_by(exception_id=exc_id).all()
        assert len(db_tasks) == 1
        s_verify.close()
        engine.dispose()

    def test_concurrent_upsert_shortfall_exception(self, tmp_path):
        """
        Two concurrent threads/sessions inserting the same shortfall exception:
        IntegrityError on exception_key collision is handled gracefully, exactly one exception created.
        """
        import concurrent.futures
        import threading
        from datetime import datetime, timezone
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from app.db.base import Base
        from app.detection.service import DetectionService
        from app.detection.detector import ShortfallFinding
        from app.db.models.exception import OperationalException

        db_path = tmp_path / "race_exc_test.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(bind=engine)
        SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)

        finding = ShortfallFinding(
            po_number="PO-RACE-EXC",
            po_line_number=1,
            canonical_product_id="SKU-1",
            canonical_location_id="STORE-1",
            expected_qty_base=100.0,
            received_qty_base=50.0,
            variance_qty_base=50.0,
            base_uom="EA",
            evidence_records=["PO-1"],
            neutral_summary="Shortfall detected",
            exception_key="TENANT-001:PO-RACE-EXC:1",
        )

        barrier = threading.Barrier(2)
        errors = []

        def worker():
            s = SessionMaker()
            try:
                barrier.wait(timeout=5)
                DetectionService._upsert_shortfall_exception(
                    session=s,
                    finding=finding,
                    tenant_id="TENANT-001",
                    mapping_version="map-test",
                    evaluation_time=datetime.now(timezone.utc),
                )
                s.commit()
            except Exception as e:
                s.rollback()
                errors.append(e)
            finally:
                s.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker), executor.submit(worker)]
            for f in futures:
                f.result()

        assert len(errors) == 0, f"Errors encountered: {errors}"
        s_verify = SessionMaker()
        excs = s_verify.query(OperationalException).filter_by(exception_key="TENANT-001:PO-RACE-EXC:1").all()
        assert len(excs) == 1
        s_verify.close()
        engine.dispose()

    def test_concurrent_complete_task_optimistic_locking(self, fresh_db_engine):
        """
        Two concurrent sessions calling complete_task on the same task simultaneously:
        One succeeds, the other raises StaleDataError or returns idempotent state.
        Exactly one COMPLETED audit event is written.
        """
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.orm.exc import StaleDataError
        from app.workflows.task_service import complete_task
        from app.db.models.task import HumanTask
        from app.db.models.audit import TaskAuditEvent
        from app.db.models.exception import OperationalException

        SessionMaker = sessionmaker(bind=fresh_db_engine, expire_on_commit=False)
        s_setup = SessionMaker()
        exc = OperationalException(
            tenant_id="TENANT-001",
            exception_key="TENANT-001:PO-RACE-2:1",
            exception_type="RECEIPT_SHORTFALL",
            status="DETECTED",
            po_number="PO-RACE-2",
            po_line_number=1,
            canonical_product_id="SKU-1",
            canonical_location_id="STORE-1",
            expected_qty_base=100.0,
            received_qty_base=50.0,
            variance_qty_base=50.0,
            base_uom="EA",
            revision=1,
            evidence_records=["R1"],
            neutral_summary="Shortfall",
            missing_evidence={},
        )
        s_setup.add(exc)
        s_setup.flush()

        task = HumanTask(
            tenant_id="TENANT-001",
            exception_id=exc.id,
            task_key="TENANT-001:TASK:PO-RACE-2:1",
            status="IN_PROGRESS",
            canonical_location_id="STORE-1",
        )
        s_setup.add(task)
        s_setup.commit()
        task_id = task.id
        s_setup.close()

        # Session 1 and Session 2 both read the task while it is IN_PROGRESS
        s1 = SessionMaker()
        s2 = SessionMaker()

        t1 = s1.query(HumanTask).filter_by(id=task_id).first()
        t2 = s2.query(HumanTask).filter_by(id=task_id).first()
        assert t1.status == "IN_PROGRESS"
        assert t2.status == "IN_PROGRESS"

        # S1 completes the task
        res1 = complete_task(
            s1,
            task_id,
            actor_id="HR-102",
            resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
            resolution_evidence_ref="REF-001",
        )
        assert res1.status == "COMPLETED"

        # S2 tries to complete the task concurrently with the stale object/version
        s2_stale_caught = False
        try:
            res2 = complete_task(
                s2,
                task_id,
                actor_id="HR-102",
                resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
                resolution_evidence_ref="REF-001",
            )
            # If complete_task handled StaleDataError idempotently, it must return the completed task
            assert res2.status == "COMPLETED"
        except StaleDataError:
            s2_stale_caught = True
            s2.rollback()

        s1.close()
        s2.close()

        # In either case, exactly ONE COMPLETED audit event must be in the DB
        s_verify = SessionMaker()
        audits = (
            s_verify.query(TaskAuditEvent)
            .filter_by(task_id=task_id, event_type="COMPLETED")
            .all()
        )
        assert len(audits) == 1, f"Expected exactly 1 COMPLETED audit event, found {len(audits)}"
        s_verify.close()

    def test_multi_tenant_isolation_same_po_number(self, fresh_client: TestClient, fresh_db_engine):
        """
        Two tenants with identical PO number do not collide in DB constraints
        and do not leak into each other's reconciliation runs.
        """
        from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine
        from app.db.models.exception import OperationalException
        from sqlalchemy.orm import sessionmaker

        # Pre-seed aliases
        fresh_client.post("/api/v1/imports/product_aliases", json=[
            {"source_system": "ERP", "external_item_id": "SKU-MT", "canonical_product_id": "SKU-MT", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
            {"source_system": "WMS", "external_item_id": "SKU-MT", "canonical_product_id": "SKU-MT", "source_uom": "EA", "base_uom": "EA", "to_base_factor": 1.0},
        ])
        fresh_client.post("/api/v1/imports/location_aliases", json=[
            {"source_system": "ERP", "external_location_id": "LOC-MT", "canonical_location_id": "STORE-MT", "region_id": "R1"},
            {"source_system": "WMS", "external_location_id": "LOC-MT", "canonical_location_id": "STORE-MT", "region_id": "R1"},
            {"source_system": "HRMS", "external_location_id": "LOC-MT", "canonical_location_id": "STORE-MT", "region_id": "R1"},
        ])

        # Tenant A: PO-999 ordered 100, received 60 (Shortfall = 40)
        po_a = [
            {"source_system": "ERP", "source_record_id": "ERP-TENANT-A-PO999", "po_number": "PO-999", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "SKU-MT", "location_code": "LOC-MT", "ordered_qty": 100.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"}
        ]
        rcpt_a = [
            {"source_system": "WMS", "source_record_id": "WMS-TENANT-A-GRN999", "receipt_number": "GRN-999", "receipt_line": 1, "po_number": "PO-999", "po_line_number": 1, "product_ref": "SKU-MT", "location_code": "LOC-MT", "accepted_qty": 60.0, "uom": "EA", "received_at": "2026-10-15T10:00:00+05:30", "posting_status": "POSTED"}
        ]

        # Tenant B: PO-999 ordered 50, received 50 (Shortfall = 0, fully satisfied!)
        po_b = [
            {"source_system": "ERP", "source_record_id": "ERP-TENANT-B-PO999", "po_number": "PO-999", "line_number": 1, "supplier_code": "SUPP-01", "item_code": "SKU-MT", "location_code": "LOC-MT", "ordered_qty": 50.0, "ordered_uom": "EA", "status": "OPEN", "expected_receipt_by": "2026-10-15T18:00:00+05:30"}
        ]
        rcpt_b = [
            {"source_system": "WMS", "source_record_id": "WMS-TENANT-B-GRN999", "receipt_number": "GRN-999", "receipt_line": 1, "po_number": "PO-999", "po_line_number": 1, "product_ref": "SKU-MT", "location_code": "LOC-MT", "accepted_qty": 50.0, "uom": "EA", "received_at": "2026-10-15T10:00:00+05:30", "posting_status": "POSTED"}
        ]

        # Ingest Tenant A
        r_po_a = fresh_client.post("/api/v1/imports/purchase_order_lines?tenant_id=TENANT-ALPHA", json=po_a)
        assert r_po_a.status_code == 201
        assert r_po_a.json()["accepted_rows"] == 1
        r_rcpt_a = fresh_client.post("/api/v1/imports/goods_receipt_lines?tenant_id=TENANT-ALPHA", json=rcpt_a)
        assert r_rcpt_a.status_code == 201

        # Ingest Tenant B with identical po_number="PO-999", line_number=1
        r_po_b = fresh_client.post("/api/v1/imports/purchase_order_lines?tenant_id=TENANT-BETA", json=po_b)
        assert r_po_b.status_code == 201, f"Tenant B PO import failed: {r_po_b.text}"
        assert r_po_b.json()["accepted_rows"] == 1
        r_rcpt_b = fresh_client.post("/api/v1/imports/goods_receipt_lines?tenant_id=TENANT-BETA", json=rcpt_b)
        assert r_rcpt_b.status_code == 201, f"Tenant B receipt import failed: {r_rcpt_b.text}"

        # Run reconciliation for Tenant A
        r_rec_a = fresh_client.post("/api/v1/reconciliation/run?tenant_id=TENANT-ALPHA")
        assert r_rec_a.status_code == 200
        data_a = r_rec_a.json()
        assert data_a["shortfalls_detected"] == 1
        assert data_a["exceptions_created"] == 1

        # Run reconciliation for Tenant B
        r_rec_b = fresh_client.post("/api/v1/reconciliation/run?tenant_id=TENANT-BETA")
        assert r_rec_b.status_code == 200
        data_b = r_rec_b.json()
        # Tenant B had 50 ordered and 50 received, so shortfalls should be 0!
        # If tenant isolation failed and Tenant A's 100-qty PO leaked into Tenant B, this would fail!
        assert data_b["shortfalls_detected"] == 0, f"Tenant B detected unexpected shortfall: {data_b}"
        assert data_b["fully_satisfied"] == 1

        # Check DB exceptions per tenant
        SessionMaker = sessionmaker(bind=fresh_db_engine, expire_on_commit=False)
        s = SessionMaker()
        exc_alpha = s.query(OperationalException).filter_by(tenant_id="TENANT-ALPHA").all()
        exc_beta = s.query(OperationalException).filter_by(tenant_id="TENANT-BETA").all()
        assert len(exc_alpha) == 1
        assert len(exc_beta) == 0
        s.close()

    def test_concurrency_race_handling_loop_20x(self, tmp_path):
        """
        Runs the concurrent task creation and optimistic lock completion 20x in a loop
        to verify rock-solid reliability with zero race flakes, zero duplicates, and zero 500s.
        """
        import concurrent.futures
        import threading
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.orm.exc import StaleDataError
        from app.db.base import Base
        from app.workflows.task_service import create_task_for_exception, complete_task
        from app.db.models.exception import OperationalException
        from app.db.models.task import HumanTask
        from app.db.models.audit import TaskAuditEvent

        db_path = tmp_path / "loop_20x.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(bind=engine)
        SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)

        for iteration in range(1, 21):
            s_setup = SessionMaker()
            exc_key = f"TENANT-001:PO-LOOP-{iteration}:1"
            exc = OperationalException(
                tenant_id="TENANT-001",
                exception_key=exc_key,
                exception_type="RECEIPT_SHORTFALL",
                status="DETECTED",
                po_number=f"PO-LOOP-{iteration}",
                po_line_number=1,
                canonical_product_id="SKU-1",
                canonical_location_id="STORE-1",
                expected_qty_base=100.0,
                received_qty_base=50.0,
                variance_qty_base=50.0,
                base_uom="EA",
                revision=1,
                evidence_records=[f"R-{iteration}"],
                neutral_summary="Shortfall",
                missing_evidence={},
            )
            s_setup.add(exc)
            s_setup.commit()
            exc_id = exc.id
            s_setup.close()

            # 1. Concurrent create_task_for_exception
            barrier = threading.Barrier(2)
            created_tasks = []
            errors = []

            def create_worker():
                s = SessionMaker()
                try:
                    local_exc = s.query(OperationalException).filter_by(id=exc_id).first()
                    barrier.wait(timeout=5)
                    t = create_task_for_exception(s, local_exc, tenant_id="TENANT-001")
                    created_tasks.append(t.id)
                except Exception as e:
                    errors.append(e)
                finally:
                    s.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(create_worker)
                f2 = executor.submit(create_worker)
                f1.result()
                f2.result()

            assert len(errors) == 0, f"Iteration {iteration} errors: {errors}"
            assert len(created_tasks) == 2
            assert created_tasks[0] == created_tasks[1]
            task_id = created_tasks[0]

            # Verify in DB: exactly 1 task
            s_chk = SessionMaker()
            tasks_in_db = s_chk.query(HumanTask).filter_by(exception_id=exc_id).all()
            assert len(tasks_in_db) == 1
            s_chk.close()

            # 2. Advance task to IN_PROGRESS
            s_advance = SessionMaker()
            t_adv = s_advance.query(HumanTask).filter_by(id=task_id).first()
            t_adv.status = "IN_PROGRESS"
            s_advance.commit()
            s_advance.close()

            # 3. Concurrent complete_task
            s1 = SessionMaker()
            s2 = SessionMaker()
            t1 = s1.query(HumanTask).filter_by(id=task_id).first()
            t2 = s2.query(HumanTask).filter_by(id=task_id).first()
            assert t1.version == t2.version

            res1 = complete_task(
                s1,
                task_id,
                actor_id="HR-102",
                resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
                resolution_evidence_ref=f"REF-LOOP-{iteration}",
            )
            assert res1.status == "COMPLETED"

            try:
                res2 = complete_task(
                    s2,
                    task_id,
                    actor_id="HR-102",
                    resolution_code="SUPPLIER_SHORT_SHIPMENT_CONFIRMED",
                    resolution_evidence_ref=f"REF-LOOP-{iteration}",
                )
                assert res2.status == "COMPLETED"
            except StaleDataError:
                s2.rollback()

            s1.close()
            s2.close()

            # Verify exactly 1 COMPLETED audit event
            s_aud = SessionMaker()
            audits = (
                s_aud.query(TaskAuditEvent)
                .filter_by(task_id=task_id, event_type="COMPLETED")
                .all()
            )
            assert len(audits) == 1, f"Iteration {iteration}: expected 1 COMPLETED audit event, found {len(audits)}"
            s_aud.close()

        engine.dispose()


