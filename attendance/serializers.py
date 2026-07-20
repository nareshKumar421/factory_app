from rest_framework import serializers

from .models import Employee, AttendanceRecord


class EmployeeSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)

    class Meta:
        model = Employee
        fields = "__all__"


class AttendanceRecordSerializer(serializers.ModelSerializer):
    employee_detail = EmployeeSerializer(source="employee", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = AttendanceRecord
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]
