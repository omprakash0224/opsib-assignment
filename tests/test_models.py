"""
Verification tests for Phase 1 & 2: Database schemas, constraints, and models.
"""
from datetime import datetime, timezone
import pytest
from sqlalchemy.exc import IntegrityError
from app.config import FROZEN_TIME, get_current_time
from app.db.models import (
    ImportBatch,
    RawRecord,
    ProductAlias,
    LocationAlias,
    PurchaseOrderLine,
    GoodsReceiptLine,
    Employee,
    WorkforceShift,
    OperationalException,
    DataQualityIssue,
    HumanTask,
    TaskAuditEvent,
)


def test_frozen_time_anchor():
    """Verify that frozen evaluation time is pinned to 2026-10-16T15:00:00+05:30."""
    cur_time = get_current_time()
    assert cur_time == FROZEN_TIME
    assert cur_time.year == 2026
    assert cur_time.month == 10
    assert cur_time.day == 16
    assert cur_time.hour == 15
    assert cur_time.minute == 0
    assert cur_time.tzinfo is not None


def test_product_and_location_aliases(db_session):
    """Verify product and location crosswalk persistence and composite primary keys."""
    prod_alias = ProductAlias(
        source_system="ERP",
        external_item_id="ERP-BOX-12",
        canonical_product_id="SKU-002",
        source_uom="BOX",
        base_uom="EA",
        to_base_factor=12.0,
    )
    db_session.add(prod_alias)

    loc_alias = LocationAlias(
        source_system="ERP",
        external_location_id="MUM-014",
        canonical_location_id="STORE-014",
        region_id="REGION-MH",
    )
    db_session.add(loc_alias)
    db_session.flush()

    retrieved_prod = db_session.get(ProductAlias, ("ERP", "ERP-BOX-12"))
    assert retrieved_prod is not None
    assert retrieved_prod.to_base_factor == 12.0

    retrieved_loc = db_session.get(LocationAlias, ("ERP", "MUM-014"))
    assert retrieved_loc is not None
    assert retrieved_loc.canonical_location_id == "STORE-014"


def test_raw_record_unique_constraint(db_session):
    """Verify raw record deduplication constraint on (dataset_name, source_record_id)."""
    batch = ImportBatch(dataset_name="purchase_order_lines", status="COMPLETED")
    db_session.add(batch)
    db_session.flush()

    rec1 = RawRecord(
        batch_id=batch.id,
        dataset_name="purchase_order_lines",
        source_system="ERP",
        source_record_id="ERP-PO506-L1",
        content_hash="hash123",
        raw_payload={"po": "PO-506", "qty": 20},
        status="ACCEPTED",
    )
    db_session.add(rec1)
    db_session.flush()

    rec2 = RawRecord(
        batch_id=batch.id,
        dataset_name="purchase_order_lines",
        source_system="ERP",
        source_record_id="ERP-PO506-L1",
        content_hash="hash123",
        raw_payload={"po": "PO-506", "qty": 20},
        status="DUPLICATE_SKIPPED",
    )
    db_session.add(rec2)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_exception_and_task_lifecycle_link(db_session):
    """Verify that operational exception and human task can be created and linked."""
    exc = OperationalException(
        exception_key="TENANT-001:RECEIPT_SHORTFALL:PO-501:1",
        exception_type="RECEIPT_SHORTFALL",
        status="DETECTED",
        po_number="PO-501",
        po_line_number=1,
        canonical_product_id="SKU-001",
        canonical_location_id="STORE-014",
        expected_qty_base=100.0,
        received_qty_base=92.0,
        variance_qty_base=8.0,
        base_uom="EA",
        rule_version="v1.0.0",
        evidence_records=["ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"],
        neutral_summary="Receipt shortfall of 8.0 EA detected against PO PO-501 Line 1.",
        missing_evidence=["SUPPLIER_DISPATCH_ASN", "CARRIER_POD"],
    )
    db_session.add(exc)
    db_session.flush()

    emp = Employee(
        employee_id="HR-102",
        display_name="Dev Shah",
        employment_status="ACTIVE",
        role="receiving_associate",
        canonical_location_id="STORE-014",
    )
    db_session.add(emp)
    db_session.flush()

    task = HumanTask(
        exception_id=exc.id,
        task_key="TASK:TENANT-001:RECEIPT_SHORTFALL:PO-501:1",
        status="ASSIGNED",
        assignee_id=emp.employee_id,
        assignment_reason="PRIMARY_ON_SHIFT_RECEIVING_ASSOCIATE",
        canonical_location_id="STORE-014",
    )
    db_session.add(task)
    db_session.flush()

    audit = TaskAuditEvent(
        task_id=task.id,
        actor_id="SYSTEM",
        event_type="ASSIGNED",
        from_status="CREATED",
        to_status="ASSIGNED",
        reason="Assigned to primary eligible receiving associate",
    )
    db_session.add(audit)
    db_session.flush()

    assert task.id is not None
    assert audit.task_id == task.id
    assert exc.variance_qty_base == 8.0
