"""Order-processing domain models.

Source of truth, per the specification and confirmed against the live systems:

    OMS   -> orders, order lines, customers        (we mirror, never write)
    SAP   -> items, warehouses, stock, BOM, POs    (we query, never store)
    HERE  -> workflow, allocation, requirements, audit

Two shapes here are driven by findings from the live OMS rather than by the spec.

**Quantities.** ``qty`` is the number SAP is actually sent — verified against 1,081
real payload lines, where it matched 1,021 against 96 for ``boxes`` and 23 for
``pcs``. All four figures are mirrored anyway, because 29 lines carry a ``qty``
that disagrees with ``boxes × pcs`` and ``ltrs``, and a line we cannot trust must
be visible rather than silently wrong.

**Reservations.** Since July 2026 OMS posts SAP *Sales Orders*, which commit stock
in ``OITW.IsCommited``. So availability reads ``OnHand − IsCommited`` and we do NOT
keep a parallel ledger — it would double-count every pushed order. A local
reservation exists only for orders that never reached SAP (``sap_created=false``,
~273 of them), whose demand SAP has never been told about.
"""
from decimal import Decimal

from django.db import models


class SyncStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class OrderState(models.TextChoices):
    """Our workflow state. Distinct from the OMS approval status, which we mirror
    separately — that one belongs to OMS and we must not overwrite its meaning."""

    RECEIVED = "RECEIVED", "Received from OMS"
    VALIDATED = "VALIDATED", "Validated"
    STOCK_CHECKED = "STOCK_CHECKED", "Stock checked"
    PARTIALLY_AVAILABLE = "PARTIALLY_AVAILABLE", "Partially available"
    STOCK_ALLOCATED = "STOCK_ALLOCATED", "Stock allocated"
    PRODUCTION_REQUIRED = "PRODUCTION_REQUIRED", "Production required"
    READY_FOR_FULFILLMENT = "READY_FOR_FULFILLMENT", "Ready for fulfillment"
    FULFILLED = "FULFILLED", "Fulfilled"
    CANCELLED = "CANCELLED", "Cancelled"
    ON_HOLD = "ON_HOLD", "On hold"
    FAILED = "FAILED", "Failed"


class LineIssue(models.TextChoices):
    """Why a mirrored line cannot be trusted as-is.

    Recorded rather than rejected: an order with a doubtful line still has to be
    visible, and a silently dropped line is how a real shortage hides.
    """

    QTY_DISAGREES = "QTY_DISAGREES", "qty disagrees with boxes x pcs"
    NO_WAREHOUSE = "NO_WAREHOUSE", "No warehouse rule for this category"
    NO_ITEM_CODE = "NO_ITEM_CODE", "Line has no item code"
    ZERO_QTY = "ZERO_QTY", "Quantity is zero or missing"


class OmsSyncRun(models.Model):
    """One pull from OMS. A sync with no record is an unauditable sync."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=SyncStatus.choices, default=SyncStatus.RUNNING)

    watermark_from = models.DateTimeField(
        null=True, blank=True,
        help_text="orders.updated_at we pulled FROM — the previous run's high-water mark.",
    )
    watermark_to = models.DateTimeField(
        null=True, blank=True, help_text="Highest orders.updated_at seen in this run.",
    )
    orders_seen = models.PositiveIntegerField(default=0)
    orders_created = models.PositiveIntegerField(default=0)
    orders_updated = models.PositiveIntegerField(default=0)
    lines_written = models.PositiveIntegerField(default=0)
    issues_found = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"sync {self.started_at:%Y-%m-%d %H:%M} ({self.status})"


class OmsOrder(models.Model):
    """Mirror of one OMS order header.

    Mirrored rather than queried live because we attach workflow state to it, join
    it locally, and must keep working when OMS is unreachable. OMS remains the
    source of truth: every field below is overwritten on each sync.
    """

    # ── identity ──
    oms_order_id = models.IntegerField(
        unique=True, db_index=True, help_text="orders.id — the dedup key.",
    )
    order_number = models.CharField(max_length=64, db_index=True, help_text="ORD-YYYYMMDD-NNNN")

    # ── mirrored from OMS ──
    customer_code = models.CharField(max_length=50, db_index=True, help_text="SAP OCRD.CardCode")
    customer_name = models.CharField(max_length=255, blank=True)
    company_code = models.CharField(
        max_length=10, blank=True,
        help_text="orders.company — a varchar holding '1' (Jivo Wellness) or '2' (Jivo Mart).",
    )
    branch_bpl_id = models.IntegerField(
        null=True, blank=True, help_text="orders.dispatch_from_id = SAP BPL_ID.",
    )
    branch_name = models.CharField(max_length=120, blank=True)
    oms_status = models.CharField(max_length=40, db_index=True, help_text="order_statuses.code")
    order_type = models.CharField(max_length=20, blank=True, help_text="PARTY or STAFF")
    po_number = models.CharField(max_length=80, blank=True)
    ship_to_address = models.TextField(blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    is_foc = models.BooleanField(default=False)
    remarks = models.TextField(blank=True)

    delivery_date = models.DateField(
        null=True, blank=True,
        help_text="Parsed from orders.delivery_date, which is TEXT in OMS, not a date.",
    )
    delivery_date_raw = models.CharField(
        max_length=64, blank=True,
        help_text="The unparsed text, kept so an unparseable value is visible rather than lost.",
    )

    sap_created = models.BooleanField(default=False)
    sap_doc_number = models.CharField(max_length=50, blank=True)
    quotation_cancelled = models.BooleanField(default=False)

    oms_created_at = models.DateTimeField(null=True, blank=True)
    oms_updated_at = models.DateTimeField(
        null=True, blank=True, db_index=True, help_text="The sync watermark.",
    )

    # ── ours ──
    state = models.CharField(
        max_length=25, choices=OrderState.choices, default=OrderState.RECEIVED, db_index=True,
    )
    first_synced_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(auto_now=True)
    last_sync_run = models.ForeignKey(
        OmsSyncRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders",
    )

    class Meta:
        ordering = ["-oms_created_at", "-oms_order_id"]
        indexes = [
            models.Index(fields=["state", "-oms_created_at"]),
            models.Index(fields=["oms_status", "sap_created"]),
        ]

    def __str__(self):
        return f"{self.order_number} ({self.oms_status})"

    @property
    def is_demand(self):
        """Whether this order should consume stock at all.

        Rejected and cancelled orders never ship, and counting them makes the
        factory look permanently short.
        """
        from django.conf import settings

        if self.quotation_cancelled or self.state == OrderState.CANCELLED:
            return False
        return self.oms_status in (
            list(settings.OMS_SHIPPING_STATUSES) + list(settings.OMS_PIPELINE_STATUSES)
        )

    @property
    def committed_in_sap(self):
        """True when SAP already holds this demand in ``OITW.IsCommited``.

        Since July 2026 OMS posts Sales Orders, which commit. So a pushed order is
        already reflected in SAP's committed figure and must NOT be reserved again
        locally, or it is counted twice.
        """
        return bool(self.sap_created)


class OmsOrderLine(models.Model):
    """Mirror of one OMS order line."""

    order = models.ForeignKey(OmsOrder, on_delete=models.CASCADE, related_name="lines")
    oms_line_id = models.IntegerField(unique=True, db_index=True, help_text="order_items.id")

    item_code = models.CharField(max_length=100, db_index=True, help_text="= SAP OITM.ItemCode")
    item_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=40, blank=True, help_text="OIL / BEVERAGES")
    brand = models.CharField(max_length=80, blank=True)
    sub_group = models.CharField(max_length=80, blank=True)

    # All four are mirrored. `quantity` is what SAP receives; the rest are the
    # cross-check that makes a bad line visible instead of silently wrong.
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, default=0,
        help_text="order_items.qty — the quantity SAP is sent.",
    )
    pack_size = models.DecimalField(
        max_digits=12, decimal_places=4, default=0, help_text="order_items.pcs — pieces per case.",
    )
    cases = models.DecimalField(
        max_digits=18, decimal_places=4, default=0, help_text="order_items.boxes = round(qty/pcs, 2).",
    )
    litres = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    scheme_quantity = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    unit_price = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    line_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    warehouse_code = models.CharField(
        max_length=40, blank=True,
        help_text="Resolved from category. Blank for BEVERAGES — OMS sends no WarehouseCode.",
    )
    issues = models.JSONField(
        default=list, blank=True, help_text="LineIssue codes. Empty means the line is trustworthy.",
    )

    class Meta:
        ordering = ["order_id", "oms_line_id"]
        indexes = [models.Index(fields=["item_code", "warehouse_code"])]

    def __str__(self):
        return f"{self.item_code} x{self.quantity}"

    @property
    def implied_quantity(self):
        """``cases × pack_size`` — the independent reading of the same line."""
        return Decimal(self.cases) * Decimal(self.pack_size)

    @property
    def is_trustworthy(self):
        return not self.issues


class ProcessingEvent(models.Model):
    """Append-only audit. Every state change, integration call and decision.

    The spec asks the system to answer "why was production created?" months later.
    That is only possible if the reason was written down when it happened.
    """

    correlation_id = models.CharField(
        max_length=64, db_index=True,
        help_text="Ties every event from one operation together across services.",
    )
    event = models.CharField(max_length=60, db_index=True)
    entity_type = models.CharField(max_length=40, blank=True)
    entity_id = models.CharField(max_length=64, blank=True, db_index=True)
    source = models.CharField(max_length=20, blank=True, help_text="OMS / SAP / SYSTEM / USER")
    actor = models.CharField(max_length=150, blank=True)
    old_state = models.CharField(max_length=40, blank=True)
    new_state = models.CharField(max_length=40, blank=True)
    result = models.CharField(max_length=20, blank=True, help_text="OK / FAILED / SKIPPED")
    detail = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["entity_type", "entity_id", "-created_at"])]

    def __str__(self):
        return f"{self.event} {self.entity_type}:{self.entity_id}"


class OrderProcessingPermission(models.Model):
    """Permission holder only — no rows are ever created."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_orders", "Can view order processing"),
            ("can_sync_orders", "Can trigger an OMS sync"),
            ("can_allocate_stock", "Can allocate stock to an order"),
            ("can_plan_production", "Can raise production requirements"),
            ("can_plan_procurement", "Can raise procurement requirements"),
        ]
