"""
Citation Guardrail & Safety Validator for AI Copilot (Task D / ADR-7).

Enforces:
1. Grounded citation integrity: every cited record ID must exist in the evidence package whitelist.
   Ungrounded or hallucinated IDs are stripped, and an explicit safety warning is recorded.
2. Epistemic neutrality assurance: ensures mandatory disclaimers regarding absent ASN data are preserved.
"""
import logging
from typing import Set
from app.schemas.ai_schemas import EvidencePackage, InvestigationBrief

logger = logging.getLogger(__name__)

MANDATORY_NEUTRALITY_DISCLAIMER = (
    "Advisory summary only. Supplier dispatch / ASN data absent; "
    "findings do not establish supplier fault or transit loss."
)


class AIGuardrailValidator:
    """
    Validates and sanitizes AI-generated investigation briefs before returning
    to the caller or client.
    """

    @classmethod
    def validate_and_sanitize(
        cls,
        brief: InvestigationBrief,
        evidence: EvidencePackage,
    ) -> InvestigationBrief:
        """
        Runs citation integrity checks and epistemic neutrality verification.
        Mutates a copy of the brief to strip hallucinated citations and attach warnings.
        """
        valid_ids_set: Set[str] = set(evidence.valid_source_record_ids)
        sanitized_cited_ids = []
        warnings = list(brief.safety_warnings)

        for cited_id in brief.cited_record_ids:
            if cited_id in valid_ids_set:
                sanitized_cited_ids.append(cited_id)
            else:
                warning_msg = (
                    f"Citation Guardrail: Stripped ungrounded citation '{cited_id}' "
                    f"which does not exist in the evidence package."
                )
                logger.warning(warning_msg)
                warnings.append(warning_msg)

        # Enforce epistemic disclaimer
        disclaimer = brief.neutrality_disclaimer
        if not disclaimer or "supplier dispatch" not in disclaimer.lower():
            disclaimer = MANDATORY_NEUTRALITY_DISCLAIMER

        # Return sanitized brief
        return brief.model_copy(
            update={
                "cited_record_ids": sanitized_cited_ids,
                "safety_warnings": warnings,
                "neutrality_disclaimer": disclaimer,
            }
        )
