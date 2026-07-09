from django.contrib import admin

from .models import (
    ReturnableGatePass,
    ReturnableGatePassAttachment,
    ReturnableGatePassItem,
    ReturnableGatePassLog,
    ReturnableGatePassSequence,
    ReturnableReturnEvent,
    ReturnableReturnEventItem,
)


class ReturnableGatePassItemInline(admin.TabularInline):
    model = ReturnableGatePassItem
    extra = 0
    fields = ("line_num", "item_name", "serial_no", "uom", "quantity_out", "quantity_returned", "condition_out")
    readonly_fields = ("quantity_returned",)


class ReturnableGatePassAttachmentInline(admin.TabularInline):
    model = ReturnableGatePassAttachment
    extra = 0


@admin.register(ReturnableGatePass)
class ReturnableGatePassAdmin(admin.ModelAdmin):
    list_display = (
        "pass_no",
        "company",
        "party_name",
        "purpose",
        "status",
        "expected_return_date",
        "is_overdue",
        "gate_out_at",
    )
    list_filter = ("status", "purpose", "is_overdue", "company")
    search_fields = ("pass_no", "party_name", "items__item_name", "items__serial_no")
    date_hierarchy = "created_at"
    readonly_fields = ("pass_no", "created_at", "updated_at")
    inlines = [ReturnableGatePassItemInline, ReturnableGatePassAttachmentInline]


class ReturnableReturnEventItemInline(admin.TabularInline):
    model = ReturnableReturnEventItem
    extra = 0


@admin.register(ReturnableReturnEvent)
class ReturnableReturnEventAdmin(admin.ModelAdmin):
    list_display = ("event_ref", "gate_pass", "returned_at", "verified_by", "acknowledged_at")
    list_filter = ("company",)
    search_fields = ("event_ref", "gate_pass__pass_no")
    inlines = [ReturnableReturnEventItemInline]


@admin.register(ReturnableGatePassLog)
class ReturnableGatePassLogAdmin(admin.ModelAdmin):
    list_display = ("gate_pass", "action", "actor", "at")
    list_filter = ("action",)
    search_fields = ("gate_pass__pass_no",)


@admin.register(ReturnableGatePassSequence)
class ReturnableGatePassSequenceAdmin(admin.ModelAdmin):
    list_display = ("company", "financial_year", "last_number", "updated_at")
