from django.contrib import admin

from .models import Dismantle, DismantleComponent


class DismantleComponentInline(admin.TabularInline):
    model = DismantleComponent
    extra = 0


@admin.register(Dismantle)
class DismantleAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no",
        "company",
        "status",
        "item_code",
        "batch_number",
        "quantity",
        "warehouse_code",
        "sap_order_doc_num",
        "created_at",
    )
    list_filter = ("company", "status", "source", "warehouse_code")
    search_fields = (
        "entry_no",
        "item_code",
        "item_name",
        "batch_number",
        "sap_order_doc_num",
        "sap_receipt_doc_num",
        "sap_issue_doc_num",
    )
    inlines = [DismantleComponentInline]
    # The SAP keys are written by the posting run, never by hand: a typed
    # DocEntry would make the app claim a document that is not this one's.
    readonly_fields = (
        "sap_order_doc_entry",
        "sap_order_doc_num",
        "sap_receipt_doc_entry",
        "sap_receipt_doc_num",
        "sap_issue_doc_entry",
        "sap_issue_doc_num",
    )
