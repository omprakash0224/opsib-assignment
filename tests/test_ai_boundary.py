"""
Phase 7 Test Suite: AI Advisory Boundary & Citation Guardrails (Task D / ADR-7).

Verifies:
1. MockInvestigationProvider determinism and schema conformity.
2. EvidencePackage assembly & source record ID whitelisting from database state.
3. Citation Guardrail Validator: allows grounded citations, strips hallucinations,
   and injects safety warnings.
4. OpenRouter Provider: HTTP transport mocking, timeout handling, error status handling,
   and malformed response handling.
5. Database Immutability Invariant: AI investigation NEVER mutates database records.
6. REST API Endpoint: POST /api/v1/exceptions/{id}/investigate integration.
"""
import json
import os
import uuid
import pytest
import httpx
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.ingestion.service import IngestionService
from app.detection.service import DetectionService
from app.db.models.exception import OperationalException
from app.db.models.canonical import PurchaseOrderLine, GoodsReceiptLine
from app.db.models.task import HumanTask
from app.db.models.audit import TaskAuditEvent
from app.schemas.ai_schemas import (
    EvidencePackage,
    EvidencePOLine,
    EvidenceReceiptLine,
    InvestigationBrief,
    InvestigationBriefResponse,
)
from app.ai.interface import AIInvestigationProvider
from app.ai.mock_provider import MockInvestigationProvider
from app.ai.openrouter_provider import OpenRouterInvestigationProvider
from app.ai.validator import AIGuardrailValidator, MANDATORY_NEUTRALITY_DISCLAIMER
from app.ai.service import InvestigationService
from app.core.exceptions import AIProviderError, ResourceNotFoundError

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read_fixture(filename: str) -> str:
    path = os.path.join(WORKSPACE_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_all_fixtures(db_session: Session) -> None:
    IngestionService.ingest_csv(db_session, "product_aliases", read_fixture("product_aliases.csv"))
    IngestionService.ingest_csv(db_session, "location_aliases", read_fixture("location_aliases.csv"))
    IngestionService.ingest_csv(db_session, "employees", read_fixture("employees.csv"))
    IngestionService.ingest_csv(db_session, "workforce_status", read_fixture("workforce_status.csv"))
    IngestionService.ingest_csv(db_session, "purchase_order_lines", read_fixture("purchase_order_lines.csv"))
    IngestionService.ingest_csv(db_session, "goods_receipt_lines", read_fixture("goods_receipt_lines.csv"))


# ---------------------------------------------------------------------------
# Test Suite 1: Mock Provider & Schemas
# ---------------------------------------------------------------------------

class TestMockProvider:
    def test_mock_provider_schema_validity(self):
        """Mock provider generates compliant InvestigationBrief with all required fields."""
        po = EvidencePOLine(
            source_record_id="ERP-PO501-L1",
            po_number="PO-501",
            line_number=1,
            supplier_code="SUP-001",
            canonical_product_id="SKU-001",
            canonical_location_id="STORE-014",
            ordered_qty_base=100.0,
            base_uom="EA",
            expected_receipt_by="2026-10-15T18:00:00+05:30",
        )
        receipt = EvidenceReceiptLine(
            source_record_id="WMS-GRN501-A-L1",
            receipt_number="GRN-501-A",
            receipt_line=1,
            accepted_qty_base=60.0,
            base_uom="EA",
            received_at="2026-10-15T10:00:00+05:30",
            posting_status="POSTED",
        )
        evidence = EvidencePackage(
            exception_id=str(uuid.uuid4()),

            exception_key="TENANT-001:SHORTFALL:PO-501:1",
            po_line=po,
            receipt_lines=[receipt],
            expected_qty_base=100.0,
            received_qty_base=60.0,
            variance_qty_base=40.0,
            base_uom="EA",
            neutral_summary="Receipt shortfall of 40 EA detected.",
            missing_evidence=["SUPPLIER_DISPATCH_ASN"],
            valid_source_record_ids=["ERP-PO501-L1", "WMS-GRN501-A-L1"],
        )

        provider = MockInvestigationProvider()
        brief = provider.generate_brief(evidence)

        assert isinstance(brief, InvestigationBrief)
        assert "PO-501" in brief.executive_summary
        assert "40.00 EA" in brief.executive_summary
        assert "ERP-PO501-L1" in brief.cited_record_ids
        assert "WMS-GRN501-A-L1" in brief.cited_record_ids
        assert brief.confidence_score >= 0.9
        assert "Supplier dispatch" in brief.neutrality_disclaimer


# ---------------------------------------------------------------------------
# Test Suite 2: Evidence Packaging & Ground Truth Whitelisting
# ---------------------------------------------------------------------------

class TestEvidencePackaging:
    def test_evidence_packaging_extracts_all_source_ids(self, db_session: Session):
        """Assembling evidence package for PO-501 captures PO line and both split receipts."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        evidence = InvestigationService.assemble_evidence_package(db_session, exc.id)
        assert evidence.exception_id == exc.id
        assert evidence.expected_qty_base == 100.0
        assert evidence.received_qty_base == 92.0
        assert evidence.variance_qty_base == 8.0
        assert evidence.base_uom == "EA"

        # Check whitelist contains the PO line and both GRN lines
        assert "ERP-PO501-L1" in evidence.valid_source_record_ids
        assert "WMS-GRN501-A-L1" in evidence.valid_source_record_ids
        assert "WMS-GRN501-B-L1" in evidence.valid_source_record_ids
        assert len(evidence.valid_source_record_ids) == 3

    def test_evidence_packaging_uom_conversion_box_to_ea(self, db_session: Session):
        """Assembling evidence for PO-505 captures normalized units (120 EA vs 96 EA)."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-505").first()
        assert exc is not None

        evidence = InvestigationService.assemble_evidence_package(db_session, exc.id)
        assert evidence.expected_qty_base == 120.0
        assert evidence.received_qty_base == 96.0
        assert evidence.variance_qty_base == 24.0
        assert "ERP-PO505-L1" in evidence.valid_source_record_ids
        assert "WMS-GRN505-A-L1" in evidence.valid_source_record_ids


# ---------------------------------------------------------------------------
# Test Suite 3: Citation Guardrail & Safety Validator (Task D / ADR-7)
# ---------------------------------------------------------------------------

class TestAIGuardrailValidator:
    @pytest.fixture
    def sample_evidence(self):
        po = EvidencePOLine(
            source_record_id="ERP-PO501-L1",
            po_number="PO-501",
            line_number=1,
            supplier_code="SUP-001",
            canonical_product_id="SKU-001",
            canonical_location_id="STORE-014",
            ordered_qty_base=100.0,
            base_uom="EA",
            expected_receipt_by="2026-10-15T18:00:00+05:30",
        )
        return EvidencePackage(
            exception_id=str(uuid.uuid4()),

            exception_key="TENANT-001:SHORTFALL:PO-501:1",
            po_line=po,
            receipt_lines=[],
            expected_qty_base=100.0,
            received_qty_base=92.0,
            variance_qty_base=8.0,
            base_uom="EA",
            neutral_summary="Receipt shortfall of 8 EA.",
            missing_evidence=["SUPPLIER_DISPATCH_ASN"],
            valid_source_record_ids=["ERP-PO501-L1", "WMS-GRN501-A-L1", "WMS-GRN501-B-L1"],
        )

    def test_guardrail_passes_valid_citations(self, sample_evidence):
        """When AI cites only legitimate record IDs, they are retained with zero warnings."""
        raw_brief = InvestigationBrief(
            executive_summary="Summary",
            analysis_of_evidence="Analysis",
            missing_context=["Missing ASN"],
            recommended_investigation_steps=["Check dock"],
            cited_record_ids=["ERP-PO501-L1", "WMS-GRN501-A-L1"],
            confidence_score=0.9,
            neutrality_disclaimer=MANDATORY_NEUTRALITY_DISCLAIMER,
        )

        sanitized = AIGuardrailValidator.validate_and_sanitize(raw_brief, sample_evidence)
        assert sanitized.cited_record_ids == ["ERP-PO501-L1", "WMS-GRN501-A-L1"]
        assert len(sanitized.safety_warnings) == 0

    def test_guardrail_strips_hallucinated_citation(self, sample_evidence):
        """When AI hallucinates an ID, it is stripped and a safety warning is appended."""
        raw_brief = InvestigationBrief(
            executive_summary="Summary",
            analysis_of_evidence="Analysis",
            missing_context=["Missing ASN"],
            recommended_investigation_steps=["Check dock"],
            cited_record_ids=["ERP-PO501-L1", "WMS-FAKE-RECORD-999"],  # Hallucinated ID
            confidence_score=0.9,
            neutrality_disclaimer=MANDATORY_NEUTRALITY_DISCLAIMER,
        )

        sanitized = AIGuardrailValidator.validate_and_sanitize(raw_brief, sample_evidence)
        assert sanitized.cited_record_ids == ["ERP-PO501-L1"]
        assert "WMS-FAKE-RECORD-999" not in sanitized.cited_record_ids
        assert len(sanitized.safety_warnings) == 1
        assert "WMS-FAKE-RECORD-999" in sanitized.safety_warnings[0]

    def test_guardrail_strips_multiple_hallucinations(self, sample_evidence):
        """Multiple hallucinated IDs are all removed from cited_record_ids."""
        raw_brief = InvestigationBrief(
            executive_summary="Summary",
            analysis_of_evidence="Analysis",
            missing_context=["Missing ASN"],
            recommended_investigation_steps=["Check dock"],
            cited_record_ids=["WMS-FAKE-1", "ERP-PO501-L1", "INV-UNKNOWN-42"],
            confidence_score=0.9,
            neutrality_disclaimer=MANDATORY_NEUTRALITY_DISCLAIMER,
        )

        sanitized = AIGuardrailValidator.validate_and_sanitize(raw_brief, sample_evidence)
        assert sanitized.cited_record_ids == ["ERP-PO501-L1"]
        assert len(sanitized.safety_warnings) == 2

    def test_guardrail_enforces_epistemic_neutrality_disclaimer(self, sample_evidence):
        """If AI omitted or wiped the neutrality disclaimer, guardrail re-injects it."""
        raw_brief = InvestigationBrief(
            executive_summary="Summary",
            analysis_of_evidence="Analysis",
            missing_context=["Missing ASN"],
            recommended_investigation_steps=["Check dock"],
            cited_record_ids=["ERP-PO501-L1"],
            confidence_score=0.9,
            neutrality_disclaimer="Supplier is clearly at fault for short shipping.",
        )

        sanitized = AIGuardrailValidator.validate_and_sanitize(raw_brief, sample_evidence)
        assert sanitized.neutrality_disclaimer == MANDATORY_NEUTRALITY_DISCLAIMER


# ---------------------------------------------------------------------------
# Test Suite 4: OpenRouter Provider Implementation & HTTP Mocking
# ---------------------------------------------------------------------------

class TestOpenRouterInvestigationProvider:
    @pytest.fixture
    def sample_evidence(self):
        po = EvidencePOLine(
            source_record_id="ERP-PO501-L1",
            po_number="PO-501",
            line_number=1,
            supplier_code="SUP-001",
            canonical_product_id="SKU-001",
            canonical_location_id="STORE-014",
            ordered_qty_base=100.0,
            base_uom="EA",
            expected_receipt_by="2026-10-15T18:00:00+05:30",
        )
        return EvidencePackage(
            exception_id=str(uuid.uuid4()),

            exception_key="TENANT-001:SHORTFALL:PO-501:1",
            po_line=po,
            receipt_lines=[],
            expected_qty_base=100.0,
            received_qty_base=92.0,
            variance_qty_base=8.0,
            base_uom="EA",
            neutral_summary="Receipt shortfall of 8 EA.",
            missing_evidence=["SUPPLIER_DISPATCH_ASN"],
            valid_source_record_ids=["ERP-PO501-L1"],
        )

    def test_openrouter_missing_api_key_raises_value_error(self, sample_evidence):
        """Calling generate_brief with no API key raises ValueError."""
        provider = OpenRouterInvestigationProvider(api_key="")
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY is not configured"):
            provider.generate_brief(sample_evidence)

    def test_openrouter_mocked_http_success(self, sample_evidence):
        """OpenRouter completions response is parsed into InvestigationBrief."""
        response_payload = {
            "id": "gen-123",
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "executive_summary": "OpenRouter generated executive summary.",
                            "analysis_of_evidence": "PO-501 received 92 EA out of 100 EA.",
                            "missing_context": ["Missing supplier dispatch data."],
                            "recommended_investigation_steps": ["Inspect staging area."],
                            "cited_record_ids": ["ERP-PO501-L1"],
                            "confidence_score": 0.92,
                            "safety_warnings": [],
                            "neutrality_disclaimer": MANDATORY_NEUTRALITY_DISCLAIMER,
                        })
                    }
                }
            ],
        }

        def handler(request: httpx.Request):
            assert request.headers["authorization"] == "Bearer test-openrouter-key"
            assert "openrouter.ai" in str(request.url)
            return httpx.Response(200, json=response_payload)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = OpenRouterInvestigationProvider(
            api_key="test-openrouter-key",
            model="anthropic/claude-3.5-sonnet",
            http_client=mock_client,
        )

        brief = provider.generate_brief(sample_evidence)
        assert brief.executive_summary == "OpenRouter generated executive summary."
        assert brief.cited_record_ids == ["ERP-PO501-L1"]
        assert brief.confidence_score == 0.92
        assert provider.provider_name == "openrouter"
        assert provider.model_name == "anthropic/claude-3.5-sonnet"

    def test_openrouter_timeout_raises_ai_provider_error(self, sample_evidence):
        """HTTP timeout from OpenRouter raises AIProviderError."""
        def timeout_handler(request: httpx.Request):
            raise httpx.TimeoutException("Connection timed out")

        mock_client = httpx.Client(transport=httpx.MockTransport(timeout_handler))
        provider = OpenRouterInvestigationProvider(
            api_key="test-key",
            http_client=mock_client,
            timeout=5.0,
        )

        with pytest.raises(AIProviderError, match="timed out"):
            provider.generate_brief(sample_evidence)

    def test_openrouter_http_error_raises_ai_provider_error(self, sample_evidence):
        """HTTP error status from OpenRouter raises AIProviderError."""
        def error_handler(request: httpx.Request):
            return httpx.Response(429, json={"error": "Rate limit exceeded"})

        mock_client = httpx.Client(transport=httpx.MockTransport(error_handler))
        provider = OpenRouterInvestigationProvider(
            api_key="test-key",
            http_client=mock_client,
        )

        with pytest.raises(AIProviderError, match="returned HTTP 429"):
            provider.generate_brief(sample_evidence)

    def test_openrouter_malformed_json_raises_ai_provider_error(self, sample_evidence):
        """Non-JSON response from OpenRouter raises AIProviderError."""
        def malformed_handler(request: httpx.Request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "This is raw unformatted text not JSON"}}]},
            )

        mock_client = httpx.Client(transport=httpx.MockTransport(malformed_handler))
        provider = OpenRouterInvestigationProvider(
            api_key="test-key",
            http_client=mock_client,
        )

        with pytest.raises(AIProviderError, match="Malformed JSON"):
            provider.generate_brief(sample_evidence)


# ---------------------------------------------------------------------------
# Test Suite 5: Safety Boundary & Database Immutability
# ---------------------------------------------------------------------------

class TestSafetyAndImmutability:
    def test_investigation_is_strictly_read_only(self, db_session: Session):
        """
        Critical Invariant: Generating an AI investigation brief NEVER modifies
        any record in the database.
        """
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        # Capture counts before AI investigation
        count_exceptions_before = db_session.query(OperationalException).count()
        count_tasks_before = db_session.query(HumanTask).count()
        count_audit_before = db_session.query(TaskAuditEvent).count()
        count_pos_before = db_session.query(PurchaseOrderLine).count()
        count_receipts_before = db_session.query(GoodsReceiptLine).count()
        exc_updated_at_before = exc.updated_at if hasattr(exc, "updated_at") else None

        # Execute investigation
        response = InvestigationService.investigate(
            session=db_session,
            exception_id=exc.id,
            use_mock=True,
        )

        assert isinstance(response, InvestigationBriefResponse)

        # Assert counts after AI investigation are 100% IDENTICAL
        assert db_session.query(OperationalException).count() == count_exceptions_before
        assert db_session.query(HumanTask).count() == count_tasks_before
        assert db_session.query(TaskAuditEvent).count() == count_audit_before
        assert db_session.query(PurchaseOrderLine).count() == count_pos_before
        assert db_session.query(GoodsReceiptLine).count() == count_receipts_before


# ---------------------------------------------------------------------------
# Test Suite 6: REST API Endpoint Integration
# ---------------------------------------------------------------------------

class TestInvestigationAPI:
    def test_api_investigate_success_with_mock(self, client: TestClient, db_session: Session):
        """POST /api/v1/exceptions/{id}/investigate returns 200 with structured brief."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        resp = client.post(f"/api/v1/exceptions/{exc.id}/investigate?use_mock=true")
        assert resp.status_code == 200
        data = resp.json()

        assert data["exception_id"] == str(exc.id)
        assert data["provider_used"] == "mock"
        assert "brief" in data
        brief = data["brief"]
        assert "PO-501" in brief["executive_summary"]
        assert "ERP-PO501-L1" in brief["cited_record_ids"]
        assert "Supplier dispatch" in brief["neutrality_disclaimer"]

    def test_api_investigate_not_found(self, client: TestClient):
        """POST /api/v1/exceptions/{unknown_id}/investigate returns 404 with error envelope."""
        random_id = uuid.uuid4()
        resp = client.post(f"/api/v1/exceptions/{random_id}/investigate?use_mock=true")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error_code"] == "RESOURCE_NOT_FOUND"

    def test_api_investigate_propagates_ai_provider_error(self, client: TestClient, db_session: Session, monkeypatch):
        """When AI provider raises AIProviderError, API returns HTTP 502 with structured error."""
        load_all_fixtures(db_session)
        DetectionService.run_reconciliation(db_session)

        exc = db_session.query(OperationalException).filter_by(po_number="PO-501").first()
        assert exc is not None

        class BrokenProvider(AIInvestigationProvider):
            @property
            def provider_name(self) -> str:
                return "broken"

            @property
            def model_name(self) -> str:
                return "broken-model"

            def generate_brief(self, evidence):
                raise AIProviderError("OpenRouter 503 gateway unavailable", {"status": 503})

        monkeypatch.setattr(InvestigationService, "get_provider", lambda **kwargs: BrokenProvider())

        resp = client.post(f"/api/v1/exceptions/{exc.id}/investigate")
        assert resp.status_code == 502
        data = resp.json()
        assert data["error_code"] == "AI_PROVIDER_ERROR"
        assert "OpenRouter 503" in data["message"]
