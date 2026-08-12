# quality_control/models/qc_parameter_master.py

from django.db import models
from gate_core.models import BaseModel
from ..enums import ParameterType


class QCParameterMaster(BaseModel):
    """
    A single QC parameter definition — what to test and the value it must hit.

    Parameters belong to a :class:`QCParameterSet`, not to the material type
    directly, so one material type can carry different limits per vendor. The
    material type is still reachable through ``parameter_set``.
    """
    parameter_set = models.ForeignKey(
        "quality_control.QCParameterSet",
        on_delete=models.CASCADE,
        related_name="parameters"
    )

    parameter_name = models.CharField(max_length=200)
    parameter_code = models.CharField(max_length=50)

    standard_value = models.CharField(
        max_length=200,
        help_text="e.g., '1.35±0.10', 'Blue', 'Free from defects'"
    )

    parameter_type = models.CharField(
        max_length=20,
        choices=ParameterType.choices,
        default=ParameterType.TEXT
    )

    # For numeric validation
    min_value = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True
    )
    max_value = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True
    )

    uom = models.CharField(
        max_length=50,
        blank=True,
        help_text="Unit of measurement"
    )

    sequence = models.PositiveIntegerField(default=0)
    is_mandatory = models.BooleanField(default=True)

    class Meta:
        ordering = ["sequence"]
        unique_together = ("parameter_set", "parameter_code")

    def __str__(self):
        return f"{self.parameter_set.material_type.code} - {self.parameter_name}"

    @property
    def material_type(self):
        """The material type this parameter ultimately belongs to."""
        return self.parameter_set.material_type

    @property
    def material_type_id(self):
        return self.parameter_set.material_type_id
