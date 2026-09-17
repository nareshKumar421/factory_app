"""
Attendance: what the punching machine recorded, and what a human corrected.

Three tables, and the split between the first two is the point of the module.

* :class:`DailyAttendance` -- one row per employee per day. Holds *both* the
  machine's reading and the effective one, side by side.
* :class:`AttendanceOverrideLog` -- append-only; every time somebody changed an
  effective status, from what, to what and why.
* :class:`AttendanceRecord` -- the manual gate fallback, a photographed mark
  taken when the machine itself is down.

**The machine's reading is immutable.** ``machine_status`` is written once by
the sync and never updated by a human action -- corrections land in
``effective_status`` instead. That is what makes the dashboard's default view
possible: showing "only what the machine said" is not a filter that reconstructs
history, it is just a different column on the same row. If a correction
overwrote the reading, the question "what did the machine actually record?"
would be unanswerable the moment anyone touched it, and that question is the
one payroll disputes turn on.

**Why the override is denormalised onto the row** rather than read from the log.
The screen this feeds shows three hundred people for one day and filters and
counts by status. Reading the current override from the tail of a log would be
a subquery per row; the log stays authoritative for *history*, the row carries
*now*. :class:`AttendanceOverrideLog` is never edited or deleted, so the two can
be reconciled at any time.

**Why a stored roll-up and not a live query.** The punches live on a SQL Server
box on the factory LAN, reachable only from the plant, with 432,000 rows and
climbing. A dashboard that queried it per page load would be slow when the link
was up and blank when it was not. :mod:`attendance.services` rolls punches into
these rows on a schedule, and every screen reads Postgres.

**Employees come from the directory**, :class:`employee_hierarchy.Employee`,
keyed by the JWPL ``employee_code`` the machines punch on. This module used to
carry its own parallel ``Employee`` master; two masters for one workforce meant
a person could exist in one and not the other, and the join to the punch data
would silently miss them.
"""

from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class AttendanceStatus(models.TextChoices):
    """What a person's day amounted to.

    The first five are what the machine can produce on its own; the rest can
    only ever arrive by human override, because no arrangement of punches
    distinguishes "approved leave" from "did not come in".
    """

    PRESENT = "PRESENT", "Present"
    ABSENT = "ABSENT", "Absent"
    HALF_DAY = "HALF_DAY", "Half day"
    #: Punched once. Not present, not absent -- they were here and the machine
    #: only saw them arrive (or only saw them leave). About one person-day in
    #: seven, and the single biggest reason this module needs an override.
    MISSING_PUNCH = "MISSING_PUNCH", "Missing punch"
    WEEKLY_OFF = "WEEKLY_OFF", "Weekly off"
    # --- override-only ----------------------------------------------------
    ON_LEAVE = "ON_LEAVE", "On leave"
    ON_DUTY = "ON_DUTY", "On duty (outside the plant)"
    HOLIDAY = "HOLIDAY", "Holiday"


#: Statuses the sync may write. Anything else in ``machine_status`` came from a
#: bug, not a machine.
MACHINE_STATUSES = frozenset(
    {
        AttendanceStatus.PRESENT,
        AttendanceStatus.ABSENT,
        AttendanceStatus.HALF_DAY,
        AttendanceStatus.MISSING_PUNCH,
        AttendanceStatus.WEEKLY_OFF,
    }
)

#: Statuses that count as the person having worked, for the summary counts.
PRESENT_STATUSES = frozenset(
    {
        AttendanceStatus.PRESENT,
        AttendanceStatus.HALF_DAY,
        AttendanceStatus.MISSING_PUNCH,
        AttendanceStatus.ON_DUTY,
    }
)


class OverrideReason(models.TextChoices):
    """Why the machine's reading was not the truth.

    An enum rather than free text because this is the column the module exists
    to produce: "how often does the Gate 2 reader fail?" is a report, and it
    cannot be run over prose. The free-text ``reason`` sits beside it and is
    also required -- the code says which kind of problem, the text says what
    actually happened.
    """

    FORGOT_PUNCH = "FORGOT_PUNCH", "Forgot to punch"
    MACHINE_FAILURE = "MACHINE_FAILURE", "Punching machine failed"
    FINGERPRINT_FAILED = "FINGERPRINT_FAILED", "Fingerprint not recognised"
    ON_DUTY_OUTSIDE = "ON_DUTY_OUTSIDE", "On duty outside the plant"
    APPROVED_LEAVE = "APPROVED_LEAVE", "Approved leave"
    HALF_DAY_APPROVED = "HALF_DAY_APPROVED", "Half day approved"
    SHIFT_ADJUSTMENT = "SHIFT_ADJUSTMENT", "Shift adjustment"
    NOT_ENROLLED = "NOT_ENROLLED", "Not enrolled on the machine"
    DATA_ERROR = "DATA_ERROR", "Wrong data from the machine"
    OTHER = "OTHER", "Other"


class OverrideAction(models.TextChoices):
    OVERRIDE = "OVERRIDE", "Status changed"
    AMEND = "AMEND", "Change amended"
    REVERT = "REVERT", "Reverted to the machine's status"


class AttendancePermission(models.Model):
    """Sentinel carrying the module's rights (no table of its own).

    Viewing and correcting are deliberately different grants. The daily sheet is
    meant to be widely readable -- supervisors need to see who turned up -- while
    changing a status is an assertion about a day that has already passed, made
    against the evidence of a machine, and is what payroll will later be run
    from. Those belong to HR, not to everyone who can open the page.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Attendance"
        verbose_name_plural = "Attendance"
        permissions = [
            ("can_view_daily_attendance", "Can view the daily attendance sheet"),
            ("can_override_attendance_status", "Can change an attendance status away from the machine's"),
            ("can_sync_attendance", "Can trigger a punch-machine sync"),
            ("can_export_attendance", "Can export attendance"),
        ]


class DailyAttendance(models.Model):
    """One employee, one day: what the machine saw and what stands.

    ``machine_*`` is the sync's territory and a human never writes it.
    ``effective_status`` is what the business goes with, and equals
    ``machine_status`` until somebody with the right says otherwise.
    """

    employee = models.ForeignKey(
        "employee_hierarchy.Employee",
        on_delete=models.CASCADE,
        related_name="daily_attendance",
    )
    date = models.DateField(db_index=True)

    # --- what the machine recorded. Never written by a user action. ---------
    machine_status = models.CharField(
        max_length=20,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.ABSENT,
        help_text="Derived from the punches alone. Immutable once the sync has written it.",
    )
    machine_first_punch = models.TimeField(
        null=True, blank=True, help_text="First punch of the day -- treated as the arrival."
    )
    machine_last_punch = models.TimeField(
        null=True,
        blank=True,
        help_text="Last punch of the day -- the departure. Equals the first when only one was read.",
    )
    machine_punch_count = models.PositiveSmallIntegerField(default=0)
    machine_worked_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Last punch minus first. Time between gate reads, not productive time.",
    )
    devices = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Serials of the readers that saw them, comma separated.",
    )

    # --- what stands --------------------------------------------------------
    effective_status = models.CharField(
        max_length=20,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.ABSENT,
        db_index=True,
        help_text="The status the business goes with. Equals machine_status unless overridden.",
    )
    is_overridden = models.BooleanField(default=False, db_index=True)
    override_reason_code = models.CharField(
        max_length=30, choices=OverrideReason.choices, blank=True, default=""
    )
    override_reason = models.TextField(
        blank=True,
        default="",
        help_text="Required when overriding. The code says what kind; this says what happened.",
    )
    overridden_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_overrides",
    )
    overridden_at = models.DateTimeField(null=True, blank=True)

    synced_at = models.DateTimeField(
        null=True, blank=True, help_text="When the punches for this day were last rolled up."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "employee__full_name"]
        verbose_name = "Daily attendance"
        verbose_name_plural = "Daily attendance"
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "date"], name="uniq_daily_attendance_per_employee_day"
            ),
        ]
        indexes = [
            # The dashboard: one date, everybody, filtered by status.
            models.Index(fields=["date", "effective_status"]),
            models.Index(fields=["employee", "-date"]),
            # "Show me every correction this month" -- the audit view.
            models.Index(fields=["date", "is_overridden"]),
        ]

    def __str__(self):
        return f"{self.employee_id} {self.date} {self.effective_status}"

    @property
    def is_present(self):
        return self.effective_status in PRESENT_STATUSES

    @property
    def machine_said_present(self):
        """What the default view shows -- the machine's own verdict, untouched."""
        return self.machine_status in PRESENT_STATUSES


class AttendanceOverrideLog(models.Model):
    """One change to one day's status: who, from what, to what, and why.

    Append-only. Never edited, never deleted -- an attendance correction is an
    assertion that contradicts a machine, and the value of recording it is
    entirely in it being unalterable afterwards. A second override on the same
    day adds a row; it does not amend the first.

    Not timestamped with ``updated_at`` for the same reason: there is no such
    thing as updating one of these.
    """

    daily_attendance = models.ForeignKey(
        DailyAttendance, on_delete=models.CASCADE, related_name="override_log"
    )
    action = models.CharField(
        max_length=20, choices=OverrideAction.choices, default=OverrideAction.OVERRIDE
    )
    from_status = models.CharField(max_length=20, choices=AttendanceStatus.choices)
    to_status = models.CharField(max_length=20, choices=AttendanceStatus.choices)
    #: Copied off the row so the log reads on its own, and still reads correctly
    #: if the roll-up is ever re-run.
    machine_status = models.CharField(max_length=20, choices=AttendanceStatus.choices)

    reason_code = models.CharField(
        max_length=30, choices=OverrideReason.choices, blank=True, default=""
    )
    reason = models.TextField(blank=True, default="")

    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_override_entries",
    )
    performed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-performed_at", "-id"]
        verbose_name = "Attendance override entry"
        verbose_name_plural = "Attendance override trail"
        indexes = [models.Index(fields=["daily_attendance", "-performed_at"])]

    def __str__(self):
        return f"{self.daily_attendance_id}: {self.from_status} -> {self.to_status}"


class AttendanceRecord(models.Model):
    """A single manual attendance mark, photographed at the gate.

    The fallback for when the punching machine itself is down: an operator marks
    people IN or OUT by hand with a photo as proof. Separate from
    :class:`DailyAttendance` because it is *evidence*, not a verdict -- several
    marks may exist for one person on one day, and what they add up to is still
    a question for the daily row.
    """

    class Direction(models.TextChoices):
        IN = "IN", "In"
        OUT = "OUT", "Out"

    employee = models.ForeignKey(
        "employee_hierarchy.Employee",
        on_delete=models.PROTECT,
        related_name="manual_attendance_records",
    )
    direction = models.CharField(
        max_length=3,
        choices=Direction.choices,
        default=Direction.IN,
    )
    date = models.DateField()
    time = models.TimeField()
    # Proof that the employee was indeed present at the marked time.
    photo = models.ImageField(upload_to="attendance_photos/")

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-time"]
        permissions = [
            ("can_view_attendance_dashboard", "Can view attendance dashboard"),
        ]

    def __str__(self):
        return f"{self.employee} - {self.date} {self.time} ({self.direction})"
