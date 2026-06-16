from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

WAREHOUSE_GROUP = "warehouse"
BOM_REQUESTS_URL = "/warehouse/bom-requests"
FG_RECEIPTS_URL = "/warehouse/fg-receipts"


def notify_bom_request_created(bom_request):
    """Production submitted a BOM request -> notify the warehouse group."""
    company = bom_request.company
    extra_data = {
        "reference_type": "bom_request",
        "reference_id": str(bom_request.id),
        "production_run_id": str(bom_request.production_run_id),
        "required_qty": str(bom_request.required_qty),
        "status": bom_request.status,
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=WAREHOUSE_GROUP,
            title="New BOM Request for Approval",
            body=(
                f"Production run #{bom_request.production_run_id} requested materials "
                f"(qty {bom_request.required_qty}). Awaiting warehouse approval."
            ),
            notification_type=NotificationType.BOM_REQUEST_CREATED,
            click_action_url=BOM_REQUESTS_URL,
            reference_type="bom_request",
            reference_id=bom_request.id,
            company=company,
            extra_data=extra_data,
            created_by=bom_request.requested_by,
        )
    )


def notify_bom_request_reviewed(bom_request):
    """Warehouse approved/rejected a BOM request -> notify the requester."""
    requester = bom_request.requested_by
    if requester is None:
        return

    decision = bom_request.get_status_display()
    extra_data = {
        "reference_type": "bom_request",
        "reference_id": str(bom_request.id),
        "production_run_id": str(bom_request.production_run_id),
        "status": bom_request.status,
        "rejection_reason": bom_request.rejection_reason or "",
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_to_user(
            user=requester,
            title=f"BOM Request {decision}",
            body=(
                f"Your BOM request for production run #{bom_request.production_run_id} "
                f"was {decision.lower()} by the warehouse."
            ),
            notification_type=NotificationType.BOM_REQUEST_REVIEWED,
            click_action_url=BOM_REQUESTS_URL,
            reference_type="bom_request",
            reference_id=bom_request.id,
            company=bom_request.company,
            extra_data=extra_data,
            created_by=bom_request.reviewed_by,
        )
    )


def notify_fg_receipt_result(fg_receipt):
    """Notify the warehouse group when an FG receipt is posted to SAP or fails."""
    posted = fg_receipt.status == "SAP_POSTED"
    notification_type = (
        NotificationType.FG_RECEIPT_POSTED if posted else NotificationType.FG_RECEIPT_FAILED
    )
    if posted:
        title = "Finished Goods Receipt Posted"
        body = (
            f"FG receipt for {fg_receipt.item_code or 'item'} "
            f"(good qty {fg_receipt.good_qty}) was posted to SAP "
            f"(Doc {fg_receipt.sap_receipt_doc_entry})."
        )
    else:
        title = "Finished Goods Receipt Failed"
        body = (
            f"FG receipt for {fg_receipt.item_code or 'item'} failed to post to SAP: "
            f"{fg_receipt.sap_error or 'unknown error'}."
        )

    extra_data = {
        "reference_type": "fg_receipt",
        "reference_id": str(fg_receipt.id),
        "production_run_id": str(fg_receipt.production_run_id),
        "status": fg_receipt.status,
        "item_code": fg_receipt.item_code or "",
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=WAREHOUSE_GROUP,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=FG_RECEIPTS_URL,
            reference_type="fg_receipt",
            reference_id=fg_receipt.id,
            company=fg_receipt.company,
            extra_data=extra_data,
            created_by=fg_receipt.received_by,
        )
    )
