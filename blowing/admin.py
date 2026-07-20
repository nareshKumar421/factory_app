from django.contrib import admin

from .models import (
    BlowingMachine, PreformSpec, BlowingRateConfig, BlowingRun, BlowingRunCost,
    BottleBuyPrice, BlowingSegment, BlowingBreakdown, BlowingBreakdownCategory,
    BlowingAuditLog,
)


@admin.register(BlowingMachine)
class BlowingMachineAdmin(admin.ModelAdmin):
    list_display = ('name', 'company', 'heads', 'is_active')
    list_filter = ('company', 'is_active')
    search_fields = ('name',)


@admin.register(PreformSpec)
class PreformSpecAdmin(admin.ModelAdmin):
    list_display = ('make', 'gram', 'company', 'preforms_per_box', 'is_active')
    list_filter = ('company', 'make', 'is_active')
    search_fields = ('make', 'sap_item_code')


@admin.register(BlowingRateConfig)
class BlowingRateConfigAdmin(admin.ModelAdmin):
    list_display = ('company', 'effective_from', 'operator_rate_per_day',
                    'labour_rate_per_day', 'electricity_rate_per_unit', 'is_active')
    list_filter = ('company', 'is_active')


class BlowingRunCostInline(admin.StackedInline):
    model = BlowingRunCost
    extra = 0
    can_delete = False


@admin.register(BlowingRun)
class BlowingRunAdmin(admin.ModelAdmin):
    list_display = ('date', 'run_number', 'company', 'machine', 'preform_spec',
                    'total_counter_production', 'rejection_pcs', 'status')
    list_filter = ('company', 'status', 'machine')
    search_fields = ('run_number',)
    date_hierarchy = 'date'
    inlines = [BlowingRunCostInline]


@admin.register(BottleBuyPrice)
class BottleBuyPriceAdmin(admin.ModelAdmin):
    list_display = ('preform_spec', 'company', 'supplier_name', 'buy_price',
                    'effective_from', 'is_active')
    list_filter = ('company', 'is_active')
    search_fields = ('supplier_name',)


@admin.register(BlowingBreakdownCategory)
class BlowingBreakdownCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'company', 'is_active')
    list_filter = ('company', 'is_active')


@admin.register(BlowingSegment)
class BlowingSegmentAdmin(admin.ModelAdmin):
    list_display = ('blowing_run', 'start_time', 'end_time', 'produced_pcs', 'is_active')
    list_filter = ('is_active',)


@admin.register(BlowingBreakdown)
class BlowingBreakdownAdmin(admin.ModelAdmin):
    list_display = ('blowing_run', 'breakdown_category', 'start_time', 'end_time',
                    'breakdown_minutes', 'is_active')
    list_filter = ('is_active', 'is_unrecovered')


@admin.register(BlowingAuditLog)
class BlowingAuditLogAdmin(admin.ModelAdmin):
    list_display = ('entity_type', 'entity_id', 'action', 'user', 'created_at')
    list_filter = ('company', 'entity_type', 'action')
