from django.contrib import admin

from .models import OrgChartSettings, OrgDepartment, OrgFunction


@admin.register(OrgChartSettings)
class OrgChartSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "plant_name", "plant_head")
    list_select_related = ("company",)


class OrgFunctionInline(admin.TabularInline):
    model = OrgFunction
    extra = 0
    fields = ("sort_order", "name", "subtitle", "owners", "level_1", "level_2")


@admin.register(OrgDepartment)
class OrgDepartmentAdmin(admin.ModelAdmin):
    list_display = ("company", "name", "head", "sort_order")
    list_filter = ("company",)
    list_select_related = ("company",)
    ordering = ("company", "sort_order", "name")
    inlines = [OrgFunctionInline]


@admin.register(OrgFunction)
class OrgFunctionAdmin(admin.ModelAdmin):
    list_display = (
        "department",
        "name",
        "subtitle",
        "owners",
        "level_1",
        "level_2",
        "sort_order",
    )
    list_filter = ("department__company", "department")
    list_select_related = ("department", "department__company")
    search_fields = ("name", "subtitle")
