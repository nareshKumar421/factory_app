"""What a godown keeper declares he is sending out, and to where.

The production-finished floor (``BH-PF``) fills up as the line packs, and it is
emptied by the keeper pushing pallets out to whichever godown has room or has
asked for stock — the new basement ``BH-BT``, the Gupta godown, Mayapuri.
Those moves eventually appear in SAP as inventory transfers, but not when they
are decided and often not itemised the way the keeper decided them. Nothing in
the app recorded the decision itself.

This is that record, and **only** that record: a declaration of intent, typed by
the keeper, against which a dashboard will later be able to ask what he said he
would send versus what SAP shows actually left. Nothing here posts to SAP, seeds
a BST, or reserves stock. If it did, it would be
``warehouse.models_transfer.WarehouseTransferRequest``, which is the flow for a
move that has to *happen* rather than be *stated*.

Shape, and why:

* **One document per destination**, with item lines under it. A keeper sends a
  load to one godown at a time, and a sheet mixing destinations forces every
  reader — including the dashboard — to re-group it before it means anything.
* **Not every load goes to a godown.** Plenty leaves the floor straight onto a
  customer's truck, so ``destination_kind`` says which, and a dispatch carries
  no destination warehouse at all rather than a pseudo code that would show up
  as a real godown on every grouped report. The two are also reconciled against
  different things — a godown move against SAP's inventory transfers, a dispatch
  against its sales invoices.
* **Pieces, with litres derived from them.** The quantity is typed in pieces —
  SAP's own inventory UoM, what ``OITM`` and ``OITW`` count in — so the register
  and SAP speak one unit and nothing has to be converted to compare them. Two
  factors are snapshotted from SAP when a line is typed, never asked for:
  ``SalFactor2`` (the pack size, so the box equivalent can still be shown, since
  the floor thinks in boxes) and ``SalPackUn`` (litres in one piece, gated on
  ``U_IsLitre``). Both are frozen, because an item master that moves on would
  otherwise silently restate every declaration ever filed. Never parse the item
  name for either: names state the piece volume and the carton size separately
  and lie about both.
* **Destination carries its own company.** The Gupta finished godown the PF
  floor ships into is ``GP-FGM`` in *Mart*, while the floor itself is Oil. A
  destination stored as a bare code would be ambiguous — ``BH-FG`` and ``PB-FG``
  exist in more than one schema — so the company is stored beside it.
* **Cancelling deactivates.** Same reasoning as ``RawMaterialStock``: the
  document is the evidence that the keeper once declared this, and a retracted
  declaration is itself a fact the dashboard needs.

Warehouses and items are stored as **codes**, matching every other model in this
app — they live in SAP (``OWHS``, ``OITM``) and have no Django table to point a
foreign key at.

Every write goes through ``warehouse.services.pf_movement_service`` so the
per-warehouse manager check is never skipped.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class PFMovementDestinationKind(models.TextChoices):
    """Where the stock is headed, which is not always another godown.

    Plenty of what leaves the production floor never sits in a godown at all —
    it goes straight onto a customer's truck. Modelling that as a pseudo
    warehouse code was the obvious shortcut and the wrong one: every report that
    groups by destination would carry a fake godown alongside the real ones, and
    the two are reconciled against completely different things (a godown move
    against SAP's inventory transfers, a dispatch against its sales invoices).

    So the kind is explicit, and a dispatch simply has no destination warehouse.
    """

    GODOWN = "GODOWN", "To another godown"
    DISPATCH = "DISPATCH", "Dispatched directly"


class PFStockMovement(models.Model):
    """One declared consignment: this stock, out of here, to there."""

    # The company the *source* warehouse belongs to — whose floor is being
    # emptied. The destination's company is a separate field below, because the
    # two genuinely differ on the moves this page exists to record.
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="pf_movements",
    )
    entry_no = models.CharField(max_length=30, unique=True, db_index=True)

    # --- when -------------------------------------------------------------
    # Kept apart from `created_at`: a keeper types Monday's dispatch plan on
    # Sunday evening, and a register that cannot say which day the stock is
    # meant to move cannot be read against anything.
    movement_date = models.DateField(
        db_index=True,
        help_text="The day this stock is to move.",
    )

    # --- route ------------------------------------------------------------
    # 50 to match the warehouse-code columns elsewhere in this app
    # (UserWarehouse.warehouse_code, RawMaterialStock.warehouse_code).
    from_warehouse = models.CharField(max_length=50, db_index=True)
    destination_kind = models.CharField(
        max_length=10,
        choices=PFMovementDestinationKind.choices,
        default=PFMovementDestinationKind.GODOWN,
        db_index=True,
        help_text="Whether this load is going to another godown or straight out "
                  "on a dispatch.",
    )
    # Blank on a dispatch — there is no destination godown, and a placeholder
    # code would show up as a real one on every report that groups by it. The
    # database enforces the pairing; see Meta.constraints.
    to_warehouse = models.CharField(
        max_length=50, blank=True, default="", db_index=True
    )
    to_company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="pf_movements_inbound",
        null=True,
        blank=True,
        help_text="The company the destination warehouse belongs to. Often the "
                  "same as `company`, but the Gupta finished godown the PF "
                  "floor ships into is Mart's while the floor is Oil's. Null on "
                  "a dispatch, which has no destination godown.",
    )
    # Copied from OWHS when the document was saved, so the register still reads
    # as something human when HANA is unreachable or a warehouse is renamed.
    from_warehouse_name = models.CharField(max_length=200, blank=True, default="")
    to_warehouse_name = models.CharField(max_length=200, blank=True, default="")

    # --- how ---------------------------------------------------------------
    # Optional. The keeper does not always know the truck when he writes the
    # plan, and a required field he cannot fill is a field he types junk into.
    vehicle_no = models.CharField(max_length=50, blank=True, default="")
    # The invoice or bilty number, when there is one. Optional for the same
    # reason as the vehicle: the plan is often written before the invoice is
    # cut, and a required field he cannot fill is a field he types junk into.
    reference = models.CharField(
        max_length=50, blank=True, default="", db_index=True,
        help_text="Invoice or bilty number, if the paperwork exists yet.",
    )
    remarks = models.TextField(blank=True, default="")

    # --- state -------------------------------------------------------------
    # There is no PLANNED -> MOVED progression on purpose. The document is a
    # declaration; whether the stock moved is answered by SAP's own transfers,
    # which the dashboard will read separately. A status here would be a second,
    # unverified answer to the same question.
    is_active = models.BooleanField(default=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pf_movements_cancelled",
    )
    cancellation_reason = models.TextField(blank=True, default="")

    # --- audit -------------------------------------------------------------
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pf_movements_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pf_movements_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_pf_movement"
        verbose_name = "godown stock movement"
        verbose_name_plural = "godown stock movements"
        constraints = [
            # The kind and the destination must agree. Without this a dispatch
            # could keep a stale destination godown after being switched over,
            # and every report grouping by destination would silently count it
            # under a godown it never went to.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        destination_kind=PFMovementDestinationKind.GODOWN,
                        to_company__isnull=False,
                    )
                    & ~models.Q(to_warehouse="")
                )
                | models.Q(
                    destination_kind=PFMovementDestinationKind.DISPATCH,
                    to_warehouse="",
                    to_company__isnull=True,
                ),
                name="pf_movement_destination_matches_kind",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "-movement_date"]),
            models.Index(fields=["company", "from_warehouse", "-movement_date"]),
            models.Index(fields=["to_company", "to_warehouse", "-movement_date"]),
            models.Index(fields=["company", "destination_kind", "-movement_date"]),
        ]
        permissions = [
            ("can_view_pf_movement", "Can view godown stock movements"),
            ("can_record_pf_movement", "Can record godown stock movements"),
        ]
        ordering = ["-movement_date", "-id"]

    def __str__(self) -> str:
        return f"{self.entry_no}: {self.from_warehouse} -> {self.destination_display}"

    def save(self, *args, **kwargs):
        # SAP codes are upper case. A lower-case source code would never match a
        # UserWarehouse assignment, and a lower-case destination would split one
        # godown into two on every report that groups by it.
        self.from_warehouse = (self.from_warehouse or "").strip().upper()
        self.to_warehouse = (self.to_warehouse or "").strip().upper()
        super().save(*args, **kwargs)

    @staticmethod
    def generate_entry_no() -> str:
        """``PFM-20260910-0001``, matching ``WarehouseTransferRequest``'s scheme."""
        prefix = f"PFM-{timezone.now().strftime('%Y%m%d')}"
        last = (
            PFStockMovement.objects
            .filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )
        next_number = 1
        if last:
            try:
                next_number = int(last.entry_no.split("-")[-1]) + 1
            except ValueError:
                next_number = 1
        return f"{prefix}-{next_number:04d}"

    @property
    def is_dispatch(self) -> bool:
        return self.destination_kind == PFMovementDestinationKind.DISPATCH

    @property
    def destination_display(self) -> str:
        """Where this went, in one string a report can print.

        "Dispatch" rather than a blank: on a dispatch there is no destination
        godown, and an empty cell reads as missing data instead of as the answer.
        """
        if self.is_dispatch:
            return "Dispatch"
        return self.to_warehouse

    @property
    def is_cross_company(self) -> bool:
        """Whether the stock crosses into another company's books.

        False for a dispatch. `to_company` is null there, and a bare
        ``to_company_id != company_id`` would read None as "a different company"
        and report every dispatch as intercompany.
        """
        if self.to_company_id is None:
            return False
        return self.to_company_id != self.company_id

    @property
    def total_pieces(self) -> int:
        """Pieces across every line. Read off the prefetch when there is one."""
        return sum(line.pieces for line in self.lines.all())

    @property
    def total_litres(self):
        """Litres across every line, skipping items SAP holds no volume for."""
        return sum(
            (line.litres for line in self.lines.all() if line.litres is not None),
            Decimal("0"),
        )


class PFStockMovementLine(models.Model):
    """One item on a declared consignment, counted in pieces."""

    movement = models.ForeignKey(
        PFStockMovement,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    item_code = models.CharField(max_length=50, db_index=True)
    # Snapshotted from OITM for the same reason as the warehouse names above.
    item_name = models.CharField(max_length=200, blank=True, default="")
    uom = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Inventory UoM as SAP holds it (OITM.InvntryUom).",
    )

    pieces = models.PositiveIntegerField(
        help_text="Pieces the keeper is sending — single bottles or pouches, "
                  "SAP's own inventory UoM, which is what OITM and OITW count "
                  "in. Never cartons: a carton count stored here would inflate "
                  "every figure downstream by the pack size.",
    )
    # Neither factor below is ever typed. Both are taken from SAP at the moment
    # the line is saved, so a reader months later can convert without trusting
    # an item master that has moved on.
    #
    # Null means SAP had no figure, which is a different thing from a factor of
    # one or of zero — see the `full_boxes` and `litres` properties.
    pieces_per_box = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="OITM.SalFactor2 as it stood when this line was typed — the "
                  "pack size, kept only to show the box equivalent.",
    )
    # 6 decimal places because SAP holds it that way: a 750 GMS pouch reads
    # 0.824200 litres, a weight-to-volume conversion nothing else can rederive.
    litres_per_piece = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
        help_text="OITM.SalPackUn as it stood when this line was typed — litres "
                  "in one piece. Null for an item SAP does not measure in "
                  "litres (U_IsLitre != 'Y'), such as a carton or a preform.",
    )

    remarks = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_pf_movement_line"
        verbose_name = "godown stock movement line"
        verbose_name_plural = "godown stock movement lines"
        constraints = [
            # One line per item per document. Two lines for the same item are
            # always a double-entry rather than a distinction, and they would
            # silently double the quantity every report reads.
            models.UniqueConstraint(
                fields=["movement", "item_code"],
                name="uniq_pf_movement_line_item",
            ),
        ]
        ordering = ["item_code"]

    def __str__(self) -> str:
        return f"{self.item_code} x {self.pieces} pcs"

    def save(self, *args, **kwargs):
        self.item_code = (self.item_code or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def litres(self):
        """Litres on this line, or None for an item SAP holds no volume for.

        ``pieces x SalPackUn``, the same arithmetic the monthly sales-litre
        reports run on. None rather than zero when ``U_IsLitre`` was not 'Y': a
        carton or a preform is not zero litres, it is not measured in litres,
        and a zero in that column would get added up by somebody eventually.
        """
        if self.litres_per_piece is None:
            return None
        return self.pieces * self.litres_per_piece

    @property
    def full_boxes(self):
        """Whole boxes the piece count comes to, or None without a pack size.

        The floor still counts in boxes even though the quantity is typed in
        pieces, so this is worth showing beside it. Floor division, with
        `loose_pieces` alongside rather than a rounded figure: 485 pieces at 20
        a box is 24 boxes and 5 loose, and "24.25 boxes" is not something
        anybody can load onto a truck.
        """
        if not self.pieces_per_box:
            return None
        return self.pieces // self.pieces_per_box

    @property
    def loose_pieces(self):
        """Pieces left over after the whole boxes, or None without a pack size."""
        if not self.pieces_per_box:
            return None
        return self.pieces % self.pieces_per_box


class PFStockMovementEvent(models.Model):
    """What happened to a document — the trail behind an editable declaration.

    The document can be edited and retracted, and a declaration somebody can
    rewrite with no trace is one nobody can be held to. Totals are denormalised
    onto the event rather than recomputed from the lines, because the point of
    the trail is to say what the document said *then*.
    """

    class Action(models.TextChoices):
        CREATED = "CREATED", "Created"
        UPDATED = "UPDATED", "Updated"
        CANCELLED = "CANCELLED", "Cancelled"
        RESTORED = "RESTORED", "Restored"

    movement = models.ForeignKey(
        PFStockMovement,
        on_delete=models.CASCADE,
        related_name="events",
    )
    action = models.CharField(max_length=10, choices=Action.choices)

    # The document as it stood after this event.
    movement_date = models.DateField(null=True, blank=True)
    # Snapshotted beside the warehouse so the trail can say "was going to BH-BT,
    # now a direct dispatch" rather than showing a destination that just emptied.
    destination_kind = models.CharField(
        max_length=10,
        choices=PFMovementDestinationKind.choices,
        blank=True,
        default="",
    )
    to_warehouse = models.CharField(max_length=50, blank=True, default="")
    line_count = models.PositiveIntegerField(default=0)
    total_pieces = models.PositiveIntegerField(default=0)
    total_litres = models.DecimalField(
        max_digits=16, decimal_places=3, default=0,
        help_text="Litres the document came to, counting only the items SAP "
                  "measures in litres.",
    )
    note = models.TextField(blank=True, default="")

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pf_movement_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "warehouse_pf_movement_event"
        verbose_name = "godown stock movement change"
        verbose_name_plural = "godown stock movement changes"
        indexes = [models.Index(fields=["-changed_at"])]
        ordering = ["-changed_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} {self.movement_id} ({self.total_pieces} pcs)"
