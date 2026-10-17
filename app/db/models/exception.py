"""
Operational Exception and Data Quality Models.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, JSON, UniqueConstraint
from app.db.base import Base


class OperationalException(Base):
    """
    Operational business exception (e.g. Receipt Shortfall).
    Represents an objective anomaly, preserving neutral provenance without asserting supplier fault.
    """
    __tablename__ = "operational_exceptions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    exception_key = Column(String(128), unique=True, nullable=False, index=True)
    exception_type = Column(String(64), nullable=False, default="RECEIPT_SHORTFALL", index=True)
    status = Column(String(32), nullable=False, default="DETECTED")  # DETECTED, INVESTIGATING, RESOLVED, DISMISSED

    po_number = Column(String(64), nullable=False, index=True)
    po_line_number = Column(Integer, nullable=False)
    canonical_product_id = Column(String(64), nullable=False, index=True)
    canonical_location_id = Column(String(64), nullable=False, index=True)

    expected_qty_base = Column(Float, nullable=False)
    received_qty_base = Column(Float, nullable=False)
    variance_qty_base = Column(Float, nullable=False)
    base_uom = Column(String(16), nullable=False)

    rule_version = Column(String(32), nullable=False, default="v1.0.0")
    evidence_records = Column(JSON, nullable=False)  # List of raw/canonical source record IDs
    neutral_summary = Column(Text, nullable=False)
    missing_evidence = Column(JSON, nullable=False)  # Explicitly documents absence of ASN/dispatch data
    detected_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_notes = Column(Text, nullable=True)


class DataQualityIssue(Base):
    """
    Data quality anomalies (unmapped SKUs, unmapped locations, conflicting duplicate records).
    Kept strictly separate from operational exceptions to avoid false business alarms.
    """
    __tablename__ = "data_quality_issues"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    source_system = Column(String(32), nullable=False, index=True)
    dataset_name = Column(String(64), nullable=False, index=True)
    source_record_id = Column(String(128), nullable=False, index=True)
    issue_type = Column(String(64), nullable=False, index=True)  # UNMAPPED_PRODUCT, UNMAPPED_LOCATION, CONFLICTING_DUPLICATE
    details = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
