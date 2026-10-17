"""
Deterministic Offline Mock Provider for AI Investigation Advisory (Task D / ADR-7).

Used during automated test suites and offline environments to ensure 100% deterministic,
zero-cost, and zero-network-dependency verification.
"""
from typing import List, Optional
from app.ai.interface import AIInvestigationProvider
from app.schemas.ai_schemas import EvidencePackage, InvestigationBrief


class MockInvestigationProvider(AIInvestigationProvider):
    """
    Deterministic mock provider that simulates an LLM response grounded
    strictly in the supplied EvidencePackage.
    """

    def __init__(
        self,
        model_name: str = "mock-deterministic-v1",
        injected_hallucinations: Optional[List[str]] = None,
    ):
        self._model_name = model_name
        self.injected_hallucinations = injected_hallucinations or []

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_brief(self, evidence: EvidencePackage) -> InvestigationBrief:
        po = evidence.po_line
        receipt_lines = evidence.receipt_lines

        # Base cited IDs from valid evidence
        cited_ids = [po.source_record_id] + [r.source_record_id for r in receipt_lines]

        # Inject hallucinations if test specifically configured it to test guardrails
        if self.injected_hallucinations:
            cited_ids.extend(self.injected_hallucinations)

        receipt_summary_parts = []
        for r in receipt_lines:
            receipt_summary_parts.append(
                f"Receipt {r.receipt_number} Line {r.receipt_line} ({r.source_record_id}) accepted "
                f"{r.accepted_qty_base} {r.base_uom} on {r.received_at.strftime('%Y-%m-%d %H:%M')}"
            )
        receipt_text = "; ".join(receipt_summary_parts) if receipt_summary_parts else "No goods receipt records found"

        executive_summary = (
            f"Purchase Order {po.po_number} Line {po.line_number} at location {po.canonical_location_id} "
            f"shows a shortfall of {evidence.variance_qty_base:.2f} {evidence.base_uom}. "
            f"Ordered: {evidence.expected_qty_base:.2f} {evidence.base_uom}; "
            f"Total Accepted: {evidence.received_qty_base:.2f} {evidence.base_uom}."
        )

        analysis_of_evidence = (
            f"PO line {po.source_record_id} was expected by {po.expected_receipt_by.strftime('%Y-%m-%d')}. "
            f"Associated posted receipts: {receipt_text}. "
            f"The deterministic variance calculation confirms a deficit of {evidence.variance_qty_base:.2f} {evidence.base_uom}."
        )

        missing_context = [
            "Supplier dispatch notice / Advanced Shipping Notice (ASN) is absent from the dataset.",
            "Carrier Proof of Delivery (POD) and Bill of Lading (BOL) signatures are unverified.",
            "Potential in-transit inventory or delayed warehouse staging records are unobserved.",
        ]

        recommended_steps = [
            f"Conduct a physical inventory audit in receiving bay and staging areas at {po.canonical_location_id}.",
            f"Contact supplier {po.supplier_code} to request the dispatch manifest and ASN corresponding to PO {po.po_number}.",
            "Inspect WMS quarantine or damaged-goods intake logs for unlinked receipt records.",
            "Verify whether supplier initiated a back-order or short-shipment credit note.",
        ]

        return InvestigationBrief(
            executive_summary=executive_summary,
            analysis_of_evidence=analysis_of_evidence,
            missing_context=missing_context,
            recommended_investigation_steps=recommended_steps,
            cited_record_ids=cited_ids,
            confidence_score=0.95,
            safety_warnings=[],
            neutrality_disclaimer=(
                "Advisory summary only. Supplier dispatch / ASN data absent; "
                "findings do not establish supplier fault or transit loss."
            ),
        )
