"""
Pydantic Validation Schemas for Ingestion and Import APIs.
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator
from dateutil import parser as date_parser


def parse_iso_datetime(value: Any) -> datetime:
    """Parses ISO-8601 string to timezone-aware datetime."""
    if isinstance(value, datetime):
        return value
    if not value or not str(value).strip():
        raise ValueError("Datetime string cannot be empty")
    dt = date_parser.parse(str(value).strip())
    if dt.tzinfo is None:
        raise ValueError(f"Datetime '{value}' must include explicit timezone offset")
    return dt


class ProductAliasRawRow(BaseModel):
    """Schema for product_aliases.csv."""
    source_system: str = Field(..., min_length=1)
    external_item_id: str = Field(..., min_length=1)
    canonical_product_id: str = Field(..., min_length=1)
    source_uom: str = Field(..., min_length=1)
    base_uom: str = Field(..., min_length=1)
    to_base_factor: float = Field(gt=0.0)

    @field_validator("to_base_factor", mode="before")
    @classmethod
    def cast_float(cls, v: Any) -> float:
        return float(v)


class LocationAliasRawRow(BaseModel):
    """Schema for location_aliases.csv."""
    source_system: str = Field(..., min_length=1)
    external_location_id: str = Field(..., min_length=1)
    canonical_location_id: str = Field(..., min_length=1)
    region_id: str = Field(..., min_length=1)


class EmployeeRawRow(BaseModel):
    """Schema for employees.csv."""
    source_employee_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    employment_status: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    location_code: Optional[str] = None
    manager_employee_id: Optional[str] = None
    region_id: Optional[str] = None

    @field_validator("location_code", "manager_employee_id", "region_id", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Optional[str]:
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip()


class WorkforceStatusRawRow(BaseModel):
    """Schema for workforce_status.csv."""
    source_employee_id: str = Field(..., min_length=1)
    availability_status: str = Field(..., min_length=1)
    shift_start: datetime
    shift_end: datetime
    observed_at: datetime

    @field_validator("shift_start", "shift_end", "observed_at", mode="before")
    @classmethod
    def parse_dt(cls, v: Any) -> datetime:
        return parse_iso_datetime(v)


class PurchaseOrderLineRawRow(BaseModel):
    """Schema for purchase_order_lines.csv."""
    source_record_id: str = Field(..., min_length=1)
    po_number: str = Field(..., min_length=1)
    line_number: int = Field(ge=1)
    supplier_code: str = Field(..., min_length=1)
    location_code: str = Field(..., min_length=1)
    item_code: str = Field(..., min_length=1)
    ordered_qty: float = Field(gt=0.0)
    ordered_uom: str = Field(..., min_length=1)
    status: str = Field(default="OPEN")
    expected_receipt_by: datetime

    @field_validator("line_number", mode="before")
    @classmethod
    def cast_int(cls, v: Any) -> int:
        return int(v)

    @field_validator("ordered_qty", mode="before")
    @classmethod
    def cast_float(cls, v: Any) -> float:
        return float(v)

    @field_validator("expected_receipt_by", mode="before")
    @classmethod
    def parse_dt(cls, v: Any) -> datetime:
        return parse_iso_datetime(v)


class GoodsReceiptLineRawRow(BaseModel):
    """Schema for goods_receipt_lines.csv."""
    source_record_id: str = Field(..., min_length=1)
    receipt_number: str = Field(..., min_length=1)
    receipt_line: int = Field(ge=1)
    po_number: str = Field(..., min_length=1)
    po_line_number: int = Field(ge=1)
    location_code: str = Field(..., min_length=1)
    product_ref: str = Field(..., min_length=1)
    accepted_qty: float = Field(ge=0.0)
    uom: str = Field(..., min_length=1)
    received_at: datetime
    posting_status: str = Field(default="POSTED")

    @field_validator("receipt_line", "po_line_number", mode="before")
    @classmethod
    def cast_int(cls, v: Any) -> int:
        return int(v)

    @field_validator("accepted_qty", mode="before")
    @classmethod
    def cast_float(cls, v: Any) -> float:
        return float(v)

    @field_validator("received_at", mode="before")
    @classmethod
    def parse_dt(cls, v: Any) -> datetime:
        return parse_iso_datetime(v)


class ImportRowError(BaseModel):
    """Details of an invalid row."""
    row_index: int
    source_record_id: Optional[str] = None
    error: str


class ImportResult(BaseModel):
    """Summary result returned after ingesting a dataset batch."""
    batch_id: str
    dataset_name: str
    status: str  # COMPLETED, PARTIAL, FAILED
    total_rows: int
    accepted_rows: int
    duplicate_rows: int
    rejected_rows: int
    errors: List[ImportRowError] = []


class DataQualityIssueResponse(BaseModel):
    """API response model for data quality items."""
    id: str
    tenant_id: str
    source_system: str
    dataset_name: str
    source_record_id: str
    issue_type: str
    details: Dict[str, Any]
    created_at: datetime
    resolved_at: Optional[datetime] = None

