from django.contrib import admin
from .models import (
    BOMRequest,
    BOMRequestLine,
    FinishedGoodsReceipt,
    PFStockMovement,
    PFStockMovementEvent,
    PFStockMovementLine,
    RawMaterialStock,
    RawMaterialStockEntry,
)


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


@admin.register(RawMaterialStock)
class RawMaterialStockAdmin(admin.ModelAdmin):
    """The register. Read-mostly here — the page is where quantities get set.

    Everything but the quantity is read-only: a change made here would bypass
    `rm_stock_service`, and so would go unrecorded in the change trail. Editing
    the quantity is left available for the one case admin is actually for —
    correcting a typo nobody can undo from the floor — and the trail's gap is
    then visible as a row whose `updated_at` has no matching entry.
    """

    list_display = ['warehouse_code', 'item_code', 'item_name', 'qty', 'uom',
                    'as_of_date', 'is_active', 'set_by', 'updated_at']
    list_filter = ['company', 'warehouse_code', 'is_active']
    search_fields = ['item_code', 'item_name']
    readonly_fields = ['company', 'warehouse_code', 'item_code', 'item_name',
                       'uom', 'set_by', 'created_at', 'updated_at']


@admin.register(RawMaterialStockEntry)
class RawMaterialStockEntryAdmin(admin.ModelAdmin):
    """The change trail. Append-only by nature, so entirely read-only here."""

    list_display = ['changed_at', 'warehouse_code', 'item_code', 'action',
                    'previous_qty', 'qty', 'changed_by']
    list_filter = ['company', 'action', 'warehouse_code']
    search_fields = ['item_code']
    date_hierarchy = 'changed_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class PFStockMovementLineInline(admin.TabularInline):
    model = PFStockMovementLine
    extra = 0
    readonly_fields = ['created_at', 'updated_at']


@admin.register(PFStockMovement)
class PFStockMovementAdmin(admin.ModelAdmin):
    """Declared outward movements. Read-mostly — the page is where they are filed.

    The route and the audit stamps are read-only: a change made here would
    bypass `pf_movement_service`, and so would skip both the manager check and
    the change trail. The date, vehicle and remarks stay editable for the one
    thing admin is actually for — fixing a typo nobody can undo from the floor.
    """

    list_display = ['entry_no', 'movement_date', 'from_warehouse',
                    'destination_kind', 'to_warehouse', 'to_company',
                    'is_active', 'created_by', 'created_at']
    list_filter = ['company', 'destination_kind', 'to_company', 'is_active',
                   'from_warehouse']
    search_fields = ['entry_no', 'vehicle_no', 'reference', 'to_warehouse',
                     'lines__item_code']
    date_hierarchy = 'movement_date'
    inlines = [PFStockMovementLineInline]
    readonly_fields = ['entry_no', 'company', 'from_warehouse', 'from_warehouse_name',
                       'created_by', 'updated_by', 'cancelled_by', 'cancelled_at',
                       'created_at', 'updated_at']


@admin.register(PFStockMovementEvent)
class PFStockMovementEventAdmin(admin.ModelAdmin):
    """The change trail. Append-only by nature, so entirely read-only here."""

    list_display = ['changed_at', 'movement', 'action', 'destination_kind',
                    'to_warehouse', 'line_count', 'total_pieces',
                    'total_litres', 'changed_by']
    list_filter = ['action', 'destination_kind']
    search_fields = ['movement__entry_no']
    date_hierarchy = 'changed_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
