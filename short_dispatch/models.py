"""Short Dispatch models -- the return note for stock a posted bill claims left.

A bill summary posted into SAP is read as the stock having gone: the invoice is
raised, the inventory is relieved, and as far as SAP is concerned the goods are
on the truck. When a line is then found **short** -- the pallet was not there,
the loader could not fit it, the item was pulled off the load -- the goods are
still standing in the warehouse while SAP says they are not. An A/R Return puts
them back.

That is the same SAP document the customer-returns module posts, and none of the
story around it is the same, which is why this is its own module rather than a
basis on ``goods_return.GoodsReturn``:

* **Nothing travels.** There is no vehicle, no driver, no gate arrival and no
  mark-in -- the stock never left the floor. A short dispatch is keyed and posted
  by one person in one sitting.
* **Nothing is approved.** A customer return can arrive "on approval" and wait
  for an admin; a short dispatch is a correction of the app's own paperwork and
  posts on the spot.
* **It goes back where it came from.** A customer return lands in a ``-GR``
  warehouse because damaged goods need looking at. Short stock never moved, so it
  returns into the warehouse the invoice billed it out of (editable, because a
  bill can be picked across floors).
* **Always one invoice.** The shortfall belongs to the bill that claimed it, so
  one entry is one invoice and one SAP Return -- no basis to choose, no bill list
  to build.

No-redundancy design, as elsewhere: the invoice is referenced by its doc-entry
and re-read from SAP on demand; only what SAP cannot tell us later (the short
quantity, the reason, the batch that was actually billed) is stored, alongside a
per-line snapshot of item identity so a posted entry displays without a live SAP
round-trip.

A record only exists once SAP has taken the document: posting is part of the
single form, and a refusal rolls the whole thing back rather than leaving a draft
nobody will come back to (see ``services.ShortDispatchService.create_and_post``).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class ShortDispatchReason(models.TextChoices):
    """Why the line did not go. Reported, not enforced -- every one of them ends
    in the same document."""

    SHORT = "SHORT", "Short in stock"
    NOT_LOADED = "NOT_LOADED", "Not loaded on the vehicle"
    DAMAGED = "DAMAGED", "Damaged before loading"
    CUSTOMER_REFUSED = "CUSTOMER_REFUSED", "Pulled off at customer's request"
    OTHER = "OTHER", "Other"


class ShortDispatch(BaseModel):
    """One posted return note against one invoice's short lines."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="short_dispatches",
    )
    entry_no = models.CharField(max_length=50, unique=True)

    # The bill that claimed the stock. Referenced by doc-entry; header and lines
    # are re-read from SAP when the form needs them.
    sap_invoice_doc_entry = models.IntegerField()
    sap_invoice_doc_num = models.CharField(max_length=50, blank=True)

    # Snapshot of the customer the invoice was raised on, so the list does not
    # need a live SAP call per row. The A/R Return posts against this code.
    customer_code = models.CharField(max_length=100, blank=True)
    customer_name = models.CharField(max_length=255, blank=True)

    # Where the stock goes back. Defaulted from the invoice's own lines and
    # editable, because one bill can be picked across more than one floor.
    warehouse_code = models.CharField(max_length=50)

    # The A/R Return SAP took. Never blank in practice -- a row is only committed
    # once SAP has accepted the document -- but nullable so a future retry flow
    # has somewhere to start from.
    sap_return_doc_entry = models.IntegerField(null=True, blank=True)
    sap_return_doc_num = models.CharField(max_length=50, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="short_dispatches_posted",
    )

    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "-created_at"]),
            models.Index(fields=["sap_invoice_doc_entry"]),
        ]
        permissions = [
            ("can_view_short_dispatch", "Can view short dispatch entries"),
            ("can_create_short_dispatch", "Can record and post a short dispatch"),
        ]

    def __str__(self):
        return self.entry_no

    @property
    def active_lines(self):
        return [line for line in self.lines.all() if line.is_active]

    @staticmethod
    def _next_number(prefix: str) -> int:
        last = (
            ShortDispatch.objects.filter(entry_no__startswith=prefix)
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
        prefix = f"SD-{timezone.now().strftime('%Y%m%d')}"
        return f"{prefix}-{cls._next_number(prefix):04d}"


class ShortDispatchItem(BaseModel):
    """One invoice line that did not go, and how much of it.

    ``short_quantity`` / ``reason`` are the only genuinely new data. Item identity
    and the billed quantity are snapshotted for display and for capping a second
    entry against the same bill; ``unit_price`` / ``tax_code`` are snapshotted so
    the SAP post needs no live invoice re-read.
    """

    short_dispatch = models.ForeignKey(
        ShortDispatch,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    # INV1.LineNum on the source invoice -- what makes a line the *same* line
    # across two entries against one bill.
    source_line_num = models.IntegerField(null=True, blank=True)
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, blank=True)
    uom = models.CharField(max_length=50, blank=True)
    invoice_quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    short_quantity = models.DecimalField(max_digits=18, decimal_places=3)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    tax_code = models.CharField(max_length=20, blank=True)
    # The warehouse this line was billed out of (INV1.WhsCode), kept even when the
    # header sends the stock somewhere else -- it is the only record of where the
    # goods were supposed to have come from.
    source_warehouse_code = models.CharField(max_length=50, blank=True)

    # The batch SAP allocated on the invoice line, read from IBT1 at lookup time.
    # It cannot be reused: SAP refuses a return into a batch that already exists,
    # so the posted document mints a fresh number and this is the only record of
    # the batch physically standing on the floor.
    original_batch_number = models.CharField(max_length=100, blank=True)

    reason = models.CharField(
        max_length=30,
        choices=ShortDispatchReason.choices,
        default=ShortDispatchReason.SHORT,
    )
    remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["source_line_num", "id"]
        indexes = [models.Index(fields=["short_dispatch"])]

    def __str__(self):
        return f"{self.short_dispatch.entry_no} - {self.item_code}"
