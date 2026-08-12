# quality_control/models/qc_parameter_set.py

from django.db import models
from gate_core.models import BaseModel


class QCParameterSet(BaseModel):
    """A vendor-scoped set of QC parameters belonging to one material type.

    The same material can carry different acceptance limits depending on who
    supplied it, so parameters hang off a set rather than off the material type
    directly. That keeps one material type per real material instead of a clone
    per vendor, which matters because the material type is also what SAP item
    codes, production QC sessions and reporting key on.

    Every material type has exactly one default set (``vendor_code`` empty).
    It applies to any vendor without a set of their own, and to production QC,
    which has no vendor at all. A vendor set is a full standalone parameter
    list, not a diff against the default -- what the screen shows is what the
    inspection uses.
    """
    material_type = models.ForeignKey(
        "quality_control.MaterialType",
        on_delete=models.CASCADE,
        related_name="parameter_sets",
    )

    # SAP business-partner code, as carried on the PO (POReceipt.supplier_code).
    # Empty means "the default set", which is why this is blank rather than null:
    # a NULL would not be caught by the unique constraint below.
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    vendor_name = models.CharField(max_length=150, blank=True, default="")

    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ("material_type", "vendor_code")
        # The empty vendor code sorts first, so the default set heads the list.
        ordering = ["vendor_code", "vendor_name"]
        indexes = [
            models.Index(fields=["material_type", "vendor_code", "is_active"]),
        ]

    def __str__(self):
        return f"{self.material_type.code} · {self.label}"

    @property
    def is_default(self):
        return not self.vendor_code

    @property
    def label(self):
        if self.is_default:
            return "Default (all vendors)"
        return self.vendor_name or self.vendor_code

    def save(self, *args, **kwargs):
        self.vendor_code = (self.vendor_code or "").strip().upper()
        self.vendor_name = (self.vendor_name or "").strip()
        super().save(*args, **kwargs)
