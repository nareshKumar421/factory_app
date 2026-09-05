from django.contrib import admin

from .models import OrgDepartment, OrgFunction


class OrgFunctionInline(admin.TabularInline):
    model = OrgFunction
    extra = 0
    fields = ("sort_order", "name", "owners", "level_1", "level_2")


@admin.register(OrgDepartment)
class OrgDepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order")
    ordering = ("sort_order", "name")
    inlines = [OrgFunctionInline]


@admin.register(OrgFunction)
class OrgFunctionAdmin(admin.ModelAdmin):
    list_display = ("department", "name", "owners", "level_1", "level_2", "sort_order")
    list_filter = ("department",)
    list_select_related = ("department",)
    search_fields = ("name",)
