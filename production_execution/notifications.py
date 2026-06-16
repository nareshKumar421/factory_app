from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

PRODUCTION_GROUP = "production"
PRODUCTION_RUN_URL = "/production/runs"


def notify_production_run_sap_result(run):
    """Notify the production group when a run's SAP goods receipt succeeds or fails."""
    success = run.sap_sync_status == "SUCCESS"
    notification_type = (
        NotificationType.PRODUCTION_RUN_SAP_POSTED
        if success
        else NotificationType.PRODUCTION_RUN_SAP_FAILED
    )
    if success:
        title = "Production Run Posted to SAP"
        body = (
            f"Run #{run.run_number} ({run.product or 'product'}) goods receipt "
            f"posted to SAP (Doc {run.sap_receipt_doc_entry})."
        )
    else:
        title = "Production Run SAP Posting Failed"
        body = (
            f"Run #{run.run_number} ({run.product or 'product'}) failed to post to SAP: "
            f"{run.sap_sync_error or 'unknown error'}."
        )

    extra_data = {
        "reference_type": "production_run",
        "reference_id": str(run.id),
        "run_number": str(run.run_number),
        "sap_sync_status": run.sap_sync_status,
        "product": run.product or "",
    }

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=PRODUCTION_GROUP,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=f"{PRODUCTION_RUN_URL}/{run.id}",
            reference_type="production_run",
            reference_id=run.id,
            company=run.company,
            extra_data=extra_data,
            created_by=run.created_by,
        )
    )
