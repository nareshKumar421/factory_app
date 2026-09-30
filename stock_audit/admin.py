from django.contrib import admin

from .models import StockAudit, StockAuditCount, StockAuditLine


@admin.register(StockAudit)
class StockAuditAdmin(admin.ModelAdmin):
    list_display = ('id', 'company', 'warehouse_code', 'status', 'started_by', 'started_at',
                    'closed_at')
    list_filter = ('company', 'status')
    search_fields = ('warehouse_code', 'warehouse_name')


@admin.register(StockAuditLine)
class StockAuditLineAdmin(admin.ModelAdmin):
    list_display = ('audit', 'item_code', 'item_name', 'category', 'sap_qty', 'counted_qty')
    list_filter = ('category', 'in_sap')
    search_fields = ('item_code', 'item_name')


@admin.register(StockAuditCount)
class StockAuditCountAdmin(admin.ModelAdmin):
    list_display = ('line', 'qty', 'counted_by', 'counted_at', 'voided_at')
