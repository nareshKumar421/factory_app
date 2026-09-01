from django.contrib import admin

from .models import (
    DepartmentSalaryConfig,
    FactoryExpenseSettings,
    LabourRateConfig,
    MonthlyBudget,
)


@admin.register(LabourRateConfig)
class LabourRateConfigAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "department",
        "shift",
        "rate_per_person_per_day",
        "effective_from",
        "is_active",
    )
    list_filter = ("company", "shift", "is_active")
    search_fields = ("department__name", "notes")
    date_hierarchy = "effective_from"


@admin.register(DepartmentSalaryConfig)
class DepartmentSalaryConfigAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "department",
        "month",
        "employee_count",
        "monthly_amount",
        "is_active",
    )
    list_filter = ("company", "is_active")
    search_fields = ("department__name", "notes")
    date_hierarchy = "month"


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("company", "bucket", "month", "amount", "is_active")
    list_filter = ("company", "bucket", "is_active")
    date_hierarchy = "month"


@admin.register(FactoryExpenseSettings)
class FactoryExpenseSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "refresh_seconds", "rotate_seconds", "updated_at")
    list_filter = ("company",)
