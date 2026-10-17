"""
CLI Utility to load all synthetic CSV fixture files in dependency order.
Usage:
    python scripts/load_fixtures.py [--data-dir .]
"""
import os
import sys
import argparse

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal, init_db
from app.ingestion.service import IngestionService
from app.core.logging import logger

DATASET_ORDER = [
    ("product_aliases", "product_aliases.csv"),
    ("location_aliases", "location_aliases.csv"),
    ("employees", "employees.csv"),
    ("workforce_status", "workforce_status.csv"),
    ("purchase_order_lines", "purchase_order_lines.csv"),
    ("goods_receipt_lines", "goods_receipt_lines.csv"),
]


def load_fixtures(data_dir: str = "."):
    """Loads all fixture datasets into the database."""
    init_db()
    session = SessionLocal()

    print("\n" + "=" * 70)
    print("OPSIB FIXTURE LOADER - INGESTION PIPELINE")
    print("=" * 70)

    try:
        for dataset_name, filename in DATASET_ORDER:
            filepath = os.path.join(data_dir, filename)
            if not os.path.exists(filepath):
                print(f"[ERROR] File not found: {filepath}")
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            result = IngestionService.ingest_csv(
                session=session,
                dataset_name=dataset_name,
                csv_content=content,
            )

            status_color = "[OK]" if result.status == "COMPLETED" else f"[{result.status}]"
            print(
                f"{status_color:<10} {dataset_name:<25} "
                f"Total: {result.total_rows:<3} | Accepted: {result.accepted_rows:<3} | "
                f"Duplicate: {result.duplicate_rows:<3} | Rejected: {result.rejected_rows:<3}"
            )

            if result.errors:
                for err in result.errors:
                    print(f"           -> Row {err.row_index} ({err.source_record_id}): {err.error}")

        print("=" * 70)
        print("All fixtures loaded successfully.\n")

    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load OPSIB synthetic CSV fixtures.")
    parser.add_argument("--data-dir", default=".", help="Path to directory containing CSV fixtures")
    args = parser.parse_args()
    load_fixtures(args.data_dir)
