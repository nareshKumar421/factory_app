from django.db import models
from gate_core.models.base import BaseModel


class PickStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PICKED = "PICKED", "Picked"
    SHORT = "SHORT", "Short Pick"


class ShipmentOrderItem(BaseModel):
    """
    Line item of a ShipmentOrder, mapped from SAP Sales Order line.
    Tracks quantities through pick -> pack -> load stages.
    """
    shipment_order = models.ForeignKey(
        "outbound_dispatch.ShipmentOrder",
        on_delete=models.CASCADE,
        related_name="items"
    )

    # SAP SO line reference
    sap_line_num = models.IntegerField(help_text="SAP Sales Order line number")

    # Item details
    item_code = models.CharField(max_length=50, help_text="SAP Item code")
    item_name = models.CharField(max_length=200)

    # Quantities
    ordered_qty = models.DecimalField(max_digits=12, decimal_places=3)
    picked_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    packed_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    loaded_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    uom = models.CharField(max_length=20, help_text="Unit of measure")
    warehouse_code = models.CharField(max_length=20, help_text="Source warehouse in SAP")
    batch_number = models.CharField(max_length=50, blank=True, default="")
    weight = models.DecimalField(
        max_digits=12, decimal_places=3,
        null=True, blank=True,
        help_text="Item weight in kg"
    )

    pick_status = models.CharField(
        max_length=20,
        choices=PickStatus.choices,
        default=PickStatus.PENDING
    )

    class Meta:
        unique_together = ("shipment_order", "sap_line_num")
        ordering = ["sap_line_num"]

    def __str__(self):
        return f"{self.item_code} - {self.item_name} (qty: {self.ordered_qty})"
