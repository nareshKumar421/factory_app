from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService


RAW_MATERIAL_GROUP = "raw_material_gatein"
GRPO_GROUP = "grpo"


def raw_material_entry_url(entry, step="review"):
    return f"/gate/raw-materials/edit/{entry.id}/{step}"


def _entry_data(entry, **extra):
    data = {
        "reference_type": "vehicle_entry",
        "reference_id": str(entry.id),
        "vehicle_entry_id": str(entry.id),
        "entry_no": entry.entry_no,
        "entry_type": entry.entry_type,
        "status": entry.status,
    }
    data.update({key: str(value) for key, value in extra.items() if value is not None})
    return data


def _send_to_group_after_commit(
    *,
    group_name,
    title,
    body,
    notification_type,
    click_action_url,
    company,
    reference_type,
    reference_id,
    extra_data,
    created_by=None,
):
    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=group_name,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=click_action_url,
            reference_type=reference_type,
            reference_id=reference_id,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
        )
    )


def notify_gate_entry_created(entry):
    if entry.entry_type != "RAW_MATERIAL":
        return

    _send_to_group_after_commit(
        group_name=RAW_MATERIAL_GROUP,
        title="Raw Material Gate Entry Created",
        body=f"Raw material gate entry {entry.entry_no} has been created.",
        notification_type=NotificationType.GATE_ENTRY_CREATED,
        click_action_url=raw_material_entry_url(entry, "step1"),
        company=entry.company,
        reference_type="vehicle_entry",
        reference_id=entry.id,
        extra_data=_entry_data(entry),
        created_by=entry.created_by,
    )


def notify_security_check_done(security_check):
    entry = security_check.vehicle_entry
    if entry.entry_type != "RAW_MATERIAL":
        return

    _send_to_group_after_commit(
        group_name=RAW_MATERIAL_GROUP,
        title="Security Check Completed",
        body=f"Security check is completed for entry {entry.entry_no}.",
        notification_type=NotificationType.SECURITY_CHECK_DONE,
        click_action_url=raw_material_entry_url(entry, "step3"),
        company=entry.company,
        reference_type="security_check",
        reference_id=security_check.id,
        extra_data=_entry_data(
            entry,
            security_check_id=security_check.id,
            inspected_by_name=security_check.inspected_by_name,
        ),
        created_by=security_check.updated_by or security_check.created_by,
    )


def notify_weighment_recorded(weighment):
    entry = weighment.vehicle_entry
    if entry.entry_type != "RAW_MATERIAL":
        return

    if weighment.gross_weight is None and weighment.tare_weight is None:
        return

    weight_bits = []
    if weighment.gross_weight is not None:
        weight_bits.append(f"gross {weighment.gross_weight}")
    if weighment.tare_weight is not None:
        weight_bits.append(f"tare {weighment.tare_weight}")
    if weighment.net_weight is not None:
        weight_bits.append(f"net {weighment.net_weight}")

    _send_to_group_after_commit(
        group_name=RAW_MATERIAL_GROUP,
        title="Weighment Recorded",
        body=f"Weighment recorded for entry {entry.entry_no}: {', '.join(weight_bits)}.",
        notification_type=NotificationType.WEIGHMENT_RECORDED,
        click_action_url=raw_material_entry_url(entry, "step5"),
        company=entry.company,
        reference_type="weighment",
        reference_id=weighment.id,
        extra_data=_entry_data(
            entry,
            weighment_id=weighment.id,
            gross_weight=weighment.gross_weight,
            tare_weight=weighment.tare_weight,
            net_weight=weighment.net_weight,
        ),
        created_by=weighment.updated_by or weighment.created_by,
    )


def notify_po_received(po_receipt, user=None, *, is_update=False):
    entry = po_receipt.vehicle_entry
    if entry.entry_type != "RAW_MATERIAL":
        return

    verb = "updated" if is_update else "received"
    _send_to_group_after_commit(
        group_name=RAW_MATERIAL_GROUP,
        title="PO Items Received" if not is_update else "PO Items Updated",
        body=(
            f"PO {po_receipt.po_number} for entry {entry.entry_no} has been {verb}. "
            f"Supplier: {po_receipt.supplier_name}."
        ),
        notification_type=NotificationType.PO_RECEIVED,
        click_action_url=raw_material_entry_url(entry, "step4"),
        company=entry.company,
        reference_type="po_receipt",
        reference_id=po_receipt.id,
        extra_data=_entry_data(
            entry,
            po_receipt_id=po_receipt.id,
            po_number=po_receipt.po_number,
            supplier_code=po_receipt.supplier_code,
            supplier_name=po_receipt.supplier_name,
        ),
        created_by=user or po_receipt.updated_by or po_receipt.created_by,
    )


def notify_gate_entry_completed(entry, user=None):
    if entry.entry_type != "RAW_MATERIAL":
        return

    _send_to_group_after_commit(
        group_name=GRPO_GROUP,
        title="Gate Entry Completed - Ready for GRPO",
        body=f"Raw material entry {entry.entry_no} is completed and ready for GRPO posting.",
        notification_type=NotificationType.GATE_ENTRY_COMPLETED,
        click_action_url=f"/grpo/material/preview/{entry.id}",
        company=entry.company,
        reference_type="vehicle_entry",
        reference_id=entry.id,
        extra_data=_entry_data(entry),
        created_by=user or entry.updated_by or entry.created_by,
    )
