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
        self._sync_arrival_tare()

    def _sync_arrival_tare(self):
        """Push a recorded tare to the vehicle's arrival — the canonical single tare.

        The arrival snapshots the tare when it is created at gate-in, usually
        seconds before the operator types the weighbridge value, and dispatch reads
        the arrival's copy first. A late or corrected tare must therefore follow
        the weighment onto the arrival, else the docking keeps showing the stale
        snapshot (the 0-kg tare on DOCK-20260805-0010). Non-dispatch vehicle
        entries have no arrival and are untouched.
        """
        if self.tare_weight is None:
            return
        from django.utils import timezone

        from gate_core.models import (
            EmptyVehicleGateIn,
            SalesDispatchGateOut,
            VehicleArrival,
        )

        arrival_id = (
            EmptyVehicleGateIn.objects.filter(
                vehicle_entry_id=self.vehicle_entry_id, arrival__isnull=False
            )
            .values_list("arrival_id", flat=True)
            .first()
            or SalesDispatchGateOut.objects.filter(
                vehicle_entry_id=self.vehicle_entry_id, arrival__isnull=False
            )
            .values_list("arrival_id", flat=True)
            .first()
        )
        if arrival_id is None:
            return
        VehicleArrival.objects.filter(id=arrival_id).exclude(
            tare_weight=self.tare_weight
        ).update(tare_weight=self.tare_weight, updated_at=timezone.now())

    def __str__(self):
        return f"Weighment - {self.vehicle_entry.entry_no}"
