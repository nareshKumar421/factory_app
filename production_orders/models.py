"""A production entry and the SAP production order it becomes, step by step.

One entry is one SAP production order. It moves through the steps SAP itself
takes, each on its own page asking only what that step needs, each its own
right, each posted under the SAP login of the person who takes it:

1. **Plan** — the order, created planned, its lines exactly the BOM (SAP takes
   a new order only as planned, and refuses a standard order that differs
   from its BOM). Asks for the product, how much, and the date.
2. **Release** — nothing to ask.
3. **Issue** — every line at its planned quantity; asks for the date, the
   variety and the batches of the batch-tracked lines.
4. **Receipt** — the finished goods; asks for the date and the batch: line,
   oil code, production date, expiry.
5. **Close** — asks for the closing date; SAP then posts the variance.

Each step can be saved as a draft first and posted later. Posting goes through
``sap_postings``: a step that finds SAP down waits and is sent again, as the
same person, when SAP answers.
"""

from django.conf import settings
from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel

from .constants import LINE_CODES


class EntryKind(models.TextChoices):
    FG_FILLING = "FG_FILLING", "FG filling"


class EntryStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PLANNED = "PLANNED", "Planned"
    RELEASED = "RELEASED", "Released"
    ISSUED = "ISSUED", "Materials issued"
    RECEIVED = "RECEIVED", "FG created"
    CLOSED = "CLOSED", "Closed"


class Step(models.TextChoices):
    PLAN = "PLAN", "Plan"
    RELEASE = "RELEASE", "Release"
    ISSUE = "ISSUE", "Issue"
    RECEIPT = "RECEIPT", "Receipt"
    CLOSE = "CLOSE", "Close"


#: The steps in the order SAP needs them, and the status each one leaves.
STEPS = (Step.PLAN, Step.RELEASE, Step.ISSUE, Step.RECEIPT, Step.CLOSE)
STATUS_AFTER = {
    Step.PLAN: EntryStatus.PLANNED,
    Step.RELEASE: EntryStatus.RELEASED,
    Step.ISSUE: EntryStatus.ISSUED,
    Step.RECEIPT: EntryStatus.RECEIVED,
    Step.CLOSE: EntryStatus.CLOSED,
}
#: The step an entry in each status takes next (none once closed).
NEXT_STEP = {
    EntryStatus.DRAFT: Step.PLAN,
    EntryStatus.PLANNED: Step.RELEASE,
    EntryStatus.RELEASED: Step.ISSUE,
    EntryStatus.ISSUED: Step.RECEIPT,
    EntryStatus.RECEIVED: Step.CLOSE,
}
#: Statuses in order, so "is this step done?" is a comparison.
STATUS_ORDER = (
    EntryStatus.DRAFT, EntryStatus.PLANNED, EntryStatus.RELEASED,
    EntryStatus.ISSUED, EntryStatus.RECEIVED, EntryStatus.CLOSED,
)


def _person():
    return models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )


class ProductionOrderEntry(BaseModel):
    """One production entry: what was made, and the SAP order posted for it."""

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="production_order_entries"
    )
    entry_no = models.CharField(max_length=30)
    kind = models.CharField(max_length=20, choices=EntryKind.choices, default=EntryKind.FG_FILLING)
    status = models.CharField(max_length=10, choices=EntryStatus.choices, default=EntryStatus.DRAFT)

    # --- Plan: what was made. Pieces are SAP's unit for finished goods; the
    # floor counts boxes (OITM.SalFactor2 pieces each) and loose pieces.
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True, default="")
    uom = models.CharField(max_length=20, blank=True, default="")
    pieces_per_box = models.DecimalField(max_digits=19, decimal_places=6)
    litres_per_piece = models.DecimalField(
        max_digits=19, decimal_places=6, null=True, blank=True,
        help_text="OITM.SalPackUn, for the litres SAP's stock caps count.",
    )
    boxes = models.PositiveIntegerField(default=0)
    loose_pieces = models.PositiveIntegerField(default=0)
    quantity = models.DecimalField(
        max_digits=19, decimal_places=6, help_text="Pieces: the order's planned quantity."
    )
    warehouse = models.CharField(max_length=8, help_text="Where the goods are received (the BOM's).")
    bom_quantity = models.DecimalField(
        max_digits=19, decimal_places=6, help_text="OITT.Qauntity: the BOM's lines are per this many pieces."
    )
    posting_date = models.DateField(help_text="The order's posting, start and due date.")
    remarks = models.CharField(max_length=200, blank=True, default="")

    # --- Issue.
    variety = models.CharField(
        max_length=8, blank=True, default="",
        help_text="OOCR code (dimension 1) on the issue and receipt lines; the product's by default.",
    )
    issue_date = models.DateField(null=True, blank=True)

    # --- Receipt: the batch the goods go in under, <line><oil code> <MMYYDD> <NN>.
    receipt_date = models.DateField(null=True, blank=True)
    line_code = models.CharField(max_length=2, choices=list(LINE_CODES.items()), blank=True, default="")
    oil_code = models.CharField(max_length=10, blank=True, default="")
    batch_sequence = models.PositiveSmallIntegerField(null=True, blank=True)
    batch_number = models.CharField(max_length=36, blank=True, default="")
    mfg_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    # --- Close.
    close_date = models.DateField(null=True, blank=True)

    # The SAP documents, as each step posts them.
    sap_order_entry = models.PositiveIntegerField(null=True, blank=True)
    sap_order_num = models.PositiveBigIntegerField(null=True, blank=True)
    sap_issue_entry = models.PositiveIntegerField(null=True, blank=True)
    sap_issue_num = models.PositiveBigIntegerField(null=True, blank=True)
    sap_receipt_entry = models.PositiveIntegerField(null=True, blank=True)
    sap_receipt_num = models.PositiveBigIntegerField(null=True, blank=True)

    # Who took each step, and when SAP accepted it.
    planned_by = _person()
    planned_at = models.DateTimeField(null=True, blank=True)
    released_by = _person()
    released_at = models.DateTimeField(null=True, blank=True)
    issued_by = _person()
    issued_at = models.DateTimeField(null=True, blank=True)
    received_by = _person()
    received_at = models.DateTimeField(null=True, blank=True)
    closed_by = _person()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-posting_date", "-id"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "posting_date"]),
            models.Index(fields=["company", "item_code", "batch_number"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["company", "entry_no"], name="prod_order_entry_no_unique"),
        ]
        default_permissions = ()
        permissions = [
            ("can_view_production_orders", "Can view production order entries"),
            ("can_create_production_orders", "Can enter production and plan the SAP production order"),
            ("can_release_production_orders", "Can release SAP production orders"),
            ("can_issue_production_orders", "Can issue materials to SAP production orders"),
            (
                "can_receive_production_orders",
                "Can receive finished goods from SAP production orders",
            ),
            ("can_close_production_orders", "Can close SAP production orders"),
        ]

    def __str__(self):
        return f"{self.entry_no} {self.item_code} ({self.company.code})"

    @property
    def next_step(self):
        return NEXT_STEP.get(self.status)

    @property
    def sap_reference(self) -> str:
        """Written into every SAP document's Comments, so a retry can find what
        an earlier try posted before SAP's answer was lost."""
        return f"App {self.entry_no}"

    def step_done(self, step) -> bool:
        return STATUS_ORDER.index(EntryStatus(self.status)) > STEPS.index(Step(step))


class ProductionOrderEntryLine(models.Model):
    """One line of the order: a BOM component or resource, scaled to the entry."""

    class ItemType(models.TextChoices):
        ITEM = "item", "Item"
        RESOURCE = "resource", "Resource"

    entry = models.ForeignKey(ProductionOrderEntry, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField(help_text="The line's place in the BOM.")
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True, default="")
    item_type = models.CharField(max_length=10, choices=ItemType.choices, default=ItemType.ITEM)
    issue_method = models.CharField(max_length=1, default="M", help_text="ITT1.IssueMthd: M or B.")
    base_quantity = models.DecimalField(
        max_digits=19, decimal_places=6, help_text="Per piece of the product (BOM quantity ÷ BOM size)."
    )
    planned_quantity = models.DecimalField(max_digits=19, decimal_places=6)
    warehouse = models.CharField(max_length=8)
    uom = models.CharField(max_length=20, blank=True, default="")
    batch_managed = models.BooleanField(default=False)
    sap_line_num = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="WOR1.LineNum, once the order is in SAP."
    )

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "position"], name="prod_order_line_position_unique"),
        ]

    def __str__(self):
        return f"{self.entry.entry_no} #{self.position} {self.item_code}"


class ProductionOrderEntryBatch(models.Model):
    """A batch chosen for a batch-tracked line's issue."""

    line = models.ForeignKey(ProductionOrderEntryLine, on_delete=models.CASCADE, related_name="batches")
    batch_number = models.CharField(max_length=36)
    quantity = models.DecimalField(max_digits=19, decimal_places=6)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.batch_number} × {self.quantity}"
