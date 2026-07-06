# quality_control/models/material_arrival_slip.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from gate_core.models import BaseModel
from raw_material_gatein.models import POItemReceipt
from ..enums import ArrivalSlipStatus

User = settings.AUTH_USER_MODEL


class MaterialArrivalSlip(BaseModel):
    """
    Material Arrival Slip - filled by Security Guard.
    One-to-one with POItemReceipt (each PO item gets its own arrival slip).
    """
    po_item_receipt = models.OneToOneField(
        POItemReceipt,
        on_delete=models.CASCADE,
        related_name="arrival_slip",
        null=True,  # Temporarily nullable for migration
        blank=True
    )

    # Arrival Information
    particulars = models.TextField(help_text="Item description")
    arrival_datetime = models.DateTimeField()
    weighing_required = models.BooleanField(default=False)

    # Party/Supplier Information
    party_name = models.CharField(max_length=200)

    # Quantity Information
    billing_qty = models.DecimalField(max_digits=12, decimal_places=3)
    billing_uom = models.CharField(max_length=20, blank=True)

    # Time Tracking
    in_time_to_qa = models.DateTimeField(null=True, blank=True)

    # Vehicle/Transport Details
    truck_no_as_per_bill = models.CharField(max_length=50)

    # Document References
    commercial_invoice_no = models.CharField(max_length=100, blank=True)
    eway_bill_no = models.CharField(max_length=100, blank=True)
    bilty_no = models.CharField(max_length=100, blank=True)

    # Certificates
    has_certificate_of_analysis = models.BooleanField(default=False)
    has_certificate_of_quantity = models.BooleanField(default=False)

    # Status and Workflow
    status = models.CharField(
        max_length=20,
        choices=ArrivalSlipStatus.choices,
        default=ArrivalSlipStatus.DRAFT
    )
    is_submitted = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_arrival_slips"
    )

    remarks = models.TextField(blank=True)

    # Send-back tracking
    sent_back_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_back_arrival_slips"
    )
    sent_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "submitted_at"],
                name="matslip_status_subat_idx",
            ),
        ]
        permissions = [
            ("can_submit_arrival_slip", "Can submit arrival slip to QA"),
            ("can_send_back_arrival_slip", "Can send arrival slip back to gate for correction"),
        ]

    def __str__(self):
        return f"Arrival Slip - {self.po_item_receipt.po_item_code}"

    def submit_to_qa(self, user):
        """Submit the arrival slip to QA for inspection."""
        self.is_submitted = True
        self.submitted_at = timezone.now()
        self.submitted_by = user
        self.status = ArrivalSlipStatus.SUBMITTED
        self.in_time_to_qa = timezone.now()
        self.save(update_fields=[
            "is_submitted", "submitted_at", "submitted_by",
            "status", "in_time_to_qa", "updated_at"
        ])

    def send_back_to_gate(self, user, remarks=""):
        """Send the arrival slip back to gate for correction (before inspection starts)."""
        self.status = ArrivalSlipStatus.DRAFT
        self.is_submitted = False
        self.sent_back_by = user
        self.sent_back_at = timezone.now()
        if remarks:
            self.remarks = remarks
        self.save(update_fields=[
            "status", "is_submitted", "sent_back_by",
            "sent_back_at", "remarks", "updated_at",
        ])

    def reject_by_qa(self, remarks=""):
        """Mark as rejected by QA - sends back to security guard."""
        self.status = ArrivalSlipStatus.REJECTED
        self.is_submitted = False
        if remarks:
            self.remarks = remarks
        self.save(update_fields=["status", "is_submitted", "remarks", "updated_at"])
