from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

GRPO_GROUP = "grpo"


def _send_grpo_group_after_commit(
    *,
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
            group_name=GRPO_GROUP,
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


def _material_po_numbers(posting):
    po_numbers = list(posting.po_receipts.values_list("po_number", flat=True))
    if not po_numbers and posting.po_receipt_id:
        po_numbers = [posting.po_receipt.po_number]
    return ", ".join(po_numbers) or "N/A"


def notify_material_grpo_posted(posting):
    entry = posting.vehicle_entry
    po_numbers = _material_po_numbers(posting)
    _send_grpo_group_after_commit(
        title="GRPO Posted to SAP",
        body=(
            f"GRPO for entry {entry.entry_no} and PO(s) {po_numbers} was posted to SAP. "
            f"SAP Doc Num: {posting.sap_doc_num}."
        ),
        notification_type=NotificationType.GRPO_POSTED,
        click_action_url=f"/grpo/material/history/{posting.id}",
        company=entry.company,
        reference_type="grpo_posting",
        reference_id=posting.id,
        extra_data={
            "reference_type": "grpo_posting",
            "reference_id": str(posting.id),
            "vehicle_entry_id": str(entry.id),
            "entry_no": entry.entry_no,
            "po_numbers": po_numbers,
            "sap_doc_entry": str(posting.sap_doc_entry or ""),
            "sap_doc_num": str(posting.sap_doc_num or ""),
            "status": posting.status,
        },
        created_by=posting.posted_by,
    )


def notify_material_grpo_failed(*, company, user, error_message, vehicle_entry_id=None):
    click_url = (
        f"/grpo/material/preview/{vehicle_entry_id}"
        if vehicle_entry_id
        else "/grpo/material/history?status=failed"
    )
    _send_grpo_group_after_commit(
        title="GRPO Posting Failed",
        body=f"Material GRPO posting failed: {error_message}",
        notification_type=NotificationType.GRPO_FAILED,
        click_action_url=click_url,
        company=company,
        reference_type="vehicle_entry" if vehicle_entry_id else "grpo_posting",
        reference_id=vehicle_entry_id,
        extra_data={
            "reference_type": "vehicle_entry" if vehicle_entry_id else "grpo_posting",
            "reference_id": str(vehicle_entry_id or ""),
            "vehicle_entry_id": str(vehicle_entry_id or ""),
            "error_message": str(error_message),
            "status": "FAILED",
        },
        created_by=user,
    )


def notify_service_grpo_posted(posting):
    plan = posting.dispatch_plan
    _send_grpo_group_after_commit(
        title="Service GRPO Posted to SAP",
        body=(
            f"Service GRPO for dispatch plan {plan.id} was posted to SAP. "
            f"SAP Doc Num: {posting.sap_doc_num}."
        ),
        notification_type=NotificationType.SERVICE_GRPO_POSTED,
        click_action_url=f"/dispatch/bilty-grpo/history/{posting.id}",
        company=plan.company,
        reference_type="service_grpo_posting",
        reference_id=posting.id,
        extra_data={
            "reference_type": "service_grpo_posting",
            "reference_id": str(posting.id),
            "dispatch_plan_id": str(plan.id),
            "sap_doc_entry": str(posting.sap_doc_entry or ""),
            "sap_doc_num": str(posting.sap_doc_num or ""),
            "status": posting.status,
        },
        created_by=posting.posted_by,
    )


def notify_service_grpo_failed(*, company, user, error_message, dispatch_plan_id=None):
    click_url = (
        f"/dispatch/bilty-grpo/preview/{dispatch_plan_id}"
        if dispatch_plan_id
        else "/dispatch/bilty-grpo/history?status=failed"
    )
    _send_grpo_group_after_commit(
        title="Service GRPO Posting Failed",
        body=f"Service GRPO posting failed: {error_message}",
        notification_type=NotificationType.SERVICE_GRPO_FAILED,
        click_action_url=click_url,
        company=company,
        reference_type="dispatch_plan" if dispatch_plan_id else "service_grpo_posting",
        reference_id=dispatch_plan_id,
        extra_data={
            "reference_type": "dispatch_plan" if dispatch_plan_id else "service_grpo_posting",
            "reference_id": str(dispatch_plan_id or ""),
            "dispatch_plan_id": str(dispatch_plan_id or ""),
            "error_message": str(error_message),
            "status": "FAILED",
        },
        created_by=user,
    )
