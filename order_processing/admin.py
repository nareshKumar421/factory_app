from django.contrib import admin

from .models import OmsOrder, OmsOrderLine, OmsSyncRun, ProcessingEvent


class OmsOrderLineInline(admin.TabularInline):
    model = OmsOrderLine
    extra = 0
    fields = ("item_code", "item_name", "category", "quantity", "pack_size",
              "cases", "litres", "warehouse_code", "issues")
    readonly_fields = fields
    can_delete = False


@admin.register(OmsOrder)
class OmsOrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "customer_name", "oms_status", "state",
                    "company_code", "delivery_date", "sap_created", "oms_updated_at")
    list_filter = ("state", "oms_status", "company_code", "sap_created", "quotation_cancelled")
    search_fields = ("order_number", "customer_code", "customer_name", "sap_doc_number")
    date_hierarchy = "oms_created_at"
    inlines = [OmsOrderLineInline]
    # The mirror is OMS's data. Editing it here would be overwritten on the next
    # sync and would mislead whoever made the edit.
    readonly_fields = tuple(
        f.name for f in OmsOrder._meta.fields if f.name != "state"
    )


@admin.register(OmsSyncRun)
class OmsSyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "status", "orders_seen", "orders_created",
                    "orders_updated", "lines_written", "issues_found", "triggered_by")
    list_filter = ("status",)
    readonly_fields = tuple(f.name for f in OmsSyncRun._meta.fields)


@admin.register(ProcessingEvent)
class ProcessingEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event", "entity_type", "entity_id",
                    "source", "result", "actor")
    list_filter = ("event", "source", "result", "entity_type")
    search_fields = ("correlation_id", "entity_id")
    readonly_fields = tuple(f.name for f in ProcessingEvent._meta.fields)
