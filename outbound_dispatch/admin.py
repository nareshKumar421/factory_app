from django.contrib import admin
from .models import (
    ShipmentOrder, ShipmentOrderItem,
    PickTask, OutboundLoadRecord, GoodsIssuePosting,
)


class ShipmentOrderItemInline(admin.TabularInline):
    model = ShipmentOrderItem
    extra = 0
    readonly_fields = ("picked_qty", "packed_qty", "loaded_qty", "pick_status")


@admin.register(ShipmentOrder)
class ShipmentOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "sap_doc_num", "customer_name", "status",
        "scheduled_date", "dock_bay", "created_at",
    )
    list_filter = ("status", "scheduled_date", "company")
    search_fields = ("sap_doc_num", "customer_name", "customer_code")
    readonly_fields = ("created_at", "updated_at", "created_by")
    inlines = [ShipmentOrderItemInline]


@admin.register(ShipmentOrderItem)
class ShipmentOrderItemAdmin(admin.ModelAdmin):
    list_display = (
        "id", "shipment_order", "item_code", "item_name",
        "ordered_qty", "picked_qty", "packed_qty", "loaded_qty", "pick_status",
    )
    list_filter = ("pick_status",)
    search_fields = ("item_code", "item_name")


@admin.register(PickTask)
class PickTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id", "shipment_item", "assigned_to", "pick_location",
        "pick_qty", "actual_qty", "status", "picked_at",
    )
    list_filter = ("status",)


@admin.register(OutboundLoadRecord)
class OutboundLoadRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id", "shipment_order", "trailer_condition",
        "supervisor_confirmed", "loading_started_at", "loading_completed_at",
    )
    list_filter = ("trailer_condition", "supervisor_confirmed")


@admin.register(GoodsIssuePosting)
class GoodsIssuePostingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "shipment_order", "sap_doc_num", "status",
        "posted_at", "retry_count",
    )
    list_filter = ("status",)
    readonly_fields = ("created_at", "updated_at")
