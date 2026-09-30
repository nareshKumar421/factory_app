# quality_control/models/production_qc.py
"""
Production QC: checks QC makes on a line while a run is on it.

The masters mirror the arrival-slip ones. A :class:`ProductionParameterType`
plays the part of a material type: it carries the list of parameters to check
(:class:`ProductionParameter`, with the spec each must meet) and is linked to
the FG products it applies to (:class:`ProductionParameterTypeItem`, keyed on
the SAP item code, as material types are).

A check is a :class:`ProductionQCEntry` against the run on the line at the
time, holding one :class:`ProductionQCResult` per parameter of the chosen type.
Saving it sends it for approval; a QC lead approves it or sends it back with a
remark, and a sent-back entry is corrected and saved again.
"""

from django.conf import settings
from django.db import models

from company.models import Company
from gate_core.models import BaseModel

from ..enums import ParameterType
from .parameter_result import ParameterResultBase


class ProductionParameterType(BaseModel):
    """A family of production checks, e.g. the parameters for 1 L PET oil.

    Each type is also a paper form QA keeps (e.g. the on-line monitoring
    record), so it carries that form's revision for the printed sheet. Its
    document number lives with every other form's, in Master Data > Print
    Documents (a QCPrintDocument for this type).
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_parameter_types"
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    revision = models.CharField(max_length=20, blank=True, default="", help_text="e.g. '02'")
    revision_date = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ("company", "code")
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class ProductionParameterTypeItem(BaseModel):
    """Links an FG item code to a parameter type.

    A product may be linked to several types; the type is chosen when the check
    is made. A product with no link at all is offered every type, and the one
    chosen is linked when the check is saved.
    """

    parameter_type = models.ForeignKey(
        ProductionParameterType, on_delete=models.CASCADE, related_name="items"
    )
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_parameter_type_items"
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("company", "item_code", "parameter_type")
        ordering = ["item_code"]
        indexes = [models.Index(fields=["company", "item_code"])]

    def __str__(self):
        return f"{self.item_code} -> {self.parameter_type.code}"

    def save(self, *args, **kwargs):
        self.item_code = (self.item_code or "").strip().upper()
        self.item_name = (self.item_name or "").strip()
        super().save(*args, **kwargs)


class ProductionParameter(BaseModel):
    """One parameter to check and the value it must hit."""

    parameter_type = models.ForeignKey(
        ProductionParameterType, on_delete=models.CASCADE, related_name="parameters"
    )
    parameter_name = models.CharField(max_length=200)
    parameter_code = models.CharField(max_length=50)
    standard_value = models.CharField(
        max_length=200, help_text="e.g. '910±5', 'NLT 20', 'Proper', 'Free from leak'"
    )
    # What kind of reading it takes. The arrival-slip master calls this
    # `parameter_type`; here that name is the type the parameter belongs to.
    value_type = models.CharField(
        max_length=20, choices=ParameterType.choices, default=ParameterType.TEXT
    )
    min_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    uom = models.CharField(max_length=50, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    is_mandatory = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("parameter_type", "parameter_code")

    def __str__(self):
        return f"{self.parameter_type.code} - {self.parameter_name}"


class ProductionQCStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Approval"
    SENT_BACK = "SENT_BACK", "Sent Back"
    APPROVED = "APPROVED", "Approved"


class ProductionQCEntry(BaseModel):
    """One check made on a line, against the run that was on it."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_qc_entries"
    )
    production_run = models.ForeignKey(
        "production_execution.ProductionRun",
        on_delete=models.PROTECT,
        related_name="qc_entries",
    )
    # Snapshots of the run at the time of the check, so the list reads the same
    # even if the run is edited later.
    line = models.ForeignKey(
        "production_execution.ProductionLine",
        on_delete=models.PROTECT,
        related_name="qc_entries",
    )
    item_code = models.CharField(max_length=100, blank=True)
    product = models.CharField(max_length=200, blank=True)

    parameter_type = models.ForeignKey(
        ProductionParameterType, on_delete=models.PROTECT, related_name="entries"
    )
    checked_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=ProductionQCStatus.choices, default=ProductionQCStatus.PENDING
    )
    remarks = models.TextField(blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_qc_entries_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_qc_entries_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(blank=True)

    sent_back_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_qc_entries_sent_back",
    )
    sent_back_at = models.DateTimeField(null=True, blank=True)
    send_back_remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-checked_at", "-id"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "checked_at"]),
        ]
        permissions = [
            ("can_view_production_qc_entries", "Can view production QC entries"),
            ("can_fill_production_qc_entries", "Can make and correct production QC entries"),
            ("can_approve_production_qc_entries", "Can approve production QC entries"),
            ("can_manage_production_qc_parameters", "Can manage production QC parameter types"),
        ]

    def __str__(self):
        return f"Production QC #{self.pk} - {self.line} - {self.parameter_type.code}"


class ProductionQCResult(ParameterResultBase):
    """The reading for one parameter in an entry.

    The parameter's definition is snapshotted onto the row (see
    :class:`ParameterResultBase`), so an entry keeps the spec it was checked
    against when the master is edited later.
    """

    entry = models.ForeignKey(
        ProductionQCEntry, on_delete=models.CASCADE, related_name="results"
    )
    parameter_master = models.ForeignKey(
        ProductionParameter, on_delete=models.PROTECT, related_name="results"
    )

    class Meta:
        ordering = ["sequence", "id"]
        unique_together = ("entry", "parameter_master")

    def apply_parameter_snapshot(self, parameter):
        # The row's `parameter_type` is the kind of reading, which the master
        # calls `value_type`.
        for field in self.SNAPSHOT_FIELDS:
            source = "value_type" if field == "parameter_type" else field
            setattr(self, field, getattr(parameter, source))
