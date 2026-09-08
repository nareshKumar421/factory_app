# raw_material_gatein/services/validations.py

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings

logger = logging.getLogger(__name__)

# SAP refuses a receipt above 110% of the PO line's *open* quantity — not 110% of
# the quantity originally ordered. `SBO_SP_TransactionNotification` checks
#
#     PDN1."Quantity" > PDN1."BaseOpnQty" * 1.10   -> 200017
#     DRF1."Quantity" > DRF1."BaseOpnQty" * 1.10   -> 1120023
#
# and its message ("GRPO Quantity cannot be greater than the PO quantity + 10%")
# is what misled the original implementation here. Capping on the ordered quantity
# is what let 12,000 PCS be received onto a PO line with 9,000 PCS still open: the
# gate saw a 165,000 ceiling, SAP saw 9,900, and nobody found out until posting.
OVER_RECEIPT_TOLERANCE = Decimal("1.10")

# OCRD.GroupCode 101 = "BRANCH VENDOR" — the intercompany/branch legs. SAP's own
# over-receipt check skips this group (as does its receiving-attachment check, see
# GRPOService.SAP_ATTACHMENT_EXEMPT_BP_GROUP_CODE), so we skip it too rather than
# block a receipt SAP would happily accept.
BRANCH_VENDOR_BP_GROUP_CODE = 101


def _to_decimal(value, default=Decimal("0")):
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _format_qty(value):
    """Trim trailing zeros so an error reads '9,900' rather than '9900.000000'."""
    quantised = _to_decimal(value).quantize(Decimal("0.001")).normalize()
    if quantised == quantised.to_integral_value():
        quantised = quantised.to_integral_value()
    return f"{quantised:,f}"


def as_qty(value):
    """A quantity as a Decimal at the 3 decimal places the qty columns store.

    SAP's PO DTOs carry floats, and ``POItemReceipt.save`` derives ``short_qty`` by
    subtracting one qty from another — mixing a float in there is a TypeError.
    """
    return _to_decimal(value).quantize(Decimal("0.001"))


def over_receipt_ceiling(remaining_qty):
    """Most that may be received against a PO line with ``remaining_qty`` still open."""
    return _to_decimal(remaining_qty) * OVER_RECEIPT_TOLERANCE


def is_over_receipt_enforced(company_code):
    """Whether the tolerance applies in this company at all.

    SAP only enforces it in Oil. The posted-GRPO rule (PDN1, error 200017) is
    commented out in the Mart and Beverages copies of the procedure, and what
    survives there guards GRPO *drafts* (DRF1) — which the Service Layer never
    creates. Enforcing it in those companies would block receipts SAP accepts today,
    so the gate follows SAP company by company via
    ``settings.GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES``.
    """
    enforced = getattr(
        settings, "GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES", []
    ) or []
    return (company_code or "").strip() in {str(code).strip() for code in enforced}


def is_over_receipt_exempt(company_code, supplier_code, bp_group_code=None):
    """Whether this receipt sits outside the over-receipt check.

    True when the company does not enforce the rule at all, or when the vendor is one
    SAP's own check waves through.

    ``bp_group_code`` may be passed in when the caller has already read it; leave it
    ``None`` and it is looked up. A failed lookup is *not* treated as an exemption —
    the tolerance still applies, so a flaky read cannot silently open the gate.
    """
    # Checked first so an unenforced company costs no SAP reads.
    if not is_over_receipt_enforced(company_code):
        return True

    supplier_code = (supplier_code or "").strip()
    if not supplier_code:
        return False

    exempt_vendors = (
        getattr(settings, "GRPO_OVER_RECEIPT_EXEMPT_VENDORS", {}) or {}
    ).get(company_code) or []
    if supplier_code in {str(code).strip() for code in exempt_vendors}:
        return True

    if bp_group_code is None:
        bp_group_code = _read_bp_group_code(company_code, supplier_code)

    return bp_group_code == BRANCH_VENDOR_BP_GROUP_CODE


def _read_bp_group_code(company_code, supplier_code):
    # Imported lazily: sap_client pulls in company context and settings, and the
    # gate views import this module at startup.
    try:
        from sap_client.client import SAPClient

        return SAPClient(company_code=company_code).customer_group_code(supplier_code)
    except Exception as exc:
        logger.warning(
            "Could not read BP GroupCode for %s in %s: %s",
            supplier_code, company_code, exc,
        )
        return None


def validate_received_quantity(
    ordered_qty,
    remaining_qty,
    received_qty,
    *,
    item_label="",
    uom="",
    exempt=False,
):
    """Refuse a receipt SAP would refuse at posting time.

    The ceiling is ``remaining_qty * 1.10`` — 110% of what is still *open* on the PO
    line, which is the number SAP checks. ``ordered_qty`` is used only to explain the
    rejection, so it is safe for it to be a display value.
    """
    received_qty = _to_decimal(received_qty)
    remaining_qty = _to_decimal(remaining_qty)

    if received_qty <= 0:
        raise ValueError("Received quantity must be greater than zero")

    if exempt:
        return

    label = f"{item_label} " if item_label else ""
    unit = f" {uom}" if uom else ""

    if remaining_qty <= 0:
        raise ValueError(
            f"{label}is fully received on this PO — nothing is open against it, so "
            f"SAP will not accept another receipt. Use a fresh PO line."
        )

    max_allowed = over_receipt_ceiling(remaining_qty)
    if received_qty > max_allowed:
        raise ValueError(
            f"{label}cannot be received as {_format_qty(received_qty)}{unit}: only "
            f"{_format_qty(remaining_qty)}{unit} is still open on this PO line "
            f"(of {_format_qty(ordered_qty)}{unit} ordered), so the most SAP will "
            f"accept is {_format_qty(max_allowed)}{unit} (open + 10% tolerance)."
        )
