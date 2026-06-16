from django.db import transaction

from notifications.models import NotificationType
from notifications.services import NotificationService

DISPATCH_GROUP = "dispatch"
DISPATCH_PLAN_URL = "/dispatch/plans"


def notify_dispatch_plan_status(plan):
    """Notify the dispatch group when a plan is booked or dispatched."""
    if plan.booking_status == "BOOKED":
        notification_type = NotificationType.DISPATCH_PLAN_BOOKED
        title = "Dispatch Plan Booked"
        body = (
            f"Dispatch plan #{plan.id} (invoice {plan.sap_invoice_doc_num or plan.invoice_number}) "
            f"has been booked."
        )
    elif plan.booking_status == "DISPATCHED":
        notification_type = NotificationType.DISPATCH_PLAN_DISPATCHED
        title = "Dispatch Plan Dispatched"
        body = (
            f"Dispatch plan #{plan.id} (invoice {plan.sap_invoice_doc_num or plan.invoice_number}) "
            f"has been dispatched."
        )
    else:
        return

    extra_data = {
        "reference_type": "dispatch_plan",
        "reference_id": str(plan.id),
        "booking_status": plan.booking_status,
        "invoice_number": plan.invoice_number or plan.sap_invoice_doc_num or "",
        "vehicle_no": plan.vehicle_no or "",
    }

    created_by = getattr(plan, "updated_by", None) or getattr(plan, "created_by", None)

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_group(
            group_name=DISPATCH_GROUP,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=f"{DISPATCH_PLAN_URL}/{plan.id}",
            reference_type="dispatch_plan",
            reference_id=plan.id,
            company=plan.company,
            extra_data=extra_data,
            created_by=created_by,
        )
    )
