# quality_control/models/parameter_result.py

from django.db import models
from gate_core.models import BaseModel
from ..enums import ParameterType


class ParameterResultBase(BaseModel):
    """Shared behaviour for a recorded reading against one QC parameter.

    The parameter definition is **snapshotted onto the row** when the result is
    created, rather than read live from the master. Acceptance limits are
    vendor-specific and get edited over time, so a report reprinted months later
    has to show the limits that were in force on the day it was inspected — not
    whatever the master says now.

    ``parameter_master`` is still kept as a link for traceability and grouping.
    """

    # --- snapshot of the parameter definition, taken at creation ---
    parameter_name = models.CharField(max_length=200)
    parameter_code = models.CharField(max_length=50, blank=True, default="")
    standard_value = models.CharField(max_length=200)
    parameter_type = models.CharField(
        max_length=20,
        choices=ParameterType.choices,
        default=ParameterType.TEXT,
    )
    min_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    max_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    uom = models.CharField(max_length=50, blank=True, default="")
    sequence = models.PositiveIntegerField(default=0)
    is_mandatory = models.BooleanField(default=True)

    # --- the reading ---
    result_value = models.CharField(max_length=200, blank=True)
    result_numeric = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )

    is_within_spec = models.BooleanField(null=True)

    remarks = models.TextField(blank=True)

    class Meta:
        abstract = True

    SNAPSHOT_FIELDS = (
        "parameter_name",
        "parameter_code",
        "standard_value",
        "parameter_type",
        "min_value",
        "max_value",
        "uom",
        "sequence",
        "is_mandatory",
    )

    def apply_parameter_snapshot(self, parameter):
        """Copy the parameter definition onto this row."""
        for field in self.SNAPSHOT_FIELDS:
            setattr(self, field, getattr(parameter, field))

    def save(self, *args, **kwargs):
        # A row created without an explicit snapshot (admin, older callers)
        # still gets one, so no result can be left reading limits live.
        if self.parameter_master_id and not self.parameter_name:
            self.apply_parameter_snapshot(self.parameter_master)

        # Auto-derive is_within_spec from the snapshotted spec. The range is
        # usually free text (e.g. "235+-5.0", "NLT 20") and the reading is
        # usually typed into result_value, so both are parsed. When the spec is
        # non-numeric (visual / pass-fail) or there is no reading, the value the
        # inspector set by hand is left untouched.
        from ..services.spec_evaluation import evaluate_within_spec
        computed = evaluate_within_spec(
            self.standard_value,
            self.min_value,
            self.max_value,
            self.result_numeric,
            self.result_value,
        )
        if computed is not None:
            self.is_within_spec = computed

        super().save(*args, **kwargs)
