from django.db import models
from django.conf import settings
from gate_core.models.base import BaseModel


class TrailerCondition(models.TextChoices):
    CLEAN = "CLEAN", "Clean"
    DAMAGED = "DAMAGED", "Damaged"
    REJECTED = "REJECTED", "Rejected"


class OutboundLoadRecord(BaseModel):
    """
    Records trailer inspection and loading details for a shipment.
    Created when dock team inspects the trailer before loading.
    """
    shipment_order = models.OneToOneField(
        "outbound_dispatch.ShipmentOrder",
        on_delete=models.CASCADE,
        related_name="load_record"
    )

    # Trailer inspection
    trailer_condition = models.CharField(
        max_length=20,
        choices=TrailerCondition.choices,
        default=TrailerCondition.CLEAN
    )
    trailer_temp_ok = models.BooleanField(null=True, blank=True, help_text="Temperature check passed")
    trailer_temp_reading = models.DecimalField(
        max_digits=6, decimal_places=2,
        null=True, blank=True,
        help_text="Temperature reading in Celsius"
    )
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="trailer_inspections"
    )
    inspected_at = models.DateTimeField(null=True, blank=True)

    # Loading
    loading_started_at = models.DateTimeField(null=True, blank=True)
    loading_completed_at = models.DateTimeField(null=True, blank=True)
    loaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="outbound_loads"
    )

    # Supervisor confirmation
    supervisor_confirmed = models.BooleanField(default=False)
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="supervised_loads"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        permissions = [
            ("can_inspect_trailer", "Can inspect trailers"),
            ("can_load_truck", "Can load trucks"),
            ("can_confirm_load", "Can confirm load as supervisor"),
        ]

    def __str__(self):
        return f"Load Record for {self.shipment_order}"
