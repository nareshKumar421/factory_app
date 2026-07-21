import json
import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .base import BaseModel


class SalesDispatchDocumentType(models.TextChoices):
    INVOICE = "INVOICE", "A/R Invoice"
    STOCK_TRANSFER = "STOCK_TRANSFER", "Stock Transfer"


class SalesDispatchGateOutStatus(models.TextChoices):
    DOCKED = "DOCKED", "Docked"
    PHOTO_ATTACHED = "PHOTO_ATTACHED", "Photo Attached"
    READY_FOR_GATEPASS = "READY_FOR_GATEPASS", "Ready For Gatepass"
    GATEPASS_PRINTED = "GATEPASS_PRINTED", "Gatepass Printed"
    PRINT_COMMITTED = "PRINT_COMMITTED", "Print Committed"
    DISPATCHED = "DISPATCHED", "Dispatched"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class SalesDispatchAttachmentType(models.TextChoices):
    TRUCK_PHOTO = "TRUCK_PHOTO", "Truck Photo"
    GATEPASS = "GATEPASS", "Gatepass"
    INVOICE_COPY = "INVOICE_COPY", "Invoice Copy"
    DELIVERY_NOTE = "DELIVERY_NOTE", "Delivery Note"
    BILTY = "BILTY", "Bilty"
    EWAY_BILL = "EWAY_BILL", "E-Way Bill"
    CREDIT_NOTE = "CREDIT_NOTE", "Credit Note"
    OTHER = "OTHER", "Other"


class PartialDispatchApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class SalesDispatchGatepassPrintType(models.TextChoices):
    ORIGINAL = "ORIGINAL", "Original"
    REPRINT = "REPRINT", "Reprint"


ACTIVE_DOCUMENT_STATUSES = [
    SalesDispatchGateOutStatus.DOCKED,
    SalesDispatchGateOutStatus.PHOTO_ATTACHED,
    SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
    SalesDispatchGateOutStatus.GATEPASS_PRINTED,
    SalesDispatchGateOutStatus.PRINT_COMMITTED,
    SalesDispatchGateOutStatus.DISPATCHED,
]


class SalesDispatchGatepassSequence(models.Model):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gatepass_sequences",
    )
    financial_year = models.CharField(max_length=9)
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "financial_year"],
                name="unique_sales_dispatch_gatepass_sequence",
            )
        ]

    @classmethod
    def next_gatepass_no(cls, company):
        today = timezone.localdate()
        start_year = today.year if today.month >= 4 else today.year - 1
        financial_year = f"{start_year}-{str(start_year + 1)[-2:]}"
        with transaction.atomic():
            sequence, _ = (
                cls.objects.select_for_update().get_or_create(
                    company=company,
                    financial_year=financial_year,
                    defaults={"last_number": 0},
                )
            )
            sequence.last_number += 1
            sequence.save(update_fields=["last_number", "updated_at"])
            return f"DCK/{company.code}/{financial_year}/{sequence.last_number:06d}"


class SalesDispatchLock(BaseModel):
    """Company-level hold for Docking gatepass printing."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_locks",
    )
    is_locked = models.BooleanField(default=False)
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_locks_changed",
    )
    changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                name="unique_sales_dispatch_lock_company",
            )
        ]
        permissions = [
            ("can_manage_sales_dispatch_lock", "Can manage sales dispatch lock"),
        ]

    def __str__(self):
        state = "locked" if self.is_locked else "unlocked"
        return f"{self.company} Docking {state}"

    @classmethod
    def for_company(cls, company):
        lock, _ = cls.objects.get_or_create(company=company)
        return lock


class SalesDispatchGateOut(BaseModel):
    """Docking gate-out record for finished-goods invoice or stock-transfer dispatch."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gate_outs",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    vehicle_entry = models.ForeignKey(
        "driver_management.VehicleEntry",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gate_outs",
    )
    dispatch_plan = models.ForeignKey(
        "dispatch_plans.DispatchPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_dispatch_gate_outs",
    )
    # The cross-company physical truck trip this docking belongs to (null for
    # legacy / single-company dockings).
    arrival = models.ForeignKey(
        "gate_core.VehicleArrival",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gate_outs",
    )
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gate_outs",
    )
    transporter = models.ForeignKey(
        "vehicle_management.Transporter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_dispatch_gate_outs",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gate_outs",
    )

    document_type = models.CharField(
        max_length=30,
        choices=SalesDispatchDocumentType.choices,
    )
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.TextField(blank=True)
    sap_doc_date = models.DateField(null=True, blank=True)
    sap_doc_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    sap_branch_id = models.IntegerField(null=True, blank=True)
    sap_branch_name = models.CharField(max_length=150, blank=True)
    sap_reference = models.TextField(blank=True)
    sap_comments = models.TextField(blank=True)

    customer_code = models.TextField(blank=True)
    customer_name = models.TextField(blank=True)
    ship_to_code = models.CharField(max_length=100, blank=True)
    ship_to_address = models.TextField(blank=True)
    place_of_supply = models.CharField(max_length=150, blank=True)
    bp_gstin = models.CharField(max_length=30, blank=True)
    eway_bill = models.TextField(blank=True)

    from_warehouse = models.CharField(max_length=50, blank=True)
    to_warehouse = models.CharField(max_length=50, blank=True)
    warehouses = models.TextField(blank=True)
    item_summary = models.TextField(blank=True)
    base_refs = models.TextField(blank=True)
    total_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_litres = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_boxes = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    # Operator-entered challan/delivery weight. Used as the comparison reference for the
    # net loaded weight when the SAP document weight (total_weight) is missing or wrong.
    # Never overwrites total_weight, which stays the SAP source-of-truth.
    challan_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    challan_weight_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_challan_weights_set",
    )
    challan_weight_at = models.DateTimeField(null=True, blank=True)

    vehicle_no = models.CharField(max_length=30, blank=True)
    transporter_name = models.CharField(max_length=150, blank=True)
    transporter_gstin = models.CharField(max_length=20, blank=True)
    transporter_contact_person = models.CharField(max_length=100, blank=True)
    transporter_mobile_no = models.CharField(max_length=50, blank=True)
    driver_name = models.CharField(max_length=100, blank=True)
    driver_mobile_no = models.CharField(max_length=50, blank=True)
    driver_license_no = models.CharField(max_length=50, blank=True)
    driver_id_proof_type = models.CharField(max_length=50, blank=True)
    driver_id_proof_number = models.CharField(max_length=50, blank=True)

    bilty_no = models.CharField(max_length=50, blank=True)
    bilty_date = models.DateField(null=True, blank=True)
    freight = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_freight = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    dock_incharge = models.CharField(max_length=100, blank=True)
    docked_at = models.DateTimeField(default=timezone.now)

    gate_out_date = models.DateField(null=True, blank=True)
    out_time = models.TimeField(null=True, blank=True)
    security_name = models.CharField(max_length=100, blank=True)

    truck_photo = models.FileField(upload_to="sales_dispatch/truck_photos/", null=True, blank=True)
    photo_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    photo_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    photo_uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_truck_photos_uploaded",
    )
    photo_uploaded_at = models.DateTimeField(null=True, blank=True)

    gatepass_no = models.CharField(max_length=80, unique=True, null=True, blank=True)
    random_code = models.CharField(max_length=50, blank=True)
    qr_payload = models.TextField(blank=True)
    uom = models.CharField(max_length=50, blank=True)
    physical_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    seal_number = models.CharField(max_length=100, blank=True)
    pgi_reference = models.CharField(max_length=100, blank=True)
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_gatepasses_printed",
    )
    printed_at = models.DateTimeField(null=True, blank=True)
    print_committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_prints_committed",
    )
    print_committed_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatches_dispatched",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=30,
        choices=SalesDispatchGateOutStatus.choices,
        default=SalesDispatchGateOutStatus.DOCKED,
    )
    remarks = models.TextField(blank=True)
    reject_reason = models.TextField(blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatches_rejected",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatches_cancelled",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "document_type", "sap_doc_entry"],
                condition=Q(is_active=True, status__in=ACTIVE_DOCUMENT_STATUSES),
                name="unique_active_sales_dispatch_document",
            )
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "document_type", "sap_doc_entry"]),
            models.Index(fields=["company", "vehicle_entry"]),
            models.Index(fields=["company", "gatepass_no"]),
            models.Index(fields=["vehicle_no"]),
            models.Index(fields=["sap_doc_num"]),
        ]
        permissions = [
            ("can_view_sales_dispatch_out", "Can view sales dispatch out"),
            ("can_create_sales_dispatch_out", "Can create sales dispatch out"),
            ("can_edit_sales_dispatch_out", "Can edit sales dispatch out"),
            ("can_upload_sales_dispatch_photo", "Can upload sales dispatch truck photo"),
            ("can_print_sales_dispatch_gatepass", "Can print sales dispatch gatepass"),
            ("can_reprint_sales_dispatch_gatepass", "Can reprint sales dispatch gatepass"),
            ("can_commit_sales_dispatch_print", "Can commit sales dispatch print"),
            ("can_reject_sales_dispatch_out", "Can reject sales dispatch out"),
            ("can_cancel_sales_dispatch_out", "Can cancel sales dispatch out"),
            ("can_dispatch_sales_dispatch_out", "Can mark sales dispatch out as dispatched"),
            ("can_view_sales_dispatch_reports", "Can view sales dispatch reports"),
        ]

    def __str__(self):
        return self.entry_no

    @property
    def active_documents(self):
        """Active child documents, filtered from the prefetch cache (no extra query).

        Readers must use this rather than ``documents.all()``: a removed bill's
        document is deactivated (``is_active=False``), not deleted, so an unfiltered
        read still counts it into box totals / e-way / the invoice list.
        """
        return [document for document in self.documents.all() if document.is_active]

    @property
    def active_items(self):
        """Active line items, filtered from the prefetch cache (no extra query)."""
        return [item for item in self.items.all() if item.is_active]

    @staticmethod
    def _next_number(prefix: str, model_cls):
        last = (
            model_cls.objects
            .filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )
        if not last:
            return 1
        try:
            return int(last.entry_no.split("-")[-1]) + 1
        except ValueError:
            return 1

    @classmethod
    def generate_entry_no(cls):
        today = timezone.now()
        prefix = f"DOCK-{today.strftime('%Y%m%d')}"
        return f"{prefix}-{cls._next_number(prefix, cls):04d}"

    @classmethod
    def generate_vehicle_entry_no(cls):
        from driver_management.models import VehicleEntry

        today = timezone.now()
        prefix = f"DOCKV-{today.strftime('%Y%m%d')}"
        last = (
            VehicleEntry.objects
            .filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )
        if not last:
            next_number = 1
        else:
            try:
                next_number = int(last.entry_no.split("-")[-1]) + 1
            except ValueError:
                next_number = 1
        return f"{prefix}-{next_number:04d}"

    def build_qr_payload(self):
        documents = []
        if self.pk:
            documents = [
                {
                    "document_type": document.document_type,
                    "sap_doc_entry": document.sap_doc_entry,
                    "sap_doc_num": document.sap_doc_num,
                }
                for document in sorted(self.active_documents, key=lambda d: d.id)
            ]
        if not documents:
            documents = [
                {
                    "document_type": self.document_type,
                    "sap_doc_entry": self.sap_doc_entry,
                    "sap_doc_num": self.sap_doc_num,
                }
            ]
        return json.dumps(
            {
                "entry_no": self.entry_no,
                "gatepass_no": self.gatepass_no,
                "document_type": self.document_type,
                "sap_doc_entry": self.sap_doc_entry,
                "sap_doc_num": self.sap_doc_num,
                "documents": documents,
                "vehicle_no": self.vehicle_no,
                "random_code": self.random_code,
            },
            separators=(",", ":"),
        )

    def assign_gatepass(self, user):
        if not self.gatepass_no:
            self.gatepass_no = SalesDispatchGatepassSequence.next_gatepass_no(self.company)
        if not self.random_code:
            self.random_code = secrets.token_urlsafe(9)
        self.qr_payload = self.build_qr_payload()
        self.status = SalesDispatchGateOutStatus.GATEPASS_PRINTED
        self.printed_by = user
        self.printed_at = timezone.now()
        self.updated_by = user
        self.save(
            update_fields=[
                "gatepass_no",
                "random_code",
                "qr_payload",
                "status",
                "printed_by",
                "printed_at",
                "updated_by",
                "updated_at",
            ]
        )


class SalesDispatchGatepassPrintLog(BaseModel):
    """Append-only audit trail for Docking gatepass prints and reprints."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gatepass_print_logs",
    )
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="gatepass_print_logs",
    )
    gatepass_no = models.CharField(max_length=80)
    entry_status = models.CharField(max_length=30, blank=True)
    copy_number = models.PositiveIntegerField(default=1)
    print_type = models.CharField(
        max_length=20,
        choices=SalesDispatchGatepassPrintType.choices,
        default=SalesDispatchGatepassPrintType.ORIGINAL,
    )
    reprint_reason = models.TextField(blank=True, default="")
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_gatepass_print_logs",
    )
    printed_at = models.DateTimeField(auto_now_add=True)
    printer_name = models.CharField(max_length=100, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-printed_at", "-id"]
        indexes = [
            models.Index(fields=["company", "print_type", "printed_at"]),
            models.Index(fields=["sales_dispatch", "print_type"]),
            models.Index(fields=["gatepass_no"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch"],
                condition=Q(print_type=SalesDispatchGatepassPrintType.ORIGINAL),
                name="unique_original_sales_dispatch_gatepass_print",
            ),
            models.UniqueConstraint(
                fields=["sales_dispatch", "copy_number"],
                name="unique_sales_dispatch_gatepass_copy_number",
            ),
        ]

    def __str__(self):
        return f"{self.gatepass_no} {self.print_type} #{self.copy_number}"

    @classmethod
    def next_copy_number(cls, sales_dispatch):
        last_log = (
            cls.objects
            .filter(sales_dispatch=sales_dispatch)
            .order_by("-copy_number")
            .first()
        )
        return (last_log.copy_number + 1) if last_log else 1

    @classmethod
    def record_print(
        cls,
        *,
        sales_dispatch,
        print_type,
        user,
        reprint_reason="",
        printer_name="",
        ip_address=None,
        user_agent="",
    ):
        return cls.objects.create(
            company=sales_dispatch.company,
            sales_dispatch=sales_dispatch,
            gatepass_no=sales_dispatch.gatepass_no or "",
            entry_status=sales_dispatch.status,
            copy_number=cls.next_copy_number(sales_dispatch),
            print_type=print_type,
            reprint_reason=reprint_reason,
            printed_by=user,
            printer_name=printer_name or "",
            ip_address=ip_address or None,
            user_agent=(user_agent or "")[:500],
            created_by=user,
            updated_by=user,
        )


class SalesDispatchGateOutDocument(BaseModel):
    """SAP document carried by a Docking truck/load."""

    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_gate_out_documents",
    )
    dispatch_plan = models.ForeignKey(
        "dispatch_plans.DispatchPlan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_dispatch_gate_out_documents",
    )
    document_type = models.CharField(
        max_length=30,
        choices=SalesDispatchDocumentType.choices,
    )
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.TextField(blank=True)
    sap_doc_date = models.DateField(null=True, blank=True)
    sap_doc_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    sap_branch_id = models.IntegerField(null=True, blank=True)
    sap_branch_name = models.CharField(max_length=150, blank=True)
    sap_reference = models.TextField(blank=True)
    sap_comments = models.TextField(blank=True)

    customer_code = models.TextField(blank=True)
    customer_name = models.TextField(blank=True)
    ship_to_code = models.CharField(max_length=100, blank=True)
    ship_to_address = models.TextField(blank=True)
    place_of_supply = models.CharField(max_length=150, blank=True)
    bp_gstin = models.CharField(max_length=30, blank=True)
    eway_bill = models.TextField(blank=True)

    from_warehouse = models.CharField(max_length=50, blank=True)
    to_warehouse = models.CharField(max_length=50, blank=True)
    warehouses = models.TextField(blank=True)
    item_summary = models.TextField(blank=True)
    base_refs = models.TextField(blank=True)
    total_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_litres = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_boxes = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["company", "document_type", "sap_doc_entry"]),
            models.Index(fields=["sales_dispatch", "document_type"]),
            models.Index(fields=["sap_doc_num"]),
            models.Index(fields=["customer_name"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch", "document_type", "sap_doc_entry"],
                name="unique_sales_dispatch_child_document",
            )
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} - {self.sap_doc_num or self.sap_doc_entry}"

    @property
    def active_items(self):
        """Active line items on this document, from the prefetch cache (no extra query)."""
        return [item for item in self.items.all() if item.is_active]


class SalesDispatchGateOutItem(BaseModel):
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="items",
    )
    document = models.ForeignKey(
        SalesDispatchGateOutDocument,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="items",
    )
    line_num = models.IntegerField()
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    # Quantity actually shipped (partial dispatch). Null = full quantity ships;
    # a value < quantity means the shortfall is held and credited.
    dispatched_quantity = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    uom = models.CharField(max_length=50, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    line_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    gross_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    warehouse_code = models.CharField(max_length=50, blank=True)
    from_warehouse = models.CharField(max_length=50, blank=True)
    to_warehouse = models.CharField(max_length=50, blank=True)
    base_ref = models.CharField(max_length=100, blank=True)
    base_entry = models.IntegerField(null=True, blank=True)
    base_type = models.IntegerField(null=True, blank=True)
    tax_code = models.CharField(max_length=50, blank=True)
    total_litres = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_boxes = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    total_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["line_num"]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch", "line_num"],
                name="unique_sales_dispatch_line",
            )
        ]
        indexes = [
            models.Index(fields=["sales_dispatch", "document", "line_num"]),
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} - {self.item_code}"


class SalesDispatchBoxScan(BaseModel):
    """Box-level scan captured during Docking before gatepass preparation."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_box_scans",
    )
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="box_scans",
    )
    # The specific bill (SAP document) this scanned box is dispatched against.
    # A docking can carry several bills that share the same item, so a scan must
    # resolve to the one bill it belongs to instead of every bill with that item.
    # Nullable: legacy scans and boxes for an item no bill on the load invoices
    # stay unattributed (shown as "outside list").
    document = models.ForeignKey(
        SalesDispatchGateOutDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="box_scans",
    )
    box = models.ForeignKey(
        "barcode.Box",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_dispatch_scans",
    )
    scan_log = models.ForeignKey(
        "barcode.ScanLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_dispatch_box_scans",
    )
    box_barcode = models.CharField(max_length=100)
    barcode_raw = models.CharField(max_length=500, blank=True)
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=255, blank=True)
    batch_number = models.CharField(max_length=100, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)
    net_weight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    box_status = models.CharField(max_length=20, blank=True)
    warehouse_code = models.CharField(max_length=50, blank=True)
    pallet_code = models.CharField(max_length=100, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_box_scans",
    )
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-scanned_at", "-id"]
        indexes = [
            models.Index(fields=["company", "sales_dispatch"]),
            models.Index(fields=["sales_dispatch", "document"]),
            models.Index(fields=["box_barcode"]),
            models.Index(fields=["scanned_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch", "box_barcode"],
                name="unique_sales_dispatch_box_scan",
            )
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} - {self.box_barcode}"


class SalesDispatchAttachment(models.Model):
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    attachment_type = models.CharField(
        max_length=30,
        choices=SalesDispatchAttachmentType.choices,
        default=SalesDispatchAttachmentType.OTHER,
    )
    file = models.FileField(upload_to="sales_dispatch/attachments/")
    original_filename = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_dispatch_attachments_uploaded",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        indexes = [
            models.Index(fields=["sales_dispatch", "attachment_type"]),
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} - {self.attachment_type}"

    @property
    def has_geolocation(self):
        return self.latitude is not None and self.longitude is not None


class SalesDispatchAdditionalWeight(BaseModel):
    """A named, operator-entered weight of non-goods items loaded on the truck
    (packaging, cardboard, dunnage, securing material).

    Recorded separately so the gate user can subtract the total of these from the
    net loaded weight (gross - tare) to estimate the actual goods weight and
    reconcile it against the invoice/challan weight. This never touches the
    weighbridge weighment or the gross/net figures.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sales_dispatch_additional_weights",
    )
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="additional_weights",
    )
    name = models.CharField(max_length=150)
    weight = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["company", "sales_dispatch"]),
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} - {self.name} ({self.weight})"


class PartialDispatchApproval(BaseModel):
    """Authorisation to ship a docking bill short (some items held and credited).

    Created when an operator marks items held on a docking document. Gatepass
    printing is blocked until this is APPROVED and a credit note is recorded.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="partial_dispatch_approvals",
    )
    sales_dispatch = models.ForeignKey(
        SalesDispatchGateOut,
        on_delete=models.CASCADE,
        related_name="partial_approvals",
    )
    document = models.ForeignKey(
        SalesDispatchGateOutDocument,
        on_delete=models.CASCADE,
        related_name="partial_approvals",
    )
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=PartialDispatchApprovalStatus.choices,
        default=PartialDispatchApprovalStatus.PENDING,
    )
    credit_note_no = models.CharField(max_length=100, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="partial_dispatch_approvals_requested",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="partial_dispatch_approvals_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(is_active=True),
                name="unique_active_partial_dispatch_approval",
            )
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["sales_dispatch"]),
        ]
        permissions = [
            ("can_approve_partial_sales_dispatch", "Can approve partial sales dispatch"),
        ]

    def __str__(self):
        return f"{self.sales_dispatch.entry_no} doc {self.document_id} ({self.status})"


def decimal_or_none(value, places="0.001"):
    if value in (None, ""):
        return None
    return Decimal(str(value)).quantize(Decimal(places))
