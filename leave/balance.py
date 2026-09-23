"""
How much leave somebody has left.

**Computed, never stored.** A balance is entitlement minus what has been taken,
and both halves already exist: ``LeaveType.annual_quota`` and the approved day
rows. A stored counter would be a third copy that has to be kept in step with
every approval, cancellation and partial decision -- and the day it drifts,
nobody can tell which of the two numbers is wrong. Summing the days is a single
indexed aggregate over one person's year; there is no performance reason to
cache it and a very good correctness reason not to.

**Pending days are counted separately, not deducted.** Somebody with two days
left and two days awaiting approval has not spent them yet, but they cannot
spend them twice either. Showing ``available`` alongside ``pending`` lets the
screen say "2 left, 2 awaiting approval" -- which is the true position -- rather
than picking one of the two lies.

**A quota of 0 means untracked, not none.** Unpaid leave has no ceiling worth
storing. Those types report ``quota = None`` and no ``available`` figure, and
nothing refuses an application on their account.
"""

from decimal import Decimal

from .calendar import day_cost
from .constants import LeaveDayStatus, LeaveRequestStatus, RecordStatus
from .models import LeaveRequestDay, LeaveType


def _sum_days(employee, leave_type, year, statuses, request_statuses):
    days = LeaveRequestDay.objects.filter(
        employee=employee,
        request__leave_type=leave_type,
        request__status__in=list(request_statuses),
        status__in=list(statuses),
        date__year=year,
    ).values_list("portion", flat=True)
    return sum((Decimal(day_cost(portion)) for portion in days), Decimal("0"))


def balance_for(employee, leave_type, year):
    """One employee's position on one leave type, for one calendar year."""
    used = _sum_days(
        employee,
        leave_type,
        year,
        statuses=[LeaveDayStatus.APPROVED],
        request_statuses=[LeaveRequestStatus.APPROVED],
    )
    pending = _sum_days(
        employee,
        leave_type,
        year,
        statuses=[LeaveDayStatus.PENDING],
        request_statuses=[LeaveRequestStatus.PENDING],
    )

    tracked = bool(leave_type.annual_quota)
    quota = Decimal(leave_type.annual_quota) if tracked else None
    # Never report a negative entitlement: an over-grant is a decision somebody
    # made deliberately, and showing "-1 left" reads as a bug rather than as the
    # exception it is. `used` alongside `quota` still shows the overshoot.
    available = max(quota - used, Decimal("0")) if tracked else None

    return {
        "leave_type": leave_type.pk,
        "leave_type_code": leave_type.code,
        "leave_type_name": leave_type.name,
        "is_paid": leave_type.is_paid,
        "tracked": tracked,
        "quota": quota,
        "used": used,
        "pending": pending,
        "available": available,
    }


def balances_for(employee, year):
    """Every active leave type this employee's company offers, with their position."""
    types = LeaveType.objects.filter(
        company=employee.company, status=RecordStatus.ACTIVE
    ).order_by("sort_order", "name")
    return [balance_for(employee, leave_type, year) for leave_type in types]
