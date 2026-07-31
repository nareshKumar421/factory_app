from django.contrib import admin

from .models import FGReceipt


@admin.register(FGReceipt)
class FGReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "po_number",
        "supplier_code",
        "supplier_name",
        "vehicle_entry",
        "po_date",
        "created_at",
    )
    search_fields = ("po_number", "supplier_code", "supplier_name")
    list_filter = ("po_date", "created_at")
    ordering = ("-created_at",)
