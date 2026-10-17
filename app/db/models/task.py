"""
Human Task and Workflow Models.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, UniqueConstraint
from app.db.base import Base


class HumanTask(Base):
    """
    Actionable human task decoupled from underlying operational exceptions.
    Maintains independent assignment, lifecycle, and resolution records.
    """
    __tablename__ = "human_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, default="TENANT-001", index=True)
    exception_id = Column(String(36), ForeignKey("operational_exceptions.id", ondelete="CASCADE"), nullable=False, index=True)
    task_key = Column(String(128), unique=True, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="CREATED")  # CREATED, ASSIGNED, ACKNOWLEDGED, IN_PROGRESS, BLOCKED, COMPLETED, PENDING_VERIFICATION, VERIFIED, REOPENED

    assignee_id = Column(String(64), ForeignKey("employees.employee_id", ondelete="SET NULL"), nullable=True, index=True)
    assignment_reason = Column(String(128), nullable=True)
    canonical_location_id = Column(String(64), nullable=True, index=True)
    due_at = Column(DateTime(timezone=True), nullable=True)

    resolution_code = Column(String(64), nullable=True)
    resolution_evidence_ref = Column(String(128), nullable=True)
    resolution_notes = Column(Text, nullable=True)
    completed_by = Column(String(64), nullable=True, index=True)

    idempotency_key = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
