"""Cash sale out: a counter bill leaving the factory in the buyer's own hands.

A cash sale is billed at the factory and walks out with whoever bought it, on
foot or in their own vehicle. It never goes through docking — there is no
transporter, no booked truck, no gatepass — so it cannot be a
``SalesDispatchGateOut``, which is built around a vehicle entry, a vehicle and
a driver. This is the gate's whole record of it instead: which bills, who took
them, on what (if anything), and when.

Nothing is written to SAP. The counter's own SAP-raised cash sales carry their
dispatch stamp from the moment they are added, and the app's counter sales post
theirs through the bill summary (see ``ARInvoiceService._raise_bill_summary``),
so the stamp is already there by the time the goods reach the gate.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import BaseModel


class CashSaleGateOutStatus(models.TextChoices):
    OUT = "OUT", "Out"
    # Marked against the wrong bill or the wrong person: void, and the bills it
    # held can be marked out again.
    CANCELLED = "CANCELLED", "Cancelled"


class CashSaleGateOut(BaseModel):
    """One person (and their vehicle, if any) leaving with one or more cash bills.

    The bills of one entry are one company's: each company's book is its own
    SAP database, and the board tags every row with the company it came from.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="cash_sale_gate_outs",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    # Who carried the goods out — the buyer or someone sent by them.
    taken_by_name = models.CharField(max_length=100)
    taken_by_mobile = models.CharField(max_length=20, blank=True)
    # Blank when the goods went out by hand. Free text, not a Vehicle FK: a
    # buyer's own car or tempo is not in the vehicle master and never will be.
    vehicle_no = models.CharField(max_length=30, blank=True)
    remarks = models.TextField(blank=True)
    out_at = models.DateTimeField(default=timezone.now)

    status = models.CharField(
        max_length=20,
        choices=CashSaleGateOutStatus.choices,
        default=CashSaleGateOutStatus.OUT,
    )
    cancel_reason = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cash_sale_gate_outs_cancelled",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-out_at", "-id"]
        indexes = [
            models.Index(fields=["company", "out_at"]),
            models.Index(fields=["vehicle_no"]),
        ]
        # Marking out and cancelling ride on the Sales Dispatch Out rights
        # (dispatch / cancel), so the gate needs no new grants for this.
        default_permissions = ()

    def __str__(self):
        return self.entry_no

    @classmethod
    def generate_entry_no(cls):
        prefix = f"CASH-{timezone.localdate().strftime('%Y%m%d')}"
        last = (
            cls.objects.filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .values_list("entry_no", flat=True)
            .first()
        )
        try:
            next_number = int(last.split("-")[-1]) + 1 if last else 1
        except ValueError:
            next_number = 1
        return f"{prefix}-{next_number:04d}"


class CashSaleGateOutBill(models.Model):
    """One SAP cash-sale invoice on a cash sale out, as SAP held it when marked.

    Snapshotted rather than read live, so the board lists the day's exits
    without a SAP round-trip and still reads when SAP is down.
    """

    gate_out = models.ForeignKey(
        CashSaleGateOut,
        on_delete=models.CASCADE,
        related_name="bills",
    )
    # Repeated from the entry so the one-exit-per-bill constraint can see it.
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="cash_sale_gate_out_bills",
    )
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.IntegerField(null=True, blank=True)
    doc_date = models.DateField(null=True, blank=True)
    customer_code = models.CharField(max_length=50, blank=True)
    customer_name = models.CharField(max_length=200, blank=True)
    # OINV.NumAtCard — often the actual buyer's name on a counter bill.
    customer_ref = models.CharField(max_length=100, blank=True)
    doc_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    # "ITEM NAME x QTY; ..." — what the guard checks against the goods.
    item_summary = models.TextField(blank=True)
    total_quantity = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    # False once the entry is cancelled, which frees the bill to be marked again.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sap_doc_entry"],
                condition=Q(is_active=True),
                name="unique_active_cash_sale_gate_out_bill",
            )
        ]
        indexes = [
            models.Index(fields=["company", "sap_doc_entry"]),
            models.Index(fields=["sap_doc_num"]),
        ]

    def __str__(self):
        return f"{self.gate_out.entry_no} - {self.sap_doc_num or self.sap_doc_entry}"
