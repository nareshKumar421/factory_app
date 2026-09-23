"""
Construction projects -- the factory's own building work.

Six tables, one loop. A project is raised with a budget and a date it is
expected to finish; somebody approves both. From then on the site in-charge
writes down what happened each day and what the money went on, and when the
project needs more of either, that is asked for and approved rather than
absorbed quietly.

What this module is NOT: it holds no bill of quantities, no measurement book,
no running-account bills, no retention or statutory deductions. Those belong to
a firm that contracts construction. This factory has construction done, and
wants to know what it is costing and whether it will finish on time.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from gate_core.models.base import BaseModel

from .constants import (
    AttachmentKind,
    DimensionUnit,
    ExpenseBatchStatus,
    ExpenseCategory,
    PaymentMode,
    ProjectStatus,
    RevisionStatus,
    StopReason,
)

ZERO = Decimal("0.00")


class ProjectSequence(models.Model):
    """One row per company per year, locked while a code is handed out.

    Fifteen lines that stop two people creating PRJ-2026-007 at the same time.
    Deliberately NOT the read-the-highest-and-add-one approach
    ``construction_gatein.ConstructionGateEntry`` uses -- that hands two
    concurrent creates the same number. This mirrors
    ``gate_core.SalesDispatchGatepassSequence.next_gatepass_no()``.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="construction_project_sequences",
    )
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "year"],
                name="uniq_construction_project_sequence",
            )
        ]
        default_permissions = ()
        verbose_name = "Project Number Sequence"
        verbose_name_plural = "Project Number Sequences"

    def __str__(self):
        return f"{self.company_id}/{self.year}: {self.last_number}"

    @classmethod
    def next_code(cls, company, year=None):
        year = year or timezone.localdate().year
        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(
                company=company, year=year, defaults={"last_number": 0}
            )
            sequence.last_number += 1
            sequence.save(update_fields=["last_number", "updated_at"])
            return f"PRJ-{year}-{sequence.last_number:03d}"


class Project(BaseModel):
    """One thing being built: a shed, a wall, a tank foundation, a new floor.

    Approving the project sanctions both the money and the date -- there is no
    separate budget document. ``estimated_cost`` is what was asked for;
    ``sanctioned_budget`` is what was granted, and grows only through an
    approved :class:`ProjectRevision`.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="construction_projects",
    )
    #: Unique per COMPANY, not globally: the sequence behind it counts per
    #: company, so Oil and Mart both legitimately hold a PRJ-2026-001.
    code = models.CharField(max_length=20)
    # Everything below here is optional, because a draft is a form somebody
    # has started rather than a project that exists. Completeness is enforced
    # by ``submit_project`` instead of by the column, so a half-filled draft
    # can be parked and picked up tomorrow.
    name = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    location = models.CharField(
        max_length=200, blank=True, default="", help_text="Where on the campus"
    )

    # --- how big it is -----------------------------------------------------
    # All three optional: a boundary wall has a length and no meaningful
    # breadth, and a levelling job has no height. Nothing is derived from them
    # that would break if they are blank.
    length = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    breadth = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    height = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    dimension_unit = models.CharField(
        max_length=2, choices=DimensionUnit.choices, default=DimensionUnit.FEET
    )

    start_date = models.DateField(null=True, blank=True)
    expected_end_date = models.DateField(
        null=True, blank=True, help_text="When this is expected to finish"
    )
    actual_end_date = models.DateField(null=True, blank=True)

    estimated_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="The budget being asked for",
    )

    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="construction_projects_managed",
    )
    site_incharge = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_projects_site",
        help_text="Who fills the daily log",
    )

    status = models.CharField(
        max_length=20,
        choices=ProjectStatus.choices,
        default=ProjectStatus.DRAFT,
        db_index=True,
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_projects_submitted",
    )
    # The decision, either way. A rejection is a decision too, and recording
    # the rejector in a field called ``approved_by`` would make a refused
    # project read as approved by whoever refused it.
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_projects_decided",
    )
    decision_note = models.CharField(max_length=255, blank=True, default="")

    #: Set only when the project is APPROVED, and never cleared. This -- not
    #: ``status`` and not ``decided_at`` -- is the single test for "is the money
    #: sanctioned". Status would wrongly sanction a draft that was cancelled,
    #: and ``decided_at`` is set by a rejection as well.
    sanctioned_at = models.DateTimeField(null=True, blank=True)

    # --- roll-ups -----------------------------------------------------------
    # Recomputed by services.recompute_totals() from Expense and
    # ProjectRevision. They exist so a list of 40 projects is one query, and
    # nothing ever DECIDES anything from them -- the source is always the rows.
    sanctioned_budget = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO
    )
    spent_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    progress_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Construction Project"
        verbose_name_plural = "Construction Projects"
        unique_together = ("company", "code")
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["manager"]),
            models.Index(fields=["expected_end_date"]),
        ]
        # The module gates entirely on the eight below. Django's automatic
        # add/change/delete/view four would add 24 rows across these six models
        # that nothing checks -- and a `view_project` sitting next to
        # `can_view_project` in the group editor is a footgun, because granting
        # the wrong one looks right and does nothing. Admin for these models is
        # therefore superuser-only, which is what it is used for here.
        default_permissions = ()
        permissions = [
            ("can_view_project", "Can view construction projects"),
            ("can_view_all_projects", "Can view every construction project"),
            ("can_create_project", "Can raise a construction project"),
            ("can_edit_project", "Can edit a construction project"),
            ("can_approve_project", "Can approve a project or its revisions"),
            ("can_log_daily_work", "Can write the daily site log"),
            ("can_record_expense", "Can record construction spend"),
            ("can_approve_expense", "Can approve or disallow a day's spend"),
            ("can_close_project", "Can hold, complete or cancel a project"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    # --- derived figures ----------------------------------------------------

    @property
    def remaining_budget(self):
        return self.sanctioned_budget - self.spent_amount

    @property
    def is_over_budget(self):
        return self.sanctioned_budget > ZERO and self.spent_amount > self.sanctioned_budget

    @property
    def percent_used(self):
        if self.sanctioned_budget <= ZERO:
            return ZERO
        return (self.spent_amount / self.sanctioned_budget * 100).quantize(
            Decimal("0.01")
        )

    @property
    def is_sanctioned(self):
        return self.sanctioned_at is not None

    @property
    def is_editable(self):
        from .constants import EDITABLE_STATUSES

        return self.status in EDITABLE_STATUSES

    @property
    def is_live(self):
        from .constants import ACTIVE_STATUSES

        return self.status in ACTIVE_STATUSES

    @property
    def is_closed(self):
        from .constants import CLOSED_STATUSES

        return self.status in CLOSED_STATUSES

    @property
    def area(self):
        """Length x breadth, or None if either is missing."""
        if self.length is None or self.breadth is None:
            return None
        return (self.length * self.breadth).quantize(Decimal("0.01"))

    @property
    def volume(self):
        """Length x breadth x height, or None if any is missing."""
        if self.length is None or self.breadth is None or self.height is None:
            return None
        return (self.length * self.breadth * self.height).quantize(Decimal("0.01"))

    @property
    def area_unit(self):
        return "sq ft" if self.dimension_unit == DimensionUnit.FEET else "sq m"

    @property
    def volume_unit(self):
        return "cu ft" if self.dimension_unit == DimensionUnit.FEET else "cu m"

    # A draft may have no dates at all, so each of these answers None or False
    # rather than raising. "How late is it?" has no answer for a project whose
    # end date has not been decided yet.
    @property
    def days_elapsed(self):
        if self.start_date is None:
            return None
        end = self.actual_end_date or timezone.localdate()
        return max((end - self.start_date).days, 0)

    @property
    def days_left(self):
        """Days to the expected end. Negative once it is late; None once done."""
        if self.actual_end_date or self.expected_end_date is None:
            return None
        return (self.expected_end_date - timezone.localdate()).days

    @property
    def is_overdue(self):
        if self.actual_end_date or self.is_closed or self.expected_end_date is None:
            return False
        return timezone.localdate() > self.expected_end_date


class EstimateLine(BaseModel):
    """One line of the estimate: 700 bags of cement at 350 = 245,000.

    The breakdown behind ``Project.estimated_cost``. Optional -- a small job can
    carry a round number and no detail -- but **when any line exists the lines
    are the estimate**: ``estimated_cost`` is recomputed from their sum rather
    than typed, because two numbers that are supposed to agree eventually will
    not, and nobody can then say which one was sanctioned.

    Editable only while the project is a draft, for the same reason
    ``estimated_cost`` is: past approval the money moves through a revision.

    ``unit`` is free text, not a master. Construction quotes in BAG, CFT, SFT,
    CUM, NOS, KG, RFT and MT depending on the material, and the gate's
    ``UnitChoice`` list is about vehicles arriving, not concrete.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="estimate_lines"
    )
    line_no = models.PositiveIntegerField(help_text="Sr. No. on the sheet")
    material = models.CharField(max_length=200)
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    unit = models.CharField(max_length=20, blank=True, default="")
    rate = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    #: quantity x rate, stored so the database can sum it.
    amount = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["line_no", "id"]
        default_permissions = ()
        verbose_name = "Estimate Line"
        verbose_name_plural = "Estimate Lines"
        indexes = [models.Index(fields=["project", "line_no"])]

    def __str__(self):
        return f"{self.line_no}. {self.material} — {self.amount}"

    def save(self, *args, **kwargs):
        self.amount = (self.quantity * self.rate).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)


class ProjectAttachment(BaseModel):
    """A file that belongs to the project as a whole.

    The quotation it was costed from, a drawing, a layout, the sanction letter.
    Distinct from :class:`DailyLogPhoto`, which belongs to one day -- these are
    the papers behind the project, not the record of a shift.

    ``title`` is optional: a file named "shed-quote-verma.pdf" already says what
    it is, and forcing a caption gets "doc1" typed into it.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to="construction/projects/")
    title = models.CharField(max_length=200, blank=True, default="")
    #: The map is shown on its own, above the rest. Nothing stops a project
    #: having two -- a site plan and a layout are both maps -- so this is a
    #: kind rather than a single ``map`` field on the project.
    kind = models.CharField(
        max_length=10,
        choices=AttachmentKind.choices,
        default=AttachmentKind.DOCUMENT,
        db_index=True,
    )

    class Meta:
        # Newest first. "Maps first" is NOT expressible here -- Meta ordering
        # sorts the stored value, and "DOCUMENT" < "MAP" alphabetically, so
        # ordering by ``kind`` puts the paperwork above the map. The API
        # annotates the precedence instead; see ``ProjectAttachmentAPI.get``.
        ordering = ["-created_at", "-id"]
        default_permissions = ()
        verbose_name = "Project Attachment"
        verbose_name_plural = "Project Attachments"

    def __str__(self):
        return f"{self.project.code}: {self.title or self.file.name}"


class ProjectRevision(BaseModel):
    """More money, more time, or both.

    One model for both kinds of extension, because that is how it actually
    happens -- "we need two more months and three lakh more" is one
    conversation and one approval, not two.

    ``budget_before`` and ``end_date_before`` are snapshots taken when the
    request is made. They cost two columns and they turn the approval screen
    from "Rs 3,00,000 -- approve?" into "8,00,000 -> 11,00,000, 31 Mar -> 31
    May", which is the difference between an approval and a rubber stamp.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="revisions"
    )
    revision_no = models.PositiveIntegerField()

    additional_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text="Extra budget asked for. Zero means this is a timeline-only ask.",
    )
    new_end_date = models.DateField(
        null=True, blank=True, help_text="The new expected ending, if it is moving"
    )
    reason = models.TextField()

    budget_before = models.DecimalField(max_digits=14, decimal_places=2)
    end_date_before = models.DateField()

    status = models.CharField(
        max_length=15, choices=RevisionStatus.choices, default=RevisionStatus.PENDING
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_revisions_requested",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_revisions_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["project_id", "revision_no"]
        unique_together = ("project", "revision_no")
        default_permissions = ()
        verbose_name = "Project Revision"
        verbose_name_plural = "Project Revisions"
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.project.code} rev {self.revision_no}"

    @property
    def budget_after(self):
        return self.budget_before + self.additional_amount

    @property
    def end_date_after(self):
        return self.new_end_date or self.end_date_before

    @property
    def extension_days(self):
        if not self.new_end_date:
            return 0
        return (self.new_end_date - self.end_date_before).days


class DailyLog(BaseModel):
    """What happened on site on one day.

    One row per project per day. No approval and no verification: it is a
    diary, and a diary that needs signing off stops being written.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="daily_logs"
    )
    log_date = models.DateField()

    work_done = models.TextField(help_text="What got done today")
    workers_count = models.PositiveIntegerField(default=0)
    progress_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cumulative completion. Optional, and it may not go backwards.",
    )

    work_stopped = models.BooleanField(default=False)
    #: Why, in ``stop_reasons`` -- a day can have several at once.
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-log_date", "-id"]
        unique_together = ("project", "log_date")
        default_permissions = ()
        verbose_name = "Daily Log"
        verbose_name_plural = "Daily Logs"
        indexes = [models.Index(fields=["project", "log_date"])]

    def __str__(self):
        return f"{self.project.code} {self.log_date}"


class DailyLogStopReason(models.Model):
    """One reason work did not happen on a day.

    A child table rather than a single choice on the log, because a real day is
    often both: it rained AND the material had not arrived. Keeping it
    normalised means "we lost 11 days to rain and 4 waiting for material" stays
    one GROUP BY -- and that sentence is the whole justification for a timeline
    extension.

    Plain ``models.Model``: it is a tag on a day, with nothing of its own to
    audit and no meaningful "inactive" state.
    """

    daily_log = models.ForeignKey(
        DailyLog, on_delete=models.CASCADE, related_name="stop_reasons"
    )
    reason = models.CharField(max_length=20, choices=StopReason.choices)

    class Meta:
        ordering = ["reason"]
        unique_together = ("daily_log", "reason")
        default_permissions = ()
        verbose_name = "Daily Log Stop Reason"
        verbose_name_plural = "Daily Log Stop Reasons"

    def __str__(self):
        return f"{self.daily_log_id}: {self.reason}"


class DailyLogPhoto(BaseModel):
    """A photo a day: the cheapest progress record there is."""

    daily_log = models.ForeignKey(
        DailyLog, on_delete=models.CASCADE, related_name="photos"
    )
    photo = models.FileField(upload_to="construction/daily-logs/")
    caption = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["id"]
        default_permissions = ()
        verbose_name = "Daily Log Photo"
        verbose_name_plural = "Daily Log Photos"

    def __str__(self):
        return f"{self.daily_log_id}: {self.caption or self.photo.name}"


class ExpenseBatch(BaseModel):
    """A project's running set of payments, settled by one decision.

    Expenses pile into the project's open batch as the site records them. When
    the site sends it, the approver sees one claim -- "the week's spend, three
    lakh, forty-one lines" -- and approving it approves every line in it.

    Reviewing a hundred cement bills one at a time is how a review stops
    happening, which is why there is no per-expense decision. If a single line
    is wrong the approver returns the whole batch with a note; the site fixes
    that line and sends it again.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="expense_batches"
    )
    batch_no = models.PositiveIntegerField()
    status = models.CharField(
        max_length=10,
        choices=ExpenseBatchStatus.choices,
        default=ExpenseBatchStatus.OPEN,
        db_index=True,
    )

    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_batches_submitted",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="construction_batches_decided",
    )
    decision_note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["project_id", "-batch_no"]
        unique_together = ("project", "batch_no")
        default_permissions = ()
        verbose_name = "Expense Batch"
        verbose_name_plural = "Expense Batches"
        constraints = [
            # One batch a project may still add to. Two would split a day's
            # spend across claims by accident.
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(status__in=["OPEN", "RETURNED"], is_active=True),
                name="uniq_open_expense_batch_per_project",
            ),
        ]

    def __str__(self):
        return f"{self.project.code} batch {self.batch_no} ({self.status})"

    @property
    def is_editable(self):
        from .constants import EDITABLE_BATCH_STATUSES

        return self.status in EDITABLE_BATCH_STATUSES

    @property
    def is_settled(self):
        return self.status == ExpenseBatchStatus.APPROVED

    @property
    def total(self):
        return self.expenses.filter(is_active=True).aggregate(
            total=models.Sum("amount")
        )["total"] or ZERO

    @property
    def line_count(self):
        return self.expenses.filter(is_active=True).count()


class Expense(BaseModel):
    """One thing the money went on, against the day it was spent.

    No approval flow. The budget was approved; spending against it is recorded,
    not re-approved. What the module does instead is flag the overrun and offer
    the revision -- see ``services.record_expense``.
    """

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="expenses"
    )
    spend_date = models.DateField(db_index=True)
    category = models.CharField(max_length=20, choices=ExpenseCategory.choices)
    description = models.CharField(
        max_length=300, help_text='e.g. "90 bags cement", "mason wages, 6 days"'
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    #: Typed in, not picked from a master. Construction buys from whoever has
    #: cement that morning, and forcing a vendor master on it means the field
    #: gets filled with "OTHER".
    paid_to = models.CharField(max_length=200, blank=True, default="")
    payment_mode = models.CharField(
        max_length=10, choices=PaymentMode.choices, default=PaymentMode.CASH
    )
    reference_no = models.CharField(
        max_length=60, blank=True, default="", help_text="Bill no, UTR or cheque no"
    )
    bill = models.FileField(
        upload_to="construction/bills/", null=True, blank=True
    )

    # --- review ------------------------------------------------------------
    # Not decided line by line: the line belongs to a batch, and the batch is
    # what somebody approves. See ExpenseBatch.
    batch = models.ForeignKey(
        ExpenseBatch, on_delete=models.PROTECT, related_name="expenses"
    )

    class Meta:
        ordering = ["-spend_date", "-id"]
        default_permissions = ()
        verbose_name = "Construction Expense"
        verbose_name_plural = "Construction Expenses"
        indexes = [
            models.Index(fields=["project", "spend_date"]),
            models.Index(fields=["project", "category"]),
            models.Index(fields=["batch"]),
        ]

    def __str__(self):
        return f"{self.project.code} {self.spend_date} {self.category} {self.amount}"

    @property
    def batch_status(self):
        return self.batch.status

    @property
    def is_editable(self):
        """Only while its batch is still the site's to change."""
        return self.batch.is_editable
