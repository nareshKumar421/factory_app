from django.db import models
from django.conf import settings
from driver_management.models import VehicleEntry


class OutboundPurpose(models.Model):
    """Lookup table for outbound vehicle visit purpose."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Outbound Purpose"
        verbose_name_plural = "Outbound Purposes"
        permissions = [
            ("can_manage_outbound_purpose", "Can manage outbound purposes"),
        ]

    def __str__(self):
        return self.name


class OutboundGateEntry(models.Model):
    """
    Records details of an empty vehicle arriving at the factory gate
    for outbound dispatch (loading finished goods / dispatching shipments).
    Linked 1:1 with a VehicleEntry whose entry_type is OUTBOUND.
    """

    DOCK_ZONE_CHOICES = (
        ("ZONE_C", "Zone C (Outbound Bays 19-30)"),
        ("YARD", "Yard / Holding Area"),
    )

    vehicle_entry = models.OneToOneField(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="outbound_entry",
    )

    # Purpose & reference
    purpose = models.ForeignKey(
        OutboundPurpose,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    sales_order_ref = models.CharField(
        max_length=50, blank=True,
        help_text="SAP Sales Order number the vehicle is coming to fulfil",
    )
    customer_name = models.CharField(max_length=200, blank=True)
    customer_code = models.CharField(max_length=50, blank=True)

    # Transporter / logistics
    transporter_name = models.CharField(max_length=200, blank=True)
    transporter_contact = models.CharField(max_length=15, blank=True)
    lr_number = models.CharField(
        max_length=100, blank=True,
        help_text="Lorry Receipt / Transport document number",
    )

    # Vehicle condition on arrival
    vehicle_empty_confirmed = models.BooleanField(
        default=False,
        help_text="Security confirmed the vehicle is empty on arrival",
    )
    trailer_type = models.CharField(max_length=50, blank=True)
    trailer_length_ft = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
    )

    # Parking / dock assignment
    assigned_zone = models.CharField(
        max_length=20, choices=DOCK_ZONE_CHOICES,
        default="YARD",
    )
    assigned_bay = models.CharField(max_length=10, blank=True)
    expected_loading_time = models.DateTimeField(null=True, blank=True)

    # Timestamps
    arrival_time = models.DateTimeField(auto_now_add=True)
    released_for_loading_at = models.DateTimeField(null=True, blank=True)
    exit_time = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="outbound_gate_entries_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Outbound Gate Entry"
        verbose_name_plural = "Outbound Gate Entries"
        ordering = ["-arrival_time"]
        permissions = [
            ("can_complete_outbound_entry", "Can complete outbound gate entry"),
            ("can_release_for_loading", "Can release vehicle for loading"),
        ]

    def __str__(self):
        ve = self.vehicle_entry
        return f"OUT-{ve.entry_no} ({ve.vehicle.vehicle_number})"
