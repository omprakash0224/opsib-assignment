"""
Import API Endpoints.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.raw import ImportBatch
from app.schemas.import_schemas import ImportResult
from app.ingestion.service import IngestionService
from app.core.security import get_tenant_id

router = APIRouter(prefix="/imports", tags=["Imports & Data Quality"])


@router.post(
    "/{dataset}",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest raw dataset (CSV, JSON, or Multipart File)",
    description=(
        "Validates, deduplicates, and normalizes records for the specified dataset into canonical storage. "
        "Supports application/json (list of records or {'records': [...]}), multipart/form-data, and text/csv."
    ),
)
async def import_dataset(
    dataset: str,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Ingests and normalizes data for the specified dataset.
    Supports either JSON payload, raw CSV body, or multipart file upload.
    """
    content_type = request.headers.get("content-type", "")

    # 1. JSON Payload Handling
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload provided")

        if isinstance(payload, dict) and "records" in payload:
            records = payload["records"]
        elif isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = [payload]
        else:
            raise HTTPException(status_code=400, detail="JSON payload must be a list of records or {'records': [...]}")

        if not records:
            raise HTTPException(status_code=400, detail="Empty records list provided")

        try:
            return IngestionService.ingest_records(
                session=db,
                dataset_name=dataset,
                records=records,
                tenant_id=tenant_id,
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        except Exception as ex:
            raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(ex)}")

    # 2. Multipart or Raw CSV Handling
    csv_content = ""
    if "multipart/form-data" in content_type:
        form = await request.form()
        uploaded_file = form.get("file")
        if uploaded_file and hasattr(uploaded_file, "read"):
            content_bytes = await uploaded_file.read()
            csv_content = content_bytes.decode("utf-8")
        else:
            raise HTTPException(status_code=400, detail="Multipart field 'file' missing")
    else:
        body = await request.body()
        csv_content = body.decode("utf-8")

    if not csv_content.strip():
        raise HTTPException(status_code=400, detail="Empty CSV payload provided")

    try:
        result = IngestionService.ingest_csv(
            session=db,
            dataset_name=dataset,
            csv_content=csv_content,
            tenant_id=tenant_id,
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(ex)}")


@router.get("/{import_id}", response_model=ImportResult)
def get_import_status(import_id: str, db: Session = Depends(get_db)):
    """Retrieves processing counts, errors, and status for an import batch."""
    batch = db.get(ImportBatch, import_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Import batch '{import_id}' not found")

    return ImportResult(
        batch_id=batch.id,
        dataset_name=batch.dataset_name,
        status=batch.status,
        total_rows=batch.total_rows,
        accepted_rows=batch.accepted_rows,
        duplicate_rows=batch.duplicate_rows,
        rejected_rows=batch.rejected_rows,
        errors=batch.error_summary or [],
    )
