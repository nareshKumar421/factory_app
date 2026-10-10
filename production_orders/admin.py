from django.contrib import admin

from .models import ProductionOrderEntry, ProductionOrderEntryLine


class ProductionOrderEntryLineInline(admin.TabularInline):
    model = ProductionOrderEntryLine
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in ProductionOrderEntryLine._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ProductionOrderEntry)
class ProductionOrderEntryAdmin(admin.ModelAdmin):
    """Read-only: an entry moves only through the API, which posts to SAP as
    the person taking each step. Editing a status here would skip SAP."""

    list_display = (
        "entry_no", "company", "item_code", "quantity", "batch_number", "status",
        "sap_order_num", "posting_date", "created_by",
    )
    list_filter = ("company", "status")
    search_fields = ("entry_no", "item_code", "item_name", "batch_number")
    readonly_fields = [f.name for f in ProductionOrderEntry._meta.fields]
    inlines = [ProductionOrderEntryLineInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
