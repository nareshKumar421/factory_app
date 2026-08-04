"""Goods Return (customer return) models.

A Goods Return records finished goods coming *back* from a customer. It is created
by a returns clerk (basis + documents + returning items + vehicle + an expected
arrival) and later marked in at the gate by a different user.

No-redundancy design: every shared entity is referenced, never copied --
``company``/``vehicle``/``driver`` are FKs to their masters, the gate-in event and
the truck's inside/outside state live on the shared ``driver_management.VehicleEntry``
ledger (linked by ``vehicle_entry``), and the source SAP invoice is referenced by its
doc-entry (``GoodsReturnInvoiceRef``) with header/lines re-read on demand via
``dispatch-plans/bills/by-number/``. The only genuinely new data stored on a return
line is the return quantity / reason / condition (absent from SAP); item identity
(code/name/uom/invoice qty) is snapshotted so the return can be displayed later
without a live SAP round-trip, mirroring ``SalesDispatchGateOutItem``.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class GoodsReturnBasis(models.TextChoices):
    INVOICE = "INVOICE", "Against Invoice"
    DEBIT_NOTE = "DEBIT_NOTE", "Against Debit Note"
    LETTER_PAD = "LETTER_PAD", "Against Letter Pad"


class GoodsReturnStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    AWAITING_ARRIVAL = "AWAITING_ARRIVAL", "Awaiting Arrival"
    ARRIVED = "ARRIVED", "Arrived"
    POSTED = "POSTED", "Posted to SAP"
    CANCELLED = "CANCELLED", "Cancelled"


class GoodsReturnItemCondition(models.TextChoices):
    GOOD = "GOOD", "Good"
    DAMAGED = "DAMAGED", "Damaged"
    EXPIRED = "EXPIRED", "Expired"
    OTHER = "OTHER", "Other"


class GoodsReturnAttachmentType(models.TextChoices):
    INVOICE_COPY = "INVOICE_COPY", "Invoice Copy"
    DEBIT_NOTE = "DEBIT_NOTE", "Debit Note"
    LETTER_PAD = "LETTER_PAD", "Letter Pad"
    OTHER = "OTHER", "Other"


class GoodsReturn(BaseModel):
    """Header for one customer goods return."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="goods_returns",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    basis = models.CharField(max_length=20, choices=GoodsReturnBasis.choices)
    status = models.CharField(
        max_length=20,
        choices=GoodsReturnStatus.choices,
        default=GoodsReturnStatus.DRAFT,
    )

    # Snapshot of the returning customer (populated from the invoice for INVOICE
    # basis, entered manually for DEBIT_NOTE / LETTER_PAD). Stored -- not re-read --
    # so list/gate views don't need a live SAP call per row, mirroring how
    # SalesDispatchGateOut persists customer_code/name.
    customer_code = models.CharField(max_length=100, blank=True)
    customer_name = models.CharField(max_length=255, blank=True)

    # Reference-only FKs to the shared masters (never copied).
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_returns",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_returns",
    )
    # Date only (no time) — the gate works arrivals at day granularity.
    expected_arrival_at = models.DateField(null=True, blank=True)

    # Set at gate mark-in. The gate-in event + inside/outside state live on the
    # shared ledger row, not here.
    vehicle_entry = models.OneToOneField(
        "driver_management.VehicleEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="goods_return",
    )
    gated_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="goods_returns_gated_in",
    )
    gated_in_at = models.DateTimeField(null=True, blank=True)

    # Set when the GR creator confirms receipt of the goods (after gate-in), which
    # triggers the SAP A/R Returns posting.
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="goods_returns_received",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    # SAP A/R Returns posting result + the goods-return warehouse the stock went into.
    sap_gr_doc_entry = models.IntegerField(null=True, blank=True)
    sap_gr_doc_num = models.CharField(max_length=50, blank=True)
    sap_return_warehouse = models.CharField(max_length=50, blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="goods_returns_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["status", "expected_arrival_at"]),
            models.Index(fields=["vehicle"]),
        ]
        permissions = [
            ("can_view_goods_return", "Can view goods return"),
            ("can_create_goods_return", "Can create goods return"),
            ("can_edit_goods_return", "Can edit goods return"),
            ("can_submit_goods_return", "Can submit goods return"),
            ("can_gate_in_goods_return", "Can gate in goods return"),
            ("can_receive_goods_return", "Can receive/post a goods return"),
        ]

    def __str__(self):
        return self.entry_no

    @property
    def active_invoice_refs(self):
        return [ref for ref in self.invoice_refs.all() if ref.is_active]

    @property
    def active_lines(self):
        return [line for line in self.lines.all() if line.is_active]

    @staticmethod
    def _next_number(prefix: str) -> int:
        last = (
            GoodsReturn.objects.filter(entry_no__startswith=prefix)
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
    def generate_entry_no(cls) -> str:
        today = timezone.now()
        prefix = f"GR-{today.strftime('%Y%m%d')}"
        return f"{prefix}-{cls._next_number(prefix):04d}"


class GoodsReturnInvoiceRef(BaseModel):
    """A source SAP invoice a return is booked against (INVOICE basis; multiple
    bills per return). Stores only SAP identity keys -- header/lines are re-read on
    demand via ``dispatch-plans/bills/by-number/``."""

    goods_return = models.ForeignKey(
        GoodsReturn,
        on_delete=models.CASCADE,
        related_name="invoice_refs",
    )
    sap_invoice_doc_entry = models.IntegerField()
    sap_invoice_doc_num = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["goods_return", "sap_invoice_doc_entry"],
                name="uniq_goodsreturn_invoice_ref",
            )
        ]
        indexes = [models.Index(fields=["sap_invoice_doc_entry"])]

    def __str__(self):
        return f"{self.goods_return.entry_no} - INV {self.sap_invoice_doc_num}"


class GoodsReturnItem(BaseModel):
    """A returning line. return_quantity / reason / condition are the only genuinely
    new (non-SAP) data; item identity + invoice quantity are snapshotted for display
    and over-return validation."""

    goods_return = models.ForeignKey(
        GoodsReturn,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    invoice_ref = models.ForeignKey(
        GoodsReturnInvoiceRef,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lines",
    )
    source_line_num = models.IntegerField(null=True, blank=True)
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=255, blank=True)
    uom = models.CharField(max_length=50, blank=True)
    # Quantity on the source invoice line (0 for manual DN/LP items). Captured once
    # to validate return_quantity without a live SAP re-fetch.
    invoice_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    # Snapshot of the invoice line price / tax so the SAP A/R Returns post needs no
    # live invoice re-read (0 / blank for manual DN/LP items).
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    tax_code = models.CharField(max_length=20, blank=True)
    return_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    reason = models.TextField(blank=True)
    condition = models.CharField(
        max_length=20,
        choices=GoodsReturnItemCondition.choices,
        default=GoodsReturnItemCondition.DAMAGED,
    )
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["source_line_num", "id"]
        indexes = [models.Index(fields=["goods_return"])]

    def __str__(self):
        return f"{self.goods_return.entry_no} - {self.item_code}"


class GoodsReturnAttachment(models.Model):
    """An uploaded document (invoice copy / debit note / letter pad / other)."""

    goods_return = models.ForeignKey(
        GoodsReturn,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    attachment_type = models.CharField(
        max_length=30,
        choices=GoodsReturnAttachmentType.choices,
        default=GoodsReturnAttachmentType.OTHER,
    )
    file = models.FileField(upload_to="goods_return/attachments/")
    original_filename = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="goods_return_attachments_uploaded",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        indexes = [models.Index(fields=["goods_return", "attachment_type"])]

    def __str__(self):
        return f"{self.goods_return.entry_no} - {self.attachment_type}"
