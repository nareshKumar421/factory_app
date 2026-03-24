from django.db import models
from django.conf import settings
from gate_core.models.base import BaseModel


class PickTaskStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"
    SHORT = "SHORT", "Short Pick"


class PickTask(BaseModel):
    """
    Individual pick task assigned to a warehouse operative.
    One task per item per pick location.
    """
    shipment_item = models.ForeignKey(
        "outbound_dispatch.ShipmentOrderItem",
        on_delete=models.CASCADE,
        related_name="pick_tasks"
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_pick_tasks"
    )

    pick_location = models.CharField(max_length=50, help_text="Warehouse bin/slot location")
    pick_qty = models.DecimalField(max_digits=12, decimal_places=3, help_text="Quantity to pick")
    actual_qty = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=PickTaskStatus.choices,
        default=PickTaskStatus.PENDING
    )

    picked_at = models.DateTimeField(null=True, blank=True)
    scanned_barcode = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ["pick_location"]
        permissions = [
            ("can_execute_pick_task", "Can execute pick tasks"),
        ]

    def __str__(self):
        return f"Pick {self.pick_qty} x {self.shipment_item.item_code} from {self.pick_location}"
