# quality_control/models/production_qc.py
"""
QC Documents: the records QC maintains (on-line monitoring, water testing,
net content, checklists...), each a paper form QA keeps.

The models keep their first name, "production QC", from before the documents
were cut loose from production; in the app the area is "Documents".

A :class:`ProductionParameterType` is a document type: one form, carrying the
parameters it records (:class:`ProductionParameter`, with the spec each must
meet). It is not tied to lines, runs or products — whatever the paper header
asks for (product, line, batch...) is one of its parameters.

An entry is a :class:`ProductionQCEntry`: one filled-in copy of the form at a
time, holding one :class:`ProductionQCResult` per parameter. Saving it sends it
for approval; a QC lead approves it or sends it back with a remark, and a
sent-back entry is corrected and saved again.
"""

from django.conf import settings
from django.db import models

from company.models import Company
from gate_core.models import BaseModel

from ..enums import ParameterType
from .parameter_result import ParameterResultBase


class ProductionParameterType(BaseModel):
    """A QC document type, e.g. the oil plant on-line monitoring record.

    Each is a paper form QA keeps, so it carries that form's revision for the
    printed sheet. Its
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
    """One filled-in copy of a QC document: a reading for each of its parameters.

    Not tied to production: a document is a record QC maintains, so whatever
    the paper header asks for (product, line, batch...) is one of its
    parameters.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_qc_entries"
    )
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
            ("can_view_production_qc_entries", "Can view QC document entries"),
            ("can_fill_production_qc_entries", "Can fill and correct QC document entries"),
            ("can_approve_production_qc_entries", "Can approve QC document entries"),
            ("can_manage_production_qc_parameters", "Can manage QC document types"),
        ]

    def __str__(self):
        return f"QC document #{self.pk} - {self.parameter_type.code}"


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
