import logging

from django.db import transaction
from django.utils.timezone import now
from rest_framework.exceptions import ValidationError

from driver_management.models import VehicleEntry

logger = logging.getLogger(__name__)


@transaction.atomic
def complete_fixed_asset_gate_entry(vehicle_entry: VehicleEntry):
    """
    Completes (locks) a Fixed Asset gate entry.

    Business Rules (no security check in this 4-step flow):
    1. Entry must not already be locked
    2. Entry type must be FIXED_ASSET
    3. Fixed asset entry must exist
    4. Supplier name is required
    5. At least one asset line item must be present

    Raises:
        ValidationError: If any validation check fails
    """

    if vehicle_entry.is_locked:
        raise ValidationError("Gate entry already completed")

    if vehicle_entry.entry_type != "FIXED_ASSET":
        raise ValidationError("Invalid entry type for fixed asset completion")

    if not hasattr(vehicle_entry, "fixed_asset_entry"):
        raise ValidationError("Fixed asset entry not filled")

    asset_entry = vehicle_entry.fixed_asset_entry

    if not asset_entry.supplier_name or not asset_entry.supplier_name.strip():
        raise ValidationError("Supplier name is required")

    if not asset_entry.items.exists():
        raise ValidationError("At least one asset must be added")

    # All validations passed - complete the gate entry
    vehicle_entry.status = "COMPLETED"
    vehicle_entry.is_locked = True
    vehicle_entry.updated_at = now()
    vehicle_entry.save(update_fields=["status", "is_locked", "updated_at"])

    logger.info(
        f"Fixed asset gate entry completed. Vehicle entry ID: {vehicle_entry.id}"
    )
