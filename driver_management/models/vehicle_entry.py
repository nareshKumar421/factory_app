# driver_management/models/vehicle_entry.py
from .driver import Driver
from gate_core.models import GateEntryBase
from vehicle_management.models import Vehicle
from django.db import models
from django.conf import settings
from company.models import Company

class VehicleEntry(GateEntryBase):

    ENTRY_TYPE_CHOICES = (
        ("RAW_MATERIAL", "Raw Material"),
        ("DAILY_NEED", "Daily Need / Canteen"),
        ("MAINTENANCE", "Maintenance"),
        ("CONSTRUCTION", "Construction"),
        ("OUTBOUND", "Outbound Dispatch"),
    )

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="driver_vehicle_entries"
    )

    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
        related_name="driver_gate_entries"
    )

    driver = models.ForeignKey(
        Driver,
        on_delete=models.PROTECT,
        related_name="gate_entries"
    )

    entry_type = models.CharField(
        max_length=20,
        choices=ENTRY_TYPE_CHOICES,
        null=False,
        blank=False
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="driver_vehicleentry_created"
    )

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="driver_vehicleentry_updated"
    )

    entry_time = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True)
