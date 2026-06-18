from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

DAILY_NEEDS_GROUP = "daily_needs_gatein"
DAILY_NEEDS_URL = "/gate/daily-needs"


def notify_daily_need_entry_created(entry):
    """Notify the daily-needs gate group when a new entry is created."""
    vehicle_entry = entry.vehicle_entry
    company = getattr(vehicle_entry, "company", None)
    entry_no = getattr(vehicle_entry, "entry_no", "")

    extra_data = {
        "reference_type": "daily_need_entry",
        "reference_id": str(entry.id),
        "vehicle_entry_id": str(vehicle_entry.id),
        "entry_no": entry_no,
        "material_name": entry.material_name,
        "supplier_name": entry.supplier_name,
        "quantity": str(entry.quantity),
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=DAILY_NEEDS_GROUP,
            title="Daily Need Gate Entry Created",
            body=(
                f"Daily need entry {entry_no} for {entry.material_name} "
                f"from {entry.supplier_name} has been created."
            ),
            notification_type=NotificationType.DAILY_NEED_ENTRY_CREATED,
            click_action_url=DAILY_NEEDS_URL,
            reference_type="daily_need_entry",
            reference_id=entry.id,
            company=company,
            extra_data=extra_data,
            created_by=entry.created_by,
        )
    )
