# quality_control/models/material_type_sap_item.py

from django.db import models
from gate_core.models import BaseModel


class MaterialTypeSAPItem(BaseModel):
    """
    Links a SAP item master code to a QC material type.

    A single SAP code may be linked to several material types; the material
    type is chosen at inspection time. Each (company, item_code, material_type)
    combination is unique, so a code can't be linked to the same type twice.
    """
    material_type = models.ForeignKey(
        "quality_control.MaterialType",
        on_delete=models.CASCADE,
        related_name="sap_items",
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="qc_material_type_sap_items",
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("company", "item_code", "material_type")
        ordering = ["item_code"]
        indexes = [
            models.Index(fields=["company", "item_code"]),
            models.Index(fields=["material_type", "is_active"]),
        ]

    def __str__(self):
        return f"{self.item_code} -> {self.material_type.code}"

    def save(self, *args, **kwargs):
        self.item_code = (self.item_code or "").strip().upper()
        self.item_name = (self.item_name or "").strip()
        super().save(*args, **kwargs)
