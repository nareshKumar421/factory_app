from django.contrib import admin

from .models import MachinePick, PlanCheck, PlanningSheet, TomorrowPlan


@admin.register(PlanningSheet)
class PlanningSheetAdmin(admin.ModelAdmin):
    list_display = ("file_name", "company", "stock_date", "line_count", "uploaded_by", "uploaded_at")
    list_filter = ("company",)


@admin.register(TomorrowPlan)
class TomorrowPlanAdmin(admin.ModelAdmin):
    list_display = ("company", "for_date", "read_at", "trigger", "built_by")
    list_filter = ("company", "trigger")
    exclude = ("inputs", "plan")


@admin.register(MachinePick)
class MachinePickAdmin(admin.ModelAdmin):
    list_display = ("plan", "machine", "name", "rank", "picked_by_name", "picked_at", "cleared_at")
    list_filter = ("machine",)


@admin.register(PlanCheck)
class PlanCheckAdmin(admin.ModelAdmin):
    list_display = ("company", "for_date", "run_at", "red")
