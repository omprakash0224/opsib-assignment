"""
Security and Request Context Dependencies.
Provides Tenant and Actor context extraction from incoming HTTP headers.
"""
from typing import Optional
from fastapi import Header
from app.config import DEFAULT_TENANT_ID


def get_tenant_id(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID", description="Tenant Identifier")
) -> str:
    """Extracts tenant ID from header or falls back to DEFAULT_TENANT_ID."""
    return x_tenant_id or DEFAULT_TENANT_ID


def get_actor_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID", description="Actor / User Identifier")
) -> str:
    """Extracts actor ID from header or falls back to SYSTEM."""
    return x_user_id or "SYSTEM"
