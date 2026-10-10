"""Who hears about a fixed-asset GRPO: the "Fixed Asset GRPO" auth group.

Sent with ``notifications.services.NotificationService`` directly, after the
transaction commits (the grpo pattern), and with the existing GRPO
notification types, so no notifications migration is needed.

* **Posted** -- sent by the queue handler after a real post or an adopt. Never
  for a simulated post: nothing reached SAP.
* **Failed** -- sent by the post endpoint when SAP (or the app's last check)
  refuses. Waiting for SAP is not a failure and tells nobody.

A known residual: grpo's own ``post_save`` signal also tells the "grpo" group
when a real FA GRPO posts, with a raw-material URL. That signal cannot be
narrowed without editing grpo (see docs/README.md).
"""

from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

from .constants import FA_GRPO_GROUP, FRONTEND_BASE
from .services.state import is_simulated


def _send_after_commit(**kwargs):
    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=FA_GRPO_GROUP, **kwargs
        )
    )


def _po_numbers(posting) -> str:
    numbers = list(posting.po_receipts.values_list("po_number", flat=True))
    if not numbers and posting.po_receipt_id:
        numbers = [posting.po_receipt.po_number]
    return ", ".join(numbers) or "N/A"


def notify_fixed_asset_grpo_posted(posting) -> bool:
    """Tell the group a fixed-asset GRPO reached SAP. Returns False when skipped."""
    if is_simulated(posting) or not posting.sap_doc_entry:
        return False
    entry = posting.vehicle_entry
    po_numbers = _po_numbers(posting)
    _send_after_commit(
        title="Fixed Asset GRPO Posted to SAP",
        body=(
            f"Fixed asset GRPO for entry {entry.entry_no} and PO(s) {po_numbers} was "
            f"posted to SAP. SAP Doc Num: {posting.sap_doc_num}."
        ),
        notification_type=NotificationType.GRPO_POSTED,
        click_action_url=f"{FRONTEND_BASE}/history/{posting.id}",
        company=entry.company,
        reference_type="grpo_posting",
        reference_id=posting.id,
        extra_data={
            "reference_type": "grpo_posting",
            "reference_id": str(posting.id),
            "vehicle_entry_id": str(entry.id),
            "entry_no": entry.entry_no,
            "entry_type": "FIXED_ASSET",
            "po_numbers": po_numbers,
            "sap_doc_entry": str(posting.sap_doc_entry or ""),
            "sap_doc_num": str(posting.sap_doc_num or ""),
            "status": posting.status,
        },
        created_by=posting.posted_by,
    )
    return True


def notify_fixed_asset_grpo_failed(*, company, user, error_message, vehicle_entry_id=None) -> None:
    """Tell the group a fixed-asset GRPO was refused."""
    click_url = (
        f"{FRONTEND_BASE}/preview/{vehicle_entry_id}"
        if vehicle_entry_id
        else f"{FRONTEND_BASE}?tab=history&status=failed"
    )
    _send_after_commit(
        title="Fixed Asset GRPO Posting Failed",
        body=f"Fixed asset GRPO posting failed: {error_message}",
        notification_type=NotificationType.GRPO_FAILED,
        click_action_url=click_url,
        company=company,
        reference_type="vehicle_entry" if vehicle_entry_id else "grpo_posting",
        reference_id=vehicle_entry_id,
        extra_data={
            "reference_type": "vehicle_entry" if vehicle_entry_id else "grpo_posting",
            "reference_id": str(vehicle_entry_id or ""),
            "vehicle_entry_id": str(vehicle_entry_id or ""),
            "entry_type": "FIXED_ASSET",
            "error_message": str(error_message),
            "status": "FAILED",
        },
        created_by=user,
    )
