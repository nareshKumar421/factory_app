from django.contrib import admin

from .models import ShortDispatch, ShortDispatchItem


class ShortDispatchItemInline(admin.TabularInline):
    model = ShortDispatchItem
    extra = 0


@admin.register(ShortDispatch)
class ShortDispatchAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no",
        "company",
        "sap_invoice_doc_num",
        "customer_name",
        "warehouse_code",
        "sap_return_doc_num",
        "created_at",
    )
    list_filter = ("company", "warehouse_code")
    search_fields = (
        "entry_no",
        "sap_invoice_doc_num",
        "sap_return_doc_num",
        "customer_code",
        "customer_name",
    )
    inlines = [ShortDispatchItemInline]
