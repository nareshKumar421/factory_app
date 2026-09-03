from django.db import models

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.models import BaseModel
from vehicle_management.models import Transporter, Vehicle

# The bill summary the warehouse floor picks against lives in a sibling
# module; import so Django registers it with the `dispatch_plans` app.
from .models_bill_summary import (  # noqa: F401
    BillSummary,
    BillSummaryLine,
    BillSummarySapStatus,
    BillSummaryStatus,
)


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
    customer_code = models.CharField(max_length=50, blank=True, default="")
    customer_name = models.CharField(max_length=200, blank=True, default="")
    place_of_supply = models.CharField(max_length=150, blank=True, default="")
    # Free-text delivery location the dispatch-planning team fills in (distinct from
    # the read-only SAP place_of_supply / city / state shown alongside it).
    location = models.CharField(max_length=200, blank=True, default="")
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

    # Transport details are read through the vehicle / transporter / driver FKs
    # rather than stored (they were a duplicate, editable-override snapshot that was
    # never actually overridden). Readers should ``select_related`` those FKs to
    # avoid a per-row query. The docking (``SalesDispatchGateOut``) keeps its own
    # frozen copy for the printed gatepass -- that is a deliberate audit snapshot.
    @property
    def vehicle_no(self) -> str:
        return self.vehicle.vehicle_number if self.vehicle_id else ""

    @property
    def transporter_name(self) -> str:
        return self.transporter.name if self.transporter_id else ""

    @property
    def transporter_gstin(self) -> str:
        return (self.transporter.gstin or "") if self.transporter_id else ""

    @property
    def contact_person(self) -> str:
        return self.transporter.contact_person if self.transporter_id else ""

    @property
    def mobile_no(self) -> str:
        return self.transporter.mobile_no if self.transporter_id else ""

    @property
    def driver_name(self) -> str:
        return self.driver.name if self.driver_id else ""

    @property
    def driver_mobile_no(self) -> str:
        return self.driver.mobile_no if self.driver_id else ""

    @property
    def driver_license_no(self) -> str:
        return self.driver.license_no if self.driver_id else ""

    @property
    def driver_id_proof_type(self) -> str:
        return self.driver.id_proof_type if self.driver_id else ""

    @property
    def driver_id_proof_number(self) -> str:
        return self.driver.id_proof_number if self.driver_id else ""

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
            ("can_select_dispatch_bills", "Can select bills for dispatch planning"),
            ("can_view_dispatch_schedule", "Can view Dispatch Schedule (read-only)"),
            ("can_view_dispatch_pipeline", "Can view Dispatch Pipeline board"),
            # Inside Vehicle Manager (dispatch correction console) -- one per action.
            ("can_view_inside_vehicle_manager", "Can view the Inside Vehicle Manager"),
            ("can_add_bill_inside_vehicle", "Can add a bill to an inside vehicle"),
            ("can_remove_bill_inside_vehicle", "Can remove a bill from an inside vehicle"),
            ("can_move_bill_inside_vehicle", "Can move a bill between inside vehicles"),
            ("can_unlink_bills_inside_vehicle", "Can unlink all bills from an inside vehicle"),
            ("can_mark_out_inside_vehicle", "Can mark an inside vehicle out from the manager"),
        ]

    def __str__(self):
        doc_num = self.sap_invoice_doc_num or self.sap_invoice_doc_entry
        return f"{self.company.code} invoice {doc_num}"


class DispatchPlanAttachmentAuditAction(models.TextChoices):
    ADDED = "ADDED", "Added"
    REPLACED = "REPLACED", "Replaced"
    DELETED = "DELETED", "Deleted"


class DispatchPlanAttachmentAuditSource(models.TextChoices):
    MANUAL = "MANUAL", "Service GRPO screen"
    VEHICLE_LINKING = "VEHICLE_LINKING", "Vehicle linking"


class DispatchPlanAttachmentAudit(models.Model):
    """Every change to a plan's bilty attachment, kept forever.

    The bilty PDF is what SAP receives on the service GRPO, so a swapped or
    deleted file must stay reconstructable afterwards: each row records both
    sides of the change, and the replaced file blobs are deliberately never
    removed from storage -- the ``old_file`` path in a row stays openable.
    """

    dispatch_plan = models.ForeignKey(
        DispatchPlan,
        on_delete=models.CASCADE,
        related_name="attachment_audits",
    )
    action = models.CharField(
        max_length=10,
        choices=DispatchPlanAttachmentAuditAction.choices,
    )
    source = models.CharField(
        max_length=20,
        choices=DispatchPlanAttachmentAuditSource.choices,
        default=DispatchPlanAttachmentAuditSource.MANUAL,
    )
    old_file = models.CharField(max_length=500, blank=True, default="")
    old_filename = models.CharField(max_length=255, blank=True, default="")
    new_file = models.CharField(max_length=500, blank=True, default="")
    new_filename = models.CharField(max_length=255, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    performed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_plan_attachment_audits",
    )
    performed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-performed_at", "-id"]
        indexes = [
            models.Index(fields=["dispatch_plan", "-performed_at"]),
        ]

    def __str__(self):
        return (
            f"{self.action} bilty attachment on plan {self.dispatch_plan_id} "
            f"({self.source})"
        )


class SelectedDispatchBill(BaseModel):
    """A SAP invoice (bill) chosen to appear on the dispatch Plan page.

    Company-wide (shared): the planning team curates which bills enter dispatch
    planning. The Plan page shows only bills that have an active selection here.
    Keyed like ``DispatchPlan`` on ``(company, sap_invoice_doc_entry)``; deselecting
    flips ``is_active`` off (keeps who/when for audit) rather than deleting.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="selected_dispatch_bills",
    )
    sap_invoice_doc_entry = models.IntegerField()
    sap_invoice_doc_num = models.CharField(max_length=30, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sap_invoice_doc_entry"],
                name="unique_selected_dispatch_bill_per_company",
            )
        ]
        indexes = [
            models.Index(fields=["company", "sap_invoice_doc_entry"]),
            models.Index(fields=["company", "is_active"]),
        ]

    def __str__(self):
        return f"{self.company.code} selected bill {self.sap_invoice_doc_num or self.sap_invoice_doc_entry}"


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
