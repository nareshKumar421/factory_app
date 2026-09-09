"""The raw-material quantity a store keeper states is on the floor.

SAP already carries an on-hand figure per item and warehouse (``OITW.OnHand``)
and this table does not replace it. It answers a different question: what the
person responsible for the store says is actually there, typed by them, as of
when they typed it. The two disagree in practice — receipts booked late, issues
consumed but not posted, material moved between stores without a document — and
until now there was nowhere in the app to record the floor's own figure.

Shape, and why:

* **One live row per (company, warehouse, item).** The page is a register of
  current quantities, not a log of counts, so setting a quantity twice updates
  one row rather than growing a list the reader then has to reduce.
* **Every change is appended to :class:`RawMaterialStockEntry`.** A quantity
  someone can overwrite with no trace is a quantity nobody can defend, so the
  previous value, the new one, who and when are kept on every save.
* **Removal deactivates.** Same reasoning as ``UserWarehouse``: the row is the
  record that this item was once stocked here, and past history hangs off it.
* Warehouse and item are stored as **codes**, matching every other model here —
  warehouses live in SAP ``OWHS`` and items in ``OITM``; neither has a Django
  table to point a foreign key at.

Nothing here posts to SAP. Writes go through
``warehouse.services.rm_stock_service`` so the per-warehouse manager check is
never skipped.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models

from company.models import Company


class RawMaterialStock(models.Model):
    """The current raw-material quantity for one item in one warehouse."""

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="rm_stock_rows",
    )
    # 50 to match the warehouse-code columns elsewhere in this app
    # (BSTTransfer.sap_from_warehouse, UserWarehouse.warehouse_code).
    warehouse_code = models.CharField(max_length=50, db_index=True)

    item_code = models.CharField(max_length=50, db_index=True)
    # Copied from OITM at the time the row was set, so the register still reads
    # as something human when SAP is unreachable or an item is renamed.
    item_name = models.CharField(max_length=200, blank=True, default="")
    uom = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Inventory UoM as SAP holds it (OITM.InvntryUom).",
    )

    qty = models.DecimalField(
        max_digits=18, decimal_places=3, default=Decimal("0"),
        help_text="Quantity the warehouse states it is holding.",
    )
    # Kept apart from `updated_at`: a keeper often types Monday's count on
    # Tuesday, and a register that cannot say which day the figure belongs to
    # cannot be reconciled against anything.
    as_of_date = models.DateField(
        help_text="The date this quantity is true as of.",
    )
    remarks = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)

    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rm_stock_rows_set",
        help_text="Who last set the quantity.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_rm_stock"
        verbose_name = "raw material stock"
        verbose_name_plural = "raw material stock"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "warehouse_code", "item_code"],
                name="uniq_rm_stock_company_whs_item",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "warehouse_code"]),
            models.Index(fields=["company", "item_code"]),
        ]
        permissions = [
            ("can_view_rm_stock", "Can view raw material stock quantities"),
            ("can_set_rm_stock", "Can set raw material stock quantities"),
        ]
        ordering = ["warehouse_code", "item_code"]

    def __str__(self) -> str:
        return f"{self.item_code} @ {self.warehouse_code}: {self.qty}"

    def save(self, *args, **kwargs):
        # SAP codes are upper case. A lower-case warehouse code would never
        # match a UserWarehouse assignment, and a lower-case item code would
        # quietly create a second row for the same material.
        self.warehouse_code = (self.warehouse_code or "").strip().upper()
        self.item_code = (self.item_code or "").strip().upper()
        super().save(*args, **kwargs)


class RawMaterialStockEntry(models.Model):
    """One change to a quantity — the audit trail behind the register.

    The item, warehouse and company are denormalised onto every entry rather
    than read through the ``stock`` link: the history has to stay readable after
    a register row is removed, and it is the history that answers "who changed
    this and to what" long after nobody remembers.
    """

    class Action(models.TextChoices):
        CREATED = "CREATED", "Created"
        UPDATED = "UPDATED", "Updated"
        REMOVED = "REMOVED", "Removed"
        RESTORED = "RESTORED", "Restored"

    stock = models.ForeignKey(
        RawMaterialStock,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="rm_stock_entries",
    )
    warehouse_code = models.CharField(max_length=50, db_index=True)
    item_code = models.CharField(max_length=50, db_index=True)

    action = models.CharField(max_length=10, choices=Action.choices)
    # Null on CREATED: there was no figure before, which is a different thing
    # from a figure of zero.
    previous_qty = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True,
    )
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    as_of_date = models.DateField(null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rm_stock_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "warehouse_rm_stock_entry"
        verbose_name = "raw material stock change"
        verbose_name_plural = "raw material stock changes"
        indexes = [
            models.Index(fields=["company", "warehouse_code", "item_code"]),
            models.Index(fields=["-changed_at"]),
        ]
        ordering = ["-changed_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} {self.item_code} @ {self.warehouse_code} -> {self.qty}"

    @property
    def qty_delta(self):
        """How much the figure moved, or None when there was nothing before."""
        if self.previous_qty is None:
            return None
        return self.qty - self.previous_qty
