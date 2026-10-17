"""
Detection Rules: Versioned rule constants and variance calculation helpers.

All business logic parameters are versioned together under RULE_VERSION so that
any future change in tolerance or eligibility criteria is tracked end-to-end.
"""
from app.config import RULE_VERSION

# ---------------------------------------------------------------------------
# Rule Parameters (v1.0.0)
# ---------------------------------------------------------------------------

#: Zero-tolerance: any positive variance (ordered > received) is a shortfall.
VARIANCE_TOLERANCE: float = 0.0

#: PO statuses that are eligible for shortfall checking.
ELIGIBLE_PO_STATUSES: frozenset = frozenset({"OPEN"})

#: Only POSTED receipts count toward fulfillment.
ELIGIBLE_RECEIPT_POSTING_STATUSES: frozenset = frozenset({"POSTED"})

#: Placeholder text for the missing evidence note (ADR-6 – Epistemic Neutrality).
MISSING_EVIDENCE_NOTE: list = [
    "SUPPLIER_DISPATCH_ASN",
    "CARRIER_PROOF_OF_DELIVERY",
]


def compute_variance(expected_qty_base: float, received_qty_base: float) -> float:
    """
    Compute the shortfall variance between expected and received base quantities.

    Returns:
        Positive float if there is a shortfall (expected > received).
        Zero or negative means fully satisfied.
    """
    return expected_qty_base - received_qty_base


def build_neutral_summary(
    po_number: str,
    po_line_number: int,
    variance: float,
    base_uom: str,
    expected_qty: float,
    received_qty: float,
) -> str:
    """
    Builds a factual, attribution-free shortfall description (ADR-6).

    The summary deliberately avoids assigning blame to the supplier or carrier
    because no Advanced Shipping Notice (ASN) or carrier Proof of Delivery
    data is available in the evidence set.
    """
    return (
        f"Receipt shortfall of {variance:.2f} {base_uom} detected against "
        f"Purchase Order {po_number} Line {po_line_number}. "
        f"Expected {expected_qty:.2f} {base_uom}; "
        f"received {received_qty:.2f} {base_uom} (POSTED receipts only). "
        f"Supplier dispatch / ASN data absent; findings do not establish "
        f"supplier fault or transit loss."
    )


def build_exception_key(tenant_id: str, po_number: str, po_line_number: int) -> str:
    """
    Builds the deterministic idempotent business key for an exception.

    Format: <TENANT_ID>:RECEIPT_SHORTFALL:<PO_NUMBER>:<LINE_NUMBER>

    A unique database constraint on this key prevents duplicate exceptions even
    when reconciliation runs concurrently on multiple workers (ADR-1, ADR-5).
    """
    return f"{tenant_id}:RECEIPT_SHORTFALL:{po_number}:{po_line_number}"
