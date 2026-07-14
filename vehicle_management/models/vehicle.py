# vehicle_management/models/vehicle.py
from django.db import models
from gate_core.models import BaseModel
from .transporter import Transporter


class VehicleType(BaseModel):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

class Vehicle(BaseModel):
    vehicle_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.ForeignKey(
        VehicleType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    transporter = models.ForeignKey(
        Transporter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    capacity_ton = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    # Physical size (metres). Captured when a vehicle is registered/edited; used by
    # the "Previously Registered Vehicle" detail view.
    length_m = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    width_m = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    height_m = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return self.vehicle_number
