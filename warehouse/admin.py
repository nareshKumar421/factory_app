from django.contrib import admin
from .models import BOMRequest, BOMRequestLine, FinishedGoodsReceipt


class BOMRequestLineInline(admin.TabularInline):
    model = BOMRequestLine
    extra = 0
    readonly_fields = ['created_at', 'updated_at']


@admin.register(BOMRequest)
class BOMRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'production_run', 'required_qty', 'status',
                    'material_issue_status', 'requested_by', 'created_at']
    list_filter = ['status', 'material_issue_status']
    inlines = [BOMRequestLineInline]


@admin.register(FinishedGoodsReceipt)
class FinishedGoodsReceiptAdmin(admin.ModelAdmin):
    list_display = ['id', 'production_run', 'item_code', 'good_qty',
                    'status', 'received_by', 'created_at']
    list_filter = ['status']
