"""
Ingestion Service: Orchestrates dataset ingestion, deduplication, mapping, and persistence.
"""
from typing import Optional
from sqlalchemy.orm import Session
from app.config import DEFAULT_TENANT_ID
from app.core.logging import logger
from app.core.exceptions import UnmappedEntityError
from app.db.models.raw import ImportBatch, RawRecord
from app.db.models.canonical import (
    ProductAlias,
    LocationAlias,
    PurchaseOrderLine,
    GoodsReceiptLine,
    Employee,
    WorkforceShift,
)
from app.db.models.exception import DataQualityIssue
from app.schemas.import_schemas import (
    ImportResult,
    ImportRowError,
    ProductAliasRawRow,
    LocationAliasRawRow,
    EmployeeRawRow,
    WorkforceStatusRawRow,
    PurchaseOrderLineRawRow,
    GoodsReceiptLineRawRow,
)
from app.ingestion.parser import parse_csv_content, parse_records
from app.ingestion.normalizer import Normalizer


class IngestionService:
    """Service handling multi-source dataset imports with idempotency and provenance."""

    @classmethod
    def ingest_csv(
        cls,
        session: Session,
        dataset_name: str,
        csv_content: str,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ImportResult:
        """
        Validates, deduplicates, and ingests CSV content for the specified dataset.
        Guarantees idempotency and isolates data quality issues.
        """
        batch = ImportBatch(dataset_name=dataset_name, status="PENDING")
        session.add(batch)
        session.flush()

        valid_rows, errors = parse_csv_content(dataset_name, csv_content)
        return cls._process_rows(session, batch, dataset_name, valid_rows, errors, tenant_id)

    @classmethod
    def ingest_records(
        cls,
        session: Session,
        dataset_name: str,
        records: list,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ImportResult:
        """
        Validates, deduplicates, and ingests a list of dictionary records.
        Guarantees idempotency and isolates data quality issues.
        """
        batch = ImportBatch(dataset_name=dataset_name, status="PENDING")
        session.add(batch)
        session.flush()

        valid_rows, errors = parse_records(dataset_name, records)
        return cls._process_rows(session, batch, dataset_name, valid_rows, errors, tenant_id)

    @classmethod
    def _process_rows(
        cls,
        session: Session,
        batch: ImportBatch,
        dataset_name: str,
        valid_rows: list,
        errors: list,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ImportResult:
        total_rows = len(valid_rows) + len(errors)
        accepted_count = 0
        duplicate_count = 0
        rejected_count = len(errors)
        seen_in_batch: dict[str, str] = {}

        for row_idx, raw_dict, model, content_hash in valid_rows:
            source_id = cls._extract_source_id(dataset_name, model, raw_dict)

            # 1. Intra-batch duplicate & conflict check
            if source_id in seen_in_batch:
                if seen_in_batch[source_id] == content_hash:
                    duplicate_count += 1
                    logger.info(
                        f"Skipping intra-batch duplicate row: dataset={dataset_name}, source_id={source_id}"
                    )
                    continue
                else:
                    rejected_count += 1
                    dq_issue = DataQualityIssue(
                        tenant_id=tenant_id,
                        source_system=raw_dict.get("source_system", "UNKNOWN"),
                        dataset_name=dataset_name,
                        source_record_id=source_id,
                        issue_type="CONFLICTING_DUPLICATE",
                        details={
                            "existing_hash": seen_in_batch[source_id],
                            "incoming_hash": content_hash,
                            "incoming_payload": raw_dict,
                            "row_index": row_idx,
                            "intra_batch": True,
                        },
                    )
                    session.add(dq_issue)
                    errors.append(
                        ImportRowError(
                            row_index=row_idx,
                            source_record_id=source_id,
                            error=f"Conflicting payload for existing source_record_id '{source_id}' within same batch",
                        )
                    )
                    continue

            # 2. Cross-batch Deduplication & Conflict Check against RawRecord
            existing_raw = (
                session.query(RawRecord)
                .filter_by(dataset_name=dataset_name, source_record_id=source_id)
                .first()
            )

            if existing_raw:
                if existing_raw.content_hash == content_hash:
                    # Idempotent Repeat: skip processing, do not double count
                    duplicate_count += 1
                    seen_in_batch[source_id] = content_hash
                    logger.info(
                        f"Skipping identical duplicate row: dataset={dataset_name}, source_id={source_id}"
                    )
                    continue
                else:
                    # Conflicting payload for the same source ID: record DataQualityIssue
                    rejected_count += 1
                    seen_in_batch[source_id] = content_hash
                    dq_issue = DataQualityIssue(
                        tenant_id=tenant_id,
                        source_system=raw_dict.get("source_system", "UNKNOWN"),
                        dataset_name=dataset_name,
                        source_record_id=source_id,
                        issue_type="CONFLICTING_DUPLICATE",
                        details={
                            "existing_hash": existing_raw.content_hash,
                            "incoming_hash": content_hash,
                            "incoming_payload": raw_dict,
                            "row_index": row_idx,
                        },
                    )
                    session.add(dq_issue)
                    errors.append(
                        ImportRowError(
                            row_index=row_idx,
                            source_record_id=source_id,
                            error=f"Conflicting payload for existing source_record_id '{source_id}'",
                        )
                    )
                    continue

            # 3. Record new RawRecord and mark seen
            seen_in_batch[source_id] = content_hash
            raw_record = RawRecord(
                batch_id=batch.id,
                dataset_name=dataset_name,
                source_system=raw_dict.get("source_system", cls._infer_source_system(dataset_name)),
                source_record_id=source_id,
                content_hash=content_hash,
                raw_payload=raw_dict,
                status="ACCEPTED",
            )
            session.add(raw_record)

            # 4. Canonical Normalization and Insertion
            try:
                cls._persist_canonical_entity(session, tenant_id, batch.id, dataset_name, model)
                accepted_count += 1
            except UnmappedEntityError as ue:
                rejected_count += 1
                dq_issue = DataQualityIssue(
                    tenant_id=tenant_id,
                    source_system=raw_dict.get("source_system", cls._infer_source_system(dataset_name)),
                    dataset_name=dataset_name,
                    source_record_id=source_id,
                    issue_type="UNMAPPED_CROSSWALK",
                    details={"error": str(ue), "row_index": row_idx, "payload": raw_dict},
                )
                session.add(dq_issue)
                errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=str(ue)))
            except Exception as ex:
                rejected_count += 1
                errors.append(ImportRowError(row_index=row_idx, source_record_id=source_id, error=str(ex)))

        # Update batch summary
        batch.total_rows = total_rows
        batch.accepted_rows = accepted_count
        batch.duplicate_rows = duplicate_count
        batch.rejected_rows = rejected_count
        batch.error_summary = [err.model_dump() for err in errors]

        if rejected_count == 0:
            batch.status = "COMPLETED"
        elif accepted_count > 0:
            batch.status = "PARTIAL"
        else:
            batch.status = "FAILED"

        session.commit()

        return ImportResult(
            batch_id=batch.id,
            dataset_name=dataset_name,
            status=batch.status,
            total_rows=total_rows,
            accepted_rows=accepted_count,
            duplicate_rows=duplicate_count,
            rejected_rows=rejected_count,
            errors=errors,
        )

    @staticmethod
    def _extract_source_id(dataset_name: str, model: any, raw_dict: dict) -> str:
        """Determines the unique source record ID for the row."""
        if hasattr(model, "source_record_id"):
            return model.source_record_id
        if hasattr(model, "source_employee_id"):
            if dataset_name == "workforce_status":
                # Shift status sampled by employee and observed timestamp
                return f"{model.source_employee_id}:{model.observed_at.isoformat()}"
            return model.source_employee_id
        if dataset_name == "product_aliases":
            return f"{model.source_system}:{model.external_item_id}"
        if dataset_name == "location_aliases":
            return f"{model.source_system}:{model.external_location_id}"
        return f"{dataset_name}:{id(model)}"

    @staticmethod
    def _infer_source_system(dataset_name: str) -> str:
        """Infers the source system when not explicitly present in row fields."""
        if dataset_name == "purchase_order_lines":
            return "ERP"
        if dataset_name == "goods_receipt_lines":
            return "WMS"
        if dataset_name in ("employees", "workforce_status"):
            return "HRMS"
        return "UNKNOWN"

    @classmethod
    def _persist_canonical_entity(
        cls, session: Session, tenant_id: str, batch_id: str, dataset_name: str, model: any
    ):
        """Maps and persists the canonical representation for a validated row."""
        if dataset_name == "product_aliases":
            alias = ProductAlias(
                source_system=model.source_system,
                external_item_id=model.external_item_id,
                canonical_product_id=model.canonical_product_id,
                source_uom=model.source_uom,
                base_uom=model.base_uom,
                to_base_factor=model.to_base_factor,
            )
            session.merge(alias)

        elif dataset_name == "location_aliases":
            loc = LocationAlias(
                source_system=model.source_system,
                external_location_id=model.external_location_id,
                canonical_location_id=model.canonical_location_id,
                region_id=model.region_id,
            )
            session.merge(loc)

        elif dataset_name == "employees":
            canonical_loc, reg_id = Normalizer.resolve_employee_location(session, model.location_code)
            emp = Employee(
                employee_id=model.source_employee_id,
                tenant_id=tenant_id,
                display_name=model.display_name,
                employment_status=model.employment_status,
                role=model.role,
                canonical_location_id=canonical_loc,
                raw_location_code=model.location_code,
                manager_employee_id=model.manager_employee_id,
                region_id=reg_id or model.region_id,
            )
            session.merge(emp)

        elif dataset_name == "workforce_status":
            shift = WorkforceShift(
                employee_id=model.source_employee_id,
                availability_status=model.availability_status,
                shift_start=model.shift_start,
                shift_end=model.shift_end,
                observed_at=model.observed_at,
            )
            session.add(shift)

        elif dataset_name == "purchase_order_lines":
            canonical_prod, qty_base, base_uom = Normalizer.resolve_product(
                session, "ERP", model.item_code, model.ordered_qty
            )
            canonical_loc, _ = Normalizer.resolve_location(session, "ERP", model.location_code)

            po_line = PurchaseOrderLine(
                tenant_id=tenant_id,
                source_system="ERP",
                source_record_id=model.source_record_id,
                import_batch_id=batch_id,
                po_number=model.po_number,
                line_number=model.line_number,
                supplier_code=model.supplier_code,
                canonical_location_id=canonical_loc,
                raw_location_code=model.location_code,
                canonical_product_id=canonical_prod,
                raw_item_code=model.item_code,
                ordered_qty_source=model.ordered_qty,
                ordered_uom_source=model.ordered_uom,
                ordered_qty_base=qty_base,
                base_uom=base_uom,
                status=model.status,
                expected_receipt_by=model.expected_receipt_by,
            )
            session.add(po_line)

        elif dataset_name == "goods_receipt_lines":
            canonical_prod, qty_base, base_uom = Normalizer.resolve_product(
                session, "WMS", model.product_ref, model.accepted_qty
            )
            canonical_loc, _ = Normalizer.resolve_location(session, "WMS", model.location_code)

            receipt_line = GoodsReceiptLine(
                tenant_id=tenant_id,
                source_system="WMS",
                source_record_id=model.source_record_id,
                import_batch_id=batch_id,
                receipt_number=model.receipt_number,
                receipt_line=model.receipt_line,
                po_number=model.po_number,
                po_line_number=model.po_line_number,
                canonical_location_id=canonical_loc,
                raw_location_code=model.location_code,
                canonical_product_id=canonical_prod,
                raw_product_ref=model.product_ref,
                accepted_qty_source=model.accepted_qty,
                uom_source=model.uom,
                accepted_qty_base=qty_base,
                base_uom=base_uom,
                received_at=model.received_at,
                posting_status=model.posting_status,
            )
            session.add(receipt_line)
