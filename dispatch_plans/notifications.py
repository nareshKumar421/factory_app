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


WAREHOUSE_GROUP = "warehouse"
BILL_SUMMARY_URL = "/warehouse/bill-summaries"


def notify_bill_summary_submitted(summaries):
    """Dispatch has sent sheets across; tell the godowns' managers they are waiting.

    Only the warehouse users who manage a godown on the sheets, and each of them
    about their own sheets only — the same rule as the approvals screen
    (`BillSummaryService.manages`), so nobody is told about a sheet that screen
    will not show them. One notification per person for the batch, not one per
    bill: a truck is submitted in a single action and approved in a single
    action, so eight pushes would be eight ways of saying the same thing.

    Called from inside an ``on_commit`` callback, so it sends straight away
    rather than deferring again — by the time it runs the sheets are committed.
    """
    # Imported here: the service imports this module.
    from .bill_summary_service import BillSummaryService

    summaries = [summary for summary in summaries if summary]
    if not summaries:
        return

    company = summaries[0].company
    for user in _warehouse_desk(company):
        theirs = BillSummaryService(company.code, user).managed_only(summaries)
        if theirs:
            _send_submitted(user, theirs)


def _warehouse_desk(company):
    """Active members of the warehouse group with access to this company."""
    from django.contrib.auth import get_user_model

    from company.models import UserCompany

    return (
        get_user_model()
        .objects.filter(
            groups__name=WAREHOUSE_GROUP,
            is_active=True,
            id__in=UserCompany.objects.filter(
                company=company, is_active=True
            ).values("user_id"),
        )
        .distinct()
    )


def _send_submitted(user, summaries):
    first = summaries[0]
    vehicles = sorted({(s.vehicle_no or "").strip() for s in summaries} - {""})
    truck = f" on {', '.join(vehicles)}" if vehicles else ""
    count = len(summaries)
    body = (
        f"{count} bill summar{'y' if count == 1 else 'ies'}{truck} "
        f"{'is' if count == 1 else 'are'} waiting for a dispatch date."
    )

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
