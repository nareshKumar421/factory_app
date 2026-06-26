from django.db import models
from django.conf import settings

from gate_core.models.base import BaseModel
from company.models import Company
from accounts.models import Department
from person_gatein.models import Contractor


class LabourShift(models.TextChoices):
    DAY = "DAY", "Day (07:00-19:00)"
    NIGHT = "NIGHT", "Night (19:00-07:00)"


class SheetStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    VERIFIED = "VERIFIED", "Verified"


class VerifyBasis(models.TextChoices):
    TOTAL = "TOTAL", "Grand Total"
    DEPARTMENT = "DEPARTMENT", "Department"
    CONTRACTOR = "CONTRACTOR", "Contractor"


class LabourShiftWindow(BaseModel):
    """
    Per-company, editable lock/verify times for each shift. When no row exists
    for a (company, shift), code defaults are used (see services.lock).
    """
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="labour_shift_windows"
    )
    shift = models.CharField(max_length=10, choices=LabourShift.choices)
    submit_lock_time = models.TimeField(help_text="Daily auto-submit / lock time (IST)")
    verify_deadline_time = models.TimeField(help_text="Gate verification deadline (IST)")

    class Meta:
        unique_together = ("company", "shift")
        verbose_name = "Labour Shift Window"
        verbose_name_plural = "Labour Shift Windows"

    def __str__(self):
        return f"{self.company.code} {self.shift} (lock {self.submit_lock_time})"


class LabourCountSheet(BaseModel):
    """
    One day's casual-labour man-day count for a (company, department, shift).
    Holds per-contractor counts as related LabourCountItem rows.
    """
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="labour_count_sheets"
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="labour_count_sheets"
    )
    work_date = models.DateField(help_text="For NIGHT, the shift-start date")
    shift = models.CharField(max_length=10, choices=LabourShift.choices)

    status = models.CharField(
        max_length=10, choices=SheetStatus.choices, default=SheetStatus.DRAFT
    )
    is_holiday = models.BooleanField(default=False)
    lock_at = models.DateTimeField(help_text="After this, the sheet is auto-submitted and locked")

    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_sheets_submitted",
    )
    auto_submitted = models.BooleanField(default=False)

    # Verification (filled by the gate; see gate phase)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_sheets_verified",
    )
    gate_counted = models.PositiveIntegerField(null=True, blank=True)
    verify_basis = models.CharField(max_length=12, choices=VerifyBasis.choices, blank=True)
    verify_remark = models.TextField(blank=True)

    class Meta:
        unique_together = ("company", "department", "work_date", "shift")
        ordering = ["-work_date", "department_id"]
        verbose_name = "Labour Count Sheet"
        verbose_name_plural = "Labour Count Sheets"
        indexes = [
            models.Index(fields=["work_date", "shift"]),
            models.Index(fields=["status"]),
        ]
        permissions = [
            ("can_submit_labour_count", "Can submit labour count"),
            ("can_verify_labour_count", "Can verify labour count"),
        ]

    def __str__(self):
        return f"{self.department} {self.work_date} {self.shift} ({self.status})"

    @property
    def total_count(self):
        return sum(item.count for item in self.items.all())


class LabourCountItem(BaseModel):
    sheet = models.ForeignKey(
        LabourCountSheet, on_delete=models.CASCADE, related_name="items"
    )
    contractor = models.ForeignKey(
        Contractor, on_delete=models.PROTECT, related_name="labour_count_items"
    )
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("sheet", "contractor")
        ordering = ["contractor_id"]
        verbose_name = "Labour Count Item"
        verbose_name_plural = "Labour Count Items"

    def __str__(self):
        return f"{self.contractor}: {self.count}"


class LabourVerification(BaseModel):
    """
    Audit of a gate operator's physical head-count tally against the submitted
    counts for a shift. Records HOW the gate tallied (basis + optional scope),
    the counted number and the variance vs the submitted total.
    """
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="labour_verifications"
    )
    work_date = models.DateField()
    shift = models.CharField(max_length=10, choices=LabourShift.choices)
    basis = models.CharField(max_length=12, choices=VerifyBasis.choices)
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="labour_verifications",
    )
    contractor = models.ForeignKey(
        Contractor, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="labour_verifications",
    )
    submitted_total = models.PositiveIntegerField()
    gate_counted = models.PositiveIntegerField()
    variance = models.IntegerField(help_text="gate_counted - submitted_total")
    remark = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Labour Verification"
        verbose_name_plural = "Labour Verifications"

    def __str__(self):
        return f"{self.work_date} {self.shift} {self.basis} v={self.variance}"


class LabourOutBatch(BaseModel):
    """
    One batch of labour marked out at the gate (people leave incrementally
    through the shift change). The sheet's running ``gate_counted`` is the sum
    of its batches.
    """
    sheet = models.ForeignKey(
        LabourCountSheet, on_delete=models.CASCADE, related_name="out_batches"
    )
    count = models.PositiveIntegerField()

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Labour Out Batch"
        verbose_name_plural = "Labour Out Batches"

    def __str__(self):
        return f"sheet {self.sheet_id}: +{self.count}"
