import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import DEFAULT_TENANT_ID, RULE_VERSION, get_current_time
from app.core.logging import logger
from app.db.models.canonical import ProductAlias, LocationAlias
from app.db.models.exception import OperationalException, ExceptionEvent, DataQualityIssue
from app.detection.detector import ReconciliationDetector, ShortfallFinding
from app.detection.rules import MISSING_EVIDENCE_NOTE


def compute_mapping_version(session: Session) -> str:
    """
    Derive a deterministic mapping version string from canonical alias crosswalk tables.
    """
    product_aliases = (
        session.query(ProductAlias)
        .order_by(ProductAlias.source_system, ProductAlias.external_item_id)
        .all()
    )
    location_aliases = (
        session.query(LocationAlias)
        .order_by(LocationAlias.source_system, LocationAlias.external_location_id)
        .all()
    )

    if not product_aliases and not location_aliases:
        return "map-default"

    hasher = hashlib.sha256()
    for pa in product_aliases:
        hasher.update(
            f"{pa.source_system}:{pa.external_item_id}:{pa.canonical_product_id}:{pa.to_base_factor};".encode("utf-8")
        )
    for la in location_aliases:
        hasher.update(
            f"{la.source_system}:{la.external_location_id}:{la.canonical_location_id}:{la.region_id};".encode("utf-8")
        )
    return f"map-{hasher.hexdigest()[:12]}"


class ReconciliationRunResult:
    """
    Value object summarising the outcome of one reconciliation run.
    """
    __slots__ = (
        "run_at",
        "evaluation_time",
        "total_po_lines_evaluated",
        "skipped_cancelled",
        "skipped_future_deadline",
        "fully_satisfied",
        "shortfalls_detected",
        "exceptions_created",
        "exceptions_already_existed",
        "findings",
    )

    def __init__(
        self,
        run_at: datetime,
        evaluation_time: datetime,
        total_po_lines_evaluated: int,
        skipped_cancelled: int,
        skipped_future_deadline: int,
        fully_satisfied: int,
        shortfalls_detected: int,
        exceptions_created: int,
        exceptions_already_existed: int,
        findings: List[ShortfallFinding],
    ):
        self.run_at = run_at
        self.evaluation_time = evaluation_time
        self.total_po_lines_evaluated = total_po_lines_evaluated
        self.skipped_cancelled = skipped_cancelled
        self.skipped_future_deadline = skipped_future_deadline
        self.fully_satisfied = fully_satisfied
        self.shortfalls_detected = shortfalls_detected
        self.exceptions_created = exceptions_created
        self.exceptions_already_existed = exceptions_already_existed
        self.findings = findings


class DetectionService:
    """
    Orchestrates a complete reconciliation run.

    Lifecycle:
      1. Derive mapping_version and evaluation_time (frozen clock).
      2. Instantiate ReconciliationDetector and call run().
      3. For each ShortfallFinding:
         - If shortfall: insert new OperationalException or update quantities on existing one.
         - If fully satisfied (variance <= 0): auto-close existing open exception and cancel linked task.
         - If cancelled: auto-close existing open exception and cancel linked task.
         - If resolved/dismissed and quantities change: record REOPEN_SUGGESTED without silently reopening.
      4. Commit transaction atomically.
      5. Return ReconciliationRunResult for API serialization.
    """

    @classmethod
    def run_reconciliation(
        cls,
        session: Session,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ReconciliationRunResult:
        """
        Execute deterministic reconciliation against the frozen evaluation time.

        Idempotent: running multiple times with unchanged data produces zero duplicate
        exceptions and does not bump exception revisions.
        """
        run_at = datetime.now(timezone.utc)
        evaluation_time = get_current_time()
        mapping_version = compute_mapping_version(session)

        logger.info(
            f"[DETECTION] Starting reconciliation run. "
            f"Evaluation time: {evaluation_time.isoformat()}, "
            f"Mapping version: {mapping_version}, "
            f"Tenant: {tenant_id}"
        )

        detector = ReconciliationDetector(session=session, tenant_id=tenant_id)
        findings: List[ShortfallFinding] = detector.run()

        # Tally counters
        skipped_cancelled = 0
        skipped_future_deadline = 0
        fully_satisfied = 0
        shortfalls_detected = 0
        exceptions_created = 0
        exceptions_already_existed = 0

        for finding in findings:
            if finding.skip_reason:
                if "CANCELLED" in finding.skip_reason or "status is" in finding.skip_reason:
                    skipped_cancelled += 1
                    cls._handle_cancelled_po_line(session, finding, evaluation_time)
                else:
                    skipped_future_deadline += 1
                continue

            if not finding.is_shortfall:
                fully_satisfied += 1
                cls._handle_satisfied_line(session, finding, mapping_version, evaluation_time)
                continue

            shortfalls_detected += 1

            # Persist new shortfall exception or update existing one
            created, existed = cls._upsert_shortfall_exception(
                session, finding, tenant_id, mapping_version, evaluation_time
            )
            exceptions_created += 1 if created else 0
            exceptions_already_existed += 1 if existed else 0

        try:
            session.commit()
        except Exception:
            session.rollback()
            raise

        logger.info(
            f"[DETECTION] Run complete. "
            f"shortfalls={shortfalls_detected}, "
            f"created={exceptions_created}, "
            f"already_existed={exceptions_already_existed}"
        )

        return ReconciliationRunResult(
            run_at=run_at,
            evaluation_time=evaluation_time,
            total_po_lines_evaluated=len(findings),
            skipped_cancelled=skipped_cancelled,
            skipped_future_deadline=skipped_future_deadline,
            fully_satisfied=fully_satisfied,
            shortfalls_detected=shortfalls_detected,
            exceptions_created=exceptions_created,
            exceptions_already_existed=exceptions_already_existed,
            findings=findings,
        )

    @classmethod
    def _upsert_shortfall_exception(
        cls,
        session: Session,
        finding: ShortfallFinding,
        tenant_id: str,
        mapping_version: str,
        evaluation_time: datetime,
    ) -> Tuple[bool, bool]:
        """
        Insert a new shortfall exception or update an existing one when quantities change.
        Returns (created: bool, existed: bool).
        """
        existing: Optional[OperationalException] = (
            session.query(OperationalException)
            .filter_by(exception_key=finding.exception_key)
            .first()
        )

        if not existing:
            try:
                with session.begin_nested():
                    exc = OperationalException(
                        tenant_id=tenant_id,
                        exception_key=finding.exception_key,
                        exception_type="RECEIPT_SHORTFALL",
                        status="DETECTED",
                        po_number=finding.po_number,
                        po_line_number=finding.po_line_number,
                        canonical_product_id=finding.canonical_product_id,
                        canonical_location_id=finding.canonical_location_id,
                        expected_qty_base=finding.expected_qty_base,
                        received_qty_base=finding.received_qty_base,
                        variance_qty_base=finding.variance_qty_base,
                        base_uom=finding.base_uom,
                        revision=1,
                        mapping_version=mapping_version,
                        rule_version=RULE_VERSION,
                        evidence_records=finding.evidence_records,
                        neutral_summary=finding.neutral_summary,
                        missing_evidence=MISSING_EVIDENCE_NOTE,
                        detected_at=evaluation_time,
                    )
                    session.add(exc)
                    session.flush()

                    evt = ExceptionEvent(
                        exception_id=exc.id,
                        event_type="DETECTED",
                        previous_state=None,
                        new_state={
                            "status": "DETECTED",
                            "received_qty_base": exc.received_qty_base,
                            "variance_qty_base": exc.variance_qty_base,
                            "revision": 1,
                        },
                        actor_id="SYSTEM",
                        notes="Initial shortfall detected",
                        timestamp=datetime.now(timezone.utc),
                    )
                    session.add(evt)
                    session.flush()
                    return True, False
            except IntegrityError:
                # Savepoint rolled back automatically on error; reload existing
                existing = (
                    session.query(OperationalException)
                    .filter_by(exception_key=finding.exception_key)
                    .first()
                )


        # Existing exception
        logger.info(
            f"[DETECTION] Exception already exists: key={finding.exception_key}, "
            f"status={existing.status}"
        )

        quantities_changed = (
            abs(existing.received_qty_base - finding.received_qty_base) > 1e-6
            or abs(existing.variance_qty_base - finding.variance_qty_base) > 1e-6
        )

        if existing.status in ("RESOLVED", "DISMISSED"):
            if quantities_changed:
                cls._record_reopen_suggested(session, existing, finding)
            return False, True

        if quantities_changed:
            prev_state = {
                "status": existing.status,
                "received_qty_base": existing.received_qty_base,
                "variance_qty_base": existing.variance_qty_base,
                "revision": existing.revision,
            }

            existing.received_qty_base = finding.received_qty_base
            existing.variance_qty_base = finding.variance_qty_base
            existing.evidence_records = finding.evidence_records
            existing.neutral_summary = finding.neutral_summary
            existing.mapping_version = mapping_version
            existing.revision += 1

            new_state = {
                "status": existing.status,
                "received_qty_base": existing.received_qty_base,
                "variance_qty_base": existing.variance_qty_base,
                "revision": existing.revision,
            }

            evt = ExceptionEvent(
                exception_id=existing.id,
                event_type="QUANTITY_UPDATED",
                previous_state=prev_state,
                new_state=new_state,
                actor_id="SYSTEM",
                notes=(
                    f"Quantities updated by late receipt: received {prev_state['received_qty_base']} -> "
                    f"{existing.received_qty_base}, variance {prev_state['variance_qty_base']} -> "
                    f"{existing.variance_qty_base}"
                ),
                timestamp=datetime.now(timezone.utc),
            )
            session.add(evt)
            session.flush()

        return False, True

    @classmethod
    def _handle_satisfied_line(
        cls,
        session: Session,
        finding: ShortfallFinding,
        mapping_version: str,
        evaluation_time: datetime,
    ) -> None:
        """
        Evaluate a satisfied line (variance <= 0) against existing exceptions.
        If an open exception existed, auto-close it and cancel the linked task.
        If a resolved exception existed and numbers changed, record REOPEN_SUGGESTED.
        """
        existing: Optional[OperationalException] = (
            session.query(OperationalException)
            .filter_by(exception_key=finding.exception_key)
            .first()
        )
        if not existing:
            return

        quantities_changed = (
            abs(existing.received_qty_base - finding.received_qty_base) > 1e-6
            or abs(existing.variance_qty_base - finding.variance_qty_base) > 1e-6
        )

        if existing.status in ("RESOLVED", "DISMISSED"):
            if quantities_changed:
                cls._record_reopen_suggested(session, existing, finding)
            return

        if existing.status == "AUTO_CLOSED":
            # Idempotent: already closed
            return

        # Open exception (DETECTED, INVESTIGATING) now fully covered!
        prev_state = {
            "status": existing.status,
            "received_qty_base": existing.received_qty_base,
            "variance_qty_base": existing.variance_qty_base,
            "revision": existing.revision,
        }

        existing.status = "AUTO_CLOSED"
        existing.resolution_notes = "Shortfall cleared by late receipt"
        existing.resolved_at = evaluation_time
        existing.received_qty_base = finding.received_qty_base
        existing.variance_qty_base = finding.variance_qty_base
        existing.evidence_records = finding.evidence_records
        existing.neutral_summary = finding.neutral_summary
        existing.mapping_version = mapping_version
        existing.revision += 1

        new_state = {
            "status": existing.status,
            "received_qty_base": existing.received_qty_base,
            "variance_qty_base": existing.variance_qty_base,
            "revision": existing.revision,
        }

        evt = ExceptionEvent(
            exception_id=existing.id,
            event_type="AUTO_CLOSED",
            previous_state=prev_state,
            new_state=new_state,
            actor_id="SYSTEM",
            notes="Shortfall cleared by late receipt; exception auto-closed",
            timestamp=datetime.now(timezone.utc),
        )
        session.add(evt)
        session.flush()

        from app.workflows.task_service import cancel_task_for_exception
        cancel_task_for_exception(
            session=session,
            exception_id=existing.id,
            reason="Shortfall cleared by late receipt",
            actor_id="SYSTEM",
        )

    @classmethod
    def _handle_cancelled_po_line(
        cls,
        session: Session,
        finding: ShortfallFinding,
        evaluation_time: datetime,
    ) -> None:
        """
        If a PO line with an open exception is cancelled, auto-close the exception
        and cancel any linked task.
        """
        existing: Optional[OperationalException] = (
            session.query(OperationalException)
            .filter_by(exception_key=finding.exception_key)
            .first()
        )
        if not existing or existing.status in ("AUTO_CLOSED", "RESOLVED", "DISMISSED"):
            return

        prev_state = {
            "status": existing.status,
            "revision": existing.revision,
        }
        existing.status = "AUTO_CLOSED"
        existing.resolution_notes = "Purchase order line was cancelled"
        existing.resolved_at = evaluation_time
        existing.revision += 1

        new_state = {
            "status": existing.status,
            "revision": existing.revision,
        }

        evt = ExceptionEvent(
            exception_id=existing.id,
            event_type="AUTO_CLOSED",
            previous_state=prev_state,
            new_state=new_state,
            actor_id="SYSTEM",
            notes="Purchase order line was cancelled; exception auto-closed",
            timestamp=datetime.now(timezone.utc),
        )
        session.add(evt)
        session.flush()

        from app.workflows.task_service import cancel_task_for_exception
        cancel_task_for_exception(
            session=session,
            exception_id=existing.id,
            reason="Purchase order line was cancelled",
            actor_id="SYSTEM",
        )

    @classmethod
    def _record_reopen_suggested(
        cls,
        session: Session,
        existing: OperationalException,
        finding: ShortfallFinding,
    ) -> None:
        """
        Record a REOPEN_SUGGESTED event and DataQualityIssue when numbers change
        on an already RESOLVED or DISMISSED exception without silently reopening.
        """
        last_event = (
            session.query(ExceptionEvent)
            .filter_by(exception_id=existing.id, event_type="REOPEN_SUGGESTED")
            .order_by(ExceptionEvent.timestamp.desc())
            .first()
        )
        if last_event and last_event.new_state:
            last_received = last_event.new_state.get("received_qty_base")
            if last_received is not None and abs(last_received - finding.received_qty_base) < 1e-6:
                return

        prev_state = {
            "status": existing.status,
            "received_qty_base": existing.received_qty_base,
            "variance_qty_base": existing.variance_qty_base,
            "revision": existing.revision,
        }
        new_state = {
            "status": existing.status,
            "received_qty_base": finding.received_qty_base,
            "variance_qty_base": finding.variance_qty_base,
            "revision": existing.revision,
        }

        evt = ExceptionEvent(
            exception_id=existing.id,
            event_type="REOPEN_SUGGESTED",
            previous_state=prev_state,
            new_state=new_state,
            actor_id="SYSTEM",
            notes=(
                f"Numbers changed post-{existing.status.lower()}: received {existing.received_qty_base} -> "
                f"{finding.received_qty_base}, variance {existing.variance_qty_base} -> "
                f"{finding.variance_qty_base}. Human review required."
            ),
            timestamp=datetime.now(timezone.utc),
        )
        session.add(evt)

        existing_dq = (
            session.query(DataQualityIssue)
            .filter_by(
                source_record_id=existing.exception_key,
                issue_type="REOPEN_SUGGESTED",
            )
            .first()
        )
        if not existing_dq:
            dq_issue = DataQualityIssue(
                tenant_id=existing.tenant_id,
                source_system="RECONCILIATION",
                dataset_name="operational_exceptions",
                source_record_id=existing.exception_key,
                issue_type="REOPEN_SUGGESTED",
                details={
                    "exception_id": existing.id,
                    "po_number": existing.po_number,
                    "line_number": existing.po_line_number,
                    "previous_status": existing.status,
                    "old_received_qty": existing.received_qty_base,
                    "new_received_qty": finding.received_qty_base,
                    "old_variance_qty": existing.variance_qty_base,
                    "new_variance_qty": finding.variance_qty_base,
                },
                created_at=datetime.now(timezone.utc),
            )
            session.add(dq_issue)

        session.flush()

    @classmethod
    def _upsert_exception(
        cls,
        session: Session,
        finding: ShortfallFinding,
        tenant_id: str,
    ) -> Tuple[bool, bool]:
        """Backward-compatible wrapper for _upsert_shortfall_exception."""
        mapping_version = compute_mapping_version(session)
        evaluation_time = get_current_time()
        return cls._upsert_shortfall_exception(
            session=session,
            finding=finding,
            tenant_id=tenant_id,
            mapping_version=mapping_version,
            evaluation_time=evaluation_time,
        )
