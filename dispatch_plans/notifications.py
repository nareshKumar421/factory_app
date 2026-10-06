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
            f"Dispatch plan #{plan.id} (invoice {plan.sap_invoice_doc_num}) "
            f"has been booked."
        )
    elif plan.booking_status == "DISPATCHED":
        notification_type = NotificationType.DISPATCH_PLAN_DISPATCHED
        title = "Dispatch Plan Dispatched"
        body = (
            f"Dispatch plan #{plan.id} (invoice {plan.sap_invoice_doc_num}) "
            f"has been dispatched."
        )
    else:
        return

    extra_data = {
        "reference_type": "dispatch_plan",
        "reference_id": str(plan.id),
        "booking_status": plan.booking_status,
        "invoice_number": plan.sap_invoice_doc_num or "",
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


BILL_SUMMARY_URL = "/warehouse/bill-summaries"


def _approvers(company):
    """Who can approve this company's sheets: the users given the right.

    The right itself, given through a group or directly, rather than a group
    name. The "warehouse" group this push used to go to does not exist on live,
    so every "awaiting approval" push went to nobody. Superusers are left out:
    they hold every right without being given it, and none of them is the desk
    that approves.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.db.models import Q

    from company.models import UserCompany

    right = Permission.objects.filter(
        content_type__app_label="dispatch_plans", codename="can_approve_bill_summary"
    ).first()
    if right is None:
        return []
    return list(
        get_user_model()
        .objects.filter(
            Q(groups__permissions=right) | Q(user_permissions=right),
            is_active=True,
            id__in=UserCompany.objects.filter(
                company=company, is_active=True
            ).values("user_id"),
        )
        .distinct()
    )


def notify_bill_summary_submitted(summaries):
    """Dispatch has sent sheets across; tell whoever approves them they are waiting.

    One notification for the batch, not one per bill. A truck is submitted in a
    single action and approved in a single action, so eight pushes would be eight
    ways of saying the same thing to the same person.

    Called from inside an ``on_commit`` callback, so it sends straight away
    rather than deferring again — by the time it runs the sheets are committed.
    """
    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return

    first = summaries[0]
    vehicles = sorted({(s.vehicle_no or "").strip() for s in summaries} - {""})
    truck = f" on {', '.join(vehicles)}" if vehicles else ""
    count = len(summaries)
    body = (
        f"{count} bill summar{'y' if count == 1 else 'ies'}{truck} "
        f"{'is' if count == 1 else 'are'} waiting for a dispatch date."
    )

    for user in _approvers(first.company):
        NotificationService.send_notification_to_user(
            user=user,
            title="Bill Summary Awaiting Approval",
            body=body,
            notification_type=NotificationType.BILL_SUMMARY_SUBMITTED,
            click_action_url=BILL_SUMMARY_URL,
            reference_type="bill_summary",
            reference_id=first.id,
            company=first.company,
            extra_data={
                "reference_type": "bill_summary",
                "reference_id": str(first.id),
                "count": str(count),
                "vehicle_no": ", ".join(vehicles),
                "entry_nos": ", ".join(summary.entry_no for summary in summaries),
            },
            created_by=first.issued_by,
        )


def notify_bill_summary_decided(summaries, *, approved: bool):
    """The warehouse has decided; tell the dispatch desk which way.

    A sending-back names the reason in the body. It is the whole content of the
    message — a dispatch clerk told only "sent back" has to open the sheet to
    learn what for, and will not until the truck is already late.
    """
    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return

    first = summaries[0]
    count = len(summaries)
    noun = "bill summary" if count == 1 else f"{count} bill summaries"
    if approved:
        dates = sorted({str(s.dispatch_date) for s in summaries if s.dispatch_date})
        title = "Bill Summary Approved"
        body = f"{noun} approved for dispatch on {', '.join(dates) or 'a set date'}."
        decided_by = first.approved_by
    else:
        title = "Bill Summary Sent Back"
        body = f"{first.entry_no} was sent back: {first.reject_reason}"
        decided_by = first.rejected_by

    NotificationService.send_notification_by_auth_group(
        group_name=DISPATCH_GROUP,
        title=title,
        body=body,
        notification_type=NotificationType.BILL_SUMMARY_DECIDED,
        click_action_url=f"{BILL_SUMMARY_URL}/{first.id}",
        reference_type="bill_summary",
        reference_id=first.id,
        company=first.company,
        extra_data={
            "reference_type": "bill_summary",
            "reference_id": str(first.id),
            "approved": str(approved).lower(),
            "count": str(count),
            "entry_nos": ", ".join(summary.entry_no for summary in summaries),
        },
        created_by=decided_by,
    )
