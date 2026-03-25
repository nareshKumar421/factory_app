from django.contrib import admin
from .models import OutboundGateEntry, OutboundPurpose


@admin.register(OutboundPurpose)
class OutboundPurposeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)


@admin.register(OutboundGateEntry)
class OutboundGateEntryAdmin(admin.ModelAdmin):
    list_display = (
        "vehicle_entry",
        "customer_name",
        "sales_order_ref",
        "vehicle_empty_confirmed",
        "assigned_zone",
        "assigned_bay",
        "arrival_time",
        "released_for_loading_at",
    )
    list_filter = (
        "vehicle_empty_confirmed",
        "assigned_zone",
        "created_at",
    )
    search_fields = (
        "vehicle_entry__entry_no",
        "vehicle_entry__vehicle__vehicle_number",
        "customer_name",
        "customer_code",
        "sales_order_ref",
    )
    raw_id_fields = ("vehicle_entry",)
    readonly_fields = ("created_at", "updated_at", "arrival_time")
