from django.contrib import admin
from .models import (
    ProductionLine, Machine, MachineChecklistTemplate,
    ProductionRun, ProductionLog, MachineBreakdown,
    ProductionMaterialUsage, MachineRuntime, ProductionManpower,
    LineClearance, LineClearanceItem,
    MachineChecklistEntry, WasteLog,
)


# ---------------------------------------------------------------------------
# Master Data
# ---------------------------------------------------------------------------

@admin.register(ProductionLine)
class ProductionLineAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'company', 'is_active', 'created_at']
    list_filter = ['is_active', 'company']
    search_fields = ['name']


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'machine_type', 'line', 'company', 'is_active']
    list_filter = ['machine_type', 'is_active', 'company']
    search_fields = ['name']


@admin.register(MachineChecklistTemplate)
class MachineChecklistTemplateAdmin(admin.ModelAdmin):
    list_display = ['id', 'machine_type', 'task', 'frequency', 'sort_order', 'is_active']
    list_filter = ['machine_type', 'frequency', 'is_active']
    search_fields = ['task']


# ---------------------------------------------------------------------------
# Production Runs
# ---------------------------------------------------------------------------

class ProductionLogInline(admin.TabularInline):
    model = ProductionLog
    extra = 0
    readonly_fields = [
        'time_slot', 'time_start', 'time_end', 'produced_cases',
        'machine_status', 'recd_minutes', 'breakdown_detail',
    ]
    can_delete = False


class MachineBreakdownInline(admin.TabularInline):
    model = MachineBreakdown
    extra = 0
    readonly_fields = [
        'machine', 'start_time', 'end_time', 'breakdown_minutes',
        'type', 'reason',
    ]
    can_delete = False


@admin.register(ProductionRun)
class ProductionRunAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'run_number', 'date', 'line', 'status',
        'total_production', 'total_breakdown_time', 'company',
    ]
    list_filter = ['status', 'date', 'company']
    search_fields = ['brand', 'pack', 'sap_order_no']
    readonly_fields = [
        'run_number', 'total_production', 'total_minutes_pe', 'total_minutes_me',
        'total_breakdown_time', 'line_breakdown_time', 'external_breakdown_time',
        'unrecorded_time', 'created_by', 'created_at', 'updated_at',
    ]
    inlines = [ProductionLogInline, MachineBreakdownInline]


@admin.register(ProductionLog)
class ProductionLogAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'time_slot', 'produced_cases',
        'machine_status', 'recd_minutes',
    ]
    list_filter = ['machine_status']


@admin.register(MachineBreakdown)
class MachineBreakdownAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'machine', 'start_time', 'end_time',
        'breakdown_minutes', 'type', 'reason',
    ]
    list_filter = ['type']


# ---------------------------------------------------------------------------
# Material, Runtime, Manpower
# ---------------------------------------------------------------------------

@admin.register(ProductionMaterialUsage)
class ProductionMaterialUsageAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'material_name', 'opening_qty',
        'issued_qty', 'closing_qty', 'wastage_qty', 'batch_number',
    ]
    list_filter = ['batch_number']
    search_fields = ['material_name', 'material_code']


@admin.register(MachineRuntime)
class MachineRuntimeAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'machine_type',
        'runtime_minutes', 'downtime_minutes',
    ]
    list_filter = ['machine_type']


@admin.register(ProductionManpower)
class ProductionManpowerAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'shift', 'worker_count',
        'supervisor', 'engineer',
    ]
    list_filter = ['shift']


# ---------------------------------------------------------------------------
# Line Clearance
# ---------------------------------------------------------------------------

class LineClearanceItemInline(admin.TabularInline):
    model = LineClearanceItem
    extra = 0
    readonly_fields = ['checkpoint', 'sort_order']


@admin.register(LineClearance)
class LineClearanceAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'date', 'line', 'status', 'qa_approved',
        'created_by', 'company',
    ]
    list_filter = ['status', 'qa_approved', 'date']
    inlines = [LineClearanceItemInline]


# ---------------------------------------------------------------------------
# Machine Checklists
# ---------------------------------------------------------------------------

@admin.register(MachineChecklistEntry)
class MachineChecklistEntryAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'machine', 'machine_type', 'date', 'task_description',
        'frequency', 'status',
    ]
    list_filter = ['machine_type', 'frequency', 'status', 'date']
    search_fields = ['task_description']


# ---------------------------------------------------------------------------
# Waste Management
# ---------------------------------------------------------------------------

@admin.register(WasteLog)
class WasteLogAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'production_run', 'material_name', 'wastage_qty',
        'wastage_approval_status',
    ]
    list_filter = ['wastage_approval_status']
    search_fields = ['material_name', 'material_code']
