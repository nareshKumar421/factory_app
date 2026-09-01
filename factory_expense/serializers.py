"""
factory_expense/serializers.py

The board's payload is built as plain dicts in ``services`` and returned as-is,
so only the configuration models need serializers here.
"""

from rest_framework import serializers

from accounts.models import Department

from .models import (
    DepartmentSalaryConfig,
    FactoryExpenseSettings,
    LabourRateConfig,
    MonthlyBudget,
)


class LabourRateConfigSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    shift_display = serializers.CharField(source="get_shift_display", read_only=True)

    class Meta:
        model = LabourRateConfig
        fields = [
            "id",
            "department",
            "department_name",
            "shift",
            "shift_display",
            "rate_per_person_per_day",
            "effective_from",
            "notes",
            "is_active",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class DepartmentSalaryConfigSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    per_employee = serializers.SerializerMethodField()

    class Meta:
        model = DepartmentSalaryConfig
        fields = [
            "id",
            "department",
            "department_name",
            "month",
            "employee_count",
            "monthly_amount",
            "per_employee",
            "notes",
            "is_active",
            "updated_at",
        ]
        read_only_fields = ["id", "per_employee", "updated_at"]

    def get_per_employee(self, obj):
        if not obj.employee_count:
            return None
        return round(float(obj.monthly_amount) / obj.employee_count, 2)


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


class DepartmentOptionSerializer(serializers.ModelSerializer):
    """The department dropdown both configuration tabs share."""

    class Meta:
        model = Department
        fields = ["id", "name"]
