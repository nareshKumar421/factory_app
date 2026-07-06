import logging

from django.db import transaction
from django.utils.timezone import now
from rest_framework.exceptions import ValidationError

from driver_management.models import VehicleEntry

logger = logging.getLogger(__name__)


@transaction.atomic
def complete_maintenance_gate_entry(vehicle_entry: VehicleEntry):
    """
    Completes (locks) a Maintenance & Repair Material gate entry.
    No QC, no Weighment required for this entry type (similar to daily_need).

    Args:
        vehicle_entry: VehicleEntry instance to complete

    Raises:
        ValidationError: If any validation check fails
    """

    # Already locked check
    if vehicle_entry.is_locked:
        raise ValidationError("Gate entry already completed")

    # Entry type validation
    if vehicle_entry.entry_type != "MAINTENANCE":
        raise ValidationError(
            "Invalid entry type for maintenance completion"
        )

    # Maintenance entry must exist
    if not hasattr(vehicle_entry, "maintenance_entry"):
        raise ValidationError("Maintenance entry not filled")

    # All validations passed - complete the gate entry
    vehicle_entry.status = "COMPLETED"
    vehicle_entry.is_locked = True
    vehicle_entry.updated_at = now()
    vehicle_entry.save(update_fields=[
        "status",
        "is_locked",
        "updated_at"
    ])

    logger.info(
        f"Maintenance gate entry completed. Vehicle entry ID: {vehicle_entry.id}"
    )
