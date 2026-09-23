"""
Leave: who asked to be away, who decided, and what the sheet was told.

Six tables in three groups.

**The masters**

* :class:`LeaveType` -- casual, sick, earned, unpaid. Per company, because the
  three plants do not have to offer the same kinds.
* :class:`Holiday`  -- the factory's closed days. New here: attendance already
  had a ``HOLIDAY`` status but nothing to drive it from, so a leave spanning
  Republic Day would have silently eaten a day of somebody's quota.

**The application**

* :class:`LeaveRequest`    -- one person asking for one span of dates.
* :class:`LeaveRequestDay` -- one row per date in that span. This is the table
  that matters; see below.
* :class:`LeaveApproval`   -- append-only: every decision ever made about a
  request, who made it and why.

**The rights**

* :class:`LeavePermission` -- the sentinel carrying the module's grants.

Three design decisions are worth knowing before reading further.

**Why a row per day, and not just a date range.** Attendance is one row per
employee per day, so a range would have to be expanded on every read to join
against it. A day row also lets a manager approve part of a week, lets one day
be cancelled without unpicking the rest, and gives the attendance projection
something to mark as done -- ``is_projected`` is per day because the sync
reaches days one at a time, and a request spanning a weekend is projected on
Monday for its Friday and on Tuesday for its Monday.

**Why the employee is denormalised onto the day row.** It is already on the
request, so this is duplication -- accepted for one reason: "is this person
already booked on this date?" is the question the module exists to get right,
and with the employee on the row it is a database constraint rather than a
rule some view has to remember to apply. A uniqueness constraint cannot reach
through a foreign key.

**Why approval never touches the attendance row directly.** The machine's
reading is immutable -- that is attendance's whole design, and payroll disputes
turn on it. An approved leave is projected by calling
:func:`attendance.services.override_status`, the same door a human correction
goes through, so the projection inherits the mandatory reason and the
append-only override trail rather than inventing a second way to write that
column. See :mod:`leave.projection`.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from company.models import Company

from .constants import (
    BLOCKING_DAY_STATUSES,
    DayPortion,
    LeaveAction,
    LeaveDayStatus,
    LeaveRequestStatus,
    RecordStatus,
)

User = settings.AUTH_USER_MODEL


class Stamped(models.Model):
    """Created / updated columns, without an ``is_active`` flag.

    Same reasoning as :class:`employee_hierarchy.models.Stamped`: every model
    here already has a domain status, and a second boolean beside it would be a
    competing answer to "is this live?".
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_created",
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_updated",
    )

    class Meta:
        abstract = True


class LeavePermission(models.Model):
    """Sentinel carrying the module's rights (no table of its own).

    The split that matters is between **applying**, **deciding for your own
    team**, and **deciding for anybody**. They are three audiences, not three
    rungs of one ladder:

    * Everybody with a linked employee record may apply.
    * ``can_apply_leave_for_others`` is the time office. It exists because only
      about half the workforce has a login at all -- the shop floor does not --
      and without it those people could never be recorded as on leave.
    * ``can_decide_leave`` is a manager, and is scoped by the reporting tree:
      holding it lets you decide for your own subtree and nobody else's. The
      permission alone is not the authorisation; see :mod:`leave.routing`.
    * ``can_decide_any_leave`` is HR, and is the only grant that reaches
      outside a tree.

    ``can_cancel_approved_leave`` is separate from deciding because taking back
    an approval after the fact also has to unpick an attendance projection, on
    a day that may already have closed.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Leave"
        verbose_name_plural = "Leave"
        permissions = [
            ("can_apply_leave", "Can apply for their own leave"),
            ("can_apply_leave_for_others", "Can raise a leave application for somebody else"),
            ("can_view_team_leave", "Can view the leave of everyone below them"),
            ("can_decide_leave", "Can approve or reject leave for their own team"),
            ("can_decide_any_leave", "Can approve or reject leave for anybody"),
            ("can_cancel_approved_leave", "Can cancel an already approved leave"),
            ("can_manage_leave_types", "Can maintain leave types and the holiday calendar"),
        ]


class LeaveType(Stamped):
    """A kind of leave a company offers.

    ``annual_quota`` of 0 means *untracked*, not *none* -- unpaid leave has no
    ceiling worth storing, and a company that does not run a balance yet should
    not have every application refused for exceeding a quota of zero. Balances
    are Phase 5; until then this column is documentation.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="leave_types")
    code = models.CharField(max_length=30, help_text="Short handle, e.g. 'CL'.")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")

    is_paid = models.BooleanField(
        default=True,
        help_text="Whether a day of this counts as paid. Read by payroll, not by this module.",
    )
    allow_half_day = models.BooleanField(default=True)
    requires_document = models.BooleanField(
        default=False,
        help_text="Whether an attachment is expected, e.g. a medical certificate.",
    )
    annual_quota = models.PositiveSmallIntegerField(
        default=0,
        help_text="Days per year. 0 means the balance is not tracked for this type.",
    )
    max_consecutive_days = models.PositiveSmallIntegerField(
        default=0, help_text="Longest single application allowed. 0 means no limit."
    )
    status = models.CharField(
        max_length=10, choices=RecordStatus.choices, default=RecordStatus.ACTIVE
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["company_id", "sort_order", "name"]
        verbose_name = "Leave type"
        verbose_name_plural = "Leave types"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_leave_type_code_per_company"
            ),
        ]

    def __str__(self):
        return f"{self.code} – {self.name}"


class Holiday(Stamped):
    """A day the factory is closed.

    Kept per company because the three plants do not shut on the same days, and
    kept as a table rather than a setting because the list changes every year
    and is maintained by HR, not by a deploy.

    ``is_optional`` is a restricted holiday -- offered, not taken by default.
    An optional holiday does **not** exempt a date from consuming leave; only a
    mandatory one does.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="holidays")
    date = models.DateField(db_index=True)
    name = models.CharField(max_length=120)
    is_optional = models.BooleanField(
        default=False,
        help_text="A restricted holiday: offered, but the plant still runs.",
    )

    class Meta:
        ordering = ["-date"]
        verbose_name = "Holiday"
        verbose_name_plural = "Holidays"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "date"], name="uniq_holiday_per_company_date"
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.name}"


class LeaveRequest(Stamped):
    """One person asking to be away over one span of dates."""

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="leave_requests"
    )
    employee = models.ForeignKey(
        "employee_hierarchy.Employee",
        on_delete=models.PROTECT,
        related_name="leave_requests",
        help_text="Who is going to be away. Not necessarily who filled the form in.",
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="requests"
    )

    from_date = models.DateField()
    to_date = models.DateField()
    #: Only meaningful on a single-day request; a multi-day span is always full
    #: days at both ends, because half a day in the middle of a week away is not
    #: a thing anybody has asked for and supporting it would double the rules.
    portion = models.CharField(
        max_length=12, choices=DayPortion.choices, default=DayPortion.FULL
    )
    #: Working days only -- weekly offs and mandatory holidays are excluded when
    #: the day rows are built, so this is what the application actually costs.
    total_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)

    reason = models.TextField(
        help_text="Mandatory. A leave record nobody can explain later is not a record."
    )
    contact_number = models.CharField(max_length=20, blank=True, default="")
    document = models.FileField(
        upload_to="leave_documents/",
        blank=True,
        null=True,
        help_text="Medical certificate and the like, where the type asks for one.",
    )

    status = models.CharField(
        max_length=12,
        choices=LeaveRequestStatus.choices,
        default=LeaveRequestStatus.PENDING,
        db_index=True,
    )

    applied_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_requests_applied",
        help_text="The login that submitted it -- the time office when raised on behalf.",
    )
    applied_at = models.DateTimeField(auto_now_add=True, db_index=True)

    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_requests_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-applied_at", "-id"]
        verbose_name = "Leave request"
        verbose_name_plural = "Leave requests"
        constraints = [
            models.CheckConstraint(
                condition=Q(to_date__gte=models.F("from_date")),
                name="leave_request_dates_in_order",
            ),
        ]
        indexes = [
            # The manager's queue: pending, newest first.
            models.Index(fields=["status", "-applied_at"]),
            # One person's history.
            models.Index(fields=["employee", "-from_date"]),
            # The team calendar: everyone, over a window.
            models.Index(fields=["company", "from_date", "to_date"]),
        ]

    def __str__(self):
        return f"{self.employee_id} {self.from_date}..{self.to_date} ({self.status})"

    @property
    def is_decided(self):
        return self.status != LeaveRequestStatus.PENDING

    @property
    def is_single_day(self):
        return self.from_date == self.to_date


class LeaveRequestDay(models.Model):
    """One date of one application.

    ``employee`` duplicates ``request.employee`` on purpose -- see the module
    docstring. It is what makes "already booked on this date" a constraint the
    database enforces rather than a rule a view has to remember.
    """

    request = models.ForeignKey(
        LeaveRequest, on_delete=models.CASCADE, related_name="days"
    )
    employee = models.ForeignKey(
        "employee_hierarchy.Employee",
        on_delete=models.CASCADE,
        related_name="leave_days",
        help_text="Copied off the request so one date can be locked per person.",
    )
    date = models.DateField(db_index=True)
    portion = models.CharField(
        max_length=12, choices=DayPortion.choices, default=DayPortion.FULL
    )
    status = models.CharField(
        max_length=10,
        choices=LeaveDayStatus.choices,
        default=LeaveDayStatus.PENDING,
        db_index=True,
    )

    #: Whether the attendance sheet has been told about this day. Per day, not
    #: per request: the sync creates attendance rows one date at a time, so a
    #: request is projected piecemeal as its dates arrive.
    is_projected = models.BooleanField(default=False, db_index=True)
    projected_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date"]
        verbose_name = "Leave day"
        verbose_name_plural = "Leave days"
        constraints = [
            models.UniqueConstraint(
                fields=["request", "date"], name="uniq_leave_day_per_request_date"
            ),
            # The one that matters: a person cannot hold the same date twice
            # while it is still live. A half day still occupies the date --
            # splitting one day across two leave types is not supported, and
            # would need its own deliberate design rather than falling out of a
            # missing constraint.
            models.UniqueConstraint(
                fields=["employee", "date"],
                condition=Q(status__in=list(BLOCKING_DAY_STATUSES)),
                name="uniq_live_leave_day_per_employee",
            ),
        ]
        indexes = [
            # The attendance projection: approved, not yet written, up to today.
            models.Index(fields=["status", "is_projected", "date"]),
            models.Index(fields=["employee", "date"]),
        ]

    def __str__(self):
        return f"{self.employee_id} {self.date} {self.portion} ({self.status})"


class LeaveApproval(models.Model):
    """One thing that happened to one request.

    Append-only, and never edited -- the same reasoning as
    :class:`attendance.models.AttendanceOverrideLog`. A decision about somebody
    being away is the kind of record whose value is entirely in nobody being
    able to change it afterwards, and a second decision adds a row rather than
    amending the first.

    Projection events are recorded here too, so one read of this table answers
    "was this ever on the sheet, and when did it come off?".
    """

    request = models.ForeignKey(
        LeaveRequest, on_delete=models.CASCADE, related_name="trail"
    )
    action = models.CharField(max_length=15, choices=LeaveAction.choices)
    from_status = models.CharField(
        max_length=12, choices=LeaveRequestStatus.choices, blank=True, default=""
    )
    to_status = models.CharField(
        max_length=12, choices=LeaveRequestStatus.choices, blank=True, default=""
    )
    comment = models.TextField(blank=True, default="")

    #: How the decider was entitled to decide -- 'manager', 'skip_level' or
    #: 'hr'. Stored rather than derived because the tree moves: somebody who
    #: approved as a manager in March may not be in that line by July, and the
    #: trail should still say what it was at the time.
    authority = models.CharField(max_length=20, blank=True, default="")

    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_decisions",
    )
    performed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-performed_at", "-id"]
        verbose_name = "Leave trail entry"
        verbose_name_plural = "Leave trail"
        indexes = [models.Index(fields=["request", "-performed_at"])]

    def __str__(self):
        return f"{self.request_id}: {self.action}"
