# quality_control/models/production_qc_result.py

from django.db import models
from gate_core.models import BaseModel


class ProductionQCResult(BaseModel):
    """
    Stores the actual test result for each QC parameter within a production QC session.
    Mirrors InspectionParameterResult but linked to ProductionQCSession.
    """
    session = models.ForeignKey(
        "quality_control.ProductionQCSession",
        on_delete=models.CASCADE,
        related_name="results"
    )

    parameter_master = models.ForeignKey(
        "quality_control.QCParameterMaster",
        on_delete=models.PROTECT,
        related_name="production_results"
    )

    # Denormalized from master for easy access
    parameter_name = models.CharField(max_length=200)
    standard_value = models.CharField(max_length=200)

    # Test results
    result_value = models.CharField(max_length=200, blank=True)
    result_numeric = models.DecimalField(
        max_digits=12, decimal_places=4,
        null=True, blank=True
    )

    # Auto-calculated validation
    is_within_spec = models.BooleanField(null=True)

    remarks = models.TextField(blank=True)

    class Meta:
        unique_together = ("session", "parameter_master")
        ordering = ["parameter_master__sequence"]

    def __str__(self):
        return f"{self.parameter_name}: {self.result_value or self.result_numeric}"

    def save(self, *args, **kwargs):
        # Auto-copy parameter name and standard value from master
        if not self.parameter_name and self.parameter_master:
            self.parameter_name = self.parameter_master.parameter_name
        if not self.standard_value and self.parameter_master:
            self.standard_value = self.parameter_master.standard_value

        # Auto-derive is_within_spec from the parameter's spec range (usually
        # free text like "235+-5.0", with the reading typed into result_value).
        # Mirrors InspectionParameterResult; leaves a manual pass/fail flag alone
        # when the spec is non-numeric or there is no reading to judge.
        if self.parameter_master:
            from ..services.spec_evaluation import evaluate_within_spec
            computed = evaluate_within_spec(
                self.parameter_master.standard_value,
                self.parameter_master.min_value,
                self.parameter_master.max_value,
                self.result_numeric,
                self.result_value,
            )
            if computed is not None:
                self.is_within_spec = computed

        super().save(*args, **kwargs)
