"""Serializers for the SAP invoice-approval module.

Reads are built straight from SAP (HANA) by ``sap_client`` and passed through, so
there are no output serializers for invoices/history. We only validate input (the
PATCH body and the ``?status=`` query param) and serialize our own local audit rows.
"""
from rest_framework import serializers

from .models import InvoiceApprovalAudit

# The three states an approval request can be in — also the FE tabs.
VALID_STATUSES = ("PENDING", "APPROVED", "REJECTED")
DECISION_CHOICES = ("APPROVED", "REJECTED")


class InvoiceStatusUpdateSerializer(serializers.Serializer):
    """Validates the approve/reject body sent to PATCH .../status/.

    ``so_number``/``party_name``/``total_amount`` are optional display context the
    frontend already has; they are stored on the local audit row so we don't need
    an extra SAP round-trip just to label it.
    """

    status = serializers.ChoiceField(choices=DECISION_CHOICES)
    rejection_reason = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True
    )
    so_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    party_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    total_amount = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=2
    )

    def validate(self, attrs):
        if attrs["status"] == "REJECTED" and not (attrs.get("rejection_reason") or "").strip():
            raise serializers.ValidationError(
                {"rejection_reason": "This field is required when status is REJECTED."}
            )
        if attrs["status"] == "APPROVED":
            attrs.pop("rejection_reason", None)
        return attrs


class InvoiceListQuerySerializer(serializers.Serializer):
    """Validates the list query: warehouse (required) + optional status tab."""

    whs = serializers.CharField()
    status = serializers.ChoiceField(choices=VALID_STATUSES, required=False)


class InvoiceApprovalAuditSerializer(serializers.ModelSerializer):
    acted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceApprovalAudit
        fields = [
            "id",
            "approval_code",
            "draft_entry",
            "so_number",
            "party_name",
            "total_amount",
            "decision",
            "rejection_reason",
            "sap_message",
            "company",
            "acted_by_name",
            "created_at",
        ]

    def get_acted_by_name(self, obj):
        user = obj.created_by
        if not user:
            return None
        return getattr(user, "full_name", "") or user.get_username()
