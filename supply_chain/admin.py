from django.contrib import admin

from .models import (
    AlarmDispatch,
    AlarmSubscription,
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SalesTrend,
    SupplyChainPolicy,
)


@admin.register(MaterialLeadTime)
class MaterialLeadTimeAdmin(admin.ModelAdmin):
    list_display = (
        "material_code", "material_name", "material_type", "supplier_name",
        "lead_time_days", "moq", "unit", "company_code", "is_active",
    )
    list_filter = ("company_code", "material_type", "is_active")
    search_fields = ("material_code", "material_name", "supplier_name")


@admin.register(MachineCapacity)
class MachineCapacityAdmin(admin.ModelAdmin):
    list_display = (
        "machine_id", "name", "location", "pack_type", "output_per_hour",
        "available_hours", "changeover_minutes", "company_code", "is_active",
    )
    list_filter = ("company_code", "location", "is_active")
    search_fields = ("machine_id", "name", "pack_type")

    @admin.display(description="Hours/month")
    def available_hours(self, obj):
        return obj.available_hours


@admin.register(MaterialMachineMap)
class MaterialMachineMapAdmin(admin.ModelAdmin):
    list_display = (
        "sku_code", "sku_name", "brand", "pack_type", "primary_machine_id",
        "alternate_machine_ids", "output_on_primary", "company_code", "is_active",
    )
    list_filter = ("company_code", "brand", "pack_type", "is_active")
    search_fields = ("sku_code", "sku_name", "primary_machine_id")


@admin.register(SupplyChainPolicy)
class SupplyChainPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "company_code", "floor_percent", "floor_basis", "urgency_window_days",
        "use_net_of_open_po", "apply_moq_rounding", "include_changeover_in_capacity",
    )


@admin.register(ReferenceImport)
class ReferenceImportAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "company_code", "filename", "lead_times_loaded",
        "machines_loaded", "mappings_loaded", "examples_skipped", "imported_by",
    )
    list_filter = ("company_code",)
    readonly_fields = tuple(f.name for f in ReferenceImport._meta.fields)


@admin.register(SalesTrend)
class SalesTrendAdmin(admin.ModelAdmin):
    list_display = ("item_code", "item_name", "three_month_qty", "source",
                    "company_code", "captured_at")
    list_filter = ("company_code", "source")
    search_fields = ("item_code", "item_name")


@admin.register(AlarmSubscription)
class AlarmSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("label", "permission_codename", "material_type", "include_overdue",
                    "include_order_now", "include_missing_lead_time", "include_capacity",
                    "company_code", "is_active")
    list_filter = ("company_code", "is_active", "material_type")
    search_fields = ("label", "permission_codename")


@admin.register(AlarmDispatch)
class AlarmDispatchAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "company_code", "subscription", "title",
                    "recipients", "overdue_count", "order_now_count")
    list_filter = ("company_code",)
    readonly_fields = tuple(f.name for f in AlarmDispatch._meta.fields)
