"""
Canonical Domain Models for Products, Locations, POs, Receipts, and Workforce.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, ForeignKey, UniqueConstraint
)
from app.db.base import Base


class ProductAlias(Base):
    """Product alias crosswalk for mapping external SKU IDs and UOM conversions."""
    __tablename__ = "product_aliases"

    source_system = Column(String(32), primary_key=True)
    external_item_id = Column(String(128), primary_key=True)
    canonical_product_id = Column(String(64), nullable=False, index=True)
    source_uom = Column(String(16), nullable=False)
    base_uom = Column(String(16), nullable=False)
    to_base_factor = Column(Float, nullable=False, default=1.0)


class LocationAlias(Base):
    """Location crosswalk for resolving ERP, WMS, and HRMS codes to canonical store/region."""
    __tablename__ = "location_aliases"

    source_system = Column(String(32), primary_key=True)
    external_location_id = Column(String(128), primary_key=True)
    canonical_location_id = Column(String(64), nullable=False, index=True)
    region_id = Column(String(64), nullable=False)


class PurchaseOrderLine(Base):
    """Canonical Purchase Order line item with normalized base UOM."""
    __tablename__ = "canonical_po_lines"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    source_system = Column(String(32), nullable=False, default="ERP")
    source_record_id = Column(String(128), nullable=False, index=True)
    import_batch_id = Column(String(36), nullable=True)

    po_number = Column(String(64), nullable=False, index=True)
    line_number = Column(Integer, nullable=False)
    supplier_code = Column(String(64), nullable=False)

    canonical_location_id = Column(String(64), nullable=False, index=True)
    raw_location_code = Column(String(64), nullable=False)

    canonical_product_id = Column(String(64), nullable=False, index=True)
    raw_item_code = Column(String(64), nullable=False)

    ordered_qty_source = Column(Float, nullable=False)
    ordered_uom_source = Column(String(16), nullable=False)
    ordered_qty_base = Column(Float, nullable=False)
    base_uom = Column(String(16), nullable=False)

    status = Column(String(32), nullable=False, default="OPEN")  # OPEN, CANCELLED, CLOSED
    expected_receipt_by = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_system", "source_record_id", name="uq_po_source_record"),
        UniqueConstraint("po_number", "line_number", name="uq_po_number_line"),
    )


class GoodsReceiptLine(Base):
    """Canonical Goods Receipt line item with normalized base UOM."""
    __tablename__ = "canonical_receipt_lines"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    source_system = Column(String(32), nullable=False, default="WMS")
    source_record_id = Column(String(128), nullable=False, index=True)
    import_batch_id = Column(String(36), nullable=True)

    receipt_number = Column(String(64), nullable=False, index=True)
    receipt_line = Column(Integer, nullable=False)
    po_number = Column(String(64), nullable=False, index=True)
    po_line_number = Column(Integer, nullable=False)

    canonical_location_id = Column(String(64), nullable=False, index=True)
    raw_location_code = Column(String(64), nullable=False)

    canonical_product_id = Column(String(64), nullable=False, index=True)
    raw_product_ref = Column(String(64), nullable=False)

    accepted_qty_source = Column(Float, nullable=False)
    uom_source = Column(String(16), nullable=False)
    accepted_qty_base = Column(Float, nullable=False)
    base_uom = Column(String(16), nullable=False)

    received_at = Column(DateTime(timezone=True), nullable=False)
    posting_status = Column(String(32), nullable=False, default="POSTED")  # POSTED, DRAFT, REVERSED
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_system", "source_record_id", name="uq_receipt_source_record"),
    )


class Employee(Base):
    """Canonical HRMS Employee record."""
    __tablename__ = "employees"

    employee_id = Column(String(64), primary_key=True)  # HR-101, etc.
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    display_name = Column(String(128), nullable=False)
    employment_status = Column(String(32), nullable=False)  # ACTIVE, INACTIVE
    role = Column(String(64), nullable=False)  # receiving_associate, store_manager, regional_operations
    canonical_location_id = Column(String(64), nullable=True, index=True)  # Nullable for regional roles
    raw_location_code = Column(String(64), nullable=True)
    manager_employee_id = Column(String(64), nullable=True)
    region_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class WorkforceShift(Base):
    """Sampled workforce availability and shift status."""
    __tablename__ = "workforce_shifts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    employee_id = Column(String(64), ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False, index=True)
    availability_status = Column(String(32), nullable=False)  # ON_SHIFT, OFF_SHIFT
    shift_start = Column(DateTime(timezone=True), nullable=False)
    shift_end = Column(DateTime(timezone=True), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
