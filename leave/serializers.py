"""
Serializers for the leave module.

Read serializers carry the **decision context** on every row -- who is meant to
decide it, and whether *you* can. The manager's queue would otherwise have to
ask the routing rules again per row on the client, and a client that computes
authorisation is a client that gets it wrong. The server already knows; it
says so.
"""

from rest_framework import serializers

from .constants import DayPortion, LeaveRequestStatus
from .models import Holiday, LeaveApproval, LeaveRequest, LeaveRequestDay, LeaveType
from .routing import authority_of, can_cancel, responsible_manager


class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = [
            "id",
            "code",
            "name",
            "description",
            "is_paid",
            "allow_half_day",
            "requires_document",
            "annual_quota",
            "max_consecutive_days",
            "status",
            "sort_order",
        ]
        read_only_fields = ["id"]


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = ["id", "date", "name", "is_optional"]
        read_only_fields = ["id"]


class LeaveRequestDaySerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequestDay
        fields = ["id", "date", "portion", "status", "is_projected", "projected_at"]


class LeaveApprovalSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source="performed_by.full_name", default="", read_only=True
    )

    class Meta:
        model = LeaveApproval
        fields = [
            "id",
            "action",
            "from_status",
            "to_status",
            "comment",
            "authority",
            "performed_by",
            "performed_by_name",
            "performed_at",
        ]


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    department_name = serializers.CharField(
        source="employee.department.name", default="", read_only=True
    )
    leave_type_name = serializers.CharField(source="leave_type.name", read_only=True)
    leave_type_code = serializers.CharField(source="leave_type.code", read_only=True)
    applied_by_name = serializers.CharField(
        source="applied_by.full_name", default="", read_only=True
    )
    decided_by_name = serializers.CharField(
        source="decided_by.full_name", default="", read_only=True
    )
    days = LeaveRequestDaySerializer(many=True, read_only=True)

    #: Who *ought* to decide this, by the tree. Shown to the applicant so they
    #: know who they are waiting on, which is the first thing anybody asks.
    responsible_manager_name = serializers.SerializerMethodField()
    #: Whether the caller may act on it, and on what footing. Computed here so
    #: the client never has to re-derive an authorisation rule.
    my_authority = serializers.SerializerMethodField()
    can_decide = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    can_withdraw = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id",
            "employee",
            "employee_code",
            "employee_name",
            "department_name",
            "leave_type",
            "leave_type_code",
            "leave_type_name",
            "from_date",
            "to_date",
            "portion",
            "total_days",
            "reason",
            "contact_number",
            "document",
            "status",
            "applied_by",
            "applied_by_name",
            "applied_at",
            "decided_by",
            "decided_by_name",
            "decided_at",
            "decision_note",
            "days",
            "responsible_manager_name",
            "my_authority",
            "can_decide",
            "can_cancel",
            "can_withdraw",
        ]

    def _user(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    def get_responsible_manager_name(self, obj):
        manager = responsible_manager(obj.employee)
        return manager.full_name if manager is not None else ""

    def get_my_authority(self, obj):
        if obj.status not in (LeaveRequestStatus.PENDING,):
            return ""
        return authority_of(self._user(), obj) or ""

    def get_can_decide(self, obj):
        if obj.status != LeaveRequestStatus.PENDING:
            return False
        return authority_of(self._user(), obj) is not None

    def get_can_cancel(self, obj):
        if obj.status != LeaveRequestStatus.APPROVED:
            return False
        return can_cancel(self._user(), obj)

    def get_can_withdraw(self, obj):
        """Only the applicant, and only before anybody decided."""
        user = self._user()
        if obj.status != LeaveRequestStatus.PENDING or user is None:
            return False
        if not user.is_authenticated:
            return False
        return obj.applied_by_id == user.pk or (
            obj.employee.user_id is not None and obj.employee.user_id == user.pk
        )


class ApplyLeaveSerializer(serializers.Serializer):
    """The application form.

    ``employee`` is optional: left out it means "me", which is the common case
    and saves the client resolving its own employee id. Supplying somebody else
    is what the time office does, and needs ``can_apply_leave_for_others``.
    """

    employee = serializers.IntegerField(required=False)
    leave_type = serializers.IntegerField()
    from_date = serializers.DateField()
    to_date = serializers.DateField()
    portion = serializers.ChoiceField(
        choices=DayPortion.choices, default=DayPortion.FULL
    )
    reason = serializers.CharField()
    contact_number = serializers.CharField(required=False, allow_blank=True, default="")
    document = serializers.FileField(required=False, allow_null=True)


class DecisionSerializer(serializers.Serializer):
    """Approve or reject. ``only_dates`` is what makes a partial approval."""

    comment = serializers.CharField(required=False, allow_blank=True, default="")
    only_dates = serializers.ListField(
        child=serializers.DateField(), required=False, allow_empty=True
    )


class ReasonSerializer(serializers.Serializer):
    """For the transitions where a reason is not optional."""

    comment = serializers.CharField()
