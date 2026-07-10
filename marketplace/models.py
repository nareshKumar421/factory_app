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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "channel", "marketplace_sku"],
                name="uniq_mp_sku_mapping",
            )
        ]
        ordering = ["channel", "marketplace_sku"]
        permissions = [
            ("view_master", "Can view marketplace master data"),
            ("change_master", "Can change marketplace master data"),
        ]

    def __str__(self):
        return f"{self.channel}:{self.marketplace_sku}"


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


class MarketplaceDispatch(BaseModel):
    """Outward dispatch session for one marketplace order (≈ SalesDispatchGateOut)."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.PROTECT, related_name="marketplace_dispatches"
    )
    channel = models.CharField(max_length=20, choices=MarketplaceChannel.choices)
    order = models.ForeignKey(
        MarketplaceOrder, on_delete=models.PROTECT, related_name="dispatches"
    )
    sap_warehouse_code = models.CharField(max_length=50, blank=True)
    status = models.CharField(
        max_length=20, choices=MarketplaceDispatchStatus.choices,
        default=MarketplaceDispatchStatus.DRAFT,
    )
    # Populated on confirm
    sap_delivery_note_doc_entry = models.IntegerField(null=True, blank=True)
    sap_delivery_note_num = models.CharField(max_length=50, blank=True)
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

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "channel", "status"])]
        permissions = [
            ("view_dispatch", "Can view marketplace dispatch"),
            ("add_dispatch", "Can create marketplace dispatch"),
            ("scan_dispatch", "Can scan marketplace dispatch"),
            ("confirm_dispatch", "Can confirm marketplace dispatch"),
            ("cancel_dispatch", "Can cancel marketplace dispatch"),
            ("view_reconciliation", "Can view marketplace reconciliation"),
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
