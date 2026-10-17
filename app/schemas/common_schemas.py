"""
Common Schemas and API Error Envelopes.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """RFC 7807-compatible structured error response envelope."""
    error_code: str = Field(..., description="Machine-readable error identifier")
    message: str = Field(..., description="Human-readable error description")
    detail: Optional[str] = Field(None, description="Detailed explanation or error message")
    details: Optional[Dict[str, Any]] = Field(None, description="Contextual parameters or validation details")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Error timestamp in UTC")

    def __init__(self, **data: Any):
        if "detail" not in data and "message" in data:
            data["detail"] = data["message"]
        super().__init__(**data)
