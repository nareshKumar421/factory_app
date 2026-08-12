# quality_control/models/production_qc_result.py

from django.db import models
from .parameter_result import ParameterResultBase


class ProductionQCResult(ParameterResultBase):
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

    class Meta:
        unique_together = ("session", "parameter_master")
        ordering = ["sequence", "id"]

    def __str__(self):
        return f"{self.parameter_name}: {self.result_value or self.result_numeric}"
