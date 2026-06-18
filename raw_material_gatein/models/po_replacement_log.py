# raw_material_gatein/models/po_replacement_log.py

from django.db import models

from gate_core.models import BaseModel
from driver_management.models import VehicleEntry


class POReplacementLog(BaseModel):
    """Audit record for replacing a wrong PO on an arrival slip QC sent back.

    The replace itself cascade-deletes the old PO items and their draft arrival
    slips/inspections, so this log preserves what was swapped, the gate user's
    reason, and the QC remark that prompted the correction.
    """

    vehicle_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="po_replacement_logs",
    )

    old_po_number = models.CharField(max_length=30)
    old_supplier_code = models.CharField(max_length=30, blank=True, default="")
    old_supplier_name = models.CharField(max_length=150, blank=True, default="")

    new_po_number = models.CharField(max_length=30)
    new_supplier_code = models.CharField(max_length=30, blank=True, default="")
    new_supplier_name = models.CharField(max_length=150, blank=True, default="")

    supplier_changed = models.BooleanField(default=False)
    reason = models.TextField()
    previous_qc_remark = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.old_po_number} -> {self.new_po_number} "
            f"({self.vehicle_entry.entry_no})"
        )
