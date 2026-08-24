"""planning_purchase/models.py

The plan itself is NOT stored here. It is authored in SAP as a sales forecast
(`OFCT` header + `FCT1` lines) — every row in this factory's OFCT is literally
named "OIL Monthly Production Planning for the <Month> <Year>" — and is read
live. Storing a copy would create a second source of truth for a number
planners edit in SAP.

What IS stored is the thing SAP has no record of until we send it: the purchase
order a planner builds from the plan's bill of materials, and the audit trail of
who approved it and what SAP said when it was posted.
"""

from django.conf import settings
from django.db import models


class PlanningPurchasePermission(models.Model):
    """Sentinel model carrying the module's permissions. No rows are ever written."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_production_plan", "Can view production plans and requirement"),
            ("can_create_purchase_order", "Can create purchase orders from a plan"),
            ("can_approve_purchase_order", "Can approve purchase orders"),
            ("can_post_purchase_order_to_sap", "Can post purchase orders to SAP"),
        ]


class MaterialType(models.TextChoices):
    PACKAGING = "PACKAGING", "Packaging Material"
    RAW = "RAW", "Raw Material"
    OTHER = "OTHER", "Other"


class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    APPROVED = "APPROVED", "Approved"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "SAP post failed"
    CANCELLED = "CANCELLED", "Cancelled"


class PurchaseOrder(models.Model):
    """A purchase order built from a plan's BOM shortfall, before and after SAP.

    One row per vendor. A requirement screen selection covering four suppliers
    creates four of these, because a SAP purchase order belongs to exactly one
    business partner and splitting later would lose the reviewer's intent.
    """

    company_code = models.CharField(max_length=50, db_index=True)

    # Where the requirement came from. Kept as plain values, not an FK: the plan
    # lives in SAP and can be edited or deleted there without our knowing.
    plan_abs_id = models.IntegerField(
        null=True, blank=True,
        help_text="SAP OFCT AbsID of the plan this was raised from",
    )
    plan_code = models.CharField(max_length=100, blank=True, default="")
    plan_name = models.CharField(max_length=255, blank=True, default="")

    vendor_code = models.CharField(max_length=50, db_index=True)
    vendor_name = models.CharField(max_length=200, blank=True, default="")

    doc_date = models.DateField(help_text="Posting date sent to SAP")
    doc_due_date = models.DateField(help_text="Delivery date required")
    warehouse_code = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Receiving warehouse for every line unless a line overrides it",
    )
    remarks = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
        db_index=True,
    )

    total_value = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    currency = models.CharField(max_length=10, blank=True, default="INR")

    # SAP posting
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.BigIntegerField(null=True, blank=True)
    sap_error_message = models.TextField(blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    simulated = models.BooleanField(
        default=False,
        help_text="True when the post ran under PLANNING_PURCHASE_SIMULATE_SAP "
                  "and no SAP document was actually created",
    )

    # The duplicate-post guard. Unique per company, so a retried request that
    # carries the same key can never create a second SAP purchase order.
    idempotency_key = models.CharField(max_length=64, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="created_planning_purchase_orders",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="approved_planning_purchase_orders",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="posted_planning_purchase_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Purchase Order (from plan)"
        verbose_name_plural = "Purchase Orders (from plan)"
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "idempotency_key"],
                name="uniq_planning_po_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["company_code", "status"]),
            models.Index(fields=["company_code", "plan_abs_id"]),
        ]

    def __str__(self):
        return f"PO {self.pk} {self.vendor_code} ({self.status})"

    @property
    def is_editable(self) -> bool:
        return self.status == PurchaseOrderStatus.DRAFT

    def recalculate_total(self) -> None:
        self.total_value = sum(
            (line.quantity * line.unit_price for line in self.lines.all()),
            start=self.total_value.__class__(0),
        )


class PurchaseOrderLine(models.Model):
    """One component on a purchase order, carrying the evidence for its quantity."""

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )

    item_code = models.CharField(max_length=100, db_index=True)
    item_name = models.CharField(max_length=255, blank=True, default="")
    material_type = models.CharField(
        max_length=20, choices=MaterialType.choices, default=MaterialType.OTHER
    )
    uom = models.CharField(max_length=30, blank=True, default="")

    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    unit_price = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    warehouse_code = models.CharField(max_length=20, blank=True, default="")
    required_date = models.DateField(
        null=True, blank=True, help_text="Date production needs it (SAP ShipDate)"
    )

    # Why this quantity — so the approver can check the number rather than
    # believe it. Snapshots, not live values: the plan and stock move on.
    required_qty = models.DecimalField(
        max_digits=20, decimal_places=6, default=0,
        help_text="BOM requirement for the plan at the time the order was raised",
    )
    available_qty = models.DecimalField(
        max_digits=20, decimal_places=6, default=0,
        help_text="Net available stock (on hand - committed) at that time",
    )
    on_order_qty = models.DecimalField(
        max_digits=20, decimal_places=6, default=0,
        help_text="Already on open purchase orders at that time",
    )
    shortage_qty = models.DecimalField(
        max_digits=20, decimal_places=6, default=0,
        help_text="required - available - on_order, floored at zero",
    )
    moq_applied = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True,
        help_text="Set when the quantity was rounded up to a minimum order quantity",
    )

    sap_line_num = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Purchase Order Line"
        verbose_name_plural = "Purchase Order Lines"
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order", "item_code"],
                name="uniq_planning_po_line_item",
            ),
        ]

    def __str__(self):
        return f"{self.item_code} x {self.quantity}"

    @property
    def line_value(self):
        return self.quantity * self.unit_price
