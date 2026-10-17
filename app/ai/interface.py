"""
Abstract Provider Interface for AI Advisory Copilot (Task D / ADR-7).
"""
from abc import ABC, abstractmethod
from app.schemas.ai_schemas import EvidencePackage, InvestigationBrief


class AIInvestigationProvider(ABC):
    """
    Abstract interface for AI Investigation Providers.
    All providers must accept a grounded EvidencePackage and return
    a structured InvestigationBrief.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the provider implementation (e.g. 'openrouter', 'mock')."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The specific model identifier used by this provider."""
        pass

    @abstractmethod
    def generate_brief(self, evidence: EvidencePackage) -> InvestigationBrief:
        """
        Generate an advisory investigation brief grounded in the evidence package.
        Must NOT calculate authoritative quantities or mutate system state.
        """
        pass
