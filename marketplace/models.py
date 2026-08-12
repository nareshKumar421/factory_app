"""Marketplace (Flipkart/Amazon) dispatch, returns and master-data models.

Modelled on ``gate_core.models.sales_dispatch`` but anchored on a marketplace
**Order ID** instead of a SAP invoice. Sales billing does not exist in SAP for
these channels, so an internal (non-SAP) billing document is produced in JI while
inventory is still decremented in SAP (via a Delivery Note) on confirm.

All models are company-scoped (``company`` FK) and inherit :class:`BaseModel`
(created_at/updated_at/created_by/updated_by/is_active).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class MarketplaceChannel(models.TextChoices):
    FLIPKART = "FLIPKART", "Flipkart"
    AMAZON = "AMAZON", "Amazon"


# ─────────────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────────────
class MarketplaceWarehouse(BaseModel):
    """Links a channel to the SAP godown (warehouse) it dispatches from."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_warehouses"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    name = models.CharField(max_length=150)
    sap_warehouse_code = models.CharField(max_length=50)
    sap_customer_card_code = models.CharField(
        max_length=50, blank=True,
        help_text="SAP business partner used as CardCode on the delivery note.",
    )
    facility_code = models.CharField(max_length=50, blank=True, help_text="e.g. MAYAPURI")
    # ── Delivery-note posting config (master data; read at confirm time) ──
    sap_series = models.CharField(
        max_length=20, blank=True,
        help_text="SAP document numbering Series for the Delivery Note / Goods Issue. Blank = SAP default.",
    )
    sap_tax_code = models.CharField(
        max_length=20, blank=True,
        help_text="Default tax code (VatGroup) applied to each Delivery Note line. Blank = none.",
    )
    sap_branch_id = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Business Place / Branch (BPLId) the delivery note is booked under. "
                  "Required by SAP's GST localization.",
    )
    shipto_by_state = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Optional ship-to STATE → SAP ship-to/bill-to address code, splitting the "
            "delivery note by GST place of supply (the branch stays sap_branch_id). "
            "e.g. {\"Delhi\": \"FLIPKART B2C DELHI\", \"*\": \"FLIPKART B2C HARYANA\"} sets "
            "Delhi ship-to orders to the Delhi address and every other state to the "
            "Haryana address. \"*\" = default for unlisted states. Empty = no ShipToCode "
            "set (SAP uses the customer's default address, one delivery note)."
        ),
    )
    post_goods_issue = models.BooleanField(
        default=True,
        help_text="Post the packing-material Goods Issue when dispatching. Off = Delivery Note only.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Pre-selected warehouse when cutting a delivery note for this channel. "
                  "The operator can still choose a different one at cut time.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "sap_warehouse_code"],
                name="uniq_mp_warehouse",
            )
        ]
        ordering = ["channel", "name"]

    def __str__(self):
        return f"{self.channel}:{self.name} ({self.sap_warehouse_code})"


class MarketplaceSettings(BaseModel):
    """Per company + channel flow settings — flexible toggles for how the
    marketplace pipeline behaves. One row per (company, channel).

    ``skip_packing``: when on, the Packing step is optional — an order becomes
    dispatchable in Outward as soon as its materials are issued/received, without
    requiring a completed packing session. See MARKETPLACE_FLIPKART_SHEET_FLOW.md.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_settings"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    skip_packing = models.BooleanField(
        default=False,
        help_text="Skip the Packing step: issued orders go straight to Outward.",
    )
    defer_delivery_note = models.BooleanField(
        default=False,
        help_text=(
            "Don't post the SAP Delivery Note when a dispatch is confirmed. Instead "
            "cut one bulk Delivery Note for all confirmed dispatches from the SAP "
            "Delivery Notes page."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "channel"], name="uniq_mp_settings"),
        ]
        ordering = ["channel"]
        verbose_name = "Marketplace settings"
        verbose_name_plural = "Marketplace settings"

    def __str__(self):
        return f"{self.channel} settings (skip_packing={self.skip_packing})"


class ComboDefinition(BaseModel):
    """A combo/kit authored in JI (SAP sales-BOM replacement).

    Expands into finished-good (FG) and packing-material (PM) components.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_combos"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    code = models.CharField(max_length=80)
    name = models.CharField(max_length=200)
    image = models.ImageField(
        upload_to="marketplace/combos/", blank=True, null=True,
        help_text="Combo/pack reference photo (pick-and-pack aid).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "code"], name="uniq_mp_combo_code"
            )
        ]
        ordering = ["channel", "code"]

    def __str__(self):
        return f"{self.channel}:{self.code}"


class ComboComponentType(models.TextChoices):
    FG = "FG", "Finished Good"
    PM = "PM", "Packing Material"


class ComboComponent(models.Model):
    """One BOM-style component line of a :class:`ComboDefinition`."""

    combo = models.ForeignKey(
        ComboDefinition, on_delete=models.CASCADE, related_name="components"
    )
    component_type = models.CharField(max_length=2, choices=ComboComponentType.choices)
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(
        max_digits=18, decimal_places=3, help_text="Quantity per 1 combo unit."
    )
    uom = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.combo_id}:{self.component_type}:{self.item_code}"


class ComboComponentOption(models.Model):
    """An interchangeable SAP item for one combo component.

    The same combo slot can often be filled by more than one SAP item depending on
    what is in stock — e.g. a "1 LTR mustard" slot could ship as FG0000030 or
    FG0000384. Each option is a valid substitute; exactly one is the default. The
    operator picks the actual item per order at delivery-note time (see
    ``MarketplaceOrderLine.component_choices``). A component with NO options ships
    its own ``item_code``, exactly as before.
    """

    component = models.ForeignKey(
        ComboComponent, on_delete=models.CASCADE, related_name="options"
    )
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Combo units of THIS item when it is the one picked. Blank = "
        "same quantity as the component.",
    )
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "id"]

    def __str__(self):
        return f"copt:{self.component_id}:{self.item_code}"


class SkuType(models.TextChoices):
    RAW = "RAW", "Raw (direct FG)"
    COMBO = "COMBO", "Combo / Kit"


class SkuMapping(BaseModel):
    """Marketplace SKU (FSN/ASIN) → internal FG item, or a combo definition."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_sku_mappings"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    marketplace_sku = models.CharField(max_length=120)
    # Flipkart FSN — the primary, stable key mapped to the SAP item. Matched first;
    # marketplace_sku is a fallback for rows without an FSN.
    fsn = models.CharField(
        max_length=60, blank=True, db_index=True,
        help_text="Flipkart FSN — primary key mapped to the SAP item code.",
    )
    sku_name = models.CharField(max_length=200, blank=True)
    sku_type = models.CharField(max_length=10, choices=SkuType.choices, default=SkuType.RAW)
    # RAW mapping
    fg_item_code = models.CharField(max_length=100, blank=True)
    fg_item_name = models.CharField(max_length=200, blank=True)
    # COMBO mapping
    combo = models.ForeignKey(
        ComboDefinition, on_delete=models.PROTECT, null=True, blank=True,
        related_name="sku_mappings",
    )
    default_uom = models.CharField(max_length=20, blank=True)
    image = models.ImageField(
        upload_to="marketplace/skus/", blank=True, null=True,
        help_text="SKU reference photo (pick-and-pack aid).",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "marketplace_sku"],
                name="uniq_mp_sku_mapping",
            ),
            # FSN is unique per channel when set (the primary mapping key).
            models.UniqueConstraint(
                fields=["company", "channel", "fsn"],
                condition=models.Q(fsn__gt=""),
                name="uniq_mp_sku_fsn",
            ),
        ]
        ordering = ["channel", "marketplace_sku"]
        permissions = [
            ("view_master", "Can view marketplace master data"),
            ("change_master", "Can change marketplace master data"),
        ]

    def __str__(self):
        return f"{self.channel}:{self.marketplace_sku}"


class SkuMappingOption(models.Model):
    """One SAP item a marketplace SKU/FSN MAY ship as.

    A single Flipkart product (FSN) can legitimately be fulfilled by more than one
    SAP item — e.g. an old vs a new sale-BOM code, or two comparable variants. Each
    option is a shippable choice; exactly one is the default. The operator picks the
    actual option per order at sheet processing / delivery-note cut time; when none
    is picked the default option is used. A mapping with NO options behaves exactly
    as before (its own ``fg_item_code``/``combo`` is the single item).
    """

    mapping = models.ForeignKey(
        SkuMapping, on_delete=models.CASCADE, related_name="options"
    )
    label = models.CharField(
        max_length=200, blank=True, help_text="Operator-facing name, e.g. 'SANO Sunflower'."
    )
    sku_type = models.CharField(max_length=10, choices=SkuType.choices, default=SkuType.RAW)
    fg_item_code = models.CharField(max_length=100, blank=True)
    fg_item_name = models.CharField(max_length=200, blank=True)
    combo = models.ForeignKey(
        ComboDefinition, on_delete=models.PROTECT, null=True, blank=True,
        related_name="mapping_options",
    )
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "id"]

    def __str__(self):
        target = self.fg_item_code or (self.combo.code if self.combo_id else "")
        return f"opt:{self.mapping_id}:{target}"


# ─────────────────────────────────────────────────────────────────────────────
# Orders
# ─────────────────────────────────────────────────────────────────────────────
class MarketplaceOrderStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    DISPATCHED = "DISPATCHED", "Dispatched"
    RETURNED = "RETURNED", "Returned"
    PARTIAL = "PARTIAL", "Partial"


class MarketplaceOrder(BaseModel):
    """A marketplace order — the anchor entity of the whole flow."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_orders"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order_id = models.CharField(max_length=120)
    order_date = models.DateField(null=True, blank=True)
    buyer_name = models.CharField(max_length=200, blank=True)
    sap_warehouse_code = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=20, choices=MarketplaceOrderStatus.choices,
        default=MarketplaceOrderStatus.OPEN,
    )
    # Sheet-driven intake (Flipkart order CSV) — see MARKETPLACE_FLIPKART_SHEET_FLOW.md §3
    import_batch = models.ForeignKey(
        "marketplace.OrderImportBatch", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="orders",
    )
    flipkart_shipment_id = models.CharField(max_length=120, blank=True)
    order_type = models.CharField(max_length=40, blank=True, help_text="e.g. NON_FBF")
    ship_to_name = models.CharField(max_length=200, blank=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=120, blank=True)
    pin_code = models.CharField(max_length=20, blank=True)
    dispatch_by = models.DateTimeField(null=True, blank=True)
    tracking_id = models.CharField(max_length=120, blank=True)
    is_cancelled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "order_id"], name="uniq_mp_order"
            )
        ]
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]

    def __str__(self):
        return f"{self.channel}:{self.order_id}"


class MarketplaceOrderLine(models.Model):
    order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.CASCADE, related_name="lines"
    )
    marketplace_sku = models.CharField(max_length=120)
    sku_name = models.CharField(max_length=200, blank=True)
    ordered_quantity = models.DecimalField(max_digits=18, decimal_places=3)
    # Sheet-driven intake fields
    order_item_id = models.CharField(max_length=120, blank=True)
    fsn = models.CharField(max_length=60, blank=True)
    # Each order item ships separately and can carry its OWN tracking ID (a
    # multi-item order has several). Scanning resolves the item by this, so it must
    # be stored per line, not just on the order.
    tracking_id = models.CharField(max_length=120, blank=True, db_index=True)
    # When this line's FSN can ship as more than one SAP item, the operator's pick
    # (see SkuMappingOption). Null → use the mapping's default option / single item.
    chosen_option = models.ForeignKey(
        "marketplace.SkuMappingOption", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="chosen_lines",
    )
    # For COMBO lines: which alternative was picked for each combo component,
    # as ``{"<combo_component_id>": <combo_component_option_id>}``. Kept on the line
    # (rather than a join table) so resolving a few hundred orders for the
    # delivery-note summary costs no extra queries. Unknown ids fall back to the
    # component's default option.
    component_choices = models.JSONField(default=dict, blank=True)
    order_state = models.CharField(max_length=40, blank=True, help_text="Flipkart 'Order State'")
    hsn_code = models.CharField(max_length=20, blank=True)
    unit_price = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    invoice_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    raw_row = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.order_id}:{self.marketplace_sku}"


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch (Outward) + scans
# ─────────────────────────────────────────────────────────────────────────────
class MarketplaceDispatchStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SCANNING = "SCANNING", "Scanning"
    READY = "READY", "Ready to confirm"
    CONFIRMED = "CONFIRMED", "Confirmed"
    CANCELLED = "CANCELLED", "Cancelled"


class MarketplaceSapPostStatus(models.TextChoices):
    """SAP delivery-note posting outcome for a confirmed dispatch."""

    PENDING = "PENDING", "Pending"
    POSTED = "POSTED", "Posted"
    FAILED = "FAILED", "Failed"
    # SAP routed the delivery note into an approval process; it is saved as a
    # draft awaiting approval and is finalized once approved (see delivery_note_service).
    AWAITING_APPROVAL = "AWAITING_APPROVAL", "Awaiting SAP approval"


class MarketplaceGateStatus(models.TextChoices):
    """Gate check on a CONFIRMED dispatch — the physical out-gate verification a gate
    person does before the parcels leave (modelled on gate_core's gate check)."""

    PENDING = "PENDING", "Pending gate check"
    APPROVED = "APPROVED", "Approved (OK from gate)"
    HOLD = "HOLD", "Held at gate"


class MarketplaceDispatch(BaseModel):
    """Outward dispatch session for one marketplace order (≈ SalesDispatchGateOut)."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_dispatches"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.PROTECT, related_name="dispatches"
    )
    # The sheet this parcel was WORKED under — pinned at creation, never moved.
    # ``order.import_batch`` tracks where the order is being worked NOW: when
    # Flipkart re-manifests an order it is pulled onto the newer sheet, and the
    # already-shipped dispatch must not travel with it (it was scanned, confirmed
    # and gated on its original sheet). Null for pre-existing rows / test fixtures,
    # where consumers fall back to ``order.import_batch``.
    import_batch = models.ForeignKey(
        "marketplace.OrderImportBatch", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="dispatches",
    )
    sap_warehouse_code = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=20, choices=MarketplaceDispatchStatus.choices,
        default=MarketplaceDispatchStatus.DRAFT,
    )
    # Populated on confirm. Each SAP document's identifiers are stored the moment
    # it is created so a later failure never causes a duplicate post on retry.
    sap_delivery_note_doc_entry = models.IntegerField(null=True, blank=True)
    sap_delivery_note_num = models.CharField(max_length=50, blank=True)
    # The DocDate the note was posted with — usually the cut date, but a cut may be
    # back-dated into the previous month for orders confirmed before it closed. Kept
    # locally so the accounting period is visible without querying SAP.
    sap_delivery_note_doc_date = models.DateField(null=True, blank=True)
    # When the delivery note is routed into a SAP approval process it is saved as a
    # draft; we keep the draft entry + the NumAtCard ref used, to finalize the real
    # delivery note once it is approved (reconcile_approved_delivery_notes).
    sap_delivery_note_draft_entry = models.IntegerField(null=True, blank=True)
    sap_dn_ref = models.CharField(max_length=60, blank=True)
    sap_goods_issue_doc_entry = models.IntegerField(null=True, blank=True)
    sap_goods_issue_num = models.CharField(max_length=50, blank=True)
    internal_billing = models.ForeignKey(
        "marketplace.MarketplaceOrderBilling", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="dispatch",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_dispatches_confirmed",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    # SAP delivery-note posting is best-effort: the order dispatches even if the
    # post fails, then FAILED can be retried (so an SAP outage doesn't stop work).
    sap_post_status = models.CharField(
        max_length=20, choices=MarketplaceSapPostStatus.choices,
        default=MarketplaceSapPostStatus.PENDING,
    )
    sap_error = models.TextField(blank=True, default="")
    # Gate check — the out-gate verification a gate person does on a CONFIRMED
    # dispatch (its parcels are physically ready to leave). PENDING until approved.
    # The outward trip that physically took this parcel off site. Set when the
    # gate pass is dispatched; null while the parcel is still waiting.
    gate_pass = models.ForeignKey(
        "marketplace.MarketplaceGatePass",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatches",
    )
    gate_status = models.CharField(
        max_length=12, choices=MarketplaceGateStatus.choices,
        default=MarketplaceGateStatus.PENDING,
    )
    gate_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_dispatches_gate_checked",
    )
    gate_checked_at = models.DateTimeField(null=True, blank=True)
    gate_remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]
        permissions = [
            ("view_dispatch", "Can view marketplace dispatch"),
            ("add_dispatch", "Can create marketplace dispatch"),
            ("scan_dispatch", "Can scan marketplace dispatch"),
            ("confirm_dispatch", "Can confirm marketplace dispatch"),
            ("backdate_delivery_note", "Can cut a marketplace delivery note into a previous month"),
            ("cancel_dispatch", "Can cancel marketplace dispatch"),
            ("view_reconciliation", "Can view marketplace reconciliation"),
            ("gate_check", "Can perform the marketplace out-gate check"),
        ]

    def __str__(self):
        return f"MPD-{self.pk} {self.channel}"


class MarketplaceScan(BaseModel):
    """A single item scan captured during outward dispatch.

    Modelled on ``SalesDispatchBoxScan``: a unique constraint on
    ``(dispatch, barcode_raw)`` is how a duplicate scan is detected.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_scans"
    )
    dispatch = models.ForeignKey(
        MarketplaceDispatch, on_delete=models.CASCADE, related_name="scans"
    )
    barcode_raw = models.CharField(max_length=500)
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    component_type = models.CharField(
        max_length=2, choices=ComboComponentType.choices, blank=True
    )
    source_sku = models.CharField(max_length=120, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=1)
    uom = models.CharField(max_length=20, blank=True)
    warehouse_code = models.CharField(max_length=50, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_scans",
    )
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dispatch", "barcode_raw"], name="uniq_mp_dispatch_scan"
            )
        ]
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"scan:{self.dispatch_id}:{self.barcode_raw}"


# ─────────────────────────────────────────────────────────────────────────────
# Returns (Inward) + scans
# ─────────────────────────────────────────────────────────────────────────────
class MarketplaceReturnStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SCANNING = "SCANNING", "Scanning"
    SUBMITTED = "SUBMITTED", "Submitted"
    CANCELLED = "CANCELLED", "Cancelled"


class MarketplaceReturnCondition(models.TextChoices):
    """Condition of a returned item, recorded per scan for tracking/reporting."""

    GOOD = "GOOD", "Good"
    DAMAGED = "DAMAGED", "Damaged"
    WRONG_ITEM = "WRONG_ITEM", "Wrong Item Received"
    PARTIAL = "PARTIAL", "Partial Receiving"
    MISSING = "MISSING", "Missing Item"
    EXCESS = "EXCESS", "Excess Quantity"
    PACKAGING_DAMAGED = "PACKAGING_DAMAGED", "Packaging Damaged"
    OTHER = "OTHER", "Other"


class MarketplaceReturn(BaseModel):
    """Inward/returns session for a marketplace order."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_returns"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.PROTECT, related_name="returns"
    )
    status = models.CharField(
        max_length=20, choices=MarketplaceReturnStatus.choices,
        default=MarketplaceReturnStatus.DRAFT,
    )
    internal_credit_doc_num = models.CharField(max_length=50, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_returns_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]
        constraints = [
            # A return note number is unique per company (blank = not yet submitted),
            # so concurrent submits can't silently produce duplicate RTN- numbers.
            models.UniqueConstraint(
                fields=["company", "internal_credit_doc_num"],
                condition=~models.Q(internal_credit_doc_num=""),
                name="uq_mp_return_note_per_company",
            ),
        ]
        permissions = [
            ("view_return", "Can view marketplace return"),
            ("add_return", "Can create marketplace return"),
            ("submit_return", "Can submit marketplace return"),
        ]

    def __str__(self):
        return f"MPR-{self.pk} {self.channel}"


class MarketplaceReturnScan(BaseModel):
    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_return_scans"
    )
    mp_return = models.ForeignKey(
        MarketplaceReturn, on_delete=models.CASCADE, related_name="scans"
    )
    barcode_raw = models.CharField(max_length=500)
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    component_type = models.CharField(
        max_length=2, choices=ComboComponentType.choices, blank=True
    )
    source_sku = models.CharField(max_length=120, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=1)
    uom = models.CharField(max_length=20, blank=True)
    # Condition of this returned item (operator-selected after scanning).
    condition = models.CharField(
        max_length=20, choices=MarketplaceReturnCondition.choices, blank=True,
    )
    condition_remarks = models.CharField(max_length=255, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_return_scans",
    )
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mp_return", "barcode_raw"], name="uniq_mp_return_scan"
            )
        ]
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"return-scan:{self.mp_return_id}:{self.barcode_raw}"


class MarketplaceReturnPhoto(models.Model):
    """Condition-evidence photo(s) for a returned item (1..N per scan)."""

    scan = models.ForeignKey(
        MarketplaceReturnScan, on_delete=models.CASCADE, related_name="photos"
    )
    image = models.ImageField(upload_to="marketplace/returns/")
    note = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"return-photo:{self.scan_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Internal (non-SAP) billing
# ─────────────────────────────────────────────────────────────────────────────
class MarketplaceBillingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CONFIRMED = "CONFIRMED", "Confirmed"
    SUBMITTED = "SUBMITTED", "Submitted"


class MarketplaceOrderBilling(BaseModel):
    """Internal billing document generated in JI (not posted to SAP).

    Modelled on ``dispatch_plans.TransporterAPInvoicePosting``.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_billings"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order_id = models.CharField(max_length=120)
    invoice_number = models.CharField(max_length=60, unique=True)
    buyer_name = models.CharField(max_length=200, blank=True)
    sap_delivery_note_doc_entry = models.IntegerField(null=True, blank=True)
    sap_delivery_note_num = models.CharField(max_length=50, blank=True)
    total_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0
    )
    status = models.CharField(
        max_length=20, choices=MarketplaceBillingStatus.choices,
        default=MarketplaceBillingStatus.CONFIRMED,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.invoice_number


# ─────────────────────────────────────────────────────────────────────────────
# Sheet import batch + warehouse issue request (see MARKETPLACE_FLIPKART_SHEET_FLOW.md)
# ─────────────────────────────────────────────────────────────────────────────
class OrderImportBatch(BaseModel):
    """One uploaded Flipkart order sheet — groups the orders it created/refreshed.

    Owns the consolidated stock list and the warehouse issue request(s) raised
    from it, and keeps the original CSV for the "which sheet did we issue against"
    audit + export.
    """

    class Status(models.TextChoices):
        PARSED = "PARSED", "Parsed"
        RESOLVED = "RESOLVED", "Resolved"
        REQUESTED = "REQUESTED", "Sent to warehouse"
        ISSUED = "ISSUED", "Issued"
        DISPATCHING = "DISPATCHING", "Dispatching"
        CLOSED = "CLOSED", "Closed"

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_import_batches"
    )
    channel = models.CharField(
        max_length=20, choices=MarketplaceChannel.choices,
        default=MarketplaceChannel.FLIPKART,
    )
    filename = models.CharField(max_length=255, blank=True)
    raw_file = models.FileField(upload_to="marketplace/imports/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSED)
    row_count = models.IntegerField(default=0)
    order_count = models.IntegerField(default=0)
    line_count = models.IntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]
        permissions = [
            ("import_orders", "Can import marketplace order sheets"),
            ("view_batch", "Can view marketplace import batches"),
        ]

    def __str__(self):
        return f"batch:{self.pk}:{self.filename}"


class ImportSkipReason(models.TextChoices):
    DISPATCHED = "DISPATCHED", "Already dispatched on an earlier sheet"
    DUPLICATE = "DUPLICATE", "Not re-imported (duplicate)"


class MarketplaceImportSkip(BaseModel):
    """An order present in an uploaded sheet but NOT imported into it.

    ``ingest()`` deliberately leaves an order that already has a live dispatch on
    its ORIGINAL sheet (so it doesn't reappear as already-done on the new one). That
    skip used to be invisible — the operator uploaded N rows and saw fewer, reading
    it as data loss. Each skip is recorded here so the board can show a
    "carried over from an earlier sheet" section that links to where the order lives.

    ``kept_order`` points at the existing order (its ``import_batch`` is the sheet
    it stays on); ``order_id`` / ``tracking_ids`` / ``row_count`` are denormalised
    from the uploaded CSV so the record is self-describing even if the order moves.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_import_skips",
    )
    import_batch = models.ForeignKey(
        OrderImportBatch, on_delete=models.CASCADE, related_name="skips",
    )
    kept_order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    order_id = models.CharField(max_length=120)
    reason = models.CharField(max_length=12, choices=ImportSkipReason.choices)
    row_count = models.PositiveSmallIntegerField(default=0)
    tracking_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["order_id"]
        indexes = [models.Index(fields=["company", "import_batch"])]
        constraints = [
            models.UniqueConstraint(
                fields=["import_batch", "order_id"], name="uq_mp_import_skip",
            ),
        ]

    def __str__(self):
        return f"skip:{self.import_batch_id}:{self.order_id}:{self.reason}"


class MarketplaceIssueStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SENT = "SENT", "Sent to warehouse"
    APPROVED = "APPROVED", "Approved"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially approved"
    REJECTED = "REJECTED", "Rejected"
    ISSUED = "ISSUED", "Issued"
    RECEIVED = "RECEIVED", "Received"


class MarketplaceIssueRequest(BaseModel):
    """A request to a warehouse to issue the batch's consolidated stock list.

    Mirrors ``warehouse.BOMRequest``: per-line partial approval, issue, receive.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_issue_requests"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    batch = models.ForeignKey(
        OrderImportBatch, on_delete=models.CASCADE, related_name="issue_requests"
    )
    sap_warehouse_code = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=20, choices=MarketplaceIssueStatus.choices,
        default=MarketplaceIssueStatus.DRAFT,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_issue_requests_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=255, blank=True)
    sap_issue_doc_entries = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]
        permissions = [
            ("send_issue_request", "Can send marketplace issue request to warehouse"),
            ("review_issue_request", "Can approve/reject marketplace issue request"),
            ("issue_materials", "Can issue materials for a marketplace request"),
            ("receive_issue", "Can receive issued marketplace materials"),
        ]

    def __str__(self):
        return f"MPIR-{self.pk} {self.channel}"


class MarketplaceIssueLineStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially approved"
    REJECTED = "REJECTED", "Rejected"


class MarketplacePackingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PACKING = "PACKING", "In progress"
    PACKED = "PACKED", "Packed"


class MarketplacePacking(BaseModel):
    """Packing session for one order — sits between warehouse issue and Outward.

    An order can only be dispatched once it is PACKED (item barcodes generated).
    See MARKETPLACE_FLIPKART_SHEET_FLOW.md — Packing.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_packings"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order = models.OneToOneField(
        MarketplaceOrder, on_delete=models.CASCADE, related_name="packing"
    )
    status = models.CharField(
        max_length=20, choices=MarketplacePackingStatus.choices,
        default=MarketplacePackingStatus.PENDING,
    )
    packed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_packings_packed",
    )
    packed_at = models.DateTimeField(null=True, blank=True)
    # The Flipkart Tracking ID barcode scanned to pack the order (already on the
    # shipping label, so no internal barcode is generated). See §Packing.
    pack_barcode = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("view_packing", "Can view marketplace packing"),
            ("pack_order", "Can pack marketplace orders"),
        ]

    def __str__(self):
        return f"MPPACK-{self.pk} {self.order_id}"


class MarketplacePackBarcode(models.Model):
    """A printable item label produced during packing.

    Every label for an order carries the same barcode — the Flipkart Tracking ID
    already printed on the shipping label — so it is not globally unique. The
    barcode is scannable at Outward; it resolves to an item_code + quantity so the
    dispatch scan matches an order line.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_pack_barcodes"
    )
    packing = models.ForeignKey(
        MarketplacePacking, on_delete=models.CASCADE, related_name="barcodes"
    )
    order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.CASCADE, related_name="pack_barcodes"
    )
    barcode = models.CharField(max_length=120, db_index=True)
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=1)
    uom = models.CharField(max_length=20, blank=True)
    source_sku = models.CharField(max_length=120, blank=True)
    printed = models.BooleanField(default=False)
    printed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.barcode


class MarketplaceIssueLine(models.Model):
    """One consolidated stock-list line inside a :class:`MarketplaceIssueRequest`."""

    request = models.ForeignKey(
        MarketplaceIssueRequest, on_delete=models.CASCADE, related_name="lines"
    )
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=200, blank=True)
    component_type = models.CharField(max_length=2, choices=ComboComponentType.choices)
    uom = models.CharField(max_length=20, blank=True)
    required_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    available_stock = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    approved_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    issued_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    received_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    status = models.CharField(
        max_length=20, choices=MarketplaceIssueLineStatus.choices,
        default=MarketplaceIssueLineStatus.PENDING,
    )
    reject_reason = models.CharField(max_length=255, blank=True)
    source_skus = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["component_type", "item_code"]

    def __str__(self):
        return f"issue-line:{self.request_id}:{self.item_code}"


# --------------------------------------------------------------------------- #
# Gate pass — the physical trip that takes a sheet's parcels off site
# --------------------------------------------------------------------------- #
class MarketplaceGatePassStatus(models.TextChoices):
    """The stages of one outward trip.

    Mirrors the sales-dispatch gate-out ladder (dock → weigh → print → out),
    minus the steps that only make sense for a SAP-invoiced truck.
    """

    DRAFT = "DRAFT", "Draft"
    WEIGHED = "WEIGHED", "Weighed"
    GATEPASS_PRINTED = "GATEPASS_PRINTED", "Gatepass printed"
    DISPATCHED = "DISPATCHED", "Dispatched out"
    CANCELLED = "CANCELLED", "Cancelled"


class MarketplaceGatepassSequence(models.Model):
    """Per-company, per-financial-year running number for marketplace gatepasses.

    Same shape as ``SalesDispatchGatepassSequence`` so the two read alike on a
    printed pass; the ``MKT/`` prefix is what distinguishes them.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="marketplace_gatepass_sequences",
    )
    financial_year = models.CharField(max_length=9)
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "financial_year"],
                name="unique_marketplace_gatepass_sequence",
            )
        ]

    def __str__(self):
        return f"{self.company.code} {self.financial_year} #{self.last_number}"

    @classmethod
    def next_gatepass_no(cls, company):
        from django.db import transaction

        today = timezone.localdate()
        # Indian financial year: April to March.
        start_year = today.year if today.month >= 4 else today.year - 1
        financial_year = f"{start_year}-{str(start_year + 1)[-2:]}"
        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(
                company=company,
                financial_year=financial_year,
                defaults={"last_number": 0},
            )
            sequence.last_number += 1
            sequence.save(update_fields=["last_number", "updated_at"])
            return f"MKT/{company.code}/{financial_year}/{sequence.last_number:06d}"


class MarketplaceGatePass(BaseModel):
    """One outward trip: a vehicle taking a sheet's gate-approved parcels off site.

    The marketplace equivalent of ``SalesDispatchGateOut``. A sheet's parcels are
    scanned, confirmed and gate-approved order by order; this is the single
    document that says which vehicle actually took them, what it weighed, and
    when it left.

    Vehicle, transporter and driver are held BOTH as FKs and as frozen text. The
    FK is the live master; the text is what was true when the pass was printed.
    Renaming a transporter next month must not rewrite a gatepass already issued
    — the same reason the sales-dispatch docking keeps its own copy.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_gate_passes"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    # The sheet whose parcels this trip carries.
    import_batch = models.ForeignKey(
        "marketplace.OrderImportBatch",
        on_delete=models.PROTECT,
        related_name="gate_passes",
    )

    status = models.CharField(
        max_length=20,
        choices=MarketplaceGatePassStatus.choices,
        default=MarketplaceGatePassStatus.DRAFT,
    )

    # --- vehicle / transporter / driver: live FK + frozen snapshot ---------- #
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes",
    )
    vehicle_no = models.CharField(max_length=30, blank=True)
    transporter = models.ForeignKey(
        "vehicle_management.Transporter", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes",
    )
    transporter_name = models.CharField(max_length=150, blank=True)
    transporter_gstin = models.CharField(max_length=20, blank=True)
    driver = models.ForeignKey(
        "driver_management.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes",
    )
    driver_name = models.CharField(max_length=100, blank=True)
    driver_mobile_no = models.CharField(max_length=15, blank=True)
    driver_license_no = models.CharField(max_length=50, blank=True)

    # --- weighment --------------------------------------------------------- #
    # Kept on the pass rather than reusing ``weighment.Weighment``: that model is
    # a OneToOne on VehicleEntry, and a marketplace trip has no gate-in entry to
    # hang off. Same three numbers, same rule.
    tare_weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # Derived on save. Null (not 0) until both halves are recorded, so
    # "not weighed yet" stays distinguishable from "weighed and empty".
    net_weight = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True, editable=False
    )
    weighbridge_slip_no = models.CharField(max_length=50, blank=True)
    first_weighment_at = models.DateTimeField(null=True, blank=True)
    second_weighment_at = models.DateTimeField(null=True, blank=True)

    # --- what went out ----------------------------------------------------- #
    # Frozen at dispatch: the sheet keeps changing, the trip does not.
    order_count = models.PositiveIntegerField(default=0)
    parcel_count = models.PositiveIntegerField(default=0)

    # --- gatepass ---------------------------------------------------------- #
    gatepass_no = models.CharField(max_length=80, unique=True, null=True, blank=True)
    random_code = models.CharField(max_length=50, blank=True)
    qr_payload = models.TextField(blank=True)
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes_printed",
    )
    printed_at = models.DateTimeField(null=True, blank=True)

    # --- out at the gate --------------------------------------------------- #
    gate_out_date = models.DateField(null=True, blank=True)
    out_time = models.TimeField(null=True, blank=True)
    security_name = models.CharField(max_length=100, blank=True)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes_dispatched",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True)
    cancel_reason = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marketplace_gate_passes_cancelled",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "channel", "status"]),
            models.Index(fields=["company", "import_batch"]),
            models.Index(fields=["company", "gate_out_date"]),
        ]
        permissions = [
            ("can_view_mp_gate_pass", "Can view marketplace gate passes"),
            ("can_manage_mp_gate_pass", "Can create and edit marketplace gate passes"),
            ("can_weigh_mp_gate_pass", "Can record marketplace gate pass weighment"),
            ("can_print_mp_gate_pass", "Can print a marketplace gatepass"),
            ("can_dispatch_mp_gate_pass", "Can mark a marketplace gate pass out"),
        ]

    def __str__(self):
        return f"{self.gatepass_no or f'draft-{self.pk}'} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        # Net is always derived; an operator never types it. Both halves needed --
        # a gross with no tare is not a net weight, it is half a weighment.
        if self.gross_weight is not None and self.tare_weight is not None:
            self.net_weight = self.gross_weight - self.tare_weight
        else:
            self.net_weight = None
        super().save(*args, **kwargs)

    @property
    def is_weighed(self):
        return self.net_weight is not None
