"""
Comprehensive Test Suite for Phase 3: Ingestion, Normalization, Deduplication, and Data Quality.
"""
import os
import pytest
from app.db.session import SessionLocal
from app.ingestion.service import IngestionService
from app.db.models.canonical import (
    ProductAlias,
    LocationAlias,
    PurchaseOrderLine,
    GoodsReceiptLine,
    Employee,
    WorkforceShift,
)
from app.db.models.exception import DataQualityIssue

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(filename: str) -> str:
    """Reads fixture file content from workspace."""
    path = os.path.join(WORKSPACE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_ingest_aliases_and_uom_factors(db_session):
    """Verify loading product and location aliases with UOM conversion factors."""
    prod_csv = read_fixture("product_aliases.csv")
    loc_csv = read_fixture("location_aliases.csv")

    res_prod = IngestionService.ingest_csv(db_session, "product_aliases", prod_csv)
    res_loc = IngestionService.ingest_csv(db_session, "location_aliases", loc_csv)

    assert res_prod.status == "COMPLETED"
    assert res_prod.accepted_rows == 6
    assert res_prod.rejected_rows == 0

    assert res_loc.status == "COMPLETED"
    assert res_loc.accepted_rows == 6
    assert res_loc.rejected_rows == 0

    # Assert BOX -> EA conversion factor is stored
    box_alias = db_session.get(ProductAlias, ("ERP", "ERP-BOX-12"))
    assert box_alias is not None
    assert box_alias.canonical_product_id == "SKU-002"
    assert box_alias.to_base_factor == 12.0
    assert box_alias.base_uom == "EA"


def test_ingest_employees_and_regional_role(db_session):
    """Verify employee ingestion, location mapping, and null location for regional roles."""
    # First ingest location aliases required for employee location resolution
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    emp_csv = read_fixture("employees.csv")
    res_emp = IngestionService.ingest_csv(db_session, "employees", emp_csv)

    assert res_emp.status == "COMPLETED"
    assert res_emp.accepted_rows == 6

    # Verify regional role HR-501 has null canonical_location_id but preserves region_id
    hr_501 = db_session.get(Employee, "HR-501")
    assert hr_501 is not None
    assert hr_501.canonical_location_id is None
    assert hr_501.region_id == "REGION-MH"
    assert hr_501.role == "regional_operations"

    # Verify store employees mapped to STORE-014 and STORE-015
    hr_102 = db_session.get(Employee, "HR-102")
    assert hr_102.canonical_location_id == "STORE-014"

    hr_401 = db_session.get(Employee, "HR-401")
    assert hr_401.canonical_location_id == "STORE-015"


def test_ingest_purchase_orders_with_duplicate_and_box_conversion(db_session):
    """
    Verify purchase order lines ingestion:
    - Row 7 & 8 are exact duplicates (ERP-PO506-L1): exactly 1 duplicate skipped.
    - PO-505 (10 BOX) is converted to 120 EA base quantity.
    """
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    po_csv = read_fixture("purchase_order_lines.csv")
    res_po = IngestionService.ingest_csv(db_session, "purchase_order_lines", po_csv)

    assert res_po.status == "COMPLETED"
    assert res_po.total_rows == 7
    assert res_po.accepted_rows == 6
    assert res_po.duplicate_rows == 1
    assert res_po.rejected_rows == 0

    # Check PO-505 UOM conversion (10 BOX * 12 -> 120 EA)
    po_505 = db_session.query(PurchaseOrderLine).filter_by(po_number="PO-505").first()
    assert po_505 is not None
    assert po_505.ordered_qty_source == 10.0
    assert po_505.ordered_uom_source == "BOX"
    assert po_505.ordered_qty_base == 120.0
    assert po_505.base_uom == "EA"
    assert po_505.canonical_location_id == "STORE-015"


def test_ingest_goods_receipts_with_duplicate_deduplication(db_session):
    """
    Verify goods receipt lines ingestion:
    - Row 7 & 8 are exact duplicates (WMS-GRN506-A-L1): exactly 1 duplicate skipped.
    - Counted once to avoid stock double-counting.
    """
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    grn_csv = read_fixture("goods_receipt_lines.csv")
    res_grn = IngestionService.ingest_csv(db_session, "goods_receipt_lines", grn_csv)

    assert res_grn.status == "COMPLETED"
    assert res_grn.total_rows == 7
    assert res_grn.accepted_rows == 6
    assert res_grn.duplicate_rows == 1
    assert res_grn.rejected_rows == 0

    # Ensure only ONE record exists in canonical receipts for PO-506
    po_506_receipts = db_session.query(GoodsReceiptLine).filter_by(po_number="PO-506").all()
    assert len(po_506_receipts) == 1
    assert po_506_receipts[0].accepted_qty_base == 10.0


def test_double_import_idempotency(db_session):
    """Importing the exact same CSV twice produces duplicate_rows and zero extra canonical lines."""
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    po_csv = read_fixture("purchase_order_lines.csv")
    res1 = IngestionService.ingest_csv(db_session, "purchase_order_lines", po_csv)
    assert res1.accepted_rows == 6

    # Re-run same import
    res2 = IngestionService.ingest_csv(db_session, "purchase_order_lines", po_csv)
    assert res2.accepted_rows == 0
    assert res2.duplicate_rows == 7  # All 7 incoming rows skipped as duplicates
    assert res2.rejected_rows == 0

    # Assert total canonical PO lines remains exactly 6
    total_po_lines = db_session.query(PurchaseOrderLine).count()
    assert total_po_lines == 6


def test_conflicting_duplicate_detection(db_session):
    """If an existing source_record_id arrives with conflicting values, record a DataQualityIssue."""
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    initial_csv = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-CONFLICT-01,PO-999,1,SUP-01,MUM-014,ERP-SKU-001,50,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    IngestionService.ingest_csv(db_session, "purchase_order_lines", initial_csv)

    # Conflicting update with quantity changed to 80
    conflicting_csv = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-CONFLICT-01,PO-999,1,SUP-01,MUM-014,ERP-SKU-001,80,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    res = IngestionService.ingest_csv(db_session, "purchase_order_lines", conflicting_csv)

    assert res.accepted_rows == 0
    assert res.rejected_rows == 1
    assert len(res.errors) == 1
    assert "Conflicting payload" in res.errors[0].error

    # Assert a DataQualityIssue was logged
    dq = db_session.query(DataQualityIssue).filter_by(source_record_id="ERP-CONFLICT-01").first()
    assert dq is not None
    assert dq.issue_type == "CONFLICTING_DUPLICATE"

    # Assert canonical quantity was NOT silently modified
    po = db_session.query(PurchaseOrderLine).filter_by(source_record_id="ERP-CONFLICT-01").first()
    assert po.ordered_qty_base == 50.0


def test_unmapped_sku_recorded_as_data_quality(db_session):
    """An unmapped SKU writes a DataQualityIssue and is not loaded into canonical state."""
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))

    unmapped_csv = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-BAD-SKU,PO-888,1,SUP-01,MUM-014,NON_EXISTENT_SKU,50,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    res = IngestionService.ingest_csv(db_session, "purchase_order_lines", unmapped_csv)

    assert res.accepted_rows == 0
    assert res.rejected_rows == 1

    dq = db_session.query(DataQualityIssue).filter_by(source_record_id="ERP-BAD-SKU").first()
    assert dq is not None
    assert dq.issue_type == "UNMAPPED_CROSSWALK"
    assert "Unmapped product" in dq.details["error"]


def test_api_import_endpoint(client):
    """Test importing a dataset via the FastAPI REST endpoint."""
    loc_csv = read_fixture("location_aliases.csv")
    response = client.post(
        "/api/v1/imports/location_aliases",
        content=loc_csv,
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["dataset_name"] == "location_aliases"
    assert data["accepted_rows"] == 6

    # Test GET /api/v1/imports/{id}
    batch_id = data["batch_id"]
    get_res = client.get(f"/api/v1/imports/{batch_id}")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Bug 2 Tests: Recover Rejected Rows on Re-import
# ---------------------------------------------------------------------------

class TestBug2RecoverRejectedRowsOnReimport:
    """
    Bug 2 Verification:
    - Import POs first (all rejected due to missing aliases).
    - Import aliases.
    - Re-import POs: assert 6 accepted, 1 duplicate, 6 canonical rows stored.
    - Re-import POs again: assert 0 accepted, 7 duplicates, still 6 canonical rows.
    - Assert UNMAPPED_CROSSWALK DataQualityIssues are marked resolved after recovery.
    - Conflicting duplicate import repeated: assert exactly one DQ issue created (idempotent).
    """

    def test_import_pos_before_aliases_then_recover_on_reimport(self, fresh_client, fresh_db_engine):
        po_csv = read_fixture("purchase_order_lines.csv")
        prod_csv = read_fixture("product_aliases.csv")
        loc_csv = read_fixture("location_aliases.csv")

        # 1. Import POs first without aliases -> all should fail canonical normalization
        r1 = fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=po_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r1.status_code == 201
        data1 = r1.json()
        assert data1["accepted_rows"] == 0
        assert data1["rejected_rows"] >= 6

        # Check that DQ issues were created
        dq_resp = fresh_client.get("/api/v1/data-quality?dataset=purchase_order_lines&issue_type=UNMAPPED_CROSSWALK").json()
        assert len(dq_resp) >= 6
        for issue in dq_resp:
            assert issue.get("resolved_at") is None

        # 2. Now import aliases
        r_prod = fresh_client.post(
            "/api/v1/imports/product_aliases",
            content=prod_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r_prod.status_code == 201
        assert r_prod.json()["accepted_rows"] == 6

        r_loc = fresh_client.post(
            "/api/v1/imports/location_aliases",
            content=loc_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r_loc.status_code == 201
        assert r_loc.json()["accepted_rows"] == 6

        # 3. Re-import POs: previously rejected rows MUST now be accepted!
        r2 = fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=po_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r2.status_code == 201
        data2 = r2.json()
        assert data2["accepted_rows"] == 6
        assert data2["duplicate_rows"] == 1  # 7th row in fixture is duplicate ERP-PO506-L1
        assert data2["rejected_rows"] == 0

        # Verify canonical rows in DB
        from sqlalchemy.orm import sessionmaker
        SessionLocal = sessionmaker(bind=fresh_db_engine)
        with SessionLocal() as s:
            canonical_count = s.query(PurchaseOrderLine).count()
            assert canonical_count == 6

        # 4. Re-import again: now all 7 rows should be counted as duplicates
        r3 = fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=po_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r3.status_code == 201
        data3 = r3.json()
        assert data3["accepted_rows"] == 0
        assert data3["duplicate_rows"] == 7
        assert data3["rejected_rows"] == 0

        with SessionLocal() as s:
            canonical_count = s.query(PurchaseOrderLine).count()
            assert canonical_count == 6

        # 5. Assert the UNMAPPED_CROSSWALK DQ issues are marked resolved after recovery
        dq_after = fresh_client.get("/api/v1/data-quality?dataset=purchase_order_lines&issue_type=UNMAPPED_CROSSWALK").json()
        assert len(dq_after) >= 6
        for issue in dq_after:
            assert issue.get("resolved_at") is not None

    def test_conflicting_duplicate_dq_issue_idempotency(self, fresh_client):
        # Setup aliases first
        fresh_client.post(
            "/api/v1/imports/product_aliases",
            content=read_fixture("product_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )
        fresh_client.post(
            "/api/v1/imports/location_aliases",
            content=read_fixture("location_aliases.csv"),
            headers={"Content-Type": "text/csv"},
        )

        initial_csv = (
            "source_record_id,po_number,line_number,supplier_code,location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP-CONFLICT-IDEMP,PO-777,1,SUP-01,MUM-014,ERP-SKU-001,50,EA,OPEN,2026-10-15T12:00:00+05:30\n"
        )
        fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=initial_csv,
            headers={"Content-Type": "text/csv"},
        )

        conflicting_csv = (
            "source_record_id,po_number,line_number,supplier_code,location_code,item_code,ordered_qty,ordered_uom,status,expected_receipt_by\n"
            "ERP-CONFLICT-IDEMP,PO-777,1,SUP-01,MUM-014,ERP-SKU-001,99,EA,OPEN,2026-10-15T12:00:00+05:30\n"
        )
        # Import conflicting payload 1st time
        r1 = fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=conflicting_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r1.status_code == 201

        # Import conflicting payload 2nd time
        r2 = fresh_client.post(
            "/api/v1/imports/purchase_order_lines",
            content=conflicting_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert r2.status_code == 201

        # Assert exactly ONE DQ issue exists for ERP-CONFLICT-IDEMP
        dq_issues = fresh_client.get(
            "/api/v1/data-quality?dataset=purchase_order_lines&issue_type=CONFLICTING_DUPLICATE"
        ).json()
        matching = [i for i in dq_issues if i["source_record_id"] == "ERP-CONFLICT-IDEMP"]
        assert len(matching) == 1, f"Expected exactly 1 conflicting duplicate DQ issue, but found {len(matching)}"

