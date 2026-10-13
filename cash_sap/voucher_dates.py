"""The voucher's dates: what is refused, and what is only worth a second look.

The spec put this in ``cash_book``; on this branch ``cash_book`` is not edited,
so the rules live here and are shown only on the cash -> SAP screens. The cash
book's own validation is unchanged.

Pure functions, no database. ``today`` defaults to ``timezone.localdate()``,
which is Asia/Kolkata (``TIME_ZONE``) -- never the UTC date, which is
yesterday's between midnight and 05:30 IST.

REFUSED (``check_voucher_dates``)
    A voucher or bill dated in the future. SAP would take it into a period
    nobody has opened, and the posting month (Dim2) would be wrong.

WARNED (``voucher_date_warnings``), each ``{"code", "field", "message"}``
    ``DATE_OLD``          more than 45 days ago (only with ``include_age``; the
                          bunch plan turns it off because the backlog is old by
                          design);
    ``BILL_AFTER_ENTRY``  the bill is dated after the voucher;
    ``BILL_OTHER_MONTH``  the bill and the voucher are in different months;
    ``LOOKS_SWAPPED``     day and month look transposed -- the old sheet mixed
                          dd/mm and mm/dd (``cash_book.sheet_import``).
"""

from datetime import date, timedelta
from typing import List, Optional

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .constants import VOUCHER_AGE_WARNING_DAYS

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt(day: date) -> str:
    """``03 Sep 2026``, independent of the server's locale."""
    return f"{day.day:02d} {MONTHS[day.month - 1]} {day.year}"


def _today(today: Optional[date]) -> date:
    return today or timezone.localdate()


def check_voucher_dates(entry_date: date, bill_date: Optional[date] = None, *,
                        today: Optional[date] = None) -> None:
    """Refuse a future voucher or bill date, with the field it belongs to."""
    now = _today(today)
    if entry_date and entry_date > now:
        raise ValidationError({"entry_date": "The voucher date cannot be in the future."})
    if bill_date and bill_date > now:
        raise ValidationError({"bill_date": "The bill date cannot be in the future."})


def _swapped(day: date) -> Optional[date]:
    if day.day > 12 or day.day == day.month:
        return None
    try:
        return date(day.year, day.day, day.month)
    except ValueError:
        return None


def _looks_swapped(day: date, now: date) -> Optional[date]:
    """The transposed date when it is a far likelier reading than ``day``."""
    other = _swapped(day)
    if other is None:
        return None
    floor = now - timedelta(days=VOUCHER_AGE_WARNING_DAYS)
    if not (floor <= other <= now):
        return None
    far = (now - day).days > VOUCHER_AGE_WARNING_DAYS
    other_month = (day.year, day.month) != (now.year, now.month)
    return other if (far or other_month) else None


def voucher_date_warnings(entry_date: date, bill_date: Optional[date] = None, *,
                          today: Optional[date] = None,
                          include_age: bool = True) -> List[dict]:
    """Everything about the two dates worth a second look; ``[]`` when clean."""
    now = _today(today)
    warnings: List[dict] = []
    dates = [("entry_date", "Voucher", entry_date)]
    if bill_date:
        dates.append(("bill_date", "Bill", bill_date))

    if include_age:
        for field, noun, day in dates:
            if day and (now - day).days > VOUCHER_AGE_WARNING_DAYS:
                warnings.append({
                    "code": "DATE_OLD",
                    "field": field,
                    "message": f"{noun} date {fmt(day)} is more than "
                               f"{VOUCHER_AGE_WARNING_DAYS} days ago — check it.",
                })

    if bill_date and entry_date:
        if bill_date > entry_date:
            warnings.append({
                "code": "BILL_AFTER_ENTRY",
                "field": "bill_date",
                "message": f"The bill is dated {fmt(bill_date)} — after the voucher "
                           f"date {fmt(entry_date)}.",
            })
        if (bill_date.year, bill_date.month) != (entry_date.year, entry_date.month):
            warnings.append({
                "code": "BILL_OTHER_MONTH",
                "field": "bill_date",
                "message": f"The bill date {fmt(bill_date)} is in a different month "
                           f"from the voucher date {fmt(entry_date)}.",
            })

    for field, _noun, day in dates:
        if not day:
            continue
        other = _looks_swapped(day, now)
        if other is not None:
            warnings.append({
                "code": "LOOKS_SWAPPED",
                "field": field,
                "message": f"{fmt(day)} may have day and month swapped — did you "
                           f"mean {fmt(other)}?",
            })
    return warnings
