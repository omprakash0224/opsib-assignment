"""
CLI Utility to execute deterministic shortfall reconciliation and workforce task routing.

Usage:
    python scripts/run_reconciliation.py [--tenant-id TENANT-001] [--format table|json]
"""
import os
import sys
import argparse
import json

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import FROZEN_TIME, DEFAULT_TENANT_ID, RULE_VERSION
from app.db.session import SessionLocal, init_db
from app.detection.service import DetectionService
from app.workflows import task_service
from app.db.models.exception import OperationalException
from app.db.models.task import HumanTask


def run_reconciliation_cli(tenant_id: str = DEFAULT_TENANT_ID, output_format: str = "table"):
    init_db()
    session = SessionLocal()

    try:
        # 1. Execute deterministic detection
        result = DetectionService.run_reconciliation(session=session, tenant_id=tenant_id)

        # 2. Automatically provision tasks for detected exceptions if not already present
        exceptions = (
            session.query(OperationalException)
            .filter_by(tenant_id=tenant_id)
            .order_by(OperationalException.po_number, OperationalException.po_line_number)
            .all()
        )

        rows = []
        for exc in exceptions:
            task = task_service.create_task_for_exception(session=session, exception=exc)
            rows.append({
                "exception_key": exc.exception_key,
                "po_number": exc.po_number,
                "line_number": exc.po_line_number,
                "product_id": exc.canonical_product_id,
                "location_id": exc.canonical_location_id,
                "expected_qty": exc.expected_qty_base,
                "received_qty": exc.received_qty_base,
                "variance_qty": exc.variance_qty_base,
                "uom": exc.base_uom,
                "status": exc.status,
                "task_status": task.status,
                "assignee_id": task.assignee_id or "UNASSIGNED",
                "assignment_reason": task.assignment_reason,
            })

        if output_format == "json":
            output = {
                "tenant_id": tenant_id,
                "frozen_time": FROZEN_TIME.isoformat(),
                "rule_version": RULE_VERSION,
                "summary": {
                    "po_lines_evaluated": result.total_po_lines_evaluated,
                    "skipped_cancelled": result.skipped_cancelled,
                    "skipped_future_deadline": result.skipped_future_deadline,
                    "fully_satisfied": result.fully_satisfied,
                    "shortfalls_detected": result.shortfalls_detected,
                    "exceptions_created": result.exceptions_created,
                    "exceptions_already_existed": result.exceptions_already_existed,
                },
                "findings": rows,
            }
            print(json.dumps(output, indent=2))
            return

        # Table format output
        print("\n" + "=" * 90)
        print("OPSIB DETERMINISTIC RECONCILIATION & TASK ROUTING ENGINE")
        print("=" * 90)
        print(f"Evaluation Clock Anchor : {FROZEN_TIME.isoformat()}")
        print(f"Rule Engine Version     : {RULE_VERSION}")
        print(f"Tenant Context          : {tenant_id}")
        print("-" * 90)
        print(
            f"Evaluated PO Lines : {result.total_po_lines_evaluated} | "
            f"Cancelled Skipped: {result.skipped_cancelled} | "
            f"Future Skipped   : {result.skipped_future_deadline}"
        )
        print(
            f"Fully Satisfied    : {result.fully_satisfied} | "
            f"Shortfalls Found : {result.shortfalls_detected} | "
            f"New Exceptions   : {result.exceptions_created}"
        )
        print("-" * 90)

        if not rows:
            print("No actionable exceptions found for this evaluation run.")
        else:
            header = f"{'PO Line':<12} {'Product':<10} {'Location':<11} {'Ordered':<8} {'Recvd':<8} {'Variance':<9} {'Assignee':<12} {'Reason'}"
            print(header)
            print("-" * 90)
            for r in rows:
                line_ref = f"{r['po_number']}-L{r['line_number']}"
                variance_str = f"{r['variance_qty']:.0f} {r['uom']}"
                ordered_str = f"{r['expected_qty']:.0f} {r['uom']}"
                recvd_str = f"{r['received_qty']:.0f} {r['uom']}"
                assignee_str = r['assignee_id']
                reason_str = r['assignment_reason']
                print(
                    f"{line_ref:<12} {r['product_id']:<10} {r['location_id']:<11} "
                    f"{ordered_str:<8} {recvd_str:<8} {variance_str:<9} {assignee_str:<12} {reason_str}"
                )

        print("=" * 90 + "\n")

    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run OPSIB shortfall reconciliation engine.")
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID, help="Tenant ID context")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format")
    args = parser.parse_args()

    run_reconciliation_cli(tenant_id=args.tenant_id, output_format=args.format)
