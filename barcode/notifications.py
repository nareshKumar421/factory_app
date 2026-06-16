from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

BARCODE_GROUP = "barcode"
TRANSFER_URL = "/barcode/intercompany-transfers"


def _transfer_extra(transfer):
    return {
        "reference_type": "intercompany_transfer",
        "reference_id": str(transfer.id),
        "transfer_number": transfer.transfer_number,
        "status": transfer.status,
        "total_barcodes": str(transfer.total_barcodes),
        "sap_doc_num": transfer.sap_doc_num or "",
    }


def notify_intercompany_transfer_completed(transfer):
    """Notify the barcode group when an intercompany transfer completes."""
    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=BARCODE_GROUP,
            title="Intercompany Transfer Completed",
            body=(
                f"Transfer {transfer.transfer_number} "
                f"({transfer.source_company.code} -> {transfer.destination_company.code}) "
                f"completed with {transfer.total_barcodes} barcode(s)."
            ),
            notification_type=NotificationType.INTERCOMPANY_TRANSFER_COMPLETED,
            click_action_url=TRANSFER_URL,
            reference_type="intercompany_transfer",
            reference_id=transfer.id,
            company=transfer.source_company,
            extra_data=_transfer_extra(transfer),
            created_by=transfer.created_by,
        )
    )


def notify_intercompany_transfer_failed(transfer):
    """Notify the barcode group when an intercompany transfer fails to sync to SAP."""
    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=BARCODE_GROUP,
            title="Intercompany Transfer SAP Sync Failed",
            body=(
                f"Transfer {transfer.transfer_number} failed to sync to SAP: "
                f"{transfer.sap_error or 'unknown error'}."
            ),
            notification_type=NotificationType.INTERCOMPANY_TRANSFER_FAILED,
            click_action_url=TRANSFER_URL,
            reference_type="intercompany_transfer",
            reference_id=transfer.id,
            company=transfer.source_company,
            extra_data=_transfer_extra(transfer),
            created_by=transfer.created_by,
        )
    )
