# raw_material_gatein/models/po_receipt.py

from django.db import models
from gate_core.models import BaseModel
from driver_management.models import VehicleEntry

class POReceipt(BaseModel):
    vehicle_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="po_receipts"
    )

    po_number = models.CharField(max_length=30)
    supplier_code = models.CharField(max_length=30)
    supplier_name = models.CharField(max_length=150)

    # SAP PO reference - needed for GRPO PO linking
    sap_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP PO DocEntry (from OPOR) - used as BaseEntry in GRPO"
    )

    # SAP PO header fields for GRPO pre-fill
    branch_id = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Branch/Business Place ID (OPOR.BPLId)"
    )
    vendor_ref = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Vendor reference from PO (OPOR.NumAtCard)"
    )
    po_date = models.DateField(
        null=True, blank=True,
        help_text="PO posting date from SAP (OPOR.DocDate)"
    )

    invoice_no = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    challan_no = models.CharField(max_length=50, blank=True)

    class Meta:
        unique_together = ("vehicle_entry", "po_number")
        permissions = [
            ("can_complete_raw_material_entry", "Can complete raw material gate entry"),
            ("can_receive_po", "Can receive PO items"),
        ]

    def __str__(self):
        return f"{self.po_number} ({self.vehicle_entry.entry_no})"
