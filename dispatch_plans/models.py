from django.db import models

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.models import BaseModel
from vehicle_management.models import Transporter, Vehicle


class DispatchPlanStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    BOOKED = "BOOKED", "Booked"
    DISPATCHED = "DISPATCHED", "Dispatched"
    CANCELLED = "CANCELLED", "Cancelled"


class TransporterAPInvoiceStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class DispatchPlan(BaseModel):
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="dispatch_plans",
    )
    sap_invoice_doc_entry = models.IntegerField()
    sap_invoice_doc_num = models.CharField(max_length=30, blank=True, default="")
    invoice_number = models.CharField(max_length=50, blank=True, default="")
    eway_bill = models.CharField(max_length=80, blank=True, default="")
    invoice_weight = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        null=True,
        blank=True,
    )
    invoice_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    place_of_supply = models.CharField(max_length=150, blank=True, default="")
    product_variety = models.CharField(max_length=50, blank=True, default="")
    total_litres = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    effective_month = models.DateField(null=True, blank=True)
    budget_delivery_point = models.CharField(max_length=100, blank=True, default="")
    service_location_code = models.IntegerField(null=True, blank=True)
    service_location_name = models.CharField(max_length=100, blank=True, default="")
    sac_entry = models.IntegerField(null=True, blank=True)
    sac_code = models.CharField(max_length=30, blank=True, default="")
    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_plans",
    )
    transporter = models.ForeignKey(
        Transporter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_plans",
    )
    driver = models.ForeignKey(
        Driver,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_plans",
    )
    linked_vehicle_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_plans",
    )

    booking_status = models.CharField(
        max_length=20,
        choices=DispatchPlanStatus.choices,
        default=DispatchPlanStatus.PENDING,
    )
    dispatch_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=50, blank=True, default="")

    transporter_name = models.CharField(max_length=150, blank=True, default="")
    transporter_gstin = models.CharField(max_length=20, blank=True, default="")
    contact_person = models.CharField(max_length=100, blank=True, default="")
    mobile_no = models.CharField(max_length=50, blank=True, default="")
    vehicle_no = models.CharField(max_length=30, blank=True, default="")
    driver_name = models.CharField(max_length=100, blank=True, default="")
    driver_mobile_no = models.CharField(max_length=50, blank=True, default="")
    driver_license_no = models.CharField(max_length=50, blank=True, default="")
    driver_id_proof_type = models.CharField(max_length=50, blank=True, default="")
    driver_id_proof_number = models.CharField(max_length=50, blank=True, default="")

    bilty_no = models.CharField(max_length=50, blank=True, default="")
    bilty_date = models.DateField(null=True, blank=True)
    bilty_attachment = models.FileField(
        upload_to="dispatch_plan_bilty/",
        null=True,
        blank=True,
    )
    bilty_attachment_name = models.CharField(max_length=255, blank=True, default="")

    freight = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_freight = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    kanta_weight = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sap_invoice_doc_entry"],
                name="unique_dispatch_plan_invoice_per_company",
            )
        ]
        indexes = [
            models.Index(fields=["company", "sap_invoice_doc_entry"]),
            models.Index(fields=["company", "booking_status"]),
            models.Index(fields=["company", "dispatch_date"]),
            models.Index(fields=["company", "vehicle"]),
            models.Index(fields=["company", "driver"]),
            models.Index(fields=["company", "linked_vehicle_entry"]),
        ]
        permissions = [
            ("can_view_dispatch_plans", "Can view Dispatch Plans dashboard"),
            ("can_edit_dispatch_plans", "Can edit Dispatch Plans bookings"),
            ("can_link_dispatch_vehicle", "Can link dispatch vehicles"),
            ("can_view_dispatch_schedule", "Can view Dispatch Schedule (read-only)"),
        ]

    def __str__(self):
        doc_num = self.sap_invoice_doc_num or self.sap_invoice_doc_entry
        return f"{self.company.code} invoice {doc_num}"


class TransporterAPInvoicePosting(BaseModel):
    """
    Tracks SAP A/P Invoices posted for transporter invoices.
    One invoice may consume multiple bilty-level service GRPO documents.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="transporter_ap_invoice_postings",
    )
    vendor_code = models.CharField(max_length=50)
    vendor_name = models.CharField(max_length=150, blank=True, default="")
    invoice_number = models.CharField(max_length=100)
    invoice_date = models.DateField(null=True, blank=True)
    invoice_amount = models.DecimalField(max_digits=18, decimal_places=2)
    selected_grpo_total = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    amount_difference = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )
    branch_id = models.IntegerField()

    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    sap_doc_total = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=TransporterAPInvoiceStatus.choices,
        default=TransporterAPInvoiceStatus.PENDING,
    )
    error_message = models.TextField(blank=True, null=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transporter_ap_invoice_postings",
    )
    comments = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "vendor_code", "invoice_number"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["sap_doc_entry"]),
        ]
        permissions = [
            ("can_view_open_bilties", "Can view open dispatch bilties"),
            ("can_post_bilty_service_grpo", "Can post bilty service GRPO"),
            ("can_view_transporter_ap_invoice", "Can view transporter AP invoices"),
            ("can_post_transporter_ap_invoice", "Can post transporter AP invoices"),
        ]

    def __str__(self):
        return f"{self.company.code} {self.vendor_code} invoice {self.invoice_number}"


class TransporterAPInvoiceLine(models.Model):
    transporter_ap_invoice = models.ForeignKey(
        TransporterAPInvoicePosting,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    service_grpo_posting = models.ForeignKey(
        "grpo.ServiceGRPOPosting",
        on_delete=models.PROTECT,
        related_name="transporter_ap_invoice_lines",
    )
    service_grpo_line = models.ForeignKey(
        "grpo.ServiceGRPOLinePosting",
        on_delete=models.PROTECT,
        related_name="transporter_ap_invoice_lines",
        null=True,
        blank=True,
    )
    dispatch_plan = models.ForeignKey(
        DispatchPlan,
        on_delete=models.PROTECT,
        related_name="transporter_ap_invoice_lines",
    )
    base_entry = models.IntegerField()
    base_line = models.IntegerField()
    base_doc_num = models.IntegerField(null=True, blank=True)
    bilty_no = models.CharField(max_length=50, blank=True, default="")
    service_description = models.CharField(max_length=255, blank=True, default="")
    line_total = models.DecimalField(max_digits=18, decimal_places=2)
    tax_code = models.CharField(max_length=50, blank=True, default="")
    gl_account = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["base_entry", "base_line"]),
            models.Index(fields=["dispatch_plan"]),
        ]

    def __str__(self):
        return f"AP invoice line for GRPO {self.base_doc_num or self.base_entry}"


class TransporterAPInvoiceAttachment(models.Model):
    transporter_ap_invoice = models.ForeignKey(
        TransporterAPInvoicePosting,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="transporter_ap_invoice_attachments/")
    original_filename = models.CharField(max_length=255)
    sap_attachment_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Pending Upload"),
            ("UPLOADED", "Uploaded to SAP"),
            ("LINKED", "Linked to SAP Document"),
            ("FAILED", "Upload Failed"),
        ],
        default="PENDING",
    )
    sap_absolute_entry = models.IntegerField(null=True, blank=True)
    sap_error_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transporter_ap_invoice_attachments",
    )

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return (
            f"Attachment for transporter AP invoice "
            f"{self.transporter_ap_invoice_id} - {self.original_filename}"
        )
