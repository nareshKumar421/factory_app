# raw_material_gatein/services/po_line_bookings.py

"""What other gate entries have already claimed on a PO line.

SAP's open quantity is the truth at the moment it is read, and the gate reads it
at gate-in — but nothing is reserved. Two trucks can be gated in against the same
PO line with room for only one of them, and the loser finds out at GRPO posting
hours later, with the material already unloaded and QC done (GE-2026-8871,
2026-09-15: 1,960 PCS of PM0000914 booked against PO 220826133 at 09:33 while
3,862 were open, and a different truck's GRPO consumed all 3,862 at 12:20).

These helpers make that visible at gate-in, where the PO can still be changed.
They are advisory: a booking is not a reservation, so this never blocks a receipt
that SAP itself would accept.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def po_line_bookings(sap_doc_entry, sap_line_num, *, exclude_po_receipt_id=None):
    """``[(entry_no, qty), ...]`` already booked on this PO line elsewhere.

    Counts only receipts that still have to draw on the PO line: a gate entry
    whose GRPO has posted has already consumed its share of ``OpenQty``, so
    counting it would double-count, and a cancelled entry never will.
    """
    # Imported here: grpo.services imports this package's validations, so a
    # module-level import of the models would close the loop at startup.
    from django.db.models import Q

    from grpo.models import GRPOStatus
    from gate_core.enums import GateEntryStatus
    from ..models import POItemReceipt

    if sap_doc_entry is None or sap_line_num is None:
        return []

    queryset = (
        POItemReceipt.objects.filter(
            po_receipt__sap_doc_entry=sap_doc_entry,
            sap_line_num=sap_line_num,
            received_qty__gt=0,
        )
        .exclude(
            po_receipt__vehicle_entry__status__in=[
                GateEntryStatus.CANCELLED,
                GateEntryStatus.QC_REJECTED,
            ]
        )
        .exclude(grpo_lines__grpo_posting__status=GRPOStatus.POSTED)
        .select_related("po_receipt__vehicle_entry")
    )

    if exclude_po_receipt_id is not None:
        queryset = queryset.exclude(po_receipt_id=exclude_po_receipt_id)

    return [
        (item.po_receipt.vehicle_entry.entry_no, Decimal(str(item.received_qty)))
        for item in queryset
    ]


def booking_overlap_warning(
    sap_doc_entry,
    sap_line_num,
    *,
    item_label,
    received_qty,
    remaining_qty,
    uom="",
    exclude_po_receipt_id=None,
):
    """A warning string when this line's open quantity is already over-promised.

    ``None`` when the open quantity covers this receipt and every other booking
    against the line, which is the ordinary case.
    """
    from .validations import _format_qty, _to_decimal

    try:
        bookings = po_line_bookings(
            sap_doc_entry, sap_line_num, exclude_po_receipt_id=exclude_po_receipt_id
        )
    except Exception as exc:
        # Advisory only — never let it break a receipt the gate should accept.
        logger.warning(
            "Could not read existing bookings for PO line %s/%s: %s",
            sap_doc_entry, sap_line_num, exc,
        )
        return None

    if not bookings:
        return None

    booked = sum((qty for _, qty in bookings), Decimal("0"))
    remaining_qty = _to_decimal(remaining_qty)
    if booked + _to_decimal(received_qty) <= remaining_qty:
        return None

    unit = f" {uom}" if uom else ""
    entries = ", ".join(
        f"{entry_no} ({_format_qty(qty)}{unit})" for entry_no, qty in bookings
    )
    return (
        f"{item_label}: only {_format_qty(remaining_qty)}{unit} is open on this PO "
        f"line and {_format_qty(booked)}{unit} of it is already booked by "
        f"{entries}, not yet posted to SAP. Adding {_format_qty(received_qty)}"
        f"{unit} over-promises the line — whichever GRPO posts last will be "
        f"refused. Check whether this material belongs on a newer PO."
    )
