"""
The module's vocabulary, in one place.

Kept apart from :mod:`leave.models` for the same reason
:mod:`employee_hierarchy.constants` is: the status sets below are consulted by
the routing rules, the attendance projection and the serializers, and a set
that means "still live" has to have exactly one definition. Three files each
listing "PENDING or APPROVED" is three chances for them to drift, and the one
that drifts is the one deciding whether a day is double-booked.
"""

from django.db import models

#: How many days one application may span. Not a policy about entitlement --
#: that is :attr:`LeaveType.annual_quota` -- but a bound on the day rows a
#: single request can generate, so a typo of 2027 instead of 2026 in the end
#: date is refused instead of writing four hundred rows.
MAX_LEAVE_DAYS_PER_REQUEST = 90

#: Half a day, as a number. Leave is counted in days and the only fraction the
#: business uses is a half, so a Decimal with one place is the whole story.
HALF_DAY = "0.5"
FULL_DAY = "1.0"


class RecordStatus(models.TextChoices):
    """Whether a master row is still offered when raising a request."""

    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"


class LeaveRequestStatus(models.TextChoices):
    """Where an application is.

    ``WITHDRAWN`` and ``CANCELLED`` are different events and are deliberately
    not merged. Withdrawn is the applicant changing their mind *before* anyone
    decided; cancelled is an approved leave being taken back *after*, which is
    the one that has to undo an attendance projection. Collapsing them would
    make "did this ever reach the sheet?" unanswerable from the status alone.
    """

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    WITHDRAWN = "WITHDRAWN", "Withdrawn by the applicant"
    CANCELLED = "CANCELLED", "Cancelled after approval"


class LeaveDayStatus(models.TextChoices):
    """One day of an application.

    Days carry their own status because a five-day request may come back three
    days approved and two rejected -- a manager who can only spare part of the
    week should not have to refuse the whole thing.
    """

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class DayPortion(models.TextChoices):
    """How much of one day is being taken.

    Maps onto what attendance can already express: a full day becomes
    ``ON_LEAVE``, a half stays ``HALF_DAY`` on the sheet, because the person
    was genuinely at the gate for half the day and the machine will say so.
    """

    FULL = "FULL", "Full day"
    FIRST_HALF = "FIRST_HALF", "First half"
    SECOND_HALF = "SECOND_HALF", "Second half"


class LeaveAction(models.TextChoices):
    """What happened to an application, for the append-only trail."""

    APPLIED = "APPLIED", "Applied"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"
    CANCELLED = "CANCELLED", "Cancelled"
    PROJECTED = "PROJECTED", "Written to the attendance sheet"
    UNPROJECTED = "UNPROJECTED", "Removed from the attendance sheet"


#: Statuses where the request still occupies its dates. A day held by one of
#: these blocks another application for the same date -- the single definition
#: of "already booked", used by the overlap check and the database constraint
#: alike.
BLOCKING_DAY_STATUSES = frozenset({LeaveDayStatus.PENDING, LeaveDayStatus.APPROVED})

#: Request statuses a decision may still act on.
DECIDABLE_STATUSES = frozenset({LeaveRequestStatus.PENDING})

#: Request statuses that are over and done with; nothing may change them.
TERMINAL_STATUSES = frozenset(
    {
        LeaveRequestStatus.REJECTED,
        LeaveRequestStatus.WITHDRAWN,
        LeaveRequestStatus.CANCELLED,
    }
)

#: Statuses whose days should appear on the attendance sheet.
PROJECTABLE_STATUSES = frozenset({LeaveRequestStatus.APPROVED})
