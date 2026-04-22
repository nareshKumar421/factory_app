from django.contrib import admin
from .models import (
    Pallet, Box, LabelPrintLog, PalletMovement, BoxMovement, ScanLog, LooseStock,
)


class BoxInline(admin.TabularInline):
    model = Box
    extra = 0
    fields = ['box_barcode', 'item_code', 'qty', 'status', 'current_warehouse']
    readonly_fields = ['box_barcode', 'item_code', 'qty', 'status', 'current_warehouse']


@admin.register(Pallet)
class PalletAdmin(admin.ModelAdmin):
    list_display = [
        'pallet_id', 'item_code', 'batch_number', 'box_count',
        'total_qty', 'current_warehouse', 'status', 'created_at',
    ]
    list_filter = ['status', 'current_warehouse', 'created_at']
    search_fields = ['pallet_id', 'item_code', 'batch_number']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [BoxInline]


@admin.register(Box)
class BoxAdmin(admin.ModelAdmin):
    list_display = [
        'box_barcode', 'item_code', 'batch_number', 'qty',
        'pallet', 'current_warehouse', 'status', 'created_at',
    ]
    list_filter = ['status', 'current_warehouse', 'created_at']
    search_fields = ['box_barcode', 'item_code', 'batch_number']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(LabelPrintLog)
class LabelPrintLogAdmin(admin.ModelAdmin):
    list_display = [
        'label_type', 'reference_code', 'print_type',
        'printed_by', 'printed_at',
    ]
    list_filter = ['label_type', 'print_type', 'printed_at']
    search_fields = ['reference_code']
    readonly_fields = ['printed_at']


@admin.register(PalletMovement)
class PalletMovementAdmin(admin.ModelAdmin):
    list_display = [
        'pallet', 'movement_type', 'from_warehouse', 'to_warehouse',
        'quantity', 'performed_by', 'performed_at',
    ]
    list_filter = ['movement_type', 'performed_at']
    readonly_fields = ['performed_at']


@admin.register(BoxMovement)
class BoxMovementAdmin(admin.ModelAdmin):
    list_display = [
        'box', 'movement_type', 'from_warehouse', 'to_warehouse',
        'performed_by', 'performed_at',
    ]
    list_filter = ['movement_type', 'performed_at']
    readonly_fields = ['performed_at']


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin):
    list_display = [
        'scan_type', 'barcode_raw', 'entity_type', 'scan_result',
        'scanned_by', 'scanned_at',
    ]
    list_filter = ['scan_type', 'entity_type', 'scan_result', 'scanned_at']
    search_fields = ['barcode_raw']
    readonly_fields = ['scanned_at']


@admin.register(LooseStock)
class LooseStockAdmin(admin.ModelAdmin):
    list_display = [
        'item_code', 'batch_number', 'qty', 'original_qty',
        'reason', 'source_box', 'status', 'current_warehouse', 'created_at',
    ]
    list_filter = ['status', 'reason', 'current_warehouse', 'created_at']
    search_fields = ['item_code', 'batch_number']
    readonly_fields = ['created_at', 'updated_at']
