# quality_control/models/qc_record.py
"""
Fillable QC record forms ("Documents") held as data, not as columns.

QA fills many different printed forms by hand -- the NMW Daily Water
Monitoring Record, and others in the same shape: a header, one or more
sections, a fixed list of parameters down the left (each with its own
frequency and specification), and a column per observation time across the
top.

Rather than a table per form, the *layout* is data:

* :class:`RecordTemplate`          -- one printed form (code, title, revision).
* :class:`RecordTemplateSection`   -- e.g. "Borewell Water", "Treated Water".
* :class:`RecordTemplateParameter` -- one row: Sr.No, name, frequency, spec.

and the *filled sheet* is another three:

* :class:`QCRecord` -- one day's sheet against a template.
* :class:`RecordTimeSlot`   -- the time columns actually used that day.
* :class:`RecordValue`      -- one cell: (time slot x parameter) -> value.

Adding a new printed form is therefore a data change, not a migration.
Values are stored as text so a single cell can hold "Clear", "No off odour"
or "7.63"; numeric parameters are additionally range-checked against their
own specification via :meth:`RecordTemplateParameter.check_value`.
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import models

from company.models import Company
from gate_core.models import BaseModel


class ValueType(models.TextChoices):
    """How one parameter's cell is captured and validated."""

    NUMBER = "NUMBER", "Number"
    TEXT = "TEXT", "Free text"
    CHOICE = "CHOICE", "One of a fixed list"


class RecordStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class RecordTemplate(BaseModel):
    """One printed QC form that gets filled in repeatedly.

    A form is *shared*: the same printed sheet is used at every plant, so
    ``company`` is normally null and every company sees it. Filled sheets
    (:class:`QCRecord`) stay company-specific -- two plants record their own
    readings on the same day.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="record_templates",
        null=True,
        blank=True,
        help_text="Null = a shared form, usable from every company. Set a "
        "company only to keep a form private to that one plant.",
    )
    document_code = models.CharField(
        max_length=64,
        help_text="Controlled document code as printed, e.g. 'QA-FRM-14-00-05-05'.",
    )
    title = models.CharField(
        max_length=255, help_text="e.g. 'NMW DAILY WATER MONITORING RECORD'."
    )
    organisation = models.CharField(max_length=255, blank=True, default="")
    revision_number = models.CharField(max_length=8, blank=True, default="")
    revision_date = models.DateField(null=True, blank=True)
    classification = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["title"]
        constraints = [
            # Scoped to live rows: delete is a soft retire, so without
            # `is_active` the retired row keeps the code reserved for ever.
            models.UniqueConstraint(
                fields=["company", "document_code"],
                condition=models.Q(is_active=True) & models.Q(company__isnull=False),
                name="uq_record_template_company_code",
            ),
            # Shared forms need their own constraint: two NULLs are never
            # equal in SQL, so the one above cannot dedupe them.
            models.UniqueConstraint(
                fields=["document_code"],
                condition=models.Q(is_active=True) & models.Q(company__isnull=True),
                name="uq_record_template_shared_code",
            ),
        ]

    def __str__(self):
        return f"{self.document_code} - {self.title}"

    @property
    def revision_label(self):
        if self.revision_number and self.revision_date:
            return f"{self.revision_number}/{self.revision_date.strftime('%d-%m-%Y')}"
        return self.revision_number or ""


class RecordTemplateSection(BaseModel):
    """A block of parameters within a form, e.g. 'Borewell Water'."""

    template = models.ForeignKey(
        RecordTemplate, on_delete=models.CASCADE, related_name="sections"
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    title = models.CharField(max_length=255)

    class Meta:
        ordering = ["sequence", "id"]
        indexes = [models.Index(fields=["template", "sequence"])]

    def __str__(self):
        return f"{self.template.document_code} - {self.title}"


class RecordTemplateParameter(BaseModel):
    """One row of a form: what is measured, how often, and to what spec."""

    section = models.ForeignKey(
        RecordTemplateSection, on_delete=models.CASCADE, related_name="parameters"
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    sr_no = models.CharField(
        max_length=8, blank=True, default="", help_text="Sr.No as printed."
    )
    name = models.CharField(max_length=120, help_text="e.g. 'pH', 'Turbidity'.")
    frequency = models.CharField(
        max_length=160,
        blank=True,
        default="",
        help_text="As printed, e.g. 'Every Startup / Every 2 Hours'.",
    )
    specification = models.CharField(
        max_length=160,
        blank=True,
        default="",
        help_text="As printed, e.g. '6.5 - 8.5', 'Max 2.0 NTU', 'No off Odour'.",
    )
    unit = models.CharField(max_length=20, blank=True, default="")
    value_type = models.CharField(
        max_length=8, choices=ValueType.choices, default=ValueType.NUMBER
    )
    # Machine-readable form of `specification`, used to flag out-of-spec cells.
    min_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    max_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    allowed_values = models.JSONField(
        default=list,
        blank=True,
        help_text="Suggestions offered for a CHOICE parameter, e.g. "
        "['No off Odour', 'Off Odour']. These are only suggestions -- the "
        "operator may still type any other observation.",
    )
    conforming_values = models.JSONField(
        default=list,
        blank=True,
        help_text="The subset of observations that MEET the specification, "
        "e.g. ['No off Odour']. Kept separate from allowed_values because "
        "that list offers both the passing and the failing option. Leave "
        "empty to record the observation without judging it.",
    )

    class Meta:
        ordering = ["sequence", "id"]
        indexes = [models.Index(fields=["section", "sequence"])]

    def __str__(self):
        return f"{self.sr_no}. {self.name}"

    def check_value(self, raw):
        """Return True (in spec), False (out of spec), or None (not checkable).

        None is returned for a blank cell, a free-text parameter, or a numeric
        parameter with no min/max recorded -- those cannot be judged, and are
        deliberately not reported as failures.
        """
        if raw is None or str(raw).strip() == "":
            return None

        value = str(raw).strip()

        if self.value_type == ValueType.CHOICE:
            # Judged against the conforming list, never against the offered
            # options -- those include the failing choice too, so matching
            # them would mark "Off Odour" as passing. A freely typed
            # observation is judged the same way: if it is not one of the
            # conforming values, it does not meet the specification.
            if not self.conforming_values:
                return None
            return any(
                value.casefold() == str(option).casefold()
                for option in self.conforming_values
            )

        if self.value_type == ValueType.TEXT:
            return None

        if self.min_value is None and self.max_value is None:
            return None
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError):
            return False

        # Coerce the bounds too. They come back from the database as Decimal,
        # but an unsaved instance holds whatever was assigned (often a string),
        # and `Decimal < str` raises rather than comparing.
        def bound(raw_bound):
            if raw_bound is None:
                return None
            try:
                return Decimal(str(raw_bound))
            except (InvalidOperation, ValueError):
                return None

        low, high = bound(self.min_value), bound(self.max_value)
        if low is not None and number < low:
            return False
        if high is not None and number > high:
            return False
        return True


class QCRecord(BaseModel):
    """One filled sheet: a template, a date, and the values captured that day."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="qc_records"
    )
    template = models.ForeignKey(
        RecordTemplate, on_delete=models.PROTECT, related_name="records"
    )
    record_date = models.DateField()
    shift = models.CharField(max_length=8, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=10, choices=RecordStatus.choices, default=RecordStatus.DRAFT
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_records_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_records_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-record_date", "-id"]
        constraints = [
            # One sheet per form per day per shift -- the paper form works the
            # same way, and it stops a second sheet being opened by mistake.
            models.UniqueConstraint(
                fields=["company", "template", "record_date", "shift"],
                condition=models.Q(is_active=True),
                # Scoped to live rows: delete is a soft retire, so without
                # `is_active` the retired row keeps the value reserved for
                # ever and it can never be re-used.
                name="uq_qc_record_day",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "record_date"]),
            models.Index(fields=["company", "status"]),
        ]
        permissions = [
            ("can_view_qc_records", "Can view QC record forms and filled records"),
            ("can_fill_qc_records", "Can open and fill QC records"),
            (
                "can_approve_qc_records",
                "Can approve QC records and maintain record forms",
            ),
        ]

    def __str__(self):
        return f"{self.template.title} - {self.record_date}"


class RecordTimeSlot(BaseModel):
    """One observation-time column of a filled sheet, e.g. 08:10."""

    record = models.ForeignKey(
        QCRecord, on_delete=models.CASCADE, related_name="time_slots"
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    slot_time = models.TimeField()

    class Meta:
        ordering = ["sequence", "slot_time", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "slot_time"], name="uq_record_time_slot"
            ),
        ]

    def __str__(self):
        return self.slot_time.strftime("%H:%M")


class RecordValue(BaseModel):
    """One cell of the grid: what was observed for a parameter at a time."""

    record = models.ForeignKey(
        QCRecord, on_delete=models.CASCADE, related_name="values"
    )
    time_slot = models.ForeignKey(
        RecordTimeSlot, on_delete=models.CASCADE, related_name="values"
    )
    parameter = models.ForeignKey(
        RecordTemplateParameter, on_delete=models.PROTECT, related_name="values"
    )
    # Text, so one column holds "Clear", "No off Odour" and "7.63" alike.
    value = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["time_slot__sequence", "parameter__sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["time_slot", "parameter"], name="uq_record_value_cell"
            ),
        ]
        indexes = [models.Index(fields=["record"])]

    def __str__(self):
        return f"{self.parameter.name} @ {self.time_slot}: {self.value}"

    @property
    def in_spec(self):
        """True / False / None, delegating to the parameter's specification."""
        return self.parameter.check_value(self.value)
