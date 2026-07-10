from django.contrib import admin

from .models import (
    ComboComponent,
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceOrderLine,
    MarketplaceReturn,
    MarketplaceScan,
    MarketplaceWarehouse,
    SkuMapping,
)


class ComboComponentInline(admin.TabularInline):
    model = ComboComponent
    extra = 1


class OrderLineInline(admin.TabularInline):
    model = MarketplaceOrderLine
    extra = 0


@admin.register(MarketplaceWarehouse)
class MarketplaceWarehouseAdmin(admin.ModelAdmin):
    list_display = ("channel", "name", "sap_warehouse_code", "facility_code", "company", "is_active")
    list_filter = ("channel", "company", "is_active")
    search_fields = ("name", "sap_warehouse_code", "facility_code")


@admin.register(ComboDefinition)
class ComboDefinitionAdmin(admin.ModelAdmin):
    list_display = ("channel", "code", "name", "company", "is_active")
    list_filter = ("channel", "company", "is_active")
    search_fields = ("code", "name")
    inlines = [ComboComponentInline]


@admin.register(SkuMapping)
class SkuMappingAdmin(admin.ModelAdmin):
    list_display = ("channel", "marketplace_sku", "sku_type", "fg_item_code", "combo", "company", "is_active")
    list_filter = ("channel", "sku_type", "company", "is_active")
    search_fields = ("marketplace_sku", "sku_name", "fg_item_code")


@admin.register(MarketplaceOrder)
class MarketplaceOrderAdmin(admin.ModelAdmin):
    list_display = ("channel", "order_id", "status", "buyer_name", "company", "created_at")
    list_filter = ("channel", "status", "company")
    search_fields = ("order_id", "buyer_name")
    inlines = [OrderLineInline]


@admin.register(MarketplaceDispatch)
class MarketplaceDispatchAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "order", "status", "sap_delivery_note_num", "company", "created_at")
    list_filter = ("channel", "status", "company")
    search_fields = ("order__order_id", "sap_delivery_note_num")


@admin.register(MarketplaceReturn)
class MarketplaceReturnAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "order", "status", "internal_credit_doc_num", "company", "created_at")
    list_filter = ("channel", "status", "company")
    search_fields = ("order__order_id",)


@admin.register(MarketplaceOrderBilling)
class MarketplaceOrderBillingAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "channel", "order_id", "status", "total_amount", "company", "created_at")
    list_filter = ("channel", "status", "company")
    search_fields = ("invoice_number", "order_id")


admin.site.register(MarketplaceScan)
