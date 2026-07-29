from django.contrib import admin

from .models import WarehouseMovement, WarehouseMovementLine, WarehouseRole


@admin.register(WarehouseRole)
class WarehouseRoleAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "whs_code",
        "warehouse_name",
        "role",
        "family",
        "is_grpo_target",
        "is_bom_issue_point",
        "feeds_whs_code",
        "needs_review",
        "is_active",
    )
    list_filter = ("company", "role", "family", "is_grpo_target",
                   "is_bom_issue_point", "needs_review", "is_active")
    search_fields = ("whs_code", "warehouse_name", "notes")
    list_editable = ("role", "is_grpo_target", "is_bom_issue_point",
                     "feeds_whs_code", "needs_review", "is_active")


class WarehouseMovementLineInline(admin.TabularInline):
    model = WarehouseMovementLine
    extra = 0


@admin.register(WarehouseMovement)
class WarehouseMovementAdmin(admin.ModelAdmin):
    list_display = (
        "id", "company", "movement_type", "status",
        "from_whs_code", "to_whs_code", "sap_object_type", "sap_doc_entry",
        "itr_doc_entry", "created_at",
    )
    list_filter = ("company", "movement_type", "status")
    search_fields = ("sap_doc_num", "reference", "from_whs_code", "to_whs_code")
    readonly_fields = (
        "company", "movement_type", "status", "from_whs_code", "to_whs_code",
        "sap_object_type", "sap_doc_entry", "sap_doc_num", "itr_doc_entry",
        "posting_date", "error_message", "payload_preview", "reference",
        "created_by", "created_at", "updated_at",
    )
    inlines = [WarehouseMovementLineInline]
