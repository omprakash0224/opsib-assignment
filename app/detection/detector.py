"""
Shortfall Detector: Core deterministic reconciliation engine.

Implements the exact business logic described in Phase 4 of PLAN.md and the
Deterministic Reconciliation & Shortfall Detector flowchart in ARCHITECTURE.md.

Invariants enforced here:
  1. Only OPEN PO lines are evaluated (CANCELLED => skipped).
  2. Only lines whose expected_receipt_by <= FROZEN_TIME are actionable.
  3. Only POSTED GoodsReceiptLines count toward fulfillment; draft and reversed
     receipts are excluded.
  4. Duplicate receipts are already deduplicated during ingestion (Phase 3), so
     summing accepted_qty_base is safe and accurate.
  5. Exceptions are keyed deterministically; conflicts are handled by the
     caller (DetectionService) using upsert / ON CONFLICT logic.
  6. Findings are epistemically neutral: no supplier blame is asserted.
"""
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy.orm import Session

from app.config import DEFAULT_TENANT_ID, RULE_VERSION, KOLKATA_TZ, get_current_time
from app.core.logging import logger
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine
from app.db.models.exception import OperationalException
from app.detection.rules import (
    ELIGIBLE_PO_STATUSES,
    ELIGIBLE_RECEIPT_POSTING_STATUSES,
    VARIANCE_TOLERANCE,
    MISSING_EVIDENCE_NOTE,
    compute_variance,
    build_neutral_summary,
    build_exception_key,
)


def _ensure_tz_aware(dt: datetime) -> datetime:
    """
    Ensure a datetime is timezone-aware.

    SQLite does not natively preserve timezone offsets in DateTime columns.
    Values stored as timezone-aware datetimes (e.g. +05:30) may be returned
    as naive UTC or naive local time depending on the SQLAlchemy dialect.

    Strategy: if the value is already tz-aware, return as-is.
    If naive, assume it was stored in the Kolkata timezone (the canonical
    timezone for all fixture timestamps in this assessment).
    """
    if dt.tzinfo is not None:
        return dt
    # Attach Kolkata TZ to the naive datetime
    return dt.replace(tzinfo=KOLKATA_TZ)



class ShortfallFinding:
    """
    Transient value object representing a detected shortfall before DB persistence.
    """
    __slots__ = (
        "po_number",
        "po_line_number",
        "canonical_product_id",
        "canonical_location_id",
        "expected_qty_base",
        "received_qty_base",
        "variance_qty_base",
        "base_uom",
        "evidence_records",
        "neutral_summary",
        "exception_key",
        "skip_reason",
    )

    def __init__(
        self,
        po_number: str,
        po_line_number: int,
        canonical_product_id: str,
        canonical_location_id: str,
        expected_qty_base: float,
        received_qty_base: float,
        variance_qty_base: float,
        base_uom: str,
        evidence_records: List[str],
        neutral_summary: str,
        exception_key: str,
        skip_reason: Optional[str] = None,
    ):
        self.po_number = po_number
        self.po_line_number = po_line_number
        self.canonical_product_id = canonical_product_id
        self.canonical_location_id = canonical_location_id
        self.expected_qty_base = expected_qty_base
        self.received_qty_base = received_qty_base
        self.variance_qty_base = variance_qty_base
        self.base_uom = base_uom
        self.evidence_records = evidence_records
        self.neutral_summary = neutral_summary
        self.exception_key = exception_key
        self.skip_reason = skip_reason

    @property
    def is_shortfall(self) -> bool:
        return self.skip_reason is None and self.variance_qty_base > VARIANCE_TOLERANCE


class ReconciliationDetector:
    """
    Deterministic reconciliation engine against a frozen evaluation clock.

    Iterates over all canonical PurchaseOrderLine records and computes
    the receipt shortfall for each eligible line against all POSTED
    GoodsReceiptLine records.

    The evaluation clock is injected via `get_current_time()` (FROZEN_TIME
    by default), ensuring deterministic, reproducible results regardless of
    server execution time (ADR-3).
    """

    def __init__(self, session: Session, tenant_id: str = DEFAULT_TENANT_ID):
        self._session = session
        self._tenant_id = tenant_id
        self._evaluation_time: datetime = get_current_time()

    def run(self) -> List[ShortfallFinding]:
        """
        Execute reconciliation for all PO lines and return findings.

        Returns a list of ShortfallFinding objects. Findings with
        `skip_reason` set were skipped (cancelled / future deadline / no
        shortfall). Only findings where `is_shortfall` is True should be
        persisted as OperationalExceptions.
        """
        po_lines = self._session.query(PurchaseOrderLine).all()
        findings: List[ShortfallFinding] = []

        for po_line in po_lines:
            finding = self._evaluate_po_line(po_line)
            findings.append(finding)
            if finding.skip_reason:
                logger.info(
                    f"[RECON] Skipping {po_line.po_number} L{po_line.line_number}: {finding.skip_reason}"
                )
            elif finding.is_shortfall:
                logger.info(
                    f"[RECON] SHORTFALL detected: {po_line.po_number} L{po_line.line_number} "
                    f"| variance={finding.variance_qty_base:.2f} {finding.base_uom}"
                )
            else:
                logger.info(
                    f"[RECON] No shortfall: {po_line.po_number} L{po_line.line_number} fully satisfied."
                )

        return findings

    def _evaluate_po_line(self, po_line: PurchaseOrderLine) -> ShortfallFinding:
        """
        Evaluate a single PO line against its POSTED goods receipts.

        Step 1: Cancellation filter – CANCELLED POs are excluded.
        Step 2: Deadline filter – Future expected_receipt_by => not yet actionable.
        Step 3: Aggregate POSTED receipt quantities.
        Step 4: Compute variance; generate ShortfallFinding.
        """
        # --- Step 1: Cancellation Filter ---
        if po_line.status not in ELIGIBLE_PO_STATUSES:
            return self._skip(
                po_line,
                skip_reason=f"PO status is '{po_line.status}'; only OPEN lines are evaluated.",
                received_qty_base=0.0,
                evidence_records=[po_line.source_record_id],
            )

        # --- Step 2: Deadline Filter ---
        # expected_receipt_by is timezone-aware; compare directly with evaluation_time.
        # NOTE: SQLite may return naive datetimes even for tz-aware columns; normalize.
        receipt_deadline = _ensure_tz_aware(po_line.expected_receipt_by)
        if receipt_deadline > self._evaluation_time:
            return self._skip(
                po_line,
                skip_reason=(
                    f"Receipt deadline {po_line.expected_receipt_by.isoformat()} "
                    f"is in the future relative to frozen evaluation time "
                    f"{self._evaluation_time.isoformat()}; line is not yet actionable."
                ),
                received_qty_base=0.0,
                evidence_records=[po_line.source_record_id],
            )

        # --- Step 3: Aggregate POSTED receipts ---
        receipt_lines: List[GoodsReceiptLine] = (
            self._session.query(GoodsReceiptLine)
            .filter(
                GoodsReceiptLine.po_number == po_line.po_number,
                GoodsReceiptLine.po_line_number == po_line.line_number,
                GoodsReceiptLine.posting_status.in_(ELIGIBLE_RECEIPT_POSTING_STATUSES),
                # Canonical product and location must match for valid aggregation.
                GoodsReceiptLine.canonical_product_id == po_line.canonical_product_id,
                GoodsReceiptLine.canonical_location_id == po_line.canonical_location_id,
            )
            .all()
        )

        total_received: float = sum(r.accepted_qty_base for r in receipt_lines)

        # Collect all source record IDs cited as evidence (PO line + all receipt lines)
        evidence_ids: List[str] = [po_line.source_record_id] + [r.source_record_id for r in receipt_lines]

        # --- Step 4: Variance Calculation ---
        variance = compute_variance(po_line.ordered_qty_base, total_received)

        neutral_summary = build_neutral_summary(
            po_number=po_line.po_number,
            po_line_number=po_line.line_number,
            variance=variance,
            base_uom=po_line.base_uom,
            expected_qty=po_line.ordered_qty_base,
            received_qty=total_received,
        )

        exception_key = build_exception_key(
            tenant_id=self._tenant_id,
            po_number=po_line.po_number,
            po_line_number=po_line.line_number,
        )

        return ShortfallFinding(
            po_number=po_line.po_number,
            po_line_number=po_line.line_number,
            canonical_product_id=po_line.canonical_product_id,
            canonical_location_id=po_line.canonical_location_id,
            expected_qty_base=po_line.ordered_qty_base,
            received_qty_base=total_received,
            variance_qty_base=variance,
            base_uom=po_line.base_uom,
            evidence_records=evidence_ids,
            neutral_summary=neutral_summary,
            exception_key=exception_key,
            skip_reason=None,
        )

    def _skip(
        self,
        po_line: PurchaseOrderLine,
        skip_reason: str,
        received_qty_base: float,
        evidence_records: List[str],
    ) -> ShortfallFinding:
        """Returns a ShortfallFinding marked as skipped (not a shortfall)."""
        return ShortfallFinding(
            po_number=po_line.po_number,
            po_line_number=po_line.line_number,
            canonical_product_id=po_line.canonical_product_id,
            canonical_location_id=po_line.canonical_location_id,
            expected_qty_base=po_line.ordered_qty_base,
            received_qty_base=received_qty_base,
            variance_qty_base=0.0,
            base_uom=po_line.base_uom,
            evidence_records=evidence_records,
            neutral_summary="",
            exception_key=build_exception_key(
                tenant_id=self._tenant_id,
                po_number=po_line.po_number,
                po_line_number=po_line.line_number,
            ),
            skip_reason=skip_reason,
        )
