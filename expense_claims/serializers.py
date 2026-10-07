from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .constants import COMPANY_LABELS
from .models import ExpenseClaim


def _name(user):
    if user is None:
        return None
    return user.full_name or user.email


class ExpenseClaimSerializer(serializers.ModelSerializer):
    """One claim, as the approval list reads it. Output only."""

    status_label = serializers.CharField(source="get_status_display")
    company_code = serializers.CharField(source="company.code")
    company_name = serializers.SerializerMethodField()
    submitted_by = serializers.IntegerField(source="created_by_id")
    submitted_by_name = serializers.SerializerMethodField()
    submitted_at = serializers.DateTimeField(source="created_at")
    approver_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseClaim
        fields = [
            "id",
            "company_code",
            "company_name",
            "budget_id",
            "budget_name",
            "gl_account_code",
            "gl_account_name",
            "comment",
            "amount",
            "status",
            "status_label",
            "submitted_by",
            "submitted_by_name",
            "submitted_at",
            "approver",
            "approver_name",
            "decided_at",
            "decided_by_name",
            "decision_note",
        ]
        read_only_fields = fields

    def get_company_name(self, obj):
        return COMPANY_LABELS.get(obj.company.code, obj.company.name)

    def get_submitted_by_name(self, obj):
        return _name(obj.created_by)

    def get_approver_name(self, obj):
        return _name(obj.approver)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by)


class SubmitClaimSerializer(serializers.Serializer):
    """The whole of the entry page."""

    company = serializers.CharField(max_length=50)
    budget_id = serializers.IntegerField(min_value=1)
    gl_account_code = serializers.CharField(max_length=32)
    comment = serializers.CharField(max_length=2000)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    approver = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.filter(is_active=True)
    )


class DecideClaimSerializer(serializers.Serializer):
    approve = serializers.BooleanField()
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class ApproverSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()

    def get_name(self, obj):
        return _name(obj)


class GLAccountSerializer(serializers.Serializer):
    account_code = serializers.CharField()
    account_name = serializers.CharField()
