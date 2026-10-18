"""
Raw Ingestion and Provenance Tracking Models.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, Text, JSON, ForeignKey, UniqueConstraint
from app.db.base import Base


class ImportBatch(Base):
    """Tracks a single dataset ingestion run."""
    __tablename__ = "import_batches"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    dataset_name = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="PENDING")  # PENDING, COMPLETED, PARTIAL, FAILED
    total_rows = Column(Integer, default=0, nullable=False)
    accepted_rows = Column(Integer, default=0, nullable=False)
    duplicate_rows = Column(Integer, default=0, nullable=False)
    rejected_rows = Column(Integer, default=0, nullable=False)
    error_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class RawRecord(Base):
    """
    Immutable raw ingestion record preserving exact payload, checksum, and source provenance.
    Protects against silent mutations or duplicate processing.
    """
    __tablename__ = "raw_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id = Column(String(36), ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True, index=True)
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    dataset_name = Column(String(64), nullable=False, index=True)
    source_system = Column(String(32), nullable=False, index=True)
    source_record_id = Column(String(128), nullable=False, index=True)
    content_hash = Column(String(64), nullable=False)  # SHA-256 hash of payload
    raw_payload = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="ACCEPTED")  # ACCEPTED, DUPLICATE_SKIPPED, CONFLICT, REJECTED
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset_name", "source_record_id", name="uq_raw_dataset_source_id"),
    )

