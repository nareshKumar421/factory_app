# weighment/models/weighment.py

from django.db import models
from gate_core.models import BaseModel
from driver_management.models import VehicleEntry

class Weighment(BaseModel):
    vehicle_entry = models.OneToOneField(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="weighment"
    )

    gross_weight = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    tare_weight = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )

    net_weight = models.DecimalField(
        max_digits=12, decimal_places=3, editable=False
    )

    # Weight declared on the delivery challan, kept for comparison against the
    # weighbridge net weight. Optional; blank when no challan is presented.
    challan_weight = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )

    weighbridge_slip_no = models.CharField(
        max_length=50, blank=True
    )

    first_weighment_time = models.DateTimeField(
        null=True, blank=True
    )
    second_weighment_time = models.DateTimeField(
        null=True, blank=True
    )

    remarks = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        """
        Net weight rules:
        - If both gross & tare present → net = gross - tare
        - Else → net = 0 (temporary)
        """
        if self.gross_weight is not None and self.tare_weight is not None:
            self.net_weight = self.gross_weight - self.tare_weight
        else:
            self.net_weight = 0

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Weighment - {self.vehicle_entry.entry_no}"
