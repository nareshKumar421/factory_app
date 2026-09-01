"""The bill summary a warehouse manager hands to the floor.

**One summary per bill.** The operator is sent to fetch the goods for a named
A/R invoice, so the sheet is that invoice: its customer, its lines, and the
transport details that go with it. An earlier draft of this module grouped a
whole day's dispatch by warehouse; that is how SAP's saved query happens to be
written, but it is not how the work is handed out.

The flow it replaces is manual: the details are typed onto the invoice in SAP,
then a query is printed. Here the user searches the bill number, the app fills in
everything the dispatch module already knows, the user supplies whatever is
missing — in practice the bilty, which is raised once the truck is loaded and so
is not yet known — and the summary is posted to SAP.

SAP has a UDF literally labelled "Gate Pass No." (`U_TransporterInvoice`) that
has never once been filled, so there is no number to reconcile against; ours is
the first.

What SAP demands to accept the posting is documented in
`bill_summary_service`. It is not obvious and it was established by experiment.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from company.models import Company


class BillSummaryStatus(models.TextChoices):
    GENERATED = "GENERATED", "Generated"
    PICKED = "PICKED", "Picked"
    CANCELLED = "CANCELLED", "Cancelled"


class BillSummarySapStatus(models.TextChoices):
    """Separate from the sheet's own status on purpose.

    A summary can be in the operator's hands while SAP has not been stamped —
    the network was down, or SAP refused. Folding the two together hides the case
    that actually needs chasing.
    """

    NOT_POSTED = "NOT_POSTED", "Not posted to SAP"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "SAP refused"


class BillSummary(models.Model):
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="bill_summaries"
    )
    entry_no = models.CharField(max_length=30, unique=True, db_index=True)

    # The bill this sheet is for.
    sap_invoice_doc_entry = models.IntegerField(db_index=True)
    sap_invoice_doc_num = models.CharField(max_length=30, blank=True, default="")
    customer_code = models.CharField(max_length=50, blank=True, default="")
    customer_name = models.CharField(max_length=200, blank=True, default="")
    # Snapshotted for the printed sheet, which reproduces SAP's own Bill Summary
    # layout field for field.
    delivery_address = models.TextField(blank=True, default="")
    invoice_date = models.DateField(null=True, blank=True)
    bill_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    branch_name = models.CharField(max_length=100, blank=True, default="")
    branch_gstin = models.CharField(max_length=30, blank=True, default="")
    # The legal entity, read from SAP rather than assumed. The three companies
    # are not the same: Oil is JIVO WELLNESS PVT LTD, Mart is JIVO MART PVT LTD
    # with its own GST. Printing one name against the other's GST would be a
    # document nobody should hand to a driver.
    company_legal_name = models.CharField(max_length=200, blank=True, default="")
    # Usually one warehouse; comma-joined on the rare bill that spans two, so the
    # sheet can still say where to go without inventing a second document.
    warehouse_codes = models.CharField(max_length=200, blank=True, default="")

    # What gets written onto the SAP invoice. Prefilled from the dispatch plan
    # where the app already knows it.
    dispatch_date = models.DateField(db_index=True)
    bilty_no = models.CharField(max_length=50, blank=True, default="")
    bilty_date = models.DateField(null=True, blank=True)
    transporter_name = models.CharField(max_length=150, blank=True, default="")
    vehicle_no = models.CharField(max_length=30, blank=True, default="")
    driver_name = models.CharField(max_length=100, blank=True, default="")
    driver_mobile = models.CharField(max_length=20, blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=BillSummaryStatus.choices,
        default=BillSummaryStatus.GENERATED,
        db_index=True,
    )
    sap_status = models.CharField(
        max_length=20,
        choices=BillSummarySapStatus.choices,
        default=BillSummarySapStatus.NOT_POSTED,
    )
    sap_error = models.TextField(blank=True, default="")
    sap_posted_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True, default="")
    cancel_reason = models.TextField(blank=True, default="")

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_summaries_issued",
    )
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_summaries_picked",
    )
    issued_at = models.DateTimeField(default=timezone.now)
    picked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "dispatch_bill_summary"
        verbose_name = "bill summary"
        verbose_name_plural = "bill summaries"
        ordering = ["-dispatch_date", "-id"]
        indexes = [
            models.Index(fields=["company", "dispatch_date"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "sap_invoice_doc_entry"]),
        ]
        constraints = [
            # One live sheet per bill. A cancelled one does not block a re-issue,
            # which is the whole reason cancelling exists.
            models.UniqueConstraint(
                fields=["company", "sap_invoice_doc_entry"],
                condition=models.Q(is_active=True) & ~models.Q(status="CANCELLED"),
                name="uniq_live_bill_summary_per_invoice",
            ),
        ]
        permissions = [
            ("can_view_bill_summary", "Can view bill summaries"),
            ("can_create_bill_summary", "Can generate a bill summary"),
            ("can_pick_bill_summary", "Can confirm a bill summary as picked"),
            ("can_cancel_bill_summary", "Can cancel a bill summary"),
        ]

    def __str__(self) -> str:
        return f"{self.entry_no} (bill {self.sap_invoice_doc_num})"

    @classmethod
    def generate_entry_no(cls, dispatch_date) -> str:
        """`BS-YYYYMMDD-NNN`, sequential within the dispatch date.

        Dated by the DISPATCH date, not today: that is the number the floor will
        be talking about.
        """
        stamp = dispatch_date.strftime("%Y%m%d")
        prefix = f"BS-{stamp}-"
        last = (
            cls.objects.filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .values_list("entry_no", flat=True)
            .first()
        )
        nxt = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{nxt:03d}"

    @property
    def active_lines(self):
        return self.lines.filter(is_active=True)

    def totals(self) -> dict:
        lines = list(self.active_lines)
        return {
            "lines": len(lines),
            "boxes": sum((line.boxes or Decimal("0")) for line in lines),
            "litres": sum((line.litres or Decimal("0")) for line in lines),
            "invoice_qty": sum((line.invoice_qty or Decimal("0")) for line in lines),
            "dispatch_qty": sum((line.dispatch_qty or Decimal("0")) for line in lines),
            "loose_qty": sum((line.loose_qty or Decimal("0")) for line in lines),
            "gross_weight": sum((line.gross_weight or Decimal("0")) for line in lines),
        }


class BillSummaryLine(models.Model):
    """One line of the bill, snapshotted when the sheet was generated.

    A snapshot rather than a live join: what was handed to the floor must stay
    readable even if the invoice is later amended, because the question asked
    afterwards is "what did we tell them to fetch".
    """

    summary = models.ForeignKey(
        BillSummary, on_delete=models.CASCADE, related_name="lines"
    )

    sap_line_num = models.IntegerField()
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True, default="")
    uom = models.CharField(max_length=20, blank=True, default="")
    warehouse_code = models.CharField(max_length=50, blank=True, default="")

    invoice_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    # Snapshotted, not recomputed: pieces-per-box comes from OITM.SalFactor2,
    # which master data edits, and a sheet printed in September should still foot
    # up the same in November.
    pcs_per_box = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    # FULL boxes and the leftover pieces, split the way SAP's own bill does it
    # (`gate_core.services.box_packing.split_line`). Not a bare
    # quantity/pieces-per-box: an item with SalFactor2 = 1 is not boxed at all, so
    # it is all loose, and a part case is loose pieces rather than a fraction of
    # a box. Printing "0.25 box" would send a picker looking for a quarter carton.
    boxes = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    loose_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    litres = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    gross_weight = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    # What goes to SAP as `INV1.U_Disp_Qty`. Defaults to the full billed quantity
    # because that is what all but a handful of lines do.
    dispatch_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatch_bill_summary_line"
        ordering = ["sap_line_num"]
        constraints = [
            models.UniqueConstraint(
                fields=["summary", "sap_line_num"],
                name="uniq_bill_summary_line_num",
            ),
        ]

    def __str__(self) -> str:
        return f"line {self.sap_line_num} {self.item_code}"

    @property
    def is_short(self) -> bool:
        return (self.dispatch_qty or 0) < (self.invoice_qty or 0)
