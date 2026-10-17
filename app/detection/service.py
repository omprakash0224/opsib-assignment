"""
Detection Service: Coordinates reconciliation runs and persists findings.

Wraps the ReconciliationDetector in a transaction-safe service that:
  - Runs the detector against canonical DB state.
  - Upserts OperationalException records idempotently (exception_key UNIQUE constraint).
  - Returns a structured ReconciliationRunResult for the REST API and tests.
"""
from datetime import datetime, timezone
from typing import List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import DEFAULT_TENANT_ID, RULE_VERSION, get_current_time
from app.core.logging import logger
from app.db.models.exception import OperationalException
from app.detection.detector import ReconciliationDetector, ShortfallFinding
from app.detection.rules import MISSING_EVIDENCE_NOTE


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
      1. Instantiate ReconciliationDetector and call run().
      2. For each ShortfallFinding where is_shortfall is True:
         - Attempt to INSERT a new OperationalException.
         - On duplicate exception_key (UNIQUE constraint), skip (idempotent).
      3. Return ReconciliationRunResult for API serialization.
    """

    @classmethod
    def run_reconciliation(
        cls,
        session: Session,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ReconciliationRunResult:
        """
        Execute deterministic reconciliation against the frozen evaluation time.

        This method is idempotent: running it multiple times will not create
        duplicate OperationalException records. The UNIQUE constraint on
        `exception_key` acts as the database-enforced idempotency guard.
        """
        run_at = datetime.now(timezone.utc)
        evaluation_time = get_current_time()

        logger.info(
            f"[DETECTION] Starting reconciliation run. "
            f"Evaluation time: {evaluation_time.isoformat()}, "
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
                else:
                    skipped_future_deadline += 1
                continue

            if not finding.is_shortfall:
                fully_satisfied += 1
                continue

            shortfalls_detected += 1

            # Persist or confirm idempotent existing exception
            created, existed = cls._upsert_exception(session, finding, tenant_id)
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

    @staticmethod
    def _upsert_exception(
        session: Session,
        finding: ShortfallFinding,
        tenant_id: str,
    ) -> Tuple[bool, bool]:
        """
        Insert a new OperationalException, or confirm an existing one.

        Returns:
            (created: bool, existed: bool)

        Strategy: query by exception_key first to support SQLite (which lacks
        ON CONFLICT DO UPDATE for non-UPSERT paths easily) while remaining
        PostgreSQL compatible.
        """
        existing: OperationalException = (
            session.query(OperationalException)
            .filter_by(exception_key=finding.exception_key)
            .first()
        )

        if existing:
            logger.info(
                f"[DETECTION] Exception already exists: key={finding.exception_key}, "
                f"status={existing.status}"
            )
            return False, True

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
            rule_version=RULE_VERSION,
            evidence_records=finding.evidence_records,
            neutral_summary=finding.neutral_summary,
            missing_evidence=MISSING_EVIDENCE_NOTE,
            detected_at=datetime.now(timezone.utc),
        )
        session.add(exc)
        # Flush so we get the ID but stay within the outer transaction.
        session.flush()
        return True, False
