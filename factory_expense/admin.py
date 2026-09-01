from django.contrib import admin

from .models import FactoryExpenseSettings, MonthlyBudget


@admin.register(MonthlyBudget)
class MonthlyBudgetAdmin(admin.ModelAdmin):
    list_display = ("company", "bucket", "month", "amount", "is_active")
    list_filter = ("company", "bucket", "is_active")
    date_hierarchy = "month"


@admin.register(FactoryExpenseSettings)
class FactoryExpenseSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "refresh_seconds", "rotate_seconds", "updated_at")
    list_filter = ("company",)
