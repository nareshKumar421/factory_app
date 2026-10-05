"""The bill summary the dispatch desk raises and the warehouse approves.

**One summary per bill.** The operator is sent to fetch the goods for a named
A/R invoice, so the sheet is that invoice: its customer, its lines, and the
transport details that go with it. An earlier draft of this module grouped a
whole day's dispatch by warehouse; that is how SAP's saved query happens to be
written, but it is not how the work is handed out.

**Two desks, and the paper goes down last.** Dispatch fills the sheet in and
sends it to the warehouse. The warehouse sets the dispatch date and approves —
that is the whole of its job, and it is the moment the invoice is stamped in
SAP. Dispatch then prints the approved sheet, signs it, and walks the physical
copy down to the godown, which picks against it. A sheet the warehouse will not
take is sent back with a reason for dispatch to fix and re-send.

Most sheets are never typed one at a time. Linking a vehicle to its bills is the
moment the dispatch desk knows the load, so that is where the sheets are raised:
one button submits the whole truck.

The flow all this replaces is manual: the details are typed onto the invoice in
SAP, then a query is printed. Here the app fills in everything the dispatch
module already knows, the user supplies whatever is missing — in practice the
bilty, which is raised once the truck is loaded — and the approved summary is
posted to SAP.

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


# Where a row on the bill-summary screen came from. The screen lists the app's
# own sheets alongside dispatches somebody stamped onto the invoice in SAP
# without one; those have no record behind them, so every row says which it is.
APP_SOURCE = "APP"
SAP_SOURCE = "SAP"


class BillSummaryStatus(models.TextChoices):
    """The sheet's progress across two desks and down to the godown.

    Dispatch fills the sheet in and sends it over; the warehouse sets the
    dispatch date and approves; dispatch prints the approved sheet, signs it and
    walks the paper down to the godown, which confirms the pick. The dispatch
    date is the warehouse's to give, which is why the sheet does not carry one
    before ``APPROVED`` - and why nothing is written to SAP before then either.
    """

    PENDING_APPROVAL = "PENDING_APPROVAL", "With the warehouse"
    REJECTED = "REJECTED", "Sent back"
    APPROVED = "APPROVED", "Approved"
    PRINTED = "PRINTED", "Printed"
    PICKED = "PICKED", "Picked"
    CANCELLED = "CANCELLED", "Cancelled"


#: Still the dispatch desk's to change: the warehouse has either not looked yet
#: or has handed it back. Both are editable and re-submittable.
BILL_SUMMARY_EDITABLE = (
    BillSummaryStatus.PENDING_APPROVAL,
    BillSummaryStatus.REJECTED,
)


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
    #
    # The dispatch date is the ONE field the dispatch desk does not fill: the
    # warehouse gives it when it approves, and it is null until then. Everything
    # downstream has to cope with that - a sheet with the warehouse is a real
    # sheet that simply has no date yet.
    dispatch_date = models.DateField(null=True, blank=True, db_index=True)
    bilty_no = models.CharField(max_length=50, blank=True, default="")
    bilty_date = models.DateField(null=True, blank=True)
    transporter_name = models.CharField(max_length=150, blank=True, default="")
    vehicle_no = models.CharField(max_length=30, blank=True, default="")
    driver_name = models.CharField(max_length=100, blank=True, default="")
    driver_mobile = models.CharField(max_length=20, blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=BillSummaryStatus.choices,
        default=BillSummaryStatus.PENDING_APPROVAL,
        db_index=True,
    )
    sap_status = models.CharField(
        max_length=20,
        choices=BillSummarySapStatus.choices,
        default=BillSummarySapStatus.NOT_POSTED,
    )
    sap_error = models.TextField(blank=True, default="")
    # Not an error: the posting worked, but SAP would not take part of the stamp
    # because it already held one (bilty, vehicle and the rest are write-once
    # there). The sheet in the operator's hands then differs from the invoice,
    # which is worth saying out loud.
    sap_note = models.TextField(blank=True, default="")
    sap_posted_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True, default="")
    cancel_reason = models.TextField(blank=True, default="")
    # Why the warehouse handed it back. Kept after a re-submission rather than
    # cleared: the dispatch desk is reading it while it fixes the sheet, and
    # "what was wrong last time" is the question asked when it comes back again.
    reject_reason = models.TextField(blank=True, default="")

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
    # Who gave the dispatch date. Separate from `issued_by` on purpose: the whole
    # point of the step is that the desk that fills the sheet is not the desk
    # that dates it.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_summaries_approved",
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_summaries_rejected",
    )
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_summaries_printed",
    )
    issued_at = models.DateTimeField(default=timezone.now)
    # When it was last sent to the warehouse. Moves on every re-submission, which
    # is what the approval queue sorts on - a sheet sent back and fixed this
    # morning should not sit where it was raised last week.
    submitted_at = models.DateTimeField(default=timezone.now, db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    # The first print, not the last. A reprint is a reprint; the question the
    # record answers is when the signed copy went down to the godown.
    printed_at = models.DateTimeField(null=True, blank=True)
    picked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "dispatch_bill_summary"
        verbose_name = "bill summary"
        verbose_name_plural = "bill summaries"
        # Nulls first, said out loud rather than left to the database: a sheet
        # waiting on the warehouse has no dispatch date, it is the one somebody
        # opening this screen has to act on, and which end of the list it lands
        # on otherwise depends on which database is answering.
        ordering = [models.F("dispatch_date").desc(nulls_first=True), "-id"]
        indexes = [
            models.Index(fields=["company", "dispatch_date"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "sap_invoice_doc_entry"]),
        ]
        constraints = [
            # One live sheet per bill. A cancelled one does not block a re-issue,
            # which is the whole reason cancelling exists — but a rejected one
            # does: it is the same sheet, in the dispatch desk's hands to correct
            # and send back, not a dead one to be raised around.
            models.UniqueConstraint(
                fields=["company", "sap_invoice_doc_entry"],
                condition=models.Q(is_active=True) & ~models.Q(status="CANCELLED"),
                name="uniq_live_bill_summary_per_invoice",
            ),
        ]
        permissions = [
            ("can_view_bill_summary", "Can view bill summaries"),
            ("can_create_bill_summary", "Can generate a bill summary"),
            (
                "can_approve_bill_summary",
                "Can set the dispatch date and approve a bill summary",
            ),
            ("can_pick_bill_summary", "Can confirm a bill summary as picked"),
            ("can_cancel_bill_summary", "Can cancel a bill summary"),
        ]

    def __str__(self) -> str:
        return f"{self.entry_no} (bill {self.sap_invoice_doc_num})"

    @classmethod
    def generate_entry_no(cls, raised_on=None) -> str:
        """`BS-YYYYMMDD-NNN`, sequential within the day the sheet was raised.

        It used to be dated by the DISPATCH date, on the reasoning that this is
        the number the floor would be talking about. That no longer works: the
        dispatch date is the warehouse's to give at approval, and a sheet needs
        its number the moment it is raised so the dispatch desk can refer to it
        while it is still waiting. Numbering by the day it was raised also keeps
        a number from moving when a sheet is sent back and approved for a
        different date.
        """
        stamp = (raised_on or timezone.localdate()).strftime("%Y%m%d")
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

    @property
    def is_editable(self) -> bool:
        """Still the dispatch desk's to change and re-send."""
        return self.status in BILL_SUMMARY_EDITABLE

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
