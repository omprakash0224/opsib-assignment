"""
Domain and API Exception Definitions.
"""
from typing import Any, Dict, Optional


class OPSIBException(Exception):
    """Base domain exception for OPSIB."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DataQualityError(OPSIBException):
    """Raised when incoming data fails structural or reference crosswalk mapping."""
    pass


class UnmappedEntityError(DataQualityError):
    """Raised when an external item or location cannot be mapped to a canonical entity."""
    pass


class ConflictingDuplicateError(DataQualityError):
    """Raised when an incoming record with an existing source_id arrives with conflicting payload."""
    pass


class InvalidStateTransitionError(OPSIBException):
    """Raised when an illegal lifecycle transition is attempted on a task."""

    def __init__(self, from_status: str, to_status: str, allowed: list[str]):
        message = (
            f"Cannot transition task from '{from_status}' to '{to_status}'. "
            f"Allowed next states: {allowed}."
        )
        super().__init__(message, {"from_status": from_status, "to_status": to_status, "allowed": allowed})


class IdempotencyConflictError(OPSIBException):
    """Raised when an idempotent operation is repeated with conflicting input parameters."""
    pass


class ResourceNotFoundError(OPSIBException):
    """Raised when an entity is not found in the persistence layer."""
    pass


class AIProviderError(OPSIBException):
    """Raised when an external AI provider (e.g. OpenRouter) fails or times out."""
    pass

