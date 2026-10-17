"""
CSV and Raw Payload Parser with Typed Validation and Checksum Generation.
"""
import csv
import io
import json
import hashlib
from typing import Dict, Any, List, Tuple, Type
from pydantic import BaseModel, ValidationError
from app.schemas.import_schemas import (
    ProductAliasRawRow,
    LocationAliasRawRow,
    EmployeeRawRow,
    WorkforceStatusRawRow,
    PurchaseOrderLineRawRow,
    GoodsReceiptLineRawRow,
    ImportRowError,
)

SCHEMA_MAPPING: Dict[str, Type[BaseModel]] = {
    "product_aliases": ProductAliasRawRow,
    "location_aliases": LocationAliasRawRow,
    "employees": EmployeeRawRow,
    "workforce_status": WorkforceStatusRawRow,
    "purchase_order_lines": PurchaseOrderLineRawRow,
    "goods_receipt_lines": GoodsReceiptLineRawRow,
}


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 hash of a normalized JSON payload."""
    normalized_json = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()


def parse_csv_content(
    dataset_name: str, csv_content: str
) -> Tuple[List[Tuple[int, Dict[str, Any], BaseModel, str]], List[ImportRowError]]:
    """
    Parses CSV text into typed models and returns:
    - List of tuples: (row_index, raw_dict, validated_model, content_hash)
    - List of ImportRowError for invalid rows.
    """
    if dataset_name not in SCHEMA_MAPPING:
        raise ValueError(f"Unknown dataset name: {dataset_name}. Supported: {list(SCHEMA_MAPPING.keys())}")

    model_cls = SCHEMA_MAPPING[dataset_name]
    valid_rows = []
    errors = []

    # Use io.StringIO to parse CSV
    reader = csv.DictReader(io.StringIO(csv_content.strip()))
    for row_idx, raw_row in enumerate(reader, start=1):
        # Clean leading/trailing whitespaces in keys and values
        cleaned_row = {
            (k.strip() if k else ""): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k is not None
        }

        # Determine source_record_id for error reporting
        source_id = (
            cleaned_row.get("source_record_id")
            or cleaned_row.get("source_employee_id")
            or f"{cleaned_row.get('source_system')}:{cleaned_row.get('external_item_id') or cleaned_row.get('external_location_id')}"
        )

        try:
            validated = model_cls(**cleaned_row)
            content_hash = compute_payload_hash(cleaned_row)
            valid_rows.append((row_idx, cleaned_row, validated, content_hash))
        except ValidationError as ve:
            err_msg = "; ".join([f"{err['loc']}: {err['msg']}" for err in ve.errors()])
            errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=err_msg))
        except Exception as ex:
            errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=str(ex)))

    return valid_rows, errors


def parse_records(
    dataset_name: str, records: List[Dict[str, Any]]
) -> Tuple[List[Tuple[int, Dict[str, Any], BaseModel, str]], List[ImportRowError]]:
    """
    Parses a list of dictionaries into typed models and returns:
    - List of tuples: (row_index, raw_dict, validated_model, content_hash)
    - List of ImportRowError for invalid rows.
    """
    if dataset_name not in SCHEMA_MAPPING:
        raise ValueError(f"Unknown dataset name: {dataset_name}. Supported: {list(SCHEMA_MAPPING.keys())}")

    model_cls = SCHEMA_MAPPING[dataset_name]
    valid_rows = []
    errors = []

    for row_idx, raw_row in enumerate(records, start=1):
        cleaned_row = {
            (k.strip() if isinstance(k, str) else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k is not None
        }

        source_id = (
            cleaned_row.get("source_record_id")
            or cleaned_row.get("source_employee_id")
            or f"{cleaned_row.get('source_system')}:{cleaned_row.get('external_item_id') or cleaned_row.get('external_location_id')}"
        )

        try:
            validated = model_cls(**cleaned_row)
            content_hash = compute_payload_hash(cleaned_row)
            valid_rows.append((row_idx, cleaned_row, validated, content_hash))
        except ValidationError as ve:
            err_msg = "; ".join([f"{err['loc']}: {err['msg']}" for err in ve.errors()])
            errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=err_msg))
        except Exception as ex:
            errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=str(ex)))

    return valid_rows, errors
