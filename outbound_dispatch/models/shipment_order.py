from django.db import models
from django.conf import settings
from gate_core.models.base import BaseModel


class ShipmentStatus(models.TextChoices):
    RELEASED = "RELEASED", "Released"
    PICKING = "PICKING", "Picking"
    PACKED = "PACKED", "Packed"
    STAGED = "STAGED", "Staged"
    LOADING = "LOADING", "Loading"
    DISPATCHED = "DISPATCHED", "Dispatched"
    CANCELLED = "CANCELLED", "Cancelled"


class ShipmentOrder(BaseModel):
    """
    Represents an outbound shipment order synced from SAP Sales Order.
    Tracks the full lifecycle from release to dispatch.
    """
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="shipment_orders"
    )

    # SAP Sales Order reference
    sap_doc_entry = models.IntegerField(help_text="SAP Sales Order DocEntry")
    sap_doc_num = models.IntegerField(help_text="SAP Sales Order DocNum")

    # Customer
    customer_code = models.CharField(max_length=50, help_text="SAP Business Partner code")
    customer_name = models.CharField(max_length=200)
    ship_to_address = models.TextField(blank=True, default="")

    # Carrier
    carrier_code = models.CharField(max_length=50, blank=True, default="")
    carrier_name = models.CharField(max_length=200, blank=True, default="")

    # Schedule
    scheduled_date = models.DateField(help_text="Planned dispatch date")

    # Dock assignment
    dock_bay = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="Assigned bay (Zone C: 19-30)"
    )
    dock_slot_start = models.DateTimeField(null=True, blank=True)
    dock_slot_end = models.DateTimeField(null=True, blank=True)

    # Status
    status = models.CharField(
        max_length=20,
        choices=ShipmentStatus.choices,
        default=ShipmentStatus.RELEASED
    )

    # Vehicle link (when carrier arrives)
    vehicle_entry = models.ForeignKey(
        "driver_management.VehicleEntry",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_shipments"
    )

    # Dispatch details
    bill_of_lading_no = models.CharField(max_length=50, blank=True, default="")
    seal_number = models.CharField(max_length=50, blank=True, default="")
    total_weight = models.DecimalField(
        max_digits=12, decimal_places=3,
        null=True, blank=True,
        help_text="Total load weight in kg"
    )

    notes = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("company", "sap_doc_entry")
        ordering = ["-created_at"]
        permissions = [
            ("can_sync_shipments", "Can sync shipment orders from SAP"),
            ("can_assign_dock_bay", "Can assign dock bay to shipments"),
            ("can_dispatch_shipment", "Can dispatch shipments"),
            ("can_view_outbound_dashboard", "Can view outbound dashboard"),
        ]

    def __str__(self):
        return f"SO-{self.sap_doc_num} ({self.customer_name})"
