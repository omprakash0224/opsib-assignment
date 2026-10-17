"""
Normalization and Alias Crosswalk Resolver.
Handles entity identity resolution, UOM conversions, and unmapped error isolation.
"""
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.db.models.canonical import ProductAlias, LocationAlias
from app.core.exceptions import UnmappedEntityError


class Normalizer:
    """Provides lookup and conversion helpers against the crosswalk aliases."""

    @staticmethod
    def resolve_product(
        session: Session, source_system: str, external_item_id: str, quantity: float
    ) -> Tuple[str, float, str]:
        """
        Resolves external item ID to canonical product ID and converts quantity to base UOM.
        Returns: (canonical_product_id, quantity_base, base_uom)
        Raises: UnmappedEntityError if item alias is not found.
        """
        alias = (
            session.query(ProductAlias)
            .filter_by(source_system=source_system, external_item_id=external_item_id)
            .first()
        )
        if not alias:
            raise UnmappedEntityError(
                f"Unmapped product: source_system='{source_system}', external_item_id='{external_item_id}'"
            )

        quantity_base = quantity * alias.to_base_factor
        return alias.canonical_product_id, quantity_base, alias.base_uom

    @staticmethod
    def resolve_location(
        session: Session, source_system: str, external_location_id: str
    ) -> Tuple[str, str]:
        """
        Resolves external location ID to canonical location ID and region ID.
        Returns: (canonical_location_id, region_id)
        Raises: UnmappedEntityError if location alias is not found.
        """
        alias = (
            session.query(LocationAlias)
            .filter_by(source_system=source_system, external_location_id=external_location_id)
            .first()
        )
        if not alias:
            raise UnmappedEntityError(
                f"Unmapped location: source_system='{source_system}', external_location_id='{external_location_id}'"
            )

        return alias.canonical_location_id, alias.region_id

    @staticmethod
    def resolve_employee_location(
        session: Session, location_code: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Resolves employee location code from HRMS.
        If location_code is null/empty (e.g. regional role HR-501), returns (None, None).
        Otherwise resolves against LocationAlias(source_system='HRMS').
        """
        if not location_code:
            return None, None

        alias = (
            session.query(LocationAlias)
            .filter_by(source_system="HRMS", external_location_id=location_code)
            .first()
        )
        if not alias:
            # Also try ERP/WMS as fallback if HRMS code matches directly
            alias = (
                session.query(LocationAlias)
                .filter_by(external_location_id=location_code)
                .first()
            )

        if not alias:
            raise UnmappedEntityError(
                f"Unmapped employee location: external_location_id='{location_code}'"
            )

        return alias.canonical_location_id, alias.region_id
