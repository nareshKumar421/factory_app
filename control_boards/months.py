"""
The month a monthly control board reads, from its ``?month=YYYY-MM``.

Every monthly board service here already takes a ``today`` -- the date it is
read as of -- and builds its month from it: the month's first day to ``today``.
So stepping a board back a month is choosing that date: the last day of the
ended month, which makes the whole month the window and leaves the service
untouched. The current month, or no month at all, is ``None``: the service's
own "now", exactly as before.
"""

import calendar
import re
from datetime import date
from typing import Optional

from django.utils import timezone

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def as_of_for_month(raw: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """The date a board is read as of, for the month it was asked for.

    ``None`` for no month, or the current one -- the board reads now. The last
    day of the month for one that has ended. Raises ``ValueError`` for a value
    that is not ``YYYY-MM``, or a month that has not started: there is nothing
    to report from it, and an empty board reads as a broken one.
    """
    value = (raw or "").strip()
    if not value:
        return None

    match = _MONTH.match(value)
    if not match:
        raise ValueError("month must be YYYY-MM, e.g. 2026-09.")

    today = today or timezone.localdate()
    year, month = int(match.group(1)), int(match.group(2))
    if (year, month) > (today.year, today.month):
        raise ValueError("That month has not started yet.")
    if (year, month) == (today.year, today.month):
        return None
    return date(year, month, calendar.monthrange(year, month)[1])
