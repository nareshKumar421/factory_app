"""
Which dates a leave actually costs.

A leave from Thursday to Monday is three days off, not five: the plant was shut
on Sunday anyway, and burning somebody's quota for a day they were never going
to work is the kind of quiet unfairness nobody notices until they run out of
leave in November.

Two things take a date out of the count, and they are deliberately different:

**Weekly offs** come from ``settings.ATTENDANCE_WEEKLY_OFF_DAYS`` -- the *same*
setting the attendance roll-up uses to decide ``WEEKLY_OFF`` rather than
``ABSENT``. Reading it from there rather than keeping a second list is the
whole point: if the factory moves its off day, attendance and leave have to
move together or the sheet and the quota stop agreeing.

**Holidays** come from :class:`leave.models.Holiday`, and only the mandatory
ones. A restricted holiday is offered, not taken -- the plant still runs, so
somebody away that day is away.

Both are per company, because the three plants do not shut on the same days.
"""

from datetime import timedelta

from django.conf import settings

from .constants import DayPortion
from .models import Holiday

#: Python's ``date.weekday()``: Monday is 0, Sunday is 6.
DEFAULT_WEEKLY_OFF_DAYS = (6,)


def weekly_off_days():
    """The weekday numbers the plant is closed on.

    Read off the attendance setting so the two modules cannot disagree. Falls
    back to Sunday, which is what that setting itself defaults to.
    """
    configured = getattr(settings, "ATTENDANCE_WEEKLY_OFF_DAYS", None)
    if not configured:
        return set(DEFAULT_WEEKLY_OFF_DAYS)
    return {int(day) for day in configured}


def holiday_dates(company, from_date, to_date, *, include_optional=False):
    """Mandatory holiday dates for one company inside a window, as a set."""
    queryset = Holiday.objects.filter(
        company=company, date__gte=from_date, date__lte=to_date
    )
    if not include_optional:
        queryset = queryset.filter(is_optional=False)
    return set(queryset.values_list("date", flat=True))


def working_dates(company, from_date, to_date):
    """Every date in the span the employee would otherwise have worked.

    Inclusive of both ends. Returns them in order, so the day rows a request
    generates come out sorted without anybody re-sorting them.
    """
    offs = weekly_off_days()
    holidays = holiday_dates(company, from_date, to_date)

    dates = []
    current = from_date
    while current <= to_date:
        if current.weekday() not in offs and current not in holidays:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def day_cost(portion):
    """What one day row costs against a quota, as a Decimal-friendly string."""
    return "0.5" if portion in (DayPortion.FIRST_HALF, DayPortion.SECOND_HALF) else "1.0"
