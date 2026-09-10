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
* **Boxes, not pieces.** The floor counts in boxes and so does the keeper.
  ``pieces_per_box`` is snapshotted from SAP's ``OITM.SalFactor2`` at the time
  the line was typed, never asked for, so a later reader can convert without
  re-reading a master that may have changed. See
  ``box-gen-qty-source-salfactor2``: the pack size is SalFactor2 and never a
  parse of the item name.
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

from django.conf import settings
from django.db import models
from django.utils import timezone


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
    to_warehouse = models.CharField(max_length=50, db_index=True)
    to_company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="pf_movements_inbound",
        help_text="The company the destination warehouse belongs to. Often the "
                  "same as `company`, but the Gupta finished godown the PF "
                  "floor ships into is Mart's while the floor is Oil's.",
    )
    # Copied from OWHS when the document was saved, so the register still reads
    # as something human when HANA is unreachable or a warehouse is renamed.
    from_warehouse_name = models.CharField(max_length=200, blank=True, default="")
    to_warehouse_name = models.CharField(max_length=200, blank=True, default="")

    # --- how ---------------------------------------------------------------
    # Optional. The keeper does not always know the truck when he writes the
    # plan, and a required field he cannot fill is a field he types junk into.
    vehicle_no = models.CharField(max_length=50, blank=True, default="")
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
        indexes = [
            models.Index(fields=["company", "-movement_date"]),
            models.Index(fields=["company", "from_warehouse", "-movement_date"]),
            models.Index(fields=["to_company", "to_warehouse", "-movement_date"]),
        ]
        permissions = [
            ("can_view_pf_movement", "Can view godown stock movements"),
            ("can_record_pf_movement", "Can record godown stock movements"),
        ]
        ordering = ["-movement_date", "-id"]

    def __str__(self) -> str:
        return f"{self.entry_no}: {self.from_warehouse} -> {self.to_warehouse}"

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
    def is_cross_company(self) -> bool:
        return self.to_company_id != self.company_id

    @property
    def total_boxes(self) -> int:
        """Boxes across every line. Read off the prefetch when there is one."""
        return sum(line.boxes for line in self.lines.all())


class PFStockMovementLine(models.Model):
    """One item on a declared consignment, counted in boxes."""

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

    boxes = models.PositiveIntegerField(
        help_text="Boxes the keeper is sending. A whole count — the floor does "
                  "not move part boxes between godowns.",
    )
    # Never typed: taken from SAP at the moment the line was saved so that a
    # reader months later can turn boxes into pieces without trusting today's
    # item master. Null when SAP had no figure — which is a different thing from
    # a pack size of one.
    pieces_per_box = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="OITM.SalFactor2 as it stood when this line was typed.",
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
            # silently double the box count every report reads.
            models.UniqueConstraint(
                fields=["movement", "item_code"],
                name="uniq_pf_movement_line_item",
            ),
        ]
        ordering = ["item_code"]

    def __str__(self) -> str:
        return f"{self.item_code} x {self.boxes} box"

    def save(self, *args, **kwargs):
        self.item_code = (self.item_code or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def pieces(self):
        """Boxes converted with the snapshotted pack size, or None without one."""
        if self.pieces_per_box is None:
            return None
        return self.boxes * self.pieces_per_box


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
    to_warehouse = models.CharField(max_length=50, blank=True, default="")
    line_count = models.PositiveIntegerField(default=0)
    total_boxes = models.PositiveIntegerField(default=0)
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
        return f"{self.action} {self.movement_id} ({self.total_boxes} box)"
