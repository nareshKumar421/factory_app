import logging

from django.db import transaction
from django.utils.timezone import now

from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from gate_core.services import ensure_editable

logger = logging.getLogger(__name__)


@transaction.atomic
def complete_fg_gate_entry(vehicle_entry: VehicleEntry):
    """Complete (and lock) a finished-goods gate entry.

    Finished goods are traded (bought-in) — there is NO QC arrival slip or
    inspection, and no weighment requirement. The only preconditions are that
    the entry is a FINISHED_GOODS entry, is still editable, and has at least one
    PO line received. On completion the entry goes straight to COMPLETED, which
    makes it eligible for the finished-goods material GRPO surfaces.

    Raises:
        ValueError: if a precondition is not met (mapped to HTTP 400 by the view).
    """
    ensure_editable(vehicle_entry)

    if vehicle_entry.entry_type != "FINISHED_GOODS":
        raise ValueError("Invalid entry type for finished goods completion")

    po_items = []
    for po in vehicle_entry.po_receipts.all():
        po_items.extend(list(po.items.all()))

    if not po_items:
        raise ValueError("No PO items received")

    vehicle_entry.status = GateEntryStatus.COMPLETED
    vehicle_entry.is_locked = True
    vehicle_entry.updated_at = now()
    vehicle_entry.save(update_fields=["status", "is_locked", "updated_at"])

    logger.info(
        "Finished goods gate entry completed. Vehicle entry ID: %s",
        vehicle_entry.id,
    )
    return True
