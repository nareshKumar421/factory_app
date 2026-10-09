from decimal import Decimal

from rest_framework import serializers

from .constants import COMPANY_LABELS
from .models import ExpenseClaim, ExpenseClaimAttachment


def _name(user):
    if user is None:
        return None
    return user.full_name or user.email


class ExpenseClaimAttachmentSerializer(serializers.ModelSerializer):
    """One file on an expense. ``url`` is absolute, built from the request."""

    url = serializers.SerializerMethodField()
    uploaded_at = serializers.DateTimeField(source="created_at")

    class Meta:
        model = ExpenseClaimAttachment
        fields = ["id", "original_filename", "size_bytes", "url", "uploaded_at"]
        read_only_fields = fields

    def get_url(self, attachment):
        if not attachment.file:
            return None
        url = attachment.file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class ExpenseClaimSerializer(serializers.ModelSerializer):
    """One claim, as both lists read it. Output only."""

    status_label = serializers.CharField(source="get_status_display")
    company_code = serializers.CharField(source="company.code")
    company_name = serializers.SerializerMethodField()
    submitted_by = serializers.IntegerField(source="created_by_id")
    submitted_by_name = serializers.SerializerMethodField()
    submitted_at = serializers.DateTimeField(source="created_at")
    decided_by_name = serializers.SerializerMethodField()
    attachments = ExpenseClaimAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = ExpenseClaim
        fields = [
            "id",
            "company_code",
            "company_name",
            "budget_code",
            "budget_name",
            "gl_account_code",
            "gl_account_name",
            "gl_description",
            "comment",
            "amount",
            "attachments",
            "status",
            "status_label",
            "submitted_by",
            "submitted_by_name",
            "submitted_at",
            "decided_at",
            "decided_by_name",
            "decision_note",
        ]
        read_only_fields = fields

    def get_company_name(self, obj):
        return COMPANY_LABELS.get(obj.company.code, obj.company.name)

    def get_submitted_by_name(self, obj):
        return _name(obj.created_by)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by)


class SubmitClaimSerializer(serializers.Serializer):
    """The whole of the expense form. One of the G/L account or its description."""

    company = serializers.CharField(max_length=50)
    budget_code = serializers.CharField(max_length=32)
    gl_account_code = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    gl_description = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    comment = serializers.CharField(max_length=2000)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )


class DecideClaimSerializer(serializers.Serializer):
    approve = serializers.BooleanField()
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class BudgetSerializer(serializers.Serializer):
    budget_code = serializers.CharField()
    budget_name = serializers.CharField()
    is_default = serializers.BooleanField()


class GLAccountSerializer(serializers.Serializer):
    account_code = serializers.CharField()
    account_name = serializers.CharField()
