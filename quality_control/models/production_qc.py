# quality_control/models/production_qc.py
"""
QA Reports: the records QC maintains (on-line monitoring, water testing,
net content, checklists...), each a paper form QA keeps.

The models keep their first name, "production QC", from before the reports
were cut loose from production; in the app the area is "QA Reports".

A :class:`ProductionParameterType` is a report type: one form, carrying the
parameters it records (:class:`ProductionParameter`, with the spec each must
meet). It is not tied to lines, runs or products — whatever the paper header
asks for (product, line, batch...) is one of its parameters.

A type keeps named defaults (:class:`ProductionParameterTypeDefault`, one per
SKU, say): the standards an entry is judged against and values it pre-fills,
per parameter (:class:`ProductionParameterDefaultValue`).

An entry is a :class:`ProductionQCEntry`: one filled-in copy of the form at a
time, made with one of the type's defaults or none, holding one
:class:`ProductionQCResult` per parameter. Entries filled together (a check
across several moulds, say) share a :class:`ProductionQCSubmission`: separate
everywhere, but approved, sent back and corrected as one. Saving it sends it
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
    """A QA report type, e.g. the oil plant on-line monitoring record.

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


class ProductionParameterTypeDefault(BaseModel):
    """A named set of values for one report type, e.g. "1 L PET Canola".

    The standards on a QA report are per SKU, so a report type keeps one default
    per SKU (or whatever the QC manager splits it by). Picked when an entry is
    made, it sets the standards that entry is judged against and pre-fills
    values; an entry can also be made with none, on the type's own standards.
    """

    parameter_type = models.ForeignKey(
        ProductionParameterType, on_delete=models.CASCADE, related_name="defaults"
    )
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["parameter_type", "name"],
                condition=models.Q(is_active=True),
                name="uq_production_type_default_name",
            ),
        ]

    def __str__(self):
        return f"{self.parameter_type.code} - {self.name}"


class ProductionParameterDefaultValue(models.Model):
    """What one default sets for one parameter.

    The spec — standard, min, max — replaces the parameter's own when any of the
    three is set (a blank standard then reads "-"); all three blank keeps the
    parameter's. ``value`` pre-fills the reading, which stays editable.
    """

    default = models.ForeignKey(
        ProductionParameterTypeDefault, on_delete=models.CASCADE, related_name="values"
    )
    parameter = models.ForeignKey(
        ProductionParameter, on_delete=models.CASCADE, related_name="default_values"
    )
    standard_value = models.CharField(max_length=200, blank=True)
    min_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    value = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("default", "parameter")

    @property
    def sets_spec(self):
        return bool(self.standard_value.strip()) or self.min_value is not None or self.max_value is not None

    def __str__(self):
        return f"{self.default} - {self.parameter.parameter_code}"


class ProductionQCStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Approval"
    SENT_BACK = "SENT_BACK", "Sent Back"
    APPROVED = "APPROVED", "Approved"


class ProductionQCSubmission(models.Model):
    """Entries filled and sent for approval together — e.g. a blown-bottle check
    across its moulds, one entry per mould.

    Each entry stays its own: a row in the list, a column on the sheet and the
    print. Only the decision is shared: they are approved, sent back and
    corrected as one.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_qc_submissions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_qc_submissions",
    )

    def __str__(self):
        return f"QA report submission #{self.pk}"


class ProductionQCEntry(BaseModel):
    """One filled-in copy of a QA report: a reading for each of its parameters.

    Not tied to production: a report is a record QC maintains, so whatever
    the paper header asks for (product, line, batch...) is one of its
    parameters.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="production_qc_entries"
    )
    parameter_type = models.ForeignKey(
        ProductionParameterType, on_delete=models.PROTECT, related_name="entries"
    )
    # The default the entry was made with, if any; its name is kept too, so the
    # entry reads the same if the default is renamed or removed.
    default = models.ForeignKey(
        ProductionParameterTypeDefault,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )
    default_name = models.CharField(max_length=200, blank=True)
    # The entries sent with it, decided with it. Set on every entry (0075 gave
    # each older one its own); null only for a row made outside the service.
    submission = models.ForeignKey(
        ProductionQCSubmission,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entries",
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
            ("can_view_production_qc_entries", "Can view QA report entries"),
            ("can_fill_production_qc_entries", "Can fill and correct QA report entries"),
            ("can_approve_production_qc_entries", "Can approve QA report entries"),
            ("can_manage_production_qc_parameters", "Can manage QA report types"),
        ]

    def __str__(self):
        return f"QA report #{self.pk} - {self.parameter_type.code}"


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
