# raw_material_gatein/models/po_item_receipt.py

from django.db import models
from gate_core.models import BaseModel
from ..models import POReceipt

class POItemReceipt(BaseModel):
    po_receipt = models.ForeignKey(
        POReceipt,
        on_delete=models.CASCADE,
        related_name="items"
    )

    po_item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200)

    # SAP PO line reference - needed for GRPO PO linking
    sap_line_num = models.IntegerField(
        null=True, blank=True,
        help_text="SAP PO LineNum (from POR1) - used as BaseLine in GRPO"
    )

    # SAP PO line fields for GRPO pre-fill
    unit_price = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True,
        help_text="Unit price from PO (POR1.Price)"
    )
    tax_code = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Tax code from PO (POR1.TaxCode)"
    )
    warehouse_code = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Warehouse code from PO (POR1.WhsCode)"
    )
    gl_account = models.CharField(
        max_length=30, blank=True, default="",
        help_text="G/L Account from PO (POR1.AcctCode)"
    )
    variety = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Item variety from PO (POR1.U_Variety)"
    )

    ordered_qty = models.DecimalField(max_digits=12, decimal_places=3)
    received_qty = models.DecimalField(max_digits=12, decimal_places=3)

    accepted_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0
    )
    rejected_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0
    )

    short_qty = models.DecimalField(
        max_digits=12, decimal_places=3, editable=False
    )

    uom = models.CharField(max_length=20)

    class Meta:
        unique_together = ("po_receipt", "po_item_code")

    def save(self, *args, **kwargs):
        self.short_qty = self.ordered_qty - self.received_qty
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.po_item_code} - {self.item_name}"
