from django.contrib import admin
from .models import ProductionPlan, PlanMaterialRequirement, WeeklyPlan, DailyProductionEntry


class PlanMaterialInline(admin.TabularInline):
    model = PlanMaterialRequirement
    extra = 0
    readonly_fields = ['component_code', 'component_name', 'required_qty', 'uom', 'warehouse_code']
    can_delete = False


class WeeklyPlanInline(admin.TabularInline):
    model = WeeklyPlan
    extra = 0
    readonly_fields = [
        'week_number', 'week_label', 'start_date', 'end_date',
        'target_qty', 'produced_qty', 'status',
    ]
    can_delete = False


@admin.register(ProductionPlan)
class ProductionPlanAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'item_code', 'item_name', 'planned_qty', 'completed_qty',
        'status', 'sap_posting_status', 'sap_doc_num', 'due_date', 'company',
    ]
    list_filter = ['status', 'sap_posting_status', 'due_date', 'company']
    search_fields = ['item_code', 'item_name', 'sap_doc_num']
    readonly_fields = [
        'sap_doc_entry', 'sap_doc_num', 'sap_status', 'sap_error_message',
        'sap_posting_status', 'completed_qty',
        'created_by', 'created_at', 'closed_by', 'closed_at', 'updated_at',
    ]
    inlines = [PlanMaterialInline, WeeklyPlanInline]
    fieldsets = [
        ('Plan Details', {
            'fields': [
                'company', 'item_code', 'item_name', 'uom', 'warehouse_code',
                'planned_qty', 'completed_qty',
                'target_start_date', 'due_date', 'branch_id', 'remarks',
            ],
        }),
        ('Status', {
            'fields': ['status', 'sap_posting_status'],
        }),
        ('SAP Sync', {
            'fields': ['sap_doc_entry', 'sap_doc_num', 'sap_status', 'sap_error_message'],
            'classes': ['collapse'],
        }),
        ('Audit', {
            'fields': ['created_by', 'created_at', 'closed_by', 'closed_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]


class DailyEntryInline(admin.TabularInline):
    model = DailyProductionEntry
    extra = 0
    readonly_fields = ['production_date', 'produced_qty', 'shift', 'remarks', 'recorded_by']
    can_delete = False


@admin.register(WeeklyPlan)
class WeeklyPlanAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'get_item_name', 'week_number', 'week_label',
        'start_date', 'end_date', 'target_qty', 'produced_qty', 'status',
    ]
    list_filter = ['status']
    search_fields = ['production_plan__item_code', 'production_plan__item_name', 'week_label']
    readonly_fields = ['produced_qty', 'status', 'created_by', 'created_at', 'updated_at']
    inlines = [DailyEntryInline]

    def get_item_name(self, obj):
        return obj.production_plan.item_name
    get_item_name.short_description = 'Product'


@admin.register(DailyProductionEntry)
class DailyProductionEntryAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'get_item_name', 'get_week_label',
        'production_date', 'produced_qty', 'shift', 'recorded_by',
    ]
    list_filter = ['production_date', 'shift']
    search_fields = [
        'weekly_plan__production_plan__item_code',
        'weekly_plan__production_plan__item_name',
    ]
    readonly_fields = ['recorded_by', 'created_at', 'updated_at']

    def get_item_name(self, obj):
        return obj.weekly_plan.production_plan.item_name
    get_item_name.short_description = 'Product'

    def get_week_label(self, obj):
        return obj.weekly_plan.week_label or f"Week {obj.weekly_plan.week_number}"
    get_week_label.short_description = 'Week'
