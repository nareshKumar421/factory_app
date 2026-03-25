import logging

from django.db import transaction
from django.utils.timezone import now
from rest_framework.exceptions import ValidationError

from driver_management.models import VehicleEntry

logger = logging.getLogger(__name__)


@transaction.atomic
def complete_outbound_gate_entry(vehicle_entry: VehicleEntry):
    """
    Completes (locks) an Outbound gate entry.

    Validations:
    - Entry type must be OUTBOUND
    - Not already locked
    - Security check must be submitted
    - Outbound entry must exist
    - Vehicle must be confirmed empty

    Args:
        vehicle_entry: VehicleEntry instance to complete
    """
    if vehicle_entry.is_locked:
        raise ValidationError("Gate entry already completed")

    if vehicle_entry.entry_type != "OUTBOUND":
        raise ValidationError("Invalid entry type for outbound completion")

    if not hasattr(vehicle_entry, "security_check"):
        raise ValidationError("Security check not completed")

    security_check = vehicle_entry.security_check
    if not getattr(security_check, "is_submitted", False):
        raise ValidationError("Security check not submitted")

    if not hasattr(vehicle_entry, "outbound_entry"):
        raise ValidationError("Outbound entry not filled")

    outbound_entry = vehicle_entry.outbound_entry
    if not outbound_entry.vehicle_empty_confirmed:
        raise ValidationError(
            "Vehicle must be confirmed empty before completing gate entry"
        )

    vehicle_entry.status = "COMPLETED"
    vehicle_entry.is_locked = True
    vehicle_entry.updated_at = now()
    vehicle_entry.save(update_fields=["status", "is_locked", "updated_at"])

    logger.info(
        f"Outbound gate entry completed. Vehicle entry ID: {vehicle_entry.id}"
    )
