from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

PERSON_GATE_GROUP = "person_gatein"
PERSON_GATE_URL = "/gate/visitor-labour"


def _entry_extra(entry, **extra):
    data = {
        "reference_type": "person_entry",
        "reference_id": str(entry.id),
        "name": entry.name_snapshot,
        "person_type": entry.person_type.name if entry.person_type_id else "",
        "status": entry.status,
        "gate_in": entry.gate_in.name if entry.gate_in_id else "",
        "vehicle_no": entry.vehicle_no or "",
    }
    data.update({key: str(value) for key, value in extra.items() if value is not None})
    return data


def _send_after_commit(*, title, body, notification_type, reference_id, extra_data, created_by):
    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=PERSON_GATE_GROUP,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=PERSON_GATE_URL,
            reference_type="person_entry",
            reference_id=reference_id,
            company=None,
            extra_data=extra_data,
            created_by=created_by,
        )
    )


def notify_person_entry_created(entry):
    """Notify the person gate group when a person enters."""
    person_type = entry.person_type.name if entry.person_type_id else "Person"
    _send_after_commit(
        title="Person Gate Entry",
        body=(
            f"{person_type} {entry.name_snapshot} entered via "
            f"{entry.gate_in.name if entry.gate_in_id else 'gate'}."
        ),
        notification_type=NotificationType.PERSON_ENTRY_CREATED,
        reference_id=entry.id,
        extra_data=_entry_extra(entry),
        created_by=entry.created_by,
    )


def notify_person_entry_exited(entry):
    """Notify the person gate group when a person exits."""
    person_type = entry.person_type.name if entry.person_type_id else "Person"
    _send_after_commit(
        title="Person Gate Exit",
        body=(
            f"{person_type} {entry.name_snapshot} exited via "
            f"{entry.gate_out.name if entry.gate_out_id else 'gate'}."
        ),
        notification_type=NotificationType.PERSON_ENTRY_EXITED,
        reference_id=entry.id,
        extra_data=_entry_extra(
            entry,
            gate_out=entry.gate_out.name if entry.gate_out_id else "",
        ),
        created_by=entry.created_by,
    )
