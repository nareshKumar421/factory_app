"""Dismantling: taking a finished good apart and getting its components back.

A dismantle is the app's record of one SAP **disassembly production order** and
the two documents that complete it. It exists mainly for returned stock -- a
customer return sitting in the goods-return warehouse is opened up, the oil goes
back to loose and the bottles, caps, labels and cartons go back to packing
material -- but the same operation is run on plain warehouse stock, so the source
is recorded rather than assumed.

WHY THE APP KEEPS ITS OWN RECORD
--------------------------------
SAP's disassembly order carries no link at all to the return it came from:
``OriginAbs``, ``OriginNum`` and ``Comments`` are null on every one of the 411
live orders, and ``CardCode`` is the same dummy vendor throughout. So "which
return did this stock come from" is answerable only here. Everything else --
quantities, costs, the documents themselves -- belongs to SAP and is referenced
by its keys rather than copied, except the few figures snapshotted for display.

THE THREE DOCUMENTS
-------------------
Posting one dismantle writes three documents in a FIXED order (SAP refuses the
issue while the receipt is missing -- error 20206):

1. the disassembly order      -> ``sap_order_*``
2. Receipt from Production    -> ``sap_receipt_*``   (components come back)
3. Issue for Production       -> ``sap_issue_*``     (the parent is consumed)

They are recorded separately because SAP can accept some and refuse the next, and
none of them can be withdrawn by this app. A dismantle that got two of three
documents is ``PARTIALLY_POSTED`` and finishes on a retry, which picks up exactly
where SAP stopped -- it never re-posts a document that is already in.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class DismantleSource(models.TextChoices):
    GOODS_RETURN = "GOODS_RETURN", "From a goods return"
    STOCK = "STOCK", "From warehouse stock"


class DismantleStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    # SAP took some of the three documents and refused a later one. The stock has
    # partly moved, so this is a state to finish, not one to abandon.
    PARTIALLY_POSTED = "PARTIALLY_POSTED", "Partly posted to SAP"
    POSTED = "POSTED", "Posted to SAP"
    CANCELLED = "CANCELLED", "Cancelled"


class Dismantle(BaseModel):
    """One finished-good item, in one batch, taken apart in one warehouse."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="dismantles",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    status = models.CharField(
        max_length=20,
        choices=DismantleStatus.choices,
        default=DismantleStatus.DRAFT,
    )
    source = models.CharField(
        max_length=20,
        choices=DismantleSource.choices,
        default=DismantleSource.GOODS_RETURN,
    )

    # The return this stock came back on. Reference-only, and the reason this
    # model exists at all: SAP's disassembly order cannot hold it. Null for a
    # stock-sourced dismantle, and for returns keyed straight into SAP by the
    # accounts team -- those are dismantled off warehouse stock instead.
    goods_return = models.ForeignKey(
        "goods_return.GoodsReturn",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dismantles",
    )
    goods_return_item = models.ForeignKey(
        "goods_return.GoodsReturnItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dismantles",
    )

    # Where the stock sits and where, by default, its components go back. Both
    # the order's warehouse and the component lines' -- returns are dismantled
    # into the returns warehouse rather than into the production store, which is
    # what the BOM would otherwise say.
    warehouse_code = models.CharField(max_length=50)

    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, blank=True)
    uom = models.CharField(max_length=50, blank=True)
    # The batch being consumed. Blank only for an item SAP does not batch-manage;
    # for everything else SAP needs it named on the issue, and for app-posted
    # returns it is already known (``GoodsReturnItem`` minted it).
    batch_number = models.CharField(max_length=100, blank=True)

    # PIECES, never boxes. SAP's disassembly order is quantified in pieces
    # (``OWOR."PlannedQty"``) and so is the goods issue that consumes the parent;
    # a box figure here would dismantle a sixteenth of what was asked.
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    # Snapshots taken when the BOM was exploded, kept so a dismantle can be read
    # back without a live SAP call and so the explosion can be explained later.
    # They should be equal; where they are not, the per-piece component
    # quantities are inflated by their ratio and the operator was warned.
    pieces_per_box = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    bom_batch_size = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True
    )

    # Dimension-1 profit centre for the goods issue. SAP refuses a goods issue
    # line without one outright ("60003 Please select Variety"), so it is
    # resolved and stored rather than left to the document.
    variety_code = models.CharField(max_length=50, blank=True)

    remarks = models.TextField(blank=True)

    # The three SAP documents, each recorded the moment SAP accepts it so a
    # refusal on the next one cannot lose the reference to what is already
    # posted. See the module docstring for the order they are written in.
    sap_order_doc_entry = models.IntegerField(null=True, blank=True)
    sap_order_doc_num = models.CharField(max_length=50, blank=True)
    sap_receipt_doc_entry = models.IntegerField(null=True, blank=True)
    sap_receipt_doc_num = models.CharField(max_length=50, blank=True)
    sap_issue_doc_entry = models.IntegerField(null=True, blank=True)
    sap_issue_doc_num = models.CharField(max_length=50, blank=True)
    # Closing the order is cosmetic -- the stock has already moved -- so a
    # dismantle whose close failed is still POSTED, and this records the fact.
    order_closed = models.BooleanField(default=False)

    # Why SAP refused, kept on the record so the operator can read it and retry.
    # Cleared on a successful run.
    sap_post_error = models.TextField(blank=True)

    posting_date = models.DateField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dismantles_posted",
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["warehouse_code", "item_code"]),
            models.Index(fields=["goods_return"]),
        ]
        permissions = [
            ("can_view_dismantle", "Can view dismantle"),
            ("can_create_dismantle", "Can create dismantle"),
            ("can_edit_dismantle", "Can edit dismantle"),
            ("can_post_dismantle", "Can post a dismantle to SAP"),
        ]

    def __str__(self):
        return self.entry_no

    @property
    def active_components(self):
        return [c for c in self.components.all() if c.is_active]

    @property
    def recovered_components(self):
        """The components that actually come back -- what the receipt is built of.

        A component can be present on the recipe and not survive the dismantling:
        a shrink sleeve is cut off, a glass jar arrives broken. Un-ticking it
        leaves it off the order and off the receipt, which SAP permits on a
        disassembly (the component-count and quantity rules, 2020014 and 2020041,
        fire only on standard production orders).
        """
        return [c for c in self.active_components if c.recovered]

    @property
    def is_posted(self) -> bool:
        return self.sap_issue_doc_entry is not None

    @property
    def bom_inflated(self) -> bool:
        """Whether the recipe's batch size disagrees with the item's box size.

        True means every component quantity is out by ``pieces_per_box /
        bom_batch_size``. SAP's own disassembly screen divides by the same field
        and would be wrong in exactly the same way, so this is surfaced as a
        warning rather than silently corrected -- the fix belongs in the item
        master.
        """
        if not self.bom_batch_size or not self.pieces_per_box:
            return False
        return self.bom_batch_size != self.pieces_per_box

    @staticmethod
    def _next_number(prefix: str) -> int:
        last = (
            Dismantle.objects.filter(entry_no__startswith=prefix)
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
        prefix = f"DM-{timezone.now().strftime('%Y%m%d')}"
        return f"{prefix}-{cls._next_number(prefix):04d}"


class DismantleComponent(BaseModel):
    """One component the parent yields, and how much of it comes back.

    ``qty_per_piece`` is the recipe as SAP states it -- ``ITT1."Quantity" /
    OITT."Qauntity"`` -- and ``quantity`` is what this dismantle actually
    receives. The second starts as the first times the parent quantity and is
    then the operator's to change: a carton that tore is not recovered whole, and
    the quantity that reaches SAP has to be the real one rather than the recipe's.
    """

    dismantle = models.ForeignKey(
        Dismantle,
        on_delete=models.CASCADE,
        related_name="components",
    )
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, blank=True)
    uom = models.CharField(max_length=50, blank=True)

    qty_per_piece = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    quantity = models.DecimalField(max_digits=18, decimal_places=6, default=0)

    # Defaults to the dismantle's own warehouse, not the BOM's: material recovered
    # from returned goods belongs in the returns warehouse until someone decides
    # it is fit to use, and letting it flow straight into the production store is
    # a decision nobody made. Editable for the cases where it should.
    warehouse_code = models.CharField(max_length=50, blank=True)

    is_batch_managed = models.BooleanField(default=False)
    # Minted by the app for batch-managed components, because a Receipt from
    # Production cannot reuse an existing batch number anywhere in the company
    # (error 590001). Blank for everything SAP does not batch-manage.
    batch_number = models.CharField(max_length=100, blank=True)

    recovered = models.BooleanField(default=True)

    # SAP's own line number on the disassembly order, read back after the order is
    # created. The receipt names it as ``BaseLine``; it is not our line's
    # position, because SAP drops resource lines and may reorder what it keeps.
    sap_line_num = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["dismantle"])]
        constraints = [
            models.UniqueConstraint(
                fields=["dismantle", "item_code"],
                name="uniq_dismantle_component_item",
            )
        ]

    def __str__(self):
        return f"{self.dismantle.entry_no} - {self.item_code}"
