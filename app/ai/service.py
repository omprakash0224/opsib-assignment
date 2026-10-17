"""
AI Investigation Orchestration Service (Task D / ADR-7).

Coordinates:
1. Assembly of strictly factual EvidencePackage from canonical DB state.
2. Provider selection (OpenRouter vs Mock).
3. Citation guardrail verification & sanitization.
4. Response formatting.

CRITICAL INVARIANT: This service is strictly read-only and makes ZERO database mutations.
"""
from typing import List, Optional
from uuid import UUID
from datetime import datetime
from sqlalchemy.orm import Session

from app.config import (
    AI_PROVIDER,
    OPENROUTER_API_KEY,
    get_current_time,
)
from app.core.exceptions import ResourceNotFoundError
from app.db.models.exception import OperationalException
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine
from app.schemas.ai_schemas import (
    EvidencePOLine,
    EvidenceReceiptLine,
    EvidencePackage,
    InvestigationBriefResponse,
)
from app.ai.interface import AIInvestigationProvider
from app.ai.mock_provider import MockInvestigationProvider
from app.ai.openrouter_provider import OpenRouterInvestigationProvider
from app.ai.validator import AIGuardrailValidator


class InvestigationService:
    """
    Coordinates evidence packaging, provider execution, and safety guardrails.
    """

    @classmethod
    def assemble_evidence_package(
        cls,
        session: Session,
        exception_id: str,
    ) -> EvidencePackage:
        """
        Gathers factual canonical records linked to the operational exception.
        Constructs the authoritative whitelist of valid source record IDs.
        """
        exc = session.query(OperationalException).filter_by(id=str(exception_id)).first()
        if not exc:
            raise ResourceNotFoundError(f"OperationalException with ID '{exception_id}' not found")


        # Fetch canonical PO Line
        po_line = (
            session.query(PurchaseOrderLine)
            .filter_by(po_number=exc.po_number, line_number=exc.po_line_number)
            .first()
        )
        if not po_line:
            raise ResourceNotFoundError(
                f"Canonical PurchaseOrderLine for PO '{exc.po_number}' Line '{exc.po_line_number}' not found"
            )

        # Fetch canonical Goods Receipt Lines
        receipt_lines_db = (
            session.query(GoodsReceiptLine)
            .filter_by(po_number=exc.po_number, po_line_number=exc.po_line_number)
            .order_by(GoodsReceiptLine.received_at.asc())
            .all()
        )

        # Build whitelist of valid source record IDs
        valid_ids: List[str] = [po_line.source_record_id]
        evidence_receipt_lines: List[EvidenceReceiptLine] = []

        for r in receipt_lines_db:
            valid_ids.append(r.source_record_id)
            evidence_receipt_lines.append(
                EvidenceReceiptLine(
                    source_record_id=r.source_record_id,
                    receipt_number=r.receipt_number,
                    receipt_line=r.receipt_line,
                    accepted_qty_base=r.accepted_qty_base,
                    base_uom=r.base_uom,
                    received_at=r.received_at,
                    posting_status=r.posting_status,
                )
            )

        # Deduplicate whitelist while preserving order
        unique_valid_ids = list(dict.fromkeys(valid_ids))

        evidence_po = EvidencePOLine(
            source_record_id=po_line.source_record_id,
            po_number=po_line.po_number,
            line_number=po_line.line_number,
            supplier_code=po_line.supplier_code,
            canonical_product_id=po_line.canonical_product_id,
            canonical_location_id=po_line.canonical_location_id,
            ordered_qty_base=po_line.ordered_qty_base,
            base_uom=po_line.base_uom,
            expected_receipt_by=po_line.expected_receipt_by,
        )

        return EvidencePackage(
            exception_id=exc.id,
            exception_key=exc.exception_key,
            po_line=evidence_po,
            receipt_lines=evidence_receipt_lines,
            expected_qty_base=exc.expected_qty_base,
            received_qty_base=exc.received_qty_base,
            variance_qty_base=exc.variance_qty_base,
            base_uom=exc.base_uom,
            neutral_summary=exc.neutral_summary,
            missing_evidence=exc.missing_evidence or [],
            valid_source_record_ids=unique_valid_ids,
        )

    @classmethod
    def get_provider(
        cls,
        use_mock: Optional[bool] = None,
        model_override: Optional[str] = None,
    ) -> AIInvestigationProvider:
        """
        Determines the appropriate provider based on explicit flags and environment settings.
        """
        if use_mock is True:
            return MockInvestigationProvider()
        elif use_mock is False:
            return OpenRouterInvestigationProvider(model=model_override)

        # Fallback to config settings
        if AI_PROVIDER == "openrouter" and OPENROUTER_API_KEY:
            return OpenRouterInvestigationProvider(model=model_override)
        return MockInvestigationProvider()

    @classmethod
    def investigate(
        cls,
        session: Session,
        exception_id: str,
        use_mock: Optional[bool] = None,
        model_override: Optional[str] = None,
        provider_instance: Optional[AIInvestigationProvider] = None,
    ) -> InvestigationBriefResponse:

        """
        Assembles evidence, executes advisory brief generation, applies guardrails,
        and returns the verified brief. Read-only operation.
        """
        evidence = cls.assemble_evidence_package(session=session, exception_id=exception_id)

        provider = provider_instance or cls.get_provider(use_mock=use_mock, model_override=model_override)
        raw_brief = provider.generate_brief(evidence)

        # Apply strict citation guardrails & epistemic neutrality checks
        sanitized_brief = AIGuardrailValidator.validate_and_sanitize(
            brief=raw_brief,
            evidence=evidence,
        )

        return InvestigationBriefResponse(
            exception_id=evidence.exception_id,
            exception_key=evidence.exception_key,
            provider_used=provider.provider_name,
            model_used=provider.model_name,
            generated_at=get_current_time(),
            brief=sanitized_brief,
        )
