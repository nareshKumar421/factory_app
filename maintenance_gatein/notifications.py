from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

MAINTENANCE_GROUP = "maintenance_gatein"
MAINTENANCE_URL = "/gate/maintenance"


def notify_maintenance_entry_created(entry):
    """Notify the maintenance gate group when a new entry is created."""
    vehicle_entry = entry.vehicle_entry
    company = getattr(vehicle_entry, "company", None)
    entry_no = getattr(vehicle_entry, "entry_no", "")

    urgency = entry.get_urgency_level_display()
    urgency_note = f" [{urgency}]" if entry.urgency_level != "NORMAL" else ""

    extra_data = {
        "reference_type": "maintenance_entry",
        "reference_id": str(entry.id),
        "vehicle_entry_id": str(vehicle_entry.id),
        "entry_no": entry_no,
        "work_order_number": entry.work_order_number or "",
        "supplier_name": entry.supplier_name,
        "urgency_level": entry.urgency_level,
        "quantity": str(entry.quantity),
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=MAINTENANCE_GROUP,
            title=f"Maintenance Gate Entry Created{urgency_note}",
            body=(
                f"Maintenance entry {entry_no} ({entry.work_order_number or 'no WO'}) "
                f"from {entry.supplier_name} has been created."
            ),
            notification_type=NotificationType.MAINTENANCE_ENTRY_CREATED,
            click_action_url=MAINTENANCE_URL,
            reference_type="maintenance_entry",
            reference_id=entry.id,
            company=company,
            extra_data=extra_data,
            created_by=entry.created_by,
        )
    )
