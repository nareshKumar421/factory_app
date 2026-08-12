# quality_control/models/inspection_parameter_result.py

from django.db import models
from .parameter_result import ParameterResultBase


class InspectionParameterResult(ParameterResultBase):
    """
    Stores the actual test results for each QC parameter.

    The parameter definition (name, limits, UOM) is snapshotted onto the row at
    creation — see :class:`ParameterResultBase`.
    """
    inspection = models.ForeignKey(
        "quality_control.RawMaterialInspection",
        on_delete=models.CASCADE,
        related_name="parameter_results"
    )

    # Link to master parameter definition
    parameter_master = models.ForeignKey(
        "quality_control.QCParameterMaster",
        on_delete=models.PROTECT,
        related_name="results"
    )

    class Meta:
        unique_together = ("inspection", "parameter_master")
        ordering = ["sequence", "id"]

    def __str__(self):
        return f"{self.parameter_name}: {self.result_value}"
