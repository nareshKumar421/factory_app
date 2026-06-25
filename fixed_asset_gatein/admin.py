from django.contrib import admin
from .models import FixedAssetGateEntry, FixedAssetItem, AssetCategory


@admin.register(AssetCategory)
class AssetCategoryAdmin(admin.ModelAdmin):
    list_display = ("category_name", "description", "is_active")
    list_display_links = ("category_name",)
    list_filter = ("is_active",)
    search_fields = ("category_name", "description")
    ordering = ("category_name",)
    list_per_page = 25


class FixedAssetItemInline(admin.TabularInline):
    model = FixedAssetItem
    extra = 0
    autocomplete_fields = ["asset_category"]
    fields = ("line_no", "asset_category", "asset_name", "serial_number", "quantity", "unit")


@admin.register(FixedAssetGateEntry)
class FixedAssetGateEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "work_order_number",
        "supplier_name",
        "item_count",
        "invoice_number",
        "created_at",
    )
    list_display_links = ("id", "work_order_number")
    list_filter = ("created_at",)
    search_fields = (
        "work_order_number",
        "supplier_name",
        "invoice_number",
        "vehicle_entry__entry_no",
        "items__asset_name",
        "items__serial_number",
        "remarks",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25

    inlines = [FixedAssetItemInline]
    raw_id_fields = ["vehicle_entry"]
    readonly_fields = ("created_at", "updated_at", "created_by", "vehicle_entry", "inward_time", "work_order_number")

    @admin.display(description="Assets")
    def item_count(self, obj):
        return obj.items.count()

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "vehicle_entry", "created_by"
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
