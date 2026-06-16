from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

CONSTRUCTION_GROUP = "construction_gatein"
CONSTRUCTION_URL = "/gate/construction"


def notify_construction_entry_created(entry):
    """Notify the construction gate group when a new entry is created."""
    vehicle_entry = entry.vehicle_entry
    company = getattr(vehicle_entry, "company", None)
    entry_no = getattr(vehicle_entry, "entry_no", "")

    extra_data = {
        "reference_type": "construction_entry",
        "reference_id": str(entry.id),
        "vehicle_entry_id": str(vehicle_entry.id),
        "entry_no": entry_no,
        "work_order_number": entry.work_order_number or "",
        "project_name": entry.project_name or "",
        "contractor_name": entry.contractor_name,
        "quantity": str(entry.quantity),
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=CONSTRUCTION_GROUP,
            title="Construction Gate Entry Created",
            body=(
                f"Construction entry {entry_no} ({entry.work_order_number or 'no WO'}) "
                f"by {entry.contractor_name} has been created."
            ),
            notification_type=NotificationType.CONSTRUCTION_ENTRY_CREATED,
            click_action_url=CONSTRUCTION_URL,
            reference_type="construction_entry",
            reference_id=entry.id,
            company=company,
            extra_data=extra_data,
            created_by=entry.created_by,
        )
    )
