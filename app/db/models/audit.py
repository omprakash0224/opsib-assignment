"""
Audit Event Logging Models.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, Text, JSON, ForeignKey
from app.db.base import Base


class TaskAuditEvent(Base):
    """
    Immutable audit log entry recording state transitions, assignments, and resolution events.
    """
    __tablename__ = "task_audit_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("human_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(String(64), nullable=False, default="SYSTEM")
    event_type = Column(String(64), nullable=False)  # CREATED, ASSIGNED, ACKNOWLEDGED, STATUS_CHANGE, REASSIGNED, COMPLETED, VERIFIED, REOPENED
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=False)
    reason = Column(Text, nullable=True)
    event_payload = Column(JSON, nullable=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
