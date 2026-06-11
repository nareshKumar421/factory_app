# quality_control/models/material_type_sap_item.py

from django.db import models
from gate_core.models import BaseModel


class MaterialTypeSAPItem(BaseModel):
    """
    Links one SAP item master code to exactly one QC material type.
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
        unique_together = ("company", "item_code")
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
