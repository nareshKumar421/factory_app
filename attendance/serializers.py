"""
Serializers for the attendance sheet.

The one shape that matters is :class:`DailyAttendanceSerializer`: it always
returns **both** the machine's reading and the effective one, whatever the
client asked for. The dashboard's "show only punch-machine data" default is a
presentation choice made in the browser, not a narrower payload -- if the API
withheld the override, toggling the column on would need a second round trip,
and worse, two clients could disagree about what the day says.
"""

from rest_framework import serializers

from employee_hierarchy.models import Employee

from .models import (
    AttendanceOverrideLog,
    AttendanceRecord,
    AttendanceStatus,
    DailyAttendance,
    OverrideReason,
)


class AttendanceEmployeeSerializer(serializers.ModelSerializer):
    """The directory entry, as the attendance screens need it.

    Read-only everywhere in this module. Employees are maintained in
    ``employee_hierarchy``; a second place to create them is what this module
    used to have, and it meant somebody could exist for attendance and not for
    HR.
    """

    department_name = serializers.CharField(source="department.name", read_only=True, default=None)
    designation_name = serializers.CharField(source="designation.name", read_only=True, default=None)

    class Meta:
        model = Employee
        fields = [
            "id", "employee_code", "full_name", "department", "department_name",
            "designation_name", "sap_segment", "employment_status",
        ]
        read_only_fields = fields


class OverrideLogSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source="performed_by.full_name", read_only=True, default=None
    )
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    reason_code_display = serializers.CharField(source="get_reason_code_display", read_only=True)

    class Meta:
        model = AttendanceOverrideLog
        fields = [
            "id", "action", "action_display", "from_status", "to_status", "machine_status",
            "reason_code", "reason_code_display", "reason",
            "performed_by", "performed_by_name", "performed_at",
        ]
        read_only_fields = fields


class DailyAttendanceSerializer(serializers.ModelSerializer):
    employee_detail = AttendanceEmployeeSerializer(source="employee", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    department_name = serializers.CharField(
        source="employee.department.name", read_only=True, default=None
    )
    # The branch HR files the person under -- a label on the sheet, like the
    # department, so the day can be read branch by branch.
    branch_name = serializers.CharField(
        source="employee.branch.name", read_only=True, default=None
    )
    machine_status_display = serializers.CharField(source="get_machine_status_display", read_only=True)
    effective_status_display = serializers.CharField(source="get_effective_status_display", read_only=True)
    override_reason_code_display = serializers.CharField(
        source="get_override_reason_code_display", read_only=True
    )
    overridden_by_name = serializers.CharField(
        source="overridden_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = DailyAttendance
        fields = [
            "id", "date",
            "employee", "employee_code", "employee_name", "department_name", "branch_name",
            "employee_detail",
            # What the machine said -- never written through this API.
            "machine_status", "machine_status_display", "machine_first_punch",
            "machine_last_punch", "machine_punch_count", "machine_worked_minutes", "devices",
            # What stands.
            "effective_status", "effective_status_display", "is_overridden",
            "override_reason_code", "override_reason_code_display", "override_reason",
            "overridden_by", "overridden_by_name", "overridden_at",
            "synced_at",
        ]
        # Everything is read-only: a status only ever changes through the
        # override action, which requires a reason and writes the trail. A
        # plain PATCH would bypass both.
        read_only_fields = fields


class OverrideRequestSerializer(serializers.Serializer):
    """The body of an override. Both halves of the reason are required.

    ``reason`` is checked for real content rather than mere presence -- a
    single space would satisfy ``required`` and tell a future reader nothing.
    """

    status = serializers.ChoiceField(choices=AttendanceStatus.choices)
    reason_code = serializers.ChoiceField(choices=OverrideReason.choices)
    reason = serializers.CharField(max_length=2000)

    def validate_reason(self, value):
        text = value.strip()
        if len(text) < 3:
            raise serializers.ValidationError(
                "Say why the status is being changed — this is what payroll will read later."
            )
        return text


class RevertRequestSerializer(serializers.Serializer):
    """Reverting is a decision too, so it takes a note. It may be brief."""

    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class AttendanceRecordSerializer(serializers.ModelSerializer):
    employee_detail = AttendanceEmployeeSerializer(source="employee", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=None
    )

    class Meta:
        model = AttendanceRecord
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]
