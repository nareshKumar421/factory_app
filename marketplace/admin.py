"""Django admin for the Marketplace (Flipkart/Amazon) module.

Covers the whole pipeline — master data (warehouses, settings, SKU/combo
mappings), sheet intake (import batches, warehouse issue requests), packing,
outward dispatch + scans, returns + scans/photos, and internal billing.

Conventions used throughout:

* :class:`_BaseModelAdmin` stamps ``created_by``/``updated_by`` on save and marks
  the whole audit block read-only, so every ``BaseModel`` collection behaves the
  same way.
* Status columns render as colour badges (:func:`_badge`) so the list view is
  scannable at a glance.
* Heavy foreign keys use ``autocomplete_fields`` (in-app targets that expose
  ``search_fields``) or ``raw_id_fields`` (user/audit FKs) instead of loading
  large ``<select>`` dropdowns.
"""
from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html

from .models import (
    ComboComponent,
    ComboComponentOption,
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceIssueLine,
    MarketplaceIssueRequest,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceOrderLine,
    MarketplacePackBarcode,
    MarketplacePacking,
    MarketplaceReturn,
    MarketplaceReturnPhoto,
    MarketplaceReturnScan,
    MarketplaceScan,
    MarketplaceSettings,
    MarketplaceWarehouse,
    OrderImportBatch,
    SkuMapping,
    SkuMappingOption,
)

# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────
# One colour table shared by every status-like column in the module.
_STATUS_COLORS = {
    # generic lifecycle
    "DRAFT": "#6b7280", "OPEN": "#2563eb", "PENDING": "#d97706",
    "CANCELLED": "#991b1b", "REJECTED": "#dc2626", "CLOSED": "#475569",
    # dispatch / order
    "SCANNING": "#0d9488", "READY": "#7c3aed", "CONFIRMED": "#16a34a",
    "DISPATCHED": "#16a34a", "DISPATCHING": "#0d9488", "PARTIAL": "#d97706",
    # sap post
    "POSTED": "#16a34a", "FAILED": "#dc2626", "AWAITING_APPROVAL": "#7c3aed",
    # issue request / lines
    "SENT": "#2563eb", "APPROVED": "#16a34a", "PARTIALLY_APPROVED": "#d97706",
    "ISSUED": "#0d9488", "RECEIVED": "#16a34a", "REQUESTED": "#2563eb",
    "RESOLVED": "#7c3aed", "PARSED": "#6b7280",
    # packing
    "PACKING": "#0d9488", "PACKED": "#16a34a",
    # billing
    "SUBMITTED": "#16a34a",
    # return condition
    "GOOD": "#16a34a", "DAMAGED": "#dc2626", "WRONG_ITEM": "#dc2626",
    "MISSING": "#991b1b", "EXCESS": "#d97706", "PACKAGING_DAMAGED": "#d97706",
    "OTHER": "#6b7280",
}


def _badge(value, get_label=None):
    """Render a coloured pill for a status/enum value (``—`` when empty)."""
    if not value:
        return format_html('<span style="color:#9ca3af;">—</span>')
    color = _STATUS_COLORS.get(value, "#374151")
    label = get_label(value) if get_label else value
    return format_html(
        '<span style="background:{}1a;color:{};border:1px solid {}55;'
        'padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;'
        'white-space:nowrap;">{}</span>',
        color, color, color, label,
    )


def _status_col(field="status", header="Status"):
    """Build a ``list_display`` callable that badges ``obj.<field>``.

    Uses ``get_<field>_display`` for the human label when the field has choices.
    """

    @admin.display(description=header, ordering=field)
    def col(self, obj):
        raw = getattr(obj, field)
        getter = getattr(obj, f"get_{field}_display", None)
        return _badge(raw, (lambda _v: getter()) if getter else None)

    return col


# ─────────────────────────────────────────────────────────────────────────────
# Base admin — audit stamping + read-only audit block
# ─────────────────────────────────────────────────────────────────────────────
_AUDIT_FIELDS = ("created_at", "updated_at", "created_by", "updated_by")


class _BaseModelAdmin(admin.ModelAdmin):
    """Shared behaviour for every ``gate_core.BaseModel`` collection."""

    save_on_top = True
    list_per_page = 50

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        for f in _AUDIT_FIELDS:
            if f not in ro:
                ro.append(f)
        return ro

    def save_model(self, request, obj, form, change):
        if not change and getattr(obj, "created_by_id", None) is None:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ─────────────────────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────────────────────
class ComboComponentInline(admin.TabularInline):
    model = ComboComponent
    extra = 1
    fields = ("component_type", "item_code", "item_name", "quantity", "uom")
    show_change_link = True


class ComboComponentOptionInline(admin.TabularInline):
    model = ComboComponentOption
    extra = 1
    fields = ("item_code", "item_name", "is_default")


class SkuMappingOptionInline(admin.TabularInline):
    model = SkuMappingOption
    extra = 1
    fields = ("label", "sku_type", "fg_item_code", "fg_item_name", "combo", "is_default")
    autocomplete_fields = ("combo",)


class OrderLineInline(admin.TabularInline):
    model = MarketplaceOrderLine
    extra = 0
    fields = (
        "marketplace_sku", "sku_name", "fsn", "ordered_quantity",
        "tracking_id", "order_state", "unit_price", "invoice_amount",
    )
    raw_id_fields = ("chosen_option",)
    show_change_link = True


class DispatchScanInline(admin.TabularInline):
    model = MarketplaceScan
    extra = 0
    fields = (
        "barcode_raw", "item_code", "item_name", "component_type",
        "quantity", "uom", "warehouse_code", "scanned_by", "scanned_at",
    )
    raw_id_fields = ("scanned_by",)
    readonly_fields = ("scanned_at",)


class ReturnScanInline(admin.TabularInline):
    model = MarketplaceReturnScan
    extra = 0
    fields = (
        "barcode_raw", "item_code", "item_name", "quantity", "uom",
        "condition", "condition_remarks", "scanned_by", "scanned_at",
    )
    raw_id_fields = ("scanned_by",)
    readonly_fields = ("scanned_at",)
    show_change_link = True


class ReturnPhotoInline(admin.TabularInline):
    model = MarketplaceReturnPhoto
    extra = 1
    fields = ("image", "note", "uploaded_at")
    readonly_fields = ("uploaded_at",)


class IssueLineInline(admin.TabularInline):
    model = MarketplaceIssueLine
    extra = 0
    fields = (
        "item_code", "item_name", "component_type", "uom", "required_qty",
        "available_stock", "approved_qty", "issued_qty", "received_qty",
        "status", "reject_reason",
    )


class PackBarcodeInline(admin.TabularInline):
    model = MarketplacePackBarcode
    extra = 0
    fields = ("barcode", "item_code", "item_name", "quantity", "uom", "source_sku", "printed", "printed_at")
    readonly_fields = ("created_at",)


# ─────────────────────────────────────────────────────────────────────────────
# Master data
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceWarehouse)
class MarketplaceWarehouseAdmin(_BaseModelAdmin):
    list_display = (
        "name", "channel", "sap_warehouse_code", "facility_code",
        "post_goods_issue", "is_default", "company", "is_active",
    )
    list_filter = ("channel", "company", "is_active", "is_default", "post_goods_issue")
    search_fields = ("name", "sap_warehouse_code", "facility_code", "sap_customer_card_code")
    autocomplete_fields = ("company",)
    list_select_related = ("company",)
    fieldsets = (
        (None, {"fields": ("company", "channel", "name", "is_active")}),
        ("SAP godown", {"fields": ("sap_warehouse_code", "sap_customer_card_code", "facility_code")}),
        ("Delivery-note posting", {
            "fields": ("sap_series", "sap_tax_code", "sap_branch_id", "shipto_by_state",
                       "post_goods_issue", "is_default"),
        }),
        ("Audit", {"fields": _AUDIT_FIELDS, "classes": ("collapse",)}),
    )


@admin.register(MarketplaceSettings)
class MarketplaceSettingsAdmin(_BaseModelAdmin):
    list_display = ("company", "channel", "skip_packing", "defer_delivery_note", "is_active")
    list_filter = ("channel", "company", "skip_packing", "defer_delivery_note", "is_active")
    autocomplete_fields = ("company",)
    list_select_related = ("company",)


@admin.register(ComboDefinition)
class ComboDefinitionAdmin(_BaseModelAdmin):
    list_display = ("code", "name", "channel", "component_count", "company", "is_active")
    list_filter = ("channel", "company", "is_active")
    search_fields = ("code", "name")
    autocomplete_fields = ("company",)
    list_select_related = ("company",)
    inlines = [ComboComponentInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_ncomp=Count("components"))

    @admin.display(description="Components", ordering="_ncomp")
    def component_count(self, obj):
        return obj._ncomp


@admin.register(ComboComponent)
class ComboComponentAdmin(admin.ModelAdmin):
    list_display = ("combo", "component_type", "item_code", "item_name", "quantity", "uom")
    list_filter = ("component_type",)
    search_fields = ("item_code", "item_name", "combo__code", "combo__name")
    autocomplete_fields = ("combo",)
    list_select_related = ("combo",)
    inlines = [ComboComponentOptionInline]


@admin.register(SkuMapping)
class SkuMappingAdmin(_BaseModelAdmin):
    list_display = (
        "marketplace_sku", "fsn", "channel", "sku_type", "fg_item_code",
        "combo", "option_count", "company", "is_active",
    )
    list_filter = ("channel", "sku_type", "company", "is_active")
    search_fields = ("marketplace_sku", "sku_name", "fsn", "fg_item_code", "fg_item_name")
    autocomplete_fields = ("company", "combo")
    list_select_related = ("company", "combo")
    inlines = [SkuMappingOptionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_nopt=Count("options"))

    @admin.display(description="Options", ordering="_nopt")
    def option_count(self, obj):
        return obj._nopt or "—"


# ─────────────────────────────────────────────────────────────────────────────
# Orders
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceOrder)
class MarketplaceOrderAdmin(_BaseModelAdmin):
    list_display = (
        "order_id", "channel", "status_badge", "buyer_name", "order_date",
        "sap_warehouse_code", "is_cancelled", "company", "created_at",
    )
    list_filter = ("channel", "status", "company", "is_cancelled", "order_type")
    search_fields = ("order_id", "buyer_name", "ship_to_name", "tracking_id", "flipkart_shipment_id")
    autocomplete_fields = ("company",)
    raw_id_fields = ("import_batch",)
    list_select_related = ("company",)
    date_hierarchy = "created_at"
    inlines = [OrderLineInline]
    status_badge = _status_col()
    fieldsets = (
        (None, {"fields": ("company", "channel", "order_id", "status", "order_date", "is_cancelled")}),
        ("Buyer / shipping", {
            "fields": ("buyer_name", "ship_to_name", "address_line1", "address_line2",
                       "city", "state", "pin_code"),
        }),
        ("Fulfilment", {
            "fields": ("sap_warehouse_code", "import_batch", "flipkart_shipment_id",
                       "order_type", "dispatch_by", "tracking_id"),
        }),
        ("Audit", {"fields": _AUDIT_FIELDS, "classes": ("collapse",)}),
    )


@admin.register(MarketplaceOrderLine)
class MarketplaceOrderLineAdmin(admin.ModelAdmin):
    list_display = (
        "order", "marketplace_sku", "fsn", "ordered_quantity",
        "tracking_id", "order_state", "unit_price", "invoice_amount",
    )
    list_filter = ("order_state", "order__channel")
    search_fields = ("marketplace_sku", "sku_name", "fsn", "tracking_id", "order__order_id")
    autocomplete_fields = ("order",)
    raw_id_fields = ("chosen_option",)
    list_select_related = ("order",)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch (Outward) + scans
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceDispatch)
class MarketplaceDispatchAdmin(_BaseModelAdmin):
    list_display = (
        "id", "channel", "order", "status_badge", "sap_post_badge",
        "sap_delivery_note_num", "confirmed_by", "confirmed_at", "company",
    )
    list_filter = ("channel", "status", "sap_post_status", "company")
    search_fields = ("order__order_id", "sap_delivery_note_num", "sap_goods_issue_num", "sap_dn_ref")
    autocomplete_fields = ("company", "order")
    raw_id_fields = ("confirmed_by", "internal_billing")
    list_select_related = ("company", "order", "confirmed_by")
    date_hierarchy = "created_at"
    inlines = [DispatchScanInline]
    status_badge = _status_col()
    sap_post_badge = _status_col("sap_post_status", "SAP post")
    fieldsets = (
        (None, {"fields": ("company", "channel", "order", "sap_warehouse_code", "status")}),
        ("SAP delivery note", {
            "fields": ("sap_post_status", "sap_delivery_note_doc_entry", "sap_delivery_note_num",
                       "sap_delivery_note_draft_entry", "sap_dn_ref",
                       "sap_goods_issue_doc_entry", "sap_goods_issue_num", "sap_error"),
        }),
        ("Confirmation", {"fields": ("internal_billing", "confirmed_by", "confirmed_at", "cancel_reason")}),
        ("Audit", {"fields": _AUDIT_FIELDS, "classes": ("collapse",)}),
    )


@admin.register(MarketplaceScan)
class MarketplaceScanAdmin(_BaseModelAdmin):
    list_display = (
        "id", "dispatch", "barcode_raw", "item_code", "item_name",
        "component_type", "quantity", "scanned_by", "scanned_at",
    )
    list_filter = ("component_type", "company")
    search_fields = ("barcode_raw", "item_code", "item_name", "source_sku", "dispatch__order__order_id")
    autocomplete_fields = ("company", "dispatch")
    raw_id_fields = ("scanned_by",)
    list_select_related = ("dispatch", "scanned_by")
    date_hierarchy = "scanned_at"


# ─────────────────────────────────────────────────────────────────────────────
# Returns (Inward) + scans
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceReturn)
class MarketplaceReturnAdmin(_BaseModelAdmin):
    list_display = (
        "id", "channel", "order", "status_badge", "internal_credit_doc_num",
        "submitted_by", "submitted_at", "company",
    )
    list_filter = ("channel", "status", "company")
    search_fields = ("order__order_id", "internal_credit_doc_num")
    autocomplete_fields = ("company", "order")
    raw_id_fields = ("submitted_by",)
    list_select_related = ("company", "order", "submitted_by")
    date_hierarchy = "created_at"
    inlines = [ReturnScanInline]
    status_badge = _status_col()


@admin.register(MarketplaceReturnScan)
class MarketplaceReturnScanAdmin(_BaseModelAdmin):
    list_display = (
        "id", "mp_return", "barcode_raw", "item_code", "condition_badge",
        "quantity", "scanned_by", "scanned_at",
    )
    list_filter = ("condition", "component_type", "company")
    search_fields = ("barcode_raw", "item_code", "item_name", "mp_return__order__order_id")
    autocomplete_fields = ("company", "mp_return")
    raw_id_fields = ("scanned_by",)
    list_select_related = ("mp_return", "scanned_by")
    inlines = [ReturnPhotoInline]
    condition_badge = _status_col("condition", "Condition")


# ─────────────────────────────────────────────────────────────────────────────
# Internal billing
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceOrderBilling)
class MarketplaceOrderBillingAdmin(_BaseModelAdmin):
    list_display = (
        "invoice_number", "channel", "order_id", "status_badge",
        "total_amount", "sap_delivery_note_num", "company", "created_at",
    )
    list_filter = ("channel", "status", "company")
    search_fields = ("invoice_number", "order_id", "buyer_name", "sap_delivery_note_num")
    autocomplete_fields = ("company",)
    list_select_related = ("company",)
    date_hierarchy = "created_at"
    status_badge = _status_col()


# ─────────────────────────────────────────────────────────────────────────────
# Sheet intake: import batches + warehouse issue requests
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(OrderImportBatch)
class OrderImportBatchAdmin(_BaseModelAdmin):
    list_display = (
        "id", "filename", "channel", "status_badge", "row_count",
        "order_count", "line_count", "company", "created_at",
    )
    list_filter = ("channel", "status", "company")
    search_fields = ("filename",)
    autocomplete_fields = ("company",)
    list_select_related = ("company",)
    date_hierarchy = "created_at"
    status_badge = _status_col()


@admin.register(MarketplaceIssueRequest)
class MarketplaceIssueRequestAdmin(_BaseModelAdmin):
    list_display = (
        "id", "channel", "batch", "status_badge", "sap_warehouse_code",
        "reviewed_by", "reviewed_at", "company",
    )
    list_filter = ("channel", "status", "company")
    search_fields = ("batch__filename", "sap_warehouse_code")
    autocomplete_fields = ("company", "batch")
    raw_id_fields = ("reviewed_by",)
    list_select_related = ("company", "batch", "reviewed_by")
    date_hierarchy = "created_at"
    inlines = [IssueLineInline]
    status_badge = _status_col()


@admin.register(MarketplaceIssueLine)
class MarketplaceIssueLineAdmin(admin.ModelAdmin):
    list_display = (
        "request", "item_code", "item_name", "component_type", "required_qty",
        "approved_qty", "issued_qty", "received_qty", "status_badge",
    )
    list_filter = ("status", "component_type")
    search_fields = ("item_code", "item_name", "request__batch__filename")
    autocomplete_fields = ("request",)
    list_select_related = ("request",)
    status_badge = _status_col()


# ─────────────────────────────────────────────────────────────────────────────
# Packing
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplacePacking)
class MarketplacePackingAdmin(_BaseModelAdmin):
    list_display = (
        "id", "channel", "order", "status_badge", "pack_barcode",
        "packed_by", "packed_at", "company",
    )
    list_filter = ("channel", "status", "company")
    search_fields = ("order__order_id", "pack_barcode")
    autocomplete_fields = ("company", "order")
    raw_id_fields = ("packed_by",)
    list_select_related = ("company", "order", "packed_by")
    date_hierarchy = "created_at"
    inlines = [PackBarcodeInline]
    status_badge = _status_col()


@admin.register(MarketplacePackBarcode)
class MarketplacePackBarcodeAdmin(admin.ModelAdmin):
    list_display = (
        "id", "barcode", "packing", "order", "item_code", "item_name",
        "quantity", "printed", "printed_at",
    )
    list_filter = ("printed", "company")
    search_fields = ("barcode", "item_code", "item_name", "order__order_id")
    autocomplete_fields = ("company", "packing", "order")
    list_select_related = ("packing", "order")
    date_hierarchy = "created_at"
