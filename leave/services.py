"""
The writes, and the invariants each one protects.

Every state change a leave request can undergo lives here, and each one writes
a :class:`~leave.models.LeaveApproval` row as it goes. Nothing in a view is
allowed to move a status by hand -- not because the view could not, but because
the trail is the module's product and an untrailed change is the one nobody can
explain later.

The functions are deliberately small and each raises
:class:`LeaveRefused` rather than returning a flag. A refusal here is a
business rule, and a view turns it into a 400 with the message intact, so the
reason the operator sees is the reason the code had.
"""

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from employee_hierarchy.constants import IN_SERVICE_STATUSES

from .calendar import day_cost, working_dates
from .constants import (
    BLOCKING_DAY_STATUSES,
    MAX_LEAVE_DAYS_PER_REQUEST,
    DayPortion,
    LeaveAction,
    LeaveDayStatus,
    LeaveRequestStatus,
    RecordStatus,
)
from .models import LeaveApproval, LeaveRequest, LeaveRequestDay


class LeaveRefused(ValueError):
    """A request or decision that breaks one of the module's rules."""


def _trail(request, *, action, from_status="", to_status="", comment="", user=None, authority=""):
    """Append one row to the request's trail. The only way trail rows are made."""
    return LeaveApproval.objects.create(
        request=request,
        action=action,
        from_status=from_status or "",
        to_status=to_status or "",
        comment=(comment or "").strip(),
        authority=authority or "",
        performed_by=user if user is not None and user.is_authenticated else None,
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_for_leave(
    *,
    employee,
    leave_type,
    from_date,
    to_date,
    reason,
    portion=DayPortion.FULL,
    contact_number="",
    document=None,
    applied_by=None,
    allow_overdraw=False,
):
    """Raise an application and lay down one row per working day.

    Refuses, in this order -- cheapest and most obviously wrong first, so the
    operator gets the most useful message rather than the first one that fires:

    1. dates out of order, or a span longer than :data:`MAX_LEAVE_DAYS_PER_REQUEST`
    2. an employee who has left, or a leave type that is no longer offered
    3. a half day asked of a type that does not allow one, or of a multi-day span
    4. a span that is entirely weekly offs and holidays -- nothing to apply for
    5. a date the employee already holds
    6. a type that expects paperwork, raised without any
    7. more days than the annual quota leaves

    The overlap check is done here *and* by a database constraint. The check
    gives a decent message; the constraint is what actually holds when two
    requests are submitted at the same instant.

    ``allow_overdraw`` lets the quota be exceeded deliberately. It is the
    caller's business, not this function's, to decide who may do that -- the
    view passes it for somebody holding ``can_decide_any_leave``, because HR
    granting leave beyond an entitlement is a real decision and refusing it
    outright would just push the record out of the system and onto paper.
    """
    if to_date < from_date:
        raise LeaveRefused("The end date cannot be before the start date.")

    span_days = (to_date - from_date).days + 1
    if span_days > MAX_LEAVE_DAYS_PER_REQUEST:
        raise LeaveRefused(
            f"A single application cannot span more than "
            f"{MAX_LEAVE_DAYS_PER_REQUEST} days."
        )

    if employee.employment_status not in IN_SERVICE_STATUSES:
        raise LeaveRefused("This employee is not in service.")

    if leave_type.status != RecordStatus.ACTIVE:
        raise LeaveRefused(f"{leave_type.name} is no longer offered.")

    if leave_type.company_id != employee.company_id:
        raise LeaveRefused("That leave type belongs to another company.")

    if portion != DayPortion.FULL:
        if not leave_type.allow_half_day:
            raise LeaveRefused(f"{leave_type.name} cannot be taken as a half day.")
        if from_date != to_date:
            raise LeaveRefused("A half day can only be applied for on a single date.")

    if leave_type.max_consecutive_days and span_days > leave_type.max_consecutive_days:
        raise LeaveRefused(
            f"{leave_type.name} allows at most "
            f"{leave_type.max_consecutive_days} consecutive day(s)."
        )

    dates = working_dates(employee.company, from_date, to_date)
    if not dates:
        raise LeaveRefused(
            "Every date in that range is a weekly off or a holiday -- "
            "there is nothing to apply for."
        )

    clash = (
        LeaveRequestDay.objects.filter(
            employee=employee, date__in=dates, status__in=list(BLOCKING_DAY_STATUSES)
        )
        .order_by("date")
        .first()
    )
    if clash is not None:
        raise LeaveRefused(f"{clash.date} is already covered by another leave request.")

    if not (reason or "").strip():
        raise LeaveRefused("A reason is required.")

    if leave_type.requires_document and not document:
        raise LeaveRefused(
            f"{leave_type.name} needs supporting paperwork attached."
        )

    # The quota, last: it is the most expensive check (it aggregates the year)
    # and the least useful message to show somebody whose dates were wrong.
    #
    # Charged per calendar year, not per request. A span that crosses New Year
    # spends from both years' entitlements, and checking only the start year
    # would let somebody take next year's leave out of this year's balance.
    if not allow_overdraw and leave_type.annual_quota:
        # Imported here rather than at module scope: `balance` imports the
        # models, which import this module's constants. Keeping it local avoids
        # a circular import for a check that runs once per application.
        from .balance import balance_for

        cost_by_year = defaultdict(Decimal)
        for day in dates:
            shape = portion if from_date == to_date else DayPortion.FULL
            cost_by_year[day.year] += Decimal(day_cost(shape))

        for year, cost in sorted(cost_by_year.items()):
            position = balance_for(employee, leave_type, year)
            remaining = position["available"] - position["pending"]
            if cost > remaining:
                raise LeaveRefused(
                    f"{leave_type.name}: {remaining} day(s) left in {year} "
                    f"({position['used']} used, {position['pending']} awaiting "
                    f"approval) and this asks for {cost}."
                )

    request = LeaveRequest.objects.create(
        company=employee.company,
        employee=employee,
        leave_type=leave_type,
        from_date=from_date,
        to_date=to_date,
        portion=portion,
        reason=reason.strip(),
        contact_number=(contact_number or "").strip(),
        document=document,
        status=LeaveRequestStatus.PENDING,
        applied_by=applied_by if applied_by and applied_by.is_authenticated else None,
        created_by=applied_by if applied_by and applied_by.is_authenticated else None,
    )

    LeaveRequestDay.objects.bulk_create(
        [
            LeaveRequestDay(
                request=request,
                employee=employee,
                date=date,
                # A half day is only ever a single-date request, so the portion
                # applies to the one row that exists.
                portion=portion if request.is_single_day else DayPortion.FULL,
                status=LeaveDayStatus.PENDING,
            )
            for date in dates
        ]
    )

    request.total_days = _sum_days(request)
    request.save(update_fields=["total_days", "updated_at"])

    _trail(
        request,
        action=LeaveAction.APPLIED,
        to_status=LeaveRequestStatus.PENDING,
        comment=request.reason,
        user=applied_by,
    )
    return request


def _sum_days(request, *, statuses=None):
    """Total working days a request costs, honouring half days."""
    days = request.days.all()
    if statuses is not None:
        days = days.filter(status__in=list(statuses))
    return sum((Decimal(day_cost(day.portion)) for day in days), Decimal("0"))


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@transaction.atomic
def approve(request, *, user, comment="", authority="", only_dates=None):
    """Approve a pending request, in whole or in part.

    ``only_dates`` approves those dates and rejects the rest -- a manager who
    can spare Monday and Tuesday but not Wednesday says so once, rather than
    refusing the week and asking for it again.

    Authorisation is **not** decided here. :mod:`leave.routing` decides who may
    act on a request; this function records that they did.
    """
    _require_pending(request)

    days = list(request.days.all())
    if only_dates is not None:
        wanted = set(only_dates)
        unknown = wanted - {day.date for day in days}
        if unknown:
            raise LeaveRefused(
                "Those dates are not part of this request: "
                + ", ".join(str(date) for date in sorted(unknown))
            )
        if not wanted:
            raise LeaveRefused("Approving no dates at all is a rejection -- say so.")
    else:
        wanted = {day.date for day in days}

    for day in days:
        day.status = (
            LeaveDayStatus.APPROVED if day.date in wanted else LeaveDayStatus.REJECTED
        )
    LeaveRequestDay.objects.bulk_update(days, ["status", "updated_at"])

    previous = request.status
    request.status = LeaveRequestStatus.APPROVED
    request.decided_by = user if user and user.is_authenticated else None
    request.decided_at = timezone.now()
    request.decision_note = (comment or "").strip()
    request.total_days = _sum_days(request, statuses=[LeaveDayStatus.APPROVED])
    request.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "total_days",
            "updated_at",
        ]
    )

    _trail(
        request,
        action=LeaveAction.APPROVED,
        from_status=previous,
        to_status=request.status,
        comment=comment,
        user=user,
        authority=authority,
    )
    return request


@transaction.atomic
def reject(request, *, user, comment, authority=""):
    """Refuse a pending request. The comment is mandatory.

    Somebody was told no and will ask why. "No reason given" is not an answer,
    which is the same rule attendance applies to an override.
    """
    _require_pending(request)
    if not (comment or "").strip():
        raise LeaveRefused("A reason is required to reject a leave request.")

    request.days.update(status=LeaveDayStatus.REJECTED)

    previous = request.status
    request.status = LeaveRequestStatus.REJECTED
    request.decided_by = user if user and user.is_authenticated else None
    request.decided_at = timezone.now()
    request.decision_note = comment.strip()
    request.total_days = Decimal("0")
    request.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "total_days",
            "updated_at",
        ]
    )

    _trail(
        request,
        action=LeaveAction.REJECTED,
        from_status=previous,
        to_status=request.status,
        comment=comment,
        user=user,
        authority=authority,
    )
    return request


@transaction.atomic
def withdraw(request, *, user, comment=""):
    """The applicant taking it back before anybody decided.

    Distinct from cancelling: nothing has been approved, so nothing has reached
    the attendance sheet and there is nothing to unpick.
    """
    _require_pending(request)

    request.days.update(status=LeaveDayStatus.CANCELLED)

    previous = request.status
    request.status = LeaveRequestStatus.WITHDRAWN
    request.total_days = Decimal("0")
    request.save(update_fields=["status", "total_days", "updated_at"])

    _trail(
        request,
        action=LeaveAction.WITHDRAWN,
        from_status=previous,
        to_status=request.status,
        comment=comment,
        user=user,
    )
    return request


@transaction.atomic
def cancel(request, *, user, comment, authority=""):
    """Take back an approval that has already been given.

    The only transition that may have to undo something outside this module: an
    approved day may already be on the attendance sheet. The projection is
    reversed by the caller (see :mod:`leave.projection`) *after* this returns,
    so a failure to reach attendance cannot leave the request half-cancelled.
    """
    if request.status != LeaveRequestStatus.APPROVED:
        raise LeaveRefused("Only an approved request can be cancelled.")
    if not (comment or "").strip():
        raise LeaveRefused("A reason is required to cancel an approved leave.")

    # The days have to come back with the request, not just the request. They
    # are what ``BLOCKING_DAY_STATUSES`` and the ``uniq_live_leave_day_per_
    # employee`` constraint read, so an APPROVED row left behind on a cancelled
    # request goes on occupying its date: the employee could never book it
    # again, and the refusal would name a request that no longer exists. Only
    # the approved ones -- a partly approved request carries REJECTED days, and
    # those keep their own record of having been refused.
    request.days.filter(status=LeaveDayStatus.APPROVED).update(
        status=LeaveDayStatus.CANCELLED
    )

    previous = request.status
    request.status = LeaveRequestStatus.CANCELLED
    request.decision_note = comment.strip()
    request.save(update_fields=["status", "decision_note", "updated_at"])

    _trail(
        request,
        action=LeaveAction.CANCELLED,
        from_status=previous,
        to_status=request.status,
        comment=comment,
        user=user,
        authority=authority,
    )
    return request


def _require_pending(request):
    if request.status != LeaveRequestStatus.PENDING:
        raise LeaveRefused(
            f"This request is already {request.get_status_display().lower()}."
        )
