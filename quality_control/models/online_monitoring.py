"""
Online Quality Monitoring — digitises the paper "On Line Monitoring Quality
Record" (QA-FRM-14-00-05-04) filled during production.

A record is opened against a production line/batch; the operator adds unlimited
time-based readings (filler speed, organoleptic checks, water quality, package
parameters), each with per-filling-head torque values. Specifications are NOT
hardcoded — they live in :class:`OnlineQualitySpec` and drive validation.
"""

from django.conf import settings
from django.db import models

from gate_core.models.base import BaseModel


class OnlineRecordStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ShiftChoice(models.TextChoices):
    A = "A", "Shift A"
    B = "B", "Shift B"
    C = "C", "Shift C"


class Organoleptic(models.TextChoices):
    ACCEPTABLE = "ACCEPTABLE", "Acceptable"
    NOT_ACCEPTABLE = "NOT_ACCEPTABLE", "Not Acceptable"


class OkNotOk(models.TextChoices):
    OK = "OK", "OK"
    NOT_OK = "NOT_OK", "Not OK"


class PassFail(models.TextChoices):
    PASS = "PASS", "Pass"
    FAIL = "FAIL", "Fail"


class SpecValidationType(models.TextChoices):
    RANGE = "RANGE", "Range (min–max)"
    MIN = "MIN", "Minimum only"
    MAX = "MAX", "Maximum only"
    NONE = "NONE", "No numeric check"


class OnlineQualitySpec(BaseModel):
    """Company specification master for online-monitoring parameters.

    Specs are fetched by the form to validate readings (e.g. pH 6.5–8.5). Keyed
    by ``parameter_key`` which matches a reading field (``ph``, ``tds`` …) or
    ``torque``.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE,
        related_name="online_quality_specs", null=True, blank=True,
        help_text="Null = applies to all companies (global default).",
    )
    parameter_key = models.CharField(
        max_length=40,
        help_text="Stable key matching a reading field, e.g. 'ph', 'tds', 'torque'.",
    )
    parameter_name = models.CharField(max_length=120)
    unit = models.CharField(max_length=20, blank=True, default="")
    min_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    max_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    specification_text = models.CharField(
        max_length=60, blank=True, default="",
        help_text="Human spec as printed, e.g. '6.5-8.5' or '10 ± 2'.",
    )
    validation_type = models.CharField(
        max_length=10, choices=SpecValidationType.choices,
        default=SpecValidationType.RANGE,
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "parameter_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "parameter_key"],
                name="uq_online_spec_company_key",
            ),
        ]

    def __str__(self):
        return f"{self.parameter_name} ({self.specification_text or '-'})"

    def is_within_spec(self, value):
        """Return True/False/None (None = not checkable) for a numeric value."""
        if value is None or self.validation_type == SpecValidationType.NONE:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        lo = float(self.min_value) if self.min_value is not None else None
        hi = float(self.max_value) if self.max_value is not None else None
        if self.validation_type == SpecValidationType.MIN:
            return None if lo is None else v >= lo
        if self.validation_type == SpecValidationType.MAX:
            return None if hi is None else v <= hi
        # RANGE
        if lo is None and hi is None:
            return None
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
        return True


class OnlineQualityRecord(BaseModel):
    """One online-monitoring record for a production line/batch/shift/date."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT,
        related_name="online_quality_records",
    )
    production_line = models.ForeignKey(
        "production_execution.ProductionLine", on_delete=models.PROTECT,
        related_name="online_quality_records",
    )
    date = models.DateField()
    sku = models.CharField(max_length=100, blank=True, default="", help_text="SAP item code / SKU")
    product_name = models.CharField(max_length=200, blank=True, default="")
    flavour = models.CharField(max_length=120, blank=True, default="")
    shift = models.CharField(max_length=2, choices=ShiftChoice.choices, blank=True, default="")
    batch_no = models.CharField(max_length=60, blank=True, default="")

    status = models.CharField(
        max_length=12, choices=OnlineRecordStatus.choices,
        default=OnlineRecordStatus.DRAFT,
    )
    remarks = models.TextField(blank=True, default="")

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="online_quality_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="online_quality_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(blank=True, default="")
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="online_quality_rejected",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date", "-created_at"]
        permissions = [
            ("can_view_online_monitoring", "Can view online quality monitoring"),
            ("can_create_online_monitoring", "Can create/edit draft online monitoring"),
            ("can_submit_online_monitoring", "Can submit online monitoring"),
            ("can_approve_online_monitoring", "Can approve/reject online monitoring"),
        ]

    def __str__(self):
        return f"Online QC {self.date} — {self.production_line_id} — {self.batch_no or '-'}"


class OnlineQualityReading(BaseModel):
    """One time-interval reading within a record. Unlimited per record."""

    record = models.ForeignKey(
        OnlineQualityRecord, on_delete=models.CASCADE, related_name="readings",
    )
    reading_time = models.TimeField()
    filler_speed = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, help_text="BPH",
    )

    # Organoleptic (finished product)
    taste = models.CharField(max_length=15, choices=Organoleptic.choices, blank=True, default="")
    aroma = models.CharField(max_length=15, choices=Organoleptic.choices, blank=True, default="")
    appearance = models.CharField(max_length=15, choices=Organoleptic.choices, blank=True, default="")

    # Water quality (numeric, validated against OnlineQualitySpec)
    ph = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    tds = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    turbidity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    alkalinity = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    total_hardness = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    calcium = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    magnesium = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    chloride = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)

    # Package parameters
    package_attribute = models.CharField(max_length=8, choices=OkNotOk.choices, blank=True, default="")
    date_code = models.CharField(max_length=8, choices=OkNotOk.choices, blank=True, default="")
    rub_test = models.CharField(max_length=6, choices=PassFail.choices, blank=True, default="")
    closure_jump_test = models.CharField(max_length=6, choices=PassFail.choices, blank=True, default="")

    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["reading_time", "id"]

    def __str__(self):
        return f"Reading {self.reading_time} (record {self.record_id})"


class OnlineQualityReadingAttachment(BaseModel):
    """A photo/PDF attached to one time-interval reading.

    The attachment belongs to a reading (which carries ``reading_time``), so files
    are captured against a specific time of the monitoring record. ``created_by`` /
    ``created_at`` (from :class:`BaseModel`) record who uploaded it and when.
    """

    reading = models.ForeignKey(
        OnlineQualityReading, on_delete=models.CASCADE, related_name="attachments",
    )
    file = models.FileField(upload_to="online_quality_attachments/")
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.original_name or self.file.name} — reading {self.reading_id}"


class OnlineQualityTorque(BaseModel):
    """One filling-head torque value for a reading (heads 1–8)."""

    reading = models.ForeignKey(
        OnlineQualityReading, on_delete=models.CASCADE, related_name="torque_heads",
    )
    head_no = models.PositiveSmallIntegerField()
    torque_value = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["head_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["reading", "head_no"], name="uq_online_torque_reading_head",
            ),
        ]

    def __str__(self):
        return f"Head {self.head_no}: {self.torque_value}"
