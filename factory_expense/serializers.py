"""
factory_expense/serializers.py

The board's payload is built as plain dicts in ``services`` and returned as-is,
so only the two settings models need serializers here. Rates are not among them
— they belong to ``cost_master`` and are edited in Admin › Cost Master.
"""

from rest_framework import serializers

from .models import FactoryExpenseSettings, MonthlyBudget


class MonthlyBudgetSerializer(serializers.ModelSerializer):
    bucket_display = serializers.CharField(source="get_bucket_display", read_only=True)

    class Meta:
        model = MonthlyBudget
        fields = [
            "id",
            "bucket",
            "bucket_display",
            "month",
            "amount",
            "notes",
            "is_active",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class FactoryExpenseSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = FactoryExpenseSettings
        fields = [
            "show_labour",
            "show_salary",
            "show_electricity",
            "show_maintenance",
            "maintenance_include_spares",
            "maintenance_include_indents",
            "electricity_only_company_meters",
            "refresh_seconds",
            "rotate_seconds",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]
